"""The HydraDB retrieval sidecar.

No API key is needed here. The stub mirrors the shapes the real SDK returns --
an envelope with a ``data`` payload holding ``chunks`` -- so the mapping and
the failure handling are exercised without a network call. Only the live
round-trip (``weave sidecar-verify``) needs credentials.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from weave.config import Settings
from weave.sidecar import HydraEpisodicIndex, IndexRecord, get_sidecar


# --- stub SDK ---------------------------------------------------------------


@dataclass
class _Chunk:
    id: str
    chunk_content: str = ""
    relevancy_score: float = 0.0


@dataclass
class _Payload:
    chunks: list[_Chunk] = field(default_factory=list)


@dataclass
class _Envelope:
    data: Any


@dataclass
class _Infra:
    ready_for_ingestion: bool = True


@dataclass
class _Status:
    infra: _Infra = field(default_factory=_Infra)


class _Databases:
    def __init__(self, log: list, ready: bool = True) -> None:
        self.log = log
        self.ready = ready

    def create(self, **kwargs: Any) -> None:
        self.log.append(("create", kwargs))

    def status(self, **kwargs: Any) -> _Envelope:
        self.log.append(("status", kwargs))
        return _Envelope(data=_Status(infra=_Infra(ready_for_ingestion=self.ready)))


class _Context:
    def __init__(self, log: list) -> None:
        self.log = log

    def ingest(self, **kwargs: Any) -> None:
        self.log.append(("ingest", kwargs))


class StubClient:
    """Enough of the SDK surface for the sidecar to talk to."""

    def __init__(self, chunks: list[_Chunk] | None = None, ready: bool = True) -> None:
        self.log: list = []
        self.databases = _Databases(self.log, ready=ready)
        self.context = _Context(self.log)
        self._chunks = chunks or []

    def query(self, **kwargs: Any) -> _Envelope:
        self.log.append(("query", kwargs))
        return _Envelope(data=_Payload(chunks=self._chunks))


class ExplodingClient(StubClient):
    def query(self, **kwargs: Any) -> _Envelope:
        raise RuntimeError("hydra is down")


def _index(client: Any) -> HydraEpisodicIndex:
    settings = Settings()
    settings.sidecar_api_key = "test-key"
    settings.sidecar_database = "weave-test"
    settings.sidecar_collection = "default"
    index = HydraEpisodicIndex(settings, client=client)
    index.poll_interval = 0.0
    index.ready_timeout = 0.05
    return index


# --- configuration ----------------------------------------------------------


def test_sidecar_is_off_without_a_key():
    """The feature must be opt-in: no key, no sidecar, no behaviour change."""
    settings = Settings()
    settings.sidecar_api_key = ""
    assert get_sidecar(settings) is None


def test_sidecar_is_off_when_explicitly_disabled():
    settings = Settings()
    settings.sidecar_api_key = "test-key"
    settings.sidecar_enabled = False
    assert get_sidecar(settings) is None


# --- indexing ---------------------------------------------------------------


def test_index_addresses_memories_by_weave_id():
    """The stored id must be Weave's, or a hit cannot be hydrated back."""
    client = StubClient()
    written = _index(client).index(
        [
            IndexRecord(id="utt_1", text="I moved to Lisbon.", session_id="s1"),
            IndexRecord(id="utt_2", text="   ", session_id="s1"),  # dropped
        ]
    )
    assert written == 1

    call = dict(next(payload for name, payload in client.log if name == "ingest"))
    memories = json.loads(call["memories"])
    assert [m["id"] for m in memories] == ["utt_1"]
    assert call["type"] == "memory"
    # Weave has already extracted its own facts; a second, disagreeing semantic
    # layer is exactly what this must not create.
    assert memories[0]["infer"] is False


def test_index_survives_a_failing_backend():
    class Broken(StubClient):
        def __init__(self):
            super().__init__()
            self.context = self

        def ingest(self, **kwargs: Any):
            raise RuntimeError("nope")

    assert _index(Broken()).index([IndexRecord(id="u1", text="hello")]) == 0


# --- search -----------------------------------------------------------------


