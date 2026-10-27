import json
import uuid

import httpx
import pytest
import respx
from sqlalchemy import select

from app.models.base import DEFAULT_TENANT_ID
from app.models.project import Content, ContentVersion, Project
from app.models.publish import PlatformAccount, PublishEvent, PublishJob
from app.publish.wechat import DRAFT_URL, TOKEN_URL, WeChatMPAdapter
from app.security import decrypt_secret, encrypt_secret
from app.services.publish import apply_action

OTHER_TENANT = "00000000-0000-0000-0000-000000000002"

CREDENTIAL = {"app_id": "wx123", "app_secret": "secret"}


def _account_payload(
    *,
    name="主号",
    platform="wechat",
    credential=None,
    meta=None,
):
    return {
        "platform": platform,
        "name": name,
        "credential": dict(credential or CREDENTIAL),
        "meta": meta if meta is not None else {"author": "作者甲"},
    }


async def _create_account(client, **overrides):
    response = await client.post("/api/accounts", json=_account_payload(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


async def _create_content(
    client,
    *,
    title="测试标题",
    body="这是**正文**。",
    platform="wechat",
):
    project = await client.post(
        "/api/projects", json={"title": f"项目-{uuid.uuid4().hex[:8]}"}
    )
    assert project.status_code == 201
    content = await client.post(
        f"/api/projects/{project.json()['id']}/contents",
        json={"platform": platform, "title": title, "body": body},
    )
    assert content.status_code == 201, content.text
    return content.json()


async def _create_job(client, content_id, *, account_id=None, platform="wechat"):
    payload = {"content_id": str(content_id), "platform": platform}
    if account_id is not None:
        payload["account_id"] = str(account_id)
    response = await client.post("/api/publish/jobs", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def _approve(client, job_id):
    precheck = await client.post(f"/api/publish/jobs/{job_id}/precheck")
    assert precheck.status_code == 200, precheck.text
    assert precheck.json()["passed"] is True
    approve = await client.post(
        f"/api/publish/jobs/{job_id}/actions", json={"action": "approve"}
    )
    assert approve.status_code == 200, approve.text
    return approve.json()


async def _execute(client, job_id):
    return await client.post(f"/api/publish/jobs/{job_id}/execute")


async def _act(client, job_id, action):
    return await client.post(
        f"/api/publish/jobs/{job_id}/actions", json={"action": action}
    )


async def _stored_job(db_session, job_id):
    db_session.expire_all()
    return (
        await db_session.execute(
            select(PublishJob).where(PublishJob.id == uuid.UUID(str(job_id)))
        )
    ).scalar_one()


async def _stored_account(db_session, account_id):
    db_session.expire_all()
    return (
        await db_session.execute(
            select(PlatformAccount).where(
                PlatformAccount.id == uuid.UUID(str(account_id))
            )
        )
    ).scalar_one()


async def _events(db_session, job_id):
    db_session.expire_all()
    return (
        await db_session.execute(
            select(PublishEvent).where(
                PublishEvent.job_id == uuid.UUID(str(job_id))
            )
        )
    ).scalars().all()


def _mock_success(token="tok-abc", media_id="media-123"):
    token_route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": token})
    )
    draft_route = respx.post(DRAFT_URL).mock(
        return_value=httpx.Response(200, json={"media_id": media_id})
    )
    return token_route, draft_route


async def _seed_foreign_job(db_session):
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
    job = PublishJob(
        tenant_id=OTHER_TENANT,
        content_id=content.id,
        content_version=1,
        platform="wechat",
        status="approved",
        idempotency_key=f"foreign-{uuid.uuid4().hex}",
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job


# --- accounts ---------------------------------------------------------------


async def test_create_account_masks_and_encrypts_credentials(client, db_session):
    body = await _create_account(client)

    assert body["platform"] == "wechat"
    assert body["name"] == "主号"
    assert body["status"] == "active"
    assert set(body) == {"id", "platform", "name", "status", "credential_masked"}
    assert json.loads(body["credential_masked"]) == {
        "app_id": "wx1****",
        "app_secret": "sec****",
    }
    assert "wx123" not in json.dumps(body)
    assert '"secret"' not in json.dumps(body)

    stored = await _stored_account(db_session, body["id"])
    assert stored.tenant_id == DEFAULT_TENANT_ID
    assert "wx123" not in stored.credential_enc
    assert "secret" not in stored.credential_enc
    assert json.loads(decrypt_secret(stored.credential_enc)) == CREDENTIAL


async def test_list_accounts_is_tenant_scoped_and_masked(client, db_session):
    await _create_account(client, name="本租户")
    foreign = PlatformAccount(
        tenant_id=OTHER_TENANT,
        platform="wechat",
        name="外部账号",
        credential_enc=encrypt_secret(json.dumps(CREDENTIAL)),
        status="active",
        meta={},
    )
    db_session.add(foreign)
    await db_session.commit()

    response = await client.get("/api/accounts")
    assert response.status_code == 200
    items = response.json()
    assert [item["name"] for item in items] == ["本租户"]
    assert json.loads(items[0]["credential_masked"])["app_id"] == "wx1****"
    assert "wx123" not in response.text
    assert '"secret"' not in response.text


async def test_duplicate_account_name_per_platform_conflicts(client):
    await _create_account(client, name="同名")

    response = await client.post("/api/accounts", json=_account_payload(name="同名"))
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "account_exists"
    assert body["message"] == "该平台下已存在同名账号"
    assert "wx123" not in response.text
    assert "secret" not in response.text


@pytest.mark.parametrize(
    "credential",
    [
        {"app_id": "wx123"},
        {"app_secret": "secret"},
        {"app_id": "  ", "app_secret": "secret"},
    ],
)
async def test_missing_wechat_credentials_are_rejected(client, credential):
    response = await client.post(
        "/api/accounts", json=_account_payload(credential=credential)
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert "secret" not in response.text


async def test_unknown_platform_is_rejected(client):
    response = await client.post(
        "/api/accounts", json=_account_payload(platform="weibo")
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_unknown_meta_key_is_rejected(client):
    response = await client.post(
        "/api/accounts", json=_account_payload(meta={"shell": "rm -rf"})
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert "rm -rf" not in response.text


async def test_delete_account_soft_revokes(client, db_session):
    account = await _create_account(client)

    deleted = await client.delete(f"/api/accounts/{account['id']}")
    assert deleted.status_code == 204
    assert deleted.text == ""

    stored = await _stored_account(db_session, account["id"])
    assert stored.status == "revoked"

    listed = await client.get("/api/accounts")
    assert [item["status"] for item in listed.json()] == ["revoked"]

    again = await client.delete(f"/api/accounts/{account['id']}")
    assert again.status_code == 204


async def test_delete_foreign_or_malformed_account_returns_404(client, db_session):
    foreign = PlatformAccount(
        tenant_id=OTHER_TENANT,
        platform="wechat",
        name="外部账号",
        credential_enc=encrypt_secret(json.dumps(CREDENTIAL)),
        status="active",
        meta={},
    )
    db_session.add(foreign)
    await db_session.commit()

    foreign_response = await client.delete(f"/api/accounts/{foreign.id}")
    assert foreign_response.status_code == 404
    assert foreign_response.json()["code"] == "not_found"

    malformed = await client.delete("/api/accounts/not-a-uuid")
    assert malformed.status_code == 404
    assert malformed.json()["message"] == "账号不存在"


async def test_revoked_account_cannot_create_job(client):
    account = await _create_account(client)
    content = await _create_content(client)
    assert (
        await client.delete(f"/api/accounts/{account['id']}")
    ).status_code == 204

    response = await client.post(
        "/api/publish/jobs",
        json={"content_id": content["id"], "platform": "wechat", "account_id": account["id"]},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "account_inactive"


# --- happy path -------------------------------------------------------------


@respx.mock
async def test_happy_path_creates_wechat_draft(client, db_session):
    token_route, draft_route = _mock_success()
    account = await _create_account(
        client,
        meta={
            "author": "作者甲",
            "need_open_comment": 1,
            "only_fans_can_comment": 0,
        },
    )
    content = await _create_content(client)
    job = await _create_job(client, content["id"], account_id=account["id"])
    await _approve(client, job["id"])

    response = await _execute(client, job["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "published"
    assert body["external_id"] == "media-123"
    assert body["attempts"] == 1
    assert body["external_url"] == ""
    assert body["error"] == ""
    assert body["published_at"] is not None
    assert body["published_at"].endswith(("Z", "+00:00"))
    assert "tenant_id" not in body

    assert token_route.call_count == 1
    assert draft_route.call_count == 1
    draft_request = draft_route.calls[0].request
    assert draft_request.url.params["access_token"] == "tok-abc"
    payload = json.loads(draft_request.content)
    article = payload["articles"][0]
    assert article["title"] == "测试标题"
    assert article["author"] == "作者甲"
    assert article["digest"] == "这是正文。"
    assert "<strong>正文</strong>" in article["content"]
    assert article["need_open_comment"] == 1
    assert article["only_fans_can_comment"] == 0
    assert "thumb_media_id" not in article

    events = await _events(db_session, job["id"])
    assert [event.action for event in events] == [
        "create",
        "submit",
        "pass",
        "approve",
        "publish",
        "succeed",
    ]
    assert (events[-2].from_status, events[-2].to_status) == (
        "approved",
        "publishing",
    )
    assert (events[-1].from_status, events[-1].to_status) == (
        "publishing",
        "published",
    )

    accounts = await client.get("/api/accounts")
    combined = response.text + accounts.text
    assert "wx123" not in combined
    assert '"secret"' not in combined
    assert "tok-abc" not in combined


@respx.mock
async def test_adapter_truncates_title_and_uses_thumb_media_id():
    token_route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-abc"})
    )
    draft_route = respx.post(DRAFT_URL).mock(
        return_value=httpx.Response(200, json={"media_id": "media-1"})
    )
    account = PlatformAccount(
        tenant_id=DEFAULT_TENANT_ID,
        platform="wechat",
        name="直连账号",
        credential_enc=encrypt_secret(json.dumps(CREDENTIAL)),
        status="active",
        meta={"thumb_media_id": "thumb-1"},
    )

    result = await WeChatMPAdapter().publish(
        account=account, title="长" * 100, body="正文"
    )

    assert result.external_id == "media-1"
    assert result.url == ""
    article = json.loads(draft_route.calls[0].request.content)["articles"][0]
    assert article["title"] == "长" * 64
    assert article["thumb_media_id"] == "thumb-1"
    assert article["need_open_comment"] == 0
    assert article["only_fans_can_comment"] == 0
    assert token_route.call_count == 1


@respx.mock
async def test_adapter_uses_fallback_title():
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-abc"})
    )
    draft_route = respx.post(DRAFT_URL).mock(
        return_value=httpx.Response(200, json={"media_id": "media-1"})
    )
    account = PlatformAccount(
        tenant_id=DEFAULT_TENANT_ID,
        platform="wechat",
        name="直连账号",
        credential_enc=encrypt_secret(json.dumps(CREDENTIAL)),
        status="active",
        meta={},
    )

    await WeChatMPAdapter().publish(account=account, title="   ", body="正文")
    article = json.loads(draft_route.calls[0].request.content)["articles"][0]
    assert article["title"] == "未命名"


# --- adapter failures -------------------------------------------------------


@respx.mock
async def test_token_errcode_fails_without_leaking_secrets(client, db_session):
    respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(
                200, json={"errcode": 40001, "errmsg": "invalid credential"}
            ),
            httpx.Response(200, json={"access_token": "tok-abc"}),
        ]
    )
    respx.post(DRAFT_URL).mock(
        return_value=httpx.Response(200, json={"media_id": "media-123"})
    )
    account = await _create_account(client)
    content = await _create_content(client)
    job = await _create_job(client, content["id"], account_id=account["id"])
    await _approve(client, job["id"])

    response = await _execute(client, job["id"])
    assert response.status_code == 502
    body = response.json()
    assert body["code"] == "publish_failed"
    assert "40001" in body["message"]
    assert "secret" not in response.text
    assert "tok-abc" not in response.text

    failed = await _stored_job(db_session, job["id"])
    assert failed.status == "publish_failed"
    assert "40001" in failed.error
    assert "secret" not in failed.error

    retry = await _act(client, job["id"], "retry")
    assert retry.status_code == 200
    assert retry.json()["status"] == "approved"

    recovered = await _execute(client, job["id"])
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "published"
    assert recovered.json()["external_id"] == "media-123"
    assert recovered.json()["attempts"] == 2
    assert recovered.json()["error"] == ""


@respx.mock
async def test_draft_errcode_fails_without_leaking_token(client, db_session):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-abc"})
    )
    respx.post(DRAFT_URL).mock(
        return_value=httpx.Response(
            200, json={"errcode": 40007, "errmsg": "invalid media_id"}
        )
    )
    account = await _create_account(client)
    content = await _create_content(client)
    job = await _create_job(client, content["id"], account_id=account["id"])
    await _approve(client, job["id"])

    response = await _execute(client, job["id"])
    assert response.status_code == 502
    assert response.json()["code"] == "publish_failed"
    assert "40007" in response.json()["message"]
    assert "tok-abc" not in response.text
    assert "secret" not in response.text

    failed = await _stored_job(db_session, job["id"])
    assert failed.status == "publish_failed"
    assert failed.external_id == ""
    assert "40007" in failed.error


# --- preconditions ----------------------------------------------------------


async def test_execute_without_account_returns_409(client):
    content = await _create_content(client)
    job = await _create_job(client, content["id"])
    await _approve(client, job["id"])

    response = await _execute(client, job["id"])
    assert response.status_code == 409
    assert response.json()["code"] == "account_missing"


async def test_execute_with_revoked_account_returns_409(client, db_session):
    account = await _create_account(client)
    content = await _create_content(client)
    job = await _create_job(client, content["id"], account_id=account["id"])
    await _approve(client, job["id"])
    assert (
        await client.delete(f"/api/accounts/{account['id']}")
    ).status_code == 204

    response = await _execute(client, job["id"])
    assert response.status_code == 409
    assert response.json()["code"] == "account_inactive"
    stored = await _stored_job(db_session, job["id"])
    assert stored.status == "approved"


async def test_execute_requires_approved_state(client):
    account = await _create_account(client)
    content = await _create_content(client)
    job = await _create_job(client, content["id"], account_id=account["id"])

    response = await _execute(client, job["id"])
    assert response.status_code == 409
    assert response.json()["code"] == "invalid_state"
    assert response.json()["message"] == "只有审核通过的任务可以执行发布"


async def test_non_wechat_platform_requires_manual_export(client):
    content = await _create_content(client, platform="douyin")
    job = await _create_job(client, content["id"], platform="douyin")

    response = await _execute(client, job["id"])
    assert response.status_code == 409
    assert response.json()["code"] == "manual_export_required"
    assert response.json()["message"] == "该平台请使用导出流程"


async def test_foreign_job_returns_404(client, db_session):
    foreign = await _seed_foreign_job(db_session)

    response = await _execute(client, foreign.id)
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"

    malformed = await _execute(client, "not-a-uuid")
    assert malformed.status_code == 404


async def test_stale_content_version_returns_content_changed(client, db_session):
    account = await _create_account(client)
    content = await _create_content(client)
    job = await _create_job(client, content["id"], account_id=account["id"])
    await _approve(client, job["id"])

    db_session.expire_all()
    stored_content = (
        await db_session.execute(
            select(Content).where(Content.id == uuid.UUID(content["id"]))
        )
    ).scalar_one()
    stored_content.current_version = 2
    await db_session.commit()

    response = await _execute(client, job["id"])
    assert response.status_code == 409
    assert response.json()["code"] == "content_changed"

    stuck = await _stored_job(db_session, job["id"])
    assert stuck.status == "approved"
    assert stuck.attempts == 0


async def test_execute_published_job_returns_invalid_state(client, db_session):
    account = await _create_account(client)
    content = await _create_content(client)
    job = await _create_job(client, content["id"], account_id=account["id"])
    stored = await _stored_job(db_session, job["id"])
    stored.status = "published"
    await db_session.commit()

    response = await _execute(client, job["id"])
    assert response.status_code == 409
    assert response.json()["code"] == "invalid_state"


async def test_corrupted_credentials_returns_sanitized_502(client, db_session):
    account = await _create_account(client)
    content = await _create_content(client)
    job = await _create_job(client, content["id"], account_id=account["id"])
    await _approve(client, job["id"])

    stored_account = await _stored_account(db_session, account["id"])
    stored_account.credential_enc = "broken"
    await db_session.commit()

    listed = await client.get("/api/accounts")
    assert listed.status_code == 200
    assert listed.json()[0]["credential_masked"] == "{}"

    response = await _execute(client, job["id"])
    assert response.status_code == 502
    assert response.json()["code"] == "publish_failed"
    assert response.json()["message"] == "账号凭据不可用，请重新授权"
    assert "broken" not in response.text

    failed = await _stored_job(db_session, job["id"])
    assert failed.status == "publish_failed"
    assert failed.error == "账号凭据不可用，请重新授权"


async def test_apply_action_rejects_opaque_fields(client, db_session):
    content = await _create_content(client)
    job = await _create_job(client, content["id"])

    with pytest.raises(ValueError):
        await apply_action(
            db_session,
            uuid.UUID(job["id"]),
            "approve",
            tenant_id=DEFAULT_TENANT_ID,
            actor="tester@local",
            fields={"status": "published"},
        )

    stored = await _stored_job(db_session, job["id"])
    assert stored.status == "draft"
    assert [event.action for event in await _events(db_session, job["id"])] == [
        "create"
    ]
