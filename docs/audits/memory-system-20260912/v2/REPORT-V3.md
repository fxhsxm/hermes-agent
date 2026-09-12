# Memory System v2.1 — measurement fix, corrected baseline, backfill resumed (2026-09-13)

Follow-on to `REPORT-V2.md`. That report closed with a backfill that had **stopped itself on a
false alarm**: batch 3 failed a gate whose criterion was measurement noise. This report records
what the real defect was, the corrected criterion, the re-measured baseline, the resumed
backfill, and one live-service finding. Runtime evidence only; no memory content, credentials
or conversation text is reproduced.

Branch `audit/memory-system-20260912`, baseline commit for the original work
`fa24c030a5e48af63eb152a0b3b344a99e991244`.

---

## 1. The batch-3 stop was a defect in the measurement, not in the memory

Three separate defects, all in the measuring instruments. None of them in the bank.

| # | Defect | Effect | Fix |
|---|---|---|---|
| 1 | The harness matched the question set's `expected` key; the committed set uses `expect` | `recall` was `null` in **every live run**, so the gate's `recall@3_not_worse` check compared `None` to `None` and passed **vacuously** for batches 1-3 | harness 1.1 accepts both keys; emits flat `recall_at_1/3/10` aliases (the weekly cron wrapper was already reading those names and printing `None`) |
| 2 | The batch script read `aggregate["recall_at_1"]`, but the aggregate only had `aggregate["recall"]["recall@1"]` | same vacuous pass, one layer up | replaced by `scripts/compare_retrieval_runs.py`, which reads the documented shape |
| 3 | The pass/fail criterion was the "top-1 superseded" PROXY count | fired on **8 of 10 questions before any backfill ran**, and the orderings it flagged were decided by 0.004-0.005 score gaps; batch 3 "failed" only because that count went 8 -> 9 while every other signal improved | demoted to informational (with the decided-subset count reported); the gate now uses the rank of the known-correct answer plus score margins |

Side by side, same bank, same question set — what the old gate could see and what the new one sees:

| Signal | Old gate (batches 1-3 artifacts) | Corrected (this report) |
|---|---|---|
| recall@1 (expect questions) | **null** in all six measurements | 4/8 = 0.50 |
| recall@3 | **null** | 5/8 = 0.625 |
| recall@10 | not reported | 7/8 = 0.875 |
| duplicate-item rate | 0.2162 -> 0.2174 (batch 3) | 0.2439 (same bank state, wider response sample) |
| stale proxy (strict) | 8 -> 9: the only reason batch 3 stopped | informational: 8/10, of which 1 is a decided question |
| judgeable questions | all 10 treated as judgeable | 1 decided, 9 unstable near-ties |

`REPORT-V2.md` §4 quotes *"recall@1 6/10, recall@3 8/10"*. That number is **not reproducible
from any committed artifact**: the archived live runs (`retrieval_quality_v2.json`,
`retrieval_post_restart.json`, the batch gates) all have an empty recall block because of
defect 1, so it can only have come from an ad-hoc computation that was never archived. The
reproducible numbers are the ones in this report, produced by the committed harness from the
committed question set (`v2/artifacts/baseline_v3.json`).

**Why this matters beyond bookkeeping:** the property that had been "verified" for three
production batches — *recall did not get worse* — was never actually checked. The new gate
measures it, and the corrected baseline above is the first real measurement of it.

## 2. The corrected criterion (what may and may not stop a batch)

Integrated in `scripts/compare_retrieval_runs.py` (per-question diff, exit 0 pass / 2 not
comparable / 3 gate failed) and documented in `MAINTENANCE.md` §5.1:

| Check | Fails when |
|---|---|
| `recall@3_not_worse` (primary) | the known-correct answer leaves the top 3 more often than before |
| `no_answer_lost` | an answer that was retrievable before is absent from the response now |
| `no_decided_rank_regression` | a question whose answer held its rank by >= `--min-margin` (0.05) moved down |
| `all_questions_answered` | either run has an errored question |
| `dup_within_tolerance` | duplicate-item rate rose by more than 0.02 |

Everything else is **reported, never gated**: recall@1 (one near-tie flip moves it), rank
changes on near-tie questions, the staleness proxy. A question is `unstable` when the top1-top2
final-score gap is below `--min-margin`; the answers' own holding margins are reported too
(`expect_answer_gap`), because the answer can sit inside a decided top-1 while ranks 2/3/4 are
a tie.

Measured margins, live bank (baseline run):

| question | top1-top2 margin | class | rank of the known-correct answer |
|---|---|---|---|
| q01-txt-encoding | 0.0484 | unstable | 1 |
| q02-hindsight-port | 0.0086 | unstable | 1 |
| q03-pg0-port | 0.0433 | unstable | 2 |
| q04-reranker | 0.0166 | unstable | 1 |
| q05-worker-id | 0.0063 | unstable | absent (coverage - see §3) |
| q06-delivery-dir | 0.0040 | unstable | 8 |
| q07-delegation-policy | 0.0677 | decided | 6 |
| q08-hindsight-llm | 0.0469 | unstable | 1 |
| q09/q10 (liveness, no expect list) | 0.0470 / 0.0296 | not gated | - |

