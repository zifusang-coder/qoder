from app.publish.base import PublishConfigError, PublishContent
from app.publish.open_platform import (
    MAX_TEXT,
    MAX_TITLE,
    OpenPlatformVideoAdapter,
)


class ToutiaoAdapter(OpenPlatformVideoAdapter):
    platform = "toutiao"
    label = "头条"
    capability = "auto"
    UPLOAD_URL = (
        "https://open.douyin.com/api/toutiao/v1/video/upload_video/"
    )
    CREATE_URL = (
        "https://open.douyin.com/api/toutiao/v1/video/create_video/"
    )

    def _check_content(self, content: PublishContent) -> None:
        if content.kind == "article":
            raise PublishConfigError(
                "头条图文发布接口已下线或未获批，请提供官方接口文档后再启用"
            )
        super()._check_content(content)

    def _create_payload(
        self, content: PublishContent, video_id: str
    ) -> dict:
        return {
            "video_id": video_id,
            "title": content.title.strip()[:MAX_TITLE] or "未命名",
            "text": f"{content.title}\n{content.body}"[:MAX_TEXT],
        }


# 模块级常量直接取自适配器，避免测试里的路由与实现漂移
REFRESH_URL = ToutiaoAdapter.REFRESH_URL
UPLOAD_URL = ToutiaoAdapter.UPLOAD_URL
CREATE_URL = ToutiaoAdapter.CREATE_URL
