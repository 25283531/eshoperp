"""★ 定时任务的**相位错开**与**提交重试**回归（2026-10-08 真实日志暴露的缺陷）。

================================================================================
★ 这两个用例钉住的是「静默失效」，不是"补覆盖率"
================================================================================
线上真实日志（同一秒、两个 interval 5min 的任务）：

    2026-10-08 23:44:49 ERROR [app.tasks.scheduler] scheduled_task_submit_failed
      error='(sqlite3.OperationalError) attempt to write a readonly database
             [SQL: INSERT INTO task_record (...)]'
      task_type=purchase_place

同一秒 `order_sync` 提交**成功**，`purchase_place` **失败**；5 分钟后（23:49:49）
purchase_place 才正常跑。也就是说：**已匹配订单的自动下单被整轮跳过**，
而界面上什么提示都没有 —— 因为失败时 `task_record` 一行都没写，
任务恢复（启动扫描）和看门狗（running 超时回收）都扫不到它。

因此本文件断言的是**可观察行为**，刻意不锁死实现细节：

    ① 两个 5min 任务的触发序列**不再重合**（且错开量足以避开写锁竞争，不是 1 秒级噪声）；
    ② 提交失败**会重试**，重试后才判定放弃；
    ③ 重试耗尽后必须留下**可诊断痕迹**（升级日志级别 + 台账 + `/health` 可见）——
       而不是"只记一行 error 就完事"。

★★ 断言纪律 ★★
    本项目曾因断言锁死异常文本，把"兜底生效"误判成失败（见
    `test_task_runner_hygiene.py::test_execute_crash_still_reaches_terminal_state`）。
    所以这里**不**断言异常字符串，只断言：异常类型名、尝试次数、台账字段、日志级别。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from app.models.enums import TaskType
from app.tasks import scheduler as sched

ORDER_SYNC = TaskType.ORDER_SYNC.value
PURCHASE_PLACE = TaskType.PURCHASE_PLACE.value

# ★ 「足以避开写锁竞争」的下限：1~2 秒的重合毫无意义（提交本身就要几十毫秒），
#   必须是一个肉眼可辨的相位差。上限则防止有人把偏移设成接近整个周期。
MIN_PHASE_GAP_SEC = 10.0
MAX_PHASE_GAP_SEC = 240.0


class _FlakySubmitRunner:
    """假运行器：前 `fail_times` 次提交抛瞬时错误，之后成功。"""

    def __init__(self, fail_times: int, task_id: int = 4242, error: Exception | None = None) -> None:
        """初始化。

        Args:
            fail_times: 连续失败次数（0 表示永不失败）。
            task_id: 成功后返回的 `TaskRecord.id`。
            error: 失败时抛出的异常；默认模拟 SQLite 写锁冲突。
        """
        self.fail_times = int(fail_times)
        self.task_id = int(task_id)
        self.error = error or RuntimeError("attempt to write a readonly database")
        self.calls: list[dict[str, Any]] = []

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
        """记录调用并按配置抛错 / 返回。"""
        self.calls.append({"task_type": task_type, "payload": dict(payload), "task_key": task_key})
        if len(self.calls) <= self.fail_times:
            raise self.error
        return self.task_id


class _RecordingLogger:
    """替身日志器：记录 (级别, 事件, 字段)，用于断言「告警确实升级」。"""

    def __init__(self) -> None:
        """初始化。"""
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def _record(self, level: str, event: str, **kwargs: Any) -> None:
        self.records.append((level, str(event), dict(kwargs)))

    def debug(self, event: str, **kwargs: Any) -> None:
        """记录 debug。"""
        self._record("debug", event, **kwargs)

    def info(self, event: str, **kwargs: Any) -> None:
        """记录 info。"""
        self._record("info", event, **kwargs)

    def warning(self, event: str, **kwargs: Any) -> None:
        """记录 warning。"""
        self._record("warning", event, **kwargs)

    def error(self, event: str, **kwargs: Any) -> None:
        """记录 error。"""
        self._record("error", event, **kwargs)

    def critical(self, event: str, **kwargs: Any) -> None:
        """记录 critical。"""
        self._record("critical", event, **kwargs)

    def exception(self, event: str, **kwargs: Any) -> None:
        """记录 exception（按 error 级处理）。"""
        self._record("error", event, **kwargs)

    def levels_for(self, event: str) -> list[str]:
        """返回某个事件名出现过的所有级别。"""
        return [level for level, evt, _ in self.records if evt == event]


@pytest.fixture(autouse=True)
def _clean_submit_failures() -> object:
    """★ 台账是**模块级**全局状态，用例之间必须隔离，否则互相污染。"""
    sched.reset_submit_failure_stats()
    yield
    sched.reset_submit_failure_stats()


# ======================================================================
#  ① 相位错开：同周期任务不得再撞在同一秒
# ======================================================================
def _build_running_scheduler() -> Any:
    """构建**并启动**一个调度器（用假运行器，不写库、不跑业务）。

    ★ 为什么必须 `start()`：APScheduler 未启动时 job 被放在 `_pending_jobs` 里，
      `get_jobs()` 直接抛 `SchedulerNotRunningError`，`job.next_run_time` 也未赋值。
      启动后取到的才是**真正会被执行**的触发时间；用例结束必须 shutdown（线程）。
    """
    built = sched.build_scheduler(runner=_FlakySubmitRunner(0))
    built.start()
    return built


def _first_fire_time(job: Any) -> Any:
    """取任务首次触发时间。

    ★ 不能读 `job.next_run_time`：调度器**未启动**时该 slot 未赋值，取值直接抛
      `AttributeError`（APScheduler 把未启动的 job 放进 `_pending_jobs`）。
      这里直接问触发器，构建期即可验证相位。
    """
    from datetime import datetime

    return job.trigger.get_next_fire_time(None, datetime.now(job.trigger.timezone))


def test_same_interval_jobs_do_not_share_first_fire_time() -> None:
    """★ `order_sync` 与 `purchase_place` 同为 5min，首次触发时间必须不同。"""
    built = _build_running_scheduler()
    try:
        jobs = {job.id: job for job in built.get_jobs()}
        assert ORDER_SYNC in jobs, f"缺少 {ORDER_SYNC}：{[j.id for j in built.get_jobs()]}"
        assert PURCHASE_PLACE in jobs, f"缺少 {PURCHASE_PLACE}：{[j.id for j in built.get_jobs()]}"

        first_order = _first_fire_time(jobs[ORDER_SYNC])
        first_purchase = _first_fire_time(jobs[PURCHASE_PLACE])
        assert first_order is not None and first_purchase is not None, "周期任务必须有首次触发时间"
        assert first_order != first_purchase, (
            f"两个 5min 任务仍在同秒触发，必然并发抢 SQLite 写锁："
            f"{ORDER_SYNC}={first_order} / {PURCHASE_PLACE}={first_purchase}"
        )
        gap = abs((first_purchase - first_order).total_seconds())
        assert MIN_PHASE_GAP_SEC <= gap <= MAX_PHASE_GAP_SEC, (
            f"相位差 {gap}s 不合理（应在 {MIN_PHASE_GAP_SEC}~{MAX_PHASE_GAP_SEC}s 之间）"
        )
    finally:
        built.shutdown(wait=False)


def test_phase_offset_holds_for_whole_fire_sequence() -> None:
    """★ 不只是首次：**整个触发序列**都不能重合（否则只是把碰撞推迟了一轮）。"""
    built = _build_running_scheduler()
    try:
        jobs = {job.id: job for job in built.get_jobs()}

        def _sequence(job_id: str, count: int = 6) -> list[Any]:
            job = jobs[job_id]
            previous = None
            now = _first_fire_time(job)
            times: list[Any] = []
            for _ in range(count):
                nxt = job.trigger.get_next_fire_time(previous, now)
                assert nxt is not None, f"{job_id} 的触发器排不出下一次触发时间"
                times.append(nxt)
                previous = nxt
            return times

        order_times = _sequence(ORDER_SYNC)
        purchase_times = _sequence(PURCHASE_PLACE)
        for left, right in zip(order_times, purchase_times):
            gap = abs((right - left).total_seconds())
            assert gap >= MIN_PHASE_GAP_SEC, (
                f"触发序列在第 {order_times.index(left) + 1} 轮重合/过近（gap={gap}s）："
                f"{left} vs {right}"
            )
    finally:
        built.shutdown(wait=False)


def test_inventory_sync_is_also_off_the_five_minute_grid() -> None:
    """★ 30min 的 `inventory_sync` 每隔 30 分钟就会落进 5min 网格 —— 同样要错开。"""
    built = _build_running_scheduler()
    try:
        jobs = {job.id: job for job in built.get_jobs()}
        inventory = jobs["inventory_sync"]
        order = jobs[ORDER_SYNC]
        previous = None
        now = _first_fire_time(order)
        for _ in range(6):
            nxt_order = order.trigger.get_next_fire_time(previous, now)
            nxt_inventory = inventory.trigger.get_next_fire_time(previous, now)
            gap = abs((nxt_inventory - nxt_order).total_seconds())
            assert gap >= MIN_PHASE_GAP_SEC, f"inventory_sync 与 order_sync 触发过近（gap={gap}s）"
            previous = nxt_order
            now = nxt_order
    finally:
        built.shutdown(wait=False)


# ======================================================================
#  ② 提交失败必须重试
# ======================================================================
async def test_submit_retries_transient_failure_then_succeeds() -> None:
    """★ 写锁冲突是瞬时的：重试一次几乎必成，不重试 = 白白丢一轮自动下单。"""
    runner = _FlakySubmitRunner(fail_times=2)
    task_id = await sched.submit_scheduled_task(
        PURCHASE_PLACE, runner, "purchase_place", base_delay_sec=0.0
    )
    assert task_id == 4242, f"重试后应当成功并返回 TaskRecord.id，实际 {task_id}"
    assert len(runner.calls) == 3, f"必须 1 次首次 + 2 次重试，实际调用 {len(runner.calls)} 次"
    # ★ 重试不得改变任务语义：payload / task_key 与修复前逐字一致
    assert runner.calls[-1]["payload"] == {"trigger": "purchase_place"}, (
        f"payload 被改动会污染处理器语义：{runner.calls[-1]['payload']}"
    )
    assert runner.calls[-1]["task_key"] == f"{PURCHASE_PLACE}:scheduled"
    assert sched.get_scheduler_health()["unhealthy"] is False, "成功之后不应留下告警台账"


async def test_submit_retry_waits_between_attempts() -> None:
    """★ 重试必须**带间隔**：立刻重试等于在同一场写锁冲突里连撞三次。"""
    runner = _FlakySubmitRunner(fail_times=2)
    started = time.perf_counter()
    task_id = await sched.submit_scheduled_task(
        ORDER_SYNC, runner, "order_sync", base_delay_sec=0.05, max_delay_sec=0.05
    )
    elapsed = time.perf_counter() - started
    assert task_id == 4242
    assert elapsed >= 0.05, f"重试之间没有等待（{elapsed:.3f}s），等于同一次冲突连撞三次"


async def test_submit_succeeds_first_try_without_extra_calls() -> None:
    """★ 反向对照：正常路径**只提交一次**，不许为了"显得稳"而白白多写库。"""
    runner = _FlakySubmitRunner(fail_times=0)
    task_id = await sched.submit_scheduled_task(ORDER_SYNC, runner, "order_sync", base_delay_sec=0.0)
    assert task_id == 4242
    assert len(runner.calls) == 1, f"正常路径只应提交一次，实际 {len(runner.calls)} 次"


# ======================================================================
#  ③ 重试耗尽：必须留下可诊断痕迹，而不是"一行 error 就完事"
# ======================================================================
async def test_submit_gives_up_after_max_attempts_and_records_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 全部尝试失败 ⇒ 返回 None、台账 +1、日志**升级到 critical**。"""
    recorder = _RecordingLogger()
    monkeypatch.setattr(sched, "logger", recorder)

    runner = _FlakySubmitRunner(fail_times=99)
    result = await sched.submit_scheduled_task(
        PURCHASE_PLACE, runner, "purchase_place", base_delay_sec=0.0
    )
    assert result is None, "提交最终失败时必须显式返回 None（调用方据此知道本轮没跑成）"
    assert len(runner.calls) == sched.SUBMIT_MAX_ATTEMPTS, (
        f"尝试次数应等于 SUBMIT_MAX_ATTEMPTS={sched.SUBMIT_MAX_ATTEMPTS}，实际 {len(runner.calls)}"
    )

    health = sched.get_scheduler_health()
    assert health["unhealthy"] is True, "提交失败后 /health 必须能查到（否则就是静默失效）"
    assert PURCHASE_PLACE in health["unhealthy_task_types"], health
    entry = health["submit_failures"][PURCHASE_PLACE]
    assert int(entry["consecutive_failures"]) == 1, f"连续失败计数异常：{entry}"
    assert int(entry["last_attempts"]) == sched.SUBMIT_MAX_ATTEMPTS
    # ★ 只断言**异常类型名**，绝不断言异常文本（本项目有过锁死文本的翻车）
    assert str(entry["last_error_type"]) == "RuntimeError", entry
    assert str(entry["last_error"]).strip(), "失败台账必须带上可读原因"

    # ★ 告警级别必须升级：早期实现只有一条 error，且不留任何可巡检状态
    levels = recorder.levels_for("scheduled_task_submit_gave_up")
    assert "critical" in levels, f"重试耗尽必须升级到 critical，实际级别 {levels}"
    gave_up = [kw for level, evt, kw in recorder.records if evt == "scheduled_task_submit_gave_up"][0]
    assert gave_up.get("task_type") == PURCHASE_PLACE
    assert int(gave_up.get("consecutive_failures") or 0) == 1
    assert int(gave_up.get("attempts") or 0) == sched.SUBMIT_MAX_ATTEMPTS


