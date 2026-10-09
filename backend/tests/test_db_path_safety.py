"""★ 数据库路径安全回归（"空库"事故的防复发闸门）。

事故复盘：
    `.env.example` 曾写着 `DATABASE_URL=sqlite+aiosqlite:///./data/erp.db`（**相对路径**）。
    相对路径随**进程工作目录**漂移 —— 以 `backend/` 为 cwd 启动 uvicorn 时连到的是
    `backend/data/erp.db`：一个 0 表的空库。于是"所有写接口集体 500、只读接口仍 200"，
    而真正的应用库（`<项目根>/data/erp.db`，24 张表）其实一直是好的。

    排查顺序因此被带偏：先怀疑多实例争用、再怀疑任务泄漏事务，绕了两轮。
    教训：**集体性写失败，先验最基础的 —— 库还在不在、表有没有、连的是不是那个库。**

防复发两道闸门：
    1. `Settings._resolve_relative_sqlite()`：任何相对 SQLite 路径强制解析到项目根；
    2. `.env.example` 不再给出相对路径示例（改为注释 + 绝对路径占位）。
"""

from __future__ import annotations

import pytest

from app.core.config import PROJECT_ROOT, Settings


def test_relative_sqlite_url_resolved_to_project_root() -> None:
    """相对路径（`./data/erp.db`、`data/erp.db`）一律钉到项目根。"""
    for raw in ("sqlite+aiosqlite:///./data/erp.db", "sqlite+aiosqlite:///data/erp.db"):
        resolved = Settings(database_url=raw).database_url
        expected = (PROJECT_ROOT / "data" / "erp.db").resolve().as_posix()
        assert resolved.endswith(expected), f"{raw} 应解析到 {expected}，实际 {resolved}"


def test_absolute_sqlite_url_untouched() -> None:
    """绝对路径（含 Windows 盘符）保持原样，不被改写。"""
    raw = "sqlite+aiosqlite:///G:/somewhere/erp.db"
    assert Settings(database_url=raw).database_url == raw


def test_default_database_url_points_to_project_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认值必须指向 `<项目根>/data/erp.db`（与 alembic / seed 一致）。

    注：conftest 会设置 `DATABASE_URL` 指向测试库，这里临时摘掉环境变量再看默认值。
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    default = Settings().database_url
    expected = (PROJECT_ROOT / "data" / "erp.db").resolve().as_posix()
    assert default.endswith(expected), f"默认库路径异常：{default}"


def test_non_sqlite_url_untouched() -> None:
    """PostgreSQL 等 URL 不受影响。"""
    raw = "postgresql+asyncpg://user:pwd@localhost:5432/erp"
    assert Settings(database_url=raw).database_url == raw
