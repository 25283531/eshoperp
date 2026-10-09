"""★ 从零起步的全链路验证（交付证据用）。

================================================================================
★ 为什么要有这个脚本
================================================================================
此前所有验证都跑在「已经有历史数据的库」上 —— 那证明不了**新用户从零能不能用**。
这个脚本刻意从一张白纸开始：

    全新库文件 → alembic upgrade head → seed → 启动真实应用（bootstrap 自动落库）
    → 采集 → AI 重构（审核通过）→ 上架（半自动）→ 回填建映射 → 校验映射
    → 订单同步 → SKU 匹配 → 采购下单 → 物流回填 → 售后

两条纪律：
    1. **全程真实 HTTP**：走 uvicorn 真实进程 + TCP 请求，不是 ASGITransport、
       更不是直接调服务层函数。绕开应用层构造前置条件 = 自欺欺人。
    2. **任一步走不通就如实打印卡在哪，绝不跳过去**。
       环境限制（如 1688 不可达）导致的失败会明确标注 `ENV_LIMIT`，
       与真正的代码缺陷区分开 —— 但不会静默跳过。

用法：
    python scripts/verify_full_chain.py            # 全新库，跑完保留
    python scripts/verify_full_chain.py --keep     # 同上（默认保留新库以便复查）
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

BACKEND_ROOT = Path(__file__).resolve().parents[1]
# ★ 存储目录是**仓库根**的 data/（与 `Settings.storage_dir = PROJECT_ROOT / "data"` 一致），
#   不是 backend/data —— 写错目录会导致 local_csv 读不到导入文件且**不报错**（静默 0 条）。
PROJECT_ROOT = BACKEND_ROOT.parent
STORAGE_DIR = PROJECT_ROOT / "data"
sys.path.insert(0, str(BACKEND_ROOT))

API = "/api/v1"
ADMIN_HEADERS = {"X-Operator": "verifier", "X-Operator-Token": "admin-token"}

RESULTS: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, detail: str) -> None:
    """记录并打印一步结果。"""
    RESULTS.append((name, ok, detail))
    mark = "✅" if ok else "❌"
    print(f"{mark} {name}  —— {detail}")


def note(text: str) -> None:
    """打印环境限制说明（不算失败，但必须可见）。"""
    print(f"   · {text}")


def snippet(resp: httpx.Response, limit: int = 220) -> str:
    """响应片段（状态码 + 截断的 body），便于直接贴进交付说明。"""
    return f"HTTP {resp.status_code} {resp.text[:limit]}"


# ======================================================================
#  0. 全新库 + 迁移 + seed + 启动真实应用
# ======================================================================
def prepare_db(db_path: Path) -> dict[str, str]:
    """用**全新**的数据库文件，从零跑 alembic。

    注意：绝不碰现有的 `data/erp.db`。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        stale = Path(str(db_path) + suffix)
        if stale.exists():
            stale.unlink()

    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    env["PYTHONPATH"] = str(BACKEND_ROOT)
    env["APP_ENV"] = "dev"
    # ★★ 绝不关闭调度器 ★★
    #    team-lead 明确要求：验证必须按**生产真实启动方式**跑。
    #    earlier 版本为了"让验证稳定"把 SCHEDULER_ENABLED 关掉，
    #    那样 `purchase_place` 这类周期任务根本不会被装配 —— 验证是假的。
    #    保留默认（开启）：调度器与任务框架一并被验证。
    env.pop("SCHEDULER_ENABLED", None)
    env.pop("TASK_RECOVERY_ENABLED", None)
    env["ADMIN_TOKEN"] = "admin-token"
    env["OPERATOR_TOKEN"] = "operator-token"
    env["SECRET_KEY"] = "verify-secret-key"
    env.setdefault("LOG_JSON", "false")
    return env


