"""Daily memory watchdog: silent when healthy, prints the report only on WARN/FAIL."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from memory_ops_common import health, AUD

r = health(no_probe=True, out="health-daily.json")
if r.returncode == 0:
    sys.exit(0)                      # empty stdout => cron sends nothing
print(f"\u26a0 Memory health check exit={r.returncode} (1=warnings, 2=degraded)")
print((r.stdout or "")[-4000:])
if r.stderr:
    print(r.stderr[-800:])
