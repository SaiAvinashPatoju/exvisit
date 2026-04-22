"""Deterministic canonical serializer: parse(serialize(doc)) == doc."""
from __future__ import annotations
from typing import List
from .ast import exvisitDoc, Namespace, Node, Edge, EdgeKind


def _fmt_bounds(b) -> str:
    return f"[{b[0]},{b[1]},{b[2]},{b[3]}]"


def _fmt_line_range(line_range) -> str:
    return f"lines={line_range[0]}..{line_range[1]}"


def _fmt_node(n: Node) -> str:
    parts = [n.name, _fmt_bounds(n.bounds)]
    if n.src_path:
        if any(ch in n.src_path for ch in " \t"):
            parts.append(f'"{n.src_path}"')
        else:
            parts.append(n.src_path)
    if n.line_range:
        parts.append(_fmt_line_range(n.line_range))
    if n.states:
        parts.append("{" + " -> ".join(n.states) + "}")
    return " ".join(parts)


def _fmt_ns(ns: Namespace, indent: int = 0) -> List[str]:
    pad = "  " * indent
    head = f"{pad}@L{ns.level} {ns.name} {_fmt_bounds(ns.bounds)}"
    if ns.src_glob:
        head += f' "{ns.src_glob}"'
    head += " {"
    lines = [head]
    # sort nodes by (y, x) for determinism
    for n in sorted(ns.nodes, key=lambda n: (n.bounds[1], n.bounds[0], n.name)):
        lines.append(f"{pad}  {_fmt_node(n)}")
    for c in sorted(ns.children, key=lambda c: (c.bounds[1], c.bounds[0], c.name)):
        lines.extend(_fmt_ns(c, indent + 1))
    lines.append(f"{pad}}}")
    return lines


def serialize(doc: exvisitDoc) -> str:
    lines = _fmt_ns(doc.root, 0)
    if doc.edges:
        lines.append("=== edges ===")
        for e in sorted(doc.edges, key=lambda e: (e.src, e.dst, e.kind.value)):
            arrow = "->" if e.kind == EdgeKind.SYNC else "~>"
            lines.append(f"{e.src} {arrow} {e.dst}")
    return "\n".join(lines) + "\n"

