"""Tests for mini-SWE-agent sandbox materialization."""
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.mini_swe_agent import exvisit_MOUNT_PATH, materialize_sandbox
from bench.swebench_lite_harness import BenchmarkCase


def test_materialize_sandbox_writes_expected_files():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = root / "repo"
        repo.mkdir()
        exvisit_path = root / "sample.exv"
        exvisit_path.write_text("@L0 App [0,0,100,100] {\n}\n", encoding="utf-8")

        case = BenchmarkCase(
            case_id="case-1",
            repo="sample/repo",
            repo_path=str(repo),
            base_commit="abc123",
            issue_text="Fix Session.resolve_redirects",
            oracle_files=[],
            exvisit_path=str(exvisit_path),
        )
        sandbox = materialize_sandbox(case, root / "sandbox")

        dockerfile = sandbox.dockerfile_path.read_text(encoding="utf-8")
        entrypoint = sandbox.entrypoint_path.read_text(encoding="utf-8")
        claude = sandbox.claude_path.read_text(encoding="utf-8")
        run_script = sandbox.run_script_path.read_text(encoding="utf-8")

        assert "ghcr.io/epoch-research/mini-swe-agent:latest" in dockerfile
        assert "rm -f \"$SANITIZED_BIN/grep\" \"$SANITIZED_BIN/find\" \"$SANITIZED_BIN/rg\"" in entrypoint
        assert exvisit_MOUNT_PATH in claude
        assert "Use `exvisit-query`, `exvisit-blast`, and `exvisit-anchor`" in claude
        assert f'"{exvisit_path.resolve().as_posix()}:{exvisit_MOUNT_PATH}:ro"' in run_script


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
