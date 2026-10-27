import uuid

import pytest
from sqlalchemy import select

from app.models.profile import Profile, ProfileMemory

OTHER_TENANT_ID = "11111111-1111-1111-1111-111111111111"
PROFILE_ARRAY_FIELDS = ["taboos", "goals", "platform_focus"]


def _payload(**overrides) -> dict:
    values = {
        "name": "主账号",
        "account_name": "小王的效率笔记",
        "positioning": "给职场人讲 AI 工具",
        "audience": "25-35 岁职场人",
        "tone": "直接、有案例、不说教",
        "taboos": ["夸大收益", "贬低同行"],
        "goals": ["涨粉", "引流私域"],
        "platform_focus": ["wechat", "xiaohongshu"],
    }
    values.update(overrides)
    return values


async def _create_profile(client, **overrides) -> str:
    resp = await client.post("/api/profiles", json=_payload(**overrides))
    assert resp.status_code == 201
    return resp.json()["id"]


def _foreign_profile() -> Profile:
    return Profile(tenant_id=OTHER_TENANT_ID, name="其他租户账号")


async def test_profile_crud_flow(client):
    created = await client.post("/api/profiles", json=_payload())
    assert created.status_code == 201
    body = created.json()
    profile_id = body["id"]
    assert body["name"] == "主账号"
    assert body["account_name"] == "小王的效率笔记"
    assert body["taboos"] == ["夸大收益", "贬低同行"]
    assert body["platform_focus"] == ["wechat", "xiaohongshu"]
    assert body["is_active"] is True
    assert "tenant_id" not in created.text

    listed = await client.get("/api/profiles")
    assert listed.status_code == 200
    assert [p["id"] for p in listed.json()["items"]] == [profile_id]
    assert "tenant_id" not in listed.text

    fetched = await client.get(f"/api/profiles/{profile_id}")
    assert fetched.status_code == 200
    assert fetched.json() == body
    assert "tenant_id" not in fetched.text

    updated = await client.patch(
        f"/api/profiles/{profile_id}",
        json={"tone": "克制、有数据", "is_active": False},
    )
    assert updated.status_code == 200
    assert updated.json()["tone"] == "克制、有数据"
    assert updated.json()["is_active"] is False
    assert updated.json()["name"] == "主账号"

    deleted = await client.delete(f"/api/profiles/{profile_id}")
    assert deleted.status_code == 204
    assert deleted.content == b""

    gone = await client.get(f"/api/profiles/{profile_id}")
    assert gone.status_code == 404
    assert gone.json()["code"] == "not_found"


@pytest.mark.parametrize("name", ["", "   "])
async def test_profile_name_must_not_be_blank(client, name):
    resp = await client.post("/api/profiles", json=_payload(name=name))
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"


async def test_profile_name_required(client):
    payload = _payload()
    del payload["name"]
    resp = await client.post("/api/profiles", json=payload)
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"


@pytest.mark.parametrize(
    ("field", "value"),
    [("name", "名" * 101), ("account_name", "a" * 101)],
)
async def test_profile_field_max_length(client, field, value):
    resp = await client.post("/api/profiles", json=_payload(**{field: value}))
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"


async def test_profile_name_is_stripped(client):
    created = await client.post("/api/profiles", json=_payload(name="  主账号  "))
    assert created.status_code == 201
    profile_id = created.json()["id"]
    assert created.json()["name"] == "主账号"

    fetched = await client.get(f"/api/profiles/{profile_id}")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "主账号"


