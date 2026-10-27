# Graph Report - <legacy-checkout>  (2026-08-04)

## Corpus Check
- 101 files · ~199,829 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 2075 nodes · 7940 edges · 73 communities (60 shown, 13 thin omitted)
- Extraction: 94% EXTRACTED · 6% INFERRED · 0% AMBIGUOUS · INFERRED: 489 edges (avg confidence: 0.59)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Addon
- Mcp
- Addon
- Tests
- Addon
- Addon
- Addon
- Addon
- Addon
- Addon
- Mcp
- Addon
- Addon
- Scripts
- Addon
- Addon
- Addon
- Addon
- Addon
- Addon
- Addon
- Tests
- Addon
- Addon
- Mcp
- Mcp
- Mcp
- Addon
- Tests
- Tests
- Addon
- Tests
- Addon
- Mcp
- Addon
- Mcp
- Tests
- Addon
- Tests
- Mcp
- Addon
- Addon
- Mcp
- Tests
- Addon
- Addon
- Tests
- Tests
- Tests
- Tests
- Tests
- Tests
- Addon
- Tests
- Skills
- Tests
- Tests
- Mcp
- Skills
- Tests
- Tests
- Tests
- Tests
- Tests
- Tests
- Tests
- Mcp
- Tests
- Tests
- Scripts
- Mcp
- Tests
- Pkg

## God Nodes (most connected - your core abstractions)
1. `InvalidParamsError` - 410 edges
2. `InvalidStateError` - 194 edges
3. `ToolkitProxy` - 162 edges
4. `get_bpy()` - 140 edges
5. `current_state_token()` - 136 edges
6. `NotFoundError` - 135 edges
7. `create_registry()` - 126 edges
8. `MutationApplyError` - 104 edges
9. `bump_epoch()` - 96 edges
10. `check_expected_state()` - 91 edges

## Surprising Connections (you probably didn't know these)
- `AnnotationInspectionHelperTests` --uses--> `InvalidParamsError`  [INFERRED]
  tests/test_annotation_helpers.py → addon/codex_blender_toolkit/errors.py
- `ArchitectureWallPlanningTests` --uses--> `InvalidParamsError`  [INFERRED]
  tests/test_architecture_helpers.py → addon/codex_blender_toolkit/errors.py
- `BridgeTests` --uses--> `InvalidParamsError`  [INFERRED]
  tests/test_bridge.py → addon/codex_blender_toolkit/errors.py
- `CheckpointHelperTests` --uses--> `InvalidParamsError`  [INFERRED]
  tests/test_checkpoint_helpers.py → addon/codex_blender_toolkit/errors.py
- `CompositorHelperTests` --uses--> `InvalidParamsError`  [INFERRED]
  tests/test_compositor_interchange_helpers.py → addon/codex_blender_toolkit/errors.py

## Import Cycles
- None detected.

## Communities (73 total, 13 thin omitted)

### Community 0 - "Addon"
Cohesion: 0.06
Nodes (122): InvalidStateError, Raised when the current Blender state cannot support an operation., _adjacent_elements(), _aggregate(), _apply_operation(), _apply_selection(), _axes(), _axis_index() (+114 more)

### Community 2 - "Addon"
Cohesion: 0.08
Nodes (64): InvalidParamsError, Raised when tool parameters are missing or invalid., _action_fcurves(), _action_for(), _action_summary(), create_action(), _curve_record(), edit_keyframes() (+56 more)

### Community 3 - "Tests"
Cohesion: 0.07
Nodes (46): _action(), _bounded_replay_result(), _compact_replay_result(), _envelope(), _execute_normalized(), _failed_step_change_possible(), _call_step(), _digest() (+38 more)

### Community 4 - "Addon"
Cohesion: 0.11
Nodes (74): create_driver(), create_shape_key(), _driver_record(), _driver_target(), _enum(), _find_named(), inspect_drivers(), inspect_nla() (+66 more)

### Community 5 - "Addon"
Cohesion: 0.10
Nodes (64): NotFoundError, Raised when a requested Blender resource does not exist., Optional handler return type for attaching non-fatal warnings., ToolResult, _compositor(), _format(), inspect_render_manifest(), _issue() (+56 more)

