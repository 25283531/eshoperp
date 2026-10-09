"""上架任务 / 平台商品 / 凭证 Schema（§5.5.7、§5.5.8、§5.5.2）。"""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from app.schemas.common import BaseSchema, cents_to_yuan, iso_or_none

__all__ = [
    "CredentialCreate",
    "CredentialRevealRequest",
    "CredentialUpdate",
    "CredentialVo",
    "BatchOfflineRequest",
    "FillPriceItem",
    "FillPriceRequest",
    "ListingProductDetailVo",
    "ListingProductVo",
    "ListingSkuVo",
    "PlatformAccountAuthorizeRequest",
    "PlatformAccountCreate",
    "PlatformAccountVo",
    "OfflineRequest",
    "PrecheckResultVo",
    "PublishTaskBatchRequest",
    "PublishTaskCreate",
    "PublishTaskDetailVo",
    "PublishTaskVo",
]


# ---------------------------------------------------------------------------
#  平台账号与凭证
# ---------------------------------------------------------------------------


class PlatformAccountVo(BaseSchema):
    """平台账号响应体（Token 只返回掩码，明文永不外泄）。"""

    id: int = 0
    platform: str = ""
    shop_id: str = ""
    shop_name: str | None = None
    credential_id: int | None = None
    granted_scopes: list[str] = Field(default_factory=list)
    token_masked: str = ""
    token_expires_at: str | None = None
    status: str = "active"

    @classmethod
    def from_model(cls, model: Any, *, token_masked: str = "") -> "PlatformAccountVo":
        """从 ORM 对象构造。"""
        scopes = model.granted_scopes_json
        return cls(
            id=int(model.id or 0),
            platform=model.platform or "",
            shop_id=model.shop_id or "",
            shop_name=model.shop_name,
            credential_id=model.credential_id,
            granted_scopes=[str(s) for s in scopes] if isinstance(scopes, list) else [],
            token_masked=token_masked,
            token_expires_at=iso_or_none(model.token_expires_at),
            status=model.status or "active",
        )


class PlatformAccountCreate(BaseSchema):
    """创建平台账号。"""

    platform: str = Field(..., description="taobao / douyin / pdd / alibaba1688")
    shop_id: str = Field(..., min_length=1, max_length=64)
    shop_name: str | None = None
    granted_scopes: list[str] = Field(default_factory=list)
    credential: dict[str, Any] | None = Field(default=None, description="{app_key,app_secret,access_token}")


class PlatformAccountAuthorizeRequest(BaseSchema):
    """平台账号授权（★ 走 scope_guard 校验，越权 403）。"""

    granted_scopes: list[str] = Field(default_factory=list)


class CredentialVo(BaseSchema):
    """凭证响应体（仅 `value_masked`，明文永不出现在列表接口）。"""

    id: int = 0
    owner_type: str = ""
    owner_key: str = ""
    credential_key: str = ""
    value_masked: str | None = None
    expires_at: str | None = None
    status: str = "active"
    last_verified_at: str | None = None

    @classmethod
    def from_model(cls, model: Any) -> "CredentialVo":
        """从 ORM 对象构造。"""
        return cls(
            id=int(model.id or 0),
            owner_type=model.owner_type or "",
            owner_key=model.owner_key or "",
            credential_key=model.credential_key or "",
            value_masked=model.value_masked,
            expires_at=iso_or_none(model.expires_at),
            status=model.status or "active",
            last_verified_at=iso_or_none(model.last_verified_at),
        )


class CredentialCreate(BaseSchema):
    """创建凭证（明文仅在本次请求内使用，落库即加密）。"""

    owner_type: str = Field(..., description="platform / fulfillment / source")
    owner_key: str = Field(..., description="如 taobao:123456 / miaoshou")
    credential_key: str = Field(..., description="app_key / app_secret / access_token")
    value: str = Field(..., description="凭证明文（★ 永不落库）")
    expires_at: str | None = None


class CredentialUpdate(BaseSchema):
    """更新凭证。"""

    value: str | None = None
    expires_at: str | None = None
    status: str | None = None


