"""★ QA 探针：请求级会话是否自动提交？

发现 `app/core/database.py:get_db()` 只 rollback / close，**从不 commit**。
因此任何「依赖隐式提交」的接口，其写入会被静默丢弃 ——
正是「不报错、只静默失效」的温床。

本探针用 AST 扫描 app/api/v1/*.py：
  找出「路由处理函数里出现了 session.add / 直接改 ORM 对象，但从未 session.commit()」的端点。
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
API_DIR = BACKEND / "app" / "api" / "v1"

SEP = "=" * 96


def banner(t: str) -> None:
    """分节标题。"""
    print(f"\n{SEP}\n{t}\n{SEP}")


def is_route(node: ast.AST) -> bool:
    """判断是否为路由处理函数（带装饰器）。"""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    for dec in node.decorator_list:
        src = ast.unparse(dec)
        if ".get(" in src or ".post(" in src or ".put(" in src or ".patch(" in src or ".delete(" in src:
            return True
    return False


def main() -> None:
    """主入口。"""
    banner("扫描 app/api/v1/*.py：路由处理函数是否 commit")
    risky: list[tuple[str, str, int, list[str]]] = []
    total = 0

    for py in sorted(API_DIR.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not is_route(node):
                continue
            total += 1
            src = ast.unparse(node)
            writes: list[str] = []
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                    if sub.func.attr == "add" and "session" in ast.unparse(sub.func.value):
                        writes.append("session.add")
                    if sub.func.attr == "delete" and "session" in ast.unparse(sub.func.value):
                        writes.append("session.delete")
            # 直接对 ORM 对象赋值（obj.field = ...）也算写
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign):
                    for tgt in sub.targets:
                        if isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name):
                            if tgt.value.id not in {"self", "params", "payload", "body"}:
                                writes.append(f"{tgt.value.id}.{tgt.attr}")
            committed = "session.commit()" in src.replace(" ", "")
            flushed = "session.flush()" in src.replace(" ", "")
            if writes and not committed:
                risky.append((py.name, node.name, node.lineno,
                              sorted(set(writes))[:4] + (["(仅 flush)"] if flushed else [])))

    print(f"  路由处理函数总数: {total}")
    print(f"  存在写操作但**从未 commit** 的端点: {len(risky)}\n")
    for fname, fn, lineno, w in risky:
        print(f"  ⚠ {fname}:{lineno}  {fn}()")
        print(f"        写操作: {w}")
        print(f"        是否 flush: {'是' if '(仅 flush)' in w else '否'}")

    if not risky:
        print("  ✅ 未发现依赖隐式提交的写接口")

    banner("结论")
    print("  get_db() 的实现只做 rollback / close，不 commit：")
    print("    async def get_db():")
    print("        session = factory()")
    print("        try:    yield session")
    print("        except: await session.rollback(); raise")
    print("        finally: await session.close()")
    print("\n  → 凡是不显式 commit 的写接口，其落库都是「看起来成功、实际丢弃」。")


if __name__ == "__main__":
    main()
