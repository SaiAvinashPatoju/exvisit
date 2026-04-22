"""Delta-based CRDT memory engine.

Primitives:
  - OR-Set   -> namespaces (observed-remove: add wins over concurrent remove with older tag)
  - LWW-Map  -> tabular nodes (last-write-wins on timestamp)
  - 2P-Set   -> edges (two-phase: once removed, never re-added with same tag)

A minimal, dependency-free implementation suitable for the reference port.
Conflicts merge deterministically; `merge()` is commutative/associative/idempotent.
"""
from __future__ import annotations
import time
import itertools
from dataclasses import dataclass, field
from typing import Dict, Set, Tuple, Optional, Iterable, List
from .ast import exvisitDoc, Namespace, Node, Edge, EdgeKind
from .spatial import RTree, world_coords


_counter = itertools.count()
def _ts() -> Tuple[float, int]:
    """Hybrid logical clock: wall-time + monotonic counter tiebreaker."""
    return (time.time(), next(_counter))


# -------------------- OR-Set ------------------------------------------------
@dataclass
class ORSet:
    """add(tag) -> adds element with unique tag. remove() removes all observed tags."""
    _adds: Dict[str, Set[Tuple]] = field(default_factory=dict)   # elem -> {tag,..}
    _rems: Dict[str, Set[Tuple]] = field(default_factory=dict)

    def add(self, elem: str, tag: Optional[Tuple] = None):
        tag = tag or _ts()
        self._adds.setdefault(elem, set()).add(tag)

    def remove(self, elem: str):
        observed = self._adds.get(elem, set())
        self._rems.setdefault(elem, set()).update(observed)

    def contains(self, elem: str) -> bool:
        a = self._adds.get(elem, set())
        r = self._rems.get(elem, set())
        return bool(a - r)

    def elements(self) -> Set[str]:
        return {e for e in self._adds if self.contains(e)}

    def merge(self, other: "ORSet") -> "ORSet":
        out = ORSet()
        for d_self, d_other in ((self._adds, other._adds), ):
            pass
        for elem, tags in {**self._adds, **other._adds}.items():
            out._adds[elem] = set(self._adds.get(elem, set())) | set(other._adds.get(elem, set()))
        for elem, tags in {**self._rems, **other._rems}.items():
            out._rems[elem] = set(self._rems.get(elem, set())) | set(other._rems.get(elem, set()))
        return out


# -------------------- LWW-Map -----------------------------------------------
@dataclass
class LWWMap:
    """Last-write-wins map with explicit timestamps."""
    _data: Dict[str, Tuple[Tuple, object]] = field(default_factory=dict)  # key -> (ts, value)

    def set(self, key: str, value, ts: Optional[Tuple] = None):
        ts = ts or _ts()
        cur = self._data.get(key)
        if cur is None or ts > cur[0]:
            self._data[key] = (ts, value)

    def get(self, key: str):
        v = self._data.get(key)
        return v[1] if v else None

    def keys(self):
        return list(self._data.keys())

    def items(self):
        return [(k, v[1]) for k, v in self._data.items()]

    def merge(self, other: "LWWMap") -> "LWWMap":
        out = LWWMap()
        for k in set(self._data) | set(other._data):
            a = self._data.get(k); b = other._data.get(k)
            if a and b:
                out._data[k] = a if a[0] >= b[0] else b
            else:
                out._data[k] = a or b  # type: ignore
        return out


# -------------------- 2P-Set ------------------------------------------------
@dataclass
class TwoPSet:
    _added: Set[Tuple[str, str, str]] = field(default_factory=set)   # (src, dst, kind)
    _removed: Set[Tuple[str, str, str]] = field(default_factory=set)

    def add(self, src, dst, kind: EdgeKind):
        self._added.add((src, dst, kind.value))

    def remove(self, src, dst, kind: EdgeKind):
        self._removed.add((src, dst, kind.value))

    def contains(self, src, dst, kind: EdgeKind) -> bool:
        t = (src, dst, kind.value)
        return t in self._added and t not in self._removed

    def elements(self) -> Set[Tuple[str, str, str]]:
        return self._added - self._removed

    def merge(self, other: "TwoPSet") -> "TwoPSet":
        out = TwoPSet()
        out._added = self._added | other._added
        out._removed = self._removed | other._removed
        return out


