# Weave — Hack Hydra 2026 Submission

**Track 3: Memory & Context Retrieval**
Repo: https://github.com/Dotman-Bei/Weave · Solo build by Emmanuel Bamigboye ([@Dotman-Bei](https://github.com/Dotman-Bei))

---

## Tagline (one line)

Graph-native agent memory that supersedes facts instead of overwriting them, and says "I don't know" before it spends a token.

## Elevator pitch (~50 words)

Weave stores conversations as an immutable episodic graph, consolidates them into semantic facts linked by `SUPERSEDES` edges, and learns which traversal answers which kind of question. On a question the graph has never heard, it abstains with `tokens_used = 0`. Measured on 500 real LongMemEval questions.

## Description (Devpost "What it does")

Most agent memory is a vector index with a `facts` table bolted on. It ranks memories by resemblance to the question, which means four ordinary questions are unanswerable by construction:

- *"Where did I live before?"* The answer is the value the question does **not** name. Nearest-neighbour ranks the current fact top.
- *"What database do I use?"* stated in session 2 and session 7. Top-k returns k restatements of one session, never one hop out from every fact sharing a slot.
- *"Do I still prefer Python?"* Both statements are in the index, both match, and neither carries which one replaced the other.
- *"What is my blood type?"* never discussed. Cosine similarity has no null. The top hit is a confident wrong neighbour, which is exactly how memory layers hallucinate.

Weave answers all four because they are properties of relationships between memories, not of any memory's content.

**Three layers, one graph:**

| Layer | Holds | Answers |
|---|---|---|
| **Episodic** `Session` `Turn` `Utterance` | Raw history, append-only, never edited | "What did I say in session 3?" |
| **Semantic** `Entity` `Fact` `Conflict` | What is true now, plus what it replaced | "What do I prefer now?" |
| **Procedural** `QueryType` `RetrievalPath` `Outcome` | Which traversal worked last time | "How should I retrieve this?" |

**Four things it does that a flat store cannot:**

1. **Overwrites become history, not mutation.** A contradiction raises a `Conflict` node. Consolidation resolves it under an explicit policy (`recency` / `frequency` / `confidence` / `trust`) and leaves a `SUPERSEDES` edge. The old fact keeps its node, its evidence, and its `valid_until` stamp. Ingestion never decides who wins.
2. **Cross-session synthesis with citations.** Facts from session 2 and session 7 merge into one answer carrying both source ids.
3. **Abstention before generation.** Entity coverage and result counts are not enough, since a user always has *some* stored facts. The decisive signal is topical overlap between question and retrieved subgraph. On a miss: "I don't know", zero tokens spent.
4. **Multi-valued predicates never false-conflict.** Liking tea does not contradict disliking coffee. Only functional predicates (`lives_in_city`, one value at a time) can conflict; accumulating ones (`uses_tool`) cannot.

## Results (measured, not claimed)

**Retrieval, against three baselines, same 100 stratified LongMemEval questions, 600-token budget for every baseline:**

| Retriever | Recall when attempting | Context tokens | vs full haystack |
|---|---|---|---|
| `full-context` | 100% | 103,174 | 1× |
| `lexical-topk` (IDF keywords, no graph) | 87.2% | 596 | 173× |
| **`weave`** | **90.6%** | **368** | **280×** |
| `recency` (sliding window) | 4.3% | 600 | 172× |

Weave beats a strong keyword baseline on gold-evidence recall using **38% fewer tokens**, and 280× less context than stuffing the haystack. The `recency` row is the control proving retrieval is worth doing at all.

**Full 500-question `longmemeval_s` run** (embedded backend, rule-based extraction, template answers, no LLM key): 20.4% accuracy, 62.3% context recall, 541 context tokens vs 103,156, median 1,510 ms.

Accuracy is **generator-bound, not retrieval-bound**: Weave finds the evidence three times more often than it produces the graded string, because the template answerer quotes evidence verbatim while the grader wants one particular phrase. The abstention threshold sweep proves it. Loosening from 0.30 to 0.00 buys 10 points of context recall and converts **none** of it into accuracy.

**Ablation, one variable at a time, 60 stratified questions:** the full system wins on the two metrics it should.

| Config | Accuracy | Context recall | Abstention F1 |
|---|---|---|---|
| `semantic-only` | 10.0% | 0.0% | 13.3% |
| `episodic-only` | 26.7% | 64.3% | 22.2% |
| `no-consolidation` | 28.3% | 64.3% | 22.2% |
| **`full-weave`** | **28.3%** | **69.6%** | **25.0%** |

`semantic-only` scoring **zero** context recall is the load-bearing row: distilled facts are an excellent index and a useless substitute for evidence. That is why the episodic layer is not optional.

## HydraDB integration

HydraDB is wired in as an **optional episodic retrieval sidecar**, which is the shape that actually fits. HydraDB answers "which utterances look relevant?"; Weave's graph stays the source of truth for provenance, supersession, conflicts, and routing. Every hit is hydrated back into a local node before it can become evidence, so an id HydraDB knows that the graph does not is discarded. The sidecar influences **which** utterances are considered, never what they say or cite. `infer` is deliberately off, because a second disagreeing semantic layer is the one thing this must not create.

