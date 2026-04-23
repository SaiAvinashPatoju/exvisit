# ExVisit Benchmark — 101-Case Django SWE-Bench Lite

**Repository:** `django/django`  
**Dataset:** SWE-Bench Lite  
**Cases:** 101  
**Runner model:** `nvidia/nemotron-3-super-120b-a12b:free` via OpenRouter  
**ExVisit scoring:** v2 (log-linear multi-signal ranker + `.meta.json` sidecar)  
**Date:** 2026-04-23  

---

## Summary table

| Metric | Control (grep baseline) | ExVisit v2 | Δ |
|---|---:|---:|---:|
| Avg input tokens | 129,652 | **1,763** | **−98.6%** |
| Avg tool calls (steps) | 5.0 | **1.0** | **−80%** |
| Avg context rot index | 3.83 | **1.57** | **−58.9%** |
| Oracle file hit rate | 7.9% | **61.4%** | **+53.5 pp** |
| Oracle rank-1 precision (hit@1) | 0.99% | **40.6%** | **+41× improvement** |

**Token compression factor: 73.5×** — the ExVisit blast bundle for the median Django issue fits in ~1.8 K tokens vs. 130 K for the grep control strategy.

---

## What these metrics measure

**Oracle file hit rate** — the fraction of cases where the file that was actually patched in the gold SWE-Bench answer appears anywhere in the ExVisit blast-selected file list. This is a navigation precision metric, not a pass@1 code-generation metric.

**Oracle rank-1 precision (hit@1)** — the gold patch file was the *first* file ExVisit selected as the anchor. This is the tightest signal: the agent gets exactly the right file as its primary context with no need for follow-up navigation.

**Context rot index** — the rank of the oracle file in the ordered selection list (0 = first; higher = more noise before the relevant file). Lower is better. The control baseline loads many irrelevant files before stumbling on the correct one; ExVisit's spatial anchor selection inverts that distribution.

**Steps** — tool calls made in the navigation phase. ExVisit consistently resolves the correct context in exactly 1 call (`exvisit blast`). The grep baseline averages 5 calls.

---

## Control baseline methodology

The control strategy simulates a standard agentic file-search loop:

1. Extract keywords from the issue text.
2. Score all repo files by keyword overlap (TF-IDF).
3. Read top-K file snippets (up to token budget).
4. Repeat until budget exhausted.

Token counts include all source file content loaded during navigation. The 129 K average reflects loading multiple full Django modules per issue.

---

## ExVisit v2 methodology

1. **Precompute phase:** `exvisit init --repo django/django --out <case>.exv` scaffolds a `.exv` graph per case, plus a `.meta.json` sidecar with typed edges (import, inherit, config-ref, test-of), per-node PageRank, cluster membership, and line counts.
2. **Run phase:** `exvisit blast <case>.exv --issue-text "..."` runs the v2 log-linear ranker:
   - BM25 lexical match
   - Explicit path match (literal file references in issue text)
   - Symbol-exact match (quoted class/function names)
   - Django dunder lookup signal
   - Domain bias (ORM vs. forms vs. admin disambiguation)
   - Error code match (e.g., `models.E028`)
   - Management command match (e.g., `sqlmigrate`)
   - PageRank centrality prior
   - Cluster-IDF
   - Test-file and migration-file gates (penalty, not exclusion)
3. Returns a compact bundle: ≤5 files, ≤6 code snippets, total ≤2 K tokens.

---

## Per-strategy cost estimate

At standard Anthropic Claude pricing ($3 / 1M input tokens):

| Navigation mode | Avg tokens | Cost per case |
|---|---:|---:|
| Control (grep) | 129,652 | ~$0.389 |
| ExVisit v2 | 1,763 | ~$0.005 |
| **Savings per case** | | **~$0.384 (98.6%)** |

At scale: 1,000 bug-fix sessions per day — ExVisit saves **~$384/day** in navigation-only token cost, before any code generation.

---

## Failure mode analysis

Cases where ExVisit missed the oracle file at rank 1 fall into identifiable buckets:

| Failure category | Share of misses | Description |
|---|---:|---|
| Config-leaf nodes | ~20% | Files like `conf/global_settings.py` have zero import-graph inbound edges; only reached via settings-constant signal |
| Same-cluster near-miss | ~18% | Right directory, wrong file; sibling expansion partially addresses this |
| Domain vocabulary gap | ~15% | Issue text uses natural-language synonyms not present in file tokens |
| Multi-oracle cases | ~12% | Gold patch touches 2+ files; only first is measured |
| Registry `__init__.py` | ~8% | Large `__init__.py` registries picked up correctly by v2 but crowded out by test-file BM25 mass |

---

## Reproducing this benchmark

```bash
# Prerequisites: Python 3.11+, git bash on PATH (Windows)
pip install exvisit[bench]

export OPENROUTER_API_KEY="<your-key>"
export EXVISIT_MODEL="nvidia/nemotron-3-super-120b-a12b:free"

python scratch/run_vnext_openrouter.py \
  --limit 101 \
  --resume \
  --out bench/.cache/django_trial/results_vnext_openrouter_101.json \
  --workspace-root bench/.cache/django_trial/workspaces/full_101
```

The script precomputes the manifest (one-time, cached), then runs the navigation harness. Total wall time on a single machine: ~40 minutes for 101 cases using a free OpenRouter model.

---

## Comparison with prior ExVisit iterations

| Run | Cases | Oracle hit | Oracle hit@1 | Avg tokens |
|---|---:|---:|---:|---:|
| v1 baseline (Gemini Flash, 101 cases) | 101 | 36.6% | 28.7% | 1,270 |
| v2.2 (Nemotron 40 cases) | 40 | 37.5% | 27.5% | 1,278 |
| **v2.3 (Nemotron 101 cases)** | **101** | **61.4%** | **40.6%** | **1,763** |

The jump from v1→v2.3 on oracle_hit (+24.8 pp) comes from five architectural changes:
1. Typed edge expansion (import / inherit / config-ref) with structural priors
2. Domain bias signal (ORM vs. forms disambiguation)
3. Django-specific error code, settings constant, and management command signals
4. Test-file and migration-file multiplicative gating (not additive penalty)
5. `__init__.py` registry inclusion in scaffolder
