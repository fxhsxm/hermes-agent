#!/usr/bin/env python3
"""check_artifacts_privacy.py -- fail closed if a committed artifact carries private memory.

Why this exists: the audit artifacts are published from a PUBLIC fork. The first harness version
wrote memory item text into its outputs and four of those outputs were committed; a later review
pointed out that the golden question set is itself memory-derived (a question plus its expected
token is a structured statement about the user's machine and preferences), so redacting item text
alone was not enough. Editing a file does not remove the old blob from git history either - this
check keeps the WORKING TREE honest and flags what history still needs.

What it does: collects private strings from sources that must never be published, then scans the
tree. A match is a failure (exit 1), naming the file and the matched string's fingerprint - never
the string itself, because printing it would leak exactly what it detects.

Sources (all read-only, all optional):
  * a question set JSON: every `query` and every `expect`/`expected` token;
  * the injected memory store (MEMORY.md / USER.md): every line of useful length;
  * optionally the bank's memory item texts (`--with-bank-items`), which is how the original leak
    would have been caught before the commit rather than after.

USAGE
  python check_artifacts_privacy.py --tree docs/audits --questions <set.json> \
      --builtin-file <MEMORY.md> --builtin-file <USER.md> [--with-bank-items]
  # exit 0 = clean, 1 = private strings found, 2 = a source could not be read

For a whole-repository scan prefer `--changed-since <ref>`: this tree is a fork of an upstream
project, and upstream code legitimately contains identifiers that also appear in a question's
expected token (a schema name, for example). Only what this branch adds or modifies is ours to
answer for.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys

# Short strings like "3.11" or "user" are identifiers, not memory, and match everything. Question
# text and expected tokens get a lower bar than free text: they are short by nature but a question
# plus its answer is exactly the structured memory that must not be published.
MIN_LEN = 20
MIN_LEN_QUESTIONS = 12
# Scan everything readable, not just known suffixes: a private file with an unexpected name (or no
# extension at all) is exactly the case a suffix allow-list misses. Only clearly binary containers
# are skipped.
SKIP_SUFFIXES = (".zip", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".exe", ".dll", ".so",
                 ".whl", ".7z", ".gz", ".tar", ".db", ".pyc", ".bin", ".mp3", ".mp4", ".wav")


def fingerprint(s: str) -> str:
    """Stable, non-reversible tag for a matched string."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def strings_from_questions(path: str, min_len: int = MIN_LEN):
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    questions = doc["questions"] if isinstance(doc, dict) else doc
    for q in questions:
        if not isinstance(q, dict):
            continue
        for key in ("query", "expect", "expected"):
            v = q.get(key)
            if isinstance(v, str):
                v = [v]
            for s in (v or []):
                if isinstance(s, str) and len(s.strip()) >= min_len:
                    yield f"question {q.get('id')}.{key}", s.strip()


def strings_from_files(paths, min_len: int = MIN_LEN):
    for p in paths:
        try:
            with open(p, encoding="utf-8") as fh:
                for i, line in enumerate(fh, 1):
                    line = line.strip()
                    if len(line) >= min_len:
                        yield f"{os.path.basename(p)}:{i}", line
        except OSError as exc:
            raise OSError(f"{p}: {exc}") from exc


def strings_from_bank(api: str, bank: str, min_len: int = MIN_LEN):
    import httpx

    client = httpx.Client(timeout=180)
    offset = 0
    while True:
        d = client.get(f"{api}/v1/default/banks/{bank}/memories/list",
                       params={"limit": 500, "offset": offset}).json()
        batch = d.get("items") or []
        for it in batch:
            t = (it.get("text") or "").strip()
            if len(t) >= min_len:
                yield "bank item", t
        if not batch or (d.get("total") is not None and offset + len(batch) >= d["total"]):
            break
        offset += len(batch)


def walk_tree(tree: str):
    for root, dirs, files in os.walk(tree):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__")]
        for f in files:
            if not f.lower().endswith(SKIP_SUFFIXES):
                yield os.path.join(root, f)


def scan(private, files, min_len: int = MIN_LEN):
    """Return [{'file', 'source', 'fingerprint', 'length'}]; never returns the matched string."""
    # Longest first: a leak usually contains a whole line, and reporting the most specific
    # fingerprint makes the finding reproducible without printing private content.
    plist = sorted(private, key=lambda kv: -len(kv[1]))
    findings = []
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                body = fh.read()
        except OSError:
            continue
        for source, s in plist:
            if len(s) >= min_len and s in body:
                findings.append({"file": f, "source": source, "fingerprint": fingerprint(s),
                                 "length": len(s)})
                break          # one finding per file is enough to fail it
    return findings


