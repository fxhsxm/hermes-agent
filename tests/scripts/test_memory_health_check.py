"""Tests for scripts/memory_health_check.py.

The health check is the recurring signal for a memory system whose failure mode is
silence (a green /health while nothing is being written), so its parsing and its
severity aggregation are contracts worth pinning:

* config.yaml parsing must read the ``memory:`` block — reading the whole file picks up
  ``model.provider`` (proven in production: the check reported the wrong provider).
* the worst finding must dominate the exit status, otherwise a degraded system could
  report "healthy" because the last check happened to pass.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "memory_health_check.py"
_spec = importlib.util.spec_from_file_location("memory_health_check", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load memory_health_check.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


CONFIG = """\
model:
  provider: opencode-go
memory:
  memory_enabled: true
  memory_char_limit: 2200
  user_char_limit: 1375
  provider: hindsight
delegation:
  provider: ''
"""


def test_memory_block_is_scoped_to_the_memory_section(tmp_path):
    """A whole-file scan returns model.provider; only the memory block is correct."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG, encoding="utf-8")
    block = _mod._memory_block(cfg)
    assert block["provider"] == "hindsight"
    assert block["memory_char_limit"] == "2200"
    assert "opencode-go" not in block.values()


def test_memory_block_reads_repo_style_config_without_memory_section(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("model:\n  provider: openai\n", encoding="utf-8")
    assert _mod._memory_block(cfg) == {}


def test_provider_wiring_reports_the_memory_provider(tmp_path):
    home = tmp_path / "hermes"
    (home / "hindsight").mkdir(parents=True)
    (home / "hindsight" / "config.json").write_text(
        json.dumps({"mode": "local_external", "api_url": "http://127.0.0.1:8888", "bank_id": "b"}), encoding="utf-8")
    (home / "config.yaml").write_text(CONFIG, encoding="utf-8")
    res = _mod.Result()
    assert _mod.check_wiring(res, home) == "b"
    assert res.status == 0
    assert any(c["check"] == "provider_wiring" and c["level"] == "ok" for c in res.checks)


def test_wrong_provider_is_a_failure(tmp_path):
    home = tmp_path / "hermes"
    (home / "hindsight").mkdir(parents=True)
    (home / "hindsight" / "config.json").write_text(json.dumps({"bank_id": "b"}), encoding="utf-8")
    (home / "config.yaml").write_text(CONFIG.replace("provider: hindsight", "provider: mem0"), encoding="utf-8")
    res = _mod.Result()
    _mod.check_wiring(res, home)
    assert res.status == 2


def test_severity_is_the_worst_finding_not_the_last():
    res = _mod.Result()
    res.add("a", "ok", "fine")
    res.add("b", "fail", "broken")
    res.add("c", "ok", "fine again")
    assert res.status == 2
    assert res.json()["status"] == "degraded"


def test_write_age_thresholds_map_to_warn_and_fail():
    """The freshness signal is what catches a silent write outage, so the bands matter."""
    fresh = _mod._age_hours("2026-01-01T00:00:00+00:00")
    assert fresh is not None and fresh > _mod.FAIL_WRITE_AGE_H
    assert _mod.WARN_WRITE_AGE_H < _mod.FAIL_WRITE_AGE_H
    assert _mod._age_hours(None) is None
    assert _mod._age_hours("not-a-timestamp") is None


def test_builtin_store_threshold_is_the_documented_85_percent():
    """The code and the runbook must agree, or the check warns at a band nobody documented."""
    assert _mod.WARN_BUILTIN_FULL == 0.85


def _operations(monkeypatch, stats, prior=None):
    """Run check_operations against a stubbed stats/operations endpoint."""
    import httpx

    class Resp:
        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

    def fake_get(url, timeout=None, **kw):
        return Resp({"operations": []} if "operations" in url else stats)

    monkeypatch.setattr(httpx, "get", fake_get)
    res = _mod.Result()
    _mod.check_operations(res, "http://api", "bank", 50, prior=prior)
    return res, [c for c in res.checks if c["check"] == "failed_consolidation"]


def test_historical_consolidation_failure_stops_warning_once_unchanged(monkeypatch):
    """It is a cumulative counter: unchanged means historical residue, not a live fault."""
    stats = {"last_memory_write_at": None, "last_consolidated_at": None,
             "pending_operations": 0, "failed_consolidation": 1}
    _res, fc = _operations(monkeypatch, stats, prior={"failed_consolidation": 1})
    assert fc[0]["level"] == "ok" and "historical" in fc[0]["detail"]


def test_a_new_consolidation_failure_warns(monkeypatch):
    stats = {"last_memory_write_at": None, "last_consolidated_at": None,
             "pending_operations": 0, "failed_consolidation": 2}
    res, fc = _operations(monkeypatch, stats, prior={"failed_consolidation": 1})
    assert fc[0]["level"] == "warn" and "up from 1" in fc[0]["detail"]
    assert res.status == 1


def _trend(tmp_path, samples, pending, cons_hours_ago=0.0):
    """Run the trend check with a seeded history and return its finding."""
    import datetime as dt

    hist = tmp_path / "pending_consolidation_history.json"
    hist.write_text(json.dumps([{"at": f"2026-01-0{i + 1}T00:00:00+00:00",
                                 "pending_consolidation": s} for i, s in enumerate(samples)]),
                    encoding="utf-8")
    stats = {"pending_consolidation": pending, "pending_operations": 0,
             "failed_consolidation": 0, "total_documents": 1,
             "last_consolidated_at": (_mod._now() - dt.timedelta(hours=cons_hours_ago))
             .isoformat(timespec="seconds")}
    res = _mod.Result()
    _mod.check_pending_consolidation(res, stats, str(hist), 3, 6)
    return [c for c in res.checks if c["check"] == "pending_consolidation"][0], res


def test_pending_consolidation_empty_is_ok(tmp_path):
    finding, res = _trend(tmp_path, [], 0)
    assert finding["level"] == "ok" and finding["trend"] == "empty"
    assert res.status == 0


def test_pending_consolidation_draining_is_ok_even_when_large(tmp_path):
    """A big number right after a backfill is healthy as long as it is falling."""
    finding, _ = _trend(tmp_path, [1000], 738)
    assert finding["level"] == "ok" and finding["trend"] == "draining"


def test_pending_consolidation_flat_streak_warns(tmp_path):
    """Three consecutive non-draining readings is the first real signal, not one reading."""
    finding, res = _trend(tmp_path, [5, 5], 5)
    assert finding["level"] == "warn" and finding["trend"] == "not_draining"
    assert res.status == 1


def test_pending_consolidation_long_stall_fails(tmp_path):
    finding, res = _trend(tmp_path, [5, 5, 5, 5, 5, 5], 5)
    assert finding["level"] == "fail" and finding["trend"] == "stalled"
    assert res.status == 2


def test_pending_consolidation_with_stale_last_consolidation_fails(tmp_path):
    """Pending work plus a consolidation older than a day is a stall, whatever the streak."""
    finding, _ = _trend(tmp_path, [5], 5, cons_hours_ago=30.0)
    assert finding["level"] == "fail" and finding["trend"] == "stalled"


def test_pending_consolidation_history_is_written_and_bounded(tmp_path):
    finding, _ = _trend(tmp_path, [9], 8)
    hist = json.loads((tmp_path / "pending_consolidation_history.json").read_text(encoding="utf-8"))
    assert len(hist) == 2 and hist[-1]["pending_consolidation"] == 8
    assert finding["history_written"] is True


def test_pending_consolidation_survives_an_unwritable_history(tmp_path):
    """The trend check itself must not fail because it cannot write its own scratch file."""
    res = _mod.Result()
    _mod.check_pending_consolidation(
        res, {"pending_consolidation": 0, "last_consolidated_at": None},
        str(tmp_path), 3, 6)  # a DIRECTORY, so the write must fail
    finding = [c for c in res.checks if c["check"] == "pending_consolidation"][0]
    assert finding["level"] == "ok" and finding["history_written"] is False


def test_an_unwritable_history_warns_because_the_trend_monitor_goes_blind(tmp_path):
    """Without samples the check cannot tell draining from stuck - that must be visible.

    A silently unwritable history turns the trend monitor into a single-reading check, which is
    exactly the failure mode the trend was added to remove. So it warns (status 1) instead of
    only noting it in a detail field.
    """
    res = _mod.Result()
    _mod.check_pending_consolidation(
        res, {"pending_consolidation": 0, "last_consolidated_at": None},
        str(tmp_path), 3, 6)
    blind = [c for c in res.checks if c["check"] == "pending_consolidation_history"]
    assert len(blind) == 1 and blind[0]["level"] == "warn"
    assert res.status == 1
