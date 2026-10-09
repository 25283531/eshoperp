"""FastAPI 应用工厂（T-A01）。

职责：
    * 初始化配置、日志、数据库；
    * 注册中间件（trace_id）、CORS、全局异常处理器；
    * 挂载 /api/v1 路由（**若 app.api 尚未实现则优雅降级**，保证 `import app.main` 可用）；
    * lifespan：启动调度器 + 任务恢复；关闭时停止调度器与释放连接池。
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from app.core.config import Settings, get_settings
from app.core.database import dispose_engine, get_engine, init_db
from app.core.errors import register_exception_handlers
from app.core.logging import TraceIdMiddleware, get_logger, setup_logging
from app.core.response import ApiResponse

logger = get_logger(__name__)

DESCRIPTION = """
自用电商 ERP 后端。

**架构红线**
- R1 第三方永远不持有商品编辑权（scope 白名单硬校验，落在适配器工厂）
- R2 只有 ListingAdapter 能写店铺商品（offline / update_stock_price）
- R3 第三方能力不可靠时业务不中断（UNSUPPORTED / DEGRADED 降级，绝不抛裸异常）

**统一响应体**：`{code, message, data, trace_id}`，`code=0` 表示成功。
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    """构建 FastAPI 应用实例。"""
    settings = settings or get_settings()
    setup_logging(level=settings.log_level, json_logs=settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        """应用生命周期：启动初始化 + 关闭清理。"""
        await init_db(settings)
        logger.info("database_ready", url=_mask_db_url(settings.database_url))

        # ★★ 启动期显式初始化 + 约束自检（QA-05 / QA-01 / QA-02）★★
        #   - `bootstrap_all()`：把「三个履约适配器 / 平台账号 / 凭证」**幂等落库并 commit**。
        #     此前这些行靠"第一次 GET 时顺手 flush"播种，而 `get_db()` 不 commit
        #     ⇒ 行随请求结束一起回滚 ⇒ `PUT config` 恒定 404、凭证页与授权页恒空。
        #   - `check_required_constraints()`：`one_to_many` / `duplicate_item` 的保护
        #     **完全**依赖部分唯一索引（读时扫描已移除），索引若被人误删必须立刻可见。
        try:
            from app.core.database import get_session_factory
            from app.services.bootstrap import bootstrap_all, check_required_constraints

            factory = get_session_factory()
            async with factory() as boot_session:
                boot_stats = await bootstrap_all(boot_session, commit=True)
                logger.info("reference_data_bootstrap_done", **boot_stats)
                constraint_issues = await check_required_constraints(boot_session)
                if constraint_issues:
                    logger.error(
                        "required_constraints_missing_at_startup",
                        count=len(constraint_issues),
                        issues=[i["name"] for i in constraint_issues],
                    )
                else:
                    logger.info("required_constraints_ok", count=3)
        except Exception as exc:  # noqa: BLE001  初始化失败不得阻断 Web 启动
            logger.error("reference_data_bootstrap_failed", error=str(exc))

        # 启动异步任务运行器与调度器
        runner = _start_runner()
        app.state.task_runner = runner
        try:
            from app.tasks.scheduler import start_scheduler

            start_scheduler(settings, runner)
        except Exception as exc:  # noqa: BLE001  调度器失败不应阻断 Web 启动
            logger.error("scheduler_start_failed", error=str(exc))

        # 重启恢复：扫描未完成任务（★ 即使恢复开关关闭也会扫描并如实上报残留，
        #   避免"recovered=0"被误读成"没有残留任务"）
        try:
            from app.tasks.recovery import recover_pending_tasks, start_watchdog

            stats = await recover_pending_tasks(runner=runner, settings=settings)
            logger.info("task_recovery_summary", **stats.to_dict())
        except Exception as exc:  # noqa: BLE001
            logger.error("task_recovery_failed", error=str(exc))

        # 卡死看门狗：进程运行期间周期性回收 running 超时任务
        try:
            start_watchdog(settings, runner)
        except Exception as exc:  # noqa: BLE001
            logger.error("task_watchdog_start_failed", error=str(exc))

        yield

        try:
            from app.tasks.recovery import stop_watchdog

            stop_watchdog()
        except Exception as exc:  # noqa: BLE001
            logger.warning("task_watchdog_stop_failed", error=str(exc))
        try:
            from app.tasks.scheduler import stop_scheduler

            stop_scheduler(wait=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("scheduler_stop_failed", error=str(exc))
        if runner is not None:
            try:
                runner.shutdown(wait=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning("task_runner_shutdown_failed", error=str(exc))
        await dispose_engine()
        logger.info("application_shutdown_complete")

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description=DESCRIPTION,
        docs_url=settings.docs_url if settings.debug else None,
        redoc_url=None,
        lifespan=lifespan,
    )

    # 中间件顺序：CORS → trace_id → 路由
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(TraceIdMiddleware)

    register_exception_handlers(app)

    # 健康检查（不进 /api/v1 前缀，便于容器与运维探活）
    @app.get("/health", tags=["system"], summary="健康检查")
    async def health() -> ApiResponse[dict[str, Any]]:
        """健康检查：应用 / 数据库 / **核心唯一约束**是否健在。

        ★ `constraints` 是 QA-01 / QA-02 的「保护可验证」要求：
          `one_to_many` 与 `duplicate_item` 靠部分唯一索引
          `uq_sku_mapping_shop_sku` 在写入边界阻断，读时扫描 SQL 已删除。
          索引一旦被误删，冲突检测会安静地返回 0（不报错、只静默失效），
          所以这里必须能**立刻**查到它还在不在。
        """
        db_ok = True
        try:
            engine = get_engine(settings)
            async with engine.connect() as conn:
                from sqlalchemy import text

                await conn.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            db_ok = False
            logger.error("health_db_check_failed", error=str(exc))

        constraint_issues: list[dict[str, Any]] = []
        required_count = 0
        try:
            from app.core.database import get_session_factory
            from app.services.bootstrap import REQUIRED_CONSTRAINTS, check_required_constraints

            required_count = len(REQUIRED_CONSTRAINTS)
            factory = get_session_factory()
            async with factory() as health_session:
                constraint_issues = await check_required_constraints(health_session)
        except Exception as exc:  # noqa: BLE001  约束自检失败不得让探活 500
            logger.error("health_constraint_check_failed", error=str(exc))
            constraint_issues = [
                {
                    "name": "constraint_check",
                    "table": "",
                    "reason": f"约束自检执行失败：{exc}",
                    "expected": "",
                    "found": "",
                }
            ]

        # ★ 调度器健康（复用既有 /health 通道，不新造告警通道）：
        #   定时提交失败时**没有 TaskRecord 落库**，任务恢复与看门狗都扫不到它，
        #   只有这里能把"本轮被静默跳过"暴露出来。
        scheduler_health: dict[str, Any] = {"status": "unknown", "running": False, "unhealthy": False}
        try:
            from app.tasks.scheduler import get_scheduler_health

            scheduler_health = get_scheduler_health()
        except Exception as exc:  # noqa: BLE001  调度器自检失败不得让探活 500
            logger.error("health_scheduler_check_failed", error=str(exc))
            scheduler_health = {
                "status": "unknown",
                "running": False,
                "unhealthy": False,
                "reason": f"调度器自检执行失败：{exc}",
            }

        constraints_ok = not constraint_issues
        scheduler_unhealthy = bool(scheduler_health.get("unhealthy"))
        status_value = "ok" if (db_ok and constraints_ok and not scheduler_unhealthy) else "degraded"
        return ApiResponse.ok(
            data={
                "status": status_value,
                "app": settings.app_name,
                "env": settings.app_env,
                "database": "ok" if db_ok else "down",
                "dialect": settings.db_dialect,
                "constraints": {
                    "ok": constraints_ok,
                    "required": required_count,
                    "missing": constraint_issues,
                },
                "scheduler": scheduler_health,
            }
        )

    # 挂载业务路由（T-A07 交付；未实现时降级为占位，保证应用可启动）
    _mount_api_router(app, settings)

    # ★ 挂载前端静态资源（容器 / 单机部署形态；桌面 exe 已废弃）
    #   必须在 _mount_api_router **之后**：挂载到 "/" 会兜住所有未被上面命中路径，
    #   顺序反了会把 /api/v1/* 一起吞掉。
    _mount_frontend(app)

    logger.info("application_created", app=settings.app_name, env=settings.app_env)
    return app


def _mount_api_router(app: FastAPI, settings: Settings) -> None:
    """挂载 /api/v1 总路由。

    ★ 采用 try/except ImportError：T-A07 完成前应用也能启动（便于分阶段验证）。
    """
    try:
        from app.api.router import api_router  # type: ignore[import-not-found]

        app.include_router(api_router, prefix=settings.api_prefix)
        logger.info("api_router_mounted", prefix=settings.api_prefix)
    except ImportError:
        logger.warning(
            "api_router_not_ready",
            message="app.api.router 尚未实现（T-A07），当前仅暴露 /health 与 /docs",
        )

        @app.get(f"{settings.api_prefix}/_ping", tags=["system"], summary="API 层占位探活")
        async def api_ping() -> ApiResponse[dict[str, str]]:
            """API 层占位探活（路由层实现后自动失效）。"""
            return ApiResponse.ok(data={"message": "API 层尚未实现（T-A07）"})


def _resolve_web_dist() -> Path | None:
    """定位前端构建产物目录 `web/dist`，找不到返回 `None`。

    四处候选，按优先级：环境变量 > PyInstaller 解包目录 > 源码树约定 > 当前工作目录。

    ★ 为什么打包形态必须靠 `sys._MEIPASS`：PyInstaller 会把数据文件解包到临时目录，
      源码树约定路径（`backend/app/main.py` 上溯两级）在打包环境下**根本不存在**，
      只按约定找的结果是 exe 起来后浏览器打开一片空白，且日志里没有任何线索。

    ⚠️ 桌面 exe 形态**已废弃**（当前唯一交付形态是 Docker 容器，见 README 第二节第 6 步），
      上面这条 `sys._MEIPASS` 分支今后不会再被走到，保留只是不主动破坏历史环境。
      容器形态走的是"源码树约定"这一条：镜像里 `/app/backend/app/main.py` → `/app/web/dist`。
    """
    candidates: list[Path] = []

    env_dir = os.getenv("ERP_WEB_DIST")
    if env_dir:
        candidates.append(Path(env_dir))

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "web" / "dist")

    # 源码树约定：backend/app/main.py → parents[0]=app, [1]=backend, [2]=项目根
    candidates.append(Path(__file__).resolve().parents[2] / "web" / "dist")
    candidates.append(Path.cwd() / "web" / "dist")

    for candidate in candidates:
        try:
            if (candidate / "index.html").is_file():
                return candidate
        except OSError:
            continue
    return None


# 静态资源扩展名。带这些后缀的路径 404 时**绝不**回退成 index.html，
# 详见 `_SpaStaticFiles` 的说明。
STATIC_ASSET_SUFFIXES = frozenset(
    {
        ".js", ".mjs", ".cjs", ".css", ".map", ".json",
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp", ".avif",
        ".woff", ".woff2", ".ttf", ".eot", ".otf",
        ".mp4", ".webm", ".mp3", ".wav", ".pdf",
    }
)


class _SpaStaticFiles(StaticFiles):
    """支持 SPA 深链回退的静态资源服务。
    前端用 React Router，直接访问 `/publish-tasks` 这类深链时磁盘上没有对应文件，
    标准 `StaticFiles` 会返回 404。这里只对**无扩展名的路由路径**回退到 `index.html`。

    ★★ 为什么必须按扩展名区分（这里踩过一次）：
      若对所有 404 一律回退 index.html，那么缺失的 js / css 也会拿到一份 HTML 并被
      浏览器当成脚本 / 样式执行，报出 `Unexpected token '<'` 这类**完全指不出真正病根**
      的错误 —— 真正的问题（构建产物不完整 / 资源路径写错）被彻底掩盖。
      这恰恰是本项目反复提防的失效模式：「前端看起来崩了，但没人知道是资源没打包进去」。
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            # 带静态资源扩展名 ⇒ 缺了就是真缺了，如实 404，不回退。
            if PurePosixPath(path).suffix.lower() in STATIC_ASSET_SUFFIXES:
                raise
            index = Path(str(self.directory)) / "index.html"
            if index.is_file():
                return FileResponse(str(index))
            raise


def _mount_frontend(app: FastAPI) -> None:
    """挂载前端 SPA 静态资源（容器 / 单机部署形态；桌面 exe 已废弃）。

    ★ fail-safe 是硬要求：**dist 不存在时必须静默跳过，绝不抛异常**。
      开发态通常没构建前端，这里一旦抛错，`import app.main` 与整个 pytest 套件会全线崩溃 ——
      "为了打包而加的适配把正常开发态弄挂"，是典型的本末倒置。
    """
    dist = _resolve_web_dist()
    if dist is None:
        logger.info(
            "frontend_dist_absent",
            message="未检测到前端构建产物，本次仅提供 API（开发态 / 前后端分离部署）",
        )
        return

    app.mount("/", _SpaStaticFiles(directory=str(dist), html=True), name="frontend")
    logger.info("frontend_mounted", dist=str(dist))


def _start_runner() -> Any:
    """启动全局 TaskRunner。"""
    try:
        from app.tasks.runner import get_task_runner

        runner = get_task_runner()
        logger.info("task_runner_ready", runner=type(runner).__name__)
        return runner
    except Exception as exc:  # noqa: BLE001
        logger.error("task_runner_start_failed", error=str(exc))
        return None


def _mask_db_url(url: str) -> str:
    """数据库 URL 脱敏（避免密码进日志）。"""
    if "://" not in url:
        return url
    scheme, _, rest = url.partition("://")
    if "@" in rest:
        rest = "***@" + rest.split("@", 1)[1]
    return f"{scheme}://{rest}"


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
    )
