import uuid
from datetime import datetime

import pytest
from sqlalchemy import select

from app.config import DEFAULT_TENANT_ID
from app.models.project import Content, ContentVersion, Project
from app.models.publish import PlatformAccount, PublishEvent, PublishJob
from app.publish.state import InvalidTransition, next_status
from app.services.publish import apply_action

OTHER_TENANT = "00000000-0000-0000-0000-000000000002"


async def _create_content(client, *, title="标题", body="正文", platform="wechat"):
    project = await client.post(
        "/api/projects", json={"title": f"发布测试-{uuid.uuid4().hex[:8]}"}
    )
    assert project.status_code == 201
    payload = {"platform": platform}
    if title:
        payload["title"] = title
    if body:
        payload["body"] = body
    content = await client.post(
        f"/api/projects/{project.json()['id']}/contents", json=payload
    )
    assert content.status_code == 201
    return content.json()


async def _create_job(
    client, content_id, *, platform="wechat", account_id=None, scheduled_at=None
):
    payload = {"content_id": str(content_id), "platform": platform}
    if account_id is not None:
        payload["account_id"] = str(account_id)
    if scheduled_at is not None:
        payload["scheduled_at"] = scheduled_at
    return await client.post("/api/publish/jobs", json=payload)


async def _seed_account(
    db_session, *, tenant_id=DEFAULT_TENANT_ID, platform="wechat", name=None
):
    account = PlatformAccount(
        tenant_id=tenant_id,
        platform=platform,
        name=name or f"账号-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(account)
    await db_session.commit()
    await db_session.refresh(account)
    return account


async def _seed_foreign_content(db_session):
    project = Project(tenant_id=OTHER_TENANT, title="外部项目")
    db_session.add(project)
    await db_session.flush()
    content = Content(
        tenant_id=OTHER_TENANT,
        project_id=project.id,
        platform="wechat",
        title="外部内容",
        current_version=1,
    )
    db_session.add(content)
    await db_session.flush()
    db_session.add(
        ContentVersion(
            tenant_id=OTHER_TENANT,
            content_id=content.id,
            version=1,
            title="外部内容",
            body="外部正文",
        )
    )
    await db_session.commit()
    await db_session.refresh(content)
    return content


def test_happy_path_transitions():
    status = "draft"
    for action, expected in [
        ("submit", "prechecking"),
        ("pass", "awaiting_review"),
        ("approve", "approved"),
        ("publish", "publishing"),
        ("succeed", "published"),
    ]:
        status = next_status(status, action)
        assert status == expected


def test_manual_export_path():
    assert next_status("draft", "export") == "exporting"
    assert next_status("exporting", "export_done") == "exported"


def test_failure_and_retry_paths():
    assert next_status("publishing", "fail") == "failed"
    assert next_status("failed", "retry") == "approved"
    assert next_status("prechecking", "fail") == "precheck_failed"
    assert next_status("precheck_failed", "submit") == "prechecking"
    assert next_status("awaiting_review", "reject") == "draft"


@pytest.mark.parametrize(
    "current", ["draft", "awaiting_review", "approved", "failed", "precheck_failed"]
)
def test_cancel_allowed(current):
    assert next_status(current, "cancel") == "cancelled"


@pytest.mark.parametrize(
    ("current", "action"),
    [
        ("draft", "approve"),
        ("published", "publish"),
        ("draft", "succeed"),
        ("cancelled", "submit"),
    ],
)
def test_illegal_transitions(current, action):
    with pytest.raises(InvalidTransition):
        next_status(current, action)


async def test_create_job_from_current_version(client):
    content = await _create_content(client)
    response = await _create_job(client, content["id"])

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "draft"
    assert body["content_id"] == content["id"]
    assert body["content_version"] == content["current_version"] == 1
    assert body["account_id"] is None
    assert body["checks"] == {}
    assert "tenant_id" not in body


async def test_scheduled_at_round_trips(client):
    content = await _create_content(client)
    response = await _create_job(
        client, content["id"], scheduled_at="2026-11-01T09:30:00+00:00"
    )

    assert response.status_code == 201
    scheduled = datetime.fromisoformat(response.json()["scheduled_at"])
    assert scheduled.replace(tzinfo=None) == datetime(2026, 11, 1, 9, 30)


async def test_approve_on_draft_returns_409(client):
    content = await _create_content(client)
    job = (await _create_job(client, content["id"])).json()

    response = await client.post(
        f"/api/publish/jobs/{job['id']}/actions", json={"action": "approve"}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "invalid_transition"


@pytest.mark.parametrize("action", ["publish", "pass", "succeed", "export"])
async def test_system_actions_are_forbidden(client, action):
    content = await _create_content(client)
    job = (await _create_job(client, content["id"])).json()

    response = await client.post(
        f"/api/publish/jobs/{job['id']}/actions", json={"action": action}
    )
    assert response.status_code == 403
    assert response.json()["code"] == "action_forbidden"


async def test_title_only_content_is_not_publishable(client):
    content = await _create_content(client, title="只有标题", body="")
    assert content["current_version"] == 1

    response = await _create_job(client, content["id"])
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "empty_content"
    assert body["message"] == "当前版本正文为空，无法发布"


async def test_content_without_version_is_not_publishable(client):
    content = await _create_content(client, title="", body="")
    assert content["current_version"] == 0

    response = await _create_job(client, content["id"])
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "empty_content"
    assert body["message"] == "内容还没有可发布的版本"


async def test_idempotent_replay_returns_same_job(client):
    content = await _create_content(client)
    first = await _create_job(client, content["id"])
    second = await _create_job(client, content["id"])

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]


async def test_idempotency_varies_by_platform_and_account(client, db_session):
    content = await _create_content(client)
    base = await _create_job(client, content["id"], platform="wechat")
    other_platform = await _create_job(client, content["id"], platform="douyin")
    assert base.status_code == 201
    assert other_platform.status_code == 201
    assert base.json()["id"] != other_platform.json()["id"]

    account_a = await _seed_account(db_session)
    account_b = await _seed_account(db_session)
    first = await _create_job(client, content["id"], account_id=account_a.id)
    second = await _create_job(client, content["id"], account_id=account_b.id)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


async def test_foreign_tenant_idempotency_key_is_ignored(client, db_session):
    content = await _create_content(client)
    key = f"{uuid.UUID(content['id'])}:1:wechat:-"
    foreign_content = await _seed_foreign_content(db_session)
    foreign_job = PublishJob(
        tenant_id=OTHER_TENANT,
        content_id=foreign_content.id,
        content_version=1,
        platform="wechat",
        status="draft",
        idempotency_key=key,
    )
    db_session.add(foreign_job)
    await db_session.commit()
    await db_session.refresh(foreign_job)

    response = await _create_job(client, content["id"])
    assert response.status_code == 201
    assert response.json()["id"] != str(foreign_job.id)

    stored = (
        await db_session.execute(
            select(PublishJob).where(PublishJob.id == uuid.UUID(response.json()["id"]))
        )
    ).scalar_one()
    assert stored.tenant_id == DEFAULT_TENANT_ID


async def test_foreign_content_returns_404(client, db_session):
    foreign_content = await _seed_foreign_content(db_session)

    response = await _create_job(client, foreign_content.id)
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_unknown_and_malformed_job_ids_return_404(client):
    unknown = await client.get(f"/api/publish/jobs/{uuid.uuid4()}")
    malformed = await client.get("/api/publish/jobs/not-a-uuid")

    assert unknown.status_code == 404
    assert malformed.status_code == 404
    assert unknown.json()["code"] == "not_found"
    assert malformed.json()["code"] == "not_found"


async def test_foreign_job_returns_404(client, db_session):
    foreign_content = await _seed_foreign_content(db_session)
    foreign_job = PublishJob(
        tenant_id=OTHER_TENANT,
        content_id=foreign_content.id,
        content_version=1,
        platform="wechat",
        status="draft",
        idempotency_key=f"foreign-{uuid.uuid4().hex}",
    )
    db_session.add(foreign_job)
    await db_session.commit()
    await db_session.refresh(foreign_job)

    response = await client.get(f"/api/publish/jobs/{foreign_job.id}")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_account_validation(client, db_session):
    content = await _create_content(client)
    account = await _seed_account(db_session)
    ok = await _create_job(client, content["id"], account_id=account.id)
    assert ok.status_code == 201
    assert ok.json()["account_id"] == str(account.id)

    random_account = await _create_job(
        client, content["id"], account_id=uuid.uuid4()
    )
    assert random_account.status_code == 404
    assert random_account.json()["code"] == "not_found"

    foreign_account = await _seed_account(db_session, tenant_id=OTHER_TENANT)
    foreign = await _create_job(client, content["id"], account_id=foreign_account.id)
    assert foreign.status_code == 404
    assert foreign.json()["code"] == "not_found"


async def test_account_platform_mismatch(client, db_session):
    content = await _create_content(client)
    account = await _seed_account(db_session, platform="douyin")

    response = await _create_job(
        client, content["id"], platform="wechat", account_id=account.id
    )
    assert response.status_code == 409
    assert response.json()["code"] == "account_platform_mismatch"


async def test_happy_path_actions_record_events(client, db_session):
    content = await _create_content(client)
    job_body = (await _create_job(client, content["id"])).json()
    job_id = job_body["id"]

    submitted = await client.post(
        f"/api/publish/jobs/{job_id}/actions", json={"action": "submit"}
    )
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "prechecking"

    job = (
        await db_session.execute(
            select(PublishJob).where(PublishJob.id == uuid.UUID(job_id))
        )
    ).scalar_one()
    await apply_action(db_session, job, "pass", actor="system")

    approved = await client.post(
        f"/api/publish/jobs/{job_id}/actions",
        json={"action": "approve", "note": "人工确认"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    events = (
        await db_session.execute(
            select(PublishEvent).where(PublishEvent.job_id == uuid.UUID(job_id))
        )
    ).scalars().all()
    by_action = {event.action: event for event in events}
    assert set(by_action) == {"submit", "pass", "approve"}
    assert (by_action["submit"].from_status, by_action["submit"].to_status) == (
        "draft",
        "prechecking",
    )
    assert (by_action["pass"].from_status, by_action["pass"].to_status) == (
        "prechecking",
        "awaiting_review",
    )
    assert (by_action["approve"].from_status, by_action["approve"].to_status) == (
        "awaiting_review",
        "approved",
    )
    assert all(event.tenant_id == DEFAULT_TENANT_ID for event in events)
    assert all(event.job_id == uuid.UUID(job_id) for event in events)
    assert by_action["pass"].actor == "system"
    assert by_action["approve"].actor == "tester@local"
    assert by_action["approve"].note == "人工确认"


async def test_list_jobs_filters_tenant_and_status(client, db_session):
    content = await _create_content(client)
    draft = (await _create_job(client, content["id"], platform="wechat")).json()
    other = (await _create_job(client, content["id"], platform="douyin")).json()
    submitted = await client.post(
        f"/api/publish/jobs/{other['id']}/actions", json={"action": "submit"}
    )
    assert submitted.status_code == 200

    foreign_content = await _seed_foreign_content(db_session)
    db_session.add(
        PublishJob(
            tenant_id=OTHER_TENANT,
            content_id=foreign_content.id,
            content_version=1,
            platform="wechat",
            status="draft",
            idempotency_key=f"foreign-list-{uuid.uuid4().hex}",
        )
    )
    await db_session.commit()

    all_jobs = await client.get("/api/publish/jobs")
    assert all_jobs.status_code == 200
    assert {item["id"] for item in all_jobs.json()["items"]} == {
        draft["id"],
        other["id"],
    }

    drafts = await client.get("/api/publish/jobs", params={"status": "draft"})
    assert drafts.status_code == 200
    assert [item["id"] for item in drafts.json()["items"]] == [draft["id"]]

    prechecking = await client.get(
        "/api/publish/jobs", params={"status": "prechecking"}
    )
    assert prechecking.status_code == 200
    assert [item["id"] for item in prechecking.json()["items"]] == [other["id"]]


async def test_request_validation(client):
    content = await _create_content(client)

    unknown_platform = await _create_job(client, content["id"], platform="twitter")
    assert unknown_platform.status_code == 422

    extra_field = await client.post(
        "/api/publish/jobs",
        json={"content_id": content["id"], "platform": "wechat", "oops": 1},
    )
    assert extra_field.status_code == 422

    malformed_content = await client.post(
        "/api/publish/jobs", json={"content_id": "not-a-uuid", "platform": "wechat"}
    )
    assert malformed_content.status_code == 422
