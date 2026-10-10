"""AI 重构任务服务（§5.5.5）。

★ 用户决策 ②：AI = **WorkBuddy 文件桥 + HTTP API** 双通道，`ai.client` 默认 `file_bridge`。
   文件桥超时 → 抛 `AiTimeoutError`（可重试），提示指向 `data/ai_queue/<task_id>/prompt.md`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select

from app.adapters.ai.base import (
    AiImagePrompt,
    AiInputPrompt,
    AiRedrawResult,
    AiReworkResult,
    AiTaskContext,
    AiTimeoutError,
    AiTitleCandidate,
    AiVideoScriptResult,
)
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

# ★★ 任务类型分派表：`ai_task.task_type` → AI 客户端方法名 ★★
#   ★ 为什么队列 `TaskType` 仍然**只有 `ai_rework` 一种**（不是遗漏，是刻意取舍）：
#       `TaskType` 回答"这条异步任务由哪个 handler 执行"，
#       `AiTaskType`（`ai_task.task_type`）回答"这条 AI 任务要为使用者产出什么"。
#       三种新能力的执行骨架完全一样（① 短事务读参数 → ② 无事务等产出 → ③ 短事务落库），
#       再复制三个 handler 只是把同一段代码抄四遍、并把"要产出什么"这个信息
#       在两张表上各存一份（**两个真相源**，改一处忘一处就会对不上）。
#       因此队列类型保持 `ai_rework` 不变，真正的种类只记在 `ai_task.task_type` 上，
#       由 `run_task()` 在这里按类型分派到对应能力。
#
#   ★ 本表是**合法取值集合**（`create_tasks()` 校验、`run_task()` 报错文案都用它）；
#     真正的分派在 `run_task()` 里用 `if/elif` 写死、不走 `getattr` 动态取 ——
#     原因见那里的注释：事务纪律 A 的结构性护栏必须能在源码里看见真实的等待点。
AI_TASK_TYPE_DISPATCH: dict[str, str] = {
    AiTaskType.AI_REWORK.value: "rework_images",
    AiTaskType.IMAGE_REDRAW.value: "redraw_images",
    AiTaskType.TITLE_SUGGEST.value: "suggest_titles",
    AiTaskType.VIDEO_SCRIPT.value: "suggest_video_script",
}


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
    # ★ 创建时固化的 AI 客户端名（README 第九节第 17 条）：
    #   ② 阶段（无事务等待期）必须**凭它**取客户端，绝不能再去读当前配置 ——
    #   否则"切换 ai.client 后在途任务仍按原客户端跑完"这条保证就无从落地。
    ai_client_name: str = ""
    task_type: str = ""


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
        task_type: str | None = None,
        input_prompt: AiInputPrompt | None = None,
        global_prompt: str | None = None,
        image_prompts: list[AiImagePrompt] | None = None,
        title_prompt: str | None = None,
        video_script_prompt: str | None = None,
        operator: str = "system",
    ) -> list[AiTask]:
        """批量创建 AI 任务（状态 `queued`）。

        ★ 新增入参**全部可空**：老调用方不传时行为与改动前完全一致。

        Args:
            task_type: AI 任务类型（`AiTaskType`）；None → `ai_rework`（存量口径）。
            input_prompt: 完整提示词契约（传了就以它为准）。
            global_prompt / image_prompts / title_prompt / video_script_prompt:
                分字段入参；`input_prompt` 为空时由这些字段组装成 `AiInputPrompt`。
        """
        if not source_product_ids:
            raise BusinessError("source_product_ids 不能为空", code=ErrorCode.PARAM_ERROR)
        if task_type is not None and task_type not in AI_TASK_TYPE_DISPATCH:
            raise BusinessError(
                f"不支持的 AI 任务类型：{task_type}，可用：{', '.join(sorted(AI_TASK_TYPE_DISPATCH))}",
                code=ErrorCode.PARAM_ERROR,
            )

        config = await AiTaskService.concurrency_config(session)
        items = rework_items or list(DEFAULT_REWORK_ITEMS)
        prompt_payload = input_prompt or AiInputPrompt(
            global_prompt=str(global_prompt or ""),
            images=list(image_prompts or []),
            title_prompt=str(title_prompt or ""),
            video_script_prompt=str(video_script_prompt or ""),
        )
        # ★ 一个提示词都没给时**不落空壳** `{}`：NULL 的语义是"使用者没填过提示词"，
        #   而 `{}` 看着像"填过但都是空串"，两者在查问题时不是一回事（同 0005 迁移的口径）。
        has_prompt = any(
            (
                prompt_payload.global_prompt,
                prompt_payload.title_prompt,
                prompt_payload.video_script_prompt,
                [p for p in prompt_payload.images if str(p.prompt or "").strip()],
            )
        )
        input_prompt_json = prompt_payload.to_dict() if has_prompt else None
        resolved_task_type = task_type or AiTaskType.AI_REWORK.value
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
                task_type=resolved_task_type,
                ai_client=client_name,
                input_prompt_json=input_prompt_json,
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

        # ★★ 按任务行上**固化的** `ai_client` 取客户端，而不是读当前配置 ★★
        #   这正好补上 README 第九节第 17 条留下的缺口（原话：该要求"在数据结构上无法实现"，
        #   0004 补上列之后剩下"还没把值用起来"）：切换 ai.client 后，在途任务仍按原通道跑完。
        #
        # ★ 用 `instantiate()` 而非 `create()`：② 阶段按事务纪律 A **不得持有会话**，
        #   而 `create()` 在 name 为空时会自己开会话读 SystemSetting。
        # ★ 客户端不可用时**明确报错**（instantiate 抛 1099），**绝不静默回落到默认客户端** ——
        #   静默回落会让上面这条保证形同虚设：看着在跑，其实已经换了通道。
        try:
            # ★ 放进 try：客户端取不到时也要走 `mark_failed()`，
            #   否则任务会永远停在 running（"客户端没了"是永久性故障，不会自愈）。
            client = AiClientFactory.instantiate(plan.ai_client_name)

            # ★★ ② 阶段：按 `ai_task.task_type` 分派到对应能力 ★★
            #   ★ 四个 `await` **故意**都留在 `run_task()` 里，不抽进 helper：
            #     `tests/test_task_transaction_hygiene.py` 用 AST 断言「等产出的调用
            #     不得位于任何 `async with` 会话块内」（事务纪律 A 的**结构性护栏**）。
            #     把 await 藏进 helper 会让这条护栏失效 —— 它只读 `run_task` 的源码，
            #     看不见 helper 里的等待点。宁可这里长一点，也要让护栏看得见真实的等待。
            task_type = str(plan.task_type or AiTaskType.AI_REWORK.value)
            title_candidates: list[AiTitleCandidate] | None = None
            video_script: AiVideoScriptResult | None = None
            if task_type == AiTaskType.AI_REWORK.value:
                # ★ 存量路径：**行为一字不改**（回归的核心），仍是一体产出图 + 标题 + 属性。
                result: AiReworkResult = await client.rework_images(plan.context)
            elif task_type == AiTaskType.IMAGE_REDRAW.value:
                redraw: AiRedrawResult = await client.redraw_images(plan.context)
                result = AiTaskService.adapt_redraw(redraw)
            elif task_type == AiTaskType.TITLE_SUGGEST.value:
                suggest = await client.suggest_titles(plan.context)
                result = AiTaskService.adapt_title_suggest(suggest)
                # ★ 候选数组**原样**交给落库（含每条的风格 / 依据 / 分数），
                #   否则前端只能展示"当前那条"，使用者无从比较。
                title_candidates = list(suggest.candidates)
            elif task_type == AiTaskType.VIDEO_SCRIPT.value:
                script: AiVideoScriptResult = await client.suggest_video_script(plan.context)
                result = AiTaskService.adapt_video_script(script)
                video_script = script
            else:
                raise BusinessError(
                    f"未知的 AI 任务类型：{task_type}（可用：{', '.join(sorted(AI_TASK_TYPE_DISPATCH))}）",
                    code=ErrorCode.PARAM_ERROR,
                )
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
            title_candidates=title_candidates,
            video_script=video_script,
        )
        return await AiTaskService.load_task(plan.ai_task_id)

    # ==================================================================
    #  新能力产出 → 既有落库口径的适配器（★ 只做结构转换，不含任何 IO）
    # ==================================================================
    #
    # ★★ 为什么统一降级成 `AiReworkResult`（而不是给每种能力写一套落库）★★
    #     `persist_result()` 是既有的、被存量 `ai_rework` 链路依赖的落库实现，
    #     三种新能力**复用它**即可（图片走 `images`，标题走 `title_result`），
    #     新造一套落库等于把同一段"落 asset / 落结果 / 转 pending_review / 写审计"
    #     抄两遍，且两边行为迟早分叉。

    @staticmethod
    def adapt_redraw(redraw: AiRedrawResult) -> AiReworkResult:
        """图片重绘产出 → 落库口径（**只灌图片**，不掺标题 / 属性）。"""
        return AiReworkResult(
            images=list(redraw.images),
            model_name=redraw.model_name,
            prompt_snapshot=redraw.prompt_snapshot,
            elapsed_ms=redraw.elapsed_ms,
            raw=dict(redraw.raw or {}),
        )

    @staticmethod
    def adapt_title_suggest(suggest: Any) -> AiReworkResult:
        """标题建议产出 → 落库口径（`to_title_result()` 把最高分那条降级成老口径单条）。

        ★ `output_title` 先落到"首选"：人工挑完由 `select_title_candidate()` 覆盖，
          这样即便使用者一条都不挑，下游发布链路也拿得到一条像样的标题。
        """
        return AiReworkResult(
            title_result=suggest.to_title_result(),
            model_name=suggest.model_name,
            prompt_snapshot=suggest.prompt_snapshot,
            elapsed_ms=suggest.elapsed_ms,
            raw=dict(suggest.raw or {}),
        )

    @staticmethod
    def adapt_video_script(script: AiVideoScriptResult) -> AiReworkResult:
        """视频脚本产出 → 落库口径（无图、无标题改写，只带模型名与提示词快照留痕）。

        ★ 不碰 `output_title` 的既有回落逻辑（货源原标题）—— 改回落就会动到存量口径。
        """
        return AiReworkResult(
            model_name=script.model_name,
            prompt_snapshot=script.prompt_snapshot,
            elapsed_ms=script.elapsed_ms,
            raw=dict(script.raw or {}),
        )

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

            # ★ 把使用者输入的提示词灌进上下文：全局 / 逐图 / 标题 / 视频脚本四类。
            #   `AiInputPrompt.from_dict()` 是防御性的：None / 空 dict / 乱码结构
            #   一律退化为**空契约**，绝不让一句脏 JSON 把整个任务打崩。
            input_prompt = AiInputPrompt.from_dict(task.input_prompt_json)

            context = AiTaskContext(
                task_id=str(task.id),
                target_platform=task.target_platform,
                source_product_id=int(task.source_product_id),
                original_title=(product.title if product else ""),
                rework_items=list(task.rework_items_json or DEFAULT_REWORK_ITEMS),
                source_image_paths=image_paths,
                selling_points=[],
                attributes_json=dict(product.params_json or {}) if product else {},
                image_prompts=list(input_prompt.image_prompts),
                global_prompt=input_prompt.global_prompt,
                title_prompt=input_prompt.title_prompt,
                video_script_prompt=input_prompt.video_script_prompt,
                task_type=str(task.task_type or AiTaskType.AI_REWORK.value),
            )
            plan = AiTaskRunPlan(
                ai_task_id=int(task.id),
                source_product_id=int(task.source_product_id),
                original_title=(product.title if product else ""),
                context=context,
                ai_client_name=str(task.ai_client or ""),
                task_type=context.task_type,
            )
            # ★ 立即提交：把写事务的持有时长压到"读参数"这一小段
            await session.commit()

        logger.info(
            "ai_task_prepared",
            ai_task_id=plan.ai_task_id,
            image_count=len(plan.context.source_image_paths),
            image_prompt_count=len(plan.context.image_prompts),
            task_type=plan.task_type,
            ai_client=plan.ai_client_name,
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
        title_candidates: list[AiTitleCandidate] | None = None,
        video_script: AiVideoScriptResult | None = None,
    ) -> int:
        """③ 短事务：落素材 → 落结果 → 转 `pending_review` → 提交。

        Args:
            task_id: AI 任务 ID。
            result: `AiReworkResult`（三种新能力由 `invoke_capability()` 统一降级成它）。
            client_name: AI 客户端名（审计留痕）。
            operator: 操作人。
            title_candidates: 标题候选数组（仅 `title_suggest`），落 `output_title_candidates_json`。
            video_script: 视频脚本（仅 `video_script`），落 `output_video_script_json`。

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
                # ★ 主图 / 详情图的判定：**产出方标了 `image_role` 就以它为准**，
                #   没标（存量 `ai_rework` 产出）才回落到"第一张是主图"的老规则
                #   ⇒ 存量路径 asset_type 一个字都不变，新能力又能按角色可靠区分。
                role = str(image.image_role or "") or (
                    "main_image" if index == 0 else "detail_image"
                )
                # ★ 序号：产出方显式的 `image.index` 优先；老产出该字段恒为 0
                #   （它是后加的字段，存量产出方不填），此时回落成产出列表中的位置。
                seq = int(image.index or 0) or index
                asset = Asset(
                    source_product_id=task.source_product_id,
                    asset_type=(
                        AssetType.MAIN_IMAGE.value if role == "main_image" else AssetType.DETAIL_IMAGE.value
                    ),
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
                    # ★ 把"这张是主图还是详情图 / 第几张 / 按哪句提示词画的"一起落到 `tags_json`：
                    #   ① 使用者要"重绘后的图按主图 / 详情页分组展示"，没有这两项就只能靠文件名猜；
                    #   ② 逐图提示词是否真的生效（`prompt_source=per_image`）要能复盘；
                    #   ★ 命名规则（中文名 `主图01.jpg` 还是内部名 `main_00.jpg`）**尚未最终确认**，
                    #     故本次**不动文件名**；把角色与序号落进记录，将来换命名规则时
                    #     只改文件命名那一处，不必重做整条落库管线。
                    tags_json={
                        "tags": [role],
                        "image_role": role,
                        "index": int(seq),
                        "prompt": str(image.prompt or ""),
                        "prompt_source": str(image.prompt_source or ""),
                        "source_path": str(image.source_path or ""),
                    },
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
                # ★ 标题候选数组（仅 `title_suggest` 有值；其余能力留 None 表示"没走过多候选口径"）。
                #   ★ 为什么存 `to_dict()` 而不是原始对象：这一列要能跨产次回显，
                #     `char_count` 等派生字段一并固化，前端不必再算一遍。
                output_title_candidates_json=(
                    [c.to_dict() for c in title_candidates] if title_candidates else None
                ),
                # ★ 视频脚本（仅 `video_script` 有值），`text` 是把脚本渲染好的纯文本，
                #   前端/导出可直接用，不必再按 scenes 拼一遍。
                output_video_script_json=(
                    {**video_script.to_dict(), "text": video_script.to_text()} if video_script else None
                ),
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
    async def select_title_candidate(
        session: Any,
        task_id: int,
        *,
        index: int | None = None,
        title: str | None = None,
        note: str = "",
        operator: str = "system",
    ) -> AiTaskResult:
        """★ 选定某条标题候选 —— 让「AI 出候选 → 人工挑 → 上架用那条」闭环。

        ★★ 为什么必须有这一步（而不是让使用者自己改标题）★★
            ① `output_title` 是**下游发布链路读标题的唯一字段**
               （`PublishService._build_payload()` / `_persist_listing()` 都从它取），
               光把候选展示在页面上、却写不回 `output_title`，
               "选了"与"上架用哪条"就是两回事 —— 使用者以为选了，上架还是用首选那条；
            ② 选定动作要**可追溯**：写"选了第几条 / 谁 / 什么时候"，
               否则事后查"这个链接当时为什么用这个标题"无从回答。

        Args:
            index: 候选下标（0 起）；与 `title` 二选一，**两个都给时以 index 为准**。
            title: 候选标题原文（候选列表被前端重排过时的兜底定位方式）。

        Returns:
            更新后的 `AiTaskResult`（`output_title` 已是选中的那条）。

        Raises:
            NotFoundError: 任务或产出不存在。
            BusinessError: 没有候选 / 定位不到候选 / index 越界。
        """
        task = await AiTaskService.get_task(session, task_id)
        result = await AiTaskService.latest_result(session, task_id)
        if result is None:
            raise NotFoundError(f"AI 任务 {task_id} 尚无可选择的产出")

        raw = result.output_title_candidates_json
        candidates = [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []
        if not candidates:
            raise BusinessError(
                f"AI 任务 {task_id} 没有标题候选可选（该任务类型可能不是 title_suggest）",
                code=ErrorCode.PARAM_ERROR,
            )

        picked_index: int | None = None
        picked: dict[str, Any] | None = None
        if index is not None:
            if int(index) >= len(candidates):
                raise BusinessError(
                    f"标题候选下标 {index} 越界（共 {len(candidates)} 条）", code=ErrorCode.PARAM_ERROR
                )
            picked_index = int(index)
            picked = candidates[picked_index]
        elif title:
            for position, candidate in enumerate(candidates):
                if str(candidate.get("title", "")) == str(title):
                    picked_index = position
                    picked = candidate
                    break
            if picked is None:
                raise BusinessError(f"未在候选中找到标题：{title}", code=ErrorCode.PARAM_ERROR)
        else:
            raise BusinessError("index 与 title 至少提供一个", code=ErrorCode.PARAM_ERROR)

        picked_title = str(picked.get("title", "") or "")
        # ★ 写回下游发布链路真正读的三个字段，`_build_payload()` 无需任何改动即可拿到选中那条。
        result.output_title = picked_title
        points = [str(s) for s in (picked.get("selling_points") or [])]
        if points:
            result.output_selling_points = "\n".join(points)
        result.banned_words_json = [
            {"word": str(w), "type": "banned", "suggestion": "请替换"}
            for w in (picked.get("banned_words") or [])
        ]
        # ★ 选中留痕：选了第几条 / 什么时候 / 谁
        result.selected_title_index = picked_index
        result.selected_title_at = utc_now()
        result.selected_title_by = operator
        if note:
            result.review_note = note
        await session.flush()

        await AuditService.write(
            session,
            action_type=AuditActionType.PUBLISH.value,
            object_type=AuditObjectType.SYSTEM_SETTING.value,
            object_id=task.id,
            operator=operator,
            new_value={
                "selected_title_index": picked_index,
                "selected_title": picked_title,
                "candidate_count": len(candidates),
                "note": note,
            },
            trace_id=get_trace_id(),
            remark=f"选定标题候选 #{picked_index}：{picked_title}",
        )
        await session.flush()
        logger.info(
            "ai_title_candidate_selected",
            ai_task_id=int(task.id),
            index=picked_index,
            title=picked_title,
            operator=operator,
        )
        return result

    @staticmethod
    def review_summary(result: AiTaskResult) -> dict[str, Any]:
        """审核摘要（任务回调用）。"""
        return {
            "review_status": result.review_status,
            "reviewed_at": iso_utc(result.reviewed_at) if result.reviewed_at else None,
        }
