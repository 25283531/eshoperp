"""`/api/v1` 总路由（T-A07）。

`app/main.py :: _mount_api_router()` 以 `prefix=settings.api_prefix` 挂载本路由。

★ 端点总数：按 ARCH §5.5 契约实现 **112 个 endpoint**
  （文档标注 90 个；此处按契约表格逐条落地，含文件下载流与 `/health` 之外的全部条目；
   `POST /assets/upload` 为 1688 权限未开通期间的手工上传入口，不计入文档标注的那一版）。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    adapters,
    after_sales,
    ai_tasks,
    assets,
    audit,
    backup,
    dashboard,
    inventory,
    listings,
    mappings,
    orders,
    publish,
    settings,
    source,
    tasks,
)

__all__ = ["api_router"]

api_router = APIRouter()

# 顺序只影响 OpenAPI 文档分组，不影响路由匹配（路径无冲突）
for _module in (
    dashboard,
    settings,
    source,
    assets,
    ai_tasks,
    mappings,
    publish,
    listings,
    orders,
    after_sales,
    inventory,
    adapters,
    audit,
    tasks,
    backup,
):
    api_router.include_router(_module.router)
