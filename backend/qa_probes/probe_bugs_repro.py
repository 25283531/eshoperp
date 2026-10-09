"""★ QA 探针：复现并定位 B 段发现的两个 P0（真复现，非推断）。

缺陷 1：ManualListingAdapter 被以 str 传入 platform，凡是读 `self.platform.value` 的路径全崩。
缺陷 2：AiClientFactory.create(session) 把 session 绑到 name 形参 → AI 重构 100% 失败。
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = Path(__file__).resolve().parents[1]
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + (ROOT / "data" / "erp.db").as_posix()
os.environ["APP_ENV"] = "test"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["TASK_RECOVERY_ENABLED"] = "false"
os.environ["LOG_JSON"] = "false"

sys.path.insert(0, str(BACKEND_ROOT))

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.core.database import dispose_engine, get_db  # noqa: E402
from app.main import create_app  # noqa: E402

SEP = "=" * 96


def banner(title: str) -> None:
    """分节标题。"""
    print(f"\n{SEP}\n{title}\n{SEP}")


async def bugs() -> None:
    """复现缺陷 1：半自动主路径 platform 类型漂移。"""
    from app.adapters.listing.manual import ManualListingAdapter
    from app.models.enums import Platform

    banner("缺陷 1a  直接单元复现：ManualListingAdapter 被传入 str 类型的 platform")
    adapter = ManualListingAdapter(session=None, platform="taobao")  # ← 服务层的真实写法
    print(f"  self.platform = {adapter.platform!r}  type={type(adapter.platform).__name__}")
    print(f"  ManualListingAdapter.__init__ 声明的类型应为 Platform(枚举)，实际收到 {type(adapter.platform).__name__}")

    payload = ManualListingPayloadFactory()
    for method in ("build_form_data", "build_readme"):
        print(f"\n  -- 调用 {method}()")
        try:
            result = getattr(adapter, method)(payload)
            print(f"     ✅ 成功：{str(result)[:120]}")
        except Exception as exc:  # noqa: BLE001
            print(f"     ❌ {type(exc).__name__}: {exc}")
            tb = traceback.extract_tb(exc.__traceback__)
            frame = tb[-1]
            print(f"     源码位置: {Path(frame.filename).name}:{frame.lineno}  ->  {frame.line}")

    banner("缺陷 1b  用正确类型（枚举）时同一方法是否正常")
    adapter_ok = ManualListingAdapter(session=None, platform=Platform.TAOBAO)
    try:
        result = adapter_ok.build_form_data(payload)
        print(f"  ✅ platform=Platform.TAOBAO 时 build_form_data 正常，返回 keys={list(result)}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ 仍失败：{exc}")

    banner("缺陷 1c  经 HTTP：GET /publish-tasks/manual/{id}/form-data 与 /package")
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://qa") as client:
        _, products = await _json(client.get, "/api/v1/source-products?page=1&page_size=1")
        rows = (products or {}).get("items") or []
        source_id = int(rows[0]["id"]) if rows else 0
        print(f"  source_product_id = {source_id}")
        if not source_id:
            return

        status, res = await _json(
            client.post,
            "/api/v1/publish-tasks",
            json_body={
                "source_product_ids": [source_id],
                "platform": "taobao",
                "shop_id": "qa-shop-formdata",
                "mode": "manual",
                "ai_task_result_ids": {},
            },
        )
        task_ids = ((res or {}).get("data") or {}).get("task_ids") or []
        if not task_ids:
            print(f"  ❌ 创建失败 {status}: {res}")
            return
        task_id = int(task_ids[0])
        print(f"  publish_task_id = {task_id}")

        for path, label in (
            (f"/api/v1/publish-tasks/manual/{task_id}/form-data", "★ 预填表单（运营要复制到平台后台的数据）"),
            (f"/api/v1/publish-tasks/manual/{task_id}/package", "★ 素材包 ZIP 下载"),
        ):
            code, body = await _json(client.get, path)
            verdict = "✅ 200" if code == 200 else f"❌ {code}"
            print(f"  {verdict}  GET {path}")
            print(f"        {label}")
            print(f"        response={{'code': {body.get('code') if isinstance(body, dict) else '?'}, "
                  f"'message': {str(body.get('message'))[:120] if isinstance(body, dict) else str(body)[:120]}}}")


async def _json(method, path, *, json_body=None):
    """发请求并返回 (status, json)。"""
    try:
        if json_body is None:
            resp = await method(path)
        else:
            resp = await method(path, json=json_body)
    except Exception as exc:  # noqa: BLE001
        return -1, {"error": str(exc)}
    try:
        return resp.status_code, resp.json()
    except Exception:  # noqa: BLE001
        return resp.status_code, {"raw": resp.text[:200]}


class ManualListingPayloadFactory:
    """最小 ListingPayload（直接引用真实负载类型）。"""

    def __new__(cls):  # noqa: D102
        from app.adapters.listing.base import ListingPayload, ListingSkuPayload

        return ListingPayload(
            shop_id="qa-shop",
            title="演示标题",
            selling_points=["卖点1"],
            attributes_json={},
            category_id="16",
            main_images=[],
            detail_images=[],
            skus=[
                ListingSkuPayload(
                    spec_json={"颜色": "红"},
                    sale_price_cents=9900,
                    stock_qty=10,
                    source_sku_id=1,
                    source_sku_code_1688="SKU-A",
                    purchase_cost_cents=1200,
                )
            ],
            source_product_id=1,
            ai_task_result_id=None,
            trace_id="qa",
        )


async def bug_ai() -> None:
    """复现缺陷 2：AI 工厂参数错位。"""
    banner("缺陷 2  AiClientFactory 参数绑定错位")
    from app.adapters.ai.factory import AiClientFactory
    from app.core.database import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        print("  服务层真实写法：AiClientFactory.create(session)")
        try:
            client = await AiClientFactory.create(session)
            print(f"  ✅ 返回 {client}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ {type(exc).__name__}: {str(exc)[:220]}")

        print("\n  正确写法：AiClientFactory.create(session=session)")
        try:
            client = await AiClientFactory.create(session=session)
            print(f"  ✅ 返回 {type(client).__name__}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ {type(exc).__name__}: {exc}")

        await session.rollback()


async def main() -> None:
    """主入口。"""
    await bugs()
    await bug_ai()
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
