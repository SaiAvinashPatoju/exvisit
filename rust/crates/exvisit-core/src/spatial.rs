//! Spatial index — rstar-backed RTree over world coordinates.
//! `world(x, y, depth) = (x * 2^depth, ln(2^depth))` per vision.

pub fn world_coords(x: f64, y: f64, depth: u32) -> (f64, f64) {
    let z = 2f64.powi(depth.max(1) as i32);
    (x * z, z.ln())
}

#[derive(Default)]
pub struct RTree {
    // stub; production: rstar::RTree<NodeEntry>
}
