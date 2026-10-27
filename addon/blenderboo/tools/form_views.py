"""Temporary, deterministic form shaders for detail rendering.

All materials are generated from fixed numeric ramps.  They never enter the
saved document: callers restore the view-layer override and remove them.
"""

from __future__ import annotations

from math import isfinite

from ..errors import InvalidParamsError


VIEW_MODES = ("lit", "solid", "normal", "matcap", "curvature", "depth", "wireframe_on_shaded")


def view_mode(params):
    mode = params.get("view_mode", "lit")
    if not isinstance(mode, str) or mode not in VIEW_MODES:
        raise InvalidParamsError("view_mode must be lit, solid, normal, matcap, curvature, depth, or wireframe_on_shaded")
    return mode


def depth_range(bounds, camera, scene):
    """Return camera-space distances of the eight world-space target corners."""
    from mathutils import Vector

    minimum, maximum = bounds
    inverse = camera.matrix_world.inverted()
    distances = [-float((inverse @ Vector((x, y, z))).z)
                 for x in (minimum.x, maximum.x)
                 for y in (minimum.y, maximum.y)
                 for z in (minimum.z, maximum.z)]
    near = max(0.0, min(distances))
    far = max(distances)
    if not all(isfinite(v) for v in (near, far)) or far <= near:
        raise InvalidParamsError("Target has no finite camera-space depth range")
    unit_settings = scene.unit_settings
    return {"near": near, "far": far, "units": "Blender units",
            "scene_length_unit": unit_settings.length_unit}


def _link(nodes, links, source, output, target, input_name):
    links.new(source.outputs[output], target.inputs[input_name])


def create_form_material(bpy, mode, depth=None):
    """Create one emission material, with no scene object/material edits."""
    material = bpy.data.materials.new(f"CodexFormView_{mode}")
    try:
        material.use_nodes = True
        nodes = material.node_tree.nodes
        nodes.clear()
        links = material.node_tree.links
        output = nodes.new("ShaderNodeOutputMaterial")
        emission = nodes.new("ShaderNodeEmission")
        emission.inputs["Strength"].default_value = 1.0
        _link(nodes, links, emission, "Emission", output, "Surface")
        geometry = nodes.new("ShaderNodeNewGeometry")

        if mode == "normal":
            scale = nodes.new("ShaderNodeVectorMath")
            scale.operation = "MULTIPLY"
            scale.inputs[1].default_value = (0.5, 0.5, 0.5)
            bias = nodes.new("ShaderNodeVectorMath")
            bias.operation = "ADD"
            bias.inputs[1].default_value = (0.5, 0.5, 0.5)
            _link(nodes, links, geometry, "Normal", scale, 0)
            _link(nodes, links, scale, "Vector", bias, 0)
            _link(nodes, links, bias, "Vector", emission, "Color")
        elif mode == "depth":
            if depth is None:
                raise InvalidParamsError("depth range is required for depth view")
            transform = nodes.new("ShaderNodeVectorTransform")
            transform.vector_type = "POINT"
            transform.convert_from = "WORLD"
            transform.convert_to = "CAMERA"
            split = nodes.new("ShaderNodeSeparateXYZ")
            negate = nodes.new("ShaderNodeMath")
            negate.operation = "MULTIPLY"
            negate.inputs[1].default_value = -1.0
            map_range = nodes.new("ShaderNodeMapRange")
            map_range.clamp = True
            map_range.inputs["From Min"].default_value = depth["near"]
            map_range.inputs["From Max"].default_value = depth["far"]
            ramp = nodes.new("ShaderNodeValToRGB")
            ramp.color_ramp.interpolation = "LINEAR"
            ramp.color_ramp.elements.remove(ramp.color_ramp.elements[1])
            for index, (position, color) in enumerate((
                (0.0, (0.11, 0.20, 0.76, 1.0)),
                (0.33, (0.03, 0.77, 0.88, 1.0)),
                (0.67, (0.96, 0.83, 0.16, 1.0)),
                (1.0, (0.84, 0.11, 0.10, 1.0)),
            )):
                element = ramp.color_ramp.elements[0] if index == 0 else ramp.color_ramp.elements.new(position)
                element.position, element.color = position, color
            _link(nodes, links, geometry, "Position", transform, "Vector")
            _link(nodes, links, transform, "Vector", split, "Vector")
            _link(nodes, links, split, "Z", negate, 0)
            _link(nodes, links, negate, "Value", map_range, "Value")
            _link(nodes, links, map_range, "Result", ramp, "Fac")
            _link(nodes, links, ramp, "Color", emission, "Color")
        elif mode == "curvature":
            # Geometry.Pointiness is supported by Cycles.  The caller switches
            # only this temporary render to Cycles and restores the engine.
            ramp = nodes.new("ShaderNodeValToRGB")
            ramp.color_ramp.elements[0].position = 0.25
            ramp.color_ramp.elements[0].color = (0.08, 0.16, 0.55, 1.0)
            ramp.color_ramp.elements[1].position = 0.75
            ramp.color_ramp.elements[1].color = (0.92, 0.67, 0.21, 1.0)
            neutral = ramp.color_ramp.elements.new(0.5)
            neutral.color = (0.72, 0.72, 0.72, 1.0)
            _link(nodes, links, geometry, "Pointiness", ramp, "Fac")
            _link(nodes, links, ramp, "Color", emission, "Color")
        else:
            # Fixed, orientation-based clay ramp; stable across lighting setups.
            normal = nodes.new("ShaderNodeVectorMath")
            normal.operation = "DOT_PRODUCT"
            normal.inputs[1].default_value = (0.24, -0.55, 0.80)
            map_range = nodes.new("ShaderNodeMapRange")
            map_range.inputs["From Min"].default_value = -1.0
            map_range.inputs["From Max"].default_value = 1.0
            map_range.inputs["To Min"].default_value = 0.18
            map_range.inputs["To Max"].default_value = 0.90
            _link(nodes, links, geometry, "Normal", normal, 0)
            _link(nodes, links, normal, "Value", map_range, "Value")
            if mode == "wireframe_on_shaded":
                wire = nodes.new("ShaderNodeWireframe")
                wire.use_pixel_size = True
                wire.inputs["Size"].default_value = 1.0
                mix = nodes.new("ShaderNodeMixRGB")
                mix.blend_type = "MIX"
                mix.inputs[2].default_value = (0.015, 0.020, 0.030, 1.0)
                _link(nodes, links, wire, "Fac", mix, 0)
                _link(nodes, links, map_range, "Result", mix, 1)
                _link(nodes, links, mix, "Color", emission, "Color")
            else:
                _link(nodes, links, map_range, "Result", emission, "Color")
        return material
    except Exception:
        bpy.data.materials.remove(material)
        raise


