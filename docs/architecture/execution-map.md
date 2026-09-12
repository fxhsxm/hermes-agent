# Hermes Agent — request execution architecture map

What actually happens between "a user sends a message" and "the answer is stored and
delivered", traced through the running source rather than through the subsystem docs.

Audience: developers changing the core, anyone debugging a live session, and agents
maintaining this repository. Read §1 for the shape, §2 for the stage-by-stage chain,
§5 when something fails. Every load-bearing claim carries a `path/file.py:LINE`
anchor in the grammar documented in `docs/architecture/README.md`; re-check them all
with `python docs/architecture/tools/verify_anchors.py`.

## 0. Baseline and method

- **Pinned baseline:** commit `fa24c030a5e48af63eb152a0b3b344a99e991244`
  (`fix(bridge): bridge-injected turns must reach their bound Telegram topic (Issue #2)`),
  branch `docs/execution-architecture-map-20260912`. Every anchor below was verified
  against that tree.
- **Runtime cross-check:** the live gateway on the authoring machine ran *that same
  commit* — `gateway_state.json` reports
  `code_sha = fa24c030a5e48af63eb152a0b3b344a99e991244`, `code_version = 0.21.0`,
  `platforms.telegram.state = connected`. Live database rows and process state were
  used to confirm (or correct) the source reading; see Appendix A.
- **Tree size at baseline:** 5,565 Python files outside `venv/`, `node_modules/`, `.git/`;
  3,754 test files under `tests/` containing 39,629 `def test_` functions.
- **What "runtime wins" means here:** where the documentation and the code disagreed,
  the code (and the live deployment) decided, and the disagreement is recorded in
  `doc-runtime-deltas.md` with both anchors.
- **Method:** the end-to-end spine was traced by hand (Main) from the platform adapter
  to the delivery call; the five planes (ingress, turn loop, tools, state, delivery) and
  the documentation audit were traced in parallel delegated lanes, each required to
  verify every anchor by reading the referenced lines. All anchors quoted in this
  document were then re-verified mechanically against the pinned tree; the delegated
  artifacts were separately anchor-checked before use.

## 1. The path in one page

One normal request on a messaging platform, in order. Anchors are the *entry symbol*
of each stage; §2 expands each stage.

```text
 S0  platform adapter receives the message
     plugins/platforms/<platform>/adapter.py           (e.g. telegram)
     -> gateway/platforms/base.py:3936 _process_message_background()
     -> gateway/platforms/base.py:3952 self._message_handler(event)

 S1  gateway admits the event and binds a session
     gateway/run_inbound.py:1176 GatewayInboundMixin._handle_message()
        auth -> control-command intercept -> busy/queue guard -> session key -> SessionEntry

 S2  turn dispatch + agent resolution
     gateway/run_turn.py:1918 _handle_message_with_agent()
     -> gateway/run_turn.py:3774 _run_agent_inner()
     -> gateway/run_turn_runner.py:1601 TurnRunner.run_sync()   (executor thread)
        resolve model+provider -> find/build the cached AIAgent -> load history

 S3  the agent runs the turn
     agent/turn_facade.py:22 TurnFacadeMixin.run_conversation()
     -> agent/conversation_loop.py:1390 run_conversation()      (durable turn lease, context scopes)

 S4  iteration loop (bounded by max_iterations + iteration budget)
     agent/conversation_loop.py:1479 while-loop
        begin_iteration -> prepare_iteration -> assemble_api_request -> preflight gate
        -> perform_api_call (model inference) -> normalize_model_response

 S5  branch on the response
     agent/turn_tool_round.py:45 run_tool_round()      if tool_calls
     agent/turn_final_response.py:46 finish_text_response()   if plain text
        tool dispatch: model_tools.py:801 handle_function_call()
          -> model_tools.py:697 _pre_dispatch_guards() -> model_tools.py:754 _execute_tool()

 S6  turn finalization
     agent/turn_finalizer.py:433 finalize_turn()  (post-turn hooks, memory/review, result dict)

 S7  result handoff back through the gateway
     gateway/run_turn_runner.py:1601 run_sync() returns the gateway result dict
     -> gateway/run_turn.py  post-turn: session bookkeeping, persistence, failure classification

 S8  state write (durable)
     hermes_state.py:327 class SessionDB  <->  gateway/session_persistence.py:42 SessionPersistenceMixin
        session row, user/assistant/tool message rows, usage, title, transcript

 S9  delivery to the human
     gateway/delivery.py:147 DeliveryRouter
     -> gateway/delivery_ledger.py:161 record_obligation()   (at-least-once bookkeeping)
     -> gateway/platforms/base.py:2503 BasePlatformAdapter.send()

 X   cross-cutting: cancellation / mid-turn steering, context compression,
     approval prompts, budgets and leases (see §3 and §5)
```

