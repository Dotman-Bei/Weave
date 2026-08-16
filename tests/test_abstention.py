"""The abstention router -- refusing to answer is a feature, not a failure."""

from __future__ import annotations

from .conftest import session_payload


def test_abstains_on_a_topic_never_mentioned(weave, python_session):
    weave.ingest(python_session)
    result = weave.query("What is my favorite color?", explore=False)

    assert result.abstained is True
    assert "don't know" in result.answer.lower()
    assert result.abstention_reasons


def test_abstention_costs_no_context_tokens(weave, python_session):
    weave.ingest(python_session)
    result = weave.query("What is my blood type?", explore=False)

    # The whole point: decide before assembling context or calling an LLM.
    assert result.abstained is True
    assert result.tokens_used == 0
    assert result.context == ""
    assert result.generator == "abstained"


def test_abstains_on_an_empty_graph(weave):
    result = weave.query("What language do I prefer?", explore=False)
    assert result.abstained is True
    assert result.confidence < 0.5


def test_answers_when_the_fact_is_present(weave, python_session):
    weave.ingest(python_session)
    result = weave.query("What language do I prefer?", explore=False)

    assert result.abstained is False
    assert result.abstention_reasons == []
    assert result.confidence > 0.5
    assert result.tokens_used > 0


def test_related_but_absent_topic_still_abstains(weave):
    """The user has technology facts, but nothing about a phone."""
    weave.ingest(session_payload("a", 1, "2025-01-01T10:00:00", "I use Postgres and Docker."))
    result = weave.query("What phone do I own?", explore=False)
    assert result.abstained is True


def test_abstention_signals_are_reported(weave, python_session):
    weave.ingest(python_session)
    result = weave.query("What is my shoe size?", explore=False)

    signals = result.abstention["signals"]
    assert signals["topical_overlap"] == 0.0
    assert result.abstention["threshold"] == weave.settings.abstention_threshold
    assert result.abstention["score"] < result.abstention["threshold"]


def test_threshold_is_configurable(weave, python_session):
    weave.ingest(python_session)
    weave.settings.abstention_threshold = -5.0  # answer no matter what
    result = weave.query("What is my favorite color?", explore=False)
    assert result.abstained is False


def test_only_superseded_facts_lowers_confidence(weave, python_session, go_session):
    weave.ingest(python_session)
    weave.ingest(go_session)
    weave.consolidate(policy="recency")

    answered = weave.query("What language do I prefer?", explore=False)
    assert answered.abstained is False
    assert answered.abstention["signals"]["has_current_facts"] is True


def test_open_conflicts_are_flagged_in_the_signals(weave, python_session, go_session):
    weave.ingest(python_session)
    weave.ingest(go_session)
    # Deliberately skip consolidation so the conflict is still open.
    result = weave.query("What language do I prefer?", explore=False)
    assert result.abstention["signals"]["open_conflicts"] >= 1


def test_uncovered_term_signal_ignores_question_operators(weave):
    """Uncovered query words must not include the question's *operator*.

    "before" and "changed" are usually absent from stored text, so counting
    them as uncovered content would make every "what did I use before X?"
    abstain. Words that describe the shape of a question are excluded.
    """
    from weave.services.retrieval import _OPERATOR_TOKENS

    for operator in ("before", "previously", "changed", "switched", "originally"):
        assert operator in _OPERATOR_TOKENS


def test_idf_stands_down_on_a_tiny_corpus(weave):
    """Document frequency over a handful of utterances is noise, not signal."""
    from weave.services.retrieval import token_weights

    weave.ingest(
        {
            "session_id": "s1",
            "timestamp": "2025-01-10T09:00:00",
            "turns": [{"speaker": "user", "text": "I live in Berlin."}],
        }
    )
    with weave.store.transaction() as tx:
        assert token_weights(tx, ["berlin", "live"]) == {}


def test_near_miss_evidence_does_not_earn_an_answer(weave):
    """The decisive word being absent must beat a high partial overlap.

    LongMemEval's unanswerable questions are built as near-misses: the haystack
    holds vintage *cameras*, the question asks about vintage *films*. Two words
    of three match, so overlap alone says "answer this". Only the missing word
    says otherwise.
    """
    turns = [{"speaker": "user", "text": "I've been collecting vintage cameras for three months now."}]
    turns += [
        {"speaker": "user", "text": f"Note {n}: the camera collection keeps growing steadily."}
        for n in range(60)
    ]
    weave.ingest({"session_id": "s1", "timestamp": "2025-01-10T09:00:00", "turns": turns})
    weave.consolidate()

    result = weave.query("How long have I been collecting vintage films?")
    assert result.abstained, f"answered a near-miss: {result.answer!r}"
    assert result.abstention["signals"]["uncovered_query_terms"] > 0