class CredentialRevealRequest(BaseSchema):
    """二次验证后查看凭证明文（留审计，60s 有效）。"""

    verify_code: str = Field(..., description="管理员二次验证码")


# ---------------------------------------------------------------------------
#  上架任务
# ---------------------------------------------------------------------------


class PublishTaskVo(BaseSchema):
    """上架任务列表响应体（§5.5.7）。"""

    id: int = 0
    source_product_id: int = 0
    source_product_title: str | None = None
    ai_task_result_id: int | None = None
    platform: str = ""
    shop_id: str = ""
    listing_mode: str = "mock"
    status: str = "pending_precheck"
    is_mock: bool = False
    shop_item_id: str | None = None
    sku_count: int = 0
    precheck_passed: bool = False
    validate_passed: bool = False
    platform_error_code: str | None = None
    error_advice: str | None = None
    package_path: str | None = None
    task_record_id: int | None = None
    created_by: str | None = None
    created_at: str | None = None

    @classmethod
    def from_model(cls, model: Any, *, source_product_title: str | None = None) -> "PublishTaskVo":
        """从 ORM 对象构造。"""
        precheck = model.precheck_result_json or {}
        validate = model.validate_result_json or {}
        return cls(
            id=int(model.id or 0),
            source_product_id=int(model.source_product_id or 0),
            source_product_title=source_product_title,
            ai_task_result_id=model.ai_task_result_id,
            platform=model.platform or "",
            shop_id=model.shop_id or "",
            listing_mode=model.listing_mode or "mock",
            status=model.status or "pending_precheck",
            is_mock=bool(model.is_mock),
            shop_item_id=model.shop_item_id,
            sku_count=len(model.shop_sku_codes_json or []),
            precheck_passed=bool(precheck.get("passed", False)),
            validate_passed=bool(validate.get("passed", False)),
            platform_error_code=model.platform_error_code,
            error_advice=model.error_advice,
            package_path=model.package_path,
            task_record_id=model.task_record_id,
            created_by=model.created_by,
            created_at=iso_or_none(model.created_at),
        )


class PublishTaskDetailVo(PublishTaskVo):
    """上架任务详情（含预检 / 校验结果全文）。"""

    precheck_result: dict[str, Any] | None = None
    validate_result: dict[str, Any] | None = None
    platform_error_msg: str | None = None
    shop_sku_codes: list[str] = Field(default_factory=list)
    published_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_model(cls, model: Any, *, source_product_title: str | None = None) -> "PublishTaskDetailVo":
        """从 ORM 对象构造。"""
        base = PublishTaskVo.from_model(model, source_product_title=source_product_title)
        return cls(
            **base.model_dump(),
            precheck_result=model.precheck_result_json,
            validate_result=model.validate_result_json,
            platform_error_msg=model.platform_error_msg,
            shop_sku_codes=[str(c) for c in (model.shop_sku_codes_json or [])],
            published_at=iso_or_none(model.published_at),
            updated_at=iso_or_none(model.updated_at),
        )


class PublishTaskCreate(BaseSchema):
    """创建上架任务。"""

    source_product_ids: list[int] = Field(default_factory=list, description="货源商品 ID 列表")
    platform: str = Field(..., description="目标平台")
    shop_id: str = Field(..., description="目标店铺")
    mode: str = Field(default="manual", description="real / mock / manual")
    ai_task_result_ids: dict[str, int] = Field(
        default_factory=dict, description="{货源商品ID: 审核通过的 AI 结果ID}"
    )
    scheduled_at: str | None = None


class PublishTaskBatchRequest(BaseSchema):
    """批量创建上架任务（≤50）。"""

    source_product_ids: list[int] = Field(default_factory=list, max_length=50)
    platform: str = Field(...)
    shop_id: str = Field(...)
    mode: str = "manual"


class PrecheckResultVo(BaseSchema):
    """合规预检结果。"""

    passed: bool = False
    failed_items: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
#  平台商品
# ---------------------------------------------------------------------------


