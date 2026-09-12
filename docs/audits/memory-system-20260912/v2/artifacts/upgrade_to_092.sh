#!/usr/bin/env bash
# Durable fix: upgrade Hindsight 0.9.0 -> 0.9.2 so the opencode-go session header
# (HINDSIGHT_API_LLM_DEFAULT_HEADERS) is honoured natively and the local venv patch is dropped.
# Records a verifiable rollback point, restarts the API, then runs scripts/v2/upgrade_gates.py.
# Any gate failure => VERDICT FAIL: roll back (steps printed) instead of pushing forward.
set -u
export MSYS_NO_PATHCONV=1
HS=/c/Users/Fwhne/hindsight
ART=/c/Users/Fwhne/Documents/HermesOutput/hermes-memory-audit-20260912/v2
ARTW=C:/Users/Fwhne/Documents/HermesOutput/hermes-memory-audit-20260912/v2
PY=$HS/.venv/Scripts/python.exe
PYW=C:/Users/Fwhne/hindsight/.venv/Scripts/python.exe
TARGET="hindsight-api-slim[embedded-db]==0.9.2"
mkdir -p "$ART"
LOG="$ART/upgrade_v092.log"; : > "$LOG"
say(){ echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

say "== rollback point =="
uv pip freeze --python "$PYW" > "$ART/requirements-before-v092.txt"
say "frozen $(wc -l < "$ART/requirements-before-v092.txt") packages"
rm -rf "$ART/pkg-0.9.0-snapshot"; mkdir -p "$ART/pkg-0.9.0-snapshot"
cp -r "$HS/.venv/Lib/site-packages/hindsight_api" "$ART/pkg-0.9.0-snapshot/hindsight_api"
say "package snapshot: $(du -sh "$ART/pkg-0.9.0-snapshot" | cut -f1)"

say "== before-state =="
curl -s -m 10 "http://127.0.0.1:8888/version" | tee -a "$LOG"; echo
curl -s -m 15 "http://127.0.0.1:8888/v1/default/banks/fwh-main/stats" -o "$ART/stats_before.json"
say "before stats: $(head -c 200 "$ART/stats_before.json")"

say "== remove the local 0.9.0 hotfix (pristine 0.9.2 is the durable fix) =="
for f in "$HS/.venv/Lib/site-packages/hindsight_api/engine/llm_wrapper.py" \
         "$HS/.venv/Lib/site-packages/hindsight_api/engine/providers/openai_compatible_llm.py"; do
  [ -f "$f.orig-opencode-header" ] && cp "$f.orig-opencode-header" "$f" && say "restored $(basename "$f") from .orig"
done

say "== install $TARGET =="
uv pip install --python "$PYW" --upgrade "$TARGET" >> "$LOG" 2>&1
say "install rc=$?"
"$PY" -c "import importlib.metadata as m; print('installed:', m.version('hindsight-api-slim'))" 2>&1 | tee -a "$LOG"
rm -f "$HS/.venv/Lib/site-packages/hindsight_api/engine/llm_wrapper.py.orig-opencode-header" \
      "$HS/.venv/Lib/site-packages/hindsight_api/engine/providers/openai_compatible_llm.py.orig-opencode-header"

say "== restart =="
bash /c/Users/Fwhne/Documents/HermesOutput/hermes-memory-audit-20260912/restart_hindsight.sh > "$ART/restart_after_upgrade.log" 2>&1
grep -E "HEALTHY|FAIL|listener" "$ART/restart_after_upgrade.log" | tail -3 | tee -a "$LOG"

say "== gates =="
"$PY" "$ARTW/upgrade_gates.py" 2>&1 | tee -a "$LOG"
RC=${PIPESTATUS[0]}
if [ "$RC" = "0" ]; then
  say "VERDICT=PASS upgrade to 0.9.2 accepted (write path native, no venv patch)"
else
  say "VERDICT=FAIL -> rollback: cp -r $ART/pkg-0.9.0-snapshot/hindsight_api $HS/.venv/Lib/site-packages/ ; uv pip install --python $PYW -r $ART/requirements-before-v092.txt ; bash restart_hindsight.sh"
fi
exit 0
