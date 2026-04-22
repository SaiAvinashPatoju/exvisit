"""Raw error-log anchoring for `.exv` graphs."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .ast import exvisitDoc, Node
from .blast import extract_trace_frames, rank_nodes_for_text


@dataclass
class AnchorHit:
    role: str
    fqn: str
    file_path: str
    line: int
    score: int
    reason: str


@dataclass
class AnchorReport:
    anchor: str
    frame_count: int
    hits: List[AnchorHit] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


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


def _resolve_edge_targets(doc: exvisitDoc, ref: str) -> Set[str]:
    resolved = {node.fqn for node in doc.all_nodes() if node.name == ref or node.fqn == ref}
    node = doc.find_node(ref)
    if node is not None:
        resolved.add(node.fqn)
    return resolved


def _line_for_node(node: Node, trace_text: str) -> int:
    default_line = node.line_range[0] if node.line_range else 1
    for frame in extract_trace_frames(trace_text):
        if _path_match_score(node.src_path, frame.file_path) <= 0:
            continue
        if node.line_range and node.line_range[0] <= frame.line <= node.line_range[1]:
            return frame.line
        if not node.line_range:
            return frame.line
    return default_line


def _direct_neighbors(doc: exvisitDoc, anchor_fqn: str) -> Tuple[Set[str], Set[str]]:
    imports: Set[str] = set()
    dependents: Set[str] = set()
    for edge in doc.edges:
        srcs = _resolve_edge_targets(doc, edge.src)
        dsts = _resolve_edge_targets(doc, edge.dst)
        if anchor_fqn in srcs:
            imports |= {dst for dst in dsts if dst != anchor_fqn}
        if anchor_fqn in dsts:
            dependents |= {src for src in srcs if src != anchor_fqn}
    return imports, dependents


def build_anchor_report(
    doc: exvisitDoc,
    repo_root: str,
    trace_text: str,
    max_hits: int = 6,
) -> AnchorReport:
    repo = Path(repo_root)
    ranked = rank_nodes_for_text(doc, repo, trace_text)
    if not ranked:
        raise KeyError("could not resolve an exvisit anchor from the provided trace text")

    frames = extract_trace_frames(trace_text)
    anchor_score, anchor_node, anchor_reasons = ranked[0]
    rank_map: Dict[str, Tuple[int, List[str], Node]] = {
        node.fqn: (score, reasons, node) for score, node, reasons in ranked
    }
    warnings: List[str] = []
    if not frames:
        warnings.append("no stack frames detected; used lexical anchor resolution")

    hits: List[AnchorHit] = [
        AnchorHit(
            role="ground_zero",
            fqn=anchor_node.fqn,
            file_path=anchor_node.src_path or "",
            line=_line_for_node(anchor_node, trace_text),
            score=anchor_score,
            reason=", ".join(anchor_reasons[:4]) or "top-ranked anchor",
        )
    ]

    imports, dependents = _direct_neighbors(doc, anchor_node.fqn)
    seen = {anchor_node.fqn}

    def add_hits(role: str, node_ids: Sequence[str], fallback_reason: str):
        for node_id in sorted(node_ids, key=lambda value: (-rank_map.get(value, (0, [], None))[0], value)):
            if node_id in seen or len(hits) >= max_hits:
                continue
            score, reasons, node = rank_map.get(node_id, (0, [], doc.find_node(node_id)))
            if node is None:
                continue
            hits.append(
                AnchorHit(
                    role=role,
                    fqn=node.fqn,
                    file_path=node.src_path or "",
                    line=_line_for_node(node, trace_text),
                    score=score,
                    reason=", ".join(reasons[:3]) or fallback_reason,
                )
            )
            seen.add(node_id)

    add_hits("direct_import", imports, f"direct outbound edge from {anchor_node.name}")
    add_hits("direct_dependent", dependents, f"direct inbound edge to {anchor_node.name}")
    structural_neighbors = [
        node.fqn for _, node, _ in ranked
        if node.fqn not in seen and node.src_path and node.fqn != anchor_node.fqn
    ]
    add_hits("structural_neighbor", structural_neighbors, f"high-scoring structural neighbor of {anchor_node.name}")

    return AnchorReport(anchor=anchor_node.fqn, frame_count=len(frames), hits=hits, warnings=warnings)


def anchor_report_to_json(report: AnchorReport) -> str:
    return json.dumps(asdict(report), indent=2)


def render_anchor_text(report: AnchorReport) -> str:
    lines = []
    for hit in report.hits:
        location = f"{hit.file_path}:{hit.line}" if hit.file_path else f"line {hit.line}"
        lines.append(f"[{hit.role}] {hit.fqn} at {location} ({hit.reason})")
    if report.warnings:
        lines.append("")
        for warning in report.warnings:
            lines.append(f"[warn] {warning}")
    return "\n".join(lines) + "\n"