def redact_tree(private, files, min_len: int = MIN_LEN, dry_run: bool = False):
    """Replace every private string found in `files` with a fingerprint marker, in place.

    A marker keeps the audit trail (the reader can see that something was removed and which
    fingerprint it was) without republishing the content. Returns per-file replacement counts.
    """
    plist = sorted(private, key=lambda kv: -len(kv[1]))
    out = {}
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                body = fh.read()
        except OSError:
            continue
        hits = 0
        for source, s in plist:
            if len(s) >= min_len and s in body:
                body = body.replace(s, f"[REDACTED:{fingerprint(s)}]")
                hits += 1
        if hits and not dry_run:
            with open(f, "w", encoding="utf-8", newline="") as fh:
                fh.write(body)
        if hits:
            out[f] = hits
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fail if committed artifacts carry private memory.")
    ap.add_argument("--tree", default="docs/audits", help="directory to scan")
    ap.add_argument("--questions", action="append", default=[],
                    help="golden question set(s) that must not appear in the tree")
    ap.add_argument("--builtin-file", action="append", default=[],
                    help="injected memory file(s) (MEMORY.md/USER.md) that must not appear")
    ap.add_argument("--api", default="http://127.0.0.1:8888")
    ap.add_argument("--bank", default="fwh-main")
    ap.add_argument("--with-bank-items", action="store_true",
                    help="also treat every bank memory item as private (slow, strongest)")
    ap.add_argument("--min-len", type=int, default=MIN_LEN,
                    help="minimum length for free-text sources (bank items, memory files)")
    ap.add_argument("--min-len-questions", type=int, default=MIN_LEN_QUESTIONS,
                    help="minimum length for question text and expected tokens")
    ap.add_argument("--max-findings", type=int, default=25)
    ap.add_argument("--changed-since",
                    help="only scan files this branch added or modified since <ref> (plus "
                         "untracked files); run from the repository root")
    ap.add_argument("--redact", action="store_true",
                    help="replace the matches in place with [REDACTED:<fingerprint>] instead of "
                         "only reporting them")
    ap.add_argument("--dry-run", action="store_true", help="with --redact: report, do not write")
    args = ap.parse_args(argv)

    private = []
    try:
        for q in args.questions:
            private += list(strings_from_questions(q, args.min_len_questions))
        private += list(strings_from_files(args.builtin_file, args.min_len))
        if args.with_bank_items:
            private += list(strings_from_bank(args.api, args.bank, args.min_len))
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"[fail] could not read a private source: {exc}", file=sys.stderr)
        return 2        # fail closed: an unreadable source means "cannot prove clean"

    if not private:
        print("[fail] no private sources given, so nothing can be checked", file=sys.stderr)
        return 2

    files = list(walk_tree(args.tree))
    if not files:
        # A scan that matched nothing is not a clean tree, it is a wrong scope - and reporting
        # [ok] for it is exactly the "vacuous pass" failure this whole audit keeps finding.
        print(f"[fail] no files matched --tree {args.tree} "
              f"(cwd={os.getcwd()}), so 'clean' would be vacuous", file=sys.stderr)
        return 2
    if args.changed_since:
        def git(*a):
            r = subprocess.run(["git", *a], capture_output=True, text=True, encoding="utf-8")
            return [x for x in (r.stdout or "").splitlines() if x.strip()]
        changed = set(git("diff", "--name-only", "--diff-filter=ACMR",
                          f"{args.changed_since}..HEAD"))
        changed |= set(git("ls-files", "--others", "--exclude-standard"))
        keep = {os.path.abspath(x) for x in changed}
        files = [f for f in files if os.path.abspath(f) in keep]
        print(f"scope limited to files changed since {args.changed_since}")
    print(f"scanned {len(files)} file(s) under {args.tree} against {len(private)} private string(s)")
    if args.redact:
        counts = redact_tree(private, files, args.min_len, dry_run=args.dry_run)
        verb = "would redact" if args.dry_run else "redacted"
        print(f"\n{verb} {len(counts)} file(s):")
        for f, n in sorted(counts.items()):
            print(f"  {f}  ({n} string(s))")
        if not args.dry_run:
            findings = scan(private, files, args.min_len)
            print(f"\nre-scan: {len(findings)} file(s) still contain private memory")
            return 1 if findings else 0
        return 0

    findings = scan(private, files, args.min_len)
    if findings:
        print(f"\n[fail] {len(findings)} file(s) contain private memory "
              f"(showing up to {args.max_findings}):")
        for x in findings[:args.max_findings]:
            print(f"  {x['file']}  <- {x['source']} "
                  f"[len={x['length']} fp={x['fingerprint']}]")
        print("\nRedact these files (harness/validator do it by default) and re-run. "
              "Note that any already-committed version of them remains in git history.")
        return 1
    print("[ok] no private memory found in the scanned tree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
