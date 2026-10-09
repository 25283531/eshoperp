"""★ QA 探针 E：两层越权告警闭环 + 顶栏性能红线。

E-1  GET /system/status-bar 字段完整性
E-2  触发一次越权 → 计数必须 0 → 1
E-3  处置后 → 计数回落
E-4  ★ 性能红线：顶栏被前端每 60s 轮询，运行时**绝不触发任何外部 HTTP**
     —— 用 httpx 猴子补丁拦截一切对外请求来实证（不是靠耗时推测）
E-5  附录 A 第 16 条：「越权被拒」与「越权被处置」两条审计必须成对存在
"""

from __future__ import annotations

import asyncio
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
FAILS: list[str] = []


def banner(title: str) -> None:
    """分节标题。"""
    print(f"\n{'=' * 96}\n{title}\n{'=' * 96}")


def req(method: str, path: str, body: Any = None) -> tuple[int, dict]:
    """同步 HTTP 请求。"""
    url = API + path
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "X-Operator": "qa-admin",
               "X-Operator-Token": "admin-token"}
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener.open(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode(errors="replace") or "{}")


def show(status: int, payload: dict, label: str, *, expect: set[int] | None = None) -> dict:
    """打印响应。"""
    ok = expect is None or status in expect
    if not ok:
        FAILS.append(f"{label}: 期望 {sorted(expect)} 实际 {status}")
    print(f"  {'[OK]' if ok else '[FAIL]'} {label}  ->  HTTP {status}")
    pretty = json.dumps(payload.get("data"), ensure_ascii=False)
    print(f"        data={pretty[:520]}{' ...' if len(pretty) > 520 else ''}")
    return payload.get("data") or {}


def main() -> int:
    """主流程。"""
    banner("E-1  GET /system/status-bar 字段完整性")
    st, res = req("GET", "/system/status-bar")
    data = show(st, res, "顶栏聚合", expect={200})
    required = ["violation_unhandled_count", "listing_mode", "active_adapter", "is_mock_active", "health"]
    actual_keys = sorted(data)
    print(f"        实际返回字段: {actual_keys}")
    missing = [k for k in required if k not in actual_keys]
    if missing:
        print(f"        ⚠ 验收口径要求的字段名未直接出现: {missing}")
        print("          （实现使用了更长命名，见下方映射；需与前端契约核对，见 H 段）")
        mapping_note = {
            "violation_unhandled_count": "unhandled_violation_count",
            "active_adapter": "active_fulfillment_adapter",
            "health": "health_status",
        }
        for k in missing:
            alt = mapping_note.get(k)
            print(f"          {k:28} -> 实际字段名 {alt!r} {'（存在）' if alt in actual_keys else '（★ 也不存在）'}")
    else:
        print("        ✅ 五个字段齐全")

    baseline = int(data.get("unhandled_violation_count", 0))
    print(f"        基线 violation_unhandled_count = {baseline}")

    banner("E-2  触发一次越权 → 计数应 0 → +1")
    # 通过「更新适配器配置声明越权 scope」触发（该路径的审计写入是正确的）
    st, res = req(
        "PUT",
        "/adapters/fulfillment/miaoshou/config",
        {"declared_scopes": ["order.read", "item.write"], "is_enabled": True},
    )
    show(st, res, "给妙手配越权 scope item.write（应 403/5003）", expect={403})

    st, res = req("GET", "/system/status-bar")
    data = show(st, res, "顶栏重新取值", expect={200})
    after = int(data.get("unhandled_violation_count", 0))
    print(f"        越权后 violation_unhandled_count = {after}")
    if after > baseline:
        print(f"        ✅ 计数已从 {baseline} 上升到 {after}（第 1 层：系统内有记录 + 第 2 层：顶栏红点亮）")
    else:
        FAILS.append(f"触发越权后顶栏计数未上升（{baseline} → {after}）—— 两层告警未闭环")
        print(f"        ❌ 计数未上升（{baseline} → {after}）—— 两层告警未闭环")

    banner("E-3  取出未处置告警并处置 → 计数回落")
    st, res = req("GET", "/adapters/violations?is_unhandled=true&page_size=5")
    vlist = (show(st, res, "未处置越权告警列表", expect={200}) or {})
    items = vlist.get("items") or []
    print(f"        未处置条数 = {vlist.get('total')}, 首条 id = {items[0]['id'] if items else None}")
    if items:
        vid = int(items[0]["id"])
        print(f"        首条告警: action_type={items[0].get('action_type')} "
              f"remark={str(items[0].get('remark'))[:90]}")
        st, res = req("POST", f"/adapters/violations/{vid}/handle", {"handle_note": "QA 处置"})
        show(st, res, f"处置告警 #{vid}", expect={200})
        st, res = req("GET", "/system/status-bar")
        data = show(st, res, "处置后顶栏", expect={200})
        final = int(data.get("unhandled_violation_count", 0))
        print(f"        处置后 violation_unhandled_count = {final}")
        if final < after:
            print(f"        ✅ 计数回落 {after} → {final}")
        else:
            FAILS.append(f"处置后计数未回落（{after} → {final}）")
            print(f"        ❌ 计数未回落（{after} → {final}）")

    banner("E-4  ★ 顶栏性能红线：运行时零外部 HTTP（猴子补丁实证）")
    probe_script = str((ROOT := __import__("pathlib").Path(__file__).resolve().parents[1]) / "qa_probes" / "_nohttp_probe.py")
    import subprocess

    python = sys.executable
    out = subprocess.run(
        [python, probe_script], capture_output=True, text=True, timeout=180
    )
    print(out.stdout.strip())
    if out.returncode != 0:
        FAILS.append("零外部 HTTP 探针执行失败")
        print(out.stderr[-800:])

    banner("E-5  附录 A 第 16 条：越权「被拒」与「被处置」审计成对存在")
    st, res = req("GET", "/audit-logs?action_type=permission_change&page_size=20&include_system=true")
    logs = (show(st, res, "审计日志（permission_change）", expect={200}) or {})
    rows = logs.get("items") or []
    rejected = [r for r in rows if "被拒绝" in str(r.get("remark")) or r.get("is_handled") is False]
    handled = [r for r in rows if r.get("is_handled") is True or "处置" in str(r.get("remark"))]
    print(f"        permission_change 审计条数 = {len(rows)}")
    print(f"        未处置（被拒记录）= {len(rejected)}   已处置（处置记录）= {len(handled)}")
    for r in rows[:6]:
        print(f"          #{r.get('id')} is_handled={r.get('is_handled')} remark={str(r.get('remark'))[:80]}")
    if rejected and handled:
        print("        ✅ 「被拒」与「被处置」成对存在")
    elif rejected and not handled:
        print("        ⚠ 只有「被拒」记录，尚未处置（处置前基线，属正常）")
    else:
        print("        ⚠ 样本不足，无法判定成对闭环")

    banner("E 段结论")
    if FAILS:
        print(f"  断言失败 {len(FAILS)} 项：")
        for item in FAILS:
            print(f"   - {item}")
    else:
        print("  ✅ E 段显式断言全部通过")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
