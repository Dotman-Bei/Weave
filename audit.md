# Weave — Track 3 Winning Audit
## Pre-Submission Compliance & Quality Checklist
### Hack Hydra 2026 | Memory & Context Retrieval

---

> **How to use this file:**
> Go through each section in order. Every item must pass (✅) before submission.
> Any ❌ is a blocker. Fix it before submitting.
> Score yourself 1–5 on each judging criterion at the end.

---

## Section A: Hackathon Rules Compliance (Hard Blockers)

These are disqualification risks. A single failure here eliminates the submission.

| # | Requirement | How to Verify | Status |
|---|-------------|-------------|--------|
| A1 | **No commits before Aug 12, 2026** | Run `git log --all --format="%H %ad %s" --date=short` in repo root. Earliest commit date must be `2026-08-12` or later. | ✅ **PASS** — earliest commit `66c1920` is 2026-08-16. |
| A2 | **Repository is public** on GitHub | Open repo in incognito browser. Must be viewable without login. | ⚠️ **MANUAL** — remote is `git@github.com:Dotman-Bei/Weave.git`; visibility not checkable offline. |
| A3 | **Open-source license present** | `LICENSE` file exists at repo root. Must be a recognized OSI license (MIT, Apache-2.0, AGPL, etc.). | ✅ **PASS** — MIT `LICENSE` at repo root. |
| A4 | **No private submodules or dependencies** | Check `.gitmodules`. All linked repos must be public. | ✅ **PASS** — no `.gitmodules`; all deps are public PyPI. |
| A5 | **Team size 1–4** | Count contributors in submission form. Must match GitHub commit authors. | ✅ **PASS** — 1 author: Dotman-Bei. |
| A6 | **Submitted before Aug 20, 11:59 PM PT** | Confirm submission form timestamp. Do not rely on "almost midnight." | ⚠️ **MANUAL** — 2 days remain as of the audit run. |
| A7 | **All three submission artifacts present** | Repo link + demo video link + completed form. Open each link yourself in incognito. | ⚠️ **MANUAL** — no demo video exists in or referenced from the repo. |
| A8 | **Demo video ≤ 3 minutes** | Check video runtime. Anything past 3:00 may not be reviewed. | ⚠️ **MANUAL** — video not produced yet. |
| A9 | **Demo video is accessible** | Unlisted YouTube links must not require login. Test in incognito. | ⚠️ **MANUAL** — video not produced yet. |
| A10 | **Attribution for borrowed code in README** | All third-party libs, APIs, datasets, open-source tools credited with links. | ❌ **FAIL** — README has no attribution section. LongMemEval, LoCoMo, model2vec/potion-base-8M, FastAPI, the neo4j driver and hydradb-sdk are all uncredited. |

**Section A Result:** ❌ **FAIL** — A10 (attribution) is a fixable blocker. A2/A6–A9 need manual confirmation.

---

## Section B: Track 3 — Core Problem Fit (Hard Blockers)

Track 3 asks for an agent memory layer that handles:
- 30–40 sessions
- 115,000 tokens per question
- Cross-session fact synthesis
- Chronological order preservation
- Overwritten information tracking
- Abstention (knowing when the answer is NOT in history)

| # | Track Requirement | How to Verify | Status |
|---|-------------------|-------------|--------|
| B1 | **Processes 30–40 sessions** | Benchmark harness loads ≥30 sessions. Not a toy 3-session demo. | ✅ **PASS** — real haystacks carry 39–66 sessions/question (median 50), well past the 30–40 floor. |
| B2 | **Handles 115K token histories** | Ingest a full benchmark conversation (LongMemEval/LoCoMo) and confirm no truncation. Token count logged per session. | ✅ **PASS** — `avg_haystack_tokens` 103,156 on the 500-question run; no truncation, logged per question. |
| B3 | **Synthesizes facts across sessions** | Query that requires combining info from Session 3 + Session 12 returns correct synthesized answer. Not just retrieval from one session. | ✅ **PASS** — implemented and demoed (`[sess-02, sess-07]` citations); measured `multi-session` accuracy is only 18.8%. |
| B4 | **Preserves chronological order** | Episodic layer stores `Turn` nodes with `turn_number` and `timestamp`. Query "what did I say first?" returns chronologically ordered results. | ✅ **PASS** — `Turn.turn_number` + `timestamp`, `NEXT`/`PREVIOUS` edges, `turn_time_index`. |
| B5 | **Tracks overwritten information** | When a fact changes (e.g., preference Python → Go), the old fact is NOT deleted. Query "what did I prefer before session 10?" returns the old value. | ✅ **PASS** — `SUPERSEDES` + `valid_until`; verified live: *"User lives in lisbon. Previously: berlin. [sess-06, sess-01]"*. |
| B6 | **Abstention works** | Ask a question about a topic NEVER mentioned in any session. System returns "I don't know" or equivalent — NOT a hallucinated answer. | ✅ **PASS (design)** — verified live: *"I don't know — that isn't in the stored conversation history."* But real-data abstention F1 is 7–15%. |
| B7 | **Token efficiency vs. full context** | Log tokens used per query. Target: <10K tokens vs. 115K full context. At minimum, show measurable savings. | ✅ **PASS** — 520 context tokens vs 103,156 full context = **198× reduction**. |
| B8 | **Functional demo, not just architecture** | `README.md` has a "Quick Start" section. A judge can clone, install deps, run one command, and get a query response in <5 minutes. | ✅ **PASS** — re-verified this run: `pip install -e` → `scripts/ingest_sample.py` → `weave query` answered and abstained correctly. |

