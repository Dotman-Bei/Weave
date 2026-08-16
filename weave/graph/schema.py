"""Label, relationship and index definitions for the Weave graph.

Mirrors the build specification's section 4 schema. Kept in one place so the
embedded engine and the HydraDB engine provably create the same indexes.
"""

from __future__ import annotations

# --- Episodic layer ---------------------------------------------------------
SESSION = "Session"
TURN = "Turn"
UTTERANCE = "Utterance"

# --- Semantic layer ---------------------------------------------------------
ENTITY = "Entity"
FACT = "Fact"
CONFLICT = "Conflict"

# --- Procedural layer -------------------------------------------------------
QUERY_TYPE = "QueryType"
RETRIEVAL_PATH = "RetrievalPath"
OUTCOME = "Outcome"

EPISODIC_LABELS = (SESSION, TURN, UTTERANCE)
SEMANTIC_LABELS = (ENTITY, FACT, CONFLICT)
PROCEDURAL_LABELS = (QUERY_TYPE, RETRIEVAL_PATH, OUTCOME)
ALL_LABELS = EPISODIC_LABELS + SEMANTIC_LABELS + PROCEDURAL_LABELS

LAYER_OF_LABEL = {
    **{label: "episodic" for label in EPISODIC_LABELS},
    **{label: "semantic" for label in SEMANTIC_LABELS},
    **{label: "procedural" for label in PROCEDURAL_LABELS},
}

# --- Relationship types -----------------------------------------------------
HAS_TURN = "HAS_TURN"
HAS_UTTERANCE = "HAS_UTTERANCE"
NEXT = "NEXT"
PREVIOUS = "PREVIOUS"
MENTIONS = "MENTIONS"

HAS_FACT = "HAS_FACT"
DERIVED_FROM = "DERIVED_FROM"
SUPERSEDES = "SUPERSEDES"
CONFLICTS_WITH = "CONFLICTS_WITH"
INVOLVES = "INVOLVES"
RESOLVED_TO = "RESOLVED_TO"

BEST_PATH_FOR = "BEST_PATH_FOR"
TRIED = "TRIED"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"

ALL_REL_TYPES = (
    HAS_TURN,
    HAS_UTTERANCE,
    NEXT,
    PREVIOUS,
    MENTIONS,
    HAS_FACT,
    DERIVED_FROM,
    SUPERSEDES,
    CONFLICTS_WITH,
    INVOLVES,
    RESOLVED_TO,
    BEST_PATH_FOR,
    TRIED,
    SUCCEEDED,
    FAILED,
)

# --- Indexes ----------------------------------------------------------------
# (index_name, label, property)
INDEX_DEFINITIONS: tuple[tuple[str, str, str], ...] = (
    # Entity lookup
    ("entity_name_index", ENTITY, "canonical_name"),
    ("entity_type_index", ENTITY, "entity_type"),
    # Temporal queries
    ("fact_valid_from_index", FACT, "valid_from"),
    ("fact_valid_until_index", FACT, "valid_until"),
    ("fact_is_current_index", FACT, "is_current"),
    ("fact_subject_index", FACT, "subject"),
    ("fact_predicate_index", FACT, "predicate"),
    # Session queries
    ("session_time_index", SESSION, "start_time"),
    ("session_user_index", SESSION, "user_id"),
    ("turn_time_index", TURN, "timestamp"),
    # Conflict resolution
    ("conflict_status_index", CONFLICT, "status"),
    ("conflict_entity_index", CONFLICT, "entity_id"),
    # Procedural learning
    ("query_type_name_index", QUERY_TYPE, "name"),
    ("retrieval_path_name_index", RETRIEVAL_PATH, "name"),
    ("outcome_path_index", OUTCOME, "retrieval_path_id"),
    ("outcome_time_index", OUTCOME, "timestamp"),
)


def cypher_index_statements() -> list[str]:
    """The OpenCypher DDL, as executed against HydraDB.

    ``IF NOT EXISTS`` keeps this idempotent: schema setup runs on every start
    and on a second call the plain form errors, which previously left the
    caller believing no indexes existed at all.
    """
    return [
        f"CREATE INDEX {name} IF NOT EXISTS FOR (n:{label}) ON (n.{prop})"
        for name, label, prop in INDEX_DEFINITIONS
    ]
