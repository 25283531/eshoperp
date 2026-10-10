"""通用小工具：UTC 时间、内容哈希、规格指纹、ZIP 打包。

（原 hashkit.py / dt.py / zipkit.py 已按 ARCH §3.1 C4 合并为本模块。）
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
ISO_FORMAT_MS = "%Y-%m-%dT%H:%M:%S.%fZ"
_MOCK_SEQ_LOCK_FILE = ".mock_seq"

# ---------------------------------------------------------------------------
#  手工录入货源的 ID / 平台约定
# ---------------------------------------------------------------------------
# ★ 为什么手工货源要打 `MANUAL-` 前缀：
#   `source_product.product_1688_id` 承载的是「货源侧商品唯一标识」，
#   手工录入没有 1688 商品 ID，但**又不能改表结构新增 source_platform 列**
#   （约束：不动表 / 不新增迁移）。于是用前缀表达来源：
#       * `MANUAL-<code>`  → 手工录入 / CSV 导入
#       * 其它            → 1688 采集得到
#   同时在 `params_json["source_platform"]` 再记一份，避免只靠前缀做判断。
SOURCE_PLATFORM_KEY = "source_platform"
SOURCE_PLATFORM_MANUAL = "manual"
SOURCE_PLATFORM_1688 = "alibaba1688"
MANUAL_ID_PREFIX = "MANUAL-"


def manual_source_id(product_code: str) -> str:
    """手工货源商品编码 → `source_product.product_1688_id`（幂等：`MANUAL-` 不重复加）。

    Args:
        product_code: 运营手填的商品编码（如 `SZ-1002`）。

    Returns:
        `MANUAL-SZ-1002`；已带前缀时原样返回。
    """
    code = str(product_code or "").strip()
    if not code:
        return MANUAL_ID_PREFIX
    if code.upper().startswith(MANUAL_ID_PREFIX):
        return code
    return f"{MANUAL_ID_PREFIX}{code}"


def derive_source_platform(product_1688_id: str | None, params_json: Mapping[str, Any] | None = None) -> str:
    """推导货源来源平台（`manual` / `alibaba1688`）。

    判定优先级：`params_json["source_platform"]` → ID 前缀 → 兜底 `alibaba1688`。
    """
    declared = str((params_json or {}).get(SOURCE_PLATFORM_KEY) or "").strip().lower()
    if declared:
        return declared
    return SOURCE_PLATFORM_MANUAL if str(product_1688_id or "").startswith(MANUAL_ID_PREFIX) else SOURCE_PLATFORM_1688


# ---------------------------------------------------------------------------
#  1688 商品链接解析
# ---------------------------------------------------------------------------
# ★ 为什么必须解析整条链接：前端「1688 采集」的 placeholder 就是
#   `https://detail.1688.com/offer/123456.html`，使用者复制的一定是整条 URL；
#   早年后端把这个字符串整体当作 productID 打给开放平台 ⇒ 恒失败。
# ★ 覆盖的形态（正则从左到右逐步放宽）：
#       https://detail.1688.com/offer/694567890123.html
#       https://detail.1688.com/offer/694567890123.htm?spm=a260k.1.bxxx
#       http://m.1688.com/offer/694567890123.html#anchor
#       detail.1688.com/offer/694567890123.html      （没复制 scheme）
#       https://detail.1688.com/offer/694567890123   （没 .html）
#       https://detail.1688.com/?offerId=694567890123 （活动页带查询参数）
#       694567890123                                  （纯数字 Offer ID）
_OFFER_URL_RE = re.compile(
    r"(?:https?://)?(?:[a-z0-9-]+\.)*1688\.com/offer/(\d{4,32})(?:[^\d]|$)",
    re.IGNORECASE,
)
_OFFER_QUERY_RE = re.compile(r"[?&](?:offerI[dD]|productI[dD])=(\d{4,32})")
_PURE_ID_RE = re.compile(r"^\d{4,32}$")

PARSE_1688_ERROR = (
    "无法从链接中识别商品 ID，请检查是否复制完整"
    "（支持 https://detail.1688.com/offer/123456.html 或纯数字 Offer ID）"
)


def parse_1688_product_id(identifier: str) -> tuple[str, str]:
    """从「1688 商品链接 / 纯数字 ID」中提取 Offer ID。

    Args:
        identifier: 使用者粘贴的原始输入（可以是整条 URL）。

    Returns:
        `(offer_id, reason)`：成功时 reason 为空串，失败时 offer_id 为空串、
        reason 是**人话**错误提示（直接回给使用者即可）。
    """
    # ★ 从网页复制的链接常带不换行空格（U+00A0），strip() 吃不掉它
    text = str(identifier or "").replace("\u00a0", " ").strip()
    if not text:
        return "", "1688 商品链接或商品 ID 不能为空"

    # 去掉 URL 里的跟踪参数与锚点，避免 (.html?spm=...) 影响尾部匹配
    text = text.split("#", 1)[0].strip()

    match = _OFFER_URL_RE.search(text)
    if match:
        return match.group(1), ""

    match = _OFFER_QUERY_RE.search(text)
    if match:
        return match.group(1), ""

    if _PURE_ID_RE.match(text):
        return text, ""

    # 带 .html 但域名不是 1688 → 明确报错比静默当 ID 更有用
    if text.lower().startswith(("http://", "https://")):
        return "", PARSE_1688_ERROR + f"（看起来不是 1688 商品链接：{text[:80]}）"
    return "", PARSE_1688_ERROR + f"（收到：{text[:80]}）"


# ---------------------------------------------------------------------------
#  时间工具（§10.4：存储 UTC，传输 ISO8601）
# ---------------------------------------------------------------------------


def utc_now() -> dt.datetime:
    """返回当前 UTC 时间（naive datetime，数据库统一按 UTC 存储）。"""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def utc_now_iso() -> str:
    """返回当前 UTC 时间的 ISO8601 字符串，如 `2026-10-08T02:30:00Z`。"""
    return iso_utc(utc_now())


def iso_utc(value: dt.datetime | None = None) -> str:
    """将 datetime 序列化为 ISO8601 UTC 字符串。naive datetime 视为 UTC。"""
    if value is None:
        value = utc_now()
    if value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value.strftime(ISO_FORMAT)


def parse_iso(value: str | None) -> dt.datetime | None:
    """解析 ISO8601 字符串为 UTC naive datetime；解析失败返回 None。"""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        for fmt in (ISO_FORMAT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = dt.datetime.strptime(value.strip(), fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


def add_hours(value: dt.datetime, hours: int) -> dt.datetime:
    """时间加减小时。"""
    return value + dt.timedelta(hours=hours)


def minutes_ago(minutes: int) -> dt.datetime:
    """返回 N 分钟前的 UTC 时间（增量拉取起点）。"""
    return utc_now() - dt.timedelta(minutes=minutes)


def to_timestamp_ms(value: dt.datetime | None = None) -> int:
    """返回毫秒时间戳。"""
    if value is None:
        value = utc_now()
    return int(value.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


# ---------------------------------------------------------------------------
#  内容哈希与规格指纹（AST-P0-01 去重 / MAP-P0-04 变更检测）
# ---------------------------------------------------------------------------


def content_hash_bytes(data: bytes) -> str:
    """计算字节内容的 sha256 十六进制摘要（素材去重依据）。"""
    return hashlib.sha256(data).hexdigest()


def content_hash_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    """流式计算文件内容的 sha256，避免大文件占内存。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def spec_signature(spec_json: Mapping[str, Any] | None) -> str:
    """计算规格指纹：规格名值对按键排序后 md5（MAP-P0-04）。

    规格为空时返回空串，调用方据此判定"无规格"而非"规格不匹配"。
    """
    if not spec_json:
        return ""
    normalized = {str(k).strip(): ("" if v is None else str(v).strip()) for k, v in spec_json.items()}
    canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.md5(canonical.encode("utf-8")).hexdigest()


