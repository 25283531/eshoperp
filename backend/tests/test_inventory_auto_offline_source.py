"""★ 库存自动下架的「数据源门槛」回归测试（v1.14 §5.8 / INV-P0-03）。

对应 leader 派发的 P0：**自动下架不区分数据源，会把手工导入的在售商品批量下架**。

两组对照（与本套件之外的真实 HTTP 实验互为佐证）：
    ① 手工录入（CSV 兜底路径）且库存 = 0 的在售商品 → **不得**被自动下架，只告警；
    ② 1688 采集（有真实上游）且库存归零的在售商品 → **仍然**被自动下架（保原行为，别误伤）。

判定键是 `inventory_snapshot.source`（**不是** `source_platform`），见 `InventoryService._snapshot_is_auto`。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from app.models.enums import InventorySource, MappingStatus
from app.models.inventory import InventorySnapshot
from app.models.listing import ListingProduct, ListingProductStatus
from app.models.mapping import SkuMapping
from app.models.source import SourceProduct, SourceSku
from app.services.inventory_service import InventoryService

SUFFIX_MANUAL = "INVSRC-MANUAL"
SUFFIX_AUTO = "INVSRC-AUTO"


async def _build_on_sale_chain(
    session: Any,
    suffix: str,
    *,
    manual: bool,
    stock_qty: int,
) -> dict[str, int]:
    """搭一条「货源 SKU → 映射 → 在售平台商品」的完整链路。

    Args:
        session: 数据库会话（用例结束回滚，无需清理）。
        suffix: 唯一后缀，避免与其它用例撞唯一约束。
        manual: True 走手工录入口径（`MANUAL-` 前缀 + params_json 标记）；
                False 走 1688 采集口径。
        stock_qty: 货源 SKU 的当前库存。

    Returns:
        `{"source_sku_id": int, "listing_product_id": int}`。
    """
    product_1688_id = f"MANUAL-{suffix}" if manual else f"1688-{suffix}"
    params_json = {"source_platform": "manual"} if manual else {}

    product = SourceProduct(
        product_1688_id=product_1688_id,
        title=f"库存门槛测试商品 {suffix}",
        cost_price_cents=1800,
        params_json=params_json,
    )
    session.add(product)
    await session.flush()

    sku = SourceSku(
        source_product_id=int(product.id),
        sku_code_1688=f"SKU-{suffix}",
        spec_json={"尺码": "XL"},
        spec_signature=f"sig-{suffix}",
        cost_price_cents=1800,
        stock_qty=int(stock_qty),
        status="on_sale",
    )
    session.add(sku)
    await session.flush()

    listing = ListingProduct(
        platform="taobao",
        shop_id=f"shop-{suffix}",
        shop_item_id=f"item-{suffix}",
        source_product_id=int(product.id),
        title=f"库存门槛测试在售商品 {suffix}",
        status=ListingProductStatus.ON_SALE.value,
        is_mock=False,
        listing_mode="mock",
    )
    session.add(listing)
    await session.flush()

    session.add(
        SkuMapping(
            platform="taobao",
            shop_id=f"shop-{suffix}",
            shop_item_id=f"item-{suffix}",
            shop_sku_code=f"SKU-{suffix}",
            listing_product_id=int(listing.id),
            source_product_id=int(product.id),
            source_sku_id=int(sku.id),
            source_sku_code_1688=f"SKU-{suffix}",
            purchase_cost_cents=1800,
            spec_signature=f"sig-{suffix}",
            status=MappingStatus.VALID.value,
            cost_source="auto",
            source="manual",
        )
    )
    await session.flush()
    return {"source_sku_id": int(sku.id), "listing_product_id": int(listing.id)}


async def _latest_snapshot(session: Any, source_sku_id: int) -> InventorySnapshot | None:
    """取该 SKU 最近一次库存快照。"""
    return (
        (
            await session.execute(
                select(InventorySnapshot)
                .where(InventorySnapshot.source_sku_id == int(source_sku_id))
                .order_by(InventorySnapshot.collected_at.desc(), InventorySnapshot.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def _listing_status(session: Any, listing_product_id: int) -> str:
    """读平台商品当前状态。"""
    row = (
        await session.execute(select(ListingProduct).where(ListingProduct.id == int(listing_product_id)))
    ).scalars().first()
    return str(row.status) if row else "<missing>"


@pytest.mark.asyncio
async def test_snapshot_source_key_matrix(session: Any) -> None:
    """★ 判定键矩阵：只有自动同步来源允许自动下架；手工与无快照一律不开闸。"""
    fixture = await _build_on_sale_chain(session, SUFFIX_AUTO, manual=False, stock_qty=10)

    async def _is_auto_with_source(source: str | None) -> bool:
        session.add(
            InventorySnapshot(
                source_sku_id=fixture["source_sku_id"],
                stock_qty=0,
                source=source or "erp_poll",
            )
        )
        await session.flush()
        return await InventoryService._snapshot_is_auto(session, fixture["source_sku_id"])

    # 自动同步来源 → 放行
    assert await _is_auto_with_source(InventorySource.ERP_POLL.value) is True
    assert await _is_auto_with_source(InventorySource.THIRD_PARTY_PUSH.value) is True
    # 手工维护来源 → 不放行
    assert await _is_auto_with_source(InventorySource.MANUAL_IMPORT.value) is False
    assert await _is_auto_with_source(InventorySource.MANUAL_EDIT.value) is False


@pytest.mark.asyncio
async def test_no_snapshot_never_auto_offlines(session: Any) -> None:
    """★ 无快照 = unknown：保守优先，绝不自动下架。"""
    fixture = await _build_on_sale_chain(session, SUFFIX_MANUAL, manual=True, stock_qty=0)
    session.expire_all()

    assert await InventoryService._snapshot_is_auto(session, fixture["source_sku_id"]) is False


@pytest.mark.asyncio
async def test_manual_imported_zero_stock_stays_on_sale(session: Any) -> None:
    """★ 主路径：手工录入（库存没填 = 0）的在售商品，跑完 `inventory_sync` 后**仍在售**。

    这是本次 P0 的正例 —— 修复前它会被 `inventory_sync` 静默下架，
    而运营会往「供应商缺货」方向归因，极难定位到「我刚导了个 CSV」。
    """
    fixture = await _build_on_sale_chain(session, SUFFIX_MANUAL, manual=True, stock_qty=0)

    await InventoryService.sync(session, source_sku_ids=[fixture["source_sku_id"]], operator="tester")
    await session.flush()
    session.expire_all()

    snapshot = await _latest_snapshot(session, fixture["source_sku_id"])
    assert snapshot is not None, "库存同步必须留下快照，否则该 SKU 处于检测盲区"
    # ① 写侧：手工商品的数据必须被如实标注为 manual_import（不得伪造成 erp_poll）
    assert snapshot.source == InventorySource.MANUAL_IMPORT.value
    # ② 结果：商品仍在售（本 P0 的核心断言）
    assert await _listing_status(session, fixture["listing_product_id"]) == "on_sale"
    # ③ 仍然产生了告警（GET /inventory/alerts 可见），并标记出「不会自动下架」
    alerts, total = await InventoryService.list_alerts(session, alert_type="out_of_stock")
    assert total >= 1
    mine = [a for a in alerts if int(a.source_sku_id) == int(fixture["source_sku_id"])]
    assert mine, "手工缺货必须出现在告警里 —— 不下架 ≠ 不告警"
    assert all(a.data_source == InventorySource.MANUAL_IMPORT.value for a in mine)
    assert all(a.auto_offline_allowed is False for a in mine)
    # ④ 只针对本 SKU 的告警执行时，一条都不该下架
    single = [a for a in mine if a.suggested_action == "offline"]
    assert single, "配置 out_of_stock_action=offline 时该告警应落到处置路径"
    assert await InventoryService._apply_actions(session, single, operator="tester") == 0
    await session.flush()
    session.expire_all()
    assert await _listing_status(session, fixture["listing_product_id"]) == "on_sale"


@pytest.mark.asyncio
async def test_auto_synced_zero_stock_still_offlines(session: Any) -> None:
    """★ 对照组：1688 来源（自动同步）库存归零 → 仍然自动下架（保原行为，别误伤）。"""
    fixture = await _build_on_sale_chain(session, SUFFIX_AUTO, manual=False, stock_qty=0)

    await InventoryService.sync(session, source_sku_ids=[fixture["source_sku_id"]], operator="tester")
    await session.flush()
    session.expire_all()

    snapshot = await _latest_snapshot(session, fixture["source_sku_id"])
    assert snapshot is not None
    assert snapshot.source == InventorySource.ERP_POLL.value
    # 核心断言：有真实上游的数据源归零，自动下架照旧生效（门槛没有把正常能力砍掉）
    assert await _listing_status(session, fixture["listing_product_id"]) == "off_shelf"


@pytest.mark.asyncio
async def test_manual_edit_after_auto_history_blocks_auto_offline(session: Any) -> None:
    """★ 状态迁移：即使**历史上**有过自动同步，只要**最近一次**被人工改过 → 退出自动下架范围。

    按 §5.8：自动同步中的 SKU 一旦被人工改库存 → 最近快照变 `manual_edit`
    → 退出自动下架范围，直到下一次成功的自动同步把它拉回 `erp_poll`。

    ★ 为什么这里直接 `_detect_alerts` + `_apply_actions` 而不是再跑一次 `sync()`：
      `sync()` 本身是**一次成功的自动同步**，它会把这个 1688 SKU 的最新快照
      重新标成 `erp_poll`（即文档说的"拉回"）—— 那时理应恢复自动下架能力。
      本用例要验的是「标签处于人工态」这一个时间切面，所以不经过 `sync()`。
    """
    fixture = await _build_on_sale_chain(session, SUFFIX_AUTO, manual=False, stock_qty=0)

    # ① 先有一次自动同步的历史
    session.add(
        InventorySnapshot(
            source_sku_id=fixture["source_sku_id"],
            stock_qty=0,
            source=InventorySource.ERP_POLL.value,
        )
    )
    await session.flush()
    # ② 随后人工改了库存 → 最近一次快照变成 manual_edit
    session.add(
        InventorySnapshot(
            source_sku_id=fixture["source_sku_id"],
            stock_qty=0,
            source=InventorySource.MANUAL_EDIT.value,
        )
    )
    await session.flush()
    session.expire_all()

    assert await InventoryService._snapshot_is_auto(session, fixture["source_sku_id"]) is False

    alerts = await InventoryService._detect_alerts(session)
    mine = [
        a
        for a in alerts
        if int(a.source_sku_id) == int(fixture["source_sku_id"]) and a.suggested_action == "offline"
    ]
    assert mine, "库存为 0 且配置为 offline 时应产生待处置告警"
    assert mine[0].data_source == InventorySource.MANUAL_EDIT.value
    assert mine[0].auto_offline_allowed is False

    assert await InventoryService._apply_actions(session, mine, operator="tester") == 0
    await session.flush()
    session.expire_all()
    assert await _listing_status(session, fixture["listing_product_id"]) == "on_sale"
