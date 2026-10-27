# BlenderBoo 0.8.1 repair handoff (2026-10-20)

Status for Claude: the Owner asked Codex to fix the R4 bake, oversized source
comparison response, and quality-gate pole output, then deploy and push.

- A live `bake.from_ai_source` call now queues a background snapshot job and
  returns `operation_id`; use `operation.status` for progress and final paths.
  The bake child uses `--factory-startup` so an older enabled add-on cannot
  shadow the source checkout. The installed add-on and editable MCP adapter
  both report 0.8.1.
- Ray-missed non-shell material texels try a distance-bounded nearest high
  surface sample from the source-UV cache. `fallback_max_distance` defaults to
  `cage_extrusion + max_ray_distance` (capped at 1000 scene units) and can be
  set explicitly. The result separates ray misses, fallback hits and remaining
  colour misses; `FallbackHitMask` is written. Missing high normals stay flat
  low-poly tangent-space normals and remain counted as missing high detail.
  A nearest sample can cross a close neighbouring part, so inspect the mask
  and rendered texture before asset acceptance.
- `asset.compare_to_source` omits `radial.delta_scene_units` by default. Set
  `include_radial_grid=true` for an inline grid, or provide `output_dir` to
  receive `radial_delta.json`. `asset.quality_gate` keeps full `pole_count` and
  returns at most 20 pole examples, plus `poles_truncated`.
- Verification: 84 focused unit tests and headless Blender tests passed. A
  real background child produced 1,024 fallback hits with zero remaining
  misses. A fresh installed Blender 5.2.2 plus the configured MCP adapter
  passed `system.doctor` and schema audit (184 tools, zero errors/warnings).
  The global Codex and Claude MCP entries point to `<checkout>`.
- Helmet production acceptance is still open. A corrected-unit 2K replay
  stopped at its declared 600-second limit during nearest projection, before
  producing coverage metrics. The original high and low `.blend` hashes were
  unchanged. Do not claim the `<0.1%` colour-miss target is met from the
  synthetic test. Full parameters and hashes are in
  `docs/reports/2026-10-20-r4-fallback-timeout.json`; installed smoke receipts
  are alongside it in `docs/reports/`.

This is a status handoff, with no requested changes to the Helmet asset.

Later on 2026-10-20, a supplied Helmet 0.8.1 MCP replay reported 0.0024%
colour misses and recovered the historical max ray distance. The Helmet
acceptance status above was accurate at this handoff time; use
`docs/reports/2026-10-20-helmet-r4-081-replay.md` and the subsequent
`docs/reports/2026-10-20-r4-background-082-followup.md` for the newer state.