def signature_matches(left: str | None, right: str | None) -> bool:
    """比较两个规格指纹是否一致（空值视为"未采集"，不做不匹配判定）。"""
    if not left or not right:
        return True
    return left == right


# ---------------------------------------------------------------------------
#  Mock ID 生成（§10.10：`MOCK-{platform}-{timestamp}-{seq}`）
# ---------------------------------------------------------------------------


def mock_item_id(platform: str, seq: int | None = None, prefix: str = "MOCK") -> str:
    """生成肉眼可辨的 Mock 商品 ID。"""
    if seq is None:
        seq = _next_mock_seq()
    timestamp = utc_now().strftime("%Y%m%d%H%M%S")
    return f"{prefix}-{platform.upper()}-{timestamp}-{seq}"


def mock_sku_code(platform: str, index: int, seq: int | None = None) -> str:
    """生成 Mock SKU 编码。"""
    base = mock_item_id(platform, seq)
    return f"{base}-SKU{index:02d}"


def _next_mock_seq() -> int:
    """进程内自增序号（不落库，仅用于 ID 唯一性）。"""
    global _MOCK_SEQ
    _MOCK_SEQ += 1
    return _MOCK_SEQ


_MOCK_SEQ = 0


# ---------------------------------------------------------------------------
#  ZIP 打包（半自动素材包）
# ---------------------------------------------------------------------------


