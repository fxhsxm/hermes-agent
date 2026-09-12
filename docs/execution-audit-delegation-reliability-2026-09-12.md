# Execution audit — async-delegation reliability fix

What was done on this branch, when, by which executor, with which tools, on which files — and
which units actually overlapped in time. Every timestamp is taken from a runtime record
(§2); anything that no record carries is marked `unavailable` rather than reconstructed.
No conversation text or chain-of-thought is reproduced.

## 1. Run identity

| Field | Value |
|---|---|
| Repository (fix) | `https://github.com/fxhsxm/hermes-agent` — public **fork** of `NousResearch/hermes-agent`, created during this run (the upstream remote is read-only for this account: `git push --dry-run origin` returned 403 at 10:37) |
| Branch | `fix/delegation-reliability-audit-20260912` — no PR opened, `main` not merged |
| Baseline SHA | `fa24c030a5e48af63eb152a0b3b344a99e991244` (HEAD of the live editable install `hermes_agent-0.21.0`) |
| Upstream ref at audit time | `origin/main` = `0f1668ef76a4401d1d799647199c1a8337c1747c` (all three defects verified still present there) |
| Fix worktree | `C:\Users\Fwhne\Documents\HermesOutput\hermes-reliability-audit-20260912\wt` (cut from the baseline SHA; the 23 uncommitted, skills-related files in the live install were never touched — re-checked 10:58 and 11:14) |
| Audit SHA | the branch **tip**, i.e. the commit that contains this file (a file cannot carry its own commit SHA — it is reported with the delivery, together with the push read-back) |
| Orchestration | two Hermes sessions: `20260912_103316_46f77a05` (Telegram DM topic 25476, 10:33–10:59) and `20260912_111132_acf84ede` (topic 25520, 11:11→) |

## 2. Sources of every number below

| Timestamps for | Source | Notes |
|---|---|---|
| Session/lane start + end | `state.db` → `sessions` (`started_at`, `ended_at`), read-only | local time UTC+08:00; one `sessions` row per subagent |
| Lane batch dispatch/completion/delivery | `state.db` → `async_delegations` (`dispatched_at`, `completed_at`, `delivered_at`) | row `deleg_59912c81`, `owner_pid=51476` |
| Main-agent work units | `state.db` → `messages` (`timestamp`, `tool_name`, `tool_calls`) for the two sessions | one row per tool call, second resolution |
| Child tool usage | `async_delegations.event_json` → per-child `tool_trace` / `api_calls` / `duration_seconds` | self-reported by the child runtime |
| Commits | `git log --format=%cI` in the worktree | committer date, UTC+08:00 |
| Test runs | the canonical runner's own stdout, captured to `evidence/*.log` at the time | `scripts/run_tests.sh` |
| Anything else | — | marked `unavailable` |

## 3. Work units, as actually executed

Session A — main agent (`20260912_103316_46f77a05`), every row below is a real tool-activity span:

