import json

import httpx
import pytest
import respx

from app.models.publish import PlatformAccount
from app.publish.base import PublishConfigError, PublishContent, PublishMedia
from app.publish.toutiao import (
    CREATE_URL,
    REFRESH_URL,
    UPLOAD_URL,
    ToutiaoAdapter,
)
from app.security import encrypt_secret

CREDS = {
    "client_key": "ck-2",
    "client_secret": "cs-2",
    "open_id": "open-2",
    "refresh_token": "rt-2",
}


class FakeStorage:
    def read_bytes(self, project_id, relpath):
        return b"video-bytes"


def _account():
    return PlatformAccount(
        tenant_id="t",
        platform="toutiao",
        name="头条号",
        credential_enc=encrypt_secret(json.dumps(CREDS)),
        status="active",
        meta={"author": "作者"},
    )


def _content(kind="video"):
    return PublishContent(
        project_id="p1",
        title="头条标题",
        body="头条正文",
        kind=kind,
        media=(PublishMedia("video.mp4", "video", "media/video.mp4"),),
        cover_path="media/cover.png",
    )


async def test_article_returns_explicit_pending_error():
    with pytest.raises(
        PublishConfigError,
        match="头条图文发布接口已下线或未获批",
    ):
        await ToutiaoAdapter().publish(
            account=_account(),
            content=_content("article"),
            storage=FakeStorage(),
        )


@respx.mock
async def test_video_success():
    respx.post(REFRESH_URL).mock(
        return_value=httpx.Response(
            200, json={"data": {"access_token": "tok-2"}}
        )
    )
    respx.post(UPLOAD_URL).mock(
        return_value=httpx.Response(
            200, json={"data": {"error_code": 0, "video_id": "vid-3"}}
        )
    )
    create = respx.post(CREATE_URL).mock(
        return_value=httpx.Response(200, json={"data": {"error_code": 0}})
    )

    result = await ToutiaoAdapter().publish(
        account=_account(),
        content=_content(),
        storage=FakeStorage(),
    )

    assert result.external_id == "vid-3"
    payload = json.loads(create.calls[0].request.content)
    assert payload["video_id"] == "vid-3"
    assert payload["title"] == "头条标题"
