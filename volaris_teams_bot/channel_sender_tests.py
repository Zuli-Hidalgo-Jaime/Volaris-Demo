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
    BOT_CONNECTOR_CLIENT_KEY = "connector_client"

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
    def __init__(self, send_side_effects: list[object], adapter: FakeAdapter | None = None, channel_id: str = "msteams"):
        self.activity = Activity(
            type="message",
            id="activity-1",
            channel_id=channel_id,
            service_url="https://smba.trafficmanager.net/amer/",
            conversation=ConversationAccount(id="conversation-1"),
            from_property=ChannelAccount(id="user-1"),
            recipient=ChannelAccount(id="bot-1"),
        )
        self.adapter = adapter or FakeAdapter()
        self.responded = False
        self.send_side_effects = list(send_side_effects)
        self.send_calls = 0
        self.turn_state = {}

    async def send_activity(self, activity):
        self.send_calls += 1
        effect = self.send_side_effects.pop(0) if self.send_side_effects else ResourceResponse(id="primary-ok")
        if isinstance(effect, Exception):
            raise effect
        self.responded = True
        return effect


class FakeConversations:
    def __init__(self, reply_side_effects: list[object] | None = None, send_side_effects: list[object] | None = None):
        self.reply_side_effects = list(reply_side_effects or [])
        self.send_side_effects = list(send_side_effects or [])
        self.reply_calls: list[tuple[str, str, Activity, dict]] = []
        self.send_calls: list[tuple[str, Activity, dict]] = []

    async def reply_to_activity(self, conversation_id, activity_id, activity, **kwargs):
        self.reply_calls.append((conversation_id, activity_id, activity, kwargs))
        effect = self.reply_side_effects.pop(0) if self.reply_side_effects else ResourceResponse(id="reply-ok")
        if isinstance(effect, Exception):
            raise effect
        return effect

    async def send_to_conversation(self, conversation_id, activity, **kwargs):
        self.send_calls.append((conversation_id, activity, kwargs))
        effect = self.send_side_effects.pop(0) if self.send_side_effects else ResourceResponse(id="send-ok")
        if isinstance(effect, Exception):
            raise effect
        return effect


class FakeConnectorClient:
    def __init__(self, conversations: FakeConversations):
        self.conversations = conversations


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

    async def test_teams_fast_path_uses_short_timeout(self) -> None:
        conversations = FakeConversations()
        turn_context = FakeTurnContext([], channel_id="msteams")
        turn_context.turn_state[turn_context.adapter.BOT_CONNECTOR_CLIENT_KEY] = FakeConnectorClient(conversations)

        sent = await safe_send_text(
            turn_context,
            "hola",
            logger=logging.getLogger("channel_sender_tests"),
            channel_timeout_seconds=7,
        )

        self.assertTrue(sent)
        self.assertEqual(turn_context.send_calls, 0)
        self.assertEqual(len(conversations.reply_calls), 1)
        self.assertEqual(conversations.reply_calls[0][3]["timeout"], 7)

    async def test_teams_fast_path_falls_back_quickly(self) -> None:
        conversations = FakeConversations(
            reply_side_effects=[requests.exceptions.ReadTimeout("timed out")],
        )
        turn_context = FakeTurnContext([], channel_id="msteams")
        turn_context.turn_state[turn_context.adapter.BOT_CONNECTOR_CLIENT_KEY] = FakeConnectorClient(conversations)

        sent = await safe_send_text(
            turn_context,
            "hola",
            logger=logging.getLogger("channel_sender_tests"),
            max_attempts=1,
            fallback_attempts=1,
            retry_delay_seconds=0,
            channel_timeout_seconds=5,
        )

        self.assertTrue(sent)
        self.assertEqual(turn_context.send_calls, 0)
        self.assertEqual(len(conversations.reply_calls), 1)
        self.assertEqual(len(conversations.send_calls), 1)
        self.assertEqual(conversations.send_calls[0][2]["timeout"], 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
