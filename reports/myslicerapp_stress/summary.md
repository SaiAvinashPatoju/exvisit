# MySlicerApp Stress Test

Repo: `C:\B\MySlicerApp`
Published exvisit atlas: `C:\projects\work\atlas_pro\reports\myslicerapp_stress\MySlicerApp.exv`
Cases: 7

## Strategy Averages

| Strategy | Avg tokens | Avg steps | Avg context rot | Oracle hit rate | Oracle hit@1 |
|---|---:|---:|---:|---:|---:|
| rg_semantic | 61486.3 | 6.00 | 0.00 | 100.00% | 100.00% |
| semantic_exvisit | 852.1 | 2.00 | 0.00 | 100.00% | 100.00% |
| rg_exvisit_semantic | 1328.9 | 3.00 | 0.00 | 100.00% | 100.00% |

## Pairwise Deltas

| Comparison | Context reduction | Step reduction | Context rot reduction |
|---|---:|---:|---:|
| rg_semantic__vs__semantic_exvisit | 98.6% | 66.7% | 0.0% |
| rg_semantic__vs__rg_exvisit_semantic | 97.8% | 50.0% | 0.0% |
| semantic_exvisit__vs__rg_exvisit_semantic | -55.9% | -50.0% | 0.0% |

## Per-Case Selections

### eraser_mesh_cache

The eraser tool gets sluggish on large meshes and seems to rebuild its spatial index even when the same mesh is reused. Find where the eraser caches KD-tree or locator state and clears it on deactivate.

Oracle: maxmorph/tools/eraser_tool.py

- rg_semantic: files=['maxmorph/tools/eraser_tool.py', 'maxmorph/app.py', 'maxmorph/tools/mesh_ops.py', 'tests/test_eraser.py'] steps=6 tokens=59421 rot=0
- semantic_exvisit: files=['maxmorph/tools/eraser_tool.py', 'maxmorph/app.py'] steps=2 tokens=724 rot=0
- rg_exvisit_semantic: files=['maxmorph/tools/eraser_tool.py', 'maxmorph/app.py', 'maxmorph/tools/mesh_ops.py', 'tests/test_eraser.py'] steps=3 tokens=1223 rot=0

### layout_contour_proxy

Quad layout contour overlays should skip proxy generation when there are no slice panes and should reuse a decimated proxy when panes stay active. Find the viewport layout code that manages contour proxy caching.

Oracle: maxmorph/viewport/layout_manager.py

- rg_semantic: files=['maxmorph/viewport/layout_manager.py', 'tests/test_layout_manager.py', 'maxmorph/viewport/slice_viewport.py', 'maxmorph/app.py'] steps=6 tokens=64418 rot=0
- semantic_exvisit: files=['maxmorph/viewport/layout_manager.py', 'maxmorph/viewport/slice_pane.py'] steps=2 tokens=425 rot=0
- rg_exvisit_semantic: files=['maxmorph/viewport/layout_manager.py', 'maxmorph/viewport/slice_pane.py', 'tests/test_layout_manager.py', 'maxmorph/viewport/slice_viewport.py'] steps=3 tokens=894 rot=0

### measurement_lifecycle

Distance measurements need a clean create update remove lifecycle with stored details and stable record lookup. Find the code that owns measurement records and mutations.

Oracle: maxmorph/core/measurements.py

- rg_semantic: files=['maxmorph/core/measurements.py', 'tests/test_measurements.py', 'maxmorph/app.py', 'maxmorph/ui/measurements_panel.py'] steps=6 tokens=53715 rot=0
- semantic_exvisit: files=['maxmorph/core/measurements.py', 'maxmorph/ui/measurements_panel.py'] steps=2 tokens=455 rot=0
- rg_exvisit_semantic: files=['maxmorph/core/measurements.py', 'maxmorph/ui/measurements_panel.py', 'tests/test_measurements.py', 'maxmorph/app.py'] steps=3 tokens=924 rot=0

### scissors_polygon_selection

Freeform scissors should draw a screen polygon, prefilter cells by bounding box, and then test points inside the polygon before deleting cells. Find where freeform scissors selection is implemented.

Oracle: maxmorph/tools/scissors_tool.py

- rg_semantic: files=['maxmorph/tools/scissors_tool.py', 'tests/test_scissors.py', 'tests/test_mesh_ops.py', 'maxmorph/app.py'] steps=6 tokens=56798 rot=0
- semantic_exvisit: files=['maxmorph/tools/scissors_tool.py', 'maxmorph/app.py'] steps=2 tokens=1121 rot=0
- rg_exvisit_semantic: files=['maxmorph/tools/scissors_tool.py', 'maxmorph/app.py', 'tests/test_scissors.py', 'tests/test_mesh_ops.py'] steps=3 tokens=1623 rot=0

### mesh_boolean_pipeline

Mesh processing needs robust boolean wrappers, decimation to a target face count, and mesh statistics for validation. Find the mesh utility module that owns those operations.

Oracle: maxmorph/tools/mesh_ops.py

- rg_semantic: files=['maxmorph/tools/mesh_ops.py', 'tests/test_mesh_ops.py', 'maxmorph/app.py', 'maxmorph/tools/splint_ops.py'] steps=6 tokens=65820 rot=0
- semantic_exvisit: files=['maxmorph/tools/mesh_ops.py', 'maxmorph/app.py'] steps=2 tokens=892 rot=0
- rg_exvisit_semantic: files=['maxmorph/tools/mesh_ops.py', 'maxmorph/app.py', 'tests/test_mesh_ops.py', 'maxmorph/tools/splint_ops.py'] steps=3 tokens=1383 rot=0

### splint_wafer_generation

Splint generation builds an occlusal wafer from signed distance fields and falls back to a slab if the field extraction is empty. Find the code that generates the wafer and the shared mesh operations it depends on.

Oracle: maxmorph/tools/splint_ops.py, maxmorph/tools/mesh_ops.py

- rg_semantic: files=['maxmorph/tools/splint_ops.py', 'maxmorph/app.py', 'maxmorph/tools/mesh_ops.py', 'tests/test_splint_ops.py'] steps=6 tokens=66014 rot=0
- semantic_exvisit: files=['maxmorph/tools/splint_ops.py', 'maxmorph/tools/mesh_ops.py'] steps=2 tokens=1819 rot=0
- rg_exvisit_semantic: files=['maxmorph/tools/splint_ops.py', 'maxmorph/tools/mesh_ops.py', 'maxmorph/app.py', 'tests/test_splint_ops.py'] steps=3 tokens=2304 rot=0

### transform_gizmo_commit

The transform gizmo must disable stale pickers before attaching the affine widget and only push undo when the matrix actually changes on release. Find the transform gizmo implementation.

Oracle: maxmorph/tools/transform_gizmo_tool.py

- rg_semantic: files=['maxmorph/tools/transform_gizmo_tool.py', 'maxmorph/app.py', 'tests/test_transform.py', 'qa/run_qa_suite.py'] steps=6 tokens=64218 rot=0
- semantic_exvisit: files=['maxmorph/tools/transform_gizmo_tool.py', 'maxmorph/app.py'] steps=2 tokens=529 rot=0
- rg_exvisit_semantic: files=['maxmorph/tools/transform_gizmo_tool.py', 'maxmorph/app.py', 'tests/test_transform.py', 'qa/run_qa_suite.py'] steps=3 tokens=951 rot=0
