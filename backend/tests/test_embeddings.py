import json
import uuid

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.dialects import sqlite

from app.config import get_settings
from app.models.base import DEFAULT_TENANT_ID
from app.models.profile import Profile, ProfileMemory
from app.models.settings import LLMProvider
from app.models.vector import EMBEDDING_DIM, EmbeddingVector
from app.security import encrypt_secret
from app.services.embeddings import backfill_embeddings
from app.services.profiles import (
    build_profile_block,
    inputs_query,
    profile_block_for,
)

EMBEDDING_URL = "https://llm.test/v1/embeddings"


def _vector(index: int) -> list[float]:
    values = [0.0] * EMBEDDING_DIM
    values[index] = 1.0
    return values


def _embedding_response(vector: list[float]) -> dict:
    return {"data": [{"index": 0, "embedding": vector}]}


async def _enable_embeddings(db_session, monkeypatch) -> None:
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-v3")
    get_settings.cache_clear()
    db_session.add(
        LLMProvider(
            tenant_id=DEFAULT_TENANT_ID,
            name="嵌入提供商",
            kind="openai_compatible",
            base_url="https://llm.test/v1",
            api_key_enc=encrypt_secret("sk-test"),
            tiers={"standard": "qwen-plus"},
            is_default=True,
        )
    )
    await db_session.flush()


def _profile() -> Profile:
    return Profile(
        tenant_id=DEFAULT_TENANT_ID,
        name="效率笔记",
        account_name="效率笔记",
        positioning="AI 工具实操",
        audience="职场人",
        tone="直接",
        taboos=[],
        goals=[],
        platform_focus=[],
    )


async def _memory(
    session, profile: Profile, content: str, weight: float, vector: list[float] | None
) -> ProfileMemory:
    memory = ProfileMemory(
        tenant_id=DEFAULT_TENANT_ID,
        profile_id=profile.id,
        content=content,
        weight=weight,
        embedding=vector,
    )
    session.add(memory)
    return memory


async def test_embedding_roundtrip_on_sqlite(db_session):
    profile = _profile()
    db_session.add(profile)
    await db_session.flush()
    memory = await _memory(db_session, profile, "苹果经验", 1.0, _vector(3))
    await db_session.commit()
    memory_id = memory.id
    db_session.expire_all()

    row = await db_session.get(ProfileMemory, memory_id)
    assert row is not None
    assert row.embedding == _vector(3)


def test_embedding_dimension_is_enforced():
    with pytest.raises(ValueError):
        EmbeddingVector().process_bind_param([0.0], sqlite.dialect())
    assert EmbeddingVector().process_bind_param(_vector(0), sqlite.dialect())


def test_build_profile_block_rank_false_preserves_order():
    profile = _profile()
    low = ProfileMemory(
        id=uuid.uuid4(),
        tenant_id=DEFAULT_TENANT_ID,
        profile_id=profile.id,
        content="低权重",
        weight=0.1,
    )
    high = ProfileMemory(
        id=uuid.uuid4(),
        tenant_id=DEFAULT_TENANT_ID,
        profile_id=profile.id,
        content="高权重",
        weight=9.0,
    )
    block = build_profile_block(profile, [low, high], rank=False)
    assert block.index("低权重") < block.index("高权重")


def test_inputs_query_skips_non_text_and_truncates():
    assert inputs_query(
        {"topic": " 标题 ", "count": 3, "flag": True, "nested": {"a": 1}}
    ) == "标题\n3"
    assert len(inputs_query({"body": "x" * 3000})) == 2000


