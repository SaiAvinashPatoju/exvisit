"""Unit tests for the SWE-bench Lite exvisit harness."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exvisit.scaffold import generate as scaffold_generate
from bench.swebench_lite_harness import (
    BenchmarkCase,
    PricingConfig,
    _FETCHED_REPOS,
    exvisit_strategy,
    control_strategy,
    ensure_checkout,
    extract_usage_summary,
    extract_issue_terms,
    extract_oracle_files_from_patch,
    run_benchmark,
)


def test_extract_oracle_files_from_patch():
    patch = """diff --git a/pkg/foo.py b/pkg/foo.py
index 123..456 100644
--- a/pkg/foo.py
+++ b/pkg/foo.py
@@ -1,3 +1,4 @@
+
diff --git a/pkg/bar.py b/pkg/bar.py
--- a/pkg/bar.py
+++ b/pkg/bar.py
"""
    assert extract_oracle_files_from_patch(patch) == ["pkg/foo.py", "pkg/bar.py"]


def test_extract_issue_terms_backticks_and_symbols():
    code_terms, keywords = extract_issue_terms("Fix `Session.resolve_redirects` when builtin_str mangles GET in sessions.py")
    assert "Session.resolve_redirects" in code_terms
    assert "sessions" in keywords
    assert "builtin_str" in code_terms


def test_exvisit_strategy_hits_oracle_with_fewer_tokens_than_control():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        pkg = root / "sample"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "sessions.py").write_text(
            """
class Session:
    def resolve_redirects(self):
        value = 'redirect'
        return value
""".strip() + "\n",
            encoding="utf-8",
        )
        (pkg / "adapters.py").write_text(
            """
from sample.sessions import Session

def build():
    return Session()
""".strip() + "\n",
            encoding="utf-8",
        )
        (root / "README.py").write_text("def noise():\n    return 'noise'\n", encoding="utf-8")

        exvisit_path = root / "sample.exv"
        exvisit_path.write_text(scaffold_generate(str(root), root_name="SampleApp"), encoding="utf-8")

        case = BenchmarkCase(
            case_id="sample-1",
            repo="sample/repo",
            repo_path=str(root),
            base_commit=None,
            issue_text="Fix `Session.resolve_redirects` behavior in sessions.py when redirects are followed",
            oracle_files=["sample/sessions.py"],
            exvisit_path=str(exvisit_path),
        )

        control = control_strategy(case)
        exvisit = exvisit_strategy(case)

        assert control.oracle_hit
        assert exvisit.oracle_hit
        assert exvisit.oracle_hit_at_1
        assert exvisit.selected_files[0] == "sample/sessions.py"
        assert len(exvisit.selected_files) <= len(control.selected_files)
        assert exvisit.input_tokens < control.input_tokens


def test_extract_usage_summary_computes_cost_without_reasoning_double_count():
    payload = {
        "steps": [
            {
                "usage_metadata": {
                    "input_tokens": 1000,
                    "cache_creation_input_tokens": 200,
                    "cache_read_input_tokens": 300,
                    "output_tokens": 150,
                    "output_tokens_details": {"reasoning_tokens": 80},
                }
            }
        ]
    }
    pricing = PricingConfig(
        input_base_per_1m=10.0,
        cache_write_per_1m=5.0,
        cache_read_per_1m=1.0,
        output_per_1m=20.0,
    )

    usage = extract_usage_summary(payload, pricing=pricing)

    assert usage is not None
    assert usage.prompt_base_tokens == 500
    assert usage.cache_write_tokens == 200
    assert usage.cache_read_tokens == 300
    assert usage.completion_tokens == 150
    assert usage.reasoning_tokens == 80
    expected = ((500 * 10.0) + (200 * 5.0) + (300 * 1.0) + (150 * 20.0)) / 1_000_000.0
    assert usage.cost_to_resolve_usd == expected


def test_run_benchmark_resumes_existing_output():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        pkg = root / "sample"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "sessions.py").write_text(
            """
class Session:
    def resolve_redirects(self):
        value = 'redirect'
        return value
