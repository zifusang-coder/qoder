import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app.api import agent as agent_api
from app.config import Settings, get_settings
from app.db import get_session
from app.deps import get_current_user
from app.main import app
from app.models.agent import (
    AgentApproval,
    AgentPolicy,
    AgentRun,
    AgentStep,
)
from app.models.base import DEFAULT_TENANT_ID
from app.models.project import Project
from app.models.user import User
from app.models.workflow import (
    Workflow,
    WorkflowApproval,
    WorkflowRun,
    WorkflowVersion,
)
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


async def test_waiting_approval_runs_do_not_count_against_the_cap(
    api, queue, db_session
):
    http, state = api
    user = await make_user(db_session, role="editor")
    state["user"] = user
    for goal in ("等待审批一", "等待审批二"):
        db_session.add(
            AgentRun(
                tenant_id=user.tenant_id,
                user_id=user.id,
                goal=goal,
                status="waiting_approval",
                budget={},
                policy={},
            )
        )
    await db_session.commit()

    # 挂起在人工审批上的运行不占并发额度（只有 queued/running 计数）。
    resp = await create_run(http)

    assert resp.status_code == 202, resp.text
    assert queue.jobs == [("run_agent_job", resp.json()["id"])]


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


async def test_zero_daily_quota_disables_the_limit(
    api, queue, db_session, monkeypatch
):
    http, state = api
    user = await make_user(db_session, role="editor")
    state["user"] = user
    db_session.add(
        AgentRun(
            tenant_id=user.tenant_id,
            user_id=user.id,
            goal="今天的用量",
            status="succeeded",
            usage={"prompt_tokens": 10_000_000, "completion_tokens": 0},
        )
    )
    await db_session.commit()
    # spec §12：`0` 关闭日配额，任何历史用量都不再拦截。
    monkeypatch.setattr(
        agent_api, "get_settings", lambda: Settings(agent_daily_token_quota=0)
    )

    resp = await create_run(http)

    assert resp.status_code == 202, resp.text


