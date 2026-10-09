# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：Windows 桌面 exe（onedir 形态）。

在 `backend/` 目录下执行（前端需先构建出 `../web/dist`）：

    pyinstaller --noconfirm --clean ../packaging/erp-windows.spec

产物：`backend/dist/erp/erp.exe`

★ 为什么放在 `packaging/` 而不是 `build/`：
  项目 .gitignore 里有 `build/` 这条通用规则（Python 构建产物约定），
  放在 `build/` 下会被静默排除、永不入库 —— CI 上表现为
  `ERROR: Spec file not found!`，而本地一切正常，极难定位。
  已经踩过一次，不要挪回去。

★ 为什么用 onedir 而不是 onefile：
  onefile 每次启动都要把整个依赖树解压到临时目录 —— 本项目依赖较多，
  解压既慢又容易被杀毒软件当成可疑行为拦截。onedir 启动直接、排错也更容易，
  代价是多一个目录，用 zip 分发即可。

★ 为什么 `--collect-all` 式的全量子模块是必要的：
  本项目大量使用工厂 + 注册表 + 动态 import（`app/adapters/*/factory.py`、
  `app/tasks/handlers/*` 都是运行时按名字加载），静态依赖分析**必然漏**，
  漏了之后的表现是「exe 能起来，一用某功能就 ModuleNotFoundError」——
  这种"构建成功但用不了"正是本项目最反对的失败形态。
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821  PyInstaller 注入
BACKEND = ROOT / "backend"


def submodules_or_empty(pkg: str) -> list:
    """单个包搜集失败不该让整次打包崩掉（缺的是某个可选依赖时尤其重要）。"""
    try:
        return collect_submodules(pkg)
    except Exception:  # noqa: BLE001
        return []


hiddenimports: list = []
# app.* 全量子模块（工厂/动态 import 的兜底）
hiddenimports += submodules_or_empty("app")
hiddenimports += [
    # Web 服务
    "uvicorn.logging",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.loops.auto",
    # 数据库
    "sqlite3",
    "aiosqlite",
    "greenlet",
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.pool",
    # 迁移（可选但带上，便于现场排錯）
    "alembic",
    "alembic.command",
    "alembic.autogenerate",
    # 任务调度
    "apscheduler.schedulers.background",
    "apscheduler.executors.pool",
    "apscheduler.triggers.cron",
    "apscheduler.triggers.interval",
    # 凭证加密
    "cryptography.fernet",
    # 邮件 / MIME（部分依赖会按需引用）
    "email.mime.multipart",
    "email.mime.text",
]
for _pkg in ("structlog", "httpx", "apscheduler", "pydantic", "uvicorn", "sqlalchemy"):
    hiddenimports += submodules_or_empty(_pkg)

datas: list = []

# 前端产物：缺失就明确失败，绝不静默产出一个打开是空白的 exe
web_dist = ROOT / "web" / "dist"
if not (web_dist / "index.html").is_file():
    raise SystemExit(f"未找到前端产物 {web_dist}，请先执行 `npm run build`")
datas.append((str(web_dist), "web/dist"))

# 包内数据文件（适配器 profile YAML 等）
datas += collect_data_files("app")

# alembic 配置与迁移脚本
if (BACKEND / "alembic.ini").is_file():
    datas.append((str(BACKEND / "alembic.ini"), "."))
if (BACKEND / "alembic").is_dir():
    datas.append((str(BACKEND / "alembic"), "alembic"))

a = Analysis(  # noqa: F821
    ["desktop_entry.py"],
    pathex=[str(BACKEND)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "pytest",
        "_pytest",
        "IPython",
        "jupyter",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="erp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # ★ 保留控制台：自用 ERP 排错优先，日志必须看得见
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="erp",
)
