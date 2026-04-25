//! exvisit-mcp — MCP stdio server (v1).
//!
//! Exposes the full exvisit toolset to any MCP-compatible AI client
//! (Claude Desktop, Cursor, VS Code Copilot, OpenHands, etc.):
//!
//!   - `exv_init`    — generate a .exv structural map from a repository root
//!   - `exv_blast`   — rank files most relevant to an issue
//!   - `exv_query`   — extract a topological slice around a node
//!   - `exv_locate`  — score nodes with confidence margin for anchoring
//!   - `exv_expand`  — weighted neighborhood expansion from an anchor
//!   - `exv_anchor`  — resolve stacktrace to ground-zero anchor
//!   - `exv_deps`    — outbound dependency list for a node
//!   - `exv_callers` — inbound caller list for a node
//!   - `exv_verify`  — check structural consistency of .exv vs repo
//!
//! All tools subprocess-delegate to the `exv` / `python -m exvisit` CLI.
//! The binary speaks JSON-RPC 2.0 over stdin/stdout (stdio transport).
//!
//! Configuration (claude_desktop_config.json / .cursor/mcp.json / settings.json):
//!   {
//!     "mcpServers": {
//!       "exvisit": {
//!         "command": "/absolute/path/to/exvisit-mcp",
//!         "args": []
//!       }
//!     }
//!   }

use std::{
    io::ErrorKind,
    sync::Arc,
    time::{SystemTime, UNIX_EPOCH},
};

use anyhow::Result;
use rmcp::{
    RoleServer, ServerHandler, ServiceExt,
    model::{
        Annotated, CallToolRequestParams, CallToolResult, ErrorData,
        ListToolsResult, PaginatedRequestParams, ProtocolVersion,
        RawContent, RawTextContent, ServerInfo, Tool,
    },
    service::RequestContext,
    transport::stdio,
};
use serde_json::{Map, Value, json};

// ---------------------------------------------------------------------------
// Server struct
// ---------------------------------------------------------------------------

struct ExvisitMcp;

struct ExvisitCommandCandidate {
    program: String,
    prefix_args: Vec<String>,
    display: String,
}

impl ExvisitCommandCandidate {
    fn binary(program: impl Into<String>, display: impl Into<String>) -> Self {
        Self {
            program: program.into(),
            prefix_args: Vec::new(),
            display: display.into(),
        }
    }

    fn python_module(program: impl Into<String>, display: impl Into<String>) -> Self {
        Self {
            program: program.into(),
            prefix_args: vec!["-m".to_owned(), "exvisit".to_owned()],
            display: display.into(),
        }
    }
}

impl ExvisitMcp {
    /// Build a JSON Schema `Arc<JsonObject>` from a `serde_json::json!()` literal.
    fn schema(v: Value) -> Arc<Map<String, Value>> {
        Arc::new(
            v.as_object()
                .expect("schema must be a JSON object")
                .clone(),
        )
    }

    /// Construct a text `Content` item.
    fn text(s: impl Into<String>) -> rmcp::model::Content {
        Annotated {
            raw: RawContent::Text(RawTextContent { text: s.into(), meta: None }),
            annotations: None,
        }
    }

    fn exvisit_command_candidates() -> Vec<ExvisitCommandCandidate> {
        let mut candidates = Vec::new();

        if let Ok(cmd) = std::env::var("EXVISIT_CMD") {
            if !cmd.trim().is_empty() {
                candidates.push(ExvisitCommandCandidate::binary(
                    cmd,
                    "EXVISIT_CMD",
                ));
            }
        }

        candidates.push(ExvisitCommandCandidate::binary("exv", "exv from PATH"));

        if let Ok(python) = std::env::var("EXVISIT_PYTHON") {
            if !python.trim().is_empty() {
                candidates.push(ExvisitCommandCandidate::python_module(
                    python,
                    "EXVISIT_PYTHON -m exvisit",
                ));
            }
        }

        candidates.push(ExvisitCommandCandidate::python_module(
            "python",
            "python -m exvisit from PATH",
        ));
        candidates.push(ExvisitCommandCandidate::python_module(
            "py",
            "py -m exvisit from PATH",
        ));

        candidates
    }