The same core is reached from other surfaces (CLI `cli.py`, TUI/desktop JSON-RPC
`tui_gateway/`, ACP, the HTTP API server, cron): they differ in the ingress and egress
edges (S0/S9 and how the session is bound), not in S3–S7.

## 2. Stage reference

Each stage: what triggers it, the entry symbol, what it produces, and the invariants
that must hold. Anchors verified against the pinned baseline; lane attribution is in
`docs/architecture/audits/`.

### 2.1 S0 — Ingress at the platform adapter

Bundled platforms are **plugins**, not in-tree modules: `plugins/platforms/<name>/plugin.yaml`
declares `kind: platform` and `adapter.py` exports `register(ctx)`
(`plugins/platforms/telegram/adapter.py:6519`) → `ctx.register_platform(...)`
(`plugins/platforms/telegram/adapter.py:6521`). Registration is **deferred** — the heavy
adapter module is not imported until the platform is materialised
(`gateway/platform_registry.py:156` `register_deferred()`), which is what keeps gateway
startup cheap. In-tree adapters still live in `gateway/platforms/` (see `doc-runtime-deltas.md` D3).

- Instantiation: `gateway/run_adapters.py:1434` `_create_adapter()` → `_instantiate_adapter()`.
- Handler wiring happens **before** `connect()`: `gateway/run_adapters.py:1032`
  `_wire_adapter_handlers()` → `set_message_handler(...)` at `gateway/run_adapters.py:1039`.
- Connect: `gateway/run_startup.py:921` `_start_connect_pending()`; failures go to a
  supervised reconnect watcher (`gateway/run_startup.py:1207`).

Invariants: an unwired adapter silently discards every inbound message
(`gateway/platforms/base.py:3526`); the bot's own messages must be dropped at intake
(`plugins/platforms/telegram/adapter.py:5619`); Telegram text is debounced/coalesced before
dispatch so a client-side 4096-char split arrives as one turn
(`plugins/platforms/telegram/adapter.py:5703` → `:5797` `_flush_text_batch`).

### 2.2 S1 — Adapter session guard and handoff

`gateway/platforms/base.py:3523` `handle_message()` is the single funnel. It resolves the
session key (`gateway/platforms/base.py:3534`; `build_session_key` at `gateway/session.py:641`)
and then takes one of three routes: inline bypass dispatch (control commands), queue/merge
into `_pending_messages`, or a fresh background task.

Invariants (each one is a bug class that was fixed in the past):

- **Guard installed synchronously before the task spawns** —
  `gateway/platforms/base.py:3440-3442`.
- **On-entry self-heal**: a guard whose owner task is already done is cleared, so a
  split-brain cannot trap a chat until restart (`gateway/platforms/base.py:3541`).
- **Late arrivals are reconciled, never dropped** (`gateway/platforms/base.py:3912`, `:3929`).
- **One pending slot, merged not overwritten** (`gateway/platforms/base.py:3613`) and a
  bounded FIFO behind it (`gateway/run.py:3863` `_BUSY_QUEUE_MAX_PENDING = 32`).

### 2.3 S2 — Gateway admission, routing, turn dispatch

`gateway/run_inbound.py:1176` `GatewayInboundMixin._handle_message()`, in order:

