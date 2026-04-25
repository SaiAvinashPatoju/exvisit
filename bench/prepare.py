"""Repo preparation: clone, checkout, generate .exv maps for each SWE-bench case."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import stat
from pathlib import Path
from typing import Optional

from .dataset import SWEBenchInstance


def _rmtree_readonly(path: Path):
    """Remove a directory tree, handling Windows read-only files."""
    def _on_error(func, fpath, exc_info):
        os.chmod(fpath, stat.S_IWRITE)
        func(fpath)
    shutil.rmtree(path, onerror=_on_error)


def ensure_repo_clone(repo: str, cache_dir: Path) -> Path:
    """Clone (or reuse) a full clone of the repo."""
    org, name = repo.split("/")
    repo_dir = cache_dir / "repos" / name
    repo_dir = repo_dir.resolve()
    if repo_dir.exists() and (repo_dir / ".git").exists():
        return repo_dir
    if repo_dir.exists():
        _rmtree_readonly(repo_dir)
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{repo}.git"
    result = subprocess.run(
        ["git", "clone", url, str(repo_dir)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git clone failed: {result.stderr}")
    return repo_dir


def checkout_worktree(
    repo_dir: Path,
    commit: str,
    workspace: Path,
) -> Path:
    """Create a working copy at a specific commit via local clone."""
    workspace = workspace.resolve()
    repo_dir = repo_dir.resolve()
    if workspace.exists() and (workspace / ".git").exists():
        # Reuse existing clone: reset to target commit
        subprocess.run(
            ["git", "checkout", "-f", commit],
            cwd=str(workspace),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "clean", "-fdx"],
            cwd=str(workspace),
            check=True,
            capture_output=True,
        )
        return workspace
    if workspace.exists():
        _rmtree_readonly(workspace)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    # Use --shared to avoid copying objects (fast local clone)
    subprocess.run(
        ["git", "clone", "--shared", str(repo_dir), str(workspace)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "-f", commit],
        cwd=str(workspace),
        check=True,
        capture_output=True,
    )
    return workspace


def generate_exv_map(
    workspace: Path,
    exv_out: Optional[Path] = None,
) -> Path:
    """Run `exv init` to generate a .exv structural map."""
    if exv_out is None:
        exv_out = workspace / "project.exv"
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "exvisit", "init",
         "--repo", str(workspace), "--out", str(exv_out)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"exv init failed: {result.stderr}")
    return exv_out


def run_exv_blast(
    exv_path: Path,
    issue_text: str,
    max_files: int = 5,
    format: str = "json",
) -> dict:
    """Run `exv blast` and return the result."""
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as f:
        f.write(issue_text)
        issue_file = f.name
    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        result = subprocess.run(
            [sys.executable, "-m", "exvisit", "blast", str(exv_path),
             "--issue-file", issue_file, "--format", format],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
    finally:
        os.unlink(issue_file)
    if result.returncode != 0:
        raise RuntimeError(f"exv blast failed: {result.stderr}")
    if format == "json":
        return json.loads(result.stdout)
    return {"markdown": result.stdout}


def run_exv_blast_md(
    exv_path: Path,
    issue_text: str,
    max_files: int = 5,
) -> str:
    """Run `exv blast` and return markdown output."""
    result = run_exv_blast(exv_path, issue_text, max_files=max_files, format="md")
    return result.get("markdown", str(result))


def prepare_case(
    instance: SWEBenchInstance,
    cache_dir: Path,
    workspace_root: Path,
) -> dict:
    """Full preparation for a single SWE-bench case.

    Returns dict with: workspace, exv_path, blast_bundle, blast_md
    """
    case_id = instance.instance_id.replace("/", "__")
    workspace = workspace_root / case_id

    # Clone and checkout
    repo_dir = ensure_repo_clone(instance.repo, cache_dir)
    checkout_worktree(repo_dir, instance.base_commit, workspace)

    # Generate .exv map
    exv_path = workspace / "project.exv"
    generate_exv_map(workspace, exv_path)

    # Run blast
    blast_json = run_exv_blast(exv_path, instance.problem_statement, format="json")
    blast_md = run_exv_blast(exv_path, instance.problem_statement, format="md")
    blast_md_text = blast_md.get("markdown", str(blast_md))

    return {
        "workspace": workspace,
        "exv_path": exv_path,
        "blast_bundle": blast_json,
        "blast_md": blast_md_text,
    }


if __name__ == "__main__":
    from .dataset import load_django_instances
    cases = load_django_instances(limit=1)
    cache = Path("bench/.cache")
    ws = Path("bench/.cache/workspaces")
    for c in cases:
        result = prepare_case(c, cache, ws)
        print(f"Prepared {c.instance_id}: {result['exv_path']}")
        print(f"Blast files: {list(result['blast_bundle'].get('files', {}).keys())}")
