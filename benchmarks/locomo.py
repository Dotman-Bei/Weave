"""LoCoMo harness (specification §10, "if time permits").

LoCoMo is a long-conversation memory benchmark: multi-session dialogues between
two speakers, with questions spanning single-hop, multi-hop, temporal,
open-domain and adversarial (deliberately unanswerable) categories.

This module is an *adapter*, not a second benchmark. It converts the LoCoMo
release format into the same ``BenchmarkSample`` shape the LongMemEval harness
consumes, so both datasets are scored by identical code and the numbers are
comparable.

Unlike the LongMemEval harness there is **no synthetic fallback**. A generated
stand-in would measure nothing the LongMemEval one does not already measure,
and presenting fabricated data under a real benchmark's name is exactly the
kind of thing this project refuses to do elsewhere. Without the dataset the
command says so and exits.

    data/locomo/locomo10.json      <- put the release here
    python -m benchmarks.locomo
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from .dataset import BenchmarkSample
from .longmemeval import LongMemEvalBenchmark, format_report

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "locomo"
DEFAULT_FILE = DATA_DIR / "locomo10.json"

# LoCoMo labels questions with a numeric category; category 5 is the
# adversarial set, which has no answer in the conversation and must be refused.
CATEGORY_NAMES = {
    1: "multi-hop",
    2: "temporal-reasoning",
    3: "open-domain",
    4: "single-hop",
    5: "adversarial",
}
ADVERSARIAL = 5

_SESSION_KEY = re.compile(r"^session_(\d+)$")


def _sessions_from_conversation(conversation: dict[str, Any], sample_id: str) -> list[dict]:
    """Turn LoCoMo's ``session_N`` / ``session_N_date_time`` keys into sessions."""
    sessions: list[dict[str, Any]] = []
    for key, value in conversation.items():
        match = _SESSION_KEY.match(key)
        if not match or not isinstance(value, list):
            continue
        number = int(match.group(1))
        turns = []
        for entry in value:
            text = (entry.get("text") or "").strip()
            if not text:
                continue
            # LoCoMo is speaker-to-speaker. Weave attributes facts to the user,
            # so speaker_a is mapped to "user" and the other side to
            # "assistant" -- otherwise both sides' statements would be recorded
            # as the same person's.
            speaker = "user" if entry.get("speaker") == conversation.get("speaker_a") else "assistant"
            turns.append({"speaker": speaker, "text": text})
        if not turns:
            continue
        sessions.append(
            {
                "session_id": f"{sample_id}-s{number}",
                "user_id": sample_id,
                "session_number": number,
                "timestamp": conversation.get(f"{key}_date_time") or "",
                "turns": turns,
            }
        )
    sessions.sort(key=lambda s: s["session_number"])
    return sessions


def load_locomo(path: Path | None = None, limit: int | None = None) -> list[BenchmarkSample]:
    """Load the LoCoMo release and adapt it to ``BenchmarkSample``."""
    path = path or DEFAULT_FILE
    if not path.exists():
        raise FileNotFoundError(path)

    records = json.loads(path.read_text(encoding="utf-8"))
    samples: list[BenchmarkSample] = []

    for index, record in enumerate(records):
        sample_id = str(record.get("sample_id") or f"locomo-{index:03d}")
        conversation = record.get("conversation") or {}
        sessions = _sessions_from_conversation(conversation, sample_id)
        if not sessions:
            continue

        for qa_index, qa in enumerate(record.get("qa") or []):
            question = str(qa.get("question", "")).strip()
            if not question:
                continue
            category = int(qa.get("category", 0) or 0)
            answer = qa.get("answer")
            if answer is None:
                answer = qa.get("adversarial_answer")

            samples.append(
                BenchmarkSample(
                    id=f"{sample_id}-q{qa_index}",
                    question=question,
                    category=CATEGORY_NAMES.get(category, f"category-{category}"),
                    sessions=sessions,
                    # LoCoMo answers are free text; the shared scorer checks
                    # the expected string is present, matching how it treats a
                    # LongMemEval answer.
                    answer_keywords=(
                        [] if category == ADVERSARIAL else [str(answer).lower()] if answer else []
                    ),
                    should_abstain=category == ADVERSARIAL,
                )
            )
            if limit and len(samples) >= limit:
                return samples
    return samples


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the LoCoMo benchmark.")
    parser.add_argument("--path", type=Path, default=DEFAULT_FILE)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--output", default="")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    try:
        samples = load_locomo(args.path, limit=args.limit)
    except FileNotFoundError:
        print(
            f"LoCoMo dataset not found at {args.path}.\n"
            "\n"
            "It is not redistributable, so it is not bundled. Download the\n"
            "release and place locomo10.json there, then re-run.\n"
            "\n"
            "There is deliberately no synthetic stand-in: it would measure\n"
            "nothing the LongMemEval harness does not already cover, and\n"
            "reporting invented data under a real benchmark's name would be\n"
            "misleading. Run `python -m benchmarks.longmemeval` meanwhile."
        )
        return 1

    if not samples:
        print(f"No usable questions found in {args.path}.")
        return 1

    print(f"Weave · LoCoMo harness · {len(samples)} questions from {args.path.name}")
    benchmark = LongMemEvalBenchmark(samples, source=f"locomo:{args.path.name}")
    report = benchmark.run(verbose=args.verbose)
    print(format_report(report))

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\n  written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
