"""素材服务：版本管理 / 回滚 / 标签 / 打包下载 / 手工上传（§5.5.4）。"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.errors import BusinessError, ErrorCode, NotFoundError
from app.core.logging import get_logger, get_trace_id
from app.models.asset import Asset
from app.models.enums import AssetOrigin, AssetType, AuditActionType, AuditObjectType
from app.models.source import SourceProduct
from app.services.audit_service import AuditService
from app.utils.kit import (
    ASSET_ROLE_DIRNAMES,
    allocate_asset_path,
    content_hash_bytes,
    iso_utc,
    utc_now,
)

logger = get_logger(__name__)

__all__ = ["AssetService", "AssetUploadOutcome"]


# ★★ 手工上传的硬边界（上传是外部输入，三条限制缺一不可）★★
#   ★ 为什么必须有这条路：1688 商品详情接口的权限还没批下来（调用返回 `gw.APIACLDecline`），
#     采集链路进不来任何一张真实图片 ⇒ AI 重绘整条主链路无法验证。
#     手工上传是**当前唯一能把图片送进系统**的入口，必须能用，且不能成为攻击面。
MAX_UPLOAD_IMAGE_BYTES = 10 * 1024 * 1024  # 单张上限 10MB
MAX_UPLOAD_FILES = 20  # 单次最多 20 张
UPLOAD_ALLOWED_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")
# 没有归属商品时的兜底目录（素材仍需落盘，不能因为没有商品就被丢弃）
UPLOAD_UNASSIGNED_DIR = "unassigned"
_READ_CHUNK = 256 * 1024

# ★ 文件头签名 → 规范扩展名。**内容是什么由这里说了算，不信客户端扩展名**：
#   把 `evil.exe` 改名为 `cat.jpg` 骗过扩展名白名单是最常见的绕过手法。
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)


def sniff_image_ext(data: bytes) -> str:
    """按**文件头**判定真实图片格式，返回规范扩展名；不是图片返回空串。

    Args:
        data: 文件字节（含头部即可，不足 12 字节也能安全判定）。
    """
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    head = data[:12]
    for magic, ext in _IMAGE_SIGNATURES:
        if head.startswith(magic):
            return ext
    return ""


def resolve_upload_role(raw: str) -> str:
    """入参角色 → `AssetType` 值；非法值**显式报错**（不静默兜底充当详情页）。

    ★ 中文角色名（"主图" / "详情页"）也认：反查 `ASSET_ROLE_DIRNAMES`，
      这样前端传中文或英文都能通，且中文串仍然只在 `utils/kit.py` 一处定义。
    """
    candidate = str(raw or "").strip()
    if candidate in ASSET_ROLE_DIRNAMES:
        return candidate
    for role_value, dirname in ASSET_ROLE_DIRNAMES.items():
        if candidate == dirname:
            return role_value
    raise BusinessError(
        f"不支持的素材角色：{raw!r}，可用：{' / '.join(sorted(ASSET_ROLE_DIRNAMES))}",
        code=ErrorCode.PARAM_ERROR,
    )


def _client_filename(raw: Any) -> str:
    """取客户端文件名的**纯 basename**（仅供展示与报错提示）。

    ★★ 这个值**永远不参与拼路径** ★★
        存储路径一律由 `kit.asset_path()` 用服务端生成的序号构成，
        否则 `../../windows/system32/evil.jpg` 这类文件名就是一次现成的路径穿越。
    """
    name = str(raw or "").strip()
    return name.replace("\\", "/").rsplit("/", 1)[-1]


async def _read_capped(upload: Any, limit: int) -> tuple[bytes, bool]:
    """分块读文件，超过 `limit` 立刻停手（`(字节, 是否超限)`）。

    ★ 分块而不是一次 `read()`：一次读完会把整份不可信输入拉进内存，
      20 张 × 10MB 的上限下这是实打实的资源消耗。
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            return b"", True
        chunks.append(chunk)
    return b"".join(chunks), False


@dataclass
class AssetUploadOutcome:
    """一次上传的**逐文件**结果（新增 / 去重 / 被拒及原因）。

    ★ 为什么不清澈地"整体成功或整体失败"：批量上传时一张脏文件（比如伪装成 jpg 的 exe）
      让使用者前面选的 9 张全部作废、还得重传一遍，是最差的体验。
      逐文件给结论、失败给**可读中文原因**，才是"并非静默截断"。
    """

    created: list[Asset] = field(default_factory=list)
    duplicated: list[Asset] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)


