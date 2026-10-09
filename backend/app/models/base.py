"""DeclarativeBase 与通用 Mixin（§4.2）。

所有表均含：`id` / `created_at` / `updated_at`。
软删除表（sku_mapping / source_product / source_sku / asset / listing_product /
listing_sku / supplier）额外含 `is_deleted` / `deleted_at` / `deleted_by` / `delete_reason`。

★ 所有查询默认带 `is_deleted = 0`，由 `SoftDeleteMixin.not_deleted()` 提供，禁止裸写查询。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.utils.kit import utc_now

# JSON 列统一类型：SQLite / PostgreSQL 均可用（PG 上为 json，SQLite 上以文本存储）
JSONType = JSON()


class Base(DeclarativeBase):
    """全局 DeclarativeBase。"""

    # 统一命名约定，保证索引/约束名在 SQLite 与 PG 下一致
    naming_convention = {
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s",
        "pk": "pk_%(table_name)s",
    }

    def to_dict(self, exclude: set[str] | None = None) -> dict[str, Any]:
        """将 ORM 对象转为字典（用于审计日志 old/new value 与导出）。"""
        skip = exclude or set()
        return {
            column.name: getattr(self, column.name)
            for column in self.__table__.columns  # type: ignore[attr-defined]
            if column.name not in skip
        }


class TimestampMixin:
    """创建/更新时间（UTC）。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now, server_default=func.now(), comment="创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utc_now,
        server_default=func.now(),
        onupdate=utc_now,
        comment="更新时间（UTC）",
    )


class BaseMixin(TimestampMixin):
    """主键 + 时间戳。"""

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="自增主键")


class SoftDeleteMixin:
    """软删除字段（保留 ≥180 天可回滚）。"""

    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0", comment="是否已软删除"
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="删除时间（UTC）")
    deleted_by: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="删除人")
    delete_reason: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="删除原因")

    @classmethod
    def not_deleted(cls) -> Any:
        """返回 `is_deleted = 0` 过滤条件，所有查询必须带上。"""
        return cls.is_deleted.is_(False)

    def mark_deleted(self, operator: str = "system", reason: str = "") -> None:
        """执行软删除（不提交事务，由调用方 commit）。"""
        self.is_deleted = True
        self.deleted_at = utc_now()
        self.deleted_by = operator
        self.delete_reason = (reason or "")[:255]

    def restore(self) -> None:
        """回滚软删除（不提交事务，由调用方 commit）。"""
        self.is_deleted = False
        self.deleted_at = None
        self.deleted_by = None
        self.delete_reason = None


__all__ = ["Base", "BaseMixin", "JSONType", "SoftDeleteMixin", "TimestampMixin"]
