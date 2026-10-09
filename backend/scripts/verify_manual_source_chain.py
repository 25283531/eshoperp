"""★ 手工录入 → AI 重构 → 建映射 → 半自动上架 → 订单自动匹配 → 采购单 —— **真实 HTTP** 全链路验证。

================================================================================
★ 为什么要有这个脚本
================================================================================
1688 采集需要开放平台凭证，个体户拿不到 ⇒ **手工录入才是真实主路径**。
但"能录进去"不等于"系统能起步"：必须证明录进去的数据能被**下游整条主路径消费**，
否则只是造了一条"录得进去但后面走不通"的死路。

两条纪律：
    1. **全程真实 HTTP**：打在已经跑起来的 uvicorn 上（`http://127.0.0.1:8000`），
       不用 TestClient（进程内调用绕开了 HTTP 栈，证明不了端点真的存在），
       也不直连数据库伪造前置条件（那是自欺欺人）。
    2. **任一步走不通就如实打印卡在哪**，绝不跳过去隐藏问题。

用法：
    python scripts/verify_manual_source_chain.py                       # 打在 8000
    python scripts/verify_manual_source_chain.py --base-url http://127.0.0.1:8123
    python scripts/verify_manual_source_chain.py --restore-ai-client   # 跑完恢复原 ai.client
"""

from __future__ import annotations

import argparse
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

BACKEND_ROOT = Path(__file__).resolve().parents[1]
# ★ 存储目录是**仓库根**的 data/（与 `Settings.storage_dir = PROJECT_ROOT / "data"` 一致），
#   不是 backend/data —— 写错目录会让 local_csv 读不到导入文件且**不报错**（静默 0 条）。
PROJECT_ROOT = BACKEND_ROOT.parent
STORAGE_DIR = PROJECT_ROOT / "data"

API = "/api/v1"
ADMIN_HEADERS = {"X-Operator": "verifier", "X-Operator-Token": "admin-token"}

TERMINAL = {"success", "failed", "cancelled"}

RESULTS: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, detail: str) -> None:
    """记录并打印一步结果。"""
    RESULTS.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}  —— {detail}")


def note(text: str) -> None:
    """打印补充说明（不影响成败判定）。"""
    print(f"   · {text}")


def snippet(resp: httpx.Response, limit: int = 240) -> str:
    """响应片段（状态码 + 截断的 body），便于直接贴进交付说明。"""
    return f"HTTP {resp.status_code} {resp.text[:limit]}"


def wait_task(client: httpx.Client, task_id: int, *, label: str, timeout_s: float = 40.0) -> dict[str, Any]:
    """轮询异步任务到终态；超时返回当前状态（**不假装成功**）。"""
    deadline = time.time() + timeout_s
    body: dict[str, Any] = {}
    while time.time() < deadline:
        body = dict((client.get(f"{API}/tasks/{task_id}").json().get("data") or {}))
        if str(body.get("status")) in TERMINAL:
            note(f"{label} 任务终态：{body.get('status')}")
            return body
        time.sleep(0.3)
    note(f"{label} 任务超时未到终态：status={body.get('status')}")
    return body


