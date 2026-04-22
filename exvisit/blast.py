"""Blast-radius context bundler inspired by the legacy TS exvisit CLI.

The old `exvisit blast` command was not a distinct search algorithm; it was a
manifest-first bundle builder anchored on an error or issue description. This
module ports the useful architecture into exvisit_pro while staying faithful to
the `.exv` DSL as the source of truth.
"""
from __future__ import annotations

import ast
import json
import re
import warnings
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .ast import exvisitDoc, Node


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "blast_presets.json"
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "bug", "by", "can",
    "case", "does", "error", "for", "from", "get", "if", "in", "into",
    "is", "it", "its", "just", "not", "of", "on", "or", "should",
    "that", "the", "their", "there", "this", "to", "when", "with",
    "without", "while", "under", "using", "use", "used", "returns",
    "return", "issue", "problem", "traceback", "exception", "failure",
}


@dataclass
class BlastPreset:
    name: str
    max_files: int
    max_snippets: int
    hops: int
    max_snippet_lines: int
    direction: str = "both"
    prefer_same_file: bool = True
    summary_budget_tokens: int = 900


@dataclass
class BlastSelectionReason:
    node_id: str
    phase: str
    reason: str
    score: int


@dataclass
class BlastSnippet:
    file_path: str
    label: str
    reason: str
    code: str


@dataclass
class BlastBundle:
    preset: str
    anchor: str
    anchor_file: str
    confidence: float
    selected_nodes: List[str]
    selected_files: List[str]
    omitted_node_count: int
    omitted_file_count: int
    token_estimate: int
    selection_reasons: List[BlastSelectionReason] = field(default_factory=list)
    snippets: List[BlastSnippet] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class TraceFrame:
    file_path: str
    line: int
    symbol: Optional[str] = None


def estimate_tokens(text: str) -> int:
    try:
        import tiktoken  # type: ignore

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return max(1, len(text) // 4)


def load_blast_presets(config_path: Optional[str] = None) -> Dict[str, BlastPreset]:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    payload = json.loads(path.read_text(encoding="utf-8"))
    presets: Dict[str, BlastPreset] = {}
    for name, cfg in payload.get("presets", {}).items():
        presets[name] = BlastPreset(
            name=name,
            max_files=int(cfg["max_files"]),
            max_snippets=int(cfg["max_snippets"]),
            hops=int(cfg["hops"]),
            max_snippet_lines=int(cfg["max_snippet_lines"]),
            direction=str(cfg.get("direction", "both")),
            prefer_same_file=bool(cfg.get("prefer_same_file", True)),
            summary_budget_tokens=int(cfg.get("summary_budget_tokens", 900)),
        )
    return presets


def extract_issue_terms(text: str) -> Tuple[List[str], List[str]]:
    code_terms: List[str] = []
    fenced_blocks = re.findall(r"```(.*?)```", text, flags=re.DOTALL)
    scrubbed = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)

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

    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_\.]{2,}", scrubbed):
        if "." in token or "_" in token or any(ch.isupper() for ch in token[1:]):
            code_terms.append(token)

    keywords: List[str] = []
    seen_keywords = set()
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", scrubbed.lower()):
        if token in STOPWORDS or token in seen_keywords:
            continue
        seen_keywords.add(token)
        keywords.append(token)

    dedup_code: List[str] = []
    seen_code = set()
    for token in code_terms:
        key = token.lower()
        if key in seen_code:
            continue
        seen_code.add(key)
        dedup_code.append(token)
    return dedup_code, keywords


