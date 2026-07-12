from pathlib import Path

from persome.store import entries as entries_mod
from persome.store import fts
from persome.writer import tools as wtools


def test_dispatch_append_and_commit(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name="project-foo.md", description="Foo project", tags=["project"]
        )
        state = wtools.CommitState()
        r1 = wtools.dispatch(
            "append",
            {"path": "project-foo.md", "content": "Bar happened.", "tags": ["bar"]},
            conn=conn,
            soft_limit_tokens=20000,
            state=state,
        )
        assert r1["ok"]
        r2 = wtools.dispatch(
            "commit", {"summary": "wrote 1"}, conn=conn, soft_limit_tokens=20000, state=state
        )
        assert r2["ok"]
        assert state.committed
        assert state.summary == "wrote 1"
        assert len(state.written_ids) == 1


def test_dispatch_read_memory(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name="tool-cursor.md", description="Cursor editor", tags=["tool"]
        )
        state = wtools.CommitState()
        wtools.dispatch(
            "append",
            {"path": "tool-cursor.md", "content": "User uses Cursor.", "tags": ["editor"]},
            conn=conn,
            soft_limit_tokens=20000,
            state=state,
        )
        r = wtools.dispatch(
            "read_memory",
            {"path": "tool-cursor.md"},
            conn=conn,
            soft_limit_tokens=20000,
            state=state,
        )
        assert r["path"] == "tool-cursor.md"
        assert len(r["entries"]) == 1


def test_flag_compact_keeps_nested_same_basename_identity(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name="skill-same.md",
            description="Top-level skill",
            tags=[],
        )
        entries_mod.create_file(
            conn,
            name="skills/skill-same.md",
            description="Nested skill",
            tags=[],
        )
        state = wtools.CommitState()

        result = wtools.tool_flag_compact(
            conn,
            path="skills/skill-same.md",
            reason="nested only",
            state=state,
        )
        flags = {
            row["path"]: row["needs_compact"]
            for row in conn.execute(
                "SELECT path, needs_compact FROM files ORDER BY path"
            ).fetchall()
        }

    assert result == {"ok": True}
    assert flags == {"skill-same.md": 0, "skills/skill-same.md": 1}


def test_dispatch_search(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name="topic-rust.md", description="Rust learning", tags=["topic"]
        )
        state = wtools.CommitState()
        wtools.dispatch(
            "append",
            {"path": "topic-rust.md", "content": "User learning async Rust.", "tags": ["rust"]},
            conn=conn,
            soft_limit_tokens=20000,
            state=state,
        )
        r = wtools.dispatch(
            "search_memory",
            {"query": "async", "top_k": 3},
            conn=conn,
            soft_limit_tokens=20000,
            state=state,
        )
        assert len(r["results"]) == 1
        assert r["results"][0]["path"] == "topic-rust.md"
