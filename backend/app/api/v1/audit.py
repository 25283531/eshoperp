"""审计日志（ARCH §5.5.13）。

★ 审计噪音控制（v1.4）：系统自动产生的记录（如 `cost_sync` 成本自动同步）
  **默认排除** —— 否则人工改动会被淹没；传 `include_system=true` 查看全量。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.api.v1._common import page_of
from app.core.config import get_settings
from app.core.deps import DbSession
from app.core.pagination import PageParams, page_params
from app.core.response import ApiResponse
from app.schemas.system import AuditLogVo
from app.services.audit_service import AuditService
from app.utils.csvio import export_csv
from app.utils.kit import utc_now

router = APIRouter(tags=["审计"])

__all__ = ["router"]

EXPORT_HEADERS = [
    "id",
    "operator",
    "operator_role",
    "action_type",
    "object_type",
    "object_id",
    "old_value",
    "new_value",
    "ip",
    "trace_id",
    "remark",
    "is_handled",
    "handled_by",
    "handled_at",
    "created_at",
]


@router.get("/audit-logs", summary="审计日志列表")
async def list_audit_logs(
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    action_type: Annotated[str | None, Query()] = None,
    object_type: Annotated[str | None, Query()] = None,
    object_id: Annotated[str | None, Query()] = None,
    operator: Annotated[str | None, Query()] = None,
    created_from: Annotated[str | None, Query(description="ISO8601")] = None,
    created_to: Annotated[str | None, Query(description="ISO8601")] = None,
    include_system: Annotated[
        bool, Query(description="★ 是否包含系统自动记录（默认 false，审计噪音控制）")
    ] = False,
) -> ApiResponse[Any]:
    """分页返回审计日志。"""
    rows, total = await AuditService.list_logs(
        session,
        action_type=action_type,
        object_type=object_type,
        object_id=object_id,
        operator=operator,
        created_from=created_from,
        created_to=created_to,
        include_system=include_system,
        page=params.page,
        page_size=params.page_size,
    )
    return ApiResponse.ok(data=page_of([AuditLogVo.from_model(r) for r in rows], total, params))


@router.get("/audit-logs/export", summary="导出审计日志 CSV")
async def export_audit_logs(
    session: DbSession,
    action_type: Annotated[str | None, Query()] = None,
    object_type: Annotated[str | None, Query()] = None,
    object_id: Annotated[str | None, Query()] = None,
    operator: Annotated[str | None, Query()] = None,
    created_from: Annotated[str | None, Query(description="ISO8601")] = None,
    created_to: Annotated[str | None, Query(description="ISO8601")] = None,
    include_system: Annotated[bool, Query()] = False,
) -> ApiResponse[dict[str, Any]]:
    """按当前筛选条件导出 CSV 到 `data/exports/`。"""
    rows, _total = await AuditService.list_logs(
        session,
        action_type=action_type,
        object_type=object_type,
        object_id=object_id,
        operator=operator,
        created_from=created_from,
        created_to=created_to,
        include_system=include_system,
        page=1,
        page_size=10000,
    )
    payload = [
        {
            "id": int(r.id or 0),
            "operator": r.operator or "",
            "operator_role": r.operator_role or "",
            "action_type": r.action_type or "",
            "object_type": r.object_type or "",
            "object_id": r.object_id or "",
            "old_value": r.old_value or "",
            "new_value": r.new_value or "",
            "ip": r.ip or "",
            "trace_id": r.trace_id or "",
            "remark": r.remark or "",
            "is_handled": 1 if r.is_handled else 0,
            "handled_by": r.handled_by or "",
            "handled_at": r.handled_at.isoformat() if r.handled_at else "",
            "created_at": r.created_at.isoformat() if r.created_at else "",
        }
        for r in rows
    ]
    filename = f"audit_logs_{utc_now().strftime('%Y%m%d%H%M%S')}.csv"
    path = export_csv(payload, get_settings().exports_dir / filename, headers=list(EXPORT_HEADERS))
    return ApiResponse.ok(
        data={
            "download_url": f"/api/v1/files/exports/{filename}",
            "row_count": len(payload),
            "path": path,
        }
    )
