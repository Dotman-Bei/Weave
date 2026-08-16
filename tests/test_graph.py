"""The graph substrate: filters, ordering, traversal, transactions, indexes."""

from __future__ import annotations

import pytest

from weave.graph import schema as S
from weave.graph.store import contains, gt, in_, is_null, not_null


def test_verify_and_indexes(store):
    assert store.verify() is True
    applied = store.ensure_schema()
    assert len(applied) == len(S.INDEX_DEFINITIONS)


def test_create_match_and_property_filters(store):
    with store.transaction() as tx:
        tx.create_node(["Fact"], {"id": "f1", "object": "python", "is_current": True,
                                  "confidence": 0.7, "valid_until": None})
        tx.create_node(["Fact"], {"id": "f2", "object": "go", "is_current": False,
                                  "confidence": 0.9, "valid_until": "2025-01-01"})

    with store.transaction() as tx:
        assert [n.id for n in tx.match("Fact", {"is_current": True})] == ["f1"]
        assert [n.id for n in tx.match("Fact", {"object": in_(["go"])})] == ["f2"]
        assert [n.id for n in tx.match("Fact", {"confidence": gt(0.8)})] == ["f2"]
        assert [n.id for n in tx.match("Fact", {"valid_until": is_null()})] == ["f1"]
        assert [n.id for n in tx.match("Fact", {"valid_until": not_null()})] == ["f2"]
        assert [n.id for n in tx.match("Fact", {"object": contains("yth")})] == ["f1"]
        ordered = tx.match("Fact", order_by=[("confidence", "desc")])
        assert [n.id for n in ordered] == ["f2", "f1"]
        assert tx.count("Fact") == 2


def test_merge_node_is_idempotent(store):
    with store.transaction() as tx:
        first, created_first = tx.merge_node(
            "Entity", {"canonical_name": "go"}, on_create={"mention_count": 1}
        )
        second, created_second = tx.merge_node(
            "Entity", {"canonical_name": "go"}, on_match={"mention_count": 5}
        )
    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    assert second.props["mention_count"] == 5


def test_expand_directions(store):
    with store.transaction() as tx:
        tx.create_node(["Entity"], {"id": "e1", "canonical_name": "user"})
        tx.create_node(["Fact"], {"id": "f1", "object": "go"})
        tx.create_edge("e1", "f1", S.HAS_FACT, {"confidence": 0.8})

    with store.transaction() as tx:
        out = tx.expand(["e1"], [S.HAS_FACT], "out")
        assert [(a.id, e.type, b.id) for a, e, b in out] == [("e1", S.HAS_FACT, "f1")]
        assert tx.expand(["e1"], [S.HAS_FACT], "in") == []
        incoming = tx.expand(["f1"], [S.HAS_FACT], "in")
        assert [b.id for _, _, b in incoming] == ["e1"]


def test_bounded_paths_do_not_revisit_nodes(store):
    with store.transaction() as tx:
        for node_id in ("a", "b", "c", "d"):
            tx.create_node(["Entity"], {"id": node_id, "canonical_name": node_id})
        tx.create_edge("a", "b", S.MENTIONS, {})
        tx.create_edge("b", "c", S.MENTIONS, {})
        tx.create_edge("c", "d", S.MENTIONS, {})

    with store.transaction() as tx:
        paths = tx.paths(["a"], [S.MENTIONS], "out", max_len=3, path_count=10, result_limit=50)
        walks = {"".join(n.id for n in p.nodes) for p in paths}
        assert walks == {"ab", "abc", "abcd"}

        shallow = tx.paths(["a"], [S.MENTIONS], "out", max_len=1, path_count=10, result_limit=50)
        assert {"".join(n.id for n in p.nodes) for p in shallow} == {"ab"}


def test_paths_respect_result_limit(store):
    with store.transaction() as tx:
        tx.create_node(["Entity"], {"id": "hub", "canonical_name": "hub"})
        for index in range(10):
            tx.create_node(["Utterance"], {"id": f"u{index}", "text": str(index)})
            tx.create_edge("hub", f"u{index}", S.MENTIONS, {})

    with store.transaction() as tx:
        paths = tx.paths(["hub"], [S.MENTIONS], "out", max_len=1, path_count=3, result_limit=3)
        assert len(paths) == 3


def test_transaction_rolls_back_on_error(store):
    with pytest.raises(RuntimeError):
        with store.transaction() as tx:
            tx.create_node(["Fact"], {"id": "doomed", "object": "x"})
            raise RuntimeError("boom")

    with store.transaction() as tx:
        assert tx.get_node("doomed") is None


def test_nested_transactions_join_the_outer_one(store):
    with store.transaction() as outer:
        outer.create_node(["Fact"], {"id": "outer", "object": "x"})
        with store.transaction() as inner:
            inner.create_node(["Fact"], {"id": "inner", "object": "y"})

    with store.transaction() as tx:
        assert tx.get_node("outer") is not None
        assert tx.get_node("inner") is not None


