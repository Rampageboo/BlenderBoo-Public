# AI-source game asset implementation evidence

2026-10-20. The requirement source is `game-asset-retopo-requirements.md`.
This records observed results, not a release claim. Production `.blend` files
under `<private-project>/ModelAssets` were read headlessly and not saved;
before/after SHA-256 values matched. Evidence scripts and JSON are under
`<redacted-evidence-directory>`.

## Headless mechanics

Verifier ran Python scoped tests and Blender 5.2.2 LTS headless fixtures for
R1, R2, R3, and R4. The R5/R6 synthetic fixture includes Blender UV unwrap,
overlap, FBX reimport, source-quad preservation, and physical size at scene
scales 1 and 0.01. A failed bake context, sRGB PNG roundtrip, false UV
shared-edge overlap, and 100x FBX scale were fixed before the passing reruns.
The fixture scripts are in `tests/run_*_headless.py`.

## Production measurements

| Saved asset | Quad faces | Triangle faces | Nonadjacent intersections | UV median | UV overlap |
|---|---:|---:|---:|---:|---:|
| Flashlight_low | 5,600 | 124 | 180 | 70.736 px/cm at 2K | 0.000496% at 2K |
| Tent_low | 1,552 | 0 | 0 | 2.760 px/cm at 4K | 0% at 4K |
| Helmet_low | 3,569 | 0 | 0 | 27.557 px/cm at 2K | 0.016328% at 2K |

The helmet **fails** the specified 0.01% overlap limit at 2K. Its 351
ambiguous texels include 305 within one folded UV quad; an earlier audit
omitted same-face triangle overlap and understated the result. That auditor
defect is fixed. Flashlight and tent pass with raster sizes matching their
stated texture sizes. The flashlight's saved image datablocks are 4K; its 2K density above uses the requirement's
comparison size. `uv.audit` reports both texture and raster size explicitly.

The final flashlight **fails** the all-quad/intersection gate. Independent
full-mesh BVH analysis matched all 180 intersection pairs; 128 are strict
transverse crossings between disconnected mesh islands. This is a real asset
defect rather than a quality-gate indexing error. Its 124 triangle faces are
also incompatible with the stated all-quad source bar. The old flashlight
versions fail in different ways, including slivers and n-gons in some versions.

The final helmet has eight actual degree-three quad cap junctions. They match
the reference generator's outer and inner cap corners exactly. The gate now
accepts explicit `allowed_loop_break_vertices`; with
`[1233,1251,1269,1287,1810,1828,1846,1864]`, the final helmet passes R1,
with zero intersections/slivers/unexpected endpoints. Without the declared
breaks it fails the strict default. The archived `Helmet_low` shape versions
have no detected self-intersection; the stated v1 NVG intersection was not
reproduced from the supplied archive.

The final tent has eight deliberate gable quad junctions documented by its
generator. With explicit allowlist `[79,81,89,91,915,917,925,927]`, it passes
R1 with zero intersections, slivers, unexpected endpoints, or irregular fans.
Without this declaration it fails the strict default.

## Acceptance still open

- R2 saved helmet high and low meshes are in different scales (source high
  Y extent 0.980133 m, final low 0.282462 m). The Owner recovered the exact
  post-bake low-mesh rescale command: for source high world coordinates,
  `p_low = 0.2857 * (p_high - (0,0,0.55))`. Apply the source object's
  `matrix_world` before this transform. The Owner independently measured
  nearest-surface median 0.48 mm, p90 2.62 mm, p99 6.35 mm, maximum 10.3 mm.
  A saved-vertex recheck reproduced these figures. Area sampling gives larger
  deviations because it samples face interiors: final median 1.052 mm,
  p90 4.707 mm, p99 14.916 mm, maximum 22.216 mm. At 512 px, final minimum
  silhouette IoU is 0.893304 (side); old shape versions score 0.873786 and
  0.890819. A threshold was not given in advance, so `pass` remains null.
  Full-sphere radial rays cover only 60.96% of paired directions; the radial
  comparison is partial rather than a demonstrated pass.
