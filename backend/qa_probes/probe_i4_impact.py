"""★ QA 探针 I-4 影响面实证：spec_mismatch 检测到但映射不转 pending_confirm，
那么「已上架商品」的订单会不会照发不误？

这是「不报错、只静默失效」的典型形态：
  - 冲突检测确实发现了 spec_mismatch（日志 by_type 里有 1）
  - 但映射 status 仍是 valid
  - → local_csv.match_sku 只看 status == valid
  - → match_order 判定 matched
  - → 订单正常发货，用的是规格已经变了的货源 SKU
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

SEP = "=" * 96
STATE: dict[str, Any] = {}


def banner(t: str) -> None:
    """分节标题。"""
    print(f"\n{SEP}\n{t}\n{SEP}")


async def setup() -> None:
    """造：货源 SKU + 一条 valid 映射 + 一张订单。"""
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.enums import MatchStatus, OrderFulfillmentStatus
    from app.models.mapping import SkuMapping
    from app.models.order import Order, OrderItem
    from app.models.source import SourceProduct, SourceSku

    async with AsyncSessionLocal() as s:
        for model, cond in (
            (OrderItem, OrderItem.shop_sku_code == "QA-SPEC-SKU"),
            (Order, Order.platform_order_no == "QA-SPEC-ORDER"),
            (SkuMapping, SkuMapping.shop_sku_code == "QA-SPEC-SKU"),
            (SourceSku, SourceSku.sku_code_1688 == "QA-SPEC-SRC"),
            (SourceProduct, SourceProduct.product_1688_id == "QA-SPEC-ITEM"),
        ):
            for row in (await s.execute(select(model).where(cond))).scalars().all():
                await s.delete(row)
        await s.commit()

        prod = SourceProduct(product_1688_id="QA-SPEC-ITEM", title="QA-SPEC",
                             status="on_sale", is_deleted=False)
        s.add(prod); await s.flush()
        src = SourceSku(source_product_id=int(prod.id), sku_code_1688="QA-SPEC-SRC",
                        spec_json={"颜色": "红", "尺码": "XL"}, spec_signature="sig-v1",
                        cost_price_cents=1000, status="on_sale", is_deleted=False)
        s.add(src); await s.flush()

        m = SkuMapping(platform="taobao", shop_id="qa-shop-spec", shop_item_id="QA-SPEC-ITEM",
                       shop_sku_code="QA-SPEC-SKU", source_sku_id=int(src.id),
                       purchase_cost_cents=1000, cost_source="auto", status="valid",
                       spec_signature="sig-v1", is_mock=False, is_deleted=False)
        s.add(m); await s.flush()

        o = Order(platform="taobao", shop_id="qa-shop-spec", platform_order_no="QA-SPEC-ORDER",
                  fulfillment_status=OrderFulfillmentStatus.PENDING_MATCH.value,
                  adapter_name="local_csv", total_amount_cents=5000, is_mock=True)
        s.add(o); await s.flush()
        s.add(OrderItem(order_id=int(o.id), shop_item_id="QA-SPEC-ITEM",
                        shop_sku_code="QA-SPEC-SKU", quantity=1, sale_price_cents=5000,
                        match_status=MatchStatus.UNMATCHED.value))
        await s.commit()
    STATE.update(mid=int(m.id), oid=int(o.id), src=int(src.id), prod=int(prod.id))


async def drift_spec() -> None:
    """货源侧改规格 → 指纹漂移。"""
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.source import SourceSku

    async with AsyncSessionLocal() as s:
        src = (await s.execute(select(SourceSku).where(SourceSku.id == STATE["src"]))).scalar_one()
        src.spec_json = {"颜色": "红", "尺码": "XXL"}   # 货源改规格：XL → XXL
        src.spec_signature = "sig-v2"
        await s.commit()
    print("  货源侧规格 XL → XXL，指纹 sig-v1 → sig-v2")


async def run_detect() -> None:
    """跑一次冲突检测（并同步抓取原始 SQL 结果做旁证）。"""
    from sqlalchemy import text

    from app.core.database import AsyncSessionLocal
    from app.models.mapping import get_conflict_queries
    from app.services.mapping_validator import MappingValidator

    # 旁证：直接用 ORM 里那条 spec_mismatch SQL 查一次
    sql = get_conflict_queries("sqlite")["spec_mismatch"]
    async with AsyncSessionLocal() as s:
        raw = (await s.execute(text(sql))).mappings().all()
        print(f"  [旁证] spec_mismatch 原始 SQL 命中 {len(raw)} 行: {[dict(r) for r in raw][:3]}")

    # ① 用「API 默认值」调用（DetectConflictsRequest.all 默认 False）
    async with AsyncSessionLocal() as s:
        r_false = await MappingValidator.detect_conflicts(s, detect_all=False)
        print(f"  detect_conflicts(detect_all=False  ← API 默认) -> {r_false}")
        await s.commit()

    # 清掉刚落库的冲突，避免影响下一组对照
    from sqlalchemy import text as _text
    async with AsyncSessionLocal() as s:
        await s.execute(_text("DELETE FROM mapping_conflict WHERE sku_mapping_id = :mid")
                        .bindparams(mid=STATE["mid"]))
        await s.commit()

    # ② 强制全量
    async with AsyncSessionLocal() as s:
        try:
            r = await MappingValidator.detect_conflicts(s, detect_all=True)
            print(f"  detect_conflicts(detect_all=True) -> {r}")
            await s.commit()
        except Exception as exc:  # noqa: BLE001
            print(f"  detect_conflicts 抛异常: {type(exc).__name__}: {exc}")

    async with AsyncSessionLocal() as s:
        rows = (await s.execute(text(
            "SELECT conflict_type, level, is_resolved, description FROM mapping_conflict "
            "WHERE sku_mapping_id = :mid"
        ).bindparams(mid=STATE["mid"]))).mappings().all()
        print(f"  [旁证] mapping_conflict 落库 {len(rows)} 条: {[dict(r) for r in rows][:3]}")


async def mapping_status() -> str:
    """读回映射状态。"""
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.mapping import SkuMapping

    async with AsyncSessionLocal() as s:
        m = (await s.execute(select(SkuMapping).where(SkuMapping.id == STATE["mid"]))).scalar_one()
        return str(m.status)


async def run_match() -> tuple[bool, str]:
    """真实调用订单匹配。"""
    from app.core.database import AsyncSessionLocal
    from app.services.order_service import OrderService

    async with AsyncSessionLocal() as s:
        ok, reason = await OrderService.match_order(s, STATE["oid"], operator="qa")
        await s.commit()
        return ok, reason


async def order_status() -> dict[str, Any]:
    """读回订单状态。"""
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.order import Order

    async with AsyncSessionLocal() as s:
        o = (await s.execute(select(Order).where(Order.id == STATE["oid"]))).scalar_one()
        return {"fulfillment_status": o.fulfillment_status, "match_status": o.match_status,
                "exception_note": o.exception_note}


async def cleanup() -> None:
    """清理。"""
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.mapping import MappingConflict, SkuMapping
    from app.models.order import Order, OrderItem
    from app.models.source import SourceProduct, SourceSku

    async with AsyncSessionLocal() as s:
        for model, cond in (
            (MappingConflict, MappingConflict.sku_mapping_id == STATE.get("mid", -1)),
            (OrderItem, OrderItem.shop_sku_code == "QA-SPEC-SKU"),
            (Order, Order.platform_order_no == "QA-SPEC-ORDER"),
            (SkuMapping, SkuMapping.shop_sku_code == "QA-SPEC-SKU"),
            (SourceSku, SourceSku.sku_code_1688 == "QA-SPEC-SRC"),
            (SourceProduct, SourceProduct.product_1688_id == "QA-SPEC-ITEM"),
        ):
            for row in (await s.execute(select(model).where(cond))).scalars().all():
                await s.delete(row)
        await s.commit()


async def main_async() -> None:
    """异步主流程。"""
    await setup()

    banner("场景：商品已上架并持续出单 → 货源侧悄悄把 XL 改成 XXL")
    await drift_spec()
    await run_detect()

    st = await mapping_status()
    print(f"\n  映射 status = {st}")
    if st == "pending_confirm":
        print("  ✅ 映射已自动转 pending_confirm（符合 ARCHITECTURE.md:715）")
    else:
        print(f"  ❌ 映射 status 仍是 {st} —— ARCHITECTURE.md:715 要求「映射自动转 pending_confirm」未落地")

    ok, reason = await run_match()
    o = await order_status()
    print(f"\n  match_order -> matched={ok}  reason={reason!r}")
    print(f"  订单状态: {o}")

    banner("判定")
    if st == "valid" and ok is True:
        print("  ❌ P0 缺陷确认：规格已变的映射仍被判定 matched，订单照发不误。")
        print("     冲突确实被检测到了（日志里 spec_mismatch=1），但因为映射没转 pending_confirm，")
        print("     履约链路完全看不到这个冲突 —— 典型「不报错、只静默失效」。")
        print("     后果：买家下单 XL，实际按 XXL 货源发货。")
    elif ok is False:
        print("  ✅ 订单被挂起，未盲发。")
    else:
        print(f"  ⚠ 未预期组合: status={st} matched={ok}")

    await cleanup()


if __name__ == "__main__":
    asyncio.run(main_async())
