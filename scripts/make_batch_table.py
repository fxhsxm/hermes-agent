#!/usr/bin/env python3
"""Render the batch results in a result JSON directory as a Markdown table (for REPORT-V3)."""
from __future__ import annotations

import glob
import json
import os
import sys


def main(d: str) -> int:
    rows = []
    for p in sorted(glob.glob(os.path.join(d, "backfill_batch*_result.json"))):
        with open(p, encoding="utf-8") as fh:
            r = json.load(fh)
        n = r.get("batch")
        if not n:  # batch 0 is the protocol rehearsal, not a written batch
            continue
        if not (r.get("backfill") or {}).get("manifest_documents"):
            continue  # the legacy batch1-3 runner results (no manifest) are excluded
        b, a = r.get("before") or {}, r.get("after") or {}
        g, w = r.get("gate") or {}, r.get("write_check") or {}
        bank = f"{(r.get('bank_before') or {}).get('total_documents')} -> {(r.get('bank_after') or {}).get('total_documents')}"
        rows.append([
            n,
            str((r.get("backfill") or {}).get("manifest_documents")),
            str((r.get("backfill") or {}).get("ops_failed")),
            bank,
            f"{b.get('recall@1')} -> {a.get('recall@1')}",
            f"{b.get('recall@3')} -> {a.get('recall@3')}",
            f"{b.get('dup_item_rate')} -> {a.get('dup_item_rate')}",
            ", ".join(g.get("flipped") or []) or "-",
            "PASS" if (g.get("all_ok") and w.get("ok")) else "**FAIL**",
        ])
    rows.sort(key=lambda r: r[0])
    print("| batch | docs | ops failed | bank documents | recall@1 | recall@3 | duplicate rate | flipped (rank changed) | gate |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print("| " + " | ".join(str(x) for x in r) + " |")
    tot = sum(int(r[1]) for r in rows)
    print(f"\nTotal: {len(rows)} batches, {tot} documents written under the corrected gate.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