The gate's logic is pinned by `tests/scripts/test_compare_retrieval_runs.py` (7 tests, passing):
near-tie flip excluded, decided rank regression named and failed, lost answer failed, liveness
question never gated, old harness output not comparable (never a silent pass), duplicate
tolerance, flips listed.

## 3. Corrected baseline for `fwh-main` (with the 120 backfilled documents in place)

`v2/artifacts/baseline_v3.json`, harness 1.1, `--min-margin 0.05`, 10 questions, all answered:

* **recall@1 4/8, recall@3 5/8, recall@10 7/8** (8 questions carry an `expect` list; q09/q10 are
  liveness checks with no ground truth by design)
* duplicate-item rate **0.2439** (90 of 369 returned items sit in a duplicate pair, every pair
  observation-vs-raw-twin), median latency 0.97 s, reranker engaged on 10/10 questions
* 1 decided question, 9 unstable; the answer's own position is a near-tie in 7 of the 8 expect
  questions

**The one miss is coverage, not retrieval.** q05 asks for the Hindsight background worker id.
A scan of all 6,267 memory items in the bank (13 pages of `/memories/list`, matching on that
single token only) finds **zero** items containing the value: the fact was never retained. A
conclusive answer to "which query failed to find it" would be wrong - there is nothing to find.

**Variance probe (repeat=3, `v2/artifacts/baseline_v3_var3.json`)**: identical ranks and
identical margins in all three runs on 9/10 questions (two questions returned 39 vs 43 items
with the same rank and margin). So the ranking is *deterministic for a given bank state*; the
0.004-gap flips reported earlier are caused by **additions** perturbing scores in the third
decimal, not by run-to-run randomness. That is why the fix is a margin threshold, not repeats.

## 4. Attribution: what the 120 backfilled documents actually did to retrieval

New in harness 1.1, and the first thing to read when a gate fails: `recall_without_backfill`
recomputes the **same** responses with every backfill-sourced item removed
(`metadata.source == "backfill"` or tag `kind:backfill`) and the remaining items re-ranked. No
deletion, no rollback, an in-band A/B on the live ranking.

Result on the corrected baseline:

| | with backfill items | without them |
|---|---|---|
| recall@1 | 4/8 | 4/8 |
| recall@3 | 5/8 | 5/8 |
| recall@10 | 7/8 | **6/8** |

* 29 of the returned items across the 10 responses came from backfill documents; **the top-1
  item was never a backfilled one** in any question (`questions_top1_is_backfilled: []`).
* `q07-delegation-policy`: rank 6 with them, **absent without them** - the answer is reachable
  *because* of the backfill.
* `q06-delivery-dir`: rank 8 with them, rank 7 without them - a **one-position** move inside a
  question whose margin is 0.0040, i.e. the near-tie the old gate's 8 -> 9 proxy count was
  reading as a regression.

So: the batch-3 stop was a false alarm **with numbers behind it**, the 120 documents are not
merely "unproven harmful" but measurable neutral-to-helpful, and the decision not to roll them
back was right. What remains true from `batch3_regression_analysis.json` is the underlying
property: the bank has no supersession, so old and new facts compete on semantic score alone and
near-ties are decided by additions. `reflect` remains the route for decision-grade questions
(`SOURCE_OF_TRUTH.md` §4).

## 5. Rollback granularity and the run protocol (both were gaps)

* **Per-run document manifest** (`memory_backfill.py --manifest`): every run now writes
  `<state dir>/manifests/<bank>-<run start>.json` with the exact document ids it wrote, per
  session and as a flat list, plus the rollback recipe. The state file records doc ids per
  *session*; the manifest records them per *run*, which is the granularity undoing one batch
  needs. Rollback by the `kind:backfill` tag would delete the batches that already passed, so
  the manifest points at `DELETE /v1/default/banks/<bank>/documents/<document_id>`.
  (Aside: `rollback_batch3_ids.json` from the previous session is an **empty** id list - a
  rollback artifact that would have done nothing. The manifest replaces that pattern.)
* Rehearsed against the throwaway bank `fwh-audit-sandbox`: 1 document written, manifest
  written with its id, then a real `DELETE` by that id -> HTTP 200 and a subsequent GET 404,
  sandbox document count back to its pre-run value. The rollback path is exercised, not just
  documented.
* **Timestamped backups** (`memory_backfill_batch.py`): `<bank>-<UTCstamp>.zip`. The old fixed
  name `backup-before-batch1.zip` was overwritten by every later run, leaving only the newest
  rollback point. A batch now refuses to proceed if its backup is missing or implausibly small
  (< 5 MB).
