"""供应商与货源商品（ARCH §5.5.3）。

★ 两条录入路径并存（本项目面向个体户，**手工 / CSV 是真实主路径**）：
    1. `POST /source-products/collect` 与 `/batch-import`：1688 采集，**走 Alibaba1688Adapter**，
       需要 AppKey / AccessToken（缺失时任务会把 `task_record.status` 诚实地标为 `failed`）；
    2. `POST /source-products/manual` 与 `/import-csv`：手工录入 / CSV 批量导入，
       **完全不碰 1688 适配器**，写入的数据同样能被 AI 重构 / SKU 映射 / 上架 / 订单匹配消费。

采集为**长任务**：`POST /source-products/collect` 与 `/batch-import` 提交异步任务返回 202，
由 `source_collect_handler` 在后台落库（处理器自己开 session，见 `app/tasks/handlers/`）。
手工录入与 CSV 导入是**同步**的：运营填完立刻要看到"哪行成功、哪行为什么失败"。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Query, UploadFile

from app.api.v1._common import enqueue_task, page_of
from app.core.deps import CurrentOperator, DbSession
from app.core.errors import BusinessError, ErrorCode
from app.core.pagination import PageParams, page_params
from app.core.response import ApiResponse
from app.models.enums import TaskType
from app.schemas.source import (
    SourceCollectRequest,
    SourceProductDetailVo,
    SourceProductManualCreate,
    SourceProductVo,
    SourceSkuVo,
    SupplierCreate,
    SupplierUpdate,
    SupplierVo,
)
from app.services.source_service import MAX_IMPORT_BYTES, SourceService

router = APIRouter(tags=["货源"])

__all__ = ["router"]


# ======================================================================
#  供应商
# ======================================================================


@router.get("/suppliers", summary="供应商列表")
async def list_suppliers(
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    keyword: Annotated[str | None, Query(description="名称 / 1688 ID 模糊")] = None,
    status: Annotated[str | None, Query(description="active/blacklist")] = None,
) -> ApiResponse[Any]:
    """分页返回供应商。"""
    rows, total = await SourceService.list_suppliers(
        session, keyword=keyword, status=status, page=params.page, page_size=params.page_size
    )
    return ApiResponse.ok(data=page_of([SupplierVo.from_model(r) for r in rows], total, params))


@router.post("/suppliers", status_code=201, summary="创建供应商")
async def create_supplier(
    payload: SupplierCreate,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[SupplierVo]:
    """创建供应商。"""
    row = await SourceService.create_supplier(session, payload, operator=operator.name)
    await session.commit()
    return ApiResponse.ok(data=SupplierVo.from_model(row), message="供应商已创建")


@router.put("/suppliers/{supplier_id}", summary="更新供应商")
async def update_supplier(
    supplier_id: int,
    payload: SupplierUpdate,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[SupplierVo]:
    """局部更新供应商。"""
    row = await SourceService.update_supplier(
        session, supplier_id, payload, operator=operator.name
    )
    await session.commit()
    return ApiResponse.ok(data=SupplierVo.from_model(row))


@router.delete("/suppliers/{supplier_id}", summary="删除供应商（软删除）")
async def delete_supplier(
    supplier_id: int,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, int]]:
    """软删除供应商。"""
    deleted = await SourceService.delete_supplier(session, supplier_id, operator=operator.name)
    await session.commit()
    return ApiResponse.ok(data={"id": int(deleted)})


# ======================================================================
#  货源商品
# ======================================================================


@router.post("/source-products/collect", status_code=202, summary="采集 1688 商品（异步）")
async def collect_source_products(
    payload: SourceCollectRequest,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """提交采集任务（单次 ≤ 50 个 ID / 链接）。"""

    identifiers = [str(i).strip() for i in (payload.identifiers or []) if str(i).strip()]
    task_record_id = await enqueue_task(
        TaskType.SOURCE_COLLECT.value,
        {
            "identifiers": identifiers,
            "supplier_id": payload.supplier_id,
            "operator": operator.name,
        },
    )
    await session.commit()
    return ApiResponse.ok(
        data={
            "accepted": len(identifiers),
            "task_record_id": task_record_id,
            "source": payload.source,
            "hint": "采集结果落库后可在 /source-products 查询；任务进度轮询 /tasks/{id}",
        },
        message="采集任务已受理" if task_record_id else "采集任务已受理（任务框架未就绪，请稍后重试）",
    )


@router.post("/source-products/batch-import", status_code=202, summary="批量导入货源商品")
async def batch_import_source_products(
    payload: SourceCollectRequest,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """批量导入（与 collect 同一异步链路，便于前端复用一个入口）。"""
    return await collect_source_products(payload, session, operator)


# ======================================================================
#  ★ 手工录入 / CSV 导入（不经过 1688 适配器）
# ======================================================================


@router.post("/source-products/manual", status_code=201, summary="手工录入货源商品（含 SKU）")
async def create_source_product_manual(
    payload: SourceProductManualCreate,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """★ 单条手工录入：`source_platform='manual'`，一次请求带 SKU 列表。

    ★ 为什么必须有这个端点：1688 采集需要开放平台凭证，个体户拿不到；
      没有手工入口就意味着系统里一条货源都进不来，整条主路径起不了步。

    ★ 写入的数据**必须能被下游消费**：录入后可进 AI 重构、可建 SKU 映射、
      可半自动上架、可被订单匹配命中（见 `scripts/verify_manual_source_chain.py` 的真实 HTTP 证据）。

    Raises:
        400 / 1001: 参数非法（SKU 为空、成本不是数字等），`data.failed[]` 逐条给中文原因。
    """
    outcome = await SourceService.create_manual(session, payload, operator=operator.name)
    await session.commit()
    product = outcome["product"]
    hints = SourceService.suggested_sale_prices(product)
    return ApiResponse.ok(
        data={
            "id": int(product.id),
            "product_1688_id": str(product.product_1688_id),
            "source_platform": "manual",
            "title": str(product.title),
            "created": bool(outcome["created"]),
            "created_skus": int(outcome["created_skus"]),
            "updated_skus": int(outcome["updated_skus"]),
            "skus": [
                SourceSkuVo.from_model(
                    sku, suggested_sale_price_cents=hints.get(str(sku.sku_code_1688))
                ).model_dump()
                for sku in outcome["skus"]
            ],
        },
        message="货源商品已录入（手工）",
    )


@router.post("/source-products/import-csv", summary="CSV 批量导入货源商品")
async def import_source_products_csv(
    session: DbSession,
    operator: CurrentOperator,
    file: Annotated[UploadFile, File(description="按 scripts/source_products_template.csv 填写的 CSV")],
) -> ApiResponse[dict[str, Any]]:
    """★ CSV 批量导入，返回 `{created, updated, failed:[{row, identifier, reason}]}`。

    ★ 失败**逐行**返回可读中文原因，绝不静默吞掉任何一行 ——
      "导入成功了但一行没进去"是本项目反复踩过的静默失效，必须可观测。

    Raises:
        400 / 1001: 文件为空 / 超过 5MB / 缺必填列『商品标题』/ 超过 500 行。
    """
    content = await file.read()
    if len(bytes(content or b"")) > MAX_IMPORT_BYTES:
        raise BusinessError(
            f"CSV 文件超过 {MAX_IMPORT_BYTES // (1024 * 1024)}MB，请拆分后再导入",
            code=ErrorCode.PARAM_ERROR,
        )
    result = await SourceService.import_csv(session, content, operator=operator.name)
    await session.commit()
    failed = [item.model_dump() for item in result.failed]
    message = (
        f"导入完成：新增 {result.created} 个商品 / 更新 {result.updated} 个商品"
        + (f"，失败 {len(failed)} 行" if failed else "")
    )
    return ApiResponse.ok(
        data={
            "total": int(result.total),
            "created": int(result.created),
            "updated": int(result.updated),
            "created_skus": int(result.created_skus),
            "updated_skus": int(result.updated_skus),
            "failed": failed,
        },
        message=message,
    )


@router.get("/source-products", summary="货源商品列表")
async def list_source_products(
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    keyword: Annotated[str | None, Query(description="标题 / 1688 ID 模糊")] = None,
    product_1688_id: Annotated[str | None, Query()] = None,
    supplier_id: Annotated[int | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    source_platform: Annotated[str | None, Query(description="manual=手工录入 / alibaba1688=1688 采集")] = None,
    collected_from: Annotated[str | None, Query(description="ISO8601")] = None,
    collected_to: Annotated[str | None, Query(description="ISO8601")] = None,
) -> ApiResponse[Any]:
    """分页返回货源商品（含 `sku_count` 与供应商名）。"""
    rows, supplier_names, sku_counts, total = await SourceService.list_source_products(
        session,
        keyword=keyword,
        product_1688_id=product_1688_id,
        supplier_id=supplier_id,
        status=status,
        collected_from=collected_from,
        collected_to=collected_to,
        source_platform=source_platform,
        page=params.page,
        page_size=params.page_size,
    )
    items = [
        SourceProductVo.from_model(
            r,
            supplier_name=supplier_names.get(int(r.supplier_id)) if r.supplier_id else None,
            sku_count=int(sku_counts.get(int(r.id), 0)),
        )
        for r in rows
    ]
    return ApiResponse.ok(data=page_of(items, total, params))


@router.get("/source-products/{product_id}", summary="货源商品详情")
async def get_source_product(
    product_id: int,
    session: DbSession,
) -> ApiResponse[SourceProductDetailVo]:
    """详情含 `skus[]` 与 `assets[]`。"""
    product = await SourceService.get_source_product(session, product_id)
    skus = await SourceService.list_source_skus(session, product_id)
    assets = await SourceService.list_assets(session, product_id)

    from app.schemas.asset import AssetVo

    hints = SourceService.suggested_sale_prices(product)
    base = SourceProductVo.from_model(product)
    detail = SourceProductDetailVo(
        **base.model_dump(),
        params_json=dict(product.params_json or {}),
        skus=[
            SourceSkuVo.from_model(s, suggested_sale_price_cents=hints.get(str(s.sku_code_1688)))
            for s in skus
        ],
        assets=[AssetVo.from_model(a).model_dump() for a in assets],
    )
    return ApiResponse.ok(data=detail)


@router.delete("/source-products/{product_id}", summary="删除货源商品（软删除）")
async def delete_source_product(
    product_id: int,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, int]]:
    """软删除货源商品。"""
    deleted = await SourceService.delete_source_product(session, product_id, operator=operator.name)
    await session.commit()
    return ApiResponse.ok(data={"id": int(deleted)})


@router.get("/source-products/{product_id}/skus", summary="货源 SKU 列表")
async def list_source_skus(
    product_id: int,
    session: DbSession,
) -> ApiResponse[list[SourceSkuVo]]:
    """返回该商品的 1688 SKU 与规格指纹。"""
    skus = await SourceService.list_source_skus(session, product_id)
    return ApiResponse.ok(data=[SourceSkuVo.from_model(s) for s in skus])
