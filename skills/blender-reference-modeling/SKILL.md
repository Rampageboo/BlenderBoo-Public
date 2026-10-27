---
name: blender-reference-modeling
description: Reconstruct or refine a Blender asset against supplied photos, drawings, scans, or masks using BlenderBoo. Use when matching a reference matters; not for unconstrained modeling.
---

# Blender Reference Modeling

Use this with the relevant asset skill when form or measurements must match a reference. The editable `.blend` is the source; keep references, camera assumptions, and comparison evidence identifiable.

## Establish the comparison

- Record which references constrain which visible surfaces, dimensions, and views. Mark occluded or inferred geometry as inferred. Agree on tolerances where a measurable match is needed.
- Establish the camera before chasing a silhouette: image size, crop, lens, and orientation. A camera from a solved package or a calibration with inspected pixel residuals counts as established; a hand-placed camera does not support a claim of precise match.
- Fix the reference, camera, crop, object scope, and mask or color rule for each before/after comparison. If any of them changes, label it a new baseline rather than attributing the difference to geometry.

## Judge the result

- One matched view constrains a silhouette, not depth or hidden form. Confirm depth with another view, measured dimensions, or scan evidence.
- Measured comparisons (contour deviation, point-cloud distance, interface checks) decide; a favorable-looking render does not.
- Keep the original reference or scan data separate from proxies and fitted geometry.

## Report

Show matched before and final-state evidence, the final view captured after saving. State the camera and references used, measured residuals and comparison results, unresolved mismatches, and regions that remain inferred or unobserved. A single-view or sparse-scan result is not a complete three-dimensional validation.
