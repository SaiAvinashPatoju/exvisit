"""exvisit-query — microscopic topological extraction.

Returns a minimal .exv text slice containing only:
  - the parent namespace chain (preserved bounds)
  - the target node
  - its 1-hop topological neighbors (in or out, configurable)
  - optional expanded radius
"""
from __future__ import annotations
from typing import List, Set, Optional
from .ast import exvisitDoc, Namespace, Node, Edge, EdgeKind
from .serialize import serialize


def _resolve(doc: exvisitDoc, target: str) -> Optional[Node]:
    return doc.find_node(target)


def _neighbors(doc: exvisitDoc, node_fqn: str, hops: int = 1,
               direction: str = "both") -> Set[str]:
    """Return set of fqns within `hops` topological hops of node_fqn."""
    # Build adjacency (using bare names or fqns as they appear in edges)
    out_adj: dict = {}
    in_adj: dict = {}
    for e in doc.edges:
        out_adj.setdefault(e.src, set()).add(e.dst)
        in_adj.setdefault(e.dst, set()).add(e.src)

    # Map bare name -> fqn
    name_map = {}
    for n in doc.all_nodes():
        name_map.setdefault(n.name, []).append(n.fqn)
        name_map[n.fqn] = [n.fqn]

    target_node = doc.find_node(node_fqn)
    if not target_node:
        return set()
    seed = target_node.name  # edges commonly use bare name

    frontier = {seed}
    visited = {seed}
    for _ in range(hops):
        nxt = set()
        for v in frontier:
            if direction in ("out", "both"):
                nxt |= out_adj.get(v, set())
            if direction in ("in", "both"):
                nxt |= in_adj.get(v, set())
        nxt -= visited
        visited |= nxt
        frontier = nxt

    # translate to fqns that exist
    result: Set[str] = set()
    for v in visited:
        for fqn in name_map.get(v, [v]):
            if doc.find_node(fqn):
                result.add(fqn)
    return result


def query(doc: exvisitDoc, target: str, hops: int = 1,
          direction: str = "both", preserve_bounds: bool = True) -> str:
    """Return a minimal serialized .exv slice around `target`."""
    target_node = _resolve(doc, target)
    if target_node is None:
        raise KeyError(f"target node not found: {target}")

    keep_fqns = _neighbors(doc, target_node.fqn, hops=hops, direction=direction)
    keep_fqns.add(target_node.fqn)

    # determine which namespaces to preserve: the ns-path of every kept node
    keep_ns = set()
    for fqn in keep_fqns:
        n = doc.find_node(fqn)
        if not n:
            continue
        p = n.ns_path
        while p:
            keep_ns.add(p)
            if "." in p:
                p = p.rsplit(".", 1)[0]
            else:
                break
        keep_ns.add(p)

    def prune(ns: Namespace) -> Optional[Namespace]:
        new_children = [c for c in (prune(c) for c in ns.children) if c is not None]
        new_nodes = [n for n in ns.nodes if n.fqn in keep_fqns]
        if not new_children and not new_nodes and ns.path not in keep_ns:
            return None
        return Namespace(level=ns.level, name=ns.name,
                         bounds=ns.bounds if preserve_bounds else (0, 0, 0, 0),
                         src_glob=ns.src_glob, children=new_children,
                         nodes=new_nodes, path=ns.path)

    pruned_root = prune(doc.root)
    if pruned_root is None:
        raise KeyError(f"nothing left after prune for {target}")

    # filter edges to those whose endpoints are kept
    kept_bare = set()
    for fqn in keep_fqns:
        n = doc.find_node(fqn)
        if n:
            kept_bare.add(n.name)
            kept_bare.add(n.fqn)
    pruned_edges = [e for e in doc.edges
                    if (e.src in kept_bare and e.dst in kept_bare)]

    return serialize(exvisitDoc(root=pruned_root, edges=pruned_edges))

