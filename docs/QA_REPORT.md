# 独立验证报告（QA）

- **验证人**：严过关（Yan）· QA 工程师
- **验证对象**：自用电商 ERP（backend `backend/` · frontend `web/`）
- **验证方式**：独立实现探针，**不复用**实现团队的脚本；全程真实 HTTP / 真实 SQL / 真实 ORM
- **验证时间**：2026-10-08
- **环境**：Python 3.11 · SQLite `data/erp.db` · Node 22.22.2 · 服务端口 8131（`SCHEDULER_ENABLED=false TASK_RECOVERY_ENABLED=false`）

---

## TL;DR

> ### 🟢 第三轮最终复核后，结论已更新为「**有条件放行**」（详见 §六）
>
> 原 4 个 P0（QA-01 / QA-02 / QA-03 / QA-05）+ QA-06 + QA-13 **我逐一复检，全部真修好了**，
> 并且我用「反向验证」确认约束是**真的生效**（DROP 掉索引后 `/health` 会报缺失），不是嘴上说有。
> 新增的 `place_purchase` 链路、R1 全链路、AI 客户端选择也全部实测通过。
> **当前未闭合项：P0 = 0 · P1 = 0 · P2 = 3**（QA-07 / QA-09 / QA-12，均为纵深防御与可维护性，非可用性缺陷），
> 另有 **2 项放行前置条件**（进程必须重启 · 1688 采集必须回真实环境实测）。
>
> 下面第一轮的原始结论**按存档保留**，不代表当前状态。

**结论（第一轮）：不建议按现状发布。实现团队自报的「IS_PASS: YES / 30 个绿灯 / uvicorn 冒烟通过」与实测不符。**

自报的绿灯是「代码存在且能启动」的绿灯，不是「业务能跑通」的绿灯。我实测发现：

1. **全链路第一步（1688 采集）100% 失败**，且 AI 重构任务同样被它拖死 —— 源头就断了。
2. **6 类冲突里有 2 类是空转的死 SQL**（`one_to_many` / `duplicate_item`）—— 文档写 P0 拦截，实际永远命中 0 行，**不报错**。
3. **`spec_mismatch` 检测到了但映射不转 `pending_confirm`**，于是订单拿规格已变的货源照发 —— 这正是「不报错、只静默失效」的典型形态，且绕开了用户点名最高风险的 ORD-P0-03。
4. **第三方履约适配器在生产环境无法配置**（`PUT /adapters/fulfillment/{name}/config` 100% 404）。

同时必须说清楚：**实现团队在我验证期间修好了 5 个 P0**（见下表「已修复」），并且下面这些模块我实测是**真的能用**的：半自动上架主链路、三层成本语义 + 历史利润不可回溯改写、适配器热切换 + 在途订单渠道冻结、三条红线里的 R2/R3、顶栏零外部 HTTP、前后端契约、前端构建。这些不是敷衍出来的。

**缺陷分布（第二轮复测后）**：P0 **4** 个 · P1 **3** 个 · P2 **3** 个
**已修复/已关闭**：6 个（含本轮新修的 1688 采集、AI 事务、并发 500、越权告警闭环）——见 §5「第二轮复测」
**缺陷分布（第三轮最终复核后）**：P0 **0** 个 · P1 **0** 个 · P2 **3** 个 ——见 §六

> **第二轮复测说明**：实现团队在我提交报告后修了 DB 相对路径解析、AI 事务三段式、1688 采集等项，
> 我已在**25 表的根库 `data/erp.db`** 上全部重跑。结果：QA-04 已修复、QA-08/QA-11 关闭，
> **QA-01/02/03/05 依然存活**；并**新发现 QA-13**（`load_config` 吞异常 → R1 静默放行）。
> 详见 §四。**该轮发布判定已被 §六 覆盖。**

---

## 一、缺陷清单

### 1.1 缺陷总表（★ 以**第三轮最终复核（§六）**为准）

> ✅=已修/已关闭，❌=仍存活。前两轮的状态列保留作存档，**当前状态以最后一列为准**。

| 编号 | 级别 | 一句话 | 模块 | 二轮 | **三轮（现行）** |
|---|---|---|---|---|---|
| QA-01 | **P0** | `one_to_many` 检测器空转，永远命中 0 行 | `app/models/mapping.py` | ❌ | **✅ 已修** |
| QA-02 | **P0** | `duplicate_item` 检测器空转，永远命中 0 行 | `app/models/mapping.py` | ❌ | **✅ 已修** |
| QA-03 | **P0** | `spec_mismatch` 检出后映射不转 `pending_confirm`，订单照发 | `app/services/mapping_validator.py` | ❌ | **✅ 已修** |
| QA-05 | **P0** | 适配器配置行不落库 → `config` 404 → **连带 R1 红线与两层告警整体失效** | `app/services/fulfillment_service.py:79` | ❌ | **✅ 已修** |
| QA-06 | P1 | 冲突检测 SQL 异常被全量吞掉 → 检测静默空转 | `app/services/mapping_validator.py:523` | ❌ | **✅ 已修（fail-closed）** |
| QA-13 | P1 | `load_config()` 读失败 → 空 scope → **R1 静默放行** | `app/adapters/fulfillment/factory.py:86` | ❌ | **✅ 已修（无法确认=拒绝）** |
| QA-10 | P2 | 顶栏字段名与验收口径不一致 | `app/services/system_service.py` | ❌ | **✅ 已关闭**（字段已就位） |
| QA-08 | ~~P1~~ | 并发写冲突返回 HTTP 500 | `app/core/database.py` | ✅ | ✅ 不再复现 |
| QA-11 | ~~P2~~ | 越权告警闭环未证实 | `app/api/v1/adapters.py` | ✅ | ✅ 机制正常 |
| QA-04 | ~~P0~~ | 1688 采集 100% 失败 | `source_service.py:270` | ✅ | ✅ 代码已修，⚠ **真实网络未验**（见 §六 C2） |
| **QA-07** | **P2**（↓自 P1） | R1 可被绕过：脱离工厂直接实例化适配器不做 scope 校验 | `app/adapters/fulfillment/factory.py` | ❌ P1 | **❌ P2 — 降级**，`app/` 内 0 处绕过；`Capability` 8 项无商品写能力，绕过也调不到 |
| **QA-09** | **P2** | 工厂暴露 `strict_scope=False` 后门开关 | `app/adapters/fulfillment/factory.py` | ❌ | **❌ P2**，默认 True，`app/` 内 0 个调用点传 False（潜伏） |
| **QA-12** | **P2** | `publish` 走字符串派发，静态无法枚举唯一调用点 | `app/services/publish_service.py:383` | ❌ | **❌ P2**，`normalize_capability()` 已兜底，功能正确，仅静态可枚举性弱 |

**现行未闭合：P0 × 0 · P1 × 0 · P2 × 3**（QA-07 / QA-09 / QA-12），另加 **2 项放行前置条件**（§六 C1 / C2）。

### 1.2 我验证期间**已修复**的缺陷（复测通过）

| 编号 | 级别 | 问题 | 修复证据 |
|---|---|---|---|
| FIX-1 | P0 | `ManualListingAdapter` 收到 `str` 平台值 → `self.platform.value` → `AttributeError` → `form-data` / `package` 500 | 现在返回 `Platform.TAOBAO` 并成功构建表单 |
| FIX-2 | P0 | `AiTaskService` 调 `AiClientFactory.create(session)` → session 绑到 `name` → `1099 AI 客户端未注册` | 改为 `create(session=session)`，返回 `WorkBuddyFileBridgeClient` |
| FIX-3 | P0 | `FulfillmentAdapter.invoke()` 声明 `Capability` 但全部生产调用点传字符串 → `'str' object has no attribute 'value'`，所有 `order_sync` 失败 | 新增 `normalize_capability()`；现在 `order_sync` 任务状态 **success**（task 21/22/24/25） |
| FIX-4 | P0 | AI 重构成任务在长轮询期间持有写事务 → 全库写锁 | A/B 复测：B 组 3× `POST /publish-tasks` 全 202，C 组 `orders/sync` + `sku-mappings` 均成功 |
| FIX-5 | P0 | `scope_guard._write_audit()` 调 `audit_service.write(**payload)` 缺 `session` → 越权审计永不落库 | `audit_log` 已有 6 条 `permission_change` 记录（E-5 实测） |

---

## 二、缺陷详情（含复现步骤 / 证据 / 根因 / 建议）

> ### ⚠️ 本节为第一轮原始取证记录（存档）
> ### 修复后的复检结果在 **§四（第二轮）** 与 **§六（第三轮最终复核）**。
>
> 特别提醒 **QA-01 / QA-02**：我最初给的修复建议是「把 SQL 改对」，
> 实现团队最终采用的做法是**在写入边界用唯一约束硬阻断 + 把 IntegrityError 翻译成 409/1006**，
> 并**删掉**那两条恒空的读时扫描 SQL。我复检后认为**这个做法比我的原建议更正确**——
> 写入边界阻断是结构性保证，读时扫描只能事后发现。已在 §六 6.1 用**反向验证**（DROP 索引）确认生效。

