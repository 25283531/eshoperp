"""1688 货源采集适配器（§3 目录结构 / §4.4.1）。

职责：
    1. 采集 1688 商品详情（**已按官方 OpenAPI 签名规则实装**，需 AppKey / AppSecret / AccessToken）；
    2. **SKU 规格树笛卡尔展开** —— 纯函数，可离线使用；
    3. **规格指纹计算** —— 与 `SkuMapping.spec_signature` 同源（utils.kit.spec_signature）。

================================================================================
★ 签名规则（2026-10-10 用真实凭证实测确认，**与网上流传的说法不同**）
================================================================================
网上大量教程写「MD5 + 首尾各包一次 AppSecret」—— 那样打网关一律 `gw.SignatureMissing`。
官方规则（open.1688.com/doc/signature.htm）如下：

    请求 URL：
        https://gw.open.1688.com/openapi/param2/1/{namespace}/{method}/{appKey}
        ★ AppKey 必须放在 **URL 路径段末尾**；放进 query 会报 `gw.AppKeyMissing`

    签名串 = urlPath + 排序后的参数串
        urlPath = `param2/1/{namespace}/{method}/{appKey}`
                  （从 `param2` 起、到 `?` 为止；**不含域名，也不含 `/openapi/`**）
        参数串  = 每个参数把 key 与 value **直接拼**成 `k1v1k2v2`，按 key 升序连接
        sign    = uppercase(hex(HMAC-SHA1(签名串, appSecret)))
                  ★ HMAC-SHA1，不是 MD5，也不是首尾包秘钥

    query 参数：
        _aop_signature  签名值（**它自己不参与签名**）
        _aop_timestamp  毫秒时间戳
        access_token    授权令牌
        业务参数         如 productId

实测结果（2026-10-10，AppKey 8670841）：
    * `param2/1/system/currentTime/{appKey}`            → HTTP 200 `"20261010164552223+0800"`
    * `param2/1/com.alibaba.product/alibaba.product.get` → `gw.APIACLDecline`（**未订购该 API**）
    * 不带 access_token                                  → `401 Request need user authenticated`
⇒ 签名链路正确；商品详情取不到是**账号侧权限未开通**，不是代码缺陷。

★ 待实测确认（★ PROBE-PENDING）：`alibaba.product.get` 的 ACL 权限开通前拿不到真实响应体，
  因此**详情图字段名不敢单点假设**，一律走多别名防御性遍历（见 `extract_image_urls`）。
  业务参数名 `productId` 同样未实测，权限开通后需复核。
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.utils.kit import (
    iso_utc,
    parse_1688_product_id,
    spec_signature,
    utc_now,
)

logger = get_logger(__name__)

__all__ = [
    "Alibaba1688Adapter",
    "CollectedProduct",
    "CollectedSku",
    "DETAIL_IMAGE_FIELD_ALIASES",
    "expand_spec_tree",
    "extract_image_urls",
    "normalize_spec_json",
    "sku_code_from_spec",
]

# ---------------------------------------------------------------------------
#  网关常量
# ---------------------------------------------------------------------------
# ★ 网关主机 **不含** /openapi —— 签名用的 urlPath 从 `param2` 起算，
#   把 `/openapi` 一并加入会给后面的路径拼接造成歧义，所以这里只存 scheme + host。
GATEWAY_URL = "https://gw.open.1688.com"
OPENAPI_PREFIX = "openapi"
API_VERSION = "param2/1"

# 商品详情接口（★ ACL 权限需在 open.1688.com 控制台为应用开通）
PRODUCT_NAMESPACE = "com.alibaba.product"
PRODUCT_METHOD = "alibaba.product.get"

# 自证接口：唯一能证明「签名算法 + 凭证」正确的最小 API（无 ACL 限制）
PROBE_NAMESPACE = "system"
PROBE_METHOD = "currentTime"

# 单张图片下载上限（字节），防止异常响应把内存打爆
MAX_IMAGE_BYTES = 20 * 1024 * 1024

# 申请 API 权限的控制台入口（错误提示里要给使用者可点的下一步）
OPEN_PLATFORM_CONSOLE = "https://open.1688.com"

# ★ 本适配器在 `credential` 表里的定位键（单一事实来源，`SourceService` 从这里导入）。
#   凭证的具体取值由调用方注入，这里**只有坐标，没有明文**。
CREDENTIAL_OWNER_TYPE = "source"
CREDENTIAL_OWNER_KEY = "alibaba1688"


@dataclass
class CollectedSku:
    """采集到的单个货源 SKU。"""

    sku_code_1688: str
    spec_json: dict[str, str] = field(default_factory=dict)
    spec_signature: str = ""
    cost_price_cents: int = 0
    stock_qty: int = 0
    status: str = "on_sale"

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "sku_code_1688": self.sku_code_1688,
            "spec_json": dict(self.spec_json),
            "spec_signature": self.spec_signature,
            "cost_price_cents": self.cost_price_cents,
            "stock_qty": self.stock_qty,
            "status": self.status,
        }


@dataclass
class CollectedProduct:
    """采集到的货源商品（含 SKU 列表与图片列表）。"""

    product_1688_id: str
    title: str = ""
    category_path: str = ""
    supplier_1688_id: str = ""
    cost_price_cents: int = 0
    origin_url: str = ""
    main_image_url: str = ""
    # ★ 详情图 URL 列表（**普通数据结构，不是表列**；完整响应仍在 `raw` 里）
    detail_image_urls: list[str] = field(default_factory=list)
    params_json: dict[str, Any] = field(default_factory=dict)
    skus: list[CollectedSku] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    collected_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化（不含 raw，避免日志膨胀）。"""
        return {
            "product_1688_id": self.product_1688_id,
            "title": self.title,
            "category_path": self.category_path,
            "supplier_1688_id": self.supplier_1688_id,
            "cost_price_cents": self.cost_price_cents,
            "origin_url": self.origin_url,
            "main_image_url": self.main_image_url,
            "detail_image_count": len(self.detail_image_urls),
            "params_json": dict(self.params_json),
            "skus": [sku.to_dict() for sku in self.skus],
            "collected_at": self.collected_at or iso_utc(utc_now()),
        }


