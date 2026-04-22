# `.exv` DSL — v0.4 (DRAFT)

> Backward-compatible superset of v0.1. All v0.3 files parse unchanged.
> New tokens are additive. Unknown tags MUST be preserved round-trip.

## New capabilities (summary)

1. **Typed edges** replace binary `-> / ~>`  (legacy forms still accepted, aliased).
2. **`lines=`** per-node AST range locator.
3. **`@tag=value`** edge / node / namespace annotations (first-class concurrency).
4. **`@L3` execution-constraint namespaces** — thread/mutex/reentrancy as data.
5. **Sidecar `.exv.metrics`** — live telemetry, never hand-edited.
6. **Stacktrace anchor protocol** — `exvisit anchor` CLI contract.

## Typed edges

```
Scissors     -call->         MeshOps
MeshOps      -writes_state-> Scene       @thread=worker
PyVistaW     -reads_state->  Scene       @thread=main
Layout       -renders->      PyVistaW
MainWindow   -imports->      Scene
Scene        -emits->        SceneChanged
ObjectPanel  -listens->      SceneChanged
TestScissors -tests->        Scissors
MeshOps      -owns->         WorkerPool
```

Aliases (v0.3 compat):
- `->`    ≡ `-call->` or `-imports->` depending on context (verify picks `imports` when src_path edges cross files)
- `~>`    ≡ `-emits->`

Verified edge kinds (LSP enforces): `call`, `import`, `reads_state`, `writes_state`, `renders`, `tests`.
Informational only: `emits`, `listens`, `owns`.

## Node + edge annotations

```
Scene [1,1,12,6] scene.py lines=234..980 @thread=main @reentrant=false
MeshOps -writes_state-> Scene @thread=worker @via=QueuedConnection
```

Grammar extension:
```
annotation := '@' IDENT '=' (IDENT | NUMBER | STRING)
```

## `@L3` execution-constraint blocks

```
@L3 Scene:runtime "maxmorph/core/scene.py" {
  @thread     = main
  @reentrant  = false
  @mutex      = _objects_lock
  @invariant  "objects map stable between begin_batch/end_batch"
  @owns       = objects, _by_uid
}
```

`@L3 <Node>:runtime` blocks **attach metadata to an existing node** rather than
declaring a new one. Their sole purpose is execution context.

## `lines=` locator

```
Scissors [1,6,10,4] scissors_tool.py lines=88..312 {idle -> drawing -> cut}
```

Enables `exvisit snippet <fqn>` to emit exactly those lines.

## LSP diagnostics v0.4 adds

| Code       | Severity | Condition |
|------------|----------|-----------|
| `exvisit001` | error    | `-imports->` declared but no matching import in source (v0.3 carryover) |
| `exvisit002` | error    | Real import exists with no `-imports->` edge (v0.3 carryover) |
| `exvisit003` | error    | `reads_state @thread=X` and `writes_state @thread=Y` target the same node without a documented synchronization edge |
| `exvisit004` | warn     | `@reentrant=false` node is in a cycle of `-call->` edges |
| `exvisit005` | warn     | Edge declares `@thread=X` but source file has no matching `QThread`/`threading`/`tokio::spawn` primitive |
| `exvisit006` | info     | Node has `emits` but zero `listens` — orphaned signal |

## Sidecar metrics format

File: `<name>.exv.metrics` — always auto-generated, `.gitignore`d by default.

```
# schema 1
# generated_at 2026-04-22T18:44Z  source otel
<fqn>  calls=<n>  p50=<ms>  p95=<ms>  errors=<n>  thread=<tag>
```

Special thread tag `mixed!` = multiple threads observed on a node declared `@thread=single`.
This is a **runtime-detected spec violation**; `exvisit verify --metrics` promotes it to an error.

## Error-anchor protocol

```
exvisit anchor <exvisit-file> [--stacktrace <file>|stdin]
```

Input: any text containing lines matching `<path>:<line>` triples.
Output: ranked list of `(role, fqn, file, line)` where role ∈ `ground_zero | direct_import | direct_dependent | structural_neighbor`.
Also emits diagnostics if the anchor chain violates any exvisit00x rule.

## Non-goals for v0.4

- No SMT-backed invariant proving.
- No mutex-ordering analysis (defer to v0.5 + Rust LLVM pass).
- No replacement of `verify.py` — v0.4 extends it, not supplants it.

## Migration path

1. `exvisit upgrade <file>` — reads v0.3, rewrites `->` as `-imports->` or `-call->`, `~>` as `-emits->`, preserves comments.
2. Hand-add `@thread=` and `@L3` blocks where concurrency matters.
3. `exvisit verify --strict` accepts only v0.4 syntax; `exvisit verify` accepts both.

## Status

DRAFT — targeting promotion once these ship:

- [ ] Python parser: tokenize `-ident->`, `lines=`, `@kv`, `@L3 ... :runtime`
- [ ] `verify.py`: exvisit003 / exvisit004 / exvisit005 / exvisit006
- [ ] `exvisit upgrade` codemod
- [ ] `exvisit anchor` CLI
- [ ] Sidecar metrics reader
- [ ] Rust `exvisit-core` ports each of the above

Once checked, promote this file to `spec/exvisit-dsl-v0.4.md` and retire v0.1.

