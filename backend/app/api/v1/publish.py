"""上架任务与半自动模式（ARCH §5.5.7）。

★ 用户决策 ①：个体户 / 个人身份证店 → **半自动（manual）是主路径**，
  真实平台适配器保持 skeleton + TODO。半自动链路必须完整可用：
  ZIP 素材包 → 预填表单 → 商品 ID 回填（★ `sale_price` 必填）→ 自动建映射。

★ `PublishService.execute()` 是全系统**唯一**的 `ListingAdapter.publish()` 调用点，
  且前置 `MappingValidator.validate()` 硬拦截（附录 A 第 3 条，无绕过）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from app.api.v1._common import page_of
from app.core.deps import CurrentOperator, DbSession
from app.core.errors import NotFoundError
from app.core.pagination import PageParams, page_params
from app.core.response import ApiResponse
from app.schemas.listing import (
    PublishTaskBatchRequest,
    PublishTaskCreate,
    PublishTaskDetailVo,
    PublishTaskVo,
)
from app.schemas.mapping import FillBackRequest
from app.services.publish_service import PublishService

router = APIRouter(tags=["上架任务"])

__all__ = ["router"]


@router.post("/publish-tasks", status_code=202, summary="创建上架任务")
async def create_publish_tasks(
    payload: PublishTaskCreate,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """创建并异步执行。

    ★ AIR-P0-03：`ai_task_result_ids` 对应的产出未 `approved` → **422 / 4005**。
    """
    tasks, task_record_ids = await PublishService.create_tasks(
        session,
        source_product_ids=[int(i) for i in payload.source_product_ids],
        platform=payload.platform,
        shop_id=payload.shop_id,
        mode=payload.mode,
        ai_task_result_ids=payload.ai_task_result_ids,
        operator=operator.name,
        submit_async=True,
    )
    await session.commit()
    return ApiResponse.ok(
        data={
            "task_ids": [int(t.id) for t in tasks],
            "task_record_ids": [int(i) for i in task_record_ids],
        },
        message=f"已创建 {len(tasks)} 个上架任务",
    )


@router.post("/publish-tasks/batch", status_code=202, summary="批量创建上架任务（≤50）")
async def batch_create_publish_tasks(
    payload: PublishTaskBatchRequest,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """批量创建（与单个创建同一服务方法，保证校验规则一致）。"""
    tasks, task_record_ids = await PublishService.create_tasks(
        session,
        source_product_ids=[int(i) for i in payload.source_product_ids],
        platform=payload.platform,
        shop_id=payload.shop_id,
        mode=payload.mode,
        operator=operator.name,
        submit_async=True,
    )
    await session.commit()
    return ApiResponse.ok(
        data={
            "task_ids": [int(t.id) for t in tasks],
            "task_record_ids": [int(i) for i in task_record_ids],
        }
    )


@router.get("/publish-tasks", summary="上架任务列表")
async def list_publish_tasks(
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    platform: Annotated[str | None, Query()] = None,
    shop_id: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    mode: Annotated[str | None, Query()] = None,
    source_product_id: Annotated[int | None, Query()] = None,
) -> ApiResponse[Any]:
    """分页返回上架任务。"""
    rows, titles, total = await PublishService.list_tasks(
        session,
        platform=platform,
        shop_id=shop_id,
        status=status,
        mode=mode,
        source_product_id=source_product_id,
        page=params.page,
        page_size=params.page_size,
    )
    items = [
        PublishTaskVo.from_model(t, source_product_title=titles.get(int(t.source_product_id)))
        for t in rows
    ]
    return ApiResponse.ok(data=page_of(items, total, params))


@router.get("/publish-tasks/{task_id}", summary="上架任务详情")
async def get_publish_task(task_id: int, session: DbSession) -> ApiResponse[PublishTaskDetailVo]:
    """详情含 `precheck_result` / `validate_result` / `error_advice`。"""
    task = await PublishService.get_task(session, task_id)
    vo = PublishTaskDetailVo.from_model(task)
    return ApiResponse.ok(data=vo)


@router.post("/publish-tasks/{task_id}/precheck", summary="合规预检")
async def precheck_publish_task(
    task_id: int,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """预检：标题 / 素材 / 违禁词（不触碰平台）。"""
    result = await PublishService.precheck(session, task_id, operator=operator.name)
    await session.commit()
    return ApiResponse.ok(data=result)


@router.post("/publish-tasks/{task_id}/retry", summary="重试上架任务")
async def retry_publish_task(
    task_id: int,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """回到 `pending_precheck` 并重新执行（仍经映射校验）。"""
    task = await PublishService.retry(session, task_id, operator=operator.name)
    await session.commit()
    return ApiResponse.ok(data=PublishService.task_summary(task))


@router.post("/publish-tasks/{task_id}/cancel", summary="取消上架任务")
async def cancel_publish_task(
    task_id: int,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """取消任务。"""
    task = await PublishService.cancel(session, task_id, operator=operator.name)
    await session.commit()
    return ApiResponse.ok(data={"id": int(task.id), "status": task.status})


# ======================================================================
#  半自动模式（★ 主路径）
# ======================================================================


@router.get("/publish-tasks/manual/{task_id}/package", summary="下载半自动素材包（ZIP）")
async def download_manual_package(task_id: int, session: DbSession) -> FileResponse:
    """ZIP：图片 + `form_data.json` + `README.txt`。"""
    package = await PublishService.build_manual_package(session, task_id)
    await session.commit()
    path = Path(getattr(package, "package_path", "") or "")
    if not path.exists():
        raise NotFoundError(f"素材包不存在（task {task_id}）")
    return FileResponse(path=str(path), filename=path.name, media_type="application/zip")


@router.get("/publish-tasks/manual/{task_id}/form-data", summary="半自动预填表单数据")
async def manual_form_data(task_id: int, session: DbSession) -> ApiResponse[dict[str, Any]]:
    """可直接复制到平台后台的表单数据（含 `copy_text`）。"""
    return ApiResponse.ok(data=await PublishService.form_data(session, task_id))


@router.post("/publish-tasks/manual/{task_id}/fill-back", summary="★ 回填商品 ID 并建映射")
async def fill_back_manual(
    task_id: int,
    payload: FillBackRequest,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """★ v1.5 `sale_price` 必填（PRD LST-P0-07）：缺失或 ≤ 0 → **422**。

    回填成功后自动建 `SkuMapping`，并触发 `cost_underwater` 重算。
    """
    result = await PublishService.fill_back(session, task_id, payload, operator=operator.name)
    await session.commit()
    return ApiResponse.ok(data=result, message="商品 ID 回填成功，映射已建立")
