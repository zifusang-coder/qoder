import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app.api import workflow_runs as workflow_runs_api
from app.api import workflows as workflows_api
from app.config import Settings
from app.db import get_session
from app.deps import get_current_user
from app.main import app
from app.models.agent import AgentApproval, AgentRun, AgentStep
from app.models.base import DEFAULT_TENANT_ID
from app.models.user import User
from app.models.workflow import (
    Workflow,
    WorkflowApproval,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowVersion,
)
from app.queue import get_queue
from app.workflow import approvals as workflow_approvals

OTHER_TENANT = "00000000-0000-0000-0000-000000000002"


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


def graph(*, days="{{inputs.days}}"):
    return {
        "nodes": [
            {
                "id": "in",
                "type": "input",
                "position": {"x": 0, "y": 0},
                "data": {
                    "fields": [
                        {"key": "days", "type": "number", "required": True}
                    ]
                },
            },
            {
                "id": "read",
                "type": "tool",
                "position": {"x": 220, "y": 0},
                "data": {"tool": "analytics.summary", "args": {"days": days}},
            },
            {
                "id": "out",
                "type": "output",
                "position": {"x": 440, "y": 0},
                "data": {},
            },
        ],
        "edges": [
            {"source": "in", "target": "read"},
            {"source": "read", "target": "out"},
        ],
    }


async def create_via_api(http, *, name="数据复盘", body=None):
    return await http.post(
        "/api/workflows",
        json={"name": name, "graph": body if body is not None else graph()},
    )


