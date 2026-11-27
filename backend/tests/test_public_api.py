import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db import get_session
from app.deps import get_current_user
from app.main import app
from app.models.agent import AgentRun
from app.models.audit import AuditLog
from app.models.base import DEFAULT_TENANT_ID
from app.models.tenant import Tenant
from app.models.user import User
from app.models.workflow import Workflow, WorkflowRun, WorkflowVersion
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


@pytest_asyncio.fixture
async def api(db_session):
    await _ensure_tenant(db_session)
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


async def _ensure_tenant(db_session, tenant_id=DEFAULT_TENANT_ID):
    if await db_session.get(Tenant, tenant_id) is None:
        db_session.add(
            Tenant(id=tenant_id, name="默认租户", slug=f"t-{tenant_id[-6:]}",
                   status="active")
        )
        await db_session.commit()


async def make_user(db_session, *, role="admin", tenant_id=DEFAULT_TENANT_ID):
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


async def create_key(http, *, name="CI 集成", scopes=None):
    return await http.post(
        "/api/api-keys",
        json={
            "name": name,
            "scopes": scopes if scopes is not None else ["agent:run", "agent:read"],
        },
    )


def public_headers(secret: str) -> dict[str, str]:
    return {"X-API-Key": secret}


def workflow_graph():
    return {
        "nodes": [
            {
                "id": "in",
                "type": "input",
                "position": {"x": 0, "y": 0},
                "data": {"fields": [{"key": "topic", "type": "string", "required": True}]},
            },
            {"id": "out", "type": "output", "position": {"x": 220, "y": 0}, "data": {}},
        ],
        "edges": [{"source": "in", "target": "out"}],
    }


async def seed_published_workflow(db_session, user, *, tenant_id=DEFAULT_TENANT_ID):
    workflow = Workflow(
        tenant_id=tenant_id,
        name="公共 API 工作流",
        created_by=user.id,
        draft_graph=workflow_graph(),
    )
    db_session.add(workflow)
    await db_session.flush()
    version = WorkflowVersion(
        tenant_id=tenant_id,
        workflow_id=workflow.id,
        version=1,
        graph=workflow_graph(),
        source_revision=0,
        published_by=user.id,
        published_at=datetime.now(UTC),
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(workflow)
    return workflow


async def test_admin_creates_lists_and_revokes_keys(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")

    created = await create_key(
        http, name="数据团队", scopes=["agent:read", "workflow:run"]
    )

    assert created.status_code == 201, created.text
    body = created.json()
    secret = body["secret"]
    assert secret.startswith("nmk_")
    assert body["prefix"] == secret[:12]
    assert body["scopes"] == ["agent:read", "workflow:run"]
    assert body["revoked_at"] is None

    listed = await http.get("/api/api-keys")

    assert listed.status_code == 200, listed.text
    items = listed.json()
    assert [item["id"] for item in items] == [body["id"]]
    assert "secret" not in items[0]
    assert secret not in listed.text

    revoked = await http.delete(f"/api/api-keys/{body['id']}")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked_at"] is not None

    again = await http.delete(f"/api/api-keys/{body['id']}")
    assert again.status_code == 200, again.text
    assert again.json()["revoked_at"] == revoked.json()["revoked_at"]

    actions = (
        await db_session.execute(
            select(AuditLog.action).where(
                AuditLog.tenant_id == DEFAULT_TENANT_ID,
                AuditLog.action.in_(["api_key.create", "api_key.revoke"]),
            )
        )
    ).scalars().all()
    assert sorted(actions) == ["api_key.create", "api_key.revoke"]
    rows = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action.like("api_key.%"))
        )
    ).scalars().all()
    for row in rows:
        assert secret not in str(row.detail)


