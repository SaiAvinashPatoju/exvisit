# exvisit-mcp — Technical Research and Execution Plan

**Goal:** Make `.exv` maps natively available to Claude Desktop, Cursor, and any MCP-compatible AI client via a Model Context Protocol server. A user adds 5 lines to their `claude_desktop_config.json` and Claude can navigate their repository's structural map without any extra tool calls.

---

## 1. What we are building

An MCP server that:
1. Loads a `.exv` graph and its `.meta.json` sidecar at startup.
2. Exposes the graph as **MCP resources** (navigable nodes/edges readable by the client).
3. Exposes `blast`, `query`, `locate`, `expand`, and `verify` as **MCP tools** (callable by the AI).
4. Streams results as structured JSON + rendered Markdown.

The end state:

```json
// claude_desktop_config.json
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

Claude sees the repository structure as a first-class navigable resource, not a black box requiring repeated `read_file` calls.

---

## 2. MCP protocol primer (what we need)

The MCP specification defines three primitives:

| Primitive | Description | exvisit use |
|---|---|---|
| **Resources** | Static or dynamic data the client can `read` | Each `.exv` node as a resource URI |
| **Tools** | Functions the AI can `call` with arguments | `blast`, `query`, `locate`, `expand`, `verify` |
| **Prompts** | Parameterized prompt templates | Optional: `exvisit_explain_graph` prompt |

We will implement **Resources + Tools**. Prompts are optional and deferred.

The MCP transport for stdio-based servers (required for Claude Desktop) is JSON-RPC 2.0 over stdin/stdout. The Python `mcp` SDK handles the framing.

---

## 3. Architecture decision: Python vs. Rust

### Option A: Python MCP server (recommended for v0.1)

**Implementation:** `exvisit/mcp_server.py` + `exv mcp` CLI subcommand.

**Pros:**
- Reuses the existing Python blast/query/scoring stack immediately.
- Faster time to working demo (1–2 days of implementation).
- `mcp` Python SDK is stable and well-documented.
- Zero build step for users — `pip install "exvisit[mcp]"` is sufficient.

**Cons:**
- Python startup latency (~200ms) per server launch. Acceptable for Claude Desktop which keeps the server alive.
- Not the long-term production implementation.

### Option B: Rust MCP server (target for v0.3+)

**Implementation:** `rust/crates/exvisit-mcp/src/main.rs` (stub already exists).

**Pros:**
- Sub-10ms startup. Zero GIL. Suitable for high-frequency Cursor integration.
- Single static binary, no Python runtime dependency.
- Uses `exvisit-core` Rust crate for parsing and graph ops.

**Cons:**
- Requires `exvisit-core` to reach feature parity with Python scoring (currently at ~40% coverage).
- Rust MCP SDK (`rmcp`) is newer and less battle-tested.

**Decision:** Ship Python v0.1 immediately. Track Rust v0.3 milestone.

---

## 4. Python MCP server — detailed design

### 4.1 Entry point

```bash
# Added to pyproject.toml scripts (already done)
exv mcp --exv ./my-project.exv [--port 0] [--log-level info]
```

Internally: `exvisit/cli.py::cmd_mcp()` → `exvisit/mcp_server.py::run()`.

### 4.2 Resource tree

The MCP server exposes the `.exv` graph as a resource tree rooted at `exvisit://graph`.

```
exvisit://graph                    — graph summary (node count, edge count, namespaces)
exvisit://graph/nodes              — list of all nodes
exvisit://graph/nodes/{node_id}    — individual node with metadata
exvisit://graph/edges              — list of all edges
exvisit://graph/namespaces/{ns}    — namespace subtree
```

Each resource returns structured JSON. The `exvisit://graph/nodes/{node_id}` resource includes:

```json
{
  "id": "PaymentModels",
  "fqn": "payments.models",
  "kind": "code",
  "symbols": ["PaymentRecord", "StripeWebhook", "charge_card"],
  "loc": 342,
  "pagerank": 0.043,
  "cluster": "payments",
  "edges_out": [
    { "target": "AuthModels", "type": "inherit" },
    { "target": "PaymentConfig", "type": "config-ref" }
  ],
  "edges_in": [
    { "source": "PaymentViews", "type": "import" }
  ]
}
```

