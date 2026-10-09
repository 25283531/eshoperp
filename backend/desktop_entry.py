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
from pathlib import Path

DEFAULT_PORT = 8000
MAX_PORT_TRIES = 20
#: 首次启动要跑 3 个迁移脚本建 24 张表，给足时间；超时视为失败并如实上报
MIGRATION_TIMEOUT_SEC = 180


def pick_port(preferred: int = DEFAULT_PORT, tries: int = MAX_PORT_TRIES) -> int:
    """挑一个空闲端口，从 preferred 开始顺延。

    ★ 端口被占时**必须让使用者看见换了端口**，不能默默连到别的端口上去：
      否则「双击 exe、浏览器开了、但连到的是另一个程序」这种事根本查不出来。
    """
    for port in range(preferred, preferred + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                if port != preferred:
                    print(
                        f"[warn] 端口 {preferred} 已被占用，改用 {port}，请按此地址访问",
                        file=sys.stderr,
                    )
                return port
    raise RuntimeError(f"端口 {preferred}~{preferred + tries - 1} 均被占用，无法启动")


def resolve_preferred_port() -> tuple[int, bool]:
    """读 `ERP_PORT`：返回 (首选端口, 是否被显式指定)。

    ★ 显式指定与缺省走**两套语义**，这是刻意的：

      * 显式指定（CI 冒烟、多人同机协作各占一端口）→ **硬要求**。
        占着就直接报错退出。理由：既然人/脚本明确说了"我要 8000"，
        偷偷漂到 8001 会让校验脚本连到**别的进程**上，
        得到一个看着像 exe 实际是别人的响应 ——
        这种"测试通过但测的不是被测物"比直接失败危险得多，
        而且排查时日志完全看不出异常。
      * 未指定（双击 exe 的日常场景）→ **尽力而为**。
        顺延到空闲端口并打印醒目提示（见 `pick_port`）。
        自用场景下不因为端口冲突就拒绝启动，属于可用性优先的合理取舍。
    """
    raw = os.getenv("ERP_PORT")
    if raw is None or raw.strip() == "":
        return DEFAULT_PORT, False
    try:
        return int(raw.strip()), True
    except ValueError as exc:
        raise SystemExit(f"ERP_PORT 不是合法端口号：{raw!r}") from exc


def bind_preferred_port(port: int) -> socket.socket:
    """占用首选端口；被占时抛错（仅用于 `ERP_PORT` 显式指定的场景）。

    ★ **不要**在这里设 `SO_REUSEADDR`：Windows 上该选项的语义与 Unix 不同，
      它允许多个套接字绑定**同一个**地址（即所谓的端口劫持），
      结果就是这个探针在端口已被别人 LISTENING 的情况下**照样绑定成功** ——
      检测形同虚设，反而给出"端口可用"的假结论。不设它，被占时才会真的抛错。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError as exc:
        sock.close()
        raise SystemExit(
            f"ERP_PORT 指定的端口 {port} 已被占用，请换一个端口或先停掉占用它的进程。\n"
            f"  （占用者：{exc}）"
        ) from exc
    return sock


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


def _bundle_root() -> Path | None:
    """冻结环境的资源根目录（PyInstaller 注入的 `sys._MEIPASS`），源码态返回 None。"""
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else None


def _resolve_alembic_ini() -> Path | None:
    """定位 `alembic.ini`：先冻结产物内部，再源码树。"""
    candidates: list[Path] = []
    root = _bundle_root()
    if root is not None:
        candidates.append(root / "alembic.ini")  # spec 里 datas 的目的地是 "."
    candidates.append(Path(__file__).resolve().parent / "alembic.ini")
    for path in candidates:
        if path.is_file():
            return path
    return None


def should_auto_migrate() -> tuple[bool, str]:
    """决定本次启动是否自动建库 / 迁移，返回 (是否执行, 原因)。

    ★ 为什么要显式决策而不是无条件跑：
      自动跑 DDL 的对象是**别人的数据库**时，这件事就从"贴心"变成"越界"。
      所以这里划一条清楚的线：**只对我们自己这个 appliance 的 SQLite 文件动手**，
      其余情况一律不动，但**必须说出口**（静默跳过比报错更危险）。
    """
    explicit = os.getenv("ERP_AUTO_MIGRATE")
    if explicit == "1":
        return True, "由环境变量 ERP_AUTO_MIGRATE=1 显式启用"
    if explicit not in (None, ""):
        return False, f"由环境变量 ERP_AUTO_MIGRATE={explicit!r} 显式关闭"
    if getattr(sys, "frozen", False):
        return True, "打包产物默认自助建库（首次启动必须能直接用）"
    return False, "源码态默认不自动迁移，请按 README 第二节第 3 步执行 `alembic upgrade head`"


def _schema_exists_without_stamp(url: str) -> bool:
    """判断「业务表已存在但没有迁移版本记录」。

    这是从 create_all 时代过渡到 alembic 管理的库会遇到的状态。
    只看 SQLite —— 外部数据库的归属判定见 `run_migrations()` 里的说明。
    """
    if not url.startswith("sqlite"):
        return False
    try:
        from sqlalchemy import create_engine, inspect, text

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
                stamped = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone() is not None
            return not stamped
    except Exception:  # noqa: BLE001  连不上库时保守判定为「需要正常迁移」
        return False
    finally:
        engine.dispose()


def run_migrations() -> bool:
    """把数据库迁到 head。返回是否成功（失败不抛，交由调用方决定处置）。

    ★ 为什么桌面形态必须自己建库：
      开发流程里 "先 `alembic upgrade head` 再启动" 写在 README 第二节第 3 步，
      开发者照做即可。但**双击 exe 的人没有这一步** —— 他机器上 `data/` 是空的，
      于是每张表都不存在，所有接口 500（`no such table: fulfillment_adapter`）。
      "双击即用"是本项目选定的交付形态，那么第一次启动就必须能自己把库建好，
      否则交付的是一个"装完就坏"的东西。

    ★ 为什么走 alembic 而不是 `Base.metadata.create_all()`：
      `create_all()` 会把模型再抄一份成 schema，与迁移脚本**两套事实来源**，
      短期能跑、长期必然漂移（尤其本项目承重的是**部分唯一索引**
      `uq_sku_mapping_shop_sku ... WHERE is_deleted=0`，这种带 WHERE 的索引
      最容易在两套实现之间对不齐，而它一旦缺失，核心风险 ① 的写入边界阻断就失效）。
      迁移脚本是唯一权威，exe 也应该走它。

    ★ 为什么在**独立线程**里跑：
      `alembic/env.py` 的 `run_migrations_online()` 内部就是 `asyncio.run(...)`。
      等 uvicorn 起来后再调它，事件循环已经在跑，`asyncio.run` 会直接抛
      "cannot be called from a running event loop"。
      放在启动**之前**的独立线程里，那个线程还没有事件循环，`asyncio.run` 正常工作。
      （这不是绕问题，是尊重 alembic 同步入口的契约。）
    """
    do_migrate, reason = should_auto_migrate()
    if not do_migrate:
        print(f"[info] 跳过自动建库：{reason}", file=sys.stderr)
        return False

    # ★ 只对 SQLite 自助建库。切到 PostgreSQL 后库是使用者自己的资产，
    #   exe 不该在每次启动时悄悄改它的 schema —— 需要升级请显式跑 `alembic upgrade head`。
    try:
        from app.core.config import get_settings

        settings = get_settings()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 无法读取配置，跳过自动建库：{exc!r}", file=sys.stderr)
        return False

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

    ini = _resolve_alembic_ini()
    if ini is None:
        print(
            "[warn] 未找到 alembic.ini，跳过自动建库。"
            "若这是首次启动，接口会因缺表返回 500。",
            file=sys.stderr,
        )
        return False

    try:
        from alembic import command
        from alembic.config import Config
    except ImportError as exc:  # pragma: no cover - 打包漏打 alembic 时的人肉排错路径
        print(f"[warn] alembic 未打进产物，跳过自动建库：{exc}", file=sys.stderr)
        return False

    cfg = Config(str(ini))
    # alembic 对相对 script_location 的解析与 cwd 相关，冻结后 cwd 不确定 —— 直接给绝对路径
    cfg.set_main_option("script_location", str(ini.parent / "alembic"))

    # ★ 已有 schema 却没有版本记录 → 走「打基线」而不是 upgrade。
    #
    #   背景：早期开发库是用 `Base.metadata.create_all()` 建的（`scripts/seed.py` 即如此），
    #   `alembic_version` 表可能被创建但**从未写入版本号**（本仓库 `env.py` 原先
    #   不提交事务，见上方说明）。修复提交逻辑后，这类库若直接 upgrade，
    #   alembic 会认为它还在 base，于是重跑 0001 ——
    #   撞 `table supplier already exists` 直接崩。
    #
    #   这不是把问题藏起来：这类库的 schema 本就来自当前模型（等价于 head），
    #   正确动作是 `stamp head` 把它纳入版本管理，而不是重放历史。
    #   同时必须**明确告知**，不能悄悄改版本记录。
    if _schema_exists_without_stamp(settings.database_url):
        print(
            "=" * 66 + "\n"
            "  检测到数据库**已有业务表但没有迁移版本记录**。\n"
            "  判断：该库由 create_all 建立，schema 等价于当前模型。\n"
            "  处置：执行 `alembic stamp head` 打基线，**不重放历史迁移**。\n"
            "  若你确认该库 schema 落后于 head，请改为手工执行 alembic upgrade。\n"
            + "=" * 66
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

    # 自动建库：打包产物默认启用 —— "双击即用"的第一次启动必须自己把 schema 建好。
    #   源码态默认关闭：README 第二节第 3 步已把 `alembic upgrade head` 列为标准流程，
    #   开发者按文档走即可，不想被入口悄悄改库；需要时用 ERP_AUTO_MIGRATE=1 打开。
    if should_auto_migrate()[0]:
        run_migrations()

    # 显式指定 ERP_PORT 时按硬要求处理（占着就报错），否则自动顺延并提示
    preferred, explicit = resolve_preferred_port()
    if explicit:
        held = bind_preferred_port(preferred)
        port = preferred
    else:
        held = None
        port = pick_port(preferred)
    url = f"http://127.0.0.1:{port}"

    app = create_app()
    maybe_open_browser(url)

    print("=" * 58)
    print("  自用电商 ERP 已启动")
    print(f"  访问地址：{url}")
    print("  保持本窗口开启即是运行中；关闭窗口即可停止服务")
    print("=" * 58)

    try:
        # 探针只用于检测，用完即放，让 uvicorn 自己绑同一个端口。
        # 释放与重绑之间有微秒级窗口，极端情况下 uvicorn 会报
        # "address already in use" —— 那是**响亮的失败**，好过默默漂到别的端口。
        if held is not None:
            held.close()
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
