# ExVisit

**Structural context compiler for AI coding agents � find the right files without an LLM.**

```bash
pip install exvisit
exv init --repo ./my-project
exv blast my-project.exv --issue "TypeError in User.save() for blank email"
```

ExVisit renders a codebase's structural graph into a navigable map. An agent using ExVisit doesn't read files � it **browses code**. It reads one structural view, reasons, and navigates to the next, converging on the right file without ever loading raw source.

---

## Benchmark results � v0.5.0

**No-LLM oracle navigation** on SWE-bench Lite (Django subset, 32 cases):

| Metric | ExVisit v0.5.0 | ExVisit v0.4.x |
|---|---:|---:|
| Oracle hit@1 (exact file match) | **68.8%** | 46.7% |
| Avg files in bundle | **4.2** | 3.1 |
| Avg tokens per query | **~2,000** | ~2,000 |
| Token reduction vs full-repo read | **97.5%** | 98% |

**68.8% hit rate with zero LLM calls** � the correct file is in the returned bundle more than 2 out of 3 times, at 97.5% lower token cost than reading the full repo.

**Agentic loop** (LLM + ExVisit tools, 43-case Django SWE-bench Lite):

| Metric | Value |
|---|---:|
| Oracle hit when HIGH confidence | **70.0%** |
| Avg nav tokens | **~2,972** |
| Token reduction vs full-repo read | **94%** |

---

## How it works

```
my-project/
+-- auth/
�   +-- models.py          ? Node: AuthModels
�   +-- forms.py           ? Node: AuthForms
+-- payments/
�   +-- models.py          ? Node: PaymentModels
�   +-- views.py           ? Node: PaymentViews
+-- ...
```

After `exv init`:

```
# my-project.exv
AuthModels -> AuthForms [import]
PaymentViews -> PaymentModels [import]
PaymentModels -> AuthModels [inherit]
```

After `exv blast my-project.exv --issue "Stripe webhook fails on invalid card"`:

```
# Blast bundle (3 files, ~1,840 tokens)
payments/views.py     ? anchor (score: 42.1)
payments/models.py    ? neighbor via [inherit]
auth/models.py        ? neighbor via [inherit]
```

The agent gets the 3 files most likely to contain the bug � not a 130K token dump of the entire project.

---

## Installation

### Python CLI (recommended)

```bash
pip install exvisit                  # core CLI only
pip install "exvisit[mcp]"           # + MCP server deps (fastapi, uvicorn, mcp)
pip install "exvisit[dev]"           # + all extras + test/lint tooling
```

Requires **Python 3.11+**. No Rust build step required.

### MCP server binary (for Claude Desktop / Cursor / VS Code)

Download the pre-built binary from the [latest release](https://github.com/SaiAvinashPatoju/exvisit/releases/latest):

| Platform | Asset |
|---|---|
| Windows x64 | `exvisit-mcp-windows-amd64.exe` |
| macOS Apple Silicon | `exvisit-mcp-macos-arm64` |
| macOS Intel | `exvisit-mcp-macos-x64` |
| Linux x64 | `exvisit-mcp-linux-amd64` |

The binary is a self-contained MCP stdio server. It requires **Python + `exvisit`** installed separately � it delegates all work to `exv` / `python -m exvisit`.

### npm (installs binary automatically)

```bash
npm install -g exvisit-mcp
```

---

## MCP setup guide

The `exvisit-mcp` binary speaks JSON-RPC 2.0 over stdin/stdout. Configure it in your AI client � **do not double-click it** (it has nothing to talk to without a client).

### Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "exvisit": {
      "command": "C:\\path\\to\\exvisit-mcp-windows-amd64.exe",
      "args": []
    }
  }
}
```

Or if installed via npm:

```json
{
  "mcpServers": {
    "exvisit": {
      "command": "npx",
      "args": ["exvisit-mcp"]
    }
  }
}
```

### Cursor

Create `.cursor/mcp.json` in your project root:

```json
{
  "mcpServers": {
    "exvisit": {
      "command": "C:\\path\\to\\exvisit-mcp-windows-amd64.exe",
      "args": []
    }
  }
}
```

### VS Code (GitHub Copilot)

In `.vscode/mcp.json` or user `settings.json`:

```json
{
  "mcp": {
    "servers": {
      "exvisit": {
        "type": "stdio",
        "command": "C:\\path\\to\\exvisit-mcp-windows-amd64.exe",
        "args": []
      }
    }
  }
}
```

### Verify the server is running

```bash
# Linux / macOS
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | ./exvisit-mcp-linux-amd64

