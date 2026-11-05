import logging
import math
from dataclasses import dataclass

import httpx
from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.profile import ProfileMemory
from app.models.settings import LLMProvider
from app.models.vector import EMBEDDING_DIM
from app.security import decrypt_secret

logger = logging.getLogger(__name__)

TIMEOUT = 30.0
BATCH_SIZE = 10


@dataclass(frozen=True)
class EmbeddingConfig:
    base_url: str
    api_key: str
    model: str


async def load_embedding_config(
    session: AsyncSession, tenant_id: str
) -> EmbeddingConfig | None:
    """复用默认 LLM 提供商的地址与密钥，模型由 EMBEDDING_MODEL 指定。

    未配置模型、缺少提供商或提供商不是 OpenAI 兼容类型时返回 None，
    调用方据此回退到权重排序。
    """
    model = get_settings().embedding_model.strip()
    if not model:
        return None
    provider = (
        await session.execute(
            select(LLMProvider)
            .where(
                LLMProvider.tenant_id == tenant_id,
                LLMProvider.is_default.is_(True),
            )
            .order_by(LLMProvider.created_at, LLMProvider.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if provider is None:
        return None
    if provider.kind != "openai_compatible":
        logger.warning("嵌入模型要求 OpenAI 兼容提供商，当前类型为 %s", provider.kind)
        return None
    try:
        api_key = decrypt_secret(provider.api_key_enc)
    except (InvalidToken, UnicodeDecodeError):
        logger.warning("嵌入模型密钥无法解密，请重新填写提供商密钥")
        return None
    return EmbeddingConfig(
        base_url=provider.base_url.rstrip("/"),
        api_key=api_key,
        model=model,
    )


async def _request_batch(
    config: EmbeddingConfig, texts: list[str]
) -> list[list[float]] | None:
    payload = {
        "model": config.model,
        "input": texts,
        "dimensions": EMBEDDING_DIM,
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(
                f"{config.base_url}/embeddings",
                headers={
                    "Authorization": f"Bearer {config.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except httpx.HTTPError as exc:
        # 只记录异常类型，避免把带密钥的上游信息写进日志
        logger.warning("嵌入请求失败: %s", type(exc).__name__)
        return None
    if not response.is_success:
        logger.warning("嵌入提供商返回错误（%s）", response.status_code)
        return None
    try:
        data = response.json()
        items = data["data"]
        if not isinstance(items, list):
            raise TypeError("data 不是列表")
        ordered = sorted(items, key=lambda item: item.get("index", 0))
        vectors = [[float(value) for value in item["embedding"]] for item in ordered]
    except (AttributeError, KeyError, TypeError, ValueError):
        logger.warning("嵌入返回格式异常")
        return None
    if len(vectors) != len(texts) or any(
        len(vector) != EMBEDDING_DIM for vector in vectors
    ):
        logger.warning("嵌入返回维度不符合预期（期望 %s）", EMBEDDING_DIM)
        return None
    return vectors


async def embed_texts(
    config: EmbeddingConfig, texts: list[str]
) -> list[list[float]] | None:
    if not texts:
        return []
    vectors: list[list[float]] = []
    for start in range(0, len(texts), BATCH_SIZE):
        batch = texts[start : start + BATCH_SIZE]
        batch_vectors = await _request_batch(config, batch)
        if batch_vectors is None:
            return None
        vectors.extend(batch_vectors)
    return vectors


async def embed_memories(
    session: AsyncSession,
    tenant_id: str,
    memories: list[ProfileMemory],
) -> bool:
    """为记忆写入向量；任何失败都只记录日志，不阻塞业务写入。"""
    if not memories:
        return False
    config = await load_embedding_config(session, tenant_id)
    if config is None:
        return False
    vectors = await embed_texts(config, [memory.content for memory in memories])
    if vectors is None:
        return False
    for memory, vector in zip(memories, vectors, strict=True):
        memory.embedding = vector
    return True


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return -1.0
    return dot / (norm_a * norm_b)


async def search_memories(
    session: AsyncSession,
    *,
    tenant_id: str,
    profile_id,
    query: str,
    limit: int,
) -> list[ProfileMemory] | None:
    """按语义相似度检索记忆；未配置或调用失败时返回 None 供调用方回退。"""
    text = query.strip()
    if not text or limit <= 0:
        return None
    config = await load_embedding_config(session, tenant_id)
    if config is None:
        return None
    vectors = await embed_texts(config, [text])
    if not vectors:
        return None
    vector = vectors[0]

    conditions = (
        ProfileMemory.tenant_id == tenant_id,
        ProfileMemory.profile_id == profile_id,
        ProfileMemory.embedding.is_not(None),
    )
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        try:
            rows = (
                await session.execute(
                    select(ProfileMemory)
                    .where(*conditions)
                    .order_by(
                        ProfileMemory.embedding.cosine_distance(vector),
                        ProfileMemory.weight.desc(),
                        ProfileMemory.id,
                    )
                    .limit(limit)
                )
            ).scalars().all()
        except SQLAlchemyError as exc:
            logger.warning("向量检索失败，回退到权重排序: %s", type(exc).__name__)
            return None
        return list(rows)

    # 测试环境（SQLite）不加载 pgvector，退化为本地余弦计算
    rows = (
        await session.execute(select(ProfileMemory).where(*conditions))
    ).scalars().all()
    ranked = sorted(
        rows,
        key=lambda memory: (_cosine(vector, memory.embedding or []), memory.weight),
        reverse=True,
    )
    return ranked[:limit]


async def backfill_embeddings(
    session: AsyncSession, tenant_id: str, *, limit: int = 200
) -> int:
    """为缺少向量的记忆补生成 embedding，返回成功条数。"""
    config = await load_embedding_config(session, tenant_id)
    if config is None:
        return 0
    rows = (
        await session.execute(
            select(ProfileMemory)
            .where(
                ProfileMemory.tenant_id == tenant_id,
                ProfileMemory.embedding.is_(None),
            )
            .order_by(ProfileMemory.created_at, ProfileMemory.id)
            .limit(limit)
        )
    ).scalars().all()
    done = 0
    for start in range(0, len(rows), BATCH_SIZE):
        chunk = rows[start : start + BATCH_SIZE]
        vectors = await embed_texts(config, [memory.content for memory in chunk])
        if vectors is None:
            break
        for memory, vector in zip(chunk, vectors, strict=True):
            memory.embedding = vector
            done += 1
        await session.flush()
    await session.commit()
    return done
