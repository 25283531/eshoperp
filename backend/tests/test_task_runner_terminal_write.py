"""★ 任务终态写入的健壮性回归（2026-10-08 由一条 flaky 用例暴露）。

================================================================================
★ 为什么单独开一个文件
================================================================================
`tests/test_task_runner_hygiene.py` 里有一条用例约 1/4 概率随机红
（`retry_count=1` / `database is locked`）。根因不在那条用例的断言，而在
**终态写入本身不抗抖动**：`session.commit()` 一旦撞上 SQLite 的写锁或
`StaleDataError`，异常就直接穿出 ⇒ 任务被**永久留在 running** ⇒
"受理了却永远没有结论"（本项目已经栽过一次的静默失效）。

修复两件事，本文件把这两件事**钉死**，避免以后有人把重试删掉：

    ① 瞬时 DB 错误（写锁 / 行被并发改动）→ 退避重试后照常落终态；
    ② 终态实在写不进去 → 放回 `pending` 交给看门狗，**绝不伪造 failed**
       （结论未知就是未知，标 failed 是在编造事实）。

用例刻意**直接驱动** `_finish_failed`：抖动是基础设施层的事，
走完整 submit→执行链路会让用例自己变成 flaky，反而盖住要守的语义。
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from app.core.database import get_session_factory
from app.core.errors import BusinessError
from app.models.enums import TaskStatus
from app.models.task import TaskRecord
from app.tasks.runner import LocalTaskRunner, is_transient_db_error

pytestmark = pytest.mark.asyncio


def _locked_error() -> OperationalError:
    """构造一个与真实 SQLite 写锁**同形**的异常（SQLAlchemy 包 sqlite3 驱动异常）。"""
    return OperationalError(
        "UPDATE task_record SET status=? WHERE id=?",
        {},
        sqlite3.OperationalError("database is locked"),
    )


class _FailBudget:
    """跨会话共享的失败预算。

    ★ 为什么必须是**共享**的：`_commit_state` 每次重试都会新建一个 session，
    若计数器挂在 session 实例上，则"第 1 次提交失败"会被**每一次重试**各触发一次
    ⇒ 所有尝试全失败，测不出"重试后成功"这条语义（我自己先踩了这个坑）。
    """

    def __init__(self, times: int) -> None:
        self.remaining = int(times)

    def should_fail(self) -> bool:
        if self.remaining > 0:
            self.remaining -= 1
            return True
        return False


class _FlakySession:
    """会话代理：按**共享预算**在 `commit()` 时抛写锁异常，之后恢复正常。"""

    def __init__(self, real: Any, budget: _FailBudget) -> None:
        self._real = real
        self._budget = budget
        self.commit_calls = 0

    async def __aenter__(self) -> "_FlakySession":
        await self._real.__aenter__()
        return self

    async def __aexit__(self, *exc_info: Any) -> Any:
        return await self._real.__aexit__(*exc_info)

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        return await self._real.get(*args, **kwargs)

    async def commit(self) -> None:
        self.commit_calls += 1
        if self._budget.should_fail():
            raise _locked_error()
        await self._real.commit()


async def _seed_running_task(task_type: str = "probe_terminal_write") -> int:
    """建一条 `running` 状态的任务记录，返回其 id。"""
    factory = get_session_factory()
    async with factory() as session:
        record = TaskRecord(task_type=task_type, status=TaskStatus.RUNNING.value, payload_json={})
        session.add(record)
        await session.commit()
        return int(record.id)


async def _read_status(task_id: int) -> dict[str, Any]:
    factory = get_session_factory()
    async with factory() as session:
        record = await session.get(TaskRecord, task_id)
        if record is None:
            return {"status": "not_found"}
        return {"status": str(record.status), "retry_count": int(record.retry_count or 0)}


async def _delete_task(task_id: int) -> None:
    from sqlalchemy import delete

    factory = get_session_factory()
    async with factory() as session:
        await session.execute(delete(TaskRecord).where(TaskRecord.id == task_id))
        await session.commit()


# ======================================================================
#  ① 判定函数本身：分类必须准，否则重试逻辑无从谈起
# ======================================================================
def test_transient_db_error_classification() -> None:
    """写锁类异常必须判为可自愈；业务错误**不能**被误判成可自愈。"""
    assert is_transient_db_error(_locked_error()) is True
    assert is_transient_db_error(sqlite3.OperationalError("attempt to write a readonly database")) is True
    # StaleDataError 的文本特征：行被并发改动 / 已删除
    assert is_transient_db_error(RuntimeError("UPDATE statement on table 'task_record' expected to update 1 row(s); 0 were matched.")) is True

    # ★ 反向对照：业务错误是**确定性**的，绝不能因为"反正会重试"就混进来
    assert is_transient_db_error(BusinessError("identifiers 不能为空")) is False
    assert is_transient_db_error(ValueError("价格不能为负")) is False


# ======================================================================
#  ② 撞写锁 → 重试后照常落终态（绝不留在 running）
# ======================================================================
async def test_terminal_state_survives_transient_write_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 终态写入第一次撞 `database is locked` ⇒ 退避重试 ⇒ 仍然落到 `failed`。

    修复前：异常直接穿出，任务永久停在 `running`，
    而"永远 running"在所有看板上都显示为"进行中"，是最难被发现的失效。
    """
    task_id = await _seed_running_task()
    runner = LocalTaskRunner(max_workers=1)
    real_factory = get_session_factory()
    try:
        # ★ 两个坑，都是"mock 没接上真实调用约定"导致的假失败：
        #   ① 预算必须建在**工厂外面**共享：若每次调用都新建预算，每次重试都会被
        #      重新失败一次 ⇒ 永远走不到"退避重试后成功"这条语义（见 `_FailBudget` docstring）。
        #   ② `runner._commit_state` 的约定是 `factory = get_session_factory()` 再 `factory()`
        #      —— **两层**。所以 patch 进去的必须是一个"返回工厂的工厂"；
        #      若直接返回 `_FlakySession` 实例，第二次调用会抛
        #      `TypeError: '_FlakySession' object is not callable`，异常在第一次就穿出，
        #      重试逻辑压根没被执行到（症状：任务卡在 running，看起来像"兜底失效"）。
        budget = _FailBudget(1)

        def _flaky_factory() -> Any:
            return _FlakySession(real_factory(), budget)

        monkeypatch.setattr(
            "app.tasks.runner.get_session_factory",
            lambda: _flaky_factory,
        )
        # 确定性业务错误：重试只应发生在**写终态**这一次提交上，
        # 绝不能变成"业务重跑"（否则 retry_count 会 +1，正是那条 flaky 用例的症状）
        await runner._finish_failed(task_id, "probe_terminal_write", "trace-x", BusinessError("boom"), 1)
    finally:
        monkeypatch.undo()
        runner.shutdown(wait=False)

    state = await _read_status(task_id)
    await _delete_task(task_id)

    assert state["status"] == TaskStatus.FAILED.value, f"撞写锁后仍必须落终态，实际 {state}"
    assert state["retry_count"] == 0, (
        f"写终态的重试不得计入业务重试（确定性错误重试只会推迟结论），实际 {state}"
    )