# -------------------- Graph -------------------------------------------------
@dataclass
class exvisitGraph:
    """In-memory CRDT representation of an exvisitDoc, backed by an R-Tree."""
    namespaces: ORSet = field(default_factory=ORSet)             # dotted paths
    ns_meta: LWWMap = field(default_factory=LWWMap)              # path -> Namespace (sans children/nodes)
    nodes: LWWMap = field(default_factory=LWWMap)                # fqn -> Node
    edges: TwoPSet = field(default_factory=TwoPSet)
    rtree: RTree = field(default_factory=RTree)

    @classmethod
    def from_doc(cls, doc: exvisitDoc) -> "exvisitGraph":
        g = cls()
        def visit(ns: Namespace, origin=(0, 0), depth=0):
            g.namespaces.add(ns.path)
            # store a lightweight meta copy (don't store children tree in LWW-Map)
            g.ns_meta.set(ns.path, {
                "level": ns.level, "name": ns.name, "bounds": ns.bounds,
                "src_glob": ns.src_glob, "path": ns.path,
            })
            ox, oy = origin
            nx, ny = ox + ns.bounds[0], oy + ns.bounds[1]
            for n in ns.nodes:
                g.nodes.set(n.fqn, n)
                wx, wy = nx + n.bounds[0], ny + n.bounds[1]
                u, v = world_coords(wx, wy, depth + 1)
                g.rtree.insert(n.fqn, (u, v, n.bounds[2], n.bounds[3]))
            for c in ns.children:
                visit(c, (nx, ny), depth + 1)
        visit(doc.root)
        for e in doc.edges:
            g.edges.add(e.src, e.dst, e.kind)
        return g

    # Sync-daemon style delta merge -----------------------------------------
    def apply_node_bounds(self, fqn: str, bounds):
        n = self.nodes.get(fqn)
        if n is None:
            return
        # mutate via LWW-Map to preserve CRDT semantics (new ts wins)
        new_node = Node(name=n.name, bounds=tuple(bounds), src_path=n.src_path,
                        line_range=n.line_range, states=list(n.states), ns_path=n.ns_path)
        self.nodes.set(fqn, new_node)

    def add_edge(self, src: str, dst: str, kind: EdgeKind = EdgeKind.SYNC):
        self.edges.add(src, dst, kind)

    def merge(self, other: "exvisitGraph") -> "exvisitGraph":
        out = exvisitGraph()
        out.namespaces = self.namespaces.merge(other.namespaces)
        out.ns_meta = self.ns_meta.merge(other.ns_meta)
        out.nodes = self.nodes.merge(other.nodes)
        out.edges = self.edges.merge(other.edges)
        # rebuild r-tree from nodes deterministically
        for fqn, n in out.nodes.items():
            out.rtree.insert(fqn, (n.bounds[0], n.bounds[1], n.bounds[2], n.bounds[3]))
        return out

    def to_doc(self) -> exvisitDoc:
        """Flush CRDT state back to a canonical exvisitDoc tree."""
        meta_by_path: Dict[str, dict] = {p: self.ns_meta.get(p) for p in self.namespaces.elements()}
        # build namespace tree
        paths = sorted(meta_by_path.keys(), key=lambda p: p.count("."))
        by_path: Dict[str, Namespace] = {}
        root: Optional[Namespace] = None
        for p in paths:
            m = meta_by_path[p]
            ns = Namespace(level=m["level"], name=m["name"], bounds=tuple(m["bounds"]),
                           src_glob=m["src_glob"], path=p)
            by_path[p] = ns
            if "." in p:
                parent = p.rsplit(".", 1)[0]
                by_path[parent].children.append(ns)
            else:
                root = ns
        assert root is not None, "exvisit graph has no root namespace"
        for fqn, n in self.nodes.items():
            by_path[n.ns_path].nodes.append(n)
        edges = [Edge(src=s, dst=d, kind=EdgeKind(k)) for (s, d, k) in sorted(self.edges.elements())]
        return exvisitDoc(root=root, edges=edges)

