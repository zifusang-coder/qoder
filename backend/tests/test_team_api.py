import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.db import get_session
from app.deps import get_current_user
from app.main import app
from app.models.agent import AgentRun
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