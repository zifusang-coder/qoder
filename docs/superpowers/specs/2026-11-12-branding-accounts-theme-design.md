# 改名、多平台多账号发布与清爽主题设计

**Goal:** 把系统展示名改为「云AI新媒体运营系统」；让每个租户能在公众号、小红书、抖音、头条、百家号等平台下管理多个发布账号，并按平台能力执行真实自动发布或人工导出；把界面从深色靛蓝主题改为白灰底、自然绿主色、酒红点缀的清爽风格。

**Architecture:** 后端新增平台能力矩阵与适配器注册表，作为账号校验、发布选路和前端展示的唯一事实来源；账号表已支持同平台多账号，无需迁移；发布适配器改为接收内容上下文（标题、正文、类型、媒体文件），抖音/头条视频走官方开放平台接口，接口未获批的平台返回明确的「待接入」配置错误。前端账号页由能力矩阵动态渲染，主题通过 Tailwind v4 设计令牌与组件类统一落地。

**Tech Stack:** FastAPI、SQLAlchemy 2.0 async、Pydantic v2、httpx、arq、React 19、TanStack Query、Tailwind CSS v4、vitest。

**Status:** 设计方向已与用户确认，等待规格评审。

---

## 1. 背景

当前系统已具备：

- 多租户、四级角色、审计与登录加固。
- `PlatformAccount` 已有唯一索引 `(tenant_id, platform, name) WHERE status = 'active'`，数据层天然支持同平台多账号。
- 公众号自动草稿、小红书/知乎/快手/视频号人工导出、抖音开放平台媒体文件生成。

但存在以下缺口：

- 系统名仍是「新媒体运营系统」。
- 账号页只支持公众号，平台写死为 `wechat`，凭据字段写死为 `app_id`/`app_secret`。
- 平台白名单没有 `toutiao` 与 `baijiahao`。
- 发布接口与定时任务都只处理 `wechat`，其他平台没有真实发布路径。
- 前端全部使用深色 slate + indigo 配色，与目标视觉不符。

## 2. 范围

### 2.1 本期实现

1. 展示名统一为「云AI新媒体运营系统」。
2. 平台白名单扩展到 9 个：公众号、小红书、抖音、头条、百家号、知乎、快手、视频号、B站。
3. 同一租户在同一平台下可创建多个账号，账号名同平台内唯一。
4. 平台能力矩阵与账号动态表单。
5. 抖音视频、头条视频真实自动发布。
6. 公众号草稿保持现有真实发布行为。
7. 人工导出平台保持现有导出行为，并补齐能力提示。
8. 头条图文、百家号、B站标记为「待接入」，执行时返回明确配置错误，不产生假成功。
9. 白灰 + 自然绿 + 酒红主题与组件类统一迁移。
10. 后端单测、前端单测、构建与逐页视觉验收。

### 2.2 本期不做

- 小红书、知乎、快手、视频号、B站的真实接口发布。
- 头条图文与百家号的真实发布实现（依赖官方文档，见第 12 节）。
- OAuth 授权回调页；抖音/头条账号采用手工填入 `refresh_token` 的方式接入。
- 视频转码、封面自动生成、平台侧定时发布。
- 账号分组、账号级权限、账号间继承。
- 页面信息架构与交互流程重构；主题只改视觉层。

## 3. 命名

展示名统一为 **云AI新媒体运营系统**。仅修改用户可见文案与文档标题，不改包名、目录名、数据库名、URL 与迁移历史。

| 位置 | 现状 | 目标 |
|---|---|---|
| `web/index.html` | `<title>新媒体运营系统</title>` | `<title>云AI新媒体运营系统</title>` |
| `web/src/pages/Login.tsx` | `新媒体运营系统` | `云AI新媒体运营系统` |
| `web/src/components/Layout.tsx` | `新媒体运营` | `云AI新媒体运营系统` |
| `backend/app/config.py` | `app_name = "新媒体运营系统"` | `app_name = "云AI新媒体运营系统"` |
| `backend/app/api/chat.py` | 助理人设首句 | 改为「云AI新媒体运营系统」 |
| `README.md` / `CHANGELOG.md` | 旧标题 | 新标题，正文首次出现处同步 |

