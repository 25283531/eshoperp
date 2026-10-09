"""ManualListingAdapter —— 半自动上架（★ 用户决策 ①：个体户 / 个人身份证店 → **主路径**）。

================================================================================
★ ★ ★ 定位变更（用户决策 ①，2026-10-08）★ ★ ★
================================================================================
用户确认店铺主体是**个体户 / 个人身份证店**，大概率拿不到平台开放平台资质。
因此半自动模式**不再是"拿不到资质时的兜底"，而是 MVP 的主路径**，必须完整可用：

    1. 生成素材包 ZIP（图片 + `form_data.json` + `README.txt`）到 `data/packages/`；
    2. 提供可直接复制到平台后台的预填表单数据（标题 / 卖点 / 属性 / 价格 / 库存）；
    3. 人工在平台发布后回填 `shop_item_id`：
       - **售价 `sale_price` 必填**（PRD LST-P0-07）：缺失或 ≤ 0 → 422 拒绝提交；
       - 商品 ID 唯一性校验（重复则 4006）；
    4. 回填成功后自动建立 SKU 映射（由 PublishService 调用 MappingService）。

★ 为什么售价必须必填（这是"必填"，不是"建议填"）：
    `cost_underwater`（成本倒挂）检测依赖 `listing_sku.sale_price_cents`。
    售价为空的商品会被 `sale_price_cents > 0` 过滤掉 —— **表面看冲突面板干干净净，
    实际是这些商品永久失去了倒挂检测能力**，属于静默失效。
    运营手上本来就有这个价格（半自动流程里他就是按这个价填的平台表单），
    必填不增加工作量，只是把已有的数字填回系统。

★ 平台真实适配器（taobao / douyin / pdd）保持 skeleton + TODO 不变：
    它们 `available() == False`，工厂会自动降级。半自动是主路径，不是"等资质下来再换"。
================================================================================
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import select

from app.adapters.listing.base import (
    ListingAdapter,
    ListingPayload,
    ListingPublishResult,
    ListingStatus,
    ManualPackage,
    normalize_platform,
)
from app.adapters.fulfillment.manifest import AdapterResult, HealthStatus
from app.core.config import get_settings
from app.core.errors import BusinessError, ErrorCode
from app.core.logging import get_logger
from app.models.enums import ListingMode, Platform
from app.models.listing import ListingProduct
from app.utils.kit import iso_utc, json_dumps, make_zip_package, safe_filename, utc_now

logger = get_logger(__name__)

__all__ = [
    "ManualListingAdapter",
    "MANUAL_INSTRUCTIONS_TEMPLATE",
    "SALE_PRICE_REQUIRED_MESSAGE",
    "validate_fill_back_skus",
]

# ★ PRD LST-P0-07 / ARCH v1.5：半自动回填时售价必填
SALE_PRICE_REQUIRED_MESSAGE = (
    "每个 SKU 的 sale_price（售价，单位：元）为必填项且必须大于 0。"
    "缺少售价的商品无法参与成本倒挂检测，相当于永久失去该保护。"
)


def validate_fill_back_skus(skus: Sequence[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """★ 校验半自动回填的 SKU 列表：售价必填且 > 0（LST-P0-07）。

    Args:
        skus: 回填请求中的 `skus` 列表，元素含 `shop_sku_code` / `sale_price` 等字段。

    Returns:
        规范化后的 SKU 列表（补充 `sale_price_cents` 整数分）。

    Raises:
        BusinessError: 422 / 1001 —— 售价缺失或 ≤ 0，**拒绝提交**（不是警告）。
    """
    if not skus:
        # 422（业务校验不通过），code=1001 参数错误
        raise BusinessError(
            f"{SALE_PRICE_REQUIRED_MESSAGE}（且 skus 不能为空）",
            code=ErrorCode.PARAM_ERROR,
            http_status=422,
        )

    normalized: list[dict[str, Any]] = []
    missing: list[str] = []
    for index, raw in enumerate(skus):
        if not isinstance(raw, dict):
            raise BusinessError(
                f"skus[{index}] 必须是对象", code=ErrorCode.PARAM_ERROR, http_status=422
            )
        sku_code = str(raw.get("shop_sku_code") or raw.get("shop_sku_name") or f"#{index + 1}").strip()
        raw_price = raw.get("sale_price")
        cents: int | None = None
        if isinstance(raw_price, (int, float)) and not isinstance(raw_price, bool):
            # 数值型输入视为「元」（API 层约定金额用元），转分
            cents = int(round(float(raw_price) * 100))
        elif isinstance(raw_price, str) and raw_price.strip():
            try:
                cents = int(round(float(raw_price.strip()) * 100))
            except ValueError:
                cents = None
        elif isinstance(raw_price, (int,)) and raw_price > 0 and raw.get("sale_price_cents") is None:
            cents = int(raw_price)

        if cents is None or cents <= 0:
            missing.append(sku_code)
            continue

        item = dict(raw)
        item["shop_sku_code"] = sku_code
        item["sale_price_cents"] = cents
        normalized.append(item)

    if missing:
        raise BusinessError(
            f"{SALE_PRICE_REQUIRED_MESSAGE}（缺失或非法售价的 SKU：{', '.join(missing[:10])}"
            f"{'...' if len(missing) > 10 else ''}）",
            code=ErrorCode.PARAM_ERROR,
            http_status=422,
        )
    return normalized


MANUAL_INSTRUCTIONS_TEMPLATE = """【半自动上架操作指引】
================================================
本系统未取得 {platform_label} 开放平台资质，无法直接调用发布接口。
请按以下步骤人工完成上架，然后回写商品 ID。

