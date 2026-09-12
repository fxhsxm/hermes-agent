#!/usr/bin/env python3
"""Persistent-memory health check for Hermes + an external memory provider (Hindsight).

Read-only by design: it never writes a memory, never deletes one, and never repairs
anything. It measures the layers separately, because a healthy `/health` endpoint says
nothing about whether memories are still being *written*:

  1. provider wiring        -- config.yaml provider + $HERMES_HOME/hindsight/config.json
  2. API liveness           -- Hindsight /health + /version
  3. write path             -- dry-run extraction (exercises the real LLM extraction call
                               WITHOUT persisting) + bank last_memory_write_at staleness
  4. operation health       -- terminal failed operations, failure ratio, pending work, and the
                               pending_consolidation TREND (one reading cannot tell "draining a
                               backlog" from "stuck"; after a big backfill a large count is
                               expected, so samples are compared over time)
  5. retrieval              -- recall smoke test incl. reranker engagement + latency
  6. coverage               -- Hermes sessions vs sessions that actually reached the bank
  7. built-in stores        -- MEMORY.md / USER.md presence and capacity vs config limits
                               (warn at 85 % of budget, the documented routing threshold)

Exit codes: 0 = healthy, 1 = warnings, 2 = degraded (a core layer failed).

Usage:
  python scripts/memory_health_check.py
  python scripts/memory_health_check.py --bank fwh-main --json report.json
  python scripts/memory_health_check.py --no-probe-write
  python scripts/memory_health_check.py --queries queries.json   # [{"query":..., "expect":[...]}]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_API = os.environ.get("HINDSIGHT_API_URL", "http://127.0.0.1:8888")
# Non-persisting probe text. It names no real user, project or credential.
# Wording matters: the extractor keeps concrete dated statements and (correctly) filters an
# abstract "token exists" sentence as low value, which would look like a write-path failure.
# Both variants below were measured against the live extractor on 2026-09-12.
PROBE_TEXT = ("The memory health probe MEMHEALTH-2 ran on 2026-09-12 against the bank "
              "fwh-main. Probe result: healthy.")

WARN_WRITE_AGE_H = 24.0
FAIL_WRITE_AGE_H = 72.0
WARN_FAIL_RATIO = 0.10
FAIL_FAIL_RATIO = 0.25
# Unified with the documented policy (MAINTENANCE.md §1, SOURCE_OF_TRUTH.md §4): a built-in
# store above 85 % of its injected budget is a warning, because the capacity is a routing
# signal - new durable facts start displacing old ones.
WARN_BUILTIN_FULL = 0.85
# pending_consolidation is judged by TREND, not by a single reading: right after a large
# backfill a big number is expected and healthy (the worker is draining it). So samples are
# appended to a small numeric history file and the streak of non-draining readings decides.
PENDING_STALL_SAMPLES = 3   # consecutive flat-or-growing readings -> warn
PENDING_FAIL_SAMPLES = 6    # ... -> fail
PENDING_HISTORY_MAX = 60    # keep the last N samples


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _age_hours(value: str | None) -> float | None:
    if not value:
        return None
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return round((_now() - stamp).total_seconds() / 3600.0, 1)


class Result:
    """Collects findings; `status` is the worst finding level seen."""

    def __init__(self) -> None:
        self.checks: list[dict] = []
        self.status = 0

    def add(self, name: str, level: str, detail: str, **data) -> None:
        rank = {"ok": 0, "warn": 1, "fail": 2, "skip": 0}[level]
        self.status = max(self.status, rank)
        self.checks.append({"check": name, "level": level, "detail": detail, **data})

    def json(self) -> dict:
        return {
            "generated_at": _now().isoformat(timespec="seconds"),
            "status": {0: "healthy", 1: "warnings", 2: "degraded"}[self.status],
            "checks": self.checks,
        }


def _memory_block(cfg_yaml: Path) -> dict[str, str]:
    """Flat key/value pairs inside the top-level ``memory:`` block of config.yaml.

    A naive whole-file scan for ``provider:``/``char_limit:`` picks up unrelated
    top-level keys (``model.provider`` above all), so scope the scan to the block.
    """
    out: dict[str, str] = {}
    if not cfg_yaml.exists():
        return out
    in_block = False
    for line in cfg_yaml.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("memory:"):
            in_block = True
            continue
        if in_block and line and not line[0].isspace():
            break
        if in_block and ":" in line:
            key, val = line.strip().split(":", 1)
            out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def check_wiring(res: Result, hermes_home: Path) -> str:
    cfg_path = hermes_home / "hindsight" / "config.json"
    provider = _memory_block(hermes_home / "config.yaml").get("provider", "")
    if not cfg_path.exists():
        res.add("provider_wiring", "fail", f"missing {cfg_path}")
        return ""
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    bank = str(cfg.get("bank_id", ""))
    if provider and provider != "hindsight":
        res.add("provider_wiring", "fail", f"config.yaml memory.provider={provider!r}, expected 'hindsight'")
    elif not provider:
        res.add("provider_wiring", "warn", "no memory.provider found in config.yaml")
    else:
        res.add("provider_wiring", "ok", f"provider=hindsight mode={cfg.get('mode')} bank={bank}")
    return bank


def check_api(res: Result, api: str) -> bool:
    import httpx

    try:
        health = httpx.get(f"{api}/health", timeout=15).json()
        ver = httpx.get(f"{api}/version", timeout=15).json()
    except Exception as exc:  # noqa: BLE001
        res.add("api_liveness", "fail", f"{api} unreachable: {type(exc).__name__}: {exc}")
        return False
    ok = health.get("status") == "healthy"
    res.add("api_liveness", "ok" if ok else "fail",
            f"/health={health.get('status')} db={health.get('database')} api={ver.get('api_version')}",
            api_version=ver.get("api_version"))
    return ok


def check_write_path(res: Result, api: str, bank: str, probe: bool) -> None:
    """The probe is a dry run: it runs the real extraction LLM call and persists nothing."""
    import httpx

    if probe:
        t0 = time.time()
        try:
            r = httpx.post(f"{api}/v1/default/banks/{bank}/memories/dry-run-extract",
                           json={"content": PROBE_TEXT, "context": "memory health probe"}, timeout=300)
            secs = round(time.time() - t0, 1)
            if r.status_code == 200:
                facts = r.json().get("facts") or []
                # HTTP 200 proves the extraction call reached the model and returned. Zero facts
                # means the extractor judged the probe content low-value: report it, but it is not
                # the failure this probe exists to catch (non-200 = the route is broken).
                res.add("write_path_probe", "ok" if facts else "warn",
                        f"dry-run extraction HTTP 200 in {secs}s, {len(facts)} fact(s) extracted "
                        "(nothing persisted)" + ("" if facts else " — extractor filtered the probe content"),
                        elapsed_s=secs, facts=len(facts), http=200)
            else:
                res.add("write_path_probe", "fail",
                        f"dry-run extraction HTTP {r.status_code} after {secs}s: {r.text[:180]}",
                        elapsed_s=secs, http=r.status_code)
        except Exception as exc:  # noqa: BLE001
            res.add("write_path_probe", "fail", f"{type(exc).__name__}: {str(exc)[:180]}")
    else:
        res.add("write_path_probe", "skip", "probe disabled (--no-probe-write)")


def _history_path_default(json_out: str, hermes_home: Path) -> str:
    """Where the numeric sample history lives: beside the JSON report when one is given.

    The file holds counts and timestamps only - never memory content - so the check stays
    "read-only with respect to memory" while gaining a trend.
    """
    if json_out:
        return str(Path(json_out).with_name("pending_consolidation_history.json"))
    return str(hermes_home / "logs" / "pending_consolidation_history.json")


def _load_samples(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        return [s for s in loaded if isinstance(s, dict)]
    except Exception:
        # Missing or corrupt history must never fail the check: it re-seeds from this run.
        return []


def _save_samples(path: str, samples: list[dict]) -> str | None:
    """Persist the samples. Returns an error string when the file could not be written."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(samples, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
        return None
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def check_pending_consolidation(res: Result, stats: dict, history: str, stall: int, fail: int) -> None:
    """Trend of the bank's pending-consolidation queue.

    One reading cannot separate "the worker is draining a backlog" from "consolidation is
    stuck": right after a 1,000-document backfill a large count is the expected, healthy state.
    Direction over time is the signal, so every run appends a sample and the consecutive
    non-draining streak decides - with `last_consolidated_at` older than a day as a hard stall.
    """
    cur = stats.get("pending_consolidation")
    ops = stats.get("pending_operations")
    cons_age = _age_hours(stats.get("last_consolidated_at"))
    samples = _load_samples(history)
    vals = [s.get("pending_consolidation") for s in samples] + [cur]
    write_error = _save_samples(history, (samples + [{
        "at": _now().isoformat(timespec="seconds"),
        "pending_consolidation": cur,
        "pending_operations": ops,
        "failed_consolidation": stats.get("failed_consolidation"),
        "total_documents": stats.get("total_documents"),
        "last_consolidated_at": stats.get("last_consolidated_at"),
    }])[-PENDING_HISTORY_MAX:])

    # consecutive samples that did not drain, ending at the newest one
    streak = 0
    for i in range(len(vals) - 1, 0, -1):
        if vals[i] is None or vals[i - 1] is None or vals[i] <= 0 or vals[i] < vals[i - 1]:
            break
        streak += 1
    prev = vals[-2] if len(vals) > 1 else None

    if cur is None:
        level, trend, detail = "skip", "unknown", "bank reports no pending_consolidation"
    elif cur == 0:
        level, trend, detail = "ok", "empty", "0 pending consolidations"
    elif prev is None:
        level, trend = "ok", "first_sample"
        detail = f"{cur} pending; baseline recorded (no earlier sample to compare)"
    elif cur < prev:
        level, trend = "ok", "draining"
        detail = f"{cur} pending, down from {prev} (draining)"
    elif cons_age is not None and cons_age >= 24.0:
        level, trend = "fail", "stalled"
        detail = (f"{cur} pending and not draining (was {prev}); last consolidation "
                  f"{cons_age}h ago - the consolidation worker is not making progress")
    elif streak + 1 >= fail:
        level, trend = "fail", "stalled"
        detail = f"{cur} pending, not draining for {streak + 1} consecutive samples"
    elif streak + 1 >= stall:
        level, trend = "warn", "not_draining"
        detail = f"{cur} pending, not draining for {streak + 1} consecutive samples (last {prev})"
    else:
        level, trend = "ok", "flat"
        detail = f"{cur} pending (flat vs {prev}, first non-draining sample)"

    known = [v for v in vals if v is not None]
    res.add("pending_consolidation", level,
            f"{detail} [trend={trend}; "
            + (f"history={history}, {len(samples) + 1} samples" if not write_error
               else f"history NOT written ({write_error})")
            + "]",
            pending=cur, previous=prev, trend=trend, samples=len(samples) + 1,
            window_max=max(known) if known else None, history=history,
            history_written=not write_error,
            failed_consolidation=stats.get("failed_consolidation"), pending_operations=ops)

    if write_error:
        # The trend check is only as good as its memory of earlier samples: with no history it
        # cannot tell draining from stuck, so a non-draining backlog would pass unseen. A silent
        # detail field is not enough - surface it as a warning.
        res.add("pending_consolidation_history", "warn",
                f"could not append the trend sample ({write_error}) - the trend monitor is blind "
                "until this is fixed, so a non-draining backlog would go unnoticed")


