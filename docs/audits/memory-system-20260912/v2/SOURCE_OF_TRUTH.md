# Memory source-of-truth and retention policy (Memory System v2)

Owner of every kind of durable information, plus the retention rules that decide what is
ever sent to the long-term bank. Written from the measured behaviour of this deployment
(Hermes Agent v0.21.0 + Hindsight, bank `fwh-main`), not from intent.

## 1. One owner per kind of information

| Kind of information | Owner (single source of truth) | Explicitly NOT here |
|---|---|---|
| Stable user identity facts and preferences (who, how to talk to them, language, format) | `USER.md` | events, project state, anything that changes weekly |
| Cross-session environment facts / pointers that change behaviour in **every** session (paths, hosts, endpoints, standing decisions) | `MEMORY.md` | procedures, logs, values a live tool can read cheaply |
| Rich events, outcomes, decisions, project history, cross-session synthesis | Hindsight bank (durable layer of record) | anything that must never be silently lost without an alarm |
| Reusable procedure, workflow, or pitfall | the narrowest matching **Skill** | one-off facts, session state |
| Project architecture, conventions, local commands | project context (`AGENTS.md` / repo docs) | user preferences |
| Current task state, temp paths, values readable from files/config | **this session only** | never persisted |

## 2. Rules that follow (each one is traceable to a measured failure)

1. **Write-through on promotion.** A fact first observed in a session belongs to the bank (it is
   captured automatically). Promote it to `USER.md`/`MEMORY.md` only when it must influence *every*
   session — then remove it from the other layer's backlog, do not keep both. Rationale: the injected
   block is a fixed-size budget (MEMORY.md was found at 97 % of 2200 chars), and duplication is what
   pushes it to the cap, which then forces eviction of a durable fact to make room for a duplicate.
2. **"Accepted" is not "durable".** Treat a write as durable only when its operation reports
   `completed`. This audit's own incident: 4.5 days of retains were accepted and then failed
   asynchronously; `last_memory_write_at` is the only honest freshness signal.
3. **The bank is the record of narration; the built-in stores are the always-on digest; skills are
   the record of procedure.** If the bank is unreachable the agent must not behave as though nothing
   happened — the plugin now reports a failed retain instead of draining it silently.
4. **Capacity is a routing signal.** Above ~85 % of a built-in store's budget, route new facts to the
   bank (or a skill) instead of evicting an existing invariant.
5. **Never persist** credentials, tokens, keys, connection strings, temporary task state, values a
   reliable tool can read (model names, ports, versions), or anything already owned by a skill.
6. **Sensitive data does not belong in permanently injected files.** `USER.md`/`MEMORY.md` are sent
   on every request; anything that would be damaging in a prompt dump goes to the bank instead (or is
   not stored at all).

## 3. Retention policy for delegated (subagent) work

Measured start state: **233 subagent sessions, 0 of them in the bank** — children run without a
provider session of their own, so nothing they do is retained. The policy keeps what has value and
structurally excludes what does not:

* **Never retain a child's transcript.** Intermediate reasoning, tool output, dead ends and wrong
  hypotheses are exactly what the user asked not to pollute long-term memory with. Enforced by
  construction: the digest is assembled from the two strings the delegation hook provides (goal +
  the child's final summary), so none of that material has a path into memory.
* **Retain one outcome digest per completed child**, tagged `kind:delegation-outcome` under the
  parent's session tag (plus `child:<child_session_id>` for provenance), in its own document so it
  can never overwrite the parent's turn document. Digest = goal (≤400 chars), result summary
  (≤1200 chars), up to 6 artifacts extracted from the summary (paths/URLs), child session id.
* **One digest per distinct outcome.** A repeated `subagent_stop` cannot write twice (bounded
  in-session dedupe), and an empty/whitespace outcome is not retained at all.
* **The parent still owns the narrative.** The digest is provenance for "this work happened and
  produced X", not a replacement for the parent's own retained turn.

Implementation: `plugins/memory/hindsight/__init__.py` (`on_delegation`,
`_make_delegation_retain_job`, `_build_delegation_digest`) + tests in
`tests/plugins/memory/test_hindsight_provider.py`. Companion policy for anything the *user* wants
kept verbatim: it goes in the normal session narration, which is retained per turn.

## 4. Recall hygiene (duplication + freshness)

* The bank returns a raw fact and the consolidated `observation` built from it as two results with
  near-identical text; both used to be injected, spending the caller's context twice on one fact.
  The plugin now folds text (case/whitespace/punctuation), keeps the **highest-ranked** occurrence
  and drops later repeats (`_dedupe_recall_results`), on both the prefetch injection path and the
  `hindsight_recall` tool.
* A superseded fact can still outrank its replacement in **plain recall** (measured in the sandbox:
  old value rank 1, new value rank 3, both `state: valid`), because retain keeps history and recall
  ranks by similarity. `reflect` resolves it correctly using `mentioned_at`. Rule of use:
  **decision-grade questions go to `reflect`; recall is for context, not for adjudicating a value
  that has changed.**
* Re-measure both properties with `measure_retrieval_quality.py` (baseline JSON is kept next to it)
  after any change to the recall path, bank config, or embedding/reranking route.

## 5. What must never be done to this system

* No bank rebuild / embedding-dimension change without a full export + import plan and a maintenance
  window.
* No destructive cleanup (`failed_operations`, historical residue, GC) without a fresh
  `hindsight-admin backup` zip verified in the same run.
* No historical replay (backfill) without: sandbox dry-run first, a backup, unique
  `kind:backfill` tags for rollback, and a document cap.
* Never disable native compression or context engines as part of memory maintenance.
