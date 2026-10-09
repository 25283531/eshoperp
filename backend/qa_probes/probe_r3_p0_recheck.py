"""★ 第三轮 · 原 4 个 P0 + QA-13 的聚焦复核（全部针对**当前源码**，进程内 TestClient）。

为什么用 TestClient 而不是连 8000 实例：
    8000 那个进程启动早于 `app/api/v1/orders.py`（mtime 21:51）的最后一次修改，
    它的 OpenAPI 里没有 `place-purchase` 路由 —— 属于**进程加载了旧代码**，
    不是路由没写。为了不被陈旧进程误导，本轮一律用 `create_app()` 载入当前源码验证。

覆盖：
  S0   先证明「测的是当前代码」：ConflictType 枚举 + /health 返回 constraints
  QA-05  三个履约适配器 PUT config 必须 200，且库里真的落行 + declared_scopes 可读回
  QA-01/02
        (a) 静态：死 SQL 常量是否已移除、DETECTION_ORDER 是否已剔除
        (b) 动态：重复唯一键插入必须 409 / 1006 中文可读错误，不能漏成 500
        (c) ★反向验证：DROP 掉 uq_sku_mapping_shop_sku，/health.constraints 必须报缺失
  QA-13  不传 session 创建 item.write 适配器必须被 ScopeViolationError 拒绝（不静默放行）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402

SEP = "=" * 92
FAILS: list[str] = []
H = {"X-Operator": "qa", "X-Operator-Token": "admin-token", "Content-Type": "application/json"}
LEGAL_SCOPES = ["order.read", "logistics.write"]


def banner(t: str) -> None:
    """分节标题。"""
    print(f"\n{SEP}\n{t}\n{SEP}")


def check(ok: bool, label: str, detail: str = "") -> bool:
    """统一判定输出。"""
    print(f"  {'✅' if ok else '❌'} {label}" + (f"  —— {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)
    return ok


def s0_current_code() -> None:
    """S0：证明本轮验证对象是当前代码，不是旧进程。"""
    banner("S0  先证明「我测的是当前代码」")
    from app.models.enums import ConflictType

    vals = [c.value for c in ConflictType]
    print(f"  ConflictType = {vals}")
    check("duplicate_item" not in vals, "ConflictType 已剔除 duplicate_item（枚举层）",
          "detection 层仍保留常量仅为前端展示兼容")

    from app.models.mapping import DETECTION_ORDER

    print(f"  DETECTION_ORDER = {DETECTION_ORDER}")
    check("one_to_many" not in DETECTION_ORDER and "duplicate_item" not in DETECTION_ORDER,
          "DETECTION_ORDER 已剔除两个恒空检测器")

    client = TestClient(create_app(), raise_server_exceptions=False)
    r = client.get("/api/v1/health", headers=H)
    data = r.json().get("data", {})
    print(f"  GET /health -> HTTP {r.status_code}  status={data.get('status')}")
    print(f"  constraints = {data.get('constraints')}")
    check(r.status_code == 200 and "constraints" in data,
          "/health 具备 constraints 自检字段（本轮新增能力，旧进程没有）")


def qa05_config() -> None:
    """QA-05：三个适配器 PUT config 必须都能 200 且真的落库。"""
    banner("QA-05  三个履约适配器 PUT config（合法 scope）→ 必须 200 + 真落库")
    client = TestClient(create_app(), raise_server_exceptions=False)
    results: dict[str, int] = {}
    for name in ("miaoshou", "yitao", "local_csv"):
        r = client.put(
            f"/api/v1/adapters/fulfillment/{name}/config",
            headers=H,
            json={"declared_scopes": LEGAL_SCOPES, "is_enabled": True},
        )
        body = r.json()
        results[name] = r.status_code
        print(f"  PUT {name:<10} -> HTTP {r.status_code}  code={body.get('code')}  {body.get('message')}")
    check(all(v == 200 for v in results.values()), f"三个适配器 config 全部 200（实测 {results}）",
          "旧缺陷是 100% 404")

    # 库里必须真的有行，且 declared_scopes 可读回
    async def read_rows() -> list[tuple[Any, ...]]:
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal
        from app.models.order import FulfillmentAdapterConfig

        async with AsyncSessionLocal() as s:
            rows = (await s.execute(select(FulfillmentAdapterConfig))).scalars().all()
            return [(r.adapter_name, r.declared_scopes_json, bool(r.is_enabled)) for r in rows]

    rows = asyncio.run(read_rows())
    print(f"  [DB] fulfillment_adapter_config 行数 = {len(rows)}")
    for row in rows:
        print(f"        {row}")
    check(len(rows) >= 3, f"配置行真的落库（{len(rows)} 行）", "旧缺陷：表里 0 行 ⇒ R1 与告警整体失效")
    check(all(r[1] for r in rows), "每行 declared_scopes 非空",
          "declared_scopes 为空 ⇒ check_scope([]) 通过 ⇒ R1 永不拒绝")


def qa01_02() -> None:
    """QA-01 / QA-02：死 SQL 已移除 + 重复键 409/1006 + 反向验证约束自检。"""
    banner("QA-01 / QA-02  重复映射：必须 409/1006，且约束自检能反向发现索引缺失")
    client = TestClient(create_app(), raise_server_exceptions=False)

    payload = {
        "platform": "taobao",
        "shop_id": "qa-r3-shop",
        "shop_item_id": "QA-R3-ITEM",
        "shop_sku_code": "QA-R3-SKU",
        "shop_sku_name": "QA 重复键探针",
        "purchase_cost": "12.50",
    }
    # 先清掉可能的历史数据（软删除也算撞键，所以物理删）
    asyncio.run(_purge("qa-r3-shop", "QA-R3-SKU"))

    r1 = client.post("/api/v1/sku-mappings", headers=H, json=payload)
    print(f"  第一次 POST -> HTTP {r1.status_code}  code={r1.json().get('code')}")
    check(r1.status_code == 201, f"首次创建成功（HTTP {r1.status_code}）")

    r2 = client.post("/api/v1/sku-mappings", headers=H, json=payload)
    b2 = r2.json()
    print(f"  第二次 POST（撞唯一键）-> HTTP {r2.status_code}  code={b2.get('code')}")
    print(f"        message = {b2.get('message')}")
    check(r2.status_code == 409, f"重复键返回 409（实测 HTTP {r2.status_code}）", "旧缺陷：漏成 500")
    check(b2.get("code") == 1006, f"业务码 = 1006（实测 {b2.get('code')}）")
    msg = str(b2.get("message") or "")
    check(bool(msg) and any("\u4e00" <= ch <= "\u9fff" for ch in msg),
          "错误信息是可直接展示给运营的中文", msg[:60])

    # ★ 反向验证：把索引 DROP 掉，/health.constraints 必须报缺失
    print("\n  ---- 反向验证：DROP INDEX uq_sku_mapping_shop_sku ----")
    asyncio.run(_drop_index())
    r3 = client.get("/api/v1/health", headers=H)
    c = r3.json().get("data", {}).get("constraints", {})
    missing = [m.get("name") if isinstance(m, dict) else m for m in c.get("missing", [])]
    print(f"  GET /health -> HTTP {r3.status_code}  status={r3.json().get('data', {}).get('status')}")
    print(f"  constraints = ok={c.get('ok')} missing_count={c.get('missing_count')} missing={missing}")
    if c.get("reason"):
        print(f"        reason = {c.get('reason')}")
    check(c.get("ok") is False and "uq_sku_mapping_shop_sku" in missing,
          "索引缺失被 /health 自检真实报出（不是嘴上说有约束）")

    asyncio.run(_restore_index())
    r4 = client.get("/api/v1/health", headers=H)
    c2 = r4.json().get("data", {}).get("constraints", {})
    print(f"  恢复索引后 constraints = ok={c2.get('ok')} missing_count={c2.get('missing_count')}")
    check(c2.get("ok") is True, "索引恢复后自检回到 ok=true")

    asyncio.run(_purge("qa-r3-shop", "QA-R3-SKU"))


async def _purge(shop_id: str, sku_code: str) -> None:
    """物理清理探针数据（软删除仍会撞唯一键）。"""
    from sqlalchemy import delete, select

    from app.core.database import AsyncSessionLocal
    from app.models.mapping import MappingConflict, SkuMapping

    async with AsyncSessionLocal() as s:
        ids = [r for r in (await s.execute(
            select(SkuMapping.id).where(SkuMapping.shop_sku_code == sku_code))).scalars().all()]
        if ids:
            await s.execute(delete(MappingConflict).where(MappingConflict.sku_mapping_id.in_(ids)))
        await s.execute(delete(SkuMapping).where(SkuMapping.shop_sku_code == sku_code))
        await s.commit()


async def _drop_index() -> None:
    """DROP 掉局部唯一索引。"""
    from sqlalchemy import text

    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as s:
        await s.execute(text("DROP INDEX IF EXISTS uq_sku_mapping_shop_sku"))
        await s.commit()


async def _restore_index() -> None:
    """恢复局部唯一索引（与建表 DDL 一致）。"""
    from sqlalchemy import text

    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as s:
        await s.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_sku_mapping_shop_sku "
            "ON sku_mapping (platform, shop_id, shop_sku_code) WHERE is_deleted = 0"
        ))
        await s.commit()


def qa13_no_session() -> None:
    """QA-13：不传 session 时，越权适配器必须被拒绝，不能静默放行。"""
    banner("QA-13  不传 session 创建 item.write 适配器 → 必须拒绝（fail-closed）")
    from app.adapters.fulfillment import FulfillmentAdapterFactory

    async def run() -> tuple[str, str | None]:
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal
        from app.models.order import FulfillmentAdapterConfig

        # 先把 miaoshou 的声明 scope 写成越权的 item.write，模拟「库里明知它越权」
        async with AsyncSessionLocal() as s:
            row = (await s.execute(select(FulfillmentAdapterConfig).where(
                FulfillmentAdapterConfig.adapter_name == "miaoshou"))).scalar_one_or_none()
            if row is None:
                return "SKIP", None
            row.declared_scopes_json = ["item.write"]
            await s.commit()
            saved = row.declared_scopes_json
        # ★ 关键：调用方不传 session
        try:
            adapter = await FulfillmentAdapterFactory.create("miaoshou")
            return "PASSED", type(adapter).__name__
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}", str(exc)

    verdict, detail = asyncio.run(run())
    print(f"  库里 miaoshou.declared_scopes = ['item.write']（越权）")
    print(f"  FulfillmentAdapterFactory.create('miaoshou')  [不传 session]")
    print(f"  -> {verdict}")
    if detail:
        print(f"     {detail[:220]}")
    check(verdict == "ScopeViolationError",
          "不传 session 时按「无法确认权限 = 拒绝」处理（不再静默放行）",
          "旧缺陷：返回实例 + 审计记 passed")

    # 复原
    async def restore() -> None:
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal
        from app.models.order import FulfillmentAdapterConfig

        async with AsyncSessionLocal() as s:
            row = (await s.execute(select(FulfillmentAdapterConfig).where(
                FulfillmentAdapterConfig.adapter_name == "miaoshou"))).scalar_one_or_none()
            if row is not None:
                row.declared_scopes_json = LEGAL_SCOPES
                await s.commit()

    asyncio.run(restore())
    print("  （已把 miaoshou 的 scope 复原为合法值）")


def main() -> None:
    """主流程。"""
    print("  Settings.database_url =", get_settings().database_url)
    s0_current_code()
    qa05_config()
    qa01_02()
    qa13_no_session()

    banner("第三轮 P0 复检结论")
    if FAILS:
        for f in FAILS:
            print(f"  ❌ {f}")
        print(f"\n  未通过 {len(FAILS)} 项")
    else:
        print("  ✅ 原 P0（QA-01/02/03/05）+ QA-06/QA-13 全部复检通过")
        print("     （QA-03 / QA-06 见 probe_i4_impact.py 与 probe_r3_qa06.py 的实测输出）")


if __name__ == "__main__":
    main()
