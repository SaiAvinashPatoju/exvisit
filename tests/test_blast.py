"""Tests for the exvisit blast-radius bundler."""
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exvisit import parse
from exvisit.anchor import build_anchor_report, render_anchor_text
from exvisit.blast import build_blast_bundle, render_blast_markdown
from exvisit.scaffold import generate as scaffold_generate


def test_blast_resolves_anchor_and_bundle():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        pkg = root / "sample"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "sessions.py").write_text(
            "class Session:\n    def resolve_redirects(self):\n        return 'redirect'\n",
            encoding="utf-8",
        )
        (pkg / "adapters.py").write_text(
            "from sample.sessions import Session\n\ndef build():\n    return Session()\n",
            encoding="utf-8",
        )

        doc = parse(scaffold_generate(str(root), root_name="SampleApp"))
        bundle = build_blast_bundle(
            doc,
            str(root),
            "Fix `Session.resolve_redirects` behavior in requests/sessions.py when redirects are followed",
            preset_name="test-fix",
        )

        assert bundle.anchor.endswith("Sessions")
        assert bundle.selected_files[0] == "sample/sessions.py"
        assert any(reason.phase == "anchor" for reason in bundle.selection_reasons)
        assert bundle.snippets[0].file_path == "sample/sessions.py"


def test_blast_markdown_contains_selection_reasons():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        pkg = root / "sample"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "worker.py").write_text(
            "def do_work():\n    return 'ok'\n",
            encoding="utf-8",
        )

        doc = parse(scaffold_generate(str(root), root_name="SampleApp"))
        trigger = "Crash in worker.py during do_work execution"
        bundle = build_blast_bundle(doc, str(root), trigger, preset_name="default")
        md = render_blast_markdown(bundle, trigger)

        assert "# exvisit Blast Bundle" in md
        assert "## Selection Reasons" in md
        assert "worker.py" in md


def test_blast_uses_exvisit_line_ranges_for_snippets():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        pkg = root / "sample"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "worker.py").write_text(
            "def noise():\n    return 'n'\n\ndef do_work():\n    return 'ok'\n\ndef tail():\n    return 'tail'\n",
            encoding="utf-8",
        )
        exvisit_src = """
@L0 SampleApp [0,0,100,100] {
  @L1 Sample [1,1,98,98] "sample/*.py" {
    Worker [1,1,10,4] sample/worker.py lines=4..5
  }
}
"""

        bundle = build_blast_bundle(parse(exvisit_src), str(root), "Crash in worker.py during do_work execution", preset_name="default")

        assert bundle.snippets[0].label.startswith("worker.py:4-5")
        assert "4: def do_work():" in bundle.snippets[0].code
        assert "1: def noise():" not in bundle.snippets[0].code


def test_anchor_resolves_traceback_ground_zero():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        pkg = root / "sample"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "sessions.py").write_text(
            "class Session:\n    def resolve_redirects(self):\n        return 'redirect'\n",
            encoding="utf-8",
        )
        (pkg / "adapters.py").write_text(
            "from sample.sessions import Session\n\ndef build():\n    return Session()\n",
            encoding="utf-8",
        )

        doc = parse(scaffold_generate(str(root), root_name="SampleApp"))
        trace = f'Traceback (most recent call last):\n  File "{(pkg / "sessions.py").as_posix()}", line 2, in resolve_redirects\n    return \'redirect\'\nRuntimeError: boom\n'
        report = build_anchor_report(doc, str(root), trace)
        text = render_anchor_text(report)

        assert report.hits[0].role == "ground_zero"
        assert report.hits[0].fqn.endswith("Sessions")
        assert report.hits[0].file_path == "sample/sessions.py"
        assert report.hits[0].line == 2
        assert "[ground_zero]" in text


def test_blast_handles_empty_file_line_ranges():
        with TemporaryDirectory() as tmp:
                root = Path(tmp)
                pkg = root / "sample"
                pkg.mkdir()
                (pkg / "__init__.py").write_text("", encoding="utf-8")
                (pkg / "empty.py").write_text("", encoding="utf-8")
                exvisit_src = """
@L0 SampleApp [0,0,100,100] {
    @L1 Sample [1,1,98,98] "sample/*.py" {
        Empty [1,1,10,4] sample/empty.py lines=1..1
    }
}
"""

                bundle = build_blast_bundle(parse(exvisit_src), str(root), "Crash in empty.py", preset_name="default")

                assert bundle.snippets[0].label == "empty.py:0-0 (empty-file)"
                assert bundle.snippets[0].code == ""


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for test in tests:
        try:
            test()
            print(f"  ok  {test.__name__}")
            passed += 1
        except Exception as exc:
            print(f"  FAIL {test.__name__}: {exc}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(0 if failed == 0 else 1)

