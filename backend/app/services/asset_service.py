"""素材服务：版本管理 / 回滚 / 标签 / 打包下载（§5.5.4）。"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.errors import BusinessError, ErrorCode, NotFoundError
from app.core.logging import get_logger, get_trace_id
from app.models.asset import Asset
from app.models.enums import AuditActionType, AuditObjectType
from app.services.audit_service import AuditService
from app.utils.kit import iso_utc, utc_now

logger = get_logger(__name__)

__all__ = ["AssetService"]


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
