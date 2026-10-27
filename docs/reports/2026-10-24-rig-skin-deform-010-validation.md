# BlenderBoo 0.10.0 rig, skin and goal-fitting validation (2026-10-24)

The Owner requested the rig-export and skinning follow-up, then added
goal-driven deformation and contour fitting. This release changes the toolkit.
Production Alpha4/monster assets and the UE project were read-only; the
Alpha4 head acceptance run copied its `.blend` to a temporary directory.

## Correctness repairs and capabilities

- `armature.export_ue_fbx` shares one node/axis/scale/deform-only path for a
  bound mesh and selected Actions with per-action FPS. Auto or explicit root
  modes state which bone UE receives as root. Supported staged FBX positions,
  bind matrices and linear translation curves are normalized to literal
  centimetres with unit metadata 1; root node scale remains near 1.
  Serialization and Blender reimport receipts check hierarchy, scale, mesh
  bounds, Action timing and sampled motion. A failing candidate is not
  published. Unsupported shape geometry and positional FBX properties refuse.
- `skin.audit` reports bounded per-bone opposite-side weight bleed, far spill,
  evaluated edge p99/max/min and added nonadjacent self-intersection pairs
  against rest for explicit pose/Action samples. Incomplete scans say unknown;
  threshold flags are advisory, not an asset pass/fail gate.
- `mesh.goal_deform` provides landmark warp, region relax, normal offset,
  crease and seam smoothing, sharing mask, pin, symmetry, displacement and
  clearance preflight. `mesh.fit_contours` uses the same validator for two
  perpendicular orthographic front/side silhouettes and reports before/after
  contour error and IoU. Both default to dry run and refuse conflicting
  constraints before mutation; unselected vertices stay unchanged.
- `scene.save_current(confirm=true, expected_state=...)` now applies even
  when disk identity changed since load, while explicit `dry_run=true` still
  plans. `render.detail` adds a light-independent Workbench `solid` mode and a
  warning for `lit` with no render-visible Light objects. The default MCP
  response summarizes large successful `python.exec` change lists and keeps
  the complete list in a retrievable artifact. Windowless FBX import-context
  errors identify `interchange.import` as the typed route.
- `animation.action_retarget` was reviewed: it copies/remaps F-curves and
  does not compensate different rest poses, axes, units or chain lengths. Its
  public description now states those limits. One unreproduced render cancel
  was not converted into an automatic retry.

## Measured acceptance

- Blender 5.2.3 LTS synthetic FBX fixtures passed both root conventions,
  old-mode scale rejection, deform-only ancestry, 30/120 FPS Action timing,
  source-context restoration and destination preservation on failure.
- A disposable UE **5.8.3** project imported a synthetic mesh and animation
  with standard `FbxFactory` defaults. `AnimPoseExtensions` measured root
  scale approximately **1**, pelvis reference height **79.999995 cm**, and
  pelvis animation height **87.499995 cm** at 0.5 s and **95.000002 cm** at
  1 s. The first candidate, before literal-centimetre normalization, had
  scale 1 but pelvis height **0.8 cm** under those defaults; that failure
  motivated the staged FBX correction. Receipt:
  `2026-10-24-rig-ue-058-validation.json`.
- In the synthetic skin fixture, targeted cleanup reduced opposite-side bleed
  and far-spill fractions from **0.24 to 0**, while posed maximum edge stretch
  fell from **4.36 to 1.0**. Quaternion Action sampling and state restoration
  passed.
- The Alpha4 head test copied a 677 MB production `.blend` to Temp and fitted
  **fresh scaled Quinn topology toward the aligned A02 head**. Across all
  5,580 original head samples, median distance fell from **3.2286 to
  0.0748 mm**. Explicit strength 0.98 and 695 reported collision pins yielded
  **zero new self-intersection pairs** (existing pairs 563 to 547). Unselected
  vertices were byte-identical. Receipt:
  `2026-10-24-goal-deform-head-fit.json`.
- The contour fixture passed two-view improvement, dry-run restoration,
  selected-region/pin preservation, symmetry and constraint refusals, mask
  cleanup, final measured receipts and non-unit scene scale.
- Clean integration source: **389 add-on Python tests**, **152 MCP Python
  tests**, and all seven affected Blender 5.2.3 headless fixtures exited 0.
  Actual add-on/MCP schema audit found **190 shared tools, zero errors and
  zero warnings**. Production fit and UE commandlet were not repeated after
  their passing runs because no relevant source changed.
- The installable `dist/blenderboo-0.10.0.zip` was extracted into a fresh
  disposable add-on directory. A new Blender 5.2.3 process loaded that package
  and the isolated 0.10.0 MCP source. `system.doctor` was healthy; schema audit
  found **190 tools, zero errors and zero warnings**. Fourteen genuine MCP
  bridge calls covered execution, refusal/analysis receipts, quality gate,
  slab, UV, named join and clipped render; the call-log token-redaction check
  passed. Receipts: `2026-10-24-blenderboo-010-isolated-addon-smoke.json`
  and `2026-10-24-blenderboo-010-isolated-adapter-smoke.json`.

## Limits and release state

UE evidence is a disposable synthetic rig; Alpha4 and monster production
skeletal assets were not imported again. Already-imported UE skeletons with
the old ×100 root require a fresh skeleton import. The FBX normalizer refuses
unsupported positional content. Skin thresholds need asset-specific review;
low spill can still hide joint cracks. Goal fitting does not certify anatomy
or automatically reject all new self-intersections in every call; the Alpha4
acceptance runner measured them explicitly. Contour fitting supports two
row-convex orthographic silhouettes sharing a vertical axis, not hidden
concavities or perspective views. A failed or disconnected live mutation must
be inspected before retrying.

This task's release was validated in an isolated integration worktree because
another active MetaHuman task had uncommitted changes to the canonical
dispatcher, MCP server and README. Those unrelated changes were excluded from
this source, tests and release commit. The global user-installed add-on and
Codex/Claude MCP registrations were not changed during the concurrent work;
their desktop-client qualification remains a separate step after the shared
checkout/configuration is reconciled. The temporary ZIP qualification above
does not imply the currently running desktop clients loaded 0.10.0.
