"""★ 素材命名统一验证：中文子目录 + 序号文件名（`主图/01.jpg`、`详情页/01.jpg`）。

覆盖四条硬要求：
    ① 命名函数单一口径（中文串只在 `utils/kit.py` 一处）；
    ② Windows 真机**真的**建出中文目录 / 读写 / 删除（不是只在内存里拼字符串）；
    ③ ZIP 里中文目录名必须带 UTF-8 标志位，否则 Windows 解压乱码；
    ④ AI 重绘落盘（`persist_result()`）也走同一套命名，且 `storage_path` 与磁盘一致。
"""

from __future__ import annotations

import uuid
import zipfile
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.adapters.ai.base import AiImageResult, AiReworkResult
from app.core.config import get_settings
from app.models.asset import AiTask, AiTaskResult, Asset
from app.models.enums import AiTaskStatus, AiTaskType, AssetOrigin, AssetType
from app.models.source import SourceProduct
from app.services.ai_task_service import AiTaskService
from app.utils.kit import (
    ASSET_ROLE_DETAIL,
    ASSET_ROLE_DIRNAMES,
    ASSET_ROLE_MAIN,
    allocate_asset_path,
    asset_path,
    content_hash_bytes,
    make_zip_package,
    parse_legacy_asset_name,
)

__all__ = ["test_zip_chinese_names_are_utf8_flagged"]


def _uniq(prefix: str) -> str:
    """生成唯一串（避开唯一约束，也避免与别的用例撞目录）。"""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ======================================================================
#  ① 命名函数：唯一口径
# ======================================================================


def test_asset_path_is_chinese_dir_plus_seq() -> None:
    """`asset_path()`：主图 / 详情页各占一个中文子目录，文件名是两位序号。"""
    base = Path("data/assets/raw") / "123456"
    assert asset_path(base, role=ASSET_ROLE_MAIN, index=1, ext=".jpg") == base / "主图" / "01.jpg"
    assert asset_path(base, role=ASSET_ROLE_DETAIL, index=2, ext=".png") == base / "详情页" / "02.png"
    # 扩展名不带点也认；空 / 0 序号抬到 01；脏角色值一律按详情页处理（绝不生成第三个目录）
    assert asset_path(base, role=ASSET_ROLE_MAIN, index=0, ext="jpg").name == "01.jpg"
    assert asset_path(base, role="", index=3, ext=".jpg").parent.name == ASSET_ROLE_DIRNAMES[ASSET_ROLE_DETAIL]
    assert asset_path(base, role="乱七八糟", index=1).parent.name == ASSET_ROLE_DIRNAMES[ASSET_ROLE_DETAIL]


def test_parse_legacy_asset_name_roundtrip() -> None:
    """旧英文命名反解（只给存量迁移用）。"""
    assert parse_legacy_asset_name("main_00.jpg") == (ASSET_ROLE_MAIN, 0)
    assert parse_legacy_asset_name("detail_07.png") == (ASSET_ROLE_DETAIL, 7)
    assert parse_legacy_asset_name("主图/01.jpg") is None
    assert parse_legacy_asset_name("01.jpg") is None
    assert parse_legacy_asset_name("") is None


# ======================================================================
#  ② Windows 真机：中文目录真的建得出来、读得回来、删得掉
# ======================================================================


