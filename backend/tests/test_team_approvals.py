import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.db import get_session
from app.deps import get_current_user
from app.main import app
from app.models.agent import AgentApproval, AgentRun, AgentStep
from app.models.base import DEFAULT_TENANT_ID
from app.models.user import User
from app.queue import get_queue


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


async def make_user(db_session, role="owner"):
    user = User(
        id=uuid.uuid4(), tenant_id=DEFAULT_TENANT_ID,
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


async def make_team(db_session, owner):
    parent = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=owner.id, goal="团队目标",
        mode="team", status="waiting_approval",
        heartbeat_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)
    return parent


async def make_waiting_child(db_session, parent, role):
    child = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=parent.user_id, goal="任务",
        mode="role", role=role, parent_run_id=parent.id,
        status="waiting_approval",
    )
    # child.id 在 flush 时才生成，step.run_id 必须先有真实外键值。
    db_session.add(child)
    await db_session.flush()
    step = AgentStep(
        tenant_id=DEFAULT_TENANT_ID, run_id=child.id, step_index=0,
        kind="tool", tool_name="demo.write", status="waiting",
    )
    db_session.add(step)
    await db_session.commit()
    await db_session.refresh(child)
    await db_session.refresh(step)
    approval = AgentApproval(
        tenant_id=DEFAULT_TENANT_ID, run_id=child.id, step_id=step.id,
        tool_name="demo.write", risk="low", payload={},
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    db_session.add(approval)
    await db_session.commit()
    await db_session.refresh(approval)
    return child, approval


@pytest.mark.asyncio
async def test_sibling_pending_keeps_parent_waiting(db_session, api, queue):
    http, state = api
    owner = await make_user(db_session)
    approver = await make_user(db_session)
    state["user"] = approver
    parent = await make_team(db_session, owner)
    _child_a, approval_a = await make_waiting_child(db_session, parent, "creator")
    _child_b, approval_b = await make_waiting_child(db_session, parent, "publisher")

    resp = await http.post(
        f"/api/agent/approvals/{approval_a.id}/decide",
        json={"decision": "approve"},
    )
    assert resp.status_code == 200
    await db_session.refresh(parent)
    assert parent.status == "waiting_approval"
    assert queue.jobs == []

    # 兄弟未决时唤醒回滚会过期会话内全部实例，重新加载审批人与第二条审批
    # 再走第二次决策（与 test_agent_api 的既有口径一致）。
    await db_session.refresh(approver)
    await db_session.refresh(approval_b)
    resp = await http.post(
        f"/api/agent/approvals/{approval_b.id}/decide",
        json={"decision": "approve"},
    )
    assert resp.status_code == 200
    await db_session.refresh(parent)
    assert parent.status == "queued"
    assert queue.jobs == [("run_agent_job", str(parent.id))]
    assert int((parent.usage or {}).get("paused_ms") or 0) > 0


@pytest.mark.asyncio
async def test_reject_fails_team(db_session, api, queue):
    http, state = api
    owner = await make_user(db_session)
    approver = await make_user(db_session)
    state["user"] = approver
    parent = await make_team(db_session, owner)
    child, approval = await make_waiting_child(db_session, parent, "creator")

    resp = await http.post(
        f"/api/agent/approvals/{approval.id}/decide",
        json={"decision": "reject", "note": "不行"},
    )
    assert resp.status_code == 200
    await db_session.refresh(parent)
    await db_session.refresh(child)
    assert child.status == "failed"
    assert child.error_code == "approval_rejected"
    assert parent.status == "failed"
    assert parent.error_code == "approval_rejected"
    assert queue.jobs == []