"""Quick analysis of benchmark results."""
import json, sys

with open("bench/results/exvisit_nav_qwen-qwen3-coder_1777097341.json") as f:
    d = json.load(f)

traces = d["traces"]
print(f"Total traces: {len(traces)}")

# Count hits vs misses
hits = [t for t in traces if t.get("oracle_hit_at1")]
misses = [t for t in traces if not t.get("oracle_hit_at1")]
any_hits = [t for t in traces if t.get("oracle_hit")]
print(f"Hits@1: {len(hits)}, Hits(any): {len(any_hits)}, Misses: {len(misses)}")
print(f"Oracle@1 rate: {len(hits)/len(traces)*100:.1f}%")
print(f"Oracle(any) rate: {len(any_hits)/len(traces)*100:.1f}%")

# Show all hits
print("\n=== HITS ===")
for t in any_hits:
    cid = t["case_id"]
    print(f"  {cid}")
    print(f"    pred={t.get('predicted_files',[])}")
    print(f"    oracle={t.get('oracle_files',[])}")
    print(f"    tools={t.get('total_tool_calls',0)} navtok={t.get('total_nav_tokens',0)}")
    print(f"    at1={t.get('oracle_hit_at1')} at3={t.get('oracle_hit_at3')}")

# Analyze predictions
none_preds = [t for t in traces if not t.get("predicted_files") or t["predicted_files"] == ["NONE"] or t["predicted_files"] == []]
has_preds = [t for t in traces if t.get("predicted_files") and t["predicted_files"] != ["NONE"] and t["predicted_files"] != []]
print(f"\nNo prediction (NONE/empty): {len(none_preds)}")
print(f"Has prediction: {len(has_preds)}")

# Show misses WITH predictions
print("\n=== MISSES WITH PREDICTIONS (LLM tried but wrong) ===")
for t in has_preds:
    if not t.get("oracle_hit"):
        cid = t["case_id"]
        print(f"  {cid}")
        print(f"    pred={t.get('predicted_files',[])}")
        print(f"    oracle={t.get('oracle_files',[])}")

# Tool success/fail breakdown
print("\n=== TOOL SUCCESS/FAIL BREAKDOWN ===")
blast_ok = blast_fail = rg_ok = rg_fail = 0
for t in traces:
    for c in t.get("tool_calls", []):
        tool = c.get("tool", "")
        if tool == "exv_blast":
            if c.get("success"): blast_ok += 1
            else: blast_fail += 1
        elif tool == "rg":
            if c.get("success"): rg_ok += 1
            else: rg_fail += 1
print(f"  exv_blast: {blast_ok} OK, {blast_fail} FAIL ({100*blast_fail/max(1,blast_ok+blast_fail):.0f}% fail rate)")
print(f"  rg:        {rg_ok} OK, {rg_fail} FAIL ({100*rg_fail/max(1,rg_ok+rg_fail):.0f}% fail rate)")

# Show the error for blast
print("\n=== BLAST ERROR (first) ===")
for t in traces:
    for c in t.get("tool_calls", []):
        if c.get("tool") == "exv_blast" and not c.get("success"):
            print(f"  {c.get('error','')}")
            break
    else:
        continue
    break

# Cases grouped by tool usage pattern
print("\n=== CASE PATTERNS ===")
patterns = {}
for t in traces:
    key = f"tools={t.get('total_tool_calls',0)} blast_ok={sum(1 for c in t.get('tool_calls',[]) if c['tool']=='exv_blast' and c['success'])} blast_fail={sum(1 for c in t.get('tool_calls',[]) if c['tool']=='exv_blast' and not c['success'])}"
    patterns[key] = patterns.get(key, 0) + 1
for k, v in sorted(patterns.items(), key=lambda x: -x[1]):
    print(f"  [{v:2d}x] {k}")