**Section B Result:** ✅ **PASS** — all eight requirements met in code and verified by execution. Quality of the *results* is scored separately in E4.

---

## Section C: HydraDB Usage (Hard Blocker + Scoring)

> "HydraDB has to do real work in your project, not just sit in the README."
> "Be ready to say where it is used and what the project would lose without it."

| # | HydraDB Requirement | How to Verify | Status |
|---|---------------------|-------------|--------|
| C1 | **HydraDB is the primary data store** | Search codebase for `neo4j` or `hydra` imports. HydraDB client instantiated in ≥3 service files. No fallback to SQLite/Postgres for core memory. | ❌ **FAIL** — default backend is `embedded` (SQLite). Services speak only the `GraphStore` contract, never HydraDB directly. The `hydra` backend is verified against **Neo4j 5.26**, and real HydraDB is an *optional, off-by-default* sidecar. |
| C2 | **Graph-native queries, not just key-value** | Cypher queries use `MATCH`, `OPTIONAL MATCH`, variable-length paths, or `algo.*` procedures. No `SELECT * FROM memory_table` patterns. | ✅ **PASS** — 17 `MATCH` forms, variable-length `[r*1..n]`, `UNWIND` in stats; zero SQL in `weave/services/`. |
| C3 | **Uses HydraDB-specific features** | At least ONE of: `algo.SSpaths`, `algo.MSpaths`, property indexes, temporal edge filtering, `UNWIND` batch writes. Not generic CRUD. | ⚠️ **PARTIAL** — property indexes ✅ (16 defined), `UNWIND` ✅, temporal edge filtering ✅. `algo.MSpaths` is implemented but is dead code on every server tested. |
| C4 | **HydraDB does work a vector DB cannot** | Document in README: which queries require graph traversal (conflict resolution, temporal hops, multi-session synthesis) and why vectors fail. | ❌ **FAIL** — README never states which queries need traversal or why vectors fail. Zero occurrences of "vector-only" / "vector DB". |
| C5 | **Object-store economics mentioned** | README explains why HydraDB's S3-backed storage matters for 115K-token archival. Not just "we used HydraDB because it's the hackathon." | ❌ **FAIL** — zero occurrences of "object store", "S3", "archival" or "cold storage" in the README. |
| C6 | **Repo commit history shows HydraDB integration** | `git log --grep="hydra\|cypher\|graph"` returns commits. Integration happened during build, not bolted on at the end. | ⚠️ **WEAK** — only 7 commits total; `--grep=hydra` returns 1 (the squashed initial commit). History does not evidence incremental integration. |
| C7 | **Can explain what is lost without HydraDB** | In submission form or README: one paragraph on why the three-layer architecture collapses without graph-native traversal. | ❌ **FAIL** — no such paragraph anywhere in README or the Section I draft. |

**Section C Result:** ❌ **FAIL** — C1, C4, C5, C7 unmet. C4/C5/C7 are README work. C1 is a product-reality problem (see findings).

---

## Section D: Submission Artifacts Quality

### D1: GitHub Repository

| # | Requirement | How to Verify | Status |
|---|-------------|-------------|--------|
| D1.1 | **README is clear and complete** | Contains: problem, what was built, setup instructions, how HydraDB is used, tech stack, team members. | ⚠️ **PARTIAL** — problem, build, setup, tech stack, HydraDB usage all present and strong. **Missing: team members and attribution.** |
| D1.2 | **Setup instructions work from scratch** | Test on a clean machine / fresh virtualenv. `pip install -e .` + `docker-compose up` + `python scripts/ingest_sample.py` must succeed. | ✅ **PASS** — executed end-to-end during this audit. |
| D1.3 | **Complete source code** | No `TODO` blocks in core logic. All service files implemented. | ✅ **PASS** — zero `TODO`/`FIXME`/stub blocks in 9,543 lines. |
| D1.4 | **Environment/dependency info** | `pyproject.toml`, `requirements.txt`, or `poetry.lock` present. Python version specified. | ✅ **PASS** — `pyproject.toml`, `requires-python = ">=3.11"`, 7 optional-dependency extras. |
| D1.5 | **No secrets in repo** | Run `git log -p | grep -i "api_key\|secret\|password\|token"`. Must return nothing sensitive. | ✅ **PASS** — no secrets in history; only `.env.example` with empty values is tracked. |
| D1.6 | **Code is readable** | Functions have docstrings. Cypher queries are formatted, not one-line strings. Variable names are meaningful. | ✅ **PASS** — docstrings throughout, Cypher is multi-line and formatted, rationale comments explain *why*. |

