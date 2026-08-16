#!/usr/bin/env python3
"""Run the LongMemEval-style benchmark and, optionally, the ablation study."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from benchmarks.ablation import main as ablation_main  # noqa: E402
from benchmarks.longmemeval import main as longmemeval_main  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="longmemeval-s")
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--ablation", action="store_true", help="also run the ablation")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    shared = ["--dataset", args.dataset, "--limit", str(args.limit)]
    if args.verbose:
        shared.append("--verbose")

    code = longmemeval_main(shared + ["--output", str(args.output_dir / "longmemeval.json")])
    if code != 0:
        return code

    if args.ablation:
        print()
        code = ablation_main(shared + ["--output", str(args.output_dir / "ablation.json")])
    return code


if __name__ == "__main__":
    raise SystemExit(main())
