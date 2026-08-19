"""Retrieval baselines: is the graph earning its context budget?

Weave's headline number is that it answers from ~520 tokens instead of
~103,000. That is only interesting if the 520 tokens are *better chosen* than
the cheapest possible way of picking 520 tokens. This harness makes that
comparison directly, by running four retrievers over the identical dataset and
scoring them on the identical metric:

``full-context``
    The entire haystack. The upper bound: the gold evidence is in it by
    construction, so recall is 100% and the only interesting column is cost.

``recency``
    The most recent turns until the budget is spent -- what a truncating
    context window does, and what most agents do today.

``lexical-topk``
    Naive keyword retrieval: IDF-weighted term overlap over raw turns, no
    graph, no consolidation, no supersession. This is the honest "just do
    retrieval" baseline.

``weave``
    The full three-layer system.

Two things are measured over every arm.

*Context recall* -- did the turn the dataset marks as holding the answer reach
the assembled context -- is the half a memory layer is responsible for.

*Accuracy under a fixed reader* is the head-to-head. Comparing accuracy across
retrievers requires holding the generator constant, so every arm is scored
through the same trivial reader: **the retrieved context is submitted verbatim
as the answer**, and graded by :meth:`LongMemEvalBenchmark.evaluate` -- the
same grader the main benchmark uses. That grader demands every expected keyword
be present, no *forbidden* keyword (a superseded value) leak, and an
unanswerable question be abstained on. One generator, four retrievers: the only
variable is retrieval.

This makes the comparison possible without an API key, and it is deliberately
unflattering to Weave in one direction and to ``full-context`` in another:

* A verbatim-quote reader is far weaker than an LLM. Real accuracy for every
  arm would be higher, Weave's included -- see :mod:`benchmarks.longmemeval`
  for the shipped generator's number.
* ``full-context`` scores ~100% on *having* the answer, then loses on the two
  things stuffing cannot do: it hands the reader the superseded value alongside
  the current one, and it cannot abstain. An LLM reading the full haystack
  could recover some of both from timestamps, so this is a **lower** bound on
  what full-context could achieve, not a ceiling.

What survives both caveats is the shape of the result: retrieval alone does not
separate these systems; resolution and abstention do.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from weave.util import count_tokens

from .dataset import BenchmarkSample, load_dataset
from .longmemeval import LongMemEvalBenchmark, _overlaps, default_weave_factory

# Matched to WEAVE_MAX_CONTEXT_TOKENS' effective spend rather than its ceiling:
# Weave's measured average is ~520 tokens per query, so giving the baselines
# 600 hands them a slightly *larger* budget than the system they are being
# compared against. Any advantage from the budget accrues to the baselines.
DEFAULT_BUDGET = 600

_WORD = 3  # minimum token length that counts as a content word


@dataclass
class BaselineResult:
    question_id: str
    category: str
    context_hit: bool | None
    context_tokens: int
    haystack_tokens: int
    latency_ms: int
    # Only Weave abstains. Recorded so that its recall can be split into the
    # two failures it conflates: ranking the wrong turns, and refusing to
    # answer a question whose evidence it actually held.
    abstained: bool = False
    # Graded through the shared verbatim-quote reader (see module docstring).
    correct: bool = False
    # `answer_available` mirrors `correct` on answerable questions and is None
    # on the unanswerable ones, which is how the report tells the two halves of
    # the metric apart. `distractor_present` is what consolidation was supposed
    # to remove -- only the synthetic generator labels it.
    answer_available: bool | None = None
    distractor_present: bool | None = None


@dataclass
class BaselineReport:
    name: str
    description: str
    results: list[BaselineResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        graded = [r for r in self.results if r.context_hit is not None]
        hits = sum(1 for r in graded if r.context_hit)
        context = [r.context_tokens for r in self.results] or [0]
        haystack = [r.haystack_tokens for r in self.results] or [0]
        latency = [r.latency_ms for r in self.results] or [0]
        avg_context = statistics.mean(context)
        avg_haystack = statistics.mean(haystack)
        # Recall restricted to questions the retriever actually attempted.
        # For every baseline this is identical to the headline number; for
        # Weave it isolates ranking quality from abstention aggression.
        attempted = [r for r in graded if not r.abstained]
        attempted_hits = sum(1 for r in attempted if r.context_hit)
        correct = sum(1 for r in self.results if r.correct)
        unanswerable = [r for r in self.results if r.answer_available is None
                        and r.context_hit is None]
        unanswerable_ok = sum(1 for r in unanswerable if r.correct)
        distractors = [r for r in self.results if r.distractor_present is not None]
        distractor_hits = sum(1 for r in distractors if r.distractor_present)
        return {
            "config": self.name,
            "description": self.description,
            "total": len(self.results),
            "context_recall": {
                "rate": round(hits / len(graded), 4) if graded else None,
                "hits": hits,
                "graded": len(graded),
            },
            "accuracy": {
                "rate": round(correct / len(self.results), 4) if self.results else None,
                "correct": correct,
                "graded": len(self.results),
                "reader": "oracle",
            },
            "unanswerable": {
                "rate": (
                    round(unanswerable_ok / len(unanswerable), 4)
                    if unanswerable else None
                ),
                "correct": unanswerable_ok,
                "graded": len(unanswerable),
            },
            "distractor_present": {
                "rate": (
                    round(distractor_hits / len(distractors), 4) if distractors else None
                ),
                "hits": distractor_hits,
                "graded": len(distractors),
            },
            "context_recall_when_attempted": {
                "rate": (
                    round(attempted_hits / len(attempted), 4) if attempted else None
                ),
                "hits": attempted_hits,
                "graded": len(attempted),
                "abstained_on_answerable": len(graded) - len(attempted),
            },
            "tokens": {
                "avg_context_tokens": round(avg_context, 1),
                "avg_haystack_tokens": round(avg_haystack, 1),
                "reduction_x": (
                    round(avg_haystack / avg_context, 1) if avg_context else None
                ),
            },
            "latency_ms": {
                "mean": round(statistics.mean(latency), 2),
                "median": round(statistics.median(latency), 2),
            },
        }


# ---------------------------------------------------------------------------
# Turn access
# ---------------------------------------------------------------------------


def _turns(sample: BenchmarkSample) -> list[str]:
    """Every turn in the haystack, in chronological order."""
    return [
        turn.get("text", "")
        for session in sample.sessions
        for turn in session.get("turns", [])
        if turn.get("text")
    ]


def _content_tokens(text: str) -> list[str]:
    return [
        word
        for word in "".join(
            char.lower() if char.isalnum() else " " for char in text
        ).split()
        if len(word) >= _WORD
    ]


def _fill(turns: Iterable[str], budget: int) -> str:
    """Concatenate turns until the token budget is spent."""
    kept: list[str] = []
    spent = 0
    for text in turns:
        cost = count_tokens(text)
        if spent + cost > budget:
            continue
        kept.append(text)
        spent += cost
    return "\n".join(kept)


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------


def retrieve_full_context(sample: BenchmarkSample, budget: int) -> str:
    """Everything. The upper bound on recall and on cost."""
    return "\n".join(_turns(sample))


def retrieve_recency(sample: BenchmarkSample, budget: int) -> str:
    """The most recent turns that fit. What a truncating context window does."""
    return _fill(reversed(_turns(sample)), budget)


def retrieve_lexical_topk(sample: BenchmarkSample, budget: int) -> str:
    """IDF-weighted term overlap over raw turns. No graph, no consolidation.

    Deliberately a *fair* implementation rather than a strawman: IDF weighting
    and length normalisation are what a competent keyword baseline does, and
    beating a crippled baseline would prove nothing.
    """
    turns = _turns(sample)
    if not turns:
        return ""

    tokenised = [_content_tokens(text) for text in turns]
    document_frequency: Counter[str] = Counter()
    for tokens in tokenised:
        document_frequency.update(set(tokens))
    total = len(tokenised)

    query = set(_content_tokens(sample.question))
    scored: list[tuple[float, int]] = []
    for index, tokens in enumerate(tokenised):
        if not tokens:
            continue
        present = query & set(tokens)
        if not present:
            continue
        score = sum(
            math.log(total / (1 + document_frequency[term])) for term in present
        ) / math.sqrt(len(tokens))
        scored.append((score, index))

    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return _fill((turns[index] for _, index in scored), budget)


BASELINES = {
    "full-context": (
        retrieve_full_context,
        "the entire haystack -- upper bound on recall, and on cost",
    ),
    "recency": (
        retrieve_recency,
        "most recent turns until the budget is spent (truncating window)",
    ),
    "lexical-topk": (
        retrieve_lexical_topk,
        "IDF-weighted keyword retrieval over raw turns, no graph",
    ),
}


def _score(context: str, sample: BenchmarkSample) -> bool | None:
    """Context recall, by the same rule :mod:`benchmarks.longmemeval` uses."""
    if sample.should_abstain:
        return None
    lowered = (context or "").lower()
    if sample.evidence_texts:
        return any(_overlaps(evidence, lowered) for evidence in sample.evidence_texts)
    if not sample.answer_keywords:
        return None
    return all(keyword.lower() in lowered for keyword in sample.answer_keywords)


def _grade(
    context: str, abstained: bool, sample: BenchmarkSample
) -> tuple[bool, bool | None, bool | None]:
    """Score one retrieved context under a shared **oracle reader**.

    Returns ``(correct, answered_correctly, distractor_present)``.

    The oracle reader answers correctly whenever the gold evidence is in the
    context, and abstains exactly when the system it sits on top of tells it
    to. It is the per-arm ceiling: no real generator does better, and the
    ranking between arms is not confounded by how good any generator is.

    Grading the *literal* answer string against the context instead -- the
    obvious first design -- is not sound here, and measurably so. LongMemEval's
    expected answers are paraphrases of their evidence turns, so an arm that
    returns raw conversation scores well simply for being verbatim, while one
    that returns consolidated facts is marked wrong for saying the same thing
    in its own words. That is a property of the grader, not of the retrieval,
    and it penalises exactly what consolidation is for. The recall rule below
    is the fairness-corrected one :mod:`benchmarks.longmemeval` already uses.

    The unanswerable questions are the half ``context_recall`` cannot see: it
    returns ``None`` for them by construction. Folding them in is the whole
    point of this metric -- stuffing the entire haystack has perfect recall and
    no way at all to decline.
    """
    distractor = (
        None
        if not sample.forbidden_keywords
        else any(k.lower() in (context or "").lower() for k in sample.forbidden_keywords)
    )
    if sample.should_abstain:
        # Nothing to retrieve; the only right move is to refuse.
        return abstained, None, distractor
    hit = _score(context, sample)
    if hit is None:
        return False, None, distractor
    # Refusing a question the evidence would have answered is still a miss.
    answered = bool(hit) and not abstained
    return answered, answered, distractor


def run_baseline(
    name: str, samples: list[BenchmarkSample], budget: int
) -> BaselineReport:
    retriever, description = BASELINES[name]
    report = BaselineReport(name=name, description=description)
    for sample in samples:
        started = time.perf_counter()
        context = retriever(sample, budget)
        elapsed = int((time.perf_counter() - started) * 1000)
        # No baseline has an abstention mechanism, so every one of them answers
        # every question -- including the ones that cannot be answered.
        correct, available, distractor = _grade(context, False, sample)
        report.results.append(
            BaselineResult(
                question_id=sample.id,
                category=sample.category,
                context_hit=_score(context, sample),
                context_tokens=count_tokens(context),
                haystack_tokens=sample.haystack_tokens,
                latency_ms=elapsed,
                correct=correct,
                answer_available=available,
                distractor_present=distractor,
            )
        )
    return report


def run_weave(samples: list[BenchmarkSample]) -> BaselineReport:
    """Weave itself, over the identical samples and the identical metric."""
    report = BaselineReport(
        name="weave",
        description="the full three-layer system",
    )
    for sample in samples:
        weave = default_weave_factory()
        try:
            started = time.perf_counter()
            for session in sample.sessions:
                weave.ingest(session)
            weave.consolidate()
            response = weave.query(sample.question, explore=False)
            elapsed = int((time.perf_counter() - started) * 1000)
            # Graded on its *context*, not its generated answer, so the reader
            # is identical to the one the baselines get. Weave's shipped
            # accuracy with the real generator is a separate number.
            correct, available, distractor = _grade(
                response.context, response.abstained, sample
            )
            report.results.append(
                BaselineResult(
                    question_id=sample.id,
                    category=sample.category,
                    context_hit=_score(response.context, sample),
                    context_tokens=response.tokens_used,
                    haystack_tokens=sample.haystack_tokens,
                    latency_ms=elapsed,
                    abstained=response.abstained,
                    correct=correct,
                    answer_available=available,
                    distractor_present=distractor,
                )
            )
        finally:
            weave.close()
    return report


def format_comparison(study: dict[str, Any]) -> str:
    header = (
        f"\n  {'retriever':<16} {'accuracy':>9} {'unanswerable':>13} "
        f"{'ctx recall':>11} {'ctx tokens':>11} {'vs haystack':>12}"
    )
    lines = [header, "  " + "-" * 76]
    pct = lambda v, w: f"{v:>{w}.1%}" if v is not None else f"{'n/a':>{w}}"
    for report in study["configs"].values():
        reduction = report["tokens"]["reduction_x"]
        lines.append(
            f"  {report['config']:<16} "
            f"{pct(report['accuracy']['rate'], 9)} "
            f"{pct(report['unanswerable']['rate'], 13)} "
            f"{pct(report['context_recall']['rate'], 11)} "
            f"{report['tokens']['avg_context_tokens']:>11.0f} "
            + (f"{reduction:>11.1f}x" if reduction else f"{'n/a':>12}")
        )
    lines.append("")
    lines.append("  accuracy: oracle reader over all questions, identical across arms.")
    lines.append("  unanswerable: share of the no-answer questions correctly declined.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="longmemeval-s")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--budget",
        type=int,
        default=DEFAULT_BUDGET,
        help="token budget handed to each baseline retriever",
    )
    parser.add_argument(
        "--configs",
        default=",".join(list(BASELINES) + ["weave"]),
        help="comma-separated subset of: " + ", ".join(list(BASELINES) + ["weave"]),
    )
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)

    samples, source = load_dataset(args.dataset, limit=args.limit)
    names = [name.strip() for name in args.configs.split(",") if name.strip()]
    print(
        f"Weave · retrieval baselines · {len(samples)} questions from {source} "
        f"· budget {args.budget} tokens"
    )

    configs: dict[str, Any] = {}
    for name in names:
        print(f"\n── {name} ──")
        if name == "weave":
            report = run_weave(samples)
        elif name in BASELINES:
            report = run_baseline(name, samples, args.budget)
        else:
            raise ValueError(
                f"unknown config {name!r}; expected "
                + ", ".join(list(BASELINES) + ["weave"])
            )
        payload = report.to_dict()
        configs[name] = payload
        recall = payload["context_recall"]["rate"]
        print(
            f"  context recall {recall:.1%}  "
            f"({payload['context_recall']['hits']}/{payload['context_recall']['graded']})"
            if recall is not None
            else "  context recall n/a"
        )
        print(f"  context tokens {payload['tokens']['avg_context_tokens']:.0f}")
        acc = payload["accuracy"]
        print(f"  accuracy       {acc['rate']:.1%}  ({acc['correct']}/{acc['graded']})")

    study = {
        "dataset_source": source,
        "budget_tokens": args.budget,
        "configs": configs,
    }
    print(format_comparison(study))

    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(study, indent=2), encoding="utf-8")
        print(f"\n  written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
