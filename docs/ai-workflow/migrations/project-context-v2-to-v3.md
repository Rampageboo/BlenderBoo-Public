# Project Context V2 to V3 Migration

V3 removes the default `system-map` module and narrows architecture and current
state so repository-derived structure can live in a codebase map. The canonical
map-versus-Project-Context boundary is in `../codebase-map.md`; the steps below
apply it to this migration.

1. Adopt and build the project map, or explicitly choose source inspection as
   the fallback.
2. Verify map health and represented source revision.
3. Move code-derived components, ownership, calls, imports, dependencies,
   flows, and as-built architecture out of `02-system-map` and
   `03-architecture`.
4. Retain normative architecture decisions and external boundaries in
   `02-architecture-decisions`.
5. Retain only a durable current stage, active transition, blocker, or next
   endpoint in `03-project-state`; move routine progress and validation logs to
   the task record or archive.
6. Rename decisions, risks, and research modules to the V3 layout.
7. Update `index.yaml` and review all deletions and renames.

Do not delete old structural modules before the replacement map or fallback is
verified. Archive historical context under the project's retention policy; do
not leave it active as a competing source of truth.
