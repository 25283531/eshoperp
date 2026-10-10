"""半自动主路径回归：素材包平台标识 + AI 改写标题落库。

覆盖两个已修缺陷（用户决策 ① 把半自动升级为主路径后，这两处都属于高频链路）：

    缺陷 1：`ListingAdapterFactory.create()` 构造适配器时**没有传 platform**，
            `ManualListingAdapter.__init__` 的 `platform` 默认值是 `Platform.TAOBAO`，
            导致抖店 / 拼多多的任务被打成 `manual-taobao-*.zip`、
            README.txt 写「登录淘宝商家后台」—— 直接误导使用者。
            本文件对**三个平台**逐一断言 ZIP 前缀与 README 指引文案。

    缺陷 2：`_persist_listing()` 落库时用的是货源原标题，AI 改写标题永远进不了
            `listing_product` 表。本文件**直接查表**校验：
            有 AI 产出 → 落 AI 标题；无 AI 产出 → 回落货源原标题（不得为空串）。

★ 注意：本文件**不覆盖**卖点（`selling_points`）—— `listing_product` 目前只有
  `title` 一个文案字段，卖点与图片**仍然丢失**，等表结构变更后再补。
"""

from __future__ import annotations

import uuid
import zipfile
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from app.models.asset import AiTaskResult, Asset
from app.models.enums import ListingMode, Platform, PublishStatus, ReviewStatus
from app.models.publish import PublishTask
from app.models.source import SourceProduct, SourceSku
from app.schemas.mapping import FillBackRequest, FillBackSku
from app.services.publish_service import PublishService

PLATFORM_LABELS = {
    Platform.TAOBAO.value: "淘宝",
    Platform.DOUYIN.value: "抖店",
    Platform.PDD.value: "拼多多",
}


async def _make_task(
    session: Any,
    *,
    source_product_id: int,
    platform: str,
    ai_task_result_id: int | None = None,
) -> PublishTask:
    """直接构造 `publish_task`（绕过异步入队，避免污染其它用例的库状态）。"""
    task = PublishTask(
        source_product_id=int(source_product_id),
        ai_task_result_id=ai_task_result_id,
        platform=platform,
        shop_id=f"shop-{platform}",
        listing_mode=ListingMode.MANUAL.value,
        status=PublishStatus.PENDING_PRECHECK.value,
        is_mock=False,
        created_by="tester",
    )
    session.add(task)
    await session.flush()
    return task


async def _seed_source_product(session: Any, *, suffix: str) -> int:
    product = SourceProduct(
        product_1688_id=f"1688-{suffix}",
        title=f"【货源原标题】-{suffix}",
        cost_price_cents=1000,
    )
    session.add(product)
    await session.flush()

    session.add(
        SourceSku(
            source_product_id=int(product.id),
            sku_code_1688=f"SKU-{suffix}",
            spec_json={"颜色": "红"},
            cost_price_cents=1000,
            stock_qty=50,
        )
    )
    session.add(
        Asset(
            source_product_id=int(product.id),
            asset_type="main_image",
            storage_path=f"data/assets/{suffix}/main.jpg",
            content_hash=f"hash-{suffix}",
            version=1,
            is_current=True,
        )
    )
    await session.flush()
    return int(product.id)


async def _seed_ai_result(
    session: Any,
    *,
    output_title: str | None,
    approved: bool = True,
) -> int:
    """造一条 AI 重构产出（默认已审核通过，符合 AIR-P0-03 入口约束）。"""
    result = AiTaskResult(
        ai_task_id=1,
        output_title=output_title,
        output_selling_points="卖点甲\n卖点乙",
        output_attributes_json={"category_id": "1234"},
        review_status=ReviewStatus.APPROVED.value if approved else ReviewStatus.PENDING.value,
    )
    session.add(result)
    await session.flush()
    return int(result.id)