""".strip() + "\n",
            encoding="utf-8",
        )
        (pkg / "adapters.py").write_text(
            """
from sample.sessions import Session

def build():
    return Session()
""".strip() + "\n",
            encoding="utf-8",
        )
        exvisit_path = root / "sample.exv"
        exvisit_path.write_text(scaffold_generate(str(root), root_name="SampleApp"), encoding="utf-8")

        case = BenchmarkCase(
            case_id="sample-1",
            repo="sample/repo",
            repo_path=str(root),
            base_commit=None,
            issue_text="Fix `Session.resolve_redirects` behavior in sessions.py when redirects are followed",
            oracle_files=["sample/sessions.py"],
            exvisit_path=str(exvisit_path),
        )
        out = root / "results.json"

        first = run_benchmark([case], None, None, None, output_path=out, resume=True)
        second = run_benchmark([case], None, None, None, output_path=out, resume=True)
        persisted = json.loads(out.read_text(encoding="utf-8"))

        assert len(first["results"]) == 1
        assert len(second["results"]) == 1
        assert len(persisted["results"]) == 1
        assert persisted["results"][0]["case_id"] == "sample-1"


def test_run_benchmark_uses_isolated_strategy_workspaces():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        pkg = root / "sample"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "sessions.py").write_text(
            """
class Session:
    def resolve_redirects(self):
        value = 'redirect'
        return value
""".strip() + "\n",
            encoding="utf-8",
        )
        exvisit_path = root / "sample.exv"
        exvisit_path.write_text(scaffold_generate(str(root), root_name="SampleApp"), encoding="utf-8")

        runner = root / "runner.py"
        runner.write_text(
            """
from __future__ import annotations

import json
import sys
from pathlib import Path

repo_path = Path(sys.argv[1])
assert (repo_path / 'sample' / 'sessions.py').exists()
marker = repo_path / 'marker.txt'
marker.write_text(repo_path.name, encoding='utf-8')
print(json.dumps({'pass_at_1': True}))
""".strip() + "\n",
            encoding="utf-8",
        )

        case = BenchmarkCase(
            case_id="sample-1",
            repo="sample/repo",
            repo_path=str(root),
            base_commit=None,
            issue_text="Fix `Session.resolve_redirects` behavior in sessions.py when redirects are followed",
            oracle_files=["sample/sessions.py"],
            exvisit_path=str(exvisit_path),
        )

        runner_cmd = f'"{sys.executable}" "{runner}" "{{repo_path}}"'
        workspace_root = root / "workspaces"

        payload = run_benchmark(
            [case],
            control_runner_cmd=runner_cmd,
            exvisit_runner_cmd=runner_cmd,
            input_cost_per_1m=None,
            resume=False,
            workspace_root=workspace_root,
        )

        control_workspace = workspace_root / "sample__repo" / "working" / "sample-1" / "control"
        exvisit_workspace = workspace_root / "sample__repo" / "working" / "sample-1" / "exvisit"

        assert payload["results"][0]["control"]["runner_exit_code"] == 0
        assert payload["results"][0]["exvisit"]["runner_exit_code"] == 0
        assert control_workspace != exvisit_workspace
        assert (control_workspace / "marker.txt").read_text(encoding="utf-8") == "control"
        assert (exvisit_workspace / "marker.txt").read_text(encoding="utf-8") == "exvisit"


def test_ensure_checkout_fetches_each_repo_once(monkeypatch, tmp_path):
    calls = []

    def fake_git(command, cwd=None):
        calls.append((tuple(command), cwd))

    monkeypatch.setattr("bench.swebench_lite_harness.git", fake_git)
    _FETCHED_REPOS.clear()
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    ensure_checkout(repo_path, "abc123")
    ensure_checkout(repo_path, "def456")

    fetch_calls = [call for call in calls if call[0][:1] == ("fetch",)]
    checkout_calls = [call for call in calls if call[0][:1] == ("checkout",)]
    clean_calls = [call for call in calls if call[0][:1] == ("clean",)]

    assert len(fetch_calls) == 1
    assert len(checkout_calls) == 2
    assert len(clean_calls) == 2


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
