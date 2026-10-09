"""订单与履约服务（§5.5.9、§7.2）。

================================================================================
★ ★ ★ ORD-P0-03：匹配失败 100% 挂起，绝不盲发 ★ ★ ★
================================================================================
订单 SKU 匹配失败 **或** 映射处于 `pending_confirm`：
    → **一律**置为 `exception_unmatched`（异常-待匹配）+ 写异常说明 + 告警日志；
    → **绝不**"猜一个货源 SKU 发出去"。发错货的成本远高于挂起等待人工。

★ ★ ★ 附录 A 第 18 条：历史订单利润不可回溯改写 ★ ★ ★
================================================================================
`order_item.purchase_cost_cents` 是下单时点固化的**不可变快照**。
计算订单利润时**禁止 join 回 `sku_mapping` / `source_sku` 取成本** ——
货源涨价后镜像成本会变，但历史订单利润必须仍按下单时点的成本计算。
本模块所有利润相关逻辑一律读 `order_item` 快照。
================================================================================

★ `Order.adapter_name` 在订单创建时**固化**：切换适配器后在途订单仍按原渠道跑完（ADR-6）。
"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import func, or_, select

from app.adapters.fulfillment.base import MatchSkuRequest, PurchaseRequest
from app.adapters.fulfillment.manifest import ResultCode
from app.core.config import get_settings
from app.core.errors import BusinessError, ErrorCode, NotFoundError, StateConflictError
from app.core.logging import get_logger, get_trace_id
from app.models.enums import (
    AuditActionType,
    AuditObjectType,
    ExceptionType,
    HandlingAction,
    MappingStatus,
    MatchStatus,
    OrderFulfillmentStatus,
    PurchaseStatus,
    TaskType,
    WriteBackStatus,
)
from app.models.listing import ListingSku
from app.models.mapping import SkuMapping
from app.models.order import AfterSale, Order, OrderItem, PurchaseOrder
from app.services.audit_service import AuditService
from app.services.fulfillment_service import FulfillmentService
from app.utils.kit import iso_utc, parse_iso, utc_now

logger = get_logger(__name__)

__all__ = ["OrderService"]

# 拉取订单的默认回看窗口（分钟）
DEFAULT_SYNC_LOOKBACK_MIN = 60


class OrderService:
    """订单履约。"""

    # ==================================================================
    #  同步拉取
    # ==================================================================

    @staticmethod
    async def sync_orders(
        session: Any,
        *,
        adapter_name: str | None = None,
        shop_ids: list[str] | None = None,
        force: bool = False,
        operator: str = "system",
    ) -> dict[str, Any]:
        """★ 拉取订单并落库（`Order.adapter_name` 在此固化）。

        Returns:
            `{"task_record_id": int|None, "fetched": int, "new": int, "duplicated": int, "suspended": int}`。
        """
        adapter = await FulfillmentService.get_adapter(
            session, adapter_name=adapter_name, actor=operator
        )
        from datetime import timedelta

        from app.adapters.fulfillment.base import FetchOrdersRequest

        updated_from = iso_utc(utc_now() - timedelta(minutes=DEFAULT_SYNC_LOOKBACK_MIN)) if not force else ""

        # ★ 附录 A 第 5 条：禁止裸调 adapter.fetch_orders(...)，必须走 invoke(Capability.XXX)
        result = await adapter.invoke(
            "fetch_orders",
            req=FetchOrdersRequest(shop_ids=list(shop_ids or []), updated_from=updated_from),
        )
        if result is None or result.code not in {ResultCode.OK.value, ResultCode.DEGRADED.value}:
            raise BusinessError(
                f"拉取订单失败：{result.message if result else '适配器无响应'}",
                code=ErrorCode.ADAPTER_UNAVAILABLE,
                http_status=503,
            )

        payloads = list(result.data or [])
        fetched = len(payloads)
        new_count = 0
        duplicated = 0
        suspended = 0

        for payload in payloads:
            exists = (
                await session.execute(
                    select(Order.id).where(
                        Order.platform == payload.platform,
                        Order.shop_id == payload.shop_id,
                        Order.platform_order_no == payload.platform_order_no,
                    )
                )
            ).scalar_one_or_none()
            if exists is not None:
                duplicated += 1
                continue

            order = Order(
                platform=payload.platform,
                shop_id=payload.shop_id,
                platform_order_no=payload.platform_order_no,
                buyer_info_enc=payload.buyer_info_enc,
                receiver_addr_enc=payload.receiver_addr_enc,
                total_amount_cents=int(payload.total_amount_cents or 0),
                paid_at=parse_iso(payload.paid_at) or utc_now(),
                fulfillment_status=OrderFulfillmentStatus.PENDING_MATCH.value,
                adapter_name=adapter.adapter_name,  # ★ 固化下单渠道
                is_mock=False,
            )
            session.add(order)
            await session.flush()
            new_count += 1

            for item in payload.items:
                session.add(
                    OrderItem(
                        order_id=int(order.id),
                        shop_item_id=str(item.get("shop_item_id", "")),
                        shop_sku_code=str(item.get("shop_sku_code", "")),
                        quantity=int(item.get("quantity") or 1),
                        sale_price_cents=int(item.get("price_cents") or 0),
                        match_status=MatchStatus.UNMATCHED.value,
                    )
                )
            await session.flush()

            # 落库即匹配：失败则挂起（ORD-P0-03）
            matched, reason = await OrderService.match_order(session, int(order.id), operator=operator)
            if not matched:
                suspended += 1
                logger.warning("order_sync_suspended", order_id=order.id, reason=reason)

        await session.flush()
        task_record_id = None
        try:
            from app.tasks.runner import get_task_runner

            task_record_id = await get_task_runner().submit(
                task_type=TaskType.ORDER_SYNC.value,
                payload={"adapter_name": adapter.adapter_name, "fetched": fetched},
                task_key=f"order_sync:{iso_utc(utc_now())[:13]}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("order_sync_task_record_failed", error=str(exc))

        logger.info("order_sync_done", fetched=fetched, new=new_count, duplicated=duplicated, suspended=suspended)
        return {
            "task_record_id": task_record_id,
            "fetched": fetched,
            "new": new_count,
            "duplicated": duplicated,
            "suspended": suspended,
        }

    # ==================================================================
    #  ★ SKU 匹配（ORD-P0-03：失败 100% 挂起）
    # ==================================================================

    @staticmethod
    async def match_order(session: Any, order_id: int, *, operator: str = "system") -> tuple[bool, str]:
        """★ 匹配订单的全部 SKU。

        - 命中 `valid` 映射 → `matched`，成本**快照**写入 `order_item.purchase_cost_cents`；
        - 映射 `pending_confirm` / 未找到 / Mock → **一律挂起**为 `exception_unmatched`。

        Returns:
            `(是否全部匹配成功, 失败原因)`。
        """
        order = (
            await session.execute(select(Order).where(Order.id == int(order_id)))
        ).scalars().first()
        if order is None:
            raise NotFoundError(f"订单 {order_id} 不存在")

        items = (
            await session.execute(select(OrderItem).where(OrderItem.order_id == int(order_id)))
        ).scalars().all()
        if not items:
            order.match_status = MatchStatus.UNMATCHED.value
            await session.flush()
            return False, "订单无明细"

        adapter = await FulfillmentService.get_adapter(
            session, adapter_name=order.adapter_name, actor=operator
        )

        all_matched = True
        reasons: list[str] = []

        for item in items:
            result = await adapter.invoke(
                "match_sku",
                req=MatchSkuRequest(
                    platform=order.platform,
                    shop_id=order.shop_id,
                    shop_sku_code=item.shop_sku_code,
                    shop_item_id=item.shop_item_id,
                ),
            )
            data = getattr(result, "data", None)
            matched = bool(data and getattr(data, "matched", False))
            mapping_status = str(getattr(data, "mapping_status", "") or "")

            if not matched:
                all_matched = False
                item.match_status = MatchStatus.UNMATCHED.value
                reasons.append(f"{item.shop_sku_code}: {getattr(data, 'reason', '未匹配') or mapping_status}")
                continue
            if mapping_status and mapping_status != "valid":
                # ★ pending_confirm 同样挂起 —— 绝不拿不确定映射去下单
                all_matched = False
                item.match_status = MatchStatus.PENDING_CONFIRM.value
                reasons.append(f"{item.shop_sku_code}: 映射状态 {mapping_status}")
                continue

            mapping = (
                await session.execute(
                    select(SkuMapping).where(
                        SkuMapping.platform == order.platform,
                        SkuMapping.shop_id == order.shop_id,
                        SkuMapping.shop_sku_code == item.shop_sku_code,
                        SkuMapping.is_deleted.is_(False),
                        SkuMapping.is_mock.is_(False),
                    )
                )
            ).scalars().first()

            item.match_status = MatchStatus.MATCHED.value
            item.sku_mapping_id = int(mapping.id) if mapping else None
            item.source_sku_id = int(mapping.source_sku_id) if mapping else None
            item.source_sku_code_1688 = mapping.source_sku_code_1688 if mapping else (
                getattr(data, "source_sku_code_1688", None)
            )
            # ★★ 成本快照：下单时点固化，此后货源涨价**不得**回改（附录 A 第 18 条）
            if item.purchase_cost_cents is None:
                cost = getattr(data, "purchase_cost_cents", None)
                item.purchase_cost_cents = int(cost or 0) if cost else (
                    int(mapping.purchase_cost_cents) if mapping else None
                )

        reason = "; ".join(reasons)
        if all_matched:
            order.match_status = MatchStatus.MATCHED.value
            try:
                order.transition_to(OrderFulfillmentStatus.MATCHED.value)
            except ValueError:
                order.fulfillment_status = OrderFulfillmentStatus.MATCHED.value
            order.exception_type = None
            order.exception_note = None
        else:
            # ★★ ORD-P0-03：100% 挂起，绝不盲发
            order.match_status = MatchStatus.UNMATCHED.value
            order.fulfillment_status = OrderFulfillmentStatus.EXCEPTION_UNMATCHED.value
            order.exception_type = ExceptionType.UNMATCHED.value
            order.exception_note = reason or "SKU 匹配失败"
            logger.error(
                "order_suspended_unmatched",
                order_id=order.id,
                platform_order_no=order.platform_order_no,
                reason=order.exception_note,
            )

        await session.flush()
        return all_matched, reason

    # ==================================================================
    #  查询
    # ==================================================================

    @staticmethod
    async def list_orders(
        session: Any,
        *,
        platform: str | None = None,
        shop_id: str | None = None,
        fulfillment_status: str | None = None,
        adapter_name: str | None = None,
        exception_type: str | None = None,
        paid_from: Any = None,
        paid_to: Any = None,
        keyword: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Order], dict[int, int], int]:
        """分页查询订单（附带明细条数）。"""
        stmt = select(Order)
        if platform:
            stmt = stmt.where(Order.platform == platform)
        if shop_id:
            stmt = stmt.where(Order.shop_id == shop_id)
        if fulfillment_status:
            stmt = stmt.where(Order.fulfillment_status == fulfillment_status)
        if adapter_name:
            stmt = stmt.where(Order.adapter_name == adapter_name)
        if exception_type:
            stmt = stmt.where(Order.exception_type == exception_type)
        if paid_from is not None:
            stmt = stmt.where(Order.paid_at >= paid_from)
        if paid_to is not None:
            stmt = stmt.where(Order.paid_at <= paid_to)
        if keyword:
            like = f"%{keyword.strip()}%"
            stmt = stmt.where(
                or_(Order.platform_order_no.like(like), Order.receiver_name_enc.like(like))
            )

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await session.execute(count_stmt)).scalar_one() or 0)
        rows = (
            (await session.execute(stmt.order_by(Order.id.desc()).offset((page - 1) * page_size).limit(page_size)))
            .scalars()
            .all()
        )
        order_ids = [int(o.id) for o in rows]
        item_counts: dict[int, int] = {}
        if order_ids:
            counted = (
                await session.execute(
                    select(OrderItem.order_id, func.count(OrderItem.id))
                    .where(OrderItem.order_id.in_(order_ids))
                    .group_by(OrderItem.order_id)
                )
            ).all()
            item_counts = {int(oid): int(cnt) for oid, cnt in counted}
        return list(rows), item_counts, total

    @staticmethod
    async def get_order(session: Any, order_id: int) -> Order:
        """取订单（404）。"""
        order = (await session.execute(select(Order).where(Order.id == int(order_id)))).scalars().first()
        if order is None:
            raise NotFoundError(f"订单 {order_id} 不存在")
        return order

    @staticmethod
    async def order_items(session: Any, order_id: int) -> list[OrderItem]:
        """订单明细。"""
        rows = (
            await session.execute(select(OrderItem).where(OrderItem.order_id == int(order_id)).order_by(OrderItem.id))
        ).scalars().all()
        return list(rows)

    @staticmethod
    async def purchase_orders(session: Any, order_id: int) -> list[PurchaseOrder]:
        """订单关联的采购单。"""
        rows = (
            await session.execute(
                select(PurchaseOrder).where(PurchaseOrder.order_id == int(order_id)).order_by(PurchaseOrder.id)
            )
        ).scalars().all()
        return list(rows)

    @staticmethod
    async def after_sales(session: Any, order_id: int) -> list[AfterSale]:
        """订单关联的售后单。"""
        rows = (
            await session.execute(
                select(AfterSale).where(AfterSale.order_id == int(order_id)).order_by(AfterSale.id)
            )
        ).scalars().all()
        return list(rows)

    @staticmethod
    async def list_exceptions(
        session: Any,
        *,
        exception_type: str | None = None,
        handled: bool | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Order], int]:
        """异常订单列表。"""
        stmt = select(Order).where(Order.exception_type.isnot(None))
        if exception_type:
            stmt = stmt.where(Order.exception_type == exception_type)
        if handled is not None:
            stmt = (
                stmt.where(Order.handling_action.isnot(None))
                if handled
                else stmt.where(Order.handling_action.is_(None))
            )
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await session.execute(count_stmt)).scalar_one() or 0)
        rows = (
            (await session.execute(stmt.order_by(Order.id.desc()).offset((page - 1) * page_size).limit(page_size)))
            .scalars()
            .all()
        )
        return list(rows), total

    # ==================================================================
    #  采购下单 / 物流
    # ==================================================================

    @staticmethod
    async def place_purchase(
        session: Any,
        order_id: int,
        *,
        operator: str = "system",
    ) -> PurchaseOrder:
        """★ 1688 采购下单（走履约适配器）。

        本地兜底（`local_csv`）会返回 `manual_pending` —— 它**不做真实下单**，
        只导出采购清单 CSV 到 `data/exports/`，由人工去 1688 完成（用户决策 ③）。
        """
        order = await OrderService.get_order(session, order_id)
        if order.fulfillment_status != OrderFulfillmentStatus.MATCHED.value:
            raise StateConflictError(f"订单状态 {order.fulfillment_status} 不允许下单")

        items = await OrderService.order_items(session, order_id)
        if not items:
            raise BusinessError("订单无明细，无法下单", code=ErrorCode.ORDER_MATCH_FAILED)

        adapter = await FulfillmentService.get_adapter(
            session, adapter_name=order.adapter_name, actor=operator
        )
        first = items[0]

        result = await adapter.invoke(
            "place_purchase_order",
            req=PurchaseRequest(
                order_id=int(order.id),
                platform_order_no=order.platform_order_no,
                source_product_1688_id=str(first.source_sku_code_1688 or ""),
                source_sku_code_1688=str(first.source_sku_code_1688 or ""),
                quantity=int(first.quantity or 1),
                receiver_enc=order.receiver_addr_enc or "",
            ),
        )
        data = getattr(result, "data", None)
        if result is None or result.code not in {ResultCode.OK.value, ResultCode.DEGRADED.value} or data is None:
            order.fulfillment_status = OrderFulfillmentStatus.EXCEPTION_PURCHASE_FAILED.value
            order.exception_type = ExceptionType.PURCHASE_FAILED.value
            order.exception_note = result.message if result else "适配器无响应"
            await session.flush()
            raise BusinessError(
                order.exception_note, code=ErrorCode.ADAPTER_PURCHASE_FAILED, http_status=503
            )

        # ★ 成本金额取 order_item 快照之和（不 join 回映射）
        amount = sum(int(i.purchase_cost_cents or 0) * int(i.quantity or 1) for i in items)
        purchase = PurchaseOrder(
            order_id=int(order.id),
            purchase_order_no=str(getattr(data, "purchase_order_no", "") or ""),
            amount_cents=amount or int(getattr(data, "amount_cents", 0) or 0),
            adapter_name=adapter.adapter_name,
            purchase_status=str(getattr(data, "status", PurchaseStatus.PENDING.value) or PurchaseStatus.PENDING.value),
            raw_payload_json=dict(getattr(data, "raw", None) or {}),
        )
        session.add(purchase)
        await session.flush()

        for item in items:
            item.purchase_order_id = int(purchase.id)

        if purchase.purchase_status == PurchaseStatus.MANUAL_PENDING.value:
            # 本地兜底：挂「待人工下单」异常，提醒运营去 1688 完成
            order.fulfillment_status = OrderFulfillmentStatus.PURCHASED.value
        else:
            order.fulfillment_status = OrderFulfillmentStatus.PURCHASED.value
        await session.flush()

        await AuditService.write(
            session,
            action_type=AuditActionType.ORDER_ACTION.value,
            object_type=AuditObjectType.ORDER.value,
            object_id=order.id,
            operator=operator,
            new_value={
                "purchase_order_no": purchase.purchase_order_no,
                "status": purchase.purchase_status,
                "adapter": adapter.adapter_name,
            },
            trace_id=get_trace_id(),
            remark=f"采购下单：{purchase.purchase_order_no}（{purchase.purchase_status}）",
        )
        await session.flush()
        return purchase

    @staticmethod
    async def fill_tracking(
        session: Any,
        purchase_order_id: int,
        *,
        logistics_company: str,
        tracking_no: str,
        operator: str = "system",
    ) -> PurchaseOrder:
        """本地兜底手工录入物流单号。"""
        if not tracking_no or not tracking_no.strip():
            raise BusinessError("物流单号不能为空", code=ErrorCode.ORDER_TRACKING_INVALID)

        purchase = (
            await session.execute(select(PurchaseOrder).where(PurchaseOrder.id == int(purchase_order_id)))
        ).scalars().first()
        if purchase is None:
            raise NotFoundError(f"采购单 {purchase_order_id} 不存在")

        purchase.logistics_company = logistics_company.strip()
        purchase.tracking_no = tracking_no.strip()
        purchase.shipped_at = utc_now()
        purchase.purchase_status = PurchaseStatus.PLACED.value
        await session.flush()

        order = await OrderService.get_order(session, purchase.order_id)
        order.fulfillment_status = OrderFulfillmentStatus.SHIPPED.value
        await session.flush()

        await AuditService.write(
            session,
            action_type=AuditActionType.ORDER_ACTION.value,
            object_type=AuditObjectType.ORDER.value,
            object_id=order.id,
            operator=operator,
            new_value={"tracking_no": tracking_no, "company": logistics_company},
            trace_id=get_trace_id(),
            remark="手工录入物流单号",
        )
        await session.flush()
        return purchase

    @staticmethod
    async def retry_writeback(
        session: Any,
        purchase_order_id: int,
        *,
        operator: str = "system",
    ) -> dict[str, Any]:
        """重试物流回填（★ 红线 R2：回填走 `ListingAdapter`，履约适配器不得写店铺）。"""
        purchase = (
            await session.execute(select(PurchaseOrder).where(PurchaseOrder.id == int(purchase_order_id)))
        ).scalars().first()
        if purchase is None:
            raise NotFoundError(f"采购单 {purchase_order_id} 不存在")

        order = await OrderService.get_order(session, purchase.order_id)
        items = await OrderService.order_items(session, int(order.id))
        shop_item_ids = sorted({str(i.shop_item_id) for i in items if i.shop_item_id})

        try:
            from app.services.listing_service import ListingService

            await ListingService.write_back_tracking(
                session,
                shop_item_ids=shop_item_ids,
                logistics_company=purchase.logistics_company or "",
                tracking_no=purchase.tracking_no or "",
                operator=operator,
            )
            purchase.writeback_status = WriteBackStatus.SUCCESS.value
            order.fulfillment_status = OrderFulfillmentStatus.COMPLETED.value
        except Exception as exc:  # noqa: BLE001
            purchase.writeback_status = WriteBackStatus.FAILED.value
            purchase.writeback_retry = int(purchase.writeback_retry or 0) + 1
            order.fulfillment_status = OrderFulfillmentStatus.EXCEPTION_WRITEBACK_FAILED.value
            order.exception_type = ExceptionType.WRITEBACK_FAILED.value
            order.exception_note = str(exc)
            logger.warning("writeback_retry_failed", purchase_order_id=purchase.id, error=str(exc))

        await session.flush()
        return {
            "writeback_status": purchase.writeback_status,
            "retry": int(purchase.writeback_retry or 0),
        }

    # ==================================================================
    #  手工匹配 / 处置动作
    # ==================================================================

    @staticmethod
    async def manual_match(
        session: Any,
        order_id: int,
        *,
        sku_mapping_id: int | None = None,
        source_sku_id: int | None = None,
        create_mapping: bool = False,
        operator: str = "system",
    ) -> dict[str, Any]:
        """手工指定货源 SKU（可补建映射），匹配成功后订单恢复可下单。"""
        order = await OrderService.get_order(session, order_id)
        mapping = None
        if sku_mapping_id:
            mapping = (
                await session.execute(select(SkuMapping).where(SkuMapping.id == int(sku_mapping_id)))
            ).scalars().first()
        elif source_sku_id:
            mapping = (
                await session.execute(
                    select(SkuMapping)
                    .where(
                        SkuMapping.source_sku_id == int(source_sku_id),
                        SkuMapping.platform == order.platform,
                        SkuMapping.shop_id == order.shop_id,
                        SkuMapping.is_deleted.is_(False),
                    )
                    .order_by(SkuMapping.id.desc())
                )
            ).scalars().first()

        if mapping is None:
            if not create_mapping:
                raise BusinessError(
                    "未找到可用映射；如需补建请传 create_mapping=true 与 source_sku_id",
                    code=ErrorCode.MAPPING_MISSING,
                )
            raise BusinessError("补建映射请走映射管理接口", code=ErrorCode.MAPPING_MISSING)

        # ★★ ORD-P0-03 的**手工旁路**必须堵住 ★★
        #   实测（真实 HTTP）：映射因 spec_mismatch 已被自动置为 `pending_confirm`、
        #   自动匹配也正确地把订单挂起了，但只要**手工**传 `sku_mapping_id`
        #   调 `POST /orders/{id}/match`，就能把订单直接刷成 `matched` ——
        #   规格已经变了却照样发货，这正是 QA-03 要防的"发错货"，只是换了个入口。
        #   「手工」指的是**手工选哪条映射**，不是「手工绕过安全规则」：
        #   非 valid / Mock 的映射一律不得用于发货匹配，先去待确认工单处理。
        if str(mapping.status) != MappingStatus.VALID.value:
            raise BusinessError(
                f"映射 #{int(mapping.id)} 当前状态为 {mapping.status}，不得用于发货匹配"
                f"（请先到「待确认工单」处理，处理完成后映射恢复 valid 即可继续）",
                code=ErrorCode.MAPPING_STATUS_INVALID,
                http_status=409,
            )
        if bool(getattr(mapping, "is_mock", False)):
            raise BusinessError(
                f"映射 #{int(mapping.id)} 是 Mock 数据，不得用于真实发货匹配",
                code=ErrorCode.MAPPING_STATUS_INVALID,
                http_status=409,
            )

        items = await OrderService.order_items(session, order_id)
        for item in items:
            if item.shop_sku_code != mapping.shop_sku_code and items.index(item) != 0:
                continue
            item.sku_mapping_id = int(mapping.id)
            item.source_sku_id = mapping.source_sku_id
            item.source_sku_code_1688 = mapping.source_sku_code_1688
            item.match_status = MatchStatus.MATCHED.value
            # ★ 快照：固化当前镜像成本（此后货源涨价不影响本单）
            if item.purchase_cost_cents is None:
                item.purchase_cost_cents = int(mapping.purchase_cost_cents or 0)
        await session.flush()

        order.match_status = MatchStatus.MATCHED.value
        order.exception_type = None
        order.exception_note = None
        order.fulfillment_status = OrderFulfillmentStatus.MATCHED.value
        await session.flush()

        await AuditService.write(
            session,
            action_type=AuditActionType.ORDER_ACTION.value,
            object_type=AuditObjectType.ORDER.value,
            object_id=order.id,
            operator=operator,
            new_value={"mapping_id": int(mapping.id), "action": "manual_match"},
            trace_id=get_trace_id(),
            remark=f"手工匹配订单到映射 #{mapping.id}",
        )
        await session.flush()
        return {"order_id": int(order.id), "match_status": "matched", "mapping_id": int(mapping.id)}

    @staticmethod
    async def handle_action(
        session: Any,
        order_id: int,
        *,
        action: str,
        payload: dict[str, Any] | None = None,
        operator: str = "system",
    ) -> dict[str, Any]:
        """订单处置动作：retry / switch_source / refund / ignore。"""
        payload = payload or {}
        order = await OrderService.get_order(session, order_id)
        message = ""

        if action == HandlingAction.RETRY.value:
            matched, reason = await OrderService.match_order(session, order_id, operator=operator)
            message = "重新匹配成功" if matched else f"重新匹配仍失败：{reason}"
        elif action == HandlingAction.SWITCH_SOURCE.value:
            new_code = str(payload.get("source_sku_code_1688") or "")
            if not new_code:
                raise BusinessError("switch_source 需要 payload.source_sku_code_1688", code=ErrorCode.PARAM_ERROR)
            items = await OrderService.order_items(session, order_id)
            for item in items:
                item.source_sku_code_1688 = new_code
                item.match_status = MatchStatus.MATCHED.value
            order.exception_type = None
            order.exception_note = None
            order.fulfillment_status = OrderFulfillmentStatus.MATCHED.value
            message = f"已切换货源 SKU 为 {new_code}"
        elif action == HandlingAction.REFUND.value:
            order.fulfillment_status = OrderFulfillmentStatus.AFTER_SALE.value
            message = "已转入售后流程"
        elif action == HandlingAction.IGNORE.value:
            message = f"已忽略异常：{payload.get('reason', '')}"
        else:
            raise BusinessError(f"不支持的处置动作：{action}", code=ErrorCode.PARAM_ERROR)

        order.handling_action = action
        order.handled_by = operator
        order.handled_at = utc_now()
        await session.flush()

        await AuditService.write(
            session,
            action_type=AuditActionType.ORDER_ACTION.value,
            object_type=AuditObjectType.ORDER.value,
            object_id=order.id,
            operator=operator,
            new_value={"action": action, "payload": payload},
            trace_id=get_trace_id(),
            remark=message,
        )
        await session.flush()
        return {"order_id": int(order.id), "fulfillment_status": order.fulfillment_status, "message": message}

    # ==================================================================
    #  ★ 利润计算（附录 A 第 18 条：只读快照，严禁 join 回取成本）
    # ==================================================================

    @staticmethod
    def profit_cents(items: Iterable[OrderItem]) -> int:
        """★ 计算订单利润（分）—— **只读 `order_item` 快照**，绝不 join 回 `sku_mapping` / `source_sku`。

        这是附录 A 第 18 条「历史订单利润不可回溯改写」的核心落地：
        货源涨价后 `sku_mapping.purchase_cost_cents` 会变，
        但历史订单的利润**必须**仍按下单时点固化的 `order_item.purchase_cost_cents` 计算。
        """
        total = 0
        for item in items:
            sale = int(item.sale_price_cents or 0)
            cost = int(item.purchase_cost_cents or 0)
            total += (sale - cost) * int(item.quantity or 1)
        return total

    @staticmethod
    def order_summary(order: Order) -> dict[str, Any]:
        """订单摘要（任务回调用）。"""
        return {
            "order_id": int(order.id),
            "platform_order_no": order.platform_order_no,
            "fulfillment_status": order.fulfillment_status,
            "adapter_name": order.adapter_name,
            "synced_at": iso_utc(utc_now()),
        }

    @staticmethod
    def listing_sku_price_cents(session: Any, listing_sku: ListingSku) -> int:
        """取平台 SKU 售价（分）。

        说明：仅用于**当前**商品展示与告警，**不得**用于历史订单利润核算。
        """
        return int(listing_sku.sale_price_cents or 0)

    @staticmethod
    def settings_digest() -> dict[str, Any]:
        """订单同步配置摘要。"""
        settings = get_settings()
        return {"order_sync_interval_min": 5, "db_dialect": settings.db_dialect}
