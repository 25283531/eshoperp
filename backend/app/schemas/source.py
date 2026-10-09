"""货源模块 Schema（§5.5.3）。"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.schemas.common import BaseSchema, cents_to_yuan, iso_or_none
from app.utils.kit import SOURCE_PLATFORM_1688, derive_source_platform

__all__ = [
    "ManualSourceSkuInput",
    "SourceCollectRequest",
    "SourceCsvImportResult",
    "SourceProductDetailVo",
    "SourceProductManualCreate",
    "SourceProductVo",
    "SourceSkuVo",
    "SupplierCreate",
    "SupplierUpdate",
    "SupplierVo",
]


# ---------------------------------------------------------------------------
#  供应商
# ---------------------------------------------------------------------------


class SupplierVo(BaseSchema):
    """供应商响应体。"""

    id: int = 0
    supplier_1688_id: str = ""
    name: str = ""
    location: str | None = None
    lead_time_hours: int | None = None
    moq: int | None = None
    cooperation_score: int | None = None
    status: str = "active"
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_model(cls, model: Any) -> "SupplierVo":
        """从 ORM 对象构造。"""
        return cls(
            id=int(model.id or 0),
            supplier_1688_id=model.supplier_1688_id or "",
            name=model.name or "",
            location=model.location,
            lead_time_hours=model.lead_time_hours,
            moq=model.moq,
            cooperation_score=model.cooperation_score,
            status=model.status or "active",
            created_at=iso_or_none(model.created_at),
            updated_at=iso_or_none(model.updated_at),
        )


class SupplierCreate(BaseSchema):
    """创建供应商。"""

    supplier_1688_id: str = Field(..., min_length=1, max_length=64, description="1688 供应商 ID")
    name: str = Field(..., min_length=1, max_length=255, description="供应商名称")
    location: str | None = None
    lead_time_hours: int | None = None
    moq: int | None = None
    cooperation_score: int | None = None
    status: str = "active"


class SupplierUpdate(BaseSchema):
    """更新供应商（部分字段）。"""

    supplier_1688_id: str | None = None
    name: str | None = None
    location: str | None = None
    lead_time_hours: int | None = None
    moq: int | None = None
    cooperation_score: int | None = None
    status: str | None = None


# ---------------------------------------------------------------------------
#  货源商品
# ---------------------------------------------------------------------------


class SourceProductVo(BaseSchema):
    """货源商品列表响应体（§5.5.3）。"""

    id: int = 0
    product_1688_id: str = ""
    source_platform: str = SOURCE_PLATFORM_1688  # ★ manual=手工录入 / alibaba1688=1688 采集
    title: str = ""
    category_path: str | None = None
    supplier_id: int | None = None
    supplier_name: str | None = None
    cost_price: str = ""
    origin_url: str | None = None
    main_image_url: str | None = None
    stock_status: str | None = None
    status: str = "on_sale"
    collected_at: str | None = None
    sku_count: int = 0

    @classmethod
    def from_model(cls, model: Any, *, supplier_name: str | None = None, sku_count: int = 0) -> "SourceProductVo":
        """从 ORM 对象构造。"""
        return cls(
            id=int(model.id or 0),
            product_1688_id=model.product_1688_id or "",
            source_platform=derive_source_platform(model.product_1688_id, model.params_json),
            title=model.title or "",
            category_path=model.category_path,
            supplier_id=model.supplier_id,
            supplier_name=supplier_name,
            cost_price=cents_to_yuan(model.cost_price_cents),
            origin_url=model.origin_url,
            main_image_url=model.main_image_url,
            stock_status=model.stock_status,
            status=model.status or "on_sale",
            collected_at=iso_or_none(model.collected_at),
            sku_count=int(sku_count or 0),
        )


class SourceSkuVo(BaseSchema):
    """货源 SKU 响应体。"""

    id: int = 0
    source_product_id: int = 0
    sku_code_1688: str = ""
    spec_json: dict[str, Any] = Field(default_factory=dict)
    spec_signature: str = ""
    cost_price: str = ""
    suggested_sale_price: str = ""  # ★ 录入时填的建议售价（暂存在商品 params_json 里）
    stock_qty: int | None = None
    status: str = "on_sale"
    last_checked_at: str | None = None

    @classmethod
    def from_model(
        cls,
        model: Any,
        *,
        suggested_sale_price_cents: int | None = None,
    ) -> "SourceSkuVo":
        """从 ORM 对象构造。

        Args:
            suggested_sale_price_cents: 录入时填的建议售价（分）。不传则展示为空串，
                避免与「售价为 0」混淆。
        """
        return cls(
            id=int(model.id or 0),
            source_product_id=int(model.source_product_id or 0),
            sku_code_1688=model.sku_code_1688 or "",
            spec_json=dict(model.spec_json or {}),
            spec_signature=model.spec_signature or "",
            cost_price=cents_to_yuan(model.cost_price_cents),
            suggested_sale_price=cents_to_yuan(suggested_sale_price_cents)
            if suggested_sale_price_cents is not None
            else "",
            stock_qty=model.stock_qty,
            status=model.status or "on_sale",
            last_checked_at=iso_or_none(model.last_checked_at),
        )


class SourceProductDetailVo(SourceProductVo):
    """货源商品详情（含 skus / assets / params_json）。"""

    params_json: dict[str, Any] = Field(default_factory=dict)
    skus: list[SourceSkuVo] = Field(default_factory=list)
    assets: list[dict[str, Any]] = Field(default_factory=list)


class SourceCollectRequest(BaseSchema):
    """采集货源商品请求（§5.5.3）。"""

    source: str = "1688"
    identifiers: list[str] = Field(default_factory=list, description="商品 ID 或链接，≤50")
    supplier_id: int | None = None


# ---------------------------------------------------------------------------
#  ★ 手工录入 / CSV 导入（不经过 1688 适配器）
# ---------------------------------------------------------------------------


class ManualSourceSkuInput(BaseSchema):
    """手工录入的单个货源 SKU。

    ★ 为什么 SKU 是必填的：货源商品**没有 SKU 就建不了映射、上不了架**，
      放行"只有商品没有 SKU"的录入等于给下游造死路。
    """

    spec_name: str | None = Field(
        default=None, description='规格名；多维规格用英文分号分隔，如 "颜色;尺码"'
    )
    spec_value: str | None = Field(
        default=None, description='规格值；与 spec_name 一一对应，如 "红色;XL"'
    )
    sku_code: str | None = Field(default=None, max_length=128, description="货源 SKU 编码；留空按规格自动生成")
    sale_price: str | float | int | None = Field(default=None, description="建议售价（元）")
    cost_price: str | float | int | None = Field(default=None, description="采购成本（元），下单/定价依据")
    stock_qty: int = 0
    status: str = "on_sale"


class SourceProductManualCreate(BaseSchema):
    """手工录入货源商品（一次请求带 SKU 列表）。"""

    title: str = Field(..., min_length=1, max_length=512, description="商品标题")
    product_code: str | None = Field(
        default=None, max_length=57, description="商品编码（存入前加 MANUAL- 前缀，≤64）"
    )
    category_path: str | None = None
    supplier_id: int | None = None
    cost_price: str | float | int | None = Field(default=None, description="商品级采购成本（元）")
    origin_url: str | None = None
    main_image_url: str | None = None
    status: str = "on_sale"
    stock_status: str | None = None
    # ★ 不设 min_length：空 SKU 列表要落到服务层，用**中文**原因拒绝（400 + failed[]），
    #   而不是 Pydantic 的英文校验错误（422 + "列表应至少包含 1 项"）。
    skus: list[ManualSourceSkuInput] = Field(default_factory=list, max_length=200)


class SourceManualCreateResult(BaseSchema):
    """手工录入结果。"""

    id: int = 0
    product_1688_id: str = ""
    source_platform: str = "manual"
    created: bool = True
    created_skus: int = 0
    updated_skus: int = 0
    skus: list[SourceSkuVo] = Field(default_factory=list)


class SourceImportFailure(BaseSchema):
    """CSV 导入的**单行**失败明细（★ 逐行可读，不静默吞掉）。"""

    row: int = 0
    identifier: str = ""
    reason: str = ""


class SourceCsvImportResult(BaseSchema):
    """CSV 导入结果。"""

    total: int = 0
    created: int = 0
    updated: int = 0
    created_skus: int = 0
    updated_skus: int = 0
    failed: list[SourceImportFailure] = Field(default_factory=list)

    @property
    def failed_count(self) -> int:
        """失败行数。"""
        return len(self.failed)
