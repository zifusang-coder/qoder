import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.agent.contracts import ToolResult, ToolSpec
from app.agent.registry import ToolRegistry
from app.agent.runtime import (
    INTERRUPTED_OBSERVATION,
    SYSTEM_PROMPT,
    _persist,
    run_agent,
)
from app.agent.sanitize import COMPRESSED_PREFIX, MAX_MESSAGES_BYTES
from app.config import get_settings
from app.errors import AppError
from app.llm.tools import ToolCall, ToolTurn
from app.llm.types import Usage
from app.models.agent import AgentRun, AgentStep
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
        # 深拷贝，避免运行时后续修改影响断言
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


def tool(name, handler, risk="read", timeout=60.0, schema=None):
    return ToolSpec(
        name=name,
        description="测试工具",
        input_schema=schema
        or {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "additionalProperties": False,
        },
        risk=risk,
        handler=handler,
        timeout_seconds=timeout,
    )


def body_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode())


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


async def new_run(db_session, *, policy=None, budget=None, **overrides):
    values = {
        "tenant_id": TENANT_A,
        "user_id": uuid.uuid4(),
        "goal": "写一篇新文章并创建草稿",
        "budget": dict(budget or BUDGET),
        "policy": dict(policy or {}),
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


async def test_multi_step_run_succeeds(db_session):
    seen = []

    async def read_handler(ctx, args):
        await asyncio.sleep(0.02)
        seen.append((args["text"], ctx.run_id, ctx.step_index))
        return ToolResult(
            summary="找到画像",
            data={"items": [{"id": "p1"}]},
            artifacts=[{"type": "profile", "id": "p1"}],
        )

    async def write_handler(ctx, args):
        return ToolResult(
            summary="已建项目",
            data={"project_id": "pr1"},
            artifacts=[{"type": "project", "id": "pr1"}],
        )

    turns = [
        turn("计划：先看画像，再建项目。", [("c1", "demo.read", {"text": "a"})]),
        turn("", [("c2", "demo.write", {"text": "b"})]),
        turn("已完成两个步骤。"),
    ]
    run = await new_run(db_session, policy={"demo.read": "auto", "demo.write": "auto"})
    await run_agent(
        db_session,
        run,
        **deps(turns, [tool("demo.read", read_handler), tool("demo.write", write_handler, risk="low")]),
    )
    await db_session.refresh(run)
    steps = await steps_of(db_session, run)

    assert run.status == "succeeded"
    assert run.result_text == "已完成两个步骤。"
    assert run.plan["summary"].startswith("计划")
    assert run.result["artifacts"] == [
        {"type": "profile", "id": "p1"},
        {"type": "project", "id": "pr1"},
    ]
    assert [step.kind for step in steps] == ["llm", "tool", "llm", "tool", "llm"]
    assert steps[1].status == "ok"
    assert steps[1].output["summary"] == "找到画像"
    assert steps[1].started_at is not None
    assert steps[1].finished_at is not None
    assert steps[1].duration_ms >= 10
    assert run.usage["prompt_tokens"] == 30
    assert run.usage["completion_tokens"] == 15
    assert run.usage["tool_calls"] == 2
    assert run.heartbeat_at is not None
    assert seen == [("a", str(run.id), 2)]


async def test_unknown_tool_becomes_observation(db_session):
    turns = [turn("", [("c1", "nope.tool", {})]), turn("无法完成，工具不存在。")]
    run = await new_run(db_session)
    await run_agent(db_session, run, **deps(turns, []))
    await db_session.refresh(run)
    steps = await steps_of(db_session, run)

    assert run.status == "succeeded"
    assert steps[1].status == "error"
    assert "不存在" in steps[1].error
    tool_messages = [item for item in run.messages["items"] if item["role"] == "tool"]
    assert tool_messages[0]["content"].startswith("以下为系统数据")
    assert "nope.tool" in tool_messages[0]["content"]


async def test_invalid_arguments_are_reported(db_session):
    async def handler(ctx, args):
        raise AssertionError("handler must not run")

    turns = [turn("", [("c1", "demo.read", "not-json")]), turn("参数有误，已停止。")]
    run = await new_run(db_session)
    await run_agent(db_session, run, **deps(turns, [tool("demo.read", handler)]))
    await db_session.refresh(run)
    steps = await steps_of(db_session, run)

    assert run.status == "succeeded"
    assert steps[1].status == "error"
    assert "JSON" in steps[1].error


async def test_tool_app_error_is_safe_observation(db_session):
    async def handler(ctx, args):
        raise AppError("not_found", "项目不存在", 404)

    turns = [turn("", [("c1", "demo.read", {})]), turn("项目不存在，需要新建。")]
    run = await new_run(db_session)
    await run_agent(db_session, run, **deps(turns, [tool("demo.read", handler)]))
    await db_session.refresh(run)
    steps = await steps_of(db_session, run)

    assert run.status == "succeeded"
    assert steps[1].status == "error"
    assert steps[1].error == "项目不存在"
    assert steps[1].error != steps[1].output  # 输出为脱敏字典，不是原始异常


async def test_denied_tool_is_not_executed(db_session):
    calls = []

    async def handler(ctx, args):
        calls.append(args)
        return ToolResult(summary="不应执行")

    turns = [turn("", [("c1", "demo.read", {})]), turn("该操作被禁止。")]
    run = await new_run(db_session, policy={"demo.read": "deny"})
    await run_agent(db_session, run, **deps(turns, [tool("demo.read", handler)]))
    await db_session.refresh(run)
    steps = await steps_of(db_session, run)

    assert calls == []
    assert steps[1].status == "denied"
    assert "策略禁止" in steps[1].error


async def test_step_budget_exhausted(db_session):
    async def handler(ctx, args):
        return ToolResult(summary="ok")

    turns = [turn("", [("c1", "demo.read", {})]), turn("继续"), turn("结束")]
    run = await new_run(
        db_session,
        budget={**BUDGET, "max_steps": 2},
    )
    await run_agent(db_session, run, **deps(turns, [tool("demo.read", handler)]))
    await db_session.refresh(run)

    assert run.status == "failed"
    assert run.error_code == "agent_budget_exhausted"
    assert run.error_message == "步数预算已用尽"
    assert run.finished_at is not None


async def test_token_budget_exhausted(db_session):
    calls = []

    async def handler(ctx, args):
        calls.append(args)
        return ToolResult(summary="ok")

    turns = [turn("", [("c1", "demo.read", {})], prompt=15, completion=10), turn("结束")]
    run = await new_run(db_session, budget={**BUDGET, "max_tokens": 20})
    await run_agent(db_session, run, **deps(turns, [tool("demo.read", handler)]))
    await db_session.refresh(run)

    assert run.status == "failed"
    assert run.error_code == "agent_budget_exhausted"
    assert calls == []


async def test_tool_call_budget_exhausted(db_session):
    calls = []

    async def handler(ctx, args):
        calls.append(args)
        return ToolResult(summary="ok")

    turns = [
        turn("", [("c1", "demo.read", {"text": "a"}), ("c2", "demo.read", {"text": "b"})]),
        turn("结束"),
    ]
    run = await new_run(db_session, budget={**BUDGET, "max_tool_calls": 1})
    await run_agent(db_session, run, **deps(turns, [tool("demo.read", handler)]))
    await db_session.refresh(run)

    assert run.status == "failed"
    assert run.error_code == "agent_budget_exhausted"
    assert len(calls) == 1


async def test_loop_detection_stops_repeated_calls(db_session):
    calls = []

    async def handler(ctx, args):
        calls.append(args)
        return ToolResult(summary="ok")

    repeated = [("c1", "demo.read", {"text": "same"})]
    turns = [turn("", repeated) for _ in range(3)]
    run = await new_run(db_session)
    await run_agent(db_session, run, **deps(turns, [tool("demo.read", handler)]))
    await db_session.refresh(run)

    assert run.status == "failed"
    assert run.error_code == "agent_loop_detected"
    assert len(calls) == 2


async def test_cancel_during_run_is_observed(db_session):
    run = await new_run(db_session)

    async def handler(ctx, args):
        run.status = "cancelled"
        await ctx.session.commit()
        return ToolResult(summary="已取消")

    turns = [turn("", [("c1", "demo.read", {})]), turn("不应再调用")]
    await run_agent(db_session, run, **deps(turns, [tool("demo.read", handler)]))
    await db_session.refresh(run)

    assert run.status == "cancelled"
    assert run.finished_at is not None


async def test_interrupted_step_is_recovered_without_replay(db_session):
    run = await new_run(
        db_session,
        messages={
            "items": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "写一篇新文章并创建草稿"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c0",
                            "type": "function",
                            "function": {"name": "demo.read", "arguments": "{}"},
                        }
                    ],
                },
            ]
        },
    )
    run.step_count = 2
    interrupted = AgentStep(
        tenant_id=TENANT_A,
        run_id=run.id,
        step_index=2,
        kind="tool",
        tool_name="demo.read",
        call_id="c0",
        status="running",
    )
    db_session.add(interrupted)
    await db_session.commit()

    async def handler(ctx, args):
        raise AssertionError("中断步骤不应被重放")

    turns = [turn("上次中断了，继续完成。")]
    await run_agent(db_session, run, **deps(turns, [tool("demo.read", handler)]))
    await db_session.refresh(run)
    await db_session.refresh(interrupted)
    steps = await steps_of(db_session, run)

    assert run.status == "succeeded"
    assert interrupted.status == "interrupted"
    assert interrupted.error == INTERRUPTED_OBSERVATION
    tool_messages = [item for item in run.messages["items"] if item["role"] == "tool"]
    assert INTERRUPTED_OBSERVATION in tool_messages[0]["content"]
    assert [step.kind for step in steps] == ["tool", "llm"]
    assert steps[1].step_index == 3


