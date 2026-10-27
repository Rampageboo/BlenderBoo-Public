# BlenderBoo

Historical validation records in this public snapshot have developer-specific
paths and asset names redacted; placeholder paths are not usable locations.

BlenderBoo is an independent local control layer for Blender, with a name
inspired by Bangboo from *Zenless Zone Zero*. It is not affiliated with Blender
Foundation or HoYoverse and does not include their logos or character art.
The Blender Foundation's [trademark policy](https://www.blender.org/about/trademark-policy/)
advises against using "Blender" in an independent product name, and its
[extension terms](https://extensions.blender.org/terms-of-service/) do not
accept that word in extension names. The Owner chose BlenderBoo with those
publication limits understood.

Through v0.7.4 this project was named Codex Blender Toolkit. Older import
paths, MCP registration keys and ZIP names in historical reports refer to that
release; current installation uses the BlenderBoo names below. Existing
`cbt_*` data inside `.blend` files remains readable.

## Agent skills

The `skills/` directory contains the toolkit-specific modeling guidance.
`blender-hard-surface-modeling` and `blender-creature-modeling` define
asset-specific completion. `blender-reference-modeling` covers calibrated
photo, drawing, and scan comparison; `blender-asset-validation` checks saved
assets and requested exports; `blender-python-scripting` routes custom BMesh,
node, modifier, shader, rigging, simulation, compositor, and scene code to the
live toolkit and Blender API. Install the same folders into each host's skill
directory and start a new session after updating them. The skills are task
guidance; the selected add-on's `system.capabilities` supplies current tool
names and parameter schemas.

## Global MCP registration

Register `blenderboo` in your MCP client's user or project configuration.
From your checkout, install the adapter with
`python -m pip install -e ./mcp_server`, then configure the client to launch
`blenderboo-mcp` using that Python environment. Keep `CBT_BRIDGE_TOKEN` in local configuration and out of
the repository. Restart the client after changing its MCP registration and
restart Blender after installing a rebuilt add-on.

Multiple Codex and Claude clients can connect, but one Blender instance
executes scene work on its main thread. Use one writer per active `.blend`,
with a document lease and `expected_state` for coordinated edits. Independent
modeling runs can proceed in separate Blender instances and files before
reviewed integration; subagents sharing one MCP process also share its
client identity.

## v0.7.4 AI-source static game asset tooling

Use `asset.quality_gate` for source quad topology, loops, slivers, and
intersections; `asset.compare_to_source` for matched silhouettes and surface
deviation; the `mesh.build_*` constructors for controlled low-poly parts;
`bake.from_ai_source` for a CPU texture transfer (live calls queue a background
Blender job; poll `operation.status`); `uv.audit` for absolute
density and overlap; and `export.game_asset` to stage a triangulated FBX,
textures, and an Unreal hand-off JSON. The authoring mesh remains editable
quads. `render.ortho_set` with the same explicit bounds in each call, plus
`render.detail` and `render.compare`, covers matched-view review. Toolset calls
and eager MCP schemas must agree before rollout. Production acceptance remains
open for the helmet UV/bake, flashlight topology, and Unreal collision; see
`docs/game-asset-retopo-validation.md` for measurements and evidence.
The 0.8.3 toolkit follow-up fixes default MCP refusal and analysis receipts,
lathe cap topology, projected-plate clearance, and constructor shading; see
`docs/reports/2026-10-21-r3-mcp-083-followup.md`. The delivered Flashlight
asset was not changed by this toolkit repair.

## v0.9 USP production tools

`export.game_asset` now supports each object's own pivot and controlled
replacement of its exact output files in a shared directory. `render.detail`
can isolate named subjects inside fixed bounds. The AI-source bake accepts
fixed channels for FACE INT parts without a matching high-poly surface and
reports per-part misses. `mesh.build_slab` creates bounded all-quad slabs,
through openings and blind pockets. Multi-object `uv.audit`, region and turn
seams, and the named selection-independent `mesh.join` path support a shared
hard-surface atlas. See `docs/SPEC.md` and the USP follow-up report for
limits and verification.

The 0.9.1 camp-antenna follow-up accepts cached legacy defaults on the named
`mesh.join` path, reports each tool's current input-schema digest in
`system.doctor`, states that `mesh.build_slab` grows opposite the outline
normal, and adds `render.detail(clip_to_bounds=true)` for camera-depth clipping
inside explicit bounds. Existing Codex and Claude MCP sessions need a restart
to refresh changed parameters on existing tools.

## v0.10 rig, skin and goal-driven fitting

`armature.export_ue_fbx` exports a bound mesh or selected Actions through one
FBX layout, with per-action FPS, explicit/automatic UE root convention and
deform-only selection. Staged FBX data uses literal centimetre coordinates;
qualification checks scale, hierarchy, translation, mesh bounds and motion
before publication. The 0.10 fixture also imported into a disposable UE
5.8.3 project at default import settings: root scale 1, pelvis 80 cm, and
animated pelvis 87.5/95 cm at 0.5/1 s. Existing UE skeletons with the old
×100 root layout need a fresh skeleton import.

`skin.audit` reports bounded opposite-side weight bleed, far spill, posed edge
stretch and added self-intersection pairs. Its thresholds are advisory.
`mesh.goal_deform` supports landmark warps, region relax/offset, crease and
seam smoothing with masks, pins, symmetry and clearance preflight.
`mesh.fit_contours` fits selected base geometry to two orthographic front/side
silhouettes and reports pixel contour error before/after. Both deformation
tools default to dry run and refuse conflicting constraints before writing.
They are numerical fitting tools, not sculpt brushes or an anatomy judge.

`scene.save_current(confirm=true, expected_state=...)` now saves when the
on-disk file changed since load; an explicit `dry_run=true` still plans only.
`render.detail(view_mode="solid")` uses Workbench lighting, while `lit` warns
when no render-visible Light objects exist. Large `python.exec` change lists
are summarized in the default MCP response with full details in an artifact.

## v0.7 creature workflow

Inspect a source mesh with `source.inspect`, prepare a separate projection proxy, set landmarks and a body graph, then create and fit a region-labeled base. `mesh.topology_report` checks game topology and budgets. The graph also builds a custom armature; an exact UE humanoid skeleton requires an imported Owner-provided Mannequin FBX and a complete joint map. Limit and validate weights, bake normal/AO/color maps from the untouched original source, and run range-of-motion review before exporting a skeletal FBX. The source is not part of the game asset.

`scene.fast_capture`, `render.detail`, and `render.ortho_set` provide lit, normal, matcap, curvature, labeled depth, and wireframe-on-shaded views. `reference.crop_grid`, `pointcloud.section`, and `mesh.compare_to_points` support measured form review. `camera.import_colmap_package` imports Camera Refinement Workbench handoffs with coupled intrinsics, poses, kind labels, and scores. `camera.import_photogrammetry` previews standard RealityScan camera CSVs; its default `csv_principal` keeps solved principal offsets, while the candidate pose convention still needs image/3D point verification. Single-client edits need no lease; `session.call_log_summary` supports compact deltas. The v0.7 synthetic tests cover the pipeline mechanics. Real Tripo/UE Mannequin qualification awaits Owner assets.


## What changed in v0.6 and v0.6.1

v0.6 added unrestricted `python.exec` on Blender's main thread and smaller MCP
responses. v0.6.1 adds a local per-call log (`session.call_log_summary`),
operation IDs for long calls, and tighter receipts that return Blender's
decision without echoing the request. The add-on now supports RNA-validated
material, Geometry Nodes, and modifier edits; evaluated mesh conversion;
Smart UV and edge marks; PLY import; Cycles texture baking; explicit save-as;
checkpoint retention preview; and render quality settings. The modeling
skill defines asset completion criteria. Rebuild the add-on ZIP and
restart both Blender and the MCP adapter to see the new contracts.

## v0.6.2 change detection and detail review

`python.exec` reports affected geometry, transforms, modifiers, shading,
Geometry Nodes, UV/attributes, and custom properties where detected. Since
v0.7.2, default detection uses depsgraph updates and an ID inventory;
`diff:none` skips snapshots. A `changed:false` receipt reports
`change_status:no_changes_detected`, not proof that arbitrary RNA was unchanged.
The bridge reports measured user-code and change-detection overhead in response
metadata and the local call log.

`render.detail` frames an object, point, or bounds; `render.ortho_set` makes
orthographic views; `render.reference_match` creates a matched render,
side-by-side sheet, and overlay from an explicit or existing camera;
`render.contact_sheet` combines PNG views. GUI rendering uses a deferred
operation and restores temporary camera, render, and visibility state.

## v0.6.4 assembly and reference review

Assembly analysis now gates penetration by tolerance and reports unknown
penetration for open meshes. Calibrate a camera from reference correspondences
with `camera.calibrate_from_reference`, then compare silhouettes with
`render.contour_compare`. Named `python.exec` helpers persist in the `.blend`
through `python.module_put/list/remove`. Fixed-name checkpoints and synchronous
background render option validation are also available.

### v0.6.3 capture and background

`assembly.analyze` measures candidate part interfaces on evaluated,
world-space mesh surfaces. It reports sampled gap/contact/penetration evidence,
expectation results, and a `focus` point and radius accepted by `render.detail`.
Its distances and penetration depth are estimates based on bounded samples.

`scene.fast_capture` uses the deferred viewport draw path for one or several
targeted views in a single PNG. `render.background` queues a saved copy for a
separate Blender process and returns an operation ID; `operation.status` or
`render.background_status` reads progress, and `render.background_cancel`
terminates the child. The live scene can change while its snapshot renders;
the result and warnings identify that boundary. For background jobs,
`operation.status(wait_ms)` waits for completion or the requested timeout;
running status includes the current phase, a five-second heartbeat and child
CPU/wall time. Restart Codex or Claude after upgrading the MCP adapter so its
new parameter schema, including `fallback_max_distance`, is loaded.

BlenderBoo v0.10.0 is a Blender add-on plus a separate MCP stdio
adapter. The add-on targets Blender 4.x and later and exposes reliable, high-level
operations through one JSON-in/JSON-out Python entry point. It is currently validated
on Blender 5.2.2 LTS for the v0.6 through v0.7.4 synthetic headless paths and the v0.6.2
deferred-render and v0.6.3 fast-capture isolated GUI paths, with earlier baseline evidence
from 5.1.2; Blender 4.x is
untested. The toolkit does not require or
modify the community Blender MCP.

## v0.7.3 live API and fragment review

`api.lookup` reads the selected Blender's API directly by dotted path or
paged search. It helps check node, property, operator, `bmesh`, and
`mathutils` names before `python.exec` and can add a bounded hint to a
recognizable API error. It does not change the document.

`python.exec` and selected typed mesh mutations report informational
`new_islands` evidence for bounded, known mesh targets. `mesh.topology_report`
adds focus points for small islands. `assembly.analyze` reports unexpected
isolated parts and accepts `expect_isolated` for intentionally free parts.
Exec scripts that choose targets dynamically have no pre-edit fragment
baseline; check the receipt's `fragment_check` coverage.

## Import camera handoffs

Use `camera.import_colmap_package` with an absolute `package_dir` containing
either the Camera Refinement Workbench's `cameras.json` export or COLMAP
`cameras.txt` and `images.txt`. A text-only model needs `kind="reproduction"`
or `kind="constrained"` unless an adjacent `run.json` declares it. The tool
accepts undistorted `PINHOLE` and `SIMPLE_PINHOLE` cameras with square pixels.
It previews by default, reporting the camera count, kind, frame range, median
IoU when scored, and missing images when `images_dir` is supplied. Pass
`dry_run=false` to import. Each run gets its own collection, so reproduction
and constrained cameras can coexist; per-camera scores and image dimensions
are stored as object properties. An optional `transform_object` maps the
package world frame into the scene. `import_points=true` also imports
`points3D.txt` as a vertex-only reference mesh when the file exists. Applying
sets the first imported camera active and matches the scene render resolution
to its image size.

For RealityScan GUI CSV imports, `camera.import_photogrammetry` defaults to
`csv_principal`, preserving the solved principal offsets. Use that mode for
M4A4 CSV imports. Its yaw/pitch/roll mapping still requires explicit
orientation confirmation; `centered_zero` is only a diagnostic.

Every call returns this envelope, including failures:

```json
{
  "ok": true,
  "tool": "scene.inspect",
  "data": {},
  "error": null,
  "warnings": []
}
```

On failure, `ok` is `false` and `error` contains `type`, `message`, and `traceback`.

## Install

1. Build the ZIP from the repository root:

   ```powershell
   python scripts/build_zip.py
   ```

2. In Blender, open **Edit > Preferences > Add-ons**, choose **Install from Disk**,
and select `dist/blenderboo-0.10.0.zip`.
3. Enable **BlenderBoo**.

The archive root is the installable `blenderboo/` package. There are no
runtime dependencies beyond Blender and Python's standard library.

## Direct Python dispatch

From Blender's main thread, call the transport-independent dispatcher directly:

```python
import blenderboo as cbt
result = cbt.dispatch("scene.inspect", {"object_name": "Cube"})
```

The `result` value is a plain dictionary suitable for JSON serialization. Blender
tools must be dispatched on Blender's main thread.

Transport adapters can discover typed metadata without importing `bpy`:

```python
import blenderboo as cbt
specs = cbt.tool_specs()
```

Each specification contains `name`, `toolset`, `description`, and a JSON-serializable
`input_schema`. The MCP adapter uses matching typed signatures; `dispatch()` and its
stable result envelope remain unchanged.

## Unrestricted Python execution

`python.exec` runs a Python module body on Blender's main thread through the
same authenticated, instance-routed bridge as typed tools. It is intended for
custom operations, bulk edits, and focused queries the typed surface does not
express well. It is unrestricted: code can use `bpy`, `bmesh`, `mathutils`,
the standard library, and Blender-installed modules. The namespace includes
`C` (`bpy.context`), `D` (`bpy.data`), `Vector`, `Matrix`, and
`view3d_override()` for operators that need a visible 3D View.

```python
cbt.dispatch("python.exec", {
    "code": "result = [obj.name for obj in D.objects if obj.type == 'MESH']",
    "diff": "summary",
})
```

For mutations, pass an inspected `expected_state` and request `checkpoint`
before broad changes. Assign a JSON-compatible value to `result`; non-JSON
values use `repr` with a flag. The response includes captured streams,
before/after datablock change evidence, and a new `state_token`, including
partial evidence when code raises. The snapshot does not cover every RNA
property, so verify the affected scene state afterward. A started script
cannot be interrupted; a timeout or lost response may leave it running.
Never retry the call without inspecting the selected Blender instance.
GPU work in the bridge timer can crash Blender; use the deferred capture and
render tools. See `docs/SPEC.md` and the `python.exec` tool description for
the contract.

## Local bridge and MCP server

The add-on keeps the MCP SDK outside Blender. It exposes an
authenticated TCP bridge on `127.0.0.1` only, queues calls from network threads, and
executes each tool through a Blender main-thread timer. The separate stdio process in
`mcp_server/` publishes the typed MCP tools and forwards them to that bridge.

On enable/load, the bridge automatically starts on port `9877` using the token in
`~/.codex/config.toml` at
`[mcp_servers.blenderboo].env.CBT_BRIDGE_TOKEN`. That config value is the single
source of truth for both the add-on and the MCP subprocess. If the table has no token,
the add-on generates one and updates only that table after creating
`config.toml.bak-blenderboo`; unrelated configuration is preserved. Startup is
deferred until Blender is ready, and any config, bind, or registry failure is a warning
only: enabling the add-on still succeeds and the panel remains available.

Disable this behavior in **Edit > Preferences > Add-ons > BlenderBoo** by
turning off **Auto-start bridge on load**. In a 3D View, open the sidebar with **N** and
choose **BlenderBoo > BlenderBoo Bridge** to start/stop manually or copy the token.
**Rotate Token** safely stops and restarts the instance with a newly written config
token; Start and Rotate may also copy it to the clipboard without displaying or logging
it.

Each running bridge atomically registers its non-secret identity (`id`, `pid`, `port`,
`filepath`, start time, and token-location label) in
`~/.codex/codex_blender_instances.json`. The active `.blend` filepath is refreshed
after a file switch, and a clean stop removes only that instance. Tokens are never
written to this registry.

Direct API startup remains available for automation. Create a fresh token outside
Blender and keep it out of the repository and project context:

```powershell
$env:CBT_BRIDGE_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Start the bridge from Blender's main thread:

```python
import os
import blenderboo as cbt
cbt.start_bridge(
    os.environ["CBT_BRIDGE_TOKEN"],
    port=9877,
    token_location="env:CBT_BRIDGE_TOKEN",
)
```

Blender must inherit `CBT_BRIDGE_TOKEN` for that exact example. Alternatively, pass
the same transient token string to `start_bridge()` without writing it to a file.
The token must contain 32â€“4096 characters. Port `9877` is the default and only the
IPv4 loopback address is accepted. `cbt.bridge_status()` never returns the token;
`cbt.stop_bridge()` stops the socket, timer, and pending work safely. The listener now
uses exclusive address binding: a second Blender bridge on the same live port fails
loudly with `InvalidStateError` instead of sharing the port. A clean stop releases the
port so the bridge can be restarted normally. Stopping the bridge also clears any
process-local document lease so a restarted MCP client is not blocked by an abandoned
holder identity.

Install the standalone stable-v1 MCP adapter in a separate Python environment:

```powershell
python -m pip install -e ./mcp_server
blenderboo-mcp
```

The adapter constrains the official Python SDK to `mcp>=1.27,<2` because SDK v2 is
still a breaking pre-release line. The stdio process writes no token to output. Calls
that time out before Blender starts them are cancelled; after a tool begins, the
adapter waits for its authoritative result so a mutating operation cannot silently
continue behind a reported timeout.

The MCP server reads the shared registry and exposes `session.instances`. Every other
MCP tool has an optional `instance` selector accepting a registry ID or port. With one
registered instance, omission remains backward compatible; with multiple instances,
omission fails explicitly so a call can never drift to the wrong Blender process.
The MCP process reads the same `CBT_BRIDGE_TOKEN` environment value supplied by the
Codex config table. Existing ID/port-keyed `CBT_BRIDGE_TOKENS` mappings remain accepted
for legacy multi-instance bootstrap configurations, but auto-start and the panel write
the config-backed token.

Each adapter process generates one stable UUID `client_id` and includes it beside
`token`, `tool`, and `params` in every bridge request. Set `CBT_CLIENT_ID` before
starting the adapter to override it. The value is attribution, not authentication; it
never belongs inside tool parameters. Mutation transaction data records `client_id`
(or `null` for direct/legacy callers).

The bridge applies bounded FIFO backpressure: at most 64 calls may wait for Blender's
main thread, at most 16 socket handlers may be active, and one `client_id` may have at
most 4 queued or running calls. This preserves capacity for another adapter process
without changing Blender's authoritative main-thread serialization. Capacity rejection
returns a normal structured `BridgeBusyError` instead of silently closing the socket.
Its data includes `reason` (`connection_limit`, `client_inflight_limit`, or
`queue_full`), `queue_depth`, `queue_capacity`, and a `retry_after_ms` hint; callers may
retry after that delay, but the bridge never retries a mutation implicitly.

Clients can additionally acquire one expiring document lease around a multi-call
critical section. Reads and dry-runs remain available to every client, but while a
lease is active, every live document mutation (including material, image, geometry,
UV, object, checkpoint, and versioning changes) from other client IDs
fail with structured `LockedError`
data naming the holder and remaining TTL. The default TTL is 300 seconds; the allowed
range is 5â€“3600 seconds. The adapter supplies its `client_id` automatically.

For `scene.screenshot`, `render.preview`, `render.review_set`, and a written `render.compare` diff, the
MCP adapter keeps the structured envelope and also returns MCP image content read from
the local PNG paths. PNGs whose base64 form would exceed 8 MiB are not inlined; their
paths remain in the envelope and an adapter warning explains the fallback.

## Capability discovery, diagnostics, and workflows

The authoritative add-on registry now groups every tool into a `toolset` derived from
its domain. `system.capabilities` lists compact toolset summaries or returns one
toolset's schemas with a SHA-256 capability digest. `system.doctor` reports non-secret
add-on, Blender, document, bridge, adapter, and registry health; it never returns the
bridge token.

The MCP adapter defaults to backward-compatible `eager` mode, which advertises every
typed tool. Large agent contexts can opt into search mode before starting the adapter:

```powershell
$env:CBT_MCP_TOOL_MODE = "search"
blenderboo-mcp
```

Search mode advertises only `toolsets.list`, `toolsets.describe`, `toolsets.call`,
`workflow.run`, `workflow.template`, `workflow.batch_plan`, `workflow.job_start`,
`workflow.job_control`, `adapter.schema_audit`, and `response.artifact_get`. `toolsets.list` and
`toolsets.describe` discover authoritative schemas on demand; `toolsets.call` dispatches
one exact add-on tool and preserves image results for render/review calls.
This reduces initial schema volume without removing strong per-tool validation inside
Blender. Changing modes requires restarting only the standalone MCP adapter when it is
convenient; it does not require changing a running Blender add-on.

### MCP transport limits and recovery

The loopback bridge accepts requests up to 1 MiB and responses up to 4 MiB. The
adapter rejects an oversized request before opening a socket; an oversized Blender
response returns `BridgeResponseTooLargeError` with its measured size, the limit, and
a bounded outcome summary. Because a mutation may already have completed, this error
is explicitly unsafe for automatic retry: inspect the current document state before
deciding whether to issue another mutation. Do not embed large data URIs, images, or
document payloads in tool arguments: write the artifact to an allowed path and pass
that path instead. For inspection, use filters plus a smaller `limit` and continue with
`next_cursor`; for
capability discovery, use search mode and request one toolset at a time. PNG previews
are separately inlined only up to 8 MiB of base64 content, while their file path is
always retained. Bridge JSON frames use compact encoding so structural whitespace does
not consume the transport budget.

### Agent and output discipline

The MCP server instructions tell callers to begin with summaries and small filtered
pages, avoid scene-wide listings by default, pass file paths instead of inline
payloads, capture screenshots only when explicitly requested or genuinely needed for
visual validation, and never automatically retry a mutation or oversized response.

The adapter supports three restart-time response modes through
`CBT_MCP_RESPONSE_MODE`:

```powershell
$env:CBT_MCP_RESPONSE_MODE = "default"  # compact | default | full
blenderboo-mcp
```

- `default` returns a recovery-safe receipt for mutations and keeps complete
  read results up to 2 KiB; larger results use a receipt.
- `compact` uses a receipt for every result.
- `full` preserves the prior complete `structuredContent` behavior.

A compacted receipt retains outcome, errors, warnings, state/change/progress fields,
important identifiers and paths, plus a `response_artifact`. The authoritative full
JSON is kept only in a bounded process-local LRU: at most 64 results, 4 MiB per result,
and 32 MiB total. It is neither written to disk nor retained across adapter restarts.
Use `response.artifact_get` with the returned `artifact_id`, cursor, and a bounded
`max_chars` to retrieve only the exact chunks needed. If the store cannot retain a
result, the adapter returns the full result with a warning rather than silently losing
data. Render image content is preserved in every mode.

The selected public result is in `structuredContent`. By default,
`TextContent` carries only a one-line summary. Set
`CBT_MCP_TEXT_CHANNEL=full` before starting the adapter if a client needs
the complete JSON in text; this duplicates the public envelope. Actual
Claude Code and Codex forwarding costs need client-side measurement. Blender headless
smoke tests similarly print one-line pass records instead of complete envelopes. A
failing assertion retains bounded diagnostic JSON so useful evidence is not lost.

`workflow.run` accepts at most 32 ordered steps and defaults to `execute: false`, so the
first call only validates references and returns a plan. A later live call can reference
an earlier envelope using an exact object such as `{"$ref":
"inspect.data.state_token"}`. Workflows stop on the first failed step and never retry a
mutation implicitly. Optional compensating actions run in reverse order only when both
`rollback_on_failure: true` and `confirm_rollback: true` are supplied; every underlying
tool still enforces its own lease, state-token, dry-run, confirmation, and path rules.
For an executing workflow, callers can also supply a stable `idempotency_key`. Within
the current MCP process, the first request reserves that key and stores both successful
and failed outcomes; an identical repeat replays the stored outcome without calling
Blender, an in-flight duplicate is rejected, and reusing the key with different steps,
instance, or rollback options is a conflict. This bounded cache is duplicate protection,
not an automatic retry system, and it does not survive an MCP process restart. Oversized
results retain a compact replay summary while still preventing duplicate execution.
Every validated workflow response also carries `manifest_version`, a stable
`workflow_digest`, and structured progress counts. These counts describe the returned
synchronous call; they are not a live background-job feed. Cancellation is explicitly
reported unavailable because a Blender tool already dispatched through the bridge cannot
be interrupted without making final scene state ambiguous. On failure,
`failed_step_change` records whether the failed tool explicitly reported a `changed`
boolean. An explicit `changed: true` or an unknown failure keeps
`partial_change_possible: true` even if all earlier completed steps compensate; only an
explicit unchanged result or a known preflight rejection can clear that part of the
risk signal.

`workflow.job_start` adds bounded cross-call progress without pretending that Blender
operations are interruptible. It defaults to plan-only mode. Live creation requires an
exact `instance`, `execute: true`, and a process-local `idempotency_key`; it runs at most
four steps in one synchronous call and pauses when more work remains. Use
`workflow.job_control` with `status`, `resume`, or `cancel`. Resume and cancel require the
latest `expected_revision`, and cancel additionally requires `confirm_cancel: true`.
Cancellation takes effect only between tool calls: an in-flight Blender call must return
its authoritative result before the job can stop. Optional cancellation compensation
requires both `rollback_on_cancel: true` and `confirm_rollback: true` at job creation.
Confirmed `rollback_on_failure` also applies when the MCP job orchestrator itself fails
after recording successful steps. It compensates those recorded steps once, never
retries a compensation already in progress, preserves the original failure as a nested
cause, and keeps `partial_change_possible: true` whenever the active step's outcome is
unknown or compensation is incomplete.

Job state is local to one MCP process and uses no background worker threads; every resume
call synchronously performs the next bounded chunk. The manager retains at most 32 jobs,
allows at most 8 non-terminal jobs, caps stored successful result envelopes at 16 MiB per
job for later `$ref` resolution, and clears full results when a job becomes terminal.
Status and repeated-start responses therefore provide summaries rather than replaying
all prior output. Restarting the MCP adapter loses job state. These limits make the job
surface a cooperative coordinator, not durable scheduling or a universal transaction.

`workflow.template` is a local, data-only preset catalog. Its `list`, `describe`, and
`expand` actions never call Blender: expansion resolves bounded parameters into ordinary
`workflow.run` steps, validates their references, and returns a plan-only `run_request`
for explicit review. The versioned catalog contains `scene_review_v1`,
`object_move_review_v1`, `animation_health_v1`, `boolean_opening_v1`,
`mirror_array_v1`, `panel_inset_v1`, and `support_loop_pair_v1`. The move template takes
a checkpoint, chains an optimistic state token, and supplies exact prior local-position
compensation. The hard-surface templates require a caller-inspected state token;
panel/support operations remain dry-run-first atomic `mesh.edit` calls, while the
Mirror+Array template chains the first mutation's new state token and ships confirmed
modifier-removal compensation if Array creation fails. Every expansion includes its
recommended `rollback_on_failure`/`confirm_rollback` run options, but still executes
nothing until the caller separately invokes `workflow.run`. Templates cannot contain
arbitrary Python, caller-selected tools, or unrestricted operator kwargs.

`workflow.batch_plan` builds a bounded manifest for at most 32 explicitly attributed
jobs. Each job must name an exact `instance` and contain either typed workflow steps or
one built-in template. Serial plans may revisit an instance; parallel plans require
distinct normalized selectors so an exact repeated target is never presented as
concurrently safe. Because the planner does not resolve id/port aliases, callers must
still compare the plan with `session.instances`. The planner performs no bridge call,
background work, or mutation, and every returned `run_request` remains `execute: false`.
Call `session.instances`, review the manifest, then invoke each job through
`workflow.run` or `workflow.job_start`; `failure_policy` is recorded for the external
coordinator and is not silently enacted by the planner.

`adapter.schema_audit` is a read-only adapter diagnostic. In eager mode it retrieves
every authoritative add-on schema and compares it with the SDK-generated MCP input
schema for the corresponding typed wrapper. It reports missing/extra tools and
fields, required-field drift, JSON type differences, enum/const/bound/pattern/array
constraint drift, unknown-field policy, and default warnings, with a bounded issue
list and independent schema digests. The optional MCP `instance` routing field is
ignored by design. In search mode it verifies that the authoritative catalog can be
read and reports that no duplicated eager field surface is advertised.

```python
steps = [
    {"id": "inspect", "tool": "scene.inspect", "arguments": {}},
    {
        "id": "rename",
        "tool": "object.rename",
        "arguments": {
            "object_name": "Cube",
            "name": "Hero",
            "expected_state": {"$ref": "inspect.data.state_token"},
        },
        "rollback": {
            "tool": "object.rename",
            "arguments": {"object_name": "Hero", "name": "Cube"},
        },
    },
]
```

## Multi-instance session bootstrap

Start the bridge panel in each Blender instance on a distinct port. Then run:

```powershell
python scripts/bridge_session.py
```

For any panel-generated token not already supplied through an environment variable,
the script securely prompts for the clipboard value without echoing it. It verifies
`session.whoami` against every registered ID and port, then atomically updates only
`[mcp_servers.blenderboo]` in `~/.codex/config.toml` after backing up the existing
file to `config.toml.bak-blenderboo`. The resulting Codex configuration contains
the per-instance token map so the MCP subprocess can authenticate; it is outside the
repository and must be treated as secret-bearing. The script never prints tokens and
no longer connects to the removed community MCP port 9876. Restart Codex Desktop or
reload its MCP servers afterward.

## Tool reference

### `scene.inspect`

The default response is bounded and summary-only: scene name, total object count,
counts by type, collection-name hierarchy with per-collection object counts, selected
object count, active object name, and scene statistics. It does not include object-name
lists. Pass optional `object_name` for transform, dimensions, modifier names,
constraint names, collections, and visibility.

Every response also reports the active `filepath` (empty when unsaved), `saved`,
Blender's `dirty` flag, and an opaque `state_token`. The token changes after a toolkit
mutation and when the active filepath changes outside the toolkit.

Set `include_names` to `True` to add a flat, sorted `names` list of `{name, type}`
records. Names are paginated with `limit` (default 200, maximum 2000) and the opaque
`cursor` returned by the previous page. When names are included, `next_cursor` is null
after the last page and `truncated` indicates whether another page remains.

```python
cbt.dispatch("scene.inspect", {})
cbt.dispatch("scene.inspect", {"object_name": "Cube"})
first_page = cbt.dispatch("scene.inspect", {"include_names": True, "limit": 100})
next_page = cbt.dispatch(
    "scene.inspect",
    {"include_names": True, "limit": 100, "cursor": first_page["data"]["next_cursor"]},
)
```

### `annotation.inspect`

Reads Blender's native Annotation datablocks without changing the document. The
request selects one bounded hierarchy level at a time: no selector lists Annotation
datablocks; `annotation_name` lists layers; `layer_name` lists frames;
`frame_number` lists strokes; and `stroke_index` returns point coordinates. Every
level uses `limit` (default 200, maximum 2000) and the decimal `cursor` returned by
the previous page.

The response includes the current frame, document dirty flag, state token, parent
summaries, and a `page` record. Layer summaries include display color, opacity,
thickness, visibility, lock, and active-frame state. Stroke summaries expose their
Blender display mode; point pages expose `co` coordinates. This is structural data,
not a rendered interpretation of what the mark means, so pair it with
`scene.screenshot` when visual placement matters.

```python
annotations = cbt.dispatch("annotation.inspect", {})
layers = cbt.dispatch(
    "annotation.inspect", {"annotation_name": "ReviewNotes"}
)
points = cbt.dispatch(
    "annotation.inspect",
    {
        "annotation_name": "ReviewNotes",
        "layer_name": "Issues",
        "frame_number": 12,
        "stroke_index": 0,
        "limit": 200,
    },
)
```

### `scene.screenshot`

Captures the current 3D viewport or a caller-selected independent orbit view using the
viewport's shading mode. It does not move or otherwise change the visible Blender UI.
The bridge defers GPU work to a real `VIEW_3D` draw callback and serializes concurrent
requests in FIFO order, one completed draw job at a time. It is therefore a bridge/MCP
tool rather than a synchronous in-process `cbt.dispatch` operation.

With no supplied path, it writes a timestamped PNG under `previews/` beside the active
`.blend` (or the system temporary directory for an unsaved file). `overwrite` defaults
to `false`; `width` and `height` default to the live viewport region dimensions and
must remain within the same default 4K pixel budget as `render.preview`.

```json
{}

{
  "output_path": "C:/tmp/shrub-profile.png",
  "width": 1280,
  "height": 720,
  "view": {
    "mode": "orbit",
    "object_name": "Shrub",
    "azimuth_degrees": 90,
    "elevation_degrees": 15,
    "distance": 8
  }
}
```

For an orbit view, `object_name` targets the named object's world-space bounding-box
center. Alternatively, `target: [x, y, z]` targets a world coordinate; omitting both
targets the origin. Distance is derived from a target object's dimensions when omitted.
The result returns `path`, dimensions, `area_type`, `shading_type`, and view metadata.
Background/headless Blender has no viewport and returns
`InvalidStateError("no active 3D viewport available")`.

### `object.inspect`

Returns a sorted, paginated object listing with summary rows containing name, Blender
type, direct collection names, visibility, selection, and parent. Optional filters are
AND-combined: `name` substring, `name_regex`, `type`, `collection`, `visible`,
`selected`, direct `parent`, material-slot `material`, and custom-property key/value.
`limit` defaults to 200 and is capped at 2000; continue with
`next_cursor`. Pass `object_name` for full transform/dimensions, modifier, constraint,
custom-property-key, collection, visibility, selection, and parenting detail.

```python
cbt.dispatch("object.inspect", {"type": "MESH", "collection": "Stage", "visible": True})
cbt.dispatch("object.inspect", {"object_name": "Hero", "name_regex": "^Hero"})
```

### Collection lifecycle and links

`collection.inspect` returns a sorted, paginated scene-collection listing. Filters
include name substring/regex, direct parent, viewport-hidden state, and render-hidden
state, plus custom-property key/value. Pass `collection_name` for direct parents, children, objects, recursive counts,
visibility flags, and custom-property keys. The scene root is inspectable but cannot
be renamed or deleted.

`collection.create`, `collection.rename`, and `collection.set_visibility` provide
guarded CRUD and visibility changes. Creation targets the scene root by default or a
named parent collection. `collection.link_objects` adds existing scene objects to a
collection, while `collection.link_child` adds an additional hierarchy edge after a
cycle check.

`collection.parent_objects` replaces hundreds of one-object workflow steps with one
dry-run-first operation over direct or recursive collection members. It preserves
world transforms by default, checks hierarchy cycles, caps the reviewed target count,
and rolls back applied parents on failure. `collection.transform` applies one bounded
world-space delta and refuses scopes that would indirectly move outside descendants.

All unlink and deletion operations are dry-run-first. `collection.unlink_objects`
reports objects that would lose their final collection link;
`collection.unlink_child` reports whether the subtree would leave the current scene;
and `collection.delete` reports the complete affected collection/object set.
Execution requires both `dry_run: false` and `confirm: true`. Non-recursive deletion
accepts only empty leaf collections; recursive deletion rejects collection graphs
shared with another scene or an untargeted external parent.

```python
created = cbt.dispatch("collection.create", {"name": "Stage"})
cbt.dispatch("collection.link_objects", {"collection": "Stage", "objects": ["Hero"]})
cbt.dispatch("collection.parent_objects", {"collection": "ImportedPlan", "parent": "PlanOffset"})
cbt.dispatch("collection.transform", {"collection": "ImportedPlan", "translation": [140, 0, 0]})
plan = cbt.dispatch("collection.unlink_objects", {"collection": "Stage", "objects": ["Hero"]})
```

### Camera, light, World, and render settings

`camera.inspect` and `light.inspect` return bounded scene-object listings with typed
datablock settings and transforms. `camera.patch` covers projection, lens, clipping,
shift, and sensor fields; `light.patch` covers common energy/color/shadow values plus
validated area- and spot-light fields. Object placement remains the responsibility of
`object.transform` or `object.align_to_workplane`. `camera.set_active` assigns a camera
object or clears the scene camera.

`world.inspect`, `world.create`, `world.patch`, and `world.set_active` manage World
datablocks independently from scene assignment. Basic flat color and node usage are
supported. Background color/strength patching requires an already node-enabled World
with exactly one Background node; enabling nodes and changing Background inputs are
intentionally separate calls so rollback remains unambiguous.

`render.settings` is read-only when called without setting fields. A patch may change
the currently available render engine, resolution, percentage, transparency, frame
rate/range/step, output image format, and Blender output filepath. Engine names are
validated against the active Blender build rather than a hard-coded version list.
Every mutation uses the document lease, optional `expected_state`, a field-level
transaction journal, rollback on apply failure, and a new state token only after a
real change.

```python
cbt.dispatch("camera.set_active", {"object_name": "ReviewCamera"})
cbt.dispatch("light.patch", {"object_name": "Key", "energy": 1200, "color": [1, 0.8, 0.65]})
cbt.dispatch("render.settings", {"resolution_x": 1920, "resolution_y": 1080})
```

### Mesh inspection, measurement, modeling, and Modifier lifecycle

`mesh.inspect` reads one mesh object's source datablock without evaluating or changing
the dependency graph. It reports vertex/edge/polygon/loop counts, local and world
bounds, material slots, UV layers, shape-key count, and a paginated attribute list.
Topology diagnostics are opt-in with `include_topology: true`; edge/loop work is
refused above `max_elements` (250,000 by default, hard-capped at 5,000,000). The
diagnostics report loose vertices/edges, boundary and multi-face edges, degenerate
faces, and a closed-manifold candidate flag.

`mesh.measure` evaluates up to 100 bounded scalar queries without changing the mesh.
It measures local- or world-space vertex distance, three-vertex angle, aggregate edge
length, aggregate face area, semantic-selection bounds, and closed-manifold volume.
Edge, face, and bounds queries reuse the same predicate language as `mesh.edit` and
return scalar summaries rather than unbounded element arrays. World-space face area
uses concave-safe tessellation; world-space volume accounts for the object's complete
linear transform. The target must be outside Edit mode and the source/result work is
refused above `max_elements`.

`mesh.edit` evaluates an ordered operation chain against an in-memory BMesh and writes
the mesh datablock only once after every operation and budget check succeeds. It is
dry-run-first and accepts at most 100 operations. The default total-element budget is
500,000 and the hard cap is 5,000,000. Selection can target vertices, edges, or faces
using all-elements, indices, local-space bounds, material, normal, loose, boundary,
non-manifold, connected-component, sharp-edge, connected-coplanar, edge-length,
face-area, valence, edge-loop, edge-ring, bounded shortest-path, or constrained
region-growth predicates. Supported operations are
selection, transform, face-region extrude, inset, bevel, subdivide, spin/screw,
merge, delete,
normal recalculation, dissolve, boundary fill, rail-based quad Grid Fill, loop bridge,
same-face vertex connect, face Poke, plane bisect, triangulate, symmetrize, local-axis
align, even vertex distribution, and grid/point/plane snap.
`connect` requires exactly two non-adjacent vertices sharing one face and returns the
single splitting edge. `grid_fill` requires exactly two selected open boundary/loose
edge chains; compatible unselected boundary or wire edges form the two rails, and a
zero-face Blender result is rejected instead of reported as success. `poke` creates
one center vertex per selected face with bounded center and offset controls. `bisect`
is the deterministic viewport-independent Knife path for an exact local-space plane;
optional side clearing remains explicit.
`loop_cut` uses Blender's context-free edge-ring subdivision and requires exactly one
complete ring connected through quad faces. It leaves the new ring vertices selected
inside the transaction, so a following `edge_slide` can move a single cut toward an
unambiguous local-space direction by a signed factor without relying on a viewport.
Directional slide rejects branched/disconnected selections, poles without exactly two
side neighbors, zero-length side edges, and ambiguous directions.
`workplane` builds a stateless local U/V/N frame either from an exact face plus one of
its edges or from an explicit origin, normal, and U axis. Selected vertices can be
projected to the plane, aligned to exact U/V/N coordinates, or snapped to increments
in that frame. The resolved orthonormal frame is returned in the operation journal;
no 3D Cursor, viewport orientation, or persistent scene helper is changed.
`spin` extrudes or duplicates the selected vertex/edge/face closure around an exact
local-space center and normalized axis. `angle` and `steps` are explicit;
`screw_offset` is the total axial travel for the whole operation and is divided into
per-step BMesh translation internally. A conservative topology upper bound is checked
against `max_elements` before Blender allocates the result. Seam merging is allowed
only for a non-duplicate, zero-offset, one-revolution spin with at least three steps;
coincident vertices on the rotation axis are welded within the bounded
`axis_merge_distance`. The journal reports the resolved mode, axis, per-step
translation, selected counts, weld count, and projected upper bound.
`SHORTEST_PATH` selects one deterministic path between two same-domain indices using
either topological hops or element-center distance. `REGION` grows a fixed number of
topological layers from one or more seeds. Both accept an optional `within` predicate,
so paths and regions can be limited by existing material, normal, coplanar, length,
area, valence, bounds, or other semantic filters; search/result budgets fail loudly
instead of returning a truncated selection.
Precision operations accept an inline semantic selection and stay inside the same
one-shot BMesh transaction; alignment and snapping values are source-mesh local
coordinates so unapplied object transforms remain explicit.

`mesh.join` performs Blender's native mesh-object join so material slots, UV layers,
attributes, vertex groups, and world-space placement follow Blender's established
behavior. It preflights the combined element budget, preserves the target object
identity, snapshots the target mesh and groups, and restores removed source objects
if post-join validation fails. Removing sources is restricted to plain, unreferenced
mesh objects; `keep_sources: true` instead joins temporary copies when source objects
carry modifiers, constraints, animation, hierarchy, or custom properties.

`mesh.separate` supports `LOOSE`, `MATERIAL`, and semantic `SELECTION` modes. It
predicts and bounds the number of parts, assigns collision-safe deterministic names,
keeps the source object identity, and restores its mesh and vertex groups if Blender
creates too many elements or any later validation fails. Shared mesh data, linked
library data, and Shape Keys are refused by both multi-object workflows where their
recovery contract is not exact.

### Architectural wall lines and openings

`architecture.wall_build` converts 1-256 world-XY centerlines into separate editable
wall Mesh objects in one atomic call. Every line uses one global height, thickness,
and base Z and may contain up to 32 non-overlapping rectangular openings; the whole
request is capped at 512 openings and the normal mesh element budget. `DOOR` openings
must start at wall base, while `WINDOW` and generic `OPENING` records may include a
sill height. Openings are built directly into watertight geometry rather than emitted
as unapplied Boolean modifiers or cutter objects.

The tool defaults to `dry_run: true`. Preflight validates zero-length lines, opening
bounds and overlap, deterministic exact object/mesh names, collection and material
references, state token, and output budget before creating anything. A live call uses
`dry_run: false`, creates one object per line (`<name>_001`, ... for multi-line input),
assigns one optional existing material during creation, and attempts to remove all
objects and meshes it created if a later segment fails. Complete cleanup is reported
as rolled back; a cleanup failure preserves the journal, warning, and partial-change
signal. Intersecting wall centerlines remain
independent objects and are not Boolean-unioned or automatically mitered; floor slabs,
roofs, rooms, and annotation-to-wall semantic inference remain separate capabilities.

```python
request = {
    "name": "Floor_A_Walls",
    "wall_lines": [{
        "start": [0, 0], "end": [8, 0],
        "openings": [
            {"id": "Door_A", "kind": "DOOR", "offset": 1, "width": 1, "height": 2.1},
            {"id": "Window_A", "kind": "WINDOW", "offset": 4, "width": 2, "bottom": 1, "height": 1.2},
        ],
    }],
    "height": 3,
    "thickness": 0.2,
}
cbt.dispatch("architecture.wall_build", request)  # bounded plan
cbt.dispatch("architecture.wall_build", {**request, "dry_run": False})
```

Topology-changing chains require `dry_run: false` and `confirm: true`. The tool only
runs in Object mode and refuses shared mesh datablocks and meshes with Shape Keys.
Preflight reports per-operation counts and explicitly recommends a checkpoint; failed
writeback attempts to restore a temporary mesh backup before returning an error.

### Curve and spline lifecycle

`curve.inspect` lists Curve objects and returns detailed spline summaries for a named
object. Each spline reports its type, point count, cyclic state, endpoint gap, minimum
and maximum segment lengths, zero-length segments, and BÃ©zier aligned-handle
violations. Named-object audits and optional control-point data are budgeted by
`max_points` (10,000 by default, 100,000 maximum); returned points are paginated.

`curve.create` creates one legacy Curve object with an initial `POLY`, `BEZIER`, or
`NURBS` spline. `curve.patch` changes allowlisted Curve datablock settings, while
`curve.spline_add` and `curve.spline_patch` provide typed control-point and handle
editing. A spline patch deliberately preserves both the spline type and point count so
the prior values can be restored field-for-field if Blender rejects an edit. Shared
Curve datablocks and non-Object mode mutations are refused.

`curve.spline_remove` is dry-run-first, requires `dry_run: false` plus `confirm: true`,
and currently removes only the final spline so ordering remains deterministic and the
complete removed spline can be retained in recovery metadata.

```python
cbt.dispatch("curve.create", {"name": "Path", "spline": {"type": "BEZIER", "points": [{"co": [0, 0, 0], "handle_right_type": "AUTO"}, {"co": [2, 0, 1], "handle_left_type": "AUTO"}]}})
cbt.dispatch("curve.inspect", {"object_name": "Path", "include_points": True})
cbt.dispatch("curve.patch", {"object_name": "Path", "bevel_depth": 0.05, "bevel_resolution": 4})
```

### Images, material slots, and texture binding

`image.inspect` returns paginated image metadata without reading pixel buffers. File
paths are omitted unless `include_filepath: true` is explicitly requested.
`image.load` reads one explicitly named existing file with an allowlisted image
extension; it never writes to or deletes the source file. `image.create` creates a
generated image under a caller-visible pixel budget (16,777,216 by default), and
`image.patch` atomically changes colorspace, alpha mode, or fake-user retention.

`material.slot_inspect` reports slot assignments and polygon-use counts.
`material.slot_assign` assigns an existing material or appends a slot, rejecting
shared object data so other objects cannot be changed indirectly.
`material.slot_remove` is dry-run-first, scans at most 250,000 polygons by default,
and requires both `dry_run: false` and `confirm: true`; it snapshots the complete slot
list and polygon material indices for apply-time rollback.

`material.texture_bind` creates or reuses a stably named Image Texture node, can add
an explicit UV Map and Mapping chain, and connects color plus an optional alpha
target. Shared materials require `allow_shared: true` together with a reviewed
`expected_user_count`. Existing links are never replaced implicitly: set
`replace_existing: true`, review the dry-run journal, and pass `confirm: true` for the
live apply. The implementation restores touched properties and links if an apply
fails; a checkpoint remains the recovery boundary when Blender rejects rollback.

```python
cbt.dispatch("image.load", {"filepath": "C:/textures/hero_basecolor.png", "name": "Hero Base Color"})
cbt.dispatch("material.slot_assign", {"object_name": "Hero", "material": "Hero Material"})
cbt.dispatch("material.texture_bind", {"material": "Hero Material", "image": "Hero Base Color", "uv_map": "UVMap"})
```

### Geometry Nodes graphs and modifier inputs

`geometry_nodes.inspect` lists Geometry Nodes modifiers and reports node-group user
counts, stable interface socket identifiers, modifier input overrides, and optionally
paginated interface, node, socket, and link records. Detailed reads enforce explicit
node and link budgets.

`geometry_nodes.create` attaches a new modifier and a minimal geometry pass-through
group. `geometry_nodes.patch` accepts Geometry Nodes node types Blender can
instantiate, writable RNA properties, paired zones, node defaults, links, and
interface sockets. Patches are dry-run-first. A real patch is applied to a working
node-group copy and the modifier reference is swapped only after every operation and
graph budget succeeds. Removing nodes, links, or interface sockets additionally
requires `confirm: true`.

Shared groups are refused by default. Set `make_single_user: true` to fork only the
target modifier while leaving other users on the original group. Preconditions can
pin the expected group name and node/link counts. `geometry_nodes.modifier_inputs`
uses the stable interface identifiers returned by inspection and rolls individual
input assignments back on failure. Object, Collection, and Material values use
explicit `{kind, name}` references rather than arbitrary RNA access.

```python
cbt.dispatch("geometry_nodes.create", {"object_name": "Hero"})
cbt.dispatch("geometry_nodes.patch", {"object_name": "Hero", "operations": [{"op": "add_node", "node_type": "GeometryNodeTransform", "name": "Transform", "location": [0, 0]}]})
cbt.dispatch("geometry_nodes.inspect", {"object_name": "Hero", "include_graph": True})
```

### UV inspection, projection, unwrap, and packing

`uv.inspect` reports UV layers, active/render/clone roles, bounds, selected and pinned
loop counts, and optionally paginated per-loop records. Its island audit joins adjacent
faces only when both shared-edge UV endpoints match and also reports zero-area UV
faces. Every read and write is protected by a configurable UV-item budget (250,000 by
default, hard-capped at 5,000,000).

`uv.layer_create`, `uv.layer_rename`, and `uv.layer_set_active` provide guarded layer
lifecycle controls. Creation can copy an existing layer. `uv.layer_remove` is
dry-run-first and requires explicit confirmation because failed recovery may require a
checkpoint.

`uv.project` performs deterministic planar, cube, cylindrical, or spherical projection
without changing mesh topology. `uv.transform` translates, rotates, and scales UVs
around origin, bounds center, or median. Both support AND-combined face-index,
material-index, and local-normal predicates, compute complete output before mutation,
and restore touched loops if assignment fails.

`uv.unwrap` supports angle-based, conformal, and Smart UV Project modes;
its result includes zero-area UV and texel-density diagnostics. Overlap is
currently reported as unmeasured. `uv.pack` uses Blender's native UV operator.
Both snapshot and restore
Object/Edit mode, object selection, active object, mesh-face selection, active UV
layer, and UV selection flags. Operator failure restores the original UV coordinates;
context-restoration failures are returned explicitly rather than silently ignored.

```python
cbt.dispatch("uv.inspect", {"object_name": "Hero", "include_islands": True})
cbt.dispatch("uv.project", {"object_name": "Hero", "layer_name": "UVMap", "mode": "CUBE"})
cbt.dispatch("uv.unwrap", {"object_name": "Hero", "layer_name": "UVMap", "method": "ANGLE_BASED"})
cbt.dispatch("uv.pack", {"object_name": "Hero", "layer_name": "UVMap", "margin": 0.002})
```

### Compositor graphs and render manifests

`compositor.inspect` reports a bounded compositor node/link page, socket defaults,
supported node properties, and File Output formats and slots. On Blender 5.2,
the scene compositor node group and Group Output are used; the older scene
node tree and Composite node remain the Blender 4.x path. `compositor.create`
is dry-run-first and ensures a minimal Render Layers to output graph, with an
optional Viewer.

`compositor.patch` accepts at most 100 allowlisted built-in node, socket, link, File
Output, and file-slot operations. It pins optional node/link counts, requires an active
document lease for apply, and requires `confirm: true` for removals, disconnects, or
link replacement. Before apply it snapshots the graph; an apply failure removes added
nodes, recreates removed allowlisted nodes, restores settings and slots, and rebuilds
the original links. Custom/script/group nodes cannot be created or removed through
this surface.

`render.manifest` returns a stable structured record of engine, camera, frame range,
resolution, primary output, view-layer passes, AOVs, light groups, Composite nodes,
and File Output slots without rendering. Paths are omitted unless `include_paths` is
true. `render.manifest_validate` adds actionable errors and warnings for missing
cameras or enabled layers, unlinked Composite/File Output nodes, empty outputs, and
output-pattern collisions.

```python
cbt.dispatch("compositor.create", {"include_viewer": True})
cbt.dispatch("compositor.patch", {"operations": [{"op": "add_node", "node_type": "CompositorNodeGlare", "name": "Bloom"}]})
cbt.dispatch("render.manifest_validate", {"require_file_output": True})
```

### Guarded core-format import and export

`interchange.inspect` reports availability, accepted extensions, and the small option
allowlist for OBJ, FBX, glTF, USD, and Alembic. `interchange.import` requires one
explicit existing file, validates its extension and byte budget, is dry-run-first,
and requires confirmation for apply. It inventories new datablocks, restores the
previous selection, attempts to remove only newly created data after failure, and
returns a bounded created-data postflight.

`interchange.export` requires one explicit target and a non-empty selected, scene, or
named-collection scope. All live exports require a reviewed dry-run and confirmation;
existing files additionally require `overwrite: true`. The exporter writes one staged
single-file artifact beside the target, verifies it is non-empty, and only then moves
it into place. Existing targets are moved to an exact temporary backup during the
replacement window and restored if installation fails. To keep that boundary honest,
glTF export is initially limited to `.glb`, OBJ material sidecars are disabled, and
USD texture copying is disabled. No arbitrary operator name or filepath-bearing option
is accepted.

`interchange.blend_import` is the native `.blend` path for named Collection or Object
datablocks. It supports Append (`link: false`) and Link (`link: true`), preserves
Blender-native dependencies, validates names and file size during dry run, and links
the loaded data into an explicit target collection only after confirmation.

```python
cbt.dispatch("interchange.import", {"filepath": "C:/assets/hero.glb"})
cbt.dispatch("interchange.blend_import", {"filepath": "C:/assets/source.blend", "names": ["Plans"]})
cbt.dispatch("interchange.export", {"filepath": "C:/exports/hero.glb", "scope": "selected"})
```

### Armatures, skinning, and animation

The rigging surface is bounded and avoids arbitrary RNA paths. `armature.inspect`
pages rest hierarchy, deform flags, pose transforms, and constraints.
`armature.create` creates up to 512 explicitly described bones, while
`armature.edit` batches `add`, `patch`, and `rename` operations with context
restoration and targeted rollback. `armature.bone_remove` handles conservative leaf
bone deletion: children, custom properties, constraints, Action curves, owner and
cross-datablock drivers, external constraint targets, and matching groups on bound
meshes must be resolved first. `armature.validate` checks hierarchy, connected joints,
constraint targets, animation paths, and deform groups.

`pose.patch` changes allowlisted location, rotation, and scale fields.
`pose.constraint_patch` adds, patches, or confirmation-gates removal of common Copy,
Track, IK, Stretch, and Limit constraints. Constraint removal scans active/NLA Actions,
owner drivers, and driver-variable targets across animated datablocks; batched failures
recreate recoverable constraints at their previous index.

`skin.inspect`, `skin.bind`, and `skin.unbind` cover Armature modifier lifecycle without
implicitly parenting objects. Unbind preserves vertex groups. `skin.auto_weights`
wraps Blender's automatic solver with dry-run, confirmation, single-user mesh, sample,
and snapshot budgets. `skin.weights_patch` performs explicit indexed SET, ADD,
SUBTRACT, or REMOVE operations. `skin.weights_normalize` prunes small influences,
keeps the strongest configurable count, and renormalizes deform weights.
`skin.group_remove` only removes an empty group after a bounded membership scan.
Mutation tools restore group order, weights, locks, active group, frame, mode, and
selection where applicable. `skin.validate` reports modifier, deform-group,
membership, unweighted, and normalization problems.

`animation.inspect` supports legacy F-Curves and Blender 5 layered Action
slots/channelbags. Actions can be created, assigned or unassigned, dependency-checked
and removed. `animation.action_retarget` copies an Action between armatures, rewrites
pose-bone F-Curve prefixes through an explicit bone map (with same-name fallback),
validates every destination path, and can optionally assign the result.
`animation.pose_bake` bakes evaluated pose transforms over a bounded frame/bone sample
range into a new Action, including a selected-bones mode and visual keying.

`animation.keyframe_edit` accepts only object or pose-bone transform channels and uses
an isolated Action working copy. Set operations support interpolation, easing, and
left/right handle types; removal is confirmation-gated. `animation.validate` reports
missing Actions or slots, unresolved paths, duplicate/out-of-range keys, NLA range and
Action problems, invalid driver targets, and shape-key dependencies.

`animation.nla_inspect/patch/remove` covers bounded track and strip lifecycle; a track
can only be removed when empty. `animation.driver_inspect/create/remove` exposes one
safe generated linear mapping (`source * multiplier + offset`) on allowlisted object
or pose transforms—caller-supplied expressions and RNA paths remain unavailable.
`animation.shape_key_inspect/create/patch/remove` covers relative keys, vertex-group
settings, bounded point coordinates, dependency checks, and Basis protection.
Destructive lifecycle operations are dry-run-first and confirmation-gated.

```python
cbt.dispatch("armature.create", {"name": "Rig", "bones": [{"name": "Root", "head": [0, 0, 0], "tail": [0, 0, 1]}]})
cbt.dispatch("armature.edit", {"object_name": "Rig", "operations": [{"op": "add", "name": "Spine", "head": [0, 0, 1], "tail": [0, 0, 2], "parent": "Root", "use_connect": True}]})
cbt.dispatch("skin.bind", {"object_name": "Body", "armature_name": "Rig"})
cbt.dispatch("skin.auto_weights", {"object_name": "Body", "armature_name": "Rig", "dry_run": False, "confirm": True})
cbt.dispatch("skin.weights_normalize", {"object_name": "Body", "armature_name": "Rig", "max_influences": 4})
cbt.dispatch("animation.action_create", {"object_name": "Rig", "name": "RigAction"})
cbt.dispatch("animation.keyframe_edit", {"object_name": "Rig", "operations": [{"op": "set", "target": "pose_bone", "bone": "Spine", "property": "rotation_euler", "index": 1, "frame": 12, "value": 0.35, "interpolation": "BEZIER"}]})
cbt.dispatch("animation.action_retarget", {"source_object": "Rig", "source_action": "RigAction", "target_object": "GameRig", "name": "GameAction", "bone_map": {"Spine": "spine_01"}})
cbt.dispatch("animation.pose_bake", {"object_name": "Rig", "name": "Baked", "frame_start": 1, "frame_end": 48, "bones": ["Root", "Spine"]})
cbt.dispatch("animation.nla_patch", {"object_name": "Rig", "operations": [{"op": "add_track", "track": "Main"}, {"op": "add_strip", "track": "Main", "name": "Idle", "action": "RigAction", "frame_start": 1}]})
cbt.dispatch("animation.driver_create", {"object_name": "Rig", "property": "location", "index": 2, "source_object": "Control", "source_transform": "LOC_Z", "multiplier": 0.5})
cbt.dispatch("animation.shape_key_create", {"object_name": "Body", "name": "Basis"})
cbt.dispatch("animation.validate", {"object_name": "Rig", "frame_start": 1, "frame_end": 48})
```

Cascading dependency deletion and arbitrary driver formulas remain deliberately out
of scope; callers remove dependencies explicitly so dry-run output stays reviewable.
`modifier.inspect`, `modifier.add`, and `modifier.patch` expose a bounded allowlist of
common built-in modifiers: Array, Bevel, Boolean, Curve, Decimate, Displace, Lattice,
Mirror, Remesh, Screw, Shrinkwrap, Simple Deform, Smooth, Solidify, Subdivision
Surface, Triangulate, Weld, and Weighted Normal. Each modifier type has a small
explicit settings allowlist. Object pointers resolve only compatible named objects in
the current scene; Boolean collections must be scene-linked and cannot contain the
modified object; Displace textures, vertex groups, and UV layers must already exist.
Pointer, collection, texture, scalar, and vector settings are restored from typed
snapshots if a later field fails. Remesh voxel estimates and Screw step/output
estimates are rejected before mutation when they exceed the toolkit budgets.
Arbitrary RNA properties and operator names are not accepted.

`modifier.reorder` is dry-run-first. `modifier.remove` and `modifier.apply` require
both `dry_run: false` and `confirm: true`. Applying is limited to mesh objects in Object
mode, refuses shared mesh datablocks, restores selection/active-object state, and
reports before/after mesh counts. Because successful application changes geometry
irreversibly, its preflight explicitly requires checkpoint-based recovery.

```python
cbt.dispatch("mesh.inspect", {"object_name": "Hero", "include_topology": True})
cbt.dispatch("mesh.measure", {"object_name": "Hero", "queries": [{"type": "VOLUME", "space": "WORLD"}, {"type": "EDGE_LENGTH", "predicate": {"kind": "SHARP", "min_angle": 0.785398}}]})
cbt.dispatch("mesh.edit", {"object_name": "Hero", "operations": [{"op": "select", "domain": "FACE", "predicate": {"kind": "ALL"}}, {"op": "extrude", "offset": [0, 0, 0.1]}]})
cbt.dispatch("mesh.edit", {"object_name": "Hero", "operations": [{"op": "select", "domain": "FACE", "predicate": {"kind": "ALL"}}, {"op": "extrude", "offset": [0, 0, 0.1]}], "dry_run": False, "confirm": True})
cbt.dispatch("mesh.edit", {"object_name": "Hero", "operations": [{"op": "select", "domain": "EDGE", "predicate": {"kind": "EDGE_RING", "seed_index": 12}}, {"op": "bevel", "offset": 0.02, "segments": 3}]})
cbt.dispatch("mesh.edit", {"object_name": "Hero", "operations": [{"op": "loop_cut", "selection": {"domain": "EDGE", "predicate": {"kind": "EDGE_RING", "seed_index": 12}}, "cuts": 1}, {"op": "edge_slide", "direction": [0, 0, 1], "factor": 0.25}], "dry_run": False, "confirm": True})
cbt.dispatch("mesh.edit", {"object_name": "Hero", "operations": [{"op": "connect", "selection": {"domain": "VERT", "predicate": {"kind": "INDICES", "indices": [20, 24]}}}], "dry_run": False, "confirm": True})
cbt.dispatch("mesh.edit", {"object_name": "Hero", "operations": [{"op": "grid_fill", "selection": {"domain": "EDGE", "predicate": {"kind": "INDICES", "indices": [30, 31, 38, 39]}}, "use_interp_simple": True}], "dry_run": False, "confirm": True})
cbt.dispatch("mesh.edit", {"object_name": "Hero", "operations": [{"op": "poke", "selection": {"domain": "FACE", "predicate": {"kind": "INDICES", "indices": [4]}}, "center_mode": "MEAN_WEIGHTED"}], "dry_run": False, "confirm": True})
cbt.dispatch("mesh.edit", {"object_name": "Hero", "operations": [{"op": "workplane", "selection": {"domain": "VERT", "predicate": {"kind": "INDICES", "indices": [20, 21, 22]}}, "plane": {"source": "FACE", "face_index": 4, "edge_index": 9}, "mode": "GRID", "axes": ["U", "V", "N"], "increment": [0.1, 0.1, 1.0]}], "dry_run": False})
cbt.dispatch("mesh.edit", {"object_name": "Hero", "operations": [{"op": "select", "domain": "VERT", "predicate": {"kind": "SHORTEST_PATH", "start_index": 12, "end_index": 48, "metric": "DISTANCE", "max_steps": 10000}}, {"op": "transform", "translation": [0, 0, 0.05]}]})
cbt.dispatch("mesh.edit", {"object_name": "Hero", "operations": [{"op": "select", "domain": "FACE", "predicate": {"kind": "REGION", "seed_indices": [4], "steps": 3, "within": {"kind": "MATERIAL", "material_index": 2}, "max_elements": 5000}}]})
cbt.dispatch("mesh.edit", {"object_name": "Profile", "operations": [{"op": "spin", "selection": {"domain": "EDGE", "predicate": {"kind": "INDICES", "indices": [0, 1, 2]}}, "center": [0, 0, 0], "axis": [0, 0, 1], "angle": 6.283185307179586, "steps": 32, "use_merge": True}]})
cbt.dispatch("mesh.edit", {"object_name": "ThreadProfile", "operations": [{"op": "spin", "selection": {"domain": "EDGE", "predicate": {"kind": "INDICES", "indices": [0]}}, "axis": [0, 0, 1], "angle": 12.566370614359172, "steps": 48, "screw_offset": 0.1}], "dry_run": False, "confirm": True})
cbt.dispatch("mesh.edit", {"object_name": "Hero", "operations": [{"op": "snap", "selection": {"domain": "VERT", "predicate": {"kind": "BOUNDARY"}}, "mode": "GRID", "axes": ["X", "Z"], "increment": [0.1, 1.0, 0.1]}], "dry_run": False})
cbt.dispatch("mesh.join", {"target_object": "Hero", "source_objects": ["Panel_A", "Panel_B"]})
cbt.dispatch("mesh.join", {"target_object": "Hero", "source_objects": ["Panel_A", "Panel_B"], "dry_run": False, "confirm": True})
cbt.dispatch("mesh.separate", {"object_name": "Hero", "mode": "SELECTION", "selection": {"domain": "FACE", "predicate": {"kind": "MATERIAL", "material_index": 2}, "name_prefix": "HeroPanel"})
cbt.dispatch("modifier.add", {"object_name": "Hero", "type": "BEVEL", "settings": {"width": 0.02, "segments": 3}})
cbt.dispatch("modifier.add", {"object_name": "Hero", "type": "BOOLEAN", "settings": {"operation": "DIFFERENCE", "solver": "EXACT", "object": "PanelCutter"}})
cbt.dispatch("modifier.apply", {"object_name": "Hero", "modifier_name": "Bevel"})
```

Equivalent reusable hard-surface plans can be expanded locally through
`workflow.template`. For example, `panel_inset_v1` accepts exact face indices,
thickness/depth, a current `expected_state`, and the same `dry_run`/`confirm` gate as
`mesh.edit`; `boolean_opening_v1` adds a non-applied Difference modifier so the cutter
remains editable; and `mirror_array_v1` creates a deterministic two-modifier stack.

### Object lifecycle tools

`object.create` creates exactly one allowlisted mesh primitive (`PLANE`, `CUBE`,
`UV_SPHERE`, `ICO_SPHERE`, `CYLINDER`, `CONE`, `TORUS`, `CIRCLE`, `GRID`, or
`MONKEY`), `EMPTY`, `CAMERA`, or `LIGHT`. Lights additionally accept `light_type` as
`POINT`, `SUN`, `SPOT`, or `AREA`. Creation never exposes an arbitrary operator.
Optional location/rotation/scale default to identity. A named existing scene
collection can be targeted; otherwise the active collection or scene root is used.
Name collisions resolve with a Blender-style suffix and the result reports both the
requested and resolved names.

`object.duplicate` makes a full object/data copy by default. Set `linked: true` to
share the source data, and optionally target an existing collection. `object.rename`
rejects an occupied exact name and reports the conflict; pass `allow_suffix: true`
only when automatic suffixing is acceptable. `object.set_visibility` applies any
combination of `hide_viewport`, `hide_render`, and `hide_select` to one name or a list
of at most 2000 unique names.

`object.set_selection` selects or deselects a bounded object set without changing
mode. `exclusive: true` replaces the current selection and is valid only while
selecting. Pass `active` to assign one of the selected objects; pass
`set_active: true` with no `active` value to clear the active object. The tool refuses
to run outside Object mode so it cannot silently disturb Edit/Pose-mode state.

Every live lifecycle mutation accepts optional `expected_state`, checks the document
lease, journals exact before/after state, increments the state epoch only on real
change, and returns the resulting `state_token`.

### `object.parent`

Sets `parent` to an object name or `null` to unparent. Hierarchy changes are
dry-run-first: the default `dry_run: true` returns the affected objects and planned
journal without mutation; pass `dry_run: false` to apply. `keep_transform` defaults
to true and preserves the child's world matrix. Cycles are rejected before mutation.

```python
planned = cbt.dispatch("object.parent", {"object_name": "Prop", "parent": "Hero"})
applied = cbt.dispatch(
    "object.parent",
    {"object_name": "Prop", "parent": "Hero", "dry_run": False},
)
```

### `object.mode_set`

Dry-runs and explicitly switches the active object between Object, Edit, Pose,
Sculpt, Vertex Paint, Weight Paint, and Texture Paint. An optional `object_name` must
already be active for a live switch, so the operation never silently changes active
object or selection ownership.

### `object.transform`

With only `object_name`, this is a pure read returning both local and world
location/rotation_euler/scale. Supply any subset of those three fields to patch it.
`space: "local"` (the default) writes object-local transform channels. `space:
"world"` composes the requested world transform and converts it back through the
parent matrix and `matrix_parent_inverse`, so a parented object's local values update
while the requested world placement is respected. Every vector must contain exactly
three finite numbers.

### `object.align_to_workplane`

Plans or places one object on a workplane derived from a reference mesh face plus one
of its edges, or from an explicit origin/normal/U axis local to any reference object.
The default maps the target's local X/Y/Z axes to workplane U/V/N, preserves world
scale, and places its origin at the plane origin plus an optional U/V/N `offset`.
`axis_map` accepts a right-handed signed permutation such as `["V", "U", "-N"]`;
reflections are rejected. Location and rotation alignment can be enabled separately.
The operation is dry-run-first, supports parented targets, reports both local and
world planned transforms, and never changes viewport orientation or the 3D Cursor.

```python
request = {
    "object_name": "Panel",
    "reference_object": "Body",
    "plane": {"source": "FACE", "face_index": 12, "edge_index": 31},
    "offset": [0.0, 0.0, 0.01],
}
planned = cbt.dispatch("object.align_to_workplane", request)
applied = cbt.dispatch(
    "object.align_to_workplane",
    {**request, "dry_run": False, "expected_state": planned["data"]["state_token"]},
)
```

### `object.delete` and destructive confirmation

Deletion uses a double gate. The default `dry_run: true` returns affected objects,
dependent children, retained object-data blocks, and orphan candidates without
deleting anything. Execution requires both `dry_run: false` and `confirm: true`;
omitting confirmation returns `ConfirmationRequiredError` with the same bounded
affected-datablock transaction and zero change. Mesh/camera/light data is deliberately
retained for Blender's orphan-data management rather than force-deleted.

```python
plan = cbt.dispatch("object.delete", {"objects": ["DraftA", "DraftB"]})
deleted = cbt.dispatch(
    "object.delete",
    {"objects": ["DraftA", "DraftB"], "dry_run": False, "confirm": True},
)
```

### `checkpoint.save`

Saves a copy in `checkpoints/` beside the current `.blend` without changing the current
working file. It never overwrites an existing checkpoint. The optional tag is sanitized
for filenames; the default is `checkpoint`.

```python
cbt.dispatch("checkpoint.save", {"tag": "before-material-pass"})
```

Files are named `<basename>_ckpt_<NNN>_<tag>.blend`. The current file must already be
saved.

### `checkpoint.list`

Lists checkpoints for the current working file with name, absolute path, sequence
index, tag, byte size, and UTC modification time.

```python
cbt.dispatch("checkpoint.list", {})
```

### `checkpoint.restore`

Opens a checkpoint from the current file's checkpoint directory. It accepts a filename,
not an arbitrary path, and refuses unless `confirm` is exactly `True` because opening a
checkpoint discards unsaved state.

Like the other document mutators, it accepts optional `expected_state`. A stale token
returns `StaleStateError` without opening a file. A successful restore returns the new
`state_token` and transaction attribution.

```python
cbt.dispatch(
    "checkpoint.restore",
    {"name": "scene_ckpt_001_before-material-pass.blend", "confirm": True},
)
```

### `render.preview`

Renders a PNG from the active scene camera. If the scene has no camera, the tool creates
an auto-framed temporary camera and removes it after rendering. Render path, resolution,
file format, and scene camera are restored after the call.

```python
cbt.dispatch("render.preview", {})
cbt.dispatch(
    "render.preview",
    {
        "width": 1280,
        "height": 720,
        "output_path": "C:/tmp/review.png",
        "overwrite": False,
    },
)
```

The default resolution is 960x540. If `output_path` is omitted, a timestamped file is
written under `previews/` beside the current file, or under the system temporary
directory for an unsaved scene. `overwrite` defaults to `False`; an existing resolved
output path is rejected unless `overwrite` is exactly `True`.

The default pixel budget is 8,294,400 pixels (4K UHD). Width and height must each be
between 1 and 16,384. Set `allow_large` to `True` to raise the total-pixel ceiling to
the hard cap of 67,108,864 pixels (8192x8192 equivalent). The hard cap cannot be
overridden.

### `render.review_set`

Renders a canonical review sequence in one call. The available presets are `camera`,
`top`, `side`, and `three_quarter`; pass `views` to request a subset. `camera` uses the
active scene camera and is skipped with a warning when none exists. The other presets
use temporary auto-framed cameras from fixed directions. The response contains an
ordered `views` list and a `manifest` mapping each rendered view to its absolute PNG
path, camera name, dimensions, and per-view warnings.

```python
cbt.dispatch(
    "render.review_set",
    {
        "output_prefix": "C:/tmp/shrub_review",
        "views": ["camera", "top", "side", "three_quarter"],
        "width": 1280,
        "height": 720,
        "show_only": ["Shrub"],
        "hide": ["ReviewMarkers"],
    },
)
```

Use either `output_prefix` (producing `<prefix>_<view>.png`) or `output_dir`. When
neither is supplied, or when only `output_dir` is supplied, the tool allocates the
next timestamp-free `review_set_NNN_<view>.png` sequence under that directory; the
default directory is `previews/` beside the current `.blend` (or under the system
temporary directory for an unsaved scene). All target paths are preflighted. Explicit
prefixes therefore require `overwrite: true` to replace existing files.

`show_only` keeps the named collections, their descendants, and required ancestors
render-visible while hiding other collections. `hide` is applied afterward and takes
precedence. Every supplied collection must exist. Collection visibility, active camera,
render path, resolution, and PNG format are restored even if rendering fails. Width,
height, `allow_large`, and `overwrite` use the same limits and strict guards as
`render.preview`.

### `render.compare`

Compares two readable, equal-dimension PNGs using Blender-loaded RGBA pixels and
returns `changed_pixels`, `changed_fraction`, `mean_abs_rgb_delta`, and the changed
region `bbox` in addition to dimensions and total pixels. A pixel changes only when
its maximum absolute RGB-channel delta, scaled to 0â€“255, is strictly greater than
`threshold` (default 8). Alpha is ignored. The mean is across all RGB channels and all
pixels; bounding-box coordinates use Blender's lower-left pixel origin.

```python
cbt.dispatch(
    "render.compare",
    {
        "image_a": "C:/tmp/shrub_before.png",
        "image_b": "C:/tmp/shrub_after.png",
        "threshold": 8,
        "write_diff": True,
        "diff_output_path": "C:/tmp/shrub_diff.png",
    },
)
```

`threshold` must be an integer from 0 to 255. Different dimensions fail with both
sizes named. With `write_diff: true`, changed pixels are highlighted red over a dimmed
copy of `image_b`; the returned `diff_path` is beside `image_b` by default. Existing
diff outputs are rejected unless `overwrite` is exactly `True`.

### `material.inspect`

Returns summary records for one named material, materials used by an object or
collection, or all materials. A summary includes Blender user/object counts, node-use
and material-output status, plus the key Principled BSDF inputs when present.
Node socket records include an identifier, index, display name, type, and
editable flag so a patch can address duplicate display names. It is
read-only.

Pass `include_nodes: true` to add JSON-safe node input summaries and graph links. Node
records use bounded pagination: `limit` defaults to 200 (maximum 2000), with
`next_cursor` and `truncated` on each material record.

```python
cbt.dispatch("material.inspect", {"material": "M_LeafClusterV3_Deep"})
cbt.dispatch("material.inspect", {"material": "M_LeafClusterV3_Deep", "include_nodes": True})
```

### `material.patch`

Applies a Blender RNA-validated node/material mutation to exactly one named material. It is
**dry-run first**: `dry_run` defaults to `True`; pass `dry_run: false` only after
reviewing the planned journal and taking a `checkpoint.save` if rollback is needed.
All preconditions and operation validation happen before a live mutation starts.

Operations include `add_node`, `remove_node`, `set_input`, `connect`,
`disconnect`, `set_node_property`, Color Ramp element changes, and
`set_material_flag`. Node additions require a stable name and a ShaderNode
type Blender can instantiate, including a named node group. Writable node
properties are validated against Blender RNA. Supported preconditions are `node_present`, `input_equals`, and
`flag_equals`. `expected_user_count` guards against accidentally patching a shared
material whose usage has changed.

Pass the latest `state_token` as optional `expected_state` on a live apply. If another
conversation has mutated or switched the document since inspection, the call returns
`StaleStateError` before material lookup, preconditions, or mutation. `expected_state`
remains optional for backward compatibility; dry-run-first behavior is unchanged.

```python
patch = {
    "material": "M_LeafClusterV3_Deep",
    "preconditions": [{"kind": "node_present", "name": "CBT_Backface", "present": False}],
    "operations": [
        {"op": "add_node", "node_type": "ShaderNodeNewGeometry", "name": "CBT_Backface"},
        {"op": "set_material_flag", "flag": "use_backface_culling", "value": False},
    ],
}
planned = cbt.dispatch("material.patch", patch)
applied = cbt.dispatch("material.patch", {**patch, "dry_run": False})
```

Responses place mutation metadata inside `data`: a deterministic `operation_id`,
`client_id`, affected datablocks/user count, `changed`, the resulting `state_token`,
and an ordered before/after journal. An
unexpected apply-time failure remains a normal error envelope and retains its partial
journal in `data`; the prior checkpoint is the rollback safety net.

### `scene.save_version`

Promotes the working document using a non-overwriting Blender save-as. Unlike
`checkpoint.save`, it is not a sidecar copy: the new version becomes the active file,
so subsequent calls operate on it. The default base is derived from the active file
name with a trailing `_vNNN`/`_versionNNN` removed; an optional filename-safe `tag`
is appended. The result returns `old_path`, `new_path`, sequence `version`, and the
shared transaction metadata.

Pass optional `expected_state` to prevent saving a version from a stale view. The
successful result contains a new `state_token` for the now-active version file.

```python
cbt.dispatch("scene.save_version", {"tag": "backface-repair"})
```

### `scene.save_current`

Saves the active `.blend` back to its existing path. Because it overwrites the current
file, it is dry-run-first and requires `dry_run: false, confirm: true`. The result
returns before/after disk modification time, byte size, existence, filepath, and
Blender dirty state. `scene.inspect` and `session.whoami` expose the same `document`
record. Their legacy top-level `saved` field means only that Blender has a filepath;
it is not evidence of a recent disk write.

```python
cbt.dispatch("scene.save_current", {})
cbt.dispatch("scene.save_current", {"dry_run": False, "confirm": True})
```

### `session.whoami`

Returns one bounded identity/health record: Blender process ID, active filepath,
saved/dirty state, current `state_token`, bridge running/host/port state, and pending
call count. Its bridge record also reports queue depth/capacity, active connection
count/capacity, total client in-flight calls, active client count, and the per-client
limit. It also reports the registry instance ID, start time, safe token-location
label, registry path/error state, current transport `client_id`, and document-lease status.
The opaque `lease_id` is shown only when the caller is the holder. It is read-only and
accepts no parameters.

```python
cbt.dispatch("session.whoami", {})
```

### `session.instances` (MCP only)

Enumerates live registered Blender processes with their IDs, PIDs, ports, filepaths,
start times, and safe token-location labels. It never returns token values. This tool
is handled by the standalone MCP adapter, so it is not part of Blender-side
`cbt.dispatch()` or `tool_specs()`.

All other MCP tools accept optional `instance`. The selector may be a registry ID or
registered port. For example, after enumeration, call `scene.inspect` with:

```json
{"instance": "a6c62f8e-...", "include_names": false}
```

### `session.acquire_lease`

Acquires the process-global `document` lease for the calling MCP client. `ttl`
defaults to 300 seconds and must be from 5 to 3600. Repeating acquire as the same
holder is idempotent: it returns the same `lease_id` without extending expiry; use
`session.renew_lease` to extend it. Another client receives `LockedError` with
`held_by` and `expires_in` but never receives the holder's `lease_id`.

Direct in-Blender dispatch must supply a transport context; normal MCP calls already
receive one from the adapter:

```python
context = {"client_id": "repair-run"}
lease = cbt.dispatch(
    "session.acquire_lease",
    {"scope": "document", "ttl": 300},
    context=context,
)["data"]
```

### `session.renew_lease`

Extends the holder's active lease, preserving its opaque `lease_id`. Omitting `ttl`
reuses the lease's current TTL. A wrong holder receives `LockedError`; an expired or
mismatched ID receives `NotFoundError`.

```python
cbt.dispatch(
    "session.renew_lease",
    {"lease_id": lease["lease_id"], "ttl": 600},
    context=context,
)
```

### `session.release_lease`

Releases the holder's lease. A successful toolkit-controlled `checkpoint.restore` or
`scene.save_version` carries the lease to the newly active filepath so it can still be
released in a `finally` block. An out-of-band filepath switch invalidates the old
lease, and TTL expiry is enforced by the bridge timer.

```python
cbt.dispatch(
    "session.release_lease",
    {"lease_id": lease["lease_id"]},
    context=context,
)
```

## Lease plus optimistic concurrency example

Acquire a lease for the multi-step workflow, then chain each new `state_token` rather
than reusing an old token. Always release in `finally`:

```python
context = {"client_id": "repair-run"}
lease = cbt.dispatch("session.acquire_lease", {"ttl": 300}, context=context)["data"]
try:
    state = cbt.dispatch("scene.inspect", {}, context=context)["data"]["state_token"]
    applied = cbt.dispatch(
        "material.patch",
        {**patch, "dry_run": False, "expected_state": state},
        context=context,
    )
    next_state = applied["data"]["state_token"]
    saved = cbt.dispatch(
        "scene.save_version",
        {"tag": "backface-repair", "expected_state": next_state},
        context=context,
    )
finally:
    cbt.dispatch(
        "session.release_lease",
        {"lease_id": lease["lease_id"]},
        context=context,
    )
```

## Test

Pure-Python tests do not require Blender or `bpy`:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

After installing `mcp_server/`, validate the real SDK-generated tool schemas and
routing in that Python environment:

```powershell
python -m unittest discover -s mcp_server/tests -p "test_*.py" -v
```

Run the Blender integration smoke tests with a Blender executable (the current test
evidence includes Blender 5.2.0 LTS; Blender 4.x remains untested):

```powershell
blender --background --python tests/run_headless.py
blender --background --python tests/run_geometry_headless.py
blender --background --python tests/run_modeling_headless.py
blender --background --python tests/run_topology_precision_headless.py
blender --background --python tests/run_selection_workplane_headless.py
blender --background --python tests/run_modeling_workflows_headless.py
blender --background --python tests/run_rigging_headless.py
blender --background --python tests/run_architecture_headless.py
```

The pure-Python bridge tests include a real TCP overload case proving that a saturated
connection bound returns explicit backpressure, queue-full and cross-client fairness,
atomic registry concurrency, and two real loopback bridges routed independently by ID
and port. The smoke test covers bridge-panel registration, registry lifecycle, bounded
scene/material summaries and document
identity, optimistic state-token success/stale rejection, document lease
acquire/renew/release, competing-write rejection, lease continuity across a
toolkit-controlled save-as, pressure-aware session identity, exclusive bridge
bind/rebind, full
name/node-pagination traversal, two
non-overwriting checkpoint saves, listing, restore safety gates, confirmed restoration,
preview overwrite refusal and opt-in replacement, pixel-budget rejection, a dry-run and
live allowlisted material patch with a precondition-abort proof, active version saving
and persistence re-inspection, auto-framed and active-camera previews, four-view review
rendering with state restoration and overwrite refusal, structured image comparison,
dimension mismatch handling, and diff output. Generated files are placed in the ignored
`tests/.tmp_headless/` directory. The geometry smoke covers guarded modifier and
Geometry Nodes creation, patching, inspection, reordering, removal, application, and
5.2 modifier-input compatibility. The modeling smoke covers local/world and concave-
face measurement, semantic boundary/coplanar/valence/loop/ring/connected selection,
fill, bridge, triangulate, bisect, dissolve, symmetrize, confirmation gates, resource
budgets, and rejection of an invalid later operation without source-mesh writeback.
The modeling-workflows smoke covers align/distribute and grid/point/plane snap,
native Join preservation of world transforms/materials/UVs/vertex groups, guarded
source removal and copy-join, Join rollback, loose/material/semantic separation,
deterministic result names, context restoration, and result-budget rollback.
The topology-precision smoke covers dry-run and confirmed true loop cut plus signed
directional edge slide, complete-ring enforcement, atomic rejection of ambiguous
slide directions, and face-derived tilted workplane projection/grid snapping with
invalid-reference rejection plus explicit-frame alignment.
The selection/workplane smoke covers deterministic topology paths, material-constrained
region growth, search/result budget rejection, transformed face-derived object frames,
parented-target placement, right-handed axis-map enforcement, and explicit frames on
non-mesh references.
The topology-construction smoke covers exact same-face connect, two-open-chain Grid
Fill with rail inference and closed-loop rejection, single-face Poke, a no-clear exact
plane Bisect/Knife cut, confirmation gates, and rejected-chain atomicity.
The geometry smoke additionally qualifies Blender 5.2 Boolean, Shrinkwrap, Simple
Deform, Curve, Lattice, Remesh, Screw, and Displace contracts; compatible object,
collection, texture, vertex-group, and UV references; Remesh/Screw resource rejection;
typed pointer rollback; and confirmed Boolean application.
`tests/run_hard_surface_workflows_headless.py` executes all four modeling templates in
Blender 5.2, including panel dry-run/application, support geometry, exact cutter binding,
Mirror+Array state chaining, and forced Array-failure compensation.
The rigging smoke covers automatic-weight dry run,
confirmation, native apply, shared-mesh and compute-budget rejection, result-budget
rollback of both created and overwritten groups, and exact restoration of frame, mode,
object/bone selection, active objects/bones, active group, locks, and prior weights.
It also covers owner and cross-datablock driver-dependency rejection, constraint-remove
dry run and confirmation, original-index/field rollback after a later batch failure,
and successful guarded removal.
Their artifacts use `tests/.tmp_geometry_headless/`,
`tests/.tmp_modeling_headless/`, `tests/.tmp_topology_precision_headless/`,
`tests/.tmp_modeling_workflows_headless/`, and
`tests/.tmp_rigging_headless/`.

## Repository layout

```text
addon/blenderboo/  Installable add-on package
mcp_server/                   Standalone typed MCP stdio adapter (official SDK v1)
tests/                        Pure-Python and Blender headless tests
scripts/build_zip.py          ZIP packaging script
dist/                         Generated installable archives (ignored)
```