def test_stats_group_by_layer(store):
    with store.transaction() as tx:
        tx.create_node([S.SESSION], {"id": "s1"})
        tx.create_node([S.FACT], {"id": "f1"})
        tx.create_node([S.QUERY_TYPE], {"id": "q1"})
    stats = store.stats()
    assert stats["by_layer"] == {"episodic": 1, "semantic": 1, "procedural": 1}


def test_upsert_edge_replaces_rather_than_duplicating(store):
    """Edges that carry running state must not accumulate copies.

    ``create_edge`` may add a parallel relationship -- that is fine for
    provenance edges but silently breaks a counter, because each duplicate
    holds only part of the count.
    """
    with store.transaction() as tx:
        tx.create_node([S.QUERY_TYPE], {"id": "qt", "name": "factual"})
        tx.create_node([S.RETRIEVAL_PATH], {"id": "rp", "name": "semantic-only"})
        for attempts in (1, 2, 3):
            tx.upsert_edge("qt", "rp", S.BEST_PATH_FOR, {"attempts": attempts})

    with store.transaction() as tx:
        edges = tx.expand(["qt"], [S.BEST_PATH_FOR], "out")
        assert len(edges) == 1, "upsert must not create parallel edges"
        assert edges[0][1].get("attempts") == 3


def test_consistency_mode_is_configurable(store):
    """§8.3: causal for ingestion, strong for reads needing latest state."""
    if store.name != "hydra":
        pytest.skip("consistency modes apply to the Bolt backend")

    # Strong consistency chains bookmarks between sessions so a read observes
    # every write this client has already committed.
    store.consistency = "strong"
    store._bookmarks = None
    with store.transaction() as tx:
        tx.create_node([S.FACT], {"id": "consistency-probe", "object": "go"})
    assert store._bookmarks is not None

    with store.transaction() as tx:
        found = tx.match(S.FACT, {"id": "consistency-probe"})
    assert found and found[0].get("object") == "go"


def test_reset_clears_everything(store):
    with store.transaction() as tx:
        tx.create_node([S.FACT], {"id": "f1"})
    store.reset()
    assert store.stats()["nodes"] == 0


def test_contains_is_case_insensitive_on_every_backend(store):
    """SQL LIKE ignores ASCII case; Cypher CONTAINS does not.

    The filter has to mean one thing, or the same query returns different rows
    depending on which backend is configured.
    """
    with store.transaction() as tx:
        tx.create_node([S.UTTERANCE], {"id": "u1", "text": "I moved to Lisbon last month."})

    with store.transaction() as tx:
        for probe in ("lisbon", "Lisbon", "LISBON"):
            found = tx.match(S.UTTERANCE, {"text": contains(probe)})
            assert [n.id for n in found] == ["u1"], f"{probe!r} did not match"


def test_search_text_finds_terms_and_stays_in_sync(store):
    """The text index must never disagree with the nodes table.

    A stale index returns confidently wrong answers, which is worse than
    having none — so it is maintained by triggers, and this checks the whole
    lifecycle: insert, update, delete.
    """
    with store.transaction() as tx:
        tx.create_node([S.UTTERANCE], {"id": "u1", "text": "I moved to Lisbon in March."})
        tx.create_node([S.UTTERANCE], {"id": "u2", "text": "I still run Docker for everything."})

    with store.transaction() as tx:
        found = tx.search_text(S.UTTERANCE, "text", ["lisbon"])
        assert [n.id for n in found] == ["u1"]
        assert {n.id for n in tx.search_text(S.UTTERANCE, "text", ["lisbon", "docker"])} == {"u1", "u2"}
        assert tx.search_text(S.UTTERANCE, "text", ["kubernetes"]) == []

    with store.transaction() as tx:
        tx.set_props("u1", {"text": "I moved to Porto instead."})
    with store.transaction() as tx:
        assert tx.search_text(S.UTTERANCE, "text", ["lisbon"]) == [], "index kept stale text"
        assert [n.id for n in tx.search_text(S.UTTERANCE, "text", ["porto"])] == ["u1"]

    store.reset()
    with store.transaction() as tx:
        assert tx.search_text(S.UTTERANCE, "text", ["porto"]) == [], "index outlived reset"


def test_search_text_survives_punctuation(store):
    """Query text is user input; FTS5 MATCH syntax must not leak into it."""
    with store.transaction() as tx:
        tx.create_node([S.UTTERANCE], {"id": "u1", "text": "My co-worker's laptop broke."})
    with store.transaction() as tx:
        for probe in ("co-worker's", '"quoted"', "wild*card", "OR", "("):
            tx.search_text(S.UTTERANCE, "text", [probe])  # must not raise