def normalize_spec_json(spec: dict[str, Any] | None) -> dict[str, str]:
    """规范化规格名值对：去空白、去空值、按键排序。"""
    if not spec:
        return {}
    normalized: dict[str, str] = {}
    for key, value in spec.items():
        k = str(key).strip()
        v = "" if value is None else str(value).strip()
        if k:
            normalized[k] = v
    return dict(sorted(normalized.items(), key=lambda kv: kv[0]))


def expand_spec_tree(spec_tree: dict[str, list[Any]] | None) -> list[dict[str, str]]:
    """★ SKU 规格树笛卡尔展开。

    输入示例：
        {"颜色": ["红", "蓝"], "尺码": ["S", "XL"]}

    输出：
        [{"颜色": "红", "尺码": "S"}, {"颜色": "红", "尺码": "XL"},
         {"颜色": "蓝", "尺码": "S"}, {"颜色": "蓝", "尺码": "XL"}]

    Args:
        spec_tree: `{规格名: [规格值, ...]}`。

    Returns:
        笛卡尔积后的规格组合列表（顺序稳定：按规格名排序后逐级展开）。
    """
    if not spec_tree:
        return []
    cleaned: dict[str, list[str]] = {}
    for key, values in spec_tree.items():
        name = str(key).strip()
        if not name:
            continue
        if not isinstance(values, (list, tuple)):
            values = [values]
        cleaned[name] = [str(v).strip() for v in values if str(v).strip()]

    if not cleaned:
        return []

    names = sorted(cleaned.keys())
    combos: list[dict[str, str]] = []
    for values in itertools.product(*(cleaned[name] for name in names)):
        combos.append({name: value for name, value in zip(names, values)})
    return combos


def sku_code_from_spec(product_1688_id: str, spec_json: dict[str, str]) -> str:
    """由商品 ID 与规格生成稳定的 1688 SKU 编码（无官方编码时的兜底）。"""
    normalized = normalize_spec_json(spec_json)
    if not normalized:
        return f"{product_1688_id}-DEFAULT"
    signature = spec_signature(normalized)
    return f"{product_1688_id}-{signature[:12].upper()}"


