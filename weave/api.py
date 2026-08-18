"""Weave HTTP API.

Endpoints follow the build specification (`/ingest`, `/query`, `/consolidate`,
`/health`) plus the read models the workspace UI needs to show the graph, the
fact timeline and what the procedural layer has learned.
"""

from __future__ import annotations

import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import get_settings
from .sidecar import describe_sidecar
from .db import HydraDBClient, get_store
from .graph import schema as S
from .models.episodic import Session
from .services.consolidation import ConsolidationService
from .services.procedural import ProceduralLearningService
from .services.retrieval import RetrievalService
from .services.ingestion import IngestionService
from .util import human_date

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
DATA_DIR = ROOT.parent / "data" / "sample_sessions"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class TurnPayload(BaseModel):
    speaker: str = "user"
    text: str
    timestamp: str | None = None


class IngestRequest(BaseModel):
    session_id: str | None = None
    user_id: str = "default"
    timestamp: str | None = None
    session_number: int = 0
    turns: list[TurnPayload] = Field(default_factory=list)


class IngestResponse(BaseModel):
    session_id: str
    turns: int
    utterances: int
    entities_extracted: int
    facts_created: int
    facts_reinforced: int
    conflicts_detected: int
    already_ingested: bool
    latency_ms: int
    extraction_method: str


class QueryRequest(BaseModel):
    query: str
    user_id: str | None = None
    max_tokens: int = 4000
    retrieval_path: str | None = None
    explore: bool = True


class QueryResponse(BaseModel):
    query: str
    query_id: str
    answer: str
    abstained: bool
    abstention_reasons: list[str]
    confidence: float
    query_type: str
    retrieval_path: str
    path_reason: str
    entities: list[str]
    facts_used: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    # Size of the traversal before evidence that matched nothing was pruned.
    retrieved_count: int
    context: str
    tokens_used: int
    latency_ms: int
    layers_touched: list[str]
    generator: str
    abstention: dict[str, Any]


class ConsolidateRequest(BaseModel):
    user_id: str | None = None
    policy: Literal["recency", "frequency", "confidence", "trust"] | None = None
    max_conflicts: int = 50


class FeedbackRequest(BaseModel):
    query_id: str = ""
    query_type: str
    retrieval_path: str
    success: bool
    latency_ms: int = 0
    tokens_used: int = 0
    abstained: bool = False


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = get_store()
    ProceduralLearningService(store).ensure_seed()
    yield
    store.close()


app = FastAPI(
    title="Weave Memory API",
    description="Three-layer cognitive memory with cross-session fact consolidation.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

TOKEN_COOKIE = "weave_token"


@app.middleware("http")
async def require_access_token(request: Request, call_next):
    """Gate every request when an access token is configured.

    Off by default, so a loopback dev server and the test suite are unaffected.
    Set ``WEAVE_ACCESS_TOKEN`` before exposing the server on a public
    interface. A browser can present the token once as ``?k=<token>``; the
    response sets a cookie so the page's own fetches carry it from then on.
    """
    token = get_settings().access_token
    if not token:
        return await call_next(request)

    header = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    from_query = request.query_params.get("k")
    provided = (
        request.headers.get("x-weave-token")
        or header
        or from_query
        or request.cookies.get(TOKEN_COOKIE)
        or ""
    )

    if not secrets.compare_digest(str(provided), token):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)

    response = await call_next(request)
    if from_query is not None and secrets.compare_digest(str(from_query), token):
        response.set_cookie(
            TOKEN_COOKIE, token, httponly=True, samesite="lax", max_age=86400, path="/"
        )
    return response


def _services() -> tuple[IngestionService, ConsolidationService, RetrievalService]:
    store = get_store()
    settings = get_settings()
    return (
        IngestionService(store, settings),
        ConsolidationService(store, settings),
        RetrievalService(store, settings),
    )


# ---------------------------------------------------------------------------
# Core endpoints
# ---------------------------------------------------------------------------


@app.post("/ingest", response_model=IngestResponse)
def ingest_session(req: IngestRequest) -> IngestResponse:
    """Ingest a complete chat session: episodic store, extraction, merge."""
    if not req.turns:
        raise HTTPException(status_code=400, detail="a session needs at least one turn")

    ingestion, consolidation, _ = _services()
    session = Session.from_payload(req.model_dump())
    result = ingestion.process_session(session)

    if get_settings().auto_consolidate and result.conflicts_detected:
        consolidation.run_sleep_cycle(user_id=req.user_id)

    return IngestResponse(**result.to_dict())


@app.post("/query", response_model=QueryResponse)
def query_memory(req: QueryRequest) -> QueryResponse:
    """Classify, route, retrieve, decide whether to answer, then answer."""
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="query must not be empty")

    _, _, retrieval = _services()
    result = retrieval.query(
        req.query,
        user_id=req.user_id,
        max_tokens=req.max_tokens,
        force_path=req.retrieval_path,
        explore=req.explore,
    )
    payload = result.to_dict()
    return QueryResponse(**{k: payload[k] for k in QueryResponse.model_fields})