async def publish_version(db_session, workflow: Workflow) -> WorkflowVersion:
    version = WorkflowVersion(
        tenant_id=workflow.tenant_id,
        workflow_id=workflow.id,
        version=1,
        graph=workflow.draft_graph or {"nodes": [], "edges": []},
        source_revision=workflow.draft_revision,
        published_by=workflow.created_by,
        published_at=datetime.now(UTC),
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return version


async def test_admin_creates_lists_and_reads_detail(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")

    created = await create_via_api(http)

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "数据复盘"
    assert body["latest_version"] is None
    assert body["has_unpublished"] is True
    assert body["draft_revision"] == 0

    listed = await http.get("/api/workflows")

    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [body["id"]]
    assert listed.json()[0]["latest_version"] is None

    detail = await http.get(f"/api/workflows/{body['id']}")

    assert detail.status_code == 200, detail.text
    assert detail.json()["draft_graph"] == graph()
    assert detail.json()["latest_published"] is None
    assert detail.json()["schedules"] == []


async def test_role_matrix_blocks_editor_and_viewer_writes(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id = (await create_via_api(http)).json()["id"]

    state["user"] = await make_user(db_session, role="editor")
    assert (await http.get(f"/api/workflows/{workflow_id}")).status_code == 200
    assert (
        await http.patch(
            f"/api/workflows/{workflow_id}",
            json={"revision": 0, "name": "改名"},
        )
    ).status_code == 403
    assert (
        await create_via_api(http, name="越权创建")
    ).status_code == 403
    assert (
        await http.post(f"/api/workflows/{workflow_id}/publish",
                        json={"revision": 0})
    ).status_code == 403
    assert (await http.get("/api/workflows/templates")).status_code == 403

    state["user"] = await make_user(db_session, role="viewer")
    assert (await http.get("/api/workflows")).status_code == 200
    assert (
        await http.delete(f"/api/workflows/{workflow_id}")
    ).status_code == 403
    assert (
        await http.post(f"/api/workflows/{workflow_id}/validate")
    ).status_code == 403


async def test_patch_is_optimistic_and_rejects_bad_shape(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id = (await create_via_api(http)).json()["id"]

    saved = await http.patch(
        f"/api/workflows/{workflow_id}",
        json={"revision": 0, "name": "周度复盘", "description": "每周一跑"},
    )

    assert saved.status_code == 200, saved.text
    assert saved.json()["draft_revision"] == 1
    assert saved.json()["name"] == "周度复盘"

    stale = await http.patch(
        f"/api/workflows/{workflow_id}",
        json={"revision": 0, "name": "旧会话"},
    )

    assert stale.status_code == 409
    assert stale.json()["code"] == "workflow_conflict"

    broken = await http.patch(
        f"/api/workflows/{workflow_id}",
        json={"revision": 1, "draft_graph": {"nodes": "不是数组"}},
    )

    assert broken.status_code == 422
    assert broken.json()["code"] == "workflow_invalid"
    assert broken.json()["detail"]["issues"]


async def test_draft_save_rejects_revision_race_atomically(
    api, db_session, monkeypatch
):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    created = await create_via_api(http, name="原始名称")
    workflow_id = uuid.UUID(created.json()["id"])

    real_owned = workflows_api._owned

    async def racing_owned(session, raw, user):
        row = await real_owned(session, raw, user)
        # 模拟另一个会话在读与写之间提交：库里已到 revision 1，
        # 而本次请求手里的 ORM 对象仍是旧的 revision 0。
        await session.execute(
            update(Workflow)
            .where(Workflow.id == row.id)
            .values(draft_revision=Workflow.draft_revision + 1)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        return row

    monkeypatch.setattr(workflows_api, "_owned", racing_owned)

    raced = await http.patch(
        f"/api/workflows/{workflow_id}",
        json={"revision": 0, "name": "被覆盖"},
    )

    assert raced.status_code == 409, raced.text
    assert raced.json()["code"] == "workflow_conflict"

    db_session.expire_all()
    stored = (
        await db_session.execute(
            select(Workflow).where(Workflow.id == workflow_id)
        )
    ).scalar_one()
    assert stored.name == "原始名称"
    assert stored.draft_revision == 1


async def test_publish_runs_authoritative_validation(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    # 结构合法但语义不完整的草稿：可以保存，不能发布。
    draft = {
        "nodes": [
            {"id": "out", "type": "output", "position": {}, "data": {}}
        ],
        "edges": [],
    }
    workflow_id = (await create_via_api(http, body=draft)).json()["id"]

    saved = await http.patch(
        f"/api/workflows/{workflow_id}",
        json={"revision": 0, "draft_graph": draft},
    )
    assert saved.status_code == 200, saved.text

    validated = await http.post(f"/api/workflows/{workflow_id}/validate")
    assert validated.status_code == 200
    assert validated.json()["ok"] is False
    assert any(
        issue["code"] == "input_invalid"
        for issue in validated.json()["issues"]
    )

    published = await http.post(
        f"/api/workflows/{workflow_id}/publish", json={"revision": 1}
    )

    assert published.status_code == 422
    assert published.json()["code"] == "workflow_invalid"
    assert published.json()["detail"]["issues"]


async def test_publish_versions_are_immutable_and_restorable(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id = (await create_via_api(http)).json()["id"]

    first = await http.post(
        f"/api/workflows/{workflow_id}/publish",
        json={"revision": 0, "note": "首版"},
    )

    assert first.status_code == 201, first.text
    assert first.json()["version"] == 1
    assert first.json()["source_revision"] == 0
    assert first.json()["note"] == "首版"

    changed = graph(days=7)
    saved = await http.patch(
        f"/api/workflows/{workflow_id}",
        json={"revision": 0, "draft_graph": changed},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["has_unpublished"] is True

    second = await http.post(
        f"/api/workflows/{workflow_id}/publish", json={"revision": 1}
    )
    assert second.status_code == 201, second.text
    assert second.json()["version"] == 2

    versions = await http.get(f"/api/workflows/{workflow_id}/versions")

    assert versions.status_code == 200
    assert [item["version"] for item in versions.json()] == [2, 1]
    assert "graph" not in versions.json()[0]

    one = await http.get(f"/api/workflows/{workflow_id}/versions/1")

    assert one.status_code == 200
    assert one.json()["graph"] == graph()

    restored = await http.post(
        f"/api/workflows/{workflow_id}/versions/1/restore"
    )

    assert restored.status_code == 200, restored.text
    assert restored.json()["draft_graph"] == graph()
    assert restored.json()["draft_revision"] == 2
    assert restored.json()["has_unpublished"] is True
    # 恢复不改动已发布版本。
    assert (
        await http.get(f"/api/workflows/{workflow_id}/versions/1")
    ).json()["graph"] == graph()


async def test_publish_rejects_stale_revision(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id = (await create_via_api(http)).json()["id"]

    resp = await http.post(
        f"/api/workflows/{workflow_id}/publish", json={"revision": 3}
    )

    assert resp.status_code == 409
    assert resp.json()["code"] == "workflow_conflict"


async def test_cross_tenant_workflows_are_404(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    foreign = Workflow(
        tenant_id=OTHER_TENANT,
        name="别人的流",
        created_by=uuid.uuid4(),
        draft_graph=graph(),
    )
    db_session.add(foreign)
    await db_session.commit()
    await db_session.refresh(foreign)

    for method, path, kwargs in (
        ("get", f"/api/workflows/{foreign.id}", {}),
        ("patch", f"/api/workflows/{foreign.id}", {"json": {"revision": 0}}),
        ("delete", f"/api/workflows/{foreign.id}", {}),
        ("post", f"/api/workflows/{foreign.id}/publish", {"json": {"revision": 0}}),
        ("post", f"/api/workflows/{foreign.id}/validate", {}),
    ):
        resp = await getattr(http, method)(path, **kwargs)
        assert resp.status_code == 404, (method, path, resp.text)
        assert resp.json()["code"] == "workflow_not_found"


async def test_delete_is_soft_and_blocked_by_active_run(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id = (await create_via_api(http)).json()["id"]
    workflow = await db_session.get(Workflow, uuid.UUID(workflow_id))
    version = await publish_version(db_session, workflow)
    run = WorkflowRun(
        tenant_id=DEFAULT_TENANT_ID,
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        status="running",
        inputs={},
        policy={},
    )
    db_session.add(run)
    await db_session.commit()

    blocked = await http.delete(f"/api/workflows/{workflow_id}")

    assert blocked.status_code == 409
    assert blocked.json()["code"] == "workflow_run_active"

    run.status = "succeeded"
    await db_session.commit()

    deleted = await http.delete(f"/api/workflows/{workflow_id}")

    assert deleted.status_code == 204
    assert (await http.get(f"/api/workflows/{workflow_id}")).status_code == 404
    assert (await http.get("/api/workflows")).json() == []
    stored = await db_session.get(Workflow, uuid.UUID(workflow_id))
    await db_session.refresh(stored)
    assert stored.archived_at is not None


async def test_templates_list_and_instantiate(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")

    listed = await http.get("/api/workflows/templates")

    assert listed.status_code == 200, listed.text
    keys = [item["key"] for item in listed.json()]
    assert keys == [
        "weekly_topic",
        "multi_platform_rewrite",
        "post_publish_review",
    ]
    assert all(item["parameters"] for item in listed.json())

    created = await http.post(
        "/api/workflows/templates/weekly_topic/instantiate",
        json={"name": "我的周更"},
    )

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "我的周更"
    assert body["has_unpublished"] is True
    detail = await http.get(f"/api/workflows/{body['id']}")
    assert len(detail.json()["draft_graph"]["nodes"]) == 5

    missing = await http.post(
        "/api/workflows/templates/nope/instantiate", json={"name": "x"}
    )
    assert missing.status_code == 404


async def test_agent_tools_endpoint_exposes_risk_and_schema(api, db_session):
    http, state = api

    state["user"] = await make_user(db_session, role="editor")
    resp = await http.get("/api/agent/tools")

    assert resp.status_code == 200, resp.text
    tools = {item["name"]: item for item in resp.json()}
    assert tools["content.create_version"]["risk"] == "low"
    assert tools["analytics.summary"]["risk"] == "read"
    assert tools["project.create"]["parameters"]["type"] == "object"
    assert tools["publish.create_draft_job"]["description"]

    state["user"] = await make_user(db_session, role="viewer")
    assert (await http.get("/api/agent/tools")).status_code == 403


WEEKLY = {"kind": "weekly", "weekdays": [1, 4], "time": "08:30", "timezone": "Asia/Shanghai"}
DAILY = {"kind": "daily", "time": "09:00", "timezone": "Asia/Shanghai"}


async def publish_once(http, workflow_id, revision=0):
    return await http.post(
        f"/api/workflows/{workflow_id}/publish", json={"revision": revision}
    )


async def test_schedule_requires_published_version(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id = (await create_via_api(http)).json()["id"]

    resp = await http.post(
        f"/api/workflows/{workflow_id}/schedules",
        json={"cadence": DAILY, "inputs": {"days": 7}},
    )

    assert resp.status_code == 409
    assert resp.json()["code"] == "workflow_not_published"


async def test_schedule_crud_and_next_run(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id = (await create_via_api(http)).json()["id"]
    assert (await publish_once(http, workflow_id)).status_code == 201

    created = await http.post(
        f"/api/workflows/{workflow_id}/schedules",
        json={"cadence": DAILY, "inputs": {"days": 7}},
    )

    assert created.status_code == 201, created.text
    schedule = created.json()
    assert schedule["enabled"] is True
    assert schedule["next_run_at"] is not None
    assert schedule["cadence"]["kind"] == "daily"

    listed = await http.get(f"/api/workflows/{workflow_id}/schedules")
    assert [item["id"] for item in listed.json()] == [schedule["id"]]

    disabled = await http.patch(
        f"/api/workflows/{workflow_id}/schedules/{schedule['id']}",
        json={"cadence": WEEKLY, "enabled": False},
    )

    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False
    assert disabled.json()["next_run_at"] is None
    assert disabled.json()["cadence"]["weekdays"] == [1, 4]

    enabled = await http.patch(
        f"/api/workflows/{workflow_id}/schedules/{schedule['id']}",
        json={"enabled": True},
    )
    assert enabled.status_code == 200
    assert enabled.json()["next_run_at"] is not None

    bad_cadence = await http.patch(
        f"/api/workflows/{workflow_id}/schedules/{schedule['id']}",
        json={"cadence": {"kind": "hourly", "time": "09:00", "timezone": "UTC"}},
    )
    assert bad_cadence.status_code == 422
    assert bad_cadence.json()["code"] == "workflow_schedule_invalid"

    bad_inputs = await http.patch(
        f"/api/workflows/{workflow_id}/schedules/{schedule['id']}",
        json={"inputs": {"days": "七天"}},
    )
    assert bad_inputs.status_code == 422
    assert bad_inputs.json()["code"] == "workflow_schedule_invalid"
    assert bad_inputs.json()["detail"]["issues"]

    deleted = await http.delete(
        f"/api/workflows/{workflow_id}/schedules/{schedule['id']}"
    )
    assert deleted.status_code == 204
    assert (await http.get(f"/api/workflows/{workflow_id}/schedules")).json() == []


async def test_schedule_role_matrix_and_tenant_isolation(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id = (await create_via_api(http)).json()["id"]
    assert (await publish_once(http, workflow_id)).status_code == 201

    state["user"] = await make_user(db_session, role="editor")
    denied = await http.post(
        f"/api/workflows/{workflow_id}/schedules",
        json={"cadence": DAILY, "inputs": {"days": 7}},
    )
    assert denied.status_code == 403
    assert (
        await http.get(f"/api/workflows/{workflow_id}/schedules")
    ).status_code == 200

    foreign = Workflow(
        tenant_id=OTHER_TENANT,
        name="别人的流",
        created_by=uuid.uuid4(),
        draft_graph=graph(),
    )
    db_session.add(foreign)
    await db_session.commit()
    await db_session.refresh(foreign)

    resp = await http.get(f"/api/workflows/{foreign.id}/schedules")
    assert resp.status_code == 404
    assert resp.json()["code"] == "workflow_not_found"


async def test_schedule_pins_the_requested_version(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id = (await create_via_api(http)).json()["id"]
    assert (await publish_once(http, workflow_id)).status_code == 201
    saved = await http.patch(
        f"/api/workflows/{workflow_id}",
        json={"revision": 0, "draft_graph": graph(days=30)},
    )
    assert saved.status_code == 200
    assert (
        await publish_once(http, workflow_id, revision=1)
    ).status_code == 201

    v1 = await http.get(f"/api/workflows/{workflow_id}/versions/1")

    created = await http.post(
        f"/api/workflows/{workflow_id}/schedules",
        json={"cadence": DAILY, "inputs": {"days": 7}, "version": 1},
    )

    assert created.status_code == 201, created.text
    assert created.json()["workflow_version_id"] == v1.json()["id"]


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


@pytest.fixture
def down_queue():
    app.dependency_overrides[get_queue] = lambda: DownQueue()
    yield
    app.dependency_overrides.pop(get_queue, None)


async def published(http, *, name="数据复盘", body=None):
    workflow_id = (await create_via_api(http, name=name, body=body)).json()["id"]
    assert (await publish_once(http, workflow_id)).status_code == 201
    version = (
        await http.get(f"/api/workflows/{workflow_id}/versions/1")
    ).json()
    return workflow_id, version["id"]


async def start_run(http, workflow_id, *, inputs=None, version=None, key=None):
    headers = {"Idempotency-Key": key} if key else {}
    payload = {"version": version}
    if inputs is not None:
        payload["inputs"] = inputs
    else:
        payload["inputs"] = {"days": 7}
    return await http.post(
        f"/api/workflows/{workflow_id}/runs", json=payload, headers=headers
    )


async def park(db_session, run_id, *, node_id="review", title="请确认草稿"):
    run = await db_session.get(WorkflowRun, uuid.UUID(run_id))
    run.status = "waiting_approval"
    db_session.add(
        WorkflowNodeRun(
            tenant_id=run.tenant_id,
            run_id=run.id,
            node_id=node_id,
            kind="review",
            status="waiting",
        )
    )
    approval = await workflow_approvals.create_approval(
        db_session,
        run=run,
        node_id=node_id,
        kind="review",
        title=title,
        payload={"summary": "草稿"},
    )
    await db_session.commit()
    await db_session.refresh(approval)
    return run, approval


async def add_child_pending(db_session, run: WorkflowRun) -> AgentRun:
    child = AgentRun(
        tenant_id=run.tenant_id,
        user_id=uuid.uuid4(),
        goal="节点任务",
        mode="node",
        status="waiting_approval",
        workflow_run_id=run.id,
        workflow_node_id="agent",
        budget={},
        policy={},
    )
    db_session.add(child)
    await db_session.flush()
    step = AgentStep(
        tenant_id=run.tenant_id, run_id=child.id, step_index=0, kind="tool"
    )
    db_session.add(step)
    await db_session.flush()
    db_session.add(
        AgentApproval(
            tenant_id=run.tenant_id,
            run_id=child.id,
            step_id=step.id,
            tool_name="content.create_version",
            risk="high",
            payload={},
        )
    )
    await db_session.commit()
    return child


async def scheduled_run(
    db_session, workflow_id: str, version_id: str, *, status="queued"
):
    run = WorkflowRun(
        tenant_id=DEFAULT_TENANT_ID,
        workflow_id=uuid.UUID(workflow_id),
        workflow_version_id=uuid.UUID(version_id),
        trigger="schedule",
        triggered_by=None,
        scheduled_for=datetime.now(UTC),
        status=status,
        inputs={"days": 7},
        policy={},
    )
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)
    return run


async def foreign_run(db_session):
    workflow = Workflow(
        tenant_id=OTHER_TENANT, name="别人的流", created_by=uuid.uuid4()
    )
    db_session.add(workflow)
    await db_session.flush()
    version = WorkflowVersion(
        tenant_id=OTHER_TENANT,
        workflow_id=workflow.id,
        version=1,
        graph=graph(),
        source_revision=0,
        published_by=uuid.uuid4(),
        published_at=datetime.now(UTC),
    )
    db_session.add(version)
    await db_session.flush()
    run = WorkflowRun(
        tenant_id=OTHER_TENANT,
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        trigger="manual",
        status="waiting_approval",
        inputs={},
        policy={},
    )
    db_session.add(run)
    await db_session.flush()
    approval = await workflow_approvals.create_approval(
        db_session,
        run=run,
        node_id="review",
        kind="review",
        title="别人的审批",
        payload={},
    )
    await db_session.commit()
    await db_session.refresh(approval)
    return run, approval


async def test_start_run_pins_published_version_and_enqueues(api, queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id, version_id = await published(http)
    editor = await make_user(db_session, role="editor")
    state["user"] = editor

    resp = await start_run(http, workflow_id)

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert body["trigger"] == "manual"
    assert body["triggered_by"] == str(editor.id)
    assert body["version"] == 1
    assert body["workflow_version_id"] == version_id
    assert body["inputs"] == {"days": 7}
    assert queue.jobs == [("run_workflow_job", body["id"])]

    detail = await http.get(f"/api/workflow-runs/{body['id']}")

    assert detail.status_code == 200, detail.text
    assert detail.json()["run"]["id"] == body["id"]
    assert detail.json()["node_runs"] == []
    assert detail.json()["approvals"] == []
    assert detail.json()["result"] == {}


async def test_start_run_validates_inputs_before_creating(api, queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    unpublished = (await create_via_api(http, name="未发布")).json()["id"]
    state["user"] = await make_user(db_session, role="editor")

    missing = await start_run(http, unpublished)

    assert missing.status_code == 409
    assert missing.json()["code"] == "workflow_not_published"

    state["user"] = await make_user(db_session, role="admin")
    workflow_id, _ = await published(http)
    state["user"] = await make_user(db_session, role="editor")

    wrong_type = await start_run(http, workflow_id, inputs={"days": "七天"})
    assert wrong_type.status_code == 422
    assert wrong_type.json()["code"] == "workflow_invalid"
    assert wrong_type.json()["detail"]["issues"]

    absent = await start_run(http, workflow_id, inputs={})
    assert absent.status_code == 422

    unknown = await start_run(http, workflow_id, inputs={"days": 7, "x": 1})
    assert unknown.status_code == 422

    assert queue.jobs == []


async def test_start_run_is_idempotent_per_user(api, queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id, _ = await published(http)
    first_user = await make_user(db_session, role="editor")
    state["user"] = first_user

    first = await start_run(http, workflow_id, key="same-key")
    second = await start_run(http, workflow_id, key="same-key")

    assert first.status_code == 202, first.text
    assert second.status_code == 202
    assert second.json()["id"] == first.json()["id"]
    assert queue.jobs == [("run_workflow_job", first.json()["id"])]

    state["user"] = await make_user(db_session, role="editor")
    other = await start_run(http, workflow_id, key="same-key")

    assert other.status_code == 409
    assert other.json()["code"] == "workflow_conflict"


async def test_start_run_enforces_active_limit(api, queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id, _ = await published(http)
    state["user"] = await make_user(db_session, role="editor")

    first = await start_run(http, workflow_id)
    second = await start_run(http, workflow_id)
    assert first.status_code == 202
    assert second.status_code == 202

    blocked = await start_run(http, workflow_id)

    assert blocked.status_code == 409
    assert blocked.json()["code"] == "workflow_run_limit"

    row = await db_session.get(WorkflowRun, uuid.UUID(first.json()["id"]))
    row.status = "succeeded"
    await db_session.commit()

    assert (await start_run(http, workflow_id)).status_code == 202


async def test_start_run_checks_daily_quota(api, queue, db_session, monkeypatch):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id, _ = await published(http)
    editor = await make_user(db_session, role="editor")
    state["user"] = editor
    db_session.add(
        AgentRun(
            tenant_id=DEFAULT_TENANT_ID,
            user_id=editor.id,
            goal="消耗配额",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )
    )
    await db_session.commit()
    monkeypatch.setattr(
        workflow_runs_api,
        "get_settings",
        lambda: Settings(agent_daily_token_quota=1),
    )

    resp = await start_run(http, workflow_id)

    assert resp.status_code == 429
    assert resp.json()["code"] == "agent_daily_quota_exceeded"
    assert queue.jobs == []


async def test_start_queue_failure_fails_run_and_releases_key(
    api, down_queue, db_session
):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id, _ = await published(http)
    state["user"] = await make_user(db_session, role="editor")

    first = await start_run(http, workflow_id, key="retry-me")

    assert first.status_code == 503
    assert first.json()["code"] == "queue_unavailable"
    rows = (
        (await db_session.execute(select(WorkflowRun))).scalars().all()
    )
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].error_code == "queue_unavailable"
    assert rows[0].idempotency_key is None

    again = await start_run(http, workflow_id, key="retry-me")

    assert again.status_code == 503
    total = (await db_session.execute(select(WorkflowRun))).scalars().all()
    assert len(total) == 2


async def test_run_detail_aggregates_nodes_and_both_channels(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id, _ = await published(http)
    state["user"] = await make_user(db_session, role="editor")
    run = (await start_run(http, workflow_id)).json()
    row, _ = await park(db_session, run["id"])
    await add_child_pending(db_session, row)

    detail = await http.get(f"/api/workflow-runs/{run['id']}")

    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert len(body["node_runs"]) == 1
    assert body["node_runs"][0]["node_id"] == "review"
    assert body["node_runs"][0]["status"] == "waiting"
    sources = {item["source"] for item in body["approvals"]}
    assert sources == {"workflow", "agent"}
    assert all(item["run_id"] == run["id"] for item in body["approvals"])
    assert "raw_output" not in detail.text

    _, foreign = await foreign_run(db_session)
    foreign_detail = await http.get(f"/api/workflow-runs/{foreign.run_id}")

    assert foreign_detail.status_code == 404
    assert foreign_detail.json()["code"] == "workflow_run_not_found"


async def test_run_list_filters_by_status(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id, _ = await published(http)
    state["user"] = await make_user(db_session, role="editor")
    first = (await start_run(http, workflow_id)).json()
    second = (await start_run(http, workflow_id)).json()
    row = await db_session.get(WorkflowRun, uuid.UUID(second["id"]))
    row.status = "failed"
    await db_session.commit()

    failed = await http.get(f"/api/workflows/{workflow_id}/runs?status=failed")
    all_runs = await http.get(f"/api/workflows/{workflow_id}/runs")

    assert failed.status_code == 200
    assert [item["id"] for item in failed.json()] == [second["id"]]
    assert {item["id"] for item in all_runs.json()} == {
        first["id"],
        second["id"],
    }


async def test_cancel_run_permissions_and_terminal_state(api, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id, version_id = await published(http)
    owner = await make_user(db_session, role="editor")
    state["user"] = owner
    run = (await start_run(http, workflow_id)).json()

    state["user"] = await make_user(db_session, role="editor")
    denied = await http.post(f"/api/workflow-runs/{run['id']}/cancel")
    assert denied.status_code == 403

    state["user"] = owner
    cancelled = await http.post(f"/api/workflow-runs/{run['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"

    again = await http.post(f"/api/workflow-runs/{run['id']}/cancel")
    assert again.status_code == 409
    assert again.json()["code"] == "workflow_run_not_cancellable"

    scheduled = await scheduled_run(db_session, workflow_id, version_id)
    state["user"] = await make_user(db_session, role="editor")
    assert (
        await http.post(f"/api/workflow-runs/{scheduled.id}/cancel")
    ).status_code == 403

    state["user"] = await make_user(db_session, role="admin")
    admin_cancel = await http.post(f"/api/workflow-runs/{scheduled.id}/cancel")
    assert admin_cancel.status_code == 200
    assert admin_cancel.json()["status"] == "cancelled"


async def test_resume_requires_failed_and_enqueues(api, queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id, _ = await published(http)
    owner = await make_user(db_session, role="editor")
    state["user"] = owner
    run = (await start_run(http, workflow_id)).json()
    row = await db_session.get(WorkflowRun, uuid.UUID(run["id"]))
    row.status = "failed"
    row.error_code = "node_failed"
    await db_session.commit()

    queued_run = await db_session.get(WorkflowRun, uuid.UUID(run["id"]))
    queued_run.status = "queued"
    await db_session.commit()
    not_failed = await http.post(f"/api/workflow-runs/{run['id']}/resume")
    assert not_failed.status_code == 409
    assert not_failed.json()["code"] == "workflow_run_not_resumable"

    queued_run.status = "failed"
    await db_session.commit()
    resumed = await http.post(f"/api/workflow-runs/{run['id']}/resume")

    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "queued"
    assert resumed.json()["resume_count"] == 1
    assert queue.jobs[-1] == ("run_workflow_job", run["id"])

    state["user"] = await make_user(db_session, role="editor")
    denied = await http.post(f"/api/workflow-runs/{run['id']}/resume")
    assert denied.status_code == 403


async def test_resume_queue_failure_rolls_back_to_failed(api, queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id, _ = await published(http)
    state["user"] = await make_user(db_session, role="editor")
    run = (await start_run(http, workflow_id)).json()
    row = await db_session.get(WorkflowRun, uuid.UUID(run["id"]))
    row.status = "failed"
    await db_session.commit()
    # 只有续跑本身遇到队列故障，建运行那一步走的是正常队列。
    app.dependency_overrides[get_queue] = lambda: DownQueue()

    resp = await http.post(f"/api/workflow-runs/{run['id']}/resume")

    assert resp.status_code == 503
    await db_session.refresh(row)
    assert row.status == "failed"
    assert row.error_code == "queue_unavailable"


async def test_approval_visibility_and_self_approval_guard(api, queue, db_session):
    http, state = api
    admin = await make_user(db_session, role="admin")
    state["user"] = admin
    workflow_id, version_id = await published(http)
    editor = await make_user(db_session, role="editor")
    state["user"] = editor
    editor_run = (await start_run(http, workflow_id)).json()
    _, editor_approval = await park(db_session, editor_run["id"])

    state["user"] = admin
    own_run = (await start_run(http, workflow_id)).json()
    _, own_approval = await park(db_session, own_run["id"])
    scheduled = await scheduled_run(
        db_session, workflow_id, version_id, status="waiting_approval"
    )
    _, scheduled_approval = await park(db_session, str(scheduled.id))

    listed = await http.get("/api/workflow-approvals")

    assert listed.status_code == 200, listed.text
    ids = {item["id"] for item in listed.json()}
    assert str(editor_approval.id) in ids
    assert str(scheduled_approval.id) in ids
    assert str(own_approval.id) not in ids
    assert {item["source"] for item in listed.json()} == {"workflow"}

    decided = await http.post(
        f"/api/workflow-approvals/{scheduled_approval.id}/decide",
        json={"decision": "approve"},
    )

    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "queued"
    assert queue.jobs[-1] == ("run_workflow_job", str(scheduled.id))

    own = await http.post(
        f"/api/workflow-approvals/{own_approval.id}/decide",
        json={"decision": "approve"},
    )

    assert own.status_code == 403

    _, foreign = await foreign_run(db_session)
    foreign_decide = await http.post(
        f"/api/workflow-approvals/{foreign.id}/decide",
        json={"decision": "approve"},
    )

    assert foreign_decide.status_code == 404
    assert foreign_decide.json()["code"] == "workflow_not_found"


async def test_reject_fails_run_and_denies_node(api, queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id, _ = await published(http)
    state["user"] = await make_user(db_session, role="editor")
    run = (await start_run(http, workflow_id)).json()
    _, approval = await park(db_session, run["id"])
    state["user"] = await make_user(db_session, role="admin")

    resp = await http.post(
        f"/api/workflow-approvals/{approval.id}/decide",
        json={"decision": "reject", "note": "不通过"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "failed"
    assert resp.json()["error_code"] == "review_rejected"
    assert len(queue.jobs) == 1

    node = (
        await db_session.execute(
            select(WorkflowNodeRun).where(
                WorkflowNodeRun.run_id == uuid.UUID(run["id"]),
                WorkflowNodeRun.node_id == "review",
            )
        )
    ).scalar_one()
    assert node.status == "denied"

    again = await http.post(
        f"/api/workflow-approvals/{approval.id}/decide",
        json={"decision": "reject"},
    )
    assert again.status_code == 409
    assert again.json()["code"] == "workflow_approval_not_pending"


async def test_approve_queue_failure_keeps_pending(api, queue, db_session):
    http, state = api
    state["user"] = await make_user(db_session, role="admin")
    workflow_id, _ = await published(http)
    state["user"] = await make_user(db_session, role="editor")
    run = (await start_run(http, workflow_id)).json()
    _, approval = await park(db_session, run["id"])
    state["user"] = await make_user(db_session, role="admin")
    # 只有决策入队时故障，建运行与挂起审批都走正常队列。
    app.dependency_overrides[get_queue] = lambda: DownQueue()

    resp = await http.post(
        f"/api/workflow-approvals/{approval.id}/decide",
        json={"decision": "approve"},
    )

    assert resp.status_code == 503
    assert resp.json()["code"] == "queue_unavailable"
    run_row = await db_session.get(WorkflowRun, uuid.UUID(run["id"]))
    await db_session.refresh(run_row)
    assert run_row.status == "waiting_approval"
    approval_row = await db_session.get(WorkflowApproval, approval.id)
    await db_session.refresh(approval_row)
    assert approval_row.status == "pending"
