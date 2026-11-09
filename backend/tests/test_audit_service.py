import uuid
from types import SimpleNamespace

from sqlalchemy import select

from app.config import DEFAULT_TENANT_ID
from app.models.audit import AuditLog
from app.models.user import User
from app.services.audit import record, record_best_effort, sanitize_detail


def test_sanitize_drops_sensitive_keys_and_truncates():
    detail = {
        "password": "p",
        "new_password": "np",
        "current_password": "cp",
        "access_token": "t",
        "api_key": "k",
        "secret": "s",
        "credential": {"app_secret": "x"},
        "authorization": "Bearer x",
        "cookie": "sid",
        "safe": "ok",
        "long": "x" * 300,
        "nested": {"role": "editor", "password": "p", "objects": object()},
        "items": [1, True, None, "abc", object()],
        "too_deep": {"a": {"b": 1}},
    }

    cleaned = sanitize_detail(detail)

    assert "password" not in cleaned
    assert "new_password" not in cleaned
    assert "current_password" not in cleaned
    assert "access_token" not in cleaned
    assert "api_key" not in cleaned
    assert "secret" not in cleaned
    assert "credential" not in cleaned
    assert "authorization" not in cleaned
    assert "cookie" not in cleaned
    assert cleaned["safe"] == "ok"
    assert cleaned["long"] == "x" * 200
    assert cleaned["nested"] == {"role": "editor"}
    assert cleaned["items"] == [1, True, None, "abc"]
    assert cleaned["too_deep"] == {}


def test_sanitize_non_dict_returns_empty():
    assert sanitize_detail(None) == {}
    assert sanitize_detail(["x"]) == {}


async def test_record_writes_actor_and_request_metadata(db_session):
    user = User(
        id=uuid.uuid4(),
        tenant_id=DEFAULT_TENANT_ID,
        email="actor@local",
        password_hash="x",
        role="owner",
    )
    request = SimpleNamespace(
        client=SimpleNamespace(host="10.0.0.1"),
        headers={"user-agent": "UA" * 200},
    )

    await record(
        db_session,
        tenant_id=DEFAULT_TENANT_ID,
        action="member.create",
        actor=user,
        resource_type="user",
        resource_id=str(user.id),
        detail={"role": "editor"},
        request=request,
    )
    await db_session.commit()

    row = (await db_session.execute(select(AuditLog))).scalar_one()
    assert row.actor_id == str(user.id)
    assert row.actor_email == "actor@local"
    assert row.action == "member.create"
    assert row.resource_type == "user"
    assert row.resource_id == str(user.id)
    assert row.ip == "10.0.0.1"
    assert len(row.user_agent) == 300
    assert row.detail == {"role": "editor"}


async def test_record_string_actor_has_no_actor_id(db_session):
    await record(
        db_session,
        tenant_id=DEFAULT_TENANT_ID,
        action="publish.auto_execute",
        actor="scheduler",
    )
    await db_session.commit()

    row = (await db_session.execute(select(AuditLog))).scalar_one()
    assert row.actor_id is None
    assert row.actor_email == "scheduler"


async def test_record_best_effort_swallows_commit_errors(db_session, monkeypatch):
    async def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(db_session, "commit", boom)

    await record_best_effort(
        db_session,
        tenant_id=DEFAULT_TENANT_ID,
        action="auth.login",
        actor="tester@local",
    )
