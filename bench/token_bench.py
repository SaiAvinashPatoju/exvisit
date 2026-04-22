"""Token efficiency benchmark: .exv vs JSON equivalent vs raw source.

Uses tiktoken (cl100k_base) if installed, else a calibrated word/char estimator.
"""
from __future__ import annotations
import json
import sys
import os
import glob
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from exvisit import parse
from exvisit.ast import EdgeKind


def estimate_tokens(text: str) -> int:
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        # cl100k averages ~4 chars/token on code+prose
        return max(1, len(text) // 4)


def doc_to_json(doc) -> str:
    def ns_dict(ns):
        return {
            "level": ns.level, "name": ns.name, "bounds": list(ns.bounds),
            "src_glob": ns.src_glob, "path": ns.path,
            "nodes": [
                {"name": n.name, "bounds": list(n.bounds),
                 "src_path": n.src_path, "states": n.states, "fqn": n.fqn}
                for n in ns.nodes
            ],
            "children": [ns_dict(c) for c in ns.children],
        }
    return json.dumps({
        "root": ns_dict(doc.root),
        "edges": [{"src": e.src, "dst": e.dst,
                   "kind": "sync" if e.kind == EdgeKind.SYNC else "async"}
                  for e in doc.edges],
    }, indent=2)


def doc_to_json_compact(doc) -> str:
    def ns_dict(ns):
        return {
            "level": ns.level, "name": ns.name, "bounds": list(ns.bounds),
            "src_glob": ns.src_glob, "path": ns.path,
            "nodes": [
                {"name": n.name, "bounds": list(n.bounds),
                 "src_path": n.src_path, "states": n.states, "fqn": n.fqn}
                for n in ns.nodes
            ],
            "children": [ns_dict(c) for c in ns.children],
        }
    return json.dumps({
        "root": ns_dict(doc.root),
        "edges": [{"src": e.src, "dst": e.dst,
                   "kind": "sync" if e.kind == EdgeKind.SYNC else "async"}
                  for e in doc.edges],
    }, separators=(",", ":"))


def scan_sources(root: str, patterns=("*.py",)) -> str:
    buf = []
    base = Path(root)
    if not base.exists():
        return ""
    for pat in patterns:
        for path in base.rglob(pat):
            # skip noise dirs
            if any(part in {".git", "__pycache__", ".pytest_cache", "node_modules"}
                   for part in path.parts):
                continue
            try:
                buf.append(f"# === {path.relative_to(base)} ===\n")
                buf.append(path.read_text(encoding="utf-8", errors="replace"))
                buf.append("\n")
            except Exception:
                pass
    return "".join(buf)


def scan_sources_imports_only(root: str) -> str:
    """Lighter architecture-inference payload: just module name + import lines + class/def signatures."""
    buf = []
    base = Path(root)
    if not base.exists():
        return ""
    for path in base.rglob("*.py"):
        if any(part in {".git", "__pycache__", ".pytest_cache"} for part in path.parts):
            continue
        try:
            rel = path.relative_to(base)
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            sig = [l for l in lines
                   if l.startswith(("import ", "from ", "class ", "def "))
                   or l.startswith(("    def ",))]
            if sig:
                buf.append(f"# {rel}\n")
                buf.extend(s + "\n" for s in sig)
                buf.append("\n")
        except Exception:
            pass
    return "".join(buf)


def main():
    if len(sys.argv) < 2:
        print("usage: token_bench.py <path/to/file.exv> [repo_root]")
        sys.exit(2)
    exvisit_path = sys.argv[1]
    repo_root = sys.argv[2] if len(sys.argv) > 2 else None

    exvisit_src = Path(exvisit_path).read_text(encoding="utf-8")
    doc = parse(exvisit_src)

    json_pretty = doc_to_json(doc)
    json_compact = doc_to_json_compact(doc)

    results = [
        ("exvisit (canonical)", exvisit_src),
        ("json (indent=2)",   json_pretty),
        ("json (compact)",    json_compact),
    ]

    if repo_root:
        results.append(("repo: full source",       scan_sources(repo_root)))
        results.append(("repo: imports+sigs only", scan_sources_imports_only(repo_root)))

    print(f"\n{'format':28}  {'bytes':>10}  {'tokens':>10}  {'vs exvisit':>10}")
    print("-" * 66)
    exvisit_tok = estimate_tokens(exvisit_src)
    for label, text in results:
        tok = estimate_tokens(text)
        ratio = tok / exvisit_tok if exvisit_tok else 0
        print(f"{label:28}  {len(text):>10}  {tok:>10}  {ratio:>9.2f}x")

    # report
    print()
    print(f"exvisit             : {exvisit_tok:>6} tok")
    print(f"json (compact)    : {estimate_tokens(json_compact):>6} tok  "
          f"(exvisit saves {100*(1-exvisit_tok/max(1,estimate_tokens(json_compact))):.1f}%)")
    if repo_root:
        full = estimate_tokens(scan_sources(repo_root))
        print(f"repo full source  : {full:>6} tok  "
              f"(exvisit is {full/max(1,exvisit_tok):.1f}x smaller)")


if __name__ == "__main__":
    main()