### QA-01 / QA-02（P0）：两个冲突检测器是死 SQL

**现象**：`one_to_many`（一平台 SKU 映射多个货源 SKU，P0）与 `duplicate_item`（同一 SKU 编码挂多个商品，P0）**永远命中 0 行**。

**复现**（`qa_probes/probe_c_conflicts.py`，用 ORM 真实造数据后跑 ORM 里那条检测 SQL）：

```
C-7  结论汇总：各冲突检测 SQL 在当前 schema 下能否真实命中
  类型                 文档级别         实测可触发     说明
  many_to_one        P1 不拦截       是            跨平台铺货（用户主业务）
  duplicate          P0 拦截        是            同店铺内一个货源 SKU 对多个店铺 SKU
  duplicate_item     P0 拦截        否（空转）      同一 SKU 编码挂在多个商品下
  one_to_many        P0 拦截        否（空转）      一平台 SKU 映射多个货源 SKU
  cost_invalid       P0 拦截        是            成本为空 / 0 / 负
  cost_underwater    P1 不拦截       是            成本 >= 售价
  spec_mismatch      P0 拦截        是            规格指纹不一致
```

`one_to_many` 更狠：想造反例时数据库直接拒绝插入第二行
（`UNIQUE constraint failed`），也就是说这类冲突**在库里根本存不下**。

**根因**（实测确认）：`sku_mapping` 上有部分唯一索引

```sql
CREATE UNIQUE INDEX uq_sku_mapping_shop_sku
  ON sku_mapping (platform, shop_id, shop_sku_code) WHERE is_deleted = 0
```

而这两条检测 SQL 的 `GROUP BY` 分别是
`(platform, shop_id, shop_item_id, shop_sku_code)` 与 `(platform, shop_id, shop_sku_code)`，
**都包含该唯一键** ⇒ 每个分组最多 1 行 ⇒ `HAVING COUNT(DISTINCT ...) > 1` 恒为假。

**危害**：文档（ARCHITECTURE.md §4.3.1 / PRD MAP-P0-03）承诺这两类 P0 拦截，实际**静默不拦截**。
用户以为有保护，其实没有——标准的「不报错、只静默失效」。

**建议**：
- 要么承认这两类冲突在当前 schema 下不可能发生，把检测 SQL 删掉并在文档里写明「由唯一索引保证，无需检测」；
- 要么放行索引（例如把 `is_deleted=0` 的唯一约束改为应用层校验），让冲突真的能落库再被检出。
两者选一，**不能继续留着空转的检测器**。

---

### QA-03（P0）：`spec_mismatch` 检出后映射不转 `pending_confirm`，订单照发

**现象**：规格指纹漂移被检出（落了 level=P0 的 `mapping_conflict`），但 `sku_mapping.status` 仍是 `valid`；
于是 `local_csv.match_sku` 只看 `status == 'valid'` → `match_order` 判定 `matched` → 订单正常发货。

**复现**（`qa_probes/probe_i4_impact.py`，真实调用 `MappingValidator.detect_conflicts` + `OrderService.match_order`）：

```
场景：商品已上架并持续出单 → 货源侧悄悄把 XL 改成 XXL
  货源侧规格 XL → XXL，指纹 sig-v1 → sig-v2
  [旁证] spec_mismatch 原始 SQL 命中 1 行: [{'id': 3, 'shop_sku_code': 'QA-SPEC-SKU',
          'mapping_signature': 'sig-v1', 'current_signature': 'sig-v2', ...}]
  detect_conflicts(detect_all=True) -> {'detected': 1, 'by_type': {..., 'spec_mismatch': 1}}
  [旁证] mapping_conflict 落库 1 条: [{'conflict_type': 'spec_mismatch', 'level': 'P0', 'is_resolved': 0}]

  映射 status = valid
  ❌ 映射 status 仍是 valid —— ARCHITECTURE.md:715 要求「映射自动转 pending_confirm」未落地

  match_order -> matched=True  reason=''
  订单状态: {'fulfillment_status': 'matched', 'match_status': 'matched', 'exception_note': None}
```

**判定**：❌ P0 缺陷确认。冲突确实被检出了，但履约链路完全看不到它 —— 买家下单 XL，实际按 XXL 发货。

**对照**：如果映射状态**本来就是** `pending_confirm`，ORD-P0-03 是对的（见 I-1，订单正确挂起）。
所以问题不在挂起逻辑，而在**上游没把映射置为 `pending_confirm`**。

**根因**：`MappingValidator` 全程只写 `mapping_conflict`，从不写 `mapping.status`；
`app/services/` 里唯一会置 `pending_confirm` 的地方是 CSV 导入（`mapping_service.py:888`）。

**建议**：`detect_conflicts` 命中 `spec_mismatch`（及其他 P0）时，同步把 `sku_mapping.status` 置为
`pending_confirm` 并写变更日志，与 ARCHITECTURE.md:715「映射自动转 `pending_confirm`」对齐。

---

### QA-04（P0）：1688 采集 100% 失败，且 AI 重构任务被同一根因拖死

**现象**：`POST /source-products/collect` 受理 202，任务随即失败。

**复现**（真实 HTTP）：

```
POST /api/v1/source-products/collect -> 202 采集任务已受理
 task 26 source_collect failed | Alibaba1688Adapter.__init__() got an unexpected keyword argument 'session'
```

**更严重的是**：`POST /ai-tasks` 触发的任务里也出现了同一条错误 ——

```
task_failed error="Alibaba1688Adapter.__init__() got an unexpected keyword argument 'session'" task_id=27
```

**根因**：

```python
# app/services/source_service.py:270
adapter = Alibaba1688Adapter(session=session)

# app/adapters/source/alibaba1688.py:161
def __init__(self, *, app_key="", app_secret="", access_token="",
             endpoint=..., timeout_sec=20.0, max_retry=3) -> None:   # ← 没有 session
```

**危害**：采集是全链路第一步（1688 选品 → AI 改写 → 上架 → 映射 → 履约）。
这一步 100% 失败 = 整条链路没有输入。

**建议**：`Alibaba1688Adapter.__init__` 增加 `session` 关键字参数（与 `LocalCsvAdapter` 保持一致），
或调用点改为先构造再注入 session。

---

### QA-05（P0）：第三方履约适配器在生产环境无法配置

**现象**：`GET /adapters/fulfillment` 正常列出三个适配器，但 `PUT /adapters/fulfillment/{name}/config` 全部 404。

**复现**（真实 HTTP，同一个服务实例）：

```
GET  /api/v1/adapters/fulfillment -> 200  ['local_csv', 'miaoshou', 'yitao']
PUT  config local_csv  -> 404  履约适配器 local_csv 不存在
PUT  config miaoshou   -> 404  履约适配器 miaoshou 不存在
PUT  config yitao      -> 404  履约适配器 yitao 不存在
POST test  local_csv   -> 200  ok
POST test  miaoshou    -> 200  ok
POST test  yitao       -> 200  ok
```

**落库旁证**（同一时刻）：

```
BEFORE count: (0,)      ← data/erp.db 的 fulfillment_adapter 行数
API adapters: ['local_csv', 'miaoshou', 'yitao']
AFTER  count: (0,)      ← 请求结束仍然 0 行
```

**根因**：`FulfillmentService._ensure_rows()` 只 `session.add()` + `await session.flush()`，**不 commit**；
而 `get_db()`（`app/core/database.py:132`）也**从不 commit**（只 rollback / close）；
读接口 `list_adapters` 因此只在单次请求的内存里播种，下一个请求的 `update_config` 查不到任何行。

**★ 为什么 56 个绿灯没发现**：我补的回归用例 `test_adapter_config_endpoint_is_reachable` 在 pytest 里是 **XPASS** ——
因为 `tests/conftest.py` 把 `get_db` 覆盖成「一个用例共享一个会话」，GET 与 PUT 恰好共用同一个 session，
缺陷被夹具掩盖。证据：生产库 `fulfillment_adapter` 恒 0 行，测试库会被 PUT 顺带写出 1 行。

**危害**：妙手 / 逸淘永远配不上 `base_url` / 凭证 / `declared_scopes` ⇒ R1 的 scope 声明校验在真实配置路径上走不到（连带 QA-11）。

**建议**：`_ensure_rows()` 后显式 `commit`；或在应用启动时做一次带 commit 的播种。
同时建议 conftest 的 `get_db` 覆盖按请求粒度隔离，避免这类「共享会话掩盖缺陷」。

---

### QA-06（P1）：冲突检测 SQL 异常被全量吞掉

