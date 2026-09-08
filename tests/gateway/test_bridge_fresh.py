from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from gateway.bridge_fresh import FreshRouteError, create_fresh_telegram_route
from gateway.config import Platform
from gateway.session import SessionSource


class FakeDB:
    def __init__(self, binding=None):
        self.binding = binding
        self.bind_calls = []

    def get_telegram_topic_binding(self, *, chat_id, thread_id, profile_name):
        return self.binding

    def bind_telegram_topic(self, **kwargs):
        self.bind_calls.append(dict(kwargs))
        self.binding = dict(kwargs)


class FakeAdapter:
    def __init__(self, thread_id="77"):
        self.thread_id = thread_id
        self.ensure_calls = []

    async def ensure_dm_topic(self, chat_id, topic_name, force_create=False):
        self.ensure_calls.append((chat_id, topic_name, force_create))
        return self.thread_id


class FakeAsyncStore:
    def __init__(self, entry):
        self.entry = entry
        self.calls = []

    async def get_or_create_session(self, source, force_new=False):
        self.calls.append((source, force_new))
        if self.entry is None:
            self.entry = _entry(thread_id=str(source.thread_id))
        return self.entry


class FakeRunner:
    _primary_profile_name = "default"

    def __init__(self, *, adapter=None, binding=None, entry=None):
        self.config = SimpleNamespace(
            get_home_channel=lambda platform: SimpleNamespace(
                chat_id="5611439557", user_id="5611439557", name="Fwh"
            ) if platform == Platform.TELEGRAM else None
        )
        self.adapter = adapter
        self.db = FakeDB(binding=binding)
        self.entry = entry
        self.session_store = SimpleNamespace(
            lookup_by_session_key=lambda key: self.entry if self.entry and self.entry.session_key == key else None,
            lookup_by_session_id=lambda sid: self.entry if self.entry and self.entry.session_id == sid else None,
        )
        self._async_session_store = FakeAsyncStore(entry)

    def _adapter_for_source(self, source):
        return self.adapter

    def _session_key_for_source(self, source):
        return f"agent:main:telegram:dm:{source.chat_id}:{source.thread_id}"

    def _sync_session_db(self):
        return self.db


def _entry(thread_id="77"):
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="5611439557",
        chat_name="Fwh",
        chat_type="dm",
        user_id="5611439557",
        thread_id=thread_id,
        profile="default",
    )
    return SimpleNamespace(
        session_id="20260909_000000_abcdef12",
        session_key=f"agent:main:telegram:dm:5611439557:{thread_id}",
        origin=source,
    )


def test_success_creates_and_reads_back_coherent_route():
    runner = FakeRunner(adapter=FakeAdapter(), entry=None)

    route = asyncio.run(create_fresh_telegram_route(runner, "Bridge fresh test"))

    assert route == {
        "session_id": "20260909_000000_abcdef12",
        "session_key": "agent:main:telegram:dm:5611439557:77",
        "thread_id": "77",
        "created_new": True,
    }
    assert len(runner.db.bind_calls) == 1
    assert runner._async_session_store.calls[0][1] is False


def test_duplicate_binding_reuses_route_without_creating_session():
    entry = _entry()
    binding = {
        "session_id": entry.session_id,
        "session_key": entry.session_key,
    }
    runner = FakeRunner(adapter=FakeAdapter(), binding=binding, entry=entry)

    route = asyncio.run(create_fresh_telegram_route(runner, "Bridge fresh test"))

    assert route["created_new"] is False
    assert route["session_id"] == entry.session_id
    assert runner._async_session_store.calls == []
    assert runner.db.bind_calls == []


def test_partial_existing_binding_fails_closed():
    entry = _entry()
    runner = FakeRunner(
        adapter=FakeAdapter(),
        binding={"session_id": entry.session_id, "session_key": "wrong-key"},
        entry=entry,
    )

    with pytest.raises(FreshRouteError, match="partial_existing_route"):
        asyncio.run(create_fresh_telegram_route(runner, "Bridge fresh test"))


def test_missing_topic_primitive_fails_closed():
    runner = FakeRunner(adapter=None, entry=_entry())

    with pytest.raises(FreshRouteError, match="telegram_topic_primitive_unavailable"):
        asyncio.run(create_fresh_telegram_route(runner, "Bridge fresh test"))
