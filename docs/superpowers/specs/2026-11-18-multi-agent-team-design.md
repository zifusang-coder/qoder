# 多 Agent 团队设计（第 3 层）

**Goal:** 在第 1 层 Agent 运行时与第 2 层工作流之上增加多 Agent 团队：Planner（Lead）、Scout、Creator、Critic、Publisher 五个内置角色围绕一个目标串行协作，通过追加式黑板共享上下文，Critic 拥有上限两轮的硬否决，团队既可作为工作流节点参与定时/审批自动化，也可从 AI 助理页直接发起；所有角色复用同一套工具注册表、策略引擎与审批语义，真发布仍然结构上不可达。

**Architecture:** 一次团队运行是一个 `agent_runs` 父记录（`mode="team"`），每个角色的一次执行是一个 `agent_runs` 子记录（`mode="role"`、`parent_run_id` 指向父、`role` 标明角色）。新增 `app/agent/team.py` 编排器代替新的执行内核：它以结构化输出驱动 Lead 决策，串行调用既有 `run_agent`（与第 2 层 Agent 节点相同的进程内调用方式），把每一步结果追加到 `agent_team_entries` 黑板，并按团队预算、轮次与返工上限收敛。角色与租户配置由代码内角色白名单 + `agent_team_roles` 覆盖共同决定，租户只能收窄工具与调低预算。

**Status:** 已实现。

**Date:** 2026-11-18

---

## 1. 背景

第 1 层已交付单 Agent 自主执行（工具调用循环、策略引擎、审批、运行记录、usage 记账），第 2 层已交付可视化工作流（节点引擎、React Flow 画布、版本与定时、Agent 节点复用第 1 层运行时）。第 2 层验证了一个关键形状：父流程创建子 `AgentRun` 后在同一 worker 内调用 `run_agent`，子运行挂审批时父流程同步挂起，审批结算后从断点续跑。第 3 层沿用该形状，把「一次角色执行」当作子运行，把「Lead 决策」当作编排器的一轮。

对比参照 Easel（ZJU-REAL/Easel，Apache 2.0），差的是多 Agent 协作而非执行内核；因此本期不引入第三方 Agent 框架，也不新增第二个运行时。

## 2. 范围

### 2.1 本期实现

1. 五个内置角色及其工具白名单、默认预算与系统提示词（代码内定义，不可在线编辑提示词）。
2. 父运行 + 子运行的数据模型与迁移：`agent_runs` 增加 `parent_run_id`、`role`、`team_state`；新增 `agent_team_entries`（黑板）与 `agent_team_roles`（租户角色配置）。
3. 编排器：Lead 结构化决策、串行委派、强制 Critic 审稿与两轮返工上限、循环检测、预算收敛、断点续跑、取消。
4. 子运行审批：挂起、批准续跑、驳回、过期，以及父团队的唤醒与对账兜底。
5. 团队 API 与租户角色配置 API；worker 按 `mode` 分派；对账扩展。
6. 工作流新增 `team` 节点类型、内置「内容团队」模板、画布与检查器支持、运行详情映射。
7. AI 助理页团队入口、团队运行详情（角色时间线 + 黑板 + 产物）、角色配置页。
8. 后端单测、前端单测、可选真实模型 E2E、真实环境浏览器验收。

### 2.2 本期不做

- 角色并行执行（一期严格串行，同一时刻最多一个子运行在跑）。
- 租户自定义角色提示词或新增角色（角色集合固定为 5 个）。
- 团队嵌套团队、角色调用工作流、工作流调用工作流。
- 订阅计费与成本结算（继续只记 usage，复用 `agent_runs.usage`）。
- 真发布、排期、导出工具（注册表不提供，任何角色不可达）。
- Agent 自主抓取外部网页或平台数据。
- 团队运行的手动 retry 接口（失败后重新发起；对账重投不等于用户 retry）。

## 3. 角色定义

角色是「系统提示词 + 工具子集 + 预算」的组合，不是新的执行内核。内置定义位于 `backend/app/agent/roles.py`。

