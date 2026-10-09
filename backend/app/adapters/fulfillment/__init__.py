"""履约适配器包（架构核心）。

★ 红线提示：
    R1 本包内所有适配器的实例化**必须**走 `factory.FulfillmentAdapterFactory.create()`，
       该方法内强制调用 `scope_guard.enforce_scope()`；
    R2 本包内**禁止** import `app.adapters.listing.*`，禁止实现 `offline` / `update_stock_price`。
"""

from app.adapters.fulfillment.base import (
    Capability,
    FetchOrdersRequest,
    FulfillmentAdapter,
    InventoryChangeEvent,
    MatchSkuRequest,
    MatchSkuResult,
    OrderPayload,
    PurchaseOrderPayload,
    PurchaseRequest,
    RefundRequest,
    RefundResult,
    ReturnAddress,
    ReturnAddressQuery,
    TrackingPayload,
    TrackingQuery,
    WriteBackRequest,
    WriteBackResult,
)
from app.adapters.fulfillment.factory import (
    DEFAULT_ADAPTER,
    FulfillmentAdapterFactory,
    get_active_adapter_name,
    get_fulfillment_adapter,
)
from app.adapters.fulfillment.manifest import (
    AdapterConfig,
    AdapterManifest,
    AdapterResult,
    CapabilityLevel,
    CapabilitySpec,
    CredentialBundle,
    HealthStatus,
    ResultCode,
    normalize_capability,
)
from app.adapters.fulfillment.registry import (
    ADAPTER_REGISTRY,
    get_adapter_class,
    list_adapter_names,
    list_adapters,
    register,
)
from app.adapters.fulfillment.scope_guard import (
    ALLOWED_SCOPES,
    FORBIDDEN_SCOPES,
    ScopeViolationError,
    check_scope,
    enforce_scope,
    enforce_scope_with_audit,
    scope_policies,
)

__all__ = [
    "ADAPTER_REGISTRY",
    "ALLOWED_SCOPES",
    "AdapterConfig",
    "AdapterManifest",
    "AdapterResult",
    "Capability",
    "CapabilityLevel",
    "CapabilitySpec",
    "CredentialBundle",
    "DEFAULT_ADAPTER",
    "FORBIDDEN_SCOPES",
    "FetchOrdersRequest",
    "FulfillmentAdapter",
    "FulfillmentAdapterFactory",
    "HealthStatus",
    "InventoryChangeEvent",
    "MatchSkuRequest",
    "MatchSkuResult",
    "OrderPayload",
    "PurchaseOrderPayload",
    "PurchaseRequest",
    "RefundRequest",
    "RefundResult",
    "ResultCode",
    "ReturnAddress",
    "ReturnAddressQuery",
    "ScopeViolationError",
    "TrackingPayload",
    "TrackingQuery",
    "WriteBackRequest",
    "WriteBackResult",
    "check_scope",
    "enforce_scope",
    "enforce_scope_with_audit",
    "get_active_adapter_name",
    "get_adapter_class",
    "get_fulfillment_adapter",
    "list_adapter_names",
    "list_adapters",
    "normalize_capability",
    "register",
    "scope_policies",
]
