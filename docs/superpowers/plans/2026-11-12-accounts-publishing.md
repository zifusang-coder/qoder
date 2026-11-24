# 多平台多账号与真实发布实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让每个租户在 9 个平台下管理多个发布账号，并按「平台 + 内容类型」能力执行公众号草稿、抖音视频、头条视频真实发布，人工导出与待接入平台不产生假成功。

**Architecture:** 新增 `backend/app/publish/registry.py` 作为平台能力、凭据字段、meta 字段与自动发布集合的唯一事实来源。发布适配器改为接收 `PublishContent` 上下文与 `StorageBackend`；抖音/头条共用 `open_platform.py`，百家号是明确的占位实现。创建任务、手动执行、worker 定时扫描全部按能力矩阵分流。前端新增 `web/src/platforms.ts`，账号页与项目详情按能力动态渲染。

**Tech Stack:** FastAPI、Pydantic v2、SQLAlchemy 2.0 async、httpx、respx、pytest、React 19、TanStack Query、vitest。

---

## 文件结构

**后端新增**

- `backend/app/publish/registry.py`：平台规格、能力查询、自动/导出集合。
- `backend/app/publish/credentials.py`：凭据解密、必填校验、令牌补丁合并。
- `backend/app/publish/open_platform.py`：开放平台视频发布公共流程。
- `backend/app/publish/douyin.py`：抖音 endpoint 与创建载荷。
- `backend/app/publish/toutiao.py`：头条 endpoint、视频创建与图文拒绝。
- `backend/app/publish/baijiahao.py`：百家号占位适配器。
- `backend/app/publish/adapters.py`：`ADAPTERS` 与 `adapter_for`。
- `backend/tests/test_platform_registry.py`
- `backend/tests/test_accounts.py`
- `backend/tests/test_publish_context.py`
- `backend/tests/test_publish_douyin.py`
- `backend/tests/test_publish_toutiao.py`
- `backend/tests/test_publish_baijiahao.py`
- `backend/tests/test_publish_service.py`

**后端修改**

- `backend/app/publish/base.py`：9 平台、内容上下文、结果令牌补丁。
- `backend/app/publish/wechat.py`：新签名、内容类型校验、公共凭据加载。
- `backend/app/publish/export.py`：`MANUAL_EXPORT_PLATFORMS` 从注册表派生。
- `backend/app/api/accounts.py`：动态凭据/meta、能力字段、平台过滤、能力矩阵接口。
- `backend/app/api/publish.py`：创建规则、`JobOut.content_kind`、执行分流。
- `backend/app/services/publish.py`：内容上下文组装、令牌回写。
- `backend/app/worker.py`：按「平台 + 内容类型」扫描、按平台解析适配器、传存储。
- `backend/tests/test_publish.py`、`test_wechat_publish.py`、`test_worker.py`：适配新签名与新规则。

**前端新增**

- `web/src/platforms.ts`：平台规格、回退矩阵、`usePlatforms`、能力与账号过滤。
- `web/src/__tests__/platforms.test.ts`

**前端修改**

- `web/src/types.ts`：`Account`/`Job` 新字段、9 平台、能力感知的 `actionFor`。
- `web/src/pages/Accounts.tsx`：按平台动态表单与分组列表。
- `web/src/pages/Publish.tsx`：能力感知的账号提示、执行文案与待接入提示。
- `web/src/pages/ProjectDetail.tsx`：内容类型选择、按平台过滤账号。
- `web/src/__tests__/publish-actions.test.ts`：按内容类型更新预期。

**文档**

- `README.md`、`CHANGELOG.md`。

---

### Task 1: 平台能力注册表

**Files:**
- Create: `backend/app/publish/registry.py`
- Test: `backend/tests/test_platform_registry.py`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_platform_registry.py`：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_platform_registry.py -q`

Expected: FAIL，`ModuleNotFoundError: No module named 'app.publish.registry'`。

- [ ] **Step 3: 创建注册表**

创建 `backend/app/publish/registry.py`：

```python
from dataclasses import dataclass
from typing import Literal

Capability = Literal["auto_publish", "manual_export", "pending"]

CAP_AUTO: Capability = "auto_publish"
CAP_MANUAL: Capability = "manual_export"
CAP_PENDING: Capability = "pending"

CONTENT_KIND_LABELS = {
    "article": "图文",
    "note": "笔记",
    "video": "视频",
    "audio": "音频",
}


def content_kind_label(kind: str) -> str:
    return CONTENT_KIND_LABELS.get(kind, kind)


@dataclass(frozen=True)
class CredentialField:
    key: str
    label: str
    secret: bool = False
    required: bool = True
    placeholder: str = ""


@dataclass(frozen=True)
class PlatformSpec:
    key: str
    label: str
    content_capabilities: dict[str, Capability]
    credential_fields: tuple[CredentialField, ...] = ()
    meta_fields: tuple[str, ...] = ()
    note: str = ""


SPECS: dict[str, PlatformSpec] = {
    "wechat": PlatformSpec(
        key="wechat",
        label="公众号",
        content_capabilities={"article": CAP_AUTO},
        credential_fields=(
            CredentialField("app_id", "AppID", placeholder="公众号 AppID"),
            CredentialField("app_secret", "AppSecret", secret=True),
        ),
        meta_fields=(
            "author",
            "thumb_media_id",
            "need_open_comment",
            "only_fans_can_comment",
        ),
        note="使用公众号草稿接口，发布前需配置封面素材 ID",
    ),
    "douyin": PlatformSpec(
        key="douyin",
        label="抖音",
        content_capabilities={"video": CAP_AUTO},
        credential_fields=(
            CredentialField("client_key", "Client Key"),
            CredentialField("client_secret", "Client Secret", secret=True),
            CredentialField("open_id", "Open ID"),
            CredentialField("refresh_token", "Refresh Token", secret=True),
        ),
        note="使用抖音开放平台视频发布接口",
    ),
    "toutiao": PlatformSpec(
        key="toutiao",
        label="头条",
        content_capabilities={"video": CAP_AUTO, "article": CAP_PENDING},
        credential_fields=(
            CredentialField("client_key", "Client Key"),
            CredentialField("client_secret", "Client Secret", secret=True),
            CredentialField("open_id", "Open ID"),
            CredentialField("refresh_token", "Refresh Token", secret=True),
        ),
        meta_fields=("author",),
        note="视频走开放平台接口；图文接口已下线或未获批",
    ),
    "xiaohongshu": PlatformSpec(
        key="xiaohongshu",
        label="小红书",
        content_capabilities={"note": CAP_MANUAL, "article": CAP_MANUAL},
        note="导出素材包后手动发布",
    ),
    "zhihu": PlatformSpec(
        key="zhihu",
        label="知乎",
        content_capabilities={"article": CAP_MANUAL},
        note="导出素材包后手动发布",
    ),
    "kuaishou": PlatformSpec(
        key="kuaishou",
        label="快手",
        content_capabilities={"video": CAP_MANUAL, "note": CAP_MANUAL},
        note="导出素材包后手动发布",
    ),
    "channels": PlatformSpec(
        key="channels",
        label="视频号",
        content_capabilities={"video": CAP_MANUAL, "article": CAP_MANUAL},
        note="导出素材包后手动发布",
    ),
    "baijiahao": PlatformSpec(
        key="baijiahao",
        label="百家号",
        content_capabilities={"article": CAP_PENDING},
        credential_fields=(
            CredentialField("app_id", "AppID"),
            CredentialField("app_token", "App Token", secret=True),
        ),
        note="官方发布接口尚未接入，可先保存凭据",
    ),
    "bilibili": PlatformSpec(
        key="bilibili",
        label="B站",
        content_capabilities={"video": CAP_PENDING},
        note="暂无官方发布接口，可先保存账号",
    ),
}

PLATFORM_ORDER = tuple(SPECS)
PLATFORM_KEYS = frozenset(SPECS)
AUTO_PUBLISH_PAIRS = tuple(
    sorted(
        (spec.key, kind)
        for spec in SPECS.values()
        for kind, capability in spec.content_capabilities.items()
        if capability == CAP_AUTO
    )
)
AUTO_PUBLISH_PLATFORMS = frozenset(
    platform for platform, _kind in AUTO_PUBLISH_PAIRS
)
MANUAL_EXPORT_PLATFORMS = frozenset(
    spec.key
    for spec in SPECS.values()
    if any(value == CAP_MANUAL for value in spec.content_capabilities.values())
)


def spec_for(platform: str) -> PlatformSpec:
    return SPECS[platform]


def capability_for(platform: str, content_kind: str) -> Capability:
    return SPECS[platform].content_capabilities.get(content_kind, CAP_PENDING)


def headline_capability(spec: PlatformSpec) -> Capability:
    values = set(spec.content_capabilities.values())
    if CAP_AUTO in values:
        return CAP_AUTO
    if CAP_MANUAL in values:
        return CAP_MANUAL
    return CAP_PENDING
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_platform_registry.py -q`

Expected: 4 passed。

- [ ] **Step 5: 提交**

```bash
git add backend/app/publish/registry.py backend/tests/test_platform_registry.py
git commit -m "feat: add platform capability registry"
```

---

### Task 2: 内容上下文、凭据模块与公众号适配器

**Files:**
- Modify: `backend/app/publish/base.py`
- Create: `backend/app/publish/credentials.py`
- Modify: `backend/app/publish/wechat.py:74-91,130-150`
- Modify: `backend/app/services/publish.py:296-406`
- Test: `backend/tests/test_publish_context.py`
- Modify: `backend/tests/test_wechat_publish.py:729-782,989-1064`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_publish_context.py`：

```python
import json
import uuid

import pytest
from sqlalchemy import select

from app.config import DEFAULT_TENANT_ID
from app.models.project import ContentVersion
from app.models.publish import PlatformAccount
from app.publish.base import PublishConfigError, PublishContent, PublishResult
from app.publish.wechat import WeChatMPAdapter
from app.security import decrypt_secret
from app.services.publish import apply_action, execute_job
from app.storage import StorageBackend

DOUYIN_CREDENTIAL = {
    "client_key": "ck-1",
    "client_secret": "cs-1",
    "open_id": "open-1",
    "refresh_token": "rt-1",
}


class FakeStorage(StorageBackend):
    def save_text(self, project_id, relpath, text):
        raise NotImplementedError

    def save_bytes(self, project_id, relpath, data):
        raise NotImplementedError

    def read_text(self, project_id, relpath):
        raise NotImplementedError

    def read_bytes(self, project_id, relpath):
        return b"video-bytes"

    def list_files(self, project_id):
        raise NotImplementedError


class CapturingAdapter:
    platform = "douyin"
    capability = "auto"

    def __init__(self):
        self.calls: list[PublishContent] = []

    async def publish(self, *, account, content, storage):
        assert isinstance(storage, FakeStorage)
        self.calls.append(content)
        return PublishResult(
            external_id="video-1",
            credential_patch={"access_token": "fresh-token"},
        )


async def _create_douyin_job(client, db_session):
    project = await client.post(
        "/api/projects", json={"title": f"上下文-{uuid.uuid4().hex[:8]}"}
    )
    assert project.status_code == 201
    content = await client.post(
        f"/api/projects/{project.json()['id']}/contents",
        json={
            "platform": "douyin",
            "kind": "video",
            "title": "视频标题",
            "body": "视频正文",
        },
    )
    assert content.status_code == 201, content.text
    account = await client.post(
        "/api/accounts",
        json={
            "platform": "douyin",
            "name": f"抖音号-{uuid.uuid4().hex[:6]}",
            "credential": DOUYIN_CREDENTIAL,
            "meta": {},
        },
    )
    assert account.status_code == 201, account.text
    job = await client.post(
        "/api/publish/jobs",
        json={
            "content_id": content.json()["id"],
            "platform": "douyin",
            "account_id": account.json()["id"],
        },
    )
    assert job.status_code == 201, job.text

    version = (
        await db_session.execute(
            select(ContentVersion).where(
                ContentVersion.content_id == uuid.UUID(content.json()["id"])
            )
        )
    ).scalar_one()
    version.meta = {
        "files": [
            {
                "name": "video.mp4",
                "path": "media/run/video.mp4",
                "kind": "video",
                "size": 10,
            }
        ],
        "cover": "media/run/cover.png",
    }
    await db_session.commit()

    job_id = uuid.UUID(job.json()["id"])
    for action in ("submit", "pass", "approve"):
        await apply_action(
            db_session,
            job_id,
            action,
            tenant_id=DEFAULT_TENANT_ID,
            actor="tester",
        )
    return job_id, account.json()["id"]


async def test_execute_job_passes_content_context(client, db_session):
    job_id, account_id = await _create_douyin_job(client, db_session)
    adapter = CapturingAdapter()

    job = await execute_job(
        db_session,
        job_id,
        adapter,
        tenant_id=DEFAULT_TENANT_ID,
        actor="tester",
        storage=FakeStorage(),
    )

    assert job.status == "published"
    assert job.external_id == "video-1"
    content = adapter.calls[0]
    assert content.title == "视频标题"
    assert content.body == "视频正文"
    assert content.kind == "video"
    assert content.project_id
    assert [item.path for item in content.media] == ["media/run/video.mp4"]
    assert content.media[0].kind == "video"
    assert content.cover_path == "media/run/cover.png"

    db_session.expire_all()
    account = (
        await db_session.execute(
            select(PlatformAccount).where(
                PlatformAccount.id == uuid.UUID(account_id)
            )
        )
    ).scalar_one()
    stored = json.loads(decrypt_secret(account.credential_enc))
    assert stored["access_token"] == "fresh-token"
    assert "fresh-token" not in account.credential_enc


async def test_credential_patch_failure_keeps_published(
    client, db_session, monkeypatch
):
    job_id, _account_id = await _create_douyin_job(client, db_session)

    def boom(account, patch):
        raise RuntimeError("write failed")

    monkeypatch.setattr("app.services.publish.merge_credential_patch", boom)

    job = await execute_job(
        db_session,
        job_id,
        CapturingAdapter(),
        tenant_id=DEFAULT_TENANT_ID,
        actor="tester",
        storage=FakeStorage(),
    )

    assert job.status == "published"
    assert job.error == ""


