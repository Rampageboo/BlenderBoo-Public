# BlenderBoo architecture

## Components and call path

```text
Agent / MCP client
  -> stdio MCP adapter (mcp_server/src/blenderboo_mcp/)
  -> selected instance, authenticated 127.0.0.1 TCP request
  -> add-on bridge queue (addon/blenderboo/bridge.py)
  -> Blender main-thread timer
  -> registry / dispatcher
  -> typed tool or python.exec
  -> result envelope -> MCP response profile -> client
```

`dispatcher.py` registers tool names, toolsets, descriptions, and input
schemas. `registry.py` validates and wraps handler outcomes in the stable
envelope. Tool implementations live under `tools/`. The bridge owns token
authentication, request/response bounds, backpressure, and queued
main-thread execution. The adapter declares MCP tools in `server.py`, routes
to an explicit or unambiguous registered instance, and compares declarations
with the add-on using `adapter.schema_audit`.

The add-on remains transport-independent; the MCP SDK stays outside Blender.
The bridge token is session-scoped. Instance IDs and document leases guard
which process and document receive a mutation. `expected_state` prevents
known-stale edits; an epoch bump supplies a new state token after a possible
change. Checkpoints are explicit recovery points, not an undo guarantee.

## Execution and failure

The bridge serializes Blender work through the timer. Pending work can time
out before it starts. Started Python or typed mutations are authoritative
only when their result arrives; Python that hangs can block the main thread
and cannot be cancelled safely by a socket timeout. The client must treat a
lost response as an ambiguous mutation and inspect before continuing.

The bridge records measured queue and Blender execution time in response
metadata. Its bounded operation registry accepts an authenticated start,
assigns an ID, and exposes queued/running/completed status with the final
result. `operation.status` can wait on the operation event for up to 30 seconds;
its data exposes the inner result outcome and error separately from status-call
transport success. While a long operation runs, another main-thread call gets
a busy response with that operation ID. A status query never executes the work again. It does not cancel a
running Blender call. The adapter correlates timeouts and late results by
operation ID and call-log sequence.

`python.exec` runs in a fresh namespace with Blender modules and context
aliases. Its default detector combines depsgraph update flags with a cheap
datablock inventory and limits custom-property reads to implicated IDs.
`diff:none` bypasses snapshots. Detailed mode reads focused fingerprints
and writes a breadcrumb before each field read so a native crash identifies
the last datablock and field; geometry arrays use bulk `foreach_get` reads.
The report is observational: `change_status:no_changes_detected` cannot prove that all RNA
data stayed identical. The response records user-code and detector overhead
separately so scene-size costs remain visible in the call log.
Connectivity probes in `tools/mesh_topology.py` use bulk mesh arrays and
NumPy roots to summarize islands. Exec samples only literal mesh-object
targets before code runs; typed mesh handlers sample their explicit targets
around actual apply operations. Count increases produce bounded focus
candidates and informational warnings. Neither path scans every mesh in the
scene. Dynamic exec targets have no pre-edit connectivity baseline.
Exceptions preserve captured streams and observed changes in the failure
envelope. A file load or add-on reload may invalidate bridge state and requires
reconciliation. GPU calls from the timer are hazardous; screenshot capture
uses a deferred draw-handler path. GUI detail renders use deferred Blender
render operators and completion callbacks while the bridge holds the request
open, so two clients can queue review work without running render/GPU work in
the bridge timer. Headless rendering uses the synchronous path. Render tools
restore temporary camera, output, and visibility state on completion or
failure.

`assembly.analyze` builds world-space BVHs from base or evaluated mesh data,
using object bounds only to choose candidate pairs. It reports bounded
surface samples and overlap evidence rather than a scene-wide quality score.
The focus point and radius connect numerical joint findings to the existing
detail-render tool. The v0.6.4 parity probe uses scale-aware ray steps and
only runs on closed target meshes. Open surfaces yield undetermined
penetration; status evidence distinguishes crossings, interior samples, and
distance. Tolerance gates interior depth and crossing extent.

`reference_review.py` fits camera pose/intrinsics from image/world points and
compares a reference silhouette to evaluated meshes with a CPU raster. The
camera mutation obeys the document lease/state token; the contour comparison
is read-only for Blender state. Both write review PNGs, which the adapter
returns through its image channel. Calibration residuals and contour distances
are measurements, while missing visible surfaces can leave a focus unknown.

