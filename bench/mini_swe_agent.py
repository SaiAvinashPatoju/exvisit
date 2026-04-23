"""Materialize a constrained mini-SWE-agent sandbox around an exvisit benchmark case."""
from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from bench.swebench_lite_harness import BenchmarkCase, load_manifest


DEFAULT_BASE_IMAGE = "ghcr.io/epoch-research/mini-swe-agent:latest"
exvisit_MOUNT_PATH = "/opt/exvisit/repo.exv"
INPUT_REPO_MOUNT = "/opt/exvisit/input-repo"
WORKSPACE_REPO_PATH = "/workspace/repo"

ENTRYPOINT_SH = """#!/bin/sh
set -eu

SANITIZED_BIN=/opt/exvisit/sanitized-bin
INPUT_REPO=${INPUT_REPO:-/opt/exvisit/input-repo}
WORKSPACE_REPO=${WORKSPACE_REPO:-/workspace/repo}

mkdir -p "$SANITIZED_BIN" "$WORKSPACE_REPO"

for tool in sh bash env cat sed awk git python python3 pytest ls cp mv rm mkdir touch pwd head tail wc sort xargs; do
  if command -v "$tool" >/dev/null 2>&1; then
    ln -sf "$(command -v "$tool")" "$SANITIZED_BIN/$tool"
  fi
done

for tool in exvisit-query exvisit-blast exvisit-anchor exvisit-edit; do
  ln -sf "/opt/exvisit/bin/$tool" "$SANITIZED_BIN/$tool"
done

rm -f "$SANITIZED_BIN/grep" "$SANITIZED_BIN/find" "$SANITIZED_BIN/rg" || true

export PATH="$SANITIZED_BIN"
export PYTHONPATH="/opt/exvisit/python"
export exvisit_FILE=${exvisit_FILE:-/opt/exvisit/repo.exv}

if [ -d "$INPUT_REPO" ]; then
  cp -a "$INPUT_REPO"/. "$WORKSPACE_REPO"/
fi

cd "$WORKSPACE_REPO"

if [ "$#" -eq 0 ]; then
  exec sh
fi
exec "$@"
"""


def _exvisit_command_wrapper(command: str, extra_args: str) -> str:
    return f"#!/bin/sh\nset -eu\nexec python -m exvisit {command} \"$exvisit_FILE\" {extra_args} \"$@\"\n"


exvisit_QUERY_SH = _exvisit_command_wrapper("query", "")
exvisit_BLAST_SH = _exvisit_command_wrapper("blast", f"--repo {WORKSPACE_REPO_PATH}")
exvisit_ANCHOR_SH = _exvisit_command_wrapper("anchor", f"--repo {WORKSPACE_REPO_PATH}")
exvisit_EDIT_SH = "#!/bin/sh\nset -eu\nexec python -m exvisit.edit_tool \"$@\"\n"


DOCKERFILE_TEMPLATE = """ARG BASE_IMAGE={base_image}
FROM ${{BASE_IMAGE}}

WORKDIR /opt/exvisit

COPY python /opt/exvisit/python
COPY bin /opt/exvisit/bin
COPY CLAUDE.md /opt/exvisit/CLAUDE.md
COPY entrypoint.sh /opt/exvisit/entrypoint.sh

RUN chmod +x /opt/exvisit/entrypoint.sh /opt/exvisit/bin/*

ENTRYPOINT ["/opt/exvisit/entrypoint.sh"]
CMD ["sh"]
"""


CLAUDE_MD_TEMPLATE = """# exvisit Mini-SWE Sandbox

You are operating inside a constrained debugging sandbox.

Rules:

1. Traditional repo-wide search tools are intentionally unavailable on PATH. Do not rely on `grep`, `find`, or `rg`.
2. Use `exvisit-query`, `exvisit-blast`, and `exvisit-anchor` to navigate the repository spatially.
3. Use `exvisit-edit` for code changes. It requires an AST locator and only edits a single unambiguous match inside that locator's span.
4. Treat `{exvisit_mount_path}` as the canonical exvisit map for this repository.
5. The writable repository lives at `{workspace_repo_path}` and is copy-on-start from the read-only input mount.

Case:

- Case ID: `{case_id}`
- Repo: `{repo}`
- Base commit: `{base_commit}`

Issue text:

```text
{issue_text}
```
"""


RUN_SCRIPT_TEMPLATE = """#!/bin/sh
set -eu

docker build -t exvisit-mini-swe:{case_id} .
docker run --rm -it --memory=18g \\
  -v "{exvisit_host_path}:{exvisit_mount_path}:ro" \\
  -v "{repo_host_path}:{input_repo_mount}:ro" \\
  exvisit-mini-swe:{case_id}
"""


