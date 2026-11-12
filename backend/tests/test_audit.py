from datetime import UTC, datetime

from app.models.audit import AuditLog
from app.models.base import DEFAULT_TENANT_ID
from app.services.audit import record

OTHER_TENANT = "00000000-0000-0000-0000-000000000002"


async def _seed(session):
    session.add_all(
        [
            AuditLog(
                tenant_id=DEFAULT_TENANT_ID,
                actor_email="owner@local",
                action="member.create",
                status="success",
                detail={"role": "editor"},
                created_at=datetime(2026, 11, 1, 10, tzinfo=UTC),
            ),
            AuditLog(
                tenant_id=DEFAULT_TENANT_ID,
                actor_email="owner@local",
                action="auth.login_failed",
                status="failure",
                detail={"reason": "bad_password"},
                created_at=datetime(2026, 11, 2, 10, tzinfo=UTC),
            ),
            AuditLog(
                tenant_id=OTHER_TENANT,
                actor_email="other@local",
                action="member.create",
                status="success",
                detail={"role": "viewer"},
                created_at=datetime(2026, 11, 2, 11, tzinfo=UTC),
            ),
        ]
    )
    await session.commit()


async def test_audit_is_tenant_scoped_and_sorted(client, db_session):
    await _seed(db_session)
    resp = await client.get("/api/audit-logs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert [item["action"] for item in body["items"]] == [
        "auth.login_failed",
        "member.create",
    ]
    assert all(item["actor_email"] != "other@local" for item in body["items"])


async def test_audit_filters(client, db_session):
    await _seed(db_session)
    by_action = await client.get(
        "/api/audit-logs", params={"action": "member.create"}
    )
    assert by_action.json()["total"] == 1
    by_status = await client.get(
        "/api/audit-logs", params={"status": "failure"}
    )
    assert by_status.json()["total"] == 1
    assert by_status.json()["items"][0]["detail"] == {"reason": "bad_password"}
    by_actor = await client.get(
        "/api/audit-logs", params={"actor_email": "other@local"}
    )
    assert by_actor.json()["total"] == 0


async def test_audit_time_range_and_pagination(client, db_session):
    await _seed(db_session)
    ranged = await client.get(
        "/api/audit-logs",
        params={
            "from": "2026-11-01T00:00:00Z",
            "to": "2026-11-02T00:00:00Z",
        },
    )
    assert ranged.json()["total"] == 1
    assert ranged.json()["items"][0]["action"] == "member.create"

    page = await client.get(
        "/api/audit-logs", params={"limit": 1, "offset": 0}
    )
    assert page.json()["total"] == 2
    assert len(page.json()["items"]) == 1
    second = await client.get(
        "/api/audit-logs", params={"limit": 1, "offset": 1}
    )
    assert second.json()["items"][0]["id"] != page.json()["items"][0]["id"]


async def test_audit_detail_is_sanitized(client, db_session):
    await record(
        db_session,
        tenant_id=DEFAULT_TENANT_ID,
        action="auth.change_password",
        actor="scheduler",
        detail={
            "password": "raw-password",
            "new_password": "raw-new",
            "access_token": "raw-token",
            "role": "editor",
        },
    )
    await db_session.commit()
    resp = await client.get(
        "/api/audit-logs", params={"action": "auth.change_password"}
    )
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["detail"] == {"role": "editor"}
    assert item["actor_email"] == "scheduler"


async def test_audit_has_no_delete_route(client):
    resp = await client.delete("/api/audit-logs")
    assert resp.status_code == 405


async def test_viewer_cannot_read_audit(client, db_session):
    from tests.test_permissions import _role_client

    async with _role_client(db_session, "viewer") as viewer:
        resp = await viewer.get("/api/audit-logs")
        assert resp.status_code == 403
