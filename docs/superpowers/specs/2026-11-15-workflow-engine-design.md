# 工作流引擎与画布设计（第 2 层）

**Goal:** 把第 1 层的工具、Agent 运行时与审批能力编排成可视化、可版本化、可定时的工作流：管理员在 React Flow 画布上把「输入 → 工具 / Agent / 条件 / 审核 → 输出」连成有向无环图，发布为不可变版本后，由人手动或按星期+时间定时运行；运行时复用第 1 层的工具注册表、策略引擎与审批语义，任何节点都无法绕过「真发布永远人工」的边界。

**Architecture:** 在现有 FastAPI + arq + PostgreSQL 架构内新增 `workflow_*` 六张表与 `app/workflow/` 包。引擎是**时间片续跑的单一执行器**：一个 arq 任务推进整条 DAG，拓扑就绪的节点内联执行，并行分支受并发上限约束；工具节点直接调用第 1 层工具 handler，Agent 节点创建子 `AgentRun` 并内联调用 `run_agent`；到达时间片、遇审批或审核时落库并退出，由审批决策或对账任务重新入队。工作流级审批存 `workflow_approvals`，Agent 节点内部确认沿用 `agent_approvals`。前端新增 `/workflows` 列表、React Flow 编辑器与运行详情三页。

**Tech Stack:** 复用第 1 层技术栈（FastAPI、SQLAlchemy 2.0 async、Pydantic v2、jsonschema、arq、PostgreSQL/SQLite、React 19、TanStack Query、Tailwind CSS v4、vitest）；新增前端依赖 `@xyflow/react`（React Flow 官方包）。后端不新增第三方依赖，定时计算使用标准库 `zoneinfo`。

**Status:** 与用户逐节确认完毕，等待规格评审。

**关联:** 三层总览与第 1 层详见 `docs/superpowers/specs/2026-11-13-ai-native-automation-design.md`；本文档只定义第 2 层。

---

## 1. 背景与范围

### 1.1 本期实现

1. 工作流定义：草稿、不可变发布版本、内置模板。
2. 节点引擎：输入、工具、Agent、条件、审核、输出六种节点；DAG 并行分支与汇合；条件未选中分支的跳过传播。
3. 执行语义：时间片续跑、节点重试、从失败处续跑、取消、心跳对账、运行级预算。
4. 审批：工作流级 `tool_confirm` 与 `review`；Agent 节点内部确认复用第 1 层。
5. 定时：租户级「星期 + 时间」，绑定已发布版本，防重复触发。
6. API：定义、版本、模板、定时、运行、审批；新增只读 `GET /api/agent/tools`。
7. 前端：工作流列表、React Flow 画布编辑器、运行详情（画布状态叠加）、定时管理。
8. 后端单测、前端纯函数单测、真实模型端到端与浏览器验收。

### 1.2 本期不做

- 真发布、排期、导出节点：注册表里没有对应工具，工作流结构上不可达。
- 循环/批量（逐项）节点：下一轮迭代。
- 事件触发（内容/发布/媒体状态变化自动启动工作流）。
- 用户自定义脚本或 HTTP 节点；跨租户共享工作流；公开模板市场。
- cron 表达式：定时只支持每日/每周 + 时分 + 时区。
- 工作流级别的新执行内核或第二套权限体系。

## 2. 数据模型

新文件 `backend/app/models/workflow.py`。全部表继承 `TenantScoped`（复用 `id`、`tenant_id`、`created_at`、`updated_at` 与 `JSONType`）。一次 Alembic 迁移，接在第 1 层最新 revision 之后。

新包 `backend/app/workflow/`：`graph.py`（结构读取、拓扑与变量解析）、`validate.py`（草稿/发布校验）、`schedule.py`（cadence 与 `next_run_at` 计算）、`engine.py`（Executor 与节点执行）、`templates.py`（内置模板常量）、`approvals.py`（两条审批通道的读写）。API 层见 §7。

### 2.1 `workflows`

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | String(120) | 显示名，非唯一 |
| `description` | Text，默认空 | |
| `created_by` | UUID | 创建人；定时运行的发起人取此值 |
| `draft_graph` | JSON，可空 | 可编辑图；`null` 表示尚未建图 |
| `draft_revision` | Integer，默认 0 | 每次保存草稿 +1，用于并发控制与「有无未发布修改」判定 |
| `archived_at` | DateTime(timezone)，可空 | 软删除；有运行历史的定义不物理删除 |

索引 `(tenant_id, archived_at)`。**没有** `published_version_id` 列：最新发布版本 = `workflow_versions` 中最大 `version`；「有未发布修改」= `workflows.draft_revision > 最新版本.source_revision`。这样避免循环外键。

### 2.2 `workflow_versions`

| 字段 | 类型 | 说明 |
|---|---|---|
| `workflow_id` | UUID，FK `workflows` CASCADE，索引 | |
| `version` | Integer | 从 1 递增 |
| `graph` | JSON | 发布时的完整图，发布后不可修改 |
| `source_revision` | Integer | 对应发布时的 `draft_revision` |
| `published_by` | UUID | |
| `published_at` | DateTime(timezone) | |
| `note` | Text，默认空 | 发布说明 |