* One command per batch, in this order and no other: timestamped backup -> measure before ->
  backfill one capped batch (with its manifest) -> measure after -> gate -> write
  `backfill_batch<N>_result.json`. Driven by `scripts/run_backfill_batches.py`, which stops at
  the first non-zero exit and never rolls anything back on its own.

## 6. Live-service finding: one API listener, not two (no double-open)

Reported open item: two `hindsight_api.main --host 127.0.0.1 --port 8888` processes (PID 3156,
venv python; PID 33904, uv python) - double-open would duplicate writes and consolidation.

**Answer: no double listener; nothing to fix, nothing killed.** Evidence in
`v2/artifacts/listener_check.json`:

* `netstat`: exactly **one** LISTENING socket on `127.0.0.1:8888` (PID 33904). PID 3156 is its
  direct parent: the same venv-launcher -> real-interpreter pair Hermes itself uses for
  `hermes serve` (observed live for the gateway: 3172 -> 30728), not a second server.
* The pg0 embedded Postgres is a **separate process tree**: `postgres.exe` (PID 2192, parent
  `cmd.exe` 2152) from `C:/Users/Fwhne/.pg0/installation/18.1.0/bin/postgres.exe`,
  `-D C:/Users/Fwhne/.pg0/instances/hindsight/data -p 5432`, with 4 ESTABLISHED connections from
  the API. It is not one of the two python PIDs.
* Neither python PID needed to be killed to establish this; the check is read-only.

## 7. Repo/deployment divergence found and closed

The v2 plugin implementation (failed-retain warning, outcome-only delegation digests, recall
de-duplication) was live in production and present in this worktree, but **never committed on
this branch**: `git log HEAD -- plugins/memory/hindsight/__init__.py` ended at the
failed-retain commit, while the branch's own test file already imported
`_build_delegation_digest` / `_dedupe_recall_results`. HEAD was internally inconsistent - its
tests could not import the module under test. Verified before committing:

* against HEAD's plugin file: `ImportError: cannot import name '_build_delegation_digest'`,
  0 tests run, exit 1
* against the worktree file: **97 passed / 0 failed / 1 skipped**
* the file is byte-identical (sha256 `1c383c99ecaa...`) to the plugin the running gateway loads

So the deployment, not the branch, was the only copy of that code. It is now committed
(no behaviour change - the same bytes production already executes). The stash/pop used to test
HEAD's version was verified byte-for-byte afterwards, and a copy is kept at
`v2/plugin-worktree-copy.py`.

## 8. Backfill resumed under the corrected gate

Scope: the incident segment only, `2026-09-08..09-12`, oldest first, 6 sessions / 40 documents
per batch. Remaining before the resume: **26 uncovered sessions / 729 documents / ~3.2 M
estimated tokens** (`v2/artifacts/backfill_remaining_dryrun.json`) - lower than the earlier
estimate because the first three batches (120 documents) were already written.

| batch | docs | ops failed | bank documents | recall@1 | recall@3 | duplicate rate | flipped (rank changed) | gate |
|---|---|---|---|---|---|---|---|---|
| 4 | 40 | 0 | 295 -> 335 | 0.5 -> 0.5 | 0.625 -> 0.75 | 0.2466 -> 0.2324 | q07-delegation-policy | PASS |
| 5 | 31 | 0 | 335 -> 366 | 0.5 -> 0.5 | 0.75 -> 0.75 | 0.2324 -> 0.2334 | - | PASS |
| 6 | 40 | 0 | 366 -> 406 | 0.5 -> 0.5 | 0.75 -> 0.75 | 0.2328 -> 0.2328 | - | PASS |
| 7 | 40 | 0 | 406 -> 446 | 0.5 -> 0.5 | 0.75 -> 0.75 | 0.2334 -> 0.2480 | - | PASS |
| 8 | 40 | 0 | 446 -> 486 | 0.5 -> 0.5 | 0.75 -> 0.75 | 0.2480 -> 0.2480 | - | PASS |
| 9 | 32 | 0 | 486 -> 518 | 0.5 -> 0.5 | 0.75 -> 0.75 | 0.2453 -> 0.2406 | - | PASS |
| 10 | 40 | 0 | 518 -> 558 | 0.5 -> 0.5 | 0.75 -> 0.75 | 0.2421 -> 0.2421 | - | PASS |
| 11 | 2 | 0 | 558 -> 560 | 0.5 -> 0.5 | 0.75 -> 0.75 | 0.2474 -> 0.2474 | - | PASS |
| 12 | 0 | 0 | 560 -> 560 | - | - | - | - | n/a (segment exhausted) |

Total for the first phase: 8 batches, **265 documents**, every gate PASS.

