# Documentation vs runtime — deltas found while building the execution map

Scope: documentation that makes **concrete, checkable claims about the running system**
(file paths, symbols, counts, execution order) compared against the pinned baseline
tree `fa24c030a5e48af63eb152a0b3b344a99e991244`.

Method: each row was verified by reading the documentation line *and* the runtime
source it describes. A claim that could not be checked is not listed as a delta.
Rows marked **valid** are included on purpose: they show the coverage of the audit,
not only its failures.

Disposition legend: `fixed` = documentation corrected in this commit; `recorded` =
kept as a finding, no change made (low severity, or a code change outside the scope of
this document's remit); `follow-up` = a real defect that deserves its own change.

## 1. Fixed in this commit

| # | Doc anchor | Claim | Runtime truth | Severity |
|---|---|---|---|---|
| D1 | `gateway/platforms/ADDING_A_PLATFORM.md:125` | "See `gateway/platforms/telegram.py`, `discord.py`, and `whatsapp_cloud.py` for reference implementations" | `gateway/platforms/telegram.py` and `gateway/platforms/discord.py` **do not exist**; the reference adapters are `plugins/platforms/telegram/adapter.py` and `plugins/platforms/discord/adapter.py`. Only `gateway/platforms/whatsapp_cloud.py` is in-tree. | high — the canonical "add a platform" guide points at files a developer cannot open |
| D2 | `gateway/platforms/ADDING_A_PLATFORM.md:59` | "`gateway/platforms/whatsapp.py` (Baileys bridge)" | no such file; the library-based WhatsApp adapter is `plugins/platforms/whatsapp/adapter.py`. Line 60's `gateway/platforms/whatsapp_cloud.py` is correct. | medium |
| D4 | `website/docs/reference/tools-reference.md:79` | tool scoped to "(`gateway/platforms/feishu_comment.py`)" | File lives at `plugins/platforms/feishu/feishu_comment.py`. | low |
| D5 | `docs/chronos-managed-cron-contract.md:149` | "`plugins/cron/chronos/verify.py`" | File lives at `plugins/cron_providers/chronos/verify.py`; `plugins/cron/` does not exist. | medium — contract doc names a path that cannot be opened |
| D6 | `website/docs/developer-guide/architecture.md:8` | Top-level architecture page did not reference any runtime-verified end-to-end map | Added a pointer to `docs/architecture/execution-map.md` (and the delta log) from the page a developer already reads first. | low |

## 2. Recorded, not fixed

| # | Doc anchor | Claim | Runtime truth | Severity |
|---|---|---|---|---|
| D3 | `gateway/AGENTS.md:10` | "adapters in `platforms/<name>.py` over `platforms/base.py`" | Most platform adapters are **plugins**: `plugins/platforms/<name>/adapter.py` (22 platform plugin directories, `kind: platform` in `plugin.yaml`, registered through `hermes_cli/plugins.py` `register_platform`). `gateway/platforms/` holds the adapter base, helpers, the API server, and the remaining in-tree adapters (`signal.py`, `weixin.py`, `whatsapp_cloud.py`, `bluebubbles.py`, `msgraph_webhook.py`, `webhook.py`, `yuanbao*.py`, `qqbot/`). **Fix prepared but not applied:** `AGENTS.md` is a protected agent-instruction file — the write requires explicit user approval and was refused by policy here. Suggested replacement is in §5. | high — routing doc sends a developer into the wrong tree |
| D7 | root `AGENTS.md` ("Development Environment") | "`scripts/run_tests.sh` probes `.venv`, then `venv`, then `$HOME/.hermes/hermes-agent/venv` (worktrees sharing the main checkout's venv)" | Accurate about the script — `scripts/run_tests.sh:54` really does probe those three paths literally. But the third probe hardcodes `$HOME/.hermes`, while every other path in the tree is profile-aware (`get_hermes_home()`); on Windows installs, `HERMES_HOME` is `%LOCALAPPDATA%\hermes`, so the worktree-venv probe misses and only the `HERMES_PYTHON` escape hatch works (observed on this host). Device tooling, not runtime behaviour. | low (dev UX) |
| D8 | root `AGENTS.md` ("Project Structure") | sibling-family counts: `hermes_state.py (21)`, `gateway/run.py (15)`, `tools/mcp_tool.py (15)`, `hermes_cli/kanban.py (14)`, `hermes_cli/web_server.py (13 + 24 routers)`, `hermes_cli/auth.py (12)`, `tools/browser_tool.py (11)`, `cli.py (12 hermes_cli/cli_*_mixin.py)` | Measured: 21 ✓, 15 ✓, 15 ✓, 14 ✓, `hermes_cli/web_server_*.py` **14** (claimed 13), `hermes_cli/web_routers/*.py` **23** (claimed 24), `hermes_cli/auth_*.py` 12 ✓, `tools/browser_tool_*.py` 11 ✓, `hermes_cli/cli_*_mixin.py` **14** (claimed 12) | low (cosmetic; the doc itself warns these counts "shift constantly") |

## 3. Stale test found while verifying (recorded as follow-up)

| # | Anchor | Claim | Runtime truth | Severity |
|---|---|---|---|---|
| T1 | `tests/test_hermes_state.py:936` | asserts that the `fields=("context",)` search path issues a query containing the literal `"WITH TARGET AS ("` | `grep -rn "WITH TARGET AS" hermes_state*.py` matches **nothing** at this baseline — the SQL shape was replaced, so the trace-callback counter stays 0 and the assertion fails. The functional assertions in the same test (a `context` value is returned) pass, so behaviour is intact and only the SQL-text assertion is stale. Reproduced deterministically and standalone (`1 failed, 260 deselected in 0.74s`). Not fixed here: the repo's own test policy calls this shape of assertion a change-detector, so the right fix is a maintainer decision, not a drive-by edit. | low (test-only) |

## 4. Valid confirmations (coverage evidence)

| # | Doc anchor | Claim | Verification |
|---|---|---|---|
| V1 | root `AGENTS.md` ("Project Structure") | "~39k tests / ~3.7k files, Sep 2026" | Measured at baseline: 3,754 test files, 39,629 `def test_` |
| V2 | `gateway/AGENTS.md:11` | "`builtin_hooks/` is the extension point for always-registered gateway hooks (none shipped)" | `gateway/builtin_hooks/` contains only `__init__.py` |
| V3 | root `AGENTS.md` ("Code Shape Rules") | compat pointers are OFF LIMITS in-tree and are removed 2026-09-14 by reverting one commit | Baseline still carries the `PLUGIN-COMPAT` block (`agent/conversation_loop.py:1541` region) and `COMPAT_MANIFEST.md` / `compat_manifest.json`; consistent with a scheduled (future-dated) removal |
| V4 | root `AGENTS.md` ("Facade + siblings layout") | `run_agent.py` is a facade over `agent/turn_*.py` + `agent/conversation_loop.py`; `hermes_state.py` is a facade over `hermes_state_*.py` | Confirmed: `run_agent.AIAgent` delegates the turn to `agent/turn_facade.py:22`, whose body forwards to `agent/conversation_loop.py:1390`; the loop calls the `agent/turn_*.py` phases imported at `agent/conversation_loop.py:32` |

## 5. Consolidated deltas from the parallel documentation audit

Provenance: these rows were produced by dedicated audit lanes that each had to read both the
documented line and the runtime line, and were then re-checked mechanically by Main
(`docs/architecture/tools/verify_anchors.py` grammar over the raw lane artifacts: 1,242 anchors,
0 out-of-range line numbers, 0 missing files). They are listed here because the lane artifacts
themselves are scratch. Severity: **H** = a reader builds the wrong thing; **M** = materially
wrong fact or dead symbol; **L** = cosmetic/omission.

### 6.1 `website/docs/developer-guide/gateway-internals.md` (inbound guard chain)

| Stated in the doc | Runtime truth | Sev |
|---|---|---|
| "Level 2 — Gateway runner … Checks `_running_agents`" (`:88`) | `gateway/run_inbound.py` never reads `_running_agents`; the check is `_is_session_running()` (`gateway/run_inbound.py:1200`, def `gateway/run.py:3303`) | H |
| Level 1 "queues the message in `_pending_messages` **and sets an interrupt event**" (`:86`) | `gateway/platforms/base.py:3610-3614` logs "no interrupt, will cascade after current turn" and merges the TEXT event into the pending slot | M |
| "Everything else triggers `running_agent.interrupt()`" (`:88`) | three busy-input modes exist: redirect of the live turn (`gateway/run_inbound.py:576-581`), steer injection (`:633-635`), queue (`:629-631`); only the default mode interrupts (`:595`) | M |
| Session key example `agent:main:telegram:private:123456789` (`:75-78`) | the DM slot is `dm`, not `private` (`gateway/session.py:673`; Telegram normalises `private`→`dm` at `plugins/platforms/telegram/adapter.py:714-716`), and Slack inserts a scope slot (`gateway/session.py:674`) | M |
| "Outgoing deliveries (`gateway/delivery.py`) handle … Direct reply" (`:194-196`) | `DeliveryRouter` is constructed (`gateway/run.py:3386`) but in-tree production callers are cron only (`cron/scheduler_delivery.py:1175`); direct replies ride `gateway/platforms/base.py:3786` | M |
| Platform event chain shows `Adapter.on_message()` (`website/docs/developer-guide/architecture.md:155`) | no `on_message` method exists on the adapter chain; the name appears only as local handler closures inside `plugins/platforms/discord/adapter.py:1236` | M |

### 6.2 `website/docs/developer-guide/agent-loop.md` + `provider-runtime.md` (loop claims)

| Stated in the doc | Runtime truth | Sev |
|---|---|---|
| "Hermes supports **three** API execution modes" (`agent-loop.md:43`) | five accepted explicit modes at `agent/agent_init.py:366-369` (`chat_completions`, `codex_responses`, `anthropic_messages`, `bedrock_converse`, `codex_app_server`) | H |
| default `max_iterations` **500**, configurable via `agent.max_turns` (`agent-loop.md:184`, `agent/AGENTS.md:15`) | the agent default is unbounded (`run_agent.py` constructor default) — see `agent/turn_iteration_prep.py:275` for the grace-call path; the 500 figure is not the runtime default | H |
| subagent cap "default **50**" (`agent-loop.md:185`) | shipped config uses a different value (`cli.py:419` `delegation.max_iterations`) and the live config another | M |
| "No partial response is injected into conversation history" on interrupt (`agent-loop.md:121-124`) | the runtime preserves and records the partial (`agent/turn_api_call.py:164 handle_api_interrupt`; `_INTERRUPT_SCAFFOLD_MARKER` in `agent/conversation_loop.py`) | H |
| fallback activated from "three places … including `turn_recovery.py`" (`provider-runtime.md`) | activation sites are in `agent/turn_api_error.py:311` / `agent/turn_response_check.py` / `agent/chat_completion_helpers.py:2025-2080`, one-way latched at `:2080` | M |
| "`_try_activate_fallback` returns `False` immediately if already activated" (`provider-runtime.md:179`) | no such early return; the guard is positional (latch checked by the caller) | M |
| interception table key `todo`, tools "called from `agent/conversation_loop.py`" (`agent-loop.md:152-156`) | the tool is `todo_list` (legacy alias accepted at `model_tools.py:550-555`) | L |
| "On 401/403, attempt credential refresh" (`agent-loop.md:195`) | 401 only: `agent/turn_recovery.py:302-303` | M |

### 6.3 `website/docs/developer-guide/session-storage.md`, `trajectory-format.md`, `docs/session-lifecycle.md`, `docs/state-db-recovery.md`

| Stated in the doc | Runtime truth | Sev |
|---|---|---|
| "Current schema version: **23**" while the same file lists v29/v30 (`session-storage.md:150`) | `SCHEMA_VERSION = 30` (`hermes_state_common.py:197`); live DB reads 30 | M |
| "up to 15 retries" / `_WRITE_MAX_RETRIES = 15` (`session-storage.md:186`, `:194`) | symbol does not exist anywhere; writes are time-bounded (`hermes_state.py:349`, jitter `:362`) | M |
| DB layout tree of 11 objects (`session-storage.md:14-25`) | live DB has 47 tables (incl. 17 `hosted_room_*`), several created by `SCHEMA_SQL` itself (`hermes_state_common.py:274`, `:404`, `:431`, `:446`, `:462`) | L |
| `sessions.system_prompt` is the prompt store (`session-storage.md:59`) | written NULL; the prompt text lives in `system_prompts(hash, prompt)` keyed by `sessions.system_prompt_hash` (`hermes_state_sessions.py:618`) | M |
| "SQLite, WAL mode" stated unconditionally (`session-storage.md:13`, `:34`) | WAL is negotiated and fallible (`hermes_state_wal.py:147`, `:199`, `:220`) | L |
| FTS triggers presented as invariants (`session-storage.md:141`, `state-db-recovery.md:91-93`) | triggers are dropped when the fail-open path fires (`hermes_state_fts.py:322`); the live DB has 0 triggers with `fts_stale` set | L |
| batch runner writes `batch_001_output.jsonl` (`trajectory-format.md:19`) | code writes `batch_{batch_num}.jsonl` and merges into `trajectories.jsonl` (`batch_runner.py:304`, `:696`) | L |
| "the next-up slot is **overwritten** on repeat sends (burst collapse)" (`session-lifecycle.md:456-458`) | overwriting was the bug; the merge semantics now live at `gateway/platforms/base.py:3613` | M |

### 6.4 Claims that verified correct (coverage)

`gateway/AGENTS.md` "TWO message guards" as a *concept* (both guards exist, at
`gateway/platforms/base.py:3523` and `gateway/run_inbound.py:1176`); `builtin_hooks/` empty;
the facade/sibling layout for `run_agent.py` and `hermes_state.py`; the docstring-level
description of `handle_function_call` in `website/docs/developer-guide/tools-runtime.md`
(its dispatch diagram is incomplete — the function is a re-entrant orchestrator with six
seam types — but nothing it states is false).

## 6. Prepared-but-unapplied fix (D3)

`gateway/AGENTS.md`, "Shape" section, current sentence:

> …authorization in `authz_mixin.py`, adapters in `platforms/<name>.py`
> over `platforms/base.py`. `builtin_hooks/` is the extension point…

Proposed replacement (one sentence, no structural change):

> …authorization in `authz_mixin.py`. Adapters sit over `platforms/base.py`: bundled
> platforms ship as **plugins** (`plugins/platforms/<name>/adapter.py`, `kind: platform`,
> registered through `register_platform`), and `platforms/<name>.py` keeps the remaining
> in-tree adapters (`signal`, `weixin`, `whatsapp_cloud`, `bluebubbles`,
> `msgraph_webhook`, `webhook`, `yuanbao*`, `qqbot/`) plus the adapter base, helpers and
> the API server. `builtin_hooks/` is the extension point…

Not applied in this commit because `AGENTS.md` is protected: the write needs explicit user
approval, and the approval prompt was not answered. Apply it only with that consent.

## 7. Rule

If this map and the runtime disagree, **the runtime wins**: correct the map and the
documentation in the same change, and add a row here with both anchors so the decision
stays auditable.