    /// Write issue/error text to a temp file. Returns the path.
    fn write_temp_file(content: &str, prefix: &str) -> std::result::Result<std::path::PathBuf, String> {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis();
        let file_name = format!(
            "exv_{}_{}_{}.txt",
            prefix,
            std::process::id(),
            unique,
        );

        let mut candidates = vec![std::env::temp_dir().join(&file_name)];
        if let Ok(cwd) = std::env::current_dir() {
            candidates.push(cwd.join(&file_name));
        }

        let mut errors = Vec::new();
        for path in candidates {
            match std::fs::write(&path, content) {
                Ok(()) => return Ok(path),
                Err(err) => errors.push(format!("{} ({})", path.display(), err)),
            }
        }

        Err(format!(
            "Failed to write temp file. Tried: {}",
            errors.join("; ")
        ))
    }

    /// Run `exv <args>` as a subprocess, capturing stdout.
    fn run_exv(args: &[&str]) -> std::result::Result<String, String> {
        let mut launch_failures = Vec::new();

        for candidate in Self::exvisit_command_candidates() {
            let mut command = std::process::Command::new(&candidate.program);
            command.args(&candidate.prefix_args);
            command.args(args);

            match command.output() {
                Ok(output) => {
                    if output.status.success() {
                        return Ok(String::from_utf8_lossy(&output.stdout).into_owned());
                    }

                    let stderr = String::from_utf8_lossy(&output.stderr);
                    let stdout = String::from_utf8_lossy(&output.stdout);
                    let detail = if stderr.trim().is_empty() {
                        stdout
                    } else {
                        stderr
                    };
                    return Err(format!(
                        "exvisit CLI failed via {} (exit {}): {}",
                        candidate.display,
                        output.status,
                        detail.trim()
                    ));
                }
                Err(err) if err.kind() == ErrorKind::NotFound => {
                    launch_failures.push(format!("{} not found", candidate.display));
                }
                Err(err) => {
                    return Err(format!(
                        "Failed to launch exvisit CLI via {}: {}",
                        candidate.display,
                        err
                    ));
                }
            }
        }

        Err(format!(
            "exvisit CLI not found. Tried: {}. Install with `pip install exvisit` or set EXVISIT_PYTHON / EXVISIT_CMD.",
            launch_failures.join("; ")
        ))
    }

    /// Helper: get a required string argument.
    fn require_str<'a>(args: &'a Map<String, Value>, key: &str) -> std::result::Result<&'a str, CallToolResult> {
        args.get(key)
            .and_then(Value::as_str)
            .ok_or_else(|| CallToolResult::error(vec![Self::text(format!("Missing required argument: {key}"))]))
    }

    /// Helper: get an optional string argument.
    fn opt_str<'a>(args: &'a Map<String, Value>, key: &str) -> Option<&'a str> {
        args.get(key).and_then(Value::as_str)
    }

    /// Helper: get an optional integer argument.
    fn opt_int(args: &Map<String, Value>, key: &str) -> Option<i64> {
        args.get(key).and_then(Value::as_i64)
    }
}

// ---------------------------------------------------------------------------
// Tool definitions
// ---------------------------------------------------------------------------

fn tool_exv_init() -> Tool {
    Tool::new(
        "exv_init",
        "Generate a .exv structural map of a repository. \
         Call this once to build the spatial graph before using other exv_ tools. \
         Returns the absolute path of the generated .exv file.",
        ExvisitMcp::schema(json!({
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Absolute path to the repository root to map."
                },
                "out": {
                    "type": "string",
                    "description": "Output path for the .exv file. \
                                    Defaults to <repo_path>/project.exv."
                }
            },
            "required": ["repo_path"]
        })),
    )
}

