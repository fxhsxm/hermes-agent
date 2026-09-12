# RCA — async delegation lifecycle: unschedulable units, evicted live records, inline-fallback fan-out

Scope: `tools/async_delegation.py`, `tools/delegate_tool_dispatch.py`, plus the flaky teardown
test in `tests/tools/test_delegate_timeout_cleanup.py`.

Three defects share one theme: **a background delegation's bookkeeping and its actual lifecycle
could disagree, and every disagreement failed silently.** Each was reproduced with a test that
is red on the pre-fix tree. All three are still present on `origin/main` (0f1668ef, upstream
v0.21.2) at audit time — `tools/async_delegation.py:573` (`_persist_dispatch(record)` unguarded),
`tools/async_delegation.py:499` (`status != "running"` prune predicate) and
`tools/delegate_tool_dispatch.py:393` (`_detach_child` with no re-attach path, `_attach_child`
absent from the file) — so this is a fix, not a duplicate.

## 1 — A failed durable registration stranded a `running` phantom and leaked the built children

`_dispatch` registered the in-memory record *before* writing the durable row, and only the
executor-submit branch was guarded:

```python
_records[delegation_id] = record      # slot consumed here
_persist_dispatch(record)             # unguarded: opens state.db, PRAGMAs, DDL, prunes
```

`_persist_dispatch` (`tools/async_delegation.py`) touches a shared SQLite file — a `state.db`
locked past the 10s busy timeout (gateway + CLI + cron write concurrently), a full disk, or a
failed durability barrier all raise. The exception then escaped `_dispatch` out of
`_run_batch`, so:

* the `running` record stayed in `_records` forever, permanently consuming one
  `delegation.max_concurrent_children` slot — after N such failures **no background delegation
  can ever be dispatched again in that process** (`active_count()` never drops);
* the children `_build_children` had already built were neither run nor closed (their SessionDB
  handles and live-transcript writers leaked), because the caller's `at_capacity` synchronous
  fallback was never reached;
* there was no durable row (restart recovery cannot see it), no completion event, and **no log**.

Fix: `_persist_dispatch` is wrapped; on failure the record is unregistered, the durable row
deleted, the reason logged at WARNING, and `{"status": "rejected", "reason": "not_scheduled"}`
returned so the caller runs the batch inline
(`tools/async_delegation.py:554-565`). The pool-submit failure path now reports the same
`reason` and also logs (`tools/async_delegation.py:583-590`).

Test: `tests/tools/test_async_delegation.py::test_persist_failure_rejects_instead_of_leaking_a_capacity_slot`
(asserts `active_count() == 0`, no durable row, a WARNING record, and that the freed slot really
accepts the next dispatch *and* delivers it).

### 1b — The compensation re-entered the very path that had just failed

The rejection is what hands the batch to the inline fallback, so it must be unconditional. But
both rejection paths then DELETEd the durable row, and that DELETE re-opens state.db
(`_connect` → `_initialize_schema` → `apply_durability_barriers`) — the same lock/disk/barrier
failure that triggered the rejection. When the cleanup raised too, the exception escaped
`dispatch_async_delegation` and took the rejection with it: the caller got an error instead of
`{"status": "rejected"}`, so the batch never ran inline and the children — already built and
already detached from the parent's interrupt list (`_dispatch_background`) — were neither run nor
closed. Reproduced with fault injection (`evidence/probe_compensation_failure.py` in the audit
workspace: persist failure + unavailable DB ⇒ `***RAISED OUT OF THE DISPATCH***`,
`active_count=0`; the same on the pool-submit arm).

Fix: `_discard_durable_dispatch` (`tools/async_delegation.py:211-227`) makes the cleanup
best-effort and diagnosable (WARNING naming the delegation), and both arms use it
(`tools/async_delegation.py:562`, `:587`). A row that survives owns no capacity slot and no
in-memory record; the next process start's `recover_abandoned_delegations` classifies it as
outcome unknown and the retention prune reclaims it.

Test: `tests/tools/test_async_delegation.py::test_unschedulable_dispatch_still_rejects_when_its_own_cleanup_write_fails`
(both arms, red before this change with `RuntimeError: database is locked` escaping the call).

## 2 — The retention cap evicted LIVE records, so stalled delegations vanished without a result

