---
name: blender-creature-modeling
description: Model, fit, rig, bake, or review an organic creature or character asset in Blender through the BlenderBoo MCP server.
---

# Blender Creature Modeling

Agree on the body plan, reference views, and the budget and tolerances the requested deliverable needs. Keep the original source untouched for fitting and baking, and keep any fitted game mesh separate. Do not add a rig, bake, or export to a static model request. For photo or scan matching, also use `blender-reference-modeling`.

## Done means

- Silhouette and proportions match the source or concept in matched views, with camera and scale uncertainty stated.
- If fitting a game mesh: connectivity is preserved, required appendages are covered, fit distance and coverage meet the stated tolerances, and the topology report meets the triangle budget, n-gon, loop, and bend-zone rules. Fragments flagged after edits are reviewed.
- If rigging: the skeleton validates, against the Owner's imported Mannequin reference when exact UE humanoid compatibility is requested. Weights meet the influence limit with no unintended unweighted or non-deforming assignments. Range-of-motion results stay within agreed thresholds, reviewed again after body or accessory changes.
- If baking: required maps cover the mesh and come from the untouched original source.
- If exporting: the game asset excludes the high-poly source and passes the requested fresh-import check (`blender-asset-validation`).

If a real source, reference skeleton, or capture is missing, report that qualification as pending; synthetic fixtures are not proof.
