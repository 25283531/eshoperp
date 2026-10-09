"""★ Bug 3 / Bug 4 端到端取证脚本（team-lead 指派的两个 P0）。

    python scripts/verify_bug3_bug4.py

进程内直连（ASGI transport + 独立验证库），不需要先起服务，可重复执行。

================================================================================
★★ 取证纪律：全部前置条件**只走真实 HTTP 接口**，不绕过应用层直连 DB ★★
================================================================================
早期版本为了"制造越权"，直接 UPDATE `fulfillment_adapter.declared_scopes_json`。
That是作弊：它绕过了 `PUT /adapters/fulfillment/{name}/config` 本身的校验，
等于假设"越权配置已经落库"，而真实世界里这个接口**第一步就该把它挡在门外**。
本脚本现在全部用真实请求构造场景：
    * 越权 = `PUT config` 声明黑名单 scope → 期望 **403 / 5003**；
    * 越权 = `POST /platform-accounts/{id}/authorize` 索要黑名单 scope → 期望 **403 / 5003**；
    * 基线 / 复位 = `PUT config` 声明白名单 scope → 期望 **200**。
只有"独立验证库的建表与基础数据播种"用 DB（等价于启动期 `bootstrap_all()`，
因为 ASGITransport 不触发 lifespan），其余一律走 HTTP。

**Bug 3（履约链路全断）**
    `FulfillmentAdapter.invoke()` 曾对字符串能力名抛 AttributeError（str 没有 .value），
    订单同步任务 3 次重试后 failed。本脚本：`POST /orders/sync` → 任务从 pending 走到 success。

**Bug 4（★ 红线 R1 静默失效：越权被拒但审计 0 条）**
    取证六项：
        ① 声明越权 scope 被拒（403 / 5003）+ `permission_change` 审计 +1；
        ② `GET /system/status-bar` 的 `unhandled_violation_count` +1；
        ③ 处置后该计数回落；
        ④ 「被拒」与「被处置」成对存在（附录 A 第 16 条）；
        ⑤ 合法 scope 校验通过不得污染红点计数（`is_handled` 语义）；
        ⑥ 红线 R1 的**第三个调用点**（平台账号授权）同样越权必拒 + 必留痕。

★ 退出码：全部成立 0，任一不成立 1。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

import httpx  # noqa: E402

API_PREFIX = "/api/v1"
ADMIN_HEADERS = {"X-Operator": "verifier", "X-Operator-Token": "admin-token"}
TARGET_ADAPTER = "local_csv"

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    """记录一条断言并即时打印。"""
    CHECKS.append((name, bool(ok), detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))
    return bool(ok)


async def _get(client: httpx.AsyncClient, path: str, **kwargs: Any) -> httpx.Response:
    """GET 请求。"""
    return await client.get(f"{API_PREFIX}{path}", **kwargs)


async def _post(client: httpx.AsyncClient, path: str, **kwargs: Any) -> httpx.Response:
    """POST 请求。"""
    return await client.post(f"{API_PREFIX}{path}", **kwargs)


async def _put(client: httpx.AsyncClient, path: str, **kwargs: Any) -> httpx.Response:
    """PUT 请求。"""
    return await client.put(f"{API_PREFIX}{path}", **kwargs)


async def _data(response: httpx.Response) -> dict[str, Any]:
    """解统一响应体的 `data`（对象形态）。"""
    body = response.json()
    payload = body.get("data")
    return dict(payload) if isinstance(payload, dict) else {}


async def _rows(response: httpx.Response) -> list[dict[str, Any]]:
    """解统一响应体的 `data`（列表形态，如 `/platform-accounts`）。

    说明：本项目部分列表接口直接返回数组而非 `{items, total}`，
    这里统一成 `list[dict]` 以便调用方无需关心形态差异。
    """
    body = response.json()
    payload = body.get("data")
    if isinstance(payload, list):
        return [dict(row) for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        return [dict(row) for row in (payload.get("items") or []) if isinstance(row, dict)]
    return []


async def _permission_change_total(client: httpx.AsyncClient) -> int:
    """当前 `permission_change` 审计总数（真实 HTTP 查询）。"""
    logs = await _data(await _get(client, "/audit-logs?action_type=permission_change&page_size=1"))
    return int(logs.get("total") or 0)


async def _unhandled_count(client: httpx.AsyncClient) -> tuple[int, dict[str, Any]]:
    """顶栏未处置越权计数 + 最近一条越权（真实 HTTP 查询）。"""
    bar = await _data(await _get(client, "/system/status-bar"))
    return int(bar.get("unhandled_violation_count") or 0), dict(bar.get("latest_violation") or {})


async def _handle_first_violation(client: httpx.AsyncClient, note: str) -> tuple[bool, str]:
    """处置第一条未处置越权（真实 HTTP），返回 `(是否处置成功, 说明)`。"""
    violations = await _data(await _get(client, "/adapters/violations?is_unhandled=true&page_size=5"))
    items = list(violations.get("items") or [])
    if not items:
        return False, "没有未处置的越权记录"
    violation_id = int(items[0]["id"])
    resp = await _post(
        client,
        f"/adapters/violations/{violation_id}/handle",
        json={"handle_note": note},
        headers=ADMIN_HEADERS,
    )
    handled = await _data(resp)
    ok = resp.status_code == 200 and handled.get("is_handled") is True
    return ok, f"HTTP {resp.status_code} handled_by={handled.get('handled_by')}"


# ======================================================================
#  Bug 4：越权审计闭环（全部走真实 API）
# ======================================================================


async def verify_bug4(client: httpx.AsyncClient) -> None:
    """越权 → 审计 +1 → 顶栏红点 +1 → 处置后回落 → 成对留痕 → 通过不污染。"""
    print("\n" + "=" * 92)
    print("Bug 4  红线 R1 闭环：越权被拒必须落审计（此前审计 0 条 = 安全功能静默失效）")
    print("=" * 92)

    policies = await _data(await _get(client, "/adapters/scope-policies"))
    forbidden = list(policies.get("forbidden") or [])
    allowed = list(policies.get("allowed") or [])
    if not forbidden or not allowed:
        check("scope 白/黑名单非空", False, f"allowed={allowed} forbidden={forbidden}")
        return

    # ---------- 0. 适配器行必须真实落库（QA-05：否则 PUT config 恒定 404）----------
    listed = await _data(await _get(client, "/adapters/fulfillment"))
    names = [str(a.get("adapter_name")) for a in list(listed.get("adapters") or [])]
    check("0. 履约适配器清单非空（启动期 bootstrap 已落库）", bool(names), f"adapters={names}")
    if TARGET_ADAPTER not in names:
        check(f"0. 目标适配器 {TARGET_ADAPTER} 在清单内", False, f"实际：{names}")
        return

    # ---------- 基线：合法 scope 配置成功 ----------
    base_resp = await _put(
        client,
        f"/adapters/fulfillment/{TARGET_ADAPTER}/config",
        json={"declared_scopes": allowed, "is_enabled": True},
        headers=ADMIN_HEADERS,
    )
    check("基线：合法 scope 的 PUT config 受理（200）", base_resp.status_code == 200,
          f"HTTP {base_resp.status_code} {base_resp.text[:100]}")

    base_count, _ = await _unhandled_count(client)
    base_total = await _permission_change_total(client)
    print(f"  基线：顶栏红点 = {base_count}，permission_change 审计总数 = {base_total}")

    # ---------- ① 声明越权 scope → 403 / 5003 ----------
    violation_resp = await _put(
        client,
        f"/adapters/fulfillment/{TARGET_ADAPTER}/config",
        json={"declared_scopes": [allowed[0], forbidden[0]], "is_enabled": True},
        headers=ADMIN_HEADERS,
    )
    body = violation_resp.json()
    check(
        "① 声明越权 scope 被拒（403 / 5003）",
        violation_resp.status_code == 403 and int(body.get("code") or 0) == 5003,
        f"HTTP {violation_resp.status_code} code={body.get('code')} msg={str(body.get('message'))[:70]}",
    )

    after_total = await _permission_change_total(client)
    check(
        "① 越权后 permission_change 审计 +1（★ 此前恒为 0）",
        after_total >= base_total + 1,
        f"{base_total} → {after_total}",
    )

    # ---------- ② 顶栏红点 +1 ----------
    alert_count, latest = await _unhandled_count(client)
    check("② 顶栏 unhandled_violation_count +1", alert_count == base_count + 1,
          f"{base_count} → {alert_count}")
    check(
        "② 顶栏能给出最近一条越权（适配器名 + 被拒 scope）",
        bool(latest.get("adapter_name")) and bool(latest.get("denied_scopes")),
        f"adapter={latest.get('adapter_name')} denied={latest.get('denied_scopes')}",
    )

    # ---------- ③ 处置后回落 ----------
    ok, detail = await _handle_first_violation(client, "取证脚本：已回收越权 scope")
    check("③ 管理员处置成功（is_handled=true）", ok, detail)
    settled_count, _ = await _unhandled_count(client)
    check("③ 处置后顶栏计数回落（红点熄灭）", settled_count == base_count,
          f"{alert_count} → {settled_count}（基线 {base_count}）")

    # ---------- ④ 成对留痕 ----------
    all_logs = await _data(await _get(client, "/audit-logs?action_type=permission_change&page_size=10"))
    rows = list(all_logs.get("items") or [])
    rejected = [r for r in rows if "rejected" in f"{r.get('new_value')}{r.get('remark')}"]
    handled_rows = [r for r in rows if "handled" in f"{r.get('new_value')}{r.get('remark')}"]
    check(
        "④ 「被拒」与「被处置」成对存在（附录 A 第 16 条）",
        bool(rejected) and bool(handled_rows),
        f"被拒 {len(rejected)} 条 / 被处置 {len(handled_rows)} 条（近 10 条内）",
    )

    # ---------- ⑤ 通过记录不污染红点 ----------
    pass_resp = await _put(
        client,
        f"/adapters/fulfillment/{TARGET_ADAPTER}/config",
        json={"declared_scopes": allowed, "is_enabled": True},
        headers=ADMIN_HEADERS,
    )
    pass_count, _ = await _unhandled_count(client)
    check(
        "⑤ 校验通过后红点计数不变（is_handled 语义正确）",
        pass_resp.status_code == 200 and pass_count == base_count,
        f"HTTP {pass_resp.status_code}，计数 {pass_count}（基线 {base_count}）",
    )

    # ---------- ⑥ 红线 R1 第三个调用点：平台账号授权 ----------
    items = await _rows(await _get(client, "/platform-accounts"))
    if not items:
        check("⑥ 平台账号授权越权被拒", False, "平台账号列表为空（bootstrap 未落库）")
        return
    account_id = int(items[0]["id"])
    before_total = await _permission_change_total(client)
    before_count, _ = await _unhandled_count(client)

    auth_resp = await _post(
        client,
        f"/platform-accounts/{account_id}/authorize",
        json={"granted_scopes": [allowed[0], forbidden[0]]},
        headers=ADMIN_HEADERS,
    )
    auth_body = auth_resp.json()
    check(
        "⑥ 平台账号索要越权 scope 被拒（403 / 5003）",
        auth_resp.status_code == 403 and int(auth_body.get("code") or 0) == 5003,
        f"HTTP {auth_resp.status_code} code={auth_body.get('code')} msg={str(auth_body.get('message'))[:70]}",
    )
    auth_total = await _permission_change_total(client)
    auth_count, _ = await _unhandled_count(client)
    check("⑥ 授权越权同样落审计 + 亮红点", auth_total >= before_total + 1 and auth_count == before_count + 1,
          f"审计 {before_total} → {auth_total}，红点 {before_count} → {auth_count}")

    ok, detail = await _handle_first_violation(client, "取证脚本：已回收越权授权")
    check("⑥ 处置后红点回落", ok, detail)

    # 合法授权复原（避免把演示账号留在 rejected 态）
    await _post(
        client,
        f"/platform-accounts/{account_id}/authorize",
        json={"granted_scopes": allowed},
        headers=ADMIN_HEADERS,
    )


# ======================================================================
#  Bug 3：履约链路
# ======================================================================


async def verify_bug3(client: httpx.AsyncClient) -> None:
    """`POST /orders/sync` → 任务 pending → success（此前 AttributeError → failed 且重试耗尽）。"""
    print("\n" + "=" * 92)
    print("Bug 3  履约链路：订单同步任务必须跑到 success（此前 AttributeError → failed）")
    print("=" * 92)

    resp = await _post(client, "/orders/sync", json={"adapter_name": TARGET_ADAPTER, "force": True})
    body = await _data(resp)
    task_id = body.get("task_record_id")
    check("POST /orders/sync 受理（202 + task_record_id）", resp.status_code == 202 and bool(task_id),
          f"HTTP {resp.status_code} task_id={task_id}")
    if not task_id:
        return

    status = ""
    detail = ""
    for _ in range(40):
        await asyncio.sleep(0.5)
        task = await _data(await _get(client, f"/tasks/{task_id}"))
        status = str(task.get("status") or "")
        detail = str(task.get("error_message") or task.get("message") or "")[:160]
        if status in {"success", "failed", "cancelled"}:
            break

    check(
        "订单同步任务走到 success（★ 此前 invoke(str) 抛 AttributeError）",
        status == "success",
        f"status={status} {detail}",
    )
    check(
        "任务错误信息不得再出现 'has no attribute'",
        "has no attribute" not in detail,
        detail or "（无错误信息）",
    )


def _prepare_isolated_db() -> None:
    """准备一个**独立**的验证库（`data/verify_erp.db`）。

    ★ 为什么不用开发库：开发库可能被正在运行的 uvicorn（前端 / QA 联调用）持有写锁，
      取证脚本并发写入会撞 `database is locked`，导致结论不稳定。
      独立库每次重建，取证结果可重复、也不污染联调数据。

    ★ 只在这里用 DB：建表 + `bootstrap_all()`（等价于应用 lifespan 的启动期初始化，
      因为 httpx 的 `ASGITransport` **不触发 lifespan**）。
      除此之外，本脚本的所有场景构造一律走真实 HTTP 接口。
    """
    import os

    if not os.environ.get("DATABASE_URL"):
        db_path = BACKEND_ROOT / "data" / "verify_erp.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        if db_path.exists():
            db_path.unlink()
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    os.environ.setdefault("APP_ENV", "test")
    os.environ.setdefault("LOG_JSON", "false")
    os.environ.setdefault("SCHEDULER_ENABLED", "false")
    os.environ.setdefault("TASK_RECOVERY_ENABLED", "false")

    from app.core.database import get_engine
    from app.models import Base  # noqa: F401  导入即注册全部表

    async def _create() -> None:
        from sqlalchemy import select

        from app.core.database import get_session_factory
        from app.models.system import DEFAULT_SETTINGS, SystemSetting

        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with get_session_factory()() as session:
            for item in DEFAULT_SETTINGS:
                exists = (
                    await session.execute(
                        select(SystemSetting.id).where(SystemSetting.setting_key == item["key"])
                    )
                ).scalar_one_or_none()
                if exists is None:
                    session.add(
                        SystemSetting(
                            setting_key=item["key"],
                            setting_value=item["value"],
                            value_type=item["value_type"],
                            description=item.get("description"),
                        )
                    )
            await session.commit()

        # ★ 与 `app/main.py` lifespan 完全同一套初始化
        async with get_session_factory()() as boot_session:
            from app.services.bootstrap import bootstrap_all, check_required_constraints

            stats = await bootstrap_all(boot_session, commit=True)
            issues = await check_required_constraints(boot_session)
            print(f"  · 启动期初始化：{stats}")
            if issues:
                print(f"  · ❌ 结构性约束缺失：{[i['name'] for i in issues]}")
            else:
                print("  · ✅ 结构性约束自检通过")

    asyncio.run(_create())


async def main() -> int:
    """主流程。"""
    from app.core.database import dispose_engine
    from app.main import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://verify") as client:
        await verify_bug4(client)
        await verify_bug3(client)
    await dispose_engine()

    passed = sum(1 for _n, ok, _d in CHECKS if ok)
    total = len(CHECKS)
    print("\n" + "-" * 92)
    print(f"{'✅' if passed == total else '❌'} 取证结果：{passed}/{total} 项成立")
    return 0 if passed == total else 1


if __name__ == "__main__":
    _prepare_isolated_db()
    raise SystemExit(asyncio.run(main()))
