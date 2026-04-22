### PROJECT exvisit: Master System Architecture Document

This is the definitive, macro-level system architecture for the `.exv` open-source standard. This document serves as the foundational "Context Zero" payload to feed into your elite reasoning model (e.g., Opus 4.7) before initiating any phase-wise agentic development.

It defines the strict separation of concerns across the memory, translation, execution, and presentation layers.

---

### 1. The Core Paradigm: The Spatial Source of Truth

Project exvisit is not a visual drawing application; it is a structural context compiler. The entire system revolves around the `.exv` Domain-Specific Language (DSL).

* **The Law:** The raw text file is the absolute source of truth. The 3D canvas (ZUI) and the in-memory graphs are strictly transient views or representations of this file.
* **Token Annihilation:** The architecture relies on tabular node arrays and isolated 2P-Set Adjacency Matrices to mathematically guarantee a 50%+ reduction in LLM context token consumption compared to JSON.

---

### 2. The Data & Memory Layer (The CRDT Engine)

To enable lock-free, simultaneous multi-agent and human editing, the backend memory cannot rely on standard Abstract Syntax Trees (ASTs). It must be modeled as a Delta-based Conflict-Free Replicated Data Type (CRDT).

* **In-Memory Graph Construction:**
  * **Namespaces (Z-Layers):** Stored as an OR-Set (Observed-Remove Set) to track hierarchical depth (`@L1`, `@L2`).
  * **Tabular Nodes:** Stored as an LWW-Map (Last-Write-Wins Map). The node ID is the key; the spatial geometry `[x, y, w, h]` and hardware config parameters are the values.
  * **Topological Edges:** Stored as a 2P-Set (Two-Phase Set) to track data flow (`->`, `~>`) without strict order dependency.
* **Spatial Indexing:** The engine utilizes a high-performance in-memory R-Tree (`rtree.rs`). It maps the logical Z-layer bounds to absolute world coordinates $(u = xz, v = \log(z))$ for instantaneous $O(1)$ spatial queries.
* **The Sync Daemon:** A background loop that continuously calculates CRDT deltas between incoming canvas actions and agent text mutations, flushing the resolved state back to the `.exv` file deterministically without destroying human-written annotations.

---

### 3. The Translation Layer (Parsing & Lexing)

This layer bridges the raw text and the CRDT engine.

* **The PEG Parser:** A highly optimized Parsing Expression Grammar built in Rust (via `pest` or `nom`). It lexes the `.exv` file into four distinct passes:
    1. Extracts `@L` namespace bounds.
    2. Parses tabular array schemas and instantiates LWW-Map nodes.
    3. Parses the inline state machine syntax (`{idle -> active}`).
    4. Lexes the bottom-level Adjacency Matrix into the 2P-Set.
* **Codebase Anchoring:** The parser explicitly tracks the `src_path` column in the tabular arrays, mapping the theoretical node to a physical file glob (e.g., `src/backend/*.go`).

---

### 4. The Agentic Interface Layer (API & Daemons)

This layer provides headless access to the exvisit engine for AI agents and IDEs, ensuring agents never suffer context rot by loading the entire architecture.

* **`exvisit-query` (The Extraction Protocol):** A CLI utility functioning like `ripgrep` for ASTs. Agents pass parameters like `--target "Backend.OrderService" --preserve-bounds "Z0"`. The engine queries the R-Tree and 2P-Set, returning a pristine, microscopic subset of the `.exv` text containing only local geometry and immediate topological neighbors.
* **`exvisit-mcp` (The Architect Server):** A Model Context Protocol (MCP) JSON-RPC server. When a human manually alters the canvas (e.g., splitting a monolith node), the server calculates the architectural delta and exposes a strict "Phase-Refactor Plan." Executing agents read this plan to perform physical file movements and import refactoring, preventing LLM architectural hallucinations.

---

### 5. The Developer Experience (Execution & Presentation)

This layer integrates the standard into the physical reality of software engineering.

* **`exvisit-lsp` (Spatial Constraint Linting):** A Language Server Protocol implementation built with `tower-lsp`. It runs locally in the developer's IDE (VS Code, Zed). It cross-references local code imports against the exvisit CRDT Adjacency Matrix. If a developer imports a package that violates the defined spatial topology, the LSP fires a real-time `textDocument/publishDiagnostics` error, enforcing the blueprint as executable law.
* **The Glass Viewport (ZUI Canvas):** A frontend stripped of all business logic and DOM-state bloat. Compiled in Rust to WebAssembly (WASM), it utilizes `wgpu` and WebGL. It receives frustum-culled arrays of bounding boxes from the engine's R-Tree and renders the infinite grid and nodes purely via screen-space fragment shaders. It captures mouse events (pan, zoom, drag) and pipes them directly back to the CRDT Sync Daemon.

***

### ⚙️ PHASE 1: The Lexical Core & AST Generation

**Context Rule for Opus:** *Focus purely on text parsing and Rust data structures. Do not write any UI or networking code.*

