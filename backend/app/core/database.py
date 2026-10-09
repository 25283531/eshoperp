"""异步数据库引擎与会话管理。

设计要点（ADR-2 / N4）：
    * 默认 SQLite 文件库 `data/erp.db`，DATABASE_URL 可切 PostgreSQL（asyncpg）；
    * SQLite 启用 WAL 模式 + busy_timeout + foreign_keys，缓解单机并发写锁冲突；
    * 统一通过 `get_db()` 依赖注入 AsyncSession，禁止各处自行 create_engine。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool, StaticPool

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger("app.database")

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

#: WAL 是 SQLite 的**持久化**属性（写进库文件头），设一次即可；
#: 但 `build_async_engine` / Alembic 这类独立引擎不会走 `get_engine()` 的监听器，
#: 故仍需在每条连接上设一次，保证"新建的库"第一次被打开时就是 WAL。
WAL_JOURNAL_MODE = "wal"


def _sqlite_pragmas(dbapi_connection: Any, busy_timeout_ms: int = 8000) -> None:
    """SQLite 连接级 PRAGMA：WAL + busy_timeout + 外键约束。

    注意：这里是**唯一**设置 PRAGMA 的地方，`get_engine()` 与 `build_async_engine()`
    共用，避免两处各写一份、改了一处忘了另一处。
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"PRAGMA journal_mode={WAL_JOURNAL_MODE}")
        cursor.execute("PRAGMA foreign_keys=ON")
        # WAL 模式下 NORMAL 已足够安全（崩溃最多丢最后一个 checkpoint 之后的事务）
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    finally:
        cursor.close()


def _attach_sqlite_pragmas(engine: AsyncEngine, busy_timeout_ms: int = 8000) -> None:
    """给**文件型** SQLite 引擎挂上 PRAGMA 监听器（内存库不需要，跳过）。

    PRAGMA 失败**不阻断启动**，但必须**留痕**：WAL 设不上意味着并发写仍会互相阻塞，
    这是"静默降级"，以前 `except: pass` 让运维完全看不到。
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _on_sqlite_connect(dbapi_connection: Any, _record: Any) -> None:  # noqa: ANN401
        try:
            _sqlite_pragmas(dbapi_connection, busy_timeout_ms=busy_timeout_ms)
        except Exception as exc:  # noqa: BLE001  PRAGMA 失败不应阻断启动，但必须可见
            logger.warning("sqlite_pragma_failed", error=f"{type(exc).__name__}: {exc}")


def _ensure_sqlite_parent_dir(url: str) -> None:
    """确保 SQLite 文件所在目录存在（避免首次启动时目录不存在而报错）。"""
    if not url.startswith("sqlite"):
        return
    # sqlite+aiosqlite:///C:/path/data/erp.db  →  取 /// 之后的部分
    _, _, path_part = url.partition("///")
    if not path_part:
        return
    if path_part.startswith(":memory:") or "mode=memory" in path_part:
        return
    Path(path_part).parent.mkdir(parents=True, exist_ok=True)


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """获取（或惰性创建）全局异步引擎。"""
    global _engine
    if _engine is not None:
        return _engine

    settings = settings or get_settings()
    url = settings.database_url
    _ensure_sqlite_parent_dir(url)

    if url.startswith("sqlite"):
        # SQLite：单写者模型，使用 NullPool 减少跨线程/跨事件循环的连接复用风险
        engine = create_async_engine(
            url,
            echo=settings.db_echo,
            future=True,
            poolclass=NullPool,
            connect_args={"check_same_thread": False, "timeout": settings.sqlite_busy_timeout_ms / 1000},
        )
        # ★ NullPool 是特意选的：单写者模型下不复用跨事件循环的连接。别顺手改掉。
        _attach_sqlite_pragmas(engine, busy_timeout_ms=int(settings.sqlite_busy_timeout_ms))

    elif url.startswith("postgres"):
        engine = create_async_engine(
            url,
            echo=settings.db_echo,
            future=True,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
        )
    else:
        engine = create_async_engine(url, echo=settings.db_echo, future=True, pool_pre_ping=True)

    _engine = engine
    return engine


def get_session_factory(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    """获取（或惰性创建）全局异步会话工厂。"""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(settings),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


def set_engine(engine: AsyncEngine) -> None:
    """显式替换全局引擎（测试 / Alembic 环境使用）。"""
    global _engine, _session_factory
    _engine = engine
    _session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)


async def dispose_engine() -> None:
    """释放引擎连接池（应用关闭 / 测试收尾）。"""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：为每个请求提供一个 AsyncSession，退出时自动关闭。"""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


