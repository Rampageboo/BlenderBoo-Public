# BlenderBoo rename contract

Owner decision, 2026-10-20: rename the software, GitHub repository, and local
folder to BlenderBoo. The Owner accepted the Blender Foundation naming and
extension-listing risk after reviewing its
[trademark policy](https://www.blender.org/about/trademark-policy/) and
[extension terms](https://extensions.blender.org/terms-of-service/).
This supersedes the earlier unexecuted Blender Agent Bridge name proposal.

## Identities

| Surface | New identity |
|---|---|
| Product / Blender add-on display | BlenderBoo |
| GitHub | `<original-private-repository>` |
| Local checkout | `<checkout>` |
| Add-on import package / installed folder | `blenderboo` |
| MCP import package | `blenderboo_mcp` |
| MCP distribution / primary console script | `blenderboo-mcp` |
| User-global MCP registration key | `blenderboo` in Codex and Claude |
| ZIP | `blenderboo-0.8.0.zip` with `blenderboo/` at archive root |
| Next version | `0.8.0` |

## Compatibility and boundaries

- Preserve all persisted `cbt_*` `.blend` fields and text datablocks, `CBT_*`
  environment variables, bridge wire protocol and port 9877. Do not copy
  Bangboo/ZZZ or Blender logos into the repository.
- Keep legacy Blender operator `codex_blender.*` idnames and `CBT_*` RNA class
  names for scripts; change visible panel, category and add-on preferences to
  BlenderBoo. A user-facing rename need not break saved or scripted IDs.
- Add-on token access reads `mcp_servers.blenderboo` first, then the old
  `mcp_servers.codex-blender` table. Token writes target the table in use and
  never log values. Both global clients are switched only after the new
  adapter/add-on smoke test succeeds.
- New instance registry: `~/.blenderboo/instances.json`. New writes use it;
  reads fall back to `~/.codex/codex_blender_instances.json` when the new file
  is absent. `CBT_INSTANCE_REGISTRY` remains authoritative when set. Both the
  add-on and adapter must implement the same rule.
- New default call log: `~/.blenderboo/call-logs/`; `CBT_CALL_LOG_DIR` still
  overrides. Historical log files are left in place.
- Keep `codex-blender-mcp` as a console-script alias for one release if it can
  point to the new module without retaining the old import package. Source
  imports should use the new Python packages.
- Historical `docs/v0.*`, task evidence, and referenced source scripts remain
  historical. Current README, SPEC, ARCHITECTURE, AGENTS, CLAUDE, project
  context, skills and tests use the new name and path.
- The two code scopes own disjoint files: add-on agent `addon/` and its scoped
  tests; MCP agent `mcp_server/` and its scoped tests. Root owns scripts,
  documentation, skills, installation, configuration, GitHub, folder move and
  cross-scope integration. Test execution belongs to an independent Verifier.

## Cutover evidence

1. Source package imports, unit/headless tests and add-on/MCP schema audit.
2. Fresh Blender 5.2 process imports the installed new add-on. Its doctor
   reports version 0.8.0 and a fresh global adapter call succeeds.
3. Both user-global client registrations name `blenderboo`, invoke
   `blenderboo_mcp.server`, preserve their token, and point to the renamed
   checkout. Old registration entries are removed after verification.
4. GitHub reports `<original-private-repository>`, origin points there, and the local
   checkout is `<checkout>`. All unrelated untracked work moves
   with the checkout. The old installed add-on is removed so two auto-starting
   bridges cannot bind the same port.
