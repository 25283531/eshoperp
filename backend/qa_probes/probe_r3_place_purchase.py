"""★ 第三轮 · place_purchase 全链路（真实 HTTP）。

断言：
  P1  订单同步 → SKU 匹配 → POST /orders/{id}/place-purchase → 状态推进，跨请求可见
  P2  本地兜底（local_csv）的采购单状态是 manual_pending，**绝不**伪装成 placed
  P3  未匹配的订单下单必须 409（不能盲发）
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))

BASE = "http://127.0.0.1:8000"
API = f"{BASE}/api/v1"
DB = ROOT / "data" / "erp.db"

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
H = {"Content-Type": "application/json", "X-Operator": "qa", "X-Operator-Token": "admin-token"}
FAILS: list[str] = []


def req(method: str, path: str, body: object = None) -> tuple[int, dict]:
    """HTTP 请求。"""
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    r = urllib.request.Request(API + path, data=data, headers=H, method=method)
    try:
        with _opener.open(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return exc.code, {"message": raw[:200]}


def banner(t: str) -> None:
    """分节标题。"""
    print(f"\n{'=' * 92}\n{t}\n{'=' * 92}")


def seed_orders() -> tuple[int, int]:
    """造两张订单：一张能匹配上，一张匹配不上。返回 (ok_order_id, bad_order_id)。"""
    import asyncio

    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.enums import MatchStatus, OrderFulfillmentStatus
    from app.models.mapping import SkuMapping
    from app.models.order import Order, OrderItem

    async def _go() -> tuple[int, int]:
        async with AsyncSessionLocal() as s:
            for no in ("QA-PP-OK", "QA-PP-BAD"):
                for row in (await s.execute(
                        select(Order).where(Order.platform_order_no == no))).scalars().all():
                    await s.delete(row)
            await s.commit()

            m = (await s.execute(
                select(SkuMapping).where(SkuMapping.status == "valid",
                                         SkuMapping.is_deleted.is_(False))
                .order_by(SkuMapping.id))).scalars().first()
            assert m is not None, "库里没有 valid 映射，无法构造"

            ok = Order(platform=m.platform, shop_id=m.shop_id,
                       platform_order_no="QA-PP-OK",
                       fulfillment_status=OrderFulfillmentStatus.PENDING_MATCH.value,
                       adapter_name="local_csv", total_amount_cents=9900, is_mock=True)
            s.add(ok); await s.flush()
            s.add(OrderItem(order_id=int(ok.id), shop_item_id=m.shop_item_id,
                            shop_sku_code=m.shop_sku_code, quantity=2,
                            sale_price_cents=9900, match_status=MatchStatus.UNMATCHED.value))

            bad = Order(platform=m.platform, shop_id=m.shop_id,
                        platform_order_no="QA-PP-BAD",
                        fulfillment_status=OrderFulfillmentStatus.PENDING_MATCH.value,
                        adapter_name="local_csv", total_amount_cents=9900, is_mock=True)
            s.add(bad); await s.flush()
            s.add(OrderItem(order_id=int(bad.id), shop_item_id="no-such-item",
                            shop_sku_code="QA-NO-MAPPING-SKU", quantity=1,
                            sale_price_cents=9900, match_status=MatchStatus.UNMATCHED.value))
            await s.commit()
        return int(ok.id), int(bad.id)

    return asyncio.run(_go())


def purchase_status(order_id: int) -> str | None:
    """跨请求读回采购单状态（独立 HTTP 请求，证明跨请求可见）。"""
    st, pl = req("GET", f"/purchase-orders?page=1&page_size=50")
    for it in (pl.get("data") or {}).get("items", []):
        if int(it.get("order_id") or 0) == order_id:
            return it.get("purchase_status")
    return None


def main() -> None:
    """主流程。"""
    ok_id, bad_id = seed_orders()
    print(f"  已造订单: 可匹配={ok_id}  不可匹配={bad_id}")

    # ---------------- P1：同步 → 匹配 → 下单 ----------------
    banner("P1  订单同步 → SKU 匹配 → 下单，并跨请求可见")
    st, pl = req("POST", "/orders/sync", {"force": True})
    print(f"  POST /orders/sync           -> {st} {pl.get('message')}")

    st, pl = req("POST", f"/orders/{ok_id}/match", {})
    print(f"  POST /orders/{ok_id}/match   -> {st} {pl.get('message')}")

    st, pl = req("GET", f"/orders/{ok_id}")
    o = (pl.get("data") or {})
    print(f"  GET  /orders/{ok_id}         -> {st} status={o.get('fulfillment_status')} "
          f"match={o.get('match_status')}")
    if o.get("fulfillment_status") != "matched":
        FAILS.append(f"P1 匹配后订单状态应为 matched，实际 {o.get('fulfillment_status')}")

    st, pl = req("POST", f"/orders/{ok_id}/place-purchase", {})
    d = pl.get("data") or {}
    print(f"  POST place-purchase         -> {st} {pl.get('message')}")
    print(f"       data = {json.dumps(d, ensure_ascii=False)[:300]}")
    if st not in (200, 201, 202):
        FAILS.append(f"P1 place-purchase 期望 200/201/202，实际 {st} {pl.get('message')}")

    po_id = d.get("id") or d.get("purchase_order_id")
    # ★ 跨请求可见：换个请求重新读
    st2, pl2 = req("GET", f"/purchase-orders/{po_id}" if po_id else "/purchase-orders?page=1&page_size=5")
    pdata = pl2.get("data") or {}
    print(f"  GET  /purchase-orders/{po_id} -> {st2} purchase_status={pdata.get('purchase_status')}")
    if not pdata.get("purchase_status") and not po_id:
        FAILS.append("P1 采购单跨请求读不到")

    # ---------------- P2：本地兜底不得伪装成已下单 ----------------
    banner("P2  本地兜底：采购单必须是 manual_pending，不能是 placed")
    ps = pdata.get("purchase_status") or purchase_status(ok_id)
    print(f"  purchase_status = {ps!r}")
    print(f"  adapter 备注    = {str(pdata.get('remark') or pdata.get('note') or '')[:120]}")
    if ps == "manual_pending":
        print("  ✅ 本地兜底如实返回 manual_pending（没有假装已下单）")
    elif ps in {"placed", "success"}:
        FAILS.append(f"P2 ★ 本地兜底伪装成已下单：purchase_status={ps}")
        print(f"  ❌ ★ 本地兜底返回 {ps} —— 本地从未真正下过单，这是静默失效")
    else:
        FAILS.append(f"P2 采购单状态异常：{ps}")
        print(f"  ❌ 状态异常 {ps}")

    # ---------------- P3：未匹配的订单不得下单 ----------------
    banner("P3  未匹配的订单下单必须 409（不能盲发）")
    st, pl = req("POST", f"/orders/{bad_id}/match", {})
    print(f"  POST /orders/{bad_id}/match  -> {st} {pl.get('message')}")
    st3, pl3 = req("GET", f"/orders/{bad_id}")
    print(f"  GET  /orders/{bad_id}        -> {st3} status={(pl3.get('data') or {}).get('fulfillment_status')}")

    st4, pl4 = req("POST", f"/orders/{bad_id}/place-purchase", {})
    print(f"  POST place-purchase          -> {st4} code={pl4.get('code')} {pl4.get('message')}")
    if st4 == 409:
        print("  ✅ 未匹配订单下单被 409 拒绝（未盲发）")
    else:
        FAILS.append(f"P3 未匹配订单下单期望 409，实际 {st4}")
        print(f"  ❌ 期望 409，实际 {st4}")

    # ---------------- 清理 + 结论 ----------------
    c = sqlite3.connect(str(DB), timeout=10)
    n = c.execute("select count(*) from erp_order where platform_order_no in ('QA-PP-OK','QA-PP-BAD')").fetchone()[0]
    print(f"\n  （清理前库里 QA-PP 订单数 = {n}）")
    c.close()

    banner("place_purchase 结论")
    if FAILS:
        print(f"  ❌ {len(FAILS)} 项未通过：")
        for f in FAILS:
            print(f"     - {f}")
    else:
        print("  ✅ place_purchase 链路全部通过")


if __name__ == "__main__":
    main()
