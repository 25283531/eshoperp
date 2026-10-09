"""SKU 映射 —— 系统最高等级资产（ARCH §5.5.6）。

★ 两条最重要的硬约束都落在端点层的调用顺序上：
    1. `POST /sku-mappings/validate`：`blocking=true` 时返回 **422**，
       响应 `data` 仍携带完整 `MappingValidationVo`（MAP-P0-02，前端可直接渲染冲突明细）；
    2. 成本三层语义：`PUT` 带 `purchase_cost` 且未显式 `cost_source` → 服务端置 `manual`，
       此后货源变动**不再静默覆盖**，改生成「成本待确认」工单。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy import select

from app.api.v1._common import enqueue_task, page_of
from app.core.deps import CurrentOperator, DbSession
from app.core.errors import BusinessError, ErrorCode
from app.core.pagination import PageParams, page_params
from app.core.response import ApiResponse
from app.models.enums import TaskType
from app.models.mapping import SkuMapping
from app.schemas.mapping import (
    DetectConflictsRequest,
    MappingChangeLogVo,
    MappingConflictVo,
    MappingPendingResolveRequest,
    MappingPendingVo,
    MappingPushRequest,
    MappingStatsVo,
    MappingValidationRequest,
    MappingValidationVo,
    SkuMappingBatchRequest,
    SkuMappingCreate,
    SkuMappingDeleteRequest,
    SkuMappingUpdate,
    SkuMappingVo,
)
from app.services.mapping_service import MappingService
from app.services.mapping_validator import MappingValidator

router = APIRouter(tags=["SKU 映射"])

__all__ = ["router"]


# ======================================================================
#  CRUD
# ======================================================================


@router.get("/sku-mappings", summary="映射列表")
async def list_mappings(
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    platform: Annotated[str | None, Query()] = None,
    shop_id: Annotated[str | None, Query()] = None,
    shop_item_id: Annotated[str | None, Query()] = None,
    source_product_id: Annotated[int | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    has_conflict: Annotated[bool | None, Query()] = None,
    keyword: Annotated[str | None, Query(description="店铺 SKU 编码 / 1688 SKU 编码模糊")] = None,
    include_deleted: Annotated[bool, Query(description="是否包含软删除记录")] = False,
) -> ApiResponse[Any]:
    """分页返回映射（含 `cost_source` 等成本三层字段）。"""
    rows, total = await MappingService.list_mappings(
        session,
        platform=platform,
        shop_id=shop_id,
        shop_item_id=shop_item_id,
        source_product_id=source_product_id,
        status=status,
        has_conflict=has_conflict,
        keyword=keyword,
        include_deleted=include_deleted,
        page=params.page,
        page_size=params.page_size,
    )
    return ApiResponse.ok(data=page_of([SkuMappingVo.from_model(r) for r in rows], total, params))


@router.post("/sku-mappings", status_code=201, summary="创建映射")
async def create_mapping(
    payload: SkuMappingCreate,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[SkuMappingVo]:
    """创建映射（唯一键冲突返回 409）。"""
    row = await MappingService.create(session, payload, operator=operator.name)
    await session.commit()
    return ApiResponse.ok(data=SkuMappingVo.from_model(row), message="映射已创建")


@router.put("/sku-mappings/{mapping_id}", summary="更新映射")
async def update_mapping(
    mapping_id: int,
    payload: SkuMappingUpdate,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[SkuMappingVo]:
    """更新映射（自动写 `mapping_change_log`）。

    ★ 成本语义：body 含 `purchase_cost` 且未显式传 `cost_source` →
      自动置 `cost_source='manual'`（人工覆盖，此后自动同步不静默冲掉）。
    """
    row = await MappingService.update(session, mapping_id, payload, operator=operator.name)
    await session.commit()
    return ApiResponse.ok(data=SkuMappingVo.from_model(row), message="映射已更新")


@router.delete("/sku-mappings/{mapping_id}", summary="删除映射（★ 二次确认）")
async def delete_mapping(
    mapping_id: int,
    payload: SkuMappingDeleteRequest,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, int]]:
    """软删除（保留 180 天）；`confirm != true` 时返回 400。"""
    if not payload.confirm:
        raise BusinessError(
            "删除映射需二次确认：请在请求体中传 confirm=true",
            code=ErrorCode.PARAM_ERROR,
        )
    deleted = await MappingService.delete(
        session, mapping_id, reason=payload.reason, confirm=True, operator=operator.name
    )
    await session.commit()
    return ApiResponse.ok(data={"id": int(deleted)}, message="映射已软删除")


@router.post("/sku-mappings/{mapping_id}/restore", summary="恢复软删除的映射")
async def restore_mapping(
    mapping_id: int,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[SkuMappingVo]:
    """回滚恢复。"""
    row = await MappingService.restore(session, mapping_id, operator=operator.name)
    await session.commit()
    return ApiResponse.ok(data=SkuMappingVo.from_model(row), message="映射已恢复")


@router.post("/sku-mappings/batch", status_code=201, summary="批量导入映射（≤200）")
async def batch_upsert_mappings(
    payload: SkuMappingBatchRequest,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """按唯一键 upsert；单条失败不影响整批。"""
    result = await MappingService.batch_upsert(session, list(payload.items or []), operator=operator.name)
    await session.commit()
    return ApiResponse.ok(data=result)


# ======================================================================
#  校验与冲突
# ======================================================================


@router.post("/sku-mappings/validate", summary="★ 上架前强制映射校验（MAP-P0-02）")
async def validate_mappings(
    payload: MappingValidationRequest,
    session: DbSession,
) -> ApiResponse[MappingValidationVo]:
    """★ 预发布校验。

    `blocking=true` 时返回 **422**，但响应 `data` 仍是完整 `MappingValidationVo`
    （含 `missing_mappings[]` 与 `conflicts[]`），前端可直接渲染修复指引。

    ★ 检测顺序硬约束：`many_to_one`（跨平台铺货，**P1 永不拦截**）先跑并剔除，
      其余 P0 冲突才参与拦截判定 —— 避免把正常铺货误判为致命冲突。
    """
    vo = await MappingValidator.validate(
        session,
        source_product_id=payload.source_product_id,
        platform=payload.platform,
        shop_id=payload.shop_id,
        sku_codes=list(payload.sku_codes or []) or None,
    )
    if vo.blocking:
        raise BusinessError(
            vo.blocked_reason or "映射校验不通过，禁止上架",
            code=ErrorCode.MAPPING_CONFLICT,
            http_status=422,
            detail=vo.model_dump(),
        )
    return ApiResponse.ok(data=vo)


async def _fetch_shop_sku_codes(session: Any, rows: list[Any]) -> dict[int, str | None]:
    """批量预取冲突关联映射的店铺 SKU 编码，返回 {sku_mapping_id: shop_sku_code}。

    ★ 一次查完（批量预取），不在循环里逐行查询，避免 N+1。
      `sku_mapping_id` 可能为 0 或指向已删除映射 ⇒ 结果里查不到，
      schema 层如实渲染为「未知」，不伪造、不抛异常。
    """
    ids = {int(r.sku_mapping_id) for r in rows if getattr(r, "sku_mapping_id", None)}
    if not ids:
        return {}
    result = await session.execute(
        select(SkuMapping.id, SkuMapping.shop_sku_code).where(SkuMapping.id.in_(ids))
    )
    return {int(row[0]): row[1] for row in result.all()}


@router.get("/sku-mappings/conflicts", summary="冲突列表")
async def list_conflicts(
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    level: Annotated[str | None, Query(description="P0 / P1")] = None,
    conflict_type: Annotated[str | None, Query()] = None,
    is_resolved: Annotated[bool | None, Query()] = None,
) -> ApiResponse[Any]:
    """分页返回冲突记录。"""
    rows, total = await MappingService.list_conflicts(
        session,
        level=level,
        conflict_type=conflict_type,
        is_resolved=is_resolved,
        page=params.page,
        page_size=params.page_size,
    )
    code_by_id = await _fetch_shop_sku_codes(session, rows)
    return ApiResponse.ok(
        data=page_of(
            [MappingConflictVo.from_model(r, code_by_id.get(int(r.sku_mapping_id))) for r in rows],
            total,
            params,
        )
    )


@router.post("/sku-mappings/detect-conflicts", status_code=202, summary="触发冲突检测（异步）")
async def detect_conflicts(
    payload: DetectConflictsRequest,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """提交 `mapping_check` 任务（六类冲突检测 + 镜像成本同步）。"""
    task_record_id = await enqueue_task(
        TaskType.MAPPING_CHECK.value,
        {
            "source_product_ids": [int(i) for i in (payload.source_product_ids or [])],
            "all": bool(payload.all),
            "operator": operator.name,
        },
    )
    await session.commit()
    return ApiResponse.ok(
        data={"task_record_id": task_record_id, "accepted": True},
        message="冲突检测任务已受理",
    )


@router.get("/sku-mappings/pending", summary="待确认工单列表")
async def list_pending(
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
) -> ApiResponse[Any]:
    """「成本待确认 / 规格变更」等人工确认工单。"""
    rows, mappings, titles, total = await MappingService.list_pending(
        session, page=params.page, page_size=params.page_size
    )
    items = [
        MappingPendingVo.from_conflict(
            c,
            mapping=mappings.get(int(c.sku_mapping_id)) if c.sku_mapping_id else None,
            source_product_title=titles.get(int(c.sku_mapping_id)) if c.sku_mapping_id else None,
        )
        for c in rows
    ]
    return ApiResponse.ok(data=page_of(items, total, params))


@router.post("/sku-mappings/pending/{conflict_id}/resolve", summary="处理待确认工单")
async def resolve_pending(
    conflict_id: int,
    payload: MappingPendingResolveRequest,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[SkuMappingVo]:
    """confirm（接受货源新值）/ reject（保留人工值）/ manual_assign（人工改绑）。"""
    row = await MappingService.resolve_pending(
        session,
        conflict_id,
        action=payload.action,
        new_source_sku_code_1688=payload.new_source_sku_code_1688,
        note=payload.note,
        operator=operator.name,
    )
    await session.commit()
    return ApiResponse.ok(data=SkuMappingVo.from_model(row), message="工单已处理")


# ======================================================================
#  导入 / 导出 / 推送
# ======================================================================


@router.post("/sku-mappings/export", summary="导出映射 CSV")
async def export_mappings(
    session: DbSession,
    adapter_name: Annotated[str, Query(description="miaoshou / yitao / generic")] = "generic",
    platform: Annotated[str | None, Query()] = None,
    shop_id: Annotated[str | None, Query()] = None,
    updated_from: Annotated[str | None, Query(description="ISO8601")] = None,
    updated_to: Annotated[str | None, Query(description="ISO8601")] = None,
) -> ApiResponse[dict[str, Any]]:
    """导出 CSV 到 `data/exports/`，返回下载地址。"""
    result = await MappingService.export_mappings(
        session,
        platform=platform,
        shop_id=shop_id,
        adapter_name=adapter_name,
        updated_from=updated_from,
        updated_to=updated_to,
    )
    return ApiResponse.ok(data=result)


@router.post("/sku-mappings/import", summary="导入第三方映射 CSV")
async def import_mappings(
    session: DbSession,
    file: Annotated[UploadFile, File(description="第三方导出的映射 CSV")],
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """导入 CSV（必需列：platform / shop_id / shop_item_id / shop_sku_code）。"""
    content = await file.read()
    result = await MappingService.import_mappings(session, content, operator=operator.name)
    await session.commit()
    return ApiResponse.ok(data=result, message="映射导入完成")


@router.post("/sku-mappings/push", status_code=202, summary="推送映射到第三方")
async def push_mappings(
    payload: MappingPushRequest,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """★ 诚实降级：三个适配器推送能力均未实测，本接口**只导出 CSV**并把
    推送状态标为 `degraded`，**绝不伪装成 success**。
    """
    result = await MappingService.push_mappings(
        session,
        adapter_name=payload.adapter_name,
        ids=[int(i) for i in (payload.ids or [])] or None,
        all_valid=bool(payload.all_valid),
        operator=operator.name,
    )
    await session.commit()
    return ApiResponse.ok(data=result)


# ======================================================================
#  变更日志与统计
# ======================================================================


@router.get("/sku-mappings/stats", summary="映射统计")
async def mapping_stats(session: DbSession) -> ApiResponse[MappingStatsVo]:
    """总数 / 有效 / 待确认 / 失效 / P0 / P1 冲突 / 近期删除。"""
    return ApiResponse.ok(data=await MappingService.stats(session))


@router.get("/sku-mappings/{mapping_id}/logs", summary="映射变更历史")
async def mapping_change_logs(
    mapping_id: int,
    session: DbSession,
    include_system: Annotated[
        bool, Query(description="是否包含系统自动同步记录（默认排除，审计噪音控制）")
    ] = False,
) -> ApiResponse[list[MappingChangeLogVo]]:
    """★ 默认排除 `change_source='system'` 的自动成本同步记录。"""
    rows = await MappingService.list_change_logs(
        session, mapping_id, include_system=include_system
    )
    return ApiResponse.ok(data=[MappingChangeLogVo.from_model(r) for r in rows])
