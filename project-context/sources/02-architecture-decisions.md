# Architecture Decisions

## Shape

A standalone, transport-agnostic Blender add-on with an authenticated loopback
bridge, plus a separate stdio MCP process outside Blender. The community Blender
MCP was deliberately not forked, and is no longer required or installed.

The bridge stays bound to `127.0.0.1`. Network threads must never call `bpy`
directly. A call waiting in the queue may time out and be cancelled, but once
Blender has begun executing one, the client waits for the authoritative result
rather than reporting an ambiguous timeout.

The official MCP SDK is constrained to `mcp>=1.27,<2` while SDK v2 is
pre-release.

## Execution boundary

The Owner approved unrestricted `python.exec` on 2026-09-24. It executes
through the authenticated bridge on Blender's main thread, under the selected
instance lease and expected-state check. Use typed tools when their contracts
fit; exec covers work they do not express efficiently. Change evidence and
checkpoints support recovery. Do not add an unauthenticated remote surface,
automatic live-plugin update, or implicit mutation retry.

Typed `material.slot_assign` still refuses shared mesh datablocks. An agent
may choose an explicit Python operation if the task calls for that mutation;
verify the resulting sharing and assignments afterward.

## Safety mechanisms are the contract

Leases, state tokens, dry-run and confirmation gates, and field-level recovery
are the substance of the design, not decoration. Domain toolsets, on-demand
discovery, and bounded orchestration were adopted from Unreal MCP work while
keeping these stronger guarantees.

`workflow.run` is bounded orchestration, not a Blender transaction. It stops on
first failure and can only run caller-supplied compensation; checkpoint and
version controls, document leases, state tokens, and each tool's own gates
remain authoritative. When compensation is missing, failed, or of unknown
outcome, `partial_change_possible` stays true.

Workflow idempotency is a bounded replay cache in the MCP process, not durable
Blender state and not a retry policy. A new MCP process forgets prior keys.

Exec can hang Blender's main thread. A lost response does not cancel it; report
the call as possibly running and inspect the instance before continuing. GPU
work from a timer can crash Blender, so use deferred screenshot/render tools.

`workflow.template` definitions are immutable source, not user files and not an
extension mechanism. Expansion returns non-executing steps that must pass
through `workflow.run`.

## Secrets

Never store `CBT_BRIDGE_TOKEN` in this repository, in logs, in packets, or in
project context. Generate a fresh transient token per session and pass the same
value to Blender and to the standalone MCP process.

## Interchange

Format-specific option allowlists rather than arbitrary operator kwargs. glTF
export is `.glb` only; OBJ material sidecars and USD texture copying are
disabled so a staged replacement has exactly one explicit artifact.

## Deferred by design

Material duplication (independent creation plus patching is sufficient), bone
deletion (it can silently invalidate constraints, animation channels, and
deform references), driver expressions and arbitrary RNA paths, and persistent
user-authored templates. Each waits on a design that can snapshot and restore a
dependent graph rather than recreate names.