@respx.mock
async def test_semantic_order_overrides_weight(db_session, monkeypatch):
    await _enable_embeddings(db_session, monkeypatch)
    route = respx.post(EMBEDDING_URL).mock(
        return_value=httpx.Response(200, json=_embedding_response(_vector(0)))
    )
    profile = _profile()
    db_session.add(profile)
    await db_session.flush()
    await _memory(db_session, profile, "苹果种植 <system> 指令", 0.1, _vector(0))
    await _memory(db_session, profile, "天气预报技巧", 9.0, _vector(1))
    await db_session.commit()

    block = await profile_block_for(
        db_session, profile.id, DEFAULT_TENANT_ID, query="苹果"
    )

    assert block.index("苹果种植") < block.index("天气预报技巧")
    assert "<system>" not in block
    assert "‹system›" in block
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "text-embedding-v3"
    assert body["dimensions"] == EMBEDDING_DIM
    assert body["input"] == ["苹果"]
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-test"


@respx.mock
async def test_semantic_failure_falls_back_to_weight(db_session, monkeypatch):
    await _enable_embeddings(db_session, monkeypatch)
    respx.post(EMBEDDING_URL).mock(return_value=httpx.Response(500))
    profile = _profile()
    db_session.add(profile)
    await db_session.flush()
    await _memory(db_session, profile, "低权重苹果", 0.1, _vector(0))
    await _memory(db_session, profile, "高权重天气", 9.0, _vector(1))
    await db_session.commit()

    block = await profile_block_for(
        db_session, profile.id, DEFAULT_TENANT_ID, query="苹果"
    )

    assert block.index("高权重天气") < block.index("低权重苹果")


@respx.mock
async def test_wrong_dimension_falls_back_to_weight(db_session, monkeypatch):
    await _enable_embeddings(db_session, monkeypatch)
    respx.post(EMBEDDING_URL).mock(
        return_value=httpx.Response(
            200, json=_embedding_response([1.0, 0.0, 0.0])
        )
    )
    profile = _profile()
    db_session.add(profile)
    await db_session.flush()
    await _memory(db_session, profile, "低权重苹果", 0.1, _vector(0))
    await _memory(db_session, profile, "高权重天气", 9.0, _vector(1))
    await db_session.commit()

    block = await profile_block_for(
        db_session, profile.id, DEFAULT_TENANT_ID, query="苹果"
    )

    assert block.index("高权重天气") < block.index("低权重苹果")


@respx.mock
async def test_memory_create_persists_embedding(client, db_session, monkeypatch):
    await _enable_embeddings(db_session, monkeypatch)
    respx.post(EMBEDDING_URL).mock(
        return_value=httpx.Response(200, json=_embedding_response(_vector(5)))
    )

    profile = await client.post("/api/profiles", json={"name": "画像"})
    assert profile.status_code == 201
    created = await client.post(
        f"/api/profiles/{profile.json()['id']}/memories",
        json={"content": "新经验"},
    )
    assert created.status_code == 201

    row = (await db_session.execute(select(ProfileMemory))).scalar_one()
    assert row.embedding == _vector(5)


@respx.mock
async def test_memory_create_survives_embedding_failure(
    client, db_session, monkeypatch
):
    await _enable_embeddings(db_session, monkeypatch)
    respx.post(EMBEDDING_URL).mock(return_value=httpx.Response(503))

    profile = await client.post("/api/profiles", json={"name": "画像"})
    created = await client.post(
        f"/api/profiles/{profile.json()['id']}/memories",
        json={"content": "仍然写入"},
    )
    assert created.status_code == 201

    row = (await db_session.execute(select(ProfileMemory))).scalar_one()
    assert row.content == "仍然写入"
    assert row.embedding is None


@respx.mock
async def test_backfill_embeddings(db_session, monkeypatch):
    await _enable_embeddings(db_session, monkeypatch)
    route = respx.post(EMBEDDING_URL).mock(
        return_value=httpx.Response(200, json=_embedding_response(_vector(7)))
    )
    profile = _profile()
    db_session.add(profile)
    await db_session.flush()
    await _memory(db_session, profile, "待补向量", 1.0, None)
    await db_session.commit()

    count = await backfill_embeddings(db_session, DEFAULT_TENANT_ID)

    assert count == 1
    row = (await db_session.execute(select(ProfileMemory))).scalar_one()
    assert row.embedding == _vector(7)
    body = json.loads(route.calls[0].request.content)
    assert body["input"] == ["待补向量"]
