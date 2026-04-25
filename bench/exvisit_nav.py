"""
ExVisit-powered agentic LLM navigation benchmark.

A low-intelligence LLM uses ExVisit MCP tools (blast, locate, expand) and rg
in a real multi-turn tool-calling loop to navigate to the correct file.

Benchmark hypothesis: a 50% oracle LLM + ExVisit + rg → ~100% oracle hit rate,
at ~100× fewer tokens than raw file reading.

Usage:
    python -m bench.exvisit_nav --limit 10 --model qwen/qwen-2.5-7b-instruct
    python -m bench.exvisit_nav --limit 114 --model meta-llama/llama-3.1-8b-instruct
    python -m bench.exvisit_nav --dry-run --limit 3
    python -m bench.exvisit_nav --skip-to 40 --limit 114  # resume

Output: bench/results/exvisit_nav_<model>_<ts>.json + .report.md
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .metrics import (
    NavTrace, ToolCallRecord, BaselineRates, get_baseline,
    generate_comparison_report, save_nav_traces, count_tokens,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
_BENCH = _ROOT / "bench"
_CASES = _BENCH / "cases_dump.json"
_RESULTS = _BENCH / "results"
_DRIVER_PROMPT = _BENCH / "driver_prompt.md"

# WSL paths
_DJANGO_WSL = "/home/avinash_unix/bench_cache/repos/django"
_EXV_WSL = "/tmp/django_bench.exv"

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "qwen/qwen-2.5-7b-instruct"
MAX_TOOL_CALLS = 4   # budget per case


# ---------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling format)
# ---------------------------------------------------------------------------

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "exv_blast",
            "description": (
                "Rank files in the codebase by relevance to a bug report. "
                "Returns JSON list of {file, score} pairs. Use as your primary navigator."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_text": {"type": "string", "description": "Full issue / bug report text."},
                    "preset": {"type": "string", "enum": ["issue-fix", "crash-fix", "test-fix"], "default": "issue-fix"},
                },
                "required": ["issue_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "exv_locate",
            "description": "Multi-signal anchor scoring — more precise than blast. Use when blast is ambiguous.",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_text": {"type": "string"},
                    "topk": {"type": "integer", "default": 3},
                },
                "required": ["issue_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "exv_expand",
            "description": "Expand neighbors around a known anchor node. Use to verify a candidate file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "anchor": {"type": "string", "description": "Dotted FQN, e.g. django.core.management.commands.sqlmigrate"},
                    "hops": {"type": "integer", "default": 1},
                },
                "required": ["anchor"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rg",
            "description": (
                "Ripgrep: search for a pattern in the Django repo. "
                "Returns list of matching file paths. "
                "Use when blast score is low (< 0.65) to confirm with identifier search."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for (class name, method, error string)."},
                    "flags": {"type": "string", "description": "Extra rg flags, e.g. '-l' (files only) or '-i' (case insensitive). Default: '-l'", "default": "-l"},
                },
                "required": ["pattern"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool executors
# ---------------------------------------------------------------------------

def _wsl(cmd: str, timeout: int = 60) -> tuple[bool, str]:
    """Run a bash command in WSL. Returns (success, stdout_or_stderr)."""
    result = subprocess.run(
        ["wsl", "-d", "Ubuntu", "--", "bash", "-c", cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode == 0:
        return True, result.stdout.strip()
    return False, (result.stderr or result.stdout).strip()


def exec_exv_blast(issue_text: str, preset: str = "issue-fix", max_files: int = 5) -> tuple[bool, str]:
    import tempfile, os
    # Write issue to a WSL temp file
    tmp = f"/tmp/exv_issue_{os.getpid()}.txt"
    escaped = issue_text.replace("'", "'\\''")
    ok, err = _wsl(f"printf '%s' '{escaped}' > {tmp}")
    if not ok:
        return False, f"Could not write issue temp file: {err}"
    ok, out = _wsl(
        f"python3 -m exvisit blast {_EXV_WSL} "
        f"--issue-file {tmp} "
        f"--preset {preset} "
        f"--format json",
        timeout=90,
    )
    _wsl(f"rm -f {tmp}")
    if ok and max_files:
        try:
            results = json.loads(out)
            if isinstance(results, list):
                out = json.dumps(results[:max_files])
        except (json.JSONDecodeError, TypeError):
            pass
    return ok, out


def exec_exv_locate(issue_text: str, topk: int = 3) -> tuple[bool, str]:
    tmp = f"/tmp/exv_locate_{os.getpid()}.txt"
    escaped = issue_text.replace("'", "'\\''")
    _wsl(f"printf '%s' '{escaped}' > {tmp}")
    ok, out = _wsl(
        f"python3 -m exvisit locate {_EXV_WSL} "
        f"--issue-file {tmp} "
        f"--topk {topk} "
        f"--format json",
        timeout=60,
    )
    _wsl(f"rm -f {tmp}")
    return ok, out


def exec_exv_expand(anchor: str, hops: int = 1) -> tuple[bool, str]:
    ok, out = _wsl(
        f"python3 -m exvisit expand {_EXV_WSL} "
        f"--anchor '{anchor}' "
        f"--hops {hops} "
        f"--format json",
        timeout=60,
    )
    return ok, out


def exec_rg(pattern: str, flags: str = "-l") -> tuple[bool, str]:
    escaped = pattern.replace("'", "'\\''")
    ok, out = _wsl(
        f"rg {flags} '{escaped}' {_DJANGO_WSL}/django 2>/dev/null | head -20",
        timeout=30,
    )
    if not ok and not out:
        return True, "[]"  # rg returns exit 1 when no matches — treat as empty
    # Return as JSON list of paths
    files = [f.strip() for f in out.splitlines() if f.strip()]
    # Strip Django repo prefix to get relative paths
    rel = []
    for f in files:
        if _DJANGO_WSL in f:
            rel.append(f.replace(_DJANGO_WSL + "/", ""))
        else:
            rel.append(f)
    return True, json.dumps(rel)


def dispatch_tool(name: str, args: dict) -> tuple[bool, str]:
    """Execute a tool call and return (success, output_str)."""
    if name == "exv_blast":
        return exec_exv_blast(
            issue_text=args.get("issue_text", ""),
            preset=args.get("preset", "issue-fix"),
            max_files=int(args.get("max_files", 5)),
        )
    elif name == "exv_locate":
        return exec_exv_locate(
            issue_text=args.get("issue_text", ""),
            topk=int(args.get("topk", 3)),
        )
    elif name == "exv_expand":
        return exec_exv_expand(
            anchor=args.get("anchor", ""),
            hops=int(args.get("hops", 1)),
        )
    elif name == "rg":
        return exec_rg(
            pattern=args.get("pattern", ""),
            flags=args.get("flags", "-l"),
        )
    else:
        return False, f"Unknown tool: {name}"


def _dry_dispatch(name: str, args: dict, issue: str, oracle: list[str]) -> tuple[bool, str]:
    """Mock tool execution for dry runs."""
    if name == "exv_blast":
        # Return mock results that include oracle file
        mock = [{"file": oracle[0] if oracle else "django/db/models/query.py", "score": 0.82}]
        return True, json.dumps(mock)
    elif name == "exv_locate":
        mock = [{"file": oracle[0] if oracle else "django/db/models/query.py", "score": 0.78}]
        return True, json.dumps(mock)
    elif name == "exv_expand":
        return True, json.dumps({"anchor": args.get("anchor", ""), "neighbors": []})
    elif name == "rg":
        files = [oracle[0]] if oracle else []
        return True, json.dumps(files)
    return False, "unknown tool"


# ---------------------------------------------------------------------------
# LLM multi-turn conversation
# ---------------------------------------------------------------------------

def parse_final_prediction(content: str) -> tuple[list[str], str, str]:
    """Extract predicted_files, confidence, solve_mode from LLM JSON output."""
    # Strip <think>...</think> tags (Qwen3-Coder reasoning wrapper)
    content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
    # Try full JSON match
    m = re.search(
        r'\{[^}]*"predicted_files"\s*:\s*(\[[^\]]*\])[^}]*"confidence"\s*:\s*"([^"]+)"[^}]*"solve_mode"\s*:\s*"([^"]+)"',
        content, re.DOTALL,
    )
    if m:
        try:
            files = json.loads(m.group(1))
            return files, m.group(2), m.group(3)
        except Exception:
            pass

    # Fallback: grab any django/...py paths
    files = re.findall(r'django/[^\s"\'<>\]]+\.py', content)
    conf = "HIGH" if re.search(r'"confidence"\s*:\s*"HIGH"', content) else \
           "MED" if re.search(r'"confidence"\s*:\s*"MED"', content) else "LOW"
    mode = "blast_only"
    for m2 in ["blast+rg", "blast+locate", "multi_tool"]:
        if m2 in content:
            mode = m2
    return list(dict.fromkeys(files)), conf, mode  # deduplicated


def call_llm_loop(
    model: str,
    api_key: str,
    system_prompt: str,
    issue: str,
    exv_file_path: str,
    oracle: list[str],
    dry_run: bool = False,
) -> tuple[NavTrace, list[str], str, str]:
    """
    Run the multi-turn agentic conversation.
    Returns (trace, predicted_files, confidence, solve_mode).
    """
    import urllib.request, urllib.error

    trace_tool_calls: list[ToolCallRecord] = []
    prompt_tokens = 0
    completion_tokens = 0

    user_msg = (
        f"exv_file: {exv_file_path}\n"
        f"repo_path: {_DJANGO_WSL}\n\n"
        f"Issue:\n{issue}"
    )

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/atlas-pro/exvisit-bench",
        "X-Title": "ExVisit SWE-bench Benchmark",
    }

    tool_call_count = 0
    predicted_files: list[str] = []
    confidence = "LOW"
    solve_mode = "unknown"

    while tool_call_count <= MAX_TOOL_CALLS:
        if dry_run:
            # Simulate: first turn calls blast, second turn submits
            if tool_call_count == 0:
                mock_args = json.dumps({"issue_text": issue[:100], "preset": "issue-fix", "max_files": 5})
                tool_call_count += 1
                success, result = _dry_dispatch("exv_blast", {"issue_text": issue, "max_files": 5}, issue, oracle)
                rec = ToolCallRecord(
                    tool="exv_blast",
                    args_summary="issue_text[:100], preset=issue-fix, max_files=5",
                    args_tokens=count_tokens(mock_args),
                    output_tokens=count_tokens(result),
                    success=success,
                )
                trace_tool_calls.append(rec)
                messages.append({"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_dry_1", "type": "function", "function": {"name": "exv_blast", "arguments": mock_args}}
                ]})
                messages.append({"role": "tool", "tool_call_id": "call_dry_1", "content": result})
            else:
                # Final answer
                predicted_files = [oracle[0]] if oracle else ["UNKNOWN"]
                confidence = "HIGH"
                solve_mode = "blast_only"
                break
            continue

        # Real LLM call
        body = json.dumps({
            "model": model,
            "messages": messages,
            "tools": _TOOLS,
            "tool_choice": "auto" if tool_call_count < MAX_TOOL_CALLS else "none",
            "temperature": 0,
            "max_tokens": 512,
        }).encode()

        req = urllib.request.Request(
            f"{OPENROUTER_BASE}/chat/completions",
            data=body, headers=headers, method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:300]}")

        usage = data.get("usage", {})
        prompt_tokens += usage.get("prompt_tokens", 0)
        completion_tokens += usage.get("completion_tokens", 0)

        choice = data["choices"][0]
        msg = choice["message"]
        finish = choice.get("finish_reason", "")

        messages.append(msg)

        # If no tool call → parse final answer
        tool_calls_in_response = msg.get("tool_calls") or []
        if not tool_calls_in_response or finish == "stop":
            content = msg.get("content") or ""
            predicted_files, confidence, solve_mode = parse_final_prediction(content)
            break

        # Execute each tool call
        for tc in tool_calls_in_response:
            if tool_call_count >= MAX_TOOL_CALLS:
                # Force answer — inject a stop message
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": "Tool call budget exhausted. Submit your best guess now.",
                })
                continue

            tool_name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except Exception:
                args = {}

            args_str = tc["function"]["arguments"]
            success, result = dispatch_tool(tool_name, args)

            rec = ToolCallRecord(
                tool=tool_name,
                args_summary=args_str[:120],
                args_tokens=count_tokens(args_str),
                output_tokens=count_tokens(result),
                success=success,
                error=None if success else result[:200],
            )
            trace_tool_calls.append(rec)

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result if success else f"ERROR: {result}",
            })
            tool_call_count += 1

    return trace_tool_calls, predicted_files, confidence, solve_mode, prompt_tokens, completion_tokens


# ---------------------------------------------------------------------------
# Main benchmark loop
# ---------------------------------------------------------------------------

def run_benchmark(
    model: str,
    limit: int,
    api_key: str,
    dry_run: bool = False,
    skip_to: int = 0,
) -> None:
    cases = json.loads(_CASES.read_text(encoding="utf-8"))[:limit]
    system_prompt = _DRIVER_PROMPT.read_text(encoding="utf-8")
    sys_tokens = count_tokens(system_prompt)
    baseline = get_baseline(model)

    slug = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
    ts = int(time.time())
    out_path = _RESULTS / f"exvisit_nav_{slug}_{ts}.json"
    _RESULTS.mkdir(parents=True, exist_ok=True)

    traces: list[NavTrace] = []
    hits = hit1 = 0

    print(f"\n=== ExVisit Agentic Navigator ===")
    print(f"Model        : {model}")
    print(f"Cases        : {len(cases)}")
    print(f"Baseline@1   : {baseline.oracle_hit_at1 or 'unknown'}")
    print(f"Dry run      : {dry_run}")
    print()

    # --- Smoke test: verify blast works before spending hours ---
    if not dry_run:
        print("Smoke test: checking exv_blast...", end=" ", flush=True)
        first = cases[0]
        _wsl(f"cd {_DJANGO_WSL} && git checkout -f {first['commit']} -q 2>/dev/null", timeout=30)
        ok_init, err_init = _wsl(
            f"python3 -m exvisit init --repo {_DJANGO_WSL} --out {_EXV_WSL}",
            timeout=120,
        )
        if not ok_init:
            print(f"FAIL (exv init: {err_init[:100]})")
            sys.exit(1)
        ok_blast, out_blast = exec_exv_blast(first["issue"][:500], preset="issue-fix", max_files=5)
        if not ok_blast:
            print(f"FAIL (exv_blast: {out_blast[:200]})")
            sys.exit(1)
        print(f"OK ({len(out_blast)} bytes)")

    last_commit = None

    for i, case in enumerate(cases):
        if i < skip_to:
            continue

        case_id = case["id"]
        commit = case["commit"]
        oracle = case["oracle"]
        issue = case["issue"]

        t = NavTrace(
            case_id=case_id,
            oracle_files=oracle,
            system_prompt_tokens=sys_tokens,
            issue_tokens=count_tokens(issue),
        )

        print(f"[{i+1:3d}/{len(cases)}] {case_id}", end="  ", flush=True)
        t_start = time.time()

        # 1. Checkout commit + generate .exv
        try:
            if commit != last_commit:
                _wsl(f"cd {_DJANGO_WSL} && git checkout -f {commit} -q 2>/dev/null", timeout=30)
                if not dry_run:
                    ok, err = _wsl(
                        f"python3 -m exvisit init --repo {_DJANGO_WSL} --out {_EXV_WSL}",
                        timeout=120,
                    )
                    if not ok:
                        raise RuntimeError(f"exv init: {err[:100]}")
                last_commit = commit
                print("✓exv", end=" ", flush=True)
        except Exception as e:
            t.error = str(e)
            traces.append(t)
            print(f"  SKIP ({e})")
            continue

        # 2. Run agentic loop
        try:
            tool_records, predicted, confidence, solve_mode, ptok, ctok = call_llm_loop(
                model=model,
                api_key=api_key,
                system_prompt=system_prompt,
                issue=issue,
                exv_file_path=_EXV_WSL,
                oracle=oracle,
                dry_run=dry_run,
            )
        except Exception as e:
            t.error = str(e)
            t.elapsed_s = time.time() - t_start
            traces.append(t)
            print(f"  LLM_ERR({e})")
            continue

        # 3. Record
        for rec in tool_records:
            t.record_tool(rec)
        t.predicted_files = predicted
        t.confidence = confidence  # type: ignore[assignment]
        t.total_prompt_tokens = ptok
        t.total_completion_tokens = ctok
        t.elapsed_s = time.time() - t_start
        t.finalize()

        if t.oracle_hit:
            hits += 1
        if t.oracle_hit_at1:
            hit1 += 1

        n_done = i + 1 - skip_to
        print(
            f"{'HIT✓' if t.oracle_hit else 'MISS✗'} "
            f"conf={confidence} mode={solve_mode} "
            f"tools={len(tool_records)} navtok={t.total_nav_tokens} "
            f"pred={predicted[0] if predicted else 'NONE'}"
        )

        traces.append(t)

        # Incremental save
        save_nav_traces(traces, model, out_path, baseline)
        time.sleep(0.3)

    # Final
    n = len(traces) - sum(1 for t in traces if t.error)
    print(f"\n=== Results ===")
    print(f"oracle_hit     : {hits}/{n} ({100*hits/max(1,n):.1f}%)")
    print(f"oracle_hit@1   : {hit1}/{n} ({100*hit1/max(1,n):.1f}%)")
    print(f"baseline@1     : {baseline.oracle_hit_at1 or 'unknown'}")
    print(f"Results        : {out_path}")
    print(f"Report         : {out_path.with_suffix('.report.md')}")
    print()
    print(generate_comparison_report(traces, model, baseline))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="ExVisit agentic LLM navigation benchmark")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--limit", type=int, default=114)
    parser.add_argument("--skip-to", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        keys_file = _ROOT / "config" / "keys.ps1"
        if keys_file.exists():
            m = re.search(
                r'OPENROUTER_API_KEY\s*[=:]\s*["\']?([^"\';\s]+)',
                keys_file.read_text(encoding="utf-8"),
            )
            if m:
                api_key = m.group(1)

    if not api_key and not args.dry_run:
        print("ERROR: OPENROUTER_API_KEY not set. Use --dry-run or set the env var.")
        sys.exit(1)

    run_benchmark(
        model=args.model,
        limit=args.limit,
        api_key=api_key,
        dry_run=args.dry_run,
        skip_to=args.skip_to,
    )


if __name__ == "__main__":
    main()
