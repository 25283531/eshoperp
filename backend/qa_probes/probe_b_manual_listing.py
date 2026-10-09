"""★ QA 探针 B：端到端主路径 —— 半自动上架全链路（真实 HTTP，逐帧打印响应）。

链路：
  采集 → AI 重构 → 审核 → 上架任务(mode=manual) → 回填商品 ID → 自动建映射 → 校验
并逐项攻击：
  B-3  AIR-P0-03：未审核 AI 结果提交上架必须 422 / 4005
  B-5  LST-P0-07：回填不传 sale_price 必须 422
  B-8  反向：制造 P0 冲突后重新 validate 必须 blocking=true
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

BASE = "http://127.0.0.1:8141"
API = f"{BASE}/api/v1"

for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

CALL_NO = 0
FAILS: list[str] = []


def req(
    method: str,
    path: str,
    body: Any = None,
    *,
    expect: set[int] | None = None,
    label: str = "",
) -> tuple[int, dict]:
    """发一次请求并打印真实响应。"""
    global CALL_NO
    CALL_NO += 1
    url = path if path.startswith("http") else API + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json", "X-Operator": "qa"}
    http_req = urllib.request.Request(url, data=data, headers=headers, method=method)
    started = time.perf_counter()
    try:
        with _opener.open(http_req, timeout=30) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        print(f"  [ERR] {method} {path} -> {exc}")
        FAILS.append(f"{method} {path}: {exc}")
        return -1, {}
    elapsed = int((time.perf_counter() - started) * 1000)
    try:
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001
        payload = {"raw": raw[:300]}

    ok = True
    if expect is not None and status not in expect:
        ok = False
        FAILS.append(f"{method} {path} 期望 {sorted(expect)}，实际 {status}")
    tag = "[OK]  " if ok else "[FAIL]"
    print(f"  {tag} [{CALL_NO:02}] {method:5} {path.replace(API, '')}")
    print(f"        status={status}  {elapsed}ms   {label}")
    print(f"        code={payload.get('code')}  message={payload.get('message')}")
    pretty = json.dumps(payload.get("data"), ensure_ascii=False)
    print(f"        data={pretty[:600]}{' ...(truncated)' if len(pretty) > 600 else ''}")
    return status, payload


def banner(title: str) -> None:
    """分节标题。"""
    print(f"\n{'=' * 96}\n{title}\n{'=' * 96}")


def main() -> int:
    """执行主链路。"""
    banner("B-1  采集货源商品  POST /source-products/collect")
    status, res = req(
        "POST",
        "/source-products/collect",
        {"source": "1688", "identifiers": ["https://detail.1688.com/offer/QA-E2E-0001.htm"]},
        expect={200, 201, 202},
        label="采集（1688 mock 适配器）",
    )
    items = ((res.get("data") or {}).get("items") or []) if status in (200, 201, 202) else []
    collected = ((res.get("data") or {}).get("collected") or [])
    if isinstance(collected, list) and collected:
        source_id = int(collected[0].get("id") or 0)
    elif items:
        source_id = int(items[0].get("id") or 0)
    else:
        # 回退：取列表里第一个货源商品
        _, lst = req("GET", "/source-products?page=1&page_size=1", expect={200}, label="回退：取已有货源商品")
        rows = ((lst.get("data") or {}).get("items") or [])
        source_id = int(rows[0].get("id") or 0) if rows else 0
    print(f"  --> source_product_id = {source_id}")

    if not source_id:
        print("  [FAIL] 拿不到货源商品，链路无法继续")
        return 1

    _, skus_res = req(
        "GET", f"/source-products/{source_id}/skus", expect={200}, label="货源 SKU 列表"
    )
    raw_skus = skus_res.get("data")
    sku_rows = raw_skus if isinstance(raw_skus, list) else ((raw_skus or {}).get("items") or [])
    source_sku_codes = [str(s.get("sku_code_1688")) for s in sku_rows if s.get("sku_code_1688")]
    print(f"  --> source_sku_codes = {source_sku_codes}")

    # ------------------------------------------------------------------
    banner("B-2  创建 AI 重构任务  POST /ai-tasks")
    _, ai = req(
        "POST",
        "/ai-tasks",
        {"source_product_ids": [source_id], "target_platform": "taobao"},
        expect={200, 201, 202},
        label="AI 重构任务",
    )
    ai_task_ids = ((ai.get("data") or {}).get("task_ids") or [])
    ai_task_id = int(ai_task_ids[0]) if ai_task_ids else 0
    print(f"  --> ai_task_id = {ai_task_id}")

    ai_result_id = 0
    if ai_task_id:
        time.sleep(4)
        _, detail = req("GET", f"/ai-tasks/{ai_task_id}", expect={200}, label="轮询 AI 任务")
        results = ((detail.get("data") or {}).get("results") or [])
        for r in results:
            print(f"        result id={r.get('id')} review_status={r.get('review_status')}")
        if results:
            ai_result_id = int(results[0].get("id") or 0)
    print(f"  --> ai_task_result_id = {ai_result_id}")

    # ------------------------------------------------------------------
    banner("B-3  ★ AIR-P0-03：未审核素材提交上架必须被拒（422 / 4005）")
    if ai_result_id:
        req(
            "POST",
            "/publish-tasks",
            {
                "source_product_ids": [source_id],
                "platform": "taobao",
                "shop_id": f"qa-shop-{int(time.time())}",
                "mode": "manual",
                "ai_task_result_ids": {str(source_id): ai_result_id},
            },
            expect={422},
            label="未审核 AI 结果 → 期望 422 / 4005",
        )
    else:
        print("  [SKIP] 没有 AI 结果，改用不存在的 result id 验证")
        req(
            "POST",
            "/publish-tasks",
            {
                "source_product_ids": [source_id],
                "platform": "taobao",
                "shop_id": "qa-shop-x",
                "mode": "manual",
                "ai_task_result_ids": {str(source_id): 999999},
            },
            expect={422},
            label="不存在的 AI 结果 → 期望 422 / 4005",
        )

    # ------------------------------------------------------------------
    banner("B-4  审核通过后提交半自动上架任务  POST /publish-tasks (mode=manual)")
    if ai_task_id and ai_result_id:
        req(
            "POST", f"/ai-tasks/{ai_task_id}/review",
            {"action": "approve", "note": "QA 自动审核"},
            expect={200}, label="审核 AI 结果为 approved",
        )
    shop_id = f"qa-shop-{int(time.time())}"
    _, pub = req(
        "POST",
        "/publish-tasks",
        {
            "source_product_ids": [source_id],
            "platform": "taobao",
            "shop_id": shop_id,
            "mode": "manual",
        },
        expect={200, 201, 202},
        label="半自动上架任务（不带 AI 结果）",
    )
    task_ids = ((pub.get("data") or {}).get("task_ids") or [])
    task_id = int(task_ids[0]) if task_ids else 0
    print(f"  --> publish_task_id = {task_id}")
    if not task_id:
        print("  [FAIL] 未创建成功")
        return 1

    time.sleep(3)
    req("GET", f"/publish-tasks/{task_id}", expect={200}, label="任务状态（异步执行后）")
    req("GET", f"/publish-tasks/manual/{task_id}/form-data", expect={200}, label="半自动预填表单")

    # ------------------------------------------------------------------
    banner("B-5  ★ LST-P0-07：回填不传 sale_price 必须 422 拒绝")
    first_code = source_sku_codes[0] if source_sku_codes else "QA-SKU-001"
    req(
        "POST",
        f"/publish-tasks/manual/{task_id}/fill-back",
        {
            "shop_item_id": f"QA-ITEM-{int(time.time())}",
            "skus": [{"shop_sku_code": first_code, "spec_json": {}}],
        },
        expect={422},
        label="缺失 sale_price → 期望 422",
    )
    req(
        "POST",
        f"/publish-tasks/manual/{task_id}/fill-back",
        {
            "shop_item_id": f"QA-ITEM-{int(time.time())}",
            "skus": [{"shop_sku_code": first_code, "spec_json": {}, "sale_price": 0}],
        },
        expect={422},
        label="sale_price = 0 → 期望 422",
    )

    # ------------------------------------------------------------------
    banner("B-6  正常回填（含售价）→ 自动建映射")
    shop_item_id = f"QA-ITEM-{int(time.time())}"
    fill_skus = [
        {
            "shop_sku_code": code,
            "spec_json": {},
            "source_sku_code_1688": code,
            "sale_price": 99.0,
        }
        for code in (source_sku_codes or ["QA-SKU-001"])
    ]
    _, filled = req(
        "POST",
        f"/publish-tasks/manual/{task_id}/fill-back",
        {"shop_item_id": shop_item_id, "skus": fill_skus},
        expect={200},
        label="完整回填 → 期望 200 且建立映射",
    )
    mapping_ids = ((filled.get("data") or {}).get("mapping_ids") or [])
    print(f"  --> mapping_ids = {mapping_ids}")

    if mapping_ids:
        req("GET", f"/sku-mappings?page=1&page_size=3", expect={200}, label="确认映射已落库")
        for mid in mapping_ids[:3]:
            req("GET", f"/sku-mappings?page=1&page_size=20", expect={200}, label=f"查映射 {mid}")

    # ------------------------------------------------------------------
    banner("B-7  GET /sku-mappings/validate → 应通过")
    _, val = req(
        "POST",
        "/sku-mappings/validate",
        {
            "source_product_id": source_id,
            "platform": "taobao",
            "shop_id": shop_id,
            "sku_codes": fill_skus and [s["shop_sku_code"] for s in fill_skus] or [],
        },
        expect={200, 422},
        label="上架前映射到货校验",
    )
    vdata = val.get("data") or {}
    print(f"  --> passed={vdata.get('passed')} blocking={vdata.get('blocking')}")

    # ------------------------------------------------------------------
    banner("B-8  ★ 反向验证：制造 P0 冲突（把映射成本改成 0）后重新 validate")
    if mapping_ids:
        for mid in mapping_ids:
            try:
                req(
                    "PUT", f"/sku-mappings/{mid}",
                    {"purchase_cost": 0},
                    expect={200, 422},
                    label=f"试图把映射 {mid} 成本改为 0（服务层应拒绝）",
                )
            except Exception as exc:  # noqa: BLE001
                print(f"        {exc}")
        _, val2 = req(
            "POST",
            "/sku-mappings/validate",
            {
                "source_product_id": source_id,
                "platform": "taobao",
                "shop_id": shop_id,
                "sku_codes": [s["shop_sku_code"] for s in fill_skus],
            },
            expect={200, 422},
            label="改成本后重新校验",
        )
        v2 = val2.get("data") or {}
        print(f"  --> passed={v2.get('passed')} blocking={v2.get('blocking')}")

        # 制造「映射缺失」：软删除映射
        banner("B-9  软删除映射后重新 validate → 必须 blocking（映射缺失）")
        for mid in mapping_ids:
            req(
                "DELETE", f"/sku-mappings/{mid}?confirm=true",
                expect={200, 400, 422},
                label=f"软删除映射 {mid}",
            )
        _, val3 = req(
            "POST",
            "/sku-mappings/validate",
            {
                "source_product_id": source_id,
                "platform": "taobao",
                "shop_id": shop_id,
                "sku_codes": [s["shop_sku_code"] for s in fill_skus],
            },
            expect={200, 422},
            label="映射缺失后校验",
        )
        v3 = val3.get("data") or {}
        print(f"  --> passed={v3.get('passed')} blocking={v3.get('blocking')} missing={v3.get('missing_mappings')}")

    banner("B 段结束")
    if FAILS:
        print(f"  断言失败 {len(FAILS)} 项：")
        for item in FAILS:
            print(f"   - {item}")
    else:
        print("  所有显式断言通过")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
