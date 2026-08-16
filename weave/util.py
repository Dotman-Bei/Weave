"""Small shared helpers: ids, UTC timestamps, token counting, canonicalisation."""

from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Iterable

# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex[:20]
    return f"{prefix}_{raw}" if prefix else raw


# ---------------------------------------------------------------------------
# Time
#
# Every timestamp in the graph is a UTC ISO-8601 string with a fixed layout, so
# lexicographic ordering is chronological ordering in both backends.
# ---------------------------------------------------------------------------

_ISO_FMT = "%Y-%m-%dT%H:%M:%S.%f+00:00"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime(_ISO_FMT)


def to_iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        parsed = parse_iso(value)
        return parsed.strftime(_ISO_FMT) if parsed else value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime(_ISO_FMT)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def human_date(value: str | None) -> str:
    parsed = parse_iso(value)
    return parsed.strftime("%Y-%m-%d") if parsed else "unknown date"


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

try:  # pragma: no cover - optional dependency
    import tiktoken

    _ENCODER = tiktoken.get_encoding("cl100k_base")
except Exception:  # pragma: no cover
    _ENCODER = None


def count_tokens(text: str) -> int:
    """Token count. Uses tiktoken when installed, else a calibrated estimate."""
    if not text:
        return 0
    if _ENCODER is not None:  # pragma: no cover - depends on optional install
        # Conversation text is arbitrary user input and may legitimately
        # contain the literal "<|endoftext|>". tiktoken raises on special
        # tokens by default, which would turn a stray string in someone's
        # message into an ingestion crash; count them as ordinary text.
        return len(_ENCODER.encode(text, disallowed_special=()))
    # ~4 chars/token for English prose, with a floor of one token per word.
    return max(len(text) // 4, len(text.split()))


# ---------------------------------------------------------------------------
# Canonicalisation
# ---------------------------------------------------------------------------

_PUNCT = re.compile(r"[^\w\s-]", re.UNICODE)
_SPACE = re.compile(r"\s+")

_ALIASES = {
    "i": "user",
    "me": "user",
    "my": "user",
    "myself": "user",
    "the user": "user",
    "js": "javascript",
    "ts": "typescript",
    "py": "python",
    "golang": "go",
    "postgres": "postgresql",
    "k8s": "kubernetes",
}

_ARTICLES = ("a ", "an ", "the ")

# British/American variants are normalised so that "my favourite colour" and
# "my favorite color" produce the *same* predicate, and a question asked in
# either spelling matches a fact stored in the other.
_SPELLING = {
    "colour": "color", "colours": "colors",
    "favourite": "favorite", "favourites": "favorites",
    "flavour": "flavor", "behaviour": "behavior",
    "organisation": "organization", "organise": "organize",
    "realise": "realize", "recognise": "recognize",
    "analyse": "analyze", "specialise": "specialize",
    "centre": "center", "litre": "liter", "metre": "meter",
    "defence": "defense", "licence": "license",
    "catalogue": "catalog", "dialogue": "dialog",
    "grey": "gray", "programme": "program",
}


def normalise_spelling(token: str) -> str:
    """Map a British spelling onto its American form, else return it unchanged."""
    return _SPELLING.get(token, token)


def canonicalize(name: str) -> str:
    """Normalise an entity mention to its canonical graph key."""
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", name).strip().lower()
    text = _PUNCT.sub(" ", text)
    text = _SPACE.sub(" ", text).strip()
    for article in _ARTICLES:
        if text.startswith(article):
            text = text[len(article) :]
            break
    text = " ".join(normalise_spelling(word) for word in text.split())
    text = _ALIASES.get(text, text)
    if len(text) > 3 and text.endswith("s") and not text.endswith("ss"):
        singular = text[:-1]
        text = _ALIASES.get(singular, text)
    return text.strip()


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