### D2: Demo Video (≤3 Minutes)

| # | Requirement | How to Verify | Status |
|---|-------------|-------------|--------|
| D2.1 | **Problem stated in first 30s** | Viewer understands why current memory systems fail within 30 seconds. | ⚠️ **BLOCKED** — no video. |
| D2.2 | **What was built is shown** | Architecture slide or code walkthrough of the three layers. | ⚠️ **BLOCKED** — no video. |
| D2.3 | **Live demo of system working** | Screen recording of actual queries running, not mockups. Show terminal or API responses. | ⚠️ **BLOCKED** — no video. |
| D2.4 | **HydraDB usage explained** | At least one query shown with Cypher or graph visualization. Explain WHY graph traversal matters. | ⚠️ **BLOCKED** — no video. |
| D2.5 | **Abstention demonstrated** | Show a query where the system says "I don't know." This is Track 3's hardest requirement. | ⚠️ **BLOCKED** — no video. |
| D2.6 | **Overwritten info demonstrated** | Show old preference → new preference. Query the old one and get the historical answer. | ⚠️ **BLOCKED** — no video. |
| D2.7 | **Cross-session synthesis demonstrated** | Query requiring info from 2+ sessions. Show the synthesized answer. | ⚠️ **BLOCKED** — no video. |
| D2.8 | **Video is ≤3:00** | Check runtime. Trim ruthlessly if over. | ⚠️ **BLOCKED** — no video. |
| D2.9 | **Audio is clear** | No background music drowning out narration. Captions are a plus. | ⚠️ **BLOCKED** — no video. |

### D3: Submission Form

| # | Requirement | How to Verify | Status |
|---|-------------|-------------|--------|
| D3.1 | **All fields filled** | No blank optional fields that should be filled (e.g., tech stack, team contributions). | ⚠️ **MANUAL** — Section I draft is complete except team contributions. |
| D3.2 | **GitHub link works** | Click it. Opens public repo. | ⚠️ **MANUAL** |
| D3.3 | **Demo video link works** | Click it. Opens playable video. | ⚠️ **MANUAL** — no video. |
| D3.4 | **Deployed link (if applicable) works** | If you provided one, test it. If not, that's fine. | ⚠️ **MANUAL** — no deploy; acceptable. |
| D3.5 | **HydraDB usage explanation is specific** | Not "we used HydraDB for storage." Something like "We use HydraDB's `algo.SSpaths` for multi-hop conflict resolution across 30 sessions." | ⚠️ **PARTIAL** — the draft is specific, but it claims `algo.SSpaths` multi-hop as if it runs. It does not. |

**Section D Result:** ❌ **FAIL** — D1 is near-perfect (missing team + attribution). **D2 is entirely unmet: no demo video exists.**

---

## Section E: Judging Criteria Self-Assessment

Rate your project **1–5** on each criterion. Be honest. A 3 is "average for this hackathon." A 5 is "best in track."

### E1: Technical Execution (Weight: High)

| # | Checkpoint | Score | Evidence |
|---|------------|-------|----------|
| E1.1 | Three-layer architecture is fully implemented (not stubbed) | **5** | Three layers fully implemented across `models/`, `services/`, `graph/`. 9,543 lines, zero stubs. |
| E1.2 | Ingestion pipeline works end-to-end (session → episodic → semantic) | **5** | Verified live this run: 8 sessions → 120 nodes / 218 edges / 3 layers. |
| E1.3 | Consolidation ("sleep") runs and resolves conflicts deterministically | **5** | Four explicit policies (`recency`/`frequency`/`confidence`/`trust`); the winning policy is written onto the `Conflict` node for audit. |
| E1.4 | Procedural layer routes queries to different paths based on type | **4** | Four paths, epsilon-greedy, `path_reason: learned` vs `default`. Learned routing is real but the benefit is unmeasured. |
| E1.5 | Abstention router uses graph signals, not just empty result check | **4** | Seven weighted signals incl. topical grounding, uncovered-term IDF, open conflicts. Design is a 5; **real-data F1 of 7–15% drags it to 4**. |
| E1.6 | System handles 30+ sessions without crashing or timing out | **5** | 500 questions × ~50 sessions completed; LoCoMo 300 in 930s wall clock. No crashes. |
| E1.7 | Code is production-quality (error handling, logging, types) | **4** | 86 tests pass, full type hints, docstrings, rationale comments. Logging exists in only 4 modules; none in `services/`. |

