"""适配器层（本项目架构核心，§1.1 / §5）。

    listing/       上架适配器（★ 只有它有 offline / update_stock_price —— 红线 R2）
    fulfillment/   履约适配器（8 项能力 + 能力声明 + scope 红线 —— 红线 R1）
    ai/            AI 重构客户端（mock / file_bridge / http）
    source/        货源采集适配器（1688）

铁律：
    R1 第三方永远不持有商品编辑权 —— 所有履约适配器实例化必经 scope_guard；
    R2 FulfillmentAdapter 及其子类禁止 import 或持有 ListingAdapter；
    R3 能力不可靠时业务不中断 —— UNSUPPORTED / DEGRADED 返回信封，绝不抛裸异常。
"""

from app.adapters.fulfillment.manifest import (
    AdapterConfig,
    AdapterManifest,
    AdapterResult,
    Capability,
    CapabilityLevel,
    CapabilitySpec,
    CredentialBundle,
    HealthStatus,
    ResultCode,
)

__version__ = "0.1.0"

__all__ = [
    "AdapterConfig",
    "AdapterManifest",
    "AdapterResult",
    "Capability",
    "CapabilityLevel",
    "CapabilitySpec",
    "CredentialBundle",
    "HealthStatus",
    "ResultCode",
]
