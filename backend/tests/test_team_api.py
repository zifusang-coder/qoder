import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.db import get_session
from app.deps import get_current_user
from app.main import app
from app.models.agent import AgentApproval, AgentRun, AgentStep, AgentTeamEntry
from app.models.base import DEFAULT_TENANT_ID
from app.models.user import User
from app.queue import get_queue

OTHER_TENANT = "00000000-0000-0000-0000-000000000002"


class FakeQueue:
    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, name, *args):
        self.jobs.append((name, *args))


@pytest.fixture
def queue():
    fake = FakeQueue()
    app.dependency_overrides[get_queue] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_queue, None)


async def make_user(db_session, role="owner", tenant_id=DEFAULT_TENANT_ID):
    user = User(
        id=uuid.uuid4(), tenant_id=tenant_id,
        email=f"{uuid.uuid4().hex[:8]}@example.com", password_hash="x",
        display_name="u", role=role, is_active=True,
        must_change_password=False, is_platform_admin=False, token_version=0,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def api(db_session):
    state = {"user": None}

    async def current_user():
        return state["user"]

    app.dependency_overrides[get_session] = lambda: db_session
    app.dependency_overrides[get_current_user] = current_user
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            yield http, state
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_session, None)


@pytest.mark.asyncio
async def test_roles_get_and_put(db_session, api):
    http, state = api
    state["user"] = await make_user(db_session, role="owner")
    resp = await http.get("/api/agent/team/roles")
    assert resp.status_code == 200
    roles = resp.json()
    assert [r["role"] for r in roles] == ["planner", "scout", "creator", "critic", "publisher"]
    resp = await http.put("/api/agent/team/roles", json={"roles": [
        {"role": "scout", "enabled": False, "tools": ["profile.get"],
         "budget": {"max_tokens": 1000}},
    ]})
    assert resp.status_code == 200
    scout = next(r for r in resp.json() if r["role"] == "scout")
    assert scout["enabled"] is False and scout["tools"] == ["profile.get"]
    resp = await http.put("/api/agent/team/roles", json={"roles": [
        {"role": "scout", "tools": ["publish.execute"]},
    ]})
    assert resp.status_code == 422
    assert resp.json()["code"] == "policy_not_loosenable"
    resp = await http.put("/api/agent/team/roles", json={"roles": [
        {"role": "creator", "enabled": False},
    ]})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_editor_cannot_put_roles(db_session, api):
    http, state = api
    state["user"] = await make_user(db_session, role="editor")
    resp = await http.put("/api/agent/team/roles", json={"roles": []})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_run_validation_and_success(db_session, api, queue):
    http, state = api
    state["user"] = await make_user(db_session, role="editor")
    resp = await http.post("/api/agent/team/runs",
                           json={"goal": "做内容", "roles": ["scout"]})
    assert resp.status_code == 422
    assert resp.json()["code"] == "team_roles_invalid"
    resp = await http.post("/api/agent/team/runs",
                           json={"goal": "做内容", "roles": ["creator"],
                                 "budget": {"max_tokens": 999999}})
    assert resp.status_code == 422
    assert resp.json()["code"] == "budget_invalid"
    resp = await http.post("/api/agent/team/runs",
                           json={"goal": "做内容", "roles": ["scout", "creator"]})
    assert resp.status_code == 202
    body = resp.json()
    assert body["mode"] == "team"
    assert queue.jobs == [("run_agent_job", body["id"])]


