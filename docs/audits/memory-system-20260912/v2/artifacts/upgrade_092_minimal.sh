#!/usr/bin/env bash
# Minimal-blast-radius upgrade 0.9.0(+header patch) -> 0.9.2.
# Rationale: 0.9.2's OpenAI-compatible provider honours llm_default_headers natively, so the
# local patch can be dropped. The previous full-dependency install damaged the venv twice
# (locked files), so this attempt: stops the API first, replaces ONLY the app package
# (--no-deps), imports it, restarts, and runs the gates. Failure => printed rollback.
set -u
export MSYS_NO_PATHCONV=1
HS=/c/Users/Fwhne/hindsight
ART=/c/Users/Fwhne/Documents/HermesOutput/hermes-memory-audit-20260912
ARTW=C:/Users/Fwhne/Documents/HermesOutput/hermes-memory-audit-20260912
PYW=C:/Users/Fwhne/hindsight/.venv/Scripts/python.exe
PY=$HS/.venv/Scripts/python.exe
LOG="$ART/v2/upgrade_minimal.log"; : > "$LOG"
say(){ echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

say "== rollback points =="
HINDSIGHT_API_EMBEDDINGS_PROVIDER=openai HINDSIGHT_API_RERANKER_PROVIDER=litellm-sdk \
  "$PY" "$HS/.venv/Scripts/hindsight-admin.exe" backup "$ART/v2/backup-pre-092-min.zip" >> "$LOG" 2>&1
say "db backup: $(du -h "$ART/v2/backup-pre-092-min.zip" 2>/dev/null | cut -f1)"
uv pip freeze --python "$PYW" > "$ART/v2/requirements-090-patched.txt"
rm -rf "$ART/v2/pkg-0.9.0-patched-snapshot"; mkdir -p "$ART/v2/pkg-0.9.0-patched-snapshot"
cp -r "$HS/.venv/Lib/site-packages/hindsight_api" "$ART/v2/pkg-0.9.0-patched-snapshot/hindsight_api"
say "pkg snapshot + freeze saved"

say "== stop the API (it must not hold the package files) =="
PID=$(netstat -ano | grep ':8888' | grep -i LISTENING | awk '{print $5}' | head -1)
[ -n "$PID" ] && { taskkill /PID "$PID" /F >/dev/null 2>&1; say "killed listener $PID"; } || say "no listener"
for i in $(seq 1 10); do netstat -ano | grep ':8888' | grep -qi LISTENING || break; sleep 1; done
netstat -ano | grep ':8888' | grep -qi LISTENING && { say "ABORT: port still held"; exit 3; }

say "== install 0.9.2 (app package only) =="
uv pip install --python "$PYW" --upgrade --no-deps "hindsight-api-slim==0.9.2" >> "$LOG" 2>&1
say "install rc=$? ; version: $("$PY" -c "import importlib.metadata as m;print(m.version('hindsight-api-slim'))" 2>&1)"
say "== import smoke =="
"$PY" -c "import hindsight_api, hindsight_api.main, hindsight_api.engine.llm_wrapper as w; print('import OK; default_headers wired:', 'default_headers=default_headers' in open(w.__file__, encoding='utf-8').read())" 2>&1 | tee -a "$LOG"

say "== restart + gates =="
bash "$ART/restart_hindsight.sh" > "$ART/v2/restart_after_minimal_upgrade.log" 2>&1
grep -E "HEALTHY|FAIL|listener" "$ART/v2/restart_after_minimal_upgrade.log" | tail -3 | tee -a "$LOG"
"$PY" "$ARTW/v2/upgrade_gates.py" 2>&1 | tail -10 | tee -a "$LOG"
RC=${PIPESTATUS[0]}
if [ "$RC" = "0" ]; then
  say "VERDICT=PASS 0.9.2 native (venv patch no longer needed)"
else
  say "VERDICT=FAIL -> rollback:"
  say "  cp -r $ARTW/v2/pkg-0.9.0-patched-snapshot/hindsight_api $HS/.venv/Lib/site-packages/"
  say "  uv pip install --python $PYW --reinstall --no-deps hindsight-api-slim==0.9.0"
  say "  bash $ART/restart_hindsight.sh"
fi
exit 0
