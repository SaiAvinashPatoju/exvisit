"""SWE-bench Lite dataset loader — loads directly from HuggingFace without swebench.harness.

Avoids importing swebench.harness which requires the Unix-only `resource` module.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from datasets import load_dataset


DATASET_NAME = "princeton-nlp/SWE-bench_Lite"
SPLIT = "test"


@dataclass
class SWEBenchInstance:
    instance_id: str
    repo: str               # e.g. "django/django"
    base_commit: str
    patch: str               # gold patch
    test_patch: str
    problem_statement: str
    hints_text: str
    created_at: str
    version: str
    environment_setup_commit: str
    FAIL_TO_PASS: list[str] = field(default_factory=list)
    PASS_TO_PASS: list[str] = field(default_factory=list)

    @property
    def repo_name(self) -> str:
        return self.repo.split("/")[-1]

    @property
    def org_name(self) -> str:
        return self.repo.split("/")[0]

    @property
    def oracle_files(self) -> list[str]:
        """Extract files modified in the gold patch."""
        files = []
        for line in self.patch.splitlines():
            if line.startswith("diff --git"):
                parts = line.split()
                if len(parts) >= 4:
                    path = parts[2].removeprefix("a/")
                    files.append(path)
            elif line.startswith("--- a/"):
                path = line.removeprefix("--- a/")
                if path != "/dev/null":
                    files.append(path)
        return sorted(set(files))


def load_swebench_lite(
    repo_filter: Optional[str] = None,
    limit: Optional[int] = None,
    cache_dir: Optional[str] = None,
) -> list[SWEBenchInstance]:
    """Load SWE-bench Lite instances.

    Args:
        repo_filter: Filter to a specific repo (e.g. "django/django").
        limit: Max number of instances to return.
        cache_dir: HuggingFace cache directory.
    """
    ds = load_dataset(DATASET_NAME, split=SPLIT, cache_dir=cache_dir)
    instances = []
    for row in ds:
        if repo_filter and row["repo"] != repo_filter:
            continue
        fail_to_pass = row.get("FAIL_TO_PASS", "[]")
        pass_to_pass = row.get("PASS_TO_PASS", "[]")
        if isinstance(fail_to_pass, str):
            fail_to_pass = json.loads(fail_to_pass)
        if isinstance(pass_to_pass, str):
            pass_to_pass = json.loads(pass_to_pass)
        inst = SWEBenchInstance(
            instance_id=row["instance_id"],
            repo=row["repo"],
            base_commit=row["base_commit"],
            patch=row["patch"],
            test_patch=row.get("test_patch", ""),
            problem_statement=row["problem_statement"],
            hints_text=row.get("hints_text", ""),
            created_at=row.get("created_at", ""),
            version=row.get("version", ""),
            environment_setup_commit=row.get("environment_setup_commit", ""),
            FAIL_TO_PASS=fail_to_pass,
            PASS_TO_PASS=pass_to_pass,
        )
        instances.append(inst)
        if limit and len(instances) >= limit:
            break
    return instances


def load_django_instances(limit: Optional[int] = None) -> list[SWEBenchInstance]:
    """Convenience: load only django/django cases."""
    return load_swebench_lite(repo_filter="django/django", limit=limit)


if __name__ == "__main__":
    cases = load_django_instances(limit=5)
    for c in cases:
        print(f"{c.instance_id}  oracle_files={c.oracle_files}")
