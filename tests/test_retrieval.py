"""Query classification, procedural routing, and answer construction."""

from __future__ import annotations

from weave.services.retrieval import classify_query, content_tokens, lexical_overlap

from .conftest import session_payload


def test_classification_of_each_query_type(settings):
    assert classify_query("When did I switch to Go?", settings)[0] == "temporal"
    assert classify_query("What language do I prefer?", settings)[0] == "preference"
    assert classify_query("How do I deploy the worker?", settings)[0] == "procedural"
    assert classify_query("Who is my employer?", settings)[0] == "factual"


def test_lexical_overlap_handles_inflection():
    assert lexical_overlap(["live"], "user lives in lisbon") == 1.0
    assert lexical_overlap(["prefer"], "user prefers go") == 1.0
    assert lexical_overlap(["color"], "user works at acme") == 0.0
    assert "the" not in content_tokens("What is the color?")


def test_routing_uses_the_default_path_per_query_type(weave, python_session):
    weave.ingest(python_session)
    temporal = weave.query("When did I first mention Berlin?", explore=False)
    assert temporal.query_type == "temporal"
    assert temporal.retrieval_path == "episodic-depth-3"

    preference = weave.query("What language do I prefer?", explore=False)
    assert preference.query_type == "preference"
    assert preference.retrieval_path == "hybrid-conflict"


def test_forced_path_overrides_routing(weave, python_session):
    weave.ingest(python_session)
    result = weave.query("What language do I prefer?", force_path="semantic-only")
    assert result.retrieval_path == "semantic-only"
    assert result.path_reason.startswith("forced")


def test_current_fact_wins_over_superseded_one(weave, python_session, go_session):
    weave.ingest(python_session)
    weave.ingest(go_session)
    weave.consolidate(policy="recency")

    result = weave.query("What language do I prefer for pipelines?", explore=False)
    assert result.abstained is False
    assert "go" in result.answer.lower()
    assert "python" not in result.answer.lower()


def test_temporal_query_surfaces_the_dated_utterance(weave, python_session, go_session):
    weave.ingest(python_session)
    weave.ingest(go_session)
    weave.consolidate()

    result = weave.query("When did I switch to Go?", explore=False)
    assert result.query_type == "temporal"
    assert "episodic" in result.layers_touched
    assert "switched to go" in result.answer.lower()


def test_multi_valued_answer_lists_every_current_value(weave):
    weave.ingest(session_payload("a", 1, "2025-01-01T10:00:00", "I use Postgres and Docker."))
    weave.ingest(session_payload("b", 2, "2025-02-01T10:00:00", "I use ClickHouse."))

    result = weave.query("What tools do I use?", explore=False)
    answer = result.answer.lower()
    assert "postgresql" in answer and "clickhouse" in answer


def test_spelling_variants_reach_the_same_predicate(weave):
    """A fact stored in British spelling must answer an American-spelled question.

    Without normalisation these are two different predicates
    (favourite_colour vs favorite_color) and the question abstains.
    """
    weave.ingest(
        session_payload("a", 1, "2025-01-01T10:00:00", "My favourite colour is teal.")
    )
    result = weave.query("What is my favorite color?", explore=False)
    assert result.abstained is False
    assert "teal" in result.answer.lower()


def test_history_uses_chronology_when_nothing_was_superseded(weave):
    """"Before X" on a multi-valued predicate has no SUPERSEDES edge to walk.

    Both values stay current, so the answer has to come from the ordering of
    the facts themselves.
    """
    weave.ingest(
        session_payload("a", 1, "2025-01-01T10:00:00", "I use Postgres for analytics.")
    )
    weave.ingest(
        session_payload("b", 2, "2025-06-01T10:00:00", "I use ClickHouse for analytics.")
    )
    result = weave.query(
        "What did I use before I changed to ClickHouse?", explore=False
    )
    assert result.prefer_history is True
    assert "postgresql" in result.answer.lower()


def test_answers_cite_their_sessions(weave, python_session):
    weave.ingest(python_session)
    result = weave.query("Where do I live?", explore=False)
    assert "s-python" in result.answer


def test_context_stays_within_the_token_budget(weave, python_session, go_session):
    weave.ingest(python_session)
    weave.ingest(go_session)
    result = weave.query("What language do I prefer?", max_tokens=40, explore=False)
    assert result.tokens_used <= 40


