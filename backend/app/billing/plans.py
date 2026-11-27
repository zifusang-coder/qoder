import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import Plan

FREE_PLAN_ID = "1f0a0000-0000-4000-8000-000000000001"
BASIC_PLAN_ID = "1f0a0000-0000-4000-8000-000000000002"
PRO_PLAN_ID = "1f0a0000-0000-4000-8000-000000000003"
ENTERPRISE_PLAN_ID = "1f0a0000-0000-4000-8000-000000000004"

DEFAULT_PLAN_CODE = "free"
DEFAULT_PLAN_ID = FREE_PLAN_ID

PLAN_SEEDS: tuple[dict, ...] = (
    {
        "id": FREE_PLAN_ID,
        "code": "free",
        "name": "免费版",
        "description": "小团队试跑：基础额度，超出不计费。",
        "price_cents": 0,
        "limits": {
            "llm.prompt_tokens": 200_000,
            "llm.completion_tokens": 100_000,
            "run.skill": 500,
            "run.agent": 100,
            "run.workflow": 50,
            "media.jobs": 50,
            "media.seconds": 1_800,
        },
        "prices": {},
    },
    {
        "id": BASIC_PLAN_ID,
        "code": "basic",
        "name": "基础版",
        "description": "日常创作与发布，含超额按量计费。",
        "price_cents": 9_900,
        "limits": {
            "llm.prompt_tokens": 2_000_000,
            "llm.completion_tokens": 1_000_000,
            "run.skill": 5_000,
            "run.agent": 1_000,
            "run.workflow": 300,
            "media.jobs": 300,
            "media.seconds": 18_000,
        },
        "prices": {"llm.prompt_tokens": 1, "llm.completion_tokens": 3},
    },
    {
        "id": PRO_PLAN_ID,
        "code": "pro",
        "name": "专业版",
        "description": "多账号矩阵与更高 Agent/工作流额度。",
        "price_cents": 39_900,
        "limits": {
            "llm.prompt_tokens": 10_000_000,
            "llm.completion_tokens": 5_000_000,
            "run.skill": 20_000,
            "run.agent": 5_000,
            "run.workflow": 1_000,
            "media.jobs": 1_000,
            "media.seconds": 72_000,
        },
        "prices": {"llm.prompt_tokens": 1, "llm.completion_tokens": 3},
    },
    {
        "id": ENTERPRISE_PLAN_ID,
        "code": "enterprise",
        "name": "企业版",
        "description": "团队协作与大规模内容生产。",
        "price_cents": 199_900,
        "limits": {
            "llm.prompt_tokens": 50_000_000,
            "llm.completion_tokens": 25_000_000,
            "run.skill": 100_000,
            "run.agent": 20_000,
            "run.workflow": 5_000,
            "media.jobs": 5_000,
            "media.seconds": 360_000,
        },
        "prices": {"llm.prompt_tokens": 1, "llm.completion_tokens": 3},
    },
)


async def ensure_plans(session: AsyncSession) -> None:
    existing = set((await session.execute(select(Plan.code))).scalars().all())
    for seed in PLAN_SEEDS:
        if seed["code"] in existing:
            continue
        session.add(
            Plan(
                id=uuid.UUID(seed["id"]),
                code=seed["code"],
                name=seed["name"],
                description=seed["description"],
                price_cents=seed["price_cents"],
                currency="CNY",
                limits=dict(seed["limits"]),
                prices=dict(seed["prices"]),
                status="active",
            )
        )
