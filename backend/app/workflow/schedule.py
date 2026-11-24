"""cadence 解析与下次触发时刻计算。

只支持「每日/每周 + 时分 + 时区」，用标准库 zoneinfo，不引入 cron 依赖。
所有计算结果是 UTC aware datetime，方便直接与数据库列比较。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.errors import AppError

TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])\Z")
KINDS = frozenset({"daily", "weekly"})
# weekly 一定能在 7 天内命中；多留一天只在异常输入下兜底
MAX_DAYS_AHEAD = 8


@dataclass(frozen=True)
class Cadence:
    """kind 为 daily 时 weekdays 恒为空元组。"""

    kind: str
    weekdays: tuple[int, ...]
    hour: int
    minute: int
    timezone: str

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "weekdays": list(self.weekdays),
            "time": f"{self.hour:02d}:{self.minute:02d}",
            "timezone": self.timezone,
        }


def _invalid(message: str) -> AppError:
    return AppError("workflow_schedule_invalid", message, 422)


def parse_cadence(raw: object) -> Cadence:
    if not isinstance(raw, dict):
        raise _invalid("cadence 必须是对象")
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        raise _invalid("kind 只能是 daily 或 weekly")
    match = TIME_RE.match(str(raw.get("time") or ""))
    if match is None:
        raise _invalid("time 必须形如 09:00")
    hour, minute = int(match.group(1)), int(match.group(2))
    timezone = raw.get("timezone")
    if not isinstance(timezone, str) or not timezone.strip():
        raise _invalid("timezone 不能为空")
    timezone = timezone.strip()
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise _invalid(f"未知时区：{timezone}") from None

    weekdays: tuple[int, ...] = ()
    if kind == "weekly":
        values = raw.get("weekdays")
        if not isinstance(values, (list, tuple)) or not values:
            raise _invalid("weekly 需要非空的 weekdays")
        for value in values:
            # bool 是 int 的子类，必须显式排除，否则 True 会被当成周一
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= 7
            ):
                raise _invalid("weekdays 只能取 1-7 的整数（周一为 1）")
        weekdays = tuple(sorted(set(values)))

    return Cadence(
        kind=kind, weekdays=weekdays, hour=hour, minute=minute, timezone=timezone
    )


def next_run_at(cadence: Cadence, after: datetime) -> datetime:
    """返回严格晚于 after 的第一个触发时刻（UTC）。

    夏令时缺口/重叠按 zoneinfo 默认 fold=0 归一：缺口顺延到表盘时间之后，
    重叠取第一次出现，和「每天 09:00」的直觉一致。
    """
    if after.tzinfo is None:
        raise _invalid("after 必须是带时区的时间")
    try:
        zone = ZoneInfo(cadence.timezone)
        trigger = time(cadence.hour, cadence.minute)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise _invalid("cadence 的时区或时分非法") from None
    local = after.astimezone(zone)
    start = local.date()
    for offset in range(MAX_DAYS_AHEAD):
        current: date = start + timedelta(days=offset)
        if cadence.kind == "weekly" and current.isoweekday() not in cadence.weekdays:
            continue
        candidate = datetime.combine(current, trigger, tzinfo=zone)
        # 必须比较时间轴上的瞬间：local 与 candidate 共用同一个 ZoneInfo，
        # 直接比较表盘时间会忽略 fold/偏移（重叠、缺口都会算错）
        candidate_utc = candidate.astimezone(UTC)
        if candidate_utc > after:
            return candidate_utc
    raise _invalid("无法计算下次运行时间")
