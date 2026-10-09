"""适配器与权限（ARCH §5.5.12）。

★ 红线 R1：`scope` 白名单硬校验落在适配器工厂（`enforce_scope_with_audit()`），
  越权声明直接拒绝并埋 `permission_change` 审计（5003）。
★ SYS-P0-06：越权告警**处置态**写入 `audit_log.is_handled / handled_by / handled_at`，
  处置动作**再埋一条**同类型审计（「被拒」与「被处置」成对存在）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.adapters.listing.factory import ListingAdapterFactory, resolve_listing_mode
from app.api.v1._common import page_of
from app.core.deps import AdminOperator, CurrentOperator, DbSession
from app.core.pagination import PageParams, page_params
from app.core.response import ApiResponse
from app.schemas.system import (
    AdapterSwitchRequest,
    FulfillmentAdapterConfigUpdate,
    FulfillmentAdapterVo,
    ListingModeUpdate,
    ScopeViolationHandleRequest,
    ScopeViolationVo,
)
from app.services.audit_service import AuditService
from app.services.fulfillment_service import FulfillmentService
from app.services.system_service import SystemService

router = APIRouter(tags=["适配器"])

__all__ = ["router"]


# ======================================================================
#  上架适配器
# ======================================================================


@router.get("/adapters/listing", summary="上架适配器与当前模式")
async def list_listing_adapters(session: DbSession) -> ApiResponse[dict[str, Any]]:
    """当前上架模式 + 各平台适配器可用性（★ 未取得资质会降级为 Mock）。"""
    mode = await resolve_listing_mode(session)
    return ApiResponse.ok(
        data={
            "current_mode": mode,
            "adapters": ListingAdapterFactory.list_adapters(),
        }
    )


@router.put("/adapters/listing/mode", summary="切换上架模式（管理员）")
async def switch_listing_mode(
    payload: ListingModeUpdate,
    session: DbSession,
    admin: AdminOperator,
) -> ApiResponse[dict[str, Any]]:
    """切换 real / mock / manual（写审计，前端顶栏会即时刷新）。"""
    result = await SystemService.set_listing_mode(
        session, payload.mode, reason=payload.reason, operator=admin.name
    )
    await session.commit()
    return ApiResponse.ok(data=result, message=f"上架模式已切换为 {payload.mode}")


# ======================================================================
#  履约适配器
# ======================================================================


@router.get("/adapters/fulfillment", summary="履约适配器列表")
async def list_fulfillment_adapters(session: DbSession) -> ApiResponse[dict[str, Any]]:
    """当前生效适配器 + 三个适配器的能力矩阵与 scope 校验状态。"""
    active, rows = await FulfillmentService.list_adapters(session)
    return ApiResponse.ok(
        data={
            "active_adapter": active,
            "adapters": [
                FulfillmentAdapterVo.from_model(r, is_active=(r.adapter_name == active))
                for r in rows
            ],
        }
    )


@router.get(
    "/adapters/fulfillment/{adapter_name}/capabilities", summary="适配器能力矩阵"
)
async def adapter_capabilities(
    adapter_name: str, session: DbSession
) -> ApiResponse[dict[str, Any]]:
    """能力 manifest（SUPPORTED / DEGRADED / UNSUPPORTED + fallback + unverified）。"""
    return ApiResponse.ok(data=await FulfillmentService.capabilities(session, adapter_name))


@router.put("/adapters/fulfillment/{adapter_name}/config", summary="配置适配器（管理员）")
async def update_adapter_config(
    adapter_name: str,
    payload: FulfillmentAdapterConfigUpdate,
    session: DbSession,
    admin: AdminOperator,
) -> ApiResponse[FulfillmentAdapterVo]:
    """★ 声明越权 scope → **403 / 5003**（红线 R1）。"""
    row = await FulfillmentService.update_config(
        session,
        adapter_name,
        credential_id=payload.credential_id,
        config_json=payload.config_json,
        declared_scopes=payload.declared_scopes,
        is_enabled=payload.is_enabled,
        operator=admin.name,
    )
    await session.commit()
    active, _rows = await FulfillmentService.list_adapters(session)
    return ApiResponse.ok(
        data=FulfillmentAdapterVo.from_model(row, is_active=(row.adapter_name == active)),
        message="适配器配置已更新",
    )


@router.post("/adapters/fulfillment/{adapter_name}/test", summary="适配器连通性自检")
async def test_adapter(
    adapter_name: str,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """连通性自检（适配器不可用时返回 503）。"""
    return ApiResponse.ok(
        data=await FulfillmentService.test_adapter(session, adapter_name, actor=operator.name)
    )


@router.post("/adapters/fulfillment/switch", summary="热切换履约适配器（管理员）")
async def switch_adapter(
    payload: AdapterSwitchRequest,
    session: DbSession,
    admin: AdminOperator,
) -> ApiResponse[dict[str, Any]]:
    """★ FUL-P0-05：只改系统配置；已落库订单的 `adapter_name` **不变**，在途订单按原渠道跑完。"""
    result = await FulfillmentService.switch(
        session,
        payload.adapter_name,
        reason=payload.reason or "",
        drain_inflight=bool(payload.drain_inflight),
        operator=admin.name,
    )
    await session.commit()
    return ApiResponse.ok(data=result, message=f"履约适配器已切换为 {payload.adapter_name}")


@router.get("/adapters/scope-policies", summary="scope 白名单 / 黑名单")
async def scope_policies() -> ApiResponse[dict[str, Any]]:
    """★ 第三方**永不**持有商品编辑权：`forbidden` 中的 scope 一律拒绝。"""
    return ApiResponse.ok(data=FulfillmentService.scope_policies())


# ======================================================================
#  越权告警（SYS-P0-05 / SYS-P0-06）
# ======================================================================


@router.get("/adapters/violations", summary="越权告警记录")
async def list_violations(
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    is_unhandled: Annotated[
        bool | None, Query(description="★ true：仅未处置（供顶栏红点判定）")
    ] = None,
) -> ApiResponse[Any]:
    """越权记录**不新建表**，复用 `audit_log`（`action_type='permission_change'`）。"""
    rows, total = await AuditService.list_violations(
        session, is_unhandled=is_unhandled, page=params.page, page_size=params.page_size
    )
    return ApiResponse.ok(
        data=page_of([ScopeViolationVo.from_audit(r) for r in rows], total, params)
    )


@router.post("/adapters/violations/{violation_id}/handle", summary="处置越权告警（管理员）")
async def handle_violation(
    violation_id: int,
    payload: ScopeViolationHandleRequest,
    session: DbSession,
    admin: AdminOperator,
) -> ApiResponse[dict[str, Any]]:
    """★ 处置后红点计数 -1，并**再埋一条** `permission_change` 审计（成对留痕）。"""
    row = await AuditService.handle_violation(
        session,
        violation_id,
        operator=admin.name,
        handle_note=payload.handle_note or "",
    )
    await session.commit()
    return ApiResponse.ok(
        data={
            "id": int(row.id),
            "is_handled": bool(row.is_handled),
            "handled_by": row.handled_by,
            "handled_at": row.handled_at.isoformat() if row.handled_at else None,
        },
        message="越权告警已处置",
    )
