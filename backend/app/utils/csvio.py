"""CSV 导入导出（BOM 处理，兼容妙手 / 逸淘导入）。

★ ARCH §11.2 N5：第三方导入的列名 / 顺序 / 编码未实测，先导出通用全字段模板；
  实测后可在 `app/adapters/fulfillment/profiles/` 下增加 `{adapter}_mapping.csv.tpl` 覆盖。
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# 导出默认带 UTF-8 BOM，保证 Excel 与第三方工具打开不乱码
DEFAULT_ENCODING = "utf-8-sig"
DEFAULT_BOM = True

# ---------------- 通用导出模板 ----------------

MAPPING_CSV_HEADERS: list[str] = [
    "platform",
    "shop_id",
    "shop_item_id",
    "shop_sku_code",
    "shop_sku_name",
    "source_product_1688_id",
    "source_sku_code_1688",
    "source_sku_name",
    "spec_signature",
    "purchase_cost",
    "cost_currency",
    "status",
    "is_mock",
    "remark",
]

PURCHASE_CSV_HEADERS: list[str] = [
    "order_id",
    "platform_order_no",
    "source_product_1688_id",
    "source_sku_code_1688",
    "quantity",
    "receiver_name",
    "receiver_phone",
    "receiver_address",
    "remark",
]

TRACKING_CSV_HEADERS: list[str] = [
    "order_id",
    "platform_order_no",
    "purchase_order_no",
    "logistics_company",
    "tracking_no",
    "shipped_at",
]

ORDER_IMPORT_CSV_HEADERS: list[str] = [
    "platform",
    "shop_id",
    "platform_order_no",
    "total_amount",
    "paid_at",
    "receiver_name",
    "receiver_phone",
    "receiver_address",
    "shop_item_id",
    "shop_sku_code",
    "quantity",
    "price",
]


def export_csv(
    rows: Sequence[Mapping[str, Any]],
    dest: str | Path,
    headers: Sequence[str] | None = None,
    *,
    bom: bool = DEFAULT_BOM,
    encoding: str = DEFAULT_ENCODING,
) -> str:
    """导出 CSV 文件。

    Args:
        rows: 数据行（dict 序列）；缺失字段写空串。
        dest: 输出文件路径（父目录自动创建）。
        headers: 表头顺序；为 None 时取首行 keys 的并集。
        bom: 是否写 UTF-8 BOM（默认写，兼容 Excel 与第三方工具）。
        encoding: 文件编码。

    Returns:
        生成的文件绝对路径。
    """
    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)

    if headers is None:
        ordered: list[str] = []
        for row in rows:
            for key in row.keys():
                if key not in ordered:
                    ordered.append(str(key))
        headers = ordered
    header_list = [str(h) for h in headers]

    final_encoding = "utf-8-sig" if bom and encoding.lower().replace("-", "") == "utf8sig" else encoding
    with open(path, "w", newline="", encoding=final_encoding) as handle:
        writer = csv.DictWriter(handle, fieldnames=header_list, extrasaction="ignore", restval="")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _stringify(row.get(k, "")) for k in header_list})
    return str(path)


def read_csv_dicts(
    src: str | Path,
    *,
    encoding: str = DEFAULT_ENCODING,
    required_headers: Sequence[str] | None = None,
) -> list[dict[str, str]]:
    """读取 CSV 为 dict 列表（自动处理 BOM 与列名空白）。

    Raises:
        ValueError: 缺少必需列时抛出，消息为中文可直接展示。
    """
    path = Path(src)
    if not path.exists():
        return []

    with open(path, "r", newline="", encoding=encoding, errors="replace") as handle:
        reader = csv.DictReader(handle)
        fieldnames = [ (name or "").strip() for name in (reader.fieldnames or []) ]
        if required_headers:
            missing = [h for h in required_headers if h not in fieldnames]
            if missing:
                raise ValueError(f"CSV 缺少必需列：{', '.join(missing)}")
        rows: list[dict[str, str]] = []
        for raw in reader:
            row: dict[str, str] = {}
            for index, name in enumerate(fieldnames):
                key = name or f"col_{index}"
                value = raw.get(name)
                row[key] = (value or "").strip() if isinstance(value, str) else ""
            if any(row.values()):
                rows.append(row)
        return rows


def append_csv_rows(
    rows: Sequence[Mapping[str, Any]],
    dest: str | Path,
    headers: Sequence[str],
    *,
    bom: bool = DEFAULT_BOM,
) -> str:
    """追加写入 CSV（文件不存在时先写表头），用于本地兜底累积采购清单 / 物流单号。"""
    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    header_list = [str(h) for h in headers]
    need_header = (not path.exists()) or path.stat().st_size == 0

    with open(path, "a", newline="", encoding="utf-8-sig" if bom else "utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=header_list, extrasaction="ignore", restval="")
        if need_header:
            writer.writeheader()
        for row in rows:
            writer.writerow({k: _stringify(row.get(k, "")) for k in header_list})
    return str(path)


def dumps_csv(rows: Sequence[Mapping[str, Any]], headers: Sequence[str] | None = None) -> str:
    """导出为 CSV 字符串（下载接口用），带 BOM。"""
    buffer = io.StringIO()
    if headers is None:
        ordered: list[str] = []
        for row in rows:
            for key in row.keys():
                if key not in ordered:
                    ordered.append(str(key))
        headers = ordered
    header_list = [str(h) for h in headers]
    writer = csv.DictWriter(buffer, fieldnames=header_list, extrasaction="ignore", restval="")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _stringify(row.get(k, "")) for k in header_list})
    return "\ufeff" + buffer.getvalue()


def cents_to_yuan_str(cents: int | None) -> str:
    """分 → 元字符串（导出 CSV 用，避免浮点误差）。"""
    if cents is None:
        return ""
    negative = cents < 0
    value = abs(int(cents))
    text = f"{value // 100}.{value % 100:02d}"
    return f"-{text}" if negative else text


def yuan_str_to_cents(text: str | None) -> int:
    """元字符串 → 分（导入 CSV 用），空值或非数字返回 0。"""
    if not text:
        return 0
    try:
        return int(round(float(str(text).strip()) * 100))
    except (TypeError, ValueError):
        return 0


def _stringify(value: Any) -> str:
    """CSV 单元格序列化：None→空串，bool→0/1，dict/list→JSON。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (dict, list)):
        import json

        return json.dumps(value, ensure_ascii=False)
    return str(value)


def iter_csv_chunks(src: str | Path, chunk_size: int = 500) -> Iterable[list[dict[str, str]]]:
    """大文件分块读取，避免一次性载入内存。"""
    buffer: list[dict[str, str]] = []
    for row in read_csv_dicts(src):
        buffer.append(row)
        if len(buffer) >= chunk_size:
            yield buffer
            buffer = []
    if buffer:
        yield buffer


__all__ = [
    "DEFAULT_ENCODING",
    "MAPPING_CSV_HEADERS",
    "ORDER_IMPORT_CSV_HEADERS",
    "PURCHASE_CSV_HEADERS",
    "TRACKING_CSV_HEADERS",
    "append_csv_rows",
    "cents_to_yuan_str",
    "dumps_csv",
    "export_csv",
    "iter_csv_chunks",
    "read_csv_dicts",
    "yuan_str_to_cents",
]
