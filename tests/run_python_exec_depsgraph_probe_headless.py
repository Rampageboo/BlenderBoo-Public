"""Exploratory Blender 5.2 dependency graph update coverage for python.exec."""

import bpy

bpy.ops.wm.read_factory_settings(use_empty=True)
mesh = bpy.data.meshes.new("ProbeMesh")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
obj = bpy.data.objects.new("Probe", mesh)
bpy.context.scene.collection.objects.link(obj)
modifier = obj.modifiers.new("Subdivision", "SUBSURF")
material = bpy.data.materials.new("ProbeMaterial")
material.use_nodes = True
mesh.materials.append(material)
uv = mesh.uv_layers.new(name="UVMap")
bpy.context.view_layer.update()

cases = [
    ("position", lambda: setattr(mesh.vertices[0].co, "x", 0.5)),
    ("modifier", lambda: setattr(modifier, "levels", 2)),
    ("node", lambda: setattr(material.node_tree.nodes.get("Principled BSDF").inputs["Base Color"],
                            "default_value", (0.2, 0.3, 0.4, 1.0))),
    ("uv", lambda: setattr(uv.data[0], "uv", (0.1, 0.2))),
    ("custom", lambda: obj.__setitem__("probe", 7)),
    ("transform", lambda: setattr(obj.location, "x", 2.0)),
]

for label, change in cases:
    observed = []

    def on_update(scene, depsgraph):
        observed.extend((update.id.name, update.is_updated_geometry,
                         update.is_updated_transform, update.is_updated_shading)
                        for update in depsgraph.updates)

    bpy.app.handlers.depsgraph_update_post.append(on_update)
    try:
        change()
        bpy.context.view_layer.update()
        print(label, observed, flush=True)
    finally:
        bpy.app.handlers.depsgraph_update_post.remove(on_update)
