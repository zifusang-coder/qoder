from sqlalchemy.ext.asyncio import AsyncSession

from app.models.publish import PublishEvent, PublishJob
from app.publish.state import next_status


async def apply_action(
    session: AsyncSession,
    job: PublishJob,
    action: str,
    *,
    actor: str = "user",
    note: str = "",
) -> PublishJob:
    current = job.status
    target = next_status(current, action)
    job.status = target
    session.add(
        PublishEvent(
            tenant_id=job.tenant_id,
            job_id=job.id,
            from_status=current,
            to_status=target,
            action=action,
            note=note,
            actor=actor,
        )
    )
    await session.commit()
    await session.refresh(job)
    return job
