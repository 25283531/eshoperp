"""PddAdapter —— 拼多多开放平台真实上架适配器【骨架 + TODO】。

★ 现状（PRD Q1 未决）：拼多多「商品发布」类 API 需企业主体 + 类目资质，
  个人 / 个体户基本无法取得，且缺少字段名文档，**本次不实现真实调用**。

TODO（取得资质后按序补齐）：
    1. 接入拼多多开放平台（`https://open-api.pinduoduo.com`，需在商家后台申请 `client_id/secret`）；
    2. 实现 `publish()` → `pdd.goods.add`（注意：拼多多为单 SKU 多规格结构，与淘宝差异大）；
       TODO: 确认 goods.add 的 `sku_list` / `spec_id_list` 组合规则（Q1）
    3. 实现 `offline()` → `pdd.goods.sale.status.set`（is_onsale=0）；
       TODO: 确认下架参数是 goods_id 列表还是单个
    4. 实现 `update_stock_price()` → `pdd.goods.quantity.update` / `pdd.goods.price.update`；
       TODO: 确认拼多多价格单位（元 vs 分）与库存更新频率限制
    5. 实现 `query_status()` → `pdd.goods.detail.get`；
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

__all__ = ["PddAdapter"]


class PddAdapter(ListingAdapter):
    """拼多多真实上架适配器（骨架，未实现 —— 待资质与文档）。"""

    def __init__(
        self,
        account: Any = None,
        config: dict[str, Any] | None = None,
        session: Any = None,
        http: Any = None,
    ) -> None:
        """初始化拼多多适配器骨架。"""
        super().__init__(account=account, config=config, session=session, http=http)
        self.platform = Platform.PDD
        self.mode = ListingMode.REAL

    async def publish(self, payload: ListingPayload) -> AdapterResult[ListingPublishResult]:
        """TODO: 调用 pdd.goods.add；当前返回 UNSUPPORTED 由工厂降级。"""
        return AdapterResult.unsupported("publish", fallback="mock", message="拼多多真实发布未实现（待资质，PRD Q1）")

    async def query_status(self, shop_item_ids: Sequence[str]) -> AdapterResult[list[ListingStatus]]:
        """TODO: 调用 pdd.goods.detail.get。"""
        return AdapterResult.unsupported("query_status", fallback="mock", message="拼多多状态查询未实现（待资质）")

    async def offline(self, shop_item_ids: Sequence[str], reason: str) -> AdapterResult[dict]:
        """TODO: 调用 pdd.goods.sale.status.set。"""
        return AdapterResult.unsupported("offline", fallback="mock", message="拼多多下架未实现（待资质）")

    async def update_stock_price(self, items: Sequence[dict]) -> AdapterResult[dict]:
        """TODO: 调用 pdd.goods.quantity.update / pdd.goods.price.update。"""
        return AdapterResult.unsupported("update_stock_price", fallback="mock", message="拼多多改价改库存未实现（待资质）")

    async def health_check(self) -> AdapterResult[HealthStatus]:
        """TODO: 真实连通性自检；当前报 unknown。"""
        return AdapterResult.success(
            HealthStatus.unknown("拼多多适配器未取得资质，未做连通性自检"),
            message="未实现",
        )

    def available(self) -> bool:
        """未取得资质 → 不可用，工厂降级到 Mock。"""
        return False

    def note(self) -> str:
        """前端展示说明。"""
        return "拼多多真实 API：未取得资质，暂不可用（PRD Q1）"
