"""TaobaoAdapter —— 淘宝开放平台真实上架适配器【骨架 + TODO】。

★ 现状（PRD Q1 未决）：个人 / 个体户能否取得淘宝「商品发布」类 API 资质未确认，
  且缺少官方 SDK 与字段名文档，**本次不实现真实调用**。

TODO（取得资质后按序补齐）：
    1. 接入淘宝开放平台 TOP SDK（或使用 httpx 直连 `https://eco.taobao.com/router/rest`）；
    2. 实现 `publish()` → `taobao.item.add` / `taobao.item.sku.add`；
       ★ 需确认：商品发布接口是否对个人开发者开放（Q1）
    3. 实现 `offline()` → `taobao.item.update`（approve_status=instock）；
       TODO: 确认下架用的是 `item.update` 还是独立的 `item.delisting`
    4. 实现 `update_stock_price()` → `taobao.item.quantity.update` / `taobao.item.price.update`；
       TODO: 确认 SKU 维度改库存的接口名（sku.quantity.update?）
    5. 实现 `query_status()` → `taobao.item.get`；
    6. `health_check()` 改为真实 ping 接口。

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

__all__ = ["TaobaoAdapter"]


class TaobaoAdapter(ListingAdapter):
    """淘宝真实上架适配器（骨架，未实现 —— 待资质与文档）。"""

    def __init__(
        self,
        account: Any = None,
        config: dict[str, Any] | None = None,
        session: Any = None,
        http: Any = None,
    ) -> None:
        """初始化淘宝适配器骨架。"""
        super().__init__(account=account, config=config, session=session, http=http)
        self.platform = Platform.TAOBAO
        self.mode = ListingMode.REAL

    async def publish(self, payload: ListingPayload) -> AdapterResult[ListingPublishResult]:
        """TODO: 调用 taobao.item.add；当前返回 UNSUPPORTED 由工厂降级。"""
        return AdapterResult.unsupported("publish", fallback="mock", message="淘宝真实发布未实现（待资质，PRD Q1）")

    async def query_status(self, shop_item_ids: Sequence[str]) -> AdapterResult[list[ListingStatus]]:
        """TODO: 调用 taobao.item.get。"""
        return AdapterResult.unsupported("query_status", fallback="mock", message="淘宝状态查询未实现（待资质）")

    async def offline(self, shop_item_ids: Sequence[str], reason: str) -> AdapterResult[dict]:
        """TODO: 调用 taobao.item.update 下架。"""
        return AdapterResult.unsupported("offline", fallback="mock", message="淘宝下架未实现（待资质）")

    async def update_stock_price(self, items: Sequence[dict]) -> AdapterResult[dict]:
        """TODO: 调用 taobao.item.quantity.update / item.price.update。"""
        return AdapterResult.unsupported("update_stock_price", fallback="mock", message="淘宝改价改库存未实现（待资质）")

    async def health_check(self) -> AdapterResult[HealthStatus]:
        """TODO: 真实连通性自检；当前报 unknown。"""
        return AdapterResult.success(
            HealthStatus.unknown("淘宝适配器未取得资质，未做连通性自检"),
            message="未实现",
        )

    def available(self) -> bool:
        """未取得资质 → 不可用，工厂降级到 Mock。"""
        return False

    def note(self) -> str:
        """前端展示说明。"""
        return "淘宝真实 API：未取得资质，暂不可用（PRD Q1）"
