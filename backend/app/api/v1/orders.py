"""订单与履约（ARCH §5.5.9）。

★ 三条硬约束：
    1. ORD-P0-03：未匹配 / `pending_confirm` 的订单一律**挂起并告警**，绝不盲发；
    2. 历史订单利润**只读 `order_item` 快照**，禁止 join 回 `sku_mapping` / `source_sku`（附录 A 第 18 条）；
    3. 订单同步**禁止裸调** `adapter.fetch_orders()`，必须 `invoke(Capability.FETCH_ORDERS, ...)`（附录 A 第 12 条）。
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.api.v1._common import enqueue_task, page_of
from app.core.deps import CurrentOperator, DbSession
from app.core.errors import NotFoundError
from app.core.pagination import PageParams, page_params
from app.core.response import ApiResponse
from app.models.enums import AuditObjectType, TaskType
from app.models.order import PurchaseOrder
from app.schemas.order import (
    OrderActionRequest,
    OrderDetailVo,
    OrderExceptionVo,
    OrderMatchRequest,
    OrderSyncRequest,
    OrderVo,
    PurchaseOrderTrackingRequest,
    PurchaseOrderVo,
)
from app.services.audit_service import AuditService
from app.services.order_service import OrderService

router = APIRouter(tags=["订单履约"])

__all__ = ["router"]


# ======================================================================
#  同步与查询
# ======================================================================


@router.post("/orders/sync", status_code=202, summary="拉取增量订单（异步）")
async def sync_orders(
    payload: OrderSyncRequest,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """提交 `order_sync` 任务。适配器不可用 → 503（由任务内降级处理）。"""
    task_record_id = await enqueue_task(
        TaskType.ORDER_SYNC.value,
        {
            "adapter_name": payload.adapter_name,
            "shop_ids": list(payload.shop_ids or []),
            "force": bool(payload.force),
            "operator": operator.name,
        },
    )
    await session.commit()
    return ApiResponse.ok(
        data={"task_record_id": task_record_id, "fetched": 0, "new": 0, "duplicated": 0},
        message="订单同步任务已受理",
    )


@router.get("/orders", summary="订单列表")
async def list_orders(
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    platform: Annotated[str | None, Query()] = None,
    shop_id: Annotated[str | None, Query()] = None,
    fulfillment_status: Annotated[str | None, Query()] = None,
    adapter_name: Annotated[str | None, Query()] = None,
    exception_type: Annotated[str | None, Query()] = None,
    paid_from: Annotated[str | None, Query(description="ISO8601")] = None,
    paid_to: Annotated[str | None, Query(description="ISO8601")] = None,
    keyword: Annotated[str | None, Query(description="订单号 / 买家脱敏信息")] = None,
) -> ApiResponse[Any]:
    """分页返回订单。"""
    rows, item_counts, total = await OrderService.list_orders(
        session,
        platform=platform,
        shop_id=shop_id,
        fulfillment_status=fulfillment_status,
        adapter_name=adapter_name,
        exception_type=exception_type,
        paid_from=paid_from,
        paid_to=paid_to,
        keyword=keyword,
        page=params.page,
        page_size=params.page_size,
    )
    items = [OrderVo.from_model(o, item_count=int(item_counts.get(int(o.id), 0))) for o in rows]
    return ApiResponse.ok(data=page_of(items, total, params))


@router.get("/orders/board", summary="订单看板（按履约状态分列）")
async def orders_board(
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    platform: Annotated[str | None, Query()] = None,
    shop_id: Annotated[str | None, Query()] = None,
    adapter_name: Annotated[str | None, Query()] = None,
) -> ApiResponse[dict[str, Any]]:
    """看板：按 `fulfillment_status` 分列，每列返回订单与计数。"""
    rows, item_counts, total = await OrderService.list_orders(
        session,
        platform=platform,
        shop_id=shop_id,
        adapter_name=adapter_name,
        page=params.page,
        page_size=params.page_size,
    )
    buckets: dict[str, list[OrderVo]] = {}
    for order in rows:
        buckets.setdefault(order.fulfillment_status or "unknown", []).append(
            OrderVo.from_model(order, item_count=int(item_counts.get(int(order.id), 0)))
        )
    columns = [
        {"status": status, "count": len(items), "orders": items}
        for status, items in sorted(buckets.items())
    ]
    return ApiResponse.ok(data={"columns": columns, "total": total})


@router.get("/orders/exceptions", summary="异常订单列表")
async def list_order_exceptions(
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    exception_type: Annotated[str | None, Query()] = None,
    handled: Annotated[bool | None, Query()] = None,
) -> ApiResponse[Any]:
    """异常订单（未匹配 / 采购失败 / 回填失败等）。"""
    rows, total = await OrderService.list_exceptions(
        session,
        exception_type=exception_type,
        handled=handled,
        page=params.page,
        page_size=params.page_size,
    )
    return ApiResponse.ok(
        data=page_of([OrderExceptionVo.from_model(o) for o in rows], total, params)
    )


@router.get("/orders/{order_id}", summary="订单详情")
async def get_order(order_id: int, session: DbSession) -> ApiResponse[OrderDetailVo]:
    """详情含 `items[]` / `purchase_orders[]` / `after_sales[]`。

    ★ `OrderDetailVo.profit_cents` 只读 `order_item` 快照（历史利润不可回溯改写）。
    """
    order = await OrderService.get_order(session, order_id)
    items = await OrderService.order_items(session, order_id)
    purchases = await OrderService.purchase_orders(session, order_id)
    after_sales = await OrderService.after_sales(session, order_id)
    detail = OrderDetailVo.from_model(
        order, items=items, purchase_orders=purchases, after_sales=after_sales
    )
    return ApiResponse.ok(data=detail)


@router.get("/orders/{order_id}/timeline", summary="订单状态流转时间线")
async def order_timeline(order_id: int, session: DbSession) -> ApiResponse[dict[str, Any]]:
    """时间线取自 `audit_log`（`object_type='order'`），不新建表。"""
    await OrderService.get_order(session, order_id)  # 404 校验
    logs, _total = await AuditService.list_logs(
        session,
        object_type=AuditObjectType.ORDER.value,
        object_id=str(order_id),
        include_system=True,
        page=1,
        page_size=200,
    )
    events: list[dict[str, Any]] = []
    for log in logs:
        old_value = _safe_json(log.old_value)
        new_value = _safe_json(log.new_value)
        events.append(
            {
                "at": _iso(log.created_at),
                "from_status": (old_value or {}).get("fulfillment_status"),
                "to_status": (new_value or {}).get("fulfillment_status"),
                "operator": log.operator,
                "note": log.remark,
                "trace_id": log.trace_id,
            }
        )
    return ApiResponse.ok(data={"events": events})


# ======================================================================
#  订单操作
# ======================================================================


@router.post("/orders/{order_id}/match", summary="手工匹配货源 SKU（可补建映射）")
async def match_order(
    order_id: int,
    payload: OrderMatchRequest,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """★ ORD-P0-03：匹配失败仍挂起并告警，不盲发。"""
    result = await OrderService.manual_match(
        session,
        order_id,
        sku_mapping_id=payload.sku_mapping_id,
        source_sku_id=payload.source_sku_id,
        create_mapping=bool(payload.create_mapping),
        operator=operator.name,
    )
    await session.commit()
    return ApiResponse.ok(data=result)


@router.post("/orders/{order_id}/place-purchase", summary="★ 采购下单（履约主路径闭环）")
async def place_purchase(
    order_id: int,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """★ 已匹配订单 → 1688 采购下单。

    ★★ 为什么补这个入口 ★★
        `OrderService.place_purchase()` 此前**没有任何调用点**：既没有 HTTP 入口，
        也没有定时任务入口。订单流转停死在 `matched`（已匹配），走不到 `purchased`，
        履约主路径在半路断掉 —— 而"订单能自动往下走"正是这套系统的核心价值。

    ★ 下单**必须**走履约适配器（`FulfillmentAdapter.invoke`），禁止裸调 1688：
        本地兜底 `local_csv` 会返回 `manual_pending` 并导出采购 CSV，
        **绝不伪装成"已下单"**（用户决策 ③）。

    Raises:
        409: 订单状态不是 `matched`（`StateConflictError`）。
        503: 适配器下单失败（订单置 `exception_purchase_failed`）。
    """
    purchase = await OrderService.place_purchase(session, order_id, operator=operator.name)
    await session.commit()
    return ApiResponse.ok(
        data={
            "id": int(purchase.id),
            "order_id": int(purchase.order_id),
            "purchase_order_no": purchase.purchase_order_no,
            "purchase_status": purchase.purchase_status,
            "amount_cents": int(purchase.amount_cents or 0),
            "adapter_name": purchase.adapter_name,
        },
        message=f"采购单已生成（{purchase.purchase_status}）",
    )


@router.post("/orders/{order_id}/actions", summary="订单处置动作")
async def order_action(
    order_id: int,
    payload: OrderActionRequest,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """retry / switch_source / refund / ignore。"""
    result = await OrderService.handle_action(
        session, order_id, action=payload.action, payload=payload.payload, operator=operator.name
    )
    await session.commit()
    return ApiResponse.ok(data=result)


# ======================================================================
#  采购单
# ======================================================================


@router.get("/purchase-orders", summary="采购单列表")
async def list_purchase_orders(
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    order_id: Annotated[int | None, Query()] = None,
    purchase_status: Annotated[str | None, Query()] = None,
    writeback_status: Annotated[str | None, Query()] = None,
) -> ApiResponse[Any]:
    """分页返回采购单。"""
    stmt = select(PurchaseOrder)
    if order_id is not None:
        stmt = stmt.where(PurchaseOrder.order_id == int(order_id))
    if purchase_status:
        stmt = stmt.where(PurchaseOrder.purchase_status == purchase_status)
    if writeback_status:
        stmt = stmt.where(PurchaseOrder.writeback_status == writeback_status)
    from sqlalchemy import func


    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = int((await session.execute(count_stmt)).scalar_one() or 0)
    rows = (
        (await session.execute(stmt.order_by(PurchaseOrder.id.desc()).offset(params.offset).limit(params.limit)))
        .scalars()
        .all()
    )
    return ApiResponse.ok(data=page_of([PurchaseOrderVo.from_model(r) for r in rows], total, params))


@router.get("/purchase-orders/{purchase_order_id}", summary="采购单详情")
async def get_purchase_order(
    purchase_order_id: int, session: DbSession
) -> ApiResponse[PurchaseOrderVo]:
    """采购单详情。"""
    row = (
        await session.execute(select(PurchaseOrder).where(PurchaseOrder.id == int(purchase_order_id)))
    ).scalars().first()
    if row is None:
        raise NotFoundError(f"采购单 {purchase_order_id} 不存在")
    return ApiResponse.ok(data=PurchaseOrderVo.from_model(row))


@router.post("/purchase-orders/{purchase_order_id}/writeback", summary="重试物流单号回填")
async def retry_writeback(
    purchase_order_id: int,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """★ 回填写店铺**只能**经 `ListingService.write_back_tracking`（R2）。"""
    result = await OrderService.retry_writeback(
        session, purchase_order_id, operator=operator.name
    )
    await session.commit()
    return ApiResponse.ok(data=result)


@router.post("/purchase-orders/{purchase_order_id}/tracking", summary="手工录入物流单号")
async def fill_tracking(
    purchase_order_id: int,
    payload: PurchaseOrderTrackingRequest,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[PurchaseOrderVo]:
    """本地兜底模式下由人工录入单号。"""
    row = await OrderService.fill_tracking(
        session,
        purchase_order_id,
        logistics_company=payload.logistics_company,
        tracking_no=payload.tracking_no,
        operator=operator.name,
    )
    await session.commit()
    return ApiResponse.ok(data=PurchaseOrderVo.from_model(row), message="物流单号已录入")


def _safe_json(raw: str | None) -> dict[str, Any]:
    """审计字段（旧值 / 新值）安全反序列化。"""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _iso(value: Any) -> str | None:
    """datetime → ISO8601 字符串。"""
    from app.utils.kit import iso_utc

    return iso_utc(value)
