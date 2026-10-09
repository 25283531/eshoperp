"""★ QA 探针 G4 + H：独立实现，不复用实现团队的脚本。

G4  附录 A 第 18 条：利润计算禁止 join 回 sku_mapping / source_sku 取成本
    —— 独立用 AST 扫描（不是 grep，不是复用他们的实现）
H   前后端契约一致性：OpenAPI 端点  ↔  web/src/api/*.ts 实际调用
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
WEB = ROOT / "web"
SEP = "=" * 96


def banner(t: str) -> None:
    """分节标题。"""
    print(f"\n{SEP}\n{t}\n{SEP}")


# ======================================================================
#  G4：利润计算不得 join 回 sku_mapping / source_sku
# ======================================================================
def g4() -> None:
    """AST 扫描利润计算链。"""
    banner("G4  附录 A 第 18 条：历史订单利润只能读 order_item.purchase_cost_cents 快照")

    # ① 找出所有「利润计算」相关函数/文件
    targets = list((BACKEND / "app" / "services").rglob("*.py"))
    profit_files = [
        p for p in targets
        if re.search(r"profit|margin", p.read_text(encoding="utf-8"), re.I)
    ]
    print(f"  含 profit/margin 字样的 service 文件: {[p.name for p in profit_files]}")

    banned_tables = {"sku_mapping", "source_sku", "SkuMapping", "SourceSku"}
    violations: list[str] = []
    scanned: list[str] = []

    for py in sorted((BACKEND / "app").rglob("*.py")):
        src = py.read_text(encoding="utf-8")
        if not re.search(r"profit", src, re.I):
            continue
        scanned.append(py.name)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            # 函数/方法名含 profit
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and "profit" in node.name.lower():
                body = ast.dump(node)
                names = {
                    n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
                }
                names |= {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                hits = sorted(names & banned_tables)
                # join() 调用里的表名
                joins = [
                    ast.unparse(call) for call in ast.walk(node)
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "join"
                ]
                bad_joins = [j for j in joins if any(t.lower() in j.lower() for t in ("sku_mapping", "source_sku"))]
                status = "OK" if not hits and not bad_joins else "VIOLATION"
                if status == "VIOLATION":
                    violations.append(f"{py.name}:{node.lineno} {node.name} 引用 {hits} join={bad_joins}")
                print(f"  [{status:9}] {py.relative_to(BACKEND)}:{node.lineno}  {node.name}()")
                if hits:
                    print(f"              ↳ 引用了禁止表: {hits}")
                if bad_joins:
                    print(f"              ↳ 存在 join: {bad_joins}")

    print(f"\n  扫描 {len(scanned)} 个含 profit 的模块")
    if violations:
        print(f"  ❌ 发现 {len(violations)} 处违规：")
        for v in violations:
            print(f"     - {v}")
    else:
        print("  ✅ 未发现利润计算 join 回 sku_mapping / source_sku 的实现")

    # ② 反向确认：利润字段确实来自 order_item 快照
    banner("G4  反向确认：利润口径读取的字段")
    order_svc = BACKEND / "app" / "services" / "order_service.py"
    src = order_svc.read_text(encoding="utf-8")
    for m in re.finditer(r"purchase_cost_cents", src):
        line_no = src[: m.start()].count("\n") + 1
        line = src.splitlines()[line_no - 1].strip()
        print(f"  order_service.py:{line_no}  {line[:110]}")


# ======================================================================
#  H：前后端契约
# ======================================================================
def _openapi_calls() -> set[tuple[str, str]]:
    """从 openapi.json 提取 (method, path)。"""
    spec = json.loads((BACKEND / "qa_probes" / "openapi.json").read_text(encoding="utf-8"))
    out: set[tuple[str, str]] = set()
    for path, ops in spec.get("paths", {}).items():
        for method in ops:
            if method.lower() in {"get", "post", "put", "patch", "delete"}:
                out.add((method.upper(), path))
    return out


def _frontend_calls() -> set[tuple[str, str]]:
    """扫描 web/src/api/*.ts 中的实际调用。"""
    out: set[tuple[str, str]] = set()
    api_dir = WEB / "src" / "api"
    if not api_dir.exists():
        print(f"  ⚠ 前端目录不存在: {api_dir}")
        return out
    # 注意：泛型可能是嵌套的（http.get<PageResult<OrderVo>>('/orders')），
    # 用 <[^>]*> 会漏匹配，因此直接跳到第一个字符串字面量。
    pattern = re.compile(
        r"""(?:request|client|http|api)\s*\.\s*(get|post|put|patch|delete)\b[^`'\"]{0,80}[`'"]([^`'"]+)[`'"]""",
        re.I,
    )
    for ts in sorted(api_dir.glob("*.ts")):
        text = ts.read_text(encoding="utf-8")
        for m in pattern.finditer(text):
            method = m.group(1).upper()
            raw = m.group(2)
            path = raw if raw.startswith("/") else "/" + raw
            # 统一加 /api/v1 前缀（前端 baseURL 一般已含）
            norm = path if path.startswith("/api/v1") else "/api/v1" + path
            out.add((method, norm))
    return out


def _params_of(method: str, path: str) -> list[str]:
    """取 OpenAPI 中该端点的参数名（用于发现带变量的路径）。"""
    spec = json.loads((BACKEND / "qa_probes" / "openapi.json").read_text(encoding="utf-8"))
    return []


def h() -> None:
    """前后端契约 diff。"""
    banner("H  前后端契约一致性：OpenAPI ↔ web/src/api/*.ts")
    backend = _openapi_calls()
    frontend = _frontend_calls()
    print(f"  后端端点总数: {len(backend)}")
    print(f"  前端扫描到的调用: {len(frontend)}")

    # 用「路径模板」匹配：
    #   后端 OpenAPI 用 {violation_id} / {product_id} / {id} …
    #   前端 TS 用模板字面量 ${id} 或直接拼数字
    #   统一归一化为 {p} 后再比对，避免命名不一致造成误报
    def template(p: str) -> str:
        p = re.sub(r"\$\{[^}]*\}", "{p}", p)   # 前端模板字面量 ${id}
        p = re.sub(r"\{[^}]*\}", "{p}", p)     # 后端 OpenAPI 路径参数 {id}
        p = re.sub(r"/\d+(?=/|$)", "/{p}", p)  # 硬编码数字 id
        return p

    backend_tpl = {(m, template(p)) for m, p in backend}
    frontend_tpl = {(m, template(p)) for m, p in frontend}

    only_fe = sorted(frontend_tpl - backend_tpl)
    only_be = sorted(backend_tpl - frontend_tpl)

    print(f"\n  ---- 前端调用了但后端不存在（★ 缺陷）---- {len(only_fe)} 条")
    for m, p in only_fe:
        print(f"     ✗ {m:6} {p}")

    print(f"\n  ---- 后端有但前端未使用（不是缺陷，仅列出）---- {len(only_be)} 条")
    for m, p in only_be[:60]:
        print(f"     · {m:6} {p}")
    if len(only_be) > 60:
        print(f"     ... 其余 {len(only_be) - 60} 条省略")

    # ★ 专项核对任务点名的 5 个端点
    banner("H  专项核对：新增/变更过的 5 个端点是否真实存在")
    spec = json.loads((BACKEND / "qa_probes" / "openapi.json").read_text(encoding="utf-8"))
    paths = spec.get("paths", {})
    checks = [
        ("GET", "/api/v1/system/status-bar"),
        ("POST", "/api/v1/adapters/violations/{violation_id}/handle"),
        ("GET", "/api/v1/adapters/violations"),
        ("POST", "/api/v1/listing-products/{product_id}/fill-price"),
        ("GET", "/api/v1/listing-products"),
    ]
    for method, path in checks:
        ops = paths.get(path, {})
        exists = method.lower() in ops
        marks = "✅" if exists else "❌"
        extra = ""
        if exists:
            params = [p.get("name") for p in ops[method.lower()].get("parameters", [])]
            extra = f"  query参数={params}"
        print(f"  {marks} {method:5} {path}{extra}")

    # is_unhandled 参数
    ops = paths.get("/api/v1/adapters/violations", {}).get("get", {})
    names = [p.get("name") for p in ops.get("parameters", [])]
    print(f"  {'✅' if 'is_unhandled' in names else '❌'} GET /adapters/violations 的 is_unhandled 查询参数: {names}")

    # missing_price 参数
    ops = paths.get("/api/v1/listing-products", {}).get("get", {})
    names = [p.get("name") for p in ops.get("parameters", [])]
    print(f"  {'✅' if 'missing_price' in names else '❌'} GET /listing-products 的 missing_price 查询参数: {names}")

    # 前端枚举是否含 duplicate_item
    banner("H  ★ 前端冲突类型 key 是否含 duplicate_item")
    enums = WEB / "src" / "constants" / "enums.ts"
    if enums.exists():
        text = enums.read_text(encoding="utf-8")
        for key in ("one_to_many", "duplicate", "duplicate_item", "many_to_one",
                    "cost_invalid", "cost_underwater", "spec_mismatch"):
            found = key in text
            print(f"  {'✅' if found else '❌'} enums.ts 含 {key}")
        dup = re.search(r"duplicate_item[^,}\]]*", text)
        print(f"  duplicate_item 上下文: {dup.group(0)[:120] if dup else '（无）'}")
    else:
        print(f"  ⚠ 未找到 {enums}")


def main() -> None:
    """主入口。"""
    g4()
    h()


if __name__ == "__main__":
    main()
