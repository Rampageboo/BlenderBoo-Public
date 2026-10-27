"""Orthographic silhouette masks of two objects from 4 axes (same camera), saved as PNG + IoU numbers."""
import bpy, sys, json, math
import numpy as np
from mathutils import Vector
a = sys.argv[sys.argv.index('--') + 1:]; HI, LO, OUT = a[0], a[1], a[2]
sc = bpy.context.scene
sc.render.engine = 'BLENDER_WORKBENCH'; sh = sc.display.shading; sh.light = 'FLAT'; sh.color_type = 'SINGLE'; sh.single_color = (1, 1, 1)
sc.render.film_transparent = True; sc.render.resolution_x = sc.render.resolution_y = 800
cam = bpy.data.objects.new('SC', bpy.data.cameras.new('SC')); sc.collection.objects.link(cam); sc.camera = cam
cam.data.type = 'ORTHO'; cam.data.ortho_scale = 1.15
T = Vector((0, 0, 0.5))
views = {'front': (0, -1, 0), 'side': (1, 0, 0), 'top': (0, 0, 1), 'back': (0, 1, 0)}
res = {}
def render(name, v):
    for o in sc.objects:
        if o.type == 'MESH': o.hide_render = o.name != name
    d = Vector(v); cam.location = T + d * 3; cam.rotation_euler = (-d).to_track_quat('-Z', 'Y').to_euler()
    path = f"{OUT}_{name}_{vn}.png"; sc.render.filepath = path; bpy.ops.render.render(write_still=True)
    im = bpy.data.images.load(path); p = np.array(im.pixels[:]).reshape(800, 800, 4)[:, :, 3] > 0.5; bpy.data.images.remove(im); return p
for vn, v in views.items():
    A = render(HI, v); B = render(LO, v)
    inter = (A & B).sum(); uni = (A | B).sum()
    res[vn] = dict(iou=round(inter / uni, 4), src_only=int((A & ~B).sum()), low_only=int((B & ~A).sum()))
    rgb = np.zeros((800, 800, 3)); rgb[A & B] = (0.75, 0.75, 0.75); rgb[A & ~B] = (1, 0.2, 0.2); rgb[B & ~A] = (0.2, 0.6, 1)
    np.save(f"{OUT}_{vn}.npy", rgb)
json.dump(res, open(OUT + '_iou.json', 'w')); print("SIL", json.dumps(res))
