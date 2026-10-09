"""货源模块：Supplier / SourceProduct / SourceSku（§4.4.1）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BaseMixin, JSONType, SoftDeleteMixin
from app.models.enums import SourceStatus, SupplierStatus
from app.utils.kit import utc_now


class Supplier(BaseMixin, SoftDeleteMixin, Base):
    """1688 供应商。"""

    __tablename__ = "supplier"

    supplier_1688_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, comment="1688 供应商 ID")
    name: Mapped[str] = mapped_column(String(255), nullable=False, comment="供应商名称")
    location: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="所在地")
    lead_time_hours: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="发货时效（小时）")
    moq: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="起订量")
    cooperation_score: Mapped[float | None] = mapped_column(
        Integer, nullable=True, comment="合作评分 ×100（0–500，避免浮点）"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SupplierStatus.ACTIVE.value, comment="active/inactive/blacklist"
    )
    contact_enc: Mapped[str | None] = mapped_column(Text, nullable=True, comment="联系方式（AES-256 加密）")

    __table_args__ = (
        Index("ix_supplier_status", "status", "is_deleted"),
        Index("ix_supplier_name", "name"),
    )

    def __repr__(self) -> str:  # noqa: D105
        return f"<Supplier {self.supplier_1688_id} {self.name}>"


class SourceProduct(BaseMixin, SoftDeleteMixin, Base):
    """1688 货源商品。"""

    __tablename__ = "source_product"

    product_1688_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, comment="1688 商品 ID")
    title: Mapped[str] = mapped_column(String(512), nullable=False, comment="标题")
    category_path: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="类目路径")
    supplier_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="FK → supplier.id")
    cost_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="成本价（分）")
    origin_url: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="原始 URL")
    main_image_url: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="主图 URL")
    params_json: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True, comment="参数表")
    collected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=utc_now, comment="采集时间")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SourceStatus.ON_SALE.value, comment="on_sale/off_shelf/out_of_stock"
    )
    stock_status: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="库存状态快照")
    raw_payload_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONType, nullable=True, comment="原始 API 响应存档（便于字段补全回溯）"
    )

    __table_args__ = (
        Index("ix_source_product_supplier", "supplier_id", "is_deleted"),
        Index("ix_source_product_status", "status", "is_deleted"),
    )

    def __repr__(self) -> str:  # noqa: D105
        return f"<SourceProduct {self.product_1688_id}>"


class SourceSku(BaseMixin, SoftDeleteMixin, Base):
    """1688 货源 SKU（含规格指纹，映射变更检测依据）。"""

    __tablename__ = "source_sku"

    source_product_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="FK → source_product.id")
    sku_code_1688: Mapped[str] = mapped_column(String(128), nullable=False, comment="1688 SKU 编码")
    spec_json: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, default=dict, comment='规格名值组合，如 {"颜色":"红","尺码":"XL"}'
    )
    spec_signature: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", comment="规格指纹：spec_json 排序后 md5"
    )
    cost_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="成本价（分）")
    stock_qty: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="库存")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SourceStatus.ON_SALE.value, comment="on_sale/off_shelf/out_of_stock"
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="最近检测时间")

    __table_args__ = (
        UniqueConstraint("source_product_id", "sku_code_1688", name="uq_source_sku_product_code"),
        Index("ix_source_sku_signature", "spec_signature"),
        Index("ix_source_sku_status", "status", "is_deleted"),
    )

    def __repr__(self) -> str:  # noqa: D105
        return f"<SourceSku {self.sku_code_1688}>"


__all__ = ["Supplier", "SourceProduct", "SourceSku"]
