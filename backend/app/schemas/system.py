"""系统设置 / 审计 / 任务 / 适配器 Schema（§5.5.12、§5.5.13）。

★ `StatusBarVo` —— 全局顶栏专用聚合接口（SYS-P0-05）：
   只读 `SystemSetting` 缓存与 `audit_log` 未处理计数，**绝不触发外部 HTTP**。
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.schemas.common import BaseSchema, iso_or_none

__all__ = [
    "AdapterSwitchRequest",
    "AuditLogVo",
    "FulfillmentAdapterConfigUpdate",
    "FulfillmentAdapterVo",
    "ListingModeUpdate",
    "ScopeViolationHandleRequest",
    "ScopeViolationVo",
    "SettingUpdate",
    "SettingVo",
    "StatusBarLatestViolation",
    "StatusBarUnhealthyItem",
    "StatusBarVo",
    "TaskRecordVo",
]


# ---------------------------------------------------------------------------
#  顶栏聚合（SYS-P0-05）
# ---------------------------------------------------------------------------


class StatusBarLatestViolation(BaseSchema):
    """顶栏：最近一条未处理越权记录。"""

    id: int = 0
    adapter_name: str = ""
    denied_scopes: list[str] = Field(default_factory=list)
    created_at: str | None = None


class StatusBarUnhealthyItem(BaseSchema):
    """顶栏：不健康项。"""

    name: str = ""
    status: str = ""
    message: str = ""


class StatusBarVo(BaseSchema):
    """★ `GET /system/status-bar` 响应体（单次响应 < 1KB，前端 60s 轮询）。

    ★ 禁止性约束：顶栏状态**只能来自本接口**，不得再挂新轮询接口。
      后续若新增顶栏状态项（如队列积压数），**必须并入本响应体**。
    """

    # ① 越权告警红点（> 0 时常亮，全部处置后熄灭）
    unhandled_violation_count: int = 0
    latest_violation: StatusBarLatestViolation | None = None

    # ② Mock 模式标识（防止运营误以为商品真的上到平台）
    listing_mode: str = "manual"
    listing_mode_label: str = "半自动"
    is_mock_active: bool = False
    active_fulfillment_adapter: str = "local_csv"
    active_fulfillment_adapter_label: str = "本地兜底"

    # ③ 系统健康
    health_status: str = "healthy"  # healthy / degraded / down
    unhealthy_items: list[StatusBarUnhealthyItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
#  系统设置
# ---------------------------------------------------------------------------


class SettingVo(BaseSchema):
    """单项系统配置。"""

    key: str = ""
    value: str | None = None
    value_type: str = "string"
    description: str | None = None
    updated_by: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_model(cls, model: Any) -> "SettingVo":
        """从 ORM 对象构造。"""
        return cls(
            key=model.setting_key or "",
            value=model.setting_value,
            value_type=model.value_type or "string",
            description=model.description,
            updated_by=model.updated_by,
            updated_at=iso_or_none(model.updated_at),
        )


class SettingUpdate(BaseSchema):
    """更新系统配置（管理员）。"""

    value: str = Field(..., description="配置值（字符串形态，按 value_type 解析）")
    reason: str | None = None


# ---------------------------------------------------------------------------
#  审计日志
# ---------------------------------------------------------------------------


class AuditLogVo(BaseSchema):
    """审计日志响应体（含越权处置态四字段）。"""

    id: int = 0
    operator: str = ""
    operator_role: str | None = None
    action_type: str = ""
    object_type: str = ""
    object_id: str | None = None
    old_value: str | None = None
    new_value: str | None = None
    ip: str | None = None
    trace_id: str | None = None
    remark: str | None = None
    # ★ v1.2 处置态（仅 permission_change 使用）
    is_handled: bool = False
    handled_by: str | None = None
    handled_at: str | None = None
    handle_note: str | None = None
    created_at: str | None = None

    @classmethod
    def from_model(cls, model: Any) -> "AuditLogVo":
        """从 ORM 对象构造。"""
        return cls(
            id=int(model.id or 0),
            operator=model.operator or "",
            operator_role=model.operator_role,
            action_type=model.action_type or "",
            object_type=model.object_type or "",
            object_id=model.object_id,
            old_value=model.old_value,
            new_value=model.new_value,
            ip=model.ip,
            trace_id=model.trace_id,
            remark=model.remark,
            is_handled=bool(model.is_handled),
            handled_by=model.handled_by,
            handled_at=iso_or_none(model.handled_at),
            handle_note=model.handle_note,
            created_at=iso_or_none(model.created_at),
        )


class ScopeViolationVo(BaseSchema):
    """★ 越权告警记录（源表 `audit_log` 中 `action_type='permission_change'`）。

    ★ 不新建表：越权本身就是必须留痕的审计事件，单独建表会产生
      「审计里有、告警列表里没有」的双份真相。
    """

    id: int = 0
    adapter_name: str = ""
    platform: str | None = None
    denied_scopes: list[str] = Field(default_factory=list)
    declared_scopes: list[str] = Field(default_factory=list)
    message: str = ""
    operator: str = ""
    created_at: str | None = None
    trace_id: str | None = None
    # ★ 处置态（SYS-P0-06）
    is_handled: bool = False
    handled_by: str | None = None
    handled_at: str | None = None
    handle_note: str | None = None

    @classmethod
    def from_audit(cls, model: Any) -> "ScopeViolationVo":
        """从 `AuditLog` 记录解析越权详情。

        `new_value` 结构（由 scope_guard 写入）：
            `{"scope_check_status": "rejected",
              "detail": {"declared_scopes": [...], "forbidden_scopes": [...], "unknown_scopes": [...]}}`
        """
        detail: dict[str, Any] = {}
        raw = model.new_value
        if isinstance(raw, dict):
            detail = raw.get("detail") if isinstance(raw.get("detail"), dict) else raw
        elif isinstance(raw, str) and raw.strip():
            import json

            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    detail = parsed.get("detail") if isinstance(parsed.get("detail"), dict) else parsed
            except ValueError:
                detail = {}

        denied = detail.get("forbidden_scopes") or []
        unknown = detail.get("unknown_scopes") or []
        return cls(
            id=int(model.id or 0),
            adapter_name=model.object_id or "",
            platform=None,
            denied_scopes=[str(s) for s in denied] + [str(s) for s in unknown],
            declared_scopes=[str(s) for s in (detail.get("declared_scopes") or [])],
            message=model.remark or "",
            operator=model.operator or "",
            created_at=iso_or_none(model.created_at),
            trace_id=model.trace_id,
            is_handled=bool(model.is_handled),
            handled_by=model.handled_by,
            handled_at=iso_or_none(model.handled_at),
            handle_note=model.handle_note,
        )


class ScopeViolationHandleRequest(BaseSchema):
    """处置越权告警（★ 仅管理员）。"""

    handle_note: str | None = Field(default=None, max_length=512, description="处置说明（写审计）")


# ---------------------------------------------------------------------------
#  适配器
# ---------------------------------------------------------------------------


class FulfillmentAdapterVo(BaseSchema):
    """履约适配器响应体（§5.5.12）。"""

    id: int = 0
    adapter_name: str = ""
    display_name: str = ""
    capability_json: dict[str, Any] = Field(default_factory=dict)
    declared_scopes: list[str] = Field(default_factory=list)
    scope_check_status: str = "passed"
    scope_check_message: str | None = None
    is_enabled: bool = False
    is_active: bool = False
    health_status: str = "unknown"
    last_heartbeat_at: str | None = None
    heartbeat_fail_count: int = 0
    config_json: dict[str, Any] | None = None

    @classmethod
    def from_model(cls, model: Any, *, is_active: bool = False) -> "FulfillmentAdapterVo":
        """从 ORM 对象构造。"""
        return cls(
            id=int(model.id or 0),
            adapter_name=model.adapter_name or "",
            display_name=model.display_name or "",
            capability_json=dict(model.capability_json or {}),
            declared_scopes=[str(s) for s in (model.declared_scopes_json or [])],
            scope_check_status=model.scope_check_status or "passed",
            scope_check_message=model.scope_check_message,
            is_enabled=bool(model.is_enabled),
            is_active=bool(is_active),
            health_status=model.health_status or "unknown",
            last_heartbeat_at=iso_or_none(model.last_heartbeat_at),
            heartbeat_fail_count=int(model.heartbeat_fail_count or 0),
            config_json=model.config_json,
        )


class FulfillmentAdapterConfigUpdate(BaseSchema):
    """更新适配器配置（★ 保存声明 scope 时走 scope_guard，越权 403 / 5003）。"""

    credential_id: int | None = None
    config_json: dict[str, Any] | None = None
    declared_scopes: list[str] | None = None
    is_enabled: bool | None = None


class AdapterSwitchRequest(BaseSchema):
    """切换生效适配器。"""

    adapter_name: str = Field(..., description="miaoshou / yitao / local_csv")
    reason: str = Field(default="", max_length=255, description="切换原因（写审计）")
    drain_inflight: bool = True


# ---------------------------------------------------------------------------
#  异步任务
# ---------------------------------------------------------------------------


class TaskRecordVo(BaseSchema):
    """异步任务响应体。"""

    id: int = 0
    task_type: str = ""
    task_key: str | None = None
    status: str = "pending"
    priority: int = 5
    retry_count: int = 0
    max_retry: int = 3
    scheduled_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    result_json: dict[str, Any] | None = None
    trace_id: str | None = None
    created_at: str | None = None

    @classmethod
    def from_model(cls, model: Any) -> "TaskRecordVo":
        """从 ORM 对象构造。"""
        return cls(
            id=int(model.id or 0),
            task_type=model.task_type or "",
            task_key=model.task_key,
            status=model.status or "pending",
            priority=int(model.priority or 5),
            retry_count=int(model.retry_count or 0),
            max_retry=int(model.max_retry or 3),
            scheduled_at=iso_or_none(model.scheduled_at),
            started_at=iso_or_none(model.started_at),
            finished_at=iso_or_none(model.finished_at),
            duration_ms=model.duration_ms,
            error_code=model.error_code,
            error_message=model.error_message,
            result_json=model.result_json,
            trace_id=model.trace_id,
            created_at=iso_or_none(model.created_at),
        )


class ListingModeUpdate(BaseSchema):
    """切换上架模式（`PUT /adapters/listing/mode`）。"""

    mode: str = Field(..., description="real / mock / manual")
    reason: str = Field(default="", max_length=255, description="切换原因（写审计）")
