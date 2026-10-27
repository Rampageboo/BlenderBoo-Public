# Risks and Gotchas

Hard-won, expensive to rediscover, and mostly invisible from reading one file.

## GPU work must never run from the bridge timer callback

This one hard-crashes Blender. `bpy.ops.render.opengl(view_context=True)` under
repeated calls from `bpy.app.timers` crashed immediately, and a timer-owned
`GPUOffScreen.draw_view3d` survived 20 sequential calls before crashing when two
conversations issued screenshots concurrently.

The accepted pattern: the timer submits one deferred job; a real
`SpaceView3D.draw_handler_add(..., "WINDOW", "POST_PIXEL")` callback binds
GPUOffScreen, draws, reads UBYTE pixels, and frees GPU state; a later non-GPU
timer saves the PNG and resolves the waiter. While a deferred call is active,
later calls stay queued so only one capture enters a draw cycle. The draw
callback must use explicitly captured space, region, and matrices rather than
`bpy.context.area/region` identity, because Blender can retain the timer's
global context during a valid draw. Flatten the framebuffer through its public
`dimensions` property, never as raw bytes.

**Sequential success does not qualify a GPU change.** Any change here needs
simultaneous multi-client stress, visual checks, and PID/port liveness checks.

## Upgrading the add-on inside a running Blender always fails

Enable can report `cannot import name ... from blenderboo.dispatcher` (or the
pre-0.8 `codex_blender_toolkit.dispatcher`).
`__init__.py` re-executes but submodules stay cached in `sys.modules`. The
installed files are fine — restart Blender, or purge `blenderboo*`
from `sys.modules`. This recurs on every upgrade; it is not a packaging defect.

## A passing schema audit does not prove the client sees the right types

`adapter.schema_audit` compares add-on and adapter schemas. It cannot prove how
a client renders the declaration or that handler types match. The
`object.create`/`object.transform` declaration kept a stale-looking string-array
surface after restart while the live handler accepted numeric vectors. After
every adapter update, restart the client and make a harmless real typed call.

## Windows process liveness

Python 3.11 `os.kill(other_pid, 0)` can raise `SystemError` wrapping WinError 87
while the process is alive. MCP instance filtering must use
`OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` plus `GetExitCodeProcess` on
Windows; keep `os.kill` only for other hosts.

## Blender 5.2 API shifts that broke working code

- Collection membership is name-based: `target in collection.children` raises
  `TypeError`. Use `collection.children.get(target.name) is target`.
- `Bone.select` was removed from the object-mode RNA; the selection surface is
  `PoseBone.select`.
- Boolean solvers are `FLOAT`, `EXACT`, `MANIFOLD` — the older `FAST` spelling
  is gone.
- Geometry Nodes inputs come through typed
  `modifier.properties.inputs.<id>.value/type/attribute_name` wrappers and
  `modifier.keys()` raises. Keep the older ID-property path too.
- `mathutils.geometry.tessellate_polygon` can return triangle indices for
  concave polygons where older behaviour gave vector triples. Accept both.
- Actions use slots, layers, keyframe strips, and channelbags rather than a
  guaranteed `action.fcurves`. Traverse both layouts.

## A screenshot is not render evidence

`scene.screenshot` is a fast solid-viewport geometry review. Aesthetic decisions
need camera, light, world, and render settings plus `render.preview` and image
comparison. Keep screenshots for modeling checks only.

## Do not claim a document is clean

A create/edit/delete qualification against a saved document still leaves Blender
`dirty:true`: `object.delete` intentionally retains zero-user data for orphan
management, and the typed surface exposes non-overwriting version and checkpoint
saves rather than overwrite-save or revert. Start mutation qualification from a
disposable `.blend`, or take a checkpoint before the first mutation.

## Never overwrite the only good `.blend`

Save staged versions before any systemic operation.

## Scene-modelling judgement

Owner feedback prioritizes silhouette, natural "ordered irregularity", density,
and branch/leaf relationships over clean topology. Moderate intersections are
acceptable; floating parts and implausible growth relationships are not. Never
reconnect a floating branch to whatever is spatially closest — respect
hierarchy. A single beauty render is insufficient for structural review.

## The map has known edge damage

The 2026-08-04 build reported 437 dangling-endpoint edges and 837 collapsed
edges. Use it to locate source, then read that source.
