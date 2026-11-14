import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.agent.contracts import ToolResult, ToolSpec
from app.agent.registry import ToolRegistry
from app.agent.runtime import (
    INTERRUPTED_OBSERVATION,
    decide_approval,
    expire_approvals,
    run_agent,
)
from app.config import get_settings
from app.errors import AppError
from app.llm.tools import ToolCall, ToolTurn
from app.llm.types import Usage
from app.models.agent import AgentApproval, AgentRun, AgentStep
from app.models.user import User
from app.skills.registry import SkillRegistry
from app.storage import get_storage

TENANT_A = "aaaaaaaa-1111-1111-1111-111111111111"
BUDGET = {
    "max_steps": 10,
    "max_tokens": 10000,
    "max_tool_calls": 10,
    "max_seconds": 120,
}


class FakeGateway:
    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []

    async def complete_tool_calls(self, messages, *, tools):
        self.calls.append(json.loads(json.dumps(messages)))
        return self.turns.pop(0)


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


def tool(name, handler, risk="read"):
    return ToolSpec(
        name=name,
        description="测试工具",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "additionalProperties": False,
        },
        risk=risk,
        handler=handler,
    )


def deps(turns, specs):
    registry = ToolRegistry()
    registry.register_all(specs)
    return {
        "gateway": FakeGateway(turns),
        "tool_registry": registry,
        "skill_registry": SkillRegistry(get_settings().skills_dir),
        "storage": get_storage(),
        "queue": None,
        "settings": get_settings(),
        "session_factory": None,
    }


async def make_user(
    db_session,
    *,
    role="admin",
    tenant_id=TENANT_A,
    user_id=None,
    email=None,
):
    user = User(
        id=user_id or uuid.uuid4(),
        tenant_id=tenant_id,
        email=email or f"{uuid.uuid4().hex[:10]}@example.com",
        password_hash="x",
        display_name="审批人",
        role=role,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def new_run(db_session, *, user_id=None, **overrides):
    values = {
        "tenant_id": TENANT_A,
        "user_id": user_id or uuid.uuid4(),
        "goal": "创建一条草稿",
        "budget": dict(BUDGET),
        "policy": {},
    }
    values.update(overrides)
    run = AgentRun(**values)
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)
    return run


async def steps_of(db_session, run):
    return (
        await db_session.execute(
            select(AgentStep)
            .where(AgentStep.run_id == run.id)
            .order_by(AgentStep.step_index)
        )
    ).scalars().all()


def draft_specs(calls):
    async def handler(ctx, args):
        calls.append(args)
        return ToolResult(
            summary="已创建草稿",
            data={"job_id": "j1"},
            artifacts=[{"type": "publish_job", "id": "j1"}],
        )

    return handler, [tool("demo.high", handler, risk="high")]


async def pause_run(db_session, run, calls):
    """触发一次 confirm 挂起，返回 (run, approval, specs, handler)。"""
    handler, specs = draft_specs(calls)
    turns = [turn("", [("c1", "demo.high", {"text": "x"})])]
    await run_agent(db_session, run, **deps(turns, specs))
    await db_session.refresh(run)
    approval = (
        await db_session.execute(
            select(AgentApproval).where(AgentApproval.run_id == run.id)
        )
    ).scalars().one()
    return run, approval, specs, handler


async def test_confirm_tool_pauses_and_skips_remaining_calls(db_session):
    executions = []

    async def high(ctx, args):
        executions.append("high")
        return ToolResult(summary="不应执行")

    async def read(ctx, args):
        executions.append("read")
        return ToolResult(summary="不应执行")

    specs = [tool("demo.high", high, risk="high"), tool("demo.read", read)]
    turns = [
        turn(
            "",
            [
                ("c1", "demo.high", {"text": "x"}),
                ("c2", "demo.read", {}),
            ],
        )
    ]
    run = await new_run(db_session)
    await run_agent(db_session, run, **deps(turns, specs))
    await db_session.refresh(run)
    steps = await steps_of(db_session, run)
    approval = (
        await db_session.execute(
            select(AgentApproval).where(AgentApproval.run_id == run.id)
        )
    ).scalars().one()

    assert run.status == "waiting_approval"
    assert executions == []
    assert [step.kind for step in steps] == ["llm", "approval"]
    assert steps[1].status == "skipped"
    assert steps[1].tool_name == "demo.high"
    assert steps[1].input == {"text": "x"}
    assert approval.status == "pending"
    assert approval.tool_name == "demo.high"
    assert approval.payload == {"text": "x"}
    assert approval.expires_at is not None
    assert run.messages["pending_call"] == {
        "call_id": "c1",
        "name": "demo.high",
        "arguments": json.dumps({"text": "x"}),
        "step_index": 2,
    }
    tool_messages = [
        item for item in run.messages["items"] if item["role"] == "tool"
    ]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "c2"
    assert "未执行" in tool_messages[0]["content"]


