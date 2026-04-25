"""Graph metadata sidecar for ExVisit vNext.

Stores per-node and per-edge structural facts that don't fit the `.exv` DSL:

  * node `kind`        — "registry" | "test" | "migration" | "normal"
  * node `symbols`     — list of top-level class/function names
  * node `loc`         — file line count
  * node `pagerank`    — float in [0,1]
  * node `cluster`     — directory key (for cluster-IDF)
  * edges by type      — {"import": [(src, dst), ...], "inherit": [...], ...}
  * edge_priors        — learned/default per-edge oracle-coverage weights
  * cluster_size       — {dir: int}
  * version            — schema version

Stored as `<exvisit>.meta.json` next to the `.exv` file.

The blast-v2 scorer reads this file when present; if absent, scoring v2 falls
back to deterministic structural heuristics computed at load time so the system
still works on legacy `.exv` files.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


META_VERSION = 1

DEFAULT_EDGE_PRIORS = {
    "import": 0.12,
    "call": 0.28,
    "inherit": 0.32,
    "descriptor": 0.34,
    "config-ref": 0.41,
    "migration-of": 0.18,
    "test-of": 0.05,
}


@dataclass
class NodeMeta:
    fqn: str
    kind: str = "normal"          # registry | test | migration | normal
    symbols: List[str] = field(default_factory=list)
    loc: int = 0
    pagerank: float = 0.0
    cluster: str = ""             # directory of src_path

    def to_dict(self) -> dict:
        return {
            "fqn": self.fqn,
            "kind": self.kind,
            "symbols": self.symbols,
            "loc": self.loc,
            "pagerank": self.pagerank,
            "cluster": self.cluster,
        }


@dataclass
class GraphMeta:
    version: int = META_VERSION
    nodes: Dict[str, NodeMeta] = field(default_factory=dict)
    edges_by_type: Dict[str, List[List[str]]] = field(default_factory=dict)
    edge_priors: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_EDGE_PRIORS))
    cluster_size: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges_by_type": self.edges_by_type,
            "edge_priors": self.edge_priors,
            "cluster_size": self.cluster_size,
        }

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> "GraphMeta":
        payload = json.loads(path.read_text(encoding="utf-8"))
        meta = cls(
            version=int(payload.get("version", META_VERSION)),
            edges_by_type={k: [tuple(p) for p in v] for k, v in payload.get("edges_by_type", {}).items()},
            edge_priors={**DEFAULT_EDGE_PRIORS, **payload.get("edge_priors", {})},
            cluster_size={k: int(v) for k, v in payload.get("cluster_size", {}).items()},
        )
        for n in payload.get("nodes", []):
            meta.nodes[n["fqn"]] = NodeMeta(
                fqn=n["fqn"],
                kind=n.get("kind", "normal"),
                symbols=list(n.get("symbols", [])),
                loc=int(n.get("loc", 0)),
                pagerank=float(n.get("pagerank", 0.0)),
                cluster=n.get("cluster", ""),
            )
        return meta


def sidecar_path(exvisit_path: Path) -> Path:
    """Return the canonical sidecar path for an `.exv` file."""
    return exvisit_path.with_suffix(exvisit_path.suffix + ".meta.json")


def load_for(exvisit_path: Path) -> Optional[GraphMeta]:
    """Load sidecar meta for the given `.exv` path, or None if missing."""
    sp = sidecar_path(exvisit_path)
    if not sp.exists():
        return None
    try:
        return GraphMeta.read(sp)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# PageRank — small, dependency-free implementation
# ---------------------------------------------------------------------------
def pagerank(
    nodes: List[str],
    weighted_edges: List[Tuple[str, str, float]],
    damping: float = 0.85,
    iterations: int = 30,
) -> Dict[str, float]:
    """Compute PageRank over a weighted directed graph.

    `weighted_edges` is a list of (src, dst, weight); weights are summed when
    multiple edges connect the same pair.
    """
    n = len(nodes)
    if n == 0:
        return {}
    idx = {fqn: i for i, fqn in enumerate(nodes)}
    out_w: List[float] = [0.0] * n
    adj: List[List[Tuple[int, float]]] = [[] for _ in range(n)]
    for s, d, w in weighted_edges:
        if s not in idx or d not in idx or w <= 0:
            continue
        si, di = idx[s], idx[d]
        adj[si].append((di, w))
        out_w[si] += w
    rank = [1.0 / n] * n
    teleport = (1.0 - damping) / n
    for _ in range(iterations):
        nxt = [teleport] * n
        leaked = 0.0
        for i in range(n):
            if out_w[i] == 0:
                leaked += rank[i]
                continue
            r = damping * rank[i] / out_w[i]
            for di, w in adj[i]:
                nxt[di] += r * w
        # distribute dangling mass uniformly
        if leaked:
            add = damping * leaked / n
            for i in range(n):
                nxt[i] += add
        rank = nxt
    return {fqn: rank[idx[fqn]] for fqn in nodes}