def make_zip_package(
    files: Sequence[str | Path],
    dest: str | Path,
    extra_files: Mapping[str, str] | None = None,
    base_dir: str | Path | None = None,
    arcname_map: Mapping[str, str] | None = None,
) -> str:
    """打包半自动素材包 ZIP。

    Args:
        files: 需要打包的本地文件（图片等）。
        dest: 输出 ZIP 绝对路径。
        extra_files: 额外写入的文本文件 `{包内相对路径: 文本内容}`（如 README.txt）。
        base_dir: 文件在包内的根目录，默认 `images/`。
        arcname_map: 指定单个文件的包内路径（覆盖默认规则）。

    Returns:
        生成的 ZIP 绝对路径字符串。
    """
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    root = str(base_dir or "images").strip("/")

    with zipfile.ZipFile(dest_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, item in enumerate(files):
            file_path = Path(item)
            if not file_path.exists():
                continue
            if arcname_map and str(item) in arcname_map:
                arcname = arcname_map[str(item)]
            else:
                suffix = file_path.suffix or ".jpg"
                arcname = f"{root}/{'main' if index == 0 else 'detail'}_{index:02d}{suffix}"
            archive.write(file_path, arcname=arcname)

        for name, content in (extra_files or {}).items():
            archive.writestr(name, content)

    return str(dest_path)


def safe_filename(name: str, max_length: int = 120) -> str:
    """生成文件系统安全的文件名（去除路径分隔符与控制字符）。"""
    cleaned = "".join(ch for ch in name if ch.isprintable() and ch not in '/\\:*?"<>|').strip()
    cleaned = cleaned.replace(" ", "_")
    return (cleaned or "unnamed")[:max_length]


def ensure_dir(path: str | Path) -> Path:
    """确保目录存在并返回 Path。"""
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def human_size(num_bytes: int) -> str:
    """人类可读的文件大小。"""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{size:.1f}GB"


def json_dumps(data: Any) -> str:
    """统一 JSON 序列化（中文不转义、按 key 排序）。"""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)


def chunked(items: Iterable[Any], size: int) -> list[list[Any]]:
    """将可迭代对象按固定大小切片。"""
    chunk: list[Any] = []
    result: list[list[Any]] = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= size:
            result.append(chunk)
            chunk = []
    if chunk:
        result.append(chunk)
    return result


__all__ = [
    "ISO_FORMAT",
    "MANUAL_ID_PREFIX",
    "SOURCE_PLATFORM_1688",
    "SOURCE_PLATFORM_KEY",
    "SOURCE_PLATFORM_MANUAL",
    "add_hours",
    "chunked",
    "content_hash_bytes",
    "content_hash_file",
    "derive_source_platform",
    "ensure_dir",
    "human_size",
    "iso_utc",
    "json_dumps",
    "make_zip_package",
    "manual_source_id",
    "minutes_ago",
    "mock_item_id",
    "mock_sku_code",
    "parse_1688_product_id",
    "parse_iso",
    "safe_filename",
    "signature_matches",
    "spec_signature",
    "to_timestamp_ms",
    "utc_now",
    "utc_now_iso",
]
