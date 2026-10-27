# BlenderBoo specification

## Product

Provide an agent with local, inspectable control of Blender. A typed tool is
preferred when its contract fits. `python.exec` covers custom operations,
bulk work, and queries that the typed surface cannot express economically.
Both paths must identify the intended Blender instance and return evidence
that supports checking the resulting scene. Asset-specific modeling is a use
case, not the product.

Blender 5.2 LTS is the current qualification target. The add-on runs in
Blender; the MCP adapter runs in a separate Python process using the official
MCP SDK. The bridge is loopback-only and token-authenticated. A network thread
must never execute `bpy` directly.

## Public behavior

- Direct dispatch accepts a registered tool name and JSON-compatible params.
  Success and failure use a stable `ok`, `tool`, `data`, `error`, `warnings`
  envelope. The MCP adapter exposes the same add-on capabilities through
  declarations or search-mode discovery.
- The default MCP response keeps a small `changed:false` result intact when
  it carries a semantic `status` such as `unsupported`, `planned`, or
  `analysis`. A larger analysis keeps `status`, `reason`, coverage and fit
  summary inline and places its full grid in a response artifact. The text
  receipt also includes the refusal reason and analysis summary for clients
  that do not display structured content.
- `python.exec` accepts `code`, optional `expected_state`, `checkpoint`,
  `diff` (`none`, `summary`, `detailed`), and an informational
  `timeout_hint_s`. Code runs without AST, import, or module restrictions on
  Blender's main thread. Its fresh namespace includes `bpy`, `bmesh`,
  `mathutils`, `Vector`, `Matrix`, `C`, `D`, and a view-context helper.
- `api.lookup` reads the selected running Blender's RNA, operators, and
  public `bmesh`/`mathutils` members by dotted path or paged search. It
  reports the Blender version, signatures and metadata where available,
  and same-parent suggestions for missing members. A curated replacement is
  shown only when its official change-log entry and live target are verified.
  It does not inspect the
  scene, snapshot it, or advance its state epoch. Operator poll context is
  reported as unknown. A matching `python.exec` attribute failure can carry
  a bounded `api_hint` without changing the original error.
- The value assigned to `result` is returned as JSON when possible. A
  non-JSON result is represented with `repr` and marked as converted.
  Standard output and error are captured with bounded inline text. Captured
  overflow is stored in the full MCP response artifact when it fits the
  process-local artifact limit; the stream capture itself also has a limit.
- Exec returns an observational change report, `changed`, and a
  `state_token`. It reports partial changes even if code raises. A checkpoint
  request saves before execution when the document has a path. The diff is
  evidence, not proof that every Blender property is unchanged when no
  change is detected.
- The v0.6.2 report groups observed changes by geometry, transforms and
  hierarchy, modifiers, shading, Geometry Nodes, UV and attributes, and other
  datablock or custom-property edits. The default `diff:summary` checks
  depsgraph updates and a bounded datablock inventory; its receipt uses
  `detection:depsgraph+inventory`. `diff:none` takes no snapshots and reports
  `detection:disabled`; `diff:detailed` uses focused fingerprints and leaves
  a field breadcrumb if a native crash interrupts a read. An empty result
  has `change_status:no_changes_detected`, which does not prove arbitrary RNA
  unchanged. Every started exec still advances the state
  epoch. Response metadata separates user-code time (`exec_user`) from change
  detection overhead (`exec_overhead`), in milliseconds; the adapter records
  both in its call log when present.
- Exec fragment checks compare pre/post connectivity only for mesh objects
  explicitly named in the script and within the bounded target limit. The
  `fragment_check` receipt identifies coverage and skipped targets;
  `diff:none` performs no fragment sampling. `new_islands` findings are
  informational and include count change and small-island focus candidates.
  The relative small-island threshold is configurable. Dynamic target
  selection cannot be pre-sampled without a scene-wide scan.
- An exec that starts cannot be interrupted by the bridge. If the client
  loses the authoritative response, report that it may still be running and
  inspect the instance before another mutation. Never retry it automatically.
