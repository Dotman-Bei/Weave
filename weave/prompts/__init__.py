"""Prompt templates, loaded from plain text files so they can be tuned freely."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    path = PROMPT_DIR / f"{name}.txt"
    if not path.exists():
        raise FileNotFoundError(f"no prompt template named {name!r} in {PROMPT_DIR}")
    return path.read_text(encoding="utf-8")