Every batch passed: **265 documents** written, **0 failed operations**, bank documents
295 -> 560, and recall@1/recall@3 unchanged from batch 5 onwards (`q07` moved into the top 3 in
batch 4, which is why recall@3 reads 0.625 -> 0.75 for that batch only). The single rank change
in eight batches was an `UNSTABLE_FLIP` on a 0.004-gap question - exactly the class of change the
old criterion would have reported as a regression. Batch 12 wrote nothing because no uncovered
session was left in range: segment D is exhausted at **session** granularity.

### 8.1 The gate stopped a batch correctly, and the segment is NOT complete at document level

`v2/artifacts/backfill_completeness.json`, measured from the bank itself (every backfilled
document carries `chunk_index` / `chunk_count` in its metadata, so completeness needs no
assumption about the tool's bookkeeping):

| | value |
|---|---|
| backfill sessions present in the bank | 37 |
| documents written | 387 |
| documents planned (`chunk_count` sum) | 1,003 |
| **document-level completeness** | **38.6 %** |
| sessions complete | 26 |
| sessions partial | 11 (**616 documents missing**) |

The worst: `…205955_fdcab8f7` 8/267, `…050243_7e17c9e7` 39/227, `…195429_302d77d7` 32/111.

**Mechanism (a real defect, not a measurement artefact):** a batch stops when it reaches
`--max-documents`, which lands *mid-session*. That session is recorded `failed` in the state
file, but every chunk already submitted carried its `session:<id>` tag, and the tool selects
candidates by *tag-based coverage* - so the partially written session immediately counts as
"covered" and is never offered as a candidate again. `state` says `failed`; selection never
looks. Result: the tail of every cap-stopped session is orphaned, and the same is true of the
earlier 12-document production-proof session (`…114629_a77d1329`, 12/19).

Consequences to be explicit about: the 120 documents from batches 1-3 were judged on retrieval
quality (that judgement stands - see §4), but "the incident segment is backfilled" was never true
at document granularity, in either the old or the new runs. The gate could not have caught this:
it measures retrieval quality, not coverage.

Fixing it does not need a new gate, just chunk-level resume:
`--resume-partial` should treat a session with a tag AND missing `chunk_index` values as a
candidate (the missing indexes are derivable from the run manifests, or from the bank's own
`chunk_count` metadata), so a batch finishes tails instead of opening new sessions. Estimated
cost of the remaining 616 documents at the measured ~4.4 k tokens/document: **~2.7 M tokens**.
(Executed in §8.2: 606 documents written, 16 batches, every gate PASS, completeness 100 %.)

### 8.2 Closing the tails (`--resume-partial`)

Chunk-level resume implemented in `memory_backfill.py` (`--resume-partial`, plumbed through
`memory_backfill_batch.py` and `run_backfill_batches.py`): a tagged-but-incomplete session is
re-entered and only the missing chunk indexes are submitted. Chunking is deterministic, so the
document ids of the missing chunks are the same ones the first run would have produced, and a
session whose recomputed chunk count disagrees with the bank's is refused (`chunking_mismatch`)
rather than risked as duplicates.

Rehearsed on the throwaway bank before production: a `2/6` session resumed to `6/6`, writing
exactly indexes 2-5, manifest `resumed: true, chunks_skipped_already_in_bank: 2`, no duplicate
documents (`v2/artifacts/selftest_manifest4.json`, `v2/artifacts/selftest_report4.json`).

Then the tails were closed in production under the same gate (batches 13-28, same 6 sessions /
40 documents caps, `--resume-partial`):

| batch | docs | ops failed | bank documents | recall@1 | recall@3 | duplicate rate | flipped | gate |
|---|---|---|---|---|---|---|---|---|
| 13 | 40 | 0 | 560 -> 600 | 0.5 -> 0.625 | 0.75 -> 0.75 | 0.2415 -> 0.2245 | q07 | PASS |
| 14 | 40 | 0 | 600 -> 640 | 0.625 | 0.75 | 0.2211 -> 0.2200 | - | PASS |
| 15 | 40 | 0 | 640 -> 680 | 0.625 | 0.75 | 0.2121 -> 0.2045 | q06 | PASS |
| 16 | 40 | 0 | 680 -> 720 | 0.625 | 0.75 | 0.2084 | - | PASS |
| 17 | 40 | 0 | 720 -> 760 | 0.625 | 0.75 | 0.2113 -> 0.2145 | q06 | PASS |
| 18 | 40 | 0 | 760 -> 800 | 0.625 | 0.75 | 0.2145 -> 0.2167 | q06 | PASS |
| 19 | 40 | 0 | 800 -> 840 | 0.625 | 0.75 | 0.2206 -> 0.2195 | - | PASS |
| 20 | 40 | 0 | 840 -> 880 | 0.625 | 0.75 | 0.2250 -> 0.2189 | - | PASS |
| 21 | 40 | 0 | 880 -> 920 | 0.625 | 0.75 | 0.2139 -> 0.2238 | - | PASS |
| 22 | 40 | 0 | 920 -> 960 | 0.625 | 0.75 | 0.2195 -> 0.2146 | - | PASS |
| 23 | 40 | 0 | 960 -> 1000 | 0.625 | 0.75 | 0.2146 -> 0.2108 | - | PASS |
| 24 | 40 | 0 | 1000 -> 1040 | 0.625 | 0.75 | 0.2108 -> 0.2110 | - | PASS |
| 25 | 40 | 0 | 1040 -> 1080 | 0.625 | 0.75 | 0.2087 | - | PASS |
| 26 | 40 | 0 | 1080 -> 1120 | 0.625 | 0.75 | 0.2126 -> 0.2110 | - | PASS |
| 27 | 40 | 0 | 1120 -> 1160 | 0.625 | 0.75 | 0.2110 -> 0.2062 | - | PASS |
| 28 | 6 | 0 | 1160 -> 1166 | 0.625 | 0.75 | 0.2110 -> 0.2225 | - | PASS |

**606 documents** written in the tail phase, **0 failed operations**, 16/16 gates PASS, and batch
29 then selected 0 candidates and exited 0 with "segment exhausted" (the runner distinguishes a
finished segment from a failure; the driver stops on either). The only flips in the whole phase
were `q06-delivery-dir` (gap 0.004-0.009) and `q07` - both `UNSTABLE_FLIP`, both reported and
excluded, which is the entire point of the corrected criterion: five of those flips would have
failed the old proxy-based gate.

### 8.3 Final state after completion

| | value |
|---|---|
| backfill sessions | 37 |
| documents written / planned | **1,003 / 1,003 = 100.0 %** (`v2/artifacts/backfill_completeness.json`) |
| partial sessions | **0** |
| bank documents | 175 (before any backfill) -> 1,166 |
| bank nodes | 5,312 -> 10,911, links 192,519 |
| failed operations | 332 (all historical, from the September outage; 0 in the last 50) |

Measured retrieval, same harness/question set as §3 (`v2/artifacts/final_after_complete.json`):

| | corrected baseline (295 docs) | final (1,166 docs) |
|---|---|---|
| recall@1 | 4/8 = 0.500 | **5/8 = 0.625** |
| recall@3 | 5/8 = 0.625 | **6/8 = 0.750** |
| recall@10 | 7/8 = 0.875 | 7/8 = 0.875 |
| duplicate-item rate | 0.244 | 0.21-0.22 |
| recall@1 excluding backfill items | 4/8 = 0.500 | 4/8 = 0.500 |
| recall@3 excluding backfill items | 5/8 = 0.625 | 5/8 = 0.625 |

So the finished backfill is **net positive on the measurement that matters** (the known-correct
answer moved into the top 3 for one more question, and into rank 1 for `q07`, whose answer is
reachable *only* through backfilled documents), it did not harm anything that was already
retrievable, and it reduced the duplicate-item rate. The health check's `coverage` warning also
cleared: 29/63 sessions with memory over the last 7 days (before) -> **53/63** (after), with
`recent_operations` 0/50 failed and the write probe passing in 4.0 s
(`v2/artifacts/health_after_complete.json`). The only remaining warning is the single historical
consolidation failure.


## 9. Still open (decisions, not work)

1. **Segments A+B+C** (everything before 2026-09-08): 425 uncovered sessions all-history, of
   which the earlier full-history estimate put the remaining cost at ≈7.6 M tokens for 1,713
   documents. **Not started, by instruction** - segment D was to be finished first. It now is
   (§8.3). The tools, the gate and the resume path are all ready; `--from/--to`,
   `--limit/--max-documents` and `--resume-partial` are the only knobs, and the same
   measurement-then-gate loop applies unchanged.
2. **`q05`-style coverage gaps**: a question whose ground truth was never retained cannot be
   fixed by ranking. Any future question set should distinguish coverage misses from retrieval
   misses (the harness reports `recall@10` misses separately; this report checks one of them
   manually against the bank).
3. **Dependency refresh window** for the Hindsight venv (unchanged from REPORT-V2 §8.4).
4. **Extraction latency** watch item (REPORT-V2 §4) - unchanged; the weekly regression tracks it.
5. **The bank has no supersession** (§4). Mitigation remains `reflect` for decision-grade
   questions; not eliminated.

---

## 10. v2.2 hardening pass (2026-09-13)

Five items, all landed on the same branch with tests. Two of them were defects this work had
left behind.

### 10.1 `pending_consolidation` is now judged by TREND, not by one reading

One reading cannot separate "the worker is draining a backlog" from "consolidation is stuck", and
right after this work the first thing the new signal saw was exactly that ambiguity: **738
pending consolidations** — the backlog of the 1,003 backfilled documents. Reported as a number it
would have looked like a fault; reported as a trend it is healthy, and the very next sample
answered it: **738 → 730, draining**.

Implementation: every run appends a sample (counts and timestamps only — never memory content) to
`pending_consolidation_history.json` beside the JSON report. The consecutive non-draining streak
decides: `ok` while draining or empty, `warn` after 3 consecutive flat-or-rising samples, `fail`
after 6 — or immediately if anything is pending while `last_consolidated_at` is ≥ 24 h old. An
unwritable history is reported in the finding but never fails the check (a monitor must not die
because it cannot write its own scratch file); an unreadable one is re-seeded. New CLI:
`--history`, `--pending-stall-samples`, `--pending-fail-samples`. Eight unit tests cover
empty / draining / flat / stalled / stale-consolidation / unwritable-history.

The same single-reading defect appeared one layer over, and is fixed the same way:
`failed_consolidation` is a **cumulative** counter, so the old check warned forever about one
failure from the September outage — the weekly regression therefore reported "warnings" every week
regardless of health. It now warns only when the counter RISES since the previous sample (two more
tests), and the unchanged case is reported as `ok` with the word "historical".

### 10.2 The 85 % band is now the same number everywhere, and the stores are slimmer

The code warned at 90 % while the runbook documented 85 % — a check whose behaviour nobody had
written down. Both are now 85 % (`WARN_BUILTIN_FULL`), and a test pins the constant to the
documented policy.

Slimming, with byte-for-byte backups (`v2/memory-backup/pre-v22-20260913/`, sha256 in
`SHA256SUMS.txt`):

| Store | Before | After | What changed |
|---|---|---|---|
| `MEMORY.md` | 1,968 chars (**89.5 %**) | 1,561 chars (**71.0 %**) | three entries that were pure procedure moved to the skills that own them: plugin discovery/hooks → `hermes-plugins`; the Windows launcher creation-flags lesson → `windows-self-hosted-service-setup`; the cron bash-wrapper/exit-127 trap → `hermes-cron-jobs`; the two Hermes state facts (async_delegations join key, in-memory `todo_list`) → `hermes-session-management`. One line points at where they went |
| `USER.md` | 1,129 chars (82.1 %) | 1,101 chars (80.1 %) | reporting and closing-style rules tightened, no preference removed |

Both now sit comfortably below the band instead of one warning away from it.

### 10.3 The question set is now a real golden set for long-term memory

The 10 questions this work started with mixed config trivia with two liveness probes and held one
coverage gap. The committed set is **48 questions** (`retrieval_questions.json`; the old set is
kept as `retrieval_questions_v1_10q.json`): 12 user preferences/standing decisions, 16
environment facts, 14 project decisions, 2 monitoring facts, 4 liveness.

Every scored question is **coverage-validated before use** by the new
`scripts/validate_golden_set.py`, which folds the bank's memory texts exactly the way the harness
scores a hit and counts the items containing each expected value. First run of the *draft* set
found 4 questions whose answer was nowhere in the bank (they could only ever fail) — they were
replaced, not kept; the frozen set is **44/44 covered** (`artifacts/golden_set_coverage.json`).

The token-specificity rule came out of the same data and is now written down: a question whose
token matches hundreds of items (e.g. `session`: 1,272 items) makes every response a "hit" and the
benchmark meaningless, so questions were re-pointed at specific tokens (`exit 127` 7 items,
`hindsight/.venv` 3, `[REDACTED:e873a6b0c3cc]` 2) instead.

Measured baseline for the frozen set (`artifacts/golden_baseline_v22.json`):

| | value |
|---|---|
| recall@1 | **20/44 = 0.455** |
| recall@3 | **27/44 = 0.614** |
| recall@10 | **32/44 = 0.727** |
| decided / unstable | 8 / 40 |
| questions answered | 48/48, 0 errors |

Deliberately harder than the old set, so these numbers must not be compared with the 10-question
figures in §3 — the old ones were mostly config trivia. The 12 misses at @10 are the interesting
output: preference-language paraphrases (`繁體`, `artifact`, `全量`) and facts retained in only one
or two items are where recall actually fails, which is what a golden set is for.

### 10.4 The resume path is now fail-closed

Previously a failed progress walk logged and continued (silently resuming nothing), and a
`chunking_mismatch` skipped that session while the run reported success. Now:

* progress walk fails with `--resume-partial` → **refuse to run**, exit 2, nothing written;
* any session whose recomputed chunk count disagrees with the bank's → **refuse to run**, exit 2,
  with the session and the chunking parameters named, and a hint to re-run with the original
  parameters;
* inside the run, a resumed session that submits fewer chunks than its missing list (and was not
  stopped by the document cap) is recorded as a failure and the process exits 2;
* after the writes, `memory_backfill_batch.py` re-reads progress **from the bank** and requires a
  session called `completed` to be complete there, and every session to account for
  `chunks_skipped + chunks_submitted`; otherwise the batch exits 3. Async writes are given three
  attempts before that verdict.

So "the run closed its gap" is now a claim the tools verify rather than assert — the failure mode
that produced the 39.6 %-complete segment (§8.1) cannot recur silently.

### 10.5 `recall_without_backfill` is documented as an attribution proxy

Removing items from an already-returned list cannot reproduce what retrieval would have returned
without them (top-k/token budget, reranker input set and consolidation window would all have
differed). The harness docstring, its methodology block and the JSON now say **attribution proxy,
not a counterfactual**, and state the narrower question it does answer: "would the answer still be
present in *this* response if the added documents had not been inserted?" The runbook and the
`hindsight-memory-server` skill carry the same wording.

### 10.6 Verification
> CORRECTED in §10.8 (1): the count below was read from the runner's Summary line, which excludes
> files whose tests never collected. The honest number, measured on a fresh clone, is **89 passed**
> with the same single pre-existing failure.

* `tests/scripts/` — **68 passed**, including 21 new tests over the trend logic, the bank-progress
  walk, the manifest, and the golden-set validator. One unrelated pre-existing failure remains:
  `test_contributor_map.py::test_add_contributor_refuses_a_case_collision` asserts a lower-case
  path does not exist after writing a mixed-case one, which cannot hold on a case-insensitive
  Windows filesystem; the file is untouched by this work and the assertion is host-dependent, not
  a regression here.
* A Windows footgun the repo's own checker caught in the new tools (`subprocess.run(text=True)`
  without `encoding=` on the same line) is fixed — the child output would have been decoded with
  the locale code page (cp936 on this box).
