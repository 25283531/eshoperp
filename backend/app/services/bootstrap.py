"""★ 启动期显式初始化与约束自检（QA-05 / QA-01 / QA-02 修复）。

================================================================================
★ 为什么要有这个文件
================================================================================
QA-05 的根因是「适配器配置行靠**第一次 GET 时顺手 flush 一下**来播种」：
`FulfillmentService._ensure_rows()` 只 `session.add()` + `session.flush()`，
而 `get_db()` 从来不 commit ⇒ 这三行只活在**单次请求的内存里**，
下一个请求 `PUT /adapters/fulfillment/{name}/config` 查表查不到 ⇒ **恒定 404**。
运营因此永远配不上妙手 / 逸淘的 base_url / 凭证 / declared_scopes，
红线 R1 的「声明越权 scope → 403 + 落审计 + 顶栏红点」这条真实触发路径**第一步就走不到**。

本模块把初始化改成**显式、幂等、带 commit**的动作：
    * `bootstrap_all()` 在应用 lifespan 启动阶段调用一次；
    * `scripts/seed.py` 也调用同一套函数（保证开发库与生产库一致）；
    * 幂等：已存在的行**绝不覆盖**（运营在界面上改过的配置必须活过重启）。

================================================================================
★ 约束自检（QA-01 / QA-02 的"保护必须可验证"）
================================================================================
`one_to_many` / `duplicate_item` 两类冲突靠部分唯一索引
    uq_sku_mapping_shop_sku (platform, shop_id, shop_sku_code) WHERE is_deleted = 0
在**写入边界**结构性阻断（详见 `app/models/mapping.py` 头部注释）。
读时扫描 SQL 已删除 —— 那么"索引还在不在"就成了**唯一的保护**，
必须能被立即验证。`check_required_constraints()` 就是干这个的：
启动时自检 + 挂到 `/health`，索引一旦被人误删，`/health` 立刻暴露。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, text

from app.core.logging import get_logger
from app.models.enums import (
    CredentialOwnerType,
    CredentialStatus,
    PlatformAccountStatus,
    ScopeCheckStatus,
)

logger = get_logger(__name__)

__all__ = [
    "DEFAULT_FULFILLMENT_ADAPTERS",
    "REQUIRED_CONSTRAINTS",
    "bootstrap_all",
    "bootstrap_credentials",
    "bootstrap_fulfillment_adapters",
    "bootstrap_platform_accounts",
    "check_required_constraints",
]


# ============================================================================
#  一、履约适配器配置行（★ QA-05：必须真实落库，否则 config 接口 404）
# ============================================================================

# ★ 默认声明 scope 取自 R1 白名单（`scope_guard.ALLOWED_SCOPES`）：
#   第三方分销工具只允许「订单读取」与「物流回填」，其余一律拒绝。
#   local_csv 是本地兜底，不需要任何第三方授权。
DEFAULT_FULFILLMENT_ADAPTERS: tuple[dict[str, Any], ...] = (
    {
        "adapter_name": "local_csv",
        "display_name": "本地兜底",
        "declared_scopes": [],
        "is_enabled": True,
        "priority": 10,
        "config_json": {},
    },
    {
        "adapter_name": "miaoshou",
        "display_name": "妙手",
        "declared_scopes": ["order.read", "logistics.write"],
        "is_enabled": False,
        "priority": 20,
        "config_json": {},
    },
    {
        "adapter_name": "yitao",
        "display_name": "逸淘",
        "declared_scopes": ["order.read", "logistics.write"],
        "is_enabled": False,
        "priority": 30,
        "config_json": {},
    },
)


async def bootstrap_fulfillment_adapters(session: Any, *, commit: bool = True) -> dict[str, Any]:
    """★ 幂等 upsert 三个内置履约适配器的配置行，并 **commit** 落库。

    ★★ 绝不覆盖已存在的行 ★★
        运营在界面上改过的 `declared_scopes` / `is_enabled` / `config_json`
        必须活过重启 —— 本函数只负责「缺失时按默认补齐」。

    Args:
        session: AsyncSession。
        commit: 是否立即提交（默认 True；这是修复 QA-05 的关键）。

    Returns:
        `{"created": [...新建的适配器名], "existing": [...已存在的], "total": 行数}`。
    """
    from app.adapters.fulfillment.registry import ADAPTER_REGISTRY
    from app.adapters.fulfillment.factory import _ensure_registered
    from app.models.order import FulfillmentAdapterConfig

    _ensure_registered()

    created: list[str] = []
    existing: list[str] = []

    for spec in DEFAULT_FULFILLMENT_ADAPTERS:
        name = str(spec["adapter_name"])
        row = (
            await session.execute(
                select(FulfillmentAdapterConfig).where(FulfillmentAdapterConfig.adapter_name == name)
            )
        ).scalars().first()
        if row is not None:
            existing.append(name)
            continue


        session.add(
            FulfillmentAdapterConfig(
                adapter_name=name,
                display_name=str(spec["display_name"]),
                capability_json={},
                declared_scopes_json=list(spec["declared_scopes"]),
                scope_check_status=ScopeCheckStatus.PASSED.value,
                scope_check_message="默认 scope（已通过白名单校验）",
                is_enabled=bool(spec["is_enabled"]),
                is_active=False,
                priority=int(spec["priority"]),
                config_json=dict(spec["config_json"]),
            )
        )
        created.append(name)

    # ★ 注册表里出现但默认表里没有的适配器（未来新增）也补齐，避免再次出现 404
    display_fallback = {"miaoshou": "妙手", "yitao": "逸淘", "local_csv": "本地兜底"}
    known = {str(spec["adapter_name"]) for spec in DEFAULT_FULFILLMENT_ADAPTERS}
    for name in sorted(ADAPTER_REGISTRY.keys()):
        if name in known:
            continue
        row = (
            await session.execute(
                select(FulfillmentAdapterConfig).where(FulfillmentAdapterConfig.adapter_name == name)
            )
        ).scalars().first()
        if row is not None:
            existing.append(name)
            continue
        session.add(
            FulfillmentAdapterConfig(
                adapter_name=name,
                display_name=display_fallback.get(name, name),
                capability_json={},
                declared_scopes_json=[],
                scope_check_status=ScopeCheckStatus.PASSED.value,
                scope_check_message="",
                is_enabled=False,
                is_active=False,
                priority=100,
            )
        )
        created.append(name)

    # ------------------------------------------------------------------
    #  ★ 重算 `scope_check_status`（对**所有**行，包括运营改过的）
    # ------------------------------------------------------------------
    #  `scope_check_status` 是 `declared_scopes` 的**派生值**，不是独立状态。
    #  但它在历史上只在「保存配置」时写入一次，于是出现不一致：
    #      * 运营把越权 scope 改回合法 → 状态仍是 rejected → **顶栏/页面误报红灯**；
    #      * 直接改库、或用旧库启动 → 状态与实际声明对不上。
    #  「声明合法却亮红」比「亮红但没人看」更糟 —— 它会训练运营忽略告警。
    #  所以启动期按声明**重算一次**：合法 → passed，越权 → rejected（该亮的还得亮）。
    #
    #  ★★ 绝不写审计 ★★
    #     顶栏红点计数的是 `permission_change` 且 `is_handled=0` 的审计条数。
    #     重算若顺手写一条审计，**每次重启都会点亮一次红点** —— 那才是真正的误报。
    recomputed = await _recompute_scope_check_status(session)

    if commit:
        await session.commit()

    result = {
        "created": created,
        "existing": existing,
        "recomputed": recomputed,
        "total": len(created) + len(existing),
    }
    if created or recomputed:
        logger.info(
            "fulfillment_adapters_bootstrapped",
            created=created,
            existing=existing,
            recomputed=recomputed,
        )
    return result


async def _recompute_scope_check_status(session: Any) -> list[str]:
    """按 `declared_scopes_json` 重算每个适配器的 `scope_check_status`（★ 不写审计）。

    Returns:
        被纠正的适配器名列表（状态或文案发生了变化的行）。
    """
    from app.adapters.fulfillment.scope_guard import check_scope
    from app.models.enums import ScopeCheckStatus
    from app.models.order import FulfillmentAdapterConfig

    rows = (await session.execute(select(FulfillmentAdapterConfig))).scalars().all()
    changed: list[str] = []
    for row in rows:
        passed, forbidden, unknown = check_scope(list(row.declared_scopes_json or []))
        denied = list(forbidden or []) + list(unknown or [])
        new_status = ScopeCheckStatus.PASSED.value if passed else ScopeCheckStatus.REJECTED.value
        new_message = (
            "scope 校验通过" if passed else f"声明了越权 scope：{', '.join(denied)}"
        )
        if row.scope_check_status != new_status or row.scope_check_message != new_message:
            row.scope_check_status = new_status
            row.scope_check_message = new_message
            changed.append(str(row.adapter_name))
    if changed:
        logger.warning(
            "adapter_scope_status_recomputed",
            changed=changed,
            reason="启动期按声明的 scope 重算（修复『声明合法但状态仍是 rejected』的不一致）",
        )
    return changed


# ============================================================================
#  二、平台账号与凭证（占位 / 演示，同样幂等）
# ============================================================================

# ★ 演示用平台账号：真实环境由运营在「设置 → 平台账号」里录入，
#   这里只保证库里**有行**，避免前端列表恒空、授权入口无从下手。
DEFAULT_PLATFORM_ACCOUNTS: tuple[dict[str, Any], ...] = (
    {
        "platform": "taobao",
        "shop_id": "shop-seed-001",
        "shop_name": "演示淘宝店铺（种子数据）",
        "granted_scopes": ["order.read", "logistics.write"],
    },
)

# ★ 第三方履约工具的凭证占位行：`__NOT_CONFIGURED__` 表示「尚未配置」，
#   适配器读到该值会走 R3 优雅降级（返回 RETRYABLE / UNSUPPORTED 信封，不抛异常）。
DEFAULT_CREDENTIALS: tuple[dict[str, Any], ...] = (
    {
        "owner_type": CredentialOwnerType.FULFILLMENT.value,
        "owner_key": "miaoshou",
        "credential_key": "app_key",
        "value": "__NOT_CONFIGURED__",
    },
    {
        "owner_type": CredentialOwnerType.FULFILLMENT.value,
        "owner_key": "miaoshou",
        "credential_key": "app_secret",
        "value": "__NOT_CONFIGURED__",
    },
    {
        "owner_type": CredentialOwnerType.FULFILLMENT.value,
        "owner_key": "yitao",
        "credential_key": "app_key",
        "value": "__NOT_CONFIGURED__",
    },
    {
        "owner_type": CredentialOwnerType.FULFILLMENT.value,
        "owner_key": "yitao",
        "credential_key": "app_secret",
        "value": "__NOT_CONFIGURED__",
    },
)


async def bootstrap_platform_accounts(session: Any, *, commit: bool = True) -> dict[str, Any]:
    """幂等补齐演示平台账号（已存在则跳过，绝不覆盖）。"""
    from app.models.listing import PlatformAccount

    created: list[str] = []
    existing: list[str] = []

    for spec in DEFAULT_PLATFORM_ACCOUNTS:
        row = (
            await session.execute(
                select(PlatformAccount).where(
                    PlatformAccount.platform == spec["platform"],
                    PlatformAccount.shop_id == spec["shop_id"],
                )
            )
        ).scalars().first()
        if row is not None:
            existing.append(f"{spec['platform']}:{spec['shop_id']}")
            continue
        session.add(
            PlatformAccount(
                platform=str(spec["platform"]),
                shop_id=str(spec["shop_id"]),
                shop_name=str(spec["shop_name"]),
                granted_scopes_json=list(spec["granted_scopes"]),
                status=PlatformAccountStatus.ACTIVE.value,
            )
        )
        created.append(f"{spec['platform']}:{spec['shop_id']}")

    if commit:
        await session.commit()
    return {"created": created, "existing": existing, "total": len(created) + len(existing)}


async def bootstrap_credentials(session: Any, *, commit: bool = True) -> dict[str, Any]:
    """幂等补齐第三方履约工具的凭证占位行（★ 明文 AES-256 加密后落库）。

    ★ 加密失败**不阻断启动**（记录 warning 后跳过），凭证属于可后补数据。
    """
    from app.core.security import encrypt_credential
    from app.models.system import Credential
    from app.utils.crypto import mask_secret

    created: list[str] = []
    existing: list[str] = []
    skipped: list[str] = []

    for spec in DEFAULT_CREDENTIALS:
        key = f"{spec['owner_type']}:{spec['owner_key']}.{spec['credential_key']}"
        row = (
            await session.execute(
                select(Credential).where(
                    Credential.owner_type == spec["owner_type"],
                    Credential.owner_key == spec["owner_key"],
                    Credential.credential_key == spec["credential_key"],
                )
            )
        ).scalars().first()
        if row is not None:
            existing.append(key)
            continue
        try:
            value_enc = encrypt_credential(str(spec["value"]))
        except Exception as exc:  # noqa: BLE001  凭证可后补，不得阻断启动
            logger.warning("bootstrap_credential_encrypt_failed", key=key, error=str(exc))
            skipped.append(key)
            continue
        session.add(
            Credential(
                owner_type=str(spec["owner_type"]),
                owner_key=str(spec["owner_key"]),
                credential_key=str(spec["credential_key"]),
                value_enc=value_enc,
                value_masked=mask_secret(str(spec["value"])),
                status=CredentialStatus.ACTIVE.value,
            )
        )
        created.append(key)

    if commit:
        await session.commit()
    return {
        "created": created,
        "existing": existing,
        "skipped": skipped,
        "total": len(created) + len(existing),
    }


async def bootstrap_all(session: Any, *, commit: bool = True) -> dict[str, Any]:
    """★ 启动期一次性初始化（幂等 + 已 commit）。

    Returns:
        `{"adapters": {...}, "platform_accounts": {...}, "credentials": {...}}`。
    """
    result: dict[str, Any] = {}
    try:
        result["adapters"] = await bootstrap_fulfillment_adapters(session, commit=commit)
    except Exception as exc:  # noqa: BLE001  初始化失败不得阻断 Web 启动
        logger.error("bootstrap_adapters_failed", error=str(exc))
        result["adapters"] = {"error": str(exc)}
    try:
        result["platform_accounts"] = await bootstrap_platform_accounts(session, commit=commit)
    except Exception as exc:  # noqa: BLE001
        logger.error("bootstrap_platform_accounts_failed", error=str(exc))
        result["platform_accounts"] = {"error": str(exc)}
    try:
        result["credentials"] = await bootstrap_credentials(session, commit=commit)
    except Exception as exc:  # noqa: BLE001
        logger.error("bootstrap_credentials_failed", error=str(exc))
        result["credentials"] = {"error": str(exc)}
    return result


# ============================================================================
#  三、★ 约束自检：保护必须真实且可验证（QA-01 / QA-02）
# ============================================================================

REQUIRED_CONSTRAINTS: tuple[dict[str, Any], ...] = (
    {
        "name": "uq_sku_mapping_shop_sku",
        "table": "sku_mapping",
        "columns": ("platform", "shop_id", "shop_sku_code"),
        "unique": True,
        "partial": True,  # ★ 必须带 `WHERE is_deleted = 0`，否则软删除记录会占用唯一键
        "why": (
            "QA-01/QA-02：one_to_many 与 duplicate_item 由它在写入边界结构性阻断；"
            "读时扫描 SQL 因同一索引恒为空已移除 —— 索引是本保护**唯一的**承载者，"
            "一旦缺失这两类冲突将静默不再受控。"
        ),
    },
    {
        "name": "uq_platform_account_platform_shop",
        "table": "platform_account",
        "columns": ("platform", "shop_id"),
        "unique": True,
        "partial": False,
        "why": "同一平台同一店铺不得重复建账号（授权入口幂等的前提）。",
    },
    {
        "name": "uq_credential_owner_key",
        "table": "credential",
        "columns": ("owner_type", "owner_key", "credential_key"),
        "unique": True,
        "partial": False,
        "why": "凭证保险箱按 (owner_type, owner_key, credential_key) 唯一，防重复写入。",
    },
)


def _dialect_name(session: Any) -> str:
    """取会话绑定的数据库方言名（`sqlite` / `postgresql` / ...）。"""
    try:
        return str(session.bind.dialect.name or "").lower()
    except Exception:  # noqa: BLE001
        return ""


async def check_required_constraints(session: Any) -> list[dict[str, Any]]:
    """★ 校验 `REQUIRED_CONSTRAINTS` 里的索引确实存在于当前库中。

    ★★ 这不是"锦上添花"的检查 ★★
        `one_to_many` / `duplicate_item` 的读时扫描已删除，保护**完全**依赖唯一索引。
        索引若被误删，冲突检测会安静地返回 0 —— 正是本项目反复踩的
        「不报错、只静默失效」。本函数让"索引不在了"这件事**立刻可见**。

    Returns:
        问题清单；**空列表表示全部通过**。每条形如
        `{"name": ..., "table": ..., "reason": ..., "expected": ..., "found": ...}`。
    """
    dialect = _dialect_name(session)
    is_pg = "postgres" in dialect
    if is_pg:
        sql = "SELECT indexname AS name, tablename AS tbl, indexdef AS ddl FROM pg_indexes WHERE indexname = :name"
        # ★ 表级 UNIQUE 约束**不是**索引：PG 里它们只存在于 `pg_constraint`
        constraint_sql = (
            "SELECT conname AS name, conrelid::regclass::text AS tbl, "
            "pg_get_constraintdef(oid) AS ddl FROM pg_constraint WHERE conname = :cname"
        )
    else:
        sql = "SELECT name AS name, tbl_name AS tbl, sql AS ddl FROM sqlite_master WHERE type = 'index' AND name = :name"
        # ★ SQLite 把 `UniqueConstraint(...)` 编进 `CREATE TABLE` 的 DDL 里
        #   （生成的是 `sqlite_autoindex_*`，查不到我们命名的那个名字），
        #   所以必须回退到「表 DDL 里是否声明了该具名约束」。
        constraint_sql = (
            "SELECT name AS name, tbl_name AS tbl, sql AS ddl FROM sqlite_master "
            "WHERE type = 'table' AND tbl_name = :tbl AND sql LIKE '%' || :cname || '%'"
        )

    issues: list[dict[str, Any]] = []
    for spec in REQUIRED_CONSTRAINTS:
        name = str(spec["name"])
        try:
            row = (await session.execute(text(sql), {"name": name})).mappings().first()
            if row is None:
                # 回退：按「表级具名约束」再查一次
                row = (
                    await session.execute(
                        text(constraint_sql), {"cname": name, "tbl": str(spec["table"])}
                    )
                ).mappings().first()
        except Exception as exc:  # noqa: BLE001
            issues.append(
                {
                    "name": name,
                    "table": spec["table"],
                    "reason": f"约束自检本身执行失败：{exc}",
                    "expected": _describe_expected(spec),
                    "found": "",
                }
            )
            continue

        if row is None:
            issues.append(
                {
                    "name": name,
                    "table": spec["table"],
                    "reason": f"★ 索引缺失：{spec['why']}",
                    "expected": _describe_expected(spec),
                    "found": "",
                }
            )
            continue

        ddl = str(row.get("ddl") or "")
        found_tbl = str(row.get("tbl") or "")
        problems: list[str] = []
        if found_tbl and found_tbl != str(spec["table"]):
            problems.append(f"索引建在表 {found_tbl} 上，期望 {spec['table']}")
        if spec["unique"] and "unique" not in ddl.lower():
            problems.append("不是 UNIQUE 索引")
        if spec["partial"] and "is_deleted" not in ddl.lower():
            problems.append("缺少部分索引条件（应带 is_deleted，软删除记录不得占用唯一键）")
        for column in spec["columns"]:
            if column not in ddl:
                problems.append(f"缺少键列 {column}")

        if problems:
            issues.append(
                {
                    "name": name,
                    "table": spec["table"],
                    "reason": "；".join(problems) + " —— " + str(spec["why"]),
                    "expected": _describe_expected(spec),
                    "found": ddl,
                }
            )

    if issues:
        logger.error(
            "required_constraints_missing",
            count=len(issues),
            names=[i["name"] for i in issues],
        )
    return issues


def _describe_expected(spec: dict[str, Any]) -> str:
    """把期望的约束描述成一行人类可读文本（进日志与 `/health`）。"""
    columns = ", ".join(spec["columns"])
    head = "UNIQUE INDEX" if spec["unique"] else "INDEX"
    tail = " WHERE is_deleted = 0" if spec["partial"] else ""
    return f"{head} {spec['name']} ON {spec['table']} ({columns}){tail}"