唯一约束 `(workflow_id, version)`。

### 2.3 `workflow_schedules`

| 字段 | 类型 | 说明 |
|---|---|---|
| `workflow_id` | UUID，FK `workflows` CASCADE，索引 | 一个工作流可有多条定时 |
| `workflow_version_id` | UUID，FK `workflow_versions` CASCADE | 固定绑定某个已发布版本 |
| `cadence` | JSON | `{"kind":"daily"|"weekly","weekdays":[1..7],"time":"09:00","timezone":"Asia/Shanghai"}`；`daily` 忽略 `weekdays` |
| `inputs` | JSON | 默认运行入参 |
| `enabled` | Boolean，默认 true | |
| `next_run_at` | DateTime(timezone)，可空，索引 | 由 `zoneinfo` 计算 |
| `last_run_at` | DateTime(timezone)，可空 | |
| `created_by` | UUID | |

版本更新不会自动改写定时：换版本等于删除旧定时、新建一条。

### 2.4 `workflow_runs`

| 字段 | 类型 | 说明 |
|---|---|---|
| `workflow_id` | UUID，FK `workflows` CASCADE，索引 | 定义只软删除，CASCADE 实际不会触发 |
| `workflow_version_id` | UUID，FK `workflow_versions` | 运行绑定版本 |
| `trigger` | String(16) | `manual` / `schedule` |
| `triggered_by` | UUID，可空 | 手动运行的发起人；定时为 `null` |
| `schedule_id` | UUID，可空，FK SET NULL | 删除定时不丢运行历史 |
| `scheduled_for` | DateTime(timezone)，可空 | 该次定时计划时刻 |
| `status` | String(24)，索引 | `queued`/`running`/`waiting_approval`/`succeeded`/`failed`/`cancelled` |
| `inputs` | JSON | 运行入参（用户提供或定时默认值） |
| `policy` | JSON | 运行启动时的策略快照 |
| `result` | JSON | 输出节点汇总的产物与值 |
| `error_code` / `error_message` | String(100) / Text | |
| `usage` | JSON | `prompt_tokens`/`completion_tokens`/`tool_calls`/`nodes`/`elapsed_ms`/`paused_ms` |
| `attempts` | Integer，默认 0 | 心跳对账重入队次数 |
| `resume_count` | Integer，默认 0 | 「从失败处续跑」次数 |
| `step_counter` | Integer，默认 0 | 分配稳定的 `tool_step_index` |
| `idempotency_key` | String(64)，可空 | 唯一约束 `(tenant_id, idempotency_key)` |
| `heartbeat_at` / `started_at` / `finished_at` | DateTime(timezone) | |

额外约束：唯一 `(schedule_id, scheduled_for)`，防止定时重复触发（删除定时后该约束随 `schedule_id` 置空而失效，历史运行保留）。索引 `(tenant_id, workflow_id, created_at)`。

### 2.5 `workflow_node_runs`

| 字段 | 类型 | 说明 |
|---|---|---|
| `run_id` | UUID，FK `workflow_runs` CASCADE，索引 | |
| `node_id` | String(64) | 图内节点 id |
| `attempt` | Integer，默认 1 | 每次尝试一行 |
| `kind` | String(16) | `input`/`tool`/`agent`/`condition`/`review`/`output` |
| `status` | String(16) | `running`/`ok`/`error`/`skipped`/`denied`/`waiting`/`interrupted` |
| `tool_step_index` | Integer，可空 | 工具节点专用，同一节点跨尝试复用 |
| `agent_run_id` | UUID，可空 | Agent 节点对应的子运行 |
| `input` | JSON | 解析后的入参（脱敏截断，供展示） |
| `output` | JSON | 脱敏截断输出（供 API 与页面） |
| `raw_output` | JSON | 仅变量解析使用，不返回 API、不进日志 |
| `error` | Text | 安全文案 |
| `duration_ms` | Integer，默认 0 | |
| `started_at` / `finished_at` | DateTime(timezone) | |

唯一约束 `(run_id, node_id, attempt)`。

### 2.6 `workflow_approvals`

| 字段 | 类型 | 说明 |
|---|---|---|
| `run_id` | UUID，FK `workflow_runs` CASCADE，索引 | |
| `node_id` | String(64) | |
| `kind` | String(16) | `tool_confirm` / `review` |
| `tool_name` | String(100)，默认空 | 工具节点专用 |
| `risk` | String(16)，默认空 | |
| `payload` | JSON | 脱敏后的待执行参数或上游摘要 |
| `title` | String(200) | 审批/审核标题 |
| `status` | String(16)，默认 `pending` | `pending`/`approved`/`rejected`/`expired` |
| `decided_by` / `decided_at` | UUID / DateTime，可空 | |
| `note` | Text，默认空 | |
| `expires_at` | DateTime(timezone)，可空 | |

索引 `(tenant_id, status)`。

### 2.7 对第 1 层表的扩展

`agent_runs` 增加两列：

