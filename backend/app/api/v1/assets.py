"""素材库（ARCH §5.5.4）与导出文件下载。

★ 素材版本链：`rollback()` 会把同 `lineage_id` 的其他版本置 `is_current=false`，
  并把目标版本恢复为当前版本（写审计）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from app.api.v1._common import page_of
from app.core.config import get_settings
from app.core.deps import CurrentOperator, DbSession
from app.core.errors import NotFoundError
from app.core.pagination import PageParams, page_params
from app.core.response import ApiResponse
from app.schemas.asset import (
    AssetRollbackRequest,
    AssetTagUpdate,
    AssetVo,
    BatchDownloadRequest,
    BatchDownloadVo,
)
from app.services.asset_service import AssetService

router = APIRouter(tags=["素材"])

__all__ = ["router"]


@router.get("/assets", summary="素材列表")
async def list_assets(
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    source_product_id: Annotated[int | None, Query()] = None,
    origin: Annotated[str | None, Query(description="raw / ai_rework")] = None,
    asset_type: Annotated[str | None, Query()] = None,
    tag: Annotated[str | None, Query()] = None,
    version: Annotated[int | None, Query()] = None,
) -> ApiResponse[Any]:
    """分页返回素材（默认 `preview_url` 指向下载端点）。"""
    rows, total = await AssetService.list_assets(
        session,
        source_product_id=source_product_id,
        origin=origin,
        asset_type=asset_type,
        tag=tag,
        version=version,
        page=params.page,
        page_size=params.page_size,
    )
    return ApiResponse.ok(data=page_of([AssetVo.from_model(r) for r in rows], total, params))


@router.get("/assets/{asset_id}", summary="素材详情")
async def get_asset(asset_id: int, session: DbSession) -> ApiResponse[AssetVo]:
    """素材详情。"""
    asset = await AssetService.get(session, asset_id)
    return ApiResponse.ok(data=AssetVo.from_model(asset))


@router.get("/assets/{asset_id}/download", summary="下载素材文件")
async def download_asset(asset_id: int, session: DbSession) -> FileResponse:
    """文件流下载（本地存储路径由 `Asset.storage_path` 决定）。"""
    asset = await AssetService.get(session, asset_id)
    path = Path(asset.storage_path or "")
    if not str(asset.storage_path or "") or not path.exists():
        raise NotFoundError(f"素材 {asset_id} 的文件不存在（路径：{asset.storage_path}）")
    return FileResponse(path=str(path), filename=path.name, media_type="application/octet-stream")


@router.post("/assets/batch-download", summary="批量下载素材（ZIP）")
async def batch_download(
    payload: BatchDownloadRequest,
    session: DbSession,
) -> ApiResponse[BatchDownloadVo]:
    """按 ID 打包为 ZIP，返回下载地址与缺失 ID。"""
    assets = []
    missing_ids: list[int] = []
    for raw_id in payload.ids or []:
        try:
            assets.append(await AssetService.get(session, int(raw_id)))
        except NotFoundError:
            missing_ids.append(int(raw_id))
        except Exception:  # noqa: BLE001  单个素材取不到不应中断整批
            missing_ids.append(int(raw_id))

    if not assets:
        return ApiResponse.ok(
            data=BatchDownloadVo(download_url="", file_count=0, missing_ids=missing_ids)
        )

    zip_path, missing = AssetService.build_zip(assets, f"assets_{len(assets)}.zip")
    missing_ids = sorted(set(missing_ids + [int(i) for i in missing]))
    filename = Path(zip_path).name
    return ApiResponse.ok(
        data=BatchDownloadVo(
            download_url=f"/api/v1/files/exports/{filename}",
            file_count=len(assets),
            missing_ids=missing_ids,
        )
    )


@router.post("/assets/{asset_id}/rollback", summary="素材版本回滚")
async def rollback_asset(
    asset_id: int,
    payload: AssetRollbackRequest,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[AssetVo]:
    """回滚到指定版本（同族其他版本自动置为非当前）。"""
    asset = await AssetService.rollback(session, asset_id, reason=payload.reason, operator=operator.name)
    await session.commit()
    return ApiResponse.ok(data=AssetVo.from_model(asset), message="素材已回滚为当前版本")


@router.put("/assets/{asset_id}/tags", summary="更新素材标签")
async def update_asset_tags(
    asset_id: int,
    payload: AssetTagUpdate,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[AssetVo]:
    """全量覆盖标签列表。"""
    asset = await AssetService.update_tags(session, asset_id, list(payload.tags or []), operator=operator.name)
    await session.commit()
    return ApiResponse.ok(data=AssetVo.from_model(asset))


@router.get("/files/exports/{filename}", summary="导出文件下载（ZIP / CSV）")
async def download_export(filename: str) -> FileResponse:
    """下载 `data/exports/` 下的导出文件。

    ★ 安全：仅取 `filename` 的 basename，杜绝目录穿越。
    """
    safe_name = Path(filename).name
    path = Path(get_settings().exports_dir) / safe_name
    if not path.exists() or not path.is_file():
        raise NotFoundError(f"导出文件 {safe_name} 不存在")
    return FileResponse(path=str(path), filename=safe_name, media_type="application/octet-stream")