| 角色 | 定位 | 工具白名单 | 默认预算（tokens / 工具调用 / 步数 / 秒） |
|---|---|---|---|
| `planner` | Lead：首轮出计划，之后决定派谁、是否返工、何时交付 | `project.get`、`project.list`、`content.get`、`content.list`、`profile.get`、`profile.list`、`platform.capabilities`、`analytics.summary`、`calendar.list`、`publish.job.get`、`publish.job.list` | 12000 / 4 / 4 / 180（按每次 Lead 决策计量） |
| `scout` | 调研：查画像、历史内容、数据表现与平台能力，产出事实与约束 | `planner` 白名单 + `media.job.get` | 20000 / 8 / 6 / 240 |
| `creator` | 创作：建项目、新建内容、写内容版本、跑创作技能、生成媒体 | `project.create`、`project.get`、`project.list`、`content.get`、`content.list`、`content.create`、`content.create_version`、`profile.get`、`profile.list`、`skill.run`、`media.generate`、`media.job.get`、`platform.capabilities` | 40000 / 10 / 16 / 360 |
| `critic` | 审稿：只读评审，输出通过/不通过与问题清单 | `content.get`、`content.list`、`platform.capabilities`、`publish.job.get`、`publish.job.list`、`analytics.summary` | 15000 / 4 / 4 / 180 |
| `publisher` | 交付：建发布草稿并预检；不排期、不发布 | `content.get`、`content.list`、`platform.capabilities`、`publish.create_draft_job`、`publish.precheck`、`publish.job.get`、`publish.job.list` | 12000 / 6 / 8 / 180 |

约束：

- `planner` 是 Lead，隐含参与且不可关闭；它不以子运行形式执行，每次决策是一次轻量 LLM 调用，按上表限额计量。`creator` 是唯一必选执行角色，租户不可全局禁用。
- `scout`、`critic`、`publisher` 可被租户关闭，也可在发起运行时按次勾选。
- 角色工具只能是内置白名单的子集；租户与运行请求都不能新增工具。
- 所有角色工具当前风险均为 `read` 或 `low`；租户把某个工具收紧为 `confirm`/`deny` 后，角色继承该策略。
- 角色提示词由代码版本管理，随发布更新，不做租户级编辑。

## 4. 数据模型

### 4.1 `agent_runs` 扩展

| 列 | 类型 | 说明 |
|---|---|---|
| `parent_run_id` | UUID，可空，外键 `agent_runs.id`，索引 | 子运行指向团队父运行；单 Agent 与工作流节点子运行为空 |
| `role` | String(16)，默认 `""` | 子运行角色；父运行与普通运行留空 |
| `team_state` | JSON，默认 `{}` | 仅团队父运行使用；断点续跑状态，见 4.4 |

`mode` 取值扩展为：`single`（默认）、`node`（工作流 Agent 节点子运行）、`team`（团队父运行）、`role`（团队子运行）。

### 4.2 `agent_team_entries`（黑板）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | UUID 主键 | |
| `tenant_id` | UUID | 租户隔离 |
| `run_id` | UUID，外键 `agent_runs.id`，索引 | 团队父运行 |
| `seq` | Integer | 单调递增，从 1 开始 |
| `kind` | String(16) | `goal` / `plan` / `delegation` / `result` / `critic` / `decision` / `final` |
| `role` | String(16)，默认 `""` | 相关角色；目标与最终交付可为空 |
| `content` | JSON | 已脱敏、截断的条目内容 |
| `created_at` | DateTime | 服务端时间 |

唯一约束 `(run_id, seq)`。条目只追加，不更新、不删除。角色本身不直接写黑板；编排器在每步结束后代表 Lead 追加条目，保证顺序确定、可回放。

### 4.3 `agent_team_roles`（租户角色配置）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | UUID 主键 | |
| `tenant_id` | UUID | |
| `role` | String(16) | `planner` / `scout` / `creator` / `critic` / `publisher` |
| `enabled` | Boolean，默认 true | `planner`、`creator` 强制 true |
| `tools` | JSON | 生效工具列表，必须是内置白名单子集 |
| `budget` | JSON | 生效预算，每项不得高于内置默认值 |
| `created_at` / `updated_at` | DateTime | |

