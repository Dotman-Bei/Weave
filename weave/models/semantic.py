"""Semantic layer -- "what is true". Consolidated facts, entities, conflicts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..util import canonicalize, new_id, now_iso


@dataclass
class Entity:
    name: str
    entity_type: str = "concept"
    id: str = field(default_factory=lambda: new_id("ent"))
    canonical_name: str = ""
    first_seen: str = field(default_factory=now_iso)
    last_seen: str = field(default_factory=now_iso)
    mention_count: int = 1

    def __post_init__(self) -> None:
        if not self.canonical_name:
            self.canonical_name = canonicalize(self.name)

    def to_props(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "canonical_name": self.canonical_name,
            "entity_type": self.entity_type,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "mention_count": self.mention_count,
        }


@dataclass
class Fact:
    subject: str
    predicate: str
    object: str
    id: str = field(default_factory=lambda: new_id("fact"))
    confidence: float = 0.7
    valid_from: str = field(default_factory=now_iso)
    valid_until: str | None = None
    is_current: bool = True
    extraction_method: str = "rule-based"
    source_sessions: list[str] = field(default_factory=list)
    evidence: str = ""
    qualifier: str = ""
    polarity: str = "positive"

    @property
    def base_predicate(self) -> str:
        return self.predicate.split("@", 1)[0]

    def to_props(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "subject": self.subject,
            "predicate": self.predicate,
            "base_predicate": self.base_predicate,
            "object": self.object,
            "confidence": round(float(self.confidence), 4),
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "is_current": self.is_current,
            "extraction_method": self.extraction_method,
            "source_sessions": list(self.source_sessions),
            "evidence": self.evidence,
            "qualifier": self.qualifier,
            "polarity": self.polarity,
        }

    def statement(self) -> str:
        """Human-readable rendering used in assembled context and answers."""
        predicate = self.base_predicate.replace("_", " ")
        text = f"{self.subject} {predicate} {self.object}"
        if self.qualifier:
            text += f" (for {self.qualifier})"
        return text


@dataclass
class Conflict:
    id: str = field(default_factory=lambda: new_id("conf"))
    conflict_type: str = "contradiction"
    detected_at: str = field(default_factory=now_iso)
    resolved_at: str | None = None
    resolution_policy: str = "pending"
    status: str = "open"
    entity_id: str = ""
    subject: str = ""
    predicate: str = ""

    def to_props(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "conflict_type": self.conflict_type,
            "detected_at": self.detected_at,
            "resolved_at": self.resolved_at,
            "resolution_policy": self.resolution_policy,
            "status": self.status,
            "entity_id": self.entity_id,
            "subject": self.subject,
            "predicate": self.predicate,
        }
