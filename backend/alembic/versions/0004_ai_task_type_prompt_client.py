"""为 `ai_task` 补三列：`task_type` / `input_prompt_json` / `ai_client`（+ `ix_ai_task_type`）。

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-10

★ 为什么需要这条迁移
    店铺模式调整为「1688 找货源 → 站内 AI 重绘图文 → 手工上架」后，要新增三种 AI 能力：
        image_redraw    图片重绘（主图 / 详情图，**逐图提示词**）
        title_suggest   商品标题建议（**3 条以上候选**供挑选）
        video_script    短视频拍摄脚本建议（可带拍摄风格提示词）
    而 `ai_task` 表上：
        ① **没有任务类型**。`rework_items_json` 是"本次重构要做哪几项"的**子项列表**
           （`AiReworkItem`：main_image / detail_image / title / attribute），
           **不是任务类型** ⇒ 三种新能力在数据结构上无法互相区分，也无法与老"图文重构"区分；
        ② **没有输入提示词**。使用者输入的提示词（尤其逐图提示词）**无处可存** ——
           全项目唯一的 `prompt` 是产出侧回填的 `ai_task_result.prompt_snapshot`；
        ③ **没有 `ai_client`**。README 第九节第 17 条明确记载：设计口径要求"切换 AI 客户端后
           在途任务仍按原客户端跑完"，但表上不记录创建时使用的客户端，
           **该要求在数据结构上无法实现**。既然本次要动这张表，一并补上。

★ 存量数据口径（升级后老行不能丢、语义不能错）
    - `task_type`  → `'ai_rework'`：0004 之前创建的行**全部**是老"图文重构"任务，
                     这是唯一与历史语义相符的取值（`AiTaskType.AI_REWORK`）。
    - `ai_client`  → `'file_bridge'`：`Settings.ai_client` 的文档默认值
                     （README 第三节环境变量表：`AI_CLIENT` 默认 `file_bridge`，
                      `AiClientFactory` 未注册表名的兜底也是它）。
                     ★ 如实说明：历史行**没有**记录当年生效的客户端，这个值是**按文档默认推断**的，
                       不是实测快照；若使用者当年手工切过 `mock` / `http`，
                       老行仍可能与实际执行通道不符 —— 这是无法从现有数据恢复的信息。
                       真正消除该推断要靠**创建时即固化**（待下一棒接入）。
    - `input_prompt_json` → NULL（= 未填写任何提示词），不做 `"{}"` 兜底，
      因为 JSON 列的 server_default 在 SQLite / PostgreSQL 两种方言下写法不一致（`'{}'::json`），
      统一留 NULL 由消费方 `or {}` 兜底更稳。

★ 幂等性：与 0003 同款策略 —— 先判断列 / 索引是否已存在，重复执行不报错
  （SQLite 需重建表，故统一走 batch_alter_table）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AI_TASK_TYPE_INDEX = "ix_ai_task_type"


def _has_column(table: str, column: str) -> bool:
    """判断列是否已存在（幂等执行的前提）。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column in {col["name"] for col in inspector.get_columns(table)}


def _has_index(table: str, index: str) -> bool:
    """判断索引是否已存在。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return index in {idx["name"] for idx in inspector.get_indexes(table)}


def upgrade() -> None:
    """新增三列并建 `task_type` 索引（存量行取 documented 默认值）。"""
    if not _has_column("ai_task", "task_type"):
        op.add_column(
            "ai_task",
            sa.Column(
                "task_type",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'ai_rework'"),
                comment=(
                    "AI 任务类型：ai_rework 图文重构（存量默认值）/ image_redraw 图片重绘 / "
                    "title_suggest 标题建议 / video_script 视频脚本"
                ),
            ),
        )
    if not _has_column("ai_task", "input_prompt_json"):
        op.add_column(
            "ai_task",
            sa.Column(
                "input_prompt_json",
                sa.JSON(),
                nullable=True,
                comment='使用者输入的提示词：{"global":..,"images":[{"index":0,"prompt":..}],..}（逐图 + 全局）',
            ),
        )
    if not _has_column("ai_task", "ai_client"):
        op.add_column(
            "ai_task",
            sa.Column(
                "ai_client",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'file_bridge'"),
                comment="创建时固化的 AI 客户端名（切换配置后在途任务仍按原客户端跑完）",
            ),
        )
    if not _has_index("ai_task", AI_TASK_TYPE_INDEX):
        op.create_index(AI_TASK_TYPE_INDEX, "ai_task", ["task_type"])


def downgrade() -> None:
    """回滚 0004：先删索引再删列（顺序不能反，SQLite 重建表时会连带处理索引）。"""
    if _has_index("ai_task", AI_TASK_TYPE_INDEX):
        op.drop_index(AI_TASK_TYPE_INDEX, table_name="ai_task")
    for column in ("task_type", "input_prompt_json", "ai_client"):
        if not _has_column("ai_task", column):
            continue
        with op.batch_alter_table("ai_task") as batch_op:
            batch_op.drop_column(column)