async def test_consecutive_failure_counter_accumulates_and_clears() -> None:
    """★ 连续失败次数必须**累加**（第 N 轮仍失败 = 连续 N 轮被跳过），成功即清零。"""
    for _ in range(3):
        await sched.submit_scheduled_task(
            PURCHASE_PLACE, _FlakySubmitRunner(fail_times=99), "purchase_place", base_delay_sec=0.0
        )
    entry = sched.get_scheduler_health()["submit_failures"][PURCHASE_PLACE]
    assert int(entry["consecutive_failures"]) == 3, f"连续 3 轮失败应累计为 3：{entry}"
    assert int(entry["total_failures"]) == 3, entry

    await sched.submit_scheduled_task(
        PURCHASE_PLACE, _FlakySubmitRunner(fail_times=0), "purchase_place", base_delay_sec=0.0
    )
    entry = sched.get_scheduler_health()["submit_failures"][PURCHASE_PLACE]
    assert int(entry["consecutive_failures"]) == 0, f"成功后连续失败次数必须清零：{entry}"
    assert int(entry["total_failures"]) == 3, "累计次数是历史事实，成功不该抹掉"
    assert sched.get_scheduler_health()["unhealthy"] is False


async def test_submit_retry_is_not_filtered_by_error_type() -> None:
    """★ 调度层重试的判据是「这一轮到底提交成功没有」，不是「错误类型是否可重试」。

    「错误是否值得重试」是**任务执行层**的语义（`is_retryable_error`：确定性业务错误不重试）。
    调度层面对的是"写库失败 ⇒ 根本没有 TaskRecord ⇒ 本轮彻底没跑"，
    无论异常是 `OperationalError` 还是别的，结论都一样：**这一轮被跳过了**。
    这里钉住两层职责不混淆（调度层不去解释任务语义，也不漏掉任意一种提交失败）。
    """
    from app.core.errors import BusinessError

    runner = _FlakySubmitRunner(fail_times=99, error=BusinessError("参数为空"))
    result = await sched.submit_scheduled_task(ORDER_SYNC, runner, "order_sync", base_delay_sec=0.0)
    # ★ 提交阶段抛什么都算"这一轮没提交成功"，调度层照常重试 —— 判据是"提交是否成功"，
    #   不是"错误是否可重试"（后者是任务执行层的语义）。因此这里只断言：重试到上限后放弃。
    assert result is None
    assert len(runner.calls) == sched.SUBMIT_MAX_ATTEMPTS
    assert sched.get_scheduler_health()["submit_failures"][ORDER_SYNC]["last_error_type"] == "BusinessError"


