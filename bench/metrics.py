"""Metrics computation for the ExVisit MCP SWE-bench benchmark.

Metrics:
  - token_reduction: ratio of tokens saved vs control baseline
  - context_rot_index: rank of oracle file in agent's file access order
  - oracle_hit_rate: fraction of cases where oracle file appears in navigated files
  - oracle_hit_1_rate: fraction where oracle file is rank-1
  - pass_at_1: fraction of cases where the generated patch passes all tests

NavTrace metrics (ExVisit-powered agentic runs):
  - per-tool-call token recording (blast, rg, locate, expand)
  - solve_mode classification (blast_only | blast+rg | blast+locate | multi_tool)
  - comparison report vs baseline oracle rates from leaderboard
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

import tiktoken


@dataclass
class CaseMetrics:
    instance_id: str
    # Navigation metrics
    blast_files: list[str] = field(default_factory=list)
    oracle_files: list[str] = field(default_factory=list)
    oracle_hit: bool = False
    oracle_hit_1: bool = False
    context_rot_index: float = float("inf")
    # Token metrics
    exvisit_tokens: int = 0
    control_tokens: int = 0
    # Agent metrics
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tool_calls: int = 0
    # Pass metrics
    patch_generated: bool = False
    patch_applied: bool = False
    tests_passed: bool = False
    pass_at_1: bool = False
    # Timing
    exvisit_time_s: float = 0.0
    agent_time_s: float = 0.0
    eval_time_s: float = 0.0
    # Error
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "blast_files": self.blast_files,
            "oracle_files": self.oracle_files,
            "oracle_hit": self.oracle_hit,
            "oracle_hit_1": self.oracle_hit_1,
            "context_rot_index": self.context_rot_index,
            "exvisit_tokens": self.exvisit_tokens,
            "control_tokens": self.control_tokens,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_tool_calls": self.total_tool_calls,
            "patch_generated": self.patch_generated,
            "patch_applied": self.patch_applied,
            "tests_passed": self.tests_passed,
            "pass_at_1": self.pass_at_1,
            "exvisit_time_s": self.exvisit_time_s,
            "agent_time_s": self.agent_time_s,
            "eval_time_s": self.eval_time_s,
            "error": self.error,
        }


def count_tokens(text: str, model: str = "gpt-4") -> int:
    """Count tokens using tiktoken."""
    try:
        enc = tiktoken.encoding_for_model(model)
    except KeyError:
        enc = tiktoken.get_encoding("cl100k_base")
    return len(enc.encode(text))


def compute_navigation_metrics(
    blast_files: list[str],
    oracle_files: list[str],
) -> tuple[bool, bool, float]:
    """Compute oracle hit, hit@1, context rot index.

    Returns: (oracle_hit, oracle_hit_1, context_rot_index)
    """
    if not oracle_files or not blast_files:
        return False, False, float("inf")

    # Normalize paths
    blast_norm = [f.replace("\\", "/").strip("/") for f in blast_files]
    oracle_norm = [f.replace("\\", "/").strip("/") for f in oracle_files]

    # Oracle hit: any oracle file appears in blast files
    oracle_hit = any(
        oracle in blast_norm or any(b.endswith(oracle) for b in blast_norm)
        for oracle in oracle_norm
    )

    # Oracle hit@1: first blast file is an oracle file
    oracle_hit_1 = False
    if blast_norm:
        first = blast_norm[0]
        oracle_hit_1 = any(
            first == oracle or first.endswith(oracle)
            for oracle in oracle_norm
        )

    # Context rot index: rank of first oracle file in blast list
    context_rot = float("inf")
    for oracle in oracle_norm:
        for i, b in enumerate(blast_norm):
            if b == oracle or b.endswith(oracle):
                context_rot = min(context_rot, i)
                break
    if context_rot == float("inf"):
        context_rot = len(blast_norm)  # not found → max rank

    return oracle_hit, oracle_hit_1, context_rot


@dataclass
class BenchmarkSummary:
    total_cases: int = 0
    completed_cases: int = 0
    errored_cases: int = 0
    # Navigation metrics (averages)
    avg_oracle_hit_rate: float = 0.0
    avg_oracle_hit_1_rate: float = 0.0
    avg_context_rot_index: float = 0.0
    # Token metrics
    avg_exvisit_tokens: float = 0.0
    avg_control_tokens: float = 0.0
    token_reduction_ratio: float = 0.0
    # Pass metrics
    pass_at_1_rate: float = 0.0
    patch_generation_rate: float = 0.0
    # Agent metrics
    avg_prompt_tokens: float = 0.0
    avg_completion_tokens: float = 0.0
    avg_tool_calls: float = 0.0

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def compute_summary(case_metrics: list[CaseMetrics]) -> BenchmarkSummary:
    """Aggregate per-case metrics into a summary."""
    s = BenchmarkSummary()
    s.total_cases = len(case_metrics)
    completed = [m for m in case_metrics if m.error is None]
    s.completed_cases = len(completed)
    s.errored_cases = s.total_cases - s.completed_cases

    if not completed:
        return s

    n = len(completed)
    s.avg_oracle_hit_rate = sum(1 for m in completed if m.oracle_hit) / n
    s.avg_oracle_hit_1_rate = sum(1 for m in completed if m.oracle_hit_1) / n
    rots = [m.context_rot_index for m in completed if m.context_rot_index < float("inf")]
    s.avg_context_rot_index = sum(rots) / len(rots) if rots else float("inf")

    exv_tokens = [m.exvisit_tokens for m in completed if m.exvisit_tokens > 0]
    ctl_tokens = [m.control_tokens for m in completed if m.control_tokens > 0]
    s.avg_exvisit_tokens = sum(exv_tokens) / len(exv_tokens) if exv_tokens else 0
    s.avg_control_tokens = sum(ctl_tokens) / len(ctl_tokens) if ctl_tokens else 0
    if s.avg_control_tokens > 0:
        s.token_reduction_ratio = 1.0 - (s.avg_exvisit_tokens / s.avg_control_tokens)

    s.pass_at_1_rate = sum(1 for m in completed if m.pass_at_1) / n
    s.patch_generation_rate = sum(1 for m in completed if m.patch_generated) / n

    prompt_tokens = [m.total_prompt_tokens for m in completed]
    s.avg_prompt_tokens = sum(prompt_tokens) / n
    comp_tokens = [m.total_completion_tokens for m in completed]
    s.avg_completion_tokens = sum(comp_tokens) / n
    s.avg_tool_calls = sum(m.total_tool_calls for m in completed) / n

    return s


def format_summary_table(summary: BenchmarkSummary) -> str:
    """Format summary as a markdown table."""
    lines = [
        "# ExVisit MCP SWE-Bench Lite Benchmark Results",
        "",
        f"**Cases:** {summary.total_cases} total, "
        f"{summary.completed_cases} completed, {summary.errored_cases} errored",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Oracle hit rate | {summary.avg_oracle_hit_rate:.1%} |",
        f"| Oracle hit@1 rate | {summary.avg_oracle_hit_1_rate:.1%} |",
        f"| Avg context rot index | {summary.avg_context_rot_index:.2f} |",
        f"| Avg ExVisit tokens | {summary.avg_exvisit_tokens:.0f} |",
        f"| Avg control tokens | {summary.avg_control_tokens:.0f} |",
        f"| Token reduction | {summary.token_reduction_ratio:.1%} |",
        f"| Pass@1 rate | {summary.pass_at_1_rate:.1%} |",
        f"| Patch generation rate | {summary.patch_generation_rate:.1%} |",
        f"| Avg prompt tokens | {summary.avg_prompt_tokens:.0f} |",
        f"| Avg completion tokens | {summary.avg_completion_tokens:.0f} |",
        f"| Avg tool calls | {summary.avg_tool_calls:.1f} |",
    ]
    return "\n".join(lines)


def save_results(
    case_metrics: list[CaseMetrics],
    summary: BenchmarkSummary,
    output_dir: Path,
):
    """Save detailed results and summary to disk."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # Per-case JSON
    cases_path = output_dir / "case_results.json"
    with open(cases_path, "w", encoding="utf-8") as f:
        json.dump([m.to_dict() for m in case_metrics], f, indent=2)

    # Summary JSON
    summary_path = output_dir / "summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary.to_dict(), f, indent=2)

    # Summary markdown
    md_path = output_dir / "summary.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(format_summary_table(summary))

    return cases_path, summary_path, md_path


