# Blender-Codex Plugin Issue Report

Date recorded: 2026-08-01 03:22:59
Observed during: an architectural modeling session
Host application: Blender 5.2.0 LTS

This report contains plugin and bridge issues only. Asset-design decisions, floor-plan interpretation errors, and ordinary modelling revisions are intentionally excluded.

## Confirmed plugin issues

| Area | Observed plugin behaviour | Impact | Workaround used |
| --- | --- | --- | --- |
| Call latency | Many Blender-Codex operations took approximately 2-10 seconds per request | Dozens of simple wall operations accumulated substantial communication delay | Grouped compatible operations into bounded per-level workflows |
| Bulk architectural modelling | No direct operation was available for creating a full wall run with multiple door openings | Walls had to be built from many box segments and joined later | Created segmented walls in batches and joined them by level |
| Workflow reliability | A long `workflow.run` request stopped after 23 completed steps | The scene was left with a partial result that required inspection before continuation | Inspected existing objects and completed only the missing operations |
| Instance routing | More than one Blender instance was temporarily registered during an active workflow | The bridge rejected later calls until an explicit instance was selected | Re-ran instance discovery and supplied the instance ID on subsequent calls |
| Parameter consistency | Nested `object.create` rejected expression strings and required literal numeric arrays | A corrected workflow failed before creating geometry | Verified non-mutation and resubmitted numeric vectors |
| Nested tool availability | `material.slot.assign` worked as a direct call but was unavailable inside `workflow.run` | Rename-and-material workflow stopped after the first rename | Performed renaming and material assignment as separate passes |
| Mesh-join budget | A valid mesh join was rejected because the default/requested element budget was too small | Additional diagnostic and retry calls were required | Confirmed that no mutation occurred, then used a larger bounded budget |
| Response verbosity | Successful create and join calls returned extensive transforms, topology, journals, and state data for every component | Increased processing time and made important status harder to identify | Reduced displayed results to progress and error summaries where orchestration allowed it |
| Annotation semantics | Annotation inspection returned stroke coordinates without semantic labels | The plugin could not distinguish walls, doors, corridors, room dividers, or handwritten text | Calculated stroke bounds and combined them with explicit user descriptions |
| In-place save | No standard save-current-document command was exposed | Saving the main `.blend` required a separate UI-control system | Activated the exact Blender window and sent `Ctrl+S`, then verified `dirty: false` |
| Checkpoint destination | Plugin checkpoints were written to `Blender/checkpoints` | The output did not match the repository convention `checkpoint/Blender` | Moved and renamed each checkpoint after creation |
| Review isolation | Review screenshots did not provide convenient object-level isolation through the initial interface used | Unrelated campus objects appeared in early review images | Organized edited objects into a dedicated collection and rendered that collection |

## Requested plugin improvements

1. Add a normal in-place scene-save command.
2. Keep instance routing stable throughout a running workflow.
3. Make direct-call and nested-workflow parameter formats consistent.
4. Expose the same supported tools inside and outside `workflow.run`.
5. Add compact success responses for large batch operations.
6. Add architectural wall-generation tools with multiple door and window openings.
7. Allow checkpoint destination and filename templates to be configured.
8. Add annotation grouping or optional semantic labels.
9. Add object-level isolation for clean review screenshots.

## Evidence from this session

- Workflow interruption occurred after 23 completed steps when instance routing changed.
- A nested material-assignment step returned `Unknown tool: material.slot.assign` while the direct tool remained callable.
- A mesh join reported 300 total elements above the requested limit of 10 and required a second bounded request.
- Standard scene saving required Windows UI automation rather than a Blender-Codex save operation.
- Checkpoints required relocation from `Blender/checkpoints` to `checkpoint/Blender`.