class AssetService:
    """素材库。"""

    @staticmethod
    async def get(session: Any, asset_id: int) -> Asset:
        """取素材（404）。"""
        asset = (
            await session.execute(
                select(Asset).where(Asset.id == int(asset_id), Asset.is_deleted.is_(False))
            )
        ).scalars().first()
        if asset is None:
            raise NotFoundError(f"素材 {asset_id} 不存在", code=ErrorCode.ASSET_NOT_FOUND)
        return asset

    @staticmethod
    async def list_assets(
        session: Any,
        *,
        source_product_id: int | None = None,
        origin: str | None = None,
        asset_type: str | None = None,
        tag: str | None = None,
        version: int | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Asset], int]:
        """分页查询素材。"""
        stmt = select(Asset).where(Asset.is_deleted.is_(False))
        if source_product_id is not None:
            stmt = stmt.where(Asset.source_product_id == int(source_product_id))
        if origin:
            stmt = stmt.where(Asset.origin == origin)
        if asset_type:
            stmt = stmt.where(Asset.asset_type == asset_type)
        if version is not None:
            stmt = stmt.where(Asset.version == int(version))
        if tag:
            # JSON 列模糊匹配（SQLite / PG 均支持 LIKE on text cast）
            stmt = stmt.where(func.lower(func.cast(Asset.tags_json, str)).like(f"%{tag.strip().lower()}%"))

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await session.execute(count_stmt)).scalar_one() or 0)
        rows = (
            (await session.execute(stmt.order_by(Asset.id.desc()).offset((page - 1) * page_size).limit(page_size)))
            .scalars()
            .all()
        )
        return list(rows), total

    @staticmethod
    async def import_images(
        session: Any,
        files: Iterable[Any],
        *,
        source_product_id: int | None = None,
        role: str = AssetType.MAIN_IMAGE.value,
        operator: str = "system",
    ) -> AssetUploadOutcome:
        """★ 手工上传素材：写盘 + 落 `asset`（当前**唯一**能把图片送进系统的入口）。

        ★ 为什么需要它（不是锦上添花，是解死结）：
            1688 商品详情接口权限未开通（调用返回 `gw.APIACLDecline`），采集路径进不来图片，
            于是 AI 重绘整条主链路既无法验证也无法使用。手工上传是**降级但可用**的替代路径。

        Args:
            files: 类 `UploadFile` 的可迭代对象（需有 `.filename` 与异步 `.read()`）。
            source_product_id: 归属的货源商品；为空则落到 `{UPLOAD_UNASSIGNED_DIR}/` 兜底目录。
            role: `main_image` / `detail_image`（也认中文 "主图" / "详情页"）。
            operator: 操作人（审计留痕）。

        Returns:
            `AssetUploadOutcome`：逐文件的「新增 / 内容重复 / 被拒（含原因）」。

        Raises:
            BusinessError: 没选文件 / 超过单次数量上限 / 角色非法 / 商品不存在。

        ★ 三条硬性拒绝全部**讲明原因**，绝不静默丢弃任何一张：
            ① 扩展名不在白名单；② 内容超上限或为空；③ 文件头不是图片（扩展名撒谎）。
        """
        uploads = list(files or [])
        if not uploads:
            raise BusinessError("请选择要上传的图片文件", code=ErrorCode.PARAM_ERROR)
        if len(uploads) > MAX_UPLOAD_FILES:
            raise BusinessError(
                f"单次最多上传 {MAX_UPLOAD_FILES} 张图片，本次 {len(uploads)} 张，请分次上传",
                code=ErrorCode.PARAM_ERROR,
            )
        resolved_role = resolve_upload_role(role)

        product_key = UPLOAD_UNASSIGNED_DIR
        if source_product_id is not None:
            product = (
                await session.execute(
                    select(SourceProduct).where(
                        SourceProduct.id == int(source_product_id), SourceProduct.is_deleted.is_(False)
                    )
                )
            ).scalars().first()
            if product is None:
                raise NotFoundError(
                    f"货源商品 {source_product_id} 不存在", code=ErrorCode.SOURCE_PRODUCT_NOT_FOUND
                )
            # ★ 与 1688 采集（`SourceService._download_assets`）、AI 重绘（`persist_result`）
            #   口径一致：目录键优先取 1688 商品 ID，让三种来源的素材落在同一棵树里。
            product_key = str(getattr(product, "product_1688_id", None) or source_product_id)

        base_dir = get_settings().assets_dir / "raw" / product_key
        outcome = AssetUploadOutcome()
        # ★ 会话 `autoflush=False`，本次循环里刚 add 的行不会被后续 SELECT 看到
        #   ⇒ 同一批里出现相同字节会撞 `uq_asset_content_hash`，必须自己先去重。
        batch_hashes: set[str] = set()
        next_index = 1

        for upload in uploads:
            filename = _client_filename(getattr(upload, "filename", "") or "")
            data, too_large = await _read_capped(upload, MAX_UPLOAD_IMAGE_BYTES)
            if too_large:
                outcome.failed.append(
                    {
                        "filename": filename,
                        "reason": f"文件超过 {MAX_UPLOAD_IMAGE_BYTES // (1024 * 1024)}MB 上限，请压缩后再传",
                    }
                )
                continue
            if not data:
                outcome.failed.append({"filename": filename, "reason": "文件内容为空"})
                continue

            suffix = Path(filename).suffix.lower()
            if suffix not in UPLOAD_ALLOWED_EXTS:
                outcome.failed.append(
                    {
                        "filename": filename,
                        "reason": (
                            f"不支持的文件类型 {suffix or '（无扩展名）'}，"
                            f"仅支持 {' / '.join(UPLOAD_ALLOWED_EXTS)}"
                        ),
                    }
                )
                continue
            # ★ 只信内容：扩展名过了白名单，还得证明它真的是张图。
            #   ★ 落盘的扩展名也**取嗅探结果**（客户端给的那个可能也是撒谎的）。
            sniffed = sniff_image_ext(data)
            if not sniffed:
                outcome.failed.append(
                    {
                        "filename": filename,
                        "reason": "文件内容不是有效的图片（已按文件头校验，扩展名不可信）",
                    }
                )
                continue

            digest = content_hash_bytes(data)
            if digest in batch_hashes:
                logger.info("upload_duplicate_in_batch", filename=filename, content_hash=digest[:12])
                continue
            exists = (
                await session.execute(
                    select(Asset).where(Asset.content_hash == digest, Asset.is_deleted.is_(False))
                )
            ).scalars().first()
            if exists is not None:
                # ★ 重复内容是**幂等命中**而不是错误：同一张图换个名字重传，
                #   使用者看到的是"已经是第 3 张了"，而不是一句看不懂的报错。
                outcome.duplicated.append(exists)
                batch_hashes.add(digest)
                continue
            batch_hashes.add(digest)

            # ★ 序号：由 `allocate_asset_path()` 在"同序号已被不同内容占用"时顺延，
            #   再用**实际文件名**回填记录的 `index` —— 保证"记录里的序号"与"磁盘上的序号"
            #   永远一致（两者一旦分叉，使用者按记录找图就会找错）。
            target = allocate_asset_path(
                base_dir, role=resolved_role, index=next_index, ext=sniffed, content_hash=digest
            )
            try:
                sequence = int(target.stem)
            except ValueError:  # 命名口径若变，退回请求内计数，不让整批上传崩在这里
                sequence = next_index
            next_index = sequence + 1
            target.write_bytes(data)

            asset = Asset(
                source_product_id=(int(source_product_id) if source_product_id is not None else None),
                asset_type=(
                    AssetType.MAIN_IMAGE.value
                    if resolved_role == AssetType.MAIN_IMAGE.value
                    else AssetType.DETAIL_IMAGE.value
                ),
                origin=AssetOrigin.RAW.value,
                storage_path=str(target),
                content_hash=digest,
                version=1,
                lineage_id=f"upload-{product_key}-{resolved_role}-{sequence}",
                is_current=True,
                size_bytes=len(data),
                # ★ 与 `persist_result()` 同一套键名（`image_role` / `index`）：
                #   前端据此分组排序；不要让 UI 去猜第几张是主图。
                tags_json={
                    "tags": [resolved_role],
                    "image_role": resolved_role,
                    "index": sequence,
                    "source_filename": filename,
                },
            )
            session.add(asset)
            await session.flush()
            outcome.created.append(asset)

        await AuditService.write(
            session,
            action_type=AuditActionType.MAPPING_CHANGE.value,
            object_type=AuditObjectType.SYSTEM_SETTING.value,
            object_id=str(source_product_id or UPLOAD_UNASSIGNED_DIR),
            operator=operator,
            new_value={
                "created": len(outcome.created),
                "duplicated": len(outcome.duplicated),
                "failed": [item["reason"] for item in outcome.failed],
                "role": resolved_role,
            },
            trace_id=get_trace_id(),
            remark=f"手工上传素材 {len(uploads)} 个（新增 {len(outcome.created)} / 重复 {len(outcome.duplicated)}）",
        )
        await session.flush()
        return outcome

    @staticmethod
    async def update_tags(
        session: Any, asset_id: int, tags: Iterable[str], *, operator: str = "system"
    ) -> Asset:
        """全量覆盖素材标签。"""
        asset = await AssetService.get(session, asset_id)
        asset.tags_json = {"tags": [str(t).strip() for t in tags if str(t).strip()]}
        asset.updated_at = utc_now()
        await session.flush()
        await AuditService.write(
            session,
            action_type=AuditActionType.MAPPING_CHANGE.value,
            object_type=AuditObjectType.SYSTEM_SETTING.value,
            object_id=asset.id,
            operator=operator,
            new_value={"tags": asset.tags_json},
            trace_id=get_trace_id(),
            remark=f"更新素材 {asset.id} 标签",
        )
        return asset

    @staticmethod
    async def rollback(
        session: Any, asset_id: int, *, reason: str = "", operator: str = "system"
    ) -> Asset:
        """★ 版本回滚：同 `lineage_id` 内把目标版本置为 `is_current=true`，其余置 false。

        Raises:
            BusinessError: 1004 素材不存在；2102 无版本族可回滚。
        """
        asset = await AssetService.get(session, asset_id)
        if not asset.lineage_id:
            raise BusinessError(
                "该素材不属于任何版本族，无法回滚", code=ErrorCode.ASSET_ROLLBACK_FAILED, http_status=409
            )

        siblings = (
            await session.execute(
                select(Asset).where(Asset.lineage_id == asset.lineage_id, Asset.is_deleted.is_(False))
            )
        ).scalars().all()
        if len(siblings) <= 1:
            raise BusinessError(
                "该版本族只有 1 个版本，无需回滚", code=ErrorCode.ASSET_ROLLBACK_FAILED, http_status=409
            )

        for sibling in siblings:
            sibling.is_current = int(sibling.id) == int(asset.id)
        asset.updated_at = utc_now()
        await session.flush()

        await AuditService.write(
            session,
            action_type=AuditActionType.MAPPING_CHANGE.value,
            object_type=AuditObjectType.SYSTEM_SETTING.value,
            object_id=asset.id,
            operator=operator,
            new_value={"lineage_id": asset.lineage_id, "version": asset.version, "reason": reason},
            trace_id=get_trace_id(),
            remark=f"素材回滚到版本 v{asset.version}：{reason}",
        )
        await session.flush()
        return asset

    @staticmethod
    def build_zip(assets: Iterable[Asset], filename: str) -> tuple[str, list[int]]:
        """批量打包素材为 ZIP。

        Returns:
            `(ZIP 绝对路径, 缺失文件的素材 ID 列表)`。
        """
        directory = get_settings().exports_dir
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / filename
        missing: list[int] = []

        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for asset in assets:
                path = Path(asset.storage_path or "")
                if not path.exists():
                    missing.append(int(asset.id))
                    continue
                archive.write(path, arcname=f"{int(asset.id)}_{path.name}")
        return str(target), missing

    @staticmethod
    def download_url_for(asset: Asset) -> str:
        """素材下载 URL。"""
        return f"/api/v1/assets/{int(asset.id)}/download"

    @staticmethod
    def export_summary(asset_count: int) -> dict[str, Any]:
        """导出摘要。"""
        return {"count": int(asset_count), "exported_at": iso_utc(utc_now())}
