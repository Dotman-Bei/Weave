"""A cold serverless instance must not greet its first visitor with an empty graph.

Vercel keeps the embedded database on an ephemeral, per-instance disk, so
without autoseed every cold start abstains on every question -- correct
behaviour on an empty memory, indistinguishable from a broken deployment.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from weave.config import reset_settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A fresh app over a database that has never been written to."""

    def build(**env: str) -> TestClient:
        monkeypatch.setenv("WEAVE_DB_PATH", str(tmp_path / "seed.db"))
        monkeypatch.setenv("WEAVE_EMBEDDINGS", "off")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        reset_settings()
        import weave.api as api
        import weave.db as db

        db._store = None  # a new database per test, not the module-level one
        api._autoseed_done = False
        return TestClient(api.app)

    yield build
    reset_settings()


def test_empty_graph_seeds_itself_when_enabled(client):
    stats = client(WEAVE_AUTOSEED="1").get("/stats").json()

    assert stats["by_label"]["Session"] == 8
    assert stats["edges"] > 0


def test_a_seeded_instance_answers_instead_of_abstaining(client):
    c = client(WEAVE_AUTOSEED="1")

    answered = c.post("/query", json={"query": "What database do I use?"}).json()
    assert not answered["abstained"]
    assert "postgresql" in answered["answer"].lower()

    # Autoseed must not cost the system its abstention: a question the corpus
    # never discusses still returns nothing, at zero tokens.
    refused = c.post("/query", json={"query": "What is my blood type?"}).json()
    assert refused["abstained"]
    assert refused["tokens_used"] == 0


def test_off_by_default(client):
    stats = client().get("/stats").json()

    assert stats["by_label"].get("Session", 0) == 0
