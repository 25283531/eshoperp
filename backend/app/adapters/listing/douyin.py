"""DouyinAdapter —— 抖店开放平台真实上架适配器【骨架 + TODO】。

★ 现状（PRD Q1 未决）：抖店「商品创建」类 API 需企业主体资质，个人 / 个体户大概率拿不到，
  且缺少字段名文档，**本次不实现真实调用**。

TODO（取得资质后按序补齐）：
    1. 接入抖店开放平台（`https://openapi-fxg.jinritemai.com`，需在控制台申请 `app_key/secret`）；
    2. 实现 `publish()` → `product.addV2` / `sku.add`；
       TODO: 确认商品创建接口版本（add / addV2）与 SKU 写入顺序
    3. 实现 `offline()` → `product.setOffline`；
       TODO: 确认是否支持批量下架，入参是 product_id 还是 outer_id
    4. 实现 `update_stock_price()` → `sku.editPrice` / `sku.syncStock`；
       TODO: 确认库存同步接口名与批量上限
    5. 实现 `query_status()` → `product.detail`；
    6. `health_check()` 改为真实 ping 接口（可用 `shop.brandList` 等只读接口）。

在此之前，工厂会因 `available() == False` 自动降级到 `MockListingAdapter`。
"""

from __future__ import annotations

from typing import Any, Sequence

from app.adapters.listing.base import (
    ListingAdapter,
    ListingPayload,
    ListingPublishResult,
    ListingStatus,
)
from app.adapters.fulfillment.manifest import AdapterResult, HealthStatus
from app.models.enums import ListingMode, Platform

__all__ = ["DouyinAdapter"]


class DouyinAdapter(ListingAdapter):
    """抖店真实上架适配器（骨架，未实现 —— 待资质与文档）。"""

    def __init__(
        self,
        account: Any = None,
        config: dict[str, Any] | None = None,
        session: Any = None,
        http: Any = None,
    ) -> None:
        """初始化抖店适配器骨架。"""
        super().__init__(account=account, config=config, session=session, http=http)
        self.platform = Platform.DOUYIN
        self.mode = ListingMode.REAL

    async def publish(self, payload: ListingPayload) -> AdapterResult[ListingPublishResult]:
        """TODO: 调用 product.addV2；当前返回 UNSUPPORTED 由工厂降级。"""
        return AdapterResult.unsupported("publish", fallback="mock", message="抖店真实发布未实现（待资质，PRD Q1）")

    async def query_status(self, shop_item_ids: Sequence[str]) -> AdapterResult[list[ListingStatus]]:
        """TODO: 调用 product.detail。"""
        return AdapterResult.unsupported("query_status", fallback="mock", message="抖店状态查询未实现（待资质）")

    async def offline(self, shop_item_ids: Sequence[str], reason: str) -> AdapterResult[dict]:
        """TODO: 调用 product.setOffline。"""
        return AdapterResult.unsupported("offline", fallback="mock", message="抖店下架未实现（待资质）")

    async def update_stock_price(self, items: Sequence[dict]) -> AdapterResult[dict]:
        """TODO: 调用 sku.editPrice / sku.syncStock。"""
        return AdapterResult.unsupported("update_stock_price", fallback="mock", message="抖店改价改库存未实现（待资质）")

    async def health_check(self) -> AdapterResult[HealthStatus]:
        """TODO: 真实连通性自检；当前报 unknown。"""
        return AdapterResult.success(
            HealthStatus.unknown("抖店适配器未取得资质，未做连通性自检"),
            message="未实现",
        )

    def available(self) -> bool:
        """未取得资质 → 不可用，工厂降级到 Mock。"""
        return False

    def note(self) -> str:
        """前端展示说明。"""
        return "抖店真实 API：未取得资质，暂不可用（PRD Q1）"