def check_operations(res: Result, api: str, bank: str, window: int, prior: dict | None = None) -> dict:
    import httpx

    stats = httpx.get(f"{api}/v1/default/banks/{bank}/stats", timeout=60).json()
    write_age = _age_hours(stats.get("last_memory_write_at"))
    cons_age = _age_hours(stats.get("last_consolidated_at"))
    if write_age is None:
        res.add("bank_activity", "warn", "bank reports no memory write timestamp yet")
    elif write_age >= FAIL_WRITE_AGE_H:
        res.add("bank_activity", "fail",
                f"no memory written for {write_age}h (last={stats.get('last_memory_write_at')}) — "
                "the retain path is very likely broken")
    elif write_age >= WARN_WRITE_AGE_H:
        res.add("bank_activity", "warn",
                f"no memory written for {write_age}h (last={stats.get('last_memory_write_at')})")
    else:
        res.add("bank_activity", "ok",
                f"last write {write_age}h ago, last consolidation {cons_age}h ago")

    if stats.get("pending_operations"):
        res.add("pending_operations", "warn", f"{stats['pending_operations']} operation(s) still pending")
    # failed_consolidation is a CUMULATIVE counter, not a live fault: read as-is it warns forever
    # about one historical failure. Only a rise means something new went wrong, so compare with
    # the previous sample (the same history the pending-consolidation trend uses).
    failures = stats.get("failed_consolidation") or 0
    prev_failures = (prior or {}).get("failed_consolidation")
    if failures and (prev_failures is None or failures > prev_failures):
        res.add("failed_consolidation", "warn",
                f"{failures} consolidation failure(s) recorded"
                + (f", up from {prev_failures} since the previous sample"
                   if prev_failures is not None else " (no earlier sample to compare)"),
                failed_consolidation=failures, previous=prev_failures)
    elif failures:
        res.add("failed_consolidation", "ok",
                f"{failures} historical consolidation failure(s), unchanged since the previous "
                f"sample - not a live fault",
                failed_consolidation=failures, previous=prev_failures)

    ops = httpx.get(f"{api}/v1/default/banks/{bank}/operations",
                    params={"limit": min(window, 100)}, timeout=60).json().get("operations", [])
    failed = [o for o in ops if o.get("status") == "failed"]
    ratio = (len(failed) / len(ops)) if ops else 0.0
    classes: dict[str, int] = {}
    for o in failed:
        key = (o.get("error_message") or "unknown")[:90]
        classes[key] = classes.get(key, 0) + 1
    level = "ok"
    if ops and failed:
        level = "fail" if ratio >= FAIL_FAIL_RATIO else "warn" if ratio >= WARN_FAIL_RATIO else "ok"
    window_from = min((o.get("created_at") or "") for o in ops) if ops else None
    window_to = max((o.get("created_at") or "") for o in ops) if ops else None
    res.add("recent_operations", level,
            f"{len(failed)}/{len(ops)} of the last {len(ops)} operations failed ({ratio:.0%})"
            + (f" [window {str(window_from)[:16]} .. {str(window_to)[:16]}]" if ops else ""),
            failed=len(failed), window=len(ops), window_from=window_from, window_to=window_to,
            error_classes=classes)
    return stats


