"""Blast-radius context bundler inspired by the legacy TS exvisit CLI.

The old `exvisit blast` command was not a distinct search algorithm; it was a
manifest-first bundle builder anchored on an error or issue description. This
module ports the useful architecture into exvisit_pro while staying faithful to
the `.exv` DSL as the source of truth.
"""
from __future__ import annotations

import ast
import json
import os
import re
import warnings
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from ._data import read_text as _read_packaged_data
from .ast import exvisitDoc, Node
from .graph_meta import GraphMeta, load_for as _load_meta_for
from . import scoring_v2 as _v2


DEFAULT_CONFIG_NAME = "blast_presets.json"
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
class _SpatialNeighbor:
    fqn: str
    structural_score: float
    hop: int
    via: str


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
    if config_path:
        payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
    else:
        payload = json.loads(_read_packaged_data(DEFAULT_CONFIG_NAME))
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


def _meta_weighted_neighbors(
    doc: exvisitDoc,
    meta: Optional[GraphMeta],
    anchor_fqns: Sequence[str],
    hops: int,
    direction: str,
) -> Dict[str, _SpatialNeighbor]:
    if meta is None or not meta.edges_by_type or hops <= 0:
        return {}

    nodes_by_fqn = {node.fqn: node for node in doc.all_nodes()}
    seed_fqns = [fqn for fqn in anchor_fqns if fqn in nodes_by_fqn]
    if not seed_fqns:
        return {}

    out_adj: Dict[str, List[Tuple[str, float, str]]] = {}
    in_adj: Dict[str, List[Tuple[str, float, str]]] = {}
    for edge_type, pairs in meta.edges_by_type.items():
        weight = float(meta.edge_priors.get(edge_type, 0.1))
        if weight <= 0:
            continue
        for src, dst in pairs:
            if src not in nodes_by_fqn or dst not in nodes_by_fqn:
                continue
            out_adj.setdefault(src, []).append((dst, weight, edge_type))
            in_adj.setdefault(dst, []).append((src, weight, edge_type))

    best: Dict[str, _SpatialNeighbor] = {}
    frontier: Dict[str, float] = {fqn: 1.0 for fqn in seed_fqns}
    anchor_set = set(seed_fqns)

    for hop in range(1, hops + 1):
        if not frontier:
            break
        next_frontier: Dict[str, Tuple[float, str]] = {}
        hop_decay = 0.82 ** (hop - 1)
        for src, carry in frontier.items():
            edge_stream: List[Tuple[str, float, str]] = []
            if direction in ("out", "both"):
                edge_stream.extend(out_adj.get(src, []))
            if direction in ("in", "both"):
                edge_stream.extend(in_adj.get(src, []))
            for dst, weight, edge_type in edge_stream:
                if dst in anchor_set:
                    continue
                propagated = carry * weight * hop_decay
                if propagated <= 0:
                    continue
                prev_frontier = next_frontier.get(dst)
                if prev_frontier is None or propagated > prev_frontier[0]:
                    next_frontier[dst] = (propagated, edge_type)
                prev_best = best.get(dst)
                if prev_best is None or propagated > prev_best.structural_score:
                    best[dst] = _SpatialNeighbor(
                        fqn=dst,
                        structural_score=propagated,
                        hop=hop,
                        via=edge_type,
                    )
        frontier = {dst: score for dst, (score, _edge_type) in next_frontier.items()}
    return best


def _neighbor_budget(max_files: int, anchor_count: int, confidence: float, low_margin: bool) -> int:
    remaining = max(0, max_files - anchor_count)
    if remaining == 0:
        return 0
    if low_margin or anchor_count > 1:
        return remaining
    if confidence >= 0.55:
        return min(2, remaining)
    if confidence >= 0.40:
        return min(3, remaining)
    return min(remaining, 4)


def _sibling_budget(max_files: int, selected_count: int, confidence: float, low_margin: bool) -> int:
    remaining = max(0, max_files - selected_count)
    if remaining == 0:
        return 0
    if low_margin:
        return min(3, remaining)
    if confidence < 0.30:
        return min(3, remaining)
    return min(2, remaining)


def _cluster_key(node: Node, meta: Optional[GraphMeta]) -> str:
    if meta is not None:
        node_meta = meta.nodes.get(node.fqn)
        if node_meta and node_meta.cluster:
            return node_meta.cluster
    if node.src_path:
        return str(Path(node.src_path).parent).replace("\\", "/")
    return ""


