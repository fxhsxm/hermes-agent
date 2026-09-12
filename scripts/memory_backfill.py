#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""memory_backfill.py -- bounded, resumable BACKFILL PIPELINE for Hindsight memory.

Re-retains Hermes sessions that never reached the Hindsight memory bank.

SAFETY / SCOPE
  * DRY-RUN BY DEFAULT. Nothing is ever written unless `--execute` is passed.
  * Reads state.db strictly read-only (sqlite URI mode=ro). Never writes/deletes.
  * Never writes to the production bank `fwh-main` unless `--execute` is passed
    explicitly with `--bank fwh-main`. Use `--bank fwh-audit-sandbox` to rehearse.
  * Coverage is derived from BANK TAGS (`session:<hermes-session-id>`) -- never from
    document ids (legacy ids 404) and never from /memories/list?tags= (the tags
    query parameter is silently ignored by the API).

WHAT IT DOES
  1. select uncovered sessions: source in {telegram,desktop,cli}, started_at inside
     [--from, --to], >= --min-messages retainable messages, no `session:<id>` tag.
  2. read only role=user / role=assistant rows from state.db `messages`
     (never `tool`, never `session_meta`, never system prompts).
  3. rebuild the plugin's payload shape: a JSON array of turns, each turn a list of
     {"role","content","timestamp"} messages whose content is prefixed
     "User: " / "Assistant: " -- exactly what
     plugins/memory/hindsight/__init__.py::_build_turn_messages produces.
  4. chunk long sessions (<= --chunk-chars of serialized text, <= --turns-per-chunk
     turns per document) instead of one giant blob.
  5. (--execute only) POST /v1/default/banks/<bank>/memories with one item per
     chunk: unique document_id, tags ["session:<sid>", "kind:backfill"], async=true.
  6. poll /operations to a terminal state, record completed/failed per session.
  7. resumable: skips sessions already recorded `completed` in --state; honours
     --limit (sessions) and --max-documents (documents) caps.
  8. writes a PER-RUN document manifest (--manifest, default
     <state dir>/manifests/<bank>-<run start>.json) listing every document id the run
     wrote, so a single run can be rolled back precisely (DELETE .../documents/<id>)
     without touching any earlier run. Never roll back by tag.
  9. dry-run (default) emits the candidate plan to --out with per-session estimated
     token cost, per-day / per-source aggregates and totals.

EXAMPLES
  # dry run (safe, default) over the Sept gap window
  python memory_backfill.py --from 2026-08-15 --to 2026-09-12 --out backfill_candidates.json

  # rehearse a real retain against the throwaway sandbox bank
  python memory_backfill.py --bank fwh-audit-sandbox --limit 3 --execute

  # real capped run (NOT run by the audit; parent decides)
  python memory_backfill.py --execute --limit 20 --max-documents 200
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_DB = r"C:/Users/Fwhne/AppData/Local/hermes/state.db"
DEFAULT_INDEX = r"C:/Users/Fwhne/Documents/HermesOutput/hermes-memory-audit-20260912/memories_index.json"
DEFAULT_API = "http://127.0.0.1:8888"
DEFAULT_BANK = "fwh-main"
SANDBOX_BANK = "fwh-audit-sandbox"
DEFAULT_SOURCES = ("telegram", "desktop", "cli")

# Fact extraction is LLM based: roughly 3.2k input tokens of extraction prompt +
# a few hundred output tokens per document (measured budget from the audit).
BASE_INPUT_TOKENS_PER_DOC = 3200
OUTPUT_TOKENS_PER_DOC = 300

USER_PREFIX = "User"
ASSISTANT_PREFIX = "Assistant"


# --------------------------------------------------------------------------- utils

