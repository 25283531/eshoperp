"""SKU 映射 Schema（§5.5.6 —— 核心模块）。

★ `MappingValidationVo` 是**上架前强制校验**的返回体（PRD MAP-P0-02），
  `blocking=true` 时禁止上架，**无绕过路径**。
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from app.schemas.common import BaseSchema, cents_to_yuan, iso_or_none, parse_money

__all__ = [
    "DetectConflictsRequest",
    "FillBackRequest",
    "FillBackSku",
    "MappingChangeLogVo",
    "MappingConflictVo",
    "MappingCostUpdate",
    "MappingPendingResolveRequest",
    "MappingPendingVo",
    "MappingPushRequest",
    "MappingStatsVo",
    "MappingValidationConflict",
    "MappingValidationMissing",
    "MappingValidationRequest",
    "MappingValidationVo",
    "SkuMappingBatchRequest",
    "SkuMappingDeleteRequest",
    "SkuMappingCreate",
    "SkuMappingUpdate",
    "SkuMappingVo",
]


# ---------------------------------------------------------------------------
#  映射
# ---------------------------------------------------------------------------


class SkuMappingVo(BaseSchema):
    """SKU 映射响应体（§5.5.6，含 v1.4 成本三层语义字段）。"""

    id: int = 0
    platform: str = ""
    shop_id: str = ""
    shop_item_id: str = ""
    shop_sku_code: str = ""
    shop_sku_name: str | None = None
    source_product_id: int | None = None
    source_sku_id: int | None = None
    source_product_1688_id: str | None = None
    source_sku_code_1688: str | None = None
    source_sku_name: str | None = None
    spec_signature: str | None = None

    # ---------- 成本（三层语义：本表是镜像层）----------
    purchase_cost: str = ""
    cost_currency: str = "CNY"
    cost_source: str = "auto"  # ★ auto 自动同步 / manual 人工覆盖
    cost_overridden_at: str | None = None
    cost_overridden_by: str | None = None
    last_cost_check_at: str | None = None

    # ---------- 状态 ----------
    status: str = "pending_confirm"
    has_conflict: bool = False
    conflict_types: list[str] = Field(default_factory=list)
    conflict_level: str | None = None
    is_mock: bool = False
    source: str = "system"
    effective_at: str | None = None
    last_pushed_at: str | None = None
    last_push_status: str | None = None
    is_deleted: bool = False
    deleted_at: str | None = None
    version: int = 1
    created_by: str | None = None
    updated_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    remark: str | None = None

    @classmethod
    def from_model(cls, model: Any) -> "SkuMappingVo":
        """从 ORM 对象构造。"""
        return cls(
            id=int(model.id or 0),
            platform=model.platform or "",
            shop_id=model.shop_id or "",
            shop_item_id=model.shop_item_id or "",
            shop_sku_code=model.shop_sku_code or "",
            shop_sku_name=model.shop_sku_name,
            source_product_id=model.source_product_id,
            source_sku_id=model.source_sku_id,
            source_product_1688_id=model.source_product_1688_id,
            source_sku_code_1688=model.source_sku_code_1688,
            source_sku_name=model.source_sku_name,
            spec_signature=model.spec_signature,
            purchase_cost=cents_to_yuan(model.purchase_cost_cents),
            cost_currency=model.cost_currency or "CNY",
            cost_source=model.cost_source or "auto",
            cost_overridden_at=iso_or_none(model.cost_overridden_at),
            cost_overridden_by=model.cost_overridden_by,
            last_cost_check_at=iso_or_none(model.last_cost_check_at),
            status=model.status or "pending_confirm",
            has_conflict=bool(model.has_conflict),
            conflict_types=model.conflict_type_list,
            conflict_level=model.conflict_level,
            is_mock=bool(model.is_mock),
            source=model.source or "system",
            effective_at=iso_or_none(model.effective_at),
            last_pushed_at=iso_or_none(model.last_pushed_at),
            last_push_status=model.last_push_status,
            is_deleted=bool(model.is_deleted),
            deleted_at=iso_or_none(model.deleted_at),
            version=int(model.version or 1),
            created_by=model.created_by,
            updated_by=model.updated_by,
            created_at=iso_or_none(model.created_at),
            updated_at=iso_or_none(model.updated_at),
            remark=model.remark,
        )


class SkuMappingCreate(BaseSchema):
    """创建映射。金额用字符串「元」，服务端转分。"""

    platform: str = Field(..., description="taobao/douyin/pdd")
    shop_id: str = Field(..., min_length=1, max_length=64)
    shop_item_id: str = Field(..., min_length=1, max_length=64)
    shop_sku_code: str = Field(..., min_length=1, max_length=128)
    shop_sku_name: str | None = None
    source_product_1688_id: str | None = None
    source_sku_code_1688: str | None = None
    source_sku_name: str | None = None
    source_product_id: int | None = None
    source_sku_id: int | None = None
    purchase_cost: str | float | int | None = Field(default=None, description="采购成本（元字符串，如 \"12.50\"）")
    status: str = "valid"
    remark: str | None = None

    def cost_cents(self) -> int:
        """采购成本 → 整数分。"""
        return parse_money(self.purchase_cost, field_name="采购成本")


class SkuMappingUpdate(BaseSchema):
    """更新映射（部分字段）。

    ★ 成本写入语义（ARCH v1.4）：body 含 `purchase_cost` 且未显式传 `cost_source`
      → 服务端自动置 `cost_source='manual'`（人工覆盖）。
      此后货源再变动**不得静默覆盖**，改为生成「成本待确认」工单。
      人工想恢复自动同步需显式传 `cost_source='auto'`。
    """

    shop_sku_name: str | None = None
    source_product_1688_id: str | None = None
    source_sku_code_1688: str | None = None
    source_sku_name: str | None = None
    source_product_id: int | None = None
    source_sku_id: int | None = None
    purchase_cost: str | float | int | None = None
    cost_source: str | None = Field(default=None, description="auto=恢复自动同步 / manual=人工覆盖")
    status: str | None = None
    remark: str | None = None

    @field_validator("cost_source")
    @classmethod
    def _check_cost_source(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip().lower()
        if normalized not in {"auto", "manual"}:
            raise ValueError("cost_source 只能是 auto 或 manual")
        return normalized


class MappingCostUpdate(BaseSchema):
    """单独更新采购成本（供服务层内部调用）。"""

    purchase_cost: str | float | int = Field(..., description="采购成本（元）")
    cost_source: str = "manual"
    reason: str = ""


class SkuMappingBatchRequest(BaseSchema):
    """批量创建 / 更新映射（≤200）。"""

    items: list[SkuMappingCreate] = Field(default_factory=list, max_length=200)


# ---------------------------------------------------------------------------
#  冲突与校验
# ---------------------------------------------------------------------------


class MappingConflictVo(BaseSchema):
    """映射冲突响应体。"""

    id: int = 0
    sku_mapping_id: int = 0
    conflict_type: str = ""
    level: str = "P1"
    description: str = ""
    detail_json: dict[str, Any] | None = None
    is_resolved: bool = False
    resolved_by: str | None = None
    resolved_at: str | None = None
    resolve_action: str | None = None
    created_at: str | None = None

    @classmethod
    def from_model(cls, model: Any) -> "MappingConflictVo":
        """从 ORM 对象构造。"""
        return cls(
            id=int(model.id or 0),
            sku_mapping_id=int(model.sku_mapping_id or 0),
            conflict_type=model.conflict_type or "",
            level=model.level or "P1",
            description=model.description or "",
            detail_json=model.detail_json,
            is_resolved=bool(model.is_resolved),
            resolved_by=model.resolved_by,
            resolved_at=iso_or_none(model.resolved_at),
            resolve_action=model.resolve_action,
            created_at=iso_or_none(model.created_at),
        )


class MappingValidationConflict(BaseSchema):
    """校验返回体中的单项冲突明细。"""

    conflict_type: str = ""
    level: str = "P1"
    shop_sku_code: str = ""
    description: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


class MappingValidationMissing(BaseSchema):
    """校验返回体中的映射缺失项。"""

    shop_sku_code: str = ""
    reason: str = "mapping_not_found"  # mapping_not_found / mapping_invalid / mapping_mock


class MappingValidationVo(BaseSchema):
    """★ 上架前强制校验返回体（PRD MAP-P0-02 / ARCH §5.5.6）。

    `blocking=true` → 禁止上架，**没有任何绕过路径**。

    ★ QA-06：`errors` / `incomplete` 把「本次检测空转了」与「确实没有冲突」区分开。
      检测 SQL 执行失败时 `incomplete=True` 且 `blocking=True`（fail-closed）——
      一个没跑完的检测**不能**被当成"没冲突"放行。
    """

    passed: bool = True
    source_product_id: int = 0
    platform: str = ""
    shop_id: str = ""
    checked_sku_count: int = 0
    missing_mappings: list[MappingValidationMissing] = Field(default_factory=list)
    conflicts: list[MappingValidationConflict] = Field(default_factory=list)
    blocking: bool = False
    blocked_reason: str = ""
    errors: list[str] = Field(default_factory=list)
    incomplete: bool = False


class MappingValidationRequest(BaseSchema):
    """触发强制校验的请求体。"""

    source_product_id: int = Field(..., description="货源商品 ID")
    platform: str = Field(..., description="目标平台")
    shop_id: str = Field(..., description="目标店铺")
    sku_codes: list[str] = Field(default_factory=list, description="待校验的店铺 SKU 编码列表")


class DetectConflictsRequest(BaseSchema):
    """触发冲突检测。"""

    source_product_ids: list[int] = Field(default_factory=list)
    all: bool = False


class MappingPushRequest(BaseSchema):
    """推送映射到第三方。"""

    adapter_name: str = Field(..., description="miaoshou / yitao / generic")
    ids: list[int] = Field(default_factory=list)
    all_valid: bool = False


class MappingPendingVo(BaseSchema):
    """待确认工单（§5.5.6 `/sku-mappings/pending`）。"""

    id: int = 0
    mapping_id: int = 0
    change_type: str = "spec_change"
    old_value: str | None = None
    new_value: str | None = None
    detected_at: str | None = None
    source_product_title: str | None = None
    shop_sku_code: str | None = None
    affected_order_count: int = 0
    source: str = "system"

    @classmethod
    def from_conflict(
        cls,
        conflict: Any,
        *,
        mapping: Any = None,
        source_product_title: str | None = None,
        affected_order_count: int = 0,
    ) -> "MappingPendingVo":
        """从 `MappingConflict` + `SkuMapping` 构造待确认工单。

        变更类型由冲突类型推导：
            spec_mismatch → spec_change
            cost_invalid  → cost_invalid
            cost_underwater → cost_underwater
            其余           → mapping_conflict
        """
        change_map = {
            "spec_mismatch": "spec_change",
            "cost_invalid": "cost_invalid",
            "cost_underwater": "cost_underwater",
        }
        detail = conflict.detail_json or {}
        return cls(
            id=int(conflict.id or 0),
            mapping_id=int(conflict.sku_mapping_id or 0),
            change_type=change_map.get(conflict.conflict_type, "mapping_conflict"),
            old_value=str(detail.get("old_value", "")) if detail.get("old_value") is not None else None,
            new_value=str(detail.get("new_value", "")) if detail.get("new_value") is not None else None,
            detected_at=iso_or_none(conflict.created_at),
            source_product_title=source_product_title,
            shop_sku_code=getattr(mapping, "shop_sku_code", None) if mapping is not None else None,
            affected_order_count=int(affected_order_count or 0),
            source="system",
        )


class MappingPendingResolveRequest(BaseSchema):
    """处理待确认工单。"""

    action: str = Field(..., description="confirm / reject / manual_assign")
    new_source_sku_code_1688: str | None = None
    note: str | None = None


class MappingChangeLogVo(BaseSchema):
    """映射变更日志响应体。"""

    id: int = 0
    sku_mapping_id: int = 0
    change_action: str = ""
    field_name: str | None = None
    old_value: str | None = None
    new_value: str | None = None
    change_source: str = "system"
    operator: str | None = None
    reason: str | None = None
    created_at: str | None = None
    trace_id: str | None = None

    @classmethod
    def from_model(cls, model: Any) -> "MappingChangeLogVo":
        """从 ORM 对象构造。"""
        return cls(
            id=int(model.id or 0),
            sku_mapping_id=int(model.sku_mapping_id or 0),
            change_action=model.change_action or "",
            field_name=model.field_name,
            old_value=model.old_value,
            new_value=model.new_value,
            change_source=model.change_source or "system",
            operator=model.operator,
            reason=model.reason,
            created_at=iso_or_none(model.created_at),
            trace_id=model.trace_id,
        )


class MappingStatsVo(BaseSchema):
    """映射统计（`/sku-mappings/stats`）。"""

    total: int = 0
    valid: int = 0
    pending_confirm: int = 0
    invalid: int = 0
    conflict_p0: int = 0
    conflict_p1: int = 0
    deleted_recent: int = 0


# ---------------------------------------------------------------------------
#  半自动回填（LST-P0-07 售价必填）
# ---------------------------------------------------------------------------


class FillBackSku(BaseSchema):
    """半自动回填的单个 SKU。

    ★ `sale_price` **必填且 > 0**（PRD LST-P0-07 / ARCH v1.5）：
      缺失或 ≤ 0 时服务端**拒绝提交**（422），从源头消灭空售价。
    """

    spec_json: dict[str, Any] = Field(default_factory=dict)
    shop_sku_code: str = Field(..., min_length=1, max_length=128)
    source_sku_code_1688: str | None = None
    purchase_cost: str | float | int | None = None
    sale_price: str | float | int | None = Field(default=None, description="★ 售价（元），必填且 > 0")
    stock_qty: int = 0

    @field_validator("sale_price")
    @classmethod
    def _sale_price_not_blank(cls, value: Any) -> Any:
        """★ 售价不得为 None / 空串 —— 交由服务层做数值校验，此处只挡「完全没传」。

        说明：Pydantic 无法用 Optional 表达「必传但可为 null 时报错」，
        真正的 `> 0` 校验在 `ManualListingAdapter.validate_fill_back_skus()` 中统一执行，
        保证 API 层与任务层走同一套规则（避免两处规则漂移）。
        """
        return value

    def sale_price_cents(self) -> int:
        """售价 → 整数分（服务层校验用）。"""
        return parse_money(self.sale_price, field_name="售价")

    def purchase_cost_cents(self) -> int:
        """采购成本 → 整数分。"""
        return parse_money(self.purchase_cost, field_name="采购成本")


class FillBackRequest(BaseSchema):
    """半自动回填商品 ID 请求体。"""

    shop_item_id: str = Field(..., min_length=1, max_length=64, description="平台生成的商品 ID")
    skus: list[FillBackSku] = Field(default_factory=list, description="SKU 列表，每项 sale_price 必填")


class SkuMappingDeleteRequest(BaseSchema):
    """删除映射（★ 二次确认，映射是最高等级资产）。"""

    reason: str = Field(default="", max_length=255, description="删除原因（写审计）")
    confirm: bool = Field(default=False, description="必须为 true 才允许删除")