async def test_budget_stop_closes_the_open_tool_turn(db_session):
    """预算中断时也要补齐 tool 回复，历史对 provider 始终合法。"""

    async def handler(ctx, args):
        return ToolResult(summary="ok")

    turns = [
        turn("", [("c1", "demo.read", {"text": "a"}), ("c2", "demo.read", {"text": "b"})]),
        turn("结束"),
    ]
    run = await new_run(db_session, budget={**BUDGET, "max_tool_calls": 1})
    await run_agent(db_session, run, **deps(turns, [tool("demo.read", handler)]))
    await db_session.refresh(run)

    items = run.messages["items"]
    declared = [
        call["id"]
        for item in items
        if item["role"] == "assistant"
        for call in item.get("tool_calls") or []
    ]
    answered = [item["tool_call_id"] for item in items if item["role"] == "tool"]
    assert declared == ["c1", "c2"]
    assert answered == ["c1", "c2"]
    assert run.status == "failed"


async def test_recovery_rebuilds_reply_from_completed_step(db_session):
    """步骤已成功但回复未落库时，用步骤输出补回复，不重放工具。"""

    async def handler(ctx, args):
        raise AssertionError("已完成的步骤不应被重放")

    run = await new_run(
        db_session,
        messages={
            "items": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "写一篇新文章并创建草稿"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c0",
                            "type": "function",
                            "function": {"name": "demo.read", "arguments": "{}"},
                        }
                    ],
                },
            ]
        },
    )
    run.step_count = 2
    db_session.add(
        AgentStep(
            tenant_id=TENANT_A,
            run_id=run.id,
            step_index=2,
            kind="tool",
            tool_name="demo.read",
            call_id="c0",
            status="ok",
            output={"summary": "找到画像", "data": {"items": [{"id": "p1"}]}},
        )
    )
    await db_session.commit()

    turns = [turn("已完成读取。")]
    await run_agent(db_session, run, **deps(turns, [tool("demo.read", handler)]))
    await db_session.refresh(run)

    tool_messages = [item for item in run.messages["items"] if item["role"] == "tool"]
    assert "找到画像" in tool_messages[0]["content"]
    assert INTERRUPTED_OBSERVATION not in tool_messages[0]["content"]
    assert run.status == "succeeded"
    assert run.step_count == 3


