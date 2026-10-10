"""AI 重构任务服务（§5.5.5）。

★ 用户决策 ②：AI = **WorkBuddy 文件桥 + HTTP API** 双通道，`ai.client` 默认 `file_bridge`。
   文件桥超时 → 抛 `AiTimeoutError`（可重试），提示指向 `data/ai_queue/<task_id>/prompt.md`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select

from app.adapters.ai.base import AiTaskContext, AiTimeoutError
from app.adapters.ai.factory import AiClientFactory, resolve_ai_client_name
from app.core.errors import BusinessError, ErrorCode, NotFoundError, StateConflictError
from app.core.logging import get_logger, get_trace_id
from app.models.asset import AiTask, AiTaskResult, Asset
from app.models.enums import (
    AiTaskStatus,
    AiTaskType,
    AssetOrigin,
    AssetType,
    AuditActionType,
    AuditObjectType,
    ReviewStatus,
    SettingKey,
)
from app.models.source import SourceProduct
from app.models.system import SystemSetting
from app.services.audit_service import AuditService
from app.utils.kit import content_hash_file, iso_utc, utc_now

logger = get_logger(__name__)

__all__ = ["AiTaskRunPlan", "AiTaskService"]

# 默认重构项
DEFAULT_REWORK_ITEMS = ["main_image", "detail_image", "title", "attribute"]


@dataclass
class AiTaskRunPlan:
    """★ 三段式执行的**纯数据快照**（① 阶段产出，② / ③ 阶段消费）。

    ★ 为什么必须脱离 ORM 会话：② 阶段要等待外部产出几十分钟，
      期间若还挂着一个 session / 未提交事务，SQLite 单写者会让全系统写能力归零。
    """

    ai_task_id: int = 0
    source_product_id: int = 0
    original_title: str = ""
    context: AiTaskContext = field(default_factory=lambda: AiTaskContext(task_id=""))


class AiTaskService:
    """AI 重构任务：创建 / 执行 / 重试 / 取消 / 审核。"""

    # ==================================================================
    #  配置
    # ==================================================================

    @staticmethod
    async def concurrency_config(session: Any) -> dict[str, int]:
        """读取并发与重试配置（`ai.max_concurrency` / `ai.max_retry`）。"""
        keys = [SettingKey.AI_MAX_CONCURRENCY.value, SettingKey.AI_MAX_RETRY.value]
        rows = (
            (await session.execute(select(SystemSetting).where(SystemSetting.setting_key.in_(keys))))
            .scalars()
            .all()
        )
        found = {row.setting_key: row.typed_value() for row in rows}
        return {
            "max_concurrency": int(found.get(SettingKey.AI_MAX_CONCURRENCY.value, 5) or 5),
            "max_retry": int(found.get(SettingKey.AI_MAX_RETRY.value, 3) or 3),
        }

    @staticmethod
    async def update_concurrency_config(
        session: Any,
        *,
        max_concurrency: int | None = None,
        max_retry: int | None = None,
        operator: str = "system",
    ) -> dict[str, int]:
        """更新并发与重试配置（管理员）。"""
        pairs = {
            SettingKey.AI_MAX_CONCURRENCY.value: max_concurrency,
            SettingKey.AI_MAX_RETRY.value: max_retry,
        }
        for key, value in pairs.items():
            if value is None:
                continue
            row = (
                await session.execute(select(SystemSetting).where(SystemSetting.setting_key == key))
            ).scalars().first()
            if row is None:
                session.add(
                    SystemSetting(
                        setting_key=key,
                        setting_value=str(int(value)),
                        value_type="int",
                        description="AI 并发/重试配置",
                        updated_by=operator,
                    )
                )
            else:
                row.setting_value = str(int(value))
                row.updated_by = operator
        await session.flush()
        await AuditService.write(
            session,
            action_type=AuditActionType.MAPPING_CHANGE.value,
            object_type=AuditObjectType.SYSTEM_SETTING.value,
            object_id="ai.config",
            operator=operator,
            new_value={"max_concurrency": max_concurrency, "max_retry": max_retry},
            trace_id=get_trace_id(),
            remark="更新 AI 并发与重试配置",
        )
        return await AiTaskService.concurrency_config(session)

    # ==================================================================
    #  创建
    # ==================================================================

    @staticmethod
    async def create_tasks(
        session: Any,
        *,
        source_product_ids: list[int],
        target_platform: str,
        rework_items: list[str] | None = None,
        template_version: str | None = None,
        operator: str = "system",
    ) -> list[AiTask]:
        """批量创建 AI 重构任务（状态 `queued`）。"""
        if not source_product_ids:
            raise BusinessError("source_product_ids 不能为空", code=ErrorCode.PARAM_ERROR)

        config = await AiTaskService.concurrency_config(session)
        items = rework_items or list(DEFAULT_REWORK_ITEMS)
        # ★ README 第九节第 17 条：AI 客户端必须在**创建时固化**到行上，
        #   否则"切换 ai.client 后在途任务仍按原客户端跑完"这条口径在数据结构上无从实现
        #   （0004 之前该列根本不存在）。这里读的是同一套解析逻辑（含 SystemSetting 覆盖）。
        client_name = await resolve_ai_client_name(session)
        tasks: list[AiTask] = []
        for product_id in source_product_ids:
            product = (
                await session.execute(
                    select(SourceProduct).where(
                        SourceProduct.id == int(product_id), SourceProduct.is_deleted.is_(False)
                    )
                )
            ).scalars().first()
            if product is None:
                logger.warning("ai_task_source_product_missing", source_product_id=product_id)
                continue
            task = AiTask(
                source_product_id=int(product_id),
                target_platform=target_platform,
                task_type=AiTaskType.AI_REWORK.value,
                ai_client=client_name,
                rework_items_json=list(items),
                template_version=template_version,
                status=AiTaskStatus.QUEUED.value,
                max_retry=config["max_retry"],
                created_by=operator,
            )
            session.add(task)
            tasks.append(task)
        await session.flush()
        return tasks

    # ==================================================================
    #  执行
    # ==================================================================

    @staticmethod
    async def run_task(task_id: int, *, operator: str = "system") -> AiTask:
        """★ 执行一次 AI 重构任务（**三段式**，事务纪律 A 的落地样板）。

        ★ ★ ★ 事务纪律 A（本项目硬规则）★ ★ ★
        等待外部产出（WorkBuddy 文件桥，最长可达 `ai.poll_timeout_sec`）期间
        **绝不持有任何数据库写事务** —— SQLite 是单写者，一个任务持锁 30 分钟
        会让**全系统写能力归零**（史实：ai_rework 任务卡在 running，
        所有写接口集体 500，而只读接口因命中缓存仍返回 200，排查方向被带偏两轮）。

        三段式：
            ① 短事务：读参数 → 标记 `running` → 组装上下文 → **立即提交**（释放写锁）；
            ② 无事务：等待 AI 产出（可能几十分钟，★ 期间不持有任何 session）；
            ③ 短事务：落素材 / 落结果 / 转 `pending_review` → 提交。

        Raises:
            AiTimeoutError: 等待产出超时（**不吞掉**，交给 TaskRunner 按 max_retry 重试）。
        """
        plan = await AiTaskService.prepare(task_id, operator=operator)

        # ★ session 是 keyword-only 形参：必须写 create(session=session)，
        #   写成 create(session) 会把会话绑到 name 上（见 AiClientFactory.create 的守卫）。
        client = await AiClientFactory.create()

        try:
            result = await client.rework_images(plan.context)
        except Exception as exc:  # noqa: BLE001  记录失败后原样抛出（AiTimeoutError 保持可重试语义）
            await AiTaskService.mark_failed(plan.ai_task_id, exc)
            logger.warning(
                "ai_task_failed",
                ai_task_id=plan.ai_task_id,
                error=str(exc),
                retryable=isinstance(exc, AiTimeoutError),
            )
            raise

        await AiTaskService.persist_result(
            plan.ai_task_id,
            result,
            client_name=client.client_name,
            operator=operator,
        )
        return await AiTaskService.load_task(plan.ai_task_id)

    @staticmethod
    async def prepare(task_id: int, *, operator: str = "system") -> AiTaskRunPlan:
        """① 短事务：读参数 → 标记 `running` → 组装上下文 → **提交并释放连接**。

        Args:
            task_id: AI 任务 ID。
            operator: 操作人（审计用）。

        Returns:
            `AiTaskRunPlan`：后续阶段所需的**纯数据快照**（不含 ORM 会话）。

        Raises:
            NotFoundError: 任务不存在。
            StateConflictError: 当前状态不可执行。
        """
        from app.core.database import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            task = (
                await session.execute(select(AiTask).where(AiTask.id == int(task_id)))
            ).scalars().first()
            if task is None:
                raise NotFoundError(f"AI 任务 {task_id} 不存在")
            if task.status not in {AiTaskStatus.QUEUED.value, AiTaskStatus.FAILED.value}:
                raise StateConflictError(f"任务当前状态 {task.status} 不可执行")

            task.status = AiTaskStatus.RUNNING.value
            task.error_code = None
            task.error_message = None

            product = (
                await session.execute(select(SourceProduct).where(SourceProduct.id == task.source_product_id))
            ).scalars().first()
            image_paths = await AiTaskService.collect_source_image_paths(session, int(task.source_product_id))

            context = AiTaskContext(
                task_id=str(task.id),
                target_platform=task.target_platform,
                source_product_id=int(task.source_product_id),
                original_title=(product.title if product else ""),
                rework_items=list(task.rework_items_json or DEFAULT_REWORK_ITEMS),
                source_image_paths=image_paths,
                selling_points=[],
                attributes_json=dict(product.params_json or {}) if product else {},
            )
            plan = AiTaskRunPlan(
                ai_task_id=int(task.id),
                source_product_id=int(task.source_product_id),
                original_title=(product.title if product else ""),
                context=context,
            )
            # ★ 立即提交：把写事务的持有时长压到"读参数"这一小段
            await session.commit()

        logger.info(
            "ai_task_prepared",
            ai_task_id=plan.ai_task_id,
            image_count=len(plan.context.source_image_paths),
            operator=operator,
        )
        return plan

    @staticmethod
    async def persist_result(
        task_id: int,
        result: Any,
        *,
        client_name: str = "",
        operator: str = "system",
    ) -> int:
        """③ 短事务：落素材 → 落结果 → 转 `pending_review` → 提交。

        Args:
            task_id: AI 任务 ID。
            result: `AiReworkResult`。
            client_name: AI 客户端名（审计留痕）。
            operator: 操作人。

        Returns:
            `AiTaskResult.id`。
        """
        from pathlib import Path

        from app.core.database import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            task = (
                await session.execute(select(AiTask).where(AiTask.id == int(task_id)))
            ).scalars().first()
            if task is None:
                raise NotFoundError(f"AI 任务 {task_id} 不存在")
            product = (
                await session.execute(select(SourceProduct).where(SourceProduct.id == task.source_product_id))
            ).scalars().first()

            asset_ids: list[int] = []
            for index, image in enumerate(result.images or []):
                path = Path(image.local_path or "")
                if not path.exists():
                    continue
                digest = image.content_hash or ""
                if not digest:
                    try:
                        digest = content_hash_file(path)
                    except OSError:
                        digest = ""
                if not digest:
                    continue
                exists = (
                    await session.execute(select(Asset.id).where(Asset.content_hash == digest))
                ).scalar_one_or_none()
                if exists is not None:
                    asset_ids.append(int(exists))
                    continue
                asset = Asset(
                    source_product_id=task.source_product_id,
                    asset_type=AssetType.MAIN_IMAGE.value if index == 0 else AssetType.DETAIL_IMAGE.value,
                    origin=AssetOrigin.AI_REWORK.value,
                    storage_path=str(path),
                    content_hash=digest,
                    version=1,
                    lineage_id=f"ai-{task.id}-{index}",
                    is_current=True,
                    width=image.width,
                    height=image.height,
                    size_bytes=image.size_bytes or (path.stat().st_size if path.exists() else None),
                    ai_task_id=task.id,
                )
                session.add(asset)
                await session.flush()
                asset_ids.append(int(asset.id))

            title_result = result.title_result
            attribute_result = result.attribute_result
            ai_result = AiTaskResult(
                ai_task_id=task.id,
                output_asset_ids_json=asset_ids,
                output_title=(title_result.title if title_result else (product.title if product else "")),
                output_selling_points="\n".join(title_result.selling_points) if title_result else "",
                output_attributes_json=(attribute_result.attributes_json if attribute_result else {}),
                banned_words_json=[
                    {"word": w, "type": "banned", "suggestion": "请替换"}
                    for w in (title_result.banned_words if title_result else [])
                ],
                review_status=ReviewStatus.PENDING.value,
                model_name=result.model_name or client_name,
                prompt_snapshot=result.prompt_snapshot or "",
            )
            session.add(ai_result)
            task.status = AiTaskStatus.PENDING_REVIEW.value
            task.error_code = None
            task.error_message = None
            await session.flush()

            await AuditService.write(
                session,
                action_type=AuditActionType.PUBLISH.value,
                object_type=AuditObjectType.SYSTEM_SETTING.value,
                object_id=task.id,
                operator=operator,
                new_value={"client": client_name, "assets": len(asset_ids)},
                trace_id=get_trace_id(),
                remark=f"AI 重构完成，产出 {len(asset_ids)} 个素材，待审核",
            )
            await session.commit()
            logger.info("ai_task_result_persisted", ai_task_id=int(task.id), assets=len(asset_ids))
            return int(ai_result.id)

    @staticmethod
    async def mark_failed(task_id: int, error: BaseException) -> None:
        """独立短事务：把 AI 任务标记为失败（不依赖调用方事务是否存活）。

        Args:
            task_id: AI 任务 ID。
            error: 异常（`AiTimeoutError` 会额外记录可重试标记）。
        """
        from app.core.database import get_session_factory

        factory = get_session_factory()
        try:
            async with factory() as session:
                task = (
                    await session.execute(select(AiTask).where(AiTask.id == int(task_id)))
                ).scalars().first()
                if task is None:
                    return
                task.status = AiTaskStatus.FAILED.value
                task.error_code = type(error).__name__
                task.error_message = str(error)
                await session.commit()
        except Exception as exc:  # noqa: BLE001  失败回写不得覆盖原始异常
            logger.warning("ai_task_mark_failed_error", ai_task_id=task_id, error=str(exc))

    @staticmethod
    async def load_task(task_id: int) -> AiTask:
        """只读短事务：取回 AI 任务对象（供调用方读 `status` 等标量）。"""
        from app.core.database import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            task = (
                await session.execute(select(AiTask).where(AiTask.id == int(task_id)))
            ).scalars().first()
            if task is None:
                raise NotFoundError(f"AI 任务 {task_id} 不存在")
            return task

    @staticmethod
    def _source_image_paths(session: Any, source_product_id: int) -> list[str]:
        """取货源商品的本地原始图片路径（同步查询已在调用方 await 过的 ORM 对象上不安全，故走缓存）。

        说明：本方法只读取已经加载的对象属性，不发起新的 await。
        """
        return []

    @staticmethod
    async def collect_source_image_paths(session: Any, source_product_id: int) -> list[str]:
        """取货源商品原始素材的本地路径（异步，供任务上下文组装）。"""
        rows = (
            await session.execute(
                select(Asset)
                .where(
                    Asset.source_product_id == int(source_product_id),
                    Asset.is_deleted.is_(False),
                    Asset.origin == AssetOrigin.RAW.value,
                )
                .order_by(Asset.id)
            )
        ).scalars().all()
        return [str(a.storage_path) for a in rows if a.storage_path]

    # ==================================================================
    #  查询 / 重试 / 取消 / 审核
    # ==================================================================

    @staticmethod
    async def list_tasks(
        session: Any,
        *,
        status: str | None = None,
        target_platform: str | None = None,
        source_product_id: int | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[AiTask], dict[str, str], int]:
        """分页查询 AI 任务（附带货源商品标题）。"""
        stmt = select(AiTask)
        if status:
            stmt = stmt.where(AiTask.status == status)
        if target_platform:
            stmt = stmt.where(AiTask.target_platform == target_platform)
        if source_product_id is not None:
            stmt = stmt.where(AiTask.source_product_id == int(source_product_id))

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await session.execute(count_stmt)).scalar_one() or 0)
        rows = (
            (await session.execute(stmt.order_by(AiTask.id.desc()).offset((page - 1) * page_size).limit(page_size)))
            .scalars()
            .all()
        )
        product_ids = sorted({int(t.source_product_id) for t in rows})
        titles: dict[str, str] = {}
        if product_ids:
            products = (
                (await session.execute(select(SourceProduct).where(SourceProduct.id.in_(product_ids))))
                .scalars()
                .all()
            )
            titles = {str(p.id): p.title for p in products}
        return list(rows), titles, total

    @staticmethod
    async def get_task(session: Any, task_id: int) -> AiTask:
        """取 AI 任务（404）。"""
        task = (await session.execute(select(AiTask).where(AiTask.id == int(task_id)))).scalars().first()
        if task is None:
            raise NotFoundError(f"AI 任务 {task_id} 不存在")
        return task

    @staticmethod
    async def latest_result(session: Any, task_id: int) -> AiTaskResult | None:
        """取任务最近一条结果。"""
        stmt = (
            select(AiTaskResult)
            .where(AiTaskResult.ai_task_id == int(task_id))
            .order_by(AiTaskResult.id.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalars().first()

    @staticmethod
    async def list_result_assets(session: Any, result: AiTaskResult) -> list[Asset]:
        """取结果关联的素材。"""
        ids = [int(i) for i in (result.output_asset_ids_json or [])]
        if not ids:
            return []
        rows = (await session.execute(select(Asset).where(Asset.id.in_(ids)))).scalars().all()
        return list(rows)

    @staticmethod
    async def retry(session: Any, task_id: int, *, operator: str = "system") -> AiTask:
        """重试失败任务（状态回 `queued`，重试次数 +1）。"""
        task = await AiTaskService.get_task(session, task_id)
        if task.status not in {AiTaskStatus.FAILED.value, AiTaskStatus.CANCELLED.value}:
            raise StateConflictError(f"任务状态 {task.status} 不允许重试")
        if int(task.retry_count or 0) >= int(task.max_retry or 3):
            raise StateConflictError(f"已重试 {task.retry_count} 次，达到上限 {task.max_retry}")
        task.retry_count = int(task.retry_count or 0) + 1
        task.status = AiTaskStatus.QUEUED.value
        task.error_code = None
        task.error_message = None
        await session.flush()
        return task

    @staticmethod
    async def cancel(session: Any, task_id: int, *, operator: str = "system") -> AiTask:
        """取消任务。"""
        task = await AiTaskService.get_task(session, task_id)
        if task.status in {AiTaskStatus.APPROVED.value, AiTaskStatus.REJECTED.value}:
            raise StateConflictError(f"任务已审核完成（{task.status}），不可取消")
        task.status = AiTaskStatus.CANCELLED.value
        await session.flush()
        await AuditService.write(
            session,
            action_type=AuditActionType.PUBLISH.value,
            object_type=AuditObjectType.SYSTEM_SETTING.value,
            object_id=task.id,
            operator=operator,
            new_value={"status": "cancelled"},
            trace_id=get_trace_id(),
            remark=f"取消 AI 任务 {task.id}",
        )
        return task

    @staticmethod
    async def review(
        session: Any,
        task_id: int,
        *,
        action: str,
        note: str = "",
        edited: dict[str, Any] | None = None,
        operator: str = "system",
    ) -> AiTaskResult:
        """★ 审核重构结果（AIR-P0-03：只有 `approved` 才能被上架引用）。"""
        task = await AiTaskService.get_task(session, task_id)
        result = await AiTaskService.latest_result(session, task_id)
        if result is None:
            raise NotFoundError(f"AI 任务 {task_id} 尚无可审核的产出")

        if action == "approve":
            result.review_status = ReviewStatus.APPROVED.value
            task.status = AiTaskStatus.APPROVED.value
        elif action == "reject":
            result.review_status = ReviewStatus.REJECTED.value
            task.status = AiTaskStatus.REJECTED.value
        elif action == "edit":
            edited = edited or {}
            if edited.get("title"):
                result.output_title = str(edited["title"])
            if edited.get("selling_points"):
                result.output_selling_points = "\n".join(str(s) for s in edited["selling_points"])
            if edited.get("attributes_json"):
                result.output_attributes_json = dict(edited["attributes_json"])
            # ★ 编辑后仍需显式 approve；此处保持 PENDING，提示前端再提交一次
            if result.review_status != ReviewStatus.APPROVED.value:
                task.status = AiTaskStatus.PENDING_REVIEW.value
        else:
            raise BusinessError(f"不支持的审核动作：{action}", code=ErrorCode.PARAM_ERROR)

        result.review_note = note
        result.reviewed_by = operator
        result.reviewed_at = utc_now()
        await session.flush()

        await AuditService.write(
            session,
            action_type=AuditActionType.PUBLISH.value,
            object_type=AuditObjectType.SYSTEM_SETTING.value,
            object_id=task.id,
            operator=operator,
            new_value={"action": action, "review_status": result.review_status},
            trace_id=get_trace_id(),
            remark=f"审核 AI 产出：{action} {note}".strip(),
        )
        await session.flush()
        return result

    @staticmethod
    def review_summary(result: AiTaskResult) -> dict[str, Any]:
        """审核摘要（任务回调用）。"""
        return {
            "review_status": result.review_status,
            "reviewed_at": iso_utc(result.reviewed_at) if result.reviewed_at else None,
        }
