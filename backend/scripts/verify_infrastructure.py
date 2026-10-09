"""基础设施自检脚本（Batch A：T-A01 ~ T-A04 验收）。

用法（在 backend/ 目录下执行）：

    python scripts/verify_infrastructure.py

验证项：
    1. `import app.main` 可导入；
    2. SkuMapping 部分唯一索引真实生效（重复插入抛异常）；
    3. `enforce_scope(['item.write'])` 被拒绝；`enforce_scope(['order.read'])` 通过；
    4. `FulfillmentAdapterFactory.create()` 默认返回 `LocalCsvAdapter`；
    5. 三个适配器（miaoshou / yitao / local_csv）全部注册成功；
    6. 调用 Miaoshou / Yitao 的 UNSUPPORTED 能力返回 UNSUPPORTED 信封（不抛异常）；
    7. TaskRunner 提交 → 执行 → 成功落库（含幂等键去重）；
    8. 冲突检测 SQL 常量可按方言取用；
    9. 红线 R2：`FulfillmentAdapter` 子类不含 `offline` / `update_stock_price`。

★ 使用独立的验证数据库 `data/verify_infrastructure.db`，不污染业务库。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# 让脚本可以直接 `python scripts/verify_infrastructure.py` 运行
BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

PROJECT_ROOT = BACKEND_DIR.parent
VERIFY_DB = PROJECT_ROOT / "data" / "verify_infrastructure.db"
VERIFY_DB.parent.mkdir(parents=True, exist_ok=True)
if VERIFY_DB.exists():
    try:
        VERIFY_DB.unlink()
    except OSError as exc:
        # ★ 本机环境的删除操作被安全策略接管（safe-delete），偶发 `trash-failed`。
        #   旧实现直接让异常炸出来 ⇒ 自检跑不起来；这里退化为「换一个新库文件」，
        #   结论不受影响（库本来就每次重建），但要**打印出来**，不静默。
        VERIFY_DB = PROJECT_ROOT / "data" / f"verify_infrastructure_{os.getpid()}.db"
        print(f"!! 旧验证库无法删除（安全策略），本次改用：{VERIFY_DB.name}\n   {exc}")

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{VERIFY_DB.as_posix()}"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["TASK_RECOVERY_ENABLED"] = "false"

from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.adapters.fulfillment.base import (  # noqa: E402
    Capability,
    MatchSkuRequest,
    WriteBackRequest,
)
from app.adapters.fulfillment.factory import FulfillmentAdapterFactory  # noqa: E402
from app.adapters.fulfillment.local_csv import LocalCsvAdapter  # noqa: E402
from app.adapters.fulfillment.miaoshou import MiaoshouAdapter  # noqa: E402
from app.adapters.fulfillment.registry import ADAPTER_REGISTRY  # noqa: E402
from app.adapters.fulfillment.scope_guard import (  # noqa: E402
    ScopeViolationError,
    enforce_scope,
)
from app.adapters.fulfillment.yitao import YitaoAdapter  # noqa: E402
from app.core.errors import ErrorCode  # noqa: E402
from app.models import Base, SkuMapping  # noqa: E402
from app.models.enums import (  # noqa: E402
    AdapterName,
    Capability as CapabilityEnum,
    MappingStatus,
)
from app.models.mapping import get_conflict_queries  # noqa: E402
from app.tasks.registry import task_handler  # noqa: E402
from app.tasks.runner import LocalTaskRunner  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
_results: list[tuple[str, bool, str]] = []


def _record(name: str, ok: bool, detail: str = "") -> None:
    """记录一条验证结果。"""
    _results.append((name, ok, detail))
    print(f"[{'OK' if ok else 'NG'}] {name}" + (f"  -> {detail}" if detail else ""))


# ---------------------------------------------------------------------------
#  1. 应用可导入
# ---------------------------------------------------------------------------


def check_import_app() -> None:
    """验证 `import app.main` 成功。"""
    import app.main  # noqa: F401

    _record("1. import app.main", True, f"FastAPI title={app.main.app.title}")


# ---------------------------------------------------------------------------
#  2~6. 数据库相关（唯一索引 / 工厂 / 能力降级）
# ---------------------------------------------------------------------------


_engine = None


async def _prepare_db() -> async_sessionmaker:
    """创建验证用数据库全部表 **+ 播种基础数据**。

    ★★ 为什么必须播种（QA-05 的直接教训）★★
        早期这里只 `create_all`，于是 `fulfillment_adapter` 恒为 0 行。
        而 `FulfillmentAdapterFactory.create()` 按红线 R1 是**失败关闭**的
        （"无法确认权限 = 拒绝"），读不到适配器行就直接抛 5003 ——
        自检脚本自己先崩在 `check_factory_default`，把"适配器没落库"
        这个真问题伪装成了"自检脚本坏了"。
        生产由 `main.py` lifespan 的 `bootstrap_all()` 负责播种，
        这里必须用**同一套**逻辑，否则自检的就是一个生产上不存在的环境。
    """
    global _engine
    _engine = create_async_engine(os.environ["DATABASE_URL"])
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(_engine, expire_on_commit=False)

    from app.services.bootstrap import bootstrap_all, check_required_constraints

    async with factory() as session:
        await bootstrap_all(session, commit=True)
        issues = await check_required_constraints(session)
        if issues:
            print(f"!! 结构性约束未就位：{[str(i.get('name')) for i in issues]}")
    return factory


async def _prepare_db_cached() -> async_sessionmaker:
    """复用已创建的验证数据库会话工厂。"""
    global _engine
    if _engine is None:
        return await _prepare_db()
    return async_sessionmaker(_engine, expire_on_commit=False)


async def check_unique_index(session_factory: async_sessionmaker) -> None:
    """★ SkuMapping 部分唯一索引必须阻止重复键。"""
    async with session_factory() as session:
        first = SkuMapping(
            platform="taobao",
            shop_id="shop-1",
            shop_item_id="ITEM-1",
            shop_sku_code="SKU-RED-XL",
            purchase_cost_cents=1250,
            status=MappingStatus.VALID.value,
            source_product_1688_id="1688-1",
            source_sku_code_1688="1688-SKU-1",
        )
        session.add(first)
        await session.commit()

        duplicate = SkuMapping(
            platform="taobao",
            shop_id="shop-1",
            shop_item_id="ITEM-1",
            shop_sku_code="SKU-RED-XL",  # 同键 → 必须冲突
            purchase_cost_cents=1300,
            status=MappingStatus.VALID.value,
        )
        session.add(duplicate)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            message = str(exc).splitlines()[0]
            _record("2. SkuMapping 唯一索引冲突", True, f"IntegrityError: {message[:100]}")
            return
        _record("2. SkuMapping 唯一索引冲突", False, "重复键竟然插入成功！")


async def check_soft_delete_frees_key(session_factory: async_sessionmaker) -> None:
    """软删除后唯一键应被释放（部分索引的核心价值）。"""
    async with session_factory() as session:
        mapping = (
            await session.execute(
                __import__("sqlalchemy").select(SkuMapping).where(SkuMapping.shop_sku_code == "SKU-RED-XL")
            )
        ).scalars().first()
        if mapping is None:
            _record("3. 软删除释放唯一键", False, "未找到前一步插入的映射")
            return
        mapping.mark_deleted(operator="verify", reason="自检脚本")
        await session.commit()

        reborn = SkuMapping(
            platform="taobao",
            shop_id="shop-1",
            shop_item_id="ITEM-1",
            shop_sku_code="SKU-RED-XL",
            purchase_cost_cents=1400,
            status=MappingStatus.VALID.value,
        )
        session.add(reborn)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            _record("3. 软删除释放唯一键", False, f"软删除后仍冲突：{str(exc).splitlines()[0]}")
            return
        _record("3. 软删除释放唯一键", True, "删除后可重建同键映射（id=%d）" % reborn.id)


def check_scope_guard() -> None:
    """★ 红线 R1：越权 scope 必须被拒绝。"""
    try:
        enforce_scope(["item.write", "order.read"], adapter_name="evil_adapter", actor="verify")
    except ScopeViolationError as exc:
        ok = int(exc.code) == int(ErrorCode.ADAPTER_SCOPE_DENIED)
        _record(
            "4. enforce_scope(['item.write']) 拒绝",
            ok,
            f"code={int(exc.code)} forbidden={exc.forbidden_scopes} http={exc.http_status}",
        )
    else:
        _record("4. enforce_scope(['item.write']) 拒绝", False, "越权 scope 竟然通过了！")

    for forbidden in ("item.create", "item.update", "price.update", "product.write"):
        try:
            enforce_scope([forbidden], adapter_name="evil_adapter", actor="verify")
        except ScopeViolationError:
            continue
        _record(f"4b. enforce_scope(['{forbidden}']) 拒绝", False, "未被拒绝")
        return
    _record("4b. 全部黑名单 scope 均被拒绝", True, "item.create/item.update/price.update/product.write")

    granted = enforce_scope(["order.read", "logistics.write"], adapter_name="local_csv", actor="verify")
    _record("4c. 白名单 scope 通过", set(granted) == {"order.read", "logistics.write"}, f"granted={sorted(granted)}")

    try:
        enforce_scope(["inventory.read"], adapter_name="unknown_adapter", actor="verify")
    except ScopeViolationError as exc:
        _record("4d. 白名单外 scope 被拒绝", True, f"unknown={exc.unknown_scopes}")
    else:
        _record("4d. 白名单外 scope 被拒绝", False, "白名单外 scope 竟然通过了")


async def check_factory_default(session_factory: async_sessionmaker) -> None:
    """★ 工厂默认返回 LocalCsvAdapter，且三个适配器均已注册。"""
    # 触发注册
    FulfillmentAdapterFactory.registered_names()
    names = sorted(ADAPTER_REGISTRY.keys())
    ok = set(names) == {AdapterName.MIAOSHOU.value, AdapterName.YITAO.value, AdapterName.LOCAL_CSV.value}
    _record("5. 三个适配器注册", ok, f"registered={names}")

    async with session_factory() as session:
        adapter = await FulfillmentAdapterFactory.create(session=session, actor="verify")
        is_local = isinstance(adapter, LocalCsvAdapter)
        _record(
            "6. factory.create() 默认返回 LocalCsvAdapter",
            is_local,
            f"{type(adapter).__name__} / adapter_name={adapter.adapter_name}",
        )

        # 越权适配器：把**默认那条** local_csv 的声明 scope 改成含 item.write，工厂必须拒绝。
        # ★ 早期这里是「再插一条同名 local_csv」，在 `adapter_name` 唯一约束下
        #   只有当库里本来没有该行时才插得进去 —— 也就是说这条校验能过，
        #   靠的恰恰是"适配器配置行没落库"（QA-05 的那个病）。
        #   现在自检库按生产一样播种了基础数据，必须改成**就地改**再还原。
        from sqlalchemy import select

        from app.models.order import FulfillmentAdapterConfig

        evil_row = (
            await session.execute(
                select(FulfillmentAdapterConfig).where(
                    FulfillmentAdapterConfig.adapter_name == "local_csv"
                )
            )
        ).scalars().first()
        assert evil_row is not None, "自检库缺少 local_csv 配置行（bootstrap 没生效）"
        original_scopes = list(evil_row.declared_scopes_json or [])
        evil_row.declared_scopes_json = ["item.write", "order.read"]
        await session.commit()
        try:
            await FulfillmentAdapterFactory.create(session=session, actor="verify")
        except ScopeViolationError as exc:
            _record("6b. 工厂拦截越权适配器", True, f"code={int(exc.code)} msg={exc.message[:60]}")
        else:
            _record("6b. 工厂拦截越权适配器", False, "带 item.write 的适配器竟然通过了工厂校验")
        finally:
            evil_row.declared_scopes_json = original_scopes
            await session.commit()


async def check_unsupported_capability(session_factory: async_sessionmaker) -> None:
    """★ 红线 R3：UNSUPPORTED 能力返回信封，绝不抛异常。"""
    async with session_factory() as session:
        miaoshou = await FulfillmentAdapterFactory.create(
            AdapterName.MIAOSHOU.value, session=session, actor="verify"
        )
        yitao = await FulfillmentAdapterFactory.create(AdapterName.YITAO.value, session=session, actor="verify")
        local = await FulfillmentAdapterFactory.create(
            AdapterName.LOCAL_CSV.value, session=session, actor="verify"
        )

        request = MatchSkuRequest(platform="taobao", shop_id="shop-1", shop_sku_code="SKU-RED-XL")
        results = {
            "miaoshou.match_sku": await miaoshou.invoke(Capability.MATCH_SKU, req=request),
            "yitao.match_sku": await yitao.invoke(Capability.MATCH_SKU, req=request),
            "yitao.submit_refund": await yitao.invoke(Capability.SUBMIT_REFUND, req=None),
            "local.write_back_tracking": await local.invoke(
                Capability.WRITE_BACK_TRACKING,
                req=WriteBackRequest(
                    order_id=1,
                    platform_order_no="NO-1",
                    shop_item_id="ITEM-1",
                    logistics_company="顺丰",
                    tracking_no="SF123",
                ),
            ),
        }
        all_unsupported = True
        details: list[str] = []
        for label, result in results.items():
            is_unsupported = result.code.value == "UNSUPPORTED" and not result.ok
            all_unsupported = all_unsupported and is_unsupported
            details.append(f"{label}={result.code.value}")
        _record("7. UNSUPPORTED 能力不抛异常", all_unsupported, " ; ".join(details))

        # 网络/配置缺失的能力 → 返回 RETRYABLE/FATAL 信封，同样不抛异常
        from app.adapters.fulfillment.base import FetchOrdersRequest

        miaoshou_orders = await miaoshou.invoke(
            Capability.FETCH_ORDERS,
            req=FetchOrdersRequest(shop_ids=["shop-1"], updated_from="2026-01-01T00:00:00Z"),
        )
        _record(
            "7b. 未配置端点时也不抛异常",
            miaoshou_orders.code.value in {"UNSUPPORTED", "RETRYABLE", "FATAL"},
            f"code={miaoshou_orders.code.value} msg={miaoshou_orders.message[:60]}",
        )

        # ★ 红线 R2：履约适配器不得有写店铺商品的方法
        forbidden_methods = ("offline", "update_stock_price")
        violations: list[str] = []
        for cls in (MiaoshouAdapter, YitaoAdapter, LocalCsvAdapter):
            for method in forbidden_methods:
                if hasattr(cls, method):
                    violations.append(f"{cls.__name__}.{method}")
        _record("8. 红线 R2：履约适配器无 offline/update_stock_price", not violations, ",".join(violations) or "clean")

        # 能力矩阵可序列化
        matrix = miaoshou.capability_matrix()
        _record("8b. 能力矩阵可序列化", len(matrix) == len(CapabilityEnum), f"capabilities={len(matrix)}")


async def check_task_runner(session_factory: async_sessionmaker) -> None:
    """TaskRunner：提交 → 执行 → 成功；幂等键去重。"""
    runner = LocalTaskRunner(max_workers=2)

    @task_handler("verify_demo")
    async def _handler(payload: dict, ctx) -> dict:  # noqa: ANN001
        return {"echo": payload.get("n", 0), "task_id": ctx.task_id}

    runner.register_handler("verify_demo", _handler)

    task_id = await runner.submit("verify_demo", {"n": 7}, task_key="verify:demo")
    for _ in range(50):
        await asyncio.sleep(0.1)
        status = await runner.get_status(task_id)
        if status["status"] in {"success", "failed"}:
            break

    status = await runner.get_status(task_id)
    ok = status["status"] == "success" and (status.get("result") or {}).get("echo") == 7
    _record("9. TaskRunner 提交并执行", ok, f"status={status['status']} result={status.get('result')}")

    # 幂等：活跃任务期间重复提交同一 task_key 必须返回同一个 task_id
    @task_handler("verify_slow")
    async def _slow_handler(payload: dict, ctx) -> dict:  # noqa: ANN001
        await asyncio.sleep(1.5)
        return {"slow": True}

    runner.register_handler("verify_slow", _slow_handler)
    slow_first = await runner.submit("verify_slow", {"n": 1}, task_key="verify:slow")
    slow_second = await runner.submit("verify_slow", {"n": 2}, task_key="verify:slow")
    _record("9b. TaskRunner 幂等键去重", slow_first == slow_second, f"first={slow_first} second={slow_second}")

    # 恢复钩子：把已存在记录重新入队（recovery.py 使用）
    runner._enqueue(slow_first, "verify_slow", "")  # noqa: SLF001
    _record("9c. TaskRunner 恢复入队钩子可用", True, f"enqueue task {slow_first}")

    await asyncio.sleep(2.0)
    runner.shutdown(wait=False)


def check_crypto_and_utils() -> None:
    """凭证 AES-256 加解密 + 掩码 + 规格指纹 + 规格树笛卡尔展开 + CSV。"""
    from app.adapters.source.alibaba1688 import expand_spec_tree
    from app.utils.crypto import decrypt_text, encrypt_text, is_encrypted, mask_secret
    from app.utils.csvio import export_csv, read_csv_dicts
    from app.utils.kit import spec_signature

    cipher = encrypt_text("app_secret_plain_123456")
    ok = is_encrypted(cipher) and decrypt_text(cipher) == "app_secret_plain_123456"
    _record(
        "11. 凭证 AES-256 加解密",
        ok,
        f"cipher={cipher[:24]}... masked={mask_secret('app_secret_plain_123456')}",
    )

    tree = {"颜色": ["红", "蓝"], "尺码": ["S", "XL"]}
    combos = expand_spec_tree(tree)
    ok = len(combos) == 4 and {"颜色": "红", "尺码": "XL"} in combos
    _record("12. 1688 规格树笛卡尔展开", ok, f"combos={combos}")

    sig1 = spec_signature({"颜色": "红", "尺码": "XL"})
    sig2 = spec_signature({"尺码": "XL", "颜色": "红"})
    ok = bool(sig1) and sig1 == sig2 and sig1 != spec_signature({"颜色": "红", "尺码": "L"})
    _record("13. 规格指纹（排序无关 / 值敏感）", ok, f"sig={sig1[:16]}")

    path = PROJECT_ROOT / "data" / "verify_export.csv"
    export_csv([{"a": 1, "b": "中文"}], path, headers=["a", "b"])
    rows = read_csv_dicts(path)
    ok = rows == [{"a": "1", "b": "中文"}]
    path.unlink(missing_ok=True)
    _record("14. CSV 导出导入（BOM / 中文）", ok, f"rows={rows}")


async def check_ai_and_manual() -> None:
    """AI 客户端工厂默认 mock + 半自动素材包生成 + 商品 ID 唯一性校验。"""
    from app.adapters.ai.base import AiTaskContext
    from app.adapters.ai.factory import AiClientFactory
    from app.adapters.listing.base import ListingPayload, ListingSkuPayload
    from app.adapters.listing.factory import ListingAdapterFactory
    from app.adapters.listing.manual import ManualListingAdapter
    from app.adapters.listing.mock import MockListingAdapter
    from app.core.errors import BusinessError, ErrorCode

    # ★ 显式要 mock：默认客户端已按用户决策改为 `file_bridge`（等 WorkBuddy 产出），
    #   而本项校验的意图是「mock 产出占位图」。若沿用默认，自检会卡在等产出上
    #   最长 `ai_poll_timeout_sec`（默认 1800s）—— 表现为"脚本跑一半不动了"。
    client = await AiClientFactory.create("mock")
    ctx = AiTaskContext(
        task_id="verify-ai-1",
        target_platform="taobao",
        original_title="纯棉短袖T恤",
        rework_items=["main_image", "title"],
        selling_points=["透气", "不起球"],
    )
    result = await client.rework_images(ctx)
    ok = client.client_name == "mock" and len(result.images) >= 1 and result.images[0].is_placeholder
    _record(
        "15. AI 客户端默认 mock 产出占位图",
        ok,
        f"client={client.client_name} images={len(result.images)} path={result.images[0].local_path if result.images else ''}",
    )

    title_result = await client.rewrite_title(ctx)
    _record("15b. AI 标题改写", bool(title_result.title), f"title={title_result.title}")

    # ★ 15c：默认客户端（file_bridge）的等待必须有**有限上界**。
    #   没有这条，任何一次"AI 没产出"都会让调用方干等满 1800s
    #   （任务线程卡住 → 进程/测试退不出去，见 `app/tasks/runner.py` 的踩坑说明）。
    from app.adapters.ai.base import AiTimeoutError
    from app.adapters.ai.file_bridge import WorkBuddyFileBridgeClient

    bridge = WorkBuddyFileBridgeClient(
        queue_dir=PROJECT_ROOT / "data" / "ai_queue",
        output_dir=PROJECT_ROOT / "data" / "ai_output",
        poll_timeout_sec=1.0,
        poll_interval_sec=0.2,
    )
    timed_out = False
    try:
        await bridge.wait_result("verify-no-such-task")
    except AiTimeoutError as exc:
        timed_out = True
        detail = str(exc)[:60]
    else:
        detail = "无产出却返回了结果 —— 等待没有上界"
    _record("15c. 文件桥等待有有限上界（超时抛 AiTimeoutError）", timed_out, f"detail={detail}")

    # Mock 上架适配器
    mock_adapter = await ListingAdapterFactory.create("taobao", "mock")
    publish_result = await mock_adapter.invoke(
        "publish",
        payload=ListingPayload(
            shop_id="shop-1",
            title="测试商品",
            selling_points=["卖点"],
            attributes_json={},
            category_id="123",
            main_images=[],
            detail_images=[],
            skus=[
                ListingSkuPayload(spec_json={"颜色": "红"}, sale_price_cents=3990, stock_qty=10,
                                 purchase_cost_cents=1250)
            ],
            source_product_id=1,
        ),
    )
    ok = (
        isinstance(mock_adapter, MockListingAdapter)
        and publish_result.ok
        and (publish_result.data is not None)
        and publish_result.data.is_mock
        and publish_result.data.shop_item_id.startswith("MOCK-")
    )
    _record(
        "16. MockListingAdapter 产出 is_mock 数据",
        ok,
        f"shop_item_id={publish_result.data.shop_item_id if publish_result.data else ''}",
    )

    # 半自动素材包
    manual = ManualListingAdapter(platform=__import__("app.models.enums", fromlist=["Platform"]).Platform.TAOBAO)
    package_result = await manual.build_manual_package(
        ListingPayload(
            shop_id="shop-1",
            title="半自动测试商品",
            selling_points=["卖点A"],
            attributes_json={"品牌": "X"},
            category_id="123",
            main_images=[],
            detail_images=[],
            skus=[ListingSkuPayload(spec_json={"颜色": "红"}, sale_price_cents=3990, stock_qty=10)],
            source_product_id=1,
        )
    )
    package_path = Path(package_result.data.package_path) if package_result.ok and package_result.data else None
    ok = bool(package_path and package_path.exists())
    _record("17. 半自动素材包 ZIP 生成", ok, f"path={package_path}")

    # 商品 ID 回填唯一性校验（4006）

    from app.models.listing import ListingProduct

    session_factory = await _prepare_db_cached()
    async with session_factory() as session:
        manual.session = session  # 回填唯一性校验需要数据库会话
        session.add(
            ListingProduct(
                platform="taobao", shop_id="shop-1", shop_item_id="REAL-ITEM-1", title="已存在", is_mock=False
            )
        )
        await session.commit()
        try:
            await manual.validate_shop_item_id("taobao", "shop-1", "REAL-ITEM-1")
        except BusinessError as exc:
            ok = int(exc.code) == int(ErrorCode.PUBLISH_ITEM_ID_DUPLICATE)
            _record("17b. 商品 ID 回填重复校验（4006）", ok, f"code={int(exc.code)} msg={exc.message[:50]}")
        else:
            _record("17b. 商品 ID 回填重复校验（4006）", False, "重复 ID 竟然通过了")
        # ★ 必须传 `skus` 且带 `sale_price`：LST-P0-07 规定**售价必填**，
        #   缺价商品会永久失去成本倒挂保护，所以适配器按 422 硬拒。
        #   早期这里只传 `sku_codes=["SKU-1"]`，在售价必填落地后会让自检直接崩掉。
        filled = await manual.fill_back_shop_item_id(
            platform="taobao",
            shop_id="shop-1",
            shop_item_id="REAL-ITEM-2",
            skus=[{"shop_sku_code": "SKU-1", "sale_price": "39.90"}],
        )
        _record("17c. 新商品 ID 回填成功", filled["shop_item_id"] == "REAL-ITEM-2", f"{filled}")

        # 17d：售价缺失必须被硬拒（只传 sku_codes 的老用法）
        try:
            await manual.fill_back_shop_item_id(
                platform="taobao", shop_id="shop-1", shop_item_id="REAL-ITEM-3", sku_codes=["SKU-1"]
            )
        except BusinessError as exc:
            _record(
                "17d. 售价缺失被硬拒（LST-P0-07）",
                int(exc.code) == int(ErrorCode.PARAM_ERROR),
                f"code={int(exc.code)} msg={exc.message[:50]}",
            )
        else:
            _record("17d. 售价缺失被硬拒（LST-P0-07）", False, "没有售价竟然回填成功了")


def check_conflict_queries() -> None:
    """冲突检测 SQL 常量可按方言取用（★ 只保留**真实可命中**的检测器）。

    ★★ QA-01 / QA-02 结论 ★★
        `one_to_many` 与 `duplicate_item` 的 GROUP BY 完整包含部分唯一索引
        `uq_sku_mapping_shop_sku` 的键列 ⇒ `HAVING COUNT(DISTINCT ...) > 1` 恒为假
        ⇒ 永远命中 0 行。保留一条永远查不到东西的检测，比没有检测更危险
        （它会让人误以为有保护），因此已移除，改由**写入边界**的 409 阻断。
        本检查因此**同时**断言这两条**不在**返回表里 —— 防止有人把它们加回来。
    """
    sqlite_queries = get_conflict_queries("sqlite")
    pg_queries = get_conflict_queries("postgresql")

    live = {"many_to_one", "duplicate", "cost_invalid", "cost_underwater", "spec_mismatch"}
    retired = {"one_to_many", "duplicate_item"}

    ok = live.issubset(set(sqlite_queries)) and live.issubset(set(pg_queries))
    ok = ok and not (retired & set(sqlite_queries)) and not (retired & set(pg_queries))
    ok = ok and "GROUP_CONCAT" in sqlite_queries["duplicate"]
    ok = ok and "STRING_AGG" in pg_queries["duplicate"]
    ok = ok and "FALSE" in pg_queries["cost_invalid"] and "is_deleted = 0" in sqlite_queries["cost_invalid"]
    _record(
        "10. 五类（真实可命中的）冲突检测 SQL 常量 + 两条死 SQL 已移除",
        ok,
        f"keys={sorted(sqlite_queries)} retired_absent={sorted(retired - set(sqlite_queries))}",
    )


async def check_audit_written(session_factory: async_sessionmaker) -> None:
    """★ 越权被拒必须落审计（§10.7 第 3 条）。"""
    from sqlalchemy import func, select

    from app.models.enums import AuditActionType
    from app.models.system import AuditLog

    async with session_factory() as session:
        total = (await session.execute(select(func.count()).select_from(AuditLog))).scalar_one()
        denied = (
            await session.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action_type == AuditActionType.PERMISSION_CHANGE.value)
            )
        ).scalar_one()
    _record("18. 越权拦截落审计日志", denied > 0, f"audit_log 总数={total}，permission_change={denied}")


async def main() -> int:
    """执行全部验证，返回退出码。"""
    print("=" * 78)
    print("自用电商 ERP · 基础设施自检（T-A01 ~ T-A04）")
    print(f"验证数据库：{VERIFY_DB}")
    print("=" * 78)

    check_import_app()
    session_factory = await _prepare_db()
    await check_unique_index(session_factory)
    await check_soft_delete_frees_key(session_factory)
    check_scope_guard()
    check_conflict_queries()
    check_crypto_and_utils()
    await check_factory_default(session_factory)
    await check_unsupported_capability(session_factory)
    await check_ai_and_manual()
    await check_task_runner(session_factory)
    await check_audit_written(session_factory)

    print("=" * 78)
    failed = [name for name, ok, _ in _results if not ok]
    print(f"总计 {len(_results)} 项，失败 {len(failed)} 项")
    if failed:
        for name in failed:
            print(f"  - {name}")
        print("结果：FAIL")
    else:
        print("结果：PASS（全部通过）")
    print("=" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
