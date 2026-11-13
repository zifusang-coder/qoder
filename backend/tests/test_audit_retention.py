import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.base import DEFAULT_TENANT_ID
from app.services.audit_retention import archive_expired_audit_logs
from app.storage import LocalStorage


def _row(action: str, created_at: datetime, detail=None) -> AuditLog:
    return AuditLog(
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=None,
        actor_email="tester@local",
        action=action,
        resource_type="",
        resource_id="",
        status="success",
        ip="",
        user_agent="",
        detail=detail or {"note": action},
        created_at=created_at,
    )


def _archive_lines(root):
    lines = []
    for path in sorted(root.rglob("*.ndjson")):
        lines.extend(path.read_text(encoding="utf-8").splitlines())
    return [json.loads(line) for line in lines if line]


async def test_archives_and_deletes_only_expired_rows(db_session, tmp_path):
    now = datetime.now(UTC)
    old = _row("auth.login", now - timedelta(days=200))
    recent = _row("member.create", now - timedelta(days=10))
    db_session.add_all([old, recent])
    await db_session.commit()
    old_id, recent_id = old.id, recent.id
    storage = LocalStorage(tmp_path)

    archived = await archive_expired_audit_logs(
        db_session, storage, retention_days=180, now=now
    )

    assert archived == 1
    remaining = (await db_session.execute(select(AuditLog))).scalars().all()
    assert [row.id for row in remaining] == [recent_id]

    records = _archive_lines(tmp_path)
    assert [record["id"] for record in records] == [str(old_id)]
    assert records[0]["action"] == "auth.login"
    assert records[0]["tenant_id"] == DEFAULT_TENANT_ID
    assert records[0]["detail"] == {"note": "auth.login"}


async def test_zero_retention_disables_archiving(db_session, tmp_path):
    old = _row("auth.login", datetime.now(UTC) - timedelta(days=400))
    db_session.add(old)
    await db_session.commit()

    archived = await archive_expired_audit_logs(
        db_session, LocalStorage(tmp_path), retention_days=0
    )

    assert archived == 0
    assert len((await db_session.execute(select(AuditLog))).scalars().all()) == 1
    assert list(tmp_path.rglob("*.ndjson")) == []


async def test_archive_without_expired_rows_is_noop(db_session, tmp_path):
    db_session.add(_row("auth.login", datetime.now(UTC)))
    await db_session.commit()

    archived = await archive_expired_audit_logs(
        db_session, LocalStorage(tmp_path), retention_days=180
    )

    assert archived == 0
    assert list(tmp_path.rglob("*.ndjson")) == []


async def test_storage_failure_keeps_rows(db_session, tmp_path):
    old = _row("auth.login", datetime.now(UTC) - timedelta(days=400))
    db_session.add(old)
    await db_session.commit()

    class FailingStorage:
        def save_bytes(self, *args, **kwargs):
            raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        await archive_expired_audit_logs(
            db_session, FailingStorage(), retention_days=180
        )

    await db_session.rollback()
    assert len((await db_session.execute(select(AuditLog))).scalars().all()) == 1


async def test_archives_in_batches(db_session, tmp_path):
    now = datetime.now(UTC)
    rows = [
        _row(f"event.{index}", now - timedelta(days=200, minutes=index))
        for index in range(5)
    ]
    db_session.add_all(rows)
    await db_session.commit()

    archived = await archive_expired_audit_logs(
        db_session,
        LocalStorage(tmp_path),
        retention_days=180,
        batch_size=2,
        now=now,
    )

    assert archived == 5
    assert (await db_session.execute(select(AuditLog))).scalars().all() == []
    assert len(list(tmp_path.rglob("*.ndjson"))) == 3
    assert len(_archive_lines(tmp_path)) == 5