**E1 Average Score:** **4.57** / 5

### E2: Use of HydraDB & Graph-Native Approaches (Weight: High)

| # | Checkpoint | Score | Evidence |
|---|------------|-------|----------|
| E2.1 | HydraDB is the primary data store, not a side cache | **2** | **The core miss.** SQLite embedded is the default and the benchmarked path. HydraDB is optional and off by default. |
| E2.2 | Uses graph traversal for queries vectors cannot answer (conflict chains, temporal hops) | **4** | Conflict chains, supersession history and multi-session synthesis genuinely need traversal — and are used. |
| E2.3 | Uses HydraDB-specific features (`algo.SSpaths`, `algo.MSpaths`, property indexes) | **2** | `algo.MSpaths` implemented but never executes; property indexes are real but generic. |
| E2.4 | Graph schema is well-designed (labels, relationships, indexes are intentional) | **5** | 16 intentional indexes, 3 node families, 15 relationship types, bi-temporal `valid_from`/`valid_until`. |
| E2.5 | Object-store backing is leveraged (cold episodic layer, hot semantic layer) | **1** | Not leveraged at all. No cold/hot split, no archival tier. |

**E2 Average Score:** **2.80** / 5

### E3: Product Completeness & Usability (Weight: Medium)

| # | Checkpoint | Score | Evidence |
|---|------------|-------|----------|
| E3.1 | README gets a new user running in <5 minutes | **5** | Verified: venv → install → serve → query, no DB, no key, no Docker. |
| E3.2 | API is documented (OpenAPI spec or endpoint docs) | **4** | FastAPI `/docs` auto-generated; README endpoint table plus evidence-field reference. |
| E3.3 | Demo video is compelling and easy to follow | **1** | **No video exists.** |
| E3.4 | Project has a clear use case (personal AI assistant, coding agent, etc.) | **4** | Clear: persistent memory for LLM agents. Workspace UI shipped. |
| E3.5 | No broken links, missing assets, or placeholder text | **3** | Stale text: README says "62 tests" (actual 86); the `Where did I live before?` sample output no longer matches; README results predate `results/*.json`. |

**E3 Average Score:** **3.40** / 5

### E4: Quality of Results (Weight: High)

| # | Checkpoint | Score | Evidence |
|---|------------|-------|----------|
| E4.1 | Benchmark numbers are reported (LongMemEval or LoCoMo) | **5** | LongMemEval-S 500/500 and LoCoMo 300 — both real releases, not synthetic. |
| E4.2 | Accuracy is measured against a baseline (full-context or naive retrieval) | **2** | Full-context token count is reported; **full-context accuracy is never measured**, so there is no baseline to beat. |
| E4.3 | Abstention accuracy is explicitly measured | **5** | Precision/recall/F1 reported on every run, with a signals dump in `results/abstention-signals.json`. |
| E4.4 | Token efficiency is quantified (tokens/query vs. 115K full context) | **5** | 198× on LongMemEval, 43.6× on LoCoMo. Quantified per question. |
| E4.5 | Ablations prove each layer's contribution (episodic-only vs. semantic-only vs. full) | **2** | Ablation harness works, but the published table is **synthetic-only**, where three of four configs score 100% — it proves nothing. |
| E4.6 | Results are reproducible (benchmark script runs deterministically) | **4** | Scripts are deterministic and stratified sampling is seeded; `results/` is gitignored so nothing is checked in. |

**E4 Average Score:** **3.83** / 5

### E5: Originality (Weight: High)

| # | Checkpoint | Score | Evidence |
|---|------------|-------|----------|
| E5.1 | Three-layer cognitive architecture is novel (not a Mem0/Zep clone) | **4** | Episodic→semantic→procedural with a learned router is a genuinely distinct shape. |
| E5.2 | Conflict-preservation graph (not overwrite) is a distinct approach | **5** | `Conflict` + `SUPERSEDES` + bi-temporal validity, resolved under an explicit policy. Rare. |
| E5.3 | Procedural learning layer for adaptive retrieval is unique | **4** | Epsilon-greedy path learning with `Outcome` nodes — unusual for a memory layer. |
| E5.4 | Abstention router uses graph topology signals (not just empty check) | **5** | Seven graph signals, not an empty-result check. |
| E5.5 | README or demo explicitly contrasts with vector-only approaches | **2** | **Never contrasted.** The README argues against flat stores, not against vectors. |

**E5 Average Score:** **4.00** / 5

