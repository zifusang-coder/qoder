# 原生 AI 新媒体运营系统 — 开发文档

| 项 | 内容 |
|---|---|
| 版本 | v1.0 |
| 日期 | 2026-10-26 |
| 状态 | 待评审 |
| 参考竞品 | [ZJU-REAL/Easel](https://github.com/ZJU-REAL/Easel)(Apache 2.0) |
| 架构决策 | 方案 C:双引擎混合(确定性流水线 + 受控 Agent 技能执行器) |

---

## 1. 项目概述

### 1.1 定位

一个自建的原生 AI 新媒体运营系统:以账号画像为上下文,完成「创作 → 发布 → 归因 → 回写画像」的闭环,让 AI 产出随使用越来越贴合账号。

与 Easel 的关系:**借鉴其方法论与可复用资产,重写其架构与发布合规路径。**

### 1.2 产品形态与目标用户

- **Phase 1:单租户工具**。自己 / 小团队使用,部署在自有腾讯云服务器。
- **架构预留 SaaS 演进**:所有业务表带 `tenant_id`,认证层预留 RBAC,存储用对象存储抽象而非本地路径耦合。Phase 3 再开多租户与计费。
- 目标用户画像:1-5 人的新媒体团队 / 个人创作者,已有公众号或计划运营多平台账号,愿意自配 LLM API Key。

### 1.3 核心价值主张

1. **画像驱动**:六维画像 + 归因回写,输出越用越准,区别于一次性生成工具。
2. **闭环而非清单**:创作、发布、复盘在同一系统内流转,产物项目化归档。
3. **合规优先**:发布默认人工审核,优先官方 API,不把平台风控当免费午餐。

### 1.4 MVP 范围(已确认)

创作 + 发布 + 归因最小闭环;视频 / TTS / 短剧等重媒体能力放二期;发现层(热榜聚合)二期接入,一期用「手动投喂选题 + AI 扩展」替代。

---

## 2. 竞品结论与资产决策

### 2.1 Easel 结论摘要

- 五层工作流(发现/策划/创作/发布/归因)、113 技能、画像六维度、项目化产物管理——方法论成熟,值得抄。
- 工程侧短板:OpenClaw 强绑定、单机 CLI 架构、Playwright 逆向发布(平台风控与合规风险)、Windows 兼容未完成、v0.3 API 不稳定。
- 因此:**不 fork 二开,不做纯复刻,自建架构 + 搬运资产。**

### 2.2 资产盘点

#### A. 直接搬运(Apache 2.0,Easel `skills/*/scripts` 与 `skills/shared/`)

| 资产 | Easel 位置 | 用途 |
|---|---|---|
| 媒体处理脚本(ffmpeg 封装、卡片渲染、图片增强、抠图) | `skills/builtin/card-*`, `image-*`, `remove-bg`, `skills/shared/scripts` | 原样移植,运行时无关 |
| 平台排版规则与字数约束 | 各 `skill-*-publisher/SKILL.md` | 转成 `platform_rules` 配置 |
| 热榜 API 清单 | `skills/shared/hotlist-apis.md` | 二期发现层直接采用 |
| 画像六维度字段定义 | `profiles/_template/` | 数据库 schema 参考 |
| 产物目录规约 | `docs/SKILL-SPEC.md` 产物管理章节 | 内容库文件布局 |
| 技能测试样例格式 | 各 skill `tests/*.prompt/expected` | 技能 eval 用例 |

> 搬运时保留 LICENSE 声明与出处注释;不复制 `assets/` 大文件仓库。

#### B. 改造搬迁

| 资产 | 改造方式 |
|---|---|
| SKILL.md 方法论 | 拆成 `prompt.md`(执行指令)+ `references/`(领域知识)+ `manifest.yaml`(参数 schema) |
| 质量门禁(quality-gate)/ 风险扫描(risk-scanner) | 重写为服务端校验模块,不依赖 LLM 自觉 |
| 人设一致性评分(persona_gate) | 改为「LLM 评分 + 阈值提示」,默认只提示不阻断 |
| 发布脚本(Playwright) | 仅保留其选择器与流程知识,降级为「导出辅助 / 实验性模块」,不作为默认发布路径 |

#### C. 弃用

| 资产 | 原因 |
|---|---|
| OpenClaw runtime 与 profile 机制 | 强绑定、单机、不可服务化 |
| `easel` CLI / venv 安装形态 | 产品形态是 Web 服务 |
| 默认 Playwright 自动发布 | 平台风控与合规风险;账号安全 |
| 113 技能全量迁移 | 范围失控;按 MVP 清单逐批搬 |

---

## 3. 总体架构

### 3.1 架构总览

```
┌────────────────────────────────────────────────────────────────┐
│  React 19 + Vite + TS  前端(工作台 / 对话 / 内容库 / 发布中心)      │
│         REST + SSE(流式)                                        │
└──────────────────────────┬─────────────────────────────────────┘
                           │
┌──────────────────────────▼─────────────────────────────────────┐
│                     FastAPI 后端(API 层)                        │
│  auth | profiles | projects | contents | skills | publish       │
│  analytics | conversations | settings                           │
├────────────────────────────────────────────────────────────────┤
│  受控 Agent 引擎                    确定性引擎                    │
│  ┌──────────────────────┐        ┌──────────────────────────┐  │
│  │ 意图识别(函数调用)     │        │ 发布任务编排(状态机)        │  │
│  │ 技能匹配(注册表)       │        │ 归因采集(定时 + 手动录入)    │  │
│  │ 参数抽取(schema)      │        │ 日历与排期(arq 定时)        │  │
│  │ 上下文组装(画像/参考)   │        │ 文件与产物归档              │  │
│  │ LLM 流式生成          │        └──────────────────────────┘  │
│  │ 后处理脚本(沙箱)       │                                      │
│  └──────────┬───────────┘                                       │
│             │                                                   │
│  ┌──────────▼───────────┐   ┌───────────────────────────────┐  │
│  │ LLM Gateway          │   │ 发布通道层 PublisherAdapter     │  │
│  │ 多供应商 / 密钥加密     │   │  WeChatMP | Bilibili | Douyin  │  │
│  │ 模型档位路由 / 成本统计 │   │  Export(小红书/知乎/快手/视频号) │  │
│  └──────────────────────┘   └───────────────────────────────┘  │
├────────────────────────────────────────────────────────────────┤
│  PostgreSQL(业务 + pgvector 记忆) | Redis(队列/缓存) | COS/MinIO │
└────────────────────────────────────────────────────────────────┘
```

### 3.2 双引擎边界

| 维度 | 确定性引擎 | 受控 Agent 引擎 |
|---|---|---|
| 负责 | 发布状态机、归因采集、日历排期、归档、质检结果落库 | 意图理解、内容生成、改写、评分、复盘叙述 |
| 特点 | 纯代码、可单测、可重放、无 token 成本 | LLM 参与、流式输出、有 token 成本 |
| 原则 | 涉及「对外副作用」的动作一律走确定性引擎 | 所有工具调用走白名单,不开放任意 shell / 网络 |

**受控 Agent 的边界纪律**:LLM 只决定「调用哪个技能、用什么参数、生成什么文本」;技能脚本在沙箱内执行(固定命令白名单、路径限制在项目目录内、超时与并发上限),发布动作由后端状态机发起,LLM 无权直接触发。

### 3.3 技术选型

| 层 | 选型 | 理由 |
|---|---|---|
| 后端 | FastAPI + Pydantic v2 + SQLAlchemy 2.0(async) + Alembic | 与 Easel 技能脚本同语言,搬运零摩擦 |
| 任务 | arq(Redis 队列) | asyncio 原生,比 Celery 轻;媒体处理放 worker |
| 数据库 | PostgreSQL 16 + pgvector(二期启用) | 业务关系 + 记忆向量一体 |
| 缓存/队列 | Redis 7 | 会话缓存、限流、arq |
| 对象存储 | 腾讯云 COS(本地开发 MinIO) | 贴近现有云环境,便于 SaaS 化 |
| 前端 | React 19 + Vite + TypeScript + Tailwind CSS + TanStack Query | 参考 Easel 的克制依赖策略 |
| Markdown | marked + DOMPurify | 与 Easel 一致,防 XSS |
| 媒体 | ffmpeg + Pillow(一期);rembg / edge-tts / faster-whisper(二期) | 直接复用 Easel 脚本 |
| LLM | OpenAI-compatible 网关,支持 Anthropic 原生协议 | 用户自带 Key,多供应商可切换 |
| 部署 | Docker Compose(api / worker / web / postgres / redis / minio) | 单机起步,后期可拆 K8s |

### 3.4 部署拓扑(MVP)

```
腾讯云 CVM(自有服务器)
├── Nginx(HTTPS,反向代理 / → web static,/api → api)
├── api 容器(FastAPI,uvicorn)
├── worker 容器(arq:技能脚本、归因采集、定时发布)
├── postgres 容器(数据卷持久化)
├── redis 容器
└── COS 桶(素材与产物,内网直连)
```
单机阶段不暴露 7860 类无认证端口;所有接口需登录态。每日 `pg_dump` + COS 版本化备份。

---

## 4. 核心模块设计

### 4.1 技能(Skill)系统

#### 4.1.1 技能单元结构

```
skills/
  wechat-article/
    manifest.yaml        # 元数据 + 参数 schema + 模型档位 + 后处理
    prompt.md            # 系统提示词(由 Easel SKILL.md 改造)
    references/          # 领域知识,按需注入(不常驻上下文)
      wechat-style.md
    scripts/             # 后处理脚本(搬运或新写)
      format_check.py
    tests/               # 输入/期望关键字
      01-basic.prompt
      01-basic.expected
```

#### 4.1.2 manifest 规范

```yaml
id: wechat-article
name: 公众号长文创作
version: 1.0.0
layer: produce                # discover | plan | produce | publish | attribute | general
description: 基于选题与账号画像生成公众号长文,输出标题、正文与摘要
inputs:
  topic: {type: string, required: true}
  angle: {type: string}
  target_length: {type: integer, default: 1800}
context:
  profile: required           # required | optional | none
  references: [references/wechat-style.md]
outputs:
  format: markdown
  schema: {title: string, body_md: string, summary: string}
llm:
  tier: standard              # fast | standard | premium(映射到用户配置的模型)
  temperature: 0.7
tools: [web_search]           # 工具白名单,默认空
postprocess:
  - script: scripts/format_check.py
    args: ["--max-chars", "20000"]
cost_estimate_tokens: 6000
```

#### 4.1.3 执行流程(受控 Agent)

```
用户消息
  → 意图识别:LLM function-calling,候选函数 = 已启用技能清单(技能少时可用规则/小模型降本)
  → 技能匹配与参数抽取:按 manifest.inputs 校验,缺参数则反问(最多 2 轮)
  → 上下文组装:
       系统提示词 = 技能 prompt.md
                   + 画像凝练(六维度摘要,≤800 token)
                   + references 按 manifest 注入(≤2000 token)
                   + 项目历史摘要(可选)
  → LLM 流式生成(SSE 直通前端)
  → 后处理:脚本校验 / 卡片渲染 / 敏感词扫描
  → 产物落库:contents + assets + skill_runs(审计:输入、输出、token、耗时)
```

#### 4.1.4 MVP 首批技能(12 个,均从 Easel 改造或搬运)

| 技能 | 来源 | 层 |
|---|---|---|
| topic-evaluator 选题评估 | skill-topic-evaluator 改造 | plan |
| article-outline 大纲 | skill-article-outline | plan |
| wechat-article 公众号长文 | 新写(prompt 参考 gzh-design/SKILL.md 方法论) | produce |
| xhs-note 小红书笔记 | xhs-note-creator 改造 | produce |
| copywriting 通用文案 | copywriting 改造 | produce |
| title-hook 标题生成 | skill-hook-generator | produce |
| content-repurposing 一稿多平台 | skill-content-repurposing | produce |
| cover-card 封面卡片图 | card-xiaohongshu + card-quote 脚本搬运 | produce |
| quality-gate 质量门禁 | skill-quality-gate 重写为规则 + LLM 双段 | general |
| risk-scan 敏感/版权扫描 | skill-risk-scanner 重写为关键词库 + LLM 复核 | general |
| persona-check 人设一致性 | persona_gate 改造 | general |
| postmortem 复盘报告 | skill-content-postmortem 改造 | attribute |

二期再按需搬视频/TTS/字幕/短剧等重媒体技能,原则:**一次只搬会被用到的技能**。

#### 4.1.5 技能质量保障

- `scripts/validate_skills.py`:校验 manifest schema、prompt 存在性、脚本路径、参数漂移(参考 Easel 两道校验脚本)。
- 技能 eval:`tests/*.prompt` + `*.expected` 关键字匹配,CI 中跑冒烟;人工定期抽检质量。
- 技能版本化:`skills.version` 升版,`skill_runs` 记录所用版本,便于回滚。

### 4.2 账号画像系统

#### 4.2.1 六维度模型

| 维度 | 字段 | 说明 |
|---|---|---|
| 定位 | `positioning` | 账号赛道、人设一句话、差异化 |
| 风格 | `style` | 语气、句式偏好、emoji 使用、禁用表达 |
| 受众 | `audience` | 画像、痛点、阅读场景 |
| 平台 | `platforms` (jsonb) | 平台、账号、平台特定约束 |
| 偏好与红线 | `preferences_redlines` (jsonb) | 必做、禁做、敏感话题 |
| 长期记忆 | `profile_memories` 表 | 归因沉淀的规律、反馈、指标结论 |

#### 4.2.2 记忆回写机制

```
归因数据(阅读/互动/评论)
  → 复盘技能生成结论(哪些结构有效/无效)
  → 抽取候选记忆(kind: pattern | feedback | metric)
  → 写 profile_memories,带 source_ref 指向数据快照
  → 二期:pgvector 索引,技能执行时按选题相似度检索 Top-K 注入
```
一期用 `kind + 时间 + 平台` 过滤注入(≤800 token);二期上向量检索,不做全量拼接。

#### 4.2.3 画像注入格式

统一用标记包裹,便于调试与防注入:
```
=== ACCOUNT PROFILE BEGIN ===
定位 / 风格 / 受众 / 红线(凝练摘要)
长期记忆(按权重取前 5 条)
=== ACCOUNT PROFILE END ===
```
用户可编辑的画像内容与系统指令分区存储,拼接时转义 `===` 标记,防止画像文本逃逸出上下文区。

### 4.3 内容生产流水线

```
选题(手动/AI 推荐) → 大纲 → 草稿(母版) → 多平台改写 → 质检 → 审核 → 排期/发布
```

- **母版内容**:一次创作产出母版,各平台版本由 `content-repurposing` + `platform_rules` 派生。
- **版本管理**:每次改写/编辑生成 `content_versions` 记录,可回滚。
- **产物归档**:每个选题 = 一个 `project`,文件落 `storage/projects/<project_id>/`(COS 同构):
  ```
  <project_id>/
    final.md / final.mp4 / cover.png     # 成品
    assets/                              # 中间件
    manifest.json                        # 项目元数据(DB 为准,文件为快照)
  ```

### 4.4 发布中心

#### 4.4.1 通道抽象

```python
class PublisherAdapter(Protocol):
    platform: str
    capability: Literal["auto", "semi_auto", "manual_export"]

    async def precheck(self, account: Account, content: Content,
                       profile: Profile) -> PrecheckReport: ...
    async def adapt(self, content: Content, rules: PlatformRules) -> AdaptedContent: ...
    async def publish(self, account: Account, adapted: AdaptedContent) -> PublishReceipt: ...
    async def export(self, adapted: AdaptedContent, dest: Path) -> Path: ...
```

#### 4.4.2 平台接入矩阵(全覆盖、分层落地)

| 平台 | 通道 | 能力级别 | 前置条件 | 落地阶段 |
|---|---|---|---|---|
| 微信公众号 | 官方 API(素材上传 → 草稿箱 → 发布) | auto | 微信认证的订阅号/服务号(发布接口需认证) | MVP |
| B 站 | biliup 上传工具(社区成熟项目) | auto(社区工具通道,视频二期) | 实名 + 扫码登录态 | Phase 2 |
| 抖音 | 开放平台视频发布 API | auto | **企业资质 + 应用审核** | Phase 2(资质先行申请) |
| 小红书 | 导出包 + 人工发布(可选实验性 Playwright,默认关闭) | manual_export | 无 | MVP 导出 |
| 知乎 | 导出包(富文本/图片) | manual_export | 无 | MVP 导出 |
| 快手 | 导出包(视频时随 Phase 2) | manual_export | 无 | MVP 导出 |
| 微信视频号 | 导出包 | manual_export | 无 | Phase 2 |

> 设计原则:**能力级别是数据,不是代码分支**。新增平台 = 新增 adapter + 配置,不改发布状态机。
> 小红书自动发布不内建为默认能力:平台风控与合规风险不可控,实验模块需显式开启并弹风险确认。

#### 4.4.3 发布状态机

```
draft: submit→prechecking, export→exporting, cancel→cancelled
prechecking: pass→awaiting_review, fail→precheck_failed
precheck_failed: submit→prechecking, cancel→cancelled
awaiting_review: approve→approved, reject→draft, export→exporting, cancel→cancelled
approved: publish→publishing, export→exporting, cancel→cancelled
exporting: export_done→exported, fail→export_failed
export_failed: retry→exporting, cancel→cancelled
publishing: succeed→published, fail→publish_failed
publish_failed: retry→approved, cancel→cancelled
cancelled: reopen→draft
exported / published: 终态
```
- 不变量:导出失败(`export_failed`)重试只回到 `exporting`,不能绕过人工审核到达 `approved`;`approved` 仅来自人工 `approve` 或发布失败(`publish_failed`)后的重试;`reopen` 仅用于幂等重放的内部路径。
- 幂等:`idempotency_key = content_id + version + platform + account_id`,有意不包含 `scheduled_at`;改期走独立接口(`PATCH /publish/jobs/{id}/schedule`),且仅允许在执行前修改。
- 手动导出:可从 `draft`、`awaiting_review`、`approved` 发起导出;`exported` 仅记录被导出的版本,不等于已发布;导出失败重试不能绕过审核。
- 审核快照:审核通过时冻结内容版本,之后修改需重新审核。
- 定时发布:arq 定时任务在 `scheduled_at` 触发 precheck → 按模式决定自动发布或等审核。

#### 4.4.4 发布前检查(precheck,确定性 + LLM 两段)

1. 平台规则:标题/正文字数、图片数量与格式、视频时长与画幅。
2. 敏感词与风险词:本地词库(正规开源词表)+ 正则规则。
3. 版权提示:图片/音乐来源字段缺失时警告。
4. 人设一致性:persona-check 评分 < 80 时警告(不阻断)。
5. 结构完整性:标题、封面、正文、话题标签是否齐全。

输出 `PrecheckReport`(通过/警告/阻断三级),前端展示可逐项修正。

### 4.5 归因与画像回写

- **数据采集**:
  - 公众号:官方数据统计接口(阅读、分享、涨粉),每日定时拉取。
  - 其它平台:一期手动录入/CSV 导入(字段映射模板),二期探索官方开放数据或授权采集。
- **指标快照**:`analytics_snapshots` 按 `publish_receipt × 日期` 存储,保留原始 jsonb 以便指标扩展。
- **复盘报告**:`postmortem` 技能基于快照 + 画像生成结论(有效结构、失败原因、下步建议)。
- **回写**:报告确认后抽取记忆写入 `profile_memories`,标注数据来源,可人工编辑/删除。

### 4.6 内容日历与排期

- 日历条目:`idea / draft / scheduled / published / activity` 五种类型。
- 与 `publish_jobs.scheduled_at` 联动;拖拽改期 = 更新排期并重建定时任务。
- 节日/节点:一期内置静态节日表(JSON),二期接入 Easel `event-calendar` 方法论与外部数据源。

### 4.7 对话入口(受控 Agent)

- 会话与消息落库(`conversations` / `messages`),支持多会话、按项目绑定。
- SSE 流式:文本增量 + 工具调用状态事件(`skill_started / skill_progress / skill_done / error`)。
- 工具白名单 = 已启用技能 + 只读查询工具(项目/画像/内容检索),**不含**发布、删除、任意网络请求。
- 多轮参数补齐:缺参反问,最多 2 轮,超限降级为表单引导。

### 4.8 设置与模型网关

- 供应商配置:名称、协议(openai-compatible / anthropic)、base_url、api_key(加密)、可用模型列表。
- 模型档位映射:`fast / standard / premium` 各绑定一个模型,技能 manifest 只引用档位,便于全局换模型控成本。
- 连通性自测 + 模型列表拉取(参考 Easel Web 设置页交互)。
- API Key 使用 Fernet(密钥在环境变量)加密落库,前端只回显掩码。

---

## 5. 数据模型

> 所有业务表从 Day 1 带 `tenant_id`(MVP 固定 `default`),为 SaaS 演进预留。时间统一 `timestamptz`。

### 5.1 核心表

| 表 | 关键字段 | 说明 |
|---|---|---|
| `tenants` | id, name, plan, created_at | MVP 仅 default 一行 |
| `users` | id, tenant_id, email, password_hash, role | role: owner/editor/viewer(预留) |
| `profiles` | id, tenant_id, name, positioning, style, audience, platforms(jsonb), preferences_redlines(jsonb) | 画像主体 |
| `profile_memories` | id, profile_id, kind, content, source_ref, weight, embedding(vector, 二期) | 长期记忆 |
| `projects` | id, tenant_id, profile_id, title, topic, status, created_at | 一个选题 = 一个项目 |
| `contents` | id, project_id, platform, content_type, title, body_md, adapted(jsonb), status, current_version | 平台版本 |
| `content_versions` | id, content_id, body_md, assets(jsonb), change_note, created_at | 版本快照 |
| `assets` | id, project_id, content_id?, kind, storage_key, meta(jsonb) | 图片/音频/视频 |
| `platform_accounts` | id, tenant_id, platform, account_name, credentials_encrypted, status, last_verified_at | 登录态/密钥 |
| `publish_jobs` | id, content_id, platform_account_id, mode, status, scheduled_at, idempotency_key, precheck_report(jsonb) | 发布任务 |
| `publish_receipts` | id, publish_job_id, platform_post_id, url, published_at, raw(jsonb) | 发布回执 |
| `analytics_snapshots` | id, publish_receipt_id, captured_at, metrics(jsonb) | 指标快照 |
| `skills` | id, skill_id, name, layer, version, manifest(jsonb), enabled | 技能注册表 |
| `skill_runs` | id, skill_id, skill_version, content_id?, input(jsonb), output(jsonb), prompt_tokens, completion_tokens, cost, status, error, started_at, finished_at | 审计与成本 |
| `conversations` / `messages` | id, tenant_id, profile_id, title / id, conversation_id, role, content, tool_calls(jsonb) | 对话 |
| `calendar_entries` | id, profile_id, date, kind, content_id?, note | 内容日历 |
| `llm_providers` | id, tenant_id, name, protocol, base_url, api_key_encrypted, models(jsonb) | 模型供应商 |
| `settings` | tenant_id, key, value(jsonb) | 系统设置(模型档位映射等) |

### 5.2 关键约束

- `publish_jobs.idempotency_key` 唯一索引,防重复发布。
- `contents (project_id, platform, current_version)` 唯一。
- `analytics_snapshots (publish_receipt_id, captured_at::date)` 唯一,同日重复采集走 upsert。
- 删除策略:项目软删除(status=archived),对象存储保留 30 天后清理。

---

## 6. API 设计

### 6.1 约定

- Base:`/api/v1`;认证:`Authorization: Bearer <JWT>`;错误体:`{code, message, detail?}`。
- 列表接口统一分页:`?page=&page_size=`(最大 100)。
- 流式接口用 SSE(`text/event-stream`),事件类型:`message / progress / done / error`。

### 6.2 主要端点

| 模块 | 端点 | 说明 |
|---|---|---|
| 认证 | `POST /auth/login` `POST /auth/logout` `GET /auth/me` | MVP 邮箱 + 密码,JWT |
| 画像 | `GET/POST /profiles` `GET/PATCH/DELETE /profiles/{id}` `GET /profiles/{id}/memories` `DELETE /memories/{id}` | 记忆可人工删除 |
| 项目 | `GET/POST /projects` `GET/PATCH /projects/{id}` `POST /projects/{id}/archive` | |
| 内容 | `GET/POST /contents` `GET/PATCH /contents/{id}` `POST /contents/{id}/versions` `POST /contents/{id}/repurpose` | 多平台改写 |
| 技能 | `GET /skills` `POST /skills/{id}/run` `POST /skills/{id}/run/stream`(SSE) `GET /skill-runs` | 执行与审计 |
| 对话 | `POST /conversations` `GET /conversations/{id}/messages` `POST /chat/stream`(SSE) | 受控 Agent 入口 |
| 发布 | `POST /publish/jobs` `GET /publish/jobs` `GET /publish/jobs/{id}` `PATCH /publish/jobs/{id}/schedule` `POST /publish/jobs/{id}/actions` `POST /publish/jobs/{id}/precheck`(Task 1.13) `POST /publish/jobs/{id}/export`(Task 1.15) | 状态机驱动;审核通过为 `approve` 动作 |
| 账号 | `GET/POST /platform-accounts` `POST /platform-accounts/{id}/verify` | 公众号 AppID/Secret 绑定 |
| 归因 | `POST /analytics/import` `GET /analytics/snapshots` `POST /content/{id}/postmortem` | 手动/CSV 导入 + 复盘 |
| 日历 | `GET/POST /calendar` `PATCH/DELETE /calendar/{id}` | |
| 设置 | `GET/PUT /settings/llm-providers` `POST /settings/llm-providers/{id}/test` `GET /settings/models` | 密钥掩码返回 |

### 6.3 异步任务

- `POST /skills/{id}/run` 同步短任务(<30s)直接返回;长任务返回 `202 + task_id`,前端轮询或 SSE 订阅。
- 视频/批量处理一律走 arq worker,`skill_runs.status` 作为任务状态源。

---

## 7. 非功能性设计

### 7.1 安全

- 密码:argon2id;JWT 短有效期(24h),MVP 不做 refresh token。
- 密钥:LLM Key、平台 Secret、登录态一律 Fernet 加密落库,日志中打码。
- 技能脚本沙箱:限定工作目录、命令白名单、超时(默认 120s)、并发上限;禁止 `shell=True` 拼接用户输入。
- Prompt 注入防护:用户内容与系统指令分区;画像注入标记转义;工具白名单;输出渲染走 DOMPurify。
- 限流:登录 5 次/分钟;LLM 生成接口按租户并发上限(默认 2)。
- SSRF:仅允许配置化的供应商 base_url 域名白名单;工具层不开放任意 URL 抓取。

### 7.2 合规

- 发布默认「人工审核」模式;自动发布需账号级显式开启。
- 不内建逆向自动发布;实验性通道需二次确认 + 风险提示。
- 敏感词库 + 版权来源提示 + 生成内容标注「AI 辅助创作」选项。
- 数据留存:用户内容与登录态可一键删除;备份保留 30 天。

### 7.3 可观测

- 结构化日志(structlog JSON):request_id、tenant_id、skill_id、耗时。
- 成本报表:`skill_runs` 聚合 token 与费用,按技能/模型/天维度展示。
- 健康检查:`/healthz`(进程)、`/readyz`(DB/Redis/存储)。

### 7.4 测试

- 后端:pytest + pytest-asyncio,覆盖率目标 70%(核心状态机与 adapter 90%)。
- 技能 eval:`tests/*.prompt/expected` 冒烟,LLM 输出断言用关键字与结构校验,不比对全文。
- 前端:关键页面组件测试 + 播放式 E2E(Playwright 测自己的应用,与平台自动化无关)。
- CI:lint(ruff)+ 类型(mypy 基础检查)+ 单测 + 技能校验脚本。

### 7.5 性能与成本

- 流式优先,首 token 延迟为目标指标(<3s,取决于供应商)。
- 技能上下文预算:系统提示 + 画像 + references 合计 ≤ 4000 token(manifest 声明,校验脚本拦截超限)。
- 缓存:热榜/素材元数据短缓存;同一内容重复改写命中技能结果缓存(按内容 hash)。
- 模型档位化:低价值任务用 fast 档,创作主任务用 standard/premium。

---

## 8. 开发路线图

### Phase 0 — 脚手架(第 1 周)

- 仓库初始化、Docker Compose、Alembic 初始迁移、登录、LLM 供应商设置页、`/healthz`。
- **验收**:本地一键起服务;页面配置模型后流式对话返回内容。

### Phase 1 — MVP 闭环(第 2-5 周)

- 画像 CRUD + prompt 注入;技能执行器 + 首批 12 技能;创作会话;内容库;版本管理。
- 公众号发布全链路:素材上传 → 草稿 → 提交(默认人工审核);导出包(小红书/知乎/快手/视频号格式)。
- 归因:手动录入 + CSV 导入;复盘报告;画像记忆回写。
- **验收**:
  1. 从选题到公众号发布(人工审核)全流程 ≤ 10 分钟;
  2. 发布前检查能拦截超长标题、敏感词、缺封面;
  3. 复盘报告生成并可一键写回画像;
  4. 一键导出小红书/知乎格式包。

### Phase 2 — 平台与媒体扩展(第 6-8 周)

- 抖音企业资质就绪后接官方发布 API;B 站视频发布;视频号导出。
- 搬运视频/TTS/字幕技能(ffmpeg + edge-tts + faster-whisper + rembg)。
- 内容日历 + 定时发布;评论洞察;pgvector 记忆检索;热榜发现层接入。
- **验收**:定时发布可重放且幂等;视频技能产出可在内容库预览。

### Phase 3 — SaaS 化(第 9-12 周)

- 多租户隔离验证、租户管理、订阅计费、团队成员与权限、审计日志。
- **验收**:两个租户数据互不可见;计费与用量报表准确。

---

## 9. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 抖音/视频号等平台 API 资质不确定 | 自动发布延期 | 能力分层 + 导出兜底;资质提前申请,roadmap 不依赖单一平台 |
| LLM 输出不稳定 | 内容质量波动 | 结构化输出 + references few-shot + 质量门禁 + 默认人工审核 |
| Token 成本失控 | 运营成本高 | 档位化模型、上下文预算校验、缓存、成本看板 |
| 范围蔓延(113 技能诱惑) | 永远发不了版 | MVP 技能清单冻结;一次只搬被用到的技能 |
| 平台 API 变更 | 发布失败 | Adapter 隔离 + 契约测试 + 失败告警 |
| 单人维护 | 交付慢 | YAGNI;先跑通闭环再加能力;自动化测试保底线 |
| 逆向发布诱惑 | 封号/合规 | 默认关闭实验模块;文档与 UI 双提示 |

---

## 10. 附录

### 10.1 Easel 资产搬运映射

| 本项目路径 | 来源 |
|---|---|
| `skills/*/scripts/*.py`(卡片/图片/媒体) | `Easel/skills/builtin/{card-*,image-*,remove-bg,poster-hero}/scripts` |
| `skills/shared/`(ffmpeg 封装、通用工具) | `Easel/skills/shared/scripts` |
| `skills/*/references/*.md`(平台规则) | 对应 `Easel/skills/builtin/skill-*-publisher/SKILL.md` 与方法论文档 |
| `skills/*/tests/*` | 对应 Easel 技能 tests,改写断言格式 |
| `docs/platform-rules/` | 由 Easel 各 publisher skill 规则整理 |
| 二期:热榜源 | `Easel/skills/shared/hotlist-apis.md` |
| 二期:视频/TTS 脚本 | `Easel/skills/builtin/{ai-video-gen,tts-voiceover,auto-subtitle,video-editing,...}/scripts` |

搬运约定:保留 Apache 2.0 声明与出处注释;不引入其 OpenClaw 相关依赖;脚本入本项目后纳入 lint 与测试。

### 10.2 发布适配器伪代码

```python
class WeChatMPAdapter:
    platform = "wechat_mp"
    capability = "auto"

    async def precheck(self, account, content, profile):
        issues = []
        if len(content.title) > 64:
            issues.append(Issue(level="block", msg="标题超过 64 字"))
        issues += await scan_risk_words(content.body_md)
        issues += await persona_check(content, profile)   # 仅 warning
        return PrecheckReport(issues=issues)

    async def adapt(self, content, rules):
        html = render_markdown_to_wechat_html(content.body_md, content.assets)
        return AdaptedContent(title=content.title, html=html, images=content.images)

    async def publish(self, account, adapted):
        token = await self._get_access_token(account)     # 缓存至过期前 5 分钟
        media_ids = [await self._upload_image(token, img) for img in adapted.images]
        draft_id = await self._add_draft(token, adapted, media_ids)
        publish_id = await self._freepublish(token, draft_id)   # 需认证号权限
        return PublishReceipt(platform_post_id=publish_id)
```

### 10.3 参考资料

- Easel 仓库与文档:`SKILL-SPEC.md`、`docs/prompt-stack.md`、`skills/shared/hotlist-apis.md`
- 微信公众号开放文档:素材管理 / 草稿箱 / 发布接口 / 数据统计
- 抖音开放平台:视频发布能力与资质要求
- biliup 项目: B 站投稿工具
- 方案 C 方法论:确定性工作流 + 受控 Agent 的边界设计(本文件 §3.2)