### Community 6 - "Addon"
Cohesion: 0.08
Nodes (39): _acquire_lock(), _empty_registry(), _mutate(), Any, Path, Atomic shared registry for local Blender bridge instances., Create or replace one instance entry without disturbing other processes., Remove only the caller's instance entry. Repeated removal is safe. (+31 more)

### Community 7 - "Addon"
Cohesion: 0.16
Nodes (52): align_object_to_workplane(), _create_mesh_primitive(), create_object(), _custom_property_keys(), _data_collection(), _data_identifier(), delete_object(), _delete_report() (+44 more)

### Community 8 - "Addon"
Cohesion: 0.16
Nodes (51): _apply_operation(), _assign_modifier_input(), create_geometry_nodes(), inspect_geometry_nodes(), _integer(), _interface_direction(), _interface_lookup(), _interface_socket_type() (+43 more)

### Community 9 - "Addon"
Cohesion: 0.17
Nodes (47): _armature_object(), _bone_record(), _bone_spec(), _bone_specs(), _capture_context(), _constraint_dependencies(), _constraint_json(), _constraint_operation() (+39 more)

### Community 10 - "Mcp"
Cohesion: 0.13
Nodes (42): BaseModel, CallToolResult, ImageContent, InstanceRouter, Keep authoritative full responses in a bounded process-local LRU., ResponseArtifactStore, _addon_schema_catalog(), ArchitectureOpening (+34 more)

### Community 11 - "Addon"
Cohesion: 0.21
Nodes (41): Any, vector_values(), _bounds(), _budget(), create_uv_layer(), _enum(), _face_selection(), inspect_uv() (+33 more)

### Community 12 - "Addon"
Cohesion: 0.18
Nodes (41): add_modifier(), apply_modifier(), _attribute_summary(), _bool_item(), _bounds(), _collection_reference(), _enum(), _float_item() (+33 more)

### Community 13 - "Scripts"
Cohesion: 0.09
Nodes (28): BootstrapError, build_config_table(), collect_tokens(), _configured_token_map(), _header_name(), _load_runtime(), main(), Any (+20 more)

### Community 14 - "Addon"
Cohesion: 0.23
Nodes (39): create_registry(), The toolkit's single transport-independent public dispatch entry point., MutationApplyError, An apply-time mutation failure that retains the partial transaction journal., build_walls(), _collection_detail(), _collection_summary(), create_collection() (+31 more)

### Community 15 - "Addon"
Cohesion: 0.15
Nodes (36): ConfirmationRequiredError, NameConflictError, Raised when a destructive operation lacks explicit confirmation., Raised when an exact datablock name is already owned by another item., _created_summary(), _display_options(), export_interchange(), _format() (+28 more)

### Community 16 - "Addon"
Cohesion: 0.12
Nodes (31): get_bpy(), Shared lazy Blender access and JSON conversion helpers., _context(), _entries(), list_checkpoints(), Any, Path, Non-overwriting Blender checkpoint management. (+23 more)

### Community 17 - "Addon"
Cohesion: 0.11
Nodes (23): _bounded_result_summary(), _BridgeCall, _BridgeRequestHandler, _busy_envelope(), _encode_json_line(), _error_envelope(), Any, Authenticated loopback bridge that dispatches Blender work on its main thread. (+15 more)

### Community 18 - "Addon"
Cohesion: 0.18
Nodes (39): _apply_operation(), create_compositor(), _file_output_node(), _file_slots(), _format_summary(), _graph_detail(), inspect_compositor(), _integer() (+31 more)

### Community 19 - "Addon"
Cohesion: 0.21
Nodes (36): add_curve_spline(), _alignment_violation(), create_curve(), _curve_detail(), _curve_object(), _curve_settings(), _distance(), _enum() (+28 more)

### Community 20 - "Addon"
Cohesion: 0.07
Nodes (21): bridge_status(), Return non-secret bridge state., auto_start_enabled(), register_bridge_ui(), unregister_bridge_ui(), dispatch(), Any, Dispatch a named tool. The returned envelope is safe to JSON-encode. (+13 more)