```python
# app/services/mapping_validator.py:523
async def _run(session, sql, params) -> list[dict]:
    """★ 防御性：任何异常都吞掉并返回空列表，绝不打断主流程。"""
    try:
        ...
    except Exception as exc:
        logger.warning("conflict_query_failed", error=str(exc), ...)
        return []
```

**实测遭遇**：同样一份数据，第一次 `detect_conflicts()` 返回全 0（含 `many_to_one` 也是 0），
第二次返回 `spec_mismatch: 1`。因为异常被吞成空列表，**运维完全无从察觉某次检测是空转的**。

**建议**：吞异常可以，但要（a）落一条 `mapping_check_failed` 审计，（b）在返回结构里带 `errors` 字段，
让「本次检测不完整」这件事**可见**。检测失败与「没有冲突」必须可区分。

---

### QA-07（P1）：R1 可被绕过

```
D-R1  ★ 绕过尝试 1：不走工厂，直接实例化 MiaoshouAdapter 并调用能力
  ⚠ 绕过工厂直接实例化成功：MiaoshouAdapter（未触发任何 scope 校验）
     declared_scopes 属性 = ★ 不存在
     实例上有无「已通过 scope 校验」标记：★ 无
     ⚠ 直接调 health_check() 成功 -> code=OK（★ R1 可被绕过）
```

`check_scope` / `enforce_scope` 本身的逻辑是对的（合法组合通过，`item.write` / `price.update` /
`item.offline` / 白名单外 scope 全部拒绝，拒绝时带 `code=5003`），**但只在工厂入口生效**。

**建议**：把 scope 校验下沉到 `FulfillmentAdapter.__init__` 或 `__post_init__`，
并在实例上留下「已校验」标记；`invoke()` 执行前复查该标记。

---

### QA-08（P1）：并发写冲突时返回 500，未降级

```
POST /api/v1/ai-tasks HTTP/1.1 500
  ERROR unhandled_exception  error='OperationalError'
  sqlite3.OperationalError: attempt to write a readonly database
```

发生在 AI 长轮询任务仍在 `running`、同时又有写请求进来时。
SQLite 单写者模型下这是可预期的，但对外暴露 500（而不是 503 + 重试提示）会让运营以为系统坏了。

**建议**：在数据库层统一把 `OperationalError`（locked / readonly）转成 503 + `Retry-After`，
并在前端提示「系统繁忙，请稍后重试」。

---

### QA-09 / QA-10 / QA-11 / QA-12（P2）

- **QA-09**：`FulfillmentAdapterFactory.create()` 暴露 `strict_scope=False` 开关。
  当前 `app/` 内无人使用，但红线留了后门。建议删除或改为仅测试环境可用。
- **QA-10**：`GET /system/status-bar` 实际返回字段名与验收口径不一致：

  | 验收口径 | 实际字段 |
  |---|---|
  | `violation_unhandled_count` | `unhandled_violation_count` |
  | `active_adapter` | `active_fulfillment_adapter` |
  | `health` | `health_status` |

  前端已按实际字段对接（H 段契约 diff 0 缺陷），所以**不是功能缺陷**，只是口径命名需对齐文档。
- **QA-11**：越权告警「0 → 1 → 处置后回落」的端到端闭环**未能证实** —— 触发路径
  `PUT /adapters/fulfillment/{name}/config` 被 QA-05 阻塞（404）。R1 的判定逻辑与审计落库均已单独验证通过。
- **QA-12**：`PublishService.execute()` 通过 `adapter.invoke('publish', ...)` 字符串派发，
  静态 grep `.publish(` 搜不到真实调用点，R2 的「上架唯一出口」只能靠人工保证。
  建议改为直接方法调用或至少加一处断言/测试锁定调用点数量。

---

## 三、A–I 分段实测输出

### A. 复现并怀疑他们的结论

| 项 | 结果 |
|---|---|
| `pytest` | **60 passed, 3 xfailed, 1 xpassed**（其中 3 个 xfail 是我新增的「已知缺陷」回归用例） |
| `ruff check app --select E9,F` | **All checks passed** |
| `ruff check app`（全量） | 195 项，绝大多数是风格问题，不作为卡口 |
| `alembic revision --autogenerate -m qa_drift` | 生成的 `upgrade()` / `downgrade()` **均为 `pass`** ⇒ ORM 与迁移**无漂移** ✅（临时迁移已删除） |
| uvicorn + 真实 HTTP | 超出 `scripts/smoke.py`，共发起 9 类探针、覆盖 110 个前端调用点 |

**测试质量审计**（重点查了 `test_mapping_validator.py` / `test_order_profit.py`）：
- 无 `assert True`、`pytest.skip`、TODO 占位等橡皮图章断言。
- `test_order_profit.py` 甚至用 AST 断言「利润函数代码里不得出现 `sku_mapping` / `source_sku`」—— 是真断言。
- **但**：`one_to_many` / `duplicate_item` **只有级别表断言，没有任何「检测器真的能命中」的用例**
  （`grep` 结果只有 `assert CONFLICT_LEVELS[ONE_TO_MANY] == "P0"`）。这正是 QA-01/02 能一路绿灯的原因。

**我新增的测试**（`tests/test_qa_known_defects.py`，4 条，全部 `xfail` 登记）：
`one_to_many` 真实命中 / `duplicate_item` 真实命中 / `spec_mismatch` 转 `pending_confirm` / 适配器 config 可达。
用 `xfail` 而不是 `skip`：现在 XFAIL（缺陷被显式记录），修好后 XPASS（提醒摘标记转常驻断言）。

### B. 半自动上架主链路（真实 HTTP）

| 步骤 | 结果 |
|---|---|
| B-3 AIR-P0-03：引用未审核 AI 结果 | `422 code=4005 AI 重构结果 999999 不存在` ✅ |
| B-5 LST-P0-07：回填缺 `sale_price` | `422 code=1001`；`sale_price=0` 同样 `422` ✅ |
| B-6 正常回填 | `200`，`mapping_ids=[4]`，`status=valid`，`purchase_cost=12.00`，`cost_source=auto` ✅ |
| B-7 `POST /sku-mappings/validate` | `passed=true blocking=false`（仅 `many_to_one` P1 提示） ✅ |
| B-8 反向制造 P0（成本改 0） | 服务层拒绝：`422 code=3005 采购成本必须大于 0` —— **无法通过 API 制造该 P0**（这点是好事） |

> 注：B 段唯一失败项是 `POST /ai-tasks` 返回 500，根因是并发写冲突（QA-08），
> 且该请求触发的 task 27 又因 QA-04 失败。

### C. 六类冲突命名与实测

见 QA-01/02 的 C-7 汇总表。**已确认可用的**：`many_to_one`、`duplicate`、`cost_invalid`、
`cost_underwater`、`spec_mismatch`。

`cost_underwater` 的前置过滤逐条实测生效：

```
[OK] 售价 = 0 时命中 0 行 → sale_price_cents>0 过滤生效（代价：该商品永久失去倒挂检测）
[OK] is_mock = 1 时命中 0 行 → is_mock = 0 过滤生效
[INFO] status = pending_confirm 时命中 0 行 → 倒挂只检测 status='valid'
```

源码里对这些过滤的代价有**诚实的长注释**说明盲区，这点值得肯定。

### D. 三条红线

- **R1 权限最小化**：`check_scope` 正确放行 `['order.read','logistics.write']`，正确拒绝
  `item.write` / `price.update` / `item.offline` / 白名单外 scope；`enforce_scope` 拒绝时带 `code=5003`。
  ⚠ 可被绕过（QA-07），⚠ 有后门开关（QA-09）。
- **R2 第三方不得写店铺**：AST 扫描 `app/adapters/fulfillment/` 全部 10 个文件
  → **无** `offline` / `update_stock_price` / `publish`，**无** `app.adapters.listing` 依赖 ✅
  反向枚举 `offline()` 调用点 3 处，均属 `ListingAdapter` 侧 ✅（可枚举性见 QA-12）
- **R3 优雅降级**：不配 `base_url` 时，妙手 / 逸淘 **8/8 能力全部返回信封，零异常** ✅

```
--- miaoshou  base_url='' ---            --- yitao  base_url='' ---
  fetch_orders          -> RETRYABLE       fetch_orders          -> RETRYABLE
  match_sku             -> UNSUPPORTED     match_sku             -> UNSUPPORTED
  place_purchase_order  -> RETRYABLE       place_purchase_order  -> RETRYABLE
  submit_refund         -> RETRYABLE       submit_refund         -> UNSUPPORTED
  get_return_address    -> RETRYABLE       get_return_address    -> UNSUPPORTED
  push_inventory_change -> RETRYABLE       push_inventory_change -> RETRYABLE
  fetch_tracking_no     -> RETRYABLE       fetch_tracking_no     -> RETRYABLE
  write_back_tracking   -> RETRYABLE       write_back_tracking   -> RETRYABLE
```