async def test_waiting_run_is_not_executed_again(db_session):
    creator = await make_user(db_session, role="editor")
    run = await new_run(db_session, user_id=creator.id)
    run, approval, specs, _ = await pause_run(db_session, run, [])

    environment = deps([turn("不该被调用")], specs)
    await run_agent(db_session, run, **environment)
    await db_session.refresh(run)

    assert run.status == "waiting_approval"
    assert approval.status == "pending"
    assert environment["gateway"].calls == []


async def test_approved_run_resumes_and_executes_once(db_session):
    creator = await make_user(db_session, role="editor")
    run = await new_run(db_session, user_id=creator.id)
    calls = []
    run, approval, specs, _ = await pause_run(db_session, run, calls)
    admin = await make_user(db_session, role="admin")

    updated = await decide_approval(
        db_session, approval, actor=admin, decision="approve"
    )
    assert updated.status == "queued"
    assert approval.status == "approved"
    assert approval.decided_by == admin.id
    assert approval.decided_at is not None

    await run_agent(db_session, run, **deps([turn("草稿已创建")], specs))
    await db_session.refresh(run)
    steps = await steps_of(db_session, run)

    assert run.status == "succeeded"
    assert run.result_text == "草稿已创建"
    assert calls == [{"text": "x"}]
    assert "pending_call" not in run.messages
    assert run.result["artifacts"] == [{"type": "publish_job", "id": "j1"}]
    assert steps[1].kind == "approval"
    assert steps[1].status == "ok"
    tool_messages = [
        item for item in run.messages["items"] if item["role"] == "tool"
    ]
    assert tool_messages[0]["tool_call_id"] == "c1"
    assert tool_messages[0]["content"].startswith("以下为系统数据")
    assert "已创建草稿" in tool_messages[0]["content"]


async def test_rejected_run_fails_and_closes_step(db_session):
    creator = await make_user(db_session, role="editor")
    run = await new_run(db_session, user_id=creator.id)
    run, approval, _, _ = await pause_run(db_session, run, [])
    admin = await make_user(db_session, role="owner")

    await decide_approval(
        db_session,
        approval,
        actor=admin,
        decision="reject",
        note="不符合品牌调性",
    )
    await db_session.refresh(run)
    steps = await steps_of(db_session, run)

    assert run.status == "failed"
    assert run.error_code == "approval_rejected"
    assert run.error_message == "审批已驳回"
    assert run.finished_at is not None
    assert approval.status == "rejected"
    assert approval.note == "不符合品牌调性"
    assert "pending_call" not in run.messages
    assert steps[1].status == "denied"
    assert steps[1].error == "审批已驳回"


async def test_second_decision_conflicts(db_session):
    creator = await make_user(db_session, role="editor")
    _run, approval, _, _ = await pause_run(
        db_session, await new_run(db_session, user_id=creator.id), []
    )
    admin = await make_user(db_session, role="admin")
    await decide_approval(db_session, approval, actor=admin, decision="approve")

    with pytest.raises(AppError) as exc:
        await decide_approval(
            db_session, approval, actor=admin, decision="reject"
        )

    assert exc.value.code == "agent_approval_not_pending"
    assert exc.value.status == 409


async def test_creator_cannot_approve_own_run(db_session):
    owner = await make_user(db_session, role="owner")
    _run, approval, _, _ = await pause_run(
        db_session, await new_run(db_session, user_id=owner.id), []
    )

    with pytest.raises(AppError) as exc:
        await decide_approval(
            db_session, approval, actor=owner, decision="approve"
        )

    assert exc.value.code == "forbidden"
    assert exc.value.status == 403


async def test_editor_cannot_approve(db_session):
    creator = await make_user(db_session, role="editor")
    _run, approval, _, _ = await pause_run(
        db_session, await new_run(db_session, user_id=creator.id), []
    )
    editor = await make_user(db_session, role="editor")

    with pytest.raises(AppError) as exc:
        await decide_approval(
            db_session, approval, actor=editor, decision="approve"
        )

    assert exc.value.code == "forbidden"


