"""★ 映射模块：SkuMapping / MappingConflict / MappingChangeLog（§4.3，核心资产）。

设计要点：
    * 部分唯一索引 `uq_sku_mapping_shop_sku ON (platform, shop_id, shop_sku_code) WHERE is_deleted=0`
      —— 软删除记录不占用唯一键，删除后可重新创建同键映射；
    * 软删除保留 ≥180 天，可回滚（PRD 8.4）；
    * 任何变更逐字段写 `mapping_change_log`；
    * `version` 乐观锁，每次变更 +1；
    * `spec_signature` 规格指纹（规格名值对排序后 md5），用于货源端变更检测；
    * **金额一律存整数「分」**：`purchase_cost_cents`。

本模块同时以常量字符串形式提供 §4.3.1 的冲突检测 SQL，
供上层（T-A05 `MappingValidator`）按方言取用，**避免 SQL 散落在服务层**。

================================================================================
★ ★ ★ QA-01 / QA-02 处置结论（独立 QA 实测，已确认）★ ★ ★
================================================================================
`one_to_many`（一平台 SKU 映射多个货源 SKU）与 `duplicate_item`
（同一 `shop_sku_code` 挂在多个 `shop_item_id` 下）这两条**读时扫描 SQL 已删除**。

原因不是"暂时查不到"，而是**在当前 schema 下这两类冲突根本不可能存在于库中**：
两条 SQL 的 `GROUP BY` 分别是
`(platform, shop_id, shop_item_id, shop_sku_code)` 与 `(platform, shop_id, shop_sku_code)`，
**都完整包含**部分唯一索引

    uq_sku_mapping_shop_sku (platform, shop_id, shop_sku_code) WHERE is_deleted = 0

的键列 ⇒ 每个分组最多 1 行 ⇒ `HAVING COUNT(DISTINCT ...) > 1` **恒为假** ⇒ 永远命中 0 行。
手工造反例时数据库直接拒绝插入第二行（`UNIQUE constraint failed`）。

→ 保护**不是靠读时扫描实现的**，而是由该唯一索引在**写入边界**结构性阻断：
   创建 / 更新映射时若撞键，**直接报明确的业务错误（HTTP 409 / 1006）**，
   绝不让 `IntegrityError` 漏成 500。见 `MappingService.create()` 与
   `app/core/errors.py` 的 `IntegrityError` 全局处理器。

→ 因此冲突面板上这两类**恒为 0 是正确结果，不是"没检测到"**。
   `ConflictType` 枚举保留（`one_to_many` / `duplicate_item` 前端仍在用），
   级别表 `CONFLICT_LEVELS` 也保留，仅为前端展示与历史数据兼容。

→ **保护必须可验证**：`app/services/bootstrap.py::check_required_constraints()`
   在启动时自检该唯一索引确实存在（且确实是 UNIQUE + 部分索引），
   缺失会立刻在 `/health` 暴露。若有人误删索引，必须能被立即发现。
================================================================================
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BaseMixin, JSONType, SoftDeleteMixin
from app.models.enums import (
    ChangeAction,
    ChangeSource,
    ConflictLevel,
    ConflictType,
    MappingSource,
    MappingStatus,
    ResolveAction,
)
from app.utils.kit import utc_now


# ============================================================================
#  §4.3.1 冲突检测 SQL（供上层复用的常量，按方言分列）
#
#  ★★ 命名语义已按 ARCH v1.8 §4.3.1 修正（原先与文档相反，会导致跨平台铺货被误拦截）★★
#     many_to_one  = ③ **跨平台铺货**       → P1，**永不拦截**，且必须**先跑并剔除**
#     duplicate    = ②a 同店铺内反向重复   → P0，拦截
#
#  ★★ QA-01 / QA-02（已删除两条死 SQL，详见本文件头部注释）★★
#     ① `one_to_many`（一平台 SKU → 多货源 SKU）
#     ②b `duplicate_item`（同一 shop_sku_code 挂多个 shop_item_id）
#     这两类的 GROUP BY 都包含唯一索引 `uq_sku_mapping_shop_sku` 的键列
#     ⇒ HAVING 恒假 ⇒ 永远命中 0 行 ⇒ 属于"写了检测但检测永不生效"。
#     按「写了检测不等于检测生效」原则**已删除**，改由写入边界的唯一约束强制阻断
#     （撞键 → HTTP 409 / 1006 明确业务错误，绝不漏 IntegrityError 成 500）。
#     枚举与级别表保留，仅供前端展示；冲突面板上这两类恒为 0 是**正确**结果。
# ============================================================================

# 冲突类型 ②a：同一平台同一店铺内，一个货源 SKU 被多个店铺 SKU 引用 —— P0
# （唯一索引 uq_sku_mapping_shop_sku 只约束「一店铺SKU→一映射」，不约束反向，此情形可真实发生）
CONFLICT_SQL_DUPLICATE_REVERSE_SQLITE = """
SELECT m.platform,
       m.shop_id,
       m.source_sku_id,
       m.source_sku_code_1688,
       COUNT(DISTINCT m.shop_sku_code)               AS shop_sku_cnt,
       GROUP_CONCAT(DISTINCT m.shop_sku_code)        AS shop_sku_codes,
       GROUP_CONCAT(DISTINCT m.id)                   AS mapping_ids