async def test_wechat_rejects_video_content_without_http():
    account = PlatformAccount(
        tenant_id=DEFAULT_TENANT_ID,
        platform="wechat",
        name="公众号",
        credential_enc="unused",
        status="active",
        meta={"thumb_media_id": "thumb-1"},
    )
    content = PublishContent(
        project_id="p",
        title="标题",
        body="正文",
        kind="video",
    )

    with pytest.raises(PublishConfigError, match="公众号仅支持图文内容"):
        await WeChatMPAdapter().publish(
            account=account, content=content, storage=FakeStorage()
        )
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_publish_context.py -q`

Expected: FAIL，`PublishContent` 不存在、`execute_job` 不接受 `storage`、公众号适配器签名不匹配。

- [ ] **Step 3: 扩展发布基础类型**

把 `backend/app/publish/base.py` 全文替换为：

```python
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from app.models.publish import PlatformAccount
    from app.storage import StorageBackend

PLATFORMS = (
    "wechat",
    "xiaohongshu",
    "douyin",
    "toutiao",
    "baijiahao",
    "zhihu",
    "kuaishou",
    "channels",
    "bilibili",
)
Platform = Literal[
    "wechat",
    "xiaohongshu",
    "douyin",
    "toutiao",
    "baijiahao",
    "zhihu",
    "kuaishou",
    "channels",
    "bilibili",
]


class PublishError(RuntimeError):
    """发布适配器可安全展示给调用方的错误。"""


class PublishConfigError(PublishError):
    """账号配置缺失或无效，用户可自行修复，不应消耗发布尝试次数。"""


@dataclass(frozen=True)
class PublishMedia:
    name: str
    kind: str
    path: str


@dataclass(frozen=True)
class PublishContent:
    project_id: str
    title: str
    body: str
    kind: str
    media: tuple[PublishMedia, ...] = ()
    cover_path: str | None = None


@dataclass
class PublishResult:
    external_id: str
    url: str = ""
    raw: dict = field(default_factory=dict)
    credential_patch: dict | None = None


class PublisherAdapter(Protocol):
    platform: str
    capability: str

    async def publish(
        self,
        *,
        account: "PlatformAccount",
        content: PublishContent,
        storage: "StorageBackend",
    ) -> PublishResult: ...
```

- [ ] **Step 4: 创建凭据模块**

创建 `backend/app/publish/credentials.py`：

```python
import json

from cryptography.fernet import InvalidToken

from app.models.publish import PlatformAccount
from app.publish.base import PublishConfigError
from app.security import decrypt_secret, encrypt_secret


def _decrypt(account: PlatformAccount, *, label: str) -> dict:
    try:
        raw = decrypt_secret(account.credential_enc)
        creds = json.loads(raw)
    except (InvalidToken, ValueError, TypeError, UnicodeError):
        raise PublishConfigError(f"{label}凭据不可用，请重新授权") from None
    if not isinstance(creds, dict):
        raise PublishConfigError(f"{label}凭据不完整，请重新授权")
    return creds


def load_credential(
    account: PlatformAccount,
    required: tuple[str, ...],
    *,
    label: str,
) -> dict[str, str]:
    creds = _decrypt(account, label=label)
    cleaned: dict[str, str] = {}
    for key in required:
        value = creds.get(key)
        if not isinstance(value, str) or not value.strip():
            raise PublishConfigError(f"{label}凭据不完整，请重新授权")
        cleaned[key] = value.strip()
    return cleaned


def credential_with_tokens(
    account: PlatformAccount,
    required: tuple[str, ...],
    *,
    label: str,
) -> dict[str, object]:
    creds: dict[str, object] = dict(load_credential(account, required, label=label))
    raw = _decrypt(account, label=label)
    for key in ("access_token", "expires_at"):
        value = raw.get(key)
        if isinstance(value, (str, int)) and value:
            creds[key] = value
    return creds


def merge_credential_patch(account: PlatformAccount, patch: dict) -> None:
    creds = _decrypt(account, label="账号")
    for key, value in patch.items():
        if isinstance(value, (str, int)):
            creds[str(key)] = value
    account.credential_enc = encrypt_secret(
        json.dumps(creds, ensure_ascii=False)
    )
```

- [ ] **Step 5: 改造公众号适配器**

修改 `backend/app/publish/wechat.py` 顶部导入，删除不再使用的 `json`、`InvalidToken`、`decrypt_secret`：

```python
import html
import re
from typing import Any

import httpx
import markdown

