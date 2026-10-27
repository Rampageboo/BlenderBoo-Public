# Decisions

## Resolved

- **BlenderBoo product name** (2026-10-20, Owner). Renames the software,
  GitHub repository, and local folder. The Owner accepted the Blender
  Foundation trademark and extension-listing risk after reviewing its
  published policy. Public UI and package identities change; persisted
  `cbt_*` document fields and `CBT_*` environment variables stay compatible.
  The previous unexecuted Blender Agent Bridge name proposal is superseded.
- **Standalone transport-agnostic add-on, not a fork** of the community Blender
  MCP (2026-07-12, category B). The community MCP is uninstalled and no longer
  required.
- **Authenticated loopback bridge inside the add-on plus a separate stdio MCP
  process** (2026-07-12, owner-direct). The official SDK stays outside Blender.
- **Broad practical Blender-native coverage through high-level typed tools**
  (2026-07-14, Owner, category C), not a one-to-one `bpy` wrapper. Staged plan
  in `docs/v0.5-v1-capability-roadmap.md`.
- **Adopt Unreal MCP patterns without weakening guarantees** (2026-07-16,
  Owner). Domain toolsets, on-demand discovery, diagnostics, and bounded
  orchestration were taken; the stronger authentication, leases, state tokens,
  dry-run gates, and field-level recovery were kept. Explicitly rejected: an
  unauthenticated remote surface, automatic live-plugin update, implicit
  mutation retry. (The arbitrary-Python rejection was reversed on 2026-09-24.)
- **Unrestricted Python execution alongside typed tools** (2026-09-24, Owner).
  Reverses the 2026-07-16 rejection: that rejection rested on reviewer models
  approving implausible results, which the Owner judges no longer holds. No
  AST/import restriction; safety comes from the authenticated loopback bridge,
  instance lease, state tokens, checkpoints, and returned change evidence.
  Typed tools remain the preferred path where they fit. Plan:
  `docs/v0.6-python-exec-and-lean-responses.md`.
- **Modeling before rendering** (2026-07-17, Owner, category C). Retopology was
  initially deprioritized.
- **Creature retopology and rigging are a product goal** (2026-09-25, Owner).
  Reverses the 2026-07-17 retopology priority for organic creatures. A source
  mesh is a fitting/baking target; the deliverable is a built, budgeted mesh,
  skeleton, weights, maps, and deformation evidence. Plan:
  `docs/v0.7-creature-modeling-and-rig.md`.
- **Git mode differs per host.** `codex-git` on the Codex Desktop machine,
  `claude-git` here.

## Pending

- **Shared-datablock material policy for the typed tool.**
  `material.slot_assign` refuses shared mesh data; a typed
  `make_single_user` surface has not been designed. An explicit `python.exec`
  operation may be used when the active task requires it, followed by
  inspection of mesh sharing and material assignments.
- **Hierarchy and world-transform diagnostics.** Static source confirms the
  world-transform surface and guards, but cannot verify the recorded live
  observation that a camera moved while a pre-existing `Light` was refused.
  That observation remains unverified by this audit until reproduced in Blender.
  The underlying parent/world transform evidence is not normalized or
  regression-qualified.
- **Bone deletion, driver expressions, NLA and constraint cascades** all wait on
  a design that can snapshot and restore the dependent graph.
- **Persistent or user-authored workflow templates** wait on versioning, trust,
  and migration policy.
