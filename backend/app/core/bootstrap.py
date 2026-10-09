"""启动期建库与迁移 —— 容器 / 桌面形态的**唯一实现**。

★ 为什么这个模块必须存在（而不是把逻辑抄进各个入口脚本）：

  这段逻辑是从 `desktop_entry.py` 平移出来的，而它承载了一段真实事故的修复：
  `alembic/env.py` 原先在外部 connection 下 `begin_transaction()` 是 no-op，
  加上 `connectable.connect()` 退出即关闭连接 ⇒ 未提交事务被 ROLLBACK。
  SQLite 的 DDL 自动提交，于是「表建出来了、`INSERT INTO alembic_version` 却回滚」
  ⇒ 第二次启动重放 0001 ⇒ `table supplier already exists` ⇒ 服务起不来
  （对使用者就是"能用一次，第二天打不开"）。

  若把它抄进容器入口与 exe 入口各一份，两套实现迟早漂移，
  而漂移的表现恰恰又是"能起来、但版本记录不对" —— 最不易察觉的那种。

★ 为什么走 alembic 而不是 `Base.metadata.create_all()`：
  `create_all()` 会把模型再抄一份成 schema，与迁移脚本形成**两套事实来源**，
  短期能跑、长期必然漂移。本项目承重的是**部分唯一索引**
  `uq_sku_mapping_shop_sku ... WHERE is_deleted=0` —— 带 WHERE 的索引
  最容易在两套实现之间对不齐，而它一旦缺失，
  核心风险 ①（SKU 映射错配）的写入边界阻断就失效。
  迁移脚本是唯一权威。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

MIGRATION_TIMEOUT_SEC = 180


def schema_exists_without_stamp(url: str) -> bool:
    """判断「业务表已存在但没有迁移版本记录」。

    这是从 `create_all()` 时代过渡到 alembic 管理的库会遇到的状态
    （`scripts/seed.py` 即如此建库）。只看 SQLite —— 外部数据库的 schema
    归属使用者，我们不替他判定。

    ★ fail-close：任何异常都返回 False（走正常迁移流程），
      不因为探测失败就擅自 `stamp`。
    """
    if not url.startswith("sqlite"):
        return False
    try:
        from sqlalchemy import create_engine, inspect, text
    except Exception:  # noqa: BLE001
        return False

    try:
        engine = create_engine(url.replace("+aiosqlite", ""))
    except Exception:  # noqa: BLE001  引擎建不起来就当不存在，交由后续流程报错
        return False

    try:
        with engine.connect() as conn:
            tables = set(inspect(conn).get_table_names())
            if "source_product" not in tables:
                return False  # 完全没有业务表 → 正常从零迁移
            stamped = False
            if "alembic_version" in tables:
                stamped = (
                    conn.execute(text("SELECT version_num FROM alembic_version")).fetchone() is not None
                )
            return not stamped
    except Exception:  # noqa: BLE001  连不上库时保守判定为「需要正常迁移」
        return False
    finally:
        engine.dispose()


def ensure_database_ready(ini_path: Path) -> bool:
    """把数据库迁到 head。返回是否成功（失败不抛，交由调用方决定处置）。

    ★ 为什么必须放在**独立线程**里跑：
      `alembic/env.py` 的 `run_migrations_online()` 内部就是 `asyncio.run(...)`。
      等 uvicorn 起来后再调它，事件循环已经在跑，
      `asyncio.run` 会直接抛 "cannot be called from a running event loop"。
      放在启动**之前**的独立线程里，那个线程还没有事件循环，`asyncio.run` 正常工作。
      （这不是绕问题，是尊重 alembic 同步入口的契约。）

    Args:
        ini_path: `alembic.ini` 的绝对路径。

    Returns:
        True 表示迁移成功或已打基线；False 表示跳过或失败（调用方应如实提示）。
    """
    if not ini_path.is_file():
        print(
            f"[warn] 未找到 alembic.ini（{ini_path}），跳过自动建库。"
            "若这是首次启动，接口会因缺表返回 500。",
            file=sys.stderr,
        )
        return False

    try:
        from app.core.config import get_settings
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 无法读取配置，跳过自动建库：{exc!r}", file=sys.stderr)
        return False

    settings = get_settings()

    # ★ 只对 SQLite 自助建库。切到 PostgreSQL 后库是使用者的资产，
    #   不替他悄悄改 schema —— 需要升级请显式跑 `alembic upgrade head`。
    if not getattr(settings, "is_sqlite", False):
        print(
            "=" * 66 + "\n"
            "  检测到 DATABASE_URL 不是 SQLite，已**跳过自动建库**。\n"
            "  外部数据库的 schema 变更需要你显式执行：\n"
            "      alembic upgrade head\n"
            "   （这是刻意设计：不替使用者改别人的库）\n" + "=" * 66,
            file=sys.stderr,
        )
        return False

    try:
        from alembic import command
        from alembic.config import Config
    except ImportError as exc:
        print(f"[warn] alembic 不可用，跳过自动建库：{exc}", file=sys.stderr)
        return False

    cfg = Config(str(ini_path))
    # alembic 对相对 script_location 的解析与 cwd 相关 —— 直接给绝对路径
    cfg.set_main_option("script_location", str(ini_path.parent / "alembic"))

    # ★ 已有 schema 却没有版本记录 → 走「打基线」而不是 upgrade。
    #   这类库直接 upgrade 会被 alembic 判定为还在 base，于是重跑 0001，
    #   撞 `table supplier already exists` 直接崩。
    #   这不是把问题藏起来：这类库的 schema 本就来自当前模型（等价于 head），
    #   正确动作是 `stamp head` 把它纳入版本管理，而不是重放历史。
    #   同时必须**明确告知**，不能悄悄改版本记录。
    if schema_exists_without_stamp(settings.database_url):
        print(
            "=" * 66 + "\n"
            "  检测到数据库**已有业务表但没有迁移版本记录**。\n"
            "  判断：该库由 create_all 建立，schema 等价于当前模型。\n"
            "  处置：执行 `alembic stamp head` 打基线，**不重放历史迁移**。\n"
            "  若你确认该库 schema 落后于 head，请改为手工执行 alembic upgrade。\n"
            + "=" * 66,
            file=sys.stderr,
        )
        command.stamp(cfg, "head")
        print("[ok] 已写入基线版本号 head")
        return True

    errors: list[BaseException] = []

    def _work() -> None:
        try:
            command.upgrade(cfg, "head")
        except BaseException as exc:  # noqa: BLE001  迁移失败要如实上报，不吞
            errors.append(exc)

    worker = threading.Thread(target=_work, daemon=True, name="alembic-upgrade")
    worker.start()
    worker.join(timeout=MIGRATION_TIMEOUT_SEC)

    if worker.is_alive():
        print(
            f"[error] 自动建库超时（>{MIGRATION_TIMEOUT_SEC}s），"
            "接口可能不可用，请检查数据库文件是否被占用。",
            file=sys.stderr,
        )
        return False
    if errors:
        print(f"[error] 自动建库失败：{errors[0]!r}", file=sys.stderr)
        return False

    print("[ok] 数据库已就绪（alembic upgrade head）")
    return True
