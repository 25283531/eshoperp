"""MiaoshouAdapter —— 妙手分销工具履约适配器（profile 驱动 + 能力降级）。

★ 真实 HTTP 调用未实测（PRD Q2）：
    * 端点 / 请求 / 响应字段全部外置到 `profiles/miaoshou.yaml`，不确定处标 TODO；
    * 代码中对 TODO 字段做防御性解析（`pick()` 缺失返回默认，绝不崩溃）；
    * 端点未配置或 base_url 为空时，能力自动退化为 UNSUPPORTED → 由上层按 fallback 降级。

能力矩阵（MVP 基线，可被后台配置覆盖）：
    fetch_orders / place_purchase_order / fetch_tracking_no / write_back_tracking /
    submit_refund / get_return_address / push_inventory_change  = SUPPORTED（⚠TODO 待实测）
    match_sku = UNSUPPORTED（★ 映射权威源始终在自研侧本地 sku_mapping）
"""

from __future__ import annotations

import time
from typing import Any

from app.adapters.fulfillment.base import (
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
from app.adapters.fulfillment.http_client import (
    HttpClient,
    HttpRequestSpec,
    build_spec,
    endpoint_field_map,
    load_profile,
    pick,
    profile_capabilities,
    profile_required_scopes,
)
from app.adapters.fulfillment.manifest import (
    AdapterConfig,
    AdapterManifest,
    AdapterResult,
    Capability,
    CapabilityLevel,
    CapabilitySpec,
    HealthStatus,
)
from app.adapters.fulfillment.registry import register
from app.core.logging import get_logger
from app.models.enums import AdapterName, Capability as CapabilityEnum

logger = get_logger(__name__)

__all__ = ["MiaoshouAdapter", "ProfileDrivenMixin"]

PROFILE_NAME = "miaoshou"


class ProfileDrivenMixin:
    """profile 驱动的公共能力：加载 YAML、构建 spec、安全取值。

    ★ 该类只做"配置驱动的 HTTP 调用"，不涉及任何商品编辑能力（红线 R2）。
    """

    profile_name: str = ""

    def load(self) -> dict[str, Any]:
        """加载（并缓存）profile。"""
        return load_profile(self.profile_name)

    def spec_of(self, capability: str) -> HttpRequestSpec | None:
        """取能力对应的请求规格；未配置返回 None。"""
        return build_spec(self.load(), capability)

    def has_endpoint(self, capability: str) -> bool:
        """是否已配置端点。"""
        return self.spec_of(capability) is not None

    def field_map(self, capability: str, section: str) -> dict[str, Any]:
        """取端点内的字段映射段（如 `order_mapping`）。"""
        return endpoint_field_map(self.load(), capability, section)

    def manifest_from_profile(self, *, adapter_name: str, display_name: str, version: str) -> AdapterManifest:
        """按 profile 的 `capabilities` 段生成能力矩阵（缺省 UNSUPPORTED）。"""
        profile = self.load()
        declared = profile_capabilities(profile)
        capabilities: dict[CapabilityEnum, CapabilitySpec] = {}
        for capability in CapabilityEnum:
            raw = declared.get(capability.value)
            if isinstance(raw, dict):
                try:
                    level = CapabilityLevel(str(raw.get("level", CapabilityLevel.UNSUPPORTED.value)))
                except ValueError:
                    level = CapabilityLevel.UNSUPPORTED
                capabilities[capability] = CapabilitySpec(
                    name=capability,
                    level=level,
                    fallback=raw.get("fallback"),
                    note=str(raw.get("note", "")),
                    unverified=bool(raw.get("unverified", False)),
                )
            else:
                capabilities[capability] = CapabilitySpec(
                    name=capability,
                    level=CapabilityLevel.UNSUPPORTED,
                    fallback="local_csv",
                    note="profile 未声明该能力",
                )
        return AdapterManifest(
            adapter_name=adapter_name,
            display_name=display_name,
            version=version,
            capabilities=capabilities,
            required_scopes=profile_required_scopes(profile),
            config_schema={
                "type": "object",
                "properties": {
                    "base_url": {"type": "string", "title": "接口根地址"},
                    "app_key": {"type": "string", "title": "AppKey"},
                    "app_secret": {"type": "string", "title": "AppSecret"},
                },
            },
            docs_url=profile.get("docs_url") or None,
        )

    async def call(self, capability: str, context: dict[str, Any], *, trace_id: str = "") -> Any:
        """按能力名执行 profile 驱动的调用。

        Returns:
            `HttpResult`。端点未配置 / base_url 为空时返回 `ok=False` 的 HttpResult。
        """
        spec = self.spec_of(capability)
        if spec is None:
            from app.adapters.fulfillment.http_client import HttpResult

            return HttpResult(ok=False, error=f"profile 未配置端点：{capability}", retryable=False)
        return await self.http.call_spec(spec, context=context, trace_id=trace_id)  # type: ignore[attr-defined]


@register(AdapterName.MIAOSHOU.value, display_name="妙手")
class MiaoshouAdapter(ProfileDrivenMixin, FulfillmentAdapter):
    """妙手履约适配器。

    ★ 红线 R2：本类**没有**也不允许有 `offline` / `update_stock_price` 方法。
    """

    adapter_name = AdapterName.MIAOSHOU.value
    display_name = "妙手"
    profile_name = PROFILE_NAME

    def __init__(self, config: Any = None, credential: Any = None, http: Any = None, session: Any = None) -> None:
        """初始化妙手适配器（HTTP 客户端按 profile + 后台配置覆盖构建）。"""
        profile = load_profile(PROFILE_NAME)
        merged: dict[str, Any] = dict(profile)
        if config is not None:
            if isinstance(config, AdapterConfig):
                merged.update(
                    {
                        "base_url": config.base_url or profile.get("base_url", ""),
                        "timeout_sec": config.timeout_sec,
                        "max_retry": config.max_retry,
                        "retry_backoff_sec": config.retry_backoff_sec,
                        "verify_ssl": config.verify_ssl,
                        "auth": config.auth or profile.get("auth", {}),
                        "endpoints": config.endpoints or profile.get("endpoints", {}),
                    }
                )
            elif isinstance(config, dict):
                merged.update(config)

        adapter_config = AdapterConfig.from_dict(merged, adapter_name=self.adapter_name, display_name=self.display_name)
        if http is None:
            http = HttpClient(
                base_url=str(merged.get("base_url", "")),
                timeout_sec=float(merged.get("timeout_sec", 20.0)),
                max_retry=int(merged.get("max_retry", 3)),
                retry_backoff_sec=float(merged.get("retry_backoff_sec", 0.5)),
                verify_ssl=bool(merged.get("verify_ssl", True)),
                auth=dict(merged.get("auth", {})),
                credential=credential,
            )
        super().__init__(config=adapter_config, credential=credential, http=http, session=session)

    # ---------------- 能力声明 ----------------

    def build_manifest(self) -> AdapterManifest:
        """按 profile 生成能力矩阵（⚠TODO 字段标记 unverified）。"""
        return self.manifest_from_profile(
            adapter_name=self.adapter_name,
            display_name=self.display_name,
            version=str(self.load().get("version", "0.1.0-todo")),
        )

    # ---------------- 8 项能力 ----------------

    async def fetch_orders(self, req: FetchOrdersRequest) -> AdapterResult[list[OrderPayload]]:
        """拉取订单（SUPPORTED ⚠TODO）。端点未配置 → UNSUPPORTED 降级。"""
        if not self.has_endpoint(Capability.FETCH_ORDERS.value):
            return AdapterResult.unsupported(
                Capability.FETCH_ORDERS.value, fallback="manual", message="妙手未配置订单端点（待实测）"
            )
        result = await self.call(
            Capability.FETCH_ORDERS.value,
            {
                "shop_ids": ",".join(req.shop_ids),
                "updated_from": req.updated_from,
                "updated_to": req.updated_to or "",
                "page": req.page,
                "page_size": req.page_size,
            },
        )
        if not result.ok:
            return AdapterResult.retryable(Capability.FETCH_ORDERS.value, result.error)

        mapping = self.field_map(Capability.FETCH_ORDERS.value, "order_mapping")
        raw_orders = pick(result.raw, "data.list") or pick(result.raw, "data.rows") or []
        if not isinstance(raw_orders, list):
            raw_orders = []

        orders: list[OrderPayload] = []
        for raw in raw_orders:
            items_raw = pick(raw, str(mapping.get("items", "items")), []) or []
            items: list[dict[str, Any]] = []
            if isinstance(items_raw, list):
                for item in items_raw:
                    items.append(
                        {
                            "shop_item_id": str(pick(item, str(mapping.get("item_shop_item_id", "outer_item_id")), "") or ""),
                            "shop_sku_code": str(pick(item, str(mapping.get("item_sku_code", "outer_sku_id")), "") or ""),
                            "quantity": int(pick(item, "quantity", 1) or 1),
                            "price_cents": int(pick(item, "price", 0) or 0),
                        }
                    )
            orders.append(
                OrderPayload(
                    platform="",  # 由上层按 shop_id 归属补齐
                    shop_id=str(pick(raw, str(mapping.get("shop_id", "shop_id")), "") or ""),
                    platform_order_no=str(
                        pick(raw, str(mapping.get("platform_order_no", "order_no")), "") or ""
                    ),
                    buyer_info_enc=str(pick(raw, str(mapping.get("buyer_info_enc", "buyer_nick")), "") or ""),
                    receiver_addr_enc=str(
                        pick(raw, str(mapping.get("receiver_addr_enc", "receiver_address")), "") or ""
                    ),
                    total_amount_cents=int(pick(raw, str(mapping.get("total_amount_cents", "pay_amount")), 0) or 0),
                    paid_at=str(pick(raw, str(mapping.get("paid_at", "pay_time")), "") or ""),
                    items=items,
                    raw=raw if isinstance(raw, dict) else {"value": raw},
                )
            )
        return AdapterResult.success(orders, message=f"妙手返回 {len(orders)} 条订单")

    async def match_sku(self, req: MatchSkuRequest) -> AdapterResult[MatchSkuResult]:
        """★ 恒 UNSUPPORTED：映射权威源在自研侧，第三方不参与匹配。"""
        return AdapterResult.unsupported(
            Capability.MATCH_SKU.value,
            fallback=None,
            message="SKU 匹配的权威源是 ERP 本地 sku_mapping，第三方能力不参与（架构决定）",
        )

    async def place_purchase_order(self, req: PurchaseRequest) -> AdapterResult[PurchaseOrderPayload]:
        """1688 采购下单（SUPPORTED ⚠TODO）。"""
        if not self.has_endpoint(Capability.PLACE_PURCHASE_ORDER.value):
            return AdapterResult.unsupported(
                Capability.PLACE_PURCHASE_ORDER.value, fallback="csv_export", message="妙手未配置下单端点（待实测）"
            )
        result = await self.call(
            Capability.PLACE_PURCHASE_ORDER.value,
            {
                "platform_order_no": req.platform_order_no,
                "source_product_1688_id": req.source_product_1688_id,
                "source_sku_code_1688": req.source_sku_code_1688,
                "quantity": req.quantity,
                "receiver_enc": req.receiver_enc,
                "remark": req.remark or "",
            },
        )
        if not result.ok:
            return AdapterResult.retryable(Capability.PLACE_PURCHASE_ORDER.value, result.error)

        data = result.data if isinstance(result.data, dict) else {}
        return AdapterResult.success(
            PurchaseOrderPayload(
                purchase_order_no=str(pick(data, "purchase_order_no", "") or ""),
                amount_cents=int(pick(data, "amount_cents", 0) or 0),
                status=str(pick(data, "status", "pending") or "pending"),
                supplier_id=pick(data, "supplier_id"),
                raw=result.raw if isinstance(result.raw, dict) else {},
            ),
            message="妙手下单返回（字段未实测，已做防御性解析）",
        )

    async def fetch_tracking_no(self, req: TrackingQuery) -> AdapterResult[TrackingPayload]:
        """获取物流单号（SUPPORTED ⚠TODO）。"""
        if not self.has_endpoint(Capability.FETCH_TRACKING_NO.value):
            return AdapterResult.unsupported(
                Capability.FETCH_TRACKING_NO.value, fallback="manual", message="妙手未配置物流查询端点（待实测）"
            )
        result = await self.call(
            Capability.FETCH_TRACKING_NO.value, {"purchase_order_no": req.purchase_order_no}
        )
        if not result.ok:
            return AdapterResult.retryable(Capability.FETCH_TRACKING_NO.value, result.error)
        data = result.data if isinstance(result.data, dict) else {}
        tracking_no = str(pick(data, "tracking_no", "") or "")
        if not tracking_no:
            return AdapterResult.unsupported(
                Capability.FETCH_TRACKING_NO.value,
                fallback="manual",
                message="妙手未返回物流单号（字段名待实测确认）",
            )
        return AdapterResult.success(
            TrackingPayload(
                logistics_company=str(pick(data, "logistics_company", "") or ""),
                tracking_no=tracking_no,
                shipped_at=pick(data, "shipped_at"),
            )
        )

    async def write_back_tracking(self, req: WriteBackRequest) -> AdapterResult[WriteBackResult]:
        """回填物流单号到店铺（SUPPORTED ⚠TODO）。

        ★ 注意：这是"把物流单号写回店铺"，属于 `logistics.write` 白名单能力，
          与"商品编辑权"无关 —— 第三方永远拿不到 `item.write` 类能力（红线 R1/R2）。
        """
        if not self.has_endpoint(Capability.WRITE_BACK_TRACKING.value):
            return AdapterResult.unsupported(
                Capability.WRITE_BACK_TRACKING.value, fallback="manual", message="妙手未配置回填端点（待实测）"
            )
        result = await self.call(
            Capability.WRITE_BACK_TRACKING.value,
            {
                "platform_order_no": req.platform_order_no,
                "shop_item_id": req.shop_item_id,
                "logistics_company": req.logistics_company,
                "tracking_no": req.tracking_no,
            },
        )
        if not result.ok:
            return AdapterResult.retryable(Capability.WRITE_BACK_TRACKING.value, result.error)
        data = result.data if isinstance(result.data, dict) else {}
        return AdapterResult.success(
            WriteBackResult(
                success=bool(pick(data, "success", False)),
                message=str(pick(data, "message", "") or ""),
                retryable=False,
            )
        )

    async def submit_refund(self, req: RefundRequest) -> AdapterResult[RefundResult]:
        """提交 1688 退款（SUPPORTED ⚠TODO）。"""
        if not self.has_endpoint(Capability.SUBMIT_REFUND.value):
            return AdapterResult.unsupported(
                Capability.SUBMIT_REFUND.value, fallback="manual", message="妙手未配置退款端点（待实测）"
            )
        result = await self.call(
            Capability.SUBMIT_REFUND.value,
            {
                "platform_order_no": req.platform_refund_no,
                "refund_amount_cents": req.refund_amount_cents,
                "reason": req.reason,
            },
        )
        if not result.ok:
            return AdapterResult.retryable(Capability.SUBMIT_REFUND.value, result.error)
        data = result.data if isinstance(result.data, dict) else {}
        return AdapterResult.success(
            RefundResult(
                accepted=bool(pick(data, "accepted", False)),
                refund_1688_no=pick(data, "refund_1688_no"),
                message=str(pick(data, "message", "") or ""),
            )
        )

    async def get_return_address(self, req: ReturnAddressQuery) -> AdapterResult[ReturnAddress]:
        """获取 1688 退货地址（SUPPORTED ⚠TODO）。"""
        if not self.has_endpoint(Capability.GET_RETURN_ADDRESS.value):
            return AdapterResult.unsupported(
                Capability.GET_RETURN_ADDRESS.value, fallback="manual", message="妙手未配置退货地址端点（待实测）"
            )
        result = await self.call(
            Capability.GET_RETURN_ADDRESS.value, {"purchase_order_no": req.purchase_order_no}
        )
        if not result.ok:
            return AdapterResult.retryable(Capability.GET_RETURN_ADDRESS.value, result.error)
        data = result.data if isinstance(result.data, dict) else {}
        return AdapterResult.success(
            ReturnAddress(
                receiver_name_enc=str(pick(data, "receiver_name_enc", "") or ""),
                phone_enc=str(pick(data, "phone_enc", "") or ""),
                province=str(pick(data, "province", "") or ""),
                city=str(pick(data, "city", "") or ""),
                district=str(pick(data, "district", "") or ""),
                detail=str(pick(data, "detail", "") or ""),
            )
        )

    async def push_inventory_change(self, req: InventoryChangeEvent) -> AdapterResult[dict]:
        """推送库存 / 价格变动（SUPPORTED ⚠TODO）。"""
        if not self.has_endpoint(Capability.PUSH_INVENTORY_CHANGE.value):
            return AdapterResult.unsupported(
                Capability.PUSH_INVENTORY_CHANGE.value, message="妙手未配置库存推送端点（待实测）"
            )
        result = await self.call(
            Capability.PUSH_INVENTORY_CHANGE.value,
            {
                "source_sku_code_1688": req.source_sku_code_1688,
                "stock_qty": req.stock_qty if req.stock_qty is not None else "",
                "cost_cents": req.cost_cents if req.cost_cents is not None else "",
                "change_type": req.change_type,
            },
        )
        if not result.ok:
            return AdapterResult.retryable(Capability.PUSH_INVENTORY_CHANGE.value, result.error)
        data = result.data if isinstance(result.data, dict) else {}
        return AdapterResult.success({"accepted": bool(pick(data, "accepted", False)), "raw": data})

    # ---------------- 健康检查 ----------------

    async def health_check(self) -> AdapterResult[HealthStatus]:
        """连通性自检：base_url 为空或端点未配置 → unknown/down，绝不抛异常。"""
        started = time.perf_counter()
        base_url = str(self.load().get("base_url", "") or (self.config.base_url if self.config else ""))
        if not base_url:
            return AdapterResult.success(
                HealthStatus.build(
                    healthy=False,
                    status="unknown",
                    message="妙手未配置接口根地址（base_url 为空）—— 待实测确认端点后填写",
                    started=started,
                )
            )
        try:
            result = await self.http.health_ping()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            return AdapterResult.success(
                HealthStatus.build(healthy=False, status="down", message=f"连通性自检异常：{exc}", started=started)
            )
        return AdapterResult.success(
            HealthStatus.build(
                healthy=result.ok,
                status="healthy" if result.ok else "degraded",
                message=result.error or f"HTTP {result.status_code}",
                started=started,
            )
        )
