"""★ 验证「库存自动下架的数据源门槛」（v1.14 §5.8 / INV-P0-03）—— **真实 HTTP 对照实验**。

================================================================================
这次要钉死的缺陷
================================================================================
`InventoryService._apply_actions()` 原本对数据源一视同仁：
库存快照一归零就调 `ListingService.offline()`。**手工录入 / CSV 导入**的商品
没有自动更新库存的上游，录入时库存没填（= 0）会**立刻**被判定缺货并下架，
运营还会往「供应商缺货」方向归因 —— 极难定位到「我刚导了个 CSV」。

================================================================================
两组对照（同一次 `inventory_sync` 里跑，只有数据源不同）
================================================================================
    A 组（手工 / CSV 导入，库存没填 = 0）→ 期望：**仍在售**（只告警 + 一键下架入口）
    B 组（1688 采集，库存真的归零）      → 期望：**仍被自动下架**（保原行为，别误伤）

判定键：`inventory_snapshot.source`（不是 `source_platform`）。

================================================================================
纪律（沿用 verify_manual_source_chain.py）
================================================================================
1. 全程真实 HTTP，打在已经跑起来的 uvicorn 上；不用 TestClient。
2. A 组的货源商品用**真实的 CSV 导入端点**录入（就是出事的那条路径），
   不直接写库伪造"手工商品"。
3. **唯一**绕过 HTTP 的地方：`listing_product` / `sku_mapping` 的在售前置数据
   —— 全仓没有「创建平台商品」的写端点（只能经上架流程产生），
   因此用 sqlite 直接铺这个前置；**每一步都会打印**，绝不悄悄改库。
4. 跑完默认清理现场（可 `--no-cleanup` 保留用于复看）。

用法：
    python scripts/verify_inventory_source_gate.py
    python scripts/verify_inventory_source_gate.py --base-url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
REAL_DB = PROJECT_ROOT / "data" / "erp.db"  # ★ 真实开发库（不是 backend/data 的空壳）

API = "/api/v1"
ADMIN_HEADERS = {"X-Operator": "verifier", "X-Operator-Token": "admin-token"}
TERMINAL_TASK_STATES = {"success", "failed", "cancelled"}

RESULTS: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, detail: str) -> None:
    """记录并打印一步结果。"""
    RESULTS.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}  —— {detail}")


def note(text: str) -> None:
    """打印补充说明（不影响成败）。"""
    print(f"   · {text}")


def snippet(resp: httpx.Response, limit: int = 260) -> str:
    """响应片段（状态码 + 截断 body），便于直接贴进报告。"""
    return f"HTTP {resp.status_code} {resp.text[:limit]}"


def json_data(resp: httpx.Response) -> dict[str, Any]:
    """安全取响应体里的 `data`；非 JSON 时返回空字典（**不假装成功，也不把脚本打崩**）。"""
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001  服务没起来时响应是 502 HTML/纯文本
        return {}
    return dict(payload.get("data") or {})


def wait_task(client: httpx.Client, task_id: int, *, label: str, timeout_s: float = 60.0) -> dict[str, Any]:
    """轮询异步任务到终态；超时返回当前状态（**不假装成功**）。"""
    deadline = time.time() + timeout_s
    body: dict[str, Any] = {}
    while time.time() < deadline:
        resp = client.get(f"{API}/tasks/{task_id}")
        body = json_data(resp)
        if str(body.get("status")) in TERMINAL_TASK_STATES:
            note(f"{label} 任务终态：{body.get('status')}（task_record_id={task_id}）")
            return body
        time.sleep(0.5)
    note(f"{label} 任务超时未到终态，当前状态={body.get('status')}")
    return body


def db_execute(sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """在**真实开发库**上执行 DML/查询（短事务 + busy_timeout，避开 uvicorn 的锁）。"""
    con = sqlite3.connect(REAL_DB, timeout=15.0)
    try:
        cur = con.cursor()
        cur.execute("PRAGMA busy_timeout=15000")
        cur.execute(sql, params)
        rows = cur.fetchall()
        con.commit()
        return rows
    finally:
        con.close()


def db_query(sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """只读查询真实开发库。"""
    con = sqlite3.connect(REAL_DB, timeout=15.0)
    try:
        cur = con.cursor()
        cur.execute("PRAGMA busy_timeout=15000")
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        con.close()


def build_group_a_csv(product_code: str) -> bytes:
    """A 组 CSV：**按模板填，但库存那一列留空** —— 正是事故里的真实用法。

    关键点：这里不是"填 0"，而是**没填**。使用者在 Excel 里漏填一列极其常见，
    也更难自查，比显式填 0 更能代表真实场景。
    """
    header = "商品编码,商品标题,类目,商品成本价,SKU编码,规格名,规格值,SKU成本价,售价,库存,备注"
    row = f"{product_code},库存门槛验证A·{product_code},女装/上装/T恤,18.00,{product_code}-RED-XL,颜色;尺码,红色;XL,18.00,59.90,,库存列留空（模拟运营漏填）"
    return f"{header}\n{row}\n".encode("utf-8-sig")


def scaffold_listing_and_mapping(
    *, source_product_id: int, source_sku_id: int, suffix: str
) -> tuple[int, int]:
    """铺「在售平台商品 + SKU 映射」前置数据（sqlite，全仓无对应写端点）。

    Returns:
        `(listing_product_id, sku_mapping_id)`。
    """
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    listing_rows = db_query(
        "SELECT id FROM listing_product WHERE shop_item_id = ?", (f"item-invgate-{suffix}",)
    )
    if listing_rows:
        listing_id = int(listing_rows[0][0])
    else:
        db_execute(
            "INSERT INTO listing_product "
            "(created_at, updated_at, is_deleted, platform, shop_id, shop_item_id, "
            " source_product_id, title, status, is_mock, listing_mode) "
            "VALUES (?, ?, 0, 'taobao', ?, ?, ?, ?, 'on_sale', 0, 'manual')",
            (now, now, f"shop-invgate-{suffix}", f"item-invgate-{suffix}",
             int(source_product_id), f"库存门槛验证在售商品 {suffix}"),
        )
        listing_id = int(db_query("SELECT MAX(id) FROM listing_product")[0][0])

    mapping_rows = db_query("SELECT id FROM sku_mapping WHERE shop_sku_code = ?", (f"SKU-INVGATE-{suffix}",))
    if mapping_rows:
        mapping_id = int(mapping_rows[0][0])
    else:
        db_execute(
            "INSERT INTO sku_mapping "
            "(created_at, updated_at, is_deleted, platform, shop_id, shop_item_id, shop_sku_code, "
            " listing_product_id, source_product_id, source_sku_id, source_sku_code_1688, "
            " spec_signature, purchase_cost_cents, cost_currency, status, has_conflict, "
            " is_mock, source, version, cost_source) "
            "VALUES (?, ?, 0, 'taobao', ?, ?, ?, ?, ?, ?, ?, ?, 1800, 'CNY', 'valid', 0, 0, 'manual', 1, 'auto')",
            (now, now, f"shop-invgate-{suffix}", f"item-invgate-{suffix}", f"SKU-INVGATE-{suffix}",
             listing_id, int(source_product_id), int(source_sku_id), f"SKU-INVGATE-{suffix}",
             f"sig-invgate-{suffix}"),
        )
        mapping_id = int(db_query("SELECT MAX(id) FROM sku_mapping")[0][0])
    return listing_id, mapping_id


def listing_status(client: httpx.Client, listing_id: int) -> tuple[str, str | None]:
    """读平台商品状态（HTTP）。"""
    resp = client.get(f"{API}/listing-products/{int(listing_id)}")
    data = json_data(resp)
    return str(data.get("status") or ""), data.get("offline_reason")


def cleanup(rows_to_remove: dict[str, list[int]], stock_restore: list[tuple[int, int]]) -> None:
    """还原真实库：删掉本脚本铺的行，恢复被改过的库存。"""
    print("\n==================== 现场清理 ====================")
    for table, ids in rows_to_remove.items():
        for row_id in ids:
            db_execute(f"DELETE FROM {table} WHERE id = ?", (int(row_id),))
            note(f"已删除 {table}.id={row_id}")
    # 清理可能挂在禁用的 keys 上的 delete 痕迹不需要：整行删除即可
    for source_sku_id, stock in stock_restore:
        db_execute("UPDATE source_sku SET stock_qty = ? WHERE id = ?", (int(stock), int(source_sku_id)))
        note(f"已恢复 source_sku.id={source_sku_id} 库存 → {stock}")


def main() -> int:
    """跑完整对照实验；任一硬性断言失败则返回 1。"""
    parser = argparse.ArgumentParser(description="库存自动下架数据源门槛的真实 HTTP 对照实验")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--no-cleanup", action="store_true", help="保留现场数据用于复看")
    args = parser.parse_args()

    base = str(args.base_url).rstrip("/")
    run_id = uuid.uuid4().hex[:6].upper()
    suffix_a = f"A{run_id}"
    suffix_b = f"B{run_id}"
    to_delete: dict[str, list[int]] = {"sku_mapping": [], "listing_product": [], "source_sku": [], "source_product": []}
    stock_restore: list[tuple[int, int]] = []

    print("=" * 96)
    print(f"库存自动下架 · 数据源门槛验证（run={run_id}） base={base}  db={REAL_DB}")
    print("=" * 96)

    with httpx.Client(base_url=base, timeout=30.0, headers=ADMIN_HEADERS) as client:
        # ---------------------------------------------------------------- 0. 服务存活
        resp = client.get(f"{API}/health")
        step("0. 服务存活 + 当前代码", resp.status_code == 200, snippet(resp, 120))

        # ---------------------------------------------------------------- A 组：CSV 导入
        product_code = f"INVGATE-{suffix_a}"
        csv_bytes = build_group_a_csv(product_code)
        resp = client.post(
            f"{API}/source-products/import-csv",
            files={"file": (f"inv_gate_{run_id}.csv", csv_bytes, "text/csv")},
        )
        body = json_data(resp)
        step(
            "A1. CSV 导入（库存列留空）",
            resp.status_code == 200 and int(body.get("created") or 0) >= 1,
            snippet(resp, 260),
        )

        rows = db_query(
            "SELECT p.id, p.product_1688_id, s.id, s.sku_code_1688, s.stock_qty "
            "FROM source_product p JOIN source_sku s ON s.source_product_id = p.id "
            "WHERE p.product_1688_id LIKE ? ORDER BY s.id LIMIT 1",
            (f"%{product_code}%",),
        )
        if not rows:
            step("A2. 取 A 组货源 SKU", False, "CSV 导入后没查到商品，后续无法继续")
            return 1
        a_product_id, a_1688_id, a_sku_id, a_sku_code, a_stock = (
            int(rows[0][0]), str(rows[0][1]), int(rows[0][2]), str(rows[0][3]), int(rows[0][4] or 0)
        )
        to_delete["source_product"].append(a_product_id)
        to_delete["source_sku"].append(a_sku_id)
        note(f"A 组：source_product.id={a_product_id} ({a_1688_id}) / source_sku.id={a_sku_id} "
             f"({a_sku_code}) 入库库存={a_stock}")
        step("A2. A 组库存确实为 0（漏填）", a_stock == 0, f"source_sku.stock_qty={a_stock}")

        a_listing_id, a_mapping_id = scaffold_listing_and_mapping(
            source_product_id=a_product_id, source_sku_id=a_sku_id, suffix=suffix_a
        )
        to_delete["listing_product"].append(a_listing_id)
        to_delete["sku_mapping"].append(a_mapping_id)
        note(f"A 组前置：listing_product.id={a_listing_id} / sku_mapping.id={a_mapping_id}（sqlite 铺，无写端点）")

        # ---------------------------------------------------------------- B 组：1688 采集商品
        rows = db_query(
            "SELECT p.id, p.product_1688_id, s.id, s.sku_code_1688, s.stock_qty "
            "FROM source_product p JOIN source_sku s ON s.source_product_id = p.id "
            "WHERE p.is_deleted = 0 AND p.product_1688_id NOT LIKE 'MANUAL-%' "
            "  AND s.is_deleted = 0 ORDER BY s.id LIMIT 1"
        )
        if not rows:
            step("B1. 取真实的 1688 采集货源 SKU", False, "库里没有 1688 采集的货源商品")
            return 1
        b_product_id, b_1688_id, b_sku_id, b_sku_code, b_stock = (
            int(rows[0][0]), str(rows[0][1]), int(rows[0][2]), str(rows[0][3]), int(rows[0][4] or 0)
        )
        note(f"B 组：source_product.id={b_product_id} ({b_1688_id}) / source_sku.id={b_sku_id} ({b_sku_code})")
        stock_restore.append((b_sku_id, b_stock))

        # B 组模拟「供应商真实缺货」：库存真的归零
        db_execute("UPDATE source_sku SET stock_qty = 0 WHERE id = ?", (b_sku_id,))
        note(f"B 组：source_sku.stock_qty 由 {b_stock} 改为 0（模拟真缺货，脚本结束会还原）")

        b_listing_id, b_mapping_id = scaffold_listing_and_mapping(
            source_product_id=b_product_id, source_sku_id=b_sku_id, suffix=suffix_b
        )
        to_delete["listing_product"].append(b_listing_id)
        to_delete["sku_mapping"].append(b_mapping_id)
        note(f"B 组前置：listing_product.id={b_listing_id} / sku_mapping.id={b_mapping_id}（sqlite 铺）")

        a_before, _ = listing_status(client, a_listing_id)
        b_before, _ = listing_status(client, b_listing_id)
        step(
            "3. 同步前两组都在售",
            a_before == "on_sale" and b_before == "on_sale",
            f"A={a_before} / B={b_before}",
        )

        # ---------------------------------------------------------------- 4. 跑真实 inventory_sync
        resp = client.post(f"{API}/inventory/sync", json={"source_sku_ids": [a_sku_id, b_sku_id]})
        data = json_data(resp)
        step("4. POST /inventory/sync 受理", resp.status_code == 202, snippet(resp, 200))
        task_id = int(data.get("task_record_id") or 0)
        task_body: dict[str, Any] = {}
        if task_id:
            task_body = wait_task(client, task_id, label="inventory_sync")
            summary = {k: task_body.get(k) for k in ("status", "result", "result_json", "error_message")}
            note(f"任务结果：{summary}")

        # ---------------------------------------------------------------- 5. 判定结果
        a_after, a_reason = listing_status(client, a_listing_id)
        b_after, b_reason = listing_status(client, b_listing_id)
        step(
            "★ A 组（CSV 导入 / 库存 0）仍在售",
            a_after == "on_sale",
            f"status={a_after}（修复前这里会是 off_shelf）",
        )
        step(
            "★ B 组（1688 采集 / 库存 0）已被自动下架",
            b_after == "off_shelf",
            f"status={b_after} reason={b_reason}",
        )

        # ---------------------------------------------------------------- 6. 快照来源标签
        for label, sku_id, expect in (("A", a_sku_id, "manual_import"), ("B", b_sku_id, "erp_poll")):
            resp = client.get(f"{API}/inventory/snapshots", params={"source_sku_id": sku_id, "page_size": 5})
            items = list((resp.json().get("data") or {}).get("items") or [])
            sources = [str(it.get("source")) for it in items]
            step(f"6{label}. {label} 组快照来源 = {expect}", bool(items) and sources[0] == expect, f"sources={sources}")

        # ---------------------------------------------------------------- 7. 告警可见且标了门槛
        resp = client.get(f"{API}/inventory/alerts", params={"type": "out_of_stock", "page_size": 50})
        alert_items = list((resp.json().get("data") or {}).get("items") or [])
        mine = [a for a in alert_items if int(a.get("source_sku_id") or 0) == a_sku_id]
        step(
            "7. A 组产生告警（不下架 ≠ 不告警）",
            bool(mine),
            f"命中 {len(mine)} 条；data_source={[a.get('data_source') for a in mine]} "
            f"auto_offline_allowed={[a.get('auto_offline_allowed') for a in mine]}",
        )
        if mine:
            step(
                "7b. 告警标出数据源为手工 + 不自动下架",
                all(a.get("data_source") == "manual_import" for a in mine)
                and all(a.get("auto_offline_allowed") is False for a in mine),
                "UI 据此展示「手工维护」并给出一键下架入口",
            )

        # ---------------------------------------------------------------- 8. 审计留痕
        resp = client.get(f"{API}/inventory/auto-offline-records", params={"page_size": 5})
        records = list((resp.json().get("data") or {}).get("items") or [])
        latest = str((records[0].get("message") or records[0].get("reason") or "")) if records else ""
        step("8. 自动下架记录可审计", bool(records), f"最新一条：{latest[:200]}")

    if not args.no_cleanup:
        cleanup(to_delete, stock_restore)

    failed = [name for name, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 96)
    print(f"结论：{len(RESULTS) - len(failed)} 项通过 / {len(failed)} 项失败" + (f" —— 失败项：{failed}" if failed else ""))
    print("=" * 96)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