* The weekly regression was run end to end on the operational path (`memory-ops/`) with the new
  question set: `v2/regression_v22.log`, artifacts in `v2/regression-v22/`.
* Repo and operational copies of every tool are byte-identical (sha256-verified after the sync).
  The **question set** is no longer mirrored into the repo (§10.8 (4)): the live set stays where
  the tools run it, the repo carries a synthetic template, and `run_memory_regression.sh` resolves
  the live set from its home or reports `SKIPPED` — it never falls back to the template.

### 10.7 Privacy defect found and fixed: committed artifacts contained memory text

While deciding which artifacts to commit for this pass, the fork's visibility was checked for the
first time: `fxhsxm/hermes-agent` is **public**. A scan for the field names the harness writes
found **1,107 memory-derived text fields** already committed under
`docs/audits/memory-system-20260912/v2/artifacts/`:

| file | long text fields | committed by |
|---|---|---|
| `baseline_v3_var3.json` | 440 | this pass (§2) |
| `final_after_complete.json` | 227 | this pass (§8.3) |
| `baseline_v3.json` | 221 | this pass (§2) |
| `batch3_rerun_confirmation.json` | 207 | the previous session |
| `sandbox_simulation.json` / `sandbox_questions.json` | 7 | earlier work (synthetic sandbox markers, harmless) |