def _anchor_has_signal(
    anchors: Sequence[_v2.ScoredNode],
    component: str,
    threshold: float = 0.5,
) -> bool:
    return any(abs(anchor.components.get(component, 0.0)) >= threshold for anchor in anchors)


def _inject_precision_guards(
    scored: Sequence[_v2.ScoredNode],
    anchors: Sequence[_v2.ScoredNode],
    selected_nodes: List[Node],
    selected_fqns: Set[str],
    neighbor_reasons: Dict[str, str],
    max_files: int,
) -> None:
    if len(selected_nodes) >= max_files:
        return

    top_window = list(scored[: max(12, max_files * 4)])

    def add_guard(predicate, reason: str, limit: int = 1) -> None:
        remaining = max_files - len(selected_nodes)
        if remaining <= 0:
            return
        added = 0
        for scored_node in top_window:
            if added >= min(limit, remaining):
                break
            node = scored_node.node
            if node.fqn in selected_fqns:
                continue
            if not predicate(scored_node):
                continue
            selected_nodes.append(node)
            selected_fqns.add(node.fqn)
            neighbor_reasons[node.fqn] = reason
            added += 1

    if not _anchor_has_signal(anchors, "explicit_path"):
        add_guard(
            lambda s: s.components.get("explicit_path", 0.0) > 0.0,
            "precision-guard: literal path cited in issue",
        )
    if not _anchor_has_signal(anchors, "mgmt_command"):
        add_guard(
            lambda s: s.components.get("mgmt_command", 0.0) > 0.0,
            "precision-guard: management command named in issue",
        )
    if not _anchor_has_signal(anchors, "error_code"):
        add_guard(
            lambda s: s.components.get("error_code", 0.0) > 0.0,
            "precision-guard: error/check code named in issue",
        )
    if not _anchor_has_signal(anchors, "upper_const"):
        add_guard(
            lambda s: s.components.get("upper_const", 0.0) > 0.0,
            "precision-guard: settings constant named in issue",
            limit=2,
        )
    if not _anchor_has_signal(anchors, "domain", threshold=0.4):
        add_guard(
            lambda s: abs(s.components.get("domain", 0.0)) >= 0.4 and (
                s.components.get("stem", 0.0) > 0.0
                or s.components.get("path", 0.0) > 0.0
                or s.components.get("explicit_path", 0.0) > 0.0
                or s.components.get("symbol_exact", 0.0) > 0.0
            ),
            "precision-guard: issue vocabulary points to a different subsystem",
        )


def _weak_neighbor_penalty(
    scored_node: _v2.ScoredNode,
    meta: Optional[GraphMeta],
) -> float:
    if meta is None:
        return 1.0
    node_meta = meta.nodes.get(scored_node.node.fqn)
    if node_meta is None:
        return 1.0

    strong_signal = (
        scored_node.components.get("explicit_path", 0.0) > 0.0
        or scored_node.components.get("path", 0.0) > 0.0
        or scored_node.components.get("stem", 0.0) > 0.0
        or scored_node.components.get("symbol_exact", 0.0) > 0.0
        or scored_node.components.get("mgmt_command", 0.0) > 0.0
        or scored_node.components.get("upper_const", 0.0) > 0.0
        or abs(scored_node.components.get("domain", 0.0)) >= 0.4
        or scored_node.components.get("lex", 0.0) > 1.5
        or scored_node.components.get("term_idf", 0.0) > 1.0
    )
    if strong_signal:
        return 1.0
    if node_meta.kind == "registry":
        return 0.65
    if node_meta.kind == "test":
        return 0.25
    if node_meta.kind == "migration":
        return 0.20
    return 1.0


