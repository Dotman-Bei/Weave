"""In-process client -- the same pipeline the HTTP API exposes.

Used by the scripts, the benchmark harnesses and the tests so none of them has
to stand up a server just to exercise the memory system.
"""

from __future__ import annotations

from typing import Any

from .config import Settings, get_settings
from .db import build_store
from .graph.store import GraphStore
from .models.episodic import Session
from .services.consolidation import ConsolidationResult, ConsolidationService
from .services.ingestion import IngestionResult, IngestionService
from .services.procedural import ProceduralLearningService
from .services.retrieval import RetrievalResult, RetrievalService


class Weave:
    """Facade over the three layers."""

    def __init__(
        self, store: GraphStore | None = None, settings: Settings | None = None
    ) -> None:
        self.settings = settings or get_settings()
        self.store = store or build_store(self.settings)
        self.store.ensure_schema()
        self.procedural = ProceduralLearningService(self.store, self.settings)
        self.procedural.ensure_seed()
        self.ingestion = IngestionService(self.store, self.settings)
        self.consolidation = ConsolidationService(self.store, self.settings)
        self.retrieval = RetrievalService(self.store, self.settings)

    # -- pipeline ----------------------------------------------------------

    def ingest(self, session: Session | dict[str, Any]) -> IngestionResult:
        if isinstance(session, dict):
            session = Session.from_payload(session)
        return self.ingestion.process_session(session)

    def ingest_all(self, sessions: list[Any]) -> list[IngestionResult]:
        return [self.ingest(session) for session in sessions]

    def consolidate(
        self, policy: str | None = None, max_conflicts: int = 200
    ) -> ConsolidationResult:
        return self.consolidation.run_sleep_cycle(
            policy=policy, max_conflicts=max_conflicts
        )

    def query(self, text: str, **kwargs: Any) -> RetrievalResult:
        return self.retrieval.query(text, **kwargs)

    def log_outcome(self, result: RetrievalResult, success: bool) -> None:
        self.retrieval.log_outcome(result, success)

    # -- inspection --------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        return self.store.stats()

    def routing_table(self) -> list[dict[str, Any]]:
        return self.procedural.routing_table()

    def reset(self) -> None:
        self.store.reset()
        self.procedural.ensure_seed()

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "Weave":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
