"""履约适配器抽象基类（架构核心，§5.2）。

★ ★ ★ 红 线 R2（写死在代码里，不得违反）★ ★ ★
================================================================================
本模块及其所有子类（miaoshou / yitao / local_csv ...）：

    1. **禁止 import `app.adapters.listing.*`（ListingAdapter 及其子类）**；
    2. **禁止持有 ListingAdapter 实例或任何"写店铺商品"的可调用对象**；
    3. **禁止实现 `offline()` / `update_stock_price()` / 任何商品编辑方法**。

理由：系统中不存在"第三方直写店铺商品"的代码路径（INV-P0-02）。
库存 / 价格变动只能走 `InventoryService → ListingAdapter.update_stock_price / offline`，
该链路的输入是 ERP 内部事件，不接受外部写入。违反此禁令 = 交付不合格。
================================================================================

三条铁律：
    1. **默认 UNSUPPORTED**：8 项能力基类有默认实现，返回 `code=UNSUPPORTED`；
    2. **绝不抛裸异常**：调度层只调 `invoke()`，异常统一转 FATAL / RETRYABLE 信封；
    3. **降级有路**：UNSUPPORTED 时由上层按 `manifest.fallback` 转 local_csv 或人工。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.adapters.fulfillment.manifest import (
    AdapterConfig,
    AdapterManifest,
    AdapterResult,
    Capability,
    CapabilityLevel,
    CredentialBundle,
    HealthStatus,
    normalize_capability,
)
from app.core.logging import get_logger, get_trace_id

logger = get_logger(__name__)

__all__ = [
    "Capability",
    "normalize_capability",
    "FetchOrdersRequest",
    "OrderPayload",
    "MatchSkuRequest",
    "MatchSkuResult",
    "PurchaseRequest",
    "PurchaseOrderPayload",
    "TrackingQuery",
    "TrackingPayload",
    "WriteBackRequest",
    "WriteBackResult",
    "RefundRequest",
    "RefundResult",
    "ReturnAddressQuery",
    "ReturnAddress",
    "InventoryChangeEvent",
    "FulfillmentAdapter",
]


# ---------------------------------------------------------------------------
#  请求 / 响应载荷（§5.2）
# ---------------------------------------------------------------------------


@dataclass
class FetchOrdersRequest:
    """拉取订单请求（增量）。"""

    shop_ids: list[str] = field(default_factory=list)
    updated_from: str = ""
    updated_to: str | None = None
    page: int = 1
    page_size: int = 100


@dataclass
class OrderPayload:
    """订单载荷（★ 密文字段全程不解密落日志）。"""

    platform: str
    shop_id: str
    platform_order_no: str  # 幂等去重键
    buyer_info_enc: str = ""
    receiver_addr_enc: str = ""
    total_amount_cents: int = 0
    paid_at: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] | None = None


@dataclass
class MatchSkuRequest:
    """SKU 匹配请求。"""

    platform: str
    shop_id: str
    shop_sku_code: str
    shop_item_id: str | None = None


@dataclass
class MatchSkuResult:
    """SKU 匹配结果。"""

    matched: bool
    source_product_1688_id: str | None = None
    source_sku_code_1688: str | None = None
    purchase_cost_cents: int | None = None
    mapping_status: str | None = None  # valid / pending_confirm / invalid
    reason: str | None = None  # 未匹配原因


@dataclass
class PurchaseRequest:
    """1688 采购下单请求。"""

    order_id: int
    platform_order_no: str
    source_product_1688_id: str
    source_sku_code_1688: str
    quantity: int
    receiver_enc: str = ""  # 密文收货信息（虚拟号脱敏）
    remark: str | None = None


@dataclass
class PurchaseOrderPayload:
    """采购单结果。"""

    purchase_order_no: str
    amount_cents: int
    status: str  # placed / failed / pending
    supplier_id: str | None = None
    raw: dict[str, Any] | None = None


@dataclass
class TrackingQuery:
    """物流单号查询。"""

    purchase_order_no: str
    order_id: int | None = None


@dataclass
class TrackingPayload:
    """物流单号结果。"""

    logistics_company: str
    tracking_no: str
    shipped_at: str | None = None


@dataclass
class WriteBackRequest:
    """物流回填请求。"""

    order_id: int
    platform_order_no: str
    shop_item_id: str
    logistics_company: str
    tracking_no: str


@dataclass
class WriteBackResult:
    """物流回填结果。"""

    success: bool
    message: str | None = None
    retryable: bool = False


@dataclass
class RefundRequest:
    """退款请求。"""

    order_id: int
    platform_refund_no: str
    purchase_order_no: str | None = None
    refund_amount_cents: int = 0
    reason: str = ""


@dataclass
class RefundResult:
    """退款结果。"""

    accepted: bool
    refund_1688_no: str | None = None
    message: str | None = None


@dataclass
class ReturnAddressQuery:
    """退货地址查询。"""

    purchase_order_no: str
    order_id: int


@dataclass
class ReturnAddress:
    """退货地址（收件人信息密文）。"""

    receiver_name_enc: str
    phone_enc: str
    province: str = ""
    city: str = ""
    district: str = ""
    detail: str = ""


@dataclass
class InventoryChangeEvent:
    """库存 / 价格变动事件（ERP 内部产生）。"""

    source_sku_code_1688: str
    stock_qty: int | None = None
    cost_cents: int | None = None
    change_type: str = "stock"  # stock / price / off_shelf


# ---------------------------------------------------------------------------
#  抽象基类
# ---------------------------------------------------------------------------


class FulfillmentAdapter(ABC):
    """履约适配器抽象基类。

    ★ 子类必须实现 `build_manifest()` 与 `health_check()`；
      8 项能力按需覆写，未覆写的走基类 UNSUPPORTED 默认实现。
    """

    adapter_name: str = ""  # "miaoshou" / "yitao" / "local_csv"
    display_name: str = ""
    manifest: AdapterManifest

    def __init__(
        self,
        config: AdapterConfig | None = None,
        credential: CredentialBundle | None = None,
        http: Any = None,
        session: Any = None,
    ) -> None:
        """初始化适配器（配置 / 凭证 / HTTP 客户端 / 数据库会话）。"""
        self.config = config or AdapterConfig(adapter_name=self.adapter_name, display_name=self.display_name)
        self.credential = credential
        self.http = http
        self.session = session
        self.manifest = self.build_manifest()

    # ---------------- 必须实现 ----------------

    @abstractmethod
    def build_manifest(self) -> AdapterManifest:
        """声明能力矩阵。"""
        raise NotImplementedError

    @abstractmethod
    async def health_check(self) -> AdapterResult[HealthStatus]:
        """连通性自检。"""
        raise NotImplementedError

    # ---------------- 8 项能力：基类默认 UNSUPPORTED ----------------

    async def fetch_orders(self, req: FetchOrdersRequest) -> AdapterResult[list[OrderPayload]]:
        """拉取订单。"""
        return AdapterResult.unsupported(Capability.FETCH_ORDERS.value, fallback=self._fallback(Capability.FETCH_ORDERS))

    async def match_sku(self, req: MatchSkuRequest) -> AdapterResult[MatchSkuResult]:
        """SKU 匹配。★ ERP 权威源始终是本地 sku_mapping，第三方结果仅作交叉校验。"""
        return AdapterResult.unsupported(Capability.MATCH_SKU.value, fallback=self._fallback(Capability.MATCH_SKU))

    async def place_purchase_order(self, req: PurchaseRequest) -> AdapterResult[PurchaseOrderPayload]:
        """1688 采购下单。"""
        return AdapterResult.unsupported(
            Capability.PLACE_PURCHASE_ORDER.value, fallback=self._fallback(Capability.PLACE_PURCHASE_ORDER)
        )

    async def fetch_tracking_no(self, req: TrackingQuery) -> AdapterResult[TrackingPayload]:
        """获取物流单号。"""
        return AdapterResult.unsupported(
            Capability.FETCH_TRACKING_NO.value, fallback=self._fallback(Capability.FETCH_TRACKING_NO)
        )

    async def write_back_tracking(self, req: WriteBackRequest) -> AdapterResult[WriteBackResult]:
        """回填物流单号到店铺。"""
        return AdapterResult.unsupported(
            Capability.WRITE_BACK_TRACKING.value, fallback=self._fallback(Capability.WRITE_BACK_TRACKING)
        )

    async def submit_refund(self, req: RefundRequest) -> AdapterResult[RefundResult]:
        """提交 1688 退款。"""
        return AdapterResult.unsupported(Capability.SUBMIT_REFUND.value, fallback=self._fallback(Capability.SUBMIT_REFUND))

    async def get_return_address(self, req: ReturnAddressQuery) -> AdapterResult[ReturnAddress]:
        """获取 1688 退货地址。"""
        return AdapterResult.unsupported(
            Capability.GET_RETURN_ADDRESS.value, fallback=self._fallback(Capability.GET_RETURN_ADDRESS)
        )

    async def push_inventory_change(self, req: InventoryChangeEvent) -> AdapterResult[dict]:
        """推送库存 / 价格变动。"""
        return AdapterResult.unsupported(
            Capability.PUSH_INVENTORY_CHANGE.value, fallback=self._fallback(Capability.PUSH_INVENTORY_CHANGE)
        )

    # ---------------- 统一入口：能力校验 + 异常兜底 ----------------

    async def invoke(self, capability: Capability | str, **kwargs: Any) -> AdapterResult[Any]:
        """★ 唯一推荐调用入口：先查能力声明，再执行，异常永不外泄。

        - 能力未声明或 `UNSUPPORTED` → 返回 UNSUPPORTED + fallback（不抛异常）；
        - 执行抛异常 → 转 `FATAL`（或按异常类型转 `RETRYABLE`）；
        - 传入未知能力名 → 返回 UNSUPPORTED **信封**（绝不抛 AttributeError）。

        Args:
            capability: `Capability` 枚举或其字符串值（如 `"fetch_orders"`）。
                ★ 入口处用 `normalize_capability()` 做唯一一次归一化，
                调用方传枚举 / 字符串都能工作（Bug 3 的修复点）。
        """
        cap = normalize_capability(capability)
        if cap is None:
            legal = ", ".join(item.value for item in Capability)
            logger.warning("adapter_unknown_capability", adapter=self.adapter_name, capability=str(capability))
            return AdapterResult.unsupported(
                str(capability),
                message=f"未知能力：{capability}（合法值：{legal}）",
            )

        spec = getattr(self, "manifest", None).capabilities.get(cap) if getattr(self, "manifest", None) else None
        if spec is None or spec.level == CapabilityLevel.UNSUPPORTED:
            return AdapterResult.unsupported(
                cap.value,
                fallback=spec.fallback if spec else None,
                message=spec.note if spec and spec.note else "",
            )

        started = time.perf_counter()
        try:
            result = await getattr(self, cap.value)(**kwargs)
        except Exception as exc:  # noqa: BLE001  ★ 绝不抛裸异常
            result = AdapterResult.fatal(method=cap.value, error=exc)
            result.fallback = spec.fallback
            logger.exception(
                "adapter_capability_error",
                adapter=self.adapter_name,
                capability=cap.value,
                error=str(exc),
            )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if isinstance(result, AdapterResult):
            result.elapsed_ms = elapsed_ms or result.elapsed_ms
            result.trace_id = result.trace_id or get_trace_id()
        return result

    # ---------------- 辅助 ----------------

    def _fallback(self, capability: Capability | str) -> str | None:
        """取能力降级目标（枚举 / 字符串入参均可）。"""
        if not getattr(self, "manifest", None):
            return None
        cap = normalize_capability(capability)
        if cap is None:
            return None
        spec = self.manifest.capabilities.get(cap)
        return spec.fallback if spec else None

    def capability_matrix(self) -> list[dict[str, Any]]:
        """返回能力矩阵（供 `GET /adapters/fulfillment/{name}/capabilities`）。"""
        return [spec.to_dict() for spec in self.manifest.capabilities.values()]

    def describe(self) -> dict[str, Any]:
        """适配器自述（manifest + 当前配置摘要）。"""
        return {
            "adapter_name": self.adapter_name,
            "display_name": self.display_name,
            "manifest": self.manifest.to_dict(),
            "config": {
                "base_url": self.config.base_url,
                "timeout_sec": self.config.timeout_sec,
                "max_retry": self.config.max_retry,
            },
        }

    def __repr__(self) -> str:  # noqa: D105
        return f"<{type(self).__name__} {self.adapter_name}>"
