import pytest
import respx
from httpx import Response

from app import metrics
from app.config import get_settings
from app.services import alerts


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset()
    yield
    metrics.reset()


@pytest.mark.asyncio
async def test_request_id_header_is_echoed(auth_client):
    resp = await auth_client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert resp.status_code == 200
    assert resp.headers["x-request-id"] == "abc-123"


@pytest.mark.asyncio
async def test_request_id_is_generated_and_counted(auth_client):
    resp = await auth_client.get("/health")
    assert resp.headers.get("x-request-id")
    text = metrics.render_metrics()
    assert 'newmedia_http_requests_total{method="GET",path="/health",status="200"}' in text


@pytest.mark.asyncio
async def test_metrics_disabled_without_token(auth_client, monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "")
    get_settings.cache_clear()
    resp = await auth_client.get("/metrics")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_metrics_requires_token(auth_client, monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "s3cret-token")
    get_settings.cache_clear()
    assert (await auth_client.get("/metrics")).status_code == 401
    ok = await auth_client.get(
        "/metrics", headers={"Authorization": "Bearer s3cret-token"}
    )
    assert ok.status_code == 200
    assert "newmedia_http_requests_total" in ok.text


def test_metrics_render_counts():
    metrics.observe_request("GET", "/api/projects", 200)
    metrics.observe_request("GET", "/api/projects", 500)
    metrics.observe_failure("workflow", "workflow_internal_error")
    text = metrics.render_metrics()
    assert (
        'newmedia_http_requests_total{method="GET",path="/api/projects",status="500"} 1'
        in text
    )
    assert (
        'newmedia_run_failures_total{kind="workflow",code="workflow_internal_error"} 1'
        in text
    )


@respx.mock
@pytest.mark.asyncio
async def test_report_failure_posts_webhook(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://alerts.example/hook")
    get_settings.cache_clear()
    route = respx.post("https://alerts.example/hook").mock(
        return_value=Response(204)
    )
    await alerts.report_failure(
        kind="agent",
        resource_id="run-1",
        tenant_id="tenant-1",
        error_code="agent_internal_error",
    )
    assert route.called
    body = route.calls[0].request.read().decode()
    assert "agent_internal_error" in body
    assert "run-1" in body
    assert (
        'newmedia_run_failures_total{kind="agent",code="agent_internal_error"} 1'
        in metrics.render_metrics()
    )


@pytest.mark.asyncio
async def test_report_failure_without_webhook_is_noop(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "")
    get_settings.cache_clear()
    await alerts.report_failure(
        kind="team", resource_id="run-2", tenant_id=None, error_code="critic_rejected"
    )
    assert (
        'newmedia_run_failures_total{kind="team",code="critic_rejected"} 1'
        in metrics.render_metrics()
    )


@respx.mock
@pytest.mark.asyncio
async def test_report_failure_swallows_webhook_errors(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://alerts.example/hook")
    get_settings.cache_clear()
    respx.post("https://alerts.example/hook").mock(side_effect=OSError("down"))
    await alerts.report_failure(
        kind="workflow", resource_id="run-3", tenant_id=None, error_code="boom"
    )
