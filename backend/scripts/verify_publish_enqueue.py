"""★ 上架任务「入队可见性」真实 HTTP 验证脚本（2026-10-08 修复举证）。

================================================================================
★ 它要证明什么
================================================================================
缺陷（实测）：`POST /api/v1/publish-tasks` 长期返回 **202 且 `task_record_ids: []`**，
`publish_task.task_record_id` 全 NULL、`task_record` 里 0 条 publish ——
上架任务**从未真正入队**，前端看到"受理成功"，任务永远停在 `pending_precheck`。

修复两条：
    1. **先 commit 再入队**（请求会话不再持有未提交写事务，根治 SQLite 写锁）；
    2. **入队失败抛 500 / 1098**（不再 `except Exception: return None` 假装成功），
       并把失败原因写到 `publish_task.error_advice`（列表页可见）。

顺带验证第三条结论：**首次上架不再被「尚不存在的店铺 SKU 编码」判成映射缺失而硬拦截**
（旧实现拿 `{平台}-{任务ID}-{序号}` 这组预测编码去查映射，必然 missing ⇒ 恒失败）。

两条纪律（与 `verify_manual_source_chain.py` 一致）：
    1. **全程真实 HTTP**：打在已启动的 uvicorn 上，不用 TestClient、不直连库伪造前置条件；
    2. **任一步走不通就如实打印卡在哪**，绝不跳过去隐藏问题。

用法：
    python scripts/verify_publish_enqueue.py
    python scripts/verify_publish_enqueue.py --base-url http://127.0.0.1:8123
"""

from __future__ import annotations

import argparse
import time
import uuid
from typing import Any

import httpx

API = "/api/v1"
ADMIN_HEADERS = {"X-Operator": "verifier", "X-Operator-Token": "admin-token"}
TERMINAL = {"success", "failed", "cancelled"}

RESULTS: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, detail: str) -> None:
    """记录并打印一步结果。"""
    RESULTS.append((name, ok, detail))
    print(f"{'OK  ' if ok else 'FAIL'} {name}  -- {detail}")


def note(text: str) -> None:
    """打印补充说明（不影响成败判定）。"""
    print(f"   . {text}")


def wait_task(client: httpx.Client, task_id: int, *, label: str, timeout_s: float = 60.0) -> dict[str, Any]:
    """轮询异步任务到终态；超时返回当前状态（**不假装成功**）。"""
    deadline = time.time() + timeout_s
    body: dict[str, Any] = {}
    while time.time() < deadline:
        body = dict(client.get(f"{API}/tasks/{task_id}").json().get("data") or {})
        if str(body.get("status")) in TERMINAL:
            note(f"{label} 任务终态：{body.get('status')}")
            return body
        time.sleep(0.3)
    note(f"{label} 任务超时未到终态：status={body.get('status')}")
    return body


