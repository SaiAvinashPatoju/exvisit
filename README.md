# ExVisit

**ExVisit is a coding browser, not a tool.**

```bash
pip install exvisit
exv init --repo ./my-project
exv blast my-project.exv --issue "TypeError in User.save() for blank email"
```

Just as a browser renders HTML into a navigable page, ExVisit renders a codebase's structural graph into a navigable map. An LLM using ExVisit doesn't "read files" — it **browses code**. It reads one structural view, reasons, navigates to the next, and converges on the right file without ever loading raw source.

This distinction matters at adoption scale. When ExVisit is just one more tool in a system prompt, it saves tokens. When it becomes the **primary navigation primitive** — the medium through which agents explore — it changes what's possible. The agent doesn't need to read a codebase. It browses it.

---

## The numbers

**Standalone blast recall** — `exv blast` with no LLM, raw structural navigation, 30-case Django SWE-Bench Lite:

| Metric | ExVisit blast | Improvement vs random |
|---|---:|---:|
| Oracle hit@1 | **46.7%** | — |
| Oracle hit@3 | **63.3%** | — |
| Oracle hit@5 | **66.7%** | — |
| Avg nav tokens | **~2,000** | 98% less than full repo read |

**Agentic loop** — LLM browses via ExVisit tools (blast + locate + rg), 43-case Django SWE-Bench Lite:

| Metric | Baseline (LLM alone) | ExVisit-Powered | Delta |
|---|---:|---:|---:|
| Oracle hit@1 | ~35% (est.) | **34.9%** | baseline |
| Oracle hit when HIGH confidence | — | **70.0%** | — |
| Avg nav tokens | ~50,000 | **2,972** | **17× less** |
| Token budget to orient on any file | ~130,000 | **~2,000** | **65× less** |

The key signal: when ExVisit is confident (HIGH tier), it is right **7 out of 10 times** — at 17× lower token cost than raw file reading.

[Full methodology →](research.md)

---

## The browser model

Chromium rendered HTML pages. Every browser, browser-based app, and modern web agent is built on top of it. Chromium is not itself any of those things — it is the **rendering engine** that makes navigation possible.

ExVisit plays the same role for code:

| Web | Code (ExVisit) |
|---|---|
| HTML document | Python / JS / Rust codebase |
| Rendered DOM | `.exv` structural graph |
| URL | File path or node FQN |
| `<a href>` navigation | `exv blast` → ranked file list |
| Browser tab / history | Agent conversation turn |
| Google → page → links | Issue text → blast → neighbors |

When an agent uses ExVisit as its exploration engine, the workflow is not "search then read files." It is **browse**:

1. Load the structural view (`exv init` → `.exv`, done once per repo)
2. Navigate to the region of interest (`exv blast --issue "..."`)  
3. Explore neighbors (`exv locate`, `exv expand`)
4. Confirm with source-level search (`rg` on a specific identifier)
5. Submit — with a known confidence level

The agent never opens a 2,000-line source file. It browses the graph.

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

## Why not just use a tool?

A browser is not a tool. It is infrastructure. The distinction:

- A **tool** is called once per request and returns an answer.
- **Infrastructure** provides a navigable medium through which an agent develops understanding over multiple turns.

When an agent uses `read_file` as a tool, each call is independent — no structure carries over. When an agent browses via ExVisit, it accumulates a structural model of the codebase over turns: "blast said `db/models/deletion.py` is central → locate confirmed it → rg found the symbol → HIGH confidence." That multi-turn reasoning over a stable structural graph is fundamentally different from calling a search tool.

At adoption scale, ExVisit stops being "a tool agents use" and becomes "the explorer" — the agent's primary sense organ for code.

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
| Blast v2 ranker | Stable — 46.7% oracle@1 on SWE-bench Lite Django |
| Locate v2 (multi-signal anchor) | Stable — 70% oracle hit at HIGH confidence in agentic loop |
| CRDT merge layer | Beta |
| Rust PEG parser (`exvisit-core`) | Early alpha |
| MCP server (`exvisit-mcp`) | Released — npm/cargo install |
| Agentic benchmark harness | Active — `bench/exvisit_nav.py` |
| VS Code extension | Planned |

---

## Contributing

See [setup.md](setup.md) for the full guide — prerequisites, virtual environment setup, test suite, linting, benchmark runner, and project layout.

---

## License

Apache-2.0