def log(msg: str) -> None:
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def day_local(ts: float) -> str:
    """Local (UTC+08 on this host) calendar day for an epoch float."""
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def iso_local(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def iso_utc(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="milliseconds")


def parse_day(text: str, end: bool = False) -> float:
    d = dt.datetime.strptime(text, "%Y-%m-%d")
    if end:
        d = d + dt.timedelta(days=1)
    return d.timestamp()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class TokenCounter:
    """Token estimate: tiktoken (o200k_base, matches modern extraction models) when
    available, else a chars/3.5 heuristic. Never fatal."""

    def __init__(self) -> None:
        self.enc = None
        self.name = "heuristic(chars/3.5)"
        try:
            import tiktoken  # type: ignore
            try:
                self.enc = tiktoken.get_encoding("o200k_base")
                self.name = "tiktoken:o200k_base"
            except Exception:
                self.enc = tiktoken.get_encoding("cl100k_base")
                self.name = "tiktoken:cl100k_base"
        except Exception:
            self.enc = None

    def __call__(self, text: str) -> int:
        if not text:
            return 0
        if self.enc is not None:
            try:
                return len(self.enc.encode(text))
            except Exception:
                pass
        return max(1, int(round(len(text) / 3.5)))


# ------------------------------------------------------------------------ http api

class Api:
    """Thin Hindsight REST client with retry/backoff (the API may be restarted
    mid-run by another workstream -> ~30s of connection refusals)."""

    def __init__(self, base: str, timeout: float = 30.0, attempts: int = 6) -> None:
        import httpx  # local import so --help works without httpx
        self._httpx = httpx
        self.base = base.rstrip("/")
        self.attempts = attempts
        self.client = httpx.Client(timeout=timeout, headers={"Accept": "application/json"})

    def _request(self, method: str, path: str, **kw) -> Any:
        url = f"{self.base}{path}"
        last: Optional[Exception] = None
        for attempt in range(1, self.attempts + 1):
            try:
                r = self.client.request(method, url, **kw)
                if r.status_code >= 500:
                    raise self._httpx.HTTPStatusError(
                        f"server {r.status_code}", request=r.request, response=r)
                return r
            except (self._httpx.TransportError, self._httpx.HTTPStatusError) as exc:
                last = exc
                if isinstance(exc, self._httpx.HTTPStatusError) and exc.response is not None \
                        and exc.response.status_code < 500:
                    raise
                if attempt == self.attempts:
                    break
                backoff = min(30.0, 2.0 ** attempt)
                log(f"  ! {method} {path} failed ({exc.__class__.__name__}); retry {attempt}/{self.attempts} in {backoff:.0f}s")
                time.sleep(backoff)
        raise RuntimeError(f"{method} {path} failed after {self.attempts} attempts: {last}")

    def get_json(self, path: str, **params) -> Any:
        r = self._request("GET", path, params={k: v for k, v in params.items() if v is not None})
        r.raise_for_status()
        return r.json()

    def post_json(self, path: str, body: dict) -> Tuple[int, Any]:
        r = self._request("POST", path, json=body)
        try:
            payload = r.json()
        except Exception:
            payload = {"raw": r.text[:500]}
        return r.status_code, payload

    def reachable(self) -> bool:
        try:
            self.client.get(f"{self.base}/health", timeout=5)
            return True
        except Exception:
            try:
                self.client.get(f"{self.base}/openapi.json", timeout=5)
                return True
            except Exception:
                return False


# --------------------------------------------------------------------- coverage

def bank_session_tags(api: Api, bank: str, page: int = 500) -> Tuple[set, dict]:
    """Full paginated /memories/list walk -> set of `session:<id>` tags.

    This is the *only* reliable coverage signal: GET .../memories/list?tags=...
    silently ignores the filter, and GET /documents/<legacy-id> 404s.
    """
    tags: set = set()
    stats = {"pages": 0, "items": 0, "total": None}
    offset = 0
    while True:
        d = api.get_json(f"/v1/default/banks/{bank}/memories/list", limit=page, offset=offset)
        items = d.get("items") or []
        stats["pages"] += 1
        stats["items"] += len(items)
        stats["total"] = d.get("total", stats["total"])
        for it in items:
            for t in (it.get("tags") or []):
                if isinstance(t, str) and t.startswith("session:"):
                    tags.add(t)
        if not items or (stats["total"] is not None and stats["items"] >= stats["total"]):
            break
        offset += len(items)
    return tags, stats


def observation_scope_tags(api: Api, bank: str) -> set:
    """Secondary coverage source: /observations/scopes (tags + counts)."""
    out: set = set()
    try:
        d = api.get_json(f"/v1/default/banks/{bank}/observations/scopes")
    except Exception as exc:
        log(f"  ! observations/scopes unavailable: {exc}")
        return out
    scopes = d.get("scopes") if isinstance(d, dict) else d
    if not isinstance(scopes, list):
        return out
    for sc in scopes:
        if isinstance(sc, dict):
            for t in (sc.get("tags") or []):
                if isinstance(t, str) and t.startswith("session:"):
                    out.add(t)
    return out


def bank_backfill_progress(api: Api, bank: str, page: int = 500) -> Dict[str, dict]:
    """Per-session chunk progress, read from the BANK (not from our own bookkeeping).

    Every backfilled document id is `bf-<session>-cNNN-<hash>` and carries `chunk_count`
    in its metadata, so this walk yields exactly which chunk indexes exist for a session.
    Needed because a batch that stops at --max-documents leaves a session tagged (covered)
    but incomplete: tag-based coverage then hides its missing tail forever.

    Returns {session_id: {"written": {idx, ...}, "chunk_count": N, "documents": N}}.
    """
    out: Dict[str, dict] = {}
    offset = 0
    seen = 0
    while True:
        d = api.get_json(f"/v1/default/banks/{bank}/documents", limit=page, offset=offset)
        items = d.get("items") or []
        for it in items:
            did = str(it.get("id") or "")
            md = it.get("document_metadata")
            if isinstance(md, str):
                try:
                    import ast
                    md = ast.literal_eval(md)
                except Exception:
                    md = {}
            md = md or {}
            m = re.match(r"^bf-(.+)-c(\d+)-([0-9a-f]+)$", did)
            if m:
                sid, idx = m.group(1), int(m.group(2))
            elif str(md.get("source", "")).lower() == "backfill":
                sid, idx = str(md.get("hermes_session_id") or "?"), int(md.get("chunk_index") or -1)
            else:
                continue
            rec = out.setdefault(sid, {"written": set(), "chunk_count": 0, "documents": 0})
            rec["written"].add(idx)
            rec["documents"] = len(rec["written"])
            try:
                rec["chunk_count"] = max(int(md.get("chunk_count") or 0), rec["chunk_count"])
            except Exception:
                pass
        seen += len(items)
        if not items or (d.get("total") is not None and seen >= d["total"]):
            break
        offset += len(items)
    return out


def index_tags(path: str) -> set:
    """Index-only fallback (used when the API is unreachable)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return set()
    out = set()
    for it in data or []:
        for t in (it.get("tags") or []):
            if isinstance(t, str) and t.startswith("session:"):
                out.add(t)
    return out


# ------------------------------------------------------------------- session read

def open_db_ro(path: str) -> sqlite3.Connection:
    uri = "file:" + path.replace("\\", "/") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def fetch_sessions(con: sqlite3.Connection, sources: Iterable[str], start: float, end: float) -> List[sqlite3.Row]:
    q = ("SELECT id, source, started_at, message_count, tool_call_count, parent_session_id, title "
         "FROM sessions WHERE source IN (%s) AND started_at >= ? AND started_at < ? "
         "ORDER BY started_at" % ",".join("?" * len(tuple(sources))))
    src = tuple(sources)
    return list(con.execute(q, (*src, start, end)))


def fetch_transcript(con: sqlite3.Connection, sid: str) -> List[sqlite3.Row]:
    """role user/assistant only -- tool results, tool calls and system/session_meta
    rows are never read into the payload."""
    return list(con.execute(
        "SELECT id, role, content, timestamp FROM messages "
        "WHERE session_id = ? AND role IN ('user','assistant') ORDER BY timestamp, id", (sid,)))


def build_turns(rows: List[sqlite3.Row]) -> Tuple[List[dict], dict]:
    """Turn = one user message + the assistant message(s) that follow it, matching
    the plugin's per-turn (user_content, assistant_content) pairing."""
    turns: List[dict] = []
    dq = {"empty_content": 0, "assistant_without_user": 0, "user_without_assistant": 0,
          "truncated_chars": 0}
    cur: Optional[dict] = None
    for r in rows:
        text = (r["content"] or "").strip()
        if not text:
            dq["empty_content"] += 1
            continue
        ts = iso_utc(r["timestamp"])
        if r["role"] == "user":
            if cur is not None:
                turns.append(cur)
            cur = {"user": text, "user_ts": ts, "assistant": "", "assistant_ts": ts}
        else:
            if cur is None:
                dq["assistant_without_user"] += 1
                cur = {"user": "", "user_ts": ts, "assistant": text, "assistant_ts": ts}
            else:
                cur["assistant"] = (cur["assistant"] + "\n\n" + text) if cur["assistant"] else text
                cur["assistant_ts"] = ts
    if cur is not None:
        turns.append(cur)
    for t in turns:
        if t["user"] and not t["assistant"]:
            dq["user_without_assistant"] += 1
    return turns, dq


def turn_json(turn: dict) -> str:
    """Plugin-shaped turn: JSON list of role/content/timestamp dicts with the
    literal 'User: ' / 'Assistant: ' content prefixes."""
    msgs = []
    if turn["user"]:
        msgs.append({"role": "user", "content": f"{USER_PREFIX}: {turn['user']}", "timestamp": turn["user_ts"]})
    if turn["assistant"]:
        msgs.append({"role": "assistant", "content": f"{ASSISTANT_PREFIX}: {turn['assistant']}", "timestamp": turn["assistant_ts"]})
    return json.dumps(msgs, ensure_ascii=False)


def split_oversized(turn: dict, budget: int) -> List[dict]:
    """A single turn whose serialization exceeds the chunk budget is split at the
    text level so no document ever blows past the limit."""
    if len(turn_json(turn)) <= budget:
        return [turn]
    out: List[dict] = []
    # Split user then assistant halves; roughly half the budget for text, the rest
    # is JSON/prefix overhead.
    text_budget = max(400, int(budget * 0.45))
    for side in ("user", "assistant"):
        text = turn[side]
        ts_key = f"{side}_ts"
        while text:
            piece, text = text[:text_budget], text[text_budget:]
            part = {"user": "", "user_ts": turn["user_ts"], "assistant": "", "assistant_ts": turn["assistant_ts"]}
            part[side] = piece
            part[ts_key] = turn[ts_key]
            out.append(part)
    return out or [turn]


def chunk_session(turns: List[dict], chunk_chars: int, turns_per_chunk: int) -> List[dict]:
    """Greedily pack turns into documents bounded by chars AND turns-per-document."""
    flat: List[dict] = []
    for t in turns:
        flat.extend(split_oversized(t, chunk_chars))
    chunks: List[dict] = []
    cur: List[str] = []
    cur_len = 2  # "[]"
    for t in flat:
        s = turn_json(t)
        extra = len(s) + (1 if cur else 0)
        if cur and (cur_len + extra > chunk_chars or len(cur) >= turns_per_chunk):
            chunks.append({"turns": cur, "content": "[" + ",".join(cur) + "]", "start_ts": json.loads(cur[0])[0].get("timestamp") if cur else None})
            cur, cur_len = [], 2
            extra = len(s)
        cur.append(s)
        cur_len += extra
    if cur:
        chunks.append({"turns": cur, "content": "[" + ",".join(cur) + "]", "start_ts": json.loads(cur[0])[0].get("timestamp") if cur else None})
    return chunks


def document_id_for(sid: str, index: int, content: str) -> str:
    """Deterministic + unique per chunk: resuming a partially failed session reuses
    the same ids for identical content (append/overwrite is idempotent)."""
    h = hashlib.sha1(content.encode("utf-8")).hexdigest()[:8]
    return f"bf-{sid}-c{index:03d}-{h}"


# ------------------------------------------------------------------- state file

class State:
    """Resumable JSON state: sessions already `completed` are skipped forever."""

    def __init__(self, path: str, bank: str) -> None:
        self.path = path
        self.bank = bank
        self.data: dict = {"version": 1, "bank": bank, "created_at": now_iso(),
                           "updated_at": now_iso(), "sessions": {}, "runs": []}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict) and "sessions" in loaded:
                    self.data = loaded
                    self.data.setdefault("runs", [])
            except Exception as exc:
                log(f"  ! state file unreadable ({exc}); starting fresh state")

    def status(self, sid: str) -> Optional[str]:
        rec = self.data["sessions"].get(sid)
        return rec.get("status") if isinstance(rec, dict) else None

    def completed_sessions(self) -> set:
        return {k for k, v in self.data["sessions"].items()
                if isinstance(v, dict) and v.get("status") == "completed"}

    def record(self, sid: str, status: str, **extra) -> None:
        rec = self.data["sessions"].setdefault(sid, {})
        rec.update(extra)
        rec["status"] = status
        rec["updated_at"] = now_iso()
        self.flush()

    def start_run(self, info: dict) -> dict:
        run = {"started_at": now_iso(), **info}
        self.data["runs"].append(run)
        self.flush()
        return run

    def flush(self) -> None:
        self.data["updated_at"] = now_iso()
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)


