# Memory system maintenance runbook (v2)

Everything needed to keep the Hermes + Hindsight memory system trustworthy over months:
what to run, how often, what the numbers mean, and how to recover. Commands are exact and
copy-pasteable on this box (Windows, git-bash).

## 1. What is monitored and why

The failure this system is designed against is **silence**: on 2026-09-12 the write path had
been dead for 4.5 days while `/health`, the provider status and the gateway all looked fine.
Every signal below exists because one specific silent failure mode was observed.

| Signal | Healthy | Warning | Degraded | Catches |
|---|---|---|---|---|
| `write_path_probe` (dry-run extraction) | facts returned | 0 facts | non-200 | the LLM extraction route rejecting every call (the 2026-09 outage) |
| `bank_activity` (age of last durable write) | < 24 h | ≥ 24 h | ≥ 72 h | retains accepted but never durably written |
| `recent_operations` failure ratio | < 10 % | ≥ 10 % | ≥ 25 % | provider errors, with the window's time range so historical residue is not misread as live |
| `retrieval` | recall returns items, reranker engaged | empty result set | — | embedding/reranker route breakage |
| `coverage` (per day: sessions vs sessions in the bank) | no silent days | a day with ≥3 sessions and 0 documents | — | write outages AND retention-policy gaps |
| `pending_consolidation` (TREND, not one reading) | 0, or falling vs the previous sample | flat-or-rising for ≥3 samples | non-draining for ≥6 samples, or any pending with `last_consolidated_at` ≥ 24 h | consolidation stalling behind the write backlog (a large count right after a backfill is expected and must not be read as a fault) |
| `failed_consolidation` (cumulative counter) | unchanged since the previous sample | it rose since the previous sample | — | a *new* consolidation failure; reading the raw counter warns forever about one historical event |
| `builtin_MEMORY.md` / `USER.md` | < 85 % of budget | ≥ 85 % | — | injection budget exhaustion forcing eviction of durable facts |
| `provider_wiring` | `hindsight` + `local_external` + bank id | missing key | wrong provider | config drift after `hermes update` |

## 2. Cadence

Daily (cheap, no LLM cost):

```bash
python C:/Users/Fwhne/AppData/Local/hermes/scripts/memory-ops/memory_health_check.py \
  --no-probe-write --json C:/Users/Fwhne/AppData/Local/hermes/scripts/memory-ops/health-daily.json
```

Weekly (with the write probe — one small extraction call, and the retrieval harness):

```bash
bash C:/Users/Fwhne/AppData/Local/hermes/scripts/memory-ops/run_memory_regression.sh
```

Interpretation: exit 0 = healthy, 1 = warnings, 2 = degraded. `recent_operations` FAIL with a
window that ends in the past is residue; a window that includes "now" is a live incident.

The `pending_consolidation` check keeps a numeric sample history beside the JSON report
(`pending_consolidation_history.json`, counts and timestamps only — never memory content), so a
single run can say "draining" or "not draining" instead of reporting an alarming number that may
simply be a backfill the worker has not finished digesting. Warnings only start when the queue
stops falling across samples, or when the last consolidation is older than a day.

## 3. Incident playbook — "記憶好像冇再寫入"

```bash
# 1. Is anything being written at all?
curl -s http://127.0.0.1:8888/v1/default/banks/fwh-main/stats        # last_memory_write_at, failed_operations
# 2. What are the most recent operations, and why did they fail?
curl -s "http://127.0.0.1:8888/v1/default/banks/fwh-main/operations?limit=50"
# 3. Does the LLM extraction route work right now (persists nothing)?
curl -s -X POST http://127.0.0.1:8888/v1/default/banks/fwh-main/memories/dry-run-extract \
  -H "Content-Type: application/json" -d '{"content":"probe","context":"incident triage"}'
# 4. Which days have sessions but no memory?
python <ops>/memory_health_check.py --json <ops>/health-now.json   # coverage.per_day
```

