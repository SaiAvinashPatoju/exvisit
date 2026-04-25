"""Standalone blast quality test — measures raw ExVisit recall without LLM."""
import json, time, sys
from pathlib import Path
from bench.exvisit_nav import exec_exv_blast, _wsl, _DJANGO_WSL, _EXV_WSL

_CASES = Path("bench/cases_dump.json")
cases = json.loads(_CASES.read_text(encoding="utf-8"))

limit = int(sys.argv[1]) if len(sys.argv) > 1 else 20

hit1 = hit3 = hit5 = hit_any = 0
total = 0
last_commit = None
failures = []

for i, case in enumerate(cases[:limit]):
    cid = case["id"]
    commit = case["commit"]
    oracle = case["oracle"]
    issue = case["issue"]

    print(f"[{i+1:3d}/{limit}] {cid}", end="  ", flush=True)

    # Checkout + init .exv if new commit
    if commit != last_commit:
        _wsl(f"cd {_DJANGO_WSL} && git checkout -f {commit} -q 2>/dev/null", timeout=30)
        ok, err = _wsl(
            f"python3 -m exvisit init --repo {_DJANGO_WSL} --out {_EXV_WSL}",
            timeout=120,
        )
        if not ok:
            print(f"SKIP (init: {err[:60]})")
            continue
        last_commit = commit

    # Run blast
    ok, out = exec_exv_blast(issue[:2000], preset="issue-fix", max_files=10)
    if not ok:
        print(f"BLAST_ERR: {out[:80]}")
        continue

    total += 1

    # Parse results
    try:
        data = json.loads(out)
        files = data.get("selected_files", [])
    except Exception:
        files = []

    # Check oracle
    oracle_set = set(oracle)
    at1 = files[0] in oracle_set if files else False
    at3 = any(f in oracle_set for f in files[:3])
    at5 = any(f in oracle_set for f in files[:5])
    at_any = any(f in oracle_set for f in files)

    if at1: hit1 += 1
    if at3: hit3 += 1
    if at5: hit5 += 1
    if at_any: hit_any += 1

    tag = "HIT@1" if at1 else ("HIT@3" if at3 else ("HIT@5" if at5 else ("HIT" if at_any else "MISS")))
    print(f"{tag:6s} oracle={oracle[0]:50s} blast_top={files[0] if files else 'NONE':50s}")

    if not at5:
        failures.append({
            "case": cid,
            "oracle": oracle,
            "blast_top5": files[:5],
        })

print(f"\n=== Standalone Blast Quality ({total} cases) ===")
print(f"  hit@1:   {hit1}/{total} ({100*hit1/max(1,total):.1f}%)")
print(f"  hit@3:   {hit3}/{total} ({100*hit3/max(1,total):.1f}%)")
print(f"  hit@5:   {hit5}/{total} ({100*hit5/max(1,total):.1f}%)")
print(f"  hit@10:  {hit_any}/{total} ({100*hit_any/max(1,total):.1f}%)")

if failures:
    print(f"\n=== Failures (not in top-5, {len(failures)} cases) ===")
    for f in failures[:10]:
        print(f"  {f['case']}")
        print(f"    oracle: {f['oracle']}")
        print(f"    blast:  {f['blast_top5']}")