FROM sku_mapping m
WHERE m.is_deleted = 0 AND m.source_sku_id IS NOT NULL
GROUP BY m.platform, m.shop_id, m.source_sku_id, m.source_sku_code_1688
HAVING COUNT(DISTINCT m.shop_sku_code) > 1
"""

CONFLICT_SQL_DUPLICATE_REVERSE_PG = """
SELECT m.platform,
       m.shop_id,
       m.source_sku_id,
       m.source_sku_code_1688,
       COUNT(DISTINCT m.shop_sku_code)                 AS shop_sku_cnt,
       STRING_AGG(DISTINCT m.shop_sku_code, ',')        AS shop_sku_codes,
       ARRAY_AGG(DISTINCT m.id)                         AS mapping_ids
FROM sku_mapping m
WHERE m.is_deleted = FALSE AND m.source_sku_id IS NOT NULL
GROUP BY m.platform, m.shop_id, m.source_sku_id, m.source_sku_code_1688
HAVING COUNT(DISTINCT m.shop_sku_code) > 1
"""

# 冲突类型 ③：跨平台铺货（同一货源铺到淘宝 / 抖店 / 拼多多）—— ★ P1，仅提示，永不拦截
# ★★ 这是**正常业务**。PRD v1.1 验收口径写死：
#    "运营在三个平台铺同一个货源商品时，不得出现『为什么我三平台铺货被拦了』的拦截"
# ★★ 本条必须**最先执行并剔除命中项**，否则跨平台铺货会被 ②a 误判为 P0 拦截。
CONFLICT_SQL_MANY_TO_ONE_SQLITE = """
SELECT m.source_sku_id,
       m.source_sku_code_1688,
       COUNT(DISTINCT m.platform || ':' || m.shop_id || ':' || m.shop_sku_code) AS shop_sku_cnt,
       COUNT(DISTINCT m.platform)                                               AS platform_cnt,
       GROUP_CONCAT(DISTINCT m.platform)                                        AS platforms,
       GROUP_CONCAT(DISTINCT m.id)                                              AS mapping_ids
FROM sku_mapping m
WHERE m.is_deleted = 0 AND m.source_sku_id IS NOT NULL
GROUP BY m.source_sku_id, m.source_sku_code_1688
HAVING COUNT(DISTINCT m.platform || ':' || m.shop_id || ':' || m.shop_sku_code) > 1
"""

CONFLICT_SQL_MANY_TO_ONE_PG = """
SELECT m.source_sku_id,
       m.source_sku_code_1688,
       COUNT(DISTINCT m.platform || ':' || m.shop_id || ':' || m.shop_sku_code) AS shop_sku_cnt,
       COUNT(DISTINCT m.platform)                                               AS platform_cnt,
       STRING_AGG(DISTINCT m.platform, ',')                                     AS platforms,
       ARRAY_AGG(DISTINCT m.id)                                                 AS mapping_ids
FROM sku_mapping m
WHERE m.is_deleted = FALSE AND m.source_sku_id IS NOT NULL
GROUP BY m.source_sku_id, m.source_sku_code_1688
HAVING COUNT(DISTINCT m.platform || ':' || m.shop_id || ':' || m.shop_sku_code) > 1
"""

# 冲突类型 ④：采购成本为空 / 为 0 / 为负 —— P0，禁止上架
CONFLICT_SQL_COST_INVALID = """
SELECT m.id, m.platform, m.shop_id, m.shop_item_id, m.shop_sku_code,
       m.source_sku_code_1688, m.purchase_cost_cents, m.status
