# Codebase Map

Load when a project adopts a repository map.

```yaml
map:
  status: optional derived navigation evidence
  owns: files, symbols, calls, imports, dependencies, flows, impact, as-built topology
  authority: source + tests
project_context_owns: intent, decisions, exact facts, boundaries, risks
failure: blocks map-dependent claims only
```

## Adoption

Record `provider/version`, corpus/data boundary, artifact/Git policy,
maintainer, represented revision, and update trigger. Adoption never implies
semantic transmission, hooks, watchers, CI, global graphs, or committed output.

The provider owns commands, cache, health, hooks, exports, and troubleshooting.
Keep artifacts inside the project's approved corpus, data, retention, and Git
policy. If unavailable or stale, inspect source normally and qualify map-based
claims.

## Freshness and checkpoints

- If only agents read the map, refresh immediately before querying.
- Use automation only for outside-session consumers; record its installation
  location because machine-global automation does not travel with a clone.
- When mapped relationships may change, the code owner synchronizes the affected
  scope before editing and before handoff. Prefer targeted sync; full rebuild only
  when scope cannot be trusted.
- No adopted map or no possible topology change means no checkpoint. Missing map
  evidence weakens only map-dependent claims. Source wins disagreements.
- Behavioral checks do not by themselves prove map freshness or completeness.

Report map state only when it affected the task: provider, represented revision,
query, source checked, freshness/health warnings, and artifact disposition.
