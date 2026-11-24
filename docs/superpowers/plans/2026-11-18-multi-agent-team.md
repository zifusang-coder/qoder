# 多 Agent 团队（第 3 层）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在第 1 层 Agent 运行时与第 2 层工作流之上交付五角色串行协作团队：Lead 结构化决策、追加式黑板、Critic 硬否决（≤2 轮返工）、团队预算、审批唤醒与对账、工作流 `team` 节点与双前端入口。

**Architecture:** 团队父运行是 `agent_runs(mode="team")`，每个角色执行是子运行 `agent_runs(mode="role", parent_run_id)`。新增 `app/agent/team.py` 编排器以结构化 Lead 决策驱动，串行调用既有 `run_agent`（与工作流 Agent 节点同形状），结果追加到 `agent_team_entries` 黑板。不新增第二个执行内核。

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy async / Alembic / arq / pytest（SQLite+aiosqlite 夹具）；React 18 + Vite + TypeScript + TanStack Query + React Flow / Vitest。

**Spec:** `docs/superpowers/specs/2026-11-18-multi-agent-team-design.md`

## Global Constraints

- 所有后端命令在 `backend/` 下用 `uv run pytest ...` / `uv run ruff check .` / `uv run alembic ...` 执行；禁止裸 `python`/`pytest`/`alembic`。前端命令在 `web/` 下 `npm test` / `npm run build`。
- 不引入第三方 Agent 框架、不新增执行内核；角色执行一律复用 `app.agent.runtime.run_agent`。
- 真发布、排期、导出工具结构上不可达：注册表不提供；Publisher 只建 `draft` 且 `scheduled_at=None`。
- 租户只能收窄工具、调低预算；`planner`/`creator` 不可禁用；提示词代码管理不可在线编辑。
- 所有查询按 `tenant_id` 过滤；跨租户访问返回 404；API 响应与黑板不含原始凭据（入库前 `scrub` + `fit_bytes`）。
- 审批人沿用第 1 层：admin/owner 且非发起人；viewer 无写入口。
- 迁移保持单一 head（当前 head `e5f6a7b8c9d0`）；不改既有列语义；历史行 `role=""`、`team_state={}`。
- 测试不依赖真实模型（脚本化网关）；不写 Postgres 专属并发测试；真实模型 E2E 用 `RUN_TEAM_E2E=1` 门控、默认跳过。
- 不修改全局 git config；不 push/amend；每个任务结束提交一次（提交信息见各任务）。
- 角色默认预算/白名单是代码常量，不进环境变量；团队预算字段进 `Settings` 与 `.env.example`。

## 文件结构

**新建（后端）**
- `backend/app/agent/roles.py` — 角色常量、租户生效配置解析、收窄校验、团队预算 clamp、参与角色校验。
- `backend/app/agent/blackboard.py` — 黑板追加（seq 分配、脱敏截断）、最近 N 条读取与渲染。
- `backend/app/agent/team.py` — 编排器：决策契约、`run_team` 循环、委派/Critic/返工/循环/预算/恢复、失败/取消/子运行收敛。
- `backend/app/api/agent_team.py` — `/agent/team/*` 路由（角色配置、发起、详情、取消）。
- `backend/migrations/versions/f6a7b8c9d0e1_add_agent_team.py` — 三列 + 两表迁移。
- `backend/tests/test_team_models.py`、`test_team_blackboard.py`、`test_team_roles.py`、`test_team_orchestrator.py`、`test_team_approvals.py`、`test_team_worker.py`、`test_team_api.py`、`test_workflow_team_node.py`、`test_team_e2e.py`。

**修改（后端）**
- `backend/app/config.py`、`backend/.env.example` — 8 个团队配置字段。
- `backend/app/models/agent.py` — `AgentRun` 三列；`AgentTeamEntry`、`AgentTeamRole`。
- `backend/app/api/agent.py` — `RunOut` 增 `role`/`parent_run_id`；decide 团队分支 `_wake_parent_team`/驳回终结；retry 团队门禁。
- `backend/app/worker.py` — `run_agent_job` 按 mode 分派；stale 扫描排除 `role`；对账新增等待团队结算与终态残留收敛。
- `backend/app/workflow/graph.py`、`validate.py`、`engine.py`、`templates.py` — `team` 节点。
- `backend/app/main.py` — 挂载 `agent_team.router`。

**新建/修改（前端）**
- `web/src/agentTeam.ts`（新）+ `web/src/__tests__/agentTeam.test.ts`（新）。
- `web/src/pages/TeamRunDetail.tsx`（新）、`web/src/pages/TeamRoles.tsx`（新）。
- `web/src/pages/Assistant.tsx` — 「协作团队」页签与发起面板。
- `web/src/agent.ts` — `AgentRun` 增 `role`/`parent_run_id` 可选字段。
- `web/src/App.tsx`、`web/src/pages/Settings.tsx` — 路由与入口链接。
- `web/src/workflows.ts`、`web/src/components/workflow/NodePalette.tsx`、`NodeInspector.tsx`、`WorkflowCanvas.tsx`、`web/src/pages/WorkflowRunDetail.tsx` — 团队节点支持。
- `README.md`、`docs/superpowers/specs/2026-11-13-ai-native-automation-design.md` — 文档收尾。

---

### Task 1: 配置、角色常量、模型与迁移

**Files:**
- Modify: `backend/app/config.py`（agent 配置块之后）
- Modify: `backend/.env.example`
- Create: `backend/app/agent/roles.py`
- Modify: `backend/app/models/agent.py`
- Create: `backend/migrations/versions/f6a7b8c9d0e1_add_agent_team.py`
- Test: `backend/tests/test_team_models.py`

**Interfaces:**
- Produces: `ROLES`/`ROLE_KEYS`/`EXECUTION_ROLES`/`RoleSpec`（Task 3/5/6/10/12 消费）；`AgentRun.parent_run_id/role/team_state`、`AgentTeamEntry`、`AgentTeamRole`（全部后续任务消费）；`settings.agent_team_*` 8 字段。

- [ ] **Step 1: 写失败测试** `backend/tests/test_team_models.py`

```python
import uuid

import pytest
from sqlalchemy import select

from app.agent.roles import EXECUTION_ROLES, ROLE_KEYS, ROLES
from app.config import get_settings
from app.models.agent import AgentRun, AgentTeamEntry, AgentTeamRole
from app.models.base import DEFAULT_TENANT_ID


def test_role_constants():
    assert ROLE_KEYS == ("planner", "scout", "creator", "critic", "publisher")
    assert EXECUTION_ROLES == ("scout", "creator", "critic", "publisher")
    assert ROLES["planner"].required and ROLES["creator"].required
    assert not ROLES["scout"].required
    # scout = planner 白名单 + media.job.get
    assert ROLES["scout"].tools == ROLES["planner"].tools + ("media.job.get",)
    for spec in ROLES.values():
        assert set(spec.budget) == {
            "max_steps", "max_tokens", "max_tool_calls", "max_seconds"
        }
        assert all(v > 0 for v in spec.budget.values())
    # 任何角色都拿不到排期/真发布/导出工具
    forbidden = ("publish.schedule", "publish.execute", "publish.export")
    for spec in ROLES.values():
        assert not set(spec.tools) & set(forbidden)


def test_team_settings_defaults():
    settings = get_settings()
    assert settings.agent_team_max_turns == 12
    assert settings.agent_team_rework_limit == 2
    assert settings.agent_team_blackboard_inject == 12
    assert settings.agent_team_max_tokens == 120000
    assert settings.agent_team_max_tool_calls == 40
    assert settings.agent_team_max_seconds == 900
    assert settings.agent_team_token_ceiling == 200000
    assert settings.agent_team_seconds_ceiling == 1800


@pytest.mark.asyncio
async def test_team_models(db_session):
    parent = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=uuid.uuid4(), goal="团队目标",
        mode="team",
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)
    assert parent.role == "" and parent.team_state == {}
    assert parent.parent_run_id is None

    child = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=parent.user_id, goal="子任务",
        mode="role", role="scout", parent_run_id=parent.id,
    )
    entry = AgentTeamEntry(
        tenant_id=DEFAULT_TENANT_ID, run_id=parent.id, seq=1, kind="goal",
        content={"goal": "团队目标"},
    )
    config = AgentTeamRole(tenant_id=DEFAULT_TENANT_ID, role="scout")
    db_session.add_all([child, entry, config])
    await db_session.commit()

    rows = (await db_session.execute(
        select(AgentRun).where(AgentRun.parent_run_id == parent.id)
    )).scalars().all()
    assert [row.role for row in rows] == ["scout"]
    assert config.enabled is True and config.tools == [] and config.budget == {}
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_team_models.py -q`
Expected: FAIL（`ImportError: app.agent.roles` / 模型缺列）。

- [ ] **Step 3: 实现**

`config.py` agent 块后追加：

```python
    # 团队（第 3 层）预算与编排上限
    agent_team_max_turns: int = 12
    agent_team_rework_limit: int = 2
    agent_team_blackboard_inject: int = 12
    agent_team_max_tokens: int = 120000
    agent_team_max_tool_calls: int = 40
    agent_team_max_seconds: int = 900
    agent_team_token_ceiling: int = 200000
    agent_team_seconds_ceiling: int = 1800
```

`.env.example` 对应追加 8 行 `AGENT_TEAM_...=` 同名默认值。

`roles.py` 完整内容（角色表逐字来自规格 §3）：

```python
"""内置团队角色：提示词、工具白名单与默认预算。代码管理，租户只能收窄。"""

from dataclasses import dataclass

ROLE_BUDGET_KEYS = ("max_steps", "max_tokens", "max_tool_calls", "max_seconds")
TEAM_BUDGET_KEYS = ("max_tokens", "max_tool_calls", "max_turns", "max_seconds")
EXECUTION_ROLES = ("scout", "creator", "critic", "publisher")
ROLE_KEYS = ("planner",) + EXECUTION_ROLES

PLANNER_TOOLS = (
    "project.get",
    "project.list",
    "content.get",
    "content.list",
    "profile.get",
    "profile.list",
    "platform.capabilities",
    "analytics.summary",
    "calendar.list",
    "publish.job.get",
    "publish.job.list",
)


@dataclass(frozen=True)
class RoleSpec:
    key: str
    label: str
    system_prompt: str
    tools: tuple[str, ...]
    budget: dict[str, int]
    required: bool = False


ROLES: dict[str, RoleSpec] = {
    "planner": RoleSpec(
        key="planner",
        label="Lead 规划",
        system_prompt=(
            "你是内容团队的 Lead。根据目标与黑板，每轮决定：委派一个执行角色、"
            "要求 creator 返工、或 finalize 交付。只输出一个 JSON 对象，不要其他文字。"
        ),
        tools=PLANNER_TOOLS,
        budget={"max_steps": 4, "max_tokens": 12000, "max_tool_calls": 4, "max_seconds": 180},
        required=True,
    ),
    "scout": RoleSpec(
        key="scout",
        label="Scout 调研",
        system_prompt=(
            "你是内容团队的调研角色。查画像、历史内容、数据表现与平台能力，"
            "输出事实与约束；不要创作内容。"
        ),
        tools=PLANNER_TOOLS + ("media.job.get",),
        budget={"max_steps": 6, "max_tokens": 20000, "max_tool_calls": 8, "max_seconds": 240},
    ),
    "creator": RoleSpec(
        key="creator",
        label="Creator 创作",
        system_prompt=(
            "你是内容团队的创作角色。建项目、写内容版本、跑创作技能、生成媒体；"
            "发布相关操作只能创建草稿，不能声称已经发布。"
        ),
        tools=(
            "project.create", "project.get", "project.list",
            "content.get", "content.list", "content.create_version",
            "profile.get", "profile.list",
            "skill.run", "media.generate", "media.job.get",
            "platform.capabilities",
        ),
        budget={"max_steps": 8, "max_tokens": 40000, "max_tool_calls": 10, "max_seconds": 360},
        required=True,
    ),
    "critic": RoleSpec(
        key="critic",
        label="Critic 审稿",
        system_prompt=(
            "你是内容团队的审稿角色。只读评审交付内容，"
            '最终只输出 JSON：{"verdict":"pass|fail","issues":["..."]}。'
        ),
        tools=(
            "content.get", "content.list", "platform.capabilities",
            "publish.job.get", "publish.job.list", "analytics.summary",
        ),
        budget={"max_steps": 4, "max_tokens": 15000, "max_tool_calls": 4, "max_seconds": 180},
    ),
    "publisher": RoleSpec(
        key="publisher",
        label="Publisher 交付",
        system_prompt=(
            "你是内容团队的交付角色。创建发布草稿并预检；不排期、不发布。"
        ),
        tools=(
            "content.get", "content.list", "platform.capabilities",
            "publish.create_draft_job", "publish.precheck",
            "publish.job.get", "publish.job.list",
        ),
        budget={"max_steps": 5, "max_tokens": 12000, "max_tool_calls": 6, "max_seconds": 180},
    ),
}
```

`models/agent.py`：`AgentRun` 末尾追加三列；文件追加两个模型（`Boolean` 加入 sqlalchemy import）：

```python
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(16), default="")
    team_state: Mapped[dict] = mapped_column(JSONType, default=dict)


class AgentTeamEntry(TenantScoped):
    __tablename__ = "agent_team_entries"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_agent_team_entries_run_seq"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        index=True,
    )
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    role: Mapped[str] = mapped_column(String(16), default="")
    content: Mapped[dict] = mapped_column(JSONType, default=dict)


class AgentTeamRole(TenantScoped):
    __tablename__ = "agent_team_roles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "role", name="uq_agent_team_roles_tenant_role"),
    )

    role: Mapped[str] = mapped_column(String(16))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    tools: Mapped[dict] = mapped_column(JSONType, default=list)
    budget: Mapped[dict] = mapped_column(JSONType, default=dict)
```

迁移 `f6a7b8c9d0e1_add_agent_team.py`（风格对齐 `e5f6a7b8c9d0`）：

```python
"""add agent team

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-11-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    op.add_column(
        "agent_runs", sa.Column("parent_run_id", sa.Uuid(as_uuid=True), nullable=True)
    )
    op.add_column(
        "agent_runs",
        sa.Column("role", sa.String(length=16), nullable=False, server_default=""),
    )
    op.add_column(
        "agent_runs",
        sa.Column("team_state", JSON, nullable=False, server_default="{}"),
    )
    op.create_index("ix_agent_runs_parent_run_id", "agent_runs", ["parent_run_id"])
    op.create_foreign_key(
        "fk_agent_runs_parent_run_id", "agent_runs", "agent_runs",
        ["parent_run_id"], ["id"], ondelete="CASCADE",
    )

    op.create_table(
        "agent_team_entries",
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("content", JSON, nullable=False),
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "seq", name="uq_agent_team_entries_run_seq"),
    )
    op.create_index("ix_agent_team_entries_tenant_id", "agent_team_entries", ["tenant_id"])
    op.create_index("ix_agent_team_entries_run_id", "agent_team_entries", ["run_id"])

    op.create_table(
        "agent_team_roles",
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("tools", JSON, nullable=False),
        sa.Column("budget", JSON, nullable=False),
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "role", name="uq_agent_team_roles_tenant_role"),
    )
    op.create_index("ix_agent_team_roles_tenant_id", "agent_team_roles", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_team_roles_tenant_id", table_name="agent_team_roles")
    op.drop_table("agent_team_roles")
    op.drop_index("ix_agent_team_entries_run_id", table_name="agent_team_entries")
    op.drop_index("ix_agent_team_entries_tenant_id", table_name="agent_team_entries")
    op.drop_table("agent_team_entries")
    op.drop_constraint("fk_agent_runs_parent_run_id", "agent_runs", type_="foreignkey")
    op.drop_index("ix_agent_runs_parent_run_id", table_name="agent_runs")
    op.drop_column("agent_runs", "team_state")
    op.drop_column("agent_runs", "role")
    op.drop_column("agent_runs", "parent_run_id")
```

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run pytest tests/test_team_models.py -q && uv run alembic heads`
Expected: 3 passed；heads 输出单一 `f6a7b8c9d0e1 (head)`。

- [ ] **Step 5: 提交**

```bash
git add backend/app/config.py backend/.env.example backend/app/agent/roles.py backend/app/models/agent.py backend/migrations/versions/f6a7b8c9d0e1_add_agent_team.py backend/tests/test_team_models.py
git commit -m "feat(team): add role constants, team models and migration"
```

---

### Task 2: 黑板模块

**Files:**
- Create: `backend/app/agent/blackboard.py`
- Test: `backend/tests/test_team_blackboard.py`

**Interfaces:**
- Consumes: `AgentTeamEntry`（Task 1）、`app.agent.sanitize.scrub/fit_bytes`。
- Produces: `append_entry(session, *, run, kind, role="", content) -> AgentTeamEntry`、`recent_entries(session, run_id, limit) -> list[AgentTeamEntry]`、`render_entries(entries) -> list[dict]`、`MAX_ENTRY_BYTES = 4096`（Task 5/6/10 消费）。

- [ ] **Step 1: 写失败测试** `backend/tests/test_team_blackboard.py`

```python
import json
import uuid

import pytest

from app.agent.blackboard import (
    MAX_ENTRY_BYTES, append_entry, recent_entries, render_entries,
)
from app.models.agent import AgentRun
from app.models.base import DEFAULT_TENANT_ID


@pytest.fixture
async def parent(db_session):
    run = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=uuid.uuid4(), goal="目标", mode="team"
    )
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)
    return run


@pytest.mark.asyncio
async def test_seq_increments_and_kind(db_session, parent):
    a = await append_entry(db_session, run=parent, kind="goal", content={"goal": "g"})
    b = await append_entry(db_session, run=parent, kind="delegation", role="scout",
                           content={"task": "t"})
    await db_session.commit()
    assert (a.seq, b.seq) == (1, 2)
    assert a.role == "" and b.role == "scout"


@pytest.mark.asyncio
async def test_scrub_and_truncate(db_session, parent):
    entry = await append_entry(
        db_session, run=parent, kind="result",
        content={"api_key": "sk-secret", "text": "x" * 20000},
    )
    await db_session.commit()
    assert "api_key" not in entry.content
    assert len(json.dumps(entry.content, ensure_ascii=False).encode()) <= MAX_ENTRY_BYTES


