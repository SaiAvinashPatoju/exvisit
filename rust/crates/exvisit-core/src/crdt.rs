//! Delta-CRDT memory engine (scaffold).
//!
//! Port target: `exvisit_pro/exvisit/crdt.py`.
//! - ORSet     -> namespaces
//! - LWWMap    -> nodes
//! - TwoPSet   -> edges
//!
//! Implementation note: use `rstar` RTree in `spatial.rs` for geometric queries.

use crate::ast::*;
use std::collections::{HashMap, HashSet};

#[derive(Default, Debug, Clone)]
pub struct exvisitGraph {
    pub namespaces: HashSet<String>,
    pub nodes: HashMap<String, Node>,
    pub edges: HashSet<(String, String, EdgeKind)>,
}

impl exvisitGraph {
    pub fn from_doc(_doc: &exvisitDoc) -> Self { Self::default() }
    pub fn merge(self, _other: Self) -> Self { self }
}

