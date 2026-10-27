# BlenderBoo 0.8.0 rename evidence

2026-10-20. The Owner chose BlenderBoo for the software, GitHub repository,
and local folder, accepting the naming and extension-listing risk described
by the [Blender trademark policy](https://www.blender.org/about/trademark-policy/)
and [extension terms](https://extensions.blender.org/terms-of-service/).
BlenderBoo is independent of Blender Foundation and HoYoverse. No third-party
logos or Bangboo artwork were added.

## Verified

- GitHub reports `<original-private-repository>` on `main`; this checkout's `origin`
  uses `<original-private-repository-url>`.
- Add-on import package and installed Blender 5.2 folder: `blenderboo`;
  distribution ZIP: `blenderboo-0.8.0.zip`. All 72 files in the installed
  package matched the ZIP by SHA-256. The old installed 5.2
  `codex_blender_toolkit` add-on was removed after the new isolated smoke.
- MCP import package and editable distribution: `blenderboo_mcp` /
  `blenderboo-mcp` 0.8.0 from `<checkout>/mcp_server`.
  `codex-blender-mcp` remains a console alias in the new distribution for
  compatibility. Existing `CBT_*` environment variables, `cbt_*` document
  data and legacy Blender operator IDs remain stable.
- Source verification: add-on pure tests 346/346, MCP tests 140/140; Blender
  5.2.2 headless source smoke passed. Live add-on/adapter schema audit found
  184 add-on tools, 0 errors and 0 warnings.
- Fresh installed smoke from the final folder: add-on and adapter 0.8.0,
  doctor healthy, `python.exec` returned 42, schema aligned, four calls logged
  with bridge token redacted.
- The Blender 5.2 saved user preferences were migrated after an isolated-copy
  rehearsal: `codex_blender_toolkit` was disabled, `blenderboo` enabled, and
  preferences saved. The twelve unrelated enabled add-ons stayed unchanged.
  A separate fresh normal-user Blender process automatically loaded the new
  add-on, found the old entry absent, and emitted no missing-module warning.
  The GUI event-loop bridge auto-start remains unqualified by background mode.
- Exact Codex and Claude user-global MCP command/env entries both initialized
  as BlenderBoo and listed 198 tools through stdio. Old global
  `codex-blender` registrations were removed after those checks, without
  changing the copied bridge token.
- Five canonical Blender skills passed their quick validator. The global
  `ai-dotfiles` commit `f0d805d` was synced locally and pushed, with project
  skill wording updated to BlenderBoo. Earlier divergences in creature and
  hard-surface completion criteria were reconciled additively in the global
  source; neither side's existing criteria were discarded.
- The new independent checkout `<checkout>` matches all 1,365
  non-Git files in the original folder byte-for-byte, including untracked
  Owner files and ignored build artifacts. Git metadata came from the
  renamed GitHub repository, and the new checkout is the canonical folder.

Evidence files are under
`<redacted-evidence-directory>`.
In particular, `final-installed/` and `config-stdio-result.json` cover the
installed and actual-config checks.

## Retired checkout cleanup

- A direct move was initially blocked by Codex, Claude and code-index
  processes. They were not terminated. After they released the directory, the
  old checkout was renamed to
  `<checkout>.retired-20261020-191800` for review.
- Immediately before deletion, all 1,365 retired non-Git files existed in
  BlenderBoo; the only differing common file was `exact-facts.yaml`, where the
  new checkout records `verified: 2026-10-20` in place of the old pending
  value. The old `main` was an ancestor of new `main`, its local branches were
  imported with exact SHA matches, stash was empty, and no process had the
  retired folder as its working directory.
- The entire old `.git` directory, including reflogs and unreachable objects,
  was SHA-verified in the ignored local archive
  `dist/retired-codex-blender-git-20261020-191800.zip`. Additional archival
  refs remain in the new repository. The retired folder was then removed.
- The old `codex-blender-mcp` 0.7.4 editable distribution and copied ignored
  egg-info/pycache were removed. `blenderboo-mcp` 0.8.0 still imports from
  `<checkout>/mcp_server`.
- Codex desktop project listing now reads label `BlenderBoo` and path
  `<checkout>`; the old path is absent from that listing.

## Remaining live qualification

- Interactive GUI and already-running Codex/Claude sessions were not restarted
  or qualified by the fresh-process checks. Restart both clients and Blender
  before expecting them to use the new registration and add-on.
