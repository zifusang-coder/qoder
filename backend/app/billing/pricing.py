from collections.abc import Mapping

from app.billing.dimensions import DIMENSIONS


def charge_cents(
    used: int, included: int, price_cents: int, unit_size: int
) -> int:
    overage = max(0, used - included)
    if overage == 0 or price_cents <= 0 or unit_size <= 0:
        return 0
    return (overage * price_cents + unit_size - 1) // unit_size


def build_charges(
    usage: Mapping[str, int],
    limits: Mapping[str, int],
    prices: Mapping[str, int],
) -> list[dict]:
    charges: list[dict] = []
    for dimension in DIMENSIONS:
        price = int(prices.get(dimension.key, 0) or 0)
        if not dimension.billable or price <= 0:
            continue
        used = int(usage.get(dimension.key, 0) or 0)
        included = int(limits.get(dimension.key, 0) or 0)
        amount = charge_cents(used, included, price, dimension.unit_size)
        if amount == 0:
            continue
        charges.append(
            {
                "dimension": dimension.key,
                "used": used,
                "included": included,
                "overage": max(0, used - included),
                "unit_size": dimension.unit_size,
                "price_cents": price,
                "amount_cents": amount,
            }
        )
    return charges


def _percent(used: int, limit: int) -> int:
    if limit <= 0:
        return 100 if used > 0 else 0
    return round(used * 100 / limit)


def build_usage_rows(
    usage: Mapping[str, int],
    limits: Mapping[str, int],
    prices: Mapping[str, int],
) -> list[dict]:
    rows: list[dict] = []
    for dimension in DIMENSIONS:
        used = int(usage.get(dimension.key, 0) or 0)
        limit = limits.get(dimension.key)
        included = int(limit or 0)
        price = int(prices.get(dimension.key, 0) or 0)
        rows.append(
            {
                "dimension": dimension.key,
                "label": dimension.label,
                "unit": dimension.unit,
                "unit_size": dimension.unit_size,
                "used": used,
                "limit": None if limit is None else included,
                "remaining": None if limit is None else max(0, included - used),
                "percent": None if limit is None else _percent(used, included),
                "over_limit": limit is not None and used > included,
                "price_cents": price,
                "amount_cents": charge_cents(
                    used, included, price, dimension.unit_size
                )
                if dimension.billable
                else 0,
            }
        )
    return rows
