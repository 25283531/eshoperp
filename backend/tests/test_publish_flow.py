"""上架链路测试（MAP-P0-02 硬拦截 + 用户决策 ① 售价必填）。

覆盖：
    1. ★ 售价必填（PRD LST-P0-07）：`sale_price` 缺失 / ≤ 0 → **422**；
    2. 半自动适配器 `publish()` 对零售价 → FATAL（不静默上架）；
    3. ★ AIR-P0-03：引用未 `approved` 的 AI 产出 → 422 / 4005；
    4. 映射校验不通过 → `validate()` 抛出 422，且 `data` 携带完整 `MappingValidationVo`。
"""

from __future__ import annotations

import pytest

from app.adapters.listing.manual import (
    SALE_PRICE_REQUIRED_MESSAGE,
    ManualListingAdapter,
    validate_fill_back_skus,
)
from app.core.errors import BusinessError, ErrorCode
from app.models.enums import ReviewStatus
from app.models.asset import AiTaskResult
from app.services.mapping_validator import MappingValidator
from app.services.publish_service import PublishService


def test_sale_price_missing_rejected() -> None:
    """★ 售价缺失 → 422（不得出现 sale_price_cents=0 的商品）。"""
    with pytest.raises(BusinessError) as excinfo:
        validate_fill_back_skus([{"shop_sku_code": "SKU-A", "sale_price": None}])
    assert excinfo.value.http_status == 422
    assert SALE_PRICE_REQUIRED_MESSAGE.split("（")[0] in str(excinfo.value)


def test_sale_price_zero_or_negative_rejected() -> None:
    """★ 售价 ≤ 0 → 422。"""
    for price in (0, -1, "0", "-3.5", "", "  "):
        with pytest.raises(BusinessError) as excinfo:
            validate_fill_back_skus([{"shop_sku_code": "SKU-A", "sale_price": price}])
        assert excinfo.value.http_status == 422, f"售价 {price!r} 应被拒绝"


def test_sale_price_empty_skus_rejected() -> None:
    """空 skus → 422。"""
    with pytest.raises(BusinessError) as excinfo:
        validate_fill_back_skus([])
    assert excinfo.value.http_status == 422
    assert int(excinfo.value.code) == int(ErrorCode.PARAM_ERROR)


def test_sale_price_accepted_and_normalized() -> None:
    """合法售价 → 规范化为整数分。"""
    normalized = validate_fill_back_skus(
        [{"shop_sku_code": "SKU-A", "sale_price": "29.90"}, {"shop_sku_code": "SKU-B", "sale_price": 30}]
    )
    assert [item["sale_price_cents"] for item in normalized] == [2990, 3000]


async def test_manual_adapter_publish_rejects_zero_price(session: object) -> None:
    """半自动适配器 `publish()` 对零售价 → FATAL（不静默上架）。"""
    from app.adapters.listing.base import ListingPayload, ListingSkuPayload

    adapter = ManualListingAdapter(session=session, platform="taobao")
    payload = ListingPayload(
        source_product_id=1,
        shop_id="shop-taobao",
        title="零售价商品",
        selling_points=["卖点一"],
        attributes_json={},
        category_id=None,
        main_images=[],
        detail_images=[],
        skus=[
            ListingSkuPayload(
                spec_json={"颜色": "红"},
                sale_price_cents=0,  # ★ 零售价
                stock_qty=10,
                source_sku_code_1688="1688-A",
                purchase_cost_cents=1200,
            )
        ],
    )
    result = await adapter.invoke("publish", payload=payload)
    assert result is not None
    assert not result.ok, "零售价必须被拒绝，不得静默上架"
    assert SALE_PRICE_REQUIRED_MESSAGE.split("（")[0] in (result.message or "")


async def test_ai_result_must_be_approved(session: object) -> None:
    """★ AIR-P0-03：未 approved 的产出禁止被上架引用（422 / 4005）。"""
    result = AiTaskResult(
        ai_task_id=1,
        output_title="未审核标题",
        review_status=ReviewStatus.PENDING.value,
    )
    session.add(result)
    await session.flush()

    with pytest.raises(BusinessError) as excinfo:
        await MappingValidator.validate_ai_result_approved(session, int(result.id))
    assert excinfo.value.http_status == 422
    assert int(excinfo.value.code) == int(ErrorCode.PUBLISH_ASSET_NOT_APPROVED)


async def test_validate_endpoint_returns_422_with_full_vo(client: object) -> None:
    """★ 端点层：映射校验不通过 → **HTTP 422**，且 `data` 携带完整 `MappingValidationVo`。

    这是 MAP-P0-02 的落地口径：前端拿到 422 后直接渲染 `data.conflicts[]` / `missing_mappings[]`。
    """
    response = await client.post(
        "/api/v1/sku-mappings/validate",
        json={
            "source_product_id": 999999,
            "platform": "taobao",
            "shop_id": "shop-not-exist",
            "sku_codes": ["SKU-NOT-EXIST"],
        },
    )
    assert response.status_code == 422, f"映射校验不通过必须返回 422，实际 {response.status_code}"
    body = response.json()
    assert body["code"] != 0
    data = body.get("data") or {}
    assert "blocking" in data, "422 响应必须携带完整 MappingValidationVo"
    assert data["blocking"] is True
    assert data["missing_mappings"], "缺失明细必须返回"


async def test_validate_blocking_vo_fields(session: object) -> None:
    """服务层 `validate()` 在映射缺失时 `blocking=true`、`passed=false`。"""
    vo = await MappingValidator.validate(
        session,
        source_product_id=999999,
        platform="taobao",
        shop_id="shop-not-exist",
        sku_codes=["SKU-NOT-EXIST"],
    )
    assert vo.blocking is True
    assert vo.passed is False
    assert vo.missing_mappings
    assert vo.blocked_reason


def test_publish_service_has_single_execute_entry() -> None:
    """★ 结构性断言：`execute()` 是唯一的上架执行入口（附录 A 第 3 条）。"""
    import inspect

    source = inspect.getsource(PublishService.execute)
    assert "MappingValidator.validate" in source, "上架前必须先做映射校验"
    assert "publish" in source