`_prune_completed_locked` classified records with `status != "running"`, while the module's live
states are `_LIVE_STATES = {"running", "stalling", "finalizing"}`
(`tools/async_delegation.py:68-69`). A `stalling` record — the stale monitor tripped it, it is
waiting out `_STALL_GRACE_SECONDS` before force-finalizing — has **no `completed_at`**, so it
sorted to the front by `dispatched_at` and was the first record evicted once 50 terminal records
accumulated.

Consequences, all silent:

* `_finalize` bailed out on the missing record (`record is None`), so the parent **never received
  the terminal `stalled` event** and the delegation disappeared from the listing with no result
  and no durable row;
* `interrupt_all` / `interrupt_for_session` (`/stop`, session end) can no longer find that
  record, so a runaway child keeps running tools and burning tokens after the user asked it to
  stop;
* the freed slot let a later dispatch exceed `max_concurrent_children`.

Fix: the predicate is now `status not in _LIVE_STATES`
(`tools/async_delegation.py:474-485`), and `_finalize` distinguishes "already finalized"
(a legitimate no-op) from "record gone" (a dropped terminal event, now logged at WARNING)
(`tools/async_delegation.py:656-670`).

Test: `tests/tools/test_async_delegation.py::test_completed_cap_never_evicts_a_stalling_delegation`.

## 3 — The inline fallback left the children detached from the parent's interrupt fan-out

`_dispatch_background` detaches every child from `parent_agent._active_children` before it knows
whether the async unit was accepted, because the async registry owns their lifecycle from then
on. When the unit is rejected the batch runs inline instead, and the comment claimed
"re-attaching to the parent list is not needed" — but `_run_children_parallel` relies on exactly
that list, twice:

* `AIAgent.interrupt()` fans out through `self._active_children` (`agent/interrupt_control.py`);
* on a parent interrupt the still-pending futures are recorded as `interrupted`
  (`tools/delegate_tool_dispatch.py:107-123`).

So a user pressing stop during a rejected→inline batch got a result entry saying *"Parent agent
interrupted — child did not finish in time"* while those children were still running tools and
writing files. The model trusts that entry and re-dispatches the work, duplicating side effects.
The fallback note also blamed the pool for every failure, including registration failures that
have nothing to do with capacity.

Fix: the fallback re-attaches the children before running inline, picks the note from the
rejection `reason` when it is known, and passes the real error text through
(`tools/delegate_tool_dispatch.py:335-347`, `_SYNC_FALLBACK_NOTES["not_scheduled"]`).

Tests: `tests/tools/test_delegate_inline_fallback.py` (both the capacity and the registration
failure arm).

## 4 — Test defect: a wall-clock race made the timeout-teardown test flaky

`tests/tools/test_delegate_timeout_cleanup.py` parked its fake child on fixed 1s / 2s waits and
then asserted `not child.closed.is_set()`. The parent's post-timeout teardown (steer close →
interrupt → activity summary → `subagent.complete` → `close_deferred = is_timeout and not
future.done()` → cleanup) takes unbounded wall time on a loaded runner, so the child's budget
could expire first: the worker exited, the parent *correctly* computed `close_deferred = False`
and closed an already-exited worker, and the assertion read that as the bug under test. The
canonical runner reported the file ⚠ FLAKY (failed attempt took 13.16s vs 4.28s on retry);
a 3s stall injected into the teardown reproduces the same failure deterministically.

Fix: the worker is released by the test, never by a stopwatch (generous budget plus an explicit
`release_not_seen` diagnostic), the assertions target the invariant
(*`close()` must never overlap the live conversation thread*) rather than a race snapshot, and a
second test pins the other half of the contract — an already-exited worker **is** closed
immediately by cleanup.

## Not changed on purpose

* `child_timeout_seconds = 0` (no wall-clock cap) and the heartbeat/stall thresholds: product
  decisions, not defects.
* A batch occupying ONE async pool slot: upstream keeps the same accounting
  (`slot_key` per call), so it is design, not a bug.
* `validate_output` raising inside `_validate_child_output_schema` discards a completed child's
  answer and its cost rollup — real, but a separate change; see the audit notes.
* `subagent.interrupt` lacking ownership checks (`tui_gateway/methods_session.py`): already
  removed upstream, and a live UI may still call it.
