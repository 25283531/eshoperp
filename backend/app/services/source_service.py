"""货源服务：供应商 / 货源商品 / 货源 SKU（§5.5.3）。

★ 两条录入路径（§5.5.3 增补）：
    1. `collect()` —— 1688 开放平台采集（**需要 AppKey / AccessToken**）；
    2. `create_manual()` / `import_csv()` —— 手工录入与 CSV 导入，**完全不碰 1688 适配器**。

   第 2 条不是"降级预案"，是**真实主路径**：本项目面向个体户 / 个人身份证店，
   1688 开放平台凭证大概率拿不到；若只能靠采集，系统里一条货源都进不来，
   后面的 AI 重构 / SKU 映射 / 上架 / 订单匹配全部无从起步。
"""

from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import func, or_, select

from app.adapters.source.alibaba1688 import CREDENTIAL_OWNER_KEY, Alibaba1688Adapter
from app.core.config import get_settings
from app.core.errors import BusinessError, ErrorCode, NotFoundError
from app.core.logging import get_logger, get_trace_id
from app.core.security import decrypt_credential
from app.models.asset import Asset
from app.models.enums import (
    AssetOrigin,
    AssetType,
    AuditActionType,
    AuditObjectType,
    CredentialOwnerType,
    CredentialStatus,
    SourceStatus,
)
from app.models.source import SourceProduct, SourceSku, Supplier
from app.models.system import Credential
from app.schemas.common import parse_money
from app.schemas.source import SourceCsvImportResult, SourceImportFailure, SourceProductManualCreate
from app.services.audit_service import AuditService
from app.utils.kit import (
    MANUAL_ID_PREFIX,
    SOURCE_PLATFORM_KEY,
    SOURCE_PLATFORM_MANUAL,
    content_hash_bytes,
    iso_utc,
    manual_source_id,
    parse_1688_product_id,
    spec_signature,
    utc_now,
)

logger = get_logger(__name__)

__all__ = [
    "MAX_COLLECT_IDENTIFIERS",
    "MAX_IMPORT_ROWS",
    "SOURCE_1688_OWNER_KEY",
    "SOURCE_PLATFORM_MANUAL",
    "SourceService",
    "load_source_credentials",
]

# 单次采集上限（§5.5.3：≤50）
MAX_COLLECT_IDENTIFIERS = 50

# ---------------------------------------------------------------------------
#  1688 采集：凭证定位键 + 素材下载上限
# ---------------------------------------------------------------------------
# ★ 1688 采集凭证在 `credential` 表里的坐标（与适配器同源的单一事实来源）。
#   **凭证一律不硬编码**，必须从这里读。
SOURCE_1688_OWNER_KEY = CREDENTIAL_OWNER_KEY
SOURCE_1688_CREDENTIAL_KEYS: tuple[str, ...] = ("app_key", "app_secret", "access_token")
# ★ bootstrap.py 给占位行填的就是这个值，读到它等价于「用户还没配」
PLACEHOLDER_CREDENTIAL = "__NOT_CONFIGURED__"
# 单个商品最多下载几张素材（主图 1 + 详情图 8），防止超大详情把采集拖死
MAX_DOWNLOAD_IMAGES = 9

# ---------------------------------------------------------------------------
#  手工录入 / CSV 导入的常量
# ---------------------------------------------------------------------------
# 单次 CSV 上限：运营用 Excel 整理的一批货通常几十行，500 行足够且能在一次请求内跑完
MAX_IMPORT_ROWS = 500
MAX_IMPORT_BYTES = 5 * 1024 * 1024
# 建议售价暂存的键（受"不改表结构"约束，写在 `source_product.params_json` 里）
SKU_PRICE_HINT_KEY = "sku_price_hints"
MAX_SKU_CODE_LEN = 128
MAX_PRODUCT_ID_LEN = 64

# ★ CSV 列名：中文优先（运营用 Excel 直接填），同时兼容英文名。
#   匹配前会把空格 / 全半角括号去掉并转小写，所以 "成本价（元）" 与 "成本价(元)" 等价。
CSV_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "title": ("商品标题", "标题", "title", "product_title", "productname"),
    "product_code": ("商品编码", "商品id", "货号", "货源编码", "product_code", "external_code"),
    "category_path": ("类目", "类目路径", "category", "category_path"),
    "supplier_id": ("供应商id", "供应商", "supplier_id", "supplier"),
    "cost_price": ("商品成本价", "成本价", "成本", "cost_price"),
    "origin_url": ("原链接", "商品链接", "链接", "origin_url", "url"),
    "main_image_url": ("主图", "主图url", "主图链接", "main_image_url", "image_url"),
    "status": ("商品状态", "状态", "status"),
    "stock_status": ("库存状态", "stock_status"),
    "sku_code": ("sku编码", "sku", "规格编码", "sku_code", "sku_code_1688"),
    "spec_name": ("规格名", "规格名称", "规格", "spec_name"),
    "spec_value": ("规格值", "spec_value"),
    "sku_cost_price": ("sku成本价", "sku成本", "采购价", "进货价", "sku_cost_price"),
    "sale_price": ("售价", "建议售价", "销售价", "sale_price"),
    "stock_qty": ("库存", "库存数量", "stock_qty", "stock"),
    "remark": ("备注", "remark"),
}
# 必填列：没有它整份 CSV 无法落地
REQUIRED_CSV_COLUMNS: tuple[str, ...] = ("title",)


def normalize_header(name: str) -> str:
    """表头归一化：去 BOM / 空格 / 括号（含全角）并转小写，便于中英文列名混填。"""
    text = str(name or "").replace("\ufeff", "").strip().lower()
    for old, new in ((" ", ""), ("（", "("), ("）", ")"), ("\t", "")):
        text = text.replace(old, new)
    return text


def decode_csv_bytes(data: bytes) -> str:
    """CSV 字节流 → 文本：UTF-8(BOM) → UTF-8 → GBK，最后兜底 latin-1 不抛异常。

    ★ 为什么必须做编码兜底：国内运营用 Excel 导出的 CSV **默认是 GBK**，
      拿 utf-8 直接 decode 会抛 `UnicodeDecodeError`，而报错文案里只有个字节位置，
      使用者根本看不懂 —— 这里直接按最大兼容度吃掉。
    """
    raw = bytes(data or b"")
    for encoding in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", errors="replace")


@dataclass
class _ManualSkuSpec:
    """手工录入的单条 SKU（内部规格，已做类型归一化）。"""

    row: int = 0
    sku_code: str = ""
    spec_json: dict[str, str] = field(default_factory=dict)
    cost_price_cents: int | None = None
    sale_price_cents: int | None = None
    stock_qty: int = 0
    status: str = SourceStatus.ON_SALE.value
    remark: str = ""