async def test_daily_quota_of_one_still_blocks_any_usage(
    api, queue, db_session, monkeypatch
):
    http, state = api
    user = await make_user(db_session, role="editor")
    state["user"] = user
    db_session.add(
        AgentRun(
            tenant_id=user.tenant_id,
            user_id=user.id,
            goal="已用满一个 token",
            status="succeeded",
            usage={"prompt_tokens": 1, "completion_tokens": 0},
        )
    )
    await db_session.commit()
    monkeypatch.setattr(
        agent_api, "get_settings", lambda: Settings(agent_daily_token_quota=1)
    )

    resp = await create_run(http)

    assert resp.status_code == 429
    assert resp.json()["code"] == "agent_daily_quota_exceeded"
    assert queue.jobs == []


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

    # Retargeted from the brief: cancel is editor_access plus self-ownership,
    # so nobody else -- not even an admin -- may cancel another user's run.
    other = await make_user(db_session, role="editor")
    state["user"] = other
    forbidden = await http.post(f"/api/agent/runs/{run_id}/cancel")
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "forbidden"

    admin = await make_user(db_session, role="admin")
    state["user"] = admin
    admin_forbidden = await http.post(f"/api/agent/runs/{run_id}/cancel")
    assert admin_forbidden.status_code == 403

    state["user"] = creator
    cancelled = await http.post(f"/api/agent/runs/{run_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    again = await http.post(f"/api/agent/runs/{run_id}/cancel")
    assert again.status_code == 409
    assert again.json()["code"] == "agent_run_not_cancellable"


async def test_viewer_cannot_cancel_own_run(api, queue, db_session):
    http, state = api
    creator = await make_user(db_session, role="editor")
    state["user"] = creator
    run_id = (await create_run(http)).json()["id"]

    # A run owner demoted to viewer is read-only: editor_access rejects even
    # the creator's own cancellation.
    creator.role = "viewer"
    await db_session.commit()

    resp = await http.post(f"/api/agent/runs/{run_id}/cancel")

    assert resp.status_code == 403
    assert resp.json()["code"] == "forbidden"
    run = await db_session.get(AgentRun, uuid.UUID(run_id))
    await db_session.refresh(run)
    assert run.status == "queued"


async def test_cancel_returns_409_when_run_finished_concurrently(
    api, queue, db_session, monkeypatch
):
    http, state = api
    creator = await make_user(db_session, role="editor")
    state["user"] = creator
    run_id = (await create_run(http)).json()["id"]

    original_owned_run = agent_api._owned_run

    async def finish_after_load(session, raw_id, user):
        run = await original_owned_run(session, raw_id, user)
        # Another writer terminates the run after this request read it but
        # before the conditional update lands.
        await session.execute(
            update(AgentRun)
            .where(AgentRun.id == run.id)
            .values(status="succeeded")
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        return run

    monkeypatch.setattr(agent_api, "_owned_run", finish_after_load)

    resp = await http.post(f"/api/agent/runs/{run_id}/cancel")

    assert resp.status_code == 409
    assert resp.json()["code"] == "agent_run_not_cancellable"
    stored = await db_session.get(AgentRun, uuid.UUID(run_id))
    await db_session.refresh(stored)
    assert stored.status == "succeeded"


async def test_concurrent_cancels_succeed_only_once(
    api, queue, db_session, monkeypatch
):
    http, state = api
    creator = await make_user(db_session, role="editor")
    state["user"] = creator
    run_id = (await create_run(http)).json()["id"]

    original_owned_run = agent_api._owned_run

    async def rival_cancels_first(session, raw_id, user):
        run = await original_owned_run(session, raw_id, user)
        # A second cancel request commits between our load and our update.
        await session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run.id,
                AgentRun.status.in_(agent_api.ACTIVE_STATUSES),
            )
            .values(status="cancelled", finished_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        return run

    monkeypatch.setattr(agent_api, "_owned_run", rival_cancels_first)

    resp = await http.post(f"/api/agent/runs/{run_id}/cancel")

    assert resp.status_code == 409
    assert resp.json()["code"] == "agent_run_not_cancellable"
    stored = await db_session.get(AgentRun, uuid.UUID(run_id))
    await db_session.refresh(stored)
    assert stored.status == "cancelled"


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


async def test_reject_keeps_a_concurrently_cancelled_run_cancelled(
    api, queue, db_session, monkeypatch
):
    http, state = api
    creator = await make_user(db_session, role="editor")
    admin = await make_user(db_session, role="admin")
    run, approval = await make_pending(db_session, creator=creator)
    state["user"] = admin

    original_owned_approval = agent_api._owned_approval

    async def cancel_after_load(session, raw_id, user):
        loaded = await original_owned_approval(session, raw_id, user)
        # 取消在“读取审批”和“写运行状态”之间提交。
        await session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run.id,
                AgentRun.status == "waiting_approval",
            )
            .values(status="cancelled", finished_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        return loaded

    monkeypatch.setattr(agent_api, "_owned_approval", cancel_after_load)

    resp = await http.post(
        f"/api/agent/approvals/{approval.id}/decide",
        json={"decision": "reject", "note": "不合适"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
    stored = await db_session.get(AgentRun, run.id)
    await db_session.refresh(stored)
    # 驳回不得把已取消的运行改写成 failed。
    assert stored.status == "cancelled"
    assert stored.error_code == ""
    # 审批自身的 CAS 独立生效。
    await db_session.refresh(approval)
    assert approval.status == "rejected"


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


async def test_enqueue_failure_releases_idempotency_key(api, db_session):
    http, state = api
    user = await make_user(db_session, role="editor")
    state["user"] = user
    headers = {"Idempotency-Key": "retryable-key"}
    body = {"goal": "写一篇周报"}

    app.dependency_overrides[get_queue] = lambda: DownQueue()
    try:
        first = await http.post("/api/agent/runs", json=body, headers=headers)
    finally:
        app.dependency_overrides.pop(get_queue, None)

    assert first.status_code == 503
    assert first.json()["code"] == "queue_unavailable"
    failed = (
        await db_session.execute(
            select(AgentRun).where(AgentRun.user_id == user.id)
        )
    ).scalars().all()
    assert len(failed) == 1
    assert failed[0].status == "failed"
    assert failed[0].error_code == "queue_unavailable"
    # The row survives as a failure artifact but no longer owns the key, so a
    # replay makes a fresh attempt instead of replaying the failure.
    assert failed[0].idempotency_key is None

    fake = FakeQueue()
    app.dependency_overrides[get_queue] = lambda: fake
    try:
        second = await http.post("/api/agent/runs", json=body, headers=headers)
    finally:
        app.dependency_overrides.pop(get_queue, None)

    assert second.status_code == 202, second.text
    assert second.json()["id"] != str(failed[0].id)
    assert second.json()["status"] == "queued"
    assert fake.jobs == [("run_agent_job", second.json()["id"])]
    rows = (
        await db_session.execute(
            select(AgentRun).where(AgentRun.user_id == user.id)
        )
    ).scalars().all()
    assert len(rows) == 2


async def test_decide_enqueue_failure_rolls_back_the_consent(api, db_session):
    http, state = api
    creator = await make_user(db_session, role="editor")
    admin = await make_user(db_session, role="admin")
    run, approval = await make_pending(db_session, creator=creator)
    state["user"] = admin

    app.dependency_overrides[get_queue] = lambda: DownQueue()
    try:
        failed = await http.post(
            f"/api/agent/approvals/{approval.id}/decide",
            json={"decision": "approve", "note": "可以"},
        )
    finally:
        app.dependency_overrides.pop(get_queue, None)

    assert failed.status_code == 503
    assert failed.json()["code"] == "queue_unavailable"
    await db_session.refresh(approval)
    await db_session.refresh(run)
    assert approval.status == "pending"
    assert approval.decided_by is None
    assert approval.decided_at is None
    assert approval.note == ""
    assert run.status == "waiting_approval"
    assert run.error_code == ""
    assert run.error_message == ""

    fake = FakeQueue()
    app.dependency_overrides[get_queue] = lambda: fake
    try:
        retried = await http.post(
            f"/api/agent/approvals/{approval.id}/decide",
            json={"decision": "approve", "note": "可以"},
        )
    finally:
        app.dependency_overrides.pop(get_queue, None)

    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "queued"
    assert fake.jobs == [("run_agent_job", str(run.id))]


async def test_budget_rejects_unknown_dimension(api, queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="editor")

    resp = await create_run(http, budget={"max_step": 5})

    assert resp.status_code == 422
    assert resp.json()["code"] == "agent_budget_invalid"
    assert queue.jobs == []

    wrong_type = await create_run(http, budget={"max_steps": "many"})
    assert wrong_type.status_code == 422
    assert wrong_type.json()["code"] == "agent_budget_invalid"


async def test_budget_defaults_come_from_settings(api, queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="editor")
    settings = get_settings()

    resp = await create_run(http)

    assert resp.status_code == 202, resp.text
    expected = {
        "max_steps": settings.agent_max_steps_default,
        "max_tokens": settings.agent_token_budget_default,
        "max_tool_calls": settings.agent_max_tool_calls,
        "max_seconds": settings.agent_max_seconds,
    }
    assert resp.json()["budget"] == expected
    run = await db_session.get(AgentRun, uuid.UUID(resp.json()["id"]))
    assert run.budget == expected


def test_step_ceiling_comes_from_settings():
    """``AGENT_MAX_STEPS_CEILING`` bounds both explicit and defaulted steps."""
    settings = Settings(agent_max_steps_ceiling=5)

    explicit = agent_api._clamp_budget({"max_steps": 20}, settings)
    assert explicit["max_steps"] == 5
    assert list(explicit) == list(agent_api.BUDGET_KEYS)

    defaulted = agent_api._clamp_budget({}, settings)
    # The configured default (12) is itself above the lowered ceiling.
    assert defaulted["max_steps"] == 5
    assert defaulted["max_tokens"] == settings.agent_token_budget_default


async def test_idempotency_conflict_across_users_is_generic(
    api, queue, db_session
):
    http, state = api
    user_a = await make_user(db_session, role="editor")
    user_b = await make_user(db_session, role="editor")
    user_a_id, user_b_id = user_a.id, user_b.id
    headers = {"Idempotency-Key": "shared-key"}

    state["user"] = user_a
    first = await http.post(
        "/api/agent/runs", json={"goal": "A 的周报"}, headers=headers
    )
    assert first.status_code == 202

    state["user"] = user_b
    second = await http.post(
        "/api/agent/runs", json={"goal": "B 的周报"}, headers=headers
    )

    assert second.status_code == 409
    assert second.json()["code"] == "agent_idempotency_conflict"
    message = second.json()["message"]
    # The conflict is generic: it must not leak that another user or run exists.
    assert first.json()["id"] not in message
    assert str(user_a_id) not in message
    assert "A 的周报" not in message
    own = (
        await db_session.execute(
            select(AgentRun).where(AgentRun.user_id == user_b_id)
        )
    ).scalars().all()
    assert own == []


async def test_list_runs_populates_pending_approval(api, queue, db_session):
    http, state = api
    creator = await make_user(db_session, role="editor")
    run, approval = await make_pending(db_session, creator=creator)
    state["user"] = creator

    resp = await http.get("/api/agent/runs")

    assert resp.status_code == 200
    item = next(row for row in resp.json() if row["id"] == str(run.id))
    assert item["pending_approval"] is not None
    assert item["pending_approval"]["id"] == str(approval.id)


async def test_list_runs_orders_by_created_at_then_id(api, queue, db_session):
    http, state = api
    user = await make_user(db_session, role="editor")
    state["user"] = user
    same = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
    tied = []
    for index in range(3):
        run = AgentRun(
            tenant_id=DEFAULT_TENANT_ID,
            user_id=user.id,
            goal=f"同秒 {index}",
            created_at=same,
        )
        db_session.add(run)
        tied.append(run)
    older = AgentRun(
        tenant_id=DEFAULT_TENANT_ID,
        user_id=user.id,
        goal="更早",
        created_at=same - timedelta(minutes=1),
    )
    db_session.add(older)
    await db_session.commit()

    resp = await http.get("/api/agent/runs")

    ids = [item["id"] for item in resp.json()]
    expected_tied = sorted((str(run.id) for run in tied), reverse=True)
    assert ids == expected_tied + [str(older.id)]


async def test_run_list_pagination(api, queue, db_session):
    http, state = api
    user = await make_user(db_session, role="editor")
    state["user"] = user
    for index in range(3):
        db_session.add(
            AgentRun(
                tenant_id=DEFAULT_TENANT_ID,
                user_id=user.id,
                goal=f"任务 {index}",
            )
        )
    await db_session.commit()

    all_rows = (await http.get("/api/agent/runs?limit=100")).json()
    first = (await http.get("/api/agent/runs?limit=2&offset=0")).json()
    second = (await http.get("/api/agent/runs?limit=2&offset=2")).json()

    assert len(all_rows) == 3
    assert [item["id"] for item in first + second] == [
        item["id"] for item in all_rows
    ]
    assert (await http.get("/api/agent/runs?limit=0")).status_code == 422


async def test_run_detail_step_pagination(api, queue, db_session):
    http, state = api
    user = await make_user(db_session, role="editor")
    state["user"] = user
    run = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=user.id, goal="多步运行"
    )
    db_session.add(run)
    await db_session.flush()
    for index in range(1, 4):
        db_session.add(
            AgentStep(
                tenant_id=DEFAULT_TENANT_ID,
                run_id=run.id,
                step_index=index,
                kind="tool",
                tool_name="project.create",
            )
        )
    await db_session.commit()

    page = (
        await http.get(f"/api/agent/runs/{run.id}?limit=2&offset=1")
    ).json()

    assert [step["step_index"] for step in page["steps"]] == [2, 3]
    assert (
        await http.get(f"/api/agent/runs/{run.id}?limit=500")
    ).status_code == 422


async def test_approval_list_pagination(api, queue, db_session):
    http, state = api
    creator = await make_user(db_session, role="editor")
    admin = await make_user(db_session, role="admin")
    for _ in range(3):
        await make_pending(db_session, creator=creator)
    state["user"] = admin

    all_ids = [
        item["id"] for item in (await http.get("/api/agent/approvals")).json()
    ]
    head = (await http.get("/api/agent/approvals?limit=2")).json()
    tail = (
        await http.get("/api/agent/approvals?limit=2&offset=2")
    ).json()

    assert len(all_ids) == 3
    assert [item["id"] for item in head + tail] == all_ids
    assert (
        await http.get("/api/agent/approvals?limit=101")
    ).status_code == 422


async def test_retry_expires_source_pending_approvals(api, queue, db_session):
    http, state = api
    creator = await make_user(db_session, role="editor")
    state["user"] = creator
    source, approval = await make_pending(db_session, creator=creator)
    source.status = "failed"
    source.error_code = "agent_internal_error"
    await db_session.commit()

    resp = await http.post(f"/api/agent/runs/{source.id}/retry")

    assert resp.status_code == 202, resp.text
    assert resp.json()["id"] != str(source.id)
    await db_session.refresh(approval)
    assert approval.status == "expired"
    assert approval.decided_at is not None


async def test_retry_snapshots_current_policy(api, queue, db_session):
    http, state = api
    user = await make_user(db_session, role="editor")
    state["user"] = user
    run_id = (await create_run(http)).json()["id"]
    source = await db_session.get(AgentRun, uuid.UUID(run_id))
    assert source.policy["project.create"] == "auto"
    source.status = "failed"
    await db_session.commit()

    db_session.add(
        AgentPolicy(
            tenant_id=DEFAULT_TENANT_ID,
            overrides={"tool:project.create": "deny"},
        )
    )
    await db_session.commit()

    resp = await http.post(f"/api/agent/runs/{run_id}/retry")

    assert resp.status_code == 202, resp.text
    retried = await db_session.get(AgentRun, uuid.UUID(resp.json()["id"]))
    assert retried.policy["project.create"] == "deny"
    await db_session.refresh(source)
    assert source.policy["project.create"] == "auto"


async def make_workflow_child(
    db_session,
    *,
    creator,
    trigger="manual",
    parent_status="waiting_approval",
):
    triggered_by = creator.id if trigger == "manual" else None
    workflow = Workflow(
        tenant_id=creator.tenant_id, name="审批流", created_by=creator.id
    )
    db_session.add(workflow)
    await db_session.flush()
    version = WorkflowVersion(
        tenant_id=creator.tenant_id,
        workflow_id=workflow.id,
        version=1,
        graph={},
        source_revision=0,
        published_by=creator.id,
        published_at=datetime.now(UTC),
    )
    db_session.add(version)
    await db_session.flush()
    parent = WorkflowRun(
        tenant_id=creator.tenant_id,
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        trigger=trigger,
        triggered_by=triggered_by,
        status=parent_status,
        inputs={},
        policy={},
    )
    db_session.add(parent)
    await db_session.flush()
    child = AgentRun(
        tenant_id=creator.tenant_id,
        user_id=creator.id,
        goal="子任务",
        mode="node",
        status="waiting_approval",
        budget={},
        policy={},
        workflow_run_id=parent.id,
        workflow_node_id="writer",
    )
    db_session.add(child)
    await db_session.flush()
    step = AgentStep(
        tenant_id=creator.tenant_id,
        run_id=child.id,
        step_index=1,
        kind="approval",
        tool_name="project.create",
        status="skipped",
        input={},
    )
    db_session.add(step)
    await db_session.flush()
    approval = AgentApproval(
        tenant_id=creator.tenant_id,
        run_id=child.id,
        step_id=step.id,
        tool_name="project.create",
        risk="low",
        payload={"title": "草稿"},
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    db_session.add(approval)
    await db_session.commit()
    return parent, child, approval


async def test_child_approval_wakes_parent_workflow(api, queue, db_session):
    http, state = api
    creator = await make_user(db_session, role="editor")
    reviewer = await make_user(db_session, role="owner")
    parent, child, approval = await make_workflow_child(
        db_session, creator=creator
    )
    state["user"] = reviewer

    resp = await http.post(
        f"/api/agent/approvals/{approval.id}/decide",
        json={"decision": "approve"},
    )

    assert resp.status_code == 200, resp.text
    await db_session.refresh(child)
    await db_session.refresh(parent)
    assert child.status == "queued"
    assert parent.status == "queued"
    assert queue.jobs == [("run_workflow_job", str(parent.id))]


async def test_child_approval_credits_parent_paused_time(
    api, queue, db_session
):
    http, state = api
    creator = await make_user(db_session, role="editor")
    reviewer = await make_user(db_session, role="owner")
    parent, child, approval = await make_workflow_child(
        db_session, creator=creator
    )
    # 父运行 5 分钟前最后一次心跳：审批等待时间必须计入父运行的 paused_ms，
    # 否则恢复后会把等待时长当作已消耗的墙钟预算。
    paused_at = datetime.now(UTC) - timedelta(minutes=5)
    parent.heartbeat_at = paused_at
    await db_session.commit()
    state["user"] = reviewer

    resp = await http.post(
        f"/api/agent/approvals/{approval.id}/decide",
        json={"decision": "approve"},
    )

    assert resp.status_code == 200, resp.text
    await db_session.refresh(child)
    await db_session.refresh(parent)
    assert child.status == "queued"
    assert parent.status == "queued"
    assert int((parent.usage or {}).get("paused_ms") or 0) >= 300_000
    assert queue.jobs == [("run_workflow_job", str(parent.id))]


async def test_child_approval_waits_for_sibling_approval(
    api, queue, db_session
):
    http, state = api
    creator = await make_user(db_session, role="editor")
    reviewer = await make_user(db_session, role="owner")
    parent, _child, approval = await make_workflow_child(
        db_session, creator=creator
    )
    db_session.add(
        WorkflowApproval(
            tenant_id=creator.tenant_id,
            run_id=parent.id,
            node_id="review",
            kind="review",
            title="审核",
            payload={},
            status="pending",
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
    )
    await db_session.commit()
    state["user"] = reviewer

    resp = await http.post(
        f"/api/agent/approvals/{approval.id}/decide",
        json={"decision": "approve"},
    )

    assert resp.status_code == 200, resp.text
    await db_session.refresh(parent)
    assert parent.status == "waiting_approval"
    # 父运行仍是唯一执行者：兄弟审批未决时什么都不入队。
    assert queue.jobs == []


async def test_child_approval_rolls_back_when_parent_enqueue_fails(
    api, queue, db_session
):
    http, state = api
    creator = await make_user(db_session, role="editor")
    reviewer = await make_user(db_session, role="owner")
    parent, child, approval = await make_workflow_child(
        db_session, creator=creator
    )
    # 补偿必须把记账也一起还原：等待时长回滚后再次决策应重新累计。
    paused_at = datetime.now(UTC) - timedelta(minutes=5)
    parent.heartbeat_at = paused_at
    await db_session.commit()
    state["user"] = reviewer
    # ``queue`` fixture owns the override cleanup, so the DownQueue override
    # cannot leak into later tests.
    app.dependency_overrides[get_queue] = lambda: DownQueue()

    resp = await http.post(
        f"/api/agent/approvals/{approval.id}/decide",
        json={"decision": "approve"},
    )

    assert resp.status_code == 503
    await db_session.refresh(parent)
    await db_session.refresh(child)
    await db_session.refresh(approval)
    assert parent.status == "waiting_approval"
    assert child.status == "waiting_approval"
    assert approval.status == "pending"
    assert (parent.usage or {}).get("paused_ms", 0) == 0
    stored = parent.heartbeat_at
    if stored is not None and stored.tzinfo is None:
        stored = stored.replace(tzinfo=UTC)
    assert stored == paused_at
    assert queue.jobs == []


async def test_child_rejection_fails_parent_workflow(api, queue, db_session):
    http, state = api
    creator = await make_user(db_session, role="editor")
    reviewer = await make_user(db_session, role="owner")
    parent, child, approval = await make_workflow_child(
        db_session, creator=creator
    )
    state["user"] = reviewer

    resp = await http.post(
        f"/api/agent/approvals/{approval.id}/decide",
        json={"decision": "reject"},
    )

    assert resp.status_code == 200, resp.text
    await db_session.refresh(child)
    await db_session.refresh(parent)
    assert child.status == "failed"
    assert child.error_code == "approval_rejected"
    assert parent.status == "failed"
    assert parent.error_code == "approval_rejected"
    assert queue.jobs == []


async def test_scheduled_child_approval_visible_to_its_creator(
    api, queue, db_session
):
    http, state = api
    creator = await make_user(db_session, role="owner")
    parent, _child, approval = await make_workflow_child(
        db_session, creator=creator, trigger="schedule"
    )
    state["user"] = creator

    listing = await http.get("/api/agent/approvals")
    approved = await http.post(
        f"/api/agent/approvals/{approval.id}/decide",
        json={"decision": "approve"},
    )

    assert listing.status_code == 200, listing.text
    assert [row["id"] for row in listing.json()] == [str(approval.id)]
    # 定时运行没有发起人：创建人自己也能审批，否则单管理员租户必然卡到过期。
    assert approved.status_code == 200, approved.text
    await db_session.refresh(parent)
    assert parent.status == "queued"
    assert queue.jobs == [("run_workflow_job", str(parent.id))]


async def test_manual_child_approval_still_excludes_its_creator(
    api, queue, db_session
):
    http, state = api
    creator = await make_user(db_session, role="owner")
    _parent, _child, approval = await make_workflow_child(
        db_session, creator=creator, trigger="manual"
    )
    state["user"] = creator

    listing = await http.get("/api/agent/approvals")
    decided = await http.post(
        f"/api/agent/approvals/{approval.id}/decide",
        json={"decision": "approve"},
    )

    assert listing.status_code == 200, listing.text
    assert listing.json() == []
    assert decided.status_code == 403
    assert queue.jobs == []


async def test_agent_approval_list_keeps_independent_runs(
    api, queue, db_session
):
    http, state = api
    creator = await make_user(db_session, role="editor")
    reviewer = await make_user(db_session, role="owner")
    _parent, _child, workflow_approval = await make_workflow_child(
        db_session, creator=creator
    )
    independent = AgentRun(
        tenant_id=creator.tenant_id,
        user_id=creator.id,
        goal="独立任务",
        status="waiting_approval",
        budget={},
        policy={},
    )
    db_session.add(independent)
    await db_session.flush()
    step = AgentStep(
        tenant_id=creator.tenant_id,
        run_id=independent.id,
        step_index=1,
        kind="approval",
        tool_name="project.create",
        status="skipped",
        input={},
    )
    db_session.add(step)
    await db_session.flush()
    standalone = AgentApproval(
        tenant_id=creator.tenant_id,
        run_id=independent.id,
        step_id=step.id,
        tool_name="project.create",
        risk="low",
        payload={},
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    db_session.add(standalone)
    await db_session.commit()
    state["user"] = reviewer

    listing = await http.get("/api/agent/approvals")

    assert listing.status_code == 200, listing.text
    ids = {row["id"] for row in listing.json()}
    assert ids == {str(standalone.id), str(workflow_approval.id)}
