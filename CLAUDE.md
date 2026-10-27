@AGENTS.md

# BlenderBoo: Claude-specific notes

Follow the project contract in `AGENTS.md`, `docs/SPEC.md`, and
`docs/ARCHITECTURE.md`. Inspect the actual tool catalog and repository source
before claiming a capability is missing. Coordinate parallel edits through
non-overlapping file ownership and return concrete evidence to the Owner.

Claude and Codex may each plan, implement, or review. Treat each view
independently, challenge claims with evidence, and follow the Owner's explicit
assignment. Do not route work through an external collaboration protocol.

Tool behavior for modeling agents lives in the tool descriptions and MCP
server instructions; asset completion criteria live in `skills/`. Keep this
file to development guidance.
