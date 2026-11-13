"""Single source of truth for supported publishing platforms.

Platform insertion order is the UI order. Capabilities are keyed by content
kind; an unknown kind resolves to ``pending``. ``capability_for`` is the
authoritative gate for whether a platform may handle a given content kind,
while the platform-level sets (``AUTO_PUBLISH_PLATFORMS`` /
``MANUAL_EXPORT_PLATFORMS``) are coarse filters for listing and navigation.
The canonical table is read-only, and the derived sets are import-time
snapshots of it: editing the registry at runtime cannot desynchronize them.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

Capability = Literal["auto_publish", "manual_export", "pending"]

CAP_AUTO: Capability = "auto_publish"
CAP_MANUAL: Capability = "manual_export"
CAP_PENDING: Capability = "pending"

CONTENT_KIND_LABELS = {
    "article": "图文",
    "note": "笔记",
    "video": "视频",
    "audio": "音频",
}


def content_kind_label(kind: str) -> str:
    return CONTENT_KIND_LABELS.get(kind, kind)


@dataclass(frozen=True)
class CredentialField:
    key: str
    label: str
    secret: bool = False
    required: bool = True
    placeholder: str = ""


@dataclass(frozen=True)
class PlatformSpec:
    key: str
    label: str
    content_capabilities: Mapping[str, Capability]
    credential_fields: tuple[CredentialField, ...] = ()
    meta_fields: tuple[str, ...] = ()
    note: str = ""


_SPECS: dict[str, PlatformSpec] = {
    "wechat": PlatformSpec(
        key="wechat",
        label="公众号",
        content_capabilities=MappingProxyType({"article": CAP_AUTO}),
        credential_fields=(
            CredentialField("app_id", "AppID", placeholder="公众号 AppID"),
            CredentialField("app_secret", "AppSecret", secret=True),
        ),
        meta_fields=(
            "author",
            "thumb_media_id",
            "need_open_comment",
            "only_fans_can_comment",
        ),
        note="使用公众号草稿接口，发布前需配置封面素材 ID",
    ),
    "douyin": PlatformSpec(
        key="douyin",
        label="抖音",
        content_capabilities=MappingProxyType({"video": CAP_AUTO}),
        credential_fields=(
            CredentialField("client_key", "Client Key"),
            CredentialField("client_secret", "Client Secret", secret=True),
            CredentialField("open_id", "Open ID"),
            CredentialField("refresh_token", "Refresh Token", secret=True),
        ),
        note="使用抖音开放平台视频发布接口",
    ),
    "toutiao": PlatformSpec(
        key="toutiao",
        label="头条",
        content_capabilities=MappingProxyType(
            {"video": CAP_AUTO, "article": CAP_PENDING}
        ),
        credential_fields=(
            CredentialField("client_key", "Client Key"),
            CredentialField("client_secret", "Client Secret", secret=True),
            CredentialField("open_id", "Open ID"),
            CredentialField("refresh_token", "Refresh Token", secret=True),
        ),
        meta_fields=("author",),
        note="视频走开放平台接口；图文接口已下线或未获批",
    ),
    "xiaohongshu": PlatformSpec(
        key="xiaohongshu",
        label="小红书",
        content_capabilities=MappingProxyType(
            {"note": CAP_MANUAL, "article": CAP_MANUAL}
        ),
        note="导出素材包后手动发布",
    ),
    "zhihu": PlatformSpec(
        key="zhihu",
        label="知乎",
        content_capabilities=MappingProxyType({"article": CAP_MANUAL}),
        note="导出素材包后手动发布",
    ),
    "kuaishou": PlatformSpec(
        key="kuaishou",
        label="快手",
        content_capabilities=MappingProxyType(
            {"video": CAP_MANUAL, "note": CAP_MANUAL}
        ),
        note="导出素材包后手动发布",
    ),
    "channels": PlatformSpec(
        key="channels",
        label="视频号",
        content_capabilities=MappingProxyType(
            {"video": CAP_MANUAL, "article": CAP_MANUAL}
        ),
        note="导出素材包后手动发布",
    ),
    "baijiahao": PlatformSpec(
        key="baijiahao",
        label="百家号",
        content_capabilities=MappingProxyType({"article": CAP_PENDING}),
        credential_fields=(
            CredentialField("app_id", "AppID"),
            CredentialField("app_token", "App Token", secret=True),
        ),
        note="官方发布接口尚未接入，可先保存凭据",
    ),
    "bilibili": PlatformSpec(
        key="bilibili",
        label="B站",
        content_capabilities=MappingProxyType({"video": CAP_PENDING}),
        note="暂无官方发布接口，可先保存账号",
    ),
}

SPECS: Mapping[str, PlatformSpec] = MappingProxyType(_SPECS)

for _key, _spec in SPECS.items():
    if _spec.key != _key:
        raise RuntimeError(f"registry key mismatch: {_key} != {_spec.key}")
del _key, _spec

PLATFORM_ORDER = tuple(SPECS)
PLATFORM_KEYS = frozenset(SPECS)
AUTO_PUBLISH_PAIRS = tuple(
    sorted(
        (spec.key, kind)
        for spec in SPECS.values()
        for kind, capability in spec.content_capabilities.items()
        if capability == CAP_AUTO
    )
)
AUTO_PUBLISH_PLATFORMS = frozenset(
    platform for platform, _kind in AUTO_PUBLISH_PAIRS
)
MANUAL_EXPORT_PLATFORMS = frozenset(
    spec.key
    for spec in SPECS.values()
    if any(value == CAP_MANUAL for value in spec.content_capabilities.values())
)


def spec_for(platform: str) -> PlatformSpec:
    return SPECS[platform]


def capability_for(platform: str, content_kind: str) -> Capability:
    return SPECS[platform].content_capabilities.get(content_kind, CAP_PENDING)


def headline_capability(spec: PlatformSpec) -> Capability:
    values = set(spec.content_capabilities.values())
    if CAP_AUTO in values:
        return CAP_AUTO
    if CAP_MANUAL in values:
        return CAP_MANUAL
    return CAP_PENDING