def test_chinese_dirs_really_written_and_read_on_windows(tmp_path: Path) -> None:
    """在本机（Windows）真实创建 / 读取 / 删除 `主图/01.jpg`、`详情页/01.jpg`。"""
    base = tmp_path / _uniq("商品")
    main_file = allocate_asset_path(base, role=ASSET_ROLE_MAIN, index=1, ext=".jpg")
    detail_file = allocate_asset_path(base, role=ASSET_ROLE_DETAIL, index=1, ext=".jpg")

    main_file.write_bytes(b"\xff\xd8\xff-MAIN")
    detail_file.write_bytes(b"\xff\xd8\xff-DETAIL")

    # ★ 真的落到盘上，且分属两个不同子目录
    assert main_file.exists() and detail_file.exists()
    assert main_file.parent.name == "主图" and detail_file.parent.name == "详情页"
    assert main_file.parent != detail_file.parent
    assert main_file.read_bytes() == b"\xff\xd8\xff-MAIN"
    assert sorted(p.name for p in base.iterdir()) == sorted(["主图", "详情页"])

    # ★ 同一角色再来一张：序号顺延，不覆盖上一张
    second = allocate_asset_path(
        base, role=ASSET_ROLE_DETAIL, index=1, ext=".jpg", content_hash=content_hash_bytes(b"OTHER")
    )
    assert second.name == "02.jpg", f"同序号被不同内容占用应顺延：{second}"
    assert detail_file.read_bytes() == b"\xff\xd8\xff-DETAIL", "顺延不得改动已有文件"

    # ★ 同内容重复落盘：回到原序号（幂等）
    same = allocate_asset_path(
        base, role=ASSET_ROLE_DETAIL, index=1, ext=".jpg", content_hash=content_hash_bytes(b"\xff\xd8\xff-DETAIL")
    )
    assert same == detail_file

    # ★ 清理：中文目录真的删得掉（Windows 上删不掉 = 后续用例 / 迁移都埋雷）
    for child in sorted(base.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        child.unlink() if child.is_file() else child.rmdir()
    base.rmdir()
    assert not base.exists()


# ======================================================================
#  ③ ZIP：中文目录名必须带 UTF-8 标志位（Windows 解压不乱码的前提）
# ======================================================================


def test_zip_chinese_names_are_utf8_flagged(tmp_path: Path) -> None:
    """打包含中文目录的素材包，逐条校验 UTF-8 标志位（0x800）与解压后的目录名。"""
    base = tmp_path / _uniq("pkg")
    files: list[Path] = []
    roles: list[str] = []
    for role, count in ((ASSET_ROLE_MAIN, 1), (ASSET_ROLE_DETAIL, 2)):
        for seq in range(1, count + 1):
            target = asset_path(base, role=role, index=seq, ext=".jpg")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f"{role}-{seq}".encode())
            files.append(target)
            roles.append(role)

    zip_path = tmp_path / "素材包.zip"
    make_zip_package(files=files, dest=zip_path, base_dir="images", roles=roles)

    with zipfile.ZipFile(zip_path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]

        assert names == [
            "images/主图/01.jpg",
            "images/详情页/01.jpg",
            "images/详情页/02.jpg",
        ], f"包内路径不是「中文子目录 + 序号」：{names}"

        for info in infos:
            # ★ 0x800 = general purpose bit 11 = 文件名按 UTF-8 编码
            assert info.flag_bits & 0x800, (
                f"{info.filename} 未置 UTF-8 标志位（0x800）⇒ Windows 资源管理器按 "
                f"CP437 解码会乱码：{info.filename.encode('utf-8').decode('cp437', errors='replace')}"
            )
            # ★ 反证：这个标志位确实在起作用（按 CP437 解就是乱码，不是"刚好都对"）
            mojibake = info.filename.encode("utf-8").decode("cp437", errors="replace")
            assert mojibake != info.filename, "UTF-8 标志位的对照失效：两种解码结果一样"

        # ★ 真解压一遍：目录名确实是中文
        out = tmp_path / "解压结果"
        archive.extractall(out)
        extracted = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.jpg"))
        assert extracted == [
            "images/主图/01.jpg",
            "images/详情页/01.jpg",
            "images/详情页/02.jpg",
        ], f"解压后目录名不对：{extracted}"


def test_manual_package_zip_groups_by_role(tmp_path: Path) -> None:
    """半自动素材包：主图 / 详情页在包内各归一个中文目录（使用者解压即分组）。"""
    base = tmp_path / _uniq("listing")
    main_file = asset_path(base, role=ASSET_ROLE_MAIN, index=1, ext=".jpg")
    detail_file = asset_path(base, role=ASSET_ROLE_DETAIL, index=1, ext=".jpg")
    for target in (main_file, detail_file):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"IMG")

    zip_path = tmp_path / "manual.zip"
    make_zip_package(
        files=[main_file, detail_file],
        dest=zip_path,
        extra_files={"README.txt": "上架指引"},
        base_dir="images",
        roles=[ASSET_ROLE_MAIN, ASSET_ROLE_DETAIL],
    )
    with zipfile.ZipFile(zip_path) as archive:
        assert "images/主图/01.jpg" in archive.namelist()
        assert "images/详情页/01.jpg" in archive.namelist()
        assert "README.txt" in archive.namelist()


# ======================================================================
#  ④ AI 重绘落盘（`persist_result()`）走同一套命名
# ======================================================================