### 4.3 Tool definitions

```python
# Tool: exvisit_blast
{
  "name": "exvisit_blast",
  "description": "Select the optimal file bundle for an issue or query using the .exv structural map. Returns a ranked list of files the agent should read to resolve the issue. Use this instead of searching or reading random files.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "issue_text": {
        "type": "string",
        "description": "The bug report, feature request, or question to resolve."
      },
      "max_files": {
        "type": "integer",
        "description": "Maximum number of files to return. Default 5. Use 3 for focused issues, 6 for complex multi-file changes.",
        "default": 5
      }
    },
    "required": ["issue_text"]
  }
}
```

```python
# Tool: exvisit_query
{
  "name": "exvisit_query",
  "description": "Navigate the .exv graph from a named node. Returns neighbors, edge types, and structural metadata. Use when you know the starting file/class and want to find related files.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "node": { "type": "string", "description": "Node name (CamelCase, e.g. PaymentModels)" },
      "depth": { "type": "integer", "default": 1, "description": "Traversal depth (1=direct neighbors, 2=2-hop)" }
    },
    "required": ["node"]
  }
}
```

```python
# Tool: exvisit_locate
{
  "name": "exvisit_locate",
  "description": "Find which .exv node maps to a given file path or symbol name.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "path": { "type": "string", "description": "File path (relative) or symbol name" }
    },
    "required": ["path"]
  }
}
```

```python
# Tool: exvisit_expand
{
  "name": "exvisit_expand",
  "description": "Expand a node's cluster — return all sibling nodes in the same cluster. Useful when the issue spans multiple files in the same module.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "node": { "type": "string" }
    },
    "required": ["node"]
  }
}
```

```python
# Tool: exvisit_verify
{
  "name": "exvisit_verify",
  "description": "Verify structural consistency of the .exv graph. Returns any orphan nodes, dangling edges, or schema violations.",
  "inputSchema": { "type": "object", "properties": {} }
}
```

### 4.4 Server implementation skeleton

```python
# exvisit/mcp_server.py
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp import types
from .blast import build_blast_bundle, render_blast_markdown
from .query import query as graph_query
from .graph_meta import load_for as load_meta

async def run(exv_path: str) -> None:
    from . import parse
    from pathlib import Path

    doc = parse(Path(exv_path).read_text(encoding="utf-8"))
    meta = load_meta(Path(exv_path))

    server = Server("exvisit")

    @server.list_resources()
    async def list_resources():
        return [
            types.Resource(
                uri="exvisit://graph",
                name="ExVisit Graph",
                description=f"{len(doc.all_nodes())} nodes, {len(doc.edges)} edges",
                mimeType="application/json",
            )
        ]

    @server.read_resource()
    async def read_resource(uri: str):
        # Return graph summary or node detail based on URI
        ...

    @server.list_tools()
    async def list_tools():
        return [BLAST_TOOL, QUERY_TOOL, LOCATE_TOOL, EXPAND_TOOL, VERIFY_TOOL]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        if name == "exvisit_blast":
            bundle = build_blast_bundle(doc, arguments["issue_text"], exvisit_path=exv_path)
            return [types.TextContent(type="text", text=render_blast_markdown(bundle))]
        elif name == "exvisit_query":
            result = graph_query(doc, arguments["node"], depth=arguments.get("depth", 1))
            return [types.TextContent(type="text", text=str(result))]
        # ... etc.

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         InitializationOptions(server_name="exvisit", server_version="0.4.0",
                                               capabilities=server.get_capabilities(
                                                   notification_options=NotificationOptions(),
                                                   experimental_capabilities={})))
```

### 4.5 CLI integration

Add to `exvisit/cli.py`:

```python
def cmd_mcp(args):
    """Run the ExVisit MCP server (stdio transport)."""
    import asyncio
    from .mcp_server import run
    asyncio.run(run(args.exv))

# In build_parser():
p_mcp = sub.add_parser("mcp", help="Run the ExVisit MCP server")
p_mcp.add_argument("--exv", required=True, help="Path to .exv graph file")
p_mcp.set_defaults(func=cmd_mcp)
```

---

## 5. Cursor integration

