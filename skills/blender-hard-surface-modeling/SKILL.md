---
name: blender-hard-surface-modeling
description: Model, edit, or review hard-surface, mechanical, or architectural assets in Blender through the BlenderBoo MCP server. Not for organic creatures or characters.
---

# Blender Hard-Surface Modeling

Tool behavior, hazards, and limits are in the BlenderBoo tool descriptions
and server instructions. This skill defines what a finished asset means.

## Before building

State the done criteria for this asset in a few lines: which references it
must match, which part interfaces matter, and the tolerances. Use the
references the task provides or the user points to; if none are given, look
in the working directory and confirm with the user before modeling parts
whose fidelity depends on them. Do not assume reference locations from
earlier sessions.

When matching a photograph or scan, use `blender-reference-modeling` to
separate measured visible features from inferred hidden structure. Fix the
reference camera and crop before judging a geometry edit. Check another view
before accepting depth inferred from one projection.

## Done means

- **Form matches the references.** Silhouettes and key sections agree with the
  references in views matched to them, and key dimensions are within the
  stated tolerance.
- **Interfaces are built, not implied.** Where parts meet, the mounting and
  transition structure visible in the references exists (tabs, pins, bosses,
  fillets, recesses). `assembly.analyze` with explicit expectations passes:
  intended contacts are contact, nothing intersects unintentionally, and no
  part floats unless it is meant to.
- **Detail lives in the editable Blender source.** Normals, UV seams, and
  object identity survive refinement; an externally generated mesh that
  must be regenerated for every change does not count as the source.
- **Evidence is current.** Each key interface has a close-up beside its
  reference from the final state. Review the reverse side and any secondary
  functional area included in the request. Numbers come from tools, not from
  memory. A quick two-view repair check does not replace final view coverage.
- **The file is saved** and the save is confirmed on disk.

For an AI-source game prop, first inspect the source structure and set a triangle
budget, texture size, target viewing distance, and reference tolerances. Build a
fair low-poly form with an editable all-quad source; triangulate only the export.
Check continuous edge loops except at deliberate breaks, crisp hard edges,
fair arcs, symmetric counterparts, and actual rail/slot/mount geometry. Measure
slivers, zero-area faces, self-intersections, part penetrations, silhouette and
section fidelity, UV density and overlap, and bake coverage. A clean full-view
render is insufficient evidence. Report missing or incomplete checks as
unverified; the Unreal import and collision need separate Unreal-side evidence.
- **Requested exports are checked.** When a game or exchange format is part of
  the deliverable, use `blender-asset-validation` to inspect the exported file
  after a fresh import; a correct editable `.blend` alone does not prove the
  export retained geometry, materials, hierarchy, or dimensions.

When a criterion cannot be met, say which one and why rather than reporting
the asset as done.

## Reports

Counts, timings, errors, and response sizes in a report or retrospective come
from `session.call_log_summary`. If no log exists, say so.