`python_modules.py` stores active helper texts in the `.blend` and retains
hash-named source versions. `python.exec` installs a document-local importer
for `cbt_modules`, invalidates imports when active text changes, and records
used source hashes. The adapter call log records those hashes alongside exec
code; call-log redaction/size bounds still limit exact replay from logs alone.

`api.lookup` resolves allowlisted public Python paths against the running
Blender process: RNA structs/properties/functions, operators, and
`bmesh`/`mathutils` members. Search builds a bounded process-local index of
API identifiers, not scene objects. Missing-member suggestions are ranked
within the same parent. One curated Blender 5.0 `Scene.node_tree` rename is
cross-checked against the live replacement before it is suggested; additional
change-table entries require official sources. It is a read-only dispatcher
call; `python.exec` adds a small lookup hint only for recognizable API member
errors.

The adapter reads the selected add-on version on first contact and when the
instance selector changes. On mismatch, it queries the live schema before a
typed call and rejects supplied fields the add-on lacks. A context-local set
of caller-provided fields prevents wrapper defaults from being forwarded.
`toolsets.call` and completed operation records use the inner tool name and
outcome in the call log.

`scene.fast_capture` shares the screenshot draw callback and bridge deferred
envelope. Multi-view capture composes one image before resolving the request;
the active deferred bridge call serializes subsequent client requests.
Temporary viewport shading and overlays are restored after completion or
failure.

Background rendering has a separate lifetime from bridge calls. A brief
main-thread call normalizes and validates options, then queues a job ID; a timer saves a `.blend` copy and launches
the Blender binary against that copy with a JSON job file. A manager records
queue, child, progress, and final result under a bounded registry. The bridge
status endpoint can read that manager's thread-safe status without calling
`bpy` from its network thread. Cancellation terminates the child; the live
document is never the child render target. Failed job snapshots retain bounded
diagnostics, while successful/cancelled copies are removed. Linked libraries
with relative paths may fail while the child loads the temporary `.blend`
before its path fix-up runs, so their paths require separate validation.
Fixed-name checkpoints carry an `asset` identity in a sidecar for retention;
an existing destination requires `confirm`, and the active document cannot be
overwritten as a checkpoint.

## Adapter orchestration and responses

`workflow.py` validates bounded steps and runs them in order with explicit
compensation only. Its replay cache is process-local; it does not provide a
durable transaction or authorize automatic mutation retry.

`response_profiles.py` shapes `compact`, `default`, and `full` public results.
The adapter's artifact store holds omitted full responses for bounded later
retrieval with `response.artifact_get`; eviction and process restart remove
them. The full public envelope is supplied once in MCP structured content,
with a brief text summary by default. Text-full compatibility is controlled
by `CBT_MCP_TEXT_CHANNEL`.

The adapter's `call_log.py` writes local JSONL independently of Blender
document saves. It records direct calls and child workflow steps with a
parent ID. Redaction precedes disk writes, including previews and exec code;
failure to log warns once and does not change the dispatched result. The
summary CLI and `session.call_log_summary` read those records. A direct
`tests/run_headless.py` dispatcher call bypasses the adapter and therefore
does not create an MCP call-log entry; MCP-routed headless tests exercise this
boundary.

Tool ownership follows the operation: the add-on validates RNA and changes
Blender state; the adapter declares parameters and controls routing/receipt
shape. Baking runs synchronously on Blender's main thread behind the bridge
operation ID. Evaluated conversion does not force realization of Geometry
Nodes instances; callers use a Realize Instances node when required.

## v0.7 modeling pipeline

The source intake and graph live in `tools/creature.py`; body-plan presets are package JSON. The original source stays intact for baking, while `source.prepare` creates a projection proxy. A graph object stores joints and appendage decisions. Base creation uses that graph for the mesh, region groups, and skeleton. Fitting moves vertices of a new copy and compares connectivity hashes so a projection cannot silently alter topology. `tools/skinning.py` owns vertex-group constraints. ROM pose presets are package JSON and are interpreted by `tools/creature_qa.py`.

The add-on owns all mesh, pose, bake, and FBX operations on Blender's main thread. The adapter mirrors their schemas and exposes images as MCP image content. `adapter.schema_audit` checks declaration parity. `scripts/build_zip.py` packages the preset JSON alongside Python modules; an installed copy and a source checkout must therefore use the same data files.

