"""库存与价格模块：InventorySnapshot / PriceSnapshot（§4.4.6）。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Index, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BaseMixin
from app.models.enums import InventorySource
from app.utils.kit import utc_now


class InventorySnapshot(BaseMixin, Base):
    """货源 SKU 库存快照（时间序列）。"""

    __tablename__ = "inventory_snapshot"

    source_sku_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="FK → source_sku.id")
    stock_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="库存数")
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default=InventorySource.ERP_POLL.value, comment="third_party_push/erp_poll"
    )
    collected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now, comment="采集时间")

    __table_args__ = (Index("ix_inventory_snapshot_sku_time", "source_sku_id", "collected_at"),)

    def __repr__(self) -> str:  # noqa: D105
        return f"<InventorySnapshot sku={self.source_sku_id} qty={self.stock_qty}>"


class PriceSnapshot(BaseMixin, Base):
    """货源 SKU 成本价快照与环比涨幅。"""

    __tablename__ = "price_snapshot"

    source_sku_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="FK → source_sku.id")
    cost_price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="成本价（分）")
    prev_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="上次价格（分）")
    change_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(6, 4), nullable=True, comment="环比涨幅，如 0.1234 = 12.34%"
    )
    collected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now, comment="采集时间")

    __table_args__ = (Index("ix_price_snapshot_sku_time", "source_sku_id", "collected_at"),)

    @property
    def change_rate_float(self) -> float:
        """涨幅的浮点表示（展示用）。"""
        return float(self.change_rate) if self.change_rate is not None else 0.0

    def __repr__(self) -> str:  # noqa: D105
        return f"<PriceSnapshot sku={self.source_sku_id} cost={self.cost_price_cents}>"


__all__ = ["InventorySnapshot", "PriceSnapshot"]
