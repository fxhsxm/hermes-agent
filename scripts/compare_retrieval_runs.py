#!/usr/bin/env python3
"""compare_retrieval_runs.py -- per-question before/after diff and the backfill GATE.

Takes two outputs of scripts/measure_retrieval_quality.py (harness >= 1.1) and answers the
two questions a backfill batch must answer, with the flipped question NAMED instead of left
to a human diffing ids:

  1. Did any known-correct answer's RANK get worse?
  2. If so, is the move meaningful, or a near-tie decided by 0.003 of score noise?

PASS/FAIL CRITERIA (only these can stop a batch)
  <tier>_recall@3_not_worse   the expected answer stays inside the top 3 as often as before, on the
                            GATING TIER (default `core`: facts and rules that exist only because
                            they were decided or observed in conversation). `environment`
                            questions (port, version, model, path, cadence - all readable by a
                            live tool) are reported as recall smoke and never gate, because
                            remembering them proves nothing a `curl` would not.
  no_answer_lost            an answer that was present in the response is still present
  no_decided_rank_regression  a question whose answer held its rank by a real margin
                            (>= --min-margin) did not move down
  all_questions_answered    neither run has a failed/errored question
  dup_within_tolerance      duplicate-item rate did not rise by more than --dup-tolerance

Questions whose expected token is too common in the bank (see --specificity, from
validate_golden_set.py) are excluded from the verdict: an any-of match against a token that
appears in hundreds of items makes every response a hit, so such a question cannot detect a
regression in either direction. They are listed in `low_specificity_excluded`.

Everything else is REPORTED, never gated: recall@1 (a single near-tie flip moves it),
per-question rank changes on questions whose answer sits in a tie, and the staleness proxy.

USAGE
  python compare_retrieval_runs.py --before batch4_before.json --after batch4_after.json \
      --specificity golden_set_coverage.json --out batch4_compare.json
  # exit code: 0 = gate passed, 3 = gate failed, 2 = the runs are not comparable
"""
from __future__ import annotations

import argparse
import json
import os
import sys

VERSION = "1.2"
DEFAULT_DUP_TOLERANCE = 0.02


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def qindex(doc):
    """{id: row} for the questions actually answered in a harness output."""
    return {q["id"]: q for q in doc.get("questions", [])}


def run_facts(doc):
    agg = doc.get("aggregate") or {}
    rec = agg.get("recall") or {}
    rank = agg.get("ranking_decision") or {}
    per = {}
    for qid, q in qindex(doc).items():
        dec = q.get("decision") or {}
        recall = q.get("recall") or {}
        has_expect = bool(recall.get("expected"))
        per[qid] = {
            "tier": q.get("tier") or "core",
            "tier_explicit": "tier" in q,
            "has_expect": has_expect,
            "rank": recall.get("hit_rank") if has_expect else None,
            "present": bool(recall.get("hit_rank")) if has_expect else None,
            "hit@1": recall.get("hit@1") if has_expect else None,
            "hit@3": recall.get("hit@3") if has_expect else None,
            "has_expect": has_expect,
            "ok": bool(q.get("ok")),
            "n_results": q.get("n_results"),
            "margin_top1_top2": dec.get("margin_top1_top2"),
            "class": dec.get("class"),
            "answer_gap": dec.get("expect_answer_gap"),
            "answer_class": dec.get("expect_answer_class"),
            "top1_id": dec.get("top1_id"),
        }
    misses = []
    if not rec:
        misses.append("no recall block (harness < 1.1, or a question set without an "
                      "expect/expected key): recall was never measured in this run")
    return {
        "tag": (doc.get("run") or {}).get("tag"),
        "harness_version": doc.get("harness_version"),
        "min_margin": (doc.get("run") or {}).get("min_margin"),
        "n_questions": agg.get("n_questions"),
        "n_answered": agg.get("n_answered"),
        "recall@1": (rec.get("recall@1") or {}).get("rate"),
        "recall@3": (rec.get("recall@3") or {}).get("rate"),
        "recall@10": (rec.get("recall@10") or {}).get("rate"),
        "recall_n_scored": rec.get("n_scored"),
        "dup_item_rate": (agg.get("duplication") or {}).get("overall_duplicate_item_rate"),
        "dup_total_items": (agg.get("duplication") or {}).get("total_items_returned"),
        "decided_ids": rank.get("decided_ids"),
        "unstable_ids": rank.get("unstable_ids"),
        "by_tier": agg.get("by_tier"),
        "no_ground_truth_ids": [qid for qid, v in per.items() if not v["has_expect"]],
        "failed_questions": agg.get("failed_questions") or [],
        "staleness_proxy_strict": (agg.get("staleness_proxy") or {})
                                 .get("questions_top1_superseded_proxy_strict"),
        "recall_without_backfill": agg.get("recall_without_backfill"),
        "backfill_effect": agg.get("backfill_effect"),
        "limitations": misses,
        "per_question": per,
    }