| 字段 | 类型 | 说明 |
|---|---|---|
| `workflow_run_id` | UUID，可空，索引 | 属于哪次工作流运行 |
| `workflow_node_id` | String(64)，可空 | 属于哪个 Agent 节点 |

`mode` 增加取值 `node`。这两列是本层唯一改动第 1 层模型的地方，用途：

1. 审批通过时判断该重新入队 `run_agent_job`（独立运行）还是 `run_workflow_job`（工作流子运行）。
2. 运行详情聚合子运行的待处理审批。

## 3. 图结构与校验

### 3.1 图 JSON

```json
{
  "nodes": [
    {"id": "in", "type": "input", "position": {"x": 0, "y": 0},
     "data": {"label": "输入", "fields": [{"key": "topic", "type": "string", "required": true}]}},
    {"id": "n1", "type": "tool",
     "data": {"label": "读画像", "tool": "profile.list", "args": {"limit": 5}, "retry": 1}},
    {"id": "n2", "type": "agent",
     "data": {"label": "写笔记", "goal": "围绕 {{inputs.topic}} 写一版小红书图文",
              "tools": ["content.*", "media.generate"],
              "budget": {"max_steps": 8}, "retry": 0}},
    {"id": "c1", "type": "condition",
     "data": {"label": "是否通过", "left": "{{nodes.n2.output.result_text}}",
              "operator": "contains", "value": "通过"}},
    {"id": "r1", "type": "review",
     "data": {"label": "确认草稿", "title": "请确认本次生成的草稿"}},
    {"id": "out", "type": "output", "data": {"label": "输出"}}
  ],
  "edges": [
    {"id": "e1", "source": "in", "target": "n1"},
    {"id": "e2", "source": "c1", "sourceHandle": "true", "target": "r1"},
    {"id": "e3", "source": "c1", "sourceHandle": "false", "target": "out"}
  ]
}
```

- 节点 `id` 由前端生成，唯一，1–64 字符；`type` 决定 `data` 形状。
- 示例仅示意字段形状，省略了部分边，不构成完整可发布图（草稿允许不完整，见 §3.4）。
- `position` 是画布坐标，属于图数据的一部分，随草稿与版本一起保存。
- 边可以带 `sourceHandle`；只有条件节点的边允许且必须带 `"true"`/`"false"`。

### 3.2 节点 data 规格

| type | data 字段 | 约束 |
|---|---|---|
| `input` | `fields[]`：`{key, label?, type: "string"\|"number"\|"boolean", required?, maxLength?}` | `key` 唯一、`[a-z][a-z0-9_]{0,63}`；至少一个字段 |
| `tool` | `tool`、`args`（对象）、`retry` 0–3 | 工具必须存在于注册表；`args` 按工具 schema 校验 |
| `agent` | `goal`(1..4000)、`tools`（可空，支持 `前缀.*`）、`budget`、`retry` 0–3 | 默认工具集 = 注册表全部工具；白名单只能收窄；预算按第 1 层口径夹紧 |
| `condition` | `left`、`operator`、`value` | 运算符 `equals`/`not_equals`/`contains`/`not_empty`/`gt`/`lt`；`not_empty` 不需要 `value`；必须有两条出边 |
| `review` | `title`(1..200)、`note?`（给审批人的说明） | 至少一条入边与一条出边 |
| `output` | `label?` | 可以有多个，每个都参与结果汇总 |

### 3.3 变量解析

- 语法只有 `{{inputs.<key>}}` 与 `{{nodes.<id>.output.<路径>}}`，路径用点号与数字下标（如 `nodes.n2.output.result.items.0.id`）；空路径表示整个输出。
- **没有**表达式、函数、算术、默认值或条件。
- 字符串字段可以混写文本与占位符；非字符串字段（如 `args.limit`）要求整个值就是一个占位符，解析结果保持原始类型。
- 解析失败（缺 key、路径不存在、类型不匹配）让该节点 `error`，错误文案包含节点 id 与缺失路径，不包含值本身。
- 变量解析只在节点执行时进行；保存与发布只做语法静态检查。

### 3.4 校验规则

保存草稿只做结构校验（`nodes`/`edges` 是数组、node id 唯一、边指向存在的节点、规模上限）；允许暂时不完整（例如还没有输出节点、存在环）。**发布必须全部通过**：

1. 恰好一个 `input` 节点、至少一个 `output` 节点。
2. 无环（拓扑检测）。
3. 所有节点从 `input` 可达；所有节点能到达某个 `output`。
4. 条件节点恰好两条出边，句柄各为 `true`/`false`；非条件节点出边不得带句柄。
5. `review` 节点至少一条入边与一条出边；`input` 无入边；`output` 无出边。
6. 工具名存在；`skill.run` 的 `args.skill_id` 指向已注册技能；Agent 工具白名单中的名字或前缀至少匹配一个工具。
7. `retry`、预算、`inputs.fields` 合法。
8. 规模：节点 ≤50、边 ≤100、序列化后 ≤256 KB。

校验器返回 `{ok, issues: [{code, node_id?, message}]}`；发布接口把 `issues` 原样返回。若某工具当前被租户策略 `deny`，只产生**警告**（`code=policy_denies_tool`），不阻断发布：策略可变，运行期才权威。

