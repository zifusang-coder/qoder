"""草稿结构校验与发布校验。保存只做结构检查，发布必须全部通过。"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.agent.policy import resolve_policy
from app.agent.registry import ToolRegistry
from app.agent.roles import EXECUTION_ROLES, TEAM_BUDGET_KEYS
from app.workflow.graph import (
    Graph,
    GraphError,
    GraphNode,
    parse_graph,
    topological_order,
)

MAX_NODES = 50
MAX_EDGES = 100
MAX_BYTES = 256 * 1024
FIELD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
FIELD_TYPES = {"string", "number", "boolean"}
OPERATORS = {"equals", "not_equals", "contains", "not_empty", "gt", "lt"}
BUDGET_KEYS = frozenset({"max_steps", "max_tokens", "max_tool_calls", "max_seconds"})


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
    """保存草稿只做的轻量结构检查：数组形状、id 唯一、边端点存在、规模上限。

    允许暂时不完整（无输出节点、有环、缺字段），这些留给发布校验。
    """
    if _size_bytes(raw) > MAX_BYTES:
        return ValidationResult([Issue("size_exceeded", "图超过 256 KB")])
    try:
        graph = parse_graph(raw)
    except GraphError as exc:
        return ValidationResult([Issue(exc.code, exc.message, exc.node_id or "")])
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


def _whitelist_matches(patterns: list, registry: ToolRegistry) -> bool:
    """白名单里的精确名或 ``前缀.*`` 至少匹配注册表里的一个工具。"""
    names = registry.names()
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern:
            continue
        if pattern.endswith(".*"):
            prefix = pattern[:-1]
            if any(candidate.startswith(prefix) for candidate in names):
                return True
        elif pattern in names:
            return True
    return False


def _check_fields(node: GraphNode, issues: list[Issue]) -> None:
    fields = node.data.get("fields")
    if not isinstance(fields, list) or not fields:
        issues.append(Issue("input_invalid", "输入节点至少需要一个字段", node.id))
        return
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
            not isinstance(max_length, int)
            or isinstance(max_length, bool)
            or max_length <= 0
        ):
            issues.append(Issue("input_invalid", f"字段 {key} maxLength 不合法", node.id))
        if not isinstance(item.get("required", False), bool):
            issues.append(Issue("input_invalid", f"字段 {key} required 必须是布尔值", node.id))


def _check_condition(node: GraphNode, outgoing, issues: list[Issue]) -> None:
    data = node.data
    handles = sorted(item.handle for item in outgoing)
    if handles != ["false", "true"]:
        issues.append(
            Issue("condition_invalid", "条件节点必须有一条 true 和一条 false 出边", node.id)
        )
    operator = data.get("operator")
    if operator not in OPERATORS:
        issues.append(Issue("condition_invalid", "条件运算符不合法", node.id))
    elif operator != "not_empty" and "value" not in data:
        issues.append(Issue("condition_invalid", "条件缺少比较值", node.id))
    if not isinstance(data.get("left"), str) or not data["left"].strip():
        issues.append(Issue("condition_invalid", "条件缺少左值", node.id))


def _check_tool(
    node: GraphNode,
    *,
    registry: ToolRegistry,
    skill_ids: set[str],
    overrides: dict,
    issues: list[Issue],
) -> None:
    name = node.data.get("tool")
    spec = registry.get(name) if isinstance(name, str) else None
    if spec is None:
        issues.append(Issue("tool_unknown", f"未知工具: {name}", node.id))
        return
    if resolve_policy(overrides, spec) == "deny":
        # 策略可变，运行期才权威；发布只提示，不阻断。
        issues.append(
            Issue(
                "policy_denies_tool",
                f"当前策略拒绝 {name}，运行时会失败",
                node.id,
                severity="warning",
            )
        )
    args = node.data.get("args", {})
    if not isinstance(args, dict):
        issues.append(Issue("node_invalid", "工具参数必须是对象", node.id))
        return
    if name == "skill.run":
        skill_id = args.get("skill_id")
        if not isinstance(skill_id, str) or skill_id not in skill_ids:
            issues.append(Issue("skill_unknown", f"未知技能: {skill_id}", node.id))


def _check_agent(
    node: GraphNode, *, registry: ToolRegistry, issues: list[Issue]
) -> None:
    data = node.data
    goal = data.get("goal")
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 4000:
        issues.append(Issue("node_invalid", "Agent 目标长度必须为 1–4000", node.id))
    tools = data.get("tools") or []
    if not isinstance(tools, list):
        issues.append(Issue("node_invalid", "Agent 工具白名单必须是数组", node.id))
    elif tools and not _whitelist_matches(tools, registry):
        issues.append(
            Issue("agent_tools_unknown", "工具白名单没有匹配任何工具", node.id)
        )
    budget = data.get("budget") or {}
    if not isinstance(budget, dict):
        issues.append(Issue("node_invalid", "Agent 预算必须是对象", node.id))
        return
    for key, value in budget.items():
        if key not in BUDGET_KEYS:
            issues.append(Issue("node_invalid", f"未知预算维度: {key}", node.id))
        elif not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            issues.append(Issue("node_invalid", f"预算 {key} 必须是正整数", node.id))


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


def _check_node(
    graph: Graph,
    node: GraphNode,
    *,
    registry: ToolRegistry,
    skill_ids: set[str],
    overrides: dict,
    issues: list[Issue],
) -> None:
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
        _check_condition(node, outgoing, issues)
    elif node.kind == "review":
        if not incoming or not outgoing:
            issues.append(Issue("review_invalid", "审核节点必须有入边与出边", node.id))
        title = node.data.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            issues.append(Issue("review_invalid", "审核标题长度必须为 1–200", node.id))
    elif node.kind == "tool":
        _check_tool(
            node,
            registry=registry,
            skill_ids=skill_ids,
            overrides=overrides,
            issues=issues,
        )
    elif node.kind == "agent":
        _check_agent(node, registry=registry, issues=issues)
    elif node.kind == "team":
        _check_team(node, issues)
    if node.kind != "condition" and any(item.handle for item in outgoing):
        issues.append(Issue("edge_invalid", "只有条件节点的出边可以带句柄", node.id))
    retry = node.data.get("retry", 0)
    if not isinstance(retry, int) or isinstance(retry, bool) or not 0 <= retry <= 3:
        issues.append(Issue("node_invalid", "retry 必须是 0–3 的整数", node.id))


def _dedupe(issues: list[Issue]) -> list[Issue]:
    """同一节点上完全相同的提示只保留一条，错误优先于警告。"""
    unique: dict[tuple[str, str, str], Issue] = {}
    for issue in issues:
        key = (issue.code, issue.node_id, issue.message)
        current = unique.get(key)
        if current is None or (current.severity == "warning" and issue.severity == "error"):
            unique[key] = issue
    return list(unique.values())


def validate_publish(
    raw: Any,
    *,
    registry: ToolRegistry,
    skill_ids: set[str],
    overrides: dict | None = None,
) -> ValidationResult:
    """发布前的权威校验；返回全部 error/warning，不写数据库。"""
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
    return ValidationResult(_dedupe(issues))


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
