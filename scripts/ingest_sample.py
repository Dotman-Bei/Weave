#!/usr/bin/env python3
"""Ingest the bundled demo sessions and print what the graph learned."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from weave.client import Weave  # noqa: E402

SAMPLES = ROOT / "data" / "sample_sessions"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=SAMPLES)
    parser.add_argument("--reset", action="store_true", help="clear the graph first")
    parser.add_argument(
        "--policy", default="recency", choices=["recency", "frequency", "confidence", "trust"]
    )
    args = parser.parse_args(argv)

    files = sorted(args.path.glob("*.json")) if args.path.is_dir() else [args.path]
    if not files:
        print(f"no session files found in {args.path}", file=sys.stderr)
        return 1

    weave = Weave()
    try:
        if args.reset:
            weave.reset()

        print("Ingesting")
        for file in files:
            payload = json.loads(file.read_text(encoding="utf-8"))
            for session in payload if isinstance(payload, list) else [payload]:
                result = weave.ingest(session)
                status = "skipped (already ingested)" if result.already_ingested else (
                    f"{result.turns} turns · {result.facts_created} facts · "
                    f"{result.conflicts_detected} conflicts"
                )
                print(f"  {result.session_id:<12} {status}")

        print(f"\nConsolidating (policy={args.policy})")
        report = weave.consolidate(policy=args.policy)
        print(
            f"  {report.conflicts_resolved} conflict(s) resolved · "
            f"{report.facts_superseded} fact(s) superseded · "
            f"{report.duplicates_merged} duplicate(s) merged"
        )
        for resolution in report.resolutions:
            print(f"    {resolution.subject} {resolution.predicate} → {resolution.winner}")

        stats = weave.stats()
        print("\nGraph")
        print(f"  nodes {stats['nodes']} · edges {stats['edges']}")
        print(f"  layers {stats['by_layer']}")

        print("\nTry it")
        for question in (
            "What language do I prefer for pipelines?",
            "Where do I live?",
            "Where did I live before?",
            "What is my blood type?",
        ):
            answer = weave.query(question, explore=False)
            mark = "abstained" if answer.abstained else "answered "
            print(f"  [{mark}] {question}\n             {answer.answer}")
    finally:
        weave.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