# ======================================================================
#  ③ 终态始终写不进去 → 放回 pending，不伪造 failed
# ======================================================================
async def test_terminal_state_lost_falls_back_to_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 退避重试全部失败 ⇒ 放回 `pending` 由看门狗接手，**不得**标成 failed。

    为什么必须是 pending 而不是 failed：我们**没能**把结论写进去，
    此刻既不知道成功也不知道失败；标 failed 是在伪造结论。
    放回 pending 后，`reclaim_stuck_tasks` 到点会按重试上限判死 —— 上限仍在，
    不会退化成无限循环。
    """
    task_id = await _seed_running_task()
    runner = LocalTaskRunner(max_workers=1)
    real_factory = get_session_factory()
    attempts = int(LocalTaskRunner.TERMINAL_WRITE_ATTEMPTS)
    try:
        # 同上两个坑：预算共享 + 工厂必须是两层的（见上面用例里的说明）。
        budget = _FailBudget(attempts)

        def _flaky_factory() -> Any:
            return _FlakySession(real_factory(), budget)

        monkeypatch.setattr(
            "app.tasks.runner.get_session_factory",
            lambda: _flaky_factory,
        )
        await runner._finish_failed(task_id, "probe_terminal_write", "trace-y", BusinessError("boom"), 1)
    finally:
        monkeypatch.undo()
        runner.shutdown(wait=False)

    state = await _read_status(task_id)
    await _delete_task(task_id)

    assert state["status"] == TaskStatus.PENDING.value, (
        f"终态写不进去时必须放回 pending 交给看门狗（不丢任务、不伪造结论），实际 {state}"
    )