async def test_prefixed_policy_override_is_honored(db_session):
    """AgentPolicy 存的 ``tool:`` 前缀键同样生效。"""

    calls = []

    async def handler(ctx, args):
        calls.append(args)
        return ToolResult(summary="不应执行")

    turns = [turn("", [("c1", "demo.read", {})]), turn("该操作被禁止。")]
    run = await new_run(db_session, policy={"tool:demo.read": "deny"})
    await run_agent(db_session, run, **deps(turns, [tool("demo.read", handler)]))
    await db_session.refresh(run)
    steps = await steps_of(db_session, run)

    assert calls == []
    assert steps[1].status == "denied"


async def test_high_risk_policy_cannot_be_loosened(db_session):
    """快照里的宽松动作会被 resolve_policy 收敛回风险默认值。"""

    async def handler(ctx, args):
        raise AssertionError("高风险工具未获批不应执行")

    turns = [turn("", [("c1", "demo.write", {})]), turn("需要确认。")]
    run = await new_run(db_session, policy={"tool:demo.write": "auto"})
    await run_agent(
        db_session,
        run,
        **deps(turns, [tool("demo.write", handler, risk="high")]),
    )
    await db_session.refresh(run)
    steps = await steps_of(db_session, run)

    assert steps[1].status == "denied"
    assert "需要人工确认" in steps[1].error