# ------------------------------------------------------------------- planning

def plan_session(con: sqlite3.Connection, row: sqlite3.Row, counter: TokenCounter,
                 chunk_chars: int, turns_per_chunk: int) -> dict:
    sid = row["id"]
    rows = fetch_transcript(con, sid)
    turns, dq = build_turns(rows)
    chunks = chunk_session(turns, chunk_chars, turns_per_chunk) if turns else []
    payload_chars = sum(len(c["content"]) for c in chunks)
    content_tokens = sum(counter(c["content"]) for c in chunks)
    docs = len(chunks)
    est_input = docs * BASE_INPUT_TOKENS_PER_DOC + content_tokens
    est_output = docs * OUTPUT_TOKENS_PER_DOC
    user_msgs = sum(1 for r in rows if r["role"] == "user" and (r["content"] or "").strip())
    asst_msgs = sum(1 for r in rows if r["role"] == "assistant" and (r["content"] or "").strip())
    retainable = user_msgs + asst_msgs
    return {
        "session_id": sid,
        "source": row["source"],
        "day": day_local(row["started_at"]),
        "started_at": iso_local(row["started_at"]),
        "title": (row["title"] or "")[:80],
        "parent_session_id": row["parent_session_id"],
        "is_top_level": row["parent_session_id"] is None,
        "session_message_count": row["message_count"] or 0,
        "tool_call_count": row["tool_call_count"] or 0,
        "user_messages": user_msgs,
        "assistant_messages": asst_msgs,
        "retainable_messages": retainable,
        # Two definitions, both reported so the parent can cap either way:
        #   has_6plus_messages       -> >= 6 retainable (user+assistant, non-empty) msgs
        #   has_6plus_session_count  -> >= 6 rows per state.db sessions.message_count
        "has_6plus_messages": retainable >= 6,
        "has_6plus_session_count": (row["message_count"] or 0) >= 6,
        "assistant_rows_empty_content": dq["empty_content"],
        "turns": len(turns),
        "documents": docs,
        "payload_chars": payload_chars,
        "content_tokens": content_tokens,
        "est_input_tokens": est_input,
        "est_output_tokens": est_output,
        "est_total_tokens": est_input + est_output,
        "skipped_empty_content": dq["empty_content"],
        "user_without_assistant": dq["user_without_assistant"],
        "assistant_without_user": dq["assistant_without_user"],
    }


