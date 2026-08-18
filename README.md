# Weave

**Three-layer cognitive memory with cross-session fact consolidation.**
Hack Hydra · Track 3: Memory & Context Retrieval

Weave is a graph-native agent memory layer. It stores every conversation as an
immutable **episodic** graph, consolidates it into **semantic** facts that
supersede rather than overwrite, and learns in a **procedural** layer which
traversal answers which kind of question. When the graph cannot support an
answer, it abstains — before assembling context and before calling a model.

```
  EPISODIC  ──▶  SEMANTIC  ──▶  PROCEDURAL
  what happened   what is true    how to find it
  Session         Entity          QueryType
  Turn            Fact            RetrievalPath
  Utterance       Conflict        Outcome
```

---

## Quick start

No database, no API key, no Docker required.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,embeddings]"   # embeddings optional; see below

weave serve                      # http://127.0.0.1:8000
```

Open the workspace, click **Load demo memory**, and ask it something. Or drive
it from the shell:

```bash
python scripts/ingest_sample.py         # ingest 8 demo sessions + consolidate
weave query "What language do I prefer for pipelines?"
weave query "Where did I live before?"
weave query "What is my blood type?"    # abstains
```

```
$ weave query "What language do I prefer for pipelines?"
User prefers go (for pipelines). [sess-05]

  abstained=False type=preference path=hybrid-conflict confidence=1.00 tokens=58
```

---

## What it does that a flat memory store does not

**Overwrites are preserved as history.** Two contradictory statements do not
mutate a row. Ingestion creates a `Conflict` node; consolidation resolves it
under an explicit policy and leaves a `SUPERSEDES` edge. The old fact keeps its
node, its evidence and its `valid_until` stamp.

```
$ weave query "Where do I live?"         →  User lives in lisbon. [sess-06]
$ weave query "Where did I live before?" →  User lives in lisbon. Previously: berlin.
                                            [sess-06, sess-01]