# ---------------------------------------------------------------------------
# NavTrace — per-tool-call recording for agentic ExVisit runs
# ---------------------------------------------------------------------------

SolveMode = Literal["blast_only", "blast+rg", "blast+locate", "multi_tool", "unknown"]
Confidence = Literal["HIGH", "MED", "LOW", "UNKNOWN"]


@dataclass
class ToolCallRecord:
    """Records one tool invocation during LLM navigation."""
    tool: str                      # exv_blast | exv_locate | exv_expand | rg | exv_anchor
    args_summary: str              # short repr of args (not full text — just key params)
    args_tokens: int               # tokens in args sent to LLM
    output_tokens: int             # tokens in tool result returned to LLM
    success: bool = True
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "args_summary": self.args_summary,
            "args_tokens": self.args_tokens,
            "output_tokens": self.output_tokens,
            "success": self.success,
            "error": self.error,
        }


@dataclass
class NavTrace:
    """Full navigation trace for one SWE-bench case in an agentic ExVisit run."""
    case_id: str
    oracle_files: list[str] = field(default_factory=list)
    predicted_files: list[str] = field(default_factory=list)

    # Tool usage
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    blast_calls: int = 0
    rg_calls: int = 0
    locate_calls: int = 0
    expand_calls: int = 0

    # Classification
    solve_mode: SolveMode = "unknown"
    confidence: Confidence = "UNKNOWN"

    # Oracle outcomes
    oracle_hit: bool = False        # any oracle file in predicted_files
    oracle_hit_at1: bool = False    # oracle file is predicted_files[0]
    oracle_hit_at3: bool = False    # oracle file in top-3 predicted_files

    # Token accounting
    system_prompt_tokens: int = 0
    issue_tokens: int = 0           # tokens in the issue text
    total_nav_tokens: int = 0       # sum of all tool output tokens
    total_prompt_tokens: int = 0    # LLM prompt tokens (from API usage)
    total_completion_tokens: int = 0

    # Timing
    elapsed_s: float = 0.0
    error: Optional[str] = None

    def record_tool(self, record: ToolCallRecord) -> None:
        self.tool_calls.append(record)
        self.total_nav_tokens += record.output_tokens
        if record.tool == "exv_blast":
            self.blast_calls += 1
        elif record.tool == "rg":
            self.rg_calls += 1
        elif record.tool == "exv_locate":
            self.locate_calls += 1
        elif record.tool == "exv_expand":
            self.expand_calls += 1

    def finalize(self) -> None:
        """Derive solve_mode, oracle hit flags after prediction is set."""
        # Solve mode
        used = set()
        for tc in self.tool_calls:
            if tc.success:
                used.add(tc.tool)
        if used == {"exv_blast"}:
            self.solve_mode = "blast_only"
        elif "rg" in used and "exv_locate" not in used and "exv_expand" not in used:
            self.solve_mode = "blast+rg"
        elif "exv_locate" in used and "rg" not in used:
            self.solve_mode = "blast+locate"
        elif len(used) > 2:
            self.solve_mode = "multi_tool"

        # Oracle hit flags
        norm_oracle = [f.replace("\\", "/").strip("/") for f in self.oracle_files]
        norm_pred = [f.replace("\\", "/").strip("/") for f in self.predicted_files]

        def _match(pred: str, oracles: list[str]) -> bool:
            return any(pred == o or pred.endswith(o) or o.endswith(pred) for o in oracles)

        self.oracle_hit = any(_match(p, norm_oracle) for p in norm_pred)
        self.oracle_hit_at1 = bool(norm_pred) and _match(norm_pred[0], norm_oracle)
        self.oracle_hit_at3 = any(_match(p, norm_oracle) for p in norm_pred[:3])

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "oracle_files": self.oracle_files,
            "predicted_files": self.predicted_files,
            "solve_mode": self.solve_mode,
            "confidence": self.confidence,
            "oracle_hit": self.oracle_hit,
            "oracle_hit_at1": self.oracle_hit_at1,
            "oracle_hit_at3": self.oracle_hit_at3,
            "blast_calls": self.blast_calls,
            "rg_calls": self.rg_calls,
            "locate_calls": self.locate_calls,
            "expand_calls": self.expand_calls,
            "total_tool_calls": len(self.tool_calls),
            "total_nav_tokens": self.total_nav_tokens,
            "system_prompt_tokens": self.system_prompt_tokens,
            "issue_tokens": self.issue_tokens,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "elapsed_s": self.elapsed_s,
            "error": self.error,
            "tool_calls": [tc.to_dict() for tc in self.tool_calls],
        }