@pytest.mark.asyncio
async def test_detail_and_cancel_and_isolation(db_session, api, queue):
    http, state = api
    editor = await make_user(db_session, role="editor")
    state["user"] = editor
    resp = await http.post("/api/agent/team/runs",
                           json={"goal": "做内容", "roles": ["creator"]})
    run_id = resp.json()["id"]
    resp = await http.get(f"/api/agent/team/runs/{run_id}")
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["run"]["id"] == run_id
    assert detail["children"] == [] and detail["entries"] == []
    # 跨租户 404
    other = await make_user(db_session, role="owner", tenant_id=OTHER_TENANT)
    state["user"] = other
    resp = await http.get(f"/api/agent/team/runs/{run_id}")
    assert resp.status_code == 404
    # 非发起人 editor 不能取消
    stranger = await make_user(db_session, role="editor")
    state["user"] = stranger
    resp = await http.post(f"/api/agent/team/runs/{run_id}/cancel")
    assert resp.status_code == 403
    state["user"] = editor
    resp = await http.post(f"/api/agent/team/runs/{run_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    resp = await http.post(f"/api/agent/team/runs/{run_id}/cancel")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_team_retry_forbidden(db_session, api, queue):
    http, state = api
    editor = await make_user(db_session, role="editor")
    state["user"] = editor
    resp = await http.post("/api/agent/team/runs",
                           json={"goal": "做内容", "roles": ["creator"]})
    run_id = resp.json()["id"]
    run = await db_session.get(AgentRun, uuid.UUID(run_id))
    run.status = "failed"
    await db_session.commit()
    resp = await http.post(f"/api/agent/runs/{run_id}/retry")
    assert resp.status_code == 409
    assert resp.json()["code"] == "run_not_retryable"


@pytest.mark.asyncio
async def test_detail_excludes_foreign_tenant_rows(db_session, api, queue):
    http, state = api
    editor = await make_user(db_session, role="editor")
    state["user"] = editor
    resp = await http.post("/api/agent/team/runs",
                           json={"goal": "做内容", "roles": ["creator"]})
    parent_id = uuid.UUID(resp.json()["id"])
    # 本租户的 role 子运行与黑板条目应正常返回
    own_child = AgentRun(
        tenant_id=editor.tenant_id, user_id=editor.id, goal="子任务",
        mode="role", role="scout", status="succeeded", parent_run_id=parent_id,
    )
    own_entry = AgentTeamEntry(
        tenant_id=editor.tenant_id, run_id=parent_id, seq=0,
        kind="note", role="scout", content={"text": "own"},
    )
    # 外租户行直接挂到同一 parent，详情接口不得泄露
    foreign_child = AgentRun(
        tenant_id=OTHER_TENANT, user_id=editor.id, goal="foreign",
        mode="role", role="scout", status="succeeded", parent_run_id=parent_id,
    )
    foreign_entry = AgentTeamEntry(
        tenant_id=OTHER_TENANT, run_id=parent_id, seq=1,
        kind="note", role="scout", content={"text": "foreign"},
    )
    db_session.add_all([own_child, own_entry, foreign_child, foreign_entry])
    await db_session.commit()
    resp = await http.get(f"/api/agent/team/runs/{parent_id}")
    assert resp.status_code == 200
    detail = resp.json()
    assert [c["id"] for c in detail["children"]] == [str(own_child.id)]
    assert [e["seq"] for e in detail["entries"]] == [0]


@pytest.mark.asyncio
async def test_role_child_retry_forbidden(db_session, api, queue):
    http, state = api
    editor = await make_user(db_session, role="editor")
    state["user"] = editor
    child = AgentRun(
        tenant_id=editor.tenant_id, user_id=editor.id, goal="子任务",
        mode="role", role="scout", status="failed",
    )
    db_session.add(child)
    await db_session.commit()
    resp = await http.post(f"/api/agent/runs/{child.id}/retry")
    assert resp.status_code == 409
    assert resp.json()["code"] == "run_not_retryable"


async def _seed_child_approval(db_session, parent, *, role, status,
                               tool="content.create_version", approved=False,
                               created_at=None):
    child = AgentRun(
        tenant_id=parent.tenant_id, user_id=parent.user_id,
        goal=f"{role} 子任务", mode="role", role=role,
        parent_run_id=parent.id, status=status,
    )
    db_session.add(child)
    await db_session.flush()
    step = AgentStep(
        tenant_id=parent.tenant_id, run_id=child.id, step_index=0,
        kind="tool", tool_name=tool, status="waiting",
    )
    db_session.add(step)
    await db_session.flush()
    approval = AgentApproval(
        tenant_id=parent.tenant_id, run_id=child.id, step_id=step.id,
        tool_name=tool, risk="high", payload={"title": "演示"},
        status="approved" if approved else "pending",
    )
    if created_at is not None:
        approval.created_at = created_at
    db_session.add(approval)
    await db_session.commit()
    return child, approval


@pytest.mark.asyncio
async def test_detail_child_pending_approval(db_session, api):
    http, state = api
    owner = await make_user(db_session, role="owner")
    state["user"] = owner
    parent = AgentRun(
        tenant_id=owner.tenant_id, user_id=owner.id, goal="团队目标",
        mode="team", status="waiting_approval", plan={"roles": ["creator"]},
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)
    _child, approval = await _seed_child_approval(
        db_session, parent, role="creator", status="waiting_approval")
    await _seed_child_approval(
        db_session, parent, role="scout", status="succeeded", approved=True)

    resp = await http.get(f"/api/agent/team/runs/{parent.id}")
    assert resp.status_code == 200
    by_role = {c["role"]: c for c in resp.json()["children"]}
    pending = by_role["creator"]["pending_approval"]
    assert pending["id"] == str(approval.id)
    assert pending["tool_name"] == "content.create_version"
    assert pending["payload"] == {"title": "演示"}
    assert by_role["scout"]["pending_approval"] is None


@pytest.mark.asyncio
async def test_detail_approval_earliest_pending_wins(db_session, api):
    http, state = api
    owner = await make_user(db_session, role="owner")
    state["user"] = owner
    parent = AgentRun(
        tenant_id=owner.tenant_id, user_id=owner.id, goal="团队目标",
        mode="team", status="waiting_approval", plan={"roles": ["creator"]},
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)
    now = datetime.now(UTC)
    child, _later = await _seed_child_approval(
        db_session, parent, role="creator", status="waiting_approval",
        created_at=now)
    _child2, earlier = await _seed_child_approval(
        db_session, parent, role="creator", status="waiting_approval",
        created_at=now - timedelta(minutes=5))
    # 两条审批挂在同一子运行上：最早 pending 胜出。
    earlier.run_id = child.id
    await db_session.commit()

    resp = await http.get(f"/api/agent/team/runs/{parent.id}")
    assert resp.status_code == 200
    children = resp.json()["children"]
    assert len(children) == 2
    with_pending = [c for c in children if c["pending_approval"]]
    assert len(with_pending) == 1
    assert with_pending[0]["id"] == str(child.id)
    assert with_pending[0]["pending_approval"]["id"] == str(earlier.id)