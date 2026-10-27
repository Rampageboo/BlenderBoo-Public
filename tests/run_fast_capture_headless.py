"""Blender background assertions for target framing; GPU draw remains GUI-only."""

from pathlib import Path
import sys

import bpy
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo.tools.render import _target_bounds, DETAIL_DIRECTIONS
from blenderboo.tools.scene import _fast_capture_matrix


bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(location=(3, 5, 7))
cube = bpy.context.object
cube.name = "FrameTarget"
scene = bpy.context.scene
minimum, maximum, _ = _target_bounds(bpy, scene, {"object_name": cube.name})
center = (minimum + maximum) * 0.5
assert (center - Vector((3, 5, 7))).length < 1e-6

for projection in ("perspective", "orthographic"):
    for name in ("front", "top", "three_quarter"):
        view, lens, receipt = _fast_capture_matrix(
            (minimum, maximum), DETAIL_DIRECTIONS[name], 512, 384, projection
        )
        assert receipt["projection"] == projection
        assert (Vector(receipt["target_center"]) - center).length < 1e-6
        for corner in cube.bound_box:
            world = cube.matrix_world @ Vector(corner)
            clip = lens @ (view @ Vector((world.x, world.y, world.z, 1.0)))
            x, y, z = (clip[i] / clip.w for i in range(3))
            assert -1 < x < 1 and -1 < y < 1, (projection, name, x, y)
            assert -1 < z < 1, (projection, name, z)

print("PASS fast capture target framing for perspective and orthographic views", flush=True)
