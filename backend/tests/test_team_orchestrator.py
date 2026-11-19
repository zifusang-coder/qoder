import pytest

from app.agent.team_contracts import (
    critic_task,
    lead_messages,
    parse_critic_verdict,
    parse_lead_decision,
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