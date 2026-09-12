"""Bridge v0.4.1 Telegram observability (Issue #2).

Two contracts are pinned here:

1. A bridge-injected turn must still be able to reach its bound Telegram topic.
   The control socket injects a synthetic event (previously
   ``message_id="bridge:<ns>"``); if that value is treated as a Telegram reply
   anchor/thread id, every send of the turn fails with ``int()`` and the bridge
   topic stays title-only while GitHub shows progress.
2. A freshly bound bridge Main topic must be able to receive a deterministic
   (non-LLM) ``ROLE: MAIN`` identity banner, so the topic is never an empty
   surface even when the Main turn produces no output.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from gateway.bridge_fresh import FreshRouteError, post_telegram_topic_message
from gateway.config import Platform
from gateway.platforms.base import (
    MessageEvent,
    MessageType,
    _is_platform_message_id,
    _reply_anchor_for_event,
)
from gateway.session import SessionSource

BRIDGE_EVENT_ID = "bridge:1789154224411789600"


def _dm_topic_source(thread_id: str = "25356") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="5611439557",
        chat_name="Fwh",
        chat_type="dm",
        user_id="5611439557",
        thread_id=thread_id,
        profile="default",
    )


def _event(source: SessionSource, message_id) -> MessageEvent:
    return MessageEvent(
        text="bridge work", message_type=MessageType.TEXT, source=source,
        message_id=message_id, internal=True,
    )


class TestPlatformMessageIdValidation:
    def test_synthetic_bridge_id_is_not_a_platform_id(self):
        assert _is_platform_message_id(BRIDGE_EVENT_ID) is False

    def test_real_ids_are_platform_ids(self):
        assert _is_platform_message_id("24048") is True
        assert _is_platform_message_id(24048) is True

    def test_empty_and_non_positive_ids_are_rejected(self):
        assert _is_platform_message_id(None) is False
        assert _is_platform_message_id("") is False
        assert _is_platform_message_id("0") is False
        assert _is_platform_message_id(0) is False
        assert _is_platform_message_id(True) is False
        assert _is_platform_message_id(-5) is False


class TestReplyAnchorForBridgeInjection:
    def test_bridge_injected_turn_has_no_reply_anchor(self):
        """Regression: this is the exact failure that made the topic title-only."""
        event = _event(_dm_topic_source(), BRIDGE_EVENT_ID)
        assert _reply_anchor_for_event(event) is None

    def test_injected_turn_without_message_id_has_no_reply_anchor(self):
        event = _event(_dm_topic_source(), None)
        assert _reply_anchor_for_event(event) is None

    def test_real_user_message_still_anchors_in_dm_topic(self):
        event = _event(_dm_topic_source(), "24048")
        assert _reply_anchor_for_event(event) == "24048"

    def test_group_topic_never_anchors(self):
        source = _dm_topic_source()
        source.chat_type = "group"
        assert _reply_anchor_for_event(_event(source, "24048")) is None


class TestTelegramAdapterAnchorTolerance:
    """The platform boundary must never fail a whole send over a synthetic id."""

    @staticmethod
    def _adapter(reply_to_mode: str = "first"):
        from plugins.platforms.telegram.adapter import TelegramAdapter

        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter._reply_to_mode = reply_to_mode
        return adapter

    def test_metadata_reply_anchor_degrades_to_none(self):
        from plugins.platforms.telegram.adapter import TelegramAdapter

        assert TelegramAdapter._metadata_reply_to_message_id(
            {"telegram_reply_to_message_id": BRIDGE_EVENT_ID}) is None
        assert TelegramAdapter._metadata_reply_to_message_id(
            {"telegram_reply_to_message_id": "24048"}) == 24048
        assert TelegramAdapter._metadata_reply_to_message_id(
            {"telegram_reply_to_message_id": 24048}) == 24048

    def test_reply_to_message_id_for_send_degrades_to_none(self):
        from plugins.platforms.telegram.adapter import TelegramAdapter

        assert TelegramAdapter._reply_to_message_id_for_send(BRIDGE_EVENT_ID) is None
        assert TelegramAdapter._reply_to_message_id_for_send("24048") == 24048

    def test_thread_ids_must_be_numeric(self):
        from plugins.platforms.telegram.adapter import TelegramAdapter

        assert TelegramAdapter._message_thread_id_for_send("25356") == 25356
        assert TelegramAdapter._message_thread_id_for_send(BRIDGE_EVENT_ID) is None
        assert TelegramAdapter._message_thread_id_for_send("") is None
        assert TelegramAdapter._message_thread_id_for_send("1") is None
        assert TelegramAdapter._message_thread_id_for_typing("25356") == 25356
        assert TelegramAdapter._message_thread_id_for_typing(BRIDGE_EVENT_ID) is None

    def test_bridge_anchor_still_lands_in_the_topic_lane(self):
        """A synthetic anchor (legacy gateway) must not raise and must still route to the topic."""
        adapter = self._adapter()
        metadata = {
            "thread_id": "25356",
            "telegram_dm_topic_reply_fallback": True,
            "direct_messages_topic_id": "25356",
            "telegram_reply_to_message_id": BRIDGE_EVENT_ID,
        }
        _, anchor_off, reply_to_id = adapter._chunk_reply_routing(
            "5611439557", BRIDGE_EVENT_ID, metadata, "25356", 0)
        assert anchor_off is False
        assert reply_to_id is None
        kwargs = adapter._thread_kwargs_for_send(
            "5611439557", "25356", metadata, reply_to_message_id=reply_to_id,
            reply_to_mode="first")
        assert kwargs.get("message_thread_id") == 25356

    def test_fixed_gateway_metadata_routes_without_any_anchor(self):
        """The fixed gateway sends no synthetic anchor at all — the lane still resolves."""
        adapter = self._adapter()
        metadata = {
            "thread_id": "25356",
            "telegram_dm_topic_reply_fallback": True,
            "direct_messages_topic_id": "25356",
        }
        _, _, reply_to_id = adapter._chunk_reply_routing(
            "5611439557", None, metadata, "25356", 0)
        assert reply_to_id is None
        kwargs = adapter._thread_kwargs_for_send(
            "5611439557", "25356", metadata, reply_to_message_id=reply_to_id,
            reply_to_mode="first")
        assert kwargs.get("message_thread_id") == 25356

    def test_no_dm_topic_fallback_still_threads_a_real_anchor(self):
        adapter = self._adapter()
        kwargs = adapter._thread_kwargs_for_send(
            "5611439557", "25356", {}, reply_to_message_id=24048, reply_to_mode="first")
        assert kwargs == {"message_thread_id": 25356}


class _FakeSendResult:
    def __init__(self, success: bool, message_id=None, error=None):
        self.success = success
        self.message_id = message_id
        self.error = error


class _FakeAdapter:
    def __init__(self, result=None, error: Exception | None = None):
        self.calls = []
        self.result = result or _FakeSendResult(True, message_id="24100")
        self.error = error

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.calls.append(
            {"chat_id": chat_id, "content": content, "reply_to": reply_to, "metadata": metadata})
        if self.error is not None:
            raise self.error
        return self.result


class _FakeRunner:
    _primary_profile_name = "default"

    def __init__(self, adapter):
        self._adapter = adapter
        self.config = SimpleNamespace(
            get_home_channel=lambda platform: SimpleNamespace(
                chat_id="5611439557", user_id="5611439557", name="Fwh"),
        )

    def _adapter_for_source(self, source):
        return self._adapter

    def _thread_metadata_for_target(self, platform, chat_id, thread_id, **kwargs):
        return {
            "thread_id": str(thread_id),
            "telegram_dm_topic_reply_fallback": True,
            "direct_messages_topic_id": str(thread_id),
            "hermes_profile": "default",
        }


class TestTopicBannerSend:
    def test_banner_goes_to_the_exact_topic_lane_without_an_anchor(self):
        adapter = _FakeAdapter()
        runner = _FakeRunner(adapter)

        result = asyncio.run(post_telegram_topic_message(
            runner, text="ROLE: MAIN | STARTED", thread_id="25356"))

        assert result["message_id"] == "24100"
        assert result["thread_id"] == "25356"
        call = adapter.calls[0]
        assert call["chat_id"] == "5611439557"
        assert call["reply_to"] is None
        assert call["metadata"]["telegram_dm_topic_reply_fallback"] is True
        assert call["metadata"]["direct_messages_topic_id"] == "25356"

    def test_failed_send_raises_instead_of_being_silent(self):
        runner = _FakeRunner(_FakeAdapter(result=_FakeSendResult(False, error="bad request")))
        with pytest.raises(FreshRouteError, match="topic_message_send_failed"):
            asyncio.run(post_telegram_topic_message(
                runner, text="ROLE: MAIN | STARTED", thread_id="25356"))

    def test_adapter_exception_is_visible(self):
        runner = _FakeRunner(_FakeAdapter(error=RuntimeError("boom")))
        with pytest.raises(RuntimeError, match="boom"):
            asyncio.run(post_telegram_topic_message(
                runner, text="ROLE: MAIN | STARTED", thread_id="25356"))

    def test_invalid_inputs_fail_closed(self):
        runner = _FakeRunner(_FakeAdapter())
        with pytest.raises(FreshRouteError, match="empty_topic_message"):
            asyncio.run(post_telegram_topic_message(runner, text="   ", thread_id="25356"))
        with pytest.raises(FreshRouteError, match="invalid_thread_id"):
            asyncio.run(post_telegram_topic_message(
                runner, text="banner", thread_id=BRIDGE_EVENT_ID))

    def test_missing_adapter_fails_closed(self):
        runner = _FakeRunner(None)
        with pytest.raises(FreshRouteError, match="telegram_adapter_unavailable"):
            asyncio.run(post_telegram_topic_message(
                runner, text="banner", thread_id="25356"))
