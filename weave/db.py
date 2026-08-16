"""Graph store resolution and the connection facade.

``HydraDBClient`` is the entry point named in the build specification; it wraps
whichever backend is configured so callers never import a backend directly.
"""

from __future__ import annotations

from typing import Any

from .config import Settings, get_settings
from .graph.embedded import EmbeddedGraphStore
from .graph.store import GraphStore


def build_store(settings: Settings | None = None) -> GraphStore:
    """Construct the configured backend."""
    settings = settings or get_settings()
    if settings.backend == "hydra":
        from .graph.hydra import HydraGraphStore

        return HydraGraphStore(
            uri=settings.hydra_uri,
            auth_token=settings.hydra_auth_token,
            database=settings.hydra_database or None,
            consistency=settings.hydra_consistency,
        )
    if settings.backend == "embedded":
        return EmbeddedGraphStore(settings.db_path)
    raise ValueError(
        f"unknown WEAVE_BACKEND {settings.backend!r}; expected 'embedded' or 'hydra'"
    )


class HydraDBClient:
    """Facade over the graph substrate."""

    def __init__(self, store: GraphStore | None = None, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.store = store or build_store(self.settings)

    def verify(self) -> bool:
        """Round-trip write to confirm the graph is alive."""
        try:
            return self.store.verify()
        except Exception:
            return False

    def ensure_schema(self) -> list[str]:
        return self.store.ensure_schema()

    def stats(self) -> dict[str, Any]:
        return self.store.stats()

    def reset(self) -> None:
        self.store.reset()

    def close(self) -> None:
        self.store.close()


_store: GraphStore | None = None


def get_store() -> GraphStore:
    """Process-wide graph store singleton."""
    global _store
    if _store is None:
        _store = build_store()
        _store.ensure_schema()
    return _store


def set_store(store: GraphStore | None) -> None:
    """Test hook: swap in an in-memory store."""
    global _store
    _store = store