### Community 21 - "Tests"
Cohesion: 0.11
Nodes (9): _BridgeController, _MainThreadExecutor, DeferredEnvelope, Bridge-facing deferred value that still resolves to the stable envelope., Dispatch, BridgeTests, success_dispatch(), Pure-Python tests for the authenticated Blender main-thread bridge. (+1 more)

### Community 22 - "Addon"
Cohesion: 0.15
Nodes (23): _apply_operation(), _flag_value(), inspect_material(), _journal_entry(), _json_safe(), _material_flag_names(), _material_objects(), _node_lookup() (+15 more)

### Community 23 - "Addon"
Cohesion: 0.15
Nodes (20): _build_wall_mesh(), _element_total(), _find_collection(), _integer(), _mesh_counts(), _name(), _number(), _opening() (+12 more)

### Community 24 - "Mcp"
Cohesion: 0.12
Nodes (20): InstanceRecord, InstanceRoutingError, _legacy_port(), _legacy_record(), load_instances(), _normalize_selector(), _pid_is_alive(), Any (+12 more)

### Community 25 - "Mcp"
Cohesion: 0.07
Nodes (5): create_server(), main(), _tool_mode(), RecordingProxy, ServerSchemaTests

### Community 26 - "Mcp"
Cohesion: 0.13
Nodes (16): apply_response_profile(), _bounded_value(), _compact_error(), _compact_warnings(), _json_text(), Any, ResponseMode, Bounded MCP response profiles and process-local full-result artifacts. (+8 more)

### Community 27 - "Addon"
Cohesion: 0.15
Nodes (19): config_path(), ConfigTokenError, _optional_table_span(), Path, RuntimeError, Safe access to the bridge token stored in Codex's MCP configuration., Back up and update only the existing codex-blender config table., Read the authoritative token or create and persist a new one. (+11 more)

### Community 28 - "Tests"
Cohesion: 0.08
Nodes (8): LockedError, Raised when another client holds the active document lease., setter, FailingVisibilityObject, FakeMatrix, FakeObject, ObjectHelperTests, Pure-Python tests for object lifecycle guards and shared destructive gating.

### Community 29 - "Tests"
Cohesion: 0.11
Nodes (14): BridgeClient, BridgeClientError, Any, RuntimeError, socket, Blocking one-request-per-connection client for the Blender loopback bridge., Raised when the local Blender bridge cannot return a trustworthy response., _request_too_large_result() (+6 more)

### Community 30 - "Addon"
Cohesion: 0.27
Nodes (29): assign_material_slot(), bind_material_texture(), create_image(), _cursor(), _finite(), _image(), _image_summary(), inspect_image() (+21 more)

### Community 31 - "Tests"
Cohesion: 0.10
Nodes (9): A small registry that always dispatches into a stable result envelope., Return transport-ready tool metadata without exposing mutable registry state., Return compact domain summaries derived from the registered tools., Return detached specs for one exact toolset name., ToolRegistry, BeforeDispatch, Pure-Python tests for registry, dispatch, and envelope behavior., RegistryTests (+1 more)

### Community 32 - "Addon"
Cohesion: 0.13
Nodes (23): _active_viewport(), _collection_tree(), _default_orbit_distance(), _draw_view3d(), _finite_number(), inspect_scene(), _object_detail(), _object_orbit_target() (+15 more)

### Community 33 - "Mcp"
Cohesion: 0.17
Nodes (18): _additional_properties(), audit_schemas(), _compare_array_items(), _compare_schema(), _constraint(), _deref(), _issue(), _json_type() (+10 more)

### Community 34 - "Addon"
Cohesion: 0.17
Nodes (13): CBT_OT_bridge_copy_token, CBT_OT_bridge_rotate_token, CBT_OT_bridge_start, CBT_OT_bridge_stop, CBT_PT_bridge, CBTAddonPreferences, CBTBridgeSettings, _config_token_for_start() (+5 more)

### Community 35 - "Mcp"
Cohesion: 0.17
Nodes (12): describe_template(), _digest(), expand_template(), _expand_value(), _json_type(), list_templates(), Any, Versioned data-only workflow templates that expand into validated typed steps. (+4 more)

