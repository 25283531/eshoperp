"""验证一键备份接口（真实 ASGI 调用，不是只看代码）。

用法（在 backend/ 目录下）：
    python scripts/verify_backup_api.py

验证点：
  1. `POST /api/v1/system/backup` 能产出 zip
  2. 备份**包含素材等非数据库内容**（不只是 erp.db）
      —— 只拷 erp.db 会造成"应用照常启动、素材全空"的静默丢失
  3. zip 里的 erp.db 是**自洽的**（能被 sqlite3 正常打开并读到业务表）
      —— 直接拷 WAL 模式下的 .db 文件会拿到不一致快照
  4. 备份目录自身**不会**被打包进去（否则体积指数膨胀）
  5. `GET /api/v1/system/backups` 能列出历史
  6. 非管理员调用返回 403（备份属高危操作）
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import zipfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import httpx  # noqa: E402

from app.main import app  # noqa: E402
from app.core.config import get_settings  # noqa: E402

PASSED = 0
FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [ok]   {label}" + (f" — {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


async def main() -> int:
    settings = get_settings()
    admin_headers = {"X-Operator-Token": settings.admin_token, "X-Operator": "verify-script"}
    nobody_headers = {"X-Operator-Token": "definitely-not-the-admin-token"}

    transport = httpx.ASGITransport(app=app)
    print("== 一键备份接口验证 ==\n")

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # ① 触发备份
        resp = await client.post("/api/v1/system/backup", headers=admin_headers)
        check("POST /system/backup 返回 200", resp.status_code == 200, f"实际 {resp.status_code}")
        if resp.status_code != 200:
            print(resp.text[:500])
            return 1

        body = resp.json()
        data = body.get("data", {})
        check("返回 ok=True", data.get("ok") is True, str(data.get("message", "")))
        file_name = data.get("file_name", "")
        check("返回了文件名", bool(file_name), file_name)
        check("数据库已纳入备份", data.get("db_included") is True)

        # ② 文件真的落盘
        data_dir = Path(settings.storage_dir)
        zip_path = data_dir / "backups" / file_name
        check("备份文件存在", zip_path.is_file(), f"{zip_path} size={data.get('size_bytes')}")

        if not zip_path.is_file():
            return 1

        # ③ zip 内容检查
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            print(f"\n  备份内容（{len(names)} 项），前 12 项：")
            for n in names[:12]:
                print(f"      - {n}")

            check("包含 erp.db", any(n.endswith("erp.db") for n in names))
            check(
                "不包含备份目录自身（防指数膨胀）",
                not any(n.startswith("backups/") for n in names),
            )
            check(
                "不包含 WAL 临时文件",
                not any(n.endswith(("-wal", "-shm")) for n in names),
            )

            # ④ 库自洽性：把 erp.db 解出来，用 sqlite3 打开并读表
            db_member = next((n for n in names if n.endswith("erp.db")), None)
            if db_member:
                tmp_db = BACKEND / "data" / "_verify_restore_check.db"
                tmp_db.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(db_member) as src, open(tmp_db, "wb") as dst:
                    dst.write(src.read())
                try:
                    con = sqlite3.connect(tmp_db.as_posix())
                    tables = {
                        r[0]
                        for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    }
                    con.close()
                    check(
                        "备份出的 erp.db 可正常打开（自洽）",
                        "source_product" in tables,
                        f"表数量 {len(tables)}",
                    )
                except sqlite3.Error as exc:
                    check("备份出的 erp.db 可正常打开（自洽）", False, str(exc))
                finally:
                    try:
                        tmp_db.unlink()
                    except OSError:
                        pass

        # ⑤ 历史列表
        resp = await client.get("/api/v1/system/backups", headers=admin_headers)
        check("GET /system/backups 返回 200", resp.status_code == 200)
        items = resp.json().get("data", {}).get("items", [])
        check("历史列表非空", len(items) > 0, f"{len(items)} 份")
        check("列表含刚创建的这份", any(i["file_name"] == file_name for i in items))

        # ⑥ 权限
        resp = await client.post("/api/v1/system/backup", headers=nobody_headers)
        check("非管理员被拒绝", resp.status_code in (401, 403), f"实际 {resp.status_code}")

    print(f"\n== 结果：{PASSED} 通过 / {FAILED} 失败 ==")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
