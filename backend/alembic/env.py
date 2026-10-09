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
config.set_main_option("sqlalchemy.url", os.getenv("DATABASE_URL") or settings.database_url)

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
    """异步模式：创建引擎 → 连接 → run_sync 执行迁移。"""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
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
