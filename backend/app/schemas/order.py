"""订单与履约 / 售后 Schema（§5.5.9、§5.5.10）。

★ v1.4 成本三层语义 —— 快照层硬约束：
    `OrderItem.purchase_cost_cents` 是**下单时点固化**的快照，**永不可变**。
    历史订单利润**禁止 join 回 `sku_mapping` / `source_sku` 取成本**
    （附录 A 第 18 条）。本模块所有利润相关字段一律读 `order_item` 快照。
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.core.logging import mask_address, mask_name, mask_phone
from app.schemas.common import BaseSchema, cents_to_yuan, iso_or_none

__all__ = [
    "AfterSaleRefundRequest",
    "AfterSaleResponsibilityUpdate",
    "AfterSaleVo",
    "OrderActionRequest",
    "OrderDetailVo",
    "OrderExceptionVo",
    "OrderItemVo",
    "OrderMatchRequest",
    "OrderSyncRequest",
    "OrderVo",
    "PurchaseOrderTrackingRequest",
    "PurchaseOrderVo",
]


class OrderItemVo(BaseSchema):
    """订单明细响应体（成本为**不可变快照**）。"""

    id: int = 0
    order_id: int = 0
    platform_order_item_no: str | None = None
    shop_item_id: str = ""
    shop_sku_code: str = ""
    sku_mapping_id: int | None = None
    source_sku_code_1688: str | None = None
    quantity: int = 1
    purchase_cost: str = ""  # ★ 快照：下单时点固化，不随货源涨价变化
    sale_price: str = ""
    match_status: str = "unmatched"
    purchase_order_id: int | None = None

    @classmethod
    def from_model(cls, model: Any) -> "OrderItemVo":
        """从 ORM 对象构造。"""
        return cls(
            id=int(model.id or 0),
            order_id=int(model.order_id or 0),
            platform_order_item_no=model.platform_order_item_no,
            shop_item_id=model.shop_item_id or "",
            shop_sku_code=model.shop_sku_code or "",
            sku_mapping_id=model.sku_mapping_id,
            # ★ OrderItem 只固化 `sku_mapping_id` / `source_sku_id`（不冗余存 1688 编码），
            #   此处用 getattr 兜底，避免不同版本模型字段缺失导致属性异常。
            source_sku_code_1688=getattr(model, "source_sku_code_1688", None),
            quantity=int(model.quantity or 1),
            purchase_cost=cents_to_yuan(model.purchase_cost_cents),
            sale_price=cents_to_yuan(model.sale_price_cents),
            match_status=model.match_status or "unmatched",
            purchase_order_id=model.purchase_order_id,
        )


class OrderVo(BaseSchema):
    """订单列表响应体（§5.5.9）。买家 / 收货信息一律脱敏。"""

    id: int = 0
    platform: str = ""
    shop_id: str = ""
    platform_order_no: str = ""
    buyer_masked: str = ""
    receiver_masked: str = ""
    total_amount: str = ""
    paid_at: str | None = None
    fulfillment_status: str = "pending_match"
    adapter_name: str = "local_csv"
    match_status: str | None = None
    exception_type: str | None = None
    exception_note: str | None = None
    is_mock: bool = False
    item_count: int = 0
    created_at: str | None = None

    @classmethod
    def from_model(cls, model: Any, *, item_count: int = 0) -> "OrderVo":
        """从 ORM 对象构造（脱敏在调用方统一处理）。"""
        return cls(
            id=int(model.id or 0),
            platform=model.platform or "",
            shop_id=model.shop_id or "",
            platform_order_no=model.platform_order_no or "",
            buyer_masked=_mask_buyer(model),
            receiver_masked=_mask_receiver(model),
            total_amount=cents_to_yuan(model.total_amount_cents),
            paid_at=iso_or_none(model.paid_at),
            fulfillment_status=model.fulfillment_status or "pending_match",
            adapter_name=model.adapter_name or "local_csv",
            match_status=model.match_status,
            exception_type=model.exception_type,
            exception_note=model.exception_note,
            is_mock=bool(model.is_mock),
            item_count=int(item_count or 0),
            created_at=iso_or_none(model.created_at),
        )


class OrderDetailVo(OrderVo):
    """订单详情（含 items[] / purchase_orders[] / after_sales[]）。"""

    items: list[OrderItemVo] = Field(default_factory=list)
    purchase_orders: list[PurchaseOrderVo] = Field(default_factory=list)
    after_sales: list[AfterSaleVo] = Field(default_factory=list)
    handling_action: str | None = None
    handled_by: str | None = None
    handled_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_model(
        cls,
        model: Any,
        *,
        items: list[Any] | None = None,
        purchase_orders: list[Any] | None = None,
        after_sales: list[Any] | None = None,
    ) -> "OrderDetailVo":
        """从 ORM 对象构造。"""
        base = OrderVo.from_model(model, item_count=len(items or []))
        return cls(
            **base.model_dump(),
            items=[OrderItemVo.from_model(i) for i in (items or [])],
            purchase_orders=[PurchaseOrderVo.from_model(p) for p in (purchase_orders or [])],
            after_sales=[AfterSaleVo.from_model(a) for a in (after_sales or [])],
            handling_action=model.handling_action,
            handled_by=model.handled_by,
            handled_at=iso_or_none(model.handled_at),
            updated_at=iso_or_none(model.updated_at),
        )

    @property
    def profit_cents(self) -> int:
        """★ 订单利润（分）—— **只读 order_item 快照**，绝不 join 回 sku_mapping / source_sku。

        这是附录 A 第 18 条「历史订单利润不可回溯改写」的落地：
        货源涨价后镜像成本会变，但**历史订单利润必须仍按下单时点的成本计算**。
        """
        from app.schemas.common import parse_money

        total = 0
        for item in self.items:
            sale = parse_money(item.sale_price, field_name="售价")
            cost = parse_money(item.purchase_cost, field_name="成本")
            total += (sale - cost) * int(item.quantity or 1)
        return total

    @property
    def profit(self) -> str:
        """订单利润（元字符串，展示用）。"""
        return cents_to_yuan(self.profit_cents)


class OrderExceptionVo(BaseSchema):
    """订单异常响应体（`/orders/exceptions`）。"""

    id: int = 0
    order_id: int = 0
    platform_order_no: str = ""
    exception_type: str = ""
    exception_note: str | None = None
    handling_action: str | None = None
    handled: bool = False
    handled_by: str | None = None
    handled_at: str | None = None
    fulfillment_status: str = ""
    detected_at: str | None = None

    @classmethod
    def from_model(cls, model: Any) -> "OrderExceptionVo":
        """从 `erp_order` 构造（异常订单 = 有 exception_type 的订单）。"""
        return cls(
            id=int(model.id or 0),
            order_id=int(model.id or 0),
            platform_order_no=model.platform_order_no or "",
            exception_type=model.exception_type or "",
            exception_note=model.exception_note,
            handling_action=model.handling_action,
            handled=model.handling_action is not None,
            handled_by=model.handled_by,
            handled_at=iso_or_none(model.handled_at),
            fulfillment_status=model.fulfillment_status or "",
            detected_at=iso_or_none(model.updated_at),
        )


class PurchaseOrderVo(BaseSchema):
    """采购单响应体。"""

    id: int = 0
    order_id: int = 0
    purchase_order_no: str | None = None
    supplier_id: int | None = None
    amount: str = ""
    adapter_name: str | None = None
    purchase_status: str = "pending"
    logistics_company: str | None = None
    tracking_no: str | None = None
    shipped_at: str | None = None
    writeback_status: str = "pending"
    writeback_retry: int = 0
    created_at: str | None = None

    @classmethod
    def from_model(cls, model: Any) -> "PurchaseOrderVo":
        """从 ORM 对象构造。"""
        return cls(
            id=int(model.id or 0),
            order_id=int(model.order_id or 0),
            purchase_order_no=model.purchase_order_no,
            supplier_id=model.supplier_id,
            amount=cents_to_yuan(model.amount_cents),
            adapter_name=model.adapter_name,
            purchase_status=model.purchase_status or "pending",
            logistics_company=model.logistics_company,
            tracking_no=model.tracking_no,
            shipped_at=iso_or_none(model.shipped_at),
            writeback_status=model.writeback_status or "pending",
            writeback_retry=int(model.writeback_retry or 0),
            created_at=iso_or_none(model.created_at),
        )


class PurchaseOrderTrackingRequest(BaseSchema):
    """本地兜底手工录入物流单号。"""

    logistics_company: str = Field(..., min_length=1, max_length=64)
    tracking_no: str = Field(..., min_length=1, max_length=64)


class AfterSaleVo(BaseSchema):
    """售后响应体（§5.5.10）。"""

    id: int = 0
    order_id: int = 0
    platform_refund_no: str | None = None
    refund_reason: str | None = None
    refund_amount: str = ""
    refund_1688_status: str = "pending"
    refund_1688_no: str | None = None
    return_address_json: dict[str, Any] | None = None
    return_address_push_status: str = "pending"
    responsibility: str | None = None
    handling_status: str = "processing"
    created_at: str | None = None

    @classmethod
    def from_model(cls, model: Any) -> "AfterSaleVo":
        """从 ORM 对象构造。"""
        return cls(
            id=int(model.id or 0),
            order_id=int(model.order_id or 0),
            platform_refund_no=model.platform_refund_no,
            refund_reason=model.refund_reason,
            refund_amount=cents_to_yuan(model.refund_amount_cents),
            refund_1688_status=model.refund_1688_status or "pending",
            refund_1688_no=(model.return_address_json or {}).get("refund_1688_no")
            if isinstance(model.return_address_json, dict)
            else None,
            return_address_json=model.return_address_json,
            return_address_push_status=model.return_address_push_status or "pending",
            responsibility=model.responsibility,
            handling_status=model.handling_status or "processing",
            created_at=iso_or_none(model.created_at),
        )


class AfterSaleResponsibilityUpdate(BaseSchema):
    """更新售后责任归属。"""

    responsibility: str = Field(..., description="our_shop / supplier / buyer / platform")
    note: str | None = None


class OrderSyncRequest(BaseSchema):
    """拉取订单请求。"""

    adapter_name: str | None = None
    shop_ids: list[str] = Field(default_factory=list)
    force: bool = False


class OrderMatchRequest(BaseSchema):
    """手工指定货源 SKU 并补建映射。"""

    sku_mapping_id: int | None = None
    source_sku_id: int | None = None
    create_mapping: bool = False


class OrderActionRequest(BaseSchema):
    """订单处置动作。"""

    action: str = Field(..., description="retry / switch_source / refund / ignore")
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
#  脱敏（§10.8：姓名 / 手机 / 地址一律脱敏展示）
# ---------------------------------------------------------------------------


def _mask_buyer(model: Any) -> str:
    """买家信息脱敏（密文不可解时返回占位）。"""
    raw = model.buyer_info_enc or ""
    if not raw:
        return ""
    return mask_name(raw) if len(raw) < 24 else mask_phone(raw)


def _mask_receiver(model: Any) -> str:
    """收货地址脱敏。"""
    raw = model.receiver_addr_enc or ""
    if not raw:
        return ""
    return mask_address(raw)


class AfterSaleRefundRequest(BaseSchema):
    """提交 1688 退款（`POST /after-sales/{id}/submit-refund`）。"""

    refund_amount: str | float | int = Field(..., description="退款金额（元）")
    reason: str | None = None
