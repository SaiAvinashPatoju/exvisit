"""AST for the .exv DSL."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple


class EdgeKind(str, Enum):
    SYNC = "->"
    ASYNC = "~>"


Bounds = Tuple[int, int, int, int]  # x, y, w, h
LineRange = Tuple[int, int]  # start_line, end_line


@dataclass
class Node:
    name: str
    bounds: Bounds
    src_path: Optional[str] = None
    line_range: Optional[LineRange] = None
    states: List[str] = field(default_factory=list)  # ordered state machine
    ns_path: str = ""  # filled by parser: dotted namespace path this node lives in

    @property
    def fqn(self) -> str:
        return f"{self.ns_path}.{self.name}" if self.ns_path else self.name


@dataclass
class Edge:
    src: str  # fully-qualified dotted name
    dst: str
    kind: EdgeKind = EdgeKind.SYNC


@dataclass
class Namespace:
    level: int
    name: str
    bounds: Bounds
    src_glob: Optional[str] = None
    children: List["Namespace"] = field(default_factory=list)
    nodes: List[Node] = field(default_factory=list)
    path: str = ""  # dotted path

    def iter_nodes(self):
        yield from self.nodes
        for c in self.children:
            yield from c.iter_nodes()

    def iter_namespaces(self):
        yield self
        for c in self.children:
            yield from c.iter_namespaces()


@dataclass
class exvisitDoc:
    root: Namespace
    edges: List[Edge] = field(default_factory=list)

    def all_nodes(self) -> List[Node]:
        return list(self.root.iter_nodes())

    def find_node(self, fqn: str) -> Optional[Node]:
        # accept bare name if unique
        if "." in fqn:
            for n in self.all_nodes():
                if n.fqn == fqn or n.fqn.endswith("." + fqn):
                    return n
            return None
        matches = [n for n in self.all_nodes() if n.name == fqn]
        return matches[0] if len(matches) == 1 else None

