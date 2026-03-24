from __future__ import annotations

import logging
import unittest

import requests
from botbuilder.schema import Activity, ChannelAccount, ConversationAccount, ResourceResponse
from botbuilder.schema._models_py3 import ErrorResponseException

from channel_sender import safe_send_text


class FakeErrorResponse:
    reason = ""

    def raise_for_status(self):
        raise RuntimeError("channel send failed")


def make_empty_status_error() -> ErrorResponseException:
    return ErrorResponseException(lambda *args, **kwargs: None, FakeErrorResponse())


class FakeAdapter:
    def __init__(self, side_effects: list[object] | None = None):
        self.side_effects = list(side_effects or [])
        self.calls: list[list[Activity]] = []

    async def send_activities(self, turn_context, activities):
        self.calls.append(activities)
        if self.side_effects:
            effect = self.side_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return effect
        return [ResourceResponse(id="fallback-ok")]


class FakeTurnContext:
    def __init__(self, send_side_effects: list[object], adapter: FakeAdapter | None = None):
        self.activity = Activity(
            type="message",
            id="activity-1",
            channel_id="msteams",
            service_url="https://smba.trafficmanager.net/amer/",
            conversation=ConversationAccount(id="conversation-1"),
            from_property=ChannelAccount(id="user-1"),
            recipient=ChannelAccount(id="bot-1"),
        )
        self.adapter = adapter or FakeAdapter()
        self.responded = False
        self.send_side_effects = list(send_side_effects)
        self.send_calls = 0

    async def send_activity(self, activity):
        self.send_calls += 1
        effect = self.send_side_effects.pop(0) if self.send_side_effects else ResourceResponse(id="primary-ok")
        if isinstance(effect, Exception):
            raise effect
        self.responded = True
        return effect


class SafeSendTextTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_primary_send_and_succeeds(self) -> None:
        turn_context = FakeTurnContext(
            [
                requests.exceptions.ConnectionError("Connection aborted."),
                ResourceResponse(id="ok"),
            ]
        )

        sent = await safe_send_text(
            turn_context,
            "hola",
            logger=logging.getLogger("channel_sender_tests"),
            max_attempts=2,
            retry_delay_seconds=0,
        )

        self.assertTrue(sent)
        self.assertEqual(turn_context.send_calls, 2)
        self.assertEqual(len(turn_context.adapter.calls), 0)

    async def test_falls_back_to_send_to_conversation(self) -> None:
        adapter = FakeAdapter()
        turn_context = FakeTurnContext(
            [
                requests.exceptions.ConnectionError("Connection aborted."),
                requests.exceptions.ConnectionError("Connection aborted."),
            ],
            adapter=adapter,
        )

        sent = await safe_send_text(
            turn_context,
            "hola",
            logger=logging.getLogger("channel_sender_tests"),
            max_attempts=2,
            retry_delay_seconds=0,
        )

        self.assertTrue(sent)
        self.assertEqual(turn_context.send_calls, 2)
        self.assertEqual(len(adapter.calls), 1)
        self.assertIsNone(adapter.calls[0][0].reply_to_id)
        self.assertEqual(adapter.calls[0][0].conversation.id, "conversation-1")

    async def test_returns_false_when_primary_and_fallback_fail(self) -> None:
        adapter = FakeAdapter(
            side_effects=[
                requests.exceptions.ConnectionError("Fallback failed."),
                requests.exceptions.ConnectionError("Fallback failed again."),
            ]
        )
        turn_context = FakeTurnContext(
            [requests.exceptions.ConnectionError("Connection aborted.")],
            adapter=adapter,
        )

        sent = await safe_send_text(
            turn_context,
            "hola",
            logger=logging.getLogger("channel_sender_tests"),
            max_attempts=1,
            retry_delay_seconds=0,
        )

        self.assertFalse(sent)
        self.assertEqual(turn_context.send_calls, 1)
        self.assertEqual(len(adapter.calls), 2)

    async def test_retries_empty_status_error_from_connector(self) -> None:
        turn_context = FakeTurnContext(
            [
                make_empty_status_error(),
                ResourceResponse(id="ok"),
            ]
        )

        sent = await safe_send_text(
            turn_context,
            "hola",
            logger=logging.getLogger("channel_sender_tests"),
            max_attempts=2,
            retry_delay_seconds=0,
        )

        self.assertTrue(sent)
        self.assertEqual(turn_context.send_calls, 2)
        self.assertEqual(len(turn_context.adapter.calls), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
