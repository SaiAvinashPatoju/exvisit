//! pest-backed parser. TODO: implement the tree walk that lowers Pairs -> exvisitDoc.
//! The Python reference (`exvisit_pro/exvisit/parser.py`) is the authoritative algorithm.

use crate::ast::*;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum ParseError {
    #[error("grammar violation: {0}")]
    Grammar(String),
    #[error("semantic: {0}")]
    Semantic(String),
}

#[derive(pest_derive::Parser)]
#[grammar = "exvisit.pest"]
pub struct exvisitGrammar;

pub fn parse(_src: &str) -> Result<exvisitDoc, ParseError> {
    // Scaffolding only — port the Python reference recursive walk here.
    // Tests in `tests/parser_tests.rs` (TODO) will mirror Python's test_exvisit.py.
    Err(ParseError::Grammar("not yet implemented; use Python reference".into()))
}