@app.post("/consolidate")
def trigger_consolidation(req: ConsolidateRequest) -> dict[str, Any]:
    """Run the background sleep cycle: resolve conflicts, merge duplicates."""
    _, consolidation, _ = _services()
    try:
        result = consolidation.run_sleep_cycle(
            user_id=req.user_id, policy=req.policy, max_conflicts=req.max_conflicts
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result.to_dict()


@app.post("/feedback")
def log_feedback(req: FeedbackRequest) -> dict[str, Any]:
    """Teach the procedural layer whether a retrieval path worked."""
    procedural = ProceduralLearningService(get_store(), get_settings())
    procedural.log_outcome(
        query_type=req.query_type,
        path_name=req.retrieval_path,
        success=req.success,
        query_id=req.query_id,
        latency_ms=req.latency_ms,
        tokens_used=req.tokens_used,
        abstained=req.abstained,
    )
    return {"status": "recorded", "query_type": req.query_type, "path": req.retrieval_path}


@app.get("/health")
def health_check() -> dict[str, Any]:
    """HydraDB connection status and per-layer statistics."""
    client = HydraDBClient(store=get_store())
    alive = client.verify()
    settings = get_settings()
    return {
        "status": "ok" if alive else "degraded",
        "graph_alive": alive,
        "config": settings.describe(),
        "llm_configured": settings.has_llm,
        "hydra_sidecar": describe_sidecar(settings),
        "stats": client.stats(),
    }


# ---------------------------------------------------------------------------
# Read models for the workspace UI
# ---------------------------------------------------------------------------


@app.get("/stats")
def stats() -> dict[str, Any]:
    store = get_store()
    base = store.stats()
    with store.transaction() as tx:
        base["open_conflicts"] = tx.count(S.CONFLICT, {"status": "open"})
        base["resolved_conflicts"] = tx.count(S.CONFLICT, {"status": "resolved"})
        base["current_facts"] = tx.count(S.FACT, {"is_current": True})
        base["superseded_facts"] = tx.count(S.FACT, {"is_current": False})
    return base


@app.get("/sessions")
def list_sessions(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    store = get_store()
    with store.transaction() as tx:
        sessions = tx.match(
            S.SESSION, order_by=[("start_time", "desc")], limit=limit
        )
        rows = []
        for session in sessions:
            rows.append(
                {
                    "id": session.id,
                    "session_number": session.get("session_number", 0),
                    "user_id": session.get("user_id", "default"),
                    "start_time": session.get("start_time"),
                    "date": human_date(str(session.get("start_time", ""))),
                    "total_turns": session.get("total_turns", 0),
                    "total_tokens": session.get("total_tokens", 0),
                    "summary": session.get("session_summary", ""),
                }
            )
    return {"sessions": rows, "count": len(rows)}


@app.get("/facts")
def list_facts(
    subject: str | None = None,
    only_current: bool = False,
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    """Facts with their supersession history -- the overwrite-proof timeline."""
    store = get_store()
    where: dict[str, Any] = {}
    if subject:
        where["subject"] = subject
    if only_current:
        where["is_current"] = True

    with store.transaction() as tx:
        facts = tx.match(
            S.FACT, where or None, order_by=[("valid_from", "desc")], limit=limit
        )
        rows = []
        for fact in facts:
            supersedes = [
                other.get("object")
                for _, _, other in tx.expand(
                    [fact.id], [S.SUPERSEDES], "out", target_label=S.FACT
                )
            ]
            superseded_by = [
                other.get("object")
                for _, _, other in tx.expand(
                    [fact.id], [S.SUPERSEDES], "in", target_label=S.FACT
                )
            ]
            rows.append(
                {
                    "id": fact.id,
                    "subject": fact.get("subject"),
                    "predicate": fact.get("predicate"),
                    "object": fact.get("object"),
                    "qualifier": fact.get("qualifier", ""),
                    "polarity": fact.get("polarity", "positive"),
                    "is_current": bool(fact.get("is_current")),
                    "confidence": float(fact.get("confidence", 0.0)),
                    "valid_from": fact.get("valid_from"),
                    "valid_until": fact.get("valid_until"),
                    "date": human_date(str(fact.get("valid_from", ""))),
                    "source_sessions": list(fact.get("source_sessions", [])),
                    "evidence": fact.get("evidence", ""),
                    "extraction_method": fact.get("extraction_method", ""),
                    "supersedes": supersedes,
                    "superseded_by": superseded_by,
                }
            )
    return {"facts": rows, "count": len(rows)}


@app.get("/conflicts")
def list_conflicts(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    store = get_store()
    with store.transaction() as tx:
        conflicts = tx.match(
            S.CONFLICT, order_by=[("detected_at", "desc")], limit=limit
        )
        rows = []
        for conflict in conflicts:
            involved = [
                {
                    "id": other.id,
                    "object": other.get("object"),
                    "is_current": bool(other.get("is_current")),
                    "valid_from": other.get("valid_from"),
                    "date": human_date(str(other.get("valid_from", ""))),
                    "evidence": other.get("evidence", ""),
                    "role": edge.get("role", ""),
                }
                for _, edge, other in tx.expand(
                    [conflict.id], [S.INVOLVES], "out", target_label=S.FACT
                )
            ]
            involved.sort(key=lambda row: str(row.get("valid_from") or ""))
            winner = [
                other.id
                for _, _, other in tx.expand(
                    [conflict.id], [S.RESOLVED_TO], "out", target_label=S.FACT
                )
            ]
            rows.append(
                {
                    "id": conflict.id,
                    "conflict_type": conflict.get("conflict_type"),
                    "status": conflict.get("status"),
                    "subject": conflict.get("subject"),
                    "predicate": conflict.get("predicate"),
                    "detected_at": conflict.get("detected_at"),
                    "resolved_at": conflict.get("resolved_at"),
                    "resolution_policy": conflict.get("resolution_policy"),
                    "involved": involved,
                    "winner_id": winner[0] if winner else None,
                }
            )
    return {"conflicts": rows, "count": len(rows)}


@app.get("/procedural")
def procedural_table() -> dict[str, Any]:
    procedural = ProceduralLearningService(get_store(), get_settings())
    return {"routing": procedural.routing_table()}


@app.get("/graph")
def graph_view(
    limit: int = Query(160, ge=10, le=600),
    layer: str | None = None,
) -> dict[str, Any]:
    """A bounded slice of the graph for visualisation."""
    store = get_store()
    labels = list(S.ALL_LABELS)
    if layer == "episodic":
        labels = list(S.EPISODIC_LABELS)
    elif layer == "semantic":
        labels = list(S.SEMANTIC_LABELS)
    elif layer == "procedural":
        labels = list(S.PROCEDURAL_LABELS)

    nodes: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    per_label = max(4, limit // max(1, len(labels)))

    with store.transaction() as tx:
        for label in labels:
            for node in tx.match(label, limit=per_label):
                if node.id in node_ids:
                    continue
                node_ids.add(node.id)
                nodes.append(
                    {
                        "id": node.id,
                        "label": label,
                        "layer": S.LAYER_OF_LABEL.get(label, "semantic"),
                        "title": _node_title(label, node.props),
                        "is_current": node.props.get("is_current"),
                        "status": node.props.get("status"),
                    }
                )
        edges = []
        if node_ids:
            for anchor, edge, other in tx.expand(list(node_ids), direction="out"):
                if other.id in node_ids:
                    edges.append(
                        {"source": anchor.id, "target": other.id, "type": edge.type}
                    )
    return {"nodes": nodes, "edges": edges, "counts": {"nodes": len(nodes), "edges": len(edges)}}


def _node_title(label: str, props: dict[str, Any]) -> str:
    if label == S.FACT:
        return f"{props.get('predicate', '')}: {props.get('object', '')}"
    if label == S.ENTITY:
        return str(props.get("canonical_name", ""))
    if label == S.SESSION:
        number = props.get("session_number")
        return f"session {number}" if number else human_date(str(props.get("start_time", "")))
    if label == S.TURN:
        return f"{props.get('speaker', '')} #{props.get('turn_number', 0)}"
    if label == S.UTTERANCE:
        return str(props.get("text", ""))[:60]
    if label == S.CONFLICT:
        return f"{props.get('predicate', '')} ({props.get('status', '')})"
    if label in (S.QUERY_TYPE, S.RETRIEVAL_PATH):
        return str(props.get("name", ""))
    if label == S.OUTCOME:
        return f"{props.get('query_type', '')} {'ok' if props.get('success') else 'fail'}"
    return str(props.get("id", ""))[:24]


# ---------------------------------------------------------------------------
# Demo helpers
# ---------------------------------------------------------------------------


@app.post("/demo/seed")
def seed_demo() -> dict[str, Any]:
    """Load the bundled sample sessions and consolidate them."""
    ingestion, consolidation, _ = _services()
    files = sorted(DATA_DIR.glob("*.json"))
    if not files:
        raise HTTPException(status_code=404, detail=f"no sample sessions in {DATA_DIR}")

    ingested = []
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for session_payload in payload if isinstance(payload, list) else [payload]:
            result = ingestion.process_session(Session.from_payload(session_payload))
            ingested.append(result.to_dict())

    consolidated = consolidation.run_sleep_cycle()
    return {"ingested": ingested, "consolidation": consolidated.to_dict()}


@app.post("/reset")
def reset_graph() -> dict[str, Any]:
    """Drop every node and edge, then re-seed the procedural layer."""
    store = get_store()
    store.reset()
    ProceduralLearningService(store, get_settings()).ensure_seed()
    return {"status": "reset", "stats": store.stats()}


# ---------------------------------------------------------------------------
# Static workspace
# ---------------------------------------------------------------------------

if WEB_DIR.exists():

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        """The landing page: what Weave is, no live data required."""
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/workspace", include_in_schema=False)
    def workspace() -> FileResponse:
        """The app itself -- everything that needs a populated graph."""
        return FileResponse(WEB_DIR / "workspace.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