- Warn when code contains obvious GPU-in-timer calls; direct the caller to
  deferred screenshot/render tools. Warn and reconcile state when code
  switches the current file or reloads add-ons.
- `workflow.run` may contain `python.exec` with the same guard and failure
  semantics as a direct call. Workflows remain bounded orchestration rather
  than atomic transactions.

## Response behavior

The MCP public result contains one complete copy of the selected response
profile. `structuredContent` carries it; `TextContent` defaults to one line.
`CBT_MCP_TEXT_CHANNEL=full` provides a compatibility mode for clients that
require the complete JSON in text. Do not claim token savings until the
actual Claude Code and Codex client forwarding paths have been measured.

`default` exact-success mutation receipts contain only `ok`, `tool`,
`changed`, and `state_token`. Deviations add only Blender-decided facts such
as a renamed object, a clamp, `changed:false`, or warnings. Failure receipts
retain error, partial-change status, step status, and recovery guidance.
Reads keep requested data inline, with selection and pagination to bound it.
Only omitted, non-reconstructible data earns a short `artifact` ID;
`response.artifact_get` supports a JSON-path selection and is subject to
process-local capacity and eviction. `full` retains the complete journal.

## v0.6.1 detail and observability

- The MCP adapter writes a local JSONL record for each routed call and each
  workflow step, unless globally disabled with `CBT_CALL_LOG=0`. Records
  include tool, selected instance, outcome, measured timing fields, raw and
  public response sizes, and a digest/preview of redacted parameters. Bridge
  credentials must not appear even in embedded code. Rotation and retention
  bound local storage. `session.call_log_summary` and
  `scripts/summarize_call_log.py` summarize recorded calls; missing logs must
  not be represented as measured usage.
- Long operations can be started with an operation ID and queried without
  resubmitting the mutation. A client timeout can leave Blender processing;
  status inspection, not an automatic retry, decides the next step.
  `operation.status(wait_ms)` waits up to 30 seconds and exposes the inner
  result's outcome, error, change, and partial-change fields beside status.
  Other main-thread calls return the running operation ID in a busy response.
- Material, Geometry Nodes, and modifier edits validate Blender RNA rather
  than a fixed product allowlist. Material sockets are addressed by stable
  identifiers. A typed tool's inspect/capabilities path must show its live
  writable boundary.
- Typed detail tools include evaluated-mesh conversion, Smart UV projection,
  edge marks, PLY import, Cycles texture bake, explicit first save,
  checkpoint retention preview, and render quality controls. `python.exec`
  remains available for custom construction and inspection.
- A bake requires an explicit output path and restores selection, active
  object, UV layer, and render engine. The returned coverage is a measured
  sampled ratio, not an assertion that the normal map is artistically correct.
- Current limits: UV overlap and cross-object texel-density comparison are
  reported as unmeasured; evaluated conversion does not force Geometry Nodes
  instance realization; material curve-map point editing remains an exec
  recipe. These limits must not be presented as completed typed features.
- `workflow.run(execute:false)` validates known tool names and statically
  checkable parameters before execution, identifying values that depend on
  prior steps as deferred checks.

## v0.6.2 visual review

- `render.detail` frames an object or object set, a world-space point and
  radius, or explicit bounds. It accepts a named or vector view direction,
  perspective or orthographic projection, optional isolation, and resolution,
  and returns the camera parameters used with the PNG path.
- `render.ortho_set` renders the requested front, back, left, right, top, or
  bottom views. It can share one orthographic scale across the views.
- `render.reference_match` uses explicit camera parameters or an existing
  camera, matches the reference image's aspect ratio, and writes a render,
  side-by-side sheet, and opacity-controlled overlay. Camera solving is not
  part of this tool.
- `render.contact_sheet` combines labeled PNGs into one review image.
  Temporary camera, render, and visibility settings are restored after detail
  rendering. GUI renders complete through the bridge's deferred operation
  path; a headless Blender invocation uses synchronous rendering.

## v0.6.3 assembly, capture, and background work

