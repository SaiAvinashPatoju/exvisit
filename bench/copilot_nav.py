"""LLM Navigation benchmark harness — Copilot edition.

This script:
1. Loads cases from cases_dump.json
2. For each case, checks out the right Django commit
3. Prints the issue text for the LLM (Copilot) to navigate
4. Records Copilot's file predictions
5. Compares against oracle and ExVisit

Usage:
  Called by Copilot agent — not standalone.
"""
import json
import subprocess
from pathlib import Path

CASES_PATH = Path(__file__).parent / "cases_dump.json"
RESULTS_PATH = Path(__file__).parent / "results" / "copilot_nav.json"
DJANGO_REPO = Path("/home/avinash_unix/bench_cache/repos/django")


def load_cases():
    with open(CASES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def checkout_commit(commit: str):
    """Checkout a specific commit in the Django repo."""
    subprocess.run(
        ["git", "checkout", "-f", commit],
        cwd=str(DJANGO_REPO),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "clean", "-fdx"],
        cwd=str(DJANGO_REPO),
        capture_output=True,
    )


def save_prediction(case_id: str, predicted_files: list, results_path=RESULTS_PATH):
    """Append a prediction to the results file."""
    results_path.parent.mkdir(parents=True, exist_ok=True)
    
    if results_path.exists():
        with open(results_path, "r") as f:
            results = json.load(f)
    else:
        results = []
    
    # Update or append
    existing = {r["id"]: r for r in results}
    existing[case_id] = {
        "id": case_id,
        "predicted_files": predicted_files,
    }
    results = list(existing.values())
    
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2)


def compute_metrics(results_path=RESULTS_PATH, cases_path=CASES_PATH):
    """Compute oracle hit rate and hit@1 from predictions."""
    with open(cases_path) as f:
        cases = {c["id"]: c for c in json.load(f)}
    with open(results_path) as f:
        results = json.load(f)
    
    total = 0
    hits = 0
    hits_at_1 = 0
    
    for r in results:
        case = cases.get(r["id"])
        if not case:
            continue
        total += 1
        oracle = set(case["oracle"])
        predicted = r["predicted_files"]
        
        if oracle & set(predicted):
            hits += 1
        if predicted and predicted[0] in oracle:
            hits_at_1 += 1
    
    return {
        "total": total,
        "oracle_hit_rate": hits / total if total else 0,
        "oracle_hit_at_1": hits_at_1 / total if total else 0,
        "hits": hits,
        "hits_at_1": hits_at_1,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "metrics":
        m = compute_metrics()
        print(json.dumps(m, indent=2))
    else:
        cases = load_cases()
        print(f"Loaded {len(cases)} cases")
        for c in cases[:3]:
            print(f"  {c['id']} -> oracle: {c['oracle']}")
