# Helmet R4 Replay on 0.8.1 (2026-10-20)

Claude ran the replay through the live MCP: add-on and adapter 0.8.1,
`system.doctor` healthy. It follows up the 0.8.0 replay
(`codex_blender_toolkit/docs/reports/2026-10-20-helmet-replay-validation.md`)
and the Codex timeout in `2026-10-20-r4-fallback-timeout.json`.

## Setup

- **Frame: real scale.** The high was appended into the open
  `Helmet_game.blend` and its mesh was transformed by
  `p = 0.2857 · (p_high_world − (0, 0, 0.55))`. The low was left untouched.
  The high was removed afterwards and nothing was saved.
- **Parameters.** The original CLI was `bake_asset.py … 2048 Helmet 0.06 0.1`,
  so cage was 0.06 and max ray distance 0.1 in model units. Here both are
  scaled by 0.2857:
  - `cage_extrusion` 0.0171
  - `max_ray_distance` 0.0286
  - `fallback_max_distance` at its default of 0.0457
  - `highpass_sigma_px` 24 (the old script's value)
  - DirectX normals, ORM, `shell_parts` [0], 2K
- **Why the Codex run timed out.** The original max ray distance was 0.1, not
  "unavailable". The Codex run used 0, which means unlimited.

## Result

| Metric | 0.8.0 | 0.8.1 |
|---|---|---|
| `black_texels_pct` | 3.17% | **0.0024%** (51 texels) |
| `fallback_hit_texels` / candidates | — | 68,086 / 68,086 |
| `normal_miss_pct` (flat fallback) | 6.37% | 6.37% (unchanged by design) |
| Texels outside the fallback mask vs 0.8.0 | — | identical (mean abs diff 0.0) |
| Wall time | ~4.4 min (direct `blender -b`) | **43 min** (MCP background job) |

**Visual check.** The densest fallback cluster was compared against the
delivered texture, the 0.8.0 result, and the 0.8.1 result:

- The chin-cup inner area was empty or black in both earlier textures. In
  0.8.1 it is filled with the part's own dark grey.
- The adjacent strap takes its neighbours' tone.
- No colour bleeds in from a different part.

Across all fallback texels, 0.8.1 has fewer dark texels (luminance < 0.06)
than the delivered texture: 29.7% against 50.4%.

**Colour-coverage target (< 0.1%): met.**

## Open issues

1. **Background job is about 10× slower than a direct run.** CPU time at
   15 minutes was only 315 s, with about 0.3 core in use. The 0.8.0 direct
   run finished all the work in 262 s of wall time. The `_nearest_uv` loop is
   unchanged and handles only about 4% more texels (1.55M → 1.62M), so the
   process looks starved rather than busy. Possible causes, not yet tested:
   - Windows applies efficiency mode or low priority to `CREATE_NO_WINDOW`
     children.
   - Thread or Cycles defaults change under `--factory-startup`.

   To find out, time the same `job.json` once from a normal console and once
   through the bridge.
2. **The adapter schema for `bake.from_ai_source` lacks
   `fallback_max_distance`.** The add-on accepts it, but MCP clients cannot
   pass it.
3. **`operation.status` ignores `wait_ms`.** With 30000 ms requested, it
   returns after about 5 s.
4. **`progress` stops updating.** `last_updated_at` stayed at the job start
   time, and the phase showed `rendering` for 40 minutes. It should report
   sub-phases such as ray bake, nearest projection and AO, with texel
   counts.
5. **Shell normals are still flat for 6.4% of texels.** That is acceptable
   for a mid-distance NPC prop, but it still loses the source's normal
   detail in those areas.

Not covered: R3 and R6, the flashlight and tent, and the old-version
regression checks.
