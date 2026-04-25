"""exvisit init --from-repo — generate a draft .exv from a Python repo.

Heuristics:
  - Each top-level package dir under repo/ becomes an @L1 namespace.
  - Each .py file becomes a Node (name = CamelCase of filename stem).
  - Imports of internal modules seed `->` edges.
  - Layout: simple grid per namespace.

vNext additions (sidecar `.meta.json`):
  - Always-include policy for registry files (`__init__.py`, `apps.py`,
    `urls.py`, `admin.py`, `settings.py`, `manage.py`, migrations).
  - Multi-typed edge extraction (import, inherit, call, config-ref, test-of).
  - Per-node `kind`, `symbols`, `loc`, `pagerank`, `cluster` written to
    a sidecar file.

Output is deliberately minimal; user is expected to refine bounds + state machines.
"""
from __future__ import annotations
import ast
import fnmatch
import os
import re
import warnings
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .graph_meta import DEFAULT_EDGE_PRIORS, GraphMeta, NodeMeta, pagerank, sidecar_path

SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", ".nox", ".venv", "venv", "env", "ENV",
    "node_modules", ".next", "build", "dist", "target", "site-packages",
    ".eggs",
}
IMPORT_RE = re.compile(r"^\s*import\s+([A-Za-z_][A-Za-z0-9_\.]*)", re.MULTILINE)
FROM_RE = re.compile(r"^\s*from\s+([A-Za-z_][A-Za-z0-9_\.]*)\s+import\s+(.+)$", re.MULTILINE)

# Files that are always included even when otherwise filtered (registry/wiring
# files where the bug often lives but lexical signal is weak).
ALWAYS_INCLUDE_NAMES = {
    "__init__.py", "apps.py", "urls.py", "admin.py", "settings.py",
    "manage.py", "wsgi.py", "asgi.py",
}
REGISTRY_NAMES = {
    "__init__.py", "apps.py", "urls.py", "admin.py", "settings.py",
    "global_settings.py", "config.py",
}
# Heuristic threshold: __init__.py with > N lines is treated as a real
# registry/aggregator module rather than an empty package marker.
REGISTRY_INIT_MIN_LOC = 10

# Match dotted string literals that look like Django-style "app.Model" or
# "package.module" config refs.
CONFIG_REF_RE = re.compile(r"['\"]([a-z_][a-z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)+)['\"]")


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
    return _scan_with_ignores(repo, ignore_file=None)


def _load_ignore_patterns(repo: Path, ignore_file: Optional[str]) -> List[str]:
    candidate = Path(ignore_file) if ignore_file else repo / ".exvisitignore"
    if not candidate.exists():
        return []
    patterns: List[str] = []
    for raw in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line.replace("\\", "/").rstrip("/"))
    return patterns


def _matches_ignore(rel_posix: str, name: str, patterns: List[str]) -> bool:
    rel_norm = rel_posix.replace("\\", "/").lstrip("./")
    for pattern in patterns:
        pat = pattern.replace("\\", "/").lstrip("./")
        if fnmatch.fnmatch(rel_norm, pat) or fnmatch.fnmatch(name, pat):
            return True
        if fnmatch.fnmatch(rel_norm, pat + "/*"):
            return True
    return False


def _looks_like_virtualenv(path: Path) -> bool:
    return (
        (path / "pyvenv.cfg").exists()
        or (path / "Lib" / "site-packages").exists()
        or (path / "Scripts" / "activate").exists()
        or (path / "bin" / "activate").exists()
    )


def _should_skip_dir(repo: Path, parent: Path, name: str, patterns: List[str]) -> bool:
    candidate = parent / name
    rel_posix = candidate.relative_to(repo).as_posix()
    if name in SKIP_DIRS:
        return True
    if name.startswith(".exvisit-") or name.endswith(".egg-info"):
        return True
    if _looks_like_virtualenv(candidate):
        return True
    if _matches_ignore(rel_posix, name, patterns):
        return True
    return False


def _should_skip_file(repo: Path, file_path: Path, patterns: List[str]) -> bool:
    rel_posix = file_path.relative_to(repo).as_posix()
    return _matches_ignore(rel_posix, file_path.name, patterns)


def _scan_with_ignores(repo: Path, ignore_file: Optional[str]) -> Dict[Path, List[Path]]:
    """Return {package_dir: [py_files]} for all dirs that directly contain .py files."""
    patterns = _load_ignore_patterns(repo, ignore_file)
    packages: Dict[Path, List[Path]] = {}
    for dirpath, dirnames, filenames in os.walk(repo):
        parent = Path(dirpath)
        dirnames[:] = [
            name for name in dirnames
            if not _should_skip_dir(repo, parent, name, patterns)
        ]
        py_files = [
            parent / name
            for name in filenames
            if name.endswith(".py") and not _should_skip_file(repo, parent / name, patterns)
        ]
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