def _select_v2_nodes(
    doc: exvisitDoc,
    meta: Optional[GraphMeta],
    scored: Sequence[_v2.ScoredNode],
    anchors: Sequence[_v2.ScoredNode],
    preset: BlastPreset,
    confidence: float,
    low_margin: bool,
) -> Tuple[List[Node], Dict[str, str]]:
    rank_map: Dict[str, _v2.ScoredNode] = {s.node.fqn: s for s in scored}
    rank_index: Dict[str, int] = {s.node.fqn: idx for idx, s in enumerate(scored)}
    selected_nodes: List[Node] = []
    neighbor_reasons: Dict[str, str] = {}
    selected_fqns: Set[str] = set()

    for anchor in anchors:
        if anchor.node.fqn in selected_fqns:
            continue
        selected_nodes.append(anchor.node)
        selected_fqns.add(anchor.node.fqn)
        if len(selected_nodes) >= preset.max_files:
            return selected_nodes, neighbor_reasons

    _inject_precision_guards(
        scored,
        anchors,
        selected_nodes,
        selected_fqns,
        neighbor_reasons,
        preset.max_files,
    )

    neighbor_budget = _neighbor_budget(preset.max_files, len(selected_nodes), confidence, low_margin)
    primary_score = anchors[0].score if anchors else 0.0
    rank_cap = max(6, preset.max_files * (3 if low_margin else 2))

    meta_neighbors = _meta_weighted_neighbors(
        doc,
        meta,
        [anchor.node.fqn for anchor in anchors],
        preset.hops,
        preset.direction,
    )
    if meta_neighbors:
        ranked_neighbors = []
        for candidate in meta_neighbors.values():
            scored_node = rank_map.get(candidate.fqn)
            if scored_node is None:
                continue
            idx = rank_index.get(candidate.fqn, 10**9)
            adjusted_structural = candidate.structural_score * _weak_neighbor_penalty(scored_node, meta)
            if idx > rank_cap and adjusted_structural < 0.12:
                continue
            ranked_neighbors.append((candidate, scored_node, idx, adjusted_structural))
        ranked_neighbors.sort(
            key=lambda item: (
                -item[3],
                item[2],
                -item[1].score,
                item[1].node.fqn,
            )
        )
        for candidate, scored_node, _idx, _adjusted_structural in ranked_neighbors:
            if neighbor_budget <= 0:
                break
            if candidate.fqn in selected_fqns:
                continue
            selected_nodes.append(scored_node.node)
            selected_fqns.add(candidate.fqn)
            neighbor_reasons[candidate.fqn] = (
                f"typed-{candidate.via} edge within {candidate.hop}-hop spatial radius"
            )
            neighbor_budget -= 1
    else:
        keep_fqns: Set[str] = set()
        for anchor in anchors:
            keep_fqns |= _neighbors(doc, anchor.node.fqn, hops=preset.hops, direction=preset.direction)
        fallback_neighbors = [
            scored_node for scored_node in scored
            if scored_node.node.fqn in keep_fqns and scored_node.node.fqn not in selected_fqns
        ]
        for scored_node in fallback_neighbors:
            if neighbor_budget <= 0:
                break
            idx = rank_index.get(scored_node.node.fqn, 10**9)
            if idx > rank_cap and scored_node.score <= 0:
                continue
            selected_nodes.append(scored_node.node)
            selected_fqns.add(scored_node.node.fqn)
            neighbor_reasons[scored_node.node.fqn] = (
                f"within {preset.hops}-hop blast radius of {anchors[0].node.name}"
            )
            neighbor_budget -= 1

    sibling_budget = _sibling_budget(preset.max_files, len(selected_nodes), confidence, low_margin)
    if sibling_budget > 0:
        selected_clusters = {
            _cluster_key(node, meta)
            for node in selected_nodes
            if _cluster_key(node, meta)
        }
        ratio_floor = 0.25 if low_margin else 0.35
        score_floor = primary_score * ratio_floor if primary_score > 0 else 0.0
        rank_cap_sib = max(16, preset.max_files * 5)
        for scored_node in scored:
            if sibling_budget <= 0:
                break
            node = scored_node.node
            if node.fqn in selected_fqns or not node.src_path:
                continue
            cluster = _cluster_key(node, meta)
            if cluster not in selected_clusters:
                continue
            if scored_node.score < score_floor:
                continue
            idx = rank_index.get(node.fqn, 10**9)
            if idx > rank_cap_sib:
                continue
            selected_nodes.append(node)
            selected_fqns.add(node.fqn)
            neighbor_reasons[node.fqn] = "same-cluster fallback near anchor score"
            sibling_budget -= 1

    # ---- Package __init__.py injection ------------------------------------
    # If we selected any file from a Python package, always consider that
    # package's __init__.py — it defines the public API and is often the
    # oracle file for namespace-adjacent issues.
    init_budget = max(0, preset.max_files - len(selected_nodes))
    if init_budget > 0:
        selected_dirs: Set[str] = set()
        for node in selected_nodes:
            if node.src_path:
                d = str(Path(node.src_path).parent).replace("\\", "/")
                if d:
                    selected_dirs.add(d)
        for scored_node in scored:
            if init_budget <= 0:
                break
            node = scored_node.node
            if node.fqn in selected_fqns or not node.src_path:
                continue
            if Path(node.src_path).name != "__init__.py":
                continue
            d = str(Path(node.src_path).parent).replace("\\", "/")
            if d not in selected_dirs:
                continue
            # Only require positive score (very permissive for __init__.py)
            if scored_node.score <= 0:
                continue
            selected_nodes.append(node)
            selected_fqns.add(node.fqn)
            neighbor_reasons[node.fqn] = "package __init__.py for selected directory"
            init_budget -= 1

    # ---- Parent-package sibling expansion ---------------------------------
    # If we selected e.g. migrations/operations/fields.py, also consider
    # files in the parent directory (migrations/*.py) as candidates.
    parent_budget = max(0, preset.max_files - len(selected_nodes))
    if parent_budget > 0:
        parent_dirs: Set[str] = set()
        for node in selected_nodes:
            if node.src_path:
                d = str(Path(node.src_path).parent).replace("\\", "/")
                parent = str(Path(d).parent).replace("\\", "/")
                if parent and parent != ".":
                    parent_dirs.add(parent)
        parent_ratio_floor = 0.20 if low_margin else 0.30
        parent_score_floor = primary_score * parent_ratio_floor if primary_score > 0 else 0.0
        parent_rank_cap = max(20, preset.max_files * 6)
        for scored_node in scored:
            if parent_budget <= 0:
                break
            node = scored_node.node
            if node.fqn in selected_fqns or not node.src_path:
                continue
            d = str(Path(node.src_path).parent).replace("\\", "/")
            if d not in parent_dirs:
                continue
            if scored_node.score < parent_score_floor:
                continue
            idx = rank_index.get(node.fqn, 10**9)
            if idx > parent_rank_cap:
                continue
            selected_nodes.append(node)
            selected_fqns.add(node.fqn)
            neighbor_reasons[node.fqn] = "parent-package sibling of selected file"
            parent_budget -= 1

    # ---- Graph-neighbor fill phase ----------------------------------------
    # If budget remains, scan import/call neighbors of ALL selected files.
    # This catches cross-subsystem oracle files (e.g. migrations/serializer.py
    # when the anchor is in models/fields/__init__.py).
    graph_fill_budget = max(0, preset.max_files - len(selected_nodes))
    if graph_fill_budget > 0 and meta is not None:
        all_selected_fqns = list(selected_fqns)
        fill_neighbors = _meta_weighted_neighbors(
            doc, meta, all_selected_fqns, hops=2, direction="both",
        )
        if fill_neighbors:
            fill_candidates = []
            for candidate in fill_neighbors.values():
                scored_node = rank_map.get(candidate.fqn)
                if scored_node is None or candidate.fqn in selected_fqns:
                    continue
                idx = rank_index.get(candidate.fqn, 10**9)
                # Only include if it has reasonable scoring rank
                if idx > max(15, preset.max_files * 5):
                    continue
                fill_candidates.append((candidate, scored_node, idx))
            fill_candidates.sort(
                key=lambda item: (-item[1].score, item[2], item[1].node.fqn)
            )
            for candidate, scored_node, _idx in fill_candidates:
                if graph_fill_budget <= 0:
                    break
                if candidate.fqn in selected_fqns:
                    continue
                selected_nodes.append(scored_node.node)
                selected_fqns.add(candidate.fqn)
                neighbor_reasons[candidate.fqn] = (
                    f"graph-fill: {candidate.via} edge {candidate.hop}-hop from selected"
                )
                graph_fill_budget -= 1

    return selected_nodes[: preset.max_files], neighbor_reasons