**Verified against the live API**, not a stub. `weave sidecar-verify` round-trips a probe, and the demo corpus resolves 18 of 18 search hits back to local nodes. Running it for real found three bugs a stub could not:

| Bug | Why only a live key found it |
|---|---|
| Ingest failed right after workspace creation | Provisioning is async; `POST /databases` returns before the vectorstore exists. Now polls `ready_for_ingestion`. |
| An unready workspace re-waited on every ingest | Only success was cached, so one outage added minutes to every session forever. The check is now tri-state. |
| Whole sessions rejected with 413 | The API caps a request at ~1000 memory tokens, all-or-nothing. Uploads now split by estimated tokens; one failed chunk no longer discards the rest. |

**And the honest part: it does not make retrieval faster.** On a real 6,369-utterance haystack, local scan medians 586 ms against the sidecar's 877 ms. A cloud round-trip costs more than scanning 6,369 rows of local SQLite. The integration is real, verified and correct, and it earns its place for scale-out past a single local file, not for speed. The fix for latency is a local FTS5 index behind the same `GraphStore` contract.

**Why object storage is the right substrate anyway:** 115K tokens of history retained per question, ~520 ever read. That 198:1 ratio splits cleanly along the layer boundary. Episodic is ~99% of bytes, written once and read rarely, which is exactly the workload S3-backed durability is good at. Semantic is ~1% of bytes and read on every query. The schema and retrieval paths already address the layers separately, so the cold/hot boundary falls where it should. Weave does not yet drive a tiered store, and claiming otherwise would be a claim the code does not support.

## How it was built

Python, FastAPI, Pydantic. Two interchangeable graph backends behind one `GraphStore` contract: **embedded** (single SQLite file, zero setup, FTS5 text index) and **hydra** (OpenCypher over Bolt, with bookmark chaining for strong consistency). Both pass the identical suite. Static `model2vec` embeddings for the similarity fallback, numpy only, no torch. Optional Anthropic key upgrades extraction and generation from rules and templates to a model. Browser workspace UI, deployable to Vercel. **101 tests: 99 passed, 1 skipped, 1 xfailed.**

## What I learned

**Measure the measurement.** An earlier version of this README reported 21.5% context recall. The retrieval was fine; the probe could not match sentence-granular storage. The real number was 62.3%. Both figures and the story are still in the README, because deleting the wrong one would hide how the right one was found.

**Report the losses.** LoCoMo is the weakest result in the project: 11.7% accuracy, 3.9% context recall, and every point comes from the `adversarial` category where abstaining *is* correct. The cause is a shape mismatch, not a bug. LoCoMo is two named speakers; Weave's extractor is built around a single `user` subject, so facts about the other speaker are dropped or misattributed. It ships in the README anyway, because the harness runs a second real dataset by identical code and this is what it says.

**The sponsor integration that fails its own performance argument is still worth building.** It found three real bugs and it is the correct architecture for scale-out. Saying so plainly is better than a benchmark that quietly omits the local-scan column.

## What's next

Local FTS5 as the default text index (already behind the contract). Multi-speaker subject resolution to fix the LoCoMo shape mismatch. An LLM-backed answerer to close the generator gap the numbers above isolate. Actually driving a tiered episodic/semantic store rather than merely being shaped for one.

## Try it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,embeddings]"
weave serve                              # http://127.0.0.1:8000
```

Click **Load demo memory**, then ask. Or from the shell:

```bash
python scripts/ingest_sample.py
weave query "What language do I prefer for pipelines?"   # cites sess-05
weave query "Where did I live before?"                   # walks SUPERSEDES back
weave query "What is my blood type?"                     # abstains, 0 tokens
```

No database, no API key, no Docker.

---

## Submission checklist

- [x] Public repo, MIT licensed
- [x] README with architecture, benchmarks, raw result JSON, attribution
- [x] Reproducible benchmark harness (`benchmarks/`, `scripts/run_benchmark.py`)
- [x] Sponsor tech integrated and verified live
- [x] Test suite (101 tests)
- [ ] **Live demo URL** (`vercel deploy`, set `WEAVE_ACCESS_TOKEN`)
- [ ] **Demo video**

### 3-minute video beats

1. **0:00** `weave query "What is my blood type?"` → abstains, `tokens_used=0`. Open on the thing nothing else does.
2. **0:25** `"Where do I live?"` → lisbon. `"Where did I live before?"` → lisbon, previously berlin, both cited. Show the `SUPERSEDES` edge in the workspace graph.
3. **1:00** `"What database do I use?"` → two sessions, one answer, two citations.
4. **1:30** The baselines table. 90.6% against 87.2% lexical, at 38% fewer tokens, 280× smaller than the haystack.
5. **2:10** HydraDB sidecar: `weave sidecar-verify` round-trip, then the latency table. Say out loud that it is slower and why it is still the right shape.
6. **2:40** `semantic-only` ablation row at 0.0% context recall. One line: this is why all three layers exist.