唯一约束 `(tenant_id, role)`。租户无记录时读取代码默认值；保存时只写与默认值不同的覆盖。

### 4.4 `team_state` 结构

```json
{
  "turn": 3,
  "current_role": "scout",
  "current_child_id": "uuid | null",
  "rework": 1,
  "critic_verdicts": [{"child_id": "uuid", "verdict": "fail", "issues": ["..."]}],
  "artifact_index": [{"kind": "content_version", "id": "uuid", "title": "...", "role": "creator", "seq": 7}],
  "last_seq": 9
}
```

`artifact_index` 由父运行在每次子运行成功后合并子运行 `result.artifacts` 得出，用于黑板注入与最终交付；完整产物仍以子运行记录为准。

### 4.5 迁移

新增一条 Alembic 迁移：`agent_runs` 三列 + 索引，两张新表与唯一约束。迁移保持单一 head，不改动既有列语义，不回填历史数据（历史行 `role=""`、`team_state={}`）。

## 5. Lead 编排循环

### 5.1 单轮流程

1. 载入父运行；非 `queued`/`waiting_approval` 的终态直接返回，保证任务幂等。
2. 置 `running`、写 `started_at`、启动心跳。
3. 载入租户默认 provider；缺失则 `failed` / `llm_not_configured`。
4. 启动前做一次日配额预检；团队与子运行都受既有配额约束。
5. 若存在 `current_child_id` 且子运行非终态：先恢复该子运行（见 5.5），再根据结果进入下一步。
6. 否则调用 Lead（`planner` 提示词 + 当前黑板注入）获得一次结构化决策。
7. 按决策执行：委派角色、返工、或交付；把结果追加黑板；更新 `team_state` 并提交。
8. 重复 6–7，直到命中终止条件。

轮次口径：一次 Lead 决策及其后至多一次角色执行算一轮；Creator 返工与强制 Critic 审稿各自计入一轮。团队 `max_steps` 约束的就是这个轮次。

### 5.2 决策契约

Lead 每轮只输出一个 JSON 对象：

```json
{"action": "delegate", "role": "scout", "task": "收集近三个月同类选题的数据表现", "reason": "先确认选题方向"}
{"action": "rework", "role": "creator", "task": "按问题清单修改标题与开头", "reason": "Critic 首轮未通过"}
{"action": "finalize", "summary": "已产出内容版本与发布草稿", "reason": "审稿通过"}
```

校验规则：

- `action` 必须是三者之一；`delegate` 的 `role` 必须已启用且属于本次参与角色；`rework` 的 `role` 只能是 `creator`。
- `task` 非空、长度不超过 2000 字符；`summary` 不超过 2000 字符。
- 输出不可解析或校验失败时，向 Lead 回填一条纠错观察并重试一次；连续两次失败 → `failed` / `team_lead_invalid`。
- 同一轮 Lead 调用计入团队轮次与 token 预算。

### 5.3 执行一个角色

1. 按角色生效配置解析工具子集与预算，创建子运行（`mode="role"`、`parent_run_id`、`role`、`goal=task`、`budget=角色预算`、`policy` 为父运行策略快照）。
2. 同进程调用 `run_agent`，工具注册表传入 `subset(角色工具)`；与工作流 Agent 节点完全相同的调用形状。
3. 子运行 `succeeded`：把 `result_text` 摘要与产物引用合并到父运行，追加黑板 `result` 条目；子运行 `usage` 不并入父记录，单独记账（见第 6 节）。
4. 子运行 `waiting_approval`：父运行转 `waiting_approval`、写心跳并返回，等待审批唤醒。
5. 子运行 `failed`/`cancelled`：父运行立即失败，`error_code` 取子运行的错误码（缺省 `role_failed`），黑板与已产产物保留。

### 5.4 Critic 强制审稿

