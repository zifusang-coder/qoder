import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import DEFAULT_TENANT_ID
from app.models.settings import LLMProvider
from app.security import decrypt_secret, encrypt_secret

OTHER_TENANT_ID = "11111111-1111-1111-1111-111111111111"
INVARIANT_TENANT_ID = "22222222-2222-2222-2222-222222222222"
REAL_KEY = "sk-real-key-123456"


def _other_tenant_provider(**overrides) -> LLMProvider:
    values = {
        "tenant_id": OTHER_TENANT_ID,
        "name": "其他租户提供商",
        "kind": "openai_compatible",
        "base_url": "https://other.example.com/v1",
        "api_key_enc": encrypt_secret("sk-other-key-123456"),
        "tiers": {},
        "is_default": False,
    }
    values.update(overrides)
    return LLMProvider(**values)


def _payload(name: str, **overrides) -> dict:
    values = {
        "name": name,
        "kind": "openai_compatible",
        "base_url": "https://api.example.com/v1/",
        "api_key": REAL_KEY,
        "tiers": {"fast": "gpt-x"},
        "is_default": False,
    }
    values.update(overrides)
    return values


def test_secret_roundtrip_and_mask():
    enc = encrypt_secret(REAL_KEY)
    assert enc != REAL_KEY
    assert decrypt_secret(enc) == REAL_KEY


