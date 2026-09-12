#!/usr/bin/env bash
# Controlled Hindsight API restart: stop the API process, re-run the single Task
# Scheduler supervisor, then verify /health and the bank. Bounded, timed, logged.
set -u
export MSYS_NO_PATHCONV=1
TASK='Hindsight API Supervisor'
LOGDIR='C:/Users/Fwhne/Documents/HermesOutput/hermes-memory-audit-20260912'
RUNLOG="$LOGDIR/restart_run.log"
: > "$RUNLOG"
say() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$RUNLOG"; }

T0=$(date +%s)
PID=$(netstat -ano | grep ':8888' | grep -i LISTENING | awk '{print $5}' | head -1)
say "listener pid=$PID"
if [ -z "$PID" ]; then say "no listener; nothing to stop"; else
  say "soft kill (taskkill /PID $PID)"
  taskkill /PID "$PID" >/dev/null 2>&1 || say "soft kill returned non-zero"
  for i in $(seq 1 10); do
    sleep 1
    if ! netstat -ano | grep ':8888' | grep -qi LISTENING; then break; fi
  done
  if netstat -ano | grep ':8888' | grep -qi LISTENING; then
    say "port still open after 10s -> force kill"
    taskkill /PID "$PID" /F >/dev/null 2>&1
    sleep 2
  fi
  if netstat -ano | grep ':8888' | grep -qi LISTENING; then
    say "FAIL: port 8888 still listening"; exit 3
  fi
fi
T1=$(date +%s); say "API stopped after $((T1-T0))s"

say "triggering supervisor task"
schtasks /run /tn "$TASK" >/dev/null 2>&1 || say "schtasks /run returned non-zero"

OK=0
for i in $(seq 1 60); do
  BODY=$(curl -s -m 5 http://127.0.0.1:8888/health 2>/dev/null)
  case "$BODY" in *'"status":"healthy"'*) OK=1; break;; esac
  sleep 3
done
T2=$(date +%s)
if [ "$OK" = "1" ]; then
  say "HEALTHY after $((T2-T1))s (total downtime $((T2-T0))s)"
  say "version: $(curl -s -m 10 http://127.0.0.1:8888/version)"
  say "banks:   $(curl -s -m 15 http://127.0.0.1:8888/v1/default/banks)"
else
  say "FAIL: /health not healthy within 180s; last body=$BODY"
fi
NEWPID=$(netstat -ano | grep ':8888' | grep -i LISTENING | awk '{print $5}' | head -1)
say "new listener pid=$NEWPID"
NEWEST=$(ls -t /c/Users/Fwhne/hindsight/logs/supervisor-*.log | head -1)
say "newest supervisor log: $NEWEST"
tail -n 12 "$NEWEST" | tee -a "$RUNLOG"
exit 0