> 我第一版 D 探针传了错误的请求类型（`TrackingPayload` 是**结果**类型，请求应是
> `TrackingQuery` / `WriteBackRequest`），得到 5 个 FATAL —— 那是**我探针的 bug**，
> 修正类型后 R3 干净通过。这条也顺带说明：适配器方法的请求/响应类型同名易混，建议加类型别名区分。

### E. 两层越权告警 + 顶栏性能红线

```
E-1  顶栏字段: ['unhandled_violation_count', 'latest_violation', 'listing_mode',
                'listing_mode_label', 'is_mock_active', 'active_fulfillment_adapter',
                'active_fulfillment_adapter_label', 'health_status', 'unhealthy_items']
     baseline violation count = 0

E-4  ★ 顶栏性能红线（猴子补丁实证）
     已在 httpx.AsyncClient.send / httpx.Client.send / socket.connect / getaddrinfo 装好拦截器
     连续 20 次 GET /system/status-bar 全部 200
     总耗时 1199.2ms，单次平均 59.96ms
     期间被拦截的对外网络调用: 0
     -- 对照组：故意发起一次对外请求，验证拦截器生效 --
     ✅ 拦截器生效：AssertionError: ★ 检测到 httpx 对外请求：https://example.com

E-5  审计日志（permission_change）已落库 6 条（FIX-5 修复生效）
```

- **E-4 通过**：顶栏被 60s 轮询，**运行时零外部 HTTP**，且有对照组证明拦截器真的生效（不是「没拦到」而是「真的没有」）。
- **E-2 / E-3 未能完成**：触发路径被 QA-05 阻塞（见 QA-11）。

### F. 适配器热切换 + 在途订单

```
F-1  GET /adapters/fulfillment -> 200，三个适配器齐全（local_csv / miaoshou / yitao），active=local_csv ✅
F-2  造数据：local_csv 名下 3 单在途 + 1 单终态
F-3  POST switch -> miaoshou -> 200
     {"active_adapter":"miaoshou","previous_adapter":"local_csv","inflight_order_count":4,"audit_id":23}
     inflight = 4（既有 1 + 本探针 3 单在途；终态 COMPLETED 被正确排除）✅
F-4  ★ 切换后 4 单订单 adapter_name 全部仍是 local_csv（ADR-6 冻结）✅
F-5  切回 local_csv，previous_adapter='miaoshou'；切 not_exist -> 400 code=5001 ✅
```

**✅ F 段全部通过。**

> 我自己第一版探针把在途数硬编码期望 3，实际 4 —— 那是**我测试写错了**（漏算库里既有种子订单），
> 已改为动态基线，不是产品缺陷。

### G. 三层成本语义

```
G-0  真源 12.00 / 镜像 12.00 / 快照 12.00      利润 = 3600 分
G-1  货源涨价 12.00 → 15.00
      真源 1500 ✅   镜像 1500 ✅（已同步）
      快照 1200 ✅（★ 未被改写）   利润 3600 ✅（★ 一动不动）
G-2  人工覆盖 13.50（cost_source='manual'）后真源再涨到 20.00
      镜像 1350 ✅（★ 未被静默覆盖）
      「成本待确认」工单数 0 → 1 ✅（只提示不覆盖）
      快照仍 1200 ✅   利润仍 3600 ✅
```

**✅ G 段全部通过。** 附录 A 第 18 条（历史订单利润不可回溯改写）**落地正确**。

**G4 独立 AST 复核**（不复用他们的实现）：扫描 8 个含 `profit` 的模块，
`schemas/order.py:148 profit_cents()` / `schemas/order.py:164 profit()` /
`mapping_validator.py:509 _min_profit_margin()` / `order_service.py:724 profit_cents()`
全部 **OK**，无一处 join 回 `sku_mapping` / `source_sku`；
`order_service.py:734 cost = int(item.purchase_cost_cents or 0)` ✅

### H. 前后端契约一致性

```
H  后端端点总数: 114   前端扫描到的调用: 110
   ---- 前端调用了但后端不存在（★ 缺陷）---- 0 条
   ---- 后端有但前端未使用（不是缺陷，仅列出）---- 4 条
      GET /api/v1/assets/{id}/download · GET /api/v1/files/exports/{filename}
      GET /api/v1/publish-tasks/manual/{id}/package · GET /health

H  专项核对 5 个端点
  ✅ GET   /api/v1/system/status-bar            query参数=[]
  ✅ POST  /api/v1/adapters/violations/{violation_id}/handle
  ✅ GET   /api/v1/adapters/violations           is_unhandled ✓
  ✅ POST  /api/v1/listing-products/{product_id}/fill-price
  ✅ GET   /api/v1/listing-products              missing_price ✓

H  前端冲突类型 key（web/src/constants/enums.ts）
  ✅ one_to_many ✅ duplicate ✅ duplicate_item ✅ many_to_one
  ✅ cost_invalid ✅ cost_underwater ✅ spec_mismatch
```

**✅ H 段 0 缺陷。** `duplicate_item` 前端已有，未漏。

> 我第一版 H 探针报了 52 条「前端调用后端没有」—— 那是我自己的 bug：
> 没把模板字面量 `${id}` 归一化成 `{id}`，且正则漏匹配嵌套泛型 `<PageResult<OrderVo>>`。
> 修正后归零。**这不是产品缺陷。**

### I. 边界与异常路径

```
I-1  ★ ORD-P0-03：映射 pending_confirm 的订单
     match_order -> (False, 'QA-I-PENDING: 映射状态为 pending_confirm，订单需挂起待确认')
     订单状态: exception_unmatched / unmatched / unmatched  ✅ 挂起未盲发
I-2  缺失映射 → exception_unmatched ✅
I-3  对照组 valid 映射 → matched ✅
I-4  spec_mismatch → 映射未转 pending_confirm ❌（= QA-03）
I-5  软删除的映射可再次创建（唯一索引已释放），现存 2 行 is_deleted=[True, False] ✅
I-6  local_csv.place_purchase_order 固定 manual_pending，绝不返回 placed ✅
```

**ORD-P0-03（用户点名最高风险）的挂起逻辑本身是对的** ✅
—— 但它在 `spec_mismatch` 场景下会被绕开，因为上游不置 `pending_confirm`（QA-03）。

### 前端

```
> tsc --noEmit && vite build
✓ 3171 modules transformed.
dist/assets/index-Ccjn8J0g.js     246.35 kB │ gzip:  78.24 kB
dist/assets/antd-BsvsgVN9.js    1,253.19 kB │ gzip: 392.45 kB
✓ built in 21.92s
```

**✅ 类型检查零错误，生产构建成功。**

---

## 四、第二轮复测（实现团队修复后）

实现团队同步了本轮改动，其中一条直接质疑我的取证环境：**「你之前跑探针时，后端很可能连的是 0 表的空壳库 `backend/data/erp.db`」**。
我必须先核实这条，因为它会动摇我全部结论的可信度。

### 5.1 取证环境核实 —— 我的探针连的是哪个库？

**结论：我连的是 25 表的根库 `data/erp.db`，不是空壳库。这条质疑不成立。**

证据：

```
# 服务启动日志（我自己的实例，端口 8141）
database_ready  url=sqlite+aiosqlite:///G:/workbuddy/2026-10-08-10-27-17/data/erp.db

# 两个库文件的实际情况
data/erp.db           : 25 张表, 450560 字节
backend/data/erp.db   : 早期确实存在过（我在最初一次探针里撞到过
                        "no such table: source_product"），那次是**我自己**用错路径，
                        随即改成根库。本轮复测时该文件已不存在。

# 我报出的错误形态也证明不是空库
  sqlite3.OperationalError: attempt to write a readonly database   ← 锁
  而不是  sqlite3.OperationalError: no such table: ...             ← 空库
```

补充：QA-01/02 的探针用的是**从 ORM metadata 现建的独立 scratch 库**，与开发库无关，
根因是 schema 层的部分唯一索引，环境无关 —— 所以这两条不受任何库路径影响。

**但我认可这条提醒本身有价值**：`DATABASE_URL` 用相对路径 `./data/erp.db`、
按 cwd 解析确实是雷，他们加的 `_resolve_relative_sqlite` + `test_db_path_safety.py` 是对的。

### 5.2 复测结果汇总

| 项 | 第一轮 | 第二轮（本轮） | 判定 |
|---|---|---|---|
| QA-04 1688 采集 | 100% 失败（TypeError） | `task 38 source_collect **success**` | ✅ **已修复** |
| QA-01/02 死 SQL | 空转 | 仍空转（scratch 库复现一致） | ❌ 存活 |
| QA-03 spec_mismatch | 不转 pending_confirm | 仍 `status=valid`，`matched=True` | ❌ 存活 |
| QA-05 适配器 config | 三个全 404 | 三个仍全 404，库里 0 行 | ❌ 存活 |
| FIX-4 AI 事务锁库 | 已修 | B 组 3×`publish-tasks` 全 202；C 组 `orders/sync`+`sku-mappings` 均成功 | ✅ 确认修复 |
| QA-08 并发 500 | 观察到 1 次 | 10 并发 `POST /sku-mappings` → **10×201，0×500** | ✅ 关闭 |
| QA-11 越权告警闭环 | 被 QA-05 阻塞 | 手工补行后 **0→1→处置→0 全通** | ✅ 机制正常 |
| QA-13 load_config 吞异常 | 未发现 | 新发现 | ❌ 新增 |

