# Hermes + Hindsight persistent-memory audit — 2026-09-12

Read-only-where-possible audit of the *working* long-term memory system: how complete, how
fresh, how accurate, how retrievable it is, and whether it stays reliable as it accumulates.
Everything below is either a measured number from the live runtime or explicitly labelled as
unverified. No memory content, credentials or personal identifiers are reproduced here; only
counts, rates, status codes and synthetic probe values.

* Baseline commit: `fa24c030a5e48af63eb152a0b3b344a99e991244` (branch `audit/memory-system-20260912`)
* Bank audited: `fwh-main` (production) + a throwaway `fwh-audit-sandbox` used for write tests
* Machine: Windows 11, Hermes Agent v0.21.0 (2026.8.31, upstream `284d220b`), local Hindsight API `0.9.0`,
  plugin client `hindsight-client 0.6.1`, embeddings Voyage, reranker `voyage/rerank-2.5`

---

## 1. Headline result

**The memory system was silently not writing anything for ~4.5 days** (last durable write
`2026-09-07T17:10Z`, detected `2026-09-12T12:41Z` = 107.7 h stale), while every health signal the
user normally sees stayed green. The read path was healthy the whole time. Root cause, fix and
end-to-end verification are in §3; the detectability gap that allowed 4.5 days of silence is §4.

Measured status after the fix: write path restored and proven end-to-end (§3.3), recall quality
unchanged (§2.2), **coverage of sessions into the bank remains the open risk** (§5).

---

## 2. Measurements

### 2.1 Freshness and operation health (production bank)

| Metric | Value | Source |
|---|---|---|
| Facts / links / documents | 5060 / 71766 / 159 | `GET /banks/fwh-main/stats` |
| Fact types | world 1745, experience 1499, observation 1816 | same |
| Last durable write | `2026-09-07T17:10:11Z` (107.7 h before the audit) | same |
| Last consolidation | `2026-09-07T04:04:44Z` | same |
| Operations all-time | 1546 completed / **332 failed (17.7 %)** | same |
| Operations in the visible window (2026-09-10…12) | **73 / 73 failed (100 %)** | `GET /operations` |
| Pending consolidation / failed consolidation | 1 / 1 | stats |
| Documents per day since Hindsight went live (2026-08-12) | 159 total, **0 on 2026-09-08…09-12** | `GET /documents` |

### 2.2 Retrieval quality (production bank, read-only)

10 fixed questions (mostly Traditional Chinese, one Cantonese, one English) whose answers must
already exist in the bank:

* recall@1 = 6/10, recall@3 = 8/10, recall@10 = 8/10, mean latency 1.02 s
* the 2 misses had **no ground-truth record in the bank at all** (confirmed by full-text search),
  so for facts that exist the effective recall@3 was **8/8**
* reranker is genuinely engaged: recall items carry `scores = {final, reranker, semantic, keyword}`
  with non-zero cross-encoder values (e.g. reranker 0.57–0.95), not semantic-only ranking
* duplicate near-identical hits appear (a raw fact and its consolidated observation are returned
  as separate results with the same score) — see §5.3

### 2.3 Coverage — the quiet, pre-existing gap

Session tags in the bank are the only reliable "did this session reach memory?" signal
(consolidated observations keep `session:<id>` but lose `document_id`; `GET /documents/<session_id>`
404s for the `<session_id>-<timestamp>` ids used by the legacy fallback path).

| Slice | Sessions | Reached the bank |
|---|---|---|
| All sessions since 2026-08-12 | 542 | 126 (**23.2 %**) |
| Excluding `subagent`/`tool` sessions | 307 | 126 (**41.0 %**) |
| `subagent` sessions | 233 | **0** |
| `telegram` / `desktop` / `cli` | 177 / 82 / 30 | 75 / 34 / 3 |

Per-day coverage found days with real traffic and **zero** memory: `2026-08-15…08-20`, `2026-08-31`
(26 sessions), `2026-09-08…09-12` (the last block is the §3 outage). The August gaps predate the
outage by weeks and are **not explained** by it; candidate causes are turns that never completed
(`ended_at IS NULL` sessions), subagent-only traffic, or earlier provider-side rejections that were
never logged. One uncovered Telegram session had 741 messages and no memory whatsoever.

### 2.4 Built-in stores (always injected)

| Store | Size | Budget | Headroom |
|---|---|---|---|
| `MEMORY.md` | 2129 chars | 2200 | **97 % full — effectively at cap** |
| `USER.md` | 1169 chars | 1375 | 85 % |

At 97 % the next fact written must evict something; churn risk, not a bug.

---

