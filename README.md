# 自用电商 ERP（selfuse_ecommerce_erp）

> **自研只管上新，第三方只管履约，第三方永远不持有商品编辑权。**

1688 素材采集 → AI 图文重构 → 淘宝 / 抖店 / 拼多多上架 → 建立 SKU 映射 → 把映射交给第三方分销工具做订单履约。

| 项 | 内容 |
| --- | --- |
| 部署形态 | **Docker 容器（linux/amd64）为唯一交付形态**，镜像由 GitHub Actions 构建并推到 GHCR；SQLite 零配置起步，`DATABASE_URL` 可切 PostgreSQL。源码态可在 Windows 直接跑（仅开发用） |
| 后端 | FastAPI + SQLAlchemy 2.0 + Alembic + Pydantic v2 + APScheduler |
| 前端 | React 18 + TypeScript + Vite + Ant Design 5 |
| 设计文档 | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |

---

## 一、目录结构

```
selfuse_ecommerce_erp/
├── README.md                 本文件
├── .gitignore
├── docs/
│   ├── PRD.md                产品需求文档
│   ├── ARCHITECTURE.md       系统架构设计（DDL / 接口契约 / 任务分解）
│   └── API_CONTRACT.md       （建议）由 OpenAPI 导出的契约快照
├── backend/
│   ├── requirements.txt
│   ├── alembic.ini           迁移配置（script_location=alembic）
│   ├── pytest.ini
│   ├── .env.example          ★ 复制为 .env 后按需修改
│   ├── app/
│   │   ├── main.py           FastAPI 应用工厂
│   │   ├── core/             配置 / 数据库 / 响应体 / 错误码 / 日志 / 安全 / 依赖 / 分页
│   │   ├── models/           24 张表（SQLAlchemy 2.0，Alembic 真源）
│   │   ├── schemas/          Pydantic DTO（T-A05/T-A06）
│   │   ├── adapters/         ★ 适配器层（listing / fulfillment / ai / source）
│   │   ├── services/         领域服务层（T-A05/T-A06）
│   │   ├── tasks/            异步任务框架（TaskRunner + APScheduler + 重启恢复）
│   │   ├── api/              FastAPI 路由层（T-A07）
│   │   └── utils/            crypto / csvio / kit
│   ├── alembic/              迁移脚本
│   ├── tests/
│   └── scripts/
├── web/                      React 前端（T-B01~T-B05）
└── data/                     运行时数据（SQLite / 素材 / 素材包 / AI 队列，已 gitignore）
```

---

## 二、快速开始

> 第 1~5 步是**源码开发态**（开发机为 Windows）。**生产部署请直接跳到第 6 步 Docker 容器** —— 那是当前唯一交付形态，桌面 exe 已废弃。

### 1. 准备 Python 环境

```bash
# 使用托管 Python（也可用自己的 3.11+）
C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe -m venv C:\Users\Administrator\.workbuddy\binaries\python\envs\default

# 激活后安装依赖
cd backend
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 至少修改 SECRET_KEY / ADMIN_TOKEN / OPERATOR_TOKEN
```

### 3. 建库（Alembic 迁移）

```bash
cd backend
alembic upgrade head
```

默认会在项目根创建 `data/erp.db`（SQLite，已启用 WAL + busy_timeout + 外键约束）。

### 4. 启动服务

**Windows 一键启动（推荐）**：双击 `backend/scripts/start_dev.bat`（已设好所有必需环境变量，并会先检查 8000 是否被占用）。

**手动启动**（在 `backend/` 目录下）：

```bash
set PYTHONPATH=.
set APP_ENV=dev
set SCHEDULER_ENABLED=true
set TASK_RECOVERY_ENABLED=true
set ADMIN_TOKEN=admin-token
set OPERATOR_TOKEN=operator-token
set SECRET_KEY=demo-secret-key
C:/Users/Administrator/AppData/Local/Programs/Python/Python311/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

三个必须注意的点：

1. **`SCHEDULER_ENABLED=true` 不能省** —— 省了周期任务（订单同步、库存轮询、映射检测、**已匹配订单自动下单**）不会装配。
2. **必须用 Python311 的绝对路径** —— bash 里的 `python` 会解析到 3.13.12 且缺依赖，启动会失败。
3. **先确认 8000 未被占用** —— 重复启动的 uvicorn 会因端口冲突**静默退出**（看起来没报错，其实没起来）。

**启动后自检**（确认拿到的确实是当前代码，别验到旧进程上）：

```bash
curl http://127.0.0.1:8000/api/v1/health     # status=ok 且 constraints.ok=true
curl http://127.0.0.1:8000/openapi.json      # 应含 /api/v1/orders/{order_id}/place-purchase
```

- 健康检查：<http://127.0.0.1:8000/api/v1/health>
- 接口文档：<http://127.0.0.1:8000/docs>

> ⚠️ 服务进程**不会常驻**。用后台任务方式启动的话会随会话结束被回收；需要长期挂机请用上面的 `.bat` 或自己在独立终端里启动。

### 5. 前端（T-B01 完成后）

```bash
cd web
npm install
npm run dev      # http://localhost:5173，已代理 /api → 127.0.0.1:8000
```

### 6. 容器部署（Docker —— 生产 / 唯一交付形态）

目标环境：OpenWrt 路由器上的 Docker，**X86 架构**，故镜像平台固定 `linux/amd64`。
镜像由 GitHub Actions 构建并推送到 GHCR（`.github/workflows/docker-build.yml`），
**不在本机构建**（本机 Docker daemon 未运行）。

**① 准备文件**（放在同一个目录，如 `/mnt/sda1/erp/`）：取仓库根目录的
`docker-compose.yml` 与 `.env.example`，然后：

```bash
cp .env.example .env
# 编辑 .env：把 ADMIN_TOKEN 改成自己的随机串（★ 必填，不设置 compose 会拒绝启动）
```

**② 启动**：

```bash
docker compose up -d
docker compose logs -f        # 首次启动会自动建库（alembic upgrade head）
```

**③ 访问**：浏览器打开 `http://<路由器IP>:8000`。若 8000 已被路由器上别的服务占用，
只改端口映射的**左侧**（如 `"8080:8000"`），右侧容器内固定 8000 —— **容器不做端口顺延**。

**④ 数据与备份**（使用者明确要求"放一个目录 + 一键备份"）：

| 项 | 位置 |
| --- | --- |
| 宿主机数据目录（**唯一**需要备份 / 迁移的目录） | `./data/`（与 compose 文件同级） |
| 容器内挂载点 | `/app/data` |
| 内容 | `erp.db`（主库）+ 素材图片 / AI 产出 + `backups/`（一键备份产物） |

两种备份方式，**推荐第一种**：

1. **页面「数据备份」一键按钮**：`POST /api/v1/system/backup`，把整个 `data/` 打成
   zip 落在 `data/backups/`，最多保留 20 份、超限自动删最旧。
   ★ 它用 `sqlite3.Connection.backup()` 导出**自洽快照** —— 直接 `cp erp.db` 在 WAL
   模式下可能漏掉尚未 checkpoint 的 `-wal` 文件，拿到的是不一致的库（见第 19 条）。
2. **整个 `data/` 目录拷贝**：停容器后拷走；换机器时整个拷回，无需任何导入动作。

> ⚠️ **只拷 `erp.db` 会静默丢素材与 AI 产出**：应用照常启动、只是素材全空，
> 属于不易察觉的丢失。

**⑤ 升级 / 迁移**：`docker compose pull && docker compose up -d`（`data/` 在宿主机，
不受影响）。换机器：停容器 → 整个 `data/` 拷走 → 新机器同目录 → `docker compose up -d`。

