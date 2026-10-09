"""映射冲突检测 / 成本同步定时任务（`mapping_check`，每日 03:10）。

职责（ARCH §4.3.1 + v1.4 成本三层语义）：
    1. 跑六类冲突检测并落库 `mapping_conflict`；
    2. 真源 → 镜像 成本自动同步（人工覆盖的不静默覆盖，改生成「成本待确认」工单）；
    3. 规格指纹比对（`spec_mismatch` 已包含在冲突检测内）。
"""

from __future__ import annotations

from typing import Any

from app.core.database import get_session_factory
from app.core.logging import get_logger
from app.models.enums import TaskType
from app.services.mapping_service import MappingService
from app.services.mapping_validator import MappingValidator
from app.tasks.registry import task_handler

logger = get_logger(__name__)

__all__ = ["mapping_check_handler"]


@task_handler(TaskType.MAPPING_CHECK.value)
async def mapping_check_handler(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """定时检测映射冲突 + 同步镜像成本。

    payload:
        source_product_ids: list[int]?  限定范围；为空则全量
        all: bool?                      强制全量检测
        operator: str?                  操作人
    """
    source_product_ids = [int(i) for i in (payload.get("source_product_ids") or [])]
    detect_all = bool(payload.get("all", True))
    operator = str(payload.get("operator") or "system")

    factory = get_session_factory()
    async with factory() as session:
        conflicts = await MappingValidator.detect_conflicts(
            session,
            source_product_ids=source_product_ids or None,
            detect_all=detect_all,
            operator=operator,
        )
        cost_sync = await MappingService.sync_cost_from_source(session, operator=operator)
        await session.commit()

    logger.info(
        "mapping_check_done",
        task_id=getattr(ctx, "task_id", 0),
        detected=conflicts.get("detected", 0),
        by_type=conflicts.get("by_type", {}),
        cost_sync=cost_sync,
    )
    return {"ok": True, "conflicts": conflicts, "cost_sync": cost_sync}
