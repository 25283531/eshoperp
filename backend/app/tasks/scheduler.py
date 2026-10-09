"""APScheduler 装配：定时任务注册与启停（ADR-3）。

周期任务从 `SystemSetting` 读取（分钟级），改配置后调用 `reschedule()` 生效：
    * `order.sync_interval_min`      → order_sync
    * `inventory.poll_interval_min`  → inventory_sync
    * `mapping_check`                → 每日 03:10（UTC 存储，展示按本地时区）
    * `fulfillment.heartbeat_interval_sec` → 适配器心跳（T-A06 提供处理器时自动生效）

★ 调度器本身不执行业务逻辑，只负责按周期 `TaskRunner.submit()`，
  真正的执行与状态持久化仍走 TaskRecord（重启可恢复）。

★ 相位错开（2026-10-08 修复）
    `order_sync` 与 `purchase_place` 同为 `interval 5min` 且同相位启动 ⇒
    必然每 5 分钟在**同一秒**并发提交，抢 SQLite 单写者写锁。抢输的一方
    只记一行 error 日志就放弃（不重试、不补跑）——界面无任何提示，
    已匹配订单的自动下单会**静默跳过一整轮**（实测日志见 `phase_offset_sec` 注释）。
    因此每个周期任务可声明 `phase_offset_sec`，把首次触发时间错开。
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.models.enums import SettingKey, TaskType
from app.models.system import SystemSetting
from app.tasks.runner import TaskRunner, get_task_runner
from app.utils.kit import utc_now

logger = get_logger(__name__)

__all__ = [
    "SCHEDULED_JOBS",
    "SUBMIT_FALLBACK_JOIN_TIMEOUT_SEC",
    "SUBMIT_MAX_ATTEMPTS",
    "SUBMIT_RETRY_BASE_DELAY_SEC",
    "SUBMIT_RETRY_MAX_DELAY_SEC",
    "build_scheduler",
    "get_scheduler",
    "get_scheduler_health",
    "is_running",
    "reschedule",
    "reset_submit_failure_stats",
    "start_scheduler",
    "stop_scheduler",
    "submit_scheduled_task",
]

# 定时任务定义：(job_id, task_type, 周期配置键, 默认周期分钟, 相位偏移秒, 说明)
#
# ★ `phase_offset_sec`：首次触发相对「基准时刻 + 一个周期」的额外偏移。
#   取值刻意选**非周期约数**：order_sync 与 purchase_place 都是 5min（300s），
#   45 不是 300 的约数，两者永远不会再撞到同一秒；inventory_sync 30min（1800s）
#   给 30s，同样与 300s / 1800s 网格错开。
SCHEDULED_JOBS: list[dict[str, Any]] = [
    {
        "job_id": "order_sync",
        "task_type": TaskType.ORDER_SYNC.value,
        "setting_key": SettingKey.ORDER_SYNC_INTERVAL_MIN.value,
        "default_minutes": 5,
        "phase_offset_sec": 0,
        "description": "定时增量拉取订单（当前生效履约适配器）",
    },
    {
        "job_id": "inventory_sync",
        "task_type": TaskType.INVENTORY_SYNC.value,
        "setting_key": SettingKey.INVENTORY_POLL_INTERVAL_MIN.value,
        "default_minutes": 30,
        "phase_offset_sec": 30,
        "description": "库存 / 价格轮询兜底 + 自动下架决策",
    },
    {
        "job_id": "mapping_check",
        "task_type": TaskType.MAPPING_CHECK.value,
        "setting_key": None,
        "cron": {"hour": 3, "minute": 10},
        "phase_offset_sec": 0,
        "description": "每日 03:10 货源端变更检测 → 生成映射待确认工单",
    },
    {
        # ★ 履约主路径闭环：已匹配订单 → 自动下单（此前 place_purchase 无入口，链路断在"已匹配"）
        # ★ phase_offset_sec=45：与同为 5min 的 order_sync 错开，避免同秒抢 SQLite 写锁
        "job_id": "purchase_place",
        "task_type": TaskType.PURCHASE_PLACE.value,
        "setting_key": SettingKey.ORDER_PURCHASE_INTERVAL_MIN.value,
        "default_minutes": 5,
        "phase_offset_sec": 45,
        "description": "已匹配订单自动采购下单（受 order.auto_purchase_enabled 控制）",
    },
]

# ★ 定时提交的重试策略（总耗时上界约 3s，远小于 5min 的周期，不会拖住下一轮）
SUBMIT_MAX_ATTEMPTS: int = 3  # 1 次首次提交 + 2 次重试
SUBMIT_RETRY_BASE_DELAY_SEC: float = 1.0  # 第 n 次重试前等待 base * n 秒
SUBMIT_RETRY_MAX_DELAY_SEC: float = 4.0  # 单次等待上界
# 退化路径（调用方已有事件循环）最多等这么久，避免吊住 APScheduler 的调度线程
SUBMIT_FALLBACK_JOIN_TIMEOUT_SEC: float = 30.0

_scheduler: BackgroundScheduler | None = None

# ★ 定时提交的失败台账（内存视图；★ 失败时**根本没有 TaskRecord 落库**，
#   所以这张表是"本轮被静默跳过"的**唯一**证据，重启恢复与看门狗都看不到它）
_submit_failures: dict[str, dict[str, Any]] = {}
_submit_failures_lock = threading.Lock()


def _read_interval_minutes(session: Any, key: str | None, default_minutes: int) -> int:
    """从 SystemSetting 读取周期（分钟），失败回退默认值。"""
    if not key or session is None:
        return default_minutes
    try:
        from sqlalchemy import select

        stmt = select(SystemSetting).where(SystemSetting.setting_key == key)
        row = session.execute(stmt).scalar_one_or_none()
        if row is None or row.setting_value is None:
            return default_minutes
        return max(int(str(row.setting_value).strip()), 1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("scheduler_setting_read_failed", key=key, error=str(exc))
        return default_minutes


def _record_submit_failure(task_type: str, error: BaseException, attempts: int) -> dict[str, Any]:
    """★ 提交最终失败：累加「连续失败次数」并返回快照。

    ★★ 为什么必须留一份台账 ★★
        提交失败 ⇒ `task_record` **一行都没写** ⇒ 任务恢复（`recover_pending_tasks`）
        和看门狗（`reclaim_stuck_tasks`）都扫不到它 ⇒ **本轮被永久跳过且无人知晓**。
        早期实现只在日志里留一行 error 就结束，运营侧看到的是"最近一次任务是 5 分钟前"，
        完全看不出中间少跑了一轮 —— 这是本项目最不接受的一类问题：静默失效。
    """
    with _submit_failures_lock:
        entry = dict(_submit_failures.get(task_type) or {})
        entry["consecutive_failures"] = int(entry.get("consecutive_failures") or 0) + 1
        entry["total_failures"] = int(entry.get("total_failures") or 0) + 1
        entry["last_error"] = str(error)
        entry["last_error_type"] = type(error).__name__
        entry["last_failed_at"] = utc_now().isoformat()
        entry["last_attempts"] = int(attempts)
        _submit_failures[task_type] = entry
        return dict(entry)


def _clear_submit_failure(task_type: str) -> None:
    """提交成功：清零连续失败计数（保留累计次数与最后成功时间用于诊断）。"""
    with _submit_failures_lock:
        entry = dict(_submit_failures.get(task_type) or {})
        if not entry:
            return
        entry["consecutive_failures"] = 0
        entry["last_success_at"] = utc_now().isoformat()
        _submit_failures[task_type] = entry


def reset_submit_failure_stats() -> None:
    """清空失败台账（运维处理完告警后调用 / 测试隔离用）。"""
    with _submit_failures_lock:
        _submit_failures.clear()


def get_scheduler_health() -> dict[str, Any]:
    """★ 调度器健康快照（供 `/health` 复用既有健康检查通道，不新造一套告警）。

    Returns:
        `running` 调度器是否在跑；`jobs` 当前任务；`submit_failures` 失败台账；
        `unhealthy` 是否存在「本轮被静默跳过」的任务；`status` 汇总状态。
    """
    with _submit_failures_lock:
        failures = {key: dict(value) for key, value in _submit_failures.items()}
    unhealthy = {
        key: value for key, value in failures.items() if int(value.get("consecutive_failures") or 0) > 0
    }
    if unhealthy:
        status = "degraded"
    elif is_running():
        status = "ok"
    else:
        status = "stopped"
    return {
        "status": status,
        "running": is_running(),
        "unhealthy": bool(unhealthy),
        "unhealthy_task_types": sorted(unhealthy),
        "job_count": len(list_jobs()),
        "jobs": list_jobs(),
        "submit_failures": failures,
    }


async def submit_scheduled_task(
    task_type: str,
    runner: TaskRunner,
    trace_source: str,
    *,
    max_attempts: int | None = None,
    base_delay_sec: float | None = None,
    max_delay_sec: float | None = None,
) -> int | None:
    """★ 定时提交（带重试）：成功返回 `TaskRecord.id`，重试耗尽返回 `None`。

    ★★ 为什么必须重试 ★★
        SQLite 是单写者模型，`order_sync` 与 `purchase_place` 曾在同一秒并发写入
        `task_record`，抢输的一方直接抛
        `sqlite3.OperationalError: attempt to write a readonly database`。
        早期实现**只记一行 error 就放弃**：那一轮的自动下单被整轮跳过，
        界面上什么也看不到（下一轮 5 分钟后才补上）。写锁冲突是**瞬时**的，
        重试一次几乎必成 —— 不重试等于把一次可自愈的抖动变成一次静默丢单。

    ★ 只重试「提交」这一步，不重试任务本身：任务内部的重试语义由 `TaskRecord` /
      `is_retryable_error` 负责，这里不动，避免混两层重试把结论推迟。

    Args:
        task_type: 任务类型。
        runner: 任务运行器。
        trace_source: 触发来源（job_id），进 payload 便于追溯。
        max_attempts: 最大尝试次数（含首次），默认 `SUBMIT_MAX_ATTEMPTS`。
        base_delay_sec: 重试基础等待秒数，默认 `SUBMIT_RETRY_BASE_DELAY_SEC`。
        max_delay_sec: 单次等待上界，默认 `SUBMIT_RETRY_MAX_DELAY_SEC`。

    Returns:
        成功时 `TaskRecord.id`；全部尝试失败时 `None`（并升级为 critical 日志 + 台账）。
    """
    attempts_limit = max(1, int(max_attempts if max_attempts is not None else SUBMIT_MAX_ATTEMPTS))
    base_delay = float(base_delay_sec if base_delay_sec is not None else SUBMIT_RETRY_BASE_DELAY_SEC)
    delay_cap = float(max_delay_sec if max_delay_sec is not None else SUBMIT_RETRY_MAX_DELAY_SEC)

    last_error: BaseException = RuntimeError("未知提交失败")
    for attempt in range(1, attempts_limit + 1):
        try:
            # ★ payload 保持与修复前一致（`{"trigger": trace_source}`）：不动任务语义
            task_id = await runner.submit(
                task_type, {"trigger": trace_source}, task_key=f"{task_type}:scheduled"
            )
        except Exception as exc:  # noqa: BLE001  瞬时写冲突 / 连接抖动都可能自愈
            last_error = exc
            logger.warning(
                "scheduled_task_submit_failed",
                task_type=task_type,
                attempt=attempt,
                max_attempts=attempts_limit,
                error=str(exc),
                error_type=type(exc).__name__,
                will_retry=attempt < attempts_limit,
            )
            if attempt < attempts_limit:
                await asyncio.sleep(min(base_delay * attempt, delay_cap))
            continue

        logger.info(
            "scheduled_task_submitted",
            task_type=task_type,
            task_id=task_id,
            attempt=attempt,
        )
        _clear_submit_failure(task_type)
        return int(task_id)

    # ★ 重试耗尽：本轮确实没跑成，且库里没有任何记录会替它"补跑"
    #   ⇒ 必须留下可诊断、可巡检的痕迹（critical 级 + 台账 + /health 可见）。
    entry = _record_submit_failure(task_type, last_error, attempts_limit)
    logger.critical(
        "scheduled_task_submit_gave_up",
        task_type=task_type,
        attempts=attempts_limit,
        consecutive_failures=entry["consecutive_failures"],
        total_failures=entry["total_failures"],
        error=str(last_error),
        error_type=entry["last_error_type"],
        hint="本轮已被跳过且无 TaskRecord 落库（任务恢复/看门狗都扫不到），下一个周期才会补跑",
    )
    return None


def _job_wrapper(task_type: str, runner: TaskRunner, trace_source: str) -> None:
    """定时任务入口：在线程内用独立事件循环提交任务（失败自动重试）。

    ★ 提交即返回，不等待执行完成 —— 执行状态由 TaskRecord 持久化。
    ★★ 本函数**绝不向 APScheduler 抛异常**：抛出去只会被记成一次 job 失败，
       而"本轮没跑成"这件事本身没有任何记录 ⇒ 又是一次静默失效。
    """
    async def _submit() -> None:
        await submit_scheduled_task(task_type, runner, trace_source)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 常规路径（APScheduler 工作线程）：本线程没有运行中的事件循环
        asyncio.run(_submit())
        return

    # ★ 退化路径：调用方已在事件循环中。
    #   早期实现在这里 `asyncio.new_event_loop().run_until_complete(...)`，
    #   而 Python 3.10+ 会直接抛 "Cannot run the event loop while another loop
    #   is running" ⇒ 异常反而穿进了 APScheduler ⇒ 任务被静默跳过。
    #   改为投递到独立守护线程执行，并**有上界**地等待（绝不吊住调度线程）。
    def _run_in_thread() -> None:
        try:
            asyncio.run(_submit())
        except BaseException as exc:  # noqa: BLE001  退化路径同样不许把异常抛出去
            logger.error(
                "scheduled_task_submit_fallback_failed",
                task_type=task_type,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    worker = threading.Thread(target=_run_in_thread, name="erp-sched-submit", daemon=True)
    worker.start()
    worker.join(timeout=SUBMIT_FALLBACK_JOIN_TIMEOUT_SEC)


def build_scheduler(settings: Settings | None = None, runner: TaskRunner | None = None) -> BackgroundScheduler:
    """构建并注册全部定时任务（不启动）。"""
    settings = settings or get_settings()
    runner = runner or get_task_runner(settings)
    scheduler = BackgroundScheduler(timezone=settings.scheduler_timezone)

    # 读取周期（同步会话：APScheduler 在线程中触发，此处仅在启动时执行一次）
    intervals: dict[str, int] = {}
    try:
        from app.core.database import get_session_factory

        from sqlalchemy import select

        factory = get_session_factory()

        async def _load() -> dict[str, int]:
            result: dict[str, int] = {}
            async with factory() as session:
                for job in SCHEDULED_JOBS:
                    key = job.get("setting_key")
                    if not key:
                        continue
                    row = (
                        await session.execute(select(SystemSetting).where(SystemSetting.setting_key == key))
                    ).scalar_one_or_none()
                    if row is not None and row.setting_value:
                        try:
                            result[str(key)] = max(int(str(row.setting_value).strip()), 1)
                        except (TypeError, ValueError):
                            result[str(key)] = int(job["default_minutes"])
            return result

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:  # pragma: no cover  启动时一般无运行中的循环
            intervals = {}
        else:
            intervals = asyncio.run(_load())
    except Exception as exc:  # noqa: BLE001
        logger.warning("scheduler_interval_load_failed", error=str(exc))
        intervals = {}

    # ★ 相位基准：全任务共用同一个「现在」，保证偏移只来自 phase_offset_sec
    base_time = datetime.now(scheduler.timezone)
    for job in SCHEDULED_JOBS:
        job_id = str(job["job_id"])
        task_type = str(job["task_type"])
        phase_offset_sec = int(job.get("phase_offset_sec") or 0)
        if job.get("cron"):
            trigger = CronTrigger(**dict(job["cron"]))
        else:
            minutes = intervals.get(str(job.get("setting_key")), int(job["default_minutes"]))
            # ★ APScheduler 默认 start_date = now + interval（即首次触发在一个周期后）。
            #   这里在它之上再叠加相位偏移：**首次触发时刻后移，但仍是"一个周期后"**，
            #   既保持原有语义，又让同周期任务不再撞在同一秒。
            trigger = IntervalTrigger(
                minutes=minutes,
                start_date=base_time + timedelta(minutes=minutes, seconds=phase_offset_sec),
            )
        scheduler.add_job(
            _job_wrapper,
            trigger=trigger,
            id=job_id,
            name=job_id,
            args=[task_type, runner, job_id],
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )
        # ★ 不能读 `job.next_run_time`：调度器**未启动**时它是未赋值的 slot（取值会抛
        #   AttributeError）。这里直接问触发器要首次触发时间，构建期就能验证相位错开。
        planned_first_fire = trigger.get_next_fire_time(None, base_time)
        logger.info(
            "scheduler_job_added",
            job_id=job_id,
            task_type=task_type,
            trigger=str(trigger),
            phase_offset_sec=phase_offset_sec,
            first_fire_time=planned_first_fire.isoformat() if planned_first_fire else None,
        )

    return scheduler


def start_scheduler(settings: Settings | None = None, runner: TaskRunner | None = None) -> BackgroundScheduler | None:
    """启动调度器（幂等）。返回调度器实例；未启用时返回 None。"""
    global _scheduler
    settings = settings or get_settings()
    if not settings.scheduler_enabled:
        logger.info("scheduler_disabled", reason="SCHEDULER_ENABLED=False")
        return None
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    _scheduler = build_scheduler(settings, runner)
    _scheduler.start()
    logger.info("scheduler_started", jobs=[job.id for job in _scheduler.get_jobs()])
    return _scheduler


def stop_scheduler(wait: bool = False) -> None:
    """停止调度器。"""
    global _scheduler
    if _scheduler is None:
        return
    try:
        if _scheduler.running:
            _scheduler.shutdown(wait=wait)
        logger.info("scheduler_stopped")
    except Exception as exc:  # noqa: BLE001
        logger.warning("scheduler_stop_failed", error=str(exc))
    finally:
        _scheduler = None


def get_scheduler() -> BackgroundScheduler | None:
    """获取当前调度器。"""
    return _scheduler


def is_running() -> bool:
    """调度器是否在运行。"""
    return _scheduler is not None and _scheduler.running


def reschedule(settings: Settings | None = None, runner: TaskRunner | None = None) -> BackgroundScheduler | None:
    """配置变更后重建调度器（热生效）。"""
    stop_scheduler(wait=False)
    return start_scheduler(settings, runner)


def list_jobs() -> list[dict[str, Any]]:
    """列出当前调度任务（供后台展示）。"""
    if _scheduler is None:
        return []
    return [
        {
            "id": job.id,
            "name": job.name,
            "trigger": str(job.trigger),
            # ★ 用 `getattr` 取值：调度器**未启动**时 `next_run_time` 这个 slot 尚未赋值，
            #   直接 `job.next_run_time` 会抛 AttributeError（本项目 /health 会调到这里，
            #   健康自检绝不能因取值方式而 500）。
            "next_run_time": _iso_or_none(getattr(job, "next_run_time", None)),
        }
        for job in _scheduler.get_jobs()
    ]


def _iso_or_none(value: Any) -> str | None:
    """把 `datetime` 转成 ISO 字符串；None 原样返回。"""
    if value is None:
        return None
    return str(value.isoformat())
