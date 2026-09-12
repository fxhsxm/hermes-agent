"""Invariant tests for scripts/validate_golden_set.py.

A benchmark question whose expected value is not in the bank cannot measure retrieval - it can
only ever fail. These tests pin the check that separates coverage from recall, and the folding
that must match the harness exactly (otherwise validation and scoring disagree).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "validate_golden_set.py"
_spec = importlib.util.spec_from_file_location("validate_golden_set", _PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_fold_removes_punctuation_and_spaces_and_casefolds():
    assert _mod.fold("TXT 檔案交付預設用咩編碼？") == _mod.fold("TXT檔案交付預設用咩編碼")
    assert _mod.fold("HermesOutput") == _mod.fold("hermesoutput")
    assert _mod.fold("C:\\Users\\x") == _mod.fold("cusersx")


def test_fold_keeps_cjk_intact():
    assert "繁體" in _mod.fold("回覆以繁體書面語為主")
    assert _mod.fold("繁體") == "繁體"


def test_covered_question_counts_containing_items():
    questions = [{"id": "a", "expect": ["BOM"]}]
    rows, uncovered, none = _mod.coverage_rows(questions, [_mod.fold("TXT 預設 UTF-8 BOM")])
    assert rows[0]["covered"] is True and rows[0]["containing_items"] == 1
    assert uncovered == [] and none == []


def test_missing_value_is_reported_as_a_coverage_gap():
    """The point of the tool: a guaranteed miss must be flagged, not scored."""
    questions = [{"id": "gone", "expect": ["fwh-hindsight-local"]}]
    rows, uncovered, _ = _mod.coverage_rows(questions, [_mod.fold("worker id 係另一個值")])
    assert rows[0]["covered"] is False and rows[0]["containing_items"] == 0
    assert uncovered == ["gone"]
    assert "COVERAGE GAP" in rows[0]["note"]


def test_question_without_expect_is_not_scored():
    rows, uncovered, none = _mod.coverage_rows([{"id": "live", "expect": []}], [])
    assert rows[0]["covered"] is None and rows[0]["containing_items"] is None
    assert none == ["live"] and uncovered == []


def test_require_min_items_sets_the_coverage_bar():
    """One stray mention is weaker evidence than several; the caller decides the bar."""
    questions = [{"id": "rare", "expect": ["LANE_CONTRACT"]}]
    folded = [_mod.fold("LANE_CONTRACT.md 有寫")]
    rows_at_1, _, _ = _mod.coverage_rows(questions, folded, require_min_items=1)
    rows_at_3, uncovered, _ = _mod.coverage_rows(questions, folded, require_min_items=3)
    assert rows_at_1[0]["covered"] is True
    assert rows_at_3[0]["covered"] is False and uncovered == ["rare"]


def test_expect_matching_mirrors_the_harness_any_of_rule():
    """Any one of the expect tokens is enough, and the match is substring-on-folded-text."""
    questions = [{"id": "multi", "expect": ["never-this", "8888"]}]
    rows, _, _ = _mod.coverage_rows(questions, [_mod.fold("Hindsight API 用 8888 port")])
    assert rows[0]["covered"] is True