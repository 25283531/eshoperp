"""平台商品管理（ARCH §5.5.8）。

★ 红线 R2：`POST /listing-products/{id}/offline` 是全系统**唯一**下架入口，
  内部调用 `ListingService.offline()` —— 第三方适配器**不得**直写店铺。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.api.v1._common import page_of
from app.core.deps import CurrentOperator, DbSession
from app.core.pagination import PageParams, page_params
from app.core.response import ApiResponse
from app.schemas.listing import (
    BatchOfflineRequest,
    FillPriceRequest,
    ListingProductDetailVo,
    ListingProductVo,
    ListingSkuVo,
    OfflineRequest,
)
from app.services.listing_service import ListingService

router = APIRouter(tags=["平台商品"])

__all__ = ["router"]


@router.get("/listing-products", summary="平台商品列表")
async def list_listing_products(
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    platform: Annotated[str | None, Query()] = None,
    shop_id: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    source_product_id: Annotated[int | None, Query()] = None,
    is_mock: Annotated[bool | None, Query()] = None,
    missing_price: Annotated[
        bool | None, Query(description="★ true：只筛出售价为空的在售商品（存量补填入口）")
    ] = None,
) -> ApiResponse[Any]:
    """分页返回平台商品（含 `missing_price_count`）。"""
    rows, titles, sku_counts, missing_counts, total = await ListingService.list_products(
        session,
        platform=platform,
        shop_id=shop_id,
        status=status,
        source_product_id=source_product_id,
        is_mock=is_mock,
        missing_price=missing_price,
        page=params.page,
        page_size=params.page_size,
    )
    items = [
        ListingProductVo.from_model(
            p,
            source_product_title=titles.get(int(p.source_product_id)) if p.source_product_id else None,
            sku_count=int(sku_counts.get(int(p.id), 0)),
            missing_price_count=int(missing_counts.get(int(p.id), 0)),
        )
        for p in rows
    ]
    return ApiResponse.ok(data=page_of(items, total, params))


@router.get("/listing-products/{product_id}", summary="平台商品详情")
async def get_listing_product(
    product_id: int, session: DbSession
) -> ApiResponse[ListingProductDetailVo]:
    """详情含 `skus[]` 与 `mappings[]`。"""
    product = await ListingService.get_product(session, product_id)
    skus = await ListingService.list_skus(session, product_id)
    mappings = await ListingService.list_mappings(session, product_id)

    from app.schemas.mapping import SkuMappingVo

    base = ListingProductVo.from_model(
        product, sku_count=len(skus), missing_price_count=sum(1 for s in skus if not (s.sale_price_cents or 0))
    )
    detail = ListingProductDetailVo(
        **base.model_dump(),
        skus=[ListingSkuVo.from_model(s) for s in skus],
        mappings=[SkuMappingVo.from_model(m).model_dump() for m in mappings],
    )
    return ApiResponse.ok(data=detail)


@router.post("/listing-products/{product_id}/offline", summary="★ 下架（唯一入口，红线 R2）")
async def offline_listing_product(
    product_id: int,
    payload: OfflineRequest,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """★ 全系统唯一的下架入口 → `ListingService.offline()` → `ListingAdapter.offline()`。"""
    product = await ListingService.offline(
        session, product_id, reason=payload.reason or "", operator=operator.name
    )
    await session.commit()
    return ApiResponse.ok(
        data={"id": int(product.id), "status": product.status}, message="商品已下架"
    )


@router.post("/listing-products/{product_id}/online", summary="重新上架")
async def online_listing_product(
    product_id: int,
    session: DbSession,
    operator: CurrentOperator,
    revalidate_mapping: Annotated[bool, Query(description="上架前重新校验映射")] = True,
) -> ApiResponse[dict[str, Any]]:
    """★ 映射无效时**只通知不上架**（422 / 7003）。"""
    product = await ListingService.online(
        session, product_id, revalidate_mapping=revalidate_mapping, operator=operator.name
    )
    await session.commit()
    return ApiResponse.ok(data={"id": int(product.id), "status": product.status})


@router.post("/listing-products/batch-offline", summary="批量下架")
async def batch_offline_listing_products(
    payload: BatchOfflineRequest,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """批量下架：逐条走 `ListingService.offline()`（唯一出口），失败不影响整批。"""
    ids = [int(i) for i in (payload.ids or [])]
    result = await ListingService.batch_offline(
        session, ids, reason=payload.reason or "", operator=operator.name
    )
    await session.commit()
    return ApiResponse.ok(data=result)


@router.post("/listing-products/{product_id}/fill-price", summary="★ 存量售价补填")
async def fill_price(
    product_id: int,
    payload: FillPriceRequest,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """★ v1.5：`sale_price` 必填且 > 0（422）；补填后**自动触发 `cost_underwater` 重算**。

    这是消灭 `listing_sku.sale_price_cents = 0` 的入口
    —— 空售价会让成本倒挂检测对这部分商品**永久静默失效**。
    """
    # ★ 传 Pydantic 对象本身（服务层按属性访问 `item.shop_sku_code` / `item.sale_price`），
    #   不能先 model_dump() 成 dict —— 否则服务层属性访问会 AttributeError（500）。
    result = await ListingService.fill_price(
        session,
        product_id,
        list(payload.items),
        reason=payload.reason or "",
        operator=operator.name,
    )
    await session.commit()
    return ApiResponse.ok(data=result, message="售价补填完成，已触发成本倒挂重算")
