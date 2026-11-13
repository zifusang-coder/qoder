from app.publish.base import PublishContent
from app.publish.open_platform import MAX_TEXT, OpenPlatformVideoAdapter


class DouyinAdapter(OpenPlatformVideoAdapter):
    platform = "douyin"
    label = "抖音"
    capability = "auto"
    UPLOAD_URL = "https://open.douyin.com/api/douyin/v1/video/upload_video/"
    CREATE_URL = "https://open.douyin.com/api/douyin/v1/video/create_video/"

    def _create_payload(
        self, content: PublishContent, video_id: str
    ) -> dict:
        return {
            "video_id": video_id,
            "text": f"{content.title}\n{content.body}"[:MAX_TEXT],
        }


# 模块级常量直接取自适配器，避免测试里的路由与实现漂移
REFRESH_URL = DouyinAdapter.REFRESH_URL
UPLOAD_URL = DouyinAdapter.UPLOAD_URL
CREATE_URL = DouyinAdapter.CREATE_URL