FROM sku_mapping m
WHERE m.is_deleted = {false_literal}
  AND (m.purchase_cost_cents IS NULL OR m.purchase_cost_cents <= 0)
"""

# 冲突类型 ⑥：成本倒挂（镜像成本 ≥ 平台售价）—— ★ P1，仅黄标提示，默认不拦截
#
# ★★ 关于 `ls.sale_price_cents > 0` 与 `m.is_mock = 0` 两个前置过滤 —— 过滤不等于解决 ★★
# 半自动模式下若人工回填 ID 时不填售价，sale_price_cents=0 会被判定成"成本 ≥ 售价"
# → 大面积误报倒挂，污染冲突面板；Mock 商品售价是模拟值，倒挂检测对它们无意义。
# 但**过滤防不了真正的危害**：被过滤掉的商品**永久失去倒挂检测能力** ——
# 冲突面板上干干净净，实际是这些商品根本没被检测。这与 `duplicate` 是同一类缺陷：
# **不报错、只静默失效**。
#
# 因此主次不能颠倒：
#   | 主次 | 手段                                                | 作用                     |
#   | ---- | --------------------------------------------------- | ------------------------ |
#   | 主   | LST-P0-07 售价必填（回填表单拒绝空售价 → 422）      | 从源头消灭空值           |
#   | 辅   | 本 SQL 的 sale_price_cents > 0 过滤                 | 仅兜底存量历史残留       |
#   | 辅   | POST /listing-products/{id}/fill-price 存量补填     | 救回历史空值并触发重算   |
#
# ⚠️ 后续维护者注意：不要因为"SQL 已经过滤了"就认为问题处理完了。
#    空值商品的检测盲区仍然存在，必须靠必填 + 补填解决。
CONFLICT_SQL_COST_UNDERWATER = """
SELECT m.id, m.platform, m.shop_id, m.shop_item_id, m.shop_sku_code,
       m.source_sku_code_1688,
       m.purchase_cost_cents              AS cost_cents,
       ls.sale_price_cents                AS price_cents,
       (ls.sale_price_cents - m.purchase_cost_cents) AS margin_cents,
       m.cost_source, m.status
FROM sku_mapping m
JOIN listing_sku ls ON ls.id = m.listing_sku_id
WHERE m.is_deleted = {false_literal}
  AND m.status = 'valid'
  AND m.is_mock = {false_literal}
  AND ls.sale_price_cents > 0
  AND m.purchase_cost_cents >= CAST(ls.sale_price_cents * (1 - :min_profit_margin) AS INTEGER)
"""

# 冲突类型 ⑤：规格指纹与货源侧当前指纹不一致 —— P0，禁止上架
# （命中后映射自动转 pending_confirm，关联订单挂起）
CONFLICT_SQL_SPEC_MISMATCH = """
SELECT m.id, m.platform, m.shop_sku_code,
       m.source_sku_code_1688,
       m.spec_signature  AS mapping_signature,
       s.spec_signature  AS current_signature,
       s.spec_json       AS current_spec_json
FROM sku_mapping m
JOIN source_sku s ON s.id = m.source_sku_id
WHERE m.is_deleted = {false_literal}
  AND m.status <> 'invalid'
  AND (m.spec_signature IS NULL OR m.spec_signature <> s.spec_signature)
