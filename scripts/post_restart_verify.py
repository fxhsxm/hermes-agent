"""Post-restart verification: plugin activation + deployed code + full health + retrieval snapshot."""
import sys, pathlib, json, subprocess, datetime, os
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from memory_ops_common import health, retrieval, AUD, PY, stamp, run

LIVE = pathlib.Path(r"C:/Users/Fwhne/AppData/Local/hermes/hermes-agent")
print(f"== Memory System v2 — post-restart verification {stamp()} ==")

gw = run(["hermes", "gateway", "status"], timeout=120)
print("gateway:", next((l.strip() for l in (gw.stdout or "").splitlines() if "Gateway process" in l), "unknown"))

ag = pathlib.Path(os.path.expandvars(r"%LOCALAPPDATA%")) / "hermes" / "logs" / "agent.log"
lines = []
if ag.exists():
    lines = [l for l in ag.read_text(encoding="utf-8", errors="replace").splitlines()[-800:]
             if "hindsight" in l.lower() and any(k in l.lower() for k in ("initialized", "activated", "registered"))]
print("plugin activation lines in the fresh log:", len(lines))
for l in lines[-3:]:
    print("   ", l[:160])

dc = run([str(LIVE / "venv" / "Scripts" / "python.exe"), "-c",
          "import sys;sys.path.insert(0, r'%s');import plugins.memory.hindsight as m;"
          "print('on_delegation',hasattr(m.HindsightMemoryProvider,'on_delegation'),"
          "'| failed_retain_warning',hasattr(m.HindsightMemoryProvider,'_warn_failed_retain'),"
          "'| recall_dedupe',hasattr(m,'_dedupe_recall_results'))" % str(LIVE).replace("\\", "/")], timeout=300)
print("deployed code:", (dc.stdout or dc.stderr or "").strip()[:200])

h = health(no_probe=False, out="health_post_restart.json")
print("-- health (write probe) --")
for line in (h.stdout or "").splitlines():
    if any(k in line for k in ("PASS", "WARN", "FAIL", "SKIP", "status:")):
        print("   " + line.strip())
if h.stderr:
    print("   stderr:", h.stderr[-300:])

r = retrieval(out="retrieval_post_restart.json", tag="post-restart")
print("retrieval exit:", r.returncode)

try:
    hp = json.load(open(AUD / "v2" / "health_post_restart.json", encoding="utf-8"))
    rp = json.load(open(AUD / "v2" / "retrieval_post_restart.json", encoding="utf-8")).get("aggregate", {})
except Exception:
    hp, rp = {}, {}
snap = {"generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "gateway_line": next((l.strip() for l in (gw.stdout or "").splitlines() if "Gateway process" in l), ""),
        "plugin_activation_lines": lines[-3:], "deployed_code": (dc.stdout or "").strip(),
        "health_status": hp.get("status"),
        "health_checks": {c["check"]: {"level": c["level"], "detail": c["detail"]} for c in hp.get("checks", [])},
        "retrieval_aggregate": rp}
(AUD / "v2" / "final_operational_snapshot.json").write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
print("snapshot: v2/final_operational_snapshot.json | health:", hp.get("status"))
