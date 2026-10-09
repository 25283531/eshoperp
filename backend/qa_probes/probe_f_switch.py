"""★ QA 探针 F：履约适配器热切换 + 在途订单按原渠道跑完（ADR-6）。

F-1  GET  /adapters/fulfillment            —— 三个适配器都在、默认 local_csv
F-2  造数据：local_csv 名下 3 单在途 + 1 单终态（终态不该计入在途）
F-3  POST /adapters/fulfillment/switch     —— 切到 miaoshou，必须返回 inflight_order_count
F-4  切换后：已落库订单的 adapter_name 必须**仍是** local_csv（冻结）
F-5  再切回 local_csv，确认可重复切换且计数语义自洽
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))

BASE = "http://127.0.0.1:8141"
API = f"{BASE}/api/v1"

for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
FAILS: list[str] = []


def banner(title: str) -> None:
    """分节标题。"""
    print(f"\n{'=' * 96}\n{title}\n{'=' * 96}")


def req(method: str, path: str, body: Any = None) -> tuple[int, dict]:
    """同步 HTTP 请求（管理员身份）。"""
    url = API + path
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "X-Operator": "qa-admin",
               "X-Operator-Token": "admin-token"}
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener.open(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return exc.code, {"message": raw[:300]}


def show(status: int, payload: dict, label: str, *, expect: set[int] | None = None) -> dict:
    """打印响应并做期望检查。"""
    ok = expect is None or status in expect
    if not ok:
        FAILS.append(f"{label}: 期望 {sorted(expect)} 实际 {status} -> {payload.get('message')}")
    print(f"  {'[OK]' if ok else '[FAIL]'} {label}  ->  HTTP {status}  code={payload.get('code')}")
    pretty = json.dumps(payload.get("data"), ensure_ascii=False)
    print(f"        data={pretty[:600]}{' ...' if len(pretty) > 600 else ''}")
    if payload.get("message"):
        print(f"        msg ={payload['message'][:300]}")
    return payload.get("data") or {}


# ----------------------------------------------------------------------
#  F-2：直接落库造订单（不依赖已损坏的 order_sync，见缺陷 P0-3）
# ----------------------------------------------------------------------
TERMINAL = ("completed", "refunded", "cancelled")


async def baseline_inflight() -> int:
    """统计在造数据之前，local_csv 名下已有的在途订单数（含种子数据）。"""
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.order import Order

    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(Order).where(
                Order.adapter_name == "local_csv",
                Order.fulfillment_status.notin_(sorted(TERMINAL)),
            )
        )).scalars().all()
        return len(rows)


async def seed_orders() -> list[str]:
    """在 local_csv 名下造 3 单在途 + 1 单终态，返回订单号。"""
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.enums import OrderFulfillmentStatus
    from app.models.order import Order

    nos: list[str] = []
    async with AsyncSessionLocal() as s:
        # 清理本探针历史数据
        old = (await s.execute(
            select(Order).where(Order.platform_order_no.like("QA-F-%"))
        )).scalars().all()
        for o in old:
            await s.delete(o)
        await s.commit()

        specs = [
            ("QA-F-1", OrderFulfillmentStatus.PENDING_MATCH.value),
            ("QA-F-2", OrderFulfillmentStatus.MATCHED.value),
            ("QA-F-3", OrderFulfillmentStatus.PURCHASED.value),
            ("QA-F-4", OrderFulfillmentStatus.COMPLETED.value),  # 终态，不该计入在途
        ]
        for no, st in specs:
            s.add(Order(
                platform="taobao",
                shop_id="qa-shop-f",
                platform_order_no=no,
                fulfillment_status=st,
                adapter_name="local_csv",
                total_amount_cents=9900,
                is_mock=True,
            ))
            nos.append(no)
        await s.commit()
    return nos


async def read_orders() -> list[dict[str, Any]]:
    """读回订单的 adapter_name。"""
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.order import Order

    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(Order).where(Order.platform_order_no.like("QA-F-%"))
        )).scalars().all()
        return [
            {"no": o.platform_order_no, "adapter": o.adapter_name, "status": o.fulfillment_status}
            for o in sorted(rows, key=lambda x: x.platform_order_no)
        ]


async def cleanup() -> None:
    """清理探针数据。"""
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.order import Order

    async with AsyncSessionLocal() as s:
        old = (await s.execute(
            select(Order).where(Order.platform_order_no.like("QA-F-%"))
        )).scalars().all()
        for o in old:
            await s.delete(o)
        await s.commit()


def main() -> None:
    """主流程。"""
    # ---------- F-1 ----------
    banner("F-1  履约适配器清单与当前生效项")
    st, pl = req("GET", "/adapters/fulfillment")
    data = show(st, pl, "GET /adapters/fulfillment", expect={200})
    items = data.get("items") if isinstance(data, dict) else data
    if isinstance(items, list):
        names = [i.get("adapter_name") or i.get("name") for i in items]
        print(f"        适配器: {names}")
        want = {"local_csv", "miaoshou", "yitao"}
        missing = want - set(names)
        if missing:
            FAILS.append(f"F-1 缺少适配器 {sorted(missing)}")
            print(f"  [FAIL] 期望三个适配器齐全，缺少 {sorted(missing)}")
        else:
            print("  [OK] 三个适配器齐全（local_csv / miaoshou / yitao）")
        active = [i for i in items if i.get("is_active")]
        print(f"        当前生效: {[i.get('adapter_name') or i.get('name') for i in active] or '(字段缺失)'}")
        if data.get("active_adapter"):
            print(f"        active_adapter = {data['active_adapter']}")
        if not active and not data.get("active_adapter"):
            FAILS.append("F-1 响应里找不到当前生效适配器")
            print("  [FAIL] 响应里找不到当前生效适配器字段")

    # ---------- F-2 ----------
    banner("F-2  造数据：local_csv 名下 3 单在途 + 1 单终态")
    base = asyncio.run(baseline_inflight())
    print(f"  造数据前 local_csv 已有在途订单（种子数据）: {base}")
    nos = asyncio.run(seed_orders())
    print(f"  已落库订单: {nos}")
    for r in asyncio.run(read_orders()):
        print(f"    {r['no']}  adapter={r['adapter']}  status={r['status']}")

    # ---------- F-3 ----------
    banner("F-3  热切换到 miaoshou —— 必须返回 inflight_order_count")
    st, pl = req("POST", "/adapters/fulfillment/switch",
                 {"adapter_name": "miaoshou", "reason": "QA 热切换验证", "drain_inflight": True})
    d3 = show(st, pl, "POST /adapters/fulfillment/switch -> miaoshou", expect={200})
    inflight = d3.get("inflight_order_count")
    expect_inflight = base + 3  # 既有在途 + 本探针 3 单在途；终态 COMPLETED 必须被排除
    print(f"  inflight_order_count = {inflight!r}   (期望 {expect_inflight} = 既有 {base} + 本探针 3 单在途；"
          f"终态 COMPLETED 必须不计)")
    if inflight != expect_inflight:
        FAILS.append(f"F-3 inflight_order_count 期望 {expect_inflight} 实际 {inflight}")
        print("  [FAIL] 在途订单计数不对")
    else:
        print("  [OK] 在途订单计数正确（终态被正确排除）")

    # ---------- F-4 ----------
    banner("F-4  ★ 切换后已落库订单的 adapter_name 必须仍是 local_csv（冻结）")
    after = asyncio.run(read_orders())
    bad = [r for r in after if r["adapter"] != "local_csv"]
    for r in after:
        mark = "OK  " if r["adapter"] == "local_csv" else "FAIL"
        print(f"    [{mark}] {r['no']}  adapter={r['adapter']}  status={r['status']}")
    if bad:
        FAILS.append(f"F-4 有 {len(bad)} 单订单的 adapter_name 被改写")
        print("  [FAIL] 在途订单渠道被改写 —— ADR-6 破裂")
    else:
        print("  [OK] 4 单订单（含 3 单在途）adapter_name 全部保持 local_csv")

    # 新单应走新渠道 —— 直接查当前生效值
    st, pl = req("GET", "/adapters/fulfillment")
    d = pl.get("data") or {}
    print(f"  切换后 active_adapter = {d.get('active_adapter')}")

    # ---------- F-5 ----------
    banner("F-5  切回 local_csv（可重复切换）")
    st, pl = req("POST", "/adapters/fulfillment/switch",
                 {"adapter_name": "local_csv", "reason": "QA 回滚", "drain_inflight": True})
    d5 = show(st, pl, "POST /adapters/fulfillment/switch -> local_csv", expect={200})
    print(f"  previous_adapter = {d5.get('previous_adapter')!r} (期望 miaoshou)")
    if d5.get("previous_adapter") != "miaoshou":
        FAILS.append(f"F-5 previous_adapter 期望 miaoshou 实际 {d5.get('previous_adapter')}")

    # 切到不存在的适配器必须被拒
    st, pl = req("POST", "/adapters/fulfillment/switch", {"adapter_name": "not_exist"})
    show(st, pl, "POST switch -> not_exist（期望 400/403/5003）", expect={400, 403, 404, 422})

    banner("F  结论")
    if FAILS:
        print(f"  ❌ {len(FAILS)} 项未通过：")
        for f in FAILS:
            print(f"     - {f}")
    else:
        print("  ✅ F 全部通过")
    asyncio.run(cleanup())


if __name__ == "__main__":
    main()
