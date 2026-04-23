//! exvisit-mcp — MCP stdio server (Phase 1).
//!
//! Exposes two tools to any MCP-compatible AI client (Claude Desktop, Cursor, etc.):
//!
//!   - `exv_init`  — generate a .exv structural map from a repository root
//!   - `exv_blast` — query the spatial graph to rank files for an issue
//!
//! Both tools subprocess-delegate to the `exv` Python CLI already installed in
//! the user's PATH. The binary speaks JSON-RPC 2.0 over stdin/stdout as required
//! by the MCP specification (stdio transport).
//!
//! Configuration (claude_desktop_config.json / .cursor/mcp.json):
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

    fn write_issue_file(issue_text: &str) -> std::result::Result<std::path::PathBuf, String> {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis();
        let file_name = format!(
            "exv_blast_issue_{}_{}.txt",
            std::process::id(),
            unique
        );

        let mut candidates = vec![std::env::temp_dir().join(&file_name)];
        if let Ok(cwd) = std::env::current_dir() {
            candidates.push(cwd.join(&file_name));
        }

        let mut errors = Vec::new();
        for path in candidates {
            match std::fs::write(&path, issue_text) {
                Ok(()) => return Ok(path),
                Err(err) => errors.push(format!("{} ({})", path.display(), err)),
            }
        }

        Err(format!(
            "Failed to write issue file. Tried: {}",
            errors.join("; ")
        ))
    }

    /// Run `exv <args>` as a subprocess, capturing stdout.
    /// Returns `Err(message)` if the process fails to launch or exits non-zero.
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
                    return Err(format!(
                        "Exvisit CLI failed via {} with status {}: {}",
                        candidate.display,
                        output.status,
                        stderr.trim()
                    ));
                }
                Err(err) if err.kind() == ErrorKind::NotFound => {
                    launch_failures.push(format!("{} not found", candidate.display));
                }
                Err(err) => {
                    return Err(format!(
                        "Failed to launch Exvisit CLI via {}: {}",
                        candidate.display,
                        err
                    ));
                }
            }
        }

        Err(format!(
            "Failed to launch Exvisit CLI. Tried: {}. Install exvisit (`pip install exvisit`) or set EXVISIT_PYTHON / EXVISIT_CMD.",
            launch_failures.join("; ")
        ))
    }
}

// ---------------------------------------------------------------------------
// ServerHandler implementation
// ---------------------------------------------------------------------------

impl ServerHandler for ExvisitMcp {
    fn get_info(&self) -> ServerInfo {
        // Use Default then mutate — required for #[non_exhaustive] structs
        // defined in an external crate.
        let mut info = ServerInfo::default();
        info.protocol_version = ProtocolVersion::LATEST;
        info.server_info.name = "exvisit-mcp".into();
        info.server_info.version = env!("CARGO_PKG_VERSION").into();
        info.instructions = Some(
            "Use exv_init to generate a .exv map of the repository, then \
             exv_blast to find the most relevant files for any issue or question."
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
        let exv_init = Tool::new(
            "exv_init",
            "Generate a .exv structural map of a repository. \
             Call this once to build the spatial graph before using exv_blast. \
             Returns the absolute path of the generated .exv file.",
            Self::schema(json!({
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
        );

        let exv_blast = Tool::new(
            "exv_blast",
            "Query the .exv structural map to rank files most relevant to a \
             bug report, error message, or question. Returns a JSON bundle \
             listing the files and code snippets the agent should read. \
             Use this instead of grepping or reading files at random.",
            Self::schema(json!({
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
                    "max_files": {
                        "type": "integer",
                        "description": "Maximum number of files to return (default 5). \
                                        Use 3 for focused issues, 7-8 for multi-file changes.",
                        "default": 5
                    }
                },
                "required": ["exv_file", "issue_text"]
            })),
        );

        Ok(ListToolsResult {
            tools: vec![exv_init, exv_blast],
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
                let repo_path = match args.get("repo_path").and_then(Value::as_str) {
                    Some(p) => p.to_owned(),
                    None => {
                        return Ok(CallToolResult::error(vec![Self::text(
                            "Missing required argument: repo_path",
                        )]));
                    }
                };

                let default_out = format!("{}/project.exv", repo_path.trim_end_matches('/'));
                let out = args
                    .get("out")
                    .and_then(Value::as_str)
                    .unwrap_or(&default_out)
                    .to_owned();

                log::info!("exv_init: repo={repo_path} out={out}");

                match Self::run_exv(&["init", "--repo", &repo_path, "--out", &out]) {
                    Ok(_) => Ok(CallToolResult::success(vec![Self::text(format!(
                        "Structural map generated: {out}\n\
                         Call exv_blast with exv_file=\"{out}\" to find relevant files."
                    ))])),
                    Err(e) => Ok(CallToolResult::error(vec![Self::text(e)])),
                }
            }

            // ---------------------------------------------------------------
            // exv_blast — rank files for an issue
            // ---------------------------------------------------------------
            "exv_blast" => {
                let exv_file = match args.get("exv_file").and_then(Value::as_str) {
                    Some(p) => p.to_owned(),
                    None => {
                        return Ok(CallToolResult::error(vec![Self::text(
                            "Missing required argument: exv_file",
                        )]));
                    }
                };

                let issue_text = match args.get("issue_text").and_then(Value::as_str) {
                    Some(t) => t.to_owned(),
                    None => {
                        return Ok(CallToolResult::error(vec![Self::text(
                            "Missing required argument: issue_text",
                        )]));
                    }
                };

                log::info!("exv_blast: file={exv_file}");

                // Write issue text to a temp file to avoid shell-quoting issues
                // with multi-line error messages.
                let tmp = match Self::write_issue_file(&issue_text) {
                    Ok(path) => path,
                    Err(e) => {
                        return Ok(CallToolResult::error(vec![Self::text(e)]));
                    }
                };
                if !tmp.exists() {
                    return Ok(CallToolResult::error(vec![Self::text(format!(
                        "Issue file was not created: {}",
                        tmp.display()
                    ))]));
                }
                let tmp_str = tmp.to_string_lossy().into_owned();

                let result = Self::run_exv(&[
                    "blast",
                    &exv_file,
                    "--issue-file",
                    &tmp_str,
                    "--format",
                    "json",
                ]);
                let _ = std::fs::remove_file(&tmp);

                match result {
                    Ok(json_out) => {
                        Ok(CallToolResult::success(vec![Self::text(json_out)]))
                    }
                    Err(e) => Ok(CallToolResult::error(vec![Self::text(e)])),
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

