import csv
import io
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models.analytics import AnalyticsSnapshot
from app.models.project import Content
from app.models.user import User

REQUIRED_COLUMNS = ("content_id", "platform", "captured_at")
METRIC_FIELDS = ("views", "likes", "comments", "shares", "saves", "followers_delta")
MAX_ERRORS = 100
MAX_ERROR_LENGTH = 199


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise AppError("invalid_csv", "CSV 必须为 UTF-8 编码", 422) from None


def _error(line: int, message: str) -> str:
    return f"第 {line} 行：{message}"[:MAX_ERROR_LENGTH]


def _parse_date(value: str) -> date | None:
    text = value.strip()
    if len(text) != 10:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _parse_metric(value: str) -> int | None:
    try:
        number = int(value)
    except ValueError:
        return None
    if number < 0:
        return None
    return number


def _raw_row(row: dict) -> dict[str, str]:
    return {
        str(key): "" if value is None else str(value)
        for key, value in row.items()
        if key is not None
    }


async def import_csv(
    session: AsyncSession, user: User, data: bytes
) -> tuple[int, list[str]]:
    reader = csv.DictReader(io.StringIO(_decode(data)))
    fieldnames = reader.fieldnames or []
    missing = [name for name in REQUIRED_COLUMNS if name not in fieldnames]
    if missing:
        raise AppError(
            "invalid_csv", f"CSV 缺少必需列：{'、'.join(missing)}", 422
        )

    contents: dict[uuid.UUID, Content | None] = {}
    snapshots: dict[tuple[uuid.UUID, str, date], AnalyticsSnapshot] = {}
    errors: list[str] = []
    upserted = 0

    def fail(line: int, message: str) -> None:
        if len(errors) < MAX_ERRORS:
            errors.append(_error(line, message))

    try:
        for line, row in enumerate(reader, start=2):
            raw_content_id = (row.get("content_id") or "").strip()
            try:
                content_id = uuid.UUID(raw_content_id)
            except (ValueError, TypeError):
                fail(line, "内容不存在或不属于当前租户")
                continue
            if content_id not in contents:
                contents[content_id] = (
                    await session.execute(
                        select(Content).where(
                            Content.id == content_id,
                            Content.tenant_id == user.tenant_id,
                        )
                    )
                ).scalar_one_or_none()
            content = contents[content_id]
            if content is None:
                fail(line, "内容不存在或不属于当前租户")
                continue

            captured_at = _parse_date(row.get("captured_at") or "")
            if captured_at is None:
                fail(line, "日期格式应为 YYYY-MM-DD")
                continue

            platform = (row.get("platform") or "").strip()
            if not platform:
                platform = content.platform
            if not 1 <= len(platform) <= 32:
                fail(line, "platform 长度应为 1-32 个字符")
                continue

            metrics: dict[str, int] = {}
            for field in METRIC_FIELDS:
                raw_value = (row.get(field) or "").strip()
                if not raw_value:
                    metrics[field] = 0
                    continue
                number = _parse_metric(raw_value)
                if number is None:
                    fail(line, f"{field} 必须是非负整数")
                    break
                metrics[field] = number
            else:
                key = (content.id, platform, captured_at)
                snapshot = snapshots.get(key)
                if snapshot is None:
                    snapshot = (
                        await session.execute(
                            select(AnalyticsSnapshot).where(
                                AnalyticsSnapshot.tenant_id == user.tenant_id,
                                AnalyticsSnapshot.content_id == content.id,
                                AnalyticsSnapshot.platform == platform,
                                AnalyticsSnapshot.captured_at == captured_at,
                            )
                        )
                    ).scalar_one_or_none()
                    if snapshot is None:
                        snapshot = AnalyticsSnapshot(
                            tenant_id=user.tenant_id,
                            content_id=content.id,
                            platform=platform,
                            captured_at=captured_at,
                        )
                        session.add(snapshot)
                    snapshots[key] = snapshot
                for field in METRIC_FIELDS:
                    setattr(snapshot, field, metrics[field])
                snapshot.raw = _raw_row(row)
                upserted += 1
    except csv.Error:
        raise AppError("invalid_csv", "CSV 解析失败", 422) from None

    await session.commit()
    return upserted, errors
