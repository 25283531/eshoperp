"""API 层内部公共工具（**非端点模块**，不对外暴露路由）。

提供两件小事，避免在 14 个端点模块里重复实现：
    1. `enqueue_task()` —— 提交异步任务并拿 `task_record_id`（runner 不可用时返回 None，调用方以同步结果兜底）；
    2. `page_of()` —— 用 `PageResult` 包装分页结果。
"""

from __future__ import annotations

from typing import Any, Sequence

from app.core.logging import get_logger
from app.core.pagination import PageParams
from app.core.response import PageResult

logger = get_logger(__name__)

__all__ = ["enqueue_task", "page_of"]


async def enqueue_task(task_type: str, payload: dict[str, Any]) -> int | None:
    """提交异步任务，返回 `TaskRecord.id`。

    Args:
        task_type: `TaskType` 枚举值（如 `source_collect`）。
        payload: 任务负载（处理器读取的键见各 handler 文档字符串）。

    Returns:
        `task_record_id`；runner 未就绪时返回 `None`（**不抛异常**，
        由调用方决定是改为同步执行还是直接返回同步结果）。
    """
    try:
        from app.tasks.runner import get_task_runner

        runner = get_task_runner()
        return int(await runner.submit(task_type=task_type, payload=payload))
    except Exception as exc:  # noqa: BLE001  任务框架不可用时不应阻断业务读接口
        logger.warning("enqueue_task_failed", task_type=task_type, error=str(exc))
        return None


def page_of(items: Sequence[Any], total: int, params: PageParams) -> PageResult[Any]:
    """按分页参数包装为 `PageResult`。"""
    return PageResult.build(items, total, params.page, params.page_size)