def judge(before_q, after_q, min_margin):
    """Verdict for one question, naming why it is or is not a regression."""
    if not before_q["has_expect"] and not after_q["has_expect"]:
        return "no_ground_truth", "question carries no expect/expected list (liveness check)"
    rb, ra = before_q["rank"], after_q["rank"]
    pb, pa = before_q["present"], after_q["present"]
    if not after_q["ok"] or not before_q["ok"]:
        return "unknown", "a run failed for this question"
    if pb and not pa:
        return "REGRESSION_ANSWER_LOST", f"answer present at rank {rb} before, absent after"
    if not pb and pa:
        return "IMPROVED", f"answer absent before, now rank {ra}"
    if rb is None and ra is None:
        return "SAME_ABSENT", "answer absent in both runs (a coverage gap, not a ranking change)"
    if ra == rb:
        return "SAME", f"rank {rb} unchanged"
    moved_down = ra > rb
    gap = min([g for g in (before_q["answer_gap"], after_q["answer_gap"]) if g is not None],
              default=None)
    decided = gap is not None and gap >= min_margin
    if moved_down and decided:
        return "REGRESSION_RANK", (f"rank {rb} -> {ra} with a real holding margin "
                                   f"({gap:+.4f} >= {min_margin})")
    if moved_down:
        return "UNSTABLE_FLIP_DOWN", (f"rank {rb} -> {ra} but the answer's holding margin is "
                                      f"{gap if gap is None else format(gap, '+.4f')} "
                                      f"(< {min_margin}) -> near-tie, excluded from pass/fail")
    return ("IMPROVED" if decided else "UNSTABLE_FLIP_UP"), (
        f"rank {rb} -> {ra} (holding margin {gap if gap is None else format(gap, '+.4f')})")


