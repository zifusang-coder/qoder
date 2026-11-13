import json
import logging
import subprocess
import sys
import uuid

import httpx
import pytest
import respx
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import DEFAULT_TENANT_ID
from app.models.project import Content, ContentVersion, Project
from app.models.publish import PlatformAccount, PublishEvent, PublishJob
from app.publish.base import PublishContent, PublishError, PublishResult
from app.publish.wechat import DRAFT_URL, TOKEN_URL, WeChatMPAdapter
from app.security import decrypt_secret, encrypt_secret
from app.services.publish import apply_action, execute_job
from app.storage import StorageBackend

OTHER_TENANT = "00000000-0000-0000-0000-000000000002"

CREDENTIAL = {"app_id": "wx123", "app_secret": "secret"}


class _FakeStorage(StorageBackend):
    def save_text(self, project_id: str, relpath: str, text: str) -> str:
        raise AssertionError("公众号发布不应写入文件")

    def save_bytes(self, project_id: str, relpath: str, data: bytes) -> str:
        raise AssertionError("公众号发布不应写入文件")

    def read_text(self, project_id: str, relpath: str) -> str:
        raise AssertionError("公众号发布不应读取媒体文件")

    def read_bytes(self, project_id: str, relpath: str) -> bytes:
        raise AssertionError("公众号发布不应读取媒体文件")

    def list_files(self, project_id: str) -> list[str]:
        raise AssertionError("公众号发布不应枚举文件")


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
        "meta": meta
        if meta is not None
        else {"author": "作者甲", "thumb_media_id": "thumb-0"},
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
    masked = json.loads(body["credential_masked"])
    assert masked == {"app_id": "****", "app_secret": "****"}
    # 键名保留，但任何凭据值都不能带出前缀；用值级断言避免误伤 app_secret 键名
    assert all(value == "****" for value in masked.values())
    assert "wx1" not in json.dumps(body)
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
    masked = json.loads(items[0]["credential_masked"])
    assert masked == {"app_id": "****", "app_secret": "****"}
    assert all(value == "****" for value in masked.values())
    assert "wx1" not in response.text
    assert "wx123" not in response.text
    assert '"secret"' not in response.text


async def test_masks_short_and_non_string_credential_values(client, db_session):
    account = PlatformAccount(
        tenant_id=DEFAULT_TENANT_ID,
        platform="wechat",
        name="短凭据",
        credential_enc=encrypt_secret(
            json.dumps({"app_id": "a", "app_secret": "b", "token": 12345})
        ),
        status="active",
        meta={},
    )
    db_session.add(account)
    await db_session.commit()

    response = await client.get("/api/accounts")
    assert response.status_code == 200
    items = response.json()
    masked = json.loads(items[0]["credential_masked"])
    assert masked == {"app_id": "****", "app_secret": "****", "token": "****"}
    assert all(value == "****" for value in masked.values())
    assert "12345" not in response.text


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


async def test_revoke_clears_stored_credentials(client, db_session):
    account = await _create_account(client)

    assert (
        await client.delete(f"/api/accounts/{account['id']}")
    ).status_code == 204

    stored = await _stored_account(db_session, account["id"])
    assert stored.status == "revoked"
    assert stored.name == "主号"
    assert stored.meta == {}
    assert decrypt_secret(stored.credential_enc) == "{}"
    assert json.loads(decrypt_secret(stored.credential_enc)) == {}

    listed = await client.get("/api/accounts")
    assert listed.json()[0]["credential_masked"] == "{}"


