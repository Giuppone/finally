"""LLM chat (PLAN.md §9). The only import surface — nothing outside imports a submodule.

    chat.verify_config()          # fail fast at startup if chat cannot work (§5)
    await chat.respond(text, market_service)
    await chat.history()
    chat.router                   # /api/chat, /api/chat/history
"""

from __future__ import annotations

from .llm import LLMError, mock_enabled, verify_config
from .models import ActionRecord, AssistantReply, TradeIntent, WatchlistIntent
from .routes import router
from .service import history, respond

__all__ = [
    "ActionRecord",
    "AssistantReply",
    "LLMError",
    "TradeIntent",
    "WatchlistIntent",
    "history",
    "mock_enabled",
    "respond",
    "router",
    "verify_config",
]
