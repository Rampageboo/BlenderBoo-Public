"""Nearest-surface texture transfer (no ray misses): for every texel of the low-poly UV layout, find the closest point
on the high-poly surface and sample its BaseColor / Roughness / Metallic from the high's textures.
Usage: blender -b file --python nearest_transfer.py -- NAME RES OUTDIR"""
import bpy, bmesh, sys, time, math
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree
a = sys.argv[sys.argv.index('--') + 1:]; NAME, RES, OUT = a[0], int(a[1]), a[2]
T0 = time.time()
lo = bpy.data.objects[NAME + '_low']; hi = bpy.data.objects[NAME + '_high']
dg = bpy.context.evaluated_depsgraph_get()

# --- rasterise the low-poly UV triangles: texel -> 3D position
bm = bmesh.new(); bm.from_mesh(lo.data)
PART = bm.faces.layers.int.get('part')
if PART is not None and len(a) > 3 and a[3] == 'shell_only':
    bmesh.ops.delete(bm, geom=[f for f in bm.faces if f[PART] != 0], context='FACES')
bmesh.ops.triangulate(bm, faces=bm.faces); uvl = bm.loops.layers.uv.active
P = np.full((RES, RES, 3), np.nan, np.float32)
for f in bm.faces:
    L = f.loops
    uv = np.array([[l[uvl].uv.x * RES - 0.5, l[uvl].uv.y * RES - 0.5] for l in L], np.float64)
    xyz = np.array([l.vert.co[:] for l in L], np.float64)
    x0, y0 = np.floor(uv.min(0)).astype(int); x1, y1 = np.ceil(uv.max(0)).astype(int)
    x0, y0 = max(x0, 0), max(y0, 0); x1, y1 = min(x1, RES - 1), min(y1, RES - 1)
    if x1 < x0 or y1 < y0: continue
    xs, ys = np.meshgrid(np.arange(x0, x1 + 1), np.arange(y0, y1 + 1))
    (ax, ay), (bx, by), (cx, cy) = uv
    den = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    if abs(den) < 1e-12: continue
    w0 = ((by - cy) * (xs - cx) + (cx - bx) * (ys - cy)) / den
    w1 = ((cy - ay) * (xs - cx) + (ax - cx) * (ys - cy)) / den
    w2 = 1 - w0 - w1
    m = (w0 >= -1e-4) & (w1 >= -1e-4) & (w2 >= -1e-4)
    if not m.any(): continue
    pos = w0[m, None] * xyz[0] + w1[m, None] * xyz[1] + w2[m, None] * xyz[2]
    P[ys[m], xs[m]] = pos
bm.free()
mask = np.isfinite(P[..., 0]); idx = np.argwhere(mask)
print("texels", len(idx), round(time.time() - T0, 1))

# --- nearest point on the high-poly
hme = hi.evaluated_get(dg).data
nv = len(hme.vertices); co = np.empty(nv * 3); hme.vertices.foreach_get('co', co); co = co.reshape(-1, 3)
nl = len(hme.loops); lv = np.empty(nl, np.int64); hme.loops.foreach_get('vertex_index', lv)
luv = np.empty(nl * 2); hme.uv_layers.active.data.foreach_get('uv', luv); luv = luv.reshape(-1, 2)
npoly = len(hme.polygons); ls = np.empty(npoly, np.int64); hme.polygons.foreach_get('loop_start', ls)
tree = BVHTree.FromObject(hi, dg)
Q = P[mask]; fi = np.empty(len(Q), np.int64); loc = np.empty((len(Q), 3))
for k in range(len(Q)):
    h = tree.find_nearest(Vector(Q[k]))
    loc[k] = h[0]; fi[k] = h[2]
print("nearest done", round(time.time() - T0, 1))
# barycentric in the high triangle -> UV
L0 = ls[fi]; ia, ib, ic = L0, L0 + 1, L0 + 2
A, B, C = co[lv[ia]], co[lv[ib]], co[lv[ic]]
v0, v1, v2 = B - A, C - A, loc - A
d00 = (v0 * v0).sum(1); d01 = (v0 * v1).sum(1); d11 = (v1 * v1).sum(1); d20 = (v2 * v0).sum(1); d21 = (v2 * v1).sum(1)
den = d00 * d11 - d01 * d01; den[np.abs(den) < 1e-20] = 1e-20
bv = (d11 * d20 - d01 * d21) / den; bw = (d00 * d21 - d01 * d20) / den; bu = 1 - bv - bw
UV = bu[:, None] * luv[ia] + bv[:, None] * luv[ib] + bw[:, None] * luv[ic]

# --- sample the high's textures (base colour = image linked to Base Color; ORM-like image: G rough, B metal)
nt = hi.active_material.node_tree; bsdf = nt.nodes['Principled BSDF']
def src_img(sock):
    n = bsdf.inputs[sock].links[0].from_node
    while n.type != 'TEX_IMAGE': n = n.inputs[0].links[0].from_node
    return n.image
def to_np(im):
    w, h = im.size; a_ = np.empty(w * h * 4, np.float32); im.pixels.foreach_get(a_); return a_.reshape(h, w, 4)
def bilinear(img, uv):
    h, w = img.shape[:2]; x = (uv[:, 0] % 1.0) * w - 0.5; y = (uv[:, 1] % 1.0) * h - 0.5
    x0 = np.floor(x).astype(int); y0 = np.floor(y).astype(int); fx = (x - x0)[:, None]; fy = (y - y0)[:, None]
    x0 %= w; y0 %= h; x1 = (x0 + 1) % w; y1 = (y0 + 1) % h
    return (img[y0, x0] * (1 - fx) + img[y0, x1] * fx) * (1 - fy) + (img[y1, x0] * (1 - fx) + img[y1, x1] * fx) * fy
bc = src_img('Base Color'); rm = src_img('Roughness')
BC = bilinear(to_np(bc), UV); RM = bilinear(to_np(rm), UV)

def dilate(img, msk, it=24):
    img = img.copy(); m = msk.copy()
    for _ in range(it):
        acc = np.zeros_like(img); cnt = np.zeros(m.shape, np.float32)
        for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            sm = np.roll(m, (dy, dx), (0, 1)); si = np.roll(img, (dy, dx), (0, 1))
            acc += si * sm[..., None]; cnt += sm
        new = (~m) & (cnt > 0)
        img[new] = acc[new] / cnt[new][:, None]; m = m | new
    return img
def save(name, arr3, noncolor):
    full = np.zeros((RES, RES, 4), np.float32); full[..., 3] = 1
    full[mask, :3] = arr3
    full[..., :3] = dilate(full[..., :3], mask)
    im = bpy.data.images.new(f"T_{NAME}_{name}", RES, RES, alpha=False)
    im.colorspace_settings.name = 'Non-Color' if noncolor else 'sRGB'
    im.pixels.foreach_set(full.ravel()); im.filepath_raw = f"{OUT}/T_{NAME}_{name}.png"; im.file_format = 'PNG'; im.save()
save('BaseColor', BC[:, :3], False)
save('Roughness', np.repeat(RM[:, 1:2], 3, 1), True)
save('Metallic', np.repeat(RM[:, 2:3], 3, 1), True)
mk = bpy.data.images.new(f"T_{NAME}_ShellMask", RES, RES); mm = np.zeros((RES, RES, 4), np.float32); mm[..., 3] = 1; mm[mask, :3] = 1
mk.pixels.foreach_set(mm.ravel()); mk.filepath_raw = f"{OUT}/T_{NAME}_ShellMask.png"; mk.file_format = 'PNG'; mk.save()
print("TRANSFER done", round(time.time() - T0, 1))