class ListingSkuVo(BaseSchema):
    """平台 SKU 响应体。"""

    id: int = 0
    listing_product_id: int = 0
    shop_sku_code: str = ""
    spec_json: dict[str, Any] = Field(default_factory=dict)
    sale_price: str = ""
    status: str = "on_sale"
    missing_price: bool = False  # ★ v1.5：售价为空（存量补填入口的数据源）

    @classmethod
    def from_model(cls, model: Any) -> "ListingSkuVo":
        """从 ORM 对象构造。"""
        price_cents = model.sale_price_cents
        return cls(
            id=int(model.id or 0),
            listing_product_id=int(model.listing_product_id or 0),
            shop_sku_code=model.shop_sku_code or "",
            spec_json=dict(model.spec_json or {}),
            sale_price=cents_to_yuan(price_cents),
            status=model.status or "on_sale",
            missing_price=not (price_cents is not None and int(price_cents) > 0),
        )


class ListingProductVo(BaseSchema):
    """平台商品列表响应体（§5.5.8）。"""

    id: int = 0
    platform: str = ""
    shop_id: str = ""
    shop_item_id: str = ""
    source_product_id: int | None = None
    source_product_title: str | None = None
    title: str | None = None
    status: str = "on_sale"
    is_mock: bool = False
    listing_mode: str = "manual"
    published_at: str | None = None
    offline_at: str | None = None
    offline_reason: str | None = None
    sku_count: int = 0
    missing_price_count: int = 0  # ★ 售价为空的 SKU 数（v1.5 存量补填）
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_model(
        cls,
        model: Any,
        *,
        source_product_title: str | None = None,
        sku_count: int = 0,
        missing_price_count: int = 0,
    ) -> "ListingProductVo":
        """从 ORM 对象构造。"""
        return cls(
            id=int(model.id or 0),
            platform=model.platform or "",
            shop_id=model.shop_id or "",
            shop_item_id=model.shop_item_id or "",
            source_product_id=model.source_product_id,
            source_product_title=source_product_title,
            title=model.title,
            status=model.status or "on_sale",
            is_mock=bool(model.is_mock),
            listing_mode=model.listing_mode or "manual",
            published_at=iso_or_none(model.published_at),
            offline_at=iso_or_none(model.offline_at),
            offline_reason=model.offline_reason,
            sku_count=int(sku_count or 0),
            missing_price_count=int(missing_price_count or 0),
            created_at=iso_or_none(model.created_at),
            updated_at=iso_or_none(model.updated_at),
        )


class ListingProductDetailVo(ListingProductVo):
    """平台商品详情（含 skus[] / mappings[]）。"""

    skus: list[ListingSkuVo] = Field(default_factory=list)
    mappings: list[dict[str, Any]] = Field(default_factory=list)


class FillPriceItem(BaseSchema):
    """存量售价补填单项。"""

    shop_sku_code: str = Field(..., min_length=1, max_length=128)
    sale_price: str | float | int = Field(..., description="★ 售价（元），必填且 > 0")

    @field_validator("sale_price", mode="before")
    @classmethod
    def _reject_blank(cls, value: Any) -> Any:
        """空串 / None 直接拒绝（422 由服务层统一抛出，此处先挡最明显的空值）。"""
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError("sale_price 为必填项，且必须大于 0")
        return value


class FillPriceRequest(BaseSchema):
    """存量售价补填请求（★ v1.5：补填后自动触发 cost_underwater 重算）。"""

    items: list[FillPriceItem] = Field(default_factory=list, description="补填项，sale_price 必填 > 0")
    reason: str | None = None


class OfflineRequest(BaseSchema):
    """下架请求（`POST /listing-products/{id}/offline`，★ 全系统唯一下架入口）。"""

    reason: str = Field(default="", max_length=255, description="下架原因（写审计）")


class BatchOfflineRequest(BaseSchema):
    """批量下架请求（`POST /listing-products/batch-offline`）。"""

    ids: list[int] = Field(default_factory=list, description="平台商品 ID 列表")
    reason: str = Field(default="", max_length=255, description="下架原因")