### Community 37 - "Addon"
Cohesion: 0.16
Nodes (14): Public error types used by toolkit tools., active_context(), _base_envelope(), _failure_envelope(), Any, Transport-independent tool registration and structured dispatch., Return the transport context active for the current serialized dispatch., Call a registered tool and always return the stable result envelope. (+6 more)

### Community 38 - "Tests"
Cohesion: 0.14
Nodes (12): Exception, Raised when a mutator's expected document state is no longer current., Base class for errors that callers can act on., StaleStateError, ToolkitError, FakeMaterial, FakeNode, FakeNodes (+4 more)

### Community 39 - "Mcp"
Cohesion: 0.23
Nodes (6): Own chunk state and prevent concurrent execution of the same process-local job., WorkflowJobManager, Pure adapter-side resumable workflow job contract tests; Blender is not…, _start(), _success(), WorkflowJobTests

### Community 40 - "Addon"
Cohesion: 0.35
Nodes (18): acquire_lease(), _client_id(), _document_path(), _DocumentLease, follow_active_document(), _lease_id(), lease_status(), _locked() (+10 more)

### Community 41 - "Addon"
Cohesion: 0.21
Nodes (16): _cancel_screenshot_job(), _clear_active_screenshot_job(), _finalize_screenshot_job(), Exception, Remove every registered UI callback independently., Cancel UI resources after the bridge has already completed the waiter., Capture only inside a real View3D WINDOW draw context., Save captured CPU pixels outside the GPU draw callback and resolve MCP. (+8 more)

### Community 42 - "Mcp"
Cohesion: 0.25
Nodes (8): _digest(), _normalize_instance(), _normalize_job(), plan_batch(), Any, Data-only planning for explicitly attributed multi-instance workflow batches., Pure adapter-side batch-planning contract tests; Blender is not required., WorkflowBatchTests

### Community 43 - "Tests"
Cohesion: 0.17
Nodes (5): Stop the bridge and cancel calls that have not begun executing., stop_bridge(), clear_lease(), Clear process-local lease state during bridge shutdown or test cleanup., FakeTimers

### Community 44 - "Addon"
Cohesion: 0.23
Nodes (4): _DeferredOutcome, DeferredToolResult, Exception, A tool result completed later by Blender's UI/draw lifecycle.

### Community 46 - "Tests"
Cohesion: 0.17
Nodes (5): Flatten Blender's UBYTE GPU buffer and normalize it for Image.pixels., _ubyte_rgba_to_pixels(), array, FakeBuffer, FakeFramebuffer

### Community 47 - "Tests"
Cohesion: 0.23
Nodes (9): Mesh, mesh_counts(), mismatched_join_counts(), object_from_bmesh(), point_object(), BMesh, Object, Blender background qualification for precision and multi-object modeling. (+1 more)

### Community 48 - "Tests"
Cohesion: 0.27
Nodes (3): fake_bpy(), Pure-Python tests for expiring, client-attributed document leases., SessionLeaseTests

### Community 49 - "Tests"
Cohesion: 0.18
Nodes (5): Built-in Blender tool handlers. Modules must not import bpy at import time., cube_object(), Object, Blender background qualification for guarded modifiers and Geometry Nodes., Pure-Python version-name allocation tests.

### Community 50 - "Tests"
Cohesion: 0.18
Nodes (4): CompositorHelperTests, InterchangeHelperTests, Pure-Python contract tests for compositor, render-manifest, and interchange…, RenderManifestHelperTests

### Community 52 - "Addon"
Cohesion: 0.31
Nodes (8): prepare_destructive_action(), Any, Shared dry-run and confirmation infrastructure for risky document changes., Build a transaction and decide whether an operation may apply. The returned…, _strict_bool(), new_transaction(), Any, Return JSON-safe mutation metadata without depending on clock or randomness.

### Community 53 - "Tests"
Cohesion: 0.28
Nodes (4): dict, GeometryNodesInputCompatibilityTests, _LegacyModifier, Pure-Python compatibility tests for Geometry Nodes modifier input storage.

### Community 54 - "Skills"
Cohesion: 0.36
Nodes (8): FreeTypeFont, ImageFont, _font(), main(), _prepare_output(), Path, render(), _wrap_lines()

