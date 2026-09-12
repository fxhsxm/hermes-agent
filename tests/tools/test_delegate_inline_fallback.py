"""delegate_task's inline fallback must restore the parent's interrupt fan-out list.

A background delegation detaches its children from ``parent_agent._active_children`` up front,
because the async registry owns their lifecycle from then on. When the async unit is NOT
accepted — the pool is at capacity, or the durable registration failed — the batch runs INLINE
on the calling thread instead, and ``_run_children_parallel`` relies on that same list twice:
to fan a parent interrupt out to the children, and to be honest about it, since it reports every
still-pending child as ``interrupted``.

With the children left detached, a parent interrupt produced a result entry saying
"Parent agent interrupted — child did not finish in time" while those children kept running
tools, writing files and burning tokens; the model then re-dispatched the work and duplicated
its side effects. The fallback note also blamed the pool for failures that had nothing to do
with capacity.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

import tools.async_delegation as ad
import tools.delegate_tool_dispatch as dispatch_mod


class _Child:
    """Minimal stand-in for a built child agent (identity matters, behaviour does not)."""

    def __init__(self, subagent_id: str) -> None:
        self._subagent_id = subagent_id
        self.session_id = f"sess-{subagent_id}"
        self._delegate_role = "leaf"
        self._interrupt_requested = False
        self.closed = False

    def interrupt(self, *args, **kwargs):
        self._interrupt_requested = True

    def hard_interrupt(self, *args, **kwargs):
        self._interrupt_requested = True

    def close(self):
        self.closed = True


def _parent(children):
    return SimpleNamespace(
        session_id="parent-inline-fallback",
        _current_task_id=None,
        _active_children=list(children),
        _active_children_lock=threading.Lock(),
        _memory_manager=None,
    )


def _batch(parent, children):
    tasks = [{"goal": f"goal {i}"} for i in range(len(children))]
    return dispatch_mod._Batch(
        task_list=tasks,
        children=[(i, task, child) for i, (task, child) in enumerate(zip(tasks, children))],
        parent_agent=parent,
        creds={"model": "test-model"},
        context=None,
        top_role="leaf",
        max_children=len(children),
        live_deleg_id=None,
        live_writers=[None] * len(children),
        live_paths=[],
        origin_wake_sid="wake-sid",
        origin_ui_session_id="",
        origin_owner_transport=None,
        origin_owner_session_record=None,
        overall_start=time.monotonic(),
    )


def _patch_inline_child_run(monkeypatch):
    """Run the batch's children inline without a real AIAgent."""

    def fake_run_child(self, i, task, child):
        return {
            "task_index": i, "status": "completed", "summary": f"inline {i}", "error": None,
            "api_calls": 1, "duration_seconds": 0.0,
        }

    monkeypatch.setattr(dispatch_mod._Batch, "run_child", fake_run_child)


def _reject(monkeypatch, payload):
    monkeypatch.setattr(ad, "dispatch_async_delegation_batch", lambda **kwargs: dict(payload))


def test_capacity_rejection_reattaches_children_for_interrupt_fan_out(monkeypatch):
    children = [_Child("sa-1"), _Child("sa-2")]
    parent = _parent(children)
    batch = _batch(parent, children)
    _patch_inline_child_run(monkeypatch)
    _reject(monkeypatch, {"status": "rejected", "reason": "at_capacity",
                          "error": "Async delegation capacity reached (2 running)."})

    payload = json.loads(dispatch_mod._dispatch_background(batch))

    assert [r["status"] for r in payload["results"]] == ["completed", "completed"], (
        "a rejected background dispatch still runs the batch inline"
    )
    assert list(parent._active_children) == children, (
        "the inline fallback must put the children back on the parent's interrupt list"
    )
    assert "capacity" in payload["note"]


def test_registration_failure_fallback_reports_the_real_reason(monkeypatch):
    """A state.db failure is not a capacity problem — say so, and keep the fan-out list."""
    children = [_Child("sa-3")]
    parent = _parent(children)
    batch = _batch(parent, children)
    _patch_inline_child_run(monkeypatch)
    _reject(monkeypatch, {"status": "rejected", "reason": "not_scheduled",
                          "error": "Failed to persist async delegation: database is locked"})

    payload = json.loads(dispatch_mod._dispatch_background(batch))

    assert payload["results"][0]["status"] == "completed"
    assert list(parent._active_children) == children
    assert "database is locked" in payload["note"]
    assert "capacity" not in payload["note"]