def test_search_unwraps_the_envelope():
    client = StubClient(
        [
            _Chunk(id="utt_9", chunk_content="I moved to Lisbon.", relevancy_score=0.9),
            _Chunk(id="utt_3", chunk_content="I live in Berlin.", relevancy_score=0.4),
        ]
    )
    hits = _index(client).search("where do I live", limit=5)
    assert [h.id for h in hits] == ["utt_9", "utt_3"]
    assert hits[0].score == pytest.approx(0.9)


def test_search_returns_nothing_when_hydra_is_down():
    """A failing index degrades to the local scan; it must never raise."""
    assert _index(ExplodingClient()).search("anything") == []


def test_search_skips_an_empty_query():
    client = StubClient()
    assert _index(client).search("   ") == []
    assert not any(name == "query" for name, _ in client.log)


# --- integration with retrieval --------------------------------------------


def test_sidecar_hits_are_hydrated_from_the_local_graph(weave):
    """A sidecar hit becomes evidence only via the local node it names.

    This is the guarantee that makes the sidecar safe: it can influence *which*
    utterances are considered, never what they say or what they cite.
    """
    weave.ingest(
        {
            "session_id": "s1",
            "timestamp": "2025-01-10T09:00:00",
            "turns": [{"speaker": "user", "text": "I moved to Lisbon in March."}],
        }
    )
    with weave.store.transaction() as tx:
        utterance = tx.match("Utterance", limit=1)[0]

    class OneHit:
        name = "stub"

        def index(self, records):
            return 0

        def search(self, query, limit=40):
            from weave.sidecar import SidecarHit

            # A real id, plus one the graph has never heard of.
            return [SidecarHit(id=utterance.id, score=1.0), SidecarHit(id="ghost", score=1.0)]

    weave.retrieval.sidecar = OneHit()
    result = weave.query("Where did I move to?")

    texts = [e.text for e in result.evidence]
    assert any("Lisbon" in text for text in texts)
    assert all("ghost" not in e.id for e in result.evidence)


def test_unready_database_is_given_up_on_once_not_per_ingest():
    """A slow or missing workspace must not stall every future ingest.

    Provisioning is asynchronous, so ``index`` waits for it. Caching only the
    success meant an unreachable API re-ran the whole wait on every session --
    turning one outage into minutes of added latency per ingest, forever.
    """
    client = StubClient(ready=False)
    index = _index(client)

    assert index.index([IndexRecord(id="u1", text="hello")]) == 0
    polls_after_first = sum(1 for name, _ in client.log if name == "status")
    assert polls_after_first > 0

    assert index.index([IndexRecord(id="u2", text="again")]) == 0
    # Second call must not poll again.
    assert sum(1 for name, _ in client.log if name == "status") == polls_after_first


def test_ready_database_is_only_checked_once():
    client = StubClient(ready=True)
    index = _index(client)
    index.index([IndexRecord(id="u1", text="hello")])
    index.index([IndexRecord(id="u2", text="world")])
    assert sum(1 for name, _ in client.log if name == "status") == 1
    assert sum(1 for name, _ in client.log if name == "ingest") == 2


def test_uploads_are_split_to_respect_the_token_budget():
    """The API rejects a whole request over ~1000 memory tokens with a 413.

    A real conversation session breaches that easily, and the rejection is
    all-or-nothing — the first live run lost most of a haystack this way, so
    the split is by estimated tokens rather than record count.
    """
    client = StubClient()
    index = _index(client)
    index.max_batch_tokens = 20

    records = [IndexRecord(id=f"u{n}", text="word " * 15) for n in range(6)]
    written = index.index(records)

    ingests = [payload for name, payload in client.log if name == "ingest"]
    assert len(ingests) > 1, "oversized upload was not split"
    assert written == len(records)

    sent = [m["id"] for call in ingests for m in json.loads(call["memories"])]
    assert sent == [r.id for r in records], "splitting must not drop or reorder"


def test_a_failed_chunk_does_not_discard_the_rest():
    """One rejected batch must not cost the whole session's index."""

    class FlakyContext(_Context):
        def __init__(self, log):
            super().__init__(log)
            self.calls = 0

        def ingest(self, **kwargs: Any):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("413")
            self.log.append(("ingest", kwargs))

    client = StubClient()
    client.context = FlakyContext(client.log)
    index = _index(client)
    index.max_batch_tokens = 20

    records = [IndexRecord(id=f"u{n}", text="word " * 15) for n in range(4)]
    written = index.index(records)
    assert 0 < written < len(records)
