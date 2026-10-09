"""库存与价格监控（ARCH §5.5.11）。

★ 红线 R2：库存归零 / 成本涨价触发的自动下架**只经** `ListingService.offline()`，
  本模块的 API 绝不直接下架（第三方更不得直写店铺）。
★ 成本三层：真源 `source_sku` → 镜像 `sku_mapping` → 快照 `order_item`（历史利润不可改写）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.api.v1._common import enqueue_task, page_of
from app.core.deps import AdminOperator, CurrentOperator, DbSession
from app.core.pagination import PageParams, page_params
from app.core.response import ApiResponse
from app.models.enums import TaskType
from app.schemas.inventory import (
    AutoOfflineRecordVo,
    InventoryAlertVo,
    InventoryConfigUpdate,
    InventoryConfigVo,
    InventorySnapshotVo,
    InventorySyncRequest,
    PriceSnapshotVo,
)
from app.services.inventory_service import InventoryService

router = APIRouter(tags=["库存价格"])

__all__ = ["router"]


@router.post("/inventory/sync", status_code=202, summary="采集库存 / 价格快照（异步）")
async def sync_inventory(
    payload: InventorySyncRequest,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """提交 `inventory_sync` 任务（快照 → 成本同步 → 告警 → 自动下架）。"""
    task_record_id = await enqueue_task(
        TaskType.INVENTORY_SYNC.value,
        {
            "source_sku_ids": [int(i) for i in (payload.source_sku_ids or [])],
            "force": bool(payload.force),
            "operator": operator.name,
        },
    )
    await session.commit()
    return ApiResponse.ok(
        data={"task_record_id": task_record_id, "snapshot_count": 0},
        message="库存同步任务已受理",
    )


@router.get("/inventory/snapshots", summary="库存快照列表")
async def list_snapshots(
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    source_sku_id: Annotated[int | None, Query()] = None,
    source_product_id: Annotated[int | None, Query()] = None,
    stock_zero_only: Annotated[bool, Query()] = False,
    collected_from: Annotated[str | None, Query(description="ISO8601")] = None,
) -> ApiResponse[Any]:
    """库存快照。"""
    rows, sku_names, product_titles, total = await InventoryService.list_snapshots(
        session,
        source_sku_id=source_sku_id,
        source_product_id=source_product_id,
        stock_zero_only=stock_zero_only,
        collected_from=collected_from,
        page=params.page,
        page_size=params.page_size,
    )
    items = [
        InventorySnapshotVo.from_model(
            r,
            source_sku_name=sku_names.get(int(r.source_sku_id)),
            source_product_title=product_titles.get(int(r.source_sku_id)),
        )
        for r in rows
    ]
    return ApiResponse.ok(data=page_of(items, total, params))


@router.get("/inventory/price-snapshots", summary="成本价快照列表")
async def list_price_snapshots(
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    source_sku_id: Annotated[int | None, Query()] = None,
    over_threshold_only: Annotated[bool, Query(description="仅看超过涨价阈值的记录")] = False,
) -> ApiResponse[Any]:
    """成本价快照（含 `change_rate`）。"""
    rows, sku_names, product_titles, total = await InventoryService.list_price_snapshots(
        session,
        source_sku_id=source_sku_id,
        over_threshold_only=over_threshold_only,
        page=params.page,
        page_size=params.page_size,
    )
    items = [
        PriceSnapshotVo.from_model(
            r,
            source_sku_name=sku_names.get(int(r.source_sku_id)),
            source_product_title=product_titles.get(int(r.source_sku_id)),
        )
        for r in rows
    ]
    return ApiResponse.ok(data=page_of(items, total, params))


@router.get("/inventory/alerts", summary="库存 / 价格告警列表")
async def list_alerts(
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    type: Annotated[str | None, Query(description="out_of_stock / price_increase")] = None,
) -> ApiResponse[Any]:
    """告警列表（含 `suggested_action`：offline / notify）。"""
    rows, total = await InventoryService.list_alerts(
        session, alert_type=type, page=params.page, page_size=params.page_size
    )
    return ApiResponse.ok(data=page_of([InventoryAlertVo(**r.model_dump()) for r in rows], total, params))


@router.get("/inventory/auto-offline-records", summary="自动下架记录")
async def list_auto_offline_records(
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
) -> ApiResponse[Any]:
    """自动下架执行记录（成功 / 失败与原因）。"""
    rows, total = await InventoryService.list_auto_offline_records(
        session, page=params.page, page_size=params.page_size
    )
    return ApiResponse.ok(
        data=page_of([AutoOfflineRecordVo(**r.model_dump()) for r in rows], total, params)
    )


@router.get("/inventory/config", summary="库存监控配置")
async def get_inventory_config(session: DbSession) -> ApiResponse[InventoryConfigVo]:
    """轮询间隔 / 涨价阈值 / 归零与涨价动作。"""
    return ApiResponse.ok(data=await InventoryService.get_config(session))


@router.put("/inventory/config", summary="修改库存监控配置（管理员）")
async def update_inventory_config(
    payload: InventoryConfigUpdate,
    session: DbSession,
    admin: AdminOperator,
) -> ApiResponse[InventoryConfigVo]:
    """修改配置（写审计）。"""
    config = await InventoryService.update_config(
        session,
        poll_interval_min=payload.poll_interval_min,
        price_increase_threshold=payload.price_increase_threshold,
        out_of_stock_action=payload.out_of_stock_action,
        price_increase_action=payload.price_increase_action,
        operator=admin.name,
    )
    await session.commit()
    return ApiResponse.ok(data=config, message="库存配置已更新")