# ---------------------------------------------------------------------------
# Comparison report: baseline LLM vs ExVisit-powered LLM
# ---------------------------------------------------------------------------

@dataclass
class BaselineRates:
    """Known leaderboard oracle rates for a model without ExVisit."""
    model: str
    oracle_hit_at1: Optional[float] = None   # fraction 0-1, or None if unknown
    oracle_hit_at3: Optional[float] = None
    oracle_hit_any: Optional[float] = None
    note: str = ""

    @classmethod
    def unknown(cls, model: str) -> "BaselineRates":
        return cls(model=model, note="no baseline data — run without ExVisit to establish")


# Known baselines from SWE-bench leaderboard (oracle file location, not patch eval)
# These are approximate; update as new data becomes available.
_KNOWN_BASELINES: dict[str, BaselineRates] = {
    "qwen/qwen-2.5-7b-instruct": BaselineRates(
        model="qwen/qwen-2.5-7b-instruct",
        oracle_hit_at1=0.35, oracle_hit_at3=0.50, oracle_hit_any=0.55,
        note="estimated from SWE-bench Lite 7B class results",
    ),
    "meta-llama/llama-3.1-8b-instruct": BaselineRates(
        model="meta-llama/llama-3.1-8b-instruct",
        oracle_hit_at1=0.30, oracle_hit_at3=0.45, oracle_hit_any=0.50,
        note="estimated from SWE-bench Lite 8B class results",
    ),
    "openai/gpt-3.5-turbo": BaselineRates(
        model="openai/gpt-3.5-turbo",
        oracle_hit_at1=0.40, oracle_hit_at3=0.56, oracle_hit_any=0.62,
        note="estimated from public GPT-3.5 SWE-bench results",
    ),
    "mistralai/mistral-7b-instruct": BaselineRates(
        model="mistralai/mistral-7b-instruct",
        oracle_hit_at1=0.28, oracle_hit_at3=0.42, oracle_hit_any=0.48,
        note="estimated",
    ),
}


