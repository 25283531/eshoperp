"""★ MockListingAdapter —— MVP 默认上架适配器（ADR-4 / PRD LST-P0-02）。

行为：
    * 生成肉眼可辨的模拟商品 ID / SKU 编码：`MOCK-{PLATFORM}-{timestamp}-{seq}`；
    * 所有产出 `is_mock=True`，**不参与真实履约**（OrderService 匹配映射时硬过滤）；
    * 不发起任何真实网络请求，保证无资质也能跑通全链路。
"""

from __future__ import annotations

import time
from typing import Any, Sequence

from app.adapters.listing.base import (
    ListingAdapter,
    ListingPayload,
    ListingPublishResult,
    ListingStatus,
    normalize_platform,
)
from app.adapters.fulfillment.manifest import AdapterResult, HealthStatus
from app.core.logging import get_logger
from app.models.enums import ListingMode, Platform
from app.utils.kit import iso_utc, mock_item_id, mock_sku_code, utc_now

logger = get_logger(__name__)

__all__ = ["MockListingAdapter"]


class MockListingAdapter(ListingAdapter):
    """Mock 上架适配器：返回模拟商品 ID / SKU 编码，不触及真实平台。"""

    def __init__(
        self,
        account: Any = None,
        config: dict[str, Any] | None = None,
        session: Any = None,
        http: Any = None,
        platform: str | Platform = Platform.TAOBAO,
    ) -> None:
        """初始化 Mock 适配器（platform 可由工厂指定，枚举或字符串均可）。"""
        super().__init__(account=account, config=config, session=session, http=http)
        self.platform = normalize_platform(platform)
        self.mode = ListingMode.MOCK
        self._seq = 0

    # ---------------- ID 生成 ----------------

    def _next_shop_item_id(self) -> str:
        """生成模拟商品 ID。"""
        self._seq += 1
        return mock_item_id(self.platform.value, self._seq)

    # ---------------- 能力实现 ----------------

    async def publish(self, payload: ListingPayload) -> AdapterResult[ListingPublishResult]:
        """模拟发布：生成商品 ID 与每个 SKU 的编码（is_mock=True）。"""
        shop_item_id = self._next_shop_item_id()
        sku_results: list[dict[str, Any]] = []
        for index, sku in enumerate(payload.skus):
            sku_results.append(
                {
                    "spec_json": dict(sku.spec_json),
                    "shop_sku_code": mock_sku_code(self.platform.value, index, self._seq),
                    "success": True,
                    "source_sku_id": sku.source_sku_id,
                    "source_sku_code_1688": sku.source_sku_code_1688,
                    "purchase_cost_cents": sku.purchase_cost_cents,
                    "sale_price_cents": sku.sale_price_cents,
                    "stock_qty": sku.stock_qty,
                }
            )

        result = ListingPublishResult(
            shop_item_id=shop_item_id,
            sku_results=sku_results,
            is_mock=True,
            raw_response={
                "mode": "mock",
                "platform": self.platform.value,
                "shop_id": payload.shop_id,
                "title": payload.title,
                "generated_at": iso_utc(utc_now()),
            },
        )
        logger.info(
            "mock_listing_publish",
            platform=self.platform.value,
            shop_item_id=shop_item_id,
            sku_count=len(sku_results),
        )
        return AdapterResult.success(
            result,
            message="Mock 模式：已生成模拟商品 ID 与 SKU 编码（不参与真实履约）",
        )

    async def query_status(self, shop_item_ids: Sequence[str]) -> AdapterResult[list[ListingStatus]]:
        """模拟状态查询：凡是 Mock 前缀的 ID 一律视为在售。"""
        statuses: list[ListingStatus] = []
        for item_id in shop_item_ids:
            is_mock = str(item_id).startswith("MOCK-")
            statuses.append(
                ListingStatus(
                    shop_item_id=str(item_id),
                    status="on_sale" if is_mock else "failed",
                    shop_sku_codes=[],
                    updated_at=iso_utc(utc_now()),
                )
            )
        return AdapterResult.success(statuses, message="Mock 模式状态查询")

    async def offline(self, shop_item_ids: Sequence[str], reason: str) -> AdapterResult[dict]:
        """模拟下架。★ 真实下架也只走这里（红线 R2）。"""
        logger.info(
            "mock_listing_offline",
            platform=self.platform.value,
            items=list(shop_item_ids),
            reason=reason,
        )
        return AdapterResult.success(
            {
                "offlined": [str(i) for i in shop_item_ids],
                "reason": reason,
                "is_mock": True,
            },
            message="Mock 模式：已模拟下架",
        )

    async def update_stock_price(self, items: Sequence[dict]) -> AdapterResult[dict]:
        """模拟改库存与价格。"""
        logger.info("mock_listing_update_stock_price", platform=self.platform.value, count=len(items))
        return AdapterResult.success(
            {"updated": len(items), "is_mock": True},
            message="Mock 模式：已模拟更新库存与价格",
        )

    async def health_check(self) -> AdapterResult[HealthStatus]:
        """Mock 适配器始终健康（无外部依赖）。"""
        started = time.perf_counter()
        status = HealthStatus.build(
            healthy=True,
            status="healthy",
            message="Mock 上架适配器：无需外部凭证与网络，始终可用",
            started=started,
        )
        return AdapterResult.success(status)

    def available(self) -> bool:
        """Mock 适配器始终可用。"""
        return True

    def note(self) -> str:
        """前端展示说明。"""
        return "Mock 模式：生成模拟商品 ID 与 SKU 编码，不参与真实履约（无需平台资质）"