- `assembly.analyze` accepts explicit mesh-object pairs, an object list, or a
  collection. It filters candidate pairs with expanded world bounds, then
  measures evaluated world-space mesh surfaces. Face and pair caps are
  explicit; larger meshes are refused without modifying the scene. The
  receipt reports gap/contact/intersection, closest points, sampled contact
  bounds and penetration, optional expectation pass/fail, and a `focus`
  point/radius usable by `render.detail`. Measurements are sampled estimates;
  a crossing without an interior sample may have an unknown depth.
  `contact: false` requires a gap, `no_intersection: false` requires an
  intersection, and `intersection_allowed: false` forbids one. Setting
  `intersection_allowed: true` permits a press fit without requiring it.
- `scene.fast_capture` captures one to six current or targeted viewport views
  with temporary shading and overlay settings and returns one PNG sheet. The
  adapter inlines it as MCP image content when within its image-size limit;
  the PNG path remains available. It uses the deferred draw handler and restores viewport
  state. Bridge, draw, and PNG times are separately measured.
- `render.background` returns an operation ID before snapshot/render work. A
  main-thread timer saves a copy, then a separate Blender process renders it;
  the live document is not the child render target. At most one child runs at
  a time. `operation.status` and `render.background_status` report progress,
  result paths, errors, and snapshot-staleness evidence;
  `render.background_cancel` terminates the child. Successful and cancelled
  temporary copies are removed; failed copies have bounded retention. For a
  background job, `operation.status(wait_ms)` waits until completion or the
  requested timeout (at most 30 seconds). Running status reads the child's
  current phase, progress counters, five-second heartbeat, and wall/CPU time
  without calling Blender APIs from the network thread.
- Server instructions give concise routing and recovery facts. Tool
  descriptions carry operation-specific limits and safe alternatives, and
  shared add-on/adapter descriptions stay identical.

## v0.6.4 measured assembly and reference review

- `assembly.analyze` uses scale-aware ray stepping and does not claim
  penetration unless sampled depth exceeds `contact_tolerance`. It reports
  `penetration: undetermined` for open target meshes and supplies per-pair
  `status_evidence`. Crossings must pass the tolerance on both sides.
- `camera.calibrate_from_reference` fits a perspective or orthographic camera
  from image/world correspondences. World points may be coordinates, an
  object vertex, or an Empty. It applies the camera and reference image
  dimensions, returns RMS/per-point pixel residuals, and writes an overlay.
  High residuals are reported. Mutations honor document lease and
  `expected_state` checks.
- `render.contour_compare` uses a supplied mask or a threshold/background
  color rule. It returns silhouette IoU, contour distance statistics,
  a deviation PNG, and focus regions suitable for `render.detail` when a
  model surface can be located. The adapter exposes both PNGs as MCP images.
- `python.module_put/list/remove` maintain named Python helpers in Text
  datablocks of the active `.blend`. `python.exec` imports them through
  `cbt_modules.<name>` and reloads changed sources between calls. Receipts
  and adapter call logs include used module hashes; immutable source versions
  remain in the document after updates or removal.
- `checkpoint.save` accepts fixed filenames and partial templates. Existing
  files require explicit `confirm`, and fixed-name retention requires an
  explicit `asset` identity. Background render parameters are normalized and
  validated before a job ID is returned, including vector options used by
  direct render calls.
- Verification requires part-specific pure Python/headless tests, matching
  add-on and MCP schemas, and a fresh-process installed add-on/adapter check.
  Historical scene-specific regression claims require the same object names
  in an available checkpoint.

## v0.7 creature and edit-cycle contract

- `scene.fast_capture`, `render.detail`, and `render.ortho_set` accept `view_mode`: `lit`, `normal`, `matcap`, `curvature`, `depth`, or `wireframe_on_shaded`. Capture uses the deferred GUI path, returns the selected mode and a numeric legend for depth, and restores viewport state. `render.contact_sheet` labels the constituent mode.
- A single client may mutate without acquiring a document lease. `scene.save_current` allows a one-call in-place save when the active file has a path and its disk revision matches the opened or last-saved revision. An externally modified file or new destination retains the confirmation gate. `session.call_log_summary` defaults to compact output and accepts `since_seq` and `detail` for bounded deltas.
- `python.exec` detailed mode reports per-object face count, bounds, and closed-mesh volume changes, plus informational `suspicious_change` warnings using configurable relative thresholds. These warnings never block execution. `python.module_put` accepts a local `path` as an alternative to inline source. Call records keep adapter and add-on versions and distinguish API success from acceptance for tools with thresholds.