- R4 synthetic nearest/ray bake passes, including PNG channel values and
  temporary-scene cleanup. For production replay, inverse-transform the
  final low back into the source model coordinate system. The first replay
  placed the inverse uniform scale on the low object's transform; Blender's
  bake cage may therefore have interpreted `cage_extrusion=0.06` in scaled
  object units. A second replay with the transform applied to temporary mesh
  coordinates is required for a full faithful source-model-unit bake. The
  helmet has an enabled Triangulate modifier: the editable base UV quad yields 351 ambiguous texels
  (0.016328%) while the evaluated bake mesh uses the opposite diagonal and
  yields 46 (0.002140%). The R4 handler evaluates modifiers, so its default
  0.01% preflight passes on evaluated geometry; a manual base-mesh diagnostic
  is a different measurement. The first 2K production bake with the default
  0.01% UV tolerance completed and wrote 12 PNGs plus a run report. It measured
  `black_texels_pct = miss_pct = 0.67523022%` and normal miss 0.721663%, so
  the `<0.1%` colour requirement **fails**. `raw_dark_pct` is 22.391235%,
  while `valid_dark_pct` is 21.716005%: most darkness is valid source albedo.
  The source cache hit mask and colour hit mask keep missing coverage distinct
  from dark material. This replay used `max_ray_distance=0`; the original
  `MAXD` command argument had not yet been recovered, so exact parameter
  reproduction was unverified at the time. The full-bake miss figure is
  therefore diagnostic rather than a faithful replay of the original command.
  Original asset hashes and scene state were unchanged.

  A cheaper white-emission ray-only probe applied the inverse transform to
  temporary low mesh coordinates and reset its object matrix to identity.
  Nine combinations tested cage 0.03/0.06/0.09 and max ray distance 0/0.1/0.2
  in source model units at 1K. The best unbounded case (cage 0.09, max 0)
  still missed 0.66991% of all UV texels on non-shell parts, more than six
  times the 0.1% target. Finite distances worsened coverage. The earlier
  full-bake cage setting was not a faithful original-unit reproduction, but
  the corrected probe shows that these reasonable settings cannot close the
  remaining ray gap. Evidence: `production_ray_probe_applied.json`.

  A read-only ownership tally mapped all 14,513 missing texels to evaluated
  low-mesh face part IDs. Part 8 accounts for 7,539 misses (51.95%), part 4
  for 3,208 (22.10%), and part 9 for 2,200 (15.16%); together they account
  for 89.21% of the gap. Part 4 includes all 46 evaluated UV-ambiguity
  misses; parts 8 and 9 do not. Inspect part 8 first, then 4 and 9. These
  counts identify affected geometry, not a proven repair. Face IDs, base-face
  mappings and UV sample coordinates are in `production_miss_by_part.json`.
- R3 constructors have bounded supported shapes. `mesh.clear_of` handles a
  planar or consistently oriented closed curved target with BVH and gap
  checks. `mesh.build_plate` supports convex 3–64 corner outlines. Concave or
  holed plates, nonplanar open-target clearance, and `delete_hidden` remain
  unsupported before mutation. The proposed general hidden-face deletion is
  not delivered because no viewing/occlusion domain is specified.
- R6 FBX mechanics and sidecar are proven by headless Blender reimport.
  Actual Unreal 5.8 static-mesh import, material wiring, and collision remain
  separate UE-side acceptance work.
- R7 uses existing fixed-bounds orthographic and detail renders. A final
  production same-camera review set has not been generated.

## 2026-10-20 source fix verification

On 2026-10-20, a subsequent source change added a live-call background bake
entry, distance-bounded nearest source-UV fallback for ray-missed material
texels, separate fallback coverage, and flat-normal miss counts. Focused unit,
headless Blender, real background-child and MCP schema checks passed. The real
background-child fixture measured 1,024 fallback hits and 0 uncovered texels.
This source change is packaged as BlenderBoo 0.8.1. All 72 installed add-on
source/data files match the checkout by SHA-256. A fresh Blender 5.2.2 process
loaded the installed 0.8.1 add-on; the editable MCP adapter reported 0.8.1,
`system.doctor` was healthy, and `adapter.schema_audit` found 184 tools with
0 errors and 0 warnings. The global Codex and Claude MCP registrations both
point to this checkout. Installed smoke receipts are
`docs/reports/2026-10-20-blenderboo-081-addon-smoke.json` and
`docs/reports/2026-10-20-blenderboo-081-adapter-smoke.json`. Already running
Blender or client processes must restart to load the installed update.

A corrected-unit Helmet 2K replay used temporary aligned mesh coordinates
and the documented R4 settings. It stopped at the predeclared 600-second
limit during nearest projection, before coverage metrics were returned. The
new fallback has therefore **not** passed the production `<0.1%` criterion;
this run also exposes a possible 2K performance limit. Both original `.blend`
SHA-256 hashes were verified unchanged. Parameters, alignment, status and
hashes are in `docs/reports/2026-10-20-r4-fallback-timeout.json`.

A later Claude MCP replay recovered the original CLI max ray distance as 0.1
in source model units and applied the corresponding scale to the final low.
Its report records 51 uncovered texels (0.0024%), below the `<0.1%` colour
target, with 68,086 fallback hits; see
`docs/reports/2026-10-20-helmet-r4-081-replay.md`. Those are the supplied
replay's measurements, not a second Codex production run. The same report
records a 43-minute background job, whose slowdown remains unexplained.
BlenderBoo 0.8.2 adds long polling and stage/CPU progress for subsequent runs;
its bounded 1024² launch comparison found no slowdown caused by
`CREATE_NO_WINDOW` or `--factory-startup`. See
`docs/reports/2026-10-20-r4-background-082-followup.md`.

## Previous 0.7.4 deployment

The 0.7.4 ZIP was installed in the Blender 5.2 user add-on directory; all 72
packaged source/data files match by SHA-256 and no stale source files remain.
The editable Python adapter is 0.7.4 from the current checkout. A fresh
Blender 5.2.2 process imported the installed add-on, started an isolated
bridge, and contacted a fresh configured adapter process. `system.doctor` was
healthy, `python.exec` returned 42, `adapter.schema_audit` found 184 add-on
tools with 0 errors and 0 warnings, and four calls were logged with the token
redacted. The full MCP unit suite passed 132/132. Both canonical global
Blender skills passed `quick_validate.py`; `sync.ps1` installed them locally
and their source commit was pushed to `<private-config-repository>`.

Evidence is in the verifier directory's `installed_smoke_074_561617c5/` and
`live_schema_audit.json`. Global Codex and Claude MCP registrations are
user-scoped; already running client processes need restarting to pick up their
configuration. Do not describe the production pipeline as fully accepted
before the open checks above are resolved.