def _score_nodes(doc: exvisitDoc, repo_root: Path, text: str) -> List[Tuple[int, Node, List[str]]]:
    code_terms, keywords = extract_issue_terms(text)
    trace_frames = extract_trace_frames(text)
    lowered = text.lower()
    src_name_counts = Counter(Path(term).name.lower() for term in code_terms if term.lower().endswith(".py"))
    symbol_terms = [term for term in code_terms if "." in term and not term.lower().endswith(".py")]
    range_cache: Dict[str, Dict[str, Tuple[int, int]]] = {}
    scored: List[Tuple[int, Node, List[str]]] = []

    config_heuristics = {"permission", "permissions", "settings", "default", "config", "configuration", "upload", "env", "environ"}
    has_config_term = any(kw in config_heuristics for kw in keywords)

    for node in doc.all_nodes():
        src_path = (node.src_path or "").lower()
        src_name = Path(node.src_path or "").name.lower()
        name = node.name.lower()
        ns = node.ns_path.lower()
        score = 0
        reasons: List[str] = []
        
        if has_config_term:
            if src_name in ("settings.py", "global_settings.py", "__init__.py", "config.py") or "conf/" in src_path or hasattr(node, "node_type") and node.node_type == "config":
                score += 50
                reasons.append("config-heuristic-boost")

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

        # --- FIX 1: Test file penalty ---
        # Test files frequently match issue keywords but are almost never the
        # oracle (gold) file.  Penalise heavily unless the issue is explicitly
        # about test infrastructure.
        if score > 0 and src_path and 'tests/' in src_path.replace('\\', '/'):
            test_infra_terms = {'testcase', 'test_runner', 'runtests', 'pytest',
                                'unittest', 'test suite', 'test framework'}
            if not any(t in lowered for t in test_infra_terms):
                score = max(0, score - 40)
                reasons.append("test-file-penalty")

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
    exvisit_path: Optional[str] = None,
    meta: Optional[GraphMeta] = None,
    scoring: Optional[str] = None,
) -> BlastBundle:
    """Build a blast context bundle for the given issue text.

    `scoring` controls ranker selection:
      - "v2"   : log-linear multi-signal ranker (requires `meta` or sidecar)
      - "v1"   : legacy heuristic ranker (current behavior)
      - None   : auto — v2 if meta is provided/loadable, else v1.
    Env var `EXVISIT_SCORING` overrides if set.
    """
    if preset_name == "test-fix":
        lowered_text = text.lower()
        if "traceback" in lowered_text or "crash" in lowered_text or "exception" in lowered_text or "error" in lowered_text:
            preset_name = "crash-fix"
        elif "test" in lowered_text or "assert" in lowered_text or "failure" in lowered_text:
            preset_name = "test-fix"
        else:
            preset_name = "issue-fix"

    repo = Path(repo_root)
    presets = load_blast_presets(config_path)
    preset = presets.get(preset_name, presets.get("default"))
    if preset is None:
        raise KeyError("no blast presets available")

    # ---- meta loading -----------------------------------------------------
    if meta is None and exvisit_path:
        try:
            meta = _load_meta_for(Path(exvisit_path))
        except Exception:
            meta = None

    scoring_mode = (os.environ.get("EXVISIT_SCORING") or scoring or "").lower()
    if not scoring_mode:
        scoring_mode = "v2" if meta is not None else "v1"

    if scoring_mode == "v2":
        return _build_bundle_v2(doc, repo, text, preset, meta)

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

    # --- FIX 3: Sibling file expansion ---
    # When blast picks the right directory but wrong file, include other scored
    # files from the same directory to capture near-misses.
    # Exclude __init__.py from siblings (they're too generic and flood results).
    selected_dirs = set()
    for node in selected_nodes:
        if node.src_path:
            selected_dirs.add(str(Path(node.src_path).parent).replace('\\', '/'))
    keep_fqn_set = {n.fqn for n in selected_nodes}
    sibling_candidates: List[Tuple[int, Node]] = []
    for other in doc.all_nodes():
        if other.fqn in keep_fqn_set or not other.src_path:
            continue
        # Skip __init__.py as siblings — they dilute precision
        if Path(other.src_path).name == '__init__.py':
            continue
        # Skip test files as siblings
        if 'tests/' in other.src_path.replace('\\', '/'):
            continue
        other_dir = str(Path(other.src_path).parent).replace('\\', '/')
        if other_dir in selected_dirs:
            other_score = rank_map.get(other.fqn, (0, []))[0]
            if other_score >= 5:  # require minimum relevance
                sibling_candidates.append((other_score, other))
    sibling_candidates.sort(key=lambda x: -x[0])
    sibling_budget = 2  # allow up to 2 extra sibling files
    for sib_score, sib in sibling_candidates:
        if sibling_budget <= 0:
            break
        selected_nodes.append(sib)
        keep_fqn_set.add(sib.fqn)
        sibling_budget -= 1
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


