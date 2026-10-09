"""★ QA 探针 G：三层成本语义（真源 / 镜像 / 快照）。

G-1  改真源成本 → 镜像同步，**历史订单快照绝不改写**（利润必须一动不动）
G-2  人工覆盖（cost_source='manual'）→ 只弹「成本待确认」工单，**绝不静默覆盖**
G-3  利润函数口径复核（真实调用一次，而不是只看代码）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

SEP = "=" * 96
FAILS: list[str] = []


def banner(t: str) -> None:
    """分节标题。"""
    print(f"\n{SEP}\n{t}\n{SEP}")


STATE: dict[str, Any] = {}


async def setup() -> None:
    """造一条完整链路：货源 SKU → 映射 → 订单明细（成本快照）。"""
    from sqlalchemy import delete, select

    from app.core.database import AsyncSessionLocal
    from app.models.enums import MatchStatus, OrderFulfillmentStatus
    from app.models.mapping import SkuMapping
    from app.models.order import Order, OrderItem
    from app.models.source import SourceProduct, SourceSku

    async with AsyncSessionLocal() as s:
        # 清理历史
        for model, cond in (
            (OrderItem, OrderItem.shop_sku_code == "QA-G-SKU"),
            (Order, Order.platform_order_no == "QA-G-ORDER"),
            (SkuMapping, SkuMapping.shop_sku_code == "QA-G-SKU"),
            (SourceSku, SourceSku.sku_code_1688 == "QA-G-SRC"),
            (SourceProduct, SourceProduct.title == "QA-G-PRODUCT"),
        ):
            for row in (await s.execute(select(model).where(cond))).scalars().all():
                await s.delete(row)
        await s.commit()

        prod = SourceProduct(
            product_1688_id="QA-G-ITEM", title="QA-G-PRODUCT",
            supplier_id=None, status="on_sale", is_deleted=False,
        )
        s.add(prod)
        await s.flush()

        src = SourceSku(
            source_product_id=int(prod.id), sku_code_1688="QA-G-SRC",
            spec_json={"颜色": "红", "尺码": "XL"}, spec_signature="qa-g-sig",
            cost_price_cents=1200,  # 12.00 元
            stock_qty=100, status="on_sale", is_deleted=False,
        )
        s.add(src)
        await s.flush()

        m = SkuMapping(
            platform="taobao", shop_id="qa-shop-g", shop_item_id="QA-G-ITEM",
            shop_sku_code="QA-G-SKU", source_sku_id=int(src.id),
            purchase_cost_cents=1200, cost_source="auto",
            status="valid", spec_signature="qa-g-sig", is_mock=False, is_deleted=False,
        )
        s.add(m)
        await s.flush()

        order = Order(
            platform="taobao", shop_id="qa-shop-g", platform_order_no="QA-G-ORDER",
            fulfillment_status=OrderFulfillmentStatus.MATCHED.value,
            adapter_name="local_csv", total_amount_cents=3000, is_mock=True,
        )
        s.add(order)
        await s.flush()

        item = OrderItem(
            order_id=int(order.id), shop_item_id="QA-G-ITEM", shop_sku_code="QA-G-SKU",
            sku_mapping_id=int(m.id), source_sku_id=int(src.id),
            quantity=2,
            purchase_cost_cents=1200,   # ★ 下单时点快照 12.00
            sale_price_cents=3000,      # 30.00
            match_status=MatchStatus.MATCHED.value,
        )
        s.add(item)
        await s.commit()

    STATE.update(source_sku_id=int(src.id), mapping_id=int(m.id),
                 order_id=int(order.id), item_id=int(item.id))


async def snapshot_costs() -> dict[str, Any]:
    """读回三层成本。"""
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.mapping import SkuMapping
    from app.models.order import OrderItem
    from app.models.source import SourceSku

    async with AsyncSessionLocal() as s:
        src = (await s.execute(
            select(SourceSku).where(SourceSku.id == STATE["source_sku_id"]))).scalar_one()
        m = (await s.execute(
            select(SkuMapping).where(SkuMapping.id == STATE["mapping_id"]))).scalar_one()
        it = (await s.execute(
            select(OrderItem).where(OrderItem.id == STATE["item_id"]))).scalar_one()
        return {
            "true_source_cost": int(src.cost_price_cents or 0),
            "mirror_cost": int(m.purchase_cost_cents or 0),
            "mirror_cost_source": m.cost_source,
            "snapshot_cost": int(it.purchase_cost_cents or 0),
        }


async def profit_now() -> int:
    """真实调用一次利润函数。"""
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.order import OrderItem
    from app.services.order_service import OrderService

    async with AsyncSessionLocal() as s:
        it = (await s.execute(
            select(OrderItem).where(OrderItem.id == STATE["item_id"]))).scalar_one()
        return OrderService.profit_cents([it])


async def bump_source_cost(new_cents: int) -> None:
    """改真源成本。"""
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.source import SourceSku

    async with AsyncSessionLocal() as s:
        src = (await s.execute(
            select(SourceSku).where(SourceSku.id == STATE["source_sku_id"]))).scalar_one()
        src.cost_price_cents = new_cents
        await s.commit()


async def run_sync() -> dict[str, int]:
    """真实调用同步服务。"""
    from app.core.database import AsyncSessionLocal
    from app.services.mapping_service import MappingService

    async with AsyncSessionLocal() as s:
        r = await MappingService.sync_cost_from_source(
            s, source_sku_ids=[STATE["source_sku_id"]], operator="qa")
        await s.commit()
        return r


async def set_manual_override(cents: int) -> None:
    """人工覆盖映射成本 → 应置 cost_source='manual'。"""
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.mapping import SkuMapping

    async with AsyncSessionLocal() as s:
        m = (await s.execute(
            select(SkuMapping).where(SkuMapping.id == STATE["mapping_id"]))).scalar_one()
        m.purchase_cost_cents = cents
        m.cost_source = "manual"
        m.cost_overridden_by = "qa"
        await s.commit()


async def count_manual_confirm() -> int:
    """统计「成本待确认」工单数。"""
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.mapping import MappingConflict

    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(MappingConflict).where(
                MappingConflict.sku_mapping_id == STATE["mapping_id"],
                MappingConflict.conflict_type == "cost_manual_confirm",
                MappingConflict.is_resolved.is_(False),
            ))).scalars().all()
        return len(rows)


async def cleanup() -> None:
    """清理探针数据。"""
    from sqlalchemy import delete, select

    from app.core.database import AsyncSessionLocal
    from app.models.mapping import MappingConflict, MappingChangeLog, SkuMapping
    from app.models.order import Order, OrderItem
    from app.models.source import SourceProduct, SourceSku

    async with AsyncSessionLocal() as s:
        for model, cond in (
            (MappingConflict, MappingConflict.sku_mapping_id == STATE.get("mapping_id", -1)),
            (MappingChangeLog, MappingChangeLog.sku_mapping_id == STATE.get("mapping_id", -1)),
            (OrderItem, OrderItem.id == STATE.get("item_id", -1)),
            (Order, Order.id == STATE.get("order_id", -1)),
            (SkuMapping, SkuMapping.id == STATE.get("mapping_id", -1)),
            (SourceSku, SourceSku.id == STATE.get("source_sku_id", -1)),
            (SourceProduct, SourceProduct.title == "QA-G-PRODUCT"),
        ):
            for row in (await s.execute(select(model).where(cond))).scalars().all():
                await s.delete(row)
        await s.commit()


def show_costs(tag: str, c: dict[str, Any]) -> None:
    """打印三层成本。"""
    print(f"  {tag}")
    print(f"      真源 source_sku.cost_price_cents    = {c['true_source_cost']} 分 "
          f"({c['true_source_cost'] / 100:.2f} 元)")
    print(f"      镜像 sku_mapping.purchase_cost_cents= {c['mirror_cost']} 分 "
          f"({c['mirror_cost'] / 100:.2f} 元)  cost_source={c['mirror_cost_source']}")
    print(f"      快照 order_item.purchase_cost_cents = {c['snapshot_cost']} 分 "
          f"({c['snapshot_cost'] / 100:.2f} 元)")


async def main_async() -> None:
    """异步主流程。"""
    await setup()

    banner("G-0  初始状态（真源 12.00 / 镜像 12.00 / 快照 12.00）")
    c0 = await snapshot_costs()
    show_costs("初始", c0)
    p0 = await profit_now()
    print(f"  利润（快照口径）= {p0} 分 ({p0 / 100:.2f} 元)   "
          f"[ (30.00-12.00) x 2 = 36.00 ]")
    if p0 != 3600:
        FAILS.append(f"G-0 初始利润期望 3600 实际 {p0}")

    # ---------------- G-1 ----------------
    banner("G-1  ★ 货源涨价 12.00 → 15.00：镜像必须同步，历史快照与利润必须一动不动")
    await bump_source_cost(1500)
    r1 = await run_sync()
    print(f"  sync_cost_from_source -> {r1}")
    c1 = await snapshot_costs()
    show_costs("同步后", c1)
    p1 = await profit_now()
    print(f"  利润（快照口径）= {p1} 分 ({p1 / 100:.2f} 元)")

    if c1["mirror_cost"] != 1500:
        FAILS.append(f"G-1 镜像成本未同步：期望 1500 实际 {c1['mirror_cost']}")
        print("  [FAIL] 镜像成本未同步")
    else:
        print("  [OK] 镜像成本已同步到 15.00")

    if c1["snapshot_cost"] != 1200:
        FAILS.append(f"G-1 ★ 历史快照被改写：期望 1200 实际 {c1['snapshot_cost']}")
        print("  [FAIL] ★ 历史订单成本快照被改写 —— 附录 A 第 18 条破裂")
    else:
        print("  [OK] ★ 历史订单成本快照保持 12.00，未被改写")

    if p1 != p0:
        FAILS.append(f"G-1 利润被回溯改写：{p0} → {p1}")
        print(f"  [FAIL] 历史订单利润被回溯改写 {p0} → {p1}")
    else:
        print(f"  [OK] 历史订单利润仍是 {p0} 分，完全没动")

    # ---------------- G-2 ----------------
    banner("G-2  ★ 人工覆盖成本（cost_source='manual'）：只提示，绝不静默覆盖")
    await set_manual_override(1350)
    c2 = await snapshot_costs()
    show_costs("人工覆盖为 13.50 后", c2)
    if c2["mirror_cost_source"] != "manual":
        FAILS.append(f"G-2 cost_source 期望 manual 实际 {c2['mirror_cost_source']}")

    await bump_source_cost(2000)   # 真源再涨到 20.00
    before = await count_manual_confirm()
    r2 = await run_sync()
    print(f"  sync_cost_from_source -> {r2}")
    c3 = await snapshot_costs()
    show_costs("真源涨到 20.00 后再同步", c3)
    after = await count_manual_confirm()

    if c3["mirror_cost"] != 1350:
        FAILS.append(f"G-2 ★ 人工覆盖被静默覆盖：期望保持 1350 实际 {c3['mirror_cost']}")
        print("  [FAIL] ★ 人工覆盖成本被静默覆盖")
    else:
        print("  [OK] ★ 人工覆盖成本保持 13.50，未被静默覆盖")

    print(f"  「成本待确认」工单数：{before} → {after}")
    if after <= before:
        FAILS.append("G-2 人工覆盖未生成「成本待确认」工单（等于静默吞掉）")
        print("  [FAIL] 未生成待确认工单 —— 涨价信息被静默吞掉")
    else:
        print("  [OK] 已生成「成本待确认」工单（提示而非覆盖）")

    if c3["snapshot_cost"] != 1200:
        FAILS.append(f"G-2 历史快照被改写：{c3['snapshot_cost']}")
        print("  [FAIL] 历史快照被改写")
    else:
        print("  [OK] 历史订单快照仍为 12.00")

    p2 = await profit_now()
    print(f"  利润（快照口径）= {p2} 分  (初始 {p0} 分)")
    if p2 != p0:
        FAILS.append(f"G-2 利润被改写 {p0} → {p2}")
        print("  [FAIL] 利润被改写")
    else:
        print("  [OK] 利润自始至终未变")

    banner("G  结论")
    if FAILS:
        print(f"  ❌ {len(FAILS)} 项未通过：")
        for f in FAILS:
            print(f"     - {f}")
    else:
        print("  ✅ G 全部通过：三层成本语义正确，历史利润不可回溯改写")
    await cleanup()


if __name__ == "__main__":
    asyncio.run(main_async())