| Local time | Unit | Executor | Tools | Artifact / evidence |
|---|---|---|---|---|
| 10:33:17–10:36:40 | Recon: located the live install, confirmed editable install + upstream drift, checked push permission, forked | main agent | `terminal`, `read_file`, `gh` | `fork.log`; `git push --dry-run` 403 |
| 10:36:42 → 10:49:57 | **5 read-only audit lanes in ONE batch** (delegation, session/state, browser/computer-use, file-write/state, skills); the delegation and session lanes each spawned their own sub-lanes | 5 `delegate_task` children (depth 2) + 11 grandchild sessions | children: `terminal`, `read_file`, `search_files`, `write_file`, `delegate_task` | `deleg_59912c81` (`state=completed`, `delivery=delivered`); live transcripts `cache/delegation/live/deleg_59912c81/task-{0..4}.log`; `lanes/area-*.md` |
| 10:39:28 | Isolated worktree created from the baseline SHA | main agent | `terminal` (`git worktree add`) | `wt/` |
| 10:42:18–10:43:39 | Baseline regression on the delegation scope (38 files) | main agent | `terminal` + canonical runner | `evidence/baseline-tests.log` — 432 passed, 1 skipped, **1 ⚠ FLAKY** |
| 10:45:45–10:49:10 | Flake diagnosis: mechanism analysis, 10-way-load probe, then a deterministic 3 s-stall reproduction | main agent | `terminal`, `write_file` | `evidence/probe_flake.py`, `probe_flake_forced.py`, `probe_*.log` |
| 10:50:18–10:54:35 | Fix + tests implemented in the worktree | main agent | `patch`, `write_file`, `terminal` | `tools/async_delegation.py`, `tools/delegate_tool_dispatch.py`, 3 test files |
| 10:53:38–10:54:35 | Red-on-base / green-on-fix proof for the 4 new defect tests | main agent | canonical runner | `evidence/fix_tests_red_on_base.txt` (4 failed), `fix_tests_green.txt` (33 passed, 1 skipped) |
| 10:54:54–10:55:30 | Post-fix regression (39 files) + RCA document | main agent | canonical runner, `write_file` | `evidence/postfix-tests.log` — 437 passed, 0 failed, no FLAKY; `docs/rca-async-delegation-lifecycle-2026-09-12.md` |
| 10:55:56 / 10:56:02 | Commits `6502aa6185` (fix) and `2c031cfb7d` (flake test) | main agent | `git` | commit objects |
| 10:56:08 | Push to the fork | main agent | `git push <fork> HEAD:refs/heads/fix/…` | transcript command entry; remote ref read-back matched the local SHA |
| 10:57:00–10:57:13 | First-hand verification of three lane claims (fuzzy-patch acceptance, Windows holder scan, skills cache) | main agent | `write_file`, `patch` | `evidence/fuzzy_probe_target.py` |
| 10:57:53–10:58:34 | Local audit draft, findings report, final live-install check | main agent | `write_file`, `terminal` | workspace files (not in the repo) |
| 10:59:52 | Session A's last recorded message | — | — | `messages` max(timestamp) |

Session B — this session (`20260912_111132_acf84ede`), a fresh context that verified the branch and completed the audit:

| Local time | Unit | Executor | Tools | Artifact / evidence |
|---|---|---|---|---|
| 11:11:31–11:12:37 | Located the existing branch/worktree/fork, read the committed diff, re-derived the timeline | main agent | `terminal`, `read_file`, `execute_code` | this file, §3 |
| 11:13:09–11:13:24 | **Independent re-run of red-on-base**: baseline source checked out temporarily, 4 tests failed, files restored (tree clean) | main agent | canonical runner | 4 failed on base (this audit) |
| 11:13:24–11:14:12 | Whole delegation scope re-run on the fix tree (background process, checked at 11:14:12) | main agent | canonical runner | 39 files / 437 passed / 0 failed (`evidence/verify-final-scope.log`) |
| 11:13:33–11:13:55 | Timeline reconstruction script from `state.db` | main agent | `write_file`, `execute_code` | `evidence/runtime_timeline.py`, `runtime-timeline.json` |
| 11:14:07–11:15:16 | Found and reproduced a **residual hole in §1's fix**: the compensating DELETE re-enters the failing state.db path | main agent | `read_file`, `search_files`, `write_file`, `terminal` | `evidence/probe_compensation_failure.py` → `***RAISED OUT OF THE DISPATCH***` on both arms |
| 11:15:28–11:16:27 | Hardening (`_discard_durable_dispatch`) + regression test in both rejection arms | main agent | `patch`, `terminal` | `tools/async_delegation.py`, `tests/tools/test_async_delegation.py` |
| 11:16:34–11:16:59 | Red-on-pre-hardening / green proof for the new test, then the focused file set | main agent | canonical runner | 1 failed before, 34 passed after (`evidence/verify-hardening-green.log`) |
| 11:17:05–11:17:37 | RCA doc updated (line references refreshed after the insertion, new §1b) | main agent | `patch` | `docs/rca-async-delegation-lifecycle-2026-09-12.md` |
| 11:17:50–11:18:03 | Data collection for this audit (per-child tool counts, session/overlap data) | main agent | `execute_code` | §4, §5 |
| 11:18–… | This audit written; hardening committed; branch pushed and read back | main agent | `write_file`, `git`, `gh` | commit objects, remote ref |

