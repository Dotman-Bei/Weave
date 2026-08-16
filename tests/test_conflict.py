"""Conflict detection and the consolidation policies that resolve it."""

from __future__ import annotations

import pytest

from weave.graph import schema as S

from .conftest import session_payload


def test_contradiction_creates_a_conflict(weave, python_session, go_session):
    weave.ingest(python_session)
    result = weave.ingest(go_session)

    assert result.conflicts_detected == 1
    with weave.store.transaction() as tx:
        conflicts = tx.match(S.CONFLICT, {"status": "open"})
        assert len(conflicts) == 1
        involved = tx.expand([conflicts[0].id], [S.INVOLVES], "out", target_label=S.FACT)
        assert {node.props["object"] for _, _, node in involved} == {"python", "go"}


def test_conflict_does_not_destroy_the_old_fact(weave, python_session, go_session):
    weave.ingest(python_session)
    weave.ingest(go_session)
    weave.consolidate(policy="recency")

    with weave.store.transaction() as tx:
        facts = {f.props["object"]: f for f in tx.match(S.FACT, {"predicate": "prefers_language"})}
        assert set(facts) == {"python", "go"}
        # The superseded fact is still present, still carries its evidence, and
        # is now bounded in time rather than deleted.
        assert facts["python"].props["is_current"] is False
        assert facts["python"].get("valid_until") is not None
        assert facts["python"].props["evidence"]
        assert facts["go"].props["is_current"] is True
        # Read with .get(): a null property is stored as null by the embedded
        # engine and removed entirely by OpenCypher. See the contract note in
        # graph/store.py.
        assert facts["go"].get("valid_until") is None


def test_recency_policy_selects_the_latest(weave, python_session, go_session):
    weave.ingest(python_session)
    weave.ingest(go_session)
    report = weave.consolidate(policy="recency")

    assert report.conflicts_resolved == 1
    assert report.facts_superseded == 1
    assert report.resolutions[0].winner == "go"

    with weave.store.transaction() as tx:
        current = tx.match(S.FACT, {"predicate": "prefers_language", "is_current": True})
        assert [f.props["object"] for f in current] == ["go"]


def test_frequency_policy_selects_the_most_supported(weave):
    # Python is stated in two sessions, Go in one.
    weave.ingest(session_payload("a", 1, "2025-01-01T10:00:00", "I prefer Python for pipelines."))
    weave.ingest(session_payload("b", 2, "2025-02-01T10:00:00", "I prefer Python for pipelines."))
    weave.ingest(session_payload("c", 3, "2025-03-01T10:00:00", "I prefer Go for pipelines."))

    report = weave.consolidate(policy="frequency")
    assert report.resolutions[0].winner == "python"


def test_supersedes_edge_points_from_winner_to_loser(weave, python_session, go_session):
    weave.ingest(python_session)
    weave.ingest(go_session)
    weave.consolidate(policy="recency")

    with weave.store.transaction() as tx:
        winner = tx.match(S.FACT, {"object": "go"}, limit=1)[0]
        superseded = tx.expand([winner.id], [S.SUPERSEDES], "out", target_label=S.FACT)
        assert [n.props["object"] for _, _, n in superseded] == ["python"]


def test_conflict_is_marked_resolved_with_an_audit_trail(weave, python_session, go_session):
    weave.ingest(python_session)
    weave.ingest(go_session)
    weave.consolidate(policy="recency")

    with weave.store.transaction() as tx:
        conflict = tx.match(S.CONFLICT, limit=1)[0]
        assert conflict.props["status"] == "resolved"
        assert conflict.props["resolved_at"] is not None
        assert conflict.props["resolution_policy"] == "recency"
        winner = tx.expand([conflict.id], [S.RESOLVED_TO], "out", target_label=S.FACT)
        assert [n.props["object"] for _, _, n in winner] == ["go"]


def test_multi_valued_predicates_do_not_conflict(weave):
    weave.ingest(session_payload("a", 1, "2025-01-01T10:00:00", "I like green tea."))
    result = weave.ingest(session_payload("b", 2, "2025-02-01T10:00:00", "I like pizza."))

    assert result.conflicts_detected == 0
    with weave.store.transaction() as tx:
        assert tx.count(S.CONFLICT) == 0
        current = {
            f.props["object"]
            for f in tx.match(S.FACT, {"is_current": True})
            if f.props["predicate"].startswith("likes")
        }
    assert {"green tea", "pizza"} <= current


def test_polarity_reversal_is_a_conflict(weave):
    weave.ingest(session_payload("a", 1, "2025-01-01T10:00:00", "I like coffee."))
    result = weave.ingest(session_payload("b", 2, "2025-02-01T10:00:00", "I hate coffee."))

    assert result.conflicts_detected == 1
    weave.consolidate(policy="recency")
    with weave.store.transaction() as tx:
        current = [
            f
            for f in tx.match(S.FACT, {"is_current": True, "object": "coffee"})
        ]
        assert len(current) == 1
        assert current[0].props["polarity"] == "negative"


def test_duplicate_facts_are_merged(weave):
    with weave.store.transaction() as tx:
        entity, _ = tx.merge_node(S.ENTITY, {"canonical_name": "user"}, on_create={})
        for index, stamp in enumerate(("2025-01-01T00:00:00", "2025-02-01T00:00:00")):
            fact = tx.create_node(
                [S.FACT],
                {
                    "id": f"dup{index}",
                    "subject": "user",
                    "predicate": "likes_food",
                    "object": "pizza",
                    "polarity": "positive",
                    "is_current": True,
                    "confidence": 0.7,
                    "valid_from": stamp,
                    "valid_until": None,
                    "source_sessions": [f"s{index}"],
                },
            )
            tx.create_edge(entity.id, fact.id, S.HAS_FACT, {})

    report = weave.consolidate()
    assert report.duplicates_merged == 1
    with weave.store.transaction() as tx:
        current = tx.match(S.FACT, {"object": "pizza", "is_current": True})
        assert len(current) == 1
        assert set(current[0].props["source_sessions"]) == {"s0", "s1"}


def test_unknown_policy_is_rejected(weave):
    with pytest.raises(ValueError):
        weave.consolidate(policy="vibes")
