#!/usr/bin/env python3
"""exvisit model runner for a single SWE-bench Lite case.

Invoked by run_sonnet_exvisit.sh (or directly). Drives a tool-use loop in which
the model navigates the repository exvisit spatially (no grep/find/rg) and
applies surgical edits via exvisit_edit.

Output (to stdout): a single JSON line consumed by swebench_lite_harness.py.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

_DEFAULT_MODEL = os.environ.get("exvisit_MODEL", "claude-sonnet-4-5")
_DEFAULT_MAX_STEPS = int(os.environ.get("exvisit_MAX_STEPS", "20"))
_DEFAULT_TRAJ_DIR = Path(os.environ.get("exvisit_TRAJ_DIR", "/tmp/exvisit-trajs"))
_FILE_READ_LINE_LIMIT = 300

_ANTHROPIC_PRICING: Dict[str, float] = {
    "input_base_per_1m": 3.00,
    "cache_write_per_1m": 3.75,
    "cache_read_per_1m": 0.30,
    "output_per_1m": 15.00,
}

_ZERO_PRICING: Dict[str, float] = {
    "input_base_per_1m": 0.0,
    "cache_write_per_1m": 0.0,
    "cache_read_per_1m": 0.0,
    "output_per_1m": 0.0,
}


exvisit_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "exvisit_query",
        "description": (
            "Navigate the repository exvisit spatially. Returns the subgraph of "
            "nodes and edges reachable from a target FQN within hops steps. "
            "Use this to discover callers, callees, and sibling nodes. "
            "Never use grep, find, or rg."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Fully-qualified node name to start from."},
                "hops": {"type": "integer", "description": "Graph hops to expand", "default": 2},
                "direction": {
                    "type": "string",
                    "enum": ["in", "out", "both"],
                    "description": "Edge direction to follow",
                    "default": "both",
                },
            },
            "required": ["target"],
        },
    },
    {
        "name": "exvisit_blast",
        "description": "Retrieve the most relevant code snippets for the issue text using exvisit blast.",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_text": {"type": "string", "description": "Issue description"},
                "preset": {"type": "string", "description": "Blast preset name", "default": "issue-fix"},
            },
            "required": ["issue_text"],
        },
    },
    {
        "name": "exvisit_edit",
        "description": "Apply a surgical AST-bounded edit to a Python file inside one locator span.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Repo-relative file path"},
                "locator": {"type": "string", "description": "Enclosing function or class FQN"},
                "old": {"type": "string", "description": "Exact literal text to replace"},
                "new": {"type": "string", "description": "Replacement text"},
            },
            "required": ["file", "locator", "old", "new"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a slice of a source file from the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repo-relative file path"},
                "start_line": {"type": "integer", "description": "1-based first line", "default": 1},
                "end_line": {"type": "integer", "description": "1-based last line", "default": _FILE_READ_LINE_LIMIT},
            },
            "required": ["path"],
        },
    },
    {
        "name": "run_tests",
        "description": "Run pytest inside the workspace to verify the fix.",
        "input_schema": {
            "type": "object",
            "properties": {
                "test_path": {"type": "string", "description": "Optional specific test file or directory", "default": ""},
                "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 120},
            },
        },
    },
]


def detect_provider(model: str) -> str:
    if model.strip().lower().startswith("gemini"):
        return "gemini"
    return "anthropic"


def get_api_key_for_provider(provider: str) -> str:
    if provider == "gemini":
        return os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY") or ""
    if provider == "anthropic":
        return os.environ.get("ANTHROPIC_API_KEY", "")
    return ""


def load_pricing(pricing_file: Optional[Path], provider: str) -> Dict[str, float]:
    pricing = dict(_ANTHROPIC_PRICING if provider == "anthropic" else _ZERO_PRICING)
    if pricing_file and pricing_file.exists():
        try:
            overrides = json.loads(pricing_file.read_text(encoding="utf-8"))
            pricing.update({k: float(v) for k, v in overrides.items() if k in pricing})
        except Exception:
            pass
    return pricing


def compute_cost(usage: Dict[str, int], pricing: Dict[str, float]) -> float:
    return (
        usage.get("input_tokens", 0) * pricing["input_base_per_1m"]
        + usage.get("cache_creation_input_tokens", 0) * pricing["cache_write_per_1m"]
        + usage.get("cache_read_input_tokens", 0) * pricing["cache_read_per_1m"]
        + usage.get("output_tokens", 0) * pricing["output_per_1m"]
    ) / 1_000_000.0


def _exvisit_env() -> Dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    return env


def _run(cmd: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, env=_exvisit_env(), **kwargs)


def _as_plain_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    for method_name in ("model_dump", "to_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            dumped = method()
            if isinstance(dumped, dict):
                return dumped
    try:
        return dict(value)
    except Exception:
        return {}


def _candidate_parts(response: Any) -> List[Any]:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return []
    content = getattr(candidates[0], "content", None)
    return list(getattr(content, "parts", None) or [])


def _gemini_usage_from_response(response: Any) -> Dict[str, int]:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        usage = getattr(response, "usageMetadata", None)
    if usage is None:
        return {
            "input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 0,
        }
    return {
        "input_tokens": int(getattr(usage, "prompt_token_count", 0) or getattr(usage, "promptTokenCount", 0) or 0),
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": int(getattr(usage, "cached_content_token_count", 0) or getattr(usage, "cachedContentTokenCount", 0) or 0),
        "output_tokens": int(getattr(usage, "candidates_token_count", 0) or getattr(usage, "candidatesTokenCount", 0) or 0),
    }


def exec_exvisit_query(exvisit_path: Path, target: str, hops: int = 2, direction: str = "both") -> str:
    result = _run(
        [
            sys.executable,
            "-m",
            "exvisit",
            "query",
            str(exvisit_path),
            "--target",
            target,
            "--neighbors",
            str(hops),
            "--direction",
            direction,
        ]
    )
    out = result.stdout
    if result.stderr.strip():
        out += f"\n[stderr]: {result.stderr.strip()}"
    return out.strip() or f"[exvisit_query: no output for target '{target}']"


def exec_exvisit_blast(exvisit_path: Path, repo_path: Path, issue_text: str, preset: str = "issue-fix") -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as temp_file:
        temp_file.write(issue_text)
        issue_file = temp_file.name
    try:
        result = _run(
            [
                sys.executable,
                "-m",
                "exvisit",
                "blast",
                str(exvisit_path),
                "--repo",
                str(repo_path),
                "--issue-file",
                issue_file,
                "--preset",
                preset,
                "--format",
                "md",
            ]
        )
        out = result.stdout
        if result.stderr.strip():
            out += f"\n[stderr]: {result.stderr.strip()}"
        return out.strip() or "[exvisit_blast: no output]"
    finally:
        Path(issue_file).unlink(missing_ok=True)


def exec_exvisit_edit(workspace: Path, file_rel: str, locator: str, old_text: str, new_text: str) -> str:
    abs_file = (workspace / file_rel).resolve()
    try:
        abs_file.relative_to(workspace.resolve())
    except ValueError:
        return "[error] path traversal rejected"
    if not abs_file.exists():
        return f"[error] file not found in workspace: {file_rel}"
    result = _run(
        [
            sys.executable,
            "-m",
            "exvisit.edit_tool",
            "--file",
            str(abs_file),
            "--locator",
            locator,
            "--old",
            old_text,
            "--new",
            new_text,
            "--format",
            "json",
        ]
    )
    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip()
        return f"[exvisit_edit error] {err}"
    return result.stdout.strip() or "[exvisit_edit applied successfully]"


def exec_read_file(workspace: Path, path_rel: str, start_line: int = 1, end_line: int = _FILE_READ_LINE_LIMIT) -> str:
    abs_path = (workspace / path_rel).resolve()
    try:
        abs_path.relative_to(workspace.resolve())
    except ValueError:
        return "[error] path traversal rejected"
    if not abs_path.exists():
        return f"[error] file not found: {path_rel}"
    try:
        text = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"[error] could not read file: {exc}"
    lines = text.splitlines()
    start_idx = max(0, start_line - 1)
    end_idx = min(len(lines), end_line)
    header = f"# {path_rel} (lines {start_idx + 1}-{end_idx} of {len(lines)})\n"
    return header + "\n".join(lines[start_idx:end_idx])


def exec_run_tests(workspace: Path, test_path: str = "", timeout: int = 120) -> Tuple[int, str]:
    cmd = [sys.executable, "-m", "pytest", "--tb=short", "-q", "--no-header"]
    if test_path.strip():
        cmd.append(test_path.strip())
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=workspace,
            timeout=timeout,
            env=_exvisit_env(),
        )
        output = (result.stdout + "\n" + result.stderr).strip()
        return result.returncode, output[-6000:]
    except subprocess.TimeoutExpired:
        return 1, f"[error] test run timed out after {timeout}s"
    except Exception as exc:
        return 1, f"[error] could not run tests: {exc}"


def _system_prompt(exvisit_path: Path, workspace: Path, issue_text: str) -> str:
    return (
        "You are a software engineer resolving a bug in a Python repository.\n\n"
        f"WORKSPACE: {workspace}\n"
        f"exvisit MAP: {exvisit_path}\n\n"
        "STRICT NAVIGATION RULES:\n"
        "1. Do not use grep, find, or rg.\n"
        "2. Start every investigation with exvisit_blast.\n"
        "3. Use exvisit_query to explore graph neighbors.\n"
        "4. Use read_file to inspect specific source.\n"
        "5. Use exvisit_edit for all code changes.\n"
        "6. After each edit, call run_tests.\n"
        "7. Stop once run_tests returns exit_code 0.\n\n"
        "ISSUE TO RESOLVE:\n"
        f"{issue_text}"
    )


def _execute_tool_call(
    tool_name: str,
    tool_input: Dict[str, Any],
    exvisit_path: Path,
    workspace: Path,
    repo_path: Path,
) -> Tuple[str, bool]:
    result_text = "[unknown tool]"
    tests_passed = False
    if tool_name == "exvisit_query":
        result_text = exec_exvisit_query(
            exvisit_path,
            target=tool_input["target"],
            hops=int(tool_input.get("hops", 2)),
            direction=str(tool_input.get("direction", "both")),
        )
    elif tool_name == "exvisit_blast":
        result_text = exec_exvisit_blast(
            exvisit_path,
            repo_path,
            issue_text=tool_input["issue_text"],
            preset=str(tool_input.get("preset", "issue-fix")),
        )
    elif tool_name == "exvisit_edit":
        result_text = exec_exvisit_edit(
            workspace,
            file_rel=tool_input["file"],
            locator=tool_input["locator"],
            old_text=tool_input["old"],
            new_text=tool_input["new"],
        )
    elif tool_name == "read_file":
        result_text = exec_read_file(
            workspace,
            path_rel=tool_input["path"],
            start_line=int(tool_input.get("start_line", 1)),
            end_line=int(tool_input.get("end_line", _FILE_READ_LINE_LIMIT)),
        )
    elif tool_name == "run_tests":
        exit_code, result_text = exec_run_tests(
            workspace,
            test_path=str(tool_input.get("test_path", "")),
            timeout=int(tool_input.get("timeout", 120)),
        )
        tests_passed = exit_code == 0
    return result_text, tests_passed


def _run_anthropic_loop(
    client: Any,
    exvisit_path: Path,
    workspace: Path,
    repo_path: Path,
    issue_text: str,
    case_id: str,
    model: str,
    max_steps: int,
) -> Dict[str, Any]:
    system = _system_prompt(exvisit_path, workspace, issue_text)
    messages: List[Dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                f"Please resolve the issue for benchmark case `{case_id}`. "
                "Begin with exvisit_blast using the full issue text."
            ),
        }
    ]
    usage_accum: Dict[str, int] = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
    }
    turns: List[Dict[str, Any]] = []
    pass_at_1 = False
    step = 0

    while step < max_steps:
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=system,
            messages=messages,
            tools=exvisit_TOOLS,
        )
        usage = response.usage
        usage_accum["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
        usage_accum["cache_creation_input_tokens"] += getattr(usage, "cache_creation_input_tokens", 0) or 0
        usage_accum["cache_read_input_tokens"] += getattr(usage, "cache_read_input_tokens", 0) or 0
        usage_accum["output_tokens"] += getattr(usage, "output_tokens", 0) or 0

        turn: Dict[str, Any] = {"step": step, "stop_reason": response.stop_reason, "tool_calls": []}
        messages.append({"role": "assistant", "content": response.content})
        step += 1

        if response.stop_reason != "tool_use":
            turns.append(turn)
            break

        tool_results: List[Dict[str, Any]] = []
        for block in response.content:
            if not hasattr(block, "type") or block.type != "tool_use":
                continue
            tool_input = block.input or {}
            result_text, tests_passed = _execute_tool_call(block.name, tool_input, exvisit_path, workspace, repo_path)
            safe_input = {k: v for k, v in tool_input.items() if k not in ("old", "new")}
            turn["tool_calls"].append({"tool": block.name, "input": safe_input, "result_preview": result_text[:400]})
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result_text})
            pass_at_1 = pass_at_1 or tests_passed

        messages.append({"role": "user", "content": tool_results})
        turns.append(turn)
        if pass_at_1:
            break

    return {
        "provider": "anthropic",
        "case_id": case_id,
        "model": model,
        "steps": step,
        "pass_at_1": pass_at_1,
        "turns": turns,
        "usage_metadata": usage_accum,
    }


def _run_gemini_loop(
    client: Any,
    exvisit_path: Path,
    workspace: Path,
    repo_path: Path,
    issue_text: str,
    case_id: str,
    model: str,
    max_steps: int,
) -> Dict[str, Any]:
    from google.genai import types

    system = _system_prompt(exvisit_path, workspace, issue_text)
    tool_decls = [
        types.FunctionDeclaration(
            name=tool["name"],
            description=tool["description"],
            parametersJsonSchema=tool["input_schema"],
        )
        for tool in exvisit_TOOLS
    ]
    config = types.GenerateContentConfig(
        systemInstruction=system,
        maxOutputTokens=4096,
        tools=[types.Tool(functionDeclarations=tool_decls)],
        automaticFunctionCalling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    contents: List[Any] = [
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(
                    text=(
                        f"Please resolve the issue for benchmark case `{case_id}`. "
                        "Begin with exvisit_blast using the full issue text."
                    )
                )
            ],
        )
    ]
    usage_accum: Dict[str, int] = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
    }
    turns: List[Dict[str, Any]] = []
    pass_at_1 = False
    step = 0

    while step < max_steps:
        response = client.models.generate_content(model=model, contents=contents, config=config)
        response_usage = _gemini_usage_from_response(response)
        for key, value in response_usage.items():
            usage_accum[key] += value

        parts = _candidate_parts(response)
        function_calls = []
        text_parts = []
        for part in parts:
            function_call = getattr(part, "function_call", None)
            if function_call is not None:
                function_calls.append(function_call)
            text = getattr(part, "text", None)
            if text:
                text_parts.append(text)

        turn: Dict[str, Any] = {
            "step": step,
            "stop_reason": "tool_use" if function_calls else "stop",
            "assistant_text": "\n".join(text_parts)[:800],
            "tool_calls": [],
        }
        step += 1

        if not function_calls:
            turns.append(turn)
            break

        contents.append(getattr(response.candidates[0], "content", types.Content(role="model", parts=[])))
        function_responses = []
        for function_call in function_calls:
            tool_name = getattr(function_call, "name", "")
            tool_input = _as_plain_dict(getattr(function_call, "args", {}))
            result_text, tests_passed = _execute_tool_call(tool_name, tool_input, exvisit_path, workspace, repo_path)
            safe_input = {k: v for k, v in tool_input.items() if k not in ("old", "new")}
            turn["tool_calls"].append({"tool": tool_name, "input": safe_input, "result_preview": result_text[:400]})
            function_responses.append(types.Part.from_function_response(name=tool_name, response={"result": result_text}))
            pass_at_1 = pass_at_1 or tests_passed

        contents.append(types.Content(role="user", parts=function_responses))
        turns.append(turn)
        if pass_at_1:
            break

    return {
        "provider": "gemini",
        "case_id": case_id,
        "model": model,
        "steps": step,
        "pass_at_1": pass_at_1,
        "turns": turns,
        "usage_metadata": usage_accum,
    }


def run_agent_loop(
    provider: str,
    client: Any,
    exvisit_path: Path,
    workspace: Path,
    repo_path: Path,
    issue_text: str,
    case_id: str,
    model: str,
    max_steps: int,
) -> Dict[str, Any]:
    if provider == "gemini":
        return _run_gemini_loop(client, exvisit_path, workspace, repo_path, issue_text, case_id, model, max_steps)
    return _run_anthropic_loop(client, exvisit_path, workspace, repo_path, issue_text, case_id, model, max_steps)


def load_issue_from_manifest(manifest_path: Path, case_id: str) -> Optional[str]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        for case in payload.get("cases", []):
            if case.get("case_id") == case_id:
                return case.get("issue_text") or None
    except Exception:
        pass
    return None


def _extract_issue_from_claude_md(claude_md: Path) -> Optional[str]:
    try:
        raw = claude_md.read_text(encoding="utf-8")
    except OSError:
        return None
    marker = "```text\n"
    start = raw.find(marker)
    if start == -1:
        return None
    end = raw.find("\n```", start + len(marker))
    if end == -1:
        return None
    return raw[start + len(marker):end].strip() or None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="runner_agent", description="exvisit model runner for a single SWE-bench Lite case.")
    parser.add_argument("--repo-path", required=True)
    parser.add_argument("--exvisit-path", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--manifest", default=os.environ.get("exvisit_MANIFEST"))
    parser.add_argument("--issue-text", default=None)
    parser.add_argument("--model", default=_DEFAULT_MODEL)
    parser.add_argument("--max-steps", type=int, default=_DEFAULT_MAX_STEPS)
    parser.add_argument("--traj-dir", default=str(_DEFAULT_TRAJ_DIR))
    parser.add_argument("--pricing-file", default=None)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    repo_path = Path(args.repo_path).resolve()
    exvisit_path = Path(args.exv_path).resolve()
    workspace = Path(args.workspace).resolve() if args.workspace else repo_path
    traj_dir = Path(args.traj_dir)
    pricing_file = Path(args.pricing_file) if args.pricing_file else None

    def _fatal(message: str) -> int:
        print(json.dumps({"error": message, "pass_at_1": False, "case_id": args.case_id}))
        return 2

    if not repo_path.exists():
        return _fatal(f"repo_path does not exist: {repo_path}")
    if not exvisit_path.exists():
        return _fatal(f"exvisit_path does not exist: {exvisit_path}")
    if not workspace.exists():
        return _fatal(f"workspace does not exist: {workspace}")

    issue_text: Optional[str] = args.issue_text
    if not issue_text and args.manifest:
        issue_text = load_issue_from_manifest(Path(args.manifest), args.case_id)
    if not issue_text:
        issue_text = _extract_issue_from_claude_md(workspace / "CLAUDE.md")
    if not issue_text:
        return _fatal("Could not determine issue_text. Pass --manifest or --issue-text.")

    provider = detect_provider(args.model)
    api_key = get_api_key_for_provider(provider)
    if not api_key:
        expected = "GOOGLE_API_KEY or GEMINI_API_KEY" if provider == "gemini" else "ANTHROPIC_API_KEY"
        return _fatal(f"{expected} environment variable is not set")

    try:
        if provider == "gemini":
            from google import genai

            client = genai.Client(api_key=api_key)
        else:
            import anthropic  # type: ignore

            client = anthropic.Anthropic(api_key=api_key)
    except ImportError as exc:
        return _fatal(f"required provider package is not installed: {exc}")

    pricing = load_pricing(pricing_file, provider)
    traj_dir.mkdir(parents=True, exist_ok=True)
    traj_path = traj_dir / f"{args.case_id}.trajectory.json"
    trajectory = run_agent_loop(
        provider=provider,
        client=client,
        exvisit_path=exvisit_path,
        workspace=workspace,
        repo_path=repo_path,
        issue_text=issue_text,
        case_id=args.case_id,
        model=args.model,
        max_steps=args.max_steps,
    )
    cost = compute_cost(trajectory["usage_metadata"], pricing)
    trajectory["cost_to_resolve_usd"] = cost
    trajectory["pricing"] = pricing
    traj_path.write_text(json.dumps(trajectory, indent=2, default=str), encoding="utf-8")

    result: Dict[str, Any] = {
        "provider": provider,
        "pass_at_1": trajectory["pass_at_1"],
        "trajectory_path": str(traj_path),
        "case_id": args.case_id,
        "steps": trajectory["steps"],
        "cost_to_resolve_usd": cost,
        "usage_metadata": trajectory["usage_metadata"],
    }
    print(json.dumps(result))
    return 0 if trajectory["pass_at_1"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
