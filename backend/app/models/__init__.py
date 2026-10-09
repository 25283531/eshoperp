"""SQLAlchemy 数据模型统一导出（24 张表）。

Alembic 通过 `Base.metadata` 自动发现全部表，任何新增模型必须在此导出，
否则不会进入迁移。
"""

from app.models.base import Base, BaseMixin, SoftDeleteMixin, TimestampMixin
from app.models.enums import *  # noqa: F401,F403  集中枚举定义
from app.models.source import SourceProduct, SourceSku, Supplier
from app.models.asset import AiTask, AiTaskResult, Asset
from app.models.listing import ListingProduct, ListingSku, PlatformAccount
from app.models.mapping import MappingChangeLog, MappingConflict, SkuMapping
from app.models.publish import PublishTask
from app.models.order import (
    AfterSale,
    FulfillmentAdapterConfig,
    Order,
    OrderItem,
    PurchaseOrder,
)
from app.models.inventory import InventorySnapshot, PriceSnapshot
from app.models.system import AuditLog, Credential, SystemSetting
from app.models.task import TaskRecord

__all__ = [
    # base
    "Base",
    "BaseMixin",
    "SoftDeleteMixin",
    "TimestampMixin",
    # source
    "Supplier",
    "SourceProduct",
    "SourceSku",
    # asset
    "Asset",
    "AiTask",
    "AiTaskResult",
    # listing
    "PlatformAccount",
    "ListingProduct",
    "ListingSku",
    # mapping
    "SkuMapping",
    "MappingConflict",
    "MappingChangeLog",
    # publish
    "PublishTask",
    # order & fulfillment
    "Order",
    "OrderItem",
    "PurchaseOrder",
    "AfterSale",
    "FulfillmentAdapterConfig",
    # inventory
    "InventorySnapshot",
    "PriceSnapshot",
    # system
    "SystemSetting",
    "AuditLog",
    "Credential",
    # task
    "TaskRecord",
]
