"""业务服务层（§10.7：所有写操作必须经 Service，禁止 API 层直接写表）。

分层纪律：
    * API 层（`app/api/v1/*`）只做参数校验与响应组装，**不得直接操作 ORM**；
    * Service 层负责业务规则、状态机迁移、审计埋点；
    * Adapter 层负责外部系统交互，Service 不感知 HTTP 细节。

★ 审计统一入口：所有 Service 通过 `AuditService.write()` 埋点，
  禁止各自 `session.add(AuditLog(...))`（§10.7 第 5 条）。
"""

from __future__ import annotations

from app.services.after_sale_service import AfterSaleService
from app.services.ai_task_service import AiTaskService
from app.services.asset_service import AssetService
from app.services.audit_service import AuditService
from app.services.fulfillment_service import FulfillmentService
from app.services.inventory_service import InventoryService
from app.services.listing_service import ListingService
from app.services.mapping_service import MappingService
from app.services.mapping_validator import MappingValidator
from app.services.order_service import OrderService
from app.services.publish_service import PublishService
from app.services.source_service import SourceService
from app.services.system_service import SystemService

__all__ = [
    "AfterSaleService",
    "AiTaskService",
    "AssetService",
    "AuditService",
    "FulfillmentService",
    "InventoryService",
    "ListingService",
    "MappingService",
    "MappingValidator",
    "OrderService",
    "PublishService",
    "SourceService",
    "SystemService",
]