@dataclass
class _ManualProductSpec:
    """手工录入的单个商品（含 SKU 列表）。"""

    product_1688_id: str = ""
    product_code: str = ""
    title: str = ""
    category_path: str | None = None
    supplier_id: int | None = None
    cost_price_cents: int | None = None
    origin_url: str | None = None
    main_image_url: str | None = None
    status: str = SourceStatus.ON_SALE.value
    stock_status: str | None = None
    remark: str = ""
    rows: list[int] = field(default_factory=list)
    skus: list[_ManualSkuSpec] = field(default_factory=list)


async def load_source_credentials(session: Any) -> dict[str, str]:
    """★ 从 `credential` 表读取并解密 1688 采集凭证（★ 绝不允许硬编码）。

    坐标：`owner_type=source`、`owner_key=alibaba1688`，
    `credential_key` ∈ {app_key, app_secret, access_token}（与 bootstrap 占位行同源）。

    Returns:
        `{credential_key: 明文}`；未配置 / 是占位值 / 解密失败 的键**不出现在结果里**，
        调用方据此判断缺了哪一项。
    """
    rows = (
        await session.execute(
            select(Credential).where(
                Credential.owner_type == CredentialOwnerType.SOURCE.value,
                Credential.owner_key == SOURCE_1688_OWNER_KEY,
                Credential.status == CredentialStatus.ACTIVE.value,
            )
        )
    ).scalars().all()

    values: dict[str, str] = {}
    for row in rows:
        key = str(row.credential_key or "").strip().lower()
        if key not in SOURCE_1688_CREDENTIAL_KEYS or key in values:
            continue
        try:
            plain = decrypt_credential(row.value_enc).strip()
        except Exception as exc:  # noqa: BLE001  单条解密失败不得带崩整次采集
            logger.warning("source_credential_decrypt_failed", credential_key=key, error=str(exc))
            continue
        if not plain or plain == PLACEHOLDER_CREDENTIAL:
            continue
        values[key] = plain
    return values


def missing_credential_message(missing_keys: Iterable[str]) -> str:
    """未配置凭证时的**可操作**错误提示（使用者照着做就能配好）。"""
    names = [str(key) for key in missing_keys if str(key)]
    return (
        f"未配置 1688 采集凭证（缺少 {'、'.join(names) or '未知项'}）："
        "请在「系统设置 → 凭证」中新增三行凭证——"
        f"owner_type=source、owner_key={SOURCE_1688_OWNER_KEY}，"
        "credential_key 分别为 app_key / app_secret / access_token；"
        "AppKey 与 AppSecret 在 https://open.1688.com 的应用详情页领取，"
        "access_token 由该应用走 OAuth 授权后换取。"
    )


