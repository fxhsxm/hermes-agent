"""Weekly memory regression: unit invariants + health (with write probe) + retrieval quality."""
import sys, pathlib, subprocess
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from memory_ops_common import health, retrieval, AUD, PY, stamp

print(f"Memory regression (weekly) — {stamp()}")
h = health(no_probe=False, out="health-weekly.json")
print("health exit:", h.returncode)
for line in (h.stdout or "").splitlines():
    if any(k in line for k in ("PASS", "WARN", "FAIL", "status:")):
        print("  " + line.strip())
if h.stderr:
    print("  health stderr:", h.stderr[-300:])
r = retrieval(out="retrieval-weekly.json", tag="weekly")
print("retrieval exit:", r.returncode)
try:
    import json
    agg = json.load(open(AUD / "v2" / "retrieval-weekly.json", encoding="utf-8")).get("aggregate", {})
    dup = (agg.get("duplication") or {})
    lat = (agg.get("latency") or {})
    rr = (agg.get("reranker") or {})
    print(f"  recall median {lat.get('median_s')}s · duplicate item rate {dup.get('overall_duplicate_item_rate')}"
          f" · reranker engaged {rr.get('questions_with_reranker_engaged')}/{agg.get('n_questions')}"
          f" · recall@1 {agg.get('recall_at_1')} recall@3 {agg.get('recall_at_3')}")
except Exception as exc:
    print("  retrieval summary unavailable:", exc)
print("artifacts:", AUD / "v2")
