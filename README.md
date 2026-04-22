# Project exvisit

**Structural context compiler.** Project exvisit derives a compact architectural map from source code and lets agents query microscopic, topology-aware slices of that map instead of loading entire codebases. In the current Python reference, the practical workflow is scaffold from source, optionally curate or overlay, verify back against source, then query, blast, anchor, and edit against the bounded structure.

```
.exv text
    └── PEG parser (Python / Rust pest)
            └── AST (exvisitDoc / Namespace / Node / Edge)
                    └── CRDT graph (OR-Set · LWW-Map · 2P-Set)
                            └── R-Tree spatial index (world coords u=x·z, v=ln z)
                                    ├── exvisit-query  — shipped microscopic topological extraction
                                    ├── exvisit-blast  — shipped manifest-first debug context bundles
                                    ├── exvisit-anchor — shipped stacktrace → node FQN mapping
                                    ├── exvisit-edit   — shipped AST-bounded surgical edit primitive
                                    └── exvisit-mcp / exvisit-lsp / viewport — planned follow-on interfaces
```

Today the repo ships the Python reference implementation, benchmark harnesses, exvisit-bounded edit tooling, and a mini-SWE sandbox materializer. The Rust workspace, MCP server, LSP daemon, and viewport remain design targets rather than implemented deliverables in this workspace.

---

## Measured results (sample navigation runs, 2026-04-22)

| Metric | Raw source | exvisit slice |
|---|---:|---:|
| Tokens to load full architectural context | 133 844 | **1 015** (131× reduction) |
| Tokens per targeted query | ~8 000+ | **150–350** |
| SWE-bench Lite token reduction (`psf/requests`, 3 cases) | baseline | **91.8%** |
| Steps to orient on a target | 5–9 tool calls | **1 CLI call** |
| Context rot index improvement | baseline | **83.3%** lower |
| Oracle-hit@1 | 0% (control) | **66.7%** |
| Token compression vs compact JSON | baseline | **34.4%** |
| Token compression vs raw source | baseline | **125×** |

These numbers measure navigation economics on the sampled runs listed in `status.md`; they are not a claim of general pass@1 superiority across all SWE-bench tasks.

---

## Repository layout

```
exvisit/          Python reference implementation (ground truth)
  __main__.py   Entry point: python -m exvisit <cmd>
  cli.py        Subcommand dispatch
  ast.py        Core data structures: exvisitDoc, Namespace, Node, Edge
  parser.py     Hand-rolled recursive-descent lexer/parser
  serialize.py  Deterministic canonical serializer
  query.py      Topological extraction (n-hop neighbor slices)
  verify.py     Cross-check declared edges vs real Python imports
  scaffold.py   Auto-generate .exv from any Python repo
  blast.py      Manifest-first context bundle builder
  anchor.py     Stacktrace → exvisit node FQN mapper
  crdt.py       Delta-CRDT engine (OR-Set / LWW-Map / 2P-Set)
  spatial.py    R-Tree spatial index + world-coord transform

rust/           Planned Rust port (design target; not implemented here yet)
  crates/
    exvisit-core/     Parser (pest PEG) + AST + CRDT + spatial index
    exvisit-query/    Headless extraction CLI
    exvisit-mcp/      MCP JSON-RPC architect server
    exvisit-lsp/      LSP diagnostics daemon (tower-lsp)
    exvisit-viewport/ wgpu/WASM Glass Viewport renderer

bench/          Benchmarking harnesses
  token_bench.py           .exv vs JSON vs raw source token counts
  swebench_lite_harness.py SWE-bench Lite navigation economics benchmark
  exvisit_edit.py            AST-bounded surgical edit entry point
  mini_swe_agent.py        Mini-SWE sandbox materializer

spec/           DSL grammar and versioned specifications
  exvisit-dsl-v0.1.md        Live grammar (v0.3 shipped)
  exvisit-dsl-v0.4-draft.md  Typed edges, @L3 constraints, lines= locator (draft)

config/
  blast_presets.json   Named blast bundle profiles (default / test-fix / issue-fix / crash-fix)

examples/
  myslicer.exv           Hand-authored, verify-clean (MySlicerApp, 1 071 tokens)
  myslicer.generated.exv Auto-scaffolded, verify-clean

reports/
  experience_report.md    Agent vs no-agent navigation case studies
  research_2026-04-22.md  Deep research: CRDT redesign, concurrency stress test, v0.4 proposals
  spec_sheet.md           Consolidated spec sheet
```

---

## The `.exv` DSL

