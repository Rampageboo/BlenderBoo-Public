# BlenderBoo 0.9.0 USP toolkit follow-up (2026-10-22)

The Owner requested implementation and verification of the ten toolkit findings in
the local `2026-10-21-usp-production-findings.md`. This release changes toolkit
behavior; it does not edit or re-export the delivered USP, Helmet or Flashlight
assets.

## Implemented behavior

1. `export.game_asset` adds `pivot=OBJECT_ORIGIN`. Local mesh coordinates and
   the source object's pivot survive FBX reimport while relative placement is
   retained. The manifest records each source pivot.
2. `export.game_asset(overwrite=true)` can publish into an existing directory.
   It stages the exact files this call writes, restores prior outputs on a
   failed publish, and leaves unrelated files alone. `auto_convex` results and
   the sidecar warn that a single convex hull can fill a cavity.
3. `render.detail` accepts explicit framing `bounds` together with named
   isolation subjects. Isolation uses render visibility, not viewport
   `hide_set`.
4. `render.ortho_set` accepts `output_dir` and a basename prefix together;
   deterministic file paths keep the existing overwrite guard.
5. `bake.from_ai_source` reports per-part UV ownership, ray misses and
   unresolved samples. A bounded normal-ray diagnostic marks *potential*
   obstruction by another low part, without claiming to reproduce Cycles'
   exact source-ray path.
6. `mesh.build_slab` constructs an all-quad solid slab, through opening or
   blind pocket from matched planar loops, with FACE INT `fregion` labels.
   Unsupported or folded input refuses before scene mutation. This is a
   bounded constructor, not an arbitrary mesh Boolean.
7. `bake.from_ai_source` accepts `fill_face_attribute` and fixed `fill_parts`
   channels (linear base colour, roughness, metallic, AO and flat normal).
   Owned texels bypass source transfer and are counted separately from misses.
8. `uv.audit` covers one atlas shared by several objects and reports self and
   cross-object overlap plus bounded face-pair evidence. Region-boundary and
   accumulated-turn seam helpers provide dry-run plans before application.
   Turn cuts require explicit ordered edge rows.
9. `mesh.join` adds a named, selection-independent route that creates a new
   world-space mesh and preserves the source objects, UVs, supported FACE
   attributes, materials, sharp edges and seams. The earlier target/source
   route remains available. The named route refuses unsupported data before
   mutation; Blender's internal UV selection attributes do not block a join.
10. `python.module_put` was verified across a `.blend` switch: saved helper
    source returns with its own file, while a different file exposes its own
    helper set. A path input is copied into the document once, not linked.

The background render and bake descriptions now say the snapshot is made when
the queued job **starts**. The operation-ID response alone does not establish
that a snapshot already exists.

## Verification

- Source tests: 133 Python tests passed. After the final response-profile
  change, 80 focused MCP/server/schema tests passed. Blender 5.2.2 headless
  fixtures passed for bake, slab/quad topology, source comparison, atlas/seams,
  named join, R2 game-asset and R3 workflows. A final named-join regression
  passed with Blender primitive planes carrying internal UV selection data.
- FBX reimport checked 12 pivot cases across all three pivot modes and scene
  scales 1 and 0.01. Exact-file overwrite rollback and source preservation
  passed. Module file-switch fixture passed A11 → B22 → A11.
- Fresh installed smoke exited 0 with `INSTALLED_DEPLOYMENT_OK`. A separate
  Blender 5.2.2 process loaded the user-installed add-on 0.9.0 and called the
  configured editable MCP adapter 0.9.0. `system.doctor` was healthy;
  `adapter.schema_audit` found **187 tools, zero errors and zero warnings**.
  Real MCP calls covered the slab pocket, 16 planned region seams, 4032
  cross-object overlap pixels, named join preserving sources, and earlier R3
  refusal/analysis and quality-gate paths. The call log's token redaction check
  passed. Receipts: `2026-10-22-blenderboo-090-addon-smoke.json` and
  `2026-10-22-blenderboo-090-adapter-smoke.json`.
- All 73 installed add-on `.py`/`.json` files matched the source by SHA-256.
  The global editable `blenderboo-mcp` installation reports 0.9.0 at
  `<checkout>/mcp_server`. The installable ZIP is
  `dist/blenderboo-0.9.0.zip`. `git diff --check` exited 0.

## Qualification boundary

The tests use disposable scenes and FBX reimports. This release has not
re-baked the production USP or verified Unreal import, VR magazine clearance,
or a live desktop client after restart. Source-ray occlusion classification is
a bounded diagnostic, and the slab constructor does not replace the USP's
custom curved Coons frame builder. Restart running Blender, Codex and Claude
processes to load the installed add-on and updated MCP declarations.
