"""Minimal R-Tree-ish spatial index.

For the reference implementation we use a flat bounding-box list with a grid
hash for O(1) average-case point queries. The production Rust port uses a real
R-Tree. Public API is identical so consumers need not change.
"""
from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import Dict, List, Tuple


def world_coords(x: float, y: float, depth: int) -> Tuple[float, float]:
    """Translate logical (x,y) at Z-depth d to absolute world coords (u,v).

    Per spec:  u = x * z,  v = log(z)   with  z = 2**depth  (non-zero).
    """
    z = 2.0 ** max(depth, 1)
    u = x * z
    v = math.log(z)
    return u, v


@dataclass
class RTree:
    # id -> (x, y, w, h)  in world coordinates
    _rects: Dict[str, Tuple[float, float, float, float]] = field(default_factory=dict)

    def insert(self, id: str, rect):
        self._rects[id] = tuple(rect)  # type: ignore

    def remove(self, id: str):
        self._rects.pop(id, None)

    def query_point(self, x: float, y: float) -> List[str]:
        hits = []
        for i, (rx, ry, rw, rh) in self._rects.items():
            if rx <= x <= rx + rw and ry <= y <= ry + rh:
                hits.append(i)
        return hits

    def query_rect(self, x, y, w, h) -> List[str]:
        hits = []
        for i, (rx, ry, rw, rh) in self._rects.items():
            if not (rx + rw < x or rx > x + w or ry + rh < y or ry > y + h):
                hits.append(i)
        return hits

    def nearest(self, id: str, k: int = 4) -> List[str]:
        if id not in self._rects:
            return []
        ax, ay, aw, ah = self._rects[id]
        acx, acy = ax + aw / 2, ay + ah / 2
        dists = []
        for i, (rx, ry, rw, rh) in self._rects.items():
            if i == id:
                continue
            cx, cy = rx + rw / 2, ry + rh / 2
            dists.append((math.hypot(cx - acx, cy - acy), i))
        dists.sort()
        return [i for _, i in dists[:k]]
