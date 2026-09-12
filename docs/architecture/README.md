# Architecture documentation (runtime-verified)

This directory holds the **execution architecture map** of Hermes Agent: one
end-to-end, anchor-verified description of what actually happens between a user
request arriving and the answer being persisted and delivered.

It exists because the developer guide (`website/docs/developer-guide/`) describes
subsystems in isolation and drifts silently, while debugging and self-maintenance
need the *live* chain: which function runs next, what state it writes, and where
it can fail. Every load-bearing claim here carries a `path/file.py:LINE` anchor
that a script can re-check, so the map is falsifiable rather than prose.

## Documents

| File | Contents |
|---|---|
| `execution-map.md` | The map itself: entry surfaces → turn admission → loop → model call → tool round → state writes → delivery, plus the control, state and failure paths. **Start here.** |
| `doc-runtime-deltas.md` | Every place where the documentation disagreed with the pinned runtime, with evidence, disposition, and the minimal fix applied or proposed. |
| `audits/` | Concise execution audits of work that produced/revised the map (work units, executors, tools, timing, overlap). Generated from runtime records, never from conversation content. |
| `tools/verify_anchors.py` | Re-checks every code anchor in the documents against a checkout. |
| `tools/collect_execution_audit.py` | Regenerates an execution audit from the Hermes state database plus subagent live transcripts. |

Both tools are stdlib-only, read-only, and safe to run on a live machine.

## Anchor grammar (how to read and write the map)

Anchors are inline code spans, optionally followed by the symbol that must be at
that line:

```markdown
`agent/turn_facade.py:22` `run_conversation`   # file:line + symbol assertion
`agent/conversation_loop.py:1479`              # file:line, no symbol assertion
`gateway/platforms/base.py`                    # file exists
```

The symbol assertion (when present) requires the identifier to appear within
±3 lines of the anchor, which catches the common rot mode where the line number
still looks plausible but now points at unrelated code.

A path is resolved in three tiers: repository root → the document's own directory →
a **unique** basename anywhere in the tree (so the delta log can cite another document
as `session-storage.md:150`). An ambiguous basename is reported as a failure, never
guessed. Paths without a line number are treated as *mentions*, not anchors: they are
counted separately and never fail the run, because prose, runtime paths outside the repo
and deliberately quoted stale paths all look like that.

## Verification

```bash
python docs/architecture/tools/verify_anchors.py            # default: this directory
python docs/architecture/tools/verify_anchors.py --json
```

Exit code `0` means every anchor resolved at that checkout. Run it after any
refactor that touches the files named here; a non-zero exit prints the exact
document line and the reason (`file not found` / `line out of range` /
`symbol X not found near line N`).

## Provenance and update protocol

- The map is written against the **pinned baseline commit recorded in
  `execution-map.md` § "Baseline and method"**. Anchors are valid for that tree.
- When you change a file the map names — especially the loop (`agent/turn_*.py`,
  `agent/conversation_loop.py`), tool dispatch (`model_tools.py`,
  `agent/turn_tool_round.py`), gateway turn handling (`gateway/run_turn*.py`), or
  the state facade (`hermes_state*.py`) — update the anchor in the same commit.
  Moving a symbol without fixing its anchors is the failure mode this directory
  exists to prevent.
- If the runtime and the documentation disagree, **the runtime wins**: fix the
  doc, and record the disagreement in `doc-runtime-deltas.md` with both anchors so
  the decision is auditable later.
- Keep the map navigational rather than exhaustive: prefer one verified anchor
  over five unverified ones, and push per-subsystem minutiae to the area
  `AGENTS.md` / developer guide instead of duplicating it here.