async def test_tool_secrets_never_reach_history(db_session):
    """工具返回的数据先脱敏再入观测与 run.result，敏感键的值不会泄漏。"""

    async def handler(ctx, args):
        return ToolResult(
            summary="返回含秘钥",
            data={"api_key": "sk-secret", "text": "ok"},
        )

    turns = [turn("", [("c1", "demo.read", {"text": "a"})]), turn("完成")]
    run = await new_run(db_session)
    await run_agent(db_session, run, **deps(turns, [tool("demo.read", handler)]))
    await db_session.refresh(run)
    steps = await steps_of(db_session, run)

    persisted = json.dumps(
        {
            "messages": run.messages,
            "result": run.result,
            "outputs": [step.output for step in steps],
            "errors": [step.error for step in steps],
        },
        ensure_ascii=False,
    )
    assert "sk-secret" not in persisted
    assert steps[1].output["data"] == {"text": "ok"}
    tool_messages = [item for item in run.messages["items"] if item["role"] == "tool"]
    assert "ok" in tool_messages[0]["content"]


async def test_schema_error_does_not_echo_argument_values(db_session):
    """jsonschema 的 message 会回显参数值，落库只保留路径与关键字。"""

    async def handler(ctx, args):
        raise AssertionError("schema 不通过时不应执行")

    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "additionalProperties": False,
    }
    turns = [
        turn("", [("c1", "demo.read", {"count": "sk-secret"})]),
        turn("参数有误。"),
    ]
    run = await new_run(db_session)
    await run_agent(
        db_session, run, **deps(turns, [tool("demo.read", handler, schema=schema)])
    )
    await db_session.refresh(run)
    steps = await steps_of(db_session, run)

    assert steps[1].status == "error"
    assert "参数不合法" in steps[1].error
    assert "type" in steps[1].error
    assert "$.count" in steps[1].error
    assert "sk-secret" not in steps[1].error
    tool_messages = [item for item in run.messages["items"] if item["role"] == "tool"]
    assert "sk-secret" not in tool_messages[0]["content"]


async def test_tool_exception_message_is_scrubbed(db_session):
    async def handler(ctx, args):
        raise RuntimeError("调用失败 api_key=sk-secret")

    turns = [turn("", [("c1", "demo.read", {})]), turn("工具失败。")]
    run = await new_run(db_session)
    await run_agent(db_session, run, **deps(turns, [tool("demo.read", handler)]))
    await db_session.refresh(run)
    steps = await steps_of(db_session, run)

    assert steps[1].status == "error"
    assert "RuntimeError" in steps[1].error
    assert "sk-secret" not in steps[1].error
    assert "***" in steps[1].error
    tool_messages = [item for item in run.messages["items"] if item["role"] == "tool"]
    assert "sk-secret" not in tool_messages[0]["content"]


async def test_inline_api_key_in_error_is_redacted(db_session):
    async def handler(ctx, args):
        raise RuntimeError("Incorrect API key provided: sk-abc123")

    turns = [turn("", [("c1", "demo.read", {})]), turn("工具失败。")]
    run = await new_run(db_session)
    await run_agent(db_session, run, **deps(turns, [tool("demo.read", handler)]))
    await db_session.refresh(run)
    steps = await steps_of(db_session, run)

    assert steps[1].status == "error"
    assert "sk-abc123" not in steps[1].error
    assert "[已脱敏]" in steps[1].error
    assert "Incorrect API key provided:" in steps[1].error
    tool_messages = [item for item in run.messages["items"] if item["role"] == "tool"]
    assert "sk-abc123" not in tool_messages[0]["content"]


