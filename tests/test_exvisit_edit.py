"""Tests for the AST-located exvisit_edit primitive."""
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exvisit.edit_tool import apply_locator_edit, resolve_locator_span


def test_resolve_locator_span_for_method():
    source = """
class Session:
    def resolve_redirects(self):
        value = 'redirect'
        return value
""".strip() + "\n"

    span = resolve_locator_span(source, "Session.resolve_redirects")

    assert span.start_line == 2
    assert span.end_line == 4
    assert span.start_byte < span.end_byte


def test_apply_locator_edit_replaces_within_scope():
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "sessions.py"
        path.write_text(
            """
class Session:
    def resolve_redirects(self):
        value = 'redirect'
        return value


def helper():
    value = 'redirect'
    return value
""".strip() + "\n",
            encoding="utf-8",
        )

        result = apply_locator_edit(
            path,
            "Session.resolve_redirects",
            "value = 'redirect'",
            "value = 'redirect-fixed'",
        )
        updated = path.read_text(encoding="utf-8")

        assert result.replaced_count == 1
        assert "value = 'redirect-fixed'" in updated
        assert "def helper():\n    value = 'redirect'" in updated


def test_apply_locator_edit_rejects_ambiguous_match_in_scope():
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "sessions.py"
        path.write_text(
            """
class Session:
    def resolve_redirects(self):
        value = 'redirect'
        mirror = value
        return value
""".strip() + "\n",
            encoding="utf-8",
        )

        try:
            apply_locator_edit(path, "Session.resolve_redirects", "value", "token")
        except ValueError as exc:
            assert "multiple times" in str(exc)
            return
        raise AssertionError("expected ValueError for ambiguous locator-local match")


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
