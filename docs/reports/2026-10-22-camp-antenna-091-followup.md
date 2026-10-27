# BlenderBoo 0.9.1 camp-antenna follow-up (2026-10-22)

The Owner requested repair and verification of the four findings in the local
`2026-10-22-camp-antenna-090-test.md`. That report tested a live 0.9.0 MCP
session against a partially retopologized camp antenna. This change updates
the toolkit; it does not save or export that production asset.

## Changes

1. **Cached `mesh.join` defaults:** the named `object_names` + `name` route
   accepts and ignores the old default `keep_sources=false`,
   `max_elements=500000`, `fragment_small_ratio=0.01`, and optional
   `confirm=true`. It still preserves all named source objects. Non-default
   legacy controls refuse, so a caller cannot silently change join semantics.
   The add-on dispatcher and MCP adapter agree. Named apply uses
   `dry_run=false`; the earlier target/source route still requires confirmation
   to apply. Both descriptions now state this distinction.
2. **Cached client schemas:** `system.doctor` includes a SHA-256 digest for
   each current add-on input schema and a client-restart notice. MCP server
   instructions explain that an adapter upgrade requires the MCP client to
   restart before changed parameters on existing tools become visible. A
   doctor digest can reveal a mismatch; the server cannot rewrite an
   already-running client's cached declaration.
3. **Slab direction:** `mesh.build_slab` documents and reports
   `thickness_direction=negative_outline_normal`. The supplied outline is
   the top face; a counter-clockwise XY outline grows toward negative Z.
4. **Detail clipping:** `render.detail(clip_to_bounds=true)` requires explicit
   bounds and sets the temporary camera near/far planes to their projected
   depth interval. A top-view fixture showed a lower object disappearing
   while the in-bounds subject remained. The camera and render state were
   restored. This is camera-depth clipping, not an exact 3D box bisect;
   out-of-depth objects may still affect lighting or shadows.

## Verification and deployment

- Python checks: registry 15/15, hard-surface builder 13/13, schema audit
  9/9, MCP server 47/47, add-on metadata 1/1, all exit 0. The MCP
  instructions measured 908 UTF-8 bytes under the existing 1200-byte gate.
- Blender 5.2.2 headless fixtures for named join, slab and rendering all
  exited 0. The render fixture compared clipped and unclipped pixels, checked
  the lower object was absent from the clipped image, and verified temporary
  camera cleanup. `git diff --check` exited 0.
- Fresh installed smoke exited 0 with `INSTALLED_DEPLOYMENT_OK`. A new
  Blender 5.2.2 process loaded the real user-installed add-on 0.9.1 and the
  configured editable MCP adapter 0.9.1. `system.doctor` was healthy with
  **187 per-tool schema digests**; `adapter.schema_audit` found **187 tools,
  zero errors and zero warnings**. Real MCP calls accepted the cached join
  defaults, preserved its sources, and returned `camera_depth` for a clipped
  detail render. The call-log token redaction check passed.
- All 73 installed add-on `.py`/`.json` files matched source SHA-256. The
  editable `blenderboo-mcp` package reports 0.9.1 and the installable ZIP is
  `dist/blenderboo-0.9.1.zip`. Persistent receipts:
  `2026-10-22-blenderboo-091-addon-smoke.json` and
  `2026-10-22-blenderboo-091-adapter-smoke.json`.

The production CampAntenna `.blend`, source GLB, textures and exports were
not modified. This synthetic installed smoke does not establish a retested
camp-antenna render or successful use of new parameters in the existing
Claude/Codex sessions. Restart Blender and the MCP clients to load the new
add-on and refresh their cached tool declarations, then repeat the production
close-up and remaining 0.9 feature checks.
