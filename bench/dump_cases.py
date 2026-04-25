"""Dump SWE-bench Lite Django cases to JSON for manual LLM navigation."""
import json
import sys
sys.path.insert(0, ".")
from bench.dataset import load_django_instances

cases = load_django_instances(limit=300)
out = []
for c in cases:
    out.append({
        "id": c.instance_id,
        "repo": c.repo,
        "commit": c.base_commit,
        "oracle": c.oracle_files,
        "issue": c.problem_statement,
    })

with open("bench/cases_dump.json", "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2)

print(f"Dumped {len(out)} cases to bench/cases_dump.json")
# Print first 5 summaries
for c in out[:5]:
    print(f"\n{'='*60}")
    print(f"ID: {c['id']}")
    print(f"Oracle: {c['oracle']}")
    print(f"Issue (first 200 chars): {c['issue'][:200]}")