## 4. 执行语义

### 4.1 状态机

```
queued → running → succeeded
                 → failed
                 → cancelled
                 → waiting_approval → running（审批/审核通过后重新入队）
                                    → failed（驳回或过期）
```

`waiting_approval` 期间不占用 worker；墙钟等待时间记入 `usage.paused_ms`，不计入 `workflow_max_seconds`。

### 4.2 就绪与跳过传播

- 边两态：**激活** = 上游 `ok` 且（普通边 或 条件节点选中的句柄）；**未激活** = 上游 `skipped`、或上游 `ok` 但条件走了另一条分支。
- 节点入边全部落定后：至少一条激活 → 执行一次；全部未激活 → 写一条 `skipped`（原因「上游分支未选中」）并继续传播。
- 汇合节点因此会等待所有并行入边；多条激活入边的 `raw_output` 同时进入变量作用域。
- `input` 节点在启动时自动 `ok`（输出 = 运行入参）；`output` 节点把各激活上游的 `data`/`output` 与 `artifacts` 收进 `{items, artifacts}` 作为运行结果。
- 所有节点落定（`ok`/`skipped`）即 `succeeded`。

### 4.3 节点执行

| 类型 | 行为 |
|---|---|
| `input` | 建运行时按 `fields` 校验入参：类型、必填、`maxLength`；不通过 → 422，不建运行 |
| `tool` | 解析 `args` → 按工具 schema 校验 → 策略判定（见 §5）。`deny` → 节点 `denied`、运行 `failed/policy_denied`；`confirm` → 建 `tool_confirm` 审批、节点 `waiting`、运行 `waiting_approval`、退出 worker；`auto` → 用第 1 层 `ToolContext` 执行，写工具审计，节点 `ok` |
| `agent` | 解析目标 → 建子 `AgentRun`（`mode="node"`、`workflow_run_id`、`workflow_node_id`、`user_id`=运行发起人，定时运行取 `workflows.created_by`；预算夹紧）→ 用**过滤后的工具注册表视图**内联调用 `run_agent`。子运行 `succeeded` → 节点 `ok`（输出含子运行 `result_text`/`result`）；`failed` → 节点 `error`（`approval_rejected` 时为 `denied`，运行同码失败）；`waiting_approval` → 节点 `waiting`、运行 `waiting_approval` |
| `condition` | 解析 `left`/`value` 后按运算符求值，输出 `{result: bool}`；只有选中句柄的边激活 |
| `review` | 建 `review` 审批（`title` + 上游输出摘要作 `payload`）、节点 `waiting`、运行 `waiting_approval`；通过 → 节点 `ok`，驳回 → 节点 `denied`、运行 `failed/review_rejected` |
| `output` | 汇总激活上游，节点 `ok` |

**原始输出只用于变量解析。** 节点运行存 `output`（脱敏截断，给 API 与页面）与 `raw_output`（变量解析用，不返回、不进日志）。`raw_output` 受工具自身输出上限约束（`ToolResult.data` ≤20 KB），并沿用第 1 层的密钥字段丢弃规则做兜底。不这样做，>2000 字的正文经过一次节点就会在下游被截断。

**审批通过后的执行参数由确定性重解析得到。** 工具节点的 `confirm` 审批只保存脱敏预览；通过后引擎用同一组上游 `raw_output` 重新解析 `args` 再执行。变量解析是纯函数，运行级策略快照也已固定，因此重解析结果与暂停前一致，不需要像第 1 层那样额外持久化一份原始参数。

**工具幂等。** 第一次执行工具节点时用原子 `UPDATE workflow_runs SET step_counter = step_counter + 1` 分配 `tool_step_index` 并写进节点运行；重试、续跑、审批恢复都复用该值。工具节点的 `ToolContext.run_id` 取 `str(workflow_run_id)`、`step_index` 取 `tool_step_index`，第 1 层写工具既有的 `(run_id, step_index)` 幂等键因此直接生效，不需要给工具新增参数。

### 4.4 并行与时间片

- 一次 `run_workflow_job` 最多推进 `workflow_time_slice_seconds`（480 秒），给 `job_timeout=600` 留余量。
- 就绪节点分批执行，并发上限 `workflow_max_parallel_nodes`（3）；每个节点用独立 session，节点完成后提交节点运行与运行汇总。
- 时间片余量不足以覆盖下一个节点的最大可能耗时（工具节点取 `timeout_seconds`，Agent 节点取子运行 `budget.max_seconds`）时，先把运行落回 `queued` 并自我重新入队，不在本次任务里硬跑。Agent 节点的 `max_seconds` 由引擎夹紧到 `workflow_time_slice_seconds`，因此任何单节点都不会跨越时间片。
- SQLite（测试夹具）下自动串行，生产 PostgreSQL 才真正并行。
- 运行级墙钟上限 `workflow_max_seconds`（3600），审批等待不计入；超限 → `failed/workflow_timeout`。

### 4.5 重试与从失败处续跑