Form views use the existing deferred capture/render path. They apply temporary shading and depth display state only during image generation, then restore the viewport. Scan section and reference grid images are CPU-generated review artifacts. `python.exec` change detection is observational and the collapse warning is informational; callers inspect the affected object before treating a result as accepted geometry.

The call log stores adapter/add-on versions and acceptance separately from transport/API success. Compact `since_seq` summaries inspect the current adapter session and avoid replaying the full table. File-save eligibility is based on the opened or last-saved disk revision, while document leases remain useful for multiple clients editing the same file.

RealityScan GUI CSV parsing is separate from Blender camera creation. The parser preserves each row and flags suspect raw distortion and abrupt focal changes; it does not infer pose or a distortion model. `tools/photogrammetry_import.py` composes a provisional camera pose with the selected point-cloud object's world similarity, creates isolated camera data in a new collection, and leaves scene render settings alone. Its default `csv_principal` mode preserves solved principal offsets; `centered_zero` is diagnostic. Preview is the default and applying an unverified pose needs explicit acknowledgement. `tools/colmap_import.py` is the primary workbench handoff: it parses the current JSON camera package or COLMAP text model, validates coupled intrinsics and poses, then creates one labeled run collection. It can import the sparse points, and sets the active camera/render resolution to the first imported frame. A true pixel residual needs identified 3D/image point pairs; mask overlap is only a weaker review signal. A synthetic armature fixture checks serialization and FBX mechanics; only an Owner-provided Mannequin export can establish UE skeleton fidelity.

## Code and verification ownership

## v0.7.4 AI-source game asset path

The MCP adapter is registered at user scope for both Codex and Claude.
Codex's global config launches the editable `mcp_server` package from the
current checkout; both user-global entries carry `PYTHONPATH` to the same
source. Neither registration belongs to a specific project folder.
The bridge token remains in local user configuration, never in this repo.

`asset_quality.py` and `asset_source_compare.py` are read-only analysis. The
former inspects base mesh topology and classifies intersections by face part;
the latter evaluates world-space low/high geometry, rasterises matched
orthographic silhouettes on CPU, samples surface distance, and optionally
casts radial rays. Both return explicit coverage and bounded samples, so a
partial analysis cannot silently become a pass. Optional comparison PNGs are
external review artifacts and must use a new or empty directory. The radial
grid is omitted from the default response; a caller can request it inline or
retrieve JSON from the comparison output directory. The quality gate returns
the full pole count and at most 20 pole examples.

The MCP response profile treats `changed:false` plus a semantic `status` as
an analytical or refusal result, rather than an empty mutation receipt.
Small results remain intact. Large fit grids move to a retrievable response
artifact while status, reason and decision metrics remain inline and in the
bounded text receipt.

`hard_surface_build.py` owns controlled construction and clearance fitting.
It writes face-domain integer part IDs on created meshes. Constructors reject
unsupported outlines and degenerate forms before editing scene data; their
output can be checked by `asset.quality_gate` and `asset.compare_to_source`.
Capped lathes map a quad grid over the disk, retaining four deliberate rim
corners per cap. The common construction path smooths polygons and marks
explicit rings and steep dihedral edges sharp. Projected plates thicken away
from their targets and run a bounded BVH clearance search before allocation;
failure leaves the scene unchanged.
The bounded slab builder constructs paired perimeter bands and optional
blind-pocket floor quads. It refuses folds or unsupported loops before
creating an object and writes FACE INT `fregion` labels so UV seams can follow
top, walls, bottom and pocket floor.

`ai_source_bake.py` runs the bake only in background Blender with CPU Cycles.
A live tool call queues a job through the existing background snapshot manager
and returns an operation ID for `operation.status`. The child makes temporary
scene/object/material copies, uses emission for colour/material
channels, routes shell face IDs through nearest transfer and other parts
through ray baking. Ray misses may use a distance-bounded nearest source-UV
cache sample for colour, roughness and metallic; fallback and unresolved texels
are counted separately. Normal ray misses use a flat tangent-space normal and
remain identified as lacking source normal detail. It then computes
UV-interior coverage and output maps. The
source `.blend` is not the bake target. The output directory receives a new
run folder; incomplete coverage remains reported. The child writes atomic
progress snapshots, including bake phase, nearest-projection texel counts,
wall time and process CPU time. A file-only heartbeat updates every five
seconds. The bridge's background status wait uses a completion event and
does not enter Blender's Python API from a network thread.
Optional fixed part fills override source projection after the channel bakes
and before ORM packing; their coverage is counted apart from ray and nearest
samples. The per-part report is exact for UV ownership and ray misses. The
other-low-part obstruction diagnostic samples at most a bounded set of
normal rays and labels matches as potential, not a complete source-ray proof.