历史设计文档与历史计划保持原样，避免无意义的大面积改动。

## 4. 平台能力矩阵

新增 `backend/app/publish/registry.py`，定义 `PlatformSpec` 与注册表。所有平台相关校验与 UI 展示都从矩阵派生。

```python
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
    content_capabilities: dict[str, str]  # 内容类型 -> 能力
    credential_fields: tuple[CredentialField, ...]
    meta_fields: tuple[str, ...]
    note: str = ""
```

能力按「平台 + 内容类型」判定，避免头条出现“平台可发布但图文不可发布”的歧义：

| key | 展示名 | 内容能力 | 说明 |
|---|---|---|---|
| `wechat` | 公众号 | `article: auto_publish` | 现有草稿接口 |
| `douyin` | 抖音 | `video: auto_publish` | 官方开放平台视频发布 |
| `toutiao` | 头条 | `video: auto_publish`、`article: pending` | 官方视频接口；图文待接入 |
| `xiaohongshu` | 小红书 | `note/article: manual_export` | 现有素材包导出 |
| `zhihu` | 知乎 | `article: manual_export` | 现有素材包导出 |
| `kuaishou` | 快手 | `video/note: manual_export` | 现有素材包导出 |
| `channels` | 视频号 | `video/article: manual_export` | 现有素材包导出 |
| `baijiahao` | 百家号 | `article: pending` | 待官方发布文档 |
| `bilibili` | B站 | `video: pending` | 暂无官方发布接口 |

能力查询接口：

```python
def capability_for(platform: str, content_kind: str) -> str:
    spec = SPECS[platform]  # KeyError 由调用方转 404/422
    return spec.content_capabilities.get(content_kind, "pending")


def headline_capability(spec: PlatformSpec) -> str:
    """账号页展示用：只要存在可由接口执行的内容类型，就标为 auto_publish。"""
    values = set(spec.content_capabilities.values())
    if "auto_publish" in values:
        return "auto_publish"
    if "manual_export" in values:
        return "manual_export"
    return "pending"
```

`PLATFORMS` 从注册表派生，`Platform` Literal 同步加入 `toutiao`、`baijiahao`。账号表、内容表、任务表均使用 `String(32)`，**不需要 Alembic 迁移**。

能力语义：

- `auto_publish`：该内容类型可创建需要账号的发布任务，可执行真实发布，可被 worker 定时执行。
- `manual_export`：该内容类型走现有导出流程，不需要凭据，不进入 worker。
- `pending`：可以保存账号与凭据，但发布按钮不可用，执行返回配置错误。

## 5. 账号 API

### 5.1 凭据字段

| 平台 | 凭据字段 |
|---|---|
| 公众号 | `app_id`、`app_secret` |
| 抖音 / 头条 | `client_key`、`client_secret`、`open_id`、`refresh_token` |
| 百家号 | `app_id`、`app_token` |
| 人工导出平台 | 无；允许空对象 |

`_clean_credential` 改为按 `PlatformSpec.credential_fields` 校验：必填字段去空白后非空，丢弃未声明字段，拒绝非字符串值。`app_secret`、`client_secret`、`app_token`、`refresh_token` 只加密存储，任何响应只返回 `****`。

### 5.2 Meta

`_clean_meta` 按平台声明校验：

- 公众号保留 `author`、`thumb_media_id`、`need_open_comment`、`only_fans_can_comment`。
- 头条保留 `author`。
- 其他平台本期不允许 meta；提交非空 meta 返回 422。

