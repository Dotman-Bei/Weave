# Weave — Build Specification
## Three-Layer Cognitive Memory with Cross-Session Fact Consolidation
### Hack Hydra | Track 3: Memory & Context Retrieval

---

## 1. Executive Summary

**Weave** is a graph-native agent memory layer that models human-like memory consolidation using three interconnected graph layers stored in HydraDB. It solves the core failure modes of current LLM memory systems: overwritten information loss, cross-session isolation, abstention hallucination, and one-size-fits-all retrieval.

**What it does:**
- Preserves every conversation as an immutable **episodic graph**
- Consolidates scattered observations into unified **semantic facts** via background conflict resolution
- Learns which retrieval strategy works best for each query type via a **procedural memory layer**
- Explicitly abstains when information is missing — before calling the LLM

**Why it wins Track 3:**
- Directly addresses all Track 3 requirements (overwritten info, chronology, cross-session synthesis, abstention)
- Uses HydraDB as a graph-native substrate — not a vector store with graph sprinkled on top
- Benchmarks against LongMemEval with measurable improvements over full-context baselines
- Novel architecture: no other open-source memory system uses a three-layer cognitive model with learned retrieval routing

---

## 2. Problem Statement

Current agent memory systems fail on four dimensions critical to Track 3:

| Failure | Current State | Consequence |
|---------|--------------|-------------|
| **Overwrites destroy history** | Flat KV stores overwrite old facts | Agent forgets evolution, retrieves stale versions |
| **Cross-session isolation** | Sessions stored as separate vectors | Facts from session 3 and 12 never synthesized |
| **Abstention failure** | No mechanism to detect missing info | LLM hallucinates when answer is not in history |
| **Fixed retrieval** | Same strategy for all query types | Temporal questions use semantic search; preferences use full-text |

**Track 3 constraints:**
- 30–40 sessions, 115,000 tokens per question
- Long-context models drop 30–60% accuracy
- Must track overwritten information
- Must maintain chronological order
- Must abstain when answer is absent

---

## 3. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              WEAVE                                       │
│                    Three-Layer Cognitive Memory                          │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│   ┌─────────────┐    ┌─────────────┐    ┌─────────────┐               │
│   │  EPISODIC   │    │  SEMANTIC   │    │ PROCEDURAL  │               │
│   │   LAYER     │◄──►│   LAYER     │◄──►│   LAYER     │               │
│   │  (Raw)      │    │ (Facts)     │    │ (Strategy)  │               │
│   └──────┬──────┘    └──────┬──────┘    └──────┬──────┘               │
│          │                  │                  │                       │
│          ▼                  ▼                  ▼                       │
│   ┌─────────────────────────────────────────────────────┐             │
│   │                    HydraDB Graph                     │             │
│   │  (OpenCypher · GraphBLAS · Object-Store Backed)    │             │
│   └─────────────────────────────────────────────────────┘             │
│                                                                         │
│   ┌─────────────┐    ┌─────────────┐    ┌─────────────┐               │
│   │  Ingestion  │    │ Consolidation│   │   Query     │               │
│   │  Pipeline   │───►│  Pipeline   │───►│  Pipeline   │               │
│   │  (Hot)      │    │ (Background)│    │  (Hot)      │               │
│   └─────────────┘    └─────────────┘    └─────────────┘               │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### 3.1 Layer Definitions

#### Episodic Layer — "What happened"
- **Purpose:** Immutable record of every conversation
- **Nodes:** `Session`, `Turn`, `Utterance`
- **Edges:** `HAS_TURN`, `HAS_UTTERANCE`, `NEXT`, `PREVIOUS`
- **Properties:** `timestamp`, `session_id`, `speaker`, `text`, `token_count`
- **Query pattern:** "What did I say about Go in session 12?"

#### Semantic Layer — "What is true"
- **Purpose:** Consolidated facts extracted from episodes
- **Nodes:** `Entity`, `Fact`, `Attribute`
- **Edges:** `HAS_FACT`, `DERIVED_FROM`, `SUPERSEDES`, `CONFLICTS_WITH`, `RESOLVED_TO`
- **Properties:** `confidence`, `valid_from`, `valid_until`, `source_sessions[]`, `extraction_method`
- **Query pattern:** "What language does the user prefer for pipelines?"

#### Procedural Layer — "How to find it"
- **Purpose:** Learned retrieval strategies per query type
- **Nodes:** `QueryType`, `RetrievalPath`, `Outcome`
- **Edges:** `BEST_PATH_FOR`, `TRIED`, `SUCCEEDED`, `FAILED`
- **Properties:** `success_rate`, `avg_latency`, `avg_token_count`, `last_used`
- **Query pattern:** "This is a temporal question → use episodic layer, depth 3, chronological sort"

### 3.2 Data Flow

