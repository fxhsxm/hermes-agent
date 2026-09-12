#!/usr/bin/env python3
"""validate_golden_set.py -- coverage-check a golden question set against a bank (read-only).

A question set for LONG-TERM MEMORY must only contain questions the memory could answer. If the
expected value was never retained, the question measures coverage, not retrieval, and a miss on
it says nothing about the memory system. This tool separates the two BEFORE the set is used as a
benchmark:

  * for every question it folds the bank's memory texts and counts the items containing any
    `expect` token (the same any-of/folded match the harness uses to score a hit);
  * `present=0` means a guaranteed miss -> the question must be dropped or explicitly labelled a
    coverage probe;
  * it also reports how deep the answer sits if you want a rough retrieval expectation.

It also separates the STORES. A question whose answer lives in the injected MEMORY.md/USER.md
store (always in context, never retrieved from the bank) cannot measure bank retrieval, so
`--builtin-files` verifies those questions against those files instead. Without this split a
generic expected token ("STOP", "backup") matches unrelated bank items and the question looks
covered while measuring nothing.

Read-only: it walks `/memories/list` and never writes to the bank.

USAGE
  python validate_golden_set.py --bank fwh-main --questions retrieval_questions.json \
      --out golden_set_coverage.json [--require-min-items 1]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata

FOLD_PUNCT = True


def fold(text: str) -> str:
    """Same folding the harness uses: NFKC, casefold, punctuation/symbol/space removed."""
    t = unicodedata.normalize("NFKC", text or "").casefold()
    if FOLD_PUNCT:
        t = "".join(" " if unicodedata.category(ch)[0] in ("P", "S", "Z") else ch for ch in t)
    return "".join(t.split())


def walk_items(api: str, bank: str, page: int = 500):
    import httpx
    client = httpx.Client(timeout=180)
    offset, items = 0, []
    while True:
        d = client.get(f"{api}/v1/default/banks/{bank}/memories/list",
                       params={"limit": page, "offset": offset}).json()
        batch = d.get("items") or []
        items.extend(batch)
        if not batch or (d.get("total") is not None and len(items) >= d["total"]):
            break
        offset += len(batch)
    return items


def coverage_rows(questions, folded_texts, require_min_items: int = 1,
                  low_spec_max_items: int = 20):
    """Per-question coverage against already-folded bank texts.

    Pure so it can be tested without a bank: `folded_texts` is the bank's memory texts after
    fold(), and the match is the same any-of substring test the harness uses for a hit.
    Returns (rows, uncovered_ids, no_ground_truth_ids).

    Also measures SPECIFICITY, because coverage alone is not enough: the harness scores a hit on
    ANY expected token, so a question whose most common token appears in hundreds of items is
    trivially satisfied by almost any response and cannot detect a regression. `max_token_items`
    (the count for the most common expected token) drives that false-hit risk, so it is the
    number that classifies the question, and `low_spec_max_items` is the cut-off.
    """
    rows, uncovered, no_ground_truth = [], [], []
    for q in questions:
        # Accept both keys, exactly as the harness does: a validator that reads only one of
        # them silently reports "no ground truth" for a fully specified question set.
        if not q.get("expected"):
            q["expected"] = q.get("expect") or []
        tier = q.get("tier")
        if tier and tier not in ("core", "environment"):
            # builtin answers live in the injected files (checked by builtin_rows), liveness has
            # no expectation, gap is retained nowhere. Judging any of them against the bank would
            # report a fake coverage gap.
            rows.append({"id": q.get("id"), "category": q.get("category"), "tier": tier,
                         "expect": list(q.get("expect") or []), "containing_items": None,
                         "covered": None,
                         "note": (f"tier={tier}: not a bank question, so bank coverage is not "
                                  "applicable here")})
            continue
        exp = [e for e in (q.get("expected") or []) if fold(e)]
        if not exp:
            no_ground_truth.append(q.get("id"))
            rows.append({"id": q.get("id"), "category": q.get("category"),
                         "tier": q.get("tier"),
                         "expect": [], "containing_items": None,
                         "covered": None, "note": "no expect list: liveness/read-only probe"})
            continue
        folded_exp = [fold(e) for e in exp]
        hits = [i for i, f in enumerate(folded_texts) if any(e in f for e in folded_exp)]
        token_items = {e: sum(1 for f in folded_texts if fe in f)
                       for e, fe in zip(exp, folded_exp)}
        max_token_items = max(token_items.values()) if token_items else 0
        low_spec = max_token_items > low_spec_max_items
        covered_ok = len(hits) >= require_min_items
        if not covered_ok:
            uncovered.append(q.get("id"))
        rows.append({
            "id": q.get("id"), "category": q.get("category"), "tier": q.get("tier"),
            "expect": exp, "token_items": token_items,
            "max_token_items": max_token_items, "low_specificity": low_spec,
            "containing_items": len(hits), "first_item_index": (hits[0] + 1) if hits else None,
            "covered": covered_ok,
            "note": ("COVERAGE GAP: the expected value is nowhere in the bank, so this question "
                     "cannot measure retrieval" if not covered_ok else
                     f"LOW SPECIFICITY: the most common expected token appears in "
                     f"{max_token_items} items, so nearly any response scores a hit - exclude it "
                     f"from the verdict (keep it as coverage evidence)" if low_spec else
                     "answer is retained somewhere in the bank, and the expected token is "
                     "discriminating enough to detect a rank change"),
        })
    return rows, uncovered, no_ground_truth


def builtin_rows(questions, folded_files):
    """Check `builtin`-tier questions against the injected memory files (pure, testable).

    `folded_files` maps a label ("USER.md") to that file's folded text. Returns
    (rows, uncovered_ids): a builtin question is covered when at least one of its expected
    phrases appears in at least one file - that is the store that is supposed to hold it.
    """
    rows, uncovered = [], []
    for q in questions:
        if (q.get("tier") or "") != "builtin":
            continue
        exp = [e for e in (q.get("expect") or q.get("expected") or []) if fold(e)]
        found = {e: [k for k, t in folded_files.items() if fold(e) in t] for e in exp}
        ok = any(v for v in found.values())
        if not ok:
            uncovered.append(q.get("id"))
        rows.append({
            "id": q.get("id"), "tier": "builtin", "expect": exp, "found_in": found,
            "covered": ok,
            "note": ("answer is in the injected memory store (" +
                     ", ".join(sorted({k for v in found.values() for k in v})) + ")"
                     if ok else
                     "NOT FOUND in the injected memory files: the answer is retained in neither "
                     "store, so this question measures nothing - fix the memory or drop it"),
        })
    return rows, uncovered


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Coverage-check a golden question set (read-only).")
    ap.add_argument("--bank", default="fwh-main")
    ap.add_argument("--api", default="http://127.0.0.1:8888")
    ap.add_argument("--questions", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--require-min-items", type=int, default=1,
                    help="how many containing items count as 'covered' (default 1)")
    ap.add_argument("--keep-expect", action="store_true",
                    help="keep the expected tokens in the report (default: redacted, because the "
                         "report is published and a token IS the answer)")
    ap.add_argument("--builtin-files", nargs="*", default=None,
                    help="files of the injected memory store (default: the active profile's "
                         "MEMORY.md and USER.md); builtin-tier questions are verified against "
                         "them because they are never retrieved from the bank")
    ap.add_argument("--low-spec-max-items", type=int, default=20,
                    help="a question whose most common expected token appears in MORE than this "
                         "many items is low-specificity: nearly any response scores a hit, so it "
                         "is excluded from the gate verdict (default 20)")
    args = ap.parse_args(argv)

    with open(args.questions, encoding="utf-8") as fh:
        doc = json.load(fh)
    questions = doc["questions"] if isinstance(doc, dict) else doc
    for q in questions:
        if isinstance(q, dict) and not q.get("expected"):
            q["expected"] = q.get("expect") or []

    files = args.builtin_files
    if files is None:
        home = os.environ.get("HERMES_HOME") or os.path.join(
            os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "hermes")
        files = [os.path.join(home, "memories", n) for n in ("MEMORY.md", "USER.md")]
    folded_files = {}
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                folded_files[os.path.basename(f)] = fold(fh.read())
        except OSError as exc:
            print(f"[warn] builtin file unreadable: {f} ({exc})", file=sys.stderr)

    items = walk_items(args.api, args.bank)
    folded = [fold(it.get("text") or "") for it in items]
    print(f"bank {args.bank}: {len(items)} memory items scanned")

    rows, uncovered, no_ground_truth = coverage_rows(questions, folded, args.require_min_items,
                                                     args.low_spec_max_items)
    covered = sum(1 for r in rows if r["covered"])
    low_spec_ids = [r["id"] for r in rows if r.get("low_specificity")]
    if folded_files:
        b_rows, b_uncovered = builtin_rows(questions, folded_files)
    else:
        b_rows, b_uncovered = [], [q.get("id") for q in questions
                                   if (q.get("tier") or "") == "builtin"]
    by_tier: dict = {}
    for r in rows:
        t = by_tier.setdefault(r.get("tier") or "core", {"n": 0, "covered": 0, "low_specificity": 0})
        t["n"] += 1
        t["covered"] += 1 if r["covered"] else 0
        t["low_specificity"] += 1 if r.get("low_specificity") else 0
    for r in b_rows:
        b = by_tier.setdefault("builtin", {"n": 0, "covered": 0, "low_specificity": 0})
        b["n"] += 1
        b["covered"] += 1 if r["covered"] else 0

    out = {
        "bank": args.bank, "items_scanned": len(items),
        "questions": len(questions),
        "n_with_expect": len(questions) - len(no_ground_truth),
        "n_covered": covered, "n_uncovered": len(uncovered),
        "uncovered_ids": uncovered, "no_ground_truth_ids": no_ground_truth,
        "core_uncovered_ids": [r["id"] for r in rows
                               if r.get("covered") is False
                               and (r.get("tier") or "core") == "core"],
        "builtin_files": sorted(folded_files), "builtin_uncovered_ids": b_uncovered,
        "builtin_detail": b_rows,
        "gap_ids": [q.get("id") for q in questions if (q.get("tier") or "") == "gap"],
        "require_min_items": args.require_min_items,
        "low_spec_max_items": args.low_spec_max_items,
        "low_specificity_ids": low_spec_ids,
        "by_tier": by_tier,
        "method": ("every memory item's text is folded (NFKC, casefold, punctuation/symbol/space "
                   "removed) and matched any-of against the question's expect tokens - the same "
                   "match the harness uses for a hit; a question with zero containing items is a "
                   "guaranteed miss and measures coverage, not retrieval. Specificity is measured "
                   "per expected token; because the match is any-of, the MOST common token decides "
                   "the false-hit risk, so `max_token_items` classifies the question and "
                   "`low_specificity_ids` must be excluded from the gate verdict."),
        "detail": rows,
    }
    # Snapshot what the console shows before redaction: the console is local, the report is
    # published, and mutating the shared dicts would blank the tokens in both.
    shown = [dict(r) for r in rows]
    b_shown = [dict(r) for r in b_rows]
    if not args.keep_expect:
        # The expected tokens and their per-token counts are memory-derived: a token IS the
        # answer. Keep the counts and ids (the coverage evidence) and drop the strings, because
        # this report gets committed to a public repository.
        for r in out["detail"] + out["builtin_detail"]:
            if r.get("expect"):
                r["expect"] = [f"[redacted {len(r['expect'])} token(s)]"]
            r.pop("token_items", None)
        out["expect_redacted"] = True
        out["privacy_note"] = ("expected tokens redacted; counts (containing_items, "
                              "max_token_items) are kept as the coverage evidence. "
                              "Use --keep-expect for a local run.")
    text = json.dumps(out, ensure_ascii=False, indent=1)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {args.out}")
    for r in shown:
        flag = "ok " if r["covered"] else ("-- " if r["covered"] is None else "GAP")
        spec = "" if r.get("low_specificity") is None else (
            "  LOW-SPEC" if r["low_specificity"] else "")
        print(f"  {flag} {str(r['id']):32s} {str(r.get('tier') or 'core'):11s} "
              f"items={r['containing_items']} max_token_items={r.get('max_token_items')}"
              f"{spec}{('  ' + json.dumps(r['expect'], ensure_ascii=False)) if r['expect'] else ''}")
    print(f"\ncovered {covered}/{out['n_with_expect']} (no ground truth: {len(no_ground_truth)})")
    print(f"tiers: {json.dumps(by_tier)}")
    print(f"low specificity ({len(low_spec_ids)}, excluded from the gate verdict): {low_spec_ids}")
    for r in b_shown:
        flag = "ok " if r["covered"] else "GAP"
        print(f"  {flag} {str(r['id']):32s} builtin      "
              f"{json.dumps(r['found_in'], ensure_ascii=False)}")
    print(f"builtin store {sum(1 for r in b_rows if r['covered'])}/{len(b_rows)} "
          f"verified in {sorted(folded_files)}")
    gap_ids = [q.get("id") for q in questions if (q.get("tier") or "") == "gap"]
    if gap_ids:
        print(f"NOT RETAINED in either store ({len(gap_ids)}, not scored - memory gap to fix): "
              f"{gap_ids}")
    core_uncovered = [r["id"] for r in rows
                      if r.get("covered") is False and (r.get("tier") or "core") == "core"]
    # Only `core` (the benchmark) and `builtin` (checked against its own store) can fail this
    # run. An environment question is answerable by a live tool, not by the bank, so a bank miss
    # there is information, not a defect.
    if uncovered and not core_uncovered:
        print(f"note: {len(uncovered)} environment question(s) not found in the bank "
              f"({uncovered}); they are smoke, and their store is the tool, not the bank")
    return 0 if not core_uncovered and not b_uncovered else 1


if __name__ == "__main__":
    sys.exit(main())
