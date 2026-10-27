import uuid
from pathlib import Path

import pytest

from app.llm.types import Usage
from app.skills.executor import SkillExecutor, parse_json_output
from app.skills.registry import SkillRegistry

FIXTURE = Path(__file__).parent / "fixtures" / "skills"
TENANT = "00000000-0000-0000-0000-000000000001"


class FakeGateway:
    def __init__(self, reply: str = "口号：少加班，多产出"):
        self.reply = reply
        self.last_usage = Usage(11, 7)
        self.messages = []

    async def complete(self, messages, **kwargs):
        self.messages = messages
        return self.reply


class FailingGateway:
    def __init__(self):
        self.last_usage = Usage()

    async def complete(self, messages, **kwargs):
        raise RuntimeError("upstream boom")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('说明文字 {"a": 2} 结束', {"a": 2}),
    ],
)
def test_parse_json_output_handles_fences(raw, expected):
    assert parse_json_output(raw) == expected


def test_parse_json_output_rejects_non_object():
    with pytest.raises(ValueError, match="必须是对象"):
        assert parse_json_output("[1, 2, 3]")


async def test_executor_runs_and_audits(db_session):
    from sqlalchemy import select

    from app.models.skill import SkillRun

    user_id = uuid.uuid4()
    gateway = FakeGateway()
    executor = SkillExecutor(
        registry=SkillRegistry(FIXTURE),
        gateway=gateway,
        session=db_session,
        tenant_id=TENANT,
        profile_block="<profile_context>账号名：效率笔记</profile_context>",
        created_by=user_id,
    )
    result = await executor.run("demo-skill", {"topic": "AI 周报"})
    assert result.text == "口号：少加班，多产出"
    assert result.prompt_tokens == 11
    assert result.completion_tokens == 7
    assert result.data is None

    assert gateway.messages[0]["role"] == "system"
    assert "效率笔记" in gateway.messages[0]["content"]
    assert gateway.messages[1] == {"role": "user", "content": '{"topic": "AI 周报"}'}

    run = (await db_session.execute(select(SkillRun))).scalar_one()
    assert run.status == "succeeded"
    assert run.skill_id == "demo-skill"
    assert run.created_by == user_id
    assert run.prompt_tokens == 11
    assert run.completion_tokens == 7
    assert run.duration_ms >= 0
    assert run.finished_at is not None


async def test_executor_audits_failure(db_session):
    from sqlalchemy import select

    from app.models.skill import SkillRun

    executor = SkillExecutor(
        registry=SkillRegistry(FIXTURE),
        gateway=FailingGateway(),
        session=db_session,
        tenant_id=TENANT,
    )
    with pytest.raises(RuntimeError, match="upstream boom"):
        await executor.run("demo-skill", {"topic": "AI 周报"})

    run = (await db_session.execute(select(SkillRun))).scalar_one()
    assert run.status == "failed"
    assert "upstream boom" in run.error
    assert run.finished_at is not None