### Community 56 - "Tests"
Cohesion: 0.22
Nodes (3): FakeImage, FakeImages, FakePixels

### Community 58 - "Skills"
Cohesion: 0.43
Nodes (7): Namespace, _append_report(), main(), _markdown_cell(), Path, _reject_sensitive(), _resolve_outputs()

### Community 59 - "Tests"
Cohesion: 0.32
Nodes (6): object_from_bmesh(), BMesh, Object, Blender background qualification for semantic mesh modeling and measurement., require_ok(), selection_plan()

### Community 60 - "Tests"
Cohesion: 0.32
Nodes (5): cube(), edge_indices(), face_pair(), Object, Blender background qualification for deterministic topology construction.

### Community 61 - "Tests"
Cohesion: 0.36
Nodes (5): cube(), loop_slide_operations(), Object, Blender background qualification for true loop cut and directional edge slide., vertical_edge_index()

### Community 62 - "Tests"
Cohesion: 0.36
Nodes (3): ModifierSafetyTests, owner(), Pure-Python tests for modifier resource guards and typed snapshots.

### Community 65 - "Tests"
Cohesion: 0.48
Nodes (4): CompletedProcess, Path, RecordLearningTests, _run()

### Community 67 - "Tests"
Cohesion: 0.47
Nodes (5): bounded_debug_json(), Blender background smoke test for the v0.5 add-on tools., Retain useful failure evidence without flooding Blender or MCP logs., report_pass(), require_ok()

### Community 69 - "Scripts"
Cohesion: 0.50
Nodes (3): build(), Path, Build an installable Blender add-on ZIP with the package at archive root.

## Knowledge Gaps
- **1 isolated node(s):** `codex-blender-mcp`
  These have ≤1 connection - possible missing edges or undocumented components.
- **13 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `InvalidParamsError` connect `Addon` to `Addon`, `Addon`, `Addon`, `Addon`, `Addon`, `Addon`, `Addon`, `Addon`, `Addon`, `Addon`, `Addon`, `Addon`, `Addon`, `Addon`, `Addon`, `Tests`, `Addon`, `Addon`, `Tests`, `Addon`, `Tests`, `Addon`, `Tests`, `Addon`, `Tests`, `Addon`, `Addon`, `Tests`, `Addon`, `Addon`, `Tests`, `Tests`, `Addon`, `Tests`, `Tests`, `Tests`, `Tests`, `Tests`, `Tests`?**
  _High betweenness centrality (0.336) - this node is a cross-community bridge._
- **Why does `InvalidStateError` connect `Addon` to `Addon`, `Addon`, `Addon`, `Addon`, `Addon`, `Addon`, `Addon`, `Addon`, `Addon`, `Addon`, `Addon`, `Addon`, `Addon`, `Addon`, `Tests`, `Addon`, `Addon`, `Tests`, `Addon`, `Tests`, `Addon`, `Addon`, `Tests`, `Addon`, `Tests`, `Tests`, `Tests`, `Tests`, `Tests`?**
  _High betweenness centrality (0.128) - this node is a cross-community bridge._
- **Why does `ToolkitProxy` connect `Mcp` to `Mcp`, `Mcp`, `Mcp`, `Mcp`, `Mcp`, `Tests`, `Mcp`?**
  _High betweenness centrality (0.101) - this node is a cross-community bridge._
- **Are the 48 inferred relationships involving `InvalidParamsError` (e.g. with `_BridgeCall` and `_BridgeController`) actually correct?**
  _`InvalidParamsError` has 48 INFERRED edges - model-reasoned connections that need verification._
- **Are the 23 inferred relationships involving `InvalidStateError` (e.g. with `_BridgeCall` and `_BridgeController`) actually correct?**
  _`InvalidStateError` has 23 INFERRED edges - model-reasoned connections that need verification._
- **Are the 25 inferred relationships involving `ToolkitProxy` (e.g. with `BridgeClient` and `ArchitectureOpening`) actually correct?**
  _`ToolkitProxy` has 25 INFERRED edges - model-reasoned connections that need verification._
- **What connects `codex-blender-mcp` to the rest of the system?**
  _1 weakly-connected nodes found - possible documentation gaps or missing edges._