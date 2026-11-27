# 商业化计费设计（Phase 4）

**Goal:** 在现有 SaaS 基础上交付「可配置套餐 + 分项计量 + 月度账单」：平台管理员维护套餐并把套餐指派给租户，租户管理员查看本期套餐、分项用量与预计费用，每个自然月结束后由平台管理员关账生成不可变账单；LLM token（输入/输出分开）是唯一计费项，技能/Agent/工作流运行次数与媒体任务数、媒体时长作为用量指标展示。

**Architecture:** 套餐是平台级 `plans` 表（额度与价格均为 JSON 维度映射），租户通过 `tenants.plan_id` 绑定套餐。计量不新增记账表：`skill_runs`、`agent_runs.usage`、`workflow_runs`、`media_jobs` 已包含全部原始数据，按当期 UTC 时间范围即时聚合；账单在关账时把「套餐快照 + 用量 + 分项费用」固化到 `billing_statements`，关账后不再随源数据变化。计费口径集中在 `app/billing/` 的维度注册表与纯函数中，API 与前端都只消费这一份定义。

**Status:** 已实现

**Date:** 2026-11-25

---

## 1. 背景与决策

Phase 3 已交付多租户、角色权限、审计与团队成员，但 `Tenant` 只有 id/name/slug/status，套餐、额度、价格、账单均不存在；`docs/ops.md` 也把「无计费」列为已知限制。本期补齐商业化闭环的记账与结算部分，明确不做在线支付。

已确认的三项方向：

1. **商业化范围**：套餐 + 计量 + 账单/用量报表，不含在线支付、发票、退款。
2. **计量口径**：分项计量。LLM token 作为计费主项，prompt 与 completion 分开计价；技能/Agent/工作流运行次数、媒体任务数与媒体时长作为用量指标。
3. **套餐形态**：可配置套餐。`plans` 表存各维度额度与价格，平台管理员可创建、编辑、上下架并指派给租户，无需重新发版调整额度。

本期未逐项追问、由实现方裁定的口径（均已写入本文档，评审时可改）：

- **账期**：按北京时间（UTC+8，无夏令时）自然月切分，`period` 形如 `2026-11`。
- **超额行为**：软限额。超额不阻断任何功能，只在用量页与跨租户报表标记超额并计入费用；不做硬性熔断，避免在无支付体系时造成业务中断。
- **历史用量**：不单独回填。计量直接聚合源表，第一份账单自然包含当期已存在的历史运行记录。

## 2. 范围

### 2.1 本期实现

1. 维度注册表：计费维度与指标维度的 key、单位、展示名、`unit_size`、是否可计价。
2. `plans` 表与 `tenants.plan_id` 关联；内置 free/basic/pro/enterprise 四个种子套餐；创建租户自动分配默认套餐。
3. 用量聚合服务：按租户与账期汇总 token、运行次数、媒体指标，单一来源、互不重复计数。
4. 账单服务：按套餐快照计算分项费用与总额；关账写入 `billing_statements`，支持平台管理员重开重算。
5. 租户 API：当前套餐、本期用量与预计费用、账单列表与详情。
6. 平台 API：套餐 CRUD/上下架、租户套餐指派、跨租户用量总览、关账与重开。
7. 前端「账单」页：套餐卡、用量进度、预计费用、账单列表/详情；平台管理员的套餐管理与跨租户关账面板；租户列表增加套餐列与指派入口。
8. 审计埋点、后端单测、前端单测、文档与全量验证。

### 2.2 本期不做

- 在线支付、支付网关回调、发票、退款、优惠券、按年付费。
- 硬性额度熔断、自动停服、欠费催缴流程。
- 超额通知（站内信/邮件/短信）；超额只在用量页与跨租户报表中以标记呈现。
- 按日/按小时结算、滚动 30 天账期、租户自定义账期时区。
- 套餐级功能开关（例如某套餐禁用 Agent 团队）；本期套餐只控制额度与价格。
- 按项目/成员拆分的成本分摊。
- 修改历史账单的逐条调整单（只提供重开关账）。

## 3. 计量口径

### 3.1 维度注册表

`backend/app/billing/dimensions.py` 定义唯一权威列表，顺序即前端展示顺序：

