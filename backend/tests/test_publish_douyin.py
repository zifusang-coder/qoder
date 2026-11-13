import json
import time

import httpx
import pytest
import respx

from app.models.publish import PlatformAccount
from app.publish.base import (
    PublishConfigError,
    PublishContent,
    PublishError,
    PublishMedia,
)
from app.publish.douyin import CREATE_URL, REFRESH_URL, UPLOAD_URL, DouyinAdapter
from app.security import encrypt_secret

CREDS = {
    "client_key": "ck-1",
    "client_secret": "cs-1",
    "open_id": "open-1",
    "refresh_token": "rt-1",
}


class FakeStorage:
    def __init__(self, *, exists=True):
        self.exists = exists
        self.calls = []

    def read_bytes(self, project_id, relpath):
        self.calls.append((project_id, relpath))
        if not self.exists:
            raise FileNotFoundError(relpath)
        return b"video-bytes"


def _account(creds=None):
    return PlatformAccount(
        tenant_id="t",
        platform="douyin",
        name="抖音号",
        credential_enc=encrypt_secret(json.dumps(creds or CREDS)),
        status="active",
        meta={},
    )


def _content(*, kind="video", media=True):
    return PublishContent(
        project_id="p1",
        title="标题",
        body="正文",
        kind=kind,
        media=(
            (PublishMedia("video.mp4", "video", "media/video.mp4"),)
            if media
            else ()
        ),
    )


@respx.mock
async def test_missing_video_raises_without_http():
    upload = respx.post(UPLOAD_URL).mock(
        return_value=httpx.Response(200, json={})
    )

    with pytest.raises(PublishConfigError, match="需要视频文件"):
        await DouyinAdapter().publish(
            account=_account(),
            content=_content(media=False),
            storage=FakeStorage(),
        )

    assert not upload.called


@respx.mock
async def test_upload_and_create_success():
    refresh = respx.post(REFRESH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "access_token": "tok-1",
                    "expires_in": 3600,
                    "refresh_token": "rt-2",
                }
            },
        )
    )
    upload = respx.post(UPLOAD_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": {"error_code": 0, "video": {"video_id": "vid-1"}}},
        )
    )
    create = respx.post(CREATE_URL).mock(
        return_value=httpx.Response(
            200, json={"data": {"error_code": 0, "item_id": "item-1"}}
        )
    )

    result = await DouyinAdapter().publish(
        account=_account(),
        content=_content(),
        storage=FakeStorage(),
    )

    assert result.external_id == "vid-1"
    assert result.credential_patch["access_token"] == "tok-1"
    assert result.credential_patch["refresh_token"] == "rt-2"
    assert refresh.call_count == 1
    assert upload.calls[0].request.headers["content-type"].startswith(
        "multipart/form-data"
    )
    payload = json.loads(create.calls[0].request.content)
    assert payload["video_id"] == "vid-1"
    assert payload["text"] == "标题\n正文"


@respx.mock
async def test_expired_token_refreshes_and_reuses_valid_token():
    refresh = respx.post(REFRESH_URL).mock(
        return_value=httpx.Response(
            200, json={"data": {"access_token": "tok-new"}}
        )
    )
    respx.post(UPLOAD_URL).mock(
        return_value=httpx.Response(
            200, json={"data": {"error_code": 0, "video_id": "vid-2"}}
        )
    )
    respx.post(CREATE_URL).mock(
        return_value=httpx.Response(200, json={"data": {"error_code": 0}})
    )

    expired = {
        **CREDS,
        "refresh_token": "rt-old",
        "access_token": "tok-old",
        "expires_at": int(time.time()) - 10,
    }
    await DouyinAdapter().publish(
        account=_account(expired),
        content=_content(),
        storage=FakeStorage(),
    )

    assert refresh.call_count == 1
    assert b"refresh_token=rt-old" in refresh.calls[0].request.content


@respx.mock
async def test_http_error_is_sanitized():
    respx.post(REFRESH_URL).mock(
        side_effect=httpx.ConnectError("failed https://open.douyin.com?token=rt-1")
    )

    with pytest.raises(PublishError) as excinfo:
        await DouyinAdapter().publish(
            account=_account(),
            content=_content(),
            storage=FakeStorage(),
        )

    message = str(excinfo.value)
    assert "https://" not in message
    assert "rt-1" not in message
    assert "cs-1" not in message