They held real memory item text, duplicate-pair text and contradiction windows (up to 240 chars
each) — the user's own retained observations, not test fixtures. None of it is a credential, and
nothing outside this repository was ever exposed, but it is memory content in a public repo, which
this work's own rules forbid.

Fixes applied:

* **Harness 1.2 redacts by default.** `text`, `a_text`, `b_text`, `sibling_text` and marker
  `window` fields are replaced with `[redacted N chars]`; `--keep-text` opts out for a local
  debugging run. `run.text_redacted` and `run.redacted_text_fields` record what was removed, and
  the methodology block documents why. New test file
  `tests/scripts/test_measure_retrieval_redaction.py` pins both the redaction and the fact that
  every number a judge reads (ranks, margins, scores, ids, counts, classes, our own query text)
  survives it.
* **The four affected files were rewritten in place** with the text replaced by length markers, all
  numbers left untouched — so every figure in §2, §3 and §8.3 still verifies against them, now
  without the memory content. `run.redaction_note` says so in each file.
* The rule is written into `MAINTENANCE.md` §5.4: anything copied into the repo's audit directory
  must come from a default (redacted) harness run.

**Still open, and deliberately not decided here:** editing the files does not remove the old blobs
from git history. The options are (a) accept it — the content is environment/preference
observation from a private box, no credentials; (b) rewrite the fork's history with
`git filter-repo` and force-push (changes every SHA on the branch, breaks existing clones);
(c) make the fork private; or (d) delete and re-create the fork. That is a user decision, so
nothing was done beyond reporting it.

