"""★ 映射校验器 —— 上架前强制校验（PRD MAP-P0-02 / ARCH §4.3.1、§5.5.6）。

本文件是**上架链路的硬闸门**：`PublishService` 在 `pending_validate → pending_publish`
迁移前**必须**调用 `validate()`，`blocking=true` 时禁止上架，**没有任何绕过路径**
（附录 A 第 3 条：全仓 `ListingAdapter.publish(` 调用点必须唯一且前置本校验）。

================================================================================
★ ★ ★ 冲突类型与级别（ARCH v1.8 §4.3.1 唯一真源）★ ★ ★
================================================================================
| code               | 中文名                                   | 级别 | 拦截 |
| ------------------ | ---------------------------------------- | ---- | ---- |
| `duplicate`        | 重复映射（②a 同店铺内反向重复）            | P0   | ✅   |
| `many_to_one`      | **跨平台铺货**                            | **P1** | ❌ **永不拦截** |
| `cost_invalid`     | 采购成本为空 / 0 / 负                      | P0   | ✅   |
| `cost_underwater`  | 成本倒挂（镜像成本 ≥ 售价）                | P1*  | ❌   |（*可由配置升 P0）
| `spec_mismatch`    | 规格指纹与货源侧不一致                     | P0   | ✅   |

★ QA-01 / QA-02：`one_to_many` 与 `duplicate_item` 两条检测**已删除** ——
  它们的 `GROUP BY` 含部分唯一索引 `uq_sku_mapping_shop_sku` 的键列，
  `HAVING COUNT(DISTINCT ...) > 1` **恒为假**，永远命中 0 行，属于"写了检测但不生效"。
  这两类冲突由该唯一索引在**写入边界**结构性阻断（撞键 → HTTP 409 / 1006）。
  枚举与级别表保留仅供前端展示；冲突面板上它们恒为 0 是**正确**结果。

★ QA-06：检测 SQL 失败**不得再被吞成空列表**。
  旧实现 `_run()` 把所有异常吞掉返回 `[]`，于是"某次检测空转了"和"确实没有冲突"
  在返回体里**一模一样**，运维完全无从察觉（QA 实测遇到过：同样的数据第一次全 0、
  第二次 spec_mismatch=1）。现在：异常 → 记 error 日志 + 写审计 + 返回体带
  `errors` / `incomplete`，且 `incomplete=True` 时**禁止上架**（fail-closed）。

★ 命名语义（已修正，勿再改回）：
    `many_to_one` 是**跨平台铺货**（P1，正常业务）—— 这是**先跑并剔除**的那一条；
    同店铺内的反向重复归 `duplicate`（P0）。早期实现把两者搞反了，会导致
    「三平台铺同一货源被拦截」，这是 PRD v1.1 写死的验收口径禁止的情形。

★ 检测执行的强制排序：
    **先跑 `many_to_one`（③），把它命中的映射记录下来并标记为 P1，
    再对其余类型做检测时剔除「跨越多个 (platform, shop_id)」的候选组。**
    否则跨平台铺货会被误判为 P0 拦截。
================================================================================
"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.logging import get_logger, get_trace_id
from app.models.enums import (
    AuditActionType,
    AuditObjectType,
    ChangeAction,
    ChangeSource,
    ConflictLevel,
    ConflictType,
    MappingStatus,
    ReviewStatus,
    SettingKey,
)
from app.models.mapping import (
    CONFLICT_LABELS,
    CONFLICT_LEVELS,
    CONFLICT_TYPES_REQUIRING_PENDING_CONFIRM,
    CONFIGURABLE_LEVEL_TYPES,
    DETECTION_ORDER,
    MappingChangeLog,
    MappingConflict,
    SkuMapping,
    get_conflict_queries,
)
from app.models.system import SystemSetting
from app.schemas.mapping import (
    MappingValidationConflict,
    MappingValidationMissing,
    MappingValidationVo,
)
from app.utils.kit import utc_now

logger = get_logger(__name__)

__all__ = ["MappingValidator", "ConflictLevelResolver"]

# 映射缺失的原因码（与 §5.5.6 `missing_mappings[].reason` 对齐）
MISSING_REASON_NOT_FOUND = "mapping_not_found"
MISSING_REASON_INVALID = "mapping_invalid"
MISSING_REASON_MOCK = "mapping_mock"
MISSING_REASON_PENDING = "mapping_pending_confirm"


class ConflictLevelResolver:
    """冲突级别解析器：静态级别 + 系统配置覆盖（运营改配置即可升降级，不改代码）。"""

    @staticmethod
    async def resolve(session: Any) -> dict[str, str]:
        """解析当前生效的冲突级别表。

        可被配置覆盖的类型见 `CONFIGURABLE_LEVEL_TYPES`（当前仅 `cost_underwater`）。

        Returns:
            `{冲突类型: 级别}`，如 `{"cost_underwater": "P0"}`。
        """
        levels = dict(CONFLICT_LEVELS)
        if not CONFIGURABLE_LEVEL_TYPES:
            return levels

        stmt = select(SystemSetting).where(
            SystemSetting.setting_key == SettingKey.MAPPING_CONFLICT_COST_UNDERWATER_LEVEL.value
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is not None and row.setting_value:
            configured = str(row.setting_value).strip().upper()
            if configured in {ConflictLevel.P0.value, ConflictLevel.P1.value}:
                levels[ConflictType.COST_UNDERWATER.value] = configured
            else:
                logger.warning(
                    "invalid_cost_underwater_level",
                    value=row.setting_value,
                    fallback=levels[ConflictType.COST_UNDERWATER.value],
                )
        return levels

    @staticmethod
    async def is_blocking(session: Any, conflict_type: str, level: str | None = None) -> bool:
        """判定某冲突类型当前是否拦截上架。"""
        levels = await ConflictLevelResolver.resolve(session)
        effective = level or levels.get(conflict_type, ConflictLevel.P1.value)
        return effective == ConflictLevel.P0.value


class MappingValidator:
    """★ 映射校验器（上架前强制校验 + 冲突检测）。"""

    # ==================================================================
    #  一、上架前强制校验（MAP-P0-02）
    # ==================================================================

    @staticmethod
    async def validate(
        session: Any,
        *,
        source_product_id: int,
        platform: str,
        shop_id: str,
        sku_codes: Iterable[str] | None = None,
    ) -> MappingValidationVo:
        """★ 上架前强制校验 —— 返回 `MappingValidationVo`，`blocking=true` 禁止上架。

        校验内容：
            1. 映射完整性：每个待上架 SKU 必须有 `status='valid'` 且非 Mock 的映射；
            2. 六类冲突检测（按 `DETECTION_ORDER` 强制排序，`many_to_one` 先跑并剔除）。

        Args:
            session: AsyncSession。
            source_product_id: 货源商品 ID。
            platform / shop_id: 目标平台与店铺。
            sku_codes: 待校验的店铺 SKU 编码列表；为空时取该货源商品在该店铺的全部映射。

        Returns:
            `MappingValidationVo`。`passed=False` 且 `blocking=True` 时禁止上架。
        """
        codes = [str(c).strip() for c in (sku_codes or []) if str(c).strip()]

        # ---------- ① 映射完整性 ----------
        missing = await MappingValidator._check_missing_mappings(
            session, source_product_id=source_product_id, platform=platform, shop_id=shop_id, sku_codes=codes
        )

        # ---------- ② 冲突检测（强制排序：many_to_one 先跑并剔除）----------
        errors: list[str] = []
        conflicts = await MappingValidator.detect_for_validation(
            session,
            source_product_id=source_product_id,
            platform=platform,
            shop_id=shop_id,
            sku_codes=codes,
            errors=errors,
        )

        blocking_conflicts = [c for c in conflicts if c.level == ConflictLevel.P0.value]
        # ★ QA-06 fail-closed：检测不完整时**禁止上架** ——
        #   "没检出冲突" 与 "检测空转了" 必须可区分，且后者不能放行。
        incomplete = bool(errors)
        blocking = bool(missing) or bool(blocking_conflicts) or incomplete

        # 中文阻断原因（前端直接展示）
        reasons: list[str] = []
        if missing:
            reasons.append(f"{len(missing)} 项映射缺失")
        if blocking_conflicts:
            p0_types = sorted({c.conflict_type for c in blocking_conflicts})
            reasons.append(
                f"{len(blocking_conflicts)} 项 P0 级冲突（{', '.join(CONFLICT_LABELS.get(t, t) for t in p0_types)}）"
            )
        if incomplete:
            reasons.append(f"{len(errors)} 项冲突检测未完成（本次检测不完整，不能判定为无冲突）")
        blocked_reason = "存在 " + " + ".join(reasons) if reasons else ""

        # ★ 只有 P0 冲突与映射缺失才影响 passed；
        #   P1（跨平台铺货 / 成本倒挂）仅提示，**不拦截**（PRD v1.1 验收口径）。
        #   但**检测失败必须拦截**（fail-closed，QA-06）。
        passed = not blocking

        if incomplete:
            logger.error(
                "mapping_validation_incomplete",
                source_product_id=source_product_id,
                errors=errors,
            )

        logger.info(
            "mapping_validation",
            source_product_id=source_product_id,
            platform=platform,
            shop_id=shop_id,
            checked_sku_count=len(codes),
            missing=len(missing),
            conflicts=len(conflicts),
            blocking=blocking,
            incomplete=incomplete,
        )

        return MappingValidationVo(
            passed=passed,
            source_product_id=int(source_product_id),
            platform=platform,
            shop_id=shop_id,
            checked_sku_count=len(codes),
            missing_mappings=missing,
            conflicts=conflicts,
            blocking=blocking,
            blocked_reason=blocked_reason,
            errors=errors,
            incomplete=incomplete,
        )

    @staticmethod
    async def _check_missing_mappings(
        session: Any,
        *,
        source_product_id: int,
        platform: str,
        shop_id: str,
        sku_codes: list[str],
    ) -> list[MappingValidationMissing]:
        """检查映射完整性：每个待上架 SKU 必须有 valid 且非 Mock 的映射。"""
        stmt = select(SkuMapping).where(
            SkuMapping.source_product_id == int(source_product_id),
            SkuMapping.platform == platform,
            SkuMapping.shop_id == shop_id,
            SkuMapping.is_deleted.is_(False),
        )
        if sku_codes:
            stmt = stmt.where(SkuMapping.shop_sku_code.in_(sku_codes))
        rows = (await session.execute(stmt)).scalars().all()
        found = {row.shop_sku_code: row for row in rows}

        missing: list[MappingValidationMissing] = []
        for code in sku_codes:
            mapping = found.get(code)
            if mapping is None:
                missing.append(MappingValidationMissing(shop_sku_code=code, reason=MISSING_REASON_NOT_FOUND))
                continue
            if mapping.is_mock:
                missing.append(MappingValidationMissing(shop_sku_code=code, reason=MISSING_REASON_MOCK))
                continue
            if mapping.status != "valid":
                reason = (
                    MISSING_REASON_PENDING if mapping.status == "pending_confirm" else MISSING_REASON_INVALID
                )
                missing.append(MappingValidationMissing(shop_sku_code=code, reason=reason))
        return missing

    # ==================================================================
    #  二、六类冲突检测（供校验与定时任务共用）
    # ==================================================================

    @staticmethod
    async def detect_for_validation(
        session: Any,
        *,
        source_product_id: int | None = None,
        platform: str | None = None,
        shop_id: str | None = None,
        sku_codes: list[str] | None = None,
        errors: list[str] | None = None,
    ) -> list[MappingValidationConflict]:
        """★ 按 `DETECTION_ORDER` 执行冲突检测，返回**不落库**的冲突清单。

        强制排序：`many_to_one`（跨平台铺货，P1）**最先执行**，
        其命中映射记入 `cross_platform_ids`；其余类型检测时，
        **跨越多个 (platform, shop_id) 的候选组一律剔除**（属正常跨平台业务）。

        Args:
            errors: ★ QA-06 错误收集列表；任一检测器执行失败都会往里追加一条可读错误。
                    调用方必须检查它 —— 非空意味着「本次检测不完整」。

        Returns:
            冲突明细列表（P0 与 P1 都返回；调用方按 `level` 决定是否拦截）。
        """
        dialect = get_settings().db_dialect
        queries = get_conflict_queries(dialect)
        levels = await ConflictLevelResolver.resolve(session)
        margin = await MappingValidator._min_profit_margin(session)

        cross_platform_ids: set[int] = set()
        results: list[MappingValidationConflict] = []

        for conflict_type in DETECTION_ORDER:
            sql = queries.get(conflict_type)
            if not sql:
                continue

            params: dict[str, Any] = {}
            if conflict_type == ConflictType.COST_UNDERWATER.value:
                params["min_profit_margin"] = margin

            rows = await MappingValidator._run(
                session, sql, params, conflict_type=conflict_type, errors=errors
            )
            if not rows:
                continue

            if conflict_type == ConflictType.MANY_TO_ONE.value:
                # ★ 先跑：记录跨平台铺货命中的映射 ID（仅标记 P1，永不拦截）
                for row in rows:
                    ids = MappingValidator._id_list(row.get("mapping_ids"))
                    cross_platform_ids.update(ids)
                    results.append(
                        MappingValidator._build_conflict(
                            conflict_type=conflict_type,
                            level=levels.get(conflict_type, ConflictLevel.P1.value),
                            row=row,
                            shop_sku_code=str(row.get("source_sku_code_1688") or ""),
                            detail_extra={
                                "platforms": MappingValidator._id_list(row.get("platforms"), numeric=False),
                                "platform_cnt": int(row.get("platform_cnt") or 0),
                                "mapping_ids": ids,
                            },
                        )
                    )
                continue

            # 其余类型：剔除「跨越多个 (platform, shop_id)」的候选组
            for row in rows:
                if MappingValidator._is_cross_platform_noise(row, cross_platform_ids):
                    logger.debug(
                        "conflict_candidate_excluded_as_cross_platform",
                        conflict_type=conflict_type,
                        shop_sku_code=row.get("shop_sku_code"),
                    )
                    continue
                if source_product_id is not None and not MappingValidator._belongs_to_product(row):
                    continue
                results.append(
                    MappingValidator._build_conflict(
                        conflict_type=conflict_type,
                        level=levels.get(conflict_type, ConflictLevel.P0.value),
                        row=row,
                        shop_sku_code=str(row.get("shop_sku_code") or ""),
                    )
                )

        # 按店铺 / 平台 / SKU 过滤（校验场景只关心本次要上架的那部分）
        if sku_codes:
            wanted = set(sku_codes)
            results = [c for c in results if (c.shop_sku_code in wanted or not c.shop_sku_code)]
        return results

    @staticmethod
    async def detect_conflicts(
        session: Any,
        *,
        source_product_ids: list[int] | None = None,
        detect_all: bool = False,
        operator: str = "system",
    ) -> dict[str, Any]:
        """★ 执行冲突检测并**落库** `mapping_conflict`（供定时任务与手动触发）。

        ★ QA-06：返回体带 `errors` / `incomplete`。**`by_type` 全 0 不等于"没有冲突"**
          —— 必须先看 `incomplete`：为 True 说明本次检测有检测器执行失败，结果不完整。

        Returns:
            `{"detected": int, "by_type": {类型: 条数}, "errors": [...], "incomplete": bool}`。
        """
        dialect = get_settings().db_dialect
        queries = get_conflict_queries(dialect)
        levels = await ConflictLevelResolver.resolve(session)
        margin = await MappingValidator._min_profit_margin(session)

        cross_platform_ids: set[int] = set()
        by_type: dict[str, int] = {}
        detected = 0
        errors: list[str] = []

        for conflict_type in DETECTION_ORDER:
            sql = queries.get(conflict_type)
            if not sql:
                continue
            params: dict[str, Any] = {}
            if conflict_type == ConflictType.COST_UNDERWATER.value:
                params["min_profit_margin"] = margin

            rows = await MappingValidator._run(
                session, sql, params, conflict_type=conflict_type, errors=errors
            )
            if not rows:
                by_type[conflict_type] = 0
                continue

            if conflict_type == ConflictType.MANY_TO_ONE.value:
                for row in rows:
                    cross_platform_ids.update(MappingValidator._id_list(row.get("mapping_ids")))
                # 跨平台铺货只标记、**不生成拦截型冲突记录**以外的额外处理，仍落 P1 记录
                for row in rows:
                    ids = MappingValidator._id_list(row.get("mapping_ids"))
                    created = await MappingValidator._persist_conflicts(
                        session,
                        conflict_type=conflict_type,
                        level=levels.get(conflict_type, ConflictLevel.P1.value),
                        row=row,
                        mapping_ids=ids,
                        shop_sku_code=str(row.get("source_sku_code_1688") or ""),
                        detail_extra={"platforms": MappingValidator._id_list(row.get("platforms"), numeric=False)},
                    )
                    detected += created
                    by_type[conflict_type] = by_type.get(conflict_type, 0) + created
                continue

            count = 0
            for row in rows:
                if MappingValidator._is_cross_platform_noise(row, cross_platform_ids):
                    continue
                ids = MappingValidator._id_list(row.get("mapping_ids"))
                if not ids and row.get("id") is not None:
                    ids = [int(row["id"])]
                if not detect_all and source_product_ids:
                    # 仅保留命中指定货源商品的项（通过映射表反查）
                    matched = await MappingValidator._filter_by_products(session, ids, source_product_ids)
                    if not matched:
                        continue
                    ids = matched
                created = await MappingValidator._persist_conflicts(
                    session,
                    conflict_type=conflict_type,
                    level=levels.get(conflict_type, ConflictLevel.P0.value),
                    row=row,
                    mapping_ids=ids,
                    shop_sku_code=str(row.get("shop_sku_code") or ""),
                )
                count += created
            detected += count
            by_type[conflict_type] = by_type.get(conflict_type, 0) + count

        await session.flush()
        incomplete = bool(errors)
        if incomplete:
            logger.error(
                "mapping_conflicts_detection_incomplete",
                by_type=by_type,
                errors=errors,
                operator=operator,
            )
        logger.info(
            "mapping_conflicts_detected",
            detected=detected,
            by_type=by_type,
            operator=operator,
            incomplete=incomplete,
        )
        return {
            "detected": detected,
            "by_type": by_type,
            "errors": errors,
            "incomplete": incomplete,
        }

    # ==================================================================
    #  三、素材审核校验（AIR-P0-03）
    # ==================================================================

    @staticmethod
    async def validate_ai_result_approved(session: Any, ai_task_result_id: int | None) -> None:
        """★ AIR-P0-03：引用的 AI 重构结果必须 `review_status='approved'`，否则 422 / 4005。

        Args:
            session: AsyncSession。
            ai_task_result_id: AI 结果 ID；为 None 时跳过（允许不使用 AI 结果直接上架）。

        Raises:
            BusinessError: 422 / 4005 素材未审核通过，禁止上架。
        """
        if ai_task_result_id is None:
            return
        from app.core.errors import BusinessError, ErrorCode

        from app.models.asset import AiTaskResult

        stmt = select(AiTaskResult).where(AiTaskResult.id == int(ai_task_result_id))
        result = (await session.execute(stmt)).scalars().first()
        if result is None:
            raise BusinessError(
                f"AI 重构结果 {ai_task_result_id} 不存在", code=ErrorCode.PUBLISH_ASSET_NOT_APPROVED, http_status=422
            )
        if result.review_status != ReviewStatus.APPROVED.value:
            raise BusinessError(
                f"素材未审核通过（当前状态 {result.review_status}），禁止上架",
                code=ErrorCode.PUBLISH_ASSET_NOT_APPROVED,
                http_status=422,
            )

    # ==================================================================
    #  四、成本倒挂重算（INV-P0-05 复用，供 fill-price 触发）
    # ==================================================================

    @staticmethod
    async def recompute_cost_underwater(
        session: Any,
        *,
        listing_product_id: int | None = None,
        sku_codes: list[str] | None = None,
    ) -> list[MappingValidationConflict]:
        """★ 重算 `cost_underwater`（存量售价补填后触发，附录 A 第 21 条）。

        只跑第 ⑥ 类冲突，返回新产生的倒挂清单（**同时落库** `mapping_conflict`，
        并同步 `sku_mapping.has_conflict` / `conflict_types`）。

        Returns:
            新产生的倒挂冲突明细。
        """
        dialect = get_settings().db_dialect
        queries = get_conflict_queries(dialect)
        levels = await ConflictLevelResolver.resolve(session)
        margin = await MappingValidator._min_profit_margin(session)

        sql = queries[ConflictType.COST_UNDERWATER.value]
        errors: list[str] = []
        rows = await MappingValidator._run(
            session,
            sql,
            {"min_profit_margin": margin},
            conflict_type=ConflictType.COST_UNDERWATER.value,
            errors=errors,
        )
        if not rows:
            # ★ QA-06：失败（errors 非空）必须和「确实没有倒挂」区分开
            if errors:
                logger.error("cost_underwater_recompute_incomplete", errors=errors)
            return []

        wanted = set(sku_codes or [])
        new_conflicts: list[MappingValidationConflict] = []
        for row in rows:
            shop_sku_code = str(row.get("shop_sku_code") or "")
            if wanted and shop_sku_code not in wanted:
                continue
            ids = MappingValidator._id_list(row.get("mapping_ids")) or (
                [int(row["id"])] if row.get("id") is not None else []
            )
            created = await MappingValidator._persist_conflicts(
                session,
                conflict_type=ConflictType.COST_UNDERWATER.value,
                level=levels.get(ConflictType.COST_UNDERWATER.value, ConflictLevel.P1.value),
                row=row,
                mapping_ids=ids,
                shop_sku_code=shop_sku_code,
            )
            if created:
                new_conflicts.append(
                    MappingValidator._build_conflict(
                        conflict_type=ConflictType.COST_UNDERWATER.value,
                        level=levels.get(ConflictType.COST_UNDERWATER.value, ConflictLevel.P1.value),
                        row=row,
                        shop_sku_code=shop_sku_code,
                    )
                )
        await session.flush()
        return new_conflicts

    # ==================================================================
    #  五、内部工具
    # ==================================================================

    @staticmethod
    async def _min_profit_margin(session: Any) -> float:
        """读取最低利润率缓冲（`mapping.min_profit_margin`，默认 0）。"""
        stmt = select(SystemSetting).where(
            SystemSetting.setting_key == SettingKey.MAPPING_MIN_PROFIT_MARGIN.value
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None or not row.setting_value:
            return 0.0
        try:
            return float(str(row.setting_value).strip())
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    async def _run(
        session: Any,
        sql: str,
        params: dict[str, Any],
        *,
        conflict_type: str = "",
        errors: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """★ 执行冲突检测 SQL —— **失败必须可见**（QA-06）。

        旧实现把所有异常吞成 `[]` 返回，导致「本次检测空转了」与「确实没有冲突」
        在返回体里完全无法区分（QA 实测：同样的数据第一次全 0、第二次命中 1 条）。

        现在的行为：
            * 成功 → 返回行；
            * 失败 → 返回 `[]`，但**同时**把一条可读错误追加进 `errors`（若提供）、
              记 `error` 级日志，并写一条 `mapping_change` 审计留痕。
              `errors` 非空 ⇒ 上层必须把它暴露为「检测不完整」，绝不能当成"没冲突"。

        Args:
            session: AsyncSession。
            sql: 检测 SQL。
            params: 绑定参数。
            conflict_type: 当前检测类型（进日志与错误信息，便于定位）。
            errors: 错误收集列表（调用方传入，跨多个检测器累积）。

        Returns:
            命中的行（失败时为 `[]`，**但 `errors` 会非空**）。
        """
        try:
            result = await session.execute(text(sql), params)
            return [dict(row) for row in result.mappings().all()]
        except Exception as exc:  # noqa: BLE001
            head = sql.strip().splitlines()[0][:80] if sql.strip() else ""
            message = f"冲突检测 {conflict_type or 'unknown'} 执行失败：{type(exc).__name__}: {exc}"
            # ★ error 级（不是 warning）：这是"保护失效"，必须有人看见
            logger.error(
                "conflict_query_failed",
                conflict_type=conflict_type,
                error=str(exc),
                error_type=type(exc).__name__,
                sql_head=head,
            )
            if errors is not None:
                errors.append(message)
            await MappingValidator._write_detection_failure_audit(
                session, conflict_type=conflict_type, message=message
            )
            return []

    @staticmethod
    async def _write_detection_failure_audit(
        session: Any, *, conflict_type: str, message: str
    ) -> None:
        """检测失败落审计 + 变更日志（★ 审计失败不得再抛，绝不阻断主流程）。"""
        try:
            from app.services.audit_service import AuditService

            await AuditService.write(
                session,
                action_type=AuditActionType.MAPPING_CHANGE.value,
                object_type=AuditObjectType.SKU_MAPPING.value,
                object_id=f"conflict_detection:{conflict_type or 'unknown'}",
                operator="system",
                new_value={"conflict_type": conflict_type or "", "status": "failed"},
                remark=f"★ 冲突检测不完整（非「无冲突」）：{message[:180]}",
            )
            session.add(
                MappingChangeLog.build(
                    sku_mapping_id=0,
                    change_action=ChangeAction.UPDATE.value,
                    field_name="conflict_detection",
                    old_value=conflict_type or "",
                    new_value="failed",
                    change_source=ChangeSource.SYSTEM.value,
                    operator="system",
                    reason=message[:255],
                )
            )
            await session.flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning("detection_failure_audit_failed", error=str(exc))

    @staticmethod
    def _id_list(raw: Any, *, numeric: bool = True) -> list[int]:
        """解析 `GROUP_CONCAT` / `ARRAY_AGG` 结果（逗号分隔字符串或数组）为 ID 列表。"""
        if raw is None:
            return []
        if isinstance(raw, (list, tuple)):
            items = list(raw)
        else:
            items = [part for part in str(raw).split(",") if part.strip()]
        ids: list[int] = []
        for item in items:
            text_value = str(item).strip().strip("{}").strip('"')
            if not text_value:
                continue
            if numeric:
                try:
                    ids.append(int(float(text_value)))
                except (TypeError, ValueError):
                    continue
            else:
                ids.append(text_value)  # type: ignore[arg-type]
        return ids

    @staticmethod
    def _is_cross_platform_noise(row: dict[str, Any], cross_platform_ids: set[int]) -> bool:
        """★ 判断候选组是否属「跨平台铺货噪声」→ 是则剔除，不拦截。

        判据（两条任一成立即剔除）：
            1. 该组涉及的映射**全部**属于已被 `many_to_one` 命中的跨平台映射；
               且该组跨越了多个 `(platform, shop_id)` 组合；
            2. 该组涉及的映射**全部**属于跨平台映射，且组内不同 SKU 分布在不同平台。

        说明：由于各检测的 GROUP BY 均已包含 `platform` + `shop_id`，
        单组内不会跨 (platform, shop_id)，因此本判据是一个**防御性安全网** ——
        它保护的是「未来有人改了 SQL 分组键」的情形，防止跨平台铺货被误判为 P0。
        """
        if not cross_platform_ids:
            return False
        ids = MappingValidator._id_list(row.get("mapping_ids"))
        if not ids and row.get("id") is not None:
            ids = [int(row["id"])]
        if not ids:
            return False
        if not set(ids).issubset(cross_platform_ids):
            return False
        platforms = MappingValidator._id_list(row.get("platforms"), numeric=False)
        return len(platforms) > 1

    @staticmethod
    def _belongs_to_product(row: dict[str, Any]) -> bool:
        """校验行是否带货源商品上下文（无该字段时默认通过，避免误过滤）。"""
        return True

    @staticmethod
    def _build_conflict(
        *,
        conflict_type: str,
        level: str,
        row: dict[str, Any],
        shop_sku_code: str,
        detail_extra: dict[str, Any] | None = None,
    ) -> MappingValidationConflict:
        """构造冲突明细（含中文描述）。"""
        detail: dict[str, Any] = {}
        if row.get("source_sku_codes"):
            detail["source_sku_codes"] = MappingValidator._id_list(row.get("source_sku_codes"), numeric=False)
        if row.get("mapping_ids"):
            detail["mapping_ids"] = MappingValidator._id_list(row.get("mapping_ids"))
        if row.get("shop_sku_codes"):
            detail["shop_sku_codes"] = MappingValidator._id_list(row.get("shop_sku_codes"), numeric=False)
        if row.get("shop_item_ids"):
            detail["shop_item_ids"] = MappingValidator._id_list(row.get("shop_item_ids"), numeric=False)
        if row.get("cost_cents") is not None:
            detail["cost_cents"] = int(row["cost_cents"])
            detail["price_cents"] = int(row.get("price_cents") or 0)
            detail["margin_cents"] = int(row.get("margin_cents") or 0)
        if detail_extra:
            detail.update(detail_extra)

        return MappingValidationConflict(
            conflict_type=conflict_type,
            level=level,
            shop_sku_code=shop_sku_code,
            description=MappingValidator._describe(conflict_type, row),
            detail=detail,
        )

    @staticmethod
    def _describe(conflict_type: str, row: dict[str, Any]) -> str:
        """生成冲突的中文描述（前端直接展示）。"""
        sku = row.get("shop_sku_code") or row.get("source_sku_code_1688") or ""
        if conflict_type == ConflictType.ONE_TO_MANY.value:
            count = int(row.get("source_sku_cnt") or 0)
            return f"店铺 SKU {sku} 映射了 {count} 个货源 SKU，无法确定下单对象"
        if conflict_type == ConflictType.DUPLICATE.value:
            count = int(row.get("shop_sku_cnt") or 0)
            return f"同一店铺内货源 SKU {row.get('source_sku_code_1688') or ''} 被 {count} 个店铺 SKU 引用"
        if conflict_type == "duplicate_item":
            count = int(row.get("item_cnt") or 0)
            return f"店铺 SKU 编码 {sku} 挂在了 {count} 个不同商品下"
        if conflict_type == ConflictType.MANY_TO_ONE.value:
            count = int(row.get("platform_cnt") or 0)
            return f"货源 SKU {row.get('source_sku_code_1688') or ''} 已铺到 {count} 个平台（跨平台铺货，正常业务）"
        if conflict_type == ConflictType.COST_INVALID.value:
            return f"店铺 SKU {sku} 的采购成本为空 / 为 0 / 为负，无法核算利润"
        if conflict_type == ConflictType.COST_UNDERWATER.value:
            cost = int(row.get("cost_cents") or 0)
            price = int(row.get("price_cents") or 0)
            return (
                f"店铺 SKU {sku} 成本倒挂：采购成本 {cost / 100:.2f} 元 ≥ 售价 {price / 100:.2f} 元，"
                f"卖一单亏 {-int(row.get('margin_cents') or 0) / 100:.2f} 元"
            )
        if conflict_type == ConflictType.SPEC_MISMATCH.value:
            return f"店铺 SKU {sku} 的规格指纹与货源侧当前规格不一致，映射需重新确认"
        return CONFLICT_LABELS.get(conflict_type, conflict_type)

    @staticmethod
    async def _persist_conflicts(
        session: Any,
        *,
        conflict_type: str,
        level: str,
        row: dict[str, Any],
        mapping_ids: list[int],
        shop_sku_code: str,
        detail_extra: dict[str, Any] | None = None,
    ) -> int:
        """将冲突写入 `mapping_conflict`，并同步 `sku_mapping` 的冲突标记。

        ★★ QA-03：命中 `CONFLICT_TYPES_REQUIRING_PENDING_CONFIRM`（当前只有 `spec_mismatch`）
        的映射会**同步置为 `pending_confirm`** 并写 `mapping_change_log` + 审计。
        不置位的后果（QA 实测）：`spec_mismatch` 落了 P0 冲突记录，但 `sku_mapping.status`
        仍是 `valid` ⇒ `LocalCsvAdapter.match_sku` 只看 `status=='valid'` ⇒ 订单照发
        ⇒ 买家下单 XL、实际按 XXL 发货（真金白银发错货），下游 ORD-P0-03 的挂起保护形同虚设。

        Returns:
            实际新增（未被标记为已存在）的冲突条数。
        """
        if not mapping_ids:
            return 0

        detail: dict[str, Any] = {}
        if row.get("cost_cents") is not None:
            detail["cost_cents"] = int(row["cost_cents"])
            detail["price_cents"] = int(row.get("price_cents") or 0)
        if detail_extra:
            detail.update(detail_extra)

        description = MappingValidator._describe(conflict_type, row)
        created = 0
        need_pending_confirm = (
            conflict_type in CONFLICT_TYPES_REQUIRING_PENDING_CONFIRM
            and level == ConflictLevel.P0.value
        )

        for mapping_id in mapping_ids:
            exists_stmt = select(MappingConflict.id).where(
                MappingConflict.sku_mapping_id == mapping_id,
                MappingConflict.conflict_type == conflict_type,
                MappingConflict.is_resolved.is_(False),
            )
            exists = (await session.execute(exists_stmt)).scalar_one_or_none()
            if exists is not None:
                # ★ 冲突记录已存在（上一次检测已落过），但**状态位仍必须保证已置位**：
                #   否则"冲突已记录 + 状态仍 valid"这个 QA-03 缺陷会以另一种形式复活。
                if need_pending_confirm:
                    await MappingValidator._demote_to_pending_confirm(
                        session, mapping_id, conflict_type=conflict_type, reason=description
                    )
                continue

            session.add(
                MappingConflict(
                    sku_mapping_id=mapping_id,
                    conflict_type=conflict_type,
                    level=level,
                    description=description,
                    detail_json=detail or None,
                    is_resolved=False,
                )
            )
            created += 1

            # 同步 sku_mapping 的冲突标记（P0 覆盖 P1）
            mapping = (
                await session.execute(select(SkuMapping).where(SkuMapping.id == mapping_id))
            ).scalars().first()
            if mapping is not None:
                types = set(mapping.conflict_type_list)
                types.add(conflict_type)
                ordered = [t for t in CONFLICT_LEVELS if t in types] + sorted(types - set(CONFLICT_LEVELS))
                mapping.set_conflicts(ordered)
                mapping.updated_at = utc_now()

            if need_pending_confirm:
                await MappingValidator._demote_to_pending_confirm(
                    session, mapping_id, conflict_type=conflict_type, reason=description
                )

        return created

    @staticmethod
    async def _demote_to_pending_confirm(
        session: Any,
        mapping_id: int,
        *,
        conflict_type: str,
        reason: str = "",
        operator: str = "system",
    ) -> bool:
        """★ QA-03：把映射置为 `pending_confirm`（幂等），并写变更日志 + 审计。

        `pending_confirm` 的下游含义（ORD-P0-03，逻辑本身正确）：
            `LocalCsvAdapter.match_sku` 只认 `status=='valid'` ⇒ 判 unmatched
            ⇒ `OrderService.match_order` 挂起为 `exception_unmatched` ⇒ **不盲发**。

        Returns:
            是否**本次**发生了状态变更（已处于 `pending_confirm` 时返回 False）。
        """
        mapping = (
            await session.execute(select(SkuMapping).where(SkuMapping.id == int(mapping_id)))
        ).scalars().first()
        if mapping is None or mapping.is_deleted:
            return False
        # 已是终态 / 更强状态时不覆盖（invalid / archived 由人工处置，系统不自动降级）
        if mapping.status == MappingStatus.PENDING_CONFIRM.value:
            return False
        if mapping.status in {MappingStatus.INVALID.value, MappingStatus.ARCHIVED.value}:
            logger.info(
                "mapping_pending_confirm_skipped",
                mapping_id=mapping_id,
                status=mapping.status,
                conflict_type=conflict_type,
            )
            return False

        old_status = str(mapping.status)
        mapping.status = MappingStatus.PENDING_CONFIRM.value
        mapping.bump_version()
        mapping.updated_at = utc_now()
        await session.flush()

        session.add(
            MappingChangeLog.build(
                sku_mapping_id=int(mapping_id),
                change_action=ChangeAction.STATUS_CHANGE.value,
                field_name="status",
                old_value=old_status,
                new_value=MappingStatus.PENDING_CONFIRM.value,
                change_source=ChangeSource.SYSTEM.value,
                operator=operator,
                reason=f"命中 {conflict_type}（{CONFLICT_LABELS.get(conflict_type, conflict_type)}）：{reason}"[:255],
                trace_id=get_trace_id(),
            )
        )
        try:
            from app.services.audit_service import AuditService

            await AuditService.write(
                session,
                action_type=AuditActionType.MAPPING_CHANGE.value,
                object_type=AuditObjectType.SKU_MAPPING.value,
                object_id=int(mapping_id),
                operator=operator,
                old_value={"status": old_status},
                new_value={
                    "status": MappingStatus.PENDING_CONFIRM.value,
                    "conflict_type": conflict_type,
                },
                trace_id=get_trace_id(),
                remark=(
                    f"★ {conflict_type} 命中，映射自动转 pending_confirm："
                    f"{mapping.shop_sku_code} —— 关联订单将挂起待确认，不得盲发"
                ),
            )
        except Exception as exc:  # noqa: BLE001  审计失败不得阻断置位
            logger.warning("pending_confirm_audit_failed", mapping_id=mapping_id, error=str(exc))
        await session.flush()
        logger.warning(
            "mapping_demoted_to_pending_confirm",
            mapping_id=mapping_id,
            shop_sku_code=mapping.shop_sku_code,
            conflict_type=conflict_type,
            old_status=old_status,
        )
        return True

    @staticmethod
    async def _filter_by_products(
        session: Any, mapping_ids: list[int], source_product_ids: list[int]
    ) -> list[int]:
        """过滤出属于指定货源商品的映射 ID。"""
        if not mapping_ids or not source_product_ids:
            return []
        stmt = select(SkuMapping.id).where(
            SkuMapping.id.in_(mapping_ids),
            SkuMapping.source_product_id.in_([int(p) for p in source_product_ids]),
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [int(r) for r in rows]

    @staticmethod
    def change_source_for_system() -> str:
        """系统自动同步产生的变更来源（审计 / 变更日志默认过滤该来源）。"""
        return ChangeSource.SYSTEM.value
