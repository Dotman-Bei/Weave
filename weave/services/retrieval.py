"""Query pipeline: classify -> route -> retrieve -> abstain? -> answer -> learn.

The abstention router is the part that matters most. A memory system that
answers from an adjacent-but-wrong fact is worse than one that says it does not
know, so coverage is scored *before* any LLM call: if the retrieved subgraph has
no topical overlap with the question, Weave abstains and never spends a token.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..config import Settings, get_settings
from ..embeddings import cosine, embed_one
from ..graph import schema as S
from ..graph.store import GraphStore, Node, Tx, contains, in_
from ..prompts import load_prompt
from ..util import (
    count_tokens,
    dedupe,
    human_date,
    new_id,
    normalise_spelling,
    truncate,
)
from ..sidecar import get_sidecar
from .extraction import extract_query_entities
from .procedural import PathChoice, ProceduralLearningService

# Per query term, when selecting episodic candidates by wording. Bounded so a
# common word cannot pull the whole haystack into scoring.
_TOKEN_CANDIDATES = 60

# Sidecar hits that make the local scan redundant. Below this the index either
# is not populated or did not understand the question, so the scan still runs.
_SIDECAR_SUFFICIENT = 5

# Share of the query's weighted content that the retrieved subgraph must not
# mention before the match is called incidental, and how hard that counts.
# Both measured against LongMemEval's abstention set, not guessed.
_UNCOVERED_SIGNIFICANT = 0.25
_UNCOVERED_WEIGHT = 0.9

# How many of the top-scoring items count as "the evidence behind the answer".
_GROUNDING_HEAD = 3

ABSTENTION_ANSWER = (
    "I don't know — that isn't in the stored conversation history."
)

# --- Query classification ---------------------------------------------------

QUERY_TYPE_PATTERNS: dict[str, list[str]] = {
    "temporal": ["when", "last time", "previously", "before", "after", "session",
                 "first", "originally", "ago", "used to", "back then", "earlier",
                 "how long", "since"],
    "preference": ["prefer", "like", "love", "hate", "favorite", "favourite",
                   "want", "should i use", "recommend", "enjoy", "dislike"],
    "factual": ["what", "who", "where", "how many", "is it", "which", "name",
                "am i", "do i", "my"],
    "procedural": ["how do i", "how to", "steps to", "process for", "workflow",
                   "set up", "configure", "how did i"],
}

# Cues that a question asks about the *superseded* value rather than the
# current one ("what did I use before I moved to Go?").
_PRIOR_CUES = (
    "before",
    "previously",
    "used to",
    "prior to",
    "earlier",
    "back then",
    "originally",
    "instead of",
    "at first",
    "switch from",
    "switched from",
)


def wants_history(query: str) -> bool:
    lowered = (query or "").lower()
    return any(cue in lowered for cue in _PRIOR_CUES)


# "factual" is the residual category: its cues ("what", "my", "do i") appear in
# almost every question, so it is weighted to win only when nothing specific
# fires. Otherwise it would swallow every preference and temporal query.
_TYPE_WEIGHTS = {"temporal": 1.4, "preference": 1.3, "procedural": 1.5, "factual": 0.4}

_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "am", "do", "does",
    "did", "i", "me", "my", "mine", "you", "your", "it", "its", "of", "in", "on",
    "at", "to", "for", "with", "and", "or", "but", "if", "then", "than", "that",
    "this", "these", "those", "what", "which", "who", "whom", "when", "where",
    "why", "how", "can", "could", "would", "should", "will", "shall", "may",
    "might", "must", "have", "has", "had", "about", "into", "from", "by", "as",
    "so", "up", "out", "there", "here", "now", "again", "ever", "any", "some",
    "tell", "say", "said", "know", "much", "many", "most", "s", "t", "re", "ve",
}

_WORD = re.compile(r"[a-z0-9+#]+")


def content_tokens(text: str) -> list[str]:
    """Meaningful lowercase tokens, stopwords removed."""
    return [
        token
        for token in _WORD.findall((text or "").lower())
        if token not in _STOPWORDS and len(token) > 1
    ]


def _stem(token: str) -> str:
    """Crude stem so 'live' matches 'lives' and 'prefer' matches 'prefers'.

    Only the plural / third-person ``s`` is stripped; truncation absorbs the
    rest. Stripping ``es`` as a unit would turn "lives" into "liv" and stop it
    matching "live", which is exactly the case this needs to get right.
    Spelling is normalised first so "colour" matches a stored "color".
    """
    token = normalise_spelling(token)
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        token = token[:-1]
        token = normalise_spelling(token)
    return token[:5]


def lexical_overlap(
    query_tokens: Iterable[str],
    text: str,
    weights: dict[str, float] | None = None,
) -> float:
    """Share of the query covered by ``text``, with prefix stemming.

    ``weights`` carries each term's inverse document frequency. Unweighted,
    every query word counts the same, so "What is my blood type?" is judged
    half-grounded by any utterance containing the word "type" -- which at
    haystack scale is thousands of them. Weighting by rarity makes the decisive
    word the one that is actually about the question.
    """
    query_list = list(query_tokens)
    if not query_list:
        return 0.0
    candidates = {_stem(token) for token in content_tokens(text)}
    if not candidates:
        return 0.0
    if not weights:
        hits = sum(1 for token in query_list if _stem(token) in candidates)
        return hits / len(query_list)

    total = sum(weights.get(token, 1.0) for token in query_list)
    if total <= 0:
        return 0.0
    hit = sum(
        weights.get(token, 1.0) for token in query_list if _stem(token) in candidates
    )
    return hit / total


# Words that describe the *shape* of a question rather than its subject.
# "What did I say before I changed to ClickHouse?" is about ClickHouse;
# "before" and "changed" are the operator. They are often absent from stored
# text, which would make them look maximally rare and hijack the focus term.
_OPERATOR_TOKENS = frozenset(
    token
    for phrase in (
        *_PRIOR_CUES,
        *QUERY_TYPE_PATTERNS["temporal"],
        *QUERY_TYPE_PATTERNS["procedural"],
        "changed", "change", "switch", "switched", "moved", "move", "still",
    )
    for token in _WORD.findall(phrase.lower())
)

# Indefinite pronouns and generic time/quantity nouns. These are rare inside
# one person's memory -- so inverse document frequency calls them highly
# informative -- while carrying nothing about what is being asked. "What am I
# learning at the moment?" is about learning, not about "moment"; counting the
# framing as missing subject matter made Weave refuse questions it could answer.
# Same category as _OPERATOR_TOKENS: question shape, not question subject.
_VAGUE_TOKENS = frozenset(
    """
    anything something everything nothing anyone someone everyone
    anybody somebody everybody
    moment moments day days week weeks month months year years
    time times thing things stuff way ways kind kinds sort sorts
    currently lately recently nowadays today nowaday
    """.split()
)

# Below this many stored utterances, document frequency is noise: every term
# looks rare because the corpus is tiny. Weighting and the focus-term gate
# both stand down rather than act on a statistic that has not converged.
_MIN_CORPUS_FOR_IDF = 50


def token_weights(tx: Tx, query_tokens: Iterable[str]) -> dict[str, float]:
    """Smoothed inverse document frequency for each query term.

    Document frequency is counted over the stored utterances, so the weights
    describe *this* memory rather than a general corpus: a word that appears
    everywhere in someone's history carries little information about which of
    their utterances answers a question.
    """
    tokens = {
        token
        for token in query_tokens
        # Numerals are short but among the most discriminating tokens a
        # question has -- a 30-gallon tank is not a 20-gallon one -- so they
        # are kept despite failing the length test.
        if (len(token) >= 3 or token.isdigit())
        and token not in _OPERATOR_TOKENS
        and token not in _VAGUE_TOKENS
    }
    if not tokens:
        return {}
    total = tx.count(S.UTTERANCE)
    if total < _MIN_CORPUS_FOR_IDF:
        return {}
    weights: dict[str, float] = {}
    for token in tokens:
        frequency = tx.count(S.UTTERANCE, {"text": contains(token)})
        weights[token] = math.log((total + 1) / (frequency + 1)) + 1.0
    return weights


def semantic_grounding(similarity: float, settings: Settings) -> float:
    """Map a cosine similarity onto the 0-1 scale lexical overlap uses.

    Below the floor a match contributes nothing, so unrelated text cannot drift
    a question over the abstention threshold on vector noise alone.
    """
    floor, ceiling = settings.embedding_floor, settings.embedding_ceiling
    if similarity <= floor or ceiling <= floor:
        return 0.0
    return min(1.0, (similarity - floor) / (ceiling - floor))


def classify_query(query: str, settings: Settings | None = None) -> tuple[str, float]:
    """Keyword scoring, with an LLM tie-break only when nothing matches."""
    settings = settings or get_settings()
    lowered = (query or "").lower()

    scores: dict[str, float] = {}
    for qtype, keywords in QUERY_TYPE_PATTERNS.items():
        hits = sum(1 for keyword in keywords if keyword in lowered)
        scores[qtype] = hits * _TYPE_WEIGHTS[qtype]

    best = max(scores, key=lambda key: scores[key])
    if scores[best] > 0:
        total = sum(scores.values()) or 1.0
        return best, round(scores[best] / total, 4)

    from ..llm import get_llm

    llm = get_llm(settings)
    if llm is not None:
        try:
            raw = llm.complete(
                load_prompt("query_classification").format(query=query),
                max_tokens=16,
                effort="low",
            )
            guess = raw.strip().lower().split()[0].strip(".,")
            if guess in QUERY_TYPE_PATTERNS:
                return guess, 0.6
        except Exception:
            pass
    return "factual", 0.25


# --- Fact rendering ---------------------------------------------------------
#
# A predicate is "<base>_<category>"; the category exists to keep unrelated
# facts from colliding, not to be read aloud. These templates turn the stored
# triple back into a sentence a person would actually write.

_PREDICATE_BASES = (
    "allergic_to",
    "lives_in",
    "works_at",
    "favorite",
    "favourite",
    "learning",
    "prefers",
    "likes",
    "uses",
    "role",
    "name",
    "has",
)

_POSITIVE_TEMPLATES = {
    "name": "{s}'s name is {o}",
    "prefers": "{s} prefers {o}",
    "likes": "{s} likes {o}",
    "lives_in": "{s} lives in {o}",
    "works_at": "{s} works at {o}",
    "favorite": "{s}'s favorite {cat} is {o}",
    "favourite": "{s}'s favourite {cat} is {o}",
    "role": "{s} is {o}",
    "uses": "{s} uses {o}",
    "learning": "{s} is learning {o}",
    "has": "{s} has {o}",
    "allergic_to": "{s} is allergic to {o}",
}

_PAST_TEMPLATES = {
    "name": "{s}'s name was {o}",
    "prefers": "{s} preferred {o}",
    "likes": "{s} liked {o}",
    "lives_in": "{s} lived in {o}",
    "works_at": "{s} worked at {o}",
    "favorite": "{s}'s favorite {cat} was {o}",
    "favourite": "{s}'s favourite {cat} was {o}",
    "role": "{s} was {o}",
    "uses": "{s} used {o}",
    "learning": "{s} was learning {o}",
    "has": "{s} had {o}",
    "allergic_to": "{s} was allergic to {o}",
}

_NEGATIVE_TEMPLATES = {
    "likes": "{s} dislikes {o}",
    "prefers": "{s} does not prefer {o}",
    "uses": "{s} no longer uses {o}",
    "learning": "{s} is not learning {o}",
    "has": "{s} does not have {o}",
}


def _split_predicate(predicate: str) -> tuple[str, str]:
    """Split ``prefers_language`` into ``("prefers", "language")``."""
    predicate = predicate.split("@", 1)[0]
    for base in _PREDICATE_BASES:
        if predicate == base:
            return base, ""
        if predicate.startswith(f"{base}_"):
            return base, predicate[len(base) + 1 :]
    return predicate, ""


def join_objects(objects: list[str]) -> str:
    """"a", "a and b", "a, b and c"."""
    objects = dedupe([o for o in objects if o])
    if len(objects) <= 1:
        return objects[0] if objects else ""
    return f"{', '.join(objects[:-1])} and {objects[-1]}"


def render_fact(
    subject: str,
    predicate: str,
    obj: str,
    qualifier: str = "",
    negated: bool = False,
    past: bool = False,
) -> str:
    """Turn a stored triple back into a readable sentence."""
    base, category = _split_predicate(predicate)
    if negated:
        templates = _NEGATIVE_TEMPLATES
    else:
        templates = _PAST_TEMPLATES if past else _POSITIVE_TEMPLATES
    template = templates.get(base)
    if template is None:
        phrase = base.replace("_", " ")
        template = ("{s} does not {p} {o}" if negated else "{s} {p} {o}").replace(
            "{p}", phrase
        )
    text = template.format(s=subject, o=obj, cat=category.replace("_", " ") or "one")
    if qualifier:
        text += f" (for {qualifier})"
    return " ".join(text.split())


# --- Evidence ---------------------------------------------------------------


@dataclass
class Evidence:
    kind: str  # "fact" | "utterance"
    id: str
    text: str
    score: float = 0.0
    lexical: float = 0.0
    semantic: float = 0.0
    subject: str = ""
    session_id: str = ""
    session_label: str = ""
    timestamp: str = ""
    speaker: str = ""
    is_current: bool | None = None
    confidence: float = 0.0
    predicate: str = ""
    object: str = ""
    qualifier: str = ""
    polarity: str = "positive"
    superseded_by: list[str] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)
    # Reached over a SUPERSEDES edge from something that did match. See
    # RetrievalService._prune_evidence.
    graph_linked: bool = False

    @property
    def matched_by(self) -> str:
        """Which signal carried this match: wording, meaning, graph, or none."""
        if self.semantic > 0 and self.lexical > 0:
            return "both"
        if self.semantic > 0:
            return "meaning"
        if self.lexical > 0:
            return "wording"
        if self.graph_linked:
            return "graph"
        return "none"

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "kind": self.kind,
            "id": self.id,
            "text": self.text,
            "score": round(self.score, 4),
            "lexical": round(self.lexical, 4),
            "semantic": round(self.semantic, 4),
            "matched_by": self.matched_by,
            "session_id": self.session_id,
            "session_label": self.session_label,
            "timestamp": self.timestamp,
        }
        if self.kind == "fact":
            payload.update(
                {
                    "predicate": self.predicate,
                    "object": self.object,
                    "qualifier": self.qualifier,
                    "polarity": self.polarity,
                    "is_current": self.is_current,
                    "confidence": round(self.confidence, 4),
                    "supersedes": self.supersedes,
                    "superseded_by": self.superseded_by,
                }
            )
        else:
            payload["speaker"] = self.speaker
        return payload


@dataclass
class AbstentionDecision:
    abstain: bool
    confidence: float
    score: float
    reasons: list[str]
    threshold: float
    signals: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "abstain": self.abstain,
            "confidence": round(self.confidence, 4),
            "score": round(self.score, 4),
            "reasons": self.reasons,
            "threshold": self.threshold,
            "signals": self.signals,
        }


@dataclass
class RetrievalResult:
    query: str
    query_id: str
    answer: str = ""
    abstained: bool = False
    abstention_reasons: list[str] = field(default_factory=list)
    confidence: float = 0.0
    query_type: str = "factual"
    classification_confidence: float = 0.0
    retrieval_path: str = ""
    path_reason: str = ""
    entities: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    # How many pieces of evidence the traversal actually returned, before the
    # ones that matched nothing were pruned. Reported separately so the pruned
    # list never reads as the whole subgraph.
    retrieved_count: int = 0
    facts_used: list[dict[str, Any]] = field(default_factory=list)
    context: str = ""
    tokens_used: int = 0
    latency_ms: int = 0
    layers_touched: list[str] = field(default_factory=list)
    generator: str = "template"
    prefer_history: bool = False
    abstention: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "query_id": self.query_id,
            "answer": self.answer,
            "abstained": self.abstained,
            "abstention_reasons": self.abstention_reasons,
            "confidence": round(self.confidence, 4),
            "query_type": self.query_type,
            "classification_confidence": self.classification_confidence,
            "retrieval_path": self.retrieval_path,
            "path_reason": self.path_reason,
            "entities": self.entities,
            "evidence": [e.to_dict() for e in self.evidence],
            "retrieved_count": self.retrieved_count,
            "facts_used": self.facts_used,
            "context": self.context,
            "tokens_used": self.tokens_used,
            "latency_ms": self.latency_ms,
            "layers_touched": self.layers_touched,
            "generator": self.generator,
            "prefer_history": self.prefer_history,
            "abstention": self.abstention,
        }


# --- Service ----------------------------------------------------------------


class RetrievalService:
    def __init__(self, store: GraphStore, settings: Settings | None = None) -> None:
        self.store = store
        self.settings = settings or get_settings()
        self.sidecar = get_sidecar(self.settings)
        self.procedural = ProceduralLearningService(store, self.settings)

    def query(
        self,
        query: str,
        user_id: str | None = None,
        max_tokens: int | None = None,
        force_path: str | None = None,
        explore: bool = True,
        generate: bool = True,
        restrict_layers: set[str] | None = None,
    ) -> RetrievalResult:
        started = time.perf_counter()
        budget = max_tokens or self.settings.max_context_tokens
        result = RetrievalResult(query=query, query_id=new_id("q"))

        query_type, confidence = classify_query(query, self.settings)
        result.query_type = query_type
        result.classification_confidence = confidence

        choice = self._resolve_path(query_type, force_path, explore)
        result.retrieval_path = choice.name
        result.path_reason = choice.reason
        result.layers_touched = list(choice.node.get("layers", []))

        entities = extract_query_entities(query)
        result.entities = entities
        result.prefer_history = wants_history(query)

        query_tokens = content_tokens(query)
        query_vector = embed_one(query, self.settings)
        with self.store.transaction() as tx:
            weights = token_weights(tx, query_tokens)
            entity_nodes = self._resolve_entities(tx, entities, query_tokens)
            evidence = self._execute_path(
                tx,
                choice,
                entity_nodes,
                query_tokens,
                user_id,
                restrict_layers,
                result.prefer_history,
                query_vector,
                weights,
            )

            # If the routed path surfaced nothing topical, widen into the
            # episodic layer before deciding to abstain. A false "I don't know"
            # costs more than one extra bounded traversal.
            best = max((self._grounding(e, query_tokens) for e in evidence), default=0.0)
            may_widen = restrict_layers is None or "episodic" in restrict_layers
            if best < 0.34 and may_widen and "episodic" not in result.layers_touched:
                widened = self._episodic(
                    tx,
                    [n.id for n in entity_nodes],
                    query_tokens,
                    depth=2,
                    limit=12,
                    query_vector=query_vector,
                    weights=weights,
                )
                if widened:
                    evidence.extend(widened)
                    result.layers_touched = [*result.layers_touched, "episodic"]
                    result.path_reason = f"{result.path_reason}+widened"

        # Cap each layer separately rather than taking the global top 25. A
        # question whose wording matches many utterances would otherwise crowd
        # every fact out of the result -- and the facts are the distilled
        # answer the semantic layer exists to provide. This showed up as a
        # "before X?" question losing the superseded fact it needed because
        # twenty excerpts outscored it.
        evidence.sort(key=lambda item: item.score, reverse=True)
        facts = [item for item in evidence if item.kind == "fact"][:12]
        rest = [item for item in evidence if item.kind != "fact"][:13]
        result.evidence = sorted(
            facts + rest, key=lambda item: item.score, reverse=True
        )
        result.retrieved_count = len(result.evidence)

        # Abstention is scored against the *unpruned* subgraph: its result-count
        # signal is a measure of what the traversal reached, and pruning first
        # would quietly change the decision.
        decision = self.abstention_check(
            result,
            entity_nodes_found=bool(entity_nodes),
            has_vectors=bool(query_vector),
            weights=weights,
        )
        result.abstention = decision.to_dict()
        result.confidence = decision.confidence
        result.abstained = decision.abstain
        result.abstention_reasons = decision.reasons

        # Only now drop the evidence that matched nothing, so neither the
        # context budget nor the UI is padded with unrelated utterances that
        # the traversal happened to walk past.
        result.evidence = self._prune_evidence(result.evidence)

        if decision.abstain:
            result.answer = ABSTENTION_ANSWER
            result.tokens_used = 0
            result.generator = "abstained"
        else:
            result.context = self.assemble_context(result.evidence, budget)
            result.tokens_used = count_tokens(result.context)
            if generate:
                result.answer, result.generator = self._answer(query, result)
            else:
                result.answer = ""
                result.generator = "none"

        result.facts_used = [
            e.to_dict() for e in result.evidence if e.kind == "fact"
        ][:10]
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result

    # -- routing -----------------------------------------------------------

    def _resolve_path(
        self, query_type: str, force_path: str | None, explore: bool
    ) -> PathChoice:
        if force_path:
            with self.store.transaction() as tx:
                nodes = tx.match(S.RETRIEVAL_PATH, {"name": force_path}, limit=1)
            if nodes:
                return PathChoice(
                    name=force_path,
                    node=nodes[0],
                    success_rate=0.0,
                    attempts=0,
                    reason="forced",
                )
        return self.procedural.get_best_path(query_type, explore=explore)

    # -- retrieval ---------------------------------------------------------

    def _resolve_entities(
        self, tx: Tx, entities: list[str], query_tokens: list[str]
    ) -> list[Node]:
        """Anchor nodes for traversal: named entities plus query-token matches."""
        names = dedupe(list(entities) + query_tokens)
        if not names:
            return []
        nodes = tx.match(S.ENTITY, {"canonical_name": in_(names)})
        return nodes

    def _execute_path(
        self,
        tx: Tx,
        choice: PathChoice,
        entity_nodes: list[Node],
        query_tokens: list[str],
        user_id: str | None,
        restrict_layers: set[str] | None = None,
        prefer_history: bool = False,
        query_vector: list[float] | None = None,
        weights: dict[str, float] | None = None,
    ) -> list[Evidence]:
        name = choice.name
        entity_ids = [node.id for node in entity_nodes]
        semantic_allowed = restrict_layers is None or "semantic" in restrict_layers
        episodic_allowed = restrict_layers is None or "episodic" in restrict_layers

        def semantic(include_history: bool, cap: int | None = None) -> list[Evidence]:
            if not semantic_allowed:
                return []
            found = self._semantic(
                tx,
                entity_nodes,
                query_tokens,
                include_history or prefer_history,
                prefer_history,
                query_vector,
                weights,
            )
            return found[:cap] if cap else found

        def episodic(depth: int, limit: int) -> list[Evidence]:
            if not episodic_allowed:
                return []
            return self._episodic(
                tx, entity_ids, query_tokens, depth=depth, limit=limit,
                query_vector=query_vector, weights=weights,
            )

        if name == "semantic-only":
            return semantic(include_history=False)
        if name == "hybrid-conflict":
            return semantic(include_history=True)
        if name == "episodic-depth-3":
            # Facts stay useful even on the episodic path; they are cheap and
            # carry the chronology the question usually wants.
            return episodic(3, 20) + semantic(include_history=True, cap=6)
        if name == "episodic-depth-2":
            return episodic(2, 15) + semantic(include_history=False, cap=4)
        return semantic(include_history=True)

    def _semantic(
        self,
        tx: Tx,
        entity_nodes: list[Node],
        query_tokens: list[str],
        include_history: bool,
        prefer_history: bool = False,
        query_vector: list[float] | None = None,
        weights: dict[str, float] | None = None,
    ) -> list[Evidence]:
        names = [str(node.get("canonical_name")) for node in entity_nodes]
        facts: dict[str, Node] = {}

        if names:
            for fact in tx.match(S.FACT, {"subject": in_(names)}):
                facts[fact.id] = fact
            for fact in tx.match(S.FACT, {"object": in_(names)}):
                facts[fact.id] = fact
        if not facts:
            # No entity anchor: fall back to a bounded scan of current facts so
            # a bare question ("what do I like?") still has something to rank.
            for fact in tx.match(
                S.FACT, {"is_current": True}, order_by=[("confidence", "desc")], limit=200
            ):
                facts[fact.id] = fact

        evidence: list[Evidence] = []
        for fact in facts.values():
            is_current = bool(fact.get("is_current"))
            if not include_history and not is_current:
                continue
            evidence.append(
                self._fact_evidence(
                    tx, fact, query_tokens, include_history, prefer_history,
                    query_vector, weights,
                )
            )
        # Sorted here so callers that cap the list ("take the top 6 facts")
        # take the best ones rather than whichever the scan happened to hit.
        evidence.sort(key=lambda item: item.score, reverse=True)
        return evidence

    def _fact_evidence(
        self,
        tx: Tx,
        fact: Node,
        query_tokens: list[str],
        include_history: bool,
        prefer_history: bool = False,
        query_vector: list[float] | None = None,
        weights: dict[str, float] | None = None,
    ) -> Evidence:
        predicate = str(fact.get("predicate", ""))
        obj = str(fact.get("object", ""))
        qualifier = str(fact.get("qualifier", "") or "")
        evidence_text = str(fact.get("evidence", "") or "")

        # Three tiers, most specific first. A question's verb usually names the
        # predicate ("who do I *work* for"), so a predicate hit is worth more
        # than the same word appearing inside an object phrase ("long walks
        # after work"), which in turn beats a hit in the surrounding sentence.
        score = 0.45 * lexical_overlap(query_tokens, predicate.replace("_", " "), weights)
        score += 0.25 * lexical_overlap(query_tokens, f"{obj} {qualifier}".strip(), weights)
        score += 0.15 * lexical_overlap(query_tokens, evidence_text, weights)

        # Semantic similarity is additive, not a replacement: it rescues a
        # question worded in synonyms without letting vector noise outrank a
        # direct lexical hit.
        semantic = 0.0
        if query_vector:
            semantic = semantic_grounding(
                cosine(query_vector, list(fact.get("embedding", []) or [])),
                self.settings,
            )
            score += self.settings.embedding_weight * semantic

        is_current = bool(fact.get("is_current"))
        if prefer_history:
            # "What did I use before?" wants the superseded value.
            score += 0.05 if is_current else 0.3
        else:
            score += 0.25 if is_current else 0.0
        score += 0.1 * float(fact.get("confidence", 0.0))

        supersedes: list[str] = []
        superseded_by: list[str] = []
        if include_history:
            for _, _, other in tx.expand([fact.id], [S.SUPERSEDES], "out", target_label=S.FACT):
                supersedes.append(str(other.get("object", "")))
            for _, _, other in tx.expand([fact.id], [S.SUPERSEDES], "in", target_label=S.FACT):
                superseded_by.append(str(other.get("object", "")))

        sessions = list(fact.get("source_sessions", []))
        text = self._fact_sentence(fact)
        return Evidence(
            kind="fact",
            id=fact.id,
            text=text,
            score=score,
            lexical=lexical_overlap(query_tokens, text, weights),
            semantic=semantic,
            subject=str(fact.get("subject", "user")),
            session_id=sessions[0] if sessions else "",
            timestamp=str(fact.get("valid_from", "")),
            is_current=is_current,
            confidence=float(fact.get("confidence", 0.0)),
            predicate=predicate,
            object=obj,
            qualifier=qualifier,
            polarity=str(fact.get("polarity", "positive")),
            supersedes=supersedes,
            superseded_by=superseded_by,
        )

    @staticmethod
    def _fact_sentence(fact: Node) -> str:
        return render_fact(
            subject=str(fact.get("subject", "user")),
            predicate=str(fact.get("predicate", "")),
            obj=str(fact.get("object", "")),
            qualifier=str(fact.get("qualifier", "") or ""),
            negated=str(fact.get("polarity", "positive")) == "negative",
        )

    def _episodic(
        self,
        tx: Tx,
        entity_ids: list[str],
        query_tokens: list[str],
        depth: int,
        limit: int,
        query_vector: list[float] | None = None,
        weights: dict[str, float] | None = None,
    ) -> list[Evidence]:
        """Bounded multi-hop traversal into the episodic layer."""
        utterances: dict[str, Node] = {}

        if entity_ids:
            paths = tx.paths(
                entity_ids,
                rel_types=[S.MENTIONS, S.HAS_UTTERANCE, S.HAS_TURN, S.DERIVED_FROM],
                direction="both",
                max_len=depth,
                path_count=40,
                result_limit=400,
            )
            for path in paths:
                for node in path.nodes:
                    if node.labels and node.labels[0] == S.UTTERANCE:
                        utterances[node.id] = node

        # Ask the retrieval sidecar first, when one is configured. It is a
        # real text index, so it finds relevant utterances without the scan
        # below -- but its answers are still only *candidates*: each id is
        # hydrated back into a local node, and anything the sidecar knows about
        # that the graph does not is discarded rather than trusted.
        from_sidecar = 0
        if self.sidecar is not None:
            for hit in self.sidecar.search(" ".join(query_tokens) or " ", limit=40):
                node = tx.get_node(hit.id)
                if node is not None and node.labels and node.labels[0] == S.UTTERANCE:
                    utterances[node.id] = node
                    from_sidecar += 1

        # Anchor on the question's own words as well as on its entities.
        #
        # Entity anchoring only reaches what the extractor recognised as an
        # entity, and the recency fallback below can only see the newest slice
        # of the episodic layer. On a real haystack -- LongMemEval stores ~6500
        # utterances per question -- that means evidence which is merely *old*
        # is never scored at all, however well it matches. Selecting candidates
        # by the query's own terms is what makes retrieval independent of
        # haystack size.
        # Skipped when the sidecar already supplied candidates: it *is* a text
        # index, so repeating the scan pays for the same answer twice. Running
        # both was measurably slower than either alone.
        if from_sidecar < _SIDECAR_SUFFICIENT:
            terms = [token for token in query_tokens[:6] if len(token) >= 3]
            for node in tx.search_text(
                S.UTTERANCE, "text", terms, limit=_TOKEN_CANDIDATES
            ):
                utterances[node.id] = node

        if not utterances:
            # Nothing anchored the question at all: rank recent utterances so
            # the abstention router still has something to judge.
            for node in tx.match(
                S.UTTERANCE, order_by=[("timestamp", "desc")], limit=200
            ):
                utterances[node.id] = node

        sessions: dict[str, Node] = {}
        evidence: list[Evidence] = []
        for node in utterances.values():
            text = str(node.get("text", ""))
            score = lexical_overlap(query_tokens, text, weights)
            semantic = 0.0
            if query_vector:
                semantic = semantic_grounding(
                    cosine(query_vector, list(node.get("embedding", []) or [])),
                    self.settings,
                )
                score += self.settings.embedding_weight * semantic
            if score <= 0 and len(utterances) > limit:
                continue
            session_id = str(node.get("session_id", ""))
            if session_id and session_id not in sessions:
                found = tx.match(S.SESSION, {"id": session_id}, limit=1)
                if found:
                    sessions[session_id] = found[0]
            session = sessions.get(session_id)
            evidence.append(
                Evidence(
                    kind="utterance",
                    id=node.id,
                    text=text,
                    score=score + 0.05,
                    lexical=lexical_overlap(query_tokens, text, weights),
                    semantic=semantic,
                    session_id=session_id,
                    session_label=self._session_label(session),
                    timestamp=str(node.get("timestamp", "")),
                    speaker=str(node.get("speaker", "")),
                )
            )

        evidence.sort(key=lambda item: (item.score, item.timestamp), reverse=True)
        return evidence[:limit]

    @staticmethod
    def _session_label(session: Node | None) -> str:
        if session is None:
            return ""
        number = session.get("session_number")
        date = human_date(str(session.get("start_time", "")))
        if number:
            return f"session {number} ({date})"
        return f"session {date}"

    # -- abstention --------------------------------------------------------

    @staticmethod
    def _grounding(
        item: Evidence, query_tokens: list[str], has_vectors: bool = False
    ) -> float:
        """How well one piece of evidence covers the question, 0-1.

        The stronger signal wins: shared words, or -- when the question is
        worded in synonyms -- vector similarity.

        Using the embedding to *veto* a lexical hit was tried and reverted. It
        does suppress a coincidental match ("favourite season" sharing one word
        with a stored favourite colour), but it also halves legitimate matches
        whose subject is too short or too common for the vector to corroborate
        -- "what did I use before I changed to Go?" -- and those false
        abstentions cost far more than the coincidental match does.
        """
        if not has_vectors:
            return item.lexical
        return max(item.lexical, item.semantic)

    @staticmethod
    def _prune_evidence(evidence: list[Evidence], keep_min: int = 3) -> list[Evidence]:
        """Drop evidence that neither wording nor meaning connected to the query.

        A traversal returns everything it reaches, so a deep episodic path picks
        up whole sessions of unrelated utterances. They score near zero and
        contribute nothing to the answer, but they inflate the context budget
        and bury the real matches in the UI.

        ``keep_min`` guards only the case where *nothing* matched: the caller
        has usually abstained by then, and showing the top few anyway is what
        makes the abstention inspectable rather than a blank panel.
        """
        matched = [item for item in evidence if item.lexical > 0 or item.semantic > 0]

        # A prior value shares no wording with a question that names the value
        # that replaced it -- "what did I use before I changed to ClickHouse?"
        # is asking for postgres precisely because postgres is *not* in the
        # question. Such a fact is grounded by the graph rather than by text,
        # in one of two ways:
        #
        # The test is subject+predicate rather than the SUPERSEDES edge, which
        # covers both cases and needs no extra traversal:
        #
        #   * a conflict is *detected* by subject+predicate matching with a
        #     different object, so a superseded fact always shares the slot
        #     with the fact that replaced it; and
        #   * a multi-valued predicate never supersedes at all -- "uses" allows
        #     Postgres and ClickHouse at once, and the history is recovered
        #     chronologically -- so the slot is the only link there is.
        #
        # (Evidence.supersedes holds object *names* for rendering, not ids, so
        # it cannot be used to identify nodes here.)
        slots: set[tuple[str, str]] = set()
        for item in matched:
            if item.kind == "fact" and item.predicate:
                slots.add((item.subject, item.predicate))

        kept: list[Evidence] = []
        for item in evidence:
            if item.lexical > 0 or item.semantic > 0:
                kept.append(item)
            elif item.kind == "fact" and (item.subject, item.predicate) in slots:
                item.graph_linked = True
                kept.append(item)

        # Padding up to keep_min whenever *anything* matched would put the
        # unrelated items straight back; the floor is only for the case where
        # nothing matched at all, so an abstention still shows what was walked.
        if kept:
            return kept
        return evidence[:keep_min]

    def abstention_check(
        self,
        result: RetrievalResult,
        entity_nodes_found: bool,
        has_vectors: bool = False,
        weights: dict[str, float] | None = None,
    ) -> AbstentionDecision:
        """Decide whether the retrieved subgraph can support an answer.

        Entity coverage, result count and "is there a current fact" are all
        true for practically any question a user asks of their own memory --
        they say the graph is populated, not that it is *relevant*. So they
        contribute little, and topical grounding carries the decision on a
        continuous scale. An earlier three-band version of this let a question
        clear the threshold on the always-true signals alone whenever grounding
        was merely small rather than exactly zero.
        """
        evidence = result.evidence
        query_tokens = content_tokens(result.query)
        score = 0.0
        reasons: list[str] = []

        # Signal 1 -- entity coverage. Weak: "user" almost always resolves.
        if not entity_nodes_found:
            reasons.append("No matching entities in the memory graph")
            score -= 0.25
        else:
            score += 0.10

        # Signal 2 -- did the traversal return anything at all?
        count = len(evidence)
        if count == 0:
            reasons.append("Retrieval returned no facts or utterances")
            score -= 0.45
        elif count < 3:
            score += 0.05
        else:
            score += 0.10

        # Signal 3 -- topical grounding, the decisive signal, continuous from
        # -0.45 (nothing relevant) to +0.45 (fully covered).
        topical = max((e.score for e in evidence), default=0.0)
        best_overlap = max(
            (self._grounding(e, query_tokens, has_vectors) for e in evidence),
            default=0.0,
        )
        full_cover = 0.45
        score += -0.45 + 0.9 * min(1.0, best_overlap / full_cover)
        if best_overlap <= 0.0:
            reasons.append("Nothing stored matches the subject of the question")
        elif best_overlap < full_cover / 2:
            reasons.append(
                "Only a weak match for the subject of the question"
            )

        # Signal 3b -- how much of the question the subgraph never mentions.
        #
        # Partial overlap is worth little when the missing part is the decisive
        # one. "How long have I been collecting vintage films?" matches an
        # utterance about collecting vintage *cameras* on two words out of
        # three; the word that separates a right answer from a wrong one is
        # "films", and nothing in memory contains it.
        #
        # An earlier version tested only the single highest-IDF term, which is
        # brittle: here that term was "collecting", which the near-miss
        # utterance does contain, so the check passed and the wrong answer went
        # out. Weighing *every* uncovered term is what catches the near-miss.
        #
        # Deliberately *not* overridden by embedding similarity. A near-miss is
        # precisely the case a vector cannot separate -- "vintage films" and
        # "vintage cameras" are neighbours in embedding space -- so letting a
        # high cosine cancel this signal defeats the check it exists to make.
        uncovered = 0.0
        if weights and evidence:
            # Scored against the evidence that actually composes the answer,
            # not the union of everything retrieved. On a real haystack some
            # unrelated utterance almost always contains the missing word --
            # "films" turns up in a chat about movies -- which cancelled the
            # signal while the sentence being quoted was still about cameras.
            seen: set[str] = set()
            for item in evidence[:_GROUNDING_HEAD]:
                seen.update(_stem(token) for token in content_tokens(item.text))
            total_weight = sum(weights.values())
            if total_weight > 0:
                uncovered = (
                    sum(w for token, w in weights.items() if _stem(token) not in seen)
                    / total_weight
                )
            if uncovered >= _UNCOVERED_SIGNIFICANT:
                missing = sorted(
                    (t for t in weights if _stem(t) not in seen),
                    key=lambda t: -weights[t],
                )[:3]
                reasons.append(
                    "Nothing stored mentions " + ", ".join(repr(m) for m in missing)
                )
            score -= _UNCOVERED_WEIGHT * uncovered

        # Signal 4 -- is there a current fact, or only history?
        has_current = any(e.kind == "fact" and e.is_current for e in evidence)
        if has_current:
            score += 0.05
        elif any(e.kind == "fact" for e in evidence):
            reasons.append("Only superseded facts are available")
            score -= 0.1

        # Signal 5 -- unresolved conflicts make any answer provisional.
        open_conflicts = self._open_conflicts_for(evidence)
        if open_conflicts:
            reasons.append(f"{open_conflicts} unresolved conflict(s) touch this answer")
            score -= 0.15

        threshold = self.settings.abstention_threshold
        abstain = score < threshold
        confidence = max(0.0, min(1.0, score + 0.5))
        return AbstentionDecision(
            abstain=abstain,
            confidence=confidence,
            score=score,
            reasons=reasons if abstain else [],
            threshold=threshold,
            signals={
                "entity_coverage": entity_nodes_found,
                "result_count": count,
                "top_score": round(topical, 4),
                "topical_overlap": round(best_overlap, 4),
                "uncovered_query_terms": round(uncovered, 4),
                "has_current_facts": has_current,
                "open_conflicts": open_conflicts,
            },
        )

    def _open_conflicts_for(self, evidence: list[Evidence]) -> int:
        fact_ids = [e.id for e in evidence if e.kind == "fact"]
        if not fact_ids:
            return 0
        with self.store.transaction() as tx:
            conflicts = {
                other.id
                for _, _, other in tx.expand(
                    fact_ids, [S.INVOLVES], "in", target_label=S.CONFLICT
                )
                if other.get("status") == "open"
            }
        return len(conflicts)

    # -- context + answer --------------------------------------------------

    def assemble_context(self, evidence: list[Evidence], budget: int) -> str:
        """Token-bounded context. Facts first -- they are the distilled answer."""
        lines: list[str] = []
        used = 0

        facts = [e for e in evidence if e.kind == "fact"]
        utterances = [e for e in evidence if e.kind == "utterance"]

        if facts:
            lines.append("FACTS")
            for item in facts[:12]:
                state = "CURRENT" if item.is_current else "SUPERSEDED"
                line = f"- [{state}] {item.text} (confidence {item.confidence:.2f}"
                if item.session_id:
                    line += f", session {item.session_id}"
                line += ")"
                if item.superseded_by:
                    line += f" — later replaced by: {', '.join(item.superseded_by)}"
                cost = count_tokens(line)
                if used + cost > budget:
                    break
                lines.append(line)
                used += cost

        if utterances:
            header = "\nCONVERSATION EXCERPTS"
            lines.append(header)
            used += count_tokens(header)
            for item in utterances[:15]:
                label = item.session_label or item.session_id or "unknown session"
                text = truncate(item.text, self.settings.max_utterance_chars)
                line = f"- [{label}] {item.speaker}: {text}"
                cost = count_tokens(line)
                if used + cost > budget:
                    break
                lines.append(line)
                used += cost

        return "\n".join(lines)

    def _answer(self, query: str, result: RetrievalResult) -> tuple[str, str]:
        from ..llm import get_llm

        llm = get_llm(self.settings)
        if llm is not None and result.context:
            try:
                prompt = load_prompt("answer_generation").format(
                    context=result.context, query=query
                )
                text = llm.complete(prompt, max_tokens=700, effort="low")
                if text:
                    return text.strip(), "llm"
            except Exception:
                pass
        return self._compose_answer(result), "template"

    @staticmethod
    def _history_answer(facts: list[Evidence], query_tokens: list[str]) -> str:
        """Answer from the predecessor of a fact the question names."""
        stems = {_stem(token) for token in query_tokens}

        def names_pivot(fact: Evidence) -> bool:
            return bool(stems & {_stem(t) for t in content_tokens(fact.object)})

        def compose(fact: Evidence, previous: str) -> str:
            sentence = render_fact(
                subject=fact.subject or "user",
                predicate=fact.predicate,
                obj=previous,
                past=True,
            )
            sentence = sentence[0].upper() + sentence[1:]
            parts = [f"{sentence}.", f"That later changed to {fact.object}."]
            if fact.session_id:
                parts.append(f"[{fact.session_id}]")
            return " ".join(parts)

        # Preferred route: one hop back along SUPERSEDES from the named fact.
        for fact in facts:
            if fact.is_current and fact.supersedes and names_pivot(fact):
                return compose(fact, join_objects(fact.supersedes))

        # A multi-valued predicate never supersedes, so there is no edge to
        # walk. Fall back to chronology: the value recorded for the same
        # predicate immediately before the one the question names.
        for pivot in facts:
            if not names_pivot(pivot) or not pivot.timestamp:
                continue
            earlier = [
                f
                for f in facts
                if f.id != pivot.id
                and f.predicate == pivot.predicate
                and f.timestamp
                and f.timestamp < pivot.timestamp
                and not names_pivot(f)
            ]
            if earlier:
                latest = max(earlier, key=lambda f: f.timestamp)
                return compose(pivot, latest.object)
        return ""

    @staticmethod
    def _siblings(
        facts: list[Evidence], chosen: Evidence, narrow: bool
    ) -> list[Evidence]:
        """Other current facts that belong in the same answer as ``chosen``."""
        base = _split_predicate(chosen.predicate)[0]
        out: list[Evidence] = []
        for fact in facts:
            if fact.id == chosen.id or not fact.is_current:
                continue
            if fact.polarity != chosen.polarity:
                continue
            if narrow:
                if fact.predicate != chosen.predicate:
                    continue
            elif _split_predicate(fact.predicate)[0] != base:
                continue
            out.append(fact)
        return out[:4]

    def _compose_answer(self, result: RetrievalResult) -> str:
        """Deterministic, fully grounded answer used when no LLM is configured."""
        if not result.evidence:
            return ABSTENTION_ANSWER

        facts = [e for e in result.evidence if e.kind == "fact"]
        utterances = [e for e in result.evidence if e.kind == "utterance"]
        top = result.evidence[0]

        # "What did I use before I switched to Go?" names Go as a pivot, not as
        # the answer. Walking SUPERSEDES back from the fact it names is exact
        # where lexical matching would just return the pivot itself.
        if result.prefer_history:
            historical = self._history_answer(facts, content_tokens(result.query))
            if historical:
                return historical

        # When a conversation excerpt is the strongest match -- typical for
        # temporal questions, where the dated sentence *is* the answer -- quote
        # it rather than falling back to a distilled fact.
        if top.kind == "utterance" and (not facts or top.score > facts[0].score):
            label = top.session_label or top.session_id or "an earlier session"
            speaker = top.speaker or "the user"
            return f'In {label}, {speaker} said: "{truncate(top.text, 300)}"'

        if not facts:
            if utterances:
                best = utterances[0]
                label = best.session_label or best.session_id or "an earlier session"
                return f'In {label}, {best.speaker} said: "{truncate(best.text, 300)}"'
            return ABSTENTION_ANSWER

        chosen = next((f for f in facts if f.is_current), facts[0])

        # A multi-valued predicate ("uses") should answer with everything that
        # is still true, not with whichever value happened to rank first.
        # Prefer the narrow grouping (same full predicate, so "what database"
        # lists only databases) and widen to the base predicate only when that
        # leaves nothing to add.
        siblings = self._siblings(facts, chosen, narrow=True)
        if not siblings:
            siblings = self._siblings(facts, chosen, narrow=False)
        objects = join_objects([chosen.object, *(s.object for s in siblings)])
        sentence = render_fact(
            subject=chosen.subject or "user",
            predicate=chosen.predicate,
            obj=objects,
            qualifier="" if siblings else chosen.qualifier,
            negated=chosen.polarity == "negative",
        )
        parts = [sentence[0].upper() + sentence[1:] + "."]

        if chosen.superseded_by:
            parts.append(
                f"This replaced an earlier value: {join_objects(chosen.superseded_by)}."
            )

        cited = [chosen, *siblings]
        history = [
            f
            for f in facts
            if not f.is_current
            and f.id != chosen.id
            and f.predicate == chosen.predicate
        ]
        if history and result.query_type == "temporal":
            parts.append(f"Previously: {join_objects([f.object for f in history][:3])}.")
            cited.extend(history[:3])

        citations = dedupe([f.session_id for f in cited if f.session_id])[:3]
        if citations:
            parts.append(f"[{', '.join(citations)}]")
        return " ".join(parts)

    # -- learning ----------------------------------------------------------

    def log_outcome(self, result: RetrievalResult, success: bool) -> None:
        self.procedural.log_outcome(
            query_type=result.query_type,
            path_name=result.retrieval_path,
            success=success,
            query_id=result.query_id,
            latency_ms=result.latency_ms,
            tokens_used=result.tokens_used,
            abstained=result.abstained,
        )