async def test_expire_approvals_fails_run(db_session):
    creator = await make_user(db_session, role="editor")
    run, approval, _, _ = await pause_run(
        db_session, await new_run(db_session, user_id=creator.id), []
    )
    approval.expires_at = datetime.now(UTC) - timedelta(minutes=5)
    await db_session.commit()

    count = await expire_approvals(db_session, tenant_id=TENANT_A)
    await db_session.refresh(run)
    await db_session.refresh(approval)
    steps = await steps_of(db_session, run)

    assert count == 1
    assert approval.status == "expired"
    assert run.status == "failed"
    assert run.error_code == "approval_expired"
    assert run.error_message == "审批已过期，运行已结束"
    assert steps[1].status == "denied"
    assert "pending_call" not in run.messages


async def test_resume_reuses_already_finished_step(db_session):
    creator = await make_user(db_session, role="editor")
    calls = []
    run, approval, specs, _ = await pause_run(
        db_session, await new_run(db_session, user_id=creator.id), calls
    )
    admin = await make_user(db_session, role="admin")
    await decide_approval(db_session, approval, actor=admin, decision="approve")

    steps = await steps_of(db_session, run)
    step = steps[1]
    step.status = "ok"
    step.output = {
        "summary": "已创建草稿",
        "data": {"job_id": "j1"},
        "artifacts": [{"type": "publish_job", "id": "j1"}],
    }
    await db_session.commit()

    await run_agent(db_session, run, **deps([turn("草稿已创建")], specs))
    await db_session.refresh(run)

    assert calls == []
    assert run.status == "succeeded"
    assert run.result["artifacts"] == [{"type": "publish_job", "id": "j1"}]
    assert "pending_call" not in run.messages
    tool_messages = [
        item for item in run.messages["items"] if item["role"] == "tool"
    ]
    assert "已创建草稿" in tool_messages[0]["content"]


async def test_pause_does_not_spend_tool_budget(db_session):
    creator = await make_user(db_session, role="editor")
    calls = []
    run, approval, specs, _ = await pause_run(
        db_session, await new_run(db_session, user_id=creator.id), calls
    )

    # A parked call has not run yet, so it must not consume the tool budget.
    assert run.usage["tool_calls"] == 0

    admin = await make_user(db_session, role="admin")
    await decide_approval(db_session, approval, actor=admin, decision="approve")
    await run_agent(db_session, run, **deps([turn("完成")], specs))
    await db_session.refresh(run)

    assert calls == [{"text": "x"}]
    assert run.usage["tool_calls"] == 1


TENANT_B = "bbbbbbbb-2222-2222-2222-222222222222"


async def test_cross_tenant_decision_is_not_found(db_session):
    creator = await make_user(db_session, role="editor")
    _run, approval, _, _ = await pause_run(
        db_session, await new_run(db_session, user_id=creator.id), []
    )
    outsider = await make_user(db_session, role="admin", tenant_id=TENANT_B)

    with pytest.raises(AppError) as exc:
        await decide_approval(
            db_session, approval, actor=outsider, decision="approve"
        )

    assert exc.value.code == "agent_run_not_found"
    assert exc.value.status == 404
    await db_session.refresh(approval)
    assert approval.status == "pending"


async def test_expire_approvals_is_tenant_scoped(db_session):
    creator = await make_user(db_session, role="editor")
    run, approval, _, _ = await pause_run(
        db_session, await new_run(db_session, user_id=creator.id), []
    )
    approval.expires_at = datetime.now(UTC) - timedelta(minutes=5)
    await db_session.commit()

    assert await expire_approvals(db_session, tenant_id=TENANT_B) == 0
    await db_session.refresh(approval)
    assert approval.status == "pending"

    assert await expire_approvals(db_session) == 1
    await db_session.refresh(approval)
    await db_session.refresh(run)
    assert approval.status == "expired"
    assert run.status == "failed"
    assert run.error_code == "approval_expired"


async def test_interrupted_approved_call_is_not_replayed(db_session):
    creator = await make_user(db_session, role="editor")
    calls = []
    run, approval, specs, _ = await pause_run(
        db_session, await new_run(db_session, user_id=creator.id), calls
    )
    admin = await make_user(db_session, role="admin")
    await decide_approval(db_session, approval, actor=admin, decision="approve")

    steps = await steps_of(db_session, run)
    steps[1].status = "interrupted"
    steps[1].error = INTERRUPTED_OBSERVATION
    await db_session.commit()

    await run_agent(db_session, run, **deps([turn("已确认，未重复执行。")], specs))
    await db_session.refresh(run)

    assert calls == []
    assert run.status == "succeeded"
    assert "pending_call" not in run.messages
    tool_messages = [
        item for item in run.messages["items"] if item["role"] == "tool"
    ]
    assert INTERRUPTED_OBSERVATION in tool_messages[0]["content"]