def extract_trace_frames(text: str) -> List[TraceFrame]:
    frames: List[TraceFrame] = []
    seen = set()
    patterns = [
        re.compile(r'File "(?P<path>[^"\n]+)", line (?P<line>\d+), in (?P<symbol>[A-Za-z_][A-Za-z0-9_\.]*)'),
        re.compile(r'(?P<path>(?:[A-Za-z]:[\\/])?[^:\s"\']+?\.py):(?P<line>\d+)(?::\d+)?'),
    ]
    for pattern in patterns:
        for match in pattern.finditer(text):
            path = match.group("path").replace("\\", "/")
            line = int(match.group("line"))
            symbol = match.groupdict().get("symbol")
            key = (path.lower(), line, (symbol or "").lower())
            if key in seen:
                continue
            seen.add(key)
            frames.append(TraceFrame(file_path=path, line=line, symbol=symbol))
    return frames


def _path_match_score(node_path: Optional[str], frame_path: str) -> int:
    if not node_path:
        return 0
    node_norm = node_path.replace("\\", "/").lower()
    frame_norm = frame_path.replace("\\", "/").lower()
    if frame_norm.endswith(node_norm) or node_norm.endswith(frame_norm):
        return 18
    if Path(frame_norm).name == Path(node_norm).name:
        return 12
    if Path(frame_norm).stem == Path(node_norm).stem:
        return 8
    return 0


def resolve_repo_file(repo_root: Path, src_path: Optional[str]) -> Optional[Path]:
    if not src_path:
        return None
    direct = repo_root / src_path
    if direct.exists():
        return direct
    name = Path(src_path).name
    for path in repo_root.rglob("*.py"):
        if path.name == name:
            return path
    return None


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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = getattr(node, "lineno", None)
            end = getattr(node, "end_lineno", start)
            if start and end:
                ranges[node.name] = (start, end)
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    start = getattr(child, "lineno", None)
                    end = getattr(child, "end_lineno", start)
                    if start and end:
                        ranges[f"{node.name}.{child.name}"] = (start, end)
                        ranges.setdefault(child.name, (start, end))
    return ranges


def rank_nodes_for_text(doc: exvisitDoc, repo_root: Path, text: str) -> List[Tuple[int, Node, List[str]]]:
    return _score_nodes(doc, repo_root, text)


def _best_frame_line(node: Node, frames: Sequence[TraceFrame]) -> Optional[int]:
    for frame in frames:
        if _path_match_score(node.src_path, frame.file_path) <= 0:
            continue
        if node.line_range and node.line_range[0] <= frame.line <= node.line_range[1]:
            return frame.line
        if not node.line_range:
            return frame.line
    return None


def _render_snippet(lines: Sequence[str], lo: int, hi: int) -> str:
    return "\n".join(f"{idx}: {lines[idx - 1]}" for idx in range(lo, hi + 1))


def _clip_focus_window(bound_lo: int, bound_hi: int, focus_line: int, max_context_lines: int) -> Tuple[int, int]:
    span = bound_hi - bound_lo + 1
    if span <= max_context_lines:
        return bound_lo, bound_hi
    half = max_context_lines // 2
    lo = max(bound_lo, focus_line - half)
    hi = min(bound_hi, lo + max_context_lines - 1)
    lo = max(bound_lo, hi - max_context_lines + 1)
    return lo, hi


