"""Benchmark dataset loading, with a synthetic fallback.

LongMemEval is not redistributable and needs a download. When it is absent the
harness generates a dataset with the *same shape* -- a long haystack of
sessions per question, evidence buried in one or two of them, plus knowledge
updates and unanswerable questions -- so the pipeline can still be measured
end to end. Reports always state which source was used.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from weave.util import canonicalize, count_tokens

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "longmemeval"

CATEGORIES = (
    "single-session-user",
    "multi-session",
    "knowledge-update",
    "temporal-reasoning",
    "abstention",
)


@dataclass
class BenchmarkSample:
    id: str
    question: str
    category: str
    sessions: list[dict[str, Any]] = field(default_factory=list)
    answer_keywords: list[str] = field(default_factory=list)
    forbidden_keywords: list[str] = field(default_factory=list)
    should_abstain: bool = False
    # predicate -> the object that should be current once the conflict for that
    # predicate has been resolved. Empty when the sample raises no conflict.
    expected_resolution: dict[str, str] = field(default_factory=dict)
    # The turns the dataset marks as holding the answer. Retrieval is measured
    # against these rather than against the answer string: LongMemEval's
    # expected answers are paraphrases ("february 14th" for a turn that says
    # "Feb 14"), so substring containment scores a perfect retrieval as a miss.
    evidence_texts: list[str] = field(default_factory=list)

    @property
    def haystack_tokens(self) -> int:
        return sum(
            count_tokens(turn.get("text", ""))
            for session in self.sessions
            for turn in session.get("turns", [])
        )


# ---------------------------------------------------------------------------
# Synthetic generator
# ---------------------------------------------------------------------------

_FILLER = [
    ("Can you summarise the release notes for me?",
     "Sure — the main change is a faster scheduler and two bug fixes."),
    ("The build is slow again today.",
     "Caching the dependency layer usually recovers most of that time."),
    ("Remind me what a p99 latency actually measures.",
     "It is the latency below which 99 percent of requests complete."),
    ("Any thoughts on the meeting agenda?",
     "Lead with the migration status, then the open incidents."),
    ("What is a good way to structure a postmortem?",
     "Timeline first, then contributing factors, then the follow-up actions."),
    ("The dashboard looks off after the deploy.",
     "Check whether the metric labels changed in the new version."),
    ("Explain backpressure briefly.",
     "It is a consumer signalling a producer to slow down before queues grow."),
    ("How should we name the new service?",
     "Something descriptive and boring tends to age best."),
]

# Questions are deliberately worded differently from the evidence sentence, so
# a run measures retrieval rather than string equality.
_TOPICS = [
    {
        "predicate": "prefers_language",
        "functional": True,
        "old": ("Python", "I prefer Python for data pipelines."),
        "new": ("Go", "I switched to Go for pipelines."),
        "question": "Which language do I prefer for pipelines?",
    },
    {
        "predicate": "lives_in_city",
        "functional": True,
        "old": ("Berlin", "I live in Berlin."),
        "new": ("Lisbon", "I moved to Lisbon."),
        "question": "Where do I live these days?",
    },
    {
        "predicate": "works_at",
        "functional": True,
        "old": ("Northwind Labs", "I work at Northwind Labs."),
        "new": ("Halcyon Systems", "I work at Halcyon Systems."),
        "question": "Who do I work for now?",
    },
    {
        "predicate": "favorite_color",
        "functional": True,
        "old": ("teal", "My favorite color is teal."),
        "new": ("amber", "My favorite color is amber."),
        "question": "What colour do I like best?",
    },
    {
        "predicate": "uses_database",
        "functional": False,
        "old": ("Postgres", "I use Postgres for analytics."),
        "new": ("ClickHouse", "I use ClickHouse for analytics."),
        "question": "Which database do I use for analytics?",
    },
]

_SIMPLE = [
    ("I am allergic to shellfish.", "Is there anything I am allergic to?", ["shellfish"]),
    ("I adopted a dog named Mira.", "What is my dog called?", ["mira"]),
    ("I really like green tea.", "Which tea do I like?", ["green tea"]),
    ("I have been learning Rust.", "What am I learning at the moment?", ["rust"]),
    ("I hate coffee.", "How do I feel about coffee?", ["coffee"]),
]

# Unanswerable questions sit deliberately close to what *is* stored: the graph
# holds preferences, a home city and an employer, so asking for a different
# attribute of the same kind is the hard case for an abstention router.
_UNANSWERABLE = [
    "What is my blood type?",
    "What is my shoe size?",
    "Which university did I attend?",
    "What is my favourite season?",
    "How tall am I?",
    "What is my partner's name?",
    "Which gym do I go to?",
    "What car do I drive?",
]

# Injected into filler sessions so the semantic layer holds competing facts
# that a naive "return the user's facts" retriever would surface by mistake.
_DISTRACTORS = [
    "I usually work from a café on Fridays.",
    "I speak Spanish reasonably well.",
    "I have a road bike I commute on.",
    "I use Vim as my editor.",
    "I like long walks after work.",
    "I run 10k on weekends.",
    "I prefer dark mode in every tool.",
    "I read a lot of science fiction.",
]


def _session(session_id: str, number: int, day: int, turns: list[tuple[str, str]]) -> dict[str, Any]:
    month = 1 + (day // 28)
    date = f"2025-{month:02d}-{1 + (day % 28):02d}T{9 + (day % 8):02d}:00:00"
    payload_turns: list[dict[str, str]] = []
    for user_text, assistant_text in turns:
        payload_turns.append({"speaker": "user", "text": user_text})
        payload_turns.append({"speaker": "assistant", "text": assistant_text})
    return {
        "session_id": session_id,
        "user_id": "bench",
        "session_number": number,
        "timestamp": date,
        "turns": payload_turns,
    }


def generate_synthetic(
    count: int = 50, sessions_per_question: int = 30, seed: int = 7
) -> list[BenchmarkSample]:
    """Build a LongMemEval-shaped dataset deterministically."""
    rng = random.Random(seed)
    samples: list[BenchmarkSample] = []

    for index in range(count):
        category = CATEGORIES[index % len(CATEGORIES)]
        # Topic must not be selected by the same modulus as the category, or
        # every sample in a category lands on one topic and the whole category
        # only ever measures a single predicate.
        topic_index = (index // len(CATEGORIES)) % len(_TOPICS)
        sample_id = f"syn-{index:04d}-{category}"
        n_sessions = sessions_per_question
        evidence_slots = sorted(rng.sample(range(n_sessions), 2))
        sessions: list[dict[str, Any]] = []

        keywords: list[str] = []
        forbidden: list[str] = []
        expected_resolution: dict[str, str] = {}
        should_abstain = False

        if category == "knowledge-update":
            topic = _TOPICS[topic_index]
            question = topic["question"]
            keywords = [topic["new"][0].lower()]
            if topic["functional"]:
                # Only a single-valued predicate supersedes; for an
                # accumulating one both values stay true, so the old value is
                # not an error and no conflict should be raised.
                forbidden = [topic["old"][0].lower()]
                expected_resolution = {
                    topic["predicate"]: canonicalize(topic["new"][0])
                }
            evidence = {
                evidence_slots[0]: topic["old"][1],
                evidence_slots[1]: topic["new"][1],
            }
        elif category == "temporal-reasoning":
            topic = _TOPICS[topic_index]
            question = f"What did I say before I changed to {topic['new'][0]}?"
            keywords = [topic["old"][0].lower()]
            if topic["functional"]:
                expected_resolution = {
                    topic["predicate"]: canonicalize(topic["new"][0])
                }
            evidence = {
                evidence_slots[0]: topic["old"][1],
                evidence_slots[1]: topic["new"][1],
            }
        elif category == "multi-session":
            first = _SIMPLE[index % len(_SIMPLE)]
            second = _SIMPLE[(index + 2) % len(_SIMPLE)]
            question = second[1]
            keywords = list(second[2])
            evidence = {evidence_slots[0]: first[0], evidence_slots[1]: second[0]}
        elif category == "abstention":
            question = _UNANSWERABLE[index % len(_UNANSWERABLE)]
            should_abstain = True
            filler = _SIMPLE[index % len(_SIMPLE)]
            evidence = {evidence_slots[0]: filler[0]}
        else:  # single-session-user
            simple = _SIMPLE[index % len(_SIMPLE)]
            question = simple[1]
            keywords = list(simple[2])
            evidence = {evidence_slots[0]: simple[0]}

        for slot in range(n_sessions):
            turns: list[tuple[str, str]] = []
            if slot in evidence:
                turns.append((evidence[slot], "Understood — I'll remember that."))
            # Roughly a third of filler sessions carry an unrelated user fact,
            # so the graph is not trivially "one fact per question".
            if slot not in evidence and rng.random() < 0.35:
                turns.append((rng.choice(_DISTRACTORS), "Noted."))
            for _ in range(rng.randint(3, 6)):
                turns.append(rng.choice(_FILLER))
            rng.shuffle(turns)
            sessions.append(
                _session(f"{sample_id}-s{slot}", slot + 1, index * 3 + slot, turns)
            )

        samples.append(
            BenchmarkSample(
                id=sample_id,
                question=question,
                category=category,
                sessions=sessions,
                answer_keywords=keywords,
                forbidden_keywords=forbidden,
                should_abstain=should_abstain,
                expected_resolution=expected_resolution,
            )
        )
    return samples


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


# --- LongMemEval release format ---------------------------------------------
#
# The official release stores a haystack as a list of *message lists*
# ([{"role", "content"}, ...]) alongside parallel date and id arrays, which is
# not the shape Weave ingests. This adapter is the only place that knows the
# difference.

_LME_DATE = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2}).*?(\d{1,2}):(\d{2})")


def _lme_timestamp(raw: str) -> str:
    """"2023/05/20 (Sat) 02:21" -> "2023-05-20T02:21:00"."""
    match = _LME_DATE.search(raw or "")
    if not match:
        return ""
    year, month, day, hour, minute = (int(part) for part in match.groups())
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:00"


def _is_longmemeval(record: dict[str, Any]) -> bool:
    sessions = record.get("haystack_sessions")
    return bool(sessions) and isinstance(sessions[0], list)


def _adapt_longmemeval(record: dict[str, Any]) -> BenchmarkSample:
    question_id = str(record.get("question_id", ""))
    raw_sessions = record.get("haystack_sessions") or []
    dates = record.get("haystack_dates") or []
    ids = record.get("haystack_session_ids") or []

    sessions: list[dict[str, Any]] = []
    for index, messages in enumerate(raw_sessions):
        turns = [
            {"speaker": message.get("role", "user"), "text": message.get("content", "")}
            for message in messages
            if (message.get("content") or "").strip()
        ]
        if not turns:
            continue
        sessions.append(
            {
                "session_id": str(ids[index]) if index < len(ids) else f"{question_id}-s{index}",
                "user_id": question_id,
                "timestamp": _lme_timestamp(dates[index]) if index < len(dates) else "",
                "turns": turns,
            }
        )

    # The release marks unanswerable questions with an "_abs" id suffix rather
    # than a distinct question_type -- they carry the type of the question they
    # were derived from, and their "answer" is prose explaining the absence.
    should_abstain = question_id.endswith("_abs")
    answer = "" if should_abstain else str(record.get("answer") or "")

    evidence_texts = [
        (message.get("content") or "").strip()
        for messages in raw_sessions
        for message in messages
        if message.get("has_answer") and (message.get("content") or "").strip()
    ]

    return BenchmarkSample(
        id=question_id,
        question=str(record.get("question", "")),
        category=str(record.get("question_type") or "unknown"),
        sessions=sessions,
        answer_keywords=[answer.lower()] if answer else [],
        should_abstain=should_abstain,
        evidence_texts=evidence_texts,
    )


def _from_records(records: Iterator[dict[str, Any]]) -> list[BenchmarkSample]:
    samples: list[BenchmarkSample] = []
    for record in records:
        if _is_longmemeval(record):
            samples.append(_adapt_longmemeval(record))
            continue
        samples.append(
            BenchmarkSample(
                id=str(record.get("question_id") or record.get("id") or len(samples)),
                question=str(record.get("question", "")),
                category=str(record.get("question_type") or record.get("category") or "unknown"),
                sessions=list(record.get("haystack_sessions") or record.get("sessions") or []),
                answer_keywords=[
                    token.lower()
                    for token in (
                        record.get("answer_keywords")
                        or ([record["answer"]] if record.get("answer") else [])
                    )
                ],
                forbidden_keywords=[
                    token.lower() for token in record.get("forbidden_keywords", [])
                ],
                should_abstain=bool(
                    record.get("should_abstain")
                    or str(record.get("question_type", "")).startswith("abstention")
                ),
                expected_resolution=dict(record.get("expected_resolution") or {}),
            )
        )
    return samples


def stratified_subset(
    samples: list[BenchmarkSample], limit: int, seed: int = 11
) -> list[BenchmarkSample]:
    """A representative slice, not the first ``limit`` rows.

    The LongMemEval release is ordered by question type, so plain slicing
    returns one category and no abstention questions at all -- a number
    measured that way says nothing about the benchmark. Strata are
    (category, should_abstain) and are filled largest-remainder, so the subset
    keeps the full set's proportions and is deterministic for a given seed.
    """
    if limit >= len(samples):
        return samples

    strata: dict[tuple[str, bool], list[BenchmarkSample]] = {}
    for sample in samples:
        strata.setdefault((sample.category, sample.should_abstain), []).append(sample)

    exact = {key: len(group) * limit / len(samples) for key, group in strata.items()}
    quota = {key: int(value) for key, value in exact.items()}
    # Hand out what rounding dropped, largest fractional part first.
    remainder = sorted(strata, key=lambda key: exact[key] - quota[key], reverse=True)
    for key in remainder[: limit - sum(quota.values())]:
        quota[key] += 1

    rng = random.Random(seed)
    chosen: list[BenchmarkSample] = []
    for key, group in strata.items():
        take = min(quota[key], len(group))
        chosen.extend(rng.sample(group, take) if take else [])

    order = {id(sample): index for index, sample in enumerate(samples)}
    chosen.sort(key=lambda sample: order[id(sample)])
    return chosen


def load_dataset(name: str = "longmemeval-s", limit: int | None = None) -> tuple[list[BenchmarkSample], str]:
    """Return ``(samples, source)``.

    Looks for a local JSON export first, then the synthetic generator. The
    source string is reported so no benchmark number is ever ambiguous about
    what it was measured on.
    """
    local = DATA_DIR / f"{name}.json"
    if local.exists():
        records = json.loads(local.read_text(encoding="utf-8"))
        samples = _from_records(iter(records))
        if samples:
            if limit:
                samples = stratified_subset(samples, limit)
            return samples, f"local:{local.name}"

    samples = generate_synthetic(count=limit or 50)
    return samples, "synthetic"
