from typing import get_args

import pytest

from app.publish.base import Platform
from app.publish.registry import (
    AUTO_PUBLISH_PAIRS,
    AUTO_PUBLISH_PLATFORMS,
    CAP_AUTO,
    CAP_MANUAL,
    CAP_PENDING,
    MANUAL_EXPORT_PLATFORMS,
    PLATFORM_KEYS,
    PLATFORM_ORDER,
    SPECS,
    capability_for,
    content_kind_label,
    headline_capability,
    spec_for,
)

EXPECTED = {
    "wechat": ("公众号", {"article": CAP_AUTO}, CAP_AUTO),
    "douyin": ("抖音", {"video": CAP_AUTO}, CAP_AUTO),
    "toutiao": ("头条", {"video": CAP_AUTO, "article": CAP_PENDING}, CAP_AUTO),
    "xiaohongshu": ("小红书", {"note": CAP_MANUAL, "article": CAP_MANUAL}, CAP_MANUAL),
    "zhihu": ("知乎", {"article": CAP_MANUAL}, CAP_MANUAL),
    "kuaishou": ("快手", {"video": CAP_MANUAL, "note": CAP_MANUAL}, CAP_MANUAL),
    "channels": ("视频号", {"video": CAP_MANUAL, "article": CAP_MANUAL}, CAP_MANUAL),
    "baijiahao": ("百家号", {"article": CAP_PENDING}, CAP_PENDING),
    "bilibili": ("B站", {"video": CAP_PENDING}, CAP_PENDING),
}


def test_every_platform_has_expected_capabilities():
    assert PLATFORM_ORDER == tuple(EXPECTED)
    for key, (label, capabilities, headline) in EXPECTED.items():
        spec = spec_for(key)
        assert spec.label == label
        assert spec.content_capabilities == capabilities
        assert headline_capability(spec) == headline
        assert all(
            value in {CAP_AUTO, CAP_MANUAL, CAP_PENDING}
            for value in capabilities.values()
        )


def test_auto_and_manual_sets_are_derived():
    assert AUTO_PUBLISH_PAIRS == (
        ("douyin", "video"),
        ("toutiao", "video"),
        ("wechat", "article"),
    )
    assert AUTO_PUBLISH_PLATFORMS == {"wechat", "douyin", "toutiao"}
    assert MANUAL_EXPORT_PLATFORMS == {
        "xiaohongshu",
        "zhihu",
        "kuaishou",
        "channels",
    }


def test_capability_defaults_to_pending():
    assert capability_for("toutiao", "article") == CAP_PENDING
    assert capability_for("wechat", "video") == CAP_PENDING
    with pytest.raises(KeyError):
        capability_for("unknown", "article")


def test_content_kind_labels():
    assert content_kind_label("article") == "图文"
    assert content_kind_label("video") == "视频"
    assert content_kind_label("unknown") == "unknown"


CREDENTIAL_KEYS = {
    "wechat": {"app_id", "app_secret"},
    "douyin": {"client_key", "client_secret", "open_id", "refresh_token"},
    "toutiao": {"client_key", "client_secret", "open_id", "refresh_token"},
    "baijiahao": {"app_id", "app_token"},
}

SECRET_KEYS = {
    "wechat": {"app_secret"},
    "douyin": {"client_secret", "refresh_token"},
    "toutiao": {"client_secret", "refresh_token"},
    "baijiahao": {"app_token"},
}

META_FIELDS = {
    "wechat": (
        "author",
        "thumb_media_id",
        "need_open_comment",
        "only_fans_can_comment",
    ),
    "toutiao": ("author",),
}


def credential_keys(spec):
    return {field.key for field in spec.credential_fields}


def secret_keys(spec):
    return {field.key for field in spec.credential_fields if field.secret}


def test_registry_keys_match_spec_keys():
    assert set(SPECS) == PLATFORM_KEYS
    for key, spec in SPECS.items():
        assert spec.key == key


def test_credential_field_metadata():
    for key, expected in CREDENTIAL_KEYS.items():
        spec = SPECS[key]
        assert credential_keys(spec) == expected
        assert secret_keys(spec) == SECRET_KEYS[key]

    for key, spec in SPECS.items():
        if key not in CREDENTIAL_KEYS:
            assert spec.credential_fields == ()


def test_meta_field_metadata():
    for key, expected in META_FIELDS.items():
        assert SPECS[key].meta_fields == expected

    for key, spec in SPECS.items():
        if key not in META_FIELDS:
            assert spec.meta_fields == ()


def test_platform_literal_matches_registry():
    assert set(get_args(Platform)) == PLATFORM_KEYS


def test_registry_cannot_be_mutated():
    with pytest.raises(TypeError):
        SPECS["wechat"] = SPECS["wechat"]  # type: ignore[index]

    wechat = SPECS["wechat"]
    with pytest.raises(TypeError):
        wechat.content_capabilities["video"] = CAP_AUTO  # type: ignore[index]


def test_adapter_registry_covers_auto_and_pending_platforms():
    from app.publish.adapters import ADAPTERS, adapter_for

    assert set(ADAPTERS) == {"wechat", "douyin", "toutiao", "baijiahao"}
    assert adapter_for("douyin").platform == "douyin"
    with pytest.raises(Exception, match="暂不支持自动发布"):
        adapter_for("xiaohongshu")