@pytest.mark.parametrize("platform", [Platform.TAOBAO.value, Platform.DOUYIN.value, Platform.PDD.value])
async def test_manual_package_is_tagged_with_real_platform(session: Any, platform: str) -> None:
    """★ 缺陷 1：三个平台的素材包 ZIP 前缀与 README 指引必须指向**本平台**。

    走的是 `PublishService.execute()` → `ListingAdapterFactory.create()` 这条真实链路
    （不是直接 `new ManualListingAdapter(platform=...)`），否则验不到工厂漏传 platform。
    """
    label = PLATFORM_LABELS[platform]
    product_id = await _seed_source_product(session, suffix=f"pkg-{platform}-{uuid.uuid4().hex[:8]}")
    task = await _make_task(session, source_product_id=product_id, platform=platform)

    executed = await PublishService.execute(session, int(task.id))

    assert executed.package_path, f"{platform} 未产出素材包（package_path 为空）"
    zip_path = Path(str(executed.package_path))
    assert zip_path.exists(), f"素材包文件不存在：{zip_path}"

    print(f"\n[{platform}] ZIP 路径: {zip_path}")
    print(f"[{platform}] ZIP 文件名: {zip_path.name}")
    assert zip_path.name.startswith(f"manual-{platform}-"), (
        f"{platform} 的素材包前缀错误：{zip_path.name}"
    )

    with zipfile.ZipFile(zip_path) as zf:
        assert "README.txt" in zf.namelist(), f"素材包缺少 README.txt：{zf.namelist()}"
        readme = zf.read("README.txt").decode("utf-8")
        form_data = zf.read("form_data.json").decode("utf-8")

    print(f"[{platform}] README.txt 原文:\n{readme}")

    assert f"登录 {label} 商家后台" in readme, f"{platform} 的 README 指引文案错误，未写「登录 {label} 商家后台」"
    assert f'"platform": "{platform}"' in form_data, f"{platform} 的 form_data.json platform 字段错误"

    # 其它平台的标签不得串台
    for other_platform, other_label in PLATFORM_LABELS.items():
        if other_platform == platform:
            continue
        assert f"登录 {other_label} 商家后台" not in readme, f"{platform} 的 README 混入了 {other_label} 指引"

    zip_path.unlink(missing_ok=True)


async def test_listing_product_title_uses_ai_title(session: Any) -> None:
    """★ 缺陷 2（有 AI 产出）：`listing_product.title` 必须是 AI 改写标题。

    走完整半自动主路径：`execute()` 产出素材包 → `fill_back()` 回填商品 ID
    → `_persist_listing()` 落库。最后**直接查表**核对。
    """
    suffix = f"ai-{uuid.uuid4().hex[:8]}"
    product_id = await _seed_source_product(session, suffix=suffix)
    ai_title = f"【AI 改写标题】-{suffix}"
    ai_result_id = await _seed_ai_result(session, output_title=ai_title)
    task = await _make_task(
        session,
        source_product_id=product_id,
        platform=Platform.DOUYIN.value,
        ai_task_result_id=ai_result_id,
    )

    executed = await PublishService.execute(session, int(task.id))
    assert executed.package_path, "半自动素材包未生成"
    Path(str(executed.package_path)).unlink(missing_ok=True)

    shop_item_id = f"ITEM-{suffix}"
    await PublishService.fill_back(
        session,
        int(task.id),
        FillBackRequest(
            shop_item_id=shop_item_id,
            skus=[FillBackSku(shop_sku_code=f"SKU-{suffix}", source_sku_code_1688=f"SKU-{suffix}", sale_price="39.90")],
        ),
        operator="tester",
    )

    row = (
        await session.execute(
            text(
                "SELECT id, platform, shop_item_id, title FROM listing_product "
                "WHERE shop_item_id = :shop_item_id"
            ),
            {"shop_item_id": shop_item_id},
        )
    ).first()

    print(f"\n[有 AI 产出] listing_product 直查结果: {tuple(row) if row else None}")
    assert row is not None, "listing_product 未落库"
    assert row[3] == ai_title, f"落库标题不是 AI 标题：{row[3]!r}（期望 {ai_title!r}）"
    assert "货源原标题" not in str(row[3])


