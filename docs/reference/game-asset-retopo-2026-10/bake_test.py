"""Quick base-colour bake at 1K for several cage settings; reports black texels inside UV islands."""
import bpy, sys, json, numpy as np
a = sys.argv[sys.argv.index('--') + 1:]; NAME = a[0]; SETS = json.loads(a[1]); OUTDIR = a[2]
sc = bpy.context.scene; vl = bpy.context.view_layer
lo = bpy.data.objects[NAME + '_low']; hi = bpy.data.objects[NAME + '_high']
hi.hide_set(False); hi.hide_render = False
sc.render.engine = 'CYCLES'
prefs = bpy.context.preferences.addons['cycles'].preferences
prefs.compute_device_type = 'OPTIX'; prefs.get_devices()
for d in prefs.devices: d.use = d.type != 'CPU'
sc.cycles.device = 'GPU'; sc.cycles.samples = 1
R = 1024
lo.data.materials.clear(); mat = bpy.data.materials.new("T"); mat.use_nodes = True; lo.data.materials.append(mat)
img = bpy.data.images.new("bt", R, R); n = mat.node_tree.nodes.new('ShaderNodeTexImage'); n.image = img
mat.node_tree.nodes.active = n
mask = bpy.data.images.new("mask", R, R);
hm = hi.active_material.node_tree; hb = hm.nodes['Principled BSDF']; outn = hm.nodes['Material Output']
em = hm.nodes.new('ShaderNodeEmission'); hm.links.new(hb.inputs['Base Color'].links[0].from_socket, em.inputs['Color'])
hm.links.new(em.outputs[0], outn.inputs['Surface'])
for o in vl.objects: o.select_set(False)
hi.select_set(True); lo.select_set(True); vl.objects.active = lo
b = sc.render.bake; b.use_selected_to_active = True; b.target = 'IMAGE_TEXTURES'; b.margin = 0
# coverage mask: bake a white emission from the low itself
lm = mat.node_tree; le = lm.nodes.new('ShaderNodeEmission'); le.inputs['Color'].default_value = (1, 1, 1, 1)
lo_out = lm.nodes['Material Output']; old = lo_out.inputs['Surface'].links[0].from_socket
lm.links.new(le.outputs[0], lo_out.inputs['Surface'])
for o in vl.objects: o.select_set(False)
lo.select_set(True); vl.objects.active = lo; b.use_selected_to_active = False
bpy.ops.object.bake(type='EMIT')
MASK = np.array(img.pixels[:]).reshape(-1, 4)[:, 0] > 0.5
lm.links.new(old, lo_out.inputs['Surface'])
hi.select_set(True); lo.select_set(True); vl.objects.active = lo; b.use_selected_to_active = True
print("BT uv coverage", round(MASK.mean(), 4))
for cage, maxd in SETS:
    img.pixels[:] = [1.0, 0.0, 1.0, 1.0] * (R * R)                  # magenta = never written (outside islands)
    b.cage_extrusion = cage; b.max_ray_distance = maxd
    bpy.ops.object.bake(type='EMIT')
    p = np.array(img.pixels[:]).reshape(-1, 4)[:, :3]
    inside = MASK
    black = (p.max(1) < 0.015) & inside
    print("BT cage", cage, "maxd", maxd, "covered", int(inside.sum()), "black", int(black.sum()), "pct", round(100 * black.sum() / max(1, inside.sum()), 2))
    img.filepath_raw = f"{OUTDIR}/bt_{NAME}_{cage}_{maxd}.png"; img.file_format = 'PNG'; img.save()
