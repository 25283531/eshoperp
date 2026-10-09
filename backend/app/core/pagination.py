"""分页参数与分页结果构造工具（§10.2）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from fastapi import Query
from sqlalchemy import Select, func, select

from app.core.response import PageResult

DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200


@dataclass
class PageParams:
    """分页参数。`offset()` / `limit()` 供 SQL 使用。"""

    page: int = DEFAULT_PAGE
    page_size: int = DEFAULT_PAGE_SIZE

    def __post_init__(self) -> None:
        """规范化分页参数，防止越界。"""
        self.page = max(int(self.page or DEFAULT_PAGE), 1)
        self.page_size = min(max(int(self.page_size or DEFAULT_PAGE_SIZE), 1), MAX_PAGE_SIZE)

    @property
    def offset(self) -> int:
        """SQL OFFSET。"""
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        """SQL LIMIT。"""
        return self.page_size

    def slice(self) -> slice:
        """供内存分页使用的切片。"""
        return slice(self.offset, self.offset + self.limit)

    def clamp_total(self, total: int) -> int:
        """修正总页数溢出时的页码，返回真实页码。"""
        if total <= 0:
            return 1
        max_page = (total + self.page_size - 1) // self.page_size
        return min(self.page, max_page)


def build_page(items: Sequence[Any], total: int, params: PageParams) -> PageResult[Any]:
    """按分页参数构造 `PageResult`。"""
    return PageResult.build(items, total, params.page, params.page_size)


async def paginate(session: Any, stmt: Select[Any], params: PageParams) -> PageResult[Any]:
    """执行分页查询。

    Args:
        session: AsyncSession。
        stmt: 待分页的 SELECT 语句。
        params: 分页参数。

    Returns:
        `PageResult`，`items` 为 ORM 对象列表。
    """
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = int((await session.execute(count_stmt)).scalar_one() or 0)
    rows = (await session.execute(stmt.offset(params.offset).limit(params.limit))).scalars().all()
    return build_page(rows, total, params)


def page_params(
    page: int = Query(default=DEFAULT_PAGE, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="每页条数"),
) -> PageParams:
    """FastAPI 依赖：从 query 参数构造 PageParams。"""
    return PageParams(page=page, page_size=page_size)


__all__ = ["MAX_PAGE_SIZE", "PageParams", "build_page", "page_params", "paginate"]
