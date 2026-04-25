"""OpenHands + ExVisit MCP harness for SWE-bench Lite.

Runs OpenHands CodeActAgent in Docker with ExVisit navigation context.
The exvisit-mcp binary and exvisit Python package are mounted/installed inside
the container so the agent can use them as tools during its session.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from .dataset import SWEBenchInstance
from .metrics import CaseMetrics, compute_navigation_metrics, count_tokens
from .prepare import prepare_case

console = Console()

# ---------------------------------------------------------------------------
# ExVisit-enhanced system prompt injected into the agent
# ---------------------------------------------------------------------------

EXVISIT_SYSTEM_PROMPT = """\
You are an expert software engineer fixing a bug in a Python repository at /workspace.
You have access to the **ExVisit MCP structural code navigation server** — a powerful set of tools for precise code analysis. USE THESE TOOLS. They give you deep structural understanding of the codebase.

## Pre-computed Navigation Context

The ExVisit blast analysis has ALREADY identified the most relevant files for this issue.
These files are your starting point — read them carefully:

{blast_context}

## ExVisit MCP Tools — USE THESE

You MUST use the ExVisit MCP tools for navigation. They are available as function calls:

1. **exv_blast** — Find relevant files/symbols for a query. Use when you need to search for related code.
2. **exv_locate** — Multi-signal anchor scoring. Use to pinpoint exact locations in large files.
3. **exv_expand** — Expand neighborhood around a symbol. Use to understand what a class/function connects to.
4. **exv_query** — Topological slice (callers, callees, dependencies). Use to trace data/control flow.
5. **exv_deps** — Outbound dependencies of a symbol. Use to find what a symbol depends on.
6. **exv_callers** — Inbound callers of a symbol. Use to find what calls/uses a symbol.
7. **exv_anchor** — Resolve stacktraces/error messages to code locations.
8. **exv_verify** — Verify structural consistency of the .exv map.

The .exv structural map is at `/workspace/project.exv` — all tools use this file.

## Workflow — FOLLOW THIS ORDER

1. **READ** the blast context above — these are the most relevant files
2. **READ** the identified files using `cat` or `head` to understand the code
3. **USE exv_expand/exv_deps/exv_callers** to trace relationships if needed
4. **UNDERSTAND** the root cause of the bug
5. **MAKE** the minimal fix using file editing
6. **VERIFY** your fix by re-reading the modified code
7. **DO NOT** modify test files
8. **DO NOT** refactor unrelated code
"""

CONTROL_SYSTEM_PROMPT = """\
You are a software engineer working on fixing a bug in a Python repository.
The repository is available at /workspace.

## Strategy

1. Understand the issue from the problem statement
2. Search the codebase to find relevant files
3. Read the relevant code sections
4. Make the minimal fix
5. Verify your fix addresses the issue
"""


def build_agent_instruction(
    instance: SWEBenchInstance,
    blast_md: str,
    use_exvisit: bool = True,
) -> str:
    """Build the full instruction for the OpenHands agent."""
    issue = instance.problem_statement.strip()

    if use_exvisit:
        system = EXVISIT_SYSTEM_PROMPT.format(blast_context=blast_md)
    else:
        system = CONTROL_SYSTEM_PROMPT

    instruction = f"""{system}

## Issue to Fix

{issue}

## Instructions

