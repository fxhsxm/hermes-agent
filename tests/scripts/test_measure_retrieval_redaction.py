"""Invariant tests for the harness's privacy redaction (scripts/measure_retrieval_quality.py).

Committed harness outputs go into a public repository's audit directory, so memory-derived text
must not be in them. The numbers a judge needs must survive redaction untouched - otherwise the
"fix" would silently invalidate the reports.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "measure_retrieval_quality.py"
_spec = importlib.util.spec_from_file_location("measure_retrieval_quality", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load measure_retrieval_quality.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _doc():
    return {
        "run": {"tag": "t"},
        "aggregate": {
            "duplication": {"overall_duplicate_item_rate": 0.21,
                            "pairs": [{"i": 1, "j": 2, "a_text": "some memory sentence here",
                                       "b_text": "some memory sentence here",
                                       "similarity": 1.0}]},
        },
        "questions": [{
            "id": "q1", "query": "which port?",
            "top1": {"id": "doc-1", "final": 0.63, "text": "the answer is 8888 in this memory"},
            "recall": {"expect": ["8888"], "hit_rank": 1, "hit@1": True},
            "decision": {"margin_top1_top2": 0.0086, "class": "unstable"},
            "backfill": {"expected_rank": 1, "expected_rank_without_backfill": None},
            "ranking_proxy": {"evidence": {"sibling_text": "another memory sentence here",
                                           "sibling_id": "doc-2", "token_overlap": 0.5}},
            "stale_fresh": {"stale_contexts": [{"window": "memory text window content"}],
                            "stale_best_rank": 2},
        }],
    }


def test_redact_replaces_memory_text_with_a_length_marker():
    doc = _doc()
    n = _mod.redact_text(doc)
    assert n == 5  # top1.text, a_text, b_text, sibling_text, window
    q = doc["questions"][0]
    assert q["top1"]["text"].startswith("[redacted")
    assert doc["aggregate"]["duplication"]["pairs"][0]["a_text"].startswith("[redacted")
    assert q["ranking_proxy"]["evidence"]["sibling_text"].startswith("[redacted")
    assert q["stale_fresh"]["stale_contexts"][0]["window"].startswith("[redacted")


def test_redaction_keeps_every_number_a_judge_reads():
    doc = _doc()
    _mod.redact_text(doc)
    q = doc["questions"][0]
    assert q["top1"]["final"] == 0.63
    assert q["top1"]["id"] == "doc-1"
    assert q["recall"]["hit_rank"] == 1 and q["recall"]["expect"] == ["8888"]
    assert q["decision"]["margin_top1_top2"] == 0.0086
    assert q["backfill"]["expected_rank"] == 1
    assert doc["aggregate"]["duplication"]["overall_duplicate_item_rate"] == 0.21


def test_redaction_leaves_our_own_question_text_alone():
    """The query is the benchmark definition we authored, not memory content."""
    doc = _doc()
    _mod.redact_text(doc)
    assert doc["questions"][0]["query"] == "which port?"


def test_redacted_output_has_no_long_free_text_left():
    doc = _doc()
    _mod.redact_text(doc)
    blob = json.dumps(doc, ensure_ascii=False)
    assert "memory sentence" not in blob and "memory text window" not in blob


def test_question_text_and_expected_tokens_are_redacted_too():
    """A question plus its expected token IS memory content, even without the item text."""
    doc = {"questions": [{"id": "q1", "query": "一般交付檔案應該寫入邊個目錄？",
                          "tier": "core", "recall": {"expected": ["Documents\\HermesOutput"],
                                                     "hit_rank": 2}}]}
    n = _mod.redact_questions(doc)
    q = doc["questions"][0]
    assert n == 2
    assert "HermesOutput" not in json.dumps(doc, ensure_ascii=False)
    assert q["query"].startswith("[redacted question")
    assert q["recall"]["expected"] == ["[redacted 1 token(s)]"]
    assert q["recall"]["expected"]                      # truthy: still a scored question
    assert q["recall"]["hit_rank"] == 2                 # the numbers the gate reads survive


def test_a_question_without_ground_truth_stays_unscored_after_redaction():
    doc = {"questions": [{"id": "live", "query": "x", "tier": "liveness",
                          "recall": {"expected": [], "hit_rank": None}}]}
    _mod.redact_questions(doc)
    assert not doc["questions"][0]["recall"]["expected"]   # redaction must not invent a miss


def _agg_rows(tiers):
    """Minimal harness-shaped rows: `tiers` is [(tier, hit_rank)] (None rank = no hit)."""
    rows = []
    for i, (tier, rank) in enumerate(tiers):
        rows.append({
            "id": f"q{i}", "ok": True, "tier": tier, "n_results": 10, "top1_is_raw": False,
            "latency_s": 0.1,
            "recall": {"expected": ["T"], "hit@1": rank == 1,
                       "hit@3": rank is not None and rank <= 3,
                       "hit@10": rank is not None and rank <= 10,
                       "k_capped_by_response": False, "expected_rank": rank,
                       "expected_rank_without_backfill": rank},
            "decision": {"class": "decided", "margin_top1_top2": 0.2},
            "duplication": {"duplicate_pairs": 0, "items_in_duplicate_pair": 0,
                            "identical_pairs": 0, "fuzzy_pairs": 0,
                            "observation_vs_raw_twin_pairs": 0, "duplicate_item_rate": 0.0},
            "backfill": {"n_backfilled_items": 0, "answer_rank_changed_by_backfill": False},
            "reranker": {"reranker_engaged": False, "n_with_reranker_score": 0,
                         "reranker_changed_top1_vs_semantic": False},
            # no "stale_fresh"/"reflect" keys on purpose: aggregate() gates on key PRESENCE, so
            # a None value here would be treated as a probed result
            "top1": {"id": "item-1"},
        })
    return rows


def test_the_headline_recall_is_the_core_tier_only():
    """builtin answers live in the injected store and must not drag the bank benchmark down."""
    rows = _agg_rows([("core", 1), ("core", 4), ("environment", 1), ("builtin", None),
                      ("liveness", None)])
    agg = _mod.aggregate(rows, "bank")
    assert agg["recall"]["n_scored"] == 2          # the two core questions, nothing else
    assert agg["recall_at_1"] == 0.5 and agg["recall_at_3"] == 0.5
    assert "core tier only" in agg["recall_scope"]
    assert agg["recall_bank_scored"]["n_scored"] == 3      # core + environment, kept for continuity
    assert agg["by_tier"]["builtin"]["bank_scored"] is False
    assert agg["by_tier"]["builtin"]["store"] == "memory files"
    assert agg["by_tier"]["core"]["bank_scored"] is True
