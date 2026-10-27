import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from app.config import DEFAULT_TENANT_ID
from app.models.project import Content, ContentVersion, Project
from app.models.publish import PlatformAccount, PublishEvent, PublishJob
from app.publish.state import TRANSITIONS, InvalidTransition, next_status
from app.services.publish import (
    ContentChanged,
    _locked_job,
    apply_action,
    apply_precheck,
)

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


async def _act(client, job_id, action, *, note=None):
    payload = {"action": action}
    if note is not None:
        payload["note"] = note
    return await client.post(f"/api/publish/jobs/{job_id}/actions", json=payload)


async def _stored_job(db_session, job_id):
    return (
        await db_session.execute(
            select(PublishJob).where(PublishJob.id == uuid.UUID(str(job_id)))
        )
    ).scalar_one()


async def _system_act(db_session, job_id, action):
    return await apply_action(
        db_session,
        uuid.UUID(str(job_id)),
        action,
        tenant_id=DEFAULT_TENANT_ID,
        actor="system",
    )


async def _advance_to_awaiting_review(client, db_session, job_id):
    submitted = await _system_act(db_session, job_id, "submit")
    assert submitted.status == "prechecking"
    await _system_act(db_session, job_id, "pass")


async def _advance_to_approved(client, db_session, job_id):
    await _advance_to_awaiting_review(client, db_session, job_id)
    assert (await _act(client, job_id, "approve")).status_code == 200


