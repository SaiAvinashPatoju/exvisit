# ExVisit — Developer Setup Guide

This is the contributor setup guide. It covers: forking, local install, running tests, building the CLI, and working with the Rust extension.

---

## Prerequisites

| Tool | Required version | Notes |
|---|---|---|
| Python | 3.11 or 3.12 | 3.13 untested |
| Git | Any recent | Git Bash required on Windows for the benchmark runner |
| Rust / Cargo | 1.78+ | Only needed to build `exvisit-core` Rust extension |
| `maturin` | 1.5+ | Only needed for Rust extension |

---

## 1. Fork and clone

```bash
# Fork on GitHub first, then:
git clone https://github.com/<you>/exvisit
cd exvisit
```

---

## 2. Create a virtual environment

```bash
python -m venv .venv

# Unix / macOS
source .venv/bin/activate

# Windows PowerShell
.\.venv\Scripts\Activate.ps1

# Windows CMD
.\.venv\Scripts\activate.bat
```

---

## 3. Install in editable mode with all dev extras

```bash
pip install -e ".[dev]"
```

This installs:
- The `exvisit` package in editable mode (changes to `exvisit/` take effect immediately)
- `pytest`, `pytest-asyncio`, `ruff`, `mypy`
- The benchmark stack: `tiktoken`, `datasets`, `openai`, `anthropic`, `google-genai`, `tqdm`
- The MCP server stack: `mcp`, `uvicorn`, `fastapi`

Verify the CLI is on PATH:

```bash
exv --help
exvisit --help
```

Both commands should print the command list. They are identical entry points.

---

## 4. Run the test suite

```bash
pytest
```

Expected output: all tests passing. The suite is fast (< 5 seconds) — no network calls, no disk-heavy fixtures.

```
tests/test_blast.py          — blast bundle construction, v2 scoring, confidence-adaptive selection
tests/test_exvisit.py        — parser, serializer, query
tests/test_exvisit_edit.py   — CRDT merge and edit operations
tests/test_scoring_v2.py     — log-linear ranker, signal extraction, anchor selection
tests/test_swebench_harness.py — benchmark harness unit tests
tests/test_mini_swe_agent.py — mini SWE agent integration
tests/test_runner_agent.py   — runner agent
```

---

## 5. Running a single test file

```bash
pytest tests/test_scoring_v2.py -v
```

---

## 6. Linting

```bash
ruff check exvisit/
```

Config lives in `pyproject.toml` under `[tool.ruff]`. Line length is 100. The project does not use `black` — `ruff format` is acceptable.

---

## 7. Type checking

```bash
mypy exvisit/
```

Type checking is non-strict (many dynamic patterns in the parser). The goal is to catch obvious bugs in new code, not achieve 100% strict annotation coverage.

---

## 8. Working with `.exv` files

The quickest way to understand the format is:

```bash
# Parse and inspect
exv parse examples/myslicer.exv

# Generate .exv from a Python project (uses scaffold.py)
exv init --repo path/to/your/project --out my-project.exv

# Run the blast ranker with a test query
exv blast my-project.exv --issue "bug in payment processing"
```

The `examples/` directory contains committed `.exv` samples that you can use for quick iteration without a real project.

---

## 9. Building the Rust extension (optional)

The core Python implementation (`exvisit/`) does not require Rust. The Rust crates in `rust/` are optional accelerators and the start of the production implementation.

```bash
cd rust
cargo build --workspace      # debug build, all crates
cargo test --workspace       # run Rust tests
```

To build the Python extension wheel from `exvisit-core`:

```bash
pip install maturin
cd rust/crates/exvisit-core
maturin develop               # installs .pyd/.so into the active venv
```

The Python package will prefer the Rust implementation when it's present (not yet implemented — stub in place).

---

## 10. Running the benchmark

The benchmark requires:
- An API key for at least one supported provider (Anthropic, OpenRouter, Google).
- `git` on PATH (used to clone and checkout SWE-Bench repos).
- Git Bash on Windows (the shell runner `run_sonnet_exvisit.sh` requires bash).

```bash
# Set your API key
export OPENROUTER_API_KEY="sk-or-v1-..."
export EXVISIT_MODEL="nvidia/nemotron-3-super-120b-a12b:free"

# Run 10 cases to verify your setup
python scratch/run_vnext_openrouter.py \
  --limit 10 \
  --out bench/.cache/django_trial/results_test.json \
  --workspace-root bench/.cache/django_trial/workspaces/test_run
```

All benchmark artifacts go into `bench/.cache/` which is gitignored. Never commit benchmark cache.

---

## 11. Project layout

```
exvisit/            — Python package (the core implementation)
  __init__.py       — public API exports
  cli.py            — argparse entry point (exvisit + exv)
  ast.py            — data structures: Namespace, Node, Edge, exvisitDoc
  parser.py         — .exv text → exvisitDoc
  serialize.py      — exvisitDoc → .exv text (roundtrip)
  scaffold.py       — Python repo → .exv + .meta.json (auto-generation)
  blast.py          — blast bundle builder (v2 ranker + typed edge traversal)
  scoring_v2.py     — log-linear multi-signal ranker
  graph_meta.py     — .meta.json sidecar: pagerank, clusters, symbols
  query.py          — graph query engine
  anchor.py         — anchor selection + report generation
  crdt.py           — CRDT merge for .exv graphs
  verify.py         — structural consistency checker
  edit_tool.py      — structured edit operations
  spatial.py        — R-tree spatial index (for 2D world-coord queries)

config/             — scoring weights and bundle presets (runtime config)
  blast_betas.json  — v2 scoring β weights
  blast_presets.json — bundle size presets per issue type

spec/               — format specification documents
  exvisit-dsl-v0.4-draft.md — current DSL spec

examples/           — committed .exv sample files

tests/              — pytest test suite

bench/              — benchmark harness
  swebench_lite_harness.py
  runner_agent.py
  mini_swe_agent.py
  token_bench.py
  bench/.cache/     — gitignored: cloned repos, results, workspaces

rust/               — Rust implementation (optional)
  crates/
    exvisit-core/   — PEG parser + AST (Pest grammar)
    exvisit-mcp/    — MCP server (planned)
    exvisit-lsp/    — LSP server (planned)
    exvisit-query/  — Query engine (planned)

scratch/            — throwaway research scripts (not part of the package)
```

---

## 12. Making a change

1. Create a branch: `git checkout -b feat/my-change`
2. Edit code under `exvisit/`.
3. Add a test in `tests/` that covers the new behavior.
4. Run `pytest` and `ruff check exvisit/`.
5. Open a pull request against `main`.

There are no required commit message formats yet. Be descriptive.

---

## 13. Config file locations

The scoring weights and bundle presets are in `config/`:

```
config/blast_betas.json    — scoring β weights (v2)
config/blast_presets.json  — max_files per issue type
```

These are loaded at runtime by `scoring_v2.load_v2_config()` and `blast.load_blast_presets()`. Changes take effect immediately without reinstalling the package. To experiment with scoring behavior, edit `blast_betas.json` directly.

---

## 14. Known issues

- The Rust PEG parser does not yet fully handle all edge type annotations. Use the Python parser for anything beyond the basic smoke test.
- `exv edit` is beta. The CRDT merge is correct but the CLI surface for conflict resolution is minimal.
- Windows Git Bash is required for the SWE-Bench harness runner. Native PowerShell is not supported as the bash runner target.
