---
name: blender-asset-validation
description: Validate a Blender asset or deliverable against stated requirements using BlenderBoo. Use for completion reviews, handoffs, or independent QA; not for open-ended modeling.
---

# Blender Asset Validation

Validate the requested deliverable against its stated acceptance criteria and its final saved state. Use the hard-surface or creature skill for asset-specific criteria. Source-file quality, visual match, and export behavior are separate; one does not prove the others.

## Check the evidence

- Identify the intended asset, references, required interfaces, topology or rig constraints, output format, and tolerances. If a criterion lacks a reference or threshold, report the gap; do not invent a pass.
- When the editable `.blend` is part of the deliverable, inspect it and its confirmed disk save. For export-only QA, inspect the export in an isolated review scene and mark source-file quality unverified.
- Use broad final views plus close-ups where visual judgment matters, and fixed, matched reference views for comparisons. Change the reviewed scene only when the review scope allows it, preferably in a copy.
- Use measured checks for interfaces, mesh limits, and scan agreement. Read their acceptance and uncertainty fields; a successful tool call is not a passed criterion, and review findings such as fragments or isolated parts call for inspection, not automatic failure.
- If an export is requested, inspect the exported file after a fresh import or the supported round trip. Do not impose an export on a Blender-only deliverable.

## Report

For each material criterion: pass, fail, or unverified, with the measurement or evidence path and the reason for any gap. Distinguish unit/headless checks from live Blender review. Final images and numbers must refer to the saved deliverable; rerun affected checks after any edit. Call counts, timings, and response sizes come from `session.call_log_summary`.

## AI-source hard-surface game assets

Before building, inspect source structure with sections or ray maps and state the triangle budget, texture size, and expected viewing distance. Preserve a designed low-poly form rather than following every noisy high-poly bump.

For this class of deliverable, require an editable all-quad source and triangulate only for export. Check continuous edge loops except at deliberate breaks, crisp hard edges, fair arcs, consistent symmetric parts, and integrated rails, slots, and attachments. No sliver or zero-area faces and no unintended self-intersection or part-to-part penetration may remain. Full-view renders do not prove these conditions; use measured quality, source-comparison, UV, and interface evidence with task-specific thresholds. Report unavailable measurements as unverified rather than a pass.
