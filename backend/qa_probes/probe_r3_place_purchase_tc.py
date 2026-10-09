"""★ 第三轮 · place_purchase 全链路（载入**当前源码**的进程内验证）。

为什么不用 8000 那个实例：
    它的 OpenAPI 里**没有** `/orders/{order_id}/place-purchase`，
    而源码 `app/api/v1/orders.py:220` 有。该文件 mtime=21:51，
    8000 实例启动更早 —— 属于**进程加载了旧代码**，不是路由没写。
    因此这里用 TestClient 载入当前源码来验，避免被陈旧进程误导。

断言：
  P1  匹配 → 下单 → 采购单跨请求可见
  P2  本地兜底状态是 manual_pending，**绝不**是 placed
  P3  未匹配订单下单 → 409（不能盲发）
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

import asyncio  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402

FAILS: list[str] = []
H = {"X-Operator": "qa", "X-Operator-Token": "admin-token"}


def banner(t: str) -> None:
    """分节标题。"""
    print(f"\n{'=' * 92}\n{t}\n{'=' * 92}")


def main() -> None:
    """主流程。"""
    print("  当前 Settings 数据库 =", get_settings().database_url)

    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    # 确认路由确实注册了（用 OpenAPI，避免 routes 里有非 Route 对象）
    spec = app.openapi()
    paths = list(spec.get("paths", {}))
    has_route = any("place-purchase" in p for p in paths)
    print(f"  当前源码 OpenAPI 端点数 = {len(paths)}")
    print(f"  含 place-purchase: {has_route}  ->  {[p for p in paths if 'place-purchase' in p]}")
    if not has_route:
        FAILS.append("当前源码也没有注册 place-purchase 路由")

    # ---------- 造数据 ----------
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.enums import MatchStatus, OrderFulfillmentStatus
    from app.models.mapping import SkuMapping
    from app.models.order import Order, OrderItem

    async def seed() -> tuple[int, int, int]:
        async with AsyncSessionLocal() as s:
            for no in ("QA-PPT-OK", "QA-PPT-BAD"):
                for row in (await s.execute(
                        select(Order).where(Order.platform_order_no == no))).scalars().all():
                    await s.delete(row)
            await s.commit()
            m = (await s.execute(
                select(SkuMapping).where(SkuMapping.status == "valid",
                                         SkuMapping.is_deleted.is_(False))
                .order_by(SkuMapping.id))).scalars().first()
            ok = Order(platform=m.platform, shop_id=m.shop_id, platform_order_no="QA-PPT-OK",
                       fulfillment_status=OrderFulfillmentStatus.PENDING_MATCH.value,
                       adapter_name="local_csv", total_amount_cents=9900, is_mock=True)
            s.add(ok); await s.flush()
            s.add(OrderItem(order_id=int(ok.id), shop_item_id=m.shop_item_id,
                            shop_sku_code=m.shop_sku_code, quantity=2,
                            sale_price_cents=9900, match_status=MatchStatus.UNMATCHED.value))
            bad = Order(platform=m.platform, shop_id=m.shop_id, platform_order_no="QA-PPT-BAD",
                        fulfillment_status=OrderFulfillmentStatus.PENDING_MATCH.value,
                        adapter_name="local_csv", total_amount_cents=9900, is_mock=True)
            s.add(bad); await s.flush()
            s.add(OrderItem(order_id=int(bad.id), shop_item_id="no-such-item",
                            shop_sku_code="QA-NO-MAPPING-SKU", quantity=1,
                            sale_price_cents=9900, match_status=MatchStatus.UNMATCHED.value))
            await s.commit()
            return int(ok.id), int(bad.id), int(m.id)

    ok_id, bad_id, mapping_id = asyncio.run(seed())
    print(f"  已造订单: 可匹配={ok_id}  不可匹配={bad_id}  可用映射 id={mapping_id}")

    # ---------------- P1 ----------------
    banner("P1  匹配 → 下单 → 跨请求可见")
    # ★ `POST /orders/{id}/match` 是**手工**匹配（manual_match），必须显式给 sku_mapping_id
    r = client.post(f"/api/v1/orders/{ok_id}/match", json={"sku_mapping_id": mapping_id}, headers=H)
    print(f"  POST /orders/{ok_id}/match -> {r.status_code} {r.json().get('message')}")
    r = client.get(f"/api/v1/orders/{ok_id}", headers=H)
    o = r.json().get("data") or {}
    print(f"  GET  /orders/{ok_id}       -> {r.status_code} status={o.get('fulfillment_status')}")
    if o.get("fulfillment_status") != "matched":
        FAILS.append(f"P1 匹配后应为 matched，实际 {o.get('fulfillment_status')}")

    r = client.post(f"/api/v1/orders/{ok_id}/place-purchase", json={}, headers=H)
    body = r.json()
    print(f"  POST place-purchase        -> {r.status_code} {body.get('message')}")
    print(f"       data = {str(body.get('data'))[:260]}")
    if r.status_code not in (200, 201, 202):
        FAILS.append(f"P1 place-purchase 期望 200/201/202，实际 {r.status_code} {body.get('message')}")

    po_id = (body.get("data") or {}).get("id") or (body.get("data") or {}).get("purchase_order_id")
    r2 = client.get(f"/api/v1/purchase-orders/{po_id}" if po_id
                    else "/api/v1/purchase-orders?page=1&page_size=5", headers=H)
    pd = r2.json().get("data") or {}
    print(f"  GET  /purchase-orders/{po_id} -> {r2.status_code} purchase_status={pd.get('purchase_status')}")
    if po_id and not pd.get("purchase_status"):
        FAILS.append("P1 采购单跨请求读不到")

    # ---------------- P2 ----------------
    banner("P2  本地兜底必须是 manual_pending，不能伪装成 placed")
    ps = pd.get("purchase_status")
    print(f"  purchase_status = {ps!r}")
    print(f"  remark/note     = {str(pd.get('remark') or pd.get('note') or '')[:140]}")
    if ps == "manual_pending":
        print("  ✅ 如实返回 manual_pending（本地从未真正下单）")
    elif ps in {"placed", "success"}:
        FAILS.append(f"P2 ★ 本地兜底伪装成已下单：{ps}")
        print(f"  ❌ ★ 返回 {ps} —— 静默失效")
    else:
        FAILS.append(f"P2 采购单状态异常：{ps}")
        print(f"  ❌ 状态异常 {ps}")

    # ---------------- P3 ----------------
    banner("P3  未匹配订单下单必须 409（不能盲发）")
    r = client.post(f"/api/v1/orders/{bad_id}/match", json={}, headers=H)
    print(f"  POST /orders/{bad_id}/match -> {r.status_code} {r.json().get('message')}")
    r = client.get(f"/api/v1/orders/{bad_id}", headers=H)
    print(f"  GET  /orders/{bad_id}       -> {r.status_code} "
          f"status={(r.json().get('data') or {}).get('fulfillment_status')}")
    r = client.post(f"/api/v1/orders/{bad_id}/place-purchase", json={}, headers=H)
    print(f"  POST place-purchase         -> {r.status_code} code={r.json().get('code')} "
          f"{r.json().get('message')}")
    if r.status_code == 409:
        print("  ✅ 未匹配订单下单被 409 拒绝（未盲发）")
    else:
        FAILS.append(f"P3 未匹配订单下单期望 409，实际 {r.status_code}")
        print(f"  ❌ 期望 409，实际 {r.status_code}")

    banner("place_purchase 结论（当前源码）")
    if FAILS:
        print(f"  ❌ {len(FAILS)} 项未通过：")
        for f in FAILS:
            print(f"     - {f}")
    else:
        print("  ✅ place_purchase 链路全部通过")


if __name__ == "__main__":
    main()
