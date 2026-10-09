"""YitaoAdapter —— 逸淘分销工具履约适配器（profile 驱动 + 能力降级）。

与妙手的差异（MVP 基线能力矩阵）：
    * `submit_refund` / `get_return_address` 声明为 **UNSUPPORTED**（能力未确认，保守起见）；
      实测可用后只改 `profiles/yitao.yaml` 的 level 即可，**不改代码**。
    * `match_sku` 恒 UNSUPPORTED（★ 映射权威源始终在自研侧本地 sku_mapping）。
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
    TrackingPayload,
    TrackingQuery,
    WriteBackRequest,
    WriteBackResult,
)
from app.adapters.fulfillment.http_client import HttpClient, load_profile, pick
from app.adapters.fulfillment.manifest import (
    AdapterConfig,
    AdapterManifest,
    AdapterResult,
    Capability,
    HealthStatus,
)
from app.adapters.fulfillment.miaoshou import ProfileDrivenMixin
from app.adapters.fulfillment.registry import register
from app.core.logging import get_logger
from app.models.enums import AdapterName

logger = get_logger(__name__)

__all__ = ["YitaoAdapter"]

PROFILE_NAME = "yitao"


@register(AdapterName.YITAO.value, display_name="逸淘")
class YitaoAdapter(ProfileDrivenMixin, FulfillmentAdapter):
    """逸淘履约适配器。

    ★ 红线 R2：本类**没有**也不允许有 `offline` / `update_stock_price` 方法。
    """

    adapter_name = AdapterName.YITAO.value
    display_name = "逸淘"
    profile_name = PROFILE_NAME

    def __init__(self, config: Any = None, credential: Any = None, http: Any = None, session: Any = None) -> None:
        """初始化逸淘适配器。"""
        profile = load_profile(PROFILE_NAME)
        merged: dict[str, Any] = dict(profile)
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
        """按 profile 生成能力矩阵。"""
        return self.manifest_from_profile(
            adapter_name=self.adapter_name,
            display_name=self.display_name,
            version=str(self.load().get("version", "0.1.0-todo")),
        )

    # ---------------- 8 项能力 ----------------

    async def fetch_orders(self, req: FetchOrdersRequest) -> AdapterResult[list[OrderPayload]]:
        """拉取订单（SUPPORTED ⚠TODO）。"""
        if not self.has_endpoint(Capability.FETCH_ORDERS.value):
            return AdapterResult.unsupported(
                Capability.FETCH_ORDERS.value, fallback="manual", message="逸淘未配置订单端点（待实测）"
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
        raw_orders = pick(result.raw, "data.rows") or pick(result.raw, "data.list") or []
        if not isinstance(raw_orders, list):
            raw_orders = []

        orders: list[OrderPayload] = []
        for raw in raw_orders:
            items_raw = pick(raw, str(mapping.get("items", "order_items")), []) or []
            items: list[dict[str, Any]] = []
            if isinstance(items_raw, list):
                for item in items_raw:
                    items.append(
                        {
                            "shop_item_id": str(pick(item, str(mapping.get("item_shop_item_id", "item_id")), "") or ""),
                            "shop_sku_code": str(pick(item, str(mapping.get("item_sku_code", "sku_code")), "") or ""),
                            "quantity": int(pick(item, "num", pick(item, "quantity", 1)) or 1),
                            "price_cents": int(pick(item, "price", 0) or 0),
                        }
                    )
            orders.append(
                OrderPayload(
                    platform="",
                    shop_id=str(pick(raw, str(mapping.get("shop_id", "shop_id")), "") or ""),
                    platform_order_no=str(pick(raw, str(mapping.get("platform_order_no", "order_sn")), "") or ""),
                    buyer_info_enc=str(pick(raw, str(mapping.get("buyer_info_enc", "buyer_info")), "") or ""),
                    receiver_addr_enc=str(pick(raw, str(mapping.get("receiver_addr_enc", "address")), "") or ""),
                    total_amount_cents=int(pick(raw, str(mapping.get("total_amount_cents", "total_fee")), 0) or 0),
                    paid_at=str(pick(raw, str(mapping.get("paid_at", "created_at")), "") or ""),
                    items=items,
                    raw=raw if isinstance(raw, dict) else {"value": raw},
                )
            )
        return AdapterResult.success(orders, message=f"逸淘返回 {len(orders)} 条订单")

    async def match_sku(self, req: MatchSkuRequest) -> AdapterResult[MatchSkuResult]:
        """★ 恒 UNSUPPORTED：映射权威源在自研侧。"""
        return AdapterResult.unsupported(
            Capability.MATCH_SKU.value,
            fallback=None,
            message="SKU 匹配的权威源是 ERP 本地 sku_mapping，第三方能力不参与（架构决定）",
        )

    async def place_purchase_order(self, req: PurchaseRequest) -> AdapterResult[PurchaseOrderPayload]:
        """1688 采购下单（SUPPORTED ⚠TODO）。"""
        if not self.has_endpoint(Capability.PLACE_PURCHASE_ORDER.value):
            return AdapterResult.unsupported(
                Capability.PLACE_PURCHASE_ORDER.value, fallback="csv_export", message="逸淘未配置下单端点（待实测）"
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
            message="逸淘下单返回（字段未实测，已做防御性解析）",
        )

    async def fetch_tracking_no(self, req: TrackingQuery) -> AdapterResult[TrackingPayload]:
        """获取物流单号（SUPPORTED ⚠TODO）。"""
        if not self.has_endpoint(Capability.FETCH_TRACKING_NO.value):
            return AdapterResult.unsupported(
                Capability.FETCH_TRACKING_NO.value, fallback="manual", message="逸淘未配置物流查询端点（待实测）"
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
                Capability.FETCH_TRACKING_NO.value, fallback="manual", message="逸淘未返回物流单号（字段名待实测确认）"
            )
        return AdapterResult.success(
            TrackingPayload(
                logistics_company=str(pick(data, "logistics_company", "") or ""),
                tracking_no=tracking_no,
                shipped_at=pick(data, "shipped_at"),
            )
        )

    async def write_back_tracking(self, req: WriteBackRequest) -> AdapterResult[WriteBackResult]:
        """回填物流单号（SUPPORTED ⚠TODO，属 `logistics.write` 白名单能力）。"""
        if not self.has_endpoint(Capability.WRITE_BACK_TRACKING.value):
            return AdapterResult.unsupported(
                Capability.WRITE_BACK_TRACKING.value, fallback="manual", message="逸淘未配置回填端点（待实测）"
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
            WriteBackResult(success=bool(pick(data, "success", False)), message=str(pick(data, "message", "") or ""))
        )

    # submit_refund / get_return_address 未覆写 → 走基类 UNSUPPORTED（profile 亦声明 unsupported）

    async def push_inventory_change(self, req: InventoryChangeEvent) -> AdapterResult[dict]:
        """推送库存 / 价格变动（SUPPORTED ⚠TODO）。"""
        if not self.has_endpoint(Capability.PUSH_INVENTORY_CHANGE.value):
            return AdapterResult.unsupported(
                Capability.PUSH_INVENTORY_CHANGE.value, message="逸淘未配置库存推送端点（待实测）"
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
        """连通性自检。"""
        started = time.perf_counter()
        base_url = str(self.load().get("base_url", "") or (self.config.base_url if self.config else ""))
        if not base_url:
            return AdapterResult.success(
                HealthStatus.build(
                    healthy=False,
                    status="unknown",
                    message="逸淘未配置接口根地址（base_url 为空）—— 待实测确认端点后填写",
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