- 每次 `creator` 子运行成功后，若 `critic` 已启用，编排器必须运行一次 Critic，不依赖 Lead 主动派发。
- Critic 输出 `{"verdict": "pass|fail", "issues": ["..."]}`；不可解析时纠错重试一次，仍失败 → `failed` / `critic_invalid`。
- `fail` 且 `rework < 2`：`rework += 1`，编排器强制 `rework` 到 Creator，任务附带问题清单。
- `fail` 且 `rework == 2`：`failed` / `critic_rejected`，保留草稿、问题清单与黑板。
- `pass`：追加 `critic` 条目，控制权回到 Lead，可 `publisher` 或 `finalize`。
- `critic` 被关闭时跳过强制审稿，Lead 可直接交付；`team_state.critic_verdicts` 记录实际发生的裁决。

### 5.5 恢复与幂等

- worker 崩溃后父运行心跳过期，对账重新入队；编排器读取 `team_state.current_child_id`：
  - 子运行 `succeeded`：走 5.3 的成功分支，不重复委派；
  - 子运行 `waiting_approval`：父运行重新挂起，不重复创建审批；
  - 子运行 `queued`/`running`/`interrupted`：再次调用 `run_agent` 续跑；已 `ok` 的步骤由运行时复用，工具写操作按 `(run_id, step_index)` 幂等；
  - 子运行 `failed`/`cancelled`：父运行失败。
- 每一步黑板追加与 `team_state` 更新在同一事务提交；`(run_id, seq)` 唯一约束防止重启后重复追加。
- Lead 决策本身不入库为可重放调用；恢复时以「当前子运行是否终态」判定是否重新决策，已完成步骤不会重放。

### 5.6 终止条件

| 条件 | 结果 |
|---|---|
| Lead `finalize` | `succeeded` |
| 团队轮次达到 `max_steps` | `failed` / `team_budget_exhausted` |
| token/工具调用/墙钟超团队总预算 | `failed` / `team_budget_exhausted` |
| 角色子运行失败 | `failed` / 子运行错误码，缺省 `role_failed` |
| Critic 两轮返工仍不通过 | `failed` / `critic_rejected` |
| Critic 输出两次不可解析 | `failed` / `critic_invalid` |
| 同一 `(role, 规范化 task)` 连续 3 次 | `failed` / `team_loop_detected` |
| 用户取消 | `cancelled` |

父运行 `result` 结构：

```json
{
  "summary": "交付摘要",
  "roles": ["scout", "creator", "critic", "publisher"],
  "artifacts": [{"kind": "content_version", "id": "uuid", "title": "..."}],
  "critic": {"verdict": "pass", "rework_rounds": 1, "issues": []},
  "team": {"turns": 5, "children": 6}
}
```

## 6. 预算与用量

| 层级 | 默认值 | 约束 |
|---|---|---|
| 团队默认 | 120000 tokens / 40 工具调用 / 12 轮 / 900 秒 | 发起请求可在 `tokens ≤ 200000`、`tool_calls ≤ 60`、`turns ≤ 20`、`seconds ≤ 1800` 内调整 |
| 单角色 | 见第 3 节表格 | 租户配置只能调低；每次角色执行独立计量 |

- 角色预算按「每次执行」生效；返工与多次委派各自消耗角色预算。`planner` 不以子运行执行，其预算上限约束每次 Lead 决策调用。
- 团队总预算是跨角色、跨轮次的硬上限；统计口径 = Lead 用量 + 所有子运行用量之和。
- 父运行 `usage` 只记录 Lead 与编排开销（`prompt_tokens`、`completion_tokens`、`steps`、`elapsed_ms`、`paused_ms`）；子运行各自记账。这与既有日配额查询天然兼容：每个子运行是一条普通 `agent_runs` 记录，父运行不重复计入子用量。
- 展示与预算判断使用实时汇总（子运行数量 ≤ 5 × 返工轮次，查询代价可忽略）。
- 审批等待时间记入父运行与子运行的 `paused_ms`，不计入墙钟预算。

## 7. 审批、唤醒与对账