- 节点 `retry` 0–3，只对异常、超时和可重试的工具错误生效，指数退避 2/4/8 秒（在时间片内）。
- 策略拒绝、参数非法、审批/审核驳回**不重试**。
- `POST /api/workflow-runs/{id}/resume` 仅接受 `failed`：`resume_count` +1（上限 10，超过返回 409），运行置回 `queued` 重新入队。已有 `ok` 尝试的节点直接复用；`error`/`denied`/`interrupted` 的节点以 `attempt+1` 重跑；审核节点重新生成待审批。
- 崩溃恢复：心跳对账把超时运行重新入队；重新执行时把停在 `running` 的节点运行标为 `interrupted`，按续跑规则处理。

### 4.6 取消

- `queued`/`running`/`waiting_approval` 可取消；CAS 置 `cancelled`。
- 待处理的 `workflow_approvals` 置 `expired`；子 `AgentRun` 置 `cancelled`，其 pending 的 `agent_approvals` 置 `expired`；在途节点运行标 `interrupted`。

### 4.7 心跳与对账

- 运行期间每 15 秒更新 `heartbeat_at`（独立 session）。
- `reconcile_workflow_runs` 每分钟：`running` 且 `heartbeat_at < now - workflow_run_stale_seconds` → `attempts < 2` 则重新入队，否则 `failed/workflow_stale`。
- 同一任务扫描 `waiting_approval` 的运行：若工作流审批与全部子运行审批都已无 `pending`（例如子审批被第 1 层过期任务处理掉），置 `failed/approval_expired`，避免永久悬挂。
- 同时把过期的 `workflow_approvals` 置 `expired` 并把对应运行落 `failed/approval_expired`。

### 4.8 审计与用量

审计事件：`workflow.create`、`workflow.update`、`workflow.publish`、`workflow.schedule.create/update/delete`、`workflow.schedule.skipped`、`workflow.run.create/cancel/resume/complete/fail`、`workflow.node.execute`（写工具）、`workflow.review.approve/reject`。审计只记 id、摘要与结果码，不记入参与正文。

`usage` = 子 AgentRun 的 token 汇总 + 工具调用次数 + 已执行节点数 + 墙钟耗时（含 `paused_ms`）；`skill.run` 的 token 仍记在既有技能审计里，不重复计入。

## 5. 策略与审批

### 5.1 策略

- 策略解析仍只有第 1 层的 `resolve_policy`，工作流不另立规则。
- 运行启动时解析并快照进 `workflow_runs.policy`，续跑沿用同一快照；Agent 节点内部的子运行按第 1 层规则各自快照。
- 工具节点命中 `deny` → `failed/policy_denied`（没有模型可以协商）；命中 `confirm` → `tool_confirm` 审批。

### 5.2 审批通道

| 通道 | 表 | 覆盖 | 决策入口 |
|---|---|---|---|
| 工作流级 | `workflow_approvals` | 工具节点 `tool_confirm`、`review` 审核节点 | `POST /api/workflow-approvals/{id}/decide` |
| Agent 节点内 | `agent_approvals`（第 1 层） | 子运行内部工具的 `confirm` | `POST /api/agent/approvals/{id}/decide` |

规则：

- 审批人必须是 `admin`/`owner`；**有发起人的运行**（`trigger=manual`）排除发起人本人。
- **定时运行（`trigger=schedule`，无发起人）** 的审批对租户内所有 admin/owner 开放，包括工作流创建人：触发来自时间而非某个人，创建人本来就有直接调用接口的权限，不构成越权；审计记录实际审批人。这条用于避免「只有一个管理员」的租户定时流程必然卡到过期。
- 为了让上面的规则对子运行一致，第 1 层「不能审批自己发起的运行」调整为：**只有当子运行属于手动工作流运行（或独立运行）时才排除发起人**；定时工作流运行没有发起人，不排除。
- 决策一次性：CAS；已处理或已过期返回 409。过期 TTL `workflow_approval_ttl_hours`（24）。
- 并行分支可能同时挂起多条审批：通过一条时若运行仍有其他 `pending` 工作流审批或子运行审批，保持 `waiting_approval`；最后一条通过时才 CAS 回 `queued` 并重新入队。驳回任一条即终止运行。
- 审批预览只展示脱敏副本；工具节点通过后用原始上游输出重解析参数执行（§4.3），Agent 子运行沿用第 1 层 `raw_arguments` 机制。
- 决策通过时的入队与回滚沿用第 1 层模式：审批状态与运行状态在同一事务内 CAS，入队失败回滚并返回 503。

### 5.3 第 1 层需要配合的两处小改动

1. `app/api/agent.py` 的审批决策：子运行带 `workflow_run_id` 时，入队 `run_workflow_job`，并在该工作流运行已无其他待处理审批时把父运行从 `waiting_approval` CAS 回 `queued`；否则只落审批结果、父运行继续等待。
2. 自批校验按 §5.2 的规则判断「运行是否存在发起人」。

## 6. 发布边界与安全