1. Read the blast context above — the relevant files are already identified
2. Use the ExVisit MCP tools (exv_expand, exv_deps, exv_callers) to deeply understand the code structure
3. Fix the issue in the repository at /workspace — make minimal changes only
4. Do NOT modify any test files
5. After making changes, re-read the modified code to verify correctness
6. The fix must address the root cause, not just symptoms
7. If unsure about relationships, use exv_query or exv_callers to trace the flow
"""
    return instruction


def estimate_control_tokens(instance: SWEBenchInstance) -> int:
    """Estimate control baseline token count (grep-based navigation).

    Based on benchmark.md data: avg 129,652 tokens for Django cases.
    For individual estimation, use issue text length as proxy.
    """
    base = 80_000  # minimum for django repo navigation
    issue_factor = len(instance.problem_statement) * 5  # more complex issues = more exploration
    return min(base + issue_factor, 200_000)


async def run_single_case(
    instance: SWEBenchInstance,
    cache_dir: Path,
    workspace_root: Path,
    llm_model: str,
    llm_api_key: str,
    llm_base_url: Optional[str] = None,
    max_iterations: int = 30,
    use_exvisit: bool = True,
    timeout: int = 600,
) -> CaseMetrics:
    """Run a single SWE-bench case with OpenHands + ExVisit.

    Returns CaseMetrics with all measurements.
    """
    metrics = CaseMetrics(instance_id=instance.instance_id)
    metrics.oracle_files = instance.oracle_files

    # Phase 1: ExVisit navigation
    t0 = time.time()
    try:
        prep = prepare_case(instance, cache_dir, workspace_root)
        workspace = prep["workspace"]
        blast_bundle = prep["blast_bundle"]
        blast_md = prep["blast_md"]

        # Extract blast files list
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
        metrics.exvisit_tokens = count_tokens(blast_md if isinstance(blast_md, str) else str(blast_md))
        metrics.control_tokens = estimate_control_tokens(instance)
        metrics.exvisit_time_s = time.time() - t0

        # Compute navigation metrics
        hit, hit1, rot = compute_navigation_metrics(blast_files, instance.oracle_files)
        metrics.oracle_hit = hit
        metrics.oracle_hit_1 = hit1
        metrics.context_rot_index = rot

    except Exception as e:
        metrics.error = f"ExVisit navigation failed: {e}"
        metrics.exvisit_time_s = time.time() - t0
        return metrics

    # Phase 2: Run OpenHands agent
    t1 = time.time()
    try:
        instruction = build_agent_instruction(instance, blast_md, use_exvisit=use_exvisit)

        from openhands.core.config.utils import load_openhands_config
        from openhands.core.config import LLMConfig
        from openhands.core.main import run_controller
        from openhands.events.action import MessageAction

        config = load_openhands_config()
        config.default_agent = "CodeActAgent"
        config.runtime = "docker"
        config.max_iterations = max_iterations
        config.max_budget_per_task = 4.0
        config.sandbox.timeout = timeout
        config.sandbox.base_container_image = "python:3.12-slim"
        config.sandbox.keep_runtime_alive = False
        config.sandbox.enable_auto_lint = False
        config.sandbox.initialize_plugins = False
        config.workspace_base = str(workspace)
        config.workspace_mount_path = str(workspace)

        # Mount the exvisit-mcp binary into the container
        exvisit_mcp_bin = Path(__file__).parent.parent / "rust" / "target" / "release" / "exvisit-mcp"
        if exvisit_mcp_bin.exists():
            config.sandbox.volumes = f"{exvisit_mcp_bin}:/usr/local/bin/exvisit-mcp:ro"

        config.set_llm_config(LLMConfig(
            model=llm_model,
            api_key=llm_api_key,
            base_url=llm_base_url,
            temperature=0.0,
            max_output_tokens=4096,
            num_retries=5,
            retry_min_wait=15,
            retry_max_wait=120,
            native_tool_calling=True,
        ))

        # Enable ExVisit MCP server for full tool-use
        # MCP stdio servers run INSIDE the Docker container, so use the
        # container path (volume-mounted from host binary).
        if use_exvisit:
            from openhands.core.config.mcp_config import MCPConfig, MCPStdioServerConfig
            exvisit_mcp_bin = Path(__file__).parent.parent / "rust" / "target" / "release" / "exvisit-mcp"
            if exvisit_mcp_bin.exists():
                config.mcp = MCPConfig(
                    stdio_servers=[MCPStdioServerConfig(
                        name="exvisit",
                        command="/usr/local/bin/exvisit-mcp",
                        args=[],
                        env={},
                    )]
                )

        action = MessageAction(content=instruction)

        def auto_respond(state) -> str:
            return (
                "Please continue working on the task. If you need help, "
                "re-read the issue and the ExVisit blast context above. "
                "Focus on making the minimal fix."
            )

        state = await run_controller(
            config=config,
            initial_user_action=action,
            exit_on_message=False,
            headless_mode=True,
            fake_user_response_fn=auto_respond,
        )

        metrics.agent_time_s = time.time() - t1

        if state:
            # Extract metrics from state
            if state.metrics:
                m = state.metrics.get()
                usage = m.get("accumulated_token_usage", {})
                metrics.total_prompt_tokens = usage.get("prompt_tokens", 0)
                metrics.total_completion_tokens = usage.get("completion_tokens", 0)
                # Count tool calls from history
                from openhands.events.action import CmdRunAction
                metrics.total_tool_calls = sum(
                    1 for e in state.history
                    if isinstance(e, CmdRunAction)
                )

            # Extract patch via git diff in the workspace
            import subprocess
            diff_result = subprocess.run(
                ["git", "diff", instance.base_commit],
                cwd=str(workspace),
                capture_output=True,
                text=True,
            )
            if diff_result.returncode == 0 and diff_result.stdout.strip():
                metrics.patch_generated = True
                patch_path = workspace / "generated.patch"
                patch_path.write_text(diff_result.stdout, encoding="utf-8")
            else:
                # Also check staged changes
                diff_result = subprocess.run(
                    ["git", "diff", "--cached", instance.base_commit],
                    cwd=str(workspace),
                    capture_output=True,
                    text=True,
                )
                if diff_result.returncode == 0 and diff_result.stdout.strip():
                    metrics.patch_generated = True
                    patch_path = workspace / "generated.patch"
                    patch_path.write_text(diff_result.stdout, encoding="utf-8")

    except Exception as e:
        metrics.error = f"Agent execution failed: {traceback.format_exc()}"
        metrics.agent_time_s = time.time() - t1
        return metrics

    return metrics


async def run_batch(
    instances: list[SWEBenchInstance],
    cache_dir: Path,
    workspace_root: Path,
    llm_model: str,
    llm_api_key: str,
    llm_base_url: Optional[str] = None,
    max_iterations: int = 30,
    use_exvisit: bool = True,
    timeout: int = 600,
    resume_from: Optional[Path] = None,
) -> list[CaseMetrics]:
    """Run a batch of SWE-bench cases sequentially."""

    # Load existing results for resume
    existing_results: dict[str, CaseMetrics] = {}
    if resume_from and resume_from.exists():
        with open(resume_from, "r") as f:
            for item in json.load(f):
                m = CaseMetrics(**{k: v for k, v in item.items()
                                  if k in CaseMetrics.__dataclass_fields__})
                existing_results[m.instance_id] = m

    all_metrics: list[CaseMetrics] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Running cases", total=len(instances))

        for i, instance in enumerate(instances):
            progress.update(task, description=f"[{i+1}/{len(instances)}] {instance.instance_id}")

            # Skip if already completed
            if instance.instance_id in existing_results:
                existing = existing_results[instance.instance_id]
                if existing.error is None:
                    console.print(f"  [dim]Skipping (cached): {instance.instance_id}[/dim]")
                    all_metrics.append(existing)
                    progress.advance(task)
                    continue

            try:
                m = await run_single_case(
                    instance=instance,
                    cache_dir=cache_dir,
                    workspace_root=workspace_root,
                    llm_model=llm_model,
                    llm_api_key=llm_api_key,
                    llm_base_url=llm_base_url,
                    max_iterations=max_iterations,
                    use_exvisit=use_exvisit,
                    timeout=timeout,
                )
                all_metrics.append(m)

                # Status
                status = "[green]OK" if m.error is None else f"[red]ERR: {m.error[:60]}"
                hit = "[green]HIT" if m.oracle_hit else "[red]MISS"
                console.print(
                    f"  {instance.instance_id}: {status} | "
                    f"oracle={hit} | tokens={m.exvisit_tokens} | "
                    f"patch={'YES' if m.patch_generated else 'NO'}"
                )

            except Exception as e:
                err_metrics = CaseMetrics(instance_id=instance.instance_id)
                err_metrics.error = str(e)
                all_metrics.append(err_metrics)
                console.print(f"  [red]FATAL: {instance.instance_id}: {e}[/red]")

            progress.advance(task)

            # Incremental save
            if resume_from:
                save_path = resume_from
            else:
                save_path = cache_dir / "case_results_incremental.json"
            save_path.parent.mkdir(parents=True, exist_ok=True)
            with open(save_path, "w", encoding="utf-8") as f:
                json.dump([m.to_dict() for m in all_metrics], f, indent=2)

    return all_metrics
