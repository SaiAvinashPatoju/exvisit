"""Main benchmark orchestrator: SWE-bench Lite + OpenHands + ExVisit MCP.

Usage:
    # Smoke test (5 Django cases, navigation only)
    python -m bench.run_bench --mode smoke --limit 5

    # Full 300-case benchmark
    python -m bench.run_bench --mode full --limit 300

    # Navigation-only benchmark (no agent, just exvisit metrics)
    python -m bench.run_bench --mode nav-only --limit 300

    # With specific model
    python -m bench.run_bench --mode smoke --model anthropic/claude-sonnet-4 --limit 5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .dataset import load_django_instances, SWEBenchInstance
from .metrics import (
    CaseMetrics, BenchmarkSummary,
    compute_navigation_metrics, compute_summary,
    format_summary_table, save_results, count_tokens,
)
from .prepare import prepare_case

console = Console()

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
_BENCH_ROOT = Path(__file__).resolve().parent
DEFAULT_CACHE_DIR = _BENCH_ROOT / ".cache"
DEFAULT_WORKSPACE_ROOT = _BENCH_ROOT / ".cache" / "workspaces"
DEFAULT_OUTPUT_DIR = _BENCH_ROOT / "results"
DEFAULT_MODEL = "anthropic/claude-sonnet-4"
DEFAULT_MAX_ITERATIONS = 30
DEFAULT_TIMEOUT = 600


def get_api_key() -> str:
    """Get LLM API key from environment."""
    for var in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "LLM_API_KEY"]:
        key = os.environ.get(var)
        if key:
            return key
    raise ValueError(
        "No API key found. Set one of: ANTHROPIC_API_KEY, OPENAI_API_KEY, "
        "OPENROUTER_API_KEY, or LLM_API_KEY"
    )


def get_base_url(model: str) -> str | None:
    """Infer base URL from model name."""
    if model.startswith("openrouter/") or os.environ.get("OPENROUTER_API_KEY"):
        return "https://openrouter.ai/api/v1"
    return None


def normalize_model_name(model: str) -> str:
    """Ensure model has the correct litellm provider prefix."""
    if os.environ.get("OPENROUTER_API_KEY") and not model.startswith("openrouter/"):
        return f"openrouter/{model}"
    return model


# ---------------------------------------------------------------------------
# Navigation-only benchmark (no agent needed)
# ---------------------------------------------------------------------------

def run_navigation_only(
    instances: list[SWEBenchInstance],
    cache_dir: Path,
    workspace_root: Path,
    output_dir: Path,
) -> list[CaseMetrics]:
    """Run ExVisit navigation benchmark without an agent.

    Measures: oracle hit rate, oracle hit@1, context rot, token reduction.
    """
    from .prepare import (
        ensure_repo_clone, checkout_worktree,
        generate_exv_map, run_exv_blast,
    )

    all_metrics: list[CaseMetrics] = []

    for i, instance in enumerate(instances):
        console.print(f"[bold][{i+1}/{len(instances)}][/bold] {instance.instance_id}")
        metrics = CaseMetrics(instance_id=instance.instance_id)
        metrics.oracle_files = instance.oracle_files

        t0 = time.time()
        try:
            prep = prepare_case(instance, cache_dir, workspace_root)
            blast_bundle = prep["blast_bundle"]
            blast_md = prep["blast_md"]

            # Extract blast files
            if isinstance(blast_bundle, dict):
                blast_files = blast_bundle.get("selected_files", [])
                if not blast_files:
                    blast_files = list(blast_bundle.get("files", {}).keys())
                if not blast_files and "ranked" in blast_bundle:
                    blast_files = [r.get("file", r.get("path", ""))
                                   for r in blast_bundle["ranked"]]
            else:
                blast_files = []

            metrics.blast_files = blast_files
            metrics.exvisit_tokens = count_tokens(
                blast_md if isinstance(blast_md, str) else str(blast_md)
            )
            metrics.control_tokens = 129_652  # benchmark.md average

            hit, hit1, rot = compute_navigation_metrics(blast_files, instance.oracle_files)
            metrics.oracle_hit = hit
            metrics.oracle_hit_1 = hit1
            metrics.context_rot_index = rot
            metrics.exvisit_time_s = time.time() - t0

            status = "[green]HIT" if hit else "[red]MISS"
            rank1 = "[green]@1" if hit1 else ""
            console.print(
                f"  oracle={status}{rank1} | rot={rot:.0f} | "
                f"tokens={metrics.exvisit_tokens} | "
                f"files={blast_files[:3]}"
            )

        except Exception as e:
            metrics.error = str(e)
            metrics.exvisit_time_s = time.time() - t0
            console.print(f"  [red]ERROR: {e}[/red]")

        all_metrics.append(metrics)

        # Incremental save
        incremental = output_dir / "case_results_incremental.json"
        incremental.parent.mkdir(parents=True, exist_ok=True)
        with open(incremental, "w", encoding="utf-8") as f:
            json.dump([m.to_dict() for m in all_metrics], f, indent=2)

    return all_metrics


# ---------------------------------------------------------------------------
# Full benchmark (agent + evaluation)
# ---------------------------------------------------------------------------

async def run_full_benchmark(
    instances: list[SWEBenchInstance],
    cache_dir: Path,
    workspace_root: Path,
    output_dir: Path,
    model: str,
    api_key: str,
    base_url: str | None,
    max_iterations: int,
    timeout: int,
    run_eval: bool = True,
) -> list[CaseMetrics]:
    """Run full benchmark with OpenHands agent + optional evaluation."""
    from .harness import run_batch
    from .evaluate import evaluate_case

    resume_path = output_dir / "case_results_incremental.json"

    all_metrics = await run_batch(
        instances=instances,
        cache_dir=cache_dir,
        workspace_root=workspace_root,
        llm_model=model,
        llm_api_key=api_key,
        llm_base_url=base_url,
        max_iterations=max_iterations,
        use_exvisit=True,
        timeout=timeout,
        resume_from=resume_path,
    )

    # Phase 3: Evaluate patches
    if run_eval:
        console.print("\n[bold]Phase 3: Evaluating patches...[/bold]")
        for i, (inst, metrics) in enumerate(zip(instances, all_metrics)):
            if not metrics.patch_generated:
                continue
            console.print(f"  Evaluating [{i+1}/{len(instances)}] {inst.instance_id}")
            case_id = inst.instance_id.replace("/", "__")
            workspace = workspace_root / case_id
            metrics = evaluate_case(inst, metrics, workspace)
            pass_status = "[green]PASS" if metrics.pass_at_1 else "[red]FAIL"
            console.print(f"    {pass_status}")

    return all_metrics


# ---------------------------------------------------------------------------
# Display results
# ---------------------------------------------------------------------------

def display_results(
    case_metrics: list[CaseMetrics],
    summary: BenchmarkSummary,
):
    """Display results in a rich table."""
    console.print()
    console.print(format_summary_table(summary))
    console.print()

    # Detailed table
    table = Table(title="Per-Case Results", show_lines=True)
    table.add_column("Instance", style="cyan", max_width=40)
    table.add_column("Oracle Hit", justify="center")
    table.add_column("Hit@1", justify="center")
    table.add_column("Rot", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Patch", justify="center")
    table.add_column("Pass@1", justify="center")
    table.add_column("Error", max_width=30)

    for m in case_metrics:
        table.add_row(
            m.instance_id.split("__")[-1] if "__" in m.instance_id else m.instance_id,
            "[green]YES" if m.oracle_hit else "[red]NO",
            "[green]YES" if m.oracle_hit_1 else "[red]NO",
            f"{m.context_rot_index:.0f}" if m.context_rot_index < float("inf") else "∞",
            str(m.exvisit_tokens),
            "[green]YES" if m.patch_generated else "[red]NO",
            "[green]PASS" if m.pass_at_1 else "[red]FAIL",
            (m.error or "")[:30],
        )

    console.print(table)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="SWE-bench Lite + OpenHands + ExVisit MCP benchmark"
    )
    parser.add_argument(
        "--mode", choices=["smoke", "full", "nav-only"],
        default="smoke",
        help="Benchmark mode: smoke (5 cases), full (300), nav-only (no agent)"
    )
    parser.add_argument("--limit", type=int, default=None,
                        help="Max number of cases (overrides mode default)")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"LLM model (default: {DEFAULT_MODEL})")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--workspace-root", type=Path, default=DEFAULT_WORKSPACE_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--no-eval", action="store_true",
                        help="Skip pass@1 evaluation")
    parser.add_argument("--repo", default="django/django",
                        help="Repository to filter cases for")

    args = parser.parse_args()

    # Determine limit
    if args.limit is not None:
        limit = args.limit
    elif args.mode == "smoke":
        limit = 5
    elif args.mode == "full":
        limit = 300
    else:
        limit = None

    console.print(f"[bold]ExVisit MCP SWE-Bench Lite Benchmark[/bold]")
    console.print(f"  Mode: {args.mode}")
    console.print(f"  Repo: {args.repo}")
    console.print(f"  Limit: {limit or 'all'}")
    console.print(f"  Model: {args.model}")
    console.print()

    # Load dataset
    console.print("[bold]Loading SWE-bench Lite dataset...[/bold]")
    from .dataset import load_swebench_lite
    instances = load_swebench_lite(repo_filter=args.repo, limit=limit)
    console.print(f"  Loaded {len(instances)} instances")

    # Create output dir with timestamp
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = args.output_dir / f"{args.mode}_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Save run config
    run_config = {
        "mode": args.mode,
        "limit": limit,
        "model": args.model,
        "repo": args.repo,
        "max_iterations": args.max_iterations,
        "timeout": args.timeout,
        "num_instances": len(instances),
        "timestamp": timestamp,
    }
    with open(output_dir / "run_config.json", "w") as f:
        json.dump(run_config, f, indent=2)

    t_start = time.time()

    if args.mode == "nav-only":
        all_metrics = run_navigation_only(
            instances, args.cache_dir, args.workspace_root, output_dir
        )
    else:
        try:
            api_key = get_api_key()
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            console.print("[yellow]Falling back to nav-only mode[/yellow]")
            all_metrics = run_navigation_only(
                instances, args.cache_dir, args.workspace_root, output_dir
            )
        else:
            model = normalize_model_name(args.model)
            base_url = get_base_url(model)
            all_metrics = asyncio.run(run_full_benchmark(
                instances=instances,
                cache_dir=args.cache_dir,
                workspace_root=args.workspace_root,
                output_dir=output_dir,
                model=model,
                api_key=api_key,
                base_url=base_url,
                max_iterations=args.max_iterations,
                timeout=args.timeout,
                run_eval=not args.no_eval,
            ))

    elapsed = time.time() - t_start

    # Compute and display summary
    summary = compute_summary(all_metrics)
    display_results(all_metrics, summary)
    save_results(all_metrics, summary, output_dir)

    console.print(f"\n[bold]Total time: {elapsed:.1f}s[/bold]")
    console.print(f"Results saved to: {output_dir}")

    return 0 if summary.completed_cases > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
