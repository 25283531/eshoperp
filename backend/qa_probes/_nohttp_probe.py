"""零外部 HTTP 实证探针：拦截一切 httpx / urllib / socket 对外连接后调 status-bar。

★ 不用「耗时 < 1.5s」这种推测式判据，而是把 httpx.AsyncClient.send、
  httpx.Client.send、socket.socket.connect 全部替换成「记录 + 放行本地 / 拦截外网」的钩子，
  任何第三方心跳都会留下调用记录。
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(__file__).resolve().parents[2]
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + (ROOT / "data" / "erp.db").as_posix()
os.environ["APP_ENV"] = "test"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["TASK_RECOVERY_ENABLED"] = "false"
os.environ["LOG_JSON"] = "false"

sys.path.insert(0, str(BACKEND_ROOT))

NETWORK_CALLS: list[str] = []

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _guard(self, address, *, _real):
    """拦截所有对外 socket 连接；仅放行 127.0.0.1 / localhost。"""
    host = address[0] if isinstance(address, tuple) else str(address)
    if str(host) in {"127.0.0.1", "::1", "localhost"}:
        return _real(self, address)
    NETWORK_CALLS.append(f"socket.connect -> {host}:{address[1] if len(address) > 1 else '?'}")
    raise AssertionError(f"★ 检测到对外网络连接：{host}")


def _patched_connect(self, address):
    return _guard(self, address, _real=_real_connect)


def _patched_connect_ex(self, address):
    return _guard(self, address, _real=_real_connect_ex)


# 也拦截 urllib 的 getaddrinfo 级别调用可被 socket.connect 覆盖，这里再补一道 DNS
_real_getaddrinfo = socket.getaddrinfo


def _patched_getaddrinfo(host, *args, **kwargs):
    if str(host) not in {"127.0.0.1", "::1", "localhost"}:
        NETWORK_CALLS.append(f"DNS getaddrinfo -> {host}")
        raise AssertionError(f"★ 检测到对外域名解析：{host}")
    return _real_getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _patched_connect
socket.socket.connect_ex = _patched_connect_ex
socket.getaddrinfo = _patched_getaddrinfo
socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(AssertionError("★ create_connection 被外部调用"))

import httpx  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

_real_async_send = httpx.AsyncClient.send
_real_sync_send = httpx.Client.send


async def _patched_async_send(self, request, **kwargs):
    host = request.url.host
    if host not in {"127.0.0.1", "localhost", "testserver", "test"}:
        NETWORK_CALLS.append(f"httpx.AsyncClient.send -> {request.method} {request.url}")
        raise AssertionError(f"★ 检测到 httpx 对外请求：{request.url}")
    return await _real_async_send(self, request, **kwargs)


def _patched_sync_send(self, request, **kwargs):
    host = request.url.host
    if host not in {"127.0.0.1", "localhost", "testserver", "test"}:
        NETWORK_CALLS.append(f"httpx.Client.send -> {request.method} {request.url}")
        raise AssertionError(f"★ 检测到 httpx 对外请求：{request.url}")
    return _real_sync_send(self, request, **kwargs)


httpx.AsyncClient.send = _patched_async_send
httpx.Client.send = _patched_sync_send

from app.main import create_app  # noqa: E402


async def run() -> int:
    """执行探针。"""
    print("  已在 httpx.AsyncClient.send / httpx.Client.send / socket.connect / getaddrinfo 上装好拦截器")
    print("  任何指向非 127.0.0.1 的连接都会抛 AssertionError 并留下记录\n")

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 预热一次（排除首次导入 / 惰性初始化产生的调用）
        await client.get("/api/v1/system/status-bar")
        NETWORK_CALLS.clear()

        started = time.perf_counter()
        for i in range(20):
            resp = await client.get("/api/v1/system/status-bar")
            if resp.status_code != 200:
                print(f"  ❌ 第 {i + 1} 次状态 {resp.status_code}")
                return 1
        elapsed_ms = (time.perf_counter() - started) * 1000
        data = resp.json().get("data") or {}

        print(f"  连续 20 次 GET /system/status-bar 全部 200")
        print(f"  总耗时 {elapsed_ms:.1f}ms，单次平均 {elapsed_ms / 20:.2f}ms")
        print(f"  期间被拦截的对外网络调用: {len(NETWORK_CALLS)}")
        for call in NETWORK_CALLS:
            print(f"     ✗ {call}")

        print(f"\n  返回字段: {sorted(data)}")
        print(f"  unhandled_violation_count = {data.get('unhandled_violation_count')}")
        print(f"  listing_mode = {data.get('listing_mode')}  is_mock_active = {data.get('is_mock_active')}")
        print(f"  active_fulfillment_adapter = {data.get('active_fulfillment_adapter')}")
        print(f"  health_status = {data.get('health_status')}")

        # 对照组：证明拦截器真的会拦截（打一个外网地址应当失败）
        print("\n  -- 对照组：故意发起一次对外请求，验证拦截器生效 --")
        try:
            probe = httpx.AsyncClient(timeout=5.0)
            await probe.get("https://example.com")
            print("  ⚠ 未拦截 —— 探针失效，结论不可信")
            await probe.aclose()
            return 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ✅ 拦截器生效：{type(exc).__name__}: {str(exc)[:90]}")
        return 0


if __name__ == "__main__":
    import time

    raise SystemExit(asyncio.run(run()))