### 10.8 v2.2.1 closing pass — review findings, each with evidence

An independent review of `6152677942` found two real defects, one overstated claim and one privacy
gap. All four are addressed here; the numbers above that the review disputed are corrected in
place rather than left standing.

**(1) The committed redaction test never ran.** `test_measure_retrieval_redaction.py` built
`_spec` and called `_spec.loader.exec_module(_mod)` without ever creating `_mod`, so pytest raised
`NameError` at collection. The repo's parallel runner reports such files under "N file(s) where no
tests ran (collection/import error...)" and keeps them out of the passed/failed totals, so the
headline read "70 tests passed, 1 failed" and the earlier report's "70 passed" was wrong: four
tests had not executed at all. Fixed (`module_from_spec`), and the count below is now measured on
a **fresh clone** of the pushed branch, not the worktree. Lesson recorded in the runbook: read the
"no tests ran" section, never just the Summary line.

**(2) The golden set is now split by the store that is supposed to hold each answer**, because a
question scored against the wrong store cannot measure retrieval, and a generic expected token
hides that by matching unrelated items:

| tier | n | what it is | scored? |
|---|---|---|---|
| `core` | 13 | bank-retrievable long-term memory, expected token verified selective (≤20 items) | **yes — the headline, and the only tier the backfill gate uses** |
| `environment` | 23 | facts a live tool reads back (port, version, model, path, cadence) | smoke only |
| `builtin` | 7 | answers live in the injected `MEMORY.md`/`USER.md` (always in context, never retrieved) | verified against those files |
| `gap` | 1 | retained in neither store | no — reported as a finding |
| `liveness` | 4 | no expectation | no |