### 5.3 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/accounts/platforms` | 返回能力矩阵，不含任何凭据 |
| GET | `/accounts?platform=` | 可选平台过滤；返回按平台分组所需的字段 |
| POST | `/accounts` | 创建账号；同平台同名 409 |
| PATCH | `/accounts/{id}` | 改名、更新凭据、更新 meta |
| DELETE | `/accounts/{id}` | 停用并销毁凭据与 meta |

`AccountOut` 增加：

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
```

`meta` 不含密钥；`capability` 取 `headline_capability(spec)`，用于账号页平台徽章；`capability_note` 从注册表派生，避免前端猜测。创建与更新仍要求 admin。

### 5.4 多账号规则

- 同一租户、同一平台、同名且 `active` → 409。
- 不同平台可以同名。
- 停用后的同名账号不占用唯一索引，可重新创建。
- 列表默认按平台顺序、再按创建时间倒序排列，前端可直接分组。

## 6. 发布适配器

### 6.1 内容上下文

`PublisherAdapter` 的输入从 `title/body` 扩展为 `PublishContent`，避免适配器查询数据库：

```python
@dataclass(frozen=True)
class PublishMedia:
    name: str
    kind: str   # video / image / audio / subtitles
    path: str   # storage 相对路径


@dataclass(frozen=True)
class PublishContent:
    project_id: str
    title: str
    body: str
    kind: str
    media: tuple[PublishMedia, ...]
    cover_path: str | None


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
        account: PlatformAccount,
        content: PublishContent,
        storage: StorageBackend,
    ) -> PublishResult: ...
```

媒体来自 `ContentVersion.meta["files"]`，封面来自 `ContentVersion.meta["cover"]`。`execute_job` 负责组装内容上下文，并按版本所属 `project_id` 读取文件。

### 6.2 注册表

```python
def adapter_for(platform: str) -> PublisherAdapter:
    try:
        factory = ADAPTERS[platform]
    except KeyError:
        raise PublishConfigError(f"{platform} 暂不支持自动发布") from None
    return factory()
```

`ADAPTERS` 只包含 `wechat`、`douyin`、`toutiao`、`baijiahao` 等已登记实现；`baijiahao` 为占位实现。

### 6.3 公众号

- 行为不变：Markdown 转 HTML，创建草稿。
- 内容类型不是 `article` → `PublishConfigError("公众号仅支持图文内容")`。
- 封面 `thumb_media_id` 仍从账号 meta 读取。

### 6.4 抖音

- 凭据：`client_key`、`client_secret`、`open_id`、`refresh_token`；内部可能含 `access_token`、`expires_at`。
- `access_token` 缺失或过期时，用 `refresh_token` 调官方刷新接口换取新令牌，返回 `credential_patch` 由发布服务加密回写。
- 视频文件：从 `content.media` 中取第一个 `kind == "video"` 的文件，通过 `storage.read_bytes` 读取。
- 发布两步：官方视频上传接口获得 `video_id`，再调官方创建视频接口，正文使用 `title + "\n" + body` 并按平台限制截断。
- 缺少视频文件、内容类型不是视频 → `PublishConfigError`，不计入发布尝试次数。
- HTTP 失败只返回平台错误码与安全文案，不包含 URL、token、请求体。
- 具体 endpoint 与字段名在实现时以用户方已获批的官方文档为准；全部封装在 `backend/app/publish/douyin.py`，不扩散到服务层。

### 6.5 头条

- 视频：走抖音开放平台的头条视频创建接口，凭据与请求封装在 `backend/app/publish/toutiao.py`。
- 图文：返回 `PublishConfigError("头条图文发布接口已下线或未获批，请提供官方接口文档后再启用")`。
- 账号 meta 允许 `author`，用于视频标题前缀时按平台规则处理。

### 6.6 百家号

- 占位实现：读取凭据完整性后，直接返回 `PublishConfigError("百家号官方发布接口尚未接入，请提供已获批的接口文档")`。
- 不发起任何 HTTP 请求，不产生 `external_id`，不会进入已发布状态。

### 6.7 人工导出

`ExportAdapter` 行为不变，`MANUAL_EXPORT_PLATFORMS` 从注册表派生，保持小红书、知乎、快手、视频号。

## 7. 发布服务与 worker

### 7.1 创建任务

- 先按 `capability_for(job.platform, content.kind)` 判定该任务的真实能力。
- `auto_publish`：必须提供同平台有效账号，否则 409 `account_missing`。
- `manual_export` 与 `pending`：仍可不关联账号。
- 平台与内容平台不一致的校验保持现状。
- 内容类型不在该平台声明范围内（例如给公众号选择视频内容）→ 409 `platform_content_mismatch`，消息包含平台与内容类型。

### 7.2 手动执行

`POST /publish/jobs/{id}/execute`：

1. 读取任务、内容版本与 `capability_for(job.platform, version.kind)`。
2. `manual_export` → 409 `manual_export_required`。
3. `pending` → 409 `platform_not_available`，消息包含平台 note。
4. `auto_publish` → 通过注册表取适配器，组装内容上下文，调用 `execute_job`。
5. `PublishConfigError` → 409 `account_misconfigured`；`PublishError` → 502 `publish_failed`。

### 7.3 令牌回写

`execute_job` 在发布成功后检查 `result.credential_patch`：非空时与现有凭据合并，重新 Fernet 加密写回账号。回写失败不回滚已成功的发布，只记录日志并保留 `published` 状态。

### 7.4 定时任务

- `list_due_jobs` 从 `PublishJob.platform == "wechat"` 改为 `PublishJob.platform.in_(AUTO_PUBLISH_PLATFORMS)`。
- `publish_due_jobs` 按任务平台解析适配器，并把 `get_storage()` 传入执行链。
- 测试可注入 `adapter_resolver` 与 `session_factory`，不改变现有可测试性。

## 8. 前端

### 8.1 平台模块

新增 `web/src/platforms.ts`：

```ts
export type Capability = "auto_publish" | "manual_export" | "pending";