| key | 展示名 | 单位 | `unit_size` | 可计价 | 数据来源 |
|---|---|---|---|---|---|
| `llm.prompt_tokens` | 输入 token | token | 1000 | 是 | `skill_runs.prompt_tokens` + `agent_runs.usage.prompt_tokens` |
| `llm.completion_tokens` | 输出 token | token | 1000 | 是 | `skill_runs.completion_tokens` + `agent_runs.usage.completion_tokens` |
| `run.skill` | 技能运行 | 次 | 1 | 否（默认无价） | `skill_runs` 行数 |
| `run.agent` | Agent 运行 | 次 | 1 | 否 | `agent_runs` 中 `mode in ('single','node','team')` 的行数 |
| `run.workflow` | 工作流运行 | 次 | 1 | 否 | `workflow_runs` 行数 |
| `media.jobs` | 媒体任务 | 次 | 1 | 否 | `media_jobs` 行数 |
| `media.seconds` | 媒体时长 | 秒 | 1 | 否 | `media_jobs` 成功任务的 `finished_at - started_at` 之和 |

规则：

- 维度 key 采用 `域.名称` 形式；`limits`/`prices` 中出现未注册 key 时拒绝（422）。
- 可计价只是默认语义：实际是否收费由套餐 `prices` 是否包含该维度决定；`prices` 缺失即单价 0。
- 未来新增维度只需在注册表加一项并提供聚合表达式；旧套餐缺该 key 时视为不限量/不收费。

### 3.2 数据来源与去重

- **token 只统计两处**：`skill_runs`（所有经 `SkillExecutor` 的直接运行，包括前端直跑、工作流技能节点、Agent 技能工具、对话、复盘、媒体与一稿多改）与 `agent_runs.usage`（Agent 编排器自身的 LLM 轮次）。`workflow_runs.usage` 汇总的是子运行消耗，**不计入**，否则与子 `skill_runs`/`agent_runs` 重复。
- 失败运行同样消耗 token，`skill_runs` 只要创建即计入；`prompt_tokens`/`completion_tokens` 默认 0，不影响无消耗的失败。
- `agent_runs` 的 `role` 子运行（团队角色）不计入 `run.agent` 次数，避免一次团队运行被放大为 5～6 次；其 token 仍计入（真实消耗）。
- 媒体时长只统计 `status="succeeded"` 且 `started_at`、`finished_at` 均非空的 `media_jobs`；按秒累加，前端以分钟展示（保留一位小数）。

### 3.3 账期与边界

- `period` 校验 `^\d{4}-(0[1-9]|1[0-2])$`。
- `period_bounds("2026-11")` 返回 `[2026-10-31T16:00:00Z, 2026-11-30T16:00:00Z)`，即北京时间 11 月 1 日 00:00 至 12 月 1 日 00:00。实现使用固定 `+08:00`，不依赖 tz 数据库。
- 聚合区间左闭右开：`created_at >= start AND created_at < end`。
- 当前月可查询实时用量；只有 `end <= now` 的已结束月份允许关账。

### 3.4 金额计算

单维度费用（整数分，无浮点）：

```
overage = max(0, used - included)          # included 缺省视为 0
amount  = ceil(overage * price_cents / unit_size)
        = (overage * price_cents + unit_size - 1) // unit_size
```

账单总额 = 套餐月费 `plan.price_cents` + Σ 各维度 `amount`。`price_cents` 表示「每个 `unit_size` 单位的价格（分）」，例如 prompt token 的 `price_cents=20` 即每千 token 0.20 元。所有金额字段为整数分，货币固定 `CNY`。

## 4. 数据模型

新增 `backend/app/models/billing.py`，迁移版本紧随当前 head `c9d0e1f2a3b4`。

### 4.1 `plans`（平台级，不按租户隔离）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | UUID 主键 | |
| `code` | String(50)，唯一 | `free`/`basic`/`pro`/`enterprise`，创建后不可改 |
| `name` | String(100) | 展示名 |
| `description` | Text，默认 `""` | 一句话说明 |
| `price_cents` | Integer，默认 0，非负 | 月费（分） |
| `currency` | String(8)，默认 `CNY` | 本期固定 CNY |
| `limits` | JSON，默认 `{}` | `{dimension_key: 额度}`；缺失=不限 |
| `prices` | JSON，默认 `{}` | `{dimension_key: 分/unit_size}`；缺失=不收费 |
| `status` | String(16)，默认 `active` | `active`/`archived`；归档后不可指派，已指派租户不受影响 |
| `created_at`/`updated_at` | TimestampMixin | |

