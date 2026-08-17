"""LongMemEval-style harness.

Reports the metrics named in the build specification: accuracy (overall and per
category), abstention precision/recall, token efficiency against a
full-context baseline, and end-to-end latency.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from weave.client import Weave
from weave.config import Settings
from weave.graph import schema as S
from weave.graph.embedded import EmbeddedGraphStore

from .dataset import BenchmarkSample, load_dataset


def _overlaps(evidence: str, context: str, span: int = 60) -> bool:
    """Whether a gold evidence turn is present in the assembled context.

    Compared on a leading span rather than in full: the context truncates long
    utterances, so requiring the whole turn would fail on evidence that is
    demonstrably there.
    """
    probe = " ".join((evidence or "").lower().split())[:span]
    return bool(probe) and probe in context


@dataclass
class SampleResult:
    question_id: str
    category: str
    question: str
    answer: str
    correct: bool
    abstained: bool
    should_abstain: bool
    retrieval_path: str
    query_type: str
    context_tokens: int
    haystack_tokens: int
    latency_ms: int
    conflicts_detected: int = 0
    conflicts_expected: int = 0
    conflicts_correct: int = 0
    # Did the gold answer reach the assembled context at all? None for
    # abstention questions, which have no gold answer to find.
    context_hit: bool | None = None
    note: str = ""


def default_weave_factory() -> Weave:
    settings = Settings()
    settings.backend = "embedded"
    settings.db_path = ":memory:"
    settings.exploration_rate = 0.0
    return Weave(store=EmbeddedGraphStore(":memory:"), settings=settings)


class LongMemEvalBenchmark:
    def __init__(
        self,
        samples: list[BenchmarkSample],
        source: str = "unknown",
        weave_factory: Callable[[], Weave] = default_weave_factory,
        consolidate: bool = True,
        restrict_layers: set[str] | None = None,
        label: str = "full-weave",
    ) -> None:
        self.samples = samples
        self.source = source
        self.weave_factory = weave_factory
        self.consolidate = consolidate
        self.restrict_layers = restrict_layers
        self.label = label

    # -- scoring -----------------------------------------------------------

    @staticmethod
    def evaluate(answer: str, abstained: bool, sample: BenchmarkSample) -> tuple[bool, str]:
        """Exact-ish match: every expected keyword present, none forbidden."""
        if sample.should_abstain:
            return (abstained, "" if abstained else "answered an unanswerable question")
        if abstained:
            return False, "abstained on an answerable question"

        lowered = answer.lower()
        missing = [k for k in sample.answer_keywords if k.lower() not in lowered]
        if missing:
            return False, f"missing expected term(s): {', '.join(missing)}"
        leaked = [k for k in sample.forbidden_keywords if k.lower() in lowered]
        if leaked:
            return False, f"returned superseded value(s): {', '.join(leaked)}"
        return True, ""

    @staticmethod
    def context_recall(context: str, sample: BenchmarkSample) -> bool | None:
        """Whether the gold answer is present in the retrieved context.

        This separates the two halves of the task. Finding the evidence in a
        100k-token haystack is the memory system's job; turning that evidence
        into the exact phrase the grader wants is the generator's. Without an
        LLM the template generator quotes evidence verbatim, so answer accuracy
        understates retrieval quality -- this metric measures the half Weave is
        actually responsible for.
        """
        if sample.should_abstain:
            return None
        lowered = (context or "").lower()

        # Prefer the dataset's own evidence turns. LongMemEval's expected
        # answers are paraphrases -- "february 14th" for a turn reading
        # "Feb 14", "the sports store downtown" for one naming the shop -- so
        # substring containment scores a *perfect* retrieval as a miss. On six
        # sampled misses, three had the answer string nowhere in the haystack
        # at all, which made the metric pessimistic by construction.
        if sample.evidence_texts:
            return any(
                _overlaps(evidence, lowered) for evidence in sample.evidence_texts
            )
        if not sample.answer_keywords:
            return None
        return all(keyword.lower() in lowered for keyword in sample.answer_keywords)

    # -- run ---------------------------------------------------------------

    def run(self, verbose: bool = False) -> dict[str, Any]:
        results: list[SampleResult] = []
        started = time.perf_counter()

        for index, sample in enumerate(self.samples, start=1):
            # Each question gets a clean graph: its haystack is its own world.
            weave = self.weave_factory()
            try:
                conflicts = 0
                for session in sample.sessions:
                    conflicts += weave.ingest(session).conflicts_detected
                if self.consolidate:
                    weave.consolidate()

                response = weave.query(
                    sample.question,
                    explore=False,
                    restrict_layers=self.restrict_layers,
                )
                correct, note = self.evaluate(
                    response.answer, response.abstained, sample
                )
                resolved_correct, resolved_expected = self._score_resolution(weave, sample)
                hit = self.context_recall(response.context, sample)
                weave.log_outcome(response, success=correct)

                results.append(
                    SampleResult(
                        question_id=sample.id,
                        category=sample.category,
                        question=sample.question,
                        answer=response.answer,
                        correct=correct,
                        abstained=response.abstained,
                        should_abstain=sample.should_abstain,
                        retrieval_path=response.retrieval_path,
                        query_type=response.query_type,
                        context_tokens=response.tokens_used,
                        haystack_tokens=sample.haystack_tokens,
                        latency_ms=response.latency_ms,
                        conflicts_detected=conflicts,
                        conflicts_expected=resolved_expected,
                        conflicts_correct=resolved_correct,
                        context_hit=hit,
                        note=note,
                    )
                )
                if verbose:
                    mark = "PASS" if correct else "FAIL"
                    print(f"  [{mark}] {sample.category:22} {sample.question[:52]}")
                    if not correct:
                        print(f"         {note} | got: {response.answer[:80]}")
            finally:
                weave.close()

        report = self.aggregate(results)
        report["wall_clock_s"] = round(time.perf_counter() - started, 2)
        return report

    @staticmethod
    def _score_resolution(weave: Weave, sample: BenchmarkSample) -> tuple[int, int]:
        """Return ``(correct, expected)`` conflict resolutions for one sample.

        ``expected`` counts the conflicts the data *should* have raised, so a
        contradiction that was never detected counts against the score rather
        than silently vanishing from the denominator.
        """
        expected = len(sample.expected_resolution)
        if not expected:
            return 0, 0

        correct = 0
        with weave.store.transaction() as tx:
            for predicate, winner in sample.expected_resolution.items():
                for conflict in tx.match(S.CONFLICT, {"predicate": predicate}):
                    won = [
                        node
                        for _, _, node in tx.expand(
                            [conflict.id], [S.RESOLVED_TO], "out", target_label=S.FACT
                        )
                    ]
                    if len(won) == 1 and str(won[0].get("object")) == winner:
                        correct += 1
                        break
        return correct, expected

    # -- aggregation -------------------------------------------------------

    def aggregate(self, results: list[SampleResult]) -> dict[str, Any]:
        total = len(results)
        if total == 0:
            return {"config": self.label, "dataset_source": self.source, "total": 0}

        correct = sum(1 for r in results if r.correct)

        abstained = [r for r in results if r.abstained]
        should = [r for r in results if r.should_abstain]
        true_abstentions = [r for r in abstained if r.should_abstain]

        precision = len(true_abstentions) / len(abstained) if abstained else 0.0
        recall = len(true_abstentions) / len(should) if should else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall)
            else 0.0
        )

        by_category: dict[str, dict[str, Any]] = {}
        for result in results:
            bucket = by_category.setdefault(
                result.category, {"total": 0, "correct": 0}
            )
            bucket["total"] += 1
            bucket["correct"] += int(result.correct)
        for bucket in by_category.values():
            bucket["accuracy"] = round(bucket["correct"] / bucket["total"], 4)

        conflicts_expected = sum(r.conflicts_expected for r in results)
        conflicts_correct = sum(r.conflicts_correct for r in results)

        graded = [r for r in results if r.context_hit is not None]
        context_hits = sum(1 for r in graded if r.context_hit)

        answered = [r for r in results if not r.abstained]
        context_tokens = [r.context_tokens for r in answered] or [0]
        haystack_tokens = [r.haystack_tokens for r in results] or [1]
        latencies = [r.latency_ms for r in results]

        avg_context = statistics.mean(context_tokens)
        avg_haystack = statistics.mean(haystack_tokens)

        return {
            "config": self.label,
            "dataset_source": self.source,
            "total": total,
            "accuracy": round(correct / total, 4),
            "correct": correct,
            # Retrieval quality on its own: did the gold answer reach the
            # context, whether or not the generator then said it?
            "context_recall": {
                "rate": round(context_hits / len(graded), 4) if graded else None,
                "hits": context_hits,
                "graded": len(graded),
            },
            "by_category": by_category,
            "abstention": {
                "precision": round(precision, 4),
                "recall": round(recall, 4),
                "f1": round(f1, 4),
                "abstained": len(abstained),
                "should_abstain": len(should),
            },
            "conflict_resolution": {
                "accuracy": (
                    round(conflicts_correct / conflicts_expected, 4)
                    if conflicts_expected
                    else None
                ),
                "correct": conflicts_correct,
                "expected": conflicts_expected,
            },
            "tokens": {
                "avg_context_tokens": round(avg_context, 1),
                "avg_haystack_tokens": round(avg_haystack, 1),
                "context_ratio": round(avg_context / avg_haystack, 5) if avg_haystack else 0.0,
                "reduction_x": round(avg_haystack / avg_context, 1) if avg_context else None,
            },
            "latency_ms": {
                "mean": round(statistics.mean(latencies), 2),
                "median": round(statistics.median(latencies), 2),
                "max": max(latencies),
            },
            "results": [asdict(r) for r in results],
        }


def format_report(report: dict[str, Any]) -> str:
    lines = [
        "",
        f"  config            {report['config']}",
        f"  dataset           {report['dataset_source']}  ({report['total']} questions)",
        f"  accuracy          {report['accuracy']:.1%}  ({report['correct']}/{report['total']})",
    ]
    recall_block = report.get("context_recall") or {}
    if recall_block.get("rate") is not None:
        lines.append(
            f"  context recall    {recall_block['rate']:.1%}  "
            f"({recall_block['hits']}/{recall_block['graded']} gold answers reached the context)"
        )
    lines.append("  per category")
    for name, bucket in sorted(report["by_category"].items()):
        lines.append(
            f"    {name:24} {bucket['accuracy']:.1%}  ({bucket['correct']}/{bucket['total']})"
        )
    abstention = report["abstention"]
    lines.append(
        f"  abstention        precision {abstention['precision']:.1%}  "
        f"recall {abstention['recall']:.1%}  f1 {abstention['f1']:.1%}"
    )
    resolution = report.get("conflict_resolution") or {}
    if resolution.get("expected"):
        lines.append(
            f"  conflict resol.   {resolution['accuracy']:.1%}  "
            f"({resolution['correct']}/{resolution['expected']} conflicts)"
        )
    tokens = report["tokens"]
    reduction = tokens["reduction_x"]
    lines.append(
        f"  context tokens    {tokens['avg_context_tokens']:.0f} vs "
        f"{tokens['avg_haystack_tokens']:.0f} full-context"
        + (f"  ({reduction}x smaller)" if reduction else "")
    )
    latency = report["latency_ms"]
    lines.append(
        f"  latency           mean {latency['mean']:.1f} ms  "
        f"median {latency['median']:.1f} ms  max {latency['max']} ms"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the LongMemEval-style benchmark.")
    parser.add_argument("--dataset", default="longmemeval-s")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--output", default="")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    samples, source = load_dataset(args.dataset, limit=args.limit)
    print(f"Weave · LongMemEval harness · {len(samples)} questions from {source}")
    benchmark = LongMemEvalBenchmark(samples, source=source)
    report = benchmark.run(verbose=args.verbose)
    print(format_report(report))

    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\n  written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