Cursor supports MCP servers via its settings JSON. The integration is identical to Claude Desktop:

```json
// cursor settings.json / mcp section
{
  "mcp": {
    "servers": {
      "exvisit": {
        "command": "exv",
        "args": ["mcp", "--exv", "${workspaceFolder}/my-project.exv"]
      }
    }
  }
}
```

Cursor's `${workspaceFolder}` macro resolves to the open project root, making this configuration portable.

---

## 6. Auto-discovery (v0.2 feature)

Instead of requiring `--exv <path>`, the server can auto-discover `.exv` files from the current working directory:

```python
# Auto-discovery priority:
# 1. Explicit --exv argument
# 2. *.exv file in cwd (if exactly one exists)
# 3. *.exv file in parent directories (walk up)
# 4. Error with helpful message
```

This means users can simply run `exv mcp` from within their project directory without specifying a path.

---

## 7. Rust MCP server — future state

The `rust/crates/exvisit-mcp/src/main.rs` stub will implement the same protocol surface using:

- [`rmcp`](https://github.com/modelcontextprotocol/rust-sdk) — the official Rust MCP SDK.
- `exvisit-core` — the Rust PEG parser and graph engine.
- `tokio` — async runtime.

Target binary size: < 5 MB. Target startup: < 20 ms.

The Rust MCP server is the target for Cursor's "always-on" integration mode where the server is kept running across multiple sessions.

---

## 8. Implementation milestones

| Milestone | Target | Deliverable |
|---|---|---|
| v0.1 Python MCP | Week 1 | `exv mcp` working with Claude Desktop, all 5 tools, resource list |
| v0.1 Cursor integration | Week 1 | `cursor settings.json` snippet tested and documented |
| v0.2 Auto-discovery | Week 2 | `exv mcp` auto-finds `.exv` in cwd, portable config |
| v0.2 Resource tree | Week 2 | Full `exvisit://graph/nodes/{id}` resource navigation |
| v0.3 Rust MVP | Month 2 | Rust binary with `exvisit_blast` and `exvisit_query` tools |
| v0.3 Packaging | Month 2 | `pip install "exvisit[mcp]"` includes pre-built Rust binary for common platforms |
| v1.0 | TBD | Rust server full feature parity with Python, auto-update `.exv` on file save |

---

## 9. Research questions to resolve

1. **MCP streaming:** Does the `mcp` Python SDK support streaming responses for large blast bundles? The `blast` output for a 6-file bundle can be 6K tokens — should it stream or return at once?
2. **`.exv` hot reload:** If the user edits their codebase and `exv init` is re-run, should the MCP server detect the `.meta.json` modification and reload? Requires a file-watch loop.
3. **Multi-project support:** A single Claude Desktop session may have multiple open projects. Should one MCP server instance handle multiple `.exv` files, or should users launch one instance per project? The latter is simpler; the former requires a project registry.
4. **Authentication:** MCP stdio transport has no auth layer. The server inherits the parent process's environment. This is fine for local development; remote deployment requires a different transport (SSE/HTTP with auth headers).
5. **Schema evolution:** The `.exv` format is pre-1.0. What is the backward-compatibility contract for the MCP resource schema? Define a `version` field in the resource metadata to allow clients to adapt.

---

## 10. Security considerations

- The MCP server runs with the user's local file-system permissions. `exvisit_blast` and `exvisit_query` are read-only operations and present no escalation risk.
- `exvisit_verify` reads `.exv` and source files for cross-referencing — also read-only.
- The server must not execute arbitrary code or shell commands. The `blast` and `query` implementations are pure in-process Python/Rust — no subprocess calls in the MCP server path.
- Input validation: `node` and `issue_text` arguments should be length-capped and sanitized before passing to the graph engine to prevent degenerate inputs from causing excessive CPU use.

---

## 11. Testing strategy

```
tests/
  test_mcp_server.py      — unit tests for tool handlers (mock stdio transport)
  test_mcp_integration.py — integration test using mcp.client.stdio (requires mcp[cli])
```

The integration test spins up `exv mcp` as a subprocess, connects via the MCP client, calls `exvisit_blast` with a test issue, and asserts that the returned bundle contains the expected files.