`code` 不可修改；套餐不做物理删除，避免历史账单与租户外键悬空。

### 4.2 `tenants` 扩展

| 列 | 类型 | 说明 |
|---|---|---|
| `plan_id` | UUID，非空，外键 `plans.id`（`ON DELETE RESTRICT`） | 当前套餐 |
| `plan_changed_at` | DateTime(timezone=True)，非空，默认 now | 最近一次指派时间 |

迁移顺序：建 `plans` → 插入四个种子套餐 → 给每个租户写入 free 套餐 → 置 `plan_id` 非空。`create_tenant` 按配置 `DEFAULT_PLAN_CODE = "free"` 查表分配；默认套餐不存在时返回 500 `default_plan_missing`（部署配置错误）。

### 4.3 `billing_statements`（账单快照，`TenantScoped`）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | UUID 主键 | |
| `tenant_id` | String(36)，索引 | 租户隔离 |
| `period` | String(7) | `YYYY-MM` |
| `status` | String(16)，默认 `closed` | `closed`/`open`（重开后） |
| `plan_code`/`plan_name` | String | 关账时的套餐快照 |
| `plan_price_cents` | Integer | 关账时月费 |
| `currency` | String(8) | 默认 `CNY` |
| `limits`/`prices` | JSON | 关账时的额度与单价快照 |
| `usage` | JSON | `{dimension_key: int}` 全部维度用量 |
| `charges` | JSON | `[{dimension, used, included, overage, unit_size, price_cents, amount_cents}]` 仅含 `amount_cents > 0` 的维度 |
| `amount_cents` | Integer | 月费 + 分项费用 |
| `closed_by` | UUID，可空 | 关账平台管理员 |
| `closed_at` | DateTime(timezone=True)，可空 | 重开时清空 |
| `reopened_at` | DateTime(timezone=True)，可空 | 最近一次重开时间 |
| `created_at`/`updated_at` | TimestampMixin | |

唯一约束 `uq_billing_statements_tenant_period (tenant_id, period)`。源数据变化不会改写已关账行；只有重开后才允许重新关账覆盖该行。

租户删除沿用现有逻辑：`billing_statements` 含 `tenant_id`，会被通用清理循环删除；账单历史随租户一并消失，与现有「删除租户即清空数据」语义一致，需在文档说明。

## 5. 计费与关账流程

### 5.1 实时预览（无写入）

`GET /api/billing/usage?period=YYYY-MM` 每次请求：

1. 读取租户当前套餐；
2. 用维度注册表聚合账期内用量；
3. 按 3.4 计算各维度超额与费用、总额；
4. 返回每维度 `limit`（可空）、`used`、`remaining`、`percent`（上限存在时）、`over_limit`，以及 `amount_cents`。

未指定 `period` 时取北京时间当前月。查询任意历史月份不会因为该月未关账而被拒绝。

### 5.2 关账

`POST /api/billing/platform/statements/{tenant_id}/{period}/close`（平台管理员）：

- 账期必须已结束，否则 409 `period_not_finished`；
- 已有 `closed` 账单则 409 `statement_exists`；
- 已有 `open` 账单（重开状态）则重新计算并覆盖，状态恢复 `closed`；
- 计算套餐快照、用量、分项费用，写审计 `billing.close`，提交；
- 并发关账由唯一约束兜底，`IntegrityError` 转 409。

### 5.3 重开

`POST /api/billing/platform/statements/{tenant_id}/{period}/reopen`：

- 仅 `closed` 可直接重开；已 `open` 返回 409 `statement_already_open`；
- 置 `status="open"`、记录 `reopened_at`，并将 `closed_at`、`closed_by` 置空，写审计 `billing.reopen`；
- 只有 `open` 账单允许重新关账覆盖。

### 5.4 不变式

- 租户侧只能读取自己租户的套餐、用量与账单；跨租户访问一律 404（沿用现有隔离语义）。
- 已 `closed` 账单的金额与明细不随套餐编辑、运行记录删除、媒体任务清理而变化。
- 套餐归档或改价只影响之后的关账；历史账单保存完整快照。
- 所有金额为整数分；接口不返回浮点金额。

## 6. API