```
INBOUND CHAT
     │
     ▼
┌─────────────┐
│  EPISODIC   │ ◄── Store raw session immediately (hot path)
│   GRAPH     │
└──────┬──────┘
       │
       │ (trigger)
       ▼
┌─────────────┐
│  ENTITY     │ ◄── Extract entities & relations from new utterances
│ EXTRACTION  │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  SEMANTIC   │ ◄── Merge into existing facts or create new ones
│  MERGE/     │     Detect conflicts, create CONFLICT nodes
│  CONFLICT   │
└──────┬──────┘
       │
       │ (scheduled / threshold-triggered)
       ▼
┌─────────────┐
│ CONSOLIDATION│ ◄── Background "sleep" process
│   (Sleep)   │     Resolve conflicts, update confidence scores
└──────┬──────┘
       │
       ▼
┌─────────────┐
│ PROCEDURAL  │ ◄── Log retrieval outcomes, update path rankings
│   LEARN     │
└─────────────┘

QUERY INBOUND
     │
     ▼
┌─────────────┐
│   QUERY     │ ◄── Classify query type (factual, temporal, preference, abstention)
│  CLASSIFY   │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│ PROCEDURAL  │ ◄── Look up best retrieval path for this query type
│   ROUTE     │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  MULTI-LAYER│ ◄── Execute HydraDB traversal across selected layers
│  RETRIEVAL  │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  ABSTENTION │ ◄── Coverage check: does retrieved subgraph contain answer?
│   CHECK     │     If no → return "I don't know" (no LLM call)
└──────┬──────┘       If yes → assemble context window → LLM
       │
       ▼
┌─────────────┐
│   ANSWER    │
│  + LOG      │ ◄── Log outcome to procedural layer for learning
└─────────────┘
```

---

## 4. HydraDB Graph Schema

### 4.1 Node Labels & Properties

```cypher
// Episodic Layer
(:Session {
  id: string,              // UUID
  user_id: string,
  start_time: datetime,
  end_time: datetime,
  total_turns: int,
  total_tokens: int,
  session_summary: string   // LLM-generated 1-sentence summary
})

(:Turn {
  id: string,
  turn_number: int,
  timestamp: datetime,
  speaker: string,          // "user" | "assistant"
  text: string,
  token_count: int
})

(:Utterance {
  id: string,
  text: string,
  embedding: vector,        // For semantic similarity fallback
  timestamp: datetime
})

// Semantic Layer
(:Entity {
  id: string,
  name: string,
  entity_type: string,      // "person", "preference", "technology", "task"
  canonical_name: string,   // Normalized form for dedup
  first_seen: datetime,
  last_seen: datetime,
  mention_count: int
})

(:Fact {
  id: string,
  subject: string,
  predicate: string,
  object: string,
  confidence: float,        // 0.0–1.0
  valid_from: datetime,
  valid_until: datetime,    // NULL if currently valid
  is_current: boolean,
  extraction_method: string, // "llm-extract", "rule-based", "consolidated"
  source_sessions: [string]  // Session IDs this fact was derived from
})

(:Conflict {
  id: string,
  conflict_type: string,    // "contradiction", "update", "correction"
  detected_at: datetime,
  resolved_at: datetime,
  resolution_policy: string, // "recency", "frequency", "trust", "manual"
  status: string            // "open", "resolved", "superseded"
})

// Procedural Layer
(:QueryType {
  id: string,
  name: string,             // "factual", "temporal", "preference", "procedural"
  description: string,
  keywords: [string]        // Trigger words for classification
})

(:RetrievalPath {
  id: string,
  name: string,             // "semantic-only", "episodic-depth-3", "hybrid-conflict"
  layers: [string],         // ["episodic", "semantic"]
  max_depth: int,
  cypher_template: string,  // Parameterized Cypher query
  use_conflict_resolution: boolean
})

(:Outcome {
  id: string,
  query_id: string,
  retrieval_path_id: string,
  success: boolean,         // Did the LLM produce a correct answer?
  latency_ms: int,
  tokens_used: int,
  timestamp: datetime
})
```

### 4.2 Relationship Types & Properties

```cypher
// Episodic relationships
(:Session)-[:HAS_TURN {turn_number: int}]->(:Turn)
(:Turn)-[:HAS_UTTERANCE {utterance_number: int}]->(:Utterance)
(:Turn)-[:NEXT]->(:Turn)
(:Turn)-[:PREVIOUS]->(:Turn)
(:Utterance)-[:MENTIONS]->(:Entity)

// Cross-layer relationships
(:Fact)-[:DERIVED_FROM]->(:Utterance)
(:Fact)-[:DERIVED_FROM]->(:Session)
(:Entity)-[:HAS_FACT {confidence: float}]->(:Fact)

// Temporal / conflict relationships
(:Fact)-[:SUPERSEDES {superseded_at: datetime}]->(:Fact)
(:Fact)-[:CONFLICTS_WITH {detected_at: datetime}]->(:Fact)
(:Conflict)-[:INVOLVES]->(:Fact)
(:Conflict)-[:RESOLVED_TO {resolved_at: datetime}]->(:Fact)

// Procedural relationships
(:QueryType)-[:BEST_PATH_FOR {success_rate: float, last_updated: datetime}]->(:RetrievalPath)
(:RetrievalPath)-[:TRIED]->(:Outcome)
(:RetrievalPath)-[:SUCCEEDED]->(:Outcome)
(:RetrievalPath)-[:FAILED]->(:Outcome)
```

