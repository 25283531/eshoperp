"""★ QA 探针 B 实测复现的两个 P0 缺陷 —— 回归锁（T-A08 补充）。

缺陷 1（半自动主路径 100% 500）：
    `PublishTask.platform` 是**字符串列**，服务层 `ManualListingAdapter(platform=task.platform)`
    把 str 塞进声明为 `Platform` 的形参，随后所有 `self.platform.value` 访问抛
    `AttributeError: 'str' object has no attribute 'value'`，
    `GET /publish-tasks/manual/{id}/form-data` 与 `/package` 直接 500。

缺陷 2（AI 重构 100% 失败）：
    `AiClientFactory.create(session)` 中 `session` 是 **keyword-only** 形参，
    位置传参会把 AsyncSession 绑到 `name` 上 → `AI_CLIENT_REGISTRY.get(session)` 落空 → 1099。

本文件把这两条锁死：既验证修复后的正确行为，也验证误用会立刻报错（不静默降级）。
"""

from __future__ import annotations

import pytest

from app.adapters.ai.factory import AiClientFactory
from app.adapters.listing.base import ListingPayload, ListingSkuPayload, normalize_platform
from app.adapters.listing.manual import ManualListingAdapter
from app.adapters.listing.mock import MockListingAdapter
from app.models.enums import ListingMode, Platform, PublishStatus
from app.models.publish import PublishTask
from app.models.source import SourceProduct, SourceSku
from app.services.publish_service import PublishService


def _payload() -> ListingPayload:
    """构造一个最小上架载荷（与 QA 探针一致）。"""
    return ListingPayload(
        source_product_id=1,
        shop_id="qa-shop",
        title="演示标题",
        selling_points=["卖点1"],
        attributes_json={},
        category_id="16",
        main_images=[],
        detail_images=[],
        skus=[
            ListingSkuPayload(
                spec_json={"颜色": "红"},
                sale_price_cents=9900,
                stock_qty=10,
                source_sku_code_1688="SKU-A",
                purchase_cost_cents=1200,
            )
        ],
    )


# ======================================================================
#  缺陷 1：platform 类型漂移
# ======================================================================


def test_normalize_platform_coerces_str_and_unknown() -> None:
    """`normalize_platform` 对枚举 / 字符串 / 空值 / 非法值的四种行为。"""
    assert normalize_platform("taobao") is Platform.TAOBAO
    assert normalize_platform("PDD") is Platform.PDD
    assert normalize_platform(Platform.DOUYIN) is Platform.DOUYIN
    assert normalize_platform(None) is Platform.TAOBAO
    assert normalize_platform("") is Platform.TAOBAO
    # 非法值：不抛异常，兜底淘宝（并记 warning）
    assert normalize_platform("not-a-platform") is Platform.TAOBAO


def test_manual_adapter_accepts_str_platform() -> None:
    """★ 回归：传 str 平台不再崩（`self.platform.value` 必须可用）。"""
    adapter = ManualListingAdapter(session=None, platform="taobao")
    assert adapter.platform is Platform.TAOBAO, "str 必须被归一化为 Platform 枚举"

    form = adapter.build_form_data(_payload())
    assert form["platform"] == "taobao", f"预填表单平台字段异常：{form['platform']}"
    assert form["sku_list"][0]["sale_price"] == "99.00"

    readme = adapter.build_readme(_payload())
    assert "淘宝" in readme, "操作指引必须带中文平台名"


def test_mock_adapter_accepts_str_platform() -> None:
    """Mock 适配器同样容忍 str 平台（工厂外直接构造的场景）。"""
    adapter = MockListingAdapter(session=None, platform="pdd")
    assert adapter.platform is Platform.PDD
    assert "PDD" in adapter._next_shop_item_id(), "Mock 商品 ID 必须带平台前缀（证明枚举已生效）"


async def test_publish_service_form_data_with_str_platform(session: object) -> None:
    """★ 服务层整链回归：`PublishService.form_data()` 在 `platform` 为 str 时必须 200 级可用。

    这是 QA 探针 1c 的两个 500 接口的后端根因路径，直接按真实调用方式复现。
    """
    product = SourceProduct(
        product_1688_id="qa-p0-product",
        title="QA P0 商品",
        cost_price_cents=1200,
        params_json={},
    )
    session.add(product)
    await session.flush()

    session.add(
        SourceSku(
            source_product_id=int(product.id),
            sku_code_1688="SKU-A",
            spec_json={"颜色": "红"},
            spec_signature="qa-sig",
            cost_price_cents=1200,
            stock_qty=10,
        )
    )
    task = PublishTask(
        source_product_id=int(product.id),
        platform="taobao",  # ★ 字符串，正是缺陷 1 的入参形态
        shop_id="qa-shop",
        listing_mode=ListingMode.MANUAL.value,
        status=PublishStatus.PUBLISHING.value,
    )
    session.add(task)
    await session.flush()

    form = await PublishService.form_data(session, int(task.id))
    assert form["platform"] == "taobao", f"form_data 平台字段异常：{form['platform']}"
    assert form["sku_list"], "预填表单必须携带 SKU 列表"
    assert form["copy_text"], "必须提供可复制文本"

    package = await PublishService.build_manual_package(session, int(task.id))
    assert package is not None and package.package_path, "素材包必须生成成功"


# ======================================================================
#  缺陷 2：AI 工厂参数绑定错位
# ======================================================================


async def test_ai_client_factory_accepts_session_kwarg(session: object) -> None:
    """★ 回归：`create(session=session)` 必须正常返回客户端实例。"""
    client = await AiClientFactory.create(session=session)
    assert client is not None
    assert hasattr(client, "rework_images"), "AI 客户端必须实现 rework_images"


async def test_ai_client_factory_rejects_positional_session(session: object) -> None:
    """★ 回归：`create(session)` 这种误用必须立刻炸，而不是静默降级或 1099。"""
    with pytest.raises(TypeError) as excinfo:
        await AiClientFactory.create(session)  # type: ignore[arg-type]
    message = str(excinfo.value)
    assert "name" in message and "session=session" in message, f"错误信息必须指出正确写法：{message}"
