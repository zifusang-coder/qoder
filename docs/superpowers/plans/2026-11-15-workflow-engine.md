# 工作流引擎与 React Flow 画布实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 `docs/superpowers/specs/2026-11-15-workflow-engine-design.md` 交付第 2 层：可版本化、可定时、可人工审批的 DAG 工作流，以及 React Flow 编辑器与运行详情页。

**Architecture:** 新增 `workflow_*` 六张表与 `backend/app/workflow/` 包。一个 `run_workflow_job` 任务用时间片推进整条 DAG：拓扑就绪节点内联执行，并行分支受 `workflow_max_parallel_nodes` 约束；工具节点直接调用第 1 层 `ToolSpec.handler`，Agent 节点创建子 `AgentRun(mode="node")` 后内联调用 `run_agent`。遇审批、审核、时间片结束或崩溃恢复时落库退出，由审批决策或对账任务重新入队。前端新增 `/workflows` 列表、`/workflows/:id` 编辑器、`/workflows/:id/runs/:runId` 运行详情。

**Tech Stack:** FastAPI、SQLAlchemy 2.0 async、Pydantic v2、jsonschema、arq、Alembic、PostgreSQL/SQLite；React 19、react-router-dom 7、TanStack Query 5、Tailwind CSS v4、vitest 4；新增 `@xyflow/react`。

**Spec:** `docs/superpowers/specs/2026-11-15-workflow-engine-design.md`（绑定权威；本计划与规格冲突时以规格为准，除非本计划记录了一条明确的 Ruling）。

---

## 0. 执行约定与本计划的偏离裁决

### 0.1 Rulings（计划对规格的实现级澄清）

1. **前端类型文件位置**：规格 §9 的 `web/src/types/workflow.ts` 照此落地，只放跨页面共用的 API 类型；纯函数与常量放 `web/src/workflows.ts`（与既有 `web/src/agent.ts` 同风格），单测放 `web/src/__tests__/workflow.test.ts`。仓库既有的 `web/src/types.ts` 保持不动，新目录 `web/src/types/` 只包含 `workflow.ts`。
2. **节点 id 字符集**：规格只要求 1–64 字符。为了让变量路径 `nodes.<id>.output...` 无歧义，节点 id 收紧为 `^[A-Za-z][A-Za-z0-9_-]{0,63}$`；前端生成时用 `n<时间戳><随机>`，迁移与校验同源。
3. **工具节点 `raw_output` 形状**：统一为 `{"summary": str, "data": ..., "artifacts": [...]}`，因此变量路径是 `nodes.n1.output.data.items.0.id`。条件/审核/输出节点也遵循同一包裹形状（`{"result": bool}`、`{"approved": bool}`、`{"items": [...], "artifacts": [...]}`）。
4. **幂等键冲突错误码**：规格错误码表没有为工作流幂等键单列代码，复用 `workflow_conflict`（409）。
5. **新增运行级错误码** `workflow_internal_error`：worker 兜底未预期异常时使用；规格 §7.7 的列表是「主要取值」，此码为补充，且不对应 HTTP。
6. **启动预检不要求 LLM**：`run_workflow_job` 尝试加载默认网关，失败时传 `gateway=None`；只有 Agent 节点或 `skill.run` 工具真正需要模型时才落 `llm_not_configured`。避免纯读工具的工作流被无关配置阻断。
7. **暂停时长记账**：`workflow_approvals` 决策通过时按第 1 层 `decide_approval` 的写法把等待时长累加进 `usage.paused_ms` 并刷新 `heartbeat_at`；引擎重新领取时再累加从 `heartbeat_at` 到领取时刻的极小差额。墙钟上限只比较 `elapsed - paused_ms`。
8. **节点尝试号**：`attempt` 在每次「执行/重试/续跑」时递增，唯一约束 `(run_id, node_id, attempt)`。节点级 `retry` 是单次执行内的重试预算；从失败处续跑会开启新一轮，`retry` 预算重新计算。
9. **并行执行**：PostgreSQL 下每个节点使用独立会话并行；SQLite（测试夹具）下强制串行。并行路径不在 SQLite 验收，Playwright/真实环境验收覆盖「汇合节点只执行一次」。
10. **时间片关闭开关**：`workflow_time_slice_seconds <= 0` 表示不切片，仅供单元测试直接驱动引擎；生产默认 480。
11. **Agent 节点预算夹紧留 5 秒余量**：Agent 子运行 `max_seconds` 夹紧到 `workflow_time_slice_seconds - 5`（下限 1）。若恰好夹到整片，时间片门禁里「剩余时间 < 节点预估」在全新时间片上也永远成立（剩余总略小于整片），会无限重入队。5 秒是给任务启动开销的固定余量，仍满足规格「任何单节点都不跨越时间片」。
12. **Agent 子运行失败的重试路径**：子运行非审批类失败时，`_execute_agent` 抛 `service_unavailable`（属于 `RETRYABLE_CODES`），复用 `_run_node` 的节点重试循环；重试会新建子 `AgentRun`。审批驳回、配额超额、未配置模型不重试，直接落运行失败码。
13. **活跃运行上限只计 `queued`/`running`**：与第 1 层 `LIMITED_STATUSES` 一致。`waiting_approval` 可能停留到审批 TTL（默认 24 小时），若计入上限，发起人会被自己等待审批的运行锁死；定时运行的 `triggered_by` 为空，本来也不计入任何人的上限。
14. **`claim_run` 的成功值是 `"claimed"`**：CAS 未命中时会返回观察到的状态（可能是 `running`），与自己的领取无法区分；worker 若按状态判断，重复投递会双执行同一运行。成功返回专用值，调用方只认它。
15. **子运行审批的父运行恢复**：通过时在父运行已无任何 pending 审批（工作流 + 子运行两通道）的情况下 CAS `waiting_approval → queued` 再入队 `run_workflow_job`，仍有 pending 时只落审批结果；入队失败回滚父运行与子审批。驳回时父运行不再入队，由决策 API 调用 `lifecycle.fail_waiting_run` 在同一事务内直接终结父运行（`failed/approval_rejected`），避免等一分钟让对账把它误判成 `approval_expired`。
16. **worker 兜底错误码**：工作流运行的 `error_code` 除规格列表外使用 `tenant_suspended`（租户停用时，与第 1 层一致）与 `queue_unavailable`（入队失败，带 HTTP 503 语义）；二者都不新增 HTTP 端点。
17. **测试文件拆分**：规格 §11 把定时计算并入 `test_workflow_graph.py`、把 Agent 节点映射并入 `test_workflow_engine.py`。本计划按被测模块拆成 `test_workflow_schedule.py`（cadence/`next_run_at`）与 `test_workflow_agent_node.py`（Agent 节点四种结果映射），测试内容与规格逐条一致；规格要求的其余用例仍在原文件。

### 0.2 全局约束（每个任务都必须遵守）

- 角色：写定义/发布/定时/审批 = admin/owner（`admin_access`）；创建运行/取消/续跑自己的运行 = editor（`editor_access`）；读 = viewer。
- 跨租户一律 404；所有查询带 `tenant_id`。
- `raw_output` 只进 `workflow_node_runs.raw_output`，**绝不**出现在任何 API 响应、审计 detail 或日志里。
- 审批 payload、节点 `input`/`output` 必须经过 `scrub` + `fit_bytes`。
- 真发布不可达：注册表没有真发布/排期/导出工具；工作流只允许调用注册表里已有的工具。
- 事件触发、循环节点、HTTP/代码节点、cron 表达式本期不做。
- 后端命令必须走 `cd backend && uv run ...`；前端必须走 `cd web && npm ...`。
- 每个任务结束提交一次；提交信息用 `feat(workflow): ...` / `test(workflow): ...` / `docs(workflow): ...`。

### 0.3 文件结构总览

**后端新增**

```
backend/app/models/workflow.py                六张表 + 两个 AgentRun 列
backend/migrations/versions/e5f6a7b8c9d0_add_workflow_engine.py
backend/app/workflow/__init__.py
backend/app/workflow/graph.py                 图解析、拓扑、就绪/跳过、变量解析
backend/app/workflow/validate.py              草稿结构校验 + 发布校验
backend/app/workflow/schedule.py              cadence 解析与 next_run_at
backend/app/workflow/approvals.py             两条审批通道的读写与决策
backend/app/workflow/templates.py             三个内置模板常量
backend/app/workflow/engine.py                执行器（节点执行、重试、时间片、续跑、取消）
backend/app/workflow/lifecycle.py             领取、取消、续跑、驳回终结、心跳对账
backend/app/agent/quota.py                    日 token 用量（API 与引擎共用）
backend/app/api/workflows.py                  定义/版本/模板/定时
backend/app/api/workflow_runs.py              运行/审批
backend/tests/test_workflow_models.py
backend/tests/test_workflow_graph.py
backend/tests/test_workflow_validate.py
backend/tests/test_workflow_schedule.py
backend/tests/test_workflow_engine.py
backend/tests/test_workflow_agent_node.py
backend/tests/test_workflow_api.py
backend/tests/test_workflow_worker.py
backend/tests/test_workflow_e2e.py
```

**后端修改**

```
backend/app/config.py                         6 个 workflow_* 配置
backend/app/models/agent.py                   workflow_run_id / workflow_node_id
backend/app/agent/registry.py                 ToolRegistry.subset
backend/app/agent/runtime.py                  定时工作流子运行的自批规则
backend/app/api/agent.py                      审批决策路由到父工作流；GET /agent/tools；审批列表可见性
backend/app/main.py                           注册 workflows / workflow_runs 路由
backend/app/worker.py                         run_workflow_job、定时触发、对账
```

**前端新增/修改**

```
web/package.json                              + @xyflow/react
web/src/types/workflow.ts                     跨页面共用的 API 类型
web/src/workflows.ts                          图编辑纯函数、状态映射、权限与格式化
web/src/__tests__/workflow.test.ts
web/src/pages/Workflows.tsx                   列表 + 新建 + 模板实例化
web/src/pages/WorkflowEditor.tsx              React Flow 编辑器（含属性/定时页签）
web/src/pages/WorkflowRunDetail.tsx           运行详情 + 审批
web/src/components/workflow/WorkflowNode.tsx  自定义节点（编辑/运行两种状态）
web/src/components/workflow/NodePalette.tsx   左栏节点面板
web/src/components/workflow/NodeInspector.tsx 右栏属性面板
web/src/components/workflow/WorkflowCanvas.tsx 画布容器（节点/边、删除、迷你地图）
web/src/App.tsx                               三条路由（lazy 编辑与详情）
web/src/auth.ts                               导航项「工作流」
README.md / .env.example / CHANGELOG.md
```

---

## Task 1: 配置、模型与迁移

**Files:**
- Modify: `backend/app/config.py:48-56`
- Modify: `backend/app/models/agent.py:33-59`
- Create: `backend/app/models/workflow.py`
- Create: `backend/migrations/versions/e5f6a7b8c9d0_add_workflow_engine.py`
- Test: `backend/tests/test_workflow_models.py`

- [ ] **Step 1: 写失败的模型/配置测试**

```python
# backend/tests/test_workflow_models.py
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.models.agent import AgentRun
from app.models.workflow import (
    Workflow,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowVersion,
)


def test_workflow_settings_defaults():
    settings = Settings(_env_file=None)
    assert settings.workflow_max_parallel_nodes == 3
    assert settings.workflow_time_slice_seconds == 480
    assert settings.workflow_max_seconds == 3600
    assert settings.workflow_approval_ttl_hours == 24
    assert settings.workflow_max_active_runs_per_user == 2
    assert settings.workflow_run_stale_seconds == 300


def test_agent_run_has_workflow_columns():
    assert "workflow_run_id" in AgentRun.__table__.columns
    assert "workflow_node_id" in AgentRun.__table__.columns
    assert AgentRun.__table__.columns["workflow_run_id"].nullable is True
    assert AgentRun.__table__.columns["workflow_node_id"].nullable is True


async def test_node_attempt_is_unique_per_run(db_session, tenant_factory):
    tenant = await tenant_factory()
    workflow = Workflow(
        tenant_id=tenant.id, name="测试", created_by=uuid.uuid4()
    )
    db_session.add(workflow)
    await db_session.commit()
    version = WorkflowVersion(
        tenant_id=tenant.id,
        workflow_id=workflow.id,
        version=1,
        graph={"nodes": [], "edges": []},
        source_revision=1,
        published_by=uuid.uuid4(),
        published_at=datetime.now(UTC),
    )
    db_session.add(version)
    await db_session.commit()
    run = WorkflowRun(
        tenant_id=tenant.id,
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        trigger="manual",
        inputs={},
        policy={},
    )
    db_session.add(run)
    await db_session.commit()
    db_session.add(
        WorkflowNodeRun(
            tenant_id=tenant.id, run_id=run.id, node_id="n1", attempt=1,
            kind="tool", status="ok",
        )
    )
    await db_session.commit()
    db_session.add(
        WorkflowNodeRun(
            tenant_id=tenant.id, run_id=run.id, node_id="n1", attempt=1,
            kind="tool", status="ok",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_workflow_models.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.models.workflow'`。

- [ ] **Step 3: 加配置**

在 `backend/app/config.py` 的 `agent_run_stale_seconds` 之后追加：

```python
    workflow_max_parallel_nodes: int = 3
    workflow_time_slice_seconds: int = 480
    workflow_max_seconds: int = 3600
    workflow_approval_ttl_hours: int = 24
    workflow_max_active_runs_per_user: int = 2
    workflow_run_stale_seconds: int = 300
```

- [ ] **Step 4: 扩展 AgentRun**

在 `backend/app/models/agent.py` 的 `finished_at` 之前加：

```python
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=True
    )
    workflow_node_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
```

- [ ] **Step 5: 新建 workflow 模型**

```python
# backend/app/models/workflow.py
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import JSONType, TenantScoped


class Workflow(TenantScoped):
    __tablename__ = "workflows"
    __table_args__ = (
        Index("ix_workflows_tenant_archived", "tenant_id", "archived_at"),
    )

    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True))
    draft_graph: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    draft_revision: Mapped[int] = mapped_column(Integer, default=0)
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class WorkflowVersion(TenantScoped):
    __tablename__ = "workflow_versions"
    __table_args__ = (
        UniqueConstraint(
            "workflow_id", "version", name="uq_workflow_versions_workflow_version"
        ),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workflows.id", ondelete="CASCADE"),
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer)
    graph: Mapped[dict] = mapped_column(JSONType, default=dict)
    source_revision: Mapped[int] = mapped_column(Integer)
    published_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    note: Mapped[str] = mapped_column(Text, default="")


class WorkflowSchedule(TenantScoped):
    __tablename__ = "workflow_schedules"
    __table_args__ = (
        Index("ix_workflow_schedules_next_run", "tenant_id", "next_run_at"),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workflows.id", ondelete="CASCADE"),
        index=True,
    )
    workflow_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workflow_versions.id", ondelete="CASCADE")
    )
    cadence: Mapped[dict] = mapped_column(JSONType, default=dict)
    inputs: Mapped[dict] = mapped_column(JSONType, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True))


class WorkflowRun(TenantScoped):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_workflow_runs_tenant_key"
        ),
        UniqueConstraint(
            "schedule_id", "scheduled_for", name="uq_workflow_runs_schedule_slot"
        ),
        Index(
            "ix_workflow_runs_tenant_workflow_created",
            "tenant_id",
            "workflow_id",
            "created_at",
        ),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workflows.id", ondelete="CASCADE"),
        index=True,
    )
    workflow_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workflow_versions.id")
    )
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workflow_schedules.id", ondelete="SET NULL"),
        nullable=True,
    )
    scheduled_for: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    inputs: Mapped[dict] = mapped_column(JSONType, default=dict)
    policy: Mapped[dict] = mapped_column(JSONType, default=dict)
    result: Mapped[dict] = mapped_column(JSONType, default=dict)
    error_code: Mapped[str] = mapped_column(String(100), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    usage: Mapped[dict] = mapped_column(JSONType, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    resume_count: Mapped[int] = mapped_column(Integer, default=0)
    step_counter: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class WorkflowNodeRun(TenantScoped):
    __tablename__ = "workflow_node_runs"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "node_id",
            "attempt",
            name="uq_workflow_node_runs_run_node_attempt",
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        index=True,
    )
    node_id: Mapped[str] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="running")
    tool_step_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    input: Mapped[dict] = mapped_column(JSONType, default=dict)
    output: Mapped[dict] = mapped_column(JSONType, default=dict)
    raw_output: Mapped[dict] = mapped_column(JSONType, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class WorkflowApproval(TenantScoped):
    __tablename__ = "workflow_approvals"
    __table_args__ = (
        Index("ix_workflow_approvals_tenant_status", "tenant_id", "status"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        index=True,
    )
    node_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    tool_name: Mapped[str] = mapped_column(String(100), default="")
    risk: Mapped[str] = mapped_column(String(16), default="")
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    title: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    note: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

- [ ] **Step 6: 写迁移**

```python
# backend/migrations/versions/e5f6a7b8c9d0_add_workflow_engine.py
"""add workflow engine

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-11-15
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    op.add_column(
        "agent_runs", sa.Column("workflow_run_id", sa.Uuid(as_uuid=True), nullable=True)
    )
    op.add_column(
        "agent_runs",
        sa.Column("workflow_node_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_agent_runs_workflow_run_id", "agent_runs", ["workflow_run_id"]
    )

    op.create_table(
        "workflows",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("draft_graph", JSON, nullable=True),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workflows_tenant_id", "workflows", ["tenant_id"])
    op.create_index(
        "ix_workflows_tenant_archived", "workflows", ["tenant_id", "archived_at"]
    )

    op.create_table(
        "workflow_versions",
        sa.Column("workflow_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("graph", JSON, nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("published_by", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"], ["workflows.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workflow_id", "version", name="uq_workflow_versions_workflow_version"
        ),
    )
    op.create_index(
        "ix_workflow_versions_tenant_id", "workflow_versions", ["tenant_id"]
    )
    op.create_index(
        "ix_workflow_versions_workflow_id", "workflow_versions", ["workflow_id"]
    )

    op.create_table(
        "workflow_schedules",
        sa.Column("workflow_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("workflow_version_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("cadence", JSON, nullable=False),
        sa.Column("inputs", JSON, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"], ["workflows.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workflow_version_id"], ["workflow_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workflow_schedules_tenant_id", "workflow_schedules", ["tenant_id"]
    )
    op.create_index(
        "ix_workflow_schedules_workflow_id", "workflow_schedules", ["workflow_id"]
    )
    op.create_index(
        "ix_workflow_schedules_next_run_at", "workflow_schedules", ["next_run_at"]
    )
    op.create_index(
        "ix_workflow_schedules_next_run",
        "workflow_schedules",
        ["tenant_id", "next_run_at"],
    )

    op.create_table(
        "workflow_runs",
        sa.Column("workflow_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("workflow_version_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("triggered_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("schedule_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("inputs", JSON, nullable=False),
        sa.Column("policy", JSON, nullable=False),
        sa.Column("result", JSON, nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("usage", JSON, nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("resume_count", sa.Integer(), nullable=False),
        sa.Column("step_counter", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"], ["workflows.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workflow_version_id"], ["workflow_versions.id"]
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"], ["workflow_schedules.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_workflow_runs_tenant_key"
        ),
        sa.UniqueConstraint(
            "schedule_id", "scheduled_for", name="uq_workflow_runs_schedule_slot"
        ),
    )
    op.create_index("ix_workflow_runs_tenant_id", "workflow_runs", ["tenant_id"])
    op.create_index("ix_workflow_runs_workflow_id", "workflow_runs", ["workflow_id"])
    op.create_index("ix_workflow_runs_status", "workflow_runs", ["status"])
    op.create_index(
        "ix_workflow_runs_tenant_workflow_created",
        "workflow_runs",
        ["tenant_id", "workflow_id", "created_at"],
    )

    op.create_table(
        "workflow_node_runs",
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("tool_step_index", sa.Integer(), nullable=True),
        sa.Column("agent_run_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("input", JSON, nullable=False),
        sa.Column("output", JSON, nullable=False),
        sa.Column("raw_output", JSON, nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["workflow_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "node_id",
            "attempt",
            name="uq_workflow_node_runs_run_node_attempt",
        ),
    )
    op.create_index(
        "ix_workflow_node_runs_tenant_id", "workflow_node_runs", ["tenant_id"]
    )
    op.create_index(
        "ix_workflow_node_runs_run_id", "workflow_node_runs", ["run_id"]
    )

    op.create_table(
        "workflow_approvals",
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("risk", sa.String(length=16), nullable=False),
        sa.Column("payload", JSON, nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("decided_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["workflow_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workflow_approvals_tenant_id", "workflow_approvals", ["tenant_id"]
    )
    op.create_index(
        "ix_workflow_approvals_run_id", "workflow_approvals", ["run_id"]
    )
    op.create_index(
        "ix_workflow_approvals_tenant_status",
        "workflow_approvals",
        ["tenant_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("workflow_approvals")
    op.drop_table("workflow_node_runs")
    op.drop_table("workflow_runs")
    op.drop_table("workflow_schedules")
    op.drop_table("workflow_versions")
    op.drop_table("workflows")
    op.drop_index("ix_agent_runs_workflow_run_id", table_name="agent_runs")
    op.drop_column("agent_runs", "workflow_node_id")
    op.drop_column("agent_runs", "workflow_run_id")
```

- [ ] **Step 7: 运行模型测试与迁移检查**

Run: `cd backend && uv run pytest tests/test_workflow_models.py -q`
Expected: PASS。

Run: `cd backend && uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head`
Expected: 三次命令均成功；`downgrade` 后回到 `d4e5f6a7b8c9`。

Run: `cd backend && uv run ruff check app/models/workflow.py migrations/versions/e5f6a7b8c9d0_add_workflow_engine.py`
Expected: `All checks passed!`

- [ ] **Step 8: 提交**

```bash
git add backend/app/config.py backend/app/models/agent.py backend/app/models/workflow.py backend/migrations/versions/e5f6a7b8c9d0_add_workflow_engine.py backend/tests/test_workflow_models.py
git commit -m "feat(workflow): add models, settings and migration"
```

---

## Task 2: 图解析、拓扑、就绪判定与变量解析

**Files:**
- Create: `backend/app/workflow/__init__.py`
- Create: `backend/app/workflow/graph.py`
- Test: `backend/tests/test_workflow_graph.py`

- [ ] **Step 1: 写失败的测试**

```python
# backend/tests/test_workflow_graph.py
import pytest

from app.workflow.graph import (
    GraphError,
    VariableError,
    parse_graph,
    ready_nodes,
    resolve_value,
    topological_order,
)


def node(node_id, kind, **data):
    return {"id": node_id, "type": kind, "data": data}


def edge(source, target, handle=None):
    value = {"id": f"{source}-{target}", "source": source, "target": target}
    if handle is not None:
        value["sourceHandle"] = handle
    return value


def graph(nodes, edges):
    return {"nodes": nodes, "edges": edges}


def test_parse_graph_reads_nodes_edges_and_handles():
    raw = graph(
        [node("in", "input"), node("c", "condition"), node("out", "output")],
        [edge("in", "c"), edge("c", "out", "true")],
    )
    parsed = parse_graph(raw)
    assert list(parsed.nodes) == ["in", "c", "out"]
    assert parsed.outgoing["c"][0].handle == "true"
    assert parsed.incoming["out"][0].source == "c"


def test_parse_graph_rejects_duplicate_ids_and_unknown_kind():
    with pytest.raises(GraphError) as duplicate:
        parse_graph(graph([node("a", "input"), node("a", "output")], []))
    assert duplicate.value.code == "graph_invalid"
    with pytest.raises(GraphError) as unknown:
        parse_graph(graph([node("a", "loop")], []))
    assert unknown.value.code == "graph_invalid"


def test_parse_graph_accepts_cycle_but_topology_rejects_it():
    raw = graph(
        [node("a", "tool"), node("b", "tool")],
        [edge("a", "b"), edge("b", "a")],
    )
    parsed = parse_graph(raw)
    with pytest.raises(GraphError) as exc:
        topological_order(parsed)
    assert exc.value.code == "graph_cycle"


def test_topological_order_is_deterministic():
    raw = graph(
        [node("in", "input"), node("b", "tool"), node("a", "tool"), node("out", "output")],
        [edge("in", "b"), edge("in", "a"), edge("b", "out"), edge("a", "out")],
    )
    assert topological_order(parse_graph(raw)) == ("in", "a", "b", "out")


def test_resolve_value_keeps_original_type_for_whole_placeholder():
    resolved = resolve_value(
        {"limit": "{{nodes.n1.output.data.total}}", "topic": "{{inputs.topic}}"},
        inputs={"topic": "选题"},
        outputs={"n1": {"data": {"total": 7}}},
    )
    assert resolved == {"limit": 7, "topic": "选题"}


def test_resolve_value_supports_mixed_text_and_list_index():
    resolved = resolve_value(
        "标题：{{inputs.topic}} / 首条：{{nodes.n1.output.data.items.0.title}}",
        inputs={"topic": "春季"},
        outputs={"n1": {"data": {"items": [{"title": "第一条"}]}}},
    )
    assert resolved == "标题：春季 / 首条：第一条"


def test_resolve_value_rejects_missing_path_and_embedded_object():
    with pytest.raises(VariableError) as missing:
        resolve_value("{{inputs.topic}}", inputs={}, outputs={})
    assert "inputs.topic" in str(missing.value)
    with pytest.raises(VariableError):
        resolve_value("前缀 {{nodes.n1.output.data}}", inputs={}, outputs={"n1": {"data": {}}})


def test_ready_nodes_waits_for_join_and_propagates_skip():
    raw = graph(
        [
            node("in", "input"),
            node("a", "tool"),
            node("b", "tool"),
            node("join", "output"),
            node("right", "output"),
        ],
        [
            edge("in", "a"),
            edge("in", "b"),
            edge("a", "join"),
            edge("b", "join"),
            edge("in", "right"),
        ],
    )
    parsed = parse_graph(raw)
    ready, skipped = ready_nodes(parsed, {"in": "ok", "a": "ok"}, {})
    assert ready == ["b", "right"]
    assert skipped == []


def test_condition_branch_skips_the_other_side():
    raw = graph(
        [node("in", "input"), node("c", "condition"), node("yes", "output"), node("no", "output")],
        [
            edge("in", "c"),
            edge("c", "yes", "true"),
            edge("c", "no", "false"),
        ],
    )
    parsed = parse_graph(raw)
    statuses = {"in": "ok", "c": "ok"}
    ready, skipped = ready_nodes(parsed, statuses, {"c": "false"})
    assert ready == ["no"]
    assert skipped == ["yes"]
    # 被跳过的分支继续向下传播。
    ready2, skipped2 = ready_nodes(
        parsed, {**statuses, "yes": "skipped", "no": "ok"}, {"c": "false"}
    )
    assert ready2 == []
    assert skipped2 == []


def test_ready_nodes_propagates_skipped_upstream():
    raw = graph(
        [node("in", "input"), node("c", "tool"), node("out", "output")],
        [edge("in", "c"), edge("c", "out")],
    )
    parsed = parse_graph(raw)
    ready, skipped = ready_nodes(parsed, {"in": "ok", "c": "skipped"}, {})
    assert ready == []
    assert skipped == ["out"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_workflow_graph.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.workflow'`。

- [ ] **Step 3: 实现 graph.py**

```python
# backend/app/workflow/__init__.py
```

```python
# backend/app/workflow/graph.py
"""图结构读取、拓扑判定、就绪/跳过传播与受限变量解析。"""

import re
from dataclasses import dataclass
from typing import Any

NODE_KINDS = ("input", "tool", "agent", "condition", "review", "output")
NODE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
SETTLED = frozenset({"ok", "skipped", "denied", "error"})
_MISSING = object()


class GraphError(Exception):
    def __init__(self, code: str, message: str, node_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.node_id = node_id


class VariableError(Exception):
    pass


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    handle: str = ""


@dataclass(frozen=True)
class GraphNode:
    id: str
    kind: str
    data: dict
    position: dict


@dataclass(frozen=True)
class Graph:
    nodes: dict[str, GraphNode]
    edges: tuple[GraphEdge, ...]
    incoming: dict[str, tuple[GraphEdge, ...]]
    outgoing: dict[str, tuple[GraphEdge, ...]]


def parse_graph(raw: Any) -> Graph:
    if not isinstance(raw, dict):
        raise GraphError("graph_invalid", "图必须是对象")
    nodes_raw = raw.get("nodes")
    edges_raw = raw.get("edges")
    if not isinstance(nodes_raw, list) or not isinstance(edges_raw, list):
        raise GraphError("graph_invalid", "nodes 与 edges 必须是数组")
    nodes: dict[str, GraphNode] = {}
    for item in nodes_raw:
        if not isinstance(item, dict):
            raise GraphError("graph_invalid", "节点必须是对象")
        node_id = item.get("id")
        if not isinstance(node_id, str) or not NODE_ID_RE.match(node_id):
            raise GraphError("graph_invalid", f"节点 id 不合法: {node_id}")
        if node_id in nodes:
            raise GraphError("graph_invalid", f"节点 id 重复: {node_id}", node_id)
        kind = item.get("type")
        if kind not in NODE_KINDS:
            raise GraphError("graph_invalid", f"未知节点类型: {kind}", node_id)
        data = item.get("data") or {}
        if not isinstance(data, dict):
            raise GraphError("graph_invalid", "节点 data 必须是对象", node_id)
        position = item.get("position") or {}
        if not isinstance(position, dict):
            position = {}
        nodes[node_id] = GraphNode(node_id, str(kind), data, position)

    edges: list[GraphEdge] = []
    seen: set[tuple[str, str, str]] = set()
    for item in edges_raw:
        if not isinstance(item, dict):
            raise GraphError("graph_invalid", "边必须是对象")
        source = item.get("source")
        target = item.get("target")
        if source not in nodes or target not in nodes:
            raise GraphError(
                "graph_invalid", f"边引用了不存在的节点: {source} → {target}"
            )
        handle = item.get("sourceHandle") or ""
        if handle not in {"", "true", "false"}:
            raise GraphError("edge_invalid", f"未知句柄: {handle}", str(source))
        key = (str(source), str(target), str(handle))
        if key in seen:
            raise GraphError("graph_invalid", "存在重复的边")
        seen.add(key)
        edges.append(GraphEdge(str(source), str(target), str(handle)))

    incoming: dict[str, list[GraphEdge]] = {node_id: [] for node_id in nodes}
    outgoing: dict[str, list[GraphEdge]] = {node_id: [] for node_id in nodes}
    for item in edges:
        outgoing[item.source].append(item)
        incoming[item.target].append(item)
    return Graph(
        nodes=nodes,
        edges=tuple(edges),
        incoming={key: tuple(value) for key, value in incoming.items()},
        outgoing={key: tuple(value) for key, value in outgoing.items()},
    )


def topological_order(graph: Graph) -> tuple[str, ...]:
    indegree = {node_id: 0 for node_id in graph.nodes}
    for item in graph.edges:
        indegree[item.target] += 1
    ready = sorted(node_id for node_id, degree in indegree.items() if degree == 0)
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for item in sorted(graph.outgoing[current], key=lambda e: e.target):
            indegree[item.target] -= 1
            if indegree[item.target] == 0:
                ready.append(item.target)
                ready.sort()
    if len(order) != len(graph.nodes):
        raise GraphError("graph_cycle", "工作流存在环")
    return tuple(order)


def edge_active(
    item: GraphEdge, statuses: dict[str, str], choices: dict[str, str]
) -> bool:
    if statuses.get(item.source) != "ok":
        return False
    if item.handle:
        return choices.get(item.source) == item.handle
    return True


def ready_nodes(
    graph: Graph,
    statuses: dict[str, str],
    choices: dict[str, str],
) -> tuple[list[str], list[str]]:
    """返回 (可执行节点, 应跳过节点)。

    入边未全部落定时节点既不可执行也不跳过；全部落定后按是否有激活入边二选一。
    没有入边的节点不在这里处理（唯一合法的无入边节点是 input，由引擎启动时写入）。
    """
    ready: list[str] = []
    skipped: list[str] = []
    for node_id in sorted(graph.nodes):
        if statuses.get(node_id) in SETTLED:
            continue
        incoming = graph.incoming[node_id]
        if not incoming:
            continue
        if any(statuses.get(item.source) not in SETTLED for item in incoming):
            continue
        if any(edge_active(item, statuses, choices) for item in incoming):
            ready.append(node_id)
        else:
            skipped.append(node_id)
    return ready, skipped


def condition_choice(
    node_id: str, status: str | None, raw_outputs: dict[str, Any]
) -> str | None:
    if status != "ok":
        return None
    value = raw_outputs.get(node_id)
    if isinstance(value, dict) and isinstance(value.get("result"), bool):
        return "true" if value["result"] else "false"
    return None


def _lookup(reference: str, inputs: dict, outputs: dict) -> Any:
    segments = reference.strip().split(".")
    if segments[0] == "inputs":
        if len(segments) < 2:
            raise VariableError(f"变量路径不完整: {reference}")
        current = inputs.get(segments[1], _MISSING)
        rest = segments[2:]
    elif segments[0] == "nodes":
        if len(segments) < 3 or segments[2] != "output":
            raise VariableError(f"变量路径不合法: {reference}")
        node_id = segments[1]
        if node_id not in outputs:
            raise VariableError(f"节点尚未产出: {node_id}")
        current = outputs[node_id]
        rest = segments[3:]
    else:
        raise VariableError(f"变量路径不合法: {reference}")
    if current is _MISSING:
        raise VariableError(f"变量不存在: {reference}")
    for segment in rest:
        if isinstance(current, dict) and segment in current:
            current = current[segment]
            continue
        if isinstance(current, list) and segment.isdigit():
            index = int(segment)
            if index < len(current):
                current = current[index]
                continue
        raise VariableError(f"变量路径不存在: {reference}")
    return current


def resolve_value(value: Any, *, inputs: dict, outputs: dict) -> Any:
    """解析字符串里的 ``{{...}}``；整值占位符保持原始类型。"""
    if isinstance(value, dict):
        return {
            key: resolve_value(item, inputs=inputs, outputs=outputs)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [resolve_value(item, inputs=inputs, outputs=outputs) for item in value]
    if not isinstance(value, str):
        return value
    matches = list(PLACEHOLDER_RE.finditer(value))
    if not matches:
        return value
    if len(matches) == 1 and matches[0].span() == (0, len(value)):
        return _lookup(matches[0].group(1), inputs, outputs)
    parts: list[str] = []
    last = 0
    for match in matches:
        parts.append(value[last : match.start()])
        resolved = _lookup(match.group(1), inputs, outputs)
        if isinstance(resolved, (dict, list)):
            raise VariableError("对象或数组不能嵌入文本")
        parts.append("" if resolved is None else str(resolved))
        last = match.end()
    parts.append(value[last:])
    return "".join(parts)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_workflow_graph.py -q`
Expected: PASS（10 passed）。

Run: `cd backend && uv run ruff check app/workflow/graph.py`
Expected: `All checks passed!`

- [ ] **Step 5: 提交**

```bash
git add backend/app/workflow/__init__.py backend/app/workflow/graph.py backend/tests/test_workflow_graph.py
git commit -m "feat(workflow): add graph parsing and variable resolution"
```

---

## Task 3: 草稿结构校验与发布校验

**Files:**
- Create: `backend/app/workflow/validate.py`
- Test: `backend/tests/test_workflow_validate.py`

- [ ] **Step 1: 写失败的测试**

```python
# backend/tests/test_workflow_validate.py
import copy

from app.agent.registry import get_tool_registry
from app.workflow.validate import check_structure, validate_publish


def node(node_id, kind, **data):
    return {"id": node_id, "type": kind, "data": data}


def edge(source, target, handle=None):
    value = {"id": f"{source}-{target}", "source": source, "target": target}
    if handle is not None:
        value["sourceHandle"] = handle
    return value


def valid_graph():
    return {
        "nodes": [
            node(
                "in",
                "input",
                fields=[{"key": "topic", "type": "string", "required": True}],
            ),
            node("read", "tool", tool="profile.list", args={"limit": 5}, retry=1),
            node("out", "output"),
        ],
        "edges": [edge("in", "read"), edge("read", "out")],
    }


def codes(result):
    return {issue.code for issue in result.issues}


def publish(raw, overrides=None):
    return validate_publish(
        raw,
        registry=get_tool_registry(),
        skill_ids={"xhs-note"},
        overrides=overrides or {},
    )


def test_valid_graph_passes():
    result = publish(valid_graph())
    assert result.ok is True
    assert result.issues == []


def test_structure_allows_incomplete_draft_but_rejects_bad_shape():
    draft = copy.deepcopy(valid_graph())
    draft["nodes"] = [draft["nodes"][0]]
    draft["edges"] = []
    assert check_structure(draft).ok is True
    assert check_structure({"nodes": "x", "edges": []}).ok is False


def test_publish_requires_single_input_and_one_output():
    raw = copy.deepcopy(valid_graph())
    raw["nodes"].append(
        node("in2", "input", fields=[{"key": "x", "type": "string"}])
    )
    issue_codes = codes(publish(raw))
    assert "input_invalid" in issue_codes

    raw = copy.deepcopy(valid_graph())
    raw["nodes"] = [raw["nodes"][0], raw["nodes"][1]]
    raw["edges"] = [edge("in", "read")]
    assert "output_missing" in codes(publish(raw))


def test_publish_rejects_cycle_and_unreachable_node():
    raw = copy.deepcopy(valid_graph())
    raw["edges"].append(edge("out", "read"))
    assert "graph_cycle" in codes(publish(raw))

    raw = copy.deepcopy(valid_graph())
    raw["nodes"].append(node("lonely", "tool", tool="profile.list", args={}))
    assert "unreachable_node" in codes(publish(raw))


def test_condition_requires_two_labeled_edges():
    raw = {
        "nodes": [
            node("in", "input", fields=[{"key": "topic", "type": "string"}]),
            node("c", "condition", left="{{inputs.topic}}", operator="not_empty"),
            node("out", "output"),
        ],
        "edges": [edge("in", "c", "true"), edge("c", "out", "true")],
    }
    issue_codes = codes(publish(raw))
    assert "condition_invalid" in issue_codes
    assert "edge_invalid" in issue_codes


def test_review_needs_input_and_output_and_title():
    raw = {
        "nodes": [
            node("in", "input", fields=[{"key": "topic", "type": "string"}]),
            node("r", "review", title=""),
            node("out", "output"),
        ],
        "edges": [edge("in", "r"), edge("r", "out")],
    }
    assert "review_invalid" in codes(publish(raw))


def test_unknown_tool_skill_and_agent_tools_are_rejected():
    raw = copy.deepcopy(valid_graph())
    raw["nodes"][1]["data"]["tool"] = "publish.now"
    assert "tool_unknown" in codes(publish(raw))

    raw = copy.deepcopy(valid_graph())
    raw["nodes"][1]["data"] = {"tool": "skill.run", "args": {"skill_id": "nope"}}
    assert "skill_unknown" in codes(publish(raw))

    raw = copy.deepcopy(valid_graph())
    raw["nodes"].insert(
        1,
        node(
            "a",
            "agent",
            goal="写一篇 {{inputs.topic}}",
            tools=["nope.*"],
            budget={"max_steps": 3},
        ),
    )
    raw["edges"] = [edge("in", "a"), edge("a", "read"), edge("read", "out")]
    assert "agent_tools_unknown" in codes(publish(raw))


def test_retry_and_input_fields_are_validated():
    raw = copy.deepcopy(valid_graph())
    raw["nodes"][1]["data"]["retry"] = 9
    assert "node_invalid" in codes(publish(raw))

    raw = copy.deepcopy(valid_graph())
    raw["nodes"][0]["data"]["fields"] = [
        {"key": "Bad Key", "type": "string"}
    ]
    assert "input_invalid" in codes(publish(raw))

    raw = copy.deepcopy(valid_graph())
    raw["nodes"][0]["data"]["fields"] = []
    assert "input_invalid" in codes(publish(raw))


def test_denied_tool_is_only_a_warning():
    result = publish(
        valid_graph(), overrides={"tool:profile.list": "deny"}
    )
    assert result.ok is True
    warnings = [issue for issue in result.issues if issue.severity == "warning"]
    assert [issue.code for issue in warnings] == ["policy_denies_tool"]


def test_size_limit_is_enforced():
    raw = copy.deepcopy(valid_graph())
    raw["nodes"][0]["data"]["fields"] = [
        {"key": "topic", "type": "string", "maxLength": 10**7}
    ]
    # 图本身未超限：maxLength 不是体积。
    assert publish(raw).ok is True
    raw["nodes"][1]["data"]["args"] = {"note": "x" * (300 * 1024)}
    assert "size_exceeded" in codes(publish(raw))
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_workflow_validate.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.workflow.validate'`。

- [ ] **Step 3: 实现 validate.py**

```python
# backend/app/workflow/validate.py
"""草稿结构校验与发布校验。保存只做结构检查，发布必须全部通过。"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.agent.policy import resolve_policy
from app.agent.registry import ToolRegistry
from app.workflow.graph import (
    Graph,
    GraphError,
    GraphNode,
    NODE_ID_RE,
    parse_graph,
    topological_order,
)

MAX_NODES = 50
MAX_EDGES = 100
MAX_BYTES = 256 * 1024
FIELD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
FIELD_TYPES = {"string", "number", "boolean"}
OPERATORS = {"equals", "not_equals", "contains", "not_empty", "gt", "lt"}


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    node_id: str = ""
    severity: str = "error"

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "node_id": self.node_id,
            "severity": self.severity,
        }


@dataclass
class ValidationResult:
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "issues": [issue.as_dict() for issue in self.issues]}


def _size_bytes(raw: Any) -> int:
    return len(json.dumps(raw, ensure_ascii=False, default=str).encode())


def check_structure(raw: Any) -> ValidationResult:
    if _size_bytes(raw) > MAX_BYTES:
        return ValidationResult(
            [Issue("size_exceeded", "图超过 256 KB")]
        )
    try:
        graph = parse_graph(raw)
    except GraphError as exc:
        return ValidationResult(
            [Issue(exc.code, exc.message, exc.node_id or "")]
        )
    issues: list[Issue] = []
    if len(graph.nodes) > MAX_NODES:
        issues.append(Issue("size_exceeded", f"节点不能超过 {MAX_NODES} 个"))
    if len(graph.edges) > MAX_EDGES:
        issues.append(Issue("size_exceeded", f"边不能超过 {MAX_EDGES} 条"))
    return ValidationResult(issues)


def _reachable(graph: Graph, start: str) -> set[str]:
    seen = {start}
    stack = [start]
    while stack:
        current = stack.pop()
        for item in graph.outgoing[current]:
            if item.target not in seen:
                seen.add(item.target)
                stack.append(item.target)
    return seen


def _can_reach_outputs(graph: Graph) -> set[str]:
    outputs = {node.id for node in graph.nodes.values() if node.kind == "output"}
    seen = set(outputs)
    stack = list(outputs)
    reverse: dict[str, list[str]] = {node_id: [] for node_id in graph.nodes}
    for item in graph.edges:
        reverse[item.target].append(item.source)
    while stack:
        current = stack.pop()
        for source in reverse[current]:
            if source not in seen:
                seen.add(source)
                stack.append(source)
    return seen


def _matches_tool(name: str, patterns: list[str], registry: ToolRegistry) -> bool:
    names = registry.names()
    for pattern in patterns:
        if not isinstance(pattern, str):
            continue
        if pattern.endswith(".*"):
            prefix = pattern[:-1]
            if any(candidate.startswith(prefix) for candidate in names):
                return True
        elif pattern in names:
            return True
    return False


def _check_fields(node: GraphNode, issues: list[Issue]) -> list[str]:
    fields = node.data.get("fields")
    if not isinstance(fields, list) or not fields:
        issues.append(Issue("input_invalid", "输入节点至少需要一个字段", node.id))
        return []
    keys: list[str] = []
    for item in fields:
        if not isinstance(item, dict):
            issues.append(Issue("input_invalid", "字段必须是对象", node.id))
            continue
        key = item.get("key")
        if not isinstance(key, str) or not FIELD_KEY_RE.match(key):
            issues.append(Issue("input_invalid", f"字段 key 不合法: {key}", node.id))
            continue
        if key in keys:
            issues.append(Issue("input_invalid", f"字段 key 重复: {key}", node.id))
            continue
        keys.append(key)
        if item.get("type") not in FIELD_TYPES:
            issues.append(Issue("input_invalid", f"字段 {key} 类型不合法", node.id))
        max_length = item.get("maxLength")
        if max_length is not None and (
            not isinstance(max_length, int) or max_length <= 0
        ):
            issues.append(Issue("input_invalid", f"字段 {key} maxLength 不合法", node.id))
        if not isinstance(item.get("required", False), bool):
            issues.append(Issue("input_invalid", f"字段 {key} required 必须是布尔值", node.id))
    return keys


def _check_node(
    graph: Graph,
    node: GraphNode,
    *,
    registry: ToolRegistry,
    skill_ids: set[str],
    overrides: dict,
    issues: list[Issue],
) -> None:
    data = node.data
    incoming = graph.incoming[node.id]
    outgoing = graph.outgoing[node.id]
    if node.kind == "input":
        if incoming:
            issues.append(Issue("input_invalid", "输入节点不能有入边", node.id))
        _check_fields(node, issues)
    elif node.kind == "output":
        if outgoing:
            issues.append(Issue("node_invalid", "输出节点不能有出边", node.id))
    elif node.kind == "condition":
        handles = sorted(item.handle for item in outgoing)
        if handles != ["false", "true"]:
            issues.append(
                Issue("condition_invalid", "条件节点必须有一条 true 和一条 false 出边", node.id)
            )
        if data.get("operator") not in OPERATORS:
            issues.append(Issue("condition_invalid", "条件运算符不合法", node.id))
        elif data["operator"] != "not_empty" and "value" not in data:
            issues.append(Issue("condition_invalid", "条件缺少比较值", node.id))
        if not isinstance(data.get("left"), str) or not data["left"].strip():
            issues.append(Issue("condition_invalid", "条件缺少左值", node.id))
    elif node.kind == "review":
        if not incoming or not outgoing:
            issues.append(Issue("review_invalid", "审核节点必须有入边与出边", node.id))
        title = data.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            issues.append(Issue("review_invalid", "审核标题长度必须为 1–200", node.id))
    elif node.kind == "tool":
        name = data.get("tool")
        spec = registry.get(name) if isinstance(name, str) else None
        if spec is None:
            issues.append(Issue("tool_unknown", f"未知工具: {name}", node.id))
        else:
            if resolve_policy(overrides, spec) == "deny":
                issues.append(
                    Issue(
                        "policy_denies_tool",
                        f"当前策略拒绝 {name}，运行时会失败",
                        node.id,
                        severity="warning",
                    )
                )
            args = data.get("args", {})
            if not isinstance(args, dict):
                issues.append(Issue("node_invalid", "工具参数必须是对象", node.id))
            if name == "skill.run":
                skill_id = (args or {}).get("skill_id") if isinstance(args, dict) else None
                if not isinstance(skill_id, str) or skill_id not in skill_ids:
                    issues.append(Issue("skill_unknown", f"未知技能: {skill_id}", node.id))
    elif node.kind == "agent":
        goal = data.get("goal")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 4000:
            issues.append(Issue("node_invalid", "Agent 目标长度必须为 1–4000", node.id))
        tools = data.get("tools") or []
        if not isinstance(tools, list):
            issues.append(Issue("node_invalid", "Agent 工具白名单必须是数组", node.id))
        elif tools and not _matches_tool("", tools, registry):
            issues.append(Issue("agent_tools_unknown", "工具白名单没有匹配任何工具", node.id))
        budget = data.get("budget") or {}
        if not isinstance(budget, dict):
            issues.append(Issue("node_invalid", "Agent 预算必须是对象", node.id))
        else:
            for key, value in budget.items():
                if key not in {"max_steps", "max_tokens", "max_tool_calls", "max_seconds"}:
                    issues.append(Issue("node_invalid", f"未知预算维度: {key}", node.id))
                elif (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value <= 0
                ):
                    issues.append(Issue("node_invalid", f"预算 {key} 必须是正整数", node.id))
    if node.kind != "condition" and any(item.handle for item in outgoing):
        issues.append(Issue("edge_invalid", "只有条件节点的出边可以带句柄", node.id))
    retry = data.get("retry", 0)
    if not isinstance(retry, int) or isinstance(retry, bool) or not 0 <= retry <= 3:
        issues.append(Issue("node_invalid", "retry 必须是 0–3 的整数", node.id))


def validate_publish(
    raw: Any,
    *,
    registry: ToolRegistry,
    skill_ids: set[str],
    overrides: dict | None = None,
) -> ValidationResult:
    structure = check_structure(raw)
    if not structure.ok:
        return structure
    graph = parse_graph(raw)
    issues: list[Issue] = []
    inputs = [node for node in graph.nodes.values() if node.kind == "input"]
    outputs = [node for node in graph.nodes.values() if node.kind == "output"]
    if len(inputs) != 1:
        issues.append(Issue("input_invalid", "必须恰好有一个输入节点"))
    if not outputs:
        issues.append(Issue("output_missing", "至少需要一个输出节点"))
    if len(inputs) == 1:
        reachable = _reachable(graph, inputs[0].id)
        for node_id in graph.nodes:
            if node_id not in reachable:
                issues.append(Issue("unreachable_node", "节点从输入节点不可达", node_id))
    if outputs:
        reaching = _can_reach_outputs(graph)
        for node_id in graph.nodes:
            if node_id not in reaching:
                issues.append(Issue("unreachable_node", "节点无法到达任何输出", node_id))
    try:
        topological_order(graph)
    except GraphError as exc:
        issues.append(Issue(exc.code, exc.message))
    for node in graph.nodes.values():
        _check_node(
            graph,
            node,
            registry=registry,
            skill_ids=skill_ids,
            overrides=overrides or {},
            issues=issues,
        )
    # 同一节点/同一 code 只保留一条，错误优先于警告。
    unique: dict[tuple[str, str, str], Issue] = {}
    for issue in issues:
        key = (issue.code, issue.node_id, issue.message)
        current = unique.get(key)
        if current is None or (current.severity == "warning" and issue.severity == "error"):
            unique[key] = issue
    return ValidationResult(list(unique.values()))
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_workflow_validate.py -q`
Expected: PASS（10 passed）。

Run: `cd backend && uv run ruff check app/workflow/validate.py`
Expected: `All checks passed!`

- [ ] **Step 5: 提交**

```bash
git add backend/app/workflow/validate.py backend/tests/test_workflow_validate.py
git commit -m "feat(workflow): add draft and publish validation"
```

---

## Task 4: 定时 cadence 与 `next_run_at`

**Files:**
- Create: `backend/app/workflow/schedule.py`
- Test: `backend/tests/test_workflow_schedule.py`

**背景与接口：** 规格只支持「每日/每周 + 时分 + 时区」，不引入 cron 表达式解析。`parse_cadence(raw)` 负责把 JSON（来自 API 请求或数据库 `cadence` 列）变成 `Cadence`，非法输入一律抛 `AppError("workflow_schedule_invalid", …, 422)`。`next_run_at(cadence, after)` 返回严格晚于 `after` 的第一个触发时刻，统一转成 UTC。worker 的 `fire_due_workflow_schedules` 每分钟用 `next_run_at(cadence, now)` 推进 `next_run_at`，因此这里必须是纯函数，不读数据库、不读配置。

**关键约定（实现时照抄，不要自行改动）：**
- ISO 星期：周一 = 1 … 周日 = 7。`to_json()` 输出排序去重后的 `weekdays`，`daily` 输出空列表。
- 时间字符串必须是零填充的 `HH:MM`（`9:00` 非法），避免同一含义多种写法。
- `after` 必须是 aware datetime；naive 直接 422，不做猜测性补时区。
- 夏令时缺口/重叠按 `zoneinfo` 默认（`fold=0`）归一：缺口顺延到表盘时间之后，重叠取第一次。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_workflow_schedule.py`:

```python
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.errors import AppError
from app.workflow.schedule import Cadence, next_run_at, parse_cadence

SHANGHAI = "Asia/Shanghai"


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def daily(hour: int = 9, minute: int = 0, zone: str = SHANGHAI) -> Cadence:
    return parse_cadence(
        {"kind": "daily", "time": f"{hour:02d}:{minute:02d}", "timezone": zone}
    )


def weekly(weekdays, hour: int = 9, zone: str = SHANGHAI) -> Cadence:
    return parse_cadence(
        {
            "kind": "weekly",
            "weekdays": list(weekdays),
            "time": f"{hour:02d}:00",
            "timezone": zone,
        }
    )


def test_parse_daily_ignores_weekdays():
    cadence = parse_cadence(
        {
            "kind": "daily",
            "time": "09:30",
            "timezone": SHANGHAI,
            "weekdays": [3],
        }
    )
    assert cadence == Cadence(
        kind="daily", weekdays=(), hour=9, minute=30, timezone=SHANGHAI
    )
    assert cadence.to_json() == {
        "kind": "daily",
        "weekdays": [],
        "time": "09:30",
        "timezone": SHANGHAI,
    }


def test_parse_weekly_sorts_and_dedupes_weekdays():
    cadence = parse_cadence(
        {
            "kind": "weekly",
            "time": "18:05",
            "timezone": SHANGHAI,
            "weekdays": [5, 1, 5],
        }
    )
    assert cadence.weekdays == (1, 5)
    assert cadence.to_json()["weekdays"] == [1, 5]
    assert cadence.to_json()["time"] == "18:05"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "daily",
        {},
        {"kind": "hourly", "time": "09:00", "timezone": SHANGHAI},
        {"kind": "daily", "time": "9:00", "timezone": SHANGHAI},
        {"kind": "daily", "time": "24:00", "timezone": SHANGHAI},
        {"kind": "daily", "time": "09:60", "timezone": SHANGHAI},
        {"kind": "daily", "time": "09:00"},
        {"kind": "daily", "time": "09:00", "timezone": "Mars/Olympus"},
        {"kind": "weekly", "time": "09:00", "timezone": SHANGHAI},
        {"kind": "weekly", "time": "09:00", "timezone": SHANGHAI, "weekdays": []},
        {
            "kind": "weekly",
            "time": "09:00",
            "timezone": SHANGHAI,
            "weekdays": [0, 8],
        },
        {
            "kind": "weekly",
            "time": "09:00",
            "timezone": SHANGHAI,
            "weekdays": [True],
        },
    ],
)
def test_parse_rejects_invalid_cadence(raw):
    with pytest.raises(AppError) as excinfo:
        parse_cadence(raw)
    assert excinfo.value.code == "workflow_schedule_invalid"
    assert excinfo.value.status == 422


def test_daily_same_day_when_time_not_passed():
    # 2026-11-16 00:30 UTC = 上海 08:30
    assert next_run_at(daily(), utc(2026, 11, 16, 0, 30)) == utc(2026, 11, 16, 1, 0)


def test_daily_rolls_to_next_day_when_time_reached():
    # 恰好到点也取下一次，保证严格大于 after
    assert next_run_at(daily(), utc(2026, 11, 16, 1, 0)) == utc(2026, 11, 17, 1, 0)


def test_daily_crosses_month_boundary():
    # 2026-01-31 10:00 上海 → 2026-02-01 09:00 上海
    assert next_run_at(daily(), utc(2026, 1, 31, 2, 0)) == utc(2026, 2, 1, 1, 0)


def test_weekly_picks_next_matching_weekday():
    cadence = weekly([1, 4])  # 周一、周四
    assert next_run_at(cadence, utc(2026, 11, 16, 0, 0)) == utc(2026, 11, 16, 1, 0)
    assert next_run_at(cadence, utc(2026, 11, 16, 2, 0)) == utc(2026, 11, 19, 1, 0)


def test_weekly_wraps_to_next_week():
    cadence = weekly([1])
    assert next_run_at(cadence, utc(2026, 11, 16, 2, 0)) == utc(2026, 11, 23, 1, 0)


def test_next_run_at_accepts_after_in_other_timezone():
    after = datetime(2026, 11, 16, 8, 30, tzinfo=ZoneInfo(SHANGHAI))
    assert next_run_at(daily(), after) == utc(2026, 11, 16, 1, 0)


def test_next_run_at_follows_dst_offset_change():
    cadence = daily(zone="America/New_York")
    # 2026-03-08 美国进入夏令时：09:00 从 UTC-5 变成 UTC-4
    assert next_run_at(cadence, utc(2026, 3, 7, 6, 0)) == utc(2026, 3, 7, 14, 0)
    assert next_run_at(cadence, utc(2026, 3, 7, 15, 0)) == utc(2026, 3, 8, 13, 0)


def test_next_run_at_requires_aware_after():
    with pytest.raises(AppError) as excinfo:
        next_run_at(daily(), datetime(2026, 11, 16, 0, 30))
    assert excinfo.value.code == "workflow_schedule_invalid"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_workflow_schedule.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.workflow.schedule'`。

- [ ] **Step 3: 写最小实现**

Create `backend/app/workflow/schedule.py`:

```python
"""cadence 解析与下次触发时刻计算。

只支持「每日/每周 + 时分 + 时区」，用标准库 zoneinfo，不引入 cron 依赖。
所有计算结果是 UTC aware datetime，方便直接与数据库列比较。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.errors import AppError

TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
KINDS = frozenset({"daily", "weekly"})
# weekly 一定能在 7 天内命中；多留一天只在异常输入下兜底
MAX_DAYS_AHEAD = 8


@dataclass(frozen=True)
class Cadence:
    """kind 为 daily 时 weekdays 恒为空元组。"""

    kind: str
    weekdays: tuple[int, ...]
    hour: int
    minute: int
    timezone: str

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "weekdays": list(self.weekdays),
            "time": f"{self.hour:02d}:{self.minute:02d}",
            "timezone": self.timezone,
        }


def _invalid(message: str) -> AppError:
    return AppError("workflow_schedule_invalid", message, 422)


def parse_cadence(raw: object) -> Cadence:
    if not isinstance(raw, dict):
        raise _invalid("cadence 必须是对象")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise _invalid("kind 只能是 daily 或 weekly")
    match = TIME_RE.match(str(raw.get("time") or ""))
    if match is None:
        raise _invalid("time 必须形如 09:00")
    hour, minute = int(match.group(1)), int(match.group(2))
    timezone = raw.get("timezone")
    if not isinstance(timezone, str) or not timezone.strip():
        raise _invalid("timezone 不能为空")
    timezone = timezone.strip()
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise _invalid(f"未知时区：{timezone}") from None

    weekdays: tuple[int, ...] = ()
    if kind == "weekly":
        values = raw.get("weekdays")
        if not isinstance(values, (list, tuple)) or not values:
            raise _invalid("weekly 需要非空的 weekdays")
        for value in values:
            # bool 是 int 的子类，必须显式排除，否则 True 会被当成周一
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= 7
            ):
                raise _invalid("weekdays 只能取 1-7 的整数（周一为 1）")
        weekdays = tuple(sorted(set(values)))

    return Cadence(
        kind=kind, weekdays=weekdays, hour=hour, minute=minute, timezone=timezone
    )


def next_run_at(cadence: Cadence, after: datetime) -> datetime:
    """返回严格晚于 after 的第一个触发时刻（UTC）。

    夏令时缺口/重叠按 zoneinfo 默认 fold=0 归一：缺口顺延到表盘时间之后，
    重叠取第一次出现，和「每天 09:00」的直觉一致。
    """
    if after.tzinfo is None:
        raise _invalid("after 必须是带时区的时间")
    zone = ZoneInfo(cadence.timezone)
    local = after.astimezone(zone)
    start = local.date()
    for offset in range(MAX_DAYS_AHEAD):
        current: date = start + timedelta(days=offset)
        if cadence.kind == "weekly" and current.isoweekday() not in cadence.weekdays:
            continue
        candidate = datetime.combine(
            current, time(cadence.hour, cadence.minute), tzinfo=zone
        )
        if candidate > local:
            return candidate.astimezone(UTC)
    raise _invalid("无法计算下次运行时间")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_workflow_schedule.py -q`
Expected: PASS（23 passed）。

Run: `cd backend && uv run ruff check app/workflow/schedule.py`
Expected: `All checks passed!`

- [ ] **Step 5: 提交**

```bash
git add backend/app/workflow/schedule.py backend/tests/test_workflow_schedule.py
git commit -m "feat(workflow): add schedule cadence calculation"
```

---

## Task 5: 工作流审批读写与决策

**Files:**
- Create: `backend/app/workflow/approvals.py`
- Test: `backend/tests/test_workflow_approvals.py`

**背景与接口：** 工作流级审批覆盖工具节点的 `tool_confirm` 与审核节点的 `review`，存在 `workflow_approvals`。Agent 子运行内部的 `confirm` 仍走第 1 层 `agent_approvals`，本模块用一条 join 查询把它算进「这个运行还有没有待处理审批」。并行分支可能同时挂起多条：通过一条时若还有任何通道的 pending 审批，运行保持 `waiting_approval`；最后一条通过才 CAS 回 `queued` 并把等待时长累加进 `usage.paused_ms`。驳回任一条即终止运行。

**关键约定（实现时照抄）：**
- `create_approval` 只 `add` 不 `commit`，由调用方与运行/节点状态一起提交；`decide_approval` 自己提交。
- 决策先做 CAS（`pending → approved/rejected`），运行状态 CAS 失败时整体回滚，避免留下「审批已通过但运行已取消」的脏数据。
- 审核驳回 → 运行 `failed/review_rejected`；工具审批驳回 → `failed/approval_rejected`。两种驳回都把该节点 `waiting → denied`，并把同运行其他 pending 工作流审批置 `expired`；同时把该运行的子 `AgentRun` 置 `cancelled`、它们的 pending `agent_approvals` 置 `expired`、在途 `AgentStep` 置 `interrupted`。否则驳回后仍能审批一个永远不会继续的分支。
- `node_approval` 优先返回 pending；没有 pending 时返回「最近决定」的一条（按 `decided_at`，其次 `expires_at`，最后 `created_at`）。SQLite 的 `created_at` 只有秒级精度，不能只靠它排序。
- 决策本身不做角色/自批校验，那是 API 层的事（Task 11）。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_workflow_approvals.py`:

```python
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.errors import AppError
from app.models.agent import AgentApproval, AgentRun, AgentStep
from app.models.workflow import (
    Workflow,
    WorkflowApproval,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowVersion,
)
from app.workflow import approvals

NOW = datetime.now(UTC)


async def make_run(
    db_session,
    *,
    status: str = "waiting_approval",
    heartbeat_at: datetime | None = None,
    usage: dict | None = None,
) -> WorkflowRun:
    workflow = Workflow(name="测试流", created_by=uuid.uuid4())
    db_session.add(workflow)
    await db_session.flush()
    version = WorkflowVersion(
        workflow_id=workflow.id,
        version=1,
        graph={"nodes": [], "edges": []},
        source_revision=0,
        published_by=uuid.uuid4(),
        published_at=NOW,
    )
    db_session.add(version)
    await db_session.flush()
    run = WorkflowRun(
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        status=status,
        inputs={},
        policy={},
        usage=dict(usage or {}),
        heartbeat_at=heartbeat_at,
        started_at=NOW - timedelta(minutes=10),
    )
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)
    return run


async def make_approval(
    db_session,
    run: WorkflowRun,
    *,
    node_id: str = "review",
    kind: str = "review",
    payload: dict | None = None,
) -> WorkflowApproval:
    approval = await approvals.create_approval(
        db_session,
        run=run,
        node_id=node_id,
        kind=kind,
        title="标题",
        payload=payload or {"summary": "内容"},
    )
    await db_session.commit()
    await db_session.refresh(approval)
    return approval


async def add_waiting_node(db_session, run: WorkflowRun, node_id: str):
    node = WorkflowNodeRun(
        run_id=run.id, node_id=node_id, kind="review", status="waiting"
    )
    db_session.add(node)
    await db_session.commit()
    return node


async def add_child_pending(db_session, run: WorkflowRun) -> AgentRun:
    child = AgentRun(
        tenant_id=run.tenant_id,
        user_id=uuid.uuid4(),
        goal="节点任务",
        mode="node",
        status="waiting_approval",
        workflow_run_id=run.id,
        workflow_node_id="agent",
        budget={},
        policy={},
    )
    db_session.add(child)
    await db_session.flush()
    step = AgentStep(
        tenant_id=run.tenant_id, run_id=child.id, step_index=0, kind="tool"
    )
    db_session.add(step)
    await db_session.flush()
    db_session.add(
        AgentApproval(
            tenant_id=run.tenant_id,
            run_id=child.id,
            step_id=step.id,
            tool_name="content.create_version",
            risk="high",
            payload={},
        )
    )
    await db_session.commit()
    return child


async def decided_status(db_session, approval_id) -> str:
    row = await db_session.get(WorkflowApproval, approval_id)
    await db_session.refresh(row)
    return row.status


async def test_create_approval_is_pending_with_ttl(db_session):
    run = await make_run(db_session)
    approval = await make_approval(db_session, run)
    assert approval.status == "pending"
    assert approval.tenant_id == run.tenant_id
    assert approval.expires_at is not None
    assert approval.expires_at > NOW


async def test_node_approval_prefers_pending_then_latest_decision(db_session):
    run = await make_run(db_session)
    pending = await make_approval(db_session, run, payload={"round": 1})
    resolved = await make_approval(db_session, run, payload={"round": 2})
    resolved.status = "approved"
    resolved.decided_at = NOW + timedelta(minutes=5)
    await db_session.commit()

    found = await approvals.node_approval(
        db_session, tenant_id=run.tenant_id, run_id=run.id, node_id="review"
    )
    assert found.id == pending.id

    pending.status = "expired"
    pending.decided_at = NOW
    await db_session.commit()
    found = await approvals.node_approval(
        db_session, tenant_id=run.tenant_id, run_id=run.id, node_id="review"
    )
    assert found.id == resolved.id


async def test_has_pending_covers_both_channels(db_session):
    run = await make_run(db_session)
    args = {"tenant_id": run.tenant_id, "run_id": run.id}
    assert await approvals.has_pending(db_session, **args) is False

    approval = await make_approval(db_session, run)
    assert await approvals.has_pending(db_session, **args) is True
    await approvals.decide_approval(
        db_session,
        run=run,
        approval=approval,
        decision="approve",
        actor_id=uuid.uuid4(),
        now=NOW,
    )
    assert await approvals.has_pending(db_session, **args) is False

    await add_child_pending(db_session, run)
    assert await approvals.has_pending(db_session, **args) is True


async def test_approve_last_one_requeues_and_records_pause(db_session):
    run = await make_run(
        db_session,
        heartbeat_at=NOW - timedelta(minutes=3),
        usage={"paused_ms": 0},
    )
    approval = await make_approval(db_session, run)
    status = await approvals.decide_approval(
        db_session,
        run=run,
        approval=approval,
        decision="approve",
        actor_id=uuid.uuid4(),
        now=NOW,
    )
    assert status == "queued"
    await db_session.refresh(run)
    assert run.status == "queued"
    assert run.usage["paused_ms"] == 180_000
    assert await decided_status(db_session, approval.id) == "approved"


async def test_approve_with_sibling_pending_keeps_waiting(db_session):
    run = await make_run(
        db_session,
        heartbeat_at=NOW - timedelta(minutes=3),
        usage={"paused_ms": 0},
    )
    first = await make_approval(db_session, run, node_id="a")
    await make_approval(db_session, run, node_id="b")

    status = await approvals.decide_approval(
        db_session,
        run=run,
        approval=first,
        decision="approve",
        actor_id=uuid.uuid4(),
        now=NOW,
    )
    await db_session.refresh(run)
    assert status == "waiting_approval"
    assert run.status == "waiting_approval"
    assert run.usage["paused_ms"] == 0


@pytest.mark.parametrize(
    ("kind", "error_code"),
    [("review", "review_rejected"), ("tool_confirm", "approval_rejected")],
)
async def test_reject_fails_run_and_denies_node(db_session, kind, error_code):
    run = await make_run(db_session, heartbeat_at=NOW)
    approval = await make_approval(db_session, run, node_id="gate", kind=kind)
    node = await add_waiting_node(db_session, run, "gate")
    sibling = await make_approval(db_session, run, node_id="other")
    child = await add_child_pending(db_session, run)

    status = await approvals.decide_approval(
        db_session,
        run=run,
        approval=approval,
        decision="reject",
        actor_id=uuid.uuid4(),
        note="不合适",
        now=NOW,
    )
    assert status == "failed"
    await db_session.refresh(run)
    await db_session.refresh(node)
    await db_session.refresh(child)
    assert run.status == "failed"
    assert run.error_code == error_code
    assert node.status == "denied"
    assert await decided_status(db_session, approval.id) == "rejected"
    assert await decided_status(db_session, sibling.id) == "expired"
    # 驳回后子运行与它的待处理审批不再可决策。
    assert child.status == "cancelled"
    child_approvals = (
        (
            await db_session.execute(
                select(AgentApproval).where(AgentApproval.run_id == child.id)
            )
        )
        .scalars()
        .all()
    )
    assert [row.status for row in child_approvals] == ["expired"]


async def test_decide_twice_conflicts(db_session):
    run = await make_run(db_session)
    approval = await make_approval(db_session, run)
    await approvals.decide_approval(
        db_session,
        run=run,
        approval=approval,
        decision="approve",
        actor_id=uuid.uuid4(),
        now=NOW,
    )
    await db_session.refresh(run)
    with pytest.raises(AppError) as excinfo:
        await approvals.decide_approval(
            db_session,
            run=run,
            approval=approval,
            decision="approve",
            actor_id=uuid.uuid4(),
            now=NOW,
        )
    assert excinfo.value.code == "workflow_approval_not_pending"


async def test_expired_approval_conflicts_and_is_marked(db_session):
    run = await make_run(db_session)
    approval = await make_approval(db_session, run)
    approval.expires_at = NOW - timedelta(minutes=1)
    await db_session.commit()

    with pytest.raises(AppError) as excinfo:
        await approvals.decide_approval(
            db_session,
            run=run,
            approval=approval,
            decision="approve",
            actor_id=uuid.uuid4(),
            now=NOW,
        )
    assert excinfo.value.code == "workflow_approval_not_pending"
    assert await decided_status(db_session, approval.id) == "expired"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_workflow_approvals.py -q`
Expected: FAIL，`ImportError: cannot import name 'approvals' from 'app.workflow'`。

- [ ] **Step 3: 实现 approvals.py**

Create `backend/app/workflow/approvals.py`:

```python
"""工作流级审批：创建、查询、决策与过期。

Agent 子运行内部的审批仍在 ``agent_approvals``；本模块在判断「还有没有
待处理审批」时把两条通道合并，保证并行分支不会提前唤醒运行。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models.agent import AgentApproval, AgentRun
from app.models.workflow import WorkflowApproval, WorkflowNodeRun, WorkflowRun

APPROVAL_KINDS = frozenset({"tool_confirm", "review"})
DECISIONS = frozenset({"approve", "reject"})


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def create_approval(
    session: AsyncSession,
    *,
    run: WorkflowRun,
    node_id: str,
    kind: str,
    title: str,
    payload: dict,
    tool_name: str = "",
    risk: str = "",
    ttl_hours: int = 24,
    now: datetime | None = None,
) -> WorkflowApproval:
    if kind not in APPROVAL_KINDS:
        raise ValueError(f"未知审批类型: {kind}")
    moment = now or _now()
    approval = WorkflowApproval(
        tenant_id=run.tenant_id,
        run_id=run.id,
        node_id=node_id,
        kind=kind,
        tool_name=tool_name[:100],
        risk=risk[:16],
        payload=payload,
        title=title[:200],
        status="pending",
        expires_at=moment + timedelta(hours=max(1, ttl_hours)),
    )
    session.add(approval)
    return approval


async def node_approval(
    session: AsyncSession,
    *,
    tenant_id: str,
    run_id: uuid.UUID,
    node_id: str,
) -> WorkflowApproval | None:
    rows = (
        (
            await session.execute(
                select(WorkflowApproval).where(
                    WorkflowApproval.tenant_id == tenant_id,
                    WorkflowApproval.run_id == run_id,
                    WorkflowApproval.node_id == node_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    pending = [row for row in rows if row.status == "pending"]
    pool = pending or rows
    floor = datetime.min.replace(tzinfo=UTC)

    def order(row: WorkflowApproval) -> tuple[datetime, datetime, str]:
        moment = (
            _aware(row.decided_at)
            or _aware(row.expires_at)
            or _aware(row.created_at)
        )
        created = _aware(row.created_at)
        return (moment or floor, created or floor, str(row.id))

    return max(pool, key=order)


async def has_pending(
    session: AsyncSession,
    *,
    tenant_id: str,
    run_id: uuid.UUID,
    exclude_id: uuid.UUID | None = None,
) -> bool:
    workflow_stmt = select(WorkflowApproval.id).where(
        WorkflowApproval.tenant_id == tenant_id,
        WorkflowApproval.run_id == run_id,
        WorkflowApproval.status == "pending",
    )
    if exclude_id is not None:
        workflow_stmt = workflow_stmt.where(WorkflowApproval.id != exclude_id)
    if (await session.execute(workflow_stmt.limit(1))).first() is not None:
        return True
    agent_stmt = (
        select(AgentApproval.id)
        .join(AgentRun, AgentRun.id == AgentApproval.run_id)
        .where(
            AgentRun.tenant_id == tenant_id,
            AgentRun.workflow_run_id == run_id,
            AgentApproval.status == "pending",
        )
        .limit(1)
    )
    return (await session.execute(agent_stmt)).first() is not None


async def decide_approval(
    session: AsyncSession,
    *,
    run: WorkflowRun,
    approval: WorkflowApproval,
    decision: str,
    actor_id: uuid.UUID,
    note: str = "",
    now: datetime | None = None,
) -> str:
    """做出一次决策，返回运行的新状态（queued/waiting_approval/failed）。

    角色与「不能审批自己发起」的校验在 API 层；这里只保证状态机与并发。
    """
    if decision not in DECISIONS:
        raise AppError("workflow_invalid", "审批决定不合法", 422)
    if approval.run_id != run.id or approval.tenant_id != run.tenant_id:
        raise AppError(
            "workflow_approval_not_pending", "审批与运行不匹配", 409
        )
    if approval.status != "pending":
        raise AppError(
            "workflow_approval_not_pending", "该审批已处理或已过期", 409
        )
    moment = now or _now()
    expires = _aware(approval.expires_at)
    if expires is not None and expires <= moment:
        await session.execute(
            update(WorkflowApproval)
            .where(
                WorkflowApproval.id == approval.id,
                WorkflowApproval.status == "pending",
            )
            .values(status="expired", decided_at=moment)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        raise AppError(
            "workflow_approval_not_pending", "该审批已过期", 409
        )

    target = "approved" if decision == "approve" else "rejected"
    marked = await session.execute(
        update(WorkflowApproval)
        .where(
            WorkflowApproval.id == approval.id,
            WorkflowApproval.status == "pending",
        )
        .values(
            status=target,
            decided_by=actor_id,
            decided_at=moment,
            note=note[:2000],
        )
        .execution_options(synchronize_session=False)
    )
    if marked.rowcount == 0:
        await session.rollback()
        raise AppError(
            "workflow_approval_not_pending", "该审批已处理或已过期", 409
        )

    if decision == "approve":
        others = await has_pending(
            session,
            tenant_id=run.tenant_id,
            run_id=run.id,
            exclude_id=approval.id,
        )
        if others:
            await session.commit()
            return "waiting_approval"
        usage = dict(run.usage or {})
        paused_at = _aware(run.heartbeat_at) or moment
        waited_ms = int(
            max(0.0, (moment - min(paused_at, moment)).total_seconds()) * 1000
        )
        usage["paused_ms"] = int(usage.get("paused_ms") or 0) + waited_ms
        queued = await session.execute(
            update(WorkflowRun)
            .where(
                WorkflowRun.id == run.id,
                WorkflowRun.tenant_id == run.tenant_id,
                WorkflowRun.status == "waiting_approval",
            )
            .values(
                status="queued",
                usage=usage,
                heartbeat_at=moment,
                error_code="",
                error_message="",
                finished_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        if queued.rowcount == 0:
            await session.rollback()
            raise AppError(
                "workflow_approval_not_pending", "运行状态已变化，无法继续", 409
            )
        await session.commit()
        await session.refresh(run)
        await session.refresh(approval)
        return "queued"

    error_code = (
        "review_rejected" if approval.kind == "review" else "approval_rejected"
    )
    message = "审核未通过" if approval.kind == "review" else "审批被驳回"
    await session.execute(
        update(WorkflowNodeRun)
        .where(
            WorkflowNodeRun.run_id == run.id,
            WorkflowNodeRun.node_id == approval.node_id,
            WorkflowNodeRun.status == "waiting",
        )
        .values(status="denied", error=message, finished_at=moment)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(WorkflowApproval)
        .where(
            WorkflowApproval.run_id == run.id,
            WorkflowApproval.status == "pending",
            WorkflowApproval.id != approval.id,
        )
        .values(status="expired", decided_at=moment)
        .execution_options(synchronize_session=False)
    )
    # 驳回即终结：子运行、它们的待处理审批与在途步骤一并关闭，避免留下可
    # 审批却永远不会继续的分支。
    child_ids = select(AgentRun.id).where(
        AgentRun.tenant_id == run.tenant_id,
        AgentRun.workflow_run_id == run.id,
    )
    await session.execute(
        update(AgentApproval)
        .where(
            AgentApproval.run_id.in_(child_ids),
            AgentApproval.status == "pending",
        )
        .values(status="expired", decided_at=moment, note="工作流运行已驳回")
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(AgentStep)
        .where(
            AgentStep.run_id.in_(child_ids),
            AgentStep.status == "running",
        )
        .values(
            status="interrupted",
            error="工作流运行已驳回",
            finished_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(AgentRun)
        .where(
            AgentRun.tenant_id == run.tenant_id,
            AgentRun.workflow_run_id == run.id,
            AgentRun.status.in_(("queued", "running", "waiting_approval")),
        )
        .values(status="cancelled", finished_at=moment)
        .execution_options(synchronize_session=False)
    )
    failed = await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run.id,
            WorkflowRun.tenant_id == run.tenant_id,
            WorkflowRun.status == "waiting_approval",
        )
        .values(
            status="failed",
            error_code=error_code,
            error_message=message,
            finished_at=moment,
            heartbeat_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    if failed.rowcount == 0:
        await session.rollback()
        raise AppError(
            "workflow_approval_not_pending", "运行状态已变化，无法继续", 409
        )
    await session.commit()
    await session.refresh(run)
    await session.refresh(approval)
    return "failed"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_workflow_approvals.py -q`
Expected: PASS（9 passed）。

Run: `cd backend && uv run ruff check app/workflow/approvals.py`
Expected: `All checks passed!`

- [ ] **Step 5: 提交**

```bash
git add backend/app/workflow/approvals.py backend/tests/test_workflow_approvals.py
git commit -m "feat(workflow): add approval channel helpers"
```

---

## Task 6: 引擎核心（工具/条件/审核/输出节点与推进循环）

**Files:**
- Create: `backend/app/workflow/engine.py`
- Test: `backend/tests/test_workflow_engine.py`

**背景与接口：** 这是整层的心脏。`run_workflow(factory, run_id, deps=EngineDeps(...))` 推进一次运行直到终态、遇审批、时间片用尽或没有可执行节点；每一步都从数据库重读状态，因此崩溃恢复与审批续跑走同一条路径。读取状态用一个会话，执行单个节点用独立会话：PostgreSQL 下并行才有意义，SQLite 测试夹具自动串行。Agent 节点留到 Task 7，本任务遇到 `agent` 节点会以 `workflow_internal_error` 明确失败（不会有已发布图只有 agent 节点的情况漏到这里，Task 7 紧接其后）。

**关键约定（实现时照抄）：**
- 只处理 `queued`/`running`/`waiting_approval`；终态直接原样返回。
- 输入节点在首次推进时自动写一条 `ok`（attempt 1），输出 `{"inputs": run.inputs}`。
- 节点落定集合沿用 `graph.SETTLED`（`ok/skipped/denied/error`）；`waiting` 由审批恢复路径处理。
- 工具节点执行前解析变量 → `jsonschema` 校验 → `resolve_policy`。`deny` 直接失败；`confirm` 建审批并挂起；`auto` 执行。
- `tool_step_index` 用 `UPDATE workflow_runs SET step_counter = step_counter + 1` 后读回（同事务内读到的就是自己的值），一次分配、跨重试与审批恢复复用。
- `raw_output` 只做「敏感键丢弃 + 20 KB 上限」，**不做字符串截断**；`output` 走 `scrub` + `fit_bytes`（会截断到 2000 字），供 API 使用。
- 重试只对超时、非 `AppError` 异常与 `RETRYABLE_CODES` 生效，退避 2/4/8 秒。`deps.sleep` 可注入，测试用 no-op 避免真实等待。
- 墙钟上限按 `elapsed - paused_ms` 计算；审批等待由 Task 5 的决策与本节领取逻辑共同记账。
- 时间片：`workflow_time_slice_seconds <= 0` 关闭；关闭时 `run_workflow` 一直推进到收敛。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_workflow_engine.py`:

```python
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.contracts import ToolResult, ToolSpec
from app.agent.registry import ToolRegistry
from app.config import get_settings
from app.errors import AppError
from app.models.workflow import (
    Workflow,
    WorkflowApproval,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowVersion,
)
from app.skills.registry import SkillRegistry
from app.storage import get_storage
from app.workflow import approvals
from app.workflow.engine import EngineDeps, run_workflow


async def no_sleep(_seconds: float) -> None:
    return None


@pytest_asyncio.fixture
async def engine(db_session):
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    settings = get_settings()
    stats = {"calls": []}

    async def echo(ctx, args):
        stats["calls"].append((str(ctx.run_id), ctx.step_index, args))
        return ToolResult(
            summary="回声",
            data={"text": args["text"], "long": "x" * 3000},
            artifacts=[{"type": "content", "id": "c1"}],
        )

    async def flaky(ctx, args):
        stats["calls"].append((str(ctx.run_id), ctx.step_index, args))
        if len(stats["calls"]) == 1:
            raise AppError("conflict", "临时冲突", 409)
        return ToolResult(summary="第二次成功", data={"ok": True})

    async def bad(ctx, args):
        stats["calls"].append((str(ctx.run_id), ctx.step_index, args))
        raise AppError("invalid_arguments", "参数不合法", 422)

    registry = ToolRegistry()
    registry.register_all(
        [
            ToolSpec(
                name="demo.echo",
                description="回声",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                risk="read",
                handler=echo,
                timeout_seconds=30,
            ),
            ToolSpec(
                name="demo.flaky",
                description="第一次失败",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                risk="read",
                handler=flaky,
                timeout_seconds=30,
            ),
            ToolSpec(
                name="demo.bad",
                description="总是参数错误",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                risk="read",
                handler=bad,
                timeout_seconds=30,
            ),
        ]
    )

    class Env:
        pass

    env = Env()
    env.factory = factory
    env.stats = stats
    env.deps = EngineDeps(
        gateway=None,
        tool_registry=registry,
        skill_registry=SkillRegistry(settings.skills_dir),
        storage=get_storage(),
        queue=None,
        settings=settings,
        sleep=no_sleep,
    )
    return env


def node(node_id, kind, **data):
    return {"id": node_id, "type": kind, "data": data}


def edge(source, target, handle=None):
    value = {"id": f"{source}-{target}-{handle or ''}", "source": source, "target": target}
    if handle is not None:
        value["sourceHandle"] = handle
    return value


def linear_graph(tool="demo.echo", retry=0):
    return {
        "nodes": [
            node(
                "in",
                "input",
                fields=[{"key": "topic", "type": "string", "required": True}],
            ),
            node("echo", "tool", tool=tool, args={"text": "{{inputs.topic}}"}, retry=retry),
            node("out", "output"),
        ],
        "edges": [edge("in", "echo"), edge("echo", "out")],
    }


def condition_graph():
    return {
        "nodes": [
            node(
                "in",
                "input",
                fields=[{"key": "topic", "type": "string", "required": True}],
            ),
            node(
                "c",
                "condition",
                left="{{inputs.topic}}",
                operator="contains",
                value="AI",
            ),
            node("a", "tool", tool="demo.echo", args={"text": "命中"}),
            node("b", "tool", tool="demo.echo", args={"text": "未命中"}),
            node("out", "output"),
        ],
        "edges": [
            edge("in", "c"),
            edge("c", "a", "true"),
            edge("c", "b", "false"),
            edge("a", "out"),
            edge("b", "out"),
        ],
    }


def join_graph():
    return {
        "nodes": [
            node(
                "in",
                "input",
                fields=[{"key": "topic", "type": "string", "required": True}],
            ),
            node("a", "tool", tool="demo.echo", args={"text": "A"}),
            node("b", "tool", tool="demo.echo", args={"text": "B"}),
            node("out", "output"),
        ],
        "edges": [
            edge("in", "a"),
            edge("in", "b"),
            edge("a", "out"),
            edge("b", "out"),
        ],
    }


async def make_run(db_session, graph, *, inputs=None, status="running", **overrides):
    workflow = Workflow(name="流程", created_by=uuid.uuid4())
    db_session.add(workflow)
    await db_session.flush()
    version = WorkflowVersion(
        workflow_id=workflow.id,
        version=1,
        graph=graph,
        source_revision=1,
        published_by=uuid.uuid4(),
        published_at=datetime.now(UTC),
    )
    db_session.add(version)
    await db_session.flush()
    values = {
        "workflow_id": workflow.id,
        "workflow_version_id": version.id,
        "status": status,
        "inputs": inputs if inputs is not None else {"topic": "AI 写作"},
        "policy": {},
        "usage": {},
        "started_at": datetime.now(UTC) - timedelta(seconds=1),
    }
    values.update(overrides)
    run = WorkflowRun(**values)
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)
    return run


async def node_rows(db_session, run_id):
    return (
        (
            await db_session.execute(
                select(WorkflowNodeRun)
                .where(WorkflowNodeRun.run_id == run_id)
                .order_by(WorkflowNodeRun.node_id, WorkflowNodeRun.attempt)
            )
        )
        .scalars()
        .all()
    )


async def test_linear_tool_flow_succeeds(engine, db_session):
    run = await make_run(db_session, linear_graph())
    assert await run_workflow(engine.factory, run.id, deps=engine.deps) == "succeeded"

    await db_session.refresh(run)
    assert run.status == "succeeded"
    assert len(engine.stats["calls"]) == 1
    assert engine.stats["calls"][0][1] == 1
    rows = {row.node_id: row for row in await node_rows(db_session, run.id)}
    assert set(rows) == {"in", "echo", "out"}
    assert rows["echo"].status == "ok"
    assert len(rows["echo"].raw_output["data"]["long"]) == 3000
    assert len(rows["echo"].output["data"]["long"]) == 2000
    assert run.result["items"][0]["output"]["summary"] == "回声"
    assert run.result["artifacts"] == [{"type": "content", "id": "c1"}]


async def test_condition_skips_unselected_branch(engine, db_session):
    run = await make_run(db_session, condition_graph())
    assert await run_workflow(engine.factory, run.id, deps=engine.deps) == "succeeded"
    rows = {row.node_id: row for row in await node_rows(db_session, run.id)}
    assert rows["a"].status == "ok"
    assert rows["b"].status == "skipped"
    assert rows["b"].error == "上游分支未选中"
    await db_session.refresh(run)
    assert [item["node_id"] for item in run.result["items"]] == ["a"]


async def test_join_waits_for_both_branches_and_merges_output(engine, db_session):
    run = await make_run(db_session, join_graph())
    assert await run_workflow(engine.factory, run.id, deps=engine.deps) == "succeeded"
    await db_session.refresh(run)
    assert {item["node_id"] for item in run.result["items"]} == {"a", "b"}
    assert len(engine.stats["calls"]) == 2


async def test_tool_confirm_parks_then_approval_executes_once(engine, db_session):
    run = await make_run(
        db_session, linear_graph(), policy={"tool:demo.echo": "confirm"}
    )
    assert (
        await run_workflow(engine.factory, run.id, deps=engine.deps)
        == "waiting_approval"
    )
    approval = (
        await db_session.execute(
            select(WorkflowApproval).where(WorkflowApproval.run_id == run.id)
        )
    ).scalars().one()
    assert approval.kind == "tool_confirm"
    assert engine.stats["calls"] == []

    status = await approvals.decide_approval(
        db_session,
        run=run,
        approval=approval,
        decision="approve",
        actor_id=uuid.uuid4(),
    )
    assert status == "queued"
    assert await run_workflow(engine.factory, run.id, deps=engine.deps) == "succeeded"
    assert len(engine.stats["calls"]) == 1
    call_run_id, step_index, _ = engine.stats["calls"][0]
    assert call_run_id == str(run.id)
    assert step_index == 1
    await db_session.refresh(run)
    assert run.step_counter == 1


async def test_tool_confirm_reject_never_executes(engine, db_session):
    run = await make_run(
        db_session, linear_graph(), policy={"tool:demo.echo": "confirm"}
    )
    assert (
        await run_workflow(engine.factory, run.id, deps=engine.deps)
        == "waiting_approval"
    )
    approval = (
        await db_session.execute(
            select(WorkflowApproval).where(WorkflowApproval.run_id == run.id)
        )
    ).scalars().one()
    await approvals.decide_approval(
        db_session,
        run=run,
        approval=approval,
        decision="reject",
        actor_id=uuid.uuid4(),
    )
    assert await run_workflow(engine.factory, run.id, deps=engine.deps) == "failed"
    await db_session.refresh(run)
    assert run.error_code == "approval_rejected"
    assert engine.stats["calls"] == []


async def test_policy_deny_fails_without_executing(engine, db_session):
    run = await make_run(
        db_session, linear_graph(), policy={"tool:demo.echo": "deny"}
    )
    assert await run_workflow(engine.factory, run.id, deps=engine.deps) == "failed"
    await db_session.refresh(run)
    assert run.error_code == "policy_denied"
    rows = {row.node_id: row for row in await node_rows(db_session, run.id)}
    assert rows["echo"].status == "denied"
    assert engine.stats["calls"] == []


async def test_conflict_retries_with_new_attempt_and_same_step_index(engine, db_session):
    run = await make_run(db_session, linear_graph(tool="demo.flaky", retry=1))
    assert await run_workflow(engine.factory, run.id, deps=engine.deps) == "succeeded"
    assert len(engine.stats["calls"]) == 2
    assert engine.stats["calls"][0][1] == engine.stats["calls"][1][1] == 1
    attempts = [
        row
        for row in await node_rows(db_session, run.id)
        if row.node_id == "echo"
    ]
    assert [row.attempt for row in attempts] == [1, 2]
    assert [row.status for row in attempts] == ["error", "ok"]


async def test_invalid_arguments_are_not_retried(engine, db_session):
    run = await make_run(db_session, linear_graph(tool="demo.bad", retry=3))
    assert await run_workflow(engine.factory, run.id, deps=engine.deps) == "failed"
    assert len(engine.stats["calls"]) == 1
    await db_session.refresh(run)
    assert run.error_code == "node_failed"
    attempts = [
        row
        for row in await node_rows(db_session, run.id)
        if row.node_id == "echo"
    ]
    assert len(attempts) == 1


async def test_wall_clock_limit_fails_before_execution(engine, db_session):
    deps = replace(
        engine.deps,
        settings=engine.deps.settings.model_copy(
            update={"workflow_max_seconds": 1}
        ),
    )
    run = await make_run(
        db_session,
        linear_graph(),
        started_at=datetime.now(UTC) - timedelta(seconds=30),
    )
    assert await run_workflow(engine.factory, run.id, deps=deps) == "failed"
    await db_session.refresh(run)
    assert run.error_code == "workflow_timeout"
    assert engine.stats["calls"] == []


async def test_time_slice_requeues_when_next_node_cannot_fit(engine, db_session):
    deps = replace(
        engine.deps,
        settings=engine.deps.settings.model_copy(
            update={"workflow_time_slice_seconds": 1}
        ),
    )
    run = await make_run(db_session, linear_graph())
    assert await run_workflow(engine.factory, run.id, deps=deps) == "queued"
    await db_session.refresh(run)
    assert run.status == "queued"
    rows = await node_rows(db_session, run.id)
    assert [row.node_id for row in rows] == ["in"]
    assert engine.stats["calls"] == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_workflow_engine.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.workflow.engine'`。

- [ ] **Step 3: 实现 engine.py**

Create `backend/app/workflow/engine.py`:

```python
"""工作流执行引擎：就绪/跳过传播、节点执行、重试与时间片。

引擎不领取运行（worker 用 CAS 领取后调用），也不做取消/续跑迁移；
这些在 lifecycle.py。每轮推进读状态用一个会话，单节点执行用独立会话，
PostgreSQL 下才能真并行；SQLite 测试夹具自动串行。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import jsonschema
from arq.connections import ArqRedis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.contracts import ToolContext
from app.agent.policy import resolve_policy
from app.agent.registry import ToolRegistry
from app.agent.sanitize import MAX_DATA_BYTES, fit_bytes, scrub
from app.config import Settings
from app.errors import AppError
from app.llm.gateway import LLMGateway
from app.models.agent import AgentRun
from app.models.workflow import (
    Workflow,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowVersion,
)
from app.services.audit import is_sensitive_key, record_best_effort
from app.skills.registry import SkillRegistry
from app.storage import StorageBackend
from app.workflow import approvals
from app.workflow.graph import (
    SETTLED,
    Graph,
    GraphError,
    GraphNode,
    VariableError,
    condition_choice,
    edge_active,
    parse_graph,
    ready_nodes,
    resolve_value,
)

logger = logging.getLogger(__name__)

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
ACTIVE = frozenset({"queued", "running"})
RETRYABLE_CODES = frozenset(
    {"conflict", "rate_limited", "queue_unavailable", "service_unavailable"}
)
MAX_RETRY = 3
SKIPPED_MESSAGE = "上游分支未选中"


@dataclass(frozen=True)
class EngineDeps:
    gateway: LLMGateway | None
    tool_registry: ToolRegistry
    skill_registry: SkillRegistry
    storage: StorageBackend
    queue: ArqRedis | None
    settings: Settings
    # 测试注入 no-op，生产保持真实退避
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


@dataclass
class NodeState:
    node_id: str
    attempt: int
    kind: str
    status: str
    tool_step_index: int | None
    output: dict
    raw_output: dict
    error: str


@dataclass
class RunState:
    id: uuid.UUID
    tenant_id: str
    status: str
    inputs: dict
    policy: dict
    usage: dict
    triggered_by: uuid.UUID | None
    creator_id: uuid.UUID
    graph: Graph
    graph_raw: dict
    node_states: dict[str, NodeState] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _duration(started_at: datetime | None, finished_at: datetime | None) -> int:
    start = _aware(started_at)
    end = _aware(finished_at)
    if start is None or end is None:
        return 0
    return max(0, int((end - start).total_seconds() * 1000))


def _wall_seconds(state: RunState, started_at: datetime | None) -> float:
    start = _aware(started_at)
    if start is None:
        return 0.0
    paused = float((state.usage or {}).get("paused_ms") or 0) / 1000
    return max(0.0, (_now() - start).total_seconds() - paused)


def _strip_secrets(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "[截断]"
    if isinstance(value, dict):
        return {
            key: _strip_secrets(item, depth + 1)
            for key, item in value.items()
            if isinstance(key, str) and not is_sensitive_key(key)
        }
    if isinstance(value, list):
        return [_strip_secrets(item, depth + 1) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)[:2000]


def _raw_payload(payload: dict) -> dict:
    cleaned = _strip_secrets(payload)
    capped, _ = fit_bytes(
        cleaned if isinstance(cleaned, dict) else {"data": cleaned}
    )
    return capped if isinstance(capped, dict) else {"data": capped}


def _safe_dict(value: Any) -> dict:
    cleaned = scrub(value)
    return cleaned if isinstance(cleaned, dict) else {"data": cleaned}


def _error_message(exc: BaseException) -> str:
    if isinstance(exc, AppError):
        return exc.message[:500]
    if isinstance(exc, TimeoutError):
        return "节点执行超时"
    return f"节点执行失败（{type(exc).__name__}）"


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, AppError):
        return exc.code in RETRYABLE_CODES
    return True


async def _load(factory, run_id: uuid.UUID) -> RunState | None:
    async with factory() as session:
        run = await session.get(WorkflowRun, run_id)
        if run is None:
            return None
        workflow = await session.get(Workflow, run.workflow_id)
        version = await session.get(WorkflowVersion, run.workflow_version_id)
        if workflow is None or version is None:
            raise AppError("workflow_invalid", "工作流定义或版本不存在", 422)
        rows = (
            (
                await session.execute(
                    select(WorkflowNodeRun)
                    .where(WorkflowNodeRun.run_id == run_id)
                    .order_by(WorkflowNodeRun.attempt)
                )
            )
            .scalars()
            .all()
        )
        raw_graph = version.graph if isinstance(version.graph, dict) else {}
        graph = parse_graph(raw_graph)
        node_states: dict[str, NodeState] = {}
        attempts: dict[str, int] = {}
        for row in rows:
            attempts[row.node_id] = max(attempts.get(row.node_id, 0), row.attempt)
            node_states[row.node_id] = NodeState(
                node_id=row.node_id,
                attempt=row.attempt,
                kind=row.kind,
                status=row.status,
                tool_step_index=row.tool_step_index,
                output=row.output or {},
                raw_output=row.raw_output or {},
                error=row.error or "",
            )
        return RunState(
            id=run.id,
            tenant_id=run.tenant_id,
            status=run.status,
            inputs=run.inputs or {},
            policy=run.policy or {},
            usage=run.usage or {},
            triggered_by=run.triggered_by,
            creator_id=workflow.created_by,
            graph=graph,
            graph_raw=raw_graph,
            node_states=node_states,
            attempts=attempts,
        )


def _raw_outputs(state: RunState) -> dict[str, dict]:
    return {
        node_id: item.raw_output
        for node_id, item in state.node_states.items()
        if item.status == "ok"
    }


async def run_workflow(
    factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    *,
    deps: EngineDeps,
) -> str:
    try:
        state = await _load(factory, run_id)
    except (AppError, GraphError) as exc:
        return await _fail(factory, run_id, "workflow_invalid", str(exc))
    if state is None:
        return "missing"
    if state.status in TERMINAL:
        return state.status

    await _seed_input(factory, state)

    bind = getattr(factory, "kw", {}).get("bind")
    serial = bind is not None and bind.dialect.name == "sqlite"
    slice_seconds = float(deps.settings.workflow_time_slice_seconds or 0)
    deadline = (
        asyncio.get_running_loop().time() + slice_seconds
        if slice_seconds > 0
        else None
    )

    while True:
        state = await _load(factory, run_id)
        if state is None:
            return "missing"
        if state.status in TERMINAL:
            return state.status

        waiting = [
            item for item in state.node_states.values() if item.status == "waiting"
        ]
        if waiting:
            outcome = await _resume_waiting(factory, state, deps=deps)
            if outcome in {"waiting", "failed", "stopped"}:
                return outcome
            continue

        if state.status not in {"queued", "running"}:
            return state.status

        if deps.settings.workflow_max_seconds > 0:
            async with factory() as session:
                run = await session.get(WorkflowRun, run_id)
                started_at = run.started_at if run is not None else None
            if _wall_seconds(state, started_at) > deps.settings.workflow_max_seconds:
                return await _fail(
                    factory, run_id, "workflow_timeout", "运行超过墙钟上限"
                )

        statuses = {
            node_id: item.status for node_id, item in state.node_states.items()
        }
        raws = _raw_outputs(state)
        choices = {
            node_id: condition_choice(node_id, statuses.get(node_id), raws)
            for node_id in state.graph.nodes
        }
        ready, skipped = ready_nodes(state.graph, statuses, choices)
        if skipped:
            await _persist_skipped(factory, state, skipped)
            continue
        if not ready:
            unsettled = [
                node_id
                for node_id in state.graph.nodes
                if statuses.get(node_id) not in SETTLED
            ]
            if unsettled:
                return await _fail(
                    factory,
                    run_id,
                    "workflow_internal_error",
                    "没有可执行节点，工作流无法收敛",
                )
            return await _finalize(factory, run_id, deps=deps)

        if deadline is not None:
            remaining = deadline - asyncio.get_running_loop().time()
            runnable = [
                node_id
                for node_id in ready
                if _estimate(state.graph.nodes[node_id], deps) <= remaining
            ]
            if not runnable:
                return await _requeue(factory, run_id, deps=deps)
            ready = runnable

        if serial:
            async with factory() as session:
                for node_id in ready:
                    outcome = await _run_node(
                        factory, state, node_id, deps=deps, session=session
                    )
                    if outcome == "stopped":
                        break
        else:
            limit = max(1, int(deps.settings.workflow_max_parallel_nodes))
            gate = asyncio.Semaphore(limit)

            async def guarded(node_id: str) -> None:
                async with gate:
                    await _run_node(factory, state, node_id, deps=deps)

            await asyncio.gather(*(guarded(node_id) for node_id in ready))


def _estimate(node: GraphNode, deps: EngineDeps) -> float:
    if node.kind == "tool":
        spec = deps.tool_registry.get(str(node.data.get("tool") or ""))
        return float(spec.timeout_seconds) if spec is not None else 0.0
    if node.kind == "agent":
        budget = node.data.get("budget") or {}
        try:
            return float(budget.get("max_seconds") or 60)
        except (TypeError, ValueError):
            return 60.0
    return 0.0


async def _seed_input(factory, state: RunState) -> None:
    inputs = [
        node_id
        for node_id in state.graph.nodes
        if state.graph.nodes[node_id].kind == "input"
        and not state.graph.incoming[node_id]
    ]
    if not inputs or any(node_id in state.node_states for node_id in inputs):
        return
    moment = _now()
    async with factory() as session:
        for node_id in inputs:
            session.add(
                WorkflowNodeRun(
                    tenant_id=state.tenant_id,
                    run_id=state.id,
                    node_id=node_id,
                    attempt=1,
                    kind="input",
                    status="ok",
                    output=_safe_dict({"inputs": state.inputs}),
                    raw_output=_raw_payload({"inputs": state.inputs}),
                    started_at=moment,
                    finished_at=moment,
                )
            )
        await session.commit()


async def _persist_skipped(factory, state: RunState, node_ids: list[str]) -> None:
    moment = _now()
    async with factory() as session:
        for node_id in node_ids:
            session.add(
                WorkflowNodeRun(
                    tenant_id=state.tenant_id,
                    run_id=state.id,
                    node_id=node_id,
                    attempt=state.attempts.get(node_id, 0) + 1,
                    kind=state.graph.nodes[node_id].kind,
                    status="skipped",
                    error=SKIPPED_MESSAGE,
                    started_at=moment,
                    finished_at=moment,
                )
            )
        await session.commit()


async def _latest_node_run(
    session: AsyncSession, run_id: uuid.UUID, node_id: str
) -> WorkflowNodeRun | None:
    return (
        (
            await session.execute(
                select(WorkflowNodeRun)
                .where(
                    WorkflowNodeRun.run_id == run_id,
                    WorkflowNodeRun.node_id == node_id,
                )
                .order_by(WorkflowNodeRun.attempt.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def _allocate_step_index(session: AsyncSession, run_id: uuid.UUID) -> int:
    await session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == run_id)
        .values(step_counter=WorkflowRun.step_counter + 1)
        .execution_options(synchronize_session=False)
    )
    return int(
        (
            await session.execute(
                select(WorkflowRun.step_counter).where(WorkflowRun.id == run_id)
            )
        ).scalar_one()
    )


async def _run_node(
    factory,
    state: RunState,
    node_id: str,
    *,
    deps: EngineDeps,
    session: AsyncSession | None = None,
    resume_attempt: int | None = None,
):
    if session is None:
        async with factory() as own:
            return await _run_node(
                factory,
                state,
                node_id,
                deps=deps,
                session=own,
                resume_attempt=resume_attempt,
            )
    session.expire_all()
    run = await session.get(WorkflowRun, state.id)
    if run is None or run.status not in ACTIVE:
        return "stopped"
    node = state.graph.nodes[node_id]
    if node is None:
        return "stopped"

    if resume_attempt is not None:
        node_run = await _latest_node_run(session, run.id, node_id)
        if node_run is None or node_run.status != "waiting":
            return "stale"
        node_run.status = "running"
        node_run.started_at = _now()
        await session.commit()
        return await _execute_node(
            session, run, node, node_run, state, deps, resume=True
        )

    retry = max(0, min(MAX_RETRY, int(node.data.get("retry") or 0)))
    previous = state.node_states.get(node_id)
    step_index = previous.tool_step_index if previous is not None else None
    attempt = state.attempts.get(node_id, 0)

    for index in range(retry + 1):
        attempt += 1
        node_run = WorkflowNodeRun(
            tenant_id=run.tenant_id,
            run_id=run.id,
            node_id=node_id,
            attempt=attempt,
            kind=node.kind,
            status="running",
            tool_step_index=step_index,
            started_at=_now(),
        )
        session.add(node_run)
        await session.commit()
        if node.kind == "tool" and step_index is None:
            step_index = await _allocate_step_index(session, run.id)
            node_run.tool_step_index = step_index
            await session.commit()
        try:
            return await _execute_node(
                session, run, node, node_run, state, deps
            )
        except Exception as exc:  # 节点级失败：记录、按需重试、最终落运行失败
            logger.exception(
                "workflow node failed run=%s node=%s attempt=%s",
                run.id,
                node_id,
                attempt,
            )
            await session.rollback()
            message = _error_message(exc)
            node_run.status = "error"
            node_run.error = message
            node_run.finished_at = _now()
            node_run.duration_ms = _duration(node_run.started_at, node_run.finished_at)
            await session.commit()
            if _retryable(exc) and index < retry:
                await deps.sleep(2 ** (index + 1))
                session.expire_all()
                run = await session.get(WorkflowRun, state.id)
                if run is None or run.status not in ACTIVE:
                    return "stopped"
                continue
            await _fail_run(session, run, "node_failed", message)
            await session.commit()
            return "error"
    return "error"


async def _execute_node(
    session: AsyncSession,
    run: WorkflowRun,
    node: GraphNode,
    node_run: WorkflowNodeRun,
    state: RunState,
    deps: EngineDeps,
    *,
    resume: bool = False,
) -> str:
    if node.kind == "tool":
        return await _execute_tool(
            session, run, node, node_run, state, deps, resume=resume
        )
    if node.kind == "condition":
        return await _execute_condition(session, node, node_run, state)
    if node.kind == "review":
        return await _execute_review(session, run, node, node_run, state, deps)
    if node.kind == "output":
        return await _execute_output(session, node, node_run, state)
    raise AppError(
        "workflow_internal_error", f"未知节点类型: {node.kind}", 500
    )


def _resolve_args(node: GraphNode, state: RunState) -> dict:
    args = node.data.get("args") or {}
    try:
        resolved = resolve_value(
            args, inputs=state.inputs, outputs=_raw_outputs(state)
        )
    except VariableError as exc:
        raise AppError("invalid_arguments", f"参数解析失败：{exc}", 422) from None
    if not isinstance(resolved, dict):
        raise AppError("invalid_arguments", "工具参数必须是对象", 422)
    return resolved


async def _execute_tool(
    session: AsyncSession,
    run: WorkflowRun,
    node: GraphNode,
    node_run: WorkflowNodeRun,
    state: RunState,
    deps: EngineDeps,
    *,
    resume: bool = False,
) -> str:
    name = str(node.data.get("tool") or "")
    spec = deps.tool_registry.get(name)
    if spec is None:
        raise AppError("tool_unknown", f"未知工具: {name}", 422)
    args = _resolve_args(node, state)
    try:
        jsonschema.Draft202012Validator(spec.input_schema).validate(args)
    except jsonschema.ValidationError as exc:
        raise AppError(
            "invalid_arguments", f"工具参数不合法：{exc.message}", 422
        ) from None
    node_run.input = _safe_dict(args)
    action = "auto" if resume else resolve_policy(run.policy or {}, spec)

    if action == "deny":
        node_run.status = "denied"
        node_run.error = "策略拒绝执行该工具"
        node_run.finished_at = _now()
        node_run.duration_ms = _duration(node_run.started_at, node_run.finished_at)
        await _fail_run(session, run, "policy_denied", "策略拒绝执行该工具")
        await session.commit()
        return "denied"

    if action == "confirm":
        moment = _now()
        await approvals.create_approval(
            session,
            run=run,
            node_id=node.id,
            kind="tool_confirm",
            title=f"确认执行 {spec.name}",
            payload=_safe_dict(args),
            tool_name=spec.name,
            risk=spec.risk,
            ttl_hours=deps.settings.workflow_approval_ttl_hours,
            now=moment,
        )
        node_run.status = "waiting"
        parked = await session.execute(
            update(WorkflowRun)
            .where(
                WorkflowRun.id == run.id,
                WorkflowRun.tenant_id == run.tenant_id,
                WorkflowRun.status.in_(ACTIVE),
            )
            .values(status="waiting_approval", heartbeat_at=moment)
            .execution_options(synchronize_session=False)
        )
        if parked.rowcount == 0:
            await session.rollback()
            return "stopped"
        await session.commit()
        return "waiting"

    ctx = ToolContext(
        session=session,
        tenant_id=run.tenant_id,
        user_id=run.triggered_by or state.creator_id,
        run_id=str(run.id),
        step_index=int(node_run.tool_step_index or 0),
        gateway=deps.gateway,
        storage=deps.storage,
        registry=deps.skill_registry,
        queue=deps.queue,
        settings=deps.settings,
    )
    result = await asyncio.wait_for(
        spec.handler(ctx, args), timeout=spec.timeout_seconds
    )
    payload = {
        "summary": result.summary,
        "data": result.data,
        "artifacts": result.artifacts,
    }
    display, truncated = fit_bytes(scrub(payload))
    if not isinstance(display, dict):
        display = {"data": display}
    if truncated:
        display["truncated"] = True
    node_run.output = display
    node_run.raw_output = _raw_payload(payload)
    node_run.status = "ok"
    node_run.finished_at = _now()
    node_run.duration_ms = _duration(node_run.started_at, node_run.finished_at)
    await session.commit()
    if spec.risk in {"low", "high"}:
        await record_best_effort(
            session,
            tenant_id=run.tenant_id,
            action="workflow.node.execute",
            actor=str(run.triggered_by) if run.triggered_by else "scheduler",
            resource_type="workflow_run",
            resource_id=str(run.id),
            detail={"node_id": node.id, "tool": spec.name},
        )
    return "ok"


def _compare(left: Any, operator: str, value: Any) -> bool:
    if operator == "not_empty":
        return left not in (None, "", [], {})
    if operator == "equals":
        return left == value
    if operator == "not_equals":
        return left != value
    if operator == "contains":
        if isinstance(left, str):
            return isinstance(value, str) and value in left
        if isinstance(left, list):
            return value in left
        return False
    if operator in {"gt", "lt"}:
        numeric = (int, float)
        if (
            isinstance(left, numeric)
            and not isinstance(left, bool)
            and isinstance(value, numeric)
            and not isinstance(value, bool)
        ):
            return left > value if operator == "gt" else left < value
        return False
    raise AppError(
        "workflow_internal_error", f"未知条件运算符: {operator}", 500
    )


async def _execute_condition(
    session: AsyncSession,
    node: GraphNode,
    node_run: WorkflowNodeRun,
    state: RunState,
) -> str:
    try:
        left = resolve_value(
            node.data.get("left"), inputs=state.inputs, outputs=_raw_outputs(state)
        )
        value = (
            resolve_value(
                node.data.get("value"),
                inputs=state.inputs,
                outputs=_raw_outputs(state),
            )
            if "value" in node.data
            else None
        )
    except VariableError as exc:
        raise AppError("invalid_arguments", f"条件解析失败：{exc}", 422) from None
    result = _compare(left, str(node.data.get("operator") or ""), value)
    node_run.input = _safe_dict({"left": left, "value": value})
    node_run.output = {"result": result}
    node_run.raw_output = {"result": result}
    node_run.status = "ok"
    node_run.finished_at = _now()
    node_run.duration_ms = _duration(node_run.started_at, node_run.finished_at)
    await session.commit()
    return "ok"


def _upstream_payload(state: RunState, node_id: str) -> list[dict]:
    payload: list[dict] = []
    for item in state.graph.incoming[node_id]:
        source = state.node_states.get(item.source)
        if source is None or source.status != "ok":
            continue
        payload.append(
            {
                "node_id": item.source,
                "summary": str(source.output.get("summary") or ""),
                "artifacts": list(source.output.get("artifacts") or [])[:10],
            }
        )
    return payload


async def _execute_review(
    session: AsyncSession,
    run: WorkflowRun,
    node: GraphNode,
    node_run: WorkflowNodeRun,
    state: RunState,
    deps: EngineDeps,
) -> str:
    title = str(node.data.get("title") or "审核")
    payload = {
        "node_id": node.id,
        "title": title,
        "upstream": _upstream_payload(state, node.id),
    }
    moment = _now()
    node_run.input = _safe_dict({"title": title})
    await approvals.create_approval(
        session,
        run=run,
        node_id=node.id,
        kind="review",
        title=title,
        payload=payload,
        ttl_hours=deps.settings.workflow_approval_ttl_hours,
        now=moment,
    )
    node_run.status = "waiting"
    parked = await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run.id,
            WorkflowRun.tenant_id == run.tenant_id,
            WorkflowRun.status.in_(ACTIVE),
        )
        .values(status="waiting_approval", heartbeat_at=moment)
        .execution_options(synchronize_session=False)
    )
    if parked.rowcount == 0:
        await session.rollback()
        return "stopped"
    await session.commit()
    return "waiting"


async def _execute_output(
    session: AsyncSession,
    node: GraphNode,
    node_run: WorkflowNodeRun,
    state: RunState,
) -> str:
    statuses = {
        node_id: item.status for node_id, item in state.node_states.items()
    }
    raws = _raw_outputs(state)
    choices = {
        node_id: condition_choice(node_id, statuses.get(node_id), raws)
        for node_id in state.graph.nodes
    }
    items: list[dict] = []
    raw_items: list[dict] = []
    artifacts: list = []
    for item in state.graph.incoming[node.id]:
        if not edge_active(item, statuses, choices):
            continue
        source = state.node_states.get(item.source)
        if source is None or source.status != "ok":
            continue
        items.append({"node_id": item.source, "output": source.output})
        raw_items.append({"node_id": item.source, "output": source.raw_output})
        artifacts.extend(source.output.get("artifacts") or [])
    node_run.output = {"items": items[:50], "artifacts": artifacts[:50]}
    node_run.raw_output = {
        "items": raw_items[:50],
        "artifacts": artifacts[:50],
    }
    node_run.status = "ok"
    node_run.finished_at = _now()
    node_run.duration_ms = _duration(node_run.started_at, node_run.finished_at)
    await session.commit()
    return "ok"


async def _resume_waiting(factory, state: RunState, *, deps: EngineDeps) -> str:
    async with factory() as session:
        run = await session.get(WorkflowRun, state.id)
        if run is None:
            return "stopped"
        if await approvals.has_pending(
            session, tenant_id=run.tenant_id, run_id=run.id
        ):
            if run.status != "waiting_approval":
                await session.execute(
                    update(WorkflowRun)
                    .where(
                        WorkflowRun.id == run.id,
                        WorkflowRun.status.in_(ACTIVE),
                    )
                    .values(status="waiting_approval", heartbeat_at=_now())
                    .execution_options(synchronize_session=False)
                )
                await session.commit()
            return "waiting"

        moment = _now()
        if run.status == "waiting_approval":
            usage = dict(run.usage or {})
            paused_at = _aware(run.heartbeat_at) or moment
            usage["paused_ms"] = int(usage.get("paused_ms") or 0) + int(
                max(0.0, (moment - min(paused_at, moment)).total_seconds()) * 1000
            )
            claimed = await session.execute(
                update(WorkflowRun)
                .where(
                    WorkflowRun.id == run.id,
                    WorkflowRun.status == "waiting_approval",
                )
                .values(status="running", usage=usage, heartbeat_at=moment)
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount == 0:
                await session.rollback()
                return "stopped"

        active: list[str] = []
        for item in state.node_states.values():
            if item.status != "waiting":
                continue
            node = state.graph.nodes.get(item.node_id)
            if node is None:
                continue
            approval = await approvals.node_approval(
                session,
                tenant_id=run.tenant_id,
                run_id=run.id,
                node_id=item.node_id,
            )
            if approval is None:
                await _fail_run(
                    session, run, "approval_expired", "审批已失效"
                )
                await session.commit()
                return "failed"
            if approval.status == "pending":
                await session.commit()
                return "waiting"
            node_run = await _latest_node_run(session, run.id, item.node_id)
            if approval.status in {"rejected", "expired"}:
                if node_run is not None and node_run.status == "waiting":
                    node_run.status = "denied"
                    node_run.error = "审批未通过"
                    node_run.finished_at = moment
                code = (
                    "approval_expired"
                    if approval.status == "expired"
                    else "review_rejected"
                    if node.kind == "review"
                    else "approval_rejected"
                )
                await _fail_run(session, run, code, "审批未通过")
                await session.commit()
                return "failed"
            if node.kind == "review":
                if node_run is not None and node_run.status == "waiting":
                    node_run.status = "ok"
                    node_run.output = {"approved": True}
                    node_run.raw_output = {"approved": True}
                    node_run.finished_at = moment
                    node_run.duration_ms = _duration(
                        node_run.started_at, moment
                    )
            elif node.kind == "tool":
                active.append(item.node_id)
            else:
                await _fail_run(
                    session,
                    run,
                    "workflow_internal_error",
                    f"无法恢复节点类型: {node.kind}",
                )
                await session.commit()
                return "failed"
        await session.commit()

    for node_id in active:
        outcome = await _run_node(
            factory,
            state,
            node_id,
            deps=deps,
            resume_attempt=state.node_states[node_id].attempt,
        )
        if outcome in {"error", "denied"}:
            return "failed"
        if outcome == "stopped":
            return "stopped"
    return "progress"


async def _refresh_usage(session: AsyncSession, run: WorkflowRun) -> dict:
    usage = dict(run.usage or {})
    rows = (
        (
            await session.execute(
                select(WorkflowNodeRun).where(WorkflowNodeRun.run_id == run.id)
            )
        )
        .scalars()
        .all()
    )
    usage["nodes"] = sum(1 for row in rows if row.status in SETTLED)
    usage["tool_calls"] = sum(
        1 for row in rows if row.kind == "tool" and row.status == "ok"
    )
    children = (
        (
            await session.execute(
                select(AgentRun).where(AgentRun.workflow_run_id == run.id)
            )
        )
        .scalars()
        .all()
    )
    usage["prompt_tokens"] = sum(
        int((child.usage or {}).get("prompt_tokens") or 0) for child in children
    )
    usage["completion_tokens"] = sum(
        int((child.usage or {}).get("completion_tokens") or 0)
        for child in children
    )
    usage.setdefault("paused_ms", 0)
    return usage


async def _fail_run(
    session: AsyncSession,
    run: WorkflowRun,
    code: str,
    message: str,
) -> None:
    await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run.id,
            WorkflowRun.tenant_id == run.tenant_id,
            WorkflowRun.status.in_(ACTIVE),
        )
        .values(
            status="failed",
            error_code=code[:100],
            error_message=message[:500],
            finished_at=_now(),
            heartbeat_at=_now(),
        )
        .execution_options(synchronize_session=False)
    )


async def _fail(
    factory, run_id: uuid.UUID, code: str, message: str
) -> str:
    async with factory() as session:
        run = await session.get(WorkflowRun, run_id)
        if run is None:
            return "missing"
        if run.status in TERMINAL:
            return run.status
        await _fail_run(session, run, code, message)
        await session.commit()
    return "failed"


async def _finalize(factory, run_id: uuid.UUID, *, deps: EngineDeps) -> str:
    async with factory() as session:
        run = await session.get(WorkflowRun, run_id)
        if run is None:
            return "missing"
        if run.status in TERMINAL:
            return run.status
        output = (
            await session.execute(
                select(WorkflowNodeRun)
                .where(
                    WorkflowNodeRun.run_id == run.id,
                    WorkflowNodeRun.kind == "output",
                    WorkflowNodeRun.status == "ok",
                )
                .order_by(WorkflowNodeRun.attempt.desc())
                .limit(1)
            )
        ).scalars().first()
        result = dict(output.output or {}) if output is not None else {}
        usage = await _refresh_usage(session, run)
        await session.execute(
            update(WorkflowRun)
            .where(
                WorkflowRun.id == run.id,
                WorkflowRun.status.in_(ACTIVE),
            )
            .values(
                status="succeeded",
                result=result,
                usage=usage,
                error_code="",
                error_message="",
                finished_at=_now(),
                heartbeat_at=_now(),
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        await record_best_effort(
            session,
            tenant_id=run.tenant_id,
            action="workflow.run.complete",
            actor=str(run.triggered_by) if run.triggered_by else "scheduler",
            resource_type="workflow_run",
            resource_id=str(run.id),
            detail={"nodes": usage.get("nodes", 0)},
        )
    return "succeeded"


async def _requeue(factory, run_id: uuid.UUID, *, deps: EngineDeps) -> str:
    async with factory() as session:
        result = await session.execute(
            update(WorkflowRun)
            .where(
                WorkflowRun.id == run_id,
                WorkflowRun.status.in_(ACTIVE),
            )
            .values(status="queued", heartbeat_at=_now())
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 0:
            await session.rollback()
            run = await session.get(WorkflowRun, run_id)
            return run.status if run is not None else "missing"
        await session.commit()
    if deps.queue is not None:
        await deps.queue.enqueue_job("run_workflow_job", str(run_id))
    return "queued"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_workflow_engine.py -q`
Expected: PASS（10 passed）。

Run: `cd backend && uv run ruff check app/workflow/engine.py`
Expected: `All checks passed!`

- [ ] **Step 5: 提交**

```bash
git add backend/app/workflow/engine.py backend/tests/test_workflow_engine.py
git commit -m "feat(workflow): add workflow execution engine"
```

## Task 7: Agent 节点与过滤后的工具注册表

**Files:**
- Create: `backend/app/agent/quota.py`
- Modify: `backend/app/agent/registry.py`（新增 `ToolRegistry.subset`）
- Modify: `backend/app/workflow/engine.py`
- Test: `backend/tests/test_workflow_agent_node.py`

**背景与接口：** Agent 节点不在工作流里另造一套 Agent 循环，而是建一条子 `AgentRun(mode="node")` 后内联调用第 1 层的 `run_agent`。子运行继承租户、运行策略快照和发起人（定时运行取工作流创建人），工具白名单只能收窄注册表。子运行内的 `confirm` 仍走第 1 层 `agent_approvals`：父运行挂起为 `waiting_approval`，审批由第 1 层接口决策，重新入队后引擎按子运行状态继续。

**关键约定（实现时照抄）：**
- 白名单语义：空/缺省 = 注册表全集；`"demo.*"` = 前缀展开；精确名只选该工具；不存在的名字静默忽略（发布校验已拦截「匹配为零」，草稿不运行）。
- 子运行预算只保留第 1 层的四个维度，非法值丢弃；`max_seconds` 夹紧到 `workflow_time_slice_seconds - 5`（Ruling 11），其余缺省交给 `run_agent._budget` 按租户默认补齐。
- 目标文本用同一套 `{{...}}` 解析，结果截到 4000 字；解析失败是该节点 `invalid_arguments`。
- 子运行失败映射：`succeeded` → 节点 `ok`，输出 `{"result_text", "result"}`；`waiting_approval` → 节点 `waiting` + 运行 `waiting_approval`；`failed/cancelled` 且 `approval_rejected` → 节点 `denied`、运行 `failed/approval_rejected`；配额/未配置模型 → 节点 `error`、运行同码；其余失败抛 `service_unavailable` 交给节点重试（Ruling 12）。
- 子运行心跳：`run_agent(..., session_factory=deps.session_factory)`。测试不传（默认 `None`），生产 worker 传入，否则子运行超过 `agent_run_stale_seconds` 会被第 1 层对账误杀。
- `_resume_waiting` 必须认识 Agent 节点：它的审批不在 `workflow_approvals`，不能因为查不到工作流审批就落 `approval_expired`。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_workflow_agent_node.py`:

```python
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.contracts import ToolResult, ToolSpec
from app.agent.registry import ToolRegistry
from app.agent.runtime import decide_approval
from app.config import get_settings
from app.llm.tools import ToolCall, ToolTurn
from app.llm.types import Usage
from app.models.agent import AgentApproval, AgentRun
from app.models.base import DEFAULT_TENANT_ID
from app.models.user import User
from app.models.workflow import (
    Workflow,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowVersion,
)
from app.skills.registry import SkillRegistry
from app.storage import get_storage
from app.workflow.engine import EngineDeps, run_workflow


async def no_sleep(_seconds: float) -> None:
    return None


class FakeGateway:
    def __init__(self) -> None:
        self.turns: list = []
        self.tools: list[list[str]] = []

    async def complete_tool_calls(self, messages, *, tools):
        self.tools.append([item["name"] for item in tools])
        item = self.turns.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def turn(content="", calls=(), prompt=10, completion=5):
    return ToolTurn(
        content=content,
        tool_calls=[
            ToolCall(
                id=call_id,
                name=name,
                arguments=args if isinstance(args, str) else json.dumps(args),
            )
            for call_id, name, args in calls
        ],
        finish_reason="tool_calls" if calls else "stop",
        usage=Usage(prompt, completion),
    )


@pytest_asyncio.fixture
async def env(db_session):
    settings = get_settings()
    calls: list[tuple[str, dict]] = []

    async def echo(ctx, args):
        calls.append((str(ctx.run_id), args))
        return ToolResult(summary="回声", data={"text": args["text"]})

    async def publish(ctx, args):
        calls.append((str(ctx.run_id), args))
        return ToolResult(
            summary="已建草稿",
            data={"draft": True},
            artifacts=[{"type": "content", "id": "d1"}],
        )

    schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    }
    registry = ToolRegistry()
    registry.register_all(
        [
            ToolSpec(
                name="demo.echo",
                description="回声",
                input_schema=schema,
                risk="read",
                handler=echo,
                timeout_seconds=30,
            ),
            ToolSpec(
                name="demo.publish",
                description="建草稿",
                input_schema=schema,
                risk="high",
                handler=publish,
                timeout_seconds=30,
            ),
        ]
    )

    class Env:
        pass

    holder = Env()
    holder.factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    holder.settings = settings
    holder.calls = calls
    holder.gateway = FakeGateway()
    holder.deps = EngineDeps(
        gateway=holder.gateway,
        tool_registry=registry,
        skill_registry=SkillRegistry(settings.skills_dir),
        storage=get_storage(),
        queue=None,
        settings=settings,
        sleep=no_sleep,
    )
    return holder


def node(node_id, kind, **data):
    return {"id": node_id, "type": kind, "data": data}


def edge(source, target):
    return {"id": f"{source}-{target}", "source": source, "target": target}


def agent_graph(*, tools=None, budget=None, retry=0):
    data = {
        "goal": "围绕 {{inputs.topic}} 写一条笔记",
        "budget": budget or {"max_steps": 4},
        "retry": retry,
    }
    if tools is not None:
        data["tools"] = tools
    return {
        "nodes": [
            node(
                "in",
                "input",
                fields=[{"key": "topic", "type": "string", "required": True}],
            ),
            node("writer", "agent", **data),
            node("out", "output"),
        ],
        "edges": [edge("in", "writer"), edge("writer", "out")],
    }


async def make_run(db_session, *, graph, policy=None, triggered_by=None):
    owner = triggered_by or uuid.uuid4()
    workflow = Workflow(
        tenant_id=DEFAULT_TENANT_ID, name="测试工作流", created_by=owner
    )
    db_session.add(workflow)
    await db_session.flush()
    version = WorkflowVersion(
        tenant_id=DEFAULT_TENANT_ID,
        workflow_id=workflow.id,
        version=1,
        graph=graph,
        source_revision=0,
        published_by=owner,
        published_at=datetime.now(UTC),
    )
    db_session.add(version)
    await db_session.flush()
    run = WorkflowRun(
        tenant_id=DEFAULT_TENANT_ID,
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        status="queued",
        inputs={"topic": "AI 选题"},
        policy=dict(policy or {}),
        triggered_by=triggered_by,
        started_at=datetime.now(UTC),
    )
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)
    return run


async def children_of(db_session, run):
    return (
        (
            await db_session.execute(
                select(AgentRun).where(AgentRun.workflow_run_id == run.id)
            )
        )
        .scalars()
        .all()
    )


def approver() -> User:
    return User(
        id=uuid.uuid4(),
        tenant_id=DEFAULT_TENANT_ID,
        email="approver@local",
        password_hash="x",
        display_name="Approver",
        role="admin",
        is_active=True,
        must_change_password=False,
        is_platform_admin=False,
        token_version=0,
    )


async def test_registry_subset_matches_exact_and_prefix_names(env):
    assert env.deps.tool_registry.subset(None).names() == [
        "demo.echo",
        "demo.publish",
    ]
    assert env.deps.tool_registry.subset(["demo.*"]).names() == [
        "demo.echo",
        "demo.publish",
    ]
    assert env.deps.tool_registry.subset(["demo.echo"]).names() == ["demo.echo"]
    assert env.deps.tool_registry.subset(["missing.tool"]).names() == []


async def test_agent_node_succeeds_and_feeds_output(env, db_session):
    env.gateway.turns = [
        turn(content="先查一下", calls=[("c1", "demo.echo", {"text": "你好"})]),
        turn(content="已经完成"),
    ]
    run = await make_run(db_session, graph=agent_graph(tools=["demo.echo"]))
    assert await run_workflow(env.factory, run.id, deps=env.deps) == "succeeded"

    children = await children_of(db_session, run)
    assert len(children) == 1
    child = children[0]
    assert child.mode == "node"
    assert child.workflow_node_id == "writer"
    assert child.status == "succeeded"
    assert child.goal == "围绕 AI 选题 写一条笔记"
    assert env.calls and env.calls[0][0] == str(child.id)

    node_run = (
        await db_session.execute(
            select(WorkflowNodeRun).where(
                WorkflowNodeRun.run_id == run.id,
                WorkflowNodeRun.node_id == "writer",
            )
        )
    ).scalars().one()
    assert node_run.status == "ok"
    assert node_run.agent_run_id == child.id
    assert node_run.output["result_text"] == "已经完成"

    await db_session.refresh(run)
    assert run.result["items"][0]["output"]["result_text"] == "已经完成"
    assert run.usage["prompt_tokens"] == 20
    assert run.usage["completion_tokens"] == 10


async def test_agent_tool_whitelist_supports_prefix(env, db_session):
    env.gateway.turns = [turn(content="完成")]
    run = await make_run(db_session, graph=agent_graph(tools=["demo.*"]))
    assert await run_workflow(env.factory, run.id, deps=env.deps) == "succeeded"
    assert env.gateway.tools[0] == ["demo.echo", "demo.publish"]


async def test_agent_without_whitelist_uses_all_tools(env, db_session):
    env.gateway.turns = [turn(content="完成")]
    run = await make_run(db_session, graph=agent_graph())
    assert await run_workflow(env.factory, run.id, deps=env.deps) == "succeeded"
    assert env.gateway.tools[0] == ["demo.echo", "demo.publish"]


async def test_agent_budget_seconds_clamped_to_time_slice(env, db_session):
    settings = env.settings.model_copy(update={"workflow_time_slice_seconds": 60})
    deps = replace(env.deps, settings=settings)
    env.gateway.turns = [turn(content="完成")]
    run = await make_run(
        db_session,
        graph=agent_graph(budget={"max_steps": 4, "max_seconds": 600}),
    )
    assert await run_workflow(env.factory, run.id, deps=deps) == "succeeded"
    child = (await children_of(db_session, run))[0]
    assert child.budget == {"max_steps": 4, "max_seconds": 55}


async def test_agent_high_risk_tool_waits_for_approval_and_resumes(env, db_session):
    env.gateway.turns = [
        turn(content="准备建草稿", calls=[("c1", "demo.publish", {"text": "稿件"})]),
        turn(content="已经完成"),
    ]
    run = await make_run(db_session, graph=agent_graph())
    assert (
        await run_workflow(env.factory, run.id, deps=env.deps)
        == "waiting_approval"
    )
    child = (await children_of(db_session, run))[0]
    approval = (
        await db_session.execute(
            select(AgentApproval).where(AgentApproval.run_id == child.id)
        )
    ).scalars().one()
    assert approval.status == "pending"
    assert env.calls == []

    await decide_approval(
        db_session, approval, actor=approver(), decision="approve"
    )
    assert await run_workflow(env.factory, run.id, deps=env.deps) == "succeeded"
    assert len(env.calls) == 1
    await db_session.refresh(run)
    assert run.status == "succeeded"
    assert run.error_code == ""


async def test_agent_rejected_approval_fails_run(env, db_session):
    env.gateway.turns = [
        turn(content="准备建草稿", calls=[("c1", "demo.publish", {"text": "稿件"})]),
    ]
    run = await make_run(db_session, graph=agent_graph())
    assert (
        await run_workflow(env.factory, run.id, deps=env.deps)
        == "waiting_approval"
    )
    child = (await children_of(db_session, run))[0]
    approval = (
        await db_session.execute(
            select(AgentApproval).where(AgentApproval.run_id == child.id)
        )
    ).scalars().one()
    await decide_approval(
        db_session, approval, actor=approver(), decision="reject"
    )
    assert await run_workflow(env.factory, run.id, deps=env.deps) == "failed"
    await db_session.refresh(run)
    assert run.error_code == "approval_rejected"
    assert env.calls == []
    node_run = (
        await db_session.execute(
            select(WorkflowNodeRun).where(
                WorkflowNodeRun.run_id == run.id,
                WorkflowNodeRun.node_id == "writer",
            )
        )
    ).scalars().one()
    assert node_run.status == "denied"


async def test_agent_child_failure_retries_with_new_child(env, db_session):
    env.gateway.turns = [RuntimeError("boom"), turn(content="完成")]
    run = await make_run(db_session, graph=agent_graph(retry=1))
    assert await run_workflow(env.factory, run.id, deps=env.deps) == "succeeded"
    children = await children_of(db_session, run)
    assert len(children) == 2
    assert {child.status for child in children} == {"failed", "succeeded"}


async def test_agent_daily_quota_exceeded_fails_run(env, db_session):
    settings = env.settings.model_copy(update={"agent_daily_token_quota": 1})
    deps = replace(env.deps, settings=settings)
    seed = AgentRun(
        tenant_id=DEFAULT_TENANT_ID,
        user_id=uuid.uuid4(),
        goal="旧运行",
        status="succeeded",
        usage={"prompt_tokens": 5},
        created_at=datetime.now(UTC),
    )
    db_session.add(seed)
    await db_session.commit()

    env.gateway.turns = [turn(content="不应执行")]
    run = await make_run(db_session, graph=agent_graph())
    assert await run_workflow(env.factory, run.id, deps=deps) == "failed"
    await db_session.refresh(run)
    assert run.error_code == "agent_daily_quota_exceeded"
    count = (
        await db_session.execute(
            select(func.count())
            .select_from(AgentRun)
            .where(AgentRun.workflow_run_id == run.id)
        )
    ).scalar_one()
    assert count == 0
    assert env.gateway.tools == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_workflow_agent_node.py -q`
Expected: FAIL。`ToolRegistry` 没有 `subset`（`AttributeError`），Agent 节点落 `workflow_internal_error`（运行 `failed`，断言 `succeeded`/`waiting_approval` 不成立）。

- [ ] **Step 3: 实现配额模块**

Create `backend/app/agent/quota.py`:

```python
"""租户日 token 用量：API 创建工作流运行与每个 Agent 节点启动前共用。"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.errors import AppError
from app.models.agent import AgentRun


def day_start(now: datetime | None = None) -> datetime:
    moment = now or datetime.now(UTC)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


async def daily_token_usage(
    session: AsyncSession, tenant_id: str, *, now: datetime | None = None
) -> int:
    rows = (
        await session.execute(
            select(AgentRun.usage).where(
                AgentRun.tenant_id == tenant_id,
                AgentRun.created_at >= day_start(now),
            )
        )
    ).scalars().all()
    used = 0
    for usage in rows:
        if isinstance(usage, dict):
            used += int(usage.get("prompt_tokens") or 0)
            used += int(usage.get("completion_tokens") or 0)
    return used


async def ensure_daily_quota(
    session: AsyncSession,
    tenant_id: str,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> None:
    # 0 关闭日配额（与第 1 层口径一致）
    quota = int(settings.agent_daily_token_quota or 0)
    if quota <= 0:
        return
    if await daily_token_usage(session, tenant_id, now=now) >= quota:
        raise AppError("agent_daily_quota_exceeded", "今日 token 配额已用尽", 429)
```

- [ ] **Step 4: 给注册表加白名单视图**

在 `backend/app/agent/registry.py` 的 `ToolRegistry.schemas` 之后追加：

```python
    def subset(self, patterns: Iterable[str] | None) -> ToolRegistry:
        """按精确名或 ``前缀.*`` 收窄视图；空列表表示全集。"""
        selected = [str(item) for item in patterns or [] if str(item)]
        if not selected:
            return self
        allowed: set[str] = set()
        for pattern in selected:
            if pattern.endswith(".*"):
                allowed.update(
                    name
                    for name in self._specs
                    if name.startswith(pattern[:-1])
                )
            else:
                allowed.add(pattern)
        subset = ToolRegistry()
        for name in sorted(allowed):
            spec = self._specs.get(name)
            if spec is not None:
                subset.register(spec)
        return subset
```

- [ ] **Step 5: 接入引擎**

在 `backend/app/workflow/engine.py` 中按下列顺序修改。

1）导入区追加：

```python
from app.agent.quota import ensure_daily_quota
from app.agent.runtime import run_agent
```

2）常量区追加：

```python
SLICE_GUARD_SECONDS = 5
BUDGET_KEYS = frozenset(
    {"max_steps", "max_tokens", "max_tool_calls", "max_seconds"}
)
```

3）`EngineDeps` 末尾追加字段（保留原有 `sleep`）：

```python
    session_factory: async_sessionmaker[AsyncSession] | None = None
```

4）把 `_estimate` 整个函数替换为：

```python
def _estimate(node: GraphNode, deps: EngineDeps) -> float:
    if node.kind == "tool":
        spec = deps.tool_registry.get(str(node.data.get("tool") or ""))
        return float(spec.timeout_seconds) if spec is not None else 0.0
    if node.kind == "agent":
        budget = _agent_budget(node, deps.settings)
        return float(budget.get("max_seconds") or 0)
    return 0.0
```

5）在 `_execute_node` 的分派链里、`tool` 分支之后插入：

```python
    if node.kind == "agent":
        return await _execute_agent(
            session, run, node, node_run, state, deps, resume=resume
        )
```

6）把 `_resume_waiting` 中处理等待节点的循环体开头改成：

```python
            node = state.graph.nodes.get(item.node_id)
            if node is None:
                continue
            if node.kind == "agent":
                # 子运行的审批在 agent_approvals，没有 workflow_approvals 行
                active.append(item.node_id)
                continue
            approval = await approvals.node_approval(
```

7）在 `_resolve_args` 之后追加以下函数：

```python
def _agent_budget(node: GraphNode, settings: Settings) -> dict[str, int]:
    raw = node.data.get("budget")
    if not isinstance(raw, dict):
        raw = {}
    budget = {
        key: value
        for key, value in raw.items()
        if key in BUDGET_KEYS
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
    }
    cap = int(settings.workflow_time_slice_seconds or 0)
    if cap > 0:
        ceiling = max(1, cap - SLICE_GUARD_SECONDS)
        budget["max_seconds"] = min(budget.get("max_seconds", ceiling), ceiling)
    return budget


def _agent_goal(node: GraphNode, state: RunState) -> str:
    raw = node.data.get("goal") or ""
    try:
        resolved = resolve_value(
            raw, inputs=state.inputs, outputs=_raw_outputs(state)
        )
    except VariableError as exc:
        raise AppError(
            "invalid_arguments", f"Agent 目标解析失败：{exc}", 422
        ) from None
    if not isinstance(resolved, str) or not resolved.strip():
        raise AppError("invalid_arguments", "Agent 目标不能为空", 422)
    return resolved[:4000]


async def _abort_node(
    session: AsyncSession,
    run: WorkflowRun,
    node_run: WorkflowNodeRun,
    code: str,
    message: str,
) -> None:
    node_run.status = "error"
    node_run.error = message[:500]
    node_run.finished_at = _now()
    node_run.duration_ms = _duration(node_run.started_at, node_run.finished_at)
    await _fail_run(session, run, code, message)
    await session.commit()


async def _agent_succeeded(
    session: AsyncSession, node_run: WorkflowNodeRun, child: AgentRun
) -> str:
    payload = {
        "result_text": child.result_text,
        "result": child.result if isinstance(child.result, dict) else {},
    }
    node_run.output = _safe_dict(payload)
    node_run.raw_output = _raw_payload(payload)
    node_run.status = "ok"
    node_run.finished_at = _now()
    node_run.duration_ms = _duration(node_run.started_at, node_run.finished_at)
    await session.commit()
    return "ok"


async def _agent_failed(
    session: AsyncSession,
    run: WorkflowRun,
    node_run: WorkflowNodeRun,
    child: AgentRun,
) -> str:
    code = child.error_code or "node_failed"
    message = (child.error_message or "Agent 节点执行失败")[:500]
    node_run.finished_at = _now()
    node_run.duration_ms = _duration(node_run.started_at, node_run.finished_at)
    if code == "approval_rejected":
        node_run.status = "denied"
        node_run.error = message
        await _fail_run(session, run, "approval_rejected", message)
        await session.commit()
        return "error"
    if code in {"agent_daily_quota_exceeded", "llm_not_configured"}:
        node_run.status = "error"
        node_run.error = message
        await _fail_run(session, run, code, message)
        await session.commit()
        return "error"
    # 其余失败交给 _run_node 的重试循环，耗尽后落 node_failed
    raise AppError("service_unavailable", f"Agent 子运行失败：{message}")


async def _execute_agent(
    session: AsyncSession,
    run: WorkflowRun,
    node: GraphNode,
    node_run: WorkflowNodeRun,
    state: RunState,
    deps: EngineDeps,
    *,
    resume: bool = False,
) -> str:
    if deps.gateway is None:
        await _abort_node(
            session, run, node_run, "llm_not_configured", "未配置可用的大模型"
        )
        return "error"
    try:
        await ensure_daily_quota(session, run.tenant_id, deps.settings)
    except AppError as exc:
        await _abort_node(session, run, node_run, exc.code, exc.message)
        return "error"

    patterns = [str(item) for item in (node.data.get("tools") or [])]
    child: AgentRun | None = None
    if resume and node_run.agent_run_id is not None:
        child = await session.get(AgentRun, node_run.agent_run_id)
    if child is None:
        goal = _agent_goal(node, state)
        child = AgentRun(
            tenant_id=run.tenant_id,
            user_id=run.triggered_by or state.creator_id,
            goal=goal,
            mode="node",
            status="queued",
            budget=_agent_budget(node, deps.settings),
            policy=dict(run.policy or {}),
            workflow_run_id=run.id,
            workflow_node_id=node.id,
        )
        session.add(child)
        await session.commit()
        await session.refresh(child)
        node_run.agent_run_id = child.id
        node_run.input = _safe_dict({"goal": goal, "tools": patterns})
        node_run.status = "running"
        await session.commit()
    elif child.status == "succeeded":
        return await _agent_succeeded(session, node_run, child)
    elif child.status in {"failed", "cancelled"}:
        return await _agent_failed(session, run, node_run, child)
    elif child.status not in {"queued", "waiting_approval"}:
        await _abort_node(
            session,
            run,
            node_run,
            "workflow_internal_error",
            f"子运行状态异常: {child.status}",
        )
        return "error"

    await run_agent(
        session,
        child,
        gateway=deps.gateway,
        tool_registry=deps.tool_registry.subset(patterns),
        skill_registry=deps.skill_registry,
        storage=deps.storage,
        queue=deps.queue,
        settings=deps.settings,
        session_factory=deps.session_factory,
    )
    await session.refresh(child)
    if child.status == "succeeded":
        return await _agent_succeeded(session, node_run, child)
    if child.status == "waiting_approval":
        moment = _now()
        node_run.status = "waiting"
        parked = await session.execute(
            update(WorkflowRun)
            .where(
                WorkflowRun.id == run.id,
                WorkflowRun.tenant_id == run.tenant_id,
                WorkflowRun.status.in_(ACTIVE),
            )
            .values(status="waiting_approval", heartbeat_at=moment)
            .execution_options(synchronize_session=False)
        )
        if parked.rowcount == 0:
            await session.rollback()
            return "stopped"
        await session.commit()
        return "waiting"
    return await _agent_failed(session, run, node_run, child)
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_workflow_agent_node.py -q`
Expected: PASS（9 passed）。

Run: `cd backend && uv run pytest tests/test_workflow_engine.py -q`
Expected: PASS（10 passed，`resume` 重命名未改变行为）。

Run: `cd backend && uv run ruff check app/workflow/engine.py app/agent/quota.py app/agent/registry.py`
Expected: `All checks passed!`

- [ ] **Step 7: 提交**

```bash
git add backend/app/agent/quota.py backend/app/agent/registry.py backend/app/workflow/engine.py backend/tests/test_workflow_agent_node.py
git commit -m "feat(workflow): add agent nodes with filtered tool registry"
```

## Task 8: 运行生命周期（领取、取消、续跑、对账）

**Files:**
- Create: `backend/app/workflow/lifecycle.py`
- Test: `backend/tests/test_workflow_lifecycle.py`

**背景与接口：** 引擎只推进已领取的运行；领取、取消、续跑、心跳对账放在 `lifecycle.py`，worker（Task 12）与 API（Task 11）都调用它。所有状态迁移都是条件更新（CAS），并发下先到者胜。

**关键约定（实现时照抄）：**
- `claim_run`：`queued → running`，`attempts + 1`，首次领取写 `started_at`；顺手把上次崩溃遗留的 `running` 节点置 `interrupted`。**只有 CAS 成功才返回 `"claimed"`**，未领到时返回观察到的状态（`running`/`waiting_approval`/…），worker 据此区分「我拿到了」和「别人拿到了」。
- `cancel_run`：`queued/running/waiting_approval → cancelled`；pending 工作流审批置 `expired`；子 `AgentRun`（非终态）置 `cancelled`，其 pending `agent_approvals` 置 `expired`，在途 `AgentStep` 置 `interrupted`；`running/waiting` 节点运行置 `interrupted`。
- `fail_waiting_run`：审批被驳回时由决策 API 直接终结父运行 —— `waiting_approval → failed`（写入给定错误码与消息），所有 `waiting` 节点置 `denied`、`running` 节点置 `interrupted`，该运行其余 pending 工作流审批置 `expired`，所有子 `AgentRun` 非终态置 `cancelled`、其 pending 审批置 `expired`；写 `workflow.run.reject` 审计。与 `cancel_run` 的区别只在终态与错误码，清理范围一致。
- `fail_active_run`：worker 的崩溃兜底 —— 只把 `queued/running` 改写为 `failed`（写入给定错误码与消息），并关闭 pending 工作流审批、取消子运行、中断在途节点；写 `workflow.run.fail` 审计。`waiting_approval` 属于审批决策者，绝不被它覆盖。
- `resume_run`：只接受 `failed`；`resume_count` 上限 10；把每个节点「最近一次」状态为 `error/denied/interrupted/skipped` 的节点运行改为 `interrupted`，引擎据此重跑（`skipped` 也要重估，条件分支可能因上游重跑而改变）。
- `settle_stale_run`：`attempts < 2` → 回 `queued`，否则 `failed/workflow_stale`；两种都把 `running/waiting` 节点置 `interrupted`。
- `reconcile`：超时运行、过期工作流审批、无任何 pending 审批的 `waiting_approval` 运行三类一次扫完；返回计数。可选参数 `on_requeue(run_id)` 在每次把超时运行重排回 `queued` 后调用一次，worker 用它入队；回调失败由调用方自己兜底（worker 记录并落失败），不阻断其余行的对账。
- 所有函数只做状态与数据，不做入队；入队由 worker 决定。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_workflow_lifecycle.py`:

```python
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.errors import AppError
from app.models.agent import AgentApproval, AgentRun, AgentStep
from app.models.base import DEFAULT_TENANT_ID
from app.models.workflow import (
    Workflow,
    WorkflowApproval,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowVersion,
)
from app.workflow import lifecycle

NOW = datetime.now(UTC)


async def make_run(
    db_session,
    *,
    status="running",
    attempts=1,
    resume_count=0,
    heartbeat_at=None,
    error_code="",
    error_message="",
):
    workflow = Workflow(tenant_id=DEFAULT_TENANT_ID, name="测试流", created_by=uuid.uuid4())
    db_session.add(workflow)
    await db_session.flush()
    version = WorkflowVersion(
        tenant_id=DEFAULT_TENANT_ID,
        workflow_id=workflow.id,
        version=1,
        graph={"nodes": [], "edges": []},
        source_revision=0,
        published_by=uuid.uuid4(),
        published_at=NOW,
    )
    db_session.add(version)
    await db_session.flush()
    run = WorkflowRun(
        tenant_id=DEFAULT_TENANT_ID,
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        status=status,
        inputs={},
        policy={},
        attempts=attempts,
        resume_count=resume_count,
        heartbeat_at=heartbeat_at,
        error_code=error_code,
        error_message=error_message,
        started_at=NOW - timedelta(minutes=5),
    )
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)
    return run


async def add_node_run(db_session, run, node_id, status, *, attempt=1):
    row = WorkflowNodeRun(
        tenant_id=run.tenant_id,
        run_id=run.id,
        node_id=node_id,
        attempt=attempt,
        kind="tool",
        status=status,
        started_at=NOW,
    )
    db_session.add(row)
    await db_session.commit()
    return row


async def add_workflow_approval(db_session, run, *, node_id="review", expires_at=None):
    row = WorkflowApproval(
        tenant_id=run.tenant_id,
        run_id=run.id,
        node_id=node_id,
        kind="review",
        title="审核",
        payload={},
        expires_at=expires_at,
    )
    db_session.add(row)
    await db_session.commit()
    return row


async def add_waiting_child(db_session, run):
    child = AgentRun(
        tenant_id=run.tenant_id,
        user_id=uuid.uuid4(),
        goal="节点任务",
        mode="node",
        status="waiting_approval",
        workflow_run_id=run.id,
        workflow_node_id="agent",
        budget={},
        policy={},
    )
    db_session.add(child)
    await db_session.flush()
    step = AgentStep(
        tenant_id=run.tenant_id,
        run_id=child.id,
        step_index=0,
        kind="tool",
        status="running",
    )
    db_session.add(step)
    await db_session.flush()
    approval = AgentApproval(
        tenant_id=run.tenant_id,
        run_id=child.id,
        step_id=step.id,
        tool_name="content.save",
        risk="high",
        payload={},
    )
    db_session.add(approval)
    await db_session.commit()
    return child, step, approval


async def latest_node(db_session, run, node_id):
    return (
        (
            await db_session.execute(
                select(WorkflowNodeRun)
                .where(
                    WorkflowNodeRun.run_id == run.id,
                    WorkflowNodeRun.node_id == node_id,
                )
                .order_by(WorkflowNodeRun.attempt.desc())
            )
        )
        .scalars()
        .first()
    )


async def test_claim_moves_queued_to_running_and_reopens_interrupted_nodes(
    db_session,
):
    run = await make_run(db_session, status="queued", attempts=0)
    await add_node_run(db_session, run, "a", "running")
    assert await lifecycle.claim_run(db_session, run.id) == "claimed"
    await db_session.refresh(run)
    assert run.status == "running"
    assert run.attempts == 1
    assert run.started_at is not None
    assert run.heartbeat_at is not None
    node = await latest_node(db_session, run, "a")
    assert node.status == "interrupted"
    # 第二次领取不再加 attempts，返回的是观察到的状态而不是 "claimed"，
    # worker 据此跳过重复投递。
    assert await lifecycle.claim_run(db_session, run.id) == "running"
    await db_session.refresh(run)
    assert run.attempts == 1


async def test_cancel_expires_approvals_children_and_nodes(db_session):
    run = await make_run(db_session, status="waiting_approval")
    approval = await add_workflow_approval(db_session, run)
    child, step, child_approval = await add_waiting_child(db_session, run)
    node = await add_node_run(db_session, run, "review", "waiting")
    assert await lifecycle.cancel_run(db_session, run, actor="tester") is True
    await db_session.refresh(run)
    await db_session.refresh(approval)
    await db_session.refresh(child)
    await db_session.refresh(step)
    await db_session.refresh(child_approval)
    await db_session.refresh(node)
    assert run.status == "cancelled"
    assert run.finished_at is not None
    assert approval.status == "expired"
    assert child.status == "cancelled"
    assert child_approval.status == "expired"
    assert step.status == "interrupted"
    assert node.status == "interrupted"


async def test_cancel_terminal_run_returns_false(db_session):
    run = await make_run(db_session, status="succeeded")
    assert await lifecycle.cancel_run(db_session, run) is False
    await db_session.refresh(run)
    assert run.status == "succeeded"


async def test_resume_reopens_failed_denied_and_skipped_nodes(db_session):
    run = await make_run(
        db_session, status="failed", error_code="node_failed", error_message="坏了"
    )
    await add_node_run(db_session, run, "ok", "ok")
    await add_node_run(db_session, run, "bad", "error")
    await add_node_run(db_session, run, "denied", "denied")
    await add_node_run(db_session, run, "skipped", "skipped")
    await lifecycle.resume_run(db_session, run, actor="tester")
    await db_session.refresh(run)
    assert run.status == "queued"
    assert run.resume_count == 1
    assert run.error_code == ""
    assert run.finished_at is None
    statuses = {}
    for node_id in ("ok", "bad", "denied", "skipped"):
        statuses[node_id] = (await latest_node(db_session, run, node_id)).status
    assert statuses == {
        "ok": "ok",
        "bad": "interrupted",
        "denied": "interrupted",
        "skipped": "interrupted",
    }


async def test_resume_rejects_running_and_exhausted_runs(db_session):
    running = await make_run(db_session, status="running")
    with pytest.raises(AppError) as exc:
        await lifecycle.resume_run(db_session, running)
    assert exc.value.code == "workflow_run_not_resumable"

    exhausted = await make_run(db_session, status="failed", resume_count=10)
    with pytest.raises(AppError) as exc:
        await lifecycle.resume_run(db_session, exhausted)
    assert exc.value.code == "workflow_run_not_resumable"


async def test_stale_run_requeues_before_second_attempt(db_session):
    run = await make_run(
        db_session, attempts=1, heartbeat_at=NOW - timedelta(minutes=10)
    )
    await add_node_run(db_session, run, "a", "running")
    assert await lifecycle.settle_stale_run(db_session, run.id) == "queued"
    await db_session.refresh(run)
    assert run.status == "queued"
    assert run.error_code == ""
    assert (await latest_node(db_session, run, "a")).status == "interrupted"


async def test_stale_run_fails_after_second_attempt(db_session):
    run = await make_run(
        db_session, attempts=2, heartbeat_at=NOW - timedelta(minutes=10)
    )
    assert await lifecycle.settle_stale_run(db_session, run.id) == "failed"
    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.error_code == "workflow_stale"


@pytest_asyncio.fixture
async def factory(db_session):
    return async_sessionmaker(db_session.bind, expire_on_commit=False)


async def test_reconcile_expires_overdue_approval_and_fails_run(factory, db_session):
    run = await make_run(db_session, status="waiting_approval")
    approval = await add_workflow_approval(
        db_session, run, expires_at=NOW - timedelta(hours=1)
    )
    await add_node_run(db_session, run, "review", "waiting")
    counts = await lifecycle.reconcile(
        factory, settings=get_settings()
    )
    await db_session.refresh(run)
    await db_session.refresh(approval)
    assert approval.status == "expired"
    assert run.status == "failed"
    assert run.error_code == "approval_expired"
    assert (await latest_node(db_session, run, "review")).status == "denied"
    assert counts["approval_expired"] == 1


async def test_reconcile_fails_waiting_run_without_pending_approvals(factory, db_session):
    run = await make_run(db_session, status="waiting_approval")
    await add_node_run(db_session, run, "review", "waiting")
    counts = await lifecycle.reconcile(
        factory, settings=get_settings()
    )
    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.error_code == "approval_expired"
    assert counts["approval_expired"] == 1


async def test_reconcile_keeps_waiting_run_with_child_pending(factory, db_session):
    run = await make_run(db_session, status="waiting_approval")
    await add_waiting_child(db_session, run)
    counts = await lifecycle.reconcile(
        factory, settings=get_settings()
    )
    await db_session.refresh(run)
    assert run.status == "waiting_approval"
    assert counts["approval_expired"] == 0


async def test_reconcile_reports_requeued_runs_to_callback(factory, db_session):
    run = await make_run(db_session, status="running", attempts=0)
    seen = []

    async def on_requeue(run_id):
        seen.append(run_id)

    counts = await lifecycle.reconcile(
        factory, settings=get_settings(), on_requeue=on_requeue
    )
    await db_session.refresh(run)
    assert run.status == "queued"
    assert counts["requeued"] == 1
    assert seen == [run.id]


async def test_fail_waiting_run_denies_node_and_expires_the_rest(db_session):
    run = await make_run(db_session, status="waiting_approval")
    workflow_approval = await add_workflow_approval(
        db_session, run, node_id="other"
    )
    node = await add_node_run(db_session, run, "agent", "waiting")
    child, _step, child_approval = await add_waiting_child(db_session, run)
    assert (
        await lifecycle.fail_waiting_run(
            db_session,
            run.id,
            error_code="approval_rejected",
            message="审批已驳回",
            actor="tester",
        )
        is True
    )
    for row in (run, node, workflow_approval, child, child_approval):
        await db_session.refresh(row)
    assert run.status == "failed"
    assert run.error_code == "approval_rejected"
    assert run.finished_at is not None
    assert node.status == "denied"
    assert workflow_approval.status == "expired"
    assert child.status == "cancelled"
    assert child_approval.status == "expired"
    # 已经是终态的运行不会被再次改写
    assert (
        await lifecycle.fail_waiting_run(
            db_session,
            run.id,
            error_code="approval_rejected",
            message="审批已驳回",
        )
        is False
    )


async def test_fail_active_run_closes_in_flight_work_only(db_session):
    run = await make_run(db_session, status="running", attempts=1)
    workflow_approval = await add_workflow_approval(db_session, run)
    node = await add_node_run(db_session, run, "agent", "running")
    child, step, child_approval = await add_waiting_child(db_session, run)
    assert (
        await lifecycle.fail_active_run(
            db_session,
            run.id,
            error_code="workflow_internal_error",
            message="运行异常，请稍后重试",
            actor="worker",
        )
        is True
    )
    for row in (run, workflow_approval, node, child, step, child_approval):
        await db_session.refresh(row)
    assert run.status == "failed"
    assert run.error_code == "workflow_internal_error"
    assert run.finished_at is not None
    assert workflow_approval.status == "expired"
    assert node.status == "interrupted"
    assert child.status == "cancelled"
    assert step.status == "interrupted"
    assert child_approval.status == "expired"
    # waiting_approval 属于审批决策者，崩溃兜底不能覆盖它
    waiting = await make_run(db_session, status="waiting_approval")
    assert (
        await lifecycle.fail_active_run(
            db_session,
            waiting.id,
            error_code="workflow_internal_error",
            message="运行异常，请稍后重试",
        )
        is False
    )
    await db_session.refresh(waiting)
    assert waiting.status == "waiting_approval"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_workflow_lifecycle.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.workflow.lifecycle'`。

- [ ] **Step 3: 实现 lifecycle.py**

Create `backend/app/workflow/lifecycle.py`:

```python
"""工作流运行生命周期：领取、取消、续跑与心跳对账。

引擎不领取运行；worker 用 ``claim_run`` 的 CAS 拿到所有权后才推进。
这里所有状态迁移都是条件更新，并发时先到者胜，后到者读到最新状态。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.errors import AppError
from app.models.agent import AgentApproval, AgentRun, AgentStep
from app.models.workflow import (
    WorkflowApproval,
    WorkflowNodeRun,
    WorkflowRun,
)
from app.services.audit import record_best_effort
from app.workflow import approvals

logger = logging.getLogger(__name__)

CANCELLABLE = frozenset({"queued", "running", "waiting_approval"})
REOPEN = frozenset({"error", "denied", "interrupted", "skipped"})
MAX_RESUME = 10
INTERRUPTED_MESSAGE = "上次执行中断"
APPROVAL_EXPIRED_MESSAGE = "审批已失效"


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _interrupt_nodes(session: AsyncSession, run_id: uuid.UUID) -> None:
    await session.execute(
        update(WorkflowNodeRun)
        .where(
            WorkflowNodeRun.run_id == run_id,
            WorkflowNodeRun.status.in_({"running", "waiting"}),
        )
        .values(
            status="interrupted",
            error=INTERRUPTED_MESSAGE,
            finished_at=_now(),
        )
        .execution_options(synchronize_session=False)
    )


async def _close_children(
    session: AsyncSession,
    run_id: uuid.UUID,
    moment: datetime,
    note: str,
) -> None:
    """取消仍在活动的子运行，并关闭它们的审批与在途步骤。"""
    children = (
        (
            await session.execute(
                select(AgentRun.id).where(
                    AgentRun.workflow_run_id == run_id,
                    AgentRun.status.in_({"queued", "running", "waiting_approval"}),
                )
            )
        )
        .scalars()
        .all()
    )
    if not children:
        return
    await session.execute(
        update(AgentApproval)
        .where(
            AgentApproval.run_id.in_(children),
            AgentApproval.status == "pending",
        )
        .values(status="expired", decided_at=moment, note=note)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(AgentStep)
        .where(
            AgentStep.run_id.in_(children),
            AgentStep.status == "running",
        )
        .values(status="interrupted", error=note, finished_at=moment)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(AgentRun)
        .where(AgentRun.id.in_(children))
        .values(status="cancelled", finished_at=moment)
        .execution_options(synchronize_session=False)
    )


async def claim_run(session: AsyncSession, run_id: uuid.UUID) -> str:
    """CAS 领取一个 queued 运行。

    返回 ``"claimed"`` 表示本次调用拿到了运行；其余返回值是观察到的状态
    （别的执行者已经领走、还在等审批、已终态或不存在），worker 看到非
    ``"claimed"`` 就跳过，重复投递因此不会重复执行。
    """
    moment = _now()
    run = await session.get(WorkflowRun, run_id)
    if run is None:
        return "missing"
    claimed = await session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == run_id, WorkflowRun.status == "queued")
        .values(
            status="running",
            attempts=WorkflowRun.attempts + 1,
            heartbeat_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount == 0:
        await session.rollback()
        await session.refresh(run)
        return run.status
    await session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == run_id, WorkflowRun.started_at.is_(None))
        .values(started_at=moment)
        .execution_options(synchronize_session=False)
    )
    await _interrupt_nodes(session, run_id)
    await session.commit()
    return "claimed"


async def _audit(
    session: AsyncSession,
    run_id: uuid.UUID,
    tenant_id: str,
    action: str,
    actor: str,
) -> None:
    await record_best_effort(
        session,
        tenant_id=tenant_id,
        action=action,
        actor=actor,
        resource_type="workflow_run",
        resource_id=str(run_id),
    )


async def cancel_run(
    session: AsyncSession, run: WorkflowRun, *, actor: str = "system"
) -> bool:
    moment = _now()
    cancelled = await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run.id,
            WorkflowRun.tenant_id == run.tenant_id,
            WorkflowRun.status.in_(CANCELLABLE),
        )
        .values(status="cancelled", finished_at=moment, heartbeat_at=moment)
        .execution_options(synchronize_session=False)
    )
    if cancelled.rowcount == 0:
        await session.rollback()
        return False

    await session.execute(
        update(WorkflowApproval)
        .where(
            WorkflowApproval.run_id == run.id,
            WorkflowApproval.status == "pending",
        )
        .values(status="expired", decided_at=moment)
        .execution_options(synchronize_session=False)
    )
    children = (
        (
            await session.execute(
                select(AgentRun.id).where(
                    AgentRun.workflow_run_id == run.id,
                    AgentRun.status.in_({"queued", "running", "waiting_approval"}),
                )
            )
        )
        .scalars()
        .all()
    )
    if children:
        await session.execute(
            update(AgentApproval)
            .where(
                AgentApproval.run_id.in_(children),
                AgentApproval.status == "pending",
            )
            .values(status="expired", decided_at=moment)
            .execution_options(synchronize_session=False)
        )
        await session.execute(
            update(AgentStep)
            .where(
                AgentStep.run_id.in_(children),
                AgentStep.status == "running",
            )
            .values(
                status="interrupted",
                error="运行已取消",
                finished_at=moment,
            )
            .execution_options(synchronize_session=False)
        )
        await session.execute(
            update(AgentRun)
            .where(AgentRun.id.in_(children))
            .values(status="cancelled", finished_at=moment)
            .execution_options(synchronize_session=False)
        )
    await _interrupt_nodes(session, run.id)
    await session.commit()
    await _audit(session, run.id, run.tenant_id, "workflow.run.cancel", actor)
    return True


async def fail_waiting_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    error_code: str,
    message: str,
    actor: str = "system",
) -> bool:
    """终结一个因审批被驳回而永远等不到恢复的运行。

    子运行审批由第 1 层决策，引擎没有机会自己发现驳回；决策 API 在同一个
    事务里关掉父运行、清掉其余挂起审批与子运行，保证没有审批或子运行比
    父运行活得更久。只有 ``waiting_approval`` 会被改写，并发取消/过期保持
    自己的终态。
    """
    moment = _now()
    run = await session.get(WorkflowRun, run_id)
    if run is None:
        return False
    failed = await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run.id,
            WorkflowRun.tenant_id == run.tenant_id,
            WorkflowRun.status == "waiting_approval",
        )
        .values(
            status="failed",
            error_code=error_code,
            error_message=message,
            finished_at=moment,
            heartbeat_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    if failed.rowcount == 0:
        await session.rollback()
        return False

    await session.execute(
        update(WorkflowApproval)
        .where(
            WorkflowApproval.run_id == run.id,
            WorkflowApproval.status == "pending",
        )
        .values(status="expired", decided_at=moment)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(WorkflowNodeRun)
        .where(
            WorkflowNodeRun.run_id == run.id,
            WorkflowNodeRun.status == "waiting",
        )
        .values(
            status="denied", error=message, finished_at=moment
        )
        .execution_options(synchronize_session=False)
    )
    await _close_children(session, run.id, moment, message)
    # waiting 节点已经落 denied；这里只兜底可能残留的 running 节点。
    await _interrupt_nodes(session, run.id)
    await session.commit()
    await _audit(session, run.id, run.tenant_id, "workflow.run.reject", actor)
    return True


async def fail_active_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    error_code: str,
    message: str,
    actor: str = "system",
) -> bool:
    """把仍在 ``queued``/``running`` 的运行落为失败并清理在途状态。

    worker 的崩溃兜底入口。与 ``fail_waiting_run`` 的区别是只改写这两个
    状态：等待审批的运行属于审批决策者，不能被崩溃兜底覆盖。改写成功后
    同一事务里过期 pending 审批、中断在途节点并取消子运行，避免失败运行
    留下仍可决策的审批或仍在后台写结果的子运行。
    """
    moment = _now()
    run = await session.get(WorkflowRun, run_id)
    if run is None:
        return False
    failed = await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run.id,
            WorkflowRun.tenant_id == run.tenant_id,
            WorkflowRun.status.in_({"queued", "running"}),
        )
        .values(
            status="failed",
            error_code=error_code,
            error_message=message,
            finished_at=moment,
            heartbeat_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    if failed.rowcount == 0:
        await session.rollback()
        return False
    await session.execute(
        update(WorkflowApproval)
        .where(
            WorkflowApproval.run_id == run.id,
            WorkflowApproval.status == "pending",
        )
        .values(status="expired", decided_at=moment)
        .execution_options(synchronize_session=False)
    )
    await _close_children(session, run.id, moment, message)
    await _interrupt_nodes(session, run.id)
    await session.commit()
    await _audit(session, run.id, run.tenant_id, "workflow.run.fail", actor)
    return True


async def resume_run(
    session: AsyncSession, run: WorkflowRun, *, actor: str = "system"
) -> None:
    if run.status != "failed":
        raise AppError(
            "workflow_run_not_resumable", "只有失败的运行可以续跑", 409
        )
    if int(run.resume_count or 0) >= MAX_RESUME:
        raise AppError(
            "workflow_run_not_resumable", "续跑次数已达上限", 409
        )
    moment = _now()
    resumed = await session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == run.id, WorkflowRun.status == "failed")
        .values(
            status="queued",
            resume_count=WorkflowRun.resume_count + 1,
            error_code="",
            error_message="",
            finished_at=None,
            heartbeat_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    if resumed.rowcount == 0:
        await session.rollback()
        raise AppError(
            "workflow_run_not_resumable", "运行状态已变化，无法续跑", 409
        )
    rows = (
        (
            await session.execute(
                select(WorkflowNodeRun)
                .where(WorkflowNodeRun.run_id == run.id)
                .order_by(WorkflowNodeRun.attempt)
            )
        )
        .scalars()
        .all()
    )
    latest: dict[str, WorkflowNodeRun] = {}
    for row in rows:
        latest[row.node_id] = row
    for row in latest.values():
        if row.status in REOPEN:
            row.status = "interrupted"
            row.error = "等待续跑"
            row.finished_at = None
    await session.commit()
    await session.refresh(run)
    await _audit(session, run.id, run.tenant_id, "workflow.run.resume", actor)


async def settle_stale_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> str:
    moment = now or _now()
    run = await session.get(WorkflowRun, run_id)
    if run is None:
        return "missing"
    if run.status != "running":
        return run.status
    if int(run.attempts or 0) < 2:
        changed = await session.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run_id, WorkflowRun.status == "running")
            .values(status="queued", heartbeat_at=moment)
            .execution_options(synchronize_session=False)
        )
        outcome = "queued"
    else:
        changed = await session.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run_id, WorkflowRun.status == "running")
            .values(
                status="failed",
                error_code="workflow_stale",
                error_message="运行心跳超时",
                finished_at=moment,
                heartbeat_at=moment,
            )
            .execution_options(synchronize_session=False)
        )
        outcome = "failed"
    if changed.rowcount == 0:
        await session.rollback()
        return "stale"
    await _interrupt_nodes(session, run_id)
    await session.commit()
    return outcome


async def _fail_approval_run(
    session: AsyncSession, run_id: uuid.UUID, moment: datetime
) -> int:
    run = await session.get(WorkflowRun, run_id)
    if run is None or run.status != "waiting_approval":
        return 0
    await session.execute(
        update(WorkflowApproval)
        .where(
            WorkflowApproval.run_id == run_id,
            WorkflowApproval.status == "pending",
        )
        .values(status="expired", decided_at=moment)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(WorkflowNodeRun)
        .where(
            WorkflowNodeRun.run_id == run_id,
            WorkflowNodeRun.status == "waiting",
        )
        .values(
            status="denied",
            error=APPROVAL_EXPIRED_MESSAGE,
            finished_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    failed = await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run_id,
            WorkflowRun.status == "waiting_approval",
        )
        .values(
            status="failed",
            error_code="approval_expired",
            error_message=APPROVAL_EXPIRED_MESSAGE,
            finished_at=moment,
            heartbeat_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return 1 if failed.rowcount else 0


async def _expire_overdue(session: AsyncSession, moment: datetime) -> int:
    rows = (
        (
            await session.execute(
                select(WorkflowApproval).where(
                    WorkflowApproval.status == "pending",
                    WorkflowApproval.expires_at.is_not(None),
                    WorkflowApproval.expires_at <= moment,
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0
    for row in rows:
        row.status = "expired"
        row.decided_at = moment
    await session.commit()
    failed = 0
    for run_id in {row.run_id for row in rows}:
        failed += await _fail_approval_run(session, run_id, moment)
    return failed


async def _fail_orphans(session: AsyncSession, moment: datetime) -> int:
    runs = (
        (
            await session.execute(
                select(WorkflowRun)
                .where(WorkflowRun.status == "waiting_approval")
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    failed = 0
    for run in runs:
        if await approvals.has_pending(
            session, tenant_id=run.tenant_id, run_id=run.id
        ):
            continue
        failed += await _fail_approval_run(session, run.id, moment)
    return failed


async def reconcile(
    factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    now: datetime | None = None,
    on_requeue: Callable[[uuid.UUID], Awaitable[None]] | None = None,
) -> dict[str, int]:
    """每分钟对账：超时运行、过期审批、无 pending 的挂起运行。

    ``on_requeue`` 在每次把运行重排回 ``queued`` 后调用一次，worker 用它
    入队；回调抛错会中断本次扫描，由调用方自己兜底（worker 在回调内部
    消化入队失败，不会抛到这里）。
    """
    moment = now or _now()
    counts = {"requeued": 0, "stale_failed": 0, "approval_expired": 0}
    cutoff = moment - timedelta(
        seconds=max(1, int(settings.workflow_run_stale_seconds or 0))
    )
    async with factory() as session:
        stale = (
            (
                await session.execute(
                    select(WorkflowRun.id)
                    .where(
                        WorkflowRun.status == "running",
                        or_(
                            WorkflowRun.heartbeat_at.is_(None),
                            WorkflowRun.heartbeat_at < cutoff,
                        ),
                    )
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
    for run_id in stale:
        async with factory() as session:
            outcome = await settle_stale_run(session, run_id, now=moment)
        if outcome == "queued":
            counts["requeued"] += 1
            if on_requeue is not None:
                await on_requeue(run_id)
        elif outcome == "failed":
            counts["stale_failed"] += 1

    async with factory() as session:
        counts["approval_expired"] += await _expire_overdue(session, moment)
        counts["approval_expired"] += await _fail_orphans(session, moment)
    if any(counts.values()):
        logger.info("workflow reconcile %s", counts)
    return counts
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_workflow_lifecycle.py -q`
Expected: PASS（13 passed）。

Run: `cd backend && uv run ruff check app/workflow/lifecycle.py`
Expected: `All checks passed!`

- [ ] **Step 5: 提交**

```bash
git add backend/app/workflow/lifecycle.py backend/tests/test_workflow_lifecycle.py
git commit -m "feat(workflow): add run lifecycle and reconciliation"
```

---

## Task 9: 定义、版本与模板 API（含 `GET /api/agent/tools`）

**Files:**
- Create: `backend/app/workflow/templates.py`
- Create: `backend/app/api/workflows.py`
- Modify: `backend/app/api/agent.py`（在 `get_policy` 之前加只读工具清单）
- Modify: `backend/app/main.py`（注册 `workflows` 路由）
- Test: `backend/tests/test_workflow_api.py`

**背景与接口：** 定义 API 是画布的后端。列表只回摘要（未发布标记、最新版本、下次定时、最近运行）；详情回草稿图。保存用 `revision` 做乐观锁，只做结构校验（允许未完成的草稿），发布才跑权威校验。发布版本不可变，恢复版本只是把图复制回草稿。模板是三个内置常量，实例化即建草稿。

**关键约定（实现时照抄）：**
- 路由声明顺序：`/templates*` 必须在 `/{workflow_id}` 之前，否则 `templates` 会被当成 id。
- 跨租户、不存在、已归档统一 `workflow_not_found` 404；不做存在性区分。
- `has_unpublished = 最新版本为空 or draft_revision != 最新版本.source_revision`。
- 保存草稿用 `check_structure`（形状、规模）；发布用 `validate_publish`（单输入、可达、无环、句柄、工具/技能存在、预算）。两者失败都返回 `422 workflow_invalid`，`detail.issues` 为 `Issue.as_dict()` 列表。
- `validate_publish` 的 `overrides` 取当前租户策略，`skill_ids` 取技能注册表键集合。
- 所有写接口成功后调用 `record_best_effort`，审计 detail 只记 id/版本/字段名，不记图内容。
- 删除是软删除（`archived_at`）；存在 `queued/running/waiting_approval` 运行 → `409 workflow_run_active`。
- 模板图在实例化时 `copy.deepcopy`，避免两个工作流共享同一个 dict。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_workflow_api.py`:

```python
import uuid
from datetime import UTC, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.db import get_session
from app.deps import get_current_user
from app.main import app
from app.models.base import DEFAULT_TENANT_ID
from app.models.user import User
from app.models.workflow import Workflow, WorkflowRun, WorkflowVersion

OTHER_TENANT = "00000000-0000-0000-0000-000000000002"


@pytest_asyncio.fixture
async def api(db_session):
    state = {"user": None}

    async def current_user():
        return state["user"]

    app.dependency_overrides[get_session] = lambda: db_session
    app.dependency_overrides[get_current_user] = current_user
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            yield http, state
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_session, None)


async def make_user(db_session, *, role="owner", tenant_id=DEFAULT_TENANT_ID):
    user = User(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        email=f"{uuid.uuid4().hex[:10]}@example.com",
        password_hash="x",
        display_name="测试用户",
        role=role,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def graph(*, days="{{inputs.days}}"):
    return {
        "nodes": [
            {
                "id": "in",
                "type": "input",
                "position": {"x": 0, "y": 0},
                "data": {
                    "fields": [
                        {"key": "days", "type": "number", "required": True}
                    ]
                },
            },
            {
                "id": "read",
                "type": "tool",
                "position": {"x": 220, "y": 0},
                "data": {"tool": "analytics.summary", "args": {"days": days}},
            },
            {
                "id": "out",
                "type": "output",
                "position": {"x": 440, "y": 0},
                "data": {},
            },
        ],
        "edges": [
            {"source": "in", "target": "read"},
            {"source": "read", "target": "out"},
        ],
    }


async def create_via_api(http, *, name="数据复盘", body=None):
    return await http.post(
        "/api/workflows",
        json={"name": name, "graph": body if body is not None else graph()},
    )


async def publish_version(db_session, workflow: Workflow) -> WorkflowVersion:
    version = WorkflowVersion(
        tenant_id=workflow.tenant_id,
        workflow_id=workflow.id,
        version=1,
        graph=workflow.draft_graph or {"nodes": [], "edges": []},
        source_revision=workflow.draft_revision,
        published_by=workflow.created_by,
        published_at=datetime.now(UTC),
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return version


async def test_admin_creates_lists_and_reads_detail(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")

    created = await create_via_api(http)

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "数据复盘"
    assert body["latest_version"] is None
    assert body["has_unpublished"] is True
    assert body["draft_revision"] == 0

    listed = await http.get("/api/workflows")

    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [body["id"]]
    assert listed.json()[0]["latest_version"] is None

    detail = await http.get(f"/api/workflows/{body['id']}")

    assert detail.status_code == 200, detail.text
    assert detail.json()["draft_graph"] == graph()
    assert detail.json()["latest_published"] is None
    assert detail.json()["schedules"] == []


async def test_role_matrix_blocks_editor_and_viewer_writes(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id = (await create_via_api(http)).json()["id"]

    state["user"] = await make_user(db_session, role="editor")
    assert (await http.get(f"/api/workflows/{workflow_id}")).status_code == 200
    assert (
        await http.patch(
            f"/api/workflows/{workflow_id}",
            json={"revision": 0, "name": "改名"},
        )
    ).status_code == 403
    assert (
        await create_via_api(http, name="越权创建")
    ).status_code == 403
    assert (
        await http.post(f"/api/workflows/{workflow_id}/publish",
                        json={"revision": 0})
    ).status_code == 403
    assert (await http.get("/api/workflows/templates")).status_code == 403

    state["user"] = await make_user(db_session, role="viewer")
    assert (await http.get("/api/workflows")).status_code == 200
    assert (
        await http.delete(f"/api/workflows/{workflow_id}")
    ).status_code == 403
    assert (
        await http.post(f"/api/workflows/{workflow_id}/validate")
    ).status_code == 403


async def test_patch_is_optimistic_and_rejects_bad_shape(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id = (await create_via_api(http)).json()["id"]

    saved = await http.patch(
        f"/api/workflows/{workflow_id}",
        json={"revision": 0, "name": "周度复盘", "description": "每周一跑"},
    )

    assert saved.status_code == 200, saved.text
    assert saved.json()["draft_revision"] == 1
    assert saved.json()["name"] == "周度复盘"

    stale = await http.patch(
        f"/api/workflows/{workflow_id}",
        json={"revision": 0, "name": "旧会话"},
    )

    assert stale.status_code == 409
    assert stale.json()["code"] == "workflow_conflict"

    broken = await http.patch(
        f"/api/workflows/{workflow_id}",
        json={"revision": 1, "draft_graph": {"nodes": "不是数组"}},
    )

    assert broken.status_code == 422
    assert broken.json()["code"] == "workflow_invalid"
    assert broken.json()["detail"]["issues"]


async def test_publish_runs_authoritative_validation(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    # 结构合法但语义不完整的草稿：可以保存，不能发布。
    draft = {
        "nodes": [
            {"id": "out", "type": "output", "position": {}, "data": {}}
        ],
        "edges": [],
    }
    workflow_id = (await create_via_api(http, body=draft)).json()["id"]

    saved = await http.patch(
        f"/api/workflows/{workflow_id}",
        json={"revision": 0, "draft_graph": draft},
    )
    assert saved.status_code == 200, saved.text

    validated = await http.post(f"/api/workflows/{workflow_id}/validate")
    assert validated.status_code == 200
    assert validated.json()["ok"] is False
    assert any(
        issue["code"] == "input_invalid"
        for issue in validated.json()["issues"]
    )

    published = await http.post(
        f"/api/workflows/{workflow_id}/publish", json={"revision": 1}
    )

    assert published.status_code == 422
    assert published.json()["code"] == "workflow_invalid"
    assert published.json()["detail"]["issues"]


async def test_publish_versions_are_immutable_and_restorable(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id = (await create_via_api(http)).json()["id"]

    first = await http.post(
        f"/api/workflows/{workflow_id}/publish",
        json={"revision": 0, "note": "首版"},
    )

    assert first.status_code == 201, first.text
    assert first.json()["version"] == 1
    assert first.json()["source_revision"] == 0
    assert first.json()["note"] == "首版"

    changed = graph(days=7)
    saved = await http.patch(
        f"/api/workflows/{workflow_id}",
        json={"revision": 0, "draft_graph": changed},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["has_unpublished"] is True

    second = await http.post(
        f"/api/workflows/{workflow_id}/publish", json={"revision": 1}
    )
    assert second.status_code == 201, second.text
    assert second.json()["version"] == 2

    versions = await http.get(f"/api/workflows/{workflow_id}/versions")

    assert versions.status_code == 200
    assert [item["version"] for item in versions.json()] == [2, 1]
    assert "graph" not in versions.json()[0]

    one = await http.get(f"/api/workflows/{workflow_id}/versions/1")

    assert one.status_code == 200
    assert one.json()["graph"] == graph()

    restored = await http.post(
        f"/api/workflows/{workflow_id}/versions/1/restore"
    )

    assert restored.status_code == 200, restored.text
    assert restored.json()["draft_graph"] == graph()
    assert restored.json()["draft_revision"] == 2
    assert restored.json()["has_unpublished"] is True
    # 恢复不改动已发布版本。
    assert (
        await http.get(f"/api/workflows/{workflow_id}/versions/1")
    ).json()["graph"] == graph()


async def test_publish_rejects_stale_revision(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id = (await create_via_api(http)).json()["id"]

    resp = await http.post(
        f"/api/workflows/{workflow_id}/publish", json={"revision": 3}
    )

    assert resp.status_code == 409
    assert resp.json()["code"] == "workflow_conflict"


async def test_cross_tenant_workflows_are_404(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    foreign = Workflow(
        tenant_id=OTHER_TENANT,
        name="别人的流",
        created_by=uuid.uuid4(),
        draft_graph=graph(),
    )
    db_session.add(foreign)
    await db_session.commit()
    await db_session.refresh(foreign)

    for method, path, kwargs in (
        ("get", f"/api/workflows/{foreign.id}", {}),
        ("patch", f"/api/workflows/{foreign.id}", {"json": {"revision": 0}}),
        ("delete", f"/api/workflows/{foreign.id}", {}),
        ("post", f"/api/workflows/{foreign.id}/publish", {"json": {"revision": 0}}),
        ("post", f"/api/workflows/{foreign.id}/validate", {}),
    ):
        resp = await getattr(http, method)(path, **kwargs)
        assert resp.status_code == 404, (method, path, resp.text)
        assert resp.json()["code"] == "workflow_not_found"


async def test_delete_is_soft_and_blocked_by_active_run(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id = (await create_via_api(http)).json()["id"]
    workflow = await db_session.get(Workflow, uuid.UUID(workflow_id))
    version = await publish_version(db_session, workflow)
    run = WorkflowRun(
        tenant_id=DEFAULT_TENANT_ID,
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        status="running",
        inputs={},
        policy={},
    )
    db_session.add(run)
    await db_session.commit()

    blocked = await http.delete(f"/api/workflows/{workflow_id}")

    assert blocked.status_code == 409
    assert blocked.json()["code"] == "workflow_run_active"

    run.status = "succeeded"
    await db_session.commit()

    deleted = await http.delete(f"/api/workflows/{workflow_id}")

    assert deleted.status_code == 204
    assert (await http.get(f"/api/workflows/{workflow_id}")).status_code == 404
    assert (await http.get("/api/workflows")).json() == []
    stored = await db_session.get(Workflow, uuid.UUID(workflow_id))
    await db_session.refresh(stored)
    assert stored.archived_at is not None


async def test_templates_list_and_instantiate(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")

    listed = await http.get("/api/workflows/templates")

    assert listed.status_code == 200, listed.text
    keys = [item["key"] for item in listed.json()]
    assert keys == [
        "weekly_topic",
        "multi_platform_rewrite",
        "post_publish_review",
    ]
    assert all(item["parameters"] for item in listed.json())

    created = await http.post(
        "/api/workflows/templates/weekly_topic/instantiate",
        json={"name": "我的周更"},
    )

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "我的周更"
    assert body["has_unpublished"] is True
    detail = await http.get(f"/api/workflows/{body['id']}")
    assert len(detail.json()["draft_graph"]["nodes"]) == 5

    missing = await http.post(
        "/api/workflows/templates/nope/instantiate", json={"name": "x"}
    )
    assert missing.status_code == 404


async def test_agent_tools_endpoint_exposes_risk_and_schema(api, db_session):
    http, state = api

    state["user"] = await make_user(db_session, role="editor")
    resp = await http.get("/api/agent/tools")

    assert resp.status_code == 200, resp.text
    tools = {item["name"]: item for item in resp.json()}
    assert tools["content.create_version"]["risk"] == "low"
    assert tools["analytics.summary"]["risk"] == "read"
    assert tools["project.create"]["parameters"]["type"] == "object"
    assert tools["publish.create_draft_job"]["description"]

    state["user"] = await make_user(db_session, role="viewer")
    assert (await http.get("/api/agent/tools")).status_code == 403
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_workflow_api.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'app.api.workflows'` 或导入 `app.workflow.templates` 失败）。

- [ ] **Step 3: 写模板常量**

Create `backend/app/workflow/templates.py`:

```python
"""内置工作流模板。

模板只包含图，不含租户数据；实例化时深拷贝，调用方负责落库。
所有模板都必须能通过 `validate_publish`：单一输入、所有节点可达输出、无环。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Template:
    key: str
    name: str
    description: str
    parameters: list[dict]
    graph: dict


WEEKLY_TOPIC = {
    "nodes": [
        {
            "id": "in",
            "type": "input",
            "position": {"x": 0, "y": 140},
            "data": {
                "name": "本周主题",
                "fields": [
                    {
                        "key": "topic",
                        "type": "string",
                        "required": True,
                        "maxLength": 200,
                    }
                ],
            },
        },
        {
            "id": "write",
            "type": "agent",
            "position": {"x": 260, "y": 140},
            "data": {
                "name": "选题与创作",
                "goal": (
                    "围绕「{{inputs.topic}}」完成本周选题，并写出一篇可直接"
                    "发布的完整稿件，包含标题与正文。"
                ),
                "tools": ["profile.*", "content.list"],
                "budget": {"max_steps": 12, "max_tool_calls": 6},
                "retry": 1,
            },
        },
        {
            "id": "review",
            "type": "review",
            "position": {"x": 540, "y": 140},
            "data": {"name": "内容审核", "title": "审核本周稿件"},
        },
        {
            "id": "draft",
            "type": "tool",
            "position": {"x": 820, "y": 140},
            "data": {
                "name": "建立项目草稿",
                "tool": "project.create",
                "args": {"title": "{{inputs.topic}}"},
                "retry": 0,
            },
        },
        {
            "id": "out",
            "type": "output",
            "position": {"x": 1100, "y": 140},
            "data": {},
        },
    ],
    "edges": [
        {"source": "in", "target": "write"},
        {"source": "write", "target": "review"},
        {"source": "review", "target": "draft"},
        {"source": "draft", "target": "out"},
    ],
}

MULTI_PLATFORM_REWRITE = {
    "nodes": [
        {
            "id": "in",
            "type": "input",
            "position": {"x": 0, "y": 140},
            "data": {
                "name": "原文与目标平台",
                "fields": [
                    {
                        "key": "source",
                        "type": "string",
                        "required": True,
                        "maxLength": 100000,
                    },
                    {
                        "key": "platforms",
                        "type": "string",
                        "required": False,
                        "maxLength": 200,
                    },
                ],
            },
        },
        {
            "id": "rewrite",
            "type": "agent",
            "position": {"x": 300, "y": 140},
            "data": {
                "name": "分平台改写",
                "goal": (
                    "把下面的原文改写成适合 {{inputs.platforms}} 的版本，"
                    "保留全部事实，分别给出标题与正文：\n\n{{inputs.source}}"
                ),
                "tools": ["profile.*", "content.list"],
                "budget": {"max_steps": 10, "max_tool_calls": 4},
                "retry": 1,
            },
        },
        {
            "id": "out",
            "type": "output",
            "position": {"x": 600, "y": 140},
            "data": {},
        },
    ],
    "edges": [
        {"source": "in", "target": "rewrite"},
        {"source": "rewrite", "target": "out"},
    ],
}

POST_PUBLISH_REVIEW = {
    "nodes": [
        {
            "id": "in",
            "type": "input",
            "position": {"x": 0, "y": 140},
            "data": {
                "name": "复盘窗口",
                "fields": [
                    {"key": "days", "type": "number", "required": True}
                ],
            },
        },
        {
            "id": "summary",
            "type": "tool",
            "position": {"x": 260, "y": 140},
            "data": {
                "name": "拉取数据",
                "tool": "analytics.summary",
                "args": {"days": "{{inputs.days}}"},
                "retry": 1,
            },
        },
        {
            "id": "retro",
            "type": "agent",
            "position": {"x": 540, "y": 140},
            "data": {
                "name": "撰写复盘",
                "goal": (
                    "根据最近 {{inputs.days}} 天的发布数据写一份复盘："
                    "数据亮点、问题、下一步动作。"
                ),
                "tools": ["analytics.summary", "calendar.list", "publish.job.list"],
                "budget": {"max_steps": 10, "max_tool_calls": 4},
                "retry": 1,
            },
        },
        {
            "id": "out",
            "type": "output",
            "position": {"x": 820, "y": 140},
            "data": {},
        },
    ],
    "edges": [
        {"source": "in", "target": "summary"},
        {"source": "summary", "target": "retro"},
        {"source": "retro", "target": "out"},
    ],
}

TEMPLATES: tuple[Template, ...] = (
    Template(
        key="weekly_topic",
        name="每周选题 → 创作 → 审核 → 建草稿",
        description=(
            "按主题让 Agent 选题并写稿，人工审核通过后建立项目草稿。"
            "适合每周固定产出。"
        ),
        parameters=[
            {
                "key": "topic",
                "type": "string",
                "required": True,
                "description": "本周主题或方向",
            }
        ],
        graph=WEEKLY_TOPIC,
    ),
    Template(
        key="multi_platform_rewrite",
        name="批量平台改写",
        description=(
            "把一篇原文改写成多个平台的版本，保留事实并适配语气。"
        ),
        parameters=[
            {
                "key": "source",
                "type": "string",
                "required": True,
                "description": "原文正文",
            },
            {
                "key": "platforms",
                "type": "string",
                "required": False,
                "description": "目标平台，逗号分隔",
            },
        ],
        graph=MULTI_PLATFORM_REWRITE,
    ),
    Template(
        key="post_publish_review",
        name="发布后复盘",
        description=(
            "拉取最近若干天的发布数据，让 Agent 产出复盘与下一步动作。"
        ),
        parameters=[
            {
                "key": "days",
                "type": "number",
                "required": True,
                "description": "回看天数（1–365）",
            }
        ],
        graph=POST_PUBLISH_REVIEW,
    ),
)


def get_template(key: str) -> Template | None:
    for template in TEMPLATES:
        if template.key == key:
            return template
    return None
```

- [ ] **Step 4: 写定义 API**

Create `backend/app/api/workflows.py`:

```python
import copy
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.policy import load_overrides
from app.agent.registry import get_tool_registry
from app.db import get_session
from app.errors import AppError
from app.models.user import User
from app.models.workflow import (
    Workflow,
    WorkflowRun,
    WorkflowSchedule,
    WorkflowVersion,
)
from app.permissions import admin_access, viewer_access
from app.services.audit import record_best_effort
from app.skills.registry import get_registry as get_skill_registry
from app.workflow.templates import TEMPLATES, get_template
from app.workflow.validate import (
    ValidationResult,
    check_structure,
    validate_publish,
)

router = APIRouter(prefix="/workflows", tags=["workflows"])

ACTIVE_RUN_STATUSES = ("queued", "running", "waiting_approval")


class ScheduleOut(BaseModel):
    id: str
    workflow_version_id: str
    cadence: dict
    inputs: dict
    enabled: bool
    next_run_at: datetime | None
    last_run_at: datetime | None


class LastRunOut(BaseModel):
    id: str
    status: str
    created_at: datetime | None


class WorkflowOut(BaseModel):
    id: str
    name: str
    description: str
    created_by: str
    draft_revision: int
    has_unpublished: bool
    latest_version: int | None
    next_run_at: datetime | None
    last_run: LastRunOut | None
    created_at: datetime | None


class VersionOut(BaseModel):
    id: str
    version: int
    source_revision: int
    published_by: str
    published_at: datetime
    note: str


class VersionDetailOut(VersionOut):
    graph: dict


class WorkflowDetailOut(WorkflowOut):
    draft_graph: dict | None
    latest_published: VersionOut | None
    schedules: list[ScheduleOut]


class CreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    graph: dict | None = None


class UpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    draft_graph: dict | None = None


class PublishIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int
    note: str = Field(default="", max_length=500)


class TemplateOut(BaseModel):
    key: str
    name: str
    description: str
    parameters: list[dict]


class InstantiateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)


class ValidateOut(BaseModel):
    ok: bool
    issues: list[dict]


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError):
        raise AppError("workflow_not_found", "工作流不存在", 404) from None


async def _owned(session: AsyncSession, raw: str, user: User) -> Workflow:
    row = (
        await session.execute(
            select(Workflow).where(
                Workflow.id == _parse_id(raw),
                Workflow.tenant_id == user.tenant_id,
                Workflow.archived_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError("workflow_not_found", "工作流不存在", 404)
    return row


async def _latest_version(
    session: AsyncSession, workflow_id: uuid.UUID
) -> WorkflowVersion | None:
    return (
        await session.execute(
            select(WorkflowVersion)
            .where(WorkflowVersion.workflow_id == workflow_id)
            .order_by(WorkflowVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _version_out(row: WorkflowVersion) -> VersionOut:
    return VersionOut(
        id=str(row.id),
        version=row.version,
        source_revision=row.source_revision,
        published_by=str(row.published_by),
        published_at=row.published_at,
        note=row.note,
    )


def _schedule_out(row: WorkflowSchedule) -> ScheduleOut:
    return ScheduleOut(
        id=str(row.id),
        workflow_version_id=str(row.workflow_version_id),
        cadence=row.cadence or {},
        inputs=row.inputs or {},
        enabled=row.enabled,
        next_run_at=row.next_run_at,
        last_run_at=row.last_run_at,
    )


async def _next_run_at(
    session: AsyncSession, workflow_id: uuid.UUID
) -> datetime | None:
    return (
        await session.execute(
            select(func.min(WorkflowSchedule.next_run_at)).where(
                WorkflowSchedule.workflow_id == workflow_id,
                WorkflowSchedule.enabled.is_(True),
            )
        )
    ).scalar_one()


async def _last_run(
    session: AsyncSession, workflow_id: uuid.UUID
) -> WorkflowRun | None:
    return (
        await session.execute(
            select(WorkflowRun)
            .where(WorkflowRun.workflow_id == workflow_id)
            .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _summary(session: AsyncSession, row: Workflow) -> WorkflowOut:
    latest = await _latest_version(session, row.id)
    last = await _last_run(session, row.id)
    return WorkflowOut(
        id=str(row.id),
        name=row.name,
        description=row.description,
        created_by=str(row.created_by),
        draft_revision=row.draft_revision,
        has_unpublished=latest is None
        or row.draft_revision != latest.source_revision,
        latest_version=latest.version if latest is not None else None,
        next_run_at=await _next_run_at(session, row.id),
        last_run=(
            LastRunOut(
                id=str(last.id),
                status=last.status,
                created_at=last.created_at,
            )
            if last is not None
            else None
        ),
        created_at=row.created_at,
    )


async def _detail(session: AsyncSession, row: Workflow) -> WorkflowDetailOut:
    latest = await _latest_version(session, row.id)
    base = await _summary(session, row)
    schedules = (
        (
            await session.execute(
                select(WorkflowSchedule)
                .where(WorkflowSchedule.workflow_id == row.id)
                .order_by(
                    WorkflowSchedule.created_at, WorkflowSchedule.id
                )
            )
        )
        .scalars()
        .all()
    )
    return WorkflowDetailOut(
        **base.model_dump(),
        draft_graph=row.draft_graph,
        latest_published=_version_out(latest) if latest is not None else None,
        schedules=[_schedule_out(item) for item in schedules],
    )


def _issues(result: ValidationResult) -> dict:
    return {"issues": [issue.as_dict() for issue in result.issues]}


def _check_savable(raw: dict) -> None:
    result = check_structure(raw)
    if not result.ok:
        raise AppError(
            "workflow_invalid", "图结构不合法", 422, detail=_issues(result)
        )


async def _validate(
    session: AsyncSession, user: User, raw: Any
) -> ValidationResult:
    return validate_publish(
        raw,
        registry=get_tool_registry(),
        skill_ids=set(get_skill_registry().skills),
        overrides=await load_overrides(session, user.tenant_id),
    )


@router.get("/templates", response_model=list[TemplateOut])
async def list_templates(
    user: User = Depends(admin_access),  # noqa: B008
) -> list[TemplateOut]:
    return [
        TemplateOut(
            key=item.key,
            name=item.name,
            description=item.description,
            parameters=item.parameters,
        )
        for item in TEMPLATES
    ]


@router.post(
    "/templates/{key}/instantiate",
    response_model=WorkflowOut,
    status_code=201,
)
async def instantiate_template(
    key: str,
    payload: InstantiateIn,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> WorkflowOut:
    template = get_template(key)
    if template is None:
        raise AppError("workflow_not_found", "模板不存在", 404)
    name = payload.name.strip()
    if not name:
        raise AppError("workflow_invalid", "名称不能为空", 422)
    row = Workflow(
        tenant_id=user.tenant_id,
        name=name,
        description=template.description,
        created_by=user.id,
        draft_graph=copy.deepcopy(template.graph),
        draft_revision=0,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="workflow.create",
        actor=user,
        resource_type="workflow",
        resource_id=str(row.id),
        detail={"template": key},
    )
    return await _summary(session, row)


@router.post("", response_model=WorkflowOut, status_code=201)
async def create_workflow(
    payload: CreateIn,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> WorkflowOut:
    name = payload.name.strip()
    if not name:
        raise AppError("workflow_invalid", "名称不能为空", 422)
    if payload.graph is not None:
        _check_savable(payload.graph)
    row = Workflow(
        tenant_id=user.tenant_id,
        name=name,
        description=payload.description,
        created_by=user.id,
        draft_graph=payload.graph,
        draft_revision=0,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="workflow.create",
        actor=user,
        resource_type="workflow",
        resource_id=str(row.id),
    )
    return await _summary(session, row)


@router.get("", response_model=list[WorkflowOut])
async def list_workflows(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[WorkflowOut]:
    rows = (
        (
            await session.execute(
                select(Workflow)
                .where(
                    Workflow.tenant_id == user.tenant_id,
                    Workflow.archived_at.is_(None),
                )
                .order_by(Workflow.created_at.desc(), Workflow.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return [await _summary(session, row) for row in rows]


@router.get("/{workflow_id}", response_model=WorkflowDetailOut)
async def get_workflow(
    workflow_id: str,
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> WorkflowDetailOut:
    return await _detail(session, await _owned(session, workflow_id, user))


@router.patch("/{workflow_id}", response_model=WorkflowDetailOut)
async def update_workflow(
    workflow_id: str,
    payload: UpdateIn,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> WorkflowDetailOut:
    row = await _owned(session, workflow_id, user)
    if payload.revision != row.draft_revision:
        raise AppError(
            "workflow_conflict", "草稿已被其他会话修改，请重新加载", 409
        )
    changed: list[str] = []
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise AppError("workflow_invalid", "名称不能为空", 422)
        row.name = name
        changed.append("name")
    if payload.description is not None:
        row.description = payload.description
        changed.append("description")
    if payload.draft_graph is not None:
        _check_savable(payload.draft_graph)
        row.draft_graph = payload.draft_graph
        changed.append("graph")
    row.draft_revision += 1
    await session.commit()
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="workflow.update",
        actor=user,
        resource_type="workflow",
        resource_id=str(row.id),
        detail={"fields": changed, "revision": row.draft_revision},
    )
    return await _detail(session, row)


@router.delete("/{workflow_id}", status_code=204)
async def delete_workflow(
    workflow_id: str,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> None:
    row = await _owned(session, workflow_id, user)
    active = (
        await session.execute(
            select(WorkflowRun.id)
            .where(
                WorkflowRun.workflow_id == row.id,
                WorkflowRun.status.in_(ACTIVE_RUN_STATUSES),
            )
            .limit(1)
        )
    ).first()
    if active is not None:
        raise AppError(
            "workflow_run_active", "工作流仍有活跃运行，无法删除", 409
        )
    row.archived_at = _now()
    await session.commit()
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="workflow.update",
        actor=user,
        resource_type="workflow",
        resource_id=str(row.id),
        detail={"archived": True},
    )


@router.post("/{workflow_id}/validate", response_model=ValidateOut)
async def validate_workflow(
    workflow_id: str,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ValidateOut:
    row = await _owned(session, workflow_id, user)
    if row.draft_graph is None:
        return ValidateOut(
            ok=False,
            issues=[
                {
                    "code": "graph_invalid",
                    "message": "草稿为空",
                    "node_id": "",
                    "severity": "error",
                }
            ],
        )
    result = await _validate(session, user, row.draft_graph)
    return ValidateOut(**result.as_dict())


@router.post(
    "/{workflow_id}/publish", response_model=VersionOut, status_code=201
)
async def publish_workflow(
    workflow_id: str,
    payload: PublishIn,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> VersionOut:
    row = await _owned(session, workflow_id, user)
    if payload.revision != row.draft_revision:
        raise AppError(
            "workflow_conflict", "草稿已被其他会话修改，请重新加载", 409
        )
    if row.draft_graph is None:
        raise AppError("workflow_invalid", "草稿为空，无法发布", 422)
    result = await _validate(session, user, row.draft_graph)
    if not result.ok:
        raise AppError(
            "workflow_invalid", "工作流校验未通过", 422, detail=_issues(result)
        )
    latest = await _latest_version(session, row.id)
    version = WorkflowVersion(
        tenant_id=row.tenant_id,
        workflow_id=row.id,
        version=(latest.version + 1) if latest is not None else 1,
        graph=copy.deepcopy(row.draft_graph),
        source_revision=row.draft_revision,
        published_by=user.id,
        published_at=_now(),
        note=payload.note,
    )
    session.add(version)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise AppError(
            "workflow_conflict", "有并发发布，请重试", 409
        ) from None
    await session.refresh(version)
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="workflow.publish",
        actor=user,
        resource_type="workflow",
        resource_id=str(row.id),
        detail={"version": version.version},
    )
    return _version_out(version)


@router.get("/{workflow_id}/versions", response_model=list[VersionOut])
async def list_versions(
    workflow_id: str,
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[VersionOut]:
    row = await _owned(session, workflow_id, user)
    rows = (
        (
            await session.execute(
                select(WorkflowVersion)
                .where(WorkflowVersion.workflow_id == row.id)
                .order_by(WorkflowVersion.version.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_version_out(item) for item in rows]


@router.get(
    "/{workflow_id}/versions/{version}",
    response_model=VersionDetailOut,
)
async def get_version(
    workflow_id: str,
    version: int,
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> VersionDetailOut:
    row = await _owned(session, workflow_id, user)
    found = (
        await session.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == row.id,
                WorkflowVersion.version == version,
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise AppError("workflow_not_found", "版本不存在", 404)
    return VersionDetailOut(
        **_version_out(found).model_dump(), graph=found.graph
    )


@router.post(
    "/{workflow_id}/versions/{version}/restore",
    response_model=WorkflowDetailOut,
)
async def restore_version(
    workflow_id: str,
    version: int,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> WorkflowDetailOut:
    row = await _owned(session, workflow_id, user)
    found = (
        await session.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == row.id,
                WorkflowVersion.version == version,
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise AppError("workflow_not_found", "版本不存在", 404)
    row.draft_graph = copy.deepcopy(found.graph)
    row.draft_revision += 1
    await session.commit()
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="workflow.update",
        actor=user,
        resource_type="workflow",
        resource_id=str(row.id),
        detail={"restored": found.version},
    )
    return await _detail(session, row)
```

- [ ] **Step 5: 加只读工具清单**

在 `backend/app/api/agent.py` 的 `PolicyOut` 之后加：

```python
class ToolOut(BaseModel):
    name: str
    description: str
    parameters: dict
    risk: str
```

在 `@router.get("/policy", response_model=PolicyOut)` 之前加：

```python
@router.get("/tools", response_model=list[ToolOut])
async def list_tools(
    user: User = Depends(editor_access),  # noqa: B008
) -> list[ToolOut]:
    return [
        ToolOut(
            name=spec.name,
            description=spec.description,
            parameters=spec.input_schema,
            risk=spec.risk,
        )
        for spec in get_tool_registry().all()
    ]
```

- [ ] **Step 6: 注册路由**

在 `backend/app/main.py` 的 `from app.api import (...)` 列表中加入 `workflows`（按字母序放在 `tenants` 之后）：

```python
from app.api import (
    accounts,
    agent,
    analytics,
    audit,
    auth,
    calendar,
    chat,
    members,
    profiles,
    projects,
    publish,
    skills,
    tenants,
    workflows,
)
```

在 `app.include_router(settings_api.router, prefix="/api")` 之后加：

```python
app.include_router(workflows.router, prefix="/api")
```

- [ ] **Step 7: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_workflow_api.py -q`
Expected: PASS（10 passed）。

Run: `cd backend && uv run pytest tests/test_agent_api.py -q`
Expected: PASS（既有用例不回归）。

Run: `cd backend && uv run ruff check app/api/workflows.py app/workflow/templates.py app/api/agent.py app/main.py`
Expected: `All checks passed!`

- [ ] **Step 8: 提交**

```bash
git add backend/app/workflow/templates.py backend/app/api/workflows.py backend/app/api/agent.py backend/app/main.py backend/tests/test_workflow_api.py
git commit -m "feat(workflow): add definition, version and template APIs"
```

---

## Task 10: 定时管理 API 与运行入参校验

**Files:**
- Modify: `backend/app/workflow/validate.py`（追加 `validate_inputs`）
- Modify: `backend/app/api/workflows.py`（定时 CRUD）
- Test: `backend/tests/test_workflow_api.py`（追加用例）

**背景与接口：** 定时只能绑定已发布版本：不传 `version` 取最新发布版本，没有发布版本直接 409。cadence 由 Task 4 的 `parse_cadence` 解析并规整成 JSON；`next_run_at` 由 `next_run_at(cadence, now)` 计算。入参在创建/修改定时时按该版本的输入节点字段校验，错误统一 `workflow_schedule_invalid`。

**关键约定（实现时照抄）：**
- 定时只允许改 `cadence` / `inputs` / `enabled`；`version` 只能在创建时指定。
- 每次变更后重算 `next_run_at`：启用 → 下一个触发时刻；停用 → `None`（worker 只扫 `enabled=true` 且 `next_run_at<=now`）。
- `WorkflowRun.schedule_id` 是 `ON DELETE SET NULL`，删除定时不删运行历史。
- 入参校验按输入节点 `fields`：未知键、必填缺失、类型不符、超过 `maxLength` 都算错误；未声明的可选字段允许缺省。
- 写接口审计动作 `workflow.schedule.create/update/delete`。

- [ ] **Step 1: 追加失败测试**

在 `backend/tests/test_workflow_api.py` 末尾追加：

```python
WEEKLY = {"kind": "weekly", "weekdays": [1, 4], "time": "08:30", "timezone": "Asia/Shanghai"}
DAILY = {"kind": "daily", "time": "09:00", "timezone": "Asia/Shanghai"}


async def publish_once(http, workflow_id, revision=0):
    return await http.post(
        f"/api/workflows/{workflow_id}/publish", json={"revision": revision}
    )


async def test_schedule_requires_published_version(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id = (await create_via_api(http)).json()["id"]

    resp = await http.post(
        f"/api/workflows/{workflow_id}/schedules",
        json={"cadence": DAILY, "inputs": {"days": 7}},
    )

    assert resp.status_code == 409
    assert resp.json()["code"] == "workflow_not_published"


async def test_schedule_crud_and_next_run(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id = (await create_via_api(http)).json()["id"]
    assert (await publish_once(http, workflow_id)).status_code == 201

    created = await http.post(
        f"/api/workflows/{workflow_id}/schedules",
        json={"cadence": DAILY, "inputs": {"days": 7}},
    )

    assert created.status_code == 201, created.text
    schedule = created.json()
    assert schedule["enabled"] is True
    assert schedule["next_run_at"] is not None
    assert schedule["cadence"]["kind"] == "daily"

    listed = await http.get(f"/api/workflows/{workflow_id}/schedules")
    assert [item["id"] for item in listed.json()] == [schedule["id"]]

    disabled = await http.patch(
        f"/api/workflows/{workflow_id}/schedules/{schedule['id']}",
        json={"cadence": WEEKLY, "enabled": False},
    )

    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False
    assert disabled.json()["next_run_at"] is None
    assert disabled.json()["cadence"]["weekdays"] == [1, 4]

    enabled = await http.patch(
        f"/api/workflows/{workflow_id}/schedules/{schedule['id']}",
        json={"enabled": True},
    )
    assert enabled.status_code == 200
    assert enabled.json()["next_run_at"] is not None

    bad_cadence = await http.patch(
        f"/api/workflows/{workflow_id}/schedules/{schedule['id']}",
        json={"cadence": {"kind": "hourly", "time": "09:00", "timezone": "UTC"}},
    )
    assert bad_cadence.status_code == 422
    assert bad_cadence.json()["code"] == "workflow_schedule_invalid"

    bad_inputs = await http.patch(
        f"/api/workflows/{workflow_id}/schedules/{schedule['id']}",
        json={"inputs": {"days": "七天"}},
    )
    assert bad_inputs.status_code == 422
    assert bad_inputs.json()["code"] == "workflow_schedule_invalid"
    assert bad_inputs.json()["detail"]["issues"]

    deleted = await http.delete(
        f"/api/workflows/{workflow_id}/schedules/{schedule['id']}"
    )
    assert deleted.status_code == 204
    assert (await http.get(f"/api/workflows/{workflow_id}/schedules")).json() == []


async def test_schedule_role_matrix_and_tenant_isolation(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id = (await create_via_api(http)).json()["id"]
    assert (await publish_once(http, workflow_id)).status_code == 201

    state["user"] = await make_user(db_session, role="editor")
    denied = await http.post(
        f"/api/workflows/{workflow_id}/schedules",
        json={"cadence": DAILY, "inputs": {"days": 7}},
    )
    assert denied.status_code == 403
    assert (
        await http.get(f"/api/workflows/{workflow_id}/schedules")
    ).status_code == 200

    foreign = Workflow(
        tenant_id=OTHER_TENANT,
        name="别人的流",
        created_by=uuid.uuid4(),
        draft_graph=graph(),
    )
    db_session.add(foreign)
    await db_session.commit()
    await db_session.refresh(foreign)

    resp = await http.get(f"/api/workflows/{foreign.id}/schedules")
    assert resp.status_code == 404
    assert resp.json()["code"] == "workflow_not_found"


async def test_schedule_pins_the_requested_version(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id = (await create_via_api(http)).json()["id"]
    assert (await publish_once(http, workflow_id)).status_code == 201
    saved = await http.patch(
        f"/api/workflows/{workflow_id}",
        json={"revision": 0, "draft_graph": graph(days=30)},
    )
    assert saved.status_code == 200
    assert (
        await publish_once(http, workflow_id, revision=1)
    ).status_code == 201

    v1 = await http.get(f"/api/workflows/{workflow_id}/versions/1")

    created = await http.post(
        f"/api/workflows/{workflow_id}/schedules",
        json={"cadence": DAILY, "inputs": {"days": 7}, "version": 1},
    )

    assert created.status_code == 201, created.text
    assert created.json()["workflow_version_id"] == v1.json()["id"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_workflow_api.py -q`
Expected: FAIL（定时的 4 个用例 404/405，`validate_inputs` 尚不存在）。

- [ ] **Step 3: 追加运行入参校验**

在 `backend/app/workflow/validate.py` 的 `validate_publish` 之后追加：

```python
def validate_inputs(graph_raw: Any, inputs: Any) -> list[Issue]:
    """按输入节点的 fields 校验运行入参；空列表表示通过。"""
    if not isinstance(inputs, dict):
        return [Issue("input_invalid", "入参必须是对象")]
    try:
        graph = parse_graph(graph_raw)
    except GraphError as exc:
        return [Issue(exc.code, exc.message)]
    nodes = [node for node in graph.nodes.values() if node.kind == "input"]
    if len(nodes) != 1:
        return [Issue("input_invalid", "工作流必须恰好有一个输入节点")]
    fields = nodes[0].data.get("fields")
    if not isinstance(fields, list):
        return [Issue("input_invalid", "输入节点缺少字段定义")]
    declared = {
        item.get("key"): item
        for item in fields
        if isinstance(item, dict) and isinstance(item.get("key"), str)
    }
    issues: list[Issue] = []
    for key in inputs:
        if key not in declared:
            issues.append(Issue("input_invalid", f"未知入参: {key}"))
    for key, item in declared.items():
        if key not in inputs or inputs[key] is None:
            if item.get("required"):
                issues.append(Issue("input_invalid", f"缺少必填入参: {key}"))
            continue
        value = inputs[key]
        expected = item.get("type")
        if expected == "string":
            if not isinstance(value, str):
                issues.append(Issue("input_invalid", f"入参 {key} 必须是字符串"))
                continue
            max_length = item.get("maxLength")
            if (
                isinstance(max_length, int)
                and max_length > 0
                and len(value) > max_length
            ):
                issues.append(
                    Issue("input_invalid", f"入参 {key} 超过 {max_length} 字")
                )
        elif expected == "number" and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            issues.append(Issue("input_invalid", f"入参 {key} 必须是数字"))
        elif expected == "boolean" and not isinstance(value, bool):
            issues.append(Issue("input_invalid", f"入参 {key} 必须是布尔值"))
    return issues
```

`validate.py` 顶部已经 `from app.workflow.graph import GraphError, parse_graph`，不需要新增导入。

- [ ] **Step 4: 追加定时 API**

在 `backend/app/api/workflows.py` 顶部补充导入：

```python
from app.workflow.schedule import next_run_at, parse_cadence
from app.workflow.validate import validate_inputs
```

（与既有 `from app.workflow.validate import (...)` 合并成一次导入即可，最终形如：

```python
from app.workflow.schedule import next_run_at, parse_cadence
from app.workflow.validate import (
    ValidationResult,
    check_structure,
    validate_inputs,
    validate_publish,
)
```

）

在 `InstantiateIn` 之后加模型：

```python
class ScheduleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cadence: dict
    inputs: dict = Field(default_factory=dict)
    enabled: bool = True
    version: int | None = None


class SchedulePatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cadence: dict | None = None
    inputs: dict | None = None
    enabled: bool | None = None
```

在 `_validate` 之后加辅助函数：

```python
async def _version_by_number(
    session: AsyncSession, workflow_id: uuid.UUID, version: int
) -> WorkflowVersion | None:
    return (
        await session.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == workflow_id,
                WorkflowVersion.version == version,
            )
        )
    ).scalar_one_or_none()


def _check_inputs(graph_raw: Any, inputs: dict) -> None:
    issues = validate_inputs(graph_raw, inputs)
    if issues:
        raise AppError(
            "workflow_schedule_invalid",
            "入参与工作流不匹配",
            422,
            detail={"issues": [issue.as_dict() for issue in issues]},
        )


async def _owned_schedule(
    session: AsyncSession,
    workflow: Workflow,
    schedule_id: str,
    user: User,
) -> WorkflowSchedule:
    row = (
        await session.execute(
            select(WorkflowSchedule).where(
                WorkflowSchedule.id == _parse_id(schedule_id),
                WorkflowSchedule.workflow_id == workflow.id,
                WorkflowSchedule.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError("workflow_not_found", "定时不存在", 404)
    return row
```

在 `get_version` 之前加四个定时端点（放在定义端点之后、`/{workflow_id}/versions/{version}` 之前或之后均可，路径不冲突）：

```python
@router.get(
    "/{workflow_id}/schedules", response_model=list[ScheduleOut]
)
async def list_schedules(
    workflow_id: str,
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[ScheduleOut]:
    row = await _owned(session, workflow_id, user)
    rows = (
        (
            await session.execute(
                select(WorkflowSchedule)
                .where(WorkflowSchedule.workflow_id == row.id)
                .order_by(
                    WorkflowSchedule.created_at, WorkflowSchedule.id
                )
            )
        )
        .scalars()
        .all()
    )
    return [_schedule_out(item) for item in rows]


@router.post(
    "/{workflow_id}/schedules",
    response_model=ScheduleOut,
    status_code=201,
)
async def create_schedule(
    workflow_id: str,
    payload: ScheduleIn,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ScheduleOut:
    row = await _owned(session, workflow_id, user)
    if payload.version is None:
        version = await _latest_version(session, row.id)
        if version is None:
            raise AppError(
                "workflow_not_published",
                "工作流尚未发布，无法创建定时",
                409,
            )
    else:
        version = await _version_by_number(
            session, row.id, payload.version
        )
        if version is None:
            raise AppError("workflow_not_found", "版本不存在", 404)
    cadence = parse_cadence(payload.cadence)
    _check_inputs(version.graph, payload.inputs)
    schedule = WorkflowSchedule(
        tenant_id=row.tenant_id,
        workflow_id=row.id,
        workflow_version_id=version.id,
        cadence=cadence.to_json(),
        inputs=payload.inputs,
        enabled=payload.enabled,
        next_run_at=next_run_at(cadence, _now()) if payload.enabled else None,
        created_by=user.id,
    )
    session.add(schedule)
    await session.commit()
    await session.refresh(schedule)
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="workflow.schedule.create",
        actor=user,
        resource_type="workflow",
        resource_id=str(row.id),
        detail={"schedule": str(schedule.id), "version": version.version},
    )
    return _schedule_out(schedule)


@router.patch(
    "/{workflow_id}/schedules/{schedule_id}", response_model=ScheduleOut
)
async def update_schedule(
    workflow_id: str,
    schedule_id: str,
    payload: SchedulePatchIn,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ScheduleOut:
    row = await _owned(session, workflow_id, user)
    schedule = await _owned_schedule(session, row, schedule_id, user)
    if payload.cadence is not None:
        schedule.cadence = parse_cadence(payload.cadence).to_json()
    if payload.inputs is not None:
        version = await session.get(
            WorkflowVersion, schedule.workflow_version_id
        )
        graph_raw = version.graph if version is not None else {}
        _check_inputs(graph_raw, payload.inputs)
        schedule.inputs = payload.inputs
    if payload.enabled is not None:
        schedule.enabled = payload.enabled
    cadence = parse_cadence(schedule.cadence)
    schedule.next_run_at = (
        next_run_at(cadence, _now()) if schedule.enabled else None
    )
    await session.commit()
    await session.refresh(schedule)
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="workflow.schedule.update",
        actor=user,
        resource_type="workflow",
        resource_id=str(row.id),
        detail={"schedule": str(schedule.id)},
    )
    return _schedule_out(schedule)


@router.delete(
    "/{workflow_id}/schedules/{schedule_id}", status_code=204
)
async def delete_schedule(
    workflow_id: str,
    schedule_id: str,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> None:
    row = await _owned(session, workflow_id, user)
    schedule = await _owned_schedule(session, row, schedule_id, user)
    await session.delete(schedule)
    await session.commit()
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="workflow.schedule.delete",
        actor=user,
        resource_type="workflow",
        resource_id=str(row.id),
        detail={"schedule": schedule_id},
    )
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_workflow_api.py -q`
Expected: PASS（14 passed）。

Run: `cd backend && uv run pytest tests/test_workflow_schedule.py tests/test_workflow_validate.py -q`
Expected: PASS（既有 23 + 10 不回归）。

Run: `cd backend && uv run ruff check app/api/workflows.py app/workflow/validate.py`
Expected: `All checks passed!`

- [ ] **Step 6: 提交**

```bash
git add backend/app/api/workflows.py backend/app/workflow/validate.py backend/tests/test_workflow_api.py
git commit -m "feat(workflow): add schedule management API"
```

---

## Task 11: 运行与审批 API

**Files:**
- Create: `backend/app/api/workflow_runs.py`
- Modify: `backend/app/main.py`（注册 `workflow_runs` 路由）
- Test: `backend/tests/test_workflow_api.py`（追加用例）

**背景与接口：** 运行由 API 创建为 `queued` 并入队 `run_workflow_job`（worker 在 Task 12 注册）；同一幂等键重放返回同一运行，键被他人占用时 `409 workflow_conflict`（Ruling 4）。入参按所绑定版本的输入节点校验，入队失败把运行落为 `failed/queue_unavailable` 并释放幂等键。审批决策复用 Task 5 的 `decide_approval`：通过后入队，入队失败把审批与运行一起回滚为可再次决策的状态；驳回是终态，不再入队。

**关键约定（实现时照抄）：**
- 路由用不带 prefix 的 `APIRouter(tags=["workflows"])` 声明完整路径：`/workflows/{workflow_id}/runs`、`/workflow-runs/{run_id}`、`/workflow-approvals`。`main.py` 统一加 `prefix="/api"`。
- 运行权限：创建 = `editor_access`；读 = `viewer_access`；取消/续跑 = 发起人本人，或定时运行的 admin/owner（`user.role in ("owner", "admin")`）；审批 = `admin_access`，但手动运行的发起人不能审批自己的运行（403）。
- 活跃上限只数当前用户的 `queued`/`running`（Ruling 13）；`waiting_approval` 不占名额。配额用 Task 7 的 `ensure_daily_quota`，超额 429。
- 版本选择：不传 `version` 取最新发布版本，没有 → `409 workflow_not_published`；显式版本不存在 → `404 workflow_not_found`。运行永远绑定创建时的版本，之后发布新版本不影响它。
- 跨租户、运行不存在统一 `workflow_run_not_found` 404；跨租户审批统一 `workflow_not_found` 404；工作流不存在/已归档仍是 `workflow_not_found` 404。
- `ApprovalOut` 在规格字段之外追加 `run_id`/`workflow_id`/`risk`/`note`/`created_at`，供审批列表直接跳转运行详情；`raw_output` 仍然绝不出现在响应里，节点只回 `input`/`output`。
- `GET /workflow-approvals` 只列工作流通道的待处理审批：手动运行排除发起人自己的，定时运行（`trigger == "schedule"`）对全部管理员可见。
- 入队失败补偿只在该行仍处于本次写入的状态时生效（`queued` → `waiting_approval`、`approved` → `pending`、续跑 `queued` → `failed`），并发取消/过期不会被“复活”。

- [ ] **Step 1: 追加失败测试**

把 `backend/tests/test_workflow_api.py` 顶部的导入替换为：

```python
import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api import workflow_runs as workflow_runs_api
from app.config import Settings
from app.db import get_session
from app.deps import get_current_user
from app.main import app
from app.models.agent import AgentApproval, AgentRun, AgentStep
from app.models.base import DEFAULT_TENANT_ID
from app.models.user import User
from app.models.workflow import (
    Workflow,
    WorkflowApproval,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowVersion,
)
from app.queue import get_queue
from app.workflow import approvals as workflow_approvals
```

在文件末尾追加：

```python
class FakeQueue:
    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, name, *args):
        self.jobs.append((name, *args))


class DownQueue:
    async def enqueue_job(self, name, *args):
        raise RuntimeError("redis down")


@pytest.fixture
def queue():
    fake = FakeQueue()
    app.dependency_overrides[get_queue] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_queue, None)


@pytest.fixture
def down_queue():
    app.dependency_overrides[get_queue] = lambda: DownQueue()
    yield
    app.dependency_overrides.pop(get_queue, None)


async def published(http, *, name="数据复盘", body=None):
    workflow_id = (await create_via_api(http, name=name, body=body)).json()["id"]
    assert (await publish_once(http, workflow_id)).status_code == 201
    version = (
        await http.get(f"/api/workflows/{workflow_id}/versions/1")
    ).json()
    return workflow_id, version["id"]


async def start_run(http, workflow_id, *, inputs=None, version=None, key=None):
    headers = {"Idempotency-Key": key} if key else {}
    payload = {"version": version}
    if inputs is not None:
        payload["inputs"] = inputs
    else:
        payload["inputs"] = {"days": 7}
    return await http.post(
        f"/api/workflows/{workflow_id}/runs", json=payload, headers=headers
    )


async def park(db_session, run_id, *, node_id="review", title="请确认草稿"):
    run = await db_session.get(WorkflowRun, uuid.UUID(run_id))
    run.status = "waiting_approval"
    db_session.add(
        WorkflowNodeRun(
            tenant_id=run.tenant_id,
            run_id=run.id,
            node_id=node_id,
            kind="review",
            status="waiting",
        )
    )
    approval = await workflow_approvals.create_approval(
        db_session,
        run=run,
        node_id=node_id,
        kind="review",
        title=title,
        payload={"summary": "草稿"},
    )
    await db_session.commit()
    await db_session.refresh(approval)
    return run, approval


async def add_child_pending(db_session, run: WorkflowRun) -> AgentRun:
    child = AgentRun(
        tenant_id=run.tenant_id,
        user_id=uuid.uuid4(),
        goal="节点任务",
        mode="node",
        status="waiting_approval",
        workflow_run_id=run.id,
        workflow_node_id="agent",
        budget={},
        policy={},
    )
    db_session.add(child)
    await db_session.flush()
    step = AgentStep(
        tenant_id=run.tenant_id, run_id=child.id, step_index=0, kind="tool"
    )
    db_session.add(step)
    await db_session.flush()
    db_session.add(
        AgentApproval(
            tenant_id=run.tenant_id,
            run_id=child.id,
            step_id=step.id,
            tool_name="content.create_version",
            risk="high",
            payload={},
        )
    )
    await db_session.commit()
    return child


async def scheduled_run(
    db_session, workflow_id: str, version_id: str, *, status="queued"
):
    run = WorkflowRun(
        tenant_id=DEFAULT_TENANT_ID,
        workflow_id=uuid.UUID(workflow_id),
        workflow_version_id=uuid.UUID(version_id),
        trigger="schedule",
        triggered_by=None,
        scheduled_for=datetime.now(UTC),
        status=status,
        inputs={"days": 7},
        policy={},
    )
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)
    return run


async def foreign_run(db_session):
    workflow = Workflow(
        tenant_id=OTHER_TENANT, name="别人的流", created_by=uuid.uuid4()
    )
    db_session.add(workflow)
    await db_session.flush()
    version = WorkflowVersion(
        tenant_id=OTHER_TENANT,
        workflow_id=workflow.id,
        version=1,
        graph=graph(),
        source_revision=0,
        published_by=uuid.uuid4(),
        published_at=datetime.now(UTC),
    )
    db_session.add(version)
    await db_session.flush()
    run = WorkflowRun(
        tenant_id=OTHER_TENANT,
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        trigger="manual",
        status="waiting_approval",
        inputs={},
        policy={},
    )
    db_session.add(run)
    await db_session.flush()
    approval = await workflow_approvals.create_approval(
        db_session,
        run=run,
        node_id="review",
        kind="review",
        title="别人的审批",
        payload={},
    )
    await db_session.commit()
    await db_session.refresh(approval)
    return run, approval


async def test_start_run_pins_published_version_and_enqueues(api, queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id, version_id = await published(http)
    editor = await make_user(db_session, role="editor")
    state["user"] = editor

    resp = await start_run(http, workflow_id)

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert body["trigger"] == "manual"
    assert body["triggered_by"] == str(editor.id)
    assert body["version"] == 1
    assert body["workflow_version_id"] == version_id
    assert body["inputs"] == {"days": 7}
    assert queue.jobs == [("run_workflow_job", body["id"])]

    detail = await http.get(f"/api/workflow-runs/{body['id']}")

    assert detail.status_code == 200, detail.text
    assert detail.json()["run"]["id"] == body["id"]
    assert detail.json()["node_runs"] == []
    assert detail.json()["approvals"] == []
    assert detail.json()["result"] == {}


async def test_start_run_validates_inputs_before_creating(api, queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    unpublished = (await create_via_api(http, name="未发布")).json()["id"]
    state["user"] = await make_user(db_session, role="editor")

    missing = await start_run(http, unpublished)

    assert missing.status_code == 409
    assert missing.json()["code"] == "workflow_not_published"

    state["user"] = await make_user(db_session, role="admin")
    workflow_id, _ = await published(http)
    state["user"] = await make_user(db_session, role="editor")

    wrong_type = await start_run(http, workflow_id, inputs={"days": "七天"})
    assert wrong_type.status_code == 422
    assert wrong_type.json()["code"] == "workflow_invalid"
    assert wrong_type.json()["detail"]["issues"]

    absent = await start_run(http, workflow_id, inputs={})
    assert absent.status_code == 422

    unknown = await start_run(http, workflow_id, inputs={"days": 7, "x": 1})
    assert unknown.status_code == 422

    assert queue.jobs == []


async def test_start_run_is_idempotent_per_user(api, queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id, _ = await published(http)
    first_user = await make_user(db_session, role="editor")
    state["user"] = first_user

    first = await start_run(http, workflow_id, key="same-key")
    second = await start_run(http, workflow_id, key="same-key")

    assert first.status_code == 202, first.text
    assert second.status_code == 202
    assert second.json()["id"] == first.json()["id"]
    assert queue.jobs == [("run_workflow_job", first.json()["id"])]

    state["user"] = await make_user(db_session, role="editor")
    other = await start_run(http, workflow_id, key="same-key")

    assert other.status_code == 409
    assert other.json()["code"] == "workflow_conflict"


async def test_start_run_enforces_active_limit(api, queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id, _ = await published(http)
    state["user"] = await make_user(db_session, role="editor")

    first = await start_run(http, workflow_id)
    second = await start_run(http, workflow_id)
    assert first.status_code == 202
    assert second.status_code == 202

    blocked = await start_run(http, workflow_id)

    assert blocked.status_code == 409
    assert blocked.json()["code"] == "workflow_run_limit"

    row = await db_session.get(WorkflowRun, uuid.UUID(first.json()["id"]))
    row.status = "succeeded"
    await db_session.commit()

    assert (await start_run(http, workflow_id)).status_code == 202


async def test_start_run_checks_daily_quota(api, queue, db_session, monkeypatch):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id, _ = await published(http)
    editor = await make_user(db_session, role="editor")
    state["user"] = editor
    db_session.add(
        AgentRun(
            tenant_id=DEFAULT_TENANT_ID,
            user_id=editor.id,
            goal="消耗配额",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )
    )
    await db_session.commit()
    monkeypatch.setattr(
        workflow_runs_api,
        "get_settings",
        lambda: Settings(agent_daily_token_quota=1),
    )

    resp = await start_run(http, workflow_id)

    assert resp.status_code == 429
    assert resp.json()["code"] == "agent_daily_quota_exceeded"
    assert queue.jobs == []


async def test_start_queue_failure_fails_run_and_releases_key(
    api, down_queue, db_session
):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id, _ = await published(http)
    state["user"] = await make_user(db_session, role="editor")

    first = await start_run(http, workflow_id, key="retry-me")

    assert first.status_code == 503
    assert first.json()["code"] == "queue_unavailable"
    rows = (
        (await db_session.execute(select(WorkflowRun))).scalars().all()
    )
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].error_code == "queue_unavailable"
    assert rows[0].idempotency_key is None

    again = await start_run(http, workflow_id, key="retry-me")

    assert again.status_code == 503
    total = (await db_session.execute(select(WorkflowRun))).scalars().all()
    assert len(total) == 2


async def test_run_detail_aggregates_nodes_and_both_channels(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id, _ = await published(http)
    state["user"] = await make_user(db_session, role="editor")
    run = (await start_run(http, workflow_id)).json()
    row, _ = await park(db_session, run["id"])
    await add_child_pending(db_session, row)

    detail = await http.get(f"/api/workflow-runs/{run['id']}")

    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert len(body["node_runs"]) == 1
    assert body["node_runs"][0]["node_id"] == "review"
    assert body["node_runs"][0]["status"] == "waiting"
    sources = {item["source"] for item in body["approvals"]}
    assert sources == {"workflow", "agent"}
    assert all(item["run_id"] == run["id"] for item in body["approvals"])
    assert "raw_output" not in detail.text

    _, foreign = await foreign_run(db_session)
    foreign_detail = await http.get(f"/api/workflow-runs/{foreign.run_id}")

    assert foreign_detail.status_code == 404
    assert foreign_detail.json()["code"] == "workflow_run_not_found"


async def test_run_list_filters_by_status(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id, _ = await published(http)
    state["user"] = await make_user(db_session, role="editor")
    first = (await start_run(http, workflow_id)).json()
    second = (await start_run(http, workflow_id)).json()
    row = await db_session.get(WorkflowRun, uuid.UUID(second["id"]))
    row.status = "failed"
    await db_session.commit()

    failed = await http.get(f"/api/workflows/{workflow_id}/runs?status=failed")
    all_runs = await http.get(f"/api/workflows/{workflow_id}/runs")

    assert failed.status_code == 200
    assert [item["id"] for item in failed.json()] == [second["id"]]
    assert {item["id"] for item in all_runs.json()} == {
        first["id"],
        second["id"],
    }


async def test_cancel_run_permissions_and_terminal_state(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id, version_id = await published(http)
    owner = await make_user(db_session, role="editor")
    state["user"] = owner
    run = (await start_run(http, workflow_id)).json()

    state["user"] = await make_user(db_session, role="editor")
    denied = await http.post(f"/api/workflow-runs/{run['id']}/cancel")
    assert denied.status_code == 403

    state["user"] = owner
    cancelled = await http.post(f"/api/workflow-runs/{run['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"

    again = await http.post(f"/api/workflow-runs/{run['id']}/cancel")
    assert again.status_code == 409
    assert again.json()["code"] == "workflow_run_not_cancellable"

    scheduled = await scheduled_run(db_session, workflow_id, version_id)
    state["user"] = await make_user(db_session, role="editor")
    assert (
        await http.post(f"/api/workflow-runs/{scheduled.id}/cancel")
    ).status_code == 403

    state["user"] = await make_user(db_session, role="admin")
    admin_cancel = await http.post(f"/api/workflow-runs/{scheduled.id}/cancel")
    assert admin_cancel.status_code == 200
    assert admin_cancel.json()["status"] == "cancelled"


async def test_resume_requires_failed_and_enqueues(api, queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id, _ = await published(http)
    owner = await make_user(db_session, role="editor")
    state["user"] = owner
    run = (await start_run(http, workflow_id)).json()
    row = await db_session.get(WorkflowRun, uuid.UUID(run["id"]))
    row.status = "failed"
    row.error_code = "node_failed"
    await db_session.commit()

    queued_run = await db_session.get(WorkflowRun, uuid.UUID(run["id"]))
    queued_run.status = "queued"
    await db_session.commit()
    not_failed = await http.post(f"/api/workflow-runs/{run['id']}/resume")
    assert not_failed.status_code == 409
    assert not_failed.json()["code"] == "workflow_run_not_resumable"

    queued_run.status = "failed"
    await db_session.commit()
    resumed = await http.post(f"/api/workflow-runs/{run['id']}/resume")

    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "queued"
    assert resumed.json()["resume_count"] == 1
    assert queue.jobs[-1] == ("run_workflow_job", run["id"])

    state["user"] = await make_user(db_session, role="editor")
    denied = await http.post(f"/api/workflow-runs/{run['id']}/resume")
    assert denied.status_code == 403


async def test_resume_queue_failure_rolls_back_to_failed(api, down_queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id, _ = await published(http)
    state["user"] = await make_user(db_session, role="editor")
    run = (await start_run(http, workflow_id)).json()
    row = await db_session.get(WorkflowRun, uuid.UUID(run["id"]))
    row.status = "failed"
    await db_session.commit()

    resp = await http.post(f"/api/workflow-runs/{run['id']}/resume")

    assert resp.status_code == 503
    await db_session.refresh(row)
    assert row.status == "failed"
    assert row.error_code == "queue_unavailable"


async def test_approval_visibility_and_self_approval_guard(api, queue, db_session):
    http, state = api
    admin = await make_user(db_session, role="admin")
    state["user"] = admin
    workflow_id, version_id = await published(http)
    editor = await make_user(db_session, role="editor")
    state["user"] = editor
    editor_run = (await start_run(http, workflow_id)).json()
    _, editor_approval = await park(db_session, editor_run["id"])

    state["user"] = admin
    own_run = (await start_run(http, workflow_id)).json()
    _, own_approval = await park(db_session, own_run["id"])
    scheduled = await scheduled_run(
        db_session, workflow_id, version_id, status="waiting_approval"
    )
    _, scheduled_approval = await park(db_session, str(scheduled.id))

    listed = await http.get("/api/workflow-approvals")

    assert listed.status_code == 200, listed.text
    ids = {item["id"] for item in listed.json()}
    assert str(editor_approval.id) in ids
    assert str(scheduled_approval.id) in ids
    assert str(own_approval.id) not in ids
    assert {item["source"] for item in listed.json()} == {"workflow"}

    decided = await http.post(
        f"/api/workflow-approvals/{scheduled_approval.id}/decide",
        json={"decision": "approve"},
    )

    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "queued"
    assert queue.jobs[-1] == ("run_workflow_job", str(scheduled.id))

    own = await http.post(
        f"/api/workflow-approvals/{own_approval.id}/decide",
        json={"decision": "approve"},
    )

    assert own.status_code == 403

    _, foreign = await foreign_run(db_session)
    foreign_decide = await http.post(
        f"/api/workflow-approvals/{foreign.id}/decide",
        json={"decision": "approve"},
    )

    assert foreign_decide.status_code == 404
    assert foreign_decide.json()["code"] == "workflow_not_found"


async def test_reject_fails_run_and_denies_node(api, queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id, _ = await published(http)
    state["user"] = await make_user(db_session, role="editor")
    run = (await start_run(http, workflow_id)).json()
    _, approval = await park(db_session, run["id"])
    state["user"] = await make_user(db_session, role="admin")

    resp = await http.post(
        f"/api/workflow-approvals/{approval.id}/decide",
        json={"decision": "reject", "note": "不通过"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "failed"
    assert resp.json()["error_code"] == "review_rejected"
    assert len(queue.jobs) == 1

    node = (
        await db_session.execute(
            select(WorkflowNodeRun).where(
                WorkflowNodeRun.run_id == uuid.UUID(run["id"]),
                WorkflowNodeRun.node_id == "review",
            )
        )
    ).scalar_one()
    assert node.status == "denied"

    again = await http.post(
        f"/api/workflow-approvals/{approval.id}/decide",
        json={"decision": "reject"},
    )
    assert again.status_code == 409
    assert again.json()["code"] == "workflow_approval_not_pending"


async def test_approve_queue_failure_keeps_pending(api, down_queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id, _ = await published(http)
    state["user"] = await make_user(db_session, role="editor")
    run = (await start_run(http, workflow_id)).json()
    _, approval = await park(db_session, run["id"])
    state["user"] = await make_user(db_session, role="admin")

    resp = await http.post(
        f"/api/workflow-approvals/{approval.id}/decide",
        json={"decision": "approve"},
    )

    assert resp.status_code == 503
    assert resp.json()["code"] == "queue_unavailable"
    run_row = await db_session.get(WorkflowRun, uuid.UUID(run["id"]))
    await db_session.refresh(run_row)
    assert run_row.status == "waiting_approval"
    approval_row = await db_session.get(WorkflowApproval, approval.id)
    await db_session.refresh(approval_row)
    assert approval_row.status == "pending"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_workflow_api.py -q`
Expected: FAIL（14 个新用例因路由不存在得到 404/405；`app.api.workflow_runs` 导入失败）。

- [ ] **Step 3: 实现 `workflow_runs.py`**

Create `backend/app/api/workflow_runs.py`:

```python
import logging
import uuid
from datetime import UTC, datetime

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.policy import load_overrides, snapshot_policy
from app.agent.quota import ensure_daily_quota
from app.agent.registry import get_tool_registry
from app.config import Settings, get_settings
from app.db import get_session
from app.errors import AppError
from app.models.agent import AgentApproval, AgentRun
from app.models.user import User
from app.models.workflow import (
    Workflow,
    WorkflowApproval,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowVersion,
)
from app.permissions import admin_access, editor_access, viewer_access
from app.queue import get_queue
from app.services.audit import record_best_effort
from app.workflow import approvals as workflow_approvals
from app.workflow import lifecycle
from app.workflow.validate import validate_inputs

logger = logging.getLogger(__name__)

router = APIRouter(tags=["workflows"])

QUEUE_UNAVAILABLE_MESSAGE = "队列暂不可用，请稍后重试"
LIMITED_STATUSES = ("queued", "running")


class StartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inputs: dict = Field(default_factory=dict)
    version: int | None = Field(default=None, ge=1)


class DecideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str
    note: str = Field(default="", max_length=2000)


class WorkflowRunOut(BaseModel):
    id: str
    workflow_id: str
    workflow_version_id: str
    version: int
    trigger: str
    triggered_by: str | None
    schedule_id: str | None
    scheduled_for: datetime | None
    status: str
    inputs: dict
    result: dict
    error_code: str
    error_message: str
    usage: dict
    attempts: int
    resume_count: int
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None


class NodeRunOut(BaseModel):
    node_id: str
    attempt: int
    kind: str
    status: str
    tool_step_index: int | None
    agent_run_id: str | None
    input: dict
    output: dict
    error: str
    duration_ms: int
    started_at: datetime | None
    finished_at: datetime | None


class ApprovalOut(BaseModel):
    id: str
    run_id: str
    workflow_id: str
    source: str
    kind: str
    node_id: str
    tool_name: str
    risk: str
    title: str
    payload: dict
    status: str
    note: str
    expires_at: datetime | None
    created_at: datetime | None


class WorkflowRunDetailOut(BaseModel):
    run: WorkflowRunOut
    node_runs: list[NodeRunOut]
    approvals: list[ApprovalOut]
    result: dict


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid(raw: str, code: str, message: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError):
        raise AppError(code, message, 404) from None


async def _owned_workflow(
    session: AsyncSession, raw: str, user: User
) -> Workflow:
    row = (
        await session.execute(
            select(Workflow).where(
                Workflow.id == _uuid(raw, "workflow_not_found", "工作流不存在"),
                Workflow.tenant_id == user.tenant_id,
                Workflow.archived_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError("workflow_not_found", "工作流不存在", 404)
    return row


async def _owned_run(session: AsyncSession, raw: str, user: User) -> WorkflowRun:
    row = (
        await session.execute(
            select(WorkflowRun).where(
                WorkflowRun.id
                == _uuid(raw, "workflow_run_not_found", "运行不存在"),
                WorkflowRun.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError("workflow_run_not_found", "运行不存在", 404)
    return row


async def _owned_approval(
    session: AsyncSession, raw: str, user: User
) -> WorkflowApproval:
    row = (
        await session.execute(
            select(WorkflowApproval).where(
                WorkflowApproval.id
                == _uuid(raw, "workflow_not_found", "审批不存在"),
                WorkflowApproval.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError("workflow_not_found", "审批不存在", 404)
    return row


async def _version_row(
    session: AsyncSession, workflow: Workflow, number: int | None
) -> WorkflowVersion:
    if number is None:
        row = (
            await session.execute(
                select(WorkflowVersion)
                .where(WorkflowVersion.workflow_id == workflow.id)
                .order_by(WorkflowVersion.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            raise AppError("workflow_not_published", "工作流尚未发布", 409)
        return row
    row = (
        await session.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == workflow.id,
                WorkflowVersion.version == number,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError("workflow_not_found", "版本不存在", 404)
    return row


async def _version_number(session: AsyncSession, run: WorkflowRun) -> int:
    row = await session.get(WorkflowVersion, run.workflow_version_id)
    if row is None or row.tenant_id != run.tenant_id:
        raise AppError("workflow_run_not_found", "运行不存在", 404)
    return row.version


def _run_out(run: WorkflowRun, version: int) -> WorkflowRunOut:
    return WorkflowRunOut(
        id=str(run.id),
        workflow_id=str(run.workflow_id),
        workflow_version_id=str(run.workflow_version_id),
        version=version,
        trigger=run.trigger,
        triggered_by=str(run.triggered_by) if run.triggered_by else None,
        schedule_id=str(run.schedule_id) if run.schedule_id else None,
        scheduled_for=run.scheduled_for,
        status=run.status,
        inputs=run.inputs or {},
        result=run.result or {},
        error_code=run.error_code,
        error_message=run.error_message,
        usage=run.usage or {},
        attempts=run.attempts,
        resume_count=run.resume_count,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _node_out(row: WorkflowNodeRun) -> NodeRunOut:
    # raw_output 只用于变量解析，永远不进入响应。
    return NodeRunOut(
        node_id=row.node_id,
        attempt=row.attempt,
        kind=row.kind,
        status=row.status,
        tool_step_index=row.tool_step_index,
        agent_run_id=str(row.agent_run_id) if row.agent_run_id else None,
        input=row.input or {},
        output=row.output or {},
        error=row.error,
        duration_ms=row.duration_ms,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def _approval_out(
    approval,
    run: WorkflowRun,
    *,
    source: str,
    node_id: str,
    risk: str,
) -> ApprovalOut:
    return ApprovalOut(
        id=str(approval.id),
        run_id=str(run.id),
        workflow_id=str(run.workflow_id),
        source=source,
        kind=approval.kind if source == "workflow" else "tool",
        node_id=node_id,
        tool_name=approval.tool_name,
        risk=risk,
        title=approval.title,
        payload=approval.payload or {},
        status=approval.status,
        note=approval.note,
        expires_at=approval.expires_at,
        created_at=approval.created_at,
    )


async def _enqueue(queue: ArqRedis | None, run_id: uuid.UUID) -> None:
    if queue is None:
        raise AppError("queue_unavailable", QUEUE_UNAVAILABLE_MESSAGE, 503)
    try:
        await queue.enqueue_job("run_workflow_job", str(run_id))
    except Exception:
        logger.exception("workflow enqueue failed run=%s", run_id)
        raise AppError(
            "queue_unavailable", QUEUE_UNAVAILABLE_MESSAGE, 503
        ) from None


async def _find_by_key(
    session: AsyncSession, tenant_id: str, user_id: uuid.UUID, key: str
) -> WorkflowRun | None:
    return (
        await session.execute(
            select(WorkflowRun).where(
                WorkflowRun.tenant_id == tenant_id,
                WorkflowRun.triggered_by == user_id,
                WorkflowRun.idempotency_key == key,
            )
        )
    ).scalar_one_or_none()


async def _key_owned_by_other(
    session: AsyncSession, tenant_id: str, key: str
) -> bool:
    return (
        await session.execute(
            select(WorkflowRun.id)
            .where(
                WorkflowRun.tenant_id == tenant_id,
                WorkflowRun.idempotency_key == key,
            )
            .limit(1)
        )
    ).first() is not None


async def _ensure_active_limit(
    session: AsyncSession,
    tenant_id: str,
    user_id: uuid.UUID,
    settings: Settings,
) -> None:
    active = (
        await session.execute(
            select(func.count())
            .select_from(WorkflowRun)
            .where(
                WorkflowRun.tenant_id == tenant_id,
                WorkflowRun.triggered_by == user_id,
                WorkflowRun.status.in_(LIMITED_STATUSES),
            )
        )
    ).scalar_one()
    if active >= settings.workflow_max_active_runs_per_user:
        raise AppError(
            "workflow_run_limit",
            f"最多同时运行 {settings.workflow_max_active_runs_per_user} 个工作流",
            409,
        )


def _can_control(run: WorkflowRun, user: User) -> bool:
    if run.triggered_by == user.id:
        return True
    return run.trigger == "schedule" and user.role in ("owner", "admin")


async def _restore_pending(
    session: AsyncSession,
    approval: WorkflowApproval,
    run: WorkflowRun,
) -> None:
    reset_run = await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run.id,
            WorkflowRun.tenant_id == run.tenant_id,
            WorkflowRun.status == "queued",
        )
        .values(
            status="waiting_approval",
            finished_at=None,
            error_code="",
            error_message="",
        )
        .execution_options(synchronize_session=False)
    )
    reset_approval = await session.execute(
        update(WorkflowApproval)
        .where(
            WorkflowApproval.id == approval.id,
            WorkflowApproval.tenant_id == approval.tenant_id,
            WorkflowApproval.status == "approved",
        )
        .values(status="pending", decided_by=None, decided_at=None, note="")
        .execution_options(synchronize_session=False)
    )
    if reset_run.rowcount and reset_approval.rowcount:
        await session.commit()
        return
    await session.rollback()
    logger.warning(
        "workflow approval compensation skipped run=%s approval=%s",
        run.id,
        approval.id,
    )


@router.post(
    "/workflows/{workflow_id}/runs",
    response_model=WorkflowRunOut,
    status_code=202,
)
async def start_run(
    workflow_id: str,
    payload: StartIn,
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key"
    ),
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    queue: ArqRedis | None = Depends(get_queue),  # noqa: B008
) -> WorkflowRunOut:
    key = idempotency_key.strip() if idempotency_key else None
    if idempotency_key is not None and (not key or len(key) > 64):
        raise AppError("workflow_invalid", "Idempotency-Key 不合法", 422)
    tenant_id = user.tenant_id
    user_id = user.id
    workflow = await _owned_workflow(session, workflow_id, user)
    version = await _version_row(session, workflow, payload.version)
    if key:
        existing = await _find_by_key(session, tenant_id, user_id, key)
        if existing is not None:
            return _run_out(existing, await _version_number(session, existing))

    issues = validate_inputs(version.graph, payload.inputs)
    if issues:
        raise AppError(
            "workflow_invalid",
            "运行入参不合法",
            422,
            detail={"issues": [item.as_dict() for item in issues]},
        )
    settings = get_settings()
    await _ensure_active_limit(session, tenant_id, user_id, settings)
    await ensure_daily_quota(session, tenant_id, settings)

    specs = {spec.name: spec for spec in get_tool_registry().all()}
    run = WorkflowRun(
        tenant_id=tenant_id,
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        trigger="manual",
        triggered_by=user_id,
        status="queued",
        inputs=payload.inputs,
        policy=snapshot_policy(
            await load_overrides(session, tenant_id), specs
        ),
        idempotency_key=key,
    )
    session.add(run)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = (
            await _find_by_key(session, tenant_id, user_id, key)
            if key
            else None
        )
        if existing is not None:
            return _run_out(existing, await _version_number(session, existing))
        if key and await _key_owned_by_other(session, tenant_id, key):
            raise AppError(
                "workflow_conflict", "幂等键已被占用", 409
            ) from None
        raise
    await session.refresh(run)

    try:
        await _enqueue(queue, run.id)
    except AppError:
        # 行保留为失败记录，但释放幂等键，重放会重新尝试而不是回放一个
        # 从未进队的运行。
        await session.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run.id, WorkflowRun.status == "queued")
            .values(
                status="failed",
                error_code="queue_unavailable",
                error_message=QUEUE_UNAVAILABLE_MESSAGE,
                idempotency_key=None,
                finished_at=_now(),
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        raise

    await record_best_effort(
        session,
        tenant_id=tenant_id,
        action="workflow.run.create",
        actor=user,
        resource_type="workflow_run",
        resource_id=str(run.id),
        detail={"version": version.version},
    )
    return _run_out(run, version.version)


@router.get(
    "/workflows/{workflow_id}/runs",
    response_model=list[WorkflowRunOut],
)
async def list_runs(
    workflow_id: str,
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[WorkflowRunOut]:
    workflow = await _owned_workflow(session, workflow_id, user)
    stmt = (
        select(WorkflowRun, WorkflowVersion.version)
        .join(
            WorkflowVersion,
            WorkflowVersion.id == WorkflowRun.workflow_version_id,
        )
        .where(
            WorkflowRun.workflow_id == workflow.id,
            WorkflowRun.tenant_id == user.tenant_id,
        )
    )
    if status:
        stmt = stmt.where(WorkflowRun.status == status)
    stmt = (
        stmt.order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).all()
    return [_run_out(run, version) for run, version in rows]


@router.get("/workflow-runs/{run_id}", response_model=WorkflowRunDetailOut)
async def get_run(
    run_id: str,
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> WorkflowRunDetailOut:
    run = await _owned_run(session, run_id, user)
    version = await _version_number(session, run)
    nodes = (
        (
            await session.execute(
                select(WorkflowNodeRun)
                .where(WorkflowNodeRun.run_id == run.id)
                .order_by(
                    WorkflowNodeRun.created_at,
                    WorkflowNodeRun.node_id,
                    WorkflowNodeRun.attempt,
                )
            )
        )
        .scalars()
        .all()
    )
    pending_workflow = (
        (
            await session.execute(
                select(WorkflowApproval)
                .where(
                    WorkflowApproval.run_id == run.id,
                    WorkflowApproval.status == "pending",
                )
                .order_by(WorkflowApproval.created_at, WorkflowApproval.id)
            )
        )
        .scalars()
        .all()
    )
    pending_agent = (
        await session.execute(
            select(AgentApproval, AgentRun.workflow_node_id)
            .join(AgentRun, AgentRun.id == AgentApproval.run_id)
            .where(
                AgentRun.tenant_id == run.tenant_id,
                AgentRun.workflow_run_id == run.id,
                AgentApproval.status == "pending",
            )
            .order_by(AgentApproval.created_at, AgentApproval.id)
        )
    ).all()
    approvals = [
        _approval_out(
            row,
            run,
            source="workflow",
            node_id=row.node_id,
            risk=row.risk,
        )
        for row in pending_workflow
    ]
    approvals.extend(
        _approval_out(
            row,
            run,
            source="agent",
            node_id=node_id or "",
            risk=row.risk,
        )
        for row, node_id in pending_agent
    )
    return WorkflowRunDetailOut(
        run=_run_out(run, version),
        node_runs=[_node_out(row) for row in nodes],
        approvals=approvals,
        result=run.result or {},
    )


@router.post("/workflow-runs/{run_id}/cancel", response_model=WorkflowRunOut)
async def cancel_run(
    run_id: str,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> WorkflowRunOut:
    run = await _owned_run(session, run_id, user)
    if not _can_control(run, user):
        raise AppError("forbidden", "无权操作该运行", 403)
    if not await lifecycle.cancel_run(session, run, actor=str(user.id)):
        raise AppError(
            "workflow_run_not_cancellable", "运行已结束，无法取消", 409
        )
    await session.refresh(run)
    return _run_out(run, await _version_number(session, run))


@router.post("/workflow-runs/{run_id}/resume", response_model=WorkflowRunOut)
async def resume_run(
    run_id: str,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    queue: ArqRedis | None = Depends(get_queue),  # noqa: B008
) -> WorkflowRunOut:
    run = await _owned_run(session, run_id, user)
    if not _can_control(run, user):
        raise AppError("forbidden", "无权操作该运行", 403)
    await lifecycle.resume_run(session, run, actor=str(user.id))
    try:
        await _enqueue(queue, run.id)
    except AppError:
        # 没进队就不算续跑成功：退回 failed，节点保持 interrupted，可再次续跑。
        await session.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run.id, WorkflowRun.status == "queued")
            .values(
                status="failed",
                error_code="queue_unavailable",
                error_message=QUEUE_UNAVAILABLE_MESSAGE,
                finished_at=_now(),
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        raise
    await session.refresh(run)
    return _run_out(run, await _version_number(session, run))


@router.get("/workflow-approvals", response_model=list[ApprovalOut])
async def list_approvals(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[ApprovalOut]:
    rows = (
        await session.execute(
            select(WorkflowApproval, WorkflowRun)
            .join(WorkflowRun, WorkflowRun.id == WorkflowApproval.run_id)
            .where(
                WorkflowApproval.tenant_id == user.tenant_id,
                WorkflowApproval.status == "pending",
                or_(
                    WorkflowRun.trigger != "manual",
                    WorkflowRun.triggered_by.is_(None),
                    WorkflowRun.triggered_by != user.id,
                ),
            )
            .order_by(WorkflowApproval.created_at, WorkflowApproval.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [
        _approval_out(
            approval,
            run,
            source="workflow",
            node_id=approval.node_id,
            risk=approval.risk,
        )
        for approval, run in rows
    ]


@router.post(
    "/workflow-approvals/{approval_id}/decide",
    response_model=WorkflowRunOut,
)
async def decide(
    approval_id: str,
    payload: DecideIn,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    queue: ArqRedis | None = Depends(get_queue),  # noqa: B008
) -> WorkflowRunOut:
    approval = await _owned_approval(session, approval_id, user)
    run = await session.get(WorkflowRun, approval.run_id)
    if run is None or run.tenant_id != user.tenant_id:
        raise AppError(
            "workflow_approval_not_pending", "审批与运行不匹配", 409
        )
    if run.trigger == "manual" and run.triggered_by == user.id:
        raise AppError("forbidden", "不能审批自己发起的运行", 403)

    state = await workflow_approvals.decide_approval(
        session,
        run=run,
        approval=approval,
        decision=payload.decision,
        actor_id=user.id,
        note=payload.note,
    )
    if state == "queued":
        try:
            await _enqueue(queue, run.id)
        except AppError:
            # 同意已落库但 worker 不可达：把审批和运行一起退回可再次决策的状态。
            await _restore_pending(session, approval, run)
            raise
    await record_best_effort(
        session,
        tenant_id=run.tenant_id,
        action="workflow.approval.decide",
        actor=user,
        resource_type="workflow_run",
        resource_id=str(run.id),
        detail={"decision": payload.decision, "node": approval.node_id},
    )
    return _run_out(run, await _version_number(session, run))
```

- [ ] **Step 4: 注册路由**

在 `backend/app/main.py` 的 `from app.api import (...)` 块中，按字母序把 `workflow_runs,` 插到 `workflows,` 之前：

```python
from app.api import (
    accounts,
    agent,
    analytics,
    audit,
    auth,
    calendar,
    chat,
    members,
    profiles,
    projects,
    publish,
    skills,
    tenants,
    workflow_runs,
    workflows,
)
```

在 `app.include_router(workflows.router, prefix="/api")` 之后加：

```python
app.include_router(workflow_runs.router, prefix="/api")
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_workflow_api.py -q`
Expected: PASS（28 passed：Task 9/10 的 14 个 + 本任务新增 14 个）。

Run: `cd backend && uv run pytest tests/test_agent_api.py -q`
Expected: PASS（不回归）。

Run: `cd backend && uv run ruff check app/api/workflow_runs.py app/main.py`
Expected: `All checks passed!`

- [ ] **Step 6: 提交**

```bash
git add backend/app/api/workflow_runs.py backend/app/main.py backend/tests/test_workflow_api.py
git commit -m "feat(workflow): add run and approval APIs"
```

---

## Task 12: Worker 与第 1 层接入

**Files:**
- Create: `backend/tests/test_workflow_worker.py`
- Modify: `backend/app/worker.py`
- Modify: `backend/app/api/agent.py`
- Modify: `backend/app/agent/runtime.py`
- Test: `backend/tests/test_agent_api.py`（在文件末尾追加）

**背景与接口：** 引擎（Task 6/7）、生命周期（Task 8）、审批（Task 5）、API（Task 9–11）都已就绪。本任务把三件事接上：worker 执行 `run_workflow_job`、定时触发 `fire_due_workflow_schedules`、心跳对账 `reconcile_workflow_runs`；以及第 1 层子运行审批的工作流分支。

**关键约定（实现时照抄）：**
- `run_workflow_job`：只认 `queued`；先做租户校验，再用 `lifecycle.claim_run` CAS 领取（返回非 `"claimed"` 直接退出，重复投递不会重复执行）。网关加载失败（`AppError`，例如 `llm_not_configured`）**不**终结运行：没有 Agent 节点的工作流照样能跑，需要网关的 Agent 节点自己落 `llm_not_configured`。任何逃逸异常把仍处于 `queued`/`running` 的运行落 `workflow_internal_error`；等待审批/终态绝不被兜底改写（`fail_active_run` 内建此边界）。
- 执行期间每 `heartbeat_seconds`（默认 15 秒）用独立会话刷新 `heartbeat_at`，只改 `queued`/`running` 行；任务结束（成功、失败、暂停、重排）即停止心跳。
- 定时：扫描 `enabled` 且 `next_run_at <= now` 的定时，**先 CAS 推进 `next_run_at`/`last_run_at` 并提交**，再判断是否建运行。推进失败说明另一个 worker 已消费该时刻，直接跳过。`scheduled_for` 取被消费的计划时刻（`next_run_at` 原值），不是扫描墙钟 —— 这样唯一 `(schedule_id, scheduled_for)` 能兜住重复触发。该工作流已有活跃运行（`queued`/`running`/`waiting_approval`）、工作流已归档、版本缺失、租户停用时跳过并写 `workflow.schedule.skipped` 审计；**跳过时同样推进**，否则会每秒重复触发。入库失败（`IntegrityError`）视为并发重复触发，静默跳过。入队失败把这条新运行落 `failed/queue_unavailable`。
- 定时运行不查日 token 配额（可能根本没有 Agent 节点）；配额在 Agent 节点启动前由引擎再查一次。
- 对账：`lifecycle.reconcile` 负责状态，worker 只提供 `on_requeue` 入队回调；回调入队失败（含没有队列）立即把这条运行落 `failed/queue_unavailable`，避免它停在 `queued` 永远没人执行。
- 第 1 层子运行审批（`app/api/agent.py`）：`run.workflow_run_id is not None` 时，**父工作流是唯一执行者**。同意后不要入队 `run_agent_job`（父子并发执行会让父运行看到子 `running` 而报 `workflow_internal_error`），而是：若父运行已无任何待处理审批（工作流审批 + 全部子运行审批），把父运行 `waiting_approval → queued` 并只入队 `run_workflow_job`；仍有兄弟审批时只落审批结果，父运行继续等待。父运行入队失败时，在同一事务里把父运行、子运行、审批三者一起回滚到可再次决策的状态。
- 驳回子运行审批：第 1 层已把子运行落 `failed/approval_rejected`；随后调用 `lifecycle.fail_waiting_run` 终结父运行（同码），保证没有审批或子运行比父运行活得更久。
- 自批校验（`app/agent/runtime.py`）：只有**存在发起人**的运行才排除发起人。独立运行与手动工作流运行有发起人；定时工作流运行 `triggered_by is None`，不排除（见规格 §5.2）。
- 审批列表（`GET /api/agent/approvals`）：手动工作流子运行仍排除发起人；定时工作流子运行对全部 admin/owner 可见。用外连接 `WorkflowRun`，条件写成 `or_(AgentRun.user_id != user.id, WorkflowRun.trigger == "schedule")`。
- `trigger` 取值固定为 `"manual"` / `"schedule"`（规格 §4.1 表；`String(16)` 足够）。

- [ ] **Step 1: 写失败的 worker 测试**

Create `backend/tests/test_workflow_worker.py`:

```python
import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import worker
from app.models.audit import AuditLog
from app.models.base import DEFAULT_TENANT_ID, Base
from app.models.tenant import Tenant
from app.models.workflow import (
    Workflow,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowSchedule,
    WorkflowVersion,
)

NOW = datetime.now(UTC)

GRAPH = {
    "nodes": [
        {
            "id": "in",
            "type": "input",
            "data": {"fields": [{"key": "topic", "type": "string"}]},
        },
        {"id": "out", "type": "output", "data": {}},
    ],
    "edges": [{"source": "in", "target": "out"}],
}

CADENCE = {
    "kind": "daily",
    "weekdays": [],
    "time": "09:00",
    "timezone": "Asia/Shanghai",
}


def aware(value):
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class FakeQueue:
    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, name, *args):
        self.jobs.append((name, *args))


class DownQueue:
    async def enqueue_job(self, name, *args):
        raise RuntimeError("redis down")


@pytest_asyncio.fixture
async def worker_factory(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'workflow-worker.db'}"
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            Tenant(
                id=DEFAULT_TENANT_ID,
                name="默认租户",
                slug="default",
                status="active",
            )
        )
        await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


async def new_run(
    factory,
    *,
    status="queued",
    attempts=0,
    heartbeat_at=None,
):
    async with factory() as session:
        workflow = Workflow(
            tenant_id=DEFAULT_TENANT_ID, name="测试流", created_by=uuid.uuid4()
        )
        session.add(workflow)
        await session.flush()
        version = WorkflowVersion(
            tenant_id=DEFAULT_TENANT_ID,
            workflow_id=workflow.id,
            version=1,
            graph=GRAPH,
            source_revision=0,
            published_by=workflow.created_by,
            published_at=datetime.now(UTC),
        )
        session.add(version)
        await session.flush()
        run = WorkflowRun(
            tenant_id=DEFAULT_TENANT_ID,
            workflow_id=workflow.id,
            workflow_version_id=version.id,
            status=status,
            inputs={"topic": "AI 周报"},
            policy={},
            triggered_by=None,
            attempts=attempts,
            heartbeat_at=heartbeat_at,
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run


async def load_run(factory, run_id):
    async with factory() as session:
        return await session.get(WorkflowRun, run_id)


async def new_schedule(factory, *, archived=False, next_run_at=None):
    async with factory() as session:
        workflow = Workflow(
            tenant_id=DEFAULT_TENANT_ID,
            name="定时流",
            created_by=uuid.uuid4(),
            archived_at=datetime.now(UTC) if archived else None,
        )
        session.add(workflow)
        await session.flush()
        version = WorkflowVersion(
            tenant_id=DEFAULT_TENANT_ID,
            workflow_id=workflow.id,
            version=1,
            graph=GRAPH,
            source_revision=0,
            published_by=workflow.created_by,
            published_at=datetime.now(UTC),
        )
        session.add(version)
        await session.flush()
        slot = next_run_at or NOW - timedelta(minutes=5)
        schedule = WorkflowSchedule(
            tenant_id=DEFAULT_TENANT_ID,
            workflow_id=workflow.id,
            workflow_version_id=version.id,
            cadence=dict(CADENCE),
            inputs={"topic": "定时早报"},
            enabled=True,
            next_run_at=slot,
            created_by=workflow.created_by,
        )
        session.add(schedule)
        await session.commit()
        await session.refresh(schedule)
        return schedule, slot


async def all_runs(factory):
    async with factory() as session:
        return (
            (await session.execute(select(WorkflowRun))).scalars().all()
        )


async def test_run_workflow_job_completes_queued_run(worker_factory):
    run = await new_run(worker_factory)

    await worker.run_workflow_job(
        None, str(run.id), session_factory=worker_factory
    )

    row = await load_run(worker_factory, run.id)
    assert row.status == "succeeded"
    assert row.attempts == 1
    assert row.started_at is not None
    async with worker_factory() as session:
        nodes = (
            (
                await session.execute(
                    select(WorkflowNodeRun).where(
                        WorkflowNodeRun.run_id == run.id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert {(item.node_id, item.status) for item in nodes} == {
        ("in", "ok"),
        ("out", "ok"),
    }


async def test_run_workflow_job_skips_run_that_is_not_queued(worker_factory):
    run = await new_run(worker_factory, status="running", attempts=1)

    await worker.run_workflow_job(
        None, str(run.id), session_factory=worker_factory
    )

    row = await load_run(worker_factory, run.id)
    assert row.status == "running"
    assert row.attempts == 1


async def test_run_workflow_job_fails_suspended_tenant(worker_factory):
    run = await new_run(worker_factory)
    async with worker_factory() as session:
        tenant = await session.get(Tenant, DEFAULT_TENANT_ID)
        tenant.status = "suspended"
        await session.commit()

    await worker.run_workflow_job(
        None, str(run.id), session_factory=worker_factory
    )

    row = await load_run(worker_factory, run.id)
    assert row.status == "failed"
    assert row.error_code == "tenant_suspended"


async def test_run_workflow_job_fails_run_when_engine_crashes(
    worker_factory, monkeypatch
):
    run = await new_run(worker_factory)

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(worker, "run_workflow", boom)

    await worker.run_workflow_job(
        None,
        str(run.id),
        session_factory=worker_factory,
        heartbeat_seconds=0.01,
    )

    row = await load_run(worker_factory, run.id)
    assert row.status == "failed"
    assert row.error_code == "workflow_internal_error"


async def test_heartbeat_loop_refreshes_active_run(worker_factory):
    run = await new_run(worker_factory)

    task = asyncio.create_task(
        worker._heartbeat_loop(worker_factory, run.id, interval=0.01)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = await load_run(worker_factory, run.id)
    assert row.heartbeat_at is not None


async def test_heartbeat_loop_ignores_finished_run(worker_factory):
    run = await new_run(worker_factory, status="succeeded")

    task = asyncio.create_task(
        worker._heartbeat_loop(worker_factory, run.id, interval=0.01)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = await load_run(worker_factory, run.id)
    assert row.heartbeat_at is None


async def test_fire_schedule_creates_and_enqueues_run(worker_factory):
    schedule, slot = await new_schedule(worker_factory)
    queue = FakeQueue()

    fired = await worker.fire_due_workflow_schedules(
        session_factory=worker_factory, queue=queue, now=NOW
    )

    assert fired == 1
    runs = await all_runs(worker_factory)
    assert len(runs) == 1
    created = runs[0]
    assert created.trigger == "schedule"
    assert created.triggered_by is None
    assert created.schedule_id == schedule.id
    assert aware(created.scheduled_for) == slot
    assert created.status == "queued"
    assert created.policy
    assert queue.jobs == [("run_workflow_job", str(created.id))]
    async with worker_factory() as session:
        row = await session.get(WorkflowSchedule, schedule.id)
    assert aware(row.last_run_at) == slot
    assert aware(row.next_run_at) > NOW


async def test_fire_schedule_is_idempotent_for_the_same_slot(worker_factory):
    await new_schedule(worker_factory)
    queue = FakeQueue()

    first = await worker.fire_due_workflow_schedules(
        session_factory=worker_factory, queue=queue, now=NOW
    )
    second = await worker.fire_due_workflow_schedules(
        session_factory=worker_factory, queue=queue, now=NOW
    )

    assert first == 1
    assert second == 0
    assert len(await all_runs(worker_factory)) == 1
    assert len(queue.jobs) == 1


async def test_fire_schedule_skips_archived_workflow_and_advances(
    worker_factory,
):
    schedule, slot = await new_schedule(worker_factory, archived=True)

    fired = await worker.fire_due_workflow_schedules(
        session_factory=worker_factory,
        queue=FakeQueue(),
        now=NOW,
    )

    assert fired == 0
    assert await all_runs(worker_factory) == []
    async with worker_factory() as session:
        audits = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.action == "workflow.schedule.skipped"
                    )
                )
            )
            .scalars()
            .all()
        )
        row = await session.get(WorkflowSchedule, schedule.id)
    assert len(audits) == 1
    assert audits[0].detail["reason"] == "workflow_archived"
    assert aware(row.last_run_at) == slot
    assert aware(row.next_run_at) > NOW


async def test_fire_schedule_skips_when_workflow_has_active_run(
    worker_factory,
):
    schedule, _slot = await new_schedule(worker_factory)
    async with worker_factory() as session:
        session.add(
            WorkflowRun(
                tenant_id=DEFAULT_TENANT_ID,
                workflow_id=schedule.workflow_id,
                workflow_version_id=schedule.workflow_version_id,
                status="queued",
                inputs={},
                policy={},
            )
        )
        await session.commit()

    fired = await worker.fire_due_workflow_schedules(
        session_factory=worker_factory,
        queue=FakeQueue(),
        now=NOW,
    )

    assert fired == 0
    assert len(await all_runs(worker_factory)) == 1
    async with worker_factory() as session:
        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "workflow.schedule.skipped"
                )
            )
        ).scalars().one()
    assert audit.detail["reason"] == "active_run"


async def test_fire_schedule_fails_run_when_queue_is_unavailable(
    worker_factory,
):
    schedule, _slot = await new_schedule(worker_factory)

    fired = await worker.fire_due_workflow_schedules(
        session_factory=worker_factory,
        queue=DownQueue(),
        now=NOW,
    )

    assert fired == 0
    runs = await all_runs(worker_factory)
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert runs[0].error_code == "queue_unavailable"
    async with worker_factory() as session:
        row = await session.get(WorkflowSchedule, schedule.id)
    assert aware(row.next_run_at) > NOW


async def test_reconcile_requeues_stale_run(worker_factory):
    run = await new_run(
        worker_factory,
        status="running",
        attempts=1,
        heartbeat_at=NOW - timedelta(minutes=10),
    )
    queue = FakeQueue()

    counts = await worker.reconcile_workflow_runs(
        session_factory=worker_factory, queue=queue, now=NOW
    )

    assert counts["requeued"] == 1
    row = await load_run(worker_factory, run.id)
    assert row.status == "queued"
    assert queue.jobs == [("run_workflow_job", str(run.id))]


async def test_reconcile_fails_run_when_requeue_cannot_enqueue(
    worker_factory,
):
    run = await new_run(
        worker_factory,
        status="running",
        attempts=1,
        heartbeat_at=NOW - timedelta(minutes=10),
    )

    await worker.reconcile_workflow_runs(
        session_factory=worker_factory, queue=DownQueue(), now=NOW
    )

    row = await load_run(worker_factory, run.id)
    assert row.status == "failed"
    assert row.error_code == "queue_unavailable"


async def test_reconcile_fails_run_after_second_stale_attempt(
    worker_factory,
):
    run = await new_run(
        worker_factory,
        status="running",
        attempts=2,
        heartbeat_at=NOW - timedelta(minutes=10),
    )

    counts = await worker.reconcile_workflow_runs(
        session_factory=worker_factory, queue=FakeQueue(), now=NOW
    )

    assert counts["stale_failed"] == 1
    row = await load_run(worker_factory, run.id)
    assert row.status == "failed"
    assert row.error_code == "workflow_stale"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_workflow_worker.py -q`
Expected: FAIL / ERROR（`AttributeError: module 'app.worker' has no attribute 'run_workflow_job'`）。

- [ ] **Step 3: 实现 worker**

在 `backend/app/worker.py` 顶部导入区：

1）在 `import logging` 之前加：

```python
import asyncio
import contextlib
```

2）在现有导入之后追加：

```python
from sqlalchemy.exc import IntegrityError

from app.agent.policy import load_overrides, snapshot_policy
from app.models.workflow import (
    Workflow,
    WorkflowRun,
    WorkflowSchedule,
    WorkflowVersion,
)
from app.workflow import lifecycle
from app.workflow.engine import EngineDeps, run_workflow
from app.workflow.schedule import next_run_at, parse_cadence
```

3）在常量区追加：

```python
WORKFLOW_INTERNAL_ERROR = "workflow_internal_error"
WORKFLOW_INTERNAL_MESSAGE = "工作流运行异常，请稍后重试"
WORKFLOW_QUEUE_MESSAGE = "队列暂不可用，请稍后重试"
WORKFLOW_HEARTBEAT_SECONDS = 15
WORKFLOW_SCHEDULE_BATCH_SIZE = 100
# 只有这两个状态属于「仍在执行」，心跳与崩溃兜底都只碰它们。
WORKFLOW_ACTIVE_STATUSES = {"queued", "running"}
WORKFLOW_RUNNING_STATUSES = ("queued", "running", "waiting_approval")
```

4）在 `run_agent_job` 之后追加：

```python
async def _heartbeat_loop(
    session_factory: Callable[
        [], AbstractAsyncContextManager[AsyncSession]
    ],
    run_id: uuid.UUID,
    *,
    interval: float,
) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            async with session_factory() as session:
                await session.execute(
                    update(WorkflowRun)
                    .where(
                        WorkflowRun.id == run_id,
                        WorkflowRun.status.in_(WORKFLOW_ACTIVE_STATUSES),
                    )
                    .values(heartbeat_at=datetime.now(UTC))
                    .execution_options(synchronize_session=False)
                )
                await session.commit()
        except Exception:  # noqa: BLE001 - 心跳失败不能打断运行本身
            logger.warning("workflow heartbeat failed run=%s", run_id)


async def _fail_active_workflow_run(
    session_factory: Callable[
        [], AbstractAsyncContextManager[AsyncSession]
    ],
    run_uuid: uuid.UUID,
    run_id: str,
) -> None:
    """把崩溃后仍处于 queued/running 的运行落为失败。

    ``waiting_approval`` 与其他终态属于别的写入者，``fail_active_run``
    只改 queued/running，因此这里可以无条件调用。
    """
    try:
        async with session_factory() as session:
            await lifecycle.fail_active_run(
                session,
                run_uuid,
                error_code=WORKFLOW_INTERNAL_ERROR,
                message=WORKFLOW_INTERNAL_MESSAGE,
                actor="worker",
            )
    except Exception:  # noqa: BLE001 - 兜底写入失败只记录日志
        logger.exception("workflow run failure could not be recorded run=%s", run_id)


async def run_workflow_job(
    _ctx: dict | None = None,
    run_id: str | None = None,
    *,
    session_factory: Callable[
        [], AbstractAsyncContextManager[AsyncSession]
    ] = SessionLocal,
    gateway: LLMGateway | None = None,
    tool_registry: ToolRegistry | None = None,
    skill_registry: SkillRegistry | None = None,
    storage: StorageBackend | None = None,
    queue: ArqRedis | None = None,
    heartbeat_seconds: float = WORKFLOW_HEARTBEAT_SECONDS,
) -> None:
    """执行一个已入队的工作流运行。

    只有 ``queued`` 会被领取：``lifecycle.claim_run`` 的条件更新保证 arq
    至少一次投递下的幂等，重复投递或已被其他执行者接手的运行直接返回。
    网关缺失不终结运行（没有 Agent 节点的工作流不需要网关），任何逃逸
    异常把仍处于 queued/running 的运行落 ``workflow_internal_error``。
    """
    worker_queue = queue or (_ctx or {}).get("redis")
    try:
        run_uuid = uuid.UUID(str(run_id))
    except (AttributeError, TypeError, ValueError):
        logger.warning("workflow run id 不合法，跳过 run=%s", run_id)
        return
    tenant_id: str | None = None
    async with session_factory() as session:
        try:
            run = await session.get(WorkflowRun, run_uuid)
            if run is None or run.status != "queued":
                return
            tenant_id = run.tenant_id
            tenant = await session.get(Tenant, run.tenant_id)
            if tenant is None or tenant.status != "active":
                await lifecycle.fail_active_run(
                    session,
                    run_uuid,
                    error_code="tenant_suspended",
                    message="租户已停用",
                    actor="worker",
                )
                return
            if await lifecycle.claim_run(session, run_uuid) != "claimed":
                return
        except Exception:
            logger.exception("workflow run setup failed run=%s", run_id)
            await _fail_active_workflow_run(
                session_factory, run_uuid, str(run_id)
            )
            return

    active_gateway = gateway
    if active_gateway is None and tenant_id is not None:
        try:
            async with session_factory() as session:
                active_gateway = await load_gateway_default(
                    session, tenant_id
                )
        except AppError:
            # 没有 Agent 节点的工作流不需要网关；需要时由 Agent 节点自己
            # 落 llm_not_configured。
            active_gateway = None
        except Exception:
            logger.exception("workflow gateway setup failed run=%s", run_id)
            await _fail_active_workflow_run(
                session_factory, run_uuid, str(run_id)
            )
            return

    deps = EngineDeps(
        gateway=active_gateway,
        tool_registry=tool_registry or get_tool_registry(),
        skill_registry=skill_registry or get_registry(),
        storage=storage or get_storage(),
        queue=worker_queue,
        settings=get_settings(),
        session_factory=session_factory,
    )
    beat = asyncio.create_task(
        _heartbeat_loop(
            session_factory, run_uuid, interval=heartbeat_seconds
        )
    )
    try:
        await run_workflow(session_factory, run_uuid, deps=deps)
    except Exception:
        logger.exception("workflow run crashed run=%s", run_uuid)
        await _fail_active_workflow_run(session_factory, run_uuid, str(run_uuid))
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat


async def _schedule_skip_reason(
    session: AsyncSession, schedule: WorkflowSchedule
) -> str | None:
    tenant = await session.get(Tenant, schedule.tenant_id)
    if tenant is None or tenant.status != "active":
        return "tenant_suspended"
    workflow = await session.get(Workflow, schedule.workflow_id)
    if workflow is None or workflow.archived_at is not None:
        return "workflow_archived"
    version = await session.get(WorkflowVersion, schedule.workflow_version_id)
    if version is None:
        return "version_missing"
    active = (
        await session.execute(
            select(WorkflowRun.id)
            .where(
                WorkflowRun.tenant_id == schedule.tenant_id,
                WorkflowRun.workflow_id == schedule.workflow_id,
                WorkflowRun.status.in_(WORKFLOW_RUNNING_STATUSES),
            )
            .limit(1)
        )
    ).first()
    return "active_run" if active is not None else None


async def _audit_schedule_skip(
    session: AsyncSession, schedule: WorkflowSchedule, reason: str
) -> None:
    await record_best_effort(
        session,
        tenant_id=schedule.tenant_id,
        action="workflow.schedule.skipped",
        actor="scheduler",
        resource_type="workflow_schedule",
        resource_id=str(schedule.id),
        detail={"workflow": str(schedule.workflow_id), "reason": reason},
    )


async def _fire_schedule(
    session_factory: Callable[
        [], AbstractAsyncContextManager[AsyncSession]
    ],
    schedule_id: uuid.UUID,
    *,
    queue: ArqRedis | None,
    moment: datetime,
) -> bool:
    """消费一个到期的定时：先推进计划时刻，再决定建不建运行。"""
    async with session_factory() as session:
        schedule = await session.get(WorkflowSchedule, schedule_id)
        if (
            schedule is None
            or not schedule.enabled
            or schedule.next_run_at is None
        ):
            return False
        try:
            following = next_run_at(parse_cadence(schedule.cadence), moment)
        except AppError as exc:
            # API 已校验 cadence；坏数据只记录，不阻断同批其他定时。
            logger.warning(
                "workflow schedule cadence invalid schedule=%s: %s",
                schedule.id,
                exc.message,
            )
            await session.rollback()
            return False
        slot = schedule.next_run_at
        advanced = await session.execute(
            update(WorkflowSchedule)
            .where(
                WorkflowSchedule.id == schedule.id,
                WorkflowSchedule.tenant_id == schedule.tenant_id,
                WorkflowSchedule.enabled.is_(True),
                WorkflowSchedule.next_run_at == slot,
            )
            .values(next_run_at=following, last_run_at=slot)
            .execution_options(synchronize_session=False)
        )
        if advanced.rowcount == 0:
            # 另一个 worker 已消费该触发时刻。
            await session.rollback()
            return False
        await session.commit()

        skip = await _schedule_skip_reason(session, schedule)
        if skip is not None:
            await _audit_schedule_skip(session, schedule, skip)
            return False

        run = WorkflowRun(
            tenant_id=schedule.tenant_id,
            workflow_id=schedule.workflow_id,
            workflow_version_id=schedule.workflow_version_id,
            trigger="schedule",
            triggered_by=None,
            schedule_id=schedule.id,
            scheduled_for=slot,
            status="queued",
            inputs=dict(schedule.inputs or {}),
            policy=snapshot_policy(
                await load_overrides(session, schedule.tenant_id),
                {spec.name: spec for spec in get_tool_registry().all()},
            ),
            heartbeat_at=moment,
        )
        session.add(run)
        try:
            await session.commit()
        except IntegrityError:
            # 同一 (schedule_id, scheduled_for) 已存在：并发触发，保留先到者。
            await session.rollback()
            return False
        await session.refresh(run)

        try:
            await _enqueue_workflow(queue, run.id)
        except AppError:
            await session.execute(
                update(WorkflowRun)
                .where(
                    WorkflowRun.id == run.id,
                    WorkflowRun.status == "queued",
                )
                .values(
                    status="failed",
                    error_code="queue_unavailable",
                    error_message=WORKFLOW_QUEUE_MESSAGE,
                    finished_at=moment,
                )
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            return False

        await record_best_effort(
            session,
            tenant_id=schedule.tenant_id,
            action="workflow.schedule.fire",
            actor="scheduler",
            resource_type="workflow_run",
            resource_id=str(run.id),
            detail={
                "schedule": str(schedule.id),
                "scheduled_for": slot.isoformat(),
            },
        )
        return True


async def fire_due_workflow_schedules(
    _ctx: dict | None = None,
    *,
    session_factory: Callable[
        [], AbstractAsyncContextManager[AsyncSession]
    ] = SessionLocal,
    queue: ArqRedis | None = None,
    now: datetime | None = None,
) -> int:
    """每分钟触发一次：消费所有到期的定时并创建运行。

    单条定时失败不阻断同批其他定时；每条定时独立会话，推进计划时刻与
    创建运行不共享事务（进程在两者之间崩溃只会漏掉一次，不会重复触发）。
    """
    moment = now or datetime.now(UTC)
    worker_queue = queue or (_ctx or {}).get("redis")
    async with session_factory() as session:
        due = (
            (
                await session.execute(
                    select(WorkflowSchedule.id)
                    .where(
                        WorkflowSchedule.enabled.is_(True),
                        WorkflowSchedule.next_run_at.is_not(None),
                        WorkflowSchedule.next_run_at <= moment,
                    )
                    .order_by(
                        WorkflowSchedule.next_run_at, WorkflowSchedule.id
                    )
                    .limit(WORKFLOW_SCHEDULE_BATCH_SIZE)
                )
            )
            .scalars()
            .all()
        )
    fired = 0
    for schedule_id in due:
        try:
            if await _fire_schedule(
                session_factory, schedule_id, queue=worker_queue, moment=moment
            ):
                fired += 1
        except Exception:  # noqa: BLE001 - 单条失败不阻断同批
            logger.exception("workflow schedule fire failed id=%s", schedule_id)
    return fired


async def reconcile_workflow_runs(
    _ctx: dict | None = None,
    *,
    session_factory: Callable[
        [], AbstractAsyncContextManager[AsyncSession]
    ] = SessionLocal,
    queue: ArqRedis | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """心跳对账：重排超时运行、终结无望运行、过期审批。

    状态迁移全部在 ``lifecycle.reconcile`` 内完成，worker 只负责把重排回
    ``queued`` 的运行投递出去。入队失败（含没有队列）立即落
    ``queue_unavailable``，避免运行停在 queued 永远没人执行。
    """
    worker_queue = queue or (_ctx or {}).get("redis")

    async def wake(run_id: uuid.UUID) -> None:
        if worker_queue is None:
            async with session_factory() as session:
                await lifecycle.fail_active_run(
                    session,
                    run_id,
                    error_code="queue_unavailable",
                    message=WORKFLOW_QUEUE_MESSAGE,
                    actor="worker",
                )
            return
        try:
            await worker_queue.enqueue_job("run_workflow_job", str(run_id))
        except Exception:
            logger.exception("workflow requeue failed run=%s", run_id)
            async with session_factory() as session:
                await lifecycle.fail_active_run(
                    session,
                    run_id,
                    error_code="queue_unavailable",
                    message=WORKFLOW_QUEUE_MESSAGE,
                    actor="worker",
                )

    return await lifecycle.reconcile(
        session_factory,
        settings=get_settings(),
        now=now,
        on_requeue=wake,
    )
```

5）把 `WorkerSettings` 改成：

```python
class WorkerSettings:
    functions: ClassVar[list] = [
        process_media_job,
        run_agent_job,
        run_workflow_job,
    ]
    cron_jobs: ClassVar[list] = [
        cron(publish_due_jobs, second=0, job_id="publish-due-jobs"),
        cron(
            fire_due_workflow_schedules,
            second=15,
            job_id="workflow-schedules",
        ),
        cron(reconcile_agent_runs, second=30, job_id="agent-reconcile"),
        cron(
            reconcile_workflow_runs,
            second=45,
            job_id="workflow-reconcile",
        ),
        cron(archive_audit_logs, hour=3, minute=15, job_id="audit-archive"),
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    job_timeout = 600
```

6）在 `run_agent_job` 之前（`_fail_active_agent_run` 之后）加一个共用的入队函数 —— 它被 `_fire_schedule` 使用：

```python
async def _enqueue_workflow(queue: ArqRedis | None, run_id: uuid.UUID) -> None:
    if queue is None:
        raise AppError("queue_unavailable", WORKFLOW_QUEUE_MESSAGE, 503)
    try:
        await queue.enqueue_job("run_workflow_job", str(run_id))
    except Exception:
        logger.exception("workflow enqueue failed run=%s", run_id)
        raise AppError(
            "queue_unavailable", WORKFLOW_QUEUE_MESSAGE, 503
        ) from None
```

- [ ] **Step 4: 运行 worker 测试确认通过**

Run: `cd backend && uv run pytest tests/test_workflow_worker.py -q`
Expected: PASS（14 passed）。

Run: `cd backend && uv run pytest tests/test_agent_worker.py tests/test_worker.py -q`
Expected: PASS（不回归）。

Run: `cd backend && uv run ruff check app/worker.py`
Expected: `All checks passed!`

- [ ] **Step 5: 追加第 1 层接入测试**

在 `backend/tests/test_agent_api.py` 顶部导入区追加：

```python
from app.models.workflow import (
    Workflow,
    WorkflowApproval,
    WorkflowRun,
    WorkflowVersion,
)
```

在文件末尾追加：

```python
async def make_workflow_child(
    db_session,
    *,
    creator,
    trigger="manual",
    parent_status="waiting_approval",
):
    triggered_by = creator.id if trigger == "manual" else None
    workflow = Workflow(
        tenant_id=creator.tenant_id, name="审批流", created_by=creator.id
    )
    db_session.add(workflow)
    await db_session.flush()
    version = WorkflowVersion(
        tenant_id=creator.tenant_id,
        workflow_id=workflow.id,
        version=1,
        graph={},
        source_revision=0,
        published_by=creator.id,
        published_at=datetime.now(UTC),
    )
    db_session.add(version)
    await db_session.flush()
    parent = WorkflowRun(
        tenant_id=creator.tenant_id,
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        trigger=trigger,
        triggered_by=triggered_by,
        status=parent_status,
        inputs={},
        policy={},
    )
    db_session.add(parent)
    await db_session.flush()
    child = AgentRun(
        tenant_id=creator.tenant_id,
        user_id=creator.id,
        goal="子任务",
        mode="node",
        status="waiting_approval",
        budget={},
        policy={},
        workflow_run_id=parent.id,
        workflow_node_id="writer",
    )
    db_session.add(child)
    await db_session.flush()
    step = AgentStep(
        tenant_id=creator.tenant_id,
        run_id=child.id,
        step_index=1,
        kind="approval",
        tool_name="project.create",
        status="skipped",
        input={},
    )
    db_session.add(step)
    await db_session.flush()
    approval = AgentApproval(
        tenant_id=creator.tenant_id,
        run_id=child.id,
        step_id=step.id,
        tool_name="project.create",
        risk="low",
        payload={"title": "草稿"},
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    db_session.add(approval)
    await db_session.commit()
    return parent, child, approval


async def test_child_approval_wakes_parent_workflow(api, queue, db_session):
    http, state = api
    creator = await make_user(db_session, role="editor")
    reviewer = await make_user(db_session, role="owner")
    parent, child, approval = await make_workflow_child(
        db_session, creator=creator
    )
    state["user"] = reviewer

    resp = await http.post(
        f"/api/agent/approvals/{approval.id}/decide",
        json={"decision": "approve"},
    )

    assert resp.status_code == 200, resp.text
    await db_session.refresh(child)
    await db_session.refresh(parent)
    assert child.status == "queued"
    assert parent.status == "queued"
    assert queue.jobs == [("run_workflow_job", str(parent.id))]


async def test_child_approval_waits_for_sibling_approval(
    api, queue, db_session
):
    http, state = api
    creator = await make_user(db_session, role="editor")
    reviewer = await make_user(db_session, role="owner")
    parent, _child, approval = await make_workflow_child(
        db_session, creator=creator
    )
    db_session.add(
        WorkflowApproval(
            tenant_id=creator.tenant_id,
            run_id=parent.id,
            node_id="review",
            kind="review",
            title="审核",
            payload={},
            status="pending",
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
    )
    await db_session.commit()
    state["user"] = reviewer

    resp = await http.post(
        f"/api/agent/approvals/{approval.id}/decide",
        json={"decision": "approve"},
    )

    assert resp.status_code == 200, resp.text
    await db_session.refresh(parent)
    assert parent.status == "waiting_approval"
    # 父运行仍是唯一执行者：兄弟审批未决时什么都不入队。
    assert queue.jobs == []


async def test_child_approval_rolls_back_when_parent_enqueue_fails(
    api, db_session
):
    http, state = api
    creator = await make_user(db_session, role="editor")
    reviewer = await make_user(db_session, role="owner")
    parent, child, approval = await make_workflow_child(
        db_session, creator=creator
    )
    state["user"] = reviewer
    app.dependency_overrides[get_queue] = lambda: DownQueue()

    resp = await http.post(
        f"/api/agent/approvals/{approval.id}/decide",
        json={"decision": "approve"},
    )

    assert resp.status_code == 503
    await db_session.refresh(parent)
    await db_session.refresh(child)
    await db_session.refresh(approval)
    assert parent.status == "waiting_approval"
    assert child.status == "waiting_approval"
    assert approval.status == "pending"


async def test_child_rejection_fails_parent_workflow(api, queue, db_session):
    http, state = api
    creator = await make_user(db_session, role="editor")
    reviewer = await make_user(db_session, role="owner")
    parent, child, approval = await make_workflow_child(
        db_session, creator=creator
    )
    state["user"] = reviewer

    resp = await http.post(
        f"/api/agent/approvals/{approval.id}/decide",
        json={"decision": "reject"},
    )

    assert resp.status_code == 200, resp.text
    await db_session.refresh(child)
    await db_session.refresh(parent)
    assert child.status == "failed"
    assert child.error_code == "approval_rejected"
    assert parent.status == "failed"
    assert parent.error_code == "approval_rejected"
    assert queue.jobs == []


async def test_scheduled_child_approval_visible_to_its_creator(
    api, queue, db_session
):
    http, state = api
    creator = await make_user(db_session, role="owner")
    parent, _child, approval = await make_workflow_child(
        db_session, creator=creator, trigger="schedule"
    )
    state["user"] = creator

    listing = await http.get("/api/agent/approvals")
    approved = await http.post(
        f"/api/agent/approvals/{approval.id}/decide",
        json={"decision": "approve"},
    )

    assert listing.status_code == 200, listing.text
    assert [row["id"] for row in listing.json()] == [str(approval.id)]
    # 定时运行没有发起人：创建人自己也能审批，否则单管理员租户必然卡到过期。
    assert approved.status_code == 200, approved.text
    await db_session.refresh(parent)
    assert parent.status == "queued"
    assert queue.jobs == [("run_workflow_job", str(parent.id))]


async def test_manual_child_approval_still_excludes_its_creator(
    api, queue, db_session
):
    http, state = api
    creator = await make_user(db_session, role="owner")
    _parent, _child, approval = await make_workflow_child(
        db_session, creator=creator, trigger="manual"
    )
    state["user"] = creator

    listing = await http.get("/api/agent/approvals")
    decided = await http.post(
        f"/api/agent/approvals/{approval.id}/decide",
        json={"decision": "approve"},
    )

    assert listing.status_code == 200, listing.text
    assert listing.json() == []
    assert decided.status_code == 403
    assert queue.jobs == []


async def test_agent_approval_list_keeps_independent_runs(
    api, queue, db_session
):
    http, state = api
    creator = await make_user(db_session, role="editor")
    reviewer = await make_user(db_session, role="owner")
    _parent, _child, workflow_approval = await make_workflow_child(
        db_session, creator=creator
    )
    independent = await AgentRun(
        tenant_id=creator.tenant_id,
        user_id=creator.id,
        goal="独立任务",
        status="waiting_approval",
        budget={},
        policy={},
    )
    db_session.add(independent)
    await db_session.flush()
    step = AgentStep(
        tenant_id=creator.tenant_id,
        run_id=independent.id,
        step_index=1,
        kind="approval",
        tool_name="project.create",
        status="skipped",
        input={},
    )
    db_session.add(step)
    await db_session.flush()
    standalone = AgentApproval(
        tenant_id=creator.tenant_id,
        run_id=independent.id,
        step_id=step.id,
        tool_name="project.create",
        risk="low",
        payload={},
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    db_session.add(standalone)
    await db_session.commit()
    state["user"] = reviewer

    listing = await http.get("/api/agent/approvals")

    assert listing.status_code == 200, listing.text
    ids = {row["id"] for row in listing.json()}
    assert ids == {str(standalone.id), str(workflow_approval.id)}
```

- [ ] **Step 6: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_agent_api.py -q`
Expected: FAIL（`test_child_approval_wakes_parent_workflow` 断言父运行仍 `waiting_approval`，因为还没有工作流分支；`test_manual_child_approval_still_excludes_its_creator` 之外的用例至少 4 个失败）。

- [ ] **Step 7: 修改第 1 层**

1）`backend/app/agent/runtime.py`：导入区追加 `from app.models.workflow import WorkflowRun`；在 `decide_approval` 之前加：

```python
async def _has_initiator(session: AsyncSession, run: AgentRun) -> bool:
    """Whether this run was started by a person who must not self-approve.

    Scheduled workflow runs have no initiator (``triggered_by is None``), so
    their creator may review the child run they started on the scheduler's
    behalf; only manual workflow and independent runs exclude the owner.
    """
    if run.workflow_run_id is None:
        return True
    parent = await session.get(WorkflowRun, run.workflow_run_id)
    return parent is not None and parent.triggered_by is not None
```

把 `decide_approval` 里的：

```python
    if actor.id == run.user_id:
        raise AppError("forbidden", "不能审批自己发起的运行", 403)
```

替换为：

```python
    if actor.id == run.user_id and await _has_initiator(session, run):
        raise AppError("forbidden", "不能审批自己发起的运行", 403)
```

2）`backend/app/api/agent.py`：导入区把 `from sqlalchemy import func, select, update` 改为 `from sqlalchemy import func, or_, select, update`；追加：

```python
from app.models.workflow import WorkflowRun
from app.workflow import approvals as workflow_approvals
from app.workflow import lifecycle
```

并把 runtime 导入改为：

```python
from app.agent.runtime import (
    APPROVAL_REJECTED,
    decide_approval,
    fail_run,
)
```

3）在 `_enqueue` 之后加：

```python
async def _enqueue_workflow(queue: ArqRedis | None, run_id: uuid.UUID) -> None:
    if queue is None:
        raise AppError("queue_unavailable", QUEUE_UNAVAILABLE_MESSAGE, 503)
    try:
        await queue.enqueue_job("run_workflow_job", str(run_id))
    except Exception:
        logger.exception("workflow enqueue failed run=%s", run_id)
        raise AppError(
            "queue_unavailable", QUEUE_UNAVAILABLE_MESSAGE, 503
        ) from None


async def _restore_child_wait(
    session: AsyncSession,
    parent: WorkflowRun,
    child: AgentRun,
    approval: AgentApproval,
) -> None:
    """Undo a woke parent whose worker never saw the job.

    All three rows are restored in one transaction and only while each still
    holds the value this request wrote, so a concurrent cancel or a sibling
    decision is never resurrected.
    """
    reset_parent = await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == parent.id,
            WorkflowRun.tenant_id == parent.tenant_id,
            WorkflowRun.status == "queued",
        )
        .values(
            status="waiting_approval",
            finished_at=None,
            error_code="",
            error_message="",
        )
        .execution_options(synchronize_session=False)
    )
    reset_child = await session.execute(
        update(AgentRun)
        .where(
            AgentRun.id == child.id,
            AgentRun.tenant_id == child.tenant_id,
            AgentRun.status == "queued",
        )
        .values(
            status="waiting_approval",
            finished_at=None,
            error_code="",
            error_message="",
        )
        .execution_options(synchronize_session=False)
    )
    reset_approval = await session.execute(
        update(AgentApproval)
        .where(
            AgentApproval.id == approval.id,
            AgentApproval.tenant_id == approval.tenant_id,
            AgentApproval.status == "approved",
        )
        .values(status="pending", decided_by=None, decided_at=None, note="")
        .execution_options(synchronize_session=False)
    )
    if (
        reset_parent.rowcount
        and reset_child.rowcount
        and reset_approval.rowcount
    ):
        await session.commit()
        return
    await session.rollback()
    logger.warning(
        "agent approval compensation skipped (workflow moved on) run=%s approval=%s",
        child.id,
        approval.id,
    )


async def _wake_parent_workflow(
    session: AsyncSession,
    queue: ArqRedis | None,
    child: AgentRun,
    approval: AgentApproval,
) -> None:
    """Wake the parent workflow that owns ``child`` -- never the child itself.

    The parent resumes its children inline, so enqueueing the child as well
    would let two executors run the same child. While a sibling approval is
    still pending the parent keeps waiting; whoever decides the last one wakes
    it, and the parent then resumes every approved child.
    """
    parent = await session.get(WorkflowRun, child.workflow_run_id)
    if parent is None or parent.tenant_id != child.tenant_id:
        raise AppError("not_found", "运行不存在", 404)
    if await workflow_approvals.has_pending(
        session, tenant_id=parent.tenant_id, run_id=parent.id
    ):
        return
    moment = datetime.now(UTC)
    resumed = await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == parent.id,
            WorkflowRun.tenant_id == parent.tenant_id,
            WorkflowRun.status == "waiting_approval",
        )
        .values(
            status="queued",
            heartbeat_at=moment,
            finished_at=None,
            error_code="",
            error_message="",
        )
        .execution_options(synchronize_session=False)
    )
    if resumed.rowcount == 0:
        # 父运行已被取消、已被另一个审批唤醒或已终态：它的写入者负责子运行。
        await session.rollback()
        return
    await session.commit()
    try:
        await _enqueue_workflow(queue, parent.id)
    except AppError:
        await _restore_child_wait(session, parent, child, approval)
        raise
```

4）`list_approvals` 的查询改为外连接：

```python
    rows = (
        await session.execute(
            select(AgentApproval)
            .join(AgentRun, AgentRun.id == AgentApproval.run_id)
            .outerjoin(
                WorkflowRun, WorkflowRun.id == AgentRun.workflow_run_id
            )
            .where(
                AgentApproval.tenant_id == user.tenant_id,
                AgentApproval.status == "pending",
                or_(
                    AgentRun.user_id != user.id,
                    WorkflowRun.trigger == "schedule",
                ),
            )
            .order_by(AgentApproval.created_at, AgentApproval.id)
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
```

5）`decide` 的审批后处理改为：

```python
    approval = await _owned_approval(session, approval_id, user)
    run = await decide_approval(
        session,
        approval,
        actor=user,
        decision=payload.decision,
        note=payload.note,
    )
    if payload.decision == "approve":
        if run.workflow_run_id is not None:
            # 父工作流是子运行的唯一执行者：只唤醒父运行。
            await _wake_parent_workflow(session, queue, run, approval)
            return _run_out(run)
        try:
            await _enqueue(queue, run)
        except AppError:
            # The consent was committed before the worker could be reached.
            # Roll it back so the call did not "run" and a re-decide is
            # possible; a 503 tells the reviewer to retry.
            await _compensate_approval(session, approval, run)
            raise
    elif run.workflow_run_id is not None:
        # 子运行已由第 1 层落 failed/approval_rejected；这里终结父运行，
        # 保证没有审批或子运行比父运行活得更久。
        await lifecycle.fail_waiting_run(
            session,
            run.workflow_run_id,
            error_code="approval_rejected",
            message=APPROVAL_REJECTED,
            actor=str(user.id),
        )
    return _run_out(run)
```

- [ ] **Step 8: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_agent_api.py -q`
Expected: PASS（48 passed：原有 41 + 新增 7）。

Run: `cd backend && uv run pytest tests/test_agent_worker.py tests/test_agent_runtime.py -q`
Expected: PASS（自批校验调整不回归）。

Run: `cd backend && uv run ruff check app/worker.py app/api/agent.py app/agent/runtime.py`
Expected: `All checks passed!`

- [ ] **Step 9: 提交**

```bash
git add backend/app/worker.py backend/app/api/agent.py backend/app/agent/runtime.py backend/tests/test_workflow_worker.py backend/tests/test_agent_api.py
git commit -m "feat(workflow): add workflow worker and agent approval integration"
```

---

## Task 13: 前端类型与纯函数

**Files:**
- Create: `web/src/types/workflow.ts`
- Create: `web/src/workflows.ts`
- Test: `web/src/__tests__/workflow.test.ts`

**背景与接口**

编辑器与运行详情共用的 API 类型集中在 `web/src/types/workflow.ts`，全部对齐后端 `WorkflowOut`/`WorkflowDetailOut`/`VersionOut`/`ScheduleOut`/`WorkflowRunOut`/`NodeRunOut`/`ApprovalOut`（见 Task 9–11 的 Pydantic 模型）。可测纯函数放 `web/src/workflows.ts`，供画布、属性面板、列表与详情页复用；图校验不在前端重复实现。

**关键约定**

- `cadence.weekdays` 用 ISO 星期（周一 `1` … 周日 `7`），与 `app/workflow/schedule.py` 一致；`daily` 的 `weekdays` 恒为 `[]`。
- 图节点 `data` 是 `Record<string, unknown>`；所有读取都做运行时收窄，不使用 `as` 强转整个对象。
- 权限助手只表达「界面是否展示按钮」，服务端仍是权威；审批助手与后端 `_can_control`/自批守卫保持一致：定时运行对所有管理员可见，手动运行对发起人不可见。
- 状态映射遇到未知值回退原字符串（沿用 `web/src/agent.ts` 的写法），避免后端新增状态导致页面出现空白。
- 规模上限与后端校验一致：节点 ≤50、边 ≤100；达到 90% 时提示接近上限。

- [ ] **Step 1: 写失败测试**

新建 `web/src/__tests__/workflow.test.ts`：

```ts
import { describe, expect, it } from "vitest";
import {
  approvalSourceLabel,
  canCancelWorkflowRun,
  canDecideWorkflowApproval,
  canEditWorkflow,
  canResumeWorkflowRun,
  canRunWorkflow,
  conditionTargets,
  emptyGraph,
  graphScale,
  inputFields,
  isRunTerminal,
  nodeKindLabel,
  nodeLabel,
  nodeStatusBadge,
  nodeStatusLabel,
  reachableNodes,
  runStatusLabel,
  scheduleText,
  topoOrder,
  triggerLabel,
  upstreamNodeIds,
  variableCandidates,
} from "../workflows";
import type {
  Graph,
  WorkflowApproval,
  WorkflowRun,
  WorkflowSchedule,
} from "../types/workflow";

const graph: Graph = {
  nodes: [
    { id: "in", type: "input", data: { label: "输入", fields: [{ key: "topic", label: "主题", type: "string", required: true }] } },
    { id: "a", type: "tool", data: { label: "读画像", tool: "profile.list", args: {}, retry: 1 } },
    { id: "b", type: "agent", data: { label: "写笔记", goal: "写一版", tools: [], budget: {}, retry: 0 } },
    { id: "cond", type: "condition", data: { label: "是否通过", left: "{{nodes.b.output.result_text}}", operator: "contains", value: "通过" } },
    { id: "yes", type: "tool", data: { label: "建草稿", tool: "publish.create_draft_job", args: {}, retry: 0 } },
    { id: "no", type: "tool", data: { label: "记录失败", tool: "profile.list", args: {}, retry: 0 } },
    { id: "out", type: "output", data: { label: "输出" } },
  ],
  edges: [
    { id: "e1", source: "in", target: "a" },
    { id: "e2", source: "a", target: "b" },
    { id: "e3", source: "b", target: "cond" },
    { id: "e4", source: "cond", sourceHandle: "true", target: "yes" },
    { id: "e5", source: "cond", sourceHandle: "false", target: "no" },
    { id: "e6", source: "yes", target: "out" },
    { id: "e7", source: "no", target: "out" },
  ],
};

const viewer = { id: "u1", role: "viewer" as const };
const editor = { id: "u2", role: "editor" as const };
const admin = { id: "u3", role: "admin" as const };

function run(overrides: Partial<WorkflowRun> = {}): WorkflowRun {
  return {
    id: "r1",
    workflow_id: "w1",
    workflow_version_id: "v1",
    version: 1,
    trigger: "manual",
    triggered_by: "u2",
    schedule_id: null,
    scheduled_for: null,
    status: "queued",
    inputs: {},
    result: {},
    error_code: "",
    error_message: "",
    usage: {},
    attempts: 0,
    resume_count: 0,
    created_at: null,
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

function approval(overrides: Partial<WorkflowApproval> = {}): WorkflowApproval {
  return {
    id: "ap1",
    run_id: "r1",
    workflow_id: "w1",
    source: "workflow",
    kind: "node",
    node_id: "rev",
    tool_name: "",
    risk: "low",
    title: "确认草稿",
    payload: {},
    status: "pending",
    note: "",
    expires_at: null,
    created_at: null,
    ...overrides,
  };
}

describe("图结构纯函数", () => {
  it("收集上游节点集合（不含自身）", () => {
    expect([...upstreamNodeIds(graph, "b")].sort()).toEqual(["a", "in"]);
    expect(upstreamNodeIds(graph, "in").size).toBe(0);
  });

  it("拓扑序满足依赖，有环时返回 null", () => {
    const order = topoOrder(graph);
    expect(order).not.toBeNull();
    expect(order!.indexOf("in")).toBeLessThan(order!.indexOf("a"));
    expect(order!.indexOf("b")).toBeLessThan(order!.indexOf("cond"));

    const cyclic: Graph = {
      ...graph,
      edges: [...graph.edges, { id: "e8", source: "out", target: "a" }],
    };
    expect(topoOrder(cyclic)).toBeNull();
  });

  it("条件节点的两个出边句柄", () => {
    expect(conditionTargets(graph, "cond")).toEqual({ yes: "yes", no: "no" });
    expect(conditionTargets(graph, "a")).toEqual({ yes: null, no: null });
  });

  it("按条件决策计算可达节点", () => {
    const pass = reachableNodes(graph, { cond: true });
    expect(pass.has("yes")).toBe(true);
    expect(pass.has("no")).toBe(false);
    expect(pass.has("out")).toBe(true);

    const fail = reachableNodes(graph, { cond: false });
    expect(fail.has("no")).toBe(true);
    expect(fail.has("yes")).toBe(false);
  });

  it("变量候选只包含输入字段与上游节点", () => {
    const options = variableCandidates(graph, "b", inputFields(graph));
    const values = options.map((item) => item.value);
    expect(values).toContain("{{inputs.topic}}");
    expect(values).toContain("{{nodes.a.output}}");
    expect(values).toContain("{{nodes.a.output.result_text}}");
    expect(values.some((value) => value.startsWith("{{nodes.b."))).toBe(false);
    expect(values.some((value) => value.startsWith("{{nodes.cond."))).toBe(false);
    expect(options.find((item) => item.value === "{{inputs.topic}}")?.label).toBe(
      "输入 · 主题",
    );
  });

  it("空图保留输入与输出节点且没有边", () => {
    const blank = emptyGraph();
    expect(blank.nodes.map((node) => node.type)).toEqual(["input", "output"]);
    expect(blank.edges).toEqual([]);
    expect(inputFields(blank)).toEqual([]);
  });

  it("规模提示在 90% 与超限时升级", () => {
    const make = (nodes: number, edges: number): Graph => ({
      nodes: Array.from({ length: nodes }, (_, index) => ({
        id: `n${index}`,
        type: "tool",
        data: {},
      })),
      edges: Array.from({ length: edges }, (_, index) => ({
        id: `e${index}`,
        source: "n0",
        target: "n1",
      })),
    });
    expect(graphScale(make(5, 5)).level).toBe("ok");
    expect(graphScale(make(45, 10)).level).toBe("warn");
    expect(graphScale(make(51, 10)).level).toBe("over");
    expect(graphScale(make(10, 91)).level).toBe("over");
    expect(graphScale(make(5, 5)).text).toBe("5/50 节点 · 5/100 边");
  });
});

describe("标签与状态", () => {
  it("节点类型与显示名", () => {
    expect(nodeKindLabel("agent")).toBe("Agent");
    expect(nodeKindLabel("unknown")).toBe("unknown");
    expect(nodeLabel({ id: "n1", type: "tool", data: {} })).toBe("n1");
    expect(nodeLabel({ id: "n1", type: "tool", data: { label: "读画像" } })).toBe(
      "读画像",
    );
  });

  it("节点状态标签与徽标回退", () => {
    expect(nodeStatusLabel("ok")).toBe("成功");
    expect(nodeStatusLabel("skipped")).toBe("已跳过");
    expect(nodeStatusLabel("mystery")).toBe("mystery");
    expect(nodeStatusBadge("error")).toContain("wine");
    expect(nodeStatusBadge("running")).toContain("sage");
  });

  it("运行状态、触发方式与审批来源", () => {
    expect(runStatusLabel("waiting_approval")).toBe("待审核");
    expect(runStatusLabel("succeeded")).toBe("已完成");
    expect(triggerLabel("schedule")).toBe("定时");
    expect(triggerLabel("manual")).toBe("手动");
    expect(approvalSourceLabel("workflow")).toBe("工作流审核");
    expect(approvalSourceLabel("agent")).toBe("Agent 工具确认");
  });

  it("终态判定", () => {
    expect(isRunTerminal("succeeded")).toBe(true);
    expect(isRunTerminal("failed")).toBe(true);
    expect(isRunTerminal("cancelled")).toBe(true);
    expect(isRunTerminal("running")).toBe(false);
    expect(isRunTerminal("waiting_approval")).toBe(false);
  });
});

describe("权限助手", () => {
  it("只有 admin/owner 能编辑工作流", () => {
    expect(canEditWorkflow("editor")).toBe(false);
    expect(canEditWorkflow("admin")).toBe(true);
    expect(canEditWorkflow("owner")).toBe(true);
  });

  it("运行需要写权限且已有发布版本", () => {
    expect(canRunWorkflow("editor", true)).toBe(true);
    expect(canRunWorkflow("editor", false)).toBe(false);
    expect(canRunWorkflow("viewer", true)).toBe(false);
  });

  it("手动运行排除发起人，定时运行管理员可见", () => {
    expect(canDecideWorkflowApproval(approval(), run(), admin)).toBe(true);
    expect(canDecideWorkflowApproval(approval(), run(), editor)).toBe(false);
    expect(
      canDecideWorkflowApproval(approval(), run({ triggered_by: "u3" }), admin),
    ).toBe(false);
    expect(
      canDecideWorkflowApproval(
        approval(),
        run({ trigger: "schedule", triggered_by: null }),
        admin,
      ),
    ).toBe(true);
    expect(
      canDecideWorkflowApproval(approval({ status: "approved" }), run(), admin),
    ).toBe(false);
  });

  it("取消与续跑的归属规则", () => {
    expect(canCancelWorkflowRun(run(), editor)).toBe(true);
    expect(canCancelWorkflowRun(run(), admin)).toBe(false);
    expect(
      canCancelWorkflowRun(run({ trigger: "schedule", triggered_by: null }), admin),
    ).toBe(true);
    expect(canCancelWorkflowRun(run({ status: "succeeded" }), editor)).toBe(false);

    expect(canResumeWorkflowRun(run({ status: "failed" }), editor)).toBe(true);
    expect(canResumeWorkflowRun(run({ status: "failed", resume_count: 10 }), editor)).toBe(
      false,
    );
    expect(canResumeWorkflowRun(run({ status: "running" }), editor)).toBe(false);
    expect(
      canResumeWorkflowRun(
        run({ status: "failed", trigger: "schedule", triggered_by: null }),
        editor,
      ),
    ).toBe(false);
  });
});

describe("定时文案", () => {
  const base = {
    id: "s1",
    workflow_version_id: "v1",
    inputs: {},
    enabled: true,
    next_run_at: null,
    last_run_at: null,
  };

  it("每日与每周", () => {
    const daily: WorkflowSchedule = {
      ...base,
      cadence: { kind: "daily", weekdays: [], time: "09:00", timezone: "Asia/Shanghai" },
    };
    const weekly: WorkflowSchedule = {
      ...base,
      cadence: {
        kind: "weekly",
        weekdays: [4, 1],
        time: "08:30",
        timezone: "Asia/Shanghai",
      },
    };
    expect(scheduleText(daily)).toBe("每天 09:00（Asia/Shanghai）");
    expect(scheduleText(weekly)).toBe("每周一、四 08:30（Asia/Shanghai）");
  });

  it("缺少时区时不显示括号", () => {
    const schedule: WorkflowSchedule = {
      ...base,
      cadence: { kind: "daily", weekdays: [], time: "07:05", timezone: "" },
    };
    expect(scheduleText(schedule)).toBe("每天 07:05");
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd web && npm test -- workflow
```

Expected: FAIL，错误为 `Failed to resolve import "../workflows"`（文件尚未创建）。

- [ ] **Step 3: 写类型文件**

新建 `web/src/types/workflow.ts`：

```ts
/** 图数据与后端 `app/workflow/graph.py` 的 JSON 形状一致。 */
export interface GraphNode {
  id: string;
  type: string;
  position?: { x: number; y: number };
  data: Record<string, unknown>;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  sourceHandle?: string | null;
}

export interface Graph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface InputField {
  key: string;
  label?: string;
  type: "string" | "number" | "boolean";
  required?: boolean;
  maxLength?: number;
}

export interface Cadence {
  kind: "daily" | "weekly" | string;
  weekdays?: number[];
  time?: string;
  timezone?: string;
}

export interface WorkflowLastRun {
  id: string;
  status: string;
  created_at: string | null;
}

export interface WorkflowSummary {
  id: string;
  name: string;
  description: string;
  created_by: string;
  draft_revision: number;
  has_unpublished: boolean;
  latest_version: number | null;
  next_run_at: string | null;
  last_run: WorkflowLastRun | null;
  created_at: string | null;
}

export interface WorkflowVersionSummary {
  id: string;
  version: number;
  source_revision: number;
  published_by: string;
  published_at: string;
  note: string;
}

export interface WorkflowVersionDetail extends WorkflowVersionSummary {
  graph: Graph;
}

export interface WorkflowSchedule {
  id: string;
  workflow_version_id: string;
  cadence: Cadence;
  inputs: Record<string, unknown>;
  enabled: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
}

export interface WorkflowDetail extends WorkflowSummary {
  draft_graph: Graph | null;
  latest_published: WorkflowVersionSummary | null;
  schedules: WorkflowSchedule[];
}

export interface WorkflowRun {
  id: string;
  workflow_id: string;
  workflow_version_id: string;
  version: number;
  trigger: string;
  triggered_by: string | null;
  schedule_id: string | null;
  scheduled_for: string | null;
  status: string;
  inputs: Record<string, unknown>;
  result: Record<string, unknown>;
  error_code: string;
  error_message: string;
  usage: Partial<
    Record<
      | "steps"
      | "tool_calls"
      | "prompt_tokens"
      | "completion_tokens"
      | "elapsed_ms"
      | "paused_ms",
      number
    >
  >;
  attempts: number;
  resume_count: number;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface WorkflowNodeRun {
  node_id: string;
  attempt: number;
  kind: string;
  status: string;
  tool_step_index: number | null;
  agent_run_id: string | null;
  input: Record<string, unknown>;
  output: Record<string, unknown>;
  error: string;
  duration_ms: number;
  started_at: string | null;
  finished_at: string | null;
}

export interface WorkflowApproval {
  id: string;
  run_id: string;
  workflow_id: string;
  source: "workflow" | "agent" | string;
  kind: string;
  node_id: string;
  tool_name: string;
  risk: string;
  title: string;
  payload: Record<string, unknown>;
  status: string;
  note: string;
  expires_at: string | null;
  created_at: string | null;
}

export interface WorkflowRunDetail {
  run: WorkflowRun;
  node_runs: WorkflowNodeRun[];
  approvals: WorkflowApproval[];
  result: Record<string, unknown>;
}

export interface ValidationIssue {
  code: string;
  node_id?: string;
  message: string;
}

export interface ValidationResult {
  ok: boolean;
  issues: ValidationIssue[];
}

export interface ToolSpec {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  risk: string;
}

export interface TemplateSummary {
  key: string;
  name: string;
  description: string;
  parameters: Record<string, unknown>[];
}
```

- [ ] **Step 4: 写纯函数**

新建 `web/src/workflows.ts`：

```ts
import { canWrite, isAdmin, type Me, type Role } from "./auth";
import type {
  Cadence,
  Graph,
  GraphNode,
  InputField,
  WorkflowApproval,
  WorkflowRun,
  WorkflowSchedule,
} from "./types/workflow";

const NODE_KIND_LABELS: Record<string, string> = {
  input: "输入",
  tool: "工具",
  agent: "Agent",
  condition: "条件",
  review: "审核",
  output: "输出",
};

const NODE_STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "执行中",
  ok: "成功",
  error: "失败",
  denied: "被拒绝",
  skipped: "已跳过",
  interrupted: "已中断",
};

const NODE_STATUS_BADGES: Record<string, string> = {
  queued: "badge bg-line text-ink-500",
  running: "badge bg-sage-50 text-sage-700",
  ok: "badge bg-sage-50 text-sage-700",
  error: "badge bg-wine-50 text-wine-700",
  denied: "badge bg-wine-50 text-wine-700",
  skipped: "badge bg-line text-ink-300",
  interrupted: "badge bg-surface text-warm-600 border border-line",
};

const RUN_STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "执行中",
  waiting_approval: "待审核",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const TRIGGER_LABELS: Record<string, string> = {
  manual: "手动",
  schedule: "定时",
};

const SOURCE_LABELS: Record<string, string> = {
  workflow: "工作流审核",
  agent: "Agent 工具确认",
};

const WEEKDAY_LABELS: Record<number, string> = {
  1: "周一",
  2: "周二",
  3: "周三",
  4: "周四",
  5: "周五",
  6: "周六",
  7: "周日",
};

export function nodeKindLabel(kind: string): string {
  return NODE_KIND_LABELS[kind] ?? kind;
}

export function nodeStatusLabel(status: string): string {
  return NODE_STATUS_LABELS[status] ?? status;
}

export function nodeStatusBadge(status: string): string {
  return NODE_STATUS_BADGES[status] ?? "badge bg-line text-ink-500";
}

export function runStatusLabel(status: string): string {
  return RUN_STATUS_LABELS[status] ?? status;
}

export function triggerLabel(trigger: string): string {
  return TRIGGER_LABELS[trigger] ?? trigger;
}

export function approvalSourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source;
}

export function nodeLabel(node: GraphNode): string {
  const label = node.data.label;
  return typeof label === "string" && label.trim() ? label : node.id;
}

function isInputField(value: unknown): value is InputField {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return typeof record.key === "string" && record.key.length > 0;
}

export function inputFields(graph: Graph): InputField[] {
  const input = graph.nodes.find((node) => node.type === "input");
  const fields = input?.data.fields;
  if (!Array.isArray(fields)) return [];
  return fields.filter(isInputField);
}

/** 空图给编辑器一个可拖拽的起点；连线由用户补。 */
export function emptyGraph(): Graph {
  return {
    nodes: [
      {
        id: "in",
        type: "input",
        position: { x: 80, y: 160 },
        data: {
          label: "输入",
          fields: [{ key: "topic", label: "主题", type: "string", required: true }],
        },
      },
      {
        id: "out",
        type: "output",
        position: { x: 640, y: 160 },
        data: { label: "输出" },
      },
    ],
    edges: [],
  };
}

export function upstreamNodeIds(graph: Graph, nodeId: string): Set<string> {
  const incoming = new Map<string, string[]>();
  for (const edge of graph.edges) {
    const list = incoming.get(edge.target);
    if (list) list.push(edge.source);
    else incoming.set(edge.target, [edge.source]);
  }
  const seen = new Set<string>();
  const queue = [...(incoming.get(nodeId) ?? [])];
  while (queue.length > 0) {
    const id = queue.pop()!;
    if (id === nodeId || seen.has(id)) continue;
    seen.add(id);
    queue.push(...(incoming.get(id) ?? []));
  }
  return seen;
}

export function topoOrder(graph: Graph): string[] | null {
  const ids = graph.nodes.map((node) => node.id);
  const indegree = new Map<string, number>(ids.map((id) => [id, 0]));
  const outgoing = new Map<string, string[]>();
  for (const edge of graph.edges) {
    if (!indegree.has(edge.source) || !indegree.has(edge.target)) continue;
    indegree.set(edge.target, (indegree.get(edge.target) ?? 0) + 1);
    const list = outgoing.get(edge.source);
    if (list) list.push(edge.target);
    else outgoing.set(edge.source, [edge.target]);
  }
  const queue = ids.filter((id) => indegree.get(id) === 0);
  const order: string[] = [];
  while (queue.length > 0) {
    const id = queue.shift()!;
    order.push(id);
    for (const next of outgoing.get(id) ?? []) {
      const left = (indegree.get(next) ?? 0) - 1;
      indegree.set(next, left);
      if (left === 0) queue.push(next);
    }
  }
  return order.length === ids.length ? order : null;
}

export function conditionTargets(
  graph: Graph,
  nodeId: string,
): { yes: string | null; no: string | null } {
  const yes = graph.edges.find(
    (edge) => edge.source === nodeId && edge.sourceHandle === "true",
  );
  const no = graph.edges.find(
    (edge) => edge.source === nodeId && edge.sourceHandle === "false",
  );
  return { yes: yes?.target ?? null, no: no?.target ?? null };
}

/** 从输入节点出发，条件节点只沿被选中的句柄前进。 */
export function reachableNodes(
  graph: Graph,
  decisions: Record<string, boolean>,
): Set<string> {
  const byId = new Map(graph.nodes.map((node) => [node.id, node]));
  const input = graph.nodes.find((node) => node.type === "input");
  const seen = new Set<string>();
  if (!input) return seen;
  const queue = [input.id];
  while (queue.length > 0) {
    const id = queue.pop()!;
    if (seen.has(id)) continue;
    seen.add(id);
    const node = byId.get(id);
    if (node?.type === "condition") {
      const targets = conditionTargets(graph, id);
      const next = decisions[id] ? targets.yes : targets.no;
      if (next) queue.push(next);
      continue;
    }
    for (const edge of graph.edges) {
      if (edge.source === id) queue.push(edge.target);
    }
  }
  return seen;
}

export interface VariableOption {
  value: string;
  label: string;
}

/** 属性面板的变量提示：输入字段 + 全部上游节点的整体输出与文本。 */
export function variableCandidates(
  graph: Graph,
  nodeId: string,
  fields: InputField[] = [],
): VariableOption[] {
  const options: VariableOption[] = fields.map((field) => ({
    value: `{{inputs.${field.key}}}`,
    label: `输入 · ${field.label || field.key}`,
  }));
  const upstream = upstreamNodeIds(graph, nodeId);
  for (const node of graph.nodes) {
    if (!upstream.has(node.id) || node.type === "input" || node.type === "output") {
      continue;
    }
    const name = nodeLabel(node);
    options.push({
      value: `{{nodes.${node.id}.output}}`,
      label: `${name} · 整体输出`,
    });
    options.push({
      value: `{{nodes.${node.id}.output.result_text}}`,
      label: `${name} · 文本`,
    });
  }
  return options;
}

export function canEditWorkflow(role: Role): boolean {
  return isAdmin(role);
}

export function canRunWorkflow(role: Role, hasPublished: boolean): boolean {
  return canWrite(role) && hasPublished;
}

export function canDecideWorkflowApproval(
  approval: Pick<WorkflowApproval, "status">,
  run: Pick<WorkflowRun, "trigger" | "triggered_by">,
  viewer: Pick<Me, "id" | "role">,
): boolean {
  if (approval.status !== "pending" || !isAdmin(viewer.role)) return false;
  if (run.trigger === "schedule") return true;
  return viewer.id !== run.triggered_by;
}

function controlsRun(
  run: Pick<WorkflowRun, "trigger" | "triggered_by">,
  viewer: Pick<Me, "id" | "role">,
): boolean {
  if (viewer.id === run.triggered_by) return true;
  return run.trigger === "schedule" && isAdmin(viewer.role);
}

export function canCancelWorkflowRun(
  run: Pick<WorkflowRun, "status" | "trigger" | "triggered_by">,
  viewer: Pick<Me, "id" | "role">,
): boolean {
  const live =
    run.status === "queued" ||
    run.status === "running" ||
    run.status === "waiting_approval";
  return live && controlsRun(run, viewer);
}

export function canResumeWorkflowRun(
  run: Pick<WorkflowRun, "status" | "trigger" | "triggered_by" | "resume_count">,
  viewer: Pick<Me, "id" | "role">,
): boolean {
  return run.status === "failed" && run.resume_count < 10 && controlsRun(run, viewer);
}

export function isRunTerminal(status: string): boolean {
  return status === "succeeded" || status === "failed" || status === "cancelled";
}

export function scheduleText(schedule: { cadence: Cadence }): string {
  const cadence = schedule.cadence;
  const time = typeof cadence.time === "string" ? cadence.time : "";
  const zone = cadence.timezone ? `（${cadence.timezone}）` : "";
  if (cadence.kind === "weekly") {
    const days = (cadence.weekdays ?? [])
      .slice()
      .sort((a, b) => a - b)
      .map((day) => WEEKDAY_LABELS[day] ?? String(day))
      .join("、");
    return `每${days || "周"} ${time}${zone}`;
  }
  return `每天 ${time}${zone}`;
}

export type ScaleLevel = "ok" | "warn" | "over";

export interface ScaleHint {
  level: ScaleLevel;
  text: string;
}

/** 与后端发布校验一致：节点 ≤50、边 ≤100；90% 起提示。 */
export function graphScale(graph: Graph): ScaleHint {
  const nodes = graph.nodes.length;
  const edges = graph.edges.length;
  const level: ScaleLevel =
    nodes > 50 || edges > 100 ? "over" : nodes >= 45 || edges >= 90 ? "warn" : "ok";
  return { level, text: `${nodes}/50 节点 · ${edges}/100 边` };
}
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd web && npm test -- workflow
```

Expected: PASS（`17 passed`，全部在 `src/__tests__/workflow.test.ts`）。

- [ ] **Step 6: 类型检查**

```bash
cd web && npx tsc -b
```

Expected: 无输出（退出码 0）。

- [ ] **Step 7: 提交**

```bash
git add web/src/types/workflow.ts web/src/workflows.ts web/src/__tests__/workflow.test.ts
git commit -m "feat(web): add workflow API types and pure helpers"
```

---

## Task 14: 工作流列表页、模板与导航

**Files:**
- Modify: `web/src/auth.ts`
- Modify: `web/src/App.tsx`
- Create: `web/src/pages/Workflows.tsx`
- Test: `web/src/__tests__/workflow.test.ts`

**背景与接口**

列表页是 `/workflows` 的入口：展示名称、未发布修改标记、最新版本、下次定时、最近运行与创建人；admin/owner 额外看到「新建工作流」表单与模板库。路由与导航沿用 `App.tsx` 的 `RequireRole` 与 `auth.ts` 的 `NAV_ITEMS`。

**关键约定**

- 导航「工作流」`minRole: "editor"`（editor 可查看列表并运行已发布版本，编辑由页面内按钮收敛）。
- 模板实例化只对 `admin` 及以上展示；失败时显示 `errorText(err)`。
- 列表请求 `GET /api/workflows`，`latest_version` 为 `null` 时显示「未发布」，`has_unpublished` 显示「有未发布修改」徽标。
- 新建成功后跳转 `/workflows/{id}`；模板实例化同样跳转。
- 编辑页与运行详情在后续任务接入，本任务只新增列表路由。

- [ ] **Step 1: 追加失败测试**

在 `web/src/__tests__/workflow.test.ts` 顶部新增一行 `import { visibleNavItems } from "../auth";`，并在文件末尾追加：

```ts
describe("工作流导航", () => {
  it("editor 及以上可见，viewer 不可见", () => {
    const editorItems = visibleNavItems({ role: "editor", is_platform_admin: false });
    expect(editorItems.some((item) => item.to === "/workflows" && item.label === "工作流")).toBe(
      true,
    );
    const viewerItems = visibleNavItems({ role: "viewer", is_platform_admin: false });
    expect(viewerItems.some((item) => item.to === "/workflows")).toBe(false);
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd web && npm test -- workflow
```

Expected: FAIL，`expected false to be true`（`NAV_ITEMS` 里还没有工作流）。

- [ ] **Step 3: 加导航项**

在 `web/src/auth.ts` 的 `NAV_ITEMS` 中，`/assistant` 之后插入：

```ts
  { to: "/workflows", label: "工作流", minRole: "editor" },
```

- [ ] **Step 4: 写列表页**

新建 `web/src/pages/Workflows.tsx`：

```tsx
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../auth-context";
import { api } from "../client";
import { errorText } from "../types";
import type {
  TemplateSummary,
  WorkflowSummary,
} from "../types/workflow";
import { canEditWorkflow, runStatusLabel } from "../workflows";

function timestamp(value: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

export default function Workflows() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { user: me } = useAuth();
  const editable = me ? canEditWorkflow(me.role) : false;
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [templateKey, setTemplateKey] = useState("");
  const [templateName, setTemplateName] = useState("");
  const [error, setError] = useState("");

  const workflows = useQuery({
    queryKey: ["workflows"],
    queryFn: () => api<WorkflowSummary[]>("/workflows"),
  });

  const templates = useQuery({
    queryKey: ["workflow-templates"],
    queryFn: () => api<TemplateSummary[]>("/workflows/templates"),
    enabled: editable,
  });

  const create = useMutation({
    mutationFn: () =>
      api<WorkflowSummary>("/workflows", {
        method: "POST",
        body: JSON.stringify({ name: name.trim(), description: description.trim() }),
      }),
    onSuccess: (workflow) => {
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
      navigate(`/workflows/${workflow.id}`);
    },
    onError: (err) => setError(errorText(err)),
  });

  const instantiate = useMutation({
    mutationFn: () =>
      api<WorkflowSummary>(`/workflows/templates/${templateKey}/instantiate`, {
        method: "POST",
        body: JSON.stringify({ name: templateName.trim() }),
      }),
    onSuccess: (workflow) => {
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
      navigate(`/workflows/${workflow.id}`);
    },
    onError: (err) => setError(errorText(err)),
  });

  function submitCreate(event: FormEvent) {
    event.preventDefault();
    if (!name.trim()) {
      setError("请填写工作流名称");
      return;
    }
    setError("");
    create.mutate();
  }

  function submitTemplate(event: FormEvent) {
    event.preventDefault();
    if (!templateKey) {
      setError("请选择模板");
      return;
    }
    if (!templateName.trim()) {
      setError("请填写新工作流名称");
      return;
    }
    setError("");
    instantiate.mutate();
  }

  const selected = templates.data?.find((item) => item.key === templateKey) ?? null;

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold">工作流</h1>
        <p className="mt-1 text-sm text-ink-500">
          用画布编排输入、工具、Agent、条件与审核节点，发布后手动或定时运行。
        </p>
      </header>

      {error && <p className="text-sm text-wine-700">{error}</p>}
      {workflows.isError && (
        <p className="text-sm text-wine-700">{errorText(workflows.error)}</p>
      )}

      <section className="card overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-paper text-left text-xs text-ink-500">
            <tr>
              <th className="px-4 py-3 font-medium">名称</th>
              <th className="px-4 py-3 font-medium">版本</th>
              <th className="px-4 py-3 font-medium">下次定时</th>
              <th className="px-4 py-3 font-medium">最近运行</th>
              <th className="px-4 py-3 font-medium">创建人</th>
            </tr>
          </thead>
          <tbody>
            {workflows.data?.length === 0 && (
              <tr>
                <td className="px-4 py-6 text-ink-500" colSpan={5}>
                  还没有工作流。
                </td>
              </tr>
            )}
            {workflows.data?.map((workflow) => (
              <tr key={workflow.id} className="border-t border-line">
                <td className="px-4 py-3">
                  <Link className="font-medium text-sage-700" to={`/workflows/${workflow.id}`}>
                    {workflow.name}
                  </Link>
                  {workflow.has_unpublished && (
                    <span className="badge ml-2 bg-wine-50 text-wine-700">有未发布修改</span>
                  )}
                  {workflow.description && (
                    <p className="mt-1 text-xs text-ink-500">{workflow.description}</p>
                  )}
                </td>
                <td className="px-4 py-3 text-ink-700">
                  {workflow.latest_version == null ? "未发布" : `v${workflow.latest_version}`}
                </td>
                <td className="px-4 py-3 text-ink-700">
                  {workflow.next_run_at ? timestamp(workflow.next_run_at) : "—"}
                </td>
                <td className="px-4 py-3 text-ink-700">
                  {workflow.last_run ? (
                    <span>
                      {runStatusLabel(workflow.last_run.status)}
                      <span className="ml-2 text-xs text-ink-500">
                        {timestamp(workflow.last_run.created_at)}
                      </span>
                    </span>
                  ) : (
                    "—"
                  )}
                </td>
                <td className="px-4 py-3 text-ink-500">{workflow.created_by}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {editable && (
        <div className="grid gap-6 lg:grid-cols-2">
          <form className="card card-pad space-y-3" onSubmit={submitCreate}>
            <h2 className="text-sm font-semibold">新建工作流</h2>
            <label className="space-y-1 text-sm text-ink-500">
              <span>名称 *</span>
              <input
                className="input"
                maxLength={120}
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="例如：每周选题创作"
              />
            </label>
            <label className="space-y-1 text-sm text-ink-500">
              <span>说明</span>
              <input
                className="input"
                maxLength={2000}
                value={description}
                onChange={(event) => setDescription(event.target.value)}
              />
            </label>
            <button className="btn-primary" disabled={create.isPending} type="submit">
              {create.isPending ? "创建中…" : "创建草稿"}
            </button>
          </form>

          <form className="card card-pad space-y-3" onSubmit={submitTemplate}>
            <h2 className="text-sm font-semibold">从模板创建</h2>
            <label className="space-y-1 text-sm text-ink-500">
              <span>模板</span>
              <select
                className="input"
                value={templateKey}
                onChange={(event) => {
                  setTemplateKey(event.target.value);
                  const item = templates.data?.find(
                    (template) => template.key === event.target.value,
                  );
                  setTemplateName(item?.name ?? "");
                }}
              >
                <option value="">选择模板</option>
                {(templates.data ?? []).map((template) => (
                  <option key={template.key} value={template.key}>
                    {template.name}
                  </option>
                ))}
              </select>
            </label>
            {selected && (
              <p className="text-xs text-ink-500">{selected.description}</p>
            )}
            <label className="space-y-1 text-sm text-ink-500">
              <span>新工作流名称 *</span>
              <input
                className="input"
                maxLength={120}
                value={templateName}
                onChange={(event) => setTemplateName(event.target.value)}
              />
            </label>
            <button
              className="btn-secondary"
              disabled={instantiate.isPending || !templateKey}
              type="submit"
            >
              {instantiate.isPending ? "创建中…" : "实例化模板"}
            </button>
          </form>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 5: 注册路由**

在 `web/src/App.tsx` 的导入区加入：

```tsx
import Workflows from "./pages/Workflows";
```

在 `/assistant` 路由之后加入：

```tsx
          <Route
            path="/workflows"
            element={
              <RequireRole minRole="editor">
                <Workflows />
              </RequireRole>
            }
          />
```

- [ ] **Step 6: 运行测试与构建**

```bash
cd web && npm test -- workflow
```

Expected: PASS（`18 passed`）。

```bash
cd web && npx tsc -b
```

Expected: 无输出（退出码 0）。

- [ ] **Step 7: 提交**

```bash
git add web/src/auth.ts web/src/App.tsx web/src/pages/Workflows.tsx web/src/__tests__/workflow.test.ts
git commit -m "feat(web): add workflow list page and navigation"
```

---

## Task 15: 画布组件与图编辑帮助函数

**Files:**
- Modify: `web/package.json`（新增 `@xyflow/react`，随 `npm install` 更新 `package-lock.json`）
- Modify: `web/src/types/workflow.ts`
- Modify: `web/src/workflows.ts`
- Create: `web/src/components/workflow/WorkflowNode.tsx`
- Create: `web/src/components/workflow/NodePalette.tsx`
- Create: `web/src/components/workflow/NodeInspector.tsx`
- Create: `web/src/components/workflow/WorkflowCanvas.tsx`
- Test: `web/src/__tests__/workflow.test.ts`

**背景与接口**

画布只负责渲染与交互，图数据仍由编辑器页面持有：画布把拖动、连线、删除回调给页面，页面用 `workflows.ts` 的纯函数生成新图（便于撤销/重做）。属性面板直接复用 `GET /api/agent/tools` 的 `parameters` JSON Schema 与 `GET /api/skills` 的技能列表，不硬编码工具或技能。

**关键约定**

- 节点 id 由前端生成，前缀按类型（`in`/`t`/`a`/`c`/`r`/`o`），保证唯一；条件节点两条出边句柄为 `"true"`/`"false"`。
- 输出节点可以多个；输入节点最多一个，面板上已有输入节点时禁用「输入」按钮。
- 连线规则与后端发布校验一致：输入节点不能有入边、条件节点必须带 `true`/`false` 句柄、非条件节点不得带句柄、同一分支不能重复连线。保存草稿仍允许环与不完整图。
- 画布用 `version` 数字作为重置信号：页面每次提交图变更（含撤销/重做/重新加载）递增，画布据此重建内部节点；运行详情把查询的 `dataUpdatedAt` 作为 `version`，轮询后状态自然刷新。
- 节点状态与已执行边由页面计算后传入；画布不轮询、不发请求。
- 样式只用现有令牌（`border-line`、`bg-surface`、`sage`、`wine`、`warm`），不引入新配色。

- [ ] **Step 1: 安装依赖**

```bash
cd web && npm install @xyflow/react
```

Expected: `package.json` 的 `dependencies` 中出现 `"@xyflow/react": "^12...."`，`package-lock.json` 更新，安装无 peer 冲突。

- [ ] **Step 2: 追加失败测试**

在 `web/src/__tests__/workflow.test.ts` 的导入中加入：

```ts
import {
  addNode,
  connectNodes,
  executedEdgeIds,
  moveNode,
  removeNode,
  updateNodeData,
} from "../workflows";
```

在文件末尾追加：

```ts
describe("图编辑帮助函数", () => {
  it("按类型生成默认数据与唯一 id", () => {
    const one = addNode(emptyGraph(), "tool", { x: 300, y: 120 });
    const added = one.nodes.find((node) => node.id === "t1");
    expect(added).toBeDefined();
    expect(added!.type).toBe("tool");
    expect(added!.data).toMatchObject({ label: "工具", tool: "", args: {}, retry: 0 });
    expect(added!.position).toEqual({ x: 300, y: 120 });

    const two = addNode(one, "tool", { x: 300, y: 240 });
    expect(two.nodes.map((node) => node.id)).toContain("t2");
  });

  it("条件节点必须带句柄，非条件节点不得带句柄", () => {
    const withCondition = addNode(graph, "condition", { x: 0, y: 0 });
    const plain = connectNodes(withCondition, "a", "out", null);
    expect(plain.error).toBeNull();
    expect(plain.graph.edges.at(-1)).toMatchObject({
      source: "a",
      target: "out",
    });

    const handled = connectNodes(withCondition, "a", "out", "true");
    expect(handled.error).toBe("只有条件节点可以指定出边句柄");

    const missing = connectNodes(withCondition, "cond", "out", null);
    expect(missing.error).toBe("条件节点必须从「是」或「否」出边连接");

    const taken = connectNodes(withCondition, "cond", "out", "true");
    expect(taken.error).toBe("该分支已有连线");
  });

  it("拒绝自连、输入入边与重复连线", () => {
    expect(connectNodes(graph, "a", "a").error).toBe("不能连接到自己");
    expect(connectNodes(graph, "a", "in").error).toBe("输入节点不能有入边");
    expect(connectNodes(graph, "a", "b").error).toBe("连线已存在");
  });

  it("删除节点同时删除关联边，更新位置与数据保持不变式", () => {
    const removed = removeNode(graph, "a");
    expect(removed.nodes.some((node) => node.id === "a")).toBe(false);
    expect(
      removed.edges.some((edge) => edge.source === "a" || edge.target === "a"),
    ).toBe(false);

    const moved = moveNode(graph, "a", { x: 12, y: 34 });
    expect(moved.nodes.find((node) => node.id === "a")!.position).toEqual({
      x: 12,
      y: 34,
    });
    expect(moved.nodes.length).toBe(graph.nodes.length);

    const patched = updateNodeData(graph, "a", { label: "读画像（改）" });
    expect(patched.nodes.find((node) => node.id === "a")!.data.label).toBe(
      "读画像（改）",
    );
    expect(patched.nodes.find((node) => node.id === "b")!.data).toEqual(
      graph.nodes.find((node) => node.id === "b")!.data,
    );
  });

  it("已执行边只包含两端都成功的边", () => {
    const runs = [
      { node_id: "in", status: "ok" },
      { node_id: "a", status: "ok" },
      { node_id: "b", status: "ok" },
      { node_id: "cond", status: "ok" },
      { node_id: "yes", status: "skipped" },
      { node_id: "no", status: "ok" },
      { node_id: "out", status: "ok" },
    ] as never;
    const ids = executedEdgeIds(graph, runs);
    expect(ids.has("e2")).toBe(true);
    expect(ids.has("e5")).toBe(true);
    expect(ids.has("e4")).toBe(false);
    expect(ids.has("e6")).toBe(false);
  });
});
```

- [ ] **Step 3: 运行测试确认失败**

```bash
cd web && npm test -- workflow
```

Expected: FAIL，`addNode is not a function`（帮助函数尚未实现）。

- [ ] **Step 4: 补类型**

在 `web/src/types/workflow.ts` 末尾追加：

```ts
export interface SkillOption {
  id: string;
  name: string;
  enabled?: boolean;
}

export interface SkillList {
  items: SkillOption[];
}
```

- [ ] **Step 5: 补图编辑帮助函数**

在 `web/src/workflows.ts` 的 `Graph` 相关导入中加入 `WorkflowNodeRun`：

```ts
import type {
  Cadence,
  Graph,
  GraphNode,
  InputField,
  WorkflowApproval,
  WorkflowNodeRun,
  WorkflowRun,
  WorkflowSchedule,
} from "./types/workflow";
```

在 `graphScale` 之前插入：

```ts
const NODE_PREFIXES: Record<string, string> = {
  input: "in",
  tool: "t",
  agent: "a",
  condition: "c",
  review: "r",
  output: "o",
};

function usablePosition(value: unknown): { x: number; y: number } {
  if (typeof value !== "object" || value === null) return { x: 0, y: 0 };
  const record = value as Record<string, unknown>;
  return {
    x: Number(record.x) || 0,
    y: Number(record.y) || 0,
  };
}

export function nodeDefaults(type: string): Record<string, unknown> {
  switch (type) {
    case "tool":
      return { label: "工具", tool: "", args: {}, retry: 0 };
    case "agent":
      return {
        label: "Agent",
        goal: "",
        tools: [],
        budget: { max_steps: 8 },
        retry: 0,
      };
    case "condition":
      return { label: "条件", left: "", operator: "contains", value: "" };
    case "review":
      return { label: "审核", title: "请确认本次结果", note: "" };
    case "output":
      return { label: "输出" };
    case "input":
      return {
        label: "输入",
        fields: [{ key: "topic", label: "主题", type: "string", required: true }],
      };
    default:
      return { label: type };
  }
}

export function addNode(
  graph: Graph,
  type: string,
  position: { x: number; y: number },
): Graph {
  const prefix = NODE_PREFIXES[type] ?? "n";
  const used = new Set(graph.nodes.map((node) => node.id));
  let id = `${prefix}1`;
  let counter = 1;
  while (used.has(id)) {
    counter += 1;
    id = `${prefix}${counter}`;
  }
  return {
    ...graph,
    nodes: [
      ...graph.nodes,
      {
        id,
        type,
        position: { x: position.x, y: position.y },
        data: nodeDefaults(type),
      },
    ],
  };
}

export interface ConnectResult {
  graph: Graph;
  error: string | null;
}

export function connectNodes(
  graph: Graph,
  source: string,
  target: string,
  sourceHandle: string | null = null,
): ConnectResult {
  const byId = new Map(graph.nodes.map((node) => [node.id, node]));
  const from = byId.get(source);
  const to = byId.get(target);
  if (!from || !to) return { graph, error: "节点不存在" };
  if (source === target) return { graph, error: "不能连接到自己" };
  if (to.type === "input") return { graph, error: "输入节点不能有入边" };
  const handle = typeof sourceHandle === "string" && sourceHandle ? sourceHandle : null;
  if (from.type === "condition") {
    if (handle !== "true" && handle !== "false") {
      return { graph, error: "条件节点必须从「是」或「否」出边连接" };
    }
    if (
      graph.edges.some(
        (edge) => edge.source === source && edge.sourceHandle === handle,
      )
    ) {
      return { graph, error: "该分支已有连线" };
    }
  } else if (handle) {
    return { graph, error: "只有条件节点可以指定出边句柄" };
  }
  if (
    graph.edges.some(
      (edge) =>
        edge.source === source &&
        edge.target === target &&
        (edge.sourceHandle ?? null) === handle,
    )
  ) {
    return { graph, error: "连线已存在" };
  }
  const id = `e-${source}-${handle ?? "out"}-${target}`;
  return {
    graph: {
      ...graph,
      edges: [
        ...graph.edges,
        { id, source, target, ...(handle ? { sourceHandle: handle } : {}) },
      ],
    },
    error: null,
  };
}

export function removeNode(graph: Graph, nodeId: string): Graph {
  return {
    nodes: graph.nodes.filter((node) => node.id !== nodeId),
    edges: graph.edges.filter(
      (edge) => edge.source !== nodeId && edge.target !== nodeId,
    ),
  };
}

export function moveNode(
  graph: Graph,
  nodeId: string,
  position: { x: number; y: number },
): Graph {
  return {
    ...graph,
    nodes: graph.nodes.map((node) =>
      node.id === nodeId ? { ...node, position: usablePosition(position) } : node,
    ),
  };
}

export function updateNodeData(
  graph: Graph,
  nodeId: string,
  patch: Record<string, unknown>,
): Graph {
  return {
    ...graph,
    nodes: graph.nodes.map((node) =>
      node.id === nodeId ? { ...node, data: { ...node.data, ...patch } } : node,
    ),
  };
}

/** 运行详情：两端都成功的边视为已执行路径。 */
export function executedEdgeIds(
  graph: Graph,
  nodeRuns: Pick<WorkflowNodeRun, "node_id" | "status">[],
): Set<string> {
  const done = new Set(
    nodeRuns.filter((run) => run.status === "ok").map((run) => run.node_id),
  );
  return new Set(
    graph.edges
      .filter((edge) => done.has(edge.source) && done.has(edge.target))
      .map((edge) => edge.id),
  );
}
```

- [ ] **Step 6: 节点视图**

新建 `web/src/components/workflow/WorkflowNode.tsx`：

```tsx
import { Handle, Position, type NodeProps } from "@xyflow/react";
import { memo } from "react";
import { nodeKindLabel, nodeStatusBadge, nodeStatusLabel } from "../../workflows";

function WorkflowNodeView({ data, selected }: NodeProps) {
  const kind = typeof data.kind === "string" ? data.kind : "";
  const label = typeof data.label === "string" ? data.label : "";
  const status = typeof data.status === "string" ? data.status : "";
  const condition = kind === "condition";
  return (
    <div
      className={[
        "relative min-w-[150px] rounded-lg border bg-surface px-3 py-2 text-xs shadow-sm",
        selected ? "border-sage-500 ring-2 ring-sage-100" : "border-line",
        status === "skipped" ? "opacity-50" : "",
        status === "running" ? "border-sage-500" : "",
        status === "error" || status === "denied" ? "border-wine-500" : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <div className="flex items-center justify-between gap-3">
        <span className="text-sm font-medium text-ink-900">{label}</span>
        {status && <span className={nodeStatusBadge(status)}>{nodeStatusLabel(status)}</span>}
      </div>
      <div className="mt-0.5 text-[10px] text-ink-500">{nodeKindLabel(kind)}</div>
      {kind !== "input" && <Handle type="target" position={Position.Left} />}
      {condition ? (
        <>
          <Handle
            id="true"
            type="source"
            position={Position.Right}
            style={{ top: "35%" }}
          />
          <Handle
            id="false"
            type="source"
            position={Position.Right}
            style={{ top: "70%" }}
          />
          <span className="absolute right-1 top-[22%] text-[9px] text-sage-700">是</span>
          <span className="absolute right-1 top-[57%] text-[9px] text-wine-700">否</span>
        </>
      ) : (
        kind !== "output" && <Handle type="source" position={Position.Right} />
      )}
    </div>
  );
}

export default memo(WorkflowNodeView);
```

- [ ] **Step 7: 节点面板**

新建 `web/src/components/workflow/NodePalette.tsx`：

```tsx
import { nodeKindLabel } from "../../workflows";

const PALETTE: { type: string; hint: string }[] = [
  { type: "tool", hint: "调用注册表工具" },
  { type: "agent", hint: "自主规划并调用工具" },
  { type: "condition", hint: "按比较结果分支" },
  { type: "review", hint: "人工通过后继续" },
  { type: "output", hint: "汇总结果与产物" },
  { type: "input", hint: "运行入参（最多一个）" },
];

export default function NodePalette({
  hasInput,
  readOnly,
  onAdd,
}: {
  hasInput: boolean;
  readOnly: boolean;
  onAdd: (type: string) => void;
}) {
  return (
    <aside className="card card-pad space-y-2">
      <h2 className="text-sm font-semibold">节点</h2>
      {PALETTE.map((item) => {
        const blocked = readOnly || (item.type === "input" && hasInput);
        return (
          <button
            key={item.type}
            className="w-full rounded-lg border border-line bg-white px-3 py-2 text-left hover:border-sage-500 disabled:opacity-40"
            disabled={blocked}
            onClick={() => onAdd(item.type)}
            type="button"
          >
            <span className="block text-sm text-ink-900">{nodeKindLabel(item.type)}</span>
            <span className="block text-[11px] text-ink-500">{item.hint}</span>
          </button>
        );
      })}
      <p className="pt-2 text-[11px] leading-5 text-ink-500">
        点击加入画布，拖动节点调整位置；选中后按 Backspace 删除。
      </p>
    </aside>
  );
}
```

- [ ] **Step 8: 属性面板**

新建 `web/src/components/workflow/NodeInspector.tsx`：

```tsx
import { useEffect, useState } from "react";
import type {
  Graph,
  GraphNode,
  InputField,
  SkillOption,
  ToolSpec,
} from "../../types/workflow";
import {
  inputFields,
  nodeKindLabel,
  variableCandidates,
  type VariableOption,
} from "../../workflows";

const OPERATORS = [
  { value: "equals", label: "等于" },
  { value: "not_equals", label: "不等于" },
  { value: "contains", label: "包含" },
  { value: "not_empty", label: "非空" },
  { value: "gt", label: "大于" },
  { value: "lt", label: "小于" },
];

const FIELD_TYPES = ["string", "number", "boolean"];

function schemaProperties(
  schema: Record<string, unknown> | undefined,
): [string, Record<string, unknown>][] {
  const properties = schema?.properties;
  if (typeof properties !== "object" || properties === null) return [];
  return Object.entries(properties as Record<string, Record<string, unknown>>);
}

function propertyLabel(name: string, spec: Record<string, unknown>): string {
  return typeof spec.title === "string" && spec.title ? spec.title : name;
}

function propertyType(spec: Record<string, unknown>): string {
  return typeof spec.type === "string" ? spec.type : "string";
}

function VariablePicker({
  options,
  onPick,
}: {
  options: VariableOption[];
  onPick: (value: string) => void;
}) {
  return (
    <select
      className="input text-xs"
      value=""
      onChange={(event) => {
        const value = event.target.value;
        if (value) onPick(value);
      }}
    >
      <option value="">插入变量…</option>
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  );
}

function appendValue(current: string, value: string): string {
  if (!current.trim()) return value;
  return `${current}${current.endsWith(" ") ? "" : " "}${value}`;
}

function TextAreaWithVariables({
  value,
  rows,
  options,
  onChange,
}: {
  value: string;
  rows: number;
  options: VariableOption[];
  onChange: (value: string) => void;
}) {
  return (
    <div className="space-y-1">
      <textarea
        className="input"
        rows={rows}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
      <VariablePicker
        options={options}
        onPick={(picked) => onChange(appendValue(value, picked))}
      />
    </div>
  );
}

function ArgsForm({
  tool,
  args,
  options,
  skills,
  onChange,
}: {
  tool: ToolSpec | null;
  args: Record<string, unknown>;
  options: VariableOption[];
  skills: SkillOption[];
  onChange: (args: Record<string, unknown>) => void;
}) {
  const properties = schemaProperties(tool?.parameters);
  const setArg = (key: string, value: unknown) =>
    onChange({ ...args, [key]: value });

  return (
    <div className="space-y-2">
      {properties.length === 0 && (
        <p className="text-[11px] text-ink-500">该工具没有声明参数。</p>
      )}
      {properties.map(([key, spec]) => {
        const type = propertyType(spec);
        const label = propertyLabel(key, spec);
        if (tool?.name === "skill.run" && key === "skill_id") {
          return (
            <label key={key} className="block space-y-1 text-xs text-ink-500">
              <span>{label}</span>
              <select
                className="input text-xs text-ink-900"
                value={typeof args[key] === "string" ? (args[key] as string) : ""}
                onChange={(event) => setArg(key, event.target.value)}
              >
                <option value="">选择技能</option>
                {skills.map((skill) => (
                  <option key={skill.id} value={skill.id}>
                    {skill.name}（{skill.id}）
                  </option>
                ))}
              </select>
            </label>
          );
        }
        if (type === "boolean") {
          return (
            <label key={key} className="flex items-center gap-2 text-xs text-ink-700">
              <input
                type="checkbox"
                checked={Boolean(args[key])}
                onChange={(event) => setArg(key, event.target.checked)}
              />
              <span>{label}</span>
            </label>
          );
        }
        return (
          <div key={key} className="space-y-1">
            <label className="block space-y-1 text-xs text-ink-500">
              <span>{label}</span>
              <input
                className="input text-xs text-ink-900"
                type={type === "number" || type === "integer" ? "number" : "text"}
                value={
                  typeof args[key] === "string" || typeof args[key] === "number"
                    ? String(args[key])
                    : ""
                }
                onChange={(event) => {
                  const raw = event.target.value;
                  const numeric = type === "number" || type === "integer";
                  setArg(
                    key,
                    numeric && raw && !raw.startsWith("{{") ? Number(raw) : raw,
                  );
                }}
              />
            </label>
            <VariablePicker
              options={options}
              onPick={(picked) => setArg(key, picked)}
            />
          </div>
        );
      })}
    </div>
  );
}

export default function NodeInspector({
  node,
  graph,
  tools,
  skills,
  readOnly,
  onChange,
  onDelete,
}: {
  node: GraphNode | null;
  graph: Graph;
  tools: ToolSpec[];
  skills: SkillOption[];
  readOnly: boolean;
  onChange: (patch: Record<string, unknown>) => void;
  onDelete: () => void;
}) {
  const [label, setLabel] = useState("");

  const nodeData = node?.data ?? {};
  useEffect(() => {
    setLabel(node ? String(node.data.label ?? "") : "");
  }, [node?.id]);

  if (!node) {
    return (
      <aside className="card card-pad text-sm text-ink-500">
        选中画布上的节点以编辑属性。
      </aside>
    );
  }

  const options = variableCandidates(graph, node.id, inputFields(graph));
  const fields = Array.isArray(nodeData.fields)
    ? (nodeData.fields as InputField[])
    : [];
  const toolsWanted = Array.isArray(nodeData.tools)
    ? (nodeData.tools as unknown[]).filter(
        (item): item is string => typeof item === "string",
      )
    : [];
  const budget =
    typeof nodeData.budget === "object" && nodeData.budget !== null
      ? (nodeData.budget as Record<string, unknown>)
      : {};
  const args =
    typeof nodeData.args === "object" && nodeData.args !== null
      ? (nodeData.args as Record<string, unknown>)
      : {};
  const selectedTool =
    tools.find((tool) => tool.name === nodeData.tool) ?? null;

  return (
    <aside className="card card-pad space-y-3 overflow-y-auto">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold">{nodeKindLabel(node.type)}节点</h2>
        {!readOnly && (
          <button className="text-xs text-wine-700" onClick={onDelete} type="button">
            删除
          </button>
        )}
      </div>

      <label className="block space-y-1 text-xs text-ink-500">
        <span>显示名称</span>
        <input
          className="input text-sm text-ink-900"
          disabled={readOnly}
          maxLength={80}
          value={label}
          onChange={(event) => setLabel(event.target.value)}
          onBlur={() => {
            if (label !== String(nodeData.label ?? "")) onChange({ label });
          }}
        />
      </label>

      {node.type === "input" && (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-xs text-ink-500">入参字段</span>
            {!readOnly && (
              <button
                className="text-xs text-sage-700"
                type="button"
                onClick={() =>
                  onChange({
                    fields: [
                      ...fields,
                      { key: `field_${fields.length + 1}`, type: "string" },
                    ],
                  })
                }
              >
                + 字段
              </button>
            )}
          </div>
          {fields.map((field, index) => (
            <div key={`${field.key}-${index}`} className="space-y-1 rounded-lg border border-line p-2">
              <input
                className="input text-xs"
                disabled={readOnly}
                placeholder="key（小写字母开头）"
                value={field.key}
                onChange={(event) =>
                  onChange({
                    fields: fields.map((item, position) =>
                      position === index ? { ...item, key: event.target.value } : item,
                    ),
                  })
                }
              />
              <input
                className="input text-xs"
                disabled={readOnly}
                placeholder="显示名"
                value={field.label ?? ""}
                onChange={(event) =>
                  onChange({
                    fields: fields.map((item, position) =>
                      position === index ? { ...item, label: event.target.value } : item,
                    ),
                  })
                }
              />
              <div className="flex items-center gap-2 text-xs">
                <select
                  className="input text-xs"
                  disabled={readOnly}
                  value={field.type}
                  onChange={(event) =>
                    onChange({
                      fields: fields.map((item, position) =>
                        position === index
                          ? { ...item, type: event.target.value as InputField["type"] }
                          : item,
                      ),
                    })
                  }
                >
                  {FIELD_TYPES.map((type) => (
                    <option key={type} value={type}>
                      {type}
                    </option>
                  ))}
                </select>
                <label className="flex items-center gap-1 text-ink-700">
                  <input
                    type="checkbox"
                    disabled={readOnly}
                    checked={Boolean(field.required)}
                    onChange={(event) =>
                      onChange({
                        fields: fields.map((item, position) =>
                          position === index
                            ? { ...item, required: event.target.checked }
                            : item,
                        ),
                      })
                    }
                  />
                  必填
                </label>
                {!readOnly && (
                  <button
                    className="ml-auto text-wine-700"
                    type="button"
                    onClick={() =>
                      onChange({
                        fields: fields.filter((_, position) => position !== index),
                      })
                    }
                  >
                    删除
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {node.type === "tool" && (
        <div className="space-y-2">
          <label className="block space-y-1 text-xs text-ink-500">
            <span>工具</span>
            <select
              className="input text-xs text-ink-900"
              disabled={readOnly}
              value={typeof nodeData.tool === "string" ? nodeData.tool : ""}
              onChange={(event) =>
                onChange({ tool: event.target.value, args: {} })
              }
            >
              <option value="">选择工具</option>
              {tools.map((tool) => (
                <option key={tool.name} value={tool.name}>
                  {tool.name} · {tool.risk}
                </option>
              ))}
            </select>
          </label>
          {selectedTool && (
            <p className="text-[11px] text-ink-500">{selectedTool.description}</p>
          )}
          <ArgsForm
            tool={selectedTool}
            args={args}
            options={options}
            skills={skills}
            onChange={(next) => onChange({ args: next })}
          />
        </div>
      )}

      {node.type === "agent" && (
        <div className="space-y-2">
          <label className="block space-y-1 text-xs text-ink-500">
            <span>目标</span>
            <TextAreaWithVariables
              value={typeof nodeData.goal === "string" ? nodeData.goal : ""}
              rows={5}
              options={options}
              onChange={(goal) => onChange({ goal })}
            />
          </label>
          <label className="block space-y-1 text-xs text-ink-500">
            <span>工具白名单（逗号分隔，支持前缀.*；留空为全部）</span>
            <input
              className="input text-xs text-ink-900"
              disabled={readOnly}
              value={toolsWanted.join(", ")}
              onChange={(event) =>
                onChange({
                  tools: event.target.value
                    .split(",")
                    .map((item) => item.trim())
                    .filter(Boolean),
                })
              }
            />
          </label>
          <div className="grid grid-cols-2 gap-2">
            {(["max_steps", "max_tokens", "max_tool_calls", "max_seconds"] as const).map(
              (key) => (
                <label key={key} className="block space-y-1 text-xs text-ink-500">
                  <span>{key}</span>
                  <input
                    className="input text-xs text-ink-900"
                    disabled={readOnly}
                    min={1}
                    type="number"
                    value={
                      typeof budget[key] === "number" ? String(budget[key]) : ""
                    }
                    onChange={(event) =>
                      onChange({
                        budget: {
                          ...budget,
                          [key]: Number(event.target.value) || undefined,
                        },
                      })
                    }
                  />
                </label>
              ),
            )}
          </div>
        </div>
      )}

      {node.type === "condition" && (
        <div className="space-y-2">
          <div className="space-y-1 text-xs text-ink-500">
            <span>左值</span>
            <TextAreaWithVariables
              value={typeof nodeData.left === "string" ? nodeData.left : ""}
              rows={2}
              options={options}
              onChange={(left) => onChange({ left })}
            />
          </div>
          <label className="block space-y-1 text-xs text-ink-500">
            <span>运算符</span>
            <select
              className="input text-xs text-ink-900"
              disabled={readOnly}
              value={
                typeof nodeData.operator === "string"
                  ? nodeData.operator
                  : "contains"
              }
              onChange={(event) => onChange({ operator: event.target.value })}
            >
              {OPERATORS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          {nodeData.operator !== "not_empty" && (
            <div className="space-y-1 text-xs text-ink-500">
              <span>右值</span>
              <TextAreaWithVariables
                value={typeof nodeData.value === "string" ? nodeData.value : ""}
                rows={2}
                options={options}
                onChange={(value) => onChange({ value })}
              />
            </div>
          )}
        </div>
      )}

      {node.type === "review" && (
        <div className="space-y-2">
          <label className="block space-y-1 text-xs text-ink-500">
            <span>审批标题</span>
            <input
              className="input text-xs text-ink-900"
              disabled={readOnly}
              maxLength={200}
              value={typeof nodeData.title === "string" ? nodeData.title : ""}
              onChange={(event) => onChange({ title: event.target.value })}
            />
          </label>
          <label className="block space-y-1 text-xs text-ink-500">
            <span>给审批人的说明</span>
            <textarea
              className="input text-xs"
              disabled={readOnly}
              rows={3}
              value={typeof nodeData.note === "string" ? nodeData.note : ""}
              onChange={(event) => onChange({ note: event.target.value })}
            />
          </label>
        </div>
      )}

      {(node.type === "tool" || node.type === "agent") && (
        <label className="block space-y-1 text-xs text-ink-500">
          <span>失败重试（0–3）</span>
          <select
            className="input text-xs text-ink-900"
            disabled={readOnly}
            value={Number(nodeData.retry) || 0}
            onChange={(event) => onChange({ retry: Number(event.target.value) })}
          >
            {[0, 1, 2, 3].map((count) => (
              <option key={count} value={count}>
                {count}
              </option>
            ))}
          </select>
        </label>
      )}
    </aside>
  );
}
```

- [ ] **Step 9: 画布**

新建 `web/src/components/workflow/WorkflowCanvas.tsx`：

```tsx
import "@xyflow/react/dist/style.css";
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  useEdgesState,
  useNodesState,
  type Connection,
  type Edge,
  type Node,
} from "@xyflow/react";
import { useEffect, useMemo } from "react";
import type { Graph } from "../../types/workflow";
import WorkflowNodeView from "./WorkflowNode";

const nodeTypes = {
  input: WorkflowNodeView,
  tool: WorkflowNodeView,
  agent: WorkflowNodeView,
  condition: WorkflowNodeView,
  review: WorkflowNodeView,
  output: WorkflowNodeView,
};

function fallbackPosition(index: number): { x: number; y: number } {
  return { x: 80 + (index % 4) * 240, y: 80 + Math.floor(index / 4) * 150 };
}

function toFlowNodes(
  graph: Graph,
  statuses: Record<string, string>,
  selectedId: string | null,
): Node[] {
  return graph.nodes.map((node, index) => {
    const position =
      node.position && Number.isFinite(node.position.x) && Number.isFinite(node.position.y)
        ? node.position
        : fallbackPosition(index);
    return {
      id: node.id,
      type: node.type,
      position,
      selected: node.id === selectedId,
      data: {
        ...node.data,
        kind: node.type,
        status: statuses[node.id] ?? "",
      },
    };
  });
}

function toFlowEdges(graph: Graph, executed: Set<string>): Edge[] {
  return graph.edges.map((edge) => {
    const active = executed.has(edge.id);
    const label =
      edge.sourceHandle === "true" ? "是" : edge.sourceHandle === "false" ? "否" : undefined;
    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      sourceHandle: edge.sourceHandle ?? undefined,
      label,
      animated: active,
      style: active ? { strokeWidth: 2.5, stroke: "#3f8a66" } : undefined,
      labelStyle: { fontSize: 10, fill: "#6b7570" },
    };
  });
}

export default function WorkflowCanvas({
  graph,
  version,
  readOnly = false,
  selectedId = null,
  statuses = {},
  executedEdges,
  onSelect,
  onMoveNode,
  onConnectEdge,
  onDeleteNodes,
  onDeleteEdges,
}: {
  graph: Graph;
  version: number;
  readOnly?: boolean;
  selectedId?: string | null;
  statuses?: Record<string, string>;
  executedEdges?: Set<string>;
  onSelect?: (id: string | null) => void;
  onMoveNode?: (id: string, position: { x: number; y: number }) => void;
  onConnectEdge?: (connection: Connection) => void;
  onDeleteNodes?: (ids: string[]) => void;
  onDeleteEdges?: (ids: string[]) => void;
}) {
  const emptyExecuted = useMemo(() => new Set<string>(), []);
  const [nodes, setNodes, onNodesChange] = useNodesState(
    toFlowNodes(graph, statuses, selectedId),
  );
  const [edges, setEdges, onEdgesChange] = useEdgesState(
    toFlowEdges(graph, executedEdges ?? emptyExecuted),
  );

  useEffect(() => {
    setNodes(toFlowNodes(graph, statuses, selectedId));
    setEdges(toFlowEdges(graph, executedEdges ?? emptyExecuted));
    // version 是页面提交图变更的序号；selectedId 单独变化时只更新选中态。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version]);

  useEffect(() => {
    setNodes((current) =>
      current.map((node) => ({ ...node, selected: node.id === selectedId })),
    );
  }, [selectedId, setNodes]);

  return (
    <div className="h-[520px] overflow-hidden rounded-xl border border-line bg-surface">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={(_, node) => onSelect?.(node.id)}
        onPaneClick={() => onSelect?.(null)}
        onNodeDragStop={(_, node) => onMoveNode?.(node.id, node.position)}
        onConnect={(connection) => onConnectEdge?.(connection)}
        onNodesDelete={(deleted) => onDeleteNodes?.(deleted.map((node) => node.id))}
        onEdgesDelete={(deleted) => onDeleteEdges?.(deleted.map((edge) => edge.id))}
        nodesDraggable={!readOnly}
        nodesConnectable={!readOnly}
        elementsSelectable
        deleteKeyCode={readOnly ? null : ["Backspace", "Delete"]}
        snapToGrid
        snapGrid={[16, 16]}
        defaultEdgeOptions={{ type: "smoothstep" }}
        fitView
        minZoom={0.3}
        maxZoom={1.6}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={16} color="#e3e5e1" />
        <MiniMap
          pannable
          zoomable
          nodeColor={(node) => {
            const status = String((node.data as Record<string, unknown>).status ?? "");
            if (status === "running") return "#3f8a66";
            if (status === "error" || status === "denied") return "#8c3a4a";
            if (status === "skipped") return "#e3e5e1";
            return "#dcebe1";
          }}
        />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
```

- [ ] **Step 10: 运行测试与构建**

```bash
cd web && npm test -- workflow
```

Expected: PASS（`23 passed`）。

```bash
cd web && npx tsc -b
```

Expected: 无输出（退出码 0）。

> 若 `@xyflow/react` 的类型要求 `NodeProps` 带泛型，按报错把节点视图签名改成
> `NodeProps<Node<Record<string, unknown>>>`；不要用 `any` 绕过类型检查。

- [ ] **Step 11: 提交**

```bash
git add web/package.json web/package-lock.json web/src/types/workflow.ts web/src/workflows.ts web/src/components/workflow web/src/__tests__/workflow.test.ts
git commit -m "feat(web): add workflow canvas, node palette and inspector"
```

---

## Task 16: 画布编辑器页面

**Files:**
- Modify: `web/src/workflows.ts`
- Modify: `web/src/App.tsx`
- Create: `web/src/pages/WorkflowEditor.tsx`
- Test: `web/src/__tests__/workflow.test.ts`

**背景与接口**

`/workflows/:id` 是编排主界面：顶栏负责保存/校验/发布/运行/版本历史，左栏节点面板，中间 React Flow 画布，右栏「属性 / 定时」页签。admin/owner 可编辑，editor 只读画布但可运行已发布版本。编辑用本地撤销栈（最多 50 步），保存只做轻量提示，发布由服务端权威校验。

**关键约定**

- 保存发送 `{revision, draft_graph}`；`409 workflow_conflict` 时显示冲突条，用户点「重新加载」丢弃本地修改并重新拉取。
- 校验与发布前先保存，确保服务端校验的是当前画布；发布 `422 workflow_invalid` 时把 `detail.issues` 放进底部抽屉。
- 运行只用 `latest_published.version`；未发布时按钮禁用。运行入参按输入节点字段类型转换（number/boolean），空值不提交。
- 版本恢复（`POST /versions/{version}/restore`）直接返回新的 `WorkflowDetail`，用它重置画布与 `revision`。
- 定时只提供每日/每周 + 时分 + 时区；`weekdays` 用 ISO 星期（周一 1）。启用开关走 `PATCH`，删除保留运行历史。
- 离开页面时若有未保存修改：`beforeunload` 拦截刷新，「返回」按钮弹 `window.confirm`。
- 画布 `version` 在每次提交图变更（含撤销/重做/恢复/保存回填）时递增，用于重建内部节点。

- [ ] **Step 1: 追加失败测试**

在 `web/src/__tests__/workflow.test.ts` 的 `../workflows` 导入中加入 `inputPayload`，并在文件末尾追加：

```ts
describe("运行入参转换", () => {
  it("按字段类型转换，跳过空的可选值", () => {
    const fields = [
      { key: "topic", type: "string" as const, required: true },
      { key: "limit", type: "number" as const },
      { key: "dry_run", type: "boolean" as const },
    ];
    expect(
      inputPayload(fields, { topic: "秋季护肤", limit: "5", dry_run: false }),
    ).toEqual({ topic: "秋季护肤", limit: 5, dry_run: false });
    expect(inputPayload(fields, { topic: "只有主题" })).toEqual({
      topic: "只有主题",
      dry_run: false,
    });
    expect(inputPayload(fields, { topic: "", limit: "" })).toEqual({
      dry_run: false,
    });
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd web && npm test -- workflow
```

Expected: FAIL，`inputPayload is not a function`。

- [ ] **Step 3: 补入参转换函数**

在 `web/src/workflows.ts` 的 `graphScale` 之前插入：

```ts
export type InputValues = Record<string, string | boolean>;

/** 空字符串视为未填写；布尔字段总是提交（未勾选即 false）。 */
export function inputPayload(
  fields: InputField[],
  values: InputValues,
): Record<string, unknown> {
  const payload: Record<string, unknown> = {};
  for (const field of fields) {
    const raw = values[field.key];
    if (field.type === "boolean") {
      payload[field.key] = Boolean(raw);
      continue;
    }
    if (raw === undefined || raw === "") continue;
    payload[field.key] = field.type === "number" ? Number(raw) : raw;
  }
  return payload;
}
```

- [ ] **Step 4: 写编辑器页面**

新建 `web/src/pages/WorkflowEditor.tsx`：

```tsx
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useAuth } from "../auth-context";
import { ApiError, api } from "../client";
import { errorText } from "../types";
import type {
  Cadence,
  Graph,
  InputField,
  SkillList,
  ToolSpec,
  ValidationIssue,
  ValidationResult,
  WorkflowDetail,
  WorkflowRun,
  WorkflowSchedule,
  WorkflowVersionSummary,
} from "../types/workflow";
import {
  addNode,
  canEditWorkflow,
  canRunWorkflow,
  connectNodes,
  emptyGraph,
  graphScale,
  inputFields,
  inputPayload,
  moveNode,
  removeNode,
  scheduleText,
  updateNodeData,
  type InputValues,
} from "../workflows";
import NodeInspector from "../components/workflow/NodeInspector";
import NodePalette from "../components/workflow/NodePalette";
import WorkflowCanvas from "../components/workflow/WorkflowCanvas";

const WEEKDAYS = [
  { value: 1, label: "一" },
  { value: 2, label: "二" },
  { value: 3, label: "三" },
  { value: 4, label: "四" },
  { value: 5, label: "五" },
  { value: 6, label: "六" },
  { value: 7, label: "日" },
];

interface History {
  past: Graph[];
  future: Graph[];
}

interface ScheduleForm {
  id: string | null;
  kind: "daily" | "weekly";
  weekdays: number[];
  time: string;
  timezone: string;
  values: InputValues;
}

function normalizeGraph(value: Graph | null | undefined): Graph {
  if (!value || !Array.isArray(value.nodes) || !Array.isArray(value.edges)) {
    return emptyGraph();
  }
  return { nodes: value.nodes, edges: value.edges };
}

function issuesFromDetail(detail: unknown): ValidationIssue[] {
  if (typeof detail !== "object" || detail === null) return [];
  const issues = (detail as { issues?: unknown }).issues;
  return Array.isArray(issues) ? (issues as ValidationIssue[]) : [];
}

function valuesFromSchedule(
  schedule: WorkflowSchedule,
  fields: InputField[],
): InputValues {
  const values: InputValues = {};
  for (const field of fields) {
    const raw = schedule.inputs[field.key];
    if (field.type === "boolean") values[field.key] = Boolean(raw);
    else if (raw !== undefined && raw !== null) values[field.key] = String(raw);
  }
  return values;
}

export default function WorkflowEditor() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { user: me } = useAuth();
  const editable = me ? canEditWorkflow(me.role) : false;

  const [graph, setGraph] = useState<Graph>(emptyGraph());
  const [revision, setRevision] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [version, setVersion] = useState(0);
  const [history, setHistory] = useState<History>({ past: [], future: [] });
  const [tab, setTab] = useState<"props" | "schedule">("props");
  const [issues, setIssues] = useState<ValidationIssue[]>([]);
  const [drawer, setDrawer] = useState<"none" | "issues">("none");
  const [conflict, setConflict] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [showVersions, setShowVersions] = useState(false);
  const [showPublish, setShowPublish] = useState(false);
  const [showRun, setShowRun] = useState(false);
  const [publishNote, setPublishNote] = useState("");
  const [runValues, setRunValues] = useState<InputValues>({});
  const [scheduleForm, setScheduleForm] = useState<ScheduleForm | null>(null);
  const applied = useRef<number | null>(null);

  const detailQuery = useQuery({
    queryKey: ["workflow", id],
    queryFn: () => api<WorkflowDetail>(`/workflows/${id}`),
    enabled: Boolean(id),
  });
  const detail = detailQuery.data ?? null;

  const toolsQuery = useQuery({
    queryKey: ["agent-tools"],
    queryFn: () => api<ToolSpec[]>("/agent/tools"),
  });
  const skillsQuery = useQuery({
    queryKey: ["skills"],
    queryFn: () => api<SkillList>("/skills"),
  });
  const versionsQuery = useQuery({
    queryKey: ["workflow-versions", id],
    queryFn: () => api<WorkflowVersionSummary[]>(`/workflows/${id}/versions`),
    enabled: Boolean(id) && (showVersions || showPublish),
  });

  const bump = () => setVersion((value) => value + 1);

  useEffect(() => {
    if (!detail || dirty) return;
    if (applied.current === detail.draft_revision) return;
    applied.current = detail.draft_revision;
    setGraph(normalizeGraph(detail.draft_graph));
    setRevision(detail.draft_revision);
    setSelectedId(null);
    bump();
  }, [detail, dirty]);

  useEffect(() => {
    if (!dirty) return;
    const handler = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);

  function commit(next: Graph) {
    setHistory({
      past: [...history.past, graph].slice(-50),
      future: [],
    });
    setGraph(next);
    setDirty(true);
    bump();
  }

  function undo() {
    if (history.past.length === 0) return;
    const previous = history.past[history.past.length - 1];
    setHistory({
      past: history.past.slice(0, -1),
      future: [graph, ...history.future].slice(0, 50),
    });
    setGraph(previous);
    setDirty(true);
    bump();
  }

  function redo() {
    if (history.future.length === 0) return;
    const next = history.future[0];
    setHistory({
      past: [...history.past, graph].slice(-50),
      future: history.future.slice(1),
    });
    setGraph(next);
    setDirty(true);
    bump();
  }

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (!(event.metaKey || event.ctrlKey)) return;
      if (event.key.toLowerCase() !== "z") return;
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      event.preventDefault();
      if (event.shiftKey) redo();
      else undo();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  function leave() {
    if (dirty && !window.confirm("有未保存的修改，确定离开？")) return;
    navigate("/workflows");
  }

  const save = useMutation({
    mutationFn: () =>
      api<WorkflowDetail>(`/workflows/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ revision, draft_graph: graph }),
      }),
    onSuccess: (data) => {
      applied.current = data.draft_revision;
      setRevision(data.draft_revision);
      setGraph(normalizeGraph(data.draft_graph));
      setDirty(false);
      setConflict(false);
      setError("");
      setNotice("草稿已保存");
      bump();
      queryClient.setQueryData(["workflow", id], data);
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
    },
    onError: (err) => {
      if (err instanceof ApiError && err.code === "workflow_conflict") {
        setConflict(true);
        return;
      }
      setError(errorText(err));
    },
  });

  const validateMutation = useMutation({
    mutationFn: () =>
      api<ValidationResult>(`/workflows/${id}/validate`, { method: "POST" }),
    onSuccess: (result) => {
      setIssues(result.issues);
      setDrawer("issues");
      setNotice(result.ok ? "校验通过" : `发现 ${result.issues.length} 个问题`);
    },
    onError: (err) => setError(errorText(err)),
  });

  const publish = useMutation({
    mutationFn: (input: { revision: number; note: string }) =>
      api<WorkflowVersionSummary>(`/workflows/${id}/publish`, {
        method: "POST",
        body: JSON.stringify(input),
      }),
    onSuccess: (published) => {
      setShowPublish(false);
      setPublishNote("");
      setNotice(`已发布 v${published.version}`);
      void queryClient.invalidateQueries({ queryKey: ["workflow", id] });
      void queryClient.invalidateQueries({ queryKey: ["workflow-versions", id] });
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
    },
    onError: (err) => {
      if (err instanceof ApiError && err.code === "workflow_invalid") {
        setIssues(issuesFromDetail(err.detail));
        setDrawer("issues");
        setShowPublish(false);
        return;
      }
      if (err instanceof ApiError && err.code === "workflow_conflict") {
        setConflict(true);
        setShowPublish(false);
        return;
      }
      setError(errorText(err));
    },
  });

  const startRun = useMutation({
    mutationFn: (input: { inputs: Record<string, unknown>; version: number }) =>
      api<WorkflowRun>(`/workflows/${id}/runs`, {
        method: "POST",
        body: JSON.stringify(input),
      }),
    onSuccess: (run) => {
      setShowRun(false);
      navigate(`/workflows/${id}/runs/${run.id}`);
    },
    onError: (err) => setError(errorText(err)),
  });

  const restore = useMutation({
    mutationFn: (target: number) =>
      api<WorkflowDetail>(`/workflows/${id}/versions/${target}/restore`, {
        method: "POST",
      }),
    onSuccess: (data) => {
      applied.current = data.draft_revision;
      setRevision(data.draft_revision);
      setGraph(normalizeGraph(data.draft_graph));
      setDirty(false);
      setShowVersions(false);
      setNotice("已恢复到草稿");
      bump();
      queryClient.setQueryData(["workflow", id], data);
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
    },
    onError: (err) => setError(errorText(err)),
  });

  const saveSchedule = useMutation({
    mutationFn: (form: ScheduleForm) => {
      const cadence: Cadence = {
        kind: form.kind,
        weekdays: form.kind === "weekly" ? form.weekdays.slice().sort() : [],
        time: form.time,
        timezone: form.timezone,
      };
      const body = JSON.stringify({
        cadence,
        inputs: inputPayload(inputFields(graph), form.values),
      });
      if (form.id) {
        return api<WorkflowSchedule>(`/workflows/${id}/schedules/${form.id}`, {
          method: "PATCH",
          body,
        });
      }
      return api<WorkflowSchedule>(`/workflows/${id}/schedules`, {
        method: "POST",
        body,
      });
    },
    onSuccess: () => {
      setScheduleForm(null);
      void queryClient.invalidateQueries({ queryKey: ["workflow", id] });
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
    },
    onError: (err) => setError(errorText(err)),
  });

  const toggleSchedule = useMutation({
    mutationFn: (input: { id: string; enabled: boolean }) =>
      api<WorkflowSchedule>(`/workflows/${id}/schedules/${input.id}`, {
        method: "PATCH",
        body: JSON.stringify({ enabled: input.enabled }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["workflow", id] });
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
    },
    onError: (err) => setError(errorText(err)),
  });

  const deleteSchedule = useMutation({
    mutationFn: (scheduleId: string) =>
      api<void>(`/workflows/${id}/schedules/${scheduleId}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["workflow", id] });
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
    },
    onError: (err) => setError(errorText(err)),
  });

  async function validateDraft() {
    setError("");
    setNotice("");
    try {
      await save.mutateAsync();
    } catch {
      return;
    }
    validateMutation.mutate();
  }

  async function confirmPublish() {
    setError("");
    let currentRevision = revision;
    if (dirty) {
      try {
        const saved = await save.mutateAsync();
        currentRevision = saved.draft_revision;
      } catch {
        return;
      }
    }
    publish.mutate({ revision: currentRevision, note: publishNote });
  }

  function reloadConflict() {
    setConflict(false);
    setDirty(false);
    setError("");
    applied.current = null;
    void detailQuery.refetch();
  }

  const hasInput = graph.nodes.some((node) => node.type === "input");
  const fields = inputFields(graph);
  const selected = graph.nodes.find((node) => node.id === selectedId) ?? null;
  const latest = detail?.latest_published ?? null;
  const runnable = me ? canRunWorkflow(me.role, Boolean(latest)) : false;
  const scale = graphScale(graph);

  return (
    <div className="space-y-4">
      <header className="card card-pad space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <button className="btn-secondary" type="button" onClick={leave}>
            ← 返回
          </button>
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-lg font-semibold">
              {detail?.name ?? "工作流"}
            </h1>
            <p className="text-xs text-ink-500">
              revision {revision} · {scale.text}
              {scale.level !== "ok" && (
                <span className={scale.level === "over" ? "text-wine-700" : "text-warm-600"}>
                  {" "}
                  · {scale.level === "over" ? "已超过规模上限" : "接近规模上限"}
                </span>
              )}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {editable && (
              <>
                <button
                  className="btn-secondary"
                  disabled={save.isPending}
                  type="button"
                  onClick={() => save.mutate()}
                >
                  {save.isPending ? "保存中…" : dirty ? "保存草稿 *" : "保存草稿"}
                </button>
                <button
                  className="btn-secondary"
                  disabled={validateMutation.isPending}
                  type="button"
                  onClick={() => void validateDraft()}
                >
                  校验
                </button>
                <button
                  className="btn-primary"
                  type="button"
                  onClick={() => setShowPublish(true)}
                >
                  发布
                </button>
              </>
            )}
            <button
              className="btn-primary"
              disabled={!runnable}
              title={runnable ? "" : "发布后才能运行"}
              type="button"
              onClick={() => {
                setRunValues({});
                setShowRun(true);
              }}
            >
              运行{latest ? ` v${latest.version}` : ""}
            </button>
            <button
              className="btn-secondary"
              type="button"
              onClick={() => setShowVersions(true)}
            >
              版本历史
            </button>
          </div>
        </div>
        {(error || detailQuery.isError) && (
          <p className="text-sm text-wine-700">
            {error || errorText(detailQuery.error)}
          </p>
        )}
        {conflict && (
          <div className="flex items-center gap-3 rounded-lg bg-wine-50 px-3 py-2 text-sm text-wine-700">
            <span>草稿已被其他人更新，保存冲突。</span>
            <button className="btn-secondary" type="button" onClick={reloadConflict}>
              重新加载
            </button>
          </div>
        )}
        {notice && !error && <p className="text-sm text-sage-700">{notice}</p>}
      </header>

      <div className="grid gap-4 lg:grid-cols-[160px_minmax(0,1fr)_300px]">
        <NodePalette
          hasInput={hasInput}
          readOnly={!editable}
          onAdd={(type) =>
            commit(
              addNode(graph, type, {
                x: 80 + (graph.nodes.length % 4) * 200,
                y: 80 + Math.floor(graph.nodes.length / 4) * 120,
              }),
            )
          }
        />
        <div className="space-y-2">
          <WorkflowCanvas
            graph={graph}
            version={version}
            readOnly={!editable}
            selectedId={selectedId}
            onSelect={setSelectedId}
            onMoveNode={(nodeId, position) => {
              if (!editable) return;
              commit(moveNode(graph, nodeId, position));
            }}
            onConnectEdge={(connection) => {
              if (!editable || !connection.source || !connection.target) return;
              const result = connectNodes(
                graph,
                connection.source,
                connection.target,
                connection.sourceHandle ?? null,
              );
              if (result.error) {
                setError(result.error);
                return;
              }
              setError("");
              commit(result.graph);
            }}
            onDeleteNodes={(nodeIds) => {
              if (!editable) return;
              let next = graph;
              for (const nodeId of nodeIds) next = removeNode(next, nodeId);
              commit(next);
              if (selectedId && nodeIds.includes(selectedId)) setSelectedId(null);
            }}
            onDeleteEdges={(edgeIds) => {
              if (!editable) return;
              commit({
                ...graph,
                edges: graph.edges.filter((edge) => !edgeIds.includes(edge.id)),
              });
            }}
          />
          {drawer === "issues" && (
            <div className="card card-pad space-y-2">
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-semibold">校验结果</h2>
                <button
                  className="text-xs text-ink-500"
                  type="button"
                  onClick={() => setDrawer("none")}
                >
                  收起
                </button>
              </div>
              {issues.length === 0 ? (
                <p className="text-sm text-sage-700">没有发现问题。</p>
              ) : (
                <ul className="space-y-1">
                  {issues.map((issue, index) => (
                    <li key={`${issue.code}-${index}`}>
                      <button
                        className="text-left text-sm text-ink-700 hover:text-sage-700"
                        type="button"
                        onClick={() => {
                          if (issue.node_id) {
                            setSelectedId(issue.node_id);
                            setTab("props");
                          }
                          setDrawer("none");
                        }}
                      >
                        <span className="badge mr-2 bg-line text-ink-500">
                          {issue.code}
                        </span>
                        {issue.message}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>

        <div className="space-y-2">
          <div className="flex gap-1 rounded-lg border border-line bg-surface p-1 text-sm">
            <button
              className={`flex-1 rounded-md px-2 py-1 ${
                tab === "props" ? "bg-sage-50 text-sage-700" : "text-ink-500"
              }`}
              type="button"
              onClick={() => setTab("props")}
            >
              属性
            </button>
            <button
              className={`flex-1 rounded-md px-2 py-1 ${
                tab === "schedule" ? "bg-sage-50 text-sage-700" : "text-ink-500"
              }`}
              type="button"
              onClick={() => setTab("schedule")}
            >
              定时
            </button>
          </div>
          {tab === "props" ? (
            <NodeInspector
              node={selected}
              graph={graph}
              tools={toolsQuery.data ?? []}
              skills={skillsQuery.data?.items ?? []}
              readOnly={!editable}
              onChange={(patch) => {
                if (!selected) return;
                commit(updateNodeData(graph, selected.id, patch));
              }}
              onDelete={() => {
                if (!selected) return;
                commit(removeNode(graph, selected.id));
                setSelectedId(null);
              }}
            />
          ) : (
            <section className="card card-pad space-y-3 text-sm">
              <div className="flex items-center justify-between">
                <h2 className="font-semibold">定时</h2>
                {editable && (
                  <button
                    className="text-xs text-sage-700"
                    type="button"
                    onClick={() =>
                      setScheduleForm({
                        id: null,
                        kind: "daily",
                        weekdays: [1],
                        time: "09:00",
                        timezone: "Asia/Shanghai",
                        values: {},
                      })
                    }
                  >
                    + 新建
                  </button>
                )}
              </div>
              {(detail?.schedules ?? []).length === 0 && (
                <p className="text-xs text-ink-500">还没有定时任务。</p>
              )}
              {(detail?.schedules ?? []).map((schedule) => (
                <div key={schedule.id} className="space-y-1 rounded-lg border border-line p-2">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs text-ink-900">
                      {scheduleText(schedule)}
                    </span>
                    <label className="flex items-center gap-1 text-[11px] text-ink-500">
                      <input
                        type="checkbox"
                        disabled={!editable || toggleSchedule.isPending}
                        checked={schedule.enabled}
                        onChange={(event) =>
                          toggleSchedule.mutate({
                            id: schedule.id,
                            enabled: event.target.checked,
                          })
                        }
                      />
                      启用
                    </label>
                  </div>
                  <p className="text-[11px] text-ink-500">
                    下次：{schedule.next_run_at ? new Date(schedule.next_run_at).toLocaleString() : "—"}
                  </p>
                  {editable && (
                    <div className="flex gap-3 text-[11px]">
                      <button
                        className="text-sage-700"
                        type="button"
                        onClick={() =>
                          setScheduleForm({
                            id: schedule.id,
                            kind: schedule.cadence.kind === "weekly" ? "weekly" : "daily",
                            weekdays: schedule.cadence.weekdays?.length
                              ? schedule.cadence.weekdays
                              : [1],
                            time: schedule.cadence.time ?? "09:00",
                            timezone: schedule.cadence.timezone ?? "Asia/Shanghai",
                            values: valuesFromSchedule(schedule, fields),
                          })
                        }
                      >
                        编辑
                      </button>
                      <button
                        className="text-wine-700"
                        type="button"
                        onClick={() => {
                          if (window.confirm("删除这个定时？运行历史会保留。")) {
                            deleteSchedule.mutate(schedule.id);
                          }
                        }}
                      >
                        删除
                      </button>
                    </div>
                  )}
                </div>
              ))}
              {!editable && (
                <p className="text-[11px] text-ink-500">只有 admin/owner 可以修改定时。</p>
              )}
            </section>
          )}
        </div>
      </div>

      {showVersions && (
        <div className="fixed inset-0 z-40 flex justify-end bg-ink-900/20">
          <aside className="h-full w-full max-w-md space-y-3 overflow-y-auto bg-surface p-6">
            <div className="flex items-center justify-between">
              <h2 className="text-lg font-semibold">版本历史</h2>
              <button className="text-sm text-ink-500" type="button" onClick={() => setShowVersions(false)}>
                关闭
              </button>
            </div>
            {versionsQuery.isError && (
              <p className="text-sm text-wine-700">{errorText(versionsQuery.error)}</p>
            )}
            {(versionsQuery.data ?? []).length === 0 && (
              <p className="text-sm text-ink-500">还没有发布版本。</p>
            )}
            {(versionsQuery.data ?? []).map((item) => (
              <div key={item.id} className="space-y-1 rounded-lg border border-line p-3">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">v{item.version}</span>
                  <span className="text-xs text-ink-500">
                    {new Date(item.published_at).toLocaleString()}
                  </span>
                </div>
                <p className="text-xs text-ink-500">
                  基于 revision {item.source_revision} · {item.published_by}
                </p>
                {item.note && <p className="text-xs text-ink-700">{item.note}</p>}
                {editable && (
                  <button
                    className="text-xs text-sage-700"
                    disabled={restore.isPending}
                    type="button"
                    onClick={() => restore.mutate(item.version)}
                  >
                    恢复到草稿
                  </button>
                )}
              </div>
            ))}
          </aside>
        </div>
      )}

      {showPublish && (
        <div className="fixed inset-0 z-40 flex items-center justify-center bg-ink-900/20 p-4">
          <div className="w-full max-w-md space-y-3 rounded-xl bg-surface p-6">
            <h2 className="text-lg font-semibold">发布新版本</h2>
            <p className="text-xs text-ink-500">
              发布会先保存当前草稿，再由服务端做完整校验；校验失败会列出问题。
            </p>
            <label className="block space-y-1 text-sm text-ink-500">
              <span>发布说明</span>
              <input
                className="input"
                maxLength={500}
                value={publishNote}
                onChange={(event) => setPublishNote(event.target.value)}
              />
            </label>
            <div className="flex justify-end gap-2">
              <button className="btn-secondary" type="button" onClick={() => setShowPublish(false)}>
                取消
              </button>
              <button
                className="btn-primary"
                disabled={publish.isPending || save.isPending}
                type="button"
                onClick={() => void confirmPublish()}
              >
                发布
              </button>
            </div>
          </div>
        </div>
      )}

      {showRun && latest && (
        <div className="fixed inset-0 z-40 flex items-center justify-center bg-ink-900/20 p-4">
          <div className="w-full max-w-md space-y-3 rounded-xl bg-surface p-6">
            <h2 className="text-lg font-semibold">运行 v{latest.version}</h2>
            {fields.length === 0 && (
              <p className="text-sm text-ink-500">这个工作流没有声明入参。</p>
            )}
            {fields.map((field) => (
              <label key={field.key} className="block space-y-1 text-sm text-ink-500">
                <span>
                  {field.label || field.key}
                  {field.required ? " *" : ""}
                </span>
                {field.type === "boolean" ? (
                  <input
                    type="checkbox"
                    checked={Boolean(runValues[field.key])}
                    onChange={(event) =>
                      setRunValues({ ...runValues, [field.key]: event.target.checked })
                    }
                  />
                ) : (
                  <input
                    className="input"
                    type={field.type === "number" ? "number" : "text"}
                    value={String(runValues[field.key] ?? "")}
                    onChange={(event) =>
                      setRunValues({ ...runValues, [field.key]: event.target.value })
                    }
                  />
                )}
              </label>
            ))}
            <div className="flex justify-end gap-2">
              <button className="btn-secondary" type="button" onClick={() => setShowRun(false)}>
                取消
              </button>
              <button
                className="btn-primary"
                disabled={startRun.isPending}
                type="button"
                onClick={() =>
                  startRun.mutate({
                    inputs: inputPayload(fields, runValues),
                    version: latest.version,
                  })
                }
              >
                开始运行
              </button>
            </div>
          </div>
        </div>
      )}

      {scheduleForm && (
        <div className="fixed inset-0 z-40 flex items-center justify-center bg-ink-900/20 p-4">
          <div className="w-full max-w-md space-y-3 rounded-xl bg-surface p-6">
            <h2 className="text-lg font-semibold">
              {scheduleForm.id ? "编辑定时" : "新建定时"}
            </h2>
            <div className="flex gap-2 text-sm">
              {(["daily", "weekly"] as const).map((kind) => (
                <button
                  key={kind}
                  className={
                    scheduleForm.kind === kind ? "btn-primary" : "btn-secondary"
                  }
                  type="button"
                  onClick={() => setScheduleForm({ ...scheduleForm, kind })}
                >
                  {kind === "daily" ? "每天" : "每周"}
                </button>
              ))}
            </div>
            {scheduleForm.kind === "weekly" && (
              <div className="flex flex-wrap gap-2 text-sm">
                {WEEKDAYS.map((day) => {
                  const checked = scheduleForm.weekdays.includes(day.value);
                  return (
                    <label key={day.value} className="flex items-center gap-1 text-ink-700">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={(event) =>
                          setScheduleForm({
                            ...scheduleForm,
                            weekdays: event.target.checked
                              ? [...scheduleForm.weekdays, day.value].sort()
                              : scheduleForm.weekdays.filter((value) => value !== day.value),
                          })
                        }
                      />
                      周{day.label}
                    </label>
                  );
                })}
              </div>
            )}
            <div className="grid grid-cols-2 gap-2">
              <label className="block space-y-1 text-sm text-ink-500">
                <span>时间</span>
                <input
                  className="input"
                  type="time"
                  value={scheduleForm.time}
                  onChange={(event) =>
                    setScheduleForm({ ...scheduleForm, time: event.target.value })
                  }
                />
              </label>
              <label className="block space-y-1 text-sm text-ink-500">
                <span>时区</span>
                <input
                  className="input"
                  value={scheduleForm.timezone}
                  onChange={(event) =>
                    setScheduleForm({ ...scheduleForm, timezone: event.target.value })
                  }
                />
              </label>
            </div>
            {fields.length > 0 && (
              <div className="space-y-2">
                <p className="text-xs text-ink-500">运行入参</p>
                {fields.map((field) => (
                  <label key={field.key} className="block space-y-1 text-sm text-ink-500">
                    <span>{field.label || field.key}</span>
                    {field.type === "boolean" ? (
                      <input
                        type="checkbox"
                        checked={Boolean(scheduleForm.values[field.key])}
                        onChange={(event) =>
                          setScheduleForm({
                            ...scheduleForm,
                            values: {
                              ...scheduleForm.values,
                              [field.key]: event.target.checked,
                            },
                          })
                        }
                      />
                    ) : (
                      <input
                        className="input"
                        type={field.type === "number" ? "number" : "text"}
                        value={String(scheduleForm.values[field.key] ?? "")}
                        onChange={(event) =>
                          setScheduleForm({
                            ...scheduleForm,
                            values: {
                              ...scheduleForm.values,
                              [field.key]: event.target.value,
                            },
                          })
                        }
                      />
                    )}
                  </label>
                ))}
              </div>
            )}
            <div className="flex justify-end gap-2">
              <button
                className="btn-secondary"
                type="button"
                onClick={() => setScheduleForm(null)}
              >
                取消
              </button>
              <button
                className="btn-primary"
                disabled={saveSchedule.isPending}
                type="button"
                onClick={() => saveSchedule.mutate(scheduleForm)}
              >
                保存
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 5: 注册路由**

在 `web/src/App.tsx` 的导入区加入：

```tsx
import { lazy, Suspense } from "react";
```

```tsx
const WorkflowEditor = lazy(() => import("./pages/WorkflowEditor"));
```

在 `/workflows` 路由之后加入：

```tsx
          <Route
            path="/workflows/:id"
            element={
              <RequireRole minRole="editor">
                <Suspense fallback={<p className="text-sm text-ink-500">加载编辑器…</p>}>
                  <WorkflowEditor />
                </Suspense>
              </RequireRole>
            }
          />
```

- [ ] **Step 6: 运行测试与构建**

```bash
cd web && npm test -- workflow
```

Expected: PASS（`24 passed`）。

```bash
cd web && npx tsc -b
```

Expected: 无输出（退出码 0）。

```bash
cd web && npm run build
```

Expected: 构建成功；编辑器被拆成独立 chunk（`dist/assets/WorkflowEditor-*.js`）。

- [ ] **Step 7: 浏览器验证**

`cd backend && uv run uvicorn app.main:app --reload` 与 `cd web && npm run dev` 同时运行，用 admin 登录：

1. `/workflows` 新建工作流 → 自动进入编辑器；从面板加入「工具」节点，连线，右栏选 `profile.list`，保存 → 刷新后节点与连线仍在。
2. 点「校验」看到「没有发现问题」或具体 issue；点 issue 定位到节点。
3. 发布（填说明）成功；用 editor 账号打开同一工作流，画布只读、无保存/发布按钮，「运行」可用。
4. 新建每日 09:00 定时，右栏显示「每天 09:00（Asia/Shanghai）」与下次运行时间。
5. 修改节点显示名后不保存直接点「返回」，出现确认框。

Expected: 以上行为一致；冲突场景可在两个标签页同时编辑后保存复现，页面显示「重新加载」。

- [ ] **Step 8: 提交**

```bash
git add web/src/workflows.ts web/src/App.tsx web/src/pages/WorkflowEditor.tsx web/src/__tests__/workflow.test.ts
git commit -m "feat(web): add workflow canvas editor with publish and schedules"
```

---

## Task 17: 运行详情与审批页面

**Files:**
- Modify: `web/src/workflows.ts`
- Modify: `web/src/App.tsx`
- Create: `web/src/pages/WorkflowRunDetail.tsx`
- Test: `web/src/__tests__/workflow.test.ts`

**背景与接口**

`/workflows/:id/runs/:runId` 用同一套节点/边渲染运行状态：活跃节点用 sage 描边、失败/驳回用 wine、跳过灰显、等待审核琥珀色标签，已执行路径的边加粗。右栏汇总运行、待处理审批与按节点分组的尝试记录。活跃运行每 2 秒轮询，全部终态即停（沿用 AI 助理页写法）。

**关键约定**

- 画布数据来自运行锁定的版本：`GET /api/workflows/{id}/versions/{version}`；状态来自 `node_runs`，同一节点取最大 `attempt` 的状态。
- 审批卡片按 `source` 选择接口：`workflow` → `/api/workflow-approvals/{id}/decide`，`agent` → `/api/agent/approvals/{id}/decide`；驳回必须填写备注（按钮在备注为空时禁用）。
- 审批可见性沿用 `canDecideWorkflowApproval`：手动运行对发起人隐藏，定时运行对所有管理员可见。
- 取消/续跑按钮由 `canCancelWorkflowRun`/`canResumeWorkflowRun` 控制；续跑上限 10 由后端强制，前端同时禁用。
- 轮询用 `refetchInterval`，`run.status` 为终态时返回 `false`；画布的 `version` 传查询的 `dataUpdatedAt`，每次轮询后状态刷新。
- 产物链接复用 `web/src/agent.ts` 的 `artifactLabel`/`artifactLink`。

- [ ] **Step 1: 追加失败测试**

在 `web/src/__tests__/workflow.test.ts` 的 `../workflows` 导入中加入 `latestNodeStatuses` 与 `runStatusBadge`，并在文件末尾追加：

```ts
describe("运行状态汇总", () => {
  it("同一节点取最大 attempt 的状态，并给运行状态徽标", () => {
    const runs = [
      { node_id: "a", attempt: 1, status: "error" },
      { node_id: "a", attempt: 2, status: "ok" },
      { node_id: "b", attempt: 1, status: "skipped" },
    ] as never;
    expect(latestNodeStatuses(runs)).toEqual({ a: "ok", b: "skipped" });
    expect(runStatusBadge("succeeded")).toContain("sage");
    expect(runStatusBadge("failed")).toContain("wine");
    expect(runStatusBadge("waiting_approval")).toContain("sage-100");
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd web && npm test -- workflow
```

Expected: FAIL，`latestNodeStatuses is not a function`。

- [ ] **Step 3: 补状态辅助函数**

在 `web/src/workflows.ts` 的 `NODE_STATUS_BADGES` 中补充运行级状态：

```ts
  succeeded: "badge bg-sage-50 text-sage-700",
  failed: "badge bg-wine-50 text-wine-700",
  cancelled: "badge bg-line text-ink-500",
  waiting_approval: "badge bg-sage-100 text-sage-700",
```

在 `runStatusLabel` 之后加：

```ts
export function runStatusBadge(status: string): string {
  return nodeStatusBadge(status);
}
```

在 `executedEdgeIds` 之前加：

```ts
/** 同一节点可能重试多次，画布展示最大 attempt 的状态。 */
export function latestNodeStatuses(
  nodeRuns: Pick<WorkflowNodeRun, "node_id" | "attempt" | "status">[],
): Record<string, string> {
  const latest = new Map<string, { attempt: number; status: string }>();
  for (const run of nodeRuns) {
    const current = latest.get(run.node_id);
    if (!current || run.attempt >= current.attempt) {
      latest.set(run.node_id, { attempt: run.attempt, status: run.status });
    }
  }
  return Object.fromEntries(
    [...latest].map(([nodeId, item]) => [nodeId, item.status]),
  );
}
```

- [ ] **Step 4: 写运行详情页**

新建 `web/src/pages/WorkflowRunDetail.tsx`：

```tsx
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  artifactLabel,
  artifactLink,
  formatElapsed,
  usageTokens,
  type Artifact,
} from "../agent";
import { useAuth } from "../auth-context";
import { api } from "../client";
import { errorText } from "../types";
import type {
  WorkflowApproval,
  WorkflowRunDetail as RunDetail,
  WorkflowVersionDetail,
} from "../types/workflow";
import {
  approvalSourceLabel,
  canCancelWorkflowRun,
  canDecideWorkflowApproval,
  canResumeWorkflowRun,
  executedEdgeIds,
  isRunTerminal,
  latestNodeStatuses,
  nodeKindLabel,
  nodeStatusLabel,
  runStatusBadge,
  runStatusLabel,
  triggerLabel,
} from "../workflows";
import WorkflowCanvas from "../components/workflow/WorkflowCanvas";

function pretty(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export default function WorkflowRunDetail() {
  const { id = "", runId = "" } = useParams();
  const queryClient = useQueryClient();
  const { user: me } = useAuth();
  const [error, setError] = useState("");
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const detailQuery = useQuery({
    queryKey: ["workflow-run", runId],
    queryFn: () => api<RunDetail>(`/workflow-runs/${runId}`),
    enabled: Boolean(runId),
    refetchInterval: (query) => {
      const status = query.state.data?.run.status;
      return status && isRunTerminal(status) ? false : 2000;
    },
  });
  const detail = detailQuery.data ?? null;
  const run = detail?.run ?? null;

  const graphQuery = useQuery({
    queryKey: ["workflow-version", id, run?.version],
    queryFn: () =>
      api<WorkflowVersionDetail>(`/workflows/${id}/versions/${run!.version}`),
    enabled: Boolean(id && run?.version),
  });
  const graph = graphQuery.data?.graph ?? null;

  function refresh() {
    void queryClient.invalidateQueries({ queryKey: ["workflow-run", runId] });
    void queryClient.invalidateQueries({ queryKey: ["workflow", id] });
    void queryClient.invalidateQueries({ queryKey: ["workflows"] });
  }

  const cancel = useMutation({
    mutationFn: () =>
      api<unknown>(`/workflow-runs/${runId}/cancel`, { method: "POST" }),
    onSuccess: refresh,
    onError: (err) => setError(errorText(err)),
  });

  const resume = useMutation({
    mutationFn: () =>
      api<unknown>(`/workflow-runs/${runId}/resume`, { method: "POST" }),
    onSuccess: refresh,
    onError: (err) => setError(errorText(err)),
  });

  const decide = useMutation({
    mutationFn: (input: {
      approval: WorkflowApproval;
      decision: "approve" | "reject";
      note: string;
    }) => {
      const path =
        input.approval.source === "agent"
          ? `/agent/approvals/${input.approval.id}/decide`
          : `/workflow-approvals/${input.approval.id}/decide`;
      return api<unknown>(path, {
        method: "POST",
        body: JSON.stringify({ decision: input.decision, note: input.note }),
      });
    },
    onSuccess: (_, input) => {
      setNotes((current) => ({ ...current, [input.approval.id]: "" }));
      setError("");
      refresh();
    },
    onError: (err) => setError(errorText(err)),
  });

  if (detailQuery.isError) {
    return (
      <div className="space-y-3">
        <Link className="text-sm text-sage-700" to={`/workflows/${id}`}>
          ← 返回工作流
        </Link>
        <p className="text-sm text-wine-700">{errorText(detailQuery.error)}</p>
      </div>
    );
  }

  if (!run || !detail) {
    return <p className="text-sm text-ink-500">加载运行详情…</p>;
  }

  const statuses = latestNodeStatuses(detail.node_runs);
  const executed = graph ? executedEdgeIds(graph, detail.node_runs) : new Set<string>();
  const artifacts = Array.isArray(detail.result?.artifacts)
    ? (detail.result.artifacts as Artifact[])
    : [];
  const canCancel = me ? canCancelWorkflowRun(run, me) : false;
  const canResume = me ? canResumeWorkflowRun(run, me) : false;

  const byNode = new Map<string, typeof detail.node_runs>();
  for (const item of detail.node_runs) {
    const list = byNode.get(item.node_id);
    if (list) list.push(item);
    else byNode.set(item.node_id, [item]);
  }

  return (
    <div className="space-y-4">
      <header className="card card-pad flex flex-wrap items-center gap-3">
        <Link className="btn-secondary" to={`/workflows/${id}`}>
          ← 返回工作流
        </Link>
        <div className="min-w-0 flex-1">
          <h1 className="text-lg font-semibold">
            运行 {run.id.slice(0, 8)} · v{run.version}
          </h1>
          <p className="text-xs text-ink-500">
            {triggerLabel(run.trigger)} · {run.created_at ? new Date(run.created_at).toLocaleString() : "—"}
          </p>
        </div>
        <span className={runStatusBadge(run.status)}>{runStatusLabel(run.status)}</span>
        {canCancel && (
          <button
            className="btn-danger"
            disabled={cancel.isPending}
            type="button"
            onClick={() => cancel.mutate()}
          >
            取消运行
          </button>
        )}
        {canResume && (
          <button
            className="btn-primary"
            disabled={resume.isPending || run.resume_count >= 10}
            title={run.resume_count >= 10 ? "已达到续跑上限（10 次）" : ""}
            type="button"
            onClick={() => resume.mutate()}
          >
            从失败处续跑
          </button>
        )}
      </header>

      {error && <p className="text-sm text-wine-700">{error}</p>}
      {run.error_code && (
        <div className="rounded-lg bg-wine-50 px-3 py-2 text-sm text-wine-700">
          <span className="font-medium">{run.error_code}</span>
          {run.error_message && <span> · {run.error_message}</span>}
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_340px]">
        <div className="space-y-2">
          {graph ? (
            <WorkflowCanvas
              graph={graph}
              version={detailQuery.dataUpdatedAt}
              readOnly
              selectedId={selectedId}
              statuses={statuses}
              executedEdges={executed}
              onSelect={setSelectedId}
            />
          ) : (
            <p className="text-sm text-ink-500">加载版本图…</p>
          )}
          {graphQuery.isError && (
            <p className="text-sm text-wine-700">{errorText(graphQuery.error)}</p>
          )}
          {artifacts.length > 0 && (
            <section className="card card-pad space-y-2">
              <h2 className="text-sm font-semibold">产物</h2>
              <ul className="space-y-1 text-sm">
                {artifacts.map((artifact, index) => (
                  <li key={`${artifact.type}-${artifact.id ?? index}`}>
                    <Link className="text-sage-700" to={artifactLink(artifact)}>
                      {artifactLabel(artifact.type ?? "")}
                      {artifact.title ? ` · ${artifact.title}` : ""}
                    </Link>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>

        <aside className="space-y-3">
          <section className="card card-pad grid grid-cols-2 gap-2 text-sm">
            <div>
              <p className="text-xs text-ink-500">触发方式</p>
              <p>{triggerLabel(run.trigger)}</p>
            </div>
            <div>
              <p className="text-xs text-ink-500">耗时</p>
              <p>{formatElapsed(run.started_at, run.finished_at) || "—"}</p>
            </div>
            <div>
              <p className="text-xs text-ink-500">Tokens</p>
              <p>{usageTokens(run.usage)}</p>
            </div>
            <div>
              <p className="text-xs text-ink-500">节点尝试</p>
              <p>{detail.node_runs.length}</p>
            </div>
            <div>
              <p className="text-xs text-ink-500">续跑次数</p>
              <p>
                {run.resume_count}/10
              </p>
            </div>
            <div>
              <p className="text-xs text-ink-500">入参</p>
              <p className="truncate" title={pretty(run.inputs)}>
                {Object.keys(run.inputs).length} 个字段
              </p>
            </div>
          </section>

          {detail.approvals.length > 0 && (
            <section className="card card-pad space-y-3">
              <h2 className="text-sm font-semibold">待处理审批</h2>
              {detail.approvals.map((approval) => {
                const note = notes[approval.id] ?? "";
                const decidable = me ? canDecideWorkflowApproval(approval, run, me) : false;
                return (
                  <div key={approval.id} className="space-y-2 rounded-lg border border-line p-3">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-sm font-medium">{approval.title}</span>
                      <span className="badge bg-sage-100 text-sage-700">
                        {approvalSourceLabel(approval.source)}
                      </span>
                    </div>
                    <p className="text-xs text-ink-500">
                      节点 {approval.node_id || "—"}
                      {approval.tool_name && ` · 工具 ${approval.tool_name}`}
                      {approval.risk && ` · 风险 ${approval.risk}`}
                    </p>
                    {Object.keys(approval.payload).length > 0 && (
                      <pre className="max-h-32 overflow-auto rounded bg-paper p-2 text-[11px] text-ink-700">
                        {pretty(approval.payload)}
                      </pre>
                    )}
                    {decidable ? (
                      <div className="space-y-1">
                        <textarea
                          className="input text-xs"
                          rows={2}
                          placeholder="备注（驳回必填）"
                          value={note}
                          onChange={(event) =>
                            setNotes({ ...notes, [approval.id]: event.target.value })
                          }
                        />
                        <div className="flex gap-2">
                          <button
                            className="btn-primary"
                            disabled={decide.isPending}
                            type="button"
                            onClick={() =>
                              decide.mutate({ approval, decision: "approve", note })
                            }
                          >
                            通过
                          </button>
                          <button
                            className="btn-danger"
                            disabled={decide.isPending || !note.trim()}
                            type="button"
                            onClick={() =>
                              decide.mutate({ approval, decision: "reject", note })
                            }
                          >
                            驳回
                          </button>
                        </div>
                      </div>
                    ) : (
                      <p className="text-xs text-ink-500">
                        由发起人之外的管理员处理。
                      </p>
                    )}
                  </div>
                );
              })}
            </section>
          )}

          <section className="card card-pad space-y-2">
            <h2 className="text-sm font-semibold">节点尝试</h2>
            {byNode.size === 0 && <p className="text-xs text-ink-500">还没有节点记录。</p>}
            {[...byNode.entries()].map(([nodeId, attempts]) => (
              <details key={nodeId} className="rounded-lg border border-line p-2">
                <summary className="cursor-pointer text-sm">
                  <span className="font-medium">{nodeId}</span>
                  <span className="ml-2 text-xs text-ink-500">
                    {attempts.length} 次 · {nodeKindLabel(attempts[0].kind)} ·{" "}
                    {nodeStatusLabel(attempts[attempts.length - 1].status)}
                  </span>
                </summary>
                <div className="mt-2 space-y-2">
                  {attempts.map((attempt) => (
                    <div key={`${attempt.node_id}-${attempt.attempt}`} className="space-y-1">
                      <p className="text-xs text-ink-500">
                        第 {attempt.attempt} 次 · {nodeStatusLabel(attempt.status)} ·{" "}
                        {attempt.duration_ms}ms
                        {attempt.error && ` · ${attempt.error}`}
                      </p>
                      <pre className="max-h-32 overflow-auto rounded bg-paper p-2 text-[11px] text-ink-700">
                        {pretty(attempt.input)}
                      </pre>
                      <pre className="max-h-40 overflow-auto rounded bg-paper p-2 text-[11px] text-ink-700">
                        {pretty(attempt.output)}
                      </pre>
                    </div>
                  ))}
                </div>
              </details>
            ))}
          </section>
        </aside>
      </div>
    </div>
  );
}
```

- [ ] **Step 5: 注册路由**

在 `web/src/App.tsx` 的导入区加入：

```tsx
const WorkflowRunDetail = lazy(() => import("./pages/WorkflowRunDetail"));
```

在 `/workflows/:id` 路由之后加入：

```tsx
          <Route
            path="/workflows/:id/runs/:runId"
            element={
              <RequireRole minRole="editor">
                <Suspense fallback={<p className="text-sm text-ink-500">加载运行…</p>}>
                  <WorkflowRunDetail />
                </Suspense>
              </RequireRole>
            }
          />
```

- [ ] **Step 6: 运行测试与构建**

```bash
cd web && npm test -- workflow
```

Expected: PASS（`25 passed`）。

```bash
cd web && npx tsc -b
```

Expected: 无输出（退出码 0）。

```bash
cd web && npm run build
```

Expected: 构建成功。

- [ ] **Step 7: 提交**

```bash
git add web/src/workflows.ts web/src/App.tsx web/src/pages/WorkflowRunDetail.tsx web/src/__tests__/workflow.test.ts
git commit -m "feat(web): add workflow run detail with approvals"
```

---

## Task 18: 文档、真实模型端到端与全量验证

**Files:**
- Create: `backend/tests/test_workflow_e2e.py`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `CHANGELOG.md`

**背景与接口：** 前 17 个任务把引擎、API、worker、画布与运行页都建好了，但没有一条链路在真实模型下跑过，也没有任何文档入口。本任务补上真实模型 E2E（输入主题 → Agent 产出内容与草稿 → 输出），补齐 6 个 `WORKFLOW_*` 配置示例、README 与 CHANGELOG，然后做全量验证与浏览器验收。

**关键约定（实现时照抄）：**
- E2E 用例与第 1 层同构：默认跳过，`RUN_WORKFLOW_E2E=1` 才执行；从开发库只读复制默认 LLM 提供商，不修改原配置；没有提供商时 `pytest.fail` 而不是跳过。
- 测试种子与第 1 层一致：`content.create_version` 只能给既有内容追加版本，所以先种一个项目与一条 `xiaohongshu` / `article` 内容。
- Agent 节点的产物在 `WorkflowNodeRun.output["result"]["artifacts"]`，不在 `WorkflowRun.result["artifacts"]`（输出节点只汇总直接相连的工具节点产物）。
- 真实模型可能请求人工确认；出现 `waiting_approval` 时跳过并说明审批链路已由 `test_workflow_agent_node.py` / `test_workflow_api.py` 覆盖。
- 文档命令统一 `cd backend && uv run ...` 与 `cd web && npm ...`；不要写入任何真实密钥。

- [ ] **Step 1: 写真实模型端到端测试**

Create `backend/tests/test_workflow_e2e.py`:

```python
import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.registry import get_tool_registry
from app.config import get_settings
from app.llm.gateway import load_gateway_default
from app.models.agent import AgentRun
from app.models.project import Content, ContentVersion, Project
from app.models.publish import PublishJob
from app.models.settings import LLMProvider
from app.models.user import User
from app.models.workflow import (
    Workflow,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowVersion,
)
from app.seed import ensure_admin
from app.skills.registry import get_registry
from app.storage import get_storage
from app.workflow.engine import EngineDeps, run_workflow

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_WORKFLOW_E2E") != "1",
    reason="设置 RUN_WORKFLOW_E2E=1 且配置可用 LLM 提供商后才调用真实模型",
)

def graph_for(content_id: uuid.UUID) -> dict:
    return {
        "nodes": [
            {
                "id": "in",
                "type": "input",
                "position": {"x": 0, "y": 0},
                "data": {
                    "fields": [
                        {"key": "topic", "type": "string", "required": True}
                    ]
                },
            },
            {
                "id": "writer",
                "type": "agent",
                "position": {"x": 240, "y": 0},
                "data": {
                    "goal": (
                        "围绕 {{inputs.topic}} 为主题：先用 project.create 建一个"
                        "标题为「E2E 工作流选题」的项目；再为已有内容"
                        f" {content_id} 调用 content.create_version 写一版小红书图文；"
                        "最后为这条内容调用 publish.create_draft_job"
                        "（platform 传 xiaohongshu）创建发布草稿。不要真正发布。"
                    ),
                    "tools": [
                        "project.create",
                        "content.create_version",
                        "publish.create_draft_job",
                    ],
                    "budget": {"max_steps": 8, "max_seconds": 600},
                    "retry": 0,
                },
            },
            {
                "id": "out",
                "type": "output",
                "position": {"x": 480, "y": 0},
                "data": {},
            },
        ],
        "edges": [
            {"id": "in-writer", "source": "in", "target": "writer"},
            {"id": "writer-out", "source": "writer", "target": "out"},
        ],
    }


async def _copy_default_provider(session, tenant_id: str) -> None:
    """把本地库中已配置的默认提供商只读复制进测试会话，不改动原配置。"""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as source:
            provider = (
                await source.execute(
                    select(LLMProvider)
                    .where(
                        LLMProvider.tenant_id == tenant_id,
                        LLMProvider.is_default.is_(True),
                    )
                    .order_by(LLMProvider.created_at, LLMProvider.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
    finally:
        await engine.dispose()
    if provider is None:
        pytest.fail("RUN_WORKFLOW_E2E=1 需要开发库中配置默认 LLM 提供商")
    session.add(
        LLMProvider(
            tenant_id=tenant_id,
            name=provider.name,
            kind=provider.kind,
            base_url=provider.base_url,
            api_key_enc=provider.api_key_enc,
            tiers=dict(provider.tiers or {}),
            is_default=True,
        )
    )
    await session.commit()


async def test_workflow_reaches_draft_with_real_llm(db_session):
    await ensure_admin(db_session)
    user = (
        await db_session.execute(
            select(User).where(User.role == "owner").limit(1)
        )
    ).scalar_one()
    settings = get_settings()
    await _copy_default_provider(db_session, user.tenant_id)
    gateway = await load_gateway_default(db_session, user.tenant_id)

    seeded_project = Project(
        tenant_id=user.tenant_id, title="E2E 既有项目", topic="演示"
    )
    db_session.add(seeded_project)
    await db_session.flush()
    seeded_content = Content(
        tenant_id=user.tenant_id,
        project_id=seeded_project.id,
        platform="xiaohongshu",
        kind="article",
        title="E2E 既有内容",
    )
    db_session.add(seeded_content)
    await db_session.commit()

    graph = graph_for(seeded_content.id)
    workflow = Workflow(
        tenant_id=user.tenant_id,
        name="E2E 工作流",
        description="真实模型端到端",
        created_by=user.id,
        draft_graph=graph,
        draft_revision=1,
    )
    db_session.add(workflow)
    await db_session.flush()
    version = WorkflowVersion(
        tenant_id=user.tenant_id,
        workflow_id=workflow.id,
        version=1,
        graph=graph,
        source_revision=1,
        published_by=user.id,
        published_at=datetime.now(UTC),
        note="E2E",
    )
    db_session.add(version)
    await db_session.flush()
    run = WorkflowRun(
        tenant_id=user.tenant_id,
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        trigger="manual",
        triggered_by=user.id,
        status="queued",
        inputs={"topic": "秋季护肤"},
        policy={},
        started_at=datetime.now(UTC),
    )
    db_session.add(run)
    await db_session.commit()

    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    deps = EngineDeps(
        gateway=gateway,
        tool_registry=get_tool_registry(),
        skill_registry=get_registry(),
        storage=get_storage(),
        queue=None,
        settings=settings,
    )
    outcome = await run_workflow(factory, run.id, deps=deps)
    assert outcome in {"succeeded", "waiting_approval"}

    await db_session.refresh(run)
    if run.status == "waiting_approval":
        pytest.skip(
            "模型请求人工确认，审批链路已由 test_workflow_agent_node.py 覆盖"
        )

    assert run.status == "succeeded", run.error_message
    node_run = (
        await db_session.execute(
            select(WorkflowNodeRun).where(
                WorkflowNodeRun.run_id == run.id,
                WorkflowNodeRun.node_id == "writer",
            )
        )
    ).scalars().one()
    assert node_run.status == "ok"
    assert node_run.agent_run_id is not None
    assert run.result["items"][0]["node_id"] == "writer"
    artifacts = node_run.output["result"]["artifacts"]
    types = {item["type"] for item in artifacts}
    assert {"project", "content", "publish_job"} <= types

    project_id = next(
        item["id"] for item in artifacts if item["type"] == "project"
    )
    project = await db_session.get(Project, uuid.UUID(project_id))
    assert project is not None and project.tenant_id == user.tenant_id

    content_ids = [
        uuid.UUID(item["id"]) for item in artifacts if item["type"] == "content"
    ]
    versions = (
        (
            await db_session.execute(
                select(ContentVersion).where(
                    ContentVersion.content_id.in_(content_ids),
                    ContentVersion.tenant_id == user.tenant_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert versions, "应至少生成一版内容版本"

    jobs = (
        (
            await db_session.execute(
                select(PublishJob).where(
                    PublishJob.content_id.in_(content_ids),
                    PublishJob.tenant_id == user.tenant_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert jobs, "应创建发布草稿任务"
    assert all(job.status == "draft" for job in jobs)
    assert all(job.scheduled_at is None for job in jobs)

    child = (
        await db_session.execute(
            select(AgentRun).where(AgentRun.workflow_run_id == run.id)
        )
    ).scalars().one()
    assert child.mode == "node"
    assert child.status == "succeeded"
    assert run.usage.get("nodes", 0) >= 1
```

- [ ] **Step 2: 运行测试确认默认跳过**

```bash
cd backend && uv run pytest tests/test_workflow_e2e.py -q
```

Expected: `1 skipped`，原因为未设置 `RUN_WORKFLOW_E2E`。

- [ ] **Step 3: 补环境变量示例**

在 `.env.example` 的 `AGENT_RUN_STALE_SECONDS=300` 之后追加：

```bash
# 工作流引擎：单次运行并行节点上限、单次推进时间片（秒，<=0 关闭）、运行墙钟上限（秒）、审批有效期（小时）、每用户活跃运行上限、心跳超时（秒）
WORKFLOW_MAX_PARALLEL_NODES=3
WORKFLOW_TIME_SLICE_SECONDS=480
WORKFLOW_MAX_SECONDS=3600
WORKFLOW_APPROVAL_TTL_HOURS=24
WORKFLOW_MAX_ACTIVE_RUNS_PER_USER=2
WORKFLOW_RUN_STALE_SECONDS=300
```

- [ ] **Step 4: 补 README**

在 `README.md` 的 `### 发布边界` 之前插入：

```markdown
### 工作流（第 2 层）

- **入口**：`/workflows` 列表与内置模板；`/workflows/:id` 画布编辑器；`/workflows/:id/runs/:runId` 运行详情。admin/owner 可编辑与发布，editor 可运行已发布版本，viewer 只读。
- **节点**：输入、工具、Agent、条件、审核、输出六类；DAG 支持并行分支与汇合，条件节点用 `true`/`false` 两条出边，汇合节点在全部活跃分支结束后执行一次。
- **版本**：草稿自动保存（`draft_revision` 乐观锁），发布生成不可变 `workflow_versions`；编辑草稿不影响已发布版本与历史运行，可把任意历史版本恢复为草稿。
- **运行**：`POST /api/workflows/{id}/runs` 返回 `202` 并入队 arq 的 `run_workflow_job`；按 `WORKFLOW_TIME_SLICE_SECONDS` 分片推进，崩溃由心跳对账续跑，失败可从失败处续跑（`resume_count < 10`）。
- **审批与发布边界**：命中租户策略 `confirm` 的工具或审核节点挂起为 `waiting_approval`，由非发起人的 admin/owner 决定；工作流只调用注册表里已有的工具，真实发布与排期仍无工具，只创建 `status=draft` 的发布任务。
- **定时**：`POST /api/workflows/{id}/schedules` 支持每日/每周（星期 1=周一 … 7=周日）+ 本地时间与时区；`(schedule_id, scheduled_for)` 唯一约束保证到点只触发一次，工作流已有活跃运行时跳过并写 `workflow.schedule.skipped` 审计。
- **配置**：6 个 `WORKFLOW_*` 见 `.env.example`；真实模型验收见下方「测试」。

真实模型验收（前置条件：可连接的开发 PostgreSQL，且该租户已配置默认 LLM 提供商；缺失时用例报错而非跳过）：

```bash
cd backend && RUN_WORKFLOW_E2E=1 uv run pytest tests/test_workflow_e2e.py -q
```
```

在 `README.md` 的 `## 测试` 一节末尾追加：

```markdown
工作流真实模型用例默认跳过，设置 `RUN_WORKFLOW_E2E=1` 才会执行。
```

- [ ] **Step 5: 补 CHANGELOG**

在 `CHANGELOG.md` 的 `## [Unreleased]` → `### 新增` 列表末尾追加：

```markdown
- **工作流引擎**：新增 `workflows`/`workflow_versions`/`workflow_schedules`/`workflow_runs`/`workflow_node_runs`/`workflow_approvals` 六张表与迁移，`agent_runs` 增加 `workflow_run_id`/`workflow_node_id`；新增 `app/workflow/` 包（图解析、发布校验、变量解析、引擎、审批、生命周期、定时与内置模板），支持并行分支、条件跳过、节点重试、时间片推进、崩溃续跑与从失败处续跑。
- **工作流 API**：新增 `/api/workflows` 的定义、版本、发布、恢复与模板实例化接口，`/api/workflows/{id}/schedules` 定时接口，`/api/workflows/{id}/runs` 与 `/api/workflow-runs/{id}` 运行/取消/续跑接口，`/api/workflow-approvals` 审批接口，以及供编辑器使用的 `GET /api/agent/tools`；草稿保存用 `revision` 乐观锁，发布失败返回 `422 workflow_invalid` 与 `detail.issues`。
- **工作流 worker**：新增 `run_workflow_job`、`fire_due_workflow_schedules`、`reconcile_workflow_runs` 三个任务与 cron；定时先 CAS 推进 `next_run_at` 再建运行，活跃运行跳过；第 1 层子运行审批接入父运行，父子不会并发执行。
- **工作流画布**：前端新增 React Flow 画布编辑器（节点面板、属性面板、连线校验、条件真假句柄、撤销/重做、草稿保存与发布校验抽屉）、工作流列表与模板实例化、运行详情页（节点状态、审批、产物、取消/续跑，活跃运行每 2 秒轮询）。
- **分级自治**：工作流复用第 1 层策略矩阵，租户只能收紧不能放宽；审核节点与 `confirm` 工具统一挂起审批，真实发布在工作流里结构上不可达。
```

在 `### 验证` 列表末尾追加（数字照抄本次命令输出）：

```markdown
- 工作流：`uv run pytest -q` 与 `uv run ruff check .` 通过，`npm test`、`npm run build` 通过；`RUN_WORKFLOW_E2E=1` 真实模型用例在「输入主题 → Agent → 输出」图上产出项目、内容版本与 `draft` 发布任务（未排期）；浏览器验收走通创建 → 编辑 → 保存 → 发布 → 运行 → 审核通过 → 查看产物。
```

- [ ] **Step 6: 全量验证**

```bash
cd backend && uv run pytest -q
cd backend && uv run ruff check .
cd web && npm test
cd web && npm run build
```

Expected: 后端全部 PASS（含新增 workflow 用例、`test_workflow_e2e.py` 显示 skipped）、ruff 无告警；前端 `npm test` 全部 PASS，构建成功。

- [ ] **Step 7: 浏览器验收**

```bash
make up && make migrate
make dev
make worker
npm --prefix web run dev
```

（`make dev` 与 `make worker` 各占一个终端；`make dev` 是 API，不是 `make api`。）

在浏览器中逐项确认：

1. admin 登录后导航出现「工作流」，`/workflows` 可看到模板列表与新建表单。
2. 用模板「每周选题 → 创作 → 审核 → 建草稿」实例化，进入画布后拖动节点、添加工具节点、连线；刷新后草稿仍在。
3. 未发布时「运行」按钮不可用；编辑后用第二个标签页修改同一工作流，先保存的标签页再保存会看到 `workflow_conflict` 提示并可重新加载。
4. 发布后「运行」可用，填写输入主题后跳转运行详情；节点状态与连线高亮随 2 秒轮询更新。
5. 审核节点挂起时，用另一个 admin 账号在运行详情批准，运行继续并变为 `succeeded`；驳回则显示 `approval_rejected`。
6. 运行结束后产物区可点开内容版本与 `draft` 发布任务；发布任务未排期。
7. editor 账号可运行但不能编辑/发布；viewer 打开 `/workflows` 只看到只读列表。

- [ ] **Step 8: 真实模型验收**

```bash
cd backend && RUN_WORKFLOW_E2E=1 uv run pytest tests/test_workflow_e2e.py -q
```

Expected: `1 passed`；Agent 节点子运行 `mode="node"` 且 `succeeded`；产物含项目、内容版本与 `publish_job`，发布任务全部为 `draft` 且 `scheduled_at` 为空。

- [ ] **Step 9: 提交**

```bash
cd /Users/tongxi/Desktop/新媒体运营系统
git add .env.example README.md CHANGELOG.md backend/tests/test_workflow_e2e.py
git commit -m "docs(workflow): document engine, canvas and e2e verification"
```

## 验收对照

| 规格验收项 | 落点 |
|---|---|
| 模板到「修改 → 发布 → 运行」全程在画布完成，手动运行产出内容版本与 `draft` 任务 | Task 9 模板/发布、Task 16 编辑器、Task 18 真实模型 E2E |
| 并行分支重叠执行，汇合节点只执行一次且拿到全部分支输出 | Task 2 就绪判定、Task 6 并行推进与输出汇总 |
| 审批与审核：非发起人批准继续；驳回 `failed/approval_rejected`；过期 `failed/approval_expired` | Task 5 审批读写、Task 6/7 挂起与恢复、Task 11 决策接口、Task 17 审批 UI |
| 从失败处续跑不重跑已成功节点、写工具不重复产物 | Task 6 幂等与续跑、Task 8 生命周期、Task 11 续跑接口 |
| 定时只触发一次；活跃运行跳过；草稿不影响已发布版本与历史运行 | Task 4 cadence、Task 8 领取、Task 10 定时 API、Task 12 定时触发 |
| 角色矩阵与双租户隔离自动化覆盖；真发布结构上不可达 | Task 9/10/11 API 用例、Task 12 worker 用例、Task 18 浏览器验收 |
| 后端 lint、前后端测试与构建全绿；`RUN_WORKFLOW_E2E=1` 通过 | Task 18 Step 6/8 |
| README 增「工作流」一节、`.env.example` 增 6 个 `WORKFLOW_*`、CHANGELOG 记录 | Task 18 Step 3/4/5 |

<!-- plan-end -->

