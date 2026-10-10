"""AI 重构任务与审核（ARCH §5.5.5）。

★ 硬约束 AIR-P0-03：只有 `review_status='approved'` 的产出可被上架任务引用
  （`POST /publish-tasks` 会校验，否则 422 / 4005）。

★ 用户决策 ②：AI 默认走 `file_bridge`；等待 WorkBuddy 产出超时抛
  `AiTimeoutError`，由 `TaskRunner` 按 `max_retry` 重试（**不在这里吞掉**）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.api.v1._common import enqueue_task, page_of
from app.core.deps import AdminOperator, CurrentOperator, DbSession
from app.core.pagination import PageParams, page_params
from app.core.response import ApiResponse
from app.adapters.ai.base import AiImagePrompt, AiInputPrompt
from app.models.enums import AiTaskType, TaskType
from app.schemas.asset import (
    AiConcurrencyUpdate,
    AiTaskCreate,
    AiTaskDetailVo,
    AiTaskResultVo,
    AiTaskReviewRequest,
    AiTaskVo,
    AiTitleSelectRequest,
)
from app.services.ai_task_service import AiTaskService

router = APIRouter(tags=["AI 重构"])

__all__ = ["router"]


@router.post("/ai-tasks", status_code=202, summary="批量创建 AI 任务（可指定能力类型与提示词）")
async def create_ai_tasks(
    payload: AiTaskCreate,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """创建任务并逐个提交异步执行。

    ★ `task_type` 决定跑哪种能力（`ai_rework` / `image_redraw` / `title_suggest` / `video_script`），
      默认 `ai_rework`（老调用方不带该字段时行为不变）。
    ★ 队列 `TaskType` 仍然**只用 `ai_rework` 一种**（见 `AiTaskService.AI_TASK_TYPE_DISPATCH` 的注释）：
      异步任务类型回答"由哪个 handler 执行"，AI 能力种类只记在 `ai_task.task_type` 上，
      不复制第二份真相源。
    """
    # ★ 逐图提示词：入参 schema → 数据契约 `AiImagePrompt`（键名以 `AiInputPrompt` 为准）
    image_prompts = [
        AiImagePrompt(
            index=int(item.index),
            prompt=item.prompt,
            asset_id=item.asset_id,
            source_path=item.source_path,
            tag=item.tag,
        )
        for item in (payload.image_prompts or [])
    ]
    tasks = await AiTaskService.create_tasks(
        session,
        source_product_ids=[int(i) for i in payload.source_product_ids],
        target_platform=payload.target_platform,
        rework_items=list(payload.rework_items or []) or None,
        template_version=payload.template_version,
        task_type=payload.task_type,
        input_prompt=AiInputPrompt(
            global_prompt=payload.global_prompt or "",
            images=image_prompts,
            title_prompt=payload.title_prompt or "",
            video_script_prompt=payload.video_script_prompt or "",
        ),
        operator=operator.name,
    )
    await session.commit()

    task_ids: list[int] = []
    task_record_ids: list[int] = []
    for task in tasks:
        record_id = await enqueue_task(
            TaskType.AI_REWORK.value,
            {"ai_task_id": int(task.id), "operator": operator.name},
        )
        task_ids.append(int(task.id))
        if record_id is not None:
            task_record_ids.append(record_id)

    resolved_type = payload.task_type or AiTaskType.AI_REWORK.value
    return ApiResponse.ok(
        data={"task_ids": task_ids, "task_record_ids": task_record_ids, "task_type": resolved_type},
        message=f"已创建 {len(task_ids)} 个 AI 任务（{resolved_type}）",
    )


@router.get("/ai-tasks/concurrency-config", summary="AI 并发与重试配置")
async def get_concurrency_config(session: DbSession) -> ApiResponse[dict[str, int]]:
    """当前并发上限与最大重试次数。"""
    return ApiResponse.ok(data=await AiTaskService.concurrency_config(session))


@router.put("/ai-tasks/concurrency-config", summary="修改 AI 并发配置（管理员）")
async def update_concurrency_config(
    payload: AiConcurrencyUpdate,
    session: DbSession,
    admin: AdminOperator,
) -> ApiResponse[dict[str, int]]:
    """修改并发 / 重试上限。"""
    data = await AiTaskService.update_concurrency_config(
        session,
        max_concurrency=payload.max_concurrency,
        max_retry=payload.max_retry,
        operator=admin.name,
    )
    await session.commit()
    return ApiResponse.ok(data=data)


@router.get("/ai-tasks", summary="AI 任务列表")
async def list_ai_tasks(
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    status: Annotated[str | None, Query()] = None,
    target_platform: Annotated[str | None, Query()] = None,
    source_product_id: Annotated[int | None, Query()] = None,
) -> ApiResponse[Any]:
    """分页返回 AI 任务。"""
    rows, titles, total = await AiTaskService.list_tasks(
        session,
        status=status,
        target_platform=target_platform,
        source_product_id=source_product_id,
        page=params.page,
        page_size=params.page_size,
    )
    items = [
        AiTaskVo.from_model(t, source_product_title=titles.get(int(t.source_product_id)))
        for t in rows
    ]
    return ApiResponse.ok(data=page_of(items, total, params))


@router.get("/ai-tasks/{task_id}", summary="AI 任务详情")
async def get_ai_task(task_id: int, session: DbSession) -> ApiResponse[AiTaskDetailVo]:
    """详情含 `result`（产出与审核状态）与 `assets[]`。"""
    task = await AiTaskService.get_task(session, task_id)
    result = await AiTaskService.latest_result(session, task_id)
    assets = await AiTaskService.list_result_assets(session, result) if result is not None else []

    from app.services.source_service import SourceService

    title = None
    try:
        product = await SourceService.get_source_product(session, int(task.source_product_id))
        title = product.title
    except Exception:  # noqa: BLE001  商品已删除时标题置空即可
        title = None

    base = AiTaskVo.from_model(task, source_product_title=title)
    detail = AiTaskDetailVo(
        **base.model_dump(),
        result=AiTaskResultVo.from_model(result, assets=assets) if result is not None else None,
        assets=[],
    )
    return ApiResponse.ok(data=detail)


@router.post("/ai-tasks/{task_id}/retry", summary="重试 AI 任务")
async def retry_ai_task(
    task_id: int,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """重试：状态回到 `queued` 并重新提交异步执行。"""
    task = await AiTaskService.retry(session, task_id, operator=operator.name)
    await session.commit()
    record_id = await enqueue_task(
        TaskType.AI_REWORK.value, {"ai_task_id": int(task.id), "operator": operator.name}
    )
    await session.commit()
    return ApiResponse.ok(data={"id": int(task.id), "status": task.status, "task_record_id": record_id})


@router.post("/ai-tasks/{task_id}/cancel", summary="取消 AI 任务")
async def cancel_ai_task(
    task_id: int,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """取消任务。"""
    task = await AiTaskService.cancel(session, task_id, operator=operator.name)
    await session.commit()
    return ApiResponse.ok(data={"id": int(task.id), "status": task.status})


@router.post("/ai-tasks/{task_id}/review", summary="审核 AI 产出")
async def review_ai_task(
    task_id: int,
    payload: AiTaskReviewRequest,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[AiTaskResultVo]:
    """审核产出：approve / reject / edit。只有 approve 后可被上架引用。"""
    edited = payload.edited.model_dump(exclude_none=True) if payload.edited else None
    result = await AiTaskService.review(
        session,
        task_id,
        action=payload.action,
        note=payload.note or "",
        edited=edited,
        operator=operator.name,
    )
    await session.commit()
    assets = await AiTaskService.list_result_assets(session, result)
    return ApiResponse.ok(data=AiTaskResultVo.from_model(result, assets=assets), message="审核已提交")


@router.post("/ai-tasks/{task_id}/select-title", summary="选定标题候选（写入下游发布链路读的 output_title）")
async def select_ai_title_candidate(
    task_id: int,
    payload: AiTitleSelectRequest,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[AiTaskResultVo]:
    """★ 把选中的那条候选写进 `AiTaskResult.output_title`。

    ★★ 为什么单独开这个端点（而不是复用 review 的 edit）★★
        `output_title` 是 **Basic 发布链路读标题的唯一字段**
        （`PublishService._build_payload()` / `_persist_listing()`）。
        只有把它写回去，"AI 出候选 → 人工选 → 上架用选中那条"才真正闭环；
        同时落 `selected_title_index` / `selected_title_at` / `selected_title_by` 三件套，
        保证事后能追溯"这条标题是谁在什么时候挑的第几条"。
    """
    result = await AiTaskService.select_title_candidate(
        session,
        task_id,
        index=payload.index,
        title=payload.title,
        note=payload.note or "",
        operator=operator.name,
    )
    await session.commit()
    assets = await AiTaskService.list_result_assets(session, result)
    return ApiResponse.ok(
        data=AiTaskResultVo.from_model(result, assets=assets),
        message=f"已选定标题候选 #{result.selected_title_index}",
    )
