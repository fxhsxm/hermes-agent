"""Regression coverage for timed-out delegation teardown."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from tools import delegate_tool

# The child's conversation thread is RELEASED by the test, never by a stopwatch. The parent's
# post-timeout teardown (steer close -> interrupt -> activity summary -> completion callback ->
# `close_deferred = is_timeout and not future.done()` -> cleanup) runs arbitrary wall time on a
# loaded runner — the canonical runner reported this file ⚠ FLAKY because a 1s/2s budget inside
# the fake child expired first, the worker exited, and cleanup then closed an already-exited
# worker (correct production behaviour) while the assertion below read it as "close() ran before
# the unwind". Only a budget this generous (plus an explicit diagnostic when it does expire)
# keeps the assertion measuring the invariant instead of the runner's load.
_WORKER_WAIT_SECONDS = 30.0


class _SlowUnwindingChild:
    def __init__(self) -> None:
        self.tool_progress_callback = None
        self._credential_pool = None
        self._delegate_saved_tool_names = []
        self._delegate_role = "leaf"
        self._delegate_depth = 1
        self._subagent_id = None
        self.model = "test-model"
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_estimated_cost_usd = 0.0
        self.session_cost_status = "unknown"
        self.started = threading.Event()
        self.interrupted = threading.Event()
        self.unwinding = threading.Event()
        self.allow_finish = threading.Event()
        self.finished = threading.Event()
        self.closed = threading.Event()
        self.close_while_running = False
        # Set when a wait below gave up: the worker then exited on its own, which is a test
        # diagnostic, never the failure the assertions are about.
        self.release_not_seen = None

    def run_conversation(self, **_kwargs):
        self.started.set()
        if not self.interrupted.wait(timeout=_WORKER_WAIT_SECONDS):
            self.release_not_seen = "interrupt"
            self.finished.set()
            return {
                "final_response": "",
                "completed": False,
                "interrupted": False,
                "api_calls": 1,
                "messages": [],
            }
        # Model the real child turn's finally path: it still performs session
        # activity/SQLite cleanup after the parent requests interruption.
        self.unwinding.set()
        if not self.allow_finish.wait(timeout=_WORKER_WAIT_SECONDS):
            self.release_not_seen = "allow_finish"
        self.finished.set()
        return {
            "final_response": "",
            "completed": False,
            "interrupted": True,
            "api_calls": 1,
            "messages": [],
        }

    def hard_interrupt(self, _reason=None):
        self.interrupted.set()

    def get_activity_summary(self):
        return {"api_call_count": 1}

    def close(self):
        if not self.finished.is_set():
            self.close_while_running = True
        self.closed.set()


class _ReleaseWorkerOnComplete:
    """Progress callback that lets the worker exit BEFORE the parent's close decision.

    ``subagent.complete`` is relayed from the timeout path ahead of
    ``close_deferred = is_timeout and not future.done()``, so releasing the worker here pins
    the "worker already exited => close_deferred is False => cleanup closes immediately"
    branch deterministically instead of leaving it to timing.
    """

    def __init__(self, child: _SlowUnwindingChild) -> None:
        self.child = child
        self.saw_complete = False

    def __call__(self, event_type, **_kwargs):
        if event_type != "subagent.complete":
            return
        self.saw_complete = True
        self.child.allow_finish.set()
        self.child.finished.wait(timeout=_WORKER_WAIT_SECONDS)


def _parent_for(child):
    return SimpleNamespace(
        session_id="parent-timeout-test",
        _current_task_id=None,
        _active_children=[child],
        _active_children_lock=threading.Lock(),
    )


def test_timeout_does_not_close_child_while_worker_is_unwinding(monkeypatch):
    child = _SlowUnwindingChild()
    parent = _parent_for(child)
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0.5)
    monkeypatch.setattr(delegate_tool, "_get_worktree_isolation", lambda: False)

    try:
        result = delegate_tool._run_single_child(
            task_index=0,
            goal="exercise timeout teardown",
            child=child,
            parent_agent=parent,
        )

        assert result["status"] == "timeout"
        assert child.unwinding.wait(timeout=5)
        # The contract is the INVARIANT (close() must never overlap the live conversation
        # thread), not a snapshot of `closed`: when the worker has already exited the parent
        # may close it immediately, which is safe — see the sibling test below.
        assert not child.close_while_running, (
            "timed-out child.close() raced its still-running conversation thread"
        )
        assert child.release_not_seen is None, (
            f"the fake child exited on its own via the {child.release_not_seen!r} budget — the "
            "parent's teardown outran the test's release; raise _WORKER_WAIT_SECONDS instead of "
            "reading this as a teardown bug"
        )
    finally:
        child.allow_finish.set()
    assert child.finished.wait(timeout=5)
    assert child.closed.wait(timeout=5)
    assert not child.close_while_running, (
        "timed-out child.close() raced its still-running conversation thread"
    )


def test_timed_out_child_is_closed_immediately_once_its_worker_has_exited(monkeypatch):
    """The other half of the contract: with the worker already gone (``future.done()``) the
    parent does NOT defer the close — cleanup closes the child right away, and that close is
    still ordered after the conversation thread's unwind."""
    child = _SlowUnwindingChild()
    gate = _ReleaseWorkerOnComplete(child)
    child.tool_progress_callback = gate
    parent = _parent_for(child)
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0.5)
    monkeypatch.setattr(delegate_tool, "_get_worktree_isolation", lambda: False)

    result = delegate_tool._run_single_child(
        task_index=0,
        goal="exercise timeout teardown after worker exit",
        child=child,
        parent_agent=parent,
    )

    assert result["status"] == "timeout"
    assert gate.saw_complete, "the timeout path never relayed subagent.complete"
    assert child.finished.is_set(), "the gate did not hold the close decision until worker exit"
    assert child.closed.is_set(), "an already-exited timed-out child must be closed by cleanup"
    assert not child.close_while_running, (
        "timed-out child.close() raced its still-running conversation thread"
    )
