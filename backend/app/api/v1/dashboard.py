"""工作台 / 健康检查 / ★ 全局顶栏聚合（ARCH §5.5.1）。

★ ★ ★ `GET /system/status-bar` 实现约束（PRD v1.3 验收标准，非建议）★ ★ ★
    1. **只读 `SystemSetting` 缓存与 `audit_log` 未处理计数**；
    2. **绝不触发任何外部 HTTP 调用**（顶栏 60s 轮询会把第三方 API 打爆）；
    3. **禁止拆成多个接口轮询** —— 新增顶栏状态项必须并入本响应体。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.core.deps import DbSession
from app.core.response import ApiResponse
from app.schemas.system import StatusBarVo
from app.services.system_service import SystemService

router = APIRouter(tags=["工作台"])

__all__ = ["router"]


@router.get("/dashboard/summary", summary="工作台聚合数据")
async def dashboard_summary(session: DbSession) -> ApiResponse[dict[str, Any]]:
    """工作台首页聚合：待办数 / 健康红绿灯 / 队列积压。"""
    data = await SystemService.dashboard_summary(session)
    return ApiResponse.ok(data=data)


@router.get("/health", summary="健康检查（API 层）")
async def health(session: DbSession) -> ApiResponse[dict[str, Any]]:
    """健康检查：**不主动探测第三方**（顶栏轮询安全）。"""
    data = await SystemService.health(session)
    return ApiResponse.ok(data=data)


@router.get("/system/status-bar", summary="★ 全局顶栏聚合（单次轻量轮询）")
async def status_bar(session: DbSession) -> ApiResponse[StatusBarVo]:
    """★ 顶栏专用聚合接口：越权红点 + 上架模式 + 履约渠道 + 健康状态。

    实现见 `SystemService.status_bar()`：**零外部 HTTP**。
    """
    vo = await SystemService.status_bar(session)
    return ApiResponse.ok(data=vo)