`uv.py` owns absolute density and raster overlap audit as well as SLIM unwrap
and pre-pack island scaling. Density combines UV area, texture width, object
world area, and scene `scale_length`; the raster excludes zero-area shared
triangle borders. The analysis remains separate from the guarded UV mutation
transaction.
Its atlas audit rasterizes an object set in one UV tile and distinguishes
self/cross-object overlaps with bounded face-pair evidence. Region and turn
seam helpers validate explicit face/strip structure before marking mesh
edges. `mesh_join.py` builds a new world-space mesh from named sources,
preserving source objects, UVs and supported FACE attributes without relying
on selection or collection visibility; the earlier native `mesh.join`
target/source contract remains available through the same public name.
The dispatcher drops cached legacy join defaults and optional confirmation
before invoking the named handler; non-default legacy controls refuse.

`game_asset_export.py` triangulates copies of the requested mesh objects,
converts scene-unit coordinates to metres, and exports an FBX and named
textures into an exclusive staging directory. A JSON sidecar records pivot,
scale, LOD, collision policy, yaw, and detail normal settings for a separate
Unreal importer. The source quads and live selection remain intact. FBX
reimport verifies file mechanics; Unreal import, UE 5.8 collision, and final
asset appearance require Unreal-side verification. For walk-in static props,
`complex_as_simple` is the default until the UE Interchange UCX/body-setup
behavior is resolved.
`OBJECT_ORIGIN` preserves per-object pivots and assembly placement. Staged
publication with `overwrite=true` replaces only this call's named files and
restores prior outputs on failure; it does not clear a shared directory.
`python_modules.py` stores helper source in the current `.blend` Text data,
so file switching changes the visible module set after cache reconciliation.
`system.doctor` includes per-tool SHA-256 hashes of the add-on input schemas.
They expose contract changes to an existing client, but only a client restart
refreshes that client's cached tool declarations.

R7 reuses `render.ortho_set` with explicit common bounds plus `render.detail`
and `render.compare` for clay, material, and wire review. A matched bound and
camera direction must be supplied for before/after images; separately framed
automatic views are not an equivalent comparison.
For bounded subassembly close-ups, `render.detail(clip_to_bounds=true)` narrows
the temporary camera's near/far planes to the projected bounds depth. The
camera and prior render state are restored after capture; this depth slab is
not an exact world-axis box or a geometry bisect.

The UE skeletal FBX path is `creature_rig.py` into `ue_fbx_export.py` for both
mesh and Action clips. It stages every file, applies one root-node convention
and FBX settings set, normalizes supported staged coordinates to literal
centimetres, checks serialized layout/scale and Blender reimport, then
publishes accepted outputs. The source rig, Action, FPS, selection and pose
are restored. A failed candidate leaves destination files alone. Disposable
UE commandlet verification is a separate acceptance layer; Blender reimport
alone cannot prove UE import semantics.

`skin_audit.py` reads rest weights and explicit evaluated pose samples without
changing weights. It bounds mesh and pair work, reports unknown rather than a
false zero when intersection coverage is incomplete, and restores sampled
Action, frame and pose state. `goal_deform.py` owns the common float32-final
candidate validation and mutation transaction: region, pins, symmetry,
displacement and clearance are checked before any vertex write. The
front/side solver in `contour_fit.py` calls that same validator and measures
both silhouettes on the final candidate before applying it.


The add-on and adapter each declare a tool schema, so contract changes must
update both. Pure Python tests exercise registry, bridge, data snapshots, and
adapter shaping. Headless Blender verifies `bpy` behavior. A restarted live
client plus `system.doctor`, `adapter.schema_audit`, and one real call verifies
installation and client visibility. These evidence layers are distinct.

The old `docs/ai-workflow/` package is retained as legacy material. It is not
part of this architecture or an instruction route.
