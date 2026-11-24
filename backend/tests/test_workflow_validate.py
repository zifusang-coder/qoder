import copy

from app.agent.registry import get_tool_registry
from app.workflow.validate import check_structure, validate_publish


def node(node_id, kind, **data):
    return {"id": node_id, "type": kind, "data": data}


def edge(source, target, handle=None):
    value = {"id": f"{source}-{target}", "source": source, "target": target}
    if handle is not None:
        value["sourceHandle"] = handle
    return value


def valid_graph():
    return {
        "nodes": [
            node(
                "in",
                "input",
                fields=[{"key": "topic", "type": "string", "required": True}],
            ),
            node("read", "tool", tool="profile.list", args={"limit": 5}, retry=1),
            node("out", "output"),
        ],
        "edges": [edge("in", "read"), edge("read", "out")],
    }


def codes(result):
    return {issue.code for issue in result.issues}


def publish(raw, overrides=None):
    return validate_publish(
        raw,
        registry=get_tool_registry(),
        skill_ids={"xhs-note"},
        overrides=overrides or {},
    )


def test_valid_graph_passes():
    result = publish(valid_graph())
    assert result.ok is True
    assert result.issues == []


def test_structure_allows_incomplete_draft_but_rejects_bad_shape():
    draft = copy.deepcopy(valid_graph())
    draft["nodes"] = [draft["nodes"][0]]
    draft["edges"] = []
    assert check_structure(draft).ok is True
    assert check_structure({"nodes": "x", "edges": []}).ok is False


def test_publish_requires_single_input_and_one_output():
    raw = copy.deepcopy(valid_graph())
    raw["nodes"].append(
        node("in2", "input", fields=[{"key": "x", "type": "string"}])
    )
    issue_codes = codes(publish(raw))
    assert "input_invalid" in issue_codes

    raw = copy.deepcopy(valid_graph())
    raw["nodes"] = [raw["nodes"][0], raw["nodes"][1]]
    raw["edges"] = [edge("in", "read")]
    assert "output_missing" in codes(publish(raw))


def test_publish_rejects_cycle_and_unreachable_node():
    raw = copy.deepcopy(valid_graph())
    raw["edges"].append(edge("out", "read"))
    assert "graph_cycle" in codes(publish(raw))

    raw = copy.deepcopy(valid_graph())
    raw["nodes"].append(node("lonely", "tool", tool="profile.list", args={}))
    assert "unreachable_node" in codes(publish(raw))


def test_condition_requires_two_labeled_edges():
    raw = {
        "nodes": [
            node("in", "input", fields=[{"key": "topic", "type": "string"}]),
            node("c", "condition", left="{{inputs.topic}}", operator="not_empty"),
            node("out", "output"),
        ],
        "edges": [edge("in", "c", "true"), edge("c", "out", "true")],
    }
    issue_codes = codes(publish(raw))
    assert "condition_invalid" in issue_codes
    assert "edge_invalid" in issue_codes


def test_review_needs_input_and_output_and_title():
    raw = {
        "nodes": [
            node("in", "input", fields=[{"key": "topic", "type": "string"}]),
            node("r", "review", title=""),
            node("out", "output"),
        ],
        "edges": [edge("in", "r"), edge("r", "out")],
    }
    assert "review_invalid" in codes(publish(raw))


def test_unknown_tool_skill_and_agent_tools_are_rejected():
    raw = copy.deepcopy(valid_graph())
    raw["nodes"][1]["data"]["tool"] = "publish.now"
    assert "tool_unknown" in codes(publish(raw))

    raw = copy.deepcopy(valid_graph())
    raw["nodes"][1]["data"] = {"tool": "skill.run", "args": {"skill_id": "nope"}}
    assert "skill_unknown" in codes(publish(raw))

    raw = copy.deepcopy(valid_graph())
    raw["nodes"].insert(
        1,
        node(
            "a",
            "agent",
            goal="写一篇 {{inputs.topic}}",
            tools=["nope.*"],
            budget={"max_steps": 3},
        ),
    )
    raw["edges"] = [edge("in", "a"), edge("a", "read"), edge("read", "out")]
    assert "agent_tools_unknown" in codes(publish(raw))


def test_retry_and_input_fields_are_validated():
    raw = copy.deepcopy(valid_graph())
    raw["nodes"][1]["data"]["retry"] = 9
    assert "node_invalid" in codes(publish(raw))

    raw = copy.deepcopy(valid_graph())
    raw["nodes"][0]["data"]["fields"] = [
        {"key": "Bad Key", "type": "string"}
    ]
    assert "input_invalid" in codes(publish(raw))

    raw = copy.deepcopy(valid_graph())
    raw["nodes"][0]["data"]["fields"] = []
    assert "input_invalid" in codes(publish(raw))


def test_denied_tool_is_only_a_warning():
    result = publish(
        valid_graph(), overrides={"tool:profile.list": "deny"}
    )
    assert result.ok is True
    warnings = [issue for issue in result.issues if issue.severity == "warning"]
    assert [issue.code for issue in warnings] == ["policy_denies_tool"]


def test_size_limit_is_enforced():
    raw = copy.deepcopy(valid_graph())
    raw["nodes"][0]["data"]["fields"] = [
        {"key": "topic", "type": "string", "maxLength": 10**7}
    ]
    # 图本身未超限：maxLength 不是体积。
    assert publish(raw).ok is True
    raw["nodes"][1]["data"]["args"] = {"note": "x" * (300 * 1024)}
    assert "size_exceeded" in codes(publish(raw))
