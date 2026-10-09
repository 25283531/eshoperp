"""统一响应体与分页封装（§10.2）。

    {
      "code": 0,
      "message": "ok",
      "data": {...},
      "trace_id": "..."
    }
"""

from __future__ import annotations

from typing import Any, Generic, Sequence, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """统一 API 响应体。`code=0` 表示成功。"""

    model_config = ConfigDict(populate_by_name=True)

    code: int = Field(default=0, description="业务错误码，0 表示成功")
    message: str = Field(default="ok", description="中文提示，可直接 toast 展示")
    data: T | None = Field(default=None, description="业务数据，失败时可为 null")
    trace_id: str = Field(default="", description="全链路追踪 ID")

    @classmethod
    def ok(cls, data: T | None = None, message: str = "ok", trace_id: str = "") -> "ApiResponse[T]":
        """构造成功响应。trace_id 缺省时自动取当前上下文（TraceIdMiddleware 注入）。"""
        return cls(code=0, message=message, data=data, trace_id=trace_id or current_trace_id())

    @classmethod
    def fail(cls, code: int, message: str, data: Any = None, trace_id: str = "") -> "ApiResponse[Any]":
        """构造失败响应。trace_id 缺省时自动取当前上下文。"""
        return cls(code=int(code), message=message, data=data, trace_id=trace_id or current_trace_id())

    @property
    def success(self) -> bool:
        """是否成功。"""
        return self.code == 0


class PageResult(BaseModel, Generic[T]):
    """分页结果：`{items, total, page, page_size, total_pages}`。"""

    items: list[T] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    total_pages: int = 0

    @classmethod
    def build(cls, items: Sequence[T], total: int, page: int, page_size: int) -> "PageResult[T]":
        """按总数与分页参数构造分页结果。"""
        safe_page = max(int(page), 1)
        safe_size = max(int(page_size), 1)
        total_pages = (int(total) + safe_size - 1) // safe_size if total else 0
        return cls(
            items=list(items),
            total=int(total),
            page=safe_page,
            page_size=safe_size,
            total_pages=total_pages,
        )


def current_trace_id() -> str:
    """取当前上下文 trace_id（延迟导入，避免与 logging 模块的循环依赖）。"""
    from app.core.logging import get_trace_id

    try:
        return get_trace_id()
    except Exception:  # noqa: BLE001  trace_id 缺失不应影响响应构建
        return ""


def ok_response(data: Any = None, message: str = "ok", trace_id: str = "") -> ApiResponse[Any]:
    """快捷构造成功响应。"""
    return ApiResponse.ok(data=data, message=message, trace_id=trace_id)


def fail_response(code: int, message: str, data: Any = None, trace_id: str = "") -> ApiResponse[Any]:
    """快捷构造失败响应。"""
    return ApiResponse.fail(code=code, message=message, data=data, trace_id=trace_id)


def page_response(items: Sequence[Any], total: int, page: int, page_size: int, trace_id: str = "") -> ApiResponse[PageResult[Any]]:
    """快捷构造分页响应。"""
    return ApiResponse.ok(data=PageResult.build(items, total, page, page_size), trace_id=trace_id)


__all__ = [
    "ApiResponse",
    "PageResult",
    "current_trace_id",
    "fail_response",
    "ok_response",
    "page_response",
]
