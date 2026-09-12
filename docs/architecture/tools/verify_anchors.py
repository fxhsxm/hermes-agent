#!/usr/bin/env python3
"""Verify the code anchors used by the execution architecture map.

The map (``docs/architecture/``) makes verifiable claims about the source tree in
one machine-checkable grammar.  This script re-checks them against a checkout, so
the document can be re-validated after any refactor instead of silently rotting.

Anchor grammar
--------------
Inside an inline code span:

    `path/to/file.py`               -> the file exists
    `path/to/file.py:120`           -> the file exists and has a line 120
    `path/to/file.py:120-134`       -> both bounds exist (and 120 <= 134)

Optional symbol assertion: when the anchor's code span is immediately followed on
the same line by exactly one more inline code span that parses as an identifier
(``symbol``, ``symbol()``, ``Class.method``), that identifier must occur within
``--window`` lines of the anchor line (default 3).  This catches the common rot
mode where the line number stays plausible but now points at other code.

Paths are resolved against the repository root (the parent of the directory that
holds this script, or ``--root``).  Lines quoted from third-party/vendored trees
are out of scope by construction: only anchors resolving inside the root count.

Usage
-----
    python docs/architecture/tools/verify_anchors.py
    python docs/architecture/tools/verify_anchors.py docs/architecture/*.md
    python docs/architecture/tools/verify_anchors.py --json

Exit code 0 = every anchor resolved; 1 = at least one anchor failed; 2 = usage or
setup error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Tier 1 (hard): a path WITH a line number — an anchor claim the document relies on.
ANCHOR_RE = re.compile(
    r"`(?P<path>(?:[\w.@+-]+/)*[\w.@+-]+\.[A-Za-z0-9_]+):(?P<start>\d+)(?:-(?P<end>\d+))?`"
)
# Tier 2 (informational): a bare path mention — may be prose, a runtime path, a
# placeholder, or a deliberately quoted stale path, so it is reported but not failed.
PATH_RE = re.compile(r"`(?P<path>(?:[\w.@+-]+/)*[\w.@+-]+\.[A-Za-z0-9_]+)`")
# A code span that follows the anchor span on the same line and is a symbol claim.
SYMBOL_RE = re.compile(r"^`(?P<sym>[A-Za-z_][\w.]*(?:\(\))?)`")
IDENT_RE = re.compile(r"^[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*(?:\(\))?$")
SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build"}


def _root_default() -> Path:
    return Path(__file__).resolve().parents[3]


def _line_count(path: Path) -> int:
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return -1


def _symbol_ok(path: Path, start: int, symbol: str, window: int) -> bool:
    """True when ``symbol`` appears in path[start-window : start+window]."""
    name = symbol[:-2] if symbol.endswith("()") else symbol
    leaf = name.rsplit(".", 1)[-1]
    lo = max(1, start - window)
    hi = start + window
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for idx, line in enumerate(fh, start=1):
                if idx < lo:
                    continue
                if idx > hi:
                    return False
                if name in line or leaf in line:
                    return True
    except OSError:
        return False
    return False


def _resolve(rel: str, md_path: Path, root: Path, index: dict[str, list[Path]]) -> tuple[Path | None, str]:
    """Resolve an anchor path: repo-root, then document dir, then a UNIQUE basename.

    The basename tier exists so the delta log can cite another document by its own
    filename (`session-storage.md:150`). It stays strict: an ambiguous basename is a
    failure, never a guess.
    """
    if (root / rel).is_file():
        return root / rel, ""
    if (md_path.parent / rel).is_file():
        return md_path.parent / rel, ""
    if "/" not in rel:
        matches = index.get(rel, [])
        if len(matches) == 1:
            return matches[0], "basename"
        if len(matches) > 1:
            return None, f"ambiguous basename ({len(matches)} files match)"
    return None, "file not found"


def _build_index(root: Path) -> dict[str, list[Path]]:
    # Translations/versioned copies are duplicates of the source docs; excluding them keeps
    # the "unique basename" tier honest.
    skip = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build",
            "i18n", "versioned_docs", "versioned_sidebars"}
    index: dict[str, list[Path]] = {}
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in {".py", ".md", ".json", ".yaml", ".ts", ".tsx", ".sh", ".toml"}:
            if not any(part in skip for part in path.parts):
                index.setdefault(path.name, []).append(path)
    return index


def check_file(md_path: Path, root: Path, window: int, index: dict[str, list[Path]]) -> dict:
    text = md_path.read_text(encoding="utf-8", errors="replace")
    checked = failures = mentions = 0
    details: list[dict] = []
    unresolved_mentions: list[dict] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        # Tier 2: bare path mentions that resolve nowhere (informational).
        for m in PATH_RE.finditer(line):
            rel2 = m.group("path")
            if (root / rel2).is_file() or (md_path.parent / rel2).is_file():
                continue
            if "/" not in rel2 and len(index.get(rel2, [])) == 1:
                continue
            mentions += 1
            unresolved_mentions.append({"doc_line": lineno, "path": rel2})
        for match in ANCHOR_RE.finditer(line):
            rel = match.group("path")
            start = match.group("start")
            end = match.group("end")
            checked += 1
            target, why = _resolve(rel, md_path, root, index)
            if target is None:
                failures += 1
                details.append(
                    {"doc_line": lineno, "anchor": rel, "reason": why}
                )
                continue
            if start is None:
                continue
            total = _line_count(target)
            first, last = int(start), int(end) if end else int(start)
            if first < 1 or last < first or last > total:
                failures += 1
                details.append(
                    {
                        "doc_line": lineno,
                        "anchor": f"{rel}:{start}" + (f"-{end}" if end else ""),
                        "reason": f"line out of range (file has {total} lines)",
                    }
                )
                continue
            tail = line[match.end():].strip()
            sym_match = SYMBOL_RE.match(tail)
            if sym_match and IDENT_RE.match(sym_match.group("sym")):
                symbol = sym_match.group("sym")
                if not _symbol_ok(target, first, symbol, window):
                    failures += 1
                    details.append(
                        {
                            "doc_line": lineno,
                            "anchor": f"{rel}:{start}",
                            "reason": f"symbol `{symbol}` not found near line {first}",
                        }
                    )
    return {
        "doc": str(md_path),
        "anchors_checked": checked,
        "failures": failures,
        "details": details,
        "unresolved_mentions": mentions,
        "mention_details": unresolved_mentions,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", help="markdown files (default: docs/architecture/*.md)")
    parser.add_argument("--root", default=None, help="repository root (default: repo of this script)")
    parser.add_argument("--window", type=int, default=3, help="symbol search window in lines")
    parser.add_argument("--json", action="store_true", help="emit a JSON summary")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else _root_default()
    if not (root / "run_agent.py").is_file():
        print(f"error: {root} does not look like the hermes-agent root", file=sys.stderr)
        return 2

    index = _build_index(root)

    if args.files:
        docs = [Path(p) for p in args.files]
    else:
        docs = sorted((root / "docs" / "architecture").glob("*.md"))
    docs = [d for d in docs if d.is_file()]
    if not docs:
        print("error: no documents to check", file=sys.stderr)
        return 2

    results = [check_file(d, root, args.window, index) for d in docs]
    total_checked = sum(r["anchors_checked"] for r in results)
    total_failures = sum(r["failures"] for r in results)
    total_mentions = sum(r["unresolved_mentions"] for r in results)

    if args.json:
        print(json.dumps({"root": str(root), "results": results,
                          "anchors_checked": total_checked, "failures": total_failures,
                          "unresolved_mentions": total_mentions}, indent=2))
    else:
        for r in results:
            status = "OK" if r["failures"] == 0 else f"FAIL ({r['failures']})"
            print(f"{status:>10}  {r['anchors_checked']:>4} anchors  {r['doc']}")
            for d in r["details"]:
                print(f"            line {d['doc_line']}: {d['anchor']} -> {d['reason']}")
        print(f"\nTOTAL: {total_checked} anchors checked, {total_failures} failed")
        print(f"       {total_mentions} bare path mentions did not resolve "
              f"(informational: prose, runtime paths such as HERMES_HOME files, placeholders, "
              f"or stale paths quoted on purpose)")
    return 1 if total_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
