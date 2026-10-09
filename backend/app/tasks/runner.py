"""★ TaskRunner 抽象接口 + 本地实现（ADR-3：Windows 不用 Celery + Redis）。

设计目标：**未来换 Celery 只替换本文件实现，业务代码零改动**。

    submit(task_type, payload, *, task_key=None, priority=5, max_retry=3) -> task_id
    register_handler(task_type, handler)
    schedule(task_type, payload, *, cron=None, interval_sec=None) -> job_id
    cancel(task_id) -> bool
    retry(task_id) -> bool
    get_status(task_id) -> dict

本地实现：ThreadPoolExecutor + 每任务独立事件循环（`asyncio.run`）+ TaskRecord 持久化。
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from queue import SimpleQueue
from typing import Any

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.database import get_session_factory
from app.core.errors import BusinessError
from app.core.logging import get_logger, new_trace_id
from app.models.enums import ACTIVE_TASK_STATUSES, TaskStatus
from app.models.task import TaskRecord
from app.tasks.registry import TASK_HANDLER_REGISTRY, TaskHandler, get_handler

logger = get_logger(__name__)

# ★ 处理器「声明终态」的保留键。
#   为什么需要：早期**只要处理器没抛异常就一律 success**。于是「采集 10 个商品全部失败」
#   也报 success —— 前端只看 status，运营看到"任务成功"却一条货源都没有，
#   这是典型的静默失效（失败明细明明就在 result_json 里，但没人会去看一个"成功"的任务）。
#   现在处理器可以用这个键声明：本次虽然一次异常都没抛，但**结论是失败**。
#   取值目前只支持 `failed`（其它值记录告警并按成功处理，避免语义漂移）。
TASK_STATUS_OVERRIDE_KEY = "__task_status__"
TASK_STATUS_REASON_KEY = "__task_status_reason__"


def is_retryable_error(error: BaseException) -> bool:
    """★ 只有「可能自愈」的异常才值得重试。

    `BusinessError`（参数为空 / 越权 / 唯一冲突 / 状态冲突 …）是**确定性**的：
    重试一万次结论完全一样。早期实现无条件重试到 `max_retry`，后果很具体 ——
    实测「空 identifiers 的采集任务」被重试 3 次，`status` 在
    `pending` / `running` 之间反复横跳，**结论被推迟、且中途读不到稳定终态**，
    还把 `task_record` 刷成一片噪声（3 条 warn 日志却没有任何一条是最终结论）。

    网络超时 / 数据库瞬时不可用这类**可能自愈**的错误仍然照常重试。
    """
    return not isinstance(error, BusinessError)


# ★ 数据库「可自愈」错误的指纹。命中即**重试写终态**，而不是把任务丢在 running。
#   这些全部是瞬时状态：等一下（busy_timeout 内）或重读一次就能成功。
TRANSIENT_DB_ERROR_HINTS: tuple[str, ...] = (
    "database is locked",  # SQLite 单写者：别人正持写锁
    "database is busy",
    "database table is locked",
    "attempt to write a readonly database",  # 实测撞过：持锁方释放瞬间的窗口
    "disk i/o error",
    "database schema has changed",
    # StaleDataError：UPDATE 期望 1 行却匹配 0 行 —— 行被并发改动/删除，
    # 在**已失效**的 session 上重 commit 没用，必须重读 + 重放变更。
    "expected to update",
)


def is_transient_db_error(error: BaseException) -> bool:
    """★ 数据库瞬时错误（写锁 / 行被并发改动）判定：这类错误**重试一次就好**。

    与 `is_retryable_error` 的分工：
        * `is_retryable_error` 决定「**业务逻辑**要不要重跑」（BusinessError 不重跑）；
        * `is_transient_db_error` 决定「**写终态这次提交**要不要重试」（纯基础设施抖动）。
    两者混为一谈的后果很具体：终态写不进去 ⇒ 任务永久停在 `running`
    ⇒ "受理了却永远没有结论"，这是本项目已经栽过一次的静默失效。
    """
    candidates: list[BaseException] = [error]
    # SQLAlchemy 会把底层驱动的异常挂在 `.orig` 上
    original = getattr(error, "orig", None)
    if isinstance(original, BaseException):
        candidates.append(original)
    if isinstance(error, TimeoutError):  # asyncio.TimeoutError 在 3.11 即 TimeoutError
        return True
    return any(
        any(hint in f"{type(item).__name__}: {item}".lower() for hint in TRANSIENT_DB_ERROR_HINTS)
        for item in candidates
    )


__all__ = [
    "TASK_STATUS_OVERRIDE_KEY",
    "TASK_STATUS_REASON_KEY",
    "TRANSIENT_DB_ERROR_HINTS",
    "is_transient_db_error",
    "TaskContext",
    "TaskRunner",
    "LocalTaskRunner",
    "get_task_runner",
    "set_task_runner",
    "shutdown_task_runner",
]


@dataclass
class TaskContext:
    """任务执行上下文（传给处理器）。"""

    task_id: int
    task_type: str
    task_key: str = ""
    trace_id: str = ""
    retry_count: int = 0
    worker_id: str = ""
    started_at: float = field(default_factory=time.perf_counter)

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "task_key": self.task_key,
            "trace_id": self.trace_id,
            "retry_count": self.retry_count,
            "worker_id": self.worker_id,
        }


class TaskRunner(ABC):
    """★ 异步任务运行器接口（业务层只依赖本接口）。"""

    @abstractmethod
    async def submit(
        self,
        task_type: str,
        payload: dict[str, Any],
        *,
        task_key: str | None = None,
        priority: int = 5,
        max_retry: int = 3,
        scheduled_at: Any = None,
    ) -> int:
        """提交任务，返回 `TaskRecord.id`。相同 `task_key` 且已有活跃任务时返回既有任务 ID（幂等）。"""
        raise NotImplementedError

    @abstractmethod
    def register_handler(self, task_type: str, handler: TaskHandler) -> None:
        """注册任务处理器。"""
        raise NotImplementedError

    @abstractmethod
    async def cancel(self, task_id: int) -> bool:
        """取消任务（仅 pending / running 可取消）。"""
        raise NotImplementedError

    @abstractmethod
    async def retry(self, task_id: int) -> bool:
        """重试失败任务。"""
        raise NotImplementedError

    @abstractmethod
    async def get_status(self, task_id: int) -> dict[str, Any]:
        """查询任务状态。"""
        raise NotImplementedError

    @abstractmethod
    def shutdown(self, wait: bool = False) -> None:
        """关闭运行器。"""
        raise NotImplementedError


class DaemonWorkerPool:
    """★ 守护线程池（自研，替代 `concurrent.futures.ThreadPoolExecutor`）。

    ============================================================================
    ★★ 为什么不用标准库的 ThreadPoolExecutor（这是踩出来的坑，不是偏好）★★
    ============================================================================
    它的工作线程是**非 daemon** 的，`concurrent.futures.thread._python_exit`
    会在解释器退出时 `join()` 它们。而本项目**存在最长等待 1800s 的任务**：
    `ai_rework` 处理器要等 WorkBuddy 文件桥产出（`ai_poll_timeout_sec` 默认 1800）。
    于是只要有一个这种任务在跑，**进程就永远退不出去**。

    实测症状（很容易被误读）：
        `pytest` 打印完 `................ [100%]`、用例全绿，
        然后**卡在退出阶段永不返回**；faulthandler 抓栈才看到主线程停在
        `concurrent.futures.thread._python_exit → Thread.join()`，
        而被 join 的工作线程停在 `asyncio.run → run_forever → _poll`。

    ★ 正确组合是：**daemon 线程 + `task_record` 持久化 + 看门狗**
        进程退出时没跑完的任务留在 `running`（状态是落库的，不会丢），
        下次启动由 `reclaim_stuck_tasks()` 按超时回收并重新入队。
        而不是用"进程退不出去"来换"任务跑完" —— 后者在运维上更糟：
        服务停不下来，只能 kill -9，反而丢状态。
    """

    def __init__(self, max_workers: int = 4, name_prefix: str = "erp-task") -> None:
        """初始化守护线程池（工作线程**惰性**创建：没有任务就没有线程）。"""
        self.max_workers = max(1, int(max_workers))
        self.name_prefix = str(name_prefix)
        self._queue: SimpleQueue[Callable[[], Any] | None] = SimpleQueue()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._closed = False

    def _spawn_if_needed(self) -> None:
        """按需扩容：在途线程数小于 `max_workers` 时才新建。"""
        alive = [t for t in self._threads if t.is_alive()]
        self._threads = alive
        if len(alive) < self.max_workers:
            worker = threading.Thread(
                target=self._worker_loop,
                name=f"{self.name_prefix}-{len(self._threads)}",
                daemon=True,  # ★★ 关键：进程退出不被它吊住
            )
            worker.start()
            self._threads.append(worker)

    def _worker_loop(self) -> None:
        """线程主循环：取任务执行；收到 `None` 毒丸、或池已关闭即退出。"""
        while True:
            item = self._queue.get()
            # ★ `self._closed` 也退出：关闭时队列里**尚未开始**的任务直接丢弃
            #   （等价于 ThreadPoolExecutor 的 `cancel_futures=True`）。
            if item is None or self._closed:
                return
            try:
                item()
            except Exception as exc:  # noqa: BLE001  ★ 兜底：线程池内异常绝不逃逸
                logger.exception("task_pool_worker_crashed", error=str(exc))

    def submit(self, fn: Callable[[], Any]) -> bool:
        """投递一个任务；已关闭时返回 `False`（不抛异常，调用方自行降级）。"""
        with self._lock:
            if self._closed:
                return False
            self._spawn_if_needed()
        self._queue.put(fn)
        return True

    def shutdown(self, wait: bool = False, wait_timeout_sec: float = 5.0) -> None:
        """关闭线程池：投递毒丸；`wait=True` 时**有上界**地等待。

        ★ 为什么 `wait=True` 也要有上界：等待一个可能要跑 1800s 的任务
          没有意义，上界内没退出就直接返回，剩下的交给看门狗回收。
        """
        with self._lock:
            self._closed = True
            threads = list(self._threads)
        for _ in threads:
            self._queue.put(None)
        if not wait:
            return
        for thread in threads:
            thread.join(timeout=wait_timeout_sec)


class LocalTaskRunner(TaskRunner):
    """本地任务运行器：守护线程池 + 每任务独立事件循环。

    ★ 每个任务在自己的线程里 `asyncio.run(...)`，避免跨事件循环复用 aiosqlite 连接。
    """

    def __init__(self, settings: Settings | None = None, max_workers: int | None = None) -> None:
        """初始化本地运行器。"""
        self.settings = settings or get_settings()
        self.max_workers = int(max_workers or self.settings.task_max_workers)
        self._executor = DaemonWorkerPool(max_workers=self.max_workers, name_prefix="erp-task")
        # ★ 在途任务的任务号集合（内存视图；真正的事实来源是 `task_record` 表）
        self._futures: dict[int, bool] = {}
        self._lock = threading.Lock()
        self._closed = False

    # ---------------- 提交 ----------------

    async def submit(
        self,
        task_type: str,
        payload: dict[str, Any],
        *,
        task_key: str | None = None,
        priority: int = 5,
        max_retry: int = 3,
        scheduled_at: Any = None,
    ) -> int:
        """提交任务：先写 TaskRecord（幂等键去重），再投递线程池。"""
        if self._closed:
            raise RuntimeError("TaskRunner 已关闭，无法提交新任务")

        trace_id = new_trace_id()
        factory = get_session_factory()

        async with factory() as session:
            # 幂等：同一 task_key 存在活跃任务则直接返回
            if task_key:
                stmt = select(TaskRecord).where(
                    TaskRecord.task_key == task_key,
                    TaskRecord.status.in_(list(ACTIVE_TASK_STATUSES)),
                )
                existing = (await session.execute(stmt)).scalars().first()
                if existing is not None:
                    logger.info(
                        "task_submit_idempotent_hit",
                        task_key=task_key,
                        task_id=existing.id,
                        status=existing.status,
                    )
                    return int(existing.id)

            record = TaskRecord(
                task_type=task_type,
                task_key=task_key,
                payload_json=dict(payload or {}),
                status=TaskStatus.PENDING.value,
                priority=int(priority),
                max_retry=int(max_retry),
                scheduled_at=scheduled_at,
                trace_id=trace_id,
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)
            task_id = int(record.id)

        accepted = self._executor.submit(lambda: self._run_in_thread(task_id, task_type, trace_id))
        if not accepted:
            logger.warning("task_submit_rejected_runner_closed", task_id=task_id, task_type=task_type)
        else:
            with self._lock:
                self._futures[task_id] = True
        logger.info(
            "task_submitted",
            task_id=task_id,
            task_type=task_type,
            task_key=task_key,
            trace_id=trace_id,
            accepted=accepted,
        )
        return task_id

    # ---------------- 执行 ----------------

    def _run_in_thread(self, task_id: int, task_type: str, trace_id: str) -> None:
        """线程入口：为任务创建独立事件循环并执行。"""
        worker_id = f"{threading.current_thread().name}-{uuid.uuid4().hex[:6]}"
        try:
            asyncio.run(self._execute(task_id, task_type, trace_id, worker_id))
        except Exception as exc:  # noqa: BLE001  ★ 兜底：线程内异常绝不逃逸
            logger.exception("task_thread_crashed", task_id=task_id, task_type=task_type, error=str(exc))
        finally:
            with self._lock:
                self._futures.pop(task_id, None)

    async def _execute(self, task_id: int, task_type: str, trace_id: str, worker_id: str) -> None:
        """★ 执行单个任务的**外层收口**：任何异常都必须在事件循环内部结束。

        ★★ 为什么 `_execute_inner` 之外还要再包一层 ★★
            早期只有「调处理器」那一段被 try 包住。一旦**准备阶段**
            （读 TaskRecord / `mark_running` / `commit`）抛异常 —— 实测就撞上过
            SQLite 偶发的 `attempt to write a readonly database` —— 异常会穿出
            `asyncio.run()`。后果有两个，都很致命：

              1. `asyncio.run()` 收尾时会 `_cancel_all_tasks()`，把
                 `AsyncSession.close()` 这类收尾协程**取消掉** ⇒
                 aiosqlite 的连接线程（**非 daemon**）永远等不到停止信号 ⇒
                 解释器退出时被 `threading._shutdown` join ⇒
                 **用例全绿但 pytest 永不退出**（极易被误读成"测试挂了"）；
              2. `TaskRecord` 永久停在 `running`，没有任何线程会再碰它，
                 也没有看门狗在跑 ⇒ 任务**永久挂起**，前端和测试都读不到终态
                 （"受理了却永远 running"，与本项目已经栽过一次的
                  `/orders/sync` 是同一类静默失效）。

            所以这里保证：**异常一律在事件循环内被收口，任务必然落到终态**。
        """
        started = time.perf_counter()
        try:
            await self._execute_inner(task_id, task_type, trace_id, worker_id)
        except Exception as exc:  # noqa: BLE001  ★ 兜底：绝不让它穿出 asyncio.run
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.exception(
                "task_execute_crashed",
                task_id=task_id,
                task_type=task_type,
                error=str(exc),
            )
            try:
                await self._finish_failed(task_id, task_type, trace_id, exc, elapsed_ms)
            except Exception as inner_exc:  # noqa: BLE001  ★ 落终态失败也只能记日志
                logger.exception(
                    "task_mark_failed_failed",
                    task_id=task_id,
                    task_type=task_type,
                    error=str(inner_exc),
                )

    async def _execute_inner(self, task_id: int, task_type: str, trace_id: str, worker_id: str) -> None:
        """执行单个任务：改状态 → 调处理器 → 落结果 / 重试。"""
        factory = get_session_factory()
        handler = get_handler(task_type)

        async with factory() as session:
            record = await session.get(TaskRecord, task_id)
            if record is None:
                logger.error("task_record_missing", task_id=task_id)
                return
            if record.status == TaskStatus.CANCELLED.value:
                logger.info("task_skipped_cancelled", task_id=task_id)
                return

            if handler is None:
                record.mark_failed(error_code="HANDLER_NOT_FOUND", error_message=f"未注册处理器：{task_type}")
                await session.commit()
                logger.error("task_handler_not_found", task_type=task_type, task_id=task_id)
                return

            record.mark_running(worker_id=worker_id)
            await session.commit()

            ctx = TaskContext(
                task_id=task_id,
                task_type=task_type,
                task_key=record.task_key or "",
                trace_id=trace_id,
                retry_count=int(record.retry_count or 0),
                worker_id=worker_id,
            )

        payload = await self._load_payload(task_id)
        started = time.perf_counter()
        try:
            result = await handler(payload, ctx)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            result_dict = result if isinstance(result, dict) else {"value": result}
            declared_status = str(result_dict.pop(TASK_STATUS_OVERRIDE_KEY, "") or "").strip().lower()
            declared_reason = str(result_dict.pop(TASK_STATUS_REASON_KEY, "") or "").strip()
            if declared_status == TaskStatus.FAILED.value:
                # ★ 处理器声明「结论为失败」：绝不因为没抛异常就报 success
                await self._finish_declared_failed(task_id, result_dict, declared_reason, elapsed_ms)
            else:
                if declared_status:
                    logger.warning(
                        "task_status_override_unsupported",
                        task_id=task_id,
                        task_type=task_type,
                        declared_status=declared_status,
                    )
                await self._finish_success(task_id, result_dict, elapsed_ms)
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            await self._finish_failed(task_id, task_type, trace_id, exc, elapsed_ms)

    async def _load_payload(self, task_id: int) -> dict[str, Any]:
        """读取任务 payload。"""
        factory = get_session_factory()
        async with factory() as session:
            record = await session.get(TaskRecord, task_id)
            return dict(record.payload_json or {}) if record else {}

    # ★ 终态写入的重试预算：4 次、退避 0.05→0.1→0.2s（累计 ~0.35s）。
    #   刻意压得很小：终态写入发生在任务的收尾路径上，
    #   重试太久会把"任务到底成没成"这个结论推迟到调用方超时之后，
    #   那就从"丢任务"退化成"结论迟到"，一样是静默失效。
    TERMINAL_WRITE_ATTEMPTS = 4
    TERMINAL_WRITE_BASE_DELAY_S = 0.05

    async def _commit_state(
        self,
        task_id: int,
        mutate: Callable[[TaskRecord], None],
        *,
        attempts: int = TERMINAL_WRITE_ATTEMPTS,
    ) -> bool:
        """★ 带退避地把状态写进 `task_record`；**瞬时 DB 错误重试，绝不丢任务**。

        ★★ 为什么每次重试都**重新读一遍记录**再重放变更 ★★
            `StaleDataError`（UPDATE 期望 1 行却匹配 0 行）说明这一行已被并发改动，
            当前 session 里的对象已经**失效**；在同一个 session 上再 commit 一次
            只会有同样的结果。必须换 session → 重读 → 重放。

        Returns:
            True 表示已提交；False 表示记录已不存在（被删除），调用方无需再兜底。
        """
        delay = float(self.TERMINAL_WRITE_BASE_DELAY_S)
        for attempt in range(1, int(attempts) + 1):
            factory = get_session_factory()
            try:
                async with factory() as session:
                    record = await session.get(TaskRecord, task_id)
                    if record is None:
                        logger.error("task_record_missing_on_finish", task_id=task_id)
                        return False
                    mutate(record)
                    await session.commit()
                return True
            except Exception as exc:  # noqa: BLE001  只在这里收口
                if attempt >= int(attempts) or not is_transient_db_error(exc):
                    raise
                logger.warning(
                    "task_state_write_retry",
                    task_id=task_id,
                    attempt=attempt,
                    error=f"{type(exc).__name__}: {exc}",
                )
                await asyncio.sleep(delay)
                delay *= 2
        return False

    async def _restore_to_pending(
        self, task_id: int, task_type: str, trace_id: str, error: BaseException
    ) -> None:
        """★ 终态实在写不进去时的**最后兜底**：把任务放回 `pending`，**不丢任务**。

        为什么是 pending 而不是 failed：我们**没能**把结论写进去，此时既不知道成功
        也不知道失败；标 failed 是在**伪造结论**。放回 pending 让看门狗 /
        恢复流程（`reclaim_stuck_tasks`）接手，它到点会按重试上限判死 —— 上限仍在，
        不会变成无限循环。

        只改仍是 `running` 的记录：若终态其实已写成功（只是提交回执丢了），
        把已完成的任务拽回 pending 会造成**重复执行**。
        """

        def _mutate(record: TaskRecord) -> None:
            if str(record.status) != TaskStatus.RUNNING.value:
                return
            record.status = TaskStatus.PENDING.value
            record.worker_id = None
            record.started_at = None

        try:
            await self._commit_state(task_id, _mutate)
        except Exception as exc:  # noqa: BLE001  兜底的兜底：只剩日志
            logger.error(
                "task_terminal_state_lost",
                task_id=task_id,
                task_type=task_type,
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        logger.error(
            "task_terminal_write_failed_restored_pending",
            task_id=task_id,
            task_type=task_type,
            trace_id=trace_id,
            error=f"{type(error).__name__}: {error}",
            hint="终态未能落库，任务已放回 pending 由看门狗接手（不是 failed，结论未知）",
        )

    async def _finish_success(self, task_id: int, result: dict[str, Any], elapsed_ms: int) -> None:
        """标记成功。"""

        def _mutate(record: TaskRecord) -> None:
            record.mark_finished(result)
            record.duration_ms = record.duration_ms or elapsed_ms

        await self._commit_state(task_id, _mutate)
        logger.info("task_success", task_id=task_id, elapsed_ms=elapsed_ms)

    async def _finish_declared_failed(
        self, task_id: int, result: dict[str, Any], reason: str, elapsed_ms: int
    ) -> None:
        """★ 按处理器声明的「失败结论」落终态（**保留** `result_json` 里的失败明细）。

        ★★ 为什么要单独写一个方法，而不是复用 `_finish_failed` ★★
            `_finish_failed` 走的是「异常」路径：会按 `is_retryable_error` 判断重试。
            而"采集全部失败"是**确定性结论**（凭证没配/商品不存在，重试一万次也一样），
            重试只会让 status 在 pending/running 之间横跳、把结论推迟。
            这里：标 failed + 写全量 result_json + **不重试**。
        """
        def _mutate(record: TaskRecord) -> None:
            record.mark_failed(
                error_code="TASK_RESULT_FAILED",
                error_message=reason or "任务执行完成但结论为失败（详见 result_json）",
            )
            record.result_json = dict(result or {})
            record.duration_ms = record.duration_ms or elapsed_ms

        await self._commit_state(task_id, _mutate)
        logger.warning(
            "task_declared_failed",
            task_id=task_id,
            reason=reason,
            elapsed_ms=elapsed_ms,
        )

    async def _finish_failed(
        self, task_id: int, task_type: str, trace_id: str, error: Exception, elapsed_ms: int
    ) -> None:
        """标记失败；未超过重试上限则重置为 pending 并重新入队。

        ★★ 终态写不进去时**绝不放弃**（这是本次修复的核心）★★
            以前 `session.commit()` 抛 `database is locked` 就直接穿出，
            任务被永久留在 `running` —— 前端和测试都读不到终态。
            现在：瞬时错误退避重试；实在写不进去则放回 `pending` 交给看门狗，
            **不伪造 failed**（结论未知就是未知）。
        """
        snapshot: dict[str, Any] = {"should_retry": False, "retry_count": 0, "max_retry": 0}

        def _mutate(record: TaskRecord) -> None:
            record.mark_failed(error_code=type(error).__name__, error_message=str(error))
            record.duration_ms = record.duration_ms or elapsed_ms
            if record.can_retry and is_retryable_error(error):
                record.reset_for_retry()
                snapshot["should_retry"] = True
            snapshot["retry_count"] = int(record.retry_count or 0)
            snapshot["max_retry"] = int(record.max_retry or 0)

        try:
            written = await self._commit_state(task_id, _mutate)
        except Exception as exc:  # noqa: BLE001  终态写失败也必须走兜底，不能穿出
            logger.exception(
                "task_finish_failed_write_error", task_id=task_id, error=f"{type(exc).__name__}: {exc}"
            )
            await self._restore_to_pending(task_id, task_type, trace_id, error)
            return

        if not written:
            logger.error("task_record_missing_on_failed", task_id=task_id, task_type=task_type)
            return

        logger.warning(
            "task_failed",
            task_id=task_id,
            error=str(error),
            retry_count=snapshot["retry_count"],
            max_retry=snapshot["max_retry"],
            will_retry=snapshot["should_retry"],
            deterministic=not is_retryable_error(error),
        )
        if snapshot["should_retry"] and not self._closed:
            self._enqueue(task_id, task_type, trace_id)

    # ---------------- 控制 ----------------

    def register_handler(self, task_type: str, handler: TaskHandler) -> None:
        """注册处理器（写入模块级注册表，与 `@task_handler` 装饰器等价）。"""
        TASK_HANDLER_REGISTRY[task_type] = handler

    async def cancel(self, task_id: int) -> bool:
        """取消任务。"""
        factory = get_session_factory()
        async with factory() as session:
            record = await session.get(TaskRecord, task_id)
            if record is None:
                return False
            if record.status not in ACTIVE_TASK_STATUSES:
                return False
            record.mark_cancelled()
            await session.commit()
        with self._lock:
            self._futures.pop(task_id, None)
        logger.info("task_cancelled", task_id=task_id)
        return True

    async def retry(self, task_id: int) -> bool:
        """重试任务：重置为 pending 并重新入队。"""
        factory = get_session_factory()
        async with factory() as session:
            record = await session.get(TaskRecord, task_id)
            if record is None:
                return False
            if record.status not in {TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}:
                return False
            if not record.can_retry:
                return False
            record.reset_for_retry()
            record.status = TaskStatus.PENDING.value
            await session.commit()
            task_type = str(record.task_type)
            trace_id = str(record.trace_id or "")

        if not self._closed:
            self._enqueue(task_id, task_type, trace_id)
        logger.info("task_retried", task_id=task_id)
        return True

    async def get_status(self, task_id: int) -> dict[str, Any]:
        """查询任务状态。"""
        factory = get_session_factory()
        async with factory() as session:
            record = await session.get(TaskRecord, task_id)
            if record is None:
                return {"id": task_id, "status": "not_found"}
            return {
                "id": record.id,
                "task_type": record.task_type,
                "task_key": record.task_key,
                "status": record.status,
                "priority": record.priority,
                "retry_count": record.retry_count,
                "max_retry": record.max_retry,
                "duration_ms": record.duration_ms,
                "error_code": record.error_code,
                "error_message": record.error_message,
                "worker_id": record.worker_id,
                "trace_id": record.trace_id,
                "scheduled_at": record.scheduled_at.isoformat() + "Z" if record.scheduled_at else None,
                "started_at": record.started_at.isoformat() + "Z" if record.started_at else None,
                "finished_at": record.finished_at.isoformat() + "Z" if record.finished_at else None,
                "result": record.result_json,
            }

    def shutdown(self, wait: bool = False) -> None:
        """关闭线程池。"""
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=wait)
        logger.info("task_runner_shutdown", wait=wait)

    def pending_count(self) -> int:
        """在途任务数（内存视图）。"""
        with self._lock:
            return len(self._futures)

    def _enqueue(self, task_id: int, task_type: str, trace_id: str) -> None:
        """★ 重启恢复钩子：把已存在的 TaskRecord 直接投递到线程池（不新建记录）。"""
        if self._closed:
            logger.warning("task_enqueue_skipped_runner_closed", task_id=task_id)
            return
        accepted = self._executor.submit(lambda: self._run_in_thread(task_id, task_type, trace_id))
        logger.info(
            "task_enqueued", task_id=task_id, task_type=task_type, trace_id=trace_id, accepted=accepted
        )


# ---------------------------------------------------------------------------
#  全局单例
# ---------------------------------------------------------------------------

_runner: TaskRunner | None = None
_runner_lock = threading.Lock()


def get_task_runner(settings: Settings | None = None) -> TaskRunner:
    """获取全局 TaskRunner 单例（惰性创建）。"""
    global _runner
    with _runner_lock:
        if _runner is None:
            _runner = LocalTaskRunner(settings)
        return _runner


def set_task_runner(runner: TaskRunner) -> None:
    """替换全局 TaskRunner（测试 / 未来切 Celery 时使用）。"""
    global _runner
    with _runner_lock:
        _runner = runner


def shutdown_task_runner(wait: bool = False) -> None:
    """关闭全局 TaskRunner。"""
    global _runner
    with _runner_lock:
        if _runner is not None:
            _runner.shutdown(wait=wait)
            _runner = None
