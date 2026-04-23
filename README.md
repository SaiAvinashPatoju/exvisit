# ExVisit

**73× token reduction. 41× better rank-1 file precision. One command.**

```bash
pip install exvisit
exv init --repo ./my-project
exv blast my-project.exv --issue "TypeError in User.save() for blank email"
```

ExVisit is a **structural context compiler** for AI coding agents. Instead of dumping raw source code into an LLM context window, ExVisit builds a typed spatial map of your codebase — a `.exv` graph — then uses a log-linear multi-signal ranker to pull exactly the 3–6 files the agent needs. No RAG server. No embeddings. No vector database. Pure graph arithmetic on the structure you already have.

---

## The numbers

On a 101-case `django/django` SWE-Bench Lite trial:

| Metric | Grep baseline | ExVisit v2 | Improvement |
|---|---:|---:|---:|
| Avg input tokens | 129,652 | **1,763** | **−98.6%** |
| Avg agent steps | 5 | **1** | **−80%** |
| Context rot index | 3.83 | **1.57** | **−58.9%** |
| Oracle rank-1 precision | 0.99% | **40.6%** | **41×** |
| Oracle file hit rate | 7.9% | **61.4%** | **+53 pp** |

[Full methodology and failure analysis →](benchmark.md)

---

## What it actually does

```
my-project/
├── auth/
│   ├── models.py          ← Node: AuthModels
│   └── forms.py           ← Node: AuthForms
├── payments/
│   ├── models.py          ← Node: PaymentModels
│   └── views.py           ← Node: PaymentViews
└── ...
```

After `exv init`:

```
# my-project.exv — generated structural map
AuthModels -> AuthForms [import]
PaymentViews -> PaymentModels [import]
PaymentModels -> AuthModels [inherit]
```

After `exv blast my-project.exv --issue "Stripe webhook fails on invalid card"`:

```
# Blast bundle (3 files, 1,840 tokens)
payments/views.py     ← anchor (score: 42.1)
payments/models.py    ← neighbor via [inherit]
auth/models.py        ← neighbor via [inherit]
```

The agent gets exactly what it needs in one call. Not a 130K soup of every file in the project.

---

## Installation

```bash
pip install exvisit            # core CLI only
pip install "exvisit[bench]"   # + benchmark stack (tiktoken, datasets, openai, anthropic)
pip install "exvisit[mcp]"     # + MCP server for Claude Desktop / Cursor
pip install "exvisit[dev]"     # + all extras + test/lint tooling
```

Requires Python 3.11+. No mandatory Rust build step (Rust extensions are optional acceleration).

---

## Core commands

| Command | What it does |
|---|---|
| `exv init --repo .` | Scaffold a `.exv` graph and `.meta.json` sidecar from a Python project |
| `exv blast <file.exv>` | Select the optimal file bundle for an issue / query |
| `exv query <file.exv> AuthModels` | Navigate the graph from a named node |
| `exv verify <file.exv>` | Validate `.exv` syntax and edge consistency |
| `exv edit <file.exv>` | Apply a structured edit to the graph (CRDT merge) |
| `exv anchor <file.exv>` | Show the ranked anchor selection report |

All commands have `--help`. The short form (`exv`) and the long form (`exvisit`) are identical.

---

## The `.exv` format

`.exv` is a plain-text graph format. It is intentionally human-readable and version-control-friendly.

```
# myslicer.exv
namespace core
  SlicerEngine
  SlicerConfig [config-ref: SlicerEngine]

namespace io
  FileReader
  FileWriter

SlicerEngine -> FileReader [import]
SlicerEngine -> FileWriter [import]
FileReader -> SlicerConfig [config-ref]
```

Edge types: `import`, `inherit`, `config-ref`, `test-of`, `call`.
Node types: `code`, `test`, `migration`, `registry`.

The format has a [formal grammar spec](spec/exvisit-dsl-v0.4-draft.md) and a [Rust PEG parser](rust/crates/exvisit-core/src/exvisit.pest) in progress.

---

## Why not just use RAG?

RAG embeds semantic meaning at the function/chunk level. It is excellent at "which chunk of docs answers this question." It is terrible at:

- **Cross-file dependency chains** — knowing that `PaymentView` calls `StripeClient.charge()` which is defined in `payments/integrations.py` requires graph traversal, not nearest-neighbor lookup.
- **Structural inheritance** — `AdminMixin` in `auth/mixins.py` affects `OrderAdmin` in `shop/admin.py` through a 3-hop inheritance chain. No vector similarity captures that.
- **Token budget discipline** — RAG retrievers return fixed-K chunks regardless of relevance margin. ExVisit's confidence-adaptive selection returns 1 file when highly certain and up to 5 files when uncertain.

[Full argument in research.md →](research.md)

---

## Architecture

```
.exv file (text)
      │
      ▼
  exvisit.parser        — hand-rolled recursive descent
      │
      ▼
  exvisit.ast           — Namespace / Node / Edge / exvisitDoc
      │
      ▼
  exvisit.scaffold      — Python repo → .exv + .meta.json
      │ (precompute)
      ▼
  graph_meta.NodeMeta   — per-node: fqn, symbols, loc, pagerank, cluster
      │
      ▼
  scoring_v2.score_nodes_v2   — log-linear ranker (15+ signals)
      │
      ▼
  blast.build_blast_bundle    — typed edge traversal + precision guards
      │
      ▼
  Compact bundle (≤6 files, ≤2K tokens) → agent
```

---

## Project status

| Component | Status |
|---|---|
| Core parser / AST | Stable |
| Python scaffolder (`exv init`) | Stable |
| Blast v2 ranker | Stable |
| CRDT merge layer | Beta |
| Rust PEG parser (`exvisit-core`) | Early alpha |
| MCP server (`exvisit-mcp`) | Planned |
| VS Code extension | Planned |

---

## Contributing

See [setup.md](setup.md) for the full guide — prerequisites, virtual environment setup, test suite, linting, benchmark runner, and project layout.

---

## License

MIT OR Apache-2.0 (your choice).
