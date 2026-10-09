"""自用电商 ERP 后端应用包。

分层约定（详见 docs/ARCHITECTURE.md）：
    app/core       基础设施（配置 / 数据库 / 响应体 / 错误码 / 日志 / 安全 / 依赖注入 / 分页）
    app/models     SQLAlchemy 2.0 数据模型（24 张表，Alembic 真源）
    app/schemas    Pydantic v2 请求 / 响应 DTO
    app/adapters   适配器层（上架 / 履约 / AI / 货源）—— 本项目架构核心
    app/services   领域服务层
    app/tasks      异步任务框架（TaskRunner + APScheduler + 重启恢复）
    app/api        FastAPI 路由层

两条架构红线：
    R1 第三方永远不持有商品编辑权 —— enforce_scope() 在 FulfillmentAdapterFactory.create() 中强制执行
    R2 只有 ListingAdapter 能写店铺商品 —— FulfillmentAdapter 及其子类禁止 import ListingAdapter
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