- The adapter checks the selected add-on version before a typed call. A mismatch
  marks `system.doctor` unhealthy and adds a warning to receipts; caller-supplied
  fields absent from the live schema are rejected before dispatch. MCP wrapper
  defaults are not injected into add-on requests. The call log attributes
  `toolsets.call` and completed operation results to their inner tools and outcomes.
  Save receipts retain Blender-confirmed disk-write facts through every response
  profile. Fast-capture and contact-sheet view labels are upright.
- `assembly.analyze` exposes margins, sampling density, and `borderline` evidence without automatically passing a near miss. `reference.crop_grid` returns a labeled pixel grid for manual correspondences. `pointcloud.section` and `mesh.compare_to_points` measure scan sections and surface distances; sparse point coverage remains undetermined. `render.contour_compare` can classify a supplied IoU threshold and borderline band.
- `assembly.analyze` reports scoped parts without a contact/intersection pair
  as `isolated_parts`, including a detail focus; `expect_isolated` distinguishes
  intentional free parts. A `max_pairs` cap makes isolation evidence
  incomplete rather than a definitive absence. `mesh.topology_report`
  returns focus points for its smallest islands. Selected typed mesh edits
  (`mesh.edit`, `modifier.apply`, `object.convert_evaluated`, `mesh.join`,
  `mesh.separate`) compare pre/post island totals after apply and report
  `new_islands` without blocking the mutation.
- `source.inspect` measures imported source quality; `source.prepare` makes a separate projection proxy while retaining the original for baking. Landmarks and a named body graph drive a quad-dominant region-labeled base and custom armature. `creature.graph_suggest` proposes without applying; `creature.graph_set` stores the chosen graph. `mesh.wrap_to_target` produces a fitted copy and proves topology connectivity is unchanged. `mesh.topology_report` checks game budget, loops, poles, symmetry, and tiny faces; `mesh.edit` previews tiny-face cleanup.
- UE humanoid bone fidelity requires an imported Owner-provided reference skeleton and explicit graph-to-bone mapping. Without it, the armature uses a custom skeleton. Weight limiting enforces the requested influence count; index transfer refuses a connectivity mismatch. Skeletal FBX export has an optional round-trip check.
- `creature.bake_from_source` bakes from the original high-poly source through `bake.run`; `rig.rom_test` evaluates configured poses and reports deformation measurements and a contact sheet. A source/proxy/fitted distinction is retained in receipts.
- `camera.import_colmap_package` previews or imports a Camera Refinement Workbench `cameras.json` handoff or an undistorted COLMAP text model (`PINHOLE`/`SIMPLE_PINHOLE`). It keeps a run's intrinsics and poses coupled, labels reproduction versus constrained sets, preserves per-camera scores, and can import sparse points. A mapping object's uniform world transform places the whole solution in the scene; mesh alignment recorded by the package remains metadata until applied to the mesh. Two runs can coexist in separate collections. The scene's active camera and render resolution become the first imported frame on apply.
- `camera.import_photogrammetry` reads the standard RealityScan GUI camera CSV and preserves per-image focal lengths and raw calibration fields. It previews by default. The imported Blender cameras use the CSV's solved principal offsets (`csv_principal`) by default, including for M4A4; `centered_zero` is an explicit diagnostic that mixes solved poses with unsolved intrinsics. Camera transforms are composed with a selected point-cloud mesh object's uniform world similarity. The current yaw/pitch/roll mapping is an empirical candidate, so applying it requires explicit acknowledgement of unverified orientation. Blender does not represent nonzero lens distortion from this CSV. Neither raw distortion coefficients nor mask overlap constitute a measured 3D-to-2D pixel residual. Exact qualification requires known image/3D correspondences and matching renders.
- Real Tripo and UE Manny/Quinn qualification depend on Owner assets; synthetic tests do not establish those results.

