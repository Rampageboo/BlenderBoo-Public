"""Typed Python facade used by the MCP transport adapter."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Literal
import time

from .client import BridgeClient
from .call_log import workflow_parent

supplied_tool_arguments: ContextVar[tuple[str, frozenset[str]] | None] = ContextVar(
    "supplied_tool_arguments", default=None
)


class _VersionPreflightBusy(Exception):
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result


class ToolkitProxy:
    def __init__(self, client: BridgeClient) -> None:
        self.client = client
        self.expected_addon_version: str | None = None
        self._selected_instance: str | int | None = None
        self._addon_version: str | None = None
        self._live_schemas: dict[str, set[str]] = {}

    def _version_warning(self, instance: str | int | None) -> str | None:
        expected = self.expected_addon_version
        if expected is None:
            return None
        if self._addon_version is None or instance != self._selected_instance:
            self._selected_instance = instance
            self._live_schemas.clear()
            kwargs = {} if instance is None else {"instance": instance}
            doctor = self.client.call("system.doctor", {}, **kwargs)
            failure = doctor.get("error") if isinstance(doctor.get("error"), dict) else {}
            if failure.get("type") == "BridgeBusyError":
                raise _VersionPreflightBusy(doctor)
            data = doctor.get("data") if isinstance(doctor.get("data"), dict) else {}
            self._addon_version = data.get("addon_version") if isinstance(data.get("addon_version"), str) else None
            if self._addon_version is None:
                raise ValueError("Cannot read live add-on version; restart Blender and retry")
        if self._addon_version and self._addon_version != expected:
            return f"Adapter {expected} and add-on {self._addon_version} differ; restart Blender to load add-on {expected}"
        return None

    def _validate_live_fields(self, tool: str, params: dict[str, Any], instance: str | int | None, warning: str) -> None:
        if tool.startswith("bridge.") or tool in {"system.doctor", "system.capabilities"}:
            return
        allowed = self._live_schemas.get(tool)
        if allowed is None:
            request = {"toolset": tool.split(".", 1)[0], "include_schemas": True, "limit": 200}
            kwargs = {} if instance is None else {"instance": instance}
            catalog = self.client.call("system.capabilities", request, **kwargs)
            data = catalog.get("data") if isinstance(catalog.get("data"), dict) else {}
            specs = data.get("tools") if isinstance(data.get("tools"), list) else []
            spec = next((item for item in specs if isinstance(item, dict) and item.get("name") == tool), None)
            schema = spec.get("input_schema") if isinstance(spec, dict) else None
            properties = schema.get("properties") if isinstance(schema, dict) else None
            if not isinstance(properties, dict):
                raise ValueError(f"Cannot verify live schema for {tool}; {warning}")
            allowed = set(properties)
            self._live_schemas[tool] = allowed
        unknown = set(params) - allowed
        if unknown:
            raise ValueError(f"Live add-on schema for {tool} does not declare {', '.join(sorted(unknown))}; {warning}")

    def _call(
        self,
        tool: str,
        params: dict[str, Any],
        instance: str | int | None,
    ) -> dict[str, Any]:
        supplied = supplied_tool_arguments.get()
        if supplied is not None and supplied[0] == tool:
            params = {key: value for key, value in params.items() if key in supplied[1]}
        try:
            warning = self._version_warning(instance) if tool not in {"system.doctor", "bridge.operation_status"} else None
        except _VersionPreflightBusy as exc:
            busy = {**exc.result, "tool": tool}
            meta = busy.get("meta") if isinstance(busy.get("meta"), dict) else {}
            addon_version = meta.get("addon_version")
            if isinstance(addon_version, str) and addon_version != self.expected_addon_version:
                busy["warnings"] = [*busy.get("warnings", []),
                                    f"Adapter {self.expected_addon_version} and add-on {addon_version} differ; restart Blender to load add-on {self.expected_addon_version}"]
            return busy
        if warning and supplied is not None and supplied[0] == tool:
            self._validate_live_fields(tool, params, instance, warning)
        parent = workflow_parent.get()
        logger = getattr(self, "call_log", None) if parent else None
        seq = logger.next_seq() if logger else None
        target_instance, target_blend = logger.target_context(instance, self.client) if logger else (None, None)
        started = time.perf_counter()
        try:
            if instance is None:
                result = self.client.call(tool, params)
            else:
                result = self.client.call(tool, params, instance=instance)  # type: ignore[call-arg]
            meta = result.get("meta") if isinstance(result, dict) else None
            live_version = meta.get("addon_version") if isinstance(meta, dict) else None
            if isinstance(live_version, str):
                if live_version != self._addon_version:
                    self._live_schemas.clear()
                self._addon_version = live_version
                warning = self._version_warning(instance)
            if warning and isinstance(result, dict):
                warnings = result.get("warnings")
                if isinstance(warnings, list) and warning not in warnings:
                    warnings.append(warning)
            if logger:
                logger.record(seq=seq, tool=tool, params=params, result=result, elapsed_ms=(time.perf_counter() - started) * 1000, instance=target_instance, blend_file=target_blend, parent=parent)
            return result
        except Exception as exc:
            if logger:
                logger.record(seq=seq, tool=tool, params=params, error=exc, elapsed_ms=(time.perf_counter() - started) * 1000, instance=target_instance, blend_file=target_blend, parent=parent)
            raise

    def call_named(
        self,
        tool: str,
        arguments: dict[str, Any],
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        """Call one authoritative add-on tool by exact registered name."""
        if (
            not isinstance(tool, str)
            or not tool
            or tool.startswith("toolsets.")
            or tool in {"workflow.run", "workflow.template", "workflow.batch_plan", "workflow.job_start", "workflow.job_control", "adapter.schema_audit"}
        ):
            raise ValueError("tool must name a non-meta add-on capability")
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        if tool == "session.instances":
            if arguments:
                raise ValueError("session.instances does not accept arguments")
            return self.session_instances()
        return self._call(tool, arguments, instance)

    def system_capabilities(
        self,
        toolset: str | None = None,
        include_schemas: bool = False,
        limit: int = 50,
        cursor: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"include_schemas": include_schemas, "limit": limit}
        if toolset is not None:
            params["toolset"] = toolset
        if cursor is not None:
            params["cursor"] = cursor
        return self._call("system.capabilities", params, instance)

    def system_doctor(self, instance: str | int | None = None) -> dict[str, Any]:
        return self._call("system.doctor", {}, instance)

    def session_instances(self) -> dict[str, Any]:
        list_instances = getattr(self.client, "list_instances", None)
        if list_instances is None:
            data = {
                "count": 1,
                "default_instance": f"port-{self.client.port}",
                "registry_path": None,
                "instances": [
                    {
                        "id": f"port-{self.client.port}",
                        "pid": None,
                        "port": self.client.port,
                        "filepath": "",
                        "started": None,
                        "token_location": "configured-client",
                    }
                ],
            }
        else:
            data = list_instances()
        return {
            "ok": True,
            "tool": "session.instances",
            "data": data,
            "error": None,
            "warnings": [],
        }

    def scene_inspect(
        self,
        object_name: str | None = None,
        include_names: bool = False,
        limit: int = 200,
        cursor: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"include_names": include_names, "limit": limit}
        if object_name is not None:
            params["object_name"] = object_name
        if cursor is not None:
            params["cursor"] = cursor
        return self._call("scene.inspect", params, instance)

    def annotation_inspect(
        self,
        annotation_name: str | None = None,
        layer_name: str | None = None,
        frame_number: int | None = None,
        stroke_index: int | None = None,
        limit: int = 200,
        cursor: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        for key, value in (
            ("annotation_name", annotation_name),
            ("layer_name", layer_name),
            ("frame_number", frame_number),
            ("stroke_index", stroke_index),
            ("cursor", cursor),
        ):
            if value is not None:
                params[key] = value
        return self._call("annotation.inspect", params, instance)

    def scene_screenshot(
        self,
        output_path: str | None = None,
        overwrite: bool = False,
        width: int | None = None,
        height: int | None = None,
        instance: str | int | None = None,
        view: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"overwrite": overwrite}
        for key, value in (("output_path", output_path), ("width", width), ("height", height)):
            if value is not None:
                params[key] = value
        if view is not None:
            params["view"] = view
        return self._call("scene.screenshot", params, instance)

    def scene_fast_capture(self, params: dict[str, Any], instance: str | int | None = None) -> dict[str, Any]:
        return self._call("scene.fast_capture", params, instance)

    def assembly_analyze(self, params: dict[str, Any], instance: str | int | None = None) -> dict[str, Any]:
        return self._call("assembly.analyze", params, instance)

    def object_inspect(
        self,
        name: str | None = None,
        name_regex: str | None = None,
        object_type: str | None = None,
        collection: str | None = None,
        visible: bool | None = None,
        selected: bool | None = None,
        parent: str | None = None,
        material: str | None = None,
        custom_property: str | None = None,
        custom_value: str | float | bool | None = None,
        object_name: str | None = None,
        limit: int = 200,
        cursor: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        for key, value in (
            ("name", name),
            ("name_regex", name_regex),
            ("type", object_type),
            ("collection", collection),
            ("visible", visible),
            ("selected", selected),
            ("parent", parent),
            ("material", material),
            ("custom_property", custom_property),
            ("custom_value", custom_value),
            ("object_name", object_name),
            ("cursor", cursor),
        ):
            if value is not None:
                params[key] = value
        return self._call("object.inspect", params, instance)

    def collection_inspect(
        self,
        name: str | None = None,
        name_regex: str | None = None,
        parent: str | None = None,
        hidden_viewport: bool | None = None,
        hidden_render: bool | None = None,
        custom_property: str | None = None,
        custom_value: str | float | bool | None = None,
        collection_name: str | None = None,
        limit: int = 200,
        cursor: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        for key, value in (
            ("name", name),
            ("name_regex", name_regex),
            ("parent", parent),
            ("hidden_viewport", hidden_viewport),
            ("hidden_render", hidden_render),
            ("custom_property", custom_property),
            ("custom_value", custom_value),
            ("collection_name", collection_name),
            ("cursor", cursor),
        ):
            if value is not None:
                params[key] = value
        return self._call("collection.inspect", params, instance)

    def collection_create(
        self,
        name: str,
        parent: str | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name}
        if parent is not None:
            params["parent"] = parent
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("collection.create", params, instance)

    def collection_rename(
        self,
        collection_name: str,
        name: str,
        allow_suffix: bool = False,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "collection_name": collection_name,
            "name": name,
            "allow_suffix": allow_suffix,
        }
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("collection.rename", params, instance)

    def collection_set_visibility(
        self,
        collections: str | list[str],
        hide_viewport: bool | None = None,
        hide_render: bool | None = None,
        hide_select: bool | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"collections": collections}
        for key, value in (
            ("hide_viewport", hide_viewport),
            ("hide_render", hide_render),
            ("hide_select", hide_select),
            ("expected_state", expected_state),
        ):
            if value is not None:
                params[key] = value
        return self._call("collection.set_visibility", params, instance)

    def collection_link_objects(
        self,
        collection: str,
        objects: str | list[str],
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"collection": collection, "objects": objects}
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("collection.link_objects", params, instance)

    def collection_unlink_objects(
        self,
        collection: str,
        objects: str | list[str],
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "collection": collection,
            "objects": objects,
            "dry_run": dry_run,
        }
        if confirm is not None:
            params["confirm"] = confirm
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("collection.unlink_objects", params, instance)

    def collection_parent_objects(self, collection: str, parent: str | None, recursive: bool = False, keep_transform: bool = True, max_objects: int = 2000, dry_run: bool = True, expected_state: str | None = None, instance: str | int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"collection": collection, "parent": parent, "recursive": recursive, "keep_transform": keep_transform, "max_objects": max_objects, "dry_run": dry_run}
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("collection.parent_objects", params, instance)

    def collection_transform(self, collection: str, translation: tuple[float, float, float] = (0, 0, 0), rotation_euler: tuple[float, float, float] = (0, 0, 0), scale: tuple[float, float, float] = (1, 1, 1), recursive: bool = False, max_objects: int = 2000, dry_run: bool = True, expected_state: str | None = None, instance: str | int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"collection": collection, "translation": list(translation), "rotation_euler": list(rotation_euler), "scale": list(scale), "recursive": recursive, "max_objects": max_objects, "dry_run": dry_run}
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("collection.transform", params, instance)

    def collection_link_child(
        self,
        parent: str,
        child: str,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"parent": parent, "child": child}
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("collection.link_child", params, instance)

    def collection_unlink_child(
        self,
        parent: str,
        child: str,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"parent": parent, "child": child, "dry_run": dry_run}
        if confirm is not None:
            params["confirm"] = confirm
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("collection.unlink_child", params, instance)

    def collection_delete(
        self,
        collections: str | list[str],
        recursive: bool = False,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "collections": collections,
            "recursive": recursive,
            "dry_run": dry_run,
        }
        if confirm is not None:
            params["confirm"] = confirm
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("collection.delete", params, instance)

    def camera_inspect(
        self,
        name: str | None = None,
        active: bool | None = None,
        object_name: str | None = None,
        limit: int = 200,
        cursor: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        for key, value in (("name", name), ("active", active), ("object_name", object_name), ("cursor", cursor)):
            if value is not None:
                params[key] = value
        return self._call("camera.inspect", params, instance)

    def camera_patch(
        self,
        object_name: str,
        type: str | None = None,
        lens: float | None = None,
        ortho_scale: float | None = None,
        clip_start: float | None = None,
        clip_end: float | None = None,
        shift_x: float | None = None,
        shift_y: float | None = None,
        sensor_width: float | None = None,
        sensor_height: float | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name}
        for key, value in (
            ("type", type), ("lens", lens), ("ortho_scale", ortho_scale),
            ("clip_start", clip_start), ("clip_end", clip_end),
            ("shift_x", shift_x), ("shift_y", shift_y),
            ("sensor_width", sensor_width), ("sensor_height", sensor_height),
            ("expected_state", expected_state),
        ):
            if value is not None:
                params[key] = value
        return self._call("camera.patch", params, instance)

    def camera_set_active(
        self,
        object_name: str | None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name}
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("camera.set_active", params, instance)

    def light_inspect(
        self,
        name: str | None = None,
        type: str | None = None,
        object_name: str | None = None,
        limit: int = 200,
        cursor: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        for key, value in (("name", name), ("type", type), ("object_name", object_name), ("cursor", cursor)):
            if value is not None:
                params[key] = value
        return self._call("light.inspect", params, instance)

    def light_patch(
        self,
        object_name: str,
        type: str | None = None,
        energy: float | None = None,
        color: tuple[float, float, float] | None = None,
        use_shadow: bool | None = None,
        shadow_soft_size: float | None = None,
        shape: str | None = None,
        size: float | None = None,
        size_y: float | None = None,
        spot_size: float | None = None,
        spot_blend: float | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name}
        for key, value in (
            ("type", type), ("energy", energy), ("color", color),
            ("use_shadow", use_shadow), ("shadow_soft_size", shadow_soft_size),
            ("shape", shape), ("size", size), ("size_y", size_y),
            ("spot_size", spot_size), ("spot_blend", spot_blend),
            ("expected_state", expected_state),
        ):
            if value is not None:
                params[key] = list(value) if key == "color" else value
        return self._call("light.patch", params, instance)

    def world_inspect(
        self,
        name: str | None = None,
        active: bool | None = None,
        world_name: str | None = None,
        limit: int = 200,
        cursor: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        for key, value in (("name", name), ("active", active), ("world_name", world_name), ("cursor", cursor)):
            if value is not None:
                params[key] = value
        return self._call("world.inspect", params, instance)

    def world_create(
        self,
        name: str,
        color: tuple[float, float, float] | None = None,
        use_nodes: bool = False,
        background_color: tuple[float, float, float] | None = None,
        background_strength: float | None = None,
        set_active: bool = False,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name, "use_nodes": use_nodes, "set_active": set_active}
        for key, value in (
            ("color", color), ("background_color", background_color),
            ("background_strength", background_strength), ("expected_state", expected_state),
        ):
            if value is not None:
                params[key] = list(value) if key in {"color", "background_color"} else value
        return self._call("world.create", params, instance)

    def world_patch(
        self,
        world_name: str,
        color: tuple[float, float, float] | None = None,
        use_nodes: bool | None = None,
        background_color: tuple[float, float, float] | None = None,
        background_strength: float | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"world_name": world_name}
        for key, value in (
            ("color", color), ("use_nodes", use_nodes),
            ("background_color", background_color),
            ("background_strength", background_strength),
            ("expected_state", expected_state),
        ):
            if value is not None:
                params[key] = list(value) if key in {"color", "background_color"} else value
        return self._call("world.patch", params, instance)

    def world_set_active(
        self,
        world_name: str | None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"world_name": world_name}
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("world.set_active", params, instance)

    def render_settings(
        self,
        engine: str | None = None,
        resolution_x: int | None = None,
        resolution_y: int | None = None,
        resolution_percentage: int | None = None,
        film_transparent: bool | None = None,
        fps: int | None = None,
        fps_base: float | None = None,
        frame_start: int | None = None,
        frame_end: int | None = None,
        frame_step: int | None = None,
        image_format: str | None = None,
        filepath: str | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
        cycles_samples: int | None = None,
        cycles_use_denoising: bool | None = None,
        eevee_use_raytracing: bool | None = None,
        view_transform: str | None = None,
        look: str | None = None,
        exposure: float | None = None,
        gamma: float | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        for key, value in (
            ("engine", engine), ("resolution_x", resolution_x),
            ("resolution_y", resolution_y),
            ("resolution_percentage", resolution_percentage),
            ("film_transparent", film_transparent), ("fps", fps),
            ("fps_base", fps_base), ("frame_start", frame_start),
            ("frame_end", frame_end), ("frame_step", frame_step),
            ("image_format", image_format), ("filepath", filepath),
            ("expected_state", expected_state),
            ("cycles_samples", cycles_samples),
            ("cycles_use_denoising", cycles_use_denoising),
            ("eevee_use_raytracing", eevee_use_raytracing),
            ("view_transform", view_transform),
            ("look", look),
            ("exposure", exposure),
            ("gamma", gamma),
        ):
            if value is not None:
                params[key] = value
        return self._call("render.settings", params, instance)

    def mesh_inspect(
        self,
        object_name: str,
        include_topology: bool = False,
        max_elements: int = 250000,
        limit: int = 200,
        cursor: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name,
            "include_topology": include_topology,
            "max_elements": max_elements,
            "limit": limit,
        }
        if cursor is not None:
            params["cursor"] = cursor
        return self._call("mesh.inspect", params, instance)

    def mesh_measure(
        self,
        object_name: str,
        queries: list[dict[str, Any]],
        max_elements: int = 500000,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        return self._call(
            "mesh.measure",
            {
                "object_name": object_name,
                "queries": queries,
                "max_elements": max_elements,
            },
            instance,
        )

    def mesh_edit(
        self,
        object_name: str,
        operations: list[dict[str, Any]],
        max_elements: int = 500000,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
        fragment_small_ratio: float | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name,
            "operations": operations,
            "max_elements": max_elements,
            "dry_run": dry_run,
        }
        if confirm is not None:
            params["confirm"] = confirm
        if expected_state is not None:
            params["expected_state"] = expected_state
        if fragment_small_ratio is not None:
            params["fragment_small_ratio"] = fragment_small_ratio
        return self._call("mesh.edit", params, instance)

    def mesh_join(
        self,
        target_object: str,
        source_objects: list[str],
        keep_sources: bool = False,
        max_elements: int = 500000,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
        fragment_small_ratio: float | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "target_object": target_object,
            "source_objects": source_objects,
            "keep_sources": keep_sources,
            "max_elements": max_elements,
            "dry_run": dry_run,
        }
        if confirm is not None:
            params["confirm"] = confirm
        if expected_state is not None:
            params["expected_state"] = expected_state
        if fragment_small_ratio is not None:
            params["fragment_small_ratio"] = fragment_small_ratio
        return self._call("mesh.join", params, instance)

    def mesh_separate(
        self,
        object_name: str,
        mode: Literal["LOOSE", "MATERIAL", "SELECTION"],
        selection: dict[str, Any] | None = None,
        name_prefix: str | None = None,
        max_parts: int = 100,
        max_elements: int = 500000,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
        fragment_small_ratio: float | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name,
            "mode": mode,
            "max_parts": max_parts,
            "max_elements": max_elements,
            "dry_run": dry_run,
        }
        if selection is not None:
            params["selection"] = selection
        if name_prefix is not None:
            params["name_prefix"] = name_prefix
        if confirm is not None:
            params["confirm"] = confirm
        if expected_state is not None:
            params["expected_state"] = expected_state
        if fragment_small_ratio is not None:
            params["fragment_small_ratio"] = fragment_small_ratio
        return self._call("mesh.separate", params, instance)

    def curve_inspect(
        self,
        name: str | None = None,
        object_name: str | None = None,
        include_points: bool = False,
        max_points: int = 10000,
        limit: int = 200,
        cursor: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "include_points": include_points,
            "max_points": max_points,
            "limit": limit,
        }
        for key, value in (("name", name), ("object_name", object_name), ("cursor", cursor)):
            if value is not None:
                params[key] = value
        return self._call("curve.inspect", params, instance)

    def curve_create(
        self,
        spline: dict[str, Any],
        name: str | None = None,
        collection: str | None = None,
        location: tuple[float, float, float] | None = None,
        rotation: tuple[float, float, float] | None = None,
        scale: tuple[float, float, float] | None = None,
        dimensions: str | None = None,
        resolution_u: int | None = None,
        render_resolution_u: int | None = None,
        fill_mode: str | None = None,
        bevel_depth: float | None = None,
        bevel_resolution: int | None = None,
        twist_smooth: int | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"spline": spline}
        for key, value in (
            ("name", name), ("collection", collection), ("location", location),
            ("rotation", rotation), ("scale", scale), ("dimensions", dimensions),
            ("resolution_u", resolution_u), ("render_resolution_u", render_resolution_u),
            ("fill_mode", fill_mode), ("bevel_depth", bevel_depth),
            ("bevel_resolution", bevel_resolution), ("twist_smooth", twist_smooth),
            ("expected_state", expected_state),
        ):
            if value is not None:
                params[key] = value
        return self._call("curve.create", params, instance)

    def curve_patch(
        self,
        object_name: str,
        dimensions: str | None = None,
        resolution_u: int | None = None,
        render_resolution_u: int | None = None,
        fill_mode: str | None = None,
        bevel_depth: float | None = None,
        bevel_resolution: int | None = None,
        twist_smooth: int | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name}
        for key, value in (
            ("dimensions", dimensions), ("resolution_u", resolution_u),
            ("render_resolution_u", render_resolution_u), ("fill_mode", fill_mode),
            ("bevel_depth", bevel_depth), ("bevel_resolution", bevel_resolution),
            ("twist_smooth", twist_smooth), ("expected_state", expected_state),
        ):
            if value is not None:
                params[key] = value
        return self._call("curve.patch", params, instance)

    def curve_spline_add(
        self,
        object_name: str,
        spline: dict[str, Any],
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name, "spline": spline}
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("curve.spline_add", params, instance)

    def curve_spline_patch(
        self,
        object_name: str,
        spline_index: int,
        spline: dict[str, Any],
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name,
            "spline_index": spline_index,
            "spline": spline,
        }
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("curve.spline_patch", params, instance)

    def curve_spline_remove(
        self,
        object_name: str,
        spline_index: int,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name,
            "spline_index": spline_index,
            "dry_run": dry_run,
        }
        if confirm is not None:
            params["confirm"] = confirm
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("curve.spline_remove", params, instance)

    def geometry_nodes_inspect(
        self,
        object_name: str | None = None,
        modifier_name: str | None = None,
        include_graph: bool = False,
        max_nodes: int = 2000,
        max_links: int = 10000,
        limit: int = 200,
        cursor: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "include_graph": include_graph,
            "max_nodes": max_nodes,
            "max_links": max_links,
            "limit": limit,
        }
        for key, value in (
            ("object_name", object_name), ("modifier_name", modifier_name), ("cursor", cursor),
        ):
            if value is not None:
                params[key] = value
        return self._call("geometry_nodes.inspect", params, instance)

    def geometry_nodes_create(
        self,
        object_name: str,
        modifier_name: str | None = None,
        node_group_name: str | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name}
        for key, value in (
            ("modifier_name", modifier_name), ("node_group_name", node_group_name),
            ("expected_state", expected_state),
        ):
            if value is not None:
                params[key] = value
        return self._call("geometry_nodes.create", params, instance)

    def geometry_nodes_patch(
        self,
        object_name: str,
        operations: list[dict[str, Any]],
        modifier_name: str | None = None,
        expected_group: str | None = None,
        expected_node_count: int | None = None,
        expected_link_count: int | None = None,
        make_single_user: bool = False,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name,
            "operations": operations,
            "make_single_user": make_single_user,
            "dry_run": dry_run,
        }
        for key, value in (
            ("modifier_name", modifier_name), ("expected_group", expected_group),
            ("expected_node_count", expected_node_count), ("expected_link_count", expected_link_count),
            ("confirm", confirm), ("expected_state", expected_state),
        ):
            if value is not None:
                params[key] = value
        return self._call("geometry_nodes.patch", params, instance)

    def geometry_nodes_modifier_inputs(
        self,
        object_name: str,
        values: dict[str, Any],
        modifier_name: str | None = None,
        expected_group: str | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name, "values": values}
        for key, value in (
            ("modifier_name", modifier_name), ("expected_group", expected_group),
            ("expected_state", expected_state),
        ):
            if value is not None:
                params[key] = value
        return self._call("geometry_nodes.modifier_inputs", params, instance)

    def uv_inspect(
        self,
        object_name: str,
        layer_name: str | None = None,
        include_loops: bool = False,
        include_islands: bool = False,
        max_uv_items: int = 250000,
        limit: int = 200,
        cursor: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name,
            "include_loops": include_loops,
            "include_islands": include_islands,
            "max_uv_items": max_uv_items,
            "limit": limit,
        }
        for key, value in (("layer_name", layer_name), ("cursor", cursor)):
            if value is not None:
                params[key] = value
        return self._call("uv.inspect", params, instance)

    def uv_layer_create(
        self,
        object_name: str,
        name: str,
        copy_from: str | None = None,
        set_active: bool = True,
        max_uv_items: int = 250000,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name,
            "name": name,
            "set_active": set_active,
            "max_uv_items": max_uv_items,
        }
        for key, value in (("copy_from", copy_from), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("uv.layer_create", params, instance)

    def uv_layer_rename(
        self,
        object_name: str,
        layer_name: str,
        new_name: str,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name,
            "layer_name": layer_name,
            "new_name": new_name,
        }
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("uv.layer_rename", params, instance)

    def uv_layer_set_active(
        self,
        object_name: str,
        layer_name: str,
        set_render: bool = False,
        set_clone: bool = False,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name,
            "layer_name": layer_name,
            "set_render": set_render,
            "set_clone": set_clone,
        }
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("uv.layer_set_active", params, instance)

    def uv_layer_remove(
        self,
        object_name: str,
        layer_name: str,
        max_uv_items: int = 250000,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name,
            "layer_name": layer_name,
            "max_uv_items": max_uv_items,
            "dry_run": dry_run,
        }
        for key, value in (("confirm", confirm), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("uv.layer_remove", params, instance)

    def uv_project(
        self,
        object_name: str,
        layer_name: str,
        mode: str,
        create_layer: bool = False,
        plane: str | None = None,
        face_selection: dict[str, Any] | None = None,
        normalize: bool = False,
        scale: tuple[float, float] | None = None,
        offset: tuple[float, float] | None = None,
        max_uv_items: int = 250000,
        dry_run: bool = True,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name,
            "layer_name": layer_name,
            "mode": mode,
            "create_layer": create_layer,
            "normalize": normalize,
            "max_uv_items": max_uv_items,
            "dry_run": dry_run,
        }
        for key, value in (
            ("plane", plane), ("face_selection", face_selection),
            ("scale", scale), ("offset", offset), ("expected_state", expected_state),
        ):
            if value is not None:
                params[key] = value
        return self._call("uv.project", params, instance)

    def uv_transform(
        self,
        object_name: str,
        layer_name: str,
        face_selection: dict[str, Any] | None = None,
        translation: tuple[float, float] | None = None,
        rotation: float | None = None,
        scale: tuple[float, float] | None = None,
        pivot: str | None = None,
        max_uv_items: int = 250000,
        dry_run: bool = True,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name,
            "layer_name": layer_name,
            "max_uv_items": max_uv_items,
            "dry_run": dry_run,
        }
        for key, value in (
            ("face_selection", face_selection), ("translation", translation),
            ("rotation", rotation), ("scale", scale), ("pivot", pivot),
            ("expected_state", expected_state),
        ):
            if value is not None:
                params[key] = value
        return self._call("uv.transform", params, instance)

    def uv_unwrap(
        self,
        object_name: str,
        layer_name: str,
        create_layer: bool = False,
        face_selection: dict[str, Any] | None = None,
        method: str = "ANGLE_BASED",
        margin: float = 0.001,
        fill_holes: bool = True,
        correct_aspect: bool = True,
        max_uv_items: int = 250000,
        dry_run: bool = True,
        expected_state: str | None = None,
        instance: str | int | None = None,
        angle_limit: float | None = None,
        island_margin: float | None = None,
        area_weight: float | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name,
            "layer_name": layer_name,
            "create_layer": create_layer,
            "method": method,
            "margin": margin,
            "fill_holes": fill_holes,
            "correct_aspect": correct_aspect,
            "max_uv_items": max_uv_items,
            "dry_run": dry_run,
        }
        for key, value in (("face_selection", face_selection), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        for key, value in (("angle_limit", angle_limit), ("island_margin", island_margin), ("area_weight", area_weight)):
            if value is not None:
                params[key] = value
        return self._call("uv.unwrap", params, instance)

    def uv_pack(
        self,
        object_name: str,
        layer_name: str,
        face_selection: dict[str, Any] | None = None,
        rotate: bool = True,
        scale_to_fit: bool = True,
        margin: float = 0.001,
        max_uv_items: int = 250000,
        dry_run: bool = True,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name,
            "layer_name": layer_name,
            "rotate": rotate,
            "scale_to_fit": scale_to_fit,
            "margin": margin,
            "max_uv_items": max_uv_items,
            "dry_run": dry_run,
        }
        for key, value in (("face_selection", face_selection), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("uv.pack", params, instance)

    def modifier_inspect(
        self,
        object_name: str,
        modifier_name: str | None = None,
        limit: int = 200,
        cursor: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name, "limit": limit}
        if modifier_name is not None:
            params["modifier_name"] = modifier_name
        if cursor is not None:
            params["cursor"] = cursor
        return self._call("modifier.inspect", params, instance)

    def modifier_add(
        self,
        object_name: str,
        type: str,
        name: str | None = None,
        settings: dict[str, Any] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name, "type": type}
        for key, value in (("name", name), ("settings", settings), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("modifier.add", params, instance)

    def modifier_patch(
        self,
        object_name: str,
        modifier_name: str,
        settings: dict[str, Any] | None = None,
        show_viewport: bool | None = None,
        show_render: bool | None = None,
        show_in_editmode: bool | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name, "modifier_name": modifier_name}
        for key, value in (
            ("settings", settings), ("show_viewport", show_viewport),
            ("show_render", show_render), ("show_in_editmode", show_in_editmode),
            ("expected_state", expected_state),
        ):
            if value is not None:
                params[key] = value
        return self._call("modifier.patch", params, instance)

    def modifier_reorder(
        self,
        object_name: str,
        modifier_name: str,
        index: int,
        dry_run: bool = True,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name,
            "modifier_name": modifier_name,
            "index": index,
            "dry_run": dry_run,
        }
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("modifier.reorder", params, instance)

    def modifier_remove(
        self,
        object_name: str,
        modifier_name: str,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name,
            "modifier_name": modifier_name,
            "dry_run": dry_run,
        }
        if confirm is not None:
            params["confirm"] = confirm
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("modifier.remove", params, instance)

    def modifier_apply(
        self,
        object_name: str,
        modifier_name: str,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
        fragment_small_ratio: float | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name,
            "modifier_name": modifier_name,
            "dry_run": dry_run,
        }
        if confirm is not None:
            params["confirm"] = confirm
        if expected_state is not None:
            params["expected_state"] = expected_state
        if fragment_small_ratio is not None:
            params["fragment_small_ratio"] = fragment_small_ratio
        return self._call("modifier.apply", params, instance)

    def object_create(
        self,
        type: str,
        name: str | None = None,
        location: tuple[float, float, float] | None = None,
        rotation: tuple[float, float, float] | None = None,
        scale: tuple[float, float, float] | None = None,
        collection: str | None = None,
        light_type: str | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"type": type}
        for key, value in (
            ("name", name),
            ("location", location),
            ("rotation", rotation),
            ("scale", scale),
            ("collection", collection),
            ("light_type", light_type),
            ("expected_state", expected_state),
        ):
            if value is not None:
                params[key] = list(value) if key in {"location", "rotation", "scale"} else value
        return self._call("object.create", params, instance)

    def architecture_wall_build(
        self,
        wall_lines: list[dict[str, Any]],
        height: float,
        thickness: float,
        name: str = "Walls",
        base_z: float = 0,
        collection: str | None = None,
        material: str | None = None,
        max_elements: int = 250_000,
        dry_run: bool = True,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "wall_lines": wall_lines,
            "height": height,
            "thickness": thickness,
            "name": name,
            "base_z": base_z,
            "max_elements": max_elements,
            "dry_run": dry_run,
        }
        for key, value in (
            ("collection", collection),
            ("material", material),
            ("expected_state", expected_state),
        ):
            if value is not None:
                params[key] = value
        return self._call("architecture.wall_build", params, instance)

    def object_duplicate(
        self,
        object_name: str,
        name: str | None = None,
        linked: bool = False,
        collection: str | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name, "linked": linked}
        for key, value in (("name", name), ("collection", collection), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("object.duplicate", params, instance)

    def object_rename(
        self,
        object_name: str,
        name: str,
        allow_suffix: bool = False,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name, "name": name, "allow_suffix": allow_suffix}
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("object.rename", params, instance)

    def object_set_visibility(
        self,
        objects: str | list[str],
        hide_viewport: bool | None = None,
        hide_render: bool | None = None,
        hide_select: bool | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"objects": objects}
        for key, value in (
            ("hide_viewport", hide_viewport),
            ("hide_render", hide_render),
            ("hide_select", hide_select),
            ("expected_state", expected_state),
        ):
            if value is not None:
                params[key] = value
        return self._call("object.set_visibility", params, instance)

    def object_set_selection(
        self,
        objects: str | list[str],
        selected: bool,
        exclusive: bool = False,
        active: str | None = None,
        set_active: bool = False,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "objects": objects,
            "selected": selected,
            "exclusive": exclusive,
            "set_active": set_active,
        }
        if active is not None:
            params["active"] = active
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("object.set_selection", params, instance)

    def object_parent(
        self,
        object_name: str,
        parent: str | None,
        keep_transform: bool = True,
        dry_run: bool = True,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name,
            "parent": parent,
            "keep_transform": keep_transform,
            "dry_run": dry_run,
        }
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("object.parent", params, instance)

    def object_transform(
        self,
        object_name: str,
        location: tuple[float, float, float] | None = None,
        rotation_euler: tuple[float, float, float] | None = None,
        scale: tuple[float, float, float] | None = None,
        space: Literal["local", "world"] = "local",
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name, "space": space}
        for key, value in (("location", location), ("rotation_euler", rotation_euler), ("scale", scale)):
            if value is not None:
                params[key] = list(value)
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("object.transform", params, instance)

    def object_mode_set(self, mode: str, object_name: str | None = None, dry_run: bool = True, expected_state: str | None = None, instance: str | int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"mode": mode, "dry_run": dry_run}
        if object_name is not None:
            params["object_name"] = object_name
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("object.mode_set", params, instance)

    def object_align_to_workplane(
        self,
        object_name: str,
        reference_object: str,
        plane: dict[str, Any],
        offset: tuple[float, float, float] = (0.0, 0.0, 0.0),
        axis_map: list[str] | None = None,
        align_location: bool = True,
        align_rotation: bool = True,
        dry_run: bool = True,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name,
            "reference_object": reference_object,
            "plane": plane,
            "offset": list(offset),
            "axis_map": list(axis_map or ["U", "V", "N"]),
            "align_location": align_location,
            "align_rotation": align_rotation,
            "dry_run": dry_run,
        }
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("object.align_to_workplane", params, instance)

    def object_delete(
        self,
        objects: str | list[str],
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"objects": objects, "dry_run": dry_run}
        if confirm is not None:
            params["confirm"] = confirm
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("object.delete", params, instance)

    def checkpoint_save(
        self, tag: str | None = None, instance: str | int | None = None,
        directory: str | None = None, name_template: str | None = None,
        keep_latest: int | None = None, preview_only: bool = False,
        prune: bool = False, confirm: bool | None = None,
        expected_state: str | None = None,
        asset: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"tag": tag}
        for key, value in (("directory", directory), ("name_template", name_template), ("asset", asset), ("keep_latest", keep_latest), ("confirm", confirm), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        if preview_only:
            params["preview_only"] = True
        if prune:
            params["prune"] = True
        return self._call("checkpoint.save", params, instance)

    def checkpoint_list(self, instance: str | int | None = None, directory: str | None = None, name_template: str | None = None, asset: str | None = None) -> dict[str, Any]:
        params = {key: value for key, value in (("directory", directory), ("name_template", name_template), ("asset", asset)) if value is not None}
        return self._call("checkpoint.list", params, instance)

    def checkpoint_restore(
        self,
        name: str,
        confirm: Literal[True],
        expected_state: str | None = None,
        instance: str | int | None = None,
        directory: str | None = None,
        name_template: str | None = None,
        asset: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name, "confirm": confirm}
        if expected_state is not None:
            params["expected_state"] = expected_state
        if directory is not None:
            params["directory"] = directory
        if name_template is not None:
            params["name_template"] = name_template
        if asset is not None:
            params["asset"] = asset
        return self._call("checkpoint.restore", params, instance)

    def session_whoami(self, instance: str | int | None = None) -> dict[str, Any]:
        return self._call("session.whoami", {}, instance)

    def session_acquire_lease(
        self,
        scope: Literal["document"] = "document",
        ttl: int = 300,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        return self._call("session.acquire_lease", {"scope": scope, "ttl": ttl}, instance)

    def session_renew_lease(
        self,
        lease_id: str,
        ttl: int | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"lease_id": lease_id}
        if ttl is not None:
            params["ttl"] = ttl
        return self._call("session.renew_lease", params, instance)

    def session_release_lease(
        self, lease_id: str, instance: str | int | None = None
    ) -> dict[str, Any]:
        return self._call("session.release_lease", {"lease_id": lease_id}, instance)

    def render_preview(
        self,
        width: int = 960,
        height: int = 540,
        output_path: str | None = None,
        overwrite: bool = False,
        allow_large: bool = False,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "width": width,
            "height": height,
            "overwrite": overwrite,
            "allow_large": allow_large,
        }
        if output_path is not None:
            params["output_path"] = output_path
        return self._call("render.preview", params, instance)

    def compositor_inspect(
        self,
        scene: str | None = None,
        limit: int = 200,
        cursor: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if scene is not None:
            params["scene"] = scene
        if cursor is not None:
            params["cursor"] = cursor
        return self._call("compositor.inspect", params, instance)

    def compositor_create(
        self,
        scene: str | None = None,
        include_viewer: bool = False,
        dry_run: bool = True,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"include_viewer": include_viewer, "dry_run": dry_run}
        for key, value in (("scene", scene), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("compositor.create", params, instance)

    def compositor_patch(
        self,
        operations: list[dict[str, Any]],
        scene: str | None = None,
        expected_node_count: int | None = None,
        expected_link_count: int | None = None,
        dry_run: bool = True,
        confirm: bool | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"operations": operations, "dry_run": dry_run}
        for key, value in (
            ("scene", scene), ("expected_node_count", expected_node_count),
            ("expected_link_count", expected_link_count), ("confirm", confirm),
            ("expected_state", expected_state),
        ):
            if value is not None:
                params[key] = value
        return self._call("compositor.patch", params, instance)

    def render_manifest(
        self,
        scene: str | None = None,
        include_paths: bool = False,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"include_paths": include_paths}
        if scene is not None:
            params["scene"] = scene
        return self._call("render.manifest", params, instance)

    def render_manifest_validate(
        self,
        scene: str | None = None,
        include_paths: bool = False,
        require_composite: bool = True,
        require_file_output: bool = False,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "include_paths": include_paths,
            "require_composite": require_composite,
            "require_file_output": require_file_output,
        }
        if scene is not None:
            params["scene"] = scene
        return self._call("render.manifest_validate", params, instance)

    def interchange_inspect(
        self,
        format: str | None = None,
        direction: Literal["import", "export"] | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if format is not None:
            params["format"] = format
        if direction is not None:
            params["direction"] = direction
        return self._call("interchange.inspect", params, instance)

    def interchange_import(
        self,
        filepath: str,
        format: str | None = None,
        options: dict[str, Any] | None = None,
        max_bytes: int = 2_147_483_648,
        dry_run: bool = True,
        confirm: bool | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"filepath": filepath, "max_bytes": max_bytes, "dry_run": dry_run}
        for key, value in (
            ("format", format), ("options", options), ("confirm", confirm),
            ("expected_state", expected_state),
        ):
            if value is not None:
                params[key] = value
        return self._call("interchange.import", params, instance)

    def interchange_export(
        self,
        filepath: str,
        format: str | None = None,
        scope: Literal["selected", "scene", "collection"] = "selected",
        collection: str | None = None,
        options: dict[str, Any] | None = None,
        overwrite: bool = False,
        dry_run: bool = True,
        confirm: bool | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "filepath": filepath, "scope": scope, "overwrite": overwrite, "dry_run": dry_run,
        }
        for key, value in (
            ("format", format), ("collection", collection), ("options", options),
            ("confirm", confirm), ("expected_state", expected_state),
        ):
            if value is not None:
                params[key] = value
        return self._call("interchange.export", params, instance)

    def interchange_blend_import(self, filepath: str, names: str | list[str], data_type: Literal["COLLECTION", "OBJECT"] = "COLLECTION", link: bool = False, target_collection: str | None = None, max_bytes: int = 2_147_483_648, dry_run: bool = True, confirm: bool | None = None, expected_state: str | None = None, instance: str | int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"filepath": filepath, "names": names, "data_type": data_type, "link": link, "max_bytes": max_bytes, "dry_run": dry_run}
        for key, value in (("target_collection", target_collection), ("confirm", confirm), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("interchange.blend_import", params, instance)

    def armature_inspect(
        self,
        object_name: str,
        include_pose: bool = True,
        include_constraints: bool = False,
        limit: int = 200,
        cursor: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name,
            "include_pose": include_pose,
            "include_constraints": include_constraints,
            "limit": limit,
        }
        if cursor is not None:
            params["cursor"] = cursor
        return self._call("armature.inspect", params, instance)

    def armature_create(
        self,
        name: str,
        data_name: str | None = None,
        collection: str | None = None,
        display_type: Literal["OCTAHEDRAL", "STICK", "BBONE", "ENVELOPE", "WIRE"] = "OCTAHEDRAL",
        show_in_front: bool = True,
        bones: list[dict[str, Any]] | None = None,
        dry_run: bool = True,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "name": name,
            "display_type": display_type,
            "show_in_front": show_in_front,
            "dry_run": dry_run,
        }
        for key, value in (("data_name", data_name), ("collection", collection), ("bones", bones), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("armature.create", params, instance)

    def armature_edit(
        self,
        object_name: str,
        operations: list[dict[str, Any]],
        expected_bone_count: int | None = None,
        dry_run: bool = True,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name, "operations": operations, "dry_run": dry_run}
        for key, value in (("expected_bone_count", expected_bone_count), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("armature.edit", params, instance)

    def armature_bone_remove(
        self,
        object_name: str,
        bone: str,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name, "bone": bone, "dry_run": dry_run,
        }
        for key, value in (("confirm", confirm), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("armature.bone_remove", params, instance)

    def armature_validate(
        self,
        object_name: str,
        include_animation: bool = True,
        max_bound_meshes: int = 2000,
        sample_limit: int = 50,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        return self._call(
            "armature.validate",
            {
                "object_name": object_name,
                "include_animation": include_animation,
                "max_bound_meshes": max_bound_meshes,
                "sample_limit": sample_limit,
            },
            instance,
        )

    def pose_inspect(
        self,
        object_name: str,
        bone: str | None = None,
        include_constraints: bool = True,
        limit: int = 200,
        cursor: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name, "include_constraints": include_constraints, "limit": limit}
        for key, value in (("bone", bone), ("cursor", cursor)):
            if value is not None:
                params[key] = value
        return self._call("pose.inspect", params, instance)

    def pose_patch(
        self,
        object_name: str,
        bone: str,
        location: tuple[float, float, float] | None = None,
        rotation_mode: str | None = None,
        rotation_euler: tuple[float, float, float] | None = None,
        rotation_quaternion: tuple[float, float, float, float] | None = None,
        rotation_axis_angle: tuple[float, float, float, float] | None = None,
        scale: tuple[float, float, float] | None = None,
        dry_run: bool = True,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name, "bone": bone, "dry_run": dry_run}
        for key, value in (
            ("location", location), ("rotation_mode", rotation_mode),
            ("rotation_euler", rotation_euler), ("rotation_quaternion", rotation_quaternion),
            ("rotation_axis_angle", rotation_axis_angle), ("scale", scale),
            ("expected_state", expected_state),
        ):
            if value is not None:
                params[key] = value
        return self._call("pose.patch", params, instance)

    def pose_constraint_patch(
        self,
        object_name: str,
        operations: list[dict[str, Any]],
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name, "operations": operations, "dry_run": dry_run}
        for key, value in (("confirm", confirm), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("pose.constraint_patch", params, instance)

    def animation_inspect(
        self,
        object_name: str,
        action_name: str | None = None,
        include_keyframes: bool = False,
        max_keys_per_curve: int = 200,
        limit: int = 200,
        cursor: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name,
            "include_keyframes": include_keyframes,
            "max_keys_per_curve": max_keys_per_curve,
            "limit": limit,
        }
        for key, value in (("action_name", action_name), ("cursor", cursor)):
            if value is not None:
                params[key] = value
        return self._call("animation.inspect", params, instance)

    def animation_action_create(
        self,
        object_name: str,
        name: str,
        assign: bool = True,
        replace: bool = False,
        fake_user: bool = False,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name, "name": name, "assign": assign,
            "replace": replace, "fake_user": fake_user, "dry_run": dry_run,
        }
        for key, value in (("confirm", confirm), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("animation.action_create", params, instance)

    def animation_action_assign(
        self,
        object_name: str,
        action_name: str | None = None,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name, "dry_run": dry_run}
        for key, value in (("action_name", action_name), ("confirm", confirm), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("animation.action_assign", params, instance)

    def animation_action_retarget(
        self,
        source_object: str,
        source_action: str,
        target_object: str,
        name: str,
        bone_map: dict[str, str] | None = None,
        assign: bool = True,
        replace: bool = False,
        require_all_bones: bool = True,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "source_object": source_object, "source_action": source_action,
            "target_object": target_object, "name": name,
            "bone_map": {} if bone_map is None else bone_map,
            "assign": assign, "replace": replace,
            "require_all_bones": require_all_bones, "dry_run": dry_run,
        }
        for key, value in (("confirm", confirm), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("animation.action_retarget", params, instance)

    def animation_action_remove(
        self,
        name: str,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name, "dry_run": dry_run}
        for key, value in (("confirm", confirm), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("animation.action_remove", params, instance)

    def animation_keyframe_edit(
        self,
        object_name: str,
        operations: list[dict[str, Any]],
        expected_action: str | None = None,
        make_single_user: bool = False,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name, "operations": operations,
            "make_single_user": make_single_user, "dry_run": dry_run,
        }
        for key, value in (("expected_action", expected_action), ("confirm", confirm), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("animation.keyframe_edit", params, instance)

    def animation_pose_bake(
        self,
        object_name: str,
        name: str,
        frame_start: int,
        frame_end: int,
        step: int = 1,
        bones: list[str] | None = None,
        visual_keying: bool = True,
        max_bones: int = 512,
        max_samples: int = 100_000,
        dry_run: bool = True,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name, "name": name,
            "frame_start": frame_start, "frame_end": frame_end, "step": step,
            "visual_keying": visual_keying, "max_bones": max_bones,
            "max_samples": max_samples, "dry_run": dry_run,
        }
        for key, value in (("bones", bones), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("animation.pose_bake", params, instance)

    def animation_validate(
        self,
        object_name: str,
        action_name: str | None = None,
        frame_start: float | None = None,
        frame_end: float | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name}
        for key, value in (("action_name", action_name), ("frame_start", frame_start), ("frame_end", frame_end)):
            if value is not None:
                params[key] = value
        return self._call("animation.validate", params, instance)

    def animation_nla_inspect(
        self,
        object_name: str,
        include_strips: bool = True,
        limit: int = 200,
        cursor: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name, "include_strips": include_strips, "limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return self._call("animation.nla_inspect", params, instance)

    def animation_nla_patch(
        self,
        object_name: str,
        operations: list[dict[str, Any]],
        dry_run: bool = True,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name, "operations": operations, "dry_run": dry_run}
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("animation.nla_patch", params, instance)

    def animation_nla_remove(
        self,
        object_name: str,
        track: str,
        strip: str | None = None,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name, "track": track, "dry_run": dry_run}
        for key, value in (("strip", strip), ("confirm", confirm), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("animation.nla_remove", params, instance)

    def animation_driver_inspect(
        self,
        object_name: str,
        limit: int = 200,
        cursor: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name, "limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return self._call("animation.driver_inspect", params, instance)

    def animation_driver_create(
        self,
        object_name: str,
        property: str,
        index: int,
        source_object: str,
        source_transform: str,
        target: Literal["object", "pose_bone"] = "object",
        bone: str | None = None,
        source_bone: str | None = None,
        source_space: Literal["WORLD_SPACE", "TRANSFORM_SPACE", "LOCAL_SPACE"] = "WORLD_SPACE",
        multiplier: float = 1.0,
        offset: float = 0.0,
        dry_run: bool = True,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name, "property": property, "index": index,
            "source_object": source_object, "source_transform": source_transform,
            "target": target, "source_space": source_space,
            "multiplier": multiplier, "offset": offset, "dry_run": dry_run,
        }
        for key, value in (("bone", bone), ("source_bone", source_bone), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("animation.driver_create", params, instance)

    def animation_driver_remove(
        self,
        object_name: str,
        property: str,
        index: int,
        target: Literal["object", "pose_bone"] = "object",
        bone: str | None = None,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name, "property": property, "index": index,
            "target": target, "dry_run": dry_run,
        }
        for key, value in (("bone", bone), ("confirm", confirm), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("animation.driver_remove", params, instance)

    def animation_shape_key_inspect(
        self,
        object_name: str,
        limit: int = 200,
        cursor: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name, "limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return self._call("animation.shape_key_inspect", params, instance)

    def animation_shape_key_create(
        self,
        object_name: str,
        name: str,
        from_mix: bool = False,
        relative_key: str | None = None,
        vertex_group: str | None = None,
        dry_run: bool = True,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name, "name": name, "from_mix": from_mix, "dry_run": dry_run}
        for key, value in (("relative_key", relative_key), ("vertex_group", vertex_group), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("animation.shape_key_create", params, instance)

    def animation_shape_key_patch(
        self,
        object_name: str,
        name: str,
        value: float | None = None,
        slider_min: float | None = None,
        slider_max: float | None = None,
        mute: bool | None = None,
        interpolation: str | None = None,
        relative_key: str | None = None,
        vertex_group: str | None = None,
        clear_vertex_group: bool = False,
        positions: list[dict[str, Any]] | None = None,
        max_points: int = 250_000,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name, "name": name,
            "clear_vertex_group": clear_vertex_group,
            "max_points": max_points, "dry_run": dry_run,
        }
        for key, item in (
            ("value", value), ("slider_min", slider_min), ("slider_max", slider_max),
            ("mute", mute), ("interpolation", interpolation), ("relative_key", relative_key),
            ("vertex_group", vertex_group), ("positions", positions),
            ("confirm", confirm), ("expected_state", expected_state),
        ):
            if item is not None:
                params[key] = item
        return self._call("animation.shape_key_patch", params, instance)

    def animation_shape_key_remove(
        self,
        object_name: str,
        name: str,
        max_points: int = 250_000,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name, "name": name,
            "max_points": max_points, "dry_run": dry_run,
        }
        for key, value in (("confirm", confirm), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("animation.shape_key_remove", params, instance)

    def skin_inspect(
        self,
        object_name: str,
        armature_name: str | None = None,
        max_vertices: int = 250_000,
        limit: int = 200,
        cursor: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name, "max_vertices": max_vertices, "limit": limit}
        for key, value in (("armature_name", armature_name), ("cursor", cursor)):
            if value is not None:
                params[key] = value
        return self._call("skin.inspect", params, instance)

    def skin_bind(
        self,
        object_name: str,
        armature_name: str,
        modifier_name: str = "Armature",
        preserve_volume: bool = False,
        dry_run: bool = True,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name, "armature_name": armature_name,
            "modifier_name": modifier_name, "preserve_volume": preserve_volume,
            "dry_run": dry_run,
        }
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("skin.bind", params, instance)

    def skin_unbind(
        self,
        object_name: str,
        armature_name: str | None = None,
        modifier_name: str | None = None,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name, "dry_run": dry_run}
        for key, value in (
            ("armature_name", armature_name), ("modifier_name", modifier_name),
            ("confirm", confirm), ("expected_state", expected_state),
        ):
            if value is not None:
                params[key] = value
        return self._call("skin.unbind", params, instance)

    def skin_auto_weights(
        self,
        object_name: str,
        armature_name: str,
        max_vertices: int = 250_000,
        max_weight_entries: int = 250_000,
        max_candidate_pairs: int = 5_000_000,
        require_all_weighted: bool = True,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name,
            "armature_name": armature_name,
            "max_vertices": max_vertices,
            "max_weight_entries": max_weight_entries,
            "max_candidate_pairs": max_candidate_pairs,
            "require_all_weighted": require_all_weighted,
            "dry_run": dry_run,
        }
        for key, value in (("confirm", confirm), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("skin.auto_weights", params, instance)

    def skin_weights_patch(
        self,
        object_name: str,
        group_name: str,
        operations: list[dict[str, Any]],
        create_group: bool = False,
        expected_vertex_count: int | None = None,
        max_vertices: int = 250_000,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name, "group_name": group_name,
            "operations": operations, "create_group": create_group,
            "max_vertices": max_vertices, "dry_run": dry_run,
        }
        for key, value in (("expected_vertex_count", expected_vertex_count), ("confirm", confirm), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("skin.weights_patch", params, instance)

    def skin_group_remove(
        self,
        object_name: str,
        group_name: str,
        max_vertices: int = 250_000,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name, "group_name": group_name,
            "max_vertices": max_vertices, "dry_run": dry_run,
        }
        for key, value in (("confirm", confirm), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("skin.group_remove", params, instance)

    def skin_weights_normalize(
        self,
        object_name: str,
        armature_name: str | None = None,
        max_influences: int = 4,
        min_weight: float = 0.0001,
        require_all_weighted: bool = True,
        max_vertices: int = 250_000,
        max_weight_entries: int = 250_000,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name, "max_influences": max_influences,
            "min_weight": min_weight, "require_all_weighted": require_all_weighted,
            "max_vertices": max_vertices, "max_weight_entries": max_weight_entries,
            "dry_run": dry_run,
        }
        for key, value in (("armature_name", armature_name), ("confirm", confirm), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("skin.weights_normalize", params, instance)

    def skin_validate(
        self,
        object_name: str,
        armature_name: str | None = None,
        max_vertices: int = 250_000,
        normalization_tolerance: float = 0.0001,
        sample_limit: int = 50,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "object_name": object_name,
            "max_vertices": max_vertices,
            "normalization_tolerance": normalization_tolerance,
            "sample_limit": sample_limit,
        }
        if armature_name is not None:
            params["armature_name"] = armature_name
        return self._call("skin.validate", params, instance)

    def render_review_set(
        self,
        output_dir: str | None = None,
        output_prefix: str | None = None,
        views: list[Literal["camera", "top", "side", "three_quarter"]] | None = None,
        width: int = 960,
        height: int = 540,
        overwrite: bool = False,
        allow_large: bool = False,
        hide: list[str] | None = None,
        show_only: list[str] | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "width": width,
            "height": height,
            "overwrite": overwrite,
            "allow_large": allow_large,
        }
        for key, value in (
            ("output_dir", output_dir),
            ("output_prefix", output_prefix),
            ("views", views),
            ("hide", hide),
            ("show_only", show_only),
        ):
            if value is not None:
                params[key] = value
        return self._call("render.review_set", params, instance)

    def render_compare(
        self,
        image_a: str,
        image_b: str,
        threshold: int = 8,
        write_diff: bool = False,
        diff_output_path: str | None = None,
        overwrite: bool = False,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "image_a": image_a,
            "image_b": image_b,
            "threshold": threshold,
            "write_diff": write_diff,
            "overwrite": overwrite,
        }
        if diff_output_path is not None:
            params["diff_output_path"] = diff_output_path
        return self._call("render.compare", params, instance)

    def render_detail(self, params: dict[str, Any], instance: str | int | None = None) -> dict[str, Any]:
        return self._call("render.detail", params, instance)

    def render_ortho_set(self, params: dict[str, Any], instance: str | int | None = None) -> dict[str, Any]:
        return self._call("render.ortho_set", params, instance)

    def render_reference_match(self, params: dict[str, Any], instance: str | int | None = None) -> dict[str, Any]:
        return self._call("render.reference_match", params, instance)

    def render_contact_sheet(self, params: dict[str, Any], instance: str | int | None = None) -> dict[str, Any]:
        return self._call("render.contact_sheet", params, instance)

    def render_background(self, params: dict[str, Any], instance: str | int | None = None) -> dict[str, Any]:
        return self._call("render.background", params, instance)

    def render_background_status(self, operation_id: str, instance: str | int | None = None) -> dict[str, Any]:
        return self._call("render.background_status", {"operation_id": operation_id}, instance)

    def render_background_cancel(self, operation_id: str, instance: str | int | None = None) -> dict[str, Any]:
        return self._call("render.background_cancel", {"operation_id": operation_id}, instance)

    def material_inspect(
        self,
        material: str | None = None,
        object_name: str | None = None,
        collection: str | None = None,
        include_nodes: bool = False,
        limit: int = 200,
        cursor: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"include_nodes": include_nodes, "limit": limit}
        for key, value in (("material", material), ("object_name", object_name), ("collection", collection), ("cursor", cursor)):
            if value is not None:
                params[key] = value
        return self._call("material.inspect", params, instance)

    def material_create(
        self,
        name: str,
        use_nodes: bool = True,
        use_fake_user: bool = False,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "name": name,
            "use_nodes": use_nodes,
            "use_fake_user": use_fake_user,
        }
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("material.create", params, instance)

    def material_patch(
        self,
        material: str,
        operations: list[dict[str, Any]],
        preconditions: list[dict[str, Any]] | None = None,
        dry_run: bool = True,
        expected_user_count: int | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"material": material, "operations": operations, "dry_run": dry_run}
        if preconditions is not None:
            params["preconditions"] = preconditions
        if expected_user_count is not None:
            params["expected_user_count"] = expected_user_count
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("material.patch", params, instance)

    def image_inspect(
        self,
        image: str | None = None,
        include_filepath: bool = False,
        limit: int = 200,
        cursor: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"include_filepath": include_filepath, "limit": limit}
        if image is not None:
            params["image"] = image
        if cursor is not None:
            params["cursor"] = cursor
        return self._call("image.inspect", params, instance)

    def image_load(
        self,
        filepath: str,
        name: str | None = None,
        check_existing: bool = True,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"filepath": filepath, "check_existing": check_existing}
        if name is not None:
            params["name"] = name
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("image.load", params, instance)

    def image_create(
        self,
        name: str,
        width: int,
        height: int,
        color: list[float] | None = None,
        alpha: bool = True,
        float_buffer: bool = False,
        is_data: bool = False,
        max_pixels: int = 16_777_216,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "name": name, "width": width, "height": height, "alpha": alpha,
            "float_buffer": float_buffer, "is_data": is_data, "max_pixels": max_pixels,
        }
        if color is not None:
            params["color"] = color
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("image.create", params, instance)

    def image_patch(
        self,
        image: str,
        colorspace: str | None = None,
        alpha_mode: str | None = None,
        use_fake_user: bool | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"image": image}
        for key, value in (("colorspace", colorspace), ("alpha_mode", alpha_mode), ("use_fake_user", use_fake_user), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return self._call("image.patch", params, instance)

    def material_slot_inspect(
        self,
        object_name: str,
        limit: int = 200,
        cursor: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name, "limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return self._call("material.slot_inspect", params, instance)

    def material_slot_assign(
        self,
        object_name: str,
        material: str,
        slot_index: int | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name, "material": material}
        if slot_index is not None:
            params["slot_index"] = slot_index
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("material.slot_assign", params, instance)

    def material_slot_remove(
        self,
        object_name: str,
        slot_index: int,
        max_polygons: int = 250_000,
        dry_run: bool = True,
        confirm: bool | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"object_name": object_name, "slot_index": slot_index, "max_polygons": max_polygons, "dry_run": dry_run}
        if confirm is not None:
            params["confirm"] = confirm
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("material.slot_remove", params, instance)

    def material_texture_bind(
        self,
        material: str,
        image: str,
        image_node: str = "CBT Image Texture",
        uv_map: str | None = None,
        mapping: dict[str, Any] | None = None,
        color_target: dict[str, Any] | None = None,
        alpha_target: dict[str, Any] | None = None,
        interpolation: str = "Linear",
        extension: str = "REPEAT",
        projection: str = "FLAT",
        replace_existing: bool = False,
        allow_shared: bool = False,
        expected_user_count: int | None = None,
        dry_run: bool = True,
        confirm: bool | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "material": material, "image": image, "image_node": image_node,
            "interpolation": interpolation, "extension": extension, "projection": projection,
            "replace_existing": replace_existing, "allow_shared": allow_shared, "dry_run": dry_run,
        }
        for key, value in (
            ("uv_map", uv_map), ("mapping", mapping), ("color_target", color_target),
            ("alpha_target", alpha_target), ("expected_user_count", expected_user_count),
            ("confirm", confirm), ("expected_state", expected_state),
        ):
            if value is not None:
                params[key] = value
        return self._call("material.texture_bind", params, instance)

    def scene_save_version(
        self,
        base: str | None = None,
        tag: str | None = None,
        expected_state: str | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if base is not None:
            params["base"] = base
        if tag is not None:
            params["tag"] = tag
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("scene.save_version", params, instance)

    def scene_save_current(self, dry_run: bool | None = None, confirm: bool | None = None, expected_state: str | None = None, instance: str | int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if dry_run is not None:
            params["dry_run"] = dry_run
        if confirm is not None:
            params["confirm"] = confirm
        if expected_state is not None:
            params["expected_state"] = expected_state
        return self._call("scene.save_current", params, instance)
