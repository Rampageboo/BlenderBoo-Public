# Requirements: Game-Asset Retopology From AI Sources

Status: requirements baseline, 2026-10-20. Tooling is implemented in v0.7.4;
production acceptance and remaining limitations are tracked in
`game-asset-retopo-validation.md`. The original proposed names and shapes are
retained here as the task brief; source code defines the shipped contracts.
Source: the Flashlight / Tent / Helmet retopology work in
`<private-project>/ModelAssets` (Oct 2026), which ran entirely as hand-written
`blender -b --python` scripts because this MCP was not loaded in that project
(see "Rollout"). Those scripts are kept in
`docs/reference/game-asset-retopo-2026-10/` as working reference
implementations; they are single-use, hard-coded, and not production code.

## Why

Turning AI-generated high-polys (Tripo, Hunyuan) into game assets took many
rework rounds. The Owner rejected results for being soft, for broken edge
loops, for interpenetration, for stuck-on parts, and twice for wrong shape.
Root causes, in order of cost:

1. **Checks were too weak.** Full-view renders hid shape loss, and a helmet
   whose form was wrong was reported as "passed". The checks that finally
   caught problems were numeric: face-pair self-intersection, silhouette IoU
   from four orthographic views against the source, radial deviation maps,
   and texel-density / UV-overlap audits.
2. **Every modeling primitive was rebuilt per asset.** Profile lathes,
   sweeps with per-point width and chamfer, quad plates from outlines, and
   "lift a part until it clears the shell" were written three times.
3. **Baking from AI sources needs a different recipe.** Once the low is a
   cleaned, fair shape, it intentionally differs from the noisy high, and
   ray-cast selected-to-active bakes leave black bands where the rays miss.
   The working recipe combined nearest-surface transfer, ray bake, high-pass
   normal filtering, and self-baked AO.
4. **The UE side was unscripted.** LOD groups, collision mode, ORM material
   wiring, and detail normals were all added by hand in UE Python.

## Existing coverage (verified in source, 2026-10-20)

Do not rebuild these; extend them.

| Need | Existing tool | Gap |
|---|---|---|
| Face counts, poles, symmetry, degenerate/tiny faces, boundary and non-manifold edges, loop traces | `mesh.topology_report` | No face-pair self-intersection; no angle-based sliver test; no pass/fail against a stated quality bar |
| Part-to-part contact and penetration | `assembly.analyze` | Works between objects; parts that live inside one mesh (separated only by a face attribute) are not covered |
| Silhouette vs a reference image | `render.contour_compare` | Compares against an image, not against a source mesh from the same ortho cameras |
| Distance to a point cloud | `mesh.compare_to_points` | Needs a point cloud; no direct mesh-to-mesh deviation and no radial/spherical map |
| Texel density spread | `uv.inspect` (`texel_density_min/max/spread`) | Spread only; no absolute px/cm at a given texture size and real scale; no UV overlap measure |
| Low-level edits including `spin`, `bevel`, `bridge`, `grid_fill` | `mesh.edit` | No higher-level hard-surface constructors (see R3) |
| Unwrap | `uv.unwrap` (`ANGLE_BASED`, `CONFORMAL`, `SMART`) | No `MINIMUM_STRETCH` (SLIM), which domes needed |
| Cycles bake with cage | `bake.run` | No nearest-surface transfer, no per-part routing, no high-pass, no channel packing, no DirectX normal output |
| Static mesh export | `interchange.export` | No game-export preset (triangulate, real scale, pivot, naming, texture set) |

## Decided

- Tools return **numbers and pass/fail first**, images second. An agent must
  be able to decide "done" without the Owner reading a picture, for example
  `self_intersections: 0`, `iou_min: 0.97`, `deviation_p99_mm: 2.1`.
- Checks are read-only and never block a mutation. Gating is the caller's job.
- The Owner's acceptance bar (below) lives in `skills/`, not in tool
  descriptions, per `AGENTS.md`. Tools expose thresholds as parameters.
- Everything works headless (`blender -b`), because large bakes and audits
  run in the background.

