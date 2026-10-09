"""系统设置 / 枚举字典 / 平台账号 / 凭证（ARCH §5.5.2、§5.5.13）。

凭证与适配器配置属**高危写操作**，一律走 `AdminOperator`（非管理员 403）。
★ 凭证明文永不落库、永不入日志、永不出现在列表响应（§10.8）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.core.deps import AdminOperator, CurrentOperator, DbSession
from app.core.errors import BusinessError, ErrorCode
from app.core.response import ApiResponse
from app.models.enums import ENUM_DICT
from app.schemas.listing import (
    CredentialCreate,
    CredentialRevealRequest,
    CredentialUpdate,
    CredentialVo,
    PlatformAccountAuthorizeRequest,
    PlatformAccountCreate,
    PlatformAccountVo,
)
from app.schemas.system import SettingUpdate, SettingVo
from app.services.system_service import SystemService

router = APIRouter(tags=["系统设置"])

__all__ = ["router"]


# ======================================================================
#  系统设置
# ======================================================================


@router.get("/settings", summary="全部系统配置")
async def list_settings(session: DbSession) -> ApiResponse[dict[str, dict[str, Any]]]:
    """返回 `{key: {value, value_type, description, updated_by, updated_at}}`。"""
    return ApiResponse.ok(data=await SystemService.list_settings(session))


@router.put("/settings/{key}", summary="修改系统配置（管理员）")
async def update_setting(
    key: str,
    payload: SettingUpdate,
    session: DbSession,
    admin: AdminOperator,
) -> ApiResponse[SettingVo]:
    """修改配置项；`ai.client` 等关键项切换会写审计。"""
    row = await SystemService.update_setting(
        session, key, payload.value, reason=payload.reason or "", operator=admin.name
    )
    await session.commit()
    vo = SettingVo.from_model(row)
    return ApiResponse.ok(data=vo, message=f"配置项 {key} 已更新")


@router.get("/settings/enums", summary="全部枚举字典（前端下拉唯一来源）")
async def list_enums() -> ApiResponse[dict[str, Any]]:
    """★ 前后端枚举唯一真源，避免硬编码漂移。"""
    return ApiResponse.ok(data=dict(ENUM_DICT))


# ======================================================================
#  平台账号
# ======================================================================


@router.get("/platform-accounts", summary="平台账号列表")
async def list_platform_accounts(
    session: DbSession,
    platform: Annotated[str | None, Query(description="taobao/douyin/pdd/alibaba1688")] = None,
    status: Annotated[str | None, Query(description="active/expired/revoked")] = None,
) -> ApiResponse[list[PlatformAccountVo]]:
    """Token 只返回掩码。"""
    rows = await SystemService.list_platform_accounts(session, platform=platform, status=status)
    return ApiResponse.ok(
        data=[PlatformAccountVo.from_model(account, token_masked=masked) for account, masked in rows]
    )


@router.post("/platform-accounts", status_code=201, summary="创建平台账号（管理员）")
async def create_platform_account(
    payload: PlatformAccountCreate,
    session: DbSession,
    admin: AdminOperator,
) -> ApiResponse[PlatformAccountVo]:
    """创建账号；`credential` 中的 Key 加密落库，明文不落库。"""
    account, masked = await SystemService.create_platform_account(
        session, payload, operator=admin.name
    )
    await session.commit()
    return ApiResponse.ok(data=PlatformAccountVo.from_model(account, token_masked=masked), message="平台账号已创建")


@router.put("/platform-accounts/{account_id}", summary="更新平台账号（管理员）")
async def update_platform_account(
    account_id: int,
    payload: PlatformAccountCreate,
    session: DbSession,
    admin: AdminOperator,
) -> ApiResponse[PlatformAccountVo]:
    """更新店铺名 / 状态 / 已授权 scope（平台与 shop_id 不参与更新）。"""
    account, masked = await SystemService.update_platform_account(
        session, account_id, payload, operator=admin.name
    )
    await session.commit()
    return ApiResponse.ok(data=PlatformAccountVo.from_model(account, token_masked=masked))


@router.post("/platform-accounts/{account_id}/authorize", summary="平台账号授权（★ scope 硬校验）")
async def authorize_platform_account(
    account_id: int,
    payload: PlatformAccountAuthorizeRequest,
    session: DbSession,
    admin: AdminOperator,
) -> ApiResponse[dict[str, Any]]:
    """★ 声明 scope 走 `check_scope()`：含商品编辑类 scope 直接 403/5003。"""
    result = await SystemService.authorize_platform_account(
        session, account_id, list(payload.granted_scopes or []), operator=admin.name
    )
    await session.commit()
    return ApiResponse.ok(data=result)


# ======================================================================
#  凭证
# ======================================================================


@router.get("/credentials", summary="凭证列表（仅掩码）")
async def list_credentials(
    session: DbSession,
    owner_type: Annotated[str | None, Query(description="platform/fulfillment/source")] = None,
) -> ApiResponse[list[CredentialVo]]:
    """仅返回 `value_masked`。"""
    rows = await SystemService.list_credentials(session, owner_type=owner_type)
    return ApiResponse.ok(data=[CredentialVo.from_model(row) for row in rows])


@router.post("/credentials", status_code=201, summary="创建凭证（管理员）")
async def create_credential(
    payload: CredentialCreate,
    session: DbSession,
    admin: AdminOperator,
) -> ApiResponse[CredentialVo]:
    """明文 AES-256 加密后落库。"""
    row = await SystemService.create_credential(
        session,
        owner_type=payload.owner_type,
        owner_key=payload.owner_key,
        credential_key=payload.credential_key,
        value=payload.value,
        expires_at=payload.expires_at,
        operator=admin.name,
    )
    await session.commit()
    return ApiResponse.ok(data=CredentialVo.from_model(row), message="凭证已创建")


@router.put("/credentials/{credential_id}", summary="更新凭证（管理员）")
async def update_credential(
    credential_id: int,
    payload: CredentialUpdate,
    session: DbSession,
    admin: AdminOperator,
) -> ApiResponse[CredentialVo]:
    """换 Key / 改过期时间 / 吊销。"""
    row = await SystemService.update_credential(
        session,
        credential_id,
        value=payload.value,
        expires_at=payload.expires_at,
        status=payload.status,
        operator=admin.name,
    )
    await session.commit()
    return ApiResponse.ok(data=CredentialVo.from_model(row))


@router.delete("/credentials/{credential_id}", summary="删除凭证（管理员）")
async def delete_credential(
    credential_id: int,
    session: DbSession,
    admin: AdminOperator,
) -> ApiResponse[dict[str, int]]:
    """删除凭证（审计留痕保证可追溯）。"""
    deleted = await SystemService.delete_credential(session, credential_id, operator=admin.name)
    await session.commit()
    return ApiResponse.ok(data={"id": int(deleted)})


@router.post("/credentials/{credential_id}/test", summary="凭证连通性自检")
async def test_credential(
    credential_id: int,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """★ 诚实降级：适配器未实测，本接口只验证「凭证可解密」，不伪造连通性结论。"""
    result = await SystemService.test_credential(session, credential_id, operator=operator.name)
    await session.commit()
    return ApiResponse.ok(data=result)


@router.post("/credentials/{credential_id}/reveal", summary="查看凭证明文（二次验证）")
async def reveal_credential(
    credential_id: int,
    payload: CredentialRevealRequest,
    session: DbSession,
    admin: AdminOperator,
) -> ApiResponse[dict[str, Any]]:
    """★ `verify_code` 必须等于管理员 Token；查看动作写审计。"""
    from app.core.config import get_settings

    settings = get_settings()
    if not payload.verify_code:
        raise BusinessError("缺少二次验证码", code=ErrorCode.FORBIDDEN, http_status=403)
    result = await SystemService.reveal_credential(
        session,
        credential_id,
        verify_code=payload.verify_code,
        admin_token=settings.admin_token,
        operator=admin.name,
    )
    await session.commit()
    return ApiResponse.ok(data=result)
