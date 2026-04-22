//! exvisit-mcp — Model Context Protocol architect server (Phase 3 scaffold).
//!
//! Exposes JSON-RPC methods:
//!   - exvisit.query            { target, hops, direction } -> string
//!   - exvisit.applyCanvasDelta { node_id, new_bounds }     -> refactor_plan
//!   - exvisit.refactorPlan     { before, after }           -> [ { move, update_imports }, ... ]

fn main() {
    eprintln!("exvisit-mcp scaffold — implement once exvisit-core::parse is ported.");
}

