"""初始 24 张表 + 索引 + 唯一约束 + 系统配置种子。

Revision ID: 0001
Revises: None
Create Date: 2026-10-08

★ 核心约束（不可省略）：
    - uq_sku_mapping_shop_sku：部分唯一索引 (platform, shop_id, shop_sku_code) WHERE is_deleted=0
    - uq_task_record_active_key：部分唯一索引 (task_key) WHERE status IN ('pending','running')
    - uq_erp_order_platform_no：订单幂等去重
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建全部表、索引与种子数据。"""
    _create_source_tables()
    _create_asset_tables()
    _create_listing_tables()
    _create_mapping_tables()
    _create_publish_table()
    _create_order_tables()
    _create_inventory_tables()
    _create_system_tables()
    _create_task_table()
    _seed_system_settings()


def downgrade() -> None:
    """按依赖倒序删表。"""
    for table in [
        "task_record",
        "audit_log",
        "credential",
        "system_setting",
        "price_snapshot",
        "inventory_snapshot",
        "fulfillment_adapter",
        "after_sale",
        "purchase_order",
        "order_item",
        "erp_order",
        "publish_task",
        "mapping_change_log",
        "mapping_conflict",
        "sku_mapping",
        "listing_sku",
        "listing_product",
        "platform_account",
        "ai_task_result",
        "ai_task",
        "asset",
        "source_sku",
        "source_product",
        "supplier",
    ]:
        op.drop_table(table)


# ---------------------------------------------------------------------------
#  货源模块
# ---------------------------------------------------------------------------


