//! exvisit-query — microscopic topological extraction (Phase 3).
//! Port target: `exvisit_pro/exvisit/query.py`. Scaffolding only.

use clap::Parser;

#[derive(Parser)]
#[command(name = "exvisit-query", version, about = "extract a topological slice from a .exv file")]
struct Args {
    /// path to .exv file
    file: String,
    /// target node (bare name or dotted FQN)
    #[arg(long)]
    target: String,
    /// topological hops to include
    #[arg(long, default_value_t = 1)]
    neighbors: usize,
    /// direction: in | out | both
    #[arg(long, default_value = "both")]
    direction: String,
}

fn main() -> anyhow::Result<()> {
    let args = Args::parse();
    eprintln!("exvisit-query scaffolding — use Python reference (`python -m exvisit query ...`) for now");
    eprintln!("  file={} target={} neighbors={} direction={}",
              args.file, args.target, args.neighbors, args.direction);
    Ok(())
}