### 4.3 Index Definitions

```cypher
// Entity lookup
CREATE INDEX entity_name_index FOR (e:Entity) ON (e.canonical_name);
CREATE INDEX entity_type_index FOR (e:Entity) ON (e.entity_type);

// Temporal queries
CREATE INDEX fact_valid_from_index FOR (f:Fact) ON (f.valid_from);
CREATE INDEX fact_valid_until_index FOR (f:Fact) ON (f.valid_until);
CREATE INDEX fact_is_current_index FOR (f:Fact) ON (f.is_current);

// Session queries
CREATE INDEX session_time_index FOR (s:Session) ON (s.start_time);
CREATE INDEX turn_time_index FOR (t:Turn) ON (t.timestamp);

// Conflict resolution
CREATE INDEX conflict_status_index FOR (c:Conflict) ON (c.status);
CREATE INDEX conflict_entity_index FOR (c:Conflict) ON (c.entity_id);

// Procedural learning
CREATE INDEX outcome_path_index FOR (o:Outcome) ON (o.retrieval_path_id);
CREATE INDEX outcome_time_index FOR (o:Outcome) ON (o.timestamp);
```

---

## 5. Core Algorithms

### 5.1 Entity Extraction & Semantic Merge

**Input:** New utterance text + session context
**Output:** Created/updated Entity and Fact nodes

```python
def ingest_utterance(session_id: str, turn_id: str, text: str):
    """
    1. Extract entities and relations from text using LLM
    2. For each extracted entity:
       a. Check if canonical form already exists in HydraDB
       b. If yes: link utterance to existing Entity
       c. If no: create new Entity node
    3. For each extracted fact (subject, predicate, object):
       a. Check Semantic layer for existing fact on same (subject, predicate)
       b. If no existing fact: create new Fact, mark is_current=true
       c. If existing fact with same object: increment confidence, add source
       d. If existing fact with different object: create CONFLICT node
    """

    # Step 1: LLM extraction
    extraction = llm_extract(text, schema=EXTRACTION_SCHEMA)

    with hydra_db.transaction() as tx:
        for entity in extraction.entities:
            canonical = canonicalize(entity.name)
            existing = tx.run("""
                MATCH (e:Entity {canonical_name: $canonical})
                RETURN e
            """, canonical=canonical).single()

            if existing:
                entity_node = existing["e"]
                tx.run("""
                    MATCH (e:Entity {id: $eid})
                    SET e.last_seen = datetime(), e.mention_count = e.mention_count + 1
                """, eid=entity_node["id"])
            else:
                entity_node = tx.run("""
                    CREATE (e:Entity {
                        id: $id, name: $name, canonical_name: $canonical,
                        entity_type: $type, first_seen: datetime(),
                        last_seen: datetime(), mention_count: 1
                    })
                    RETURN e
                """, id=uuid(), name=entity.name, canonical=canonical,
                     type=entity.type).single()["e"]

            # Link utterance to entity
            tx.run("""
                MATCH (u:Utterance {id: $uid}), (e:Entity {id: $eid})
                CREATE (u)-[:MENTIONS]->(e)
            """, uid=utterance_id, eid=entity_node["id"])

        for fact in extraction.facts:
            # Check for existing fact on same (subject, predicate)
            existing_fact = tx.run("""
                MATCH (e:Entity {canonical_name: $sub})-[:HAS_FACT]->(f:Fact)
                WHERE f.predicate = $pred AND f.is_current = true
                RETURN f
            """, sub=canonicalize(fact.subject), pred=fact.predicate).single()

            if not existing_fact:
                # New fact
                tx.run("""
                    MATCH (e:Entity {canonical_name: $sub})
                    CREATE (f:Fact {
                        id: $fid, subject: $sub, predicate: $pred, object: $obj,
                        confidence: 0.7, valid_from: datetime(),
                        valid_until: null, is_current: true,
                        extraction_method: 'llm-extract', source_sessions: [$sid]
                    })
                    CREATE (e)-[:HAS_FACT {confidence: 0.7}]->(f)
                """, sub=canonicalize(fact.subject), fid=uuid(),
                     pred=fact.predicate, obj=fact.object, sid=session_id)
            else:
                old_fact = existing_fact["f"]
                if old_fact["object"] == fact.object:
                    # Same fact — boost confidence
                    tx.run("""
                        MATCH (f:Fact {id: $fid})
                        SET f.confidence = min(1.0, f.confidence + 0.1),
                            f.source_sessions = f.source_sessions + $sid,
                            f.last_seen = datetime()
                    """, fid=old_fact["id"], sid=session_id)
                else:
                    # Conflict detected
                    create_conflict(tx, old_fact, fact, session_id)
```

