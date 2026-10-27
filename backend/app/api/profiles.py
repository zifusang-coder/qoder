import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import get_current_user
from app.errors import AppError
from app.models.profile import Profile, ProfileMemory
from app.models.user import User

router = APIRouter(prefix="/profiles", tags=["profiles"])

_NAME = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]
_ACCOUNT_NAME = Annotated[str, StringConstraints(max_length=100)]
_PROFILE_TEXT = Annotated[str, StringConstraints(max_length=5000)]
_PROFILE_ITEM = Annotated[str, StringConstraints(max_length=100)]
_TAG = Annotated[str, StringConstraints(max_length=50)]
_MEMORY_CONTENT = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)
]
_MEMORY_KIND = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)
]


class ProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: _NAME
    account_name: _ACCOUNT_NAME = ""
    positioning: _PROFILE_TEXT = ""
    audience: _PROFILE_TEXT = ""
    tone: _PROFILE_TEXT = ""
    taboos: list[_PROFILE_ITEM] = Field(default_factory=list, max_length=20)
    goals: list[_PROFILE_ITEM] = Field(default_factory=list, max_length=20)
    platform_focus: list[_PROFILE_ITEM] = Field(default_factory=list, max_length=20)


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: _NAME | None = None
    account_name: _ACCOUNT_NAME | None = None
    positioning: _PROFILE_TEXT | None = None
    audience: _PROFILE_TEXT | None = None
    tone: _PROFILE_TEXT | None = None
    taboos: list[_PROFILE_ITEM] | None = Field(default=None, max_length=20)
    goals: list[_PROFILE_ITEM] | None = Field(default=None, max_length=20)
    platform_focus: list[_PROFILE_ITEM] | None = Field(default=None, max_length=20)
    is_active: bool | None = None


class ProfileOut(ProfileIn):
    id: str
    is_active: bool


class ProfileList(BaseModel):
    items: list[ProfileOut]


class MemoryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: _MEMORY_CONTENT
    kind: _MEMORY_KIND = "insight"
    weight: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] = 1.0
    tags: list[_TAG] = Field(default_factory=list, max_length=20)


class MemoryOut(MemoryIn):
    id: str
    source: str


def _parse_uuid(value: str, message: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError):
        raise AppError("not_found", message, 404) from None


def _out(profile: Profile) -> ProfileOut:
    return ProfileOut(
        id=str(profile.id),
        name=profile.name,
        account_name=profile.account_name,
        positioning=profile.positioning,
        audience=profile.audience,
        tone=profile.tone,
        taboos=list(profile.taboos or []),
        goals=list(profile.goals or []),
        platform_focus=list(profile.platform_focus or []),
        is_active=profile.is_active,
    )


def _memory_out(memory: ProfileMemory) -> MemoryOut:
    return MemoryOut(
        id=str(memory.id),
        content=memory.content,
        kind=memory.kind,
        weight=memory.weight,
        tags=list(memory.tags or []),
        source=memory.source,
    )


async def _owned_profile(
    session: AsyncSession, profile_id: str, user: User
) -> Profile:
    parsed = _parse_uuid(profile_id, "账号画像不存在")
    profile = (
        await session.execute(
            select(Profile).where(
                Profile.id == parsed,
                Profile.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if profile is None:
        raise AppError("not_found", "账号画像不存在", 404)
    return profile


@router.get("", response_model=ProfileList)
async def list_profiles(
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ProfileList:
    rows = (
        await session.execute(
            select(Profile)
            .where(Profile.tenant_id == user.tenant_id)
            .order_by(Profile.created_at, Profile.id)
        )
    ).scalars()
    return ProfileList(items=[_out(row) for row in rows])


@router.post("", response_model=ProfileOut, status_code=201)
async def create_profile(
    payload: ProfileIn,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ProfileOut:
    profile = Profile(
        tenant_id=user.tenant_id,
        name=payload.name,
        account_name=payload.account_name,
        positioning=payload.positioning,
        audience=payload.audience,
        tone=payload.tone,
        taboos=payload.taboos,
        goals=payload.goals,
        platform_focus=payload.platform_focus,
    )
    session.add(profile)
    await session.commit()
    await session.refresh(profile)
    return _out(profile)


@router.get("/{profile_id}", response_model=ProfileOut)
async def get_profile(
    profile_id: str,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ProfileOut:
    return _out(await _owned_profile(session, profile_id, user))


@router.patch("/{profile_id}", response_model=ProfileOut)
async def update_profile(
    profile_id: str,
    payload: ProfileUpdate,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ProfileOut:
    profile = await _owned_profile(session, profile_id, user)

    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        if value is None:
            raise AppError("invalid_payload", f"{field} 不能为空", 422)
    for field, value in data.items():
        setattr(profile, field, value)

    await session.commit()
    await session.refresh(profile)
    return _out(profile)


@router.delete("/{profile_id}", status_code=204)
async def delete_profile(
    profile_id: str,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    profile = await _owned_profile(session, profile_id, user)
    await session.delete(profile)
    await session.commit()
    return Response(status_code=204)


@router.get("/{profile_id}/memories", response_model=list[MemoryOut])
async def list_memories(
    profile_id: str,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[MemoryOut]:
    profile = await _owned_profile(session, profile_id, user)
    rows = (
        await session.execute(
            select(ProfileMemory)
            .where(
                ProfileMemory.profile_id == profile.id,
                ProfileMemory.tenant_id == user.tenant_id,
            )
            .order_by(
                ProfileMemory.weight.desc(),
                ProfileMemory.created_at.desc(),
                ProfileMemory.id,
            )
        )
    ).scalars()
    return [_memory_out(row) for row in rows]


@router.post(
    "/{profile_id}/memories",
    response_model=MemoryOut,
    status_code=201,
)
async def create_memory(
    profile_id: str,
    payload: MemoryIn,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> MemoryOut:
    profile = await _owned_profile(session, profile_id, user)
    memory = ProfileMemory(
        tenant_id=user.tenant_id,
        profile_id=profile.id,
        kind=payload.kind,
        content=payload.content,
        weight=payload.weight,
        source="manual",
        tags=payload.tags,
    )
    session.add(memory)
    await session.commit()
    await session.refresh(memory)
    return _memory_out(memory)


@router.delete("/{profile_id}/memories/{memory_id}", status_code=204)
async def delete_memory(
    profile_id: str,
    memory_id: str,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    profile = await _owned_profile(session, profile_id, user)
    parsed_memory_id = _parse_uuid(memory_id, "记忆不存在")
    memory = (
        await session.execute(
            select(ProfileMemory).where(
                ProfileMemory.id == parsed_memory_id,
                ProfileMemory.profile_id == profile.id,
                ProfileMemory.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if memory is None:
        raise AppError("not_found", "记忆不存在", 404)
    await session.delete(memory)
    await session.commit()
    return Response(status_code=204)