@pytest.mark.parametrize("field", PROFILE_ARRAY_FIELDS)
async def test_profile_array_max_items(client, field):
    resp = await client.post(
        "/api/profiles", json=_payload(**{field: ["条目"] * 21})
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"


@pytest.mark.parametrize("field", PROFILE_ARRAY_FIELDS)
async def test_profile_array_item_max_length(client, field):
    resp = await client.post(
        "/api/profiles", json=_payload(**{field: ["项" * 101]})
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"


@pytest.mark.parametrize(
    "field",
    ["name", "account_name", "tone", "goals", "is_active"],
)
async def test_profile_patch_rejects_explicit_null(client, field):
    profile_id = await _create_profile(client)

    resp = await client.patch(f"/api/profiles/{profile_id}", json={field: None})
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_payload"
    assert resp.json()["message"] == f"{field} 不能为空"

    fetched = await client.get(f"/api/profiles/{profile_id}")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "主账号"


@pytest.mark.parametrize("method", ["GET", "PATCH", "DELETE"])
async def test_profile_invalid_uuid_returns_404(client, method):
    kwargs = {"json": {}} if method == "PATCH" else {}
    resp = await client.request(method, "/api/profiles/not-a-uuid", **kwargs)
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


async def test_memory_invalid_uuid_returns_404(client):
    profile_id = await _create_profile(client)

    bad_profile = await client.get("/api/profiles/not-a-uuid/memories")
    assert bad_profile.status_code == 404
    assert bad_profile.json()["code"] == "not_found"

    bad_memory = await client.delete(
        f"/api/profiles/{profile_id}/memories/not-a-uuid"
    )
    assert bad_memory.status_code == 404
    assert bad_memory.json()["code"] == "not_found"


async def test_foreign_tenant_profile_is_invisible(client, db_session):
    foreign = _foreign_profile()
    db_session.add(foreign)
    await db_session.commit()
    foreign_id = foreign.id

    listed = await client.get("/api/profiles")
    assert listed.status_code == 200
    assert listed.json()["items"] == []

    fetched = await client.get(f"/api/profiles/{foreign_id}")
    assert fetched.status_code == 404

    patched = await client.patch(
        f"/api/profiles/{foreign_id}", json={"tone": "篡改"}
    )
    assert patched.status_code == 404

    deleted = await client.delete(f"/api/profiles/{foreign_id}")
    assert deleted.status_code == 404

    db_session.expire_all()
    assert await db_session.get(Profile, foreign_id) is not None


async def test_foreign_tenant_memories_are_invisible(client, db_session):
    foreign = _foreign_profile()
    db_session.add(foreign)
    await db_session.commit()
    foreign_id = foreign.id
    memory = ProfileMemory(
        tenant_id=OTHER_TENANT_ID,
        profile_id=foreign_id,
        content="其他租户的记忆",
    )
    db_session.add(memory)
    await db_session.commit()
    memory_id = memory.id

    listed = await client.get(f"/api/profiles/{foreign_id}/memories")
    assert listed.status_code == 404

    deleted = await client.delete(
        f"/api/profiles/{foreign_id}/memories/{memory_id}"
    )
    assert deleted.status_code == 404

    db_session.expire_all()
    assert await db_session.get(ProfileMemory, memory_id) is not None


async def test_memory_from_another_tenant_not_deletable(client, db_session):
    profile_id = await _create_profile(client)

    foreign = _foreign_profile()
    db_session.add(foreign)
    await db_session.commit()
    memory = ProfileMemory(
        tenant_id=OTHER_TENANT_ID,
        profile_id=foreign.id,
        content="越权记忆",
    )
    db_session.add(memory)
    await db_session.commit()
    memory_id = memory.id

    resp = await client.delete(
        f"/api/profiles/{profile_id}/memories/{memory_id}"
    )
    assert resp.status_code == 404

    db_session.expire_all()
    assert await db_session.get(ProfileMemory, memory_id) is not None


async def test_profiles_require_auth(auth_client):
    resp = await auth_client.get("/api/profiles")
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


async def test_memory_write_requires_auth(auth_client):
    resp = await auth_client.post(
        f"/api/profiles/{uuid.uuid4()}/memories", json={"content": "x"}
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


async def test_memory_crud_and_ordering(client):
    profile_id = await _create_profile(client)

    low = await client.post(
        f"/api/profiles/{profile_id}/memories", json={"content": "低权重"}
    )
    assert low.status_code == 201
    assert low.json()["weight"] == 1.0
    assert low.json()["kind"] == "insight"
    assert low.json()["tags"] == []
    assert low.json()["source"] == "manual"
    assert "tenant_id" not in low.text
    low_id = low.json()["id"]

    high = await client.post(
        f"/api/profiles/{profile_id}/memories",
        json={
            "content": "高权重",
            "weight": 5.0,
            "kind": "review",
            "tags": ["数据"],
        },
    )
    assert high.status_code == 201

    mid = await client.post(
        f"/api/profiles/{profile_id}/memories",
        json={"content": "中权重", "weight": 3.0},
    )
    assert mid.status_code == 201

    listed = await client.get(f"/api/profiles/{profile_id}/memories")
    assert listed.status_code == 200
    assert [m["content"] for m in listed.json()] == [
        "高权重",
        "中权重",
        "低权重",
    ]
    assert "tenant_id" not in listed.text

    deleted = await client.delete(
        f"/api/profiles/{profile_id}/memories/{low_id}"
    )
    assert deleted.status_code == 204

    again = await client.delete(
        f"/api/profiles/{profile_id}/memories/{low_id}"
    )
    assert again.status_code == 404
    assert again.json()["code"] == "not_found"

    unknown = await client.delete(
        f"/api/profiles/{profile_id}/memories/{uuid.uuid4()}"
    )
    assert unknown.status_code == 404


@pytest.mark.parametrize(
    "content",
    ["", "   ", "字" * 5001],
    ids=["empty", "whitespace", "5001-chars"],
)
async def test_memory_content_boundaries(client, content):
    profile_id = await _create_profile(client)

    resp = await client.post(
        f"/api/profiles/{profile_id}/memories", json={"content": content}
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"


async def test_memory_content_at_max_length_is_accepted(client):
    profile_id = await _create_profile(client)
    content = "记" * 5000

    resp = await client.post(
        f"/api/profiles/{profile_id}/memories", json={"content": content}
    )
    assert resp.status_code == 201
    assert resp.json()["content"] == content


async def test_memory_kind_max_length(client):
    profile_id = await _create_profile(client)

    resp = await client.post(
        f"/api/profiles/{profile_id}/memories",
        json={"content": "有效内容", "kind": "k" * 33},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"


@pytest.mark.parametrize(
    "tags",
    [["tag"] * 21, ["t" * 51]],
    ids=["21-items", "item-51-chars"],
)
async def test_memory_tags_boundaries(client, tags):
    profile_id = await _create_profile(client)

    resp = await client.post(
        f"/api/profiles/{profile_id}/memories",
        json={"content": "有效内容", "tags": tags},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"


async def test_delete_profile_cascades_memories(client, db_session):
    profile_id = uuid.UUID(await _create_profile(client))
    created = await client.post(
        f"/api/profiles/{profile_id}/memories", json={"content": "级联删除"}
    )
    memory_id = uuid.UUID(created.json()["id"])

    resp = await client.delete(f"/api/profiles/{profile_id}")
    assert resp.status_code == 204

    memories = await client.get(f"/api/profiles/{profile_id}/memories")
    assert memories.status_code == 404

    db_session.expire_all()
    rows = (
        await db_session.execute(
            select(ProfileMemory).where(
                ProfileMemory.profile_id == profile_id
            )
        )
    ).scalars().all()
    assert rows == []
    assert await db_session.get(ProfileMemory, memory_id) is None
