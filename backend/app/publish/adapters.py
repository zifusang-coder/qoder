from app.publish.baijiahao import BaijiahaoAdapter
from app.publish.base import PublishConfigError, PublisherAdapter
from app.publish.douyin import DouyinAdapter
from app.publish.toutiao import ToutiaoAdapter
from app.publish.wechat import WeChatMPAdapter

ADAPTERS: dict[str, type] = {
    "wechat": WeChatMPAdapter,
    "douyin": DouyinAdapter,
    "toutiao": ToutiaoAdapter,
    "baijiahao": BaijiahaoAdapter,
}


def adapter_for(platform: str) -> PublisherAdapter:
    factory = ADAPTERS.get(platform)
    if factory is None:
        raise PublishConfigError(f"{platform} 暂不支持自动发布")
    return factory()