@dataclass
class MiniSweSandbox:
    case_id: str
    sandbox_dir: Path
    dockerfile_path: Path
    entrypoint_path: Path
    claude_path: Path
    run_script_path: Path


def _copy_exvisit_package(target_root: Path) -> None:
    source = Path(__file__).resolve().parent.parent / "exvisit"
    destination = target_root / "python" / "exvisit"
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)


def materialize_sandbox(case: BenchmarkCase, out_dir: Path, base_image: str = DEFAULT_BASE_IMAGE) -> MiniSweSandbox:
    out_dir.mkdir(parents=True, exist_ok=True)
    _copy_exvisit_package(out_dir)

    bin_dir = out_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    entrypoint_path = out_dir / "entrypoint.sh"
    claude_path = out_dir / "CLAUDE.md"
    dockerfile_path = out_dir / "Dockerfile"
    run_script_path = out_dir / "run_docker.sh"

    (bin_dir / "exvisit-query").write_text(exvisit_QUERY_SH, encoding="utf-8")
    (bin_dir / "exvisit-blast").write_text(exvisit_BLAST_SH, encoding="utf-8")
    (bin_dir / "exvisit-anchor").write_text(exvisit_ANCHOR_SH, encoding="utf-8")
    (bin_dir / "exvisit-edit").write_text(exvisit_EDIT_SH, encoding="utf-8")
    entrypoint_path.write_text(ENTRYPOINT_SH, encoding="utf-8")
    claude_path.write_text(
        CLAUDE_MD_TEMPLATE.format(
            exvisit_mount_path=exvisit_MOUNT_PATH,
            workspace_repo_path=WORKSPACE_REPO_PATH,
            case_id=case.case_id,
            repo=case.repo,
            base_commit=case.base_commit or "<none>",
            issue_text=case.issue_text.strip(),
        ),
        encoding="utf-8",
    )
    dockerfile_path.write_text(DOCKERFILE_TEMPLATE.format(base_image=base_image), encoding="utf-8")
    run_script_path.write_text(
        RUN_SCRIPT_TEMPLATE.format(
            case_id=case.case_id,
            exvisit_host_path=Path(case.exvisit_path or "").resolve().as_posix(),
            repo_host_path=Path(case.repo_path).resolve().as_posix(),
            exvisit_mount_path=exvisit_MOUNT_PATH,
            input_repo_mount=INPUT_REPO_MOUNT,
        ),
        encoding="utf-8",
    )
    metadata = {
        "case_id": case.case_id,
        "repo": case.repo,
        "repo_path": case.repo_path,
        "base_commit": case.base_commit,
        "issue_text": case.issue_text,
        "exvisit_path": case.exvisit_path,
        "base_image": base_image,
    }
    (out_dir / "case.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    return MiniSweSandbox(
        case_id=case.case_id,
        sandbox_dir=out_dir,
        dockerfile_path=dockerfile_path,
        entrypoint_path=entrypoint_path,
        claude_path=claude_path,
        run_script_path=run_script_path,
    )


def _case_from_args(args) -> BenchmarkCase:
    if args.manifest:
        cases = load_manifest(Path(args.manifest))
        for case in cases:
            if case.case_id == args.case_id:
                return case
        raise KeyError(f"case '{args.case_id}' not found in manifest")
    if not all([args.repo, args.repo_path, args.issue_text, args.exv_path, args.case_id]):
        raise ValueError("either provide --manifest/--case-id or provide direct case fields")
    return BenchmarkCase(
        case_id=args.case_id,
        repo=args.repo,
        repo_path=args.repo_path,
        base_commit=args.base_commit,
        issue_text=args.issue_text,
        oracle_files=[],
        exvisit_path=args.exv_path,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mini_swe_agent")
    parser.add_argument("--manifest", default=None)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--base-image", default=DEFAULT_BASE_IMAGE)
    parser.add_argument("--repo", default=None)
    parser.add_argument("--repo-path", default=None)
    parser.add_argument("--base-commit", default=None)
    parser.add_argument("--issue-text", default=None)
    parser.add_argument("--exvisit-path", default=None)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    case = _case_from_args(args)
    sandbox = materialize_sandbox(case, Path(args.out_dir), base_image=args.base_image)
    print(json.dumps({
        "case_id": sandbox.case_id,
        "sandbox_dir": str(sandbox.sandbox_dir),
        "dockerfile": str(sandbox.dockerfile_path),
        "entrypoint": str(sandbox.entrypoint_path),
        "claude_md": str(sandbox.claude_path),
        "run_script": str(sandbox.run_script_path),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
