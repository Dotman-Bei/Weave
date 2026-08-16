"""Ingestion: episodic structure, entity merge, extraction, idempotency."""

from __future__ import annotations

from weave.graph import schema as S
from weave.services.extraction import RuleBasedExtractor, is_functional

from .conftest import session_payload


def test_session_creates_episodic_chain(weave, python_session):
    result = weave.ingest(python_session)

    assert result.turns == 2
    assert result.utterances >= 2
    assert result.already_ingested is False

    with weave.store.transaction() as tx:
        assert tx.count(S.SESSION) == 1
        assert tx.count(S.TURN) == 2
        turns = tx.match(S.TURN, order_by=[("turn_number", "asc")])
        # Turns are chained both ways so chronology is walkable in either
        # direction without sorting.
        following = tx.expand([turns[0].id], [S.NEXT], "out", target_label=S.TURN)
        assert [n.id for _, _, n in following] == [turns[1].id]
        preceding = tx.expand([turns[1].id], [S.PREVIOUS], "out", target_label=S.TURN)
        assert [n.id for _, _, n in preceding] == [turns[0].id]


def test_utterances_link_to_turn_and_entities(weave, python_session):
    weave.ingest(python_session)
    with weave.store.transaction() as tx:
        utterances = tx.match(S.UTTERANCE)
        assert utterances
        for utterance in utterances:
            owners = tx.expand([utterance.id], [S.HAS_UTTERANCE], "in", target_label=S.TURN)
            assert len(owners) == 1

        python = tx.match(S.ENTITY, {"canonical_name": "python"}, limit=1)
        assert python, "the extractor should have created a python entity"
        mentions = tx.expand([python[0].id], [S.MENTIONS], "in", target_label=S.UTTERANCE)
        assert mentions


def test_facts_carry_provenance(weave, python_session):
    weave.ingest(python_session)
    with weave.store.transaction() as tx:
        facts = tx.match(S.FACT, {"predicate": "prefers_language"})
        assert len(facts) == 1
        fact = facts[0]
        assert fact.props["object"] == "python"
        assert fact.props["source_sessions"] == ["s-python"]
        assert fact.props["is_current"] is True
        assert "Python" in fact.props["evidence"]

        derived = tx.expand([fact.id], [S.DERIVED_FROM], "out")
        labels = {node.labels[0] for _, _, node in derived}
        assert S.SESSION in labels and S.UTTERANCE in labels


def test_reingesting_a_session_is_a_no_op(weave, python_session):
    weave.ingest(python_session)
    second = weave.ingest(python_session)
    assert second.already_ingested is True
    with weave.store.transaction() as tx:
        assert tx.count(S.SESSION) == 1


def test_repeated_fact_is_reinforced_not_duplicated(weave):
    first = session_payload("a", 1, "2025-01-01T10:00:00", "I prefer Python for pipelines.")
    again = session_payload("b", 2, "2025-02-01T10:00:00", "I prefer Python for pipelines.")
    weave.ingest(first)
    result = weave.ingest(again)

    assert result.facts_reinforced == 1
    assert result.conflicts_detected == 0
    with weave.store.transaction() as tx:
        facts = tx.match(S.FACT, {"predicate": "prefers_language"})
        assert len(facts) == 1
        assert set(facts[0].props["source_sessions"]) == {"a", "b"}
        assert facts[0].props["confidence"] > 0.7


def test_assistant_turns_do_not_assert_user_facts(weave):
    payload = session_payload(
        "assistant-only",
        1,
        "2025-01-01T10:00:00",
        "What should I use for pipelines?",
        "I prefer Rust for pipelines, personally.",
    )
    weave.ingest(payload)
    with weave.store.transaction() as tx:
        assert tx.match(S.FACT, {"object": "rust"}) == []


def test_multi_valued_predicate_accumulates(weave):
    weave.ingest(session_payload("t1", 1, "2025-01-01T10:00:00", "I use Postgres and Docker."))
    weave.ingest(session_payload("t2", 2, "2025-02-01T10:00:00", "I use ClickHouse."))

    with weave.store.transaction() as tx:
        objects = {
            f.props["object"]
            for f in tx.match(S.FACT, {"is_current": True})
            if f.props["predicate"].startswith("uses")
        }
    assert {"postgresql", "docker", "clickhouse"} <= objects


class TestExtractor:
    extractor = RuleBasedExtractor()

    def test_splits_clauses_and_conjuncts(self):
        facts = self.extractor.extract(
            "I live in Berlin and I work at Acme Corp.", "user"
        ).facts
        pairs = {(f.predicate, f.object) for f in facts}
        assert ("lives_in_city", "berlin") in pairs
        assert ("works_at", "acme corp") in pairs

    def test_strips_time_tails(self):
        facts = self.extractor.extract("I moved to Lisbon at the start of the month.", "user").facts
        assert facts[0].object == "lisbon"

    def test_negation_is_captured(self):
        facts = self.extractor.extract("I hate coffee.", "user").facts
        assert facts[0].polarity == "negative"
        assert facts[0].object == "coffee"

    def test_qualifier_does_not_fragment_the_predicate(self):
        a = self.extractor.extract("I prefer Python for data pipelines.", "user").facts[0]
        b = self.extractor.extract("I switched to Go for pipelines.", "user").facts[0]
        assert a.predicate == b.predicate == "prefers_language"
        assert a.qualifier and b.qualifier

    def test_questions_assert_nothing(self):
        assert self.extractor.extract("Do you think I should use Go?", "user").facts == []

    def test_predicate_arity(self):
        assert is_functional("prefers_language") is True
        assert is_functional("lives_in_city") is True
        assert is_functional("likes_beverage") is False
        assert is_functional("uses_tool") is False


def test_special_token_text_does_not_crash_ingestion(weave):
    """Real conversation text may contain tiktoken's special-token literals.

    Found on the LongMemEval haystack: tiktoken raises on "<|endoftext|>" by
    default, so a user pasting it would have crashed ingestion rather than
    being counted as ordinary text.
    """
    result = weave.ingest(
        {
            "session_id": "special",
            "timestamp": "2025-01-01T09:00:00",
            "turns": [
                {"speaker": "user", "text": "Then it printed <|endoftext|> and stopped."}
            ],
        }
    )
    assert result.utterances == 1
