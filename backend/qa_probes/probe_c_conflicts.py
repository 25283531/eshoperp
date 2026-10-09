"""★ QA 独立探针 C：六类冲突检测是否为「空转」。

不复用实现团队的测试替身，做法：
  1. 在独立 scratch 库按 ORM metadata 建表（与真实库同 schema，含部分唯一索引）；
  2. 用 ORM 逐场景构造数据；
  3. 执行 app.models.mapping.get_conflict_queries() 返回的**真实检测 SQL**；
  4. 打印命中行数 —— 某类型在任何构造下都命中 0 行，说明它在当前 schema 下**永不触发**。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCRATCH = Path(tempfile.gettempdir()) / "qa_conflict_probe.db"

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{SCRATCH.as_posix()}"
os.environ["APP_ENV"] = "test"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["TASK_RECOVERY_ENABLED"] = "false"
os.environ["LOG_JSON"] = "false"

sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select, text  # noqa: E402

from app.core.database import dispose_engine, get_engine, get_session_factory  # noqa: E402
from app.models import Base  # noqa: E402
from app.models.listing import ListingProduct, ListingSku  # noqa: E402
from app.models.mapping import SkuMapping, get_conflict_queries  # noqa: E402
from app.models.source import SourceProduct, SourceSku  # noqa: E402

SEP = "=" * 96

ALL_TYPES = (
    "many_to_one",
    "one_to_many",
    "duplicate",
    "duplicate_item",
    "cost_invalid",
    "cost_underwater",
    "spec_mismatch",
)

# 类型 → (架构文档声明级别, 说明)
SUMMARY: dict[str, tuple[str, str]] = {
    "many_to_one": ("P1 不拦截", "跨平台铺货（用户主业务）"),
    "duplicate": ("P0 拦截", "同店铺内一个货源 SKU 对多个店铺 SKU"),
    "duplicate_item": ("P0 拦截", "同一 SKU 编码挂在多个商品下"),
    "one_to_many": ("P0 拦截", "一平台 SKU 映射多个货源 SKU"),
    "cost_invalid": ("P0 拦截", "成本为空 / 0 / 负"),
    "cost_underwater": ("P1 不拦截", "成本 >= 售价"),
    "spec_mismatch": ("P0 拦截", "规格指纹不一致"),
}

TRIGGERABLE: dict[str, bool] = {}


def banner(title: str) -> None:
    """分节标题。"""
    print(f"\n{SEP}\n{title}\n{SEP}")


def mapping(
    *,
    platform: str,
    shop_id: str,
    item_id: str,
    sku_code: str,
    source_sku_id: int = 1,
    source_code: str = "SKU-A",
    sig: str = "sig-A",
    cost: int = 5000,
    status: str = "valid",
    is_mock: bool = False,
) -> SkuMapping:
    """构造一个 SkuMapping ORM 对象（主键自增，避免手工 PK 冲突）。"""
    return SkuMapping(
        platform=platform,
        shop_id=shop_id,
        shop_item_id=item_id,
        shop_sku_code=sku_code,
        source_product_id=1,
        source_sku_id=source_sku_id,
        source_sku_code_1688=source_code,
        spec_signature=sig,
        purchase_cost_cents=cost,
        cost_source="auto",
        status=status,
        is_mock=is_mock,
        source="manual",
    )


async def probe(session, queries, ctype: str) -> list[dict]:
    """跑一条检测 SQL，返回命中行。"""
    res = await session.execute(text(queries[ctype]), {"min_profit_margin": 0.0})
    return [dict(r) for r in res.mappings().all()]


async def record(session, queries, ctype: str, *, expect: bool, label: str) -> list[dict]:
    """跑一条检测 SQL，记录可触发性并打印判定。"""
    rows = await probe(session, queries, ctype)
    hit = bool(rows)
    TRIGGERABLE[ctype] = TRIGGERABLE.get(ctype, False) or hit
    ok = hit == expect
    print(f"  {'[OK]' if ok else '[FAIL]'} {label}")
    print(f"        {ctype:18} 命中 {len(rows)} 行" + (f"  {rows}" if rows else ""))
    return rows


async def seed_base(session) -> None:
    """基础数据（货源商品 + 2 个货源 SKU），只执行一次。"""
    session.add(
        SourceProduct(
            product_1688_id="1688-A",
            title="女式针织开衫",
            cost_price_cents=5000,
            status="on_sale",
        )
    )
    session.add(
        SourceSku(
            source_product_id=1,
            sku_code_1688="SKU-A",
            spec_json={"颜色": "米白"},
            spec_signature="sig-A",
            cost_price_cents=5000,
            stock_qty=100,
            status="on_sale",
        )
    )
    session.add(
        SourceSku(
            source_product_id=1,
            sku_code_1688="SKU-B",
            spec_json={"颜色": "黑"},
            spec_signature="sig-B",
            cost_price_cents=3000,
            stock_qty=100,
            status="on_sale",
        )
    )
    await session.flush()


async def run() -> None:
    """主流程。"""
    if SCRATCH.exists():
        SCRATCH.unlink(missing_ok=True)
    engine = get_engine()
    queries = get_conflict_queries("sqlite")
    factory = get_session_factory()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async with factory() as s:
        await seed_base(s)
        await s.commit()

    # ------------------------------------------------------------------
    banner("C-1  ★ 三平台铺同一货源 SKU（跨平台铺货 = 用户主业务）")
    async with factory() as s:
        for plat, shop in [("taobao", "shop-tb"), ("douyin", "shop-dy"), ("pdd", "shop-pdd")]:
            s.add(mapping(platform=plat, shop_id=shop, item_id=f"item-{plat}", sku_code="SKU-A"))
        await s.flush()
        await record(s, queries, "many_to_one", expect=True, label="跨平台铺货必须检出 many_to_one（P1，不拦截）")
        for ctype in ("one_to_many", "duplicate", "duplicate_item", "cost_invalid", "spec_mismatch"):
            await record(s, queries, ctype, expect=False, label=f"{ctype} 必须为 0 行（不得误报）")
        await s.rollback()

    # ------------------------------------------------------------------
    banner("C-2  ★ 尝试构造 one_to_many：同一店铺 SKU 再映射第二个货源 SKU")
    async with factory() as s:
        s.add(mapping(platform="taobao", shop_id="shop-tb", item_id="item-taobao", sku_code="SKU-A"))
        await s.flush()
        src_b = (
            (await s.execute(select(SourceSku.id).where(SourceSku.sku_code_1688 == "SKU-B")))
            .scalars()
            .first()
        )
        try:
            s.add(
                mapping(
                    platform="taobao",
                    shop_id="shop-tb",
                    item_id="item-taobao",
                    sku_code="SKU-A",
                    source_sku_id=int(src_b),
                    source_code="SKU-B",
                    sig="sig-B",
                )
            )
            await s.flush()
            print("  [OK] 第二行插入成功（未违反约束）")
        except Exception as exc:  # noqa: BLE001
            print(f"  [BLOCKED] 第二行插入被数据库拒绝：{type(exc).__name__}")
            print(f"     {str(exc).splitlines()[0][:240]}")
            await s.rollback()
        await record(s, queries, "one_to_many", expect=True, label="one_to_many 能否被触发")
        await s.rollback()

    # ------------------------------------------------------------------
    banner("C-3  ★ 尝试构造 duplicate_item：同一 SKU 编码挂到第二个 shop_item_id")
    async with factory() as s:
        s.add(mapping(platform="taobao", shop_id="shop-tb", item_id="item-taobao", sku_code="SKU-A"))
        await s.flush()
        try:
            s.add(mapping(platform="taobao", shop_id="shop-tb", item_id="item-OTHER", sku_code="SKU-A"))
            await s.flush()
            print("  [OK] 第二行插入成功（唯一索引不含 shop_item_id，数据可共存）")
        except Exception as exc:  # noqa: BLE001
            print(f"  [BLOCKED] 第二行插入被数据库拒绝：{type(exc).__name__}")
            print(f"     {str(exc).splitlines()[0][:240]}")
            await s.rollback()
        await record(s, queries, "duplicate_item", expect=True, label="duplicate_item 能否被触发")
        await s.rollback()

    # ------------------------------------------------------------------
    banner("C-4  duplicate：同一店铺内一个货源 SKU 被 2 个店铺 SKU 引用")
    async with factory() as s:
        s.add(mapping(platform="taobao", shop_id="shop-tb", item_id="item-taobao", sku_code="SKU-A"))
        s.add(mapping(platform="taobao", shop_id="shop-tb", item_id="item-taobao", sku_code="SKU-A-CLONE"))
        await s.flush()
        await record(s, queries, "duplicate", expect=True, label="duplicate 能否被触发")
        await s.rollback()

    # ------------------------------------------------------------------
    banner("C-5  cost_invalid / spec_mismatch")
    async with factory() as s:
        s.add(mapping(platform="taobao", shop_id="shop-tb", item_id="item-cost0", sku_code="SKU-COST0", cost=0))
        s.add(mapping(platform="taobao", shop_id="shop-tb", item_id="item-neg", sku_code="SKU-NEG", cost=-100))
        await s.flush()
        await record(s, queries, "cost_invalid", expect=True, label="成本=0 与 成本=-100 → 应各命中一行")
        await s.rollback()

    async with factory() as s:
        s.add(mapping(platform="taobao", shop_id="shop-tb", item_id="item-stale", sku_code="SKU-STALE", sig="sig-OLD"))
        s.add(mapping(platform="taobao", shop_id="shop-tb", item_id="item-null", sku_code="SKU-NULLSIG", sig=""))
        await s.flush()
        await record(s, queries, "spec_mismatch", expect=True, label="指纹陈旧 + 指纹为空 → 应各命中一行")
        await s.rollback()

    # ------------------------------------------------------------------
    banner("C-6  cost_underwater 及其前置过滤")
    async with factory() as s:
        s.add(mapping(platform="taobao", shop_id="shop-tb", item_id="item-taobao", sku_code="SKU-A", cost=5000))
        s.add(
            ListingProduct(
                platform="taobao",
                shop_id="shop-tb",
                shop_item_id="item-taobao",
                source_product_id=1,
                title="针织开衫",
                status="on_sale",
                is_mock=False,
                listing_mode="manual",
            )
        )
        await s.flush()
        listing = (await s.execute(select(ListingProduct))).scalars().first()
        s.add(
            ListingSku(
                listing_product_id=int(listing.id),
                shop_sku_code="SKU-A",
                spec_json={"颜色": "米白"},
                sale_price_cents=1000,  # 售价 10 元 < 成本 50 元 → 真实倒挂
                status="on_sale",
            )
        )
        await s.flush()
        lsku = (await s.execute(select(ListingSku))).scalars().first()
        m1 = (await s.execute(select(SkuMapping))).scalars().first()
        m1.listing_sku_id = int(lsku.id)
        await s.flush()

        await record(s, queries, "cost_underwater", expect=True, label="成本 50 元 / 售价 10 元 → 应检出倒挂")

        lsku.sale_price_cents = 0
        await s.flush()
        rows = await probe(s, queries, "cost_underwater")
        verdict = "生效（代价：该商品永久失去倒挂检测）" if not rows else "失效，会大面积误报"
        print(f"  {'[OK]' if not rows else '[FAIL]'} 售价 = 0 时命中 {len(rows)} 行 → sale_price_cents>0 过滤{verdict}")

        lsku.sale_price_cents = 1000
        m1.is_mock = True
        await s.flush()
        rows = await probe(s, queries, "cost_underwater")
        print(
            f"  {'[OK]' if not rows else '[FAIL]'} is_mock = 1 时命中 {len(rows)} 行 → is_mock = 0 过滤"
            + ("生效" if not rows else "失效")
        )

        m1.is_mock = False
        m1.status = "pending_confirm"
        await s.flush()
        rows = await probe(s, queries, "cost_underwater")
        print(f"  [INFO] status = pending_confirm 时命中 {len(rows)} 行 → 倒挂只检测 status='valid'")
        await s.rollback()

    await dispose_engine()


def summary() -> None:
    """输出结论汇总。"""
    banner("C-7  结论汇总：各冲突检测 SQL 在当前 schema 下能否真实命中")
    print(f"  {'类型':18} {'文档级别':12} {'实测可触发':12} 说明")
    print("  " + "-" * 92)
    for ctype, (level, note) in SUMMARY.items():
        able = TRIGGERABLE.get(ctype, False)
        flag = "是" if able else "否（空转）"
        print(f"  {ctype:18} {level:12} {flag:12} {note}")
    dead = sorted(k for k, v in TRIGGERABLE.items() if not v)
    print()
    print("  ★ 根因分析：sku_mapping 上存在部分唯一索引")
    print("      CREATE UNIQUE INDEX uq_sku_mapping_shop_sku")
    print("        ON sku_mapping (platform, shop_id, shop_sku_code) WHERE is_deleted = 0")
    print("    one_to_many    的 GROUP BY = (platform, shop_id, shop_item_id, shop_sku_code)")
    print("    duplicate_item 的 GROUP BY = (platform, shop_id, shop_sku_code)")
    print("    两者都包含该唯一键 => 每个分组最多 1 行 => HAVING COUNT(DISTINCT ...) > 1 恒假。")
    print(f"    => 空转的检测器：{dead}")


if __name__ == "__main__":
    asyncio.run(run())
    summary()
