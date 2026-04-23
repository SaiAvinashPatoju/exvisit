#!/usr/bin/env bash
# exvisit + Claude Sonnet runner â€” hooks into swebench_lite_harness.py via
# --exvisit-runner-cmd.
#
# USAGE (as a harness template):
#   --exvisit-runner-cmd \
#     "exvisit_MANIFEST=/data/manifest.json \
#      bash /abs/path/run_sonnet_exvisit.sh {repo_path} {exvisit_path} {case_id} {workspace_path}"
#
# NOTE: Do NOT include {issue_text} in the template â€” it contains newlines and
# special characters that break shell quoting.  The runner fetches issue_text
# from the manifest instead.
#
# REQUIRED ENVIRONMENT VARIABLES:
#   exvisit_MANIFEST      â€” absolute path to the manifest.json written by
#                         `python bench/swebench_lite_harness.py precompute`
#   ANTHROPIC_API_KEY     â€” required when exvisit_MODEL is an Anthropic model
#   GOOGLE_API_KEY        â€” required when exvisit_MODEL starts with gemini
#                           (`GEMINI_API_KEY` also works)
#   OPENROUTER_API_KEY    â€” required when exvisit_MODEL is an OpenRouter model
#
# OPTIONAL ENVIRONMENT VARIABLES:
#   exvisit_MODEL         â€” model ID
#                         (default: claude-sonnet-4-5; gemini-* and OpenRouter
#                         slash-form IDs are also supported)
#   exvisit_PYTHON        â€” Python executable to use for helper entry points
#                         (default: repo .venv python if present, else `python`)
#   exvisit_MAX_STEPS     â€” max agentic turns before abort (default: 20)
#   exvisit_TRAJ_DIR      â€” directory for trajectory JSON files
#                         (default: /tmp/exvisit-trajs)
#   exvisit_PRICING_FILE  â€” path to pricing JSON for cost annotation
#                         (default: config/pricing_sonnet.json next to this script)
#
# EXIT CODES:
#   0  â€” tests passed (pass@1 = True)
#   1  â€” agent ran but tests did not pass
#   2  â€” fatal / configuration error
#
# EXAMPLE FULL INVOCATION:
#   # Step 1 â€“ precompute (materialise .exv files and manifest)
#   python bench/swebench_lite_harness.py precompute \
#     --dataset swebench-lite --repos django/django --limit 5 \
#     --cache-dir /data/cache --manifest /data/manifest.json
#
#   # Step 2 â€“ run benchmark with this runner
#   python bench/swebench_lite_harness.py run \
#     --manifest /data/manifest.json \
#     --out /data/results.json \
#     --pricing-file config/pricing_sonnet.json \
#     --workspace-root /data/workspaces \
#     --exvisit-runner-cmd \
#       "exvisit_MANIFEST=/data/manifest.json \
#        bash $(pwd)/run_sonnet_exvisit.sh {repo_path} {exvisit_path} {case_id} {workspace_path}"

set -euo pipefail

# â”€â”€ locate project root (directory containing this script) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"

# â”€â”€ positional args (injected by harness template substitution) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
REPO_PATH="${1:?ERROR: repo_path argument missing. Did you set up the harness template correctly?}"
exvisit_PATH="${2:?ERROR: exvisit_path argument missing.}"
CASE_ID="${3:?ERROR: case_id argument missing.}"
WORKSPACE_PATH="${4:-$REPO_PATH}"

# â”€â”€ select Python interpreter deterministically â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
PYTHON_BIN="${exvisit_PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -f "$REPO_ROOT/.venv/Scripts/python.exe" ]]; then
    PYTHON_BIN="$REPO_ROOT/.venv/Scripts/python.exe"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
  else
    printf '{"error":"python executable not found","pass_at_1":false}\n'
    exit 2
  fi
fi

# â”€â”€ resolve pricing file â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
: "${exvisit_PRICING_FILE:=$REPO_ROOT/config/pricing_sonnet.json}"

# â”€â”€ validate prerequisites â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
MODEL_NAME="${5:-${exvisit_MODEL:-claude-sonnet-4-5}}"
MODEL_NAME_LOWER="$(printf '%s' "$MODEL_NAME" | tr '[:upper:]' '[:lower:]')"

if [[ "$MODEL_NAME_LOWER" == gemini* ]]; then
  G_KEY="${6:-${GOOGLE_API_KEY:-${GEMINI_API_KEY:-}}}"
  if [[ -z "$G_KEY" ]]; then
    printf '{"error":"GOOGLE_API_KEY or GEMINI_API_KEY is not set","pass_at_1":false}\n'
    exit 2
  fi
  export GOOGLE_API_KEY="$G_KEY"
elif [[ "$MODEL_NAME_LOWER" == */* ]]; then
  O_KEY="${6:-${OPENROUTER_API_KEY:-}}"
  if [[ -z "$O_KEY" ]]; then
    printf '{"error":"OPENROUTER_API_KEY is not set","pass_at_1":false}\n'
    exit 2
  fi
  export OPENROUTER_API_KEY="$O_KEY"
else
  A_KEY="${6:-${ANTHROPIC_API_KEY:-}}"
  if [[ -z "$A_KEY" ]]; then
    printf '{"error":"ANTHROPIC_API_KEY is not set","pass_at_1":false}\n'
    exit 2
  fi
  export ANTHROPIC_API_KEY="$A_KEY"
fi

if [[ ! -f "$exvisit_PATH" ]]; then
  printf '{"error":"exvisit file not found: %s","pass_at_1":false}\n' "$exvisit_PATH"
  exit 2
fi

if [[ ! -d "$WORKSPACE_PATH" ]]; then
  printf '{"error":"workspace not found: %s","pass_at_1":false}\n' "$WORKSPACE_PATH"
  exit 2
fi

# â”€â”€ materialise the mini-swe-agent sandbox context â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# This writes Dockerfile, entrypoint.sh, CLAUDE.md, and bin/ wrappers into a
# temp directory.  The sandbox is not executed as Docker here; instead the
# runner_agent.py drives the tools directly using the same constraints.
SANDBOX_DIR="$(mktemp -d /tmp/exvisit-sandbox-XXXXXX)"
trap 'rm -rf "$SANDBOX_DIR"' EXIT

"$PYTHON_BIN" -m bench.mini_swe_agent \
  --case-id     "$CASE_ID" \
  --out-dir     "$SANDBOX_DIR" \
  --repo-path   "$REPO_PATH" \
  --exvisit-path  "$exvisit_PATH" \
  ${exvisit_MANIFEST:+--manifest "$exvisit_MANIFEST"} \
  > "$SANDBOX_DIR/sandbox_meta.json" 2>&1 || true
# (failure is non-fatal; the agent falls back to manifest lookup for issue_text)

# â”€â”€ delegate to the Python agentic runner â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
exec "$PYTHON_BIN" -u "$REPO_ROOT/bench/runner_agent.py" \
  --repo-path   "$REPO_PATH" \
  --exvisit-path  "$exvisit_PATH" \
  --case-id     "$CASE_ID" \
  --workspace   "$WORKSPACE_PATH" \
  --manifest    "${7:-${exvisit_MANIFEST:-}}" \
  --model "$MODEL_NAME" \
  ${exvisit_MAX_STEPS:+--max-steps      "$exvisit_MAX_STEPS"} \
  ${exvisit_TRAJ_DIR:+--traj-dir       "$exvisit_TRAJ_DIR"} \
  ${exvisit_PRICING_FILE:+--pricing-file  "$exvisit_PRICING_FILE"}
