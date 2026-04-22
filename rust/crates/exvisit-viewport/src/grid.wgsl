// Phase 5 — infinite grid + rect renderer fragment shader.
// Screen-space; no DOM, no CSS.

struct Camera { view_proj: mat4x4<f32>, zoom: f32, pan: vec2<f32>, _pad: f32 };
@group(0) @binding(0) var<uniform> cam: Camera;

struct Rect { x: f32, y: f32, w: f32, h: f32 };
@group(0) @binding(1) var<storage, read> rects: array<Rect>;

@vertex
fn vs_main(@builtin(vertex_index) vi: u32) -> @builtin(position) vec4<f32> {
    // full-screen triangle
    var p = array<vec2<f32>, 3>(vec2(-1.0,-1.0), vec2(3.0,-1.0), vec2(-1.0,3.0));
    return vec4(p[vi], 0.0, 1.0);
}

@fragment
fn fs_main(@builtin(position) pos: vec4<f32>) -> @location(0) vec4<f32> {
    // translate screen pos -> world pos via cam
    let world = pos.xy / cam.zoom + cam.pan;

    // infinite grid lines
    let g = abs(fract(world) - 0.5);
    let line = smoothstep(0.48, 0.5, max(g.x, g.y));
    let grid = vec3<f32>(0.12, 0.12, 0.15) * (1.0 - line) + vec3<f32>(0.2, 0.22, 0.3) * line;

    // rect hit test
    var color = grid;
    let n = arrayLength(&rects);
    for (var i: u32 = 0u; i < n; i = i + 1u) {
        let r = rects[i];
        if (world.x >= r.x && world.x <= r.x + r.w && world.y >= r.y && world.y <= r.y + r.h) {
            color = mix(color, vec3<f32>(0.85, 0.7, 0.35), 0.35);
        }
    }
    return vec4(color, 1.0);
}