def _line_count(py: Path) -> int:
    """Return line count of a file, 0 on error."""
    try:
        return len(py.read_text(encoding="utf-8-sig", errors="replace").splitlines())
    except Exception:
        return 0


def _classify_kind(rel_posix: str, name: str, loc: int) -> str:
    """Return one of: 'registry' | 'test' | 'migration' | 'normal'."""
    parts = rel_posix.split("/")
    if any(p == "tests" or p.startswith("tests") or p.startswith("test_") for p in parts):
        return "test"
    if name.startswith("test_") or name == "tests.py" or name == "conftest.py":
        return "test"
    if "migrations" in parts and name.startswith("0") and name.endswith(".py"):
        return "migration"
    if name in REGISTRY_NAMES and not (name == "__init__.py" and loc < REGISTRY_INIT_MIN_LOC):
        return "registry"
    return "normal"


def _should_include(py: Path, repo: Path) -> Tuple[bool, int]:
    """Decide whether a file is admitted as a node. Returns (include, loc)."""
    loc = _line_count(py)
    name = py.name
    if name in ALWAYS_INCLUDE_NAMES:
        # __init__.py only included if it has real content
        if name == "__init__.py" and loc < REGISTRY_INIT_MIN_LOC:
            return False, loc
        return True, loc
    return True, loc