def check_retrieval(res: Result, api: str, bank: str, queries: list[dict]) -> None:
    import httpx

    rows = []
    t0 = time.time()
    for q in queries:
        label, text, expect = q.get("id", "q"), q["query"], [e.lower() for e in q.get("expect", [])]
        started = time.time()
        try:
            r = httpx.post(f"{api}/v1/default/banks/{bank}/memories/recall",
                           json={"query": text, "limit": 10}, timeout=180)
            items = r.json().get("results", []) if r.status_code == 200 else []
        except Exception as exc:  # noqa: BLE001
            rows.append({"id": label, "error": f"{type(exc).__name__}: {str(exc)[:120]}"})
            continue
        rank = None
        for i, it in enumerate(items, 1):
            if expect and any(e in json.dumps(it, ensure_ascii=False).lower() for e in expect):
                rank = i
                break
        reranker = [it.get("scores", {}).get("reranker") for it in items[:3]]
        rows.append({"id": label, "n": len(items), "hit_rank": rank,
                     "latency_s": round(time.time() - started, 2),
                     "reranker_top3": [round(x, 3) if isinstance(x, (int, float)) else x for x in reranker]})

    engaged = any(any(isinstance(x, (int, float)) and x > 0 for x in (r.get("reranker_top3") or [])) for r in rows)
    empty = [r["id"] for r in rows if r.get("n") == 0]
    level = "ok"
    if empty:
        level = "warn"
    res.add("retrieval", level,
            f"{len(rows)} recall probe(s) in {round(time.time() - t0, 1)}s; reranker_engaged={engaged}"
            + (f"; empty results: {empty}" if empty else ""),
            reranker_engaged=engaged, probes=rows)