def choose_best_snippet(
    file_path: Path,
    text: str,
    max_context_lines: int,
    line_range: Optional[Tuple[int, int]] = None,
) -> Tuple[str, str]:
    lines = file_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    if not lines:
        return f"{file_path.name}:0-0 (empty-file)", ""
    ranges = parse_python_symbol_ranges(file_path)
    code_terms, keywords = extract_issue_terms(text)
    trace_frames = [frame for frame in extract_trace_frames(text) if _path_match_score(file_path.name, frame.file_path) > 0 or _path_match_score(file_path.as_posix(), frame.file_path) > 0]
    bound_lo = 1
    bound_hi = max(1, len(lines))
    if line_range:
        bound_lo = max(1, min(line_range[0], line_range[1]))
        bound_hi = min(max(1, len(lines)), max(line_range[0], line_range[1]))
        ranges = {
            symbol: (max(start, bound_lo), min(end, bound_hi))
            for symbol, (start, end) in ranges.items()
            if not (end < bound_lo or start > bound_hi)
        }

    best_symbol: Optional[Tuple[str, int, int]] = None
    best_score = -1
    for symbol, (start, end) in ranges.items():
        sym_low = symbol.lower()
        score = 0
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
        lo = max(bound_lo, start - half_pad)
        hi = min(bound_hi, end + half_pad)
        if hi - lo + 1 > max_context_lines:
            lo, hi = _clip_focus_window(bound_lo, bound_hi, start, max_context_lines)
        snippet = _render_snippet(lines, lo, hi)
        return f"{file_path.name}:{lo}-{hi} ({symbol})", snippet

    for frame in trace_frames:
        if bound_lo <= frame.line <= bound_hi:
            lo, hi = _clip_focus_window(bound_lo, bound_hi, frame.line, max_context_lines)
            return f"{file_path.name}:{lo}-{hi} (trace-line {frame.line})", _render_snippet(lines, lo, hi)

    if line_range:
        if bound_hi - bound_lo + 1 <= max_context_lines:
            return f"{file_path.name}:{bound_lo}-{bound_hi} (exvisit-lines)", _render_snippet(lines, bound_lo, bound_hi)
        lo = bound_lo
        hi = min(bound_hi, bound_lo + max_context_lines - 1)
        return f"{file_path.name}:{lo}-{hi} (exvisit-lines)", _render_snippet(lines, lo, hi)

    hi = min(len(lines), max_context_lines)
    snippet = _render_snippet(lines, 1, hi)
    return f"{file_path.name}:1-{hi} (file-head)", snippet


def _neighbors(doc: exvisitDoc, node_fqn: str, hops: int = 1, direction: str = "both") -> Set[str]:
    out_adj: Dict[str, Set[str]] = {}
    in_adj: Dict[str, Set[str]] = {}
    for edge in doc.edges:
        out_adj.setdefault(edge.src, set()).add(edge.dst)
        in_adj.setdefault(edge.dst, set()).add(edge.src)

    name_map: Dict[str, List[str]] = {}
    for node in doc.all_nodes():
        name_map.setdefault(node.name, []).append(node.fqn)
        name_map[node.fqn] = [node.fqn]

    target_node = doc.find_node(node_fqn)
    if not target_node:
        return set()
    seed = target_node.name

    frontier = {seed}
    visited = {seed}
    for _ in range(hops):
        nxt = set()
        for value in frontier:
            if direction in ("out", "both"):
                nxt |= out_adj.get(value, set())
            if direction in ("in", "both"):
                nxt |= in_adj.get(value, set())
        nxt -= visited
        visited |= nxt
        frontier = nxt

    result: Set[str] = set()
    for value in visited:
        for fqn in name_map.get(value, [value]):
            if doc.find_node(fqn):
                result.add(fqn)
    return result


