import pytest

from app.publish.registry import (
    AUTO_PUBLISH_PAIRS,
    AUTO_PUBLISH_PLATFORMS,
    CAP_AUTO,
    CAP_MANUAL,
    CAP_PENDING,
    MANUAL_EXPORT_PLATFORMS,
    PLATFORM_ORDER,
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
