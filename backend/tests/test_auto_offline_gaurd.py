"""★ 自动下架「数据源前置条件」的对照实验（跑出来的结果，不是读代码得出的结论）。

================================================================================
为什么必须做对照
================================================================================
自动下架是**不可逆**动作。只测"手工商品没被下架"是不够的 —— 如果下架链路本身坏了
（适配器抛错、映射查不到、告警没生成），同样会观察到"没被下架"，于是假保护就混过去了。
所以本文件的结构是**一组对照**：

    [E] 手工场景（不该下架）  → 期望 executed == 0，商品仍 on_sale
    [F] 真缺货场景（该下架）  → 期望 executed >= 1，商品变 off_shelf
    [G] 涨价场景（不该被门槛误伤）→ 期望 executed >= 1，商品变 off_shelf

★ [F] 是 [E] 的对照组：只有 [F] 真的下架了，[E] 的"没下架"才能归因于门槛，
  而不是归因于"下架功能压根不工作"。
★ [G] 是反向对照：涨价告警走 `PriceSnapshot`，与库存快照**无关**，
  门槛若把它一起挡掉，等于用一个保护换掉另一个保护。

每条断言失败时，msg 里都带 [E]/[F]/[G] 标记 + 期望值 vs 实际值，一眼可定位。
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.models.inventory import InventorySnapshot, PriceSnapshot
from app.models.listing import ListingProduct, ListingProductStatus
from app.models.mapping import SkuMapping
from app.models.source import SourceProduct, SourceSku
from app.models.system import AuditLog
from app.services.inventory_service import InventoryService
from app.utils.kit import utc_now

pytestmark = pytest.mark.asyncio

ON_SALE = ListingProductStatus.ON_SALE.value
OFF_SHELF = ListingProductStatus.OFF_SHELF.value


async def _build_chain(
    session: Any,
    suffix: str,
    *,
    manual: bool,
    stock_qty: int,
    cost_cents: int = 1800,
) -> dict[str, int]:
    """搭一条「货源 SKU → 映射 → 在售平台商品」的完整链路（用例结束回滚，无需清理）。

    Args:
        manual: True = 手工 / CSV 录入（无 1688 上游）；False = 1688 采集口径。
    """
    product = SourceProduct(
        product_1688_id=f"MANUAL-{suffix}" if manual else f"1688-{suffix}",
        title=f"自动下架门槛实验商品 {suffix}",
        cost_price_cents=cost_cents,
        params_json={"source_platform": "manual"} if manual else {},
    )
    session.add(product)
    await session.flush()

    sku = SourceSku(
        source_product_id=int(product.id),
        sku_code_1688=f"SKU-{suffix}",
        spec_json={"尺码": "XL"},
        spec_signature=f"sig-{suffix}",
        cost_price_cents=cost_cents,
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
        title=f"自动下架门槛实验在售商品 {suffix}",
        status=ON_SALE,
        is_mock=False,
        listing_mode="manual",
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
            purchase_cost_cents=cost_cents,
            spec_signature=f"sig-{suffix}",
            status="valid",
            cost_source="auto",
            source="manual",
        )
    )
    await session.flush()
    return {"source_sku_id": int(sku.id), "listing_product_id": int(listing.id)}


async def _listing_status(session: Any, listing_product_id: int) -> str:
    """读平台商品当前状态。"""
    row = (
        await session.execute(select(ListingProduct).where(ListingProduct.id == int(listing_product_id)))
    ).scalars().first()
    return str(row.status) if row else "<missing>"


async def _last_auto_offline_audit(session: Any) -> dict[str, Any]:
    """取最近一条 `auto_offline` 审计的 new_value（用于区分"被门槛跳过"/"压根没跑"）。

    ★ `new_value` 是 JSON 列，不同驱动下可能回 dict 也可能是 JSON 字符串 —— 两种都接。
    """
    rows = (
        (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.object_id == "auto_offline")
                .order_by(AuditLog.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return {}
    raw = rows[0].new_value
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            loaded = json.loads(raw)
        except ValueError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


# ===========================================================================
#  [E] 手工场景 —— 不该下架
# ===========================================================================


async def test_e_manual_zero_stock_is_not_auto_offlined(session: Any) -> None:
    """[E1] 手工录入、库存恒为 0 的 SKU → 只告警，**不下架**。

    构造：手工商品（无 1688 上游）库存 0，跑一次 `inventory_sync` 采集
    → 所有库存快照的 stock_qty 都是 0 → 触发缺货告警 → 期望不下架。
    """
    fixture = await _build_chain(session, "GAURD-E1", manual=True, stock_qty=0)

    result = await InventoryService.sync(
        session, source_sku_ids=[fixture["source_sku_id"]], operator="tester"
    )
    await session.flush()
    session.expire_all()

    assert result["auto_offline"] == 0, (
        f"[E1] 手工录入的缺货商品被自动下架了 —— 期望 auto_offline=0，"
        f"实际 {result['auto_offline']}（alerts={result['alerts']}）"
    )
    assert await _listing_status(session, fixture["listing_product_id"]) == ON_SALE, (
        f"[E1] 手工录入商品的状态被改成了 "
        f"{await _listing_status(session, fixture['listing_product_id'])}，期望仍是 {ON_SALE}"
    )

    # 「不下架 ≠ 不告警」：告警必须仍然存在，否则运营完全看不到风险
    alerts = await InventoryService._detect_alerts(session)
    mine = [
        a
        for a in alerts
        if int(a.source_sku_id) == int(fixture["source_sku_id"]) and a.type == "out_of_stock"
    ]
    assert mine, "[E1] 手工缺货必须出现在告警里 —— 不下架不等于不告警"

    # 直接对这条告警执行处置 → executed 必须为 0
    assert await InventoryService._apply_actions(session, mine, operator="tester") == 0, (
        "[E1] 对手工缺货告警执行 `_apply_actions` 竟然下架了商品 —— 门槛未生效"
    )
    await session.flush()
    session.expire_all()
    assert await _listing_status(session, fixture["listing_product_id"]) == ON_SALE, (
        f"[E1] 执行处置后商品仍应 {ON_SALE}，实际 "
        f"{await _listing_status(session, fixture['listing_product_id'])}"
    )

    # ★ 关键：跳过的原因必须是「数据源不可信」，而不是「告警没走到这里」
    audit = await _last_auto_offline_audit(session)
    assert audit, "[E1] 前台置门槛跳过时应留下审计（否则无法区分'被门槛拦下'与'压根没跑'）"
    assert int(audit.get("count") or 0) == 0, f"[E1] 审计里 count 应为 0，实际 {audit}"
    assert any(
        str(key).startswith("skipped_") and int(value or 0) >= 1 for key, value in audit.items()
    ), (
        f"[E1] 审计里必须记下「因门槛跳过了 N 条」，才能证明不是下架链路压根没跑到；实际 {audit}"
    )


async def test_e_no_snapshot_never_auto_offlines(session: Any) -> None:
    """[E2] 完全没有库存快照的 SKU → 缺货告警也必须**不下架**（保守优先）。"""
    fixture = await _build_chain(session, "GAURD-E2", manual=True, stock_qty=0)

    snaps = (
        (
            await session.execute(
                select(InventorySnapshot).where(
                    InventorySnapshot.source_sku_id == int(fixture["source_sku_id"])
                )
            )
        )
        .scalars()
        .all()
    )
    assert not snaps, "[E2] 前置条件：该 SKU 不应有任何库存快照"

    from app.schemas.inventory import InventoryAlertVo

    alert = InventoryAlertVo(
        id=0,
        source_sku_id=int(fixture["source_sku_id"]),
        type="out_of_stock",
        current_stock=0,
        suggested_action="offline",
    )
    executed = await InventoryService._apply_actions(session, [alert], operator="tester")

    assert executed == 0, f"[E2] 无快照 SKU 竟被自动下架 —— 期望 executed=0，实际 {executed}"
    await session.flush()
    session.expire_all()
    assert await _listing_status(session, fixture["listing_product_id"]) == ON_SALE, (
        f"[E2] 无快照 SKU 的商品状态应仍为 {ON_SALE}，实际 "
        f"{await _listing_status(session, fixture['listing_product_id'])}"
    )


# ===========================================================================
#  [F] 真缺货场景 —— 应该下架（[E] 的正向对照）
# ===========================================================================


async def test_f_real_out_of_stock_still_auto_offlines(session: Any) -> None:
    """[F] 1688 采集的 SKU 库存从 >0 归零 → **必须**自动下架。

    ★ 这条是 [E] 的对照组。只有这里真的下架了，[E] 的"没下架"才能归因于门槛生效，
      而不是"下架链路本身坏了"。
    """
    fixture = await _build_chain(session, "GAURD-F", manual=False, stock_qty=0)

    # ① 历史快照：曾经有过正常库存（stock_qty > 0）
    session.add(
        InventorySnapshot(
            source_sku_id=int(fixture["source_sku_id"]),
            stock_qty=10,
            source="erp_poll",
            collected_at=utc_now() - timedelta(hours=2),
        )
    )
    await session.flush()

    # ② 最新快照：库存归零
    result = await InventoryService.sync(
        session, source_sku_ids=[fixture["source_sku_id"]], operator="tester"
    )
    await session.flush()
    session.expire_all()

    assert result["auto_offline"] >= 1, (
        f"[F] 1688 真缺货竟然没被自动下架 —— 期望 auto_offline>=1，"
        f"实际 {result['auto_offline']}（alerts={result['alerts']}）。"
        f"若这条失败，[E] 的'没下架'就可能是下架功能整体失效造成的假保护"
    )
    status = await _listing_status(session, fixture["listing_product_id"])
    assert status == OFF_SHELF, f"[F] 真缺货商品状态应为 {OFF_SHELF}，实际 {status}"


# ===========================================================================
#  [G] 涨价场景 —— 不该被这个门槛误伤
# ===========================================================================


async def test_g_price_increase_is_not_blocked_by_stock_gate(session: Any) -> None:
    """[G] 涨价自动下架**不该**被「库存快照」门槛挡住。

    理由：涨价告警的判据是 `PriceSnapshot`（prev → current 的环比对比），
    它本身已经证明"成本真的变了"，与 `InventorySnapshot` 没有任何关系。
    一个从未维护过库存的手工 SKU，成本翻倍照样应该触发自动下架 ——
    门槛若把它一起挡掉，等于用「防误下架」换掉「防亏本卖」。
    """
    fixture = await _build_chain(session, "GAURD-G", manual=True, stock_qty=0, cost_cents=20000)

    # 涨价动作配成 offline（默认 notify_only 时压根不会走处置路径，测不到门槛）
    await InventoryService.update_config(session, price_increase_action="offline", operator="tester")

    # 一条确凿的涨价快照：100 元 → 200 元，环比 +100%
    session.add(
        PriceSnapshot(
            source_sku_id=int(fixture["source_sku_id"]),
            cost_price_cents=20000,
            prev_price_cents=10000,
            change_rate=Decimal("1.0000"),
            collected_at=utc_now(),
        )
    )
    # ★ 故意**不给**任何库存快照：这是"库存门槛"最容易被误伤的形态
    await session.flush()
    session.expire_all()

    alerts = await InventoryService._detect_alerts(session)
    mine = [
        a
        for a in alerts
        if int(a.source_sku_id) == int(fixture["source_sku_id"]) and a.type == "price_increase"
    ]
    assert mine, "[G] 前置条件：+100% 的涨价必须产生告警（阈值默认 10%）"
    assert mine[0].suggested_action == "offline", (
        f"[G] 前置条件：涨价动作已配成 offline，实际 suggested_action="
        f"{mine[0].suggested_action}"
    )

    executed = await InventoryService._apply_actions(session, mine, operator="tester")
    await session.flush()
    session.expire_all()

    assert executed >= 1, (
        f"[G] 涨价自动下架被「库存快照」门槛一起挡掉了 —— 期望 executed>=1，实际 {executed}。"
        f"涨价判据是 PriceSnapshot，与 InventorySnapshot 无关；门槛不应扩展到这类告警"
    )
    status = await _listing_status(session, fixture["listing_product_id"])
    assert status == OFF_SHELF, f"[G] 涨价商品应被下架（期望 {OFF_SHELF}），实际 {status}"
