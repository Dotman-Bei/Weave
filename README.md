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
$ weave query "Where do I live?"        →  User lives in lisbon. [sess-06]
$ weave query "Where did I live before?" →  In session 1 (2025-01-14), user said:
                                            "I live in Berlin, so most of my syncs run overnight CET."
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
answers — no LLM key:

| Metric | Value |
|---|---|
| Accuracy | **19.6%** (98/500) |
| Context recall | **33.2%** (156/470) |
| Abstention F1 | **7.0%** (precision 5.4%, recall 10.0%) |
| Context tokens | 520 vs 103,156 full-context (**198×** smaller) |
| Latency | mean 1,181 ms · median 1,176 ms |

| Category | Accuracy | |
|---|---|---|
| `single-session-user` | 45.7% | 32/70 |
| `knowledge-update` | 26.9% | 21/78 |
| `multi-session` | 18.8% | 25/133 |
| `single-session-assistant` | 10.7% | 6/56 |
| `temporal-reasoning` | 10.5% | 14/133 |
| `single-session-preference` | 0.0% | 0/30 |

**Read this honestly.** Three things it says:

1. **Retrieval outruns generation.** Context recall (33.2%) is ~70% higher than
   accuracy (19.6%): in one question in seven, Weave puts the gold answer in
   the context and then fails to say it. The template generator quotes evidence
   verbatim — asked where a coupon was redeemed, it returned the right sentence
   while the expected answer was the store's name. That gap is what an LLM
   closes, and accuracy here is substring containment, stricter than
   LongMemEval's official GPT-4 judge.

2. **The abstention router does not generalise.** 100% F1 on synthetic, **7% on
   real data** — it abstains on ~55 questions and is right about 3 of them,
   while missing 27 of the 30 genuinely unanswerable ones. The synthetic
   unanswerables were topically adjacent but lexically distinct, which is a much
   easier problem than LongMemEval's. This is the single largest gap between
   what this system claims and what it does.

3. **The token reduction is real.** 198× less context than stuffing the
   haystack, on data nobody tuned against.

The gap between this and the synthetic 100% is exactly why the synthetic score
is labelled a regression signal rather than a capability claim.

### Ablation

Each configuration changes exactly one variable, over the same dataset:

| Config | Accuracy | Abstention F1 | Conflict resolution | Context tokens | Latency |
|---|---|---|---|---|---|
| `episodic-only` | 65.0% | 84.2% | 100.0% | 33 | 6.2 ms |
| `semantic-only` | 100.0% | 100.0% | 100.0% | 49 | 3.1 ms |
| `no-consolidation` | 80.0% | 100.0% | **0.0%** | 53 | 5.1 ms |
| **`full-weave`** | **100.0%** | **100.0%** | **100.0%** | 56 | 5.8 ms |

The informative rows are the middle two. Removing **consolidation** takes
conflict resolution to zero and costs 20 points of accuracy — unresolved
conflicts leave both values current, and the wrong one gets returned.
Restricting retrieval to the **episodic layer** costs 35 points and drops
abstention F1, because raw utterances can say what was said but not which value
is still true. That is the case for the semantic layer stated as a measurement
rather than a claim.

`episodic-only` still shows 100% conflict resolution because the ablation
restricts *retrieval*, not consolidation — the conflicts are resolved correctly,
the episodic path just cannot reach the result.

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
> regressions and for the ablation comparison above, where all four configs face
> the identical dataset. It is not a substitute for the real benchmark.
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

## Tests

```bash
pytest                      # 62 tests
```

Covering the graph substrate (filters, ordering, bounded traversal, transaction
rollback, index creation), ingestion and extraction, conflict detection and all
four resolution policies, retrieval and routing, the abstention router, and the
embedding fallback with its abstention guard. Run with
`WEAVE_EMBEDDINGS=off` to confirm the lexical-only path still passes.

---

## Project layout

```
weave/
  graph/          store.py (contract) · embedded.py (SQLite) · hydra.py (Bolt) · schema.py
  models/         episodic.py · semantic.py · procedural.py
  services/       ingestion · extraction · consolidation · retrieval · procedural
  prompts/        extraction, classification and answer templates
  web/            the workspace UI: index.html · globals.css · app.js
  api.py  cli.py  client.py  db.py  config.py  llm.py  util.py
benchmarks/       dataset.py · longmemeval.py · ablation.py
scripts/          setup_hydradb.sh · ingest_sample.py · run_benchmark.py
tests/            graph · ingestion · conflict · retrieval · abstention
data/             sample_sessions/
```

## Configuration

Copy `.env.example` to `.env`. Everything has a working default; the settings
that matter most are `WEAVE_BACKEND`, `ANTHROPIC_API_KEY`,
`WEAVE_ABSTENTION_THRESHOLD` (default `0.3`) and `WEAVE_MAX_CONTEXT_TOKENS`
(default `6000`).

## License

MIT.
