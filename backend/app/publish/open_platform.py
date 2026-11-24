import asyncio
import time
from typing import Any

import httpx

from app.logging_setup import configure_http_logging
from app.models.publish import PlatformAccount
from app.publish.base import (
    PublishConfigError,
    PublishContent,
    PublishError,
    PublishMedia,
    PublishResult,
)
from app.publish.credentials import credential_with_tokens
from app.storage import StorageBackend, StorageError

configure_http_logging()

TIMEOUT = 30.0
MAX_TEXT = 2000
MAX_TITLE = 30
REQUIRED_FIELDS = (
    "client_key",
    "client_secret",
    "open_id",
    "refresh_token",
)


class OpenPlatformVideoAdapter:
    platform = ""
    label = ""
    capability = "auto"
    REFRESH_URL = "https://open.douyin.com/oauth/refresh_token/"
    UPLOAD_URL = ""
    CREATE_URL = ""

    def _sanitized(self, action: str, name: str) -> str:
        return f"{self.label}接口请求失败（{action}:{name}）"

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        action: str,
        **kwargs: Any,
    ) -> dict:
        try:
            response = await client.request(method, url, **kwargs)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            # 只暴露异常类名，绝不回显 URL、token 或请求体
            raise PublishError(self._sanitized(action, type(exc).__name__)) from None
        except ValueError:
            raise PublishError(f"{self.label}接口响应格式异常") from None
        if not isinstance(data, dict):
            raise PublishError(f"{self.label}接口响应格式异常")
        return data

    @staticmethod
    def _platform_error(data: dict) -> tuple[Any, str] | None:
        for block in (data.get("data"), data):
            if isinstance(block, dict):
                code = block.get("error_code")
                if code not in (None, 0):
                    message = (
                        block.get("description")
                        or block.get("message")
                        or ""
                    )
                    return code, str(message)[:200]
        return None

    async def _refresh(
        self, client: httpx.AsyncClient, creds: dict[str, object]
    ) -> tuple[str, dict]:
        data = await self._request_json(
            client,
            "POST",
            self.REFRESH_URL,
            action="refresh",
            data={
                "client_key": creds["client_key"],
                "grant_type": "refresh_token",
                "refresh_token": creds["refresh_token"],
            },
        )
        error = self._platform_error(data)
        payload = data.get("data")
        if not isinstance(payload, dict):
            payload = {}
        token = payload.get("access_token")
        if error or not isinstance(token, str) or not token:
            if error:
                raise PublishConfigError(
                    f"{self.label}令牌刷新失败：{error[0]} {error[1]}"
                )
            raise PublishConfigError(f"{self.label}令牌刷新失败，请重新授权")
        patch: dict = {"access_token": token}
        expires_in = payload.get("expires_in")
        if isinstance(expires_in, (int, float)):
            patch["expires_at"] = int(time.time() + float(expires_in))
        for key in ("refresh_token", "open_id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                patch[key] = value
        return token, patch

    async def _access_token(
        self, client: httpx.AsyncClient, creds: dict[str, object]
    ) -> tuple[str, dict | None]:
        token = creds.get("access_token")
        expires_at = creds.get("expires_at")
        if (
            isinstance(token, str)
            and token
            and isinstance(expires_at, int)
            and expires_at > int(time.time()) + 60
        ):
            return token, None
        return await self._refresh(client, creds)

    def _check_content(self, content: PublishContent) -> None:
        if content.kind != "video":
            raise PublishConfigError(f"{self.label}仅支持视频内容")

    def _video(self, content: PublishContent) -> PublishMedia:
        for item in content.media:
            if item.kind == "video":
                return item
        raise PublishConfigError(
            f"{self.label}发布需要视频文件，请先生成短视频"
        )

    @staticmethod
    def _video_id(data: dict) -> str:
        for path in (("data", "video"), ("data",), ()):
            block: Any = data
            for key in path:
                block = block.get(key) if isinstance(block, dict) else None
            if isinstance(block, dict):
                value = block.get("video_id") or block.get("item_id")
                if isinstance(value, str) and value:
                    return value
        return ""

    def _create_payload(
        self, content: PublishContent, video_id: str
    ) -> dict:
        text = f"{content.title}\n{content.body}"[:MAX_TEXT]
        return {"video_id": video_id, "text": text}

    async def publish(
        self,
        *,
        account: PlatformAccount,
        content: PublishContent,
        storage: StorageBackend,
    ) -> PublishResult:
        self._check_content(content)
        creds = credential_with_tokens(
            account, REQUIRED_FIELDS, label=self.label
        )
        media = self._video(content)
        try:
            blob = await asyncio.to_thread(
                storage.read_bytes, content.project_id, media.path
            )
        except (StorageError, OSError):
            raise PublishConfigError(
                f"{self.label}视频文件不可用，请重新生成"
            ) from None

        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            token, patch = await self._access_token(client, creds)
            params = {
                "open_id": creds["open_id"],
                "access_token": token,
            }
            uploaded = await self._request_json(
                client,
                "POST",
                self.UPLOAD_URL,
                action="upload",
                params=params,
                files={"video": (media.name, blob)},
            )
            error = self._platform_error(uploaded)
            if error:
                raise PublishError(
                    f"{self.label}视频上传失败：{error[0]} {error[1]}"
                )
            video_id = self._video_id(uploaded)
            if not video_id:
                raise PublishError(
                    f"{self.label}视频上传失败：缺少 video_id"
                )
            created = await self._request_json(
                client,
                "POST",
                self.CREATE_URL,
                action="create",
                params=params,
                json=self._create_payload(content, video_id),
            )
            error = self._platform_error(created)
            if error:
                raise PublishError(
                    f"{self.label}视频发布失败：{error[0]} {error[1]}"
                )

        return PublishResult(
            external_id=video_id,
            raw=created,
            credential_patch=patch,
        )