- 子运行命中 `confirm` 时写 `agent_approvals`（归属子运行），子运行转 `waiting_approval`，父运行同步转 `waiting_approval` 并返回。
- 审批人沿用第 1 层：admin/owner，且不能是发起人；审批卡通过子运行的 `parent_run_id` 展示「团队 · 角色」上下文。
- 审批决定后：
  - 若审批属于团队子运行（子运行有 `parent_run_id`），**不**单独入队子运行；改为唤醒父运行。
  - 唤醒逻辑新增 `_wake_parent_team`：以 `FOR UPDATE` 锁定父运行、`populate_existing` 重读；若父运行仍 `waiting_approval` 且该团队所有子运行均无 pending 审批，则结算 `paused_ms` 并 CAS `waiting_approval → queued`，再入队一次；否则回滚返回。锁顺序为父运行 → 审批行，与工作流审批决策一致。
  - 审批驳回：子运行落 `failed` / `approval_rejected`，父运行失败并保留现场。
- 对账扩展：
  - 扫描 stale `running` 的团队父运行并按既有重投路径重新入队；`mode="role"` 的子运行由父驱动，**不**单独重投。
  - 父运行 `waiting_approval` 且团队零 pending：若至少有一条 `approved` 审批，重新排队；否则 `failed` / `approval_expired`，与工作流终审修复后的语义一致。
  - 父运行已终态时，把残留的 `queued`/`waiting_approval` 子运行收敛为 `cancelled`，`running` 子运行标 `interrupted` 后置 `cancelled`。
- 取消父运行：打断当前子运行（`running` 置 `interrupted`），其余非终态子运行置 `cancelled`，父运行 `cancelled`；不遗留可被再次领取的子运行。

## 8. 安全与权限

| 操作 | admin/owner | editor | viewer |
|---|---|---|---|
| 查看团队角色配置 | 是 | 是 | 是 |
| 修改团队角色配置 | 是 | 否 | 否 |
| 发起团队运行 | 是 | 是 | 否 |
| 查看团队运行与黑板 | 是 | 是 | 是（读接口） |
| 取消团队运行 | 任意 | 仅本人发起 | 否 |
| 审批子运行 | 是（非发起人） | 否 | 否 |

- 子运行工具 = 内置角色白名单 ∩ 租户配置 ∩ 父运行策略快照；三层取交集，租户只能收窄。
- 策略快照在父运行创建时写入并复制到每个子运行，后续配置变化不影响已开始的团队。
- `publish.create_draft_job` 仍强制 `status=draft`、`scheduled_at=None`；注册表没有排期、真发布或导出工具，Publisher 结构上不可越界。
- 黑板条目入库前复用既有脱敏与截断工具：单条超过 4096 字节截断，密钥类内容不得出现。
- 团队生命周期写既有审计事件（创建、取消、失败），角色配置更新写独立审计事件；黑板与运行详情不返回任何原始凭据。
- 所有查询按 `tenant_id` 过滤；跨租户访问返回 404。

## 9. API