async def test_revoke_frees_name_for_recreation(client):
    first = await _create_account(client, name="同名")
    assert (await client.delete(f"/api/accounts/{first['id']}")).status_code == 204

    second = await _create_account(client, name="同名")
    assert second["name"] == "同名"
    assert second["status"] == "active"

    duplicate = await client.post(
        "/api/accounts", json=_account_payload(name="同名")
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "account_exists"


async def test_update_account_rotates_credential(client, db_session):
    account = await _create_account(client)

    response = await client.patch(
        f"/api/accounts/{account['id']}",
        json={"credential": {"app_id": "wx999", "app_secret": "new-secret"}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert json.loads(body["credential_masked"]) == {
        "app_id": "****",
        "app_secret": "****",
    }
    assert "wx999" not in response.text
    assert "new-secret" not in response.text
    assert "wx123" not in response.text
    assert '"secret"' not in response.text

    stored = await _stored_account(db_session, account["id"])
    assert json.loads(decrypt_secret(stored.credential_enc)) == {
        "app_id": "wx999",
        "app_secret": "new-secret",
    }


async def test_update_revoked_account_returns_409(client):
    account = await _create_account(client)
    assert (
        await client.delete(f"/api/accounts/{account['id']}")
    ).status_code == 204

    response = await client.patch(
        f"/api/accounts/{account['id']}", json={"name": "新名字"}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "account_revoked"
    assert response.json()["message"] == "账号已停用，无法修改，请重新创建"


async def test_update_empty_payload_returns_422(client):
    account = await _create_account(client)

    response = await client.patch(f"/api/accounts/{account['id']}", json={})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_update_duplicate_name_returns_409(client):
    await _create_account(client, name="甲号")
    second = await _create_account(client, name="乙号")

    response = await client.patch(
        f"/api/accounts/{second['id']}", json={"name": "甲号"}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "account_exists"


@pytest.mark.parametrize(
    "meta",
    [
        {"need_open_comment": 2},
        {"author": 123},
        {"thumb_media_id": "  "},
        {"unknown": "x"},
    ],
)
async def test_update_meta_validation(client, db_session, meta):
    account = await _create_account(client)

    response = await client.patch(
        f"/api/accounts/{account['id']}", json={"meta": meta}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"

    stored = await _stored_account(db_session, account["id"])
    assert stored.meta == {"author": "作者甲", "thumb_media_id": "thumb-0"}


@respx.mock
async def test_update_meta_merges_instead_of_replacing(client, db_session):
    _, draft_route = _mock_success()
    account = await _create_account(
        client, meta={"author": "作者甲", "thumb_media_id": "thumb-0"}
    )

    response = await client.patch(
        f"/api/accounts/{account['id']}", json={"meta": {"author": "作者乙"}}
    )
    assert response.status_code == 200, response.text

    stored = await _stored_account(db_session, account["id"])
    assert stored.meta == {"author": "作者乙", "thumb_media_id": "thumb-0"}

    content = await _create_content(client)
    job = await _create_job(client, content["id"], account_id=account["id"])
    await _approve(client, job["id"])
    executed = await _execute(client, job["id"])
    assert executed.status_code == 200, executed.text

    article = json.loads(draft_route.calls[0].request.content)["articles"][0]
    assert article["author"] == "作者乙"
    assert article["thumb_media_id"] == "thumb-0"


async def test_update_meta_tolerates_legacy_stored_keys(client, db_session):
    legacy = PlatformAccount(
        tenant_id=DEFAULT_TENANT_ID,
        platform="wechat",
        name="旧账号",
        credential_enc=encrypt_secret(json.dumps(CREDENTIAL)),
        status="active",
        meta={"legacy_key": "x", "author": "旧作者"},
    )
    db_session.add(legacy)
    await db_session.commit()
    await db_session.refresh(legacy)

    response = await client.patch(
        f"/api/accounts/{legacy.id}", json={"meta": {"author": "新作者"}}
    )
    assert response.status_code == 200, response.text
    legacy_id = legacy.id
    db_session.expire_all()
    refreshed = (
        await db_session.execute(
            select(PlatformAccount).where(PlatformAccount.id == legacy_id)
        )
    ).scalar_one()
    assert refreshed.meta == {"author": "新作者"}


async def test_update_meta_tolerates_non_dict_stored_meta(client, db_session):
    # SQLite 的 JSON 列接受 JSON 数组，用来覆盖历史遗留的非 dict meta
    legacy = PlatformAccount(
        tenant_id=DEFAULT_TENANT_ID,
        platform="wechat",
        name="非dict账号",
        credential_enc=encrypt_secret(json.dumps(CREDENTIAL)),
        status="active",
        meta=["legacy"],
    )
    db_session.add(legacy)
    await db_session.commit()
    await db_session.refresh(legacy)
    assert legacy.meta == ["legacy"]

    response = await client.patch(
        f"/api/accounts/{legacy.id}", json={"meta": {"author": "新作者"}}
    )
    assert response.status_code == 200, response.text
    legacy_id = legacy.id
    db_session.expire_all()
    refreshed = (
        await db_session.execute(
            select(PlatformAccount).where(PlatformAccount.id == legacy_id)
        )
    ).scalar_one()
    assert refreshed.meta == {"author": "新作者"}


async def test_update_account_cross_tenant_or_malformed_404(client, db_session):
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

    foreign_response = await client.patch(
        f"/api/accounts/{foreign.id}", json={"name": "改名"}
    )
    assert foreign_response.status_code == 404
    assert foreign_response.json()["code"] == "not_found"

    malformed = await client.patch(
        "/api/accounts/not-a-uuid", json={"name": "改名"}
    )
    assert malformed.status_code == 404
    assert malformed.json()["code"] == "not_found"


async def test_update_invalid_credential_is_422_without_leak(client, db_session):
    account = await _create_account(client)

    response = await client.patch(
        f"/api/accounts/{account['id']}", json={"credential": {"app_id": "wx-new"}}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert "wx-new" not in response.text
    assert CREDENTIAL["app_secret"] not in response.text

    stored = await _stored_account(db_session, account["id"])
    assert stored.name == "主号"
    assert json.loads(decrypt_secret(stored.credential_enc)) == CREDENTIAL


@pytest.mark.parametrize(
    "overrides",
    [
        {"meta": {"author": "a" * 65}},
        {"meta": {"thumb_media_id": "t" * 201}},
        {"name": "n" * 101},
    ],
)
async def test_account_field_length_limits(client, overrides):
    response = await client.post(
        "/api/accounts", json=_account_payload(**overrides)
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "n" * 101},
        {"meta": {"author": "a" * 65}},
        {"meta": {"thumb_media_id": "t" * 201}},
    ],
)
async def test_update_field_length_limits(client, db_session, overrides):
    account = await _create_account(client)

    response = await client.patch(
        f"/api/accounts/{account['id']}", json=overrides
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"

    listed = await client.get("/api/accounts")
    item = next(i for i in listed.json() if i["id"] == account["id"])
    assert item["name"] == "主号"
    stored = await _stored_account(db_session, account["id"])
    assert stored.meta == {"author": "作者甲", "thumb_media_id": "thumb-0"}


@pytest.mark.parametrize(
    "meta",
    [
        {"need_open_comment": True},
        {"thumb_media_id": "  "},
        {"author": 123},
    ],
)
async def test_create_rejects_invalid_meta_types(client, meta):
    response = await client.post(
        "/api/accounts", json=_account_payload(meta=meta)
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


# --- happy path -------------------------------------------------------------


@respx.mock
async def test_happy_path_creates_wechat_draft(client, db_session, caplog):
    token_route, draft_route = _mock_success()
    account = await _create_account(
        client,
        meta={
            "author": "作者甲",
            "thumb_media_id": "thumb-0",
            "need_open_comment": 1,
            "only_fans_can_comment": 0,
        },
    )
    content = await _create_content(client)
    job = await _create_job(client, content["id"], account_id=account["id"])
    await _approve(client, job["id"])

    with caplog.at_level(logging.DEBUG):
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
    assert article["thumb_media_id"] == "thumb-0"

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
    # 即使根 logger 开到 DEBUG，httpx 的 URL 日志也不能带出 access_token
    assert "tok-abc" not in caplog.text


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
        account=account,
        content=PublishContent(
            project_id="p", title="长" * 100, body="正文", kind="article"
        ),
        storage=_FakeStorage(),
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
        meta={"thumb_media_id": "thumb-1"},
    )

    await WeChatMPAdapter().publish(
        account=account,
        content=PublishContent(
            project_id="p", title="   ", body="正文", kind="article"
        ),
        storage=_FakeStorage(),
    )
    article = json.loads(draft_route.calls[0].request.content)["articles"][0]
    assert article["title"] == "未命名"


@respx.mock
async def test_digest_unescapes_html_entities():
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
        meta={"thumb_media_id": "thumb-1"},
    )

    await WeChatMPAdapter().publish(
        account=account,
        content=PublishContent(
            project_id="p", title="标题", body="A & B", kind="article"
        ),
        storage=_FakeStorage(),
    )
    article = json.loads(draft_route.calls[0].request.content)["articles"][0]
    assert article["digest"] == "A & B"


def test_httpx_loggers_configured_by_adapter_import():
    # 子进程从干净的 logger 状态验证适配器导入即生效（本模块已导入过适配器）
    script = (
        "import logging, app.publish.wechat; "
        "assert logging.getLogger('httpx').level == logging.WARNING; "
        "assert logging.getLogger('httpcore').level == logging.WARNING"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=".",
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


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


@respx.mock
async def test_missing_thumb_media_id_fails_before_network(client, db_session):
    token_route, draft_route = _mock_success()
    account = await _create_account(client, meta={"author": "作者甲"})
    content = await _create_content(client)
    job = await _create_job(client, content["id"], account_id=account["id"])
    await _approve(client, job["id"])

    response = await _execute(client, job["id"])
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "account_misconfigured"
    assert "thumb_media_id" in body["message"]

    assert token_route.call_count == 0
    assert draft_route.call_count == 0
    failed = await _stored_job(db_session, job["id"])
    assert failed.status == "publish_failed"
    assert failed.attempts == 0


@respx.mock
async def test_config_error_does_not_consume_attempts(client, db_session):
    token_route, draft_route = _mock_success()
    account = await _create_account(client, meta={"author": "作者甲"})
    content = await _create_content(client)
    job = await _create_job(client, content["id"], account_id=account["id"])
    await _approve(client, job["id"])

    stored = await _stored_job(db_session, job["id"])
    stored.attempts = 4
    await db_session.commit()

    response = await _execute(client, job["id"])
    assert response.status_code == 409
    assert response.json()["code"] == "account_misconfigured"
    assert token_route.call_count == 0
    assert draft_route.call_count == 0

    failed = await _stored_job(db_session, job["id"])
    assert failed.status == "publish_failed"
    assert failed.attempts == 4


@respx.mock
async def test_attempt_limit_boundary(client, db_session):
    _, draft_route = _mock_success()
    account = await _create_account(client)
    content = await _create_content(client)
    job = await _create_job(client, content["id"], account_id=account["id"])
    await _approve(client, job["id"])

    stored = await _stored_job(db_session, job["id"])
    stored.attempts = 4
    await db_session.commit()

    response = await _execute(client, job["id"])
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "published"
    assert response.json()["attempts"] == 5
    assert draft_route.call_count == 1


async def test_unexpected_adapter_exception_is_sanitized(client, db_session):
    account = await _create_account(client)
    content = await _create_content(client)
    job = await _create_job(client, content["id"], account_id=account["id"])
    await _approve(client, job["id"])

    class BoomAdapter:
        platform = "wechat"
        capability = "auto"

        async def publish(self, *, account, content, storage):
            raise RuntimeError("boom wx123 secret")

    with pytest.raises(PublishError) as excinfo:
        await execute_job(
            db_session,
            uuid.UUID(job["id"]),
            BoomAdapter(),
            tenant_id=DEFAULT_TENANT_ID,
            actor="tester@local",
            storage=_FakeStorage(),
        )

    assert str(excinfo.value) == "发布执行失败（RuntimeError）"
    failed = await _stored_job(db_session, job["id"])
    assert failed.status == "publish_failed"
    assert failed.attempts == 1
    for marker in ("wx123", "secret", "boom"):
        assert marker not in str(excinfo.value)
        assert marker not in failed.error


async def test_in_flight_marker_is_persisted_before_adapter_call(
    client, db_session
):
    account = await _create_account(client)
    content = await _create_content(client)
    job = await _create_job(client, content["id"], account_id=account["id"])
    await _approve(client, job["id"])
    job_uuid = uuid.UUID(job["id"])
    observed: dict[str, object] = {}
    commits: list[bool] = []

    def _record_commit(_: object) -> None:
        commits.append(True)

    class InspectingAdapter:
        platform = "wechat"
        capability = "auto"

        async def publish(self, *, account, content, storage):
            # 独立会话读取同一引擎；after_commit 证明此刻状态已真正落库，
            # 避免 StaticPool 共享连接看到未提交的 flush 而产生假阳性
            async with AsyncSession(bind=db_session.bind) as probe:
                current = (
                    await probe.execute(
                        select(PublishJob).where(PublishJob.id == job_uuid)
                    )
                ).scalar_one()
            observed["status"] = current.status
            observed["error"] = current.error
            observed["committed"] = bool(commits)
            return PublishResult(external_id="media-inspected")

    event.listen(db_session.sync_session, "after_commit", _record_commit)
    try:
        result = await execute_job(
            db_session,
            job_uuid,
            InspectingAdapter(),
            tenant_id=DEFAULT_TENANT_ID,
            actor="tester@local",
            storage=_FakeStorage(),
        )
    finally:
        event.remove(db_session.sync_session, "after_commit", _record_commit)

    assert observed == {
        "status": "publishing",
        "error": "发布请求已提交，等待平台确认",
        "committed": True,
    }
    assert result.status == "published"
    assert result.external_id == "media-inspected"


@respx.mock
async def test_execute_twice_publishes_once(client, db_session):
    _, draft_route = _mock_success()
    account = await _create_account(client)
    content = await _create_content(client)
    job = await _create_job(client, content["id"], account_id=account["id"])
    await _approve(client, job["id"])

    first = await _execute(client, job["id"])
    assert first.status_code == 200, first.text

    second = await _execute(client, job["id"])
    assert second.status_code == 409
    assert second.json()["code"] == "invalid_state"
    assert draft_route.call_count == 1


@respx.mock
async def test_attempt_limit_blocks_execution(client, db_session):
    token_route, draft_route = _mock_success()
    account = await _create_account(client)
    content = await _create_content(client)
    job = await _create_job(client, content["id"], account_id=account["id"])
    await _approve(client, job["id"])

    stored = await _stored_job(db_session, job["id"])
    stored.attempts = 5
    await db_session.commit()

    response = await _execute(client, job["id"])
    assert response.status_code == 409
    assert response.json()["code"] == "too_many_attempts"
    assert response.json()["message"] == "发布尝试次数已达上限，请人工确认平台状态"
    assert token_route.call_count == 0
    assert draft_route.call_count == 0
    assert (await _stored_job(db_session, job["id"])).status == "approved"


@respx.mock
async def test_missing_access_token_is_sanitized(client, db_session):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"errcode": 0})
    )
    draft_route = respx.post(DRAFT_URL).mock(
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
    assert "access_token" in body["message"]
    assert "wx123" not in response.text
    assert '"secret"' not in response.text
    assert draft_route.call_count == 0

    failed = await _stored_job(db_session, job["id"])
    assert failed.status == "publish_failed"
    assert "access_token" in failed.error


@respx.mock
async def test_token_credentials_are_sent_in_body(client, db_session):
    token_route, _ = _mock_success()
    account = await _create_account(client)
    content = await _create_content(client)
    job = await _create_job(client, content["id"], account_id=account["id"])
    await _approve(client, job["id"])

    response = await _execute(client, job["id"])
    assert response.status_code == 200, response.text

    request = token_route.calls[0].request
    assert json.loads(request.content) == {
        "grant_type": "client_credential",
        "appid": "wx123",
        "secret": "secret",
        "force_refresh": False,
    }
    assert "secret" not in str(request.url)
    assert "wx123" not in str(request.url)


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


async def test_corrupted_credentials_returns_sanitized_409(client, db_session):
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
    assert response.status_code == 409
    assert response.json()["code"] == "account_misconfigured"
    assert response.json()["message"] == "公众号凭据不可用，请重新授权"
    assert "broken" not in response.text

    failed = await _stored_job(db_session, job["id"])
    assert failed.status == "publish_failed"
    assert failed.error == "公众号凭据不可用，请重新授权"
    assert failed.attempts == 0


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


@pytest.mark.parametrize(
    "fields",
    [
        {"external_id": "x" * 201},
        {"external_url": "x" * 1001},
        {"error": "x" * 1001},
        {"export_path": "x" * 1001},
        {"attempts": -1},
        {"attempts": True},
        {"published_at": "2026-10-27"},
    ],
)
async def test_apply_action_rejects_invalid_field_values(
    client, db_session, fields
):
    content = await _create_content(client)
    job = await _create_job(client, content["id"])

    with pytest.raises(ValueError):
        await apply_action(
            db_session,
            uuid.UUID(job["id"]),
            "approve",
            tenant_id=DEFAULT_TENANT_ID,
            actor="tester@local",
            fields=fields,
        )

    stored = await _stored_job(db_session, job["id"])
    assert stored.status == "draft"
    assert stored.attempts == 0
    assert stored.external_id == ""
    assert [event.action for event in await _events(db_session, job["id"])] == [
        "create"
    ]