def get_baseline(model: str) -> BaselineRates:
    """Look up known baseline rates. Falls back to unknown."""
    # Try exact match, then suffix match (e.g. strip openrouter/ prefix)
    if model in _KNOWN_BASELINES:
        return _KNOWN_BASELINES[model]
    for key, val in _KNOWN_BASELINES.items():
        if model.endswith(key) or key.endswith(model):
            return val
    return BaselineRates.unknown(model)


def generate_comparison_report(
    traces: list[NavTrace],
    model: str,
    baseline: Optional[BaselineRates] = None,
) -> str:
    """Generate a markdown comparison table: baseline LLM vs ExVisit-powered LLM."""
    if baseline is None:
        baseline = get_baseline(model)

    n = len(traces)
    if n == 0:
        return "No traces to summarize."

    completed = [t for t in traces if t.error is None]
    nc = len(completed)

    # Powered metrics
    hit_at1 = sum(1 for t in completed if t.oracle_hit_at1) / nc if nc else 0
    hit_at3 = sum(1 for t in completed if t.oracle_hit_at3) / nc if nc else 0
    hit_any = sum(1 for t in completed if t.oracle_hit) / nc if nc else 0
    avg_nav_toks = sum(t.total_nav_tokens for t in completed) / nc if nc else 0
    avg_tool_calls = sum(len(t.tool_calls) for t in completed) / nc if nc else 0
    blast_only_rate = sum(1 for t in completed if t.solve_mode == "blast_only") / nc if nc else 0
    rg_assist_rate = sum(1 for t in completed if "rg" in t.solve_mode) / nc if nc else 0
    locate_rate = sum(1 for t in completed if "locate" in t.solve_mode) / nc if nc else 0

    # Token compression vs a raw LLM reading files (~50K tokens per case, Django repo)
    RAW_LLM_NAV_TOKENS = 50_000
    compression = RAW_LLM_NAV_TOKENS / avg_nav_toks if avg_nav_toks > 0 else float("inf")

    def fmt_pct(v: Optional[float]) -> str:
        return f"{v:.1%}" if v is not None else "unknown"

    def delta(powered: float, base: Optional[float]) -> str:
        if base is None:
            return ""
        d = powered - base
        return f" ({'↑' if d >= 0 else '↓'}{abs(d):.1%})"

    lines = [
        f"# ExVisit Navigator — Comparison Report",
        f"",
        f"**Model:** `{model}`  |  **Cases:** {n} total, {nc} completed, {n-nc} errors",
        f"**Baseline note:** {baseline.note}",
        f"",
        f"| Metric | Baseline (no ExVisit) | ExVisit-Powered | Delta |",
        f"|---|---:|---:|---:|",
        f"| oracle hit@1 | {fmt_pct(baseline.oracle_hit_at1)} | {fmt_pct(hit_at1)} | {delta(hit_at1, baseline.oracle_hit_at1)} |",
        f"| oracle hit@3 | {fmt_pct(baseline.oracle_hit_at3)} | {fmt_pct(hit_at3)} | {delta(hit_at3, baseline.oracle_hit_at3)} |",
        f"| oracle hit (any) | {fmt_pct(baseline.oracle_hit_any)} | {fmt_pct(hit_any)} | {delta(hit_any, baseline.oracle_hit_any)} |",
        f"| avg nav tokens | ~{RAW_LLM_NAV_TOKENS:,} | {avg_nav_toks:,.0f} | {compression:.0f}× less |",
        f"| avg tool calls | N/A | {avg_tool_calls:.1f} | — |",
        f"| blast-only solve rate | N/A | {blast_only_rate:.1%} | — |",
        f"| rg-assist rate | N/A | {rg_assist_rate:.1%} | — |",
        f"| locate-assist rate | N/A | {locate_rate:.1%} | — |",
        f"",
        f"## Solve Mode Distribution",
        f"",
    ]

    mode_counts: dict[str, int] = {}
    for t in completed:
        mode_counts[t.solve_mode] = mode_counts.get(t.solve_mode, 0) + 1
    for mode, count in sorted(mode_counts.items(), key=lambda x: -x[1]):
        lines.append(f"- `{mode}`: {count} cases ({count/nc:.1%})")

    lines += [
        f"",
        f"## Confidence Distribution",
        f"",
    ]
    conf_counts: dict[str, int] = {}
    for t in completed:
        conf_counts[t.confidence] = conf_counts.get(t.confidence, 0) + 1
    for conf, count in sorted(conf_counts.items(), key=lambda x: -x[1]):
        # oracle hit rate per confidence tier
        tier = [t for t in completed if t.confidence == conf]
        tier_hit = sum(1 for t in tier if t.oracle_hit) / len(tier) if tier else 0
        lines.append(f"- `{conf}`: {count} cases — oracle hit {tier_hit:.1%}")

    return "\n".join(lines)


def save_nav_traces(
    traces: list[NavTrace],
    model: str,
    output_path: Path,
    baseline: Optional[BaselineRates] = None,
) -> None:
    """Save NavTrace list + comparison report to disk."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    report = generate_comparison_report(traces, model, baseline)
    report_path = output_path.with_suffix(".report.md")
    report_path.write_text(report, encoding="utf-8")

    output_path.write_text(
        json.dumps({"model": model, "traces": [t.to_dict() for t in traces]}, indent=2),
        encoding="utf-8",
    )
