import json
import uuid
from datetime import UTC, datetime, timedelta

from app.models.publish import PlatformAccount
from app.security import decrypt_secret

WECHAT = {"app_id": "wx-1", "app_secret": "wx-secret-1"}
DOUYIN = {
    "client_key": "ck-1",
    "client_secret": "cs-secret-1",
    "open_id": "open-1",
    "refresh_token": "rt-secret-1",
}


def _payload(platform="wechat", name=None, credential=None, meta=None):
    return {
        "platform": platform,
        "name": name or f"账号-{uuid.uuid4().hex[:6]}",
        "credential": credential if credential is not None else {},
        "meta": meta or {},
    }


async def _create(client, **overrides):
    return await client.post("/api/accounts", json=_payload(**overrides))


async def test_same_platform_allows_multiple_accounts(client, db_session):
    first = await _create(
        client,
        name="主号",
        credential=WECHAT,
        meta={"thumb_media_id": "thumb-1"},
    )
    second = await _create(
        client,
        name="副号",
        credential=WECHAT,
        meta={"thumb_media_id": "thumb-2"},
    )

    assert first.status_code == 201
    assert second.status_code == 201

    # SQLite 时间戳只有秒级精度，显式错开创建时间才能验证倒序
    older = await db_session.get(
        PlatformAccount, uuid.UUID(first.json()["id"])
    )
    older.created_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()

    items = (await client.get("/api/accounts")).json()
    names = [item["name"] for item in items if item["platform"] == "wechat"]
    assert names == ["副号", "主号"]


async def test_same_name_same_platform_conflicts(client):
    assert (
        await _create(
            client,
            name="重名",
            credential=WECHAT,
            meta={"thumb_media_id": "t1"},
        )
    ).status_code == 201
    duplicate = await _create(
        client,
        name="重名",
        credential=WECHAT,
        meta={"thumb_media_id": "t2"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "account_exists"


async def test_same_name_across_platforms_is_allowed(client):
    assert (
        await _create(
            client,
            platform="wechat",
            name="同名",
            credential=WECHAT,
            meta={"thumb_media_id": "t1"},
        )
    ).status_code == 201
    assert (
        await _create(
            client,
            platform="douyin",
            name="同名",
            credential=DOUYIN,
        )
    ).status_code == 201


async def test_platform_filter_and_order(client):
    await _create(
        client,
        platform="douyin",
        credential=DOUYIN,
    )
    await _create(
        client,
        platform="wechat",
        credential=WECHAT,
        meta={"thumb_media_id": "t1"},
    )

    response = await client.get("/api/accounts")
    assert [item["platform"] for item in response.json()] == [
        "wechat",
        "douyin",
    ]

    filtered = await client.get("/api/accounts", params={"platform": "douyin"})
    assert [item["platform"] for item in filtered.json()] == ["douyin"]

    missing = await client.get("/api/accounts", params={"platform": "unknown"})
    assert missing.status_code == 404


async def test_dynamic_credential_validation(client):
    missing = await _create(client, platform="douyin", credential={})
    assert missing.status_code == 422

    unsupported = await _create(
        client,
        platform="douyin",
        credential={**DOUYIN, "app_secret": "nope"},
    )
    assert unsupported.status_code == 422

    ok = await _create(client, platform="douyin", credential=DOUYIN)
    assert ok.status_code == 201


async def test_update_rejects_platform_and_invalid_credential(client):
    created = await _create(client, platform="douyin", credential=DOUYIN)
    assert created.status_code == 201
    account_id = created.json()["id"]

    platform_change = await client.patch(
        f"/api/accounts/{account_id}", json={"platform": "wechat"}
    )
    assert platform_change.status_code == 422

    bad_credential = await client.patch(
        f"/api/accounts/{account_id}",
        json={"credential": {"app_id": "wx-1", "app_secret": "x"}},
    )
    assert bad_credential.status_code == 422


async def test_manual_platform_accepts_only_empty_credential(client):
    ok = await _create(
        client, platform="xiaohongshu", credential={}, meta={}
    )
    assert ok.status_code == 201
    assert ok.json()["capability"] == "manual_export"

    rejected = await _create(
        client,
        platform="xiaohongshu",
        credential={"token": "x"},
    )
    assert rejected.status_code == 422


async def test_meta_is_validated_per_platform(client):
    toutiao = await _create(
        client,
        platform="toutiao",
        credential=DOUYIN,
        meta={"author": "作者"},
    )
    assert toutiao.status_code == 201
    assert toutiao.json()["meta"] == {"author": "作者"}

    bad_meta = await _create(
        client,
        platform="toutiao",
        credential=DOUYIN,
        meta={"thumb_media_id": "t1"},
    )
    assert bad_meta.status_code == 422
    assert bad_meta.json()["code"] == "validation_error"


async def test_platforms_endpoint_exposes_matrix_without_secrets(client):
    response = await client.get("/api/accounts/platforms")
    assert response.status_code == 200
    specs = response.json()
    assert [item["key"] for item in specs] == [
        "wechat",
        "douyin",
        "toutiao",
        "xiaohongshu",
        "zhihu",
        "kuaishou",
        "channels",
        "baijiahao",
        "bilibili",
    ]
    by_key = {item["key"]: item for item in specs}
    assert by_key["wechat"]["capability"] == "auto_publish"
    assert by_key["xiaohongshu"]["capability"] == "manual_export"
    assert by_key["baijiahao"]["capability"] == "pending"
    secret_fields = {
        field["key"]
        for field in by_key["douyin"]["credential_fields"]
        if field["secret"]
    }
    assert secret_fields == {"client_secret", "refresh_token"}
    for spec in specs:
        assert set(spec) == {
            "key",
            "label",
            "capability",
            "content_capabilities",
            "credential_fields",
            "meta_fields",
            "note",
        }
    for field in by_key["douyin"]["credential_fields"]:
        assert set(field) == {
            "key",
            "label",
            "secret",
            "required",
            "placeholder",
        }


async def test_credentials_are_masked_and_encrypted(client, db_session):
    response = await _create(client, platform="douyin", credential=DOUYIN)
    assert response.status_code == 201
    body = response.json()
    masked = json.loads(body["credential_masked"])
    assert masked == {
        "client_key": "****",
        "client_secret": "****",
        "open_id": "****",
        "refresh_token": "****",
    }
    for marker in ("cs-secret-1", "rt-secret-1"):
        assert marker not in response.text

    account = await db_session.get(PlatformAccount, uuid.UUID(body["id"]))
    assert "cs-secret-1" not in account.credential_enc
    assert json.loads(decrypt_secret(account.credential_enc)) == DOUYIN