新增 `backend/app/api/billing.py`，`router = APIRouter(prefix="/billing", tags=["billing"])`，在 `main.py` 以 `/api` 前缀注册。错误一律使用 `AppError(code, message, status)`。

### 6.1 租户侧（`admin_access`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/billing/plan` | 当前套餐：`code`、`name`、`description`、`price_cents`、`currency`、`limits`、`prices`、`plan_changed_at` |
| GET | `/billing/usage?period=` | 套餐 + 账期 + 各维度用量/额度/占比/超额 + 分项费用 + 预计总额 |
| GET | `/billing/statements?limit=12` | 本租户账单列表（按 period 倒序，1–36） |
| GET | `/billing/statements/{period}` | 单月账单详情；未关账 404 `statement_not_found` |

### 6.2 平台侧（`require_platform_admin`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/billing/plans` | 套餐目录（含归档） |
| POST | `/billing/plans` | 创建套餐；`code` 冲突 409 `plan_exists` |
| PATCH | `/billing/plans/{code}` | 修改 name/description/price/limits/prices/status；`code` 不可改 |
| PATCH | `/billing/tenants/{tenant_id}/plan` | 指派套餐（body `{"plan_code": "..."}`）；归档套餐 409 `plan_archived`；写 `tenant.plan_change` 审计 |
| GET | `/billing/platform/usage?period=` | 跨租户总览：每家租户的套餐、token 用量、预计金额、账单状态 |
| POST | `/billing/platform/statements/{tenant_id}/{period}/close` | 关账 |
| POST | `/billing/platform/statements/{tenant_id}/{period}/reopen` | 重开 |

### 6.3 校验

- `limits`/`prices`：key 必须在维度注册表内；value 为非负整数；`prices` 只允许用于注册为可计价的维度（当前为两个 token 维度），否则 422 `plan_dimension_invalid`。
- 套餐 `code`：`^[a-z][a-z0-9-]{1,49}$`。
- 租户指派：目标租户不存在 404；`plan_code` 未知 404/422；归档 409。
- 账单 `period` 格式错误 422 `period_invalid`。
- 响应中不出现任何凭据、密钥或 LLM 配置。

## 7. 前端

### 7.1 导航

`web/src/auth.ts` 的 `NavIconKey` 增加 `"billing"`，`NAV_ITEMS` 增加：

```ts
{ to: "/billing", label: "账单", minRole: "admin", group: "manage", icon: "billing" }
```

`NavIcon.tsx` 增加对应图标分支（沿用现有线性图标风格）。

### 7.2 `web/src/billing.ts`

纯函数与类型集中在此，便于单测：

- `UsageRow`、`PlanOut`、`StatementOut`、`UsageOut` 类型；
- `formatCents(cents)` → `¥0.00`；
- `formatTokens(n)` → `1.2 万` / `3,400`；
- `formatSeconds(s)` → `12.5 分钟`；
- `rowPercent(used, limit)`、`isOverLimit(used, limit)`；
- `statementStatusLabel(status)`、`periodLabel(period)`。

### 7.3 `web/src/pages/Billing.tsx`

- 顶部月份选择器（最近 12 个月 + 当前月），默认当前月；
- 套餐卡：名称、月费、绑定时间；
- 用量区：每维度一行进度条（无限额显示「不限」），超额行红色标记；token 行显示输入/输出分开；
- 费用卡：月费 + 预计超额 + 合计；
- 账单表：月份、金额、状态、关账时间，点击展开明细（分项 charges）；
- 平台管理员额外显示：
  - 套餐管理：列表 + 新建/编辑抽屉（额度与单价按维度输入，token 单价以「元/千 token」输入并换算成分）；
  - 跨租户总览：租户、套餐、token、预计金额、账单状态；行内「关账 / 重开」按钮，二次确认；
- 只读或失败场景沿用 `EmptyState` 与内联错误；写操作遵守 `canAdmin` 收敛。

### 7.4 租户列表

`Tenants.tsx` 增加「套餐」列与编辑弹窗中的套餐下拉（数据来自 `/billing/plans`），保存调用 `PATCH /billing/tenants/{id}/plan`；`web/src/types.ts` 的 `Tenant` 增加 `plan_code`、`plan_name`。

## 8. 审计与安全

