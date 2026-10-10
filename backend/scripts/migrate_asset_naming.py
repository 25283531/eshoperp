"""★ 一次性存量迁移：把 `asset.storage_path` 从旧英文命名迁到「中文子目录 + 序号」。

================================================================================
为什么必须跑这一遍（不是"改完新代码就完事"）
================================================================================
`Asset.storage_path` 存的是**真实落盘路径**。命名规则从
    data/assets/raw/{商品ID}/main_00.jpg        （旧）
改成
    data/assets/raw/{商品ID}/主图/01.jpg         （新）
之后，**库里所有旧记录仍指向英文路径**。那些文件一旦被新代码写走 / 或本来就在，
    ⇒ 记录还在、预览 404（悬空引用）。这条脚本就是消掉这个悬空。

================================================================================
方案取舍：为什么选"一次性重命名 + 回写"，而不是"保留兼容读取"
================================================================================
① 兼容读取 = 每个读取点都要记得"先按新路径找，找不到再按旧路径找一次"。
   眼下只有 `assets.py:download` 一处读，看着很便宜；但预览、批量打包、
   ZIP 素材包、未来的导出都会各加一个读取点，**漏一处就是一次 404**。
   这正是"半新半旧"的根源 —— 两套路径规则长期共存，谁也说不清哪个是真相。
② 重命名一次性把真相收敛到一套规则上，旧代码分支可以直接删掉，不留尾巴。
③ 代价可控：存量记录数量极少（本库 2 条），`os.replace()` 同盘原子替换，
   失败只影响单条且不会留下半截文件。
④ 不做跨目录搬家：文件只在**原目录内**下沉到中文子目录
   （`raw/{商品ID}/main_00.jpg` → `raw/{商品ID}/主图/01.jpg`）。
   跨目录搬移会改变"这个目录属于哪个商品"的可追溯性，收益为零、风险不为零。
   新产出的 AI 图才落到新基座 `assets/ai/{商品ID}/`。

用法（默认 dry-run，只打印计划不动手）：
    python scripts/migrate_asset_naming.py
    python scripts/migrate_asset_naming.py --apply
    python scripts/migrate_asset_naming.py --apply --database-url sqlite+aiosqlite:///G:/.../data/erp.db
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from sqlalchemy import select

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.core.database import build_async_engine, dispose_engine, get_session_factory, set_engine  # noqa: E402
from app.models.asset import Asset  # noqa: E402
from app.utils.kit import ASSET_ROLE_DETAIL, ASSET_ROLE_MAIN, asset_path, parse_legacy_asset_name  # noqa: E402


def _resolve(raw: str, storage_dir: Path) -> Path:
    """`storage_path` → 绝对路径（库里混着相对路径，如 `assets/raw/seed-placeholder.jpg`）。"""
    path = Path(str(raw or "").strip())
    if not path.is_absolute():
        path = storage_dir / path
    return path


def _new_index(parsed_index: int) -> int:
    """旧序号 → 新角色内序号（从 1 起，角色不再参与计算）。

    ★ 旧命名**本身就不统一**（这也是它必须被换掉的原因之一）：
        * 1688 采集：`main_00.jpg` + `detail_01..08.jpg`（主图恒 0，详情图跟着全局位置）
        * AI 客户端：`main_01.png` + `detail_01..NN.png`（主图也是 01）
      两类主图序号差 1，若按"主图 +1"换算，`main_01.png` 会变成 `主图/02.png`
      —— 主图凭空变成第 2 张。因此统一按"原样保留、最小 01"换算，
      主图 / 详情图都从 01 起连续，与新旧两头的实际张数一致。
    """
    return max(int(parsed_index), 1)


async def _run(apply: bool) -> int:
    settings = get_settings()
    storage_dir = Path(settings.storage_dir)
    print(f"[配置] database_url = {settings.database_url}")
    print(f"[配置] storage_dir  = {storage_dir}")
    print(f"[模式] {'APPLY（真改名 + 真回写）' if apply else 'DRY-RUN（只打印计划）'}\n")

    factory = get_session_factory()
    migrated: list[str] = []
    already_new: list[str] = []
    no_legacy: list[str] = []
    dangling: list[str] = []
    conflicts: list[str] = []

    async with factory() as session:
        rows = (await session.execute(select(Asset))).scalars().all()
        print(f"[扫描] asset 表共 {len(rows)} 行\n")
        for asset in rows:
            raw = str(asset.storage_path or "")
            old = _resolve(raw, storage_dir)
            parsed = parse_legacy_asset_name(old.name)
            if parsed is None:
                exists = old.exists()
                bucket = already_new if exists else dangling
                bucket.append(f"#{asset.id} 非旧命名（无需迁移）→ {raw}" + ("" if exists else "  ★ 文件不存在"))
                if not exists:
                    no_legacy.append(raw)
                continue

            # ★ 角色以**库里的 asset_type 列**为准（它是记录的真相），
            #   文件名只用来取序号：文件名可以被人改过，列不会。
            role = ASSET_ROLE_MAIN if str(asset.asset_type) == ASSET_ROLE_MAIN else ASSET_ROLE_DETAIL
            new_path = asset_path(old.parent, role=role, index=_new_index(parsed[1]), ext=old.suffix)

            if old.exists():
                if new_path.exists():
                    conflicts.append(f"#{asset.id} 目标已存在，跳过：{new_path}")
                    continue
                print(f"[计划] #{asset.id}  {old.name}  →  {new_path.relative_to(storage_dir).as_posix()}")
                if apply:
                    new_path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(old, new_path)  # ★ 同盘原子替换，不留半截文件
                    asset.storage_path = str(new_path)
                migrated.append(f"#{asset.id} → {new_path}")
            elif new_path.exists():
                # 文件已经在新位置（例如上一次 --apply 跑了一半）：只回写路径
                print(f"[回写] #{asset.id}  文件已在新位置，仅更新 storage_path：{new_path}")
                if apply:
                    asset.storage_path = str(new_path)
                migrated.append(f"#{asset.id} → {new_path}（仅回写）")
            else:
                dangling.append(f"#{asset.id} 旧文件与新版路径都不存在：{raw}")

        if apply:
            await session.commit()

        # ---------- 收尾验收：全表再扫一遍，确认没有悬空引用 ----------
        await session.rollback()
        still = (await session.execute(select(Asset))).scalars().all()
        broken: list[str] = []
        broken_legacy: list[str] = []
        for asset in still:
            if _resolve(str(asset.storage_path or ""), storage_dir).exists():
                continue
            line = f"#{asset.id} {asset.storage_path}"
            broken.append(line)
            # ★ 区分"这次改名改出来的悬空"与"迁移前就悬空"：
            #   旧英文命名 = 改名管辖范围内，出了悬空就是本次迁移的锅；
            #   非旧命名（如 seed 占位记录）本就没有文件，与改名无关。
            if parse_legacy_asset_name(Path(str(asset.storage_path or "")).name) is not None:
                broken_legacy.append(line)
        legacy_left = [
            f"#{a.id} {a.storage_path}"
            for a in still
            if parse_legacy_asset_name(Path(str(a.storage_path or "")).name) is not None
        ]

    # ---------- 磁盘上的孤儿旧文件（无记录引用，只报告不动手） ----------
    assets_dir = Path(settings.assets_dir)
    orphans = sorted(
        str(p)
        for p in assets_dir.rglob("*")
        if p.is_file() and parse_legacy_asset_name(p.name) is not None
    )

    print("\n================ 结果 ================")
    print(f"迁移 / 回写：{len(migrated)} 条")
    for line in migrated:
        print(f"   ✅ {line}")
    print(f"本就是新命名（未动）：{len(already_new)} 条")
    for line in already_new:
        print(f"   · {line}")
    if conflicts:
        print(f"目标冲突（跳过）：{len(conflicts)} 条")
        for line in conflicts:
            print(f"   ⚠️ {line}")
    print(f"\n[验收] 仍是旧英文命名的记录：{len(legacy_left)} 条")
    for line in legacy_left:
        print(f"   ❌ {line}")
    print(f"[验收] 文件不存在的记录（悬空）：{len(broken)} 条"
          f"（其中本次改名管辖范围内 {len(broken_legacy)} 条）")
    for line in broken:
        mark = "★ 改名所致" if line in broken_legacy else "★ 迁移前就悬空（与本次改名无关）"
        print(f"   ❌ {line}   {mark}")
    print(f"[参考] 磁盘上无记录引用的旧命名文件：{len(orphans)} 个（测试残留，不动）")
    for line in orphans[:10]:
        print(f"   · {line}")
    if len(orphans) > 10:
        print(f"   · … 其余 {len(orphans) - 10} 个省略")

    ok = not legacy_left and not broken_legacy
    verdict = (
        "✅ 存量迁移通过：无旧命名残留、且无改名所致的悬空引用"
        if ok
        else "❌ 未通过：仍有旧命名残留或改名所致的悬空引用"
    )
    print(f"\n{verdict}")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="存量 asset 命名迁移（中文子目录 + 序号）")
    parser.add_argument("--apply", action="store_true", help="真正执行（默认只打印计划）")
    parser.add_argument("--database-url", default="", help="覆盖 DATABASE_URL（SQLite 请用绝对路径）")
    args = parser.parse_args()

    if args.database_url:
        set_engine(build_async_engine(args.database_url))

    try:
        return asyncio.run(_run(bool(args.apply)))
    finally:
        asyncio.run(dispose_engine())


if __name__ == "__main__":
    raise SystemExit(main())
