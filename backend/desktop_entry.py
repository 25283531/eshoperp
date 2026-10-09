"""桌面应用入口（Windows exe 的启动点）。

职责：启动内嵌的 FastAPI 服务，并在就绪后自动打开默认浏览器。

★ 为什么单独一个入口，而不是直接跑 `app.main:app`：
  `app.main` 是**库形态**的应用工厂，不该关心「要不要开浏览器」「端口被占了怎么办」
  这类属于交付形态的问题。桌面形态需要「起服务 + 挑端口 + 开浏览器 + 告诉使用者地址」
  这一组动作，放在这里可以让 `app.main` 保持干净。
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser

DEFAULT_PORT = 8000
MAX_PORT_TRIES = 20


def pick_port(preferred: int = DEFAULT_PORT, tries: int = MAX_PORT_TRIES) -> int:
    """挑一个空闲端口，从 preferred 开始顺延。

    ★ 端口被占时**必须让使用者看见换了端口**，不能默默连到别的端口上去：
      否则「双击 exe、浏览器开了、但连到的是另一个程序」这种事根本查不出来。
    """
    for port in range(preferred, preferred + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError(f"端口 {preferred}~{preferred + tries - 1} 均被占用，无法启动")


def maybe_open_browser(url: str, delay: float = 1.2) -> None:
    """延迟一小会儿再打开浏览器（等服务真正起来）。

    ★ `ERP_NO_BROWSER=1` 时不打开 —— CI 冒烟验证、以及只想当服务跑的场景需要它。
    ★ 浏览器打开失败**不能终止服务**：服务是主体，浏览器只是个便利。
    """
    if os.getenv("ERP_NO_BROWSER"):
        return

    def _open() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 浏览器不可用不影响服务本体
            print("[warn] 无法自动打开浏览器，请手动访问上述地址", file=sys.stderr)

    threading.Thread(target=_open, daemon=True).start()


def warn_default_admin_token() -> None:
    """打包产物若仍在用默认管理令牌，启动时给一次醒目提示。

    ★ 为什么只在**打包环境**提醒：后端的默认令牌 `admin-token` 被十几个脚本、
      探针与测试用例依赖，开发态每次启动都刷警告很快会被当成噪音忽略掉。
      真正需要被提醒的是拿到 exe 的人 —— 他很可能以为"我没配过密钥，所以是安全的"。

    ★ 为什么**不阻断**：自用场景下强制改令牌会让第一次使用直接卡死，
      属于用可用性换安全的过度设计。但必须说出口 ——
      "使用者根本不知道有这回事"才是真正的风险，而不是"他知道了但暂时没改"。
    """
    print("!" * 66)
    print("!  安全提醒：未设置 ADMIN_TOKEN，管理端接口使用**默认令牌**。")
    print("!  任何能访问本机服务端口的人，都能改凭证 / 切适配器 / 处置越权。")
    print("!  建议：设置环境变量 ADMIN_TOKEN 后重新启动。")
    print("!" * 66)


def main() -> int:
    try:
        import uvicorn

        from app.main import create_app
    except ImportError as exc:  # pragma: no cover - 打包缺失依赖时的人肉排错路径
        print(f"依赖缺失：{exc}", file=sys.stderr)
        return 2

    # 仅在打包产物（PyInstaller）中提醒，开发态不打扰
    if getattr(sys, "frozen", False) and not os.getenv("ADMIN_TOKEN"):
        warn_default_admin_token()

    port = pick_port()
    url = f"http://127.0.0.1:{port}"

    app = create_app()
    maybe_open_browser(url)

    print("=" * 58)
    print("  自用电商 ERP 已启动")
    print(f"  访问地址：{url}")
    print("  保持本窗口开启即是运行中；关闭窗口即可停止服务")
    print("=" * 58)

    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
