"""★ 第三轮 · QA-06 专测：检测失败必须与「没有冲突」可区分，且 fail-closed 禁止上架。

手法：把某一个检测器的 SQL 换成**必然执行失败**的语句（模拟表结构异常 / DB locked），
让 `MappingValidator._run()` 走到它的 except 分支，再检查：
    detect_conflicts() -> errors 非空 / incomplete=True
    validate()         -> blocking=True（fail-closed）
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.database import AsyncSessionLocal  # noqa: E402
from app.services import mapping_validator as mv  # noqa: E402

BROKEN_SQL = "SELECT * FROM __这张表不存在__"


def patch_one_detector(conflict_type: str) -> None:
    """把指定检测器的 SQL 换成必然失败的语句。"""
    original = mv.get_conflict_queries

    def patched(dialect: str = "sqlite") -> dict[str, str]:
        queries = dict(original(dialect))
        if conflict_type in queries:
            queries[conflict_type] = BROKEN_SQL
        return queries

    mv.get_conflict_queries = patched  # type: ignore[assignment]


async def main() -> None:
    """主流程。"""
    from app.services.mapping_validator import MappingValidator

    print("=" * 92)
    print("QA-06  检测失败可见性（把 spec_mismatch 的 SQL 换成必然失败的语句）")
    print("=" * 92)

    # 先取一个确实存在映射的 (product, platform, shop) 组合，保证对照组 passed=True
    from sqlalchemy import select

    from app.models.mapping import SkuMapping

    async with AsyncSessionLocal() as s:
        m = (await s.execute(
            select(SkuMapping).where(SkuMapping.is_deleted.is_(False))
            .order_by(SkuMapping.id)
        )).scalars().first()
        if m is None:
            print("  库里没有可用映射，跳过")
            return
        product_id = int(m.source_product_id or 0)
        platform, shop_id, code = m.platform, m.shop_id, m.shop_sku_code

    # ---------- 对照组：一切正常 ----------
    async with AsyncSessionLocal() as s:
        base = await MappingValidator.detect_conflicts(s, detect_all=True)
        print(f"  [对照组] detect_conflicts -> {json.dumps(base, ensure_ascii=False)}")

    # ---------- 实验组：一个检测器执行失败 ----------
    patch_one_detector("spec_mismatch")
    async with AsyncSessionLocal() as s:
        broken = await MappingValidator.detect_conflicts(s, detect_all=True)
        print(f"  [实验组] detect_conflicts -> {json.dumps(broken, ensure_ascii=False)}")
        inc = broken.get("incomplete")
        errs = broken.get("errors") or []
        print(f"\n  incomplete = {inc}")
        print(f"  errors     = {errs}")
        ok_distinguishable = bool(inc is True and errs)
        print(f"  => 「检测失败」与「未检出冲突」可区分: {'✅ 成立' if ok_distinguishable else '❌ 不成立'}")

        vo = await MappingValidator.validate(
            s, source_product_id=product_id, platform=platform, shop_id=shop_id, sku_codes=[code]
        )
        print(f"\n  validate -> passed={vo.passed} blocking={vo.blocking}")
        print(f"  blocked_reason = {getattr(vo, 'blocked_reason', '')}")
        print(f"  => 检测不完整时 fail-closed 禁止上架: {'✅ 成立' if vo.blocking is True else '❌ 不成立'}")


if __name__ == "__main__":
    asyncio.run(main())
