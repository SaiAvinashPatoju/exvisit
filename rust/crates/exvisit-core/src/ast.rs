//! Abstract Syntax Tree — mirrors exvisit_pro/exvisit/ast.py.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum EdgeKind {
    /// `->` static import / direct call dependency. Enforced by `exvisit-lsp`.
    Sync,
    /// `~>` runtime wire (event / callback / signal). Informational only.
    Async,
}

/// Local bounds within the parent namespace. `(x, y, w, h)` in arbitrary units.
pub type Bounds = (i32, i32, i32, i32);

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Node {
    pub name: String,
    pub bounds: Bounds,
    pub src_path: Option<String>,
    pub states: Vec<String>,
    pub ns_path: String,
}

impl Node {
    pub fn fqn(&self) -> String {
        if self.ns_path.is_empty() { self.name.clone() }
        else { format!("{}.{}", self.ns_path, self.name) }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Edge {
    pub src: String,
    pub dst: String,
    pub kind: EdgeKind,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Namespace {
    pub level: u8,
    pub name: String,
    pub bounds: Bounds,
    pub src_glob: Option<String>,
    pub children: Vec<Namespace>,
    pub nodes: Vec<Node>,
    pub path: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct exvisitDoc {
    pub root: Namespace,
    pub edges: Vec<Edge>,
}