async def test_long_call_id_is_truncated_consistently(db_session):
    async def handler(ctx, args):
        return ToolResult(summary="ok")

    long_id = "x" * 300
    turns = [turn("", [(long_id, "demo.read", {"text": "a"})]), turn("完成")]
    run = await new_run(db_session)
    await run_agent(db_session, run, **deps(turns, [tool("demo.read", handler)]))
    await db_session.refresh(run)
    steps = await steps_of(db_session, run)

    assistant_calls = [
        call
        for item in run.messages["items"]
        if item["role"] == "assistant"
        for call in item.get("tool_calls") or []
    ]
    tool_messages = [item for item in run.messages["items"] if item["role"] == "tool"]
    assert len(assistant_calls[0]["id"]) == 100
    assert steps[1].call_id == assistant_calls[0]["id"]
    assert tool_messages[0]["tool_call_id"] == assistant_calls[0]["id"]
    assert run.status == "succeeded"


async def test_empty_and_duplicate_call_ids_are_synthesised(db_session):
    async def handler(ctx, args):
        return ToolResult(summary="ok")

    calls = [
        ("", "demo.read", {"text": "a"}),
        ("dup", "demo.read", {"text": "b"}),
        ("dup", "demo.read", {"text": "c"}),
    ]
    turns = [turn("", calls), turn("完成")]
    run = await new_run(db_session)
    await run_agent(db_session, run, **deps(turns, [tool("demo.read", handler)]))
    await db_session.refresh(run)

    items = run.messages["items"]
    declared = [
        call["id"]
        for item in items
        if item["role"] == "assistant"
        for call in item.get("tool_calls") or []
    ]
    answered = [item["tool_call_id"] for item in items if item["role"] == "tool"]
    assert len(declared) == len(set(declared)) == 3
    assert all(declared)
    assert sorted(answered) == sorted(declared)
    assert run.status == "succeeded"


async def test_more_than_four_calls_are_skipped(db_session):
    calls = []

    async def handler(ctx, args):
        calls.append(args["text"])
        return ToolResult(summary="ok")

    tool_calls = [(f"c{i}", "demo.read", {"text": f"t{i}"}) for i in range(5)]
    turns = [turn("", tool_calls), turn("完成")]
    run = await new_run(db_session)
    await run_agent(db_session, run, **deps(turns, [tool("demo.read", handler)]))
    await db_session.refresh(run)

    tool_messages = [item for item in run.messages["items"] if item["role"] == "tool"]
    assert calls == ["t0", "t1", "t2", "t3"]
    assert len(tool_messages) == 5
    assert "单轮最多执行" in tool_messages[-1]["content"]
    assert run.status == "succeeded"


async def test_max_seconds_stop_happens_before_the_next_llm_call(db_session):
    turns = [turn("不应被调用")]
    deps_map = deps(turns, [])
    run = await new_run(db_session, budget={**BUDGET, "max_seconds": 1})
    run.started_at = datetime.now(UTC) - timedelta(seconds=5)
    await db_session.commit()

    await run_agent(db_session, run, **deps_map)
    await db_session.refresh(run)

    assert run.status == "failed"
    assert run.error_code == "agent_budget_exhausted"
    assert run.error_message == "运行时间预算已用尽"
    assert deps_map["gateway"].calls == []
    assert run.usage["steps"] == run.step_count


async def test_tool_running_past_the_deadline_stops_the_run(db_session):
    async def slow_handler(ctx, args):
        await asyncio.sleep(1.0)
        return ToolResult(summary="太晚了")

    turns = [turn("", [("c1", "demo.read", {"text": "a"})]), turn("不应被调用")]
    deps_map = deps(turns, [tool("demo.read", slow_handler)])
    run = await new_run(db_session, budget={**BUDGET, "max_seconds": 2})
    run.started_at = datetime.now(UTC) - timedelta(seconds=1.5)
    await db_session.commit()

    await run_agent(db_session, run, **deps_map)
    await db_session.refresh(run)
    steps = await steps_of(db_session, run)

    assert run.status == "failed"
    assert run.error_code == "agent_budget_exhausted"
    assert run.error_message == "运行时间预算已用尽"
    assert len(deps_map["gateway"].calls) == 1
    assert steps[-1].kind == "tool"
    assert steps[-1].status == "error"
    assert "运行时间预算已用尽" in steps[-1].error
    items = run.messages["items"]
    declared = {
        call["id"]
        for item in items
        if item["role"] == "assistant"
        for call in item.get("tool_calls") or []
    }
    answered = {item["tool_call_id"] for item in items if item["role"] == "tool"}
    assert declared == answered