## 3. Finding 1 (critical): the write path was dead — `400 MissingSessionID`

### 3.1 Root cause (proven, not inferred)

1. `opencode-go` (the relay used for Hindsight's LLM) began **hard-requiring** the
   `x-opencode-session` header on every request. Direct client proof: same request, no header →
   `HTTP 400 {"type":"MissingSessionID", ...}`; with `x-opencode-session: <any opaque value>` →
   `HTTP 200`.
2. Hermes itself sends that header on every OpenCode request (`agent/opencode_affinity.py`), which
   is why chat kept working while memory did not.
3. Hindsight `0.9.0`'s OpenAI-compatible provider does **not** send it, and its sibling code path is
   the reason configuration cannot help: `engine/llm_wrapper.py` drops `llm_default_headers` when
   constructing `OpenAICompatibleLLM` (only the litellm/anthropic/openai-responses branches receive
   it), and `engine/providers/openai_compatible_llm.py` never sets headers on its `AsyncOpenAI`
   client. `HINDSIGHT_API_LLM_DEFAULT_HEADERS` is therefore a no-op on the `opencode-go` route.
4. Upstream has since added native support in exactly this provider
   (`engine/cache_affinity.apply_opencode_session`, checked against upstream `main`), i.e. **the
   pinned 0.9.0 predates the fix**.
5. Onset: earliest server-side `MissingSessionID` warning `2026-09-07T13:25:41Z`; the visible
   operations window (09-10…09-12) is 100 % failures; the bank records no new document after 09-07.

Effect: every retain after that point failed at LLM fact-extraction, so turns, decisions and
outcomes from ~4.5 days were never durable. Nothing surfaced to the user.

### 3.2 Fix applied (low-risk, reversible, no data touched)

**A. Hindsight venv patch** — `apply_opencode_header_patch.py` (+ generated diff
`artifacts/hindsight-opencode-header.patch`), 2 files / 4 hunks:

* `engine/llm_wrapper.py`: forward `default_headers` into the `OpenAICompatibleLLM` branch (+1 line)
* `engine/providers/openai_compatible_llm.py`: accept `default_headers`, store it, and pass it to
  `AsyncOpenAI(default_headers=…)` (+1 param, +3 attr, +2 kwargs)

Both originals are preserved as `*.orig-opencode-header`. This mirrors what upstream later shipped
for the same provider; it does not change the locked provider/model choice.

**B. Launcher env** — `hindsight/scripts/start-hindsight.ps1` gained
`HINDSIGHT_API_LLM_DEFAULT_HEADERS = '{"x-opencode-session":"hindsight-fwh-main"}'`
(backup: `start-hindsight.ps1.bak-20260912-before-header-fix`).

**C. Controlled restart** — soft-kill the API, then re-run the single Task Scheduler supervisor
(`restart_hindsight.sh`, log `restart_run.log`): **30 s total downtime**, `/health` 200, bank
unchanged.

### 3.3 Verification (all green)

| Evidence | Pre-fix | Post-fix |
|---|---|---|
| Patched provider, client-side call with headers | — | **200**, valid JSON facts returned |
| Same call without headers (control) | 400 MissingSessionID | still 400 → the patch is what fixes it |
| `POST /memories/dry-run-extract` (persists nothing) | **HTTP 500** (400 upstream) | **HTTP 200**, 2 facts, 5.8 s |
| Bank integrity after restart | 5060 nodes / 71766 links / 159 docs | identical (no loss, no rebuild) |
| Real Hermes plugin path (`HindsightMemoryProvider.sync_turn` → writer → client) into the sandbox bank | fails | `batch_retain` + `retain` **completed**, fact recallable |
| Sandbox write → update → recall → reflect → consolidate | — | retains `completed`; recall 0.5–0.7 s; reflection 49 s resolved the newer fact; consolidation created observations; 0 pending / 0 failed |
| `scripts/memory_health_check.py` write probe | would fail | **PASS** |

Note: `dry-run-extract` and the sandbox bank were used deliberately so production memory content
was never written to by the audit.

### 3.4 Live confirmation on the production bank (after the fix)

The restored path was exercised through the *real* Hermes plugin in production (a deliberate
`hindsight_retain` for this audit's outcome):

* `last_memory_write_at`: `2026-09-07T17:10:11Z` → **`2026-09-12T04:56:47Z`** (fresh)
* facts 5060 → 5065, documents 159 → 160, a consolidation operation **completed**
* health check immediately after: `write_path_probe` PASS, `bank_activity` PASS (0.0 h)
* `recent_operations` still FAIL (49/50) — correctly reported *with its window range*
  (`2026-09-11T19:34 .. 2026-09-12T04:56`), i.e. historical residue from the outage that decays as
  new successful operations displace the window. This is exactly the disambiguation the check was
  built for; do not read it as a live failure.

---

## 4. Finding 2 (critical, fixed in-repo): a failed retain looked exactly like a success

`plugins/memory/hindsight/__init__.py::_is_retain_op_complete()` treated server status
`{"completed", "failed"}` as the same terminal outcome — a failed async retain drained the pending
set with **no log line, no counter and no user-visible signal**. That single line is why a 100 %
write outage could run for 4.5 days while `/health` stayed green and the retain indicator said
"saving to memory…".

Fix (this branch): a failed operation is still terminal, but now emits one WARNING per operation
carrying the server's error text, plus the retain-indicator failure line — deduplicated so the poll
loop cannot spam. Three invariant tests were added and **proven red on the unpatched baseline**
(3 failed → 3 passed with the fix); the whole plugin test file is green (88 passed, 1 skipped).

---

## 5. Open risks (measured, not fixed — with proposals)

### 5.1 Coverage: most sessions never reach memory (highest long-term risk)

23 % of sessions since 2026-08-12 (41 % excluding subagents) have any memory at all; all 233
subagent sessions have none; several days with double-digit session counts produced zero documents
before the outage existed. As the bank is the only layer that holds rich history, this bounds how
"complete" long-term memory can ever be — and it degrades silently, exactly like §3.

*Proposal (needs a decision, no code written):* (a) decide explicitly whether subagent work should
be retained (today it is dropped); (b) classify uncovered-but-long sessions (≥ 20 messages) and
determine whether they are dangling sessions that never hit a retain point; (c) adopt the per-day
coverage signal from the health check as a standing alarm.

### 5.2 Staleness alarm + operation history

`332/1878` operations failed all-time. The last-50-op window still fails today (historical residue
from §3) and will only decay as new successful operations displace it — the health check reports
the window range so this cannot be misread as a live outage. *Proposal:* run the health check on a
schedule (cron) and treat `write_path_probe=fail` or `bank_activity ≥ 24 h` as a page.

### 5.3 Recall returns superseded facts at rank 1, and raw/observation duplicates

Sandbox E2E: after a contradicting update, the *older* value still ranked above the newer one in
plain recall (both `state: valid`; no invalidation at the raw level), while the LLM `reflect` path
resolved the conflict correctly via `mentioned_at`. Both the raw fact and its consolidated
observation are returned as separate hits with near-identical scores.

*Proposal:* decide per use-case — decision-grade questions should go through `reflect`, and/or the
bank's temporal-retrieval settings should be revisited; a duplicate-suppression pass at injection
time (prefer observation over its source fact) would cut injected context without losing facts.

### 5.4 Verified traps for anyone auditing this stack later

1. `GET /memories/list?tags=<anything>` **silently ignores the filter** (a bogus tag returned 10
   untagged memories). Use `GET /observations/scopes` or a full `memories/list` pagination to derive
   session coverage — a tag filter here produces false "this session has memory" conclusions.
2. `GET /documents/<session_id>` 404s for the legacy `"<session_id>-<timestamp>"` document ids, so
   document-id matching under-counts coverage (23 % vs the tag-based 23.2 % — same conclusion here,
   different ids).
3. `memory.flush_min_turns: 6` in `config.yaml` is a **dead key** — nothing reads it. Retention
   cadence is actually controlled by the plugin's `retain_every_n_turns` (default 1, every turn) in
   `$HERMES_HOME/hindsight/config.json`. The user may believe scheduling is limited when it is not.
4. The local operational skill claims Hindsight 0.9.0 has *no* HTTP text-retain endpoint (MCP only).
   `POST /banks/{bank}/memories` exists and accepts `RetainRequest` items; it is the endpoint behind
   the plugin's batch retain. Verified live.
5. `max_completion_tokens` below ~4k on this reasoning model returns `finish_reason=length` with
   empty content, which surfaces as "Provider returned empty message content" — a second, latent
   failure mode for any change to the extraction route (observed while validating the patch).

---

## 6. Layering and source-of-truth rules (requested)

Current reality: Hermes injects `MEMORY.md` + `USER.md` every turn (~3300 chars, both near cap), and
Hindsight writes/reads rich per-session history; the overlap is real but bounded — the injected block
is *not* re-sent to the provider as retained content (retain ships user/assistant turns only), so the
duplication is conceptual, not literal echo. Keep one owner per kind of information:

| Information | Owner (source of truth) | Never store here |
|---|---|---|
| Stable user facts and preferences | `USER.md` | events, project state |
| Cross-session environment facts / pointers that change behaviour in *every* session | `MEMORY.md` (keep ≤ 85 % of budget) | procedures, logs, values a tool can read |
| Rich events, outcomes, decisions, project history, cross-session synthesis | Hindsight (the bank) | anything that must never be forgotten silently |
| Reusable procedure or pitfall | the narrowest matching skill | one-off facts |
| Project architecture/commands | `AGENTS.md` / project context | user preferences |
| Current task state, temporary paths, values available from files/config | the session only | never |

Rules that follow, and that the measurements above justify:

1. **Write-through on promotion.** A fact first observed in a session belongs to Hindsight; only
   promote it to `USER.md`/`MEMORY.md` when it changes behaviour in *every* session. Never duplicate
   it in both without a stated reason (duplication is what pushes the injected block to its 97 % cap).
2. **Verify before evicting.** Treat a provider write as durable only after its operation reports
   `completed` (this audit's own outage is the counter-example: "accepted" ≠ "durable").
3. **Hindsight is the durable layer of record; the built-in stores are the always-on digest.** If
   Hindsight is unreachable, the agent must never behave as if nothing happened — see §4's fix.
4. **Capacity is a routing signal.** `MEMORY.md` above ~90 % means new facts must displace old ones;
   prefer routing the new fact to Hindsight (or a skill) instead of evicting a durable preference.
5. **One retention contract per layer:** provider for narration, built-in stores for invariants,
   skills for procedure. When in doubt whether something is an invariant, it is not — put it in
   Hindsight.

---

## 7. Reusable health check (artifact of this audit)

`scripts/memory_health_check.py` — read-only, no writes, exits 0 (healthy) / 1 (warnings) /
2 (degraded). Layers checked separately, because a green `/health` proves wiring and nothing else:

1. provider wiring (`config.yaml` `memory.provider` + `$HERMES_HOME/hindsight/config.json`)
2. API liveness (`/health`, `/version`)
3. **write path**: non-persisting `dry-run-extract` probe (exercises the real extraction LLM call)
   + `last_memory_write_at` staleness (warn ≥ 24 h, fail ≥ 72 h)
4. operation health: failure ratio over the last N operations **with the window's time range**, plus
   pending operations and failed consolidations
5. retrieval: recall probes, reranker engagement, latency; accuracy mode via `--queries` (expect lists)
6. coverage: per-day "sessions vs sessions-with-a-bank-document", flagging days with traffic but no memory
7. built-in stores: size vs configured caps

```bash
python scripts/memory_health_check.py --bank fwh-main --json health.json
python scripts/memory_health_check.py --no-probe-write          # cheaper, skips the LLM probe
python scripts/memory_health_check.py --queries queries.json    # [{"id","query","expect":[...]}]
```

Recommended cadence: daily (cheap mode) + weekly (with write probe and `--queries`). This audit's
outage would have been caught at the first daily run after 2026-09-08 — a 24 h detection instead of
107.7 h.

---

## 8. Deliberate non-actions

* No memory content was created, deleted, migrated or edited in `fwh-main`. The write tests used a
  throwaway bank (`fwh-audit-sandbox`), left in place on purpose so a reviewer can inspect the
  evidence; delete it with `DELETE /v1/default/banks/fwh-audit-sandbox` when no longer useful.
* Hindsight was **not** upgraded. The durable fix is an upgrade to a build containing
  `cache_affinity.apply_opencode_session`, which then makes the venv patch unnecessary — that is a
  deployment change with a maintenance window and needs explicit approval.
* No bank rebuild, no embedding/dimension change, no bank-config change, no consolidation of
  historical residue (`failed_operations`/`failed_consolidation` were left as found).
* The 4.5 days of lost memory are **not recoverable** (fact extraction never completed, so no
  partial rows exist) — the turns themselves remain in Hermes sessions, so a targeted re-retain is
  conceivable but was not attempted.

## 9. Reproduce

1. `hindsight/scripts/` + venv: run `apply_opencode_header_patch.py` only on an unpatched 0.9.0 venv
   (idempotent; refuses to run twice), keep the generated `.patch` for review.
2. Health check: `python scripts/memory_health_check.py --json out.json`.
3. Tests: `scripts/run_tests.sh tests/plugins/memory/test_hindsight_provider.py`.
4. Evidence for every number in this report: `artifacts/evidence.json`
   (aggregates only: status codes, counts, rates, latencies — no memory text, no identifiers).
5. Work-unit timeline, tools and concurrency: `EXECUTION_AUDIT.md`.
