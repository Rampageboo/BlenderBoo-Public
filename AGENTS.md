# BlenderBoo: project instructions

## Goal and scope

Build a reliable local Blender control layer. The Blender add-on owns tool
registration, document state, and execution on Blender's main thread. The MCP
adapter owns client-facing declarations, instance routing, workflows, and
response profiles. The deliverable is the toolkit, not a particular 3D scene.

Read `docs/SPEC.md` for behavior and acceptance criteria and
`docs/ARCHITECTURE.md` for component boundaries. Verify exact behavior in source;
older project reports and the generated `graphify-out/` map may be stale.
The retained `docs/ai-workflow/` files are legacy distribution artifacts and
do not govern this project.

## Working rules

- Claude and Codex may each plan, implement, or review. Treat their views
  independently, test claims against source, and follow the Owner's assignment.
  Do not adopt an external collaboration protocol.
- Inspect the affected path and current Git diff before editing. Preserve
  unrelated work, especially the Owner's uncommitted skill and decision edits.
- Make the smallest complete change. Keep add-on and MCP schemas aligned;
  `adapter.schema_audit` checks the live contract.
- `python.exec` intentionally runs unrestricted Python. Never call `bpy` from
  a network thread; bridge requests execute through Blender's main-thread
  queue, and GPU capture or GUI rendering requires deferred execution.
- How each tool behaves for a modeling agent (limits, hazards, result
  semantics) belongs in its tool description and the MCP server
  instructions, kept identical between add-on and adapter. Asset completion
  criteria belong in `skills/`. Do not restate either here; when tool
  behavior changes, update its description in the same change.
- When testing against a live Blender, never retry a mutation automatically
  after a timeout or ambiguous result; inspect the operation and scene first.
  Restart Blender after upgrading the add-on.
- Use `session.call_log_summary` for measured call counts, timing, and
  response sizes in a report. Do not reconstruct these numbers from memory.
- Protect bridge tokens. The bridge binds to `127.0.0.1` and requires the
  session token. Do not print or commit token values.
- Distinguish unit tests, headless Blender tests, and live desktop/client
  qualification. Report missing evidence precisely.
- On this project, delegate test execution to a Verifier agent when agents
  are available. Keep parallel write scopes disjoint.
- For parallel live testing, use separate Blender instances and files; one
  writer per open `.blend`.

## Practical entry points

- Add-on: `addon/blenderboo/dispatcher.py`, `registry.py`,
  `bridge.py`, and `tools/`.
- MCP: `mcp_server/src/blenderboo_mcp/server.py`, `workflow.py`, and
  `response_profiles.py`.
- Tests: `tests/`, `mcp_server/tests/`, and Blender headless runners under
  `tests/run_*_headless.py`.
- Local call log: `mcp_server/src/blenderboo_mcp/call_log.py` and
  `scripts/summarize_call_log.py`. The add-on's direct dispatcher bypasses
  this adapter log.
- Build: `python scripts/build_zip.py`. Restart Blender after upgrading the
  add-on; restarting only the MCP process does not reload Blender modules.
- Skills: `skills/blender-hard-surface-modeling/` and
  `skills/blender-creature-modeling/` define asset-specific completion;
  `skills/blender-reference-modeling/`, `skills/blender-asset-validation/`,
  and `skills/blender-python-scripting/` cover reference comparison,
  independent QA, and custom Blender API work. The running add-on catalog is
  authoritative for tool fields.
- For AI-source game assets, maintain the completion criteria in the relevant
  project skills.

The Owner's latest request defines the task. Use subagents only when the Owner
requests them or the task has genuinely independent, bounded scopes.