**⑥ 管理端令牌是怎么到前端的**（不直观，改部署前先看懂）：

后端令牌是**运行时**环境变量 `ADMIN_TOKEN`；容器启动时 `backend/container_entry.py`
把它写成 `web/dist/runtime-config.js`（`window.__ERP_RUNTIME__.adminToken`），前端从那里读。
**令牌只落在容器可写层，不在镜像里**；改了 `ADMIN_TOKEN` 重启容器即生效，无需重新构建镜像。

> ★ 为什么不能用构建期 `VITE_ADMIN_TOKEN`：镜像随**公开**仓库推到 GHCR，谁都能拉取 ——
> 把令牌编进前端产物等于公开发布。这与 `web/.env.development` 里把该变量留空是同一条理由。
>
> ★ 别把这条理解成"局域网内的人拿不到令牌"：能访问页面的人就能读 `runtime-config.js`，
> 这与"令牌固化在产物里"在局域网范围内等价。真正消除的是**镜像公开**这一条泄漏。

**⑦ 与源码态的三处差异**（照抄源码态会踩）：监听 `0.0.0.0`（不是 `127.0.0.1`，否则
端口映射过来也连不上）、**不做端口顺延**、**不开浏览器**。

---

## 三、环境变量清单

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/erp.db` | 数据库连接串；切 PG 改为 `postgresql+asyncpg://...` |
| `SECRET_KEY` | `dev-only-secret-key-...` | 应用密钥，AES 密钥缺省由它派生 |
| `CREDENTIAL_KEY` | 空 | base64 的 32 字节 AES-256 密钥（推荐显式配置） |
| `ADMIN_TOKEN` / `OPERATOR_TOKEN` | `admin-token` / `operator-token` | 轻量鉴权 Token（`X-Operator-Token`） |
| `STORAGE_DIR` | `./data` | 素材 / 素材包 / AI 队列根目录 |
| `LISTING_MODE` | `manual` | 上架模式（实际生效值读 `SystemSetting['listing.mode']`）。★ 个体户 / 个人身份证店铺默认**半自动** |
| `FULFILLMENT_ACTIVE_ADAPTER` | `local_csv` | ★ 当前生效履约适配器（热切换核心） |
| `AI_CLIENT` | `file_bridge` | ★ AI 客户端：`file_bridge`（默认，WorkBuddy 文件桥）/ `http` / `mock` |
| `AI_BASE_URL` / `AI_API_KEY` / `AI_MODEL` | — | `http` 客户端用的 OpenAI 兼容接口配置 |
| `TASK_MAX_WORKERS` | `8` | 任务线程池并发 |
| `TASK_RECOVERY_ENABLED` | `true` | 启动时是否扫描恢复未完成任务 |
| `SCHEDULER_ENABLED` | `true` | 是否启用 APScheduler 定时调度 |
| `LOG_LEVEL` / `LOG_JSON` | `INFO` / `false` | 日志级别与是否输出 JSON |

---

## 四、架构红线（交付验收）

| # | 红线 | 落地位置 |
| --- | --- | --- |
| R1 | 第三方永远不持有商品编辑权 | `app/adapters/fulfillment/scope_guard.py` 定义白/黑名单；`FulfillmentAdapterFactory.create()` 内强制 `enforce_scope()` |
| R2 | 系统中不存在"第三方直写店铺商品"的代码路径 | 只有 `ListingAdapter` 有 `offline()` / `update_stock_price()`；`FulfillmentAdapter` 子类禁止 import `app.adapters.listing.*` |
| R3 | 第三方能力不可靠时业务不中断 | 8 项能力默认返回 `UNSUPPORTED`；统一 `invoke()` 包装，异常转 `FATAL` / `RETRYABLE`，按 `fallback` 降级 |

### 一键复现红线 R1（越权 scope 被拒 → 顶栏红点亮起）

演示环境默认三个履约适配器均声明**合法** scope（`order.read` + `logistics.write`），
`scope_check_status` 为 `passed`，开箱即用。想看红线拦截效果，执行下面一条命令即可
（让 `miaoshou` 声明一个商品编辑类 scope `item.write`）：

```bash
curl -X PUT http://127.0.0.1:8000/api/v1/adapters/fulfillment/miaoshou/config \
  -H "Content-Type: application/json" \
  -H "X-Operator: admin" \
  -H "X-Operator-Token: admin-token" \
  -d '{"declared_scopes":["order.read","item.write"]}'
```

预期结果（三步可验证）：

1. **返回 403 / 5003**：`第三方不得持有商品编辑 / 上架 / 下架 / 改价权限` ——
   是业务拒绝，**不是** 404（配不上）也不是 500（漏异常）；
2. **顶栏红点 +1**：`GET /api/v1/system/status-bar` 的 `unhandled_violation_count` 从 0 变 1；
3. **留痕可查**：`GET /api/v1/audit-logs?action_type=permission_change` 出现一条 `is_handled=0` 的记录。

处置完熄灯（管理员确认后红点回落）：

```bash
# 取第一条未处置越权
curl -H "X-Operator-Token: admin-token" \
  "http://127.0.0.1:8000/api/v1/adapters/violations?is_unhandled=true&page_size=1"
# 处置（{id} 换成上一步返回的 id）
curl -X POST -H "Content-Type: application/json" -H "X-Operator: admin" \
  -H "X-Operator-Token: admin-token" \
  -d '{"handle_note":"演示处置"}' \
  http://127.0.0.1:8000/api/v1/adapters/violations/{id}/handle
# 恢复合法 scope（红点已熄灭，状态回到 passed）
curl -X PUT -H "Content-Type: application/json" -H "X-Operator: admin" \
  -H "X-Operator-Token: admin-token" \
  -d '{"declared_scopes":["order.read","logistics.write"],"is_enabled":true}' \
  http://127.0.0.1:8000/api/v1/adapters/fulfillment/miaoshou/config
```

> ★ 说明：`scope_check_status` 只在**每次保存配置时**刷新。若直接改库或用旧数据启动，
> 可能出现「声明的 scope 合法、状态却还是 rejected」的不一致 —— 重新 PUT 一次上面的
> 合法配置即可复位。

---

## 五、关键设计说明

1. **上架模式默认半自动（`manual`）**：个体户 / 个人身份证店铺拿不到平台 API 资质是常态，
   `ManualListingAdapter` 因此是**主路径而非兜底** —— ZIP 素材包 + 预填表单 + 商品 ID 回填
   （★ `sale_price` 售价必填，缺失返回 422）+ 自动建映射，链路完整可用；
   `taobao/douyin/pdd` 真实适配器只留骨架 + TODO（待平台资质），`MockListingAdapter` 仅自测用。
2. **履约适配层配置驱动**：妙手 / 逸淘真实端点与字段映射外置在 `app/adapters/fulfillment/profiles/*.yaml`，不确定处标 `# TODO: 需实测确认`，代码做防御性解析（字段缺失不崩）；实测后改 YAML 不改代码。
3. **本地兜底 `local_csv` 是默认适配器**：零依赖即可跑通全链路，`match_sku` 直接查本地 `sku_mapping`（★ 映射权威源永远在自研侧）。
4. **SKU 映射是最高等级资产**：部分唯一索引 `uq_sku_mapping_shop_sku`（软删除不占唯一键）、软删除保留 180 天可回滚、逐字段变更日志、`version` 乐观锁、`spec_signature` 规格指纹；**六类**冲突检测 SQL 以常量形式放在 `app/models/mapping.py::get_conflict_queries()`，
   并按 `DETECTION_ORDER` 强制排序（`many_to_one` 跨平台铺货**先跑并剔除**，它是 P1 **永不拦截**）。
