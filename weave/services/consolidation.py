"""Consolidation -- the background "sleep" cycle.

Ingestion deliberately does not decide who wins a contradiction; it only
records that one exists. This service resolves open conflicts under an explicit
policy, merges duplicate facts, and leaves an auditable trail: the losing fact
keeps its node and evidence, gains a ``valid_until``, and is pointed at by a
``SUPERSEDES`` edge from the winner.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, get_settings
from ..graph import schema as S
from ..graph.store import GraphStore, Node, Tx
from ..util import dedupe, now_iso, parse_iso

log = logging.getLogger("weave.consolidation")

POLICIES = ("recency", "frequency", "confidence", "trust")


@dataclass
class ResolvedConflict:
    conflict_id: str
    subject: str
    predicate: str
    winner: str
    superseded: list[str] = field(default_factory=list)
    policy: str = "recency"

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflict_id": self.conflict_id,
            "subject": self.subject,
            "predicate": self.predicate,
            "winner": self.winner,
            "superseded": list(self.superseded),
            "policy": self.policy,
        }


@dataclass
class ConsolidationResult:
    conflicts_examined: int = 0
    conflicts_resolved: int = 0
    facts_superseded: int = 0
    duplicates_merged: int = 0
    policy: str = "recency"
    latency_ms: int = 0
    resolutions: list[ResolvedConflict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflicts_examined": self.conflicts_examined,
            "conflicts_resolved": self.conflicts_resolved,
            "facts_superseded": self.facts_superseded,
            "duplicates_merged": self.duplicates_merged,
            "policy": self.policy,
            "latency_ms": self.latency_ms,
            "resolutions": [r.to_dict() for r in self.resolutions],
        }


class ConsolidationService:
    def __init__(self, store: GraphStore, settings: Settings | None = None) -> None:
        self.store = store
        self.settings = settings or get_settings()

    def run_sleep_cycle(
        self,
        user_id: str | None = None,
        policy: str | None = None,
        max_conflicts: int = 50,
    ) -> ConsolidationResult:
        policy = (policy or self.settings.default_resolution_policy).lower()
        if policy not in POLICIES:
            raise ValueError(f"unknown resolution policy {policy!r}; expected {POLICIES}")

        started = time.perf_counter()
        result = ConsolidationResult(policy=policy)

        with self.store.transaction() as tx:
            open_conflicts = tx.match(
                S.CONFLICT,
                {"status": "open"},
                order_by=[("detected_at", "asc")],
                limit=max_conflicts,
            )
            for conflict in open_conflicts:
                result.conflicts_examined += 1
                resolved = self._resolve(tx, conflict, policy)
                if resolved is not None:
                    result.conflicts_resolved += 1
                    result.facts_superseded += len(resolved.superseded)
                    result.resolutions.append(resolved)

            result.duplicates_merged = self._merge_duplicates(tx)

        result.latency_ms = int((time.perf_counter() - started) * 1000)
        # The sleep cycle mutates what every later query treats as true, so it
        # says what it changed and under which policy -- an unexplained answer
        # is usually explained here.
        log.info(
            "sleep cycle (%s): examined %d, resolved %d, superseded %d, "
            "merged %d duplicate(s) in %d ms",
            policy,
            result.conflicts_examined,
            result.conflicts_resolved,
            result.facts_superseded,
            result.duplicates_merged,
            result.latency_ms,
        )
        return result

    # -- internals ---------------------------------------------------------

    def _resolve(self, tx: Tx, conflict: Node, policy: str) -> ResolvedConflict | None:
        involved = [
            other
            for _, _, other in tx.expand(
                [conflict.id], [S.INVOLVES], "out", target_label=S.FACT
            )
        ]
        if len(involved) < 2:
            # Nothing to arbitrate; close it out so it stops being examined.
            tx.set_props(
                conflict.id,
                {
                    "status": "resolved",
                    "resolved_at": now_iso(),
                    "resolution_policy": policy,
                },
            )
            return None

        winner = self._pick_winner(involved, policy)
        superseded: list[str] = []

        for fact in involved:
            if fact.id == winner.id:
                continue
            tx.set_props(
                fact.id,
                {
                    "is_current": False,
                    "valid_until": now_iso(),
                },
            )
            tx.merge_edge(
                winner.id, fact.id, S.SUPERSEDES, {"superseded_at": now_iso()}
            )
            superseded.append(fact.id)

        # Repeated agreement across sessions is what earns confidence, so cap
        # the resolution bonus rather than asserting certainty.
        sources = len(dedupe(list(winner.get("source_sessions", []))))
        confidence = min(0.95, 0.8 + 0.05 * sources)
        tx.set_props(
            winner.id,
            {
                "is_current": True,
                "valid_until": None,
                "confidence": round(confidence, 4),
                "extraction_method": "consolidated",
            },
        )

        tx.set_props(
            conflict.id,
            {
                "status": "resolved",
                "resolved_at": now_iso(),
                "resolution_policy": policy,
            },
        )
        tx.merge_edge(conflict.id, winner.id, S.RESOLVED_TO, {"resolved_at": now_iso()})

        return ResolvedConflict(
            conflict_id=conflict.id,
            subject=str(winner.get("subject", "")),
            predicate=str(winner.get("predicate", "")),
            winner=str(winner.get("object", "")),
            superseded=superseded,
            policy=policy,
        )

    @staticmethod
    def _pick_winner(facts: list[Node], policy: str) -> Node:
        def valid_from(node: Node) -> Any:
            return parse_iso(node.get("valid_from")) or parse_iso("1970-01-01")

        ordered = sorted(facts, key=valid_from)
        if policy == "frequency":
            return max(
                ordered,
                key=lambda n: (len(dedupe(list(n.get("source_sessions", [])))), valid_from(n)),
            )
        if policy == "confidence":
            return max(
                ordered, key=lambda n: (float(n.get("confidence", 0.0)), valid_from(n))
            )
        if policy == "trust":
            # Prefer facts the user stated explicitly and repeatedly.
            return max(
                ordered,
                key=lambda n: (
                    1 if n.get("extraction_method") != "llm-extract" else 0,
                    len(dedupe(list(n.get("source_sessions", [])))),
                    valid_from(n),
                ),
            )
        return ordered[-1]  # recency (default)

    def _merge_duplicates(self, tx: Tx) -> int:
        """Fold identical current facts into one node, keeping the earliest."""
        merged = 0
        grouped: dict[tuple[str, str, str, str], list[Node]] = {}
        for fact in tx.match(S.FACT, {"is_current": True}):
            key = (
                str(fact.get("subject", "")),
                str(fact.get("predicate", "")),
                str(fact.get("object", "")),
                str(fact.get("polarity", "positive")),
            )
            grouped.setdefault(key, []).append(fact)

        for facts in grouped.values():
            if len(facts) < 2:
                continue
            facts.sort(key=lambda n: parse_iso(n.get("valid_from")) or parse_iso("1970-01-01"))
            canonical, duplicates = facts[0], facts[1:]

            sources = list(canonical.get("source_sessions", []))
            confidence = float(canonical.get("confidence", 0.7))
            for duplicate in duplicates:
                sources.extend(duplicate.get("source_sessions", []))
                confidence = max(confidence, float(duplicate.get("confidence", 0.7)))
                tx.set_props(
                    duplicate.id, {"is_current": False, "valid_until": now_iso()}
                )
                tx.merge_edge(
                    canonical.id,
                    duplicate.id,
                    S.SUPERSEDES,
                    {"superseded_at": now_iso(), "reason": "duplicate"},
                )
                merged += 1

            sources = dedupe(sources)
            tx.set_props(
                canonical.id,
                {
                    "source_sessions": sources,
                    "confidence": round(
                        min(1.0, max(confidence, 0.6 + 0.1 * len(sources))), 4
                    ),
                },
            )
        return merged
