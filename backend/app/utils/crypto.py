"""AES-256-GCM 加解密与凭证掩码（PRD 8.3 / SYS-P0-01）。

★ 凭证明文永不落库、永不入日志、永不出现在 API 响应。

密文格式：`v1:<base64(nonce[12] || ciphertext)>`，便于未来平滑升级算法版本。
"""

from __future__ import annotations

import base64
import hashlib
import os
from functools import lru_cache

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

CIPHER_PREFIX = "v1:"
NONCE_BYTES = 12
KEY_BYTES = 32


@lru_cache(maxsize=4)
def _key_bytes(raw_key: str) -> bytes:
    """将配置中的密钥字符串派生为 32 字节 AES key。

    支持两种形式：
        1. base64 编码的 32 字节（推荐，配置 `CREDENTIAL_KEY`）；
        2. 任意口令 —— 用 sha256 派生（默认由 `SECRET_KEY` 派生）。
    """
    value = (raw_key or "").strip()
    if value:
        try:
            decoded = base64.b64decode(value, validate=True)
            if len(decoded) == KEY_BYTES:
                return decoded
        except Exception:  # noqa: BLE001  不是 base64 则按口令处理
            pass
        return hashlib.sha256(value.encode("utf-8")).digest()
    return hashlib.sha256(get_settings().secret_key.encode("utf-8")).digest()


def get_encryption_key() -> bytes:
    """获取当前生效的 AES-256 密钥（32 字节）。"""
    settings = get_settings()
    return _key_bytes(settings.credential_key or settings.secret_key)


def encrypt_text(plain: str | bytes | None) -> str:
    """加密明文，返回 `v1:<base64>` 密文字符串。空值返回空串。"""
    if plain is None:
        return ""
    data = plain.encode("utf-8") if isinstance(plain, str) else plain
    nonce = os.urandom(NONCE_BYTES)
    token = AESGCM(get_encryption_key()).encrypt(nonce, data, None)
    return CIPHER_PREFIX + base64.b64encode(nonce + token).decode("ascii")


def decrypt_text(cipher: str | None) -> str:
    """解密密文字符串。

    Raises:
        ValueError: 密文格式非法或解密失败（调用方应转换为 8002 凭证解密失败）。
    """
    if not cipher:
        return ""
    if not is_encrypted(cipher):
        # 兼容历史明文数据：直接返回，不抛异常
        return cipher
    raw = cipher[len(CIPHER_PREFIX):]
    try:
        blob = base64.b64decode(raw)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"凭证密文 base64 解析失败: {exc}") from exc
    if len(blob) <= NONCE_BYTES:
        raise ValueError("凭证密文长度非法")
    nonce, token = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    try:
        plain = AESGCM(get_encryption_key()).decrypt(nonce, token, None)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"凭证解密失败: {exc}") from exc
    return plain.decode("utf-8")


def is_encrypted(value: str | None) -> bool:
    """判断字符串是否为本系统加密格式。"""
    return bool(value) and value.startswith(CIPHER_PREFIX)


def mask_secret(value: str | None, keep_head: int = 2, keep_tail: int = 4) -> str:
    """凭证掩码展示：`abcdef123456` → `ab****3456`。"""
    if not value:
        return ""
    if is_encrypted(value):
        return "****(encrypted)"
    if len(value) <= keep_head + keep_tail:
        return "*" * len(value)
    return f"{value[:keep_head]}****{value[-keep_tail:]}"


def sha256_hex(value: str | bytes) -> str:
    """计算 sha256 十六进制摘要（凭证指纹 / 内容校验）。"""
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def md5_hex(value: str | bytes) -> str:
    """计算 md5 十六进制摘要（规格指纹使用，非安全场景）。"""
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.md5(data).hexdigest()


__all__ = [
    "decrypt_text",
    "encrypt_text",
    "get_encryption_key",
    "is_encrypted",
    "mask_secret",
    "md5_hex",
    "sha256_hex",
]
