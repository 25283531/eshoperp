"""★ 任务事务纪律回归（team-lead 指派的 A / B / C 三层）。

背景（史实缺陷）：
    两个 `ai_rework` 任务卡在 `running`，而它们的处理器**跨 30 分钟的长等待持有写事务**。
    SQLite 是**单写者** → 全系统写能力归零 → 所有写接口集体 500；
    只读接口因命中缓存 / 返回默认值仍 200，表象把排查方向带偏了两轮。

A（根因）：handler 内禁止跨长等待持有写事务 —— 拆成「短事务读参数 → 无事务等待 → 短事务回写」。
B：文件桥等待必须有有限上界，超时抛 `AiTimeoutError` 转 failed / retry。
C：`running` 残留必须能被回收（启动恢复 + 运行期看门狗）。

本文件逐条钉死，并额外验证「等待期间 / 处理器异常后，同一个 DB 仍然可写」——
这是"没有锁死全系统"的直接证据。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from app.adapters.ai.factory import AiClientFactory
from app.adapters.ai.file_bridge import (
    HARD_MAX_TIMEOUT_SEC,
    MIN_POLL_INTERVAL_SEC,
    MIN_POLL_TIMEOUT_SEC,
    WorkBuddyFileBridgeClient,
)
from app.adapters.ai.base import AiTimeoutError
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.errors import NotFoundError
from app.models.asset import AiTask
from app.models.enums import AiTaskStatus, TaskStatus, TaskType
from app.models.source import SourceProduct
from app.models.task import TaskRecord
from app.services.ai_task_service import AiTaskService
from app.tasks.recovery import STUCK_ERROR_CODE, reclaim_stuck_tasks, recover_pending_tasks
from app.tasks.handlers.ai_rework import ai_rework_handler
from app.utils.kit import utc_now


class _StubRunner:
    """只记录入队动作的假运行器（避免真的执行任务）。"""

    def __init__(self) -> None:
        """初始化。"""
        self.enqueued: list[int] = []

    def _enqueue(self, task_id: int, task_type: str, trace_id: str) -> None:
        """记录入队。"""
        self.enqueued.append(int(task_id))


def _uniq(prefix: str) -> str:
    """生成唯一串（避开唯一约束）。"""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ======================================================================
#  A：等待外部产出期间不得持有写事务
# ======================================================================


async def test_ai_wait_does_not_hold_write_lock(session: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """★ A 的核心证据：等待 AI 产出期间，**另一个会话仍能写入同一张库**。

    修复前：整个等待过程挂在 `async with factory() as session` 里 → 写事务长期不提交
    → SQLite 单写者 → 其它写入全部 `database is locked` → 写接口集体 500。
    """
    product = SourceProduct(product_1688_id=_uniq("hygiene"), title="事务纪律自检商品")
    session.add(product)
    await session.commit()

    ai_task = AiTask(
        source_product_id=int(product.id),
        target_platform="taobao",
        rework_items_json=["main_image"],
        status=AiTaskStatus.QUEUED.value,
    )
    session.add(ai_task)
    await session.commit()
    ai_task_id = int(ai_task.id)

    writes: list[str] = []

    async def _fake_wait(self: Any, task_id: str) -> dict[str, Any]:  # noqa: ANN401  替换实例方法，需带 self
        """模拟"等待外部产出"：期间用**另一个会话**写库，验证没有锁被持有。"""
        factory = get_session_factory()
        async with factory() as other:
            probe = SourceProduct(product_1688_id=_uniq("during-wait"), title="等待期间写入")
            other.add(probe)
            await other.commit()
            writes.append(str(probe.product_1688_id))
        raise AiTimeoutError(f"模拟超时：{task_id}")

    monkeypatch.setattr(WorkBuddyFileBridgeClient, "wait_result", _fake_wait)

    async def _fake_create(*args: Any, **kwargs: Any) -> WorkBuddyFileBridgeClient:
        """返回真实文件桥客户端（wait_result 已被替换）。"""
        return WorkBuddyFileBridgeClient()

    monkeypatch.setattr(AiClientFactory, "create", _fake_create)

    with pytest.raises(AiTimeoutError):
        await AiTaskService.run_task(ai_task_id, operator="tester")

    assert writes, "等待期间必须用另一个会话成功写入（证明未持有写锁）"

    # 失败状态也必须以独立短事务落库
    async with get_session_factory()() as check:
        row = (await check.execute(select(AiTask).where(AiTask.id == ai_task_id))).scalars().first()
        assert row is not None
        assert row.status == AiTaskStatus.FAILED.value, f"超时任务必须转 failed，实际 {row.status}"
        assert row.error_code == "AiTimeoutError"


async def test_handler_failure_keeps_db_writable(session: object) -> None:
    """处理器抛异常后，**同一个库仍然可写**（不得残留未关闭的写事务）。"""
    with pytest.raises(NotFoundError):
        await ai_rework_handler({"ai_task_id": 999999, "operator": "tester"}, SimpleNamespace(task_id=0))

    product = SourceProduct(product_1688_id=_uniq("after-failure"), title="异常后仍可写")
    session.add(product)
    await session.commit()
    assert int(product.id) > 0, "处理器异常后 DB 必须仍可写入（无锁残留）"


def test_run_task_is_three_phase_by_construction() -> None:
    """★ 结构性断言：`run_task` 中「等产出」的调用**不得**位于任何 `async with` 块内。"""
    import ast
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(AiTaskService.run_task))
    tree = ast.parse(source)
    func = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_task")

    with_ranges: list[tuple[int, int]] = []
    for node in ast.walk(func):
        if isinstance(node, ast.AsyncWith):
            with_ranges.append((node.lineno, node.end_lineno or node.lineno))

    # ★ 四种能力（ai_rework / image_redraw / title_suggest / video_script）的等待点
    #   **全部**纳入护栏：新能力的 await 若被搬进 helper，本断言会因 `wait_lines` 缺失而失败。
    wait_lines = [
        node.lineno
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr
        in {"rework_images", "redraw_images", "suggest_titles", "suggest_video_script"}
    ]
    assert wait_lines, "run_task 必须直接调用 AI 客户端的产出方法（不得整体搬进 helper）"
    for line in wait_lines:
        assert not any(start <= line <= end for start, end in with_ranges), (
            "★ 事务纪律 A：`rework_images()` 的等待不得写在任何 `async with` 会话块内"
        )


# ======================================================================
#  B：文件桥等待必须有有限上界
# ======================================================================


def test_file_bridge_timeout_is_clamped() -> None:
    """超时 / 轮询间隔一律被夹到有限区间（0、负数、超大值都不允许）。"""
    settings = get_settings()

    tiny = WorkBuddyFileBridgeClient(poll_timeout_sec=0, poll_interval_sec=0)
    assert tiny.poll_timeout_sec >= MIN_POLL_TIMEOUT_SEC
    assert tiny.poll_interval_sec >= MIN_POLL_INTERVAL_SEC

    huge = WorkBuddyFileBridgeClient(poll_timeout_sec=999999, poll_interval_sec=999999)
    assert huge.poll_timeout_sec <= HARD_MAX_TIMEOUT_SEC
    assert huge.poll_timeout_sec <= float(settings.ai_poll_timeout_max_sec)

    normal = WorkBuddyFileBridgeClient(poll_timeout_sec=30, poll_interval_sec=1)
    assert normal.poll_timeout_sec == 30.0
    assert normal.poll_timeout_sec >= normal.poll_interval_sec - MIN_POLL_INTERVAL_SEC


async def test_file_bridge_timeout_raises_quickly(tmp_path: Any) -> None:
    """无产出时必须在上界内抛 `AiTimeoutError`（绝不无限等待）。"""
    client = WorkBuddyFileBridgeClient(
        queue_dir=tmp_path / "queue",
        output_dir=tmp_path / "output",
        poll_timeout_sec=1.0,
        poll_interval_sec=0.2,
    )
    with pytest.raises(AiTimeoutError) as excinfo:
        await client.wait_result("no-such-task")
    assert "prompt.md" in str(excinfo.value), "超时提示必须指向人类可读的 prompt.md"


# ======================================================================
#  C：running 残留必须能被回收
# ======================================================================


async def test_recovery_recovers_running_leftovers(session: object) -> None:
    """★ C：模拟 running 残留 → 恢复后 `recovered > 0` 且回到 pending 并重新入队。"""
    record = TaskRecord(
        task_type=TaskType.AI_REWORK.value,
        payload_json={"ai_task_id": 1},
        status=TaskStatus.RUNNING.value,
        priority=5,
        max_retry=3,
        retry_count=0,
        trace_id=_uniq("trace"),
    )
    session.add(record)
    await session.commit()
    task_id = int(record.id)

    # ★ conftest 里 TASK_RECOVERY_ENABLED=false（避免测试期真的重跑任务），
    #   这里显式打开 —— 顺带证明"recovered=0"只可能是开关关闭，而不是逻辑失效。
    settings = get_settings().model_copy(update={"task_recovery_enabled": True})
    stub = _StubRunner()
    stats = await recover_pending_tasks(session=session, runner=stub, settings=settings)

    assert stats.recovered > 0, f"running 残留必须被恢复，实际 {stats.to_dict()}"
    assert task_id in stats.task_ids
    assert stub.enqueued, "恢复的任务必须重新入队"

    await session.refresh(record)
    assert record.status == TaskStatus.PENDING.value


async def test_recovery_disabled_still_reports_leftovers(session: object) -> None:
    """★ 恢复开关关闭时也要**如实上报**残留（不得再用 recovered=0 伪装"一切正常"）。"""
    session.add(
        TaskRecord(
            task_type=TaskType.AI_REWORK.value,
            payload_json={},
            status=TaskStatus.RUNNING.value,
            priority=5,
            max_retry=3,
            retry_count=0,
            trace_id=_uniq("disabled"),
        )
    )
    await session.commit()

    settings = get_settings().model_copy(update={"task_recovery_enabled": False})
    stats = await recover_pending_tasks(session=session, runner=_StubRunner(), settings=settings)

    assert stats.scanned >= 1, "关闭恢复也必须扫描出残留数量（早期版本直接返回 0，造成静默失效）"
    assert stats.recovered == 0, "关闭恢复时不应重新调度任务"


async def test_reclaim_stuck_tasks_only_reclaims_expired(session: object) -> None:
    """看门狗：只回收**超时**的 running；刚启动的任务不动，且重置后可重试。"""
    stale = TaskRecord(
        task_type=TaskType.AI_REWORK.value,
        payload_json={},
        status=TaskStatus.RUNNING.value,
        priority=5,
        max_retry=3,
        retry_count=0,
        trace_id=_uniq("stale"),
    )
    stale.started_at = utc_now()
    session.add(stale)

    fresh = TaskRecord(
        task_type=TaskType.ORDER_SYNC.value,
        payload_json={},
        status=TaskStatus.RUNNING.value,
        priority=5,
        max_retry=3,
        retry_count=0,
        trace_id=_uniq("fresh"),
    )
    session.add(fresh)
    await session.commit()

    # 把 stale 的 started_at 往前挪 2 小时（直接 SQL，避免 ORM 事件干扰）
    from datetime import timedelta

    stale.started_at = utc_now() - timedelta(hours=2)
    session.add(stale)
    await session.commit()

    stub = _StubRunner()
    stats = await reclaim_stuck_tasks(
        session=session, timeout_sec=60.0, runner=stub, settings=get_settings()
    )
    assert stats.recovered >= 1, f"超时的 running 必须被回收：{stats.to_dict()}"
    assert int(stale.id) in stats.task_ids
    assert int(fresh.id) not in stats.task_ids, "刚启动的任务不得被判死"

    await session.refresh(stale)
    await session.refresh(fresh)
    assert fresh.status == TaskStatus.RUNNING.value, "未超时的 running 必须保持原状"
    assert stale.error_code == STUCK_ERROR_CODE
    assert stale.status == TaskStatus.PENDING.value, "可回收的卡死任务应重置为 pending 等待重试"
    assert int(stale.retry_count or 0) == 1
