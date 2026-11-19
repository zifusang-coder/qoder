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
        raise ValueError("输出不是 JSON 对象")  # noqa: TRY004
    return data


def _text(data: dict, key: str, *, required: bool) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是字符串")  # noqa: TRY004
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
        raise ValueError("role 必须是字符串")  # noqa: TRY004
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
        raise ValueError("issues 必须是数组")  # noqa: TRY004
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