### 5.3 QA-04 已修复（复测证据）

```
POST /api/v1/source-products/collect -> 202 采集任务已受理
  task 38 source_collect success | None
  task 37 order_sync     success | None
  task 36 order_sync     success | None
```

### 5.4 QA-05 仍存活 —— 且本轮证明它的危害比「404」大得多

```
GET  /adapters/fulfillment -> 200 ['local_csv', 'miaoshou', 'yitao']
PUT  config local_csv  -> 404  履约适配器 local_csv 不存在
PUT  config miaoshou   -> 404  履约适配器 miaoshou 不存在
PUT  config yitao      -> 404  履约适配器 yitao 不存在
库里 fulfillment_adapter 行数 = 0
```

**因果链（本轮实测打通）**：配置行永不落库 ⇒ `declared_scopes` 永远是 `[]` ⇒
`check_scope([])` 恒通过 ⇒ **R1 永不拒绝、越权审计永不产生、顶栏红点永远是 0**。

也就是说，**QA-05 不只是「配置接口 404」，它让整条 R1 权限红线 + 两层越权告警在生产环境整体失效。**
所以我把它从「配置不可用」升级定性为**红线失效**，级别维持 P0。

反向验证（我手工往库里插一行 `declared_scopes=["order.read","item.write"]` 后）：

```
FulfillmentAdapterFactory.create('miaoshou', session=s, actor='qa')
  -> 被拒 ✅ ScopeViolationError: [5003] 适配器 miaoshou 声明了越权 scope：item.write
  -> 审计落库 ✅ #66 scope_check_status="rejected", is_handled=0
```

**机制本身是对的**，只是生产环境里永远触发不到。

### 5.5 QA-11 越权告警闭环 —— 机制正常（生产被 QA-05 阻断）

在上面手工补行、制造出一条未处置告警后：

```
E-2 计数 = 1   latest = {'id':66, 'adapter_name':'miaoshou', 'denied_scopes':['item.write']}
未处置告警条数 = 1
处置 -> 200 越权告警已处置
E-3 处置后计数 = 0        ← 0 → 1 → 0 完整闭环 ✅
```

附录 A 第 16 条「越权被拒 + 被处置」成对审计也落地了。
**结论：E-2/E-3 的实现是对的，但它依赖一个生产环境里永远不存在的 DB 行。**

### 5.6 QA-13（新增 P1）：`load_config()` 读失败 → 空 scope → R1 静默放行

```python
# app/adapters/fulfillment/factory.py:79
async def load_config(session, adapter_name) -> tuple[AdapterConfig, list[str]]:
    ...
    if session is None:
        return config, scopes          # ← 空 scope
    try:
        row = (await session.execute(stmt)).scalar_one_or_none()
    except Exception as exc:
        logger.warning("adapter_config_read_failed", ...)
        return config, scopes          # ← ★ 读失败也返回空 scope
```

两条路径都返回**空 scope**，而 `check_scope([])` 是**通过**的。
因此只要（a）调用方不传 session，或（b）读配置时 DB 正好被锁，
`create()` 就会**静默放行**一个未经 scope 校验的适配器 —— R1 被绕过且不报错。

实测佐证：我不传 session 调用 `create('miaoshou')`，即使库里那行写着 `item.write`，
也照样返回了实例，审计还记成 `scope_check_status="passed"`。

**建议**：`load_config` 读到空 scope 且适配器非 `local_csv`（内置兜底）时，
应当按「无法确认权限」处理 —— 拒绝创建或至少落一条 warn 级审计，而不是静默通过。

---

## 五、发布判定（第二轮，★ 已被 §六 覆盖）

> ### ⚠️ 本节是**存档**，不代表当前状态。
> ### 现行判定请直接看 **§六 6.6：🟡 有条件放行（Conditional Go）**。
>
> 第三轮复检结果：本节列出的 4 个阻塞项（QA-03 / QA-05 / QA-01-02）**已全部真修好**，
> QA-06 / QA-13 也已修好并 fail-closed。**当前 P0 = 0 · P1 = 0 · P2 = 3。**
> 下方原文保留，供追溯「这些缺陷当初长什么样、我为什么判 P0」。

### ❌ 不建议按现状发布

> 已按 **第二轮复测（§四）** 更新：QA-04 已修复、QA-08/QA-11 已关闭、新增 QA-13。

**必须修复后才能上线（阻塞项，4 个）**：

1. **QA-03**（`spec_mismatch` 不转 `pending_confirm`）—— 会真金白银发错货。
2. **QA-05**（适配器配置行不落库）—— 不只是配置 404，它让 **R1 红线 + 两层越权告警在生产环境整体失效**。
3. **QA-01 / QA-02**（两个死检测器）—— 文档承诺的 P0 拦截实际不存在，属于误导性保护。

**建议本轮内修（不阻塞但要排期）**：
QA-13（`load_config` 读失败静默放行，安全相关，建议和 QA-05 一起改）、
QA-06（检测失败要可见）、QA-07（R1 校验下沉）。

**P2 可不阻塞**：QA-09 / QA-10 / QA-12。

### 修复后需重跑的最小验证集

```
pytest -q                                   # 4 条 xfail 应从 XFAIL 变 XPASS
python qa_probes/probe_c_conflicts.py       # one_to_many / duplicate_item 必须「是」
python qa_probes/probe_i4_impact.py         # 映射 status 必须 pending_confirm，订单必须挂起
PUT  /api/v1/adapters/fulfillment/miaoshou/config   # 必须 200（★ 真实 HTTP，不能只在 pytest 里验）
                                            # 且库里 fulfillment_adapter 必须真的有行
python qa_probes/probe_e_violation.py       # E-2 计数 0→1→回落
```
已在本轮通过的回归项：`POST /source-products/collect`（task success）、
`probe_lock_ab.py`（AI 等待期不持写锁）、10 并发 `POST /sku-mappings`（0×500）。

### 一句话总结

**这版代码的质量比我预期的要高**——三层成本语义、ORD-P0-03 挂起、R2/R3 红线、顶栏零外部 HTTP、
前后端契约、前端构建，这些是**真的做对了**，不是凑出来的。
但它同时存在 4 个会**静默失效**的 P0：检测不到（QA-01/02）、检测到了不生效（QA-03）、
源头采不进来（QA-04）、配不上第三方（QA-05）。
**「不报错」不等于「能用」——这正是本次验证要抓的东西。**

---

## 六、第三轮 · 最终复核（聚焦验证，2026-10-08 22:40~22:50）

### 6.0 先说方法论：我怎么保证「测的是当前代码」，而不是一个陈旧进程

这一轮最大的坑不是代码，是**进程**。我在上一轮踩到了，必须写清楚，否则后面所有结论都不成立：

```
app/api/v1/orders.py            mtime = 10-08 21:51:37   ← 源码里有 place-purchase
8000 端口那个实例              启动更早                  ← 它的 OpenAPI 里没有该路由
GET  :8000/api/v1/orders/6/place-purchase  ->  404
```

我第一次撞到这个 404 时**没有**直接判「路由没写」，而是先去查 OpenAPI 和文件 mtime，
最后确认：**进程加载的是旧代码**。这个 404 **不是产品缺陷**，差点让我误报一个 P0。

所以本轮我**一律改用 `TestClient(create_app())` 进程内载入当前源码**来验证，
从机制上排除「陈旧进程」这个变量。同时在探针里加了一段 **S0 自检**——
先证明我测的确实是新代码，再去断言业务：

```
============================================================================================
S0  先证明「我测的是当前代码」
============================================================================================
  ConflictType = ['one_to_many', 'duplicate', 'many_to_one', 'cost_invalid', 'cost_underwater', 'spec_mismatch']
  ✅ ConflictType 已剔除 duplicate_item（枚举层）  —— detection 层仍保留常量仅为前端展示兼容
  DETECTION_ORDER = ('many_to_one', 'duplicate', 'cost_invalid', 'cost_underwater', 'spec_mismatch')
  ✅ DETECTION_ORDER 已剔除两个恒空检测器
  GET /health -> HTTP 200  status=ok
  constraints = {'ok': True, 'missing_count': 0, 'missing': []}
  ✅ /health 具备 constraints 自检字段（本轮新增能力，旧进程没有）
```

**这三点就是「当前代码」的指纹**：`ConflictType` 里没有 `duplicate_item`（旧代码有）、
`/health` 会返回 `constraints`（旧代码没有）。指纹对上了，后面的结论才有效。