### 5.2 Conflict Detection & Resolution

**Input:** Existing Fact + New Fact with same (subject, predicate) but different object
**Output:** Conflict node + optionally resolved Fact

```python
def create_conflict(tx, old_fact: dict, new_fact: dict, session_id: str):
    """
    Creates a Conflict node linking the old and new facts.
    Does NOT resolve immediately — resolution happens in background.
    """
    conflict_id = uuid()
    tx.run("""
        MATCH (old:Fact {id: $old_id}), (e:Entity {canonical_name: $sub})
        CREATE (c:Conflict {
            id: $cid, conflict_type: 'contradiction',
            detected_at: datetime(), resolved_at: null,
            resolution_policy: 'pending', status: 'open'
        })
        CREATE (c)-[:INVOLVES]->(old)
        CREATE (c)-[:INVOLVES {role: 'new'}]->(new_fact:Fact {
            id: $new_fid, subject: $sub, predicate: $pred, object: $obj,
            confidence: 0.7, valid_from: datetime(), valid_until: null,
            is_current: false, extraction_method: 'llm-extract',
            source_sessions: [$sid]
        })
        CREATE (e)-[:HAS_FACT {confidence: 0.7}]->(new_fact)
        CREATE (old)-[:CONFLICTS_WITH {detected_at: datetime()}]->(new_fact)
    """, old_id=old_fact["id"], sub=old_fact["subject"],
         cid=conflict_id, new_fid=uuid(), pred=new_fact.predicate,
         obj=new_fact.object, sid=session_id)

def resolve_conflict(conflict_id: str, policy: str = "recency"):
    """
    Background consolidation process.
    Applies resolution policy to determine which fact is current.
    """
    with hydra_db.transaction() as tx:
        facts = tx.run("""
            MATCH (c:Conflict {id: $cid})-[:INVOLVES]->(f:Fact)
            RETURN f ORDER BY f.valid_from
        """, cid=conflict_id).data()

        if policy == "recency":
            winner = facts[-1]  # Most recent
        elif policy == "frequency":
            winner = max(facts, key=lambda f: len(f["source_sessions"]))
        elif policy == "confidence":
            winner = max(facts, key=lambda f: f["confidence"])
        else:
            winner = facts[-1]  # Default recency

        # Mark winner as current, others as superseded
        for fact in facts:
            if fact["id"] == winner["id"]:
                tx.run("""
                    MATCH (f:Fact {id: $fid})
                    SET f.is_current = true, f.confidence = 0.95
                """, fid=fact["id"])
            else:
                tx.run("""
                    MATCH (f:Fact {id: $fid})
                    SET f.is_current = false, f.valid_until = datetime()
                """, fid=fact["id"])

        # Update conflict node
        tx.run("""
            MATCH (c:Conflict {id: $cid})
            SET c.status = 'resolved', c.resolved_at = datetime(),
                c.resolution_policy = $policy
            CREATE (c)-[:RESOLVED_TO]->(w:Fact {id: $wid})
        """, cid=conflict_id, policy=policy, wid=winner["id"])
```

### 5.3 Query Classification & Procedural Routing

**Input:** User query string
**Output:** QueryType + best RetrievalPath

```python
QUERY_TYPE_PATTERNS = {
    "temporal": ["when", "last time", "previously", "before", "after", "session"],
    "preference": ["prefer", "like", "want", "should I use", "recommend"],
    "factual": ["what", "who", "where", "how many", "is it"],
    "procedural": ["how do I", "steps to", "process for", "workflow"]
}

def classify_query(query: str) -> str:
    """
    Simple keyword-based classification with LLM fallback.
    """
    query_lower = query.lower()
    scores = {}
    for qtype, keywords in QUERY_TYPE_PATTERNS.items():
        scores[qtype] = sum(1 for kw in keywords if kw in query_lower)

    best = max(scores, key=scores.get)
    if scores[best] == 0:
        # LLM fallback for ambiguous queries
        best = llm_classify(query)

    return best

def get_best_retrieval_path(query_type: str) -> dict:
    """
    Looks up procedural layer for highest-success-rate path.
    Falls back to default if no learned data yet.
    """
    with hydra_db.transaction() as tx:
        result = tx.run("""
            MATCH (qt:QueryType {name: $qtype})-[r:BEST_PATH_FOR]->(rp:RetrievalPath)
            RETURN rp, r.success_rate
            ORDER BY r.success_rate DESC
            LIMIT 1
        """, qtype=query_type).single()

        if result:
            return {
                "path": result["rp"],
                "success_rate": result["r.success_rate"]
            }
        else:
            # Default paths
            defaults = {
                "temporal": "episodic-depth-3",
                "preference": "hybrid-conflict",
                "factual": "semantic-only",
                "procedural": "episodic-depth-2"
            }
            return get_default_path(defaults[query_type])
```

