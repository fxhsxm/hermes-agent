"""Specificity measurement in scripts/validate_golden_set.py.

Coverage says "the answer is in the bank somewhere". It does not say the question can DETECT a
change: the harness scores a hit on ANY expected token, so if the most common expected token
appears in hundreds of items, nearly every response is a hit and the question cannot show a
regression. These tests pin the classification and its any-of semantics.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "validate_golden_set.py"
_spec = importlib.util.spec_from_file_location("validate_golden_set", _PATH)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

fold = _mod.fold
builtin_rows = _mod.builtin_rows
coverage_rows = _mod.coverage_rows


def _texts(n_common, n_rare):
    return ([fold("the common token appears here")] * n_common
            + [fold("only one item says RARETOKEN")] * n_rare)


def test_a_specific_token_keeps_the_question_gate_eligible():
    rows, uncovered, _ = coverage_rows([{"id": "q1", "expect": ["RARETOKEN"]}],
                                       _texts(30, 2))
    assert rows[0]["max_token_items"] == 2
    assert rows[0]["low_specificity"] is False
    assert uncovered == []


def test_a_token_in_many_items_marks_the_question_low_specificity():
    rows, _, _ = coverage_rows([{"id": "q1", "expect": ["common token"]}], _texts(30, 1))
    assert rows[0]["max_token_items"] == 30
    assert rows[0]["low_specificity"] is True
    assert "LOW SPECIFICITY" in rows[0]["note"]
    assert rows[0]["covered"] is True          # still coverage evidence, just not a gate item


def test_any_of_means_the_most_common_token_decides():
    """One rare + one common token is still a trivially-satisfied question."""
    rows, _, _ = coverage_rows([{"id": "q1", "expect": ["RARETOKEN", "common token"]}],
                               _texts(30, 1))
    assert rows[0]["token_items"]["RARETOKEN"] == 1
    assert rows[0]["token_items"]["common token"] == 30
    assert rows[0]["max_token_items"] == 30 and rows[0]["low_specificity"] is True


def test_the_cut_off_is_configurable():
    qs = [{"id": "q1", "expect": ["RARETOKEN"]}]
    strict, _, _ = coverage_rows(qs, _texts(0, 25), low_spec_max_items=20)
    loose, _, _ = coverage_rows(qs, _texts(0, 25), low_spec_max_items=50)
    assert strict[0]["low_specificity"] is True
    assert loose[0]["low_specificity"] is False


def test_tier_is_echoed_so_the_report_can_split_the_score():
    rows, _, _ = coverage_rows(
        [{"id": "q1", "expect": ["RARETOKEN"], "tier": "environment"},
         {"id": "q2", "expect": ["RARETOKEN"]}], _texts(0, 2))
    assert rows[0]["tier"] == "environment"
    assert rows[1]["tier"] is None       # absent tier means the harness defaults it to core


def test_builtin_questions_are_verified_against_the_injected_store_not_the_bank():
    """The answer lives in MEMORY.md/USER.md, which is always in context - not in the bank."""
    files = {"USER.md": fold("只有用戶明確要求 OCR 時才做 OCR。禁止以 CDP 強殺瀏覽器。"),
             "MEMORY.md": fold("內置記憶層係 MEMORY.md。")}
    qs = [{"id": "b1", "tier": "builtin", "expect": ["強殺瀏覽器"]},
          {"id": "b2", "tier": "builtin", "expect": ["MEMORY.md"]},
          {"id": "b3", "tier": "builtin", "expect": ["唔存在嘅規則"]},
          {"id": "c1", "tier": "core", "expect": ["強殺瀏覽器"]}]
    rows, uncovered = builtin_rows(qs, files)
    got = {r["id"]: r for r in rows}
    assert uncovered == ["b3"]
    assert got["b1"]["found_in"]["強殺瀏覽器"] == ["USER.md"]
    assert got["b2"]["found_in"]["MEMORY.md"] == ["MEMORY.md"]
    assert got["b1"]["covered"] is True and got["b3"]["covered"] is False
    assert "NOT FOUND" in got["b3"]["note"]
    assert "c1" not in got          # a core question is judged against the bank, not the files