export interface PlatformSpec {
  key: string;
  label: string;
  capability: Capability;                    // 账号页平台徽章
  content_capabilities: Record<string, Capability>;
  credential_fields: {
    key: string;
    label: string;
    secret: boolean;
    required: boolean;
    placeholder: string;
  }[];
  note: string;
}

export function capabilityFor(
  spec: PlatformSpec,
  contentKind: string,
): Capability {
  return spec.content_capabilities[contentKind] ?? "pending";
}
```

- `usePlatforms()` 通过 TanStack Query 请求 `/accounts/platforms`。
- 请求失败时回退到内置静态矩阵，只用于展示，不允许绕过后端校验。
- `platformLabel` 优先使用接口返回的 label。
- `types.ts` 的 `PLATFORMS` 扩展为 9 项，`MANUAL_EXPORT_PLATFORMS` 保留。

### 8.2 账号页

`Accounts.tsx` 重写为：

- 顶部平台选择器：9 个平台，显示能力徽章。
- 授权表单：字段由所选平台的 `credential_fields` 动态渲染；`secret` 字段使用密码输入。
- `pending` 平台在表单上方提示「可保存凭据，接口待接入」。
- `manual_export` 平台隐藏凭据字段，只要求账号名称。
- 账号列表按平台分组，每组显示数量；同平台多账号并列。
- 每个账号展示：名称、状态、凭据掩码、能力徽章、备注。
- 保留重命名、更新凭据、停用二次确认。

### 8.3 发布页

- 下拉账号时按任务平台过滤。
- 账号提示对所有 `auto_publish` 任务生效，不再只判断 `wechat`。
- `actionFor` 接受任务平台与内容类型，通过 `capabilityFor` 得到能力：
  - `auto_publish` + `approved` → `execute`，按钮文案按平台显示「发布到公众号草稿」「发布到抖音」「发布到头条」。
  - `manual_export` + `approved` → `export`。
  - `pending` → 不显示可执行按钮，显示 note。
- 发布失败的错误文案沿用后端安全返回。

### 8.4 项目与内容

- 创建项目时的平台多选自动包含新增平台。
- 内容平台、日历平台标签通过统一 `platformLabel` 显示。

## 9. 主题

### 9.1 设计令牌

在 `web/src/index.css` 使用 Tailwind v4 `@theme`：

```css
@theme {
  --color-paper: #f6f6f3;
  --color-surface: #ffffff;
  --color-ink-900: #1c211e;
  --color-ink-700: #3d4642;
  --color-ink-500: #6b7570;
  --color-ink-300: #a8b0ac;
  --color-line: #e3e5e1;
  --color-sage-50: #eef5f0;
  --color-sage-100: #dcebe1;
  --color-sage-500: #3f8a66;
  --color-sage-600: #2f7554;
  --color-sage-700: #245c43;
  --color-wine-50: #f8eef0;
  --color-wine-100: #efd9dd;
  --color-wine-500: #8c3a4a;
  --color-wine-600: #752f3d;
  --color-wine-700: #5d2531;
  --color-warm-600: #8a6a3b;
}
```

`body` 使用 `bg-paper text-ink-900`，不再使用 `bg-slate-950`。

### 9.2 组件类

```css
@layer components {
  .card { @apply rounded-xl border border-line bg-surface; }
  .card-pad { @apply p-5; }
  .input { @apply w-full rounded-lg border border-line bg-white px-3 py-2 text-ink-900 placeholder:text-ink-300 focus:border-sage-500 focus:outline-none focus:ring-2 focus:ring-sage-100; }
  .btn-primary { @apply rounded-lg bg-sage-600 px-4 py-2 text-sm font-medium text-white hover:bg-sage-700 disabled:opacity-50; }
  .btn-secondary { @apply rounded-lg border border-line bg-white px-4 py-2 text-sm text-ink-700 hover:border-ink-300; }
  .btn-danger { @apply rounded-lg border border-wine-100 bg-wine-50 px-4 py-2 text-sm text-wine-700 hover:bg-wine-100; }
  .badge { @apply inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium; }
  .nav-link { @apply rounded-md px-3 py-1.5 text-sm text-ink-500 transition-colors hover:text-ink-900; }
  .nav-link-active { @apply rounded-md bg-sage-50 px-3 py-1.5 text-sm font-medium text-sage-700; }
}
```

### 9.3 迁移映射

| 旧 | 新 |
|---|---|
| `bg-slate-950` | `bg-paper` |
| `bg-slate-900` | `bg-surface` + `.card` |
| `border-slate-800/700` | `border-line` |
| `text-slate-100/200/300` | `text-ink-900/700` |
| `text-slate-400/500` | `text-ink-500` |
| `indigo-600/400` | `sage-600/700` |
| `red-400/700/900` | `wine-600/700/100` |
| `emerald-400` | `sage-600` |
| `amber-*` 警告 | `warm-600` 或酒红浅底 + 明确文案 |

覆盖范围：`Layout`、`Login`、`Projects`、`ProjectDetail`、`Publish`、`Accounts`、`Profiles`、`Chat`、`Analytics`、`Calendar`、`Settings`、`Team`、`Audit`、`Tenants`、`ChangePassword` 及 `MediaPreview`。

### 9.4 视觉规则

- 页面底色统一 `paper`，卡片白底细边框，不使用大阴影。
- 主按钮、激活导航、成功状态使用自然绿。
- 删除、停用、错误、不可逆操作使用酒红。
- 品牌名使用深墨色；顶栏白底，底部一条 `line` 分隔线。
- 表格表头使用 `ink-500` 小号字，行分隔使用 `line`。
- 正文与按钮对比度达到 WCAG AA。
- 不新增图片素材、渐变或装饰性插画。

## 10. 错误处理与安全

- 凭据继续 Fernet 加密；响应只返回字段名与 `****`；日志不输出 URL、请求体、token、refresh_token。
- 抖音刷新令牌只通过 `PublishResult.credential_patch` 回写，且回写内容重新加密。
- 所有平台 HTTP 异常归一化为 `PublishError`，只暴露安全文案。
- `pending` 平台与内容类型不匹配使用 `PublishConfigError`，不消耗发布尝试次数。
- 不引入 Cookie、模拟登录、逆向接口或浏览器自动化。
- 平台账号仍按租户隔离，跨租户访问返回 404。
- 人工导出与自动发布仍要求任务先经人工审核。

## 11. 测试与验收

### 11.1 后端

| 测试文件 | 覆盖点 |
|---|---|
| `tests/test_platform_registry.py` | 每个平台有 spec；capability 合法；人工导出集合与注册表一致；未知平台拒绝 |
| `tests/test_accounts.py` | 同平台多账号；同平台同名 409；跨平台同名允许；按平台过滤；动态凭据校验；空凭据人工平台；掩码不含原文 |
| `tests/test_publish_douyin.py` | 缺失视频报配置错误；上传 + 创建成功；刷新令牌产生 patch；HTTP 错误脱敏 |
| `tests/test_publish_toutiao.py` | 视频成功路径；图文返回明确的待接入错误 |
| `tests/test_publish_baijiahao.py` | 永远返回待接入错误且不发请求 |
| `tests/test_publish_service.py` | 能力分流；账号必填；credential_patch 加密回写；发布失败次数规则 |
| `tests/test_worker.py` | 到期任务包含抖音/头条、排除人工导出与待接入；按平台解析适配器 |

### 11.2 前端

- `web/src/__tests__/platforms.test.ts`：label、capability、action 映射、账号过滤。
- `npm run build` 通过。
- 浏览器逐页检查：登录、项目列表、项目详情、发布、账号、画像、对话、数据、日历、设置、团队、审计、租户、改密。
- 断言页面不再出现 `bg-slate-9`、`text-slate-`、`indigo-` 等旧主题类（允许测试或注释中出现的例外需逐条说明）。

### 11.3 验收标准

1. 浏览器标题、登录页、顶栏、README 均显示「云AI新媒体运营系统」。
2. 可在 9 个平台下分别创建多个账号；同平台同名被拒绝，跨平台同名允许。
3. 公众号草稿发布行为与现状一致。
4. 抖音/头条视频任务在具备凭据与视频文件时走官方接口；缺少前置条件时返回可读错误。
5. 头条图文、百家号、B站显示「待接入」，点击不产生发布或导出副作用。
6. 小红书/知乎/快手/视频号仍可导出素材包。
7. 全站白灰底，自然绿用于主操作，酒红用于危险操作，无残留深色区块。
8. 后端全量测试通过，前端测试与构建通过。

## 12. 未决依赖

以下能力无法在没有官方接口文档的情况下实现，也不会用非官方方式替代：

- **头条图文发布**：头条投稿 OpenAPI 已于 2023-11-02 下线，需要用户方提供已获批的新接口文档。
- **百家号发布**：未找到可验证的官方发布接口文档，需要用户方提供 app 资质、鉴权方式、endpoint 与请求/响应字段。
- **抖音/头条视频**：实现时以用户方已获批的官方文档为准，确认上传与创建接口的字段和返回结构。

文档可放入 `docs/platform-apis/` 或直接提供给实现者。接口到位后，只需新增或补全对应适配器，账号模型、API 与前端无需再改。

## 13. 交付顺序

实施计划拆为三份，按顺序执行，每份完成后即可独立验收：

1. **改名计划**：后端配置、前端三处、README 与 CHANGELOG。
2. **账号与发布计划**：平台注册表、账号 API、适配器上下文、抖音/头条/百家号适配器、发布服务与 worker、前端平台模块与两个页面。
3. **主题计划**：设计令牌、组件类、逐页迁移与视觉验收。

每份计划内部按 TDD 小步提交；主题计划只改展示层，不回退业务行为。三份计划全部完成后再做一次端到端验收。
