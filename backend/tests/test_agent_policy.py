import pytest
from sqlalchemy import select

from app.agent.contracts import ToolSpec
from app.agent.policy import (
    default_action,
    resolve_policy,
    snapshot_policy,
    validate_overrides,
)
from app.agent.registry import ToolRegistry, build_registry
from app.errors import AppError
from app.models.agent import AgentPolicy
from app.models.base import DEFAULT_TENANT_ID


def _spec(name: str, risk: str) -> ToolSpec:
    async def handler(_ctx, _args):
        return None

    return ToolSpec(
        name=name,
        description="d",
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        risk=risk,
        handler=handler,
    )


def test_default_matrix():
    assert default_action("read") == "auto"
    assert default_action("low") == "auto"
    assert default_action("high") == "confirm"


def test_tenant_override_tightens():
    read_tool = _spec("demo.read", "read")
    low_tool = _spec("demo.low", "low")
    overrides = {"tool:demo.read": "confirm", "tool:demo.low": "deny"}
    assert resolve_policy(overrides, read_tool) == "confirm"
    assert resolve_policy(overrides, low_tool) == "deny"


def test_loosening_is_rejected():
    high_tool = _spec("demo.high", "high")
    with pytest.raises(AppError) as exc:
        validate_overrides({"tool:demo.high": "auto"}, {"demo.high": high_tool})
    assert exc.value.code == "policy_not_loosenable"
    assert exc.value.status == 422


def test_unknown_tool_or_action_is_rejected():
    with pytest.raises(AppError) as exc:
        validate_overrides({"tool:missing": "deny"}, {})
    assert exc.value.code == "policy_invalid"
    with pytest.raises(AppError) as exc:
        validate_overrides(
            {"tool:demo.read": "always"}, {"demo.read": _spec("demo.read", "read")}
        )
    assert exc.value.code == "policy_invalid"


def test_non_tool_keys_are_rejected():
    specs = {"demo.read": _spec("demo.read", "read")}
    with pytest.raises(AppError) as exc:
        validate_overrides({"skill:demo": "deny"}, specs)
    assert exc.value.code == "policy_invalid"


def test_snapshot_covers_every_tool():
    specs = {"a.read": _spec("a.read", "read"), "b.high": _spec("b.high", "high")}
    snapshot = snapshot_policy({"tool:a.read": "deny"}, specs)
    assert snapshot == {"a.read": "deny", "b.high": "confirm"}


def test_resolve_policy_falls_back_on_corrupt_stored_action():
    read_tool = _spec("demo.read", "read")
    assert resolve_policy({"tool:demo.read": "always"}, read_tool) == "auto"


