"""The retrieval baselines have to be trustworthy before Weave is compared to them.

These numbers are quoted in the README as evidence about Weave's own retrieval,
so the harness gets the same scrutiny as the system under test: a baseline that
flatters Weave by being broken is worse than no baseline at all.
"""

from __future__ import annotations

from benchmarks.baselines import (
    BaselineReport,
    BaselineResult,
    retrieve_full_context,
    retrieve_lexical_topk,
    retrieve_recency,
)
from benchmarks.dataset import BenchmarkSample


def _sample(**overrides) -> BenchmarkSample:
    """A haystack whose answer sits in the middle, far from the recent end."""
    sessions = [
        {
            "session_id": f"s-{index}",
            "turns": [{"speaker": "user", "text": text}],
        }
        for index, text in enumerate(
            [
                "The build is slow again today.",
                "Can you summarise the release notes?",
                "I adopted a tortoise called Marbles last spring.",
                "Remind me what p99 latency measures.",
                "Any thoughts on the meeting agenda?",
            ]
        )
    ]
    defaults = dict(
        id="q-1",
        question="What is my tortoise called?",
        category="single-session-user",
        sessions=sessions,
        answer_keywords=["marbles"],
        evidence_texts=["I adopted a tortoise called Marbles last spring."],
    )
    defaults.update(overrides)
    return BenchmarkSample(**defaults)


def test_full_context_returns_the_whole_haystack():
    sample = _sample()
    context = retrieve_full_context(sample, budget=10)

    # The budget is deliberately ignored: this baseline exists to show the
    # cost of not retrieving at all, so truncating it would hide the point.
    assert "Marbles" in context
    assert "meeting agenda" in context


def test_recency_takes_the_newest_turns_and_misses_older_evidence():
    sample = _sample()
    context = retrieve_recency(sample, budget=20)

    assert "meeting agenda" in context
    assert "Marbles" not in context


def test_lexical_topk_finds_evidence_a_recency_window_would_miss():
    sample = _sample()
    context = retrieve_lexical_topk(sample, budget=20)

    assert "Marbles" in context


def test_lexical_topk_has_no_null_answer():
    """The property that makes Weave's abstention worth having.

    Asked something the haystack never discusses, keyword retrieval still
    returns its best-scoring turns -- here matching on nothing but the word
    "what". IDF drags the score down; it never drags it to *nothing*. A
    retriever with no null is how a memory layer ends up answering confidently
    from an unrelated turn.
    """
    sample = _sample(question="What is my blood type?", evidence_texts=[])
    context = retrieve_lexical_topk(sample, budget=200)

    assert context != ""
    assert "blood" not in context.lower()


def test_lexical_topk_returns_nothing_when_no_term_matches_at_all():
    sample = _sample(question="Zzzz qqqq xxxx?", evidence_texts=[])
    context = retrieve_lexical_topk(sample, budget=200)

    assert context == ""


def test_budget_is_respected():
    sample = _sample()
    long_budget = retrieve_lexical_topk(sample, budget=1000)
    short_budget = retrieve_lexical_topk(sample, budget=12)

    assert len(short_budget) < len(long_budget)


def test_attempted_recall_separates_ranking_failure_from_abstention():
    """Weave's headline recall conflates two very different failures."""
    report = BaselineReport(name="weave", description="test")
    report.results = [
        # Found the evidence.
        BaselineResult("q1", "cat", True, 100, 1000, 5, abstained=False),
        # Ranked the wrong turns.
        BaselineResult("q2", "cat", False, 100, 1000, 5, abstained=False),
        # Refused to answer at all.
        BaselineResult("q3", "cat", False, 0, 1000, 5, abstained=True),
        # Unanswerable by design: excluded from both denominators.
        BaselineResult("q4", "cat", None, 0, 1000, 5, abstained=True),
    ]
    payload = report.to_dict()

    assert payload["context_recall"] == {"rate": 0.3333, "hits": 1, "graded": 3}
    attempted = payload["context_recall_when_attempted"]
    assert attempted["rate"] == 0.5
    assert attempted["graded"] == 2
    assert attempted["abstained_on_answerable"] == 1


def test_baselines_never_abstain():
    """Only Weave can abstain, so the split metric is a no-op for baselines."""
    report = BaselineReport(name="lexical-topk", description="test")
    report.results = [
        BaselineResult("q1", "cat", True, 100, 1000, 5),
        BaselineResult("q2", "cat", False, 100, 1000, 5),
    ]
    payload = report.to_dict()

    assert payload["context_recall"]["rate"] == 0.5
    assert payload["context_recall_when_attempted"]["rate"] == 0.5
    assert payload["context_recall_when_attempted"]["abstained_on_answerable"] == 0
