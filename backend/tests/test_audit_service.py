import logging
import uuid
from types import SimpleNamespace

import pytest
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
        "x-api-key": "xak",
        "apiKey": "camel",
        "private_key": "pk",
        "passwd": "pw",
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
    assert "x-api-key" not in cleaned
    assert "apiKey" not in cleaned
    assert "private_key" not in cleaned
    assert "passwd" not in cleaned
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


async def test_record_best_effort_keeps_caller_session_usable_on_audit_failure(
    db_session, monkeypatch, caplog
):
    user = User(
        id=uuid.uuid4(),
        tenant_id=DEFAULT_TENANT_ID,
        email="keep@local",
        password_hash="x",
        role="owner",
    )
    db_session.add(user)
    await db_session.commit()
    loaded = await db_session.get(User, user.id)
    assert loaded is not None

    # 调用方还有未提交的业务写入：审计失败不得回滚它，也不得 expire 已加载对象
    pending = User(
        id=uuid.uuid4(),
        tenant_id=DEFAULT_TENANT_ID,
        email="pending@local",
        password_hash="x",
        role="editor",
    )
    db_session.add(pending)
    await db_session.flush()

    async def boom(*args, **kwargs):
        raise RuntimeError("audit down")

    monkeypatch.setattr("app.services.audit.record", boom)

    with caplog.at_level(logging.ERROR, logger="app.services.audit"):
        await record_best_effort(
            db_session,
            tenant_id=DEFAULT_TENANT_ID,
            action="auth.login",
            actor=loaded,
        )

    assert any(record.levelno == logging.ERROR for record in caplog.records)
    # 未提交的业务写入仍在事务中
    assert await db_session.get(User, pending.id) is not None
    # 审计失败不得触发外层 rollback：否则已加载对象被 expire，异步访问会抛 MissingGreenlet
    assert loaded.email == "keep@local"


async def test_record_best_effort_rejects_bad_kwargs(db_session):
    with pytest.raises(TypeError):
        await record_best_effort(
            db_session,
            tenant_id=DEFAULT_TENANT_ID,
            action="auth.login",
            actor_email="x@local",
        )


async def test_record_writes_without_committing(db_session):
    await record(
        db_session,
        tenant_id=DEFAULT_TENANT_ID,
        action="auth.login",
        actor="tester@local",
    )
    await db_session.rollback()

    rows = (await db_session.execute(select(AuditLog))).scalars().all()
    assert rows == []
