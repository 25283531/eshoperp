"""上架模块：PublishTask（§4.4.3，状态机见 §7.1）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, String, Text, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BaseMixin, JSONType
from app.models.enums import ListingMode, PublishStatus, PUBLISH_TRANSITIONS
from app.utils.kit import utc_now


class PublishTask(BaseMixin, Base):
    """上架任务状态机载体。

    ★ 守卫规则：`pending_validate → pending_publish` 必须由 `MappingValidator.validate()`
      返回 `blocking=false`；任何绕过校验直接调用 `ListingAdapter.publish()` 均视为缺陷。
    """

    __tablename__ = "publish_task"

    source_product_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="FK → source_product.id")
    ai_task_result_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="引用的重构结果（必须 review_status=approved）"
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False, comment="目标平台")
    shop_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="目标店铺")
    listing_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ListingMode.MOCK.value, comment="real/mock/manual"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PublishStatus.PENDING_PRECHECK.value, comment="见 §7.1 状态机"
    )
    precheck_result_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONType, nullable=True, comment="合规预检结果（失败项与建议）"
    )
    validate_result_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONType, nullable=True, comment="映射校验结果（缺失 / 冲突清单）"
    )
    platform_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="平台返回错误码")
    platform_error_msg: Mapped[str | None] = mapped_column(Text, nullable=True, comment="平台返回错误信息")
    error_advice: Mapped[str | None] = mapped_column(Text, nullable=True, comment="错误码翻译后的中文建议")
    shop_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="回写的平台商品 ID")
    shop_sku_codes_json: Mapped[list[Any] | None] = mapped_column(
        JSONType, nullable=True, comment="回写的平台 SKU 编码列表"
    )
    is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="Mock 标记")
    package_path: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="半自动素材包路径（manual 模式）"
    )
    task_record_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="FK → task_record.id")
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="创建人")
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="上架成功时间")

    __table_args__ = (
        Index("ix_publish_task_status", "status", "created_at"),
        Index("ix_publish_task_product", "source_product_id"),
        Index("ix_publish_task_shop_item", "platform", "shop_id", "shop_item_id"),
    )

    def can_transition_to(self, target: str) -> bool:
        """校验状态机迁移是否合法（§7.1）。"""
        return target in PUBLISH_TRANSITIONS.get(self.status, [])

    def transition_to(self, target: str) -> str:
        """执行状态迁移，非法迁移抛 ValueError（由上层转 1005）。

        Returns:
            迁移后的状态值。
        """
        if not self.can_transition_to(target):
            raise ValueError(f"非法的上架状态迁移：{self.status} → {target}")
        self.status = target
        if target == PublishStatus.PUBLISH_SUCCESS.value:
            self.published_at = utc_now()
        return self.status

    def __repr__(self) -> str:  # noqa: D105
        return f"<PublishTask {self.id} {self.status}>"


__all__ = ["PublishTask"]