Decision table:

* `dry-run-extract` fails with a provider/model error → the LLM route. Check the relay's
  header requirements (2026-09: `x-opencode-session`), the key in Hermes `.env`, and whether
  the venv patch is still present after any reinstall (§4).
* `dry-run-extract` works but `last_memory_write_at` is old → the retain path from Hermes:
  check `agent.log` for the new `Hindsight retain operation … FAILED` warning (plugin ≥ the
  2026-09-12 fix), then the gateway's loaded plugin version.
* Both fine but coverage has silent days → retention policy or session plumbing (see §5).

## 4. Hindsight service operations

State: pinned `hindsight-api-slim[embedded-db]`, venv `C:/Users/Fwhne/hindsight/.venv`, single
Task Scheduler supervisor `Hindsight API Supervisor`, data in pg0 instance `hindsight`
(port 5432), API on 127.0.0.1:8888.

### 4.1 The opencode-go header requirement

`opencode-go` requires `x-opencode-session` on every request (400 `MissingSessionID` without
it). Hermes sends it for its own calls; Hindsight 0.9.0 does **not**, and `llm_default_headers`
is dropped on the OpenAI-compatible route — hence the two-file patch + launcher env
(`HINDSIGHT_API_LLM_DEFAULT_HEADERS`). **0.9.2 honours `default_headers` natively**, so the
durable end state is: upgrade to ≥0.9.2 and delete the patch.

Guard after any reinstall/upgrade: `HINDSIGHT_API_LLM_DEFAULT_HEADERS` must exist in
`scripts/start-hindsight.ps1`, and the daily write probe must pass. A reinstall that wipes the
patch without the header being honoured shows up as `write_path_probe=fail` within 24 h.

### 4.2 Upgrade procedure (learned the hard way — the venv must be QUIET)

`uv pip install` cannot replace files that are open, and on Windows a running API (or any
other process using the venv) holds DLLs open. A failed mid-install leaves the venv broken
(observed: `ModuleNotFoundError: pydantic_core`). Therefore:

1. Rollback points first: `hindsight-admin backup <zip>` (verified in the same run), plus
   `uv pip freeze > requirements-before-<version>.txt`, plus a copy of the `hindsight_api`
   package directory.
2. **Stop the API and make sure no other process uses the venv** (no subagents running out of
   it). The supervisor task will retry and fail while stopped — that is expected.
3. Install with a minimal blast radius: `uv pip install --python <venv python> --no-deps "hindsight-api-slim==<version>"`
   (deps rarely change in a patch release; a missing dep fails loudly at import).
4. Start the API (re-run the supervisor task) and run the gates:
   `python v2/upgrade_gates.py` — version, health, no data loss, recall, write path, sandbox retain.
5. Any gate fails → roll back: restore the package snapshot, `uv pip install -r requirements-before-<version>.txt`,
   restart, re-run the gates.

### 4.3 Controlled restart (30 s, no data loss)

`bash <audit>/restart_hindsight.sh` — soft-kills the API, re-runs the single supervisor task,
polls `/health`, prints the timings, and dumps the tail of the new supervisor log. Do not start
a second instance by hand: the supervisor checks `/health` first and exits as `already-healthy`.

### 4.4 Backups

Daily 03:30 Task Scheduler job keeps 7 daily + 4 weekly zips under
`C:/Users/Fwhne/hindsight/backups/`. A restore **wipes the target schema** — always restore into
a scratch bank when validating, and export-bank before any import.

## 5. Retention-policy maintenance

* Subagent work is retained as **one outcome digest per child** (`kind:delegation-outcome`,
  own document, parent session tag). Never widen this to full child transcripts — see
  `SOURCE_OF_TRUTH.md` §3 for the rationale and the structural enforcement.