fn tool_exv_blast() -> Tool {
    Tool::new(
        "exv_blast",
        "Query the .exv structural map to rank files most relevant to a \
         bug report, error message, or question. Returns a markdown or JSON bundle \
         with the files and code snippets the agent should read. \
         Use this instead of grepping or reading files at random.",
        ExvisitMcp::schema(json!({
            "type": "object",
            "properties": {
                "exv_file": {
                    "type": "string",
                    "description": "Path to the .exv file generated by exv_init."
                },
                "issue_text": {
                    "type": "string",
                    "description": "The bug report, error message, or question to resolve."
                },
                "preset": {
                    "type": "string",
                    "enum": ["test-fix", "crash-fix", "issue-fix"],
                    "description": "Blast preset: 'crash-fix' for tracebacks, 'test-fix' for test failures, 'issue-fix' for general issues. Auto-selected if omitted.",
                    "default": "issue-fix"
                },
                "format": {
                    "type": "string",
                    "enum": ["md", "json"],
                    "description": "Output format. 'md' for markdown, 'json' for structured JSON.",
                    "default": "md"
                },
                "max_files": {
                    "type": "integer",
                    "description": "Maximum number of files to return (default 5). \
                                    Use 3 for focused issues, 7-8 for multi-file changes.",
                    "default": 5
                }
            },
            "required": ["exv_file", "issue_text"]
        })),
    )
}

fn tool_exv_query() -> Tool {
    Tool::new(
        "exv_query",
        "Extract a topological slice of the .exv graph around a target node. \
         Returns the minimal .exv DSL showing the target + its neighbors up to N hops. \
         Use to understand the local neighborhood of a module/class.",
        ExvisitMcp::schema(json!({
            "type": "object",
            "properties": {
                "exv_file": {
                    "type": "string",
                    "description": "Path to the .exv file."
                },
                "target": {
                    "type": "string",
                    "description": "Target node: bare name (e.g. 'models') or dotted FQN (e.g. 'App.Core.Models')."
                },
                "neighbors": {
                    "type": "integer",
                    "description": "Number of hops to include (default 1).",
                    "default": 1
                },
                "direction": {
                    "type": "string",
                    "enum": ["in", "out", "both"],
                    "description": "Traversal direction: 'in' (callers), 'out' (dependencies), 'both'.",
                    "default": "both"
                }
            },
            "required": ["exv_file", "target"]
        })),
    )
}

fn tool_exv_locate() -> Tool {
    Tool::new(
        "exv_locate",
        "Score and rank all nodes against an issue/error to find the most likely files to edit. \
         Returns top-K anchors with confidence score and per-component breakdown. \
         More precise than blast for multi-signal reasoning.",
        ExvisitMcp::schema(json!({
            "type": "object",
            "properties": {
                "exv_file": {
                    "type": "string",
                    "description": "Path to the .exv file."
                },
                "issue_text": {
                    "type": "string",
                    "description": "The bug report, error message, or question."
                },
                "topk": {
                    "type": "integer",
                    "description": "Number of top anchors to return (default 3).",
                    "default": 3
                },
                "format": {
                    "type": "string",
                    "enum": ["text", "json"],
                    "description": "Output format.",
                    "default": "json"
                }
            },
            "required": ["exv_file", "issue_text"]
        })),
    )
}

fn tool_exv_expand() -> Tool {
    Tool::new(
        "exv_expand",
        "Expand outward from an anchor node to find its weighted neighbors. \
         Returns the neighborhood with PageRank + edge-type priors. \
         Use after exv_blast or exv_locate to explore around a known anchor.",
        ExvisitMcp::schema(json!({
            "type": "object",
            "properties": {
                "exv_file": {
                    "type": "string",
                    "description": "Path to the .exv file."
                },
                "anchor": {
                    "type": "string",
                    "description": "Fully-qualified node name to expand from (e.g. 'App.Core.Models')."
                },
                "hops": {
                    "type": "integer",
                    "description": "Traversal depth (default 1).",
                    "default": 1
                },
                "max_files": {
                    "type": "integer",
                    "description": "Max neighbors to return (default 4).",
                    "default": 4
                },
                "format": {
                    "type": "string",
                    "enum": ["text", "json"],
                    "description": "Output format.",
                    "default": "json"
                }
            },
            "required": ["exv_file", "anchor"]
        })),
    )
}