# ---------------------------------------------------------------------------
#  ★ 详情图多别名提取（防御性遍历）
# ---------------------------------------------------------------------------
# ★ 详情图字段名待权限开通后实测确认（PROBE-PENDING）。
#   1688 不同版本的商品接口里，详情图可能是下面任意一个 key，且结构可能是
#   `["url1","url2"]` / `[{"url":"..."}]` / 富文本 HTML 字符串，
#   所以不做单点字段假设，而是「key 含图片语义」+「值长得像图片 URL」双线索收集。
DETAIL_IMAGE_FIELD_ALIASES: tuple[str, ...] = (
    "descImageList",
    "detailImageList",
    "descImages",
    "detailImages",
    "descImageUrlList",
    "detailImageUrlList",
    "imageList",
    "images",
    "picList",
    "pictureList",
    "detail",
    "description",
    "desc",
    "descPath",
    "detailUrl",
)

# key 里出现这些片段就认为「这个字段大概率是图片」
_IMAGE_KEY_HINTS: tuple[str, ...] = ("image", "img", "pic", "photo", "thumb", "desc", "detail")
_IMAGE_EXTENSIONS: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".svg")
_HTML_IMG_SRC_RE = re.compile(r"""(?:src|data-ks-lazyload)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def extract_image_urls(node: Any, *, max_depth: int = 6, max_items: int = 60) -> list[str]:
    """★ 防御性遍历任意响应结构，收集所有图片 URL（去重、保序）。

    ★ 详情图字段名待权限开通后实测确认，因此这里**不绑定任何单一字段名**：
        * dict 的 key 命中 `DETAIL_IMAGE_FIELD_ALIASES` 或含 `_IMAGE_KEY_HINTS` 片段 → 收集其值；
        * 任何长得像图片的字符串（http(s) 且带图片扩展名 / alicdn 域名）→ 收集；
        * 富文本 HTML（含 `<img`）→ 抽 `src=` 里的地址。
      宁可多收几种写法，也不假装确定了字段名。

    Args:
        node: 任意 JSON 节点（dict / list / str）。
        max_depth: 递归深度上限（防止畸形结构把栈跑穿）。
        max_items: 收集数量上限（详情图可能上百张，采集侧另行截断）。

    Returns:
        去重后的图片 URL 列表（保持首次出现顺序）。
    """
    found: list[str] = []
    seen: set[str] = set()

    def _add(value: Any, *, force: bool = False) -> None:
        if len(found) >= max_items:
            return
        url = str(value or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return
        if not force and not _looks_like_image(url):
            return
        if url in seen:
            return
        seen.add(url)
        found.append(url)

    def _collect_html(text: str) -> None:
        if "<img" not in text.lower():
            return
        for src in _HTML_IMG_SRC_RE.findall(text):
            _add(src, force=True)

    def _walk(current: Any, depth: int) -> None:
        if depth > max_depth or len(found) >= max_items:
            return
        if isinstance(current, Mapping):
            for key, value in current.items():
                _walk((key, value), depth)
        elif isinstance(current, tuple) and len(current) == 2 and isinstance(current[0], str):
            key, value = current
            lowered = str(key).lower()
            if isinstance(value, str):
                if lowered in DETAIL_IMAGE_FIELD_ALIASES or any(h in lowered for h in _IMAGE_KEY_HINTS):
                    _add(value, force=True)
                    _collect_html(value)
                else:
                    _add(value)
                    _collect_html(value)
            else:
                _walk(value, depth + 1)
        elif isinstance(current, (list, tuple)):
            for item in current:
                _walk(item, depth + 1)
        elif isinstance(current, str):
            _add(current)
            _collect_html(current)

    _walk(node, 0)
    return found


def _looks_like_image(url: str) -> bool:
    """粗判 URL 是否指向图片（去 query 后看扩展名 / 图片 CDN 域名）。"""
    path = url.split("?", 1)[0].lower()
    if any(path.endswith(ext) for ext in _IMAGE_EXTENSIONS):
        return True
    return "alicdn.com" in path or "/img/" in path or "img.1688" in path


class Alibaba1688Adapter:
    """1688 商品详情采集适配器。

    ★ 采集为只读能力（红线 R2）：只 GET 开放平台商品数据，不持有任何写店铺的能力。

    ★ 凭证**不在此处硬编码**：由调用方（`SourceService.collect()`）从 `credential` 表
      读取解密后注入。留空时 `configured` 为 False，`collect()` 返回明确的可操作原因。
    """

    adapter_name = "alibaba1688"

    def __init__(
        self,
        *,
        app_key: str = "",
        app_secret: str = "",
        access_token: str = "",
        endpoint: str = GATEWAY_URL,
        namespace: str = PRODUCT_NAMESPACE,
        method: str = PRODUCT_METHOD,
        timeout_sec: float = 20.0,
        max_retry: int = 2,
    ) -> None:
        """初始化 1688 适配器（凭证为空时只能使用离线解析能力）。

        Args:
            endpoint: 网关根地址（**不含** `/openapi`），默认 `https://gw.open.1688.com`。
            max_retry: **仅对 5xx / 网络异常**重试；4xx 是确定的业务错误，重试没有意义。
        """
        settings = get_settings()
        self.app_key = str(app_key or "").strip()
        self.app_secret = str(app_secret or "").strip()
        self.access_token = str(access_token or "").strip()
        self.endpoint = str(endpoint or GATEWAY_URL).rstrip("/")
        self.namespace = str(namespace or PRODUCT_NAMESPACE)
        self.method = str(method or PRODUCT_METHOD)
        self.timeout_sec = float(timeout_sec or settings.http_timeout_sec)
        self.max_retry = max(int(max_retry), 0)

    @property
    def configured(self) -> bool:
        """是否已具备**发起签名请求**的最低凭证（AppKey + AppSecret + AccessToken）。"""
        return bool(self.app_key and self.app_secret and self.access_token)

    def missing_credentials(self) -> list[str]:
        """返回缺失的凭证项（用于生成人话错误提示）。"""
        missing: list[str] = []
        if not self.app_key:
            missing.append("app_key")
        if not self.app_secret:
            missing.append("app_secret（签名必需，非 MD5 而是 HMAC-SHA1）")
        if not self.access_token:
            missing.append("access_token")
        return missing

    # ---------------- 签名与请求 ----------------

    def _url_path(self, namespace: str, method: str) -> str:
        """构造参与签名的 urlPath：`param2/1/{namespace}/{method}/{appKey}`。

        ★ 必须是 **不含域名、不含 `/openapi/`** 的路径；AppKey 在末段。
        """
        return f"{API_VERSION}/{namespace}/{method}/{self.app_key}"

    def _sign(self, url_path: str, params: Mapping[str, Any]) -> str:
        """★ 官方签名： `UPPER(HMAC-SHA1(urlPath + 排序后的 key+value 串, appSecret))`。

        ★ 不是 MD5，也不是 `secret + 串 + secret` —— 那两种写法在 gw.open.1688.com
          一律返回 `gw.SignatureMissing`。本实现已用真实凭证调通 `system/currentTime`。

        Args:
            url_path: `_url_path()` 的结果。
            params: **参与签名**的参数（不含 `_aop_signature`）。

        Returns:
            大写十六进制签名串（40 字符）。
        """
        sorted_items = sorted(params.items(), key=lambda item: str(item[0]))
        concatenated = "".join(f"{key}{'' if value is None else value}" for key, value in sorted_items)
        return hmac.new(
            self.app_secret.encode("utf-8"),
            (url_path + concatenated).encode("utf-8"),
            hashlib.sha1,
        ).hexdigest().upper()

    def _signed_query(
        self, namespace: str, method: str, business_params: Mapping[str, Any]
    ) -> tuple[str, dict[str, str]]:
        """返回 `(完整 URL, query 参数)`；`_aop_signature` 单独追加，**不参与签名**。"""
        url_path = self._url_path(namespace, method)
        signable: dict[str, str] = {
            str(key): ("" if value is None else str(value))
            for key, value in business_params.items()
            if value is not None
        }
        if self.access_token:
            signable["access_token"] = self.access_token
        signable["_aop_timestamp"] = str(int(time.time() * 1000))

        query = dict(signable)
        query["_aop_signature"] = self._sign(url_path, signable)
        return f"{self.endpoint}/{OPENAPI_PREFIX}/{url_path}", query

    async def _request(
        self, namespace: str, method: str, business_params: Mapping[str, Any]
    ) -> tuple[bool, Any, str]:
        """发起一次已签名的 GET 请求。

        Returns:
            `(ok, payload, message)`：ok 为 True 时 payload 是解析后的 JSON（或纯文本）。
            ★ 绝不抛异常 —— 网络错误被翻译成决定性失败原因。
        """
        url, query = self._signed_query(namespace, method, business_params)
        last_error = ""
        for attempt in range(self.max_retry + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                    response = await client.get(url, params=query)
            except Exception as exc:  # noqa: BLE001  网络层异常必须收敛成可读原因
                last_error = f"无法连接 1688 网关：{type(exc).__name__}: {exc}"
                if attempt < self.max_retry:
                    await _sleep_backoff(attempt)
                    continue
                return False, None, last_error

            body = response.text
            if response.status_code >= 500:
                last_error = self._explain_error(
                    response.status_code, body, namespace=namespace, method=method
                )
                if attempt < self.max_retry:
                    await _sleep_backoff(attempt)
                    continue
                return False, None, last_error
            if response.status_code >= 400:
                return False, None, self._explain_error(
                    response.status_code, body, namespace=namespace, method=method
                )
            return True, _safe_json(body), ""

        return False, None, last_error or "未知失败"

    def _explain_error(self, status: int, body: str, *, namespace: str, method: str) -> str:
        """★ 把网关错误翻译成**使用者照着做就能推进**的下一步。

        分级：ACL 未授权 / 接口名不存在 / 签名失败 / AppKey 位置错误 / token 失效 /
             商品不存在 / 网关 5xx / 其它。
        """
        payload = _safe_json(body)
        error_code = ""
        error_message = ""
        if isinstance(payload, dict):
            error_code = str(payload.get("error_code") or "")
            error_message = str(payload.get("error_message") or payload.get("exception") or "")
        snippet = (error_message or body or "").strip().replace("\n", " ")
        if len(snippet) > 200:
            snippet = snippet[:200] + "…"

        api = f"{namespace}/{method}"

        if "APIACLDecline" in error_code or "acl" in error_code.lower():
            return (
                f"1688 拒绝调用「{api}」：应用未被授予该接口的调用权限（{snippet}）。\n"
                f"请到 {OPEN_PLATFORM_CONSOLE} → 我的应用 → 选中 AppKey {self.app_key or '(未配置)'} → "
                f"接口/API 权限，订购或申请『{method}』（商品详情）后重试。\n"
                "★ 这是**账号权限**问题，不是系统故障：同一凭证调 system/currentTime 能返回 200，"
                "说明签名链路本身是对的。"
            )
        if "APIUnsupported" in error_code or "unsupport api" in snippet.lower():
            return (
                f"1688 网关不认识这个接口名「{api}」（{snippet}）。"
                "请核对 open.1688.com 官方接口文档中的 namespace 与方法名；"
                "注意 1688 与淘宝开放平台的接口名并不通用。"
            )
        if "AppKeyMissing" in error_code:
            return (
                "1688 网关没收到 AppKey：AppKey 必须放在 **URL 路径段末尾** "
                f"（`.../openapi/{API_VERSION}/{namespace}/{method}/{{appKey}}`），放进 query 会被判缺失。"
            )
        if any(token in error_code for token in ("SignatureMissing", "InvalidSignature", "SignatureNotMatch")):
            return (
                f"1688 判定签名错误（{error_code}：{snippet}）。"
                f"请核对 AppSecret 是否与 AppKey {self.app_key or '(未配置)'} 配对、是否在控制台被重置过；"
                "本实现按官方规则用 HMAC-SHA1 生成，可用 system/currentTime 自证。"
            )
        if status == 401 or "authenticated" in snippet.lower():
            return (
                f"1688 要求重新授权（HTTP {status}：{snippet}）：access_token 已失效或过期，"
                "请重新走一次授权拿到新 token，再到「系统设置 → 凭证」里更新 access_token。"
            )
        if "not exist" in snippet.lower() or "不存在" in snippet:
            return f"1688 返回商品不存在（{snippet}）：请确认 offer ID 是否正确、商品是否已下架/删除。"
        if status >= 500:
            return f"1688 网关暂时不可用（HTTP {status}：{snippet}），请稍后重试。"
        return f"调用 1688 失败（HTTP {status}）：{snippet or '网关未返回可读原因'}"

    async def probe_signature(self) -> dict[str, Any]:
        """★ 签名自证：调 `system/currentTime`，HTTP 200 即证明算法与凭证正确。

        Returns:
            `{"ok": bool, "http_status": int, "message": str, "server_time": str}`。
        """
        if not (self.app_key and self.app_secret):
            missing = "、".join(self.missing_credentials())
            return {"ok": False, "http_status": 0, "message": f"未配置凭证（缺少 {missing}），无法自证", "server_time": ""}
        ok, payload, message = await self._request(PROBE_NAMESPACE, PROBE_METHOD, {})
        return {
            "ok": ok,
            "http_status": 200 if ok else 0,
            "message": message or ("" if ok else "未返回数据"),
            "server_time": str(payload or "").strip('"') if ok else "",
        }

    # ---------------- 采集 ----------------

    async def collect(self, product_1688_id: str) -> tuple[bool, CollectedProduct | None, str]:
        """采集商品详情（**入参可以是整条商品链接，也可以是纯数字 ID**）。

        Returns:
            `(ok, product, message)`。★ 绝不抛异常，失败返回 `(False, None, 原因)`。
        """
        if not product_1688_id or not str(product_1688_id).strip():
            return False, None, "缺少 1688 商品 ID"

        # ★ 前端 placeholder 就是 `https://detail.1688.com/offer/123456.html`，
        #   使用者粘贴的是**整条链接**；不解析就把整串当 ID 打给网关，必然失败。
        pid, reason = parse_1688_product_id(str(product_1688_id))
        if not pid:
            return False, None, reason

        if not self.configured:
            missing = "、".join(self.missing_credentials())
            return (
                False,
                None,
                f"未配置 1688 采集凭证（缺少 {missing}）："
                "请在「系统设置 → 凭证」中新增三行凭证——"
                f"owner_type={CREDENTIAL_OWNER_TYPE}、owner_key={CREDENTIAL_OWNER_KEY}，"
                "credential_key 分别为 app_key / app_secret / access_token；"
                f"AppKey 与 AppSecret 在 {OPEN_PLATFORM_CONSOLE} 的应用详情页领取。",
            )

        # ★ 业务参数名 productId 未实测（PROBE-PENDING：受 ACL 限制拿不到真实响应校验）
        ok, payload, message = await self._request(
            self.namespace, self.method, {"productId": pid, "webSite": "1688"}
        )
        if not ok:
            return False, None, message or "1688 未返回商品数据"
        if not isinstance(payload, (dict, list)):
            return False, None, f"1688 返回了非对象响应：{str(payload)[:120]}"

        product = self.parse_product(payload if isinstance(payload, dict) else {"product": payload}, fallback_id=pid)
        return True, product, f"采集成功，SKU {len(product.skus)} 个，详情图 {len(product.detail_image_urls)} 张"

    async def collect_batch(
        self, product_ids: list[str], *, delay_sec: float = 0.5
    ) -> list[tuple[str, CollectedProduct | None, str]]:
        """批量采集（内置简单限流，防配额超限，ARCH §11.1 Q4）。"""
        results: list[tuple[str, CollectedProduct | None, str]] = []
        for pid in product_ids:
            ok, product, message = await self.collect(pid)
            results.append((pid, product if ok else None, message))
            if delay_sec > 0:
                await _sleep_backoff(0, base=delay_sec)
        return results

    # ---------------- 解析（纯函数，可离线使用）----------------

    def parse_product(self, raw: dict[str, Any], *, fallback_id: str = "") -> CollectedProduct:
        """★ 防御性解析 1688 响应：字段缺失不崩，缺什么补默认。"""
        product = raw.get("product") if isinstance(raw.get("product"), dict) else raw

        product_id = str(
            product.get("productID") or product.get("product_id") or product.get("offerId") or fallback_id or ""
        )
        title = str(product.get("subject") or product.get("title") or "")
        category_path = str(product.get("categoryName") or product.get("category_path") or "")

        # 价格（1688 多为元字符串，转换为分）
        price_raw = product.get("price") or product.get("salePrice") or product.get("priceInfo")
        cost_cents = _to_cents(price_raw)

        image_url = str(product.get("imageUrl") or product.get("main_image_url") or "")
        supplier_id = str(product.get("supplierUserId") or product.get("memberId") or "")

        # ★ 图片：**多别名防御性遍历**（详情图字段名 PROBE-PENDING，不敢单点假设）
        images = extract_image_urls(product)
        if not images:
            images = extract_image_urls(raw)
        if not image_url and images:
            image_url = images[0]
        detail_images = [url for url in images if url != image_url]

        skus = self.parse_skus(product, fallback_product_id=product_id)
        if not skus:
            # 无 SKU 结构时构造一个默认 SKU，保证映射链路可用
            skus = [
                CollectedSku(
                    sku_code_1688=f"{product_id}-DEFAULT" if product_id else "DEFAULT",
                    spec_json={},
                    spec_signature="",
                    cost_price_cents=cost_cents,
                    stock_qty=int(product.get("amountOnSale", 0) or 0),
                )
            ]

        return CollectedProduct(
            product_1688_id=product_id,
            title=title,
            category_path=category_path,
            supplier_1688_id=supplier_id,
            cost_price_cents=cost_cents,
            origin_url=f"https://detail.1688.com/offer/{product_id}.html" if product_id else "",
            main_image_url=image_url,
            detail_image_urls=detail_images,
            params_json=_extract_params(product),
            skus=skus,
            raw=raw if isinstance(raw, dict) else {},
            collected_at=iso_utc(utc_now()),
        )

    def parse_skus(self, product: dict[str, Any], *, fallback_product_id: str = "") -> list[CollectedSku]:
        """解析 SKU 列表：支持显式 `skuList` 与 `specTree` 笛卡尔展开两种结构。"""
        skus: list[CollectedSku] = []

        raw_skus = product.get("skuList") or product.get("skus") or product.get("skuInfos")
        if isinstance(raw_skus, list):
            for item in raw_skus:
                if not isinstance(item, dict):
                    continue
                spec = _extract_spec(item)
                code = str(
                    item.get("skuCode")
                    or item.get("sku_code")
                    or item.get("specId")
                    or sku_code_from_spec(fallback_product_id, spec)
                )
                skus.append(
                    CollectedSku(
                        sku_code_1688=code,
                        spec_json=normalize_spec_json(spec),
                        spec_signature=spec_signature(normalize_spec_json(spec)),
                        cost_price_cents=_to_cents(item.get("price") or item.get("salePrice")),
                        stock_qty=int(item.get("amountOnSale", item.get("stock", 0)) or 0),
                        status=_status_from_stock(item.get("amountOnSale", item.get("stock"))),
                    )
                )

        if not skus:
            spec_tree = product.get("specTree") or product.get("spec_tree") or product.get("specs")
            if isinstance(spec_tree, dict):
                for spec in expand_spec_tree(spec_tree):
                    skus.append(
                        CollectedSku(
                            sku_code_1688=sku_code_from_spec(fallback_product_id, spec),
                            spec_json=normalize_spec_json(spec),
                            spec_signature=spec_signature(normalize_spec_json(spec)),
                            cost_price_cents=_to_cents(product.get("price")),
                            stock_qty=int(product.get("amountOnSale", 0) or 0),
                        )
                    )
        return skus

    # ---------------- 辅助：图片下载 ----------------

    @staticmethod
    async def download_image(url: str) -> tuple[bool, bytes, str]:
        """下载图片，**返回字节**：`(ok, 内容字节, message)`。

        ★ 单一语义：本方法**不落盘、不算 hash**，写盘与去重交给调用方
          （`SourceService._download_assets`）。历史上曾出现过「自己写盘并返回三元组，
          调用方又把三元组当字节传给 sha256」的 TypeError，本签名杜绝该混用。
        """
        if not url:
            return False, b"", "图片 URL 为空"
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(url, headers={"Referer": "https://detail.1688.com/"})
                if response.status_code >= 400:
                    return False, b"", f"下载失败：HTTP {response.status_code}"
                data = response.content
        except Exception as exc:  # noqa: BLE001
            return False, b"", f"下载失败：{type(exc).__name__}: {exc}"
        if not data:
            return False, b"", "下载失败：响应体为空"
        if len(data) > MAX_IMAGE_BYTES:
            return False, b"", f"图片超过 {MAX_IMAGE_BYTES // (1024 * 1024)}MB，已跳过"
        return True, data, "下载成功"

    async def health_check(self) -> dict[str, Any]:
        """★ 连通性自检 = 签名自证（`system/currentTime`）。

        ★ 早期实现是 GET 网关根路径 —— 那里不校验签名，返回 200 只能证明「网络通」，
          证明不了凭证对；真正的健康信号是「带签名的请求被网关接受」。
        """
        started = time.perf_counter()
        if not (self.app_key and self.app_secret):
            return {
                "adapter": self.adapter_name,
                "healthy": False,
                "status": "unknown",
                "message": "未配置 1688 AppKey / AppSecret",
                "latency_ms": 0,
            }
        result = await self.probe_signature()
        return {
            "adapter": self.adapter_name,
            "healthy": bool(result["ok"]),
            "status": "healthy" if result["ok"] else "down",
            "message": (
                f"签名自证通过，网关时间 {result['server_time']}" if result["ok"] else str(result["message"])
            ),
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }


async def _sleep_backoff(attempt: int, base: float = 0.5) -> None:
    """指数退避等待。"""
    import asyncio

    await asyncio.sleep(base * (2**attempt))


def _safe_json(text: str) -> Any:
    """解析响应体；非 JSON 时原样返回文本（`system/currentTime` 返回的是带引号的字符串）。"""
    import json

    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def _to_cents(value: Any) -> int:
    """价格转分：支持 `12.50` / `"12.50"` / `{"price": 12.5}` / 已是分的整数。"""
    if value is None:
        return 0
    if isinstance(value, dict):
        for key in ("price", "amount", "value"):
            if key in value:
                return _to_cents(value[key])
        return 0
    try:
        return int(round(float(str(value).strip()) * 100))
    except (TypeError, ValueError):
        return 0


def _extract_spec(item: dict[str, Any]) -> dict[str, str]:
    """从 SKU 项中提取规格名值对（★ 防御性：多种结构兼容）。"""
    spec: dict[str, str] = {}
    raw_spec = item.get("specJson") or item.get("spec_json") or item.get("attributes") or item.get("specAttrs")
    if isinstance(raw_spec, dict):
        for key, value in raw_spec.items():
            spec[str(key)] = "" if value is None else str(value)
    elif isinstance(raw_spec, list):
        for entry in raw_spec:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("attributeName") or "")
            value = str(entry.get("value") or entry.get("attributeValue") or "")
            if name:
                spec[name] = value
    return normalize_spec_json(spec)


def _status_from_stock(stock: Any) -> str:
    """库存 → 状态。"""
    try:
        quantity = int(stock or 0)
    except (TypeError, ValueError):
        return "on_sale"
    if quantity <= 0:
        return "out_of_stock"
    return "on_sale"


def _extract_params(product: dict[str, Any]) -> dict[str, Any]:
    """提取商品参数表（防御性）。"""
    raw = product.get("attributes") or product.get("params") or product.get("productFeatureList")
    if isinstance(raw, dict):
        return {str(k): ("" if v is None else str(v)) for k, v in raw.items()}
    if isinstance(raw, list):
        params: dict[str, Any] = {}
        for entry in raw:
            if isinstance(entry, dict):
                name = str(entry.get("name") or entry.get("attributeName") or "")
                value = entry.get("value") or entry.get("attributeValue") or ""
                if name:
                    params[name] = "" if value is None else str(value)
        return params
    return {}
