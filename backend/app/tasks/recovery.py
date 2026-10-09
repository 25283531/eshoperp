"""启动时任务恢复：扫描 TaskRecord 未完成任务并重新调度（PRD 8.4 / 附录 A 第 9 条）。

规则：
    * `running` → 视为上次进程被杀，重置为 `pending` 并重新入队；
    * `pending` → 直接重新入队；
    * 超过 `max_retry` 的失败任务不自动重跑（留给人工在任务列表点重试）。
"""

from __future__ import annotations

import asyncio
import threading
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.database import get_session_factory
from app.core.logging import get_logger
from app.models.enums import ACTIVE_TASK_STATUSES, TaskStatus
from app.models.task import TaskRecord
from app.tasks.runner import TaskRunner, get_task_runner
from app.utils.kit import utc_now

logger = get_logger(__name__)

__all__ = [
    "RecoveryStats",
    "recover_pending_tasks",
    "reclaim_stuck_tasks",
    "scan_pending_tasks",
    "start_watchdog",
    "stop_watchdog",
]

# 卡死任务的错误码（供前端与运维检索）
STUCK_ERROR_CODE = "STUCK_TIMEOUT"


class RecoveryStats:
    """恢复结果统计。"""

    def __init__(self) -> None:
        """初始化统计。"""
        self.scanned = 0
        self.recovered = 0
        self.reset_running = 0
        self.skipped = 0
        self.task_ids: list[int] = []

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "scanned": self.scanned,
            "recovered": self.recovered,
            "reset_running": self.reset_running,
            "skipped": self.skipped,
            "task_ids": list(self.task_ids),
        }

    def __str__(self) -> str:  # noqa: D105
        return (
            f"扫描 {self.scanned} 条，恢复 {self.recovered} 条"
            f"（其中 running→pending {self.reset_running} 条），跳过 {self.skipped} 条"
        )


