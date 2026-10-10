"""AI 重构客户端包（mock / file_bridge / http）。"""

from app.adapters.ai.base import (
    AiAttributeResult,
    AiClient,
    AiImagePrompt,
    AiImageResult,
    AiInputPrompt,
    AiReworkResult,
    AiTaskContext,
    AiTimeoutError,
    AiTitleCandidate,
    AiTitleResult,
    MIN_TITLE_CANDIDATES,
)
from app.adapters.ai.factory import (
    AI_CLIENT_REGISTRY,
    DEFAULT_AI_CLIENT,
    AiClientFactory,
    create_ai_client,
    register_ai_client,
    resolve_ai_client_name,
)
from app.adapters.ai.file_bridge import WorkBuddyFileBridgeClient
from app.adapters.ai.http_client import HttpAiClient
from app.adapters.ai.mock_client import MockAiClient

__all__ = [
    "AI_CLIENT_REGISTRY",
    "AiAttributeResult",
    "AiClient",
    "AiClientFactory",
    "AiImagePrompt",
    "AiImageResult",
    "AiInputPrompt",
    "AiReworkResult",
    "AiTaskContext",
    "AiTimeoutError",
    "AiTitleCandidate",
    "AiTitleResult",
    "DEFAULT_AI_CLIENT",
    "MIN_TITLE_CANDIDATES",
    "HttpAiClient",
    "MockAiClient",
    "WorkBuddyFileBridgeClient",
    "create_ai_client",
    "register_ai_client",
    "resolve_ai_client_name",
]
