"""``weave`` command line: serve, ingest, query, consolidate, benchmark."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .client import Weave
from .config import get_settings


def _load_sessions(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, list) else [payload]


def _sidecar_verify() -> int:
    """Round-trip the HydraDB sidecar: index a record, then search for it.

    Kept separate from the test suite because it is the one part that needs
    real credentials -- everything else about the sidecar is covered by stubs.
    """
    import time

    from .config import get_settings
    from .sidecar import IndexRecord, get_sidecar

    settings = get_settings()
    if not settings.sidecar_api_key:
        print(
            "No API key. Set HYDRA_DB_API_KEY (or WEAVE_SIDECAR_API_KEY) to a key\n"
            "from https://app.hydradb.com, then re-run.",
            file=sys.stderr,
        )
        return 1

    sidecar = get_sidecar(settings)
    if sidecar is None:
        print("Sidecar disabled or hydradb-sdk missing.", file=sys.stderr)
        return 1

    probe = f"weave-sidecar-probe-{int(time.time())}"
    text = f"Weave sidecar verification probe {probe}: the user moved to Lisbon."
    print(f"database={settings.sidecar_database} collection={settings.sidecar_collection}")

    written = sidecar.index([IndexRecord(id=probe, text=text, session_id="verify")])
    print(f"  indexed        {written} record(s)")
    if not written:
        print("  ingest failed — see the warning above.", file=sys.stderr)
        return 1

    # Ingestion is asynchronous, so a probe is not instantly searchable.
    for attempt in range(12):
        hits = sidecar.search("who moved to Lisbon", limit=10)
        if any(hit.id == probe for hit in hits):
            print(f"  searchable     after ~{attempt * 5}s ({len(hits)} hit(s))")
            print("\nHydraDB sidecar verified.")
            return 0
        time.sleep(5)

    print(
        "  indexed, but the probe was not searchable within 60s.\n"
        "  Indexing is asynchronous; re-run to check again.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="weave", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the API and workspace UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")

    ingest = sub.add_parser("ingest", help="ingest a session JSON file")
    ingest.add_argument("path", type=Path)
    ingest.add_argument("--consolidate", action="store_true")

    query = sub.add_parser("query", help="ask the memory a question")
    query.add_argument("text", nargs="+")
    query.add_argument("--json", action="store_true")

    consolidate = sub.add_parser("consolidate", help="run the sleep cycle")
    consolidate.add_argument(
        "--policy", default=None, choices=["recency", "frequency", "confidence", "trust"]
    )

    sub.add_parser("stats", help="print graph statistics")

    sub.add_parser(
        "sidecar-verify", help="check the HydraDB retrieval sidecar end to end"
    )

    bench = sub.add_parser("benchmark", help="run the LongMemEval-style harness")
    bench.add_argument("--limit", type=int, default=40)
    bench.add_argument("--ablation", action="store_true")
    bench.add_argument("--output", default="")

    args = parser.parse_args(argv)

    if args.command == "serve":
        import uvicorn

        settings = get_settings()
        print(f"Weave · backend={settings.backend} · http://{args.host}:{args.port}")
        uvicorn.run("weave.api:app", host=args.host, port=args.port, reload=args.reload)
        return 0

    if args.command == "sidecar-verify":
        return _sidecar_verify()

    if args.command == "benchmark":
        if args.ablation:
            from benchmarks.ablation import main as ablation_main

            return ablation_main(
                ["--limit", str(args.limit)] + (["--output", args.output] if args.output else [])
            )
        from benchmarks.longmemeval import main as bench_main

        return bench_main(
            ["--limit", str(args.limit)] + (["--output", args.output] if args.output else [])
        )

    weave = Weave()
    try:
        if args.command == "ingest":
            if not args.path.exists():
                print(f"no such file: {args.path}", file=sys.stderr)
                return 1
            for session in _load_sessions(args.path):
                result = weave.ingest(session)
                print(json.dumps(result.to_dict(), indent=2))
            if args.consolidate:
                print(json.dumps(weave.consolidate().to_dict(), indent=2))
            return 0

        if args.command == "query":
            result = weave.query(" ".join(args.text))
            if args.json:
                print(json.dumps(result.to_dict(), indent=2))
            else:
                print(result.answer)
                print(
                    f"\n  abstained={result.abstained} type={result.query_type} "
                    f"path={result.retrieval_path} confidence={result.confidence:.2f} "
                    f"tokens={result.tokens_used}"
                )
            return 0

        if args.command == "consolidate":
            print(json.dumps(weave.consolidate(policy=args.policy).to_dict(), indent=2))
            return 0

        if args.command == "stats":
            print(json.dumps(weave.stats(), indent=2))
            return 0
    finally:
        weave.close()

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
