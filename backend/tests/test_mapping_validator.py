"""映射校验器测试（★ T-A08 核心用例）。

覆盖 ARCH §4.3.1 六类冲突与「检测顺序硬约束」：
    * 映射缺失 → blocking；
    * `cost_invalid`（成本为空 / 为 0）→ P0 → blocking；
    * `many_to_one`（**跨平台铺货**）→ P1 → **永不拦截**；
    * `cost_underwater`（成本倒挂）→ P1（可配置升 P0）。

★ 关键断言：三个平台铺同一个货源 SKU 属于正常铺货，
  校验结果必须是 `passed=true`（跨平台 **不得**被误判为致命冲突）。
"""

from __future__ import annotations

from app.models.enums import ConflictType, MappingStatus
from app.models.mapping import CONFLICT_LEVELS, DETECTION_ORDER, SkuMapping
from app.models.source import SourceProduct, SourceSku
from app.services.mapping_service import MappingService
from app.services.mapping_validator import ConflictLevelResolver, MappingValidator

SKU_CODE = "SKU-TEST-A"
COST_CENTS = 1200  # 12.00 元


async def _seed_source(session: object, *, cost_cents: int = COST_CENTS) -> tuple[int, int]:
    """建货源商品 + SKU，返回 `(product_id, sku_id)`。"""
    product = SourceProduct(product_1688_id="1688-TEST-001", title="测试货源商品")
    session.add(product)
    await session.flush()
    sku = SourceSku(
        source_product_id=int(product.id),
        sku_code_1688=SKU_CODE,
        spec_json={"颜色": "红", "尺码": "XL"},
        spec_signature="sig-test-001",
        cost_price_cents=cost_cents,
        stock_qty=100,
        status="on_sale",
    )
    session.add(sku)
    await session.flush()
    return int(product.id), int(sku.id)


def _mapping(
    *,
    platform: str,
    shop_id: str,
    product_id: int,
    sku_id: int,
    cost_cents: int | None = COST_CENTS,
) -> SkuMapping:
    """构造映射 ORM 对象（绕过服务层，直接控制字段以便精确断言）。"""
    return SkuMapping(
        platform=platform,
        shop_id=shop_id,
        shop_item_id=f"item-{platform}",
        shop_sku_code=SKU_CODE,
        source_product_id=product_id,
        source_sku_id=sku_id,
        source_sku_code_1688=SKU_CODE,
        purchase_cost_cents=cost_cents,
        spec_signature="sig-test-001",
        status=MappingStatus.VALID.value,
        cost_source="auto",
        source="manual",
    )


async def test_missing_mapping_blocks(session: object) -> None:
    """映射缺失 → `blocking=true`，且 `missing_mappings` 非空。"""
    product_id, _sku_id = await _seed_source(session)
    vo = await MappingValidator.validate(
        session,
        source_product_id=product_id,
        platform="taobao",
        shop_id="shop-taobao",
        sku_codes=[SKU_CODE],
    )
    assert vo.passed is False
    assert vo.blocking is True
    assert vo.missing_mappings, "映射缺失必须出现在 missing_mappings 中"


async def test_cost_invalid_is_p0_blocking(session: object) -> None:
    """成本为空 → `cost_invalid` P0 → 拦截。"""
    product_id, sku_id = await _seed_source(session)
    session.add(_mapping(platform="taobao", shop_id="shop-taobao", product_id=product_id, sku_id=sku_id, cost_cents=None))
    await session.flush()

    vo = await MappingValidator.validate(
        session,
        source_product_id=product_id,
        platform="taobao",
        shop_id="shop-taobao",
        sku_codes=[SKU_CODE],
    )
    types = [c.conflict_type for c in vo.conflicts]
    assert ConflictType.COST_INVALID.value in types, f"应检出成本异常，实际：{types}"
    p0 = [c for c in vo.conflicts if c.level == "P0"]
    assert p0, "成本异常必须是 P0 级"
    assert vo.blocking is True


async def test_valid_mapping_passes(session: object) -> None:
    """单平台、成本正常 → 校验通过。"""
    product_id, sku_id = await _seed_source(session)
    session.add(_mapping(platform="taobao", shop_id="shop-taobao", product_id=product_id, sku_id=sku_id))
    await session.flush()

    vo = await MappingValidator.validate(
        session,
        source_product_id=product_id,
        platform="taobao",
        shop_id="shop-taobao",
        sku_codes=[SKU_CODE],
    )
    assert vo.missing_mappings == []
    p0 = [c for c in vo.conflicts if c.level == "P0"]
    assert p0 == [], f"正常映射不应产生 P0 冲突：{p0}"
    assert vo.blocking is False
    assert vo.passed is True