> ⚠ **本轮验证的边界（我必须自己说出来）**：
> 全部结论来自「进程内载入当前源码」，**没有**在真实 uvicorn 进程上跑过 `place-purchase`。
> 所以「进程必须重启」不是一句套话，而是本轮验证的真实缺口——见 §六 C1。

---

### 6.1 原 4 个 P0 是否真修好（QA-01 / QA-02 / QA-03 / QA-05）+ QA-06 / QA-13

探针：`backend/qa_probes/probe_r3_p0_recheck.py` + `probe_i4_impact.py` + `probe_r3_qa06.py`

#### QA-05：三个履约适配器 `PUT config` 必须 200，且库里真的落行

```
============================================================================================
QA-05  三个履约适配器 PUT config（合法 scope）→ 必须 200 + 真落库
============================================================================================
  PUT miaoshou   -> HTTP 200  code=0  适配器配置已更新
  PUT yitao      -> HTTP 200  code=0  适配器配置已更新
  PUT local_csv  -> HTTP 200  code=0  适配器配置已更新
  ✅ 三个适配器 config 全部 200（实测 {'miaoshou': 200, 'yitao': 200, 'local_csv': 200}）  —— 旧缺陷是 100% 404
  [DB] fulfillment_adapter_config 行数 = 3
        ('local_csv', ['order.read', 'logistics.write'], True)
        ('miaoshou', ['order.read', 'logistics.write'], True)
        ('yitao', ['order.read', 'logistics.write'], True)
  ✅ 配置行真的落库（3 行）  —— 旧缺陷：表里 0 行 ⇒ R1 与告警整体失效
  ✅ 每行 declared_scopes 非空  —— declared_scopes 为空 ⇒ check_scope([]) 通过 ⇒ R1 永不拒绝
```

**我不只看 HTTP 码**。QA-05 上一轮被我升级成红线级缺陷，根因不是 404，而是
「表里 0 行 ⇒ `declared_scopes=[]` ⇒ `check_scope([])` 通过 ⇒ R1 永不拒绝」。
所以这里我**回读数据库**确认 3 行都在、且 `declared_scopes` **非空**。两个条件同时成立，才判修好。

#### QA-01 / QA-02：死 SQL 已移除 + 重复键 409/1006 + ★反向验证

```
============================================================================================
QA-01 / QA-02  重复映射：必须 409/1006，且约束自检能反向发现索引缺失
============================================================================================
  第一次 POST -> HTTP 201  code=0
  ✅ 首次创建成功（HTTP 201）
  第二次 POST（撞唯一键）-> HTTP 409  code=1006
        message = 该平台 SKU 已存在映射：taobao/qa-r3-shop/QA-R3-SKU（当前指向货源 SKU 未知；同一 平台+店铺+SKU 编码 只能有一条有效映射，请先删除或改名现存映射）
  ✅ 重复键返回 409（实测 HTTP 409）  —— 旧缺陷：漏成 500
  ✅ 业务码 = 1006（实测 1006）
  ✅ 错误信息是可直接展示给运营的中文  —— 该平台 SKU 已存在映射：taobao/qa-r3-shop/QA-R3-SKU（当前指向货源 SKU 未知；同一 平

  ---- 反向验证：DROP INDEX uq_sku_mapping_shop_sku ----
  GET /health -> HTTP 200  status=degraded
  constraints = ok=False missing_count=1 missing=['uq_sku_mapping_shop_sku']
  ✅ 索引缺失被 /health 自检真实报出（不是嘴上说有约束）
  恢复索引后 constraints = ok=True missing_count=0
  ✅ 索引恢复后自检回到 ok=true
```

**这一段是本轮我最看重的证据**，也是团队点名要求的**反向验证**：

只测「重复插入返回 409」，只能证明**现在**有约束；
我把索引 `DROP` 掉再打 `/health`，它必须**主动报缺失**（`status` 从 `ok` 掉到 `degraded`，
`missing=['uq_sku_mapping_shop_sku']`），再把索引建回去，`ok` 恢复 `true`。
这才证明这条约束是被**持续自检**的——将来谁手滑删了索引，运维能当天发现，而不是等到发错货。

修法本身我也认可：不再靠「读时扫描一遍找重复」（那条 SQL 因同一索引恒为空，属于死 SQL），
而是**在写入边界用唯一约束硬阻断**，再把 `IntegrityError` 翻译成 409/1006 中文业务错误。
这是比原设计更正确的做法，文档里也如实说明了（见 `mapping.py` 头部注释）。

#### QA-03：`spec_mismatch` 检出后映射转 `pending_confirm`，订单挂起不盲发

探针：`probe_i4_impact.py`（真实 ORM 造数据 → 货源侧改规格 → 检测 → 匹配订单）

```
================================================================================================
场景：商品已上架并持续出单 → 货源侧悄悄把 XL 改成 XXL
================================================================================================
  货源侧规格 XL → XXL，指纹 sig-v1 → sig-v2
  [旁证] spec_mismatch 原始 SQL 命中 2 行: [... {'id': 13, 'shop_sku_code': 'QA-SPEC-SKU',
          'mapping_signature': 'sig-v1', 'current_signature': 'sig-v2',
          'current_spec_json': '{"颜色": "红", "尺码": "XXL"}'}]
  detect_conflicts(detect_all=False  ← API 默认) -> {'detected': 1, 'by_type': {... 'spec_mismatch': 1}, 'errors': [], 'incomplete': False}
  detect_conflicts(detect_all=True)  -> {'detected': 1, 'by_type': {... 'spec_mismatch': 1}, 'errors': [], 'incomplete': False}
  [旁证] mapping_conflict 落库 1 条: [{'conflict_type': 'spec_mismatch', 'level': 'P0', 'is_resolved': 0,
          'description': '店铺 SKU QA-SPEC-SKU 的规格指纹与货源侧当前规格不一致，映射需重新确认'}]

  映射 status = pending_confirm
  ✅ 映射已自动转 pending_confirm（符合 ARCHITECTURE.md:715）

  match_order -> matched=False  reason='QA-SPEC-SKU: 映射状态为 pending_confirm，订单需挂起待确认'
  订单状态: {'fulfillment_status': 'exception_unmatched', 'match_status': 'unmatched',
             'exception_note': 'QA-SPEC-SKU: 映射状态为 pending_confirm，订单需挂起待确认'}

================================================================================================
判定
================================================================================================
  ✅ 订单被挂起，未盲发。
```

这是 ORD-P0-03 那条最高风险路径。**上一轮它是「检测到了但订单照发」**——买家下单 XL，实际按 XXL 发货。
现在全链路闭合：检出 → 落 P0 冲突行 → 映射转 `pending_confirm` → `match_order` 返回 `False` →
订单进 `exception_unmatched` 并写明原因。**会真金白银发错货的洞已经堵上。**

（旁证里第一行 `KOU-SPEC-001` 是开发库里别人留下的历史脏数据，签名也对不上——
不是代码缺陷，但提醒一下：**上线前建议清一遍开发库**，否则一开机顶栏就有告警。）

#### QA-06：检测失败必须「可见」，且 fail-closed

上一轮我把 `_run()` 里的异常全吞了（永远返回「0 冲突」）。这一轮我**注入一条必然失败的 SQL**
（把 `spec_mismatch` 的查询换成 `SELECT * FROM __这张表不存在__`），看它会不会继续报「无冲突」：

```
  [对照组] detect_conflicts -> {"detected": 0, "by_type": {...}, "errors": [], "incomplete": false}

2026-10-08 22:41:05 [error    ] conflict_query_failed  conflict_type=spec_mismatch
       error='(sqlite3.OperationalError) no such table: __这张表不存在__'
2026-10-08 22:41:05 [error    ] mapping_conflicts_detection_incomplete   errors=['冲突检测 spec_mismatch 执行失败：OperationalError: ...']

  [实验组] detect_conflicts -> {"detected": 0, "by_type": {...},
       "errors": ["冲突检测 spec_mismatch 执行失败：OperationalError: (sqlite3.OperationalError) no such table: __这张表不存在__ ..."],
       "incomplete": true}

  incomplete = True
  errors     = ['冲突检测 spec_mismatch 执行失败：OperationalError: ...']
  => 「检测失败」与「未检出冲突」可区分: ✅ 成立

  validate -> passed=False blocking=True
  blocked_reason = 存在 2 项 P0 级冲突（重复映射（同店铺内一个货源SKU被多个店铺SKU引用）） + 1 项冲突检测未完成（本次检测不完整，不能判定为无冲突）
  => 检测不完整时 fail-closed 禁止上架: ✅ 成立
```

关键是最后一句：**检测不完整 = 禁止上架**（`blocking=True`），文案也直接说明了原因。
这就是「不报错、只静默失效」的正确反面：**宁可拦下一个本可放行的商品，也不能放行一个没查清楚的商品。**

#### QA-13：不传 session 时，`item.write` 适配器必须被拒绝，不能静默放行

