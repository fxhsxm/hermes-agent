#!/usr/bin/env python3
"""memory_backfill_batch.py -- one production backfill batch, gated end to end.

Fixed protocol, in this order and no other:

  1. timestamped backup           <dir>/backups/<bank>-<UTCstamp>.zip   (abort if it fails)
  2. measure BEFORE               <dir>/batch<N>_before.json
  3. backfill ONE capped batch    --limit <sessions> --max-documents <docs> + per-run manifest
  4. measure AFTER                <dir>/batch<N>_after.json
  5. per-question diff + gate     <dir>/batch<N>_compare.json
  6. write <dir>/backfill_batch<N>_result.json; STOP if the gate fails

WHY EACH PIECE EXISTS
  * The backup name carries a timestamp because the old fixed name
    (backup-before-batch1.zip) was overwritten by every later run, so only the newest
    rollback point survived.
  * The manifest (memory_backfill.py --manifest) records the doc ids THIS run wrote, so
    a single batch can be rolled back by document id. Rolling back by the kind:backfill
    tag would delete the earlier batches that already passed their gate.
  * The gate is compare_retrieval_runs.py: recall@3 must not drop, no answer that was
    present may be lost, no question whose answer held its rank by >= --min-margin may
    move down, every question must answer, duplicate rate within tolerance. Near-tie
    flips are reported and excluded - the criterion the first three batches lacked.
  * FAIL-CLOSED coverage: after the writes, per-session progress is re-read FROM THE BANK and
    a session called `completed` must be complete there, and every session must account for
    the chunks it skipped plus the chunks it submitted. A batch that cannot prove it closed
    its own gap exits 3 (or 2 when the backfill tool refused to run at all).

NOT DONE ON FAILURE: no automatic rollback. The comparison file names the flipped
questions, the manifest names the exact document ids, and this run's own backup zip is the
rollback point; a human (or the parent agent) decides which of the two to use.

USAGE (dry run of the protocol: everything except the writes)
  python memory_backfill_batch.py --batch 4 --from 2026-09-08 --to 2026-09-12 \\
      --dir <audit>/v2 --state <audit>/v2/backfill_state_prod.json
USAGE (production)
  python memory_backfill_batch.py --batch 4 --from 2026-09-08 --to 2026-09-12 \\
      --dir <audit>/v2 --state <audit>/v2/backfill_state_prod.json \\
      --execute --i-accept-production-writes

Exit codes: 0 gate passed, 2 setup/backup/infrastructure failure, 3 gate failed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PYH = os.environ.get("HS_PYTHON", r"C:/Users/Fwhne/hindsight/.venv/Scripts/python.exe")
ADMIN = os.environ.get("HS_ADMIN", r"C:/Users/Fwhne/hindsight/.venv/Scripts/hindsight-admin.exe")
API = os.environ.get("HS_API", "http://127.0.0.1:8888")
def default_questions() -> str:
    """Locate the LIVE question set.

    It is memory-derived, so it is deliberately not in this public repository (which ships
    retrieval_questions.template.json instead). Order: the ops directory the cron jobs use, then
    the audit workspace. A synthetic template is never acceptable as a measuring instrument.
    """
    home = os.environ.get("HERMES_HOME") or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "hermes")
    for p in (os.path.join(home, "scripts", "memory-ops", "retrieval_questions.json"),
              os.path.join(HERE, "retrieval_questions.json")):
        if os.path.exists(p):
            return p
    return os.path.join(HERE, "retrieval_questions.json")


QUESTIONS = default_questions()
BACKUP_ENV = {"HINDSIGHT_API_EMBEDDINGS_PROVIDER": "openai",
              "HINDSIGHT_API_RERANKER_PROVIDER": "litellm-sdk"}
MIN_BACKUP_BYTES = 5_000_000


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class Run:
    def __init__(self, path: str) -> None:
        self.path = path
        self.t0 = time.time()

    def log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def sh(cmd, timeout, log, label, env=None):
    log(f"$ {label}")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout,
                       env={**os.environ, **(env or {})})
    tail = [ln for ln in (r.stdout or "").strip().splitlines() if ln.strip()][-8:]
    for ln in tail:
        log(f"    {ln}")
    if r.returncode != 0:
        log(f"    ! rc={r.returncode} stderr={(r.stderr or '')[-400:]}")
    return r


def bank_stats(bank):
    import httpx
    s = httpx.get(f"{API}/v1/default/banks/{bank}/stats", timeout=60).json()
    return {k: s.get(k) for k in ("total_nodes", "total_documents", "failed_operations",
                                  "pending_operations", "last_memory_write_at")}


def harness(bank, questions, out, tag, log, min_margin):
    r = sh([PYH, os.path.join(HERE, "measure_retrieval_quality.py"), "--bank", bank,
            "--questions", questions, "--out", out, "--tag", tag,
            "--min-margin", str(min_margin), "--quiet"], 1800, log, f"measure {tag}")
    try:
        with open(out, encoding="utf-8") as fh:
            agg = json.load(fh)["aggregate"]
    except Exception as exc:
        return {"error": f"{exc}; rc={r.returncode}"}
    tiers = agg.get("by_tier") or {}
    core = tiers.get("core") or {}
    env = tiers.get("environment") or {}
    return {
        "recall@1": agg.get("recall_at_1"), "recall@3": agg.get("recall_at_3"),
        "recall@10": agg.get("recall_at_10"),
        # Per-tier recall: `core` is the long-term-memory score the gate uses; `environment`
        # (port/version/model/path) is recall smoke, reported but never gated.
        "core_recall@1": core.get("recall_at_1"), "core_recall@3": core.get("recall_at_3"),
        "core_recall@10": core.get("recall_at_10"), "core_n_scored": core.get("n_scored"),
        "env_recall@3": env.get("recall_at_3"), "env_n_scored": env.get("n_scored"),
        "recall_without_backfill@3": ((agg.get("recall_without_backfill") or {})
                                      .get("recall@3") or {}).get("rate"),
        "dup_item_rate": (agg.get("duplication") or {}).get("overall_duplicate_item_rate"),
        "decided_ids": (agg.get("ranking_decision") or {}).get("decided_ids"),
        "unstable_ids": (agg.get("ranking_decision") or {}).get("unstable_ids"),
        "staleness_proxy_strict": (agg.get("staleness_proxy") or {})
                                  .get("questions_top1_superseded_proxy_strict"),
        "n": agg.get("n_questions"), "n_answered": agg.get("n_answered"),
        "margins": (agg.get("ranking_decision") or {}).get("margins"),
    }


def bank_progress(bank):
    """Per-session (written, chunk_count) straight from the bank.

    Reuses the backfill tool's walk so there is exactly one definition of "what the bank
    already has" - the same one the resume path uses to decide what to write.
    """
    sys.path.insert(0, HERE)
    from memory_backfill import Api, bank_backfill_progress
    return bank_backfill_progress(Api(API), bank)


def verify_progress(bank, manifest_sessions, attempts=3, delay=10.0):
    """FAIL-CLOSED post-run check: did the run actually close the gaps it claimed to close?

    Reads the bank (never our own bookkeeping) and requires that a session called completed is
    complete, and that every session accounts for at least the chunks it skipped plus the chunks
    it submitted. Writes are async, so the read is retried before it is called a failure.
    """
    detail: dict = {}
    problems: list = []
    for attempt in range(attempts):
        try:
            progress = bank_progress(bank)
        except Exception as exc:  # noqa: BLE001
            problems = [f"progress read failed: {type(exc).__name__}: {exc}"]
            detail = {}
            time.sleep(delay)
            continue
        problems, detail = [], {}
        for s in manifest_sessions:
            sid = s.get("session_id")
            rec = progress.get(sid) or {"written": set(), "chunk_count": 0}
            written = len(rec["written"])
            total = rec.get("chunk_count") or 0
            accounted = (s.get("chunks_skipped_already_in_bank") or 0) + (s.get("documents") or 0)
            detail[sid] = {"written_after": written, "chunk_count": total,
                           "accounted_for": accounted, "status": s.get("status")}
            if s.get("status") == "completed" and total and written < total:
                problems.append(f"{sid}: marked completed but the bank has {written}/{total} chunks")
            if written < accounted:
                problems.append(f"{sid}: bank has {written} chunks, run accounted for {accounted}")
        if not problems:
            return [], detail
        if attempt < attempts - 1:
            time.sleep(delay)
    return problems, detail


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="One gated production backfill batch.")
    ap.add_argument("--batch", type=int, required=True, help="batch number (names the artifacts)")
    ap.add_argument("--from", dest="date_from", default="2026-09-08")
    ap.add_argument("--to", dest="date_to", default="2026-09-12")
    ap.add_argument("--limit", type=int, default=6, help="max sessions this batch")
    ap.add_argument("--max-documents", type=int, default=40, help="max documents this batch")
    ap.add_argument("--order", default="oldest", choices=["oldest", "newest", "largest"])
    ap.add_argument("--top-level-only", action="store_true", default=True)
    ap.add_argument("--all-sessions", dest="top_level_only", action="store_false",
                    help="include subagent sessions too (default: top-level only)")
    ap.add_argument("--resume-partial", action="store_true",
                    help="also resume sessions that are tagged but incomplete in the bank "
                         "(cap-truncated tails), writing only their missing chunk indexes")
    ap.add_argument("--bank", default="fwh-main")
    ap.add_argument("--state", required=True, help="resumable backfill state file")
    ap.add_argument("--dir", default=HERE, help="artifact directory (defaults to the script dir)")
    ap.add_argument("--questions", default=None)
    ap.add_argument("--execute", action="store_true", help="actually write (default: protocol dry run)")
    ap.add_argument("--i-accept-production-writes", action="store_true")
    ap.add_argument("--min-margin", type=float, default=0.05)
    ap.add_argument("--dup-tolerance", type=float, default=0.02)
    ap.add_argument("--specificity", default=os.path.join(HERE, "golden_set_coverage.json"),
                    help="validate_golden_set.py report; its low-specificity questions are "
                         "excluded from the gate verdict (default: golden_set_coverage.json next "
                         "to this script)")
    ap.add_argument("--settle-seconds", type=float, default=20.0,
                    help="wait after the writes before measuring (async extraction)")
    args = ap.parse_args(argv)

    qpath = args.questions or QUESTIONS
    try:
        with open(qpath, encoding="utf-8") as fh:
            qdoc = json.load(fh)
    except Exception as exc:
        return finish(2, error=f"question set unreadable ({qpath}): {exc}")
    if isinstance(qdoc, dict) and qdoc.get("synthetic"):
        return finish(2, error=(f"{qpath} is the synthetic template, not the live question set - "
                                "refusing to gate a batch on placeholder questions"))
    args.questions = qpath

    n = args.batch
    D = os.path.abspath(args.dir)
    questions = args.questions or os.path.abspath(QUESTIONS)
    run = Run(os.path.join(D, f"backfill_batch{n}.log"))
    log = run.log
    b_stamp = stamp()
    backup_path = os.path.join(D, "backups", f"{args.bank}-{b_stamp}.zip")

    out = {"batch": n, "tool": "memory_backfill_batch",
           "scope": {"from": args.date_from, "to": args.date_to, "limit": args.limit,
                     "max_documents": args.max_documents, "order": args.order,
                     "top_level_only": args.top_level_only},
           "execute": bool(args.execute), "started": dt.datetime.now().isoformat(timespec="seconds"),
           "min_margin": args.min_margin, "dup_tolerance": args.dup_tolerance}
    result_path = os.path.join(D, f"backfill_batch{n}_result.json")

    def finish(code: int, **extra):
        out.update(extra)
        out["finished"] = dt.datetime.now().isoformat(timespec="seconds")
        out["duration_s"] = round(time.time() - run.t0, 1)
        with open(result_path, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)
        log(f"wrote {result_path}  exit={code}")
        return code

    log(f"=== batch {n}: {args.date_from}..{args.date_to} limit={args.limit} "
        f"max_docs={args.max_documents} execute={args.execute} ===")
    try:
        out["bank_before"] = bank_stats(args.bank)
    except Exception as exc:
        return finish(2, error=f"API unreachable: {exc}")
    log(f"bank before: {json.dumps(out['bank_before'])}")

    # 1. timestamped backup -------------------------------------------------
    os.makedirs(os.path.dirname(backup_path), exist_ok=True)
    b = sh([ADMIN, "backup", backup_path], 1800, log, f"backup -> {os.path.basename(backup_path)}",
           BACKUP_ENV)
    size = os.path.getsize(backup_path) if os.path.exists(backup_path) else 0
    out["backup"] = {"path": backup_path, "rc": b.returncode, "bytes": size,
                     "ok": b.returncode == 0 and size >= MIN_BACKUP_BYTES}
    log(f"backup rc={b.returncode} bytes={size}")
    if not out["backup"]["ok"]:
        return finish(2, error="backup failed or is implausibly small - refusing to continue")

    # 2. before -------------------------------------------------------------
    before_json = os.path.join(D, f"batch{n}_before.json")
    out["before"] = harness(args.bank, questions, before_json, f"batch{n}_before", log, args.min_margin)
    log(f"BEFORE {json.dumps(out['before'], ensure_ascii=False)}")
    if out["before"].get("error"):
        return finish(2, error="before-measurement failed")

    # 3. the batch (with its per-run manifest) ------------------------------
    manifest = os.path.join(D, "manifests", f"batch{n}-{b_stamp}.json")
    report = os.path.join(D, f"backfill_batch{n}_report.json")
    cmd = [PYH, os.path.join(HERE, "memory_backfill.py"), "--bank", args.bank,
           "--from", args.date_from, "--to", args.date_to, "--order", args.order,
           "--limit", str(args.limit), "--max-documents", str(args.max_documents),
           "--state", args.state, "--out", report, "--manifest", manifest]
    if args.top_level_only:
        cmd.append("--top-level-only")
    if args.resume_partial:
        cmd.append("--resume-partial")
    if args.execute:
        cmd += ["--execute", "--i-accept-production-writes"]
    r = sh(cmd, 7200, log, f"backfill batch {n}" + ("" if args.execute else " (DRY RUN)"))
    out["backfill"] = {"rc": r.returncode, "manifest": manifest, "report": report}
    if args.execute and r.returncode != 0:
        # FAIL-CLOSED paths inside memory_backfill.py print a REFUSING/FAIL-CLOSED line; surface
        # it instead of a generic "no manifest" error.
        reason = next((ln.strip() for ln in (r.stdout or "").splitlines()
                       if "REFUSING" in ln or "FAIL-CLOSED" in ln), "")
        return finish(2, error=f"backfill exited rc={r.returncode}" + (f" - {reason}" if reason else ""))
    if args.execute:
        try:
            with open(manifest, encoding="utf-8") as fh:
                man = json.load(fh)
            manifest_sessions = man.get("sessions") or []
            out["backfill"].update({
                "manifest_sessions": len(man.get("sessions") or []),
                "manifest_documents": man.get("document_count"),
                "manifest_statuses": {s: sum(1 for x in (man.get("sessions") or [])
                                             if x.get("status") == s)
                                      for s in {x.get("status") for x in (man.get("sessions") or [])}},
                "ops_completed": (man.get("summary") or {}).get("ops_completed"),
                "ops_failed": (man.get("summary") or {}).get("ops_failed"),
                "doc_ids": man.get("document_ids"),
            })
        except Exception as exc:
            out["backfill"]["manifest_error"] = str(exc)
            return finish(2, error=f"run manifest missing/unreadable: {exc}")
        if not out["backfill"].get("doc_ids"):
            # Distinguish "this segment is finished" from "something is broken": the plan for
            # this run selects 0 candidates when no uncovered session is left in range, which
            # is a healthy end state, not an error.
            selected = None
            try:
                with open(report, encoding="utf-8") as fh:
                    plan = json.load(fh)
                selected = ((plan.get("selection") or {}).get("selected"))
                # A plan that selected incomplete sessions but reports a chunking mismatch is a
                # refusal case (memory_backfill.py exits 2 for it) - never a silent success.
                mismatched = [(s["session_id"], (s.get("partial") or {}).get("chunk_count"),
                               s.get("documents"))
                              for s in (plan.get("sessions") or [])
                              if (s.get("partial") or {}).get("chunking_mismatch")]
            except Exception:
                mismatched = []
            if mismatched:
                return finish(2, error=f"chunking mismatch on {len(mismatched)} session(s) - "
                                       f"refusing: {mismatched[:3]}")
            if selected == 0:
                return finish(0, note="segment exhausted: 0 uncovered sessions left in range - "
                                      "nothing to backfill, nothing written")
            return finish(2, error="run manifest has no document ids - nothing was written")
        if r.returncode != 0:
            return finish(2, error=f"backfill exited rc={r.returncode}")
    else:
        return finish(0, note="protocol dry run: backup + before-measurement + plan only, "
                              "no writes, no gate")

    time.sleep(args.settle_seconds)

    # 4. after --------------------------------------------------------------
    after_json = os.path.join(D, f"batch{n}_after.json")
    out["after"] = harness(args.bank, questions, after_json, f"batch{n}_after", log, args.min_margin)
    log(f"AFTER {json.dumps(out['after'], ensure_ascii=False)}")
    try:
        out["bank_after"] = bank_stats(args.bank)
    except Exception as exc:
        out["bank_after"] = {"error": str(exc)}
    if out["after"].get("error"):
        return finish(2, error="after-measurement failed")

    # 5. the gate -----------------------------------------------------------
    cmp_json = os.path.join(D, f"batch{n}_compare.json")
    gate_cmd = [PYH, os.path.join(HERE, "compare_retrieval_runs.py"), "--before", before_json,
                "--after", after_json, "--out", cmp_json, "--min-margin", str(args.min_margin),
                "--dup-tolerance", str(args.dup_tolerance)]
    # Low-specificity questions (expected token too common in the bank to detect a change) are
    # excluded from the verdict, so the gate must know about them.
    if args.specificity and os.path.exists(args.specificity):
        gate_cmd += ["--specificity", args.specificity]
    elif args.specificity:
        log(f"[warn] specificity report {args.specificity} not found - the gate runs without it, "
            "so a very common expected token could mask a regression")
    c = sh(gate_cmd, 600, log, "gate")
    try:
        with open(cmp_json, encoding="utf-8") as fh:
            cmp = json.load(fh)
    except Exception as exc:
        return finish(2, error=f"comparison unreadable: {exc}")
    out["gate"] = {"rc": c.returncode, "checks": cmp.get("checks"), "all_ok": cmp.get("all_ok"),
                   "regressions": cmp.get("regressions"), "unstable_flips": cmp.get("unstable_flips"),
                   "flipped": cmp.get("flipped"), "improved": cmp.get("improved"),
                   "counts": cmp.get("counts"), "action": cmp.get("action"),
                   "file": cmp_json}

    written = out["backfill"].get("manifest_documents") or 0
    b_docs = (out["bank_before"] or {}).get("total_documents")
    a_docs = (out["bank_after"] or {}).get("total_documents")
    out["write_check"] = {"manifest_documents": written, "bank_before_documents": b_docs,
                          "bank_after_documents": a_docs,
                          "delta": (None if (b_docs is None or a_docs is None) else a_docs - b_docs),
                          "ops_failed": out["backfill"].get("ops_failed")}
    out["write_check"]["ok"] = (out["backfill"].get("ops_failed") == 0
                                and (out["write_check"]["delta"] is None
                                     or out["write_check"]["delta"] >= written))
    log(f"write check: {json.dumps(out['write_check'], ensure_ascii=False)}")

    # FAIL-CLOSED: verify from the bank that this run really wrote what it claims. A batch that
    # reports success while leaving its own sessions short is the failure mode that produced the
    # 39.6 %-complete segment in the first place.
    problems, detail = verify_progress(args.bank, manifest_sessions)
    out["coverage_verify"] = {"problems": problems, "detail": detail, "ok": not problems}
    log("coverage verify: " + ("OK - every session in this run accounts for its chunks"
                               if not problems else f"FAILED - {problems[:3]}"))

    code = 0 if (cmp.get("all_ok") and out["write_check"]["ok"] and not problems) else 3
    log(("GATE PASSED" if code == 0 else "GATE FAILED / WRITE OR COVERAGE CHECK FAILED")
        + f" - {cmp.get('action')}")
    return finish(code)


if __name__ == "__main__":
    sys.exit(main())