| Step | Anchor |
|---|---|
| admission sub-gates (profile route, ignored channel, internal, pairing, auth) | `gateway/run_inbound.py:1181` → `_hm_admit_event` `:111` |
| emergency stop (`hermes pause`) blocks **new** turns only | `gateway/run_inbound.py:1185` → `:228` |
| routing key | `gateway/run_inbound.py:1189` `_session_key_for_source` |
| busy fast-path (interrupt / queue / steer / demote) | `gateway/run_inbound.py:1200` → `:597` |
| idle slash-command dispatch (ends here, no turn) | `gateway/run_inbound.py:1201` → `:1129` |
| claim the active-session slot (cross-process) | `gateway/run_inbound.py:1219` → `gateway/run_busy.py:170` |
| `_AGENT_PENDING_SENTINEL` + run generation | `gateway/run_inbound.py:1230` → `:1240` |
| turn with agent | `gateway/run_inbound.py:1244` → `gateway/run_turn.py:1918` |

Then: session resolve/bind (`gateway/run_turn.py:1932` → `:254`), turn preparation
(`gateway/run_turn.py:1935` → `:1832`), turn lease (`gateway/run_turn.py:1864` →
`gateway/turn_lease.py:95`), `_run_agent` (`gateway/run_turn.py:1960` → `:3774`) and worker
dispatch (`gateway/run_turn.py:3828` → `:3056`, executor task at `:3118`, executor at
`gateway/run.py:4115`) — the agent turn runs **on a worker thread**, not on the event loop, and a
per-turn `gateway-turn-watchdog-<id>` daemon thread watches for inactivity
(`gateway/run_turn.py:3110`).

### 2.4 S3–S4 — Turn runner and the agent loop

`gateway/run_turn_runner.py:1601` `TurnRunner.run_sync()` is the executor-thread body:
resolve runtime (`:1627`) → resolve/cached-build the `AIAgent` (`_resolve_turn_agent`,
`gateway/run_turn_runner.py:980`) → wire callbacks (`:1086`) → load history (`:1292`) →
run (`:1441` `return agent.run_conversation(api_message, **kwargs)`).

Inside the agent:

- `agent/turn_facade.py:22` `run_conversation()` is a **facade**: durable turn lease admission
  (`agent/turn_facade.py:77`), relay/accounting ContextVar scopes, then a forward to the real
  loop at `agent/turn_facade.py:122`.
- `agent/conversation_loop.py:1390` `run_conversation()`: turn prologue via
  `build_turn_context` (`agent/turn_context.py:744`), `_LoopState` built at
  `agent/conversation_loop.py:1465` (dataclass at `:1247`), loop guard at `:1479`
  (`api_call_count < max_iterations and iteration_budget.remaining > 0`, plus one grace call).
- Iteration phases — outer body `agent/conversation_loop.py:1480-1495`:
  `begin_iteration` (`agent/turn_iteration_prep.py:220`), `prepare_iteration` (`:34`),
  `assemble_api_request` (`agent/turn_request_assembly.py:106`), `run_preflight_gate`
  (`agent/turn_preflight_gate.py:20`), `announce_api_call` (`agent/turn_iteration_prep.py:175`),
  then the inner per-attempt retry loop (`agent/conversation_loop.py:1358`):
  `nous_rate_limit_guard` (`agent/turn_api_call.py:207`) → `build_api_request`
  (`agent/turn_api_request.py:94`) → `perform_api_call` (`agent/turn_api_call.py:61`) →
  `check_api_response` (`agent/turn_response_check.py:84`), with `handle_api_interrupt`
  (`agent/turn_api_call.py:164`) and `handle_api_error` (`agent/turn_api_error.py:51`).
- Response branch at `agent/conversation_loop.py:1513`: `run_tool_round`
  (`agent/turn_tool_round.py:45`) when the assistant message carries tool calls, otherwise
  `finish_text_response` (`agent/turn_final_response.py:46`). Loop exits are logged by
  `_log_turn_exit` (`agent/turn_finalizer.py:298`).
- Post-loop: `agent/turn_finalizer.py:433` `finalize_turn()` produces the result dict;
  the authoritative payload key is set at `agent/turn_finalizer.py:537`.

Provider/route resolution is **not** in the loop: `hermes_cli/runtime_provider.py:813`
`resolve_runtime_provider()` resolves credentials/routes, the values land on the agent in
`agent/agent_init.py:2239-2246`, and the API mode is chosen by the ladder in
`agent/agent_init.py:372` `_resolve_api_mode()` (accepted set at `:366`; the set is **five**,
not three — see `doc-runtime-deltas.md`). Streaming is decided per attempt by
`_should_stream` (`agent/turn_api_call.py:43`); the actual call is dispatched by
`perform_api_call` into the active transport (`agent/chat_completion_helpers.py:1297`
`interruptible_api_call`, streaming at `:3552`).

