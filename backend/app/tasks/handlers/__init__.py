"""异步任务处理器集合（ADR-3）。

每个模块用 `@task_handler("任务类型")` 装饰器注册，导入本包即完成注册
（`app/tasks/registry.py :: autoload_handlers()` 会惰性 import 本包）。

处理器签名：`async def handler(payload: dict, ctx: TaskContext) -> dict`

★ 会话纪律：处理器在**独立线程 + 独立事件循环**中执行，
  必须自己开 `AsyncSession`（`get_session_factory()`），**不得**复用请求上下文的会话。
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.tasks.registry import autoload_handlers

logger = get_logger(__name__)

__all__ = ["register_all_handlers"]

# 导入即注册（顺序不影响，注册写入全局 TASK_HANDLER_REGISTRY）
from app.tasks.handlers.ai_rework import ai_rework_handler  # noqa: E402,F401
from app.tasks.handlers.inventory_sync import inventory_sync_handler  # noqa: E402,F401
from app.tasks.handlers.mapping_check import mapping_check_handler  # noqa: E402,F401
from app.tasks.handlers.order_sync import order_sync_handler  # noqa: E402,F401
from app.tasks.handlers.publish import publish_handler  # noqa: E402,F401
from app.tasks.handlers.purchase_place import purchase_place_handler  # noqa: E402,F401
from app.tasks.handlers.source_collect import source_collect_handler  # noqa: E402,F401


def register_all_handlers() -> list[str]:
    """显式注册全部处理器并返回已注册的任务类型列表。"""
    autoload_handlers()
    from app.tasks.registry import list_handlers

    return list_handlers()
