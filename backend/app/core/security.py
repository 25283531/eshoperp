"""安全工具：AES-256 凭证加解密、凭证掩码、操作者身份解析。

★ 明文凭证永不落库、永不入日志、永不出现在 API 响应（§10.8）。
底层实现在 `app/utils/crypto.py`，此处做应用层封装与角色判定。
"""

from __future__ import annotations

from typing import Any

from fastapi import Header

from app.core.errors import ForbiddenError, UnauthorizedError
from app.models.enums import OperatorRole
from app.utils.crypto import (
    decrypt_text,
    encrypt_text,
    is_encrypted,
    mask_secret,
    sha256_hex,
)

__all__ = [
    "OperatorContext",
    "decrypt_credential",
    "encrypt_credential",
    "mask_credential",
    "require_admin",
    "require_operator",
    "resolve_operator",
]


class OperatorContext:
    """当前操作者上下文（轻量鉴权，见 ARCH §11.2 N1）。

    MVP 不做完整 RBAC：通过 `X-Operator` 头（或管理员 / 运营 Token）识别身份，
    仅用于审计留痕与管理员接口鉴权。
    """

    __slots__ = ("name", "role")

    def __init__(self, name: str = "system", role: OperatorRole = OperatorRole.SYSTEM) -> None:
        self.name = name or "system"
        self.role = role

    @property
    def is_admin(self) -> bool:
        """是否为管理员。"""
        return self.role == OperatorRole.ADMIN

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典，便于写入审计日志。"""
        return {"name": self.name, "role": self.role.value}


def encrypt_credential(plain: str) -> str:
    """加密凭证明文，返回可安全落库的密文字符串。"""
    return encrypt_text(plain)


def decrypt_credential(cipher: str) -> str:
    """解密凭证密文；失败时由调用方转换为 8002 错误码。"""
    return decrypt_text(cipher)


def mask_credential(value: str | None, keep_head: int = 2, keep_tail: int = 4) -> str:
    """凭证掩码展示，如 `ak****3f2a`。"""
    return mask_secret(value, keep_head=keep_head, keep_tail=keep_tail)


def fingerprint_credential(plain: str) -> str:
    """凭证指纹（sha256），用于判断凭证是否变更而不暴露明文。"""
    return sha256_hex(plain)


def resolve_operator(
    x_operator: str | None = Header(default=None, alias="X-Operator"),
    x_operator_role: str | None = Header(default=None, alias="X-Operator-Role"),
    x_operator_token: str | None = Header(default=None, alias="X-Operator-Token"),
) -> OperatorContext:
    """从请求头解析操作者（FastAPI 依赖）。

    规则：
        1. `X-Operator-Token` 等于管理员 Token → 管理员；
        2. 等于运营 Token → 运营；
        3. 否则按 `X-Operator-Role` 声明（仅 dev 环境放宽），默认 `operator`。
    """
    from app.core.config import get_settings

    settings = get_settings()
    name = (x_operator or settings.anonymous_actor).strip() or settings.anonymous_actor

    if x_operator_token and x_operator_token == settings.admin_token:
        return OperatorContext(name=name, role=OperatorRole.ADMIN)
    if x_operator_token and x_operator_token == settings.operator_token:
        return OperatorContext(name=name, role=OperatorRole.OPERATOR)

    declared = (x_operator_role or "").strip().lower()
    if declared == OperatorRole.ADMIN.value and not settings.debug:
        raise UnauthorizedError("缺少管理员凭证，无法以管理员身份操作")
    if declared == OperatorRole.ADMIN.value:
        return OperatorContext(name=name, role=OperatorRole.ADMIN)
    return OperatorContext(name=name, role=OperatorRole.OPERATOR)


def require_admin(operator: OperatorContext) -> OperatorContext:
    """校验管理员权限，否则抛 1003。"""
    if not operator.is_admin:
        raise ForbiddenError("该操作仅管理员可执行")
    return operator


def require_operator(operator: OperatorContext) -> OperatorContext:
    """校验已认证操作者（MVP 恒通过，保留扩展点）。"""
    if operator is None:
        raise UnauthorizedError("未识别操作者身份")
    return operator


def is_encrypted_value(value: str | None) -> bool:
    """判断字符串是否为本系统加密格式。"""
    return is_encrypted(value)