async def test_persist_result_writes_ai_images_with_new_naming(session: Any) -> None:
    """AI 重绘产出落库：`storage_path` 落在 `assets/ai/{商品ID}/{主图|详情页}/NN.png`。"""
    settings = get_settings()
    product_1688_id = _uniq("ai-naming")
    product = SourceProduct(product_1688_id=product_1688_id, title="命名验证商品")
    session.add(product)
    await session.commit()

    task = AiTask(
        source_product_id=int(product.id),
        target_platform="taobao",
        task_type=AiTaskType.IMAGE_REDRAW.value,
        ai_client="mock",
        status=AiTaskStatus.QUEUED.value,
        created_by="tester",
    )
    session.add(task)
    await session.commit()
    # ★ 立刻取成纯 int：本会话随后会 rollback / 删除，ORM 对象会过期，
    #   那时再碰 `task.id` 会触发同步懒加载 ⇒ MissingGreenlet。
    task_id = int(task.id)
    product_id = int(product.id)

    # ★ 三张真实存在的产出图（字节互不相同，否则被 content_hash 去重压成 1 张）
    produced: list[AiImageResult] = []
    for position, role in enumerate((ASSET_ROLE_MAIN, ASSET_ROLE_DETAIL, ASSET_ROLE_DETAIL)):
        source = asset_path(settings.assets_dir / "ai" / _uniq("produced"), role=role, index=position + 1, ext=".png")
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(f"PNG-{role}-{position}".encode())
        produced.append(
            AiImageResult(
                local_path=str(source),
                index=position,
                image_role=role,
                width=800,
                height=800,
                prompt="验证提示词",
                prompt_source="per_image",
            )
        )

    try:
        await AiTaskService.persist_result(
            task_id, AiReworkResult(images=produced, model_name="verify"), client_name="mock"
        )
        await session.rollback()

        rows = (await session.execute(select(Asset).where(Asset.ai_task_id == task_id))).scalars().all()
        assert len(rows) == 3, f"3 张产出图应落 3 条 asset，实际 {len(rows)}"

        expected_dir = settings.assets_dir / "ai" / product_1688_id
        by_role: dict[str, list[Asset]] = {}
        for row in rows:
            path = Path(str(row.storage_path))
            assert path.exists(), f"落库路径与磁盘不一致（悬空引用）：{path}"
            assert path.read_bytes(), f"落盘文件为空：{path}"
            assert path.parent.parent == expected_dir, f"AI 产出应归到 {expected_dir}，实际 {path.parent.parent}"
            assert path.parent.name in ("主图", "详情页"), f"子目录必须是中文角色目录：{path}"
            assert path.name[:2].isdigit() and path.suffix == ".png", f"文件名必须是两位序号：{path}"
            by_role.setdefault(str(row.asset_type), []).append(row)

        assert len(by_role[AssetType.MAIN_IMAGE.value]) == 1
        assert sorted(Path(str(r.storage_path)).name for r in by_role[AssetType.DETAIL_IMAGE.value]) == [
            "01.png",
            "02.png",
        ]
        # ★ 角色 / 序号仍落在记录里（文件名只能表达目录 + 序号，表达不了提示词来源）
        for row in rows:
            tags = dict(row.tags_json or {})
            assert tags.get("image_role") in (ASSET_ROLE_MAIN, ASSET_ROLE_DETAIL)
            assert isinstance(tags.get("index"), int)
        assert all(r.origin == AssetOrigin.AI_REWORK.value for r in rows)
    finally:
        await session.rollback()
        for row in (
            (await session.execute(select(AiTaskResult).where(AiTaskResult.ai_task_id == task_id))).scalars()
        ):
            await session.delete(row)
        for row in (await session.execute(select(Asset).where(Asset.ai_task_id == task_id))).scalars():
            await session.delete(row)
        task_row = await session.get(AiTask, task_id)
        if task_row is not None:
            await session.delete(task_row)
        product_row = await session.get(SourceProduct, product_id)
        if product_row is not None:
            await session.delete(product_row)
        await session.commit()


@pytest.mark.parametrize("role", [ASSET_ROLE_MAIN, ASSET_ROLE_DETAIL])
def test_zip_entry_names_never_contain_legacy_english(role: str, tmp_path: Path) -> None:
    """回归护栏：包内不许再出现 `main_00.jpg` / `detail_01.jpg` 这类旧英文名。"""
    base = tmp_path / _uniq("guard")
    target = asset_path(base, role=role, index=1, ext=".jpg")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"IMG")

    zip_path = tmp_path / "guard.zip"
    make_zip_package(files=[target], dest=zip_path, roles=[role])
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
    assert not [n for n in names if parse_legacy_asset_name(Path(n).name)], f"包内残留旧英文命名：{names}"