async def test_per_tool_timeout_is_reported(db_session):
    async def slow_handler(ctx, args):
        await asyncio.sleep(0.2)
        return ToolResult(summary="超时了")

    turns = [turn("", [("c1", "demo.read", {"text": "a"})]), turn("工具超时，继续。")]
    run = await new_run(db_session)
    await run_agent(
        db_session,
        run,
        **deps(turns, [tool("demo.read", slow_handler, timeout=0.05)]),
    )
    await db_session.refresh(run)
    steps = await steps_of(db_session, run)

    assert steps[1].status == "error"
    assert "工具执行超时" in steps[1].error
    assert run.status == "succeeded"


async def test_handler_timeout_error_before_budget_keeps_the_loop_going(db_session):
    """handler 自己抛 TimeoutError（如 socket 超时）不等于墙钟预算耗尽。"""

    async def handler(ctx, args):
        raise TimeoutError("socket timed out")

    turns = [turn("", [("c1", "demo.read", {"text": "a"})]), turn("超时了，已继续。")]
    deps_map = deps(turns, [tool("demo.read", handler, timeout=60.0)])
    # 剩余预算小于 handler 配置的超时，走 wait_for 预算裁剪分支。
    run = await new_run(db_session, budget={**BUDGET, "max_seconds": 10})
    run.started_at = datetime.now(UTC) - timedelta(seconds=9.5)
    await db_session.commit()

    await run_agent(db_session, run, **deps_map)
    await db_session.refresh(run)
    steps = await steps_of(db_session, run)

    assert run.status == "succeeded"
    assert len(deps_map["gateway"].calls) == 2
    assert steps[1].status == "error"
    assert "工具执行超时" in steps[1].error
    assert "运行时间预算已用尽" not in steps[1].error
    assert run.usage["tool_calls"] == 1


async def test_artifacts_survive_recovery_once(db_session):
    artifact = {"type": "profile", "id": "p1", "title": "画像"}
    run = await new_run(
        db_session,
        messages={
            "items": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "写一篇新文章并创建草稿"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c0",
                            "type": "function",
                            "function": {"name": "demo.read", "arguments": "{}"},
                        },
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "demo.read", "arguments": "{}"},
                        },
                    ],
                },
            ]
        },
    )
    run.step_count = 3
    for index, call_id in ((2, "c0"), (3, "c1")):
        db_session.add(
            AgentStep(
                tenant_id=TENANT_A,
                run_id=run.id,
                step_index=index,
                kind="tool",
                tool_name="demo.read",
                call_id=call_id,
                status="ok",
                output={"summary": "找到画像", "data": {}, "artifacts": [artifact]},
            )
        )
    await db_session.commit()

    async def handler(ctx, args):
        raise AssertionError("已完成的步骤不应被重放")

    turns = [turn("已完成。")]
    await run_agent(db_session, run, **deps(turns, [tool("demo.read", handler)]))
    await db_session.refresh(run)

    assert run.status == "succeeded"
    assert run.result["artifacts"] == [artifact]


async def test_long_history_is_compacted_in_loop(db_session):
    items = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "写一篇新文章并创建草稿"},
    ]
    for index in range(30):
        items.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"h{index}",
                        "type": "function",
                        "function": {"name": "demo.read", "arguments": "{}"},
                    }
                ],
            }
        )
        items.append(
            {"role": "tool", "tool_call_id": f"h{index}", "content": "x" * 12_000}
        )
    run = await new_run(db_session, messages={"items": items})

    async def handler(ctx, args):
        return ToolResult(summary="ok")

    turns = [turn("", [("n1", "demo.read", {"text": "a"})]), turn("完成")]
    deps_map = deps(turns, [tool("demo.read", handler)])
    await run_agent(db_session, run, **deps_map)
    await db_session.refresh(run)

    gateway = deps_map["gateway"]
    assert body_size(gateway.calls[0]) > MAX_MESSAGES_BYTES
    assert body_size(gateway.calls[1]) <= MAX_MESSAGES_BYTES
    compacted = gateway.calls[1]
    declared = {
        call["id"]
        for item in compacted
        if item["role"] == "assistant"
        for call in item.get("tool_calls") or []
    }
    tool_ids = {item["tool_call_id"] for item in compacted if item["role"] == "tool"}
    assert tool_ids <= declared
    assert any(
        str(item.get("content") or "").startswith("[已压缩] ")
        for item in compacted
        if item["role"] == "tool"
    )
    assert len(compacted) == len(run.messages["items"]) - 1


