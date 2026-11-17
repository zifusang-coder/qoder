"""工作流运行生命周期：领取、取消、续跑与心跳对账。

引擎不领取运行；worker 用 ``claim_run`` 的 CAS 拿到所有权后才推进。
这里所有状态迁移都是条件更新，并发时先到者胜，后到者读到最新状态。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.errors import AppError
from app.models.agent import AgentApproval, AgentRun, AgentStep
from app.models.workflow import (
    WorkflowApproval,
    WorkflowNodeRun,
    WorkflowRun,
)
from app.services.audit import record_best_effort
from app.workflow import approvals

logger = logging.getLogger(__name__)

CANCELLABLE = frozenset({"queued", "running", "waiting_approval"})
REOPEN = frozenset({"error", "denied", "interrupted", "skipped"})
MAX_RESUME = 10
INTERRUPTED_MESSAGE = "上次执行中断"
APPROVAL_EXPIRED_MESSAGE = "审批已失效"


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _interrupt_nodes(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    statuses: frozenset[str] = frozenset({"running", "waiting"}),
) -> None:
    await session.execute(
        update(WorkflowNodeRun)
        .where(
            WorkflowNodeRun.run_id == run_id,
            WorkflowNodeRun.status.in_(statuses),
        )
        .values(
            status="interrupted",
            error=INTERRUPTED_MESSAGE,
            finished_at=_now(),
        )
        .execution_options(synchronize_session=False)
    )


async def _close_children(
    session: AsyncSession,
    run_id: uuid.UUID,
    moment: datetime,
    note: str,
) -> None:
    """取消仍在活动的子运行，并关闭它们的审批与在途步骤。"""
    children = (
        (
            await session.execute(
                select(AgentRun.id).where(
                    AgentRun.workflow_run_id == run_id,
                    AgentRun.status.in_({"queued", "running", "waiting_approval"}),
                )
            )
        )
        .scalars()
        .all()
    )
    if not children:
        return
    await session.execute(
        update(AgentApproval)
        .where(
            AgentApproval.run_id.in_(children),
            AgentApproval.status == "pending",
        )
        .values(status="expired", decided_at=moment, note=note)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(AgentStep)
        .where(
            AgentStep.run_id.in_(children),
            AgentStep.status == "running",
        )
        .values(status="interrupted", error=note, finished_at=moment)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(AgentRun)
        .where(AgentRun.id.in_(children))
        .values(status="cancelled", finished_at=moment)
        .execution_options(synchronize_session=False)
    )


async def claim_run(session: AsyncSession, run_id: uuid.UUID) -> str:
    """CAS 领取一个 queued 运行。

    返回 ``"claimed"`` 表示本次调用拿到了运行；其余返回值是观察到的状态
    （别的执行者已经领走、还在等审批、已终态或不存在），worker 看到非
    ``"claimed"`` 就跳过，重复投递因此不会重复执行。
    """
    moment = _now()
    run = await session.get(WorkflowRun, run_id)
    if run is None:
        return "missing"
    claimed = await session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == run_id, WorkflowRun.status == "queued")
        .values(
            status="running",
            attempts=WorkflowRun.attempts + 1,
            heartbeat_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount == 0:
        await session.rollback()
        await session.refresh(run)
        return run.status
    await session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == run_id, WorkflowRun.started_at.is_(None))
        .values(started_at=moment)
        .execution_options(synchronize_session=False)
    )
    # 只清理上次执行遗留的 running 节点。waiting 节点必须保持原状：
    # 审批通过后引擎靠这条记录在 _resume_waiting 里结算，清掉会让
    # 审核/工具确认节点重新执行并再建一条 pending 审批，运行永远出不来。
    await _interrupt_nodes(session, run_id, statuses=frozenset({"running"}))
    await session.commit()
    return "claimed"


async def _audit(
    session: AsyncSession,
    run_id: uuid.UUID,
    tenant_id: str,
    action: str,
    actor: str,
) -> None:
    await record_best_effort(
        session,
        tenant_id=tenant_id,
        action=action,
        actor=actor,
        resource_type="workflow_run",
        resource_id=str(run_id),
    )


async def cancel_run(
    session: AsyncSession, run: WorkflowRun, *, actor: str = "system"
) -> bool:
    moment = _now()
    cancelled = await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run.id,
            WorkflowRun.tenant_id == run.tenant_id,
            WorkflowRun.status.in_(CANCELLABLE),
        )
        .values(status="cancelled", finished_at=moment, heartbeat_at=moment)
        .execution_options(synchronize_session=False)
    )
    if cancelled.rowcount == 0:
        await session.rollback()
        return False

    await session.execute(
        update(WorkflowApproval)
        .where(
            WorkflowApproval.run_id == run.id,
            WorkflowApproval.status == "pending",
        )
        .values(status="expired", decided_at=moment)
        .execution_options(synchronize_session=False)
    )
    children = (
        (
            await session.execute(
                select(AgentRun.id).where(
                    AgentRun.workflow_run_id == run.id,
                    AgentRun.status.in_({"queued", "running", "waiting_approval"}),
                )
            )
        )
        .scalars()
        .all()
    )
    if children:
        await session.execute(
            update(AgentApproval)
            .where(
                AgentApproval.run_id.in_(children),
                AgentApproval.status == "pending",
            )
            .values(status="expired", decided_at=moment)
            .execution_options(synchronize_session=False)
        )
        await session.execute(
            update(AgentStep)
            .where(
                AgentStep.run_id.in_(children),
                AgentStep.status == "running",
            )
            .values(
                status="interrupted",
                error="运行已取消",
                finished_at=moment,
            )
            .execution_options(synchronize_session=False)
        )
        await session.execute(
            update(AgentRun)
            .where(AgentRun.id.in_(children))
            .values(status="cancelled", finished_at=moment)
            .execution_options(synchronize_session=False)
        )
    await _interrupt_nodes(session, run.id)
    await session.commit()
    await _audit(session, run.id, run.tenant_id, "workflow.run.cancel", actor)
    return True


async def fail_waiting_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    error_code: str,
    message: str,
    actor: str = "system",
) -> bool:
    """终结一个因审批被驳回而永远等不到恢复的运行。

    子运行审批由第 1 层决策，引擎没有机会自己发现驳回；决策 API 在同一个
    事务里关掉父运行、清掉其余挂起审批与子运行，保证没有审批或子运行比
    父运行活得更久。只有 ``waiting_approval`` 会被改写，并发取消/过期保持
    自己的终态。
    """
    moment = _now()
    run = await session.get(WorkflowRun, run_id)
    if run is None:
        return False
    failed = await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run.id,
            WorkflowRun.tenant_id == run.tenant_id,
            WorkflowRun.status == "waiting_approval",
        )
        .values(
            status="failed",
            error_code=error_code,
            error_message=message,
            finished_at=moment,
            heartbeat_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    if failed.rowcount == 0:
        await session.rollback()
        return False

    await session.execute(
        update(WorkflowApproval)
        .where(
            WorkflowApproval.run_id == run.id,
            WorkflowApproval.status == "pending",
        )
        .values(status="expired", decided_at=moment)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(WorkflowNodeRun)
        .where(
            WorkflowNodeRun.run_id == run.id,
            WorkflowNodeRun.status == "waiting",
        )
        .values(
            status="denied", error=message, finished_at=moment
        )
        .execution_options(synchronize_session=False)
    )
    await _close_children(session, run.id, moment, message)
    # waiting 节点已经落 denied；这里只兜底可能残留的 running 节点。
    await _interrupt_nodes(session, run.id)
    await session.commit()
    await _audit(session, run.id, run.tenant_id, "workflow.run.reject", actor)
    return True


async def fail_active_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    error_code: str,
    message: str,
    actor: str = "system",
) -> bool:
    """把仍在 ``queued``/``running`` 的运行落为失败并清理在途状态。

    worker 的崩溃兜底入口。与 ``fail_waiting_run`` 的区别是只改写这两个
    状态：等待审批的运行属于审批决策者，不能被崩溃兜底覆盖。改写成功后
    同一事务里过期 pending 审批、中断在途节点并取消子运行，避免失败运行
    留下仍可决策的审批或仍在后台写结果的子运行。
    """
    moment = _now()
    run = await session.get(WorkflowRun, run_id)
    if run is None:
        return False
    failed = await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run.id,
            WorkflowRun.tenant_id == run.tenant_id,
            WorkflowRun.status.in_({"queued", "running"}),
        )
        .values(
            status="failed",
            error_code=error_code,
            error_message=message,
            finished_at=moment,
            heartbeat_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    if failed.rowcount == 0:
        await session.rollback()
        return False
    await session.execute(
        update(WorkflowApproval)
        .where(
            WorkflowApproval.run_id == run.id,
            WorkflowApproval.status == "pending",
        )
        .values(status="expired", decided_at=moment)
        .execution_options(synchronize_session=False)
    )
    await _close_children(session, run.id, moment, message)
    await _interrupt_nodes(session, run.id)
    await session.commit()
    await _audit(session, run.id, run.tenant_id, "workflow.run.fail", actor)
    return True


async def resume_run(
    session: AsyncSession, run: WorkflowRun, *, actor: str = "system"
) -> None:
    if run.status != "failed":
        raise AppError(
            "workflow_run_not_resumable", "只有失败的运行可以续跑", 409
        )
    if int(run.resume_count or 0) >= MAX_RESUME:
        raise AppError(
            "workflow_run_not_resumable", "续跑次数已达上限", 409
        )
    moment = _now()
    resumed = await session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == run.id, WorkflowRun.status == "failed")
        .values(
            status="queued",
            resume_count=WorkflowRun.resume_count + 1,
            error_code="",
            error_message="",
            finished_at=None,
            heartbeat_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    if resumed.rowcount == 0:
        await session.rollback()
        raise AppError(
            "workflow_run_not_resumable", "运行状态已变化，无法续跑", 409
        )
    rows = (
        (
            await session.execute(
                select(WorkflowNodeRun)
                .where(WorkflowNodeRun.run_id == run.id)
                .order_by(WorkflowNodeRun.attempt)
            )
        )
        .scalars()
        .all()
    )
    latest: dict[str, WorkflowNodeRun] = {}
    for row in rows:
        latest[row.node_id] = row
    for row in latest.values():
        if row.status in REOPEN:
            row.status = "interrupted"
            row.error = "等待续跑"
            row.finished_at = None
    await session.commit()
    await session.refresh(run)
    await _audit(session, run.id, run.tenant_id, "workflow.run.resume", actor)


async def settle_stale_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> str:
    moment = now or _now()
    run = await session.get(WorkflowRun, run_id)
    if run is None:
        return "missing"
    if run.status != "running":
        return run.status
    if int(run.attempts or 0) < 2:
        changed = await session.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run_id, WorkflowRun.status == "running")
            .values(status="queued", heartbeat_at=moment)
            .execution_options(synchronize_session=False)
        )
        outcome = "queued"
    else:
        changed = await session.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run_id, WorkflowRun.status == "running")
            .values(
                status="failed",
                error_code="workflow_stale",
                error_message="运行心跳超时",
                finished_at=moment,
                heartbeat_at=moment,
            )
            .execution_options(synchronize_session=False)
        )
        outcome = "failed"
    if changed.rowcount == 0:
        await session.rollback()
        return "stale"
    await _interrupt_nodes(session, run_id)
    await session.commit()
    return outcome


async def _fail_approval_run(
    session: AsyncSession, run_id: uuid.UUID, moment: datetime
) -> int:
    """把审批过期导致的挂起运行落为失败，并清理在途状态。

    清理范围与 ``fail_waiting_run`` 一致：先 CAS 运行行，再过期剩余审批、
    deny ``waiting`` 节点、取消子运行并中断在途节点。先动运行行是为了与
    ``decide_approval`` 的 ``workflow_runs → 审批`` 锁序一致，避免对账与
    审批决策交叉持锁成环；CAS 未命中说明别的执行者已经改写了运行，回滚
    且不写任何清理。
    """
    failed = await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run_id,
            WorkflowRun.status == "waiting_approval",
        )
        .values(
            status="failed",
            error_code="approval_expired",
            error_message=APPROVAL_EXPIRED_MESSAGE,
            finished_at=moment,
            heartbeat_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    if failed.rowcount == 0:
        await session.rollback()
        return 0
    await session.execute(
        update(WorkflowApproval)
        .where(
            WorkflowApproval.run_id == run_id,
            WorkflowApproval.status == "pending",
        )
        .values(status="expired", decided_at=moment)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(WorkflowNodeRun)
        .where(
            WorkflowNodeRun.run_id == run_id,
            WorkflowNodeRun.status == "waiting",
        )
        .values(
            status="denied",
            error=APPROVAL_EXPIRED_MESSAGE,
            finished_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    await _close_children(session, run_id, moment, APPROVAL_EXPIRED_MESSAGE)
    # waiting 节点已经落 denied；这里兜底可能残留的 running 节点。
    await _interrupt_nodes(session, run_id)
    await session.commit()
    return 1


async def _expire_overdue(session: AsyncSession, moment: datetime) -> int:
    rows = (
        (
            await session.execute(
                select(WorkflowApproval).where(
                    WorkflowApproval.status == "pending",
                    WorkflowApproval.expires_at.is_not(None),
                    WorkflowApproval.expires_at <= moment,
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0
    for row in rows:
        row.status = "expired"
        row.decided_at = moment
    await session.commit()
    failed = 0
    for run_id in {row.run_id for row in rows}:
        failed += await _fail_approval_run(session, run_id, moment)
    return failed


async def requeue_settled_orphan(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> bool:
    """把「审批已全部通过却没有被唤醒」的挂起运行重排回 ``queued``。

    最后一条审批通过时本应由 ``decide_approval``（工作流审批）或
    ``_wake_parent_workflow``（子运行审批）CAS 成 queued；跨通道并发下两者
    可能互相看到对方 pending，结果运行停在零 pending 的 waiting_approval。
    这里是对账兜底：只处理至少有一条 approved、且没有任何 pending 的运行，
    并把等待时长按与 ``_wake_parent_workflow`` 相同的公式计入 ``paused_ms``。

    锁顺序与 ``decide_approval`` 一致（运行行 -> 审批行）。不满足条件或
    CAS 落败时回滚释放锁并返回 False，调用方再决定是否按失败清理。
    """
    moment = now or _now()
    run = (
        await session.execute(
            select(WorkflowRun)
            .where(WorkflowRun.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if run is None or run.status != "waiting_approval":
        await session.rollback()
        return False
    if await approvals.has_pending(
        session, tenant_id=run.tenant_id, run_id=run.id
    ):
        await session.rollback()
        return False
    settled = (
        await session.execute(
            select(WorkflowApproval.id)
            .where(
                WorkflowApproval.tenant_id == run.tenant_id,
                WorkflowApproval.run_id == run.id,
                WorkflowApproval.status == "approved",
            )
            .limit(1)
        )
    ).first()
    if settled is None:
        await session.rollback()
        return False
    usage = dict(run.usage or {})
    paused_at = _aware(run.heartbeat_at) or moment
    waited_ms = int(
        max(0.0, (moment - min(paused_at, moment)).total_seconds()) * 1000
    )
    usage["paused_ms"] = int(usage.get("paused_ms") or 0) + waited_ms
    requeued = await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run.id,
            WorkflowRun.tenant_id == run.tenant_id,
            WorkflowRun.status == "waiting_approval",
        )
        .values(
            status="queued",
            usage=usage,
            heartbeat_at=moment,
            finished_at=None,
            error_code="",
            error_message="",
        )
        .execution_options(synchronize_session=False)
    )
    if requeued.rowcount == 0:
        await session.rollback()
        return False
    await session.commit()
    return True


async def _fail_orphans(
    session: AsyncSession,
    moment: datetime,
    *,
    requeued: list[uuid.UUID] | None = None,
) -> int:
    """处理没有 pending 审批的挂起运行：已结算的救回，其余判失败。"""
    runs = (
        (
            await session.execute(
                select(WorkflowRun)
                .where(WorkflowRun.status == "waiting_approval")
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    # 先取标量：某个运行 CAS 落败会 rollback 并过期 session 内全部实例，
    # 之后读 ORM 属性会触发 MissingGreenlet，打断整轮对账。
    targets = [(run.id, run.tenant_id) for run in runs]
    failed = 0
    for run_id, tenant_id in targets:
        if await requeue_settled_orphan(session, run_id, now=moment):
            if requeued is not None:
                requeued.append(run_id)
            continue
        if await approvals.has_pending(
            session, tenant_id=tenant_id, run_id=run_id
        ):
            continue
        failed += await _fail_approval_run(session, run_id, moment)
    return failed


async def reconcile(
    factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    now: datetime | None = None,
    queued_grace_seconds: int = 60,
    on_requeue: Callable[[uuid.UUID], Awaitable[None]] | None = None,
) -> dict[str, int]:
    """每分钟对账：超时运行、丢失的入队、过期审批、无 pending 的挂起运行。

    ``on_requeue`` 在每次把运行重排回 ``queued`` 后调用一次，worker 用它
    入队；回调抛错会中断本次扫描，由调用方自己兜底（worker 在回调内部
    消化入队失败，不会抛到这里）。``queued_grace_seconds`` 是「queued 后
    入队丢失」的宽限期：超过它仍未推进的运行会被重新投递（不改状态，
    ``claim_run`` 的 CAS 保证重复投递安全）。
    """
    moment = now or _now()
    counts = {
        "requeued": 0,
        "stale_failed": 0,
        "approval_expired": 0,
        "queued_reenqueued": 0,
    }
    cutoff = moment - timedelta(
        seconds=max(1, int(settings.workflow_run_stale_seconds or 0))
    )
    async with factory() as session:
        stale = (
            (
                await session.execute(
                    select(WorkflowRun.id)
                    .where(
                        WorkflowRun.status == "running",
                        or_(
                            WorkflowRun.heartbeat_at.is_(None),
                            WorkflowRun.heartbeat_at < cutoff,
                        ),
                    )
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
    for run_id in stale:
        async with factory() as session:
            outcome = await settle_stale_run(session, run_id, now=moment)
        if outcome == "queued":
            counts["requeued"] += 1
            if on_requeue is not None:
                await on_requeue(run_id)
        elif outcome == "failed":
            counts["stale_failed"] += 1

    # queued 提交后入队丢失（进程在两步之间死亡）：宽限期外重新投递一次。
    lost_cutoff = moment - timedelta(seconds=max(0, int(queued_grace_seconds)))
    async with factory() as session:
        lost = (
            (
                await session.execute(
                    select(WorkflowRun.id)
                    .where(
                        WorkflowRun.status == "queued",
                        WorkflowRun.updated_at < lost_cutoff,
                    )
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
    for run_id in lost:
        counts["queued_reenqueued"] += 1
        if on_requeue is not None:
            await on_requeue(run_id)

    orphan_requeued: list[uuid.UUID] = []
    async with factory() as session:
        counts["approval_expired"] += await _expire_overdue(session, moment)
        counts["approval_expired"] += await _fail_orphans(
            session, moment, requeued=orphan_requeued
        )
    for run_id in orphan_requeued:
        counts["requeued"] += 1
        if on_requeue is not None:
            await on_requeue(run_id)
    if any(counts.values()):
        logger.info("workflow reconcile %s", counts)
    return counts