```
============================================================================================
QA-13  不传 session 创建 item.write 适配器 → 必须拒绝（fail-closed）
============================================================================================
  库里 miaoshou.declared_scopes = ['item.write']（越权）
  FulfillmentAdapterFactory.create('miaoshou')  [不传 session]
  -> ScopeViolationError
     [5003] 无法确认适配器 miaoshou 的声明 scope（缺少数据库会话，或 `fulfillment_adapter` 表读取失败 / 无该行）。红线 R1 按「无法确认权限 = 拒绝」处理，请传入有效 session 并先完成适配器配置
  ✅ 不传 session 时按「无法确认权限 = 拒绝」处理（不再静默放行）  —— 旧缺陷：返回实例 + 审计记 passed
  （已把 miaoshou 的 scope 复原为合法值）
```

上一轮这里是**最危险的静默失效**：库里明明写着 `item.write`（越权），
只要调用方没传 session，工厂就当「空 scope」**放行**，审计还记成 `passed`。
现在改成 `declared_scopes is None` ⇒ 送 `<scope.unverifiable>` 进 scope_guard ⇒ 拒绝 + 5003。
**「读不到 = 没申请权限」这个错误假设已经改成「读不到 = 拒绝」。**

---

### 6.2 新增 `place_purchase` 全链路（真实 HTTP 语义 + 跨请求可见性）

探针：`backend/qa_probes/probe_r3_place_purchase_tc.py`（进程内 TestClient 载入当前源码）

```
  当前 Settings 数据库 = sqlite+aiosqlite:///G:/workbuddy/2026-10-08-10-27-17/data/erp.db
  当前源码 OpenAPI 端点数 = 103
  含 place-purchase: True  ->  ['/api/v1/orders/{order_id}/place-purchase']
  已造订单: 可匹配=6  不可匹配=7  可用映射 id=1

============================================================================================
P1  匹配 → 下单 → 跨请求可见
============================================================================================
  POST /orders/6/match -> 200 ok
  GET  /orders/6       -> 200 status=matched
  POST place-purchase        -> 200 采购单已生成（manual_pending）
       data = {'id': 3, 'order_id': 6, 'purchase_order_no': 'LOCAL-6-1791470474-1', 'purchase_status': 'manual_pending', 'amount_cents': 7200, 'adapter_name': 'local_csv'}
  GET  /purchase-orders/3 -> 200 purchase_status=manual_pending

============================================================================================
P2  本地兜底必须是 manual_pending，不能伪装成 placed
============================================================================================
  purchase_status = 'manual_pending'
  ✅ 如实返回 manual_pending（本地从未真正下单）

============================================================================================
P3  未匹配订单下单必须 409（不能盲发）
============================================================================================
  POST /orders/7/match -> 400 未找到可用映射；如需补建请传 create_mapping=true 与 source_sku_id
  GET  /orders/7       -> 200 status=pending_match
  POST place-purchase         -> 409 code=1005 订单状态 pending_match 不允许下单
  ✅ 未匹配订单下单被 409 拒绝（未盲发）

place_purchase 结论（当前源码）
============================================================================================
  ✅ place_purchase 链路全部通过
```

三条断言都成立，其中两条是我特意加的「反伪装」断言：

- **P2**：本地 CSV 兜底**绝不**把状态写成 `placed`。本地根本没有真正下单，
  写成 `placed` 就是骗运营——这正是「不报错、只静默失效」的另一种形态。实测如实返回 `manual_pending`。
- **P3**：未匹配订单下单被 **409 / 1005** 拒绝。这条如果放行，就是拿错货源去采购。
- **P1 的跨请求可见**：`POST` 之后我用**新的 `GET` 请求**回读 `/purchase-orders/3`，
  确认状态是 `manual_pending` 而不是会话里的临时对象——排除「只在本次会话内存里」的假象。

---

### 6.3 R1 红线全链路重确认（越权 → 403 → 顶栏 +1 → P17 → 处置 → 回落 → 审计成对）

探针：`backend/qa_probes/probe_r3_r1_and_ai.py`

```
  基线 unhandled_violation_count = 0

============================================================================================
R1-1  越权 PUT config（声明 item.write）-> 期望 403 / code 5003
============================================================================================
  PUT config yitao -> HTTP 403  code=5003
       message = 适配器 yitao 声明了越权 scope：item.write。第三方不得持有商品编辑 / 上架 / 下架 / 改价权限
  ✅ 越权被 403/5003 拒绝

============================================================================================
R1-2  顶栏未处置计数 +1
============================================================================================
  越权后 unhandled_violation_count = 1  (基线 0)
  ✅ 计数已从 0 升到 1

============================================================================================
R1-3  P17 告警列表可见
============================================================================================
  GET /adapters/violations -> HTTP 200  条数=1
     #146 adapter=yitao denied=['item.write']
  ✅ 告警在 P17 可见

============================================================================================
R1-4  处置后计数回落
============================================================================================
  POST handle/146 -> HTTP 200 越权告警已处置
  处置后 unhandled_violation_count = 0
  ✅ 计数已回落到 0（基线 0）

============================================================================================
R1-5  附录 A 第 16 条：「被拒」与「被处置」审计成对存在
============================================================================================
  audit-logs(permission_change) -> HTTP 200 条数=10
  「被拒」审计合计 = 9 条，其中已处置 = 9 条
     #146 object=yitao is_handled=1 remark=适配器 yitao 声明了越权 scope：item.write
     #141 object=yitao is_handled=1 remark=适配器 yitao 声明了越权 scope：item.write
     #138 object=yitao is_handled=1 remark=适配器 yitao 声明了越权 scope：item.write
  yitao 的越权审计: [(138, 1), (141, 1), (146, 1)]
  ✅ 「被拒」与「被处置」成对存在（9/9 已闭环）
```

**这一条链路上轮是完全断的**（审计 0 条）。现在 6 个环节全通：
越权被拒 → 403/5003 → 顶栏 0→1 → P17 可见 → 处置 → 回落 0 → 审计成对（9/9 闭环）。
「第三方永不持有商品编辑权」这条红线现在是**真的在执行**，不是写在文档里。

---

### 6.4 AI 客户端 session 绕过修复 —— 我独立验的，不采信他们的自报

上一轮的 FIX-2（`AiClientFactory.create(session)` 参数错位）是他们在我验证期间修的。
这轮我**自己改配置、自己不传 session 调工厂**，看配置说不说得算：

```
============================================================================================
AI-1  把 ai.client 改成 mock，不传 session 调工厂 -> 必须是 Mock 而非 file_bridge
============================================================================================
  ai.client=mock         -> resolve_ai_client_name()='mock'           create() -> MockAiClient
       => ✅ 配置说了算
  ai.client=file_bridge  -> resolve_ai_client_name()='file_bridge'    create() -> WorkBuddyFileBridgeClient
       => ✅ 配置说了算

第三轮 R1 + AI 结论
============================================================================================
  ✅ R1 全链路与 AI 客户端选择全部通过
```

`resolve_ai_client_name()` 现在在 `session is None` 时**自己开一个短会话**去读配置，
不再退化成默认值。配置 `mock` ⇒ 真的拿 `MockAiClient`；`file_bridge` ⇒ 真的拿 `WorkBuddyFileBridgeClient`。
**配置是权威，不是摆设。**（验完已把 `ai.client` 复原为 `file_bridge`。）

---

### 6.5 与实现团队自报的交叉核对（不一致的地方我直接说）

| 他们的自报 | 我的实测 | 判定 |
|---|---|---|
| `pytest` **88 passed** | **91 passed, 3 warnings in 40.02s**（`collected 91 items`，0 failed / 0 error） | ⚠️ **数字不一致，但方向是少报**。可能他们跑的时刻更早。不扣分，但自报数字应以后跑的为准 |
| 基线 `64 passed / 4 xfailed / 1 xpassed` → 现状 `0 xfail` | `grep -c "pytest.mark.xfail" tests/test_qa_known_defects.py` = **0**；该文件 **9 passed** | ✅ **属实**。xfail 已全部摘除并转为常驻断言，这是我最想看到的处理方式（缺陷修好后把钉子拔掉，而不是留着 `@xfail` 自欺） |
| `scripts/verify_full_chain.py` **25/26** | 我复跑同样 **25/26**，唯一失败项 = `② 采集产出货源商品: 新增 0 条` | ✅ **属实**。且该脚本在采集失败后**自动切 `ai.client=mock`** 继续证明 AI 链路，设计是诚实的 |
| 25/26 里那 1 项失败 | 我独立确认：失败原因是 **1688 采集离线（无真实网络/凭据）**，脚本自己标注 `ENV_LIMIT` | ✅ **同意归类为环境限制，不是代码缺陷**。但**不能因此就当采集链路是通的**——见 §六 C2 |
| 「`place-purchase` 已开放」 | 源码 `OpenAPI` 端点数 **103**，含 `/api/v1/orders/{order_id}/place-purchase`；链路实测全通 | ✅ **属实** |