* Historical backfill is `memory_backfill.py` (dry-run by default). Real runs must be capped
  (`--limit`, `--max-documents`), tagged `kind:backfill`, resumable via its state file, and
  rolled back by the exact document ids in the run's manifest (never by tag).
* Each run writes a **per-run manifest** (`--manifest`, default
  `<state dir>/manifests/<bank>-<run start>.json`) listing every document id it wrote, so one
  batch can be undone without touching the batches that passed. The state file records doc ids
  per session; the manifest records them per run, which is the granularity a rollback needs.
* De-duplication at injection time is `_dedupe_recall_results` (keep highest-ranked twin).
  Re-measure with the retrieval harness after any recall-path change.

### 5.1 The backfill gate (fixed 2026-09-13 — the old criterion stopped a batch on noise)

Run one batch with `scripts/memory_backfill_batch.py`: it takes a **timestamped** backup, measures
before, backfills one capped batch (writing its manifest), measures after, and judges with
`scripts/compare_retrieval_runs.py`. Exit 0 = passed, 2 = infrastructure/comparability failure,
3 = gate failed.

Criteria, in the order that matters:

| Check | Fails when |
|---|---|
| `recall@3_not_worse` | the known-correct answer leaves the top 3 more often than before (primary) |
| `no_answer_lost` | an answer that was retrievable before is absent from the response now |
| `no_decided_rank_regression` | a question whose answer held its rank by >= `--min-margin` (0.05) moved down |
| `all_questions_answered` | either run has an errored question |
| `dup_within_tolerance` | duplicate-item rate rose by more than 0.02 |

**Near-tie questions never gate.** A question counts as `unstable` when the top1-top2 final-score
gap is < `--min-margin`, because on this bank the flagged reorderings sat 0.001-0.005 apart and
flipped between runs on their own. Unstable questions are listed (with their margins) instead.
The old gate counted the "top-1 superseded" proxy, which fired on 8/10 questions *before* any
backfill: it is now informational only.

Two measurement defects that made the first three batches unjudgeable, both fixed:

* the harness read the question set's `expected` key while the committed set uses `expect`, so
  `recall` was null in every live run — the gate's recall check passed vacuously. The harness
  now accepts both keys and emits flat `recall_at_1/3/10` aliases (which the weekly cron wrapper
  was already reading).
* the batch script read `aggregate["recall_at_1"]` off a shape that only had
  `aggregate["recall"]["recall@1"]`, so its recall numbers were `null` too.

The harness also reports `recall_without_backfill`. **Read it as an attribution proxy, not a
counterfactual**: it recomputes the SAME responses with the backfill-sourced items removed, so it
answers "would the answer still be present in this response if the added documents had not been
inserted?" — it cannot answer "what would retrieval have returned without them", because the
top-k budget, the reranker's input set and the consolidation window would all have differed. Used
that way it is the first thing to read when a gate fails
(`backfill_effect.questions_where_backfill_changed_answer_rank`).

The resume path is **fail-closed**: if the bank's progress cannot be read, or a session's chunking
no longer matches the bank (`chunking_mismatch`), the run writes NOTHING and exits 2 rather than
resuming blind or silently skipping the session. After the writes, the batch runner re-reads
progress from the bank and requires a session called `completed` to be complete there, and every
session to account for the chunks it skipped plus the chunks it submitted — a batch that cannot
prove it closed its own gap exits 3. That check is what makes "the segment is finished" a claim
the tool can back up instead of one it asserts.

### 5.2 A quality gate is not a coverage check (learned 2026-09-13)

Every batch in the first two runs passed its gate while leaving the segment **61 % incomplete**,
and the gate could not have caught it: it measures retrieval quality, not how much was written.

The mechanism: a batch stops at `--max-documents`, which lands mid-session; every chunk already
submitted carries `session:<id>`, and candidate selection is *tag-based coverage*, so a partial
session counts as covered and its tail is never offered again. Measure coverage from the BANK,
never from tags: backfilled documents are `bf-<session>-cNNN-<hash>` with `chunk_count` in
their metadata, so

