"""exvisit init --from-repo — generate a draft .exv from a Python repo.

Heuristics:
  - Each top-level package dir under repo/ becomes an @L1 namespace.
  - Each .py file becomes a Node (name = CamelCase of filename stem).
  - Imports of internal modules seed `->` edges.
  - Layout: simple grid per namespace.

Output is deliberately minimal; user is expected to refine bounds + state machines.
"""
from __future__ import annotations
import ast
import os
import re
import warnings
from pathlib import Path
from typing import Dict, List, Set, Tuple

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv",
             "node_modules", "build", "dist", "target"}
IMPORT_RE = re.compile(r"^\s*import\s+([A-Za-z_][A-Za-z0-9_\.]*)", re.MULTILINE)
FROM_RE = re.compile(r"^\s*from\s+([A-Za-z_][A-Za-z0-9_\.]*)\s+import\s+(.+)$", re.MULTILINE)


def _camel(name: str) -> str:
    stem = Path(name).stem
    parts = re.split(r"[^A-Za-z0-9]+", stem)
    out = "".join(p[:1].upper() + p[1:] for p in parts if p)
    if not out:
        out = "Node"
    if out[0].isdigit():
        out = f"N{out}"
    return out


def _scan(repo: Path) -> Dict[Path, List[Path]]:
    """Return {package_dir: [py_files]} for all dirs that directly contain .py files."""
    packages: Dict[Path, List[Path]] = {}
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        py_files = [Path(dirpath) / name for name in filenames if name.endswith(".py")]
        if py_files:
            packages[Path(dirpath)] = sorted(py_files)
    return packages


def _imports_fast(py: Path) -> Set[str]:
    try:
        source = py.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return set()
    out: Set[str] = set()
    for match in IMPORT_RE.finditer(source):
        out.add(match.group(1))
    for match in FROM_RE.finditer(source):
        module = match.group(1)
        out.add(module)
        imports = match.group(2).split("#", 1)[0]
        for raw_name in imports.split(","):
            name = raw_name.strip().split()[0] if raw_name.strip() else ""
            if not name or name == "(":
                continue
            cleaned = name.strip("()")
            if cleaned and cleaned != "*":
                out.add(f"{module}.{cleaned}")
    return out


def _imports(py: Path, fast: bool = False) -> Set[str]:
    if fast:
        return _imports_fast(py)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            t = ast.parse(py.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception:
        return set()
    out = set()
    for n in ast.walk(t):
        if isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
            for a in n.names:
                out.add(f"{n.module}.{a.name}")
        elif isinstance(n, ast.Import):
            for a in n.names:
                out.add(a.name)
    return out


def _line_range(py: Path) -> Tuple[int, int]:
    try:
        line_count = len(py.read_text(encoding="utf-8-sig", errors="replace").splitlines())
    except Exception:
        line_count = 1
    return (1, max(1, line_count))


def generate(repo: str, root_name: str = "App", fast_imports: bool = False) -> str:
    r = Path(repo)
    packages = _scan(r)
    if not packages:
        return f"@L0 {root_name} [0,0,100,100] {{\n}}\n"

    # sort packages by path depth (shallow first -> root-level files)
    pkg_items = sorted(packages.items(), key=lambda kv: (len(kv[0].relative_to(r).parts), str(kv[0])))

    # layout grid: columns of namespaces across top row
    W = 100
    ncols = max(1, min(5, len(pkg_items)))
    col_w = W // ncols

    # map file -> (ns_name, node_name)
    file_to_node: Dict[Path, Tuple[str, str]] = {}

    ns_blocks: List[str] = []
    ns_used_names: Set[str] = set()
    for i, (pkg_dir, files) in enumerate(pkg_items):
        rel = pkg_dir.relative_to(r)
        base = rel.parts[-1] if rel.parts else root_name
        ns_name = _camel(base) or f"Pkg{i}"
        orig = ns_name
        k = 1
        while ns_name in ns_used_names:
            k += 1; ns_name = f"{orig}{k}"
        ns_used_names.add(ns_name)

        col = i % ncols
        row = i // ncols
        x = col * col_w + 1
        y = row * 40 + 1
        w = col_w - 2
        h = 38

        glob = str(rel).replace("\\", "/") + "/*.py" if str(rel) != "." else "*.py"

        lines = [f'  @L1 {ns_name} [{x},{y},{w},{h}] "{glob}" {{']

        # grid nodes inside
        files_sorted = sorted([f for f in files if f.name != "__init__.py"])
        ncols_in = min(3, max(1, len(files_sorted)))
        nw = max(8, (w - 2) // ncols_in - 1)
        nh = 4
        for j, f in enumerate(files_sorted):
            nn = _camel(f.name)
            # avoid duplicate node names inside same NS
            nodes_seen = [ln for ln in lines if ln.strip().startswith(nn + " ")]
            if nodes_seen:
                nn = nn + str(j)
            file_to_node[f] = (ns_name, nn)
            nx = (j % ncols_in) * (nw + 1) + 1
            ny = (j // ncols_in) * (nh + 1) + 1
            rel_src = f.relative_to(r).as_posix()
            line_start, line_end = _line_range(f)
            lines.append(f"    {nn} [{nx},{ny},{nw},{nh}] {rel_src} lines={line_start}..{line_end}")
        lines.append("  }")
        ns_blocks.append("\n".join(lines))

    # edges: per file, for each import, if it maps to another known file, add `->` edge (deduped)
    # Build resolver: module dotted name -> file
    mod_to_file: Dict[str, Path] = {}
    for pkg_dir, files in pkg_items:
        for f in files:
            if f.name == "__init__.py":
                continue
            rel = f.relative_to(r).with_suffix("")
            dotted = ".".join(rel.parts)
            mod_to_file[dotted] = f
            # also index just the tail (best-effort)
            mod_to_file.setdefault(rel.parts[-1], f)

    edge_set: Set[Tuple[str, str]] = set()
    for f, (ns, node) in file_to_node.items():
        for mod in _imports(f, fast=fast_imports):
            # try exact then prefix matches
            cand = mod_to_file.get(mod)
            if cand is None:
                # try shorter suffixes
                parts = mod.split(".")
                for k in range(len(parts), 0, -1):
                    cand = mod_to_file.get(".".join(parts[:k]))
                    if cand:
                        break
            if cand and cand != f and cand in file_to_node:
                _, tgt_node = file_to_node[cand]
                if tgt_node != node:
                    edge_set.add((node, tgt_node))

    edge_lines = [f"  {s} -> {d}" for (s, d) in sorted(edge_set)]

    out = [f'@L0 {root_name} [0,0,100,100] "{r.as_posix()}" {{']
    out.extend(ns_blocks)
    if edge_lines:
        out.append("  === edges ===")
        out.extend(edge_lines)
    out.append("}")
    return "\n".join(out) + "\n"