## Acceptance evidence

## v0.7.4 AI-source hard-surface game asset contract

`docs/game-asset-retopo-requirements.md` is the task brief. The toolkit exposes
measured, bounded tools for a cleaned low-poly static asset and its high-poly
AI source:

- `asset.quality_gate` inspects the base mesh for quad topology, loop breaks,
  slivers, intersections, symmetry (when configured), and a triangle budget.
  An incomplete scan fails; open boundaries are reported and judged by the
  asset's intended shape. Deliberate interior quad poles require explicit
  `allowed_loop_break_vertices`; the tool reports accepted and unexpected
  endpoints separately.
- `asset.compare_to_source` returns common-frame silhouette IoU, sampled
  surface deviation, and optional radial shell differences. A sampled distance
  is not a Hausdorff guarantee. Missing radial rays cannot pass a radial
  threshold. The full radial grid is opt-in inline and is saved as JSON when
  `output_dir` is supplied. Without an explicit `radial_max_mm`, the region
  display threshold is the p90 paired absolute radial delta; that display
  threshold is not an acceptance check. Optional masks use an exclusive output directory.
- `mesh.build_lathe`, `mesh.build_sweep`, `mesh.build_plate`, `mesh.clear_of`,
  and `mesh.fair_fit` construct or evaluate controlled geometry; unsupported
  outlines, sweeps, or fittings must fail clearly before mutation. The editable
  authoring result remains quads and carries a face part identifier. Capped
  lathes use a disk-mapped quad grid with four rim corners per cap rather than
  a single-rim-vertex fan. Constructors smooth face shading and mark explicit
  and steep (over 35°) feature edges sharp. A projected plate grows thickness
  away from the target, then searches a bounded minimal outward offset until
  its triangulated shell no longer intersects the target; it refuses an
  unresolved intersection before creating an object.
- `bake.from_ai_source` transfers colour and material channels through a
  headless CPU bake. A live call queues a background snapshot job and returns
  `operation_id` for `operation.status`. Named shell parts use nearest surface;
  other parts use rays, with bounded nearest-source fallback for material
  channels when a ray misses. Ray, fallback, and unresolved texels are reported
  separately. `fallback_max_distance` is an optional scene-unit override of
  the default cage plus ray distance. Missing source normals use a flat
  low-poly normal and retain a separate miss count. The tool filters high-frequency normal noise, self-bakes
  AO, and may pack ORM. Its source material and UV restrictions are in the tool
  description; a missing source sample is reported rather than accepted.
- `uv.audit` reports absolute px/cm from texture size and Blender scene scale,
  raster overlap, deficient UV islands, and explicit threshold checks.
  `uv.unwrap` accepts Blender's `MINIMUM_STRETCH` where available; `uv.pack`
  can average island scale before packing.
- `export.game_asset` stages an FBX from triangulated temporary copies and
  texture files, plus a JSON Unreal hand-off. Source mesh quads remain intact.
  Export evidence does not establish an Unreal import or collision result.

The three Oct 2026 assets in the task brief are acceptance fixtures. Pass
claims require replay on their saved versions, the specified defect versions,
and the published thresholds. Synthetic headless tests establish mechanics,
not those production results. `render.ortho_set` with fixed bounds and
`render.detail` cover matched-camera review; no new renderer is required.
The current production replay and unresolved asset failures are recorded in
`docs/game-asset-retopo-validation.md`.

## v0.9.0 USP production tool contract

- `export.game_asset(pivot="OBJECT_ORIGIN")` retains each source object's
  local pivot and relative placement in FBX. The manifest records each source
  pivot. `overwrite=true` permits an existing output directory but replaces
  only this call's exact FBX, texture and sidecar paths through staged
  publication and rollback; unrelated files remain. `auto_convex` warns that
  one hull can fill an internal cavity. Unreal import and collision still
  require UE-side inspection.