def compare(before, after, min_margin, dup_tolerance, tier="core", low_specificity_ids=None):
    """Judge the runs. `tier` selects which questions carry the verdict (default: core).

    `environment` questions (facts a live tool can read back) are reported as recall smoke and
    never gate; `low_specificity_ids` (from validate_golden_set.py, tokens present in a large
    share of the bank) are excluded from the verdict for the same reason - they make every
    response a hit and cannot detect a regression.
    """
    low_spec = set(low_specificity_ids or ())
    fb, fa = run_facts(before), run_facts(after)
    hb, ha = fb["harness_version"], fa["harness_version"]
    comparable = not fb["limitations"] and not fa["limitations"]

    ids = list(fb["per_question"].keys()) + [i for i in fa["per_question"] if i not in fb["per_question"]]
    rows, counts = [], {}
    for qid in ids:
        b = fb["per_question"].get(qid)
        a = fa["per_question"].get(qid)
        q_tier = (a or b or {}).get("tier") or "core"
        if b is None or a is None:
            verdict, note = "unknown", "question missing from one run"
        elif not comparable:
            verdict, note = "unknown", "not comparable"
        elif q_tier != tier:
            verdict, note = ("not_gated_tier",
                             f"tier={q_tier}: reported, not part of the {tier}-tier verdict")
        elif qid in low_spec:
            verdict, note = ("excluded_low_specificity",
                             "expected token is too common in the bank to detect a regression")
        else:
            verdict, note = judge(b, a, min_margin)
        counts[verdict] = counts.get(verdict, 0) + 1
        rows.append({
            "id": qid,
            "tier": q_tier,
            "rank_before": None if b is None else b["rank"],
            "rank_after": None if a is None else a["rank"],
            "delta": (None if (b is None or a is None or b["rank"] is None or a["rank"] is None)
                      else a["rank"] - b["rank"]),
            "class_before": None if b is None else b["class"],
            "class_after": None if a is None else a["class"],
            "answer_gap_before": None if b is None else b["answer_gap"],
            "answer_gap_after": None if a is None else a["answer_gap"],
            "decided": bool(b and a and b["class"] == "decided" and a["class"] == "decided"),
            "verdict": verdict,
            "note": note,
        })

    gated = [r for r in rows if r["tier"] == tier and r["id"] not in low_spec]
    real_regressions = [r["id"] for r in gated
                        if r["verdict"] in ("REGRESSION_RANK", "REGRESSION_ANSWER_LOST")]
    unstable_flips = [r["id"] for r in gated if r["verdict"].startswith("UNSTABLE_FLIP")]
    improved = [r["id"] for r in gated if r["verdict"] == "IMPROVED"]
    flipped = [r["id"] for r in rows if r["delta"] not in (None, 0)
               or r["verdict"] == "REGRESSION_ANSWER_LOST"]
    skipped = {r["id"]: r["verdict"] for r in rows
               if r["verdict"] in ("not_gated_tier", "excluded_low_specificity")}

    def tier_rates(facts):
        t = ((facts.get("by_tier") or {}).get(tier) or {})
        if not t:
            return (facts.get("recall@1"), facts.get("recall@3"), {})
        return t.get("recall_at_1"), t.get("recall_at_3"), t

    b1, b3, bt = tier_rates(fb)
    a1, a3, at = tier_rates(fa)
    # A run whose questions carry no tier at all (harness 1.0/1.1 question sets) cannot be split;
    # judge it on its overall rate and label the check so nobody reads it as a core-tier result.
    tier_fallback = not (fb.get("by_tier") or fa.get("by_tier")
                         or any(v["tier_explicit"] for v in fb["per_question"].values())
                         or any(v["tier_explicit"] for v in fa["per_question"].values()))

    # The recall@k the gate checks is recomputed from the GATED ROWS, so it covers exactly the
    # questions the verdict covers: low-specificity questions are dropped here too, otherwise the
    # aggregate could be dragged down (or propped up) by questions declared unjudgeable.
    def gated_rates(which):
        ranks, n = [], 0
        for r in rows:
            if r["tier"] != tier or r["id"] in low_spec or r["verdict"] == "unknown":
                continue
            n += 1
            ranks.append(r[f"rank_{which}"])
        if not n:
            return None, None, 0
        return (sum(1 for x in ranks if x == 1) / n,
                sum(1 for x in ranks if x is not None and x <= 3) / n, n)

    gb1, gb3, gn = gated_rates("before")
    ga1, ga3, _ = gated_rates("after")

    recall_key = None
    checks = {}
    if comparable:
        # Name the recall check after the tier it actually measured; when the run has no tier
        # data the fallback rate is the overall rate, so the key stays tier-agnostic and the
        # output records that the split was unavailable.
        recall_key = "recall@3_not_worse" if tier_fallback else f"{tier}_recall@3_not_worse"
        checks[recall_key] = (ga3 is not None and gb3 is not None and ga3 >= gb3)
        checks["no_answer_lost"] = not any(r["verdict"] == "REGRESSION_ANSWER_LOST" for r in rows)
        checks["no_decided_rank_regression"] = not any(r["verdict"] == "REGRESSION_RANK" for r in rows)
        checks["all_questions_answered"] = (not fb["failed_questions"] and not fa["failed_questions"]
                                            and fb["n_answered"] == fa["n_answered"])
        checks["dup_within_tolerance"] = (fa["dup_item_rate"] is not None and fb["dup_item_rate"] is not None
                                          and fa["dup_item_rate"] <= fb["dup_item_rate"] + dup_tolerance)
    all_ok = bool(checks) and all(checks.values())
    if not comparable:
        action = ("NOT COMPARABLE: one run has no recall measurement (see limitations) - "
                  "re-measure both sides with harness >= 1.1 before judging")
    elif all_ok:
        action = (f"CONTINUE: no {tier}-tier regression. Unstable flips are excluded by design; "
                  "re-check them on the next batch to confirm they are noise.")
    else:
        action = ("STOP: real regression - name the questions in `regressions`, then either "
                  "roll back exactly those document ids from the run manifest, or (if the "
                  "backfill is not implicated) treat it as live-turn drift and report it")

    out = {
        "tool": "compare_retrieval_runs", "version": VERSION,
        "min_margin": min_margin, "dup_tolerance": dup_tolerance,
        "gating_tier": tier, "low_specificity_excluded": sorted(low_spec),
        "harness_versions": {"before": hb, "after": ha},
        "before_file": before.get("_path"), "after_file": after.get("_path"),
        "tier_summary": {
            "before": {k: {"recall@1": v.get("recall_at_1"), "recall@3": v.get("recall_at_3"),
                           "recall@10": v.get("recall_at_10"), "n_scored": v.get("n_scored")}
                       for k, v in (fb.get("by_tier") or {}).items()},
            "after": {k: {"recall@1": v.get("recall_at_1"), "recall@3": v.get("recall_at_3"),
                          "recall@10": v.get("recall_at_10"), "n_scored": v.get("n_scored")}
                      for k, v in (fa.get("by_tier") or {}).items()},
            "gated": {"tier": tier, "recall@1_before": gb1, "recall@1_after": ga1,
                      "recall@3_before": gb3, "recall@3_after": ga3,
                      "n_scored": gn,
                      "tier_data_missing": tier_fallback,
                      "recall_check": recall_key,
                      "note": ("recomputed from the gated rows, so it covers exactly the questions "
                               "the verdict covers (low-specificity questions dropped)")},
        },
        "before": fb, "after": fa,
        "per_question": rows,
        "gated_ids": [r["id"] for r in gated],
        "skipped": skipped,
        "flipped": flipped,
        "regressions": real_regressions,
        "unstable_flips": unstable_flips,
        "improved": improved,
        "counts": counts,
        "checks": checks,
        "all_ok": all_ok,
        "action": action,
    }
    return out


