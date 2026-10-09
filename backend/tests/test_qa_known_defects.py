"""★ QA 补充用例：把「已确认缺陷」钉死成回归测试。

来源：独立验证（严过关 / Yan）实测，非实现团队自报。

★ 关于 `xfail` 标记的历史
    这些用例断言的是**正确的**业务语义（与 PRD / ARCHITECTURE 一致），
    当初实现对不上，属于**源码缺陷**，因此先以 `xfail` 登记：
        - 未修时：XFAIL（缺陷被显式记录，不再躲在「56 个绿灯」背后）
        - 修好之后：XPASS（pytest 明确提示「预期失败却通过了」，提醒摘标记）

★★ 当前状态：全部五条（QA-01 / QA-02 / QA-03 / QA-05 / QA-13）**均已修复**，
   `xfail` 标记**已全部摘除** —— 本文件现在是零 xfail 的常驻断言集 ★★
    只要有人把保护改回去（重新加回恒空的扫描 SQL、删掉 pending_confirm 置位、
    让适配器行退化成「只 flush 不 commit」、或把"读不到 scope"当空 scope 放行），
    对应那条会**立刻变红**。

覆盖的缺陷：
    QA-01  one_to_many     检测器空转（部分唯一索引导致 HAVING 恒假）
                           → 处置：删除死 SQL，改由**写入边界**唯一约束阻断（409）
    QA-02  duplicate_item  检测器空转（同上）
    QA-03  spec_mismatch   检出后映射不转 pending_confirm → 订单照发
    QA-05  PUT /adapters/fulfillment/{name}/config 永远 404（适配器行不落库）
    QA-13  load_config 读不到 / 读失败 → 空 scope → R1 静默放行
                           → 处置：fail-closed，无法确认权限 = 拒绝
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.errors import BusinessError, ErrorCode
from app.models.enums import MappingStatus
from app.models.mapping import (
    CONFLICT_TYPES_REQUIRING_PENDING_CONFIRM,
    SkuMapping,
    WRITE_BOUNDARY_GUARDED_TYPES,
    get_conflict_queries,
)
from app.models.source import SourceProduct, SourceSku

# ★ 必须在**模块顶层**导入，不能放进 async 用例体内：
#   `tests.conftest` 在用例体内首次导入时会在运行中的事件循环里执行
#   `asyncio.run(_init_db())` ⇒ `RuntimeError: asyncio.run() cannot be called
#   from a running event loop`（这条坑以前让它伪装成 XFAIL）。
from tests.conftest import ADMIN_HEADERS  # noqa: E402

pytestmark = pytest.mark.asyncio


# ----------------------------------------------------------------------
#  共用构造
# ----------------------------------------------------------------------
async def _seed_source(session: object, *, cost: int = 1200) -> tuple[int, int]:
    """建货源商品 + 首个 SKU，返回 `(product_id, sku_id)`。"""
    product = SourceProduct(product_1688_id="1688-QA-DEAD", title="QA 死检测器")
    session.add(product)
    await session.flush()
    sku = SourceSku(
        source_product_id=int(product.id), sku_code_1688="SKU-QA-A",
        spec_json={"颜色": "红"}, spec_signature="sig-qa",
        cost_price_cents=cost, stock_qty=10, status="on_sale",
    )
    session.add(sku)
    await session.flush()
    return int(product.id), int(sku.id)


def _mk(session: object, *, product_id: int, sku_id: int, shop_sku_code: str,
        shop_item_id: str = "item-qa", source_sku_code: str = "SKU-QA-A",
        spec_signature: str = "sig-qa", status: str = "valid") -> SkuMapping:
    """构造映射并加入会话。"""
    m = SkuMapping(
        platform="taobao", shop_id="shop-qa", shop_item_id=shop_item_id,
        shop_sku_code=shop_sku_code, source_product_id=product_id, source_sku_id=sku_id,
        source_sku_code_1688=source_sku_code, purchase_cost_cents=1200,
        spec_signature=spec_signature, status=status, cost_source="auto", source="manual",
    )
    session.add(m)
    return m


def _create_payload(*, shop_sku_code: str, shop_item_id: str = "item-qa",
                    source_product_id: int, source_sku_id: int,
                    source_sku_code: str = "SKU-QA-A") -> object:
    """构造 `SkuMappingCreate`（创建映射的入参）。"""
    from app.schemas.mapping import SkuMappingCreate

    return SkuMappingCreate(
        platform="taobao",
        shop_id="shop-qa",
        shop_item_id=shop_item_id,
        shop_sku_code=shop_sku_code,
        source_product_id=source_product_id,
        source_sku_id=source_sku_id,
        source_sku_code_1688=source_sku_code,
        purchase_cost="12.00",
        status="valid",
    )


# ----------------------------------------------------------------------
#  QA-01：one_to_many —— 不再靠恒空的扫描 SQL，而由写入边界唯一约束阻断
# ----------------------------------------------------------------------
async def test_one_to_many_blocked_at_write_boundary(session: object) -> None:
    """「一平台 SKU → 多货源 SKU」必须在**写入边界**被拒，而不是靠一条永远查不到东西的 SQL。

    QA-01 原状：扫描 SQL 的 `GROUP BY (platform, shop_id, shop_item_id, shop_sku_code)`
    完整包含唯一索引 `uq_sku_mapping_shop_sku` 的键列 ⇒ 每组最多 1 行 ⇒
    `HAVING COUNT(DISTINCT source_sku_id) > 1` **恒为假** ⇒ 永远命中 0 行。
    本用例断言三件事：
        1. 那条死 SQL 已**不再存在**（留着它比没有更危险：会让人误以为有保护）；
        2. 服务层创建第二条同键映射 → **409 / 1006 明确业务错误**；
        3. 绕过服务层直接写库，数据库唯一索引**仍然挡得住**（不是纸面保护）。
    """
    from app.services.mapping_service import MappingService

    # ① 死 SQL 已删除
    queries = get_conflict_queries("sqlite")
    assert "one_to_many" not in queries, (
        "one_to_many 的恒空扫描 SQL 又回来了：它的 GROUP BY 含唯一键，永远命中 0 行"
    )
    assert "one_to_many" in WRITE_BOUNDARY_GUARDED_TYPES

    product_id, sku_a = await _seed_source(session)
    sku_b = SourceSku(
        source_product_id=product_id, sku_code_1688="SKU-QA-B",
        spec_json={"颜色": "蓝"}, spec_signature="sig-qa-b",
        cost_price_cents=1300, stock_qty=10, status="on_sale",
    )
    session.add(sku_b)
    await session.flush()

    # 第一条：正常创建
    await MappingService.create(
        session,
        _create_payload(shop_sku_code="SKU-QA-SHOP", source_product_id=product_id,
                        source_sku_id=sku_a, source_sku_code="SKU-QA-A"),
        operator="qa",
    )
    await session.flush()

    # ② 第二条同键映射 → 明确的业务错误（**不是 500**）
    with pytest.raises(BusinessError) as excinfo:
        await MappingService.create(
            session,
            _create_payload(shop_sku_code="SKU-QA-SHOP", source_product_id=product_id,
                            source_sku_id=int(sku_b.id), source_sku_code="SKU-QA-B"),
            operator="qa",
        )
    assert int(excinfo.value.code) == int(ErrorCode.UNIQUE_CONFLICT), (
        f"撞唯一键应返回 1006，实际 {int(excinfo.value.code)}"
    )
    assert excinfo.value.http_status == 409
    assert "已存在" in excinfo.value.message, f"文案必须让运营看懂，实际：{excinfo.value.message}"

    # ③ 绕过服务层直写：数据库唯一索引仍然挡得住
    await session.rollback()
    _mk(session, product_id=product_id, sku_id=sku_a, shop_sku_code="SKU-QA-RAW",
        source_sku_code="SKU-QA-A")
    await session.flush()
    _mk(session, product_id=product_id, sku_id=int(sku_b.id), shop_sku_code="SKU-QA-RAW",
        source_sku_code="SKU-QA-B")
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


# ----------------------------------------------------------------------
#  QA-02：duplicate_item —— 同上，由写入边界唯一约束阻断
# ----------------------------------------------------------------------
async def test_duplicate_item_blocked_at_write_boundary(session: object) -> None:
    """「同一 SKU 编码挂在多个商品下」必须在写入边界被拒（原扫描 SQL 同样恒为空）。"""
    from app.services.mapping_service import MappingService

    queries = get_conflict_queries("sqlite")
    assert "duplicate_item" not in queries, "duplicate_item 的恒空扫描 SQL 又回来了"
    assert "duplicate_item" in WRITE_BOUNDARY_GUARDED_TYPES

    product_id, sku_a = await _seed_source(session)

    await MappingService.create(
        session,
        _create_payload(shop_sku_code="SKU-QA-DUP", shop_item_id="item-qa-1",
                        source_product_id=product_id, source_sku_id=sku_a),
        operator="qa",
    )
    await session.flush()

    # 换个 shop_item_id 再建一条同编码映射 → 仍被拒（唯一索引不含 shop_item_id）
    with pytest.raises(BusinessError) as excinfo:
        await MappingService.create(
            session,
            _create_payload(shop_sku_code="SKU-QA-DUP", shop_item_id="item-qa-2",
                            source_product_id=product_id, source_sku_id=sku_a),
            operator="qa",
        )
    assert int(excinfo.value.code) == int(ErrorCode.UNIQUE_CONFLICT)
    assert excinfo.value.http_status == 409

    await session.rollback()
    _mk(session, product_id=product_id, sku_id=sku_a,
        shop_sku_code="SKU-QA-DUP-RAW", shop_item_id="item-raw-1")
    await session.flush()
    _mk(session, product_id=product_id, sku_id=sku_a,
        shop_sku_code="SKU-QA-DUP-RAW", shop_item_id="item-raw-2")
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


# ----------------------------------------------------------------------
#  QA-03：spec_mismatch 检出后必须把映射转 pending_confirm
# ----------------------------------------------------------------------
async def test_spec_mismatch_turns_mapping_pending_confirm(session: object) -> None:
    """规格漂移被检出后，映射必须转 `pending_confirm`，否则订单会照发不误。"""
    from app.services.mapping_validator import MappingValidator

    assert "spec_mismatch" in CONFLICT_TYPES_REQUIRING_PENDING_CONFIRM

    product_id, sku_a = await _seed_source(session)
    m = _mk(session, product_id=product_id, sku_id=sku_a, shop_sku_code="SKU-QA-SPEC")
    await session.flush()

    # 货源侧改规格 → 指纹漂移
    sku = await session.get(SourceSku, sku_a)
    sku.spec_signature = "sig-qa-CHANGED"
    await session.flush()

    result = await MappingValidator.detect_conflicts(session, detect_all=True)
    await session.flush()
    await session.refresh(m)

    assert result["by_type"].get("spec_mismatch", 0) >= 1, "spec_mismatch 应被检出"
    assert not result["incomplete"], f"检测不应失败：{result.get('errors')}"
    assert m.status == MappingStatus.PENDING_CONFIRM.value, (
        f"spec_mismatch 已检出但映射 status 仍是 {m.status} —— "
        "订单匹配只看 status=='valid'，会拿规格已变的货源照发"
    )


# ----------------------------------------------------------------------
#  QA-05：适配器配置接口必须可达（不再 404）
# ----------------------------------------------------------------------
async def test_adapter_config_endpoint_is_reachable(client: object) -> None:
    """列出的适配器**每一个**都必须可以被配置（base_url / 凭证 / declared_scopes）。

    ★ 遍历断言，不只测一个 —— 旧缺陷是三个名字**全部** 404。
    ★ 本夹具（`tests/conftest.py`）已改为「每请求独立会话」，
      因此 GET 里 flush 出来的幻影行不会再被后续 PUT 看到；
      若有人把播种改回「只 flush 不 commit」，本用例会立刻变红。
    """
    listed = await client.get("/api/v1/adapters/fulfillment", headers=ADMIN_HEADERS)
    assert listed.status_code == 200
    names = [a["adapter_name"] for a in listed.json()["data"]["adapters"]]
    assert names, "适配器清单为空 —— 启动期 bootstrap 没落库"

    for name in names:
        resp = await client.put(
            f"/api/v1/adapters/fulfillment/{name}/config",
            json={"declared_scopes": ["order.read", "logistics.write"], "is_enabled": True},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200, (
            f"PUT config {name} -> {resp.status_code} {resp.text[:120]} —— "
            "适配器行只 flush 不 commit，配置接口 100% 404"
        )


# ----------------------------------------------------------------------
#  QA-13：load_config 读不到 / 读失败 → 空 scope → R1 静默放行
# ----------------------------------------------------------------------
# ★ QA-13 已修复：xfail 标记摘除，转为常驻断言。
#   修复方式（fail-closed）：`load_config()` 用 `None` 明确表达「无法确认声明 scope」，
#   与「确实声明了零个 scope」区分开；`create()` 对 `None` 一律拒绝，绝不静默放行。
async def test_factory_rejects_when_declared_scopes_unknown(session: object) -> None:
    """库里已声明越权 scope 时，即使调用方没传 session，工厂也必须拒绝而不是静默放行。"""
    from app.adapters.fulfillment.factory import FulfillmentAdapterFactory
    from app.adapters.fulfillment.scope_guard import ScopeViolationError
    from app.models.enums import ScopeCheckStatus
    from app.models.order import FulfillmentAdapterConfig

    # ★ 启动期 bootstrap 已经把三个适配器配置行落库了（QA-05 修复），
    #   因此这里**改**已存在的那一行（真实场景：配置里被写成了越权 scope），
    #   不能再 `session.add()` 一条同名的 —— 会撞 `adapter_name` 唯一键。
    from sqlalchemy import select

    row = (
        await session.execute(
            select(FulfillmentAdapterConfig).where(FulfillmentAdapterConfig.adapter_name == "miaoshou")
        )
    ).scalars().first()
    if row is None:
        session.add(
            FulfillmentAdapterConfig(
                adapter_name="miaoshou",
                display_name="妙手",
                capability_json={},
                declared_scopes_json=["order.read", "item.write"],  # ★ 越权
                scope_check_status=ScopeCheckStatus.PASSED.value,
                is_enabled=True,
                is_active=False,
                priority=100,
            )
        )
    else:
        row.declared_scopes_json = ["order.read", "item.write"]  # ★ 越权
        row.scope_check_status = ScopeCheckStatus.PASSED.value
        row.is_enabled = True
    await session.flush()

    # 不传 session —— load_config 无法确认声明 scope，R1 必须**拒绝**（fail-closed），
    # 绝不能像旧实现那样把「读不到」当成「没申请权限」静默放行。
    with pytest.raises(ScopeViolationError):
        await FulfillmentAdapterFactory.create("miaoshou", actor="qa")


# ----------------------------------------------------------------------
#  QA-06：检测失败必须"可见"，不能与「没有冲突」混淆
# ----------------------------------------------------------------------
async def test_detection_failure_is_visible_not_silent(session: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """★ QA-06：检测器 SQL 崩了必须留下痕迹，绝不能假装"检测完成、没有冲突"。

    QA 实测到「同样的数据第一次检测全 0、第二次 spec_mismatch=1」——
    某次检测空转了，但返回体与"确实没有冲突"完全一样，运维无从察觉。
    本用例把 `_run()` 打成必炸，断言 `incomplete=True` 且 `errors` 非空。
    """
    import app.services.mapping_validator as validator_module
    from app.services.mapping_validator import MappingValidator

    # ★ 只把**检测器 SQL**换成一条必炸的语句，不动 `_run()` 本身
    #   （`_run()` 内部的 try/except 正是本次修复的对象，替换掉它就测不到修复了）。
    monkeypatch.setattr(
        validator_module,
        "get_conflict_queries",
        lambda dialect="sqlite": {"spec_mismatch": "SELECT * FROM 这张表根本不存在"},
    )

    result = await MappingValidator.detect_conflicts(session, detect_all=True)
    assert result["incomplete"] is True, (
        "检测器全崩了却仍报告 incomplete=False —— "
        "调用方会把「检测没跑起来」误读成「没有冲突」（QA-06 静默失效）"
    )
    assert result["errors"], "检测失败必须留下可读错误，不能静默返回 by_type 全 0"
    assert any("冲突检测" in str(e) for e in result["errors"])


async def test_detection_success_is_not_marked_incomplete(session: object) -> None:
    """对照用例：检测器正常跑完时 `incomplete` 必须为 False（否则"不完整"就失去区分度）。"""
    from app.services.mapping_validator import MappingValidator

    await _seed_source(session)
    result = await MappingValidator.detect_conflicts(session, detect_all=True)
    assert result["incomplete"] is False, f"检测正常完成却标记了 incomplete：{result.get('errors')}"


# ----------------------------------------------------------------------
#  QA-01 / QA-02 的「保护必须可验证」：约束自检 + `/health` 暴露
# ----------------------------------------------------------------------
async def test_required_constraints_are_verifiable(session: object) -> None:
    """删掉了恒空的扫描 SQL 之后，"索引还在不在"就是**唯一的**保护承载者。

    本用例断言：自检能确认索引存在（且是 UNIQUE + 部分索引）。
    若有人误删索引 / 迁移漏建，`check_required_constraints()` 必须**返回非空**。
    """
    from app.services.bootstrap import REQUIRED_CONSTRAINTS, check_required_constraints

    names = {str(c["name"]) for c in REQUIRED_CONSTRAINTS}
    assert "uq_sku_mapping_shop_sku" in names, "核心唯一索引必须纳入自检清单"

    issues = await check_required_constraints(session)
    assert issues == [], f"结构性约束缺失（保护不可用）：{[i['name'] for i in issues]}"


async def test_health_exposes_constraint_status(client: object) -> None:
    """`/health` 必须把约束自检结果暴露出来（缺失即 degraded），否则自检白做。"""
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()["data"]
    assert "constraints" in body, "/health 未暴露约束自检结果"
    assert body["constraints"]["ok"] is True, f"约束自检未通过：{body['constraints']}"
    assert body["constraints"]["missing_count"] == 0
    assert body["status"] == "ok"
