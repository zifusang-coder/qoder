from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.errors import AppError
from app.workflow.schedule import Cadence, next_run_at, parse_cadence

SHANGHAI = "Asia/Shanghai"


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def daily(hour: int = 9, minute: int = 0, zone: str = SHANGHAI) -> Cadence:
    return parse_cadence(
        {"kind": "daily", "time": f"{hour:02d}:{minute:02d}", "timezone": zone}
    )


def weekly(weekdays, hour: int = 9, zone: str = SHANGHAI) -> Cadence:
    return parse_cadence(
        {
            "kind": "weekly",
            "weekdays": list(weekdays),
            "time": f"{hour:02d}:00",
            "timezone": zone,
        }
    )


def test_parse_daily_ignores_weekdays():
    cadence = parse_cadence(
        {
            "kind": "daily",
            "time": "09:30",
            "timezone": SHANGHAI,
            "weekdays": [3],
        }
    )
    assert cadence == Cadence(
        kind="daily", weekdays=(), hour=9, minute=30, timezone=SHANGHAI
    )
    assert cadence.to_json() == {
        "kind": "daily",
        "weekdays": [],
        "time": "09:30",
        "timezone": SHANGHAI,
    }


def test_parse_weekly_sorts_and_dedupes_weekdays():
    cadence = parse_cadence(
        {
            "kind": "weekly",
            "time": "18:05",
            "timezone": SHANGHAI,
            "weekdays": [5, 1, 5],
        }
    )
    assert cadence.weekdays == (1, 5)
    assert cadence.to_json()["weekdays"] == [1, 5]
    assert cadence.to_json()["time"] == "18:05"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "daily",
        {},
        {"kind": "hourly", "time": "09:00", "timezone": SHANGHAI},
        {"kind": [], "time": "09:00", "timezone": SHANGHAI},
        {"kind": {}, "time": "09:00", "timezone": SHANGHAI},
        {"kind": "daily", "time": "9:00", "timezone": SHANGHAI},
        {"kind": "daily", "time": "24:00", "timezone": SHANGHAI},
        {"kind": "daily", "time": "09:60", "timezone": SHANGHAI},
        {"kind": "daily", "time": "09:00\n", "timezone": SHANGHAI},
        {"kind": "daily", "time": "09:00"},
        {"kind": "daily", "time": "09:00", "timezone": "Mars/Olympus"},
        {"kind": "weekly", "time": "09:00", "timezone": SHANGHAI},
        {"kind": "weekly", "time": "09:00", "timezone": SHANGHAI, "weekdays": []},
        {
            "kind": "weekly",
            "time": "09:00",
            "timezone": SHANGHAI,
            "weekdays": [0, 8],
        },
        {
            "kind": "weekly",
            "time": "09:00",
            "timezone": SHANGHAI,
            "weekdays": [True],
        },
    ],
)
def test_parse_rejects_invalid_cadence(raw):
    with pytest.raises(AppError) as excinfo:
        parse_cadence(raw)
    assert excinfo.value.code == "workflow_schedule_invalid"
    assert excinfo.value.status == 422


def test_daily_same_day_when_time_not_passed():
    # 2026-11-16 00:30 UTC = 上海 08:30
    assert next_run_at(daily(), utc(2026, 11, 16, 0, 30)) == utc(2026, 11, 16, 1, 0)


def test_daily_rolls_to_next_day_when_time_reached():
    # 恰好到点也取下一次，保证严格大于 after
    assert next_run_at(daily(), utc(2026, 11, 16, 1, 0)) == utc(2026, 11, 17, 1, 0)


def test_daily_crosses_month_boundary():
    # 2026-01-31 10:00 上海 → 2026-02-01 09:00 上海
    assert next_run_at(daily(), utc(2026, 1, 31, 2, 0)) == utc(2026, 2, 1, 1, 0)


def test_weekly_picks_next_matching_weekday():
    cadence = weekly([1, 4])  # 周一、周四
    assert next_run_at(cadence, utc(2026, 11, 16, 0, 0)) == utc(2026, 11, 16, 1, 0)
    assert next_run_at(cadence, utc(2026, 11, 16, 2, 0)) == utc(2026, 11, 19, 1, 0)


def test_weekly_wraps_to_next_week():
    cadence = weekly([1])
    assert next_run_at(cadence, utc(2026, 11, 16, 2, 0)) == utc(2026, 11, 23, 1, 0)


def test_next_run_at_accepts_after_in_other_timezone():
    after = datetime(2026, 11, 16, 8, 30, tzinfo=ZoneInfo(SHANGHAI))
    assert next_run_at(daily(), after) == utc(2026, 11, 16, 1, 0)


def test_next_run_at_follows_dst_offset_change():
    cadence = daily(zone="America/New_York")
    # 2026-03-08 美国进入夏令时：09:00 从 UTC-5 变成 UTC-4
    assert next_run_at(cadence, utc(2026, 3, 7, 6, 0)) == utc(2026, 3, 7, 14, 0)
    assert next_run_at(cadence, utc(2026, 3, 7, 15, 0)) == utc(2026, 3, 8, 13, 0)


def test_dst_overlap_after_second_pass_advances_to_next_day():
    cadence = daily(hour=1, minute=30, zone="America/New_York")
    # 2026-11-01 01:30 纽约出现两次；06:15Z 已是第二遍（01:15 EST），
    # 第一遍 01:30 EDT = 05:30Z 已过去，必须取次日 06:30Z 而不是回到 05:30Z
    assert next_run_at(cadence, utc(2026, 11, 1, 6, 15)) == utc(2026, 11, 2, 6, 30)


def test_dst_overlap_takes_first_pass_when_after_is_before_it():
    cadence = daily(hour=1, minute=30, zone="America/New_York")
    # 04:00Z 早于第一遍 01:30 EDT（05:30Z），重叠取第一次
    assert next_run_at(cadence, utc(2026, 11, 1, 4, 0)) == utc(2026, 11, 1, 5, 30)


def test_dst_gap_rolls_forward_past_wall_time():
    cadence = daily(hour=2, minute=30, zone="America/New_York")
    # 2026-03-08 02:30 不存在（02:00 EST → 03:00 EDT）；
    # 07:00Z = 03:00 EDT，缺口顺延到 03:30 EDT（07:30Z），不跳到次日
    assert next_run_at(cadence, utc(2026, 3, 8, 7, 0)) == utc(2026, 3, 8, 7, 30)


@pytest.mark.parametrize(
    "cadence",
    [
        Cadence(kind="daily", weekdays=(), hour=25, minute=0, timezone=SHANGHAI),
        Cadence(kind="daily", weekdays=(), hour=9, minute=60, timezone=SHANGHAI),
        Cadence(kind="daily", weekdays=(), hour=9, minute=0, timezone="Mars/Olympus"),
    ],
)
def test_next_run_at_rejects_invalid_hand_built_cadence(cadence):
    with pytest.raises(AppError) as excinfo:
        next_run_at(cadence, utc(2026, 11, 16, 0, 30))
    assert excinfo.value.code == "workflow_schedule_invalid"
    assert excinfo.value.status == 422


def test_next_run_at_requires_aware_after():
    with pytest.raises(AppError) as excinfo:
        next_run_at(daily(), datetime(2026, 11, 16, 0, 30))
    assert excinfo.value.code == "workflow_schedule_invalid"
