"""Optional episodic retrieval sidecar, backed by HydraDB.

Weave stores every utterance in its own graph, but that graph has no text
index: candidate selection falls back to scanning, which is fine for a demo
and slow on a real haystack. HydraDB is a managed context API -- ingest text,
search it, get scored chunks back -- which is exactly the missing piece.

The split is deliberate:

* **HydraDB is the index.** It answers "which utterances look relevant?".
* **Weave's graph stays the source of truth.** It owns provenance, supersession,
  conflicts and the procedural layer.

So a memory is stored under *Weave's own utterance id*, and a search result is
hydrated back into a local node before it becomes evidence. Nothing about an
answer's citations, history or conflict state depends on the sidecar -- if it
is absent or unreachable, retrieval falls back to the local scan and every
result is still correct, just slower to find.

A note on the name: the specification this project was built from describes
HydraDB as a Bolt/OpenCypher server with ``algo.SSpaths`` procedures. The real
product is a REST API and speaks neither, so the Bolt backend in
``weave/graph/hydra.py`` cannot talk to it. That backend is a genuine
OpenCypher implementation -- verified against Neo4j -- and this module is the
integration with HydraDB proper.

Enabled by setting an API key; off otherwise.

    export HYDRA_DB_API_KEY=...      # or WEAVE_SIDECAR_API_KEY
    weave sidecar-verify
"""

from __future__ import annotations

import importlib.util
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Protocol

from .config import Settings, get_settings
from .util import count_tokens

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IndexRecord:
    """One utterance, addressed by the id it has in Weave's own graph."""

    id: str
    text: str
    session_id: str = ""
    timestamp: str = ""
    speaker: str = ""


@dataclass(frozen=True)
class SidecarHit:
    id: str
    score: float
    text: str = ""


class EpisodicIndex(Protocol):
    """What retrieval needs from a text index, and nothing more."""

    name: str

    def index(self, records: Iterable[IndexRecord]) -> int:
        ...

    def search(self, query: str, limit: int = 40) -> list[SidecarHit]:
        ...


