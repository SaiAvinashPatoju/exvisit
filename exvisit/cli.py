"""Command-line interface: `python -m exvisit <cmd>`."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from . import parse, serialize, query, exvisitGraph
from .anchor import build_anchor_report, anchor_report_to_json, render_anchor_text
from .blast import build_blast_bundle, bundle_to_json, render_blast_markdown
from .verify import verify, format_report
from .scaffold import generate as scaffold_generate


def cmd_parse(args):
    src = Path(args.file).read_text(encoding="utf-8")
    doc = parse(src)
    if args.roundtrip:
        out = serialize(doc)
        again = parse(out)
        out2 = serialize(again)
        ok = out == out2
        print(out, end="")
        print(f"\n# roundtrip {'OK' if ok else 'FAIL'}", file=sys.stderr)
        sys.exit(0 if ok else 1)
    print(f"namespaces: {sum(1 for _ in doc.root.iter_namespaces())}")
    print(f"nodes     : {len(doc.all_nodes())}")
    print(f"edges     : {len(doc.edges)}")


def cmd_query(args):
    src = Path(args.file).read_text(encoding="utf-8")
    doc = parse(src)
    out = query(doc, args.target, hops=args.neighbors, direction=args.direction)
    sys.stdout.write(out)


def cmd_deps(args):
    src = Path(args.file).read_text(encoding="utf-8")
    doc = parse(src)
    sys.stdout.write(query(doc, args.target, hops=args.hops, direction="out"))


def cmd_callers(args):
    src = Path(args.file).read_text(encoding="utf-8")
    doc = parse(src)
    sys.stdout.write(query(doc, args.target, hops=args.hops, direction="in"))


def cmd_graph(args):
    src = Path(args.file).read_text(encoding="utf-8")
    doc = parse(src)
    g = exvisitGraph.from_doc(doc)
    doc2 = g.to_doc()
    sys.stdout.write(serialize(doc2))


def cmd_verify(args):
    src = Path(args.file).read_text(encoding="utf-8")
    doc = parse(src)
    diags = verify(doc, args.repo)
    sys.stdout.write(format_report(diags))
    sys.exit(1 if any(d.kind in ("missing", "ghost") for d in diags) else 0)


def cmd_init(args):
    out = scaffold_generate(args.repo, root_name=args.root_name)
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
        print(f"wrote {args.out} ({len(out)} bytes)")
    else:
        sys.stdout.write(out)


def _load_blast_text(args):
    if args.issue_text:
        return args.issue_text
    for candidate in (args.issue_file, args.error_file):
        if candidate:
            return Path(candidate).read_text(encoding="utf-8", errors="replace")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise ValueError("provide --issue-text, --issue-file, --error-file, or pipe text on stdin")


def _load_anchor_text(args):
    if args.stacktrace:
        return Path(args.stacktrace).read_text(encoding="utf-8", errors="replace")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise ValueError("provide --stacktrace or pipe trace text on stdin")


def _infer_repo_root(doc, exvisit_file: Path, repo_hint: str | None):
    if repo_hint:
        return repo_hint
    if doc.root.src_glob:
        candidate = Path(doc.root.src_glob)
        if candidate.exists():
            return str(candidate)
    return str(exvisit_file.resolve().parent)


def cmd_blast(args):
    exvisit_path = Path(args.file)
    src = exvisit_path.read_text(encoding="utf-8")
    doc = parse(src)
    text = _load_blast_text(args)
    bundle = build_blast_bundle(
        doc,
        args.repo,
        text,
        preset_name=args.preset,
        config_path=args.config,
    )
    output = render_blast_markdown(bundle, text) if args.format == "md" else bundle_to_json(bundle)
    if args.out:
        Path(args.out).write_text(output, encoding="utf-8")
        print(f"wrote {args.out}")
        return
    sys.stdout.write(output)


def cmd_anchor(args):
    exvisit_path = Path(args.file)
    src = exvisit_path.read_text(encoding="utf-8")
    doc = parse(src)
    trace_text = _load_anchor_text(args)
    report = build_anchor_report(
        doc,
        _infer_repo_root(doc, exvisit_path, args.repo),
        trace_text,
        max_hits=args.max_hits,
    )
    output = render_anchor_text(report) if args.format == "text" else anchor_report_to_json(report)
    if args.out:
        Path(args.out).write_text(output, encoding="utf-8")
        print(f"wrote {args.out}")
        return
    sys.stdout.write(output)


def main(argv=None):
    p = argparse.ArgumentParser(prog="exvisit")
    sub = p.add_subparsers(required=True)

    pp = sub.add_parser("parse", help="parse and report / roundtrip")
    pp.add_argument("file"); pp.add_argument("--roundtrip", action="store_true")
    pp.set_defaults(func=cmd_parse)

    pq = sub.add_parser("query", help="extract topological slice")
    pq.add_argument("file"); pq.add_argument("--target", required=True)
    pq.add_argument("--neighbors", type=int, default=1)
    pq.add_argument("--direction", choices=["in", "out", "both"], default="both")
    pq.set_defaults(func=cmd_query)

    pd = sub.add_parser("deps", help="outbound dependencies of a node (SPEC-005)")
    pd.add_argument("file"); pd.add_argument("target"); pd.add_argument("--hops", type=int, default=1)
    pd.set_defaults(func=cmd_deps)

    pc = sub.add_parser("callers", help="inbound callers of a node (SPEC-005)")
    pc.add_argument("file"); pc.add_argument("target"); pc.add_argument("--hops", type=int, default=1)
    pc.set_defaults(func=cmd_callers)

    pg = sub.add_parser("graph", help="load into CRDT graph and re-emit")
    pg.add_argument("file"); pg.set_defaults(func=cmd_graph)

    pv = sub.add_parser("verify", help="cross-check -> edges against real imports (SPEC-001)")
    pv.add_argument("file"); pv.add_argument("--repo", required=True)
    pv.set_defaults(func=cmd_verify)

    pi = sub.add_parser("init", help="scaffold draft .exv from a repo (SPEC-003)")
    pi.add_argument("--repo", required=True); pi.add_argument("--out", default=None)
    pi.add_argument("--root-name", default="App")
    pi.set_defaults(func=cmd_init)

    pb = sub.add_parser("blast", help="build a blast-radius context bundle from issue/error text")
    pb.add_argument("file")
    pb.add_argument("--repo", required=True)
    pb.add_argument("--issue-text", default=None)
    pb.add_argument("--issue-file", default=None)
    pb.add_argument("--error-file", default=None)
    pb.add_argument("--preset", default="test-fix")
    pb.add_argument("--config", default=None)
    pb.add_argument("--format", choices=["json", "md"], default="md")
    pb.add_argument("--out", default=None)
    pb.set_defaults(func=cmd_blast)

    pa = sub.add_parser("anchor", help="resolve raw error logs or stack traces to exvisit anchors")
    pa.add_argument("file")
    pa.add_argument("--repo", default=None)
    pa.add_argument("--stacktrace", default=None)
    pa.add_argument("--format", choices=["text", "json"], default="text")
    pa.add_argument("--max-hits", type=int, default=6)
    pa.add_argument("--out", default=None)
    pa.set_defaults(func=cmd_anchor)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