```bash
# per-session completeness (documents endpoint, no assumption about our own bookkeeping)
curl -s "http://127.0.0.1:8888/v1/default/banks/fwh-main/documents?limit=500"   # walk all pages
#   written indexes  = set of cNNN per session
#   expected         = chunk_count
```

`memory_backfill.py --resume-partial` closes the gap: it re-enters tagged-but-incomplete sessions
and submits only the missing chunk indexes (the ids stay reproducible because chunking is
deterministic), refusing any session whose recomputed chunk count differs from the bank's
(`chunking_mismatch`, logged loudly) so a resume can never duplicate content. Verified on the
throwaway bank: a 2/6 session resumed to 6/6 writing exactly indexes 2-5, with the manifest
recording `resumed: true` and `chunks_skipped_already_in_bank: 2`.

Practical rule: run batches with a document cap AND a coverage check afterwards; treat
"session level complete" and "document level complete" as two different claims.

### 5.3 The golden question set (v3, 48 questions, tiered by STORE)

A question is only a benchmark if the store being measured is the store that is supposed to hold
the answer. The set is therefore split into tiers, and **the headline recall@k is the `core`
tier** — everything else is context:

| tier | n | what it is | scored? |
|---|---|---|---|
| `core` | 13 | bank-retrievable long-term memory (facts/rules that exist only because they were decided or observed), expected token verified selective (≤20 containing items) | **yes — the backfill gate uses this tier only** |
| `environment` | 23 | facts a live tool reads back: port, version, model, path, filename, cadence, code | no — recall smoke |
| `builtin` | 7 | answers live in the injected `MEMORY.md`/`USER.md`, which is always in context and is never retrieved from the bank | no — verified against those files |
| `gap` | 1 | retained in neither store | no — a finding to fix |
| `liveness` | 4 | no expectation; checks the endpoint answers | no |