# ---------------------------------------------------------------------------
# Scoring v2 wrapper — builds a BlastBundle using the log-linear ranker.
# ---------------------------------------------------------------------------
def _build_bundle_v2(
    doc: exvisitDoc,
    repo: Path,
    text: str,
    preset: BlastPreset,
    meta: Optional[GraphMeta],
) -> BlastBundle:
    config = _v2.load_v2_config()
    scored = _v2.score_nodes_v2(doc, repo, text, meta, config)
    if not scored:
        raise KeyError("scoring v2 produced no scored nodes")

    anchors, confidence, low_margin = _v2.select_anchors(scored, config)
    primary = anchors[0]
    selection_reasons: List[BlastSelectionReason] = [
        BlastSelectionReason(
            node_id=primary.node.fqn,
            phase="anchor",
            reason=("low-margin top-K; " if low_margin else "") + ", ".join(primary.reasons[:5]) or "v2 top-ranked",
            score=int(round(primary.score * 100)),
        )
    ]
    if low_margin:
        for alt in anchors[1:]:
            selection_reasons.append(
                BlastSelectionReason(
                    node_id=alt.node.fqn,
                    phase="alt-anchor",
                    reason="margin-tied: " + ", ".join(alt.reasons[:4]),
                    score=int(round(alt.score * 100)),
                )
            )

    rank_map: Dict[str, _v2.ScoredNode] = {s.node.fqn: s for s in scored}
    selected_nodes, neighbor_reasons = _select_v2_nodes(
        doc,
        meta,
        scored,
        anchors,
        preset,
        confidence,
        low_margin,
    )
    anchor_fqns = {a.node.fqn for a in anchors}

    selected_files: List[str] = []
    snippets: List[BlastSnippet] = []
    seen_files: Set[str] = set()
    for n in selected_nodes:
        file_path = resolve_repo_file(repo, n.src_path)
        if file_path is None:
            continue
        rel = file_path.relative_to(repo).as_posix()
        if rel in seen_files:
            continue
        seen_files.add(rel)
        selected_files.append(rel)
        if n.fqn not in anchor_fqns:
            sn = rank_map.get(n.fqn)
            reasons_txt = ", ".join(sn.reasons[:3]) if sn else ""
            selection_reasons.append(
                BlastSelectionReason(
                    node_id=n.fqn,
                    phase="neighbor",
                    reason=(reasons_txt + "; " if reasons_txt else "")
                    + neighbor_reasons.get(
                        n.fqn,
                        f"within {preset.hops}-hop blast radius of {primary.node.name}",
                    ),
                    score=int(round((sn.score if sn else 0.0) * 100)),
                )
            )
        if len(snippets) < preset.max_snippets:
            label, code = choose_best_snippet(
                file_path, text, preset.max_snippet_lines, line_range=n.line_range
            )
            reason = (
                "anchor file" if n.fqn == primary.node.fqn
                else ("alt anchor" if n.fqn in anchor_fqns
                      else f"blast neighbor of {primary.node.name}")
            )
            snippets.append(BlastSnippet(file_path=rel, label=label, reason=reason, code=code))

    selected_node_ids = [n.fqn for n in selected_nodes]
    omitted_node_count = max(0, len(doc.all_nodes()) - len(selected_node_ids))
    omitted_file_count = max(
        0, len({n.src_path for n in doc.all_nodes() if n.src_path}) - len(selected_files)
    )
    token_estimate = estimate_tokens(text) + sum(estimate_tokens(s.code) for s in snippets)

    bundle_warnings: List[str] = []
    if meta is None:
        bundle_warnings.append("no meta sidecar — using inferred kinds; PageRank/structural priors disabled")
    if low_margin:
        bundle_warnings.append(
            f"low-confidence anchor (margin<{config.anchor_margin}); returning {len(anchors)} candidates"
        )

    return BlastBundle(
        preset=preset.name,
        anchor=primary.node.fqn,
        anchor_file=primary.node.src_path or "",
        confidence=confidence,
        selected_nodes=selected_node_ids,
        selected_files=selected_files,
        omitted_node_count=omitted_node_count,
        omitted_file_count=omitted_file_count,
        token_estimate=token_estimate,
        selection_reasons=selection_reasons,
        snippets=snippets,
        warnings=bundle_warnings,
    )


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

