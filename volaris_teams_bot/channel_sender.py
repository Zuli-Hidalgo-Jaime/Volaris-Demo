from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

import requests
from botbuilder.core import MessageFactory, TurnContext
from botbuilder.schema import Activity, ActivityTypes
from botbuilder.schema._models_py3 import ErrorResponseException
from msrest.exceptions import ClientRequestError

LOGGER = logging.getLogger(__name__)
_DEFAULT_REPLY_ATTEMPTS = 2
_DEFAULT_RETRY_DELAY_SECONDS = 0.6
_RETRYABLE_ERROR_SNIPPETS = (
    "task was canceled",
    "connection aborted",
    "remote end closed connection without response",
    "temporarily unavailable",
    "timeout",
)


def _message_context(turn_context: TurnContext) -> dict[str, str]:
    activity = getattr(turn_context, "activity", None)
    service_url = getattr(activity, "service_url", "") or ""
    return {
        "conversation_id": getattr(getattr(activity, "conversation", None), "id", "") or "",
        "activity_id": getattr(activity, "id", "") or "",
        "channel_id": getattr(activity, "channel_id", "") or "",
        "service_host": urlparse(service_url).netloc or service_url,
    }


def _is_retryable_send_error(exc: Exception) -> bool:
    if isinstance(exc, (ClientRequestError, requests.exceptions.RequestException, TimeoutError)):
        return True
    if isinstance(exc, ErrorResponseException):
        text = str(exc).lower()
        return any(snippet in text for snippet in _RETRYABLE_ERROR_SNIPPETS)
    return False


def _build_send_to_conversation_activity(turn_context: TurnContext, text: str) -> Activity:
    activity = MessageFactory.text(text)
    reference = TurnContext.get_conversation_reference(turn_context.activity)
    activity = TurnContext.apply_conversation_reference(activity, reference)
    activity.reply_to_id = None
    activity.id = None
    activity.type = activity.type or ActivityTypes.message
    activity.input_hint = activity.input_hint or "acceptingInput"
    return activity


async def safe_send_text(
    turn_context: TurnContext,
    text: str,
    *,
    logger: logging.Logger | None = None,
    max_attempts: int = _DEFAULT_REPLY_ATTEMPTS,
    retry_delay_seconds: float = _DEFAULT_RETRY_DELAY_SECONDS,
) -> bool:
    logger = logger or LOGGER
    attempts = max(1, int(max_attempts))
    delay = max(0.0, float(retry_delay_seconds))
    ctx = _message_context(turn_context)
    last_exc: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            await turn_context.send_activity(MessageFactory.text(text))
            return True
        except Exception as exc:  # pragma: no cover - exercised via tests with concrete exception types
            last_exc = exc
            retryable = _is_retryable_send_error(exc)
            logger.warning(
                "Channel reply send failed on attempt %s/%s for conversation=%s activity=%s channel=%s host=%s retryable=%s: %s",
                attempt,
                attempts,
                ctx["conversation_id"],
                ctx["activity_id"],
                ctx["channel_id"],
                ctx["service_host"],
                retryable,
                exc,
                exc_info=True,
            )
            if not retryable or attempt >= attempts:
                break
            await asyncio.sleep(delay * attempt)

    try:
        activity = _build_send_to_conversation_activity(turn_context, text)
        await turn_context.adapter.send_activities(turn_context, [activity])
        turn_context.responded = True
        logger.warning(
            "Recovered channel send via send_to_conversation fallback for conversation=%s activity=%s channel=%s host=%s after %s",
            ctx["conversation_id"],
            ctx["activity_id"],
            ctx["channel_id"],
            ctx["service_host"],
            type(last_exc).__name__ if last_exc else "unknown_error",
        )
        return True
    except Exception as exc:
        logger.error(
            "Channel send fallback failed for conversation=%s activity=%s channel=%s host=%s: %s",
            ctx["conversation_id"],
            ctx["activity_id"],
            ctx["channel_id"],
            ctx["service_host"],
            exc,
            exc_info=True,
        )
        return False
