from datetime import UTC, datetime

import pytest

from app.billing.dimensions import (
    DIMENSION_KEYS,
    DIMENSION_MAP,
    priced_non_billable,
    unknown_dimensions,
)
from app.billing.periods import (
    normalize_period,
    period_bounds,
    period_finished,
)
from app.billing.pricing import build_charges, build_usage_rows, charge_cents


def test_registry_exposes_expected_dimensions():
    assert DIMENSION_KEYS == (
        "llm.prompt_tokens",
        "llm.completion_tokens",
        "run.skill",
        "run.agent",
        "run.workflow",
        "media.jobs",
        "media.seconds",
    )
    assert DIMENSION_MAP["llm.prompt_tokens"].billable is True
    assert DIMENSION_MAP["llm.prompt_tokens"].unit_size == 1000
    assert DIMENSION_MAP["media.seconds"].billable is False


def test_unknown_and_non_billable_keys_are_reported():
    assert unknown_dimensions({"llm.prompt_tokens": 1, "x.y": 2}) == ["x.y"]
    assert priced_non_billable({"llm.completion_tokens": 1}) == []
    assert priced_non_billable({"run.skill": 1, "media.jobs": 2}) == [
        "media.jobs",
        "run.skill",
    ]


def test_charge_rounds_up_to_full_cent():
    assert charge_cents(1000, 1000, 2, 1000) == 0
    assert charge_cents(1500, 1000, 2, 1000) == 1
    assert charge_cents(1999, 1000, 2, 1000) == 2
    assert charge_cents(2000, 1000, 2, 1000) == 2
    assert charge_cents(5000, 0, 0, 1000) == 0


def test_build_charges_keeps_dimension_order_and_skips_zero():
    usage = {"llm.completion_tokens": 1200, "llm.prompt_tokens": 1500}
    limits = {"llm.prompt_tokens": 1000, "llm.completion_tokens": 1200}
    prices = {"llm.prompt_tokens": 1, "llm.completion_tokens": 3}
    charges = build_charges(usage, limits, prices)
    assert charges == [
        {
            "dimension": "llm.prompt_tokens",
            "used": 1500,
            "included": 1000,
            "overage": 500,
            "unit_size": 1000,
            "price_cents": 1,
            "amount_cents": 1,
        }
    ]


def test_build_usage_rows_marks_over_limit_and_unlimited():
    rows = build_usage_rows(
        {"llm.prompt_tokens": 1500, "run.skill": 3},
        {"llm.prompt_tokens": 1000, "run.skill": 0},
        {"llm.prompt_tokens": 1},
    )
    by_key = {row["dimension"]: row for row in rows}
    prompt = by_key["llm.prompt_tokens"]
    assert prompt["percent"] == 150
    assert prompt["over_limit"] is True
    assert prompt["remaining"] == 0
    assert prompt["amount_cents"] == 1
    skills = by_key["run.skill"]
    assert skills["percent"] == 100
    assert skills["over_limit"] is True
    assert by_key["media.jobs"]["percent"] is None
    assert by_key["media.jobs"]["over_limit"] is False


def test_period_bounds_use_beijing_month():
    start, end = period_bounds("2026-11")
    assert start == datetime(2026, 10, 31, 16, tzinfo=UTC)
    assert end == datetime(2026, 11, 30, 16, tzinfo=UTC)
    dec_start, dec_end = period_bounds("2026-12")
    assert dec_start == datetime(2026, 11, 30, 16, tzinfo=UTC)
    assert dec_end == datetime(2026, 12, 31, 16, tzinfo=UTC)


def test_normalize_period_uses_beijing_time_and_rejects_bad_input():
    moment = datetime(2026, 11, 30, 17, tzinfo=UTC)
    assert normalize_period(None, now=moment) == "2026-12"
    assert normalize_period("2026-11") == "2026-11"
    with pytest.raises(ValueError):
        normalize_period("2026-13")
    with pytest.raises(ValueError):
        normalize_period("202611")


def test_period_finished_compares_against_beijing_end():
    assert period_finished("2026-10", now=datetime(2026, 11, 1, tzinfo=UTC))
    assert not period_finished("2026-11", now=datetime(2026, 11, 30, 15, 59, tzinfo=UTC))
    assert period_finished("2026-11", now=datetime(2026, 11, 30, 16, tzinfo=UTC))
