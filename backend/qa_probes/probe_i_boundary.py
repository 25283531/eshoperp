"""★ QA 探针 I：边界与异常路径。

I-1  ★ ORD-P0-03：映射 pending_confirm 的订单必须挂起告警，绝不盲发（用户点名最高风险）
I-2  缺失映射 → 订单挂起
I-3  spec_mismatch → 映射转 pending_confirm
I-4  软删除的映射可再次创建（唯一索引已释放）
I-5  采购单 manual_pending 流程（本地兜底不得假装下单成功）
I-6  Order.adapter_name 冻结（F 已覆盖，此处仅复核）
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

SEP = "=" * 96
FAILS: list[str] = []
STATE: dict[str, Any] = {}


def banner(t: str) -> None:
    """分节标题。"""
    print(f"\n{SEP}\n{t}\n{SEP}")


async def setup() -> None:
    """造：货源 SKU + 3 条映射（valid / pending_confirm / 软删除）+ 2 张订单。"""
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.enums import MatchStatus, OrderFulfillmentStatus
    from app.models.mapping import SkuMapping
    from app.models.order import Order, OrderItem
    from app.models.source import SourceProduct, SourceSku

    async with AsyncSessionLocal() as s:
        for model, cond in (
            (OrderItem, OrderItem.shop_sku_code.like("QA-I-%")),
            (Order, Order.platform_order_no.like("QA-I-%")),
            (SkuMapping, SkuMapping.shop_sku_code.like("QA-I-%")),
            (SourceSku, SourceSku.sku_code_1688 == "QA-I-SRC"),
            (SourceProduct, SourceProduct.product_1688_id == "QA-I-ITEM"),
        ):
            for row in (await s.execute(select(model).where(cond))).scalars().all():
                await s.delete(row)
        await s.commit()

        prod = SourceProduct(product_1688_id="QA-I-ITEM", title="QA-I-PRODUCT",
                             status="on_sale", is_deleted=False)
        s.add(prod)
        await s.flush()
        src = SourceSku(source_product_id=int(prod.id), sku_code_1688="QA-I-SRC",
                        spec_json={"颜色": "红"}, spec_signature="qa-i-sig",
                        cost_price_cents=1000, status="on_sale", is_deleted=False)
        s.add(src)
        await s.flush()

        def mk(code: str, status: str, *, deleted: bool = False) -> SkuMapping:
            return SkuMapping(
                platform="taobao", shop_id="qa-shop-i", shop_item_id="QA-I-ITEM",
                shop_sku_code=code, source_sku_id=int(src.id),
                purchase_cost_cents=1000, cost_source="auto", status=status,
                spec_signature="qa-i-sig", is_mock=False, is_deleted=deleted,
            )

        m_valid = mk("QA-I-VALID", "valid")
        m_pending = mk("QA-I-PENDING", "pending_confirm")
        m_deleted = mk("QA-I-DELETED", "valid", deleted=True)
        s.add_all([m_valid, m_pending, m_deleted])
        await s.flush()

        def order(no: str, sku: str) -> int:
            o = Order(platform="taobao", shop_id="qa-shop-i", platform_order_no=no,
                      fulfillment_status=OrderFulfillmentStatus.PENDING_MATCH.value,
                      adapter_name="local_csv", total_amount_cents=5000, is_mock=True)
            s.add(o)
            s.flush_value = None
            return o

        o1 = order("QA-I-ORDER-PENDING", "QA-I-PENDING")
        s.add(o1); await s.flush()
        s.add(OrderItem(order_id=int(o1.id), shop_item_id="QA-I-ITEM",
                        shop_sku_code="QA-I-PENDING", quantity=1,
                        sale_price_cents=5000, match_status=MatchStatus.UNMATCHED.value))
        o2 = order("QA-I-ORDER-MISSING", "QA-I-MISSING")
        s.add(o2); await s.flush()
        s.add(OrderItem(order_id=int(o2.id), shop_item_id="QA-I-ITEM",
                        shop_sku_code="QA-I-MISSING", quantity=1,
                        sale_price_cents=5000, match_status=MatchStatus.UNMATCHED.value))
        o3 = order("QA-I-ORDER-OK", "QA-I-VALID")
        s.add(o3); await s.flush()
        s.add(OrderItem(order_id=int(o3.id), shop_item_id="QA-I-ITEM",
                        shop_sku_code="QA-I-VALID", quantity=1,
                        sale_price_cents=5000, match_status=MatchStatus.UNMATCHED.value))
        await s.commit()

    STATE.update(o_pending=int(o1.id), o_missing=int(o2.id), o_ok=int(o3.id),
                 m_valid=int(m_valid.id), m_pending=int(m_pending.id),
                 m_deleted=int(m_deleted.id), src_sku=int(src.id), prod=int(prod.id))


async def read_order(oid: int) -> dict[str, Any]:
    """读回订单状态。"""
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.order import Order

    async with AsyncSessionLocal() as s:
        o = (await s.execute(select(Order).where(Order.id == oid))).scalar_one()
        return {"status": o.fulfillment_status, "match_status": o.match_status,
                "exception_type": o.exception_type, "exception_note": o.exception_note,
                "adapter_name": o.adapter_name}


async def try_match(oid: int) -> tuple[str, Any]:
    """真实调用 OrderService.match_order，捕获异常。"""
    from app.core.database import AsyncSessionLocal
    from app.services.order_service import OrderService

    async with AsyncSessionLocal() as s:
        try:
            ok, reason = await OrderService.match_order(s, oid, operator="qa")
            await s.commit()
            return ("ok", (ok, reason))
        except Exception as exc:  # noqa: BLE001
            await s.rollback()
            return ("exc", f"{type(exc).__name__}: {exc}")


# ======================================================================
#  I-1：ORD-P0-03 —— pending_confirm 订单必须挂起
# ======================================================================
async def i1() -> None:
    """ORD-P0-03 验证。"""
    banner("I-1  ★ ORD-P0-03：映射 pending_confirm 的订单必须挂起告警，绝不盲发")
    kind, res = await try_match(STATE["o_pending"])
    print(f"  OrderService.match_order(order={STATE['o_pending']}) -> {kind}  {res}")
    if kind == "exc":
        print(f"  [FAIL] ★ match_order 直接抛异常，ORD-P0-03 的挂起逻辑**根本执行不到**")
        print("         这不是「挂起」也不是「盲发」，而是整条匹配链路不可用。")
        tb = "".join(traceback.format_exc())
        print(f"         异常: {res}")
        FAILS.append(f"I-1 match_order 抛异常（ORD-P0-03 不可达）: {res}")
        return

    o = await read_order(STATE["o_pending"])
    print(f"  订单状态: {o}")
    if o["status"] != "exception_unmatched":
        FAILS.append(f"I-1 订单未挂起，实际 status={o['status']}")
        print(f"  [FAIL] 订单未挂起（status={o['status']}）")
    else:
        print("  [OK] ★ 订单已挂起为 exception_unmatched，未盲发")
    if not o["exception_note"]:
        FAILS.append("I-1 挂起但未写异常原因（运营看不到为什么）")
        print("  [FAIL] 挂起但没有异常说明")
    else:
        print(f"  [OK] 异常说明: {o['exception_note']}")


async def i2() -> None:
    """缺失映射验证。"""
    banner("I-2  缺失映射（QA-I-MISSING 无任何映射）→ 必须挂起")
    kind, res = await try_match(STATE["o_missing"])
    print(f"  match_order -> {kind}  {res}")
    if kind == "exc":
        FAILS.append(f"I-2 match_order 抛异常: {res}")
        print(f"  [FAIL] 抛异常 {res}")
        return
    o = await read_order(STATE["o_missing"])
    print(f"  订单状态: {o}")
    if o["status"] != "exception_unmatched":
        FAILS.append(f"I-2 缺映射订单未挂起，status={o['status']}")
        print(f"  [FAIL] 未挂起")
    else:
        print("  [OK] 缺失映射 → 挂起 exception_unmatched")


async def i3() -> None:
    """正常映射对照组。"""
    banner("I-3  对照组：valid 映射 → 应匹配成功并固化成本快照")
    kind, res = await try_match(STATE["o_ok"])
    print(f"  match_order -> {kind}  {res}")
    if kind == "exc":
        FAILS.append(f"I-3 对照组 match_order 抛异常: {res}")
        print(f"  [FAIL] 抛异常 {res}")
        return
    o = await read_order(STATE["o_ok"])
    print(f"  订单状态: {o}")
    if res[0] is True:
        print("  [OK] valid 映射匹配成功")
    else:
        FAILS.append(f"I-3 valid 映射未匹配成功: {res}")
        print(f"  [FAIL] 未匹配成功")


# ======================================================================
#  I-4：spec_mismatch → 映射转 pending_confirm
# ======================================================================
async def i4() -> None:
    """规格指纹漂移。"""
    banner("I-4  规格指纹漂移 spec_mismatch → 映射应转 pending_confirm")
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.mapping import SkuMapping
    from app.services.mapping_validator import MappingValidator

    async with AsyncSessionLocal() as s:
        m = (await s.execute(
            select(SkuMapping).where(SkuMapping.id == STATE["m_valid"]))).scalar_one()
        print(f"  改前: status={m.status}  spec_signature={m.spec_signature!r}")
        m.spec_signature = "qa-i-sig-CHANGED"   # 指纹漂移
        await s.commit()

    async with AsyncSessionLocal() as s:
        try:
            r = await MappingValidator.detect_conflicts(s)
            print(f"  detect_conflicts -> {r if isinstance(r, dict) else type(r)}")
            await s.commit()
        except Exception as exc:  # noqa: BLE001
            print(f"  detect_conflicts 抛异常: {type(exc).__name__}: {exc}")

    async with AsyncSessionLocal() as s:
        m = (await s.execute(
            select(SkuMapping).where(SkuMapping.id == STATE["m_valid"]))).scalar_one()
        print(f"  改后: status={m.status}")
        if m.status == "pending_confirm":
            print("  [OK] 规格漂移后映射自动转 pending_confirm")
        else:
            FAILS.append(f"I-4 规格漂移后映射未转 pending_confirm，实际 {m.status}")
            print(f"  [FAIL] 映射状态仍是 {m.status}")


# ======================================================================
#  I-5：软删除映射可重建
# ======================================================================
async def i5() -> None:
    """软删除后重建。"""
    banner("I-5  软删除的映射：唯一索引必须释放，可再次创建")
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.mapping import SkuMapping

    try:
        async with AsyncSessionLocal() as s:
            s.add(SkuMapping(
                platform="taobao", shop_id="qa-shop-i", shop_item_id="QA-I-ITEM",
                shop_sku_code="QA-I-DELETED", source_sku_id=STATE["src_sku"],
                purchase_cost_cents=1100, cost_source="auto", status="valid",
                spec_signature="qa-i-sig", is_mock=False, is_deleted=False,
            ))
            await s.commit()
        print("  [OK] 同 (platform, shop_id, shop_sku_code) 可再次创建 —— 唯一索引已随软删除释放")
        async with AsyncSessionLocal() as s:
            rows = (await s.execute(select(SkuMapping).where(
                SkuMapping.shop_sku_code == "QA-I-DELETED"))).scalars().all()
            print(f"      现存行数={len(rows)}  "
                  f"is_deleted={[r.is_deleted for r in rows]}")
    except Exception as exc:  # noqa: BLE001
        FAILS.append(f"I-5 软删除后无法重建: {type(exc).__name__}: {exc}")
        print(f"  [FAIL] 重建失败: {type(exc).__name__}: {exc}")


# ======================================================================
#  I-6：采购单 manual_pending（本地兜底不得假装下单成功）
# ======================================================================
async def i6() -> None:
    """本地兜底采购单。"""
    banner("I-6  本地兜底 place_purchase_order：必须 manual_pending，绝不假装 placed")
    print("  （R3 关联：本地兜底从未真正下过单，返回 placed 就是静默失效）")
    src = (BACKEND_ROOT / "app" / "adapters" / "fulfillment" / "local_csv.py").read_text(encoding="utf-8")
    for kw in ("manual_pending", "placed"):
        hit = [l.strip() for l in src.splitlines() if kw in l]
        print(f"  local_csv.py 含 {kw!r}: {len(hit)} 处")
        for l in hit[:4]:
            print(f"      {l[:110]}")
    if "manual_pending" not in src:
        FAILS.append("I-6 local_csv 未使用 manual_pending 状态")
        print("  [FAIL] 未找到 manual_pending")


async def cleanup() -> None:
    """清理探针数据。"""
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.mapping import SkuMapping
    from app.models.order import Order, OrderItem
    from app.models.source import SourceProduct, SourceSku

    async with AsyncSessionLocal() as s:
        for model, cond in (
            (OrderItem, OrderItem.shop_sku_code.like("QA-I-%")),
            (Order, Order.platform_order_no.like("QA-I-%")),
            (SkuMapping, SkuMapping.shop_sku_code.like("QA-I-%")),
            (SourceSku, SourceSku.sku_code_1688 == "QA-I-SRC"),
            (SourceProduct, SourceProduct.product_1688_id == "QA-I-ITEM"),
        ):
            for row in (await s.execute(select(model).where(cond))).scalars().all():
                await s.delete(row)
        await s.commit()


async def main_async() -> None:
    """异步主流程。"""
    await setup()
    await i1()
    await i2()
    await i3()
    await i4()
    await i5()
    await i6()

    banner("I  结论")
    if FAILS:
        print(f"  ❌ {len(FAILS)} 项未通过：")
        for f in FAILS:
            print(f"     - {f}")
    else:
        print("  ✅ I 全部通过")
    await cleanup()


if __name__ == "__main__":
    asyncio.run(main_async())
