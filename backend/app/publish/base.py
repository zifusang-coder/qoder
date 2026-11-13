from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from app.models.publish import PlatformAccount
    from app.storage import StorageBackend

PLATFORMS = (
    "wechat",
    "xiaohongshu",
    "douyin",
    "toutiao",
    "baijiahao",
    "zhihu",
    "kuaishou",
    "channels",
    "bilibili",
)
Platform = Literal[
    "wechat",
    "xiaohongshu",
    "douyin",
    "toutiao",
    "baijiahao",
    "zhihu",
    "kuaishou",
    "channels",
    "bilibili",
]


class PublishError(RuntimeError):
    """发布适配器可安全展示给调用方的错误。"""


class PublishConfigError(PublishError):
    """账号配置缺失或无效，用户可自行修复，不应消耗发布尝试次数。"""


@dataclass(frozen=True)
class PublishMedia:
    name: str
    kind: str
    path: str


@dataclass(frozen=True)
class PublishContent:
    project_id: str
    title: str
    body: str
    kind: str
    media: tuple[PublishMedia, ...] = ()
    cover_path: str | None = None


@dataclass
class PublishResult:
    external_id: str
    url: str = ""
    raw: dict = field(default_factory=dict)
    credential_patch: dict | None = None


class PublisherAdapter(Protocol):
    platform: str
    capability: str

    async def publish(
        self,
        *,
        account: "PlatformAccount",
        content: PublishContent,
        storage: "StorageBackend",
    ) -> PublishResult: ...
