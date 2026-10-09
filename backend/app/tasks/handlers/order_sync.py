"""订单同步定时任务（`order_sync`，每 5 分钟）。"""

from __future__ import annotations

from typing import Any

from app.core.database import get_session_factory
from app.core.logging import get_logger
from app.models.enums import TaskType
from app.services.order_service import OrderService
from app.tasks.registry import task_handler

logger = get_logger(__name__)

__all__ = ["order_sync_handler"]


@task_handler(TaskType.ORDER_SYNC.value)
async def order_sync_handler(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """拉取增量订单。

    payload:
        adapter_name: str?   指定适配器；为空则用当前生效适配器
        shop_ids: list[str]? 限定店铺
        force: bool?         强制全量（忽略时间窗口）
        operator: str?       操作人
    """
    adapter_name = payload.get("adapter_name")
    shop_ids = list(payload.get("shop_ids") or [])
    force = bool(payload.get("force", False))
    operator = str(payload.get("operator") or "system")

    factory = get_session_factory()
    async with factory() as session:
        try:
            result = await OrderService.sync_orders(
                session,
                adapter_name=adapter_name,
                shop_ids=shop_ids,
                force=force,
                operator=operator,
            )
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            logger.exception("order_sync_handler_failed", error=str(exc))
            raise

    logger.info(
        "order_sync_handler_done",
        task_id=getattr(ctx, "task_id", 0),
        fetched=result.get("fetched", 0),
        new=result.get("new", 0),
        suspended=result.get("suspended", 0),
    )
    return result