@pytest.mark.asyncio
async def test_recent_returns_tail_ascending(db_session, parent):
    for i in range(1, 6):
        await append_entry(db_session, run=parent, kind="result", content={"i": i})
    await db_session.commit()
    recent = await recent_entries(db_session, parent.id, 2)
    assert [e.seq for e in recent] == [4, 5]
    rendered = render_entries(recent)
    assert rendered[0]["seq"] == 4 and rendered[0]["content"] == {"i": 4}
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_team_blackboard.py -q`
Expected: FAIL（模块不存在）。

- [ ] **Step 3: 实现** `backend/app/agent/blackboard.py`

```python
"""团队黑板：只追加、服务端分配 seq、入库前脱敏与截断。"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.sanitize import fit_bytes, scrub
from app.models.agent import AgentRun, AgentTeamEntry

MAX_ENTRY_BYTES = 4096


async def append_entry(
    session: AsyncSession,
    *,
    run: AgentRun,
    kind: str,
    role: str = "",
    content: dict,
) -> AgentTeamEntry:
    last = (
        await session.execute(
            select(func.max(AgentTeamEntry.seq)).where(
                AgentTeamEntry.run_id == run.id
            )
        )
    ).scalar()
    cleaned, _ = fit_bytes(scrub(content), MAX_ENTRY_BYTES)
    entry = AgentTeamEntry(
        tenant_id=run.tenant_id,
        run_id=run.id,
        seq=int(last or 0) + 1,
        kind=kind,
        role=role,
        content=cleaned if isinstance(cleaned, dict) else {"text": cleaned},
    )
    session.add(entry)
    await session.flush()
    return entry


async def recent_entries(
    session: AsyncSession, run_id, limit: int
) -> list[AgentTeamEntry]:
    rows = (
        await session.execute(
            select(AgentTeamEntry)
            .where(AgentTeamEntry.run_id == run_id)
            .order_by(AgentTeamEntry.seq.desc())
            .limit(max(1, limit))
        )
    ).scalars().all()
    return list(reversed(rows))


def render_entries(entries: list[AgentTeamEntry]) -> list[dict]:
    return [
        {"seq": e.seq, "kind": e.kind, "role": e.role, "content": e.content}
        for e in entries
    ]
```

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run pytest tests/test_team_blackboard.py -q`
Expected: 3 passed。

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/blackboard.py backend/tests/test_team_blackboard.py
git commit -m "feat(team): add append-only blackboard with scrub and truncation"
```

---

### Task 3: 角色生效配置、收窄校验与团队预算

**Files:**
- Modify: `backend/app/agent/roles.py`
- Test: `backend/tests/test_team_roles.py`

**Interfaces:**
- Consumes: `ROLES`、`AgentTeamRole`、`Settings`、`AppError`。
- Produces（后续任务按此签名调用）:
  - `EffectiveRole(key, label, enabled, tools: tuple[str,...], budget: dict, required)` dataclass。
  - `async effective_roles(session, tenant_id) -> dict[str, EffectiveRole]`。
  - `validate_participants(raw: list, effective: dict) -> tuple[str, ...]`（非法抛 `AppError("team_roles_invalid", ..., 422)`）。
  - `validate_role_patch(role, *, enabled=None, tools=None, budget=None) -> None`（放宽抛 `AppError("policy_not_loosenable", ..., 422)`；未知角色抛 `team_roles_invalid`）。
  - `team_budget_defaults(settings) -> dict`、`team_budget_ceilings(settings) -> dict`、`clamp_team_budget(raw, settings) -> dict`（非法抛 `AppError("budget_invalid", ..., 422)`）。

- [ ] **Step 1: 写失败测试** `backend/tests/test_team_roles.py`

```python
import pytest

from app.agent.roles import (
    ROLES, clamp_team_budget, effective_roles, team_budget_defaults,
    validate_participants, validate_role_patch,
)
from app.config import get_settings
from app.errors import AppError
from app.models.agent import AgentTeamRole
from app.models.base import DEFAULT_TENANT_ID


@pytest.mark.asyncio
async def test_defaults_without_rows(db_session):
    roles = await effective_roles(db_session, DEFAULT_TENANT_ID)
    assert roles["scout"].enabled is True
    assert roles["scout"].tools == ROLES["scout"].tools
    assert roles["planner"].enabled and roles["creator"].enabled


@pytest.mark.asyncio
async def test_tenant_narrowing(db_session):
    db_session.add(AgentTeamRole(
        tenant_id=DEFAULT_TENANT_ID, role="scout", enabled=False,
        tools=["profile.get"], budget={"max_tokens": 1000},
    ))
    db_session.add(AgentTeamRole(
        tenant_id=DEFAULT_TENANT_ID, role="creator", enabled=False,
    ))
    await db_session.commit()
    roles = await effective_roles(db_session, DEFAULT_TENANT_ID)
    assert roles["scout"].enabled is False
    assert roles["scout"].tools == ("profile.get",)
    assert roles["scout"].budget["max_tokens"] == 1000
    assert roles["creator"].enabled is True  # 必选角色强制启用


def test_validate_participants():
    settings = get_settings()
    import asyncio
    # 用纯函数路径：构造默认 effective 需要 DB，这里直接测校验规则
    from app.agent.roles import EXECUTION_ROLES

    class Fake:
        enabled = True

    effective = {key: Fake() for key in EXECUTION_ROLES}
    assert validate_participants(["creator", "scout"], effective) == ("scout", "creator")
    with pytest.raises(AppError) as exc:
        validate_participants(["scout"], effective)
    assert exc.value.code == "team_roles_invalid"
    with pytest.raises(AppError):
        validate_participants(["creator", "planner"], effective)
    with pytest.raises(AppError):
        validate_participants([], effective)
    effective["scout"].enabled = False
    with pytest.raises(AppError):
        validate_participants(["scout", "creator"], effective)


def test_validate_role_patch():
    validate_role_patch("scout", enabled=False, tools=["profile.get"],
                        budget={"max_tokens": 1000})
    with pytest.raises(AppError) as exc:
        validate_role_patch("creator", enabled=False)
    assert exc.value.code == "policy_not_loosenable"
    with pytest.raises(AppError):
        validate_role_patch("planner", enabled=False)
    with pytest.raises(AppError):
        validate_role_patch("scout", tools=["publish.execute"])
    with pytest.raises(AppError):
        validate_role_patch("scout", budget={"max_tokens": 999999})
    with pytest.raises(AppError) as exc:
        validate_role_patch("ghost")
    assert exc.value.code == "team_roles_invalid"


def test_clamp_team_budget():
    settings = get_settings()
    defaults = team_budget_defaults(settings)
    assert defaults == {
        "max_tokens": 120000, "max_tool_calls": 40,
        "max_turns": 12, "max_seconds": 900,
    }
    assert clamp_team_budget({}, settings) == defaults
    assert clamp_team_budget({"max_tokens": 200000}, settings)["max_tokens"] == 200000
    with pytest.raises(AppError) as exc:
        clamp_team_budget({"max_tokens": 200001}, settings)
    assert exc.value.code == "budget_invalid"
    with pytest.raises(AppError):
        clamp_team_budget({"max_tokens": 0}, settings)
    with pytest.raises(AppError):
        clamp_team_budget({"max_steps": 5}, settings)
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_team_roles.py -q`
Expected: FAIL（函数不存在）。

- [ ] **Step 3: 实现**（追加到 `roles.py`）

```python
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.errors import AppError
from app.models.agent import AgentTeamRole


@dataclass(frozen=True)
class EffectiveRole:
    key: str
    label: str
    enabled: bool
    tools: tuple[str, ...]
    budget: dict[str, int]
    required: bool


async def effective_roles(session: AsyncSession, tenant_id) -> dict[str, EffectiveRole]:
    rows = (
        await session.execute(
            select(AgentTeamRole).where(AgentTeamRole.tenant_id == tenant_id)
        )
    ).scalars().all()
    overrides = {row.role: row for row in rows}
    result: dict[str, EffectiveRole] = {}
    for key in ROLE_KEYS:
        spec = ROLES[key]
        row = overrides.get(key)
        enabled = True if spec.required else (row.enabled if row is not None else True)
        tools = tuple(row.tools) if row is not None and row.tools else spec.tools
        tools = tuple(t for t in tools if t in spec.tools) or spec.tools
        budget = dict(spec.budget)
        if row is not None and isinstance(row.budget, dict):
            for dim, value in row.budget.items():
                if (
                    dim in budget
                    and isinstance(value, int)
                    and not isinstance(value, bool)
                    and 0 < value <= spec.budget[dim]
                ):
                    budget[dim] = value
        result[key] = EffectiveRole(
            key=key, label=spec.label, enabled=enabled,
            tools=tools, budget=budget, required=spec.required,
        )
    return result


def validate_participants(raw, effective: dict[str, EffectiveRole]) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise AppError("team_roles_invalid", "至少选择一个执行角色", 422)
    requested: list[str] = []
    for item in raw:
        if not isinstance(item, str) or item not in EXECUTION_ROLES:
            raise AppError("team_roles_invalid", f"未知执行角色：{item}", 422)
        if not effective[item].enabled:
            raise AppError("team_roles_invalid", f"角色 {item} 已被租户关闭", 422)
        requested.append(item)
    if "creator" not in requested:
        raise AppError("team_roles_invalid", "团队必须包含 creator", 422)
    return tuple(role for role in EXECUTION_ROLES if role in requested)


def validate_role_patch(role, *, enabled=None, tools=None, budget=None) -> None:
    if role not in ROLE_KEYS:
        raise AppError("team_roles_invalid", f"未知角色：{role}", 422)
    spec = ROLES[role]
    if enabled is False and spec.required:
        raise AppError("policy_not_loosenable", f"{role} 不可关闭", 422)
    if tools is not None:
        if not isinstance(tools, list) or not tools:
            raise AppError("policy_not_loosenable", "工具列表必须是非空数组", 422)
        for tool in tools:
            if tool not in spec.tools:
                raise AppError("policy_not_loosenable", f"不能新增工具：{tool}", 422)
    if budget is not None:
        if not isinstance(budget, dict):
            raise AppError("policy_not_loosenable", "预算必须是对象", 422)
        for dim, value in budget.items():
            if (
                dim not in spec.budget
                or not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
                or value > spec.budget[dim]
            ):
                raise AppError("policy_not_loosenable", f"预算 {dim} 只能调低", 422)


def team_budget_defaults(settings: Settings) -> dict[str, int]:
    return {
        "max_tokens": settings.agent_team_max_tokens,
        "max_tool_calls": settings.agent_team_max_tool_calls,
        "max_turns": settings.agent_team_max_turns,
        "max_seconds": settings.agent_team_max_seconds,
    }


def team_budget_ceilings(settings: Settings) -> dict[str, int]:
    return {
        "max_tokens": settings.agent_team_token_ceiling,
        "max_tool_calls": 60,
        "max_turns": 20,
        "max_seconds": settings.agent_team_seconds_ceiling,
    }


def clamp_team_budget(raw, settings: Settings) -> dict[str, int]:
    cleaned = dict(team_budget_defaults(settings))
    ceilings = team_budget_ceilings(settings)
    if raw is None:
        return cleaned
    if not isinstance(raw, dict):
        raise AppError("budget_invalid", "预算必须是对象", 422)
    for dim, value in raw.items():
        if dim not in TEAM_BUDGET_KEYS:
            raise AppError("budget_invalid", f"未知预算维度：{dim}", 422)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise AppError("budget_invalid", f"预算 {dim} 必须是正整数", 422)
        if value > ceilings[dim]:
            raise AppError("budget_invalid", f"预算 {dim} 超过上限", 422)
        cleaned[dim] = value
    return cleaned
```

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run pytest tests/test_team_roles.py tests/test_team_models.py -q`
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/roles.py backend/tests/test_team_roles.py
git commit -m "feat(team): resolve tenant role config with narrow-only validation"
```

---

### Task 4: Lead / Critic 决策契约

**Files:**
- Create: `backend/app/agent/team_contracts.py`
- Test: `backend/tests/test_team_orchestrator.py`（本任务先建文件，只放契约测试；Task 5/6 追加编排测试）

**Interfaces:**
- Produces:
  - `LeadDecision(action, role="", task="", summary="", reason="")` frozen dataclass。
  - `parse_lead_decision(text: str, *, allowed_roles: tuple[str, ...]) -> LeadDecision`（非法抛 `ValueError`）。
  - `CriticVerdict(verdict: str, issues: list[str])` frozen dataclass。
  - `parse_critic_verdict(text: str) -> CriticVerdict`（非法抛 `ValueError`）。
  - `lead_messages(*, goal, participants, entries, state, budget) -> list[dict]`。
  - `critic_task(*, goal, summary, artifacts, note="") -> str`。

- [ ] **Step 1: 写失败测试** `backend/tests/test_team_orchestrator.py`

```python
import pytest

from app.agent.team_contracts import (
    critic_task, lead_messages, parse_critic_verdict, parse_lead_decision,
)


def test_parse_delegate():
    decision = parse_lead_decision(
        '{"action":"delegate","role":"scout","task":"查数据","reason":"先调研"}',
        allowed_roles=("scout", "creator"),
    )
    assert decision.action == "delegate" and decision.role == "scout"
    assert decision.task == "查数据"


def test_parse_wrapped_json():
    decision = parse_lead_decision(
        '```json\n{"action":"finalize","summary":"完成"}\n```',
        allowed_roles=("creator",),
    )
    assert decision.action == "finalize" and decision.summary == "完成"


def test_parse_rework_only_creator():
    with pytest.raises(ValueError):
        parse_lead_decision(
            '{"action":"rework","role":"scout","task":"改"}',
            allowed_roles=("scout", "creator"),
        )
    decision = parse_lead_decision(
        '{"action":"rework","role":"creator","task":"改标题"}',
        allowed_roles=("creator",),
    )
    assert decision.action == "rework"


@pytest.mark.parametrize("text", [
    "不是 JSON",
    '{"action":"unknown"}',
    '{"action":"delegate","role":"ghost","task":"t"}',
    '{"action":"delegate","role":"scout","task":""}',
    '{"action":"finalize","summary":""}',
    '{"action":"delegate","role":"scout","task":"' + "x" * 2001 + '"}',
])
def test_parse_invalid(text):
    with pytest.raises(ValueError):
        parse_lead_decision(text, allowed_roles=("scout", "creator"))


def test_parse_critic():
    verdict = parse_critic_verdict('{"verdict":"fail","issues":["标题太长"]}')
    assert verdict.verdict == "fail" and verdict.issues == ["标题太长"]
    assert parse_critic_verdict('{"verdict":"pass","issues":[]}').verdict == "pass"
    with pytest.raises(ValueError):
        parse_critic_verdict('{"verdict":"maybe"}')
    with pytest.raises(ValueError):
        parse_critic_verdict("随便写")


def test_prompt_builders():
    messages = lead_messages(
        goal="做一篇内容", participants=("scout", "creator"),
        entries=[{"seq": 1, "kind": "goal", "role": "", "content": {"goal": "做一篇内容"}}],
        state={"turn": 1, "rework": 0},
        budget={"max_turns": 12},
    )
    assert messages[0]["role"] == "system"
    assert "做一篇内容" in messages[1]["content"]
    assert "scout" in messages[1]["content"]
    task = critic_task(goal="做一篇内容", summary="已写稿", artifacts=[], note="")
    assert "verdict" in task and "做一篇内容" in task
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_team_orchestrator.py -q`
Expected: FAIL（模块不存在）。

- [ ] **Step 3: 实现** `backend/app/agent/team_contracts.py`

```python
"""Lead 决策与 Critic 裁决的结构化契约：解析、校验与提示词构造。"""

import json
from dataclasses import dataclass

from app.agent.roles import ROLES

MAX_TEXT = 2000


@dataclass(frozen=True)
class LeadDecision:
    action: str
    role: str = ""
    task: str = ""
    summary: str = ""
    reason: str = ""


@dataclass(frozen=True)
class CriticVerdict:
    verdict: str
    issues: list[str]


def _json_object(text: str) -> dict:
    raw = str(text).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        raise ValueError("输出不是 JSON 对象")
    data = json.loads(raw[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("输出不是 JSON 对象")
    return data


def _text(data: dict, key: str, *, required: bool) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是字符串")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{key} 不能为空")
    if len(value) > MAX_TEXT:
        raise ValueError(f"{key} 超过 {MAX_TEXT} 字符")
    return value


def parse_lead_decision(text: str, *, allowed_roles: tuple[str, ...]) -> LeadDecision:
    data = _json_object(text)
    action = data.get("action")
    if action not in {"delegate", "rework", "finalize"}:
        raise ValueError("action 必须是 delegate/rework/finalize")
    reason = _text(data, "reason", required=False)
    if action == "finalize":
        return LeadDecision(action=action, summary=_text(data, "summary", required=True), reason=reason)
    role = data.get("role")
    if not isinstance(role, str):
        raise ValueError("role 必须是字符串")
    if action == "rework" and role != "creator":
        raise ValueError("rework 只能派给 creator")
    if role not in allowed_roles:
        raise ValueError(f"角色 {role} 不在本次参与角色中")
    return LeadDecision(
        action=action, role=role,
        task=_text(data, "task", required=True), reason=reason,
    )


def parse_critic_verdict(text: str) -> CriticVerdict:
    data = _json_object(text)
    verdict = data.get("verdict")
    if verdict not in {"pass", "fail"}:
        raise ValueError("verdict 必须是 pass/fail")
    raw_issues = data.get("issues") or []
    if not isinstance(raw_issues, list):
        raise ValueError("issues 必须是数组")
    issues = [str(item)[:500] for item in raw_issues[:20] if str(item).strip()]
    return CriticVerdict(verdict=verdict, issues=issues)


def lead_messages(*, goal, participants, entries, state, budget) -> list[dict]:
    board = json.dumps(entries, ensure_ascii=False)
    user = (
        f"团队目标：{goal}\n"
        f"可委派角色：{', '.join(participants)}（rework 只能派 creator）\n"
        f"当前轮次：{state.get('turn', 0)}/{budget.get('max_turns')}，"
        f"已返工：{state.get('rework', 0)}\n"
        f"黑板（最近条目）：{board}\n"
        "请输出一个 JSON 决策对象：\n"
        '{"action":"delegate","role":"<角色>","task":"<任务>","reason":"<原因>"}\n'
        '{"action":"rework","role":"creator","task":"<修改要求>","reason":"<原因>"}\n'
        '{"action":"finalize","summary":"<交付摘要>","reason":"<原因>"}'
    )
    return [
        {"role": "system", "content": ROLES["planner"].system_prompt},
        {"role": "user", "content": user},
    ]


def critic_task(*, goal, summary, artifacts, note="") -> str:
    parts = [
        f"评审团队最近一次创作交付。团队目标：{goal}",
        f"创作者摘要：{summary[:1000]}",
    ]
    if artifacts:
        parts.append("产物：" + json.dumps(artifacts, ensure_ascii=False)[:1000])
    if note:
        parts.append(f"注意：{note}")
    parts.append('只输出 JSON：{"verdict":"pass|fail","issues":["问题1","问题2"]}')
    return "\n".join(parts)[:4000]
```

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run pytest tests/test_team_orchestrator.py -q`
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/team_contracts.py backend/tests/test_team_orchestrator.py
git commit -m "feat(team): add lead decision and critic verdict contracts"
```

---

### Task 5: 编排器核心（委派、预算、finalize、恢复）

**Files:**
- Create: `backend/app/agent/team.py`
- Test: `backend/tests/test_team_orchestrator.py`（追加）

**Interfaces:**
- Consumes: Task 1–4 全部产物；`run_agent`、`_heartbeat_loop`（`app.agent.runtime`）；`ensure_daily_quota`（`app.agent.quota`）；`ToolRegistry.subset`。
- Produces:
  - `async run_team(session, run, *, gateway, tool_registry, skill_registry, storage, queue, settings, session_factory=None) -> None`（worker/引擎调用形状与 `run_agent` 一致）。
  - `team_usage(session, parent) -> dict`（汇总父+子 usage，Task 9/10 复用）。
  - 错误码常量：`TEAM_BUDGET_EXHAUSTED="team_budget_exhausted"`、`TEAM_LEAD_INVALID="team_lead_invalid"`、`CRITIC_REJECTED="critic_rejected"`、`CRITIC_INVALID="critic_invalid"`、`TEAM_LOOP_DETECTED="team_loop_detected"`、`ROLE_FAILED="role_failed"`。
  - `team_state` 键：`turn, phase("lead"|"role"|"critic"), current_child_id, current_role, creator_task, rework, roles_run, artifact_index, critic_verdicts, critic, loop_guard, forced_role, forced_task, critic_invalid, critic_note, last_seq`。
  - 参与角色存于父运行 `run.plan["roles"]`。

- [ ] **Step 1: 写失败测试**（追加到 `test_team_orchestrator.py`）

```python
import json as _json
import uuid as _uuid
from dataclasses import dataclass

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.team import run_team
from app.config import get_settings
from app.llm.tools import ToolCall, ToolTurn
from app.llm.types import Usage
from app.models.agent import AgentRun, AgentTeamEntry
from app.models.base import DEFAULT_TENANT_ID
from app.skills.registry import SkillRegistry
from app.storage import get_storage


class ScriptedGateway:
    """Lead 用 complete()，角色子运行用 complete_tool_calls()；按脚本顺序消费。"""

    def __init__(self, script):
        self.script = list(script)
        self.last_usage = Usage(10, 5)
        self.complete_calls = []

    async def complete(self, messages, **kwargs):
        self.complete_calls.append(messages)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def complete_tool_calls(self, messages, *, tools):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def role_turn(content):
    return ToolTurn(content=content, tool_calls=[], finish_reason="stop",
                    usage=Usage(20, 10))


def lead(action, **kw):
    return _json.dumps({"action": action, **kw}, ensure_ascii=False)


@pytest_asyncio.fixture
async def team_env(db_session):
    settings = get_settings()

    class Env:
        pass

    holder = Env()
    holder.factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    holder.settings = settings
    holder.session = db_session
    return holder


async def make_parent(env, *, roles=("scout", "creator"), budget=None):
    run = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=_uuid.uuid4(),
        goal="围绕主题产出一篇内容", mode="team", status="queued",
        plan={"roles": list(roles)},
        budget=budget or {
            "max_tokens": 120000, "max_tool_calls": 40,
            "max_turns": 12, "max_seconds": 900,
        },
    )
    env.session.add(run)
    await env.session.commit()
    await env.session.refresh(run)
    return run


def deps(env, gateway):
    from app.agent.registry import get_tool_registry
    return dict(
        gateway=gateway, tool_registry=get_tool_registry(),
        skill_registry=SkillRegistry(env.settings.skills_dir),
        storage=get_storage(), queue=None, settings=env.settings,
        session_factory=env.factory,
    )


@pytest.mark.asyncio
async def test_delegate_then_finalize(team_env):
    parent = await make_parent(team_env)
    gateway = ScriptedGateway([
        lead("delegate", role="scout", task="调研选题"),
        role_turn("调研结论：主题可行"),
        lead("finalize", summary="已交付"),
    ])
    await run_team(team_env.session, parent, **deps(team_env, gateway))
    await team_env.session.refresh(parent)
    assert parent.status == "succeeded"
    assert parent.result["summary"] == "已交付"
    children = (await team_env.session.execute(
        select(AgentRun).where(AgentRun.parent_run_id == parent.id)
    )).scalars().all()
    assert [(c.role, c.status) for c in children] == [("scout", "succeeded")]
    entries = (await team_env.session.execute(
        select(AgentTeamEntry).where(AgentTeamEntry.run_id == parent.id)
        .order_by(AgentTeamEntry.seq)
    )).scalars().all()
    kinds = [e.kind for e in entries]
    assert kinds[0] == "goal" and "delegation" in kinds and "result" in kinds
    assert kinds[-1] == "final"
    # 父 usage 只记 Lead 用量，不并入子运行
    assert parent.usage["prompt_tokens"] == 20  # 两次 Lead 调用 × 10


@pytest.mark.asyncio
async def test_turn_budget_exhausted(team_env):
    parent = await make_parent(team_env, budget={
        "max_tokens": 120000, "max_tool_calls": 40,
        "max_turns": 1, "max_seconds": 900,
    })
    gateway = ScriptedGateway([
        lead("delegate", role="scout", task="调研"),
        role_turn("完成"),
    ])
    await run_team(team_env.session, parent, **deps(team_env, gateway))
    await team_env.session.refresh(parent)
    assert parent.status == "failed"
    assert parent.error_code == "team_budget_exhausted"


@pytest.mark.asyncio
async def test_lead_invalid_twice_fails(team_env):
    parent = await make_parent(team_env)
    gateway = ScriptedGateway(["不是 JSON", "还不是 JSON"])
    await run_team(team_env.session, parent, **deps(team_env, gateway))
    await team_env.session.refresh(parent)
    assert parent.status == "failed"
    assert parent.error_code == "team_lead_invalid"


@pytest.mark.asyncio
async def test_resume_settled_child_no_redelegate(team_env):
    parent = await make_parent(team_env)
    child = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=parent.user_id, goal="调研",
        mode="role", role="scout", parent_run_id=parent.id,
        status="succeeded", result_text="调研完成",
        result={"artifacts": []},
    )
    team_env.session.add(child)
    await team_env.session.commit()
    await team_env.session.refresh(child)
    parent.team_state = {
        "turn": 1, "phase": "role",
        "current_child_id": str(child.id), "current_role": "scout",
    }
    parent.status = "queued"
    await team_env.session.commit()
    gateway = ScriptedGateway([lead("finalize", summary="交付")])
    await run_team(team_env.session, parent, **deps(team_env, gateway))
    await team_env.session.refresh(parent)
    assert parent.status == "succeeded"
    children = (await team_env.session.execute(
        select(AgentRun).where(AgentRun.parent_run_id == parent.id)
    )).scalars().all()
    assert len(children) == 1  # 没有重复委派
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_team_orchestrator.py -q`
Expected: 新测试 FAIL（`app.agent.team` 不存在），契约测试仍通过。

- [ ] **Step 3: 实现** `backend/app/agent/team.py`（本任务核心骨架；Critic/返工/循环在 Task 6 补全，失败收敛在 Task 7 补全）

```python
"""团队编排器：Lead 结构化决策驱动，串行调用既有 run_agent。"""

import asyncio
import contextlib
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.blackboard import append_entry, recent_entries, render_entries
from app.agent.quota import ensure_daily_quota
from app.agent.roles import (
    EXECUTION_ROLES, EffectiveRole, clamp_team_budget, effective_roles,
)
from app.agent.runtime import INTERNAL_ERROR, INTERNAL_MESSAGE, _heartbeat_loop, run_agent
from app.agent.team_contracts import lead_messages, parse_lead_decision
from app.config import Settings
from app.errors import AppError
from app.models.agent import AgentRun, AgentTeamEntry
from app.services.audit import record_best_effort

logger = logging.getLogger(__name__)

TEAM_BUDGET_EXHAUSTED = "team_budget_exhausted"
TEAM_LEAD_INVALID = "team_lead_invalid"
CRITIC_REJECTED = "critic_rejected"
CRITIC_INVALID = "critic_invalid"
TEAM_LOOP_DETECTED = "team_loop_detected"
ROLE_FAILED = "role_failed"
TERMINAL = ("succeeded", "failed", "cancelled")
ACTIVE = ("queued", "running", "waiting_approval")


class _TeamStop(Exception):
    """编排器已落终态， unwind 主循环。"""


def _now() -> datetime:
    return datetime.now(UTC)


def _load_state(run: AgentRun) -> dict:
    state = dict(run.team_state or {})
    state.setdefault("turn", 0)
    state.setdefault("phase", "lead")
    state.setdefault("rework", 0)
    state.setdefault("roles_run", [])
    state.setdefault("artifact_index", [])
    state.setdefault("critic_verdicts", [])
    state.setdefault("critic", {})
    state.setdefault("loop_guard", {})
    return state


def _save_state(run: AgentRun, state: dict) -> None:
    run.team_state = dict(state)


def _participants(run: AgentRun) -> tuple[str, ...]:
    raw = (run.plan or {}).get("roles") or []
    roles = tuple(r for r in EXECUTION_ROLES if r in raw)
    return roles or EXECUTION_ROLES


async def team_usage(session: AsyncSession, parent: AgentRun) -> dict:
    totals = {
        "prompt_tokens": 0, "completion_tokens": 0, "tool_calls": 0,
        "steps": 0, "elapsed_ms": 0, "paused_ms": 0, "children": 0,
    }
    for key in totals:
        totals[key] = int((parent.usage or {}).get(key) or 0)
    rows = (
        await session.execute(
            select(AgentRun.usage).where(AgentRun.parent_run_id == parent.id)
        )
    ).scalars().all()
    totals["children"] = len(rows)
    for usage in rows:
        if not isinstance(usage, dict):
            continue
        for key in ("prompt_tokens", "completion_tokens", "tool_calls", "steps"):
            totals[key] += int(usage.get(key) or 0)
    return totals


def _budget_of(run: AgentRun, settings: Settings) -> dict:
    return clamp_team_budget(run.budget or {}, settings) if False else {
        **clamp_team_budget({}, settings),
        **{
            key: value for key, value in (run.budget or {}).items()
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
        },
    }


async def _budget_stop(session, run, settings, state) -> str | None:
    budget = _budget_of(run, settings)
    usage = await team_usage(session, run)
    if int(state.get("turn", 0)) >= budget["max_turns"]:
        return "团队轮次已用尽"
    if usage["prompt_tokens"] + usage["completion_tokens"] >= budget["max_tokens"]:
        return "团队 token 预算已用尽"
    if usage["tool_calls"] >= budget["max_tool_calls"]:
        return "团队工具调用预算已用尽"
    started = run.started_at
    if started is not None:
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        elapsed = (_now() - started).total_seconds() - usage["paused_ms"] / 1000
        if elapsed >= budget["max_seconds"]:
            return "团队时间预算已用尽"
    return None


async def _fail_team(session, run, code: str, message: str) -> None:
    """Task 7 会在此追加子运行收敛；本任务先落父运行终态。"""
    moment = _now()
    await session.execute(
        update(AgentRun)
        .where(
            AgentRun.id == run.id,
            AgentRun.tenant_id == run.tenant_id,
            AgentRun.status.in_(ACTIVE),
        )
        .values(status="failed", error_code=code, error_message=message[:500],
                finished_at=moment)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    await record_best_effort(
        session, tenant_id=run.tenant_id, action="agent.team.fail",
        actor=None, resource_type="agent_run", resource_id=str(run.id),
        detail={"error_code": code},
    )


async def _park_parent(session, run) -> None:
    moment = _now()
    await session.execute(
        update(AgentRun)
        .where(AgentRun.id == run.id, AgentRun.status.in_(("queued", "running")))
        .values(status="waiting_approval", heartbeat_at=moment)
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def _lead_decide(session, run, *, gateway, settings, state, participants):
    entries = render_entries(
        await recent_entries(session, run.id, settings.agent_team_blackboard_inject)
    )
    messages = lead_messages(
        goal=run.goal, participants=participants, entries=entries,
        state=state, budget=_budget_of(run, settings),
    )
    for _attempt in range(2):
        text = await gateway.complete(messages, temperature=0.2, max_tokens=800)
        usage = dict(run.usage or {})
        last = getattr(gateway, "last_usage", None)
        usage["prompt_tokens"] = int(usage.get("prompt_tokens") or 0) + int(
            getattr(last, "prompt_tokens", 0) or 0)
        usage["completion_tokens"] = int(usage.get("completion_tokens") or 0) + int(
            getattr(last, "completion_tokens", 0) or 0)
        usage["steps"] = int(usage.get("steps") or 0) + 1
        run.usage = usage
        await session.commit()
        try:
            return parse_lead_decision(text, allowed_roles=participants)
        except ValueError as exc:
            messages = messages + [
                {"role": "assistant", "content": str(text)},
                {"role": "user", "content": f"输出无效：{exc}。请只输出一个 JSON 对象。"},
            ]
    await _fail_team(session, run, TEAM_LEAD_INVALID, "Lead 连续两次输出无效决策")
    raise _TeamStop()


async def _drive_child(session, parent, child, *, roles, gateway, tool_registry,
                       skill_registry, storage, queue, settings, session_factory):
    if child.status in {"running", "interrupted"}:
        await session.execute(
            update(AgentRun)
            .where(AgentRun.id == child.id,
                   AgentRun.status.in_(("running", "interrupted")))
            .values(status="queued", finished_at=None, error_code="", error_message="")
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        await session.refresh(child)
    if child.status == "queued":
        await run_agent(
            session, child,
            gateway=gateway,
            tool_registry=tool_registry.subset(roles[child.role].tools),
            skill_registry=skill_registry, storage=storage, queue=queue,
            settings=settings, session_factory=session_factory,
        )
        await session.refresh(child)


async def _start_role(session, run, *, role, task, reason, state, roles,
                      gateway, tool_registry, skill_registry, storage, queue,
                      settings, session_factory) -> None:
    child = AgentRun(
        tenant_id=run.tenant_id, user_id=run.user_id, goal=task[:4000],
        mode="role", role=role, parent_run_id=run.id, status="queued",
        budget=dict(roles[role].budget), policy=dict(run.policy or {}),
    )
    session.add(child)
    await session.commit()
    await session.refresh(child)
    state["current_child_id"] = str(child.id)
    state["current_role"] = role
    state["phase"] = "role"
    await append_entry(session, run=run, kind="delegation", role=role,
                       content={"task": task[:1000], "reason": reason[:1000]})
    _save_state(run, state)
    await session.commit()
    await _drive_child(session, run, child, roles=roles, gateway=gateway,
                       tool_registry=tool_registry, skill_registry=skill_registry,
                       storage=storage, queue=queue, settings=settings,
                       session_factory=session_factory)


async def _on_child_succeeded(session, run, child, state) -> None:
    result = child.result if isinstance(child.result, dict) else {}
    artifacts = [a for a in result.get("artifacts") or [] if isinstance(a, dict)]
    entry = await append_entry(
        session, run=run, kind="result", role=child.role,
        content={"summary": child.result_text[:1000], "artifacts": artifacts},
    )
    for item in artifacts:
        state["artifact_index"].append({
            "kind": str(item.get("type") or item.get("kind") or ""),
            "id": str(item.get("id") or ""),
            "title": str(item.get("title") or ""),
            "role": child.role, "seq": entry.seq,
        })
    state["roles_run"] = sorted(set(state["roles_run"]) | {child.role})
    state["current_child_id"] = None
    state["current_role"] = ""
    state["phase"] = "lead"
    _save_state(run, state)
    await session.commit()


async def _settle_child(session, run, state, *, roles, gateway, tool_registry,
                        skill_registry, storage, queue, settings, session_factory) -> str:
    child = await session.get(AgentRun, uuid.UUID(state["current_child_id"]))
    if child is None:
        await _fail_team(session, run, ROLE_FAILED, "子运行记录丢失")
        return "failed"
    if child.status in {"queued", "running", "interrupted"}:
        await _drive_child(session, run, child, roles=roles, gateway=gateway,
                           tool_registry=tool_registry, skill_registry=skill_registry,
                           storage=storage, queue=queue, settings=settings,
                           session_factory=session_factory)
    if child.status == "waiting_approval":
        _save_state(run, state)
        await _park_parent(session, run)
        return "waiting"
    if child.status in {"failed", "cancelled"}:
        await _fail_team(session, run, child.error_code or ROLE_FAILED,
                         child.error_message or "角色子运行失败")
        return "failed"
    if child.status != "succeeded":
        await _fail_team(session, run, INTERNAL_ERROR, INTERNAL_MESSAGE)
        return "failed"
    await _on_child_succeeded(session, run, child, state)
    return "ok"


async def _finish_team(session, run, state, summary: str) -> None:
    moment = _now()
    usage = dict(run.usage or {})
    started = run.started_at
    if started is not None:
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        usage["elapsed_ms"] = int(max(0.0, (moment - started).total_seconds()) * 1000)
    result = {
        "summary": summary,
        "roles": state.get("roles_run") or [],
        "artifacts": state.get("artifact_index") or [],
        "critic": state.get("critic") or {
            "verdict": "", "rework_rounds": state.get("rework", 0), "issues": [],
        },
        "team": {"turns": state.get("turn", 0),
                 "children": len(state.get("roles_run") or [])},
    }
    children = (await session.execute(
        select(func.count()).select_from(AgentRun)
        .where(AgentRun.parent_run_id == run.id)
    )).scalar_one()
    result["team"]["children"] = children
    await append_entry(session, run=run, kind="final",
                       content={"summary": summary, "artifacts": result["artifacts"]})
    await session.execute(
        update(AgentRun)
        .where(AgentRun.id == run.id, AgentRun.status.in_(("queued", "running")))
        .values(status="succeeded", result_text=summary[:4000], result=result,
                usage=usage, finished_at=moment, error_code="", error_message="")
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def _team_loop(session, run, *, gateway, tool_registry, skill_registry,
                     storage, queue, settings, session_factory) -> None:
    if run.status != "queued":
        return
    moment = _now()
    claimed = await session.execute(
        update(AgentRun)
        .where(AgentRun.id == run.id, AgentRun.status == "queued",
               AgentRun.mode == "team")
        .values(status="running", started_at=run.started_at or moment,
                heartbeat_at=moment)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if claimed.rowcount == 0:
        await session.refresh(run)
        return
    await session.refresh(run)
    try:
        await ensure_daily_quota(session, run.tenant_id, settings)
    except AppError as exc:
        await _fail_team(session, run, exc.code, exc.message)
        return
    roles = await effective_roles(session, run.tenant_id)
    participants = _participants(run)
    state = _load_state(run)
    has_goal = (await session.execute(
        select(AgentTeamEntry.id).where(
            AgentTeamEntry.run_id == run.id, AgentTeamEntry.kind == "goal"
        ).limit(1)
    )).first()
    if has_goal is None:
        await append_entry(session, run=run, kind="goal",
                           content={"goal": run.goal[:2000]})
        _save_state(run, state)
        await session.commit()

    while True:
        await session.refresh(run)
        if run.status == "cancelled":
            return
        state = _load_state(run)
        stop = await _budget_stop(session, run, settings, state)
        if stop is not None:
            await _fail_team(session, run, TEAM_BUDGET_EXHAUSTED, stop)
            return
        if state.get("current_child_id"):
            outcome = await _settle_child(
                session, run, state, roles=roles, gateway=gateway,
                tool_registry=tool_registry, skill_registry=skill_registry,
                storage=storage, queue=queue, settings=settings,
                session_factory=session_factory,
            )
            if outcome in {"waiting", "failed"}:
                return
            continue
        decision = await _lead_decide(
            session, run, gateway=gateway, settings=settings,
            state=state, participants=participants,
        )
        state["turn"] = int(state.get("turn", 0)) + 1
        _save_state(run, state)
        await session.commit()
        if decision.action == "finalize":
            await _finish_team(session, run, state, decision.summary)
            return
        await _start_role(
            session, run, role=decision.role, task=decision.task,
            reason=decision.reason, state=state, roles=roles, gateway=gateway,
            tool_registry=tool_registry, skill_registry=skill_registry,
            storage=storage, queue=queue, settings=settings,
            session_factory=session_factory,
        )


async def run_team(
    session: AsyncSession,
    run: AgentRun,
    *,
    gateway,
    tool_registry,
    skill_registry,
    storage,
    queue,
    settings: Settings,
    session_factory=None,
) -> None:
    heartbeat = (
        asyncio.create_task(_heartbeat_loop(session_factory, run.id))
        if session_factory is not None
        else None
    )
    try:
        try:
            await _team_loop(
                session, run, gateway=gateway, tool_registry=tool_registry,
                skill_registry=skill_registry, storage=storage, queue=queue,
                settings=settings, session_factory=session_factory,
            )
        except _TeamStop:
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("team run crashed run=%s", run.id)
            await _fail_team(session, run, INTERNAL_ERROR, INTERNAL_MESSAGE)
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
```

注意：`_budget_of` 中 `if False else` 是占位写法，实现时直接写成：

```python
def _budget_of(run: AgentRun, settings: Settings) -> dict:
    budget = clamp_team_budget({}, settings)
    for key, value in (run.budget or {}).items():
        if (
            key in budget and isinstance(value, int)
            and not isinstance(value, bool) and value > 0
        ):
            budget[key] = value
    return budget
```

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run pytest tests/test_team_orchestrator.py -q`
Expected: 全部通过（含 Task 4 契约测试）。

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/team.py backend/tests/test_team_orchestrator.py
git commit -m "feat(team): add orchestrator core with delegation, budgets and resume"
```

---

### Task 6: Critic 强制审稿、返工上限、循环检测

**Files:**
- Modify: `backend/app/agent/team.py`
- Test: `backend/tests/test_team_orchestrator.py`（追加）

**Interfaces:**
- Consumes: `parse_critic_verdict`、`critic_task`（Task 4）；`settings.agent_team_rework_limit`。
- Produces: `_on_child_succeeded` 的 critic/creator 分支、`_start_critic`、循环检测；终止码 `critic_rejected`/`critic_invalid`/`team_loop_detected` 生效。

- [ ] **Step 1: 写失败测试**（追加）

```python
@pytest.mark.asyncio
async def test_forced_critic_pass(team_env):
    parent = await make_parent(team_env, roles=("creator", "critic"))
    gateway = ScriptedGateway([
        lead("delegate", role="creator", task="写稿"),
        role_turn("稿件完成"),
        role_turn('{"verdict":"pass","issues":[]}'),
        lead("finalize", summary="交付"),
    ])
    await run_team(team_env.session, parent, **deps(team_env, gateway))
    await team_env.session.refresh(parent)
    assert parent.status == "succeeded"
    children = (await team_env.session.execute(
        select(AgentRun).where(AgentRun.parent_run_id == parent.id)
        .order_by(AgentRun.created_at)
    )).scalars().all()
    assert [c.role for c in children] == ["creator", "critic"]
    assert parent.result["critic"]["verdict"] == "pass"


@pytest.mark.asyncio
async def test_critic_fail_forces_rework_then_pass(team_env):
    parent = await make_parent(team_env, roles=("creator", "critic"))
    gateway = ScriptedGateway([
        lead("delegate", role="creator", task="写稿"),
        role_turn("稿件 v1"),
        role_turn('{"verdict":"fail","issues":["标题太长"]}'),
        role_turn("稿件 v2"),
        role_turn('{"verdict":"pass","issues":[]}'),
        lead("finalize", summary="交付"),
    ])
    await run_team(team_env.session, parent, **deps(team_env, gateway))
    await team_env.session.refresh(parent)
    assert parent.status == "succeeded"
    children = (await team_env.session.execute(
        select(AgentRun).where(AgentRun.parent_run_id == parent.id)
        .order_by(AgentRun.created_at)
    )).scalars().all()
    assert [c.role for c in children] == ["creator", "critic", "creator", "critic"]
    assert children[2].goal.find("标题太长") >= 0
    assert parent.result["critic"]["rework_rounds"] == 1


@pytest.mark.asyncio
async def test_critic_rejected_after_two_reworks(team_env):
    parent = await make_parent(team_env, roles=("creator", "critic"))
    gateway = ScriptedGateway([
        lead("delegate", role="creator", task="写稿"),
        role_turn("v1"),
        role_turn('{"verdict":"fail","issues":["问题"]}'),
        role_turn("v2"),
        role_turn('{"verdict":"fail","issues":["问题"]}'),
        role_turn("v3"),
        role_turn('{"verdict":"fail","issues":["问题"]}'),
    ])
    await run_team(team_env.session, parent, **deps(team_env, gateway))
    await team_env.session.refresh(parent)
    assert parent.status == "failed"
    assert parent.error_code == "critic_rejected"
    # 现场保留：黑板与子运行仍在
    entries = (await team_env.session.execute(
        select(AgentTeamEntry).where(AgentTeamEntry.run_id == parent.id)
    )).scalars().all()
    assert len(entries) > 0


@pytest.mark.asyncio
async def test_critic_invalid_twice(team_env):
    parent = await make_parent(team_env, roles=("creator", "critic"))
    gateway = ScriptedGateway([
        lead("delegate", role="creator", task="写稿"),
        role_turn("稿件"),
        role_turn("不是 JSON"),
        role_turn("还不是 JSON"),
    ])
    await run_team(team_env.session, parent, **deps(team_env, gateway))
    await team_env.session.refresh(parent)
    assert parent.status == "failed"
    assert parent.error_code == "critic_invalid"


@pytest.mark.asyncio
async def test_loop_detected_same_task_three_times(team_env):
    parent = await make_parent(team_env)
    gateway = ScriptedGateway([
        lead("delegate", role="scout", task="同样的任务"),
        role_turn("r1"),
        lead("delegate", role="scout", task="同样的任务"),
        role_turn("r2"),
        lead("delegate", role="scout", task="同样的任务"),
    ])
    await run_team(team_env.session, parent, **deps(team_env, gateway))
    await team_env.session.refresh(parent)
    assert parent.status == "failed"
    assert parent.error_code == "team_loop_detected"


@pytest.mark.asyncio
async def test_critic_disabled_skips_review(team_env):
    from app.models.agent import AgentTeamRole
    team_env.session.add(AgentTeamRole(
        tenant_id=DEFAULT_TENANT_ID, role="critic", enabled=False,
    ))
    await team_env.session.commit()
    parent = await make_parent(team_env, roles=("creator", "critic"))
    gateway = ScriptedGateway([
        lead("delegate", role="creator", task="写稿"),
        role_turn("稿件"),
        lead("finalize", summary="交付"),
    ])
    await run_team(team_env.session, parent, **deps(team_env, gateway))
    await team_env.session.refresh(parent)
    assert parent.status == "succeeded"
    children = (await team_env.session.execute(
        select(AgentRun).where(AgentRun.parent_run_id == parent.id)
    )).scalars().all()
    assert [c.role for c in children] == ["creator"]
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_team_orchestrator.py -q`
Expected: 新测试 FAIL（无强制 Critic/返工/循环逻辑）。

- [ ] **Step 3: 实现**（修改 `team.py`）

1) `_start_role` 开头加循环检测（`import hashlib`）：

```python
    normalized = " ".join(task.split())[:500]
    key = f"{role}|{hashlib.sha256(normalized.encode()).hexdigest()[:16]}"
    counts = state.setdefault("loop_guard", {})
    counts[key] = int(counts.get(key, 0)) + 1
    if counts[key] >= 3:
        await _fail_team(session, run, TEAM_LOOP_DETECTED, "同一任务连续委派三次")
        raise _TeamStop()
```

2) `_on_child_succeeded` 末尾的 `state["phase"] = "lead"` 替换为分支：

```python
    if child.role == "creator":
        state["creator_task"] = state.get("creator_task") or child.goal
        critic_enabled = (await effective_roles(session, run.tenant_id))["critic"].enabled
        state["phase"] = "critic" if critic_enabled else "lead"
    elif child.role == "critic":
        from app.agent.team_contracts import parse_critic_verdict
        try:
            verdict = parse_critic_verdict(child.result_text)
        except ValueError:
            if int(state.get("critic_invalid", 0)) >= 1:
                await _fail_team(session, run, CRITIC_INVALID, "Critic 连续两次输出无效")
                raise _TeamStop()
            state["critic_invalid"] = int(state.get("critic_invalid", 0)) + 1
            state["critic_note"] = "上次输出无效，只输出 JSON 裁决"
            state["phase"] = "critic"
            _save_state(run, state)
            await session.commit()
            return
        await append_entry(session, run=run, kind="critic", role="critic",
                           content={"verdict": verdict.verdict, "issues": verdict.issues})
        state["critic_verdicts"].append({
            "child_id": str(child.id), "verdict": verdict.verdict,
            "issues": verdict.issues,
        })
        state["critic"] = {
            "verdict": verdict.verdict,
            "rework_rounds": int(state.get("rework", 0)),
            "issues": verdict.issues,
        }
        if verdict.verdict == "pass":
            state["phase"] = "lead"
        else:
            limit = settings_rework_limit(session)  # 见下：改为传参 settings
            ...
    else:
        state["phase"] = "lead"
    _save_state(run, state)
    await session.commit()
```

实现时 `_on_child_succeeded` 增加 `settings` 参数（调用处 `_settle_child` 传入），critic fail 分支完整写法：

```python
        else:
            if int(state.get("rework", 0)) >= settings.agent_team_rework_limit:
                await _fail_team(session, run, CRITIC_REJECTED,
                                 "Critic 返工上限仍未通过")
                raise _TeamStop()
            state["rework"] = int(state.get("rework", 0)) + 1
            state["critic"]["rework_rounds"] = state["rework"]
            state["turn"] = int(state.get("turn", 0)) + 1
            issues = "\n".join(f"- {item}" for item in verdict.issues)
            state["forced_role"] = "creator"
            state["forced_task"] = (
                f"按审稿问题清单返工：\n{issues}\n原任务：{state.get('creator_task', '')}"
            )[:2000]
            state["phase"] = "role"
```

3) 新增 `_start_critic`：

```python
async def _start_critic(session, run, state, *, roles, gateway, tool_registry,
                        skill_registry, storage, queue, settings, session_factory):
    from app.agent.team_contracts import critic_task
    state["turn"] = int(state.get("turn", 0)) + 1
    _save_state(run, state)
    await session.commit()
    artifacts = state.get("artifact_index") or []
    summary = ""
    last = await recent_entries(session, run.id, settings.agent_team_blackboard_inject)
    for entry in reversed(last):
        if entry.kind == "result" and entry.role == "creator":
            summary = str((entry.content or {}).get("summary") or "")
            break
    task = critic_task(
        goal=run.goal, summary=summary, artifacts=artifacts,
        note=state.get("critic_note", ""),
    )
    await _start_role(
        session, run, role="critic", task=task, reason="强制审稿", state=state,
        roles=roles, gateway=gateway, tool_registry=tool_registry,
        skill_registry=skill_registry, storage=storage, queue=queue,
        settings=settings, session_factory=session_factory,
    )
```

4) `_team_loop` 在 `if state.get("current_child_id"):` 块之后、Lead 决策之前插入：

```python
        if state.get("forced_role"):
            role = state.pop("forced_role")
            task = state.pop("forced_task", "")
            await _start_role(
                session, run, role=role, task=task, reason="Critic 否决后强制返工",
                state=state, roles=roles, gateway=gateway,
                tool_registry=tool_registry, skill_registry=skill_registry,
                storage=storage, queue=queue, settings=settings,
                session_factory=session_factory,
            )
            continue
        if state.get("phase") == "critic":
            await _start_critic(
                session, run, state, roles=roles, gateway=gateway,
                tool_registry=tool_registry, skill_registry=skill_registry,
                storage=storage, queue=queue, settings=settings,
                session_factory=session_factory,
            )
            continue
```

5) Lead `rework` 决策分支：`_start_role` 前记录 `state["creator_task"] = decision.task`（若尚未记录）。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run pytest tests/test_team_orchestrator.py -q`
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/team.py backend/tests/test_team_orchestrator.py
git commit -m "feat(team): force critic review, bounded rework and loop detection"
```

---

### Task 7: 失败/取消收敛与子运行清理

**Files:**
- Modify: `backend/app/agent/team.py`
- Test: `backend/tests/test_team_orchestrator.py`（追加）

**Interfaces:**
- Produces（Task 8/9/10/12 消费）:
  - `async cancel_children(session, parent_id, tenant_id, *, moment=None) -> None` — 非终态子运行置 `cancelled`（`running` 先标 `interrupted` 语义：直接置 cancelled 并把其 `running` 步骤标 interrupted），子运行 pending 审批置 `expired`。
  - `async cancel_team_run(session, run) -> bool` — CAS 父 active→cancelled + `cancel_children`；返回是否成功。
  - `async fail_team_run(session, run_id, *, error_code, message) -> None` — 加载父运行并走 `_fail_team`（含子运行收敛）。
  - `_fail_team` 追加 `cancel_children`。

- [ ] **Step 1: 写失败测试**（追加）

```python
@pytest.mark.asyncio
async def test_child_failure_propagates_and_closes_siblings(team_env):
    parent = await make_parent(team_env)
    # 手工构造：一个失败子运行 + 一个排队子运行，父 running
    bad = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=parent.user_id, goal="t",
        mode="role", role="scout", parent_run_id=parent.id,
        status="failed", error_code="agent_budget_exhausted",
        error_message="预算用尽",
    )
    pending = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=parent.user_id, goal="t2",
        mode="role", role="creator", parent_run_id=parent.id, status="queued",
    )
    team_env.session.add_all([bad, pending])
    await team_env.session.commit()
    await team_env.session.refresh(bad)
    parent.status = "running"
    parent.team_state = {"turn": 1, "phase": "role",
                         "current_child_id": str(bad.id)}
    await team_env.session.commit()
    gateway = ScriptedGateway([])
    await run_team(team_env.session, parent, **deps(team_env, gateway))
    await team_env.session.refresh(parent)
    await team_env.session.refresh(pending)
    assert parent.status == "failed"
    assert parent.error_code == "agent_budget_exhausted"
    assert pending.status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_team_run(team_env):
    from app.agent.team import cancel_team_run
    parent = await make_parent(team_env)
    child = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=parent.user_id, goal="t",
        mode="role", role="scout", parent_run_id=parent.id, status="running",
    )
    team_env.session.add(child)
    parent.status = "running"
    await team_env.session.commit()
    assert await cancel_team_run(team_env.session, parent) is True
    await team_env.session.refresh(parent)
    await team_env.session.refresh(child)
    assert parent.status == "cancelled"
    assert child.status == "cancelled"
    assert await cancel_team_run(team_env.session, parent) is False
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_team_orchestrator.py -q -k "propagates or cancel_team"`
Expected: FAIL。

- [ ] **Step 3: 实现**（`team.py`）

```python
from app.models.agent import AgentApproval, AgentStep


async def cancel_children(session, parent_id, tenant_id, *, moment=None) -> None:
    now = moment or _now()
    children = (
        await session.execute(
            select(AgentRun).where(
                AgentRun.parent_run_id == parent_id,
                AgentRun.tenant_id == tenant_id,
                AgentRun.status.in_(ACTIVE),
            )
        )
    ).scalars().all()
    child_ids = [child.id for child in children]
    if not child_ids:
        return
    running_steps = (
        await session.execute(
            select(AgentStep).where(
                AgentStep.run_id.in_(child_ids), AgentStep.status == "running"
            )
        )
    ).scalars().all()
    for step in running_steps:
        step.status = "interrupted"
        step.finished_at = now
    await session.execute(
        update(AgentRun)
        .where(AgentRun.id.in_(child_ids), AgentRun.status.in_(ACTIVE))
        .values(status="cancelled", finished_at=now)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(AgentApproval)
        .where(
            AgentApproval.tenant_id == tenant_id,
            AgentApproval.run_id.in_(child_ids),
            AgentApproval.status == "pending",
        )
        .values(status="expired", decided_at=now, note="团队运行已结束")
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def cancel_team_run(session, run: AgentRun) -> bool:
    moment = _now()
    cancelled = await session.execute(
        update(AgentRun)
        .where(AgentRun.id == run.id, AgentRun.tenant_id == run.tenant_id,
               AgentRun.status.in_(ACTIVE))
        .values(status="cancelled", finished_at=moment)
        .execution_options(synchronize_session=False)
    )
    if cancelled.rowcount == 0:
        await session.rollback()
        return False
    await cancel_children(session, run.id, run.tenant_id, moment=moment)
    await record_best_effort(
        session, tenant_id=run.tenant_id, action="agent.team.cancel",
        actor=None, resource_type="agent_run", resource_id=str(run.id),
    )
    return True


async def fail_team_run(session, run_id, *, error_code: str, message: str) -> None:
    run = await session.get(AgentRun, run_id)
    if run is None or run.status not in ACTIVE:
        return
    await _fail_team(session, run, error_code, message)
```

`_fail_team` 在 CAS 之后、`record_best_effort` 之前追加：

```python
    await cancel_children(session, run.id, run.tenant_id, moment=moment)
```

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run pytest tests/test_team_orchestrator.py -q`
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/team.py backend/tests/test_team_orchestrator.py
git commit -m "feat(team): converge children on team failure and cancel"
```

---

### Task 8: 子运行审批唤醒与驳回

**Files:**
- Modify: `backend/app/api/agent.py`
- Test: `backend/tests/test_team_approvals.py`

**Interfaces:**
- Consumes: `decide_approval`（runtime）、`fail_team_run`（Task 7）、`_enqueue`。
- Produces: `_wake_parent_team(session, queue, child, approval)`、`_team_has_pending(session, parent_id, tenant_id)`、`_restore_team_wait(...)`；decide 端点团队分支。

- [ ] **Step 1: 写失败测试** `backend/tests/test_team_approvals.py`

```python
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db import get_session
from app.deps import get_current_user
from app.main import app
from app.models.agent import AgentApproval, AgentRun, AgentStep
from app.models.base import DEFAULT_TENANT_ID
from app.models.user import User
from app.queue import get_queue


class FakeQueue:
    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, name, *args):
        self.jobs.append((name, *args))


@pytest.fixture
def queue():
    fake = FakeQueue()
    app.dependency_overrides[get_queue] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_queue, None)


async def make_user(db_session, role="owner"):
    user = User(
        id=uuid.uuid4(), tenant_id=DEFAULT_TENANT_ID,
        email=f"{uuid.uuid4().hex[:8]}@example.com", password_hash="x",
        display_name="u", role=role, is_active=True,
        must_change_password=False, is_platform_admin=False, token_version=0,
    )
    db_session.add(user)
    await db_session.commit()
    return user


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


async def make_team(db_session, owner):
    parent = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=owner.id, goal="团队目标",
        mode="team", status="waiting_approval",
        heartbeat_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)
    return parent


async def make_waiting_child(db_session, parent, role):
    child = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=parent.user_id, goal="任务",
        mode="role", role=role, parent_run_id=parent.id,
        status="waiting_approval",
    )
    step = AgentStep(
        tenant_id=DEFAULT_TENANT_ID, run_id=child.id, step_index=0,
        kind="tool", tool_name="demo.write", status="waiting",
    )
    db_session.add_all([child, step])
    await db_session.commit()
    await db_session.refresh(child)
    await db_session.refresh(step)
    approval = AgentApproval(
        tenant_id=DEFAULT_TENANT_ID, run_id=child.id, step_id=step.id,
        tool_name="demo.write", risk="low", payload={},
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    db_session.add(approval)
    await db_session.commit()
    await db_session.refresh(approval)
    return child, approval


@pytest.mark.asyncio
async def test_sibling_pending_keeps_parent_waiting(db_session, api, queue):
    http, state = api
    owner = await make_user(db_session)
    approver = await make_user(db_session)
    state["user"] = approver
    parent = await make_team(db_session, owner)
    child_a, approval_a = await make_waiting_child(db_session, parent, "creator")
    child_b, approval_b = await make_waiting_child(db_session, parent, "publisher")

    resp = await http.post(
        f"/api/agent/approvals/{approval_a.id}/decide",
        json={"decision": "approve"},
    )
    assert resp.status_code == 200
    await db_session.refresh(parent)
    assert parent.status == "waiting_approval"
    assert queue.jobs == []

    resp = await http.post(
        f"/api/agent/approvals/{approval_b.id}/decide",
        json={"decision": "approve"},
    )
    assert resp.status_code == 200
    await db_session.refresh(parent)
    assert parent.status == "queued"
    assert queue.jobs == [("run_agent_job", str(parent.id))]
    assert int((parent.usage or {}).get("paused_ms") or 0) > 0


@pytest.mark.asyncio
async def test_reject_fails_team(db_session, api, queue):
    http, state = api
    owner = await make_user(db_session)
    approver = await make_user(db_session)
    state["user"] = approver
    parent = await make_team(db_session, owner)
    child, approval = await make_waiting_child(db_session, parent, "creator")

    resp = await http.post(
        f"/api/agent/approvals/{approval.id}/decide",
        json={"decision": "reject", "note": "不行"},
    )
    assert resp.status_code == 200
    await db_session.refresh(parent)
    await db_session.refresh(child)
    assert child.status == "failed"
    assert child.error_code == "approval_rejected"
    assert parent.status == "failed"
    assert parent.error_code == "approval_rejected"
    assert queue.jobs == []
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_team_approvals.py -q`
Expected: FAIL（团队分支不存在，父运行不会 queued/failed）。

- [ ] **Step 3: 实现**（`api/agent.py`）

新增辅助函数（放在 `_wake_parent_workflow` 附近）：

```python
from app.agent.team import fail_team_run


async def _team_has_pending(session, parent_id, tenant_id) -> bool:
    row = (
        await session.execute(
            select(AgentApproval.id)
            .join(AgentRun, AgentRun.id == AgentApproval.run_id)
            .where(
                AgentRun.parent_run_id == parent_id,
                AgentRun.tenant_id == tenant_id,
                AgentApproval.status == "pending",
            )
            .limit(1)
        )
    ).first()
    return row is not None


async def _restore_team_wait(session, parent, child, approval, *,
                             previous_paused_ms, paused_at) -> None:
    parent_id, child_id, approval_id = parent.id, child.id, approval.id
    tenant = parent.tenant_id
    usage = dict(parent.usage or {})
    usage["paused_ms"] = previous_paused_ms
    reset_parent = await session.execute(
        update(AgentRun)
        .where(AgentRun.id == parent_id, AgentRun.tenant_id == tenant,
               AgentRun.status == "queued")
        .values(status="waiting_approval", usage=usage, heartbeat_at=paused_at,
                finished_at=None, error_code="", error_message="")
        .execution_options(synchronize_session=False)
    )
    reset_child = await session.execute(
        update(AgentRun)
        .where(AgentRun.id == child_id, AgentRun.tenant_id == tenant,
               AgentRun.status == "queued")
        .values(status="waiting_approval", finished_at=None,
                error_code="", error_message="")
        .execution_options(synchronize_session=False)
    )
    reset_approval = await session.execute(
        update(AgentApproval)
        .where(AgentApproval.id == approval_id, AgentApproval.tenant_id == tenant,
               AgentApproval.status == "approved")
        .values(status="pending", decided_by=None, decided_at=None, note="")
        .execution_options(synchronize_session=False)
    )
    if reset_parent.rowcount and reset_child.rowcount and reset_approval.rowcount:
        await session.commit()
        return
    await session.rollback()
    logger.warning("team approval compensation skipped run=%s approval=%s",
                   child_id, approval_id)


async def _wake_parent_team(session, queue, child, approval) -> None:
    parent = (
        await session.execute(
            select(AgentRun)
            .where(AgentRun.id == child.parent_run_id,
                   AgentRun.tenant_id == child.tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if parent is None:
        raise AppError("not_found", "运行不存在", 404)
    if parent.status != "waiting_approval":
        await session.rollback()
        return
    if await _team_has_pending(session, parent.id, parent.tenant_id):
        await session.rollback()
        return
    moment = datetime.now(UTC)
    paused_at = _utc(parent.heartbeat_at) or moment
    previous_paused_ms = int((parent.usage or {}).get("paused_ms") or 0)
    waited_ms = int(max(0.0, (moment - min(paused_at, moment)).total_seconds()) * 1000)
    usage = dict(parent.usage or {})
    usage["paused_ms"] = previous_paused_ms + waited_ms
    resumed = await session.execute(
        update(AgentRun)
        .where(AgentRun.id == parent.id, AgentRun.tenant_id == parent.tenant_id,
               AgentRun.status == "waiting_approval")
        .values(status="queued", usage=usage, heartbeat_at=moment,
                finished_at=None, error_code="", error_message="")
        .execution_options(synchronize_session=False)
    )
    if resumed.rowcount == 0:
        await session.rollback()
        return
    await session.commit()
    try:
        await _enqueue(queue, parent)
    except AppError:
        await _restore_team_wait(session, parent, child, approval,
                                 previous_paused_ms=previous_paused_ms,
                                 paused_at=paused_at)
        raise
```

`decide` 端点改造：approve 分支在 `if run.workflow_run_id is not None:` 之前插入团队分支；reject 分支同理：

```python
    if payload.decision == "approve":
        if run.workflow_run_id is not None:
            ...  # 既有工作流分支不动
        elif run.parent_run_id is not None:
            await _wake_parent_team(session, queue, run, approval)
            run = await session.get(AgentRun, child_id)
            return _run_out(run)
        try:
            await _enqueue(queue, run)
        ...
    elif run.workflow_run_id is not None:
        ...  # 既有
    elif run.parent_run_id is not None:
        await fail_team_run(session, run.parent_run_id,
                            error_code="approval_rejected", message=APPROVAL_REJECTED)
        run = await session.get(AgentRun, child_id)
    return _run_out(run)
```

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run pytest tests/test_team_approvals.py tests/test_agent_api.py tests/test_agent_approvals.py -q`
Expected: 全部通过（既有工作流/单 Agent 审批不回归）。

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/agent.py backend/tests/test_team_approvals.py
git commit -m "feat(team): wake parent team on child approval and fail on reject"
```

---

### Task 9: 对账扩展（stale 排除 role、等待结算、终态残留）

**Files:**
- Modify: `backend/app/worker.py`
- Test: `backend/tests/test_team_worker.py`

**Interfaces:**
- Consumes: `fail_team_run`、`cancel_children`（Task 7）。
- Produces: `_settle_waiting_team_parents(session, *, moment, queue, limit=100) -> int`、`_close_terminal_team_stragglers(session, *, limit=100) -> int`；`reconcile_agent_runs` 调用二者；`_stale_agent_run_ids` 排除 `mode="role"`。

- [ ] **Step 1: 写失败测试** `backend/tests/test_team_worker.py`

```python
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.agent import AgentApproval, AgentRun, AgentStep
from app.models.base import DEFAULT_TENANT_ID
from app.worker import reconcile_agent_runs


class FakeQueue:
    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, name, *args):
        self.jobs.append((name, *args))


async def make_parent(db_session, status):
    run = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=uuid.uuid4(), goal="g",
        mode="team", status=status,
        heartbeat_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)
    return run


async def make_child(db_session, parent, status, *, approval_status=None):
    child = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=parent.user_id, goal="t",
        mode="role", role="creator", parent_run_id=parent.id, status=status,
    )
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(child)
    if approval_status:
        step = AgentStep(
            tenant_id=DEFAULT_TENANT_ID, run_id=child.id, step_index=0,
            kind="tool", tool_name="demo.write", status="waiting",
        )
        db_session.add(step)
        await db_session.commit()
        await db_session.refresh(step)
        approval = AgentApproval(
            tenant_id=DEFAULT_TENANT_ID, run_id=child.id, step_id=step.id,
            tool_name="demo.write", risk="low", payload={},
            status=approval_status,
        )
        db_session.add(approval)
        await db_session.commit()
    return child


@pytest.mark.asyncio
async def test_waiting_team_with_approved_requeues(db_session):
    parent = await make_parent(db_session, "waiting_approval")
    await make_child(db_session, parent, "waiting_approval",
                     approval_status="approved")
    queue = FakeQueue()
    await reconcile_agent_runs(session_factory=None, queue=queue) if False else None
    from app.worker import _settle_waiting_team_parents
    handled = await _settle_waiting_team_parents(
        db_session, moment=datetime.now(UTC), queue=queue,
    )
    assert handled == 1
    await db_session.refresh(parent)
    assert parent.status == "queued"
    assert queue.jobs == [("run_agent_job", str(parent.id))]


@pytest.mark.asyncio
async def test_waiting_team_zero_pending_no_approved_fails(db_session):
    parent = await make_parent(db_session, "waiting_approval")
    await make_child(db_session, parent, "waiting_approval",
                     approval_status="expired")
    from app.worker import _settle_waiting_team_parents
    handled = await _settle_waiting_team_parents(
        db_session, moment=datetime.now(UTC), queue=None,
    )
    assert handled == 1
    await db_session.refresh(parent)
    assert parent.status == "failed"
    assert parent.error_code == "approval_expired"


@pytest.mark.asyncio
async def test_terminal_team_closes_stragglers(db_session):
    parent = await make_parent(db_session, "failed")
    child = await make_child(db_session, parent, "queued")
    from app.worker import _close_terminal_team_stragglers
    handled = await _close_terminal_team_stragglers(db_session)
    assert handled == 1
    await db_session.refresh(child)
    assert child.status == "cancelled"


@pytest.mark.asyncio
async def test_stale_scan_skips_role_children(db_session):
    from app.worker import _stale_agent_run_ids
    parent = await make_parent(db_session, "running")
    await make_child(db_session, parent, "running")
    cutoff = datetime.now(UTC)
    ids = await _stale_agent_run_ids(db_session, cutoff, 100)
    assert parent.id in ids
    children = (await db_session.execute(
        select(AgentRun.id).where(AgentRun.mode == "role")
    )).scalars().all()
    assert not set(children) & set(ids)
```

注意：第一个测试里 `reconcile_agent_runs(...) if False else None` 是笔误占位，实现测试时删除该行，只保留 `_settle_waiting_team_parents` 调用。

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_team_worker.py -q`
Expected: FAIL（函数不存在）。

- [ ] **Step 3: 实现**（`worker.py`）

1) `_stale_agent_run_ids` 的 where 追加 `AgentRun.mode != "role"`。

2) 新增（`from sqlalchemy.orm import aliased`、`from sqlalchemy import exists`、`from app.agent.team import cancel_children, fail_team_run`、`from app.agent.runtime import APPROVAL_EXPIRED`）：

```python
async def _team_pending_count(session, parent_id) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(AgentApproval)
            .join(AgentRun, AgentRun.id == AgentApproval.run_id)
            .where(AgentRun.parent_run_id == parent_id,
                   AgentApproval.status == "pending")
        )
    ).scalar_one()


async def _team_approved_count(session, parent_id) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(AgentApproval)
            .join(AgentRun, AgentRun.id == AgentApproval.run_id)
            .where(AgentRun.parent_run_id == parent_id,
                   AgentApproval.status == "approved")
        )
    ).scalar_one()


async def _settle_waiting_team_parents(session, *, moment, queue, limit=100) -> int:
    ids = (
        await session.execute(
            select(AgentRun.id)
            .where(AgentRun.mode == "team",
                   AgentRun.status == "waiting_approval")
            .order_by(AgentRun.updated_at)
            .limit(limit)
        )
    ).scalars().all()
    handled = 0
    for run_id in ids:
        parent = (
            await session.execute(
                select(AgentRun).where(AgentRun.id == run_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if parent is None or parent.status != "waiting_approval":
            await session.rollback()
            continue
        if await _team_pending_count(session, parent.id) > 0:
            await session.rollback()
            continue
        if await _team_approved_count(session, parent.id) == 0:
            await session.rollback()
            await fail_team_run(session, parent.id,
                                error_code="approval_expired",
                                message=APPROVAL_EXPIRED)
            handled += 1
            continue
        paused_at = parent.heartbeat_at or moment
        if paused_at.tzinfo is None:
            paused_at = paused_at.replace(tzinfo=UTC)
        waited_ms = int(max(0.0, (moment - min(paused_at, moment)).total_seconds()) * 1000)
        usage = dict(parent.usage or {})
        usage["paused_ms"] = int(usage.get("paused_ms") or 0) + waited_ms
        resumed = await session.execute(
            update(AgentRun)
            .where(AgentRun.id == parent.id,
                   AgentRun.status == "waiting_approval")
            .values(status="queued", usage=usage, heartbeat_at=moment)
            .execution_options(synchronize_session=False)
        )
        if resumed.rowcount == 0:
            await session.rollback()
            continue
        await session.commit()
        if queue is not None:
            try:
                await queue.enqueue_job("run_agent_job", str(parent.id))
            except Exception:
                logger.exception("team requeue enqueue failed run=%s", parent.id)
        handled += 1
    return handled


async def _close_terminal_team_stragglers(session, *, limit=100) -> int:
    child = aliased(AgentRun)
    ids = (
        await session.execute(
            select(AgentRun.id)
            .where(
                AgentRun.mode == "team",
                AgentRun.status.in_(("succeeded", "failed", "cancelled")),
                exists().where(
                    child.parent_run_id == AgentRun.id,
                    child.status.in_(("queued", "running", "waiting_approval")),
                ),
            )
            .limit(limit)
        )
    ).scalars().all()
    handled = 0
    for run_id in ids:
        parent = await session.get(AgentRun, run_id)
        if parent is None:
            continue
        await cancel_children(session, parent.id, parent.tenant_id)
        handled += 1
    return handled
```

3) `reconcile_agent_runs` 在审批过期阶段之后追加第三阶段：

```python
    try:
        async with session_factory() as session:
            settled = await _settle_waiting_team_parents(
                session, moment=moment, queue=worker_queue,
            )
            closed = await _close_terminal_team_stragglers(session)
            if settled or closed:
                logger.info("团队对账：结算 %s 个等待父运行，收敛 %s 个残留", settled, closed)
    except Exception:
        logger.exception("team reconciliation sweep failed")
```

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run pytest tests/test_team_worker.py tests/test_agent_worker.py -q`
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add backend/app/worker.py backend/tests/test_team_worker.py
git commit -m "feat(team): extend reconcile for waiting parents and stragglers"
```

---

### Task 10: 团队 API 与角色配置 API

**Files:**
- Create: `backend/app/api/agent_team.py`
- Modify: `backend/app/main.py`、`backend/app/api/agent.py`（RunOut 字段 + retry 门禁）
- Test: `backend/tests/test_team_api.py`

**Interfaces:**
- Consumes: Task 3 校验函数、`agent_api._run_out/_enqueue/_ensure_limits`、`snapshot_policy/load_overrides`、`get_tool_registry`、`cancel_team_run`。
- Produces: `GET/PUT /api/agent/team/roles`、`POST /api/agent/team/runs`、`GET /api/agent/team/runs/{id}`、`POST /api/agent/team/runs/{id}/cancel`；`RunOut.role/parent_run_id`；retry 对 team 返回 `409 run_not_retryable`。

- [ ] **Step 1: 写失败测试** `backend/tests/test_team_api.py`

```python
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db import get_session
from app.deps import get_current_user
from app.main import app
from app.models.agent import AgentRun
from app.models.base import DEFAULT_TENANT_ID
from app.models.user import User
from app.queue import get_queue

OTHER_TENANT = "00000000-0000-0000-0000-000000000002"


class FakeQueue:
    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, name, *args):
        self.jobs.append((name, *args))


@pytest.fixture
def queue():
    fake = FakeQueue()
    app.dependency_overrides[get_queue] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_queue, None)


async def make_user(db_session, role="owner", tenant_id=DEFAULT_TENANT_ID):
    user = User(
        id=uuid.uuid4(), tenant_id=tenant_id,
        email=f"{uuid.uuid4().hex[:8]}@example.com", password_hash="x",
        display_name="u", role=role, is_active=True,
        must_change_password=False, is_platform_admin=False, token_version=0,
    )
    db_session.add(user)
    await db_session.commit()
    return user


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


@pytest.mark.asyncio
async def test_roles_get_and_put(db_session, api):
    http, state = api
    state["user"] = await make_user(db_session, role="owner")
    resp = await http.get("/api/agent/team/roles")
    assert resp.status_code == 200
    roles = resp.json()
    assert [r["role"] for r in roles] == ["planner", "scout", "creator", "critic", "publisher"]
    resp = await http.put("/api/agent/team/roles", json={"roles": [
        {"role": "scout", "enabled": False, "tools": ["profile.get"],
         "budget": {"max_tokens": 1000}},
    ]})
    assert resp.status_code == 200
    scout = next(r for r in resp.json() if r["role"] == "scout")
    assert scout["enabled"] is False and scout["tools"] == ["profile.get"]
    resp = await http.put("/api/agent/team/roles", json={"roles": [
        {"role": "scout", "tools": ["publish.execute"]},
    ]})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "policy_not_loosenable"
    resp = await http.put("/api/agent/team/roles", json={"roles": [
        {"role": "creator", "enabled": False},
    ]})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_editor_cannot_put_roles(db_session, api):
    http, state = api
    state["user"] = await make_user(db_session, role="editor")
    resp = await http.put("/api/agent/team/roles", json={"roles": []})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_run_validation_and_success(db_session, api, queue):
    http, state = api
    state["user"] = await make_user(db_session, role="editor")
    resp = await http.post("/api/agent/team/runs",
                           json={"goal": "做内容", "roles": ["scout"]})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "team_roles_invalid"
    resp = await http.post("/api/agent/team/runs",
                           json={"goal": "做内容", "roles": ["creator"],
                                 "budget": {"max_tokens": 999999}})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "budget_invalid"
    resp = await http.post("/api/agent/team/runs",
                           json={"goal": "做内容", "roles": ["scout", "creator"]})
    assert resp.status_code == 202
    body = resp.json()
    assert body["mode"] == "team"
    assert queue.jobs == [("run_agent_job", body["id"])]


@pytest.mark.asyncio
async def test_detail_and_cancel_and_isolation(db_session, api, queue):
    http, state = api
    editor = await make_user(db_session, role="editor")
    state["user"] = editor
    resp = await http.post("/api/agent/team/runs",
                           json={"goal": "做内容", "roles": ["creator"]})
    run_id = resp.json()["id"]
    resp = await http.get(f"/api/agent/team/runs/{run_id}")
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["run"]["id"] == run_id
    assert detail["children"] == [] and detail["entries"] == []
    # 跨租户 404
    other = await make_user(db_session, role="owner", tenant_id=OTHER_TENANT)
    state["user"] = other
    resp = await http.get(f"/api/agent/team/runs/{run_id}")
    assert resp.status_code == 404
    # 非发起人 editor 不能取消
    stranger = await make_user(db_session, role="editor")
    state["user"] = stranger
    resp = await http.post(f"/api/agent/team/runs/{run_id}/cancel")
    assert resp.status_code == 403
    state["user"] = editor
    resp = await http.post(f"/api/agent/team/runs/{run_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    resp = await http.post(f"/api/agent/team/runs/{run_id}/cancel")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_team_retry_forbidden(db_session, api, queue):
    http, state = api
    editor = await make_user(db_session, role="editor")
    state["user"] = editor
    resp = await http.post("/api/agent/team/runs",
                           json={"goal": "做内容", "roles": ["creator"]})
    run_id = resp.json()["id"]
    run = await db_session.get(AgentRun, uuid.UUID(run_id))
    run.status = "failed"
    await db_session.commit()
    resp = await http.post(f"/api/agent/runs/{run_id}/retry")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "run_not_retryable"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_team_api.py -q`
Expected: FAIL（404 路由不存在）。

- [ ] **Step 3: 实现**

`api/agent.py`：`RunOut` 增字段并在 `_run_out` 填充：

```python
    role: str = ""
    parent_run_id: str | None = None
```

```python
        role=run.role,
        parent_run_id=str(run.parent_run_id) if run.parent_run_id else None,
```

`retry_run` 开头（`_owned_run` 之后）：

```python
    if source.mode == "team":
        raise AppError("run_not_retryable", "团队运行不支持重试，请重新发起", 409)
```

新建 `api/agent_team.py`：

```python
import uuid
from datetime import datetime
from typing import Any

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.policy import load_overrides, snapshot_policy
from app.agent.registry import get_tool_registry
from app.agent.roles import (
    ROLE_KEYS, ROLES, clamp_team_budget, effective_roles,
    validate_participants, validate_role_patch,
)
from app.agent.team import cancel_team_run
from app.api import agent as agent_api
from app.config import get_settings
from app.db import get_session
from app.errors import AppError
from app.models.agent import AgentRun, AgentTeamEntry, AgentTeamRole
from app.models.user import User
from app.permissions import admin_access, editor_access, viewer_access
from app.queue import get_queue
from app.services.audit import record_best_effort

router = APIRouter(prefix="/agent/team", tags=["agent-team"])


class RolePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str
    enabled: bool | None = None
    tools: list[str] | None = None
    budget: dict[str, Any] | None = None


class RolesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roles: list[RolePatch] = Field(default_factory=list)


class TeamRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=4000)
    roles: list[str]
    budget: dict[str, Any] = Field(default_factory=dict)


def _role_out(role) -> dict:
    spec = ROLES[role.key]
    return {
        "role": role.key, "label": role.label, "enabled": role.enabled,
        "tools": list(role.tools), "default_tools": list(spec.tools),
        "budget": dict(role.budget), "limits": dict(spec.budget),
    }


@router.get("/roles")
async def get_roles(
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
):
    roles = await effective_roles(session, user.tenant_id)
    return [_role_out(roles[key]) for key in ROLE_KEYS]


@router.put("/roles")
async def put_roles(
    payload: RolesIn,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
):
    for patch in payload.roles:
        validate_role_patch(patch.role, enabled=patch.enabled,
                            tools=patch.tools, budget=patch.budget)
    for patch in payload.roles:
        spec = ROLES[patch.role]
        row = (
            await session.execute(
                select(AgentTeamRole).where(
                    AgentTeamRole.tenant_id == user.tenant_id,
                    AgentTeamRole.role == patch.role,
                )
            )
        ).scalar_one_or_none()
        enabled = spec.required if patch.enabled is None else patch.enabled
        if spec.required:
            enabled = True
        tools = list(patch.tools) if patch.tools is not None else list(spec.tools)
        budget = dict(spec.budget)
        if patch.budget is not None:
            budget.update(patch.budget)
        is_default = (
            enabled and tools == list(spec.tools) and budget == spec.budget
        )
        if is_default:
            if row is not None:
                await session.delete(row)
            continue
        if row is None:
            row = AgentTeamRole(tenant_id=user.tenant_id, role=patch.role)
            session.add(row)
        row.enabled = enabled
        row.tools = tools
        row.budget = budget
    await session.commit()
    await record_best_effort(
        session, tenant_id=user.tenant_id, action="agent.team.roles.update",
        actor=user, resource_type="agent_team_roles", resource_id=user.tenant_id,
    )
    roles = await effective_roles(session, user.tenant_id)
    return [_role_out(roles[key]) for key in ROLE_KEYS]


@router.post("/runs", status_code=202)
async def create_team_run(
    payload: TeamRunIn,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    queue: ArqRedis | None = Depends(get_queue),  # noqa: B008
):
    settings = get_settings()
    goal = payload.goal.strip()
    if not goal:
        raise AppError("invalid_payload", "目标不能为空", 422)
    effective = await effective_roles(session, user.tenant_id)
    participants = validate_participants(payload.roles, effective)
    budget = clamp_team_budget(payload.budget, settings)
    await agent_api._ensure_limits(session, user, settings)
    specs = {spec.name: spec for spec in get_tool_registry().all()}
    overrides = await load_overrides(session, user.tenant_id)
    run = AgentRun(
        tenant_id=user.tenant_id, user_id=user.id, goal=goal,
        mode="team", status="queued", plan={"roles": list(participants)},
        budget=budget, policy=snapshot_policy(overrides, specs),
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    try:
        await agent_api._enqueue(queue, run)
    except AppError:
        from app.agent.runtime import fail_run
        await fail_run(session, run, "queue_unavailable",
                       agent_api.QUEUE_UNAVAILABLE_MESSAGE)
        raise
    await record_best_effort(
        session, tenant_id=user.tenant_id, action="agent.team.create",
        actor=user, resource_type="agent_run", resource_id=str(run.id),
        detail={"roles": list(participants), "budget": budget},
    )
    return agent_api._run_out(run)


@router.get("/runs/{run_id}")
async def get_team_run(
    run_id: str,
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
):
    run = await agent_api._owned_run(session, run_id, user)
    if run.mode != "team":
        raise AppError("not_found", "资源不存在", 404)
    children = (
        await session.execute(
            select(AgentRun).where(AgentRun.parent_run_id == run.id)
            .order_by(AgentRun.created_at, AgentRun.id)
        )
    ).scalars().all()
    entries = (
        await session.execute(
            select(AgentTeamEntry).where(AgentTeamEntry.run_id == run.id)
            .order_by(AgentTeamEntry.seq)
        )
    ).scalars().all()
    return {
        "run": agent_api._run_out(run).model_dump(),
        "children": [
            {
                "id": str(child.id), "role": child.role, "status": child.status,
                "attempts": child.attempts, "usage": child.usage or {},
                "result_text": child.result_text[:500],
                "error_code": child.error_code,
                "created_at": child.created_at, "finished_at": child.finished_at,
            }
            for child in children
        ],
        "entries": [
            {"seq": e.seq, "kind": e.kind, "role": e.role,
             "content": e.content, "created_at": e.created_at}
            for e in entries
        ],
        "artifacts": (run.result or {}).get("artifacts")
        or (run.team_state or {}).get("artifact_index") or [],
    }


@router.post("/runs/{run_id}/cancel")
async def cancel_team(
    run_id: str,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
):
    run = await agent_api._owned_run(session, run_id, user)
    if run.mode != "team":
        raise AppError("not_found", "资源不存在", 404)
    if user.role not in {"admin", "owner"} and user.id != run.user_id:
        raise AppError("forbidden", "只能取消自己发起的团队运行", 403)
    if not await cancel_team_run(session, run):
        raise AppError("run_not_cancellable", "运行已结束，无法取消", 409)
    await session.refresh(run)
    return agent_api._run_out(run)
```

`main.py`：`from app.api import agent_team` 并 `app.include_router(agent_team.router, prefix="/api")`（放在 agent 之后）。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run pytest tests/test_team_api.py tests/test_agent_api.py -q`
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/agent_team.py backend/app/api/agent.py backend/app/main.py backend/tests/test_team_api.py
git commit -m "feat(team): add team run and role config APIs"
```

---

### Task 11: Worker 按 mode 分派

**Files:**
- Modify: `backend/app/worker.py`
- Test: `backend/tests/test_team_worker.py`（追加）

**Interfaces:**
- Consumes: `run_team`（Task 5）。
- Produces: `run_agent_job` 对 `mode="team"` 调 `run_team`；`mode="role"` 直接返回（永不单独执行）。

- [ ] **Step 1: 写失败测试**（追加到 `test_team_worker.py`）

```python
@pytest.mark.asyncio
async def test_job_dispatches_team(db_session, monkeypatch):
    import app.worker as worker_module
    parent = await make_parent(db_session, "queued")
    called = {}

    async def fake_run_team(session, run, **kwargs):
        called["team"] = str(run.id)

    async def fake_run_agent(session, run, **kwargs):
        called["single"] = str(run.id)

    monkeypatch.setattr(worker_module, "run_team", fake_run_team)
    monkeypatch.setattr(worker_module, "run_agent", fake_run_agent)

    class StubGateway:
        pass

    from sqlalchemy.ext.asyncio import async_sessionmaker
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    await worker_module.run_agent_job(
        None, str(parent.id), session_factory=factory, gateway=StubGateway(),
    )
    assert called == {"team": str(parent.id)}


@pytest.mark.asyncio
async def test_job_never_runs_role_child(db_session, monkeypatch):
    import app.worker as worker_module
    parent = await make_parent(db_session, "running")
    child = await make_child(db_session, parent, "queued")
    called = {}

    async def fake_run_team(session, run, **kwargs):
        called["team"] = True

    async def fake_run_agent(session, run, **kwargs):
        called["single"] = True

    monkeypatch.setattr(worker_module, "run_team", fake_run_team)
    monkeypatch.setattr(worker_module, "run_agent", fake_run_agent)
    from sqlalchemy.ext.asyncio import async_sessionmaker
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    await worker_module.run_agent_job(
        None, str(child.id), session_factory=factory, gateway=object(),
    )
    assert called == {}
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_team_worker.py -q -k dispatches or never_runs`
Expected: FAIL。

- [ ] **Step 3: 实现**（`worker.py`）

顶部 `from app.agent.team import run_team`（与既有 team import 合并）。`run_agent_job` 中读取 run 后：

```python
            if run is None or run.status != "queued":
                return
            if run.mode == "role":
                # 角色子运行只由团队编排器驱动，绝不单独执行。
                return
```

执行处把 `await run_agent(...)` 包成：

```python
            executor = run_team if run.mode == "team" else run_agent
            await executor(
                session, run,
                gateway=active_gateway,
                tool_registry=tool_registry or get_tool_registry(),
                skill_registry=skill_registry or get_registry(),
                storage=storage or get_storage(),
                queue=worker_queue,
                settings=get_settings(),
                session_factory=session_factory,
            )
```

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run pytest tests/test_team_worker.py tests/test_agent_worker.py -q`
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add backend/app/worker.py backend/tests/test_team_worker.py
git commit -m "feat(team): dispatch worker jobs by run mode"
```

---

### Task 12: 工作流 team 节点（图、校验、引擎）

**Files:**
- Modify: `backend/app/workflow/graph.py`、`backend/app/workflow/validate.py`、`backend/app/workflow/engine.py`
- Test: `backend/tests/test_workflow_team_node.py`

**Interfaces:**
- Consumes: `run_team`、`EXECUTION_ROLES`、`TEAM_BUDGET_KEYS`、`team_budget_defaults/ceilings`。
- Produces: `NODE_KINDS` 含 `"team"`；`_check_team`；`engine._execute_team`；节点数据 `{type:"team", roles:[...], goal:"...", budget:{...}, retry:0..3}`。

- [ ] **Step 1: 写失败测试** `backend/tests/test_workflow_team_node.py`

```python
import pytest

from app.agent.registry import get_tool_registry
from app.workflow.graph import NODE_KINDS, parse_graph
from app.workflow.validate import validate_publish


def team_graph(*, roles=None, goal="为 {{inputs.topic}} 产出内容", budget=None):
    return {
        "nodes": [
            {"id": "in", "type": "input",
             "data": {"fields": [{"key": "topic", "type": "string", "required": True}]}},
            {"id": "tm", "type": "team",
             "data": {"name": "内容团队", "roles": roles or ["scout", "creator", "critic"],
                      "goal": goal, "budget": budget or {}, "retry": 0}},
            {"id": "out", "type": "output", "data": {}},
        ],
        "edges": [
            {"id": "e1", "source": "in", "target": "tm"},
            {"id": "e2", "source": "tm", "target": "out"},
        ],
    }


def test_team_kind_accepted():
    assert "team" in NODE_KINDS
    graph = parse_graph(team_graph())
    assert graph.nodes["tm"].kind == "team"


def test_team_validation():
    registry = get_tool_registry()
    result = validate_publish(team_graph(), registry=registry, skill_ids=set())
    assert result.ok
    result = validate_publish(team_graph(roles=["scout"]), registry=registry, skill_ids=set())
    assert not result.ok  # 缺 creator
    result = validate_publish(team_graph(roles=["ghost", "creator"]), registry=registry, skill_ids=set())
    assert not result.ok
    result = validate_publish(team_graph(budget={"max_tokens": 0}), registry=registry, skill_ids=set())
    assert not result.ok
```

引擎执行测试复用 `tests/test_workflow_agent_node.py` 的夹具形状（`env`/`node`/`edge`/FakeGateway），但网关需同时支持 `complete`（Lead）与 `complete_tool_calls`（角色）。追加：

```python
import json
import uuid

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.contracts import ToolResult, ToolSpec
from app.agent.registry import ToolRegistry
from app.config import get_settings
from app.llm.tools import ToolTurn
from app.llm.types import Usage
from app.models.agent import AgentRun
from app.models.base import DEFAULT_TENANT_ID
from app.models.workflow import Workflow, WorkflowRun, WorkflowVersion
from app.skills.registry import SkillRegistry
from app.storage import get_storage
from app.workflow.engine import EngineDeps, run_workflow


class TeamGateway:
    def __init__(self, script):
        self.script = list(script)
        self.last_usage = Usage(10, 5)

    async def complete(self, messages, **kwargs):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def complete_tool_calls(self, messages, *, tools):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def rturn(content):
    return ToolTurn(content=content, tool_calls=[], finish_reason="stop",
                    usage=Usage(20, 10))


@pytest_asyncio.fixture
async def wf_env(db_session):
    settings = get_settings()

    class Env:
        pass

    holder = Env()
    holder.factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    holder.settings = settings
    holder.session = db_session
    holder.registry = ToolRegistry()
    return holder


async def make_workflow_run(env, graph):
    workflow = Workflow(
        tenant_id=DEFAULT_TENANT_ID, name="团队流", description="",
        created_by=uuid.uuid4(),
    )
    env.session.add(workflow)
    await env.session.commit()
    await env.session.refresh(workflow)
    version = WorkflowVersion(
        tenant_id=DEFAULT_TENANT_ID, workflow_id=workflow.id, version=1,
        graph=graph, source_revision=1, published_by=uuid.uuid4(),
        published_at=__import__("datetime").datetime.now(
            __import__("datetime").UTC),
        note="",
    )
    run = WorkflowRun(
        tenant_id=DEFAULT_TENANT_ID, workflow_id=workflow.id, version_id=None,
        status="queued", trigger="manual", triggered_by=uuid.uuid4(),
        inputs={"topic": "秋季护肤"}, policy={},
    )
    env.session.add_all([version, run])
    await env.session.commit()
    await env.session.refresh(version)
    run.version_id = version.id
    await env.session.commit()
    await env.session.refresh(run)
    return run


@pytest.mark.asyncio
async def test_engine_team_node_success(wf_env):
    graph = team_graph(roles=["creator"])
    run = await make_workflow_run(wf_env, graph)
    gateway = TeamGateway([
        json.dumps({"action": "delegate", "role": "creator", "task": "写稿"}),
        rturn("稿件完成"),
        json.dumps({"action": "finalize", "summary": "已交付"}),
    ])
    deps = EngineDeps(
        gateway=gateway, tool_registry=wf_env.registry,
        skill_registry=SkillRegistry(wf_env.settings.skills_dir),
        storage=get_storage(), queue=None, settings=wf_env.settings,
        session_factory=wf_env.factory,
    )
    outcome = await run_workflow(wf_env.session, run, deps=deps)
    assert outcome in {"ok", "succeeded"}
    await wf_env.session.refresh(run)
    assert run.status == "succeeded"
    teams = (await wf_env.session.execute(
        select(AgentRun).where(AgentRun.mode == "team",
                               AgentRun.workflow_run_id == run.id)
    )).scalars().all()
    assert len(teams) == 1 and teams[0].status == "succeeded"
```

注意：`run_workflow` 的返回口径与 `WorkflowRun` 必填字段以 `tests/test_workflow_agent_node.py` 现有夹具为准；实现测试时直接复用该文件的 `make_workflow`/运行构造帮助函数（若存在），否则按上面最小构造并对齐模型必填列。`outcome` 断言以引擎实际返回字符串为准（参照既有 agent 节点测试）。

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_workflow_team_node.py -q`
Expected: FAIL（`team` 不在 NODE_KINDS）。

- [ ] **Step 3: 实现**

`graph.py`：`NODE_KINDS = ("input", "tool", "agent", "team", "condition", "review", "output")`。

`validate.py`：新增并在 `_check_node` 分派：

```python
from app.agent.roles import EXECUTION_ROLES, TEAM_BUDGET_KEYS


def _check_team(node: GraphNode, issues: list[Issue]) -> None:
    data = node.data
    goal = data.get("goal")
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 4000:
        issues.append(Issue("node_invalid", "团队目标长度必须为 1–4000", node.id))
    roles = data.get("roles")
    if not isinstance(roles, list) or not roles:
        issues.append(Issue("team_roles_invalid", "团队角色不能为空", node.id))
    else:
        for role in roles:
            if role not in EXECUTION_ROLES:
                issues.append(Issue("team_roles_invalid", f"未知角色：{role}", node.id))
        if "creator" not in roles:
            issues.append(Issue("team_roles_invalid", "团队必须包含 creator", node.id))
    budget = data.get("budget") or {}
    if not isinstance(budget, dict):
        issues.append(Issue("node_invalid", "团队预算必须是对象", node.id))
        return
    ceilings = {"max_tokens": 200000, "max_tool_calls": 60, "max_turns": 20, "max_seconds": 1800}
    for key, value in budget.items():
        if key not in TEAM_BUDGET_KEYS:
            issues.append(Issue("node_invalid", f"未知预算维度: {key}", node.id))
        elif not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            issues.append(Issue("node_invalid", f"预算 {key} 必须是正整数", node.id))
        elif value > ceilings[key]:
            issues.append(Issue("node_invalid", f"预算 {key} 超过上限", node.id))
```

`_check_node` 中 `elif node.kind == "agent":` 之后加 `elif node.kind == "team": _check_team(node, issues)`。

`engine.py`：`_execute_node` 加分派；新增 `_execute_team`（镜像 `_execute_agent`）：

```python
from app.agent.roles import EXECUTION_ROLES, team_budget_defaults, team_budget_ceilings
from app.agent.team import run_team


def _team_budget(node: GraphNode, settings: Settings) -> dict[str, int]:
    budget = team_budget_defaults(settings)
    ceilings = team_budget_ceilings(settings)
    raw = node.data.get("budget")
    if isinstance(raw, dict):
        for key, value in raw.items():
            if (key in budget and isinstance(value, int)
                    and not isinstance(value, bool) and value > 0):
                budget[key] = min(value, ceilings[key])
    cap = int(settings.workflow_time_slice_seconds or 0)
    if cap > 0:
        attempts = _retry_budget(node) + 1
        backoffs = int(sum(_backoff(index) for index in range(attempts - 1)))
        ceiling = max(1, (cap - SLICE_GUARD_SECONDS - backoffs) // attempts)
        budget["max_seconds"] = min(budget["max_seconds"], ceiling)
    return budget


async def _execute_team(session, run, node, node_run, state, deps, *, resume=False) -> str:
    if deps.gateway is None:
        await _abort_node(session, run, node_run, "llm_not_configured", "未配置可用的大模型")
        return "error"
    try:
        await ensure_daily_quota(session, run.tenant_id, deps.settings)
    except AppError as exc:
        await _abort_node(session, run, node_run, exc.code, exc.message)
        return "error"
    roles = [r for r in (node.data.get("roles") or []) if r in EXECUTION_ROLES]
    if "creator" not in roles:
        roles.append("creator")
    parent: AgentRun | None = None
    if resume and node_run.agent_run_id is not None:
        parent = await session.get(AgentRun, node_run.agent_run_id)
    if parent is None:
        goal = _agent_goal(node, state)
        parent = AgentRun(
            tenant_id=run.tenant_id,
            user_id=run.triggered_by or state.creator_id,
            goal=goal, mode="team", status="queued",
            plan={"roles": roles}, budget=_team_budget(node, deps.settings),
            policy=dict(run.policy or {}),
            workflow_run_id=run.id, workflow_node_id=node.id,
        )
        session.add(parent)
        await session.commit()
        await session.refresh(parent)
        node_run.agent_run_id = parent.id
        node_run.input = _safe_dict({"goal": goal, "roles": roles})
        node_run.status = "running"
        await session.commit()
    elif parent.status == "succeeded":
        return await _agent_succeeded(session, node_run, parent)
    elif parent.status in {"failed", "cancelled"}:
        return await _agent_failed(session, run, node_run, parent)
    elif parent.status not in {"queued", "waiting_approval"}:
        await _abort_node(session, run, node_run, "workflow_internal_error",
                          f"团队运行状态异常: {parent.status}")
        return "error"
    await run_team(
        session, parent,
        gateway=deps.gateway, tool_registry=deps.tool_registry,
        skill_registry=deps.skill_registry, storage=deps.storage,
        queue=deps.queue, settings=deps.settings,
        session_factory=deps.session_factory,
    )
    await session.refresh(parent)
    if parent.status == "succeeded":
        return await _agent_succeeded(session, node_run, parent)
    if parent.status == "waiting_approval":
        node_run.status = "waiting"
        if not await _park(session, run, _now()):
            await session.rollback()
            return "stopped"
        await session.commit()
        return "waiting"
    return await _agent_failed(session, run, node_run, parent)
```

`_execute_node` 加：

```python
    if node.kind == "team":
        return await _execute_team(session, run, node, node_run, state, deps, resume=resume)
```

注意：`run_workflow` 签名/`EngineDeps` 字段以现有代码为准（`session_factory` 已在 deps 中）；`_agent_goal` 复用变量解析。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run pytest tests/test_workflow_team_node.py tests/test_workflow_engine.py tests/test_workflow_agent_node.py -q`
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add backend/app/workflow/graph.py backend/app/workflow/validate.py backend/app/workflow/engine.py backend/tests/test_workflow_team_node.py
git commit -m "feat(workflow): add team node kind, validation and engine execution"
```

---

### Task 13: 内置「内容团队」模板

**Files:**
- Modify: `backend/app/workflow/templates.py`
- Test: `backend/tests/test_workflow_team_node.py`（追加）

**Interfaces:**
- Consumes: Task 12 校验。
- Produces: `CONTENT_TEAM` 模板并注册进模板列表（键 `content_team`）。

- [ ] **Step 1: 写失败测试**（追加）

```python
def test_content_team_template_publishable():
    from app.workflow.templates import TEMPLATES
    template = next(t for t in TEMPLATES if t.key == "content_team")
    registry = get_tool_registry()
    result = validate_publish(template.graph, registry=registry, skill_ids=set())
    assert result.ok, [issue.message for issue in result.issues]
    kinds = [n["type"] for n in template.graph["nodes"]]
    assert "team" in kinds
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_workflow_team_node.py -q -k template`
Expected: FAIL（模板不存在）。

- [ ] **Step 3: 实现**（`templates.py`，风格对齐 `WEEKLY_TOPIC`；注册进既有模板列表变量）

```python
CONTENT_TEAM_GRAPH = {
    "nodes": [
        {
            "id": "in",
            "type": "input",
            "position": {"x": 0, "y": 140},
            "data": {
                "name": "本周主题",
                "fields": [
                    {"key": "topic", "type": "string", "required": True, "maxLength": 200}
                ],
            },
        },
        {
            "id": "team",
            "type": "team",
            "position": {"x": 280, "y": 140},
            "data": {
                "name": "内容团队",
                "roles": ["scout", "creator", "critic", "publisher"],
                "goal": (
                    "围绕「{{inputs.topic}}」完成调研、创作、审稿与交付："
                    "产出一篇可发布的内容版本，并创建 draft 发布任务（不排期）。"
                ),
                "budget": {"max_tokens": 120000, "max_turns": 12},
                "retry": 0,
            },
        },
        {
            "id": "out",
            "type": "output",
            "position": {"x": 560, "y": 140},
            "data": {"name": "交付结果"},
        },
    ],
    "edges": [
        {"id": "in-team", "source": "in", "target": "team"},
        {"id": "team-out", "source": "team", "target": "out"},
    ],
}

CONTENT_TEAM = Template(
    key="content_team",
    name="内容团队",
    description="五角色团队围绕一个主题完成调研、创作、审稿与草稿交付。",
    parameters=[{"key": "topic", "label": "主题", "type": "string", "required": True}],
    graph=CONTENT_TEAM_GRAPH,
)
```

把 `CONTENT_TEAM` 加入既有模板集合（与 `WEEKLY_TOPIC` 同列表）。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run pytest tests/test_workflow_team_node.py tests/test_workflow_templates.py -q`
Expected: 全部通过（若模板测试文件名不同，用 `uv run pytest tests -q -k template`）。

- [ ] **Step 5: 提交**

```bash
git add backend/app/workflow/templates.py backend/tests/test_workflow_team_node.py
git commit -m "feat(workflow): add content team builtin template"
```

---

### Task 14: 前端类型与纯函数

**Files:**
- Create: `web/src/agentTeam.ts`
- Modify: `web/src/agent.ts`（`AgentRun` 增可选字段）
- Test: `web/src/__tests__/agentTeam.test.ts`

**Interfaces:**
- Produces（Task 15/16 消费）: 类型 `TeamRole/TeamRoleConfig/TeamChild/TeamEntry/TeamRunDetail`；函数 `defaultTeamBudget/validateParticipants/validateTeamBudget/roleLabel/entryKindLabel/canEditTeamRoles/canStartTeamRun/teamTotals`。

- [ ] **Step 1: 写失败测试** `web/src/__tests__/agentTeam.test.ts`

```ts
import { describe, expect, it } from "vitest";
import {
  canEditTeamRoles, canStartTeamRun, defaultTeamBudget, entryKindLabel,
  roleLabel, teamTotals, validateParticipants, validateTeamBudget,
  type TeamRoleConfig,
} from "../agentTeam";

function config(role: string, enabled = true): TeamRoleConfig {
  return {
    role: role as TeamRoleConfig["role"], label: role, enabled,
    tools: [], default_tools: [], budget: {}, limits: {},
  };
}

describe("team pure functions", () => {
  it("default budget matches backend", () => {
    expect(defaultTeamBudget()).toEqual({
      max_tokens: 120000, max_tool_calls: 40, max_turns: 12, max_seconds: 900,
    });
  });

  it("participants require creator and reject disabled/unknown", () => {
    const configs = [config("scout"), config("creator"), config("critic", false)];
    expect(validateParticipants(["scout", "creator"], configs)).toBeNull();
    expect(validateParticipants(["scout"], configs)).toContain("creator");
    expect(validateParticipants(["critic", "creator"], configs)).toContain("关闭");
    expect(validateParticipants(["ghost", "creator"], configs)).toContain("未知");
    expect(validateParticipants([], configs)).not.toBeNull();
  });

  it("budget validation", () => {
    expect(validateTeamBudget({})).toBeNull();
    expect(validateTeamBudget({ max_tokens: 200000 })).toBeNull();
    expect(validateTeamBudget({ max_tokens: 200001 })).not.toBeNull();
    expect(validateTeamBudget({ max_tokens: 0 })).not.toBeNull();
    expect(validateTeamBudget({ max_steps: 5 })).not.toBeNull();
  });

  it("labels and permissions", () => {
    expect(roleLabel("creator")).toBe("创作");
    expect(roleLabel("ghost")).toBe("ghost");
    expect(entryKindLabel("delegation")).toBe("委派");
    expect(canEditTeamRoles("admin")).toBe(true);
    expect(canEditTeamRoles("editor")).toBe(false);
    expect(canStartTeamRun("editor")).toBe(true);
    expect(canStartTeamRun("viewer")).toBe(false);
  });

  it("teamTotals sums children usage", () => {
    const totals = teamTotals([
      { usage: { prompt_tokens: 10, completion_tokens: 5, tool_calls: 2 } },
      { usage: { prompt_tokens: 20, completion_tokens: 10, tool_calls: 3 } },
    ] as never);
    expect(totals.tokens).toBe(45);
    expect(totals.tool_calls).toBe(5);
  });
});
```

- [ ] **Step 2: 运行确认失败**

Run: `cd web && npm test -- agentTeam`
Expected: FAIL（模块不存在）。

- [ ] **Step 3: 实现** `web/src/agentTeam.ts`

```ts
import { canWrite, isAdmin, type Role } from "./auth";
import type { AgentRun } from "./agent";

export type TeamRole = "planner" | "scout" | "creator" | "critic" | "publisher";
export const EXECUTION_ROLES: TeamRole[] = ["scout", "creator", "critic", "publisher"];

const ROLE_LABELS: Record<string, string> = {
  planner: "Lead 规划", scout: "调研", creator: "创作",
  critic: "审稿", publisher: "交付",
};
const ENTRY_LABELS: Record<string, string> = {
  goal: "目标", plan: "计划", delegation: "委派", result: "产出",
  critic: "审稿", decision: "决策", final: "交付",
};

export interface TeamRoleConfig {
  role: TeamRole; label: string; enabled: boolean;
  tools: string[]; default_tools: string[];
  budget: Record<string, number>; limits: Record<string, number>;
}
export interface TeamChild {
  id: string; role: string; status: string; attempts: number;
  usage: Record<string, number>; result_text: string; error_code: string;
  created_at: string; finished_at: string | null;
}
export interface TeamEntry {
  seq: number; kind: string; role: string;
  content: Record<string, unknown>; created_at: string;
}
export interface TeamArtifact { kind: string; id: string; title: string; role?: string; seq?: number }
export interface TeamRunDetail {
  run: AgentRun; children: TeamChild[]; entries: TeamEntry[]; artifacts: TeamArtifact[];
}

export const TEAM_BUDGET_KEYS = ["max_tokens", "max_tool_calls", "max_turns", "max_seconds"] as const;
const TEAM_CEILINGS: Record<string, number> = {
  max_tokens: 200000, max_tool_calls: 60, max_turns: 20, max_seconds: 1800,
};

export function defaultTeamBudget(): Record<string, number> {
  return { max_tokens: 120000, max_tool_calls: 40, max_turns: 12, max_seconds: 900 };
}
export function roleLabel(role: string): string { return ROLE_LABELS[role] ?? role; }
export function entryKindLabel(kind: string): string { return ENTRY_LABELS[kind] ?? kind; }
export function canEditTeamRoles(role: Role): boolean { return isAdmin(role); }
export function canStartTeamRun(role: Role): boolean { return canWrite(role); }

export function validateParticipants(selected: string[], configs: TeamRoleConfig[]): string | null {
  if (selected.length === 0) return "至少选择一个执行角色";
  for (const role of selected) {
    if (!EXECUTION_ROLES.includes(role as TeamRole)) return `未知角色：${role}`;
    const config = configs.find((item) => item.role === role);
    if (config && !config.enabled) return `角色 ${roleLabel(role)} 已被关闭`;
  }
  if (!selected.includes("creator")) return "团队必须包含创作（creator）";
  return null;
}

export function validateTeamBudget(raw: Record<string, unknown>): string | null {
  for (const [key, value] of Object.entries(raw)) {
    if (!TEAM_BUDGET_KEYS.includes(key as (typeof TEAM_BUDGET_KEYS)[number]))
      return `未知预算维度：${key}`;
    if (typeof value !== "number" || !Number.isInteger(value) || value <= 0)
      return `预算 ${key} 必须是正整数`;
    if (value > TEAM_CEILINGS[key]) return `预算 ${key} 超过上限`;
  }
  return null;
}

export function teamTotals(children: TeamChild[]): { tokens: number; tool_calls: number } {
  let tokens = 0;
  let toolCalls = 0;
  for (const child of children) {
    const usage = child.usage ?? {};
    tokens += (Number(usage.prompt_tokens) || 0) + (Number(usage.completion_tokens) || 0);
    toolCalls += Number(usage.tool_calls) || 0;
  }
  return { tokens, tool_calls: toolCalls };
}
```

`agent.ts` 的 `AgentRun` 接口追加：

```ts
  role?: string;
  parent_run_id?: string | null;
```

- [ ] **Step 4: 运行确认通过**

Run: `cd web && npm test -- agentTeam`
Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add web/src/agentTeam.ts web/src/agent.ts web/src/__tests__/agentTeam.test.ts
git commit -m "feat(web): add team types and pure validation functions"
```

---

### Task 15: 助理页团队入口与团队运行详情页

**Files:**
- Modify: `web/src/pages/Assistant.tsx`、`web/src/App.tsx`
- Create: `web/src/pages/TeamRunDetail.tsx`

**Interfaces:**
- Consumes: Task 14 全部；`api`、`useAuth`、`agentStatusLabel/artifactLink/usageTokens/isRunTerminal`（`agent.ts`）。
- Produces: `/assistant` 团队页签与发起面板；`/assistant/teams/:id` 详情（2 秒轮询）。

- [ ] **Step 1: 实现助理页团队页签**（`Assistant.tsx`）

在现有单 Agent 表单之外新增页签状态与团队面板（保持既有单 Agent 逻辑不动）：

```tsx
const [tab, setTab] = useState<"single" | "team">("single");
const [teamGoal, setTeamGoal] = useState("");
const [teamRoles, setTeamRoles] = useState<string[]>(["scout", "creator", "critic", "publisher"]);
const [teamBudget, setTeamBudget] = useState<Record<string, number>>(defaultTeamBudget());
const [teamError, setTeamError] = useState("");
const roleConfigs = useQuery({
  queryKey: ["team-roles"],
  queryFn: () => api<TeamRoleConfig[]>("/agent/team/roles"),
});
const createTeam = useMutation({
  mutationFn: () => api<AgentRun>("/agent/team/runs", {
    method: "POST",
    body: JSON.stringify({ goal: teamGoal, roles: teamRoles, budget: teamBudget }),
  }),
  onSuccess: () => {
    setTeamGoal(""); setTeamError("");
    queryClient.invalidateQueries({ queryKey: ["agent-runs"] });
  },
  onError: (err) => setTeamError(errorText(err)),
});
```

页签切换按钮（「单 Agent」/「协作团队」）；团队面板渲染：目标 textarea、角色勾选（`creator` 固定选中 disabled、`planner` 显示为锁定 Lead 文案）、可展开预算四个数字输入、提交按钮（`canStartTeamRun(me.role)` 才可点）。提交前调用 `validateParticipants`/`validateTeamBudget`，错误写入 `teamError`。运行列表对 `mode === "team"` 的行显示「协作团队」徽标，点击跳转 `/assistant/teams/{id}`（单 Agent 仍走原详情）。

- [ ] **Step 2: 实现详情页** `web/src/pages/TeamRunDetail.tsx`

```tsx
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { agentStatusLabel, artifactLink, isRunTerminal, usageTokens } from "../agent";
import { entryKindLabel, roleLabel, teamTotals, type TeamRunDetail } from "../agentTeam";
import { api } from "../client";

export default function TeamRunDetailPage() {
  const { id } = useParams();
  const detail = useQuery({
    queryKey: ["team-run", id],
    queryFn: () => api<TeamRunDetail>(`/agent/team/runs/${id}`),
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data) return 2000;
      return isRunTerminal(data.run.status) ? false : 2000;
    },
  });
  if (detail.isLoading) return <p className="text-sm text-ink-500">加载中…</p>;
  if (detail.isError || !detail.data) return <p className="text-sm text-wine-700">加载失败</p>;
  const { run, children, entries, artifacts } = detail.data;
  const totals = teamTotals(children);
  return (
    <div className="space-y-4">
      <header className="card card-pad space-y-2">
        <div className="flex items-center gap-2">
          <h1 className="text-lg font-semibold">协作团队</h1>
          <span className={agentStatusBadge(run.status)}>{agentStatusLabel(run.status)}</span>
        </div>
        <p className="text-sm text-ink-700 whitespace-pre-wrap">{run.goal}</p>
        <p className="text-xs text-ink-500">
          子运行 token {totals.tokens} · 工具调用 {totals.tool_calls} ·
          Lead token {usageTokens(run.usage ?? {})}
        </p>
        {run.error_code && (
          <p className="text-sm text-wine-700">{run.error_code}：{run.error_message}</p>
        )}
      </header>

      <section className="card card-pad space-y-2">
        <h2 className="text-sm font-semibold">角色时间线</h2>
        {children.length === 0 && <p className="text-xs text-ink-500">暂无角色执行</p>}
        {children.map((child) => (
          <div key={child.id} className="rounded-lg border border-line p-3 space-y-1">
            <div className="flex items-center gap-2 text-sm">
              <span className="font-medium">{roleLabel(child.role)}</span>
              <span className={agentStatusBadge(child.status)}>{agentStatusLabel(child.status)}</span>
              <span className="text-xs text-ink-500">尝试 {child.attempts}</span>
            </div>
            {child.result_text && <p className="text-xs text-ink-700">{child.result_text}</p>}
            {child.error_code && <p className="text-xs text-wine-700">{child.error_code}</p>}
          </div>
        ))}
      </section>

      <section className="card card-pad space-y-2">
        <h2 className="text-sm font-semibold">黑板</h2>
        {entries.map((entry) => (
          <div key={entry.seq} className="rounded-lg border border-line p-3 space-y-1">
            <div className="flex items-center gap-2 text-xs text-ink-500">
              <span>#{entry.seq}</span>
              <span>{entryKindLabel(entry.kind)}</span>
              {entry.role && <span>{roleLabel(entry.role)}</span>}
            </div>
            <pre className="text-xs text-ink-700 whitespace-pre-wrap break-all">
              {JSON.stringify(entry.content, null, 2)}
            </pre>
          </div>
        ))}
      </section>

      <section className="card card-pad space-y-2">
        <h2 className="text-sm font-semibold">产物</h2>
        {artifacts.length === 0 && <p className="text-xs text-ink-500">暂无产物</p>}
        {artifacts.map((artifact, index) => {
          const link = artifactLink({ type: artifact.kind, id: artifact.id, title: artifact.title });
          return link ? (
            <Link key={index} to={link} className="block text-sm text-sage-700 underline">
              {artifact.title || artifact.id}
            </Link>
          ) : (
            <p key={index} className="text-sm text-ink-700">{artifact.title || artifact.id}</p>
          );
        })}
      </section>
    </div>
  );
}
```

注意：`agentStatusBadge` 若未在 `agent.ts` 导出，则复用 `workflows.ts` 的 `runStatusBadge`；`artifactLink` 的入参形状以 `agent.ts` 现有定义为准（`Artifact` 类型），实现时对齐。取消按钮：`canCancelRun` 口径复用单 Agent（发起人或 admin），调用 `POST /agent/team/runs/{id}/cancel` 后 invalidate。

- [ ] **Step 3: 路由**（`App.tsx`）

在 `/assistant` 路由附近新增（editor 守卫与 `/assistant` 一致）：

```tsx
<Route path="/assistant/teams/:id" element={<TeamRunDetailPage />} />
```

并 `import TeamRunDetailPage from "./pages/TeamRunDetail";`。

- [ ] **Step 4: 验证**

Run: `cd web && npm test && npm run build`
Expected: 测试通过、构建成功。随后启动前后端，用 editor 账号在 `/assistant` 切到「协作团队」发起一次运行（可用脚本化/真实模型），确认列表徽标、详情页时间线/黑板/产物渲染、2 秒轮询与取消按钮。若无法起真实模型，至少验证发起校验错误提示与详情页空态。

- [ ] **Step 5: 提交**

```bash
git add web/src/pages/Assistant.tsx web/src/pages/TeamRunDetail.tsx web/src/App.tsx
git commit -m "feat(web): add team launch tab and team run detail page"
```

---

### Task 16: 角色配置页与画布团队节点

**Files:**
- Create: `web/src/pages/TeamRoles.tsx`
- Modify: `web/src/pages/Settings.tsx`、`web/src/App.tsx`、`web/src/workflows.ts`、`web/src/components/workflow/NodePalette.tsx`、`NodeInspector.tsx`、`WorkflowCanvas.tsx`、`web/src/pages/WorkflowRunDetail.tsx`

**Interfaces:**
- Consumes: Task 14；`/agent/team/roles` GET/PUT。
- Produces: `/settings/team-roles` 配置页；画布 `team` 节点（面板/检查器/视图映射/默认值/标签）；运行详情团队节点链接。

- [ ] **Step 1: 角色配置页** `web/src/pages/TeamRoles.tsx`

admin/owner 页面：`useQuery(["team-roles"])` 拉取；每角色一张卡片：启停开关（`planner`/`creator` disabled）、工具勾选（仅 `default_tools` 内可取消，不能新增）、四个预算数字输入（上限 `limits`）。保存 `PUT /agent/team/roles`，body `{roles:[{role,enabled,tools,budget}]}`；失败展示 `errorText(err)`（含后端 `policy_not_loosenable` 提示）。非 admin 访问显示无权限文案。

- [ ] **Step 2: 入口与路由**

`Settings.tsx` 增加链接卡片 `<Link to="/settings/team-roles">协作团队角色配置</Link>`（仅 admin 可见，页面本身已 admin 守卫）。`App.tsx` 新增：

```tsx
<Route path="/settings/team-roles" element={<TeamRoles />} />
```

放在 admin 守卫路由组内（与 `/settings` 同组）。

- [ ] **Step 3: 画布支持**

`workflows.ts`：
- `NODE_KIND_LABELS` 增 `team: "团队"`。
- `NODE_PREFIXES` 增 `team: "tm"`。
- `nodeDefaults` 增：

```ts
    case "team":
      return {
        label: "团队",
        roles: ["scout", "creator", "critic", "publisher"],
        goal: "",
        budget: { max_tokens: 120000 },
        retry: 0,
      };
```

`NodePalette.tsx`：`PALETTE` 在 agent 之后插入 `{ type: "team", hint: "多角色协作产出内容" }`。

`WorkflowCanvas.tsx`：`nodeTypes` 增 `team: WorkflowNodeView`。

`NodeInspector.tsx`：新增 `node.type === "team"` 区块（放在 agent 区块后）：目标 `TextAreaWithVariables`；角色勾选（`EXECUTION_ROLES`，creator 固定选中）；预算四输入（`max_tokens/max_tool_calls/max_turns/max_seconds`）。重试下拉的既有条件 `(node.type === "tool" || node.type === "agent")` 扩为含 `"team"`。

`WorkflowRunDetail.tsx`：节点卡片对 `type === "team"` 且存在 `agent_run_id` 时渲染链接 `/assistant/teams/{agent_run_id}`（文案「查看团队运行」）；`waiting` 视觉与 agent 节点一致（复用既有状态徽标）。

- [ ] **Step 4: 验证**

Run: `cd web && npm test && npm run build`
Expected: 通过。浏览器验收：admin 打开 `/settings/team-roles` 收窄 scout 工具并保存成功、尝试新增工具报错；画布添加团队节点、配置角色/目标/预算、保存并发布「内容团队」模板工作流；运行详情团队节点显示链接与 waiting 态。

- [ ] **Step 5: 提交**

```bash
git add web/src/pages/TeamRoles.tsx web/src/pages/Settings.tsx web/src/App.tsx web/src/workflows.ts web/src/components/workflow/NodePalette.tsx web/src/components/workflow/NodeInspector.tsx web/src/components/workflow/WorkflowCanvas.tsx web/src/pages/WorkflowRunDetail.tsx
git commit -m "feat(web): add team role settings and canvas team node"
```

---

### Task 17: 文档、真实模型 E2E 与全量验证

**Files:**
- Create: `backend/tests/test_team_e2e.py`
- Modify: `README.md`、`docs/superpowers/specs/2026-11-13-ai-native-automation-design.md`、`docs/superpowers/specs/2026-11-18-multi-agent-team-design.md`（Status 更新）

**Interfaces:**
- Consumes: 全部已交付能力。
- Produces: `RUN_TEAM_E2E=1` 门控的真实模型用例；文档与验收记录。

- [ ] **Step 1: 写 E2E 测试** `backend/tests/test_team_e2e.py`

```python
import os
import uuid

import pytest

from app.agent.registry import get_tool_registry
from app.agent.team import run_team
from app.config import get_settings
from app.llm.gateway import load_gateway_default
from app.models.agent import AgentRun
from app.models.base import DEFAULT_TENANT_ID
from app.skills.registry import get_registry
from app.storage import get_storage

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_TEAM_E2E") != "1",
    reason="真实模型团队 E2E 需 RUN_TEAM_E2E=1",
)


@pytest.mark.asyncio
async def test_team_real_model(db_session):
    settings = get_settings()
    gateway = await load_gateway_default(db_session, DEFAULT_TENANT_ID)
    run = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=uuid.uuid4(),
        goal="围绕「秋季护肤」产出一篇内容版本，并创建 draft 发布任务（不排期）",
        mode="team", status="queued",
        plan={"roles": ["scout", "creator", "critic", "publisher"]},
        budget={"max_tokens": 120000, "max_tool_calls": 40,
                "max_turns": 12, "max_seconds": 900},
    )
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)
    await run_team(
        db_session, run, gateway=gateway, tool_registry=get_tool_registry(),
        skill_registry=get_registry(), storage=get_storage(), queue=None,
        settings=settings,
    )
    await db_session.refresh(run)
    assert run.status in {"succeeded", "failed"}
    if run.status == "succeeded":
        assert run.result.get("summary")
```

- [ ] **Step 2: 默认跳过验证**

Run: `cd backend && uv run pytest tests/test_team_e2e.py -q`
Expected: 1 skipped。

- [ ] **Step 3: 文档更新**

`README.md`：在「工作流（第 2 层）」小节后新增「协作团队（第 3 层）」小节，描述五角色、黑板、Critic 两轮返工、团队预算、审批唤醒、工作流 team 节点与「内容团队」模板、双入口与权限矩阵；注明真发布/排期不可达、`RUN_TEAM_E2E=1` 门控。

`2026-11-13-ai-native-automation-design.md`：第 3 层状态改为「已实现并通过验证」，链接本计划与验收结果。

`2026-11-18-multi-agent-team-design.md`：`Status` 改为「已实现」。

- [ ] **Step 4: 全量验证**

```bash
cd backend && uv run pytest -q && uv run ruff check .
cd ../web && npm test && npm run build
```

Expected: 后端全量通过（基线 1114 passed / 4 skipped + 新增团队用例）、ruff 无告警、前端测试与构建通过。

- [ ] **Step 5: 真实环境浏览器验收**

启动后端/worker/前端与既有 `newmedia-db-1`/`newmedia-redis-1` 容器（不新建/不删除容器与卷）。用 editor.accept 发起团队运行、admin 审批子运行、验证 Critic 返工与失败保留现场、工作流「内容团队」模板手动+定时运行产出 draft 发布任务（`scheduled_at` 为空）、viewer 只读与双租户隔离。记录结果到本计划验收小节或 README。不输出任何真实凭据。

- [ ] **Step 6: 提交**

```bash
git add backend/tests/test_team_e2e.py README.md docs/superpowers/specs/2026-11-13-ai-native-automation-design.md docs/superpowers/specs/2026-11-18-multi-agent-team-design.md
git commit -m "docs(team): document layer 3 delivery and add gated real-model e2e"
```

---

## Self-Review 记录

- **规格覆盖**：§2.1 条目 1→Task 1/3；2→Task 1；3→Task 4/5/6；4→Task 8/9；5→Task 10/11；6→Task 12/13/16；7→Task 14/15/16；8→Task 17。§3 角色表逐字进 Task 1；§4 数据模型进 Task 1；§5 编排进 Task 4/5/6；§6 预算进 Task 3/5；§7 审批/对账进 Task 8/9；§8 权限进 Task 10/15/16；§9 API 进 Task 10；§10 worker/工作流进 Task 11/12/13；§11 前端进 Task 14/15/16；§12 配置进 Task 1；§13 测试矩阵分布到各任务；§14 验收进 Task 17。
- **占位符扫描**：Task 5 `_budget_of` 与 Task 9 测试中的两处笔误占位已在正文给出正确写法，实现时按更正版本落地；Task 12 引擎测试的夹具对齐说明指向既有 `test_workflow_agent_node.py`，非 TBD。
- **类型一致性**：`run_team` 签名、`EffectiveRole`、`LeadDecision`、`CriticVerdict`、`team_state` 键、错误码常量在 Task 5–13 间一致；前端 `TeamRunDetail` 与后端 `GET /agent/team/runs/{id}` 返回结构对齐（`run/children/entries/artifacts`）。

---

## 验收记录（Task 17）

环境：真实后端 uvicorn :8000 + arq worker + vite :5173，既有容器 `newmedia-db-1`/`newmedia-redis-1`（未新建/删除任何容器与卷）。验收账号经临时脚本 `backend/scratch_accept_users.py` 在 dev 库新建：team.editor / team.admin / team.owner / team.viewer@example.com（默认租户，`must_change_password=False`、`is_active=True`，随机密码）。凭据已按口径生成并销毁，未写入任何报告/提交/截图/日志；既有用户未改动。截图统一存放于 `.superpowers/sdd/2026-11-18-multi-agent-team/screenshots/t17/`（git-ignored，不提交）。

### 场景 1：editor 发起团队运行 — 通过

team.editor 登录后在助理页「协作团队」页签发起团队运行，面板展示角色与预算配置，运行创建成功并进入团队详情页。

- `01-editor-team-launch-panel.png`：editor 发起面板。

### 场景 2：子运行审批（通过/驳回各一次）与父团队唤醒续跑 — 通过

子运行命中审批时，team.admin 在团队详情页审批卡看到「团队 · 角色」标签并作出决策；通过后父团队被唤醒续跑；驳回后子运行失败且父团队保留失败现场。助理页审批卡带团队上下文链接可跳回团队详情。

- `04-editor-approval-card-team-role.png`：审批卡（团队 · 角色标签）。
- `05-admin-approval-card-decide.png`：admin 决策。
- `06-team-resumed-after-approval.png`：审批通过后父团队续跑。
- `07-reject-failure-scene.png`：驳回后失败现场。
- `08-assistant-approval-team-context-link.png`：助理页审批卡团队上下文链接。

### 场景 3：Critic 返工与失败保留现场 — 通过

真实运行观察到失败后团队详情页保留现场（状态、错误、黑板条目不丢失）；Critic 强制审稿与返工上限由脚本化用例覆盖（`tests/test_team_orchestrator.py` 等，全量验证通过）。

- `02-team-detail-failure-scene.png`：失败保留现场。
- `03-team-blackboard-preserved.png`：黑板保留。

### 场景 4：工作流「内容团队」模板手动 + 定时运行 — 通过

工作流画布展示团队节点模板；配置定时 07:46 与手动运行各验一次，运行成功并产出团队产物与 draft 发布任务（`scheduled_at` 为空）。

- `09-workflow-team-template-canvas.png`：团队模板画布。
- `10-workflow-schedule-0746.png`：定时 07:46。
- `11-workflow-manual-run-succeeded.png`：手动运行成功。
- `12-team-succeeded-artifacts.png`：团队成功产物。
- `13-team-artifacts-draft-job.png`：draft 发布任务。

### 场景 5：viewer 只读与双租户隔离 — 通过

team.viewer 浏览器实测：读接口 200、写接口 403，页面无发起/审批/取消入口。双租户隔离以既有自动化测试为证据（续作时实跑一次，6 passed in 0.28s）：

```
uv run pytest tests/test_team_api.py::test_detail_and_cancel_and_isolation \
  tests/test_team_api.py::test_detail_excludes_foreign_tenant_rows \
  tests/test_agent_api.py::test_viewer_cannot_create_run \
  tests/test_agent_api.py::test_viewer_cannot_cancel_own_run \
  tests/test_permissions.py::test_viewer_can_read_but_not_write \
  tests/test_workflow_api.py::test_role_matrix_blocks_editor_and_viewer_writes -q
→ 6 passed
```

- `14-viewer-no-page-entry.png`：viewer 无入口。

### 场景 6：结果记录 — 完成

本节即验收记录（同内容见 `.superpowers/sdd/2026-11-18-multi-agent-team/task-17-report.md`）；全程未输出任何真实凭据。