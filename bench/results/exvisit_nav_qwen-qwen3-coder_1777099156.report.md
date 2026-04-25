# ExVisit Navigator — Comparison Report

**Model:** `qwen/qwen3-coder`  |  **Cases:** 51 total, 51 completed, 0 errors
**Baseline note:** no baseline data — run without ExVisit to establish

| Metric | Baseline (no ExVisit) | ExVisit-Powered | Delta |
|---|---:|---:|---:|
| oracle hit@1 | unknown | 35.3% |  |
| oracle hit@3 | unknown | 39.2% |  |
| oracle hit (any) | unknown | 41.2% |  |
| avg nav tokens | ~50,000 | 2,795 | 18× less |
| avg tool calls | N/A | 3.1 | — |
| blast-only solve rate | N/A | 0.0% | — |
| rg-assist rate | N/A | 5.9% | — |
| locate-assist rate | N/A | 0.0% | — |

## Solve Mode Distribution

- `multi_tool`: 38 cases (74.5%)
- `unknown`: 10 cases (19.6%)
- `blast+rg`: 3 cases (5.9%)

## Confidence Distribution

- `HIGH`: 23 cases — oracle hit 69.6%
- `LOW`: 20 cases — oracle hit 15.0%
- `MED`: 8 cases — oracle hit 25.0%