"""★ 探针：定时任务「相位错开 + 提交重试」的**真机取证**（2026-10-08 缺陷）。

================================================================================
为什么要有这个探针：单元测试证明的是"我对着假运行器断言过"，
而这里要证明的是**真实 SQLite + 真实 APScheduler + 真实 TaskRunner** 下的行为。
================================================================================

缺陷（线上日志实证）：
    2026-10-08 23:44:49 ERROR [app.tasks.scheduler] scheduled_task_submit_failed
      error='(sqlite3.OperationalError) attempt to write a readonly database
             [SQL: INSERT INTO task_record (...)]'
      task_type=purchase_place
同一秒 `order_sync` 提交成功、`purchase_place` 失败 ⇒ 已匹配订单的自动下单被**整轮跳过**。

本探针分三段取证（各自独立、可单独运行）：

    A. 相位：真实构建 APScheduler，对比「修复前（偏移全 0）」与「修复后」的
       首次触发时间 —— 证明两任务确实不再同秒；
    B. 重试：把真实 SQLite 文件置为**只读**（真实产生
       `attempt to write a readonly database`），验证提交会重试并在可写后成功；
    C. 重试耗尽：持续只读 ⇒ 验证放弃时留下 critical 级告警 + 台账 + /health 可见。

★ 全程使用**临时目录**里的独立 SQLite 文件，绝不触碰 `data/erp.db`。

运行：
    python qa_probes/probe_scheduler_phase_offset.py
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# ★★ 必须在 import app.* 之前设置（pydantic-settings 导入即读取）★★
_TMP_DIR = Path(tempfile.mkdtemp(prefix="erp_sched_probe_"))
_DB_PATH = _TMP_DIR / "probe_scheduler.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_PATH.as_posix()}"
os.environ["APP_ENV"] = "test"
os.environ["LOG_JSON"] = "false"

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.database import get_engine  # noqa: E402
from app.models import Base  # noqa: E402
from app.models.enums import TaskType  # noqa: E402
from app.tasks import scheduler as sched  # noqa: E402
from app.tasks.runner import LocalTaskRunner  # noqa: E402

ORDER_SYNC = TaskType.ORDER_SYNC.value
PURCHASE_PLACE = TaskType.PURCHASE_PLACE.value

_RESULTS: list[tuple[str, bool, str]] = []


def _check(name: str, ok: bool, detail: str = "") -> bool:
    """记录一条取证结论。"""
    _RESULTS.append((name, bool(ok), detail))
    print(f"    [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    return bool(ok)


def _prepare_db() -> None:
    """建临时库与 `task_record` 表（只建本探针需要的表结构，全量建表更省事）。"""

    async def _create() -> None:
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with engine.connect() as conn:
            mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
            print(f"    journal_mode = {mode}")

    asyncio.run(_create())


def _reset_task_records() -> None:
    """清空 `task_record`。

    ★ 必须清：定时提交用 `task_key="purchase_place:scheduled"` 做幂等，
      上一轮留下的 `pending` 行会让下一次提交直接命中幂等分支（**根本不会 INSERT**），
      于是"只读故障"根本不会被触发，取证结论会假绿。
    """

    async def _delete() -> None:
        from sqlalchemy import delete

        from app.core.database import get_session_factory
        from app.models.task import TaskRecord

        factory = get_session_factory()
        async with factory() as session:
            await session.execute(delete(TaskRecord))
            await session.commit()

    asyncio.run(_delete())


# ======================================================================
#  A. 相位错开（真实 APScheduler）
# ======================================================================
def probe_a_phase_offset() -> None:
    """A 段：修复前（偏移全 0）vs 修复后，两个 5min 任务的触发时间对比。"""
    print("\n=== A. 相位错开（真实 APScheduler 构建）===")
    runner = LocalTaskRunner(max_workers=2)

    def _first_fires(force_zero_offset: bool) -> dict[str, datetime]:
        """构建 + 启动调度器，返回各任务首次触发时间。"""
        original = [dict(job) for job in sched.SCHEDULED_JOBS]
        try:
            if force_zero_offset:
                for job in sched.SCHEDULED_JOBS:
                    job["phase_offset_sec"] = 0
            built = sched.build_scheduler(runner=runner)
            built.start()
            try:
                return {job.id: job.next_run_time for job in built.get_jobs() if job.next_run_time}
            finally:
                built.shutdown(wait=False)
        finally:
            sched.SCHEDULED_JOBS[:] = original

    before = _first_fires(force_zero_offset=True)
    after = _first_fires(force_zero_offset=False)

    print("    —— 修复前（phase_offset_sec 全部为 0，即线上原状）——")
    for job_id in sorted(before):
        print(f"       {job_id:<16} {before[job_id].isoformat()}")
    print("    —— 修复后 ——")
    for job_id in sorted(after):
        print(f"       {job_id:<16} {after[job_id].isoformat()}")

    if ORDER_SYNC in before and PURCHASE_PLACE in before:
        gap_before = abs((before[PURCHASE_PLACE] - before[ORDER_SYNC]).total_seconds())
        _check(
            "修复前：两个 5min 任务确实同秒触发（复现缺陷前提）",
            gap_before < 1.0,
            f"gap={gap_before:.3f}s",
        )
    if ORDER_SYNC in after and PURCHASE_PLACE in after:
        gap_after = abs((after[PURCHASE_PLACE] - after[ORDER_SYNC]).total_seconds())
        _check(
            "修复后：两个 5min 任务不再同秒触发",
            gap_after >= 10.0,
            f"gap={gap_after:.3f}s",
        )

    # ★ 整个触发序列都要错开，而不只是首次
    built = sched.build_scheduler(runner=runner)
    built.start()
    try:
        jobs = {job.id: job for job in built.get_jobs()}

        def _sequence(job_id: str, count: int = 5) -> list[datetime]:
            trigger = jobs[job_id].trigger
            previous = None
            now = datetime.now(trigger.timezone)
            times: list[datetime] = []
            for _ in range(count):
                nxt = trigger.get_next_fire_time(previous, now)
                assert nxt is not None
                times.append(nxt)
                previous = nxt
            return times

        order_seq = _sequence(ORDER_SYNC)
        purchase_seq = _sequence(PURCHASE_PLACE)
        worst = min(abs((p - o).total_seconds()) for o, p in zip(order_seq, purchase_seq))
        _check("修复后：连续 5 轮触发时间全部错开", worst >= 10.0, f"最小间隔={worst:.3f}s")
        print(f"       order_sync     : {[t.strftime('%H:%M:%S') for t in order_seq]}")
        print(f"       purchase_place : {[t.strftime('%H:%M:%S') for t in purchase_seq]}")
    finally:
        built.shutdown(wait=False)

    # ★ A2：走**生产入口** `start_scheduler()`（而不是直接 build），
    #   确认真实启动路径下 `list_jobs()` 报出的触发时间同样已错开。
    print("    —— A2：生产入口 start_scheduler() + list_jobs() ——")
    started = sched.start_scheduler(runner=runner)
    try:
        jobs_report = sched.list_jobs()
        for item in jobs_report:
            print(f"       {item['id']:<16} next={item['next_run_time']}  trigger={item['trigger']}")
        times = {
            item["id"]: datetime.fromisoformat(item["next_run_time"])
            for item in jobs_report
            if item.get("next_run_time")
        }
        if ORDER_SYNC in times and PURCHASE_PLACE in times:
            gap = abs((times[PURCHASE_PLACE] - times[ORDER_SYNC]).total_seconds())
            _check("生产入口下两任务触发时间错开", gap >= 10.0, f"gap={gap:.3f}s")
        _check("生产入口确实启动了调度器", started is not None and sched.is_running())
    finally:
        sched.stop_scheduler(wait=False)

    runner.shutdown(wait=False)


# ======================================================================
#  B. 真实 SQLite 写失败 → 重试 → 成功
# ======================================================================
def _set_writable(writable: bool) -> None:
    """切换临时库（含 -wal / -shm）的可写属性 —— 真实制造 SQLite 只读错误。"""
    mode = stat.S_IWRITE | stat.S_IREAD if writable else stat.S_IREAD
    for path in (_DB_PATH, _DB_PATH.with_suffix(".db-wal"), _DB_PATH.with_suffix(".db-shm")):
        if path.exists():
            os.chmod(path, mode)


def probe_b_retry_on_real_sqlite_failure() -> None:
    """B 段：真实 `attempt to write a readonly database` ⇒ 重试一次即成功。"""
    print("\n=== B. 真实 SQLite 只读错误 → 重试 → 成功 ===")
    runner = LocalTaskRunner(max_workers=2)
    sched.reset_submit_failure_stats()
    _reset_task_records()

    _set_writable(False)  # ★ 真·只读：sqlite3 会以只读方式打开，INSERT 必报 readonly
    restore_after = 0.4
    timer = threading.Timer(restore_after, lambda: _set_writable(True))
    timer.daemon = True
    timer.start()

    async def _run() -> Any:
        return await sched.submit_scheduled_task(
            PURCHASE_PLACE, runner, "probe", base_delay_sec=1.0, max_delay_sec=1.0
        )

    started = time.perf_counter()
    task_id = asyncio.run(_run())
    elapsed = time.perf_counter() - started
    timer.cancel()
    _set_writable(True)

    _check("提交最终成功（返回 TaskRecord.id）", task_id is not None, f"task_id={task_id}")
    _check("重试确实等待了（不是同一场冲突里连撞）", elapsed >= 1.0, f"耗时={elapsed:.2f}s")
    if task_id is not None:
        _check("成功后台账清零（不留假告警）", sched.get_scheduler_health()["unhealthy"] is False)
    runner.shutdown(wait=False)


# ======================================================================
#  C. 重试耗尽 ⇒ 必须留下可诊断痕迹
# ======================================================================
def probe_c_give_up_is_observable() -> None:
    """C 段：持续只读 ⇒ 放弃，但必须留下 critical 告警 + 台账。"""
    print("\n=== C. 重试耗尽：告警必须可见（不能只留一行 error）===")
    runner = LocalTaskRunner(max_workers=2)
    sched.reset_submit_failure_stats()
    _reset_task_records()

    recorder: list[tuple[str, str, dict[str, Any]]] = []

    class _SpyLogger:
        """替身日志器：只用来**记录级别**，不改变被测行为。"""

        def _record(self, level: str, event: str, **kwargs: Any) -> None:
            recorder.append((level, event, dict(kwargs)))

        def __getattr__(self, item: str) -> Any:
            def _sink(event: str, **kwargs: Any) -> None:
                self._record(item, event, **kwargs)

            return _sink

    original_logger = sched.logger
    sched.logger = _SpyLogger()  # type: ignore[assignment]
    _set_writable(False)
    try:

        async def _run() -> Any:
            return await sched.submit_scheduled_task(
                PURCHASE_PLACE, runner, "probe", base_delay_sec=0.2, max_delay_sec=0.2
            )

        result = asyncio.run(_run())
    finally:
        _set_writable(True)
        sched.logger = original_logger

    _check("全部重试失败时显式返回 None", result is None, f"result={result}")

    gave_up = [kw for level, event, kw in recorder if event == "scheduled_task_submit_gave_up"]
    levels = {level for level, event, _ in recorder if event == "scheduled_task_submit_gave_up"}
    _check("放弃时日志级别升级到 critical", "critical" in levels, f"实际级别={sorted(levels)}")
    if gave_up:
        kw = gave_up[0]
        _check(
            "告警带上 task_type / attempts / consecutive_failures",
            kw.get("task_type") == PURCHASE_PLACE
            and int(kw.get("attempts") or 0) == sched.SUBMIT_MAX_ATTEMPTS
            and int(kw.get("consecutive_failures") or 0) == 1,
            f"{ {k: kw.get(k) for k in ('task_type', 'attempts', 'consecutive_failures', 'error_type')} }",
        )
        _check(
            "失败原因是**真实**的 SQLite 只读错误",
            "readonly" in str(kw.get("error") or "").lower()
            or "readonly" in str(kw.get("error_type") or "").lower(),
            f"error={kw.get('error')!r} / error_type={kw.get('error_type')}",
        )

    health = sched.get_scheduler_health()
    entry = health["submit_failures"].get(PURCHASE_PLACE) or {}
    _check("台账记录了本轮被跳过", int(entry.get("consecutive_failures") or 0) == 1, str(entry))
    _check("/health 通道能查到（unhealthy=True）", health["unhealthy"] is True)
    print(f"    get_scheduler_health() = {health}")
    sched.reset_submit_failure_stats()
    runner.shutdown(wait=False)


def main() -> int:
    """执行全部取证段落，返回退出码（0 = 全部通过）。"""
    print(f"临时库：{_DB_PATH}（★ 与 data/erp.db 完全隔离）")
    settings = get_settings()
    print(f"DATABASE_URL = {settings.database_url}")
    _prepare_db()

    probe_a_phase_offset()
    probe_b_retry_on_real_sqlite_failure()
    probe_c_give_up_is_observable()

    failed = [name for name, ok, _ in _RESULTS if not ok]
    print("\n================ 取证结论 ================")
    for name, ok, detail in _RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    print(f"合计：{len(_RESULTS) - len(failed)} 通过 / {len(failed)} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