def test_job_wrapper_never_raises_into_scheduler_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 常规路径（APScheduler 工作线程）：job 函数绝不许把异常抛进调度线程。"""
    monkeypatch.setattr(sched, "logger", _RecordingLogger())
    runner = _FlakySubmitRunner(fail_times=99)
    sched._job_wrapper(PURCHASE_PLACE, runner, "purchase_place")  # 不抛异常即通过
    assert len(runner.calls) == sched.SUBMIT_MAX_ATTEMPTS


async def test_job_wrapper_fallback_when_loop_already_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 退化路径（调用方已有事件循环）也必须跑完且**不抛异常**。

    早期实现在这里 `new_event_loop().run_until_complete(...)`，Python 3.10+ 会直接抛
    "Cannot run the event loop while another loop is running" ⇒ 异常穿进 APScheduler
    ⇒ 任务被记成一次 job 失败后跳过，库里什么都不留 —— 又是一次静默失效。
    """
    monkeypatch.setattr(sched, "logger", _RecordingLogger())
    runner = _FlakySubmitRunner(fail_times=1)
    sched._job_wrapper(ORDER_SYNC, runner, "order_sync")  # 不抛异常即通过
    assert len(runner.calls) == 2, f"退化路径应完成 1 次失败 + 1 次重试：{len(runner.calls)}"


