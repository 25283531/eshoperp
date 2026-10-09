"""★ QA 探针：SQLite 写锁 A/B 实证 —— 一个 AI 重构任务是否会锁死全库写入。

假设：AiTaskService.run_task() 先 `status=RUNNING; flush()` 打开写事务，
      然后调用 file_bridge 客户端轮询最多 `ai.poll_timeout_sec`（默认 1800s）等人工产出。
      ⇒ 一个 AI 任务在跑的时候，全库写操作全部失败（database is locked）。

A/B 设计：
    A. 无 AI 任务时：POST /publish-tasks → 期望 202
    B. 触发 AI 任务后：POST /publish-tasks → 观察是否 500
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

BASE = "http://127.0.0.1:8141"
API = f"{BASE}/api/v1"

for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def req(method: str, path: str, body: Any = None) -> tuple[int, dict]:
    """HTTP 请求。"""
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    r = urllib.request.Request(
        API + path, data=data, method=method, headers={"Content-Type": "application/json", "X-Operator": "qa"}
    )
    started = time.perf_counter()
    try:
        with _opener.open(r, timeout=40) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode(errors="replace") or "{}")


def try_publish(tag: str, shop: str) -> int:
    """打一次上架任务并返回状态码。"""
    st, res = req(
        "POST",
        "/publish-tasks",
        {"source_product_ids": [1], "platform": "taobao", "shop_id": shop, "mode": "manual"},
    )
    note = res.get("message") or str(res.get("data"))[:80]
    print(f"  {tag:34} -> HTTP {st}  {note}")
    return st


def main() -> None:
    """执行 A/B。"""
    print("=" * 96)
    print("A 组：当前无 AI 任务在跑")
    print("=" * 96)
    a_ok = [s for s in (try_publish(f"A{i + 1} POST /publish-tasks", f"qa-a-{int(time.time())}-{i}") for i in range(3))]
    print(f"  A 组结果: {a_ok}")

    print()
    print("=" * 96)
    print("B 组：触发一个 AI 重构任务（file_bridge 会轮询等待人工产出）")
    print("=" * 96)
    st, res = req("POST", "/ai-tasks", {"source_product_ids": [1], "target_platform": "taobao"})
    print(f"  POST /ai-tasks -> HTTP {st}  {res.get('data')}")
    print("  等待 15s，让 AI 任务进入长轮询……")
    time.sleep(15)

    b_list = []
    for i in range(3):
        b_list.append(try_publish(f"B{i + 1} POST /publish-tasks", f"qa-b-{int(time.time())}-{i}"))
        time.sleep(1)
    print(f"  B 组结果: {b_list}")

    print()
    print("=" * 96)
    print("C 组：同期其它写操作（订单同步 / 建映射）")
    print("=" * 96)
    for path, body, method in (
        ("/orders/sync", {}, "POST"),
        ("/sku-mappings", {
            "platform": "douyin", "shop_id": "qa-lock", "shop_item_id": "item-lock",
            "shop_sku_code": f"LOCK-{int(time.time())}", "source_product_id": 1,
            "source_sku_id": 1, "source_sku_code_1688": "SEED-RED-XL", "purchase_cost": "12.00",
        }, "POST"),
    ):
        st, res = req(method, path, body)
        print(f"  {method} {path:22} -> HTTP {st}  {res.get('message')}")

    print()
    print("=" * 96)
    print("结论")
    print("=" * 96)
    if 500 in b_list and 500 not in a_ok:
        print("  ★ 实证成立：AI 任务运行期间，普通写接口从 202 退化为 500（database is locked）。")
        print("    根因：run_task() 在 file_bridge 长轮询（默认上限 1800s）之前已 flush 打开写事务，")
        print("    事务在整个轮询期间不提交 → SQLite 单写者模型下全库写操作被阻塞。")
    elif 500 in a_ok:
        print("  ⚠ A 组即失败 —— 说明写锁并非 AI 任务引起，需另行定位。")
    else:
        print("  未复现：AI 任务未导致写锁（可能是轮询提前返回或超时配置较短）。")


if __name__ == "__main__":
    main()
