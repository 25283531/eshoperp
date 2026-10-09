"""FastAPI 依赖注入集合：数据库会话 / trace_id / 当前操作者 / 管理员鉴权 / 设置。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.logging import get_trace_id
from app.core.security import OperatorContext, require_admin, resolve_operator

DbSession = Annotated[object, Depends(get_db)]
CurrentOperator = Annotated[OperatorContext, Depends(resolve_operator)]


def _resolve_admin(operator: CurrentOperator) -> OperatorContext:
    """管理员依赖：**必须**显式标注参数类型。

    ★ 踩坑记录：不能写成 `Depends(lambda op: require_admin(op))` ——
      lambda 的参数没有类型注解，FastAPI 不会把它解析为子依赖，
      而会当成**必填 query 参数 `op: str`**，导致 `require_admin()` 收到字符串后
      `operator.is_admin` 抛 AttributeError（全部管理端接口 500）。
    """
    return require_admin(operator)


AdminOperator = Annotated[OperatorContext, Depends(_resolve_admin)]
TraceId = Annotated[str, Depends(get_trace_id)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def get_current_user(operator: CurrentOperator) -> OperatorContext:
    """当前操作者（轻量鉴权，见 §11.2 N1）。"""
    return operator


def get_current_admin(operator: CurrentOperator) -> OperatorContext:
    """当前管理员操作者，非管理员抛 1003。"""
    return require_admin(operator)


def get_actor_name(operator: CurrentOperator) -> str:
    """当前操作者名称，用于审计日志 `operator` 字段。"""
    return operator.name


__all__ = [
    "AdminOperator",
    "AppSettings",
    "CurrentOperator",
    "DbSession",
    "TraceId",
    "get_actor_name",
    "get_current_admin",
    "get_current_user",
    "get_db",
    "get_settings",
    "get_trace_id",
    "require_admin",
    "resolve_operator",
]