## 4. Executors and tools

* **Main agent, session A** (`20260912_103316_46f77a05`): `terminal` 246, `read_file` 48, `patch` 30,
  `write_file` 18, `tool_call` 8, `process_manage` 8, `delegate_task` 2 — no browser, no OCR, no
  network fetch beyond `git`/`gh`.
* **Main agent, this session** (`20260912_111132_acf84ede`, counts at 11:18): `terminal` 76,
  `read_file` 22, `patch` 20, `execute_code` 17, `write_file` 6, `search_files` 6, `skill_view` 2.
* **Lanes (children)**: read-only against the audited repo; briefs forbade git mutations. Aggregate
  tool usage across the 5 children: `terminal` 121, `read_file` 81, `search_files` 27,
  `write_file` 8 (their own report files only), `delegate_task` 7 (which produced the 11 grandchild
  sessions). 159 API calls total, 796.0 s of batch wall time.
* **Models**: `deepseek-flash` for the main agent and every child (from `sessions.model`).
* **Test execution**: `scripts/run_tests.sh` (canonical runner, per-file subprocess isolation) with
  `HERMES_PYTHON=<live install venv>` inside the worktree, so the worktree's `tools/` shadows the
  editable install.

## 5. Concurrency — did the work units overlap?

* **Yes, heavily, by design.** The 5 top-level lanes were dispatched in ONE batch at **10:36:42**
  and finished between **10:43:11** and **10:49:57** (`async_delegations` row + one `sessions` row
  per lane). They overlapped each other for their whole life.
* **Two-level fan-out:** 11 grandchild sessions started **10:37:04–10:38:04** and ended
  **10:40:36–10:45:39**, i.e. concurrently with the parent lanes that spawned them and with the
  other lanes' grandchildren. Peak concurrency: 16 subagent sessions + the main agent.
* **The main agent worked concurrently with the lanes** rather than waiting for them: it created the
  worktree at 10:39:28, ran the baseline regression 10:42:18–10:43:39, and ran the flake probes
  10:45:45–10:49:10 — all inside the lanes' 10:36:42–10:49:57 window. The flake evidence was
  therefore collected under deliberate load (10 parallel probes), which is why the runner's
  ⚠ FLAKY classification is treated as a real bug rather than noise.
* **Serial, non-overlapping:** apply-fix → red/green proof → post-fix regression → commit → push
  (10:50:18 → 10:56:08) all ran on the single main-agent thread.
* Session B did not overlap session A: it started 11:11:31, after A's last record (10:59:52), and
  re-verified rather than re-ran the earlier work; its test runs (11:13, 11:16) are the only
  compute-heavy units in it.

## 6. Files changed on the branch

Exact `git diff --stat` against the baseline SHA (the audit file itself is added by the last commit,
so it is not in this table):

```
 docs/rca-async-delegation-lifecycle-2026-09-12.md   +146        RCA: the 3 defects, §1b, refreshed line refs
 tests/tools/test_async_delegation.py                +172        3 invariant tests (2 defects + cleanup hardening)
 tests/tools/test_delegate_inline_fallback.py        +130        capacity + registration-failure fallback arms
 tests/tools/test_delegate_timeout_cleanup.py        +113/-15    de-flaked teardown test (release-driven, no stopwatch)
 tools/async_delegation.py                           +68/-11     guarded registration, live-state prune predicate,
                                                                _finalize "record gone" warning, _discard_durable_dispatch
 tools/delegate_tool_dispatch.py                     +27/-5      re-attach children on the inline fallback, real reason
 6 files changed, 625 insertions(+), 31 deletions(-)
```

## 7. Verification and test results

