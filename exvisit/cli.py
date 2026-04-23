"""Command-line interface: `python -m exvisit <cmd>`."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from . import parse, serialize, query, exvisitGraph
from .anchor import build_anchor_report, anchor_report_to_json, render_anchor_text
from .blast import build_blast_bundle, bundle_to_json, render_blast_markdown, _neighbors
from .graph_meta import load_for as load_meta_for, sidecar_path
from .scoring_v2 import load_v2_config, score_nodes_v2, select_anchors
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
    meta_out = None
    if args.out and not args.no_meta:
        meta_out = sidecar_path(Path(args.out))
    out = scaffold_generate(args.repo, root_name=args.root_name, meta_out=meta_out)
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
        msg = f"wrote {args.out} ({len(out)} bytes)"
        if meta_out and meta_out.exists():
            msg += f"; wrote {meta_out}"
        print(msg)
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
        exvisit_path=str(exvisit_path),
        scoring=args.scoring,
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


def cmd_locate(args):
    """vNext: top-K anchor selection via scoring v2 with margin reporting."""
    exvisit_path = Path(args.file)
    src = exvisit_path.read_text(encoding="utf-8")
    doc = parse(src)
    text = _load_blast_text(args)
    meta = load_meta_for(exvisit_path)
    config = load_v2_config()
    scored = score_nodes_v2(doc, Path(args.repo), text, meta, config)
    if not scored:
        sys.stderr.write("locate: no candidates\n")
        sys.exit(2)
    anchors, confidence, low_margin = select_anchors(scored, config)
    topk = scored[: max(args.topk, len(anchors))]

    payload = {
        "confidence": confidence,
        "low_margin": low_margin,
        "anchors": [
            {
                "fqn": s.node.fqn,
                "src_path": s.node.src_path,
                "score": round(s.score, 4),
                "components": {k: round(v, 4) for k, v in s.components.items()},
                "reasons": s.reasons,
            }
            for s in topk
        ],
        "meta_present": meta is not None,
    }
    if args.format == "json":
        out_text = json.dumps(payload, indent=2)
    else:
        lines = [
            f"# exvisit locate (confidence={confidence}, low_margin={low_margin}, meta={'yes' if meta else 'no'})",
            "",
        ]
        for i, s in enumerate(topk, start=1):
            lines.append(f"[{i}] {s.node.fqn}  score={s.score:.3f}")
            lines.append(f"    src={s.node.src_path}")
            lines.append(f"    reasons={', '.join(s.reasons[:6])}")
        out_text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).write_text(out_text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(out_text)


def cmd_expand(args):
    """vNext: weighted-neighborhood expansion around an anchor."""
    exvisit_path = Path(args.file)
    src = exvisit_path.read_text(encoding="utf-8")
    doc = parse(src)
    meta = load_meta_for(exvisit_path)
    fqns = _neighbors(doc, args.anchor, hops=args.hops, direction="both")
    fqns.add(args.anchor)
    nodes = [n for n in doc.all_nodes() if n.fqn in fqns]

    # weight neighbors by per-edge prior * destination pagerank when meta present
    weights: dict = {}
    if meta is not None:
        edge_priors = meta.edge_priors

        def _pr(fqn: str) -> float:
            nm = meta.nodes.get(fqn)
            return nm.pagerank if nm else 0.0

        for etype, pairs in meta.edges_by_type.items():
            w = edge_priors.get(etype, 0.1)
            for s, d in pairs:
                if s == args.anchor and d in fqns:
                    weights[d] = max(weights.get(d, 0.0), w * (1.0 + _pr(d)))
                if d == args.anchor and s in fqns:
                    weights[s] = max(weights.get(s, 0.0), w * (1.0 + _pr(s)))
    nodes.sort(key=lambda n: (n.fqn != args.anchor, -weights.get(n.fqn, 0.0), n.fqn))
    nodes = nodes[: args.max_files]

    payload = {
        "anchor": args.anchor,
        "neighbors": [
            {"fqn": n.fqn, "src_path": n.src_path, "weight": round(weights.get(n.fqn, 0.0), 4)}
            for n in nodes
            if n.fqn != args.anchor
        ],
    }
    if args.format == "json":
        sys.stdout.write(json.dumps(payload, indent=2))
    else:
        sys.stdout.write(f"# exvisit expand: {args.anchor}\n")
        for n in nodes:
            if n.fqn == args.anchor:
                continue
            sys.stdout.write(f"  {n.fqn}  w={weights.get(n.fqn, 0.0):.3f}  src={n.src_path}\n")


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
    pi.add_argument("--no-meta", action="store_true",
                    help="skip writing the .meta.json sidecar (legacy v1-only output)")
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
    pb.add_argument("--scoring", choices=["v1", "v2"], default=None,
                    help="force ranker version (default: v2 if .meta.json present)")
    pb.add_argument("--out", default=None)
    pb.set_defaults(func=cmd_blast)

    pl = sub.add_parser("locate",
                        help="vNext: rank nodes via scoring v2 and emit top-K anchors with confidence")
    pl.add_argument("file")
    pl.add_argument("--repo", required=True)
    pl.add_argument("--issue-text", default=None)
    pl.add_argument("--issue-file", default=None)
    pl.add_argument("--error-file", default=None)
    pl.add_argument("--topk", type=int, default=3)
    pl.add_argument("--format", choices=["json", "text"], default="text")
    pl.add_argument("--out", default=None)
    pl.set_defaults(func=cmd_locate)

    pe = sub.add_parser("expand",
                        help="vNext: weighted-traversal neighborhood around an anchor")
    pe.add_argument("file")
    pe.add_argument("--anchor", required=True, help="anchor FQN to expand")
    pe.add_argument("--hops", type=int, default=1)
    pe.add_argument("--max-files", type=int, default=4)
    pe.add_argument("--format", choices=["json", "text"], default="text")
    pe.set_defaults(func=cmd_expand)

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

