"""Ingestion: raw session -> episodic graph -> semantic merge -> conflicts.

The hot path. Every session is written to the episodic layer immutably and in
full; extraction and semantic merge then run over it. Nothing is ever
overwritten -- a superseded fact keeps its node, its evidence and its edges, and
only loses ``is_current``.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from ..config import Settings, get_settings
from ..sidecar import IndexRecord, get_sidecar
from ..embeddings import embed_many, embed_one
from ..graph import schema as S
from ..graph.store import GraphStore, Node, Tx
from ..models.episodic import Session, Turn
from ..models.semantic import Conflict, Entity, Fact
from ..util import canonicalize, count_tokens, dedupe, new_id, now_iso, truncate
from .extraction import ExtractedFact, get_extractor, is_functional

log = logging.getLogger("weave.ingestion")

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


@dataclass
class IngestionResult:
    session_id: str
    turns: int = 0
    utterances: int = 0
    entities_extracted: int = 0
    facts_created: int = 0
    facts_reinforced: int = 0
    conflicts_detected: int = 0
    already_ingested: bool = False
    latency_ms: int = 0
    extraction_method: str = "rule-based"

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "turns": self.turns,
            "utterances": self.utterances,
            "entities_extracted": self.entities_extracted,
            "facts_created": self.facts_created,
            "facts_reinforced": self.facts_reinforced,
            "conflicts_detected": self.conflicts_detected,
            "already_ingested": self.already_ingested,
            "latency_ms": self.latency_ms,
            "extraction_method": self.extraction_method,
        }


class IngestionService:
    """Session -> episodic graph + entity extraction + semantic merge."""

    def __init__(self, store: GraphStore, settings: Settings | None = None) -> None:
        self.store = store
        self.settings = settings or get_settings()
        self.extractor = get_extractor(self.settings)
        self.sidecar = get_sidecar(self.settings)

    # -- public ------------------------------------------------------------

    def process_session(self, session: Session) -> IngestionResult:
        started = time.perf_counter()
        result = IngestionResult(
            session_id=session.id, extraction_method=self.extractor.method
        )

        with self.store.transaction() as tx:
            if tx.match(S.SESSION, {"id": session.id}, limit=1):
                result.already_ingested = True
                result.latency_ms = int((time.perf_counter() - started) * 1000)
                log.debug("session %s already ingested; skipping", session.id)
                return result

            if not session.session_summary:
                session.session_summary = self._summarise(session)

            session_node = tx.create_node([S.SESSION], session.to_props())
            self._link_session_order(tx, session_node)

            previous_turn: Node | None = None
            for turn in session.turns:
                turn.session_id = session.id
                turn_node = tx.create_node([S.TURN], turn.to_props())
                tx.create_edge(
                    session_node.id,
                    turn_node.id,
                    S.HAS_TURN,
                    {"turn_number": turn.turn_number},
                )
                if previous_turn is not None:
                    tx.create_edge(previous_turn.id, turn_node.id, S.NEXT, {})
                    tx.create_edge(turn_node.id, previous_turn.id, S.PREVIOUS, {})
                previous_turn = turn_node
                result.turns += 1

                self._process_turn(tx, session, turn, turn_node, session_node, result)

        # Mirror the episodic layer into the retrieval sidecar, if one is
        # configured. Deliberately outside the transaction and best-effort: the
        # graph is the source of truth, and a slow or failing index must not
        # roll back an ingest that otherwise succeeded.
        self._index_episodic(session)

        result.latency_ms = int((time.perf_counter() - started) * 1000)
        # One line per ingested session, at info: this is the hot path, and a
        # conflict count that jumps is the first sign extraction has drifted.
        log.info(
            "ingested %s: %d turns, %d facts (+%d reinforced), %d entities, "
            "%d conflict(s) in %d ms via %s",
            session.id,
            result.turns,
            result.facts_created,
            result.facts_reinforced,
            result.entities_extracted,
            result.conflicts_detected,
            result.latency_ms,
            result.extraction_method,
        )
        return result

    def _index_episodic(self, session: Session) -> None:
        if self.sidecar is None:
            return
        with self.store.transaction() as tx:
            nodes = [
                node
                for _, _, node in tx.expand(
                    [turn.id for turn in session.turns],
                    [S.HAS_UTTERANCE],
                    "out",
                    target_label=S.UTTERANCE,
                )
            ]
        records = [
            IndexRecord(
                id=node.id,
                text=str(node.get("text", "")),
                session_id=session.id,
                timestamp=str(node.get("timestamp", "") or ""),
                speaker=str(node.get("speaker", "") or ""),
            )
            for node in nodes
        ]
        if records:
            self.sidecar.index(records)

    # -- episodic ----------------------------------------------------------

    def _link_session_order(self, tx: Tx, session_node: Node) -> None:
        """Chain sessions for the same user so chronology is walkable."""
        user_id = session_node.get("user_id", "default")
        earlier = tx.match(
            S.SESSION,
            {"user_id": user_id},
            order_by=[("start_time", "desc")],
            limit=2,
        )
        for candidate in earlier:
            if candidate.id != session_node.id:
                tx.merge_edge(candidate.id, session_node.id, S.NEXT, {})
                tx.merge_edge(session_node.id, candidate.id, S.PREVIOUS, {})
                break

    def _process_turn(
        self,
        tx: Tx,
        session: Session,
        turn: Turn,
        turn_node: Node,
        session_node: Node,
        result: IngestionResult,
    ) -> None:
        sentences = [s.strip() for s in _SENTENCE_SPLIT.split(turn.text or "") if s.strip()]
        if not sentences:
            return

        extraction = self.extractor.extract(turn.text, speaker=turn.speaker)

        # Utterances: one per sentence, so a fact can point at its evidence.
        # Vectors are computed in one batch per turn -- §4.1's semantic
        # similarity fallback. Empty when embeddings are unavailable.
        vectors = embed_many(sentences, self.settings)
        utterances: list[tuple[str, Node]] = []
        for index, sentence in enumerate(sentences):
            utterance_node = tx.create_node(
                [S.UTTERANCE],
                {
                    "id": new_id("utt"),
                    "text": sentence,
                    "timestamp": turn.timestamp,
                    "utterance_number": index,
                    "token_count": count_tokens(sentence),
                    "speaker": turn.speaker,
                    "session_id": session.id,
                    "turn_id": turn.id,
                    "embedding": vectors[index] if index < len(vectors) else [],
                },
            )
            tx.create_edge(
                turn_node.id,
                utterance_node.id,
                S.HAS_UTTERANCE,
                {"utterance_number": index},
            )
            utterances.append((sentence.lower(), utterance_node))
            result.utterances += 1

        # Entities, linked to the utterances that actually mention them.
        entity_nodes: dict[str, Node] = {}
        for extracted in extraction.entities:
            canonical = extracted.canonical
            if not canonical:
                continue
            node, created = self._merge_entity(tx, extracted.name, extracted.entity_type)
            entity_nodes[canonical] = node
            if created:
                result.entities_extracted += 1
            for sentence_lower, utterance_node in utterances:
                if canonical == "user" or canonical in sentence_lower:
                    tx.merge_edge(utterance_node.id, node.id, S.MENTIONS, {})

        # Facts.
        for fact in extraction.facts:
            evidence_node = self._evidence_node(fact, utterances)
            self._merge_fact(
                tx, fact, session, evidence_node, session_node, entity_nodes, result
            )

    @staticmethod
    def _evidence_node(
        fact: ExtractedFact, utterances: list[tuple[str, Node]]
    ) -> Node | None:
        target = (fact.evidence or "").strip().lower()
        for sentence_lower, node in utterances:
            if target and (target == sentence_lower or target in sentence_lower):
                return node
        return utterances[0][1] if utterances else None

    # -- semantic ----------------------------------------------------------

    def _merge_entity(self, tx: Tx, name: str, entity_type: str) -> tuple[Node, bool]:
        canonical = canonicalize(name)
        entity = Entity(name=name, entity_type=entity_type)
        node, created = tx.merge_node(
            S.ENTITY,
            {"canonical_name": canonical},
            on_create=entity.to_props(),
            on_match={"last_seen": now_iso()},
        )
        if not created:
            node = tx.set_props(
                node.id, {"mention_count": int(node.get("mention_count", 0)) + 1}
            )
        return node, created

    def _merge_fact(
        self,
        tx: Tx,
        extracted: ExtractedFact,
        session: Session,
        evidence_node: Node | None,
        session_node: Node,
        entity_nodes: dict[str, Node],
        result: IngestionResult,
    ) -> None:
        subject = canonicalize(extracted.subject)
        subject_node = entity_nodes.get(subject)
        if subject_node is None:
            subject_node, created = self._merge_entity(tx, subject, "person")
            entity_nodes[subject] = subject_node
            if created:
                result.entities_extracted += 1

        candidates = tx.match(
            S.FACT,
            {
                "subject": subject,
                "predicate": extracted.predicate,
                "is_current": True,
            },
            order_by=[("valid_from", "desc")],
        )

        same_object = next(
            (f for f in candidates if f.get("object") == extracted.object), None
        )
        if same_object is not None:
            if same_object.get("polarity", "positive") == extracted.polarity:
                self._reinforce(tx, same_object, session.id)
                result.facts_reinforced += 1
                return
            # Same object, opposite polarity: a genuine reversal of opinion.
            new_node = self._create_fact(
                tx, extracted, session, subject_node, evidence_node, session_node,
                is_current=False,
            )
            result.facts_created += 1
            self._create_conflict(
                tx, same_object, new_node, subject_node, "correction", extracted
            )
            result.conflicts_detected += 1
            return

        rival = candidates[0] if candidates else None
        if rival is not None and is_functional(extracted.predicate):
            new_node = self._create_fact(
                tx, extracted, session, subject_node, evidence_node, session_node,
                is_current=False,
            )
            result.facts_created += 1
            conflict_type = extracted.update_cue or "contradiction"
            self._create_conflict(
                tx, rival, new_node, subject_node, conflict_type, extracted
            )
            result.conflicts_detected += 1
            return

        # New fact, or an additional value for a multi-valued predicate.
        self._create_fact(
            tx, extracted, session, subject_node, evidence_node, session_node,
            is_current=True,
        )
        result.facts_created += 1

    def _reinforce(self, tx: Tx, fact_node: Node, session_id: str) -> None:
        sources = dedupe(list(fact_node.get("source_sessions", [])) + [session_id])
        tx.set_props(
            fact_node.id,
            {
                "confidence": min(1.0, round(float(fact_node.get("confidence", 0.7)) + 0.1, 4)),
                "source_sessions": sources,
                "last_seen": now_iso(),
            },
        )

    def _create_fact(
        self,
        tx: Tx,
        extracted: ExtractedFact,
        session: Session,
        subject_node: Node,
        evidence_node: Node | None,
        session_node: Node,
        is_current: bool,
    ) -> Node:
        fact = Fact(
            subject=canonicalize(extracted.subject),
            predicate=extracted.predicate,
            object=extracted.object,
            confidence=extracted.confidence,
            valid_from=session.start_time,
            is_current=is_current,
            extraction_method=self.extractor.method,
            source_sessions=[session.id],
            evidence=truncate(extracted.evidence, 400),
            qualifier=extracted.qualifier,
            polarity=extracted.polarity,
        )
        props = fact.to_props()
        props["embedding"] = embed_one(fact.statement(), self.settings)
        node = tx.create_node([S.FACT], props)
        tx.create_edge(
            subject_node.id, node.id, S.HAS_FACT, {"confidence": fact.confidence}
        )
        if evidence_node is not None:
            tx.create_edge(node.id, evidence_node.id, S.DERIVED_FROM, {})
        tx.create_edge(node.id, session_node.id, S.DERIVED_FROM, {})
        return node

    def _create_conflict(
        self,
        tx: Tx,
        old_fact: Node,
        new_fact: Node,
        entity_node: Node,
        conflict_type: str,
        extracted: ExtractedFact,
    ) -> Node:
        """Record the disagreement. Resolution happens in consolidation."""
        conflict = Conflict(
            conflict_type=conflict_type or "contradiction",
            status="open",
            entity_id=entity_node.id,
            subject=old_fact.get("subject", ""),
            predicate=old_fact.get("predicate", ""),
        )
        node = tx.create_node([S.CONFLICT], conflict.to_props())
        tx.create_edge(node.id, old_fact.id, S.INVOLVES, {"role": "existing"})
        tx.create_edge(node.id, new_fact.id, S.INVOLVES, {"role": "new"})
        tx.create_edge(
            old_fact.id, new_fact.id, S.CONFLICTS_WITH, {"detected_at": now_iso()}
        )
        return node

    # -- summary -----------------------------------------------------------

    def _summarise(self, session: Session) -> str:
        user_turns = [t.text for t in session.turns if t.speaker == "user" and t.text]
        if not user_turns:
            return "Session with no user turns."
        return truncate(user_turns[0].replace("\n", " "), 160)
