import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db import get_session
from app.deps import get_current_user
from app.main import app
from app.models.agent import AgentApproval, AgentRun, AgentStep
from app.models.base import DEFAULT_TENANT_ID
from app.models.project import Project
from app.models.user import User
from app.queue import get_queue

OTHER_TENANT = "00000000-0000-0000-0000-000000000002"


class FakeQueue:
    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, name, *args):
        self.jobs.append((name, *args))


class DownQueue:
    async def enqueue_job(self, name, *args):
        raise RuntimeError("redis down")


@pytest.fixture
def queue():
    fake = FakeQueue()
    app.dependency_overrides[get_queue] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_queue, None)


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


async def make_user(db_session, *, role="owner", tenant_id=DEFAULT_TENANT_ID):
    user = User(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        email=f"{uuid.uuid4().hex[:10]}@example.com",
        password_hash="x",
        display_name="测试用户",
        role=role,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def create_run(http, **payload):
    return await http.post(
        "/api/agent/runs", json={"goal": "写一篇周报", **payload}
    )


async def make_pending(db_session, *, creator, tool_name="project.create"):
    run = AgentRun(
        tenant_id=creator.tenant_id,
        user_id=creator.id,
        goal="等待审批",
        status="waiting_approval",
        budget={},
        policy={},
    )
    db_session.add(run)
    await db_session.flush()
    step = AgentStep(
        tenant_id=creator.tenant_id,
        run_id=run.id,
        step_index=1,
        kind="approval",
        tool_name=tool_name,
        status="skipped",
        input={},
    )
    db_session.add(step)
    await db_session.flush()
    approval = AgentApproval(
        tenant_id=creator.tenant_id,
        run_id=run.id,
        step_id=step.id,
        tool_name=tool_name,
        risk="low",
        payload={"title": "草稿"},
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    db_session.add(approval)
    await db_session.commit()
    await db_session.refresh(approval)
    return run, approval


async def test_create_run_queues_and_snapshots_policy(api, queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="editor")

    resp = await create_run(http)

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert body["goal"] == "写一篇周报"
    assert body["user_id"] == str(state["user"].id)
    run = await db_session.get(AgentRun, uuid.UUID(body["id"]))
    assert run.policy["project.create"] == "auto"
    assert run.policy["publish.create_draft_job"] == "auto"
    assert queue.jobs == [("run_agent_job", body["id"])]


async def test_create_run_injects_project_context(api, queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="editor")
    project = Project(tenant_id=DEFAULT_TENANT_ID, title="品牌周报")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    resp = await create_run(http, project_id=str(project.id))

    assert resp.status_code == 202, resp.text
    run = await db_session.get(AgentRun, uuid.UUID(resp.json()["id"]))
    assert "品牌周报" in run.goal
    assert str(project.id) in run.goal


async def test_create_run_rejects_cross_tenant_project(api, queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="editor")
    project = Project(tenant_id=OTHER_TENANT, title="别人的项目")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    resp = await create_run(http, project_id=str(project.id))

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


async def test_viewer_cannot_create_run(api, queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="viewer")

    resp = await create_run(http)

    assert resp.status_code == 403
    assert queue.jobs == []


async def test_idempotency_key_returns_existing_run(api, queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="editor")
    headers = {"Idempotency-Key": "weekly-2026-11-13"}

    first = await http.post(
        "/api/agent/runs", json={"goal": "写一篇周报"}, headers=headers
    )
    second = await http.post(
        "/api/agent/runs", json={"goal": "写一篇周报"}, headers=headers
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert len(queue.jobs) == 1


async def test_active_run_limit(api, queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="editor")
    assert (await create_run(http)).status_code == 202
    assert (await create_run(http)).status_code == 202

    resp = await create_run(http)

    assert resp.status_code == 409
    assert resp.json()["code"] == "agent_run_limit"


async def test_daily_quota_exceeded(api, queue, db_session):
    http, state = api
    user = await make_user(db_session, role="editor")
    state["user"] = user
    db_session.add(
        AgentRun(
            tenant_id=user.tenant_id,
            user_id=user.id,
            goal="昨天用满了",
            status="succeeded",
            usage={"prompt_tokens": 500000, "completion_tokens": 0},
        )
    )
    await db_session.commit()

    resp = await create_run(http)

    assert resp.status_code == 429
    assert resp.json()["code"] == "agent_daily_quota_exceeded"


async def test_budget_is_clamped(api, queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="editor")

    resp = await create_run(
        http,
        budget={
            "max_steps": 999,
            "max_tokens": 999999,
            "max_tool_calls": 999,
            "max_seconds": 9999,
        },
    )

    assert resp.status_code == 202, resp.text
    assert resp.json()["budget"] == {
        "max_steps": 30,
        "max_tokens": 200000,
        "max_tool_calls": 40,
        "max_seconds": 900,
    }


async def test_zero_budget_is_rejected(api, queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="editor")

    resp = await create_run(http, budget={"max_steps": 0})

    assert resp.status_code == 422
    assert resp.json()["code"] == "agent_budget_invalid"


async def test_queue_down_fails_run_with_503(api, db_session):
    http, state = api
    user = await make_user(db_session, role="editor")
    state["user"] = user
    app.dependency_overrides[get_queue] = lambda: DownQueue()
    try:
        resp = await create_run(http)
    finally:
        app.dependency_overrides.pop(get_queue, None)

    assert resp.status_code == 503
    assert resp.json()["code"] == "queue_unavailable"
    run = (
        await db_session.execute(
            select(AgentRun).where(AgentRun.user_id == user.id)
        )
    ).scalar_one()
    assert run.status == "failed"
    assert run.error_code == "queue_unavailable"


async def test_runs_are_tenant_isolated(api, queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="editor")
    own = (await create_run(http)).json()
    foreign = AgentRun(
        tenant_id=OTHER_TENANT,
        user_id=uuid.uuid4(),
        goal="别人的运行",
    )
    db_session.add(foreign)
    await db_session.commit()
    await db_session.refresh(foreign)

    listed = await http.get("/api/agent/runs")
    detail = await http.get(f"/api/agent/runs/{foreign.id}")

    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [own["id"]]
    assert detail.status_code == 404


async def test_cancel_rules(api, queue, db_session):
    http, state = api
    creator = await make_user(db_session, role="editor")
    state["user"] = creator
    run_id = (await create_run(http)).json()["id"]

    other = await make_user(db_session, role="editor")
    state["user"] = other
    forbidden = await http.post(f"/api/agent/runs/{run_id}/cancel")
    assert forbidden.status_code == 403

    state["user"] = creator
    cancelled = await http.post(f"/api/agent/runs/{run_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    again = await http.post(f"/api/agent/runs/{run_id}/cancel")
    assert again.status_code == 409
    assert again.json()["code"] == "agent_run_not_cancellable"


async def test_cancel_closes_pending_approvals(api, queue, db_session):
    http, state = api
    creator = await make_user(db_session, role="editor")
    state["user"] = creator
    run_id = (await create_run(http)).json()["id"]
    run = await db_session.get(AgentRun, uuid.UUID(run_id))
    run.status = "waiting_approval"
    step = AgentStep(
        tenant_id=creator.tenant_id,
        run_id=run.id,
        step_index=1,
        kind="approval",
        tool_name="project.create",
        status="skipped",
    )
    db_session.add(step)
    await db_session.flush()
    approval = AgentApproval(
        tenant_id=creator.tenant_id,
        run_id=run.id,
        step_id=step.id,
        tool_name="project.create",
        risk="low",
        payload={},
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    db_session.add(approval)
    await db_session.commit()

    resp = await http.post(f"/api/agent/runs/{run_id}/cancel")

    assert resp.status_code == 200
    await db_session.refresh(approval)
    assert approval.status == "expired"


async def test_retry_creates_new_run(api, queue, db_session):
    http, state = api
    user = await make_user(db_session, role="editor")
    state["user"] = user
    run_id = (await create_run(http)).json()["id"]
    run = await db_session.get(AgentRun, uuid.UUID(run_id))
    run.status = "failed"
    run.error_code = "agent_internal_error"
    await db_session.commit()

    resp = await http.post(f"/api/agent/runs/{run_id}/retry")

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["id"] != run_id
    assert body["goal"] == run.goal
    assert body["status"] == "queued"

    again = await http.post(f"/api/agent/runs/{run_id}/retry")
    assert again.status_code == 202
    blocked = await http.post(f"/api/agent/runs/{body['id']}/retry")
    assert blocked.status_code == 409


async def test_approvals_exclude_own_runs(api, queue, db_session):
    http, state = api
    creator = await make_user(db_session, role="editor")
    admin = await make_user(db_session, role="admin")
    _, approval = await make_pending(db_session, creator=creator)
    _, own_approval = await make_pending(db_session, creator=admin)
    state["user"] = admin

    resp = await http.get("/api/agent/approvals")

    assert resp.status_code == 200
    ids = [item["id"] for item in resp.json()]
    assert str(approval.id) in ids
    assert str(own_approval.id) not in ids


async def test_creator_cannot_list_approvals(api, queue, db_session):
    http, state = api
    creator = await make_user(db_session, role="editor")
    await make_pending(db_session, creator=creator)
    state["user"] = creator

    resp = await http.get("/api/agent/approvals")

    assert resp.status_code == 403


async def test_decide_approval_enqueues_run(api, queue, db_session):
    http, state = api
    creator = await make_user(db_session, role="editor")
    admin = await make_user(db_session, role="admin")
    run, approval = await make_pending(db_session, creator=creator)
    state["user"] = admin

    resp = await http.post(
        f"/api/agent/approvals/{approval.id}/decide",
        json={"decision": "approve", "note": "可以"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == str(run.id)
    assert resp.json()["status"] == "queued"
    assert queue.jobs == [("run_agent_job", str(run.id))]

    again = await http.post(
        f"/api/agent/approvals/{approval.id}/decide",
        json={"decision": "reject"},
    )
    assert again.status_code == 409
    assert again.json()["code"] == "agent_approval_not_pending"


async def test_decide_rejection_returns_failed_run(api, queue, db_session):
    http, state = api
    creator = await make_user(db_session, role="editor")
    admin = await make_user(db_session, role="admin")
    run, approval = await make_pending(db_session, creator=creator)
    state["user"] = admin

    resp = await http.post(
        f"/api/agent/approvals/{approval.id}/decide",
        json={"decision": "reject", "note": "不合适"},
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"
    assert resp.json()["error_code"] == "approval_rejected"
    assert queue.jobs == []
    await db_session.refresh(run)
    assert run.error_message == "审批已驳回"


async def test_cannot_decide_own_run(api, queue, db_session):
    http, state = api
    owner = await make_user(db_session, role="owner")
    _run, approval = await make_pending(db_session, creator=owner)
    state["user"] = owner

    resp = await http.post(
        f"/api/agent/approvals/{approval.id}/decide",
        json={"decision": "approve"},
    )

    assert resp.status_code == 403
    assert resp.json()["code"] == "forbidden"


async def test_runs_invisible_across_tenants_both_ways(api, queue, db_session):
    http, state = api
    user_a = await make_user(db_session, role="editor")
    user_b = await make_user(
        db_session, role="editor", tenant_id=OTHER_TENANT
    )

    state["user"] = user_a
    run_a = (await create_run(http)).json()
    state["user"] = user_b
    run_b = (await create_run(http)).json()

    state["user"] = user_a
    assert [item["id"] for item in (await http.get("/api/agent/runs")).json()] == [
        run_a["id"]
    ]
    assert (await http.get(f"/api/agent/runs/{run_b['id']}")).status_code == 404
    assert (
        await http.post(f"/api/agent/runs/{run_b['id']}/cancel")
    ).status_code == 404

    state["user"] = user_b
    assert [item["id"] for item in (await http.get("/api/agent/runs")).json()] == [
        run_b["id"]
    ]
    assert (await http.get(f"/api/agent/runs/{run_a['id']}")).status_code == 404
    assert (
        await http.post(f"/api/agent/runs/{run_a['id']}/retry")
    ).status_code == 404


async def test_approvals_invisible_across_tenants_both_ways(api, queue, db_session):
    http, state = api
    creator_a = await make_user(db_session, role="editor")
    admin_a = await make_user(db_session, role="admin")
    creator_b = await make_user(
        db_session, role="editor", tenant_id=OTHER_TENANT
    )
    admin_b = await make_user(db_session, role="admin", tenant_id=OTHER_TENANT)
    _, approval_a = await make_pending(db_session, creator=creator_a)
    _, approval_b = await make_pending(db_session, creator=creator_b)

    state["user"] = admin_a
    listed_a = await http.get("/api/agent/approvals")
    assert listed_a.status_code == 200
    assert [item["id"] for item in listed_a.json()] == [str(approval_a.id)]
    assert (
        await http.post(
            f"/api/agent/approvals/{approval_b.id}/decide",
            json={"decision": "approve"},
        )
    ).status_code == 404
    await db_session.refresh(approval_b)
    assert approval_b.status == "pending"

    state["user"] = admin_b
    listed_b = await http.get("/api/agent/approvals")
    assert listed_b.status_code == 200
    assert [item["id"] for item in listed_b.json()] == [str(approval_b.id)]
    assert (
        await http.post(
            f"/api/agent/approvals/{approval_a.id}/decide",
            json={"decision": "approve"},
        )
    ).status_code == 404
    await db_session.refresh(approval_a)
    assert approval_a.status == "pending"
