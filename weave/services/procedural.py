"""Procedural layer -- "how to find it".

Retrieval strategies live in the graph, not in a config file, and their success
rates are learned from outcomes. Routing is epsilon-greedy: exploit the best
known path for a query type, occasionally explore an alternative so a path that
was unlucky early can recover.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from ..config import Settings, get_settings
from ..graph import schema as S
from ..graph.store import GraphStore, Node
from ..models.procedural import Outcome, QueryType, RetrievalPath
from ..util import now_iso

# --- Seed definitions -------------------------------------------------------

QUERY_TYPES: tuple[QueryType, ...] = (
    QueryType(
        name="temporal",
        description="When something happened, or what was said in a given session.",
        keywords=["when", "last time", "previously", "before", "after", "session",
                  "first", "originally", "back then", "ago", "used to"],
    ),
    QueryType(
        name="preference",
        description="What the user likes, prefers or would choose.",
        keywords=["prefer", "like", "love", "hate", "favorite", "favourite", "want",
                  "should i use", "recommend", "enjoy"],
    ),
    QueryType(
        name="factual",
        description="A stable attribute of the user or something they mentioned.",
        keywords=["what", "who", "where", "how many", "is it", "which", "name",
                  "my", "am i", "do i"],
    ),
    QueryType(
        name="procedural",
        description="How to do something, or a workflow the user described.",
        keywords=["how do i", "how to", "steps to", "process for", "workflow",
                  "set up", "configure"],
    ),
)

RETRIEVAL_PATHS: tuple[RetrievalPath, ...] = (
    RetrievalPath(
        name="semantic-only",
        layers=["semantic"],
        max_depth=1,
        description="Current facts for the queried entities, highest confidence first.",
        use_conflict_resolution=False,
        cypher_template=(
            "MATCH (e:Entity)-[:HAS_FACT]->(f:Fact) "
            "WHERE e.canonical_name IN $entities AND f.is_current = true "
            "RETURN f, e ORDER BY f.confidence DESC"
        ),
    ),
    RetrievalPath(
        name="episodic-depth-3",
        layers=["episodic"],
        max_depth=3,
        description="Entity -> utterance -> turn -> session, newest first.",
        use_conflict_resolution=False,
        cypher_template=(
            "MATCH (e:Entity)<-[:MENTIONS]-(u:Utterance)<-[:HAS_UTTERANCE]-(t:Turn)"
            "<-[:HAS_TURN]-(s:Session) "
            "WHERE e.canonical_name IN $entities "
            "RETURN s, t, u, e ORDER BY t.timestamp DESC LIMIT 20"
        ),
    ),
    RetrievalPath(
        name="hybrid-conflict",
        layers=["semantic", "episodic"],
        max_depth=2,
        description="Facts plus their supersession history and resolved conflicts.",
        use_conflict_resolution=True,
        cypher_template=(
            "MATCH (e:Entity)-[:HAS_FACT]->(f:Fact) "
            "WHERE e.canonical_name IN $entities "
            "OPTIONAL MATCH (f)-[:SUPERSEDES|CONFLICTS_WITH]-(other:Fact) "
            "OPTIONAL MATCH (f)<-[:RESOLVED_TO]-(c:Conflict) "
            "RETURN f, other, c, e ORDER BY f.is_current DESC, f.confidence DESC"
        ),
    ),
    RetrievalPath(
        name="episodic-depth-2",
        layers=["episodic"],
        max_depth=2,
        description="Shallow episodic sweep for procedural questions.",
        use_conflict_resolution=False,
        cypher_template=(
            "MATCH (e:Entity)<-[:MENTIONS]-(u:Utterance)<-[:HAS_UTTERANCE]-(t:Turn) "
            "WHERE e.canonical_name IN $entities "
            "RETURN t, u, e ORDER BY t.timestamp DESC LIMIT 15"
        ),
    ),
)

DEFAULT_PATH_FOR: dict[str, str] = {
    "temporal": "episodic-depth-3",
    "preference": "hybrid-conflict",
    "factual": "semantic-only",
    "procedural": "episodic-depth-2",
}


@dataclass
class PathChoice:
    name: str
    node: Node
    success_rate: float
    attempts: int
    reason: str  # "learned" | "default" | "exploration"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "success_rate": round(self.success_rate, 4),
            "attempts": self.attempts,
            "reason": self.reason,
            "layers": list(self.node.get("layers", [])),
            "max_depth": self.node.get("max_depth", 1),
        }


class ProceduralLearningService:
    def __init__(self, store: GraphStore, settings: Settings | None = None) -> None:
        self.store = store
        self.settings = settings or get_settings()
        self._random = random.Random()

    # -- seeding -----------------------------------------------------------

    def ensure_seed(self) -> None:
        """Create the query types and retrieval paths if they are absent."""
        with self.store.transaction() as tx:
            for query_type in QUERY_TYPES:
                tx.merge_node(
                    S.QUERY_TYPE,
                    {"name": query_type.name},
                    on_create=query_type.to_props(),
                    on_match={"description": query_type.description},
                )
            for path in RETRIEVAL_PATHS:
                tx.merge_node(
                    S.RETRIEVAL_PATH,
                    {"name": path.name},
                    on_create=path.to_props(),
                    on_match={"description": path.description},
                )

    # -- routing -----------------------------------------------------------

    def get_best_path(self, query_type: str, explore: bool = True) -> PathChoice:
        with self.store.transaction() as tx:
            type_nodes = tx.match(S.QUERY_TYPE, {"name": query_type}, limit=1)
            if not type_nodes:
                self.ensure_seed()
                type_nodes = tx.match(S.QUERY_TYPE, {"name": query_type}, limit=1)
            if not type_nodes:
                query_type = "factual"
                type_nodes = tx.match(S.QUERY_TYPE, {"name": query_type}, limit=1)

            paths = {p.get("name"): p for p in tx.match(S.RETRIEVAL_PATH)}
            if not paths:
                self.ensure_seed()
                paths = {p.get("name"): p for p in tx.match(S.RETRIEVAL_PATH)}

            learned: list[tuple[float, int, Node]] = []
            if type_nodes:
                for _, edge, path_node in tx.expand(
                    [type_nodes[0].id], [S.BEST_PATH_FOR], "out", target_label=S.RETRIEVAL_PATH
                ):
                    learned.append(
                        (
                            float(edge.get("success_rate", 0.0)),
                            int(edge.get("attempts", 0)),
                            path_node,
                        )
                    )

            trusted = [
                item for item in learned if item[1] >= self.settings.min_outcomes_to_trust
            ]

            if explore and trusted and self._random.random() < self.settings.exploration_rate:
                candidate = self._random.choice(list(paths.values()))
                return PathChoice(
                    name=str(candidate.get("name")),
                    node=candidate,
                    success_rate=0.0,
                    attempts=0,
                    reason="exploration",
                )

            if trusted:
                rate, attempts, node = max(trusted, key=lambda item: (item[0], item[1]))
                return PathChoice(
                    name=str(node.get("name")),
                    node=node,
                    success_rate=rate,
                    attempts=attempts,
                    reason="learned",
                )

            default_name = DEFAULT_PATH_FOR.get(query_type, "semantic-only")
            node = paths.get(default_name) or next(iter(paths.values()))
            known = next(
                (item for item in learned if item[2].get("name") == node.get("name")),
                None,
            )
            return PathChoice(
                name=str(node.get("name")),
                node=node,
                success_rate=known[0] if known else 0.0,
                attempts=known[1] if known else 0,
                reason="default",
            )

    # -- learning ----------------------------------------------------------

    def log_outcome(
        self,
        query_type: str,
        path_name: str,
        success: bool,
        query_id: str = "",
        latency_ms: int = 0,
        tokens_used: int = 0,
        abstained: bool = False,
    ) -> None:
        """Record an outcome and fold it into the path's success rate."""
        with self.store.transaction() as tx:
            path_nodes = tx.match(S.RETRIEVAL_PATH, {"name": path_name}, limit=1)
            type_nodes = tx.match(S.QUERY_TYPE, {"name": query_type}, limit=1)
            if not path_nodes or not type_nodes:
                return
            path_node, type_node = path_nodes[0], type_nodes[0]

            outcome = Outcome(
                query_id=query_id,
                retrieval_path_id=path_node.id,
                query_type=query_type,
                success=success,
                abstained=abstained,
                latency_ms=latency_ms,
                tokens_used=tokens_used,
            )
            outcome_node = tx.create_node([S.OUTCOME], outcome.to_props())
            tx.create_edge(path_node.id, outcome_node.id, S.TRIED, {})
            tx.create_edge(
                path_node.id,
                outcome_node.id,
                S.SUCCEEDED if success else S.FAILED,
                {},
            )

            edges = [
                (edge, other)
                for _, edge, other in tx.expand(
                    [type_node.id], [S.BEST_PATH_FOR], "out", target_label=S.RETRIEVAL_PATH
                )
                if other.id == path_node.id
            ]

            if edges:
                edge = edges[0][0]
                attempts = int(edge.get("attempts", 0)) + 1
                successes = int(edge.get("successes", 0)) + (1 if success else 0)
            else:
                attempts = 1
                successes = 1 if success else 0

            total_latency = 0
            total_tokens = 0
            if edges:
                total_latency = int(edges[0][0].get("total_latency_ms", 0))
                total_tokens = int(edges[0][0].get("total_tokens", 0))
            total_latency += latency_ms
            total_tokens += tokens_used

            # Laplace smoothing keeps a single early failure from pinning a
            # path's rate at zero forever.
            success_rate = (successes + 1) / (attempts + 2)

            tx.upsert_edge(
                type_node.id,
                path_node.id,
                S.BEST_PATH_FOR,
                {
                    "success_rate": round(success_rate, 4),
                    "attempts": attempts,
                    "successes": successes,
                    "avg_latency_ms": round(total_latency / attempts, 2),
                    "avg_token_count": round(total_tokens / attempts, 2),
                    "total_latency_ms": total_latency,
                    "total_tokens": total_tokens,
                    "last_updated": now_iso(),
                },
            )

    # -- reporting ---------------------------------------------------------

    def routing_table(self) -> list[dict[str, Any]]:
        """Everything the procedural layer has learned, for the UI and reports."""
        rows: list[dict[str, Any]] = []
        with self.store.transaction() as tx:
            for type_node in tx.match(S.QUERY_TYPE, order_by=[("name", "asc")]):
                entries = []
                for _, edge, path_node in tx.expand(
                    [type_node.id], [S.BEST_PATH_FOR], "out", target_label=S.RETRIEVAL_PATH
                ):
                    entries.append(
                        {
                            "path": path_node.get("name"),
                            "success_rate": float(edge.get("success_rate", 0.0)),
                            "attempts": int(edge.get("attempts", 0)),
                            "successes": int(edge.get("successes", 0)),
                            "avg_latency_ms": float(edge.get("avg_latency_ms", 0.0)),
                            "avg_token_count": float(edge.get("avg_token_count", 0.0)),
                            "last_updated": edge.get("last_updated"),
                        }
                    )
                entries.sort(key=lambda row: row["success_rate"], reverse=True)
                rows.append(
                    {
                        "query_type": type_node.get("name"),
                        "description": type_node.get("description"),
                        "default_path": DEFAULT_PATH_FOR.get(
                            str(type_node.get("name")), "semantic-only"
                        ),
                        "paths": entries,
                    }
                )
        return rows
