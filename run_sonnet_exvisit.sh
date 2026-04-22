#!/usr/bin/env bash
# exvisit + Claude Sonnet runner — hooks into swebench_lite_harness.py via
# --exvisit-runner-cmd.
#
# USAGE (as a harness template):
#   --exvisit-runner-cmd \
#     "exvisit_MANIFEST=/data/manifest.json \
#      bash /abs/path/run_sonnet_exvisit.sh {repo_path} {exvisit_path} {case_id} {workspace_path}"
#
# NOTE: Do NOT include {issue_text} in the template — it contains newlines and
# special characters that break shell quoting.  The runner fetches issue_text
# from the manifest instead.
#
# REQUIRED ENVIRONMENT VARIABLES:
#   exvisit_MANIFEST      — absolute path to the manifest.json written by
#                         `python bench/swebench_lite_harness.py precompute`
#   ANTHROPIC_API_KEY   — required when exvisit_MODEL is an Anthropic model
#   GOOGLE_API_KEY      — required when exvisit_MODEL starts with gemini
#                         (`GEMINI_API_KEY` also works)
#
# OPTIONAL ENVIRONMENT VARIABLES:
#   exvisit_MODEL         — model ID
#                         (default: claude-sonnet-4-5; gemini-* also supported)
#   exvisit_MAX_STEPS     — max agentic turns before abort (default: 20)
#   exvisit_TRAJ_DIR      — directory for trajectory JSON files
#                         (default: /tmp/exvisit-trajs)
#   exvisit_PRICING_FILE  — path to pricing JSON for cost annotation
#                         (default: config/pricing_sonnet.json next to this script)
#
# EXIT CODES:
#   0  — tests passed (pass@1 = True)
#   1  — agent ran but tests did not pass
#   2  — fatal / configuration error
#
# EXAMPLE FULL INVOCATION:
#   # Step 1 – precompute (materialise .exv files and manifest)
#   python bench/swebench_lite_harness.py precompute \
#     --dataset swebench-lite --repos django/django --limit 5 \
#     --cache-dir /data/cache --manifest /data/manifest.json
#
#   # Step 2 – run benchmark with this runner
#   python bench/swebench_lite_harness.py run \
#     --manifest /data/manifest.json \
#     --out /data/results.json \
#     --pricing-file config/pricing_sonnet.json \
#     --workspace-root /data/workspaces \
#     --exvisit-runner-cmd \
#       "exvisit_MANIFEST=/data/manifest.json \
#        bash $(pwd)/run_sonnet_exvisit.sh {repo_path} {exvisit_path} {case_id} {workspace_path}"

set -euo pipefail

# ── locate project root (directory containing this script) ────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"

# ── positional args (injected by harness template substitution) ───────────────
REPO_PATH="${1:?ERROR: repo_path argument missing. Did you set up the harness template correctly?}"
exvisit_PATH="${2:?ERROR: exvisit_path argument missing.}"
CASE_ID="${3:?ERROR: case_id argument missing.}"
WORKSPACE_PATH="${4:-$REPO_PATH}"

# ── resolve pricing file ───────────────────────────────────────────────────────
: "${exvisit_PRICING_FILE:=$REPO_ROOT/config/pricing_sonnet.json}"

# ── validate prerequisites ────────────────────────────────────────────────────
MODEL_NAME="${exvisit_MODEL:-claude-sonnet-4-5}"
if [[ "$MODEL_NAME" == gemini* ]]; then
  if [[ -z "${GOOGLE_API_KEY:-${GEMINI_API_KEY:-}}" ]]; then
    printf '{"error":"GOOGLE_API_KEY or GEMINI_API_KEY is not set","pass_at_1":false}\n'
    exit 2
  fi
else
  if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    printf '{"error":"ANTHROPIC_API_KEY is not set","pass_at_1":false}\n'
    exit 2
  fi
fi

if [[ ! -f "$exvisit_PATH" ]]; then
  printf '{"error":"exvisit file not found: %s","pass_at_1":false}\n' "$exvisit_PATH"
  exit 2
fi

if [[ ! -d "$WORKSPACE_PATH" ]]; then
  printf '{"error":"workspace not found: %s","pass_at_1":false}\n' "$WORKSPACE_PATH"
  exit 2
fi

# ── materialise the mini-swe-agent sandbox context ───────────────────────────
# This writes Dockerfile, entrypoint.sh, CLAUDE.md, and bin/ wrappers into a
# temp directory.  The sandbox is not executed as Docker here; instead the
# runner_agent.py drives the tools directly using the same constraints.
SANDBOX_DIR="$(mktemp -d /tmp/exvisit-sandbox-XXXXXX)"
trap 'rm -rf "$SANDBOX_DIR"' EXIT

python -m bench.mini_swe_agent \
  --case-id     "$CASE_ID" \
  --out-dir     "$SANDBOX_DIR" \
  --repo-path   "$REPO_PATH" \
  --exvisit-path  "$exvisit_PATH" \
  ${exvisit_MANIFEST:+--manifest "$exvisit_MANIFEST"} \
  > "$SANDBOX_DIR/sandbox_meta.json" 2>&1 || true
# (failure is non-fatal; the agent falls back to manifest lookup for issue_text)

# ── delegate to the Python agentic runner ────────────────────────────────────
exec python -u "$REPO_ROOT/bench/runner_agent.py" \
  --repo-path   "$REPO_PATH" \
  --exvisit-path  "$exvisit_PATH" \
  --case-id     "$CASE_ID" \
  --workspace   "$WORKSPACE_PATH" \
  ${exvisit_MANIFEST:+--manifest        "$exvisit_MANIFEST"} \
  ${exvisit_MODEL:+--model            "$exvisit_MODEL"} \
  ${exvisit_MAX_STEPS:+--max-steps      "$exvisit_MAX_STEPS"} \
  ${exvisit_TRAJ_DIR:+--traj-dir       "$exvisit_TRAJ_DIR"} \
  ${exvisit_PRICING_FILE:+--pricing-file  "$exvisit_PRICING_FILE"}

