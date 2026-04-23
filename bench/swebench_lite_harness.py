"""SWE-bench Lite navigation benchmark harness for Project exvisit.

This harness is deliberately offline-first:

- It can ingest real SWE-bench Lite instances from Hugging Face when `datasets`
  is installed.
- It can also run against local JSONL cases with the same schema.
- It measures the navigation phase economics today: token burn, steps,
  context-rot proxy, and oracle-hit rate against the gold patch's changed files.
- It exposes optional command-template hooks for future pass@1 integrations with
  external agents (e.g. SWE-agent, OpenHands, aider).

It does *not* attempt to synthesize patches itself. The benchmark value shipped
in this repo is the context-navigation delta that exvisit improves immediately.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import json
import os
import re
import shutil
import subprocess
import sys
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from exvisit import parse, query  # noqa: E402
from exvisit.blast import build_blast_bundle  # noqa: E402
from exvisit.ast import exvisitDoc, Node  # noqa: E402
from exvisit.scaffold import generate as scaffold_generate  # noqa: E402
from exvisit.graph_meta import sidecar_path  # noqa: E402


STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "bug", "by", "can",
    "case", "does", "error", "for", "from", "get", "if", "in", "into",
    "is", "it", "its", "just", "not", "of", "on", "or", "should",
    "that", "the", "their", "there", "this", "to", "when", "with",
    "without", "while", "under", "using", "use", "used", "returns",
    "return", "request", "issue", "problem",
}

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules", "build", "dist", "target"}
_FETCHED_REPOS: Set[str] = set()


@dataclass
class BenchmarkCase:
    case_id: str
    repo: str
    repo_path: str
    base_commit: Optional[str]
    issue_text: str
    oracle_files: List[str]
    exvisit_path: Optional[str] = None
    extra: Dict[str, object] = field(default_factory=dict)


@dataclass
class PricingConfig:
    input_base_per_1m: float
    cache_write_per_1m: float = 0.0
    cache_read_per_1m: float = 0.0
    output_per_1m: float = 0.0


@dataclass
class UsageSummary:
    prompt_base_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    trajectory_path: Optional[str] = None
    cost_to_resolve_usd: Optional[float] = None

    def to_dict(self) -> Dict[str, object]:
        payload: Dict[str, object] = {
            "prompt_base_tokens": self.prompt_base_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }
        if self.trajectory_path is not None:
            payload["trajectory_path"] = self.trajectory_path
        if self.cost_to_resolve_usd is not None:
            payload["cost_to_resolve_usd"] = self.cost_to_resolve_usd
        return payload


@dataclass
class RunnerExecution:
    pass_at_1: Optional[bool]
    exit_code: int
    notes: List[str] = field(default_factory=list)
    usage: Optional[UsageSummary] = None


@dataclass
class StrategyResult:
    strategy: str
    selected_targets: List[str]
    selected_files: List[str]
    snippet_labels: List[str]
    steps: int
    input_tokens: int
    context_rot_index: int
    oracle_hit: bool
    oracle_hit_at_1: bool
    first_oracle_rank: Optional[int]
    pass_at_1: Optional[bool] = None
    runner_exit_code: Optional[int] = None
    usage: Optional[Dict[str, object]] = None
    cost_to_resolve_usd: Optional[float] = None
    notes: List[str] = field(default_factory=list)


@dataclass
class CaseResult:
    case_id: str
    repo: str
    repo_path: str
    base_commit: Optional[str]
    oracle_files: List[str]
    control: StrategyResult
    exvisit: StrategyResult


def _write_json(path: Path, payload: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=json_default), encoding="utf-8")


def estimate_tokens(text: str) -> int:
    try:
        import tiktoken  # type: ignore

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return max(1, len(text) // 4)


def json_default(value):
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def load_pricing_config(path: Optional[Path]) -> Optional[PricingConfig]:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return PricingConfig(
        input_base_per_1m=float(payload["input_base_per_1m"]),
        cache_write_per_1m=float(payload.get("cache_write_per_1m", 0.0)),
        cache_read_per_1m=float(payload.get("cache_read_per_1m", 0.0)),
        output_per_1m=float(payload.get("output_per_1m", 0.0)),
    )


def _first_int(payload: object, keys: Sequence[str]) -> Optional[int]:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
    return None


def _iter_usage_metadata(value: object) -> Iterable[Dict[str, object]]:
    if isinstance(value, dict):
        usage = value.get("usage_metadata")
        if isinstance(usage, dict):
            yield usage
        for child in value.values():
            yield from _iter_usage_metadata(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_usage_metadata(child)


def compute_usage_cost(usage: UsageSummary, pricing: PricingConfig) -> float:
    return (
        (usage.prompt_base_tokens * pricing.input_base_per_1m)
        + (usage.cache_write_tokens * pricing.cache_write_per_1m)
        + (usage.cache_read_tokens * pricing.cache_read_per_1m)
        + (usage.completion_tokens * pricing.output_per_1m)
    ) / 1_000_000.0


def extract_usage_summary(
    payload: object,
    trajectory_path: Optional[str] = None,
    pricing: Optional[PricingConfig] = None,
) -> Optional[UsageSummary]:
    summary = UsageSummary(trajectory_path=trajectory_path)
    found = False
    for meta in _iter_usage_metadata(payload):
        found = True
        total_input = _first_int(meta, ["input_tokens", "prompt_tokens", "prompt_token_count"])
        cache_write = _first_int(meta, ["cache_creation_input_tokens", "cache_write_tokens", "cache_write_input_tokens"]) or 0
        cache_read = _first_int(meta, ["cache_read_input_tokens", "cache_read_tokens", "cached_input_tokens"]) or 0
        explicit_base = _first_int(meta, ["prompt_base_tokens", "base_input_tokens"])
        inferred_base = max(0, total_input - cache_write - cache_read) if total_input is not None else None
        prompt_base = explicit_base if explicit_base is not None else (inferred_base or 0)
        completion = _first_int(meta, ["output_tokens", "completion_tokens", "output_token_count"]) or 0
        detail_map = None
        for detail_key in ("output_tokens_details", "completion_tokens_details", "output_token_details"):
            value = meta.get(detail_key)
            if isinstance(value, dict):
                detail_map = value
                break
        reasoning = _first_int(meta, ["reasoning_tokens"]) or _first_int(detail_map, ["reasoning_tokens"]) or 0

        summary.prompt_base_tokens += prompt_base
        summary.cache_write_tokens += cache_write
        summary.cache_read_tokens += cache_read
        summary.completion_tokens += completion
        summary.reasoning_tokens += reasoning

    if not found:
        return None
    if pricing is not None:
        summary.cost_to_resolve_usd = compute_usage_cost(summary, pricing)
    return summary


def load_trajectory_usage(path: Path, pricing: Optional[PricingConfig] = None) -> Optional[UsageSummary]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return extract_usage_summary(payload, trajectory_path=str(path), pricing=pricing)


def extract_json_payload(text: str) -> Optional[Dict[str, object]]:
    candidates = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)
        lines = [line.strip() for line in stripped.splitlines() if line.strip()]
        if lines:
            candidates.append(lines[-1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def slugify_repo(repo: str) -> str:
    return repo.replace("/", "__")


def camelize_repo(repo: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", repo)
    return "".join(p[:1].upper() + p[1:] for p in parts if p) or "App"


def repo_text_files(repo_root: Path) -> List[Path]:
    return [
        path for path in repo_root.rglob("*.py")
        if not any(part in SKIP_DIRS for part in path.parts)
    ]


def extract_oracle_files_from_patch(patch: str) -> List[str]:
    files: List[str] = []
    seen = set()
    for line in patch.splitlines():
        if not line.startswith("+++ b/"):
            continue
        rel = line[6:].strip()
        if rel == "/dev/null" or rel in seen:
            continue
        seen.add(rel)
        files.append(rel)
    return files


def extract_issue_terms(issue_text: str) -> Tuple[List[str], List[str]]:
    code_terms: List[str] = []
    fenced_blocks = re.findall(r"```(.*?)```", issue_text, flags=re.DOTALL)
    scrubbed = re.sub(r"```.*?```", " ", issue_text, flags=re.DOTALL)

    for block in fenced_blocks:
        for token in re.findall(r"[A-Za-z0-9_./-]+\.py", block):
            code_terms.append(token)
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_\.]{2,}", block):
            if "." in token or "_" in token or any(ch.isupper() for ch in token[1:]):
                code_terms.append(token)

    for raw in re.findall(r"`([^`\n]+)`", scrubbed):
        raw = raw.strip()
        if raw:
            code_terms.append(raw)

    for token in re.findall(r"[A-Za-z0-9_./-]+\.py", scrubbed):
        code_terms.append(token)

    dotted = re.findall(r"[A-Za-z_][A-Za-z0-9_\.]{2,}", scrubbed)
    for token in dotted:
        if "." in token or "_" in token or any(ch.isupper() for ch in token[1:]):
            code_terms.append(token)

    keywords = []
    seen = set()
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", issue_text.lower()):
        if token in STOPWORDS or token in seen:
            continue
        seen.add(token)
        keywords.append(token)

    dedup_code = []
    seen_code = set()
    for token in code_terms:
        key = token.lower()
        if key in seen_code:
            continue
        seen_code.add(key)
        dedup_code.append(token)
    return dedup_code, keywords


def patch_oracle_rank(selected_files: Sequence[str], oracle_files: Sequence[str]) -> Tuple[bool, bool, Optional[int], int]:
    oracle_norm = [f.replace("\\", "/").lower() for f in oracle_files]
    selected_norm = [f.replace("\\", "/").lower() for f in selected_files]
    first_oracle_rank = None
    for idx, rel in enumerate(selected_norm, start=1):
        if any(rel.endswith(oracle) for oracle in oracle_norm):
            first_oracle_rank = idx
            break
    oracle_hit = first_oracle_rank is not None
    oracle_hit_at_1 = first_oracle_rank == 1
    context_rot = 0 if first_oracle_rank is None else max(0, first_oracle_rank - 1)
    if not oracle_hit:
        context_rot = len(selected_files)
    return oracle_hit, oracle_hit_at_1, first_oracle_rank, context_rot


def top_search_hits(repo_root: Path, keywords: Sequence[str], max_hits: int = 24) -> Tuple[str, Dict[str, int]]:
    hits: List[Tuple[int, str, int, str]] = []
    file_scores: Dict[str, int] = {}
    if not keywords:
        return "", file_scores

    def _collect_hit(rel: str, lineno: int, text: str):
        path_obj = Path(rel)
        stem_score = sum(3 for kw in keywords if kw in path_obj.stem.lower())
        score = sum(1 for kw in keywords if kw in text.lower())
        if score <= 0 and stem_score <= 0:
            return
        hits.append((score + stem_score, rel, lineno, text.strip()))
        file_scores[rel] = file_scores.get(rel, 0) + score + stem_score

    rg_path = shutil.which("rg")
    if rg_path:
        pattern = "|".join(re.escape(keyword) for keyword in keywords)
        try:
            proc = subprocess.run(
                [rg_path, "-n", "-i", "-g", "*.py", pattern, str(repo_root)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            for raw_line in proc.stdout.splitlines():
                rel_part, sep, rest = raw_line.partition(":")
                if not sep:
                    continue
                line_part, sep, text = rest.partition(":")
                if not sep:
                    continue
                try:
                    path = Path(rel_part)
                    rel = path.relative_to(repo_root).as_posix()
                except Exception:
                    rel = rel_part.replace("\\", "/")
                try:
                    lineno = int(line_part)
                except ValueError:
                    continue
                _collect_hit(rel, lineno, text)
            if hits or file_scores:
                hits.sort(key=lambda item: (-item[0], item[1], item[2]))
                lines = [f"{rel}:{lineno}: {text}" for _, rel, lineno, text in hits[:max_hits]]
                return "\n".join(lines), file_scores
        except Exception:
            pass
    git_path = shutil.which("git")
    if git_path and (repo_root / ".git").exists():
        pattern = "|".join(re.escape(keyword) for keyword in keywords)
        try:
            proc = subprocess.run(
                [git_path, "-C", str(repo_root), "grep", "-n", "-i", "-E", pattern, "--", "*.py"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            for raw_line in proc.stdout.splitlines():
                rel, sep, rest = raw_line.partition(":")
                if not sep:
                    continue
                line_part, sep, text = rest.partition(":")
                if not sep:
                    continue
                try:
                    lineno = int(line_part)
                except ValueError:
                    continue
                _collect_hit(rel.replace("\\", "/"), lineno, text)
            if hits or file_scores:
                hits.sort(key=lambda item: (-item[0], item[1], item[2]))
                lines = [f"{rel}:{lineno}: {text}" for _, rel, lineno, text in hits[:max_hits]]
                return "\n".join(lines), file_scores
        except Exception:
            pass
    for path in repo_text_files(repo_root):
        rel = path.relative_to(repo_root).as_posix()
        stem_score = sum(3 for kw in keywords if kw in path.stem.lower())
        if stem_score:
            file_scores[rel] = file_scores.get(rel, 0) + stem_score
        try:
            for lineno, line in enumerate(path.read_text(encoding="utf-8-sig", errors="replace").splitlines(), start=1):
                score = sum(1 for kw in keywords if kw in line.lower())
                if score <= 0:
                    continue
                hits.append((score + stem_score, rel, lineno, line.strip()))
                file_scores[rel] = file_scores.get(rel, 0) + score
        except Exception:
            continue
    hits.sort(key=lambda item: (-item[0], item[1], item[2]))
    lines = [f"{rel}:{lineno}: {text}" for _, rel, lineno, text in hits[:max_hits]]
    return "\n".join(lines), file_scores


def rank_control_files(repo_root: Path, issue_text: str, max_files: int = 4) -> List[str]:
    code_terms, keywords = extract_issue_terms(issue_text)
    search_output, file_scores = top_search_hits(repo_root, keywords)
    lowered_issue = issue_text.lower()
    for rel in list(file_scores.keys()):
        filename = Path(rel).stem.lower()
        if filename in lowered_issue:
            file_scores[rel] += 5
    for token in code_terms:
        low = token.lower()
        tail = low.split(".")[-1]
        for rel in list(file_scores.keys()):
            if tail in rel.lower():
                file_scores[rel] += 4
    ranked = sorted(file_scores.items(), key=lambda item: (-item[1], item[0]))
    result = [rel for rel, _ in ranked[:max_files]]
    if not result and search_output:
        result = [search_output.splitlines()[0].split(":", 1)[0]]
    return result


def parse_python_symbol_ranges(file_path: Path) -> Dict[str, Tuple[int, int]]:
    try:
        source = file_path.read_text(encoding="utf-8-sig", errors="replace")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(source)
    except Exception:
        return {}

    ranges: Dict[str, Tuple[int, int]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = getattr(node, "lineno", None)
            end = getattr(node, "end_lineno", start)
            if start and end:
                ranges[node.name] = (start, end)
        elif isinstance(node, ast.ClassDef):
            start = getattr(node, "lineno", None)
            end = getattr(node, "end_lineno", start)
            if start and end:
                ranges[node.name] = (start, end)
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    cstart = getattr(child, "lineno", None)
                    cend = getattr(child, "end_lineno", cstart)
                    if cstart and cend:
                        ranges[f"{node.name}.{child.name}"] = (cstart, cend)
                        ranges[child.name] = ranges.get(child.name, (cstart, cend))
    return ranges


def choose_best_snippet(file_path: Path, issue_text: str, max_context_lines: int = 80) -> Tuple[str, str]:
    lines = file_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    ranges = parse_python_symbol_ranges(file_path)
    code_terms, keywords = extract_issue_terms(issue_text)

    best_symbol = None
    best_score = -1
    for symbol, (start, end) in ranges.items():
        score = 0
        sym_low = symbol.lower()
        for term in code_terms:
            low = term.lower()
            if low == sym_low or low.endswith("." + sym_low) or sym_low.endswith("." + low):
                score += 10
            elif low.split(".")[-1] == sym_low.split(".")[-1]:
                score += 6
        for kw in keywords:
            if kw in sym_low:
                score += 1
        if score > best_score:
            best_score = score
            best_symbol = (symbol, start, end)

    if best_symbol and best_score > 0:
        symbol, start, end = best_symbol
        window = min(max_context_lines, max(12, end - start + 1 + 10))
        half_pad = max(3, (window - (end - start + 1)) // 2)
        lo = max(1, start - half_pad)
        hi = min(len(lines), end + half_pad)
        snippet = "\n".join(f"{idx}: {lines[idx - 1]}" for idx in range(lo, hi + 1))
        return f"{file_path.name}:{lo}-{hi} ({symbol})", snippet

    hi = min(len(lines), max_context_lines)
    snippet = "\n".join(f"{idx}: {lines[idx - 1]}" for idx in range(1, hi + 1))
    return f"{file_path.name}:1-{hi} (file-head)", snippet


def read_full_file(file_path: Path) -> Tuple[str, str]:
    text = file_path.read_text(encoding="utf-8-sig", errors="replace")
    return file_path.name, text


def score_exvisit_nodes(doc: exvisitDoc, issue_text: str, repo_root: Optional[Path] = None) -> List[Tuple[int, Node]]:
    code_terms, keywords = extract_issue_terms(issue_text)
    lowered = issue_text.lower()
    src_name_counts = Counter(Path(term).name.lower() for term in code_terms if term.lower().endswith(".py"))
    symbol_terms = [term for term in code_terms if "." in term and not term.lower().endswith(".py")]
    range_cache: Dict[str, Dict[str, Tuple[int, int]]] = {}
    scored: List[Tuple[int, Node]] = []
    for node in doc.all_nodes():
        src_path = (node.src_path or "").lower()
        src_name = Path(node.src_path or "").name.lower()
        name = node.name.lower()
        ns = node.ns_path.lower()
        score = 0
        if node.src_path and Path(node.src_path).stem.lower() in lowered:
            score += 8
        if src_name in lowered:
            score += 8
        if src_name in src_name_counts:
            score += 12 * src_name_counts[src_name]
        if name in lowered:
            score += 8
        for term in code_terms:
            low = term.lower()
            tail = low.split(".")[-1]
            if "/" in low and (src_path.endswith(low) or src_name == Path(low).name):
                score += 12
            if low == name or low.endswith("." + name):
                score += 10
            if tail == name:
                score += 6
            if tail in src_path:
                score += 6
        if repo_root is not None and symbol_terms and node.src_path:
            path = resolve_repo_file(repo_root, node.src_path)
            if path is not None:
                cache_key = str(path)
                ranges = range_cache.get(cache_key)
                if ranges is None:
                    ranges = parse_python_symbol_ranges(path)
                    range_cache[cache_key] = ranges
                lowered_keys = {key.lower() for key in ranges}
                for term in symbol_terms:
                    low = term.lower()
                    tail = low.split(".")[-1]
                    if low in lowered_keys:
                        score += 30
                    elif tail in lowered_keys:
                        score += 20
        for kw in keywords:
            if kw in name:
                score += 3
            if kw in src_path:
                score += 3
            if kw in ns:
                score += 1
        if score > 0:
            scored.append((score, node))
    scored.sort(key=lambda item: (-item[0], item[1].fqn))
    return scored


def resolve_repo_file(repo_root: Path, src_path: Optional[str]) -> Optional[Path]:
    if not src_path:
        return None
    direct = repo_root / src_path
    if direct.exists():
        return direct
    name = Path(src_path).name
    for path in repo_text_files(repo_root):
        if path.name == name:
            return path
    return None


def run_runner_command(
    template: str,
    case: BenchmarkCase,
    exvisit_path: Optional[str],
    pricing: Optional[PricingConfig] = None,
    trajectory_template: Optional[str] = None,
    workspace_path: Optional[str] = None,
) -> RunnerExecution:
    repo_root = Path(workspace_path or case.repo_path)
    mapping = {
        "repo_path": repo_root.as_posix(),
        "exvisit_path": Path(exvisit_path or "").as_posix(),
        "issue_text": case.issue_text,
        "case_id": case.case_id,
        "workspace_path": repo_root.as_posix(),
    }
    command = template.format(**mapping)
    import os
    with open("scratch/model_debug.txt", "w") as f:
        f.write(f"exvisit_MODEL='{os.environ.get('exvisit_MODEL')}'\n")
        f.write(f"command='{command}'\n")
    proc = subprocess.run(command, shell=True, text=True, capture_output=True)
    notes: List[str] = []
    if proc.stdout.strip():
        notes.append(proc.stdout.strip())
    if proc.stderr.strip():
        notes.append(proc.stderr.strip())
    payload = extract_json_payload(proc.stdout)
    passed: Optional[bool]
    if payload is not None and "pass_at_1" in payload:
        passed = bool(payload.get("pass_at_1"))
    else:
        passed = proc.returncode == 0

    trajectory_path = None
    if payload is not None:
        for key in ("trajectory_path", "traj_path", "traj_file", "last_mini_run_traj", "trajectory"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                trajectory_path = value.strip()
                break
    if trajectory_template:
        trajectory_path = trajectory_path or trajectory_template.format(**mapping)

    usage = None
    if trajectory_path:
        traj_path = Path(trajectory_path)
        if not traj_path.is_absolute():
            traj_path = repo_root / traj_path
        if traj_path.exists():
            usage = load_trajectory_usage(traj_path, pricing=pricing)
            if usage is not None:
                usage.trajectory_path = str(traj_path)
        else:
            notes.append(f"trajectory_missing={traj_path}")
    if usage is None and payload is not None:
        usage = extract_usage_summary(payload, trajectory_path=trajectory_path, pricing=pricing)

    return RunnerExecution(pass_at_1=passed, exit_code=proc.returncode, notes=notes, usage=usage)


def control_strategy(
    case: BenchmarkCase,
    max_files: int = 4,
    runner_cmd: Optional[str] = None,
    pricing: Optional[PricingConfig] = None,
    trajectory_template: Optional[str] = None,
    workspace_path: Optional[str] = None,
) -> StrategyResult:
    repo_root = Path(case.repo_path)
    selected = rank_control_files(repo_root, case.issue_text, max_files=max_files)
    code_terms, keywords = extract_issue_terms(case.issue_text)
    search_output, _ = top_search_hits(repo_root, keywords)

    token_total = estimate_tokens(case.issue_text)
    steps = 1  # repo-wide search
    notes = []
    if search_output:
        token_total += estimate_tokens(search_output)
    selected_files: List[str] = []
    snippet_labels: List[str] = []
    for rel in selected:
        path = repo_root / rel
        if not path.exists():
            continue
        label, payload = read_full_file(path)
        token_total += estimate_tokens(payload)
        steps += 1
        selected_files.append(rel)
        snippet_labels.append(label)
    oracle_hit, oracle_hit_at_1, first_oracle_rank, context_rot = patch_oracle_rank(selected_files, case.oracle_files)
    pass_at_1 = None
    runner_exit_code = None
    usage = None
    cost_to_resolve_usd = None
    if runner_cmd:
        execution = run_runner_command(
            runner_cmd,
            case,
            case.exvisit_path,
            pricing=pricing,
            trajectory_template=trajectory_template,
            workspace_path=workspace_path,
        )
        pass_at_1 = execution.pass_at_1
        runner_exit_code = execution.exit_code
        notes.extend(execution.notes)
        if execution.usage is not None:
            usage = execution.usage.to_dict()
            cost_to_resolve_usd = execution.usage.cost_to_resolve_usd
    if code_terms:
        notes.append(f"issue_code_terms={code_terms[:6]}")
    return StrategyResult(
        strategy="control",
        selected_targets=selected,
        selected_files=selected_files,
        snippet_labels=snippet_labels,
        steps=steps,
        input_tokens=token_total,
        context_rot_index=context_rot,
        oracle_hit=oracle_hit,
        oracle_hit_at_1=oracle_hit_at_1,
        first_oracle_rank=first_oracle_rank,
        pass_at_1=pass_at_1,
        runner_exit_code=runner_exit_code,
        usage=usage,
        cost_to_resolve_usd=cost_to_resolve_usd,
        notes=notes,
    )


def exvisit_strategy(
    case: BenchmarkCase,
    max_nodes: int = 1,
    hops: int = 2,
    runner_cmd: Optional[str] = None,
    pricing: Optional[PricingConfig] = None,
    trajectory_template: Optional[str] = None,
    workspace_path: Optional[str] = None,
) -> StrategyResult:
    if not case.exvisit_path:
        raise ValueError(f"case {case.case_id} has no exvisit_path")
    repo_root = Path(case.repo_path)
    exvisit_src = Path(case.exvisit_path).read_text(encoding="utf-8")
    doc = parse(exvisit_src)
    bundle = build_blast_bundle(
        doc,
        case.repo_path,
        case.issue_text,
        preset_name="test-fix",
        exvisit_path=case.exvisit_path,
    )
    token_total = bundle.token_estimate
    steps = 1  # one exvisit blast call yields the bundle
    notes = [f"blast_preset={bundle.preset}", f"blast_confidence={bundle.confidence}"]
    notes.extend(bundle.warnings)
    selected_targets = list(bundle.selected_nodes)
    selected_files = list(bundle.selected_files)
    snippet_labels = [snippet.label for snippet in bundle.snippets]

    oracle_hit, oracle_hit_at_1, first_oracle_rank, context_rot = patch_oracle_rank(selected_files, case.oracle_files)
    pass_at_1 = None
    runner_exit_code = None
    usage = None
    cost_to_resolve_usd = None
    if runner_cmd:
        execution = run_runner_command(
            runner_cmd,
            case,
            case.exvisit_path,
            pricing=pricing,
            trajectory_template=trajectory_template,
            workspace_path=workspace_path,
        )
        pass_at_1 = execution.pass_at_1
        runner_exit_code = execution.exit_code
        notes.extend(execution.notes)
        if execution.usage is not None:
            usage = execution.usage.to_dict()
            cost_to_resolve_usd = execution.usage.cost_to_resolve_usd
    return StrategyResult(
        strategy="exvisit",
        selected_targets=selected_targets,
        selected_files=selected_files,
        snippet_labels=snippet_labels,
        steps=steps,
        input_tokens=token_total,
        context_rot_index=context_rot,
        oracle_hit=oracle_hit,
        oracle_hit_at_1=oracle_hit_at_1,
        first_oracle_rank=first_oracle_rank,
        pass_at_1=pass_at_1,
        runner_exit_code=runner_exit_code,
        usage=usage,
        cost_to_resolve_usd=cost_to_resolve_usd,
        notes=notes,
    )


def summarize_results(results: Sequence[CaseResult], input_cost_per_1m: Optional[float] = None) -> Dict[str, object]:
    def avg(values: Sequence[float]) -> float:
        return sum(values) / max(1, len(values))

    control_tokens = [case.control.input_tokens for case in results]
    exvisit_tokens = [case.exvisit.input_tokens for case in results]
    control_steps = [case.control.steps for case in results]
    exvisit_steps = [case.exvisit.steps for case in results]
    control_rot = [case.control.context_rot_index for case in results]
    exvisit_rot = [case.exvisit.context_rot_index for case in results]

    payload: Dict[str, object] = {
        "cases": len(results),
        "control": {
            "avg_tokens": avg(control_tokens),
            "avg_steps": avg(control_steps),
            "avg_context_rot": avg(control_rot),
            "oracle_hit_rate": avg([1.0 if case.control.oracle_hit else 0.0 for case in results]),
            "oracle_hit_at_1_rate": avg([1.0 if case.control.oracle_hit_at_1 else 0.0 for case in results]),
            "pass_at_1_rate": None if any(case.control.pass_at_1 is None for case in results) else avg([1.0 if case.control.pass_at_1 else 0.0 for case in results]),
        },
        "exvisit": {
            "avg_tokens": avg(exvisit_tokens),
            "avg_steps": avg(exvisit_steps),
            "avg_context_rot": avg(exvisit_rot),
            "oracle_hit_rate": avg([1.0 if case.exvisit.oracle_hit else 0.0 for case in results]),
            "oracle_hit_at_1_rate": avg([1.0 if case.exvisit.oracle_hit_at_1 else 0.0 for case in results]),
            "pass_at_1_rate": None if any(case.exvisit.pass_at_1 is None for case in results) else avg([1.0 if case.exvisit.pass_at_1 else 0.0 for case in results]),
        },
    }
    control_avg = payload["control"]["avg_tokens"]  # type: ignore[index]
    exvisit_avg = payload["exvisit"]["avg_tokens"]  # type: ignore[index]
    payload["delta"] = {
        "token_reduction_pct": 100.0 * (1.0 - (exvisit_avg / max(1.0, control_avg))),
        "step_reduction_pct": 100.0 * (1.0 - (payload["exvisit"]["avg_steps"] / max(1.0, payload["control"]["avg_steps"]))),  # type: ignore[index]
        "context_rot_reduction_pct": 100.0 * (1.0 - (payload["exvisit"]["avg_context_rot"] / max(1.0, payload["control"]["avg_context_rot"]))),  # type: ignore[index]
    }
    control_costs = [case.control.cost_to_resolve_usd for case in results if case.control.cost_to_resolve_usd is not None]
    exvisit_costs = [case.exvisit.cost_to_resolve_usd for case in results if case.exvisit.cost_to_resolve_usd is not None]
    if control_costs and exvisit_costs and len(control_costs) == len(results) and len(exvisit_costs) == len(results):
        control_cost_avg = avg(control_costs)
        exvisit_cost_avg = avg(exvisit_costs)
        payload["control"]["avg_cost_to_resolve_usd"] = control_cost_avg  # type: ignore[index]
        payload["exvisit"]["avg_cost_to_resolve_usd"] = exvisit_cost_avg  # type: ignore[index]
        payload["delta"]["cost_to_resolve_reduction_pct"] = 100.0 * (1.0 - (exvisit_cost_avg / max(1e-9, control_cost_avg)))  # type: ignore[index]
    if input_cost_per_1m is not None:
        payload["control"]["avg_input_cost"] = control_avg / 1_000_000.0 * input_cost_per_1m  # type: ignore[index]
        payload["exvisit"]["avg_input_cost"] = exvisit_avg / 1_000_000.0 * input_cost_per_1m  # type: ignore[index]
        payload["delta"]["input_cost_reduction_pct"] = 100.0 * (1.0 - ((payload["exvisit"]["avg_input_cost"]) / max(1e-9, payload["control"]["avg_input_cost"])))  # type: ignore[index]
    return payload


def git(command: Sequence[str], cwd: Optional[Path] = None) -> None:
    if cwd:
        print(f"[git] cwd={cwd}")
    proc = subprocess.run(["git", *command], cwd=cwd, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(command)} failed: {proc.stderr.strip() or proc.stdout.strip()}")


def ensure_repo_cloned(repo: str, repo_root: Path) -> Path:
    repo_root.mkdir(parents=True, exist_ok=True)
    target = repo_root / slugify_repo(repo)
    if not target.exists():
        git(["clone", f"https://github.com/{repo}.git", str(target)], cwd=repo_root)
    return target


def ensure_checkout(repo_path: Path, commit: Optional[str]) -> None:
    if not commit:
        return
    repo_key = str(repo_path.resolve())
    if repo_key not in _FETCHED_REPOS:
        git(["fetch", "--all", "--tags", "--prune"], cwd=repo_path)
        _FETCHED_REPOS.add(repo_key)
    git(["checkout", "--force", commit], cwd=repo_path)
    git(["clean", "-fd"], cwd=repo_path)


def prepare_strategy_workspace(case: BenchmarkCase, strategy: str, workspace_root: Optional[Path]) -> Optional[Path]:
    if workspace_root is None:
        return None
    source_root = Path(case.repo_path)
    target = workspace_root / slugify_repo(case.repo) / (case.base_commit or "working") / case.case_id / strategy
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    ignored_root_name: Optional[str] = None
    try:
        relative_workspace_root = workspace_root.resolve().relative_to(source_root.resolve())
        if relative_workspace_root.parts:
            ignored_root_name = relative_workspace_root.parts[0]
    except ValueError:
        ignored_root_name = None

    base_ignore = shutil.ignore_patterns("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")

    def _ignore(src: str, names: List[str]) -> Set[str]:
        ignored = set(base_ignore(src, names))
        if ignored_root_name and Path(src).resolve() == source_root.resolve() and ignored_root_name in names:
            ignored.add(ignored_root_name)
        return ignored

    shutil.copytree(case.repo_path, target, ignore=_ignore)
    return target


def load_swebench_lite(split: str, limit: Optional[int], repos: Optional[Set[str]]) -> List[Dict[str, object]]:
    try:
        from datasets import load_dataset  # type: ignore
    except Exception as exc:
        raise RuntimeError("datasets package is required for --dataset swebench-lite") from exc

    ds = load_dataset("princeton-nlp/SWE-bench_Lite", "default", split=split)
    rows: List[Dict[str, object]] = []
    for row in ds:
        repo = str(row["repo"])
        if repos and repo not in repos:
            continue
        rows.append(dict(row))
        if limit is not None and len(rows) >= limit:
            break
    return rows


def load_cases_from_jsonl(path: Path) -> List[Dict[str, object]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def precompute_cases(
    dataset: str,
    split: str,
    limit: Optional[int],
    repos: Optional[Set[str]],
    local_cases: Optional[Path],
    cache_dir: Path,
    manifest_path: Path,
) -> Dict[str, object]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    exvisit_dir = cache_dir / "exvisites"
    repo_dir = cache_dir / "repos"
    exvisit_dir.mkdir(parents=True, exist_ok=True)
    repo_dir.mkdir(parents=True, exist_ok=True)

    if dataset == "swebench-lite":
        raw_cases = load_swebench_lite(split=split, limit=limit, repos=repos)
    elif dataset == "jsonl":
        if local_cases is None:
            raise ValueError("--local-cases is required for --dataset jsonl")
        raw_cases = load_cases_from_jsonl(local_cases)
    else:
        raise ValueError(f"unsupported dataset: {dataset}")

    existing_cases: Dict[str, Dict[str, object]] = {}
    if manifest_path.exists():
        try:
            existing_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            for case in existing_payload.get("cases", []):
                if isinstance(case, dict) and isinstance(case.get("case_id"), str):
                    existing_cases[str(case["case_id"])] = case
        except Exception:
            existing_cases = {}

    manifest_cases = []
    exvisit_text_cache: Dict[Tuple[str, Optional[str]], str] = {}
    ordered_cases = sorted(
        raw_cases,
        key=lambda row: (
            str(row.get("repo", "local/repo")),
            str(row.get("base_commit") or ""),
            str(row.get("instance_id") or row.get("case_id") or ""),
        ),
    )
    for row in ordered_cases:
        repo = str(row.get("repo", "local/repo"))
        case_id = str(row.get("instance_id") or row.get("case_id") or f"case-{len(manifest_cases)+1}")
        base_commit = row.get("base_commit")
        base_commit_text = str(base_commit) if base_commit else None
        existing_case = existing_cases.get(case_id)
        if existing_case is not None:
            exvisit_candidate = existing_case.get("exvisit_path")
            if isinstance(exvisit_candidate, str) and Path(exvisit_candidate).exists():
                manifest_cases.append(existing_case)
                continue
        if dataset == "swebench-lite":
            repo_path = ensure_repo_cloned(repo, repo_dir)
            ensure_checkout(repo_path, base_commit_text)
            issue_text = str(row["problem_statement"])
            oracle_files = extract_oracle_files_from_patch(str(row.get("patch", "")))
        else:
            repo_path = Path(str(row["repo_path"]))
            issue_text = str(row["issue_text"])
            oracle_files = list(row.get("oracle_files") or [])
        root_name = camelize_repo(repo)
        exvisit_path = exvisit_dir / f"{case_id}.exv"
        meta_out = sidecar_path(exvisit_path)
        exvisit_cache_key = (repo, base_commit_text)
        if exvisit_path.exists():
            exvisit_src = exvisit_path.read_text(encoding="utf-8")
        elif exvisit_cache_key in exvisit_text_cache:
            exvisit_src = exvisit_text_cache[exvisit_cache_key]
            exvisit_path.write_text(exvisit_src, encoding="utf-8")
        else:
            exvisit_src = scaffold_generate(str(repo_path), root_name=root_name, fast_imports=True, meta_out=meta_out)
            exvisit_text_cache[exvisit_cache_key] = exvisit_src
            exvisit_path.write_text(exvisit_src, encoding="utf-8")
        # Ensure meta file is generated for every case (even if .exv was cached)
        if not meta_out.exists():
            scaffold_generate(str(repo_path), root_name=root_name, fast_imports=True, meta_out=meta_out)
        manifest_cases.append({
            "case_id": case_id,
            "repo": repo,
            "repo_path": str(repo_path),
            "base_commit": base_commit_text,
            "issue_text": issue_text,
            "oracle_files": oracle_files,
            "exvisit_path": str(exvisit_path),
        })
        _write_json(manifest_path, {
            "dataset": dataset,
            "split": split,
            "cases": manifest_cases,
        })

    manifest = {
        "dataset": dataset,
        "split": split,
        "cases": manifest_cases,
    }
    _write_json(manifest_path, manifest)
    return manifest


def load_manifest(path: Path) -> List[BenchmarkCase]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [BenchmarkCase(**case) for case in payload["cases"]]


def _strategy_result_from_dict(payload: Dict[str, object]) -> StrategyResult:
    return StrategyResult(**payload)


def _case_result_from_dict(payload: Dict[str, object]) -> CaseResult:
    return CaseResult(
        case_id=str(payload["case_id"]),
        repo=str(payload["repo"]),
        repo_path=str(payload["repo_path"]),
        base_commit=payload.get("base_commit") if isinstance(payload.get("base_commit"), str) or payload.get("base_commit") is None else str(payload.get("base_commit")),
        oracle_files=list(payload.get("oracle_files") or []),
        control=_strategy_result_from_dict(dict(payload["control"])),  # type: ignore[arg-type]
        exvisit=_strategy_result_from_dict(dict(payload["exvisit"])),  # type: ignore[arg-type]
    )


def _results_payload(results: Sequence[CaseResult], input_cost_per_1m: Optional[float]) -> Dict[str, object]:
    return {
        "summary": summarize_results(results, input_cost_per_1m=input_cost_per_1m),
        "results": [
            {
                "case_id": result.case_id,
                "repo": result.repo,
                "repo_path": result.repo_path,
                "base_commit": result.base_commit,
                "oracle_files": result.oracle_files,
                "control": asdict(result.control),
                "exvisit": asdict(result.exvisit),
            }
            for result in results
        ],
    }


def _load_existing_results(path: Optional[Path], case_ids: Set[str]) -> Tuple[List[str], Dict[str, CaseResult]]:
    if path is None or not path.exists():
        return [], {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    order: List[str] = []
    out: Dict[str, CaseResult] = {}
    for item in payload.get("results", []):
        if not isinstance(item, dict):
            continue
        case_id = item.get("case_id")
        if not isinstance(case_id, str) or case_id not in case_ids:
            continue
        result = _case_result_from_dict(item)
        out[case_id] = result
        order.append(case_id)
    return order, out


def run_benchmark(
    cases: Sequence[BenchmarkCase],
    control_runner_cmd: Optional[str],
    exvisit_runner_cmd: Optional[str],
    input_cost_per_1m: Optional[float],
    output_path: Optional[Path] = None,
    resume: bool = True,
    pricing: Optional[PricingConfig] = None,
    control_traj_template: Optional[str] = None,
    exvisit_traj_template: Optional[str] = None,
    workspace_root: Optional[Path] = None,
) -> Dict[str, object]:
    requested_ids = {case.case_id for case in cases}
    ordered_ids, results_by_id = _load_existing_results(output_path, requested_ids) if resume else ([], {})
    for case in cases:
        if case.case_id in results_by_id:
            continue
        repo_path = Path(case.repo_path)
        ensure_checkout(repo_path, case.base_commit)
        control_workspace = prepare_strategy_workspace(case, "control", workspace_root)
        control = control_strategy(
            case,
            runner_cmd=control_runner_cmd,
            pricing=pricing,
            trajectory_template=control_traj_template,
            workspace_path=str(control_workspace) if control_workspace is not None else None,
        )
        exvisit_workspace = prepare_strategy_workspace(case, "exvisit", workspace_root)
        exvisit = exvisit_strategy(
            case,
            runner_cmd=exvisit_runner_cmd,
            pricing=pricing,
            trajectory_template=exvisit_traj_template,
            workspace_path=str(exvisit_workspace) if exvisit_workspace is not None else None,
        )
        result = CaseResult(
            case_id=case.case_id,
            repo=case.repo,
            repo_path=case.repo_path,
            base_commit=case.base_commit,
            oracle_files=case.oracle_files,
            control=control,
            exvisit=exvisit,
        )
        results_by_id[case.case_id] = result
        ordered_ids.append(case.case_id)
        if output_path is not None:
            partial_results = [results_by_id[case_id] for case_id in ordered_ids]
            _write_json(output_path, _results_payload(partial_results, input_cost_per_1m=input_cost_per_1m))
    final_results = [results_by_id[case_id] for case_id in ordered_ids]
    return _results_payload(final_results, input_cost_per_1m=input_cost_per_1m)


def print_human_summary(payload: Dict[str, object]) -> None:
    summary = payload["summary"]
    control = summary["control"]
    exvisit = summary["exvisit"]
    delta = summary["delta"]
    print(f"cases: {summary['cases']}")
    print(f"control: avg_tokens={control['avg_tokens']:.1f} avg_steps={control['avg_steps']:.1f} avg_context_rot={control['avg_context_rot']:.2f} oracle_hit@1={control['oracle_hit_at_1_rate']:.2%}")
    print(f"exvisit  : avg_tokens={exvisit['avg_tokens']:.1f} avg_steps={exvisit['avg_steps']:.1f} avg_context_rot={exvisit['avg_context_rot']:.2f} oracle_hit@1={exvisit['oracle_hit_at_1_rate']:.2%}")
    print(f"delta  : token_reduction={delta['token_reduction_pct']:.1f}% step_reduction={delta['step_reduction_pct']:.1f}% context_rot_reduction={delta['context_rot_reduction_pct']:.1f}%")
    if control.get("avg_cost_to_resolve_usd") is not None:
        print(f"cost   : control=${control['avg_cost_to_resolve_usd']:.6f} exvisit=${exvisit['avg_cost_to_resolve_usd']:.6f} reduction={delta['cost_to_resolve_reduction_pct']:.1f}%")
    elif control.get("avg_input_cost") is not None:
        print(f"cost   : control=${control['avg_input_cost']:.6f} exvisit=${exvisit['avg_input_cost']:.6f} reduction={delta['input_cost_reduction_pct']:.1f}%")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="swebench-lite-harness")
    sub = parser.add_subparsers(dest="command", required=True)

    pre = sub.add_parser("precompute", help="materialize repos and generate exvisit files")
    pre.add_argument("--dataset", choices=["swebench-lite", "jsonl"], default="swebench-lite")
    pre.add_argument("--split", default="test")
    pre.add_argument("--limit", type=int, default=None)
    pre.add_argument("--repos", nargs="*", default=None, help="Filter to specific repo slugs like psf/requests")
    pre.add_argument("--local-cases", default=None)
    pre.add_argument("--cache-dir", required=True)
    pre.add_argument("--manifest", required=True)

    run = sub.add_parser("run", help="execute control vs exvisit navigation benchmark")
    run.add_argument("--manifest", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--input-cost-per-1m", type=float, default=None)
    run.add_argument("--pricing-file", default=None, help="JSON file with input_base_per_1m, cache_write_per_1m, cache_read_per_1m, output_per_1m")
    run.add_argument("--control-runner-cmd", default=None, help="Optional shell template with {repo_path} {exvisit_path} {issue_text} {case_id}")
    run.add_argument("--exvisit-runner-cmd", default=None, help="Optional shell template with {repo_path} {exvisit_path} {issue_text} {case_id}")
    run.add_argument("--control-traj-template", default=None, help="Optional path template for the control trajectory JSON file")
    run.add_argument("--exvisit-traj-template", default=None, help="Optional path template for the exvisit trajectory JSON file")
    run.add_argument("--workspace-root", default=None, help="Optional root for per-strategy copy-on-start workspaces used by external runners")
    run.add_argument("--no-resume", action="store_true", help="Disable resume from an existing results file")
    run.add_argument("--limit", type=int, default=None, help="Limit number of cases to run")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "precompute":
        manifest = precompute_cases(
            dataset=args.dataset,
            split=args.split,
            limit=args.limit,
            repos=set(args.repos) if args.repos else None,
            local_cases=Path(args.local_cases) if args.local_cases else None,
            cache_dir=Path(args.cache_dir),
            manifest_path=Path(args.manifest),
        )
        print(f"wrote manifest {args.manifest} with {len(manifest['cases'])} case(s)")
        return 0

    if args.command == "run":
        cases = load_manifest(Path(args.manifest))
        if args.limit:
            cases = cases[:args.limit]
        out_path = Path(args.out)
        payload = run_benchmark(
            cases,
            control_runner_cmd=args.control_runner_cmd,
            exvisit_runner_cmd=args.exvisit_runner_cmd,
            input_cost_per_1m=args.input_cost_per_1m,
            output_path=out_path,
            resume=not args.no_resume,
            pricing=load_pricing_config(Path(args.pricing_file) if args.pricing_file else None),
            control_traj_template=args.control_traj_template,
            exvisit_traj_template=args.exvisit_traj_template,
            workspace_root=Path(args.workspace_root) if args.workspace_root else None,
        )
        _write_json(out_path, payload)
        print_human_summary(payload)
        print(f"wrote results {args.out}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())