### 5.4 Multi-Layer Retrieval

**Input:** Query string, QueryType, RetrievalPath
**Output:** Retrieved subgraph + abstention decision

```python
def retrieve(query: str, query_type: str, entities: list) -> dict:
    """
    Executes retrieval across selected layers using HydraDB traversal.
    """
    path = get_best_retrieval_path(query_type)

    with hydra_db.transaction() as tx:
        if path["name"] == "semantic-only":
            # Direct fact lookup
            results = tx.run("""
                MATCH (e:Entity)-[:HAS_FACT]->(f:Fact)
                WHERE e.canonical_name IN $entities AND f.is_current = true
                RETURN f, e
                ORDER BY f.confidence DESC
            """, entities=entities).data()

        elif path["name"] == "episodic-depth-3":
            # Traverse from entity through mentions to utterances
            results = tx.run("""
                MATCH (e:Entity)-[:MENTIONS]-(u:Utterance)-[:HAS_UTTERANCE]-(t:Turn)
                WHERE e.canonical_name IN $entities
                RETURN t, u, e
                ORDER BY t.timestamp DESC
                LIMIT 20
            """, entities=entities).data()

        elif path["name"] == "hybrid-conflict":
            # Semantic facts + conflict history
            results = tx.run("""
                MATCH (e:Entity)-[:HAS_FACT]->(f:Fact)
                WHERE e.canonical_name IN $entities
                OPTIONAL MATCH (f)-[:SUPERSEDES|CONFLICTS_WITH]-(other:Fact)
                OPTIONAL MATCH (f)<-[:RESOLVED_TO]-(c:Conflict)
                RETURN f, other, c, e
                ORDER BY f.is_current DESC, f.confidence DESC
            """, entities=entities).data()

        elif path["name"] == "episodic-depth-2":
            # Shallow episodic search for procedural queries
            results = tx.run("""
                MATCH (e:Entity)-[:MENTIONS]-(u:Utterance)-[:HAS_UTTERANCE]-(t:Turn)-[:HAS_TURN]-(s:Session)
                WHERE e.canonical_name IN $entities
                RETURN s, t, u, e
                ORDER BY s.start_time DESC
                LIMIT 15
            """, entities=entities).data()

    return {
        "results": results,
        "path_used": path["name"],
        "entity_coverage": len(entities) > 0,
        "result_count": len(results)
    }
```

### 5.5 Abstention Router

**Input:** Retrieved subgraph + original query
**Output:** Decision (answer | abstain) + confidence score

```python
def abstention_check(retrieval_result: dict, query: str) -> dict:
    """
    Determines whether the retrieved subgraph contains sufficient
    information to answer the query. Returns abstention decision.

    Scoring signals:
    1. Entity coverage: Were ALL queried entities found in the graph?
    2. Fact recency: Are there current facts for the entities?
    3. Temporal reachability: For temporal queries, are there utterances?
    4. Conflict status: Are there unresolved conflicts?
    """
    score = 0.0
    reasons = []

    # Signal 1: Entity coverage
    if not retrieval_result["entity_coverage"]:
        reasons.append("No entities found in graph")
        score -= 0.4
    else:
        score += 0.3

    # Signal 2: Result count
    if retrieval_result["result_count"] == 0:
        reasons.append("No facts or utterances retrieved")
        score -= 0.5
    elif retrieval_result["result_count"] < 3:
        score += 0.1
    else:
        score += 0.3

    # Signal 3: Current facts check (for semantic queries)
    has_current_facts = any(
        r.get("f", {}).get("is_current", False) 
        for r in retrieval_result["results"]
    )
    if has_current_facts:
        score += 0.2
    else:
        reasons.append("No current facts found")
        score -= 0.1

    # Signal 4: Conflict check
    has_unresolved_conflicts = any(
        r.get("c", {}).get("status") == "open"
        for r in retrieval_result["results"]
    )
    if has_unresolved_conflicts:
        reasons.append("Unresolved conflicts detected")
        score -= 0.15

    # Decision threshold
    THRESHOLD = 0.3
    should_abstain = score < THRESHOLD

    return {
        "abstain": should_abstain,
        "confidence": max(0.0, min(1.0, score + 0.5)),  # Normalize to 0-1
        "reasons": reasons if should_abstain else [],
        "threshold": THRESHOLD
    }
```

