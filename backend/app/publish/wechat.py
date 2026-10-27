import html
import json
import re
from typing import Any

import httpx
import markdown
from cryptography.fernet import InvalidToken

from app.logging_setup import configure_http_logging
from app.models.publish import PlatformAccount
from app.publish.base import PublishConfigError, PublishError, PublishResult
from app.security import decrypt_secret

# worker 直接导入适配器时也必须屏蔽 httpx 的完整 URL 日志
configure_http_logging()

TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/stable_token"
DRAFT_URL = "https://api.weixin.qq.com/cgi-bin/draft/add"
TIMEOUT = 15.0
MAX_TITLE = 64
MAX_DIGEST = 100


def _sanitized(name: str) -> str:
    return f"公众号接口请求失败（{name}）"


class WeChatMPAdapter:
    platform = "wechat"
    capability = "auto"

    async def _post(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        payload: dict,
        params: dict | None = None,
    ) -> dict:
        try:
            response = await client.post(url, params=params, json=payload)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            # 只暴露异常类名，绝不回显 URL/请求体中的 token 或密钥
            raise PublishError(_sanitized(type(exc).__name__)) from None
        except ValueError:
            raise PublishError("公众号接口响应格式异常") from None
        if not isinstance(data, dict):
            raise PublishError("公众号接口响应格式异常")
        return data

    async def _token(
        self, client: httpx.AsyncClient, creds: dict[str, str]
    ) -> str:
        data = await self._post(
            client,
            TOKEN_URL,
            payload={
                "grant_type": "client_credential",
                "appid": creds["app_id"],
                "secret": creds["app_secret"],
                "force_refresh": False,
            },
        )
        token = data.get("access_token")
        if data.get("errcode") or not isinstance(token, str) or not token:
            errcode = data.get("errcode")
            errmsg = data.get("errmsg", "")
            raise PublishError(f"公众号 access_token 获取失败：{errcode} {errmsg}")
        return token

    def _credentials(self, account: PlatformAccount) -> dict[str, str]:
        try:
            raw = decrypt_secret(account.credential_enc)
            creds = json.loads(raw)
        except (InvalidToken, ValueError, TypeError, UnicodeError):
            raise PublishConfigError("账号凭据不可用，请重新授权") from None
        if not isinstance(creds, dict):
            raise PublishConfigError("公众号凭据不完整，请重新授权")
        app_id = creds.get("app_id")
        app_secret = creds.get("app_secret")
        if (
            not isinstance(app_id, str)
            or not app_id.strip()
            or not isinstance(app_secret, str)
            or not app_secret.strip()
        ):
            raise PublishConfigError("公众号凭据不完整，请重新授权")
        return {"app_id": app_id.strip(), "app_secret": app_secret.strip()}

    @staticmethod
    def _meta_int(meta: dict, key: str) -> int:
        value = meta.get(key, 0)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _digest(content: str) -> str:
        text = re.sub(r"<[^>]+>", "", content)
        return html.unescape(text).strip()[:MAX_DIGEST]

    def _article(
        self, account: PlatformAccount, title: str, body: str
    ) -> dict[str, Any]:
        html_body = markdown.markdown(body, extensions=["extra", "sane_lists"])
        meta = account.meta or {}
        author = meta.get("author", "")
        thumb = meta.get("thumb_media_id")
        if not isinstance(thumb, str) or not thumb.strip():
            raise PublishConfigError(
                "公众号草稿需要封面图 thumb_media_id，请在账号设置中配置"
            )
        article: dict[str, Any] = {
            "title": title.strip()[:MAX_TITLE] or "未命名",
            "author": author if isinstance(author, str) else "",
            "digest": self._digest(html_body),
            "content": html_body,
            "thumb_media_id": thumb.strip(),
            "need_open_comment": self._meta_int(meta, "need_open_comment"),
            "only_fans_can_comment": self._meta_int(
                meta, "only_fans_can_comment"
            ),
        }
        return article

    async def publish(
        self, *, account: PlatformAccount, title: str, body: str
    ) -> PublishResult:
        creds = self._credentials(account)
        article = self._article(account, title, body)
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            token = await self._token(client, creds)
            data = await self._post(
                client,
                DRAFT_URL,
                params={"access_token": token},
                payload={"articles": [article]},
            )
        errcode = data.get("errcode")
        if errcode:
            errmsg = data.get("errmsg", "")
            raise PublishError(f"公众号草稿创建失败：{errcode} {errmsg}")
        media_id = data.get("media_id")
        if not isinstance(media_id, str) or not media_id:
            raise PublishError("公众号草稿创建失败：缺少 media_id")
        return PublishResult(external_id=media_id, raw=data)