```

**Cross-session synthesis.** Facts stated in session 2 and session 7 are merged
into one answer with both citations:

```
$ weave query "What database do I use?"
User uses postgresql and clickhouse. [sess-02, sess-07]
```

**Abstention is decided before generation.** Entity coverage and result counts
are not enough on their own — a user always has *some* stored facts, so those
signals stay high for a question the graph has never heard of. The decisive
signal is topical overlap between the question and the retrieved subgraph. On a
miss, Weave returns "I don't know" with `tokens_used = 0`.

**Multi-valued predicates never false-conflict.** Liking tea does not contradict
disliking coffee. Predicates are classified as functional (`prefers_language`,
`lives_in_city` — one value at a time) or not (`uses_tool`, `likes_beverage` —
they accumulate), and only functional ones can conflict.

---

## Why a graph, and not a vector store

Every query below is one a vector index cannot answer correctly, no matter how
good the embedding is. They are not hard because the text is hard to match —
they are hard because the answer is a property of the *relationships between*
memories, not of any single memory's content.

| Question | What it needs | Why similarity search fails |
|---|---|---|
| *"Where did I live before?"* | Walk `SUPERSEDES` backwards from the current fact | The old value is the one the question does **not** name. Nearest-neighbour ranks the *current* fact top — the superseded one is a worse lexical and semantic match to the question by construction. |
| *"What database do I use?"* (stated in s2 **and** s7) | Collect every `Fact` on one `(subject, predicate)` slot, across sessions | Top-k returns the k nearest chunks, which are usually k restatements of the *same* session. There is no operator for "one hop out from every fact sharing this slot". |
| *"Do I still prefer Python?"* | Read `Conflict.status` and `valid_until` on the edge | Both statements are in the index, both match well, and neither carries the fact that one **replaced** the other. A vector store returns the contradiction without resolving it. |
| *"What is my blood type?"* (never discussed) | Measure coverage of the query over the retrieved subgraph | Cosine similarity always returns *something*. There is no null. A vector store's top hit against an unheard-of question is a confident, wrong neighbour — which is exactly how memory layers hallucinate. |

The shared shape: **vectors rank memories by resemblance to the question, and
all four answers depend on structure that resemblance cannot see** — which fact
replaced which, which slot they compete for, which are still valid, and whether
anything relevant exists at all.

Weave still uses embeddings — see [Semantic fallback](#semantic-fallback-and-its-cost).
They are how it finds *candidates* when wording differs (*"what colour do I like
best?"* against a stored `favorite_color`). They are added to lexical scoring,
never used as the retrieval substrate. Similarity picks what to look at; the
graph decides what is true.

### What breaks without the graph

Without a graph substrate, the three-layer architecture does not degrade — it
collapses into a pile of application code. Concretely, reproducing
`weave query "Where did I live before?"` on Postgres plus a vector index means:

1. A vector search for candidate utterances (**one service**), then
2. `facts JOIN entities JOIN fact_sources JOIN sessions` to hydrate provenance
   (**4 JOINs**), then
3. A recursive CTE over the supersession chain to find what the current fact
   replaced — self-joining `facts` against itself on `(subject, predicate)`
   with a `valid_until` window (**recursive self-join, hand-written**), then
4. A second pass to check `conflicts.status` for anything unresolved touching
   those fact ids (**one more JOIN**), then
5. Application code to reconcile the vector store's ranking with the SQL
   result set, because **the two systems have no shared identity** — and
   keeping them from drifting apart on every write is its own ongoing problem.

That is two datastores, five-plus JOINs, a recursive CTE, and a consistency
problem you now own forever. In Weave it is one bounded traversal against one
store: match the entity, expand `HAS_FACT`, follow `SUPERSEDES`, filter on
`valid_until`. The procedural layer makes this worse for the SQL version, not
better — `BEST_PATH_FOR` / `TRIED` / `SUCCEEDED` is a graph of retrieval
strategies whose whole purpose is to be traversed and updated per query.

The honest counter-argument: at this corpus size SQLite would be *fast* enough
for all of it. The claim is not that a graph is faster. It is that the queries
above are one traversal each instead of a bespoke join plan each, and that the
supersession chain and the conflict graph are the data model rather than
something layered on top of it.

### Why object-store backing matters here

Track 3's shape is the argument for it: **115K tokens of history per question,
of which a query touches ~520** (measured — see [Benchmarks](#benchmarks)).
That is a 198:1 ratio between what must be *retained* and what is ever *read*,
and it splits cleanly along the layer boundary:

| Layer | Size | Access pattern | Wants |
|---|---|---|---|
| **Episodic** — raw turns, immutable | ~99% of bytes | Written once, read rarely, never updated | Cheap capacity. S3-class object storage. |
| **Semantic** — facts, conflicts, validity | ~1% of bytes | Read on every query, rewritten by every consolidation | Low latency. Memory/SSD-class. |

Episodic memory is append-only by design — an utterance is never edited, only
cited — which is precisely the workload object storage is good at, and the
reason HydraDB's S3-backed durability is a fit rather than a compromise. Keeping
30–40 sessions per user in a hot store priced for the semantic layer means
paying working-set rates for an archive that is read on maybe one query in a
hundred. Splitting them is what makes per-user retention economically boring
instead of a scaling ceiling.

**Status: architecturally supported, not yet exercised.** The layer split exists
in the schema and the retrieval paths already address the layers separately, so
the cold/hot boundary falls where it should. Weave does not currently drive a
tiered store — the embedded backend is one SQLite file and the sidecar holds a
full copy of the episodic text. Reporting this as shipped would be a claim the
code does not support.

---

## Architecture

### Layers

| Layer | Nodes | Edges | Answers |
|---|---|---|---|
| Episodic | `Session` `Turn` `Utterance` | `HAS_TURN` `HAS_UTTERANCE` `NEXT` `PREVIOUS` `MENTIONS` | "What did I say in session 3?" |
| Semantic | `Entity` `Fact` `Conflict` | `HAS_FACT` `DERIVED_FROM` `SUPERSEDES` `CONFLICTS_WITH` `INVOLVES` `RESOLVED_TO` | "What do I prefer now?" |
| Procedural | `QueryType` `RetrievalPath` `Outcome` | `BEST_PATH_FOR` `TRIED` `SUCCEEDED` `FAILED` | "How should I retrieve this?" |

### Pipelines

**Ingestion (hot path).** Session → episodic nodes → entity extraction → semantic
merge. A fact whose `(subject, predicate)` already exists is reinforced when the
object matches, and raises a `Conflict` when it does not. Ingestion never decides
who wins.

**Consolidation (background "sleep").** Resolves open conflicts under
`recency`, `frequency`, `confidence` or `trust`; merges duplicates; writes the
policy onto the conflict node for audit.

**Query (hot path).** Classify → route via the procedural layer → bounded
traversal → coverage check → abstain or assemble context → answer → log outcome.

### Retrieval paths

| Path | Layers | Used for |
|---|---|---|
| `semantic-only` | semantic | factual questions |
| `hybrid-conflict` | semantic + history | preference questions |
| `episodic-depth-3` | episodic + semantic | temporal questions |
| `episodic-depth-2` | episodic | procedural questions |

These are defaults. Each answered query writes an `Outcome`; once a path has
enough evidence the router switches to the learned choice and labels it as such
in the response (`path_reason: "learned"` vs `"default"`). Routing is
epsilon-greedy so an unlucky early failure can recover.

---

## Graph backends

Weave talks to a `GraphStore` contract — nodes, edges, labelled traversal and
bounded paths — with two implementations:

| Backend | Select with | Notes |
|---|---|---|
| **Embedded** (default) | `WEAVE_BACKEND=embedded` | SQLite property graph: expression indexes on the specification's index set, `json_extract` predicates, capped-BFS multi-hop traversal. No external services. |
| **Bolt/OpenCypher** | `WEAVE_BACKEND=hydra` | Any Bolt server, via the `neo4j` driver. Multi-hop prefers an `algo.MSpaths` path procedure and falls back to a variable-length pattern match when it is unavailable — which, on every server tested, it is. |

```bash
./scripts/setup_hydradb.sh      # docker compose up + wait for Bolt
export WEAVE_BACKEND=hydra
weave serve
```

> **Verification status.** The Bolt/OpenCypher backend has been executed
> against a live server: the **entire test suite passes on both backends**, and
> the demo workload produces an identical graph (118 nodes, 213 edges, the same
> two conflicts resolved the same way) either way.
>
> ```bash
> WEAVE_TEST_BACKEND=hydra pytest      # runs the identical suite over Bolt
> ```
>
> **This backend is not HydraDB, and cannot be.** The specification this
> project was built from describes HydraDB as a Bolt/OpenCypher server at
> `neo4j://localhost:7687` exposing `algo.SSpaths` / `algo.MSpaths`. The real
> product is a **managed REST API** at `https://api.hydradb.com`: HydraDB's own
> integration guide contains zero occurrences of Bolt, Cypher, Neo4j, port
> 7687, or those procedures. The `hydradb/hydradb` container image does not
> exist either. The specification described a product that isn't the one that
> ships.
>
> So what this backend actually is: a genuine, working Bolt/OpenCypher
> implementation, verified against **Neo4j 5.26**. Everything generic is really
> verified — transactions and rollback, MERGE semantics, property filters and
> ordering, index DDL, bounded traversal, and §8.3's consistency modes. The
> `algo.MSpaths` branch is dead code against any server that does not implement
> it, and is exercised only through its fallback.
>
> The real HydraDB is integrated separately, as a retrieval sidecar — see
> [HydraDB sidecar](#hydradb-sidecar).
>
> Running it for real found four bugs that code review had not, all now fixed:
>
> | Bug | Why it only showed up on a live server |
> |---|---|
> | The `algo.MSpaths` fallback never worked | A failed procedure call aborts the whole transaction, so the fallback could not run inside it. Availability is now probed once, in its own session. |
> | `CREATE INDEX` was not idempotent | Re-running schema setup threw and reported *zero* indexes created. Now `IF NOT EXISTS`. |
> | Procedural learning silently stopped counting | `create_edge` replaces on the embedded engine (unique index) but Cypher `CREATE` adds a *parallel* relationship, so each duplicate held part of the count. The contract gained an explicit `upsert_edge`. |
> | Null properties read back differently | `SET x = null` removes the key in Cypher; the embedded engine keeps it as null. Nullable properties must be read with `.get()`. |

## HydraDB sidecar

HydraDB proper — the REST context API — is integrated as an **optional episodic
retrieval sidecar**, which is the shape that actually fits. The split is:

* **HydraDB is the index** — it answers *"which utterances look relevant?"*
* **Weave's graph stays the source of truth** — provenance, supersession,
  conflicts, and the procedural layer.

A memory is stored under *Weave's own utterance id*, and every hit is hydrated
back into a local node before it can become evidence. An id HydraDB knows about
that the graph does not is discarded. So the sidecar can influence **which**
utterances are considered, never what they say or what they cite — and with it
absent or unreachable, retrieval falls back to the local scan and every answer
is still correct, just slower to find. `infer` is deliberately off: Weave has
already extracted its own facts, and a second, disagreeing semantic layer is
exactly what this must not create.

```bash
pip install -e ".[sidecar]"
export HYDRA_DB_API_KEY=...        # from https://app.hydradb.com (free tier)
weave sidecar-verify               # round-trips a probe through the live API
```

Off unless a key is set. `WEAVE_SIDECAR=off` disables it even when one is.

> **Verification status: executed against the live API.** `weave
> sidecar-verify` round-trips a probe (indexed, searchable in ~5s), and the
> demo corpus round-trips fully — 18 of 18 search hits resolved back to local
> utterance nodes.
>
> Running it for real found three bugs that the stub could not:
>
> | Bug | Why only a live key found it |
> |---|---|
> | Ingest failed straight after creating the workspace | Provisioning is asynchronous — `POST /databases` returns before the vectorstore exists. `ensure_database` now polls `ready_for_ingestion`. |
> | An unready workspace re-waited on every ingest | Only success was cached, so one outage added minutes of latency to every session, forever. The readiness check is now tri-state and gives up once per process. |
> | Whole sessions were rejected with a 413 | The API caps a request at ~1000 memory tokens, and rejects all-or-nothing. Uploads are now split by estimated tokens, and one failed chunk no longer discards the rest. |
>
> The suite stays hermetic with `HYDRA_DB_API_KEY` exported — the test settings
> force the sidecar off, so verifying the integration cannot silently point
> every test at the live API.

**It does not make retrieval faster, and that was the point of building it.**
The sidecar was justified as a fix for Weave's missing text index. Measured on
a real 6,369-utterance LongMemEval haystack, it is *slower*:

| | Median query | Gold answer in context |
|---|---|---|
| Local scan | **586 ms** | yes |
| HydraDB sidecar | 877 ms | yes |

A cloud round-trip costs more than scanning 6,369 rows of local SQLite, and the
call is *added* to the query rather than replacing work — skipping the local
scan when the sidecar answers (which it now does) recovered some of the gap but
not all of it. Indexing that one haystack also took **901s**, because the
API's ~1000-token request cap turns a single upload into hundreds of requests.

So the honest position: the integration is real, verified, and correct, and it
is the right shape for using HydraDB — but the performance argument for it does
not survive measurement at this scale. The fix for query latency is a **local
text index** (SQLite FTS5 behind the same `GraphStore` contract), which beats
both. The sidecar earns its place for scale-out beyond a single local file, not
for speed.

### Consistency (§8.3)

`WEAVE_HYDRA_CONSISTENCY` selects `causal` (default) or `strong`. Over HydraDB's
HTTP API strong consistency is a `consistency: strong` header; over Bolt the
equivalent is bookmark chaining, which is what the backend implements — each
committed transaction hands its bookmark to the next session, so a read is
guaranteed to observe every write this client has made even if the cluster
routes it elsewhere. Ingestion runs `causal`, which is cheaper and all that
writes require; a benchmark read that must see the latest consolidation runs
`strong`.

---

## Extraction and answers

Weave runs fully without an API key:

| | No key (default) | With `ANTHROPIC_API_KEY` |
|---|---|---|
| Extraction | Deterministic rule-based patterns | LLM structured extraction, unioned with the rules |
| Answers | Grounded template composed from retrieved facts, with citations | LLM answer over the assembled context |

The header badge always states which mode produced what you are looking at;
fallback data is never presented as live.

The rule-based extractor handles clause splitting ("I live in Berlin **and** I
work at Acme"), coordinated objects ("I use Postgres **and** Docker" → two
facts), negation ("I hate coffee"), time-tail stripping ("I moved to Lisbon **at
the start of the month**" → `lisbon`), and predicate specialisation by object
category so unrelated facts never collide.

---

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/ingest` | Ingest a session |
| `POST` | `/query` | Ask a question |
| `POST` | `/consolidate` | Run the sleep cycle |
| `POST` | `/feedback` | Train the procedural layer |
| `GET` | `/health` | Backend status + layer statistics |
| `GET` | `/facts` `/conflicts` `/sessions` `/graph` `/procedural` | Read models for the UI |
| `POST` | `/demo/seed` `/reset` | Demo helpers |

Interactive reference at `/docs`.

`GET /health` reports the graph backend, the per-layer node counts, and the
HydraDB sidecar's state — including *why* it is off, since "disabled",
"no API key" and "SDK not installed" are three different problems:

```json
{
  "status": "ok",
  "hydra_sidecar": {
    "state": "off",
    "reason": "no API key (set HYDRA_DB_API_KEY)",
    "sdk_installed": true,
    "role": "episodic text index; the graph remains the source of truth"
  }
}
```

```bash
curl -s localhost:8000/query -H 'content-type: application/json' \
  -d '{"query":"What is my favorite color?"}' | jq '.answer, .abstained, .retrieval_path'
```

Every piece of returned evidence says how the retriever reached it, so an
answer can be audited without re-running it:

| Field | Meaning |
|---|---|
| `lexical` | word overlap with the question, 0–1 |
| `semantic` | embedding similarity, rescaled by the floor/ceiling (0 when off) |
| `matched_by` | `wording`, `meaning`, `both`, `graph`, or `none` |
| `retrieved_count` | size of the traversal *before* non-matching evidence was pruned |

`graph` is the interesting one: a fact with no wording or embedding overlap at
all, kept because it occupies the same subject+predicate slot as something that
did match. That is how *"what did I use before I changed to ClickHouse?"* keeps
hold of Postgres — a question that names the replacement cannot, by
construction, mention the value it replaced.

---

## Benchmarks

```bash
python scripts/run_benchmark.py --limit 40 --ablation
python -m benchmarks.locomo                 # needs the LoCoMo release, see below
```

### Real LongMemEval results

Run over the **full 500-question `longmemeval_s`** release (~103k tokens of
haystack per question), embedded backend, rule-based extraction, template
answers — no LLM key. Raw report:
[`results/longmemeval-s-final.json`](results/longmemeval-s-final.json).

| Metric | Value |
|---|---|
| Accuracy | **20.4%** (102/500) |
| Context recall | **62.3%** (293/470) |
| Abstention F1 | **14.3%** (precision 8.4%, recall 46.7%) |
| Context tokens | 541 vs 103,156 full-context (**191×** smaller) |
| Latency | mean 1,571 ms · median 1,510 ms |

| Category | Accuracy | |
|---|---|---|
| `single-session-user` | 45.7% | 32/70 |
| `knowledge-update` | 26.9% | 21/78 |
| `multi-session` | 19.6% | 26/133 |
| `temporal-reasoning` | 12.8% | 17/133 |
| `single-session-assistant` | 10.7% | 6/56 |
| `single-session-preference` | 0.0% | 0/30 |

**Read this honestly.** Four things it says:

1. **Retrieval works; generation is the bottleneck.** Context recall is 62.3%
   against 20.4% accuracy — Weave finds the evidence **three times more often
   than it produces the graded answer**. The template generator quotes evidence
   verbatim: asked where a coupon was redeemed it returns the sentence
   containing the answer, while the grader wants the store's name. Accuracy
   here is substring containment, stricter than LongMemEval's official GPT-4
   judge, and closing that gap is what an LLM key does. It is not a retrieval
   problem, and [the baselines below](#retrieval-baselines-and-the-measurement-bug-they-exposed)
   show retrieval beating a strong keyword baseline on equal terms.

2. **`single-session-preference` is a zero, and a real one.** 0/30. The
   answerer emits `User prefers X` where the grader wants free text — the
   category where quoting verbatim helps least.

3. **Abstention is the genuine weakness.** F1 14.3%: it catches 14 of the 30
   unanswerable questions but abstains on far more than that to find them
   (precision 8.4%). Those false abstentions are exactly what separates the
   62.3% headline recall from the 90.6% Weave reaches on questions it actually
   attempts. The [threshold curve](#abstention-a-measured-operating-point)
   shows why loosening it is not the fix.

4. **The token reduction is real.** 191× less context than stuffing the
   haystack, on data nobody tuned against.

> **These numbers replace an earlier set** that reported 21.5% context recall.
> That figure was produced by a probe that could not match sentence-granular
> storage; the retrieval did not change, the measurement did. The story is
> [below](#the-bug-this-table-originally-reported).

### Retrieval baselines, and the measurement bug they exposed

"368 tokens instead of 103,000" is only interesting if those tokens are *better
chosen* than the cheapest way of picking 368 tokens. So we built the
comparison: four retrievers, the same 100 stratified questions, one metric, and
a 600-token budget for every baseline — slightly *more* than Weave spends. Raw
report:
[`results/baselines-corrected-100.json`](results/baselines-corrected-100.json),
harness in [`benchmarks/baselines.py`](benchmarks/baselines.py).

| Retriever | Context recall | When it attempted | Context tokens | vs haystack |
|---|---|---|---|---|
| `full-context` — the whole haystack | 100.0% | 100.0% | 103,174 | 1× |
| `lexical-topk` — IDF keywords over raw turns, no graph | 87.2% | 87.2% | 596 | 173× |
| **`weave`** — the full three-layer system | **61.7%** | **90.6%** | **368** | **280×** |
| `recency` — truncating context window | 4.3% | 4.3% | 600 | 172× |

**When Weave attempts a question it finds the gold evidence 90.6% of the time,
against 87.2% for a strong keyword baseline, using 38% fewer tokens.** The
`recency` row is the control that says retrieval is worth doing at all: a
sliding context window finds the evidence in 4 questions out of 94.

The gap between Weave's two columns — 61.7% headline against 90.6% when
attempting — is **entirely abstention**. It refuses 30 of the 94 answerable
questions, and every refusal scores as a miss. That is the one real weakness,
and it is measured rather than asserted below.

#### The bug this table originally reported

The first version of this table said `lexical-topk` **81.9%** against Weave
**18.1%** — a naive keyword baseline beating the graph by 4.5×. That number was
wrong, and the cause is worth recording because it is a mistake any project
measuring retrieval can make.

Weave stores **one utterance per sentence**; LongMemEval's gold evidence is a
whole **turn**. The recall probe took the gold turn's leading 60 characters and
looked for them as one contiguous string. On **35% of gold turns the first
sentence is shorter than that**, so the probe straddled a sentence boundary
that cannot exist in Weave's context — unmatchable however perfectly the right
sentence was retrieved. Every baseline stores whole turns, so none of them
were affected. We were measuring text segmentation and calling it retrieval
quality.

The probe now matches **any sentence** of the gold turn
([`benchmarks/longmemeval.py`](benchmarks/longmemeval.py)). The correction is
symmetric — a whole turn contains its own sentences, so turn-based retrievers
score identically under both rules and gain nothing — and the 30-character
floor is set from the release rather than picked: gold sentences have a median
length of 89 characters, a 10th percentile of 35, and every gold turn contains
at least one sentence of 40+. Four tests in
[`tests/test_baselines.py`](tests/test_baselines.py) pin the behaviour.

Two claims we made from the broken metric did not survive it:

* *"Candidate selection is losing the evidence."* It is not. Re-measured, the
  gold utterance reaches the FTS5 candidate set on **12 of 12** sampled
  questions under both the old term selection and a proposed IDF-ordered
  replacement. The replacement was written, measured to change **nothing**
  (byte-identical output over 100 questions), and reverted.
* *"A keyword baseline beats the graph."* It does not, on equal terms.

What did survive: consulting the raw conversation on every query rather than
only when a distilled fact scores below 0.6 (`WEAVE_WIDEN_BELOW`, now `1.0`).
Measured against a control arm — 56.4% → 61.7% recall, 82.8% → 90.6% when
attempting, for 21 extra tokens.

### Abstention: a measured operating point

Abstention is the one place Weave genuinely loses recall, so the threshold is
reported as a curve rather than a claim. Same 100 questions, one variable:

| `WEAVE_ABSTENTION_THRESHOLD` | Accuracy | Context recall | Abstention F1 |
|---|---|---|---|
| **0.30 (shipped)** | **21.0%** | 61.7% | **20.0%** |
| 0.15 | 20.0% | 68.1% | 7.4% |
| 0.00 | 21.0% | 71.3% | 8.7% |

Abstaining less does buy context recall — and **converts none of it into
accuracy**, while halving abstention F1. So the shipped value stays at 0.30.
The flat accuracy column is the more useful finding: with 61.7% of gold
evidence reaching the context and 21.0% graded correct, **accuracy is
generator-bound, not retrieval-bound**. The template answerer quotes evidence
verbatim; the grader wants a particular phrase. That gap is what an LLM key
closes, and it is the reason the retrieval numbers above are reported
separately from accuracy at all.

### LoCoMo

The same code, scored against a second real dataset — 300 questions from the
[LoCoMo](https://github.com/snap-research/locomo) release. Raw report:
[`results/locomo-300.json`](results/locomo-300.json).

| Metric | Value |
|---|---|
| Accuracy | **11.7%** (35/300) |
| Context recall | **3.9%** (9/233) |
| Abstention F1 | **27.8%** (precision 19.9%, recall 46.3%) |
| Context tokens | 277 vs 12,086 full-context (**43.6×** smaller) |
| Latency | mean 245 ms · median 246 ms |

| Category | Accuracy | |
|---|---|---|
| `adversarial` | 46.3% | 31/67 |
| `single-hop` | 3.5% | 4/114 |
| `multi-hop` | 0.0% | 0/43 |
| `temporal-reasoning` | 0.0% | 0/63 |
| `open-domain` | 0.0% | 0/13 |

**This is the weakest result in the project, and it is instructive.** Every
point comes from the `adversarial` category — LoCoMo's unanswerable questions,
where abstaining *is* the correct answer. On everything requiring an actual
retrieved fact, Weave scores near zero, and context recall of 3.9% says why:
the evidence almost never reaches the context.

The cause is a shape mismatch, not a bug. LoCoMo is multi-speaker dialogue
between two named people; Weave's extractor is built around a single `user`
subject and attributes facts to them. Facts about *the other speaker* are
either dropped or misattributed, so the semantic layer ends up sparse and
wrong-subjected, and the retrieval paths have little to find. Reporting it
anyway is the point: the harness runs a second real dataset by identical code,
and this is what it says.

### Ablation

Each configuration changes exactly one variable, over the same 60 stratified
questions from the real release. Raw reports:
[`results/abl-episodic.json`](results/abl-episodic.json),
[`results/abl-rest.json`](results/abl-rest.json).

| Config | Accuracy | Context recall | Abstention F1 | Context tokens | Latency |
|---|---|---|---|---|---|
| `semantic-only` | 10.0% | 0.0% | 13.3% | 148 | 774 ms |
| `episodic-only` | 26.7% | 64.3% | 22.2% | 463 | 1,663 ms |
| `no-consolidation` | 28.3% | 64.3% | 22.2% | 538 | 1,590 ms |
| **`full-weave`** | **28.3%** | **69.6%** | **25.0%** | 528 | 1,528 ms |

The full system wins on the two metrics it should: context recall and
abstention F1. What the rows actually say:

* **`semantic-only` collapses** — 10% accuracy, and context recall of *zero*
  by construction. Restricted to the semantic layer, retrieval returns only
  distilled facts (`user lives in lisbon`), never the raw turn the grader is
  looking for. Consolidated facts are an excellent index and a poor substitute
  for evidence; this row is why the episodic layer is not optional.
* **`episodic-only` is competent but blunt** — 26.7%. Raw utterances can say
  what was said, but not which value is still true, and abstention F1 drops
  because there is no fact-level structure to judge coverage against.
* **Consolidation buys recall and abstention, not accuracy.** Against
  `no-consolidation` the full system is level on accuracy (28.3%) and ahead on
  context recall (64.3% → 69.6%) and abstention F1 (22.2% → 25.0%), for
  slightly *fewer* tokens. On this slice, resolving conflicts sharpens which
  evidence is retrieved rather than changing the final wording.

**A limitation, not an omission:** the conflict-resolution column is `n/a` on
every real-data row. LongMemEval does not label which contradictions its
haystacks contain, so there is no ground truth to score against — that metric
is only computable on the synthetic generator, which labels its own
knowledge-updates. The earlier version of this table reported conflict
resolution at 100%/0% from synthetic data; those numbers were real but measured
on a dataset built to contain exactly the conflicts being counted, and they are
not evidence about LongMemEval.

### On the benchmark numbers

> LongMemEval is not redistributable, so the harness falls back to a **synthetic
> generator** that produces the same shape: ~30-session haystacks per question,
> evidence buried in one or two of them, distractor facts in the filler,
> paraphrased questions, knowledge updates, and topically-adjacent unanswerable
> questions. Every report prints its `dataset_source`.
>
> **The synthetic 100% is a regression signal, not a capability claim.** The
> generator draws from a fixed pool of five topics and five simple facts; a
> system tuned against it will score high on it. It is useful for catching
> regressions, and it is the only configuration that can score conflict
> resolution, because it labels the contradictions it plants. It is not a
> substitute for the real benchmark, and every table above is measured on the
> real release instead.
>
> To run the real thing, fetch the release (MIT licensed, ~278MB) — the loader
> prefers it automatically and reports `dataset_source: local:…`:
>
> ```bash
> mkdir -p data/longmemeval
> curl -L https://huggingface.co/datasets/xiaowu0162/longmemeval/resolve/main/longmemeval_s \
>   -o data/longmemeval/longmemeval-s.json
> python -m benchmarks.longmemeval --limit 120
> ```
>
> `--limit` takes a **stratified** subset, not the first N rows: the release is
> ordered by question type, so plain slicing returns one category and zero
> abstention questions, and any number measured that way is meaningless.
>
> `benchmarks/locomo.py` adapts the LoCoMo release into the same sample shape so
> both datasets are scored by identical code. It has **no synthetic fallback** by
> design — without `data/locomo/locomo10.json` it says so and exits, rather than
> reporting invented data under a real benchmark's name.

Metrics reported: accuracy overall and per category, abstention
precision/recall/F1, **conflict resolution accuracy** (correctly resolved
conflicts / conflicts the data should have raised — an undetected contradiction
counts against the score rather than leaving the denominator), **context
recall**, average context tokens against the full-context baseline, and
end-to-end latency.

**Context recall** separates the two halves of the task. Locating the evidence
inside a 100k-token haystack is the memory system's job; turning that evidence
into the exact phrase a grader wants is the generator's. Weave's default answer
generator is a template that quotes evidence verbatim, so on free-text answers
(*"45 minutes each way"*) accuracy understates retrieval — context recall
measures the half Weave is responsible for.

Answer accuracy here is **substring containment**, which is stricter than
LongMemEval's official metric (a GPT-4 judge). Treat it as a lower bound.

### Semantic fallback and its cost

`Utterance.embedding` (specification §4.1) is implemented with a static
embedding model -- a lookup table distilled from a sentence transformer, so
numpy only, no torch, ~30MB. Vectors are stored on utterances and facts at
ingest, and similarity is added to lexical scoring rather than replacing it.

It is what closes the synonym gap. *"What colour do I like best?"* has no word
in common with a stored `favorite_color`; lexically it ties with an unrelated
"likes long walks" fact and loses. The embedding separates them 0.473 to 0.221.

| | Accuracy | Abstention F1 | Mean latency |
|---|---|---|---|
| lexical only (`WEAVE_EMBEDDINGS=off`) | 97.5% | 100.0% | 3.5 ms |
| with embeddings | **100.0%** | 100.0% | 5.5 ms |

So: +2.5 points for +2ms. Everything still runs, and every test still passes,
with the model absent -- the dependency is genuinely optional.

**The similarity floor is measured, not guessed.** Similarity is soft, so
without a floor an unrelated question drifts over the abstention threshold on
vector noise. On this dataset the highest similarity any *unanswerable*
question reaches is 0.340 ("what is my favourite season?" against a stored
favourite colour); the synonym case that must be answered sits at 0.473. The
floor is the midpoint, 0.40. It is fitted to this dataset and should be
re-measured against a real one rather than inherited.

**A known limitation, recorded rather than hidden.** Asking for a *different
attribute of the same kind* -- "what is my favourite season?" when a favourite
colour is stored -- still answers instead of abstaining: the shared word
"favourite" scores 0.5 lexically, above the grounding threshold, even though
the embedding places the two well apart. Using the embedding to veto an
uncorroborated lexical hit was tried and reverted: it also halves legitimate
matches whose subject is too short for the vector to confirm ("...before I
changed to Go?"), trading one coincidental answer for three false abstentions.
The case is kept as an `xfail` in `tests/test_embeddings.py` so it stays
visible.

## Observability

Ingestion, consolidation and abstention each log one line, so a surprising
answer can be traced without a debugger. Abstentions log at `INFO` with the
deciding reasons attached, because that is the decision most likely to be
questioned; ordinary answers log at `DEBUG`.

```
$ python -c "import logging; logging.basicConfig(level=logging.INFO); ..."
INFO weave.ingestion:     ingested s1: 2 turns, 2 facts (+0 reinforced), 3 entities, 0 conflict(s) in 1093 ms via rule-based
INFO weave.ingestion:     ingested s2: 2 turns, 2 facts (+0 reinforced), 2 entities, 2 conflict(s) in 12 ms via rule-based
INFO weave.consolidation: sleep cycle (recency): examined 2, resolved 2, superseded 2, merged 0 duplicate(s) in 3 ms
INFO weave.retrieval:     abstained on 'What is my blood type?' (type=factual path=semantic-only score=-0.20 < 0.30): Nothing stored matches the subject of the question
```

## Tests

```bash
pytest                      # 98 tests
```

Covering the graph substrate (filters, ordering, bounded traversal, transaction
rollback, index creation), ingestion and extraction, conflict detection and all
four resolution policies, retrieval and routing, the abstention router, the
embedding fallback with its abstention guard, the HydraDB sidecar against a
stub, and the benchmark baselines including the context-recall probe itself.
Run with `WEAVE_EMBEDDINGS=off` to confirm the lexical-only path still passes.

---

## Project layout

```
weave/
  graph/          store.py (contract) · embedded.py (SQLite) · hydra.py (Bolt) · schema.py
  models/         episodic.py · semantic.py · procedural.py
  services/       ingestion · extraction · consolidation · retrieval · procedural
  prompts/        extraction, classification and answer templates
  web/            the workspace UI: index.html · workspace.html · globals.css · app.js
  api.py  cli.py  client.py  db.py  config.py  llm.py  sidecar.py  embeddings.py  util.py
benchmarks/       dataset.py · longmemeval.py · locomo.py · ablation.py · baselines.py
scripts/          setup_hydradb.sh · ingest_sample.py · run_benchmark.py
tests/            graph · ingestion · conflict · retrieval · abstention · embeddings · sidecar
data/             sample_sessions/
```

## Deploying to Vercel

```bash
vercel deploy          # api/index.py + vercel.json + requirements.txt are in the repo
```

Set these in the Vercel dashboard. The first four are defaulted in
[`api/index.py`](api/index.py) so a deploy works without touching anything,
but an explicit value always wins:

| Variable | Value | Why |
|---|---|---|
| `WEAVE_BACKEND` | `embedded` | There is no graph server in a serverless function |
| `WEAVE_DB_PATH` | `/tmp/weave.db` | Everything outside `/tmp` is read-only |
| `WEAVE_EMBEDDINGS` | `off` | Otherwise a ~30MB model downloads on every cold start |
| `HF_HOME` | `/tmp/hf` | Backstop for anything that still reaches for the model cache |
| `WEAVE_ACCESS_TOKEN` | *(random string)* | **Set this.** Empty means no authentication, which is right for loopback and wrong for a public URL |
| `ANTHROPIC_API_KEY` | *(optional)* | Real generated answers instead of templates |
| `HYDRA_DB_API_KEY` | *(optional)* | Activates the HydraDB sidecar |

Do **not** set `WEAVE_HYDRA_*`; those configure the Bolt backend, which is not
used in a Vercel deployment.

> **`/tmp` is ephemeral and per-instance.** Every cold start begins with an
> empty graph, and two concurrent requests may reach different instances
> holding different data. For a demo that is usually fine — the flow is *click
> **Load demo memory**, ask questions* — but ingested sessions do not survive,
> so this is a showcase deployment rather than a persistent service.
>
> For real persistence use `WEAVE_BACKEND=hydra` against a hosted Bolt server
> (Neo4j Aura has a free tier) with `WEAVE_HYDRA_URI` and `WEAVE_HYDRA_TOKEN`.
> That backend passes the identical test suite — see [Graph backends](#graph-backends).

[`.vercelignore`](.vercelignore) keeps the function bundle at ~1.4MB by
excluding `data/` — the benchmark corpora are gitignored but still visible to
the CLI when deploying from a working copy, and they are ~283MB.

## Configuration

Copy `.env.example` to `.env`. Everything has a working default; the settings
that matter most are `WEAVE_BACKEND`, `ANTHROPIC_API_KEY`,
`WEAVE_ABSTENTION_THRESHOLD` (default `0.3`) and `WEAVE_MAX_CONTEXT_TOKENS`
(default `6000`).

## Team

Solo build by **[Dotman-Bei](https://github.com/Dotman-Bei)** (Emmanuel Bamigboye) —
graph schema and both backends, the three-layer services, abstention router,
benchmark harness, and the workspace UI.

## Attribution

Weave is built on other people's work. In full:

**Datasets**

| | Used for | License |
|---|---|---|
| [LongMemEval](https://github.com/xiaowu0162/LongMemEval) (Wu et al., 2024) — [`longmemeval_s` release](https://huggingface.co/datasets/xiaowu0162/longmemeval) | The primary benchmark: 500 questions over ~103K-token, 39–66-session haystacks | MIT |
| [LoCoMo](https://github.com/snap-research/locomo) (Maharana et al., 2024) | Second benchmark, 300 questions, scored by identical code | See upstream repo |

Neither dataset is redistributed here; both are fetched at runtime into
`data/`, and the harness reports which one produced every number
(`dataset_source`).

**Libraries**

| | Used for | License |
|---|---|---|
| [FastAPI](https://github.com/tiangolo/fastapi) + [Uvicorn](https://github.com/encode/uvicorn) | HTTP API and the OpenAPI reference at `/docs` | MIT / BSD-3 |
| [Pydantic](https://github.com/pydantic/pydantic) | Request/response models | MIT |
| [httpx](https://github.com/encode/httpx) | HTTP client for the LLM and sidecar integrations | BSD-3 |
| [neo4j Python driver](https://github.com/neo4j/neo4j-python-driver) | Bolt transport for the OpenCypher backend | Apache-2.0 |
| [hydradb-sdk](https://pypi.org/project/hydradb-sdk/) | The HydraDB REST context API, used as the episodic retrieval sidecar | See upstream |
| [model2vec](https://github.com/MinishLab/model2vec) + [`minishlab/potion-base-8M`](https://huggingface.co/minishlab/potion-base-8M) | Static embeddings for the semantic-similarity fallback — numpy only, no torch | MIT |
| [anthropic](https://github.com/anthropics/anthropic-sdk-python) + [tiktoken](https://github.com/openai/tiktoken) | Optional LLM extraction/generation and token counting | MIT |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | `.env` loading | BSD-3 |
| [pytest](https://github.com/pytest-dev/pytest) | The 98-test suite | MIT |
| SQLite (via Python's [`sqlite3`](https://docs.python.org/3/library/sqlite3.html)) | Storage engine under the embedded graph backend, incl. the FTS5 text index | Public domain |

**Event**

Built for [Hack Hydra 2026](https://hackhydra.com), Track 3 — Memory & Context
Retrieval. [HydraDB](https://hydradb.com) is the sponsor database; see
[HydraDB sidecar](#hydradb-sidecar) for what it does here and
[Graph backends](#graph-backends) for what it turned out not to be.

No code was copied from another memory system. Mem0 and Zep are referenced in
this README as points of contrast only.

## License

MIT — see [LICENSE](LICENSE).