前缀沿用 `/api`。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/agent/team/roles` | viewer | 返回五个角色的生效配置：`role`、`label`、`enabled`、`tools`、`default_tools`、`budget`、`limits` |
| PUT | `/agent/team/roles` | admin/owner | 请求 `{roles: [{role, enabled, tools?, budget?}]}`；只能收窄/调低，越权返回 `422 policy_not_loosenable`；`planner`/`creator` 不可禁用 |
| POST | `/agent/team/runs` | editor | 请求 `{goal, roles[], budget?}`；`roles` 为参与的执行角色（不含 planner），`creator` 必选；返回 `202` + `RunOut`（`mode="team"`） |
| GET | `/agent/team/runs/{id}` | viewer | 返回父运行摘要、子运行列表（`id`、`role`、`status`、`usage`、`result_text` 摘要、`error_code`）、黑板条目、产物索引 |
| POST | `/agent/team/runs/{id}/cancel` | 发起人或 admin | 返回 `RunOut` |
| GET | `/agent/runs` | viewer | 兼容既有列表；`RunOut` 增加 `role`、`parent_run_id` 可空字段 |
| POST | `/agent/approvals/{id}/decide` | admin/owner | 沿用既有接口；团队子运行走父团队唤醒分支 |

校验与错误码：

- `roles` 为空、含未知角色、含未启用角色或缺 `creator` → `422 team_roles_invalid`。
- 预算越界或非正数 → `422 budget_invalid`。
- 角色配置试图新增工具、提高预算或禁用 `planner`/`creator` → `422 policy_not_loosenable`。
- 跨租户或不存在 → `404 not_found`。
- 团队已终态时取消 → `409 run_not_cancellable`。
- 既有 `POST /agent/runs/{id}/retry` 对 `mode="team"` 返回 `409 run_not_retryable`；团队失败后重新发起（对账重投不属于用户 retry）。

## 10. Worker 与工作流

### 10.1 Worker

- `run_agent_job` 按 `run.mode` 分派：`team` → `app/agent/team.py::run_team`，其余 → 既有 `run_agent`。
- 团队父运行与普通运行共用入队、心跳与对账扫描；`mode="role"` 子运行不直接入队，只由编排器调用。
- 审批决定后团队子运行不单独入队；由 `_wake_parent_team` 唤醒父运行。

### 10.2 工作流 `team` 节点

- `NODE_KINDS` 增加 `team`；节点数据：

```json
{"type": "team", "roles": ["scout", "creator", "critic", "publisher"], "goal": "为 {{input.topic}} 产出一篇内容与发布草稿", "budget": {"max_tokens": 120000}}
```

- 图校验：`roles` 非空、`creator` 必选、角色属于内置集合；`goal` 支持既有变量解析；预算字段与团队默认上限一致。
- 引擎执行：以工作流运行者为发起人、策略快照来自工作流运行，创建父运行（`workflow_run_id`、`workflow_node_id`）并同步调用 `run_team`；`node_run.agent_run_id` 复用于记录团队父运行。
- 状态映射：团队 `waiting_approval` → 节点 `waiting` 且工作流挂起；`succeeded` → 节点 `ok` 并写入父运行汇总产物；`failed` → 交给节点重试策略，重试沿用同一父运行断点，不重头创建。
- 发布时策略：无排期/真发布工具可配置；团队节点的审批仍发生在子运行层。
- 内置模板新增「内容团队」：输入 → team（scout/creator/critic/publisher）→ 输出，可发布后手动或定时运行。

## 11. 前端

- **AI 助理页 `/assistant`**：新增「协作团队」页签（避免与成员管理页 `/team` 重名）。发起面板包含目标输入、角色勾选（Creator 固定选中、Planner 显示为锁定 Lead）、可展开预算；列表以「协作团队」徽标区分 `mode="team"` 运行。
- **团队运行详情 `/assistant/teams/:id`**：父运行状态与预算用量；角色时间线把每个子运行渲染为卡片（角色、状态、尝试次数、耗时、产出摘要）；黑板按条目类型展示时间流；产物区列出内容版本与 `draft` 发布任务链接；审批卡带「团队 · 角色」标签。轮询间隔 2 秒，与工作流运行详情一致。
- **角色配置页**：admin/owner 可见；每角色启停、工具勾选（只能取消，不能新增）、每角色预算；保存失败展示后端错误码与提示。
- **工作流画布**：节点面板新增「团队」；检查器配置角色、目标模板与预算；校验错误在画布与保存接口两处提示；运行详情中团队节点显示子团队运行链接，`waiting` 视觉与 Agent 节点一致。
- **权限**：editor 可发起与取消自己的团队运行；viewer 无页面入口（与第 2 层口径一致），读接口按角色开放；角色配置入口仅 admin/owner 可见。

## 12. 配置

新增 Settings 字段（`backend/app/config.py` 与 `.env.example`）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `agent_team_max_turns` | 12 | 团队默认轮次上限 |
| `agent_team_rework_limit` | 2 | Critic 否决后的返工上限 |
| `agent_team_blackboard_inject` | 12 | 黑板注入的最近条目数 |
| `agent_team_max_tokens` | 120000 | 团队默认 token 预算 |
| `agent_team_max_tool_calls` | 40 | 团队默认工具调用预算 |
| `agent_team_max_seconds` | 900 | 团队默认墙钟预算 |
| `agent_team_token_ceiling` | 200000 | 发起请求可调的 token 上限 |
| `agent_team_seconds_ceiling` | 1800 | 发起请求可调的墙钟上限 |

角色默认预算与工具白名单属于角色定义常量，不放进环境变量；租户覆盖存 `agent_team_roles`。

## 13. 测试

| 文件 | 覆盖 |
|---|---|
| `tests/test_team_orchestrator.py` | 委派校验、Creator 必选、Critic 强制审稿与 0/1/2 轮返工、返工超限、循环检测、预算累计与耗尽、黑板截断与最近 N 条注入、取消、崩溃断点续跑、子运行失败传播 |
| `tests/test_team_approvals.py` | 子审批挂起父挂起、批准后父只排队一次、兄弟审批仍 pending 时不排队、驳回落 `approval_rejected`、重复决定幂等 |
| `tests/test_team_api.py` | 角色配置读写权限与收窄校验、发起校验（未选 Creator、角色禁用、预算越界）、列表/详情/取消角色矩阵、双租户隔离 |
| `tests/test_workflow_team_node.py` | 节点校验、引擎成功执行、审批挂起与续跑、失败重试沿用断点、产物汇总、策略快照继承、定时触发 |
| `web/src/__tests__/team.test.ts` | 角色选择与预算校验纯函数、详情时间线/黑板分组、配置页校验 |
| `tests/test_team_e2e.py` | `RUN_TEAM_E2E=1` 真实模型：目标 → 团队产出内容版本与 `draft` 发布任务（未排期），默认跳过 |

约定：编排测试用脚本化网关返回固定 Lead/Critic JSON，不依赖真实模型；审批与唤醒测试用确定性时序，不写 Postgres 专属并发测试；工作流节点测试复用既有引擎夹具与 SQLite 单连接约束。

## 14. 验收标准

1. editor 可在助理页选角色与预算发起团队运行；子运行串行执行，黑板与角色时间线可回放。
2. Creator 交付后 Critic 自动审稿；不通过强制返工且最多 2 轮，超限落 `critic_rejected` 并保留草稿与问题清单。
3. 租户关闭 Scout/Critic/Publisher、收窄工具、调低预算均生效；试图放宽返回 `policy_not_loosenable`；`planner`/`creator` 不可关闭。
4. 任一角色失败或团队超预算时运行失败，`error_code` 可区分 `role_failed`、`team_budget_exhausted`、`critic_rejected`、`team_loop_detected`，黑板与已产产物保留。
5. 子运行审批挂起、批准续跑、驳回失败、过期失败全链路正确；并发决定不会把父团队卡死。
6. 工作流「内容团队」模板手动与定时运行均产出内容版本与 `draft` 发布任务（`scheduled_at` 为空）；团队节点审批挂起后可从节点续跑。
7. viewer 只读、editor 不能改角色配置、双租户隔离；真发布与排期结构上不可达；黑板与 API 响应不含原始凭据。
8. 后端全量测试、`ruff check`、前端 `npm test` 与 `npm run build` 通过；`RUN_TEAM_E2E=1` 真实模型用例通过（默认跳过）。

## 15. 交付顺序

1. 配置字段与角色常量、`agent_runs` 扩展、两张新表与迁移。
2. 编排器基础：黑板读写、Lead 决策契约、单角色委派、预算统计。
3. Critic 强制审稿、返工上限、循环检测与终止条件。
4. 审批挂起/唤醒（`_wake_parent_team`）、对账扩展与取消。
5. 团队 API 与角色配置 API。
6. worker 分派与恢复语义。
7. 工作流 `team` 节点、校验、引擎接入与内置模板。
8. 前端团队入口、详情、角色配置页与画布支持。
9. 文档、全量验证与真实环境验收。
