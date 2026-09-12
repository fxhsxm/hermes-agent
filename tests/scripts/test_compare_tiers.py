"""Tier and specificity behaviour of the retrieval gate (scripts/compare_retrieval_runs.py).

Two things the gate must get right, because getting them wrong is how a backfill batch gets
stopped for nothing (or waved through):
  * only the CORE tier (facts and rules that exist only because they were decided or observed in
    conversation) carries the verdict; environment facts a live tool can read back are smoke;
  * a question whose expected token appears in hundreds of items cannot detect a regression, so
    it is excluded from the verdict rather than counted as a pass.

Documents are built in-process in the same shape the harness writes; nothing here touches a bank
or the network.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "compare_retrieval_runs.py"
_spec = importlib.util.spec_from_file_location("compare_retrieval_runs", _PATH)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
compare = _mod.compare


def row(qid, tier, rank, gap, expect=("TOKEN",)):
    return {
        "id": qid, "tier": tier, "ok": True, "n_results": 30,
        "recall": {"expected": list(expect) or None, "hit_rank": rank,
                   "hit@1": rank == 1, "hit@3": bool(rank and rank <= 3),
                   "hit@10": bool(rank and rank <= 10)},
        "decision": {"class": "decided" if gap >= 0.05 else "unstable",
                     "margin_top1_top2": gap, "expect_answer_gap": gap,
                     "expect_answer_class": "decided" if gap >= 0.05 else "unstable",
                     "top1_id": "item-1"},
    }


def doc(rows, tag="t", harness="1.2", dup=0.20):
    scored = [r for r in rows if r["recall"]["expected"]]
    agg = {
        "bank": "bank", "n_questions": len(rows), "n_answered": len(rows),
        "failed_questions": [],
        "duplication": {"overall_duplicate_item_rate": dup, "total_items_returned": 30 * len(rows)},
        "staleness_proxy": {"questions_top1_superseded_proxy_strict": 0},
        "ranking_decision": {
            "min_margin": 0.05,
            "decided_ids": [r["id"] for r in rows if r["decision"]["class"] == "decided"],
            "unstable_ids": [r["id"] for r in rows if r["decision"]["class"] == "unstable"],
            "margins": {r["id"]: r["decision"]["margin_top1_top2"] for r in rows},
        },
    }
    if scored:
        agg["recall"] = {}
        for k in (1, 3, 10):
            hits = sum(1 for r in scored if r["recall"][f"hit@{k}"])
            agg["recall"][f"recall@{k}"] = {"hits": hits, "n": len(scored),
                                            "rate": round(hits / len(scored), 4)}
        agg["recall"]["n_scored"] = len(scored)
        agg["recall_at_1"] = agg["recall"]["recall@1"]["rate"]
        agg["recall_at_3"] = agg["recall"]["recall@3"]["rate"]
        agg["recall_at_10"] = agg["recall"]["recall@10"]["rate"]
    agg["by_tier"] = {}
    for tier in sorted({r["tier"] for r in rows}):
        rows_t = [r for r in rows if r["tier"] == tier]
        scored_t = [r for r in rows_t if r["recall"]["expected"]]
        block = {"n_questions": len(rows_t), "n_scored": len(scored_t)}
        for k in (1, 3, 10):
            hits = sum(1 for r in scored_t if r["recall"][f"hit@{k}"])
            rate = round(hits / len(scored_t), 4) if scored_t else None
            block[f"recall@{k}"] = {"hits": hits, "n": len(scored_t), "rate": rate}
            block[f"recall_at_{k}"] = rate
        agg["by_tier"][tier] = block
    return {"harness_version": harness, "run": {"tag": tag, "min_margin": 0.05},
            "aggregate": agg, "questions": rows}


def verdict_of(res, qid):
    return {r["id"]: r for r in res["per_question"]}[qid]["verdict"]


def test_environment_questions_are_reported_but_never_gated():
    """A port/version question moving rank is smoke, not a regression."""
    before = doc([row("q-core", "core", 1, 0.2), row("q-env", "environment", 2, 0.2)])
    after = doc([row("q-core", "core", 1, 0.2), row("q-env", "environment", 9, 0.2)])
    res = compare(before, after, 0.05, 0.02, tier="core")
    assert verdict_of(res, "q-env") == "not_gated_tier"
    assert [r for r in res["per_question"] if r["id"] == "q-env"][0]["rank_after"] == 9
    assert res["regressions"] == []               # reported, never a regression
    assert res["all_ok"] is True
    assert res["tier_summary"]["gated"]["tier"] == "core"
    assert res["tier_summary"]["after"]["environment"]["recall@3"] == 0.0
    assert "q-env" in res["skipped"]


def test_a_real_core_regression_still_fails_the_gate():
    """The tier filter must not weaken the verdict it exists to protect."""
    before = doc([row("q-core", "core", 1, 0.2), row("q-core2", "core", 2, 0.2)])
    after = doc([row("q-core", "core", 1, 0.2), row("q-core2", "core", 6, 0.2)])
    res = compare(before, after, 0.05, 0.02, tier="core")
    assert res["regressions"] == ["q-core2"]
    assert res["all_ok"] is False
    assert res["action"].startswith("STOP")


def test_low_specificity_questions_are_excluded_from_the_verdict():
    """Hundreds of items contain the token, so a hit means nothing either way."""
    before = doc([row("q-core", "core", 1, 0.2), row("q-common", "core", 1, 0.2)])
    after = doc([row("q-core", "core", 1, 0.2), row("q-common", "core", 8, 0.2)])
    res = compare(before, after, 0.05, 0.02, tier="core", low_specificity_ids=["q-common"])
    assert verdict_of(res, "q-common") == "excluded_low_specificity"
    assert res["regressions"] == []
    assert res["all_ok"] is True
    assert res["low_specificity_excluded"] == ["q-common"]
    assert "q-common" not in res["gated_ids"]


def test_core_recall_at_3_is_the_gate_not_the_overall_rate():
    """The recall check must be named for the tier it measured, not left tier-agnostic."""
    before = doc([row("q-core", "core", 1, 0.2), row("q-env", "environment", 1, 0.2)])
    after = doc([row("q-core", "core", 2, 0.2), row("q-env", "environment", 1, 0.2)])
    res = compare(before, after, 0.05, 0.02, tier="core")
    g = res["tier_summary"]["gated"]
    assert g["recall@3_before"] == 1.0 and g["recall@3_after"] == 1.0   # both still top-3
    assert res["checks"]["core_recall@3_not_worse"] is True
    assert "recall@3_not_worse" not in res["checks"]
    assert g["tier_data_missing"] is False


def test_a_question_set_without_tiers_is_judged_on_the_overall_rate_and_says_so():
    """A 1.1-era run has no tier information at all; judge it, but label the check honestly."""
    before = doc([row("q1", "core", 1, 0.2)])
    after = doc([row("q1", "core", 1, 0.2)])
    for d in (before, after):
        del d["aggregate"]["by_tier"]
        for r in d["questions"]:
            del r["tier"]          # as a pre-tier question set would be
    res = compare(before, after, 0.05, 0.02, tier="core")
    assert res["checks"]["recall@3_not_worse"] is True
    assert res["tier_summary"]["gated"]["tier_data_missing"] is True
    assert "core_recall@3_not_worse" not in res["checks"]
