"""系统模块：SystemSetting / AuditLog / Credential（§4.4.6）。

★ Credential 是统一密钥保险箱：凭证明文永不落库，只存 AES-256 密文 + 掩码。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BaseMixin
from app.models.enums import (
    CredentialStatus,
    SettingKey,
    ValueType,
)
from app.utils.kit import utc_now


# 核心配置键默认值（§4.4.6，PRD 8.5 配置驱动，禁止硬编码）
DEFAULT_SETTINGS: list[dict[str, Any]] = [
    # ★ 用户决策 ①：个体户 / 个人身份证店 → 半自动（manual）是**主路径**，
    #   真实平台适配器保持 skeleton + TODO。默认必须是 manual，
    #   否则新库一起来就是 Mock 模式，顶栏会一直挂"Mock"角标误导运营。
    {"key": SettingKey.LISTING_MODE.value, "value": "manual", "value_type": ValueType.STRING.value,
     "description": "上架模式：real / mock / manual（★ 默认 manual：半自动素材包 + 预填表单）"},
    {"key": SettingKey.FULFILLMENT_ACTIVE_ADAPTER.value, "value": "local_csv", "value_type": ValueType.STRING.value,
     "description": "当前生效履约适配器（热切换核心）"},
    {"key": SettingKey.FULFILLMENT_HEARTBEAT_INTERVAL_SEC.value, "value": "300", "value_type": ValueType.INT.value,
     "description": "适配器心跳周期（秒）"},
    {"key": SettingKey.FULFILLMENT_HEARTBEAT_FAIL_THRESHOLD.value, "value": "3", "value_type": ValueType.INT.value,
     "description": "心跳连续失败告警阈值"},
    {"key": SettingKey.INVENTORY_POLL_INTERVAL_MIN.value, "value": "30", "value_type": ValueType.INT.value,
     "description": "库存轮询周期（分钟）"},
    {"key": SettingKey.INVENTORY_PRICE_INCREASE_THRESHOLD.value, "value": "0.10", "value_type": ValueType.STRING.value,
     "description": "成本涨幅告警阈值"},
    {"key": SettingKey.INVENTORY_OUT_OF_STOCK_ACTION.value, "value": "offline", "value_type": ValueType.STRING.value,
     "description": "缺货处置：offline / notify_only"},
    {"key": SettingKey.INVENTORY_PRICE_INCREASE_ACTION.value, "value": "notify_only",
     "value_type": ValueType.STRING.value, "description": "涨价处置：offline / notify_only"},
    {"key": SettingKey.AI_MAX_CONCURRENCY.value, "value": "5", "value_type": ValueType.INT.value,
     "description": "AI 重构并发上限"},
    {"key": SettingKey.AI_MAX_RETRY.value, "value": "3", "value_type": ValueType.INT.value,
     "description": "AI 重构失败重试次数"},
    # ★ 用户决策 ②：AI 图文重构 = WorkBuddy 文件桥 + HTTP API 双通道，默认走文件桥
    {"key": SettingKey.AI_CLIENT.value, "value": "file_bridge", "value_type": ValueType.STRING.value,
     "description": "AI 客户端：mock / file_bridge（默认，与 WorkBuddy 协作）/ http"},
    {"key": SettingKey.PUBLISH_BATCH_SIZE.value, "value": "50", "value_type": ValueType.INT.value,
     "description": "单批次上架数量"},
    {"key": SettingKey.PUBLISH_RATE_LIMIT_PER_MIN.value, "value": "20", "value_type": ValueType.INT.value,
     "description": "发布速率（防限流）"},
    {"key": SettingKey.ORDER_SYNC_INTERVAL_MIN.value, "value": "5", "value_type": ValueType.INT.value,
     "description": "订单拉取周期（分钟）"},
    # ★ 履约主路径闭环：已匹配订单自动下单（此前 place_purchase 无任何入口，链路断在"已匹配"）
    {"key": SettingKey.ORDER_AUTO_PURCHASE_ENABLED.value, "value": "true", "value_type": ValueType.BOOL.value,
     "description": "已匹配订单自动触发采购下单（关闭则只能手工 POST /orders/{id}/place-purchase）"},
    {"key": SettingKey.ORDER_PURCHASE_INTERVAL_MIN.value, "value": "5", "value_type": ValueType.INT.value,
     "description": "自动下单任务周期（分钟）"},
    {"key": SettingKey.ORDER_PURCHASE_BATCH_LIMIT.value, "value": "50", "value_type": ValueType.INT.value,
     "description": "单次自动下单最大订单数（防瞬时打爆 1688）"},
    {"key": SettingKey.MAPPING_RETENTION_DAYS.value, "value": "180", "value_type": ValueType.INT.value,
     "description": "映射软删除保留天数"},
    {"key": SettingKey.MAPPING_AUTO_PUSH_ENABLED.value, "value": "false", "value_type": ValueType.BOOL.value,
     "description": "是否自动推送映射到第三方"},
    # ★ ARCH v1.4：成本倒挂（cost_underwater）级别可配，默认 P1 仅提示；改为 P0 即硬拦截
    {"key": SettingKey.MAPPING_CONFLICT_COST_UNDERWATER_LEVEL.value, "value": "P1",
     "value_type": ValueType.STRING.value,
     "description": "成本倒挂冲突级别：P1 仅提示（默认） / P0 禁止上架"},
    # ★ ARCH v1.4：判定倒挂时的最低利润率缓冲（0 表示成本≥售价即算倒挂）
    {"key": SettingKey.MAPPING_MIN_PROFIT_MARGIN.value, "value": "0", "value_type": ValueType.STRING.value,
     "description": "最低利润率缓冲（0–1），成本 ≥ 售价×(1-缓冲) 判定为倒挂"},
]


class SystemSetting(BaseMixin, Base):
    """系统配置 KV（配置驱动，禁止硬编码）。"""

    __tablename__ = "system_setting"

    setting_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, comment="配置键（点分小写）")
    setting_value: Mapped[str | None] = mapped_column(Text, nullable=True, comment="值（JSON 或标量字符串）")
    value_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ValueType.STRING.value, comment="string/int/bool/json"
    )
    description: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="说明（前端展示）")
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="更新人")

    def typed_value(self) -> Any:
        """按 `value_type` 将字符串值转为 Python 原生类型。"""
        raw = self.setting_value
        if raw is None:
            return None
        if self.value_type == ValueType.INT.value:
            try:
                return int(raw)
            except (TypeError, ValueError):
                return 0
        if self.value_type == ValueType.BOOL.value:
            return str(raw).strip().lower() in {"1", "true", "yes", "on"}
        if self.value_type == ValueType.JSON.value:
            import json

            try:
                return json.loads(raw)
            except (TypeError, ValueError):
                return None
        return raw

    def __repr__(self) -> str:  # noqa: D105
        return f"<SystemSetting {self.setting_key}={self.setting_value}>"


class AuditLog(BaseMixin, Base):
    """审计日志（保留 ≥180 天，不软删除，禁止各 service 直接写表）。"""

    __tablename__ = "audit_log"

    operator: Mapped[str] = mapped_column(String(64), nullable=False, default="system", comment="操作人")
    operator_role: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="admin/operator/system")
    action_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="见 §10.7 埋点约定")
    object_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="审计对象类型")
    object_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="对象 ID")
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True, comment="变更前值（JSON）")
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True, comment="变更后值（JSON）")
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="来源 IP")
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="链路 ID")
    remark: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="备注")

    # ---------- ★ 越权告警处置态（v1.2 新增，仅 action_type='permission_change' 使用）----------
    # 「被拒」记录写入时 is_handled=0（顶栏红点亮）；管理员处置后置 1（红点熄灭）。
    # §10.7 第 6 条：处置动作本身也要埋一条 permission_change 记录，
    #               即「被拒」与「被处置」两条记录**成对存在**。
    is_handled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="越权告警是否已处置（仅 permission_change 使用）"
    )
    handled_by: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="处置人（仅管理员）")
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="处置时间")
    handle_note: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="处置说明")

    __table_args__ = (
        Index("ix_audit_log_action_time", "action_type", "created_at"),
        Index("ix_audit_log_object", "object_type", "object_id"),
        Index("ix_audit_log_operator", "operator", "created_at"),
        # ★ 顶栏红点高频轮询：只索引 permission_change 的部分索引
        Index(
            "idx_audit_violation",
            "action_type",
            "is_handled",
            "created_at",
            sqlite_where=text("action_type = 'permission_change'"),
            postgresql_where=text("action_type = 'permission_change'"),
        ),
    )

    def mark_handled(self, operator: str, note: str = "") -> None:
        """标记越权告警已处置（不提交事务）。"""
        self.is_handled = True
        self.handled_by = operator
        self.handled_at = utc_now()
        self.handle_note = note or self.handle_note

    @classmethod
    def build(
        cls,
        *,
        action_type: str,
        object_type: str,
        object_id: Any = None,
        operator: str = "system",
        operator_role: str = "system",
        old_value: Any = None,
        new_value: Any = None,
        ip: str = "",
        trace_id: str = "",
        remark: str = "",
        is_handled: bool = False,
        handled_by: str | None = None,
        handled_at: Any = None,
        handle_note: str | None = None,
    ) -> "AuditLog":
        """构造审计日志条目（序列化 old/new value 为 JSON 字符串）。"""
        import json

        def _dump(value: Any) -> str | None:
            if value is None:
                return None
            if isinstance(value, str):
                return value
            try:
                return json.dumps(value, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                return str(value)

        return cls(
            action_type=action_type,
            object_type=object_type,
            object_id="" if object_id is None else str(object_id),
            operator=operator or "system",
            operator_role=operator_role,
            old_value=_dump(old_value),
            new_value=_dump(new_value),
            ip=ip,
            trace_id=trace_id,
            remark=remark,
            is_handled=bool(is_handled),
            handled_by=handled_by,
            handled_at=handled_at,
            handle_note=handle_note,
        )

    def __repr__(self) -> str:  # noqa: D105
        return f"<AuditLog {self.action_type}/{self.object_type}>"


class Credential(BaseMixin, Base):
    """统一密钥保险箱（★ AES-256 密文，明文永不落库）。"""

    __tablename__ = "credential"

    owner_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="platform/fulfillment/source")
    owner_key: Mapped[str] = mapped_column(String(64), nullable=False, comment="如 taobao:123456 / miaoshou")
    credential_key: Mapped[str] = mapped_column(String(64), nullable=False, comment="app_key/app_secret/access_token")
    value_enc: Mapped[str] = mapped_column(Text, nullable=False, comment="AES-256 密文")
    value_masked: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="掩码展示 ak****3f2a")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="过期时间")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CredentialStatus.ACTIVE.value, comment="active/expired/revoked"
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="最近连通性验证时间")

    __table_args__ = (
        UniqueConstraint("owner_type", "owner_key", "credential_key", name="uq_credential_owner_key"),
        Index("ix_credential_owner", "owner_type", "owner_key"),
    )

    def __repr__(self) -> str:  # noqa: D105
        return f"<Credential {self.owner_type}:{self.owner_key}.{self.credential_key}>"


__all__ = ["DEFAULT_SETTINGS", "SystemSetting", "AuditLog", "Credential"]