> **Prompt to Opus:**
> "We are executing Phase 1 of Project exvisit. Read the Master System Architecture document provided. Your task is to build the Lexical Core in Rust.
>
> 1. Use `pest` or `nom` to write the strict Parsing Expression Grammar for the `.exv` format.
> 2. Implement the Rust Abstract Syntax Tree (AST) structs representing the three core components: Z-Layers (Namespaces), Tabular Nodes (with `[x,y,w,h]` and `src_path`), and the Adjacency Matrix (Edges using `->`, `~>`).
> 3. Write a high-performance lexer that takes a raw `.exv` string, strips all whitespace/semantic annotations, and successfully populates the AST structs.
> 4. Write rigorous unit tests covering syntax failures and edge cases.
>
> Output the exact `Cargo.toml` dependencies and the complete `parser.rs` and `ast.rs` implementations."

---

### ⚙️ PHASE 2: The CRDT Memory Engine & Spatial Index

**Context Rule for Opus:** *Assume Phase 1 is perfect. Focus purely on lock-free concurrency, distributed data types, and computational geometry.*

> **Prompt to Opus:**
> "We are executing Phase 2 of Project exvisit. Using the AST structs from Phase 1, architect the in-memory graph database.
>
> 1. Implement a Delta-based CRDT state machine.
> 2. Map the AST structs to CRDT primitives: Namespaces must be OR-Sets, Tabular Nodes must be LWW-Maps (Last-Write-Wins), and Topological Edges must be 2P-Sets.
> 3. Integrate an R-Tree (`rtree.rs` or similar) to manage the spatial coordinates. Write the logic that translates logical Z-layer bounds into absolute world coordinates $(u = xz, v = \log(z))$.
> 4. Write the Sync Daemon logic: a function that accepts concurrent topological mutations (e.g., updating a node's `[x,y]` bounds while simultaneously adding an edge) and merges them cleanly without file locking.
>
> Output the `crdt_graph.rs` and `spatial_index.rs` implementations, prioritizing memory safety and zero-copy references."

---

### ⚙️ PHASE 3: The Agentic Daemons (`exvisit-query` & MCP)

**Context Rule for Opus:** *Assume the CRDT engine handles all data safety. Focus entirely on high-speed headless extraction and JSON-RPC APIs.*

> **Prompt to Opus:**
> "We are executing Phase 3 of Project exvisit. We are building the headless agent interface layer.
>
> 1. Build the `exvisit-query` CLI module. It must take parameters like `--target "Backend.OrderService"` and execute an $O(1)$ lookup against the R-Tree and 2P-Set. It must output a microscopic, perfectly formatted `.exv` text slice containing *only* the parent macro-bounds and the immediate topological neighbors.
> 2. Build the `exvisit-mcp` (Model Context Protocol) server. Implement a JSON-RPC endpoint that accepts ZUI canvas delta events (e.g., a node split) and outputs a deterministic "Phase-Refactor Plan" instructing an external AI agent on which physical files to move and import paths to update.
>
> Output the CLI argument parsing logic (`clap` crate) and the core MCP server handler logic."

---

### ⚙️ PHASE 4: The Spatial LSP (`exvisit-lsp`)

**Context Rule for Opus:** *Focus purely on the Language Server Protocol boilerplate and local AST interception.*

> **Prompt to Opus:**
> "We are executing Phase 4 of Project exvisit. We are turning the blueprint into executable law.
>
> 1. Use the `tower-lsp` crate to stand up a Language Server.
> 2. Implement the `textDocument/didChange` handler. Write a mock local AST interceptor that reads local file imports (e.g., simulating parsing a `.go` or `.py` file's imports).
> 3. Write the strict validation logic: Cross-reference the local import path against the physical `src_path` mapped in the CRDT Adjacency Matrix.
> 4. If the topological edge does not exist in the `.exv` file, fire a `textDocument/publishDiagnostics` payload throwing a 'Spatial Constraint Violation' error.
>
> Output the complete `tower-lsp` server implementation and the diagnostic calculation logic."

---

### ⚙️ PHASE 5: The Glass Viewport (WGPU)

**Context Rule for Opus:** *Forget all text parsing, LSPs, and CRDTs. Focus strictly on GPU rendering, WASM, and linear algebra.*

> **Prompt to Opus:**
> "We are executing the final phase, Phase 5, of Project exvisit. We are building the "dumb canvas" visualizer to compile to WebAssembly.
>
> 1. Use `wgpu` to initialize a graphics pipeline. Completely avoid DOM elements, CSS, or heavy UI frameworks.
> 2. Write a screen-space fragment shader (WGSL) that natively renders an infinite grid and draws rectangles based on an array of bounding boxes.
> 3. Implement a camera system that handles infinite 2D panning and zooming, calculating the view frustum matrix.
> 4. Write the Wasm interop layer that receives arrays of `[x, y, w, h]` floats (simulating the frustum-culled payload from the backend Sync Daemon) and pipes them to the GPU buffer.
>
> Output the Rust `wgpu` setup code, the camera matrix math, and the raw WGSL shader code."

