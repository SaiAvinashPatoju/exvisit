//! Phase 3 stub: tree-sitter-backed topology structures.
//!
//! These types will hold parsed AST data once the `tree-sitter-python` grammar
//! is wired in. For now they are empty scaffolds — all parsing functions return
//! a default empty topology.
//!
//! Progression plan:
//!   Phase 3a (this file) — data structures + stub parse function
//!   Phase 3b             — wire tree-sitter-python grammar into parse_python_file
//!   Phase 3c             — populate .exv node line ranges from CodeTopology
//!   Phase 4              — multi-language support via tree-sitter grammars

// tree-sitter will be used here once Phase 3b begins.
// use tree_sitter::{Language, Parser};

/// A single code entity extracted from a source file by the tree-sitter parser.
///
/// Maps 1-to-1 with a node in the `.exv` spatial graph. Once Phase 3b is
/// complete, the `line_start`/`line_end` fields here will back-fill the
/// `lines=` attribute in the `.exv` DSL so that `exv blast` can extract
/// precise code snippets.
#[derive(Debug, Clone, Default)]
pub struct SpatialNode {
    /// Fully-qualified name, e.g. `"payments.models.PaymentRecord"`.
    pub fqn: String,

    /// tree-sitter grammar node type, e.g. `"class_definition"`,
    /// `"function_definition"`, `"decorated_definition"`.
    pub node_type: String,

    /// First line of the node in the source file (1-based, inclusive).
    pub line_start: usize,

    /// Last line of the node in the source file (1-based, inclusive).
    pub line_end: usize,

    /// `.exv` cluster this node belongs to (e.g. `"payments"`).
    pub cluster: String,
}

/// A topology graph extracted from one source file.
///
/// `nodes` is a flat list of top-level entities (classes, functions, etc.).
/// `edges` encodes directed relationships between nodes by index into `nodes`.
#[derive(Debug, Clone, Default)]
pub struct CodeTopology {
    pub nodes: Vec<SpatialNode>,

    /// Directed edges as `(from_index, to_index)` pairs into `nodes`.
    /// Semantics follow the `.exv` edge vocabulary (call, import, inherit, …).
    pub edges: Vec<(usize, usize)>,
}

/// Parse a Python source file into a [`CodeTopology`].
///
/// **Current status:** stub — always returns an empty topology.
/// Phase 3b will replace this with a real tree-sitter walk.
///
/// # Arguments
/// * `_source` — UTF-8 source text of the Python file.
pub fn parse_python_file(_source: &str) -> CodeTopology {
    // TODO(phase-3b): instantiate a tree_sitter::Parser with the Python
    // grammar and walk the resulting syntax tree to populate CodeTopology.
    CodeTopology::default()
}
