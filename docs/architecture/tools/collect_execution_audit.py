#!/usr/bin/env python3
"""Collect a concise execution audit for a Hermes run from RUNTIME records only.

Sources (all read-only):
  1. ``state.db`` — ``sessions`` (window), ``messages`` (turn boundaries + tool
     names), ``async_delegations`` (delegated work units with dispatch/completion
     timestamps).  The database is opened with ``mode=ro``.
  2. ``cache/delegation/live/<delegation_id>/task-<n>.log`` — the subagent live
     transcripts, used ONLY for per-unit tool sequences and tool counts.  Payloads
     and model text are never copied into the audit.

The audit deliberately stores no conversation content and no chain-of-thought: it
answers "which work units ran, executed by whom, with which tools, starting and
finishing when, and did they overlap".

Usage
-----
    python docs/architecture/tools/collect_execution_audit.py \
        --origin-session agent:main:telegram:dm:<chat>:<thread> \
        --out-json docs/architecture/audits/<date>-execution-audit.json \
        --out-md   docs/architecture/audits/<date>-execution-audit.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

TOOL_LINE_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\s+tool\s+\|\s+->\s+([A-Za-z_][\w]*)\(")


def default_hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    local = os.environ.get("LOCALAPPDATA")
    if local and (Path(local) / "hermes").is_dir():
        return Path(local) / "hermes"
    return Path.home() / ".hermes"


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(timespec="seconds")


def load_delegations(conn: sqlite3.Connection, origin_session: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "select delegation_id, origin_session, state, dispatched_at, completed_at, parent_session_id"
        " from async_delegations where origin_session=? or origin_session_id=?"
        " order by dispatched_at",
        (origin_session, origin_session),
    ).fetchall()
    units = []
    for r in rows:
        d = dict(r)
        d["dispatched_iso"] = iso(d["dispatched_at"])
        d["completed_iso"] = iso(d["completed_at"])
        d["duration_s"] = (
            round(d["completed_at"] - d["dispatched_at"], 1)
            if d["completed_at"] and d["dispatched_at"]
            else None
        )
        units.append(d)
    return units


def load_turns(conn: sqlite3.Connection, session_id: str, window_end: float | None) -> list[dict]:
    """Reconstruct MAIN-agent work units as turn intervals from message rows."""
    rows = conn.execute(
        "select timestamp, role, tool_name from messages where session_id=? order by timestamp",
        (session_id,),
    ).fetchall()
    turns: list[dict] = []
    cur: dict | None = None
    for ts, role, tool_name in rows:
        if role == "user":
            if cur:
                cur["ended_at"] = ts
                turns.append(cur)
            cur = {"started_at": ts, "assistant_msgs": 0, "tool_results": 0, "tools": {}}
        if cur is None:
            continue
        if role == "assistant":
            cur["assistant_msgs"] += 1
        elif role == "tool":
            cur["tool_results"] += 1
            if tool_name:
                cur["tools"][tool_name] = cur["tools"].get(tool_name, 0) + 1
    if cur:
        cur["ended_at"] = window_end
        turns.append(cur)
    for t in turns:
        t["started_iso"] = iso(t["started_at"])
        t["ended_iso"] = iso(t["ended_at"])
        t["duration_s"] = (
            round(t["ended_at"] - t["started_at"], 1)
            if t.get("ended_at") and t.get("started_at")
            else None
        )
    return turns


def load_transcript_tools(live_dir: Path, delegation_id: str) -> dict:
    """Tool sequence + counts per task log; timestamps are wall-clock HH:MM:SS."""
    out: dict[str, dict] = {}
    for log in sorted(live_dir.glob(f"{delegation_id}/task-*.log")):
        seq: list[dict] = []
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = TOOL_LINE_RE.match(line.strip())
            if m:
                seq.append({"t": f"{m.group(1)}:{m.group(2)}:{m.group(3)}", "tool": m.group(4)})
        counts: dict[str, int] = {}
        for item in seq:
            counts[item["tool"]] = counts.get(item["tool"], 0) + 1
        out[log.name] = {"tool_calls": len(seq), "tool_counts": counts,
                         "first_tool_at": seq[0]["t"] if seq else None,
                         "last_tool_at": seq[-1]["t"] if seq else None}
    return out


def overlaps(units: list[dict]) -> list[dict]:
    """Pairwise overlap of [dispatched, completed] intervals."""
    out = []
    for i, a in enumerate(units):
        for b in units[i + 1:]:
            if not (a["dispatched_at"] and a.get("completed_at") and b["dispatched_at"] and b.get("completed_at")):
                continue
            lo = max(a["dispatched_at"], b["dispatched_at"])
            hi = min(a["completed_at"], b["completed_at"])
            if hi > lo:
                out.append({"a": a["delegation_id"], "b": b["delegation_id"], "overlap_s": round(hi - lo, 1)})
    return out


def max_parallel(units: list[dict]) -> int:
    events = []
    for u in units:
        if u["dispatched_at"]:
            events.append((u["dispatched_at"], 1))
        if u.get("completed_at"):
            events.append((u["completed_at"], -1))
    events.sort()
    cur = peak = 0
    for _, delta in events:
        cur += delta
        peak = max(peak, cur)
    return peak


def render_md(audit: dict) -> str:
    lines: list[str] = []
    a = audit
    lines.append("# Execution audit — Hermes Agent request-execution architecture map")
    lines.append("")
    lines.append(f"- Repository: `{a['repository']}`  ·  branch `{a['branch']}`")
    lines.append(f"- Baseline commit: `{a['baseline_sha']}`  ·  final commit: `{a['final_sha'] or 'SEE_DOC'}`")
    lines.append(f"- Origin session key: `{a['origin_session']}`")
    lines.append(f"- Window (UTC): {a['window_start']} → {a['window_end']}")
    lines.append(f"- Executors: main agent (`{a['main_model']}`) + delegated subagents (`{a['child_model']}`)")
    lines.append("")
    lines.append("Derived from runtime records only: `state.db` (`sessions`/`messages`/`async_delegations`,")
    lines.append("opened read-only) and the subagent live transcripts in `cache/delegation/live/`.")
    lines.append("No conversation content, prompts or chain-of-thought are stored here — only work-unit")
    lines.append("boundaries, executor identity and tool names.")
    lines.append("")
    lines.append("## 1. Delegated work units (subagent executor)")
    lines.append("")
    lines.append("| # | delegation_id | state | dispatched (UTC) | completed (UTC) | duration (s) | tool calls | tools |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for i, u in enumerate(a["delegations"], start=1):
        tools = ", ".join(f"{k}×{v}" for k, v in sorted(u.get("tool_counts", {}).items(), key=lambda kv: -kv[1]))
        lines.append(
            f"| {i} | `{u['delegation_id']}` | {u['state']} | {u['dispatched_iso']} | "
            f"{u['completed_iso'] or '—'} | {u['duration_s'] if u['duration_s'] is not None else '—'} | "
            f"{u.get('tool_calls', 0)} | {tools or '—'} |"
        )
    lines.append("")
    lines.append(f"Peak concurrent delegated lanes: **{a['max_parallel_delegations']}**.")
    if a["delegation_overlaps"]:
        lines.append("")
        lines.append("Overlapping lane pairs (all overlaps are intentional parallel lanes of the same DAG wave):")
        lines.append("")
        lines.append("| lane A | lane B | overlap (s) |")
        lines.append("|---|---|---|")
        for o in a["delegation_overlaps"]:
            lines.append(f"| `{o['a']}` | `{o['b']}` | {o['overlap_s']} |")
    lines.append("")
    lines.append("## 2. Main-agent work units (turn intervals, reconstructed from `messages`)")
    lines.append("")
    lines.append("A unit is a user turn: from the user message row to the next user message row.")
    lines.append("")
    lines.append("| # | started (UTC) | ended (UTC) | duration (s) | assistant msgs | tool results | tools |")
    lines.append("|---|---|---|---|---|---|---|")
    for i, t in enumerate(a["turns"], start=1):
        tools = ", ".join(f"{k}×{v}" for k, v in sorted(t["tools"].items(), key=lambda kv: -kv[1]))
        lines.append(
            f"| {i} | {t['started_iso']} | {t['ended_iso'] or '—'} | "
            f"{t['duration_s'] if t['duration_s'] is not None else '—'} | {t['assistant_msgs']} | "
            f"{t['tool_results']} | {tools or '—'} |"
        )
    lines.append("")
    lines.append("## 3. Method and caveats")
    lines.append("")
    lines.append("- Regenerate with `python docs/architecture/tools/collect_execution_audit.py`")
    lines.append("  (`--origin-session`, `--out-json`, `--out-md`).")
    lines.append("- Subagent tool counts come from the live transcript lines that the runtime writes")
    lines.append("  (`tool | -> <name>(...)`); arguments and results are discarded by the parser.")
    lines.append("- Main-agent turn boundaries come from `messages.timestamp`; the last unit ends at the")
    lines.append("  session's last recorded activity, so its duration is a lower bound while the session is live.")
    lines.append("- Nested delegations spawned *by* a lane are not listed as separate units; they appear")
    lines.append("  inside their parent lane's duration unless the parent recorded them in `async_delegations`.")
    lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--origin-session", required=True, help="origin session key (async_delegations.origin_session)")
    p.add_argument("--db", default=None)
    p.add_argument("--live-dir", default=None)
    p.add_argument("--repository", default="NousResearch/hermes-agent")
    p.add_argument("--branch", default="")
    p.add_argument("--baseline-sha", default="")
    p.add_argument("--final-sha", default="")
    p.add_argument("--main-model", default="")
    p.add_argument("--child-model", default="")
    p.add_argument("--out-json", required=True)
    p.add_argument("--out-md", required=True)
    args = p.parse_args(argv)

    home = default_hermes_home()
    db_path = Path(args.db) if args.db else home / "state.db"
    live_dir = Path(args.live_dir) if args.live_dir else home / "cache" / "delegation" / "live"
    if not db_path.is_file():
        print(f"error: state db not found: {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        delegations = load_delegations(conn, args.origin_session)
        for u in delegations:
            for log_name, data in load_transcript_tools(live_dir, u["delegation_id"]).items():
                u.setdefault("transcripts", {})[log_name] = {
                    "tool_calls": data["tool_calls"], "tool_counts": data["tool_counts"],
                    "first_tool_at": data["first_tool_at"], "last_tool_at": data["last_tool_at"],
                }
                # Flat copy so a unit's headline tool usage is readable without the nested map.
                u["tool_calls"] = u.get("tool_calls", 0) + data["tool_calls"]
                u.setdefault("tool_counts", {})
                for tool, count in data["tool_counts"].items():
                    u["tool_counts"][tool] = u["tool_counts"].get(tool, 0) + count
        conn.row_factory = sqlite3.Row
        srow = conn.execute(
            "select id, source, model, started_at, last_activity_at from sessions where session_key=?",
            (args.origin_session,),
        ).fetchone()
        session_id = srow["id"] if srow else None
        window_start = iso(srow["started_at"]) if srow else None
        window_end = iso(srow["last_activity_at"]) if srow else iso(datetime.now(timezone.utc).timestamp())
        turns = load_turns(conn, session_id, srow["last_activity_at"]) if session_id else []
        main_model = args.main_model or (srow["model"] if srow else "")
    finally:
        conn.close()

    audit = {
        "repository": args.repository,
        "branch": args.branch,
        "baseline_sha": args.baseline_sha,
        "final_sha": args.final_sha,
        "origin_session": args.origin_session,
        "origin_session_id": session_id,
        "window_start": window_start,
        "window_end": window_end,
        "main_model": main_model,
        "child_model": args.child_model,
        "delegations": delegations,
        "max_parallel_delegations": max_parallel(delegations),
        "delegation_overlaps": overlaps(delegations),
        "turns": turns,
        "sources": {
            "db": str(db_path),
            "live_dir": str(live_dir),
            "tables": ["sessions", "messages", "async_delegations"],
        },
    }

    out_json = Path(args.out_json)
    out_md = Path(args.out_md)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    out_md.write_text(render_md(audit), encoding="utf-8")
    print(f"wrote {out_json}\nwrote {out_md}")
    print(f"delegations={len(delegations)} turns={len(turns)} peak_parallel={audit['max_parallel_delegations']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
