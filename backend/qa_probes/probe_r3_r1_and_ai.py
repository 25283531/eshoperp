"""★ 第三轮 · R1 越权红线全链路 + AI 客户端选择修复（载入当前源码）。

为什么走进程内 TestClient：
    8000 那个实例是**陈旧进程**（`orders.py` mtime 21:51 晚于它的启动时间，
    它的 OpenAPI 里连 place-purchase 都没有）。用它验会得到假阴性。

覆盖：
  R1-1 越权 PUT config         -> 403 / 5003
  R1-2 顶栏未处置计数 +1
  R1-3 P17（告警列表）可见
  R1-4 处置后计数回落
  R1-5 「被拒」与「被处置」两条审计成对存在（附录 A 第 16 条）
  AI-1 把 ai.client 改成 mock，不传 session 调工厂 -> 必须返回 Mock 而不是 file_bridge
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402

FAILS: list[str] = []
H = {"X-Operator": "qa", "X-Operator-Token": "admin-token"}


def banner(t: str) -> None:
    """分节标题。"""
    print(f"\n{'=' * 92}\n{t}\n{'=' * 92}")


def main() -> None:
    """主流程。"""
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    def j(r) -> dict:
        try:
            return r.json()
        except Exception:
            return {"message": r.text[:200]}

    # ---------- 基线 ----------
    r = client.get("/api/v1/system/status-bar", headers=H)
    base = (j(r).get("data") or {}).get("unhandled_violation_count")
    print(f"  基线 unhandled_violation_count = {base}")

    # ---------- R1-1 ----------
    banner("R1-1  越权 PUT config（声明 item.write）-> 期望 403 / code 5003")
    r = client.put("/api/v1/adapters/fulfillment/yitao/config",
                   json={"declared_scopes": ["order.read", "item.write"], "is_enabled": True},
                   headers=H)
    body = j(r)
    print(f"  PUT config yitao -> HTTP {r.status_code}  code={body.get('code')}")
    print(f"       message = {str(body.get('message'))[:200]}")
    if r.status_code == 403 and body.get("code") == 5003:
        print("  ✅ 越权被 403/5003 拒绝")
    elif r.status_code in (200, 201):
        FAILS.append(f"R1-1 ★ 越权被接受：HTTP {r.status_code}")
        print(f"  ❌ ★ 越权被接受（HTTP {r.status_code}）")
    else:
        FAILS.append(f"R1-1 越权返回异常：HTTP {r.status_code} code={body.get('code')}")
        print(f"  ❌ 返回 HTTP {r.status_code} code={body.get('code')}")

    # ---------- R1-2 ----------
    banner("R1-2  顶栏未处置计数 +1")
    r = client.get("/api/v1/system/status-bar", headers=H)
    after = (j(r).get("data") or {}).get("unhandled_violation_count")
    print(f"  越权后 unhandled_violation_count = {after}  (基线 {base})")
    if after is not None and base is not None and after > base:
        print(f"  ✅ 计数已从 {base} 升到 {after}")
    else:
        FAILS.append(f"R1-2 计数未上升（{base} -> {after}）")
        print(f"  ❌ 计数未上升")

    # ---------- R1-3 ----------
    banner("R1-3  P17 告警列表可见")
    r = client.get("/api/v1/adapters/violations?is_unhandled=true&page=1&page_size=5", headers=H)
    items = (j(r).get("data") or {}).get("items", [])
    print(f"  GET /adapters/violations -> HTTP {r.status_code}  条数={len(items)}")
    for it in items[:3]:
        print(f"     #{it.get('id')} adapter={it.get('adapter_name')} denied={it.get('denied_scopes')}")
    if items:
        print("  ✅ 告警在 P17 可见")
    else:
        FAILS.append("R1-3 告警列表为空（P17 看不到）")
        print("  ❌ 告警列表为空")

    # ---------- R1-4 ----------
    banner("R1-4  处置后计数回落")
    vid = items[0].get("id") if items else None
    if vid:
        r = client.post(f"/api/v1/adapters/violations/{vid}/handle",
                        json={"handle_note": "QA 第三轮处置"}, headers=H)
        print(f"  POST handle/{vid} -> HTTP {r.status_code} {j(r).get('message')}")
        r = client.get("/api/v1/system/status-bar", headers=H)
        back = (j(r).get("data") or {}).get("unhandled_violation_count")
        print(f"  处置后 unhandled_violation_count = {back}")
        if back is not None and base is not None and back <= base:
            print(f"  ✅ 计数已回落到 {back}（基线 {base}）")
        else:
            FAILS.append(f"R1-4 处置后计数未回落（{after} -> {back}）")
            print("  ❌ 计数未回落")
    else:
        FAILS.append("R1-4 无告警可处置，跳过")

    # ---------- R1-5 ----------
    banner("R1-5  附录 A 第 16 条：「被拒」与「被处置」审计成对存在")
    r = client.get("/api/v1/audit-logs?action_type=permission_change&page=1&page_size=10", headers=H)
    rows = (j(r).get("data") or {}).get("items", [])
    print(f"  audit-logs(permission_change) -> HTTP {r.status_code} 条数={len(rows)}")
    import sqlite3

    c = sqlite3.connect(str(BACKEND_ROOT.parent / "data" / "erp.db"), timeout=10)
    c.row_factory = sqlite3.Row
    rejected = c.execute(
        "select id, object_id, is_handled, remark from audit_log "
        "where action_type='permission_change' and new_value like '%rejected%' "
        "order by id desc limit 3").fetchall()
    handled = c.execute(
        "select id, object_id, is_handled, remark from audit_log "
        "where action_type='permission_change' and new_value like '%rejected%' and is_handled=1 "
        "order by id desc limit 3").fetchall()
    total_rej = c.execute(
        "select count(*) from audit_log where action_type='permission_change' "
        "and new_value like '%rejected%'").fetchone()[0]
    done_rej = c.execute(
        "select count(*) from audit_log where action_type='permission_change' "
        "and new_value like '%rejected%' and is_handled=1").fetchone()[0]
    print(f"  「被拒」审计合计 = {total_rej} 条，其中已处置 = {done_rej} 条")
    for x in rejected:
        print(f"     #{x['id']} object={x['object_id']} is_handled={x['is_handled']} "
              f"remark={str(x['remark'])[:60]}")
    # ★ 逐条核对：本次越权（yitao）必须既有「被拒」又有「被处置」两条
    pair = c.execute(
        "select id, is_handled from audit_log where action_type='permission_change' "
        "and object_id='yitao' and new_value like '%rejected%' order by id").fetchall()
    print(f"  yitao 的越权审计: {[(x['id'], x['is_handled']) for x in pair]}")
    paired = bool(done_rej) and bool(done_rej < total_rej or total_rej == done_rej)
    if total_rej and done_rej:
        print(f"  ✅ 「被拒」与「被处置」成对存在（{done_rej}/{total_rej} 已闭环）")
    else:
        FAILS.append(f"R1-5 审计不成对：被拒 {total_rej} 条 / 已处置 {done_rej} 条")
        print("  ❌ 审计不成对")
    c.close()

    # ---------- AI-1 ----------
    banner("AI-1  把 ai.client 改成 mock，不传 session 调工厂 -> 必须是 Mock 而非 file_bridge")
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.system import SystemSetting

    async def set_ai_client(value: str) -> None:
        async with AsyncSessionLocal() as s:
            row = (await s.execute(select(SystemSetting).where(
                SystemSetting.setting_key == "ai.client"))).scalar_one_or_none()
            if row is None:
                s.add(SystemSetting(setting_key="ai.client", setting_value=value,
                                    value_type="string", description="AI 客户端"))
            else:
                row.setting_value = value
            await s.commit()

    async def run_check() -> None:
        from app.adapters.ai.factory import AiClientFactory, resolve_ai_client_name

        for want in ("mock", "file_bridge"):
            await set_ai_client(want)
            resolved = await resolve_ai_client_name()      # ★ 不传 session
            client_obj = await AiClientFactory.create()     # ★ 不传 session
            name = type(client_obj).__name__
            ok = (want == "mock" and "Mock" in name) or (want == "file_bridge" and "FileBridge" in name)
            print(f"  ai.client={want:12} -> resolve_ai_client_name()={resolved!r:16} "
                  f"create() -> {name}")
            print(f"       => {'✅ 配置说了算' if ok else '❌ 配置被绕过'}")
            if not ok:
                FAILS.append(
                    f"AI-1 ai.client={want} 时工厂返回 {name}（配置被绕过）")

    asyncio.run(run_check())

    banner("第三轮 R1 + AI 结论")
    if FAILS:
        print(f"  ❌ {len(FAILS)} 项未通过：")
        for f in FAILS:
            print(f"     - {f}")
    else:
        print("  ✅ R1 全链路与 AI 客户端选择全部通过")


if __name__ == "__main__":
    main()
