import json
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.errors import AppError
from app.models.agent import AgentRun
from app.models.base import DEFAULT_TENANT_ID
from app.models.media import MediaJob
from app.models.project import Project
from app.models.workflow import Workflow, WorkflowRun, WorkflowVersion
from app.services import live


def _bind_sessions(monkeypatch, db_session):
    monkeypatch.setattr(
        live,
        "SessionLocal",
        async_sessionmaker(db_session.bind, expire_on_commit=False),
    )


async def _sse_lines(resp) -> list[str]:
    body = "".join([chunk async for chunk in resp.aiter_text()])
    return [
        line[len("data: ") :].strip()
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


async def test_snapshot_stream_skips_unchanged_and_stops_at_terminal(monkeypatch):
    snapshots = [
        {"status": "running", "value": 1},
        {"status": "running", "value": 1},
        {"status": "running", "value": 2},
        {"status": "succeeded", "value": 2},
    ]
    calls = 0

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    async def load(_session):
        nonlocal calls
        calls += 1
        return snapshots[calls - 1]

    monkeypatch.setattr(live, "SessionLocal", FakeSession)

    events = [event async for event in live.snapshot_stream(load, interval=0)]

    assert calls == 4
    payloads = [json.loads(event["data"]) for event in events[:-1]]
    assert [payload["value"] for payload in payloads] == [1, 2, 2]
    assert events[-1]["data"] == "[DONE]"


async def test_snapshot_stream_reports_errors_without_done(monkeypatch):
    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    async def load(_session):
        raise AppError("not_found", "资源不存在", 404)

    monkeypatch.setattr(live, "SessionLocal", FakeSession)

    events = [event async for event in live.snapshot_stream(load, interval=0)]

    assert len(events) == 1
    assert json.loads(events[0]["data"])["error"]["code"] == "not_found"


async def test_agent_run_stream_emits_snapshot_then_done(
    client, monkeypatch, db_session
):
    _bind_sessions(monkeypatch, db_session)
    run = AgentRun(
        tenant_id=DEFAULT_TENANT_ID,
        user_id=uuid.uuid4(),
        goal="写周报",
        status="succeeded",
    )
    db_session.add(run)
    await db_session.commit()

    async with client.stream(
        "GET", f"/api/agent/runs/{run.id}/stream"
    ) as resp:
        assert resp.status_code == 200
        lines = await _sse_lines(resp)

    assert lines[-1] == "[DONE]"
    payload = json.loads(lines[0])
    assert payload["id"] == str(run.id)
    assert payload["status"] == "succeeded"
    assert payload["goal"] == "写周报"


async def test_agent_run_stream_unknown_run_returns_404(client):
    resp = await client.get(
        "/api/agent/runs/00000000-0000-0000-0000-000000000099/stream"
    )
    assert resp.status_code == 404


async def test_workflow_run_stream_uses_nested_status(
    client, monkeypatch, db_session
):
    _bind_sessions(monkeypatch, db_session)
    workflow = Workflow(
        tenant_id=DEFAULT_TENANT_ID, name="周更流程", created_by=uuid.uuid4()
    )
    db_session.add(workflow)
    await db_session.flush()
    version = WorkflowVersion(
        tenant_id=DEFAULT_TENANT_ID,
        workflow_id=workflow.id,
        version=1,
        graph={"nodes": [], "edges": []},
        source_revision=0,
        published_by=uuid.uuid4(),
        published_at=datetime.now(UTC),
    )
    db_session.add(version)
    await db_session.flush()
    run = WorkflowRun(
        tenant_id=DEFAULT_TENANT_ID,
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        status="failed",
        error_code="boom",
    )
    db_session.add(run)
    await db_session.commit()

    async with client.stream(
        "GET", f"/api/workflow-runs/{run.id}/stream"
    ) as resp:
        assert resp.status_code == 200
        lines = await _sse_lines(resp)

    assert lines[-1] == "[DONE]"
    payload = json.loads(lines[0])
    assert payload["run"]["status"] == "failed"
    assert payload["run"]["error_code"] == "boom"


async def test_media_job_stream_emits_job_snapshot(client, monkeypatch, db_session):
    _bind_sessions(monkeypatch, db_session)
    project = Project(tenant_id=DEFAULT_TENANT_ID, title="夏季选题")
    db_session.add(project)
    await db_session.flush()
    job = MediaJob(
        tenant_id=DEFAULT_TENANT_ID,
        project_id=project.id,
        skill_id="short-video",
        status="succeeded",
    )
    db_session.add(job)
    await db_session.commit()

    async with client.stream(
        "GET", f"/api/projects/{project.id}/media/jobs/{job.id}/stream"
    ) as resp:
        assert resp.status_code == 200
        lines = await _sse_lines(resp)

    assert lines[-1] == "[DONE]"
    payload = json.loads(lines[0])
    assert payload["id"] == str(job.id)
    assert payload["status"] == "succeeded"
    assert payload["skill_id"] == "short-video"
