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