### E6: Best Use of HydraDB Award (Separate $500 Prize)

| # | Checkpoint | Score | Evidence |
|---|------------|-------|----------|
| E6.1 | Particularly strong graph data model (temporal edges, conflict nodes, procedural nodes) | **5** | Temporal edges, conflict nodes and procedural nodes are all present and load-bearing. |
| E6.2 | Novel retrieval approach using graph traversal | **4** | `matched_by: graph` — evidence kept purely by occupying the same subject+predicate slot — is a real graph-native retrieval idea. |
| E6.3 | Interesting use of relationships (SUPERSEDES, CONFLICTS_WITH, BEST_PATH_FOR) | **5** | `SUPERSEDES`, `CONFLICTS_WITH`, `RESOLVED_TO`, `BEST_PATH_FOR`, `TRIED`/`SUCCEEDED`/`FAILED`. |
| E6.4 | Use case is hard with vector/relational DBs (temporal conflict resolution) | **5** | Temporal conflict resolution with preserved history is genuinely hard on vector or relational stores. |

**E6 Average Score:** **4.75** / 5

---

## Section F: Winning Differentiation Check

These are the things that separate a "good submission" from a "track winner." Most Track 3 submissions will have some form of memory. Winners have these:

| # | Differentiator | How to Verify | Status |
|---|----------------|-------------|--------|
| F1 | **Abstention is not an afterthought** | The abstention mechanism is a core feature, not a `if not results: return "I don't know"` hack. It uses graph signals (coverage, temporal reachability, conflict status). | ✅ **PASS** — seven weighted signals (entity coverage, result count, topical grounding, uncovered-term IDF, current-vs-historical, open conflicts). Not a hack. |
| F2 | **Conflict resolution is deterministic** | When two facts contradict, the resolution is explainable (recency, frequency, or confidence policy) — not a black-box LLM guess. | ✅ **PASS** — four named policies; the applied policy is persisted on the `Conflict` node. Fully explainable. |
| F3 | **Overwritten info is queryable** | A user can ask "what did I believe before X?" and get a historical answer. This is impossible in Mem0/Zep. | ✅ **PASS** — verified live. Superseded facts keep node, evidence and `valid_until`. |
| F4 | **Cross-session synthesis is demonstrated** | The demo shows a question whose answer ONLY exists by combining Session 3 and Session 12. Not just retrieval from one session. | ✅ **PASS (implemented)** — multi-citation synthesis works; needs to be *shown* in the demo. |
| F5 | **Token savings are quantified** | A chart or table showing tokens/query vs. full 115K context. "We save 95% of tokens" is a powerful demo moment. | ✅ **PASS** — 198× table in README, measured per question on real data. |
| F6 | **Benchmark scores beat a naive baseline** | The ablation study shows episodic-only and semantic-only are worse than the full three-layer system. | ❌ **FAIL** — the ablation table is synthetic-only, where `semantic-only`, `full-weave` and conflict resolution all sit at 100%. **No real-data ablation exists.** |
| F7 | **HydraDB is irreplaceable** | The README has a paragraph: "Without HydraDB, we would need X JOINs in Postgres + Y vector searches + Z custom logic. HydraDB does this in one Cypher query." | ❌ **FAIL** — no such paragraph. |
| F8 | **Demo has a "wow" moment** | The video contains one query that makes the viewer think "I've never seen an AI memory system do that." (e.g., correct abstention, historical preference lookup, conflict explanation) | ⚠️ **BLOCKED** — no video. The material exists (abstention, historical lookup, 198×); it has not been captured. |
| F9 | **Code quality signals professionalism** | Type hints, docstrings, error handling, logging, tests. Judges are engineers — they notice sloppiness. | ✅ **PASS** — 86 tests, full type hints, docstrings, `from __future__ import annotations` throughout, no TODOs. |
| F10 | **Submission form is persuasive** | The "What you built" and "How HydraDB is used" fields are specific and technical, not generic marketing copy. | ⚠️ **PARTIAL** — the Section I draft is specific and technical, but overstates `algo.SSpaths` as running in production. |

**Section F Score:** **6 / 10 passed** (F1–F5, F9). F6 and F7 are fixable today; F8 needs the video; F10 needs one correction.

---

## Section G: Final Go/No-Go Decision

### Hard Blockers (Must ALL Pass)

| Section | Result | Blocking items |
|---------|--------|----------------|
| A: Rules Compliance | ❌ **FAIL** | A10 attribution |
| B: Track 3 Core Fit | ✅ **PASS** | — |
| C: HydraDB Usage | ❌ **FAIL** | C1, C4, C5, C7 |
| D: Submission Artifacts | ❌ **FAIL** | D1.1 (team + attribution), **all of D2 — no video** |

**If any section above is FAIL → DO NOT SUBMIT. Fix first.**

