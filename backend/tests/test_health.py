from fastapi.testclient import TestClient

from app.main import app


def test_health_ok():
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_unhandled_error_returns_generic_envelope():
    @app.get("/_boom")
    async def boom():
        raise RuntimeError("internal detail must not leak")

    try:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/_boom")
    finally:
        app.router.routes = [
            route
            for route in app.router.routes
            if getattr(route, "path", None) != "/_boom"
        ]

    assert resp.status_code == 500
    assert resp.json() == {"code": "internal_error", "message": "服务器内部错误"}
    assert "internal detail" not in resp.text
