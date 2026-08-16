"""The LLM call itself: LiteLLM -> OpenRouter -> Cerebras, plus the mock that stands in
for it (PLAN.md §9).

Two things here are deliberate and easy to get wrong:

* **`completion` runs in a worker thread.** LiteLLM's `completion` is blocking, and this
  process runs the SSE generators, the market-data loop and every request handler on one
  event loop (PLAN.md §3). Calling it inline would freeze every connected client's price
  stream for the whole inference. This is the same rule `db.run` follows, for the same
  reason.
* **Import is lazy.** `import litellm` costs seconds and pulls in a large dependency tree.
  Mock mode must never pay for it, and neither should test collection.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

from .models import AssistantReply, TradeIntent, WatchlistIntent

log = logging.getLogger(__name__)

MODEL = "openrouter/openai/gpt-oss-120b"
EXTRA_BODY = {"provider": {"order": ["cerebras"]}}
REASONING_EFFORT = "low"
REQUEST_TIMEOUT_S = 60.0


class LLMError(RuntimeError):
    """The model could not be reached, or answered with something unusable."""


def mock_enabled() -> bool:
    return os.environ.get("LLM_MOCK", "").strip().lower() == "true"


def verify_config() -> None:
    """Fail fast at startup when chat cannot possibly work (PLAN.md §5, §13 item 7).

    Chat is a core feature, so a silently half-working app is worse than a clear message at
    boot. Mock mode is the documented escape hatch for running without a key.
    """
    if mock_enabled():
        log.info("LLM_MOCK=true -> deterministic mock replies, no OpenRouter calls")
        return
    if not os.environ.get("OPENROUTER_API_KEY", "").strip():
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set and LLM_MOCK is not 'true'. Chat is a core "
            "feature of FinAlly: set OPENROUTER_API_KEY in .env, or set LLM_MOCK=true to "
            "run with deterministic mock replies."
        )
    log.info("chat: %s via OpenRouter (provider order: cerebras)", MODEL)


async def complete(messages: list[dict]) -> AssistantReply:
    """One structured-output round trip. Raises `LLMError` on anything unusable."""
    if mock_enabled():
        return mock_reply(messages)

    try:
        raw = await asyncio.wait_for(
            asyncio.to_thread(_blocking_completion, messages),
            timeout=REQUEST_TIMEOUT_S + 15.0,
        )
    except asyncio.TimeoutError as exc:
        raise LLMError("the model did not respond in time") from exc
    except Exception as exc:                      # noqa: BLE001 — provider/network/SDK
        raise LLMError(f"the model could not be reached: {exc}") from exc

    if not raw or not raw.strip():
        raise LLMError("the model returned an empty response")

    try:
        return AssistantReply.model_validate_json(raw)
    except Exception as exc:                      # noqa: BLE001 — pydantic ValidationError
        # Structured outputs make this rare, not impossible. Surfacing it as a chat error
        # beats persisting a half-parsed reply or 500-ing the endpoint (PLAN.md §12).
        log.warning("unparseable model reply: %s", raw[:400])
        raise LLMError(f"the model returned malformed JSON: {exc}") from exc


def _blocking_completion(messages: list[dict]) -> str:
    from litellm import completion              # lazy: see module docstring

    response = completion(
        model=MODEL,
        messages=messages,
        response_format=AssistantReply,
        reasoning_effort=REASONING_EFFORT,
        extra_body=EXTRA_BODY,
        timeout=REQUEST_TIMEOUT_S,
    )
    return response.choices[0].message.content


# ---- mock mode ---------------------------------------------------------------

# "buy 10 MU", "sell 2.5 amd", "buy MU 10" — enough to drive the E2E trade scenario
# deterministically without an API key (PLAN.md §9, §12).
_TRADE_RE = re.compile(
    r"\b(?P<side>buy|sell)\s+(?:(?P<qty1>\d+(?:\.\d+)?)\s+(?P<sym1>[A-Za-z][A-Za-z.\-]{0,11})"
    r"|(?P<sym2>[A-Za-z][A-Za-z.\-]{0,11})\s+(?P<qty2>\d+(?:\.\d+)?))\b",
    re.IGNORECASE,
)
_WATCH_RE = re.compile(
    r"\b(?P<action>watch|unwatch)\s+(?P<sym>[A-Za-z][A-Za-z.\-]{0,11})\b", re.IGNORECASE
)


def mock_reply(messages: list[dict]) -> AssistantReply:
    """Deterministic replies for E2E, CI and keyless development (PLAN.md §9).

    Same input always yields the same output — no clock, no randomness — because the E2E
    suite asserts on the text.
    """
    user_message = next(
        (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
    )

    trades = [
        TradeIntent(
            ticker=(m.group("sym1") or m.group("sym2")).upper(),
            side=m.group("side").lower(),
            quantity=float(m.group("qty1") or m.group("qty2")),
        )
        for m in _TRADE_RE.finditer(user_message)
    ][: 3]

    watchlist_changes = [
        WatchlistIntent(
            ticker=m.group("sym").upper(),
            action="add" if m.group("action").lower() == "watch" else "remove",
        )
        for m in _WATCH_RE.finditer(user_message)
    ][: 3]

    if trades:
        described = ", ".join(f"{t.side} {t.quantity:g} {t.ticker}" for t in trades)
        message = f"[mock] Executing {described} at the current market price."
    elif watchlist_changes:
        described = ", ".join(
            f"{c.action} {c.ticker}" for c in watchlist_changes
        )
        message = f"[mock] Updating the watchlist: {described}."
    else:
        message = (
            "[mock] FinAlly mock mode is active, so this reply is canned rather than "
            "generated. Your portfolio context was received. Ask me to 'buy 5 MU' or "
            "'watch PYPL' to exercise the action path."
        )

    return AssistantReply(
        message=message, trades=trades, watchlist_changes=watchlist_changes
    )