def _score_nodes(doc: exvisitDoc, repo_root: Path, text: str) -> List[Tuple[int, Node, List[str]]]:
    code_terms, keywords = extract_issue_terms(text)
    trace_frames = extract_trace_frames(text)
    lowered = text.lower()
    src_name_counts = Counter(Path(term).name.lower() for term in code_terms if term.lower().endswith(".py"))
    symbol_terms = [term for term in code_terms if "." in term and not term.lower().endswith(".py")]
    range_cache: Dict[str, Dict[str, Tuple[int, int]]] = {}
    scored: List[Tuple[int, Node, List[str]]] = []

    for node in doc.all_nodes():
        src_path = (node.src_path or "").lower()
        src_name = Path(node.src_path or "").name.lower()
        name = node.name.lower()
        ns = node.ns_path.lower()
        score = 0
        reasons: List[str] = []
        if node.src_path and Path(node.src_path).stem.lower() in lowered:
            score += 8
            reasons.append("file-stem-in-text")
        if src_name in lowered:
            score += 8
            reasons.append("file-name-in-text")
        for frame in trace_frames:
            path_score = _path_match_score(node.src_path, frame.file_path)
            if path_score > 0:
                score += path_score
                reasons.append("trace-file")
                if node.line_range and node.line_range[0] <= frame.line <= node.line_range[1]:
                    score += 24
                    reasons.append("trace-line-in-node")
            if frame.symbol:
                sym_low = frame.symbol.lower()
                if sym_low == name or sym_low.endswith("." + name) or name.endswith("." + sym_low):
                    score += 10
                    reasons.append("trace-symbol")
        if src_name in src_name_counts:
            delta = 12 * src_name_counts[src_name]
            score += delta
            reasons.append("path-term")
        if name in lowered:
            score += 8
            reasons.append("node-name-in-text")

        for term in code_terms:
            low = term.lower()
            tail = low.split(".")[-1]
            if "/" in low and (src_path.endswith(low) or src_name == Path(low).name):
                score += 12
                reasons.append("exact-path-match")
            if low == name or low.endswith("." + name):
                score += 10
                reasons.append("exact-symbol-match")
            if tail == name:
                score += 6
                reasons.append("symbol-tail-match")
            if tail in src_path:
                score += 6
                reasons.append("path-tail-match")

        if symbol_terms and node.src_path:
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
                        reasons.append("ast-symbol-exact")
                    elif tail in lowered_keys:
                        score += 20
                        reasons.append("ast-symbol-tail")
                for frame in trace_frames:
                    if not frame.symbol:
                        continue
                    low = frame.symbol.lower()
                    tail = low.split(".")[-1]
                    if low in lowered_keys:
                        score += 20
                        reasons.append("trace-ast-symbol-exact")
                    elif tail in lowered_keys:
                        score += 12
                        reasons.append("trace-ast-symbol-tail")

        for kw in keywords:
            if kw in name:
                score += 3
                reasons.append("keyword-in-node")
            if kw in src_path:
                score += 3
                reasons.append("keyword-in-path")
            if kw in ns:
                score += 1
                reasons.append("keyword-in-namespace")

        if score > 0:
            scored.append((score, node, reasons))

    scored.sort(key=lambda item: (-item[0], item[1].fqn))
    return scored


