from app.models.publish import PlatformAccount
from app.publish.base import (
    PublishConfigError,
    PublishContent,
    PublishResult,
)
from app.publish.credentials import load_credential
from app.storage import StorageBackend


class BaijiahaoAdapter:
    platform = "baijiahao"
    capability = "pending"

    async def publish(
        self,
        *,
        account: PlatformAccount,
        content: PublishContent,
        storage: StorageBackend,
    ) -> PublishResult:
        load_credential(account, ("app_id", "app_token"), label="百家号")
        raise PublishConfigError(
            "百家号官方发布接口尚未接入，请提供已获批的接口文档"
        )
