"""订单与履约模块（§4.4.5）：
Order / OrderItem / PurchaseOrder / AfterSale / FulfillmentAdapterConfig。

★ `Order.adapter_name` 在订单创建时固化 —— 切换适配器后在途订单仍按原渠道跑完（ADR-6）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BaseMixin, JSONType
from app.models.enums import (
    AfterSaleHandlingStatus,
    AdapterName,
    HealthStatusValue,
    MatchStatus,
    OrderFulfillmentStatus,
    ORDER_TRANSITIONS,
    PurchaseStatus,
    Refund1688Status,
    ReturnAddressPushStatus,
    ScopeCheckStatus,
    WriteBackStatus,
)


class Order(BaseMixin, Base):
    """店铺订单（表名 `erp_order`，避 SQL 保留字 `order`）。"""

    __tablename__ = "erp_order"

    platform: Mapped[str] = mapped_column(String(32), nullable=False, comment="平台")
    shop_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="店铺 ID")
    platform_order_no: Mapped[str] = mapped_column(String(64), nullable=False, comment="平台订单号")
    buyer_info_enc: Mapped[str | None] = mapped_column(Text, nullable=True, comment="买家信息密文")
    receiver_addr_enc: Mapped[str | None] = mapped_column(Text, nullable=True, comment="收货地址密文")
    receiver_name_enc: Mapped[str | None] = mapped_column(Text, nullable=True, comment="收件人姓名密文")
    receiver_phone_enc: Mapped[str | None] = mapped_column(Text, nullable=True, comment="收件人手机号密文")
    total_amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="订单金额（分）")
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="下单/付款时间")
    fulfillment_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=OrderFulfillmentStatus.PENDING_MATCH.value,
        comment="履约状态，见 §7.2",
    )
    adapter_name: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=AdapterName.LOCAL_CSV.value,
        comment="★ 订单创建时固化的履约适配器（切换后在途订单按原渠道跑完）",
    )
    match_status: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="matched/unmatched/pending_confirm"
    )
    exception_type: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="异常类型")
    exception_note: Mapped[str | None] = mapped_column(Text, nullable=True, comment="异常说明")
    handling_action: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="retry/switch_source/refund/ignore"
    )
    handled_by: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="处置人")
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="处置时间")
    is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="Mock 数据标记")

    __table_args__ = (
        # 幂等去重（ORD-P0-01）
        UniqueConstraint("platform", "shop_id", "platform_order_no", name="uq_erp_order_platform_no"),
        Index("ix_erp_order_status", "fulfillment_status", "created_at"),
        Index("ix_erp_order_adapter", "adapter_name", "fulfillment_status"),
        Index("ix_erp_order_match", "match_status", "is_mock"),
    )

    def can_transition_to(self, target: str) -> bool:
        """校验订单状态机迁移是否合法（§7.2）。"""
        return target in ORDER_TRANSITIONS.get(self.fulfillment_status, [])

    def transition_to(self, target: str) -> str:
        """执行状态迁移，非法迁移抛 ValueError（由上层转 6002）。"""
        if not self.can_transition_to(target):
            raise ValueError(f"非法的订单状态迁移：{self.fulfillment_status} → {target}")
        self.fulfillment_status = target
        return self.fulfillment_status

    @property
    def is_terminal(self) -> bool:
        """是否终态。"""
        from app.models.enums import TERMINAL_ORDER_STATUSES

        return self.fulfillment_status in TERMINAL_ORDER_STATUSES

    def __repr__(self) -> str:  # noqa: D105
        return f"<Order {self.platform_order_no} {self.fulfillment_status}>"


class OrderItem(BaseMixin, Base):
    """订单明细与匹配状态。"""

    __tablename__ = "order_item"

    order_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="FK → erp_order.id")
    platform_order_item_no: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="平台子订单号")
    shop_item_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="平台商品 ID")
    shop_sku_code: Mapped[str] = mapped_column(String(128), nullable=False, comment="平台 SKU 编码")
    sku_mapping_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="匹配到的映射")
    source_sku_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="匹配到的 1688 货源 SKU")
    # ★★ 本列由 E2E 主链路用例（`tests/test_e2e_http_main_path.py`）暴露缺失 ★★
    #    `order_service` 有三处写入（`match_order` / `manual_match` / `handle_action.switch_source`）
    #    与两处读取（`place_purchase` 构造 `PurchaseRequest`），但模型里**从来没有这一列**：
    #      * 写入 → SQLAlchemy 只是挂了个实例属性，**不落库**（静默丢数据，不报错）；
    #      * 读取 → `AttributeError` ⇒ `place_purchase` 必然 500。
    #    因为 `place_purchase` 全仓**没有任何 HTTP / 定时任务入口**（从未被真正调用过），
    #    这个错误一直潜伏着 —— 典型的「写了但没跑过」。
    source_sku_code_1688: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="匹配到的 1688 SKU 编码（冗余，采购下单与导出用）"
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="数量")
    purchase_cost_cents: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="采购成本（分）")
    sale_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="售价（分）")
    match_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=MatchStatus.UNMATCHED.value, comment="matched/unmatched/pending_confirm"
    )
    purchase_order_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="关联采购单")

    __table_args__ = (
        Index("ix_order_item_order", "order_id"),
        Index("ix_order_item_sku", "shop_item_id", "shop_sku_code"),
        Index("ix_order_item_mapping", "sku_mapping_id"),
    )

    def __repr__(self) -> str:  # noqa: D105
        return f"<OrderItem {self.shop_sku_code} x{self.quantity}>"


class PurchaseOrder(BaseMixin, Base):
    """1688 采购单。"""

    __tablename__ = "purchase_order"

    order_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="FK → erp_order.id")
    purchase_order_no: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="1688 采购单号")
    supplier_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="FK → supplier.id")
    amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="金额（分）")
    adapter_name: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="下单使用的适配器")
    purchase_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PurchaseStatus.PENDING.value, comment="pending/placed/failed/cancelled"
    )
    logistics_company: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="物流公司")
    tracking_no: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="物流单号")
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="发货时间")
    writeback_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=WriteBackStatus.PENDING.value, comment="pending/success/failed"
    )
    writeback_retry: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="回填重试次数")
    raw_payload_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONType, nullable=True, comment="第三方响应存档"
    )

    __table_args__ = (
        Index("ix_purchase_order_order", "order_id"),
        Index("ix_purchase_order_no", "purchase_order_no"),
        Index("ix_purchase_order_status", "purchase_status", "writeback_status"),
    )

    def __repr__(self) -> str:  # noqa: D105
        return f"<PurchaseOrder {self.purchase_order_no} {self.purchase_status}>"


class AfterSale(BaseMixin, Base):
    """售后退款单。"""

    __tablename__ = "after_sale"

    order_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="FK → erp_order.id")
    platform_refund_no: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="平台退款单号")
    refund_reason: Mapped[str | None] = mapped_column(Text, nullable=True, comment="退款原因")
    refund_amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="退款金额（分）")
    refund_1688_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=Refund1688Status.PENDING.value, comment="pending/submitted/success/failed"
    )
    return_address_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONType, nullable=True, comment="1688 退货地址"
    )
    return_address_push_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ReturnAddressPushStatus.PENDING.value,
        comment="pending/success/failed",
    )
    responsibility: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="our_shop/supplier/buyer/platform"
    )
    handling_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AfterSaleHandlingStatus.PROCESSING.value, comment="processing/done/closed"
    )
    evidence_json: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True, comment="举证材料（P2 预留）")

    __table_args__ = (
        Index("ix_after_sale_order", "order_id"),
        Index("ix_after_sale_status", "handling_status", "refund_1688_status"),
    )

    def __repr__(self) -> str:  # noqa: D105
        return f"<AfterSale order={self.order_id} {self.handling_status}>"


class FulfillmentAdapterConfig(BaseMixin, Base):
    """履约适配器配置与能力声明（PRD FUL-P0-05）。"""

    __tablename__ = "fulfillment_adapter"

    adapter_name: Mapped[str] = mapped_column(
        String(32), nullable=False, unique=True, comment="miaoshou/yitao/local_csv"
    )
    display_name: Mapped[str] = mapped_column(String(64), nullable=False, comment="妙手/逸淘/本地兜底")
    credential_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="FK → credential.id")
    capability_json: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, default=dict, comment="能力声明 manifest 持久化副本"
    )
    declared_scopes_json: Mapped[list[Any] | None] = mapped_column(
        JSONType, nullable=True, comment="适配器声明的 scope（送 scope_guard 校验）"
    )
    scope_check_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ScopeCheckStatus.PASSED.value, comment="passed/rejected"
    )
    scope_check_message: Mapped[str | None] = mapped_column(Text, nullable=True, comment="越权原因")
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否启用")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否当前生效（唯一）")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=10, comment="优先级（灰度分流 P2）")
    health_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=HealthStatusValue.UNKNOWN.value, comment="healthy/degraded/down/unknown"
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="最近心跳时间")
    last_heartbeat_msg: Mapped[str | None] = mapped_column(Text, nullable=True, comment="心跳消息")
    heartbeat_fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="连续失败次数")
    config_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONType, nullable=True, comment="适配器私有配置（端点覆盖、CSV 路径等）"
    )

    __table_args__ = (
        Index("ix_fulfillment_adapter_active", "is_active", "is_enabled"),
        Index("ix_fulfillment_adapter_health", "health_status"),
    )

    def __repr__(self) -> str:  # noqa: D105
        return f"<FulfillmentAdapterConfig {self.adapter_name} active={self.is_active}>"


__all__ = ["Order", "OrderItem", "PurchaseOrder", "AfterSale", "FulfillmentAdapterConfig"]
