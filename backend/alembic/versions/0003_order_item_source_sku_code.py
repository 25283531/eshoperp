"""补齐 `order_item.source_sku_code_1688`（★ E2E 主链路用例暴露的缺失列）。

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-08

★ 为什么需要这条迁移
    `OrderItem.source_sku_code_1688` 在**模型里一直不存在**，但 `app/services/order_service.py`
    一直在用它：
        - 写入 3 处：`match_order()`、`manual_match()`、`handle_action('switch_source')`
          → 对不存在的映射列赋值，SQLAlchemy 只是挂一个**实例属性**，
            **不会落库也绝不报错** ⇒ 匹配到的 1688 SKU 编码被静默丢弃；
        - 读取 2 处：`place_purchase()` 构造 `PurchaseRequest`
          → `AttributeError: 'OrderItem' object has no attribute 'source_sku_code_1688'`
            ⇒ 采购下单**必然 500**。
    因为 `place_purchase()` 全仓没有任何 HTTP / 定时任务入口（从未被真正调用过），
    这个 500 一直潜伏着，直到真实 HTTP 层端到端用例去跑整条主链路才被抓出来。

    → 这类缺陷正是「单元测试全绿但业务跑不通」的成因：
      单测证明了函数、没证明**链路**，而链路上的一段代码从未被执行过。

★ 幂等性：先检查列是否已存在，重复执行不报错（SQLite 需重建表，故用 batch_alter_table）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table: str, column: str) -> bool:
    """判断列是否已存在（幂等执行的前提）。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column in {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    """新增 `order_item.source_sku_code_1688`（可空）。"""
    if _has_column("order_item", "source_sku_code_1688"):
        return
    with op.batch_alter_table("order_item") as batch_op:
        batch_op.add_column(
            sa.Column("source_sku_code_1688", sa.String(length=128), nullable=True,
                      comment="匹配到的 1688 SKU 编码（冗余，采购下单与导出用）")
        )


def downgrade() -> None:
    """删除 `order_item.source_sku_code_1688`。"""
    if not _has_column("order_item", "source_sku_code_1688"):
        return
    with op.batch_alter_table("order_item") as batch_op:
        batch_op.drop_column("source_sku_code_1688")
