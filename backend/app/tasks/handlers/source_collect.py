"""货源采集任务处理器（`source_collect`）。

★ 任务语义（2026-10-08 修正）：
    **全部失败**（`created + updated == 0` 且 `failed` 非空）时把 `task_record.status`
    标为 `failed`，并保留 `result_json.failed[]` 明细。

    "10 个商品一个都没采到"却报 success —— 前端只看 status 就会显示"采集成功"，
    使用者面对一个"成功但空空如也"的结果无从下手。这不是显示 bug，是**静默失效**。

    局部失败（`created + updated > 0`）仍维持 `success`：
    那是运营需要知道的**部分成功**，失败明细在 `result_json.failed[]` 里照常返回。
"""

from __future__ import annotations

from typing import Any

from app.core.database import get_session_factory
from app.core.logging import get_logger
from app.models.enums import TaskType
from app.services.source_service import SourceService
from app.tasks.registry import task_handler
from app.tasks.runner import TASK_STATUS_OVERRIDE_KEY, TASK_STATUS_REASON_KEY

logger = get_logger(__name__)

__all__ = ["source_collect_handler"]


@task_handler(TaskType.SOURCE_COLLECT.value)
async def source_collect_handler(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """采集 1688 商品。

    payload:
        identifiers: list[str]  商品 ID 或链接
        supplier_id: int?       供应商 ID
        operator: str?          操作人

    Returns:
        `SourceService.collect()` 的结果 + 全失败时的 `__task_status__ = "failed"`。
    """
    identifiers = list(payload.get("identifiers") or [])
    supplier_id = payload.get("supplier_id")
    operator = str(payload.get("operator") or "system")

    factory = get_session_factory()
    async with factory() as session:
        result = await SourceService.collect(
            session,
            identifiers,
            supplier_id=supplier_id,
            operator=operator,
        )
        await session.commit()

    created = int(result.get("created") or 0)
    updated = int(result.get("updated") or 0)
    failed = list(result.get("failed") or [])
    if created + updated == 0 and failed:
        # ★ 结论性失败：**不重试**（凭证缺失是确定性问题，重试只会把结论推迟）
        first_reason = str((failed[0] or {}).get("reason") or "未知原因")
        result[TASK_STATUS_OVERRIDE_KEY] = "failed"
        result[TASK_STATUS_REASON_KEY] = (
            f"全部 {len(failed)} 个商品均未采集成功（例如：{first_reason}）；明细见 result_json.failed"
        )

    logger.info(
        "source_collect_handler_done",
        task_id=getattr(ctx, "task_id", 0),
        accepted=result.get("accepted", 0),
        created=created,
        updated=updated,
        failed=len(failed),
        declared_status=str(result.get(TASK_STATUS_OVERRIDE_KEY) or "success"),
    )
    return result
