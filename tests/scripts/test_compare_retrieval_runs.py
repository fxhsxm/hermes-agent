"""Invariant tests for the backfill gate (scripts/compare_retrieval_runs.py).

The gate exists because the previous criterion stopped a batch on a 0.004 score gap whose
order was decided by noise. These tests pin the behaviour that matters:

  * a rank change on a question whose answer sits in a near-tie is excluded from pass/fail,
  * a rank change on a question whose answer held its rank by a real margin FAILS the gate
    and names the question,
  * losing an answer that was previously retrievable FAILS the gate,
  * an old harness output (no recall measurement) is reported as not comparable instead of
    silently passing.

They build harness-shaped documents in-process; nothing here touches a bank or the network.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "compare_retrieval_runs", REPO / "scripts" / "compare_retrieval_runs.py")
compare_mod = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = compare_mod
_SPEC.loader.exec_module(compare_mod)


def q(qid, rank, gap, expect=("TOKEN",), ok=True):
    """One harness question row: rank of the expected answer + the margin holding it."""
    return {
        "id": qid, "ok": ok, "n_results": 30,
        "recall": {"expected": list(expect), "hit_rank": rank, "hit@1": rank == 1,
                   "hit@3": bool(rank and rank <= 3), "hit@10": bool(rank and rank <= 10)},
        "decision": {"class": "decided" if (gap or 0) >= 0.05 else "unstable",
                     "margin_top1_top2": gap, "expect_answer_gap": gap,
                     "expect_answer_class": "decided" if (gap or 0) >= 0.05 else "unstable",
                     "top1_id": "item-1"},
    }


def doc(questions, tag="t", harness="1.1", recall=True, dup=0.20):
    rows = questions
    scored = [r for r in rows if r["recall"]["expected"]]
    agg = {
        "bank": "bank", "n_questions": len(rows), "n_answered": sum(1 for r in rows if r["ok"]),
        "failed_questions": [r["id"] for r in rows if not r["ok"]],
        "duplication": {"overall_duplicate_item_rate": dup, "total_items_returned": 300},
        "staleness_proxy": {"questions_top1_superseded_proxy_strict": 8},
        "ranking_decision": {
            "min_margin": 0.05,
            "decided_ids": [r["id"] for r in rows if r["decision"]["class"] == "decided"],
            "unstable_ids": [r["id"] for r in rows if r["decision"]["class"] == "unstable"]},
    }
    if recall and scored:
        agg["recall"] = {}
        for k in (1, 3, 10):
            hits = sum(1 for r in scored if r["recall"][f"hit@{k}"])
            agg["recall"][f"recall@{k}"] = {"hits": hits, "n": len(scored),
                                            "rate": round(hits / len(scored), 4)}
        agg["recall"]["n_scored"] = len(scored)
        agg["recall_at_1"] = agg["recall"]["recall@1"]["rate"]
        agg["recall_at_3"] = agg["recall"]["recall@3"]["rate"]
        agg["recall_at_10"] = agg["recall"]["recall@10"]["rate"]
    return {"harness_version": harness, "run": {"tag": tag, "min_margin": 0.05},
            "aggregate": agg, "questions": rows}


def verdicts(res):
    return {r["id"]: r["verdict"] for r in res["per_question"]}


def test_near_tie_flip_is_excluded_and_does_not_fail_the_gate():
    """A rank change at a tie (gap 0.004) is reported, never gated."""
    before = doc([q("tie", 1, 0.004), q("steady", 1, 0.4)], tag="before")
    after = doc([q("tie", 2, 0.004), q("steady", 1, 0.4)], tag="after")
    res = compare_mod.compare(before, after, 0.05, 0.02)
    assert verdicts(res)["tie"] == "UNSTABLE_FLIP_DOWN"
    assert res["regressions"] == []
    assert res["unstable_flips"] == ["tie"]
    assert res["all_ok"] is True, res["checks"]


def test_decided_rank_regression_fails_and_names_the_question():
    """An answer pushed down from a rank it held by a real margin is a REGRESSION."""
    before = doc([q("decided", 1, 0.4), q("steady", 1, 0.4)], tag="before")
    after = doc([q("decided", 3, 0.4), q("steady", 1, 0.4)], tag="after")
    res = compare_mod.compare(before, after, 0.05, 0.02)
    assert verdicts(res)["decided"] == "REGRESSION_RANK"
    assert res["regressions"] == ["decided"]
    assert res["all_ok"] is False
    assert res["checks"]["no_decided_rank_regression"] is False
    assert res["checks"]["recall@3_not_worse"] is True  # still inside the top 3


def test_lost_answer_fails_even_when_the_question_was_a_near_tie():
    """Present -> absent is a regression no margin can excuse."""
    before = doc([q("lost", 2, 0.004)], tag="before")
    after = doc([q("lost", None, 0.004)], tag="after")
    res = compare_mod.compare(before, after, 0.05, 0.02)
    assert verdicts(res)["lost"] == "REGRESSION_ANSWER_LOST"
    assert res["all_ok"] is False
    assert res["checks"]["no_answer_lost"] is False


def test_question_without_ground_truth_is_never_gated():
    """A liveness question (no expect list) is reported, never judged against a rank."""
    before = doc([q("scored", 1, 0.4), q("liveness", None, 0.03, expect=())], tag="before")
    after = doc([q("scored", 1, 0.4), q("liveness", None, 0.03, expect=())], tag="after")
    res = compare_mod.compare(before, after, 0.05, 0.02)
    assert verdicts(res)["liveness"] == "no_ground_truth"
    assert verdicts(res)["scored"] == "SAME"
    assert res["before"]["no_ground_truth_ids"] == ["liveness"]
    assert res["before"]["recall_n_scored"] == 1
    assert res["all_ok"] is True


def test_old_harness_output_is_not_comparable_not_a_silent_pass():
    """harness 1.0 emitted no recall block: comparing it must say so, not pass."""
    old = doc([q("q1", 1, 0.4)], tag="v1.0", harness="1.0", recall=False)
    old["aggregate"].pop("recall_at_1", None)
    new = doc([q("q1", 1, 0.4)], tag="v1.1")
    res = compare_mod.compare(old, new, 0.05, 0.02)
    assert res["checks"] == {}
    assert res["all_ok"] is False
    assert "never measured" in " ".join(res["before"]["limitations"])


def test_duplicate_rate_tolerance_is_enforced():
    before = doc([q("q1", 1, 0.4)], tag="before", dup=0.20)
    after = doc([q("q1", 1, 0.4)], tag="after", dup=0.25)
    res = compare_mod.compare(before, after, 0.05, 0.02)
    assert res["checks"]["dup_within_tolerance"] is False
    assert res["all_ok"] is False


def test_comparison_json_is_serialisable_and_lists_flips():
    before = doc([q("a", 1, 0.4), q("b", 2, 0.004)], tag="before")
    after = doc([q("a", 1, 0.4), q("b", 1, 0.004)], tag="after")
    res = compare_mod.compare(before, after, 0.05, 0.02)
    json.dumps(res)  # must not raise
    assert res["flipped"] == ["b"]
    assert verdicts(res)["b"].startswith("UNSTABLE_FLIP_UP")
