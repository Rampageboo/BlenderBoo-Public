# Project State

Verification revision and date are maintained in `index.yaml` and
`exact-facts.yaml`; read those fields rather than copying a revision from prose.

## Where the work is

The modeling core is the active track: measurement, selection, topology, the
modifier lifecycle, reusable hard-surface workflows, workplanes, bounded
spin/screw, and read-only Annotation inspection are all in the typed surface.
Rendering and look development have a real but early pass. Dependency-aware
Join/Separate inputs, retopology, and sculpt remain later layers.

Read the current tool catalog from `system.doctor` and
`adapter.schema_audit` rather than from any written count — those numbers move
with every slice and a stale one is worse than none.

## Validation shape

Three layers, all local: pure-Python tests under `tests/`, official MCP SDK
adapter tests under `mcp_server/tests/`, and Blender headless runs such as
`tests/run_headless.py` against the Blender qualification target named in
`exact-facts.yaml`. Discover the current test set from those directories rather
than relying on a written count.
A live round additionally needs `system.doctor`, `adapter.schema_audit`, and a
real typed call after restarting the client.

Running the MCP suite inside a sandbox can hit Windows access denials on the
default Temp directory. That is an environment failure, not a product failure —
re-run with a workspace-owned `TMP`/`TEMP`.

## Environments

Developer-specific host and checkout records are omitted from the public snapshot.

## Codebase map

A local AST-only Graphify map is adopted for source navigation. Its represented
revision, corpus scope, metrics, and latest health result are maintained under
`codebase_map` in `exact-facts.yaml`; consult those fields before using it. The
map is a locator, so read source before making a consequential claim even when
its recorded health is clean.

## Superseded context

`PROJECTCONTEXT.md` at the project root provides a short public context; private session notes are omitted.