def _extract_symbols(py: Path) -> List[str]:
    """Top-level class/function names in the file, deduplicated, ≤ 32."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(py.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception:
        return []
    seen: Set[str] = set()
    out: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name not in seen:
                seen.add(node.name)
                out.append(node.name)
        if len(out) >= 32:
            break
    return out


def _extract_typed_edges(py: Path) -> Dict[str, Set[str]]:
    """Return {edge_type: {target_module_or_symbol, ...}}.

    Edge types:
      * 'import'     — from X import Y / import X
      * 'inherit'    — class Foo(Bar): -> Bar
      * 'call'       — function/method calls to imported names
      * 'config-ref' — string literals matching dotted package paths
    """
    out: Dict[str, Set[str]] = {"import": set(), "inherit": set(), "call": set(), "config-ref": set()}
    try:
        source = py.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return out
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(source)
    except Exception:
        # fall back to regex-based imports only
        out["import"] = _imports_fast(py)
        return out

    # Collect imported names for call-edge detection
    imported_names: Set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            out["import"].add(node.module)
            for a in node.names:
                out["import"].add(f"{node.module}.{a.name}")
                imported_names.add(a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                out["import"].add(a.name)
                imported_names.add(a.name.split(".")[-1])
        elif isinstance(node, ast.ClassDef):
            for base in node.bases:
                # capture 'Foo' or 'pkg.Foo'
                if isinstance(base, ast.Name):
                    out["inherit"].add(base.id)
                elif isinstance(base, ast.Attribute):
                    parts: List[str] = []
                    cur: Optional[ast.AST] = base
                    while isinstance(cur, ast.Attribute):
                        parts.append(cur.attr)
                        cur = cur.value
                    if isinstance(cur, ast.Name):
                        parts.append(cur.id)
                        out["inherit"].add(".".join(reversed(parts)))

    # Extract call edges: function/method calls to imported names
    # This distinguishes "uses" from "merely imports" within a namespace
    call_count = 0
    for node in ast.walk(tree):
        if call_count >= 128:
            break
        if isinstance(node, ast.Call):
            callee = node.func
            if isinstance(callee, ast.Name) and callee.id in imported_names:
                out["call"].add(callee.id)
                call_count += 1
            elif isinstance(callee, ast.Attribute):
                # obj.method() — if obj is an imported name
                if isinstance(callee.value, ast.Name) and callee.value.id in imported_names:
                    out["call"].add(callee.value.id)
                    call_count += 1

    # config-ref string literals — capped to avoid noise
    refs = 0
    for match in CONFIG_REF_RE.finditer(source):
        out["config-ref"].add(match.group(1))
        refs += 1
        if refs >= 64:
            break
    return out


def _extract_migration_edges(py: Path) -> Set[str]:
    """Parse a Django migration file to extract model references.

    Returns a set of model/module references the migration modifies
    (from CreateModel, AlterField, AddField, etc. operations).
    """
    try:
        source = py.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return set()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(source)
    except Exception:
        return set()

    refs: Set[str] = set()
    # Walk for migrations.CreateModel, migrations.AlterField, etc.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match migrations.X() or operations like CreateModel, AddField
        func_name = ""
        if isinstance(func, ast.Attribute):
            func_name = func.attr
        elif isinstance(func, ast.Name):
            func_name = func.id
        if func_name not in (
            "CreateModel", "AlterField", "AddField", "RemoveField",
            "RenameField", "AlterModelOptions", "AlterModelManagers",
            "AlterModelTable", "DeleteModel", "RenameModel",
            "AddIndex", "RemoveIndex", "AddConstraint", "RemoveConstraint",
            "AlterUniqueTogether", "AlterIndexTogether", "AlterOrderWithRespectTo",
            "RunPython", "RunSQL", "SeparateDatabaseAndState",
        ):
            continue
        # Extract model_name from keyword args or first positional
        for kw in node.keywords:
            if kw.arg in ("model_name", "name") and isinstance(kw.value, ast.Constant):
                refs.add(str(kw.value.value).lower())
        if node.args and isinstance(node.args[0], ast.Constant):
            refs.add(str(node.args[0].value).lower())
    # Also capture dependencies = [(app_label, migration_name), ...]
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "dependencies":
                    if isinstance(node.value, ast.List):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Tuple) and len(elt.elts) >= 1:
                                if isinstance(elt.elts[0], ast.Constant):
                                    refs.add(str(elt.elts[0].value).lower())
    return refs


def _line_range(py: Path) -> Tuple[int, int]:
    try:
        line_count = len(py.read_text(encoding="utf-8-sig", errors="replace").splitlines())
    except Exception:
        line_count = 1
    return (1, max(1, line_count))


def generate(repo: str, root_name: str = "App", fast_imports: bool = False,
             meta_out: Optional[Path] = None, ignore_file: Optional[str] = None) -> str:
    """Generate `.exv` text from a repo. If `meta_out` is given, also write
    a sidecar `<meta_out>` containing the GraphMeta JSON."""
    r = Path(repo)
    packages = _scan_with_ignores(r, ignore_file)
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
    # vNext: collect per-node meta entries
    node_meta: Dict[str, NodeMeta] = {}
    file_loc: Dict[Path, int] = {}

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

        # vNext file inclusion: always include registry/__init__ files when
        # they have real content. Other __init__.py with < REGISTRY_INIT_MIN_LOC
        # lines remain excluded (avoids empty package markers).
        files_admitted: List[Tuple[Path, int]] = []
        for f in sorted(files):
            include, loc = _should_include(f, r)
            if include:
                files_admitted.append((f, loc))
        ncols_in = min(3, max(1, len(files_admitted)))
        nw = max(8, (w - 2) // ncols_in - 1)
        nh = 4
        for j, (f, loc) in enumerate(files_admitted):
            nn = _camel(f.name)
            # avoid duplicate node names inside same NS
            nodes_seen = [ln for ln in lines if ln.strip().startswith(nn + " ")]
            if nodes_seen:
                nn = nn + str(j)
            file_to_node[f] = (ns_name, nn)
            file_loc[f] = loc
            nx = (j % ncols_in) * (nw + 1) + 1
            ny = (j // ncols_in) * (nh + 1) + 1
            rel_src = f.relative_to(r).as_posix()
            line_start, line_end = _line_range(f)
            lines.append(f"    {nn} [{nx},{ny},{nw},{nh}] {rel_src} lines={line_start}..{line_end}")

            # vNext meta: kind + symbols + cluster
            kind = _classify_kind(rel_src, f.name, loc)
            cluster = str(Path(rel_src).parent).replace("\\", "/")
            symbols = _extract_symbols(f)
            fqn = f"{root_name}.{ns_name}.{nn}"
            node_meta[fqn] = NodeMeta(
                fqn=fqn, kind=kind, symbols=symbols, loc=loc,
                pagerank=0.0, cluster=cluster,
            )
        lines.append("  }")
        ns_blocks.append("\n".join(lines))

    # ---- edge resolution (typed) -----------------------------------------
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
    # symbol → owning file (for inherit edges)
    sym_to_file: Dict[str, Path] = {}
    for f, (_, _) in file_to_node.items():
        for sym in _extract_symbols(f):
            # first-writer wins (deterministic by sorted iteration above)
            sym_to_file.setdefault(sym, f)

    typed_edges: Dict[str, Set[Tuple[str, str]]] = {
        "import": set(), "inherit": set(), "call": set(), "config-ref": set(), "test-of": set(),
    }
    flat_edge_set: Set[Tuple[str, str]] = set()
    for f, (ns, node) in file_to_node.items():
        edges = _extract_typed_edges(f) if not fast_imports else {"import": _imports_fast(f), "inherit": set(), "call": set(), "config-ref": set()}
        for etype, targets in edges.items():
            for tgt in targets:
                cand: Optional[Path] = mod_to_file.get(tgt)
                if cand is None and etype in ("inherit", "call"):
                    cand = sym_to_file.get(tgt.split(".")[-1])
                if cand is None:
                    parts = tgt.split(".")
                    for kk in range(len(parts), 0, -1):
                        cand = mod_to_file.get(".".join(parts[:kk]))
                        if cand:
                            break
                if cand and cand != f and cand in file_to_node:
                    _, tgt_node = file_to_node[cand]
                    if tgt_node != node:
                        typed_edges[etype].add((node, tgt_node))
                        flat_edge_set.add((node, tgt_node))
        # Migration edges: link migration files to the models they modify
        rel_posix = f.relative_to(r).as_posix()
        kind = _classify_kind(rel_posix, f.name, file_loc.get(f, 0))
        if kind == "migration":
            migration_refs = _extract_migration_edges(f)
            for ref in migration_refs:
                # Try to find the model/module file this migration references
                cand = mod_to_file.get(ref)
                if cand is None:
                    cand = sym_to_file.get(ref)
                if cand and cand != f and cand in file_to_node:
                    _, tgt_node = file_to_node[cand]
                    if tgt_node != node:
                        typed_edges.setdefault("migration-of", set()).add((node, tgt_node))
                        flat_edge_set.add((node, tgt_node))
        # test-of: heuristic mapping tests/test_X.py → src/X.py
        if f.name.startswith("test_") or "/tests/" in f.as_posix():
            stem = f.stem
            if stem.startswith("test_"):
                stem = stem[len("test_"):]
            cand = sym_to_file.get(stem) or mod_to_file.get(stem)
            if cand and cand != f and cand in file_to_node:
                _, tgt_node = file_to_node[cand]
                if tgt_node != node:
                    typed_edges["test-of"].add((node, tgt_node))

    edge_lines = [f"  {s} -> {d}" for (s, d) in sorted(flat_edge_set)]

    out = [f'@L0 {root_name} [0,0,100,100] "{r.as_posix()}" {{']
    out.extend(ns_blocks)
    if edge_lines:
        out.append("  === edges ===")
        out.extend(edge_lines)
    out.append("}")
    exv_text = "\n".join(out) + "\n"

    # ---- meta sidecar -----------------------------------------------------
    if meta_out is not None:
        # build short-name → fqn map for edge re-keying
        short_to_fqn: Dict[str, str] = {}
        for fqn in node_meta:
            short = fqn.rsplit(".", 1)[-1]
            short_to_fqn.setdefault(short, fqn)
        weighted_pr_edges: List[Tuple[str, str, float]] = []
        edges_by_type_serial: Dict[str, List[List[str]]] = {}
        for etype, pairs in typed_edges.items():
            serial = []
            for s, d in sorted(pairs):
                s_fqn = short_to_fqn.get(s, s)
                d_fqn = short_to_fqn.get(d, d)
                serial.append([s_fqn, d_fqn])
                w = DEFAULT_EDGE_PRIORS.get(etype, 0.1)
                # test-of edges contribute reverse weight (demotes test side)
                if etype == "test-of":
                    weighted_pr_edges.append((d_fqn, s_fqn, w))
                else:
                    weighted_pr_edges.append((s_fqn, d_fqn, w))
            edges_by_type_serial[etype] = serial

        ranks = pagerank(list(node_meta.keys()), weighted_pr_edges)
        for fqn, r_val in ranks.items():
            node_meta[fqn].pagerank = r_val

        cluster_size: Dict[str, int] = {}
        for nm in node_meta.values():
            cluster_size[nm.cluster] = cluster_size.get(nm.cluster, 0) + 1

        meta = GraphMeta(
            nodes=node_meta,
            edges_by_type=edges_by_type_serial,
            edge_priors=dict(DEFAULT_EDGE_PRIORS),
            cluster_size=cluster_size,
        )
        meta.write(meta_out)

    return exv_text


def generate_with_meta(repo: str, exv_out: Path, root_name: str = "App",
                       fast_imports: bool = False, ignore_file: Optional[str] = None) -> Tuple[str, Path]:
    """Convenience: generate `.exv` and write both `.exv` and sidecar `.meta.json`."""
    meta_path = sidecar_path(exv_out)
    text = generate(
        repo,
        root_name=root_name,
        fast_imports=fast_imports,
        meta_out=meta_path,
        ignore_file=ignore_file,
    )
    exv_out.write_text(text, encoding="utf-8")
    return text, meta_path