5. **AI 默认走 WorkBuddy 文件桥（`ai.client=file_bridge`）**：任务写入 `data/ai_queue/<task_id>/prompt.md` 等产出；
   超时抛可重试 `AiTimeoutError`（提示信息直指 prompt 文件），由 `TaskRunner` 按 `max_retry` 重试，**不静默降级**。
6. **不用 Celery + Redis**：`TaskRunner` 抽象接口 + APScheduler + ThreadPoolExecutor + `task_record` 持久化，启动时自动恢复未完成任务；未来换 Celery 只替换 `app/tasks/runner.py` 实现，业务代码零改动。
7. **金额一律存整数「分」**；时间统一 UTC + ISO8601；凭证明文永不落库（AES-256 密文 + 掩码）。

---

## 六、常用命令

```bash
# 生成迁移（模型变更后）
alembic revision --autogenerate -m "描述"

# 应用 / 回退迁移
alembic upgrade head
alembic downgrade -1

# 跑测试
pytest

# 代码检查
ruff check app
mypy app
```

---

## 七、★ 货源录入：手工录入 + CSV 导入（**真实主路径**，不是降级预案）

### 为什么要先手工录货源

1688 采集（`POST /source-products/collect`）需要**开放平台 AppKey / AccessToken**，
而本项目的使用者主体是**个体户 / 个人身份证店**——这个凭证大概率根本拿不到；
`Alibaba1688Adapter` 里 1688 开放接口的参数名与签名方式至今仍标着 `TODO: 需实测确认`。

结果是：**一条货源都进不来 ⇒ AI 重构 / SKU 映射 / 上架 / 订单匹配整条主路径无法起步**。
所以手工录入与 CSV 导入是**主路径本身**，它们**完全不调用 1688 适配器**（同一 FILE 里没有任何
`Alibaba1688Adapter` 的引用），且写入的数据与 1688 采集的数据在下游完全等价。

| 入口 | 方法 | 路径 | 说明 |
| --- | --- | --- | --- |
| 手工录入 | `POST` | `/api/v1/source-products/manual` | 单条，一次请求带 SKU 列表，同步返回（201） |
| CSV 导入 | `POST` | `/api/v1/source-products/import-csv` | `multipart/form-data` 上传 CSV，同步返回逐行结果 |
| 列表过滤 | `GET` | `/api/v1/source-products?source_platform=manual` | `manual` = 手工 / `alibaba1688` = 采集 |

> 管理端请求统一需要请求头：`X-Operator-Token: admin-token`（另可带 `X-Operator: 你的名字`）。

> ★ `source_platform` 的落法：约束是**不改表结构、不新增迁移**，因此来源不新增列，
> 而是落在两处互为印证的位置——`product_1688_id` 带 **`MANUAL-` 前缀**
> （如商品编码 `SZ-1002` ⇒ `MANUAL-SZ-1002`），以及 `params_json["source_platform"] = "manual"`。

### 7.1 手工录入

```bash
curl -X POST http://127.0.0.1:8000/api/v1/source-products/manual \
  -H "Content-Type: application/json" \
  -H "X-Operator: admin" -H "X-Operator-Token: admin-token" \
  -d '{
    "title": "纯棉圆领短袖T恤 夏季薄款",
    "product_code": "SZ-1002",
    "category_path": "女装/上装/T恤",
    "supplier_id": null,
    "cost_price": "18.00",
    "origin_url": "https://example.com/item/SZ-1002",
    "main_image_url": "https://example.com/img/SZ-1002-1.jpg",
    "skus": [
      {"spec_name": "颜色;尺码", "spec_value": "红色;XL", "sku_code": "SZ-1002-RED-XL",
       "cost_price": "18.00", "sale_price": "59.90", "stock_qty": 200}
    ]
  }'
```

返回（节选）：

```json
{"code":0,"data":{"id":12,"product_1688_id":"MANUAL-SZ-1002","source_platform":"manual",
 "created":true,"created_skus":1,"updated_skus":0,
 "skus":[{"sku_code_1688":"SZ-1002-RED-XL","spec_json":{"颜色":"红色","尺码":"XL"},
          "cost_price":"18.00","suggested_sale_price":"59.90","spec_signature":"..."}]}}
```

字段说明：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `title` | ✅ | 商品标题（≤512 字符） |
| `product_code` | ➖ | 你的商品编码/货号 → 存为 `MANUAL-<编码>`，**重复录入同一编码＝更新**（幂等，不会复制出第二份）；留空自动生成 `MANUAL-<时间戳>-<随机>` |
| `skus[]` | ✅ | **至少 1 条**。没有 SKU 的货源建不了映射、上不了架，属于"录得进去但后面走不通"的死路，录入阶段直接拒绝（400） |
| `skus[].spec_name` / `spec_value` | ➖ | 规格名 / 规格值；多规格用**英文分号**分隔且两边数量一致（`"颜色;尺码"` + `"红色;XL"`） |
| `skus[].sku_code` | ➖ | 货源 SKU 编码；留空按「商品编码 + 规格指纹」自动生成（`MANUAL-SZ-1002-<指纹12位>`），同一行反复导入落在同一个 SKU 上 |
| `skus[].cost_price` | ➖ | SKU 采购成本（元）；留空**回落商品级 `cost_price`**。成本为空会在映射校验里被判 `cost_invalid`（P0 拦截） |
| `skus[].sale_price` | ➖ | 建议售价（元）。受"不改表"约束暂存在 `params_json["sku_price_hints"]`，`GET /source-products/{id}` 的 `skus[].suggested_sale_price` 可取回，半自动上架回填时参考 |
| `skus[].stock_qty` | ➖ | 库存，默认 0 |
| `supplier_id` | ➖ | 必须先存在（不存在 → 400：`供应商 ID x 不存在（请先在「供应商」中创建）`） |
| `origin_url` / `main_image_url` | ➖ | 必须以 `http://` 或 `https://` 开头 |

### 7.2 CSV 批量导入

```bash
curl -X POST http://127.0.0.1:8000/api/v1/source-products/import-csv \
  -H "X-Operator: admin" -H "X-Operator-Token: admin-token" \
  -F "file=@backend/scripts/source_products_template.csv;type=text/csv"
```

返回：

```json
{"code":0,"message":"导入完成：新增 2 个商品 / 更新 0 个商品",
 "data":{"total":3,"created":2,"updated":0,"created_skus":3,"updated_skus":0,"failed":[]}}
```

**失败行逐行返回可读中文原因**（`failed[]`），运营照着改即可，不吞掉任何一行：

```json
"failed":[
  {"row":3,"identifier":"SZ-1003","reason":"第3行：商品标题不能为空"},
  {"row":4,"identifier":"SZ-1004","reason":"第4行：商品成本价「十五块」不是合法金额（请填数字，如 29.90）"},
  {"row":5,"identifier":"SZ-1004-001","reason":"第5行：SKU 编码「SZ-1004-001」在本商品内重复（第4行已使用，请改用不同编码）"}
]
```

列名（中文优先，同时兼容英文别名；表头大小写、空格、全半角括号都容错）：