### 2.5 S5 — Tool plane

The tool plane is **synchronous**: every tool runs on a worker thread and async handlers are
bridged into thread context. Two facts correct the common mental model:

- **There is no per-tool approval gate in the dispatcher.** `model_tools.py:801`
  `handle_function_call()` runs guards (`model_tools.py:697` `_pre_dispatch_guards`, called at
  `model_tools.py:854`) and then executes (`model_tools.py:754` `_execute_tool`). Approval lives
  inside the tools that need it: `tools/approval.py:978` `check_all_command_guards()` from
  `tools/terminal_tool.py:837`, `tools/approval.py:1047` `check_execute_code_guard()` from
  `tools/code_execution_tool.py:707`, and file-write guards from
  `tools/file_tools_write_guards.py:248`. Agent cross-session writes use a separate stage-then-
  approve path (`tools/write_approval.py:170` `evaluate_gate()`).
- **Concurrency is planner-driven, not a fixed pool.** The round is handed to
  `agent/turn_tool_round.py:152` `agent._execute_tool_calls(...)`, which is sequential when
  `len(tool_calls) <= 1` (`run_agent.py:1282`) and parallel **only** when the planner yields a
  single all-parallel segment for the whole batch (`run_agent.py:1288-1293`); a mixed batch
  degrades to segment-by-segment execution in original order. Per-call admission is decided in
  `agent/tool_dispatch_helpers.py:103` `_batch_admission()`.

Tool schemas: `model_tools.py:212` `get_tool_definitions()` memoizes per key
(`_tool_defs_cache`, cap 8 at `model_tools.py:203`, FIFO eviction, no TTL); the key covers tool
registration generation, the `config.yaml` stat fingerprint, the profile/check-fn scope and
Kanban/delegated-child context (`model_tools.py:254`). The cached list is handed out as a shallow
copy (`model_tools.py:227`) — a shared list once produced duplicate tool names and HTTP 400s
from strict providers. Availability is a **second** cache with its own policy: `check_fn` results
are TTL-cached process-wide and a failure within 60 s of a good result serves `True` without
caching (`tools/registry.py:324`). Discovery is `tools/registry.py:86` `discover_builtin_tools()`;
MCP discovery is deliberately *not* part of it (`model_tools.py:149`).

### 2.6 S6–S7 — Turn handoff and state writes

The gateway's job after the agent returns:

| Step | Anchor |
|---|---|
| publish result to the turn context | `gateway/run_turn_runner.py:1459` |
| finish the stream consumer with the final text | `gateway/run_turn_runner.py:1472-1480` |
| sync session bookkeeping (compaction flag, session id, history offset) | `gateway/run_turn_runner.py:1501` `_sync_session_after_run()` |
| shape/normalize/sanitize the user-visible response | `gateway/run_turn.py:1354` → `:1386` → `:1387` |
| delivery decision (silence, voice, media, footer) | `gateway/run_turn.py:1708` `_hmwa_deliver_turn_response()` |
| return to the adapter | `gateway/run_inbound.py:1244` → `:1265` |

Durable state is the `SessionDB` facade (`hermes_state.py:327`), whose siblings
(`hermes_state_*.py`, 21 files) each own one topic; the gateway side is
`gateway/session_persistence.py:42` `SessionPersistenceMixin` plus
`gateway/session_lifecycle.py:58` `SessionLifecycleMixin`. What is written, in order, is the
session row, the user message row, per-iteration assistant/tool rows, usage counters, the title,
and the transcript; the API-key/turn boundaries are `agent/message_metadata.py:31`
`append_message()` on the in-memory side and the gateway's persistence calls on the durable side.
The user row is written **before** the first model call, so a turn that dies mid-flight still
leaves the user's message on disk.

### 2.7 S8 — Delivery

