import csv
import io
import re
import uuid
from collections import Counter
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models.analytics import AnalyticsSnapshot
from app.models.project import Content
from app.models.user import User

REQUIRED_COLUMNS = ("content_id", "platform", "captured_at")
METRIC_FIELDS = ("views", "likes", "comments", "shares", "saves", "followers_delta")
MAX_ERRORS = 100
MAX_ERROR_LENGTH = 199
MAX_METRIC = 2**63 - 1
MAX_METRIC_DIGITS = len(str(MAX_METRIC))
CONTENT_LOOKUP_CHUNK = 500
UPSERT_CHUNK = 500

_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_METRIC_RE = re.compile(r"[0-9]+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

_INSERT_BY_DIALECT = {
    "postgresql": postgresql_insert,
    "sqlite": sqlite_insert,
}


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise AppError("invalid_csv", "CSV 必须为 UTF-8 编码", 422) from None


def _error(line: int, message: str) -> str:
    return f"第 {line} 行：{message}"[:MAX_ERROR_LENGTH]


def _parse_date(value: str) -> date | None:
    text = value.strip()
    if _DATE_RE.fullmatch(text) is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _contains_nul(value: object) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, list):
        return any(_contains_nul(item) for item in value)
    return False


def _raw_row(row: dict) -> dict[str, str]:
    return {
        str(key): "" if value is None else str(value)
        for key, value in row.items()
        if key is not None
    }


def _parse_row(
    row: dict, content: Content
) -> tuple[date, str, dict[str, int]] | str:
    """校验单行数据，返回解析结果或中文错误信息。"""
    if any(_contains_nul(value) for value in row.values()):
        return "字段包含 NUL 字符"

    captured_at = _parse_date(row.get("captured_at") or "")
    if captured_at is None:
        return "日期格式应为 YYYY-MM-DD"

    platform = (row.get("platform") or "").strip()
    if platform and _CONTROL_RE.search(platform):
        return "platform 包含非法控制字符"
    if not platform:
        platform = content.platform
    elif platform != content.platform:
        return "平台与内容不一致"
    if not 1 <= len(platform) <= 32:
        return "platform 长度应为 1-32 个字符"

    metrics: dict[str, int] = {}
    for field in METRIC_FIELDS:
        raw_value = (row.get(field) or "").strip()
        if not raw_value:
            metrics[field] = 0
            continue
        if _METRIC_RE.fullmatch(raw_value) is None:
            return f"{field} 必须是非负整数"
        digits = raw_value.lstrip("0") or "0"
        # 先按位数拦截，避免超长数字触发 int() 的位数上限
        if len(digits) > MAX_METRIC_DIGITS:
            return f"{field} 数值过大"
        number = int(digits)
        if number > MAX_METRIC:
            return f"{field} 数值过大"
        metrics[field] = number
    return captured_at, platform, metrics


def _upsert_statement(session: AsyncSession, rows: list[dict]):
    insert = _INSERT_BY_DIALECT.get(session.get_bind().dialect.name)
    if insert is None:
        raise AppError("unsupported_database", "当前数据库不支持数据导入", 500)
    statement = insert(AnalyticsSnapshot).values(rows)
    assignments = {
        field: getattr(statement.excluded, field) for field in METRIC_FIELDS
    }
    assignments["raw"] = statement.excluded.raw
    return statement.on_conflict_do_update(
        index_elements=["tenant_id", "content_id", "platform", "captured_at"],
        set_=assignments,
    )


async def _load_contents(
    session: AsyncSession,
    tenant_id: str,
    rows: list[tuple[int, dict]],
) -> dict[uuid.UUID, Content]:
    content_ids: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for _, row in rows:
        raw_content_id = (row.get("content_id") or "").strip()
        try:
            content_id = uuid.UUID(raw_content_id)
        except (ValueError, TypeError):
            continue
        if content_id not in seen:
            seen.add(content_id)
            content_ids.append(content_id)

    contents: dict[uuid.UUID, Content] = {}
    for start in range(0, len(content_ids), CONTENT_LOOKUP_CHUNK):
        chunk = content_ids[start : start + CONTENT_LOOKUP_CHUNK]
        found = (
            await session.execute(
                select(Content).where(
                    Content.tenant_id == tenant_id,
                    Content.id.in_(chunk),
                )
            )
        ).scalars().all()
        for content in found:
            contents[content.id] = content
    return contents


