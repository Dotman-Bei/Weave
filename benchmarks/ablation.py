"""Ablation study: what does each layer actually contribute?

Four configurations over the same dataset, differing in one variable each:

* ``episodic-only``    retrieval restricted to raw conversation (no facts)
* ``semantic-only``    retrieval restricted to consolidated facts (no excerpts)
* ``no-consolidation`` all layers, but conflicts are never resolved
* ``full-weave``       the complete pipeline
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .dataset import load_dataset
from .longmemeval import LongMemEvalBenchmark, format_report

CONFIGS: dict[str, dict[str, Any]] = {
    "episodic-only": {"restrict_layers": {"episodic"}, "consolidate": True},
    "semantic-only": {"restrict_layers": {"semantic"}, "consolidate": True},
    "no-consolidation": {"restrict_layers": None, "consolidate": False},
    "full-weave": {"restrict_layers": None, "consolidate": True},
}


class AblationStudy:
    def __init__(self, samples, source: str, configs: list[str] | None = None) -> None:
        self.samples = samples
        self.source = source
        self.configs = configs or list(CONFIGS)

    def run(self, verbose: bool = False) -> dict[str, Any]:
        reports: dict[str, Any] = {}
        for name in self.configs:
            if name not in CONFIGS:
                raise ValueError(f"unknown config {name!r}; expected {list(CONFIGS)}")
            options = CONFIGS[name]
            print(f"\n── {name} ──")
            benchmark = LongMemEvalBenchmark(
                self.samples,
                source=self.source,
                consolidate=options["consolidate"],
                restrict_layers=options["restrict_layers"],
                label=name,
            )
            report = benchmark.run(verbose=verbose)
            print(format_report(report))
            reports[name] = report
        return {"dataset_source": self.source, "configs": reports}


def format_comparison(study: dict[str, Any]) -> str:
    header = (
        f"\n  {'config':<18} {'accuracy':>9} {'abst. F1':>9} {'conflict':>9} "
        f"{'ctx tokens':>11} {'latency ms':>11}"
    )
    lines = [header, "  " + "-" * 71]
    for name, report in study["configs"].items():
        resolution = (report.get("conflict_resolution") or {}).get("accuracy")
        # "n/a" rather than 0% when a config never resolves conflicts at all,
        # so a disabled stage is not confused with a failing one.
        resolution_cell = f"{resolution:>8.1%}" if resolution is not None else f"{'n/a':>8}"
        lines.append(
            f"  {name:<18} {report['accuracy']:>8.1%} "
            f"{report['abstention']['f1']:>8.1%} "
            f"{resolution_cell} "
            f"{report['tokens']['avg_context_tokens']:>11.0f} "
            f"{report['latency_ms']['mean']:>11.1f}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Weave ablation study.")
    parser.add_argument("--dataset", default="longmemeval-s")
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument(
        "--configs",
        default=",".join(CONFIGS),
        help="comma-separated subset of: " + ", ".join(CONFIGS),
    )
    parser.add_argument("--output", default="")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    samples, source = load_dataset(args.dataset, limit=args.limit)
    configs = [name.strip() for name in args.configs.split(",") if name.strip()]
    print(f"Weave · ablation study · {len(samples)} questions from {source}")

    study = AblationStudy(samples, source, configs).run(verbose=args.verbose)
    print(format_comparison(study))

    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(study, indent=2), encoding="utf-8")
        print(f"\n  written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
