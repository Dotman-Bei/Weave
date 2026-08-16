"""Runtime settings. Everything is environment-driven with working defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # --- graph backend ----------------------------------------------------
    backend: str = field(default_factory=lambda: _env("WEAVE_BACKEND", "embedded"))
    db_path: str = field(
        default_factory=lambda: _env("WEAVE_DB_PATH", str(ROOT / "data" / "weave.db"))
    )
    hydra_uri: str = field(
        default_factory=lambda: _env("WEAVE_HYDRA_URI", "neo4j://localhost:7687")
    )
    hydra_auth_token: str = field(
        default_factory=lambda: _env(
            "WEAVE_HYDRA_TOKEN", "local-development-token-32-bytes"
        )
    )
    hydra_database: str = field(default_factory=lambda: _env("WEAVE_HYDRA_DB", ""))
    # Ingestion writes use causal consistency; benchmark reads that must see the
    # latest consolidation use strong (specification section 8.3).
    hydra_consistency: str = field(
        default_factory=lambda: _env("WEAVE_HYDRA_CONSISTENCY", "causal")
    )

    # --- HydraDB retrieval sidecar ---------------------------------------
    #
    # Distinct from the Bolt settings above, and deliberately so: those
    # configure the OpenCypher *graph backend*, while these configure the
    # HydraDB REST API used as an episodic text index. They are different
    # products despite the shared name -- see weave/sidecar.py.
    #
    # HYDRA_DB_API_KEY is the variable HydraDB's own docs use, so it is
    # honoured as a fallback.
    sidecar_api_key: str = field(
        default_factory=lambda: _env("WEAVE_SIDECAR_API_KEY", "")
        or _env("HYDRA_DB_API_KEY", "")
    )
    sidecar_database: str = field(
        default_factory=lambda: _env("WEAVE_SIDECAR_DB", "weave")
    )
    sidecar_collection: str = field(
        default_factory=lambda: _env("WEAVE_SIDECAR_COLLECTION", "default")
    )
    sidecar_enabled: bool = field(
        default_factory=lambda: _env("WEAVE_SIDECAR", "auto").lower()
        not in ("off", "0", "false", "no")
    )

    # --- access control ---------------------------------------------------
    # Empty means no authentication, which is the right default for a
    # loopback-only dev server. Set it before binding to a public interface:
    # every request then needs the token as a header, a `?k=` query parameter,
    # or the cookie the query parameter sets.
    access_token: str = field(default_factory=lambda: _env("WEAVE_ACCESS_TOKEN", ""))

    # --- extraction / generation -----------------------------------------
    # "auto" uses the LLM when a key is present and falls back to the
    # deterministic rule-based extractor otherwise.
    # Extraction and answering are both LLM-capable but priced very
    # differently: extraction runs once per ingested turn, answering once per
    # query. On a 500-question benchmark with ~50 sessions each that is ~25,000
    # extraction calls against ~500 answer calls, so they get separate
    # switches. "auto" follows the API key; "off" forces the rule-based path.
    llm_extraction: str = field(
        default_factory=lambda: _env("WEAVE_LLM_EXTRACTION", "auto")
    )

    llm_provider: str = field(default_factory=lambda: _env("WEAVE_LLM_PROVIDER", "auto"))
    anthropic_api_key: str = field(
        default_factory=lambda: _env("ANTHROPIC_API_KEY", "")
    )
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY", ""))
    llm_model: str = field(
        default_factory=lambda: _env("WEAVE_LLM_MODEL", "claude-opus-5")
    )
    llm_timeout_s: float = field(
        default_factory=lambda: _env_float("WEAVE_LLM_TIMEOUT", 30.0)
    )

    # --- embeddings (specification §4.1) ----------------------------------
    # "auto" uses the static embedding model when it is installed and falls
    # back to lexical scoring otherwise; "off" disables it outright.
    embeddings: str = field(default_factory=lambda: _env("WEAVE_EMBEDDINGS", "auto"))
    embedding_model: str = field(
        default_factory=lambda: _env("WEAVE_EMBEDDING_MODEL", "minishlab/potion-base-8M")
    )
    # Cosine below the floor contributes nothing; at the ceiling it counts as
    # full topical grounding. Keeps the abstention threshold calibrated against
    # the same 0-1 scale that lexical overlap uses.
    #
    # 0.40 is measured, not guessed: on the benchmark the highest similarity
    # any *unanswerable* question reaches is 0.340 ("what is my favourite
    # season?" against a stored favourite colour), while the synonym case that
    # must be answered sits at 0.473. The floor is the midpoint. Loosening it
    # trades abstention precision for recall on paraphrased questions, and it
    # should be re-measured against a real dataset rather than inherited.
    embedding_floor: float = field(
        default_factory=lambda: _env_float("WEAVE_EMBEDDING_FLOOR", 0.40)
    )
    embedding_ceiling: float = field(
        default_factory=lambda: _env_float("WEAVE_EMBEDDING_CEILING", 0.70)
    )
    embedding_weight: float = field(
        default_factory=lambda: _env_float("WEAVE_EMBEDDING_WEIGHT", 0.5)
    )

    # --- retrieval / abstention ------------------------------------------
    abstention_threshold: float = field(
        default_factory=lambda: _env_float("WEAVE_ABSTENTION_THRESHOLD", 0.3)
    )
    max_context_tokens: int = field(
        default_factory=lambda: _env_int("WEAVE_MAX_CONTEXT_TOKENS", 6000)
    )
    max_utterance_chars: int = field(
        default_factory=lambda: _env_int("WEAVE_MAX_UTTERANCE_CHARS", 600)
    )
    default_resolution_policy: str = field(
        default_factory=lambda: _env("WEAVE_RESOLUTION_POLICY", "recency")
    )
    auto_consolidate: bool = field(
        default_factory=lambda: _env_bool("WEAVE_AUTO_CONSOLIDATE", True)
    )

    # --- procedural learning ---------------------------------------------
    # Epsilon-greedy exploration rate over retrieval paths.
    exploration_rate: float = field(
        default_factory=lambda: _env_float("WEAVE_EXPLORATION_RATE", 0.15)
    )
    min_outcomes_to_trust: int = field(
        default_factory=lambda: _env_int("WEAVE_MIN_OUTCOMES", 3)
    )

    @property
    def has_llm(self) -> bool:
        if self.llm_provider == "none":
            return False
        return bool(self.anthropic_api_key or self.openai_api_key)

    def describe(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "db_path": self.db_path if self.backend == "embedded" else self.hydra_uri,
            "llm": self.llm_model if self.has_llm else "rule-based (no API key)",
            "abstention_threshold": self.abstention_threshold,
            "max_context_tokens": self.max_context_tokens,
            "resolution_policy": self.default_resolution_policy,
            # The token itself is never reported, only whether one is required.
            "auth": "token" if self.access_token else "open",
            "embeddings": self.embeddings,
        }


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Test hook: force settings to be re-read from the environment."""
    global _settings
    _settings = None
