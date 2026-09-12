"""The privacy gate (scripts/check_artifacts_privacy.py) must fail closed, not look clean.

Two failure modes matter more than the happy path: reporting "[ok]" without having scanned
anything (a vacuous pass), and printing the private string it matched (which leaks exactly what it
is protecting).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_artifacts_privacy.py"
_spec = importlib.util.spec_from_file_location("check_artifacts_privacy", _PATH)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_scan_reports_the_file_and_a_fingerprint_but_never_the_string(tmp_path):
    f = tmp_path / "artifact.json"
    secret = "<PRIVATE-DELIVERY-PATH-2026>"
    f.write_text('{"note": "the delivery directory is ' + secret + '"}', encoding="utf-8")
    findings = _mod.scan([("question q1.query", secret)], [str(f)])
    assert len(findings) == 1
    assert findings[0]["file"] == str(f)
    assert findings[0]["fingerprint"] == _mod.fingerprint(secret)
    assert secret not in repr(findings)


def test_scan_of_a_clean_file_finds_nothing(tmp_path):
    f = tmp_path / "artifact.json"
    f.write_text('{"n": 1}', encoding="utf-8")
    assert _mod.scan([("q", "a private sentence")], [str(f)]) == []


def test_an_empty_scope_is_a_failure_not_a_clean_result(tmp_path, capsys):
    """'scanned 0 files' must never print [ok]: that is how a wrong --tree passes silently."""
    q = tmp_path / "q.json"
    q.write_text('{"questions": [{"id": "q1", "query": "a long enough question text"}]}',
                 encoding="utf-8")
    rc = _mod.main(["--tree", str(tmp_path / "does-not-exist"), "--questions", str(q)])
    assert rc == 2
    assert "vacuous" in capsys.readouterr().err


def test_an_unreadable_private_source_is_a_failure(tmp_path, capsys):
    """Fail closed: if the private sources cannot be read, cleanliness is unproven."""
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.json").write_text("{}", encoding="utf-8")
    rc = _mod.main(["--tree", str(tree), "--questions", str(tmp_path / "missing.json")])
    assert rc == 2
    assert "could not read" in capsys.readouterr().err


def test_questions_yield_query_and_expect_tokens(tmp_path):
    q = tmp_path / "q.json"
    q.write_text('{"questions": [{"id": "q1", "query": "a long enough question text",'
                 ' "expect": ["a long enough token"]}]}', encoding="utf-8")
    got = dict(_mod.strings_from_questions(str(q), min_len=10))
    assert got["question q1.query"] == "a long enough question text"
    assert got["question q1.expect"] == "a long enough token"