The final text leaves the agent already scrubbed (`agent/turn_finalizer.py:533`) and is delivered
by the **adapter lane**, not by `gateway/delivery.py`: `gateway/platforms/base.py:3786`
`_send_final_text()` brackets the send with the delivery ledger —
`gateway/platforms/base.py:3794` records the obligation *before* the transport call, `:3796`
performs `_send_with_retry()` (`gateway/platforms/base.py:3236`), and `:3800` finalizes the row.
`gateway/delivery.py:147` `DeliveryRouter` is constructed by the gateway
(`gateway/run.py:3386`) but in-tree production sends through it come from cron
(`cron/scheduler_delivery.py:1175`), not from the direct-reply path.

Guarantees differ per lane and are worth knowing before "fixing" a duplicate:

- Gateway final replies are **at-least-once**, and the ledger says so on the wire:
  `RECOVERED_MARKER` / `RECONNECTED_MARKER` (`gateway/delivery_ledger.py:39`).
- Cron's durable queue is **at-most-once**: an uncertain claimed send is terminalized as
  `unknown` and never retried (`cron/delivery_queue.py:321-336`), keyed by execution id
  (`cron/scheduler_delivery.py:1605`).

Ordering/duplication at the boundary is governed by the "two egress doors"
(`send()` and `send_for_platform()`), the interim-send flag, and the rule that a non-final lane
reconciles by **edit**, never by a second plain send (see `gateway/AGENTS.md`, streaming
contract). Media is carried by the `MEDIA:` convention extracted in
`gateway/platforms/base.py:3964` and delivered after the text
(`gateway/platforms/base.py:3990`).

## 3. Control plane

Control inputs act at three different layers, and mixing them up is the usual source of
"the bot is stuck" reports.

- **Stop / interrupt** — `agent/interrupt_control.py:165` `_abort_active_request()`, surfaced as
  `InterruptedError` during the model call and caught at `agent/conversation_loop.py:1378`;
  the partial streamed text is preserved. A tool-phase interrupt degrades to a tool-level abort.
- **Steering** — `/steer` appends to `_pending_steer`, drained **before** the next API call and
  injected into the newest tool result, never as a synthetic user row
  (`agent/turn_iteration_prep.py` `_drain_pending_steer`). A mid-turn correction
  (`redirect`) downgrades to steer while tools are executing, and otherwise aborts only the
  in-flight request.
- **Queued turn** — the busy fast-path decides between interrupt, queue, steer or demote
  (`gateway/run_inbound.py:597`); an interrupt is demoted to queue while the running agent has
  live subagents. The queue is bounded (`gateway/run.py:3863`, `_BUSY_QUEUE_MAX_PENDING`).
- **Budgets** — iteration budget is thread-safe and refunded for `execute_code`-only rounds
  (`agent/iteration_budget.py:13`); the grace call is consumed at most once
  (`agent/turn_iteration_prep.py:275`); the wall-clock run budget injects a one-shot wrap-up
  notice at ≥80 % and the outer-loop exception cap is 8
  (`agent/conversation_loop.py:223`).
- **Leases** — two exist. In-process/at-gateway: `gateway/turn_lease.py:95` keyed by
  (session, owner, generation), released identity-checked. Durable: `session_turn_leases` written
  through `agent/turn_facade_lease.py:232` `admit_durable_turn_lease()`, which lets a *different
  process* refuse to start a second concurrent turn for the same session.
- **Emergency stop** — `hermes pause` blocks new turns at admission
  (`gateway/run_inbound.py:228`) and is **not** read by the turn loop: in-flight work is never
  killed by design (`agent/estop.py:52`).

## 4. State plane

Durable state is one SQLite store per profile (`HERMES_HOME/state.db`), schema version **30**
(`hermes_state_common.py:197`), plus transcript artifacts. Load-bearing properties:

- **WAL is negotiated, not assumed**: WAL can be refused on network filesystems, disabled by
  config, or withheld on buggy SQLite builds (`hermes_state_wal.py:147`, `:199`, `:220`).
- **Writes are time-bounded, not attempt-bounded**: `_WRITE_PATIENCE_S`/
  `_TRANSCRIPT_WRITE_PATIENCE_S`/`_ACTIVITY_WRITE_PATIENCE_S = 20.0/60.0/0.5`
  (`hermes_state.py:349`) with jitter (`hermes_state.py:362`); attempt-counted budgets were
  deliberately removed.
- **The stored system prompt lives in `system_prompts(hash, prompt)`**, referenced from
  `sessions.system_prompt_hash`; `sessions.system_prompt` is written NULL
  (`hermes_state_sessions.py:618`). Reading the prompt therefore requires the join, not a
  column read.