| # | Check | Result | Record |
|---|---|---|---|
| 1 | Baseline scope regression, pre-fix tree (38 files) | 432 passed, 1 skipped, **1 ⚠ FLAKY** | `evidence/baseline-tests.log` (10:42–10:43) |
| 2 | New defect tests on the pre-fix tree | **4 failed** (red) | `evidence/fix_tests_red_on_base.txt` (10:53) |
| 3 | Fix tree, focused files | 33 passed, 1 skipped | `evidence/fix_tests_green.txt` (10:54) |
| 4 | Post-fix scope regression (39 files) | **437 passed, 0 failed, 1 skipped, no FLAKY** | `evidence/postfix-tests.log` (10:54–10:55) |
| 5 | Red-on-base **re-run by session B** on the same tests | 4 failed — reproduces | 11:13 (this audit) |
| 6 | Scope regression **re-run by session B** on the fix tree | 39 files, 437 passed, 0 failed, 1 skipped, 32.5 s | `evidence/verify-final-scope.log` (11:13) |
| 7 | New hardening test on the pre-hardening tree | 1 failed (`RuntimeError: database is locked` escaping the dispatch) | 11:16 |
| 8 | New hardening test + file set after the change | 34 passed, 1 skipped | `evidence/verify-hardening-green.log` (11:16) |
| 9 | Scope regression re-run on the final tree | 39 files, **438 passed**, 0 failed, 1 skipped, 31.7 s | `evidence/verify-final-scope2.log` (11:19) |
| 10 | Live install untouched | 23 modified files (unchanged set), HEAD = baseline SHA | 10:58 and 11:14 |

Work performed and files written outside the repository (`evidence/`, `lanes/`) stays local; the
committed RCA + this audit carry the conclusions that need to survive.

## 8. Not available — marked, not reconstructed

* **Push timestamps as a first-class runtime record**: Hermes and git both keep no push event;
  GitHub exposes no push time via the API. The push **commands** and their local times are in the
  session transcript (`messages.tool_calls`), and the resulting remote ref was read back to confirm
  the SHA — that is all the evidence there is.
* **Cost**: `event_json.results[*].cost_usd` is `0.0` with `cost_status: unknown` for every child;
  real spend per work unit is `unavailable`.
* **Wall-clock of the audit's own final commit/push**: by construction it happens after this file is
  written; it is reported with the delivery (as §1's audit SHA) rather than inside it.
* **Sub-agent prompt text / conversation**: deliberately excluded (no transcript, no CoT).
* **The earlier local-only draft** `AUDIT-execution.md` in the run workspace listed the push at
  11:02–11:04 and verification at 11:04–11:20. No runtime record supports those times (the commits
  are 10:55:56/10:56:02, the push command is 10:56:08, that session's last record is 10:59:52, and
  the draft file itself was written 10:57:53). This committed audit supersedes it.

## 9. Reproduce

```bash
# branch
git fetch https://github.com/fxhsxm/hermes-agent.git fix/delegation-reliability-audit-20260912
git worktree add /tmp/wt FETCH_HEAD        # or: git checkout -b audit FETCH_HEAD
cd /tmp/wt

# the 4 defect tests, red on the baseline commit and green on this branch
git checkout fa24c030a5e48af63eb152a0b3b344a99e991244 -- tools/async_delegation.py tools/delegate_tool_dispatch.py
scripts/run_tests.sh tests/tools/test_async_delegation.py tests/tools/test_delegate_inline_fallback.py \
  -k "persist_failure_rejects or completed_cap_never_evicts or reattaches_children or registration_failure_fallback"
git checkout HEAD -- tools/async_delegation.py tools/delegate_tool_dispatch.py

# the whole delegation scope on the fix tree
scripts/run_tests.sh tests/tools/test_async_delegation.py tests/tools/test_delegate_inline_fallback.py \
  tests/tools/test_delegate_timeout_cleanup.py tests/tools/test_delegate.py tests/tools/test_subagent_steer.py
```
