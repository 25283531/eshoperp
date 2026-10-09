"""★ TaskRunner 卫生性回归（2026-10-08 由真实 HTTP 端到端用例暴露）。

================================================================================
★ 这三个用例不是"补覆盖率"，是补**上一轮没测到的失效模式**
================================================================================
前几轮的教训：单元测试证明「函数和 SQL 是对的」，而缺陷发生在**请求之间 / 线程之间**。
本文件覆盖的三个失效模式，全部是**先被跑出来的现象**抓到、再回头补的断言：

    ① 确定性业务错误被反复重试（空 identifiers 的采集任务被重试 3 次，
       status 在 pending/running 之间横跳，结论被推迟且中途读不到稳定终态）；
    ② 工作线程**非 daemon** ⇒ 只要有一个长任务（ai_rework 最长等 1800s）在跑，
       解释器退出时被 `concurrent.futures.thread._python_exit` join ⇒
       **pytest 用例全绿却卡在退出阶段永不返回**（faulthandler 抓栈才看清）；
    ③ 准备阶段崩溃（实测撞过 SQLite 偶发 "attempt to write a readonly database"）
       ⇒ 异常穿出 `asyncio.run()` ⇒ 任务**永久停在 running**，没有任何终态。

三条都是"静默失效"：不报错、不失败，只是结论永远不来。所以断言刻意只看
**可观察的终态与线程属性**，不看内部实现细节。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from app.core.database import get_session_factory
from app.core.errors import BusinessError
from app.models.enums import TaskType
from app.models.task import TaskRecord
from app.tasks.runner import LocalTaskRunner, is_retryable_error

pytestmark = pytest.mark.asyncio

TERMINAL = frozenset({"success", "failed", "cancelled"})


async def _wait_terminal(runner: LocalTaskRunner, task_id: int, *, timeout_s: float = 10.0) -> dict[str, Any]:
    """轮询到终态；超时返回**当前**状态（调用方自行断言，绝不假装成功）。"""
    deadline = asyncio.get_event_loop().time() + timeout_s
    status: dict[str, Any] = {}
    while True:
        status = await runner.get_status(task_id)
        if str(status.get("status")) in TERMINAL:
            return status
        if asyncio.get_event_loop().time() >= deadline:
            return status
        await asyncio.sleep(0.05)


async def _delete_task(task_id: int) -> None:
    """清理：用例写的 `task_record` 是**已提交**的，必须显式删。"""
    from sqlalchemy import delete

    factory = get_session_factory()
    async with factory() as session:
        await session.execute(delete(TaskRecord).where(TaskRecord.id == task_id))
        await session.commit()


# ======================================================================
#  ① 确定性业务错误不得重试
# ======================================================================
async def test_deterministic_business_error_is_not_retried() -> None:
    """★ `BusinessError` 重试一万次结论一样 —— 重试只会推迟结论并制造噪声。

    实测：空 identifiers 的采集任务被重试 3 次，`task_record` 刷出 3 条 warn，
    却没有任何一条是最终结论；期间 status 在 pending / running 之间横跳。
    """

    async def _boom(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
        raise BusinessError("identifiers 不能为空")

    runner = LocalTaskRunner(max_workers=1)
    runner.register_handler("probe_deterministic", _boom)
    try:
        task_id = await runner.submit("probe_deterministic", {}, max_retry=3)
        status = await _wait_terminal(runner, task_id)
    finally:
        runner.shutdown(wait=False)

    assert str(status.get("status")) == "failed", f"确定性业务失败必须有终态：{status}"
    assert int(status.get("retry_count") or 0) == 0, (
        f"确定性错误不应重试（重试只会推迟结论），实际 retry_count={status.get('retry_count')}"
    )
    await _delete_task(task_id)


async def test_transient_error_is_still_retried() -> None:
    """★ 反向对照：可能自愈的错误（网络/DB 抖动）**仍然要重试**。

    只写"不重试"的用例是不够的 —— 一个把所有异常都判成"不可重试"的实现
    同样能让上一条用例变绿。这条钉住**另一半**语义。
    """

    async def _flaky(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
        raise RuntimeError("connection reset by peer")

    runner = LocalTaskRunner(max_workers=1)
    runner.register_handler("probe_transient", _flaky)
    try:
        task_id = await runner.submit("probe_transient", {}, max_retry=1)
        status = await _wait_terminal(runner, task_id, timeout_s=15.0)
    finally:
        runner.shutdown(wait=False)

    assert int(status.get("retry_count") or 0) >= 1, (
        f"可自愈错误必须重试，实际 {status}"
    )
    assert str(status.get("status")) == "failed"
    await _delete_task(task_id)


async def test_is_retryable_error_classifies_by_nature() -> None:
    """★ 判据是「异常的性质」，不是「异常的文本」。"""
    assert is_retryable_error(BusinessError("参数为空")) is False
    assert is_retryable_error(RuntimeError("database is locked")) is True
    assert is_retryable_error(TimeoutError()) is True


# ======================================================================
#  ② 工作线程必须是 daemon：进程永远不能被一个长任务吊住
# ======================================================================
async def test_task_worker_threads_are_daemon() -> None:
    """★ 卡住的任务不许吊死进程退出（pytest 卡在退出阶段的根因）。

    断言的是**线程属性**而不是"能不能跑完任务"：
    只要有一个非 daemon 的工作线程在等 `ai_poll_timeout_sec`（默认 1800s），
    解释器退出时就会被 join 到天荒地老。状态安全由 `task_record` + 看门狗保证，
    不靠"退不出去"。
    """

    async def _sleep_forever(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
        await asyncio.sleep(3600)
        return {"ok": True}

    runner = LocalTaskRunner(max_workers=1)
    runner.register_handler("probe_daemon", _sleep_forever)
    try:
        task_id = await runner.submit("probe_daemon", {})
        workers: list[threading.Thread] = []
        for _ in range(200):
            workers = [t for t in threading.enumerate() if t.name.startswith("erp-task")]
            if workers:
                break
            await asyncio.sleep(0.05)
        assert workers, "任务已提交却没有工作线程被创建 —— 线程池没接上"
        assert all(t.daemon for t in workers), (
            f"工作线程必须是 daemon，否则长任务会让进程/测试永不退出："
            f"{[(t.name, t.daemon) for t in workers]}"
        )
    finally:
        runner.shutdown(wait=False)

    await _delete_task(task_id)


# ======================================================================
#  ③ 准备阶段崩溃也必须落到终态（不许永久 running）
# ======================================================================
async def test_execute_crash_still_reaches_terminal_state() -> None:
    """★ 崩溃必须被收口在事件循环**内部**，任务必须落到终态。

    实测事故：准备阶段（读 TaskRecord / mark_running / commit）撞上 SQLite
    偶发 `attempt to write a readonly database` ⇒ 异常穿出 `asyncio.run()`：
      * `asyncio.run()` 收尾 `_cancel_all_tasks()` 把 `AsyncSession.close()`
        这类收尾协程取消 ⇒ aiosqlite 连接线程（非 daemon）永远等不到停止信号；
      * TaskRecord **永久停在 running**，前端和测试都读不到终态 ——
        这正是"任务框架没接上"的典型静默失效。
    """
    factory = get_session_factory()
    async with factory() as session:
        record = TaskRecord(
            task_type=TaskType.SOURCE_COLLECT.value,
            payload_json={},
            status="pending",
            priority=5,
            max_retry=0,
            trace_id="probe-crash",
        )
        session.add(record)
        await session.commit()
        task_id = int(record.id)

    runner = LocalTaskRunner(max_workers=1)

    async def _crash(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("attempt to write a readonly database")

    runner._execute_inner = _crash  # type: ignore[method-assign]  ★ 模拟准备阶段崩溃
    try:
        await runner._execute(task_id, TaskType.SOURCE_COLLECT.value, "probe-crash", "worker-probe")
    finally:
        runner.shutdown(wait=False)

    status = await runner.get_status(task_id)
    assert str(status.get("status")) == "failed", (
        f"崩溃也必须有终态，否则任务永久 running、无人再碰：{status}"
    )
    # ★ 只断言「有可读原因」，**不**断言原因文本等于最初那句。
    #   实测过一次真实翻车：`_finish_failed()` 自己落终态时也会撞上同一个
    #   SQLite 偶发 readonly，外层兜底会再落一次，error_message 于是变成
    #   第二次失败的文本。**这恰恰是兜底生效的证据**（否则任务就永久 running 了），
    #   断言文本只会把"兜底成功"误判成失败 —— 那是最糟的一类断言。
    assert str(status.get("error_message") or "").strip(), (
        f"终态必须带上可读原因（空原因 = 又是一次静默失效），实际 {status.get('error_message')}"
    )
    assert str(status.get("error_code") or "").strip(), f"终态必须带上错误类型：{status}"
    await _delete_task(task_id)
