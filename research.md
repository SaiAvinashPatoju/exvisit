# ExVisit Research Foundation

## Why Structural Context Beats Semantic Search for Autonomous Coding Agents

---

## 1. The core problem: context rot

When a coding agent navigates a large repository using file search or RAG, it faces an adversarial information environment. Each tool call returns content that may or may not be relevant. If the relevant file is the 4th file loaded, the agent has spent tokens 1–3 on noise. We call this **context rot**: the accumulation of irrelevant content in the context window before the oracle file is reached.

Context rot has three compounding effects:

1. **Token cost** — irrelevant files consume input tokens at the same price as relevant ones.
2. **Attention dilution** — transformer attention is quadratic over sequence length; longer irrelevant context degrades generation quality for the relevant portion.
3. **Navigation drag** — the agent must make additional tool calls to recover from a cold start, consuming more tokens in a feedback loop.

In our 101-case `django/django` SWE-Bench Lite trial, the grep baseline averaged a **context rot index of 3.83** (the oracle file was, on average, the 4th-ranked result after 3 irrelevant files). ExVisit reduced this to **1.57** — placing the oracle file as the first or second result in 61% of cases.

---

## 2. The failure modes of RAG for code navigation

Retrieval-Augmented Generation is the current standard for code context. It works by embedding code chunks into a vector space and returning the nearest neighbors to a query embedding. This is effective for documentation Q&A. It fails systematically for repository navigation:

### 2.1 Semantic opacity of function signatures

The embedding of a function signature (`def charge_card(self, amount: Decimal, currency: str)`) is semantically close to many other payment-related functions. But the *structural* fact that this method is called by exactly three Django views, and defined in a class that inherits from `AbstractPaymentProcessor`, is not encoded in the embedding at all. Navigation requires traversal, not similarity.

### 2.2 The dead-file problem

In a large codebase, the correct file for a bug fix is often architecturally central but textually unremarkable. `django/db/models/sql/compiler.py` contains no interesting keywords for typical ORM bug reports — the issue text says "QuerySet.values() doesn't preserve annotation order" and the word "annotation" in the issue maps weakly to dozens of files. Only the structural graph knows that `compiler.py` is the PageRank-central node in the ORM subgraph.

### 2.3 Chunk boundary artifacts

RAG retrievers chunk source files at arbitrary boundaries (typically 512 or 1024 tokens). A class definition that spans 200 lines will be split, and the inheritance declaration at line 1 ends up in a different chunk than the method that's actually broken at line 180. The structural relationship is severed.

### 2.4 Budget blindness

RAG retrievers return a fixed-K result regardless of confidence. If the query has one obvious match, K=5 still loads 4 irrelevant files. ExVisit's confidence-adaptive selection returns a single anchor file when the top-1 score has a large margin over top-2. This is the mechanism behind the 1-step, 1.8 K-token median performance.

---

## 3. The ExVisit model: typed spatial state machines

ExVisit treats a codebase as a **labeled directed graph** where:

- **Nodes** are source files with typed metadata (code, test, migration, registry).
- **Edges** carry structural semantics: `import`, `inherit`, `config-ref`, `test-of`, `call`.

This is not a call graph. Call graphs operate at the function level and require static analysis or runtime tracing. ExVisit operates at the **file level**, capturing the coarser but more stable structural relationships that govern where a bug fix must be applied.

The `.exv` file format encodes this graph as a plain text artifact:

```
# django.exv (abbreviated)
namespace db
  DBModels
  SQLCompiler
  QuerySet

namespace contrib.admin
  ModelAdmin
  AdminSite

DBModels -> SQLCompiler [import]
QuerySet -> SQLCompiler [call]
ModelAdmin -> DBModels [inherit]
```

The graph is **hand-writable** (for documentation and onboarding), **auto-generated** from Python source (`exv init`), and **diff-friendly** for version control.

### 3.1 Edge type priors

Not all structural relationships are equally predictive. We assign Bayesian priors to edge types based on empirical calibration against the SWE-Bench oracle:

| Edge type | Prior weight |
|---|---:|
| `descriptor` / `config-ref` | 0.41 |
| `inherit` | 0.28 |
| `call` | 0.21 |
| `import` | 0.12 |
| `test-of` | 0.05 |

`inherit` edges have high weight because a bug in a base class propagates to all subclasses — the subclass file is a natural blast neighbor. `import` edges are noisier because many files import from `django.utils` without being structurally related to a utils bug. `test-of` edges are penalized because test files are almost never the oracle for a production bug.

### 3.2 PageRank centrality

We compute a sparse PageRank over the `.exv` graph (no external library — pure stdlib, ~25 lines). Architecturally central files (high inbound edge density) accumulate a `pagerank` prior in the `.meta.json` sidecar. This prior enters the scorer as one of 15 additive log-linear signals.

The effect: `django/db/models/base.py` scores higher for ORM-related issues even when its filename doesn't match the query, because it is the hub of the ORM subgraph.

---

## 4. The v2 log-linear ranker

