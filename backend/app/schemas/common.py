"""通用 Schema 基类与金额 / 时间工具（§10.2、§10.5）。

★ 金额约定（ARCH §5.5.0）：
    API 层一律用**字符串「元」**（`"29.90"`），DB 层用**整数「分」**（2990）。
    用字符串而非 float 是为了避免浮点误差在多次换算后放大。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

__all__ = [
    "BaseSchema",
    "IdVo",
    "MoneyField",
    "OkVo",
    "PageQuery",
    "cents_to_yuan",
    "iso_or_none",
    "parse_money",
]


class BaseSchema(BaseModel):
    """所有 Schema 的基类：允许从 ORM 属性构造，允许别名填充。"""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


MoneyField = str  # 金额字段类型别名：字符串「元」


def cents_to_yuan(cents: int | None) -> str:
    """整数「分」→ 字符串「元」（`2990 → "29.90"`）。None 返回空串。"""
    if cents is None:
        return ""
    negative = int(cents) < 0
    value = abs(int(cents))
    text = f"{value // 100}.{value % 100:02d}"
    return f"-{text}" if negative else text


def parse_money(text: str | float | int | None, *, field_name: str = "金额") -> int:
    """字符串「元」→ 整数「分」。

    Args:
        text: `"29.90"` / `29.9` / `"29"` 均接受；空值返回 0。
        field_name: 报错时展示的字段名。

    Returns:
        整数分。解析失败返回 0（调用方按需判 0 → 抛 1001）。

    Raises:
        ValueError: 无法解析为数字。
    """
    if text is None or text == "":
        return 0
    if isinstance(text, bool):  # bool 是 int 的子类，必须排除
        raise ValueError(f"{field_name} 不能为布尔值")
    if isinstance(text, int):
        # 纯整数输入按「元」解释（API 层约定），如 30 → 3000 分
        return int(text) * 100
    if isinstance(text, float):
        return int(round(text * 100))
    cleaned = str(text).strip().replace("¥", "").replace(",", "")
    if not cleaned:
        return 0
    try:
        return int(round(float(cleaned) * 100))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 格式不正确：{text}") from exc


def iso_or_none(value: datetime | None) -> str | None:
    """datetime → ISO8601 UTC 字符串；None 透传。"""
    if value is None:
        return None
    from app.utils.kit import iso_utc

    return iso_utc(value)


class PageQuery(BaseSchema):
    """分页查询参数（供非 FastAPI 依赖的场合复用）。"""

    page: int = 1
    page_size: int = 20


class IdVo(BaseSchema):
    """仅含 ID 的响应体（删除 / 幂等返回）。"""

    id: int = 0


class OkVo(BaseSchema):
    """通用成功响应体。"""

    ok: bool = True
    message: str = "ok"


def vo_list(items: Any, builder: Any) -> list[Any]:
    """批量构造 Vo（统一的防御性写法，空列表返回空列表）。"""
    if not items:
        return []
    return [builder(item) for item in items]