async def _seed_account(
    db_session,
    *,
    tenant_id=DEFAULT_TENANT_ID,
    platform="wechat",
    name=None,
    status="active",
):
    account = PlatformAccount(
        tenant_id=tenant_id,
        platform=platform,
        name=name or f"账号-{uuid.uuid4().hex[:8]}",
        status=status,
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


async def _seed_foreign_job(db_session, content):
    job = PublishJob(
        tenant_id=OTHER_TENANT,
        content_id=content.id,
        content_version=1,
        platform="wechat",
        status="draft",
        idempotency_key=f"foreign-{uuid.uuid4().hex}",
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job


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
    for current in ("draft", "awaiting_review", "approved"):
        assert next_status(current, "export") == "exporting"
    assert next_status("exporting", "export_done") == "exported"


def test_failure_and_retry_paths():
    assert next_status("prechecking", "fail") == "precheck_failed"
    assert next_status("precheck_failed", "submit") == "prechecking"
    assert next_status("awaiting_review", "reject") == "draft"
    assert next_status("exporting", "fail") == "export_failed"
    assert next_status("export_failed", "retry") == "exporting"
    assert next_status("publishing", "fail") == "publish_failed"
    assert next_status("approved", "fail") == "publish_failed"
    assert next_status("publish_failed", "retry") == "approved"


def test_legacy_failed_state_is_gone():
    assert "failed" not in TRANSITIONS
    with pytest.raises(InvalidTransition):
        next_status("failed", "retry")


def test_export_failure_cannot_reach_approved():
    status = next_status("draft", "export")
    status = next_status(status, "fail")
    assert status == "export_failed"
    status = next_status(status, "retry")
    assert status == "exporting"
    with pytest.raises(InvalidTransition):
        next_status(status, "approve")
    with pytest.raises(InvalidTransition):
        next_status(status, "publish")


@pytest.mark.parametrize(
    "current",
    [
        "draft",
        "precheck_failed",
        "awaiting_review",
        "approved",
        "export_failed",
        "publish_failed",
    ],
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
        ("export_failed", "approve"),
        ("publish_failed", "submit"),
        ("unknown", "submit"),
    ],
)
def test_illegal_transitions(current, action):
    with pytest.raises(InvalidTransition):
        next_status(current, action)


@pytest.mark.parametrize("current", ["exported", "published"])
@pytest.mark.parametrize(
    "action",
    [
        "submit",
        "approve",
        "reject",
        "publish",
        "export",
        "retry",
        "cancel",
        "reopen",
        "pass",
        "fail",
        "succeed",
        "export_done",
    ],
)
def test_terminal_states_reject_all_actions(current, action):
    with pytest.raises(InvalidTransition):
        next_status(current, action)


@pytest.mark.parametrize(
    "action",
    [
        "submit",
        "approve",
        "reject",
        "publish",
        "export",
        "retry",
        "cancel",
        "pass",
        "fail",
        "succeed",
        "export_done",
    ],
)
def test_cancelled_rejects_all_actions_except_reopen(action):
    with pytest.raises(InvalidTransition):
        next_status("cancelled", action)


def test_cancelled_can_be_reopened():
    assert next_status("cancelled", "reopen") == "draft"


async def test_create_job_from_current_version(client, db_session):
    content = await _create_content(client)
    response = await _create_job(client, content["id"])

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "draft"
    assert body["content_id"] == content["id"]
    assert body["content_version"] == content["current_version"] == 1
    assert body["account_id"] is None
    assert body["checks"] == {}
    assert body["updated_at"]
    assert "tenant_id" not in body

    events = (
        await db_session.execute(
            select(PublishEvent).where(
                PublishEvent.job_id == uuid.UUID(body["id"])
            )
        )
    ).scalars().all()
    assert [
        (event.from_status, event.to_status, event.action, event.actor)
        for event in events
    ] == [("created", "draft", "create", "tester@local")]
    assert events[0].tenant_id == DEFAULT_TENANT_ID


async def test_scheduled_at_round_trips(client):
    content = await _create_content(client)
    response = await _create_job(
        client, content["id"], scheduled_at="2026-11-01T09:30:00+00:00"
    )

    assert response.status_code == 201
    scheduled = datetime.fromisoformat(response.json()["scheduled_at"])
    assert scheduled.tzinfo is not None
    assert scheduled == datetime(2026, 11, 1, 9, 30, tzinfo=UTC)


async def test_scheduled_at_offset_is_normalized(client):
    content = await _create_content(client)
    response = await _create_job(
        client, content["id"], scheduled_at="2026-11-01T09:30:00+08:00"
    )

    assert response.status_code == 201
    scheduled = datetime.fromisoformat(response.json()["scheduled_at"])
    assert scheduled.tzinfo is not None
    assert scheduled == datetime(2026, 11, 1, 1, 30, tzinfo=UTC)


async def test_replay_with_equivalent_offset_returns_unchanged(client):
    content = await _create_content(client)
    first = await _create_job(
        client, content["id"], scheduled_at="2026-11-01T09:30:00+08:00"
    )
    second = await _create_job(
        client, content["id"], scheduled_at="2026-11-01T01:30:00+00:00"
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["scheduled_at"] == first.json()["scheduled_at"]
    assert second.json()["updated_at"] == first.json()["updated_at"]


async def test_naive_create_schedule_is_rejected(client):
    content = await _create_content(client)
    response = await _create_job(
        client, content["id"], scheduled_at="2026-11-01T09:30:00"
    )

    assert response.status_code == 422


async def test_approve_on_draft_returns_409(client):
    content = await _create_content(client)
    job = (await _create_job(client, content["id"])).json()

    response = await _act(client, job["id"], "approve")
    assert response.status_code == 409
    assert response.json()["code"] == "invalid_transition"


async def test_double_submit_is_rejected(client, db_session):
    content = await _create_content(client)
    job = (await _create_job(client, content["id"])).json()

    submitted = await _system_act(db_session, job["id"], "submit")
    assert submitted.status == "prechecking"
    with pytest.raises(InvalidTransition):
        await _system_act(db_session, job["id"], "submit")


@pytest.mark.parametrize(
    "action",
    [
        "submit",
        "publish",
        "pass",
        "fail",
        "succeed",
        "export",
        "export_done",
        "reopen",
    ],
)
async def test_system_actions_are_forbidden(client, action):
    content = await _create_content(client)
    job = (await _create_job(client, content["id"])).json()

    response = await _act(client, job["id"], action)
    assert response.status_code == 403
    assert response.json()["code"] == "action_forbidden"
    assert response.json()["message"] == "该动作不能通过此接口执行"


async def test_unknown_action_returns_422(client):
    content = await _create_content(client)
    job = (await _create_job(client, content["id"])).json()

    response = await _act(client, job["id"], "frobnicate")
    assert response.status_code == 422
    assert response.json()["code"] == "unknown_action"


async def test_actions_on_missing_job_return_404(client, db_session):
    malformed = await _act(client, "not-a-uuid", "approve")
    assert malformed.status_code == 404
    assert malformed.json()["code"] == "not_found"

    unknown = await _act(client, str(uuid.uuid4()), "approve")
    assert unknown.status_code == 404

    foreign_content = await _seed_foreign_content(db_session)
    foreign_job = await _seed_foreign_job(db_session, foreign_content)
    foreign = await _act(client, str(foreign_job.id), "approve")
    assert foreign.status_code == 404
    assert foreign.json()["code"] == "not_found"


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


async def test_idempotent_replay_returns_same_job(client, db_session):
    content = await _create_content(client)
    first = await _create_job(client, content["id"])
    second = await _create_job(client, content["id"])

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]

    creates = (
        await db_session.execute(
            select(PublishEvent).where(
                PublishEvent.job_id == uuid.UUID(first.json()["id"]),
                PublishEvent.action == "create",
            )
        )
    ).scalars().all()
    assert len(creates) == 1


async def test_idempotency_varies_by_platform_and_account(client, db_session):
    content = await _create_content(client, platform="wechat")
    douyin_content = await _create_content(client, platform="douyin")
    base = await _create_job(client, content["id"], platform="wechat")
    other_platform = await _create_job(client, douyin_content["id"], platform="douyin")
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


async def test_replay_updates_schedule_while_reschedulable(client):
    content = await _create_content(client)
    first = await _create_job(
        client, content["id"], scheduled_at="2026-11-01T09:30:00+00:00"
    )
    second = await _create_job(
        client, content["id"], scheduled_at="2026-11-02T10:00:00+00:00"
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    scheduled = datetime.fromisoformat(second.json()["scheduled_at"])
    assert scheduled.replace(tzinfo=None) == datetime(2026, 11, 2, 10, 0)


async def test_replay_schedule_change_after_exporting_returns_409(
    client, db_session
):
    content = await _create_content(client)
    job = (
        await _create_job(
            client, content["id"], scheduled_at="2026-11-01T09:30:00+00:00"
        )
    ).json()
    stored = await _stored_job(db_session, job["id"])
    stored.status = "exporting"
    await db_session.commit()

    response = await _create_job(
        client, content["id"], scheduled_at="2026-11-02T10:00:00+00:00"
    )
    assert response.status_code == 409
    assert response.json()["code"] == "job_not_reschedulable"


async def test_replay_reopens_cancelled_job(client, db_session):
    content = await _create_content(client)
    job = (await _create_job(client, content["id"])).json()

    stored = await _stored_job(db_session, job["id"])
    stored.attempts = 3
    stored.error = "导出失败"
    stored.export_path = "/tmp/old.docx"
    stored.external_id = "old-id"
    stored.external_url = "https://example.com/old"
    stored.published_at = datetime(2026, 1, 1, tzinfo=UTC)
    stored.checks = {"ok": False}
    await db_session.commit()

    cancelled = await _act(client, job["id"], "cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    replay = await _create_job(
        client, content["id"], scheduled_at="2026-12-01T08:00:00+00:00"
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == job["id"]
    assert replay.json()["status"] == "draft"

    reopened = await _stored_job(db_session, job["id"])
    assert reopened.status == "draft"
    assert reopened.attempts == 0
    assert reopened.error == ""
    assert reopened.export_path == ""
    assert reopened.external_id == ""
    assert reopened.external_url == ""
    assert reopened.published_at is None
    assert reopened.checks == {}
    assert reopened.scheduled_at.replace(tzinfo=None) == datetime(2026, 12, 1, 8, 0)

    event = (
        await db_session.execute(
            select(PublishEvent).where(
                PublishEvent.job_id == uuid.UUID(job["id"]),
                PublishEvent.action == "reopen",
            )
        )
    ).scalar_one()
    assert (event.from_status, event.to_status) == ("cancelled", "draft")
    assert event.actor == "tester@local"


async def test_replay_before_validation_after_account_deactivation(client, db_session):
    content = await _create_content(client)
    account = await _seed_account(db_session)
    first = await _create_job(client, content["id"], account_id=account.id)
    assert first.status_code == 201

    account.status = "revoked"
    await db_session.commit()

    replay = await _create_job(client, content["id"], account_id=account.id)
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]


async def test_patch_schedule_updates_and_clears(client):
    content = await _create_content(client)
    job = (
        await _create_job(
            client, content["id"], scheduled_at="2026-11-01T09:30:00+00:00"
        )
    ).json()

    updated = await client.patch(
        f"/api/publish/jobs/{job['id']}/schedule",
        json={"scheduled_at": "2026-11-05T08:00:00+00:00"},
    )
    assert updated.status_code == 200
    scheduled = datetime.fromisoformat(updated.json()["scheduled_at"])
    assert scheduled.replace(tzinfo=None) == datetime(2026, 11, 5, 8, 0)

    cleared = await client.patch(
        f"/api/publish/jobs/{job['id']}/schedule", json={"scheduled_at": None}
    )
    assert cleared.status_code == 200
    assert cleared.json()["scheduled_at"] is None


async def test_patch_schedule_rejected_once_exporting(client, db_session):
    content = await _create_content(client)
    job = (await _create_job(client, content["id"])).json()
    stored = await _stored_job(db_session, job["id"])
    stored.status = "exporting"
    await db_session.commit()

    response = await client.patch(
        f"/api/publish/jobs/{job['id']}/schedule",
        json={"scheduled_at": "2026-11-05T08:00:00+00:00"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "job_not_reschedulable"


@pytest.mark.parametrize(
    "payload",
    [
        {"scheduled_at": "2026-11-05T08:00:00"},
        {},
    ],
)
async def test_patch_schedule_validation(client, payload):
    content = await _create_content(client)
    job = (await _create_job(client, content["id"])).json()

    response = await client.patch(
        f"/api/publish/jobs/{job['id']}/schedule", json=payload
    )
    assert response.status_code == 422


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

    stored = await _stored_job(db_session, response.json()["id"])
    assert stored.tenant_id == DEFAULT_TENANT_ID


async def test_foreign_content_returns_404(client, db_session):
    foreign_content = await _seed_foreign_content(db_session)

    response = await _create_job(client, foreign_content.id)
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_content_platform_mismatch(client):
    content = await _create_content(client, platform="wechat")

    response = await _create_job(client, content["id"], platform="douyin")
    assert response.status_code == 409
    assert response.json()["code"] == "platform_mismatch"


async def test_unknown_and_malformed_job_ids_return_404(client):
    unknown = await client.get(f"/api/publish/jobs/{uuid.uuid4()}")
    malformed = await client.get("/api/publish/jobs/not-a-uuid")

    assert unknown.status_code == 404
    assert malformed.status_code == 404
    assert unknown.json()["code"] == "not_found"
    assert malformed.json()["code"] == "not_found"


async def test_foreign_job_returns_404(client, db_session):
    foreign_content = await _seed_foreign_content(db_session)
    foreign_job = await _seed_foreign_job(db_session, foreign_content)

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


@pytest.mark.parametrize("status", ["inactive", "revoked"])
async def test_inactive_account_is_rejected(client, db_session, status):
    content = await _create_content(client)
    account = await _seed_account(db_session, status=status)

    response = await _create_job(client, content["id"], account_id=account.id)
    assert response.status_code == 409
    assert response.json()["code"] == "account_inactive"


async def test_publish_guard_rejects_stale_content_version(client, db_session):
    content = await _create_content(client)
    job_id = (await _create_job(client, content["id"])).json()["id"]
    await _advance_to_approved(client, db_session, job_id)

    stored_content = (
        await db_session.execute(
            select(Content).where(Content.id == uuid.UUID(content["id"]))
        )
    ).scalar_one()
    stored_content.current_version = 2
    db_session.add(
        ContentVersion(
            tenant_id=DEFAULT_TENANT_ID,
            content_id=stored_content.id,
            version=2,
            title="更新标题",
            body="更新正文",
        )
    )
    await db_session.commit()

    with pytest.raises(ContentChanged):
        await _system_act(db_session, job_id, "publish")

    job = await _stored_job(db_session, job_id)
    assert job.status == "approved"


async def test_locked_job_refreshes_stale_identity_map(client, db_session):
    content = await _create_content(client)
    job_id = uuid.UUID((await _create_job(client, content["id"])).json()["id"])
    cached = (
        await db_session.execute(select(PublishJob).where(PublishJob.id == job_id))
    ).scalar_one()
    assert cached.status == "draft"

    await db_session.execute(
        update(PublishJob)
        .where(PublishJob.id == job_id)
        .values(status="exporting")
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()
    assert cached.status == "draft"

    fresh = await _locked_job(db_session, job_id, DEFAULT_TENANT_ID)
    assert fresh.status == "exporting"


async def test_apply_precheck_refreshes_stale_identity_map(client, db_session):
    content = await _create_content(client)
    job_id = uuid.UUID((await _create_job(client, content["id"])).json()["id"])
    cached = (
        await db_session.execute(select(PublishJob).where(PublishJob.id == job_id))
    ).scalar_one()
    assert cached.status == "draft"

    await db_session.execute(
        update(PublishJob)
        .where(PublishJob.id == job_id)
        .values(status="awaiting_review")
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()
    assert cached.status == "draft"

    with pytest.raises(InvalidTransition):
        await apply_precheck(
            db_session,
            job_id,
            {"passed": True, "errors": 0, "warnings": 0, "issues": []},
            tenant_id=DEFAULT_TENANT_ID,
            actor="system",
        )

    db_session.expire_all()
    job = await _stored_job(db_session, job_id)
    assert job.status == "awaiting_review"
    events = (
        await db_session.execute(
            select(PublishEvent).where(PublishEvent.job_id == job_id)
        )
    ).scalars().all()
    assert [event.action for event in events] == ["create"]


async def test_publish_guard_reads_fresh_content_version(client, db_session):
    content = await _create_content(client)
    job_id = (await _create_job(client, content["id"])).json()["id"]
    stored_job = await _stored_job(db_session, job_id)
    stored_job.status = "approved"
    await db_session.commit()

    content_id = uuid.UUID(content["id"])
    cached = (
        await db_session.execute(select(Content).where(Content.id == content_id))
    ).scalar_one()
    assert cached.current_version == 1

    await db_session.execute(
        update(Content)
        .where(Content.id == content_id)
        .values(current_version=2)
        .execution_options(synchronize_session=False)
    )
    db_session.add(
        ContentVersion(
            tenant_id=DEFAULT_TENANT_ID,
            content_id=content_id,
            version=2,
            title="更新标题",
            body="更新正文",
        )
    )
    await db_session.commit()
    assert cached.current_version == 1

    with pytest.raises(ContentChanged):
        await apply_action(
            db_session,
            uuid.UUID(job_id),
            "publish",
            tenant_id=DEFAULT_TENANT_ID,
            actor="system",
        )


async def test_happy_path_actions_record_events(client, db_session):
    content = await _create_content(client)
    job_body = (await _create_job(client, content["id"])).json()
    job_id = job_body["id"]

    submitted = await _system_act(db_session, job_id, "submit")
    assert submitted.status == "prechecking"

    await _system_act(db_session, job_id, "pass")

    approved = await _act(client, job_id, "approve", note="人工确认")
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    db_session.expire_all()
    events = (
        await db_session.execute(
            select(PublishEvent).where(PublishEvent.job_id == uuid.UUID(job_id))
        )
    ).scalars().all()
    by_action = {event.action: event for event in events}
    assert [event.action for event in events] == [
        "create",
        "submit",
        "pass",
        "approve",
    ]
    assert set(by_action) == {"create", "submit", "pass", "approve"}
    assert (by_action["create"].from_status, by_action["create"].to_status) == (
        "created",
        "draft",
    )
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
    assert by_action["create"].actor == "tester@local"
    assert by_action["submit"].actor == "system"
    assert by_action["pass"].actor == "system"
    assert by_action["approve"].actor == "tester@local"
    assert by_action["approve"].note == "人工确认"


async def test_reject_and_cancel_record_events(client, db_session):
    content = await _create_content(client)
    job_id = (await _create_job(client, content["id"])).json()["id"]

    async def _act_and_status(action):
        response = await _act(client, job_id, action)
        assert response.status_code == 200
        return response.json()["status"]

    async def _submit():
        job = await _system_act(db_session, job_id, "submit")
        assert job.status == "prechecking"

    await _submit()
    await _system_act(db_session, job_id, "pass")
    assert await _act_and_status("reject") == "draft"

    await _submit()
    await _system_act(db_session, job_id, "pass")
    assert await _act_and_status("approve") == "approved"
    assert await _act_and_status("cancel") == "cancelled"

    db_session.expire_all()
    events = (
        await db_session.execute(
            select(PublishEvent).where(PublishEvent.job_id == uuid.UUID(job_id))
        )
    ).scalars().all()
    rows = sorted(
        (event.action, event.from_status, event.to_status, event.actor)
        for event in events
    )
    assert rows == sorted(
        [
            ("create", "created", "draft", "tester@local"),
            ("submit", "draft", "prechecking", "system"),
            ("pass", "prechecking", "awaiting_review", "system"),
            ("reject", "awaiting_review", "draft", "tester@local"),
            ("submit", "draft", "prechecking", "system"),
            ("pass", "prechecking", "awaiting_review", "system"),
            ("approve", "awaiting_review", "approved", "tester@local"),
            ("cancel", "approved", "cancelled", "tester@local"),
        ]
    )
    assert all(event.tenant_id == DEFAULT_TENANT_ID for event in events)


async def test_retry_from_publish_failed_records_event(client, db_session):
    content = await _create_content(client)
    job_id = (await _create_job(client, content["id"])).json()["id"]

    job = await _stored_job(db_session, job_id)
    job.status = "publish_failed"
    await db_session.commit()

    response = await _act(client, job_id, "retry")
    assert response.status_code == 200
    assert response.json()["status"] == "approved"

    event = (
        await db_session.execute(
            select(PublishEvent).where(
                PublishEvent.job_id == uuid.UUID(job_id),
                PublishEvent.action == "retry",
            )
        )
    ).scalar_one()
    assert (event.from_status, event.to_status) == ("publish_failed", "approved")
    assert event.actor == "tester@local"
    assert event.tenant_id == DEFAULT_TENANT_ID


async def test_list_jobs_filters_tenant_and_status(client, db_session):
    content = await _create_content(client, platform="wechat")
    douyin_content = await _create_content(client, platform="douyin")
    draft = (await _create_job(client, content["id"], platform="wechat")).json()
    other = (
        await _create_job(client, douyin_content["id"], platform="douyin")
    ).json()
    submitted = await _system_act(db_session, other["id"], "submit")
    assert submitted.status == "prechecking"

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
    assert all_jobs.json()["total"] == 2
    assert {item["id"] for item in all_jobs.json()["items"]} == {
        draft["id"],
        other["id"],
    }

    drafts = await client.get("/api/publish/jobs", params={"status": "draft"})
    assert drafts.status_code == 200
    assert drafts.json()["total"] == 1
    assert [item["id"] for item in drafts.json()["items"]] == [draft["id"]]

    prechecking = await client.get(
        "/api/publish/jobs", params={"status": "prechecking"}
    )
    assert prechecking.status_code == 200
    assert prechecking.json()["total"] == 1
    assert [item["id"] for item in prechecking.json()["items"]] == [other["id"]]


async def test_list_jobs_pagination(client, db_session):
    jobs = []
    for platform in ("wechat", "douyin", "zhihu"):
        content = await _create_content(client, platform=platform)
        jobs.append(
            (await _create_job(client, content["id"], platform=platform)).json()
        )

    for job, created_at in zip(
        jobs,
        [
            datetime(2026, 1, 1),
            datetime(2026, 1, 2),
            datetime(2026, 1, 3),
        ],
        strict=True,
    ):
        stored = await _stored_job(db_session, job["id"])
        stored.created_at = created_at
    await db_session.commit()

    first_page = await client.get(
        "/api/publish/jobs", params={"limit": 2, "offset": 0}
    )
    assert first_page.status_code == 200
    assert first_page.json()["total"] == 3
    assert [item["id"] for item in first_page.json()["items"]] == [
        jobs[2]["id"],
        jobs[1]["id"],
    ]

    second_page = await client.get(
        "/api/publish/jobs", params={"limit": 2, "offset": 2}
    )
    assert second_page.status_code == 200
    assert second_page.json()["total"] == 3
    assert [item["id"] for item in second_page.json()["items"]] == [jobs[0]["id"]]

    assert (
        await client.get("/api/publish/jobs", params={"limit": 0})
    ).status_code == 422


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