### Scoring Thresholds (Self-Assessment)

| Criterion | Actual | Finalist | Win Track | Grand Champion | Verdict |
|-----------|--------|----------|-----------|----------------|---------|
| E1 Technical Execution | **4.57** | 3.0 | 4.0 | 4.5 | ✅ champion tier |
| E2 HydraDB Usage | **2.80** | 3.5 | 4.5 | 5.0 | ❌ **below finalist** |
| E3 Product Completeness | **3.40** | 3.0 | 3.5 | 4.0 | ⚠️ finalist only (video = 1) |
| E4 Quality of Results | **3.83** | 3.0 | 4.0 | 4.5 | ⚠️ finalist, short of win |
| E5 Originality | **4.00** | 3.5 | 4.5 | 5.0 | ⚠️ finalist, short of win |
| E6 Best Use of HydraDB | **4.75** | 3.0 | 4.0 | 4.5 | ✅ champion tier |
| F Differentiators | **6/10** | 5/10 | 7/10 | 9/10 | ⚠️ finalist, short of win |

**E1–E5 average: 3.72.**

### Final Decision

| Question | Answer |
|----------|--------|
| All hard blockers pass? | ❌ **NO** — A10, C1, C4, C5, C7, D1.1, D2 |
| Average score across E1–E5 ≥ 3.5? | ✅ **YES** — 3.72 |
| At least 7/10 differentiators (Section F) present? | ❌ **NO** — 6/10 |
| Demo video is compelling and under 3 minutes? | ❌ **NO** — no video exists |
| You would be impressed if YOU were the judge? | ⚠️ **By the engineering, yes. By the HydraDB story as currently told, no.** |

**FINAL VERDICT:**

⛔ **NO-GO as of 2026-08-18** — but every blocker except the video is a few hours of work, and the video is the single highest-value remaining item.

**The engineering is finalist-to-champion grade (E1 4.57, E6 4.75). The submission around it is not.** Weave loses points not for what it does but for what it fails to *say* and *show*: no video, no attribution, no "why not vectors", no "what breaks without HydraDB", and an ablation published on synthetic data where three configs tie at 100%.

**The one real structural finding (C1/E2.1).** HydraDB, as it actually ships, is a managed REST context API — not the Bolt/OpenCypher server with `algo.SSpaths` that the specification described. The project handled this correctly and honestly: it built a real Bolt backend (verified on Neo4j 5.26), integrated the real HydraDB as a live-verified retrieval sidecar, and documented the discrepancy in the README. But the *default and benchmarked* path is embedded SQLite, so against a literal reading of "HydraDB is the primary data store," this fails. The fix is not to overclaim — it is to make the honest position **prominent and framed as a finding**, and to make the sidecar carry visible weight in the demo.

---

## Section H: Fix Queue (audit run 2026-08-18)

Ordered by points-per-hour. Everything above the line is a blocker.

| P | Fix | Unblocks | Est. |
|---|-----|----------|------|
| **P0** | **Record the demo video** (≤3:00): abstention, historical preference lookup, cross-session synthesis, one Cypher/graph shot, the 198× number | A7–A9, all of D2, E3.3, F8 | 90 min |
| **P0** | Add **Attribution** section to README — LongMemEval, LoCoMo, model2vec/potion-base-8M, FastAPI, neo4j driver, hydradb-sdk, all with links | A10 | 15 min |
| **P0** | Add **"Why HydraDB, and what breaks without it"** — which queries need traversal, why vectors fail, the Postgres-JOIN counterfactual | C4, C7, E5.5, F7 | 30 min |
| **P0** | Add **object-store economics** paragraph — cold episodic archive vs hot semantic working set for 115K-token histories | C5, E2.5 | 15 min |
| **P0** | Add **Team** section to README (solo build + contribution breakdown) | D1.1, D3.1 | 5 min |
| **P1** | **Run the ablation on real LongMemEval** and publish that table alongside the synthetic one | E4.5, F6 | 45 min |
| **P1** | **Measure a full-context baseline** — accuracy of stuffing the haystack vs Weave's 520 tokens | E4.2 | 45 min |
| **P1** | Correct the Section I form draft: `algo.MSpaths` is implemented with a verified fallback, **not** running in production | D3.5, F10 | 10 min |
| **P2** | Refresh stale README numbers — "62 tests" → 86; regenerate the results table from the newest `results/*.json`; fix the `Where did I live before?` sample output | E3.5 | 20 min |
| **P2** | Commit `results/*.json` (drop from `.gitignore`) so the reported numbers are checkable | E4.6 | 5 min |
| **P2** | Promote the HydraDB sidecar in the demo path so HydraDB visibly does work | C1, E2.1 | 30 min |
| **P3** | Add `logging` to `weave/services/` (currently only 4 modules log) | E1.7 | 30 min |
| **P3** | Add a README section for the LoCoMo 300-question run (measured, unpublished) | E4.1 | 15 min |

