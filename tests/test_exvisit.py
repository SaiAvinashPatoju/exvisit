"""Parser + roundtrip + query tests."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exvisit.cli import _infer_repo_root
from exvisit import parse, serialize, query, exvisitGraph
from exvisit.ast import EdgeKind


SIMPLE = """
@L0 App [0,0,100,100] {
  @L1 Core [5,5,40,90] "src/core/*.py" {
        Scene       [1,1,12,8]  scene.py lines=10..40        {empty -> loaded -> dirty}
        DicomLoader [14,1,12,6] dicom_loader.py lines=41..72 {idle -> loading -> ready}
  }
  === edges ===
  DicomLoader -> Scene
  Scene ~> Scene
}
"""


def test_parse_basic():
    doc = parse(SIMPLE)
    assert doc.root.name == "App"
    assert len(doc.root.children) == 1
    core = doc.root.children[0]
    assert core.name == "Core"
    assert len(core.nodes) == 2
    scene = core.nodes[0]
    assert scene.bounds == (1, 1, 12, 8)
    assert scene.src_path == "scene.py"
    assert scene.line_range == (10, 40)
    assert scene.states == ["empty", "loaded", "dirty"]
    assert len(doc.edges) == 2
    assert doc.edges[0].kind == EdgeKind.SYNC
    assert doc.edges[1].kind == EdgeKind.ASYNC


def test_roundtrip():
    doc = parse(SIMPLE)
    out = serialize(doc)
    doc2 = parse(out)
    out2 = serialize(doc2)
    assert out == out2, f"roundtrip mismatch:\n--- first ---\n{out}\n--- second ---\n{out2}"


def test_fqn():
    doc = parse(SIMPLE)
    scene = doc.find_node("Scene")
    assert scene is not None
    assert scene.fqn == "App.Core.Scene"


def test_crdt_doc_roundtrip():
    doc = parse(SIMPLE)
    g = exvisitGraph.from_doc(doc)
    doc2 = g.to_doc()
    # CRDT flush produces canonically sorted tree
    assert serialize(doc2) == serialize(doc)


def test_query_slice():
    doc = parse(SIMPLE)
    out = query(doc, "Scene", hops=1)
    # must contain Scene and its neighbor DicomLoader
    assert "Scene" in out
    assert "DicomLoader" in out
    # irrelevant wrapper pruning: Core remains because Scene lives there
    assert "Core" in out


def test_query_missing():
    doc = parse(SIMPLE)
    try:
        query(doc, "Nope")
    except KeyError:
        return
    raise AssertionError("expected KeyError")


def test_myslicer_parses():
    p = Path(__file__).resolve().parent.parent / "examples" / "myslicer.exv"
    src = p.read_text(encoding="utf-8")
    doc = parse(src)
    nodes = doc.all_nodes()
    assert len(nodes) >= 20
    # roundtrip stability
    s1 = serialize(doc)
    assert serialize(parse(s1)) == s1


def test_scaffold_emits_line_ranges():
    from tempfile import TemporaryDirectory
    from exvisit.scaffold import generate as scaffold_generate

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        pkg = root / "sample"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "worker.py").write_text("def do_work():\n    return 'ok'\n", encoding="utf-8")

        doc = parse(scaffold_generate(str(root), root_name="SampleApp"))
        worker = doc.find_node("Worker")
        assert worker is not None
        assert worker.line_range == (1, 2)


def test_scaffold_sanitizes_non_identifier_filenames():
    from tempfile import TemporaryDirectory
    from exvisit.scaffold import generate as scaffold_generate

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        pkg = root / "sample"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / ".util.py").write_text("VALUE = 1\n", encoding="utf-8")
        (pkg / "~util.py").write_text("VALUE = 2\n", encoding="utf-8")
        (pkg / "0001_initial.py").write_text("VALUE = 3\n", encoding="utf-8")

        doc = parse(scaffold_generate(str(root), root_name="SampleApp"))
        names = {node.name for node in doc.all_nodes()}

        assert "Util" in names
        assert "Util2" in names
        assert "N0001Initial" in names


def test_scaffold_skips_virtualenv_like_dirs_and_exvisit_temp_dirs():
    from tempfile import TemporaryDirectory
    from exvisit.scaffold import generate as scaffold_generate

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        pkg = root / "app"
        pkg.mkdir()
        (pkg / "main.py").write_text("from app.worker import run\n", encoding="utf-8")
        (pkg / "worker.py").write_text("def run():\n    return 1\n", encoding="utf-8")

        smoke = root / ".exvisit-smoke"
        smoke.mkdir()
        (smoke / "pyvenv.cfg").write_text("home = fake\n", encoding="utf-8")
        nested = smoke / "Lib" / "site-packages" / "pip" / "_internal" / "commands"
        nested.mkdir(parents=True)
        (nested / "debug.py").write_text("class DebugCommand: pass\n", encoding="utf-8")

        text = scaffold_generate(str(root), root_name="App")

        assert ".exvisit-smoke" not in text
        assert "pip/_internal/commands/debug.py" not in text
        assert "app/main.py" in text


def test_scaffold_respects_dot_exvisitignore():
    from tempfile import TemporaryDirectory
    from exvisit.scaffold import generate as scaffold_generate

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / ".exvisitignore").write_text("generated\n", encoding="utf-8")

        app = root / "app"
        app.mkdir()
        (app / "main.py").write_text("VALUE = 1\n", encoding="utf-8")

        generated = root / "generated"
        generated.mkdir()
        (generated / "noise.py").write_text("VALUE = 2\n", encoding="utf-8")

        text = scaffold_generate(str(root), root_name="App")

        assert "app/main.py" in text
        assert "generated/noise.py" not in text


def test_infer_repo_root_defaults_to_exv_parent_when_repo_omitted():
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        exv = root / "sample.exv"
        exv.write_text("@L0 App [0,0,100,100] {\n}\n", encoding="utf-8")

        doc = parse(exv.read_text(encoding="utf-8"))
        assert _infer_repo_root(doc, exv, None) == str(root.resolve())


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)

