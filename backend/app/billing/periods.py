import re
from datetime import UTC, datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))
_PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def normalize_period(period: str | None, *, now: datetime | None = None) -> str:
    if period is None:
        moment = (now or datetime.now(UTC)).astimezone(BEIJING)
        return f"{moment.year:04d}-{moment.month:02d}"
    if not _PERIOD_PATTERN.fullmatch(period):
        raise ValueError("period_invalid")
    # datetime 只支持 1-9999 年；0001-01 转 UTC 与 9999-12 的月末边界会越界
    year = int(period[:4])
    if year < 2 or year > 9998:
        raise ValueError("period_invalid")
    return period


def period_bounds(period: str) -> tuple[datetime, datetime]:
    normalize_period(period)
    year = int(period[:4])
    month = int(period[5:])
    start = datetime(year, month, 1, tzinfo=BEIJING)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=BEIJING)
    else:
        end = datetime(year, month + 1, 1, tzinfo=BEIJING)
    return start.astimezone(UTC), end.astimezone(UTC)


def period_finished(period: str, *, now: datetime | None = None) -> bool:
    _, end = period_bounds(period)
    return end <= (now or datetime.now(UTC))
