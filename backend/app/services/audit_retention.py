import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.storage import StorageBackend

ARCHIVE_PROJECT_ID = "_audit_archive"
DEFAULT_BATCH_SIZE = 1000


def _serialize(row: AuditLog) -> dict:
    return {
        "id": str(row.id),
        "tenant_id": row.tenant_id,
        "actor_id": row.actor_id,
        "actor_email": row.actor_email,
        "action": row.action,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "status": row.status,
        "ip": row.ip,
        "user_agent": row.user_agent,
        "detail": row.detail or {},
        "created_at": row.created_at.isoformat(),
    }


async def archive_expired_audit_logs(
    session: AsyncSession,
    storage: StorageBackend,
    *,
    retention_days: int,
    batch_size: int = DEFAULT_BATCH_SIZE,
    now: datetime | None = None,
) -> int:
    """把超过保留期的审计导出为 NDJSON 后删除；归档失败时保留数据库记录。"""
    if retention_days <= 0:
        return 0
    if batch_size <= 0:
        raise ValueError("归档批次必须为正数")

    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    archived = 0
    while True:
        rows = (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.created_at < cutoff)
                .order_by(AuditLog.created_at, AuditLog.id)
                .limit(batch_size)
            )
        ).scalars().all()
        if not rows:
            break

        payload = "\n".join(
            json.dumps(_serialize(row), ensure_ascii=False, separators=(",", ":"))
            for row in rows
        ) + "\n"
        relpath = (
            f"{rows[0].created_at.date().isoformat()}/"
            f"{rows[0].id}-{rows[-1].id}.ndjson"
        )
        # 先写归档再删除；同名文件可被重试安全覆盖
        storage.save_bytes(ARCHIVE_PROJECT_ID, relpath, payload.encode("utf-8"))

        ids = [row.id for row in rows]
        await session.execute(delete(AuditLog).where(AuditLog.id.in_(ids)))
        await session.commit()
        archived += len(rows)
        if len(rows) < batch_size:
            break
    return archived
