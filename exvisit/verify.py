"""exvisit verify — cross-check declared `->` edges against real Python imports.

Reports:
  MissingEdge : source imports a module but there's no `->` edge in the exvisit.
  GhostEdge   : exvisit declares `->` but no matching import exists in source.
  `~>` edges are treated as informational (not verified).
"""
from __future__ import annotations
import ast
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Set, Tuple, Optional
from .ast import exvisitDoc, Node, EdgeKind


@dataclass
class Diagnostic:
    kind: str            # "missing" | "ghost" | "unresolved"
    src_node: str        # fqn
    dst: str             # fqn or raw import path
    detail: str = ""

    def fmt(self) -> str:
        return f"  [{self.kind:<10}] {self.src_node:30} -> {self.dst:30} {self.detail}"


def _collect_py_imports(src_file: Path) -> Set[str]:
    """Return set of dotted module names imported by this file, including
    `from pkg import name` expanded as `pkg.name` to catch module-object imports."""
    try:
        # utf-8-sig strips BOM; errors=replace guards weird encodings
        tree = ast.parse(src_file.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception:
        return set()
    mods: Set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                mods.add(a.name)
        elif isinstance(n, ast.ImportFrom):
            if n.module:
                mods.add(n.module)
                for a in n.names:
                    mods.add(f"{n.module}.{a.name}")
    return mods


def _resolve_node_for_module(mod: str, nodes_by_srcpath: Dict[str, Node],
                             repo_root: Path) -> Optional[Node]:
    """Map `maxmorph.core.scene` -> the Node whose src_path corresponds."""
    parts = mod.split(".")
    candidates: List[str] = []
    for k in range(len(parts), 0, -1):
        candidates.append("/".join(parts[:k]) + ".py")
    candidates.append(parts[-1] + ".py")

    for c in candidates:
        c_norm = c.replace("\\", "/")
        for sp, node in nodes_by_srcpath.items():
            sp_norm = sp.replace("\\", "/")
            # match only on full filename component boundary
            if sp_norm == c_norm:
                return node
            if sp_norm.endswith("/" + c_norm):
                return node
            if c_norm.endswith("/" + sp_norm):
                return node
    return None


def verify(doc: exvisitDoc, repo_root: str) -> List[Diagnostic]:
    root = Path(repo_root)
    diags: List[Diagnostic] = []

    nodes_with_src = [n for n in doc.all_nodes() if n.src_path]
    nodes_by_srcpath: Dict[str, Node] = {n.src_path: n for n in nodes_with_src}

    # Build declared outbound `->` map (bare-name keyed; edges use bare names)
    declared_out: Dict[str, Set[str]] = {}
    for e in doc.edges:
        if e.kind == EdgeKind.SYNC:
            declared_out.setdefault(e.src, set()).add(e.dst)

    # For each node with a file, parse real imports and map them to exvisit nodes
    for node in nodes_with_src:
        # locate the source file by searching under the node's namespace src_glob or repo root
        candidates: List[Path] = []
        sp = node.src_path.replace("\\", "/")
        # 1) direct match anywhere under repo
        for p in root.rglob(Path(sp).name):
            if p.is_file() and p.as_posix().endswith(sp):
                candidates.append(p)
        if not candidates:
            for p in root.rglob(Path(sp).name):
                if p.is_file():
                    candidates.append(p)
        if not candidates:
            diags.append(Diagnostic("unresolved", node.fqn, node.src_path,
                                    detail="source file not found under repo"))
            continue
        src_file = candidates[0]

        real_mods = _collect_py_imports(src_file)
        real_internal_nodes: Set[str] = set()
        for mod in real_mods:
            target = _resolve_node_for_module(mod, nodes_by_srcpath, root)
            if target and target.name != node.name:
                real_internal_nodes.add(target.name)

        decl = declared_out.get(node.name, set())

        for missing in real_internal_nodes - decl:
            diags.append(Diagnostic("missing", node.fqn, missing,
                                    detail="real import not in exvisit"))
        for ghost in decl - real_internal_nodes:
            # only flag if target is a concrete node with src_path (we can't prove absence otherwise)
            tgt = next((n for n in nodes_with_src if n.name == ghost), None)
            if tgt is not None:
                diags.append(Diagnostic("ghost", node.fqn, ghost,
                                        detail="exvisit edge with no matching import"))

    return diags


def format_report(diags: List[Diagnostic]) -> str:
    if not diags:
        return "verify: OK — all `->` edges match real imports.\n"
    lines = [f"verify: {len(diags)} diagnostic(s)\n"]
    by_kind: Dict[str, List[Diagnostic]] = {}
    for d in diags:
        by_kind.setdefault(d.kind, []).append(d)
    for kind in ("missing", "ghost", "unresolved"):
        if kind in by_kind:
            lines.append(f"-- {kind} ({len(by_kind[kind])}) --")
            for d in by_kind[kind]:
                lines.append(d.fmt())
            lines.append("")
    return "\n".join(lines)

