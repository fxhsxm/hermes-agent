#!/usr/bin/env python3
"""REPEATABLE retrieval-quality measurement harness for a Hindsight memory bank.

Measures two failure modes that matter for LLM context injection via the Hermes
plugin: DUPLICATION inside one recall response, and FRESHNESS/STALENESS of the
ranking (a superseded fact ranked above its replacement).

Read-only against the target bank. It only ever issues POST .../memories/recall
and (when asked) POST .../reflect. It never retains, patches or deletes.

USAGE
  python measure_retrieval_quality.py \
      --bank fwh-main \
      --questions questions_before.json \
      --out retrieval_quality_before.json

  python measure_retrieval_quality.py \
      --bank fwh-audit-sandbox \
      --questions questions_sandbox.json \
      --out retrieval_quality_sandbox.json --reflect

QUESTION-SET FORMAT (JSON)
  {"bank": "fwh-main",
   "questions": [
     {"id": "Q01", "lang": "zh-Hant", "query": "...",
      "expect": ["substring", "substring"],      # any-of match -> recall@k.
      "expected": [...]                          # accepted alias of "expect"; either key works
      "reflect": false,                          # ask /reflect too
      "stale_markers": ["OLD_TOKEN"],            # optional exact-ordering probe
      "fresh_markers": ["NEW_TOKEN"]}
   ]}

WHAT IS EMITTED (see "methodology" in the output JSON for the honest caveats)
  (a) duplication  - normalized (NFKC + casefold + punctuation/symbol/space
      folded) identical pairs, >=0.9-similar pairs, and how many of those pairs
      are observation-versus-raw-twin (one side type=observation, other side
      type in {world, experience}).
  (b) recall@1/3/10 over questions carrying an "expect"/"expected" list. THIS IS
      THE PRIMARY PASS/FAIL CRITERION: the rank of the known-correct answer.
  (c) ranking decision - per question, the top1-top2 final-score margin decides
      whether the ORDER of the ranking is meaningful at all ("decided",
      margin >= --min-margin, default 0.05) or noise ("unstable", a near-tie).
      Unstable questions are reported but never gate.
  (d) staleness PROXY (top-1 superseded by a newer sibling) - INFORMATIONAL
      ONLY. Measured on the live bank it fired on 8/10 questions before any
      backfill ran and is decided by <0.005 score gaps, so it is not a gate.
  (e) latency + reranker engagement per question.
  (f) backfill attribution - an ATTRIBUTION PROXY (not a counterfactual): the same responses
      with backfill-sourced items removed, i.e. "would the answer survive in THIS response
      without the added documents?". It cannot show what retrieval would have returned.
  (g) recall PER TIER, and the headline recall@k is the CORE tier. `core` = bank-retrievable
      long-term memory with a selective expected token (the only tier the backfill gate uses);
      `environment` = facts a live tool can read back (port, version, model, path, cadence), smoke
      only; `builtin` = the answer lives in the injected MEMORY.md/USER.md store, which is always
      in context and is NOT retrieved from the bank - verified against those files instead of
      being scored as bank recall; `gap` = retained in neither store (a finding); `liveness` = no
      expectation. `recall_bank_scored` keeps the pre-tier core+environment rate for continuity.

  plus stale/fresh marker ordering when the question declares both.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import statistics
import sys
import time
import unicodedata
from datetime import datetime, timezone

import httpx

HARNESS_VERSION = "1.3"

# Item text is MEMORY CONTENT. The numbers (ranks, margins, ids, counts) are what the gate and
# the reports need; the text is not, and this repo is public. So text fields are REDACTED by
# default and only kept with --keep-text (local debugging). This matters: the first version of
# this harness wrote item text into its outputs, and four of those outputs were committed.
REDACT_KEYS = {"text", "a_text", "b_text", "sibling_text", "window"}

# The QUESTION SET is memory-derived too: a question plus its expected token is a structured
# statement about the user's environment and preferences, even with the item text removed. So the
# question text and the expected tokens are redacted as well, and only the ids, tiers, ranks,
# margins and counts the gate needs are published. `expected` is replaced with a non-empty
# placeholder on purpose: consumers use truthiness to tell a scored question from a liveness
# probe, and an empty list would silently unscore every question.
QUESTION_KEYS = {"query"}


def redact_text(obj):
    """Replace memory-derived text fields with a length marker, in place. Returns the count."""
    n = 0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in REDACT_KEYS and isinstance(v, str) and v:
                obj[k] = f"[redacted {len(v)} chars]"
                n += 1
            elif isinstance(v, (dict, list)):
                n += redact_text(v)
    elif isinstance(obj, list):
        for v in obj:
            n += redact_text(v)
    return n

def redact_questions(obj):
    """Redact the question text and the expected tokens (memory-derived), in place."""
    n = 0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in QUESTION_KEYS and isinstance(v, str) and v:
                obj[k] = f"[redacted question, {len(v)} chars]"
                n += 1
            elif k == "expected" and isinstance(v, list) and v:
                obj[k] = [f"[redacted {len(v)} token(s)]"]
                n += 1
            elif k == "matched_expectation" and isinstance(v, str) and v:
                # the token that produced the hit IS the answer: redact it as well
                obj[k] = "[redacted token]"
                n += 1
            elif isinstance(v, (dict, list)):
                n += redact_questions(v)
    elif isinstance(obj, list):
        for v in obj:
            n += redact_questions(v)
    return n


# A question's ranking is only judged when the top-1 leads the top-2 by more than
# this margin. Below it the order is decided by score noise (observed: 0.001-0.005
# gaps on the live bank flipping between runs), so the question is reported as
# "unstable" and excluded from pass/fail instead of producing a false regression.
MIN_DECIDED_MARGIN = 0.05

# ---------------------------------------------------------------- HTTP layer

RETRY_DELAYS = [2, 3, 5, 8, 12, 15, 20, 25, 30, 30]
RETRYABLE = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.PoolTimeout,
)


class Call:
    """A single API call with retry/backoff; records retries + latency."""

    def __init__(self, api, path, payload, timeout=240.0, method="POST"):
        self.api, self.path, self.payload = api.rstrip("/"), path, payload
        self.timeout, self.method = timeout, method
        self.retries = 0
        self.error = None
        self.status = None
        self.body = None
        self.latency_s = None
        t0 = time.time()
        try:
            self._do()
        finally:
            self.latency_s = round(time.time() - t0, 4)

    def _do(self):
        url = f"{self.api}{self.path}"
        last = None
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                if self.method == "GET":
                    r = httpx.get(url, params=self.payload, timeout=self.timeout)
                else:
                    r = httpx.post(url, json=self.payload, timeout=self.timeout)
                if r.status_code >= 500:
                    raise httpx.RemoteProtocolError(f"HTTP {r.status_code}")
                self.status = r.status_code
                try:
                    self.body = r.json()
                except Exception:
                    self.body = {"_raw": r.text[:2000]}
                if r.status_code >= 400:
                    self.error = f"HTTP {r.status_code}: {r.text[:300]}"
                return
            except RETRYABLE as exc:  # transient (incl. API restart mid-run)
                last = exc
                if attempt >= len(RETRY_DELAYS):
                    break
                self.retries += 1
                time.sleep(RETRY_DELAYS[attempt])
            except Exception as exc:  # non-retryable
                last = exc
                break
        self.error = f"{type(last).__name__}: {str(last)[:300]}"

    @property
    def ok(self):
        return self.status == 200 and self.error is None


def recall(api, bank, query, timeout=240.0):
    return Call(api, f"/v1/default/banks/{bank}/memories/recall", {"query": query}, timeout)


def reflect(api, bank, query, timeout=600.0):
    return Call(api, f"/v1/default/banks/{bank}/reflect", {"query": query}, timeout)


def get_json(api, path, params=None, timeout=90.0):
    return Call(api, path, params or {}, timeout=timeout, method="GET")


# ------------------------------------------------------- text normalization

_ANNOT_RE = re.compile(
    r"^(when|involving|because|demonstrates|context|source|note|reason|evidence"
    r"|因為|當|涉及|來源|時間|原因)\b\s*[:：]?",
    re.I,
)
_WS_RE = re.compile(r"\s+")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff]+")
_LATIN_RE = re.compile(r"[a-z0-9][a-z0-9._\-/+]*")
_VALUE_RE = re.compile(r"^[a-z0-9]*[0-9][a-z0-9._\-/+]*$")
_SUPERSEDE_RE = re.compile(
    r"(改為|改成|已改|已經改|更正|修正|更新為|最新|作廢|廢止|取代|現時|"
    r"now is|now uses|changed to|has been changed|updated to|replaced|supersed"
    r"|no longer|instead of|deprecat|previous value|old value)",
    re.I,
)
_STALE_QUALIFIER_RE = re.compile(
    r"(改為|改成|已改|已經改|更正|修正|作廢|廢止|取代|舊|過時|不再|以前|先前|"
    r"was |previous|old |former|outdated|deprecated|superseded|no longer|replaced)",
    re.I,
)

MIN_NORM_LEN = 12  # short normalized strings are not counted as duplicates


def strip_annotations(text: str) -> str:
    """Drop the ' | When: ... | Involving: ...' annotation segments that raw
    world/experience facts carry but their derived observation does not."""
    if not text:
        return ""
    parts = re.split(r"\s*\|\s*", text)
    keep = []
    for p in parts:
        p = p.strip()
        if not p or _ANNOT_RE.match(p):
            continue
        keep.append(p)
    return " | ".join(keep) if keep else text


def normalize(text: str, fold_punct: bool = True) -> str:
    """NFKC -> casefold -> punctuation/symbol/space folding -> whitespace strip."""
    t = unicodedata.normalize("NFKC", text or "").casefold()
    if fold_punct:
        t = "".join(
            " " if unicodedata.category(ch)[0] in ("P", "S", "Z") else ch for ch in t
        )
    return _WS_RE.sub("", t)


def norm_pair(a_text: str, b_text: str):
    """Return (layer, similarity) for the best matching normalization layer.

    layer 'strict'  = full text, punctuation folded (the plain request)
    layer 'core'    = annotation-stripped text, punctuation folded
                    (catches raw-fact vs derived-observation twins)
    """
    best = ("strict", 0.0)
    for layer, transform in (("strict", lambda s: s), ("core", strip_annotations)):
        na, nb = normalize(transform(a_text)), normalize(transform(b_text))
        if len(na) < MIN_NORM_LEN or len(nb) < MIN_NORM_LEN:
            continue
        if na == nb:
            return layer, 1.0
        sim = _ratio(na, nb)
        if sim > best[1]:
            best = (layer, sim)
    return best


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    # cheap upper-bound prefilter: SequenceMatcher.ratio() <= 2*min/max
    if 2.0 * min(len(a), len(b)) / max(len(a), len(b)) < 0.9:
        return 0.0
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if sm.quick_ratio() < 0.9:
        return 0.0
    return sm.ratio()


def tokens(text: str) -> set:
    """Language-agnostic content tokens: Latin words (len>=3 or containing a
    digit) plus CJK character bigrams."""
    core = normalize(strip_annotations(text), fold_punct=True)
    out = set()
    for w in _LATIN_RE.findall(core):
        if len(w) >= 3 or any(c.isdigit() for c in w):
            out.add(w)
    for run in _CJK_RUN_RE.findall(core):
        if len(run) == 1:
            out.add(run)
        for i in range(len(run) - 1):
            out.add(run[i : i + 2])
    return out


def value_tokens(toks: set) -> set:
    """Tokens that carry a value: numbers, versions, codes (WIDGET-ALPHA-4417)."""
    return {t for t in toks if _VALUE_RE.match(t)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def parse_ts(ts: str):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


# --------------------------------------------------------------- heuristics

def contradiction_proxy(top, newer_items):
    """PROXY for 'the top-1 raw fact has been superseded by a newer sibling'.

    Not a verified contradiction. Fires when, in the same response, a newer
    (mentioned_at) sibling shares entities or >=35% of content tokens with the
    top-1 item AND either carries a value-bearing token the top-1 lacks or uses
    a supersede verb. Shared-entity newer siblings can also be additive; the
    rate therefore over-counts true contradictions.
    """
    t_toks = tokens(top["text"])
    t_vals = value_tokens(t_toks)
    t_ents = {str(e).casefold() for e in (top.get("entities") or [])}
    strict, loose = [], []
    for idx, s in newer_items:
        s_toks = tokens(s["text"])
        if not s_toks:
            continue
        s_ents = {str(e).casefold() for e in (s.get("entities") or [])}
        shared_ents = sorted(t_ents & s_ents)
        overlap = jaccard(t_toks, s_toks)
        new_vals = sorted(value_tokens(s_toks) - t_vals)
        sup = bool(_SUPERSEDE_RE.search(strip_annotations(s["text"])))
        rec = {
            "sibling_rank": idx + 1,
            "sibling_id": s.get("id"),
            "sibling_type": s.get("type"),
            "sibling_mentioned_at": s.get("mentioned_at"),
            "shared_entities": shared_ents,
            "token_overlap": round(overlap, 3),
            "differing_value_tokens": new_vals[:8],
            "supersede_verb": sup,
            "sibling_text": (s.get("text") or "")[:220],
        }
        if (shared_ents or overlap >= 0.35) and (new_vals or sup):
            strict.append(rec)
        if overlap >= 0.5 and new_vals:
            loose.append(rec)
    best = max(strict, key=lambda r: r["token_overlap"]) if strict else None
    return {"triggered_strict": bool(strict),
            "triggered_loose": bool(loose),
            "n_newer_siblings_flagged": len(strict),
            "evidence": best or (loose[0] if loose else None)}


def rank_of_markers(items, markers):
    """Best (lowest) 1-based rank of any marker, matched on folded text."""
    hits = []
    if not markers:
        return None, []
    for i, it in enumerate(items):
        txt = normalize(it.get("text", ""))
        for m in markers:
            if normalize(m) and normalize(m) in txt:
                hits.append({"rank": i + 1, "marker": m, "id": it.get("id"),
                             "type": it.get("type"),
                             "mentioned_at": it.get("mentioned_at")})
    hits.sort(key=lambda h: h["rank"])
    return (hits[0]["rank"] if hits else None), hits


def marker_context(text, markers, window=140):
    """For each stale marker occurrence, capture the surrounding window so a
    human can judge whether the answer asserted it as current or as superseded."""
    out = []
    n = normalize(text)
    for m in markers or []:
        nm = normalize(m)
        start = 0
        while nm and (pos := n.find(nm, start)) != -1:
            lo, hi = max(0, pos - window), min(len(n), pos + window)
            ctx = n[lo:hi]
            out.append({"marker": m, "qualified": bool(_STALE_QUALIFIER_RE.search(ctx)),
                        "window": ctx})
            start = pos + len(nm)
    return out


# --------------------------------------------------------------- per-question

def analyse_items(items):
    """Duplication metrics for one recall response."""
    pairs, flagged = [], set()
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            layer, sim = norm_pair(a.get("text", ""), b.get("text", ""))
            if sim < 0.9:
                continue
            types = {a.get("type"), b.get("type")}
            is_twin = ("observation" in types) and bool(types & {"world", "experience"})
            pairs.append({
                "i": i + 1, "j": j + 1,
                "a_id": a.get("id"), "a_type": a.get("type"),
                "b_id": b.get("id"), "b_type": b.get("type"),
                "similarity": round(sim, 4),
                "kind": "identical" if sim == 1.0 else "fuzzy>=0.9",
                "match_layer": layer,
                "observation_vs_raw_twin": is_twin,
                "a_text": (a.get("text") or "")[:160],
                "b_text": (b.get("text") or "")[:160],
            })
            flagged.add(i)
            flagged.add(j)
    n = len(items)
    return {
        "duplicate_pairs": len(pairs),
        "identical_pairs": sum(1 for p in pairs if p["kind"] == "identical"),
        "fuzzy_pairs": sum(1 for p in pairs if p["kind"] != "identical"),
        "observation_vs_raw_twin_pairs": sum(1 for p in pairs if p["observation_vs_raw_twin"]),
        "items_in_duplicate_pair": len(flagged),
        "duplicate_item_rate": round(len(flagged) / n, 4) if n else None,
        "pairs": pairs,
    }


def reranker_stats(items, top_n=3):
    sc = [(it.get("scores") or {}) for it in items]
    re_vals = [s.get("reranker") for s in sc if s.get("reranker") is not None]
    sem = [s.get("semantic") for s in sc if s.get("semantic") is not None]
    fin = [s.get("final") for s in sc if s.get("final") is not None]
    def argmax(key):
        best, bi = None, None
        for i, it in enumerate(items):
            v = (it.get("scores") or {}).get(key)
            if v is None:
                continue
            if best is None or v > best:
                best, bi = v, i
        return bi
    t1_rer = argmax("reranker")
    t1_sem = argmax("semantic")
    return {
        "n_items": len(items),
        "n_with_scores": sum(1 for s in sc if s),
        "n_with_reranker_score": len(re_vals),
        "reranker_engaged": bool(re_vals) and any(v for v in re_vals),
        "mean_reranker": round(statistics.fmean(re_vals), 4) if re_vals else None,
        "mean_semantic": round(statistics.fmean(sem), 4) if sem else None,
        "mean_final": round(statistics.fmean(fin), 4) if fin else None,
        "top1_id_final": items[0].get("id") if items else None,
        "top1_id_by_reranker": items[t1_rer].get("id") if t1_rer is not None else None,
        "top1_id_by_semantic": items[t1_sem].get("id") if t1_sem is not None else None,
        "reranker_changed_top1_vs_semantic": (
            t1_rer is not None and t1_sem is not None and t1_rer != t1_sem
        ),
        "top3_final": [round((it.get("scores") or {}).get("final") or 0, 4) for it in items[:top_n]],
    }


def ranking_decision(items, expect_rank=None, min_margin=MIN_DECIDED_MARGIN):
    """Is this question's ORDER meaningful, or a near-tie decided by score noise?

    'decided'  = top-1 leads top-2 by >= min_margin, so the order can be judged
    'unstable' = a smaller gap: a near-tie whose order any addition can flip
    'unscored' = fewer than two items carry a final score (not judgeable)

    expect_answer_gap applies the same test to the position the KNOWN-CORRECT
    answer occupies (its final minus the next item's), because the answer can sit
    inside a decided top-1 while ranks 2/3/4 are a tie, and the reverse.
    """
    scored = []
    for i, it in enumerate(items):
        f = (it.get("scores") or {}).get("final")
        if f is not None:
            scored.append((i + 1, round(float(f), 4)))
    top1 = scored[0][1] if scored else None
    top2 = scored[1][1] if len(scored) > 1 else None
    margin = round(top1 - top2, 4) if top2 is not None else None
    if margin is None:
        cls = "unscored"
    elif margin < min_margin:
        cls = "unstable"
    else:
        cls = "decided"

    gap = None
    if expect_rank and 1 <= expect_rank <= len(items):
        f_here = (items[expect_rank - 1].get("scores") or {}).get("final")
        f_next = ((items[expect_rank].get("scores") or {}).get("final")
                  if expect_rank < len(items) else None)
        if f_here is not None and f_next is not None:
            gap = round(float(f_here) - float(f_next), 4)
    return {
        "min_margin": min_margin,
        "class": cls,
        "top1_final": top1,
        "top2_final": top2,
        "margin_top1_top2": margin,
        "top1_id": items[0].get("id") if items else None,
        "top2_id": items[scored[1][0] - 1].get("id") if len(scored) > 1 else None,
        "expect_rank": expect_rank,
        "expect_answer_gap": gap,
        "expect_answer_class": None if gap is None else ("decided" if gap >= min_margin else "unstable"),
    }


def match_expected(items, exp):
    """First rank whose text matches any expected substring (folded, any-of)."""
    if not exp:
        return None, None
    for i, it in enumerate(items):
        text = it.get("text") or ""
        flat = normalize(text) + "|" + text.casefold()
        hit = next((e for e in exp if normalize(e) and normalize(e) in flat
                    or e.casefold() in text.casefold()), None)
        if hit:
            return i + 1, hit
    return None, None


def is_backfill_item(item) -> bool:
    """True when a recall result came from a backfill document.

    Backfilled items are self-identifying in the response: metadata.source ==
    "backfill" and the tag "kind:backfill". This is what lets the harness measure
    the backfill's effect WITHOUT deleting anything (see backfill_effect).
    """
    md = item.get("metadata") or {}
    if isinstance(md, dict) and str(md.get("source", "")).lower() == "backfill":
        return True
    return any(str(t).lower() == "kind:backfill" for t in (item.get("tags") or []))


def backfill_effect(items, exp, expect_rank):
    """Rank of the expected answer with every backfill-sourced item removed.

    This is an ATTRIBUTION PROXY, not a counterfactual. Removing items from an ALREADY
    RETURNED list cannot show what retrieval would have returned without them: the top-k /
    token budget, reranking and consolidation window would all differ, and a different list
    would have been produced. What it does show is whether the answer would still be present
    in THIS response if the added documents had not been inserted - which is what attribution
    needs, and is measurable without deleting anything.
    """
    subset = [it for it in items if not is_backfill_item(it)]
    nb_rank, _nb_matched = match_expected(subset, exp)
    ranks = [i + 1 for i, it in enumerate(items) if is_backfill_item(it)]
    return {
        "n_items": len(items),
        "n_backfilled_items": len(ranks),
        "n_non_backfill_items": len(subset),
        "backfilled_ranks": ranks[:20],
        "top1_is_backfilled": bool(items) and is_backfill_item(items[0]),
        "expected_rank": expect_rank,
        "expected_rank_without_backfill": nb_rank,
        "answer_rank_changed_by_backfill": (None if not exp else expect_rank != nb_rank),
        "hit@1_without_backfill": (None if not exp else nb_rank == 1),
        "hit@3_without_backfill": (None if not exp else bool(nb_rank and nb_rank <= 3)),
        "hit@10_without_backfill": (None if not exp else bool(nb_rank and nb_rank <= 10)),
    }


def run_question(api, bank, q, do_reflect=False, repeats=1, min_margin=MIN_DECIDED_MARGIN):
    row = {"id": q.get("id"), "lang": q.get("lang"), "query": q.get("query"),
           "tier": q.get("tier") or "core"}
    calls = []
    attempts = []
    for r_i in range(repeats):
        c = recall(api, bank, q["query"])
        calls.append(c)
        if not c.ok:
            attempts.append({"repeat": r_i + 1, "error": c.error, "retries": c.retries})
            continue
        items = c.body.get("results") or []
        a = analyse_items(items)
        rr = reranker_stats(items)
        top = items[0] if items else None
        newer = []
        if top:
            t_at = parse_ts(top.get("mentioned_at"))
            for i, it in enumerate(items[1:], start=1):
                s_at = parse_ts(it.get("mentioned_at"))
                if t_at and s_at and s_at > t_at:
                    newer.append((i, it))
        proxy = contradiction_proxy(top, newer) if top else None
        exp = q.get("expected") or []
        expect_rank, matched = match_expected(items, exp)
        decision = ranking_decision(items, expect_rank, min_margin)
        bf = backfill_effect(items, exp, expect_rank)
        entry = {
            "repeat": r_i + 1,
            "latency_s": c.latency_s,
            "retries": c.retries,
            "n_results": len(items),
            "duplication": a,
            "reranker": rr,
            "ranking_proxy": proxy,
            "decision": decision,
            "backfill": bf,
            "top1": ({
                "id": top.get("id"), "type": top.get("type"),
                "mentioned_at": top.get("mentioned_at"),
                "final": (top.get("scores") or {}).get("final"),
                "text": (top.get("text") or "")[:240],
            } if top else None),
            "top1_is_raw": bool(top and top.get("type") in ("world", "experience")),
            "newer_siblings_in_response": len(newer),
        }
        if exp:
            entry["recall"] = {
                "expected": exp, "hit_rank": expect_rank, "matched_expectation": matched,
                "hit@1": expect_rank == 1, "hit@3": bool(expect_rank and expect_rank <= 3),
                "hit@10": bool(expect_rank and expect_rank <= 10),
                "results_available": len(items),
                "k_capped_by_response": len(items) < 10,
            }
        if q.get("stale_markers") or q.get("fresh_markers"):
            s_rank, s_hits = rank_of_markers(items, q.get("stale_markers"))
            f_rank, f_hits = rank_of_markers(items, q.get("fresh_markers"))
            entry["stale_fresh"] = {
                "stale_markers": q.get("stale_markers"), "fresh_markers": q.get("fresh_markers"),
                "stale_best_rank": s_rank, "fresh_best_rank": f_rank,
                "stale_hits": s_hits, "fresh_hits": f_hits,
                "stale_above_fresh": bool(s_rank and f_rank and s_rank < f_rank),
                "stale_only": bool(s_rank and not f_rank),
                "neither_present": not s_rank and not f_rank,
            }
        attempts.append(entry)
    row["runs"] = attempts
    okruns = [a for a in attempts if "n_results" in a]
    row["ok"] = bool(okruns) and len(okruns) == repeats
    if not okruns:
        row["error"] = attempts[0].get("error") if attempts else "no successful run"
    else:
        base = okruns[0]
        row.update({k: base[k] for k in
                    ("n_results", "duplication", "reranker", "ranking_proxy",
                     "top1", "top1_is_raw", "decision", "backfill")})
        row["latency_s"] = round(statistics.fmean([a["latency_s"] for a in okruns]), 4)
        row["expect_rank"] = (base.get("recall") or {}).get("hit_rank")
        if "recall" in base:
            row["recall"] = base["recall"]
        if "stale_fresh" in base:
            row["stale_fresh"] = base["stale_fresh"]
        if repeats > 1:
            row["variance"] = {
                "n_results": sorted({a["n_results"] for a in okruns}),
                "duplicate_pairs": sorted({a["duplication"]["duplicate_pairs"] for a in okruns}),
                "stale_above_fresh_runs": sum(
                    1 for a in okruns if (a.get("stale_fresh") or {}).get("stale_above_fresh")),
            }
    if do_reflect or q.get("reflect"):
        rc = reflect(api, bank, q["query"])
        rr = {"latency_s": rc.latency_s, "retries": rc.retries, "ok": rc.ok}
        if rc.ok:
            text = rc.body.get("text") or ""
            stale = q.get("stale_markers") or []
            fresh = q.get("fresh_markers") or []
            ctx = marker_context(text, stale)
            fresh_present = [m for m in fresh if normalize(m) in normalize(text)]
            stale_present = [m for m in stale if normalize(m) in normalize(text)]
            rr.update({
                "text": text[:6000],
                "text_len": len(text),
                "usage": rc.body.get("usage"),
                "mentions_fresh": fresh_present,
                "mentions_stale": stale_present,
                "stale_asserted_as_current": any(not c["qualified"] for c in ctx) or not ctx and bool(stale_present),
                "stale_contexts": ctx,
                "resolved": bool(fresh_present) and not (
                    any(not c["qualified"] for c in ctx) or (not ctx and bool(stale_present))),
            })
        else:
            rr["error"] = rc.error
        row["reflect"] = rr
    return row


# ------------------------------------------------------------------ aggregate

def aggregate(rows, bank):
    ok = [r for r in rows if r.get("ok")]
    n = len(ok)
    agg = {"bank": bank, "n_questions": len(rows), "n_answered": n}
    # (d) recall.
    # The HEADLINE recall@k is the CORE tier only: bank-retrievable long-term memory with a
    # selective expected token. `environment` questions (port, version, model, path, cadence -
    # readable by a live tool) are smoke and reported separately; `builtin` questions are answered
    # by the injected MEMORY.md/USER.md store and are checked against those files, not scored as
    # bank recall; `liveness` has no expectation. Mixing them into one headline is how a benchmark
    # ends up measuring the wrong store.
    rec_q = [r for r in ok if "recall" in r]

    def recall_block(rows_in):
        block = {
            "n_scored": len(rows_in),
            "recall@1": {"hits": sum(1 for r in rows_in if r["recall"]["hit@1"])},
            "recall@3": {"hits": sum(1 for r in rows_in if r["recall"]["hit@3"])},
            "recall@10": {"hits": sum(1 for r in rows_in if r["recall"]["hit@10"])},
            "misses": [r["id"] for r in rows_in if not r["recall"]["hit@10"]],
            "capped_by_response": [r["id"] for r in rows_in
                                   if r["recall"]["k_capped_by_response"]],
        }
        for k in (1, 3, 10):
            block[f"recall@{k}"]["n"] = len(rows_in)
            block[f"recall@{k}"]["rate"] = round(block[f"recall@{k}"]["hits"] / len(rows_in), 4)
            # Flat aliases: consumers (the batch gate) read these without walking the nested
            # shape, and a wrong key path silently returned None for three production batches
            # -> the recall check passed vacuously.
            block[f"recall_at_{k}"] = block[f"recall@{k}"]["rate"]
        return block

    core_q = [r for r in rec_q if (r.get("tier") or "core") == "core"]
    bank_q = [r for r in rec_q if (r.get("tier") or "core") in ("core", "environment")]
    if core_q:
        agg["recall"] = recall_block(core_q)
        agg["recall_n_scored"] = len(core_q)
        agg["recall_scope"] = ("core tier only: bank-retrievable long-term memory (the benchmark). "
                              "See by_tier for environment smoke, and recall_bank_scored for the "
                              "pre-tier core+environment rate kept for continuity.")
    if bank_q and len(bank_q) != len(core_q):
        agg["recall_bank_scored"] = recall_block(bank_q)
        agg["recall_bank_scored"]["tiers"] = ["core", "environment"]
    # Top-level flat aliases for the headline (core) rate: the batch runner and the gate read
    # agg["recall_at_3"] directly, and a missing key there means a vacuous pass, not an error.
    if core_q:
        for k in (1, 3, 10):
            agg[f"recall_at_{k}"] = agg["recall"][f"recall@{k}"]["rate"]
    # (c) ranking decision: which questions are even judgeable
    if n:
        dec = [r for r in ok if (r.get("decision") or {}).get("class") == "decided"]
        uns = [r for r in ok if (r.get("decision") or {}).get("class") == "unstable"]
        unsc = [r for r in ok if (r.get("decision") or {}).get("class") == "unscored"]
        agg["ranking_decision"] = {
            "min_margin": (ok[0].get("decision") or {}).get("min_margin"),
            "decided": len(dec), "decided_ids": [r["id"] for r in dec],
            "unstable": len(uns), "unstable_ids": [r["id"] for r in uns],
            "unscored": len(unsc), "unscored_ids": [r["id"] for r in unsc],
            "margins": {r["id"]: (r.get("decision") or {}).get("margin_top1_top2") for r in ok},
            "expect_answer_unstable_ids": [
                r["id"] for r in ok
                if (r.get("decision") or {}).get("expect_answer_class") == "unstable"],
        }
    # (c2) the same responses with backfill-sourced items removed: the backfill's own effect
    nbq = [r for r in ok if (r.get("backfill") or {}).get("hit@1_without_backfill") is not None]
    if nbq:
        def rate(key):
            return {"hits": sum(1 for r in nbq if r["backfill"][key]), "n": len(nbq),
                    "rate": round(sum(1 for r in nbq if r["backfill"][key]) / len(nbq), 4)}
        agg["recall_without_backfill"] = {
            "n_scored": len(nbq),
            "recall@1": rate("hit@1_without_backfill"),
            "recall@3": rate("hit@3_without_backfill"),
            "recall@10": rate("hit@10_without_backfill"),
            "interpretation": "attribution proxy, NOT a counterfactual",
            "method": ("identical responses with the backfill-sourced items removed and the "
                       "remaining items re-ranked; it says whether the answer survives in THIS "
                       "response without the added documents - not what retrieval would have "
                       "returned without them (top-k budget, reranking and the consolidation "
                       "window would all differ)"),
        }
        agg["backfill_effect"] = {
            "n_questions_with_backfill_items": sum(
                1 for r in ok if (r.get("backfill") or {}).get("n_backfilled_items")),
            "questions_where_backfill_changed_answer_rank": [
                r["id"] for r in nbq if r["backfill"]["answer_rank_changed_by_backfill"]],
            "questions_top1_is_backfilled": [
                r["id"] for r in ok if (r.get("backfill") or {}).get("top1_is_backfilled")],
            "total_backfilled_items_in_responses": sum(
                (r.get("backfill") or {}).get("n_backfilled_items", 0) for r in ok),
            "detail": {r["id"]: {
                "rank": r["backfill"]["expected_rank"],
                "rank_without_backfill": r["backfill"]["expected_rank_without_backfill"],
                "backfilled_items_in_response": r["backfill"]["n_backfilled_items"],
            } for r in nbq},
        }
    # (a) duplication
    if n:
        pairs = sum(r["duplication"]["duplicate_pairs"] for r in ok)
        items = sum(r["duplication"]["items_in_duplicate_pair"] for r in ok)
        total_items = sum(r["n_results"] for r in ok)
        agg["duplication"] = {
            "total_duplicate_pairs": pairs,
            "identical_pairs": sum(r["duplication"]["identical_pairs"] for r in ok),
            "fuzzy_pairs": sum(r["duplication"]["fuzzy_pairs"] for r in ok),
            "observation_vs_raw_twin_pairs": sum(
                r["duplication"]["observation_vs_raw_twin_pairs"] for r in ok),
            "items_in_a_duplicate_pair": items,
            "total_items_returned": total_items,
            "overall_duplicate_item_rate": round(items / total_items, 4) if total_items else None,
            "mean_per_question_duplicate_item_rate": round(
                statistics.fmean([r["duplication"]["duplicate_item_rate"] or 0 for r in ok]), 4),
            "questions_with_any_duplicate": sum(
                1 for r in ok if r["duplication"]["duplicate_pairs"]),
            "questions_with_observation_vs_raw_twin": sum(
                1 for r in ok if r["duplication"]["observation_vs_raw_twin_pairs"]),
        }
    # (b) ranking proxy
    if n:
        trig = [r for r in ok if (r.get("ranking_proxy") or {}).get("triggered_strict")]
        loose = [r for r in ok if (r.get("ranking_proxy") or {}).get("triggered_loose")]
        agg["staleness_proxy"] = {
            "criterion": "INFORMATIONAL ONLY - never a gate (fires on near-tie orderings)",
            "questions_top1_superseded_proxy_strict": len(trig),
            "questions_top1_superseded_proxy_strict_ids": [r["id"] for r in trig],
            "questions_top1_superseded_proxy_loose": len(loose),
            "questions_top1_superseded_proxy_loose_ids": [r["id"] for r in loose],
            "rate_strict": round(len(trig) / n, 4),
            "rate_loose": round(len(loose) / n, 4),
            "questions_top1_is_raw": sum(1 for r in ok if r["top1_is_raw"]),
            "questions_top1_superseded_strict_decided_only": sum(
                1 for r in trig if (r.get("decision") or {}).get("class") == "decided"),
        }
    # (c) latency + reranker
    if n:
        lat = sorted(r["latency_s"] for r in ok)
        agg["latency"] = {
            "mean_s": round(statistics.fmean(lat), 4),
            "median_s": round(statistics.median(lat), 4),
            "p95_s": round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 4),
            "min_s": lat[0], "max_s": lat[-1],
            "total_retries": sum(r.get("retries", 0) for r in ok),
        }
        agg["reranker"] = {
            "questions_with_reranker_engaged": sum(
                1 for r in ok if r["reranker"]["reranker_engaged"]),
            "mean_reranker_engagement_fraction": round(statistics.fmean([
                (r["reranker"]["n_with_reranker_score"] / r["n_results"]) if r["n_results"] else 0
                for r in ok]), 4),
            "questions_reranker_changed_top1_vs_semantic": sum(
                1 for r in ok if r["reranker"]["reranker_changed_top1_vs_semantic"]),
            "mean_top1_final": round(statistics.fmean([
                (r["top1"] or {}).get("final") or 0 for r in ok]), 4),
        }
    sf = [r for r in ok if "stale_fresh" in r]
    if sf:
        agg["stale_fresh_ordering"] = {
            "n_probed": len(sf),
            "questions_stale_above_fresh": sum(
                1 for r in sf if r["stale_fresh"]["stale_above_fresh"]),
            "questions_stale_above_fresh_ids": [
                r["id"] for r in sf if r["stale_fresh"]["stale_above_fresh"]],
            "questions_stale_only": sum(1 for r in sf if r["stale_fresh"]["stale_only"]),
            "detail": [{"id": r["id"], **r["stale_fresh"]} for r in sf],
        }
    rl = [r for r in rows if r.get("reflect")]
    if rl:
        agg["reflect"] = {
            "n": len(rl),
            "n_ok": sum(1 for r in rl if r["reflect"].get("ok")),
            "n_resolved": sum(1 for r in rl if r["reflect"].get("resolved")),
            "mean_latency_s": round(statistics.fmean([r["reflect"]["latency_s"] for r in rl]), 3),
            "detail": [{"id": r["id"], "ok": r["reflect"].get("ok"),
                        "resolved": r["reflect"].get("resolved"),
                        "mentions_fresh": r["reflect"].get("mentions_fresh"),
                        "mentions_stale": r["reflect"].get("mentions_stale")} for r in rl],
        }
    # (d2) recall per TIER. The core tier (facts and rules that exist only because they were
    # decided or observed in conversation) is the benchmark; the environment tier (port, version,
    # model, path, cadence - all readable by a live tool) is a smoke signal and is deliberately
    # kept out of the score.
    tiers: dict[str, list] = {}
    for r in ok:
        tiers.setdefault(r.get("tier") or "core", []).append(r)
    if tiers:
        agg["by_tier"] = {}
        for tier, rows_t in sorted(tiers.items()):
            scored_t = [r for r in rows_t if "recall" in r]
            block: dict = {"n_questions": len(rows_t), "n_scored": len(scored_t),
                           "bank_scored": tier in ("core", "environment"),
                           "store": ("bank" if tier in ("core", "environment")
                                     else "memory files" if tier == "builtin"
                                     else "none" if tier in ("liveness", "gap") else "unknown")}
            if scored_t:
                for k in (1, 3, 10):
                    hits = sum(1 for r in scored_t if r["recall"][f"hit@{k}"])
                    rate = round(hits / len(scored_t), 4)
                    block[f"recall@{k}"] = {"hits": hits, "n": len(scored_t), "rate": rate}
                    block[f"recall_at_{k}"] = rate
                block["misses"] = [r["id"] for r in scored_t if not r["recall"]["hit@10"]]
            if tier == "core":
                block["decided_ids"] = [r["id"] for r in rows_t
                                        if (r.get("decision") or {}).get("class") == "decided"]
                block["unstable_ids"] = [r["id"] for r in rows_t
                                         if (r.get("decision") or {}).get("class") == "unstable"]
            agg["by_tier"][tier] = block
    agg["failed_questions"] = [r["id"] for r in rows if not r.get("ok")]
    return agg


METHODOLOGY = {
    "duplication": (
        "For every ordered pair in one recall response, text is NFKC-normalized, "
        "casefolded, punctuation/symbol/whitespace folded, then compared. A pair is a "
        "duplicate when the folded strings are equal (similarity 1.0) or "
        "difflib.SequenceMatcher ratio >= 0.9, on either of two layers: 'strict' (whole "
        "text) or 'core' (annotation segments such as ' | When: ... | Involving: ...' "
        "removed, because raw world/experience facts carry those annotations while their "
        "derived observation does not). Normalized strings shorter than %d chars are "
        "excluded to avoid matching on boilerplate. 'observation_vs_raw_twin' = one side "
        "type=observation and the other type in {world, experience}." % MIN_NORM_LEN
    ),
    "staleness_proxy": (
        "INFORMATIONAL ONLY - it is NOT a pass/fail criterion. PROXY, NOT A VERIFIED "
        "CONTRADICTION. For each question the top-1 item must be a raw fact "
        "(world/experience); a sibling is 'newer' when its mentioned_at is later; it is "
        "flagged when it shares >=1 entity or >=35% of content tokens (Latin words len>=3 "
        "or containing a digit, plus CJK character bigrams) with the top-1 item AND either "
        "carries a value-bearing token the top-1 lacks (regex: contains a digit) or uses a "
        "supersede verb (改為/更正/作廢/replaced/updated to/...). A shared-entity newer "
        "sibling can be additive rather than contradictory, so even 'strict' is an UPPER "
        "bound on true staleness. Measured on the live bank it fired on 8 of 10 questions "
        "BEFORE any backfill ran, with the flagged reorderings decided by 0.001-0.005 score "
        "gaps, so treating it as a gate produced a false regression on batch 3."
    ),
    "ranking_decision": (
        "Which questions may be judged at all. 'decided' = the top-1 item's final score "
        "exceeds the top-2 item's by >= --min-margin (default 0.05); 'unstable' = a smaller "
        "gap, i.e. a near-tie whose order any added document can flip; 'unscored' = fewer "
        "than two items carry a final score. Only 'decided' questions take part in "
        "pass/fail; 'unstable' ones are reported (with their margins) and excluded. "
        "expect_answer_gap applies the same margin test to the position the KNOWN-CORRECT "
        "answer occupies, and the answer's own hit_rank comes from `recall`."
    ),
    "reranker_engagement": (
        "Read from each result's scores object (final/reranker/semantic/keyword). "
        "'engaged' = at least one returned item carries a non-null, non-zero reranker "
        "score. 'reranker_changed_top1_vs_semantic' compares the argmax by reranker with "
        "the argmax by semantic."
    ),
    "tiers": (
        "core = bank-retrievable long-term memory with a selective expected token; this is the "
        "benchmark and the headline recall@k. environment = facts a live tool can read back "
        "(port, version, model, path, cadence): smoke only. builtin = the answer lives in the "
        "injected MEMORY.md/USER.md store, which is always in context and is NOT retrieved from "
        "the bank; it is verified against those files by validate_golden_set.py, never scored as "
        "bank recall. gap = retained in neither store (a finding, not a retrieval item). "
        "liveness = no expectation. recall_bank_scored keeps the pre-tier core+environment rate."
    ),
    "recall": (
        "A question's expected-value list is matched any-of against each returned item's "
        "text (casefold + punctuation-folded); hit_rank is the first matching rank. "
        "NOTE: the recall endpoint has no 'limit' parameter in its schema - response size "
        "is governed by the bank's recall_max_tokens budget, so recall@10 is only bounded "
        "by the response when the response returns <10 items (flagged as "
        "k_capped_by_response)."
    ),
    "backfill_isolation": (
        "ATTRIBUTION PROXY, NOT A COUNTERFACTUAL. recall_without_backfill / backfill_effect "
        "recompute the SAME responses with every backfill-sourced item removed "
        "(metadata.source == 'backfill' or tag 'kind:backfill') and the remaining items "
        "re-ranked. This cannot reproduce what retrieval WOULD have returned without those "
        "documents - the top-k/token budget, the reranker's input set and the consolidation "
        "window would all have differed, so a different list would have been produced. It "
        "answers a narrower, still useful question: 'would the answer still be present in this "
        "response if the added documents had not been inserted?'. Read it that way. If the "
        "answer's rank is identical with and without those items, the movement came from live "
        "turns, not from the backfill. answer_rank_changed_by_backfill = True means removing "
        "the backfill-sourced items changes the answer's rank, i.e. the backfill DID move it - "
        "compare expected_rank with expected_rank_without_backfill for the direction."
    ),
    "privacy": (
        "Memory-derived text (item text, duplicate-pair text, contradiction windows) is REDACTED "
        "by default and replaced with a length marker; pass --keep-text for a local debugging run. "
        "Only the numbers a judge needs - ranks, score margins, ids, counts, classes - are kept in "
        "the default output, because these outputs are committed to the audit directory of a public "
        "repository. run.text_redacted / run.redacted_text_fields record what was removed."
    ),
    "markers": (
        "stale_markers/fresh_markers are exact token probes for controlled experiments: "
        "stale_above_fresh is true when the stale token's best rank precedes the fresh "
        "token's. For reflect, 'resolved' = the fresh token appears AND no unqualified "
        "stale mentioning remains (a stale mention is 'qualified' when a supersede/old "
        "word appears within 140 normalized chars)."
    ),
}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Hindsight recall-quality harness")
    ap.add_argument("--bank", required=True)
    ap.add_argument("--questions", required=True, help="question-set JSON path")
    ap.add_argument("--out", required=True, help="output JSON path")
    ap.add_argument("--api", default=os.environ.get("HS_API", "http://127.0.0.1:8888"))
    ap.add_argument("--repeat", type=int, default=1,
                    help="run each question N times to expose ranking variance")
    ap.add_argument("--reflect", action="store_true", help="also call /reflect per question")
    ap.add_argument("--tag", default="", help="free-text label recorded in the output")
    ap.add_argument("--min-margin", type=float, default=MIN_DECIDED_MARGIN,
                    help="top1-top2 final-score gap below which a question counts as an "
                         "unstable near-tie and is excluded from pass/fail (default %.2f)"
                         % MIN_DECIDED_MARGIN)
    ap.add_argument("--keep-text", action="store_true",
                    help="keep memory-derived item text in the output (default: REDACTED, because "
                         "the output is routinely committed to a public repo)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    with open(args.questions, encoding="utf-8") as f:
        qdoc = json.load(f)
    if isinstance(qdoc, dict) and qdoc.get("synthetic"):
        # The repository ships a synthetic template instead of the live set (the live set is
        # memory-derived and this repo is public). Measuring with it would produce numbers that
        # look real and mean nothing, so refuse loudly instead of scoring them.
        print(f"REFUSING: {args.questions} is the synthetic template, not the live question set. "
              "Point --questions at the live set (see MAINTENANCE.md 5.4).", file=sys.stderr)
        return 2
    questions = qdoc["questions"] if isinstance(qdoc, dict) else qdoc
    # "expect" (the key the committed question set uses) and "expected" (the key the
    # docs describe) are both accepted. Reading only one of them silently produced
    # recall=null for three production batches, so the recall check never ran.
    for q in questions:
        if not isinstance(q, dict):
            continue
        if not q.get("expected"):
            q["expected"] = q.get("expect") or []
    started = datetime.now(timezone.utc)
    t0 = time.time()

    ver = get_json(args.api, "/version")
    stats = get_json(args.api, f"/v1/default/banks/{args.bank}/stats")
    cfg = get_json(args.api, f"/v1/default/banks/{args.bank}/config")
    banks = get_json(args.api, "/v1/default/banks")

    rows = []
    for q in questions:
        row = run_question(args.api, args.bank, q, do_reflect=args.reflect,
                           repeats=args.repeat, min_margin=args.min_margin)
        rows.append(row)
        if not args.quiet:
            d = row.get("duplication") or {}
            dd = row.get("decision") or {}
            print(f"[{row['id']:>4}] n={row.get('n_results')} "
                  f"dup_pairs={d.get('duplicate_pairs')} "
                  f"twin={d.get('observation_vs_raw_twin_pairs')} "
                  f"hit={((row.get('recall') or {}).get('hit_rank'))} "
                  f"lat={row.get('latency_s')}s "
                  f"margin={dd.get('margin_top1_top2')} {dd.get('class')} "
                  f"proxy={((row.get('ranking_proxy') or {}).get('triggered_strict'))}",
                  flush=True)

    out = {
        "harness_version": HARNESS_VERSION,
        "run": {
            "started_at": started.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "duration_s": round(time.time() - t0, 2),
            "api": args.api,
            "api_version": (ver.body or {}).get("api_version") if ver.ok else None,
            "features": (ver.body or {}).get("features") if ver.ok else None,
            "bank": args.bank,
            "questions_file": args.questions,
            "question_count": len(questions),
            "repeat": args.repeat,
            "min_margin": args.min_margin,
            "reflect_flag": args.reflect,
            "tag": args.tag,
            "recall_request_body_shape": {"query": "<string>"},
            "recall_endpoint": f"/v1/default/banks/{args.bank}/memories/recall",
        },
        "bank_snapshot": {
            "stats": stats.body if stats.ok else {"error": stats.error},
            "config": (cfg.body or {}).get("config") if cfg.ok else {"error": cfg.error},
            "banks": (banks.body or {}).get("banks") if banks.ok else {"error": banks.error},
        },
        "aggregate": aggregate(rows, args.bank),
        "questions": rows,
        "methodology": METHODOLOGY,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    if not args.keep_text:
        # Memory text is not needed to judge retrieval quality, and these outputs get committed.
        redacted = redact_text(out["questions"]) + redact_text(out["aggregate"])
        out["run"]["text_redacted"] = True
        out["run"]["redacted_text_fields"] = redacted
        # The question set is memory-derived too, so the question text and the expected tokens go
        # as well; everything the gate reads (ids, tiers, ranks, margins, rates) stays.
        out["run"]["redacted_question_fields"] = redact_questions(out["questions"])
        out["run"]["questions_redacted"] = True
        out["run"]["privacy_note"] = (
            "item text, question text and expected tokens are redacted because this artifact is "
            "published; pass --keep-text for a local run that keeps them")
    else:
        out["run"]["text_redacted"] = False
        out["run"]["questions_redacted"] = False
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    if not args.quiet:
        print("\nAGGREGATE", json.dumps(out["aggregate"], ensure_ascii=False)[:2500])
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
