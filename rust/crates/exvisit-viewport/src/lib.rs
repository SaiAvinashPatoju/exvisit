//! exvisit-viewport — wgpu-driven "dumb canvas" (Phase 5 scaffold).
//!
//! Pipeline:
//!   vertex: full-screen triangle
//!   fragment: samples a storage-buffer of [x,y,w,h] rects + infinite grid
//!   camera: 4x4 orthographic; pan/zoom via mouse deltas piped from JS
//!
//! Inputs (via wasm-bindgen):
//!   set_rects(&[f32])      — flat array of (x,y,w,h) quads already frustum-culled backend-side
//!   on_mouse(dx,dy,scroll) — update camera matrix

pub const WGSL_SHADER: &str = include_str!("grid.wgsl");