def build_blast_bundle(
    doc: exvisitDoc,
    repo_root: str,
    text: str,
    preset_name: str = "test-fix",
    config_path: Optional[str] = None,
) -> BlastBundle:
    repo = Path(repo_root)
    presets = load_blast_presets(config_path)
    preset = presets.get(preset_name, presets.get("default"))
    if preset is None:
        raise KeyError("no blast presets available")

    ranked = rank_nodes_for_text(doc, repo, text)
    if not ranked:
        raise KeyError("could not resolve a blast anchor from the provided text")

    anchor_score, anchor, anchor_reasons = ranked[0]
    second_score = ranked[1][0] if len(ranked) > 1 else 0
    confidence = round(anchor_score / max(anchor_score + second_score, 1), 2)
    keep_fqns = _neighbors(doc, anchor.fqn, hops=preset.hops, direction=preset.direction)
    keep_fqns.add(anchor.fqn)

    rank_map: Dict[str, Tuple[int, List[str]]] = {node.fqn: (score, reasons) for score, node, reasons in ranked}
    neighbors = [node for node in doc.all_nodes() if node.fqn in keep_fqns]

    def neighbor_sort_key(node: Node):
        match_score = rank_map.get(node.fqn, (0, []))[0]
        same_file = 1 if preset.prefer_same_file and node.src_path == anchor.src_path else 0
        return (node.fqn != anchor.fqn, -same_file, -match_score, node.fqn)

    selected_nodes = sorted(neighbors, key=neighbor_sort_key)[:preset.max_files]
    selected_files: List[str] = []
    snippets: List[BlastSnippet] = []
    selection_reasons: List[BlastSelectionReason] = [
        BlastSelectionReason(
            node_id=anchor.fqn,
            phase="anchor",
            reason=", ".join(anchor_reasons[:4]) or "top-ranked lexical/AST anchor",
            score=anchor_score,
        )
    ]

    seen_files = set()
    for node in selected_nodes:
        score, reasons = rank_map.get(node.fqn, (0, []))
        if node.fqn != anchor.fqn:
            selection_reasons.append(
                BlastSelectionReason(
                    node_id=node.fqn,
                    phase="neighbor",
                    reason=(", ".join(reasons[:3]) + "; " if reasons else "") + f"within {preset.hops}-hop blast radius of {anchor.name}",
                    score=score,
                )
            )
        file_path = resolve_repo_file(repo, node.src_path)
        if file_path is None:
            continue
        rel = file_path.relative_to(repo).as_posix()
        if rel in seen_files:
            continue
        seen_files.add(rel)
        selected_files.append(rel)
        if len(snippets) < preset.max_snippets:
            label, code = choose_best_snippet(file_path, text, preset.max_snippet_lines, line_range=node.line_range)
            reason = "anchor file" if node.fqn == anchor.fqn else f"blast neighbor of {anchor.name}"
            snippets.append(BlastSnippet(file_path=rel, label=label, reason=reason, code=code))

    selected_node_ids = [node.fqn for node in selected_nodes]
    omitted_node_count = max(0, len(doc.all_nodes()) - len(selected_node_ids))
    omitted_file_count = max(0, len({n.src_path for n in doc.all_nodes() if n.src_path}) - len(selected_files))
    token_estimate = estimate_tokens(text)
    token_estimate += sum(estimate_tokens(snippet.code) for snippet in snippets)

    warnings: List[str] = []
    if preset_name not in presets:
        warnings.append(f"preset '{preset_name}' not found; used '{preset.name}'")

    return BlastBundle(
        preset=preset.name,
        anchor=anchor.fqn,
        anchor_file=anchor.src_path or "",
        confidence=confidence,
        selected_nodes=selected_node_ids,
        selected_files=selected_files,
        omitted_node_count=omitted_node_count,
        omitted_file_count=omitted_file_count,
        token_estimate=token_estimate,
        selection_reasons=selection_reasons,
        snippets=snippets,
        warnings=warnings,
    )


def bundle_to_json(bundle: BlastBundle) -> str:
    return json.dumps(asdict(bundle), indent=2)


def render_blast_markdown(bundle: BlastBundle, context_text: str) -> str:
    lines = [
        "# exvisit Blast Bundle",
        "",
        "## Summary",
        f"- Preset: `{bundle.preset}`",
        f"- Anchor: `{bundle.anchor}`",
        f"- Anchor File: `{bundle.anchor_file}`",
        f"- Confidence: `{bundle.confidence}`",
        f"- Token Estimate: `{bundle.token_estimate}`",
        f"- Selected Nodes: `{len(bundle.selected_nodes)}`",
        f"- Selected Files: `{len(bundle.selected_files)}`",
        f"- Omitted Nodes: `{bundle.omitted_node_count}`",
        f"- Omitted Files: `{bundle.omitted_file_count}`",
        "",
        "## Trigger",
        "```text",
        context_text.strip(),
        "```",
        "",
        "## Selection Reasons",
    ]
    for reason in bundle.selection_reasons:
        lines.append(f"- `{reason.node_id}` :: {reason.phase} :: {reason.reason} (score {reason.score})")
    if bundle.warnings:
        lines.extend(["", "## Warnings"])
        for warning in bundle.warnings:
            lines.append(f"- {warning}")
    lines.extend(["", "## Snippets", ""])
    for snippet in bundle.snippets:
        lines.append(f"### `{snippet.file_path}` ({snippet.label})")
        lines.append(f"Reason: {snippet.reason}")
        lines.append("```python")
        lines.append(snippet.code)
        lines.append("```")
        lines.append("")
    return "\n".join(lines)

