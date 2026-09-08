"""Fresh Telegram-visible worker route creation for the local bridge control socket.

This module deliberately uses the Gateway's existing session store, Telegram topic
adapter, and topic-binding state API. It does not create a second gateway, CLI
worker, supervisor, or alternate route.
"""

from __future__ import annotations

import asyncio
from typing import Any

from gateway.config import Platform
from gateway.session import SessionSource


class FreshRouteError(RuntimeError):
    """A fresh route could not be created or read back coherently."""


async def create_fresh_telegram_route(runner: Any, topic_name: str) -> dict[str, str | bool]:
    """Create or idempotently recover one Telegram-topic-backed Gateway route.

    The topic name is the caller's idempotency key. A persisted binding is reused
    only when its session id/key and active routing entry agree exactly. A partial
    binding never gets silently repaired by choosing another session.
    """
    name = str(topic_name or "").strip()
    if not name or len(name) > 128:
        raise FreshRouteError("invalid_topic_name")

    home = runner.config.get_home_channel(Platform.TELEGRAM)
    if home is None or not str(getattr(home, "chat_id", "") or "").strip():
        raise FreshRouteError("telegram_home_channel_unavailable")

    chat_id = str(home.chat_id)
    user_id = str(getattr(home, "user_id", None) or chat_id)
    profile_name = str(getattr(runner, "_primary_profile_name", None) or "default")

    base_source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_name=getattr(home, "name", None),
        chat_type="dm",
        user_id=user_id,
        profile=profile_name,
    )
    adapter = runner._adapter_for_source(base_source)
    ensure_topic = getattr(adapter, "ensure_dm_topic", None) if adapter is not None else None
    if not callable(ensure_topic):
        raise FreshRouteError("telegram_topic_primitive_unavailable")

    thread_id = await ensure_topic(chat_id, name, force_create=False)
    if not isinstance(thread_id, str) or not thread_id:
        raise FreshRouteError("telegram_topic_creation_failed")

    source = SessionSource(
        platform=base_source.platform,
        chat_id=base_source.chat_id,
        chat_name=base_source.chat_name,
        chat_type=base_source.chat_type,
        user_id=base_source.user_id,
        thread_id=thread_id,
        profile=base_source.profile,
    )
    expected_key = runner._session_key_for_source(source)
    db = runner._sync_session_db()
    if db is None:
        raise FreshRouteError("telegram_binding_store_unavailable")

    async def read_binding() -> dict[str, Any] | None:
        return await asyncio.to_thread(
            db.get_telegram_topic_binding,
            chat_id=chat_id,
            thread_id=thread_id,
            profile_name=profile_name,
        )

    binding = await read_binding()
    if binding is not None:
        bound_id = str(binding.get("session_id") or "")
        bound_key = str(binding.get("session_key") or "")
        if not bound_id or not bound_key or bound_key != expected_key:
            raise FreshRouteError("partial_existing_route")
        entry = await asyncio.to_thread(runner.session_store.lookup_by_session_id, bound_id)
        if entry is None or entry.session_id != bound_id or entry.session_key != bound_key:
            raise FreshRouteError("partial_existing_route")
        return {
            "session_id": bound_id,
            "session_key": bound_key,
            "thread_id": thread_id,
            "created_new": False,
        }

    # A previous attempt may have created the routing entry before crashing before
    # the binding write. Reuse it rather than force-creating a second session.
    entry = await asyncio.to_thread(runner.session_store.lookup_by_session_key, expected_key)
    created_new = entry is None
    if entry is None:
        entry = await runner._async_session_store.get_or_create_session(source, force_new=False)
    if entry is None or entry.session_key != expected_key or entry.origin is None:
        raise FreshRouteError("incoherent_session_route")
    origin = entry.origin
    if (
        origin.platform != source.platform
        or str(origin.chat_id) != chat_id
        or str(origin.thread_id or "") != thread_id
    ):
        raise FreshRouteError("session_origin_mismatch")

    await asyncio.to_thread(
        db.bind_telegram_topic,
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=user_id,
        session_key=entry.session_key,
        session_id=entry.session_id,
        managed_mode="bridge",
        profile_name=profile_name,
    )
    readback = await read_binding()
    if not isinstance(readback, dict):
        raise FreshRouteError("binding_readback_missing")
    if (
        str(readback.get("session_id") or "") != entry.session_id
        or str(readback.get("session_key") or "") != entry.session_key
    ):
        raise FreshRouteError("binding_readback_mismatch")

    return {
        "session_id": entry.session_id,
        "session_key": entry.session_key,
        "thread_id": thread_id,
        "created_new": created_new,
    }
