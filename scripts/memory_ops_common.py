"""Memory System v2 ops helper (Windows-native; bash wrappers are unreliable under the cron runner)."""
import json, os, subprocess, sys, datetime, pathlib

OPS = pathlib.Path(__file__).resolve().parent
PY = r"C:/Users/Fwhne/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe"          # live Hermes venv (urllib/httpx available)
AUD = pathlib.Path(r"C:/Users/Fwhne/Documents/HermesOutput/hermes-memory-audit-20260912")
AUDW = str(AUD).replace("\\", "/")


def run(cmd, timeout=1200):
    try:
        # encoding on the same line as text= is required by scripts/check-windows-footguns.py:
        # text=True without it decodes child output with the locale code page (cp936 here).
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(cmd, 124, "", f"timeout: {exc}")


def health(no_probe=True, out="health.json", extra=()):
    cmd = [PY, str(OPS / "memory_health_check.py"), "--json", str(AUD / "v2" / out), *extra]
    if no_probe:
        cmd.append("--no-probe-write")
    return run(cmd)


def retrieval(out="retrieval.json", tag="run"):
    return run([PY, str(OPS / "measure_retrieval_quality.py"), "--bank", "fwh-main",
                "--questions", str(OPS / "retrieval_questions.json"),
                "--out", str(AUD / "v2" / out), "--tag", tag])


def stamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
