"""pytest 公共夹具（T-A08）。

要点：
    1. 测试库使用**独立 SQLite 文件**（`data/test_erp.db`），与开发库隔离；
    2. `get_db` 依赖被覆盖为「每用例一个会话，结束回滚」，
       保证用例之间互不污染；
    3. ASGI 传输（`ASGITransport`）**不触发 lifespan**，因此不会启动调度器 / 任务恢复。

环境变量必须在 `import app.*` **之前**设置（pydantic-settings 在导入时读取）。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]

# ★★ 测试库文件名带 PID ★★
#    两个 pytest 进程（例如两个人同时跑回归）若共用同一个 `test_erp.db`，
#    会在 `_init_db()` 里互相 `unlink` 对方正在用的库 ⇒
#    "no such table: xxx" / "database is locked" 这类**假故障**，
#    把真正的结论淹掉。按进程隔离后，各自的库互不干扰。
TEST_DB_PATH = BACKEND_ROOT / "data" / f"test_erp_{os.getpid()}.db"

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB_PATH.as_posix()}"
os.environ["APP_ENV"] = "test"
os.environ["LOG_JSON"] = "false"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["TASK_RECOVERY_ENABLED"] = "false"
os.environ["ADMIN_TOKEN"] = "test-admin-token"
os.environ["OPERATOR_TOKEN"] = "test-operator-token"
os.environ["SECRET_KEY"] = "test-secret-key"

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.database import get_db, get_engine, get_session_factory  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Base  # noqa: E402  导入即注册全部 24 张表

ADMIN_HEADERS = {"X-Operator": "tester", "X-Operator-Token": "test-admin-token"}
OPERATOR_HEADERS = {"X-Operator": "tester", "X-Operator-Token": "test-operator-token"}


def _init_db() -> None:
    """（同步入口）建表 + 灌入默认系统配置 + 基础数据；测试库每个 session 重建一次。

    ★★ 必须幂等：conftest 有可能被**二次导入** ★★
        pytest 在没有 `tests/__init__.py` 时把本文件导入为 `conftest`，
        而用例里写 `from tests.conftest import ADMIN_HEADERS` 会把它**再导入一遍**
        （模块名不同 ⇒ Python 视为两个模块 ⇒ 模块级代码全部重跑）。
        旧实现一进门就 `TEST_DB_PATH.unlink()`，于是**正在使用的测试库被当场删掉**，
        后续所有查询报 `no such table: xxx`；紧接着 `asyncio.run()` 在已运行的
        事件循环里再抛 `RuntimeError`。这两个报错都指向症状、不指向根因，
        极易被误判成"别的用例污染了库"。
        因此这里用进程级环境变量做哨兵：**同一进程内只初始化一次**，
        二次导入直接返回，绝不碰已经建好的库。

    ★ 只 `create_all` 不灌配置会导致 `listing.mode` 等配置项缺失，
      进而让「切换上架模式 / 改配置」这类接口在测试里返回 404 / 8001
      （线上由 alembic 0001 迁移负责灌入 DEFAULT_SETTINGS）。

    ★★ QA-05：这里同时执行 `bootstrap_all()`，与主程序启动期**同一套**逻辑 ★★
      线上 `fulfillment_adapter` / `platform_account` / `credential` 三张表恒为 0 行，
      而 pytest 里却一切正常 —— 因为旧夹具让 GET 与 PUT 共用同一 session，
      第一次 GET 时 flush 出的适配器行在同一个 session 里被后续 PUT 看到了，
      恰好把「行根本没落库」这个缺陷掩盖掉。
      现在测试库按生产的真实状态初始化：**行必须在库里，不靠请求顺手播种**。
    """

    async def _create() -> None:
        from sqlalchemy import select

        from app.models.system import DEFAULT_SETTINGS, SystemSetting

        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        from app.core.database import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
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

        # ★ 基础数据（与主程序 lifespan 一致）：幂等 upsert + **真实 commit**
        async with factory() as boot_session:
            from app.services.bootstrap import bootstrap_all, check_required_constraints

            await bootstrap_all(boot_session, commit=True)
            issues = await check_required_constraints(boot_session)
            if issues:
                raise RuntimeError(
                    "测试库的结构性约束未就位，后续用例结论不可信："
                    + ", ".join(str(i.get("name")) for i in issues)
                )

    # ★ 进程级哨兵：同一进程内只允许真正初始化一次（见上文档字符串）。
    sentinel = os.environ.get("_ERP_TEST_DB_READY")
    if sentinel == str(TEST_DB_PATH):
        return

    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink(missing_ok=True)
    TEST_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(_create())
    os.environ["_ERP_TEST_DB_READY"] = str(TEST_DB_PATH)


_init_db()


@pytest.fixture
async def session() -> AsyncIterator[object]:
    """每个用例一个 `AsyncSession`，结束回滚（不污染其他用例）。"""
    factory = get_session_factory()
    async with factory() as db_session:
        yield db_session
        await db_session.rollback()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """FastAPI 测试客户端 —— ★★ 每个请求一个**独立**会话（与生产 `get_db()` 一致）★★

    ★★ QA-05 的夹具缺陷修复 ★★
        旧夹具把 `get_db` 覆盖成「复用用例的那个 `session`」，
        于是 GET 与 PUT 共用同一会话：GET 里 `session.add()` 但没 commit 的适配器行，
        在同一个会话的后续 PUT 里能被查到 ⇒ **「行根本没落库」这个缺陷被夹具完全掩盖**
        （生产库 `fulfillment_adapter` 恒为 0 行，`PUT config` 恒定 404，
          而 pytest 里却是绿的 —— 这是比缺陷本身更危险的事）。

        现在改为**每请求独立会话**，且只在业务代码显式 `commit()` 时落库，
        与生产行为逐字一致：请求之间不再共享未提交状态。
    """
    app = create_app()
    factory = get_session_factory()

    async def _override_get_db() -> AsyncIterator[object]:
        db_session = factory()
        try:
            yield db_session
        except Exception:
            await db_session.rollback()
            raise
        finally:
            await db_session.close()

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client
    app.dependency_overrides.clear()


@pytest.fixture
def settings() -> object:
    """当前配置对象。"""
    return get_settings()


@pytest.fixture(scope="session", autouse=True)
def _shutdown_task_runner() -> object:
    """★ 会话结束必须关掉任务线程池，否则会在 pytest 关掉捕获后继续打日志。

    实测症状：全量跑时出现 `--- Logging error --- ValueError: I/O operation on
    closed file.` 并伴随 1 条 ERROR —— 用例本身是过的，报错来自**遗留的工作线程**：
    用例提交了 ai_rework 之类的任务，任务在 `ThreadPoolExecutor` 里一直跑
    （file_bridge 最长等 `ai.poll_timeout_sec` 秒），等 pytest 收尾关掉 stdout 捕获后，
    那个线程还要写一条日志 → 往已关闭的文件写 → 报错。
    这会让"绿"的套件看起来是红的，必须关掉线程池。
    """
    yield
    try:
        from app.tasks.runner import shutdown_task_runner

        shutdown_task_runner(wait=False)
    except Exception:  # noqa: BLE001  收尾失败不得影响测试结果
        pass


@pytest.fixture(autouse=True)
def _settings_sanity() -> None:
    """★ 断言测试环境确实指向测试库（防止误连开发库）。"""
    current = get_settings()
    assert "test_erp" in current.database_url and current.database_url.endswith(".db"), (
        f"测试库配置异常：{current.database_url}"
    )
