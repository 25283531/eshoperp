"""异步任务查询与运维操作（ARCH §5.5.13）。

长任务（采集 / AI 重构 / 上架 / 冲突检测 / 订单同步 / 库存同步）提交后返回 202 + `task_record_id`，
前端轮询本模块的 `GET /tasks/{id}` 获取进度与结果。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.api.v1._common import page_of
from app.core.deps import DbSession
from app.core.errors import NotFoundError
from app.core.pagination import PageParams, page_params
from app.core.response import ApiResponse
from app.models.enums import TaskStatus
from app.models.task import TaskRecord
from app.schemas.system import TaskRecordVo

router = APIRouter(tags=["任务"])

__all__ = ["router"]


@router.get("/tasks", summary="任务列表")
async def list_tasks(
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    task_type: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
) -> ApiResponse[Any]:
    """分页返回任务记录。"""
    stmt = select(TaskRecord)
    if task_type:
        stmt = stmt.where(TaskRecord.task_type == task_type)
    if status:
        stmt = stmt.where(TaskRecord.status == status)
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = int((await session.execute(count_stmt)).scalar_one() or 0)
    rows = (
        (await session.execute(stmt.order_by(TaskRecord.id.desc()).offset(params.offset).limit(params.limit)))
        .scalars()
        .all()
    )
    return ApiResponse.ok(data=page_of([TaskRecordVo.from_model(r) for r in rows], total, params))


@router.get("/tasks/{task_id}", summary="任务详情")
async def get_task(task_id: int, session: DbSession) -> ApiResponse[TaskRecordVo]:
    """任务详情（含 `error_message` 与 `result_json`）。"""
    row = (
        await session.execute(select(TaskRecord).where(TaskRecord.id == int(task_id)))
    ).scalars().first()
    if row is None:
        raise NotFoundError(f"任务 {task_id} 不存在")
    return ApiResponse.ok(data=TaskRecordVo.from_model(row))


@router.post("/tasks/{task_id}/retry", summary="重试任务")
async def retry_task(task_id: int, session: DbSession) -> ApiResponse[dict[str, Any]]:
    """重新入队（状态回到 `pending`）。"""
    row = (
        await session.execute(select(TaskRecord).where(TaskRecord.id == int(task_id)))
    ).scalars().first()
    if row is None:
        raise NotFoundError(f"任务 {task_id} 不存在")
    if row.status not in {TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}:
        from app.core.errors import StateConflictError

        raise StateConflictError(f"任务状态 {row.status} 不允许重试")

    from app.tasks.runner import get_task_runner

    runner = get_task_runner()
    ok = await runner.retry(int(task_id))
    await session.commit()
    return ApiResponse.ok(data={"id": int(task_id), "status": "pending" if ok else row.status})


@router.post("/tasks/{task_id}/cancel", summary="取消任务")
async def cancel_task(task_id: int, session: DbSession) -> ApiResponse[dict[str, Any]]:
    """取消未开始的任务。"""
    row = (
        await session.execute(select(TaskRecord).where(TaskRecord.id == int(task_id)))
    ).scalars().first()
    if row is None:
        raise NotFoundError(f"任务 {task_id} 不存在")

    from app.tasks.runner import get_task_runner

    runner = get_task_runner()
    ok = await runner.cancel(int(task_id))
    await session.commit()
    return ApiResponse.ok(data={"id": int(task_id), "status": "cancelled" if ok else row.status})