def aggregate(rows: List[dict]) -> dict:
    def block(items: List[dict]) -> dict:
        return {
            "sessions": len(items),
            "sessions_6plus": sum(1 for r in items if r["has_6plus_messages"]),
            "sessions_6plus_by_session_message_count": sum(1 for r in items if r["has_6plus_session_count"]),
            "turns": sum(r["turns"] for r in items),
            "documents": sum(r["documents"] for r in items),
            "user_messages": sum(r["user_messages"] for r in items),
            "assistant_messages": sum(r["assistant_messages"] for r in items),
            "retainable_messages": sum(r["retainable_messages"] for r in items),
            "payload_chars": sum(r["payload_chars"] for r in items),
            "est_input_tokens": sum(r["est_input_tokens"] for r in items),
            "est_output_tokens": sum(r["est_output_tokens"] for r in items),
            "est_total_tokens": sum(r["est_total_tokens"] for r in items),
        }
    return block(rows)


# ------------------------------------------------------------------- execution

def poll_operations(api: Api, bank: str, op_ids: set, timeout: float, interval: float = 3.0) -> dict:
    """Poll /operations until every tracked op reaches completed/failed (or timeout)."""
    pending = set(op_ids)
    results: Dict[str, dict] = {}
    deadline = time.time() + timeout
    while pending and time.time() < deadline:
        try:
            data = api.get_json(f"/v1/default/banks/{bank}/operations", limit=100)
        except Exception as exc:
            log(f"  ! operations poll failed ({exc}); retrying")
            time.sleep(interval)
            continue
        ops = data.get("operations") if isinstance(data, dict) else data
        for op in ops or []:
            oid = op.get("operation_id") or op.get("id")
            if oid in pending:
                st = (op.get("status") or "").lower()
                if st in ("completed", "failed", "cancelled", "canceled", "expired"):
                    results[oid] = {"status": st, "error_message": op.get("error_message") or op.get("error")}
                    pending.discard(oid)
        if pending:
            time.sleep(interval)
    for oid in pending:
        results[oid] = {"status": "timeout", "error_message": f"not terminal within {timeout:.0f}s"}
    return results


def default_manifest_path(args, run: dict) -> str:
    """<state dir>/manifests/<bank>-<run start, seconds>.json -- beside the state file it
    summarises, so a run and its manifest are found together."""
    stamp = "".join(ch for ch in run["started_at"] if ch.isdigit())[:14]
    base = os.path.dirname(os.path.abspath(args.state))
    return os.path.join(base, "manifests", f"{args.bank}-{stamp}.json")