"""


def _render(sql_template: str, dialect: str) -> str:
    """渲染 SQL 模板中的方言占位符 `{false_literal}`（可能被引用多次）。"""
    false_literal = "FALSE" if dialect == "postgresql" else "0"
    return sql_template.format(false_literal=false_literal)


def get_conflict_queries(dialect: str = "sqlite") -> dict[str, str]:
    """按数据库方言返回全部冲突检测 SQL。

    Args:
        dialect: `sqlite` 或 `postgresql`。

    ★★ 注意：本表**只包含真实可命中的检测器**。`one_to_many` 与 `duplicate_item`
    因部分唯一索引而恒为空，已按 QA-01 / QA-02 结论移除（见文件头部注释）——
    保留一条永远查不到东西的检测，比没有检测更危险：它会让人误以为有保护。

    Returns:
        `{冲突类型: SQL 语句}`。键说明（★ 语义以 ARCH §4.3.1 为准）：
            `many_to_one`      → ③ 跨平台铺货，P1，**永不拦截**
            `duplicate`        → ②a 同店铺内反向重复，P0
            `cost_invalid`     → ④ 成本异常，P0
            `cost_underwater`  → ⑥ 成本倒挂，P1（可配升 P0）
            `spec_mismatch`    → ⑤ 规格指纹不匹配，P0
    """
    is_pg = dialect == "postgresql"
    return {
        ConflictType.DUPLICATE.value: (
            CONFLICT_SQL_DUPLICATE_REVERSE_PG if is_pg else CONFLICT_SQL_DUPLICATE_REVERSE_SQLITE
        ),
        ConflictType.MANY_TO_ONE.value: (
            CONFLICT_SQL_MANY_TO_ONE_PG if is_pg else CONFLICT_SQL_MANY_TO_ONE_SQLITE
        ),
        ConflictType.COST_INVALID.value: _render(CONFLICT_SQL_COST_INVALID, dialect),
        ConflictType.COST_UNDERWATER.value: _render(CONFLICT_SQL_COST_UNDERWATER, dialect),
        ConflictType.SPEC_MISMATCH.value: _render(CONFLICT_SQL_SPEC_MISMATCH, dialect),
    }


# ★ 检测执行的强制排序：`many_to_one`（跨平台）必须**最先执行**，
#   其命中项从后续所有检测的候选集中**剔除**，再跑其余各类型。
#   否则跨平台铺货会被 `duplicate`（②a 同店铺内反向重复）误判为 P0 拦截。
#   —— ARCH §4.3.1 ★ 检测执行的强制排序；`test_mapping_validator.py` 有对应用例。
#
# ★★ `one_to_many` / `duplicate_item` 已从排序中移除（恒空的死 SQL，见文件头部）。
DETECTION_ORDER: tuple[str, ...] = (
    ConflictType.MANY_TO_ONE.value,  # 先跑（P1，仅标记）
    ConflictType.DUPLICATE.value,
    ConflictType.COST_INVALID.value,
    ConflictType.COST_UNDERWATER.value,
    ConflictType.SPEC_MISMATCH.value,
)

# ★ 由**写入边界**结构性阻断、因此不需要读时扫描的冲突类型（QA-01 / QA-02）。
#   这里的键仍在 `ConflictType` 枚举与 `CONFLICT_LEVELS` 里（前端在用），
#   但**没有任何检测 SQL** —— 冲突面板上它们恒为 0 是正确且可解释的。
WRITE_BOUNDARY_GUARDED_TYPES: frozenset[str] = frozenset(
    {
        ConflictType.ONE_TO_MANY.value,  # 一平台 SKU → 多货源 SKU
        "duplicate_item",  # 同一 shop_sku_code 挂多个 shop_item_id
    }
)

# 冲突级别判定（写入 mapping_conflict.level，§4.3.1）
#
# ★ `one_to_many` / `duplicate_item` 两条**保留仅为前端展示与历史数据兼容**：
#   它们在库里不可能发生（唯一索引阻断），冲突面板上恒为 0 属预期。
CONFLICT_LEVELS: dict[str, str] = {
    ConflictType.ONE_TO_MANY.value: ConflictLevel.P0.value,  # 写入边界阻断，恒 0
    ConflictType.DUPLICATE.value: ConflictLevel.P0.value,  # ②a 同店铺内反向重复
    "duplicate_item": ConflictLevel.P0.value,  # 写入边界阻断，恒 0
    ConflictType.MANY_TO_ONE.value: ConflictLevel.P1.value,  # ★ ③ 跨平台铺货：P1
    ConflictType.COST_INVALID.value: ConflictLevel.P0.value,
    ConflictType.COST_UNDERWATER.value: ConflictLevel.P1.value,  # ★ ⑥ 倒挂：P1，可配升 P0
    ConflictType.SPEC_MISMATCH.value: ConflictLevel.P0.value,
}

# ★★ QA-03：检出后必须把 `sku_mapping.status` 置为 `pending_confirm` 的冲突类型 ★★
#   依据 ARCHITECTURE.md:715「命中 spec_mismatch 后映射自动转 pending_confirm」。
#
#   为什么**只有** `spec_mismatch`：它意味着"买家看到的规格 ≠ 货源侧现在的规格"，
#   照发就是**真金白银发错货**，必须让下游 ORD-P0-03 的挂起保护生效。
#   其余类型逐项判断（结论已写入 docs / 交付报告）：
#     * `cost_invalid`（P0）：货是对的，只是成本数据缺失 → 已有 `cost_source` 与
#       「成本待确认」工单流程，置 pending_confirm 会连带阻断发货，代价过大 → 不置位。
#     * `duplicate`（P0）：正向（店铺SKU→货源）查找仍唯一，不影响发什么货 → 不置位。
#     * `many_to_one` / `cost_underwater`（P1）：按 PRD 永不拦截 → 不置位。
#     * `one_to_many` / `duplicate_item`：库里不可能存在 → 不适用。
CONFLICT_TYPES_REQUIRING_PENDING_CONFIRM: frozenset[str] = frozenset(
    {ConflictType.SPEC_MISMATCH.value}
)

# 是否禁止上架（blocking）：P0 拦截，P1 仅提示
CONFLICT_BLOCKING: dict[str, bool] = {
    key: (level == ConflictLevel.P0.value) for key, level in CONFLICT_LEVELS.items()
}

# ★ 级别可由配置项覆盖的类型（运营改配置即可升级拦截，不改代码）
CONFIGURABLE_LEVEL_TYPES: frozenset[str] = frozenset({ConflictType.COST_UNDERWATER.value})

# 冲突类型的中文名（冲突面板 / 校验返回体直接展示）
CONFLICT_LABELS: dict[str, str] = {
    ConflictType.ONE_TO_MANY.value: "一平台SKU映射多个货源SKU",
    ConflictType.DUPLICATE.value: "重复映射（同店铺内一个货源SKU被多个店铺SKU引用）",
    "duplicate_item": "重复映射（同一SKU编码挂在多个商品下）",
    ConflictType.MANY_TO_ONE.value: "跨平台铺货（正常业务，仅提示）",
    ConflictType.COST_INVALID.value: "采购成本为空/为0/为负",
    ConflictType.COST_UNDERWATER.value: "成本倒挂（成本≥售价）",
    ConflictType.SPEC_MISMATCH.value: "规格指纹与货源侧不一致",
}


class SkuMapping(BaseMixin, SoftDeleteMixin, Base):
    """★ SKU 映射：店铺平台 SKU ←→ 1688 货源 SKU 的唯一对应关系（最高等级资产）。"""

    __tablename__ = "sku_mapping"

    # ---------- 平台侧（店铺）----------
    platform: Mapped[str] = mapped_column(String(32), nullable=False, comment="taobao/douyin/pdd")
    shop_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="店铺 ID")
    shop_item_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="店铺商品 ID")
    shop_sku_code: Mapped[str] = mapped_column(String(128), nullable=False, comment="店铺 SKU 编码")
    shop_sku_name: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="店铺 SKU 规格描述")
    listing_product_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="FK → listing_product.id")
    listing_sku_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="FK → listing_sku.id")

    # ---------- 货源侧（1688）----------
    source_product_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="FK → source_product.id")
    source_sku_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="FK → source_sku.id")
    source_product_1688_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="1688 商品 ID（冗余，便于无 JOIN 查询）"
    )
    source_sku_code_1688: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="1688 SKU 编码")
    source_sku_name: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="1688 SKU 规格描述")
    spec_signature: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="规格指纹：规格名值对排序后 md5（MAP-P0-04）"
    )

    # ---------- 成本（★ v1.4 三层语义：本表是「镜像层」）----------
    #   真源 = source_sku.cost_price_cents（1688 侧）
    #   镜像 = sku_mapping.purchase_cost_cents（随真源自动同步；人工可覆盖）
    #   快照 = order_item.purchase_cost_cents（下单时点固化，永不可变）
    purchase_cost_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="采购成本【单位：分】，必须 > 0（镜像层）"
    )
    cost_currency: Mapped[str] = mapped_column(String(8), nullable=False, default="CNY", comment="币种")
    cost_source: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="auto",
        comment="★v1.4 auto=随货源自动同步 / manual=人工覆盖（此后自动同步不得静默覆盖）",
    )
    cost_overridden_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="★v1.4 最近一次人工覆盖成本的时间"
    )
    cost_overridden_by: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="★v1.4 最近一次人工覆盖成本的操作人"
    )
    last_cost_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="最近成本核对时间")

    # ---------- 状态 ----------
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=MappingStatus.PENDING_CONFIRM.value,
        comment="valid/pending_confirm/invalid/archived",
    )
    has_conflict: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="存在未解决冲突")
    conflict_types: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="逗号分隔：one_to_many|many_to_one|duplicate|cost_invalid|spec_mismatch"
    )
    conflict_level: Mapped[str | None] = mapped_column(String(8), nullable=True, comment="P0（禁止上架）/P1（提示）")
    is_mock: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="Mock 数据；★ 不参与真实履约，订单匹配时硬过滤"
    )

    # ---------- 来源与生效 ----------
    source: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=MappingSource.SYSTEM.value,
        comment="manual/system/third_party/auto_publish",
    )
    effective_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="生效时间")
    expire_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="失效时间")
    last_pushed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="最近推送第三方时间")
    last_push_status: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="success/failed/degraded")
    last_push_adapter: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="最近推送使用的适配器")

    # ---------- 审计 ----------
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="创建人")
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="更新人")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="乐观锁，每次变更 +1")
    remark: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="备注")

    __table_args__ = (
        CheckConstraint("platform IN ('taobao','douyin','pdd')", name="ck_sku_mapping_platform"),
        CheckConstraint(
            "status IN ('valid','pending_confirm','invalid','archived')", name="ck_sku_mapping_status"
        ),
        CheckConstraint(
            "source IN ('manual','system','third_party','auto_publish')", name="ck_sku_mapping_source"
        ),
        CheckConstraint("cost_source IN ('auto','manual')", name="ck_sku_mapping_cost_src"),  # ★v1.4
        CheckConstraint("conflict_level IS NULL OR conflict_level IN ('P0','P1')", name="ck_sku_mapping_level"),
        # ★ 核心唯一索引（部分索引：软删除记录不占用唯一键）
        Index(
            "uq_sku_mapping_shop_sku",
            "platform",
            "shop_id",
            "shop_sku_code",
            unique=True,
            sqlite_where=text("is_deleted = 0"),
            postgresql_where=text("is_deleted = FALSE"),
        ),
        Index("idx_sku_mapping_match", "platform", "shop_id", "shop_sku_code", "status", "is_deleted"),
        Index("idx_sku_mapping_shop_item", "platform", "shop_id", "shop_item_id", "is_deleted"),
        Index("idx_sku_mapping_source_sku", "source_sku_id", "is_deleted", "status"),
        Index("idx_sku_mapping_1688_item", "source_product_1688_id", "is_deleted"),
        Index("idx_sku_mapping_conflict", "has_conflict", "conflict_level", "is_deleted"),
        Index("idx_sku_mapping_status", "status", "is_deleted", "updated_at"),
        Index(
            "idx_sku_mapping_push",
            "updated_at",
            "is_deleted",
            sqlite_where=text("status = 'valid' AND is_mock = 0"),
            postgresql_where=text("status = 'valid' AND is_mock = FALSE"),
        ),
        Index("idx_sku_mapping_deleted", "is_deleted", "deleted_at"),
    )

    # ---------------- 业务辅助方法 ----------------

    @property
    def is_valid(self) -> bool:
        """是否为有效映射（唯一允许参与上架与订单匹配）。"""
        return self.status == MappingStatus.VALID.value and not self.is_deleted

    @property
    def cost_invalid(self) -> bool:
        """采购成本是否异常（为空 / 0 / 负）。"""
        return self.purchase_cost_cents is None or self.purchase_cost_cents <= 0

    @property
    def conflict_type_list(self) -> list[str]:
        """冲突类型列表。"""
        if not self.conflict_types:
            return []
        return [item.strip() for item in self.conflict_types.split(",") if item.strip()]

    def set_conflicts(self, types: list[str], level: str | None = None) -> None:
        """写入冲突标记（不提交事务）。"""
        self.conflict_types = ",".join(types) if types else None
        self.has_conflict = bool(types)
        if level is not None:
            self.conflict_level = level
        elif types:
            levels = {CONFLICT_LEVELS.get(t, ConflictLevel.P1.value) for t in types}
            self.conflict_level = ConflictLevel.P0.value if ConflictLevel.P0.value in levels else ConflictLevel.P1.value
        else:
            self.conflict_level = None

    def bump_version(self) -> int:
        """乐观锁版本号 +1，返回新版本号。"""
        self.version = int(self.version or 0) + 1
        return self.version

    def __repr__(self) -> str:  # noqa: D105
        return f"<SkuMapping {self.platform}:{self.shop_sku_code}->{self.source_sku_code_1688}>"


class MappingConflict(BaseMixin, Base):
    """映射冲突记录（P0 冲突禁止上架）。"""

    __tablename__ = "mapping_conflict"

    sku_mapping_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="FK → sku_mapping.id")
    conflict_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="one_to_many/many_to_one/duplicate/cost_invalid/spec_mismatch",
    )
    level: Mapped[str] = mapped_column(String(8), nullable=False, comment="P0/P1")
    description: Mapped[str] = mapped_column(Text, nullable=False, comment="中文描述（前端直接展示）")
    detail_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONType, nullable=True, comment="冲突明细（涉及的 mapping_id 列表等）"
    )
    is_resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否已解决")
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="解决人")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="解决时间")
    resolve_action: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="manual_fix/auto_fix/ignored"
    )

    __table_args__ = (
        Index("ix_mapping_conflict_mapping", "sku_mapping_id", "is_resolved"),
        Index("ix_mapping_conflict_level", "level", "is_resolved"),
        Index("ix_mapping_conflict_type", "conflict_type", "is_resolved"),
    )

    def resolve(self, action: str, operator: str = "system") -> None:
        """标记冲突已解决（不提交事务）。"""
        self.is_resolved = True
        self.resolve_action = action or ResolveAction.MANUAL_FIX.value
        self.resolved_by = operator
        self.resolved_at = utc_now()

    def __repr__(self) -> str:  # noqa: D105
        return f"<MappingConflict {self.conflict_type}/{self.level} mapping={self.sku_mapping_id}>"


class MappingChangeLog(Base, BaseMixin):
    """映射变更日志（逐字段记录前后值，PRD MAP-P1-02）。"""

    __tablename__ = "mapping_change_log"

    sku_mapping_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="FK → sku_mapping.id")
    change_action: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="create/update/delete/restore/status_change/push",
    )
    field_name: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="变更字段")
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True, comment="原值")
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True, comment="新值")
    change_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ChangeSource.SYSTEM.value, comment="manual/system/third_party"
    )
    operator: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="操作人")
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="变更原因")
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="链路追踪 ID")

    __table_args__ = (
        Index("ix_mapping_change_log_mapping", "sku_mapping_id", "created_at"),
        Index("ix_mapping_change_log_action", "change_action", "created_at"),
    )

    @classmethod
    def build(
        cls,
        *,
        sku_mapping_id: int,
        change_action: str,
        field_name: str | None = None,
        old_value: Any = None,
        new_value: Any = None,
        change_source: str = ChangeSource.SYSTEM.value,
        operator: str = "system",
        reason: str = "",
        trace_id: str = "",
    ) -> "MappingChangeLog":
        """构造一条变更日志（便于服务层批量写入）。"""
        return cls(
            sku_mapping_id=sku_mapping_id,
            change_action=change_action or ChangeAction.UPDATE.value,
            field_name=field_name,
            old_value="" if old_value is None else str(old_value),
            new_value="" if new_value is None else str(new_value),
            change_source=change_source,
            operator=operator,
            reason=reason,
            trace_id=trace_id,
        )

    def __repr__(self) -> str:  # noqa: D105
        return f"<MappingChangeLog mapping={self.sku_mapping_id} {self.change_action}.{self.field_name}>"


__all__ = [
    "CONFIGURABLE_LEVEL_TYPES",
    "CONFLICT_BLOCKING",
    "CONFLICT_LABELS",
    "CONFLICT_LEVELS",
    "CONFLICT_SQL_COST_INVALID",
    "CONFLICT_SQL_COST_UNDERWATER",
    "CONFLICT_SQL_DUPLICATE_REVERSE_PG",
    "CONFLICT_SQL_DUPLICATE_REVERSE_SQLITE",
    "CONFLICT_SQL_MANY_TO_ONE_PG",
    "CONFLICT_SQL_MANY_TO_ONE_SQLITE",
    "CONFLICT_SQL_SPEC_MISMATCH",
    "CONFLICT_TYPES_REQUIRING_PENDING_CONFIRM",
    "DETECTION_ORDER",
    "MappingChangeLog",
    "MappingConflict",
    "SkuMapping",
    "WRITE_BOUNDARY_GUARDED_TYPES",
    "get_conflict_queries",
]