fn tool_exv_anchor() -> Tool {
    Tool::new(
        "exv_anchor",
        "Resolve a stacktrace or error log to its ground-zero anchor in the .exv graph. \
         Returns the most likely origin file plus direct imports/dependents. \
         Use when you have a Python traceback or error log.",
        ExvisitMcp::schema(json!({
            "type": "object",
            "properties": {
                "exv_file": {
                    "type": "string",
                    "description": "Path to the .exv file."
                },
                "stacktrace": {
                    "type": "string",
                    "description": "The full stacktrace or error log text."
                },
                "max_hits": {
                    "type": "integer",
                    "description": "Maximum anchor hits to return (default 6).",
                    "default": 6
                },
                "format": {
                    "type": "string",
                    "enum": ["text", "json"],
                    "description": "Output format.",
                    "default": "json"
                }
            },
            "required": ["exv_file", "stacktrace"]
        })),
    )
}

fn tool_exv_deps() -> Tool {
    Tool::new(
        "exv_deps",
        "List the outbound dependencies (imports) of a node in the .exv graph. \
         Shows what a module depends on.",
        ExvisitMcp::schema(json!({
            "type": "object",
            "properties": {
                "exv_file": {
                    "type": "string",
                    "description": "Path to the .exv file."
                },
                "target": {
                    "type": "string",
                    "description": "Node name to query dependencies for."
                },
                "hops": {
                    "type": "integer",
                    "description": "Depth of outbound traversal (default 1).",
                    "default": 1
                }
            },
            "required": ["exv_file", "target"]
        })),
    )
}

fn tool_exv_callers() -> Tool {
    Tool::new(
        "exv_callers",
        "List the inbound callers (dependents) of a node in the .exv graph. \
         Shows what depends on this module.",
        ExvisitMcp::schema(json!({
            "type": "object",
            "properties": {
                "exv_file": {
                    "type": "string",
                    "description": "Path to the .exv file."
                },
                "target": {
                    "type": "string",
                    "description": "Node name to query callers for."
                },
                "hops": {
                    "type": "integer",
                    "description": "Depth of inbound traversal (default 1).",
                    "default": 1
                }
            },
            "required": ["exv_file", "target"]
        })),
    )
}

fn tool_exv_verify() -> Tool {
    Tool::new(
        "exv_verify",
        "Verify the structural consistency of a .exv file against the actual repository. \
         Reports missing edges (real imports not declared), ghost edges (declared but not real), \
         and unresolved source files. Use to check if .exv is stale.",
        ExvisitMcp::schema(json!({
            "type": "object",
            "properties": {
                "exv_file": {
                    "type": "string",
                    "description": "Path to the .exv file to verify."
                },
                "repo_path": {
                    "type": "string",
                    "description": "Repository root path. Inferred from .exv location if omitted."
                }
            },
            "required": ["exv_file"]
        })),
    )
}

// ---------------------------------------------------------------------------
// ServerHandler implementation
// ---------------------------------------------------------------------------