- `render.detail` accepts `bounds` for a fixed camera frame together with
  `object_name` or `object_names` as isolation subjects. Isolation changes
  render visibility, not viewport `hide_set`. `render.ortho_set` accepts an
  output directory and a basename prefix together, with overwrite guards.
- `bake.from_ai_source` accepts optional `fill_face_attribute` (FACE INT) and
  `fill_parts` keyed by part ID. Each fixed part supplies linear base colour,
  roughness, metallic, AO and a flat normal, overriding source samples only
  on owned texels. Output separates fixed-fill texels, ray misses, nearest
  fallback and unresolved colour by part. A bounded normal-ray sample can
  identify *potential* obstruction by another low part; it is not proof of
  Cycles' exact bake-ray path or a full-texel occlusion count.
- `mesh.build_slab` constructs a solid all-quad slab, a through opening or a
  blind pocket from coplanar even-length ordered loops with matching outer
  and inner counts. It writes FACE INT `fregion` labels; unsupported and
  folded shapes refuse before mutation. It is not an arbitrary mesh boolean.
- `uv.audit` can inspect a set of objects sharing one atlas and separates
  within-object from cross-object overlap, with bounded face-pair evidence.
  `uv.seams_from_regions` marks FACE INT region boundaries;
  `uv.cut_strips_by_turn` cuts explicit ordered quad-strip edge rows when
  accumulated turn or segment count reaches a bound. Both default to dry
  run. `mesh.join` keeps its legacy target/source path and adds a named,
  selection-independent `object_names` + `name` path that creates a new
  world-space mesh while preserving sources.
- `python.module_put` copies source into the current `.blend`. A saved helper
  returns when that file is reopened; a different file exposes its own
  helpers. A path input is copied once, not linked across documents.
- A background render or bake snapshots the live document when its queued
  job starts. The call returning an operation ID does not itself establish
  that the snapshot already exists; status records the snapshot state and
  whether later live edits made it stale.

### v0.9.1 camp-antenna compatibility and close-up corrections

- Named `mesh.join(object_names, name)` ignores legacy parameters only when
  they equal their old defaults (`keep_sources=false`, `max_elements=500000`,
  `fragment_small_ratio=0.01`); optional `confirm=true` is accepted and
  ignored. Named apply needs `dry_run=false`, while legacy target/source apply
  still requires confirmation. Non-default legacy options still refuse.
- `system.doctor` reports the current SHA-256 input-schema digest by tool and
  tells clients to restart after an adapter upgrade. The digest makes a cached
  declaration diagnosable; the server cannot replace an already-running
  client's cached parameter list.
- `mesh.build_slab` treats the outline as the top face and extends thickness
  opposite its normal, which is negative Z for a counter-clockwise XY loop.
  The receipt reports `thickness_direction=negative_outline_normal`.
- `render.detail(clip_to_bounds=true)` requires explicit `bounds` and limits
  the temporary camera's near/far depth to their camera-space extent. It
  removes objects outside that depth interval from the image without editing
  geometry. It does not cut an exact axis-aligned 3D box, nor remove lighting
  or shadows from objects outside the interval.

### v0.10.0 rig export and measured deformation

- `armature.export_ue_fbx` uses one exporter for mesh and selected Action
  clips. `action_names`, `action_fps`, `include_mesh`, `root_mode` and
  `use_armature_deform_only` determine the output set. `root_mode=auto` strips
  the armature object if an exported `root` bone exists; otherwise it exports
  the object as UE root. Receipts name that root and record serialized node
  scale, literal-centimetre translation, hierarchy, mesh bounds, Action
  duration and sampled motion. Candidates failing qualification are not
  published. Unsupported shape geometry or positional FBX properties refuse
  during staged centimetre normalization. Importing onto an existing UE
  skeleton with the old ×100 root layout is unsupported; use a fresh skeleton.
- `skin.audit` is read-only and bounded. It reports per-bone opposite-side
  weight fraction and distance spill from a dominant-vertex radius, and for
  explicit pose/Action samples reports evaluated edge stretch p99/max/min and
  added nonadjacent triangle-pair intersections versus rest. Coverage and
  incomplete scans are explicit; advisory thresholds do not establish asset
  acceptance. Low spill alone can still crack joints.