- **真发布不可达**：注册表没有真发布/排期/导出工具；`publish.create_draft_job` 仍强制 `status=draft`、`scheduled_at=None`。审核节点是流程内确认门，不等于平台审核或发布。将来若加入真发布工具，必须 `risk=high` + `never_auto`，工作流节点只能 `confirm` 且租户不能放宽。
- **角色**：admin/owner 创建、编辑、发布、管定时、审批；editor 只能运行已发布版本、取消/续跑自己的运行；viewer 无页面入口（读接口仍按其角色开放）。editor 不能改定义，防止用工作流绕过自身的写权限。
- **租户隔离**：全部查询按 `tenant_id` 过滤，跨租户一律 404；子运行继承租户，工具 handler 继续按第 1 层校验资源归属；图里只能引用工具/技能名，不能携带资源 id 做跨对象越权。
- **无新执行面**：变量解析是受限点路径替换，没有表达式、函数或脚本；没有 HTTP/代码节点；`skill.run` 的既有沙箱约束不变。
- **凭据与数据**：节点展示用脱敏输出；`raw_output` 只参与解析、不返回、不进日志；审批 payload 脱敏；审计不记入参与正文；工具本身不返回凭据。
- **规模与成本**：节点/边/图大小上限；每用户活跃运行 2；并行 3；运行墙钟 1 小时；租户日 token 配额在手动运行创建时与每个 Agent 节点启动前各查一次（`0` 关闭），超额节点失败、运行 `failed/agent_daily_quota_exceeded`。
- **并发正确性**：审批决策 CAS、定时 `(schedule_id, scheduled_for)` 唯一、写工具 `tool_step_index` 幂等、运行领取 CAS、心跳对账。

## 7. API

新文件 `backend/app/api/workflows.py`（定义/版本/模板/定时）与 `backend/app/api/workflow_runs.py`（运行/审批），在 `main.py` 现有路由之后注册。所有写接口写审计；跨租户统一 404。

### 7.1 定义与版本（`/api/workflows`）

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| POST | `/` | admin/owner | 创建草稿 `{name, description?, graph?}` → 201 |
| GET | `/` | viewer | 列表（不含图）：名称、有无未发布修改、最新版本、下次定时、最近运行、创建人 |
| GET | `/{id}` | viewer | 详情：`draft_graph`、`draft_revision`、最新发布版本、定时列表 |
| PATCH | `/{id}` | admin/owner | `{revision, name?, description?, draft_graph?}`；`revision` 不匹配 → `409 workflow_conflict` |
| DELETE | `/{id}` | admin/owner | 软删除；存在活跃运行 → `409 workflow_run_active` |
| POST | `/{id}/validate` | admin/owner | `{ok, issues[]}` |
| POST | `/{id}/publish` | admin/owner | `{revision, note?}`；校验失败 → `422 workflow_invalid`（含 `issues[]`） |
| GET | `/{id}/versions` | viewer | 版本列表 |
| GET | `/{id}/versions/{version}` | viewer | 版本完整图 |
| POST | `/{id}/versions/{version}/restore` | admin/owner | 复制回草稿，不改动已发布版本 |

### 7.2 模板

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| GET | `/templates` | admin/owner | 内置模板列表（key、名称、说明、参数说明） |
| POST | `/templates/{key}/instantiate` | admin/owner | `{name}` → 用模板图创建草稿 |

模板是 `backend/app/workflow/templates.py` 内的常量；首批 3 个：「每周选题 → 创作 → 审核 → 建草稿」「批量平台改写」「发布后复盘」。

### 7.3 定时（`/{id}/schedules`）

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| GET | `/{id}/schedules` | viewer | |
| POST | `/{id}/schedules` | admin/owner | `{cadence, inputs?, enabled?, version?}`；`version` 缺省为最新发布版本；无已发布版本 → `409 workflow_not_published` |
| PATCH | `/{id}/schedules/{sid}` | admin/owner | 仅 `cadence`/`inputs`/`enabled` |
| DELETE | `/{id}/schedules/{sid}` | admin/owner | 运行历史保留 |

### 7.4 运行

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| POST | `/api/workflows/{id}/runs` | editor | `{inputs, version?}`；支持 `Idempotency-Key`；入参校验失败 422；同用户活跃运行 ≥`workflow_max_active_runs_per_user`（默认 2）→ 409；配额用尽 → 429 |
| GET | `/api/workflows/{id}/runs` | viewer | 分页、状态过滤 |
| GET | `/api/workflow-runs/{run_id}` | viewer | 运行 + 每个节点的各次尝试 + 待处理审批（`source` 为 `workflow`/`agent`）+ 产物 |
| POST | `/api/workflow-runs/{run_id}/cancel` | 发起人或（定时运行）admin/owner | CAS |
| POST | `/api/workflow-runs/{run_id}/resume` | 发起人或（定时运行）admin/owner | 仅 `failed`，续跑上限 10 |

### 7.5 审批

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| GET | `/api/workflow-approvals` | admin/owner | 待处理工作流级审批；手动运行排除自己发起的，定时运行对所有管理员可见 |
| POST | `/api/workflow-approvals/{id}/decide` | admin/owner | `{decision: approve\|reject, note?}`；通过 → 重新入队工作流；驳回 → 节点 `denied`、运行失败 |