def summarize(res, stream=sys.stdout):
    b, a = res["before"], res["after"]
    tier = res.get("gating_tier", "core")
    print(f"harness {res['harness_versions']['before']} -> {res['harness_versions']['after']}  "
          f"min_margin={res['min_margin']}  gating_tier={tier}")
    print(f"  {'metric':30s} {'before':>10s} {'after':>10s}")
    ts = res.get("tier_summary") or {}
    print("  per-question tier aggregates (raw, before judging exclusions):")
    for t in sorted(set(ts.get("before") or {}) | set(ts.get("after") or {})):
        gb = ((ts.get("before") or {}).get(t) or {})
        ga = ((ts.get("after") or {}).get(t) or {})
        tag = "  <- gated" if t == tier else "     (smoke)" if t == "environment" else "     (not scored)"
        print(f"  [{t}]{tag}")
        for k in ("recall@1", "recall@3", "recall@10"):
            print(f"  {'  ' + k:30s} {str(gb.get(k)):>10s} {str(ga.get(k)):>10s}")
        print(f"  {'  n_scored':30s} {str(gb.get('n_scored')):>10s} {str(ga.get('n_scored')):>10s}")
    print(f"  {'duplicate-item rate':30s} {str(b.get('dup_item_rate')):>10s} {str(a.get('dup_item_rate')):>10s}")
    g = ts.get("gated") or {}
    print(f"  GATED ({g.get('tier')} tier, {g.get('n_scored')} scored questions"
          f"{', tier data missing -> overall rate' if g.get('tier_data_missing') else ''}):")
    print(f"  {'  recall@1':30s} {str(g.get('recall@1_before')):>10s} {str(g.get('recall@1_after')):>10s}")
    print(f"  {'  recall@3 (the gate)':30s} {str(g.get('recall@3_before')):>10s} {str(g.get('recall@3_after')):>10s}")
    print(f"  {'answered / questions':30s} {str(b.get('n_answered')):>10s} {str(a.get('n_answered')):>10s}")
    print(f"  decided ({tier}-tier, judgeable) ids   before={b.get('decided_ids')}")
    print(f"  {'':30s} after ={a.get('decided_ids')}")
    print(f"  not gated (reported only): {json.dumps(res.get('skipped') or {})}")
    print(f"\n  per question (rank of the known-correct answer; the {tier} tier carries the verdict):")
    for r in res["per_question"]:
        mark = "*" if r["verdict"] not in ("not_gated_tier", "excluded_low_specificity") else " "
        print(f"   {mark}{r['id']:26s} [{r['tier']:11s}] {str(r['rank_before']):>5s} -> {str(r['rank_after']):>5s}"
              f"  gap={str(r['answer_gap_after']):>8s} {r['class_after']:>9s}  {r['verdict']}")
        if r["verdict"].startswith(("REGRESSION", "UNSTABLE_FLIP")):
            print(f"        {r['note']}")
    if res["improved"]:
        print(f"\n  improved: {res['improved']}")
    if res["flipped"]:
        print(f"  flipped:  {res['flipped']}")
    else:
        print("  flipped:  none (every answer kept its rank)")
    print(f"\n  checks: {json.dumps(res['checks'])}")
    print(f"  VERDICT: {'PASS' if res['all_ok'] else 'FAIL/n-a'} - {res['action']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Per-question before/after diff + the backfill gate.")
    ap.add_argument("--before", required=True)
    ap.add_argument("--after", required=True)
    ap.add_argument("--min-margin", type=float, default=0.05,
                    help="answer-holding margin below which a rank change is a near-tie (0.05)")
    ap.add_argument("--dup-tolerance", type=float, default=DEFAULT_DUP_TOLERANCE,
                    help="allowed rise in the duplicate-item rate (default 0.02)")
    ap.add_argument("--tier", default="core", choices=("core", "environment"),
                    help="which question tier carries the verdict (default: core - facts and rules "
                         "that exist only because they were decided or observed; environment facts "
                         "are readable by a live tool and are reported as smoke only)")
    ap.add_argument("--specificity", help="validate_golden_set.py report; its low-specificity "
                                          "questions (expected token too common to detect a "
                                          "regression) are excluded from the verdict")
    ap.add_argument("--out", help="write the comparison JSON here")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    low_spec = None
    if args.specificity:
        spec = load(args.specificity)
        low_spec = spec.get("low_specificity_ids") or []
        if not low_spec:
            print(f"[warn] {args.specificity} lists no low-specificity questions; "
                  "the verdict may be diluted by very common expected tokens", file=sys.stderr)

    before, after = load(args.before), load(args.after)
    before["_path"], after["_path"] = os.path.abspath(args.before), os.path.abspath(args.after)
    res = compare(before, after, args.min_margin, args.dup_tolerance, args.tier, low_spec)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=1)
        print(f"wrote {args.out}")
    if not args.quiet:
        summarize(res)
    return 0 if res["all_ok"] else (2 if res["before"]["limitations"] or res["after"]["limitations"] else 3)


if __name__ == "__main__":
    sys.exit(main())
