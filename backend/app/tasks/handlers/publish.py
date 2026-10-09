"""上架任务处理器（`publish`）。

★ 本处理器是 `PublishService.execute()` 的异步入口；真正的映射校验与
  `ListingAdapter.publish()` 调用都在 `PublishService.execute()` 内，
  **全系统仅此一处** publish 调用点（附录 A 第 3 条）。
"""

from __future__ import annotations

from typing import Any

from app.core.database import get_session_factory
from app.core.logging import get_logger
from app.models.enums import TaskType
from app.services.publish_service import PublishService
from app.tasks.registry import task_handler

logger = get_logger(__name__)

__all__ = ["publish_handler"]


@task_handler(TaskType.PUBLISH.value)
async def publish_handler(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """执行上架任务。

    payload:
        publish_task_id: int  上架任务 ID
        operator: str?        操作人
    """
    publish_task_id = int(payload.get("publish_task_id") or 0)
    operator = str(payload.get("operator") or "system")
    if publish_task_id <= 0:
        return {"ok": False, "reason": "publish_task_id 缺失"}

    factory = get_session_factory()
    async with factory() as session:
        try:
            task = await PublishService.execute(session, publish_task_id, operator=operator)
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            logger.exception("publish_handler_failed", publish_task_id=publish_task_id, error=str(exc))
            raise

    summary = PublishService.task_summary(task)
    summary["ok"] = task.status == "publish_success"
    logger.info(
        "publish_handler_done",
        task_id=getattr(ctx, "task_id", 0),
        publish_task_id=publish_task_id,
        status=task.status,
    )
    return summary
