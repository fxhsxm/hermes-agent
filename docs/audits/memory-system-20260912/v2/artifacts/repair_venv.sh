#!/usr/bin/env bash
# Repair the Hindsight venv after the aborted 0.9.2 install:
#   1. drop dist-info dirs whose METADATA was lost (uv refuses to read them)
#   2. force-reinstall the exact frozen 0.9.0 set
#   3. import smoke, restart, gates
set -u
export MSYS_NO_PATHCONV=1
HS=/c/Users/Fwhne/hindsight
SP="$HS/.venv/Lib/site-packages"
PYW=C:/Users/Fwhne/hindsight/.venv/Scripts/python.exe
PY=$HS/.venv/Scripts/python.exe
ART=/c/Users/Fwhne/Documents/HermesOutput/hermes-memory-audit-20260912
ARTW=C:/Users/Fwhne/Documents/HermesOutput/hermes-memory-audit-20260912
LOG="$ART/v2/repair_venv.log"; : > "$LOG"
say(){ echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

say "== damaged dist-info dirs (no METADATA) =="
DAMAGED=""
for d in "$SP"/*.dist-info "$SP"/*.egg-info; do
  [ -d "$d" ] || continue
  if [ ! -f "$d/METADATA" ]; then DAMAGED="$DAMAGED $d"; echo "  missing METADATA: $(basename "$d")" | tee -a "$LOG"; fi
done
[ -z "$DAMAGED" ] && say "none" || { rm -rf $DAMAGED; say "removed $(echo $DAMAGED | wc -w) damaged dist-info dir(s)"; }

say "== force-reinstall the frozen set =="
uv pip install --python "$PYW" --reinstall -r "$ARTW/v2/requirements-before-v092.txt" >> "$LOG" 2>&1
say "install rc=$?"

say "== import smoke =="
"$PY" - <<'PYEOF' 2>&1 | tee -a "$LOG"
mods = ["pydantic_core", "charset_normalizer", "fastmcp", "litellm", "openai", "httpx", "uvicorn", "pg0", "hindsight_api", "hindsight_api.main"]
import importlib, importlib.metadata as md
bad = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception as e:
        bad.append(f"{m}: {type(e).__name__}: {e}")
print("import failures:", bad or "none")
print("hindsight-api-slim:", md.version("hindsight-api-slim"))
PYEOF

say "== restart =="
bash "$ART/restart_hindsight.sh" > "$ART/v2/restart_after_repair.log" 2>&1
grep -E "HEALTHY|FAIL|listener|downtime" "$ART/v2/restart_after_repair.log" | tail -4 | tee -a "$LOG"

say "== gates (version gate expected to FAIL: we are back on 0.9.0 + patch) =="
"$PY" "$ARTW/v2/upgrade_gates.py" 2>&1 | tail -12 | tee -a "$LOG"
