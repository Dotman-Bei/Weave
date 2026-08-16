"""Entity and fact extraction.

Two interchangeable extractors behind one interface:

* ``RuleBasedExtractor`` -- deterministic, dependency-free, always available.
  This is the default and is what the benchmark numbers are produced with when
  no API key is configured (specification section 13's stated mitigation).
* ``LLMExtractor`` -- structured extraction via an LLM, falling back to the
  rule-based extractor on any error so ingestion can never hard-fail.

The important design point is *predicate specialisation*. A fact's predicate is
``<base>_<category>`` (``prefers_language``), optionally qualified with
``@<purpose>`` (``prefers_language@data_pipelines``). Conflict detection keys on
(subject, predicate), so "I prefer Go" vs "I prefer Python" collides and
supersedes correctly, while "I prefer tea" does not collide with either.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..config import Settings, get_settings
from ..util import canonicalize, dedupe

# ---------------------------------------------------------------------------
# Lexicon: canonical term -> (category, entity_type)
# ---------------------------------------------------------------------------

_LEXICON: dict[str, tuple[str, str]] = {}


def _register(category: str, entity_type: str, terms: Iterable[str]) -> None:
    for term in terms:
        _LEXICON[canonicalize(term)] = (category, entity_type)


_register(
    "language",
    "technology",
    [
        "python", "go", "rust", "java", "javascript", "typescript", "c",
        "c++", "c#", "ruby", "php", "swift", "kotlin", "scala", "elixir",
        "haskell", "clojure", "perl", "r", "julia", "zig", "lua", "sql",
    ],
)
_register(
    "framework",
    "technology",
    [
        "react", "vue", "svelte", "angular", "next js", "nextjs", "django",
        "flask", "fastapi", "rails", "spring", "express", "laravel",
        "pytorch", "tensorflow", "jax", "pandas", "numpy", "polars",
    ],
)
_register(
    "database",
    "technology",
    [
        "postgresql", "mysql", "mongodb", "redis", "sqlite", "cassandra",
        "neo4j", "hydradb", "elasticsearch", "clickhouse", "duckdb", "snowflake",
    ],
)
_register(
    "cloud",
    "technology",
    ["aws", "gcp", "azure", "vercel", "netlify", "heroku", "cloudflare", "fly io"],
)
_register(
    "tool",
    "technology",
    [
        "docker", "kubernetes", "terraform", "git", "github", "gitlab", "jenkins",
        "vim", "neovim", "emacs", "vscode", "visual studio code", "jetbrains",
        "airflow", "dagster", "prefect", "dbt", "kafka", "spark", "linux",
        "macos", "windows", "ubuntu", "arch linux", "figma", "notion", "slack",
    ],
)
_register(
    "beverage",
    "food",
    ["coffee", "tea", "espresso", "latte", "green tea", "black coffee", "water", "juice", "beer", "wine"],
)
_register(
    "food",
    "food",
    [
        "pizza", "sushi", "pasta", "ramen", "salad", "steak", "tacos", "curry",
        "chocolate", "peanut", "peanuts", "shellfish", "gluten", "dairy", "eggs",
    ],
)
_register(
    "color",
    "attribute",
    ["red", "blue", "green", "yellow", "purple", "orange", "black", "white", "pink", "teal"],
)
_register(
    "city",
    "place",
    [
        "london", "berlin", "paris", "lagos", "new york", "san francisco",
        "tokyo", "toronto", "amsterdam", "nairobi", "bangalore", "singapore",
        "sydney", "dublin", "lisbon", "austin", "seattle", "chicago", "boston",
    ],
)
_register(
    "sport",
    "activity",
    ["running", "cycling", "swimming", "climbing", "yoga", "football", "tennis", "basketball", "chess"],
)
_register("pet", "animal", ["dog", "cat", "parrot", "hamster", "rabbit", "fish"])

_TECH_TERMS = {term for term, (_, kind) in _LEXICON.items() if kind == "technology"}


def lookup_category(canonical_object: str) -> tuple[str, str]:
    """Return ``(category, entity_type)`` for a canonical object phrase."""
    if canonical_object in _LEXICON:
        return _LEXICON[canonical_object]
    # Try the head word of a compound phrase ("dark roast coffee" -> coffee).
    words = canonical_object.split()
    for word in reversed(words):
        if word in _LEXICON:
            return _LEXICON[word]
    return ("", "concept")


# ---------------------------------------------------------------------------
# Extraction results
# ---------------------------------------------------------------------------


@dataclass
class ExtractedEntity:
    name: str
    entity_type: str = "concept"

    @property
    def canonical(self) -> str:
        return canonicalize(self.name)


@dataclass
class ExtractedFact:
    subject: str
    predicate: str
    object: str
    confidence: float = 0.7
    evidence: str = ""
    qualifier: str = ""
    polarity: str = "positive"
    update_cue: str = ""  # "", "update", "correction"


@dataclass
class Extraction:
    entities: list[ExtractedEntity] = field(default_factory=list)
    facts: list[ExtractedFact] = field(default_factory=list)
    method: str = "rule-based"


# ---------------------------------------------------------------------------
# Rule-based extractor
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

_OBJECT = r"(?P<obj>[^.,;!?]+)"

# Adverbs that routinely sit between "I" and the verb and would otherwise
# defeat the patterns ("I *also really* like green tea").
_ADV = (
    r"(?:also\s+|really\s+|absolutely\s+|genuinely\s+|still\s+|now\s+|generally\s+"
    r"|usually\s+|definitely\s+|mostly\s+|always\s+|honestly\s+|personally\s+)*"
)


@dataclass(frozen=True)
class _Rule:
    pattern: re.Pattern[str]
    base_predicate: str
    polarity: str = "positive"
    entity_type: str = ""
    category_group: str = ""


def _rule(
    regex: str,
    predicate: str,
    polarity: str = "positive",
    entity_type: str = "",
    category_group: str = "",
) -> _Rule:
    return _Rule(
        re.compile(regex, re.IGNORECASE), predicate, polarity, entity_type, category_group
    )


# Ordered: the first rule that matches a sentence wins for that sentence.
_RULES: tuple[_Rule, ...] = (
    _rule(r"\bmy name(?:'s| is)\s+" + _OBJECT, "name", entity_type="person"),
    _rule(r"\bi(?:'m| am)\s+called\s+" + _OBJECT, "name", entity_type="person"),
    _rule(
        r"\bmy favou?rite\s+(?P<cat>[\w\s\-]+?)\s+(?:is|are|was)\s+" + _OBJECT,
        "favorite",
        category_group="cat",
    ),
    _rule(
        r"\bi(?:'m| am)\s+allergic\s+to\s+" + _OBJECT,
        "allergic_to",
        entity_type="food",
    ),
    _rule(
        r"\bi\s+" + _ADV + r"(?:live|reside)\s+in\s+" + _OBJECT,
        "lives_in",
        entity_type="place",
    ),
    _rule(
        r"\bi(?:'ve| have)?\s*(?:just\s+)?(?:moved|relocated)\s+to\s+" + _OBJECT,
        "lives_in",
        entity_type="place",
    ),
    _rule(r"\bi(?:'m| am)\s+based\s+in\s+" + _OBJECT, "lives_in", entity_type="place"),
    _rule(
        r"\bi\s+" + _ADV + r"work\s+(?:at|for)\s+" + _OBJECT,
        "works_at",
        entity_type="organization",
    ),
    _rule(
        r"\bi\s+" + _ADV + r"(?:no longer|do ?n[o']?t)\s+(?:like|enjoy)\s+" + _OBJECT,
        "likes",
        polarity="negative",
    ),
    _rule(
        r"\bi\s+" + _ADV + r"(?:dislike|hate|can[' ]?t stand|avoid)\s+" + _OBJECT,
        "likes",
        polarity="negative",
    ),
    _rule(
        r"\bi(?:'ve| have)?\s*(?:switched|moved|migrated)\s+(?:over\s+)?to\s+" + _OBJECT,
        "prefers",
    ),
    _rule(r"\bi\s+" + _ADV + r"(?:prefer|favou?r)\s+" + _OBJECT, "prefers"),
    _rule(
        r"\bi\s+" + _ADV + r"(?:like|love|enjoy)\s+" + _OBJECT,
        "likes",
    ),
    _rule(
        r"\bi(?:'ve| have)\s+been\s+(?:learning|studying|picking up)\s+" + _OBJECT,
        "learning",
    ),
    _rule(
        r"\bi(?:'ve| have)\s+been\s+(?:using|working with|running)\s+" + _OBJECT,
        "uses",
    ),
    _rule(
        r"\bi\s+started\s+(?:learning|studying)\s+" + _OBJECT,
        "learning",
    ),
    _rule(
        r"\bi(?:'m| am)\s+(?:currently\s+)?(?:learning|studying)\s+" + _OBJECT,
        "learning",
    ),
    _rule(
        r"\bi(?:'m| am)\s+(?:currently\s+)?(?:using|working with|building with)\s+"
        + _OBJECT,
        "uses",
    ),
    _rule(r"\bi\s+" + _ADV + r"(?:use|run)\s+" + _OBJECT, "uses"),
    _rule(
        r"\bi\s+" + _ADV + r"(?:have|own|adopted|got)\s+(?:a|an|the)?\s*" + _OBJECT,
        "has",
    ),
    _rule(
        r"\bi(?:'m| am)\s+(?:a|an)\s+" + _OBJECT,
        "role",
        entity_type="role",
    ),
)

_UPDATE_CUES = (
    "switched", "moved to", "migrated", "no longer", "used to", "these days",
    "now ", "nowadays", "changed", "instead", "anymore", "recently",
)
_CORRECTION_CUES = ("actually", "i meant", "correction", "to be clear", "sorry, i")

_TRAILING_FILLER = re.compile(
    r"\s+(?:now|these days|nowadays|anymore|instead|lately|recently|a lot|too|"
    r"as well|though|actually|honestly|really|of course)\b\.?$",
    re.IGNORECASE,
)
_LEADING_FILLER = re.compile(
    r"^(?:to\s+use\s+|using\s+|the\s+|a\s+|an\s+|my\s+|really\s+|definitely\s+|"
    r"generally\s+|usually\s+|mostly\s+|always\s+|just\s+|only\s+)+",
    re.IGNORECASE,
)
_COMPARISON = re.compile(
    r"\s+(?:over|rather than|instead of|compared to|as opposed to)\s+.*$",
    re.IGNORECASE,
)
_CLAUSE = re.compile(
    r"\s+(?:because|since|so that|although|though|but|while|which|that\s+is)\s+.*$",
    re.IGNORECASE,
)
_QUALIFIER = re.compile(r"\s+(?:for|when|during|on)\s+(?P<q>.+)$", re.IGNORECASE)

# A sentence can carry several assertions. Split where a new first-person
# clause starts, so "I like tea, but I hate coffee" yields two facts.
_CLAUSE_SPLIT = re.compile(
    r";"
    r"|,\s*(?:but|and|though|although|while|so)\s+(?=(?:i|my)\b)"
    r"|\s+(?:but|and)\s+(?=(?:i|my)\b)"
    r"|,\s*(?=(?:i|my)\b)",
    re.IGNORECASE,
)

# Time and frequency tails are never part of the object.
_TRAILING_TIME = re.compile(
    r"\s+(?:"
    r"(?:last|this|next|past)\s+(?:week|month|year|night|summer|winter|spring|fall)"
    r"|yesterday|today|tomorrow|tonight"
    r"|every\s*day|everyday|daily|weekly|monthly|all\s+the\s+time"
    r"|in\s+(?:january|february|march|april|may|june|july|august|september|october"
    r"|november|december)"
    r"|in\s+\d{4}|since\s+\d{4}"
    r"|for\s+(?:a\s+)?(?:while|years?|months?|weeks?|now)"
    r"|at\s+the\s+moment"
    r"|at\s+the\s+(?:start|end|beginning)\s+of\s+(?:the\s+)?"
    r"(?:month|week|year|quarter|summer|winter)"
    r"|(?:last|this|next)\s+weekend|over\s+the\s+weekend"
    r"|a\s+(?:few\s+)?(?:days?|weeks?|months?|years?)\s+ago"
    r"|recently|lately|nowadays|these\s+days"
    r")\b\.?$",
    re.IGNORECASE,
)

# "Postgres and Docker" is two objects, not one.
_CONJUNCT_SPLIT = re.compile(r"\s*(?:,|\band\b|&|\+)\s*", re.IGNORECASE)

_PROPER_NOUN = re.compile(r"\b([A-Z][a-zA-Z0-9+#.\-]{1,}(?:\s+[A-Z][a-zA-Z0-9+#.\-]+)*)")
_STOP_PROPER = {
    "i", "i'm", "the", "a", "an", "my", "it", "we", "they", "he", "she", "you",
    "this", "that", "these", "those", "there", "here", "what", "when", "where",
    "who", "how", "why", "and", "but", "so", "if", "yes", "no", "ok", "okay",
    "hi", "hello", "hey", "thanks", "thank", "please", "sure", "great",
}


def _strip_tail(text: str) -> str:
    """Repeatedly remove trailing filler and time expressions."""
    previous = None
    while previous != text:
        previous = text
        text = _TRAILING_TIME.sub("", text).strip()
        text = _TRAILING_FILLER.sub("", text).strip()
    return text


def _clean_object(raw: str) -> tuple[list[str], str]:
    """Return ``(objects, qualifier)`` from a captured object phrase.

    Several objects come back when the phrase coordinates them ("Postgres and
    Docker"); each becomes its own fact so they can be superseded separately.
    """
    text = _COMPARISON.sub("", _CLAUSE.sub("", raw.strip()))
    # Strip the time tail before splitting off the qualifier, so
    # "go for pipelines last month" yields the qualifier "pipelines".
    text = _strip_tail(text)

    qualifier = ""
    match = _QUALIFIER.search(text)
    if match:
        qualifier = match.group("q").strip()
        text = text[: match.start()].strip()

    # "a dog named Mira" keeps the name: it is the part a later question is
    # most likely to ask about, and `has_*` is multi-valued so it costs nothing.
    text = _LEADING_FILLER.sub("", text)
    text = _strip_tail(text).strip(" .,;:!?\"'")

    qualifier = _strip_tail(_LEADING_FILLER.sub("", qualifier)).strip(" .,;:!?\"'")
    # A qualifier longer than a short phrase is prose, not a purpose tag.
    if len(qualifier.split()) > 4:
        qualifier = ""

    objects = [
        part.strip(" .,;:!?\"'")
        for part in _CONJUNCT_SPLIT.split(text)
        if part.strip(" .,;:!?\"'")
    ]
    return objects[:4], qualifier


def _detect_cue(sentence: str) -> str:
    lowered = sentence.lower()
    for cue in _CORRECTION_CUES:
        if cue in lowered:
            return "correction"
    for cue in _UPDATE_CUES:
        if cue in lowered:
            return "update"
    return ""


class RuleBasedExtractor:
    """Deterministic pattern extraction. No network, no dependencies."""

    method = "rule-based"

    def extract(self, text: str, speaker: str = "user") -> Extraction:
        entities: list[ExtractedEntity] = []
        facts: list[ExtractedFact] = []

        for sentence in self._sentences(text):
            entities.extend(self._entities_in(sentence))
            # Facts are attributed to the user, so only user turns assert them.
            # An assistant suggestion is not a user preference.
            if speaker != "user":
                continue
            for clause in _CLAUSE_SPLIT.split(sentence):
                clause = clause.strip()
                if not clause:
                    continue
                found = self._facts_in(clause, sentence)
                if found:
                    facts.extend(found)
                    entities.append(ExtractedEntity(name="user", entity_type="person"))
                    for fact in found:
                        entities.append(
                            ExtractedEntity(
                                name=fact.object,
                                entity_type=lookup_category(fact.object)[1],
                            )
                        )

        return Extraction(
            entities=self._dedupe_entities(entities),
            facts=facts,
            method=self.method,
        )

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _sentences(text: str) -> list[str]:
        return [s.strip() for s in _SENTENCE_SPLIT.split(text or "") if s.strip()]

    def _facts_in(self, clause: str, sentence: str) -> list[ExtractedFact]:
        """First matching rule wins for a clause; coordinated objects fan out.

        The qualifier is deliberately *not* folded into the predicate. Conflict
        detection keys on (subject, predicate), and "I prefer Go for pipelines"
        must supersede "I prefer Python for data pipelines" even though the two
        purpose phrases are worded differently.
        """
        for rule in _RULES:
            match = rule.pattern.search(clause)
            if not match:
                continue
            objects, qualifier = _clean_object(match.groupdict().get("obj") or "")

            explicit_category = ""
            if rule.category_group:
                raw_category = (match.groupdict().get(rule.category_group) or "").strip()
                explicit_category = canonicalize(raw_category).replace(" ", "_")

            facts: list[ExtractedFact] = []
            for obj in objects:
                canonical = canonicalize(obj)
                if not canonical or len(canonical) > 80 or canonical in _STOP_PROPER:
                    continue
                category = explicit_category or lookup_category(canonical)[0]
                predicate = (
                    f"{rule.base_predicate}_{category}"
                    if category
                    else rule.base_predicate
                )
                facts.append(
                    ExtractedFact(
                        subject="user",
                        predicate=predicate,
                        object=canonical,
                        confidence=0.7,
                        evidence=sentence.strip(),
                        qualifier=qualifier,
                        polarity=rule.polarity,
                        update_cue=_detect_cue(sentence),
                    )
                )
            if facts:
                return facts
        return []

    def _entities_in(self, sentence: str) -> list[ExtractedEntity]:
        found: list[ExtractedEntity] = []
        lowered = sentence.lower()

        for term in _LEXICON:
            if not term:
                continue
            if re.search(rf"(?<![\w]){re.escape(term)}(?![\w])", lowered):
                category, entity_type = _LEXICON[term]
                found.append(ExtractedEntity(name=term, entity_type=entity_type))

        for match in _PROPER_NOUN.finditer(sentence):
            candidate = match.group(1).strip()
            if canonicalize(candidate) in _STOP_PROPER:
                continue
            if match.start() == 0 and len(candidate.split()) == 1:
                continue  # sentence-initial capital carries no signal
            if canonicalize(candidate) in _LEXICON:
                continue
            found.append(ExtractedEntity(name=candidate, entity_type="named_entity"))

        return found

    @staticmethod
    def _dedupe_entities(entities: list[ExtractedEntity]) -> list[ExtractedEntity]:
        seen: dict[str, ExtractedEntity] = {}
        for entity in entities:
            key = entity.canonical
            if key and key not in seen:
                seen[key] = entity
        return list(seen.values())


# ---------------------------------------------------------------------------
# LLM extractor
# ---------------------------------------------------------------------------

def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


# Strict JSON Schema, enforced by the Messages API's structured outputs, so the
# extractor never has to repair malformed model output.
EXTRACTION_SCHEMA: dict[str, Any] = _obj(
    {
        "entities": {
            "type": "array",
            "items": _obj(
                {"name": {"type": "string"}, "entity_type": {"type": "string"}}
            ),
        },
        "facts": {
            "type": "array",
            "items": _obj(
                {
                    "subject": {"type": "string"},
                    "predicate": {"type": "string"},
                    "object": {"type": "string"},
                    "qualifier": {"type": "string"},
                    "polarity": {"type": "string", "enum": ["positive", "negative"]},
                    "evidence": {"type": "string"},
                }
            ),
        },
    }
)


class LLMExtractor:
    """Structured extraction via an LLM, with a rule-based safety net."""

    method = "llm-extract"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.fallback = RuleBasedExtractor()

    def extract(self, text: str, speaker: str = "user") -> Extraction:
        from ..llm import get_llm

        llm = get_llm(self.settings)
        if llm is None or not text.strip():
            return self.fallback.extract(text, speaker)

        from ..prompts import load_prompt

        prompt = load_prompt("fact_extraction").format(
            schema=json.dumps(EXTRACTION_SCHEMA, indent=2), speaker=speaker, text=text
        )
        try:
            raw = llm.complete_json(prompt, schema=EXTRACTION_SCHEMA)
        except Exception:
            return self.fallback.extract(text, speaker)
        if not isinstance(raw, dict):
            return self.fallback.extract(text, speaker)

        extraction = self._parse(raw, speaker)
        # Union with rules: the LLM is better at novel phrasings, the rules are
        # better at consistent predicate naming. Keeping both raises recall.
        rule_based = self.fallback.extract(text, speaker)
        merged_facts = {
            (f.subject, f.predicate, f.object): f
            for f in rule_based.facts + extraction.facts
        }
        merged_entities = {
            e.canonical: e for e in rule_based.entities + extraction.entities
        }
        return Extraction(
            entities=list(merged_entities.values()),
            facts=list(merged_facts.values()),
            method=self.method,
        )

    def _parse(self, raw: dict[str, Any], speaker: str) -> Extraction:
        entities = [
            ExtractedEntity(
                name=str(item.get("name", "")).strip(),
                entity_type=str(item.get("entity_type", "concept")).strip() or "concept",
            )
            for item in raw.get("entities", [])
            if isinstance(item, dict) and str(item.get("name", "")).strip()
        ]
        facts: list[ExtractedFact] = []
        if speaker == "user":
            for item in raw.get("facts", []):
                if not isinstance(item, dict):
                    continue
                subject = canonicalize(str(item.get("subject", "user")))
                predicate = canonicalize(str(item.get("predicate", ""))).replace(" ", "_")
                obj = canonicalize(str(item.get("object", "")))
                if not (subject and predicate and obj):
                    continue
                qualifier = str(item.get("qualifier", "") or "").strip()
                facts.append(
                    ExtractedFact(
                        subject=subject,
                        predicate=predicate,
                        object=obj,
                        confidence=0.75,
                        evidence=str(item.get("evidence", "")).strip(),
                        qualifier=qualifier,
                        polarity=str(item.get("polarity", "positive")).strip()
                        or "positive",
                    )
                )
        return Extraction(entities=entities, facts=facts, method=self.method)


# ---------------------------------------------------------------------------
# Predicate arity
#
# A functional predicate holds exactly one value at a time: you live in one
# city, you have one favourite colour. A second value supersedes the first, so
# it is a conflict. Non-functional predicates accumulate -- liking tea does not
# contradict disliking coffee -- so a differing object is simply another fact.
# ---------------------------------------------------------------------------

_FUNCTIONAL_PREFIXES = (
    "prefers",
    "name",
    "lives_in",
    "works_at",
    "favorite",
    "favourite",
    "role",
)


def is_functional(predicate: str) -> bool:
    """True when a new object for this predicate supersedes the previous one."""
    return predicate.split("@", 1)[0].startswith(_FUNCTIONAL_PREFIXES)


def get_extractor(settings: Settings | None = None):
    settings = settings or get_settings()
    if settings.has_llm:
        return LLMExtractor(settings)
    return RuleBasedExtractor()


def extract_query_entities(query: str) -> list[str]:
    """Canonical entity names mentioned in a query, for retrieval anchoring."""
    extractor = RuleBasedExtractor()
    found = [e.canonical for e in extractor.extract(query, speaker="query").entities]
    lowered = query.lower()
    if re.search(r"\b(i|me|my|mine|myself)\b", lowered):
        found.append("user")
    return dedupe(found)