def test_evidence_is_returned_for_inspection(weave, python_session):
    weave.ingest(python_session)
    result = weave.query("What language do I prefer?", explore=False)
    assert result.facts_used
    top = result.facts_used[0]
    assert {"predicate", "object", "is_current", "confidence"} <= set(top)


def test_outcomes_train_the_procedural_layer(weave, python_session, settings):
    weave.ingest(python_session)
    for _ in range(settings.min_outcomes_to_trust + 1):
        result = weave.query("What language do I prefer?", explore=False)
        weave.log_outcome(result, success=True)

    table = {row["query_type"]: row for row in weave.routing_table()}
    preference = table["preference"]
    assert preference["paths"], "an outcome should have been recorded"
    assert preference["paths"][0]["attempts"] >= settings.min_outcomes_to_trust
    assert preference["paths"][0]["success_rate"] > 0.5

    # With enough successful outcomes the choice is now learned, not defaulted.
    assert weave.query("What language do I prefer?", explore=False).path_reason == "learned"


def test_prune_keeps_prior_values_the_graph_links(weave):
    """Evidence pruning must not drop the answer to a "before X?" question.

    A prior value shares no wording with a question that names its replacement,
    so it survives on graph grounding alone -- either a SUPERSEDES edge or the
    same subject+predicate slot. This is a regression guard: pruning on text
    similarity alone silently turned these answers into a recital of the
    utterance that announced the change.
    """
    weave.ingest(
        {
            "session_id": "s1",
            "timestamp": "2025-01-10T09:00:00",
            "turns": [{"speaker": "user", "text": "I use Postgres for analytics."}],
        }
    )
    weave.ingest(
        {
            "session_id": "s2",
            "timestamp": "2025-06-10T09:00:00",
            "turns": [{"speaker": "user", "text": "I use ClickHouse for analytics."}],
        }
    )
    weave.consolidate()

    # Deliberately phrased so the prior value overlaps the query on *nothing*:
    # "use" would itself match "user uses postgresql", masking the bug.
    result = weave.query("What did I say before I changed to ClickHouse?")
    assert not result.abstained
    assert "postgres" in result.answer.lower()

    prior = next(
        e for e in result.evidence
        if e.kind == "fact" and "postgres" in e.text.lower()
    )
    # It cannot have matched on wording -- "postgres" is absent from the query.
    assert prior.lexical == 0
    assert prior.matched_by == "graph"


def test_prune_drops_evidence_that_matched_nothing(weave):
    """Unrelated utterances the traversal walked past must not reach the context."""
    weave.ingest(
        {
            "session_id": "s1",
            "timestamp": "2025-01-10T09:00:00",
            "turns": [
                {"speaker": "user", "text": "I live in Berlin."},
                {"speaker": "user", "text": "I have a road bike I commute on."},
                {"speaker": "user", "text": "I prefer dark mode in every tool."},
            ],
        }
    )
    weave.consolidate()

    result = weave.query("Where do I live?")
    assert not result.abstained
    texts = " ".join(e.text.lower() for e in result.evidence)
    assert "berlin" in texts
    assert "road bike" not in texts
    assert "dark mode" not in texts
    # The full retrieval is still reported, so the pruned list never reads as
    # the whole subgraph.
    assert result.retrieved_count >= len(result.evidence)


def test_facts_are_not_crowded_out_by_matching_utterances(weave):
    """A flood of episodic matches must not evict the semantic layer.

    Capping evidence globally let twenty excerpts that share a word with the
    question push every fact out of the result, which broke "before X?"
    answers: the superseded fact they walk back to was never in the list.
    """
    turns = [{"speaker": "user", "text": "I use Postgres for analytics."}]
    # Bulk filler that all matches the word "analytics".
    turns += [
        {"speaker": "user", "text": f"Some analytics note number {n} for the report."}
        for n in range(40)
    ]
    weave.ingest({"session_id": "s1", "timestamp": "2025-01-10T09:00:00", "turns": turns})
    weave.ingest(
        {
            "session_id": "s2",
            "timestamp": "2025-06-10T09:00:00",
            "turns": [{"speaker": "user", "text": "I use ClickHouse for analytics."}],
        }
    )
    weave.consolidate()

    result = weave.query("What did I say before I changed to ClickHouse?")
    facts = [e for e in result.evidence if e.kind == "fact"]
    assert facts, "the semantic layer was entirely crowded out"
    assert "postgres" in result.answer.lower()
