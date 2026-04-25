#!/usr/bin/env python3
import json
from pathlib import Path

results_dir = Path("bench/results")
# Find the latest qwen3-coder result file
qwen_files = sorted(results_dir.glob("exvisit_nav_qwen-qwen3-coder_*.json"))
if not qwen_files:
    print("No results yet.")
    exit(0)

latest = qwen_files[-1]
d = json.loads(latest.read_text())

print(f"Cases run: {d['cases_run']}/114")
print(f"Hits: {d['hits']}/{d['cases_run']} ({100*d['hits']/d['cases_run']:.1f}%)")
print(f"Avg nav tokens: {d['tokens']['avg_exv_per_case']:.0f}")
modes = set(t['solve_mode'] for t in d['results'])
print(f"Solve modes: {modes}")
print(f"\nToken summary:")
print(f"  Total ExV blast: {d['tokens']['total_exv_blast']:,}")
print(f"  Total LLM prompt: {d['tokens']['total_llm_prompt']:,}")
print(f"  Total LLM complete: {d['tokens']['total_llm_completion']:,}")
