import json

import pytest

from app.models.publish import PlatformAccount
from app.publish.baijiahao import BaijiahaoAdapter
from app.publish.base import PublishConfigError, PublishContent
from app.security import encrypt_secret


class FakeStorage:
    def read_bytes(self, project_id, relpath):
        raise AssertionError("百家号不应读取媒体文件")


def _account():
    return PlatformAccount(
        tenant_id="t",
        platform="baijiahao",
        name="百家号",
        credential_enc=encrypt_secret(
            json.dumps({"app_id": "bj-1", "app_token": "bt-1"})
        ),
        status="active",
        meta={},
    )


async def test_always_returns_pending_error_without_side_effects():
    content = PublishContent(
        project_id="p1", title="标题", body="正文", kind="article"
    )

    with pytest.raises(
        PublishConfigError,
        match="百家号官方发布接口尚未接入",
    ):
        await BaijiahaoAdapter().publish(
            account=_account(), content=content, storage=FakeStorage()
        )
