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

★ 为什么 `app.*` 必须全量收进 hiddenimports：
  本项目大量使用工厂 + 注册表 + 动态 import（`app/adapters/*/factory.py`、
  `app/tasks/handlers/*` 都是运行时按名字加载），静态依赖分析**必然漏**，
  漏了之后的表现是「exe 能起来，一用某功能就 ModuleNotFoundError」——
  这种"构建成功但用不了"正是本项目最反对的失败形态。
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821  PyInstaller 注入
BACKEND = ROOT / "backend"
ENTRY = BACKEND / "desktop_entry.py"

# ★★★ 这一行是整个 spec 的命门，曾经因为缺它连续挂掉三次 CI ★★★
#
# spec 本质是一段被 exec 的 Python 代码，执行时 sys.path[0] 是
# **spec 文件所在目录**（packaging/），**不是** 当前工作目录（backend/）。
# 因此即便在 backend/ 下运行，`app` 这个包也解析不到 ——
# `collect_submodules("app")` / `collect_data_files("app")` 全部失效。
#
# 更糟的是它失效得**很安静**：
#   collect_submodules 抛异常 → 被 submodules_or_empty 吞掉 → 返回 []
#   collect_data_files  打一行 WARNING → 返回 []
# 于是构建全绿，产物却缺了 109 个业务模块和 2 个适配器 profile YAML，
# 直到运行时才炸。宁可在这里显式插路径并断言，也不要留这种哑弹。
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# 入口脚本先校验，给出人能读懂的报错。
# PyInstaller 自己的报错是 `ERROR: script 'xxx' not found`，
# 既不说是谁算出来的路径，也不说应该在哪，排查成本极高。
if not ENTRY.is_file():
    raise SystemExit(
        f"未找到打包入口脚本：{ENTRY}\n"
        f"  ROOT    = {ROOT}      （由 SPECPATH 上溯一级得到）\n"
        f"  BACKEND = {BACKEND}\n"
        "若这两个值不对，检查 spec 是否被移到别的位置。"
    )


def submodules_or_empty(pkg: str) -> list:
    """单个包搜集失败不该让整次打包崩掉（缺的是某个可选依赖时尤其重要）。"""
    try:
        return collect_submodules(pkg)
    except Exception:  # noqa: BLE001
        return []


def collect_app_modules() -> list:
    """枚举 `backend/app` 下全部 .py 模块，转成 dotted module name。

    不用 `collect_submodules("app")` 的两个理由：

      1. 它会真的 **import** 每一个模块 —— 等于在打包阶段执行业务模块的
         顶层代码（建引擎、读配置、连网络…），副作用不可控；
      2. 它依赖 sys.path 能解析到顶层包名 `app`。见上方 sys.path 注释：
         默认解析不到，而失败又被 try/except 吞成空列表。

    文件系统枚举的结果是确定的：**要么全在，要么构建直接失败**，
    不会出现"少了一半但构建还是绿的"这种最坏情况。
    """
    root = BACKEND / "app"
    if not root.is_dir():
        raise SystemExit(f"未找到后端包目录：{root}")
    mods: list = []
    for py in sorted(root.rglob("*.py")):
        parts = py.relative_to(BACKEND).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            continue
        mods.append(".".join(parts))
    return mods


# 低于这个数说明路径算错了 —— 宁可让构建红掉，也不要产出一个残废 exe
APP_MODULE_MIN = 80

APP_MODULES = collect_app_modules()
if len(APP_MODULES) < APP_MODULE_MIN:
    raise SystemExit(
        f"app 模块枚举结果异常：只找到 {len(APP_MODULES)} 个，"
        f"低于下限 {APP_MODULE_MIN}。通常是 ROOT/BACKEND 路径算错，"
        f"请核对 ROOT={ROOT}"
    )

hiddenimports: list = list(APP_MODULES)
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
# alembic 必须在列：桌面 exe 首次启动要靠 `alembic upgrade head` 自己建库，
# 漏了它的表现是「exe 起来、页面能开、所有接口 500（no such table）」。
for _pkg in (
    "structlog",
    "httpx",
    "apscheduler",
    "pydantic",
    "uvicorn",
    "sqlalchemy",
    "alembic",
):
    hiddenimports += submodules_or_empty(_pkg)


def _covers(entries: list, target: Path) -> bool:
    """判断 target 是否已被 datas 覆盖（命中文件条目，或命中其祖先目录条目）。"""
    try:
        t = str(target.resolve()).lower()
    except Exception:  # noqa: BLE001
        t = str(target).lower()
    for src, _dest in entries:
        try:
            s = str(Path(src).resolve()).lower().rstrip("\\/")
        except Exception:  # noqa: BLE001
            s = str(src).lower().rstrip("\\/")
        if t == s or t.startswith(s + "\\") or t.startswith(s + "/"):
            return True
    return False


datas: list = []

# 前端产物：缺失就明确失败，绝不静默产出一个打开是空白的 exe
web_dist = ROOT / "web" / "dist"
if not (web_dist / "index.html").is_file():
    raise SystemExit(f"未找到前端产物 {web_dist}，请先执行 `npm run build`")
datas.append((str(web_dist), "web/dist"))

# 包内数据文件（适配器 profile YAML 等）
datas += collect_data_files("app")

# ★ 适配器 profile 是承重件，显式兜底，不把命交给 collect_data_files。
#
# `http_client.PROFILES_DIR = Path(__file__).parent / "profiles"` 是硬编码的相对路径，
# 所以目的地必须精确落在 `app/adapters/fulfillment/profiles` 下。
# 少了这两个 YAML，exe 能起来、页面能开，但一切换妙手/逸淘适配器才炸 ——
# 属于最典型的"打包期看不出、运行期才爆"的哑弹，必须在这里钉死。
profiles_dir = BACKEND / "app" / "adapters" / "fulfillment" / "profiles"
REQUIRED_PROFILES = ("miaoshou.yaml", "yitao.yaml")
_missing_profiles = [n for n in REQUIRED_PROFILES if not (profiles_dir / n).is_file()]
if _missing_profiles:
    raise SystemExit(f"适配器 profile 缺失：{_missing_profiles}（目录 {profiles_dir}）")

for name in REQUIRED_PROFILES:
    if not _covers(datas, profiles_dir / name):
        datas.append((str(profiles_dir / name), f"app/adapters/fulfillment/profiles"))

# alembic 配置与迁移脚本
if (BACKEND / "alembic.ini").is_file():
    datas.append((str(BACKEND / "alembic.ini"), "."))
if (BACKEND / "alembic").is_dir():
    datas.append((str(BACKEND / "alembic"), "alembic"))

# ★ 入口脚本给绝对路径。
#   PyInstaller 会把相对路径按 **spec 所在目录** 解析，
#   写 "desktop_entry.py" 会去找 packaging/desktop_entry.py —— 这是第二次 CI 失败的直接原因。
a = Analysis(  # noqa: F821
    [str(ENTRY)],
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
