"""Procedural layer -- "how to find it". Query types, retrieval paths, outcomes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..util import new_id, now_iso


@dataclass
class QueryType:
    name: str
    description: str = ""
    keywords: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: new_id("qt"))

    def to_props(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "keywords": list(self.keywords),
        }


@dataclass
class RetrievalPath:
    name: str
    layers: list[str] = field(default_factory=list)
    max_depth: int = 2
    cypher_template: str = ""
    use_conflict_resolution: bool = False
    description: str = ""
    id: str = field(default_factory=lambda: new_id("path"))

    def to_props(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "layers": list(self.layers),
            "max_depth": self.max_depth,
            "cypher_template": self.cypher_template,
            "use_conflict_resolution": self.use_conflict_resolution,
            "description": self.description,
        }


@dataclass
class Outcome:
    query_id: str
    retrieval_path_id: str
    success: bool
    latency_ms: int = 0
    tokens_used: int = 0
    query_type: str = ""
    abstained: bool = False
    id: str = field(default_factory=lambda: new_id("out"))
    timestamp: str = field(default_factory=now_iso)

    def to_props(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "query_id": self.query_id,
            "retrieval_path_id": self.retrieval_path_id,
            "query_type": self.query_type,
            "success": self.success,
            "abstained": self.abstained,
            "latency_ms": self.latency_ms,
            "tokens_used": self.tokens_used,
            "timestamp": self.timestamp,
        }
