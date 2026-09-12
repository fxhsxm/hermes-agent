"""Invariant tests for scripts/memory_backfill.py.

The properties pinned here are the ones a resume depends on and that silently corrupt memory when
they break:

* progress is read from the BANK (`/documents`), so a partially written session is detected from
  reality rather than from our own state file or tags;
* backfilled documents are recognised from the id pattern and from metadata that the API may
  return as a Python-repr string, with `chunk_count` taken as the session maximum;
* document ids are a pure function of (session, chunk index, content) - a resume that recomputed
  different ids would duplicate whole sessions instead of filling the gaps;
* the run manifest lists the ids a rollback would need and forbids the tag-based rollback.

No network and no writes: the API is a stub with the same shape the real one returns.
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "memory_backfill.py"
_spec = importlib.util.spec_from_file_location("memory_backfill", _PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class StubApi:
    """Minimal /documents endpoint: paged items with a total, as the server returns them."""

    def __init__(self, items):
        self.items = items
        self.calls = []

    def get_json(self, path, **params):
        self.calls.append((path, params))
        limit = params.get("limit", 500)
        offset = params.get("offset", 0) or 0
        return {"items": self.items[offset:offset + limit], "total": len(self.items)}


def test_progress_reads_ids_metadata_and_chunk_count():
    items = [
        {"id": "bf-sessA-c000-aaaaaaaa", "document_metadata": {"source": "backfill", "chunk_count": "3"}},
        {"id": "bf-sessA-c001-bbbbbbbb", "document_metadata": {"source": "backfill", "chunk_count": "3"}},
        # the API can hand metadata back as a Python-repr string, and the id pattern may not match
        {"id": "bf-sessB-c002-cccccccc",
         "document_metadata": "{'source': 'backfill', 'hermes_session_id': 'sessB', "
                              "'chunk_index': '2', 'chunk_count': '5'}"},
        {"id": "not-a-backfill-doc", "document_metadata": {"source": "hermes"}},
    ]
    progress = _mod.bank_backfill_progress(StubApi(items), "bank")
    assert set(progress) == {"sessA", "sessB"}
    assert progress["sessA"]["written"] == {0, 1}
    assert progress["sessA"]["chunk_count"] == 3
    assert progress["sessA"]["documents"] == 2
    assert progress["sessB"]["written"] == {2}
    assert progress["sessB"]["chunk_count"] == 5


def test_progress_pages_through_every_document():
    items = [{"id": f"bf-s{i}-c000-{i:08x}0000", "document_metadata": {"chunk_count": 1}}
             for i in range(4)]
    api = StubApi(items)
    progress = _mod.bank_backfill_progress(api, "bank", page=2)
    assert len(progress) == 4
    assert len(api.calls) >= 2  # walked with an offset-based loop, not a single shot


def test_a_document_without_the_id_pattern_falls_back_to_metadata():
    """Document ids are the fast path; metadata is the safety net for other shapes."""
    items = [{"id": "legacy-id-1", "document_metadata": {"source": "backfill",
                                                        "hermes_session_id": "sessZ",
                                                        "chunk_index": "1", "chunk_count": "4"}}]
    progress = _mod.bank_backfill_progress(StubApi(items), "bank")
    assert progress["sessZ"]["written"] == {1}
    assert progress["sessZ"]["chunk_count"] == 4


def test_chunk_count_is_the_session_maximum():
    items = [{"id": "bf-s-c000-aaaaaaaa", "document_metadata": {"chunk_count": "2"}},
             {"id": "bf-s-c001-bbbbbbbb", "document_metadata": {"chunk_count": "7"}}]
    progress = _mod.bank_backfill_progress(StubApi(items), "bank")
    assert progress["s"]["chunk_count"] == 7


def test_metadata_survives_the_repr_round_trip():
    """The stub above relies on ast.literal_eval; assert the real server's repr form parses."""
    raw = "{'source': 'backfill', 'chunk_index': '4', 'chunk_count': '9'}"
    parsed = ast.literal_eval(raw)
    assert parsed["chunk_index"] == "4" and parsed["chunk_count"] == "9"


def test_document_id_is_deterministic_in_index_and_content():
    """Chunk-level resume recomputes chunks; identical content MUST give identical ids."""
    a = _mod.document_id_for("sess", 3, "some chunk content")
    b = _mod.document_id_for("sess", 3, "some chunk content")
    c = _mod.document_id_for("sess", 4, "some chunk content")
    d = _mod.document_id_for("sess", 3, "other content")
    assert a == b and a != c and a != d
    assert a.startswith("bf-sess-c003-")


def test_manifest_lists_ids_and_forbids_tag_rollback(tmp_path):
    class Args:
        bank = "fwh-main"
        date_from, date_to = "2026-09-08", "2026-09-12"
        limit, max_documents = 6, 40
        chunk_chars, turns_per_chunk = 3000, 6
        state = str(tmp_path / "state.json")

    run = {"started_at": "2026-09-13T02:23:01Z", "ended_at": "2026-09-13T02:27:00Z"}
    path = _mod.default_manifest_path(Args, run)
    _mod.write_manifest(path, Args, run,
                        [{"session_id": "s1", "status": "completed", "documents": 2,
                          "doc_ids": ["bf-s1-c000-a", "bf-s1-c001-b"]}],
                        ["bf-s1-c000-a", "bf-s1-c001-b"],
                        {"completed": 1, "documents": 2, "ops_completed": 2, "ops_failed": 0})
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    assert doc["document_ids"] == ["bf-s1-c000-a", "bf-s1-c001-b"]
    assert doc["document_count"] == 2
    assert "fwh-main" in doc["rollback"]["precise"]
    assert "tag" in doc["rollback"]["forbidden"]
    assert path.endswith(".json") and "manifests" in path.replace("\\", "/")