async def test_create_provider_masks_key(auth_client, auth_headers, db_session):
    payload = _payload("主提供商", is_default=True)
    resp = await auth_client.post(
        "/api/settings/providers", json=payload, headers=auth_headers
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["api_key_masked"] == "sk-****3456"
    assert body["base_url"] == "https://api.example.com/v1"
    assert body["is_default"] is True
    assert payload["api_key"] not in resp.text

    row = (await db_session.execute(select(LLMProvider))).scalar_one()
    assert row.api_key_enc != payload["api_key"]
    assert decrypt_secret(row.api_key_enc) == payload["api_key"]


async def test_validation_error_does_not_echo_api_key(auth_client, auth_headers):
    resp = await auth_client.post(
        "/api/settings/providers",
        json=_payload("短密钥", api_key="sk-9"),
        headers=auth_headers,
    )

    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "validation_error"
    assert "sk-9" not in resp.text
    entry = next(d for d in body["detail"] if d["loc"][-1] == "api_key")
    assert entry["loc"] == ["body", "api_key"]
    assert "input" not in entry
    assert "ctx" not in entry


async def test_list_providers_scoped_to_tenant(auth_client, auth_headers, db_session):
    db_session.add(_other_tenant_provider())
    await db_session.commit()

    created = await auth_client.post(
        "/api/settings/providers", json=_payload("我的提供商"), headers=auth_headers
    )
    assert created.status_code == 201

    resp = await auth_client.get("/api/settings/providers", headers=auth_headers)
    assert resp.status_code == 200
    assert [p["name"] for p in resp.json()] == ["我的提供商"]
    assert REAL_KEY not in resp.text


async def test_default_provider_exclusive_per_tenant(
    auth_client, auth_headers, db_session
):
    foreign_default = _other_tenant_provider(name="其他租户默认", is_default=True)
    db_session.add(foreign_default)
    await db_session.commit()

    first = await auth_client.post(
        "/api/settings/providers",
        json=_payload("默认 A", is_default=True),
        headers=auth_headers,
    )
    assert first.status_code == 201

    second = await auth_client.post(
        "/api/settings/providers",
        json=_payload("默认 B", is_default=True),
        headers=auth_headers,
    )
    assert second.status_code == 201

    resp = await auth_client.get("/api/settings/providers", headers=auth_headers)
    by_name = {p["name"]: p for p in resp.json()}
    assert by_name["默认 A"]["is_default"] is False
    assert by_name["默认 B"]["is_default"] is True

    await db_session.refresh(foreign_default)
    assert foreign_default.is_default is True


async def test_delete_provider_404_cases(auth_client, auth_headers, db_session):
    missing = await auth_client.delete(
        f"/api/settings/providers/{uuid.uuid4()}", headers=auth_headers
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"

    malformed = await auth_client.delete(
        "/api/settings/providers/not-a-uuid", headers=auth_headers
    )
    assert malformed.status_code == 404
    assert malformed.json()["code"] == "not_found"

    foreign = _other_tenant_provider()
    db_session.add(foreign)
    await db_session.commit()

    blocked = await auth_client.delete(
        f"/api/settings/providers/{foreign.id}", headers=auth_headers
    )
    assert blocked.status_code == 404
    assert blocked.json()["code"] == "not_found"
    assert await db_session.get(LLMProvider, foreign.id) is not None

    created = await auth_client.post(
        "/api/settings/providers", json=_payload("待删除"), headers=auth_headers
    )
    assert created.status_code == 201
    provider_id = uuid.UUID(created.json()["id"])

    deleted = await auth_client.delete(
        f"/api/settings/providers/{provider_id}", headers=auth_headers
    )
    assert deleted.status_code == 204
    assert await db_session.get(LLMProvider, provider_id) is None


async def test_corrupt_ciphertext_falls_back_to_placeholder(
    auth_client, auth_headers, db_session
):
    corrupt = "not-a-valid-fernet-token"
    db_session.add(
        LLMProvider(
            tenant_id=DEFAULT_TENANT_ID,
            name="损坏密文",
            kind="openai_compatible",
            base_url="https://broken.example.com/v1",
            api_key_enc=corrupt,
            tiers={},
            is_default=False,
        )
    )
    await db_session.commit()

    resp = await auth_client.get("/api/settings/providers", headers=auth_headers)

    assert resp.status_code == 200
    by_name = {p["name"]: p for p in resp.json()}
    assert by_name["损坏密文"]["api_key_masked"] == "****"
    assert corrupt not in resp.text


async def test_providers_require_auth(auth_client):
    resp = await auth_client.get("/api/settings/providers")
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


async def test_provider_write_requires_auth(auth_client):
    created = await auth_client.post(
        "/api/settings/providers", json=_payload("未认证")
    )
    assert created.status_code == 401
    assert created.json()["code"] == "unauthorized"

    deleted = await auth_client.delete(f"/api/settings/providers/{uuid.uuid4()}")
    assert deleted.status_code == 401
    assert deleted.json()["code"] == "unauthorized"


async def test_api_key_max_length_rejected(auth_client, auth_headers):
    long_key = "sk-" + "a" * 510
    assert len(long_key) == 513

    resp = await auth_client.post(
        "/api/settings/providers",
        json=_payload("超长密钥", api_key=long_key),
        headers=auth_headers,
    )

    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"
    assert long_key not in resp.text


async def test_base_url_max_length_rejected(auth_client, auth_headers):
    long_url = "https://example.com/" + "a" * 490
    assert len(long_url) > 500

    resp = await auth_client.post(
        "/api/settings/providers",
        json=_payload("超长地址", base_url=long_url),
        headers=auth_headers,
    )

    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"


@pytest.mark.parametrize("base_url", ["api.example.com/v1", "ftp://api.example.com/v1"])
async def test_base_url_scheme_rejected(auth_client, auth_headers, base_url):
    resp = await auth_client.post(
        "/api/settings/providers",
        json=_payload("错误协议", base_url=base_url),
        headers=auth_headers,
    )

    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"


async def test_invalid_kind_rejected(auth_client, auth_headers):
    resp = await auth_client.post(
        "/api/settings/providers",
        json=_payload("错误类型", kind="azure"),
        headers=auth_headers,
    )

    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"


async def test_single_default_enforced_by_db(db_session):
    db_session.add(
        _other_tenant_provider(
            tenant_id=INVARIANT_TENANT_ID, name="DB 约束默认 A", is_default=True
        )
    )
    await db_session.commit()

    db_session.add(
        _other_tenant_provider(
            tenant_id=INVARIANT_TENANT_ID, name="DB 约束默认 B", is_default=True
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

    rows = (
        await db_session.execute(
            select(LLMProvider).where(LLMProvider.tenant_id == INVARIANT_TENANT_ID)
        )
    ).scalars().all()
    assert [row.name for row in rows] == ["DB 约束默认 A"]