- 审计动作：`plan.create`、`plan.update`、`tenant.plan_change`、`billing.close`、`billing.reopen`；`detail` 只含 `code`/`period`/`tenant_slug`/`amount_cents` 等非敏感信息。
- 所有平台接口先过 `require_platform_admin`；租户接口走 `admin_access` 并在查询中强制 `tenant_id == user.tenant_id`。
- 账单与用量响应不含 API Key、LLM 配置、请求内容；`usage` 只有整数计数。
- 关账/重开属于财务动作，仅平台管理员可执行，UI 二次确认。

## 9. 测试策略

后端（`backend/tests/`）：

1. `test_billing_dimensions.py`：注册表完整性、金额取整（含 0 超额、整倍数、向上取整边界）、维度校验。
2. `test_billing_usage.py`：构造两租户的 `skill_runs`/`agent_runs`/`workflow_runs`/`media_jobs`，验证分项汇总、`role` 子运行不计次数、token 不重复计数、媒体只计成功任务、账期 UTC+8 边界（月初/月末各一条）。
3. `test_billing_api.py`：套餐 CRUD 权限（viewer/editor 403、平台管理员 200）、`code` 冲突、归档指派 409、租户指派与审计、跨租户 404、`period` 校验。
4. `test_billing_statements.py`：未结束账期 409、关账快照不受后续套餐修改与源数据删除影响、重复关账 409、重开后重算、并发关账唯一约束、租户删除后账单清理。
5. `test_tenants.py` 增补：创建租户自动绑定 free 套餐、`TenantOut` 含套餐字段。

前端（`web/src/__tests__/billing.test.ts` 及页面测试）：

- 纯函数：金额/千分位/token/时长格式化，超额与百分比边界，状态文案；
- 页面：加载渲染套餐与用量、超额标记、平台面板按 `is_platform_admin` 显隐、保存套餐的请求体换算（元/千 token → 分）。

全量验证：`cd backend && uv run pytest -q`、`uv run ruff check .`、`alembic upgrade head`（真实 Postgres）与 downgrade/upgrade 往返；`cd web && npm run test && npm run build`。

## 10. 验收标准

1. 平台管理员能创建/编辑/归档套餐，并把任意租户切到指定套餐，操作全部落审计。
2. 新建租户自动获得 free 套餐；租户管理员在「账单」页看到当前套餐、上月与本月的分项用量、超额标记与预计金额。
3. 两个租户的用量与账单互不可见；任一租户的 editor/viewer 访问账单接口返回 403。
4. 已结束月份关账后生成账单，金额等于「月费 + Σ 分项向上取整费用」；随后修改套餐价格、删除运行记录，账单数值不变。
5. 重开后重新关账能反映当前源数据；并发关账只有一个成功。
6. 自动化测试与前端构建全绿；`docs/ops.md`、README 与开发文档中的「无计费」限制更新为本期已交付、仍需人工对账。

## 11. 风险与取舍

| 风险 | 影响 | 取舍 |
|---|---|---|
| 即时聚合在大租户上变慢 | 用量页响应变慢 | 本期按单租户单月范围查询并依赖 `tenant_id + created_at` 索引；数据量增长后再引入汇总表 |
| 软限额无法阻止欠费 | 收入风险 | 无支付体系前先保证业务连续性；平台管理员可在跨租户报表看到超额并人工处理 |
| 源数据删除导致未关账月份用量下降 | 当月数字波动 | 只影响未关账月份；关账即固化，文档说明「月初对账」流程 |
| 时区口径固定 UTC+8 | 海外租户账单边界不符直觉 | 本期产品面向国内运营团队；未来如需租户时区，作为独立迁移引入 |
| token 口径依赖两个来源 | 新增运行类型可能漏计 | 维度注册表集中于 `app/billing/`，新增运行类型时必须补聚合与测试，评审清单固化该检查项 |

## 12. 实现约束

- 依赖命令：`cd backend && uv run pytest -q`、`uv run ruff check .`、`uv run alembic ...`；不得使用裸 `python`/`pytest`/`alembic`。
- 迁移必须可在真实 PostgreSQL 16 上 `upgrade head`，并提供可回退的 `downgrade`（先删账单、再删租户套餐列、最后删套餐表）。
- 不改动 `.env` 中的既有凭据，不新增任何对外网络调用；所有计算在本地完成。
- 复用现有 `record(...)` 审计、`AppError`、`TenantScoped`、`get_session` 与角色依赖，不引入新框架或任务队列。