async def test_health_endpoint_surfaces_scheduler_failure(client: Any) -> None:
    """★ 复用既有 `/health` 通道：提交失败必须能被巡检看到（不新造告警系统）。"""
    await sched.submit_scheduled_task(
        PURCHASE_PLACE, _FlakySubmitRunner(fail_times=99), "purchase_place", base_delay_sec=0.0
    )

    response = await client.get("/health")
    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert "scheduler" in body, f"/health 未暴露调度器状态：{sorted(body)}"
    scheduler_block = body["scheduler"]
    assert scheduler_block["unhealthy"] is True, f"提交失败未被 /health 反映：{scheduler_block}"
    assert PURCHASE_PLACE in scheduler_block["unhealthy_task_types"]
    assert body["status"] == "degraded", f"/health 整体状态应为 degraded：{body['status']}"


async def test_health_endpoint_ok_when_no_failure(client: Any) -> None:
    """★ 反向对照：没有提交失败时 `/health` 不得因为"调度器没开"就报 degraded。

    测试环境 `SCHEDULER_ENABLED=False`，此时状态应为 `stopped` 而不是 `degraded` ——
    否则"没启用调度器"会被误报成"调度器坏了"，告警就没人信了。
    """
    response = await client.get("/health")
    assert response.status_code == 200, response.text
    body = response.json()["data"]
    scheduler_block = body["scheduler"]
    assert scheduler_block["unhealthy"] is False
    assert scheduler_block["status"] in {"ok", "stopped", "unknown"}, scheduler_block
    assert body["status"] == "ok", f"无失败时整体状态应为 ok：{body['status']}"


def test_scheduled_jobs_declare_phase_offsets() -> None:
    """★ 相位偏移是**显式声明**的：新增周期任务时必须顺带考虑它撞谁。"""
    declared = {str(job["job_id"]): int(job.get("phase_offset_sec") or 0) for job in sched.SCHEDULED_JOBS}
    assert declared[ORDER_SYNC] != declared[PURCHASE_PLACE], (
        f"两个 5min 任务的相位偏移相同，等于没错开：{declared}"
    )
    assert declared["inventory_sync"] != declared[ORDER_SYNC], declared
