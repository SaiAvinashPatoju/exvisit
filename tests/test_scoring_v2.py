"""Tests for ExVisit vNext: scoring v2, scaffold meta sidecar, locate/expand."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exvisit import parse
from exvisit.blast import build_blast_bundle
from exvisit.graph_meta import GraphMeta, load_for, sidecar_path
from exvisit.scaffold import generate as scaffold_generate, generate_with_meta
from exvisit.scoring_v2 import load_v2_config, score_nodes_v2, select_anchors


def _mk_django_like(root: Path) -> None:
    """Build a tiny tree mimicking the Django ORM registry-file failure mode."""
    pkg = root / "djsmall" / "db" / "models" / "fields"
    pkg.mkdir(parents=True)
    # Registry __init__.py — gold target for the issue below
    (pkg / "__init__.py").write_text(
        "# Field registry\n"
        "class Field:\n"
        "    def __init__(self): pass\n"
        "    def get_default(self):\n"
        "        return self.default\n"
        "class CharField(Field):\n"
        "    pass\n"
        "class AutoField(Field):\n"
        "    pass\n"
        "DEFAULT_NAMES = ('default',)\n"
        "REGISTERED = {'CharField': CharField, 'AutoField': AutoField}\n",
        encoding="utf-8",
    )
    # sibling utility file with similar surface terms (test of F3)
    (pkg / "related.py").write_text(
        "from djsmall.db.models.fields import Field\n"
        "class ForeignKey(Field):\n"
        "    pass\n",
        encoding="utf-8",
    )
    # Same-cluster lexical noise with no useful structural link.
    (pkg / "validators.py").write_text(
        "DEFAULT_FIELD_FLAGS = ('default',)\n"
        "def validate_field(value):\n"
        "    return value\n",
        encoding="utf-8",
    )
    # Test file that *also* mentions Field heavily — should be demoted
    tests = root / "djsmall" / "tests"
    tests.mkdir(parents=True)
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / "test_fields.py").write_text(
        "from djsmall.db.models.fields import Field, CharField\n"
        "class TestField:\n"
        "    def test_default(self):\n"
        "        f = Field(); f.default = 7\n"
        "        assert f.get_default() == 7\n"
        "    def test_charfield(self):\n"
        "        assert CharField().get_default() is None\n",
        encoding="utf-8",
    )


def test_scaffold_includes_registry_init():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _mk_django_like(root)
        out_path = root / "out.exv"
        text, meta_path = generate_with_meta(root.as_posix(), out_path, root_name="DjSmall")
        assert "fields/__init__.py" in text or "__init__.py lines=" in text
        assert meta_path.exists()
        meta = GraphMeta.read(meta_path)
        kinds = {nm.kind for nm in meta.nodes.values()}
        assert "registry" in kinds
        assert "test" in kinds
        # symbols extracted
        any_field_node = next(nm for nm in meta.nodes.values()
                              if nm.kind == "registry" and "Field" in nm.symbols)
        assert "CharField" in any_field_node.symbols


def test_scoring_v2_prefers_registry_over_test_file():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _mk_django_like(root)
        out_path = root / "out.exv"
        generate_with_meta(root.as_posix(), out_path, root_name="DjSmall")
        doc = parse(out_path.read_text(encoding="utf-8"))
        meta = load_for(out_path)
        config = load_v2_config()

        issue = (
            "Bug: Field.get_default() returns the wrong value when CharField "
            "is initialized with no explicit default. Affects the field registry "
            "in the ORM."
        )
        scored = score_nodes_v2(doc, root, issue, meta, config)
        assert scored, "v2 should produce scored nodes"
        top = scored[0].node
        assert top.src_path and "fields/__init__.py" in top.src_path.replace("\\", "/"), (
            f"expected registry __init__ as anchor, got {top.src_path}; "
            f"top5={[(s.node.src_path, round(s.score,3)) for s in scored[:5]]}"
        )
        # Test file must be ranked strictly below the registry — that's the
        # F1 invariant. With this tiny graph it may still appear in top-3,
        # but never as the anchor.
        registry = next(s for s in scored
                        if s.node.src_path and "fields/__init__.py" in s.node.src_path.replace("\\", "/"))
        test_node = next(s for s in scored
                         if s.node.src_path and "test_fields.py" in s.node.src_path.replace("\\", "/"))
        assert registry.score > test_node.score, (
            f"registry score {registry.score:.3f} must beat test {test_node.score:.3f}"
        )


def test_select_anchors_high_confidence_returns_one():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _mk_django_like(root)
        out_path = root / "out.exv"
        generate_with_meta(root.as_posix(), out_path, root_name="DjSmall")
        doc = parse(out_path.read_text(encoding="utf-8"))
        meta = load_for(out_path)
        config = load_v2_config()
        issue = "Field.get_default() crash in field registry __init__ for CharField"
        scored = score_nodes_v2(doc, root, issue, meta, config)
        anchors, conf, low = select_anchors(scored, config)
        assert len(anchors) >= 1
        # should be reasonably confident on this clear case
        assert conf >= 0.30, f"expected decent confidence, got {conf}"


def test_blast_bundle_v2_uses_meta_when_present():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _mk_django_like(root)
        out_path = root / "out.exv"
        generate_with_meta(root.as_posix(), out_path, root_name="DjSmall")
        doc = parse(out_path.read_text(encoding="utf-8"))
        bundle = build_blast_bundle(
            doc, root.as_posix(),
            "Field.get_default returns wrong value for CharField in field registry",
            preset_name="issue-fix",
            exvisit_path=str(out_path),
        )
        assert "fields/__init__.py" in (bundle.anchor_file or "").replace("\\", "/")
        # token estimate should be modest
        assert bundle.token_estimate < 5000


def test_blast_bundle_v2_high_confidence_stays_compact():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _mk_django_like(root)
        out_path = root / "out.exv"
        generate_with_meta(root.as_posix(), out_path, root_name="DjSmall")
        doc = parse(out_path.read_text(encoding="utf-8"))

        bundle = build_blast_bundle(
            doc,
            root.as_posix(),
            "Field.get_default returns wrong value for CharField in field registry",
            preset_name="issue-fix",
            exvisit_path=str(out_path),
        )

        assert bundle.confidence >= 0.30
        assert len(bundle.selected_files) <= 2, bundle.selected_files
        assert "djsmall/db/models/fields/validators.py" not in bundle.selected_files


def test_typed_edges_extracted():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _mk_django_like(root)
        out_path = root / "out.exv"
        generate_with_meta(root.as_posix(), out_path, root_name="DjSmall")
        meta = GraphMeta.read(sidecar_path(out_path))
        # We should have at least 'import' and 'inherit' edges, and possibly 'test-of'
        assert "import" in meta.edges_by_type
        assert "inherit" in meta.edges_by_type
        # related.py inherits from Field → expect inherit edge from Related to Init
        inherit_pairs = meta.edges_by_type["inherit"]
        assert any("Related" in s for s, d in inherit_pairs), (
            f"expected an inherit edge from Related, got {inherit_pairs}"
        )


def test_legacy_exv_without_meta_falls_back_to_v1():
    """If sidecar is missing, build_blast_bundle must still work via v1 ranker."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        pkg = root / "sample"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "worker.py").write_text(
            "def do_work():\n    return 'ok'\n", encoding="utf-8"
        )
        # legacy generate without meta
        text = scaffold_generate(root.as_posix(), root_name="Legacy")
        out_path = root / "legacy.exv"
        out_path.write_text(text, encoding="utf-8")
        doc = parse(text)
        bundle = build_blast_bundle(
            doc, root.as_posix(),
            "Crash in worker.py during do_work",
            preset_name="default",
            exvisit_path=str(out_path),  # no sidecar exists
        )
        assert "worker.py" in (bundle.anchor_file or "")


def test_pagerank_bounded_and_normalized():
    from exvisit.graph_meta import pagerank
    nodes = ["a", "b", "c", "d"]
    edges = [("a", "b", 1.0), ("b", "c", 1.0), ("c", "a", 1.0), ("d", "a", 0.5)]
    pr = pagerank(nodes, edges)
    assert set(pr) == set(nodes)
    s = sum(pr.values())
    assert 0.99 < s < 1.01, f"pagerank mass should be ~1, got {s}"


def test_force_v1_via_scoring_arg():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _mk_django_like(root)
        out_path = root / "out.exv"
        generate_with_meta(root.as_posix(), out_path, root_name="DjSmall")
        doc = parse(out_path.read_text(encoding="utf-8"))
        bundle = build_blast_bundle(
            doc, root.as_posix(),
            "Field.get_default returns wrong value for CharField",
            preset_name="issue-fix",
            exvisit_path=str(out_path),
            scoring="v1",
        )
        # v1 should not crash even with meta available
        assert bundle.anchor