**我唯一要额外指出的不一致**：他们那版 25/26 的数字，来自**新起的进程 + 隔离库**，
而当时 **8000 端口那个实例加载的是旧代码**（OpenAPI 里根本没有 `place-purchase`）。
两件事都是真的，但**不能混为一谈**——这直接引出下面 C1。

---

### 6.6 我的发布判定：🟡 **有条件放行**（Conditional Go）

**从「不建议发布」改为「有条件放行」。** 理由：上轮阻塞发布的 4 个 P0，我这轮**逐个复检、逐个拿到实测输出**，
全部真修好；其中 QA-01/02 我还做了 **DROP 索引的反向验证**，证明约束是被持续自检的，不是一次性补丁。
新增链路与 R1 红线一样实测全通。**当前 P0 = 0，P1 = 0。**

但「有条件」是有实打实的条件的，不是客套：

#### ✅ C1（阻塞放行）：上线前必须**重启进程**，并在真实 uvicorn 进程上冒烟一次

- **证据**：8000 实例 OpenAPI 缺 `/api/v1/orders/{order_id}/place-purchase`，而源码 `orders.py`（mtime 21:51）有 —— **旧进程加载旧代码**。
- **为什么这是阻塞项**：本轮我所有验证都是**进程内 `create_app()`** 载入当前源码，
  **没有**在真实 uvicorn 进程上跑过 `place-purchase`。这是我这轮验证的真实边界，我不藏。
- **放行前必须做**：杀掉所有旧实例 → 重新 `uvicorn` 启动 → 打一次
  `GET /openapi.json` 确认含 `place-purchase` → 打一次完整 `place-purchase` 链路。

#### ✅ C2（阻塞放行）：1688 采集必须回**真实网络环境**实测一次，不能用 mock 结论放行

- **证据**：`verify_full_chain.py` 25/26 的唯一失败项就是采集 0 条；脚本自己标 `ENV_LIMIT`。
- **为什么这是阻塞项**：采集是全链路**第一步**。代码层已修（QA-04 已闭环），
  但「代码能跑」和「真实 1688 页面能采下来」是两件事——后者取决于网络、登录态、页面结构。
  **在真实环境采到 ≥1 条之前，整条链路的源头都还没被证明。**
- **放行前必须做**：在目标网络环境跑一次 `POST /source-products/collect`，**采到 ≥1 条**；
  如果采不到，必须有降级预案（手工导入 CSV），并且**不能**宣称「全链路已通」。

#### ⚠️ C3（建议，不阻塞）：上线前清一遍开发库脏数据

- **证据**：`probe_i4_impact.py` 旁证里，库里已存在一条签名对不上的映射
  （`KOU-SPEC-001` / `mapping_signature=kou-sig-v1` vs `current_signature=seed-sig-001`）。
- **影响**：不是代码缺陷，但**一开机顶栏就会挂 1 条 P0 告警**，运营第一天就会来问。

#### 遗留 P2（不阻塞，建议排期）

| 编号 | 级别 | 现状与我的判断 |
|---|---|---|
| **QA-07** | **P2**（本轮**从 P1 降级**） | R1 校验只在工厂里，脱离工厂实例化就不校验。**但我实测 `app/` 内 0 处脱离工厂实例化**（grep `MiaoshouAdapter(` / `YitaoAdapter(` / `LocalCsvAdapter(` 只命中类定义本身）；且 `Capability` 枚举 8 项**完全没有商品写能力**——就算绕过也调不到 `item.write`。因此**不是可利用缺陷**，降为纵深防御缺口。建议：把 scope 校验下沉到 `FulfillmentAdapter.__init__`，或给 `invoke()` 加一道 scope 断言。 |
| **QA-09** | **P2** | 工厂 `create()` 保留 `strict_scope=False` 开关。实测默认 `True`，**`app/` 内 0 个调用点传 False**。潜伏风险，不是现行缺陷。建议删掉这个开关，或加注释说明唯一合法用途。 |
| **QA-12** | **P2** | `publish_service.py:383` 仍用 `adapter.invoke("publish", ...)` 字符串派发。实测功能正确（`normalize_capability()` 已兜底，R3 段 8/8 通过），仅**静态可枚举性弱**——将来谁改了能力名，IDE 和 grep 都搜不到这个调用点。建议统一成 `Capability.PUBLISH` 枚举。 |
| ~~QA-10~~ | ✅ 已关闭 | 顶栏字段 `unhandled_violation_count` 已在 `system_service.py:129` 与 `:409` 就位；R1-2/R1-4 实测 **0 → 1 → 0**。 |

#### 一句话总结（第三轮）

**这一版现在可以放了，但要按我说的放。**
上轮那 4 个会**静默失效**的 P0——检测不到（QA-01/02）、检测到了不生效（QA-03）、配不上第三方（QA-05）、
检测失败看不出来（QA-06 / QA-13）——我这次不是看他们的修复说明，而是**自己改数据、自己打请求、自己回读数据库**，
并且**反向验证**了一条（DROP 索引后 `/health` 必须报缺失），全部确认堵上了。

剩下三个 P2 都不是「不能用」，是「以后可能被人改坏」。
真正卡住放行的是两件**环境/卫生**层面的事：**进程必须重启**（否则上线的还是旧代码），
**1688 采集必须回真实网络验一次**（否则全链路的源头仍未被证明）。

> 补一句公道话：这两轮下来，实现团队对我报的每一个缺陷都做了**结构性修复**而不是打补丁
> （死 SQL 改成写入边界唯一约束 + 409 翻译；`load_config` 改成「读不到 = 拒绝」；
> 检测失败改成 fail-closed），还把我钉的 `xfail` 全部摘掉转成常驻断言。
> **这版代码的可信度比第一轮高出一大截**，我给的「有条件放行」是这个判断的结果，不是和稀泥。

---

## 附录：QA 探针清单（均位于 `backend/qa_probes/`，只新增测试代码，未改动任何 `app/` / `web/` 源码）

| 文件 | 用途 |
|---|---|
| `probe_b_manual_listing.py` | B 段：半自动上架主链路端到端（真实 HTTP） |
| `probe_c_conflicts.py` | C 段：六类冲突检测 SQL 是否真能命中（ORM 造数据 + 真实 SQL） |
| `probe_d_redlines.py` | D 段：R1 / R2 / R3 攻击（含绕过尝试与 AST 扫描） |
| `probe_e_violation.py` | E 段：两层越权告警 + 顶栏字段 |
| `_nohttp_probe.py` | E-4：零外部 HTTP 实证（猴子补丁 + 对照组） |
| `probe_f_switch.py` | F 段：适配器热切换 + 在途订单渠道冻结 |
| `probe_g_cost.py` | G 段：三层成本语义 + 利润不可回溯改写 |
| `probe_i_boundary.py` | I 段：ORD-P0-03 / 缺映射 / 软删除重建 / manual_pending |
| `probe_i4_impact.py` | QA-03 影响面实证：规格漂移后订单是否照发 |
| `probe_gh_ast_contract.py` | G4 利润口径 AST 复核 + H 段前后端契约 diff |
| `probe_lock_ab.py` | A/B 组对照：AI 任务是否造成全库写锁 |
| `probe_commit_audit.py` | 请求级会话是否自动提交（113 个路由处理函数扫描） |
| `probe_bugs_repro.py` | 早期 P0 的单元级复现 |
| **第三轮新增** | |
| `probe_r3_p0_recheck.py` | S0 当前代码指纹 + QA-05 落库 + QA-01/02（409/1006 + **DROP 索引反向验证**）+ QA-13 无 session 拒绝 |
| `probe_r3_qa06.py` | QA-06：注入必然失败的 SQL，验证 `errors` / `incomplete` 可区分 + `validate` fail-closed |
| `probe_r3_place_purchase_tc.py` | `place_purchase` 全链路：跨请求可见 / `manual_pending` 不伪装 `placed` / 未匹配 409 |
| `probe_r3_place_purchase.py` | 同上（真实 HTTP 版，被 8000 陈旧进程误导后改用 `_tc` 版；保留作过程记录） |
| `probe_r3_r1_and_ai.py` | R1 全链路（403→顶栏+1→P17→处置→回落→审计成对）+ AI 客户端 session 绕过复核 |

新增测试：`backend/tests/test_qa_known_defects.py`
（初版 4 条 `xfail` 回归用例 → 第三轮实测 **9 passed、0 个 `@pytest.mark.xfail`**，
已全部转为常驻断言）。

**环境清理**：本轮探针造的 `QA-*` 订单 / 采购单已物理删除
（`erp_order` 残留 3 条均为非 QA 数据）；`ai.client` 已复原 `file_bridge`、
miaoshou / yitao / local_csv 的 `declared_scopes` 已复原为合法值、
`uq_sku_mapping_shop_sku` 索引已确认重建（`ok=true`）。