async def import_csv(
    session: AsyncSession, user: User, data: bytes
) -> tuple[int, list[str]]:
    reader = csv.DictReader(io.StringIO(_decode(data)))
    try:
        fieldnames = [name.strip() for name in (reader.fieldnames or [])]
    except csv.Error:
        await session.rollback()
        raise AppError("invalid_csv", "CSV 格式错误", 422) from None

    if any("\x00" in name for name in fieldnames):
        raise AppError("invalid_csv", "CSV 列名包含非法字符", 422)
    duplicated = [name for name, count in Counter(fieldnames).items() if count > 1]
    if duplicated:
        shown = "、".join(duplicated[:5])
        raise AppError("invalid_csv", f"CSV 列名重复：{shown}", 422)
    missing = [name for name in REQUIRED_COLUMNS if name not in fieldnames]
    if missing:
        raise AppError(
            "invalid_csv", f"CSV 缺少必需列：{'、'.join(missing)}", 422
        )
    reader.fieldnames = fieldnames

    rows: list[tuple[int, dict]] = []
    try:
        for line, row in enumerate(reader, start=2):
            rows.append((line, row))
    except csv.Error:
        await session.rollback()
        raise AppError("invalid_csv", "CSV 格式错误", 422) from None

    contents = await _load_contents(session, user.tenant_id, rows)
    errors: list[str] = []
    pending: dict[tuple[uuid.UUID, str, date], dict] = {}
    upserted = 0

    def fail(line: int, message: str) -> None:
        if len(errors) < MAX_ERRORS:
            errors.append(_error(line, message))

    for line, row in rows:
        raw_content_id = (row.get("content_id") or "").strip()
        try:
            content_id = uuid.UUID(raw_content_id)
        except (ValueError, TypeError):
            fail(line, "内容不存在或不属于当前租户")
            continue
        content = contents.get(content_id)
        if content is None:
            fail(line, "内容不存在或不属于当前租户")
            continue

        parsed = _parse_row(row, content)
        if isinstance(parsed, str):
            fail(line, parsed)
            continue
        captured_at, platform, metrics = parsed

        pending[(content.id, platform, captured_at)] = {
            "tenant_id": user.tenant_id,
            "content_id": content.id,
            "platform": platform,
            "captured_at": captured_at,
            **metrics,
            "raw": _raw_row(row),
        }
        upserted += 1

    pending_rows = list(pending.values())
    try:
        for start in range(0, len(pending_rows), UPSERT_CHUNK):
            chunk = pending_rows[start : start + UPSERT_CHUNK]
            await session.execute(_upsert_statement(session, chunk))
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise AppError("import_conflict", "数据导入冲突，请重试", 409) from None
    except (OverflowError, DataError):
        await session.rollback()
        raise AppError("invalid_data", "数据超出允许范围", 422) from None

    return upserted, errors


async def latest_snapshots(
    session: AsyncSession,
    tenant_id: str,
    content_ids: list[uuid.UUID],
) -> dict[uuid.UUID, AnalyticsSnapshot]:
    """按内容取 captured_at 最新（同日按 id 兜底）的一条快照。"""
    if not content_ids:
        return {}

    ranked = (
        select(
            AnalyticsSnapshot.id.label("id"),
            func.row_number()
            .over(
                partition_by=AnalyticsSnapshot.content_id,
                order_by=(
                    AnalyticsSnapshot.captured_at.desc(),
                    AnalyticsSnapshot.id.desc(),
                ),
            )
            .label("rn"),
        )
        .where(
            AnalyticsSnapshot.tenant_id == tenant_id,
            AnalyticsSnapshot.content_id.in_(content_ids),
        )
        .subquery()
    )
    snapshots = (
        await session.execute(
            select(AnalyticsSnapshot)
            .join(ranked, AnalyticsSnapshot.id == ranked.c.id)
            .where(ranked.c.rn == 1)
        )
    ).scalars().all()
    return {snapshot.content_id: snapshot for snapshot in snapshots}