Two findings came out of doing this honestly:

* **The first attempt failed specificity on 14 of 23 core questions** (`STOP` matched 296 items,
  `manifest` 160, `backup` 87, `OCR` 196). Excluding them would have left a 9-question benchmark,
  so the tokens were replaced with on-topic selective phrases, verified against their store
  (`精準回滾` → 4 items, `唔好未經確認就回滾` → 2). Core is now 13/13 covered with **0**
  low-specificity questions.
* **Seven "core" questions were measuring the wrong store entirely.** Their answers exist only in
  the injected `USER.md` (`禁止以 CDP 或強殺瀏覽器`, `固定 provider／model 且不 fallback`,
  `子代理交付只回傳 artifact 路徑`, `STOP 即停止`, `搬去獨立`, `書面語`). They used to "pass" because
  a generic token (`CDP`, `fallback`, `artifact`, `STOP`) matched hundreds of unrelated bank items.
  They are now tier `builtin`, verified against the files, and provably score 0 in the bank.
* **One decision is retained nowhere** (`proj-reflect-rule`: which questions should use `reflect`
  rather than `recall`) — tier `gap`, reported rather than scored, and an open item to fix.

Corrected baseline (harness 1.3, 2026-09-13):

| scope | n | recall@1 | recall@3 | recall@10 |
|---|---|---|---|---|
| **core (headline)** | 13 | **0.308** | **0.538** | **0.615** |
| core + environment (pre-tier continuity) | 36 | 0.361 | 0.528 | 0.750 |
| environment (smoke) | 23 | 0.391 | 0.522 | 0.826 |
| builtin (bank retrieval, expected to be ~0) | 7 | 0.000 | 0.000 | 0.000 |

Core misses at @10: `up-asr-model`, `up-delete-rollback`, `proj-upgrade-rollback`,
`proj-gate-criterion`, `proj-coverage-check` — real signal, named rather than averaged away.

**(3) The health evidence is now unambiguous.** The committed weekly run does exit 1 (unit
invariants SKIPPED, and at that moment a first-sample `failed_consolidation` warning), and the
earlier "healthy" reading came from a run whose **write probe was skipped**, so "11 checks PASS"
was not what those artifacts showed. One run with history already primed **and** the write probe
enabled: `artifacts/health-healthy-with-probe.json` — status **healthy (0)**, 10/10 checks PASS,
`write_path_probe` PASS (dry-run extraction HTTP 200, 1 fact extracted, nothing persisted), and
`pending_consolidation` **0** (the backlog drained from 738).

**(4) Privacy: redacting item text was not enough.**
The question set is memory-derived too — a question plus its expected token is a structured
statement about the user's machine and preferences. So:
* the live set is **no longer in this repository**; it ships as
  `retrieval_questions.template.json` (synthetic, marked `"synthetic": true`) while the live set
  lives with the tools that run it (`%LOCALAPPDATA%/hermes/scripts/memory-ops/`), and the harness
  and batch runner **refuse to run on the template** rather than produce meaningless numbers;
* the harness (1.3) and validator now redact question text and expected tokens by default
  (`--keep-text` / `--keep-expect` for local runs), keeping ids, tiers, ranks, margins and counts;
* `scripts/check_artifacts_privacy.py` makes the rule executable: it scans the tree against the
  live set, the injected memory files and (optionally) every bank item, and fails closed. Run
  against this tree it found 13 files and now reports **0** after redaction
  (`[REDACTED:<fingerprint>]` markers keep the audit trail without republishing content);
* **still open, and the user's call**: the old blobs remain in git history. Editing files does not
  remove them, and a public fork's visibility cannot be changed independently of its network
  (so "make the fork private" is not available). The options are: accept it; rewrite history
  (`git filter-repo` + force-push, which changes every SHA); or delete and recreate the fork.