class DatabaseSession:
    """手动管理生命周期的会话上下文（供同步/线程任务与脚本使用）。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings
        self.session: AsyncSession | None = None

    async def __aenter__(self) -> AsyncSession:
        self.session = get_session_factory(self._settings)()
        return self.session

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.session is None:
            return
        try:
            if exc_type is not None:
                await self.session.rollback()
        finally:
            await self.session.close()
            self.session = None


async def init_db(settings: Settings | None = None) -> None:
    """启动时初始化数据库：建目录 + 连通性检查 + SQLite PRAGMA 生效验证。

    注意：正式建表由 `alembic upgrade head` 负责，此处不做 create_all，
    避免模型与迁移脚本漂移。
    """
    settings = settings or get_settings()
    Path(settings.storage_dir).mkdir(parents=True, exist_ok=True)
    engine = get_engine(settings)
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
        if settings.is_sqlite:
            await conn.execute(text(f"PRAGMA busy_timeout={int(settings.sqlite_busy_timeout_ms)}"))
            await _log_sqlite_journal_mode(conn)


async def _log_sqlite_journal_mode(conn: Any) -> None:
    """启动时把 SQLite **实际生效**的 journal_mode 打进日志。

    WAL 是"低成本高收益"项，但只有**真的生效**才算数：网络盘 / 只读目录 /
    被他人持锁时 `PRAGMA journal_mode=WAL` 会静默退回 `delete`。
    这里回读一次并留痕，让"WAL 到底开没开"可查，而不是靠猜。
    """
    try:
        mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
    except Exception as exc:  # noqa: BLE001  自检失败不应阻断启动
        logger.warning("sqlite_journal_mode_check_failed", error=f"{type(exc).__name__}: {exc}")
        return
    mode_text = str(mode or "").lower()
    if mode_text == WAL_JOURNAL_MODE:
        logger.info("sqlite_journal_mode", journal_mode=mode_text, wal_enabled=True)
    else:
        logger.warning(
            "sqlite_wal_not_effective",
            journal_mode=mode_text,
            hint="并发写仍会互相阻塞；检查库文件是否在不支持 WAL 的网络盘上，或是否被只读挂载",
        )


def build_async_engine(url: str, echo: bool = False) -> AsyncEngine:
    """根据 URL 构建独立异步引擎（Alembic env.py / 脚本使用）。

    SQLite 内存库使用 StaticPool，保证同一连接内可见已建表。
    """
    if url.startswith("sqlite"):
        if ":memory:" in url or "mode=memory" in url:
            return create_async_engine(
                url,
                echo=echo,
                future=True,
                poolclass=StaticPool,
                connect_args={"check_same_thread": False},
            )
        _ensure_sqlite_parent_dir(url)
        engine = create_async_engine(url, echo=echo, future=True, poolclass=NullPool,
                                     connect_args={"check_same_thread": False})
        # ★ 以前这条分支**没有** PRAGMA：`alembic upgrade head` 建出来的新库
        #   在应用首次连接前一直是 DELETE 模式，冷启动阶段仍会撞写锁。
        _attach_sqlite_pragmas(engine)
        return engine
    return create_async_engine(url, echo=echo, future=True, pool_pre_ping=True)


__all__ = [
    "AsyncSessionLocal",
    "DatabaseSession",
    "build_async_engine",
    "dispose_engine",
    "get_db",
    "get_engine",
    "get_session_factory",
    "init_db",
    "set_engine",
]


class _LazySessionProxy:
    """惰性会话工厂代理：避免模块导入即创建引擎（便于测试与迁移环境）。"""

    def __call__(self) -> AsyncSession:
        return get_session_factory()()

    def __getattr__(self, item: str) -> Any:
        return getattr(get_session_factory(), item)


AsyncSessionLocal: Any = _LazySessionProxy()