The live set is **not in this repository** (it is memory-derived: a question plus its expected
token describes the user's machine and preferences). The repo ships
`retrieval_questions.template.json` (synthetic, `"synthetic": true`), and both the harness and the
batch runner **refuse to run on the template** rather than publish meaningless numbers. The live
set lives with the tools that run it:

```
%LOCALAPPDATA%/hermes/scripts/memory-ops/retrieval_questions.json   <- cron jobs read this
<audit workspace>/v2/retrieval_questions.local.json                 <- working copy
```

Validate before measuring (exit 1 when a `core` question is uncovered or a `builtin` one is
missing from the files):

```bash
python scripts/validate_golden_set.py --bank fwh-main \
  --questions "$LOCALAPPDATA/hermes/scripts/memory-ops/retrieval_questions.json" \
  --out v2/golden_set_coverage.json     # --keep-expect for a local run; default redacts tokens
```

Rules for extending it:

* put each question in the tier of the store that holds its answer, and make the token SELECTIVE:
  `STOP` matched 296 items and `manifest` 160, which made every response a hit and measured
  nothing. The validator reports `max_token_items` per question and flags anything above
  `--low-spec-max-items` (20); low-specificity questions are excluded from the gate verdict;
* a token must be on-topic, not merely rare: several "core" questions used to pass on
  `CDP`/`backup`/`reflect` matching unrelated items while their real answer lived only in
  `USER.md` (now tier `builtin`);
* keep `expect` to short tokens, never a quote of memory text;
* re-run the validator and commit its (token-redacted) report whenever the set changes.

Baselines (harness 1.3, 2026-09-13, `artifacts/baseline_v221_tiered.json`):

| scope | n | recall@1 | recall@3 | recall@10 |
|---|---|---|---|---|
| **core (headline)** | 13 | 0.308 | 0.538 | 0.615 |
| core + environment (pre-tier continuity) | 36 | 0.361 | 0.528 | 0.750 |
| environment (smoke) | 23 | 0.391 | 0.522 | 0.826 |
| builtin (bank retrieval; ~0 by design) | 7 | 0.000 | 0.000 | 0.000 |

`artifacts/golden_baseline_v22.json` holds the earlier, pre-tier measurement (44 scored questions,
recall@1 0.45) and is **not** comparable to the tiered numbers.

### 5.4 Nothing memory-derived goes into the public repo, and the check is executable

`fxhsxm/hermes-agent` is a **public** fork. Redacting item text is not enough: the question set,
the expected tokens and the injected `MEMORY.md`/`USER.md` are memory-derived too.

* The harness (1.3) and validator redact by default: item text (`text`, `a_text`, `b_text`,
  `sibling_text`, marker `window`), plus the question text (`query`) and the expected tokens
  (`expected`). `--keep-text` / `--keep-expect` are for local runs only. Ids, tiers, ranks,
  margins, rates and counts stay, so a redacted artifact still verifies every figure in a report.
  (`expected` is replaced by a non-empty placeholder on purpose: consumers use truthiness to tell
  a scored question from a liveness probe, and an empty list would silently unscore everything.)
* Before committing anything under `docs/audits/`, run the gate:

```bash
python scripts/check_artifacts_privacy.py --tree docs/audits \
  --questions "$LOCALAPPDATA/hermes/scripts/memory-ops/retrieval_questions.json" \
  --builtin-file "$LOCALAPPDATA/hermes/memories/MEMORY.md" \
  --builtin-file "$LOCALAPPDATA/hermes/memories/USER.md" \
  [--with-bank-items]        # strongest: treats every bank item as private (slow)
  # add --redact to replace matches with [REDACTED:<fingerprint>] in place
```

  It fails closed (exit 2) if a private source cannot be read, and it prints fingerprints rather
  than the matched string — because printing it would leak what the check is protecting.
* `MEMORY.md` / `USER.md` and their backups stay local under `v2/memory-backup/`.
* Batch result/comparison files are text-free (counts, ranks, document ids) — keep it that way.
* **Git history is not cleaned by editing files.** Old blobs of the pre-redaction artifacts remain
  reachable in the branch's history; removing them needs `git filter-repo` + force-push (which
  changes every SHA) or a fresh fork. Note that a public fork's visibility cannot be changed
  independently of its repository network, so "make the fork private" is not an available option.

## 6. Scheduled monitoring (cron)

`hermes cron` job "memory health" runs the daily check and reports only when the exit code is
non-zero (warnings/degraded), so a healthy system stays quiet. The weekly regression job runs
the unit invariants + the retrieval harness and always reports the numbers.

## 7. Where things live

| Artifact | Path |
|---|---|
| Health check (repo) | `scripts/memory_health_check.py` |
| Regression runner | `scripts/run_memory_regression.sh` |
| Retrieval harness | `scripts/measure_retrieval_quality.py` (v1.1: recall + margins + backfill attribution proxy) |
| Golden set | `docs/audits/memory-system-20260912/v2/retrieval_questions.json` (48 q) + `retrieval_questions_v1_10q.json` (superseded set) |
| Golden-set coverage validator | `scripts/validate_golden_set.py` |
| Before/after gate | `scripts/compare_retrieval_runs.py` (per-question diff, exit 0/2/3) |
| Gated batch runner | `scripts/memory_backfill_batch.py` |
| Backfill tool | `scripts/memory_backfill.py` (writes a per-run document manifest) |
| Plugin invariants | `tests/plugins/memory/test_hindsight_provider.py` |
| Gate invariants | `tests/scripts/test_compare_retrieval_runs.py` |
| Audit + evidence | `docs/audits/memory-system-20260912/` |
| Operational copies (cron uses these) | `C:/Users/Fwhne/AppData/Local/hermes/scripts/memory-ops/` |
| Hindsight launcher/patches | `C:/Users/Fwhne/hindsight/scripts/` + `ops/` |