---

## 6. API Specification

### 6.1 Core Endpoints

```python
# weave/api.py

from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Optional

app = FastAPI(title="Weave Memory API")

class IngestRequest(BaseModel):
    session_id: str
    turns: List[dict]  # [{"speaker": "user", "text": "..."}, ...]

class IngestResponse(BaseModel):
    session_id: str
    entities_extracted: int
    facts_created: int
    conflicts_detected: int

@app.post("/ingest", response_model=IngestResponse)
def ingest_session(req: IngestRequest):
    """
    Ingest a complete chat session into Weave.
    Creates episodic graph, extracts entities, merges into semantic layer.
    """
    pass

class QueryRequest(BaseModel):
    query: str
    user_id: Optional[str] = None
    max_tokens: int = 4000

class QueryResponse(BaseModel):
    answer: str
    abstained: bool
    abstention_reasons: List[str]
    retrieval_path: str
    facts_used: List[dict]
    tokens_used: int
    latency_ms: int

@app.post("/query", response_model=QueryResponse)
def query_memory(req: QueryRequest):
    """
    Query the memory system.
    Returns answer or abstention with explanation.
    """
    pass

class ConsolidateRequest(BaseModel):
    user_id: Optional[str] = None
    max_conflicts: int = 50

@app.post("/consolidate")
def trigger_consolidation(req: ConsolidateRequest):
    """
    Trigger background consolidation.
    Resolves conflicts, updates confidence scores, merges duplicate facts.
    """
    pass

@app.get("/health")
def health_check():
    """Returns HydraDB connection status and layer statistics."""
    pass
```

### 6.2 Internal Service Interfaces

```python
# weave/services/ingestion.py
class IngestionService:
    def process_session(self, session: Session) -> IngestionResult:
        """Full pipeline: episodic store → entity extraction → semantic merge"""

# weave/services/consolidation.py
class ConsolidationService:
    def run_sleep_cycle(self, user_id: str) -> ConsolidationResult:
        """Background conflict resolution and fact merging"""

# weave/services/retrieval.py
class RetrievalService:
    def query(self, query: str) -> RetrievalResult:
        """Classify → route → retrieve → abstention check → answer"""

# weave/services/procedural.py
class ProceduralLearningService:
    def log_outcome(self, query_type: str, path: str, success: bool):
        """Update success rates for retrieval paths"""

    def get_best_path(self, query_type: str) -> RetrievalPath:
        """Return highest-success-rate path for query type"""
```

---

## 7. Implementation Roadmap (9 Days)

## 8. HydraDB-Specific Implementation Notes

### 8.1 Connection Setup

```python
# weave/db.py
from neo4j import GraphDatabase

class HydraDBClient:
    def __init__(self, uri: str = "neo4j://localhost:7687", 
                 auth_token: str = "local-development-token-32-bytes"):
        self.driver = GraphDatabase.driver(uri, auth=("", auth_token))

    def verify(self):
        """Run round-trip write to confirm node is alive"""
        with self.driver.session() as session:
            result = session.run("RETURN 1 AS n")
            return result.single()["n"] == 1
```

### 8.2 Optimized Queries for HydraDB

Use HydraDB's native path procedures for efficient multi-hop traversal:

```cypher
// Instead of multiple MATCH hops, use algo.SSpaths for bounded traversal
CALL algo.SSpaths({
  sourceLabel: 'Entity',
  sourceProperty: 'canonical_name',
  sourceValues: ['python'],
  relTypes: ['HAS_FACT', 'DERIVED_FROM', 'MENTIONS'],
  relDirection: 'both',
  maxLen: 3,
  pathCount: 10,
  resultLimit: 50
})
YIELD path
RETURN path
```

Use `algo.MSpaths` for batch entity lookups (avoids client-side fan-out):

```cypher
CALL algo.MSpaths({
  sourceLabel: 'Entity',
  sourceProperty: 'canonical_name',
  sourceValues: ['python', 'go', 'rust'],
  targetValues: ['python', 'go', 'rust'],
  pairwise: false,
  relTypes: ['HAS_FACT', 'SUPERSEDES'],
  relDirection: 'both',
  maxLen: 2,
  pathCount: 5,
  resultLimit: 100
})
YIELD path
RETURN path
```

### 8.3 Consistency Mode

For ingestion (writes), use default causal consistency.
For benchmark queries (reads requiring latest consolidation), use strong consistency:

```python
# HTTP API consistency setting
headers = {"consistency": "strong"}
```

---

## 9. Benchmarking Strategy

### 9.1 LongMemEval Integration

