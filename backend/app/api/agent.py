from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.policy import (
    clamped_overrides,
    default_action,
    load_overrides,
    save_overrides,
    validate_overrides,
)
from app.agent.registry import get_tool_registry
from app.db import get_session
from app.models.user import User
from app.permissions import admin_access

router = APIRouter(prefix="/agent", tags=["agent"])


class PolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overrides: dict[str, Any] = {}


class PolicyOut(BaseModel):
    overrides: dict[str, str]
    defaults: dict[str, str]


@router.get("/policy", response_model=PolicyOut)
async def get_policy(
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> PolicyOut:
    registry = get_tool_registry()
    specs = {spec.name: spec for spec in registry.all()}
    overrides = clamped_overrides(
        await load_overrides(session, user.tenant_id), specs
    )
    defaults = {
        f"tool:{name}": default_action(spec.risk) for name, spec in specs.items()
    }
    return PolicyOut(overrides=overrides, defaults=defaults)


@router.put("/policy", response_model=PolicyOut)
async def put_policy(
    payload: PolicyIn,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> PolicyOut:
    registry = get_tool_registry()
    specs = {spec.name: spec for spec in registry.all()}
    overrides = validate_overrides(payload.overrides, specs)
    saved = await save_overrides(session, user.tenant_id, overrides)
    defaults = {
        f"tool:{name}": default_action(spec.risk) for name, spec in specs.items()
    }
    return PolicyOut(overrides=saved, defaults=defaults)