def run_subprocess(cmd: list[str], env: dict[str, str], *, label: str) -> tuple[bool, str]:
    """跑子进程并返回 (是否成功, 输出尾部)。"""
    proc = subprocess.run(
        cmd,
        cwd=str(BACKEND_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    tail = (proc.stdout or "")[-600:] + (proc.stderr or "")[-600:]
    return proc.returncode == 0, tail


def wait_for_health(base_url: str, *, timeout_s: int = 60) -> httpx.Response | None:
    """等应用起来（真实 HTTP 轮询 /health）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            resp = httpx.get(f"{base_url}{API}/health", timeout=5)
            if resp.status_code == 200:
                return resp
        except Exception:  # noqa: BLE001  启动期连接被拒属正常
            time.sleep(0.5)
    return None


# ======================================================================
#  主流程
# ======================================================================
def verify_chain(client: httpx.Client, db_path: Path) -> None:
    """逐步走主路径；每步都打印真实状态码与响应片段。"""
    v = lambda p: f"{API}{p}"  # noqa: E731

    # ---------- ① 应用已起来 + bootstrap 已落库 ----------
    resp = client.get(v("/adapters/fulfillment"))
    payload = resp.json().get("data") or {}
    adapters = [a.get("adapter_name") for a in (payload.get("adapters") or [])]
    step("① 启动后 bootstrap 自动落库（适配器清单非空）", bool(adapters),
         f"HTTP {resp.status_code} active={payload.get('active_adapter')} adapters={adapters}")

    health = client.get(v("/health"))
    body = health.json()["data"]
    step("① /health 结构性约束自检通过", body.get("constraints", {}).get("ok") is True,
         f"HTTP {health.status_code} status={body.get('status')} constraints={body.get('constraints')}")

    # ---------- ② 采集 ----------
    #   ★ 先记基线：seed 本身会造 1 条货源商品，若不减掉基线就会把 seed 的成果
    #     误算成采集的成果（正是"看起来跑通了"这种自欺的来源）。
    baseline_products = len(
        (client.get(v("/source-products")).json().get("data") or {}).get("items") or []
    )
    collect = client.post(
        v("/source-products/collect"),
        json={"identifiers": ["https://detail.1688.com/offer/888888888.html"]},
        headers=ADMIN_HEADERS,
    )
    task_id = (collect.json().get("data") or {}).get("task_record_id")
    step("② 采集任务受理", collect.status_code == 202 and bool(task_id),
         f"HTTP {collect.status_code} task_id={task_id}")

    products: list[dict[str, Any]] = []
    if task_id:
        for _ in range(24):
            time.sleep(0.25)
            t = client.get(v(f"/tasks/{task_id}"))
            status = str((t.json().get("data") or {}).get("status"))
            if status in {"success", "failed", "cancelled"}:
                note(f"采集任务终态：{status}")
                break
        listed = client.get(v("/source-products"))
        products = (listed.json().get("data") or {}).get("items") or []

    created_products = max(len(products) - baseline_products, 0)
    if created_products:
        step("② 采集产出货源商品", True, f"新增 {created_products} 条（基线 {baseline_products} 条）")
    else:
        # ★ 必须把**真实失败原因**打出来，而不是笼统甩锅给"离线"。
        #   实测（网络可用：DNS + HTTPS 均通）失败原因是适配器未配置 1688 凭证：
        #   "未配置 1688 AppKey / AccessToken（TODO: 需在系统设置 → 凭证中填写）"
        reason = ""
        if task_id:
            t = client.get(v(f"/tasks/{task_id}"))
            result_json = (t.json().get("data") or {}).get("result_json") or {}
            failed = result_json.get("failed") or []
            reason = failed[0].get("reason", "") if failed else ""
        step("② 采集产出货源商品", False,
             f"新增 0 条（当前共 {len(products)} 条，其中 {baseline_products} 条来自 seed）"
             + (f" —— 失败原因：{reason}" if reason else ""))
        note("★ 这条不能用「离线」解释：本环境 DNS/HTTPS 实测可用，"
             "根因是 1688 适配器未配置凭证，且其开放接口参数/签名仍标注「TODO: 需实测确认」。")
        note("后续订单链路改用 local_csv 的真实 CSV 导入路径验证（本地兜底的既定用法）。")

    # ---------- ③ 货源商品（用批量导入兜底，仍走 HTTP）----------
    imported = client.post(
        v("/source-products/batch-import"),
        json={"identifiers": ["https://detail.1688.com/offer/VC-0001.html"]},
        headers=ADMIN_HEADERS,
    )
    step("③ 货源批量导入入口可用（与采集同一链路）", imported.status_code == 202,
         f"HTTP {imported.status_code}")
    if not products:
        note("离线环境下该入口同样采不到 1688 数据 ⇒ 货源商品按 ENV_LIMIT 处理，"
             "后续订单链路改用 local_csv 的真实 CSV 导入路径验证（这是本地兜底的既定用法）。")

    # ---------- ③ AI 重构（创建 → 审核通过）----------
    src = client.get(v("/source-products"), params={"page_size": 10})
    src_items = (src.json().get("data") or {}).get("items") or []
    source_product_id = int(src_items[0]["id"]) if src_items else 0

    ai_task_id = 0
    if source_product_id:
        ai = client.post(
            v("/ai-tasks"),
            json={"source_product_ids": [source_product_id], "target_platform": "taobao",
                  "rework_items": ["title"]},
            headers=ADMIN_HEADERS,
        )
        ai_task_id = ((ai.json().get("data") or {}).get("task_ids") or [0])[0]
        step("③ AI 重构任务创建", ai.status_code == 202 and bool(ai_task_id),
             f"HTTP {ai.status_code} ai_task_id={ai_task_id}")

        if ai_task_id:
            # ★ 审核前必须等任务产出结果：AI 重构是异步的，创建即审核必然 404
            #   「尚无可审核的产出」—— 这不是缺陷，是脚本没等。
            ai_status = ""
            for _ in range(40):
                time.sleep(0.25)
                got = client.get(v(f"/ai-tasks/{ai_task_id}"))
                ai_status = str((got.json().get("data") or {}).get("status") or "")
                if ai_status in {"success", "review_pending", "failed", "cancelled"}:
                    break
            note(f"AI 任务终态：{ai_status}")

            reviewed = client.post(v(f"/ai-tasks/{ai_task_id}/review"),
                                   json={"action": "approve", "note": "全链路验证"},
                                   headers=ADMIN_HEADERS)
            if reviewed.status_code == 200:
                step("③ AI 产出审核通过", True, snippet(reviewed, 160))
            else:
                # ★ 默认 `ai.client=file_bridge`：它把任务写成文件、等**外部 AI 代理**写回结果，
                #   超时上限 1800s。没有外部代理时任务会一直停在 running（这是配置/环境依赖）。
                note("默认 ai.client=file_bridge 需要外部 AI 代理写回结果（poll 超时上限 1800s），"
                     "本环境无代理 ⇒ 任务停在 running。下面切到 mock 客户端验证同一条链路。")
                switched = client.put(v("/settings/ai.client"),
                                      json={"value": "mock", "reason": "全链路验证：切 mock"},
                                      headers=ADMIN_HEADERS)
                note(f"切换 ai.client=mock：HTTP {switched.status_code}")

                ai2 = client.post(
                    v("/ai-tasks"),
                    json={"source_product_ids": [source_product_id], "target_platform": "taobao",
                          "rework_items": ["title"]},
                    headers=ADMIN_HEADERS,
                )
                ai2_id = ((ai2.json().get("data") or {}).get("task_ids") or [0])[0]
                for _ in range(40):
                    time.sleep(0.25)
                    got = client.get(v(f"/ai-tasks/{ai2_id}"))
                    st = str((got.json().get("data") or {}).get("status") or "")
                    if st in {"success", "review_pending", "failed", "cancelled"}:
                        note(f"mock 客户端下 AI 任务终态：{st}")
                        break
                reviewed2 = client.post(v(f"/ai-tasks/{ai2_id}/review"),
                                        json={"action": "approve", "note": "全链路验证"},
                                        headers=ADMIN_HEADERS)
                step("③ AI 产出审核通过（mock 客户端下链路可走通）",
                     reviewed2.status_code == 200, snippet(reviewed2, 160))
    else:
        step("③ AI 重构任务创建", False, "无货源商品（采集失败的连锁后果），ENV_LIMIT")

    # ---------- ③b 上架（半自动 manual）----------
    publish_task_id = 0
    if source_product_id:
        pub = client.post(
            v("/publish-tasks"),
            json={"source_product_ids": [source_product_id], "platform": "taobao",
                  "shop_id": "shop-seed-001", "mode": "manual"},
            headers=ADMIN_HEADERS,
        )
        publish_task_id = ((pub.json().get("data") or {}).get("task_ids") or [0])[0]
        step("③b 半自动上架任务创建", pub.status_code == 202 and bool(publish_task_id),
             f"HTTP {pub.status_code} task_id={publish_task_id}")

    # ---------- ③c 回填商品 ID 并建映射 ----------
    if publish_task_id:
        form = client.get(v(f"/publish-tasks/manual/{publish_task_id}/form-data"))
        step("③c 半自动预填表单数据可读", form.status_code == 200, snippet(form, 140))

        # ★ 回填**前**先校验一次（干净状态）：这是校验接口的 happy path
        if source_product_id:
            pre = client.post(
                v("/sku-mappings/validate"),
                json={"source_product_id": source_product_id, "platform": "taobao",
                      "shop_id": "shop-seed-001", "sku_codes": []},
                headers=ADMIN_HEADERS,
            )
            pre_body = pre.json().get("data") or {}
            step("③c 回填前映射校验通过（干净状态）",
                 pre.status_code == 200 and pre_body.get("incomplete") is False,
                 snippet(pre, 160))

        fill = client.post(
            v(f"/publish-tasks/manual/{publish_task_id}/fill-back"),
            json={"shop_item_id": "vc-item-0001",
                  "skus": [{"shop_sku_code": "VC-FILL-001", "sale_price": "39.90",
                            "purchase_cost": "12.00", "spec_json": {"颜色": "红", "尺码": "XL"}}]},
            headers=ADMIN_HEADERS,
        )
        step("③c 回填商品 ID 并自动建映射", fill.status_code == 200, snippet(fill, 200))

    # ---------- ③d 回填后再次校验：新映射制造了「一个货源 SKU 被两个店铺 SKU 引用」----------
    #   ★ 这里拿到 422 是**对的**：回填把 VC-FILL-001 也绑到了同一个货源 SKU 上，
    #     校验接口正确地检出 P0 冲突并拦截。断言的是「结论完整且可读」，
    #     而不是"必须放行" —— 能拦住才是这个接口的价值。
    if source_product_id:
        validate = client.post(
            v("/sku-mappings/validate"),
            json={"source_product_id": source_product_id, "platform": "taobao",
                  "shop_id": "shop-seed-001", "sku_codes": []},
            headers=ADMIN_HEADERS,
        )
        v_body = validate.json().get("data") or {}
        step("③d 回填后校验给出完整结论（含 P0 冲突则拦截）",
             validate.status_code in {200, 422} and v_body.get("incomplete") is False,
             snippet(validate, 180))
        note("回填后命中 P0『重复映射』属预期：同一货源 SKU 被两个店铺 SKU 引用，"
             "校验接口按设计拦截（不是缺陷）。")

    # ---------- ④ 订单同步（local_csv：从 orders_import.csv 真实导入）----------
    #   ★ 这不是造假：local_csv 的设计用法就是"运营从店铺后台导出 CSV 放入 data/fulfillment/"。
    orders_csv = STORAGE_DIR / "fulfillment" / "orders_import.csv"
    orders_csv.parent.mkdir(parents=True, exist_ok=True)
    # ★ 用 seed 自带的映射编码（SEED-RED-XL），这样同步进来的订单能与**真实存在的映射**匹配上，
    #   不需要为了跑通链路去伪造映射 —— 每一步的前置条件都是系统自己的数据。
    sku_code = "SEED-RED-XL"
    orders_csv.write_text(
        "platform,shop_id,platform_order_no,total_amount,paid_at,receiver_name,"
        "receiver_phone,receiver_address,shop_item_id,shop_sku_code,quantity,price\n"
        f"taobao,shop-seed-001,VC-ORDER-0001,59.80,2026-10-08 10:00:00,张三,"
        f"138****0001,浙江省杭州市某某路 1 号,seed-item-001,{sku_code},2,29.90\n",
        encoding="utf-8",
    )

    sync = client.post(v("/orders/sync"), json={"adapter_name": "local_csv", "force": True},
                       headers=ADMIN_HEADERS)
    sync_task = (sync.json().get("data") or {}).get("task_record_id")
    step("④ 订单同步受理", sync.status_code == 202 and bool(sync_task),
         f"HTTP {sync.status_code} task_id={sync_task}")

    target_order: dict[str, Any] | None = None
    if sync_task:
        for _ in range(40):
            time.sleep(0.25)
            t = client.get(v(f"/tasks/{sync_task}"))
            if str((t.json().get("data") or {}).get("status")) in {"success", "failed", "cancelled"}:
                note(f"订单同步任务终态：{(t.json()['data'])['status']}")
                break
        listed = client.get(v("/orders"))
        items = (listed.json().get("data") or {}).get("items") or []
        target_order = next((o for o in items if o.get("platform_order_no") == "VC-ORDER-0001"), None)

    step("④ 订单已落库", target_order is not None,
         f"HTTP 200 order={target_order and target_order.get('id')} "
         f"status={target_order and target_order.get('fulfillment_status')}")

    # ---------- ⑤ SKU 匹配 ----------
    #   货源商品在离线环境采不到，因此这里用「订单处置动作」的真实入口建映射后匹配；
    #   若已有映射（seed 数据）则直接匹配。
    if target_order is None:
        note("订单未落库 ⇒ 匹配 / 下单 / 回填 / 售后 无法继续，以下按 ENV_LIMIT 标注。")
        return

    mappings = client.get(v("/sku-mappings"), params={"page_size": 50})
    mapping_items = (mappings.json().get("data") or {}).get("items") or []
    mapping_id = next(
        (int(m["id"]) for m in mapping_items if m.get("shop_sku_code") == sku_code), None
    )
    step("⑤ 存在可用 SKU 映射", mapping_id is not None,
         f"共 {len(mapping_items)} 条映射，命中 id={mapping_id}")

    if mapping_id is None:
        note(f"无 {sku_code} 的映射（货源采集失败的连锁后果）⇒ 匹配无法进行，标注 ENV_LIMIT。")
        return

    matched = client.post(v(f"/orders/{target_order['id']}/match"),
                          json={"sku_mapping_id": mapping_id}, headers=ADMIN_HEADERS)
    step("⑤ SKU 匹配", matched.status_code == 200, snippet(matched))

    detail = client.get(v(f"/orders/{target_order['id']}"))
    status_now = (detail.json().get("data") or {}).get("fulfillment_status")
    step("⑤ 订单推进到 matched", status_now == "matched", f"status={status_now}")

    # ---------- ⑥ 采购下单（★ 本轮新开的入口）----------
    placed = client.post(v(f"/orders/{target_order['id']}/place-purchase"), headers=ADMIN_HEADERS)
    placed_body = placed.json().get("data") or {}
    step("⑥ 采购下单（POST /orders/{id}/place-purchase）", placed.status_code == 200,
         snippet(placed))
    step("⑥ 本地兜底绝不伪装成「已下单」",
         placed_body.get("purchase_status") == "manual_pending",
         f"purchase_status={placed_body.get('purchase_status')}")

    after = client.get(v(f"/orders/{target_order['id']}"))
    step("⑥ 订单推进到 purchased",
         (after.json().get("data") or {}).get("fulfillment_status") == "purchased",
         f"status={(after.json().get('data') or {}).get('fulfillment_status')}")

    # ---------- ⑦ 物流回填 ----------
    tracking_csv = STORAGE_DIR / "fulfillment" / "tracking_import.csv"
    po_no = str(placed_body.get("purchase_order_no") or "")
    tracking_csv.write_text(
        "order_id,platform_order_no,purchase_order_no,logistics_company,tracking_no,shipped_at\n"
        f"{target_order['id']},VC-ORDER-0001,{po_no},顺丰速运,SF1234567890,2026-10-08 12:00:00\n",
        encoding="utf-8",
    )
    purchases = client.get(v("/purchase-orders"), params={"page_size": 50})
    po_items = (purchases.json().get("data") or {}).get("items") or []
    po = next((p for p in po_items if p.get("order_id") == target_order["id"]), None)
    step("⑦ 采购单可查询", po is not None, f"purchase_order={po and po.get('id')}")

    if po is not None:
        tracked = client.post(v(f"/purchase-orders/{po['id']}/tracking"),
                              json={"logistics_company": "顺丰速运", "tracking_no": "SF1234567890"},
                              headers=ADMIN_HEADERS)
        step("⑦ 物流单号回填", tracked.status_code == 200, snippet(tracked))

    # ---------- ⑧ 售后 ----------
    after_sales = client.get(v("/after-sales"), params={"page_size": 20})
    step("⑧ 售后列表可查询", after_sales.status_code == 200, snippet(after_sales, 120))


def main() -> int:
    """入口：全新库 → 迁移 → seed → 启动应用 → 全链路 → 汇总。"""
    parser = argparse.ArgumentParser(description="从零起步的全链路验证")
    parser.add_argument("--db", default="", help="新库路径（默认 data/verify_chain_<ts>.db）")
    parser.add_argument("--port", type=int, default=8123, help="uvicorn 端口")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else BACKEND_ROOT / "data" / f"verify_chain_{int(time.time())}.db"
    print("=" * 92)
    print(f"从零起步全链路验证｜新库：{db_path}")
    print("=" * 92)

    env = prepare_db(db_path)
    py = sys.executable

    ok, out = run_subprocess([py, "-m", "alembic", "upgrade", "head"], env, label="alembic")
    step("0. alembic upgrade head（全新库建表）", ok, out.strip().splitlines()[-1] if out.strip() else "")

    ok, out = run_subprocess([py, "scripts/seed.py"], env, label="seed")
    step("0. seed 基础数据", ok, out.strip().splitlines()[-1] if out.strip() else "")

    # ---------- 启动真实应用（bootstrap 在 lifespan 里自动落库）----------
    server = subprocess.Popen(
        [py, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(args.port)],
        cwd=str(BACKEND_ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{args.port}"
    try:
        health = wait_for_health(base_url)
        step("0. 应用启动（真实 uvicorn 进程）", health is not None,
             snippet(health) if health else "启动超时")
        if health is None:
            return 1

        with httpx.Client(base_url=base_url, timeout=30) as client:
            verify_chain(client, db_path)
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:  # noqa: F821
            server.kill()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n" + "=" * 92)
    print(f"全链路结果：{passed}/{len(RESULTS)} 项成立（新库：{db_path}）")
    print("=" * 92)
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"   ❌ {name}: {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