def write_manifest(path: str, args, run: dict, sessions: List[dict], doc_ids: List[str],
                   summary: dict) -> None:
    """Per-run document manifest: the exact document ids THIS run wrote.

    The state file records doc ids per SESSION; this file records them per RUN, so one
    batch can be rolled back without touching any other batch. Rollback is precise
    (DELETE /v1/default/banks/<bank>/documents/<document_id>) or by restoring the run's
    own backup zip -- never by tag, which would also delete the earlier batches that
    already passed their gate.
    """
    doc = {
        "version": 1,
        "bank": args.bank,
        "run_started_at": run["started_at"],
        "run_ended_at": run.get("ended_at"),
        "range": {"from": args.date_from, "to": args.date_to},
        "caps": {"limit": args.limit, "max_documents": args.max_documents,
                 "chunk_chars": args.chunk_chars, "turns_per_chunk": args.turns_per_chunk},
        "summary": summary,
        "sessions": sessions,
        "document_ids": doc_ids,
        "document_count": len(doc_ids),
        "rollback": {
            "preferred": "restore the backup zip taken immediately before this run",
            "precise": ("DELETE /v1/default/banks/%s/documents/<document_id> for each id in "
                        "document_ids" % args.bank),
            "forbidden": ("deleting by the kind:backfill tag would also delete every earlier "
                          "batch, including the ones that passed their gate"),
        },
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def execute(api: Api, con: sqlite3.Connection, rows: List[dict], args, state: State,
            counter: TokenCounter) -> dict:
    bank = args.bank
    if bank == DEFAULT_BANK and not args.i_accept_production_writes:
        log("REFUSING: --execute against the production bank needs --i-accept-production-writes "
            "(or point --bank at fwh-audit-sandbox).")
        return {"aborted": "production guard"}
    log(f"EXECUTE: bank={bank} sessions={len(rows)} max_documents={args.max_documents or '-'}")

    submit_url = f"/v1/default/banks/{bank}/memories"
    run = state.start_run({"bank": bank, "from": args.date_from, "to": args.date_to,
                           "candidate_sessions": len(rows), "max_documents": args.max_documents})
    done_docs = 0
    summary = {"completed": 0, "failed": 0, "skipped": 0, "documents": 0, "ops_completed": 0, "ops_failed": 0}
    # Per-run manifest accumulators: the doc ids THIS run wrote, so a single batch can be
    # rolled back precisely without touching any earlier batch.
    manifest_sessions: List[dict] = []
    manifest_doc_ids: List[str] = []
    progress_failures: List[str] = []

    for item in rows:
        sid = item["session_id"]
        if state.status(sid) == "completed":
            summary["skipped"] += 1
            manifest_sessions.append({"session_id": sid, "status": "already_completed_in_state",
                                      "documents": 0, "doc_ids": []})
            continue
        if args.max_documents is not None and done_docs >= args.max_documents:
            log("max-documents cap reached; stopping")
            break

        mrows = fetch_transcript(con, sid)
        turns, _dq = build_turns(mrows)
        chunks = chunk_session(turns, args.chunk_chars, args.turns_per_chunk) if turns else []
        if not chunks:
            state.record(sid, "skipped", reason="no user/assistant content", run=run["started_at"])
            summary["skipped"] += 1
            manifest_sessions.append({"session_id": sid, "status": "skipped",
                                      "reason": "no user/assistant content",
                                      "documents": 0, "doc_ids": []})
            continue

        op_ids: set = set()
        doc_ids: List[str] = []
        errors: List[str] = []
        # --resume-partial: submit ONLY the chunk indexes the bank is missing, so a resumed
        # session neither duplicates its existing chunks nor re-pays for them.
        partial = item.get("partial") or {}
        only_indexes = set(partial["missing_chunk_indexes"]) if partial else None
        chunks_skipped = 0
        done_before = done_docs
        for idx, ch in enumerate(chunks):
            if only_indexes is not None and idx not in only_indexes:
                chunks_skipped += 1
                continue
            if args.max_documents is not None and done_docs >= args.max_documents:
                errors.append("stopped by --max-documents before this chunk")
                break
            doc_id = document_id_for(sid, idx, ch["content"])
            body = {
                "items": [{
                    "content": ch["content"],
                    "tags": [f"session:{sid}", "kind:backfill"],
                    "document_id": doc_id,
                    "metadata": {"retained_at": now_iso(), "source": "backfill",
                                 "hermes_session_id": sid, "session_source": item["source"],
                                 "chunk_index": str(idx), "chunk_count": str(len(chunks))},
                    # Historical content must keep its ORIGINAL event time: the server derives
                    # occurred_*/mentioned_at from the item timestamp, so stamping "now" would make an
                    # August decision look newer than today's facts and mislead temporal reasoning.
                    "timestamp": ch.get("start_ts") or item.get("started_iso") or iso_utc(dt.datetime.now(dt.timezone.utc).timestamp()),
                }],
                "async": True,
            }
            status, payload = api.post_json(submit_url, body)
            if status >= 400:
                msg = f"HTTP {status} chunk {idx}: {json.dumps(payload, ensure_ascii=False)[:300]}"
                log(f"  FAIL {sid} {msg}")
                errors.append(msg)
                break
            for key in ("operation_id", "operation_ids"):
                v = payload.get(key) if isinstance(payload, dict) else None
                if isinstance(v, str):
                    op_ids.add(v)
                elif isinstance(v, list):
                    op_ids.update(str(x) for x in v)
            doc_ids.append(doc_id)
            done_docs += 1
            if args.sleep:
                time.sleep(args.sleep)

        if partial and not errors:
            # FAIL-CLOSED: a resumed session must actually submit every missing chunk (up to the
            # document cap). Submitting fewer means the resume silently did not do its job.
            cap_left = None if args.max_documents is None else max(0, args.max_documents - done_before)
            expected = len(only_indexes) if cap_left is None else min(len(only_indexes), cap_left)
            if len(doc_ids) < expected:
                msg = (f"resume progress failure: submitted {len(doc_ids)} of {expected} missing "
                       f"chunks")
                errors.append(msg)
                progress_failures.append(f"{sid}: {msg}")

        ops = poll_operations(api, bank, op_ids, args.poll_timeout) if op_ids else {}
        n_failed = sum(1 for v in ops.values() if v["status"] != "completed")
        summary["ops_completed"] += len(ops) - n_failed
        summary["ops_failed"] += n_failed
        for oid, v in ops.items():
            if v["status"] != "completed":
                errors.append(f"op {oid}: {v['status']} {v.get('error_message') or ''}".strip())
        summary["documents"] += len(doc_ids)

        if errors:
            state.record(sid, "failed", documents=len(doc_ids), doc_ids=doc_ids,
                         errors=errors[:10], run=run["started_at"])
            summary["failed"] += 1
            log(f"  FAIL {sid}: {errors[0][:140]}")
            manifest_sessions.append({"session_id": sid, "source": item["source"],
                                      "status": "failed", "documents": len(doc_ids),
                                      "doc_ids": doc_ids, "errors": errors[:5],
                                      "resumed": bool(partial),
                                      "chunks_skipped_already_in_bank": chunks_skipped})
        else:
            state.record(sid, "completed", documents=len(doc_ids), doc_ids=doc_ids,
                         turns=len(turns), est_total_tokens=item["est_total_tokens"],
                         run=run["started_at"])
            summary["completed"] += 1
            log(f"  ok   {sid} ({len(doc_ids)} docs"
                + (f", resumed: {chunks_skipped} chunks already in bank)" if partial else ")"))
            manifest_sessions.append({"session_id": sid, "source": item["source"],
                                      "status": "completed", "documents": len(doc_ids),
                                      "doc_ids": doc_ids, "turns": len(turns),
                                      "est_total_tokens": item["est_total_tokens"],
                                      "resumed": bool(partial),
                                      "chunks_skipped_already_in_bank": chunks_skipped})
        manifest_doc_ids.extend(doc_ids)

    run.update({"ended_at": now_iso(), "summary": summary, "documents_sent": done_docs,
                "progress_failures": progress_failures})
    summary["progress_failures"] = progress_failures
    state.flush()
    manifest_path = args.manifest or default_manifest_path(args, run)
    write_manifest(manifest_path, args, run, manifest_sessions, manifest_doc_ids, summary)
    run["manifest"] = manifest_path
    run["manifest_document_ids"] = len(manifest_doc_ids)
    state.flush()
    log(f"manifest: {manifest_path} ({len(manifest_doc_ids)} document ids, "
        f"{len(manifest_sessions)} sessions)")
    return summary


# -------------------------------------------------------------------- reporting

def build_report(args, cov: dict, sessions_meta: dict, all_plans: List[dict], candidates: List[dict],
                 counter: TokenCounter) -> dict:
    """all_plans = EVERY uncovered in-range session (the population);
    candidates = the subset selected for this run (post --min-messages / --limit)."""
    cand_ids = {r["session_id"] for r in candidates}
    per_day: Dict[str, dict] = {}
    per_source: Dict[str, dict] = {}
    for r in all_plans:
        for bucket, key in ((per_day, r["day"]), (per_source, r["source"])):
            b = bucket.setdefault(key, {"sessions": 0, "sessions_6plus": 0,
                                        "sessions_6plus_by_session_message_count": 0,
                                        "sessions_selected": 0, "messages": 0,
                                        "retainable_messages": 0, "turns": 0, "documents": 0,
                                        "est_total_tokens": 0, "by_source": {}})
            b["sessions"] += 1
            b["sessions_6plus"] += 1 if r["has_6plus_messages"] else 0
            b["sessions_6plus_by_session_message_count"] += 1 if r["has_6plus_session_count"] else 0
            b["sessions_selected"] += 1 if r["session_id"] in cand_ids else 0
            b["messages"] += r["session_message_count"]
            b["retainable_messages"] += r["retainable_messages"]
            b["turns"] += r["turns"]
            b["documents"] += r["documents"]
            b["est_total_tokens"] += r["est_total_tokens"]
            if bucket is per_day:
                b["by_source"][r["source"]] = b["by_source"].get(r["source"], 0) + 1

    totals = aggregate(candidates)
    top20 = sorted(candidates, key=lambda r: (-r["retainable_messages"], r["session_id"]))[:20]
    capped = aggregate(top20)

    all_hist = {}
    for r in sessions_meta["all_uncovered_rows"]:
        all_hist[r["day"]] = all_hist.get(r["day"], 0) + 1

    dq = {
        "population": "all uncovered in-range sessions (before --min-messages / --limit)",
        "sessions_with_zero_retainable_messages": [r["session_id"] for r in all_plans if r["retainable_messages"] == 0],
        "sessions_with_empty_content_rows": sum(1 for r in all_plans if r["skipped_empty_content"]),
        "total_empty_content_rows": sum(r["skipped_empty_content"] for r in all_plans),
        "sessions_user_without_assistant": sum(1 for r in all_plans if r["user_without_assistant"]),
        "total_user_without_assistant_turns": sum(r["user_without_assistant"] for r in all_plans),
        "sessions_assistant_without_user": sum(1 for r in all_plans if r["assistant_without_user"]),
        "total_assistant_without_user_turns": sum(r["assistant_without_user"] for r in all_plans),
        "sessions_marked_message_count_but_no_user_assistant": [
            r["session_id"] for r in all_plans
            if r["session_message_count"] > 0 and r["retainable_messages"] == 0],
        "sessions_with_parent_session_id": sum(1 for r in all_plans if r["parent_session_id"]),
        "tokens_column_note": sessions_meta["tokens_column_note"],
    }

    return {
        "generated_at": now_iso(),
        "mode": "dry-run",
        "api_base": args.api,
        "bank": args.bank,
        "range": {"from": args.date_from, "to": args.date_to,
                  "tz": "local (UTC+%02d)" % (-time.timezone // 3600)},
        "filters": {"sources": list(args.sources), "min_messages": args.min_messages,
                    "chunk_chars": args.chunk_chars, "turns_per_chunk": args.turns_per_chunk},
        "estimator": {
            "tokenizer": counter.name,
            "base_input_tokens_per_document": BASE_INPUT_TOKENS_PER_DOC,
            "output_tokens_per_document": OUTPUT_TOKENS_PER_DOC,
            "note": ("est_input = documents*3200 + tokens(content); "
                     "est_output = documents*300. Measured LLM fact-extraction budget."),
        },
        "coverage": cov,
        "selection": {
            "eligible_sessions_in_range": sessions_meta["eligible_in_range"],
            "already_covered_in_range": sessions_meta["covered_in_range"],
            "uncovered_in_range_total": sessions_meta["uncovered_in_range_total"],
            "selected": len(candidates),
            "selected_6plus": sum(1 for r in candidates if r["has_6plus_messages"]),
            "selected_6plus_by_session_message_count": sum(1 for r in candidates if r["has_6plus_session_count"]),
            "excluded_by_min_messages": len(all_plans) - len(candidates),
            "population_uncovered_in_range": len(all_plans),
            "population_6plus": sum(1 for r in all_plans if r["has_6plus_messages"]),
            "population_6plus_by_session_message_count": sum(1 for r in all_plans if r["has_6plus_session_count"]),
            "top_level_only": args.top_level_only,
            "order": args.order,
            "limit": args.limit,
            "max_documents": args.max_documents,
        },
        "totals_full_run": totals,
        "capped_run_20_largest": {"cap": 20, "sessions": [r["session_id"] for r in top20],
                                  "by_session": [{"session_id": r["session_id"], "source": r["source"],
                                                  "day": r["day"], "retainable_messages": r["retainable_messages"],
                                                  "documents": r["documents"],
                                                  "est_total_tokens": r["est_total_tokens"]} for r in top20],
                                  "totals": capped},
        "per_day": {k: per_day[k] for k in sorted(per_day)},
        "per_source": {k: per_source[k] for k in sorted(per_source)},
        "data_quality": dq,
        "history_context": {
            "note": "uncovered telegram/desktop/cli sessions per local day across ALL of state.db (outside the selected range is informational only)",
            "all_uncovered_total": len(sessions_meta["all_uncovered_rows"]),
            "per_day": {k: all_hist[k] for k in sorted(all_hist)},
        },
        "sessions": candidates,
        "resume_state_file": args.state,
    }


# ------------------------------------------------------------------------- main

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Resumable Hindsight backfill for uncovered Hermes sessions (dry-run by default).")
    p.add_argument("--db", default=DEFAULT_DB, help="state.db path (opened mode=ro)")
    p.add_argument("--api", default=DEFAULT_API, help="Hindsight API base URL")
    p.add_argument("--bank", default=DEFAULT_BANK, help=f"target bank (default {DEFAULT_BANK}; use {SANDBOX_BANK} to rehearse)")
    p.add_argument("--index", default=DEFAULT_INDEX, help="memories_index.json fallback for coverage")
    p.add_argument("--from", dest="date_from", default="2026-08-15", help="inclusive start day (local), YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", default="2026-09-12", help="inclusive end day (local), YYYY-MM-DD")
    p.add_argument("--sources", default=",".join(DEFAULT_SOURCES), help="comma separated session sources")
    p.add_argument("--min-messages", type=int, default=1, help="min retainable (user+assistant) messages to select")
    p.add_argument("--limit", type=int, default=None, help="max sessions to process this run")
    p.add_argument("--max-documents", type=int, default=None, help="max documents (chunks) to submit this run")
    p.add_argument("--order", choices=["oldest", "newest", "largest"], default="oldest")
    p.add_argument("--top-level-only", action="store_true", help="skip sessions that have a parent_session_id")
    p.add_argument("--resume-partial", action="store_true",
                   help="also resume sessions that are already tagged but INCOMPLETE (a previous "
                        "batch stopped at --max-documents mid-session): re-enter them and write only "
                        "the chunk indexes that are missing from the bank. FAIL-CLOSED: if the "
                        "bank's progress cannot be read, or a session's chunking no longer matches, "
                        "the run refuses to write and exits 2")
    p.add_argument("--chunk-chars", type=int, default=3000, help="max serialized chars per document")
    p.add_argument("--turns-per-chunk", type=int, default=6, help="max turns per document")
    p.add_argument("--execute", action="store_true", help="ACTUALLY submit retains (default: dry-run)")
    p.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="explicitly force dry-run (this is already the default; mutually exclusive with --execute)")
    p.add_argument("--i-accept-production-writes", action="store_true",
                   help=f"required alongside --execute when --bank is {DEFAULT_BANK}")
    p.add_argument("--state", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "backfill_state.json"),
                   help="resumable JSON state file")
    p.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "backfill_candidates.json"),
                   help="dry-run candidate report output path")
    p.add_argument("--manifest", default=None,
                   help="per-run document manifest (the doc ids this run wrote) for precise "
                        "rollback; default <state dir>/manifests/<bank>-<run start>.json")
    p.add_argument("--sleep", type=float, default=0.4, help="throttle between submits (execute only)")
    p.add_argument("--poll-timeout", type=float, default=900.0, help="seconds to wait for async ops")
    p.add_argument("--no-api", action="store_true", help="skip API calls entirely (index-only coverage)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.dry_run and args.execute:
        log("--dry-run and --execute are mutually exclusive")
        return 2
    args.sources = tuple(s.strip() for s in args.sources.split(",") if s.strip())
    counter = TokenCounter()
    start, end = parse_day(args.date_from), parse_day(args.date_to, end=True)

    log(f"range {args.date_from} -> {args.date_to} (local), sources={list(args.sources)}, "
        f"min_messages={args.min_messages}, tokenizer={counter.name}")

    # ---- coverage -------------------------------------------------------
    api = Api(args.api)
    cov: dict = {"session_tag_count": 0, "sources_used": [], "degraded": False}
    tags_fresh: set = set()
    if args.no_api:
        cov["degraded"] = True
    else:
        try:
            tags_fresh, stats = bank_session_tags(api, args.bank)
            cov["memories_list_walk"] = stats
            cov["sources_used"].append(f"GET /v1/default/banks/{args.bank}/memories/list (paged)")
        except Exception as exc:
            log(f"  ! fresh coverage fetch failed: {exc}")
            cov["degraded"] = True
            cov["fresh_fetch_error"] = str(exc)[:300]
        scope_tags = observation_scope_tags(api, args.bank)
        if scope_tags:
            cov["sources_used"].append(f"GET /v1/default/banks/{args.bank}/observations/scopes")
            cov["observations_scopes_session_tags"] = len(scope_tags)
            tags_fresh |= scope_tags

    tags_index = index_tags(args.index)
    if tags_index and not tags_fresh:
        cov["degraded"] = True
        cov["sources_used"].append("local memories_index.json (API unavailable)")
    cov["index_session_tags"] = len(tags_index)
    cov["api_session_tags"] = len(tags_fresh)
    cov["index_only_tags"] = sorted(tags_index - tags_fresh)[:50]
    cov["api_only_tags"] = sorted(tags_fresh - tags_index)[:50]
    bank_tags = tags_fresh | tags_index
    cov["session_tag_count"] = len(bank_tags)
    cov["note"] = ("coverage = union of paginated /memories/list session tags and observations/scopes; "
                   "/memories/list?tags= is NOT used (silently ignores the filter)")
    log(f"coverage: {len(tags_fresh)} session tags live, {len(tags_index)} in index, union {len(bank_tags)}"
        + (" [DEGRADED: API unavailable]" if cov["degraded"] else ""))

    # ---- selection ------------------------------------------------------
    con = open_db_ro(args.db)
    all_rows = fetch_sessions(con, args.sources, 0, 4e9)
    in_range = [r for r in all_rows if start <= r["started_at"] < end]
    uncovered_all = [r for r in all_rows if f"session:{r['id']}" not in bank_tags]
    uncovered = [r for r in in_range if f"session:{r['id']}" not in bank_tags]
    log(f"sessions: {len(all_rows)} total, {len(in_range)} in range, {len(uncovered)} uncovered in range "
        f"({len(uncovered_all)} uncovered all-history)")

    plan_rows: List[dict] = []
    for row in uncovered:
        if args.top_level_only and row["parent_session_id"]:
            continue
        plan_rows.append(plan_session(con, row, counter, args.chunk_chars, args.turns_per_chunk))

    # candidates = the selectable set; data-quality stats always cover EVERY uncovered
    # in-range session (including ones filtered out by --min-messages).
    eligible_plans = [p for p in plan_rows if p["retainable_messages"] >= args.min_messages]

    # ---- partial resume: sessions already tagged but missing chunk indexes ---------------
    # A batch that stops at --max-documents leaves a session tagged (so tag-based coverage
    # calls it covered) but incomplete. Those tails are invisible to normal selection, so
    # they are re-entered here, writing only the indexes the bank does not have.
    partial_plans: List[dict] = []
    if args.resume_partial:
        try:
            progress = bank_backfill_progress(api, args.bank)
        except Exception as exc:
            # FAIL-CLOSED: resuming without knowing what is already written would re-submit
            # chunks (or silently resume nothing while claiming to have tried). Refuse to run.
            log(f"REFUSING: partial-progress walk failed ({type(exc).__name__}: {exc}); "
                f"--resume-partial cannot run blind")
            return 2
        in_range_by_id = {r["id"]: r for r in in_range}
        already = {p["session_id"] for p in plan_rows}
        mismatched: List[dict] = []
        for sid, rec in sorted(progress.items()):
            total, written = rec["chunk_count"] or 0, rec["written"]
            if sid in already or sid not in in_range_by_id or not total or len(written) >= total:
                continue
            row = in_range_by_id[sid]
            if args.top_level_only and row["parent_session_id"]:
                continue
            plan = plan_session(con, row, counter, args.chunk_chars, args.turns_per_chunk)
            missing = sorted(set(range(total)) - written)
            mismatch = plan["documents"] != total
            plan["partial"] = {"chunk_count": total, "documents_written": len(written),
                               "missing_chunk_indexes": missing,
                               "missing_documents": len(missing),
                               "chunking_mismatch": mismatch}
            partial_plans.append(plan)
            if mismatch:
                mismatched.append(plan)
                log(f"  ! {sid}: chunking changed (bank has {total} chunks, recomputed "
                    f"{plan['documents']}) - document ids would not line up")
                continue
            eligible_plans.append(plan)
        resumable = [p for p in partial_plans if not p["partial"]["chunking_mismatch"]]
        log(f"partial resume: {len(resumable)} of {len(partial_plans)} incomplete sessions queued, "
            f"{sum(p['partial']['missing_documents'] for p in resumable)} missing documents")
        if mismatched:
            # FAIL-CLOSED: a mismatched session cannot be resumed safely, and skipping it
            # silently would report a successful run that left the gap open. Stop and surface it.
            log(f"REFUSING: {len(mismatched)} session(s) cannot be resumed because chunking changed:")
            for p in mismatched:
                log(f"  - {p['session_id']}: bank {p['partial']['chunk_count']} chunks, "
                    f"recomputed {p['documents']} with --chunk-chars {args.chunk_chars} "
                    f"--turns-per-chunk {args.turns_per_chunk}")
            log("  re-run with the chunking parameters the session was originally written with")
            return 2

    if args.order == "largest":
        eligible_plans.sort(key=lambda r: (-r["retainable_messages"], r["session_id"]))
    elif args.order == "newest":
        eligible_plans.sort(key=lambda r: r["started_at"], reverse=True)

    candidates = eligible_plans[:args.limit] if args.limit else eligible_plans
    log(f"selected {len(candidates)} candidates "
        f"({sum(1 for r in candidates if r['has_6plus_messages'])} with >=6 retainable messages, "
        f"{sum(1 for r in candidates if r['has_6plus_session_count'])} with >=6 session.message_count); "
        f"{len(plan_rows) - len(eligible_plans)} excluded by min-messages")

    # ---- measure selection cost once (already done in plan_session) ------
    total_msgs = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    null_tc = con.execute("SELECT COUNT(*) FROM messages WHERE token_count IS NULL").fetchone()[0]
    meta = {
        "eligible_in_range": len(in_range),
        "covered_in_range": len(in_range) - len(uncovered),
        "uncovered_in_range_total": len(uncovered),
        "all_uncovered_rows": [{"day": day_local(r["started_at"])} for r in uncovered_all],
        "tokens_column_note": (f"state.db messages.token_count is NULL for every row "
                               f"({null_tc:,}/{total_msgs:,} populated) -> token cost is estimated, not read"),
    }

    report = build_report(args, cov, meta, plan_rows, candidates, counter)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    log(f"wrote dry-run plan: {args.out}")
    t = report["totals_full_run"]
    log(f"FULL RUN: {t['sessions']} sessions / {t['documents']} documents / ~{t['est_total_tokens']:,} tokens")
    c = report["capped_run_20_largest"]["totals"]
    log(f"CAP 20 LARGEST: {c['sessions']} sessions / {c['documents']} documents / ~{c['est_total_tokens']:,} tokens")

    if not args.execute:
        log("DRY RUN complete - nothing was written to the bank. Pass --execute to submit.")
        return 0

    # ---- execute --------------------------------------------------------
    state = State(args.state, args.bank)
    skip = state.completed_sessions()
    if skip:
        before = len(candidates)
        # A session recorded `completed` in an older state file can still be incomplete in the
        # bank (that is exactly what --resume-partial exists for): never skip those.
        candidates = [c for c in candidates
                      if c["session_id"] not in skip or c.get("partial")]
        log(f"resume: skipped {before - len(candidates)} sessions already completed in {args.state}")
    summary = execute(api, con, candidates, args, state, counter)
    log(f"execute summary: {summary}")
    log(f"state file: {args.state}")
    if summary.get("progress_failures"):
        log(f"FAIL-CLOSED: {len(summary['progress_failures'])} resume progress failure(s): "
            f"{summary['progress_failures'][:3]}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
