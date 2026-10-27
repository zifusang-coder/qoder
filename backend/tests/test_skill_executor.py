import asyncio
import uuid
from pathlib import Path

import jsonschema
import pytest

from app.llm.types import Usage
from app.skills.executor import SkillExecutor, parse_json_output
from app.skills.registry import SkillRegistry

FIXTURE = Path(__file__).parent / "fixtures" / "skills"
TENANT = "00000000-0000-0000-0000-000000000001"


def _write_skill(
    root: Path,
    skill_id: str,
    *,
    output_format: str = "markdown",
    postprocess: str | None = None,
) -> Path:
    skill_dir = root / skill_id
    skill_dir.mkdir(parents=True)
    postprocess_line = f"postprocess: {postprocess}\n" if postprocess else ""
    (skill_dir / "manifest.yaml").write_text(
        f"""id: {skill_id}
name: 测试技能
version: "0.1.0"
layer: produce
inputs:
  type: object
  required: [topic]
  properties:
    topic:
      type: string
outputs:
  format: {output_format}
{postprocess_line}llm:
  tier: fast
  temperature: 0.5
tools: [llm]
""",
        encoding="utf-8",
    )
    (skill_dir / "prompt.md").write_text("请输出测试结果。", encoding="utf-8")
    return skill_dir


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


class CancellingGateway:
    def __init__(self):
        self.last_usage = Usage()

    async def complete(self, messages, **kwargs):
        raise asyncio.CancelledError


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


async def test_executor_drops_undeclared_inputs(db_session):
    from sqlalchemy import select

    from app.models.skill import SkillRun

    gateway = FakeGateway()
    executor = SkillExecutor(
        registry=SkillRegistry(FIXTURE),
        gateway=gateway,
        session=db_session,
        tenant_id=TENANT,
    )
    await executor.run("demo-skill", {"topic": "AI", "junk": "x"})

    run = (await db_session.execute(select(SkillRun))).scalar_one()
    assert run.inputs == {"topic": "AI"}
    assert gateway.messages[1] == {"role": "user", "content": '{"topic": "AI"}'}


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


async def test_executor_parses_json_output(db_session, tmp_path):
    from sqlalchemy import select

    from app.models.skill import SkillRun

    _write_skill(tmp_path, "json-skill", output_format="json")
    executor = SkillExecutor(
        registry=SkillRegistry(tmp_path),
        gateway=FakeGateway('```json\n{"title": "hello"}\n```'),
        session=db_session,
        tenant_id=TENANT,
    )

    result = await executor.run("json-skill", {"topic": "AI"})

    assert result.data == {"title": "hello"}
    run = (await db_session.execute(select(SkillRun))).scalar_one()
    assert run.status == "succeeded"
    assert run.outputs["data"] == {"title": "hello"}


async def test_executor_runs_postprocess_script(db_session, tmp_path):
    from sqlalchemy import select

    from app.models.skill import SkillRun

    skill_dir = _write_skill(
        tmp_path, "post-skill", output_format="json", postprocess="postprocess.py"
    )
    (skill_dir / "postprocess.py").write_text(
        """import json
import sys

payload = json.load(sys.stdin)
print(json.dumps({"title": payload["output"]["title"].upper()}))
""",
        encoding="utf-8",
    )
    executor = SkillExecutor(
        registry=SkillRegistry(tmp_path),
        gateway=FakeGateway('{"title": "hello"}'),
        session=db_session,
        tenant_id=TENANT,
    )

    result = await executor.run("post-skill", {"topic": "AI"})

    assert result.data == {"title": "HELLO"}
    assert result.text == '{"title": "HELLO"}'
    run = (await db_session.execute(select(SkillRun))).scalar_one()
    assert run.status == "succeeded"


async def test_executor_audits_schema_failure(db_session):
    from sqlalchemy import select

    from app.models.skill import SkillRun

    executor = SkillExecutor(
        registry=SkillRegistry(FIXTURE),
        gateway=FakeGateway(),
        session=db_session,
        tenant_id=TENANT,
    )

    with pytest.raises(jsonschema.ValidationError):
        await executor.run("demo-skill", {})

    run = (await db_session.execute(select(SkillRun))).scalar_one()
    assert run.status == "failed"
    assert "topic" in run.error
    assert run.finished_at is not None


async def test_executor_audits_cancellation(db_session):
    from sqlalchemy import select

    from app.models.skill import SkillRun

    executor = SkillExecutor(
        registry=SkillRegistry(FIXTURE),
        gateway=CancellingGateway(),
        session=db_session,
        tenant_id=TENANT,
    )

    with pytest.raises(asyncio.CancelledError):
        await executor.run("demo-skill", {"topic": "AI 周报"})

    run = (await db_session.execute(select(SkillRun))).scalar_one()
    assert run.status == "cancelled"
    assert run.finished_at is not None
