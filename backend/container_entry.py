"""容器启动入口：自建库 → 起服务。

★ 与桌面形态（已废弃的 `desktop_entry.py`）的三处关键差异：

  1. **监听地址必须是 `0.0.0.0`**
     桌面版用 `127.0.0.1` 是刻意的（只允许本机访问）；容器里照搬会导致
     宿主机端口映射过来却连不上 —— 容器内只听回环，外部请求一律被拒。
     这是最容易照抄出错的一处。

  2. **不做端口顺延**
     容器内固定 8000，端口冲突交给 compose 的端口映射解决
     （`8000:8000` 不够就改左侧）。顺延会让"我连的是不是被测物"失去依据。

  3. **不开浏览器**
     容器无桌面环境；由使用者从宿主机浏览器访问映射后的端口。

★ 建库逻辑走 `app.core.bootstrap.ensure_database_ready()`，
  这是容器 / 桌面共用的**唯一实现**（分叉会导致版本记录漂移，见该模块文档）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

DEFAULT_HOST = "0.0.0.0"  # noqa: S104  容器必须监听全部地址，否则外部连不上
DEFAULT_PORT = 8000


def _warn_default_admin_token() -> None:
    """未显式设置 ADMIN_TOKEN 时打印醒目提醒。

    ★ 为什么只是警告而不是拒绝启动：
      阻断启动等于用可用性做安全设计，会让使用者在排障时首先怀疑"是不是没起来"。
      但**必须说出来** —— 否则默认令牌 `admin-token` 会一直留在部署里，
      而这是本项目已知的真实泄漏点之一。
    """
    if os.getenv("ADMIN_TOKEN"):
        return
    print(
        "=" * 66 + "\n"
        "  ⚠ 未设置环境变量 ADMIN_TOKEN，当前使用**默认令牌** `admin-token`。\n"
        "  该默认值写在代码里（`app/core/config.py`），是公开已知的值。\n"
        "  **局域网内任何人都能调用管理接口，请务必设置一个自己的令牌。**\n"
        "  做法：在 docker-compose.yml 的 environment 里加一行 ADMIN_TOKEN=...\n"
        + "=" * 66,
        file=sys.stderr,
    )


def main() -> int:
    _warn_default_admin_token()

    # 建库 / 迁移（失败不阻断启动 —— 接口会如实 500，日志里已有原因）
    from app.core.bootstrap import ensure_database_ready

    ensure_database_ready(BACKEND_DIR / "alembic.ini")

    import uvicorn

    host = os.getenv("ERP_HOST", DEFAULT_HOST)
    try:
        port = int(os.getenv("ERP_PORT", str(DEFAULT_PORT)))
    except ValueError:
        print(f"[warn] ERP_PORT 不是整数，回退到 {DEFAULT_PORT}", file=sys.stderr)
        port = DEFAULT_PORT

    print(f"[info] 启动服务：{host}:{port}", file=sys.stderr)
    uvicorn.run("app.main:app", host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