A `.exv` file is plain text. In the current workflow it is a derived artifact plus optional curated overlay, not a self-authenticating source of truth. Graphs and canvases are derived views, and the authoritative check remains the underlying source tree plus `exvisit verify`.

### Design axioms

1. **Token annihilation** — no JSON quoting, no repeated keys, no punctuation tax.
2. **Positional tabular rows** — one node per line; columns are fixed by header position.
3. **Adjacency matrix separate** — edges never re-specify endpoints as full paths.
4. **Hierarchical nesting via `@Ln`** — depth carries semantic weight (`@L0` = root, `@L1` = package, `@L2` = file, `@L3` = execution-constraint block).

### Grammar (EBNF, v0.1/v0.3)

```ebnf
file          := namespace+
namespace     := '@L' INT IDENT bounds ( src_glob )? '{' body '}'
body          := ( namespace | node_row | edges_block | comment )*
node_row      := IDENT bounds ( src_path )? ( lines_loc )? ( state_machine )?
bounds        := '[' INT ',' INT ',' INT ',' INT ']'   ; x,y,w,h (local to parent)
src_path      := '"' PATH_GLOB '"' | BAREPATH
lines_loc     := 'lines=' INT '..' INT                 ; v0.4 AST range locator
state_machine := '{' IDENT ( '->' IDENT )+ '}'
edges_block   := '===' 'edges' '===' edge+
edge          := IDENT ARROW IDENT
ARROW         := '->' | '~>'                           ; sync | async
```

### Minimal example

```exvisit
@L0 App [0,0,100,100] {
  @L1 Core [5,5,40,90] "src/core/*.py" {
    Scene       [1,1,12,8]  scene.py        {empty -> loaded -> dirty}
    DicomLoader [14,1,12,6] dicom_loader.py {idle -> loading -> ready}
  }
  === edges ===
  DicomLoader -> Scene
  Scene       ~> Scene
}
```

### Edge semantics

| Arrow | Meaning | Verified? |
|---|---|---|
| `->` | Synchronous call / import dependency | Partially — `exvisit verify` cross-checks Python import-like edges today |
| `~>` | Async / event / reactive | No — informational only |

Current verifier scope is intentionally narrow: it primarily cross-checks Python import relationships. Behavioral, async, ownership, and concurrency edges remain descriptive unless separately validated.

### Wire format guarantees

- Nodes sorted by `(y, x)` within each namespace; edges sorted lexicographically.
- Single-space separators; no trailing whitespace.
- **`parse(serialize(doc)) == doc`** — canonical round-trip invariant.

---

## Architecture: Python Reference Implementation

The Python package (`exvisit/`) is the specification ground truth. All semantics are defined here first; the Rust port mirrors them for performance.

### Data model (`exvisit/ast.py`)

```
exvisitDoc
  └── root: Namespace          @L0 root
        ├── children: [Namespace]   @L1..@Ln sub-trees
        └── nodes: [Node]           leaf nodes in this level
              ├── name: str
              ├── bounds: (x,y,w,h)
              ├── src_path: str?     file or glob
              ├── line_range: (start,end)?  v0.4 AST locator
              └── states: [str]      state machine labels
  └── edges: [Edge]
        ├── src: str  (FQN or bare name)
        ├── dst: str
        └── kind: EdgeKind  (SYNC='->' | ASYNC='~>')
```

**FQN rules:** `ns_path.NodeName` — e.g. `App.Core.Scene`. Bare names allowed when unambiguous within scope.

### Parser (`exvisit/parser.py`)

Hand-rolled recursive-descent parser. Single-pass regex tokenizer with explicit token priority:

```
PATH   >  IDENT    (PATH wins when token contains '/' or '*')
EDGES  >  NS       (=== edges === block header)
```

This avoids the classic lexer-precedence bug where glob paths like `src/*.py` silently lex as identifiers.

### Serializer (`exvisit/serialize.py`)

Deterministic canonical output. Guarantees the `parse(serialize(x)) == x` round-trip required for safe CRDT flush-to-disk.

### Query engine (`exvisit/query.py`)

N-hop topological slice extraction:

1. Resolve target FQN via `exvisitDoc.find_node()`.
2. BFS over the edge adjacency map for `hops` iterations (`direction: in | out | both`).
3. Prune the namespace tree to retain only ancestor chains of kept nodes.
4. Serialize the pruned sub-document.

Result: a self-contained `.exv` fragment — typically 40–350 tokens — that an agent can reason about without reading any source file.

### Verifier (`exvisit/verify.py`)

Cross-references declared `->` edges against real Python `import` / `from … import` statements:

