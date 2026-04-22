# exvisit Spec Sheet — Discovered Issues & Improvements

Living document. Each issue has an ID, severity, status, and remediation.

Status legend: `open` | `in-progress` | `fixed-v0.x` | `deferred`

---

## SPEC-001 — exvisit drift vs real imports  `severity: critical`  `status: fixed-v0.3`

**Problem.** Hand-authored `.exv` files can silently disagree with the real codebase.

**Fix shipped.** `exvisit verify <file> --repo <path>` (`exvisit/verify.py`): parses every `src_path` file, expands `from pkg import name` into `pkg.name`, maps to exvisit nodes via bidirectional `/`-boundary suffix match, reports `missing`/`ghost`/`unresolved`. Informational `~>` edges are exempt.

**Verified against MySlicerApp.** Caught 6 legitimate drift edges + exposed 2 tool bugs (BOM handling, loose suffix match) before reaching 0 diagnostics in v0.3.

---

## SPEC-002 — `~>` semantics under-specified  `severity: high`  `status: fixed-v0.1`

**Problem.** Agents (and the future LSP) cannot distinguish hard imports from runtime callback wiring. LSP enforcement would false-positive on Qt signal/slot connections.

**Fix.** `spec/exvisit-dsl-v0.1.md` now codifies: `->` = static call/import dependency (LSP-enforced); `~>` = runtime wire (event, callback, signal — informational only). `exvisit verify` will only enforce `->`.

---

## SPEC-003 — No auto-scaffolder  `severity: high`  `status: fixed-v0.3`

**Problem.** Authoring the initial exvisit from scratch is tedious and error-prone.

**Fix shipped.** `exvisit init --repo <path> --out <file>` (`exvisit/scaffold.py`): groups files by package dir → `@L1` namespaces; grid-layout nodes; walks real imports to seed `->` edges. On MySlicerApp produces a **verify-clean** exvisit in one command (36 nodes, 38 edges, 3.5 KB).

---

## SPEC-004 — Bare-name edge ambiguity  `severity: medium`  `status: open`

**Problem.** `find_node("Scene")` returns the first match. If two namespaces both declare a `Scene`, results are non-deterministic.

**Remediation.** 
- Require dotted FQN when the bare name is ambiguous.
- Parser warns at parse time with line numbers of each collision.
- `exvisit lint` command.

---

## SPEC-005 — Query direction default confusing  `severity: low`  `status: fixed-v0.3`

**Fix shipped.** `exvisit deps <target>` (outbound) and `exvisit callers <target>` (inbound) subcommands; the older `exvisit query` retains full power.

---

## SPEC-006 — No coordinate conflict detection  `severity: medium`  `status: open`

**Problem.** Two nodes can overlap in the 2D layout. No warning surfaces.

**Remediation.** On parse, R-Tree `query_rect` against each inserted rect; if non-empty, emit `ETriangle` warning (nodes X and Y overlap at layer Ln).

---

## SPEC-007 — State machines never queried  `severity: low`  `status: open`

**Problem.** The `{idle -> loading -> ready}` inline state annotations are parsed but never surfaced by the query engine.

**Remediation.** `exvisit state <target>` prints the state machine; `exvisit-lsp` validates no code-side enum diverges from exvisit.

---

## SPEC-008 — CRDT merge not network-tested  `severity: medium`  `status: open`

**Problem.** `exvisitGraph.merge` is commutative in unit tests but never stress-tested under real concurrent agent edits. No fuzz harness.

**Remediation.** Property-based test (`hypothesis`) generating random edit sequences → verify `merge(merge(a,b),c) == merge(a,merge(b,c))` and idempotency.

---

## SPEC-009 — No token-budget-aware query  `severity: medium`  `status: open`

**Problem.** `--neighbors 3` can balloon beyond the caller's token budget with no visibility.

**Remediation.** `--max-tokens N` flag. Engine expands hops in BFS order until budget is hit, then stops cleanly. Returns `# truncated at 3/5 hops for budget` comment.

---

## SPEC-010 — Rust crates missing  `severity: high (for vision)`  `status: scaffolded-v0.3`

**Scaffolded in `rust/`.** Workspace `Cargo.toml` + five crates with module structure, `pest` grammar (`exvisit.pest`), AST types, and binary entry points:
- `exvisit-core` — parser + AST + CRDT + spatial (Phases 1-2).
- `exvisit-query` — CLI (Phase 3).
- `exvisit-mcp` — JSON-RPC server (Phase 3).
- `exvisit-lsp` — tower-lsp server (Phase 4).
- `exvisit-viewport` — wgpu + WASM, including `grid.wgsl` (Phase 5).

Source-level ports of the Python reference pending Rust toolchain install. Tests (`tests/parser_tests.rs`) should mirror `tests/test_exvisit.py` 1:1.

---

## SPEC-011 — Comments lost on CRDT roundtrip  `severity: low`  `status: open`

**Problem.** `# comments` are stripped by the lexer; they don't survive `parse -> CRDT -> serialize`. Humans lose annotations.

**Remediation.** Attach trivia (comments, blank lines) to their lexically-following token. Serializer re-emits them. This is how `rustfmt` preserves comments.

---

## SPEC-012 — No visualization of the exvisit itself  `severity: low`  `status: open`

**Problem.** An agent can query a slice, but a human still wants to *see* the layout. Phase 5 (WGPU) ships that; meanwhile a quick SVG export would help authoring feedback.

**Remediation.** `exvisit render <file> --out map.svg` — simple rect-and-arrow dump.

---

## Close-out criteria for v0.3

Ship when: SPEC-001, SPEC-003, SPEC-004, SPEC-005, SPEC-006 are `fixed`. That gives us authoring-confidence parity with JSON + hand-verified graphs, plus the ergonomic wins from the experience report.

**v0.3 state (this session):** fixed SPEC-001, SPEC-002, SPEC-003, SPEC-005, SPEC-010 (scaffold). Open: SPEC-004 (name collisions), SPEC-006 (layout overlap), SPEC-007 (state queries), SPEC-008 (CRDT fuzz), SPEC-009 (token budget), SPEC-011 (comment preservation), SPEC-012 (SVG render). None block the core value proposition; all are incremental polish.

