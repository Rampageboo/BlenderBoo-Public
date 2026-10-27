---
name: blender-python-scripting
description: Write or debug custom Blender Python through BlenderBoo when its typed tools cannot express a BMesh, node, modifier, shader, animation, simulation, or scene operation. Not for ordinary asset completion or toolkit source-code changes.
---

# Blender Python Scripting

Tool behavior and limits are in the BlenderBoo tool descriptions. This skill covers Blender API judgment that those descriptions do not.

## Resolve the API against the running Blender

- Treat remembered or published API lists as leads. The running Blender's RNA, node interfaces, and operator signatures decide; check `api.lookup` and the official API docs for the running version when an identifier matters.
- Prefer direct `bpy.data` edits for datablocks. Use an operator only when necessary, after checking mode, active object, selection, and context; a script that works in one viewport may fail in background or bridge-timer context.
- In Edit Mode, edit through BMesh and update the edit mesh; object-mode mesh arrays do not hold the current edit. For evaluated geometry, read the evaluated object and release temporary meshes.
- Address node inputs through the live node/group interface and stable identifiers, not socket indices or labels from another version.
- Further per-domain pitfalls: [domain routes](references/domain-routes.md).

## Work safely in a shared document

- One writer per open `.blend`. Parallel agents work in separate Blender instances on separate files and integrate deliberately.
- Keep each script small enough that its effects can be inspected, then inspect the affected objects, data, or images. A script that raised may have changed state already.
- Run long blocking work, such as simulation bakes, in a separate headless Blender on a copy.

The output is the requested change or read result, the inspected state, and any API or version assumption left unresolved.
