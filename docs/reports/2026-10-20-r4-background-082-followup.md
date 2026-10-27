# R4 background follow-up, BlenderBoo 0.8.2 (2026-10-20)

This follows the Owner-supplied `2026-10-20-helmet-r4-081-replay.md` report.
That report measured 0.0024% colour misses on the Helmet at 2K and a 43-minute
background run. The code changes below do not alter colour projection or
normal-map results. No further Helmet production bake was run for this fix.

## Disposition

1. **43-minute background run:** A bounded four-way 256² test and a matched
   two-way 1024² test did not reproduce a launch-flag slowdown. At 1024²,
   normal-window execution took 33.266 s wall / 34.031 s CPU; the same work
   with `CREATE_NO_WINDOW` took 22.718 s wall / 25.781 s CPU. Both children
   retained Normal priority, peaked at 53 threads, and used AUTO / 16 render
   threads. One ordered synthetic comparison does not explain the Helmet run
   or prove a general speed advantage. Launch flags remain unchanged.
   `2026-10-20-r4-background-flags-1024.json` preserves the measurements.
   Runtime progress now includes child CPU and wall time so a future slow
   production job can distinguish computation from CPU starvation.
2. **`fallback_max_distance`:** A fresh MCP process already advertised this
   optional number (0–1000 scene units) and forwarded it to the add-on. The
   reported missing field is consistent with a client retaining an older tool
   schema; that cause was not observed directly. The field description now
   states its default, and README instructs a Codex/Claude restart after an
   adapter upgrade.
3. **`operation.status(wait_ms)`:** The background-job lookup previously
   ignored `wait_ms`. It now waits on the job completion event until the
   requested timeout, at most 30 seconds. The bridge network thread does not
   call `bpy` while waiting. An isolated loopback BridgeClient test requested
   6000 ms and returned after 6.016 s with the job still running and zero
   Blender dispatches; `2026-10-20-r4-background-wait6.json` records it.
4. **Progress:** The bake child now reports ray bake, nearest projection,
   source cache, material, normal, AO, and map-writing stages. Nearest
   projection reports processed/total texels and geometric hit counts. A
   file-only heartbeat updates every five seconds and includes child wall/CPU
   time. `operation.status` exposes the child's current phase and timestamp.

Targeted unit tests passed 103/103. Direct and real-background-child headless
Blender tests passed. The 256² progress probe observed processed counts from
0 to 65,536 and advancing timestamps. All 72 installed add-on files were
SHA-256 verified against source. A fresh installed Blender 5.2.2 and editable
MCP 0.8.2 passed doctor and schema audit (184 tools, zero errors/warnings);
receipts are `2026-10-20-blenderboo-082-addon-smoke.json` and
`2026-10-20-blenderboo-082-adapter-smoke.json`. Production Helmet timing remains
unexplained; the new telemetry should be read during the next comparable run.
