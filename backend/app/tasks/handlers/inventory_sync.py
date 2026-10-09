"""库存 / 价格同步定时任务（`inventory_sync`，每 30 分钟）。

★ 处理器在**独立线程 + 独立事件循环**中执行，
  必须自己开 `AsyncSession`（`get_session_factory()`），
  **不得**复用请求上下文的会话（ADR-3）。

链路（`InventoryService.sync`，见该文件顶部红线说明）：
    采集 SourceSku 当前库存 / 成本 → 写库存快照 + 价格快照
    → MappingService.sync_cost_from_source（人工覆盖的转「成本待确认」工单，不静默覆盖）
    → _detect_alerts 阈值判定
    → _apply_actions → ListingService.offline()（★ 下架唯一出口）
"""

from __future__ import annotations

from typing import Any

from app.core.database import get_session_factory
from app.core.logging import get_logger
from app.models.enums import TaskType
from app.services.inventory_service import InventoryService
from app.tasks.registry import task_handler

logger = get_logger(__name__)

__all__ = ["inventory_sync_handler"]


@task_handler(TaskType.INVENTORY_SYNC.value)
async def inventory_sync_handler(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """采集库存与价格快照，并触发告警 / 自动下架。

    payload:
        source_sku_ids: list[int]? 限定货源 SKU；为空则全量
        force: bool?              强制刷新（忽略快照去重窗口）
        operator: str?            操作人（审计留痕）
    """
    raw_ids = payload.get("source_sku_ids") or []
    source_sku_ids: list[int] = []
    for item in raw_ids:
        try:
            source_sku_ids.append(int(item))
        except (TypeError, ValueError):  # noqa: PERF203
            logger.warning("inventory_sync_invalid_sku_id", raw=item)
    force = bool(payload.get("force", False))
    operator = str(payload.get("operator") or "system")

    factory = get_session_factory()
    async with factory() as session:
        try:
            result = await InventoryService.sync(
                session,
                source_sku_ids=source_sku_ids or None,
                force=force,
                operator=operator,
            )
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            logger.exception("inventory_sync_handler_failed", error=str(exc))
            raise

    logger.info(
        "inventory_sync_handler_done",
        task_id=getattr(ctx, "task_id", 0),
        snapshot_count=result.get("snapshot_count", 0),
        alerts=result.get("alerts", 0),
        auto_offline=result.get("auto_offline", 0),
    )
    return result