```python
# weave/benchmarks/longmemeval.py
from longmemeval import load_dataset

class LongMemEvalBenchmark:
    def __init__(self, weave_client):
        self.weave = weave_client
        self.dataset = load_dataset("LongMemEval-S")  # 500 questions

    def run(self):
        results = []
        for sample in self.dataset:
            # Ingest all sessions
            for session in sample.sessions:
                self.weave.ingest(session)

            # Run consolidation
            self.weave.consolidate()

            # Query
            response = self.weave.query(sample.question)

            # Score
            correct = self.evaluate(response.answer, sample.ground_truth)
            results.append({
                "question_id": sample.id,
                "category": sample.category,
                "correct": correct,
                "abstained": response.abstained,
                "should_abstain": sample.should_abstain,
                "tokens_used": response.tokens_used,
                "latency_ms": response.latency_ms
            })

        return self.aggregate(results)

    def evaluate(self, predicted: str, ground_truth: str) -> bool:
        """Use LLM-as-judge or exact match depending on question type"""
        pass
```

### 9.2 Ablation Study Setup

```python
# weave/benchmarks/ablation.py

class AblationStudy:
    """
    Compare three configurations:
    1. Episodic-only: No semantic layer, query utterances directly
    2. Semantic-only: No episodic layer, query facts only
    3. Full Weave: All layers + procedural routing
    """

    def run_episodic_only(self, dataset):
        # Disable semantic extraction, query only (Session)-[:HAS_TURN]->(Turn)
        pass

    def run_semantic_only(self, dataset):
        # Skip episodic storage, extract facts immediately, no conflict detection
        pass

    def run_full_weave(self, dataset):
        # Full pipeline as specified
        pass
```

### 9.3 Metrics to Report

| Metric | How to Calculate |
|--------|-----------------|
| **Accuracy** | Correct answers / Total questions |
| **Abstention Precision** | Correct abstentions / Total abstentions |
| **Abstention Recall** | Correct abstentions / Questions requiring abstention |
| **Token Efficiency** | Avg tokens in context window / 115K full context |
| **Latency** | Avg ms per query (end-to-end) |
| **Conflict Resolution Accuracy** | Correctly resolved conflicts / Total conflicts |

---

## 10. Project Structure

```
weave/
├── README.md                    # Setup, run, architecture overview
├── LICENSE                      # MIT or Apache-2.0
├── pyproject.toml               # Dependencies
├── docker-compose.yml           # HydraDB + Weave services
│
├── weave/
│   ├── __init__.py
│   ├── api.py                   # FastAPI endpoints (/ingest, /query, /consolidate)
│   ├── config.py                # Settings (HydraDB URI, LLM API keys, thresholds)
│   ├── db.py                    # HydraDB connection + transaction helpers
│   │
│   ├── services/
│   │   ├── __init__.py
│   │   ├── ingestion.py         # Session → Episodic graph + entity extraction
│   │   ├── extraction.py        # LLM prompts for entity/fact extraction
│   │   ├── consolidation.py     # Background conflict resolution ("sleep")
│   │   ├── retrieval.py         # Query classify → route → retrieve → abstention
│   │   └── procedural.py        # Learned retrieval path management
│   │
│   ├── models/
│   │   ├── __init__.py
│   │   ├── episodic.py          # Session, Turn, Utterance dataclasses
│   │   ├── semantic.py          # Entity, Fact, Conflict dataclasses
│   │   └── procedural.py        # QueryType, RetrievalPath, Outcome dataclasses
│   │
│   └── prompts/
│       ├── entity_extraction.txt
│       ├── fact_extraction.txt
│       ├── query_classification.txt
│       └── answer_generation.txt
│
├── benchmarks/
│   ├── __init__.py
│   ├── longmemeval.py           # LongMemEval harness
│   ├── locomo.py                # LoCoMo harness (if time permits)
│   └── ablation.py              # Ablation study runner
│
├── scripts/
│   ├── setup_hydradb.sh         # One-command HydraDB setup
│   ├── ingest_sample.py         # Ingest demo data
│   └── run_benchmark.py         # Full benchmark execution
│
├── tests/
│   ├── test_ingestion.py
│   ├── test_conflict.py
│   ├── test_retrieval.py
│   └── test_abstention.py
│
└── data/
    ├── sample_sessions/         # Demo chat data
    └── longmemeval/             # Benchmark data (gitignored, downloaded at runtime)
```

---

## 11. Dependencies

```toml
# pyproject.toml
[project]
name = "weave"
version = "0.1.0"
dependencies = [
    "fastapi>=0.110.0",
    "uvicorn>=0.27.0",
    "neo4j>=5.18.0",           # HydraDB Bolt driver
    "pydantic>=2.6.0",
    "openai>=1.12.0",          # Entity/fact extraction
    "anthropic>=0.18.0",       # Alternative LLM
    "numpy>=1.26.0",
    "pytest>=8.0.0",
    "httpx>=0.27.0",           # Async HTTP for HydraDB REST API
    "python-dotenv>=1.0.0",
    "tiktoken>=0.6.0",         # Token counting
]

[project.optional-dependencies]
benchmark = [
    "datasets>=2.17.0",        # HuggingFace datasets for LongMemEval
    "scikit-learn>=1.4.0",
    "matplotlib>=3.8.0",
]
```