async def test_listing_product_title_falls_back_to_source_title(session: Any) -> None:
    """★ 缺陷 2（无 AI 产出）：回落货源原标题，**不得是空字符串**。"""
    suffix = f"noai-{uuid.uuid4().hex[:8]}"
    product_id = await _seed_source_product(session, suffix=suffix)
    source_title = f"【货源原标题】-{suffix}"
    task = await _make_task(session, source_product_id=product_id, platform=Platform.PDD.value)

    executed = await PublishService.execute(session, int(task.id))
    assert executed.package_path, "半自动素材包未生成"
    Path(str(executed.package_path)).unlink(missing_ok=True)

    shop_item_id = f"ITEM-{suffix}"
    await PublishService.fill_back(
        session,
        int(task.id),
        FillBackRequest(
            shop_item_id=shop_item_id,
            skus=[FillBackSku(shop_sku_code=f"SKU-{suffix}", source_sku_code_1688=f"SKU-{suffix}", sale_price="45.00")],
        ),
        operator="tester",
    )

    row = (
        await session.execute(
            text(
                "SELECT id, platform, shop_item_id, title FROM listing_product "
                "WHERE shop_item_id = :shop_item_id"
            ),
            {"shop_item_id": shop_item_id},
        )
    ).first()

    print(f"\n[无 AI 产出] listing_product 直查结果: {tuple(row) if row else None}")
    assert row is not None, "listing_product 未落库"
    assert row[3] == source_title, f"回落标题错误：{row[3]!r}（期望 {source_title!r}）"


async def test_listing_product_title_falls_back_when_ai_title_blank(session: Any) -> None:
    """★ 边界：AI 结果存在但 `output_title` 为空 → 仍回落货源原标题，不得写空串。"""
    suffix = f"blank-{uuid.uuid4().hex[:8]}"
    product_id = await _seed_source_product(session, suffix=suffix)
    source_title = f"【货源原标题】-{suffix}"
    ai_result_id = await _seed_ai_result(session, output_title=None)
    task = await _make_task(
        session,
        source_product_id=product_id,
        platform=Platform.TAOBAO.value,
        ai_task_result_id=ai_result_id,
    )

    executed = await PublishService.execute(session, int(task.id))
    assert executed.package_path, "半自动素材包未生成"
    Path(str(executed.package_path)).unlink(missing_ok=True)

    shop_item_id = f"ITEM-{suffix}"
    await PublishService.fill_back(
        session,
        int(task.id),
        FillBackRequest(
            shop_item_id=shop_item_id,
            skus=[FillBackSku(shop_sku_code=f"SKU-{suffix}", source_sku_code_1688=f"SKU-{suffix}", sale_price="31.00")],
        ),
        operator="tester",
    )

    row = (
        await session.execute(
            text("SELECT title FROM listing_product WHERE shop_item_id = :shop_item_id"),
            {"shop_item_id": shop_item_id},
        )
    ).scalar_one_or_none()

    print(f"\n[AI 标题为空] listing_product.title 直查结果: {row!r}")
    assert row == source_title, f"AI 标题为空时未回落原标题：{row!r}"


async def test_factory_passes_platform_to_manual_adapter(session: Any) -> None:
    """★ 缺陷 1 根因直测：`ListingAdapterFactory.create()` 产出的适配器 platform 必须正确。"""
    from app.adapters.listing.factory import ListingAdapterFactory
    from app.adapters.listing.manual import ManualListingAdapter

    for platform in (Platform.TAOBAO.value, Platform.DOUYIN.value, Platform.PDD.value):
        adapter = await ListingAdapterFactory.create(platform, mode=ListingMode.MANUAL.value, session=session)
        assert isinstance(adapter, ManualListingAdapter)
        assert adapter.platform.value == platform, (
            f"工厂产出的半自动适配器平台错误：{adapter.platform.value}（期望 {platform}）"
        )
        print(f"\n[factory] {platform} -> adapter.platform={adapter.platform.value}")