async def test_policy_api_roundtrip(auth_client, auth_headers):
    reg = build_registry()
    assert reg.names(), "Task 7 完成后注册表不应为空"
    sample = {f"tool:{reg.names()[0]}": "deny"}
    resp = await auth_client.put(
        "/api/agent/policy", json={"overrides": sample}, headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.json()["overrides"] == sample

    got = await auth_client.get("/api/agent/policy", headers=auth_headers)
    assert got.status_code == 200
    body = got.json()
    assert body["overrides"] == sample
    assert f"tool:{reg.names()[0]}" in body["defaults"]


async def test_policy_api_defaults_to_empty_overrides(auth_client, auth_headers):
    got = await auth_client.get("/api/agent/policy", headers=auth_headers)
    assert got.status_code == 200
    body = got.json()
    assert body["overrides"] == {}
    reg = build_registry()
    assert set(body["defaults"]) == {f"tool:{name}" for name in reg.names()}
    assert all(action == "auto" for action in body["defaults"].values())


async def test_policy_api_persists_plain_dict(auth_client, auth_headers, db_session):
    reg = build_registry()
    sample = {f"tool:{reg.names()[0]}": "confirm"}
    assert (
        await auth_client.put(
            "/api/agent/policy", json={"overrides": sample}, headers=auth_headers
        )
    ).status_code == 200

    # 第二个请求仍然读到同样的覆盖（跨请求持久化）
    got = await auth_client.get("/api/agent/policy", headers=auth_headers)
    assert got.json()["overrides"] == sample

    row = (
        await db_session.execute(
            select(AgentPolicy).where(AgentPolicy.tenant_id == DEFAULT_TENANT_ID)
        )
    ).scalar_one()
    assert row.overrides == sample
    assert all(key.startswith("tool:") for key in row.overrides)


async def test_policy_api_rejects_loosening(auth_client, auth_headers, monkeypatch):
    # 真实注册表暂时没有 high 风险工具，用临时注册表验证 API 层的防放宽门禁
    registry = ToolRegistry()
    registry.register(_spec("demo.high", "high"))
    monkeypatch.setattr("app.api.agent.get_tool_registry", lambda: registry)

    resp = await auth_client.put(
        "/api/agent/policy",
        json={"overrides": {"tool:demo.high": "auto"}},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "policy_not_loosenable"

    got = await auth_client.get("/api/agent/policy", headers=auth_headers)
    assert got.status_code == 200
    assert got.json()["overrides"] == {}
    assert got.json()["defaults"] == {"tool:demo.high": "confirm"}


async def test_policy_api_allows_resetting_override_to_default(
    auth_client, auth_headers
):
    # 覆盖是对 risk 默认值的收紧；撤回覆盖等价于回到默认值，允许
    reg = build_registry()
    assert reg.names(), "Task 7 完成后注册表不应为空"
    name = reg.names()[0]
    tightened = await auth_client.put(
        "/api/agent/policy",
        json={"overrides": {f"tool:{name}": "deny"}},
        headers=auth_headers,
    )
    assert tightened.status_code == 200

    reset = await auth_client.put(
        "/api/agent/policy",
        json={"overrides": {f"tool:{name}": "auto"}},
        headers=auth_headers,
    )
    assert reset.status_code == 200
    assert reset.json()["overrides"] == {f"tool:{name}": "auto"}


async def test_policy_api_rejects_unknown_tool(auth_client, auth_headers):
    resp = await auth_client.put(
        "/api/agent/policy",
        json={"overrides": {"tool:missing.tool": "deny"}},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "policy_invalid"


async def test_policy_api_rejects_unknown_action(auth_client, auth_headers, db_session):
    reg = build_registry()
    assert reg.names(), "Task 7 完成后注册表不应为空"
    resp = await auth_client.put(
        "/api/agent/policy",
        json={"overrides": {f"tool:{reg.names()[0]}": "always"}},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "policy_invalid"
    rows = (await db_session.execute(select(AgentPolicy))).scalars().all()
    assert rows == []


async def test_policy_api_requires_admin(db_session):
    from tests.test_permissions import _role_client

    for role in ("viewer", "editor"):
        async with _role_client(db_session, role) as client:
            got = await client.get("/api/agent/policy")
            assert got.status_code == 403
            assert got.json()["code"] == "forbidden"
            put = await client.put("/api/agent/policy", json={"overrides": {}})
            assert put.status_code == 403
            assert put.json()["code"] == "forbidden"


async def test_policy_is_tenant_scoped(db_session):
    from tests.test_isolation import OTHER_TENANT, _two_tenant_client

    reg = build_registry()
    assert reg.names(), "Task 7 完成后注册表不应为空"
    sample = {f"tool:{reg.names()[0]}": "deny"}

    async with _two_tenant_client(db_session) as (client, headers_a, headers_b):
        assert (await client.get("/api/agent/policy", headers=headers_a)).json()[
            "overrides"
        ] == {}

        saved = await client.put(
            "/api/agent/policy", json={"overrides": sample}, headers=headers_b
        )
        assert saved.status_code == 200
        assert saved.json()["overrides"] == sample

        assert (await client.get("/api/agent/policy", headers=headers_a)).json()[
            "overrides"
        ] == {}
        assert (await client.get("/api/agent/policy", headers=headers_b)).json()[
            "overrides"
        ] == sample

    rows = (await db_session.execute(select(AgentPolicy))).scalars().all()
    assert len(rows) == 1
    assert rows[0].tenant_id == OTHER_TENANT