def main() -> int:
    """入口：走完「手工录入 → AI 重构 → 上架入队 → 任务推进」并打印每步证据。"""
    parser = argparse.ArgumentParser(description="上架任务入队可见性的真实 HTTP 验证")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="后端地址")
    args = parser.parse_args()
    base_url = str(args.base_url).rstrip("/")

    print("=" * 96)
    print(f"上架任务入队可见性验证（真实 HTTP）| {base_url}")
    print("=" * 96)

    tag = uuid.uuid4().hex[:8].upper()
    shop_id = f"shop-enq-{tag}"

    # ★ `trust_env=False`：本机环境里 `HTTP_PROXY` 指向 127.0.0.1:53839，
    #   走代理时后端一挂就返回代理自己的 502 HTML（而非连接失败），
    #   很容易被误读成"接口报错"。直连才能拿到真实状态码。
    with httpx.Client(base_url=base_url, timeout=60, trust_env=False) as client:
        path = lambda p: f"{API}{p}"  # noqa: E731

        # ---------- ① 服务活着 ----------
        health = client.get(f"{API}/health")
        step("① 服务可达", health.status_code == 200, f"HTTP {health.status_code}")

        # ---------- ② ai.client 切 mock（避免 file_bridge 长等待）----------
        switched = client.put(
            path("/settings/ai.client"),
            json={"value": "mock", "reason": "上架入队验证：不等外部文件桥"},
            headers=ADMIN_HEADERS,
        )
        step("② ai.client 切成 mock", switched.status_code == 200, f"HTTP {switched.status_code}")

        # ---------- ③ 手工录入货源商品（★ 不碰 1688）----------
        created = client.post(
            path("/source-products/manual"),
            json={
                "title": f"入队验证商品 {tag}",
                "product_code": f"ENQ-{tag}",
                "cost_price": "18.00",
                "skus": [
                    {
                        "spec_name": "颜色;尺码",
                        "spec_value": "红色;XL",
                        "cost_price": "18.00",
                        "sale_price": "59.90",
                        "stock_qty": 200,
                    }
                ],
            },
            headers=ADMIN_HEADERS,
        )
        if created.status_code != 201:
            step("③ 手工录入货源商品", False, f"HTTP {created.status_code} {created.text[:240]}")
            return 1
        product_id = int(created.json()["data"]["id"])
        step("③ 手工录入货源商品", True, f"HTTP {created.status_code} source_product_id={product_id}")

        # ---------- ④ AI 重构并审核通过（★ AIR-P0-03：未过审不能上架）----------
        rework = client.post(
            path("/ai-tasks"),
            json={
                "source_product_ids": [product_id],
                "target_platform": "taobao",
                "rework_items": ["title", "main_image"],
            },
            headers=ADMIN_HEADERS,
        )
        if rework.status_code != 202:
            step("④ 创建 AI 重构任务", False, f"HTTP {rework.status_code} {rework.text[:240]}")
            return 1
        ai_task_id = int(rework.json()["data"]["task_ids"][0])
        step("④ 创建 AI 重构任务", True, f"HTTP {rework.status_code} ai_task_id={ai_task_id}")

        # ★ AI 任务的终态不是 "success"：跑完是 `pending_review`（等人工审核），
        #   审核后才是 `approved`。这里只需等到「不再排队/不再跑」。
        deadline = time.time() + 60
        ai_status = ""
        detail: dict[str, Any] = {}
        while time.time() < deadline:
            detail = dict(client.get(path(f"/ai-tasks/{ai_task_id}")).json().get("data") or {})
            ai_status = str(detail.get("status") or "")
            if ai_status not in {"", "queued", "running"}:
                break
            time.sleep(0.4)
        note(f"AI 任务状态：{ai_status}")

        result_id = 0
        result = dict(detail.get("result") or {})
        if result.get("id"):
            result_id = int(result["id"])

        reviewed_ok = False
        if result_id:
            review = client.post(
                path(f"/ai-tasks/{ai_task_id}/review"),
                json={"action": "approve", "note": "入队验证：审核通过"},
                headers=ADMIN_HEADERS,
            )
            if review.status_code != 200:
                time.sleep(1.0)  # SQLite 偶发写锁，重试一次并如实记录
                review = client.post(
                    path(f"/ai-tasks/{ai_task_id}/review"),
                    json={"action": "approve", "note": "入队验证：审核通过（重试）"},
                    headers=ADMIN_HEADERS,
                )
            reviewed_ok = review.status_code == 200
            note(f"审核：HTTP {review.status_code}（产物 id={result_id}）")
        step("④b AI 产出审核通过", reviewed_ok, f"ai_task={ai_task_id} result_id={result_id}")

        # ---------- ⑤ ★ 创建上架任务：task_record_ids 必须非空 ----------
        publish = client.post(
            path("/publish-tasks"),
            json={
                "source_product_ids": [product_id],
                "platform": "taobao",
                "shop_id": shop_id,
                "mode": "manual",
                **({"ai_task_result_ids": {str(product_id): result_id}} if result_id else {}),
            },
            headers=ADMIN_HEADERS,
        )
        if publish.status_code != 202:
            step("⑤ 上架受理（202）", False, f"HTTP {publish.status_code} {publish.text[:300]}")
            return 1

        data = dict(publish.json().get("data") or {})
        task_ids = [int(i) for i in (data.get("task_ids") or [])]
        record_ids = [int(i) for i in (data.get("task_record_ids") or [])]
        # ★★ 本脚本的核心断言：入队记录不得为空（旧实现恒为 []）★★
        step(
            "⑤ 上架受理且**真的入队**",
            bool(record_ids) and len(record_ids) == len(task_ids),
            f"HTTP {publish.status_code} task_ids={task_ids} task_record_ids={record_ids}",
        )
        if not record_ids:
            note("task_record_ids 为空 ⇒ 入队失败被当成成功返回（静默失效复发），后续无意义，停止。")
            return 1

        publish_task_id = task_ids[0]
        record_id = record_ids[0]

        # ---------- ⑥ 任务必须跑到终态 ----------
        record = wait_task(client, record_id, label="上架")
        step(
            "⑥ 上架任务跑到终态",
            str(record.get("status")) in {"success", "failed"},
            f"task_record {record_id} status={record.get('status')} "
            f"error={record.get('error_message') or '-'}",
        )

        # ---------- ⑦ 上架任务必须真的推进（不再停在 pending_precheck）----------
        detail_resp = client.get(path(f"/publish-tasks/{publish_task_id}"))
        task = dict(detail_resp.json().get("data") or {})
        status_now = str(task.get("status") or "")
        step(
            "⑦ 上架任务状态已推进",
            status_now not in {"", "pending_precheck"},
            f"HTTP {detail_resp.status_code} status={status_now} "
            f"validate_blocking={(task.get('validate_result') or {}).get('blocking')} "
            f"advice={task.get('error_advice') or '-'}",
        )
        # ★ 第三条结论的线上举证：首次上架不该被判成「映射缺失」
        validate_result = dict(task.get("validate_result") or {})
        if validate_result:
            step(
                "⑦b 首次上架未被「预测编码」误判为映射缺失",
                validate_result.get("blocking") is False
                and not (validate_result.get("missing_mappings") or []),
                f"blocking={validate_result.get('blocking')} "
                f"missing={validate_result.get('missing_mappings')} "
                f"checked_sku_count={validate_result.get('checked_sku_count')}",
            )
        else:
            note("本次未走到映射校验阶段（多半是预检未过），validate_result 为空 —— 如实记录，不算通过。")

        # ---------- ⑧ 入队失败必须 500 / 1098（★ 这一条无法在不改代码的前提下于真实服务上触发）
        note(
            "入队失败路径（500 / 1098）无法在**已启动的真实服务**上凭空触发："
            "它需要任务队列本身不可用。该路径由 tests/test_publish_enqueue_visibility.py::"
            "test_publish_enqueue_failure_returns_500_not_202 覆盖 —— 走完整 ASGI/HTTP 栈与真实测试库，"
            "仅把 runner 打桩成抛错，断言 500/1098 且失败原因落到 publish_task.error_advice。"
        )

    print("=" * 96)
    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"结论：{len(RESULTS) - len(failed)}/{len(RESULTS)} 步通过" + (f"｜未通过：{failed}" if failed else ""))
    print("=" * 96)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
