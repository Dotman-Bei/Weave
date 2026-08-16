from __future__ import annotations

import os

import pytest

from weave.client import Weave
from weave.config import Settings
from weave.graph.embedded import EmbeddedGraphStore


@pytest.fixture
def settings() -> Settings:
    config = Settings()
    config.backend = "embedded"
    config.db_path = ":memory:"
    config.llm_provider = "none"
    config.anthropic_api_key = ""
    config.openai_api_key = ""
    config.exploration_rate = 0.0  # deterministic routing in tests
    # The suite must be hermetic. Without this, exporting HYDRA_DB_API_KEY --
    # which anyone verifying the sidecar will do -- silently points every test
    # at the live API. The sidecar has its own tests, against a stub.
    config.sidecar_enabled = False
    config.sidecar_api_key = ""
    return config


@pytest.fixture
def store():
    """The graph substrate under test.

    Defaults to the embedded engine. Set ``WEAVE_TEST_BACKEND=hydra`` to run
    the identical suite against a live Bolt/OpenCypher server -- the whole
    point of the GraphStore contract is that neither the tests nor the services
    should be able to tell the difference.
    """
    if os.environ.get("WEAVE_TEST_BACKEND") == "hydra":
        from weave.graph.hydra import HydraGraphStore

        graph = HydraGraphStore(
            uri=os.environ.get("WEAVE_HYDRA_URI", "neo4j://localhost:7687"),
            auth_token=os.environ.get("WEAVE_HYDRA_TOKEN", ""),
        )
        graph.reset()  # a shared server needs clearing between tests
        graph.ensure_schema()
        yield graph
        graph.reset()
        graph.close()
        return

    graph = EmbeddedGraphStore(":memory:")
    yield graph
    graph.close()


@pytest.fixture
def weave(store: EmbeddedGraphStore, settings: Settings) -> Weave:
    return Weave(store=store, settings=settings)


def session_payload(session_id: str, number: int, timestamp: str, *turns: str) -> dict:
    """Build a session where turns alternate user/assistant, starting with user."""
    return {
        "session_id": session_id,
        "user_id": "tester",
        "session_number": number,
        "timestamp": timestamp,
        "turns": [
            {"speaker": "user" if index % 2 == 0 else "assistant", "text": text}
            for index, text in enumerate(turns)
        ],
    }


@pytest.fixture
def python_session() -> dict:
    return session_payload(
        "s-python",
        1,
        "2025-01-10T09:00:00",
        "I prefer Python for data pipelines. I live in Berlin.",
        "Python is a good fit for that.",
    )


@pytest.fixture
def go_session() -> dict:
    return session_payload(
        "s-go",
        2,
        "2025-06-10T09:00:00",
        "I switched to Go for pipelines.",
        "Go will speed those workers up.",
    )
