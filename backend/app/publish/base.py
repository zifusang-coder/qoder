from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from app.models.publish import PlatformAccount

PLATFORMS = (
    "wechat",
    "xiaohongshu",
    "douyin",
    "zhihu",
    "kuaishou",
    "channels",
    "bilibili",
)
Platform = Literal[
    "wechat", "xiaohongshu", "douyin", "zhihu", "kuaishou", "channels", "bilibili"
]


class PublishError(RuntimeError):
    """发布适配器可安全展示给调用方的错误。"""


@dataclass
class PublishResult:
    external_id: str
    url: str = ""
    raw: dict = field(default_factory=dict)


class PublisherAdapter(Protocol):
    platform: str
    capability: str

    async def publish(
        self, *, account: "PlatformAccount", title: str, body: str
    ) -> PublishResult: ...