## Owner's acceptance bar (goes into `skills/blender-asset-validation`)

- All-quad source; triangulate only at export.
- Alt-click edge loops run through except at deliberate breaks.
- No slivers, no zero-area faces, no interpenetration (inside one mesh or
  between parts).
- Hard edges stay crisp; nothing "soft" (no voxel/QuadriFlow look).
- Symmetric parts are consistent; parts are integrated, not stuck on.
- Clean designed form: fair arcs and proper hard-surface rails/slots, not a
  copy of the noisy AI surface.
- Before building: analyse structure (slices, ray maps) and state the budget
  (tris, texture size, usage distance).

## Requirements

Priority: P0 = would have prevented most rework; P3 = convenience.

### R1 (P0) `asset.quality_gate`: one-call topology gate

Input: `object_name`; optional `part_attribute` (face int attribute naming
parts); thresholds `sliver_min_edge`, `sliver_min_angle_deg`,
`triangle_budget`, `require_quads` (default true), `symmetry_plane_x`.

Output:

- Everything `mesh.topology_report` returns, plus:
- `self_intersections`: count and up to N world positions of intersecting
  non-adjacent face pairs (BVH overlap, excluding faces that share a vertex).
  Reported both inside a part and between parts.
- `slivers`: faces under the edge-length or minimum-angle threshold.
- `loop_continuity`: share of edge loops that run unbroken, with the
  positions of loops that terminate at a non-4-valent vertex away from a
  boundary or a marked-sharp edge.
- `pass`, plus `failures[]` with a reason and positions for each.

Reference: `qa.py` (topology, slivers, self-intersections, open edges,
deviation).

### R2 (P0) `asset.compare_to_source`: shape fidelity against the AI high

Input: `low`, `high`; `views` (default front/side/top/back); `resolution`;
optional `center` and `spherical: true` for domes.

Output:

- Per-view silhouette IoU from identical orthographic cameras, and
  `iou_min`, plus mask PNGs and difference PNGs.
- Mesh-to-mesh deviation (low surface sampled to high surface):
  median / p90 / p99 / max, in scene units and millimetres at real scale.
- With `spherical`: a radial-difference map (θ × φ grid, ray-cast from
  `center`) and the regions where |Δr| exceeds a threshold. This is what
  caught "the helmet shape is wrong".

References: `silhouette.py`, `radial_diff.py`, `helmet_map2.py`.

### R3 (P1) Hard-surface constructors (`mesh.build_*`)

Each produces an all-quad, manifold result with continuous loops, and a face
attribute that names the part.

- `mesh.build_lathe`: a 2D profile (points + per-point sharp flag + optional
  round radius) revolved N segments. Rounds are profile fillets, not
  post-hoc bevels; the Owner rejected blind bevels that clipped and broke
  loops.
- `mesh.build_sweep`: a 3D path with per-point width, height, and chamfer;
  open or closed; mitres that do not self-intersect.
- `mesh.build_plate`: a quad grid inside a 2D outline projected onto a target
  surface; boundary vertices snapped to the outline; thickness; optional open
  back.
- `mesh.clear_of`: lift part faces along the target normal, iterating until
  the BVH overlap with the target is 0 and the gap is within a tolerance; then
  optionally delete faces hidden against the target.
- `mesh.fair_fit` (analysis + build): a robust low-order fit to a noisy
  shell (cos(mθ)·sin(φ)^k basis, mirror symmetry, outlier rejection), so the
  shell is a fair arc rather than a copy of the AI surface. It returns a
  residual map so features can be located for R3 parts.

References: `fair_fit.py`, `plates.py`, `helmet_data.py`, `build_helmet.py`,
`build_tent2.py`.

### R4 (P1) `bake.from_ai_source`: AI-high to cleaned-low texture set

Input: `low`, `high`, `resolution`, optional `part_attribute`,
`shell_parts` (which part ids use nearest transfer), `cage_extrusion`,
`max_ray_distance`, `highpass_sigma_px`, `normal_convention` (`OPENGL` |
`DIRECTX`), `pack` (e.g. `ORM`).

