"""为 `ai_task_result` 补五列：标题候选数组 / 视频脚本 / 标题选中留痕。

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-10

★ 为什么需要这条迁移
    0004 给 `ai_task` 补上了 `task_type`，于是同一张 AI 任务表上可以区分四种产出：
        ai_rework      老口径「图文重构」（图 + 标题 + 属性一体）
        image_redraw   图片重绘
        title_suggest  商品标题建议（**多条候选**供使用者挑）
        video_script   短视频拍摄脚本文案
    但产出表 `ai_task_result` 上**只有单条标题的位置**（`output_title` String(512)），
    于是新能力产出的东西无处可放：
        ① 标题候选有 N 条（≥3），`output_title` 只装得下一条；
           「AI 出候选 → 人工挑一条 → 上架用那条」这条闭环缺了**存放候选**的那一环；
        ② 视频脚本是结构化数据（分镜 / 时长 / 机位 / 台词），
           既不是标题也不是属性，塞进 `output_attributes_json` 会污染属性语义
           （该列是给平台上架模板填的，混入脚本会让模板渲染拿到一堆无关键）。
    因此本迁移**只新增列**，不动既有列的语义。

★ 五列各自解决什么问题
    - `output_title_candidates_json`  标题候选数组（`AiTitleCandidate.to_dict()` 的列表）。
          ★ 为什么必须存下来：前端要**展示**全部候选给人挑，而挑选动作发生在产出之后
            很久（可能隔天），届时再去问 AI 要候选既慢又可能得到不同的结果。
            `output_title` 存的是「当前生效那条」（首选 / 人工选定后的那条），
            候选数组存的是「当初给过哪几条」—— 两者不是一回事，不能互相替代。
    - `output_video_script_json`      视频脚本结构（`AiVideoScriptResult.to_dict()`）。
          含 `title` / `style` / `scenes[]` / `total_duration_sec` / `scene_count`。
          ★ 只存**文案结构**，不存任何视频文件路径：本能力明确"只出文案，不生成视频"。
    - `selected_title_index` / `selected_title_at` / `selected_title_by`
          ★ 选中留痕三件套：回答「这条标题是谁在什么时候从候选里挑的第几条」。
            为什么不能只改 `output_title`：改完之后 `output_title` 与候选数组里任何一条
            都对不上号也说不清来源 —— 使用者事后问"这个链接当时为什么用这个标题"
            查不到依据（同样的坑在 `_persist_listing()` 上已经踩过一次：
            AI 改写后的标题进不了 listing_product，复盘查不到）。

★ 存量数据口径（升级后老行不能丢、语义不能错）
    - 三个 JSON 列一律 NULL（= 该产出没有候选 / 没有脚本），**不做 `"[]"` / `"{}"` 兜底**。
      理由同 0004 的 `input_prompt_json`：JSON 列的 server_default 在 SQLite / PostgreSQL
      两种方言下写法不一致（`'[]'::json`），统一留 NULL 由消费方 `or []` 兜底更稳。
      ★ 语义上 NULL 与 `[]` 也不同：NULL = "那次产出根本没走多候选口径"，
        `[]` = "走了但一条都没给出来"，混为一谈会让"AI 没给候选"这个故障被静默吞掉。
    - 选中留痕三列一律 NULL：老行从未发生过"从候选里挑"这个动作，
      **不伪造**成 index=0（那样等于凭空声称"使用者选了第一条"）。
    - 本迁移只 `add_column`，**不删任何列** ⇒ 存量行一行不丢。

★ 幂等性：与 0003 / 0004 同款策略 —— 先判断列是否已存在，重复执行不报错
  （SQLite 删列需重建表，故 downgrade 统一走 batch_alter_table）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ★ 新增列清单：(列名, 类型构造器说明)，downgrade 逆序删除
NEW_COLUMNS: tuple[str, ...] = (
    "output_title_candidates_json",
    "output_video_script_json",
    "selected_title_index",
    "selected_title_at",
    "selected_title_by",
)


def _has_column(table: str, column: str) -> bool:
    """判断列是否已存在（幂等执行的前提）。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column in {col["name"] for col in inspector.get_columns(table)}


def _add_column_if_missing(name: str, column: sa.Column) -> None:
    """幂等地加一列：已存在则跳过（重复 upgrade 不报错）。"""
    if _has_column("ai_task_result", name):
        return
    op.add_column("ai_task_result", column)


def upgrade() -> None:
    """新增五列（存量行一律取 NULL，语义见模块 docstring）。"""
    _add_column_if_missing(
        "output_title_candidates_json",
        sa.Column(
            "output_title_candidates_json",
            sa.JSON(),
            nullable=True,
            comment=(
                "标题候选数组（AiTitleCandidate.to_dict() 列表，≥3 条）；"
                "output_title 只存当前生效那条，两者不是一回事"
            ),
        ),
    )
    _add_column_if_missing(
        "output_video_script_json",
        sa.Column(
            "output_video_script_json",
            sa.JSON(),
            nullable=True,
            comment=(
                "短视频拍摄脚本（AiVideoScriptResult.to_dict()：title/style/scenes[]/"
                "total_duration_sec）；只存文案，不含任何视频文件"
            ),
        ),
    )
    _add_column_if_missing(
        "selected_title_index",
        sa.Column(
            "selected_title_index",
            sa.Integer(),
            nullable=True,
            comment="人工选中的标题候选在 output_title_candidates_json 中的下标（从 0 起）",
        ),
    )
    _add_column_if_missing(
        "selected_title_at",
        sa.Column(
            "selected_title_at",
            sa.DateTime(),
            nullable=True,
            comment="人工选中标题候选的时间（可追溯：这条标题什么时候定下来的）",
        ),
    )
    _add_column_if_missing(
        "selected_title_by",
        sa.Column(
            "selected_title_by",
            sa.String(length=64),
            nullable=True,
            comment="人工选中标题候选的操作人",
        ),
    )


def downgrade() -> None:
    """回滚 0005：删掉本迁移新增的五列（**不碰任何既有列** ⇒ 存量行不丢）。"""
    for column in reversed(NEW_COLUMNS):
        if not _has_column("ai_task_result", column):
            continue
        with op.batch_alter_table("ai_task_result") as batch_op:
            batch_op.drop_column(column)