**Not recommended:** rewriting git history to manufacture incremental HydraDB commits (C6). The history is honest at 7 commits; faking it risks far more than C6 is worth.

---

## Section I: Submission Form Cheat Sheet

Copy-paste ready answers for the official form:

### Project Name
**Weave** — Three-Layer Cognitive Memory for LLM Agents

### Short Project Description
Weave is a graph-native agent memory system that models human-like memory consolidation across three interconnected layers: episodic (raw conversations), semantic (consolidated facts with conflict resolution), and procedural (learned retrieval strategies). Built on HydraDB, it enables cross-session fact synthesis, chronological tracking, overwritten information preservation, and explicit abstention — solving the core failure modes of vector-based memory systems.

### Problem Being Addressed
Current LLM memory systems (Mem0, Zep, simple RAG) fail on four dimensions critical to long-term agent continuity: (1) they overwrite old facts instead of tracking evolution, (2) they cannot synthesize information scattered across 30+ sessions, (3) they hallucinate when asked about topics never discussed, and (4) they use the same retrieval strategy for every query type. Long-context models drop 30–60% accuracy on 115K-token histories and fail at abstention.

### What You Built
A working memory layer with:
- **Episodic Layer**: Immutable conversation graph in HydraDB (Session→Turn→Utterance)
- **Semantic Layer**: Extracted facts with bi-temporal validity, conflict detection, and background consolidation ("sleep")
- **Procedural Layer**: Learned query-type-specific retrieval paths that adapt based on success rates
- **Abstention Router**: Graph-signal-based abstention that detects missing information before calling the LLM
- **Benchmark Harness**: Evaluation on LongMemEval with ablation studies

### How the Project Uses the HydraDB Open Source Repo

> **Corrected 2026-08-18.** The original draft claimed `algo.SSpaths` /
> `algo.MSpaths` multi-hop and object-store archival as shipped features. They
> are not, and a judge who ran the code would find that out. What follows is
> what the repo actually does.

All three memory layers sit on one graph substrate, behind a `GraphStore`
contract — the services never speak SQL, vectors or dicts, only nodes, edges,
labelled traversal and bounded paths. Two backends implement it:

- **Bolt/OpenCypher** (`WEAVE_BACKEND=hydra`) — temporal edge filtering on
  `valid_from`/`valid_until`, MERGE semantics, transactional rollback, 16
  property indexes (`Entity.canonical_name`, `Fact.is_current`,
  `Conflict.status`, …), `UNWIND` aggregation, and §8.3 consistency modes via
  bookmark chaining. `algo.MSpaths` is implemented for bounded multi-hop
  traversal with a variable-length `MATCH` fallback — **the fallback is what
  executes**, because no server we could reach implements the procedure.
- **Embedded** (default) — the same contract over SQLite, with expression
  indexes, an FTS5 text index and capped-BFS traversal. Zero external services.