Behavior:

- Base colour and metallic baked as Emission; a Diffuse bake darkens metal.
- Shell parts: nearest-surface texture transfer, so there are no ray misses.
- Other parts: ray bake with cage.
- Composite by part mask, then dilate.
- Normal: ray bake, then high-pass filter so AI lumps do not come back; write
  OpenGL and/or DirectX.
- AO: self-baked on the low only. The AI high's AO is noise.
- Pack ORM (R = AO, G = roughness, B = metallic).

Output: file paths, plus coverage and miss statistics. `black_texels_pct`
inside UV islands is the number that caught the black-band defect.

References: `bake_asset.py`, `nearest_transfer.py`, `highpass_normal.py`,
`bake_ao_self.py`, `bake_test.py` (miss-rate probe over cage/distance pairs).

### R5 (P1) `uv.audit` and `uv.unwrap` `MINIMUM_STRETCH`

- `uv.audit`:
  - `px_per_cm` (p5 / median / p95), from texture size and real-world scale.
  - `overlap_pct`, measured by rasterised coverage.
  - Islands below a density floor.
  - A target check, for example ≥ 20 px/cm for a mid-distance prop.
- `uv.unwrap`: add `MINIMUM_STRETCH` (SLIM, Blender 4.3+), plus
  average-island-scale before pack.

References: `audit.py`, `uv_seams_slim.py`.

### R6 (P2) `export.game_asset`: export preset plus UE hand-off

- Triangulate at export only (the source stays quads); apply real scale; set
  the pivot; apply naming (`SM_`, `T_<Name>_{BaseColor,NormalDX,ORM}`); copy
  the textures to a staging folder.
- Write a sidecar JSON for the UE importer:
  - `lod_group`.
  - `collision`: `auto_convex` | `complex_as_simple` | `ucx`.
  - `import_yaw`.
  - Detail normal and tiling.
- UE 5.8 constraint: FBX import goes through Interchange, which ignores the
  `UCX_` hulls, and a replace-import keeps the old body setup. Until that is
  solved, walk-in static props use `complex_as_simple`.
- The UE side belongs to the separate `unreal` MCP or to UE Python. The
  reference is `import_static_prop.py` and `verify_static_props.py`.

### R7 (P3) Same-camera review renders

Clay, textured, and wire renders of low and high from identical cameras, with
optional close-ups at given points. `render.ortho_set`, `render.detail`, and
`render.compare` probably cover most of this; confirm before building
anything new. Reference: `views.py`.

## Acceptance

Replay the three Oct 2026 assets through the new tools and reproduce the
known results.

- **R1:** flags the v1 helmet NVG mitre self-intersection and the flashlight
  boolean-mount slivers and n-gons. Passes the final versions.
- **R2:** flags the shape-stripped helmet (`_old_v1_shape`) by `iou_min` and
  radial difference. Passes the final helmet.
- **R4:** on the final helmet, `black_texels_pct` stays below 0.1% inside the
  islands, where the old ray-only bake had visible black bands under the
  rails.
- **R5:** reports the final figures:

  | Asset | Texel density | Texture |
  |---|---|---|
  | Helmet | about 27.5 px/cm | 2K |
  | Tent | about 2.8 px/cm | 4K |
  | Flashlight | about 70 px/cm | 2K |

  UV overlap is below 0.01%.
- **All tools:** headless runs succeed. `adapter.schema_audit` stays clean.

Source asset paths and filenames are omitted from this public snapshot.

## Rollout

The former project-only MCP entry used
`<legacy-checkout>`, which no longer exists. Codex and Claude
now each have a user-global `codex-blender` registration pointing to
`<checkout>/mcp_server/src`. The Python editable adapter
installation also points to the current checkout. Restart both clients after
registration changes. Keep `CBT_BRIDGE_TOKEN` out of committed files (see
`AGENTS.md`).

## Out of scope

- Automatic retopology (QuadriFlow or voxel remesh) as the main path. The
  Owner rejected its soft results; the constructors in R3 are the path.
- Rigging and skinned assets; the creature tools cover those.