async def test_key_creation_validates_scopes(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")

    empty = await create_key(http, scopes=[])
    assert empty.status_code == 422, empty.text

    unknown = await create_key(http, scopes=["agent:run", "admin:everything"])
    assert unknown.status_code == 422, unknown.text


async def test_non_admin_cannot_manage_keys(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    key_id = (await create_key(http)).json()["id"]

    state["user"] = await make_user(db_session, role="editor")
    assert (await http.get("/api/api-keys")).status_code == 403
    assert (await create_key(http)).status_code == 403
    assert (await http.delete(f"/api/api-keys/{key_id}")).status_code == 403


async def test_unknown_key_is_rejected(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")

    assert (await http.post("/api/public/agent/runs", json={"goal": "x"})).status_code == 401
    assert (
        await http.post(
            "/api/public/agent/runs",
            json={"goal": "x"},
            headers=public_headers("nmk_not-a-real-key"),
        )
    ).status_code == 401


async def test_scope_is_enforced(api, db_session):
    http, state = api
    user = await make_user(db_session)
    state["user"] = user
    read_only = (await create_key(http, scopes=["agent:read"])).json()["secret"]
    workflow_only = (await create_key(http, scopes=["workflow:read"])).json()["secret"]

    denied = await http.post(
        "/api/public/agent/runs",
        json={"goal": "写一篇周报"},
        headers=public_headers(read_only),
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["code"] == "api_key_scope_denied"

    workflow = await seed_published_workflow(db_session, user)
    denied_wf = await http.post(
        f"/api/public/workflows/{workflow.id}/runs",
        json={"inputs": {"topic": "新品"}},
        headers=public_headers(workflow_only),
    )
    assert denied_wf.status_code == 403, denied_wf.text


async def test_public_agent_run_create_read_and_replay(api, db_session, queue):
    http, state = api
    user = await make_user(db_session)
    state["user"] = user
    secret = (await create_key(http)).json()["secret"]

    created = await http.post(
        "/api/public/agent/runs",
        json={"goal": "写一篇周报"},
        headers={**public_headers(secret), "Idempotency-Key": "public-1"},
    )

    assert created.status_code == 202, created.text
    body = created.json()
    assert body["status"] == "queued"
    assert body["goal"] == "写一篇周报"
    assert body["user_id"] == str(user.id)
    assert queue.jobs == [("run_agent_job", body["id"])]

    replay = await http.post(
        "/api/public/agent/runs",
        json={"goal": "写一篇周报"},
        headers={**public_headers(secret), "Idempotency-Key": "public-1"},
    )
    assert replay.status_code == 202, replay.text
    assert replay.json()["id"] == body["id"]
    assert len(queue.jobs) == 1

    detail = await http.get(
        f"/api/public/agent/runs/{body['id']}", headers=public_headers(secret)
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["id"] == body["id"]
    assert detail.json()["steps"] == []


async def test_public_workflow_run_create_and_read(api, db_session, queue):
    http, state = api
    user = await make_user(db_session)
    state["user"] = user
    secret = (
        await create_key(http, scopes=["workflow:run", "workflow:read"])
    ).json()["secret"]
    workflow = await seed_published_workflow(db_session, user)

    created = await http.post(
        f"/api/public/workflows/{workflow.id}/runs",
        json={"inputs": {"topic": "新品"}},
        headers=public_headers(secret),
    )

    assert created.status_code == 202, created.text
    body = created.json()
    assert body["status"] == "queued"
    assert body["workflow_id"] == str(workflow.id)
    assert body["inputs"] == {"topic": "新品"}
    assert queue.jobs == [("run_workflow_job", body["id"])]

    detail = await http.get(
        f"/api/public/workflow-runs/{body['id']}", headers=public_headers(secret)
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["run"]["id"] == body["id"]
    assert detail.json()["node_runs"] == []

    invalid = await http.post(
        f"/api/public/workflows/{workflow.id}/runs",
        json={"inputs": {}},
        headers=public_headers(secret),
    )
    assert invalid.status_code == 422, invalid.text
    assert invalid.json()["code"] == "workflow_invalid"


async def test_revoked_or_inactive_owner_is_rejected(api, db_session):
    http, state = api
    user = await make_user(db_session)
    state["user"] = user
    created = (await create_key(http)).json()
    secret = created["secret"]

    assert (
        await http.get("/api/public/agent/runs/x", headers=public_headers(secret))
    ).status_code == 404

    await http.delete(f"/api/api-keys/{created['id']}")
    assert (
        await http.get("/api/public/agent/runs/x", headers=public_headers(secret))
    ).status_code == 401

    second = (await create_key(http)).json()["secret"]
    user.is_active = False
    await db_session.commit()
    assert (
        await http.get("/api/public/agent/runs/x", headers=public_headers(second))
    ).status_code == 401


async def test_public_reads_respect_tenant(api, db_session):
    http, state = api
    user = await make_user(db_session)
    state["user"] = user
    secret = (
        await create_key(
            http, scopes=["agent:read", "workflow:read", "workflow:run"]
        )
    ).json()["secret"]

    foreign_agent = AgentRun(
        tenant_id=OTHER_TENANT,
        user_id=uuid.uuid4(),
        goal="别的租户",
        status="queued",
        budget={},
        policy={},
    )
    other_workflow = Workflow(
        tenant_id=OTHER_TENANT,
        name="别的租户工作流",
        created_by=uuid.uuid4(),
        draft_graph=workflow_graph(),
    )
    db_session.add_all([foreign_agent, other_workflow])
    await db_session.flush()
    foreign_version = WorkflowVersion(
        tenant_id=OTHER_TENANT,
        workflow_id=other_workflow.id,
        version=1,
        graph=workflow_graph(),
        source_revision=0,
        published_by=uuid.uuid4(),
        published_at=datetime.now(UTC),
    )
    db_session.add(foreign_version)
    await db_session.flush()
    foreign_wf_run = WorkflowRun(
        tenant_id=OTHER_TENANT,
        workflow_id=other_workflow.id,
        workflow_version_id=foreign_version.id,
        trigger="manual",
        status="queued",
        inputs={},
        policy={},
    )
    db_session.add(foreign_wf_run)
    await db_session.commit()

    assert (
        await http.get(
            f"/api/public/agent/runs/{foreign_agent.id}", headers=public_headers(secret)
        )
    ).status_code == 404
    assert (
        await http.get(
            f"/api/public/workflow-runs/{foreign_wf_run.id}",
            headers=public_headers(secret),
        )
    ).status_code == 404
    assert (
        await http.post(
            f"/api/public/workflows/{other_workflow.id}/runs",
            json={"inputs": {"topic": "x"}},
            headers=public_headers(secret),
        )
    ).status_code == 404


async def test_public_access_is_audited_without_secret(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session)
    secret = (await create_key(http, scopes=["agent:read"])).json()["secret"]

    resp = await http.get("/api/public/agent/runs/nope", headers=public_headers(secret))
    assert resp.status_code == 404

    rows = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "api_key.access")
        )
    ).scalars().all()
    assert len(rows) == 1
    detail = rows[0].detail
    assert detail["endpoint"] == "/api/public/agent/runs/nope"
    assert detail["status"] == 404
    assert detail["key_prefix"] == secret[:12]
    assert "secret" not in str(detail)
    assert secret not in str(detail)