async def test_cross_platform_many_to_one_never_blocks(session: object) -> None:
    """★ 三个平台铺同一货源 SKU → `many_to_one` P1 → **passed=true**（不得拦截）。"""
    product_id, sku_id = await _seed_source(session)
    for platform, shop_id in (
        ("taobao", "shop-taobao"),
        ("douyin", "shop-douyin"),
        ("pdd", "shop-pdd"),
    ):
        session.add(_mapping(platform=platform, shop_id=shop_id, product_id=product_id, sku_id=sku_id))
    await session.flush()

    conflicts = await MappingValidator.detect_for_validation(
        session, source_product_id=product_id, sku_codes=[SKU_CODE]
    )
    types = [c.conflict_type for c in conflicts]
    assert ConflictType.MANY_TO_ONE.value in types, f"跨平台铺货应被检出为 many_to_one，实际：{types}"

    for conflict in conflicts:
        if conflict.conflict_type == ConflictType.MANY_TO_ONE.value:
            assert conflict.level == "P1", "跨平台铺货固定 P1"

    assert await ConflictLevelResolver.is_blocking(
        session, ConflictType.MANY_TO_ONE.value
    ) is False, "many_to_one 永不拦截"

    vo = await MappingValidator.validate(
        session,
        source_product_id=product_id,
        platform="taobao",
        shop_id="shop-taobao",
        sku_codes=[SKU_CODE],
    )
    blocking_conflicts = [c for c in vo.conflicts if c.level == "P0"]
    assert blocking_conflicts == [], f"跨平台不应产生 P0：{blocking_conflicts}"
    assert vo.blocking is False
    assert vo.passed is True, "★ 跨平台铺货属于正常经营，校验必须通过"


async def test_detection_order_and_levels() -> None:
    """检测顺序硬约束：`many_to_one` 必须第一个跑；级别表与之一致。"""
    assert DETECTION_ORDER[0] == ConflictType.MANY_TO_ONE.value
    assert CONFLICT_LEVELS[ConflictType.MANY_TO_ONE.value] == "P1"
    assert CONFLICT_LEVELS[ConflictType.COST_UNDERWATER.value] == "P1"
    assert CONFLICT_LEVELS[ConflictType.ONE_TO_MANY.value] == "P0"
    assert CONFLICT_LEVELS[ConflictType.DUPLICATE.value] == "P0"
    assert CONFLICT_LEVELS[ConflictType.SPEC_MISMATCH.value] == "P0"
    assert CONFLICT_LEVELS[ConflictType.COST_INVALID.value] == "P0"


async def test_cost_underwater_recompute_is_p1(session: object) -> None:
    """成本倒挂：售价 < 成本 → `cost_underwater` P1（先过滤空售价，不静默）。"""
    from app.models.listing import ListingProduct, ListingSku

    product_id, sku_id = await _seed_source(session, cost_cents=5000)
    listing = ListingProduct(
        platform="taobao",
        shop_id="shop-taobao",
        shop_item_id="item-taobao",
        source_product_id=product_id,
        title="测试商品",
        status="on_sale",
        is_mock=True,
        listing_mode="manual",
    )
    session.add(listing)
    await session.flush()
    listing_sku = ListingSku(
        listing_product_id=int(listing.id),
        shop_sku_code=SKU_CODE,
        spec_json={"颜色": "红"},
        sale_price_cents=1000,  # ★ 售价 10 元 < 成本 50 元 → 倒挂
        status="on_sale",
    )
    session.add(listing_sku)
    await session.flush()
    mapping = _mapping(
        platform="taobao", shop_id="shop-taobao", product_id=product_id, sku_id=sku_id, cost_cents=5000
    )
    # ★ 倒挂检测 SQL 通过 `sku_mapping.listing_sku_id` 关联在售 SKU，必须显式绑定
    mapping.listing_sku_id = int(listing_sku.id)
    session.add(mapping)
    await session.flush()

    conflicts = await MappingValidator.recompute_cost_underwater(
        session, listing_product_id=int(listing.id)
    )
    assert any(
        c.conflict_type == ConflictType.COST_UNDERWATER.value for c in conflicts
    ), f"售价低于成本应检出成本倒挂，实际：{[c.conflict_type for c in conflicts]}"
    for conflict in conflicts:
        if conflict.conflict_type == ConflictType.COST_UNDERWATER.value:
            assert conflict.level == "P1"


async def test_manual_cost_not_overwritten_silently(session: object) -> None:
    """★ 人工覆盖成本（`cost_source='manual'`）后，真源变动**不静默覆盖**。"""
    from app.schemas.mapping import SkuMappingCreate

    product_id, sku_id = await _seed_source(session, cost_cents=1200)
    mapping = await MappingService.create(
        session,
        SkuMappingCreate(
            platform="taobao",
            shop_id="shop-taobao",
            shop_item_id="item-taobao",
            shop_sku_code=SKU_CODE,
            source_product_id=product_id,
            source_sku_id=sku_id,
            source_sku_code_1688=SKU_CODE,
            purchase_cost=18.88,  # 人工覆盖
        ),
        operator="tester",
    )
    assert mapping.cost_source == "manual", "传 purchase_cost 未显式 cost_source 时应自动置 manual"

    # 真源涨价
    sku = await session.get(SourceSku, sku_id)
    sku.cost_price_cents = 3000
    await session.flush()

    await MappingService.sync_cost_from_source(session, operator="system")
    await session.flush()
    await session.refresh(mapping)

    assert mapping.purchase_cost_cents == 1888, (
        "人工覆盖的成本被静默冲掉 —— 这是明令禁止的失效模式"
    )