- `mesh.goal_deform` requires an explicit vertex-group or face-index region
  and maximum millimetre displacement. Landmark warp, relax, normal offset,
  curve crease and seam smoothing share pinned-region, symmetry and
  unsigned-surface-clearance checks. Dry run is the default; apply requires
  `dry_run=false, confirm=true`. Unselected vertices remain unchanged.
  Receipts report landmark residuals, moved vertices, p50/p99/max displacement
  and volume change when the base mesh is closed and consistently wound.
  Constraint conflicts refuse rather than silently clamp. General operations
  do not automatically certify absence of new self-intersections.
- `mesh.fit_contours` accepts exactly two perpendicular orthographic reference
  views with a shared vertical axis, supplied as closed contours or masks.
  It fits row-convex silhouettes only, preserves vertical positions, reuses
  the same region/pin/symmetry/clearance checks and reports CPU-raster contour
  error and IoU before/after on final rounded coordinates. It does not infer
  hidden concavities or anatomical correctness.
- `scene.save_current` treats `confirm=true` plus a matching `expected_state`
  as an apply request when disk identity changed, unless `dry_run=true` is
  explicit. `render.detail` adds a light-independent Workbench `solid` mode
  and warns when `lit` has no visible Light objects. Large default MCP
  `python.exec` change lists retain result/stdout and summary counts inline;
  the complete changes remain in a response artifact. A windowless FBX
  operator-context failure names `interchange.import` as the typed route.
- `animation.action_retarget` remains a direct F-curve/bone-name remap. It
  does not solve rest-pose differences, axis conversions, mixed units or
  unequal chain lengths; these require separate retarget logic and pose QA.


1. Pure Python tests cover namespace, result conversion, output limits,
   exception partial changes, data diffs, response profiles, and channel
   selection. Add-on and MCP schemas agree.
2. Blender 5.2 tests create, modify, and delete via exec, compare the change
   report with inspected scene state, check a mid-script exception, stale
   `expected_state`, workflow execution, file switch reconciliation, and
   timeout ambiguity. A real client call follows adapter/add-on restart.
3. Measure serialized bytes and approximate tokens before and after on
   `scene.inspect`, `object.create`, `mesh.inspect`, `modifier.add`, an
   `architecture.wall_build` dry run, an eight-step workflow, and a
   fifty-object exec. Record both client forwarding paths separately.
4. v0.6.2 verification covers each change category and a pure-read exec,
   1,000-object/1-million-vertex and 10,000-object/5-million-vertex overhead
   benchmarks, detail image dimensions, and two-client deferred GUI rendering.
   Record measured performance even if the plan's 30 ms / 150 ms budgets are
   missed; preserve change coverage and explain the trade-off.
5. v0.6.3 verification covers exact assembly fixtures and large-mesh timing,
   screenshot and fast-capture GUI latency at 512/1024 pixels, two concurrent
   capture clients, background snapshot and cancellation in a Blender child,
   description parity, schema audit, and an installed fresh-process call.
6. v0.7.2 verification covers no-snapshot `diff:none`, a 1.5M-vertex mesh
   with 200 materialized objects, 1k and 10k object scenes, edit mode, a
   mixed-version schema fixture, inner operation failures and long polling,
   upright labels, save disk evidence, and a fresh installed-process call.
   Crash reproduction on a copy records either the affected datablock/field
   or the exact attempt and a not-reproduced result.
7. v0.7.3 verification covers live API lookup shapes, missing-path hints,
   scene-state invariance and lookup latency; an exec API failure hint;
   Boolean sliver and clean edit checks; intentional and unexpected isolated
   assembly parts; 1.5M-vertex connectivity timing; schema audit; and an
   installed fresh-process call.

The approved direction and proposed details are in
`docs/v0.6-python-exec-and-lean-responses.md`. That document is a plan;
source and live verification establish what was actually delivered.
