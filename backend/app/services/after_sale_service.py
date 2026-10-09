"""售后服务（§5.5.10）。

★ 能力降级不抛异常：`submit_refund` / `get_return_address` 在**本地兜底**下是
  UNSUPPORTED（用户决策 ③：1688 退款与退货地址已全部委托妙手 / 逸淘）。
  此时本服务返回 **503 + 明确提示去切换适配器**，而不是假装成功。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from app.adapters.fulfillment.base import RefundRequest, ReturnAddressQuery
from app.adapters.fulfillment.manifest import ResultCode
from app.core.errors import BusinessError, ErrorCode, NotFoundError
from app.core.logging import get_logger, get_trace_id
from app.models.enums import (
    AuditActionType,
    AuditObjectType,
    Refund1688Status,
    Responsibility,
    ReturnAddressPushStatus,
)
from app.models.order import AfterSale, Order, PurchaseOrder
from app.services.audit_service import AuditService
from app.services.fulfillment_service import FulfillmentService
from app.utils.csvio import cents_to_yuan_str
from app.utils.kit import iso_utc, utc_now

logger = get_logger(__name__)

__all__ = ["AfterSaleService"]


class AfterSaleService:
    """售后退款与退货地址。"""

    @staticmethod
    async def list_after_sales(
        session: Any,
        *,
        order_id: int | None = None,
        handling_status: str | None = None,
        responsibility: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[AfterSale], int]:
        """分页查询售后单。"""
        stmt = select(AfterSale)
        if order_id is not None:
            stmt = stmt.where(AfterSale.order_id == int(order_id))
        if handling_status:
            stmt = stmt.where(AfterSale.handling_status == handling_status)
        if responsibility:
            stmt = stmt.where(AfterSale.responsibility == responsibility)

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await session.execute(count_stmt)).scalar_one() or 0)
        rows = (
            (
                await session.execute(
                    stmt.order_by(AfterSale.id.desc()).offset((page - 1) * page_size).limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        return list(rows), total

    @staticmethod
    async def get(session: Any, after_sale_id: int) -> AfterSale:
        """取售后单（404）。"""
        row = (
            await session.execute(select(AfterSale).where(AfterSale.id == int(after_sale_id)))
        ).scalars().first()
        if row is None:
            raise NotFoundError(f"售后单 {after_sale_id} 不存在")
        return row

    @staticmethod
    async def create(
        session: Any,
        *,
        order_id: int,
        platform_refund_no: str = "",
        refund_reason: str = "",
        refund_amount_cents: int = 0,
        operator: str = "system",
    ) -> AfterSale:
        """创建售后单并把订单转入售后状态。"""
        order = (await session.execute(select(Order).where(Order.id == int(order_id)))).scalars().first()
        if order is None:
            raise NotFoundError(f"订单 {order_id} 不存在")
        row = AfterSale(
            order_id=int(order_id),
            platform_refund_no=platform_refund_no,
            refund_reason=refund_reason,
            refund_amount_cents=int(refund_amount_cents or 0),
            refund_1688_status=Refund1688Status.PENDING.value,
        )
        session.add(row)
        await session.flush()
        order.fulfillment_status = "after_sale"
        await session.flush()
        await AuditService.write(
            session,
            action_type=AuditActionType.ORDER_ACTION.value,
            object_type=AuditObjectType.ORDER.value,
            object_id=order.id,
            operator=operator,
            new_value={"after_sale_id": int(row.id), "amount": cents_to_yuan_str(row.refund_amount_cents)},
            trace_id=get_trace_id(),
            remark=f"创建售后单：{refund_reason}",
        )
        return row

    # ==================================================================
    #  提交 1688 退款
    # ==================================================================

    @staticmethod
    async def submit_refund(
        session: Any,
        after_sale_id: int,
        *,
        refund_amount_cents: int | None = None,
        reason: str = "",
        operator: str = "system",
    ) -> dict[str, Any]:
        """★ 提交 1688 退款（走履约适配器）。

        本地兜底**明确 UNSUPPORTED** → 返回 503 + 提示切换到妙手 / 逸淘，
        **绝不**生成"看起来成功"的待办 CSV（那会让运营以为退款已提交）。
        """
        row = await AfterSaleService.get(session, after_sale_id)
        order = await AfterSaleService._order(session, row.order_id)
        amount = int(refund_amount_cents if refund_amount_cents is not None else (row.refund_amount_cents or 0))

        purchase = (
            await session.execute(
                select(PurchaseOrder)
                .where(PurchaseOrder.order_id == int(order.id))
                .order_by(PurchaseOrder.id.desc())
                .limit(1)
            )
        ).scalars().first()

        adapter = await FulfillmentService.get_adapter(
            session, adapter_name=order.adapter_name, actor=operator
        )
        result = await adapter.invoke(
            "submit_refund",
            req=RefundRequest(
                order_id=int(order.id),
                platform_refund_no=row.platform_refund_no or "",
                purchase_order_no=(purchase.purchase_order_no if purchase else None),
                refund_amount_cents=amount,
                reason=reason or row.refund_reason or "",
            ),
        )

        if result is None or result.code not in {ResultCode.OK.value, ResultCode.DEGRADED.value}:
            message = result.message if result else "适配器无响应"
            logger.warning("submit_refund_unsupported", adapter=adapter.adapter_name, message=message)
            await AuditService.write(
                session,
                action_type=AuditActionType.ORDER_ACTION.value,
                object_type=AuditObjectType.ORDER.value,
                object_id=order.id,
                operator=operator,
                new_value={"adapter": adapter.adapter_name, "code": str(result.code) if result else "UNKNOWN"},
                trace_id=get_trace_id(),
                remark=f"提交退款失败：{message}",
            )
            await session.flush()
            raise BusinessError(message, code=ErrorCode.ORDER_REFUND_FAILED, http_status=503)

        data = result.data
        row.refund_1688_status = Refund1688Status.SUBMITTED.value
        row.refund_amount_cents = amount
        row.return_address_json = {
            **(row.return_address_json or {}),
            "refund_1688_no": getattr(data, "refund_1688_no", None) or "",
        }
        await session.flush()

        await AuditService.write(
            session,
            action_type=AuditActionType.ORDER_ACTION.value,
            object_type=AuditObjectType.ORDER.value,
            object_id=order.id,
            operator=operator,
            new_value={"refund_1688_no": getattr(data, "refund_1688_no", ""), "amount": cents_to_yuan_str(amount)},
            trace_id=get_trace_id(),
            remark="已提交 1688 退款",
        )
        await session.flush()
        return {
            "refund_1688_status": row.refund_1688_status,
            "refund_1688_no": getattr(data, "refund_1688_no", None) or "",
            "message": result.message or "",
        }

    # ==================================================================
    #  退货地址
    # ==================================================================

    @staticmethod
    async def fetch_return_address(
        session: Any,
        after_sale_id: int,
        *,
        operator: str = "system",
    ) -> dict[str, Any]:
        """★ 获取 1688 退货地址。

        本地兜底 UNSUPPORTED（含地址解密，已委托妙手 / 逸淘）→ 503 + 明确提示。
        """
        row = await AfterSaleService.get(session, after_sale_id)
        order = await AfterSaleService._order(session, row.order_id)
        purchase = (
            await session.execute(
                select(PurchaseOrder)
                .where(PurchaseOrder.order_id == int(order.id))
                .order_by(PurchaseOrder.id.desc())
                .limit(1)
            )
        ).scalars().first()

        adapter = await FulfillmentService.get_adapter(
            session, adapter_name=order.adapter_name, actor=operator
        )
        result = await adapter.invoke(
            "get_return_address",
            req=ReturnAddressQuery(
                purchase_order_no=(purchase.purchase_order_no if purchase else ""),
                order_id=int(order.id),
            ),
        )

        if result is None or result.code not in {ResultCode.OK.value, ResultCode.DEGRADED.value} or result.data is None:
            message = result.message if result else "适配器无响应"
            row.return_address_push_status = ReturnAddressPushStatus.FAILED.value
            await session.flush()
            raise BusinessError(message, code=ErrorCode.ORDER_RETURN_ADDRESS_FAILED, http_status=503)

        data = result.data
        payload = {
            "receiver_name_enc": data.receiver_name_enc,
            "phone_enc": data.phone_enc,
            "province": data.province,
            "city": data.city,
            "district": data.district,
            "detail": data.detail,
            "fetched_at": iso_utc(utc_now()),
        }
        row.return_address_json = payload
        row.return_address_push_status = ReturnAddressPushStatus.SUCCESS.value
        await session.flush()
        return {"return_address": payload, "push_status": row.return_address_push_status}

    # ==================================================================
    #  责任归属
    # ==================================================================

    @staticmethod
    async def update_responsibility(
        session: Any,
        after_sale_id: int,
        *,
        responsibility: str,
        note: str = "",
        operator: str = "system",
    ) -> AfterSale:
        """更新售后责任归属。"""
        allowed = {item.value for item in Responsibility}
        if responsibility not in allowed:
            raise BusinessError(
                f"责任归属只能是 {', '.join(sorted(allowed))}", code=ErrorCode.PARAM_ERROR
            )
        row = await AfterSaleService.get(session, after_sale_id)
        before = row.responsibility
        row.responsibility = responsibility
        await session.flush()

        await AuditService.write(
            session,
            action_type=AuditActionType.ORDER_ACTION.value,
            object_type=AuditObjectType.ORDER.value,
            object_id=row.order_id,
            operator=operator,
            old_value={"responsibility": before},
            new_value={"responsibility": responsibility, "note": note},
            trace_id=get_trace_id(),
            remark=f"售后责任归属：{before} → {responsibility}",
        )
        await session.flush()
        return row

    @staticmethod
    async def _order(session: Any, order_id: int) -> Order:
        """取订单（404）。"""
        order = (await session.execute(select(Order).where(Order.id == int(order_id)))).scalars().first()
        if order is None:
            raise NotFoundError(f"订单 {order_id} 不存在")
        return order
