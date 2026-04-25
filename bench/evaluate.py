"""Pass@1 evaluation — apply patch and run tests in Docker.

Evaluates generated patches by:
1. Applying the patch to a fresh repo checkout
2. Running the SWE-bench test suite in Docker
3. Comparing FAIL_TO_PASS tests
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from .dataset import SWEBenchInstance
from .metrics import CaseMetrics


EVAL_DOCKERFILE = """\
FROM python:3.{pyver}-slim

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git gcc g++ make libffi-dev libssl-dev && \\
    rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
COPY repo/ /workspace/
COPY patch.diff /workspace/patch.diff
COPY test_patch.diff /workspace/test_patch.diff
COPY run_eval.sh /workspace/run_eval.sh

RUN chmod +x /workspace/run_eval.sh
"""

EVAL_SCRIPT = """\
#!/bin/bash
set -e

cd /workspace

# Install repo
pip install -e . 2>/dev/null || pip install . 2>/dev/null || true

# Apply the generated patch
if [ -s patch.diff ]; then
    git apply --allow-empty patch.diff 2>/dev/null || {
        echo "PATCH_APPLY_FAILED"
        exit 1
    }
    echo "PATCH_APPLIED"
else
    echo "NO_PATCH"
    exit 1
fi

# Apply the test patch (SWE-bench gold tests)
if [ -s test_patch.diff ]; then
    git apply --allow-empty test_patch.diff 2>/dev/null || true
fi

# Run the failing tests
RESULT=0
{test_commands}

if [ $RESULT -eq 0 ]; then
    echo "ALL_TESTS_PASSED"
else
    echo "SOME_TESTS_FAILED"
    exit 1
fi
"""


def build_test_commands(instance: SWEBenchInstance) -> str:
    """Build shell test commands from FAIL_TO_PASS list."""
    if not instance.FAIL_TO_PASS:
        return 'echo "NO_TESTS_SPECIFIED"'

    lines = []
    for test in instance.FAIL_TO_PASS:
        # Django test format: "test_module.TestClass.test_method"
        # or pytest format: "path/to/test.py::TestClass::test_method"
        if "::" in test:
            # pytest format
            lines.append(f'python -m pytest -xvs "{test}" || RESULT=1')
        else:
            # Django test runner format
            # Convert dots to Django test runner format
            parts = test.rsplit(".", 1)
            if len(parts) == 2:
                module, method = parts
                lines.append(
                    f'python -m django test {module} --settings=tests.test_settings '
                    f'-v 2 2>/dev/null || '
                    f'python tests/runtests.py {test} -v 2 || RESULT=1'
                )
            else:
                lines.append(f'python tests/runtests.py {test} -v 2 || RESULT=1')

    return "\n".join(lines)


def evaluate_patch_docker(
    instance: SWEBenchInstance,
    workspace: Path,
    patch_path: Optional[Path] = None,
    timeout: int = 300,
) -> dict:
    """Evaluate a patch by running tests in Docker.

    Returns dict with: patch_applied, tests_passed, output
    """
    result = {
        "patch_applied": False,
        "tests_passed": False,
        "output": "",
        "error": None,
    }

    if patch_path is None:
        patch_path = workspace / "generated.patch"
    if not patch_path.exists() or patch_path.stat().st_size == 0:
        result["error"] = "No patch file found"
        return result

    # Build eval context in temp dir
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # Copy repo
        repo_dir = tmp / "repo"
        # Use git archive to avoid copying .git
        subprocess.run(
            ["git", "archive", "--format=tar", "HEAD"],
            cwd=str(workspace),
            stdout=open(str(tmp / "repo.tar"), "wb"),
            check=True,
        )
        repo_dir.mkdir()
        subprocess.run(
            ["tar", "xf", str(tmp / "repo.tar"), "-C", str(repo_dir)],
            check=True,
        )

        # Copy patch
        import shutil
        shutil.copy2(str(patch_path), str(tmp / "patch.diff"))

        # Write test patch
        test_patch = tmp / "test_patch.diff"
        test_patch.write_text(instance.test_patch or "", encoding="utf-8")

        # Determine Python version
        pyver = "11"  # default
        version = instance.version
        if version and "." in version:
            major, minor = version.split(".")[:2]
            try:
                if int(major) >= 4:
                    pyver = "11"
                elif int(major) == 3 and int(minor) >= 2:
                    pyver = "9"
                else:
                    pyver = "8"
            except ValueError:
                pyver = "11"

        # Write Dockerfile
        dockerfile = tmp / "Dockerfile"
        dockerfile.write_text(
            EVAL_DOCKERFILE.format(pyver=pyver),
            encoding="utf-8",
        )

        # Write eval script
        test_cmds = build_test_commands(instance)
        eval_script = tmp / "run_eval.sh"
        eval_script.write_text(
            EVAL_SCRIPT.format(test_commands=test_cmds),
            encoding="utf-8",
        )

        # Build Docker image
        case_tag = instance.instance_id.replace("/", "-").replace("__", "-").lower()
        image_name = f"exv-bench-eval:{case_tag}"

        try:
            build_result = subprocess.run(
                ["docker", "build", "-t", image_name, "."],
                cwd=str(tmp),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if build_result.returncode != 0:
                result["error"] = f"Docker build failed: {build_result.stderr[-500:]}"
                return result

            # Run evaluation
            run_result = subprocess.run(
                ["docker", "run", "--rm",
                 "--memory=2g", "--cpus=2",
                 image_name,
                 "bash", "/workspace/run_eval.sh"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            result["output"] = run_result.stdout + "\n" + run_result.stderr

            if "PATCH_APPLIED" in run_result.stdout:
                result["patch_applied"] = True
            if "ALL_TESTS_PASSED" in run_result.stdout:
                result["tests_passed"] = True

        except subprocess.TimeoutExpired:
            result["error"] = "Evaluation timed out"
        except Exception as e:
            result["error"] = str(e)
        finally:
            # Cleanup Docker image
            subprocess.run(
                ["docker", "rmi", "-f", image_name],
                capture_output=True,
            )

    return result


def evaluate_case(
    instance: SWEBenchInstance,
    case_metrics: CaseMetrics,
    workspace: Path,
    timeout: int = 300,
) -> CaseMetrics:
    """Run pass@1 evaluation for a case and update metrics."""
    t0 = time.time()

    if not case_metrics.patch_generated:
        case_metrics.eval_time_s = time.time() - t0
        return case_metrics

    eval_result = evaluate_patch_docker(instance, workspace, timeout=timeout)
    case_metrics.patch_applied = eval_result["patch_applied"]
    case_metrics.tests_passed = eval_result["tests_passed"]
    case_metrics.pass_at_1 = eval_result["tests_passed"]
    case_metrics.eval_time_s = time.time() - t0

    if eval_result.get("error"):
        if case_metrics.error:
            case_metrics.error += f"; Eval: {eval_result['error']}"
        else:
            case_metrics.error = f"Eval: {eval_result['error']}"

    return case_metrics