async def test_repeated_persist_does_not_stack_compressed_prefixes(db_session):
    items = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "写一篇新文章并创建草稿"},
    ]
    for index in range(30):
        items.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"h{index}",
                        "type": "function",
                        "function": {"name": "demo.read", "arguments": "{}"},
                    }
                ],
            }
        )
        items.append(
            {"role": "tool", "tool_call_id": f"h{index}", "content": "x" * 12_000}
        )
    run = await new_run(db_session)
    live = json.loads(json.dumps(items))

    await _persist(db_session, run, live)
    first = json.loads(json.dumps(run.messages))
    for _ in range(4):
        await _persist(db_session, run, live)

    assert run.messages == first
    contents = [
        str(item.get("content") or "")
        for item in run.messages["items"]
        if item["role"] == "tool"
    ]
    assert all(not content.startswith(COMPRESSED_PREFIX * 2) for content in contents)
    assert any(content.startswith(COMPRESSED_PREFIX) for content in contents)
    assert body_size(run.messages) <= MAX_MESSAGES_BYTES


async def test_loop_detection_ignores_argument_key_order(db_session):
    calls = []

    async def handler(ctx, args):
        calls.append(args)
        return ToolResult(summary="ok")

    schema = {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "additionalProperties": False,
    }
    turns = [
        turn("", [("c1", "demo.read", {"a": 1, "b": 2})]),
        turn("", [("c2", "demo.read", {"b": 2, "a": 1})]),
        turn("", [("c3", "demo.read", {"a": 1, "b": 2})]),
    ]
    run = await new_run(db_session)
    await run_agent(
        db_session, run, **deps(turns, [tool("demo.read", handler, schema=schema)])
    )
    await db_session.refresh(run)

    assert run.status == "failed"
    assert run.error_code == "agent_loop_detected"
    assert len(calls) == 2


async def test_explicit_zero_budget_is_honoured(db_session):
    turns = [turn("不应被调用")]
    deps_map = deps(turns, [])
    run = await new_run(db_session, budget={**BUDGET, "max_steps": 0})
    await run_agent(db_session, run, **deps_map)
    await db_session.refresh(run)

    assert run.status == "failed"
    assert run.error_code == "agent_budget_exhausted"
    assert run.error_message == "步数预算已用尽"
    assert deps_map["gateway"].calls == []
    assert run.usage["steps"] == 0


class BoomGateway:
    async def complete_tool_calls(self, messages, *, tools):
        raise RuntimeError("provider down api_key=sk-secret")


async def test_gateway_crash_marks_run_failed(db_session):
    run = await new_run(db_session)
    deps_map = deps([], [])
    deps_map["gateway"] = BoomGateway()

    await run_agent(db_session, run, **deps_map)
    await db_session.refresh(run)
    steps = await steps_of(db_session, run)

    assert run.status == "failed"
    assert run.error_code == "agent_internal_error"
    assert run.error_message == "运行发生内部错误，请稍后重试"
    assert run.finished_at is not None
    assert steps[0].status == "error"
    assert steps[0].error == "LLM 调用失败"


async def test_internal_failure_interrupts_running_steps(db_session):
    run = await new_run(db_session)
    run.step_count = 1
    orphan = AgentStep(
        tenant_id=TENANT_A,
        run_id=run.id,
        step_index=1,
        kind="tool",
        tool_name="demo.read",
        call_id="c0",
        status="running",
    )
    db_session.add(orphan)
    await db_session.commit()

    deps_map = deps([], [])
    deps_map["gateway"] = BoomGateway()
    await run_agent(db_session, run, **deps_map)
    await db_session.refresh(run)
    await db_session.refresh(orphan)
    steps = await steps_of(db_session, run)

    assert run.status == "failed"
    assert run.error_code == "agent_internal_error"
    assert orphan.status == "interrupted"
    assert orphan.error == INTERRUPTED_OBSERVATION
    assert orphan.finished_at is not None
    assert all(step.status != "running" for step in steps)
