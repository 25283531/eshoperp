"""平台与上架模块：PlatformAccount / ListingProduct / ListingSku（§4.4.3）。

★ 红线 R2：本模块的 `offline` / `update_stock_price` 能力只通过 `ListingAdapter` 暴露，
  任何 `FulfillmentAdapter` 子类不得 import 本模块的上架写入能力。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, String, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BaseMixin, JSONType, SoftDeleteMixin
from app.models.enums import (
    ListingMode,
    ListingProductStatus,
    ListingSkuStatus,
    PlatformAccountStatus,
)


class PlatformAccount(BaseMixin, Base):
    """平台店铺账号与授权 scope（Token / Secret 存 credential 表，不放本表）。"""

    __tablename__ = "platform_account"

    platform: Mapped[str] = mapped_column(String(32), nullable=False, comment="taobao/douyin/pdd/alibaba1688")
    shop_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="店铺 ID")
    shop_name: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="店铺名称")
    credential_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="FK → credential.id")
    granted_scopes_json: Mapped[list[Any]] = mapped_column(
        JSONType, nullable=False, default=list, comment="授权 scope 列表"
    )
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="过期时间")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PlatformAccountStatus.ACTIVE.value, comment="active/expired/revoked"
    )

    __table_args__ = (
        UniqueConstraint("platform", "shop_id", name="uq_platform_account_platform_shop"),
        Index("ix_platform_account_status", "status"),
    )

    def __repr__(self) -> str:  # noqa: D105
        return f"<PlatformAccount {self.platform}:{self.shop_id}>"


class ListingProduct(BaseMixin, SoftDeleteMixin, Base):
    """平台商品（Mock 数据带 is_mock=True，不参与真实履约）。"""

    __tablename__ = "listing_product"

    platform: Mapped[str] = mapped_column(String(32), nullable=False, comment="平台")
    shop_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="店铺 ID")
    shop_item_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="平台商品 ID")
    source_product_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="FK → source_product.id")
    title: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="上架标题")
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ListingProductStatus.ON_SALE.value,
        comment="on_sale/off_shelf/publishing/failed",
    )
    is_mock: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="Mock 标记（PRD LST-P0-02）"
    )
    listing_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ListingMode.MOCK.value, comment="real/mock/manual"
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="上架时间")
    offline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="下架时间")
    offline_reason: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="下架原因（INV-P0-03）")

    __table_args__ = (
        UniqueConstraint("platform", "shop_id", "shop_item_id", name="uq_listing_product_platform_shop_item"),
        Index("ix_listing_product_status", "status", "is_deleted"),
        Index("ix_listing_product_mock", "is_mock"),
    )

    def __repr__(self) -> str:  # noqa: D105
        return f"<ListingProduct {self.platform}:{self.shop_item_id}>"


class ListingSku(BaseMixin, SoftDeleteMixin, Base):
    """平台 SKU。"""

    __tablename__ = "listing_sku"

    listing_product_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="FK → listing_product.id")
    shop_sku_code: Mapped[str] = mapped_column(String(128), nullable=False, comment="平台 SKU 编码")
    spec_json: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True, comment="规格名值")
    sale_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="售价（分）")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ListingSkuStatus.ON_SALE.value, comment="on_sale/off_shelf"
    )

    __table_args__ = (
        UniqueConstraint("listing_product_id", "shop_sku_code", name="uq_listing_sku_product_code"),
        Index("ix_listing_sku_code", "shop_sku_code"),
    )

    def __repr__(self) -> str:  # noqa: D105
        return f"<ListingSku {self.shop_sku_code}>"


__all__ = ["PlatformAccount", "ListingProduct", "ListingSku"]