def check_coverage(res: Result, api: str, bank: str, state_db: Path, days: int) -> None:
    """Sessions that produced a bank document, per day, over the last *days* days.

    A day with Hermes sessions but zero bank documents is the earliest signal that the
    write path stopped, which is why this is measured per day and not as one ratio.
    """
    import httpx

    if not state_db.exists():
        res.add("coverage", "skip", f"no state.db at {state_db}")
        return
    con = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT id, source, started_at FROM sessions").fetchall()
    finally:
        con.close()

    def _day(v) -> str:
        try:
            return dt.datetime.fromtimestamp(float(v)).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError):
            return str(v)[:10]

    cutoff_day = (_now() - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    sessions: dict[str, set[str]] = {}
    for sid, source, started in rows:
        day = _day(started)
        if day >= cutoff_day and source != "subagent":
            sessions.setdefault(day, set()).add(sid)

    # Session scope tags are the reliable coverage signal: consolidated observations keep
    # the tag but lose document_id, and /documents/{session_id} 404s for
    # "<session_id>-<timestamp>" document ids.
    scopes = httpx.get(f"{api}/v1/default/banks/{bank}/observations/scopes", timeout=120).json().get("scopes", [])
    tagged = {t.split(":", 1)[1] for s in scopes for t in s.get("tags", []) if t.startswith("session:")}

    per_day = []
    for day in sorted(sessions):
        have = len(sessions[day] & tagged)
        per_day.append({"day": day, "sessions": len(sessions[day]), "in_bank": have,
                        "coverage": round(have / len(sessions[day]), 3)})
    silent = [d["day"] for d in per_day if d["sessions"] >= 3 and d["in_bank"] == 0]
    total = sum(d["sessions"] for d in per_day)
    covered = sum(d["in_bank"] for d in per_day)
    level = "warn" if silent else "ok"
    res.add("coverage", level,
            f"{covered}/{total} sessions in the last {days}d reached the bank"
            + (f"; days with sessions but no memory: {silent}" if silent else ""),
            per_day=per_day, silent_days=silent)


def check_builtin_stores(res: Result, hermes_home: Path) -> None:
    block = _memory_block(hermes_home / "config.yaml")
    limits = {k: int(v) for k, v in block.items() if k.endswith("char_limit") and v.isdigit()}
    for name, key in (("MEMORY.md", "memory_char_limit"), ("USER.md", "user_char_limit")):
        path = hermes_home / "memories" / name
        if not path.exists():
            res.add(f"builtin_{name}", "warn", f"{path} missing")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        limit = limits.get(key)
        if limit:
            ratio = len(text) / limit
            level = "warn" if ratio >= WARN_BUILTIN_FULL else "ok"
            res.add(f"builtin_{name}", level,
                    f"{len(text)}/{limit} chars ({ratio:.0%} of the injected budget)",
                    chars=len(text), limit=limit)
        else:
            res.add(f"builtin_{name}", "ok", f"{len(text)} chars (no limit configured)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Hermes + Hindsight persistent-memory health check (read-only)")
    ap.add_argument("--api-url", default=DEFAULT_API)
    ap.add_argument("--bank", default="")
    ap.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME",
                    os.path.join(os.environ.get("LOCALAPPDATA", str(Path.home())), "hermes")))
    ap.add_argument("--json", dest="json_out", default="")
    ap.add_argument("--no-probe-write", action="store_true")
    ap.add_argument("--queries", default="", help='JSON list of {"id","query","expect":[...]}')
    ap.add_argument("--coverage-days", type=int, default=7)
    ap.add_argument("--op-window", type=int, default=50)
    ap.add_argument("--history", default="",
                    help="numeric sample history for the pending_consolidation trend "
                         "(default: beside --json, else $HERMES_HOME/logs/)")
    ap.add_argument("--pending-stall-samples", type=int, default=PENDING_STALL_SAMPLES)
    ap.add_argument("--pending-fail-samples", type=int, default=PENDING_FAIL_SAMPLES)
    args = ap.parse_args()

    hermes_home = Path(args.hermes_home)
    res = Result()
    bank = args.bank or check_wiring(res, hermes_home)
    if not bank:
        bank = "hermes"
    if check_api(res, args.api_url):
        check_write_path(res, args.api_url, bank, probe=not args.no_probe_write)
        history = args.history or _history_path_default(args.json_out, hermes_home)
        prior = _load_samples(history)
        stats = check_operations(res, args.api_url, bank, args.op_window,
                                 prior=prior[-1] if prior else None)
        check_pending_consolidation(res, stats, history,
                                    args.pending_stall_samples, args.pending_fail_samples)
        queries = [{"id": "smoke", "query": "user preferences"}] if not args.queries else \
            json.loads(Path(args.queries).read_text(encoding="utf-8"))
        check_retrieval(res, args.api_url, bank, queries)
        check_coverage(res, args.api_url, bank, hermes_home / "state.db", args.coverage_days)
    check_builtin_stores(res, hermes_home)

    report = res.json()
    report["bank"] = bank
    report["api_url"] = args.api_url
    width = max(len(c["check"]) for c in report["checks"])
    mark = {"ok": "PASS", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}
    print(f"Memory health — bank={bank} api={args.api_url}")
    print("-" * 72)
    for c in report["checks"]:
        print(f"{mark[c['level']]:4s}  {c['check']:<{width}}  {c['detail']}")
    print("-" * 72)
    print(f"status: {report['status']}  (0=healthy 1=warnings 2=degraded)")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"json: {args.json_out}")
    return res.status


if __name__ == "__main__":
    sys.exit(main())
