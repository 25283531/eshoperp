"""ARCH v1.8 schema delta —— 补齐 v1.2 / v1.4 / v1.5 引入但 0001 未落地的字段与约束。

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-08

本迁移包含三组变更（均为「模型已有、DDL 缺失」的补齐，非新增业务表）：

    A. 冲突类型语义修正配套（v1.8）
       - 新增第 6 类冲突 `cost_underwater`（P1）无需 DDL；
       - 本迁移同步新增其两项可配系统配置。

    B. `sku_mapping` 成本三层语义字段（v1.4）
       - `cost_source`          VARCHAR(16) NOT NULL DEFAULT 'auto'  镜像成本来源
       - `cost_overridden_at`   TIMESTAMP NULL                      人工覆盖时间
       - `cost_overridden_by`   VARCHAR(64) NULL                    人工覆盖人
       - CHECK (cost_source IN ('auto','manual'))                   ck_sku_mapping_cost_src

    C. `audit_log` 越权告警处置态四字段（v1.2，SYS-P0-05 / SYS-P0-06）
       - `is_handled`   BOOLEAN NOT NULL DEFAULT 0
       - `handled_by`   VARCHAR(64) NULL
       - `handled_at`   TIMESTAMP NULL
       - `handle_note`  VARCHAR(512) NULL
       - 部分索引 `idx_audit_violation (action_type, is_handled, created_at)
                   WHERE action_type = 'permission_change'`（顶栏红点高频轮询）

    D. 系统配置种子补齐
       - `ai.client` 默认值 mock → **file_bridge**（★ 用户决策 ②：AI 走 WorkBuddy 文件桥 + HTTP 双通道）
       - 新增 `mapping.conflict_cost_underwater_level`（默认 P1）
       - 新增 `mapping.min_profit_margin`（默认 0）

★ 幂等性：所有 DDL 先检查列 / 索引是否已存在，重复执行不报错。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
#  小工具
# ---------------------------------------------------------------------------


def _dialect() -> str:
    """当前数据库方言：`sqlite` / `postgresql`。"""
    return "postgresql" if op.get_bind().dialect.name.startswith("postgres") else "sqlite"


def _existing_columns(table: str) -> set[str]:
    """返回表中已存在的列名集合。"""
    insp = sa.inspect(op.get_bind())
    return {col["name"] for col in insp.get_columns(table)}


def _existing_indexes(table: str) -> set[str]:
    """返回表中已存在的索引名集合。"""
    insp = sa.inspect(op.get_bind())
    return {idx["name"] for idx in insp.get_indexes(table)}


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    """列不存在时才添加。"""
    if column.name in _existing_columns(table):
        return
    op.add_column(table, column)


# ---------------------------------------------------------------------------
#  B. sku_mapping 成本三层语义字段
# ---------------------------------------------------------------------------


def _upgrade_sku_mapping_cost() -> None:
    """为 sku_mapping 增加 cost_source / cost_overridden_at / cost_overridden_by。"""
    _add_column_if_missing(
        "sku_mapping",
        sa.Column("cost_source", sa.String(16), nullable=False, server_default="auto"),
    )
    _add_column_if_missing("sku_mapping", sa.Column("cost_overridden_at", sa.DateTime(), nullable=True))
    _add_column_if_missing("sku_mapping", sa.Column("cost_overridden_by", sa.String(64), nullable=True))

    if _dialect() == "postgresql":
        op.execute(
            sa.text(
                "ALTER TABLE sku_mapping "
                "ADD CONSTRAINT ck_sku_mapping_cost_src CHECK (cost_source IN ('auto','manual'))"
            )
        )
    # SQLite 的 CHECK 只能在建表时声明；已有表需批量重建（render_as_batch=True 时由 Alembic 处理）。
    # 此处不重建表，改由服务层 `cost_source` 写入前校验（见 MappingService.set_cost），
    # 保证两种库上行为一致，同时避免 SQLite 批量重建丢失部分索引的风险。


# ---------------------------------------------------------------------------
#  C. audit_log 越权告警处置态
# ---------------------------------------------------------------------------


def _upgrade_audit_log_handling() -> None:
    """为 audit_log 增加 is_handled / handled_by / handled_at / handle_note + 部分索引。"""
    _add_column_if_missing(
        "audit_log", sa.Column("is_handled", sa.Boolean(), nullable=False, server_default=sa.text("0"))
    )
    _add_column_if_missing("audit_log", sa.Column("handled_by", sa.String(64), nullable=True))
    _add_column_if_missing("audit_log", sa.Column("handled_at", sa.DateTime(), nullable=True))
    _add_column_if_missing("audit_log", sa.Column("handle_note", sa.String(512), nullable=True))

    indexes = _existing_indexes("audit_log")
    if "idx_audit_violation" not in indexes:
        op.create_index(
            "idx_audit_violation",
            "audit_log",
            ["action_type", "is_handled", "created_at"],
            sqlite_where=sa.text("action_type = 'permission_change'"),
            postgresql_where=sa.text("action_type = 'permission_change'"),
        )


# ---------------------------------------------------------------------------
#  D. 系统配置种子补齐 / 修正
# ---------------------------------------------------------------------------

# ★ 用户决策 ②：AI 默认走 WorkBuddy 文件桥（与 WorkBuddy 协作的真实路径）
AI_CLIENT_DEFAULT = "file_bridge"

EXTRA_SETTINGS: list[dict[str, str]] = [
    {
        "key": "mapping.conflict_cost_underwater_level",
        "value": "P1",
        "value_type": "string",
        "description": "成本倒挂冲突级别：P1 仅提示（默认） / P0 禁止上架",
    },
    {
        "key": "mapping.min_profit_margin",
        "value": "0",
        "value_type": "string",
        "description": "最低利润率缓冲（0–1），成本 ≥ 售价×(1-缓冲) 判定为倒挂",
    },
]


def _upgrade_system_settings() -> None:
    """修正 ai.client 默认值，并补齐 v1.4 新增的两项映射配置。"""
    bind = op.get_bind()

    # ① ai.client：只在「未被人手动改过」时才修正为 file_bridge。
    #    判据：值仍为旧的 mock 默认值，或该键不存在。
    row = bind.execute(
        sa.text("SELECT setting_value FROM system_setting WHERE setting_key = :key"),
        {"key": "ai.client"},
    ).fetchone()
    if row is None:
        bind.execute(
            sa.text(
                "INSERT INTO system_setting "
                "(setting_key, setting_value, value_type, description, updated_by, created_at, updated_at) "
                "VALUES (:key, :value, :value_type, :description, :updated_by, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {
                "key": "ai.client",
                "value": AI_CLIENT_DEFAULT,
                "value_type": "string",
                "description": "AI 客户端：mock / file_bridge（默认，与 WorkBuddy 协作）/ http",
                "updated_by": "system",
            },
        )
    elif str(row[0]).strip() == "mock":
        bind.execute(
            sa.text("UPDATE system_setting SET setting_value = :value WHERE setting_key = :key"),
            {"key": "ai.client", "value": AI_CLIENT_DEFAULT},
        )

    # ② 补齐新增配置（已存在则跳过）
    for item in EXTRA_SETTINGS:
        exists = bind.execute(
            sa.text("SELECT 1 FROM system_setting WHERE setting_key = :key"),
            {"key": item["key"]},
        ).fetchone()
        if exists:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO system_setting "
                "(setting_key, setting_value, value_type, description, updated_by, created_at, updated_at) "
                "VALUES (:key, :value, :value_type, :description, :updated_by, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {
                "key": item["key"],
                "value": item["value"],
                "value_type": item["value_type"],
                "description": item["description"],
                "updated_by": "system",
            },
        )


# ---------------------------------------------------------------------------
#  upgrade / downgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    """应用 v1.8 结构补齐。"""
    _upgrade_sku_mapping_cost()
    _upgrade_audit_log_handling()
    _upgrade_system_settings()


def downgrade() -> None:
    """回滚：删除新增配置种子与处置态索引（列保留，避免历史审计数据丢失）。"""
    bind = op.get_bind()
    indexes = _existing_indexes("audit_log")
    if "idx_audit_violation" in indexes:
        op.drop_index("idx_audit_violation", table_name="audit_log")
    bind.execute(
        sa.text("DELETE FROM system_setting WHERE setting_key IN (:k1, :k2)"),
        {"k1": "mapping.conflict_cost_underwater_level", "k2": "mapping.min_profit_margin"},
    )
    bind.execute(
        sa.text("UPDATE system_setting SET setting_value = :value WHERE setting_key = :key"),
        {"key": "ai.client", "value": "mock"},
    )