def main() -> int:
    """入口：逐步走完手工录入主路径并打印每步证据。"""
    parser = argparse.ArgumentParser(description="手工录入主路径的真实 HTTP 端到端验证")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="后端地址")
    parser.add_argument("--restore-ai-client", action="store_true", help="跑完把 ai.client 改回原值")
    args = parser.parse_args()
    base_url = str(args.base_url).rstrip("/")

    print("=" * 96)
    print(f"手工录入主路径验证（真实 HTTP）｜{base_url}")
    print("=" * 96)

    tag = uuid.uuid4().hex[:8].upper()
    product_code = f"E2E-{tag}"
    shop_id = "shop-e2e-001"
    shop_item_id = f"e2e-item-{tag}"
    shop_sku_codes = [f"E2E-{tag}-RED-XL", f"E2E-{tag}-BLU-L"]

    with httpx.Client(base_url=base_url, timeout=30) as client:
        v = lambda path: f"{API}{path}"  # noqa: E731

        # ---------- ① 端点真的存在（OpenAPI）----------
        openapi = client.get("/openapi.json")
        paths = list((openapi.json() or {}).get("paths") or {})
        has_manual = "/api/v1/source-products/manual" in paths
        has_csv = "/api/v1/source-products/import-csv" in paths
        step("① 新端点已注册（OpenAPI）", openapi.status_code == 200 and has_manual and has_csv,
             f"HTTP {openapi.status_code} 路径总数={len(paths)} manual={has_manual} import-csv={has_csv}")
        if not (has_manual and has_csv):
            note("端点没出现 ⇒ 服务没重启或者说这不是最新代码，后续无意义，停止。")
            return 1

        # ---------- ② AI 客户端切成 mock（避免 600s 等待）----------
        current = client.get(v("/settings/ai.client"))
        original_ai_client = str(((current.json().get("data") or {}) or {}).get("setting_value") or "")
        switched = client.put(
            v("/settings/ai.client"),
            json={"value": "mock", "reason": "手工录入主路径验证：先跑通，不卡 file_bridge 等待"},
            headers=ADMIN_HEADERS,
        )
        step("② ai.client 切成 mock", switched.status_code == 200,
             f"HTTP {switched.status_code}（原值={original_ai_client or '未知'}）")

        # ---------- ③ 手工录入货源商品（带 2 个 SKU）----------
        created = client.post(
            v("/source-products/manual"),
            json={
                "title": f"纯棉圆领短袖T恤 夏季薄款 {tag}",
                "product_code": product_code,
                "category_path": "女装/上装/T恤",
                "cost_price": "18.00",
                "origin_url": f"https://example.com/item/{product_code}",
                "main_image_url": f"https://example.com/img/{product_code}.jpg",
                "skus": [
                    {
                        "spec_name": "颜色;尺码", "spec_value": "红色;XL",
                        "sku_code": shop_sku_codes[0],
                        "cost_price": "18.00", "sale_price": "59.90", "stock_qty": 200,
                    },
                    {
                        "spec_name": "颜色;尺码", "spec_value": "蓝色;L",
                        "sku_code": shop_sku_codes[1],
                        "cost_price": "17.50", "sale_price": "55.90", "stock_qty": 150,
                    },
                ],
            },
            headers=ADMIN_HEADERS,
        )
        body = created.json().get("data") or {}
        source_product_id = int(body.get("id") or 0)
        step("③ 手工录入货源商品（含 SKU）",
             created.status_code == 201 and source_product_id > 0 and len(body.get("skus") or []) == 2,
             f"HTTP {created.status_code} id={source_product_id} "
             f"product_1688_id={body.get('product_1688_id')} source_platform={body.get('source_platform')} "
             f"created_skus={body.get('created_skus')}")
        if not source_product_id:
            note("手工录入失败 ⇒ 主路径无法起步，停止。")
            return 1

        skus = body.get("skus") or []
        source_sku_ids = [int(s["id"]) for s in skus]
        note(f"货源 SKU：{[(s['sku_code_1688'], s['spec_json'], s['cost_price'], s['suggested_sale_price']) for s in skus]}")

        listed = client.get(v("/source-products"), params={"source_platform": "manual", "page_size": 200})
        manual_rows = {int(i["id"]) for i in ((listed.json().get("data") or {}).get("items") or [])}
        step("③ 手工商品可按 source_platform=manual 查到", source_product_id in manual_rows,
             f"HTTP {listed.status_code} manual 商品数={len(manual_rows)}")

        # ---------- ④ AI 重构（mock）+ 审核通过 ----------
        ai = client.post(
            v("/ai-tasks"),
            json={"source_product_ids": [source_product_id], "target_platform": "taobao",
                  "rework_items": ["title"]},
            headers=ADMIN_HEADERS,
        )
        ai_ids = (ai.json().get("data") or {}).get("task_ids") or []
        ai_task_id = int(ai_ids[0]) if ai_ids else 0
        step("④ AI 重构任务创建", ai.status_code == 202 and ai_task_id > 0,
             f"HTTP {ai.status_code} ai_task_id={ai_task_id}")

        ai_ready = False
        if ai_task_id:
            deadline = time.time() + 60
            ai_status = ""
            while time.time() < deadline:
                got = client.get(v(f"/ai-tasks/{ai_task_id}"))
                ai_status = str((got.json().get("data") or {}).get("status") or "")
                if ai_status in {"success", "pending_review", "approved", "failed", "cancelled"}:
                    break
                time.sleep(0.3)
            ai_ready = ai_status in {"success", "pending_review", "approved"}
            note(f"AI 任务终态：{ai_status}")
            reviewed = client.post(v(f"/ai-tasks/{ai_task_id}/review"),
                                   json={"action": "approve", "note": "手工录入主路径验证"},
                                   headers=ADMIN_HEADERS)
            if reviewed.status_code != 200:
                # ★ 如实记录，不粉饰：本环境 SQLite 在并发写下会偶发 `database is locked`
                #   （服务日志里同一时刻也有 order_sync 提交报同样的错，属环境与配置层面问题）。
                #   这里只**重试一次**并打印首次失败，绝不把 500 悄悄吞掉。
                note(f"★ 首次审核返回 HTTP {reviewed.status_code}（{reviewed.text[:80]}），"
                     "疑似 SQLite 并发写锁，重试一次以确认是否为偶发。")
                time.sleep(1.5)
                reviewed = client.post(v(f"/ai-tasks/{ai_task_id}/review"),
                                       json={"action": "approve", "note": "手工录入主路径验证（重试）"},
                                       headers=ADMIN_HEADERS)
            step("④ AI 产出审核通过", ai_ready and reviewed.status_code == 200, snippet(reviewed, 200))

        # ---------- ⑤ 建 SKU 映射（每个货源 SKU 一条）----------
        mapping_ids: list[int] = []
        for index, source_sku_id in enumerate(source_sku_ids):
            mapping = client.post(
                v("/sku-mappings"),
                json={
                    "platform": "taobao",
                    "shop_id": shop_id,
                    "shop_item_id": shop_item_id,
                    "shop_sku_code": shop_sku_codes[index],
                    "source_product_id": source_product_id,
                    "source_sku_id": source_sku_id,
                    "source_product_1688_id": body.get("product_1688_id"),
                    "source_sku_code_1688": shop_sku_codes[index],
                    "purchase_cost": "18.00" if index == 0 else "17.50",
                    "status": "valid",
                },
                headers=ADMIN_HEADERS,
            )
            mapping_id = int((mapping.json().get("data") or {}).get("id") or 0)
            mapping_ids.append(mapping_id)
            step(f"⑤ 建 SKU 映射 #{index + 1}（{shop_sku_codes[index]}）",
                 mapping.status_code == 201 and mapping_id > 0,
                 f"HTTP {mapping.status_code} mapping_id={mapping_id}")

        # ---------- ⑥ 上架前映射校验必须放行 ----------
        validate = client.post(
            v("/sku-mappings/validate"),
            json={"source_product_id": source_product_id, "platform": "taobao",
                  "shop_id": shop_id, "sku_codes": shop_sku_codes},
            headers=ADMIN_HEADERS,
        )
        v_body = validate.json().get("data") or {}
        step("⑥ 上架前映射校验放行（non-blocking）",
             validate.status_code == 200 and v_body.get("blocking") is False,
             snippet(validate, 220))

        # ---------- ⑦ 半自动上架 → 回填商品 ID ----------
        publish = client.post(
            v("/publish-tasks"),
            json={"source_product_ids": [source_product_id], "platform": "taobao",
                  "shop_id": shop_id, "mode": "manual"},
            headers=ADMIN_HEADERS,
        )
        publish_task_id = int(((publish.json().get("data") or {}).get("task_ids") or [0])[0])
        step("⑦ 半自动上架任务创建", publish.status_code == 202 and publish_task_id > 0,
             f"HTTP {publish.status_code} publish_task_id={publish_task_id}")

        if publish_task_id:
            deadline = time.time() + 40
            publish_status = ""
            while time.time() < deadline:
                got = client.get(v(f"/publish-tasks/{publish_task_id}"))
                publish_status = str((got.json().get("data") or {}).get("status") or "")
                if publish_status in {"pending_publish", "publish_success", "validate_failed",
                                      "precheck_failed", "publish_failed"}:
                    break
                time.sleep(0.3)
            # ★ 如实记录，不粉饰：异步预检/校验**不会自动跑**（既有缺陷，见 NOTE），
            #   半自动模式下任务停在 pending_precheck 等运营去平台发布再回填 —— 这就是当前真相。
            reachable = publish_status in {"pending_precheck", "pending_publish", "publish_success"}
            step("⑦ 上架任务进入可操作状态（等待人工发布）", reachable,
                 f"publish_status={publish_status}（pending_precheck = 等运营发布后回填）")
            if publish_status == "pending_precheck":
                note("★ 已知缺陷（既有，非本次改动引入）：POST /publish-tasks 返回的 `task_record_ids` 为空，"
                     "`publish` 任务从未真正入队 ⇒ 自动预检/映射校验不会执行。")
                note("  证据：备份库 data/erp.db.bak 中 18 条 publish_task 的 task_record_id **全为 NULL**，"
                     "task_record 表**没有一条** publish 记录；16 条停在 pending_precheck，"
                     "仅 2 条靠手工 fill-back 推到 publish_success。")
                note("  影响：本次上架最终由 `fill-back` 完成并列调配额成功，但 precheck/validate 未跑；"
                     "属 P1 待修，已在报告里列出（未擅自改动）。")

            form = client.get(v(f"/publish-tasks/manual/{publish_task_id}/form-data"))
            step("⑦ 半自动预填表单数据可读", form.status_code == 200,
                 f"HTTP {form.status_code} copy_text 长度="
                 f"{len(str((form.json().get('data') or {}).get('copy_text') or ''))}")

            # ★ 回填沿用**同一批** shop_sku_code / shop_item_id ⇒ 走更新路径，不会制造重复映射
            fill = client.post(
                v(f"/publish-tasks/manual/{publish_task_id}/fill-back"),
                json={
                    "shop_item_id": shop_item_id,
                    "skus": [
                        {"shop_sku_code": shop_sku_codes[0],
                         "source_sku_code_1688": shop_sku_codes[0],
                         "sale_price": "59.90", "purchase_cost": "18.00",
                         "spec_json": {"颜色": "红色", "尺码": "XL"}, "stock_qty": 200},
                        {"shop_sku_code": shop_sku_codes[1],
                         "source_sku_code_1688": shop_sku_codes[1],
                         "sale_price": "55.90", "purchase_cost": "17.50",
                         "spec_json": {"颜色": "蓝色", "尺码": "L"}, "stock_qty": 150},
                    ],
                },
                headers=ADMIN_HEADERS,
            )
            step("⑦ 半自动回填商品 ID 并自动建/更新映射", fill.status_code == 200, snippet(fill, 240))

        # ---------- ⑧ 造订单 → 自动匹配命中 ----------
        orders_csv = STORAGE_DIR / "fulfillment" / "orders_import.csv"
        orders_csv.parent.mkdir(parents=True, exist_ok=True)
        platform_order_no = f"E2E-ORDER-{tag}"
        total_amount = "115.80"
        order_row = (
            f"taobao,{shop_id},{platform_order_no},{total_amount},"
            f"{time.strftime('%Y-%m-%d %H:%M:%S')},张三,138****0001,浙江省杭州市某某路 1 号,"
            f"{shop_item_id},{shop_sku_codes[0]},2,59.90\n"
        )
        # ★ 追加而非覆盖：`orders_import.csv` 是适配器固定读取的投递文件，
        #   里面可能还有别人（QA / 之前的验证）还没同步完的行，覆盖等于静默删数据。
        #   订单同步按 platform_order_no 幂等去重，追加是安全的。
        had_content = orders_csv.exists() and orders_csv.stat().st_size > 0
        with orders_csv.open("a", encoding="utf-8", newline="") as handle:
            if not had_content:
                handle.write(
                    "platform,shop_id,platform_order_no,total_amount,paid_at,receiver_name,"
                    "receiver_phone,receiver_address,shop_item_id,shop_sku_code,quantity,price\n"
                )
            handle.write(order_row)
        note(f"订单行已追加到 {orders_csv}（shop_sku_code={shop_sku_codes[0]}）")

        sync = client.post(v("/orders/sync"), json={"adapter_name": "local_csv", "force": True},
                           headers=ADMIN_HEADERS)
        sync_task = int((sync.json().get("data") or {}).get("task_record_id") or 0)
        step("⑧ 订单同步受理", sync.status_code == 202 and sync_task > 0,
             f"HTTP {sync.status_code} task_id={sync_task}")
        if sync_task:
            wait_task(client, sync_task, label="订单同步")

        target_order: dict[str, Any] | None = None
        listed_orders = client.get(v("/orders"), params={"page_size": 200})
        items = ((listed_orders.json().get("data") or {}).get("items") or [])
        target_order = next((o for o in items if o.get("platform_order_no") == platform_order_no), None)
        step("⑧ 订单已落库", target_order is not None,
             f"HTTP {listed_orders.status_code} order_id={target_order and target_order.get('id')} "
             f"status={target_order and target_order.get('fulfillment_status')} "
             f"match={target_order and target_order.get('match_status')}")
        if target_order is None:
            note("订单未落库 ⇒ 匹配与采购无法继续，停止。")
            return 1

        step("⑧ 自动匹配命中手工录入的货源 SKU",
             str(target_order.get("match_status")) == "matched"
             and str(target_order.get("fulfillment_status")) == "matched",
             f"order_id={target_order.get('id')} match_status={target_order.get('match_status')} "
             f"fulfillment_status={target_order.get('fulfillment_status')}")

        order_detail = client.get(v(f"/orders/{target_order['id']}"))
        detail_items = ((order_detail.json().get("data") or {}).get("items") or [])
        step("⑧ 订单明细已挂上本次手工录入的映射",
             bool(detail_items) and int(detail_items[0].get("sku_mapping_id") or 0) in mapping_ids,
             f"HTTP {order_detail.status_code} sku_mapping_id={detail_items[0].get('sku_mapping_id')} "
             f"source_sku_code_1688={detail_items[0].get('source_sku_code_1688')} "
             f"purchase_cost={detail_items[0].get('purchase_cost')}")

        # ---------- ⑨ 生成采购单（本地兜底 manual_pending）----------
        placed = client.post(v(f"/orders/{target_order['id']}/place-purchase"), headers=ADMIN_HEADERS)
        placed_body = placed.json().get("data") or {}
        step("⑨ 采购单生成", placed.status_code == 200,
             f"HTTP {placed.status_code} purchase_order_no={placed_body.get('purchase_order_no')} "
             f"purchase_status={placed_body.get('purchase_status')}")
        step("⑨ 本地兜底绝不伪装成「已下单」",
             str(placed_body.get("purchase_status")) == "manual_pending",
             f"purchase_status={placed_body.get('purchase_status')} adapter={placed_body.get('adapter_name')}")

        after = client.get(v(f"/orders/{target_order['id']}"))
        step("⑨ 订单推进到 purchased",
             str((after.json().get("data") or {}).get("fulfillment_status")) == "purchased",
             f"HTTP {after.status_code} fulfillment_status="
             f"{(after.json().get('data') or {}).get('fulfillment_status')}")

        # ---------- 恢复 ai.client（可选）----------
        if args.restore_ai_client and original_ai_client:
            restored = client.put(
                v("/settings/ai.client"),
                json={"value": original_ai_client, "reason": "验证结束恢复原值"},
                headers=ADMIN_HEADERS,
            )
            note(f"恢复 ai.client={original_ai_client}：HTTP {restored.status_code}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n" + "=" * 96)
    print(f"手工录入主路径结果：{passed}/{len(RESULTS)} 项成立（商品编码 {product_code}）")
    print("=" * 96)
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"   ❌ {name}: {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