| Diagnostic | Meaning |
|---|---|
| `missing` | Source imports a module but no `->` edge is declared |
| `ghost` | exvisit declares `->` but no matching import found in source |
| `unresolved` | `src_path` not found under repo root |

`~>` edges are **never verified** — they are architectural intent, not import facts.

### Blast bundler (`exvisit/blast.py`)

Manifest-first context bundle builder. Given free-form issue text or a stacktrace:

1. Extract code terms and trace frames from the text.
2. Score all exvisit nodes by TF-IDF relevance + path-match bonus.
3. Expand to N-hop topological neighbors (configurable per preset).
4. Read actual source snippets from disk (capped by `max_snippet_lines`).
5. Emit a `BlastBundle` with selected files, node selection reasons, token estimate, and rendered Markdown.

**Blast presets** (`config/blast_presets.json`):

| Preset | Hops | Max files | Max snippets | Token budget |
|---|---:|---:|---:|---:|
| `default` | 1 | 2 | 2 | 700 |
| `test-fix` | 2 | 3 | 3 | 900 |
| `issue-fix` | 2 | 3 | 4 | 1 100 |
| `crash-fix` | 3 | 4 | 5 | 1 300 |

### Anchor (`exvisit/anchor.py`)

Maps a raw stacktrace or error log to exvisit node FQNs:

- Extracts `<file>:<line>` frames from any text via regex.
- Scores nodes by path-match and line-range overlap.
- Classifies each hit: `ground_zero | direct_import | direct_dependent | structural_neighbor`.

### CRDT engine (`exvisit/crdt.py`)

Delta-based conflict-free replicated data types for multi-agent concurrent editing:

| Structure | CRDT type | Used for |
|---|---|---|
| `ORSet` | Observed-Remove Set | Namespaces (add wins over concurrent remove) |
| `LWWMap` | Last-Write-Wins Map (hybrid logical clock) | Tabular nodes (bounds, metadata) |
| `TwoPSet` | Two-Phase Set | Edges (sticky removes) |

Merge is **commutative, associative, and idempotent** — concurrent edits from multiple agents or humans converge to the same state regardless of application order.

> **Note (Rust redesign target):** `TwoPSet` for edges should be promoted to `AWSet` (Add-Wins Set with causal dots), since architectures legitimately re-introduce removed edges after refactors.

### Spatial index (`exvisit/spatial.py`)

```python
def world_coords(x, y, depth):
    z = 2.0 ** max(depth, 1)
    return x * z, math.log(z)   # u = x·z,  v = ln(z)
```

Translates logical per-namespace coordinates to absolute world coordinates for O(1) average-case spatial queries. The reference implementation uses a flat dict + linear scan (sufficient for ~40–400 nodes). The Rust port uses `rstar::RTree`.

---

## Architecture: Planned Rust Port

The Rust workspace described below is the intended port shape, not a shipped implementation in this repository today.

### `exvisit-core`

Core library shared by all other crates.

| Module | Contents |
|---|---|
| `ast.rs` | `exvisitDoc`, `Namespace`, `Node`, `Edge`, `EdgeKind` |
| `parser.rs` | pest-driven PEG parser consuming `exvisit.pest` grammar |
| `crdt.rs` | `ORSet`, `LWWMap`, `TwoPSet`, `exvisitGraph` |
| `spatial.rs` | `rstar::RTree<NodeEnvelope>` + `world_coords()` |
| `serialize.rs` | Canonical serializer (byte-for-byte equivalent to Python output) |

**Key dependency:** `pest 2.7` for the grammar; `rstar 0.12` for the spatial index.

**Grammar file `exvisit.pest`** enforces token priority at the grammar level — `path` (containing `/` or `*`) must appear before `ident` in alternations, replicating the Python lexer fix.

### `exvisit-query`

Planned headless CLI crate. The semantics are defined by the Python reference implementation first.

### `exvisit-mcp`

Planned MCP JSON-RPC server. When a human alters the canvas (e.g., splits a monolith node), it would:

1. Calculates the architectural delta.
2. Exposes a structured "Phase-Refactor Plan" via JSON-RPC.
3. Executing agents read this plan to perform file movements and import refactoring.

Prevents LLM architectural hallucinations by making structural changes a protocol, not a prompt.

**Dependencies:** `jsonrpsee 0.22`, `tokio 1`.

### `exvisit-lsp`

Planned Language Server Protocol daemon built with `tower-lsp 0.20`. It would cross-reference live code imports against the exvisit adjacency model and emit import-drift diagnostics.

