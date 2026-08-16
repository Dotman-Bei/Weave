"""LLM access for extraction, query classification and answer generation.

Optional by design: when no API key (or no ``anthropic`` package) is present,
``get_llm`` returns ``None`` and every caller falls back to its deterministic
path. Weave runs end to end either way -- the LLM raises extraction recall, it
is not load-bearing.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .config import Settings, get_settings

log = logging.getLogger("weave.llm")

_JSON_BLOCK = re.compile(r"\{.*\}|\[.*\]", re.DOTALL)


class LLMUnavailable(RuntimeError):
    """Raised when a completion cannot be produced."""


class AnthropicLLM:
    """Thin wrapper over the Anthropic Messages API."""

    def __init__(self, settings: Settings) -> None:
        import anthropic  # imported lazily; optional dependency

        self._anthropic = anthropic
        self.settings = settings
        self.model = settings.llm_model
        self.client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key or None,
            timeout=settings.llm_timeout_s,
        )

    # -- core ---------------------------------------------------------------

    def _create(
        self,
        prompt: str,
        system: str | None,
        max_tokens: int,
        effort: str,
        schema: dict[str, Any] | None = None,
    ) -> str:
        output_config: dict[str, Any] = {"effort": effort}
        if schema is not None:
            # Structured outputs: the response is guaranteed to satisfy the
            # schema, so extraction never has to repair malformed JSON.
            output_config["format"] = {"type": "json_schema", "schema": schema}

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "output_config": output_config,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system

        response = self.client.messages.create(**kwargs)

        # A safety decline returns HTTP 200 with an empty/partial content list.
        if getattr(response, "stop_reason", None) == "refusal":
            raise LLMUnavailable("request was declined by the model's safeguards")

        return "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        ).strip()

    # -- public -------------------------------------------------------------

    def complete(
        self,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 2048,
        effort: str = "medium",
    ) -> str:
        return self._create(prompt, system, max_tokens, effort)

    def complete_json(
        self,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 4096,
        effort: str = "low",
        schema: dict[str, Any] | None = None,
    ) -> Any:
        raw = self._create(prompt, system, max_tokens, effort, schema=schema)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            match = _JSON_BLOCK.search(raw)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
        raise LLMUnavailable("model did not return parseable JSON")


_llm: AnthropicLLM | None = None
_llm_checked = False


def get_llm(settings: Settings | None = None) -> AnthropicLLM | None:
    """Return a configured LLM client, or ``None`` when unavailable."""
    global _llm, _llm_checked
    settings = settings or get_settings()
    if not settings.has_llm:
        return None
    if _llm_checked:
        return _llm
    _llm_checked = True
    try:
        _llm = AnthropicLLM(settings)
    except Exception as exc:  # pragma: no cover - depends on environment
        log.warning("LLM unavailable, falling back to rule-based paths: %s", exc)
        _llm = None
    return _llm


def reset_llm() -> None:
    """Test hook."""
    global _llm, _llm_checked
    _llm = None
    _llm_checked = False
