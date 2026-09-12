#!/usr/bin/env python3
"""Drive the gated backfill batches in sequence and stop at the first real problem.

Runs memory_backfill_batch.py for batch numbers [start, end], one at a time, and stops as
soon as a batch returns a non-zero exit code (2 = infrastructure/setup, 3 = gate failed) or
writes no documents (the segment is finished). It never rolls anything back: it stops, prints
which batch stopped it, and leaves the decision to the operator.

Every batch is judgeable on its own artifacts:
  v2/batch<N>_before.json   v2/batch<N>_after.json   v2/batch<N>_compare.json
  v2/backfill_batch<N>_result.json   v2/manifests/batch<N>-*.json   v2/backups/<bank>-*.zip
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.environ.get("HS_PYTHON", r"C:/Users/Fwhne/hindsight/.venv/Scripts/python.exe")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, required=True)
    ap.add_argument("--end", type=int, required=True)
    ap.add_argument("--from", dest="date_from", default="2026-09-08")
    ap.add_argument("--to", dest="date_to", default="2026-09-12")
    ap.add_argument("--limit", type=int, default=6)
    ap.add_argument("--max-documents", type=int, default=40)
    ap.add_argument("--bank", default="fwh-main")
    ap.add_argument("--state", default=os.path.join(HERE, "backfill_state_prod.json"))
    ap.add_argument("--dir", default=HERE)
    ap.add_argument("--questions", default=os.path.join(
        os.path.join(os.environ.get("HERMES_HOME") or os.path.join(
            os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "hermes"),
            "scripts", "memory-ops", "retrieval_questions.json")))
    ap.add_argument("--resume-partial", action="store_true",
                    help="resume sessions that are tagged but incomplete (cap-truncated tails)")
    ap.add_argument("--max-minutes", type=float, default=240.0,
                    help="stop starting new batches after this wall-clock budget")
    args = ap.parse_args(argv)

    t0 = time.time()
    done = []
    for n in range(args.start, args.end + 1):
        if (time.time() - t0) / 60.0 > args.max_minutes:
            print(f"[driver] wall-clock budget {args.max_minutes} min reached; stopping before "
                  f"batch {n}", flush=True)
            break
        cmd = [PY, os.path.join(HERE, "memory_backfill_batch.py"), "--batch", str(n),
               "--from", args.date_from, "--to", args.date_to,
               "--limit", str(args.limit), "--max-documents", str(args.max_documents),
               "--order", "oldest", "--top-level-only", "--bank", args.bank,
               "--state", args.state, "--dir", args.dir, "--questions", args.questions,
               "--execute", "--i-accept-production-writes"]
        if args.resume_partial:
            cmd.append("--resume-partial")
        print(f"[driver] === batch {n} start {time.strftime('%H:%M:%S')} ===", flush=True)
        rc = subprocess.run(cmd).returncode
        res_path = os.path.join(args.dir, f"backfill_batch{n}_result.json")
        summary = {"batch": n, "rc": rc}
        try:
            with open(res_path, encoding="utf-8") as fh:
                r = json.load(fh)
            summary.update({
                "docs": (r.get("backfill") or {}).get("manifest_documents"),
                "ops_failed": (r.get("backfill") or {}).get("ops_failed"),
                "bank_documents": (r.get("bank_after") or {}).get("total_documents"),
                "recall@1_before": (r.get("before") or {}).get("recall@1"),
                "recall@1_after": (r.get("after") or {}).get("recall@1"),
                "recall@3_before": (r.get("before") or {}).get("recall@3"),
                "recall@3_after": (r.get("after") or {}).get("recall@3"),
                "gate": (r.get("gate") or {}).get("checks"),
                "flipped": (r.get("gate") or {}).get("flipped"),
                "regressions": (r.get("gate") or {}).get("regressions"),
                "action": (r.get("gate") or {}).get("action") or r.get("error") or r.get("note"),
            })
        except Exception as exc:
            summary["error"] = f"no result file: {exc}"
        done.append(summary)
        print("[driver] RESULT " + json.dumps(summary, ensure_ascii=False), flush=True)
        if rc != 0:
            print(f"[driver] STOP after batch {n}: rc={rc} "
                  f"({'gate failed - inspect ' + res_path if rc == 3 else 'infrastructure/setup problem'})",
                  flush=True)
            break
        if not summary.get("docs"):
            print("[driver] STOP: batch {n} wrote no documents - nothing left to backfill".format(n=n),
                  flush=True)
            break

    print("[driver] === summary ===", flush=True)
    for s in done:
        print("[driver] " + json.dumps(s, ensure_ascii=False), flush=True)
    print(f"[driver] batches attempted={len(done)} wall={round((time.time()-t0)/60,1)}min",
          flush=True)
    return 0 if done and all(s["rc"] == 0 for s in done) else 1


if __name__ == "__main__":
    sys.exit(main())
