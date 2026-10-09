"""全局配置（pydantic-settings）。

所有默认值均可通过环境变量或 backend/.env 覆盖，禁止在业务代码中硬编码常量。
配置键与 §4.4.6「核心配置键」一一对应，业务层应优先读 SystemSetting 表而非此处。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py
#   parents[0] = backend/app/core
#   parents[1] = backend/app
#   parents[2] = backend
#   parents[3] = 项目根
BACKEND_DIR: Path = Path(__file__).resolve().parents[2]
PROJECT_ROOT: Path = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """应用配置。环境变量前缀为空，直接以字段名读取（如 DATABASE_URL）。"""

    model_config = SettingsConfigDict(
        env_file=(str(BACKEND_DIR / ".env"), str(PROJECT_ROOT / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------- 应用 ----------------
    app_name: str = "selfuse-ecommerce-erp"
    app_env: Literal["dev", "test", "prod"] = "dev"
    debug: bool = True
    host: str = "127.0.0.1"
    port: int = 8000
    api_prefix: str = "/api/v1"
    docs_url: str = "/docs"

    # ---------------- 数据库 ----------------
    database_url: str = f"sqlite+aiosqlite:///{PROJECT_ROOT.as_posix()}/data/erp.db"
    db_echo: bool = False
    db_pool_size: int = 5
    db_max_overflow: int = 10
    # ★ 单位：**毫秒**（SQLite PRAGMA busy_timeout 的单位就是毫秒，不是秒）。
    #   含义：拿不到写锁时最多**等待**这么久再抛 "database is locked"，
    #   而不是立刻失败。仅对 SQLite 生效；PostgreSQL 走连接池自己的超时。
    sqlite_busy_timeout_ms: int = 8000

    # ---------------- 存储 ----------------
    storage_dir: str = (PROJECT_ROOT / "data").as_posix()
    assets_dirname: str = "assets"
    packages_dirname: str = "packages"
    exports_dirname: str = "exports"
    ai_queue_dirname: str = "ai_queue"
    ai_output_dirname: str = "ai_output"
    fulfillment_dirname: str = "fulfillment"

    # ---------------- 安全 ----------------
    secret_key: str = "dev-only-secret-key-please-change-in-env"
    credential_key: str = ""  # base64(AES-256 key)；留空则由 secret_key 派生
    admin_token: str = "admin-token"
    operator_token: str = "operator-token"
    anonymous_actor: str = "system"

    # ---------------- 异步任务 ----------------
    task_max_workers: int = 8
    task_max_retry: int = 3
    task_recovery_enabled: bool = True
    task_recovery_limit: int = 200
    # ★ 卡死看门狗：`running` 超过该秒数仍未结束 → 判定卡死，标记失败并重置重试
    task_stuck_timeout_sec: float = 900.0
    task_watchdog_interval_sec: float = 120.0
    task_watchdog_enabled: bool = True
    scheduler_enabled: bool = True
    scheduler_timezone: str = "Asia/Shanghai"

    # ---------------- 适配器默认 ----------------
    # ★ 用户决策 ①：个体户店铺 → 半自动（manual）为主路径（与 DEFAULT_SETTINGS 保持一致）
    listing_mode: str = "manual"
    fulfillment_active_adapter: str = "local_csv"
    # ★ 用户决策 ②：AI = WorkBuddy 文件桥 + HTTP API 双通道，默认走文件桥
    ai_client: str = "file_bridge"

    # ---------------- AI ----------------
    ai_base_url: str = "https://api.openai.com/v1"
    ai_api_key: str = ""
    ai_model: str = "gpt-4o-mini"
    ai_image_model: str = ""
    ai_timeout_sec: float = 60.0
    ai_poll_interval_sec: float = 3.0
    # ★ 默认等待外部 AI 产出的时间：600s（10 分钟）。
    #   原为 1800s（30 分钟）——一条没人处理的任务会空占半小时，
    #   期间 `ai_task` 停在 running、运营看不到任何进展也不敢重试。
    #   10 分钟既够人看到队列文件去处理，也够自动化代理写回。
    ai_poll_timeout_sec: float = 600.0
    # ★ 文件桥等待的**硬上界**：配置项再大也被压到这里（防止误配成"无限等待"卡死任务）
    ai_poll_timeout_max_sec: float = 1800.0

    # ---------------- HTTP ----------------
    http_timeout_sec: float = 20.0
    http_max_retry: int = 3
    http_retry_backoff_sec: float = 0.5

    # ---------------- 日志 ----------------
    log_level: str = "INFO"
    log_json: bool = False

    # ---------------- CORS ----------------
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"])

    # ---------------- 校验 ----------------
    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors(cls, value: Any) -> Any:
        """允许用逗号分隔的字符串配置 CORS 源。"""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("database_url")
    @classmethod
    def _resolve_relative_sqlite(cls, value: str) -> str:
        """★ SQLite 相对路径一律按**项目根**解析，杜绝 cwd 漂移。

        事故复盘：`.env.example` 里写过 `sqlite+aiosqlite:///./data/erp.db`。
        这个相对路径会随**进程工作目录**漂移 —— 以 `backend/` 为 cwd 启动 uvicorn 时，
        连到的是 `backend/data/erp.db`（一个 0 表的空库），
        于是"所有写接口集体 500、只读接口仍 200"，排查方向被带偏两轮。

        这里把任何相对 SQLite 路径钉死到 `<项目根>/...`，从代码层面消除该类事故。
        """
        if not value.startswith("sqlite") or "///" not in value:
            return value
        prefix, _, path_part = value.partition("///")
        if not path_part:
            return value
        # 绝对路径判定：以 / 开头，或 Windows 盘符（C:/ 或 C:\）
        if path_part.startswith("/") or len(path_part) > 1 and path_part[1] == ":":
            return value
        resolved = (PROJECT_ROOT / path_part).resolve()
        return f"{prefix}///{resolved.as_posix()}"

    # ---------------- 派生属性 ----------------
    @property
    def is_sqlite(self) -> bool:
        """当前数据库是否为 SQLite。"""
        return self.database_url.startswith("sqlite")

    @property
    def is_postgres(self) -> bool:
        """当前数据库是否为 PostgreSQL。"""
        return self.database_url.startswith("postgresql") or self.database_url.startswith("postgres")

    @property
    def db_dialect(self) -> str:
        """返回 'sqlite' / 'postgresql'，供 SQL 方言分支使用。"""
        if self.is_postgres:
            return "postgresql"
        return "sqlite"

    def dir(self, name: str) -> Path:
        """返回 storage 下的子目录并确保存在。"""
        path = Path(self.storage_dir) / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def assets_dir(self) -> Path:
        """素材存储目录（data/assets）。"""
        return self.dir(self.assets_dirname)

    @property
    def packages_dir(self) -> Path:
        """半自动素材包目录（data/packages）。"""
        return self.dir(self.packages_dirname)

    @property
    def exports_dir(self) -> Path:
        """导出目录（data/exports）。"""
        return self.dir(self.exports_dirname)

    @property
    def ai_queue_dir(self) -> Path:
        """AI 任务队列目录（data/ai_queue）。"""
        return self.dir(self.ai_queue_dirname)

    @property
    def ai_output_dir(self) -> Path:
        """AI 产出目录（data/ai_output）。"""
        return self.dir(self.ai_output_dirname)

    @property
    def fulfillment_dir(self) -> Path:
        """本地兜底履约数据目录（data/fulfillment）。"""
        return self.dir(self.fulfillment_dirname)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例配置对象。"""
    settings = Settings()
    # 确保存储根目录存在（SQLite 文件也在此目录下）
    Path(settings.storage_dir).mkdir(parents=True, exist_ok=True)
    return settings


def reload_settings() -> Settings:
    """清空缓存重新加载配置（测试与后台改配置后使用）。"""
    get_settings.cache_clear()
    return get_settings()


def env_flag(name: str, default: bool = False) -> bool:
    """读取布尔型环境变量，'1/true/yes/on' 为真。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
