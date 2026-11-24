"""图结构读取、拓扑判定、就绪/跳过传播与受限变量解析。"""

import re
from dataclasses import dataclass
from typing import Any

NODE_KINDS = ("input", "tool", "agent", "team", "condition", "review", "output")
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
        if not isinstance(source, str) or not isinstance(target, str):
            raise GraphError("graph_invalid", "边的 source 与 target 必须是字符串")
        if source not in nodes or target not in nodes:
            raise GraphError(
                "graph_invalid", f"边引用了不存在的节点: {source} → {target}"
            )
        handle = item.get("sourceHandle") or ""
        if not isinstance(handle, str):
            raise GraphError("edge_invalid", "句柄必须是字符串", source)
        if handle not in {"", "true", "false"}:
            raise GraphError("edge_invalid", f"未知句柄: {handle}", source)
        key = (source, target, handle)
        if key in seen:
            raise GraphError("graph_invalid", "存在重复的边")
        seen.add(key)
        edges.append(GraphEdge(source, target, handle))

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