1. 解压同目录下的素材包，取出 images/ 目录中的主图与详情图；
2. 登录 {platform_label} 商家后台 → 发布商品 → 选择类目：{category_id}；
3. 复制 form_data.json 中的标题 / 卖点 / 属性 / 价格 / 库存，逐项粘贴到表单；
4. 上传 images/ 中的图片（第一张为主图）；
5. 提交发布，平台生成商品 ID 后，回到本系统「上架任务 → 回填商品 ID」粘贴；
6. 回填后系统自动建立 SKU 映射，之后该商品即可进入订单履约链路。

★ 注意：
   - 商品 ID 必须全系统唯一，重复回填会被拒绝（错误码 4006）；
   - 各 SKU 编码建议与 form_data.json 中的 sku_list 保持一致，便于自动建映射。
================================================
生成时间：{generated_at}（UTC）
货源商品 ID：{source_product_id}
"""


class ManualListingAdapter(ListingAdapter):
    """半自动上架适配器：产出素材包 + 预填表单，不直接调用平台 API。"""

    def __init__(
        self,
        account: Any = None,
        config: dict[str, Any] | None = None,
        session: Any = None,
        http: Any = None,
        platform: str | Platform = Platform.TAOBAO,
    ) -> None:
        """初始化半自动适配器。

        Args:
            platform: 平台枚举或平台字符串（如 `"taobao"`）。★ 内部统一归一化为枚举，
                避免调用方传 str 后 `self.platform.value` 崩溃。
        """
        super().__init__(account=account, config=config, session=session, http=http)
        self.platform = normalize_platform(platform)
        self.mode = ListingMode.MANUAL

    # ---------------- 素材包 ----------------

    def build_form_data(self, payload: ListingPayload) -> dict[str, Any]:
        """构造可直接复制到平台后台的预填表单数据。"""
        sku_list: list[dict[str, Any]] = []
        for index, sku in enumerate(payload.skus):
            sku_list.append(
                {
                    "seq": index + 1,
                    "spec_json": dict(sku.spec_json),
                    "spec_text": " / ".join(f"{k}:{v}" for k, v in sku.spec_json.items()),
                    "sale_price": f"{sku.sale_price_cents / 100:.2f}",
                    "stock_qty": sku.stock_qty,
                    "source_sku_code_1688": sku.source_sku_code_1688,
                    "purchase_cost": f"{(sku.purchase_cost_cents or 0) / 100:.2f}",
                }
            )
        return {
            "platform": self.platform.value,
            "shop_id": payload.shop_id,
            "category_id": payload.category_id or "",
            "title": payload.title,
            "selling_points": list(payload.selling_points),
            "attributes": dict(payload.attributes_json),
            "main_image_count": len(payload.main_images),
            "detail_image_count": len(payload.detail_images),
            "sku_list": sku_list,
            "source_product_id": payload.source_product_id,
            "generated_at": iso_utc(utc_now()),
        }

    def build_readme(self, payload: ListingPayload) -> str:
        """构造人工操作指引（中文）。"""
        labels = {"taobao": "淘宝", "douyin": "抖店", "pdd": "拼多多"}
        return MANUAL_INSTRUCTIONS_TEMPLATE.format(
            platform_label=labels.get(self.platform.value, self.platform.value),
            category_id=payload.category_id or "（请在后台自行选择）",
            source_product_id=payload.source_product_id,
            generated_at=iso_utc(utc_now()),
        )

    def package_dir(self) -> Path:
        """素材包输出目录 `data/packages/`。"""
        return get_settings().packages_dir

    async def build_manual_package(self, payload: ListingPayload) -> AdapterResult[ManualPackage]:
        """生成素材包 ZIP：图片 + form_data.json + README.txt。"""
        settings = get_settings()
        base_dir = self.package_dir()
        base_dir.mkdir(parents=True, exist_ok=True)

        stem = safe_filename(f"manual-{self.platform.value}-{payload.source_product_id}-{int(time.time())}")
        zip_path = base_dir / f"{stem}.zip"

        image_files = [p for p in list(payload.main_images) + list(payload.detail_images) if p and Path(p).exists()]
        form_data = self.build_form_data(payload)
        readme = self.build_readme(payload)

        extra_files = {
            "form_data.json": json.dumps(form_data, ensure_ascii=False, indent=2),
            "README.txt": readme,
        }

        try:
            real_path = make_zip_package(
                files=image_files,
                dest=zip_path,
                extra_files=extra_files,
                base_dir="images",
            )
        except Exception as exc:  # noqa: BLE001
            return AdapterResult.fatal(method="build_manual_package", error=exc)

        package = ManualPackage(
            package_path=real_path,
            form_data=form_data,
            image_files=image_files,
            instructions=readme,
        )
        logger.info(
            "manual_package_built",
            platform=self.platform.value,
            package_path=real_path,
            image_count=len(image_files),
            storage_dir=str(settings.storage_dir),
        )
        return AdapterResult.success(package, message="已生成半自动素材包，请人工发布后回填商品 ID")

    # ---------------- 上架能力 ----------------

    async def publish(self, payload: ListingPayload) -> AdapterResult[ListingPublishResult]:
        """半自动模式不直接发布：先产出素材包，等人工回填 ID。

        此处返回 DEGRADED 信封，`data` 为空壳结果（shop_item_id 待回填）。
        """
        # ★ 主路径硬校验：半自动模式下每个 SKU 必须有售价（LST-P0-07）
        #   没有售价 → 无法做成本倒挂检测 → 该商品永久失去该保护（静默失效）
        no_price = [sku.spec_json for sku in payload.skus if int(sku.sale_price_cents or 0) <= 0]
        if no_price:
            return AdapterResult.fatal(
                method="publish",
                error=f"{SALE_PRICE_REQUIRED_MESSAGE}（{len(no_price)} 个 SKU 售价为空）",
                fallback="manual",
            )

        package_result = await self.build_manual_package(payload)
        if not package_result.ok:
            return AdapterResult.fatal(
                method="publish",
                error=package_result.message or "素材包生成失败",
                fallback="manual",
            )
        placeholder = ListingPublishResult(
            shop_item_id="",  # 待人工回填
            sku_results=[],
            is_mock=False,
            raw_response={"package_path": package_result.data.package_path} if package_result.data else None,
        )
        return AdapterResult.degraded(
            placeholder,
            message="半自动模式：素材包已生成，请在平台人工发布后回填商品 ID",
            fallback="manual",
        )

    async def query_status(self, shop_item_ids: Sequence[str]) -> AdapterResult[list[ListingStatus]]:
        """半自动模式无法查询平台状态：从本地 listing_product 读取最后已知状态。"""
        if self.session is None:
            return AdapterResult.unsupported("query_status", fallback="manual", message="缺少数据库会话")

        try:
            stmt = select(ListingProduct).where(
                ListingProduct.platform == self.platform.value,
                ListingProduct.shop_item_id.in_([str(i) for i in shop_item_ids]),
                ListingProduct.is_deleted.is_(False),
            )
            rows = (await self.session.execute(stmt)).scalars().all()
        except Exception as exc:  # noqa: BLE001
            return AdapterResult.fatal(method="query_status", error=exc)

        statuses = [
            ListingStatus(
                shop_item_id=row.shop_item_id,
                status=row.status,
                shop_sku_codes=[],
                updated_at=iso_utc(row.updated_at) if row.updated_at else None,
            )
            for row in rows
        ]
        return AdapterResult.success(statuses, message="半自动模式：状态来自 ERP 本地记录")

    async def offline(self, shop_item_ids: Sequence[str], reason: str) -> AdapterResult[dict]:
        """半自动下架：ERP 侧记录下架原因，人工去平台后台操作。"""
        logger.info("manual_listing_offline", items=list(shop_item_ids), reason=reason)
        return AdapterResult.degraded(
            {
                "offlined": [str(i) for i in shop_item_ids],
                "reason": reason,
                "need_manual_action": True,
            },
            message="半自动模式：请在平台后台人工执行下架",
        )

    async def update_stock_price(self, items: Sequence[dict]) -> AdapterResult[dict]:
        """半自动改库存与价格：生成待办清单，人工去平台后台修改。"""
        logger.info("manual_listing_update_stock_price", count=len(items))
        return AdapterResult.degraded(
            {"pending_items": [dict(i) for i in items], "need_manual_action": True},
            message="半自动模式：请在平台后台人工调整库存与价格",
        )

    async def health_check(self) -> AdapterResult[HealthStatus]:
        """半自动适配器健康 = 素材包目录可写。"""
        started = time.perf_counter()
        try:
            base_dir = self.package_dir()
            base_dir.mkdir(parents=True, exist_ok=True)
            healthy = base_dir.is_dir()
            message = f"半自动上架适配器可用，素材包目录：{base_dir}"
        except Exception as exc:  # noqa: BLE001
            healthy = False
            message = f"素材包目录不可用：{exc}"
        status = HealthStatus.build(
            healthy=healthy,
            status="healthy" if healthy else "down",
            message=message,
            started=started,
        )
        return AdapterResult.success(status)

    # ---------------- 回填校验 ----------------

    async def validate_shop_item_id(self, platform: str, shop_id: str, shop_item_id: str) -> None:
        """★ 回填商品 ID 时校验唯一性。

        Raises:
            BusinessError: 4006 商品 ID 回填重复。
        """
        if not shop_item_id or not shop_item_id.strip():
            raise BusinessError("回填的商品 ID 不能为空", code=ErrorCode.PUBLISH_ITEM_ID_DUPLICATE)
        if self.session is None:
            return
        stmt = select(ListingProduct.id).where(
            ListingProduct.platform == platform,
            ListingProduct.shop_id == shop_id,
            ListingProduct.shop_item_id == shop_item_id.strip(),
            ListingProduct.is_deleted.is_(False),
        )
        exists = (await self.session.execute(stmt)).scalar_one_or_none()
        if exists is not None:
            raise BusinessError(
                f"商品 ID {shop_item_id} 已存在于 {platform}/{shop_id}，不允许重复回填",
                code=ErrorCode.PUBLISH_ITEM_ID_DUPLICATE,
            )

    async def fill_back_shop_item_id(
        self,
        *,
        platform: str,
        shop_id: str,
        shop_item_id: str,
        sku_codes: Sequence[str] | None = None,
        skus: Sequence[dict[str, Any]] | None = None,
        require_sale_price: bool = True,
    ) -> dict[str, Any]:
        """★ 回填商品 ID（主路径）：校验唯一性 + 售价必填，返回可写入 publish_task 的结果。

        Args:
            platform / shop_id / shop_item_id: 平台与商品标识。
            sku_codes: 仅回填 SKU 编码列表的简版入参（不校验售价）。
            skus: 完整 SKU 列表（含 `sale_price`），**推荐**；存在时按 LST-P0-07 校验售价。
            require_sale_price: 是否强制校验售价（默认 True → 缺失即 422）。

        Returns:
            可写入 `publish_task` 的字典，含 `shop_item_id` / `skus`(规范化) / `filled_at` 等。

        Raises:
            BusinessError: 4006 商品 ID 重复；422 售价缺失（LST-P0-07）。
        """
        await self.validate_shop_item_id(platform, shop_id, shop_item_id)

        normalized_skus: list[dict[str, Any]] = []
        if skus:
            normalized_skus = validate_fill_back_skus(skus) if require_sale_price else [
                dict(item) for item in skus if isinstance(item, dict)
            ]
        elif require_sale_price and sku_codes:
            # 简版入参不带价格：无法校验售价 → 明确拒绝，避免静默写入空售价
            raise BusinessError(
                f"{SALE_PRICE_REQUIRED_MESSAGE}（当前仅传了 sku_codes，请改传 skus 并带上 sale_price）",
                code=ErrorCode.PARAM_ERROR,
                http_status=422,
            )

        return {
            "shop_item_id": shop_item_id.strip(),
            "shop_sku_codes": [str(c) for c in (sku_codes or [])]
            or [str(item.get("shop_sku_code", "")) for item in normalized_skus],
            "skus": normalized_skus,
            "platform": platform,
            "shop_id": shop_id,
            "filled_at": iso_utc(utc_now()),
            "listing_mode": ListingMode.MANUAL.value,
        }

    def available(self) -> bool:
        """半自动模式始终可用（无需平台资质）。"""
        return True

    def note(self) -> str:
        """前端展示说明。"""
        return "半自动模式：生成素材包与预填表单，人工在平台发布后回填商品 ID"

    def dump_form_data(self, payload: ListingPayload) -> str:
        """调试用：返回表单数据 JSON 字符串。"""
        return json_dumps(self.build_form_data(payload))
