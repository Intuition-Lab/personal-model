import sqlite3

import pytest

from persome.evomem.models import MemoryLayer, MemoryNode, MemoryStatus
from persome.evomem.store import NodeStore
from persome.store import fts


@pytest.fixture
def store(ac_root):
    return NodeStore(user_id="u1")


def test_save_and_get(store):
    store.save(
        MemoryNode(node_id="a", content="\u559c\u6b22\u5496\u5561", layer=MemoryLayer.L2_FACT)
    )
    got = store.get("a")
    assert got is not None and got.content == "\u559c\u6b22\u5496\u5561"
    assert got.is_latest is True


def test_search_returns_hits_with_node(store):
    store.save(
        MemoryNode(
            node_id="a",
            content="\u7528\u6237\u559c\u6b22\u79d1\u5e7b\u7535\u5f71",
            layer=MemoryLayer.L2_FACT,
        )
    )
    store.save(
        MemoryNode(
            node_id="b", content="\u7528\u6237\u4f4f\u5728\u4e0a\u6d77", layer=MemoryLayer.L2_FACT
        )
    )
    hits = store.search("\u79d1\u5e7b", top_k=5)
    assert any(h["node_id"] == "a" for h in hits)
    assert hits[0]["node"].content == "\u7528\u6237\u559c\u6b22\u79d1\u5e7b\u7535\u5f71"


def test_save_and_supersede_atomic_single_active_head(store):

    store.save(MemoryNode(node_id="a", content="\u559d\u5496\u5561", layer=MemoryLayer.L2_FACT))
    new = MemoryNode(
        node_id="b", content="\u559d\u8336", layer=MemoryLayer.L2_FACT, supersedes=["a"]
    )
    store.save_and_supersede(new, old_id="a")

    old, head = store.get("a"), store.get("b")
    assert old.status is MemoryStatus.SHADOW and old.is_latest is False
    assert old.superseded_by == ["b"]
    assert head.status is MemoryStatus.ACTIVE and head.is_latest is True
    assert head.supersedes == ["a"]

    actives = [n for n in store.all_latest()]
    assert len(actives) == 1 and actives[0].node_id == "b"


def test_save_and_supersede_backfills_new_supersedes_pointer(store):

    store.save(MemoryNode(node_id="a", content="\u559d\u5496\u5561", layer=MemoryLayer.L2_FACT))
    new = MemoryNode(node_id="b", content="\u559d\u8336", layer=MemoryLayer.L2_FACT)
    store.save_and_supersede(new, old_id="a")
    assert store.get("b").supersedes == ["a"]


def test_save_and_supersede_missing_old_raises(store):
    new = MemoryNode(node_id="b", content="\u559d\u8336", layer=MemoryLayer.L2_FACT)
    with pytest.raises(KeyError):
        store.save_and_supersede(new, old_id="does-not-exist")


def test_save_and_supersede_many_links_every_predecessor_atomically(store):
    for node_id in ("a", "b"):
        store.save(MemoryNode(node_id=node_id, content=node_id, layer=MemoryLayer.L2_FACT))
    new = MemoryNode(node_id="c", content="merged", layer=MemoryLayer.L2_FACT)

    store.save_and_supersede_many(new, old_ids=["a", "b"])

    assert store.get("c").supersedes == ["a", "b"]
    for node_id in ("a", "b"):
        old = store.get(node_id)
        assert old.status is MemoryStatus.SHADOW and old.is_latest is False
        assert old.superseded_by == ["c"]
    assert [node.node_id for node in store.all_latest()] == ["c"]


def test_save_and_supersede_many_rolls_back_every_pointer_on_second_update_failure(store):
    for node_id in ("a", "b"):
        store.save(MemoryNode(node_id=node_id, content=node_id, layer=MemoryLayer.L2_FACT))
    with fts.cursor() as conn:
        conn.execute(
            "CREATE TRIGGER fail_second_predecessor BEFORE UPDATE ON evo_nodes "
            "WHEN OLD.node_id='b' AND NEW.is_latest=0 "
            "BEGIN SELECT RAISE(ABORT, 'synthetic second predecessor failure'); END"
        )
    try:
        with pytest.raises(sqlite3.IntegrityError, match="synthetic second predecessor failure"):
            store.save_and_supersede_many(
                MemoryNode(node_id="c", content="merged", layer=MemoryLayer.L2_FACT),
                old_ids=["a", "b"],
            )
    finally:
        with fts.cursor() as conn:
            conn.execute("DROP TRIGGER fail_second_predecessor")

    assert store.get("c") is None
    for node_id in ("a", "b"):
        old = store.get(node_id)
        assert old.status is MemoryStatus.ACTIVE and old.is_latest is True
        assert old.superseded_by == []


def test_save_and_shadow_single_active_head_no_chain_link(store):

    store.save(MemoryNode(node_id="a", content="\u559d\u5496\u5561", layer=MemoryLayer.L2_FACT))
    new = MemoryNode(
        node_id="b", content="\u559d\u7f8e\u5f0f\u5496\u5561", layer=MemoryLayer.L2_FACT
    )
    store.save_and_shadow(new, old_id="a")

    old, head = store.get("a"), store.get("b")
    assert old.status is MemoryStatus.SHADOW and old.is_latest is False
    assert old.superseded_by == []
    assert head.is_latest is True and head.status is MemoryStatus.ACTIVE
    assert head.supersedes == []
    actives = store.all_latest()
    assert len(actives) == 1 and actives[0].node_id == "b"


def test_save_and_shadow_missing_old_still_saves_new(store):

    new = MemoryNode(node_id="b", content="x", layer=MemoryLayer.L2_FACT)
    store.save_and_shadow(new, old_id="does-not-exist")
    actives = store.all_latest()
    assert len(actives) == 1 and actives[0].node_id == "b"


def test_get_by_ids_returns_shadow_nodes_too(store):
    store.save(
        MemoryNode(
            node_id="a",
            content="x",
            layer=MemoryLayer.L2_FACT,
            status=MemoryStatus.SHADOW,
            is_latest=False,
        )
    )
    assert store.get_by_ids(["a"])[0].node_id == "a"
