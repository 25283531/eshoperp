"""通用工具集（裁剪后 3 个模块，见 ARCH §3.1 C4）。

    crypto.py  AES-256 加解密、掩码、哈希
    csvio.py   CSV 导入导出（BOM 处理，兼容妙手 / 逸淘导入）
    kit.py     内容哈希、规格指纹、UTC 时间工具、ZIP 打包
"""

from app.utils.crypto import (
    decrypt_text,
    encrypt_text,
    is_encrypted,
    mask_secret,
    sha256_hex,
)
from app.utils.csvio import (
    MAPPING_CSV_HEADERS,
    PURCHASE_CSV_HEADERS,
    TRACKING_CSV_HEADERS,
    append_csv_rows,
    export_csv,
    read_csv_dicts,
)
from app.utils.kit import (
    content_hash_bytes,
    content_hash_file,
    iso_utc,
    make_zip_package,
    mock_item_id,
    parse_iso,
    spec_signature,
    utc_now,
)

__all__ = [
    "MAPPING_CSV_HEADERS",
    "PURCHASE_CSV_HEADERS",
    "TRACKING_CSV_HEADERS",
    "append_csv_rows",
    "content_hash_bytes",
    "content_hash_file",
    "decrypt_text",
    "encrypt_text",
    "export_csv",
    "is_encrypted",
    "iso_utc",
    "make_zip_package",
    "mask_secret",
    "mock_item_id",
    "parse_iso",
    "read_csv_dicts",
    "sha256_hex",
    "spec_signature",
    "utc_now",
]