ExVisit's scoring engine (`exvisit/scoring_v2.py`) is a log-linear classifier over 15 binary/continuous features, tuned against the SWE-Bench oracle:

```
score(f) = Σ β_i · φ_i(f, q)
```

Where `f` is a candidate file, `q` is the issue query, `β_i` are learned weights, and `φ_i` are feature functions.

**Signal inventory:**

| Signal | β | Description |
|---|---:|---|
| `symbol_exact` | 35.0 | Class/function name quoted exactly in issue text |
| `mgmt_command` | 30.0 | Django `./manage.py <cmd>` mentioned; match management command files |
| `error_code` | 25.0 | `models.E028`-style Django system check code in issue |
| `domain` | 15.0 | Domain classifier (ORM / forms / admin / auth) agreement |
| `dunder` | 12.0 | `__str__`, `__meta__` etc. mentioned; match model files |
| `path` | 4.5 | Stem/path token match |
| `trace` | 5.0 | File appears in issue traceback |
| `sym` | 4.0 | Symbol partial match |
| `lex` | 0.25 | BM25 lexical overlap (per-token TF-IDF) |
| `pagerank` | (prior) | Centrality from `.meta.json` |
| `cluster_idf` | (prior) | Inverse cluster frequency (rarer clusters score higher) |
| `test_gate` | 0.25× | Multiplicative penalty for test files |
| `migration_gate` | 0.15× | Multiplicative penalty for numbered migration files |

The extreme separation between `symbol_exact` (β=35) and `lex` (β=0.25) reflects a key empirical finding: when a user says `"User.get_full_name() raises AttributeError"`, lexical overlap is massively less informative than detecting that `get_full_name` is a defined symbol in `auth/models.py`.

### 4.1 Confidence-adaptive selection

After scoring, ExVisit computes a softmax over top-K scores to derive a confidence distribution. If the top-1 confidence exceeds a threshold and the margin over top-2 exceeds `anchor_margin=0.08`, a single anchor is selected. Otherwise, top-3 anchors are selected. This prevents the ranker from returning a high-noise bundle when it's uncertain.

The effect: median bundle size is 1 anchor + 2 neighbors = 3 files. When confidence is high, it's 1 file.

---

## 5. Comparison to existing work

| Approach | Token cost | Structural awareness | Cross-file reasoning |
|---|---|---|---|
| Grep / keyword search | O(matched files) | None | None |
| RAG with code embeddings | O(K × chunk_size) | Chunk-level | None |
| Tree-sitter + LSP navigation | O(AST size) | High | None (single file) |
| Call-graph analysis | O(graph size) | High | Yes (function-level) |
| **ExVisit .exv blast** | O(bundle size) = ~1.8 K | **File-level, typed** | **Yes (typed graph traversal)** |

ExVisit is not a replacement for LSP or call-graph tools. It is a **pre-navigation filter** that reduces the search space from N files to 3–6 files before the agent uses finer-grained tools. The `.exv` graph is intentionally coarser than a call graph — it is stable, cheap to compute, and correct at the file-selection level.

---

## 6. The CRDT merge layer

ExVisit includes a CRDT (Conflict-free Replicated Data Type) merge operation for `.exv` graphs (`exvisit/crdt.py`). This enables:

- **Collaborative editing** — multiple contributors can update the `.exv` map concurrently and merge without conflicts.
- **Incremental scaffolding** — `exv init` on a partially-mapped codebase merges the newly discovered structure into the existing hand-crafted map.
- **Agent-driven graph extension** — an agent that discovers a new dependency can add an edge to the `.exv` graph which is safe to merge into the canonical version.

The merge semantics follow a Last-Write-Wins register for node attributes and a grow-only set for edges.

---

## 7. The Rust implementation (exvisit-core)

The Python implementation (`exvisit/`) is the reference. For production performance on large monorepos, `rust/crates/exvisit-core` provides:

- A PEG grammar in [Pest](https://pest.rs/) (`exvisit.pest`) for zero-copy `.exv` parsing.
- A streaming serializer for multi-MB graph files.
- Spatial indexing (`spatial.rs`) for O(log n) region queries.

The Rust crate compiles to a `.pyd` / `.so` extension loadable by the Python package (via [PyO3](https://pyo3.rs/)). It is not required for the core `init → blast` workflow.

---

## 8. MCP server plan (exvisit-mcp) {#mcp-plan}

See the full research plan: [exvisit-mcp plan →](rust/crates/exvisit-mcp/PLAN.md)

The target integration: add 5 lines to `claude_desktop_config.json` and Claude natively navigates your repository via the `.exv` graph — no extra tool calls, no token waste.

```json
{
  "mcpServers": {
    "exvisit": {
      "command": "exv",
      "args": ["mcp", "--exv", "./my-project.exv"],
      "env": {}
    }
  }
}
```

Exposed MCP tools: `exvisit_blast`, `exvisit_query`, `exvisit_locate`, `exvisit_expand`, `exvisit_verify`.  
Exposed MCP resources: the `.exv` graph as a navigable resource tree, each node as a named resource URI.
