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

    # ★ 与本告警的 `source_sku_id` 关联、且**当前状态不是 `off_shelf`** 的平台商品 ID 列表。
    #   前端据此在告警页直接调已有的批量下架接口，不必再跑到平台商品页去找是哪个商品。
    #   由 `InventoryService._candidate_products_by_source_sku()` 一次批量预取后回填。
    #
    #   为什么是复数 `list[int]` 而不是单数：
    #       一个货源 SKU 可以同时供给多个店铺 / 多个平台。**跨平台铺货是正常业务** ——
    #       `models/mapping.py` 把它明确定位为「仅提示，永不拦截」（P1），且 `sku_mapping`
    #       上唯一的唯一索引 `uq_sku_mapping_shop_sku` 只约束「一店铺SKU → 一映射」，
    #       **不约束反向**（一货源SKU → 多映射完全合法）。用单数字段会把其余商品静默丢掉，
    #       运营点了下架、货还在别的店卖 —— 这是「发错货」方向的缺陷，比漏报更难自查。
    #
    #   为什么排除已下架（`off_shelf`）：
    #       `ListingService.offline()` 对已处于 `off_shelf` 的商品抛 `StateConflictError`
    #       → HTTP 409 / code 1005，且**不幂等**。把已下架商品放进列表，前端一点就报 409。
    #
    #   为什么不是「只保留 `on_sale`」：
    #       `offline()` 只拦 `off_shelf` 这一种状态，`publishing` / `failed` 都能正常下架。
    #       若这里只留 `on_sale`，会把「有货可下」误判成「没有」，运营就看不到下架入口 ——
    #       与告警本身「建议下架」的结论自相矛盾。故本字段的口径严格是「排除 off_shelf」。
    listing_product_ids: list[int] = Field(
        default_factory=list,
        description="与本告警关联、且当前未下架的平台商品 ID（可直接人工下架）",
    )


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