**v0.4 diagnostics:**

| Code | Severity | Condition |
|---|---|---|
| `exvisit001` | error | `-imports->` declared but no matching import in source |
| `exvisit002` | error | Real import exists with no `-imports->` edge |
| `exvisit003` | error | `reads_state @thread=X` and `writes_state @thread=Y` on same node without sync edge |
| `exvisit004` | warn | `@reentrant=false` node in a cycle of `-call->` edges |
| `exvisit005` | warn | `@thread=X` edge but no matching thread primitive in source |
| `exvisit006` | info | Node emits but zero listeners — orphaned signal |

### `exvisit-viewport`

Planned Glass Viewport ZUI canvas. Compiled to WebAssembly via `wasm-bindgen 0.2`.

- Receives frustum-culled bounding box arrays from the R-Tree.
- Renders infinite grid and nodes via screen-space fragment shaders (`grid.wgsl`).
- All business logic is in `exvisit-core`; the viewport is purely presentation.
- Mouse events (pan, zoom, drag) pipe directly to the CRDT sync daemon.

**Dependencies:** `wgpu 0.20`, `wasm-bindgen 0.2`.

---

## CLI Reference

```
python -m exvisit <command> [options]
```

| Command | Description |
|---|---|
| `parse <file>` | Parse and report namespace/node/edge counts. `--roundtrip` validates the round-trip invariant. |
| `query <file> --target <FQN> [--neighbors N] [--direction in\|out\|both]` | Extract a minimal topology slice around a target node. |
| `deps <file> --target <FQN> [--hops N]` | Outbound dependency slice. |
| `callers <file> --target <FQN> [--hops N]` | Inbound caller slice. |
| `graph <file>` | Round-trip through the CRDT graph and re-serialize. |
| `verify <file> --repo <path>` | Cross-check edges vs real imports. Exits 1 on errors. |
| `init --repo <path> [--out <file>] [--root-name NAME]` | Auto-scaffold a `.exv` from a Python repo. |
| `blast <file> [--issue-text TEXT \| --issue-file F \| --error-file F] [--preset NAME] [--repo path] [--json]` | Build a manifest-first debug context bundle. |
| `anchor <file> [--stacktrace <file>]` | Map stacktrace frames to exvisit node FQNs with roles. |

Auxiliary tools shipped in `bench/`:

| Command | Description |
|---|---|
| `python bench/exvisit_edit.py --file F --locator L --old X --new Y` | Perform an AST-bounded edit only if `X` appears exactly once inside locator `L`. |
| `python bench/mini_swe_agent.py --manifest M --case-id ID --out-dir D` | Materialize a constrained mini-SWE sandbox with exvisit-only navigation shims. |

---

## DSL Roadmap: v0.4 (Draft)

The v0.4 spec (`spec/exvisit-dsl-v0.4-draft.md`) is a backward-compatible superset of v0.3. New syntax is additive; unknown tags are preserved round-trip.

### Typed edges

```exvisit
Scissors     -call->         MeshOps
MeshOps      -writes_state-> Scene       @thread=worker
PyVistaW     -reads_state->  Scene       @thread=main
Layout       -renders->      PyVistaW
Scene        -emits->        SceneChanged
ObjectPanel  -listens->      SceneChanged
```

Verified kinds: `call`, `import`, `reads_state`, `writes_state`, `renders`, `tests`.
Informational: `emits`, `listens`, `owns`.

### `lines=` AST range locator

```exvisit
Scissors [1,6,10,4] scissors_tool.py lines=88..312 {idle -> drawing -> cut}
```

Enables `exvisit snippet <fqn>` to emit exactly those lines — deterministic token budgeting.

### `@L3` execution-constraint blocks

```exvisit
@L3 Scene:runtime "maxmorph/core/scene.py" {
  @thread     = main
  @reentrant  = false
  @mutex      = _objects_lock
  @invariant  "objects map stable between begin_batch/end_batch"
}
```

Thread/mutex/reentrancy boundaries as first-class data — enables exvisit003/exvisit004 LSP rules.

### Sidecar metrics (`.exv.metrics`)

Auto-generated, git-ignored. Injects live telemetry onto nodes:

```
# schema 1
<fqn>  calls=<n>  p50=<ms>  p95=<ms>  errors=<n>  thread=<tag>
```

`exvisit verify --metrics` promotes `thread=mixed!` (observed multi-thread on `@thread=single` node) to a hard error.

### Migration

```
exvisit upgrade <file>     # rewrites -> as -imports->/-call->, ~> as -emits->
exvisit verify --strict    # v0.4-only syntax; exvisit verify accepts both
```