### 7.6 只读工具清单

`GET /api/agent/tools`（editor+）：返回注册表内每个工具的 `{name, description, parameters, risk}`，供节点面板与参数表单使用，避免前端硬编码工具清单。

### 7.7 响应与错误码

主要形态：`WorkflowOut`、`WorkflowDetailOut`、`VersionOut`、`ScheduleOut`、`RunOut`、`RunDetailOut{run, node_runs[], approvals[], result}`、`NodeRunOut{node_id, attempt, kind, status, input, output, error, duration_ms, agent_run_id}`、`ApprovalOut{id, source, kind, node_id, tool_name, title, payload, status, expires_at}`。`raw_output` 永不出现在响应里。

| 码 | HTTP | 场景 |
|---|---|---|
| `workflow_not_found` | 404 | 不存在、跨租户或已归档 |
| `workflow_conflict` | 409 | 草稿 `revision` 不匹配 |
| `workflow_invalid` | 422 | 发布校验失败，带 `issues[]` |
| `workflow_not_published` | 409 | 对未发布工作流建运行或建定时 |
| `workflow_run_not_found` | 404 | |
| `workflow_run_not_cancellable` | 409 | 已终态 |
| `workflow_run_not_resumable` | 409 | 非 `failed` 或续跑次数超限 |
| `workflow_run_limit` | 409 | 同用户活跃运行 ≥2 |
| `workflow_run_active` | 409 | 删除定义时仍有活跃运行 |
| `workflow_schedule_invalid` | 422 | cadence/时区/入参非法 |
| `workflow_approval_not_pending` | 409 | 审批已处理或过期 |
| `agent_daily_quota_exceeded` | 429 | 复用第 1 层 |
| `queue_unavailable` | 503 | 复用第 1 层 |

`workflow_runs.error_code` 另有运行级取值（不对应 HTTP）：`policy_denied`、`approval_rejected`、`review_rejected`、`approval_expired`、`workflow_stale`、`workflow_timeout`、`node_failed`、`agent_daily_quota_exceeded`、`llm_not_configured`。

## 8. Worker

`backend/app/worker.py`：

- 新函数 `run_workflow_job(workflow_run_id)`：只认 `queued`，CAS 领跑，推进到时间片用尽、遇审批或收敛。
- 新 cron `fire_due_workflow_schedules`（每分钟第 15 秒）：对 `enabled` 且 `next_run_at <= now` 的定时 CAS 推进 `next_run_at` 并创建运行（`scheduled_for` = 触发时刻），入队；该工作流已有活跃运行（`queued`/`running`/`waiting_approval`）或已归档时跳过并写 `workflow.schedule.skipped` 审计。
- 新 cron `reconcile_workflow_runs`（每分钟第 45 秒）：心跳超时重入队/落 `workflow_stale`；`waiting_approval` 且已无待处理审批 → `approval_expired`；过期 `workflow_approvals`。
- 现有 `run_agent_job`、`reconcile_agent_runs` 不变；`app/api/agent.py` 的审批决策按 §5.3 增加工作流入队分支。
- `job_timeout=600` 不变；时间片 480 秒留余量。

## 9. 前端

新增依赖 `@xyflow/react`，编辑器路由 `React.lazy` 懒加载。导航新增「工作流」，minRole `editor`；路由：

- `/workflows`：列表（名称、未发布修改标记、版本、下次定时、最近运行、创建人），含模板库入口。
- `/workflows/:id`：画布编辑器（admin/owner 可编辑；editor 只读画布 + 可运行已发布版本）。
- `/workflows/:id/runs/:runId`：运行详情（只读画布 + 右栏）。

**编辑器**（复用现有主题令牌，不新增配色）：

- 左栏节点面板：六种节点，拖拽或点击加入画布。
- 画布：自定义节点组件、小地图、缩放、网格吸附；条件节点带「是」「否」两个出边句柄并显示标签。
- 右栏属性面板：工具节点用 `GET /api/agent/tools` 的 schema 生成参数表单并支持变量插入与上游变量提示；`skill.run` 的技能下拉复用 `/api/skills`；Agent 节点配置目标、工具白名单与预算；条件节点配置左值/运算符/右值；审核节点配置标题与说明；每个节点有 `retry` 与显示名称。
- 顶栏：保存草稿（带 `revision`，冲突提示并提供重新加载）、校验、发布（发布说明）、运行（未发布禁用）、版本历史抽屉、返回。
- 本地撤销/重做（最多 50 步，Ctrl/Cmd+Z）；离开有未保存修改时拦截确认。
- 校验结果底部抽屉列出，点击定位节点；「保存」只做轻量提示，发布由服务端权威校验。
- 定时管理：右栏「定时」页签列出 cadence、下次运行、启停开关；新建/编辑对话框只提供每日/每周 + 时分 + 时区。