**An important finding, stated plainly:** the specification we built from
describes HydraDB as a Bolt/OpenCypher server at `neo4j://localhost:7687`
exposing `algo.SSpaths`/`algo.MSpaths`. The shipping product is a managed REST
context API at `api.hydradb.com` — its integration guide contains no Bolt, no
Cypher, no port 7687, and no such procedures; the `hydradb/hydradb` image does
not exist. So we did both: the Bolt backend is real and verified end-to-end
against **Neo4j 5.26** (the entire 86-test suite passes on it, and the demo
workload produces a byte-identical graph on both backends), and **the real
HydraDB is integrated separately and verified against the live API** as an
episodic retrieval sidecar — `weave sidecar-verify` round-trips a probe, and
the demo corpus resolved 18 of 18 search hits back to local utterance nodes.
Running it for real surfaced three bugs a stub never would (async workspace
provisioning, an uncached readiness re-wait, and a ~1000-token request cap
that 413'd whole sessions).

Without a graph substrate, `"Where did I live before?"` becomes a vector search
for candidates + 4 JOINs for provenance + a hand-written recursive CTE over the
supersession chain + another JOIN for conflict status + application code to
reconcile two datastores that share no identity. In Weave it is one bounded
traversal: match the entity, expand `HAS_FACT`, follow `SUPERSEDES`, filter on
`valid_until`.

### Tech Stack
- **Graph substrate**: `GraphStore` contract with two backends — embedded SQLite property graph (default) and Bolt/OpenCypher via the neo4j driver (verified on Neo4j 5.26)
- **HydraDB**: the managed REST context API, as an episodic retrieval sidecar (`hydradb-sdk`), verified against the live service
- **Backend**: Python 3.11, FastAPI, Uvicorn, Pydantic
- **LLM** (optional): Anthropic Claude for extraction and answer generation; a deterministic rule-based extractor and grounded template answerer run without any key — **all reported benchmark numbers are from the no-key path**
- **Embeddings** (optional): model2vec `potion-base-8M` static embeddings, numpy only
- **Benchmarks**: LongMemEval-S (500 questions) and LoCoMo (300 questions)
- **Infrastructure**: Docker, Docker Compose

### Team Members and Individual Contributions
**Solo build — Emmanuel Bamigboye ([Dotman-Bei](https://github.com/Dotman-Bei)):** graph schema and both backends, the three-layer services (ingestion, extraction, consolidation, retrieval, procedural), abstention router, HydraDB sidecar integration, benchmark and ablation harnesses, workspace UI, and the demo.

---

---

## Section J: Demo Video Shot List (≤ 3:00)

Written from **verified output only** — every command below was run against
the current tree during the audit, and every line shown is what it actually
printed. Nothing here needs an API key or Docker.

**Setup before recording:** `python scripts/ingest_sample.py` (idempotent —
re-run it to reset narration timing), terminal at ~110 columns, font large
enough to read at 720p.

| Time | Shot | Say | Covers |
|---|---|---|---|
| **0:00–0:25** | Title card, then a terminal | "An agent that forgets is an agent you have to re-brief every session. The fix everyone reaches for is stuffing the whole history into context — 115,000 tokens per question. That's expensive, it's slow, and models lose 30–60% accuracy at that length. Worse: asked something never discussed, they answer anyway." | D2.1 |
| **0:25–0:50** | The three-layer diagram from the README | "Weave is three layers on one graph. Episodic — what happened, immutable. Semantic — what's true now, where new facts *supersede* rather than overwrite. Procedural — which traversal answers which kind of question, learned from outcomes." | D2.2 |
| **0:50–1:10** | `weave query "What database do I use?"` → `User uses postgresql and clickhouse. [sess-02, sess-07]` | "Cross-session synthesis. Postgres was mentioned in session 2, ClickHouse in session 7. Neither session contains this answer — the graph does, by collecting every fact on the same subject-predicate slot." | D2.7, F4 |
| **1:10–1:35** | `weave query "Where do I live?"` → `lisbon [sess-06]`, then `weave query "Where did I live before?"` → `User lives in lisbon. Previously: berlin. [sess-06, sess-01]` | "Berlin was overwritten by Lisbon — but the old fact isn't gone. It kept its node, its evidence, and a `valid_until` stamp, with a SUPERSEDES edge between them. So I can ask what I *used to* believe. Mem0 and Zep can't answer this: they mutate the row." | D2.6, F3 |
| **1:35–1:55** | `weave query "What is my blood type?"` → `I don't know — that isn't in the stored conversation history.` then show `tokens=0` | "Never discussed. Weave abstains — and note the token count: **zero**. The decision happens before any model call, from graph signals: topical coverage of the question against the retrieved subgraph, and how much of the question nothing in memory mentions." | D2.5, F1, **F8 wow moment** |
| **1:55–2:25** | Split: `weave/graph/hydra.py` Cypher + the log trace | "That last answer is a graph traversal, not a similarity search — and it has to be. The superseded value is the one the question *doesn't* name, so nearest-neighbour ranks it last by construction. Match the entity, expand HAS_FACT, follow SUPERSEDES, filter on valid_until. In Postgres plus a vector store that's five JOINs, a recursive CTE, and two systems that share no identity." | D2.4, C4 |
| **2:25–2:50** | The benchmark table | "500 real LongMemEval questions, ~103,000 tokens of haystack each. 532 context tokens per query — **194× smaller**. And I'll be straight about the rest: accuracy is 20%, abstention F1 is 14%, and a plain keyword baseline beats our retrieval. The token economics and the conflict model are real; the ranking is the next thing to fix." | D2.3, F5 |
| **2:50–3:00** | Repo URL | "Weave. Graph-native memory that knows what it used to believe — and knows when it doesn't know." | — |

**Recording notes**

* **Do not** claim `algo.SSpaths` runs. It is implemented with a verified
  fallback; the fallback is what executes. A judge may check.
* The honesty beat at 2:25 is a **feature**, not a hedge — judges are engineers
  and every one of them has seen a benchmark table that was quietly cherry-
  picked. Owning the weak numbers is what makes the strong one believable.
* Keep the abstention shot (1:35) uncut and let `tokens=0` sit on screen for a
  beat. It is the single most differentiating frame in the video.
* Captions are worth the 10 minutes — a chunk of judging happens muted.

---

*Audit Version: 1.0 | Track: Memory & Context Retrieval | Project: Weave*
*Run this audit before every submission. A 10-minute audit prevents a 10-hour regret.*
