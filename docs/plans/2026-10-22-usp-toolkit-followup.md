# USP production toolkit follow-up (2026-10-22)

The Owner asked to implement and verify all requested toolkit improvements in
`docs/reports/2026-10-21-usp-production-findings.md`. Production USP and Helmet
assets are read-only fixtures; no delivered `.blend`, FBX, GLB, or texture is
written by this task. Preserve unrelated dirty and untracked repository work.

## Contracts to settle before integration

- `export.game_asset`: add `pivot=OBJECT_ORIGIN` preserving each exported
  object's own pivot, with explicit manifest coordinates; permit controlled
  exact-file replacement through `overwrite=true` in an existing directory.
  Never delete unrelated files. Existing `ORIGIN` and `BOUNDS_CENTER` behavior
  remains supported.
- `render.detail`: allow explicit `bounds` as framing while `object_names`
  selects isolation subjects. `render.ortho_set` may combine a directory with
  a basename prefix; final paths remain deterministic and overwrite-guarded.
  Document that isolation uses render visibility, not `hide_set`.
- `bake.from_ai_source`: optional fixed per-face-part channel fills, overriding
  source projection only on those owned UV texels; separately count filled,
  ray-hit, nearest-fallback and unresolved texels. Per-part ray misses and a
  cautiously named low-part-occlusion diagnostic must not imply unmeasured
  source detail.
- `uv.audit`: multi-object atlas audit with self and cross-object overlap
  counts. Seam tools use face-region boundaries and bounded accumulated turn.
  `mesh.join` names its inputs and does not depend on selection or visibility.
- `mesh.build_slab` and pocket capability: bounded all-quad planar slab with
  specified opening/pocket behavior, explicit unsupported cases and temporary
  headless proof. Do not silently promise a general boolean for arbitrary
  concave outlines.
- `python.module_put`: test persistence and cache invalidation across a file
  switch. The result/documentation must say whether a module is file-local.
- Sidecar collision guidance: detect an explicitly modeled cavity or report
  that cavity detection is unavailable; avoid claiming a single convex hull
  preserves an opening.

Root owns shared declarations in `addon/blenderboo/dispatcher.py`,
`mcp_server/src/blenderboo_mcp/server.py`, shared docs, integration, deployment,
and Git release. Scope workers own separate worktrees and implementation/test
files only. A verifier runs final test gates after integration. Deliverable is
observable behavior and evidence, not a task checklist marked complete.
