# Execution audit — Hermes + Hindsight memory audit (2026-09-12)

One agent session (`hermes-main`, Telegram, model `deepseek-flash` via `opencode-go`), one
worktree, **no subagents** and no parallel agent lanes, so there is no delegated work to reconcile
and no cross-lane overlap. All times are local (UTC+08:00) and come either from a recorded log
line, an artifact timestamp, or the tool result that produced the work; where a window is derived
from the artifact rather than logged, the evidence column says so. Total session span
12:35:37 → 13:0x (≈30 min of wall clock, of which ≈5 min ran concurrently as noted below).

## Work units

| # | Start | End | Work unit | Evidence |
|---|---|---|---|---|
| U0 | 12:35:37 | 12:35:44 | Scope lock: baseline SHA pinned, NEW worktree `HermesOutput/hermes-memory-audit-20260912/wt`, branch `audit/memory-system-20260912`; prior audit branches **not** read or reused | `git rev-parse` == `fa24c030a5…`; worktree list |
| U1 | 12:35:44 | 12:38:10 | Runtime recon: Hermes version/gateway/scheduler, Hindsight `/health` `/version` `/banks`, bank stats, provider wiring, built-in stores | tool result timestamps |
| U2 | 12:38:10 | 12:41:20 | Failure triage: operations API paginated (limit ≤ 100), failures grouped by day / task type / error class | ops window 73/73 failed; 332 failed all-time |
| U3 | 12:39:40 | 12:41:00 | Root cause: direct opencode-go probes (no header vs `x-opencode-session`) + source read of Hindsight 0.9.0 `llm_wrapper` / `openai_compatible_llm` | probe 400 → 200; source lines cited in REPORT §3.1 |
| U3b | 12:40:20 | 12:40:40 | Upstream check: fetched `vectorize-io/hindsight` `main` provider source | `cache_affinity.apply_opencode_session` present upstream |
| U7 | 12:41:27 | 12:41:50 | Safety baseline: bank snapshot + `hindsight-admin backup` (26 MB, 101 441 rows / 20 tables) | `baseline_snapshot_before_fix.json`, `backups/audit-prefix-20260912.zip` |
| U5 | 12:41:45 | 12:42:00 | Read-path evaluation: 10 fixed questions (zh-Hant / Cantonese / English) against production, read-only | `recall_eval.json` |
| U6 | 12:42:20 | 12:44:00 | Coverage analysis: memory index vs `state.db` sessions, tag-based coverage, per-day histogram | `coverage.json`, `memories_index.json` |
| U8 | 12:44:40 | 12:45:57 | Patch authored + applied to the Hindsight venv (2 files / 4 hunks, `.orig` backups, generated diff); launcher env line with `.bak` | `hindsight-opencode-header.patch`, `apply_opencode_header_patch.py` |
| U9 | 12:45:57 | 12:46:20 | Pre-restart client-side validation of the patched provider (headers vs no headers, JSON extraction) | `validate_patched_provider.py` |
| U11a | 12:45:20 | 12:45:30 | Pre-fix production probe `POST /memories/dry-run-extract` (persists nothing) | `dryrun_prefix.json` → HTTP 500 |
| U10 | 12:46:30 | 12:46:59 | Controlled restart: soft kill → force kill after 10 s → re-run the single Task Scheduler supervisor → bound-poll `/health` | `restart_run.log` (30 s downtime, healthy) |
| U11b | 12:47:00 | 12:47:20 | Post-fix probe + integrity verification | `dryrun_postfix.json` → HTTP 200; stats identical |
| U12 | 12:47:20 | 12:52:00 | Sandbox E2E lifecycle on the throwaway bank: create → retain → update → recall → reflect → consolidate | `e2e_sandbox.json`, `e2e_sandbox.log` |
| U13 | 12:48:00 | 12:52:30 | Hermes repo fix: failed-retain surfacing in the plugin + 3 invariant tests, red-on-base proof, whole file green | worktree diff; `run_tests.sh` output |
| U14 | 12:52:06 | 12:52:20 | Hermes plugin write smoke: real provider class → writer → client → sandbox bank | `hermes_plugin_write_smoke.py` |
| U15 | 12:53:00 | 12:56:30 | Health-check tool authored and exercised against production (fail / warn / pass paths) | `scripts/memory_health_check.py`, `healthcheck_postfix.json` |
| U16 | 12:56:30 | 13:05:00 | Report, execution audit and sanitised evidence written | `REPORT.md`, `EXECUTION_AUDIT.md`, `artifacts/evidence.json` |
| U17 | 13:05:00 | — | Commit + push to the fork; final SHA recorded in the delivery reply | `git log`, `git push` |

## Concurrency and overlap

* **One intentional overlap:** U12 (sandbox E2E, backend process, 12:47:20–12:52:00) ran in the
  background *while* U13 (repo fix + tests, 12:48:00–12:52:30) proceeded in the foreground. They
  touch different systems (Hindsight bank vs repo checkout) and share no state, so the overlap is
  safe; it shortened total wall clock by ≈4 min.
* Everything else is strictly sequential, and the two production-touching units are ordered on
  purpose: U7 (backup) → U8/U9 (patch + client validation) → U10 (restart) → U11b (verify) →
  U12/U14 (write tests) — nothing touched the live bank before a verified backup and a pre-validated
  patch, and the single production write-path action (the restart) lasted 30 s.
* Read-only units (U1, U2, U5, U6, U11a) were run against the live system and made no writes.
  Production memory writes: **none** (all write tests used `fwh-audit-sandbox`; the dry-run endpoint
  does not persist).

## Executor / tooling

| Executor | Tools | Scope |
|---|---|---|
| `hermes-main` | `terminal` (bash/MSYS), `execute_code` (Python: httpx, sqlite3, json), `patch`/`write_file`, `skill_view`, `web_extract` | all units |
| Hindsight admin CLI | `hindsight-admin backup` | U7 |
| Windows Task Scheduler | `schtasks /run` (single supervisor) | U10 |
| Test runner | `scripts/run_tests.sh` (per-file subprocess isolation) | U13 |
| Human `Fwh` | request, scope and constraints | — |

## Failure of process that is worth recording

* One patch attempt failed mid-flight (a `start-hindsight.ps1` anchor mismatch, 12:45:37, exit 1)
  and the script aborted **without writing** — the file was left untouched and re-patched with a
  line-based anchor one minute later. Recorded here because a silent partial write would have been
  the dangerous outcome; the script's fail-closed behaviour is the reason it was not.
* Two harness bugs in my own test scripts were found and corrected before any conclusion was drawn
  (duplicate `document_id` per retain batch; an off-by-one when unpacking `state.db` rows). Both are
  visible in the session logs; neither affected a reported number.