| 列名 | 必填 | 兼容别名 | 说明 |
| --- | --- | --- | --- |
| `商品标题` | ✅ | `title` / `product_title` | 缺失则整份文件报 400，一行都不写 |
| `商品编码` | ➖ | `product_code` / `货号` | **同一编码的多行＝同一个商品的多个 SKU**；留空归入**上一个商品**（Excel 合并单元格的常见填法） |
| `类目` | ➖ | `category_path` | |
| `供应商ID` | ➖ | `supplier_id` | 数字，需已存在 |
| `商品成本价` | ➖ | `cost_price` | 元；作为 SKU 成本的回落值 |
| `原链接` / `主图URL` | ➖ | `origin_url` / `main_image_url` | 需 `http(s)://` 开头 |
| `商品状态` | ➖ | `status` | `on_sale` / `off_shelf` / `out_of_stock` |
| `SKU编码` | ➖ | `sku_code` | 留空自动生成 |
| `规格名` / `规格值` | ➖ | `spec_name` / `spec_value` | 多规格 `颜色;尺码` + `红色;XL` |
| `SKU成本价` | ➖ | `sku_cost_price` / `采购价` | 元 |
| `售价` | ➖ | `sale_price` / `销售价` | 元，作为建议售价 |
| `库存` | ➖ | `stock_qty` | |
| `备注` | ➖ | `remark` | 不写库，方便给自己留注记 |

其它行为：

* **编码**：模板文件是 **UTF-8 with BOM**（Excel 双击打开中文不乱码）；上传 GBK / GB18030 / UTF-8(无 BOM) 也能被自动识别。
* **规模**：单次 ≤ **500 行**、文件 ≤ **5MB**，超出直接 400（请拆分），不在半路失败。
* **幂等**：同一份 CSV 重复导入 ⇒ 第二次走 `updated`，不会造出重复商品 / SKU。
* **软删除**：被删除后又重新导入同一编码 ⇒ 商品与 SKU 自动复活，不会撞唯一键。

> 模板文件：`backend/scripts/source_products_template.csv`（含 2 个示例商品、3 行数据）。

### 7.3 录进去之后怎么用（主路径）

手工 / CSV 录入的货源与 1688 采集的货源在下游**完全等价**，可继续：

```
GET  /source-products?source_platform=manual        # 查到刚录的商品
POST /ai-tasks            {'source_product_ids':[id], 'target_platform':'taobao'}   # AI 重构
POST /ai-tasks/{id}/review {'action':'approve'}                                    # 审核
POST /sku-mappings        # 建 SKU 映射（source_product_id + source_sku_id）
POST /sku-mappings/validate  # 校验 non-blocking 才能上架
POST /publish-tasks       {'mode':'manual'}         # 半自动上架
POST /publish-tasks/manual/{id}/fill-back           # 回填店铺商品 ID（可复用建好的映射）
POST /orders/sync         # 订单同步，自动按店铺 SKU 编码命中上面那条映射
POST /orders/{id}/place-purchase                    # 生成采购单（本地兜底 = manual_pending）
```

一键验证脚本（**真实 HTTP**，打在已经跑起来的 8000 上）：

```bash
python backend/scripts/verify_manual_source_chain.py --base-url http://127.0.0.1:8000
```

---

## 八、首次体验建议：先把 AI 客户端切成 `mock`

默认 `ai.client = file_bridge`（WorkBuddy 文件桥，与 AI 协作的主路径）。
它需要**外部 AI 代理**把产出写回 `data/ai_queue/<task_id>/`；
在没有代理的环境里，AI 重构任务会停在 `running` 直到超时（默认 **600 秒 / 10 分钟**），
超时抛可重试的 `AiTimeoutError`，提示信息直指 `prompt.md`。

想**先完整看一遍链路**（采集 → 重构 → 上架 → 映射 → 订单 → 履约），建议临时切到内置 mock：

```bash
curl -X PUT http://127.0.0.1:8000/api/v1/settings/ai.client \
  -H "Content-Type: application/json" \
  -H "X-Operator: admin" -H "X-Operator-Token: admin-token" \
  -d '{"value":"mock","reason":"首次体验：先跑通全链路"}'
```

确认链路没问题后，切回 `file_bridge` 走真实协作：

```bash
curl -X PUT http://127.0.0.1:8000/api/v1/settings/ai.client \
  -H "Content-Type: application/json" \
  -H "X-Operator: admin" -H "X-Operator-Token: admin-token" \
  -d '{"value":"file_bridge","reason":"切回 WorkBuddy 文件桥"}'
```

---

## 九、已知限制（交付时请一并告知使用者）

