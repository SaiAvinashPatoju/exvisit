# Project exvisit — `status.md`

> **Session continuity payload.** Read this before every new session. Do not skip.

## Snapshot — 2026-04-22 (end of research session 5)

| Layer                     | State          | Location                                      |
|---------------------------|----------------|-----------------------------------------------|
| Spec DSL                  | **v0.3 live**  | `spec/exvisit-dsl-v0.1.md`                      |
| Spec DSL (proposed)       | **v0.4 draft** | `spec/exvisit-dsl-v0.4-draft.md`                |
| Python reference impl     | shipping       | `exvisit/*.py`                                  |
| Rust production port      | scaffolded     | `rust/crates/exvisit-*`                         |
| Canonical example         | verify-clean   | `examples/myslicer.exv` (v0.3)              |
| Auto-scaffolded example   | verify-clean   | `examples/myslicer.generated.exv`           |
| SWE-bench harness         | shipping       | `bench/swebench_lite_harness.py`              |
| Blast bundler             | shipping       | `exvisit/blast.py` + `config/blast_presets.json` |
| Error anchor              | shipping       | `exvisit/anchor.py` + `python -m exvisit anchor`  |
| Reports                   | 3 docs         | `reports/`                                    |

## Capabilities shipped

- `python -m exvisit parse|query|deps|callers|graph|verify|init|anchor`
- `python -m exvisit blast` for anchor-driven markdown/json context bundles with configurable presets
- `lines=` locators now round-trip through parser/serializer/scaffold and drive line-precise blast snippets
- 17/17 targeted tests green (`test_exvisit`, `test_blast`, `test_swebench_harness`)
- Hand-authored exvisit: **1 071 tokens**, `verify: OK`
- Auto-scaffold from any Python repo → verify-clean in one command
- Token compression: 34.4% vs compact JSON, **125× vs raw source**
- `bench/swebench_lite_harness.py precompute|run` for repository-level control-vs-exvisit navigation benchmarks
- Real SWE-bench Lite smoke trial on `psf/requests` (3 cases): **91.8% token reduction**, **80% fewer steps**, **83.3% lower context rot**, **66.7% oracle-hit@1 for exvisit vs 0% control**
- Real SWE-bench Lite `pytest-dev/pytest` trial (17 cases): **97.8% token reduction**, **80% fewer steps**, **53.7% lower context rot**, **23.53% oracle-hit@1 for exvisit vs 0% control**
- Real SWE-bench Lite `django/django` sample trial (20 cases from a 101-case materialized partial): **98.9% token reduction**, **80% fewer steps**, **58.2% lower context rot**, **35.0% oracle-hit@1 for exvisit vs 0% control**

## Recent fixes (session 2)

- UTF-8 BOM handling in AST import extraction (`utf-8-sig`)
- Slash-boundary suffix matcher in verify resolver
- Lexer precedence fix: PATH beats IDENT when token contains `/` or `*`
- `from pkg import module` expanded to `pkg.module` in scaffold + verify

## Recent fixes (session 3)

- Added real SWE-bench Lite dataset ingestion via Hugging Face `datasets`
- Added repository materialization + exvisit precompute sweep for benchmark manifests
- Added control-vs-exvisit navigation runner with token/step/context-rot/oracle-hit telemetry
- Added AST symbol-range boosting so issue terms like `Session.resolve_redirects` rank `sessions.py`
- Suppressed noisy vendored-file `SyntaxWarning` during AST parsing in scaffold + harness

## Recent fixes (session 4)

- Ported the useful core of the legacy TS `blast` architecture into exvisit_pro as `exvisit/blast.py`
- Added configurable blast presets in `config/blast_presets.json`
- Added `python -m exvisit blast` to build manifest-first debug bundles from issue/error text
- Added focused blast tests in `tests/test_blast.py`
- Switched `bench/swebench_lite_harness.py` to use the new blast bundle as the experimental exvisit path

## Recent fixes (session 5)

- Added `exvisit anchor` for raw traceback / error-log ingestion and ranked anchor reports
- Added `lines=` support to `Node`, parser, serializer, scaffold, and blast snippet selection
- Fixed scaffold output for hidden, tilde-prefixed, and numeric-prefixed Python module filenames
- Hardened blast snippet extraction for empty files and exvisit line-range edge cases
- Accelerated harness precompute with repo/commit exvisit reuse and a faster import-scanning mode
- Accelerated control baseline search with external search backends (`rg` when present, `git grep` fallback)
- Materialized 101/114 django cases into `bench/.cache/django_trial/manifest_partial.json` for resumable large-repo benchmarking

## Open research frontiers (from Task 4 of 2026-04-22 research)

- **`@L3` execution constraints** — thread/mutex/reentrancy boundaries as first-class
- **Typed edge relations** — borrow old-exvisit taxonomy (`calls`, `writes_state`, `triggers_api`)
- **Telemetry overlay** — inject runtime trace counts onto spatial nodes (`@metrics` block)
- **Concurrency contracts** — `owns_thread`, `must_run_on`, `emits_from_thread` annotations

## Don't break

- `parse(serialize(doc)) == doc`
- `~>` edges are informational only — never verified as imports
- Edge endpoints may be bare names; dotted FQNs required on collisions (SPEC-004, still open)

## Next natural step

Land the benchmark-execution layer around the new navigation core: containerized mini-SWE-agent sandboxing with exvisit-only navigation tools, AST-located `exvisit_edit`, token-and-cache cost telemetry from agent trajectories, and resumable large-repo batch execution for the remaining django cases.

