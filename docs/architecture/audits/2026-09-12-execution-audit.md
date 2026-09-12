# Execution audit — Hermes Agent request-execution architecture map

- Repository: `NousResearch/hermes-agent`  ·  branch `docs/execution-architecture-map-20260912`
- Baseline commit: `fa24c030a5e48af63eb152a0b3b344a99e991244`  ·  final commit: `27f392e6f8`
- Origin session key: `agent:main:telegram:dm:5611439557:25530`
- Window (UTC): 2026-09-12T03:46:29+00:00 → 2026-09-12T04:14:35+00:00
- Executors: main agent (`deepseek-flash`) + delegated subagents (`deepseek-flash`)

Derived from runtime records only: `state.db` (`sessions`/`messages`/`async_delegations`,
opened read-only) and the subagent live transcripts in `cache/delegation/live/`.
No conversation content, prompts or chain-of-thought are stored here — only work-unit
boundaries, executor identity and tool names.

## 1. Delegated work units (subagent executor)

| # | delegation_id | state | dispatched (UTC) | completed (UTC) | duration (s) | tool calls | tools |
|---|---|---|---|---|---|---|---|
| 1 | `deleg_6409b63b` | completed | 2026-09-12T03:48:24+00:00 | 2026-09-12T04:03:31+00:00 | 907.6 | 113 | terminal×84, execute_code×25, read_file×2, delegate_task×1, write_file×1 |
| 2 | `deleg_fa3eda9d` | completed | 2026-09-12T03:48:24+00:00 | 2026-09-12T03:58:54+00:00 | 630.5 | 87 | terminal×43, read_file×32, patch×6, execute_code×4, delegate_task×1, write_file×1 |
| 3 | `deleg_d4aef69e` | completed | 2026-09-12T03:48:24+00:00 | 2026-09-12T03:56:44+00:00 | 499.8 | 41 | terminal×17, read_file×17, execute_code×4, delegate_task×1, write_file×1, patch×1 |
| 4 | `deleg_dcf2dc0b` | completed | 2026-09-12T03:48:24+00:00 | 2026-09-12T04:00:13+00:00 | 709.3 | 82 | terminal×29, read_file×29, execute_code×12, patch×9, search_files×1, delegate_task×1, write_file×1 |
| 5 | `deleg_25292619` | completed | 2026-09-12T03:48:35+00:00 | 2026-09-12T03:57:12+00:00 | 517.0 | 52 | read_file×19, terminal×18, execute_code×9, patch×3, search_files×1, delegate_task×1, write_file×1 |
| 6 | `deleg_0ae34388` | completed | 2026-09-12T03:48:35+00:00 | 2026-09-12T04:11:51+00:00 | 1396.8 | 45 | terminal×35, read_file×3, delegate_task×3, execute_code×3, write_file×1 |

Peak concurrent delegated lanes: **6**.

Overlapping lane pairs (all overlaps are intentional parallel lanes of the same DAG wave):

| lane A | lane B | overlap (s) |
|---|---|---|
| `deleg_6409b63b` | `deleg_fa3eda9d` | 630.5 |
| `deleg_6409b63b` | `deleg_d4aef69e` | 499.8 |
| `deleg_6409b63b` | `deleg_dcf2dc0b` | 709.3 |
| `deleg_6409b63b` | `deleg_25292619` | 517.0 |
| `deleg_6409b63b` | `deleg_0ae34388` | 896.6 |
| `deleg_fa3eda9d` | `deleg_d4aef69e` | 499.8 |
| `deleg_fa3eda9d` | `deleg_dcf2dc0b` | 630.4 |
| `deleg_fa3eda9d` | `deleg_25292619` | 517.0 |
| `deleg_fa3eda9d` | `deleg_0ae34388` | 619.6 |
| `deleg_d4aef69e` | `deleg_dcf2dc0b` | 499.7 |
| `deleg_d4aef69e` | `deleg_25292619` | 489.0 |
| `deleg_d4aef69e` | `deleg_0ae34388` | 488.9 |
| `deleg_dcf2dc0b` | `deleg_25292619` | 517.0 |
| `deleg_dcf2dc0b` | `deleg_0ae34388` | 698.5 |
| `deleg_25292619` | `deleg_0ae34388` | 516.9 |

## 2. Main-agent work units (turn intervals, reconstructed from `messages`)

A unit is a user turn: from the user message row to the next user message row.

| # | started (UTC) | ended (UTC) | duration (s) | assistant msgs | tool results | tools |
|---|---|---|---|---|---|---|
| 1 | 2026-09-12T03:46:28+00:00 | 2026-09-12T04:14:35+00:00 | 1688.0 | 159 | 209 | terminal×136, patch×43, write_file×12, delegate_task×10, skill_view×3, execute_code×3, search_files×1, read_file×1 |

## 3. Method and caveats

- Regenerate with `python docs/architecture/tools/collect_execution_audit.py`
  (`--origin-session`, `--out-json`, `--out-md`).
