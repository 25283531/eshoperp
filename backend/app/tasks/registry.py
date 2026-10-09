"""任务处理器注册表：`@task_handler("publish")`（ADR-3）。

处理器必须是 `async def handler(payload: dict, ctx: TaskContext) -> dict` 形式的协程函数。
"""

from __future__ import annotations

from typing import Any, Callable, Coroutine

from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "TASK_HANDLER_REGISTRY",
    "autoload_handlers",
    "get_handler",
    "list_handlers",
    "task_handler",
    "unregister_handler",
]

# 处理器签名：async def handler(payload: dict, ctx: TaskContext) -> dict
TaskHandler = Callable[[dict[str, Any], Any], Coroutine[Any, Any, dict[str, Any]]]

TASK_HANDLER_REGISTRY: dict[str, TaskHandler] = {}
_autoloaded = False


def task_handler(task_type: str) -> Callable[[TaskHandler], TaskHandler]:
    """注册任务处理器装饰器。

    Args:
        task_type: 任务类型（source_collect / ai_rework / publish / order_sync / inventory_sync / mapping_check）。
    """

    def _decorator(fn: TaskHandler) -> TaskHandler:
        TASK_HANDLER_REGISTRY[task_type] = fn
        logger.info("task_handler_registered", task_type=task_type, fn=getattr(fn, "__name__", str(fn)))
        return fn

    return _decorator


def unregister_handler(task_type: str) -> None:
    """注销处理器（测试用）。"""
    TASK_HANDLER_REGISTRY.pop(task_type, None)


def autoload_handlers() -> None:
    """惰性导入 `app.tasks.handlers` 包以触发 `@task_handler` 注册。

    ★ 该包由 T-A05 / T-A06 实现，此处用 try/except 保证框架可独立运行。
    """
    global _autoloaded
    if _autoloaded:
        return
    _autoloaded = True
    try:  # noqa: SIM105
        import app.tasks.handlers  # noqa: F401
    except ImportError:
        logger.debug("task_handlers_package_not_ready", message="app.tasks.handlers 尚未实现（T-A05/T-A06）")
    except Exception as exc:  # noqa: BLE001
        logger.warning("task_handlers_autoload_failed", error=str(exc))


def get_handler(task_type: str) -> TaskHandler | None:
    """取处理器；未命中时先尝试自动加载一次。"""
    handler = TASK_HANDLER_REGISTRY.get(task_type)
    if handler is None:
        autoload_handlers()
        handler = TASK_HANDLER_REGISTRY.get(task_type)
    return handler


def list_handlers() -> list[str]:
    """已注册的任务类型列表。"""
    autoload_handlers()
    return sorted(TASK_HANDLER_REGISTRY.keys())