- **Two writers are supported but not free**: the facade detects foreign holders
  (`hermes_state.py:316`) and the gateway opens per-key handles
  (`gateway/session_persistence.py:142`).
- **Resume** rebuilds history from message rows with stale-marker stripping
  (`hermes_state.py:255-264`) and size guards (`hermes_state.py:86` `resolved_max_resume_messages()`),
  so a truncated transcript can be diverted instead of silently replayed
  (`hermes_state.py:297` `divert_session_transcript_jsonl()`).

## 5. Failure-path catalogue

Detect by symptom, then follow the path. Every row is anchored.

| Symptom | Detection | Code path | Behaviour / recovery |
|---|---|---|---|
| Same chat gets two turns for one message | duplicated platform delivery or a stale guard | adapter guard + late-arrival reconciliation `gateway/platforms/base.py:3912` | oldest-first FIFO, merged pending slot; never two live owners for one session key |
| Chat stuck "Interrupting…" forever | guard held with a dead owner task | `gateway/platforms/base.py:3541` | on-entry self-heal clears guard/owner/pending |
| Duplicate final reply after restart | `RECOVERED_MARKER` prefix on the message | `gateway/delivery_ledger.py:39`, sweep at `:228` | at-least-once by design; ledger row decides plain vs marked redelivery |
| Model call hangs with no output | per-provider stale timeout, else `HERMES_STREAM_STALE_TIMEOUT` (default 180 s), context-scaled | `agent/chat_completion_helpers.py:541` | stale stream aborts the attempt; retry/failover ladder continues |
| Provider 429 / 402 / 5xx | classified in `agent/turn_api_error.py:156` | `route_classified_error` (`agent/turn_recovery.py:1233`) | retry with backoff, credential refresh (401 only, `agent/turn_recovery.py:302`), or failover |
| Context window exceeded | preflight gate (`agent/turn_preflight_gate.py:20`) or provider error | `recover_from_overflow` (`agent/turn_overflow.py:419`) | compress, then retry; compression timeout ends the turn with a terminal message (`agent/turn_preflight.py:148`) |
| Empty / truncated / refusant response | response checks `agent/turn_response_check.py:84` | `recover_empty_response` (`agent/turn_empty_response.py:141`), truncation handling (`agent/turn_truncation.py`) | budgeted retry; codex `incomplete` continuation; content-filter rollback `agent/turn_truncation.py:156` |
| Turn ends with no answer | tail is a tool result | `_log_turn_exit` `agent/turn_finalizer.py:298` (warning at `:327`) | the library logs the exit reason; check `_turn_exit_reason` in the result |
| Tool never returns | per-tool timeout; thread cannot be killed | thread pool + timeouts in the tool plane (§2.5) | the tool's result is replaced by a timeout/error string; the turn continues |
| Session DB unavailable / corrupt | write patience exhausted, integrity check | `hermes_state_repair.py`, `hermes_state_guard.py` | turn fails with `session_persistence_failed` (`agent/turn_tool_round.py:131`) rather than losing the transcript invisibly |
| Second gateway or CLI on one profile | PID/state files + foreign-holder detection `hermes_state.py:316` | `gateway/run.py` claim/exit paths | second instance refuses or hands over; a concurrent turn for the same session is rejected by the durable lease |
| Approval asked with no human present | approval wait timeout | `tools/approval_gateway_wait.py` / `tools/approval_human_wait.py` | deny/timeout returns a model-visible refusal; the turn continues |

## 6. Documentation vs runtime

The documentation audit that accompanied this map found defects in the developer-facing docs:
eleven are fixed in this commit (resolving paths, dead symbols, an off-by-2× toolset count and two
wrong loop-budget defaults), one high-severity fix is prepared but unapplied (its target is a
protected `AGENTS.md`), and the rest are recorded. Everything with both anchors lives in
`doc-runtime-deltas.md`. The two findings worth knowing before reading further:

- The doc that describes the inbound guard chain (`website/docs/developer-guide/gateway-internals.md`)
  describes a *different* guard chain than the code runs (it names a `_running_agents` check and an
  "interrupt event" that the adapter does not set, and gives a session-key example that cannot occur).