- Subagent tool counts come from the live transcript lines that the runtime writes
  (`tool | -> <name>(...)`); arguments and results are discarded by the parser.
- Main-agent turn boundaries come from `messages.timestamp`; the last unit ends at the
  session's last recorded activity, so its duration is a lower bound while the session is live.
- Nested delegations spawned *by* a lane are not listed as separate units; they appear
  inside their parent lane's duration unless the parent recorded them in `async_delegations`.

The final-commit field above names the commit the audit was taken *against*
(`27f392e6f8`, the documentation state it audits). Because this file is committed after that
state, the branch tip additionally contains this audit plus two documentation-only follow-up
commits; none of them touch source files.

---

# Hand-written appendix (not generated)

The sections below are added by the executing agent, not by the generator. Times come from
`messages.timestamp` for this session; every claim is a value read from the tree or from a
command whose output is quoted.

## A1. Main-agent timeline (10-minute buckets, one user turn)

The whole run is a single user turn, so turn-level decomposition says little; these buckets
come from the message rows of the main agent only (subagent rows live in their own sessions).

| Bucket (UTC) | Messages | Tools used |
|---|---|---|
| 03:40–03:49 | 75 | terminal×39, delegate_task×6, skill_view×3, search_files×1 |
| 03:50–03:59 | 110 | terminal×41, patch×9, write_file×8, read_file×1 |
| 04:00–04:09 | 125 | terminal×39, patch×23, write_file×2, execute_code×2 |
| 04:10–04:19 | 61 | terminal×18, patch×11, delegate_task×2, execute_code×1 |

Main agent totals for the run: 159 assistant messages, 209 tool results, 1688 s wall clock.

## A2. Lane fan-out

Six top-level lanes were dispatched at 03:48:24 (four) and 03:48:35 (two), giving a peak of six
concurrent executors — the configured maximum. Two lanes fanned out further
(`delegate_task×1` on four lanes, `×3` on the documentation-audit lane): those grandchildren are
visible in the lane tool counts but have no separate `async_delegations` row for this origin
session, because the table keys on the *owner* session. Lane completion spread: 03:56:44,
03:57:12, 03:58:54, 04:00:13, 04:03:31, 04:11:51 — the last lane (documentation audit) ran 1397 s.

## A3. Verification performed

| Check | Method | Result |
|---|---|---|
| Document anchors | `docs/architecture/tools/verify_anchors.py` at commit `27f392e6f8` | **247 anchors, 0 failures** (34 bare path mentions reported informationally: prose, `HERMES_HOME` runtime paths, placeholders, and stale paths quoted on purpose) |
| Lane artifacts (L1–L5) | independent anchor re-check of the raw artifacts, resolving shorthand basenames | 1,242 anchors: 1,185 resolved to a unique file:line, 57 basename-shorthand (all `base.py` ones inside `gateway/platforms/base.py`'s range), **0 out-of-range, 0 missing files** |
| Lane artifact (L6) | same, on the documentation-audit artifact | 346 anchors: 273 resolved, 72 shorthand, **2 out-of-range**, 1 shorthand that resolves nowhere |
| Spot checks by the executing agent | read the line + grep for the symbol, 16 items drawn from lane claims | 15 confirmed as written; 1 **rejected** — a lane reported `tools-runtime.md`'s dangerous-pattern list as a high-severity safety gap, but `tools/approval_detection.py:198` does define 36 patterns including `SQL DROP` / `SQL DELETE without WHERE` / `SQL TRUNCATE` (the lane's literal-string grep missed the regex spelling). Lane claims were treated as claims, not facts |
| Targeted test run (planes) | `scripts/run_tests.sh` over 10 files covering ingress/loop/tools/state/delivery | 422 passed, 1 failed — `tests/test_hermes_state.py::TestFTS5Search::test_search_projection_skips_context_enrichment_queries`, reproduced standalone (0.74 s) and explained: the test asserts SQL text (`WITH TARGET AS (`) that no longer exists in the state code |
| Website docs tests | `scripts/run_tests.sh tests/website/` before and after the doc edits, plus an A/B run in a pristine checkout | 4 failures, identical in all three runs → pre-existing baseline failures, not caused by these edits |
| Live runtime cross-check | `gateway_state.json` (`code_sha`), read-only `state.db` queries | the running gateway was the pinned commit; schema version, session-key format, message/tool rows, prompt storage and lease rows all matched the source reading (see the map's Appendix A) |

Three known-imperfect things are stated rather than hidden: (1) one lane row misquoted a
documented test count, (2) one lane high-severity finding did not survive re-verification
(recorded in §6.6 of the delta log so the mistake is not repeated), and (3) four tests fail at
this baseline for reasons outside the scope of this work — all recorded in
`docs/architecture/doc-runtime-deltas.md`.

## A4. What the audit does not contain

No conversation content, prompts, model output, tool arguments or results, and no
chain-of-thought. Only work-unit boundaries, executor identity, tool names and counts,
timestamps, and verification outcomes.


