//! Project exvisit — Rust production implementation (Phases 1-2).
//!
//! Public surface:
//!   - `parser::parse` — pest-driven PEG parser producing an `ast::exvisitDoc`.
//!   - `crdt::exvisitGraph` — Delta-CRDT memory engine (OR-Set / LWW-Map / 2P-Set).
//!   - `spatial::RTree` — rstar-backed spatial index over world coords (u = x·z, v = ln z).
//!
//! The Python reference impl in `exvisit_pro/exvisit/*.py` is the source of truth for
//! semantics; this crate ports it for performance, memory-safety, and LSP/WASM use.

pub mod ast;
pub mod parser;
pub mod crdt;
pub mod spatial;
pub mod serialize;

