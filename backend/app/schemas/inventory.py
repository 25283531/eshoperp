"""库存与价格 Schema（§5.5.11）。"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.schemas.common import BaseSchema, cents_to_yuan, iso_or_none

__all__ = [
    "AutoOfflineRecordVo",
    "InventoryAlertVo",
    "InventoryConfigUpdate",
    "InventoryConfigVo",
    "InventorySnapshotVo",
    "InventorySyncRequest",
    "PriceSnapshotVo",
]


class InventorySnapshotVo(BaseSchema):
    """库存快照响应体。"""

    id: int = 0
    source_sku_id: int = 0
    source_sku_name: str | None = None
    source_product_title: str | None = None
    stock_qty: int = 0
    source: str = "erp_poll"
    collected_at: str | None = None

    @classmethod
    def from_model(
        cls,
        model: Any,
        *,
        source_sku_name: str | None = None,
        source_product_title: str | None = None,
    ) -> "InventorySnapshotVo":
        """从 ORM 对象构造。"""
        return cls(
            id=int(model.id or 0),
            source_sku_id=int(model.source_sku_id or 0),
            source_sku_name=source_sku_name,
            source_product_title=source_product_title,
            stock_qty=int(model.stock_qty or 0),
            source=model.source or "erp_poll",
            collected_at=iso_or_none(model.collected_at),
        )


class PriceSnapshotVo(BaseSchema):
    """成本价快照响应体。"""

    id: int = 0
    source_sku_id: int = 0
    source_sku_name: str | None = None
    source_product_title: str | None = None
    cost_price: str = ""
    prev_price: str = ""
    change_rate: str = ""
    collected_at: str | None = None

    @classmethod
    def from_model(
        cls,
        model: Any,
        *,
        source_sku_name: str | None = None,
        source_product_title: str | None = None,
    ) -> "PriceSnapshotVo":
        """从 ORM 对象构造。"""
        return cls(
            id=int(model.id or 0),
            source_sku_id=int(model.source_sku_id or 0),
            source_sku_name=source_sku_name,
            source_product_title=source_product_title,
            cost_price=cents_to_yuan(model.cost_price_cents),
            prev_price=cents_to_yuan(model.prev_price_cents),
            change_rate=f"{model.change_rate_float:.4f}" if model.change_rate is not None else "",
            collected_at=iso_or_none(model.collected_at),
        )


class InventoryAlertVo(BaseSchema):
    """库存 / 价格告警响应体（§5.5.11）。"""

    id: int = 0
    source_sku_id: int = 0
    source_sku_name: str | None = None
    source_product_title: str | None = None
    type: str = "out_of_stock"  # out_of_stock / price_increase
    current_stock: int | None = None
    current_cost: str = ""
    prev_cost: str = ""
    change_rate: str = ""
    threshold: str = ""
    suggested_action: str = "notify"  # offline / notify
    detected_at: str | None = None
    # ★ v1.14 §5.8：告知前端「这次告警的库存数据来自哪里」以及「系统会不会自己动手」
    #   两者都由 **`inventory_snapshot.source`** 推出（不是 `source_platform`），
    #   前端据此展示数据源标记 +「一键下架」入口（F11）。
    data_source: str = "unknown"  # erp_poll / third_party_push / manual_import / manual_edit / unknown
    auto_offline_allowed: bool = False  # False ⇒ 不自动执行，仅告警 + 人工一键下架


class AutoOfflineRecordVo(BaseSchema):
    """自动下架记录响应体。

    ★ 下架只能由 `InventoryService` 经 `ListingAdapter.offline()` 执行（红线 R2），
      本表只是执行结果的留痕。
    """

    id: int = 0
    listing_product_id: int = 0
    shop_item_id: str = ""
    platform: str = ""
    reason: str = ""
    trigger_type: str = ""
    executed_at: str | None = None
    success: bool = True
    message: str = ""


class InventoryConfigVo(BaseSchema):
    """库存配置响应体。"""

    poll_interval_min: int = 30
    price_increase_threshold: str = "0.10"
    out_of_stock_action: str = "offline"
    price_increase_action: str = "notify_only"


class InventoryConfigUpdate(BaseSchema):
    """更新库存配置（管理员）。"""

    poll_interval_min: int | None = None
    price_increase_threshold: str | None = None
    out_of_stock_action: str | None = Field(default=None, description="offline / notify_only")
    price_increase_action: str | None = Field(default=None, description="offline / notify_only")


class InventorySyncRequest(BaseSchema):
    """触发库存同步。"""

    source_sku_ids: list[int] = Field(default_factory=list)
    force: bool = False