| # | 限制 | 说明 / 规避 |
| --- | --- | --- |
| 1 | **1688 采集在离线 / 无凭证环境不可用** | `POST /source-products/collect` 能受理并返回任务，但 1688 需要真实网络与凭证。离线环境下采集产出 0 条（从零全链路验证里唯一失败项）。部署时需在「设置 → 凭证」配置 1688 凭证并保证出网。 ★ 补充：全部失败时任务**不再报 success**，会诚实地标为 `failed` 并把逐行明细留在 `result_json.failed[]`；货源请走第 **七** 节的手工录入 / CSV 导入（真实主路径）。 |
| 2 | **AI 重构依赖外部产出** | 默认 `file_bridge` 需外部代理写回；无代理时任务停在 `running` 至超时（600s）。首次体验可按上一节切 `mock`。 |
| 3 | **售后未做端到端闭环验证** | 已验证「售后列表可查询」；**未**验证售后单创建 / 退款提交 / 退货地址获取的完整链路（从零库里没有真实售后数据，不伪造证据）。 |
| 4 | **周期任务需要 `SCHEDULER_ENABLED=true`** | 订单同步、库存轮询、映射检测、**已匹配订单自动下单**都挂在 APScheduler 上。若以 `SCHEDULER_ENABLED=false` 启动，这些周期任务不会自动跑（HTTP 手工入口不受影响）。**自动下单已实测**：默认启动（不显式关闭 `SCHEDULER_ENABLED`）时，`purchase_place` 周期任务会自动为 `matched` 订单生成 `manual_pending` 采购单（实测订单 1、3 各生成一条 `LOCAL-*` 采购单，创建时间戳一致，属同一批次任务产物）。 |
| 5 | **冲突面板中 `one_to_many` / `duplicate_item` 恒为 0（预期行为）** | 部分唯一索引 `uq_sku_mapping_shop_sku` 让这两类冲突在**写入边界**就被阻断，检测 SQL 永远命中 0 行。因此改为：写入时返回 **409 / 1006** 明确业务错误 + 启动期校验索引是否真存在（`/health` 的 `constraints`）。保留一条永远查不到东西的检测比没有检测更危险，故不保留。 |
| 6 | **本地兜底下单不会真的下单** | `local_csv` 下采购单状态为 `manual_pending` 并导出采购清单 CSV，需人工去 1688 完成 —— 这是产品决策（绝不伪装成"已下单"），不是缺陷。<br>**补充（2026-10-09）**：`local_csv` 导出的采购清单 CSV 走的是**明文口径**（含买家姓名 / 电话 / 地址），人工去 1688 下单即等价于明文下单，会**消耗店铺解密额度**且**供应商拿到买家真实信息**（详见第 26 条 ③）。因此本地兜底只应在第三方履约不可用期间**短期**使用，不作为长期方案。 |
| 7 | **`scope_check_status` 仅在保存配置与启动期刷新** | 已在 `bootstrap` 中按声明的 scope **重算一次**（修掉"声明合法却仍显示 rejected"的不一致）。若直接改库，需重启或重新保存一次配置才会同步。 |
| 8 | **1688 采集当前完全不可用，且原本没有降级入口** | **事实更正（此前误判为"离线"，实为错判）**：实测网络是通的（`detail.1688.com` / `openapi.1688.com` DNS 解析正常，HTTPS 200），**不是离线**。真实根因是 **未配置 1688 AppKey / AccessToken**，且 `Alibaba1688Adapter` 中 1688 开放接口的**参数名与签名方式仍标 `TODO: 需实测确认`**——契约从未与真实开放平台核对过，**即使配了凭证也未必采得下来**。**重要更正**：`POST /api/v1/source-products/batch-import` 走的是**同一个 1688 适配器**，**并非**手工/CSV 入口。因此采到 ≥1 条之前，不能宣称全链路已通。应对：使用者主体为个体户/个人身份证店，1688 开放平台凭证大概率拿不到，故手工录入与 CSV 导入被定位为**真实主路径**而非降级预案，已补建 `POST /api/v1/source-products/manual`（手工录入）与 `POST /api/v1/source-products/import-csv`（CSV 批量导入），详见下一节。 |
| 9 | **遗留 P2 × 3（QA 判定不阻塞发布）** | ① `QA-07` 红线 R1 的 scope 校验只在工厂内生效——实测 `app/` 内 **0 处**脱离工厂实例化，且 `Capability` 枚举 8 项**完全没有商品写能力**，绕过也调不到 `item.write`，属纵深防御缺口而非可利用缺陷；② `QA-09` `strict_scope=False` 开关潜伏（默认 True，**0 个**调用点传 False）；③ `QA-12` `publish_service.py:383` 仍是字符串派发（功能正确，`normalize_capability()` 已兜底，仅静态可枚举性弱）。 |
| 10 | **开发库 SQLite 的 id 会复用（给后续开发者的坑，非产品缺陷）** | 表未使用 `AUTOINCREMENT`，rowid 会复用已删除的 id。因此「建临时数据拿到 id=N → 按 `id=N` 删除」**可能删到别人的数据**（本项目已真实发生过一次）。后续任何验证脚本 / 测试用例一律**按唯一业务键清理**（如 `platform_order_no`、`shop_sku_code` 前缀），严禁按 id 删。 |
| 11 | **验证结论的适用范围** | QA 第三轮 8 项复验中，`place-purchase` 的完整断言跑在**进程内** `TestClient` 上。放行前已在**真实 uvicorn 进程**复核通过：OpenAPI 共 103 条路径且含 `place-purchase`；重复下单被正确拒绝（`409 / 1005 订单状态 purchased 不允许下单`）；采购单确为 `manual_pending` 而非 `placed`（本地兜底绝不伪装成已下单）。 |
| 12 | ~~**「任务 success 但 0 条产出」会静默**~~ **（2026-10-08 已修复）** | 修复前：采集全部失败时 `task_record.status` 仍是 `success`，失败原因只存在于 `result_json.failed[]` 里，前端只看 status 会误以为采集成功了。修复后：**仅当 `created + updated == 0` 且 `failed` 非空**时降级为 `failed`（`error_code=TASK_RESULT_FAILED`，明细仍完整留在 `result_json.failed[]`，且**不重试**——凭证缺失是确定性问题，重试只会把结论推迟）；**局部失败（部分成功）仍维持 `success`**，避免把"采到一半"误报成全军覆没。实现：`app/tasks/runner.py` 支持处理器用保留键 `__task_status__` 声明失败结论，`app/tasks/handlers/source_collect.py` 按上述条件声明。 |
| 13 | **第三方履约 API 失效时没有一键止血开关（已规划为下一迭代）** | 妙手 / 逸淘 API 挂掉、物流回填超时或售后接口异常时，需到「设置 → 履约适配器」**手动**切换到 `local_csv` 本地兜底适配器。**止损路径存在（不是死局）**，但缺少一键 kill switch。该能力已规划为下一迭代第一项，含三条硬约束：① 只允许从平台适配器切到 `local_csv`（**不可反向**）；② 切换前展示在途任务数并要求确认；③ 切换后**在途任务仍按原渠道跑完，只有新任务走兜底**（与 FUL-P0-05 一致）。 |
| 14 | **严禁实现「越权即自动停用适配器」（反向约束）** | 越权的正确处置是：**拒绝启用 + 403 + 审计 + `scope_check_status=rejected`**，**绝不是**自动 `is_enabled=false`。若做成自动停用，**在途订单会因履约渠道突然失效而中断**，直接违反 FUL-P0-05。取舍原则：**安全红线与业务连续性冲突时，阻止新的越权启用，但不破坏已建立的履约链路。** |
| 15 | ~~**自动下架不区分数据源**~~ **（2026-10-09 已修复）** | 修复前：`InventoryService._apply_actions` 的自动下架**没有数据源前置条件**，手工 / CSV 导入的商品若库存为 0 或留空，会被 `inventory_sync` 判为"库存归零"而**自动下架在售商品**（命中核心风险 ③）。<br>修复后：**自动下架仅对「最近一次库存快照来源 = 自动同步（`erp_poll` / `third_party_push`）」的 SKU 生效**；来源为 `manual_import` / `manual_edit` 或**无快照**的一律**不自动执行**，只告警 + 一键下架入口（后端告警接口已新增 `data_source` / `auto_offline_allowed` 两个字段；**前端已落地** —— 告警列表显示「库存数据源」与「可否自动下架」两列）。判定键严格用 `inventory_snapshot.source`、**不用** `source_platform`（铁律 R4 禁的是"能力可用性"分支，此处是"破坏性动作门槛"分支，见架构文档 §5.8）。<br>**配套修复（同样重要）**：`sync()` 原先对所有 SKU 一律写 `source="erp_poll"`，使上述判定键形同虚设（手工 SKU 也标着 erp_poll，照样被下架）——现已按"这个数到底是怎么进系统的"逐 SKU 如实标注。<br>**边界**：门槛只作用于**缺货类**告警；涨价类（`PriceSnapshot` 判据，另一份数据）维持原行为，避免把运营显式配置的"防亏本卖"静默降级成只告警。<br>**真实 HTTP 举证**：`python scripts/verify_inventory_source_gate.py`（12/12 通过；CSV 导入且在售的 A 组 `status=on_sale`，1688 采集且库存归零的 B 组 `status=off_shelf`）。 |
| 16 | **手工 / CSV 录入的商品：库存告警取决于是否被轮询到（见第 24 条）** | 行为分两段，别混为一谈：① **导入/录入那一刻不写** `inventory_snapshot`（`SourceService.create_manual` / `import_csv` 都不写），这是下一条要说的检测盲区；② 一旦 `inventory_sync` 轮询到该 SKU（缺省会轮询全部未删除的 `source_sku`）：**手工录入 / CSV 导入的商品由 `sync()` 生成来源为 `manual_import` 的库存快照，会产生库存告警但不会被自动下架，只告警 + 一键下架入口**（**入口有两处**：①「平台商品管理页」P10 `/listing-products`（单条或勾选批量）；②「库存监控页」P14 `/inventory` 告警 Tab 的「一键下架」按钮 —— **2026-10-10 新增**。此前告警 Tab 只有标签、**没有可点击动作**，看到告警后必须自行跳到 P10 才能操作，本轮补上这个缺口）<br>**配套后端字段**：告警 VO 新增 `listing_product_ids`，语义是「与该货源 SKU 关联、且**当前不是 `off_shelf`** 的平台商品 ID 列表」，**可能为空**（无关联商品或关联商品都已下架），为空时按钮禁用并说明原因。两条设计理由别弄反：① **必须是复数** —— 同一货源 SKU 可供给多个店铺 / 平台，跨平台铺货是正常业务（`models/mapping.py` 注明"仅提示，永不拦截"），用单数会静默漏掉其余商品；② **必须排除已下架** —— `ListingService.offline()` 对已下架商品**不幂等**，会抛 409 / 1005，不排除就是"一点就报错"。但**不是只保留 `on_sale`**：`publishing` / `failed` 同样能下架，把它们排除会造成"有货可下却被判成没有"）（这正是第 15 条门槛的效果："不下架 ≠ 不告警"）。旧文案称"手工商品刻意不写快照以免被误判成有自动来源"已**不再成立**：现在的做法是**如实标注来源**，而不是拒绝记录。 |
| 17 | ~~**切换 AI 客户端时，在途任务不受保护**~~ **（2026-10-10 已修复：数据结构层面）** | 旧口径问题：设计口径要求"切换 `ai.client` 后在途任务仍按**原**客户端跑完"，但 `ai_task` 表**不记录**创建时使用的客户端 ⇒ **该要求在数据结构上无法实现**。<br>**修复（0004 迁移）**：`ai_task` 补上三列 —— `task_type`（AI 任务类型：`ai_rework` 图文重构 / `image_redraw` 图片重绘 / `title_suggest` 标题建议 / `video_script` 视频脚本）、`input_prompt_json`（使用者输入的全局 + **逐图**提示词）、`ai_client`（**创建时固化**的客户端名）；存量行回填 `ai_rework` / `file_bridge`。同时 `AiTaskService.create_tasks()` 已把当时生效的客户端（含 `SystemSetting['ai.client']` 覆盖）写进 `ai_client`，新增任务不再依赖代码默认值。<br>**仍未完成的部分（下一棒）**：把这个值**用起来** —— `run_task()` 目前走的是通用的 `AiClientFactory.create()`（读当前配置），还没改成"按 `task.ai_client` 取客户端"，所以切换配置后在途任务的保护尚未真正生效；此外老数据（0004 之前）的 `file_bridge` 是按文档默认**推断**的，不是当年实测快照。<br>**规避方法（仍然有效）**：在有历史任务的环境里，切换前先等所有 AI 任务结束，或先取消在途任务再切换。 |
| 18 | ~~**上架任务从未真正入队却返回 202**~~ **（2026-10-08 已修复）** | 修复前：`POST /publish-tasks` 返回 **202**，但 `task_record_ids` 恒为**空数组**、`publish_task.task_record_id` 全 NULL（实测 18/18）、`task_record` 里 **0 条** `publish`，16 条任务永远停在 `pending_precheck`，只有手工「回填商品 ID」能推动 —— 典型的"返回体说成功、任务永远不跑"。根因两条叠加：① 请求会话 `add(task) → flush` 后**未提交**，`TaskRunner.submit()` 另开会话写 `task_record`，SQLite 单写者模型下必然 `database is locked`；② 该异常被 `except Exception: return None` **吞掉**。修复：**先 commit 再入队**（根治写锁）+ **入队失败抛 500 / 1098**（`ErrorCode.TASK_SUBMIT_FAILED`），并把失败原因写入 `publish_task.error_advice`（列表页也看得到，不只体现在 HTTP）。同一批修复还纠正了**上架前映射校验的校验对象**：旧实现拿 `{平台}-{任务ID}-{序号}` 这组**尚不存在**的店铺 SKU 编码去查映射，而映射只在 `publish()` / `fill_back()` **成功之后**才由 `_persist_listing()` 自动建立 ⇒ 首次上架**必然**命中「N 项映射缺失」被硬拦截，半自动主路径（映射靠回填才产生）永远走不到 `pending_publish` —— "查一个还不存在的东西是否存在"不是校验，是恒失败。现在校验对象是**该商品在该店铺已存在的映射**（一个都没有就不算缺失），P0 冲突与「检测不完整 fail-closed」仍照旧拦截；映射完整性的真正兜底在**订单匹配阶段**（`MAPPING_MISSING` → 订单挂起）与回填后的 `cost_underwater` 重算。真实 HTTP 举证：`python scripts/verify_publish_enqueue.py`（9/9 步通过：`task_record_ids=[33]`、任务终态 `success`、`publish_task` 推进到 `publishing`、`validate_result.blocking=false`）。入队失败路径（500 / 1098）由 `tests/test_publish_enqueue_visibility.py` 覆盖（真实服务上无法凭空让队列不可用，故在完整 HTTP/ASGI 栈上把 runner 打桩成抛错）。 |
| 19 | **SQLite 已启用 WAL，但该优化**只对本机 SQLite 生效** | 生效范围：仅 `DATABASE_URL` 为 `sqlite*` 时生效（`app/core/database.py` 在每条连接上设 `journal_mode=WAL` + `busy_timeout` + `foreign_keys=ON` + `synchronous=NORMAL`）；**切到 PostgreSQL 后走原有连接池配置，这条分支完全不参与**。WAL 是**库文件的持久化属性**（写进文件头），设一次即可，故 `alembic/env.py` 在迁移前也会设一次，避免新库以 DELETE 模式诞生。**连接池语义未改**：SQLite 仍用 `NullPool`（单写者模型下刻意不复用跨事件循环的连接）。<br>**如何确认真的开了**：启动日志里会有 `sqlite_journal_mode journal_mode=wal wal_enabled=True`；若被静默退回，日志会打 `sqlite_wal_not_effective`（常见于网络盘 / 只读挂载 / 被他人持写锁）。<br>**残余风险**：WAL 只解决"读不阻塞写"，**写仍然串行**（SQLite 单写者）。同时跑两个 pytest 进程会出现 `attempt to write a readonly database` 这类**假故障**，单独重跑即绿——不要据此改代码；操作规程见**第 25 条**。 |
| 20 | **顶栏 `unhandled_violation_count` 是"防线在工作"的计数，不是故障计数** | 该数字 = `audit_log` 中**未处置**的越权拦截记录条数（见第四节红线 R1）。它**非零是预期状态**：R1 探针会**故意**用越权 scope 打一次，命中即证明 `ScopeGuard` 真的拦住了，被拦的请求同时落审计 + 顶栏红点。**不要把它清零来"让面板变绿"** —— 清零等于把告警证据擦掉。要求：UI 必须能展开这 N 条，看到**适配器名 + 越权动作 + 时间**，否则使用者只看到数字不知道该修哪个适配器。处置动作是「确认并修正 scope」（第四节第 3 步），处置后计数自然回落。 |
| 21 | **本机服务起停：起不来时先分清"端口被占"还是"进程被回收"，两者现象一样但处置完全不同** | **2026-10-09 修订（保号改字）**：原写法把启动失败笼统归因为"脱离父进程导致进程被回收、且静默失败"，**这个归因是错的、且有害** —— 它把"有明确日志可查的端口冲突"误导成了"玄学故障"。实测拿到完整日志后确认为两类：<br>**① 端口被占（更常见）**：uvicorn **不会静默**，它会明确打印 `[Errno 10048] error while attempting to bind on address ('127.0.0.1', 8000)` 然后 `Waiting for application shutdown` 自行退出。注意它**先把应用完整启动一遍**（建库、装调度器、启 watchdog）**再**绑端口失败，所以日志前半段一片 `INFO` 看起来"启动成功了" —— **必须看日志最后几行**。<br>**成功 / 失败的判据只有一行**（实测）：末行为 `Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)` ⇒ 真的起来了；末行为 `Application startup complete.` 紧接 `Errno 10048` ⇒ 没起来。**`Application startup complete.` 出现在绑端口之前，不能单独作为成功标志**（我曾据此误判成"启动成功然后死掉"）。处置：`netstat -ano \| grep 8000` 找到 LISTENING 的 PID，`tasklist /FI "PID eq <pid>"` **确认是谁的实例再决定是否结束**（2026-10-09 实测：本机曾同时存在多个 uvicorn 实例互抢 8000，其中一个正在正常跑 `order_sync` 任务，被当成"占端口的僵尸"误杀会直接打断别人的验证）。**协作约定：各人固定用自己的端口（如 8011 / 8012），不要都挤默认 8000**；谁要清端口，先在群里认领，别默默 `taskkill`。<br>**最常见的成因其实是"自己重复启动"**：后台任务里往往已经有一个实例还活着（多半是自己上一轮留下的），再起第二个**必然** 10048 —— 起之前先 `netstat`，看到已有 LISTENING 就**直接复用**，不要另起。判据很直接：存活实例的日志里 10048 出现 **0 次**，被挤掉的那份出现 **1 次**。<br>**启动时务必把输出重定向到文件**（`... > data/uvicorn.log 2>&1`）：后台 / 脱离式启动下 stdout 会随进程一起丢掉，**没有落盘的日志就只剩猜** —— 这是本条限制唯一可靠的兜底手段。<br>**② 进程被回收（确有此事，但仅限后台任务托管的方式）**：表现为无任何日志、端口直接空掉。长期驻留请改用系统级托管（Windows 服务 / 任务计划程序）。<br>⚠️ 此前记录的"① `cmd //c start /MIN` ② PowerShell `Start-Process` 静默失败"**当时未取到日志，归因未经证实**，很可能是同一类端口冲突或日志重定向丢失，**不要据此认定这两种启动方式本身有问题**。**★ 正确做法（最重要的一条）**：需要真实 HTTP 验证时，在**同一个命令内**完成「启动 → 取证 → 收尾」，**不要跨命令依赖"上次起的那个还在"** —— 本机**没有可靠的常驻开发服务**，后台任务托管的进程会随任务生命周期结束被回收，**这与代码无关**（不是你的改动把它弄挂的）。启动后**必须**用 `GET http://127.0.0.1:8000/api/v1/health` 与 `GET /openapi.json` 各确认一次再继续（前者证明活着，后者证明代码版本对——改完代码不重启，OpenAPI 里看不到新端点，这是本项目最容易白跑一轮的地方）。确实需要长期驻留时改用系统级托管（Windows 服务 / 任务计划程序）。<br>**多人共用一台机器时**：起服务请**显式指定自己的端口**，不要用默认 8000，避免与他人的实例互抢；占用前先 `netstat -ano \| grep <port>` 确认归属，**不要看到端口被占就直接 `taskkill`**（今天已真实发生过一次：一方杀掉了另一方的验证实例）。<br>**注意**：该限制**不写脚本绕过**——绕过只会把"起没起来"这个事实藏起来，下次照样踩。 |
| 22 | **`/source-products/batch-import` 仍走 1688 适配器（前端已改指向，后端保留）** | 前端「批量导入」已改接 `POST /source-products/import-csv`（multipart，同步逐行回执），不再指向 `batch-import`；但后端 `/batch-import` **仍在**（历史接口，走 1688 开放接口）。在凭证缺失时它会**受理任务然后产出 0 条**，且按第 12 条已修复的语义会诚实标为 `failed`。**新使用者请一律走第七节的手工录入 / CSV 导入**，`batch-import` 仅在 1688 凭证已配置且契约核对完成后才应使用（其参数名与签名方式仍标 `TODO: 需实测确认`）。 |
| 23 | **并发改同一批文件会互相覆盖（协作类限制，非代码缺陷）** | 本项目同一天内出现过 `publish_service.py` / `scheduler.py` / `web/src/api/catalog.ts` 被多人先后写、改动被覆盖的情况，直接后果是「报'未修'但实际已修」的误判。**约定**：改动前先重读目标文件确认当前内容；交付结论以**重新打开文件看到的行号与内容**为准，不以记忆或他人转述为准。<br>**2026-10-09 再次复发（同一文件两次）**：`app/services/inventory_service.py` 在修复第 15 条时被并发写入两次，第一次覆盖掉了已完成的修复，第二次写入了**不同判定键**的另一版实现。教训已沉淀为两条硬规矩：① 同一文件同一时间只允许一名 owner，改前先在群里认领；② 发现目标文件被别人动过，**先协作收敛判定键再落地**，不要各自 overwrite。 |
| 24 | **手工录入 / CSV 导入在首次 `inventory_sync` 之前没有 `inventory_snapshot` —— 只存在一个短窗口，期间不参与库存告警（下一迭代补）** | 补的第 15 条门槛解决的是"别误下架"，本条是它的**代价**，必须同时告知使用者。<br>**现状**：`SourceService.create_manual` / `import_csv` **本身不生成**库存快照；但 `InventoryService.sync()` 会遍历**全部**未删除 SKU（`inventory_service.py:82-85`）并逐 SKU 按来源**如实**标注写入（`:101-120`，手工 / CSV 商品 → `manual_import`）。因此**盲区只存在于"导入后、首次 sync 之前"这一个短窗口**：窗口内该类商品既不产生缺货告警也不会被自动下架；**一旦被 `sync()` 轮询到，它们就会产生库存告警，只是仍不参与自动下架** —— 第 15 条那道门槛拦的是**破坏性动作**，不是让告警消失。若以 `SCHEDULER_ENABLED=false` 运行且无人手工触发 `POST /inventory/sync`，这个窗口会一直存在。<br>**为什么本轮不补**：一旦补写快照，手工商品会立刻开始产生缺货告警，安全性将**完全依赖**第 15 条那道门槛。在门槛刚修好、同一文件已被并发覆盖两次的情况下，把"盲区"这层意外保护换成"只靠门槛"是不划算的取舍 —— **盲区的代价是"库存告警对主路径（手工录入）不可用"（可逆），补了之后门槛失效的代价是"在售商品被批量误下架"（不可逆）**。<br>**规避**：人工定期跑一次 `POST /api/v1/inventory/sync`，或确认 `inventory_sync` 周期任务已装配。<br>**下一迭代**：目的是**缩短首次同步前的窗口**（在导入 / 改库存时即补写 `source='manual_import'` 的快照并触发冲突重算，架构文档 §5.8 的 INV-P0-05 口径）—— **不是"让库存告警可用"**：轮询之后告警本来就已经可用，补它只是为了消灭窗口。<br>**2026-10-09 措辞修订（保号改字，编号 24 不变）**：原写法（"存在检测盲区" + "下一迭代补写快照"）会让读者以为"库存告警对手工商品整体不可用"，从而低估现有防护、或**立项去重做一个已经存在的功能**；已按 PRD §10.1 L1 核实后的事实修订。 |
| 25 | **操作约定：同一时间只开一个 pytest 进程（并发跑会产生"假故障"）** | SQLite 是**单写者**，WAL 只解决"读不阻塞写"，**写仍然串行**（见第 19 条）。同时开多个 pytest 进程时，worker 线程会被饿到超过 stale 阈值，后果有三类，**每一类都长得像真 bug**：① 报 `sqlite3.OperationalError: database is locked` / `attempt to write a readonly database`；② 耗时从 **1m49s 膨胀到 5m40s**；③ 最坏的是**时序类用例被误判** —— 2026-10-09 实测：三个 pytest 进程并发时 `tests/test_task_runner_hygiene.py::test_deterministic_business_error_is_not_retried` 失败（`retry_count=1`，违反了"确定性错误不重试"），一度被当成语义回归；该归因**已排除，真实原因仍在取证**：`reclaim_stuck_tasks()` 曾被列为嫌疑，但三条证据否定它 —— ① `task_stuck_timeout_sec` 默认 **900s（15 分钟）**（`app/core/config.py:77`），而那次失败运行总耗时仅 **5m40s**，时间上不可能；② `start_watchdog()` 全项目**只有 `app/main.py:97` 一个调用点**（在 lifespan 内），pytest 走 ASGITransport **不触发 lifespan**（`conftest.py` 自身即如此），**看门狗线程在 pytest 进程里从未存在过**；③ pytest 中唯一调用 `reclaim_stuck_tasks()` 的是 `test_task_transaction_hygiene.py:286`，传的是只记录不执行的 `_StubRunner`，回收了也不会重跑，产生不了 `retry_count=1 + status=failed`。<br>当前候选（**未证实**）：第一次执行在准备阶段（`mark_running` / commit）抛了**可重试**的 `OperationalError` → 重试 → 第二次才跑到 handler 抛 `BusinessError` —— 这是唯一能同时解释 `status=failed` 且 `retry_count=1` 的序列。取证方法：查 `task_failed` 日志里 `will_retry=True` 的 `error_type`。<br>**被推翻的是根因解释，不是这条操作约定** —— "单独重跑 5 次全绿、独占跑全量 147/0"是实测事实，"并发跑 pytest 会产生假故障"的结论不受影响。<br>**元教训（与第 23 条同源）**：这里曾把"合理推断"当成"已查清的结论"写进文档。**文档里写"查清后是 X"之前，X 必须有硬证据，不能是看起来合理的推断** —— 否则下一个人会沿错误根因去改本来正确的代码。，**单独重跑 5 次全绿、独占跑全量 147/0**。<br>**约定**：① 跑全量套件时**一次只开一个进程**；② 看到失败**先单独重跑该用例再定性**，不要直接改代码；③ 确需并行（如 CI 分片）必须给每个进程配**独立的 `DATABASE_URL`**，绝不共用同一份 `data/erp.db`；④ 判定回归的标准是"独占跑也失败"，不是"某次跑失败"。<br>**为什么这条值得单列**：假故障比真 bug 更浪费时间——真 bug 查下去有结果，假故障查下去会改坏本来正确的代码。 |
| 26 | **外部事实核实：三条硬边界（使用者实测核清，不是猜测）** | **① 个体户商品发布 API：分平台差异明显，不是稳过 —— 所以半自动是主路径，不是兜底**<br>抖店：个体户可申请 `/product/addV2`，应用描述写「自用，仅管理本人名下店铺，不对外提供 SaaS」通过率高，但 QPS 低于企业主体。<br>淘宝 TOP：个体户能申请 `taobao.item.add`，但近年审核收紧，大批量上新场景易被驳回；个人开发者几乎拿不到。<br>拼多多：可创建商家自研应用，但审核严格，会核验业务真实性。<br>→ 上架以**半自动为主路径**，API 全自动是**可选备选**，不要把业务押在"API 能过审"上。被驳回时的预案：ERP 生成图文 → 调用平台图片上传 → 半自动提交商品表单（可用 Playwright / WorkBuddy 完成网页填表上架）。<br><br>**② 妙手 / 逸淘没有 SKU 映射写入 API，只能 CSV 导入 —— 本项目已按此设计，无需改代码**<br>**妙手一键下单（履约模块）≠ 妙手 ERP**：妙手 ERP 有完整开放 API 支持写入 SKU 货源映射；但**一键下单模块官方无 REST API 用于新增 / 修改 SKU 映射**，只提供 CSV 批量导入 + 后台手动录入，客服口径「开放 API 只在妙手 ERP 版本」。<br>逸淘：国内一件代发一键下单**完全没有开放 API**，仅 CSV 导入 + 后台手动绑定（逸淘开放 API 只有跨境版本）。<br>→ 正确用法：`POST /api/v1/sku-mappings/export` 导出 CSV（已含妙手配对所需的货源侧字段 `source_product_1688_id` / `source_sku_code_1688` / `spec_signature` / `purchase_cost`），人工上传到妙手一键下单后台。**`POST /sku-mappings/push` 的推送能力在三个适配器均为 skeleton，它只导出 CSV 并把状态诚实标为 `degraded`，绝不伪装成 `success`** —— 这不是缺陷，是"第三方根本没有写入 API"的如实反映。<br>量大时的预案：写轻量脚本自动登录妙手后台上传 CSV（网页自动化）；工具切换预案：优先妙手，政策变动时逸淘可无缝替换（同样 CSV 导入 + 密文履约）。<br><br>**③ 1688 密文下单只能靠第三方 ISV 资质，自研只能明文**<br>买家收货地址解密是**店铺维度敏感权限**，需审批且有**每日解密额度**，自研调用会持续消耗额度；ISV 走密文履约链路**不解密、不消耗额度**。<br>`alibaba.trade.fenxiaoOrder.create`（密文回流下单核心接口）是**定向邀约、仅对大型服务商 ISV 开放**，要求月回流万单级别 → 个体户 / 普通商家自研**拿不到**。<br>普通商家能申请的是 `alibaba.trade.createCrossOrder`，**只能明文下单**，必须传买家真实姓名 + 手机号 + 地址。两个缺点：① 消耗店铺解密额度；② 供应商拿到买家真实信息，存在**溯源找到店铺发起盗图投诉**的风险。<br>→ 自研**不碰**订单解密与 1688 分销交易接口；密文回流履约能力由妙手 / 逸淘的 ISV 资质提供（这正是买履约模块而不自研的理由）。 |
| 27 | **容器形态下数据全部落在宿主机一个目录 `data/` —— 换机器 / 升级前先备份，且别只拷 `erp.db`** | 容器启动时 `container_entry` 会先跑一次 `alembic upgrade head` 再拉起服务，**首次启动无需手工建库**（源码态仍按第二节第 3 步手工迁移）。<br>**数据位置**：`<compose 文件所在目录>/data/`，整体挂进容器 `/app/data`（详见第二节第 6 步）。**升级 / 换机器时把 `data/` 整个目录拷走**，这是唯一需要迁移的东西。<br>**备份走页面「数据备份」一键按钮**（`POST /api/v1/system/backup`，产物落 `data/backups/`，保留最近 20 份）—— 它用 `sqlite3.Connection.backup()` 导出自洽快照；手工 `cp erp.db` 在 WAL 模式下可能漏掉未 checkpoint 的 `-wal`，拿到不一致的库。<br>**迁移基线**：若库里已有业务表却没有 `alembic_version` 记录（早期用 `create_all()` 建的库就是这种状态），入口会执行 `alembic stamp head` **打基线而不是重放历史** —— 因为重放会立刻撞 `table ... already exists`。此分支会打印醒目提示，不会静默改版本记录。<br>**修复背景（重要）**：`alembic/env.py` 原先把外部 connection 交给 `context.configure`，此时 alembic 的 `begin_transaction()` 是 **no-op**（`_in_external_transaction=True`，且 `SQLiteImpl.transactional_ddl=False`），**提交责任在调用方**；而旧代码用 `async with connectable.connect()`，退出是**关闭连接 → 未提交事务被 ROLLBACK**。DDL 在 SQLite 下自动提交因而"表建出来了"，但 `INSERT INTO alembic_version` 被回滚 ⇒ **库建好却查不到版本号 ⇒ 第二次启动重跑 0001 直接崩溃**（容器形态下就是"能用一次，重启容器就打不开"）。现已改为 `async with connectable.begin()`，由 SQLAlchemy 负责提交。<br>**端口**：容器内**固定 8000，不做顺延** —— 顺延会让"我连的是不是被测物"失去依据。宿主机端口冲突改 compose 端口映射的**左侧**（如 `8080:8000`）。<br>**令牌**：`ADMIN_TOKEN` 未设置时 compose **拒绝启动**（`${ADMIN_TOKEN:?...}` 语法），因为默认值 `admin-token` 是代码里写死的公开已知值；设置后由容器入口注入前端运行时配置（第二节第 6 步 ⑥），**不要用构建期 `VITE_ADMIN_TOKEN`** —— 镜像是公开的，编进产物等于公开发布令牌。<br>**exe 形态已废弃**：`desktop_entry.py` / PyInstaller spec / Windows 构建工作流已删除；本条按"保号改字"规则改写为容器口径，**编号 27 不变**。 |
