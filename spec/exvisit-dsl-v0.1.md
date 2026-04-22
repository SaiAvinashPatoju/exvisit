# `.exv` DSL — v0.1

The `.exv` file is the **spatial source of truth**. Text is canonical; graphs are transient.

## Design axioms

1. **Token annihilation** — no JSON quoting, no repeated keys, no punctuation tax.
2. **Positional tabular rows** — one node per line; columns are fixed by header position.
3. **Adjacency matrix separate** — edges never re-specify endpoints as full paths.
4. **Hierarchical nesting via `@Ln`** — depth carries semantic weight.

## Grammar (EBNF-ish)

```
file        := namespace+
namespace   := '@L' INT IDENT bounds ( src_glob )? '{' body '}'
body        := ( namespace | node_row | edges_block | comment )*
node_row    := IDENT bounds ( src_path )? ( state_machine )?    ; whitespace-separated
bounds      := '[' INT ',' INT ',' INT ',' INT ']'              ; x,y,w,h
src_path    := '"' PATH_GLOB '"' | BAREPATH                     ; optional
src_glob    := '"' PATH_GLOB '"'
state_machine := '{' IDENT ( ARROW IDENT )+ '}'
edges_block := '===' 'edges' '===' edge+
edge        := IDENT ARROW IDENT                                ; may use dotted Ns.Name
ARROW       := '->' | '~>'                                      ; sync | async
comment     := '#' ~'\n'*
```

## Minimal example

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

## Semantics

- `[x,y,w,h]` — float-convertible integers; coordinates are **local** to parent namespace.
- Edges: `A -> B` = synchronous call/import dependency; `A ~> B` = async/event/reactive.
- Dotted identifiers in edges (`Tools.Scissors -> Core.Scene`) required when crossing namespaces; bare names allowed when unambiguous within scope.
- `src_path` can be a file (`scene.py`), a glob (`*.py`), or omitted (virtual node).
- Comments (`#`) and blank lines stripped by lexer — zero token cost.

## Wire format guarantees

- Deterministic serialization — sort nodes by `y` then `x`, edges lexicographically.
- Canonical whitespace — single space separators in rows; no trailing whitespace.
- `parse(serialize(ast)) == ast`.

