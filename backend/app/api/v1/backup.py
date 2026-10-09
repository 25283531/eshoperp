"""一键备份 —— 把整个 `data/` 目录打包成 zip（ARCH §5.5 补充）。

★ 为什么备份的是**整个 `data/` 而不是只拷 `erp.db`**：
  `data/` 里除了 SQLite 主库，还有商品素材图片与 AI 重构产出。
  只拷 `erp.db` 的结果是：应用照常启动、所有接口正常，
  但素材全空 —— **应用不会报错，使用者也不会立刻发现**，
  等要上架时才发现图没了，属于极不易察觉的数据丢失。
  这条规则与 README 第 27 条（桌面形态）完全一致，容器形态照搬。

★ 为什么 SQLite 必须走 `Connection.backup()` 而不是直接读文件：
  库开在 **WAL 模式**下，数据可能还留在 `erp.db-wal` 里未 checkpoint。
  直接 `shutil.copy()` 那个 `.db` 文件，拿到的是**缺少 WAL 内容的不一致快照**，
  极端情况下备份出来的库是坏的，而使用者要等到真需要恢复时才发现。
  `sqlite3.Connection.backup()` 是官方在线备份接口，
  会把主库 + 当前 WAL 一并导出为一个自洽的库文件。

★ 为什么备份产物落在 `data/backups/`：
  这样备份文件**跟着 data/ 一起被迁移**，不会出现"库拷走了、备份没跟上"。
  代价是备份目录自己不能被打进下一份备份（否则指数膨胀）——
  下方 `_iter_source_files()` 显式排除它。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.core.config import get_settings
from app.core.deps import AdminOperator, CurrentOperator
from app.core.response import ApiResponse

router = APIRouter(tags=["备份"])

# 保留份数上限。路由器 Flash 空间有限，备份无限累积会把存储撑爆；
# 超限自动删最旧的，并在返回值里说明删了几个。
MAX_KEEP = 20


def _resolve_data_dir() -> Path:
    """解析数据目录。

    优先用 `STORAGE_DIR`（compose 里设为 `/app/data`），
    回落到配置的 `storage_dir` 默认值。
    """
    settings = get_settings()
    return Path(os.getenv("STORAGE_DIR") or settings.storage_dir)


def _dump_consistent_db(db_path: Path, target: Path) -> bool:
    """用 SQLite 在线备份接口导出一个自洽的库文件。

    Returns:
        True 导出成功；False 表示源库不存在或不是 SQLite（均属可跳过情形）。
    """
    if not db_path.is_file():
        return False
    try:
        src = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        dst = sqlite3.connect(target.as_posix())
        try:
            src.backup(dst)
        finally:
            dst.close()
        return True
    except sqlite3.Error:
        return False
    finally:
        src.close()


def _iter_source_files(data_dir: Path, skip_db_name: str) -> list[tuple[Path, str]]:
    """列出要打包的文件，返回 [(绝对路径, zip 内相对名)]。

    ★ 排除 `backups/` 自身：否则每备份一次就把历史备份再打进去，体积指数膨胀。
    """
    out: list[tuple[Path, str]] = []
    if not data_dir.is_dir():
        return out
    for path in data_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(data_dir).as_posix()
        # 排除备份目录自身；排除 SQLite 的 WAL/SHM 临时文件（主库已由 backup API 导出）
        if rel.split("/", 1)[0] == "backups":
            continue
        if rel.endswith(("-wal", "-shm", "-journal")):
            continue
        if rel == skip_db_name:
            continue  # 主库用导出的一致快照替代
        out.append((path, rel))
    return out


def _prune_old_backups(backup_dir: Path, max_keep: int) -> int:
    """删除超出保留上限的最旧备份，返回删除数量。"""
    if not backup_dir.is_dir():
        return 0
    files = sorted(
        (p for p in backup_dir.glob("erp-backup-*.zip") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
    )
    overflow = len(files) - max_keep
    if overflow <= 0:
        return 0
    removed = 0
    for p in files[:overflow]:
        try:
            p.unlink()
            removed += 1
        except OSError:
            break  # 删不掉就停，不因此让整个备份失败
    return removed


@router.post("/system/backup", summary="一键备份：打包整个 data/ 目录")
async def create_backup(
    _admin: AdminOperator,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """触发一次全量备份。

    ★ 备份失败**不影响主流程**：这里如实返回成功/失败，
      绝不让备份异常冒泡成 500 去打断正常业务请求。
    """
    data_dir = _resolve_data_dir()
    backup_dir = data_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    settings = get_settings()
    db_path: Path | None = None
    if getattr(settings, "is_sqlite", False):
        # sqlite+aiosqlite:////app/data/erp.db → /app/data/erp.db
        raw = settings.database_url.partition("///")[2]
        if raw:
            db_path = Path(raw)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    zip_name = f"erp-backup-{stamp}.zip"
    zip_path = backup_dir / zip_name

    db_name = db_path.name if db_path else ""

    try:
        with tempfile.TemporaryDirectory(prefix="erp-backup-") as tmp_s:
            tmp = Path(tmp_s)
            # ① 先导出一致的库快照（WAL 安全）
            consistent_db: Path | None = None
            if db_path is not None:
                consistent_db = tmp / db_name
                if not _dump_consistent_db(db_path, consistent_db):
                    consistent_db = None

            # ② 打包：一致快照 + data/ 下其余文件
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                if consistent_db is not None:
                    zf.write(consistent_db, db_name)
                for abs_path, rel in _iter_source_files(data_dir, db_name):
                    zf.write(abs_path, rel)
    except Exception as exc:  # noqa: BLE001  备份失败如实上报，不吞
        # 半个 zip 也要清掉，避免使用者误以为有一份可用备份
        try:
            if zip_path.exists():
                zip_path.unlink()
        except OSError:
            pass
        return ApiResponse.fail(
            message=f"备份失败：{exc!r}",
            data={"ok": False},
        )

    removed = _prune_old_backups(backup_dir, MAX_KEEP)

    return ApiResponse.ok(
        data={
            "ok": True,
            "file_name": zip_name,
            # 相对 data/ 的路径，方便使用者直接到宿主机上取
            "path": f"data/backups/{zip_name}",
            "size_bytes": zip_path.stat().st_size,
            "db_included": db_path is not None,
            "pruned": removed,
            "operator": getattr(operator, "name", None) or getattr(operator, "id", None),
        },
        message="备份完成（含素材与 AI 产出，不只是数据库）",
    )


@router.get("/system/backups", summary="备份历史列表")
async def list_backups(
    _admin: AdminOperator,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ApiResponse[dict[str, Any]]:
    """列出已有备份，按时间倒序。"""
    data_dir = _resolve_data_dir()
    backup_dir = data_dir / "backups"

    items: list[dict[str, Any]] = []
    if backup_dir.is_dir():
        files = sorted(
            (p for p in backup_dir.glob("erp-backup-*.zip") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for p in files[:limit]:
            items.append(
                {
                    "file_name": p.name,
                    "path": f"data/backups/{p.name}",
                    "size_bytes": p.stat().st_size,
                    "created_at": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
                }
            )

    return ApiResponse.ok(data={"items": items, "total": len(items), "max_keep": MAX_KEEP})