- Several executable-looking details are stale: the API-mode count, the default `max_iterations`,
  the credential-refresh status codes, and the claim that an interrupted turn injects no partial
  response — the runtime does the opposite.

The rule going forward: if the map and the runtime disagree, the runtime wins, and the map is
corrected in the same change.

## 7. Extension seams

Places where behaviour is expected to be added, in the order the project prefers
(least core footprint first):

| Seam | Anchor | Use it for |
|---|---|---|
| Plugin kinds (tool/toolset/platform/hook/memory/provider) | `hermes_cli/plugins.py` `register_platform` (:770), `register_tool`, hook registration | third-party or bundled capability that must not grow the core |
| Gateway hooks | `gateway/hooks.py`, `gateway/builtin_hooks/` (empty by design) | always-registered gateway extension points |
| Tool registration | `tools/registry.py:596` `register()` | a new tool (auto-discovered at `tools/registry.py:86`) |
| Toolset membership | `toolsets.py:11` `_HERMES_CORE_TOOLS` | deciding whether a tool is core or opt-in |
| Prompt assembly | `agent/prompt_builder.py` | system-prompt content |
| Request middleware / hooks | `agent/turn_api_request.py:151` `apply_llm_request_middleware`, `:44` `pre_api_request` | mutating outbound requests, redaction, routing |
| Tool-call hooks | `model_tools.py` pre/post tool-call hooks, `_emit_post_tool_call_hook` (`model_tools.py:615`) | observing or transforming tool calls/results |
| Context engine | `agent/context_engine.py` | replacing how context is assembled/compacted |
| Platform adapters | `plugins/platforms/<name>/` or `gateway/platforms/<name>.py` | a new chat platform |

Plugin discovery is a **per-process cache**, so a newly installed plugin needs a gateway
restart to take effect.

## 8. Change protocol (keep this map true)

1. Touch one of the files named here → update its anchor in the same commit, then run
   `python docs/architecture/tools/verify_anchors.py` (exit 0 required).
2. New behaviour that moves a stage boundary (a new phase, a new delivery lane, a new state
   write) → extend the relevant §2 subsection rather than the appendix.
3. Runtime contradicts the map → fix the map, record why in `doc-runtime-deltas.md`.
4. Do not add unverified anchors "for completeness": an anchor you did not read is worse than
   no anchor, because the verifier will keep it green while it silently rots.
5. Anything anchored at a line number is a maintenance liability by construction. Prefer
   anchoring the *entry symbol* of a stage over an incidental interior line.

## Appendix A — live runtime cross-check

The live gateway on the authoring host ran the pinned baseline
(`gateway_state.json`: `code_sha = fa24c030a5e48af63eb152a0b3b344a99e991244`,
`code_version = 0.21.0`, telegram `connected`, `session_store.status = ok`), so the running
process could be compared with the source reading. Observations (all read-only):

| Claim in this map | Live observation |
|---|---|
| Session keys are `<ns>:<platform>:<chat_type>…` with `dm` as the DM slot, not `private` | the live session key is `agent:main:telegram:dm:<chat_id>:<thread_id>` (`sessions.session_key`) |
| Schema version 30 | `state.db` → `schema_version` = 30, matching `hermes_state_common.py:197` |
| Assistant/tool turns persist as individual rows with `tool_name` | one live session recorded `assistant: 55`, `tool: 84` rows; `messages.tool_name` shows per-tool counts (e.g. `terminal`, `delegate_task`) |
| Sessions carry routing identity for the gateway | live `sessions` row carries `source=telegram`, `chat_id`, `thread_id`, `profile_name=default`, `model`, `api_call_count`, `tool_call_count` |
| Durable turn leases exist and are used | `session_turn_leases` held rows while the session was live |
| The system prompt is stored out-of-band | `system_prompts` holds 480 rows while `sessions.system_prompt` is NULL for current sessions |
| `api_calls` is an orphan table | `api_calls` had no rows for the live session even though `sessions.api_call_count` was non-zero |

Caveat, stated plainly: the working tree of the live install also carried unrelated uncommitted
edits (skill/prompt/send-message files) that are **not** part of the pinned baseline. Those files
are outside every path this map anchors, and no conclusion above depends on them.