async def scan_pending_tasks(session: Any, limit: int = 200) -> list[TaskRecord]:
    """扫描未完成任务（pending / running），按优先级与创建时间排序。"""
    stmt = (
        select(TaskRecord)
        .where(TaskRecord.status.in_(list(ACTIVE_TASK_STATUSES)))
        .order_by(TaskRecord.priority.asc(), TaskRecord.id.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def recover_pending_tasks(
    session: Any = None,
    runner: TaskRunner | None = None,
    settings: Settings | None = None,
    limit: int | None = None,
) -> RecoveryStats:
    """★ 重启恢复入口：扫描 → 重置 running → 重新入队。

    Args:
        session: 可选会话；None 时自建。
        runner: 可选任务运行器；None 时用全局单例。
        settings: 配置。
        limit: 单次恢复上限，默认取 `task_recovery_limit`。

    Returns:
        `RecoveryStats` 统计结果。
    """
    settings = settings or get_settings()
    if not settings.task_recovery_enabled:
        # ★ 关闭恢复时**也要扫描并如实报告**：早期版本直接返回空统计（recovered=0），
        #   让人误以为"没有残留任务"，实际 running 残留仍在库里（静默失效）。
        stats = RecoveryStats()
        try:
            factory = get_session_factory()
            async with factory() as probe_session:
                leftovers = list((await probe_session.execute(
                    select(TaskRecord.id, TaskRecord.status).where(
                        TaskRecord.status.in_(list(ACTIVE_TASK_STATUSES))
                    )
                )).all())
            stats.scanned = len(leftovers)
            if leftovers:
                logger.warning(
                    "task_recovery_disabled_but_active_found",
                    reason="TASK_RECOVERY_ENABLED=False",
                    active_count=len(leftovers),
                    hint="恢复已关闭，这些任务不会被重新调度；如需恢复请开启配置或手动重试",
                    sample=[{"id": int(i), "status": s} for i, s in leftovers[:10]],
                )
            else:
                logger.info("task_recovery_disabled", reason="TASK_RECOVERY_ENABLED=False")
        except Exception as exc:  # noqa: BLE001  扫描失败不得阻断启动
            logger.warning("task_recovery_probe_failed", error=str(exc))
        return stats

    max_items = int(limit or settings.task_recovery_limit)
    runner = runner or get_task_runner(settings)
    stats = RecoveryStats()

    owns_session = session is None
    if owns_session:
        factory = get_session_factory()
        ctx = factory()
        session = await ctx.__aenter__()
    else:
        ctx = None

    try:
        records = await scan_pending_tasks(session, max_items)
        stats.scanned = len(records)
        if not records:
            logger.info("task_recovery_nothing_to_do")
            return stats

        for record in records:
            # running → pending（上次进程中断，重新执行）
            if record.status == TaskStatus.RUNNING.value:
                record.status = TaskStatus.PENDING.value
                record.started_at = None
                record.worker_id = None
                stats.reset_running += 1

            if not record.can_retry:
                record.status = TaskStatus.FAILED.value
                record.error_message = (record.error_message or "") + " | 重启恢复时已超过最大重试次数"
                stats.skipped += 1
                continue

            stats.recovered += 1
            stats.task_ids.append(int(record.id))

        await session.commit()

        # 提交给运行器（不重复写 TaskRecord，直接执行）
        for record in records:
            if int(record.id) in stats.task_ids:
                try:
                    await _requeue(runner, int(record.id), str(record.task_type), str(record.trace_id or ""))
                except Exception as exc:  # noqa: BLE001
                    logger.error("task_requeue_failed", task_id=record.id, error=str(exc))
                    stats.skipped += 1

        logger.info(
            "task_recovery_done",
            scanned=stats.scanned,
            recovered=stats.recovered,
            reset_running=stats.reset_running,
            skipped=stats.skipped,
        )
        return stats
    finally:
        if ctx is not None:
            await ctx.__aexit__(None, None, None)


async def reclaim_stuck_tasks(
    session: Any = None,
    *,
    timeout_sec: float | None = None,
    settings: Settings | None = None,
    runner: TaskRunner | None = None,
    limit: int = 200,
) -> RecoveryStats:
    """★ 卡死任务回收（看门狗）：`running` 超过阈值仍未结束 → 判死 → 失败 + 重置重试。

    ★ 为什么必须有这条：早期只有**启动时**的恢复，进程运行期间若有任务卡在
      `running`（例如等待外部产出的 ai_rework），它会一直挂着不动：
      既不会失败、也不会重跑，还能顺带锁死 SQLite 的写能力（单写者）。
      启动恢复看不到它（它是在本次进程里卡住的），于是出现
      "recovered=0，但库里明明有 running 残留" 的假象。

    Args:
        session: 可选会话；None 时自建。
        timeout_sec: 卡死阈值秒数；None 时取 `task.stuck_timeout_sec`。
        settings: 配置。
        runner: 任务运行器；None 时用全局单例。
        limit: 单次回收上限。

    Returns:
        `RecoveryStats`：`scanned` = 扫描到的 running 数，
        `reset_running` = 判定卡死数，`recovered` = 已重置并重新入队数。
    """
    settings = settings or get_settings()
    threshold = float(timeout_sec if timeout_sec is not None else settings.task_stuck_timeout_sec)
    threshold = max(threshold, 1.0)
    cutoff = utc_now() - timedelta(seconds=threshold)
    runner = runner or get_task_runner(settings)
    stats = RecoveryStats()

    owns_session = session is None
    if owns_session:
        factory = get_session_factory()
        ctx = factory()
        session = await ctx.__aenter__()
    else:
        ctx = None

    try:
        stmt = (
            select(TaskRecord)
            .where(TaskRecord.status == TaskStatus.RUNNING.value)
            .order_by(TaskRecord.id.asc())
            .limit(int(limit))
        )
        records = list((await session.execute(stmt)).scalars().all())
        stats.scanned = len(records)
        if not records:
            return stats

        requeue_meta: dict[int, tuple[str, str]] = {}
        for record in records:
            started = record.started_at or record.created_at
            # ★ 只回收"确实超时"的：刚开始跑的任务不动
            if started is not None and started > cutoff:
                continue
            record.mark_failed(
                error_code=STUCK_ERROR_CODE,
                error_message=(
                    f"running 超过 {int(threshold)}s 未结束，判定卡死并被看门狗回收"
                    f"（started_at={record.started_at}）"
                ),
            )
            stats.reset_running += 1
            if record.can_retry:
                requeue_meta[int(record.id)] = (str(record.task_type), str(record.trace_id or ""))
                record.reset_for_retry()
                stats.recovered += 1
                stats.task_ids.append(int(record.id))
            else:
                stats.skipped += 1

        await session.commit()

        for task_id, (task_type, trace_id) in requeue_meta.items():
            try:
                await _requeue(runner, task_id, task_type, trace_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("task_requeue_failed", task_id=task_id, error=str(exc))
                stats.skipped += 1

        if stats.reset_running:
            logger.warning(
                "task_stuck_reclaimed",
                scanned=stats.scanned,
                stuck=stats.reset_running,
                recovered=stats.recovered,
                threshold_sec=int(threshold),
                task_ids=stats.task_ids[:20],
            )
        return stats
    finally:
        if ctx is not None:
            await ctx.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
#  看门狗：进程运行期间周期性回收卡死任务
# ---------------------------------------------------------------------------

_watchdog_thread: threading.Thread | None = None
_watchdog_stop = threading.Event()


def start_watchdog(
    settings: Settings | None = None,
    runner: TaskRunner | None = None,
    interval_sec: float | None = None,
) -> None:
    """启动看门狗守护线程（周期性调用 `reclaim_stuck_tasks()`）。

    ★ 独立于 APScheduler：调度器只负责**提交**任务，看门狗负责**回收**卡死任务，
      两者职责分离，避免"自己提交的任务自己判死"的循环。
    """
    global _watchdog_thread
    settings = settings or get_settings()
    if not settings.task_watchdog_enabled:
        logger.info("task_watchdog_disabled", reason="TASK_WATCHDOG_ENABLED=False")
        return
    if _watchdog_thread is not None and _watchdog_thread.is_alive():
        return

    interval = max(float(interval_sec or settings.task_watchdog_interval_sec), 5.0)
    _watchdog_stop.clear()
    active_runner = runner or get_task_runner(settings)

    def _loop() -> None:
        """守护循环：每轮独立事件循环，异常绝不逃逸。"""
        while not _watchdog_stop.wait(interval):
            try:
                stats = asyncio.run(reclaim_stuck_tasks(runner=active_runner, settings=settings))
                if stats.reset_running:
                    logger.warning("task_watchdog_reclaimed", **stats.to_dict())
            except Exception as exc:  # noqa: BLE001  ★ 守护线程永不因异常退出
                logger.warning("task_watchdog_round_failed", error=str(exc))

    _watchdog_thread = threading.Thread(target=_loop, name="erp-task-watchdog", daemon=True)
    _watchdog_thread.start()
    logger.info(
        "task_watchdog_started",
        interval_sec=interval,
        stuck_timeout_sec=float(settings.task_stuck_timeout_sec),
    )


def stop_watchdog(timeout_sec: float = 3.0) -> None:
    """停止看门狗（进程退出时调用）。"""
    global _watchdog_thread
    _watchdog_stop.set()
    if _watchdog_thread is not None:
        _watchdog_thread.join(timeout=timeout_sec)
        _watchdog_thread = None
        logger.info("task_watchdog_stopped")


async def _requeue(runner: TaskRunner, task_id: int, task_type: str, trace_id: str) -> None:
    """把已存在的 TaskRecord 重新投递到执行器。

    ★ 直接复用 `runner.retry()` 的入队语义不适配（要求 failed/cancelled），
      故此处调用运行器的内部入队钩子（LocalTaskRunner 提供 `_enqueue`）。
    """
    enqueue = getattr(runner, "_enqueue", None)
    if callable(enqueue):
        enqueue(task_id, task_type, trace_id)
        return
    # 通用回退：重新 submit（会新建 TaskRecord，语义略不同但保证任务被执行）
    await runner.submit(task_type, {"recovered_from": task_id})