class SourceService:
    """货源采集与查询。"""

    # ==================================================================
    #  供应商
    # ==================================================================

    @staticmethod
    async def list_suppliers(
        session: Any,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Supplier], int]:
        """分页查询供应商。"""
        stmt = select(Supplier).where(Supplier.is_deleted.is_(False))
        if keyword:
            like = f"%{keyword.strip()}%"
            stmt = stmt.where(or_(Supplier.name.like(like), Supplier.supplier_1688_id.like(like)))
        if status:
            stmt = stmt.where(Supplier.status == status)

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await session.execute(count_stmt)).scalar_one() or 0)
        rows = (
            (await session.execute(stmt.order_by(Supplier.id.desc()).offset((page - 1) * page_size).limit(page_size)))
            .scalars()
            .all()
        )
        return list(rows), total

    @staticmethod
    async def create_supplier(session: Any, payload: Any, *, operator: str = "system") -> Supplier:
        """创建供应商（1688 ID 唯一 → 409）。"""
        exists = (
            await session.execute(
                select(Supplier.id).where(Supplier.supplier_1688_id == payload.supplier_1688_id)
            )
        ).scalar_one_or_none()
        if exists is not None:
            raise BusinessError(
                f"供应商 {payload.supplier_1688_id} 已存在", code=ErrorCode.UNIQUE_CONFLICT, http_status=409
            )
        supplier = Supplier(
            supplier_1688_id=payload.supplier_1688_id,
            name=payload.name,
            location=payload.location,
            lead_time_hours=payload.lead_time_hours,
            moq=payload.moq,
            cooperation_score=payload.cooperation_score,
            status=payload.status or "active",
        )
        session.add(supplier)
        await session.flush()
        await AuditService.write(
            session,
            action_type=AuditActionType.MAPPING_CHANGE.value,
            object_type=AuditObjectType.SYSTEM_SETTING.value,
            object_id=supplier.id,
            operator=operator,
            new_value={"action": "create_supplier", "name": supplier.name},
            trace_id=get_trace_id(),
            remark=f"创建供应商 {supplier.name}",
        )
        return supplier

    @staticmethod
    async def update_supplier(
        session: Any, supplier_id: int, payload: Any, *, operator: str = "system"
    ) -> Supplier:
        """更新供应商。"""
        supplier = (
            await session.execute(select(Supplier).where(Supplier.id == int(supplier_id)))
        ).scalars().first()
        if supplier is None:
            raise NotFoundError(f"供应商 {supplier_id} 不存在")
        # ★ 变量名不用 `field`：它与本模块 from dataclasses import field 重名（ruff F402）
        for attribute, value in payload.model_dump(exclude_unset=True, exclude_none=True).items():
            if hasattr(supplier, attribute):
                setattr(supplier, attribute, value)
        await session.flush()
        return supplier

    @staticmethod
    async def delete_supplier(session: Any, supplier_id: int, *, operator: str = "system") -> int:
        """软删除供应商。"""
        supplier = (
            await session.execute(select(Supplier).where(Supplier.id == int(supplier_id)))
        ).scalars().first()
        if supplier is None:
            raise NotFoundError(f"供应商 {supplier_id} 不存在")
        supplier.mark_deleted(operator=operator, reason="手动删除")
        await session.flush()
        return int(supplier.id)

    # ==================================================================
    #  货源商品
    # ==================================================================

    @staticmethod
    async def list_source_products(
        session: Any,
        *,
        keyword: str | None = None,
        product_1688_id: str | None = None,
        supplier_id: int | None = None,
        status: str | None = None,
        collected_from: Any = None,
        collected_to: Any = None,
        source_platform: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[SourceProduct], dict[int, str], dict[int, int], int]:
        """分页查询货源商品。

        Args:
            source_platform: `manual`=只看手工录入 / `alibaba1688`=只看 1688 采集；留空不过滤。
                ★ 表上**没有** source_platform 列（约束：不改表结构），
                  因此用 `product_1688_id` 的 `MANUAL-` 前缀过滤（与 `derive_source_platform` 同源）。

        Returns:
            `(商品列表, {supplier_id: 名称}, {product_id: sku_count}, 总数)`。
        """
        stmt = select(SourceProduct).where(SourceProduct.is_deleted.is_(False))
        if keyword:
            like = f"%{keyword.strip()}%"
            stmt = stmt.where(or_(SourceProduct.title.like(like), SourceProduct.product_1688_id.like(like)))
        if product_1688_id:
            stmt = stmt.where(SourceProduct.product_1688_id == product_1688_id)
        if supplier_id is not None:
            stmt = stmt.where(SourceProduct.supplier_id == int(supplier_id))
        if status:
            stmt = stmt.where(SourceProduct.status == status)
        if source_platform:
            # ★ 与 derive_source_platform 保持一致：手工货源的产品 ID 恒带 `MANUAL-` 前缀
            like = f"{MANUAL_ID_PREFIX}%"
            if str(source_platform).strip().lower() == SOURCE_PLATFORM_MANUAL:
                stmt = stmt.where(SourceProduct.product_1688_id.like(like))
            else:
                stmt = stmt.where(SourceProduct.product_1688_id.not_like(like))
        if collected_from is not None:
            stmt = stmt.where(SourceProduct.collected_at >= collected_from)
        if collected_to is not None:
            stmt = stmt.where(SourceProduct.collected_at <= collected_to)

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await session.execute(count_stmt)).scalar_one() or 0)
        rows = (
            (
                await session.execute(
                    stmt.order_by(SourceProduct.id.desc()).offset((page - 1) * page_size).limit(page_size)
                )
            )
            .scalars()
            .all()
        )

        supplier_ids = sorted({int(p.supplier_id) for p in rows if p.supplier_id is not None})
        supplier_names: dict[int, str] = {}
        if supplier_ids:
            suppliers = (
                (await session.execute(select(Supplier).where(Supplier.id.in_(supplier_ids)))).scalars().all()
            )
            supplier_names = {int(s.id): s.name for s in suppliers}

        product_ids = [int(p.id) for p in rows]
        sku_counts: dict[int, int] = {}
        if product_ids:
            counted = (
                await session.execute(
                    select(SourceSku.source_product_id, func.count(SourceSku.id))
                    .where(SourceSku.source_product_id.in_(product_ids), SourceSku.is_deleted.is_(False))
                    .group_by(SourceSku.source_product_id)
                )
            ).all()
            sku_counts = {int(pid): int(cnt) for pid, cnt in counted}

        return list(rows), supplier_names, sku_counts, total

    @staticmethod
    async def get_source_product(session: Any, product_id: int) -> SourceProduct:
        """取货源商品（404）。"""
        product = (
            await session.execute(
                select(SourceProduct).where(
                    SourceProduct.id == int(product_id), SourceProduct.is_deleted.is_(False)
                )
            )
        ).scalars().first()
        if product is None:
            raise NotFoundError(f"货源商品 {product_id} 不存在", code=ErrorCode.SOURCE_PRODUCT_NOT_FOUND)
        return product

    @staticmethod
    async def list_source_skus(session: Any, product_id: int) -> list[SourceSku]:
        """取货源商品的 SKU 列表。"""
        rows = (
            await session.execute(
                select(SourceSku)
                .where(SourceSku.source_product_id == int(product_id), SourceSku.is_deleted.is_(False))
                .order_by(SourceSku.id)
            )
        ).scalars().all()
        return list(rows)

    @staticmethod
    async def list_assets(session: Any, product_id: int) -> list[Asset]:
        """取货源商品的素材列表。"""
        rows = (
            await session.execute(
                select(Asset)
                .where(Asset.source_product_id == int(product_id), Asset.is_deleted.is_(False))
                .order_by(Asset.id)
            )
        ).scalars().all()
        return list(rows)

    @staticmethod
    async def delete_source_product(session: Any, product_id: int, *, operator: str = "system") -> int:
        """软删除货源商品（连带软删除其 SKU）。"""
        product = await SourceService.get_source_product(session, product_id)
        product.mark_deleted(operator=operator, reason="手动删除")
        skus = await SourceService.list_source_skus(session, product_id)
        for sku in skus:
            sku.mark_deleted(operator=operator, reason="货源商品删除")
        await session.flush()
        return int(product.id)

    # ==================================================================
    #  采集（1688）
    # ==================================================================

    @staticmethod
    async def collect(
        session: Any,
        identifiers: Iterable[str],
        *,
        supplier_id: int | None = None,
        operator: str = "system",
        download_images: bool = True,
    ) -> dict[str, Any]:
        """★ 采集 1688 商品（Adapter 层 `collect()` 永不抛异常，本方法只做落库）。

        Returns:
            `{"accepted": int, "created": int, "updated": int, "failed": list, "product_ids": list}`。
        """
        items = [str(i).strip() for i in (identifiers or []) if str(i).strip()]
        if not items:
            raise BusinessError("identifiers 不能为空", code=ErrorCode.PARAM_ERROR)
        if len(items) > MAX_COLLECT_IDENTIFIERS:
            raise BusinessError(
                f"单次采集上限 {MAX_COLLECT_IDENTIFIERS} 个，当前 {len(items)} 个",
                code=ErrorCode.SOURCE_QUOTA_EXCEEDED,
            )

        # ★ 1688 适配器**不接受 session 参数**（它是只读采集适配器，自己不碰库）；
        #   早期写成 `Alibaba1688Adapter(session=session)` → TypeError，采集 100% 失败。
        # ★ 凭证从 `credential` 表读（owner_type=source / owner_key=alibaba1688），
        #   早年这里写成 `Alibaba1688Adapter()` 不传凭证 ⇒ configured 恒 False ⇒
        #   collect() 恒返回「未配置 AppKey / AccessToken」，整条采集链路是死的。
        credentials = await load_source_credentials(session)
        missing = [key for key in SOURCE_1688_CREDENTIAL_KEYS if not credentials.get(key)]
        if missing:
            reason = missing_credential_message(missing)
            logger.warning("source_collect_no_credentials", missing=missing)
            return {
                "accepted": len(items),
                "created": 0,
                "updated": 0,
                "failed": [{"identifier": item, "reason": reason} for item in items],
                "product_ids": [],
            }
        adapter = Alibaba1688Adapter(
            app_key=str(credentials.get("app_key") or ""),
            app_secret=str(credentials.get("app_secret") or ""),
            access_token=str(credentials.get("access_token") or ""),
        )

        created = 0
        updated = 0
        failed: list[dict[str, str]] = []
        product_ids: list[int] = []

        for identifier in items:
            # ★ 使用者粘贴的是**整条商品链接**（前端 placeholder 就是
            #   https://detail.1688.com/offer/123456.html），必须先解析出 Offer ID，
            #   否则会把整串 URL 当 productID 打给开放平台 ⇒ 必然失败。
            offer_id, parse_error = parse_1688_product_id(identifier)
            if not offer_id:
                failed.append({"identifier": identifier, "reason": parse_error})
                continue
            try:
                # ★ `collect()` 返回的是三元组 `(ok, CollectedProduct|None, message)`，
                #   不是 dict —— 早期按 dict 取字段会 AttributeError。
                ok, collected, message = await adapter.collect(offer_id)
            except Exception as exc:  # noqa: BLE001  适配器已防御；此处再兜一层
                logger.warning("source_collect_failed", identifier=identifier, error=str(exc))
                failed.append({"identifier": identifier, "reason": str(exc)})
                continue
            if not ok or collected is None:
                failed.append({"identifier": identifier, "reason": message or "1688 未返回商品数据"})
                continue

            product_1688_id = str(collected.product_1688_id or offer_id)
            stmt = select(SourceProduct).where(SourceProduct.product_1688_id == product_1688_id)
            product = (await session.execute(stmt)).scalars().first()
            if product is None:
                product = SourceProduct(product_1688_id=product_1688_id)
                session.add(product)
                created += 1
            else:
                updated += 1

            product.title = str(collected.title or product.title or product_1688_id)
            product.category_path = collected.category_path or product.category_path
            product.supplier_id = supplier_id if supplier_id is not None else product.supplier_id
            product.cost_price_cents = int(collected.cost_price_cents or 0) or product.cost_price_cents
            product.origin_url = collected.origin_url or product.origin_url
            product.main_image_url = collected.main_image_url or product.main_image_url
            product.params_json = dict(collected.params_json or {}) or product.params_json
            # ★ `raw_payload_json` 存**完整原始响应**：受「不改表结构、不加列」约束，
            #   详情图 URL 列表不新开字段，全部随原始 payload 保留在这里可追溯
            #   （详情图字段名 PROBE-PENDING，原始响应是事后确认字段名的唯一依据）。
            product.raw_payload_json = dict(collected.raw or {})
            product.status = str(product.status or "on_sale")
            product.collected_at = utc_now()
            await session.flush()
            product_ids.append(int(product.id))

            # ---------- 落 SKU ----------
            # ★ 会话 `autoflush=False`，本次循环里刚 add 的 SKU 不会被后续 SELECT 看到。
            #   1688 响应里常出现「多个 SKU 都没有 skuCode 且规格为空」的情况，
            #   兜底编码会退化成同一个 `{商品ID}-DEFAULT`；不去重就撞
            #   `uq_source_sku_product_code` ⇒ IntegrityError 把整个采集任务打挂。
            seen_sku_codes: set[str] = set()
            for sku_collected in list(collected.skus or []):
                sku_code = str(sku_collected.sku_code_1688 or "").strip()
                if not sku_code:
                    continue
                if sku_code in seen_sku_codes:
                    logger.warning(
                        "source_sku_duplicate_skipped",
                        product_1688_id=product_1688_id,
                        sku_code=sku_code,
                        reason="同一响应内 SKU 编码重复（1688 未返回 skuCode 且规格为空）",
                    )
                    continue
                sku_stmt = select(SourceSku).where(
                    SourceSku.source_product_id == product.id, SourceSku.sku_code_1688 == sku_code
                )
                sku = (await session.execute(sku_stmt)).scalars().first()
                if sku is None:
                    sku = SourceSku(source_product_id=product.id, sku_code_1688=sku_code)
                    session.add(sku)
                seen_sku_codes.add(sku_code)
                sku.spec_json = dict(sku_collected.spec_json or {})
                sku.spec_signature = str(sku_collected.spec_signature or "")
                sku.cost_price_cents = int(sku_collected.cost_price_cents or 0) or None
                sku.stock_qty = int(sku_collected.stock_qty or 0)
                sku.status = str(sku_collected.status or "on_sale")
                sku.last_checked_at = utc_now()
            await session.flush()

            # ---------- 落原始素材（可选下载图片）----------
            if download_images:
                # ★ 主图 1 张 + 详情图若干，各自带 asset_type，避免全部打成 DETAIL_IMAGE
                pending: list[tuple[str, str]] = []
                if collected.main_image_url:
                    pending.append((collected.main_image_url, AssetType.MAIN_IMAGE.value))
                for detail_url in list(collected.detail_image_urls or []):
                    if detail_url and detail_url != collected.main_image_url:
                        pending.append((detail_url, AssetType.DETAIL_IMAGE.value))
                await SourceService._download_assets(session, adapter=adapter, product=product, items=pending)

        await AuditService.write(
            session,
            action_type=AuditActionType.MAPPING_CHANGE.value,
            object_type=AuditObjectType.SYSTEM_SETTING.value,
            object_id="source_collect",
            operator=operator,
            new_value={"identifiers": items, "created": created, "updated": updated},
            trace_id=get_trace_id(),
            remark=f"采集 1688 商品 {len(items)} 个",
        )
        await session.flush()
        logger.info("source_collect_done", created=created, updated=updated, failed=len(failed))
        return {
            "accepted": len(items),
            "created": created,
            "updated": updated,
            "failed": failed,
            "product_ids": product_ids,
        }

    @staticmethod
    async def _download_assets(
        session: Any,
        *,
        adapter: Alibaba1688Adapter,
        product: SourceProduct,
        items: Iterable[tuple[str, str]],
    ) -> int:
        """下载原始图片并落 `asset`（去重：按 `content_hash` 唯一索引）。

        Args:
            items: `[(图片 URL, AssetType 值)]`，按传入顺序编号（第 0 张即主图）。

        ★ `download_image()` 现在返回 `(ok, bytes, message)`：**只负责取字节**，
          写盘与算 hash 都在本方法里做，杜绝「适配器写一遍、调用方把三元组当字节再写一遍」
          的历史 bug（`content_hash_bytes(tuple)` → TypeError）。
        """
        saved = 0
        assets_dir = get_settings().assets_dir / "raw" / str(product.product_1688_id)
        assets_dir.mkdir(parents=True, exist_ok=True)
        # ★ 会话是 `autoflush=False`（见 core/database.py），**本次循环里刚 add 的行不会被 SELECT 看到**。
        #   同一个商品的详情图里出现完全相同的字节是很常见的（商家重复贴图），
        #   只看数据库会让第二次 INSERT 直接撞 `uq_asset_content_hash` ⇒ IntegrityError，
        #   把整个采集任务打挂。因此必须再在本次批处理内去重。
        batch_hashes: set[str] = set()

        for index, (url, asset_type) in enumerate(list(items)[:MAX_DOWNLOAD_IMAGES]):
            url_str = str(url or "").strip()
            if not url_str.startswith(("http://", "https://")):
                continue
            try:
                ok, data, message = await adapter.download_image(url_str)
            except Exception as exc:  # noqa: BLE001
                logger.warning("source_image_download_failed", url=url_str, error=str(exc))
                continue
            if not ok or not data:
                # ★ `data` 是 bytes：空字节串是假值、非空字节串是真值，这个判断有效。
                #   早年 `data` 是三元组 `(bool, str, str)` 时**恒为真** ⇒ 判断形同虚设。
                logger.warning("source_image_download_failed", url=url_str, error=message or "空内容")
                continue

            digest = content_hash_bytes(data)
            if digest in batch_hashes:
                logger.info("source_image_duplicate_skipped", url=url_str, reason="本次采集已落过相同内容")
                continue
            exists = (await session.execute(select(Asset.id).where(Asset.content_hash == digest))).scalar_one_or_none()
            if exists is not None:
                continue
            batch_hashes.add(digest)

            is_main = asset_type == AssetType.MAIN_IMAGE.value
            filename = f"{'main' if is_main else 'detail'}_{index:02d}.jpg"
            target = assets_dir / filename
            target.write_bytes(data)
            session.add(
                Asset(
                    source_product_id=product.id,
                    asset_type=asset_type,
                    origin=AssetOrigin.RAW.value,
                    storage_path=str(target),
                    origin_url=url_str,
                    content_hash=digest,
                    version=1,
                    lineage_id=f"raw-{product.product_1688_id}-{index}",
                    is_current=True,
                    size_bytes=len(data),
                )
            )
            saved += 1
        await session.flush()
        return saved

    # ==================================================================
    #  ★ 手工录入 / CSV 导入（★ 真实主路径，全程不碰 1688 适配器）
    # ==================================================================

    @staticmethod
    async def create_manual(
        session: Any,
        payload: SourceProductManualCreate,
        *,
        operator: str = "system",
    ) -> dict[str, Any]:
        """★ 手工录入单个货源商品（连带 SKU）。

        ★ 为什么这条路径必须存在：`collect()` 依赖 1688 开放平台凭证
          （AppKey / AccessToken），个体户 / 个人身份证店**基本拿不到**。
          没有手工入口 = 系统里一条货源都进不来 = 下游全链路无从起步。

        Returns:
            `{"product": SourceProduct, "skus": list[SourceSku], "created": bool,
              "created_skus": int, "updated_skus": int}`。

        Raises:
            BusinessError: 1001 / 400，详情里带逐条 `failed`（可读中文原因）。
        """
        spec, failures = SourceService._build_spec_from_payload(payload, row=1)
        await SourceService._assign_product_id(session, spec)
        failures.extend(await SourceService._validate_spec(session, spec))
        if failures:
            raise BusinessError(
                failures[0].reason,
                code=ErrorCode.PARAM_ERROR,
                detail={"failed": [item.model_dump() for item in failures]},
            )

        outcome = await SourceService._upsert_manual_product(session, spec=spec, operator=operator)
        skus = await SourceService.list_source_skus(session, int(outcome["product"].id))
        return {
            "product": outcome["product"],
            "skus": skus,
            "created": bool(outcome["created"]),
            "created_skus": int(outcome["created_skus"]),
            "updated_skus": int(outcome["updated_skus"]),
        }

    @staticmethod
    async def import_csv(
        session: Any,
        content: bytes | str,
        *,
        operator: str = "system",
    ) -> SourceCsvImportResult:
        """★ CSV 批量导入货源商品。

        ★ 失败**逐行**返回可读中文原因（`failed[]`），绝不静默吞掉任何一行：
          运营拿着这份结果就能知道哪行要改；改成 success 却一行没写才是事故。

        Returns:
            `SourceCsvImportResult`：
                total        非空数据行数
                created      新建的货源商品数
                updated      命中的既有商品数（按 `MANUAL-<商品编码>` 幂等）
                created_skus 新建的 SKU 数
                updated_skus 更新的 SKU 数
                failed       `[{row, identifier, reason}]`

        Raises:
            BusinessError: 1001 / 400 —— 整份文件不可解析时的**前置校验**
                （空文件 / 缺必填列 / 超过行数上限），此时一行都没写。
        """
        raw = content if isinstance(content, (bytes, bytearray)) else str(content or "").encode("utf-8")
        raw = bytes(raw)
        if not raw.strip():
            raise BusinessError("CSV 内容为空，请先按模板填写", code=ErrorCode.PARAM_ERROR)
        if len(raw) > MAX_IMPORT_BYTES:
            raise BusinessError(
                f"CSV 文件超过 {MAX_IMPORT_BYTES // (1024 * 1024)}MB，请拆分后再导入",
                code=ErrorCode.PARAM_ERROR,
            )
        text = decode_csv_bytes(raw)

        reader = csv.DictReader(io.StringIO(text))
        headers = [str(h or "") for h in (reader.fieldnames or [])]
        column_map = SourceService._build_column_map(headers)
        missing = [key for key in REQUIRED_CSV_COLUMNS if key not in column_map]
        if missing:
            raise BusinessError(
                "CSV 缺少必填列『商品标题』（也接受列名 title / product_title）",
                code=ErrorCode.PARAM_ERROR,
                detail={"headers": headers, "missing": missing, "template": "scripts/source_products_template.csv"},
            )

        rows = list(reader)
        if not rows:
            raise BusinessError(
                "CSV 只有表头没有数据行（请照 scripts/source_products_template.csv 至少填一行商品）",
                code=ErrorCode.PARAM_ERROR,
            )
        if len(rows) > MAX_IMPORT_ROWS:
            raise BusinessError(
                f"单次导入上限 {MAX_IMPORT_ROWS} 行，当前 {len(rows)} 行，请拆分后多次导入",
                code=ErrorCode.PARAM_ERROR,
            )

        specs, failures, data_row_count = SourceService._parse_csv_rows(rows, column_map)
        result = SourceCsvImportResult(total=data_row_count, failed=list(failures))

        for spec in specs:
            spec_failures = await SourceService._validate_spec(session, spec)
            if spec_failures:
                result.failed.extend(spec_failures)
                continue
            outcome = await SourceService._upsert_manual_product(session, spec=spec, operator=operator)
            if bool(outcome["created"]):
                result.created += 1
            else:
                result.updated += 1
            result.created_skus += int(outcome["created_skus"])
            result.updated_skus += int(outcome["updated_skus"])

        # ★ 失败明细按行号排序，运营从上往下照着改即可
        result.failed.sort(key=lambda item: item.row)
        logger.info(
            "source_import_csv_done",
            total=result.total,
            created=result.created,
            updated=result.updated,
            created_skus=result.created_skus,
            failed=len(result.failed),
        )
        return result

    @staticmethod
    def suggested_sale_prices(product: Any) -> dict[str, int]:
        """取录入时填的建议售价 `{货源 SKU 编码: 分}`。

        ★ 受「不改表结构」约束，建议售价暂存在 `source_product.params_json['sku_price_hints']`，
          上架回填时可直接展示给运营参考（真正的成交售价由 `listing_sku.sale_price_cents` 承载）。
        """
        hints = dict((getattr(product, "params_json", None) or {}).get(SKU_PRICE_HINT_KEY) or {})
        return {str(code): int(cents) for code, cents in hints.items() if str(code)}

    # ---------- 手工录入的构造 / 校验 ----------

    @staticmethod
    def _build_spec_from_payload(
        payload: SourceProductManualCreate, row: int
    ) -> tuple[_ManualProductSpec, list[SourceImportFailure]]:
        """把 API 请求体转成内部商品规格（只做结构转换与字段解析，落库前的业务校验在 `_validate_spec`）。"""
        failures: list[SourceImportFailure] = []
        product_cost, error = SourceService._to_money(payload.cost_price, label="商品成本价")
        if error:
            failures.append(SourceService._failure(row, payload.product_code or "", error))

        skus: list[_ManualSkuSpec] = []
        for index, item in enumerate(payload.skus or []):
            sku_spec, sku_error = SourceService._build_sku_spec(
                item, row=row, fallback_cost_cents=product_cost
            )
            if sku_error:
                failures.append(
                    SourceService._failure(
                        row, item.sku_code or item.spec_value or f"第{index + 1}个 SKU", sku_error
                    )
                )
                continue
            skus.append(sku_spec)

        spec = _ManualProductSpec(
            product_1688_id="",
            product_code=str(payload.product_code or "").strip(),
            title=str(payload.title or "").strip(),
            category_path=(str(payload.category_path).strip() if payload.category_path else None),
            supplier_id=payload.supplier_id,
            cost_price_cents=product_cost,
            origin_url=(str(payload.origin_url).strip() if payload.origin_url else None),
            main_image_url=(str(payload.main_image_url).strip() if payload.main_image_url else None),
            status=str(payload.status or SourceStatus.ON_SALE.value),
            stock_status=(str(payload.stock_status).strip() if payload.stock_status else None),
            rows=[row],
            skus=skus,
        )
        return spec, failures

    @staticmethod
    def _build_sku_spec(
        item: Any,
        *,
        row: int,
        fallback_cost_cents: int | None,
    ) -> tuple[_ManualSkuSpec, str | None]:
        """单个 SKU 入参 → 内部 SKU 规格；返回 `(规格, 错误原因?)`。"""
        spec_json, spec_error = SourceService._parse_spec_pair(item.spec_name, item.spec_value)
        if spec_error:
            return _ManualSkuSpec(row=row), spec_error

        cost_cents, cost_error = SourceService._to_money(item.cost_price, label="SKU 成本价")
        if cost_error:
            return _ManualSkuSpec(row=row), cost_error
        if cost_cents is None:
            # ★ SKU 没填成本时回落商品级成本，避免下游因 cost_invalid 直接卡死
            cost_cents = fallback_cost_cents

        sale_cents, sale_error = SourceService._to_money(item.sale_price, label="售价")
        if sale_error:
            return _ManualSkuSpec(row=row), sale_error

        stock_qty, stock_error = SourceService._to_int(item.stock_qty, label="库存数量")
        if stock_error:
            return _ManualSkuSpec(row=row), stock_error

        return (
            _ManualSkuSpec(
                row=row,
                sku_code=str(item.sku_code or "").strip(),
                spec_json=spec_json,
                cost_price_cents=cost_cents,
                sale_price_cents=sale_cents,
                stock_qty=int(stock_qty or 0),
                status=str(item.status or SourceStatus.ON_SALE.value),
            ),
            None,
        )

    @staticmethod
    def _parse_spec_pair(name: Any, value: Any) -> tuple[dict[str, str], str | None]:
        """解析「规格名 / 规格值」→ `{"颜色": "红", "尺码": "XL"}`。

        支持多组规格：`"颜色;尺码"` + `"红;XL"`（中英文分号均可）。
        """
        raw_name = str(name or "").replace("；", ";").strip()
        raw_value = str(value or "").replace("；", ";").strip()
        if not raw_name:
            return {}, None
        names = [part.strip() for part in raw_name.split(";")]
        values = [part.strip() for part in raw_value.split(";")]
        if len(names) != len(values):
            return {}, (
                f"规格名「{raw_name}」与规格值「{raw_value}」数量不一致"
                "（多组规格请用分号分隔，且两边数量相同）"
            )
        if not all(names):
            return {}, "规格名不能为空"
        if not all(values):
            return {}, "规格值不能为空（每一组规格都要填值）"
        return dict(zip(names, values)), None

    @staticmethod
    def _to_money(value: Any, *, label: str) -> tuple[int | None, str | None]:
        """金额入参 → 分；空值返回 `(None, None)`（表示"未填"），非法值返回可读原因。"""
        if value is None:
            return None, None
        text = str(value).strip()
        if not text:
            return None, None
        try:
            cents = int(parse_money(text, field_name=label))
        except (TypeError, ValueError):
            return None, f"{label}「{text}」不是合法金额（请填数字，如 29.90）"
        if cents < 0:
            return None, f"{label}不能为负数：{text}"
        return cents, None

    @staticmethod
    def _to_int(value: Any, *, label: str) -> tuple[int | None, str | None]:
        """整数字段 → int（空值视为 0）。"""
        if value is None or str(value).strip() == "":
            return 0, None
        try:
            return int(float(str(value).strip())), None
        except (TypeError, ValueError):
            return None, f"{label}「{value}」不是整数"

    @staticmethod
    def _failure(row: int, identifier: str, reason: str) -> SourceImportFailure:
        """构造单行失败明细（原因里带行号，运营直接照着改）。"""
        return SourceImportFailure(
            row=int(row),
            identifier=str(identifier or ""),
            reason=f"第{int(row)}行：{reason}" if int(row) > 0 else reason,
        )

    @staticmethod
    async def _assign_product_id(session: Any, spec: _ManualProductSpec) -> None:
        """确定 `product_1688_id`：填了编码 ⇒ `MANUAL-<编码>`（幂等）；没填 ⇒ 自动生成并保证库内唯一。"""
        base = manual_source_id(spec.product_code) if spec.product_code else ""
        if base and base != MANUAL_ID_PREFIX:
            spec.product_1688_id = base
            return

        base = f"{MANUAL_ID_PREFIX}{utc_now().strftime('%Y%m%d%H%M%S')}"
        candidate = f"{base}-{uuid.uuid4().hex[:6]}"
        for _ in range(5):
            exists = (
                await session.execute(
                    select(SourceProduct.id).where(SourceProduct.product_1688_id == candidate)
                )
            ).scalar_one_or_none()
            if exists is None:
                spec.product_1688_id = candidate
                return
            candidate = f"{base}-{uuid.uuid4().hex[:6]}"
        spec.product_1688_id = candidate

    @staticmethod
    async def _validate_spec(session: Any, spec: _ManualProductSpec) -> list[SourceImportFailure]:
        """校验单个商品规格（商品级错误会让该商品的所有行一起失败）。"""
        identifier = spec.product_code or spec.title or spec.product_1688_id
        rows = spec.rows or [0]
        product_errors: list[str] = []

        if not spec.title:
            product_errors.append("商品标题不能为空")
        if spec.supplier_id is not None:
            supplier = (
                await session.execute(
                    select(Supplier.id).where(
                        Supplier.id == int(spec.supplier_id), Supplier.is_deleted.is_(False)
                    )
                )
            ).scalar_one_or_none()
            if supplier is None:
                product_errors.append(f"供应商 ID {spec.supplier_id} 不存在（请先在「供应商」中创建）")
        if spec.origin_url and not str(spec.origin_url).lower().startswith(("http://", "https://")):
            product_errors.append(f"原链接必须以 http:// 或 https:// 开头：{spec.origin_url}")
        if spec.main_image_url and not str(spec.main_image_url).lower().startswith(("http://", "https://")):
            product_errors.append(f"主图必须以 http:// 或 https:// 开头：{spec.main_image_url}")
        if spec.status not in SourceStatus.values():
            product_errors.append(f"商品状态非法：{spec.status}（可选 {'/'.join(SourceStatus.values())}）")

        product_id = str(spec.product_1688_id or "")
        if not product_id:
            product_errors.append("商品编码缺失（既没填商品编码，也无法自动生成）")
        elif len(product_id) > MAX_PRODUCT_ID_LEN:
            product_errors.append(
                f"商品编码过长：{len(product_id)} 字符，上限 {MAX_PRODUCT_ID_LEN}"
                f"（其中 MANUAL- 前缀占 {len(MANUAL_ID_PREFIX)} 字符）"
            )
        if not spec.skus:
            # ★ 无 SKU 的货源 = 建不了映射、上不了架的死路，录入阶段就拦住
            product_errors.append("至少需要一个 SKU（请填『规格名 / 规格值』或直接填『SKU编码』）")

        if product_errors:
            reason = "；".join(product_errors)
            return [SourceService._failure(row, identifier, reason) for row in rows]

        # ---------- 行级校验 ----------
        SourceService._resolve_sku_codes(spec)
        failures: list[SourceImportFailure] = []
        seen: dict[str, int] = {}
        for sku in spec.skus:
            duplicate_of = seen.get(sku.sku_code)
            if duplicate_of is not None:
                failures.append(
                    SourceService._failure(
                        sku.row,
                        sku.sku_code,
                        f"SKU 编码「{sku.sku_code}」在本商品内重复（第{duplicate_of}行已使用，请改用不同编码）",
                    )
                )
                continue
            seen[sku.sku_code] = sku.row

            if len(sku.sku_code) > MAX_SKU_CODE_LEN:
                failures.append(
                    SourceService._failure(
                        sku.row, sku.sku_code, f"SKU 编码超过 {MAX_SKU_CODE_LEN} 字符：{sku.sku_code}"
                    )
                )
                continue
            if sku.status not in SourceStatus.values():
                failures.append(
                    SourceService._failure(
                        sku.row, sku.sku_code, f"SKU 状态非法：{sku.status}（可选 {'/'.join(SourceStatus.values())}）"
                    )
                )
        return failures

    @staticmethod
    def _resolve_sku_codes(spec: _ManualProductSpec) -> None:
        """为未填编码的 SKU 生成稳定编码（含同一商品内的重名兜底）。

        编码规则：`MANUAL-<商品编码>-<规格指纹前 12 位>`，无规格时 `-DEFAULT`(-N)。
        ★ 编码由规格指纹派生 ⇒ 同一行反复导入落在同一个 SKU 上（幂等），
          而不是每次都新造一条。
        """
        prefix = str(spec.product_1688_id or MANUAL_ID_PREFIX)
        used: set[str] = set(str(sku.sku_code or "").strip() for sku in spec.skus if sku.sku_code)
        for sku in spec.skus:
            if str(sku.sku_code or "").strip():
                sku.sku_code = str(sku.sku_code).strip()
                continue
            signature = spec_signature(sku.spec_json)
            base = f"{prefix}-{signature[:12].upper()}" if signature else f"{prefix}-DEFAULT"
            candidate = base
            sequence = 2
            while candidate in used:
                candidate = f"{base}-{sequence:02d}"
                sequence += 1
            used.add(candidate)
            sku.sku_code = candidate

    @staticmethod
    async def _upsert_manual_product(
        session: Any, *, spec: _ManualProductSpec, operator: str = "system"
    ) -> dict[str, Any]:
        """按 `product_1688_id` 幂等落商品与 SKU。

        Returns:
            `{"product": SourceProduct, "created": bool, "created_skus": int, "updated_skus": int}`。
        """
        stmt = select(SourceProduct).where(SourceProduct.product_1688_id == spec.product_1688_id)
        product = (await session.execute(stmt)).scalars().first()
        created = product is None
        if created:
            product = SourceProduct(product_1688_id=spec.product_1688_id)
            session.add(product)
        elif bool(product.is_deleted):
            product.restore()  # 重新导入同一编码 ⇒ 视为复活（否则运营删了就再也导不进来）

        product.title = spec.title or product.title
        product.category_path = spec.category_path if spec.category_path is not None else product.category_path
        product.supplier_id = spec.supplier_id if spec.supplier_id is not None else product.supplier_id
        product.cost_price_cents = (
            spec.cost_price_cents if spec.cost_price_cents is not None else product.cost_price_cents
        )
        product.origin_url = spec.origin_url if spec.origin_url is not None else product.origin_url
        product.main_image_url = spec.main_image_url if spec.main_image_url is not None else product.main_image_url
        product.status = spec.status
        product.stock_status = spec.stock_status if spec.stock_status is not None else product.stock_status

        # ★ 来源标识写在 params_json（受约不能加列），并与 `MANUAL-` 前缀互为印证
        params = dict(product.params_json or {})
        params[SOURCE_PLATFORM_KEY] = SOURCE_PLATFORM_MANUAL
        hints = dict(params.get(SKU_PRICE_HINT_KEY) or {})
        for sku in spec.skus:
            if sku.sale_price_cents is not None:
                hints[str(sku.sku_code)] = int(sku.sale_price_cents)
        if hints:
            params[SKU_PRICE_HINT_KEY] = hints
        product.params_json = params
        product.raw_payload_json = {
            "source_platform": SOURCE_PLATFORM_MANUAL,
            "imported_at": iso_utc(utc_now()),
            "rows": [int(row) for row in spec.rows],
            "skus": [
                {
                    "sku_code": sku.sku_code,
                    "spec_json": dict(sku.spec_json or {}),
                    "cost_price_cents": sku.cost_price_cents,
                    "sale_price_cents": sku.sale_price_cents,
                    "stock_qty": int(sku.stock_qty or 0),
                }
                for sku in spec.skus
            ],
        }
        product.collected_at = utc_now()
        await session.flush()

        created_skus = 0
        updated_skus = 0
        for sku in spec.skus:
            row = (
                await session.execute(
                    select(SourceSku).where(
                        SourceSku.source_product_id == int(product.id),
                        SourceSku.sku_code_1688 == sku.sku_code,
                    )
                )
            ).scalars().first()
            if row is None:
                row = SourceSku(source_product_id=int(product.id), sku_code_1688=sku.sku_code)
                session.add(row)
                created_skus += 1
            else:
                # ★ 唯一索引覆盖软删行 ⇒ 必须复活，否则同一编码再导入会撞键
                if bool(row.is_deleted):
                    row.restore()
                updated_skus += 1
            if sku.spec_json or not row.spec_json:
                row.spec_json = dict(sku.spec_json or {})
                row.spec_signature = spec_signature(row.spec_json)
            if sku.cost_price_cents is not None:
                row.cost_price_cents = int(sku.cost_price_cents)
            if sku.stock_qty is not None:
                row.stock_qty = int(sku.stock_qty or 0)
            row.status = sku.status or row.status or SourceStatus.ON_SALE.value
            row.last_checked_at = utc_now()
        await session.flush()

        await AuditService.write(
            session,
            action_type=AuditActionType.MAPPING_CHANGE.value,
            object_type=AuditObjectType.SYSTEM_SETTING.value,
            object_id=f"source_manual:{product.product_1688_id}",
            operator=operator,
            new_value={
                "action": "manual_import" if created else "manual_update",
                "product_1688_id": product.product_1688_id,
                "sku_count": len(spec.skus),
            },
            trace_id=get_trace_id(),
            remark=f"手工录入货源商品 {product.product_1688_id}（{len(spec.skus)} 个 SKU）",
        )
        await session.flush()
        return {
            "product": product,
            "created": created,
            "created_skus": created_skus,
            "updated_skus": updated_skus,
        }

    # ---------- CSV 解析 ----------

    @staticmethod
    def _build_column_map(headers: list[str]) -> dict[str, str]:
        """表头 → 标准字段名的映射（中英文混填均支持，未知列忽略）。"""
        normalized = {normalize_header(h): h for h in headers if str(h).strip()}
        column_map: dict[str, str] = {}
        for key, aliases in CSV_COLUMN_ALIASES.items():
            for alias in aliases:
                hit = normalized.get(normalize_header(alias))
                if hit is not None:
                    column_map[key] = hit
                    break
        return column_map

    @staticmethod
    def _cell(row: dict[str, Any], key: str, column_map: dict[str, str]) -> str:
        """按标准字段名取单元格文本（列名重复时取第一个非空）。"""
        header = column_map.get(key)
        if not header:
            return ""
        value = row.get(header)
        if isinstance(value, list):
            value = next((v for v in value if str(v or "").strip()), "")
        return "" if value is None else str(value).strip()

    @staticmethod
    def _parse_csv_rows(
        rows: list[dict[str, Any]], column_map: dict[str, str]
    ) -> tuple[list[_ManualProductSpec], list[SourceImportFailure], int]:
        """CSV 行 → 商品规格分组。

        ★ 分组规则（与运营在 Excel 里的填法一致）：
            * 填了『商品编码』的行归入该编码对应的商品；
            * 编码为空的行归入**上一个商品**（Excel 合并单元格的常见填法）；
            * 首行就没有编码 ⇒ 该行独立成商品（自动生成编码）。

        Returns:
            `(商品规格列表, 行级失败明细, 非空数据行数)`。
        """
        specs: dict[str, _ManualProductSpec] = {}
        order: list[str] = []
        failures: list[SourceImportFailure] = []
        data_row_count = 0
        last_key: str | None = None
        stamp = utc_now().strftime("%Y%m%d%H%M%S")

        for offset, raw_row in enumerate(rows, start=2):  # 第 1 行是表头
            values = {key: SourceService._cell(raw_row, key, column_map) for key in CSV_COLUMN_ALIASES}
            if not any(str(v).strip() for v in values.values()):
                continue  # Excel 导出常见的尾部空行：不算数据行，也不算失败
            data_row_count += 1

            code = values.get("product_code") or ""
            title = values.get("title") or ""
            if code:
                key = manual_source_id(code)
                last_key = key
            elif last_key:
                key = last_key  # 编码留空 ⇒ 归入上一个商品的多规格 SKU 行
            else:
                key = f"{MANUAL_ID_PREFIX}{stamp}-{offset:04d}"
                last_key = key

            cost_cents, cost_error = SourceService._to_money(values.get("cost_price"), label="商品成本价")
            if cost_error:
                failures.append(SourceService._failure(offset, code or title, cost_error))
                cost_cents = None

            if key not in specs:
                specs[key] = _ManualProductSpec(
                    product_1688_id=key,
                    product_code=code,
                    title=title,
                    category_path=values.get("category_path") or None,
                    supplier_id=None,
                    origin_url=values.get("origin_url") or None,
                    main_image_url=values.get("main_image_url") or None,
                    status=values.get("status") or SourceStatus.ON_SALE.value,
                    stock_status=values.get("stock_status") or None,
                    cost_price_cents=cost_cents,
                    rows=[],
                    skus=[],
                )
                order.append(key)
            else:
                # 同组后续行补齐缺的商品级字段（但标题以第一次为准，避免同组内互相打架）
                spec = specs[key]
                if not spec.title and title:
                    spec.title = title
                if spec.category_path is None and values.get("category_path"):
                    spec.category_path = values["category_path"]
                if spec.origin_url is None and values.get("origin_url"):
                    spec.origin_url = values["origin_url"]
                if spec.main_image_url is None and values.get("main_image_url"):
                    spec.main_image_url = values["main_image_url"]
                if spec.cost_price_cents is None and cost_cents is not None:
                    spec.cost_price_cents = cost_cents

            spec = specs[key]
            spec.rows.append(offset)

            supplier_raw = values.get("supplier_id") or ""
            if supplier_raw:
                supplier_id, supplier_error = SourceService._to_int(supplier_raw, label="供应商ID")
                if supplier_error:
                    failures.append(SourceService._failure(offset, code or title, supplier_error))
                elif spec.supplier_id is None:
                    spec.supplier_id = int(supplier_id or 0)

            sku_spec, sku_error = SourceService._build_csv_sku(
                values, row=offset, fallback_cost_cents=spec.cost_price_cents
            )
            if sku_error:
                failures.append(
                    SourceService._failure(offset, values.get("sku_code") or code or title, sku_error)
                )
                continue
            spec.skus.append(sku_spec)

        return [specs[key] for key in order], failures, data_row_count

    @staticmethod
    def _build_csv_sku(
        values: dict[str, str], *, row: int, fallback_cost_cents: int | None
    ) -> tuple[_ManualSkuSpec, str | None]:
        """CSV 行 → SKU 规格；返回 `(规格, 错误原因?)`。"""
        spec_json, spec_error = SourceService._parse_spec_pair(
            values.get("spec_name"), values.get("spec_value")
        )
        if spec_error:
            return _ManualSkuSpec(row=row), spec_error

        cost_cents, cost_error = SourceService._to_money(values.get("sku_cost_price"), label="SKU 成本价")
        if cost_error:
            return _ManualSkuSpec(row=row), cost_error
        if cost_cents is None:
            cost_cents = fallback_cost_cents

        sale_cents, sale_error = SourceService._to_money(values.get("sale_price"), label="售价")
        if sale_error:
            return _ManualSkuSpec(row=row), sale_error

        stock_qty, stock_error = SourceService._to_int(values.get("stock_qty"), label="库存数量")
        if stock_error:
            return _ManualSkuSpec(row=row), stock_error

        return (
            _ManualSkuSpec(
                row=row,
                sku_code=values.get("sku_code") or "",
                spec_json=spec_json,
                cost_price_cents=cost_cents,
                sale_price_cents=sale_cents,
                stock_qty=int(stock_qty or 0),
                status=values.get("status") or SourceStatus.ON_SALE.value,
                remark=values.get("remark") or "",
            ),
            None,
        )

    @staticmethod
    def collect_summary(iso_time: str | None = None) -> dict[str, Any]:
        """采集结果摘要（任务回调用）。"""
        return {"collected_at": iso_time or iso_utc(utc_now())}