class HydraEpisodicIndex:
    """HydraDB-backed implementation of :class:`EpisodicIndex`.

    Every call is best-effort. A memory system that starts returning errors
    because an optional index is having a bad day is worse than one that
    quietly falls back to scanning, so failures are logged and swallowed.
    """

    name = "hydradb"

    def __init__(self, settings: Settings | None = None, client: Any = None) -> None:
        self.settings = settings or get_settings()
        self.database = self.settings.sidecar_database
        self.collection = self.settings.sidecar_collection
        self._client = client
        # None = not yet checked, True = ready, False = gave up this process.
        self._ready: bool | None = None
        # Instance attributes rather than call defaults so a caller (or a test)
        # can bound the wait without threading an argument through ingestion.
        self.poll_interval = 3.0
        self.ready_timeout = 180.0
        # The API rejects a request whose combined memory text exceeds 1000
        # tokens; stay under it with room for the JSON envelope.
        self.max_batch_tokens = 800

    # -- plumbing ----------------------------------------------------------

    @property
    def client(self) -> Any:
        if self._client is None:
            from hydra_db import HydraDB

            self._client = HydraDB(token=self.settings.sidecar_api_key)
        return self._client

    def ensure_database(self, timeout: float | None = None) -> bool:
        """Create the workspace and wait for it to be usable.

        Provisioning is asynchronous: ``POST /databases`` returns immediately
        and the vectorstore is spun up behind it, so ingesting straight after
        creating fails with "Database does not exist. The vectorstore
        collection has not been provisioned yet." The create call is also not
        idempotent-safe to assume -- an existing database raises -- so both the
        create and the wait have to tolerate a database that is already there.
        """
        # Tri-state on purpose. Caching only success would re-run the whole
        # wait on every subsequent ingest whenever the workspace is
        # unavailable, so one unreachable API would add minutes to each and
        # every session ingested.
        if self._ready is not None:
            return self._ready

        timeout = self.ready_timeout if timeout is None else timeout
        try:
            self.client.databases.create(database=self.database)
        except Exception as exc:  # already exists is the common, benign case
            logger.debug("hydra sidecar: database create returned %s", exc)

        deadline = time.monotonic() + timeout
        while True:
            try:
                status = self._payload(self.client.databases.status(database=self.database))
                infra = getattr(status, "infra", None)
                if infra is not None and getattr(infra, "ready_for_ingestion", False):
                    self._ready = True
                    return True
            except Exception as exc:
                logger.debug("hydra sidecar: status poll returned %s", exc)
            if time.monotonic() >= deadline:
                logger.warning(
                    "hydra sidecar: database %r not ready after %.0fs; "
                    "disabling the index for this process",
                    self.database,
                    timeout,
                )
                self._ready = False
                return False
            time.sleep(self.poll_interval)

    @staticmethod
    def _payload(envelope: Any) -> Any:
        """Unwrap the ``{success, data, error, meta}`` envelope."""
        return getattr(envelope, "data", None)

    # -- EpisodicIndex -----------------------------------------------------

    def index(self, records: Iterable[IndexRecord]) -> int:
        """Push utterances into HydraDB under their Weave ids.

        ``infer`` is left off: Weave has already done its own extraction, and
        asking HydraDB to infer a second, parallel set of facts would put two
        disagreeing semantic layers in play. Here it is a retrieval index.

        Uploads are split to stay inside the API's per-request token budget --
        a real conversation session exceeds it easily, and the whole batch is
        rejected with a 413 rather than partially accepted.
        """
        import json

        batch = [
            {
                "id": record.id,
                "text": record.text,
                "infer": False,
                "additional_metadata": {
                    "session_id": record.session_id,
                    "timestamp": record.timestamp,
                    "speaker": record.speaker,
                },
            }
            for record in records
            if (record.text or "").strip()
        ]
        if not batch:
            return 0

        if not self.ensure_database():
            return 0

        written = 0
        for chunk in self._chunks(batch):
            try:
                self.client.context.ingest(
                    database=self.database,
                    collection=self.collection,
                    type="memory",
                    memories=json.dumps(chunk),
                )
            except Exception as exc:
                logger.warning(
                    "hydra sidecar: ingest of %d record(s) failed (%s)", len(chunk), exc
                )
                continue
            written += len(chunk)
        return written

    def _chunks(self, batch: list[dict[str, Any]]) -> Iterator[list[dict[str, Any]]]:
        """Split an upload to stay under the per-request token budget.

        The budget is counted over the combined memory text, so the split is by
        estimated tokens rather than record count -- a handful of long turns
        breaches it just as easily as many short ones. A single record over
        budget is still sent on its own: rejecting it locally would silently
        drop content, and letting the API refuse it is at least visible.
        """
        current: list[dict[str, Any]] = []
        cost = 0
        for record in batch:
            record_cost = count_tokens(record["text"])
            if current and cost + record_cost > self.max_batch_tokens:
                yield current
                current, cost = [], 0
            current.append(record)
            cost += record_cost
        if current:
            yield current

    def search(self, query: str, limit: int = 40) -> list[SidecarHit]:
        if not (query or "").strip():
            return []
        try:
            envelope = self.client.query(
                database=self.database,
                collection=self.collection,
                query=query,
                type="memory",
                # "fast" keeps this on the retrieval hot path; the graph
                # reasoning that "thinking" adds is Weave's own job.
                mode="fast",
                query_by="hybrid",
                graph_context=False,
                max_results=limit,
            )
        except Exception as exc:
            logger.warning("hydra sidecar: search failed (%s); falling back", exc)
            return []

        payload = self._payload(envelope)
        chunks = getattr(payload, "chunks", None) or []
        hits: list[SidecarHit] = []
        for chunk in chunks:
            identifier = getattr(chunk, "id", None) or getattr(chunk, "chunk_uuid", None)
            if not identifier:
                continue
            hits.append(
                SidecarHit(
                    id=str(identifier),
                    score=float(getattr(chunk, "relevancy_score", 0.0) or 0.0),
                    text=str(getattr(chunk, "chunk_content", "") or ""),
                )
            )
        return hits


def describe_sidecar(settings: Settings | None = None) -> dict[str, Any]:
    """Why the HydraDB sidecar is or is not active, for the health endpoint.

    Reported rather than inferred: "off" has three quite different causes --
    disabled, no key, or the SDK missing -- and a judge or an operator looking
    at /health should not have to guess which one they are looking at. The API
    key itself is never included, only whether one is present.
    """
    settings = settings or get_settings()
    installed = importlib.util.find_spec("hydra_db") is not None
    if not settings.sidecar_enabled:
        state, reason = "off", "disabled by WEAVE_SIDECAR"
    elif not settings.sidecar_api_key:
        state, reason = "off", "no API key (set HYDRA_DB_API_KEY)"
    elif not installed:
        state, reason = "off", "hydradb-sdk not installed"
    else:
        state, reason = "active", "indexing episodic utterances"
    return {
        "state": state,
        "reason": reason,
        "sdk_installed": installed,
        "api_key_present": bool(settings.sidecar_api_key),
        "database": settings.sidecar_database,
        "collection": settings.sidecar_collection,
        "role": "episodic text index; the graph remains the source of truth",
    }


def get_sidecar(settings: Settings | None = None) -> EpisodicIndex | None:
    """The configured index, or ``None`` when the feature is off.

    Returning ``None`` rather than a no-op object is deliberate: callers then
    cannot accidentally depend on the sidecar existing.
    """
    settings = settings or get_settings()
    if not settings.sidecar_enabled or not settings.sidecar_api_key:
        return None
    if importlib.util.find_spec("hydra_db") is None:
        logger.info(
            "hydra sidecar: API key set but hydradb-sdk is not installed; "
            "pip install 'hydradb-sdk>=2,<3'"
        )
        return None
    return HydraEpisodicIndex(settings)
