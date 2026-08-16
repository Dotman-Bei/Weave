"""The §4.1 embedding field: semantic fallback, and its abstention guard.

Two properties are load-bearing and both are asserted here:

1. A question worded in synonyms reaches the right fact -- the thing lexical
   overlap cannot do.
2. Turning embeddings on does *not* start answering unanswerable questions.
   Vector similarity is soft, so without a floor an unrelated question drifts
   over the abstention threshold on noise.
"""

from __future__ import annotations

import pytest

from weave.client import Weave
from weave.embeddings import cosine, get_embedder, reset_embedder
from weave.graph import schema as S
from weave.services.retrieval import semantic_grounding

from .conftest import session_payload


def _require_embeddings(settings):
    reset_embedder()
    if get_embedder(settings) is None:
        pytest.skip("embedding model not installed")


def test_cosine_of_identical_and_orthogonal_vectors():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([], [1.0]) == 0.0  # mismatched or absent vectors are inert


def test_grounding_floor_suppresses_weak_similarity(settings):
    # Below the floor a match contributes nothing at all; this is what keeps a
    # vaguely-related question from clearing the abstention threshold.
    assert semantic_grounding(settings.embedding_floor - 0.01, settings) == 0.0
    assert semantic_grounding(settings.embedding_ceiling + 0.5, settings) == 1.0
    midpoint = (settings.embedding_floor + settings.embedding_ceiling) / 2
    assert 0.0 < semantic_grounding(midpoint, settings) < 1.0


def test_vectors_are_stored_on_utterances_and_facts(weave, settings):
    _require_embeddings(settings)
    weave.ingest(
        session_payload("a", 1, "2025-01-01T10:00:00", "My favourite colour is teal.")
    )
    with weave.store.transaction() as tx:
        facts = tx.match(S.FACT)
        utterances = tx.match(S.UTTERANCE)
        assert facts and utterances
        assert len(facts[0].props["embedding"]) > 0
        assert len(utterances[0].props["embedding"]) > 0


def test_synonym_question_reaches_the_right_fact(weave, settings):
    """"like best" means "favourite" -- there is no shared word to match on."""
    _require_embeddings(settings)
    weave.ingest(
        session_payload("a", 1, "2025-01-01T10:00:00", "My favourite colour is teal.")
    )
    weave.ingest(
        session_payload("b", 2, "2025-02-01T10:00:00", "I like long walks after work.")
    )

    result = weave.query("What colour do I like best?", explore=False)
    assert result.abstained is False
    assert "teal" in result.answer.lower()
    assert "walks" not in result.answer.lower()


def test_embeddings_do_not_leak_answers_to_unanswerable_questions(weave, settings):
    """Regression guard: a road bike is *semantically* near a car.

    With too low a similarity floor this question gets answered from the bike
    fact instead of abstaining.
    """
    _require_embeddings(settings)
    weave.ingest(
        session_payload("a", 1, "2025-01-01T10:00:00", "I have a road bike I commute on.")
    )
    weave.ingest(
        session_payload("b", 2, "2025-02-01T10:00:00", "My favourite colour is teal.")
    )

    for question in (
        "What car do I drive?",
        "What is my blood type?",
        "Which gym do I go to?",
    ):
        result = weave.query(question, explore=False)
        assert result.abstained is True, f"leaked an answer for {question!r}"
        assert result.tokens_used == 0


@pytest.mark.xfail(
    reason=(
        "Known limitation. 'What is my favourite season?' shares the word "
        "'favourite' with a stored favourite colour and scores 0.5 lexically, "
        "which clears the grounding threshold even though the embedding puts "
        "the two well apart (cos 0.34, below the floor). Vetoing a lexical hit "
        "the embedding does not corroborate was tried and reverted: it also "
        "halves legitimate matches whose subject is too short for the vector "
        "to confirm ('...before I changed to Go?'), trading one coincidental "
        "answer for three false abstentions. Recorded rather than hidden."
    ),
    strict=False,
)
def test_near_miss_attribute_of_the_same_kind_abstains(weave, settings):
    _require_embeddings(settings)
    weave.ingest(
        session_payload("a", 1, "2025-01-01T10:00:00", "My favourite colour is teal.")
    )
    result = weave.query("What is my favourite season?", explore=False)
    assert result.abstained is True


def test_pipeline_is_unchanged_when_embeddings_are_disabled(store, settings):
    """The zero-dependency promise: no model, no behaviour change."""
    settings.embeddings = "off"
    weave = Weave(store=store, settings=settings)

    weave.ingest(
        session_payload("a", 1, "2025-01-01T10:00:00", "I live in Berlin.")
    )
    result = weave.query("Where do I live?", explore=False)
    assert result.abstained is False
    assert "berlin" in result.answer.lower()

    with weave.store.transaction() as tx:
        assert all(not fact.props.get("embedding") for fact in tx.match(S.FACT))