def install_form_override(bpy, scene, mode, depth=None):
    """Install a temporary override, returning a cleanup callback."""
    if mode == "lit":
        return lambda: []
    if mode == "solid":
        previous_engine = scene.render.engine
        scene.render.engine = "BLENDER_WORKBENCH"

        def restore_solid():
            try:
                scene.render.engine = previous_engine
                return []
            except Exception as exc:
                return [f"restore render engine: {exc}"]

        return restore_solid
    view_layer = bpy.context.view_layer
    previous_material = view_layer.material_override
    previous_engine = scene.render.engine
    previous_samples = scene.cycles.samples if hasattr(scene, "cycles") else None
    material = create_form_material(bpy, mode, depth)
    try:
        if mode == "curvature":
            scene.render.engine = "CYCLES"
            scene.cycles.samples = 16
        else:
            engine_ids = {item.identifier for item in scene.render.bl_rna.properties["engine"].enum_items}
            scene.render.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in engine_ids else "BLENDER_EEVEE"
        view_layer.material_override = material
    except Exception:
        view_layer.material_override = previous_material
        scene.render.engine = previous_engine
        bpy.data.materials.remove(material)
        raise

    def restore():
        errors = []
        for label, action in (
            ("material override", lambda: setattr(view_layer, "material_override", previous_material)),
            ("render engine", lambda: setattr(scene.render, "engine", previous_engine)),
            ("Cycles samples", lambda: setattr(scene.cycles, "samples", previous_samples) if previous_samples is not None else None),
            ("temporary form material", lambda: bpy.data.materials.remove(material)),
        ):
            try:
                action()
            except Exception as exc:
                errors.append(f"restore {label}: {exc}")
        return errors

    return restore