from app.logging_setup import configure_http_logging
from app.models.publish import PlatformAccount
from app.publish.base import (
    PublishConfigError,
    PublishContent,
    PublishError,
    PublishResult,
)
from app.publish.credentials import load_credential
```

把第 74-91 行的 `_credentials` 方法整段删除。把文件末尾的 `publish` 方法（第 130-150 行）替换为：

```python
    async def publish(
        self,
        *,
        account: PlatformAccount,
        content: PublishContent,
        storage: object,
    ) -> PublishResult:
        if content.kind != "article":
            raise PublishConfigError("公众号仅支持图文内容")
        creds = load_credential(
            account, ("app_id", "app_secret"), label="公众号"
        )
        article = self._article(account, content.title, content.body)
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
```

`storage` 在公众号路径不使用，保留协议参数即可。

- [ ] **Step 6: 在发布服务中组装内容上下文并回写令牌**

修改 `backend/app/services/publish.py` 顶部导入：

```python
from app.publish.base import (
    PublishConfigError,
    PublishContent,
    PublishError,
    PublishMedia,
    PublisherAdapter,
)
from app.publish.credentials import merge_credential_patch
```

在导入区补充存储类型：

```python
from app.storage import StorageBackend
```

把 `execute_job` 的签名（第 296-304 行）改为：

```python
async def execute_job(
    session: AsyncSession,
    job_id: uuid.UUID,
    adapter: PublisherAdapter,
    *,
    tenant_id: str,
    actor: str,
    storage: StorageBackend,
    require_due: datetime | None = None,
) -> PublishJob:
```

在读取 `version` 之后（原第 328 行 `raise AppError("empty_content", ...)` 之后）加入内容查询与上下文组装：

```python
    content = (
        await session.execute(
            select(Content).where(
                Content.id == job.content_id,
                Content.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if content is None:
        raise AppError("empty_content", "内容不存在", 409)

    meta = version.meta if isinstance(version.meta, dict) else {}
    raw_files = meta.get("files")
    media: list[PublishMedia] = []
    if isinstance(raw_files, list):
        for item in raw_files:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            path = item.get("path")
            if isinstance(name, str) and name and isinstance(path, str) and path:
                media.append(
                    PublishMedia(
                        name=name,
                        kind=str(item.get("kind") or "text"),
                        path=path,
                    )
                )
    cover = meta.get("cover")
    payload = PublishContent(
        project_id=str(content.project_id),
        title=version.title,
        body=version.body,
        kind=content.kind,
        media=tuple(media),
        cover_path=cover if isinstance(cover, str) and cover else None,
    )
```

把适配器调用（原第 361-364 行）改为：

```python
    try:
        result = await adapter.publish(
            account=account, content=payload, storage=storage
        )
```

把末尾成功返回（原第 394-406 行）改为：

```python
    published = await apply_action(
        session,
        job.id,
        "succeed",
        tenant_id=tenant_id,
        actor=actor,
        fields={
            "external_id": result.external_id,
            "external_url": result.url,
            "published_at": datetime.now(UTC),
            "error": "",
        },
    )
    if result.credential_patch:
        try:
            merge_credential_patch(account, result.credential_patch)
            await session.commit()
        except Exception:
            # 刷新令牌回写失败不影响已确认的发布结果
            logger.exception("发布凭据回写失败 job=%s", published.id)
            await session.rollback()
            reloaded = await _reload_job(session, published.id, tenant_id)
            if reloaded is not None:
                return reloaded
    return published
```

- [ ] **Step 7: 更新公众号旧测试的调用方式**

在 `backend/tests/test_wechat_publish.py` 顶部导入 `PublishContent`：

```python
from app.publish.base import PublishContent, PublishError, PublishResult
```

并新增一个最小存储桩（放在 `CREDENTIAL` 定义之后）：

```python
class _FakeStorage:
    def read_bytes(self, project_id: str, relpath: str) -> bytes:
        raise AssertionError("公众号发布不应读取媒体文件")
```

把第 729-731 行改为：

```python
    result = await WeChatMPAdapter().publish(
        account=account,
        content=PublishContent(
            project_id="p", title="长" * 100, body="正文", kind="article"
        ),
        storage=_FakeStorage(),
    )
```

把第 760 行改为：

```python
    await WeChatMPAdapter().publish(
        account=account,
        content=PublishContent(
            project_id="p", title="   ", body="正文", kind="article"
        ),
        storage=_FakeStorage(),
    )
```

把第 782 行改为：

```python
    await WeChatMPAdapter().publish(
        account=account,
        content=PublishContent(
            project_id="p", title="标题", body="A & B", kind="article"
        ),
        storage=_FakeStorage(),
    )
```

把 `BoomAdapter.publish` 和 `InspectingAdapter.publish` 的签名改为：

```python
        async def publish(self, *, account, content, storage):
```

`InspectingAdapter` 中断言逻辑不变，`BoomAdapter` 仍抛 `RuntimeError`。

在两处 `execute_job(...)` 调用（第 997 与 1048 行）的 `actor=` 后加入：

```python
            storage=_FakeStorage(),
```

- [ ] **Step 8: 运行相关测试**

Run: `cd backend && uv run pytest tests/test_publish_context.py tests/test_wechat_publish.py -q`

Expected: 全部 PASS。

- [ ] **Step 9: 提交**

```bash
git add backend/app/publish/base.py backend/app/publish/credentials.py backend/app/publish/wechat.py backend/app/services/publish.py backend/tests/test_publish_context.py backend/tests/test_wechat_publish.py
git commit -m "feat: pass publish content context and persist token patches"
```

---

### Task 3: 账号 API 动态凭据与能力矩阵

**Files:**
- Modify: `backend/app/api/accounts.py`
- Test: `backend/tests/test_accounts.py`
- Modify: `backend/tests/test_wechat_publish.py:184`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_accounts.py`：

```python
import json
import uuid

WECHAT = {"app_id": "wx-1", "app_secret": "wx-secret-1"}
DOUYIN = {
    "client_key": "ck-1",
    "client_secret": "cs-secret-1",
    "open_id": "open-1",
    "refresh_token": "rt-secret-1",
}


def _payload(platform="wechat", name=None, credential=None, meta=None):
    return {
        "platform": platform,
        "name": name or f"账号-{uuid.uuid4().hex[:6]}",
        "credential": credential if credential is not None else {},
        "meta": meta or {},
    }


async def _create(client, **overrides):
    return await client.post("/api/accounts", json=_payload(**overrides))


async def test_same_platform_allows_multiple_accounts(client):
    first = await _create(
        client,
        name="主号",
        credential=WECHAT,
        meta={"thumb_media_id": "thumb-1"},
    )
    second = await _create(
        client,
        name="副号",
        credential=WECHAT,
        meta={"thumb_media_id": "thumb-2"},
    )

    assert first.status_code == 201
    assert second.status_code == 201
    items = (await client.get("/api/accounts")).json()
    names = {item["name"] for item in items if item["platform"] == "wechat"}
    assert {"主号", "副号"} <= names


async def test_same_name_same_platform_conflicts(client):
    assert (
        await _create(
            client,
            name="重名",
            credential=WECHAT,
            meta={"thumb_media_id": "t1"},
        )
    ).status_code == 201
    duplicate = await _create(
        client,
        name="重名",
        credential=WECHAT,
        meta={"thumb_media_id": "t2"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "account_exists"


async def test_same_name_across_platforms_is_allowed(client):
    assert (
        await _create(
            client,
            platform="wechat",
            name="同名",
            credential=WECHAT,
            meta={"thumb_media_id": "t1"},
        )
    ).status_code == 201
    assert (
        await _create(
            client,
            platform="douyin",
            name="同名",
            credential=DOUYIN,
        )
    ).status_code == 201


async def test_platform_filter_and_order(client):
    await _create(
        client,
        platform="douyin",
        credential=DOUYIN,
    )
    await _create(
        client,
        platform="wechat",
        credential=WECHAT,
        meta={"thumb_media_id": "t1"},
    )

    response = await client.get("/api/accounts")
    assert [item["platform"] for item in response.json()] == [
        "wechat",
        "douyin",
    ]

    filtered = await client.get("/api/accounts", params={"platform": "douyin"})
    assert [item["platform"] for item in filtered.json()] == ["douyin"]

    missing = await client.get("/api/accounts", params={"platform": "unknown"})
    assert missing.status_code == 404


async def test_dynamic_credential_validation(client):
    missing = await _create(client, platform="douyin", credential={})
    assert missing.status_code == 422

    unsupported = await _create(
        client,
        platform="douyin",
        credential={**DOUYIN, "app_secret": "nope"},
    )
    assert unsupported.status_code == 422

    ok = await _create(client, platform="douyin", credential=DOUYIN)
    assert ok.status_code == 201


async def test_manual_platform_accepts_only_empty_credential(client):
    ok = await _create(
        client, platform="xiaohongshu", credential={}, meta={}
    )
    assert ok.status_code == 201
    assert ok.json()["capability"] == "manual_export"

    rejected = await _create(
        client,
        platform="xiaohongshu",
        credential={"token": "x"},
    )
    assert rejected.status_code == 422


async def test_meta_is_validated_per_platform(client):
    toutiao = await _create(
        client,
        platform="toutiao",
        credential=DOUYIN,
        meta={"author": "作者"},
    )
    assert toutiao.status_code == 201
    assert toutiao.json()["meta"] == {"author": "作者"}

    bad_meta = await _create(
        client,
        platform="toutiao",
        credential=DOUYIN,
        meta={"thumb_media_id": "t1"},
    )
    assert bad_meta.status_code == 422
    assert bad_meta.json()["code"] == "validation_error"


async def test_platforms_endpoint_exposes_matrix_without_secrets(client):
    response = await client.get("/api/accounts/platforms")
    assert response.status_code == 200
    specs = response.json()
    assert [item["key"] for item in specs] == [
        "wechat",
        "douyin",
        "toutiao",
        "xiaohongshu",
        "zhihu",
        "kuaishou",
        "channels",
        "baijiahao",
        "bilibili",
    ]
    by_key = {item["key"]: item for item in specs}
    assert by_key["wechat"]["capability"] == "auto_publish"
    assert by_key["xiaohongshu"]["capability"] == "manual_export"
    assert by_key["baijiahao"]["capability"] == "pending"
    secret_fields = {
        field["key"]
        for field in by_key["douyin"]["credential_fields"]
        if field["secret"]
    }
    assert secret_fields == {"client_secret", "refresh_token"}
    assert "****" not in response.text
    assert "rt-secret" not in response.text


async def test_credentials_are_masked_and_encrypted(client, db_session):
    response = await _create(client, platform="douyin", credential=DOUYIN)
    assert response.status_code == 201
    body = response.json()
    masked = json.loads(body["credential_masked"])
    assert masked == {
        "client_key": "****",
        "client_secret": "****",
        "open_id": "****",
        "refresh_token": "****",
    }
    for marker in ("cs-secret-1", "rt-secret-1"):
        assert marker not in response.text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_accounts.py -q`

Expected: FAIL，动态凭据、平台过滤、`/accounts/platforms` 与 `capability` 字段尚不存在。

- [ ] **Step 3: 改造账号校验函数**

修改 `backend/app/api/accounts.py` 的导入区：

```python
from app.publish.registry import (
    PLATFORM_ORDER,
    headline_capability,
    spec_for,
)
```

把 `ALLOWED_META` 常量删除，改为：

```python
MAX_AUTHOR = 64
MAX_THUMB_MEDIA_ID = 200
```

把 `_clean_meta`（第 39-70 行）全文替换为：

```python
def _clean_meta(platform: str, meta: dict) -> dict:
    spec = spec_for(platform)
    allowed = set(spec.meta_fields)
    cleaned: dict = {}
    for key, value in meta.items():
        if key not in allowed:
            raise ValueError(f"{spec.label}不支持 meta 字段：{key}")
        if key == "author":
            if not isinstance(value, str):
                raise ValueError("meta 字段 author 必须是字符串")
            author = value.strip()
            if len(author) > MAX_AUTHOR:
                raise ValueError(
                    f"meta 字段 author 长度不能超过 {MAX_AUTHOR}"
                )
            cleaned[key] = author
        elif key == "thumb_media_id":
            if not isinstance(value, str):
                raise ValueError("meta 字段 thumb_media_id 必须是字符串")
            thumb = value.strip()
            if not thumb:
                raise ValueError("公众号封面图 thumb_media_id 不能为空")
            if len(thumb) > MAX_THUMB_MEDIA_ID:
                raise ValueError(
                    "meta 字段 thumb_media_id 长度不能超过 "
                    f"{MAX_THUMB_MEDIA_ID}"
                )
            cleaned[key] = thumb
        else:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value not in (0, 1)
            ):
                raise ValueError(f"meta 字段 {key} 只能为 0 或 1")
            cleaned[key] = value
    return cleaned
```

把 `_clean_credential`（第 73-85 行）全文替换为：

```python
def _clean_credential(platform: str, credential: dict) -> dict:
    spec = spec_for(platform)
    declared = {field.key for field in spec.credential_fields}
    unknown = set(credential) - declared
    if unknown:
        raise ValueError(
            f"{spec.label}不支持的凭据字段：{', '.join(sorted(unknown))}"
        )
    cleaned: dict[str, str] = {}
    for field in spec.credential_fields:
        value = credential.get(field.key)
        if value is None:
            if field.required:
                raise ValueError(
                    f"{spec.label}凭据不完整，请填写 {field.label}"
                )
            continue
        if not isinstance(value, str):
            raise ValueError(f"{spec.label}凭据字段 {field.label} 必须是字符串")
        value = value.strip()
        if field.required and not value:
            raise ValueError(f"{spec.label}凭据不完整，请填写 {field.label}")
        if value:
            cleaned[field.key] = value
    return cleaned
```

- [ ] **Step 4: 更新请求/响应模型**

把 `AccountIn`（第 88-103 行）替换为：

```python
class AccountIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Platform
    name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    ]
    credential: dict = Field(default_factory=dict)
    meta: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_credentials_and_meta(self) -> "AccountIn":
        self.credential = _clean_credential(self.platform, self.credential)
        self.meta = _clean_meta(self.platform, self.meta)
        return self
```

把 `AccountUpdate._validate_update`（第 119-125 行）替换为：

```python
    @model_validator(mode="after")
    def _validate_update(self) -> "AccountUpdate":
        if self.name is None and self.credential is None and self.meta is None:
            raise ValueError("至少需要提供一个要更新的字段")
        return self
```

把 `AccountOut`（第 128-133 行）替换为：

```python
class AccountOut(BaseModel):
    id: str
    platform: str
    name: str
    status: str
    credential_masked: str
    meta: dict
    capability: str
    capability_note: str


class CredentialFieldOut(BaseModel):
    key: str
    label: str
    secret: bool
    required: bool
    placeholder: str


class PlatformOut(BaseModel):
    key: str
    label: str
    capability: str
    content_capabilities: dict[str, str]
    credential_fields: list[CredentialFieldOut]
    meta_fields: list[str]
    note: str
```

把 `_out`（第 147-154 行）替换为：

```python
def _out(account: PlatformAccount) -> AccountOut:
    spec = spec_for(account.platform)
    stored_meta = account.meta if isinstance(account.meta, dict) else {}
    return AccountOut(
        id=str(account.id),
        platform=account.platform,
        name=account.name,
        status=account.status,
        credential_masked=_masked_credential(account),
        meta={
            key: value
            for key, value in stored_meta.items()
            if key in spec.meta_fields
        },
        capability=headline_capability(spec),
        capability_note=spec.note,
    )
```

- [ ] **Step 5: 增加能力矩阵与平台过滤接口**

在 `_owned_account` 之后、`list_accounts` 之前插入：

```python
@router.get("/platforms", response_model=list[PlatformOut])
async def list_platforms(
    user: User = Depends(editor_access),  # noqa: B008
) -> list[PlatformOut]:
    return [
        PlatformOut(
            key=spec.key,
            label=spec.label,
            capability=headline_capability(spec),
            content_capabilities=dict(spec.content_capabilities),
            credential_fields=[
                CredentialFieldOut(
                    key=field.key,
                    label=field.label,
                    secret=field.secret,
                    required=field.required,
                    placeholder=field.placeholder,
                )
                for field in spec.credential_fields
            ],
            meta_fields=list(spec.meta_fields),
            note=spec.note,
        )
        for spec in (spec_for(key) for key in PLATFORM_ORDER)
    ]
```

把 `list_accounts`（第 180-194 行）替换为：

```python
@router.get("", response_model=list[AccountOut])
async def list_accounts(
    platform: str | None = None,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[AccountOut]:
    stmt = select(PlatformAccount).where(
        PlatformAccount.tenant_id == user.tenant_id
    )
    if platform is not None:
        if platform not in PLATFORM_ORDER:
            raise AppError("not_found", "平台不存在", 404)
        stmt = stmt.where(PlatformAccount.platform == platform)
    rows = (
        await session.execute(
            stmt.order_by(
                PlatformAccount.created_at.desc(),
                PlatformAccount.id.desc(),
            )
        )
    ).scalars().all()
    order = {key: index for index, key in enumerate(PLATFORM_ORDER)}
    # 先按创建时间倒序，再按平台顺序稳定排序，前端可直接分组
    rows = sorted(rows, key=lambda account: order.get(account.platform, 99))
    return [_out(account) for account in rows]
```

- [ ] **Step 6: 更新 PATCH 的 meta 清洗**

把 `update_account` 中（第 257-261 行）的 meta 合并改为：

```python
    meta = None
    if payload.meta is not None:
        try:
            cleaned_meta = _clean_meta(account.platform, payload.meta)
        except ValueError as exc:
            raise AppError("validation_error", str(exc), 422) from None
        # PATCH 只合并本次提交的键，未提交的 meta 必须保留
        stored = account.meta if isinstance(account.meta, dict) else {}
        meta = {
            **{
                key: value
                for key, value in stored.items()
                if key in spec_for(account.platform).meta_fields
            },
            **cleaned_meta,
        }
```

- [ ] **Step 7: 更新旧账号响应断言**

把 `backend/tests/test_wechat_publish.py` 第 184 行改为：

```python
    assert set(body) == {
        "id",
        "platform",
        "name",
        "status",
        "credential_masked",
        "meta",
        "capability",
        "capability_note",
    }
```

- [ ] **Step 8: 运行测试**

Run: `cd backend && uv run pytest tests/test_accounts.py tests/test_wechat_publish.py tests/test_publish.py -q`

Expected: 全部 PASS。

- [ ] **Step 9: 提交**

```bash
git add backend/app/api/accounts.py backend/tests/test_accounts.py backend/tests/test_wechat_publish.py
git commit -m "feat: support dynamic multi-platform account credentials"
```

---

### Task 4: 抖音、头条、百家号适配器

**Files:**
- Create: `backend/app/publish/open_platform.py`
- Create: `backend/app/publish/douyin.py`
- Create: `backend/app/publish/toutiao.py`
- Create: `backend/app/publish/baijiahao.py`
- Create: `backend/app/publish/adapters.py`
- Modify: `backend/app/publish/export.py:1-35`
- Test: `backend/tests/test_publish_douyin.py`
- Test: `backend/tests/test_publish_toutiao.py`
- Test: `backend/tests/test_publish_baijiahao.py`
- Test: `backend/tests/test_platform_registry.py`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_publish_douyin.py`：

```python
import json
import time

import httpx
import pytest
import respx

from app.models.publish import PlatformAccount
from app.publish.base import PublishConfigError, PublishContent, PublishError, PublishMedia
from app.publish.douyin import DouyinAdapter, CREATE_URL, REFRESH_URL, UPLOAD_URL
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
```

创建 `backend/tests/test_publish_toutiao.py`：

```python
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
```

创建 `backend/tests/test_publish_baijiahao.py`：

```python
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
```

在 `backend/tests/test_platform_registry.py` 末尾追加：

```python
def test_adapter_registry_covers_auto_and_pending_platforms():
    from app.publish.adapters import ADAPTERS, adapter_for

    assert set(ADAPTERS) == {"wechat", "douyin", "toutiao", "baijiahao"}
    assert adapter_for("douyin").platform == "douyin"
    with pytest.raises(Exception, match="暂不支持自动发布"):
        adapter_for("xiaohongshu")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_publish_douyin.py tests/test_publish_toutiao.py tests/test_publish_baijiahao.py tests/test_platform_registry.py -q`

Expected: FAIL，适配器模块不存在。

- [ ] **Step 3: 实现开放平台公共适配器**

创建 `backend/app/publish/open_platform.py`：

```python
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
```

- [ ] **Step 4: 实现三个平台适配器与注册表**

创建 `backend/app/publish/douyin.py`：

```python
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
```

创建 `backend/app/publish/toutiao.py`：

```python
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
```

创建 `backend/app/publish/baijiahao.py`：

```python
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
```

创建 `backend/app/publish/adapters.py`：

```python
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
```

- [ ] **Step 5: 让导出平台集合从注册表派生**

修改 `backend/app/publish/export.py` 顶部：

```python
from app.publish.registry import MANUAL_EXPORT_PLATFORMS
```

删除原第 35 行的 `MANUAL_EXPORT_PLATFORMS = frozenset(GUIDES)`。

- [ ] **Step 6: 运行测试**

Run: `cd backend && uv run pytest tests/test_publish_douyin.py tests/test_publish_toutiao.py tests/test_publish_baijiahao.py tests/test_platform_registry.py tests/test_wechat_publish.py -q`

Expected: 全部 PASS。

- [ ] **Step 7: 提交**

```bash
git add backend/app/publish/open_platform.py backend/app/publish/douyin.py backend/app/publish/toutiao.py backend/app/publish/baijiahao.py backend/app/publish/adapters.py backend/app/publish/export.py backend/tests/test_publish_douyin.py backend/tests/test_publish_toutiao.py backend/tests/test_publish_baijiahao.py backend/tests/test_platform_registry.py
git commit -m "feat: add douyin, toutiao and baijiahao publish adapters"
```

---

### Task 5: 创建任务规则与 JobOut 内容类型

**Files:**
- Modify: `backend/app/api/publish.py`
- Modify: `backend/tests/test_publish.py`
- Modify: `backend/tests/test_wechat_publish.py`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_publish.py` 的 `_create_content` 中加入 `kind` 参数，把函数（第 21-35 行）替换为：

```python
async def _create_content(
    client,
    *,
    title="标题",
    body="正文",
    platform="wechat",
    kind=None,
):
    project = await client.post(
        "/api/projects", json={"title": f"发布测试-{uuid.uuid4().hex[:8]}"}
    )
    assert project.status_code == 201
    payload = {"platform": platform}
    if kind is not None:
        payload["kind"] = kind
    if title:
        payload["title"] = title
    if body:
        payload["body"] = body
    content = await client.post(
        f"/api/projects/{project.json()['id']}/contents", json=payload
    )
    assert content.status_code == 201
    return content.json()
```

把 `_create_job`（第 38-46 行）替换为：

```python
_AUTO_PLATFORMS = {"wechat", "douyin", "toutiao"}


async def _ensure_account(client, platform: str) -> str:
    name = "自动测试账号"
    existing = await client.get(
        "/api/accounts", params={"platform": platform}
    )
    assert existing.status_code == 200
    for item in existing.json():
        if item["name"] == name:
            return item["id"]
    if platform == "wechat":
        payload = {
            "platform": platform,
            "name": name,
            "credential": {"app_id": "wx-auto", "app_secret": "auto-secret"},
            "meta": {"thumb_media_id": "thumb-auto"},
        }
    else:
        payload = {
            "platform": platform,
            "name": name,
            "credential": {
                "client_key": "ck-auto",
                "client_secret": "cs-auto",
                "open_id": "open-auto",
                "refresh_token": "rt-auto",
            },
            "meta": {"author": "自动作者"},
        }
    created = await client.post("/api/accounts", json=payload)
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def _create_job(
    client,
    content_id,
    *,
    platform="wechat",
    account_id=None,
    scheduled_at=None,
    auto_account=True,
):
    if account_id is None and auto_account and platform in _AUTO_PLATFORMS:
        account_id = await _ensure_account(client, platform)
    payload = {"content_id": str(content_id), "platform": platform}
    if account_id is not None:
        payload["account_id"] = str(account_id)
    if scheduled_at is not None:
        payload["scheduled_at"] = scheduled_at
    return await client.post("/api/publish/jobs", json=payload)
```

在文件末尾追加：

```python
async def test_auto_platform_requires_account(client):
    content = await _create_content(client, platform="wechat")

    response = await _create_job(
        client, content["id"], platform="wechat", auto_account=False
    )

    assert response.status_code == 409
    assert response.json()["code"] == "account_missing"


async def test_manual_platform_allows_no_account(client):
    content = await _create_content(client, platform="xiaohongshu")

    response = await _create_job(
        client, content["id"], platform="xiaohongshu", auto_account=False
    )

    assert response.status_code == 201
    assert response.json()["account_id"] is None


async def test_pending_platform_allows_no_account(client):
    content = await _create_content(
        client, platform="toutiao", kind="article"
    )

    response = await _create_job(
        client, content["id"], platform="toutiao", auto_account=False
    )

    assert response.status_code == 201
    assert response.json()["account_id"] is None


async def test_platform_content_mismatch_is_rejected(client):
    content = await _create_content(
        client, platform="wechat", kind="video"
    )

    response = await _create_job(client, content["id"], platform="wechat")

    assert response.status_code == 409
    assert response.json()["code"] == "platform_content_mismatch"
    assert "不支持" in response.json()["message"]


async def test_job_out_includes_content_kind(client):
    content = await _create_content(client, platform="wechat")
    job = await _create_job(client, content["id"])

    assert job.status_code == 201
    assert job.json()["content_kind"] == "article"

    listed = await client.get("/api/publish/jobs")
    assert listed.json()["items"][0]["content_kind"] == "article"
```

把 `test_create_job_from_current_version`（第 275 行起）中的内容平台改为小红书，并断言无账号：

```python
async def test_create_job_from_current_version(client, db_session):
    content = await _create_content(client, platform="xiaohongshu")
    response = await _create_job(
        client, content["id"], platform="xiaohongshu"
    )

    assert response.status_code == 201
    body = response.json()
    assert body["content_version"] == 1
    assert body["platform"] == "xiaohongshu"
    assert body["account_id"] is None
    assert body["status"] == "draft"
```

把 `test_idempotency_varies_by_platform_and_account`、`test_list_jobs_filters_tenant_and_status`、`test_list_jobs_pagination` 中所有 `platform="douyin"` 的内容创建改为：

```python
await _create_content(client, platform="douyin", kind="video")
```

把 `test_content_platform_mismatch` 的调用改为不预建账号：

```python
    response = await _create_job(
        client, content["id"], platform="douyin", auto_account=False
    )
```

把 `test_foreign_tenant_idempotency_key_is_ignored` 改为小红书版本：

```python
async def test_foreign_tenant_idempotency_key_is_ignored(client, db_session):
    content = await _create_content(client, platform="xiaohongshu")
    key = f"{uuid.UUID(content['id'])}:1:xiaohongshu:-"
    foreign_content = await _seed_foreign_content(
        db_session, platform="xiaohongshu"
    )
    foreign_job = PublishJob(
        tenant_id=OTHER_TENANT,
        content_id=foreign_content.id,
        content_version=1,
        platform="xiaohongshu",
        status="draft",
        idempotency_key=key,
    )
    db_session.add(foreign_job)
    await db_session.commit()
    await db_session.refresh(foreign_job)

    response = await _create_job(
        client, content["id"], platform="xiaohongshu"
    )
    assert response.status_code == 201
    assert response.json()["id"] != str(foreign_job.id)

    stored = await _stored_job(db_session, response.json()["id"])
    assert stored.tenant_id == DEFAULT_TENANT_ID
```

把 `_seed_foreign_content`（第 105 行起）签名改为：

```python
async def _seed_foreign_content(db_session, *, platform="wechat"):
```

并把其中 `Content(... platform="wechat" ...)` 改为 `platform=platform`。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_publish.py -q`

Expected: 新增规则用例 FAIL，`content_kind` 缺失。

- [ ] **Step 3: 扩展 JobOut 与序列化**

修改 `backend/app/api/publish.py` 导入区，加入：

```python
from app.publish.registry import (
    CAP_AUTO,
    capability_for,
    content_kind_label,
    spec_for,
)
```

在 `JobOut`（第 94-110 行）中 `content_version` 后加入：

```python
    content_kind: str
```

把 `_out`（第 126-144 行）改为：

```python
def _out(job: PublishJob, content_kind: str = "") -> JobOut:
    return JobOut(
        id=str(job.id),
        content_id=str(job.content_id),
        content_version=job.content_version,
        content_kind=content_kind,
        platform=job.platform,
        account_id=str(job.account_id) if job.account_id else None,
        status=job.status,
        scheduled_at=_utc(job.scheduled_at),
        published_at=_utc(job.published_at),
        external_id=job.external_id,
        external_url=job.external_url,
        export_path=job.export_path,
        attempts=job.attempts,
        error=job.error,
        checks=job.checks or {},
        created_at=_utc(job.created_at),
        updated_at=_utc(job.updated_at),
    )
```

在 `_out` 之后新增：

```python
async def _job_out(
    session: AsyncSession, job: PublishJob, content_kind: str | None = None
) -> JobOut:
    kind = content_kind
    if kind is None:
        kind = (
            await session.execute(
                select(Content.kind).where(Content.id == job.content_id)
            )
        ).scalar_one_or_none() or ""
    return _out(job, kind)
```

- [ ] **Step 4: 列表查询带出内容类型**

把 `list_jobs`（第 238-258 行）替换为：

```python
@router.get("/jobs", response_model=JobList)
async def list_jobs(
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> JobList:
    base = select(PublishJob).where(PublishJob.tenant_id == user.tenant_id)
    if status is not None:
        base = base.where(PublishJob.status == status)
    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    stmt = (
        select(PublishJob, Content.kind)
        .join(
            Content,
            (Content.id == PublishJob.content_id)
            & (Content.tenant_id == PublishJob.tenant_id),
        )
        .where(PublishJob.tenant_id == user.tenant_id)
    )
    if status is not None:
        stmt = stmt.where(PublishJob.status == status)
    stmt = (
        stmt.order_by(PublishJob.created_at.desc(), PublishJob.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).all()
    return JobList(
        items=[_out(job, kind) for job, kind in rows],
        total=total,
    )
```

- [ ] **Step 5: 创建任务按能力分流**

把 `create_job` 中平台不一致校验之后（第 303-304 行）插入：

```python
    spec = spec_for(payload.platform)
    capability = capability_for(payload.platform, content.kind)
    if content.kind not in spec.content_capabilities:
        raise AppError(
            "platform_content_mismatch",
            f"{spec.label}不支持{content_kind_label(content.kind)}内容",
            409,
        )
```

把账号校验块之后（原第 323 行 `raise AppError("account_inactive"...)` 之后）加入：

```python
    if capability == CAP_AUTO and account is None:
        raise AppError("account_missing", "请先选择并授权发布账号", 409)
```

把 `create_job` 末尾 `return _out(job)`（第 366 行）改为：

```python
    return _out(job, content.kind)
```

把 `_replay_existing` 中的 `return _out(job)`（第 234 行）改为：

```python
        return await _job_out(session, job)
```

- [ ] **Step 6: 其余端点补齐 content_kind**

把以下 `return _out(...)` 替换为异步辅助：

- `get_job`（第 375 行）：
```python
    return await _job_out(session, await _owned_job(session, job_id, user))
```
- `precheck_job` 的返回（第 461 行）：
```python
    return PrecheckOut(job=_out(job, content.kind), **report)
```
- `execute_publish` 成功返回（当前 Task 5 暂保持 `return _out(job)`，Task 6 会改）。
- `export_publish`（第 541 行）：
```python
    return await _job_out(session, job)
```
- `schedule_job`（第 577 行）：
```python
    return await _job_out(session, job)
```
- `act_on_job`（第 620 行）：
```python
    return await _job_out(session, job)
```

- [ ] **Step 7: 更新微信发布测试的账号创建**

`test_wechat_publish.py` 的 `_create_job` 现有显式账号调用不受影响。把 `test_execute_without_account_returns_409` 替换为：

```python
async def test_create_job_without_account_returns_409(client):
    content = await _create_content(client)

    response = await client.post(
        "/api/publish/jobs",
        json={"content_id": content["id"], "platform": "wechat"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "account_missing"
```

把 `test_non_wechat_platform_requires_manual_export` 改为小红书内容与任务（避免依赖抖音视频）：

```python
async def test_non_wechat_platform_requires_manual_export(client, db_session):
    content = await _create_content(client, platform="xiaohongshu")
    job = await _create_job(
        client, content["id"], platform="xiaohongshu"
    )
    await _approve(client, job["id"])

    response = await _execute(client, job["id"])
    assert response.status_code == 409
    assert response.json()["code"] == "manual_export_required"
```

- [ ] **Step 8: 运行测试**

Run: `cd backend && uv run pytest tests/test_publish.py tests/test_wechat_publish.py tests/test_accounts.py -q`

Expected: 全部 PASS。

- [ ] **Step 9: 提交**

```bash
git add backend/app/api/publish.py backend/tests/test_publish.py backend/tests/test_wechat_publish.py
git commit -m "feat: gate publish job creation by platform capability"
```

---

### Task 6: 手动执行按能力分流

**Files:**
- Modify: `backend/app/api/publish.py:464-502`
- Test: `backend/tests/test_publish_service.py`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_publish_service.py`：

```python
import uuid


async def _content(client, *, platform, kind):
    project = await client.post(
        "/api/projects", json={"title": f"服务-{uuid.uuid4().hex[:8]}"}
    )
    response = await client.post(
        f"/api/projects/{project.json()['id']}/contents",
        json={"platform": platform, "kind": kind, "title": "标题", "body": "正文"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _approved_job(client, content, *, account_id=None):
    payload = {"content_id": content["id"], "platform": content["platform"]}
    if account_id:
        payload["account_id"] = account_id
    created = await client.post("/api/publish/jobs", json=payload)
    assert created.status_code == 201, created.text
    job = created.json()
    precheck = await client.post(
        f"/api/publish/jobs/{job['id']}/precheck"
    )
    assert precheck.status_code == 200, precheck.text
    approve = await client.post(
        f"/api/publish/jobs/{job['id']}/actions",
        json={"action": "approve"},
    )
    assert approve.status_code == 200, approve.text
    return job


async def test_pending_platform_execute_returns_409(client):
    content = await _content(client, platform="toutiao", kind="article")
    job = await _approved_job(client, content)

    response = await client.post(
        f"/api/publish/jobs/{job['id']}/execute"
    )

    assert response.status_code == 409
    assert response.json()["code"] == "platform_not_available"
    assert "图文接口" in response.json()["message"]


async def test_manual_platform_execute_returns_409(client):
    content = await _content(client, platform="xiaohongshu", kind="note")
    job = await _approved_job(client, content)

    response = await client.post(
        f"/api/publish/jobs/{job['id']}/execute"
    )

    assert response.status_code == 409
    assert response.json()["code"] == "manual_export_required"


async def test_manual_export_still_works(client):
    content = await _content(client, platform="xiaohongshu", kind="note")
    job = await _approved_job(client, content)

    response = await client.post(
        f"/api/publish/jobs/{job['id']}/export"
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "exported"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_publish_service.py -q`

Expected: pending 平台返回 409 `manual_export_required`（错误码不符），用例 FAIL。

- [ ] **Step 3: 改造执行端点**

修改 `backend/app/api/publish.py` 导入区：

```python
from app.publish.adapters import adapter_for
from app.publish.registry import (
    CAP_AUTO,
    CAP_MANUAL,
    capability_for,
    content_kind_label,
    spec_for,
)
```

删除 `from app.publish.wechat import WeChatMPAdapter`。

把 `execute_publish`（第 464-502 行）替换为：

```python
@router.post("/jobs/{job_id}/execute", response_model=JobOut)
async def execute_publish(
    job_id: str,
    request: Request,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    storage: StorageBackend = Depends(get_storage),  # noqa: B008
) -> JobOut:
    job = await _owned_job(session, job_id, user)
    content = (
        await session.execute(
            select(Content).where(
                Content.id == job.content_id,
                Content.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if content is None:
        raise AppError("not_found", "内容不存在", 404)

    try:
        spec = spec_for(job.platform)
        capability = capability_for(job.platform, content.kind)
    except KeyError:
        raise AppError(
            "platform_not_available", "未知平台，暂不支持自动发布", 409
        ) from None

    if capability == CAP_MANUAL:
        raise AppError("manual_export_required", "该平台请使用导出流程", 409)
    if capability != CAP_AUTO:
        raise AppError(
            "platform_not_available",
            spec.note or f"{spec.label}暂不支持自动发布",
            409,
        )

    try:
        adapter = adapter_for(job.platform)
    except PublishConfigError as exc:
        raise AppError("platform_not_available", str(exc), 409) from exc

    try:
        job = await execute_job(
            session,
            job.id,
            adapter,
            tenant_id=user.tenant_id,
            actor=user.email,
            storage=storage,
        )
    except InvalidTransition as exc:
        raise AppError(
            "invalid_state", "发布任务状态已变化，请刷新后重试", 409
        ) from exc
    except ContentChanged as exc:
        raise AppError("content_changed", str(exc), 409) from exc
    except PublishConfigError as exc:
        raise AppError("account_misconfigured", str(exc), 409) from exc
    except PublishError as exc:
        raise AppError("publish_failed", str(exc), 502) from exc
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="publish.execute",
        actor=user,
        resource_type="publish_job",
        resource_id=str(job.id),
        request=request,
    )
    return _out(job, content.kind)
```

`content_kind_label` 此时未使用，删除该导入，避免 ruff 报未使用。

- [ ] **Step 4: 运行测试**

Run: `cd backend && uv run pytest tests/test_publish_service.py tests/test_wechat_publish.py -q`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/publish.py backend/tests/test_publish_service.py
git commit -m "feat: route manual execute by platform capability"
```

---

### Task 7: worker 按平台与内容类型定时发布

**Files:**
- Modify: `backend/app/worker.py`
- Modify: `backend/tests/test_worker.py`

- [ ] **Step 1: 更新 worker 测试**

把 `backend/tests/test_worker.py` 的 `_seed_content`（第 81 行起）改为：

```python
async def _seed_content(
    session,
    *,
    tenant_id=DEFAULT_TENANT_ID,
    platform="wechat",
    kind="article",
    title="定时内容",
    body="正文",
):
    project = Project(tenant_id=tenant_id, title=f"项目-{uuid.uuid4().hex[:8]}")
    session.add(project)
    await session.flush()
    content = Content(
        tenant_id=tenant_id,
        project_id=project.id,
        platform=platform,
        kind=kind,
        title=title,
        current_version=1,
    )
    session.add(content)
    await session.flush()
    session.add(
        ContentVersion(
            tenant_id=tenant_id,
            content_id=content.id,
            version=1,
            title=title,
            body=body,
        )
    )
    await session.commit()
    return content
```

把 `test_list_due_jobs_only_approved_wechat_and_due` 全文替换为：

```python
async def test_list_due_jobs_only_auto_publish_pairs_and_due(worker_factory):
    now = datetime.now(UTC)
    async with worker_factory() as session:
        wechat = await _seed_content(session, platform="wechat")
        douyin = await _seed_content(
            session, platform="douyin", kind="video", title="抖音视频"
        )
        toutiao_video = await _seed_content(
            session, platform="toutiao", kind="video", title="头条视频"
        )
        toutiao_article = await _seed_content(
            session, platform="toutiao", kind="article", title="头条图文"
        )
        xiaohongshu = await _seed_content(
            session, platform="xiaohongshu", kind="note", title="小红书"
        )
        baijiahao = await _seed_content(
            session, platform="baijiahao", kind="article", title="百家号"
        )
        bilibili = await _seed_content(
            session, platform="bilibili", kind="video", title="B站"
        )
        session.add_all(
            [
                _job(
                    wechat,
                    key="wechat-due",
                    status="approved",
                    scheduled_at=now - timedelta(minutes=1),
                ),
                _job(
                    douyin,
                    key="douyin-due",
                    status="approved",
                    scheduled_at=now - timedelta(minutes=2),
                ),
                _job(
                    toutiao_video,
                    key="toutiao-video-due",
                    status="approved",
                    scheduled_at=now - timedelta(minutes=3),
                ),
                _job(
                    toutiao_article,
                    key="toutiao-article-pending",
                    status="approved",
                    scheduled_at=now - timedelta(minutes=4),
                ),
                _job(
                    xiaohongshu,
                    key="manual-export",
                    status="approved",
                    scheduled_at=now - timedelta(minutes=5),
                ),
                _job(
                    baijiahao,
                    key="baijiahao-pending",
                    status="approved",
                    scheduled_at=now - timedelta(minutes=6),
                ),
                _job(
                    bilibili,
                    key="bilibili-pending",
                    status="approved",
                    scheduled_at=now - timedelta(minutes=7),
                ),
                _job(
                    wechat,
                    key="wechat-future",
                    status="approved",
                    scheduled_at=now + timedelta(hours=1),
                ),
                _job(
                    wechat,
                    key="wechat-draft",
                    status="draft",
                    scheduled_at=now - timedelta(minutes=8),
                ),
                _job(
                    wechat,
                    key="wechat-no-schedule",
                    status="approved",
                    scheduled_at=None,
                ),
            ]
        )
        await session.commit()

        due = await list_due_jobs(session, now)

    assert [job.idempotency_key for job in due] == [
        "toutiao-article-pending",
        "manual-export",
        "baijiahao-pending",
        "bilibili-pending",
        "wechat-draft",
        "wechat-no-schedule",
    ] or [job.idempotency_key for job in due] == [
        "wechat-due",
        "douyin-due",
        "toutiao-video-due",
    ]
```

上面最后的断言是可执行前必须先修正的草稿：正确断言只允许三个自动发布任务，按 `scheduled_at` 升序。把 `assert [...]` 整段替换为：

```python
    assert [job.idempotency_key for job in due] == [
        "wechat-due",
        "douyin-due",
        "toutiao-video-due",
    ]
```

把 `FakeAdapter`（第 425-432 行）替换为：

```python
class FakeAdapter:
    platform = "wechat"
    capability = "auto"
    calls = 0

    async def publish(self, *, account, content, storage):
        self.calls += 1
        return PublishResult(external_id="draft-42")
```

把 `FailingAdapter.publish` 签名（第 750 行）改为：

```python
    async def publish(self, *, account, content, storage):
```

该函数体内所有 `body` 改为 `content.body`。

把测试文件中的 `adapter_factory=lambda: adapter` 全部替换为：

```python
adapter_resolver=lambda _platform: adapter
```

可以执行：

```bash
cd backend && perl -pi -e 's/adapter_factory=lambda: adapter/adapter_resolver=lambda _platform: adapter/g' tests/test_worker.py
```

把 `test_execute_job_treats_cleared_schedule_as_not_due` 中的 `execute_job(...)` 调用加入存储参数：

```python
                storage=_FakeStorage(),
```

并在该文件顶部（`FakeAdapter` 之前）加入：

```python
class _FakeStorage:
    def read_bytes(self, project_id, relpath):
        raise AssertionError("测试适配器不应读取媒体文件")
```

在文件末尾追加：

```python
async def test_publish_due_jobs_resolves_adapter_per_platform(worker_factory):
    now = datetime.now(UTC)
    async with worker_factory() as session:
        wechat = await _seed_content(session, platform="wechat")
        douyin = await _seed_content(
            session, platform="douyin", kind="video"
        )
        wechat_account = await _seed_account(session)
        douyin_account = await _seed_account(session, platform="douyin")
        session.add_all(
            [
                _job(
                    wechat,
                    key="resolve-wechat",
                    status="approved",
                    scheduled_at=now - timedelta(minutes=1),
                    account_id=wechat_account.id,
                ),
                _job(
                    douyin,
                    key="resolve-douyin",
                    status="approved",
                    scheduled_at=now - timedelta(minutes=2),
                    account_id=douyin_account.id,
                ),
            ]
        )
        await session.commit()

    resolved: list[str] = []

    def resolver(platform: str):
        resolved.append(platform)
        return FakeAdapter()

    executed = await publish_due_jobs(
        session_factory=worker_factory,
        adapter_resolver=resolver,
        storage=_FakeStorage(),
        now=now,
    )

    assert executed == 2
    assert resolved == ["douyin", "wechat"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_worker.py -q`

Expected: FAIL，`publish_due_jobs` 不接受 `adapter_resolver`/`storage`。

- [ ] **Step 3: 改造到期扫描**

修改 `backend/app/worker.py` 导入区：

```python
from sqlalchemy import and_, or_, select
```

```python
from app.models.project import Content
from app.publish.adapters import adapter_for
from app.publish.base import PublisherAdapter, PublishConfigError, PublishError
from app.publish.registry import AUTO_PUBLISH_PAIRS
```

删除 `from app.publish.wechat import WeChatMPAdapter`。

把文件顶部 docstring 改为：

```python
"""arq 定时发布 worker。

定时执行覆盖「平台 + 内容类型」能力为自动发布的任务：公众号图文、
抖音视频、头条视频。人工导出平台与待接入平台不进入扫描。worker 作为
系统进程跨租户扫描到期任务是有意为之。
"""
```

把 `BLOCKED_ERROR_CODES`（第 47-53 行）改为：

```python
BLOCKED_ERROR_CODES = {
    "too_many_attempts",
    "empty_content",
    "account_missing",
    "account_inactive",
    "account_platform_mismatch",
    "platform_content_mismatch",
    "platform_not_available",
}
```

把 `list_due_jobs`（第 56-76 行）替换为：

```python
async def list_due_jobs(
    session: AsyncSession,
    now: datetime,
    limit: int = MAX_BATCH_SIZE,
) -> list[PublishJob]:
    auto_pairs = [
        and_(
            PublishJob.platform == platform,
            Content.kind == kind,
        )
        for platform, kind in AUTO_PUBLISH_PAIRS
    ]
    rows = (
        await session.execute(
            select(PublishJob)
            .join(
                Content,
                (Content.id == PublishJob.content_id)
                & (Content.tenant_id == PublishJob.tenant_id),
            )
            .join(Tenant, Tenant.id == PublishJob.tenant_id)
            .where(
                Tenant.status == "active",
                PublishJob.status == "approved",
                or_(*auto_pairs),
                PublishJob.scheduled_at.is_not(None),
                PublishJob.scheduled_at <= now,
            )
            .order_by(PublishJob.scheduled_at, PublishJob.created_at)
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)
```

把 `publish_due_jobs` 签名（第 164-170 行）改为：

```python
async def publish_due_jobs(
    _ctx: dict | None = None,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = SessionLocal,
    adapter_resolver: Callable[[str], PublisherAdapter] = adapter_for,
    storage: StorageBackend | None = None,
    now: datetime | None = None,
) -> int:
```

把 `targets = [(job.id, job.tenant_id) for job in due[:MAX_BATCH_SIZE]]`（第 188 行）改为：

```python
        targets = [
            (job.id, job.tenant_id, job.platform)
            for job in due[:MAX_BATCH_SIZE]
        ]
```

把 `adapter = adapter_factory()`（第 190 行）改为：

```python
    backend = storage or get_storage()
```

把任务循环（第 191-203 行）改为：

```python
    executed = 0
    for job_id, tenant_id, platform in targets:
        # 每个任务独立会话；状态机保证并发/重复扫描时只有一个执行者成功
        async with session_factory() as session:
            try:
                adapter = adapter_resolver(platform)
            except PublishConfigError as exc:
                await _fail_blocked_job(
                    session, job_id, tenant_id=tenant_id, message=str(exc)
                )
                continue
            try:
                await execute_job(
                    session,
                    job_id,
                    adapter,
                    tenant_id=tenant_id,
                    actor="scheduler",
                    storage=backend,
                    require_due=current,
                )
                executed += 1
                await record_best_effort(
                    session,
                    tenant_id=tenant_id,
                    action="publish.auto_execute",
                    actor="scheduler",
                    resource_type="publish_job",
                    resource_id=str(job_id),
                )
```

其余异常分支保持不变。

- [ ] **Step 4: 运行 worker 与发布测试**

Run: `cd backend && uv run pytest tests/test_worker.py tests/test_publish_service.py tests/test_publish_context.py -q`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/worker.py backend/tests/test_worker.py
git commit -m "feat: schedule auto publish per platform and content kind"
```

---

### Task 8: 前端平台模块与类型

**Files:**
- Create: `web/src/platforms.ts`
- Modify: `web/src/types.ts:1-4,55-65,75-95,278-300,303-370`
- Test: `web/src/__tests__/platforms.test.ts`
- Modify: `web/src/__tests__/publish-actions.test.ts`

- [ ] **Step 1: 写失败测试**

创建 `web/src/__tests__/platforms.test.ts`：

```ts
import { describe, expect, it } from "vitest";
import {
  CAPABILITY_LABELS,
  FALLBACK_PLATFORMS,
  accountsForPlatform,
  capabilityFor,
  specFor,
} from "../platforms";

describe("平台规格", () => {
  it("内置 9 个平台并按固定顺序排列", () => {
    expect(FALLBACK_PLATFORMS.map((spec) => spec.key)).toEqual([
      "wechat",
      "douyin",
      "toutiao",
      "xiaohongshu",
      "zhihu",
      "kuaishou",
      "channels",
      "baijiahao",
      "bilibili",
    ]);
    expect(specFor("toutiao")?.label).toBe("头条");
    expect(specFor("unknown")).toBeUndefined();
  });

  it("能力按内容类型查询，未知类型为待接入", () => {
    const toutiao = specFor("toutiao")!;
    expect(capabilityFor(toutiao, "video")).toBe("auto_publish");
    expect(capabilityFor(toutiao, "article")).toBe("pending");
    expect(capabilityFor(toutiao, "audio")).toBe("pending");
  });

  it("账号按平台与启用状态过滤", () => {
    const accounts = [
      { id: "1", platform: "wechat", status: "active" },
      { id: "2", platform: "wechat", status: "revoked" },
      { id: "3", platform: "douyin", status: "active" },
    ];
    expect(accountsForPlatform(accounts, "wechat").map((a) => a.id)).toEqual([
      "1",
    ]);
    expect(accountsForPlatform(accounts, "douyin").map((a) => a.id)).toEqual([
      "3",
    ]);
  });

  it("能力文案齐全", () => {
    expect(CAPABILITY_LABELS.auto_publish).toBe("自动发布");
    expect(CAPABILITY_LABELS.manual_export).toBe("人工导出");
    expect(CAPABILITY_LABELS.pending).toBe("待接入");
  });
});
```

把 `web/src/__tests__/publish-actions.test.ts` 全文替换为：

```ts
import { describe, expect, it } from "vitest";
import {
  actionFor,
  canCancel,
  canReject,
  canReschedule,
  type Job,
  type JobActionKind,
} from "../types";

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: "j1",
    content_id: "c1",
    content_version: 1,
    content_kind: "article",
    platform: "wechat",
    account_id: null,
    status: "draft",
    scheduled_at: null,
    published_at: null,
    external_id: "",
    external_url: "",
    export_path: "",
    attempts: 0,
    error: "",
    checks: {},
    created_at: "",
    updated_at: "",
    ...overrides,
  };
}

type Expected = { action: JobActionKind; label: string } | null;

describe("actionFor", () => {
  it.each<[string, string, string, Expected]>([
    ["draft", "wechat", "article", { action: "precheck", label: "发布前检查" }],
    [
      "precheck_failed",
      "wechat",
      "article",
      { action: "precheck", label: "发布前检查" },
    ],
    [
      "awaiting_review",
      "wechat",
      "article",
      { action: "approve", label: "人工通过" },
    ],
    [
      "approved",
      "wechat",
      "article",
      { action: "execute", label: "发布到公众号草稿" },
    ],
    [
      "approved",
      "douyin",
      "video",
      { action: "execute", label: "发布到抖音" },
    ],
    [
      "approved",
      "toutiao",
      "video",
      { action: "execute", label: "发布到头条" },
    ],
    ["approved", "toutiao", "article", null],
    ["approved", "baijiahao", "article", null],
    ["approved", "bilibili", "video", null],
    [
      "approved",
      "xiaohongshu",
      "note",
      { action: "export", label: "导出素材包" },
    ],
    ["approved", "zhihu", "article", { action: "export", label: "导出素材包" }],
    [
      "approved",
      "kuaishou",
      "video",
      { action: "export", label: "导出素材包" },
    ],
    [
      "approved",
      "channels",
      "article",
      { action: "export", label: "导出素材包" },
    ],
    [
      "export_failed",
      "xiaohongshu",
      "note",
      { action: "retry_export", label: "重试导出" },
    ],
    ["export_failed", "wechat", "article", null],
    [
      "publish_failed",
      "wechat",
      "article",
      { action: "retry", label: "重试发布" },
    ],
    [
      "publish_failed",
      "douyin",
      "video",
      { action: "retry", label: "重试发布" },
    ],
    ["publish_failed", "xiaohongshu", "note", null],
    ["published", "wechat", "article", null],
    ["cancelled", "wechat", "article", null],
    ["exported", "xiaohongshu", "note", null],
    ["exporting", "xiaohongshu", "note", null],
    ["publishing", "wechat", "article", null],
    ["approved", "unknown", "article", null],
  ])(
    "status %s on %s/%s offers %o",
    (status, platform, contentKind, expected) => {
      expect(
        actionFor(job({ status, platform, content_kind: contentKind })),
      ).toEqual(expected);
    },
  );
});

describe("canCancel", () => {
  it.each<[string, boolean]>([
    ["draft", true],
    ["precheck_failed", true],
    ["awaiting_review", true],
    ["approved", true],
    ["export_failed", true],
    ["publish_failed", true],
    ["prechecking", false],
    ["exporting", false],
    ["exported", false],
    ["publishing", false],
    ["published", false],
    ["cancelled", false],
  ])("status %s -> %s", (status, expected) => {
    expect(canCancel(job({ status }))).toBe(expected);
  });
});

describe("canReject", () => {
  it.each<[string, boolean]>([
    ["awaiting_review", true],
    ["draft", false],
    ["approved", false],
    ["published", false],
  ])("status %s -> %s", (status, expected) => {
    expect(canReject(job({ status }))).toBe(expected);
  });
});

describe("canReschedule", () => {
  it.each<[string, boolean]>([
    ["draft", true],
    ["prechecking", true],
    ["precheck_failed", true],
    ["awaiting_review", true],
    ["approved", true],
    ["exporting", false],
    ["exported", false],
    ["publishing", false],
    ["published", false],
    ["cancelled", false],
  ])("status %s -> %s", (status, expected) => {
    expect(canReschedule(job({ status }))).toBe(expected);
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd web && npm test -- src/__tests__/platforms.test.ts src/__tests__/publish-actions.test.ts`

Expected: FAIL，`platforms.ts` 不存在，`Job` 缺少 `content_kind`。

- [ ] **Step 3: 创建平台模块**

创建 `web/src/platforms.ts`：

```ts
import { useQuery } from "@tanstack/react-query";
import { api } from "./client";

export type Capability = "auto_publish" | "manual_export" | "pending";

export interface CredentialField {
  key: string;
  label: string;
  secret: boolean;
  required: boolean;
  placeholder: string;
}

export interface PlatformSpec {
  key: string;
  label: string;
  capability: Capability;
  content_capabilities: Record<string, Capability>;
  credential_fields: CredentialField[];
  meta_fields: string[];
  note: string;
}

export const FALLBACK_PLATFORMS: PlatformSpec[] = [
  {
    key: "wechat",
    label: "公众号",
    capability: "auto_publish",
    content_capabilities: { article: "auto_publish" },
    credential_fields: [
      {
        key: "app_id",
        label: "AppID",
        secret: false,
        required: true,
        placeholder: "公众号 AppID",
      },
      {
        key: "app_secret",
        label: "AppSecret",
        secret: true,
        required: true,
        placeholder: "公众号 AppSecret",
      },
    ],
    meta_fields: [
      "author",
      "thumb_media_id",
      "need_open_comment",
      "only_fans_can_comment",
    ],
    note: "使用公众号草稿接口，发布前需配置封面素材 ID",
  },
  {
    key: "douyin",
    label: "抖音",
    capability: "auto_publish",
    content_capabilities: { video: "auto_publish" },
    credential_fields: [
      {
        key: "client_key",
        label: "Client Key",
        secret: false,
        required: true,
        placeholder: "",
      },
      {
        key: "client_secret",
        label: "Client Secret",
        secret: true,
        required: true,
        placeholder: "",
      },
      {
        key: "open_id",
        label: "Open ID",
        secret: false,
        required: true,
        placeholder: "",
      },
      {
        key: "refresh_token",
        label: "Refresh Token",
        secret: true,
        required: true,
        placeholder: "",
      },
    ],
    meta_fields: [],
    note: "使用抖音开放平台视频发布接口",
  },
  {
    key: "toutiao",
    label: "头条",
    capability: "auto_publish",
    content_capabilities: {
      video: "auto_publish",
      article: "pending",
    },
    credential_fields: [
      {
        key: "client_key",
        label: "Client Key",
        secret: false,
        required: true,
        placeholder: "",
      },
      {
        key: "client_secret",
        label: "Client Secret",
        secret: true,
        required: true,
        placeholder: "",
      },
      {
        key: "open_id",
        label: "Open ID",
        secret: false,
        required: true,
        placeholder: "",
      },
      {
        key: "refresh_token",
        label: "Refresh Token",
        secret: true,
        required: true,
        placeholder: "",
      },
    ],
    meta_fields: ["author"],
    note: "视频走开放平台接口；图文接口已下线或未获批",
  },
  {
    key: "xiaohongshu",
    label: "小红书",
    capability: "manual_export",
    content_capabilities: {
      note: "manual_export",
      article: "manual_export",
    },
    credential_fields: [],
    meta_fields: [],
    note: "导出素材包后手动发布",
  },
  {
    key: "zhihu",
    label: "知乎",
    capability: "manual_export",
    content_capabilities: { article: "manual_export" },
    credential_fields: [],
    meta_fields: [],
    note: "导出素材包后手动发布",
  },
  {
    key: "kuaishou",
    label: "快手",
    capability: "manual_export",
    content_capabilities: {
      video: "manual_export",
      note: "manual_export",
    },
    credential_fields: [],
    meta_fields: [],
    note: "导出素材包后手动发布",
  },
  {
    key: "channels",
    label: "视频号",
    capability: "manual_export",
    content_capabilities: {
      video: "manual_export",
      article: "manual_export",
    },
    credential_fields: [],
    meta_fields: [],
    note: "导出素材包后手动发布",
  },
  {
    key: "baijiahao",
    label: "百家号",
    capability: "pending",
    content_capabilities: { article: "pending" },
    credential_fields: [
      {
        key: "app_id",
        label: "AppID",
        secret: false,
        required: true,
        placeholder: "",
      },
      {
        key: "app_token",
        label: "App Token",
        secret: true,
        required: true,
        placeholder: "",
      },
    ],
    meta_fields: [],
    note: "官方发布接口尚未接入，可先保存凭据",
  },
  {
    key: "bilibili",
    label: "B站",
    capability: "pending",
    content_capabilities: { video: "pending" },
    credential_fields: [],
    meta_fields: [],
    note: "暂无官方发布接口，可先保存账号",
  },
];

export const CAPABILITY_LABELS: Record<Capability, string> = {
  auto_publish: "自动发布",
  manual_export: "人工导出",
  pending: "待接入",
};

export function capabilityFor(
  spec: PlatformSpec,
  contentKind: string,
): Capability {
  return spec.content_capabilities[contentKind] ?? "pending";
}

export function specFor(
  key: string,
  specs: PlatformSpec[] = FALLBACK_PLATFORMS,
): PlatformSpec | undefined {
  return specs.find((spec) => spec.key === key);
}

export function accountsForPlatform<
  T extends { platform: string; status: string },
>(accounts: T[], platform: string): T[] {
  return accounts.filter(
    (account) => account.platform === platform && account.status === "active",
  );
}

export function usePlatforms() {
  return useQuery({
    queryKey: ["platforms"],
    queryFn: () => api<PlatformSpec[]>("/accounts/platforms"),
    staleTime: 5 * 60 * 1000,
    placeholderData: FALLBACK_PLATFORMS,
  });
}
```

- [ ] **Step 4: 扩展前端类型**

在 `web/src/types.ts` 顶部加入：

```ts
import {
  capabilityFor,
  specFor,
  type Capability,
} from "./platforms";
```

把 `Account`（第 24-30 行）替换为：

```ts
export interface Account {
  id: string;
  platform: string;
  name: string;
  status: string;
  credential_masked: string;
  meta: Record<string, unknown>;
  capability: string;
  capability_note: string;
}
```

把 `Job`（第 75-95 行）中的 `content_version` 后加入：

```ts
  content_kind: string;
```

把 `PLATFORMS`（第 278-290 行）替换为：

```ts
export const PLATFORMS = [
  ["wechat", "公众号"],
  ["douyin", "抖音"],
  ["toutiao", "头条"],
  ["xiaohongshu", "小红书"],
  ["zhihu", "知乎"],
  ["kuaishou", "快手"],
  ["channels", "视频号"],
  ["baijiahao", "百家号"],
  ["bilibili", "B站"],
] as const;
```

把 `actionFor`（第 303-336 行）替换为：

```ts
const EXECUTE_LABELS: Record<string, string> = {
  wechat: "发布到公众号草稿",
  douyin: "发布到抖音",
  toutiao: "发布到头条",
};

export function actionFor(
  job: Pick<Job, "status" | "platform" | "content_kind">,
): { action: JobActionKind; label: string } | null {
  if (job.status === "draft" || job.status === "precheck_failed") {
    return { action: "precheck", label: "发布前检查" };
  }
  if (job.status === "awaiting_review") {
    return { action: "approve", label: "人工通过" };
  }

  const spec = specFor(job.platform);
  const capability: Capability = spec
    ? capabilityFor(spec, job.content_kind)
    : "pending";

  if (job.status === "approved") {
    if (capability === "auto_publish") {
      return {
        action: "execute",
        label:
          EXECUTE_LABELS[job.platform] ??
          `发布到${spec?.label ?? job.platform}`,
      };
    }
    if (capability === "manual_export") {
      return { action: "export", label: "导出素材包" };
    }
  }
  if (job.status === "export_failed" && capability === "manual_export") {
    return { action: "retry_export", label: "重试导出" };
  }
  if (job.status === "publish_failed" && capability === "auto_publish") {
    return { action: "retry", label: "重试发布" };
  }
  return null;
}
```

`isManualExportPlatform` 保留现有实现，`MANUAL_EXPORT_PLATFORMS` 仍为四个平台。

- [ ] **Step 5: 运行测试与类型检查**

Run: `cd web && npm test -- src/__tests__/platforms.test.ts src/__tests__/publish-actions.test.ts`

Expected: 全部 PASS。

Run: `cd web && npx tsc -b`

Expected: 通过。

- [ ] **Step 6: 提交**

```bash
git add web/src/platforms.ts web/src/types.ts web/src/__tests__/platforms.test.ts web/src/__tests__/publish-actions.test.ts
git commit -m "feat: add frontend platform registry and capability actions"
```

---

### Task 9: 账号页动态表单与分组列表

**Files:**
- Modify: `web/src/pages/Accounts.tsx`（全文重写）

- [ ] **Step 1: 重写账号页**

把 `web/src/pages/Accounts.tsx` 全文替换为：

```tsx
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { useAuth } from "../auth-context";
import { api } from "../client";
import {
  CAPABILITY_LABELS,
  FALLBACK_PLATFORMS,
  type Capability,
  type CredentialField,
  type PlatformSpec,
  specFor,
  usePlatforms,
} from "../platforms";
import { errorText, type Account } from "../types";

type Feedback = { kind: "ok" | "error"; text: string };
type RowFeedback = Feedback & { id: string };

function defaultMeta(
  platform: string,
): Record<string, string | number | boolean> {
  if (platform === "wechat") {
    return {
      author: "",
      thumb_media_id: "",
      need_open_comment: false,
      only_fans_can_comment: false,
    };
  }
  if (platform === "toutiao") return { author: "" };
  return {};
}

function statusLabel(status: string): string {
  if (status === "active") return "已授权";
  if (status === "revoked") return "已停用";
  return status;
}

function capabilityClass(capability: string): string {
  if (capability === "auto_publish") {
    return "bg-indigo-900 text-indigo-300";
  }
  if (capability === "manual_export") {
    return "bg-slate-800 text-slate-300";
  }
  return "bg-slate-800 text-slate-400";
}

function credentialPayload(
  fields: CredentialField[],
  values: Record<string, string>,
): Record<string, string> {
  const payload: Record<string, string> = {};
  for (const field of fields) {
    const value = (values[field.key] ?? "").trim();
    if (value) payload[field.key] = value;
  }
  return payload;
}

function metaPayload(
  platform: string,
  meta: Record<string, string | number | boolean>,
): Record<string, string | number> {
  if (platform === "wechat") {
    const payload: Record<string, string | number> = {
      thumb_media_id: String(meta.thumb_media_id ?? "").trim(),
      need_open_comment: meta.need_open_comment ? 1 : 0,
      only_fans_can_comment: meta.only_fans_can_comment ? 1 : 0,
    };
    const author = String(meta.author ?? "").trim();
    if (author) payload.author = author;
    return payload;
  }
  if (platform === "toutiao") {
    const author = String(meta.author ?? "").trim();
    return author ? { author } : {};
  }
  return {};
}

export default function Accounts() {
  const qc = useQueryClient();
  const { isAdmin } = useAuth();
  const platformsQuery = usePlatforms();
  const specs = platformsQuery.data ?? FALLBACK_PLATFORMS;
  const [platform, setPlatform] = useState("wechat");
  const spec = specFor(platform, specs) ?? FALLBACK_PLATFORMS[0];
  const [name, setName] = useState("");
  const [credentials, setCredentials] = useState<Record<string, string>>({});
  const [meta, setMeta] = useState(defaultMeta("wechat"));
  const [message, setMessage] = useState<Feedback | null>(null);
  const [rowFeedback, setRowFeedback] = useState<RowFeedback | null>(null);
  const [editing, setEditing] = useState<{ id: string; name: string } | null>(
    null,
  );
  const [credentialFor, setCredentialFor] = useState("");
  const [credentialForm, setCredentialForm] = useState<Record<string, string>>(
    {},
  );
  const [revokingId, setRevokingId] = useState("");

  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: () => api<Account[]>("/accounts"),
  });

  const create = useMutation({
    mutationFn: (payload: {
      platform: string;
      name: string;
      credential: Record<string, string>;
      meta: Record<string, string | number>;
    }) =>
      api<Account>("/accounts", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["accounts"] });
      setName("");
      setCredentials({});
      setMeta(defaultMeta(platform));
      setMessage({ kind: "ok", text: "账号已保存" });
    },
    onError: (err) => setMessage({ kind: "error", text: errorText(err) }),
  });

  const rename = useMutation({
    mutationFn: ({ id, newName }: { id: string; newName: string }) =>
      api<Account>(`/accounts/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ name: newName }),
      }),
    onSuccess: (_account, variables) => {
      qc.invalidateQueries({ queryKey: ["accounts"] });
      setEditing(null);
      setRowFeedback({
        id: variables.id,
        kind: "ok",
        text: "名称已更新",
      });
    },
    onError: (err, variables) =>
      setRowFeedback({
        id: variables.id,
        kind: "error",
        text: errorText(err),
      }),
  });

  const updateCredential = useMutation({
    mutationFn: ({
      id,
      credential,
    }: {
      id: string;
      credential: Record<string, string>;
    }) =>
      api<Account>(`/accounts/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ credential }),
      }),
    onSuccess: (_account, variables) => {
      qc.invalidateQueries({ queryKey: ["accounts"] });
      setCredentialFor("");
      setCredentialForm({});
      setRowFeedback({
        id: variables.id,
        kind: "ok",
        text: "凭据已更新",
      });
    },
    onError: (err, variables) =>
      setRowFeedback({
        id: variables.id,
        kind: "error",
        text: errorText(err),
      }),
  });

  const revoke = useMutation({
    mutationFn: (account: Account) =>
      api<void>(`/accounts/${account.id}`, { method: "DELETE" }),
    onSuccess: (_result, account) => {
      qc.invalidateQueries({ queryKey: ["accounts"] });
      if (editing?.id === account.id) setEditing(null);
      if (credentialFor === account.id) setCredentialFor("");
      setRevokingId("");
      setRowFeedback({
        id: account.id,
        kind: "ok",
        text: "账号已停用，凭据已销毁",
      });
    },
    onError: (err, account) =>
      setRowFeedback({
        id: account.id,
        kind: "error",
        text: errorText(err),
      }),
  });

  function selectPlatform(next: string) {
    setPlatform(next);
    setCredentials({});
    setMeta(defaultMeta(next));
    setMessage(null);
  }

  function submitCreate(event: FormEvent) {
    event.preventDefault();
    const trimmedName = name.trim();
    if (!trimmedName) {
      setMessage({ kind: "error", text: "请填写账号名称" });
      return;
    }
    for (const field of spec.credential_fields) {
      if (field.required && !(credentials[field.key] ?? "").trim()) {
        setMessage({ kind: "error", text: `请填写 ${field.label}` });
        return;
      }
    }
    if (
      platform === "wechat" &&
      !String(meta.thumb_media_id ?? "").trim()
    ) {
      setMessage({ kind: "error", text: "请填写封面素材 ID" });
      return;
    }
    setMessage(null);
    create.mutate({
      platform,
      name: trimmedName,
      credential: credentialPayload(spec.credential_fields, credentials),
      meta: metaPayload(platform, meta),
    });
  }

  function submitRename(event: FormEvent) {
    event.preventDefault();
    if (!editing) return;
    const nextName = editing.name.trim();
    if (!nextName) {
      setRowFeedback({ id: editing.id, kind: "error", text: "名称不能为空" });
      return;
    }
    setRowFeedback(null);
    rename.mutate({ id: editing.id, newName: nextName });
  }

  function submitCredential(
    event: FormEvent,
    accountSpec: PlatformSpec,
  ) {
    event.preventDefault();
    if (!credentialFor) return;
    for (const field of accountSpec.credential_fields) {
      if (field.required && !(credentialForm[field.key] ?? "").trim()) {
        setRowFeedback({
          id: credentialFor,
          kind: "error",
          text: `请填写 ${field.label}`,
        });
        return;
      }
    }
    setRowFeedback(null);
    updateCredential.mutate({
      id: credentialFor,
      credential: credentialPayload(
        accountSpec.credential_fields,
        credentialForm,
      ),
    });
  }

  function revokeAccount(account: Account) {
    setRowFeedback(null);
    setRevokingId("");
    revoke.mutate(account);
  }

  const groups = specs
    .map((groupSpec) => ({
      spec: groupSpec,
      items: (accounts.data ?? []).filter(
        (account) => account.platform === groupSpec.key,
      ),
    }))
    .filter((group) => group.items.length > 0);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">发布账号</h1>
        <p className="mt-1 text-sm text-slate-400">
          同一平台可添加多个账号；凭据加密存储，响应只返回掩码。
        </p>
      </div>

      <div className="grid gap-6 lg:grid-cols-[400px_1fr]">
        {isAdmin && (
          <form
            className="space-y-3 self-start rounded-xl border border-slate-800 bg-slate-900 p-5"
            onSubmit={submitCreate}
          >
            <h2 className="font-medium">添加账号</h2>
            <label className="block space-y-1 text-sm text-slate-400">
              <span>平台</span>
              <select
                className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                value={platform}
                onChange={(e) => selectPlatform(e.target.value)}
              >
                {specs.map((item) => (
                  <option key={item.key} value={item.key}>
                    {item.label} · {CAPABILITY_LABELS[item.capability]}
                  </option>
                ))}
              </select>
            </label>
            <p className="flex items-center gap-2 text-xs text-slate-500">
              <span
                className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${capabilityClass(spec.capability)}`}
              >
                {CAPABILITY_LABELS[spec.capability]}
              </span>
              {spec.note}
            </p>

            <label className="block space-y-1 text-sm text-slate-400">
              <span>账号名称 *</span>
              <input
                className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                maxLength={100}
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="例如：主号"
              />
            </label>

            {spec.credential_fields.map((field) => (
              <label
                key={field.key}
                className="block space-y-1 text-sm text-slate-400"
              >
                <span>
                  {field.label}
                  {field.required ? " *" : ""}
                </span>
                <input
                  className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                  type={field.secret ? "password" : "text"}
                  required={field.required}
                  value={credentials[field.key] ?? ""}
                  onChange={(e) =>
                    setCredentials((prev) => ({
                      ...prev,
                      [field.key]: e.target.value,
                    }))
                  }
                  placeholder={field.placeholder}
                />
              </label>
            ))}

            {platform === "wechat" && (
              <>
                <label className="block space-y-1 text-sm text-slate-400">
                  <span>封面素材 ID *</span>
                  <input
                    className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                    maxLength={200}
                    required
                    value={String(meta.thumb_media_id ?? "")}
                    onChange={(e) =>
                      setMeta({ ...meta, thumb_media_id: e.target.value })
                    }
                    placeholder="thumb_media_id"
                  />
                </label>
                <label className="block space-y-1 text-sm text-slate-400">
                  <span>作者（可选）</span>
                  <input
                    className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                    maxLength={64}
                    value={String(meta.author ?? "")}
                    onChange={(e) =>
                      setMeta({ ...meta, author: e.target.value })
                    }
                  />
                </label>
                <label className="flex items-center gap-2 text-sm text-slate-300">
                  <input
                    type="checkbox"
                    checked={Boolean(meta.need_open_comment)}
                    onChange={(e) =>
                      setMeta({
                        ...meta,
                        need_open_comment: e.target.checked,
                      })
                    }
                  />
                  允许读者评论
                </label>
                <label className="flex items-center gap-2 text-sm text-slate-300">
                  <input
                    type="checkbox"
                    checked={Boolean(meta.only_fans_can_comment)}
                    onChange={(e) =>
                      setMeta({
                        ...meta,
                        only_fans_can_comment: e.target.checked,
                      })
                    }
                  />
                  仅关注者可评论
                </label>
              </>
            )}

            {platform === "toutiao" && (
              <label className="block space-y-1 text-sm text-slate-400">
                <span>作者（可选）</span>
                <input
                  className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                  maxLength={64}
                  value={String(meta.author ?? "")}
                  onChange={(e) =>
                    setMeta({ ...meta, author: e.target.value })
                  }
                />
              </label>
            )}

            {spec.credential_fields.length === 0 && (
              <p className="text-xs text-slate-500">
                {spec.capability === "manual_export"
                  ? "该平台使用素材包导出，无需填写凭据。"
                  : "该平台接口待接入，可先保存账号。"}
              </p>
            )}

            {message && (
              <p
                className={
                  message.kind === "ok"
                    ? "text-sm text-emerald-400"
                    : "text-sm text-red-400"
                }
              >
                {message.text}
              </p>
            )}
            <button
              className="w-full rounded bg-indigo-600 px-4 py-2 font-medium disabled:opacity-50"
              disabled={create.isPending}
            >
              {create.isPending ? "保存中…" : "保存账号"}
            </button>
          </form>
        )}

        <section className="space-y-5">
          <h2 className="font-medium">已保存账号</h2>
          {accounts.isLoading && (
            <p className="text-sm text-slate-400">加载中…</p>
          )}
          {accounts.isError && (
            <p className="text-sm text-red-400">
              {errorText(accounts.error)}
            </p>
          )}
          {accounts.isSuccess && groups.length === 0 && (
            <p className="rounded-xl border border-slate-800 bg-slate-900 p-6 text-sm text-slate-400">
              还没有账号，先在左侧添加。
            </p>
          )}
          {groups.map((group) => (
            <div key={group.spec.key} className="space-y-2">
              <h3 className="flex items-center gap-2 text-sm text-slate-400">
                {group.spec.label}
                <span className="text-xs text-slate-500">
                  {group.items.length} 个
                </span>
              </h3>
              {group.items.map((account) => {
                const accountSpec =
                  specFor(account.platform, specs) ?? group.spec;
                const active = account.status === "active";
                const feedback =
                  rowFeedback?.id === account.id ? rowFeedback : null;
                return (
                  <article
                    key={account.id}
                    className="rounded-xl border border-slate-800 bg-slate-900 p-4"
                  >
                    <div className="flex flex-wrap items-start justify-between gap-2">
                      <div>
                        <h4 className="font-medium">{account.name}</h4>
                        <p className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-500">
                          <span>{group.spec.label}</span>
                          <span
                            className={`inline-flex rounded-full px-2 py-0.5 font-medium ${capabilityClass(account.capability)}`}
                          >
                            {CAPABILITY_LABELS[
                              account.capability as Capability
                            ] ?? account.capability}
                          </span>
                          <span>{account.capability_note}</span>
                        </p>
                      </div>
                      <span
                        className={
                          active
                            ? "text-xs text-emerald-400"
                            : "text-xs text-slate-500"
                        }
                      >
                        {statusLabel(account.status)}
                      </span>
                    </div>
                    <p className="mt-2 text-xs text-slate-500">
                      凭据：
                      <span className="ml-1 font-mono">
                        {account.credential_masked === "{}"
                          ? "无需凭据或已销毁"
                          : account.credential_masked}
                      </span>
                    </p>

                    {active && isAdmin && (
                      <div className="mt-3 flex flex-wrap gap-2">
                        <button
                          type="button"
                          className="rounded border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:border-slate-600"
                          onClick={() => {
                            setEditing({
                              id: account.id,
                              name: account.name,
                            });
                            setCredentialFor("");
                            setRowFeedback(null);
                          }}
                        >
                          重命名
                        </button>
                        {accountSpec.credential_fields.length > 0 && (
                          <button
                            type="button"
                            className="rounded border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:border-slate-600"
                            onClick={() => {
                              setCredentialFor(account.id);
                              setCredentialForm({});
                              setEditing(null);
                              setRowFeedback(null);
                            }}
                          >
                            更新凭据
                          </button>
                        )}
                        {revokingId === account.id ? (
                          <span className="flex flex-wrap items-center gap-2 rounded border border-red-900 px-3 py-1.5 text-sm text-red-300">
                            停用后凭据销毁且无法恢复
                            <button
                              type="button"
                              className="rounded bg-red-700 px-2 py-1 text-xs font-medium text-white hover:bg-red-600 disabled:opacity-50"
                              disabled={revoke.isPending}
                              onClick={() => revokeAccount(account)}
                            >
                              {revoke.isPending ? "停用中…" : "确认停用"}
                            </button>
                            <button
                              type="button"
                              className="rounded px-2 py-1 text-xs text-slate-400 hover:text-slate-200"
                              onClick={() => setRevokingId("")}
                            >
                              取消
                            </button>
                          </span>
                        ) : (
                          <button
                            type="button"
                            className="rounded border border-red-900 px-3 py-1.5 text-sm text-red-400 hover:border-red-800 disabled:opacity-50"
                            onClick={() => {
                              setRevokingId(account.id);
                              setEditing(null);
                              setCredentialFor("");
                              setRowFeedback(null);
                            }}
                          >
                            停用
                          </button>
                        )}
                      </div>
                    )}

                    {editing?.id === account.id && (
                      <form
                        className="mt-3 flex flex-wrap items-end gap-2 border-t border-slate-800 pt-3"
                        onSubmit={submitRename}
                      >
                        <label className="min-w-48 flex-1 space-y-1 text-sm text-slate-400">
                          <span>新名称</span>
                          <input
                            className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                            maxLength={100}
                            required
                            value={editing.name}
                            onChange={(e) =>
                              setEditing({
                                ...editing,
                                name: e.target.value,
                              })
                            }
                          />
                        </label>
                        <button
                          className="rounded bg-indigo-600 px-4 py-2 text-sm font-medium disabled:opacity-50"
                          disabled={rename.isPending}
                        >
                          {rename.isPending ? "保存中…" : "保存"}
                        </button>
                        <button
                          type="button"
                          className="rounded border border-slate-700 px-4 py-2 text-sm text-slate-300"
                          onClick={() => setEditing(null)}
                        >
                          取消
                        </button>
                      </form>
                    )}

                    {credentialFor === account.id && (
                      <form
                        className="mt-3 space-y-2 border-t border-slate-800 pt-3"
                        onSubmit={(event) =>
                          submitCredential(event, accountSpec)
                        }
                      >
                        <p className="text-xs text-slate-500">
                          更新凭据需填写该平台的全部凭据字段。
                        </p>
                        <div className="flex flex-wrap items-end gap-2">
                          {accountSpec.credential_fields.map((field) => (
                            <label
                              key={field.key}
                              className="min-w-48 flex-1 space-y-1 text-sm text-slate-400"
                            >
                              <span>{field.label}</span>
                              <input
                                className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                                type={field.secret ? "password" : "text"}
                                required={field.required}
                                value={credentialForm[field.key] ?? ""}
                                onChange={(e) =>
                                  setCredentialForm((prev) => ({
                                    ...prev,
                                    [field.key]: e.target.value,
                                  }))
                                }
                              />
                            </label>
                          ))}
                          <button
                            className="rounded bg-indigo-600 px-4 py-2 text-sm font-medium disabled:opacity-50"
                            disabled={updateCredential.isPending}
                          >
                            {updateCredential.isPending ? "保存中…" : "保存"}
                          </button>
                          <button
                            type="button"
                            className="rounded border border-slate-700 px-4 py-2 text-sm text-slate-300"
                            onClick={() => {
                              setCredentialFor("");
                              setCredentialForm({});
                            }}
                          >
                            取消
                          </button>
                        </div>
                      </form>
                    )}

                    {feedback && (
                      <p
                        className={
                          feedback.kind === "ok"
                            ? "mt-3 text-sm text-emerald-400"
                            : "mt-3 text-sm text-red-400"
                        }
                      >
                        {feedback.text}
                      </p>
                    )}
                  </article>
                );
              })}
            </div>
          ))}
        </section>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: 运行类型检查与构建**

Run: `cd web && npx tsc -b && npm test`

Expected: 类型检查通过，现有前端测试全部 PASS。

- [ ] **Step 3: 提交**

```bash
git add web/src/pages/Accounts.tsx
git commit -m "feat: render account form from platform capability matrix"
```

---

### Task 10: 发布页与项目详情按能力展示

**Files:**
- Modify: `web/src/pages/Publish.tsx`
- Modify: `web/src/pages/ProjectDetail.tsx`

- [ ] **Step 1: 改造发布页**

在 `web/src/pages/Publish.tsx` 的类型导入后加入：

```tsx
import {
  CAPABILITY_LABELS,
  capabilityFor,
  specFor,
} from "../platforms";
```

把 `successText` 中 `execute` 分支（第 24-25 行）改为：

```tsx
    case "execute":
      return "已提交到平台，请在平台后台确认";
```

在任务映射开始处（`const action = actionFor(job);` 之前）加入：

```tsx
          const spec = specFor(job.platform);
          const capability = spec
            ? capabilityFor(spec, job.content_kind)
            : "pending";
```

把 `missingAccount`（第 135 行）改为：

```tsx
          const missingAccount =
            capability === "auto_publish" && !job.account_id;
```

把标题行（第 153-158 行）改为：

```tsx
                  <h2 className="font-medium">
                    {platformLabel(job.platform)} ·{" "}
                    {contentKindLabel(job.content_kind)}
                    <span className="ml-2 text-sm font-normal text-slate-400">
                      v{job.content_version}
                    </span>
                  </h2>
```

把账号提示块（第 163-177 行）替换为：

```tsx
                  {capability === "auto_publish" && (
                    <p className="mt-1 text-xs text-slate-500">
                      {job.account_id ? (
                        `发布账号：${accountLabel}`
                      ) : (
                        <>
                          未关联发布账号，
                          <Link className="text-indigo-400" to="/accounts">
                            去授权
                          </Link>
                        </>
                      )}
                    </p>
                  )}
```

把“暂不支持自动发布或导出”提示（第 283-287 行）替换为：

```tsx
                  ) : capability === "pending" ? (
                    <span className="text-sm text-slate-500">
                      {spec?.note ?? "该平台暂不支持自动发布或导出"}
                    </span>
                  ) : null}
```

把定时说明（第 380 行）改为：

```tsx
                    按本机时区解释，提交时转换为 UTC；worker 到期后执行已审核的自动发布任务。
```

- [ ] **Step 2: 改造项目详情的内容平台与内容类型**

在 `web/src/pages/ProjectDetail.tsx` 的 `types` 导入中加入 `contentKindLabel`（若尚未导入），并在文件顶部加入：

```tsx
import {
  FALLBACK_PLATFORMS,
  accountsForPlatform,
  capabilityFor,
  specFor,
  usePlatforms,
} from "../platforms";
```

在状态区（第 52 行 `platform` 之后）加入：

```tsx
  const [kind, setKind] = useState("article");
```

在组件内、`accounts` 查询之后加入：

```tsx
  const platforms = usePlatforms();
  const platformSpecs = platforms.data ?? FALLBACK_PLATFORMS;
  const contentSpec = specFor(platform, platformSpecs);
  const contentKinds = Object.keys(
    contentSpec?.content_capabilities ?? { article: "pending" },
  );
```

把 `wechatAccounts`（第 110-112 行）删除，替换为平台选择函数：

```tsx
  function selectContentPlatform(next: string) {
    setPlatform(next);
    const spec = specFor(next, platformSpecs);
    const nextKind =
      Object.keys(spec?.content_capabilities ?? {})[0] ?? "article";
    setKind(nextKind);
  }
```

把 `submitContent`（第 240-248 行）改为：

```tsx
  function submitContent(event: FormEvent) {
    event.preventDefault();
    createContent.mutate({
      platform,
      kind,
      title: title.trim(),
      body,
    });
  }
```

把内容表单的平台下拉（第 328-341 行）替换为平台 + 内容类型两块：

```tsx
            <label className="block space-y-1 text-sm text-slate-400">
              <span>平台</span>
              <select
                className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                value={platform}
                onChange={(e) => selectContentPlatform(e.target.value)}
              >
                {platformSpecs.map((item) => (
                  <option key={item.key} value={item.key}>
                    {item.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="block space-y-1 text-sm text-slate-400">
              <span>内容类型</span>
              <select
                className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                value={kind}
                onChange={(e) => setKind(e.target.value)}
              >
                {contentKinds.map((value) => (
                  <option key={value} value={value}>
                    {contentKindLabel(value)}
                  </option>
                ))}
              </select>
            </label>
```

- [ ] **Step 3: 改造项目详情的发布任务账号选择**

把每张内容卡片中的 `needsAccount` 与 `selectedAccountId`（第 767-773 行）替换为：

```tsx
            const contentSpec = specFor(content.platform, platformSpecs);
            const capability = contentSpec
              ? capabilityFor(contentSpec, content.kind)
              : "pending";
            const needsAccount = capability === "auto_publish";
            const platformAccounts = accountsForPlatform(
              accounts.data ?? [],
              content.platform,
            );
            const selectedAccountId =
              accountSelections[content.id] ?? platformAccounts[0]?.id ?? "";
            const missingAccount =
              needsAccount && accounts.isSuccess && !selectedAccountId;
            const accountBlocked =
              needsAccount && (!accounts.isSuccess || !selectedAccountId);
```

把按钮禁用与标题（第 805-821 行）中的账号文案改为：

```tsx
                            : accountBlocked
                              ? accounts.isSuccess
                                ? `请先授权${contentSpec?.label ?? "平台"}账号`
                                : "正在加载发布账号"
                              : undefined
```

把按钮下方账号选择块（第 835-865 行）替换为：

```tsx
                {needsAccount && (
                  <div className="mt-3 space-y-1">
                    <label className="block text-xs text-slate-500">
                      发布账号
                      <select
                        className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 disabled:opacity-50"
                        value={selectedAccountId}
                        onChange={(e) =>
                          setAccountSelections((prev) => ({
                            ...prev,
                            [content.id]: e.target.value,
                          }))
                        }
                        disabled={
                          !accounts.isSuccess || platformAccounts.length === 0
                        }
                      >
                        {!accounts.isSuccess && (
                          <option value="">
                            {accounts.isError ? "账号加载失败" : "加载中…"}
                          </option>
                        )}
                        {accounts.isSuccess && platformAccounts.length === 0 && (
                          <option value="">暂无可用账号</option>
                        )}
                        {platformAccounts.map((account) => (
                          <option key={account.id} value={account.id}>
                            {account.name}
                          </option>
                        ))}
                      </select>
                    </label>
                    {missingAccount && (
                      <p className="text-xs text-amber-400">
                        还没有可用的{contentSpec?.label ?? "平台"}账号，请先
                        <a className="ml-1 text-indigo-400" href="/accounts">
                          添加账号
                        </a>
                      </p>
                    )}
                  </div>
                )}
```

把 `createJob.mutate` 的 `accountId`（第 826 行）改为：

```tsx
                          accountId: needsAccount ? selectedAccountId : "",
```

在内容卡片中、账号选择块之后加入待接入提示：

```tsx
                {capability === "pending" && (
                  <p className="mt-2 text-xs text-amber-400">
                    {contentSpec?.note ?? "该平台暂不支持自动发布或导出"}
                  </p>
                )}
```

- [ ] **Step 4: 运行前端测试与构建**

Run: `cd web && npm test && npm run build`

Expected: 全部 PASS，构建成功。

- [ ] **Step 5: 提交**

```bash
git add web/src/pages/Publish.tsx web/src/pages/ProjectDetail.tsx
git commit -m "feat: show publish controls based on platform capability"
```

---

### Task 11: 文档与全量验证

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: 更新 README**

在 README 的功能列表中加入：

```markdown
- 发布账号：9 个平台、同平台多账号、按平台动态凭据表单；凭据加密且只返回掩码。
- 真实发布：公众号图文草稿、抖音视频、头条视频；小红书/知乎/快手/视频号导出素材包。
- 待接入：头条图文、百家号、B站显示明确提示，不产生假成功。
```

把发布说明中的“仅公众号”改为：

```markdown
定时发布覆盖能力为自动发布的「平台 + 内容类型」：公众号图文、抖音视频、
头条视频。人工导出平台仍由用户在发布页执行导出。
```

- [ ] **Step 2: 更新 CHANGELOG**

在 `CHANGELOG.md` 顶部加入：

```markdown
## 2026-11-12

- 新增平台能力注册表，账号支持 9 个平台与同平台多账号。
- 账号 API 支持动态凭据字段、平台过滤、能力矩阵与按平台校验 meta。
- 发布适配器接收内容上下文与存储后端；新增抖音、头条视频发布，百家号占位。
- 创建任务、手动执行与定时 worker 按「平台 + 内容类型」能力分流。
- 前端账号页动态渲染凭据表单，发布页与项目详情按能力显示操作。
- 头条图文与百家号发布仍需官方接口文档，当前返回明确配置错误。
```

- [ ] **Step 3: 后端全量验证**

Run: `cd backend && uv run pytest -q`

Expected: 全部 PASS。

Run: `cd backend && uv run ruff check .`

Expected: `All checks passed!`

- [ ] **Step 4: 前端全量验证**

Run: `cd web && npm test && npm run build`

Expected: 全部 PASS，构建成功。

- [ ] **Step 5: 提交**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: document multi-platform accounts and publishing capabilities"
```

---

## 验收清单

- [ ] 同一租户、同一平台可创建多个账号；同平台同名 409；跨平台同名允许。
- [ ] 9 个平台都能在账号页选择；凭据字段由注册表渲染；手动导出平台不需要凭据。
- [ ] 公众号图文、抖音视频、头条视频任务要求同平台有效账号，并能执行到 `published`。
- [ ] 头条图文、百家号、B站执行返回 409 与明确文案，状态不变成已发布。
- [ ] 小红书/知乎/快手/视频号仍可导出素材包。
- [ ] worker 只扫描公众号图文、抖音视频、头条视频，且按任务平台解析适配器。
- [ ] 抖音刷新令牌通过 `credential_patch` 加密回写，回写失败不影响已发布状态。
- [ ] 任何响应与日志都不出现完整凭据、refresh_token 或 access_token。
- [ ] `uv run pytest`、`uv run ruff check .`、`npm test`、`npm run build` 全部通过。

## 未决依赖

- 抖音/头条视频 endpoint 与字段名以用户方已获批的官方文档为准；如文档字段不同，只改 `douyin.py`/`toutiao.py` 与对应测试。
- 头条图文接口需要已获批文档；当前按设计返回配置错误。
- 百家号需要 app 资质、鉴权方式、endpoint 与请求/响应字段；当前按设计返回配置错误。
