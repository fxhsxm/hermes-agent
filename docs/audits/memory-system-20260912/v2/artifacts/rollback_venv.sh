#!/usr/bin/env bash
# Roll the Hindsight venv back to the exact frozen 0.9.0 set recorded before the upgrade,
# then restart the API and re-verify. Safe/idempotent: only reinstalls declared versions.
set -u
export MSYS_NO_PATHCONV=1
HS=/c/Users/Fwhne/hindsight
ART=/c/Users/Fwhne/Documents/HermesOutput/hermes-memory-audit-20260912/v2
PYW=C:/Users/Fwhne/hindsight/.venv/Scripts/python.exe
LOG="$ART/rollback_venv.log"; : > "$LOG"
say(){ echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

say "== reinstall frozen 0.9.0 set =="
ARTW=C:/Users/Fwhne/Documents/HermesOutput/hermes-memory-audit-20260912/v2
uv pip install --python "$PYW" -r "$ARTW/requirements-before-v092.txt" >> "$LOG" 2>&1
say "install rc=$?"
"$PYW" -c "import importlib.metadata as m; import pydantic_core, charset_normalizer; print('versions:', m.version('hindsight-api-slim'), m.version('pydantic-core'), m.version('charset-normalizer'))" 2>&1 | tee -a "$LOG"

say "== restart =="
bash /c/Users/Fwhne/Documents/HermesOutput/hermes-memory-audit-20260912/restart_hindsight.sh > "$ART/restart_after_rollback.log" 2>&1
grep -E "HEALTHY|FAIL|listener|downtime" "$ART/restart_after_rollback.log" | tail -4 | tee -a "$LOG"

say "== gates (expect 0.9.0 healthy) =="
"$PYW" "$ART/upgrade_gates.py" 2>&1 | tail -12 | tee -a "$LOG"
say "note: version gate is expected to FAIL here (we rolled back to 0.9.0); the other gates must PASS"
