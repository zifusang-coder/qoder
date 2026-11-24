import argparse
import asyncio
import sys

from app.config import DEFAULT_TENANT_ID
from app.db import SessionLocal
from app.services.embeddings import (
    backfill_embeddings,
    load_embedding_config,
)


async def _main() -> int:
    parser = argparse.ArgumentParser(description="为缺失向量的画像记忆生成嵌入")
    parser.add_argument("--tenant", default=DEFAULT_TENANT_ID)
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()

    async with SessionLocal() as session:
        config = await load_embedding_config(session, args.tenant)
        if config is None:
            print("未启用向量回填：请配置 EMBEDDING_MODEL 和默认 OpenAI 兼容提供商")
            return 1
        count = await backfill_embeddings(session, args.tenant, limit=args.limit)
    print(f"已生成 {count} 条记忆向量")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
