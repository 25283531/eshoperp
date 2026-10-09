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

import json
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

DEFAULT_HOST = "0.0.0.0"  # noqa: S104  容器必须监听全部地址，否则外部连不上
DEFAULT_PORT = 8000

# 前端运行时配置文件名。由本模块在启动时写进 `web/dist/`，
# `web/index.html` 在业务脚本之前加载它。
RUNTIME_CONFIG_FILENAME = "runtime-config.js"


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


def _write_runtime_config() -> None:
    """把后端**实际生效**的管理令牌写成前端可读的运行时配置。

    ★ 为什么必须在运行时写，不能在构建期用 `VITE_ADMIN_TOKEN` 注入：
      镜像由 GitHub Actions 构建、随公开仓库推到 GHCR，**任何人都拉得到**。
      把令牌编进前端产物 = 把使用者的管理令牌公开发布 —— 这正是
      `web/.env.development` 里把 `VITE_ADMIN_TOKEN` 留空的原因。

      但容器里后端的令牌是**运行时**环境变量注入的（compose 里强制必填），
      前端产物里没有 ⇒ 两者不一致 ⇒ 一键备份 / 凭证配置 / 适配器切换 /
      越权处置这类管理端接口**全部 403**。使用者看到的是"按钮点了没反应"，
      而日志里只有一条不起眼的 403 —— 属于「功能在、但根本用不了」的失效模式。

      所以改为启动时写一份 `web/dist/runtime-config.js`，前端从
      `window.__ERP_RUNTIME__.adminToken` 读取。令牌只存在于**容器的可写层**，
      镜像层里没有；每次启动重写，改了 `ADMIN_TOKEN` 重启即生效。

    ★ 取值必须与后端同源：这里读 `get_settings().admin_token` 而不是
      `os.getenv("ADMIN_TOKEN")`，否则"未设置 ADMIN_TOKEN 时用默认值
      `admin-token`"这一分支前后端会对不上（后端放行、前端拿着空串被 403）。

    ★ 可见性边界要说清楚：能访问该页面的人就能读这个文件 —— 这与"令牌固化在
      前端产物里"在**局域网范围内是等价的**。本改动真正消除的泄漏是
      **镜像公开**这一条，不是"同网的人拿不到"。要防后者只能上真正的登录，
      不在 MVP 范围，别拿这条假装解决了。
    """
    # 与 `app.main._resolve_web_dist()` 的"源码树约定"候选同源：
    # 镜像里即 /app/web/dist，与挂载给 _SpaStaticFiles 的是同一个目录。
    dist = BACKEND_DIR.parent / "web" / "dist"
    if not (dist / "index.html").is_file():
        print(
            "[info] 未检测到前端构建产物，跳过 runtime-config 注入（纯 API 形态）",
            file=sys.stderr,
        )
        return

    from app.core.config import get_settings

    token = get_settings().admin_token
    payload = json.dumps({"adminToken": token}, ensure_ascii=False)
    try:
        (dist / RUNTIME_CONFIG_FILENAME).write_text(
            f"window.__ERP_RUNTIME__ = {payload};\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(
            f"[warn] 写入 {RUNTIME_CONFIG_FILENAME} 失败：{exc}"
            "（管理端按钮将全部 403）",
            file=sys.stderr,
        )
        return
    print(f"[info] 已注入前端运行时配置：{dist / RUNTIME_CONFIG_FILENAME}", file=sys.stderr)


def main() -> int:
    _warn_default_admin_token()

    # 建库 / 迁移（失败不阻断启动 —— 接口会如实 500，日志里已有原因）
    from app.core.bootstrap import ensure_database_ready

    ensure_database_ready(BACKEND_DIR / "alembic.ini")

    _write_runtime_config()

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