---

## Benchmarking

### Token efficiency (`bench/token_bench.py`)

Compares `.exv` against:

- **Compact JSON** equivalent of the same AST.
- **Indented JSON** (fully readable).
- **Raw source** files referenced by the exvisit nodes.

Uses `tiktoken cl100k_base` when available; falls back to a calibrated 4-char/token estimator.

### SWE-bench Lite harness (`bench/swebench_lite_harness.py`)

Offline-first navigation-economics benchmark. For each case:

1. **Precompute** — materialise the target repo, scaffold an exvisit, precompute blast bundles per issue.
2. **Run** — execute two strategies:
   - **Control**: keyword grep → file ranking → source snippet reads.
   - **exvisit**: `exvisit blast` bundle (topology-aware, manifest-first).
3. **Measure** four metrics:
   - `input_tokens` — tokens consumed in the navigation phase.
   - `steps` — tool calls made.
   - `context_rot_index` — files read that are irrelevant to the gold patch.
   - `oracle_hit` / `oracle_hit_at_1` — whether the gold patch files appear in top selection.

Current runner additions:

- resumable manifests and results files for long repo sweeps
- trajectory usage parsing with optional per-model pricing files
- per-strategy copy-on-start workspaces via `--workspace-root`
- sandbox materialization support via `bench/mini_swe_agent.py`

**Smoke trial results (psf/requests, 3 cases):**

| Metric | Control | exvisit | Δ |
|---|---:|---:|---:|
| Input tokens | ~12 000 | ~980 | **−91.8%** |
| Steps | ~9 | ~1.8 | **−80%** |
| Context rot | 0.72 | 0.12 | **−83.3%** |
| Oracle-hit@1 | 0% | 66.7% | **+66.7 pp** |

---

## Invariants — Do Not Break

- `parse(serialize(doc)) == doc` — canonical round-trip.
- `~>` edges are informational only — never verified as imports.
- Edge endpoints may be bare names; dotted FQNs required only on collisions.
- CRDT merge is commutative, associative, idempotent.
- Source code remains authoritative. `.exv` should be scaffolded from source or re-verified against source before relying on it for automation.

---

## Quick start

```bash
# Parse and inspect
python -m exvisit parse examples/myslicer.exv

# Topology query — 2-hop slice around Scissors
python -m exvisit query examples/myslicer.exv --target Scissors --neighbors 2

# Verify edges against real source
python -m exvisit verify examples/myslicer.exv --repo C:/B/MySlicerApp

# Auto-scaffold from any Python repo
python -m exvisit init --repo C:/B/MyProject --out project.exv

# Build a blast bundle from an issue description
python -m exvisit blast examples/myslicer.exv --issue-text "crash in scissors tool" --preset crash-fix

# Map a stacktrace to exvisit nodes
python -m exvisit anchor examples/myslicer.exv --stacktrace trace.txt

# Apply a surgical edit within a specific AST locator
python bench/exvisit_edit.py --file sample/sessions.py --locator Session.resolve_redirects --old "value = 'redirect'" --new "value = 'redirect-fixed'"

# Token efficiency comparison
python bench/token_bench.py examples/myslicer.exv C:/B/MySlicerApp

# SWE-bench Lite harness
python bench/swebench_lite_harness.py precompute --cache-dir bench/.cache/requests --manifest bench/.cache/requests/manifest.json --repos psf/requests --limit 3
python bench/swebench_lite_harness.py run --manifest bench/.cache/requests/manifest.json --out bench/.cache/requests/results.json --workspace-root bench/.workspaces/requests

# Materialize a mini-SWE sandbox for a case
python bench/mini_swe_agent.py --manifest bench/.cache/requests/manifest.json --case-id <case-id> --out-dir bench/.cache/requests/sandbox
```

---

## Development status

| Component | State |
|---|---|
| Spec DSL v0.1/v0.3 | Live |
| Spec DSL v0.4 | Draft — see `spec/exvisit-dsl-v0.4-draft.md` |
| Python reference impl | Shipping — parse/query/scaffold/verify/blast/anchor/edit paths covered by tests |
| Rust port | Planned |
| `exvisit-mcp` / `exvisit-lsp` / viewport | Planned |
| SWE-bench harness | Shipping — resume, pricing ingestion, and isolated workspaces |
| Blast bundler | Shipping |
| Anchor mapper | Shipping |
| `exvisit_edit` tool | Shipping |
| Mini-SWE sandbox materializer | Shipping |

## License

MIT OR Apache-2.0