**运行详情**：同一套节点/边渲染叠加状态（运行中脉动、成功、失败、跳过灰、等待审核琥珀色），已执行路径的边加粗；右栏为运行摘要（触发方式、耗时、tokens、节点数）、节点尝试列表（展开每次 `attempt` 的输入输出）、待处理审批卡片（通过/驳回，驳回必填备注，`source` 决定调用哪个接口）、取消与「从失败处续跑」。活跃状态每 2 秒轮询，全部终态即停（沿用 AI 助理页写法）。

**类型与纯函数**：API 类型放 `web/src/types/workflow.ts`；`web/src/workflows.ts` 放可测纯函数（单测覆盖）：上游节点集合、拓扑序、条件分支可达性、变量候选、节点状态与徽标映射、终态判定、权限助手（`canEditWorkflow`、`canRunWorkflow`、`canDecideWorkflowApproval`、`canCancelWorkflowRun`）、定时文案、规模上限提示。图校验不在前端重复实现。

## 10. 配置

`backend/app/config.py` 新增（均有默认值）：

| 配置 | 默认 | 说明 |
|---|---|---|
| `workflow_max_parallel_nodes` | 3 | 单次运行的并行节点上限 |
| `workflow_time_slice_seconds` | 480 | 单次 worker 任务的时间片 |
| `workflow_max_seconds` | 3600 | 运行墙钟上限（不含审批等待） |
| `workflow_approval_ttl_hours` | 24 | 工作流审批有效期 |
| `workflow_max_active_runs_per_user` | 2 | 手动运行并发上限 |
| `workflow_run_stale_seconds` | 300 | 心跳超时判定 |

## 11. 测试

| 文件 | 覆盖点 |
|---|---|
| `tests/test_workflow_graph.py` | 图校验（单输入/输出可达/无环/规模/句柄/工具存在性/预算夹紧）；变量解析（点路径、缺值、非字符串整值、混写）；定时计算（每日/每周、跨月、时区） |
| `tests/test_workflow_engine.py` | 线性成功；条件真/假与跳过传播；并行汇合只执行一次；节点重试成功/耗尽；时间片续跑不重跑已 `ok` 节点；崩溃 `interrupted` 恢复；从失败处续跑；工具 `deny`/`confirm` 通过与驳回；审核通过与驳回；Agent 节点四种结果映射；产物汇总；运行中取消；写工具幂等；**>2000 字正文经变量传递逐字落库** |
| `tests/test_workflow_api.py` | 角色矩阵；双租户 404；`revision` 冲突；发布校验 422；未发布运行 409；活跃上限；配额 429 与 `0` 关闭；幂等键；取消/续跑权限；审批非自批与 CAS；定时 CRUD 与版本必填；模板实例化；`GET /api/agent/tools` |
| `tests/test_workflow_worker.py` | 定时推进与唯一约束去重；活跃运行跳过并审计；心跳超时重入队与 `workflow_stale`；`waiting_approval` 无待办 → `approval_expired`；工作流审批过期 |
| `tests/test_workflow_e2e.py` | `RUN_WORKFLOW_E2E=1` 真实模型：模板「输入主题 → Agent 产出内容与草稿 → 输出」，断言产物含内容版本与 `draft` 任务 |

SQLite 夹具下引擎串行；测试把时间片调到 1–2 秒验证续跑。前端 `web/src/__tests__/workflow.test.ts` 覆盖上述纯函数。浏览器验收用 Playwright MCP 走「创建 → 编辑 → 保存 → 发布 → 运行 → 审核通过 → 查看产物」，并验证未发布不能运行、editor 只读、并发保存冲突、运行画布状态随轮询更新。

## 12. 验收标准

1. 从模板到「修改 → 发布 → 运行」全程在画布完成；手动运行在默认预算内产出内容版本与 `draft` 发布任务。
2. 并行分支重叠执行，汇合节点只执行一次且拿到全部分支输出。
3. 审批与审核：非发起人 admin 批准后继续；驳回 → `failed/approval_rejected`（审核节点 `review_rejected`）；过期 → `failed/approval_expired`。
4. 从失败处续跑：已成功节点不重跑，写工具不产生重复产物。
5. 定时到点只触发一次；工作流已有活跃运行时跳过；编辑草稿不影响已发布版本与历史运行。
6. 角色矩阵与双租户隔离自动化覆盖；真发布在工作流里结构上不可达。
7. `uv run pytest -q`、`uv run ruff check .`、`npm test`、`npm run build` 全绿；`RUN_WORKFLOW_E2E=1` 真实模型用例通过。
8. 文档同步：README 增「工作流」一节、`.env.example` 增 6 个 `WORKFLOW_*` 配置、CHANGELOG 记录。

## 13. 实施顺序

1. 配置、模型与迁移（含 `agent_runs` 两列）。
2. 图校验与变量解析。
3. 引擎核心：输入/工具/条件/输出节点、就绪与跳过、时间片、重试、取消。
4. Agent 节点、审批对接与第 1 层两处小改动。
5. 定义/版本/模板/定时 API。
6. 运行与审批 API、worker 任务与 cron。
7. 前端纯函数与类型。
8. React Flow 编辑器与列表页。
9. 运行详情、审批与定时 UI。
10. 文档、真实模型 E2E 与全量验收。
