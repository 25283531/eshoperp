"""上架任务服务（§5.5.7、§6.1）。

================================================================================
★ ★ ★ 附录 A 第 3 条：上架链路的唯一 publish 调用点 ★ ★ ★
================================================================================
本模块 `PublishService.execute()` 是全系统**唯一**调用 `ListingAdapter.publish()` 的地方，
且调用前**必须**先跑 `MappingValidator.validate()`，`blocking=true` 时直接转
`validate_failed`，**没有任何绕过路径**（PRD MAP-P0-02）。

★ 全仓检索自检：`grep -rn "\.publish(" app/ | grep -v publish_service` 应**无结果**。
================================================================================

状态机（§7.1）：
    pending_precheck → (precheck_failed | pending_validate)
    pending_validate → (validate_failed | pending_publish)
    pending_publish  → publishing → (publish_success | publish_failed)
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select

from app.adapters.listing.base import ListingPayload, ListingSkuPayload
from app.adapters.listing.factory import ListingAdapterFactory
from app.adapters.listing.manual import ManualListingAdapter, validate_fill_back_skus
from app.core.config import get_settings
from app.core.errors import BusinessError, ErrorCode, NotFoundError, StateConflictError
from app.core.logging import get_logger, get_trace_id
from app.models.asset import AiTaskResult, Asset
from app.models.enums import (
    AuditActionType,
    AuditObjectType,
    ChangeSource,
    ListingMode,
    ListingProductStatus,
    ListingSkuStatus,
    MappingSource,
    MappingStatus,
    PublishStatus,
    TaskType,
)
from app.models.listing import ListingProduct, ListingSku
from app.models.mapping import MappingChangeLog, SkuMapping
from app.models.publish import PublishTask
from app.models.source import SourceProduct, SourceSku
from app.schemas.mapping import FillBackRequest
from app.schemas.mapping import MappingValidationVo
from app.services.audit_service import AuditService
from app.services.mapping_validator import MappingValidator
from app.utils.kit import iso_utc, utc_now

logger = get_logger(__name__)

__all__ = ["PublishService"]

# 单次批量上架上限（§5.5.7：≤50）
MAX_BATCH_PUBLISH = 50


class PublishService:
    """上架任务：创建 / 预检 / 校验 / 执行 / 回填。"""

    # ==================================================================
    #  创建
    # ==================================================================

    @staticmethod
    async def create_tasks(
        session: Any,
        *,
        source_product_ids: list[int],
        platform: str,
        shop_id: str,
        mode: str | None = None,
        ai_task_result_ids: dict[str, int] | None = None,
        operator: str = "system",
        submit_async: bool = True,
    ) -> tuple[list[PublishTask], list[int]]:
        """批量创建上架任务。

        ★ AIR-P0-03：引用的 AI 结果必须 `review_status='approved'`，否则 422 / 4005。
          素材未审核就上架 = 把未过审的图发到平台，是 P0 事故。

        ★★ 入队失败**不再静默**（2026-10-08 修复）★★
            旧实现：`session.add(task) → flush → 立刻 enqueue`，
            此时请求会话**仍持有未提交的写事务**，而 `TaskRunner.submit()` 会另开会话
            写 `task_record` 并 commit —— SQLite 单写者模型下必然撞
            `database is locked`；异常又被 `except Exception: return None` 吞掉，
            于是接口照样返回 **202**、`task_record_ids` 却是**空数组**：
            前端看到"受理成功"，`publish_task` 永远停在 `pending_precheck`
            （实测 18/18 条 `task_record_id` 全 NULL、`task_record` 里 0 条 publish）。
            现在：**先 commit 再入队**（根治写锁）+ **入队失败抛 500 / 1098**（拒绝假成功）。

        Returns:
            `(任务列表, task_record_id 列表)`。
        """
        if not source_product_ids:
            raise BusinessError("source_product_ids 不能为空", code=ErrorCode.PARAM_ERROR)
        if len(source_product_ids) > MAX_BATCH_PUBLISH:
            raise BusinessError(
                f"单次批量上架上限 {MAX_BATCH_PUBLISH} 个", code=ErrorCode.PARAM_ERROR
            )

        resolved_mode = mode or await PublishService._resolve_mode(session)
        result_map = {int(k): int(v) for k, v in (ai_task_result_ids or {}).items()}

        tasks: list[PublishTask] = []
        record_ids: list[int] = []

        for product_id in source_product_ids:
            product = (
                await session.execute(
                    select(SourceProduct).where(
                        SourceProduct.id == int(product_id), SourceProduct.is_deleted.is_(False)
                    )
                )
            ).scalars().first()
            if product is None:
                logger.warning("publish_source_product_missing", source_product_id=product_id)
                continue

            ai_result_id = result_map.get(int(product_id))
            # ★ AIR-P0-03 硬校验：非 approved 一律拒绝
            await MappingValidator.validate_ai_result_approved(session, ai_result_id)

            task = PublishTask(
                source_product_id=int(product_id),
                ai_task_result_id=ai_result_id,
                platform=platform,
                shop_id=shop_id,
                listing_mode=resolved_mode,
                status=PublishStatus.PENDING_PRECHECK.value,
                is_mock=(resolved_mode == ListingMode.MOCK.value),
                created_by=operator,
            )
            session.add(task)
            await session.flush()
            tasks.append(task)

        # ★★ 根治写锁：先把 `publish_task` 落库并**提交**，释放本会话的写事务，
        #    再投递异步任务（投递方会用自己的会话写 `task_record`）。
        await session.commit()

        if submit_async:
            record_ids = await PublishService._enqueue_all(session, tasks, operator=operator)
        return tasks, record_ids

    @staticmethod
    async def _enqueue_all(
        session: Any, tasks: list[PublishTask], *, operator: str = "system"
    ) -> list[int]:
        """逐个投递上架任务到异步队列；**任一失败即抛出**（不再吞成 None）。

        ★ 失败可见性三处同时落地：
            1. HTTP：抛 `BusinessError(500 / 1098)` —— 不再返回"假装成功"的 202；
            2. 任务行：`publish_task.error_advice` 写明原因 —— 列表页看得到；
            3. 日志：`logger.error("publish_task_enqueue_failed", ...)`。

        Returns:
            成功入队的 `task_record.id` 列表。
        """
        record_ids: list[int] = []
        failed: list[dict[str, Any]] = []

        for task in tasks:
            try:
                record_id = await PublishService._enqueue_task(int(task.id), operator=operator)
            except Exception as exc:  # noqa: BLE001  ★ 只在这里收口，且收口后**继续上抛**
                reason = f"{type(exc).__name__}: {exc}"
                task.error_advice = (
                    f"异步任务入队失败，本次上架未真正执行（{reason}）。请在列表页点「重试」。"
                )
                failed.append({"publish_task_id": int(task.id), "reason": reason})
                logger.error(
                    "publish_task_enqueue_failed", publish_task_id=int(task.id), error=str(exc)
                )
                continue
            task.task_record_id = record_id
            record_ids.append(int(record_id))

        await session.commit()

        if failed:
            raise BusinessError(
                f"{len(failed)}/{len(tasks)} 个上架任务入队失败，未真正执行：{failed[0]['reason']}",
                code=ErrorCode.TASK_SUBMIT_FAILED,
                http_status=500,
                detail={"enqueued": record_ids, "failed": failed},
            )
        return record_ids

    @staticmethod
    async def _enqueue_task(publish_task_id: int, *, operator: str = "system") -> int:
        """把**单个**上架任务投递到异步队列（`TaskRunner`），返回 `task_record.id`。

        Raises:
            BusinessError: 500 / 1098 —— 队列不可用或写 `task_record` 失败。**不吞**。
        """
        try:
            from app.tasks.runner import get_task_runner

            runner = get_task_runner()
            task_id = await runner.submit(
                task_type=TaskType.PUBLISH.value,
                payload={"publish_task_id": int(publish_task_id), "operator": operator},
                task_key=f"publish:{int(publish_task_id)}",
            )
        except Exception as exc:  # noqa: BLE001
            raise BusinessError(
                f"上架任务 {publish_task_id} 投递到异步队列失败：{exc}",
                code=ErrorCode.TASK_SUBMIT_FAILED,
                http_status=500,
            ) from exc
        if not task_id:
            raise BusinessError(
                f"上架任务 {publish_task_id} 投递异常：任务队列未返回任务号",
                code=ErrorCode.TASK_SUBMIT_FAILED,
                http_status=500,
            )
        return int(task_id)

    @staticmethod
    async def _resolve_mode(session: Any) -> str:
        """解析上架模式：优先 `SystemSetting['listing.mode']`。"""
        from app.models.enums import SettingKey
        from app.models.system import SystemSetting

        stmt = select(SystemSetting).where(SystemSetting.setting_key == SettingKey.LISTING_MODE.value)
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is not None and row.setting_value:
            return str(row.setting_value).strip()
        return get_settings().listing_mode

    # ==================================================================
    #  查询
    # ==================================================================

    @staticmethod
    async def list_tasks(
        session: Any,
        *,
        platform: str | None = None,
        shop_id: str | None = None,
        status: str | None = None,
        mode: str | None = None,
        source_product_id: int | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[PublishTask], dict[int, str], int]:
        """分页查询上架任务。"""
        stmt = select(PublishTask)
        if platform:
            stmt = stmt.where(PublishTask.platform == platform)
        if shop_id:
            stmt = stmt.where(PublishTask.shop_id == shop_id)
        if status:
            stmt = stmt.where(PublishTask.status == status)
        if mode:
            stmt = stmt.where(PublishTask.listing_mode == mode)
        if source_product_id is not None:
            stmt = stmt.where(PublishTask.source_product_id == int(source_product_id))

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await session.execute(count_stmt)).scalar_one() or 0)
        rows = (
            (await session.execute(stmt.order_by(PublishTask.id.desc()).offset((page - 1) * page_size).limit(page_size)))
            .scalars()
            .all()
        )
        product_ids = sorted({int(t.source_product_id) for t in rows})
        titles: dict[int, str] = {}
        if product_ids:
            products = (
                (await session.execute(select(SourceProduct).where(SourceProduct.id.in_(product_ids))))
                .scalars()
                .all()
            )
            titles = {int(p.id): p.title for p in products}
        return list(rows), titles, total

    @staticmethod
    async def get_task(session: Any, task_id: int) -> PublishTask:
        """取上架任务（404）。"""
        task = (await session.execute(select(PublishTask).where(PublishTask.id == int(task_id)))).scalars().first()
        if task is None:
            raise NotFoundError(f"上架任务 {task_id} 不存在")
        return task

    # ==================================================================
    #  合规预检（§6.1 第一步）
    # ==================================================================

    @staticmethod
    async def precheck(session: Any, task_id: int, *, operator: str = "system") -> dict[str, Any]:
        """合规预检：素材是否就绪、标题是否含违禁词、类目是否填写。"""
        task = await PublishService.get_task(session, task_id)
        failed: list[dict[str, Any]] = []

        product = (
            await session.execute(select(SourceProduct).where(SourceProduct.id == task.source_product_id))
        ).scalars().first()
        if product is None:
            failed.append({"item": "货源商品", "reason": "货源商品不存在", "suggestion": "重新采集"})
        else:
            if not (product.title or "").strip():
                failed.append({"item": "标题", "reason": "标题为空", "suggestion": "先采集或填写标题"})

        # 素材就绪：优先用审核通过的 AI 产出，否则用原始素材
        assets = await PublishService._pick_assets(session, task)
        if not assets:
            failed.append(
                {"item": "素材", "reason": "无可用图片素材", "suggestion": "先采集货源商品或执行 AI 重构"}
            )

        # 违禁词扫描
        banned = PublishService._scan_banned_words(product.title if product else "")
        for word in banned:
            failed.append(
                {"item": f"标题-{word}", "reason": "命中违禁词/极限词", "suggestion": f"请替换或删除「{word}」"}
            )

        passed = not failed
        result = {"passed": passed, "failed_items": failed}
        task.precheck_result_json = result
        task.status = (
            PublishStatus.PENDING_VALIDATE.value if passed else PublishStatus.PRECHECK_FAILED.value
        )
        await session.flush()
        return result

    @staticmethod
    def _scan_banned_words(title: str | None) -> list[str]:
        """扫描违禁词 / 极限词。"""
        if not title:
            return []
        try:
            from app.adapters.ai.mock_client import scan_banned_words

            return [item.get("word", "") for item in scan_banned_words(title)]
        except Exception:  # noqa: BLE001  扫描失败不阻断预检
            return []

    @staticmethod
    async def _pick_assets(session: Any, task: PublishTask) -> list[Asset]:
        """挑选上架用素材：优先审核通过的 AI 产出，回退原始素材。"""
        if task.ai_task_result_id:
            result = (
                await session.execute(
                    select(AiTaskResult).where(AiTaskResult.id == int(task.ai_task_result_id))
                )
            ).scalars().first()
            ids = [int(i) for i in (result.output_asset_ids_json or [])] if result else []
            if ids:
                rows = (
                    await session.execute(
                        select(Asset).where(Asset.id.in_(ids), Asset.is_deleted.is_(False))
                    )
                ).scalars().all()
                if rows:
                    return list(rows)
        rows = (
            await session.execute(
                select(Asset)
                .where(
                    Asset.source_product_id == int(task.source_product_id),
                    Asset.is_deleted.is_(False),
                    Asset.is_current.is_(True),
                )
                .order_by(Asset.id)
            )
        ).scalars().all()
        return list(rows)

    # ==================================================================
    #  ★ 执行上架（唯一 publish 调用点）
    # ==================================================================

    @staticmethod
    async def execute(session: Any, task_id: int, *, operator: str = "system") -> PublishTask:
        """★ 执行上架：**先强制映射校验，再调 `ListingAdapter.publish()`**。

        流程（§6.1）：
            1. `precheck()` —— 合规预检；
            2. `MappingValidator.validate()` —— **blocking 则转 validate_failed，硬拦截无绕过**；
            3. `ListingAdapter.publish()` —— ★ 全系统唯一调用点。

        Returns:
            执行后的 `PublishTask`。
        """
        task = await PublishService.get_task(session, task_id)

        # ---------- ① 合规预检 ----------
        if task.status in {PublishStatus.PENDING_PRECHECK.value, PublishStatus.PRECHECK_FAILED.value}:
            precheck_result = await PublishService.precheck(session, task_id, operator=operator)
            if not precheck_result["passed"]:
                return task

        # ---------- ② ★ 强制映射校验（MAP-P0-02，无绕过路径）----------
        if task.status in {
            PublishStatus.PENDING_VALIDATE.value,
            PublishStatus.VALIDATE_FAILED.value,
            PublishStatus.PENDING_PRECHECK.value,
        }:
            sku_codes = await PublishService._validation_sku_codes(session, task)
            validation: MappingValidationVo = await MappingValidator.validate(
                session,
                source_product_id=int(task.source_product_id),
                platform=task.platform,
                shop_id=task.shop_id,
                sku_codes=sku_codes,
            )
            task.validate_result_json = json.loads(validation.model_dump_json())
            if validation.blocking:
                task.status = PublishStatus.VALIDATE_FAILED.value
                task.error_advice = f"映射校验未通过：{validation.blocked_reason}。请先处理冲突或补齐映射再重试。"
                await session.flush()
                await AuditService.write(
                    session,
                    action_type=AuditActionType.PUBLISH.value,
                    object_type=AuditObjectType.PUBLISH_TASK.value,
                    object_id=task.id,
                    operator=operator,
                    new_value={"blocked_reason": validation.blocked_reason},
                    trace_id=get_trace_id(),
                    remark="上架被映射校验硬拦截",
                )
                await session.flush()
                logger.warning("publish_blocked_by_mapping_validation", task_id=task.id, reason=validation.blocked_reason)
                return task
            task.status = PublishStatus.PENDING_PUBLISH.value
            await session.flush()

        if task.status != PublishStatus.PENDING_PUBLISH.value:
            raise StateConflictError(f"任务状态 {task.status} 不允许发布")

        # ---------- ③ ★ 唯一 publish 调用点 ----------
        task.status = PublishStatus.PUBLISHING.value
        await session.flush()

        payload = await PublishService._build_payload(session, task)
        adapter = await ListingAdapterFactory.create(
            task.platform, mode=task.listing_mode, session=session
        )

        try:
            result = await adapter.invoke("publish", payload=payload)
        except Exception as exc:  # noqa: BLE001
            result = None
            logger.exception("publish_adapter_error", task_id=task.id, error=str(exc))
            task.status = PublishStatus.PUBLISH_FAILED.value
            task.platform_error_code = type(exc).__name__
            task.platform_error_msg = str(exc)
            task.error_advice = f"上架调用异常：{exc}"
            await session.flush()
            return task

        if result is None or result.code not in {"OK", "DEGRADED"}:
            task.status = PublishStatus.PUBLISH_FAILED.value
            task.platform_error_code = str(result.code) if result else "UNKNOWN"
            task.platform_error_msg = result.message if result else "适配器未返回结果"
            task.error_advice = PublishService._advice_for(result.code if result else "UNKNOWN")
            await session.flush()
            return task

        data = result.data
        shop_item_id = str(getattr(data, "shop_item_id", "") or "")
        sku_results = list(getattr(data, "sku_results", []) or [])

        # 半自动模式：无 shop_item_id（待人工回填），保持 publishing 并挂素材包路径
        if task.listing_mode == ListingMode.MANUAL.value and not shop_item_id:
            raw = getattr(data, "raw_response", None) or {}
            task.package_path = str(raw.get("package_path") or "")
            task.shop_sku_codes_json = [str(s.get("shop_sku_code", "")) for s in sku_results]
            await session.flush()
            await AuditService.write(
                session,
                action_type=AuditActionType.PUBLISH.value,
                object_type=AuditObjectType.PUBLISH_TASK.value,
                object_id=task.id,
                operator=operator,
                new_value={"package_path": task.package_path, "mode": "manual"},
                trace_id=get_trace_id(),
                remark="半自动模式：素材包已生成，等待人工发布后回填商品 ID",
            )
            await session.flush()
            return task

        # Mock / 真实模式：有 shop_item_id → 落平台商品与 SKU
        await PublishService._persist_listing(
            session,
            task=task,
            shop_item_id=shop_item_id,
            sku_results=sku_results,
            is_mock=bool(getattr(data, "is_mock", False)) or task.listing_mode == ListingMode.MOCK.value,
            operator=operator,
        )
        task.shop_item_id = shop_item_id
        task.status = PublishStatus.PUBLISH_SUCCESS.value
        await session.flush()

        await AuditService.write(
            session,
            action_type=AuditActionType.PUBLISH.value,
            object_type=AuditObjectType.PUBLISH_TASK.value,
            object_id=task.id,
            operator=operator,
            new_value={"shop_item_id": shop_item_id, "mode": task.listing_mode},
            trace_id=get_trace_id(),
            remark=f"上架成功：{shop_item_id}",
        )
        await session.flush()
        logger.info("publish_task_success", task_id=task.id, shop_item_id=shop_item_id, mode=task.listing_mode)
        return task

    @staticmethod
    def _advice_for(code: Any) -> str:
        """错误码 → 中文处置建议（§5.5.7 `error_advice`）。"""
        advice = {
            "UNSUPPORTED": "当前上架模式不支持该能力，请切换到 Mock 或半自动模式",
            "SCOPE_DENIED": "第三方权限越权被拒（商品编辑权不在白名单内），请联系管理员",
            "RETRYABLE": "平台返回可重试错误，请稍后重试",
            "FATAL": "上架调用失败，请查看日志定位原因",
        }
        return advice.get(str(code), "未知错误，请联系管理员查看日志")

    # ==================================================================
    #  载荷与落库
    # ==================================================================

    @staticmethod
    async def _validation_sku_codes(session: Any, task: PublishTask) -> list[str]:
        """★ 上架前映射校验的**校验对象**：该货源商品在该店铺**已存在**的映射编码。

        ★★ 为什么不能用「预测的店铺 SKU 编码」（旧实现）★★
            旧实现按 `{平台}-{任务ID}-{序号}` 拼出一组**尚不存在**的店铺 SKU 编码去查映射，
            而映射只在 `publish()` / `fill_back()` **成功之后**才由 `_persist_listing()`
            自动建立。于是首次上架**必然**命中「N 项映射缺失」→ `blocking=true`
            → 状态转 `validate_failed`，**半自动主路径（映射靠回填才产生）永远走不到
            `pending_publish`**。「查一个还不存在的东西是否存在」不是校验，是恒失败。

        ★ 正确语义（上架前这一步该校验什么）：
            * 已存在映射（再次上架 / 补货 / 换店重发）→ 校验它们是否 `valid`、
              是否命中 P0 冲突（duplicate / cost_invalid / spec_mismatch）；
            * 尚无映射（首次上架，映射将由本次上架自动建立）
              → 不把「缺失」当拦截理由，只跑冲突检测（含 fail-closed 的 `incomplete`）；
            * **映射完整性（缺失）的真正兜底在下游**：
              订单匹配阶段（`MAPPING_MISSING` / 订单挂起）与
              回填后的 `cost_underwater` 重算。
            * 半自动（manual）模式下，`validate()` 就在这一步跑 ——
              **生成素材包之前**（`pending_validate → pending_publish`），
              校验通过后才会产包、等人工回填，语义正确。

        Returns:
            已存在的店铺 SKU 编码列表（去重排序）；无映射时为空列表。
        """
        rows = (
            await session.execute(
                select(SkuMapping.shop_sku_code).where(
                    SkuMapping.source_product_id == int(task.source_product_id),
                    SkuMapping.platform == task.platform,
                    SkuMapping.shop_id == task.shop_id,
                    SkuMapping.is_deleted.is_(False),
                )
            )
        ).scalars().all()
        return sorted({str(code).strip() for code in rows if str(code).strip()})

    @staticmethod
    async def _build_payload(session: Any, task: PublishTask) -> ListingPayload:
        """组装上架载荷。"""
        product = (
            await session.execute(select(SourceProduct).where(SourceProduct.id == task.source_product_id))
        ).scalars().first()
        assets = await PublishService._pick_assets(session, task)
        source_skus = (
            await session.execute(
                select(SourceSku).where(
                    SourceSku.source_product_id == int(task.source_product_id),
                    SourceSku.is_deleted.is_(False),
                )
            )
        ).scalars().all()

        main_images = [a.storage_path for a in assets if a.asset_type == "main_image"][:5]
        detail_images = [a.storage_path for a in assets if a.asset_type == "detail_image"][:9]
        if not main_images and assets:
            main_images = [assets[0].storage_path]
            detail_images = [a.storage_path for a in assets[1:9]]

        sku_payloads: list[ListingSkuPayload] = []
        for index, sku in enumerate(source_skus):
            sale_price_cents = await PublishService._resolve_sale_price(session, sku, task)
            sku_payloads.append(
                ListingSkuPayload(
                    spec_json=dict(sku.spec_json or {}),
                    sale_price_cents=sale_price_cents,
                    stock_qty=int(sku.stock_qty or 0),
                    source_sku_id=int(sku.id),
                    source_sku_code_1688=sku.sku_code_1688,
                    purchase_cost_cents=int(sku.cost_price_cents or 0),
                )
            )
        if not sku_payloads:
            # 货源无 SKU（单规格商品）：造一个兜底 SKU，成本取商品级成本
            sku_payloads.append(
                ListingSkuPayload(
                    spec_json={},
                    sale_price_cents=await PublishService._resolve_sale_price(session, None, task),
                    stock_qty=0,
                    source_sku_id=None,
                    source_sku_code_1688=None,
                    purchase_cost_cents=int(product.cost_price_cents or 0) if product else 0,
                )
            )

        title = product.title if product else ""
        attributes: dict[str, Any] = {}
        selling_points: list[str] = []
        result = await PublishService._load_ai_result(session, task)
        if result is not None:
            title = result.output_title or title
            attributes = dict(result.output_attributes_json or {})
            selling_points = [s for s in (result.output_selling_points or "").split("\n") if s.strip()]

        return ListingPayload(
            shop_id=task.shop_id,
            title=title,
            selling_points=selling_points,
            attributes_json=attributes,
            category_id=str(attributes.get("category_id", "") or ""),
            main_images=main_images,
            detail_images=detail_images,
            skus=sku_payloads,
            source_product_id=int(task.source_product_id),
            ai_task_result_id=task.ai_task_result_id,
            trace_id=get_trace_id(),
        )

    @staticmethod
    async def _load_ai_result(session: Any, task: PublishTask) -> AiTaskResult | None:
        """★ 读取本任务引用的 AI 重构产出（标题 / 卖点 / 属性的**唯一来源**）。

        ★★ 为什么抽成公共方法 ★★
            `_build_payload()` 与 `_persist_listing()` 都要用 AI 产出。
            旧实现只有 `_build_payload()` 读，`_persist_listing()` 直接落
            `product.title`（货源原标题）⇒ AI 改写后的标题永远进不了
            `listing_product` 表，运营事后复盘「这个链接当时用的哪个标题」查不到。
            两处共用本方法，保证「素材包里的标题」与「库里的标题」同源。

        ★ 前置条件：`review_status='approved'` 的硬校验在 `create()` /
          `precheck()` 入口（`MappingValidator.validate_ai_result_approved`）
          已经拦过，这里**不再重复过滤**，否则会出现
          「包里是 AI 标题、库里是原标题」的反向不一致。
        """
        if not task.ai_task_result_id:
            return None
        return (
            await session.execute(select(AiTaskResult).where(AiTaskResult.id == int(task.ai_task_result_id)))
        ).scalars().first()

    @staticmethod
    async def _resolve_title(session: Any, task: PublishTask, product: SourceProduct | None) -> str:
        """上架标题：**优先 AI 产出标题**，无 AI 产出时回落到货源原标题。"""
        title = product.title if product else ""
        result = await PublishService._load_ai_result(session, task)
        if result is not None and result.output_title:
            title = result.output_title
        return title

    @staticmethod
    async def _resolve_sale_price(session: Any, sku: SourceSku | None, task: PublishTask) -> int:
        """定价：成本 × (1 + 加价率)，加价率取 `publish.markup_rate`（默认 1.35）。

        ★ 定价**必须 > 成本**，否则上车即倒挂。半自动回填时运营可改（且必填）。
        """
        from app.models.system import SystemSetting

        stmt = select(SystemSetting).where(SystemSetting.setting_key == "publish.markup_rate")
        row = (await session.execute(stmt)).scalar_one_or_none()
        try:
            markup = float(str(row.setting_value).strip()) if row and row.setting_value else 1.35
        except (TypeError, ValueError):
            markup = 1.35
        if markup < 1.0:
            markup = 1.35
        cost = int(sku.cost_price_cents or 0) if sku is not None else 0
        if cost <= 0:
            product = (
                await session.execute(select(SourceProduct).where(SourceProduct.id == task.source_product_id))
            ).scalars().first()
            cost = int(product.cost_price_cents or 0) if product else 0
        return max(int(round(cost * markup)), cost + 1 if cost > 0 else 0)

    @staticmethod
    async def _persist_listing(
        session: Any,
        *,
        task: PublishTask,
        shop_item_id: str,
        sku_results: list[dict[str, Any]],
        is_mock: bool,
        operator: str,
    ) -> ListingProduct:
        """落平台商品 + SKU + 映射。"""
        product = (
            await session.execute(select(SourceProduct).where(SourceProduct.id == task.source_product_id))
        ).scalars().first()

        listing = (
            await session.execute(
                select(ListingProduct).where(
                    ListingProduct.platform == task.platform,
                    ListingProduct.shop_id == task.shop_id,
                    ListingProduct.shop_item_id == shop_item_id,
                )
            )
        ).scalars().first()
        if listing is None:
            listing = ListingProduct(
                platform=task.platform,
                shop_id=task.shop_id,
                shop_item_id=shop_item_id,
                source_product_id=int(task.source_product_id),
                # ★ AI 改写标题优先，回落货源原标题（与 _build_payload 同源）
                title=await PublishService._resolve_title(session, task, product),
                status=ListingProductStatus.ON_SALE.value,
                is_mock=is_mock,
                listing_mode=task.listing_mode,
                published_at=utc_now(),
            )
            session.add(listing)
            await session.flush()

        source_skus = {
            sku.sku_code_1688: sku
            for sku in (
                await session.execute(
                    select(SourceSku).where(
                        SourceSku.source_product_id == int(task.source_product_id),
                        SourceSku.is_deleted.is_(False),
                    )
                )
            ).scalars().all()
        }

        for index, item in enumerate(sku_results):
            sku_code = str(item.get("shop_sku_code") or f"{shop_item_id}-{index + 1:03d}")
            spec = dict(item.get("spec_json") or {})
            source_code = item.get("source_sku_code_1688") or (
                list(source_skus.keys())[index] if index < len(source_skus) else None
            )
            source_sku = source_skus.get(str(source_code)) if source_code else None

            listing_sku = (
                await session.execute(
                    select(ListingSku).where(
                        ListingSku.listing_product_id == listing.id, ListingSku.shop_sku_code == sku_code
                    )
                )
            ).scalars().first()
            if listing_sku is None:
                listing_sku = ListingSku(
                    listing_product_id=int(listing.id),
                    shop_sku_code=sku_code,
                    spec_json=spec,
                    sale_price_cents=int(item.get("sale_price_cents") or 0) or None,
                    status=ListingSkuStatus.ON_SALE.value,
                )
                session.add(listing_sku)
                await session.flush()

            # ★ 自动建立 SKU 映射（source='auto_publish'）
            if source_sku is not None:
                exists = (
                    await session.execute(
                        select(SkuMapping).where(
                            SkuMapping.platform == task.platform,
                            SkuMapping.shop_id == task.shop_id,
                            SkuMapping.shop_sku_code == sku_code,
                            SkuMapping.is_deleted.is_(False),
                        )
                    )
                ).scalars().first()
                if exists is None:
                    mapping = SkuMapping(
                        platform=task.platform,
                        shop_id=task.shop_id,
                        shop_item_id=shop_item_id,
                        shop_sku_code=sku_code,
                        listing_product_id=int(listing.id),
                        listing_sku_id=int(listing_sku.id),
                        source_product_id=int(task.source_product_id),
                        source_sku_id=int(source_sku.id),
                        source_product_1688_id=product.product_1688_id if product else None,
                        source_sku_code_1688=source_sku.sku_code_1688,
                        spec_signature=source_sku.spec_signature,
                        purchase_cost_cents=int(source_sku.cost_price_cents or 0),
                        cost_source="auto",
                        status=MappingStatus.VALID.value,
                        is_mock=is_mock,
                        source=MappingSource.AUTO_PUBLISH.value,
                        created_by=operator,
                        updated_by=operator,
                    )
                    session.add(mapping)
                    await session.flush()
                    session.add(
                        MappingChangeLog.build(
                            sku_mapping_id=mapping.id,
                            change_action="create",
                            change_source=ChangeSource.SYSTEM.value,
                            operator=operator,
                            reason="上架自动建映射",
                            trace_id=get_trace_id(),
                        )
                    )
        await session.flush()
        return listing

    # ==================================================================
    #  半自动回填（★ LST-P0-07 售价必填）
    # ==================================================================

    @staticmethod
    async def fill_back(
        session: Any,
        task_id: int,
        payload: FillBackRequest,
        *,
        operator: str = "system",
    ) -> dict[str, Any]:
        """★ 半自动回填商品 ID —— **售价必填**（缺失 → 422），回填后自动建映射。

        这是 MVP 主路径（用户决策 ①：个体户店铺走半自动）。

        Returns:
            `{"publish_task_id": int, "mapping_ids": [int], "listing_product_id": int}`。
        """
        task = await PublishService.get_task(session, task_id)
        if task.listing_mode != ListingMode.MANUAL.value:
            raise BusinessError(
                f"仅半自动模式需要回填商品 ID（当前模式 {task.listing_mode}）",
                code=ErrorCode.PUBLISH_MOCK_RESTRICTED,
            )

        # ★ LST-P0-07：售价必填且 > 0，缺失直接 422 拒绝提交
        normalized = validate_fill_back_skus([item.model_dump() for item in payload.skus])

        adapter = ManualListingAdapter(session=session, platform=task.platform)
        filled = await adapter.fill_back_shop_item_id(
            platform=task.platform,
            shop_id=task.shop_id,
            shop_item_id=payload.shop_item_id,
            skus=normalized,
        )

        shop_item_id = str(filled["shop_item_id"])
        task.shop_item_id = shop_item_id
        task.shop_sku_codes_json = [str(item["shop_sku_code"]) for item in normalized]

        sku_results = [
            {
                "shop_sku_code": item["shop_sku_code"],
                "spec_json": dict(item.get("spec_json") or {}),
                "source_sku_code_1688": item.get("source_sku_code_1688"),
                "sale_price_cents": int(item.get("sale_price_cents") or 0),
                "purchase_cost_cents": int(item.get("purchase_cost_cents") or 0)
                if item.get("purchase_cost_cents")
                else None,
                "stock_qty": int(item.get("stock_qty") or 0),
            }
            for item in normalized
        ]

        listing = await PublishService._persist_listing(
            session,
            task=task,
            shop_item_id=shop_item_id,
            sku_results=sku_results,
            is_mock=False,
            operator=operator,
        )
        # ★ 回填的售价写入 listing_sku（半自动场景下 adapter 产出不带价格）
        for item in normalized:
            listing_sku = (
                await session.execute(
                    select(ListingSku).where(
                        ListingSku.listing_product_id == listing.id,
                        ListingSku.shop_sku_code == item["shop_sku_code"],
                    )
                )
            ).scalars().first()
            if listing_sku is not None:
                listing_sku.sale_price_cents = int(item.get("sale_price_cents") or 0) or None

        task.status = PublishStatus.PUBLISH_SUCCESS.value
        task.published_at = utc_now()
        await session.flush()

        mapping_ids = [
            int(row[0])
            for row in (
                await session.execute(
                    select(SkuMapping.id).where(
                        SkuMapping.platform == task.platform,
                        SkuMapping.shop_id == task.shop_id,
                        SkuMapping.shop_item_id == shop_item_id,
                        SkuMapping.is_deleted.is_(False),
                    )
                )
            ).all()
        ]

        await AuditService.write(
            session,
            action_type=AuditActionType.PUBLISH.value,
            object_type=AuditObjectType.PUBLISH_TASK.value,
            object_id=task.id,
            operator=operator,
            new_value={"shop_item_id": shop_item_id, "mappings": len(mapping_ids)},
            trace_id=get_trace_id(),
            remark=f"半自动回填商品 ID {shop_item_id}，建立 {len(mapping_ids)} 条映射",
        )
        await session.flush()

        # ★ 回填即触发一次倒挂重算（售价刚写入，必须立即检出倒挂）
        await PublishService._recompute_after_fill_back(session, listing_product_id=int(listing.id))

        return {
            "publish_task_id": int(task.id),
            "mapping_ids": mapping_ids,
            "listing_product_id": int(listing.id),
        }

    @staticmethod
    async def _recompute_after_fill_back(session: Any, *, listing_product_id: int) -> int:
        """回填后重算 `cost_underwater`（不抛异常）。"""
        try:
            conflicts = await MappingValidator.recompute_cost_underwater(
                session, listing_product_id=listing_product_id
            )
            return len(conflicts)
        except Exception as exc:  # noqa: BLE001  重算失败不得阻断回填
            logger.warning("fill_back_recompute_failed", listing_product_id=listing_product_id, error=str(exc))
            return 0

    # ==================================================================
    #  半自动素材包 / 表单数据
    # ==================================================================

    @staticmethod
    async def build_manual_package(session: Any, task_id: int) -> Any:
        """生成半自动素材包（ZIP）。"""
        task = await PublishService.get_task(session, task_id)
        if task.listing_mode != ListingMode.MANUAL.value:
            raise BusinessError(
                f"仅半自动模式有素材包（当前 {task.listing_mode}）", code=ErrorCode.PARAM_ERROR
            )
        payload = await PublishService._build_payload(session, task)
        adapter = ManualListingAdapter(session=session, platform=task.platform)
        result = await adapter.invoke("build_manual_package", payload=payload)
        if result is None or not result.ok or result.data is None:
            raise BusinessError(
                result.message if result else "素材包生成失败", code=ErrorCode.PUBLISH_PLATFORM_FAILED
            )
        task.package_path = result.data.package_path
        await session.flush()
        return result.data

    @staticmethod
    async def form_data(session: Any, task_id: int) -> dict[str, Any]:
        """半自动预填表单数据（可直接复制到平台后台）。"""
        task = await PublishService.get_task(session, task_id)
        payload = await PublishService._build_payload(session, task)
        adapter = ManualListingAdapter(session=session, platform=task.platform)
        form = adapter.build_form_data(payload)
        import json as _json

        return {
            **form,
            "copy_text": _json.dumps(form, ensure_ascii=False, indent=2),
        }

    # ==================================================================
    #  重试 / 取消
    # ==================================================================

    @staticmethod
    async def retry(session: Any, task_id: int, *, operator: str = "system") -> PublishTask:
        """重试：状态回到 `pending_precheck`。"""
        task = await PublishService.get_task(session, task_id)
        if task.status not in {
            PublishStatus.PRECHECK_FAILED.value,
            PublishStatus.VALIDATE_FAILED.value,
            PublishStatus.PUBLISH_FAILED.value,
        }:
            raise StateConflictError(f"任务状态 {task.status} 不允许重试")
        task.transition_to(PublishStatus.PENDING_PRECHECK.value)
        task.platform_error_code = None
        task.platform_error_msg = None
        await session.flush()
        return await PublishService.execute(session, task_id, operator=operator)

    @staticmethod
    async def cancel(session: Any, task_id: int, *, operator: str = "system") -> PublishTask:
        """取消上架任务。"""
        task = await PublishService.get_task(session, task_id)
        try:
            task.transition_to(PublishStatus.CANCELLED.value)
        except ValueError as exc:
            raise StateConflictError(str(exc)) from exc
        await session.flush()
        await AuditService.write(
            session,
            action_type=AuditActionType.PUBLISH.value,
            object_type=AuditObjectType.PUBLISH_TASK.value,
            object_id=task.id,
            operator=operator,
            new_value={"status": "cancelled"},
            trace_id=get_trace_id(),
            remark=f"取消上架任务 {task.id}",
        )
        return task

    @staticmethod
    def task_summary(task: PublishTask) -> dict[str, Any]:
        """任务摘要（异步任务回调用）。"""
        return {
            "publish_task_id": int(task.id),
            "status": task.status,
            "shop_item_id": task.shop_item_id,
            "finished_at": iso_utc(utc_now()),
        }