---

## 12. Testing Strategy

### 12.1 Unit Tests

```python
# tests/test_conflict.py
def test_conflict_detection():
    """Ingest two contradictory facts, verify Conflict node created"""
    weave.ingest(session_with_python_pref)
    weave.ingest(session_with_go_pref)

    conflicts = hydra.run("""
        MATCH (c:Conflict {status: 'open'})-[:INVOLVES]->(f:Fact)
        RETURN count(c) AS conflict_count
    """)
    assert conflicts.single()["conflict_count"] == 1

def test_consolidation_recency():
    """Run consolidation with recency policy, verify latest fact wins"""
    weave.consolidate(policy="recency")

    current = hydra.run("""
        MATCH (e:Entity {canonical_name: 'user'})-[:HAS_FACT]->(f:Fact)
        WHERE f.predicate = 'prefers_language' AND f.is_current = true
        RETURN f.object AS current_pref
    """).single()["current_pref"]

    assert current == "go"  # Most recent preference
```

### 12.2 Integration Tests

```python
# tests/test_retrieval.py
def test_temporal_query_uses_episodic():
    """Temporal question should route to episodic-depth-3 path"""
    response = weave.query("What did I say about Python in session 3?")
    assert response.retrieval_path == "episodic-depth-3"
    assert "session 3" in response.answer.lower()

def test_abstention_on_unknown():
    """Question about never-mentioned topic should abstain"""
    response = weave.query("What is my favorite color?")
    assert response.abstained == True
    assert "don't know" in response.answer.lower()
```

### 12.3 Benchmark Tests

```bash
# Run full LongMemEval subset
python -m benchmarks.longmemeval --dataset LongMemEval-S --output results.json

# Run ablation study
python -m benchmarks.ablation --configs episodic-only,semantic-only,full-weave
```

---

## 13. Risk Mitigation

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| LLM extraction is too slow for 9 days | Medium | Use simple regex + spaCy fallback; LLM only for complex utterances |
| HydraDB setup issues | Low | Test Docker setup on Day 1; have fallback local Bolt connection |
| LongMemEval dataset unavailable | Low | Use synthetic benchmark data with same structure; still valid |
| Conflict resolution accuracy poor | Medium | Start with recency-only; add frequency/confidence if time |
| Token budget exceeded | Low | Hard limit context assembly to 6K tokens; truncate utterances |
| Abstention threshold tuning | Medium | Start with 0.3; adjust based on Day 7 benchmark results |

---

## 14. Judging Criteria Alignment

| Judging Criteria | How Weave Addresses It |
|-----------------|----------------------|
| **Technical execution** | Full 3-layer graph architecture with background consolidation, deterministic conflict resolution, and learned retrieval routing |
| **Use of HydraDB** | Uses HydraDB's graph-native features: temporal edge traversal, multi-hop path procedures (`algo.SSpaths`, `algo.MSpaths`), property-indexed conflict lookups. Not a vector store with graph bolted on. |
| **Product completeness** | Working API with `/ingest`, `/query`, `/consolidate`. End-to-end demo. Benchmark results on LongMemEval. |
| **Quality of results** | Quantified accuracy on LongMemEval categories. Ablation studies prove each layer's contribution. Token efficiency vs. full-context baseline. |
| **Originality** | Three-layer cognitive architecture is novel in open-source agent memory. Procedural learning layer for adaptive retrieval is unique. Conflict-preservation graph (not overwrite) is distinct from Mem0/Zep. |

---

## 15. Quick Start (For the Building Agent)

```bash
# 1. Clone and setup
git clone <your-repo>
cd weave
pip install -e ".[benchmark]"

# 2. Start HydraDB
docker-compose up -d hydradb

# 3. Verify connection
python -c "from weave.db import HydraDBClient; print(HydraDBClient().verify())"

# 4. Ingest sample data
python scripts/ingest_sample.py

# 5. Run query
python -c "
from weave.api import query_memory
from weave.models.semantic import QueryRequest
r = query_memory(QueryRequest(query='What language do I prefer?'))
print(r.answer, r.abstained, r.retrieval_path)
"

# 6. Run benchmark
python scripts/run_benchmark.py --dataset longmemeval-s
```

---

*Built for Hack Hydra 2026 — Track 3: Memory & Context Retrieval*
*Project: Weave | Team: [Your Team Name]*
*HydraDB Graph Schema Version: 1.0*
