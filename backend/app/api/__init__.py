"""API 路由层（T-A07）。

分层职责：
    1. `app/api/router.py` —— 聚合 `/api/v1` 全部子路由；
    2. `app/api/v1/*.py` —— 按业务域拆分的端点模块（14 个）；
    3. 端点层**只做三件事**：解析入参 → 调 `app/services` → 用 `ApiResponse` 包装出参。

★ 端点层**不得**直接写业务逻辑或裸调适配器：
  业务规则一律在 `app/services/`，第三方能力一律经 `app/adapters/` + `invoke(Capability)`。
"""

from __future__ import annotations

__all__: list[str] = []
