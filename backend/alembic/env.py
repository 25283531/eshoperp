"""Alembic 异步迁移环境。

数据库 URL 优先取 `DATABASE_URL` 环境变量，其次取 `app.core.config` 的默认值，
保证 `alembic upgrade head` 与应用使用同一个库。
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.core.database import ensure_sqlite_parent_dir
from app.models import Base  # 统一导出，确保 24 张表全部被发现

config = context.config

# 让 alembic.ini 的日志配置生效（若存在）
if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:  # noqa: BLE001  alembic.ini 无 logger 段时忽略
        pass

# 注入数据库 URL（DATABASE_URL > Settings 默认）
settings = get_settings()
db_url = os.getenv("DATABASE_URL") or settings.database_url
config.set_main_option("sqlalchemy.url", db_url)

# ★ 先建目录再连库：迁移路径不经过 `get_engine()`，没有人为我们 mkdir，
#   而"从零建库"恰恰是目录最可能不存在的时刻（首次启动 / 换机器）。
ensure_sqlite_parent_dir(db_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：生成 SQL 脚本，不连接数据库。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """在给定连接上同步执行迁移。"""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=True,  # SQLite 需要 batch 模式支持 ALTER
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """异步模式：创建引擎 → 连接 → run_sync 执行迁移。

    ★★ 为什么必须用 `connectable.begin()` 而不是 `connectable.connect()` ★★

      这是本项目踩过的一个**静默数据一致性缺陷**，改动前务必读懂：

      1. 我们把外部 connection 传给 `context.configure(connection=...)`，
         alembic 因此判定 `_in_external_transaction = True`；
      2. 于是 `context.begin_transaction()` 直接返回 `nullcontext()` ——
         **它既不开启事务，也不负责提交**（见 alembic/runtime/migration.py）；
      3. 而 alembic 的 `SQLiteImpl.transactional_ddl = False`，
         进一步确认"这里不会有任何真实事务边界"；

      结果就是：**事务的提交责任落在调用方**。原先这里用的是
      `async with connectable.connect() as connection:`，
      退出时连接被**关闭而非提交**，SQLite 驱动对未提交事务一律 ROLLBACK。

      表现出来的现象极具迷惑性：
        * `CREATE TABLE ...`（DDL）在 SQLite 驱动里是**自动提交**的 —— 表真的建出来了；
        * 但 `INSERT INTO alembic_version`（DML）落在隐式 BEGIN 里 —— 被回滚了。

      于是「库建好了，却查不到版本号」：第一次启动一切正常，
      第二次启动 alembic 认为还在 base，重跑 0001 →
      `sqlite3.OperationalError: table supplier already exists` → 服务起不来。
      （对 exe 使用者来说就是"双击能用一次，第二天打不开了"。）

      改成 `connectable.begin()` 后，退出 `async with` 会正常 COMMIT，
      版本号随表一起落库，迁移才真正幂等。
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.begin() as connection:
        url = config.get_main_option("sqlalchemy.url") or ""
        if url.startswith("sqlite") and ":memory:" not in url:
            # ★ 迁移是"从零建库"的第一站：此时若不开 WAL，新库会以 DELETE 模式诞生，
            #   应用首次连接前的这段时间里并发写仍会互相阻塞。
            #   WAL 是库文件的持久化属性，设一次即可，后续连接自动继承。
            await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """在线模式入口。"""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