def _create_source_tables() -> None:
    """supplier / source_product / source_sku。"""
    op.create_table(
        "supplier",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_by", sa.String(64), nullable=True),
        sa.Column("delete_reason", sa.String(255), nullable=True),
        sa.Column("supplier_1688_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("location", sa.String(128), nullable=True),
        sa.Column("lead_time_hours", sa.Integer(), nullable=True),
        sa.Column("moq", sa.Integer(), nullable=True),
        sa.Column("cooperation_score", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("contact_enc", sa.Text(), nullable=True),
        sa.UniqueConstraint("supplier_1688_id", name="uq_supplier_1688_id"),
    )
    op.create_index("ix_supplier_status", "supplier", ["status", "is_deleted"])
    op.create_index("ix_supplier_name", "supplier", ["name"])

    op.create_table(
        "source_product",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_by", sa.String(64), nullable=True),
        sa.Column("delete_reason", sa.String(255), nullable=True),
        sa.Column("product_1688_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("category_path", sa.String(255), nullable=True),
        sa.Column("supplier_id", sa.Integer(), nullable=True),
        sa.Column("cost_price_cents", sa.Integer(), nullable=True),
        sa.Column("origin_url", sa.String(512), nullable=True),
        sa.Column("main_image_url", sa.String(512), nullable=True),
        sa.Column("params_json", sa.JSON(), nullable=True),
        sa.Column("collected_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="on_sale"),
        sa.Column("stock_status", sa.String(16), nullable=True),
        sa.Column("raw_payload_json", sa.JSON(), nullable=True),
        sa.UniqueConstraint("product_1688_id", name="uq_source_product_1688_id"),
    )
    op.create_index("ix_source_product_supplier", "source_product", ["supplier_id", "is_deleted"])
    op.create_index("ix_source_product_status", "source_product", ["status", "is_deleted"])

    op.create_table(
        "source_sku",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_by", sa.String(64), nullable=True),
        sa.Column("delete_reason", sa.String(255), nullable=True),
        sa.Column("source_product_id", sa.Integer(), nullable=False),
        sa.Column("sku_code_1688", sa.String(128), nullable=False),
        sa.Column("spec_json", sa.JSON(), nullable=False),
        sa.Column("spec_signature", sa.String(128), nullable=False, server_default=""),
        sa.Column("cost_price_cents", sa.Integer(), nullable=True),
        sa.Column("stock_qty", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="on_sale"),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("source_product_id", "sku_code_1688", name="uq_source_sku_product_code"),
    )
    op.create_index("ix_source_sku_signature", "source_sku", ["spec_signature"])
    op.create_index("ix_source_sku_status", "source_sku", ["status", "is_deleted"])


# ---------------------------------------------------------------------------
#  素材与 AI
# ---------------------------------------------------------------------------


def _create_asset_tables() -> None:
    """asset / ai_task / ai_task_result。"""
    op.create_table(
        "asset",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_by", sa.String(64), nullable=True),
        sa.Column("delete_reason", sa.String(255), nullable=True),
        sa.Column("source_product_id", sa.Integer(), nullable=True),
        sa.Column("source_sku_id", sa.Integer(), nullable=True),
        sa.Column("asset_type", sa.String(16), nullable=False, server_default="main_image"),
        sa.Column("origin", sa.String(16), nullable=False, server_default="raw"),
        sa.Column("storage_path", sa.String(512), nullable=False),
        sa.Column("origin_url", sa.String(512), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("lineage_id", sa.String(64), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("tags_json", sa.JSON(), nullable=True),
        sa.Column("ai_task_id", sa.Integer(), nullable=True),
    )
    op.create_index("uq_asset_content_hash", "asset", ["content_hash"], unique=True)
    op.create_index("ix_asset_lineage", "asset", ["lineage_id", "version"])
    op.create_index("ix_asset_product_origin", "asset", ["source_product_id", "origin", "is_deleted"])
    op.create_index("ix_asset_current", "asset", ["is_current", "is_deleted"])

    op.create_table(
        "ai_task",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("source_product_id", sa.Integer(), nullable=False),
        sa.Column("target_platform", sa.String(32), nullable=False),
        sa.Column("rework_items_json", sa.JSON(), nullable=False),
        sa.Column("template_version", sa.String(32), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("5")),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_retry", sa.Integer(), nullable=False, server_default=sa.text("3")),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("task_record_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=True),
    )
    op.create_index("ix_ai_task_status", "ai_task", ["status", "priority"])
    op.create_index("ix_ai_task_product", "ai_task", ["source_product_id"])

    op.create_table(
        "ai_task_result",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("ai_task_id", sa.Integer(), nullable=False),
        sa.Column("output_asset_ids_json", sa.JSON(), nullable=True),
        sa.Column("output_title", sa.String(512), nullable=True),
        sa.Column("output_selling_points", sa.Text(), nullable=True),
        sa.Column("output_attributes_json", sa.JSON(), nullable=True),
        sa.Column("banned_words_json", sa.JSON(), nullable=True),
        sa.Column("review_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("reviewed_by", sa.String(64), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("model_name", sa.String(64), nullable=True),
        sa.Column("prompt_snapshot", sa.Text(), nullable=True),
    )
    op.create_index("ix_ai_task_result_task", "ai_task_result", ["ai_task_id"])
    op.create_index("ix_ai_task_result_review", "ai_task_result", ["review_status"])


# ---------------------------------------------------------------------------
#  平台与上架
# ---------------------------------------------------------------------------


def _create_listing_tables() -> None:
    """platform_account / listing_product / listing_sku。"""
    op.create_table(
        "platform_account",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("shop_name", sa.String(255), nullable=True),
        sa.Column("credential_id", sa.Integer(), nullable=True),
        sa.Column("granted_scopes_json", sa.JSON(), nullable=False),
        sa.Column("token_expires_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.UniqueConstraint("platform", "shop_id", name="uq_platform_account_platform_shop"),
    )
    op.create_index("ix_platform_account_status", "platform_account", ["status"])

    op.create_table(
        "listing_product",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_by", sa.String(64), nullable=True),
        sa.Column("delete_reason", sa.String(255), nullable=True),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("shop_item_id", sa.String(64), nullable=False),
        sa.Column("source_product_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(512), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="on_sale"),
        sa.Column("is_mock", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("listing_mode", sa.String(16), nullable=False, server_default="mock"),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("offline_at", sa.DateTime(), nullable=True),
        sa.Column("offline_reason", sa.String(255), nullable=True),
        sa.UniqueConstraint("platform", "shop_id", "shop_item_id", name="uq_listing_product_platform_shop_item"),
    )
    op.create_index("ix_listing_product_status", "listing_product", ["status", "is_deleted"])
    op.create_index("ix_listing_product_mock", "listing_product", ["is_mock"])

    op.create_table(
        "listing_sku",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_by", sa.String(64), nullable=True),
        sa.Column("delete_reason", sa.String(255), nullable=True),
        sa.Column("listing_product_id", sa.Integer(), nullable=False),
        sa.Column("shop_sku_code", sa.String(128), nullable=False),
        sa.Column("spec_json", sa.JSON(), nullable=True),
        sa.Column("sale_price_cents", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="on_sale"),
        sa.UniqueConstraint("listing_product_id", "shop_sku_code", name="uq_listing_sku_product_code"),
    )
    op.create_index("ix_listing_sku_code", "listing_sku", ["shop_sku_code"])


# ---------------------------------------------------------------------------
#  ★ 映射模块（核心资产）
# ---------------------------------------------------------------------------


def _create_mapping_tables() -> None:
    """sku_mapping / mapping_conflict / mapping_change_log。"""
    op.create_table(
        "sku_mapping",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_by", sa.String(64), nullable=True),
        sa.Column("delete_reason", sa.String(255), nullable=True),
        # 平台侧
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("shop_item_id", sa.String(64), nullable=False),
        sa.Column("shop_sku_code", sa.String(128), nullable=False),
        sa.Column("shop_sku_name", sa.String(255), nullable=True),
        sa.Column("listing_product_id", sa.Integer(), nullable=True),
        sa.Column("listing_sku_id", sa.Integer(), nullable=True),
        # 货源侧
        sa.Column("source_product_id", sa.Integer(), nullable=True),
        sa.Column("source_sku_id", sa.Integer(), nullable=True),
        sa.Column("source_product_1688_id", sa.String(64), nullable=True),
        sa.Column("source_sku_code_1688", sa.String(128), nullable=True),
        sa.Column("source_sku_name", sa.String(255), nullable=True),
        sa.Column("spec_signature", sa.String(128), nullable=True),
        # 成本（★ 单位：分）
        sa.Column("purchase_cost_cents", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("cost_currency", sa.String(8), nullable=False, server_default="CNY"),
        sa.Column("last_cost_check_at", sa.DateTime(), nullable=True),
        # 状态
        sa.Column("status", sa.String(16), nullable=False, server_default="pending_confirm"),
        sa.Column("has_conflict", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("conflict_types", sa.String(128), nullable=True),
        sa.Column("conflict_level", sa.String(8), nullable=True),
        sa.Column("is_mock", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        # 来源与生效
        sa.Column("source", sa.String(16), nullable=False, server_default="system"),
        sa.Column("effective_at", sa.DateTime(), nullable=True),
        sa.Column("expire_at", sa.DateTime(), nullable=True),
        sa.Column("last_pushed_at", sa.DateTime(), nullable=True),
        sa.Column("last_push_status", sa.String(16), nullable=True),
        sa.Column("last_push_adapter", sa.String(32), nullable=True),
        # 审计
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("updated_by", sa.String(64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("remark", sa.String(512), nullable=True),
        # 约束
        sa.CheckConstraint("platform IN ('taobao','douyin','pdd')", name="ck_sku_mapping_platform"),
        sa.CheckConstraint(
            "status IN ('valid','pending_confirm','invalid','archived')", name="ck_sku_mapping_status"
        ),
        sa.CheckConstraint(
            "source IN ('manual','system','third_party','auto_publish')", name="ck_sku_mapping_source"
        ),
        sa.CheckConstraint("conflict_level IS NULL OR conflict_level IN ('P0','P1')", name="ck_sku_mapping_level"),
    )

    # ★★ 核心：部分唯一索引（软删除记录不占用唯一键）
    op.create_index(
        "uq_sku_mapping_shop_sku",
        "sku_mapping",
        ["platform", "shop_id", "shop_sku_code"],
        unique=True,
        sqlite_where=sa.text("is_deleted = 0"),
        postgresql_where=sa.text("is_deleted = FALSE"),
    )
    op.create_index(
        "idx_sku_mapping_match",
        "sku_mapping",
        ["platform", "shop_id", "shop_sku_code", "status", "is_deleted"],
    )
    op.create_index(
        "idx_sku_mapping_shop_item", "sku_mapping", ["platform", "shop_id", "shop_item_id", "is_deleted"]
    )
    op.create_index("idx_sku_mapping_source_sku", "sku_mapping", ["source_sku_id", "is_deleted", "status"])
    op.create_index("idx_sku_mapping_1688_item", "sku_mapping", ["source_product_1688_id", "is_deleted"])
    op.create_index("idx_sku_mapping_conflict", "sku_mapping", ["has_conflict", "conflict_level", "is_deleted"])
    op.create_index("idx_sku_mapping_status", "sku_mapping", ["status", "is_deleted", "updated_at"])
    op.create_index(
        "idx_sku_mapping_push",
        "sku_mapping",
        ["updated_at", "is_deleted"],
        sqlite_where=sa.text("status = 'valid' AND is_mock = 0"),
        postgresql_where=sa.text("status = 'valid' AND is_mock = FALSE"),
    )
    op.create_index("idx_sku_mapping_deleted", "sku_mapping", ["is_deleted", "deleted_at"])

    op.create_table(
        "mapping_conflict",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("sku_mapping_id", sa.Integer(), nullable=False),
        sa.Column("conflict_type", sa.String(32), nullable=False),
        sa.Column("level", sa.String(8), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("detail_json", sa.JSON(), nullable=True),
        sa.Column("is_resolved", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("resolved_by", sa.String(64), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolve_action", sa.String(32), nullable=True),
    )
    op.create_index("ix_mapping_conflict_mapping", "mapping_conflict", ["sku_mapping_id", "is_resolved"])
    op.create_index("ix_mapping_conflict_level", "mapping_conflict", ["level", "is_resolved"])
    op.create_index("ix_mapping_conflict_type", "mapping_conflict", ["conflict_type", "is_resolved"])

    op.create_table(
        "mapping_change_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("sku_mapping_id", sa.Integer(), nullable=False),
        sa.Column("change_action", sa.String(16), nullable=False),
        sa.Column("field_name", sa.String(64), nullable=True),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("change_source", sa.String(16), nullable=False, server_default="system"),
        sa.Column("operator", sa.String(64), nullable=True),
        sa.Column("reason", sa.String(255), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
    )
    op.create_index("ix_mapping_change_log_mapping", "mapping_change_log", ["sku_mapping_id", "created_at"])
    op.create_index("ix_mapping_change_log_action", "mapping_change_log", ["change_action", "created_at"])


# ---------------------------------------------------------------------------
#  上架任务
# ---------------------------------------------------------------------------


def _create_publish_table() -> None:
    """publish_task。"""
    op.create_table(
        "publish_task",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("source_product_id", sa.Integer(), nullable=False),
        sa.Column("ai_task_result_id", sa.Integer(), nullable=True),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("listing_mode", sa.String(16), nullable=False, server_default="mock"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending_precheck"),
        sa.Column("precheck_result_json", sa.JSON(), nullable=True),
        sa.Column("validate_result_json", sa.JSON(), nullable=True),
        sa.Column("platform_error_code", sa.String(64), nullable=True),
        sa.Column("platform_error_msg", sa.Text(), nullable=True),
        sa.Column("error_advice", sa.Text(), nullable=True),
        sa.Column("shop_item_id", sa.String(64), nullable=True),
        sa.Column("shop_sku_codes_json", sa.JSON(), nullable=True),
        sa.Column("is_mock", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("package_path", sa.String(512), nullable=True),
        sa.Column("task_record_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_publish_task_status", "publish_task", ["status", "created_at"])
    op.create_index("ix_publish_task_product", "publish_task", ["source_product_id"])
    op.create_index("ix_publish_task_shop_item", "publish_task", ["platform", "shop_id", "shop_item_id"])


# ---------------------------------------------------------------------------
#  订单与履约
# ---------------------------------------------------------------------------


def _create_order_tables() -> None:
    """erp_order / order_item / purchase_order / after_sale / fulfillment_adapter。"""
    op.create_table(
        "erp_order",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("platform_order_no", sa.String(64), nullable=False),
        sa.Column("buyer_info_enc", sa.Text(), nullable=True),
        sa.Column("receiver_addr_enc", sa.Text(), nullable=True),
        sa.Column("receiver_name_enc", sa.Text(), nullable=True),
        sa.Column("receiver_phone_enc", sa.Text(), nullable=True),
        sa.Column("total_amount_cents", sa.Integer(), nullable=True),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
        sa.Column("fulfillment_status", sa.String(32), nullable=False, server_default="pending_match"),
        sa.Column("adapter_name", sa.String(32), nullable=False, server_default="local_csv"),
        sa.Column("match_status", sa.String(16), nullable=True),
        sa.Column("exception_type", sa.String(32), nullable=True),
        sa.Column("exception_note", sa.Text(), nullable=True),
        sa.Column("handling_action", sa.String(16), nullable=True),
        sa.Column("handled_by", sa.String(64), nullable=True),
        sa.Column("handled_at", sa.DateTime(), nullable=True),
        sa.Column("is_mock", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.UniqueConstraint("platform", "shop_id", "platform_order_no", name="uq_erp_order_platform_no"),
    )
    op.create_index("ix_erp_order_status", "erp_order", ["fulfillment_status", "created_at"])
    op.create_index("ix_erp_order_adapter", "erp_order", ["adapter_name", "fulfillment_status"])
    op.create_index("ix_erp_order_match", "erp_order", ["match_status", "is_mock"])

    op.create_table(
        "order_item",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("platform_order_item_no", sa.String(64), nullable=True),
        sa.Column("shop_item_id", sa.String(64), nullable=False),
        sa.Column("shop_sku_code", sa.String(128), nullable=False),
        sa.Column("sku_mapping_id", sa.Integer(), nullable=True),
        sa.Column("source_sku_id", sa.Integer(), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("purchase_cost_cents", sa.Integer(), nullable=True),
        sa.Column("sale_price_cents", sa.Integer(), nullable=True),
        sa.Column("match_status", sa.String(16), nullable=False, server_default="unmatched"),
        sa.Column("purchase_order_id", sa.Integer(), nullable=True),
    )
    op.create_index("ix_order_item_order", "order_item", ["order_id"])
    op.create_index("ix_order_item_sku", "order_item", ["shop_item_id", "shop_sku_code"])
    op.create_index("ix_order_item_mapping", "order_item", ["sku_mapping_id"])

    op.create_table(
        "purchase_order",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("purchase_order_no", sa.String(64), nullable=True),
        sa.Column("supplier_id", sa.Integer(), nullable=True),
        sa.Column("amount_cents", sa.Integer(), nullable=True),
        sa.Column("adapter_name", sa.String(32), nullable=True),
        sa.Column("purchase_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("logistics_company", sa.String(64), nullable=True),
        sa.Column("tracking_no", sa.String(64), nullable=True),
        sa.Column("shipped_at", sa.DateTime(), nullable=True),
        sa.Column("writeback_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("writeback_retry", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("raw_payload_json", sa.JSON(), nullable=True),
    )
    op.create_index("ix_purchase_order_order", "purchase_order", ["order_id"])
    op.create_index("ix_purchase_order_no", "purchase_order", ["purchase_order_no"])
    op.create_index("ix_purchase_order_status", "purchase_order", ["purchase_status", "writeback_status"])

    op.create_table(
        "after_sale",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("platform_refund_no", sa.String(64), nullable=True),
        sa.Column("refund_reason", sa.Text(), nullable=True),
        sa.Column("refund_amount_cents", sa.Integer(), nullable=True),
        sa.Column("refund_1688_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("return_address_json", sa.JSON(), nullable=True),
        sa.Column("return_address_push_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("responsibility", sa.String(16), nullable=True),
        sa.Column("handling_status", sa.String(16), nullable=False, server_default="processing"),
        sa.Column("evidence_json", sa.JSON(), nullable=True),
    )
    op.create_index("ix_after_sale_order", "after_sale", ["order_id"])
    op.create_index("ix_after_sale_status", "after_sale", ["handling_status", "refund_1688_status"])

    op.create_table(
        "fulfillment_adapter",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("adapter_name", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(64), nullable=False),
        sa.Column("credential_id", sa.Integer(), nullable=True),
        sa.Column("capability_json", sa.JSON(), nullable=False),
        sa.Column("declared_scopes_json", sa.JSON(), nullable=True),
        sa.Column("scope_check_status", sa.String(16), nullable=False, server_default="passed"),
        sa.Column("scope_check_message", sa.Text(), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("10")),
        sa.Column("health_status", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("last_heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("last_heartbeat_msg", sa.Text(), nullable=True),
        sa.Column("heartbeat_fail_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("config_json", sa.JSON(), nullable=True),
        sa.UniqueConstraint("adapter_name", name="uq_fulfillment_adapter_name"),
    )
    op.create_index("ix_fulfillment_adapter_active", "fulfillment_adapter", ["is_active", "is_enabled"])
    op.create_index("ix_fulfillment_adapter_health", "fulfillment_adapter", ["health_status"])


# ---------------------------------------------------------------------------
#  库存
# ---------------------------------------------------------------------------


def _create_inventory_tables() -> None:
    """inventory_snapshot / price_snapshot。"""
    op.create_table(
        "inventory_snapshot",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("source_sku_id", sa.Integer(), nullable=False),
        sa.Column("stock_qty", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("source", sa.String(16), nullable=False, server_default="erp_poll"),
        sa.Column("collected_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_inventory_snapshot_sku_time", "inventory_snapshot", ["source_sku_id", "collected_at"])

    op.create_table(
        "price_snapshot",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("source_sku_id", sa.Integer(), nullable=False),
        sa.Column("cost_price_cents", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("prev_price_cents", sa.Integer(), nullable=True),
        sa.Column("change_rate", sa.Numeric(6, 4), nullable=True),
        sa.Column("collected_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_price_snapshot_sku_time", "price_snapshot", ["source_sku_id", "collected_at"])


# ---------------------------------------------------------------------------
#  系统
# ---------------------------------------------------------------------------


def _create_system_tables() -> None:
    """system_setting / audit_log / credential。"""
    op.create_table(
        "system_setting",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("setting_key", sa.String(64), nullable=False),
        sa.Column("setting_value", sa.Text(), nullable=True),
        sa.Column("value_type", sa.String(16), nullable=False, server_default="string"),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("updated_by", sa.String(64), nullable=True),
        sa.UniqueConstraint("setting_key", name="uq_system_setting_key"),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("operator", sa.String(64), nullable=False, server_default="system"),
        sa.Column("operator_role", sa.String(16), nullable=True),
        sa.Column("action_type", sa.String(32), nullable=False),
        sa.Column("object_type", sa.String(32), nullable=False),
        sa.Column("object_id", sa.String(64), nullable=True),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("remark", sa.String(512), nullable=True),
    )
    op.create_index("ix_audit_log_action_time", "audit_log", ["action_type", "created_at"])
    op.create_index("ix_audit_log_object", "audit_log", ["object_type", "object_id"])
    op.create_index("ix_audit_log_operator", "audit_log", ["operator", "created_at"])

    op.create_table(
        "credential",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("owner_type", sa.String(32), nullable=False),
        sa.Column("owner_key", sa.String(64), nullable=False),
        sa.Column("credential_key", sa.String(64), nullable=False),
        sa.Column("value_enc", sa.Text(), nullable=False),
        sa.Column("value_masked", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("owner_type", "owner_key", "credential_key", name="uq_credential_owner_key"),
    )
    op.create_index("ix_credential_owner", "credential", ["owner_type", "owner_key"])


# ---------------------------------------------------------------------------
#  异步任务
# ---------------------------------------------------------------------------


def _create_task_table() -> None:
    """task_record。"""
    op.create_table(
        "task_record",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("task_type", sa.String(32), nullable=False),
        sa.Column("task_key", sa.String(128), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("5")),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_retry", sa.Integer(), nullable=False, server_default=sa.text("3")),
        sa.Column("scheduled_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("worker_id", sa.String(64), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
    )
    # ★ 幂等：同一键同时只能有一个活跃任务
    op.create_index(
        "uq_task_record_active_key",
        "task_record",
        ["task_key"],
        unique=True,
        sqlite_where=sa.text("status IN ('pending','running')"),
        postgresql_where=sa.text("status IN ('pending','running')"),
    )
    op.create_index("ix_task_record_status", "task_record", ["status", "priority", "created_at"])
    op.create_index("ix_task_record_type", "task_record", ["task_type", "status"])
    op.create_index("ix_task_record_scheduled", "task_record", ["scheduled_at"])


# ---------------------------------------------------------------------------
#  系统配置种子（§4.4.6）
# ---------------------------------------------------------------------------


def _seed_system_settings() -> None:
    """写入默认系统配置（与 models/system.py 的 DEFAULT_SETTINGS 一致）。"""
    from app.models.system import DEFAULT_SETTINGS

    bind = op.get_bind()
    for item in DEFAULT_SETTINGS:
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
