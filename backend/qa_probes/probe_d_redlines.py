"""★ QA 探针 D：三条业务红线的攻击性验证（站攻击者视角）。

R1 权限最小化：能否绕过工厂直接实例化第三方适配器并调用？
R2 第三方不直写店铺：FulfillmentAdapter 子类有无 offline / update_stock_price / import listing？
R3 能力降级不抛异常：不配 base_url（模拟未开通）时逐能力调用必须返回信封而非抛异常。
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + (ROOT / "data" / "erp.db").as_posix()
os.environ["APP_ENV"] = "test"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["TASK_RECOVERY_ENABLED"] = "false"
os.environ["LOG_JSON"] = "false"

sys.path.insert(0, str(ROOT / "backend"))

SEP = "=" * 96
FINDINGS: list[str] = []


def banner(title: str) -> None:
    """分节标题。"""
    print(f"\n{SEP}\n{title}\n{SEP}")


# =====================================================================
#  R1
# =====================================================================
async def r1() -> None:
    """R1 权限最小化攻击。"""
    banner("D-R1  权限最小化")
    from app.adapters.fulfillment.scope_guard import check_scope, enforce_scope

    # ① 正向：合法 scope 必须通过
    for scopes, label, expect_pass in (
        (["order.read", "logistics.write"], "合法组合", True),
        (["order.read", "item.write"], "含商品编辑权 item.write", False),
        (["order.read", "price.update"], "含改价权 price.update", False),
        (["order.read", "item.offline"], "含下架权 item.offline", False),
        (["order.read", "foo.bar"], "白名单外的未知 scope", False),
    ):
        passed, forbidden, unknown = check_scope(scopes)
        ok = passed == expect_pass
        if not ok:
            FINDINGS.append(f"check_scope({scopes}) 期望 passed={expect_pass}，实际 {passed}")
        print(
            f"  {'[OK]' if ok else '[FAIL]'} check_scope({label:24}) -> passed={passed} "
            f"forbidden={forbidden} unknown={unknown}"
        )

    # ② enforce_scope 行为
    banner("D-R1  enforce_scope 授权/拒绝")
    try:
        granted = enforce_scope(["order.read", "logistics.write"], adapter_name="local_csv", actor="qa")
        print(f"  [OK]   合法 scope 通过，granted={sorted(granted)} type={type(granted).__name__}")
    except Exception as exc:  # noqa: BLE001
        FINDINGS.append(f"合法 scope 被误拒：{exc}")
        print(f"  [FAIL] 合法 scope 被误拒：{exc}")

    try:
        enforce_scope(["order.read", "item.write"], adapter_name="local_csv", actor="qa")
        FINDINGS.append("含 item.write 的 scope 竟然通过了 enforce_scope —— R1 红线失效")
        print("  [FAIL] 含 item.write 竟然通过 —— R1 红线失效")
    except Exception as exc:  # noqa: BLE001
        print(f"  [OK]   含 item.write 被拒：{type(exc).__name__} code={getattr(exc,'code',None)}")

    # ③ ★ 绕过尝试 1：不通过工厂，直接实例化适配器类
    banner("D-R1  ★ 绕过尝试 1：不走工厂，直接实例化 MiaoshouAdapter 并调用能力")
    from app.adapters.fulfillment import miaoshou as miaoshou_mod

    direct_adapter = miaoshou_mod.MiaoshouAdapter(config=None, credential=None, http=None, session=None)
    print(f"  ⚠ 绕过工厂直接实例化成功：{type(direct_adapter).__name__}（未触发任何 scope 校验）")
    print(f"     declared_scopes 属性 = {getattr(direct_adapter, 'declared_scopes', '★ 不存在')}")
    has_flag = any(hasattr(direct_adapter, a) for a in ("_scope_checked", "scoped", "granted_scopes"))
    print(f"     实例上有无「已通过 scope 校验」标记：{'有' if has_flag else '★ 无'}")

    # 直接实例化后能否真的干活？（健康探针）
    for target in ("health_check", "fetch_orders"):
        try:
            result = await direct_adapter.invoke(target, req=None)
            print(f"     ⚠ 未经 scope 校验即调用 {target:14} -> code={getattr(result,'code',None)}")
        except Exception as exc:  # noqa: BLE001
            print(f"     ℹ 调用 {target:14} -> {type(exc).__name__}: {str(exc)[:70]}")
    # 绕开 invoke 直接调方法
    try:
        hc = await direct_adapter.health_check()
        print(f"     ⚠ 直接调 health_check() 成功 -> code={getattr(hc,'code',None)}（★ R1 可被绕过）")
        FINDINGS.append(
            "R1 可被绕过：MiaoshouAdapter 可脱离 FulfillmentAdapterFactory 直接实例化并成功调用能力，"
            "全程不触发 enforce_scope"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"     ℹ 直接调 health_check() -> {type(exc).__name__}: {str(exc)[:70]}")

    # ④ ★ 绕过尝试 2：工厂存在 strict_scope 开关
    banner("D-R1  ★ 绕过尝试 2：工厂 create() 自带 strict_scope 开关")
    from app.adapters.fulfillment import factory as fac
    from app.models.order import FulfillmentAdapterConfig as CfgModel

    src = inspect.getsource(fac.FulfillmentAdapterFactory.create)
    has_switch = "strict_scope: bool = True" in src
    print(f"  create() 签名含 strict_scope 开关：{has_switch}")
    if has_switch:
        FINDINGS.append(
            "FulfillmentAdapterFactory.create() 暴露 strict_scope=False 开关 —— "
            "调用方可一行绕过 R1 权限红线（当前 app/ 内无人使用，但红线留了后门）"
        )
        print("  ⚠ 当前 app/ 内无调用方传 strict_scope=False，但红线存在可被绕过的开关（P2）")

    from sqlalchemy import select

    from app.core.database import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        # 给 local_csv 配一个越权 scope，再从工厂实例化 → 必须被拒
        row = (
            await session.execute(select(CfgModel).where(CfgModel.adapter_name == "local_csv"))
        ).scalars().first()
        origin = row.declared_scopes_json if row else []
        print(f"  local_csv 当前 declared_scopes = {origin}")
        if row is not None:
            row.declared_scopes_json = ["order.read", "item.write"]
            await session.flush()
            try:
                await fac.FulfillmentAdapterFactory.create("local_csv", session=session, actor="qa-attacker")
                FINDINGS.append("配 item.write 后工厂仍放行 —— R1 红线被突破")
                print("  [FAIL] 工厂仍放行 —— R1 红线被突破")
            except Exception as exc:  # noqa: BLE001
                print(f"  [OK]   工厂拒绝实例化：{type(exc).__name__} code={getattr(exc,'code',None)}")
                print(f"         {str(exc)[:180]}")
            # 恢复
            row.declared_scopes_json = origin
            await session.flush()
        await session.commit()


# =====================================================================
#  R2
# =====================================================================
def r2() -> None:
    """R2 第三方不得直写店铺。"""
    banner("D-R2  第三方履约适配器不得持有商品写能力（AST + 反射双重核验）")
    fulfil_dir = ROOT / "backend" / "app" / "adapters" / "fulfillment"
    write_methods = {
        "offline",
        "update_stock_price",
        "update_price",
        "publish",
        "onsale",
        "online",
        "edit_item",
        "update_item",
        "delisting",
    }
    for py in sorted(fulfil_dir.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if item.name in write_methods:
                            found.append(f"{node.name}.{item.name}()")
            if isinstance(node, ast.ImportFrom) and node.module and "listing" in str(node.module):
                found.append(f"import from {node.module}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "listing" in alias.name:
                        found.append(f"import {alias.name}")
        status = "[FAIL]" if found else "[OK]"
        if found:
            FINDINGS.append(f"履约适配层 {py.name} 出现写店铺能力/依赖：{found}")
        print(f"  {status} {py.name:28} {'写店铺方法或 listing 依赖：' + str(found) if found else '无写店铺方法、无 listing 依赖'}")

    banner("D-R2  反向：ListingAdapter.publish / offline 的调用点是否可枚举")
    app_dir = ROOT / "backend" / "app"
    hits: list[str] = []
    for py in sorted(app_dir.rglob("*.py")):
        text = py.read_text(encoding="utf-8")
        for target in (".publish(", ".offline(", ".update_stock_price("):
            if target in text:
                tree = ast.parse(text)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                        if "." + node.func.attr + "(" == target:
                            hits.append(f"{py.relative_to(app_dir)}:{node.lineno}  .{node.func.attr}()")
    if not hits:
        print("  全仓未发现任何 publish / offline / update_stock_price 的属性调用")
    else:
        for h in hits:
            print(f"  ℹ {h}")
    print("  注：PublishService.execute() 通过 adapter.invoke('publish', ...) 字符串派发，")
    print("      因此静态 grep '.publish(' 搜不到真实调用点 —— 唯一性需人工保证，见报告建议。")


# =====================================================================
#  R3
# =====================================================================
# ★ 履约适配器真实的 8 项能力（见 FulfillmentAdapter 基类）
CAPABILITY_METHODS = [
    "fetch_orders",
    "match_sku",
    "place_purchase_order",
    "fetch_tracking_no",
    "write_back_tracking",
    "submit_refund",
    "get_return_address",
    "push_inventory_change",
]


async def r3() -> None:
    """R3 能力降级不得抛异常。"""
    banner("D-R3  未开通（不配 base_url）时逐能力调用：必须返回信封，不得抛异常")
    from app.adapters.fulfillment.manifest import AdapterConfig, Capability

    # 模拟「未开通」：空配置、无 http 客户端
    empty = AdapterConfig(adapter_name="miaoshou")
    print(f"  config => base_url={empty.base_url!r} timeout={empty.timeout_sec!r}")

    for cls_path, cls_name in (
        ("app.adapters.fulfillment.miaoshou", "MiaoshouAdapter"),
        ("app.adapters.fulfillment.yitao", "YitaoAdapter"),
    ):
        module = __import__(cls_path, fromlist=[cls_name])
        cls = getattr(module, cls_name)
        adapter = cls(config=empty, credential=None, http=None, session=None)
        print(f"\n  ---- {cls_name}（未配置 base_url = 模拟未开通）----")
        print(f"       {'能力':24} {'字符串入参(生产写法)':32} {'枚举入参(类声明写法)':28}")
        string_break = 0
        for method in CAPABILITY_METHODS:
            # ① 生产代码的真实写法：adapter.invoke("xxx", req=...)
            try:
                r1 = await adapter.invoke(method, req=None)
                out1 = f"code={getattr(r1,'code',None)}"
            except Exception as exc:  # noqa: BLE001
                out1 = f"抛出 {type(exc).__name__}"
                string_break += 1
            # ② 类声明要求的写法：invoke(Capability.XXX, req=...)
            try:
                cap = Capability(method)
                r2 = await adapter.invoke(cap, req=None)
                out2 = f"code={getattr(r2,'code',None)}"
            except Exception as exc:  # noqa: BLE001
                out2 = f"抛出 {type(exc).__name__}: {str(exc)[:30]}"
            verdict = "[FAIL]" if "抛出" in out1 else "[OK]  "
            print(f"       {verdict} {method:22} {out1:32} {out2:28}")
        if string_break:
            FINDINGS.append(
                f"{cls_name}: {string_break}/{len(CAPABILITY_METHODS)} 项能力以字符串调用时抛 AttributeError"
                "（生产代码全部用字符串），R3「降级不抛异常」保障失效"
            )
            print(f"       => {string_break}/{len(CAPABILITY_METHODS)} 项能力以字符串调用即抛 AttributeError")


async def r1_audit() -> None:
    """R1 的审计腿：scope 校验结果必须真的落库（附录 A 第 16 条）。"""
    banner("D-R1  越权/校验通过时的审计落库是否真的写入")
    import inspect as _inspect

    from app.adapters.fulfillment import scope_guard as sg
    from app.services.audit_service import AuditService

    print(f"  AuditService.write 签名: {_inspect.signature(AuditService.write)}")
    src = _inspect.getsource(sg._write_audit)
    call_line = [ln.strip() for ln in src.splitlines() if "audit_service.write(" in ln]
    print(f"  scope_guard._write_audit 中的调用: {call_line}")
    try:
        await AuditService.write(
            action_type="permission_change",
            object_type="adapter",
            object_id="qa-probe",
            operator="qa",
            new_value={"probe": True},
            remark="QA probe",
        )
        print("  [OK]   AuditService.write 无 session 调用成功")
    except TypeError as exc:
        FINDINGS.append(f"scope 校验审计写入失败（{exc}）—— 越权审计落不了库，附录 A 第 16 条失效")
        print(f"  [FAIL] AuditService.write 缺 session 参数 -> {exc}")


async def main() -> None:
    """主入口。"""
    await r1()
    await r1_audit()
    r2()
    await r3()
    banner("D 段结论")
    if FINDINGS:
        print(f"  发现 {len(FINDINGS)} 项：")
        for item in FINDINGS:
            print(f"   - {item}")
    else:
        print("  未发现红线被突破")


if __name__ == "__main__":
    asyncio.run(main())
