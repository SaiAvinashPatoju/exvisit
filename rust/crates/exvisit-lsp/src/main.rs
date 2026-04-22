//! exvisit-lsp — Language Server enforcing spatial-topology constraints (Phase 4).
//!
//! Algorithm (port of exvisit_pro/exvisit/verify.py):
//!   on textDocument/didChange:
//!     1. Parse local imports.
//!     2. Map each import to an exvisitGraph node via src_path suffix match.
//!     3. For each real import not declared as `->` in the exvisit,
//!        publish Diagnostic { severity: Error, code: "exvisit001", message: "Spatial Constraint Violation" }.

fn main() {
    eprintln!("exvisit-lsp scaffold — implement over tower-lsp once exvisit-core is live.");
}