# Windows PowerShell
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | .\exvisit-mcp-windows-amd64.exe
```

You should see a JSON response listing all available tools.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `EXVISIT_CMD` | � | Override the `exv` binary path |
| `EXVISIT_PYTHON` | `python` | Override the Python interpreter path |

---

## MCP tools reference

| Tool | Description |
|---|---|
| `exv_init` | Generate a `.exv` structural map from a repository root |
| `exv_blast` | Rank files most relevant to an issue / error text |
| `exv_query` | Extract a topological slice around a named node |
| `exv_locate` | Score nodes with confidence margin for anchoring |
| `exv_expand` | Weighted neighborhood expansion from an anchor |
| `exv_anchor` | Resolve a stack trace to ground-zero anchor nodes |
| `exv_deps` | Outbound dependency list for a node |
| `exv_callers` | Inbound caller list for a node |
| `exv_verify` | Check structural consistency of `.exv` vs repo |

---

## Core CLI commands

```bash
exv init --repo ./my-project          # scaffold .exv + .meta.json
exv blast my-project.exv \
  --issue "bug description"           # get ranked file bundle
exv locate my-project.exv \
  --issue "bug description"           # top-K anchors with confidence
exv query my-project.exv \
  --target AuthModels                 # topological slice
exv anchor my-project.exv \
  --stacktrace "Traceback..."         # resolve stack trace to anchors
exv verify my-project.exv             # validate syntax + edge consistency
```

All commands support `--help`. `exv` and `exvisit` are identical entry points.

---

## The `.exv` format

`.exv` is a plain-text graph format � human-readable and version-control-friendly.

```
# my-project.exv
namespace auth
  UserModel
  AuthForms [test-of: UserModel]

namespace payments
  PaymentModel
  StripeClient
  WebhookView

WebhookView -> PaymentModel [import]
PaymentModel -> UserModel [inherit]
StripeClient -> PaymentModel [call]
```

**Edge types:** `import`, `inherit`, `config-ref`, `test-of`, `call`
**Node types:** `code`, `test`, `migration`, `registry`

The format has a [formal grammar spec](spec/exvisit-dsl-v0.4-draft.md) and a [Rust PEG parser](rust/crates/exvisit-core/src/exvisit.pest) in progress.

---

## Architecture

```
.exv file (text)
      �
      ?
  exvisit.parser        � hand-rolled recursive descent
      �
      ?
  exvisit.ast           � Namespace / Node / Edge / exvisitDoc
      �
      ?
  exvisit.scaffold      � Python repo ? .exv + .meta.json (call edges, migrations)
      � (precompute)
      ?
  graph_meta.NodeMeta   � per-node: fqn, symbols, loc, pagerank, cluster
      �
      ?
  scoring_v2            � log-linear ranker (18+ signals)
    signals: BM25, trace, symbol match, PageRank (capped),
             cluster IDF, domain bias, dunder match, error codes,
             management commands, call_target, inherit_target, term_idf,
             registry penalty, neighbor adjacency
      �
      ?
  blast.build_blast_bundle  � multi-phase selection:
    anchor ? precision guards ? neighbor ? sibling
    ? __init__.py injection ? parent-package expansion ? graph-fill
      �
      ?
  Compact bundle (=6�10 files, ~2K tokens) ? agent
```

---

## Why not just use RAG?

RAG embeds semantic meaning at the function/chunk level � excellent for "which chunk of docs answers this question." It struggles with:

- **Cross-file dependency chains** � knowing that `PaymentView` calls `StripeClient.charge()` defined in `payments/integrations.py` requires graph traversal, not nearest-neighbor lookup.
- **Structural inheritance** � `AdminMixin` in `auth/mixins.py` affects `OrderAdmin` in `shop/admin.py` through a 3-hop chain. No vector similarity captures that.
- **Token budget discipline** � RAG retrievers return fixed-K chunks regardless of relevance margin. ExVisit's confidence-adaptive selection returns 1 file when highly certain, up to 10 when uncertain.

---

## Project status

| Component | Status |
|---|---|
| Core parser / AST | Stable |
| Python scaffolder (`exv init`) | Stable |
| Blast v2 ranker | **Stable � 68.8% oracle@1 (no LLM), SWE-bench Lite** |
| Locate v2 (multi-signal anchor) | Stable � 70% oracle@1 at HIGH confidence (agentic) |
| MCP server binary (`exvisit-mcp`) | **Released � v0.5.0** |
| CRDT merge layer | Beta |
| Rust PEG parser (`exvisit-core`) | Early alpha |
| VS Code extension | Planned |

---

## Contributing

```bash
git clone https://github.com/SaiAvinashPatoju/exvisit
cd exvisit

# Windows
python -m venv .venv && .venv\Scripts\activate
# Linux / macOS
python -m venv .venv && source .venv/bin/activate

pip install -e ".[dev]"
pytest tests/ -q
```

Run the navigation benchmark (requires HuggingFace SWE-bench Lite cached locally):

```bash
python -m bench.run_bench --mode nav-only --limit 32
```

---

## License
Apache-2.0