impl ServerHandler for ExvisitMcp {
    fn get_info(&self) -> ServerInfo {
        let mut info = ServerInfo::default();
        info.protocol_version = ProtocolVersion::LATEST;
        info.server_info.name = "exvisit-mcp".into();
        info.server_info.version = env!("CARGO_PKG_VERSION").into();
        info.instructions = Some(
            "Exvisit MCP provides spatial code-graph navigation tools. \
             Start with exv_init to generate a .exv map of the repository, then use \
             exv_blast to find relevant files for any issue. Use exv_locate for \
             multi-signal precision, exv_expand to explore neighborhoods, \
             exv_anchor to resolve stacktraces, exv_query for topological slices, \
             exv_deps/exv_callers for dependency traversal, and exv_verify to \
             check structural consistency."
                .into(),
        );
        info.capabilities.tools = Some(Default::default());
        info
    }

    async fn list_tools(
        &self,
        _request: Option<PaginatedRequestParams>,
        _ctx: RequestContext<RoleServer>,
    ) -> std::result::Result<ListToolsResult, ErrorData> {
        Ok(ListToolsResult {
            tools: vec![
                tool_exv_init(),
                tool_exv_blast(),
                tool_exv_query(),
                tool_exv_locate(),
                tool_exv_expand(),
                tool_exv_anchor(),
                tool_exv_deps(),
                tool_exv_callers(),
                tool_exv_verify(),
            ],
            next_cursor: None,
            meta: None,
        })
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        _ctx: RequestContext<RoleServer>,
    ) -> std::result::Result<CallToolResult, ErrorData> {
        let args = request.arguments.unwrap_or_default();

        match request.name.as_ref() {
            // ---------------------------------------------------------------
            // exv_init — scaffold a .exv from a repo root
            // ---------------------------------------------------------------
            "exv_init" => {
                let repo_path = match Self::require_str(&args, "repo_path") {
                    Ok(v) => v.to_owned(),
                    Err(e) => return Ok(e),
                };

                let default_out = format!("{}/project.exv", repo_path.trim_end_matches(['/', '\\']));
                let out = Self::opt_str(&args, "out")
                    .unwrap_or(&default_out)
                    .to_owned();

                log::info!("exv_init: repo={repo_path} out={out}");

                match Self::run_exv(&["init", "--repo", &repo_path, "--out", &out]) {
                    Ok(stdout) => {
                        let msg = format!(
                            "Structural map generated: {out}\n{stdout}\n\
                             Call exv_blast with exv_file=\"{out}\" to find relevant files."
                        );
                        Ok(CallToolResult::success(vec![Self::text(msg)]))
                    }
                    Err(e) => Ok(CallToolResult::error(vec![Self::text(e)])),
                }
            }

            // ---------------------------------------------------------------
            // exv_blast — rank files for an issue
            // ---------------------------------------------------------------
            "exv_blast" => {
                let exv_file = match Self::require_str(&args, "exv_file") {
                    Ok(v) => v.to_owned(),
                    Err(e) => return Ok(e),
                };
                let issue_text = match Self::require_str(&args, "issue_text") {
                    Ok(v) => v.to_owned(),
                    Err(e) => return Ok(e),
                };

                let tmp = match Self::write_temp_file(&issue_text, "blast") {
                    Ok(p) => p,
                    Err(e) => return Ok(CallToolResult::error(vec![Self::text(e)])),
                };
                let tmp_str = tmp.to_string_lossy().into_owned();

                let format = Self::opt_str(&args, "format").unwrap_or("md");
                let mut cli_args: Vec<&str> = vec![
                    "blast", &exv_file, "--issue-file", &tmp_str, "--format", format,
                ];

                let preset_val;
                if let Some(preset) = Self::opt_str(&args, "preset") {
                    preset_val = preset.to_owned();
                    cli_args.push("--preset");
                    cli_args.push(&preset_val);
                }

                let result = Self::run_exv(&cli_args);
                let _ = std::fs::remove_file(&tmp);

                match result {
                    Ok(out) => Ok(CallToolResult::success(vec![Self::text(out)])),
                    Err(e) => Ok(CallToolResult::error(vec![Self::text(e)])),
                }
            }

            // ---------------------------------------------------------------
            // exv_query — topological slice
            // ---------------------------------------------------------------
            "exv_query" => {
                let exv_file = match Self::require_str(&args, "exv_file") {
                    Ok(v) => v.to_owned(),
                    Err(e) => return Ok(e),
                };
                let target = match Self::require_str(&args, "target") {
                    Ok(v) => v.to_owned(),
                    Err(e) => return Ok(e),
                };

                let neighbors = Self::opt_int(&args, "neighbors").unwrap_or(1).to_string();
                let direction = Self::opt_str(&args, "direction").unwrap_or("both");

                match Self::run_exv(&[
                    "query", &exv_file, "--target", &target,
                    "--neighbors", &neighbors, "--direction", direction,
                ]) {
                    Ok(out) => Ok(CallToolResult::success(vec![Self::text(out)])),
                    Err(e) => Ok(CallToolResult::error(vec![Self::text(e)])),
                }
            }

            // ---------------------------------------------------------------
            // exv_locate — multi-signal anchor scoring
            // ---------------------------------------------------------------
            "exv_locate" => {
                let exv_file = match Self::require_str(&args, "exv_file") {
                    Ok(v) => v.to_owned(),
                    Err(e) => return Ok(e),
                };
                let issue_text = match Self::require_str(&args, "issue_text") {
                    Ok(v) => v.to_owned(),
                    Err(e) => return Ok(e),
                };

                let tmp = match Self::write_temp_file(&issue_text, "locate") {
                    Ok(p) => p,
                    Err(e) => return Ok(CallToolResult::error(vec![Self::text(e)])),
                };
                let tmp_str = tmp.to_string_lossy().into_owned();

                let topk = Self::opt_int(&args, "topk").unwrap_or(3).to_string();
                let format = Self::opt_str(&args, "format").unwrap_or("json");

                let result = Self::run_exv(&[
                    "locate", &exv_file, "--issue-file", &tmp_str,
                    "--topk", &topk, "--format", format,
                ]);
                let _ = std::fs::remove_file(&tmp);

                match result {
                    Ok(out) => Ok(CallToolResult::success(vec![Self::text(out)])),
                    Err(e) => Ok(CallToolResult::error(vec![Self::text(e)])),
                }
            }

            // ---------------------------------------------------------------
            // exv_expand — weighted neighborhood expansion
            // ---------------------------------------------------------------
            "exv_expand" => {
                let exv_file = match Self::require_str(&args, "exv_file") {
                    Ok(v) => v.to_owned(),
                    Err(e) => return Ok(e),
                };
                let anchor = match Self::require_str(&args, "anchor") {
                    Ok(v) => v.to_owned(),
                    Err(e) => return Ok(e),
                };

                let hops = Self::opt_int(&args, "hops").unwrap_or(1).to_string();
                let max_files = Self::opt_int(&args, "max_files").unwrap_or(4).to_string();
                let format = Self::opt_str(&args, "format").unwrap_or("json");

                match Self::run_exv(&[
                    "expand", &exv_file, "--anchor", &anchor,
                    "--hops", &hops, "--max-files", &max_files, "--format", format,
                ]) {
                    Ok(out) => Ok(CallToolResult::success(vec![Self::text(out)])),
                    Err(e) => Ok(CallToolResult::error(vec![Self::text(e)])),
                }
            }

            // ---------------------------------------------------------------
            // exv_anchor — stacktrace resolution
            // ---------------------------------------------------------------
            "exv_anchor" => {
                let exv_file = match Self::require_str(&args, "exv_file") {
                    Ok(v) => v.to_owned(),
                    Err(e) => return Ok(e),
                };
                let stacktrace = match Self::require_str(&args, "stacktrace") {
                    Ok(v) => v.to_owned(),
                    Err(e) => return Ok(e),
                };

                let tmp = match Self::write_temp_file(&stacktrace, "anchor") {
                    Ok(p) => p,
                    Err(e) => return Ok(CallToolResult::error(vec![Self::text(e)])),
                };
                let tmp_str = tmp.to_string_lossy().into_owned();

                let max_hits = Self::opt_int(&args, "max_hits").unwrap_or(6).to_string();
                let format = Self::opt_str(&args, "format").unwrap_or("json");

                let result = Self::run_exv(&[
                    "anchor", &exv_file, "--stacktrace", &tmp_str,
                    "--max-hits", &max_hits, "--format", format,
                ]);
                let _ = std::fs::remove_file(&tmp);

                match result {
                    Ok(out) => Ok(CallToolResult::success(vec![Self::text(out)])),
                    Err(e) => Ok(CallToolResult::error(vec![Self::text(e)])),
                }
            }

            // ---------------------------------------------------------------
            // exv_deps — outbound dependencies
            // ---------------------------------------------------------------
            "exv_deps" => {
                let exv_file = match Self::require_str(&args, "exv_file") {
                    Ok(v) => v.to_owned(),
                    Err(e) => return Ok(e),
                };
                let target = match Self::require_str(&args, "target") {
                    Ok(v) => v.to_owned(),
                    Err(e) => return Ok(e),
                };

                let hops = Self::opt_int(&args, "hops").unwrap_or(1).to_string();

                match Self::run_exv(&[
                    "deps", &exv_file, &target, "--hops", &hops,
                ]) {
                    Ok(out) => Ok(CallToolResult::success(vec![Self::text(out)])),
                    Err(e) => Ok(CallToolResult::error(vec![Self::text(e)])),
                }
            }

            // ---------------------------------------------------------------
            // exv_callers — inbound callers
            // ---------------------------------------------------------------
            "exv_callers" => {
                let exv_file = match Self::require_str(&args, "exv_file") {
                    Ok(v) => v.to_owned(),
                    Err(e) => return Ok(e),
                };
                let target = match Self::require_str(&args, "target") {
                    Ok(v) => v.to_owned(),
                    Err(e) => return Ok(e),
                };

                let hops = Self::opt_int(&args, "hops").unwrap_or(1).to_string();

                match Self::run_exv(&[
                    "callers", &exv_file, &target, "--hops", &hops,
                ]) {
                    Ok(out) => Ok(CallToolResult::success(vec![Self::text(out)])),
                    Err(e) => Ok(CallToolResult::error(vec![Self::text(e)])),
                }
            }

            // ---------------------------------------------------------------
            // exv_verify — structural consistency check
            // ---------------------------------------------------------------
            "exv_verify" => {
                let exv_file = match Self::require_str(&args, "exv_file") {
                    Ok(v) => v.to_owned(),
                    Err(e) => return Ok(e),
                };

                let mut cli_args = vec!["verify", exv_file.as_str()];

                let repo_val;
                if let Some(repo) = Self::opt_str(&args, "repo_path") {
                    repo_val = repo.to_owned();
                    cli_args.push("--repo");
                    cli_args.push(&repo_val);
                }

                match Self::run_exv(&cli_args) {
                    Ok(out) => Ok(CallToolResult::success(vec![Self::text(out)])),
                    // verify exits non-zero when diagnostics are found — still useful output
                    Err(e) => Ok(CallToolResult::success(vec![Self::text(format!("Diagnostics found:\n{e}"))])),
                }
            }

            unknown => Ok(CallToolResult::error(vec![Self::text(format!(
                "Unknown tool: {unknown}"
            ))])),
        }
    }
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

#[tokio::main]
async fn main() -> Result<()> {
    // Log to stderr so stdout remains clean for MCP JSON-RPC framing.
    env_logger::Builder::from_env(
        env_logger::Env::default().default_filter_or("warn"),
    )
    .target(env_logger::Target::Stderr)
    .init();

    log::info!("exvisit-mcp v{} starting (stdio transport)", env!("CARGO_PKG_VERSION"));

    let service = ExvisitMcp.serve(stdio()).await?;
    service.waiting().await?;

    Ok(())
}

