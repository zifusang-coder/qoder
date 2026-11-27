from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Dimension:
    key: str
    label: str
    unit: str
    unit_size: int = 1
    billable: bool = False


DIMENSIONS: tuple[Dimension, ...] = (
    Dimension("llm.prompt_tokens", "输入 token", "token", 1000, True),
    Dimension("llm.completion_tokens", "输出 token", "token", 1000, True),
    Dimension("run.skill", "技能运行", "次"),
    Dimension("run.agent", "Agent 运行", "次"),
    Dimension("run.workflow", "工作流运行", "次"),
    Dimension("media.jobs", "媒体任务", "次"),
    Dimension("media.seconds", "媒体时长", "秒"),
)

DIMENSION_MAP: dict[str, Dimension] = {item.key: item for item in DIMENSIONS}
DIMENSION_KEYS: tuple[str, ...] = tuple(item.key for item in DIMENSIONS)


def unknown_dimensions(mapping: Mapping[str, object]) -> list[str]:
    return sorted(key for key in mapping if key not in DIMENSION_MAP)


def priced_non_billable(prices: Mapping[str, object]) -> list[str]:
    return sorted(
        key
        for key in prices
        if key in DIMENSION_MAP and not DIMENSION_MAP[key].billable
    )
