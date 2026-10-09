"""端到端冒烟脚本（T-A08）。

    python scripts/smoke.py                        # 默认 http://127.0.0.1:8000
    python scripts/smoke.py --base-url http://...  # 指定服务地址
    python scripts/smoke.py --in-process           # 不起服务，进程内 ASGI 直连（CI 用）

覆盖 ≥ 10 个核心端点（ARCH §5.5 契约），断言返回码为 200 / 202 / 422：
    * 顶栏聚合 `GET /system/status-bar`（★ 零外部 HTTP）
    * 工作台、健康检查、枚举字典
    * 货源 / 素材 / AI 任务 / 映射（含 validate → 422）/ 上架 / 平台商品
    * 订单 / 售后 / 库存 / 适配器（含越权策略与告警）/ 审计 / 任务

★ 退出码：全通过 0，任一断言失败 1。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

import httpx  # noqa: E402

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
API_PREFIX = "/api/v1"

# (方法, 路径, 期望状态码集合, 说明)
CORE_ENDPOINTS: list[tuple[str, str, set[int], str]] = [
    ("GET", "/system/status-bar", {200}, "★ 顶栏聚合（零外部 HTTP）"),
    ("GET", "/dashboard/summary", {200}, "工作台聚合"),
    ("GET", "/health", {200, 503}, "健康检查"),
    ("GET", "/settings/enums", {200}, "枚举字典"),
    ("GET", "/settings", {200}, "系统配置"),
    ("GET", "/suppliers", {200}, "供应商列表"),
    ("GET", "/source-products", {200}, "货源商品列表"),
    ("GET", "/assets", {200}, "素材列表"),
    ("GET", "/ai-tasks", {200}, "AI 任务列表"),
    ("GET", "/ai-tasks/concurrency-config", {200}, "AI 并发配置"),
    ("GET", "/sku-mappings", {200}, "映射列表"),
    ("GET", "/sku-mappings/stats", {200}, "映射统计"),
    ("GET", "/sku-mappings/conflicts", {200}, "冲突列表"),
    ("GET", "/sku-mappings/pending", {200}, "待确认工单"),
    ("POST", "/sku-mappings/validate", {200, 422}, "★ 映射校验（不通过 → 422）"),
    ("GET", "/publish-tasks", {200}, "上架任务列表"),
    ("GET", "/listing-products", {200}, "平台商品列表"),
    ("GET", "/listing-products?missing_price=true", {200}, "★ 售价为空的在售商品"),
    ("GET", "/orders", {200}, "订单列表"),
    ("GET", "/orders/exceptions", {200}, "异常订单"),
    ("GET", "/purchase-orders", {200}, "采购单列表"),
    ("GET", "/after-sales", {200}, "售后列表"),
    ("GET", "/inventory/config", {200}, "库存配置"),
    ("GET", "/inventory/alerts", {200}, "库存告警"),
    ("GET", "/adapters/listing", {200}, "上架适配器"),
    ("GET", "/adapters/fulfillment", {200}, "履约适配器"),
    ("GET", "/adapters/scope-policies", {200}, "★ scope 白 / 黑名单"),
    ("GET", "/adapters/violations", {200}, "越权告警"),
    ("GET", "/audit-logs", {200}, "审计日志（默认排除系统记录）"),
    ("GET", "/tasks", {200}, "任务列表"),
]

VALIDATE_BODY = {
    "source_product_id": 999999,
    "platform": "taobao",
    "shop_id": "smoke-shop",
    "sku_codes": ["SMOKE-NOT-EXIST"],
}


def _build_client(base_url: str, in_process: bool) -> tuple[Any, Any]:
    """构造 httpx 客户端；`in_process` 时用 ASGITransport 直连应用。"""
    if in_process:
        from app.main import create_app

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        return httpx.AsyncClient(transport=transport, base_url="http://smoke"), None
    return httpx.AsyncClient(base_url=base_url, timeout=20.0), None


async def run(base_url: str, in_process: bool) -> int:
    """执行冒烟，返回退出码。"""
    client, _ = _build_client(base_url, in_process)
    passed = 0
    failed: list[str] = []

    async with client:
        # ---------- 只读 / 幂等端点 ----------
        for method, path, expected, note in CORE_ENDPOINTS:
            url = f"{API_PREFIX}{path}" if in_process or path.startswith("/") else path
            body = VALIDATE_BODY if path.startswith("/sku-mappings/validate") else None
            try:
                response = (
                    await client.post(url, json=body) if method == "POST" else await client.get(url)
                )
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{method} {path} —— 请求异常：{exc}")
                continue

            marker = "✅" if response.status_code in expected else "❌"
            if response.status_code in expected:
                passed += 1
            else:
                failed.append(
                    f"{method} {path} —— 期望 {sorted(expected)}，实际 {response.status_code}"
                )
            print(f"{marker} {method:4} {path:52} {response.status_code}  {note}")

        # ---------- 统一响应体结构校验 ----------
        response = await client.get(f"{API_PREFIX}/system/status-bar")
        payload = response.json()
        for key in ("code", "message", "data", "trace_id"):
            if key not in payload:
                failed.append(f"统一响应体缺少字段 {key}")
        else:
            passed += 1
            print(f"✅ 统一响应体结构完整（{sorted(payload)}）")

        # ---------- ★ 顶栏不得触发外部 HTTP：连续两次调用耗时应极短 ----------
        import time

        started = time.perf_counter()
        await client.get(f"{API_PREFIX}/system/status-bar")
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if elapsed_ms > 1500:
            failed.append(f"顶栏接口耗时 {elapsed_ms}ms —— 疑似触发外部调用")
        else:
            passed += 1
            print(f"✅ 顶栏接口耗时 {elapsed_ms}ms（未触发外部 HTTP）")

    print("-" * 96)
    if failed:
        print(f"❌ 冒烟失败 {len(failed)} 项：")
        for item in failed:
            print(f"   - {item}")
        return 1

    print(f"✅ 冒烟通过：{passed} 项断言全部成立（核心端点 {len(CORE_ENDPOINTS)} 个）")
    return 0


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="ERP 后端冒烟测试")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="服务地址")
    parser.add_argument(
        "--in-process", action="store_true", help="进程内 ASGI 直连（无需先启动服务）"
    )
    args = parser.parse_args()
    return asyncio.run(run(args.base_url, args.in_process))


if __name__ == "__main__":
    raise SystemExit(main())
