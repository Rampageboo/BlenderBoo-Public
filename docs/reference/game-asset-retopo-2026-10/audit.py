"""Game-asset audit: UV overlap, texel density (px/cm at the given texture size), occluded faces near other parts."""
import bpy, bmesh, sys, math, json, statistics
import numpy as np
from mathutils.bvhtree import BVHTree
a = sys.argv[sys.argv.index('--') + 1:]; NAME, TEX = a[0], int(a[1])
o = bpy.data.objects[NAME + '_low']; me = o.data
bm = bmesh.new(); bm.from_mesh(me); bm.faces.ensure_lookup_table(); uv = bm.loops.layers.uv.active
r = {}
# texel density: sqrt(uv area * TEX^2 / world area) -> px per metre (object is at real scale)
dens = []
for f in bm.faces:
    p = [l[uv].uv for l in f.loops]
    ua = abs(sum(p[i].x * p[(i + 1) % len(p)].y - p[(i + 1) % len(p)].x * p[i].y for i in range(len(p)))) / 2
    wa = f.calc_area()
    if wa > 1e-12: dens.append((math.sqrt(ua / wa) * TEX / 100.0, wa))      # px per cm
ds = sorted(d for d, _ in dens)
r['px_per_cm'] = dict(p5=round(ds[len(ds) // 20], 2), median=round(ds[len(ds) // 2], 2), p95=round(ds[len(ds) * 19 // 20], 2))
# UV overlap: rasterise every face into a 1024 grid and count texels covered more than once
N = 1024; cov = np.zeros((N, N), np.uint16)
for f in bm.faces:
    P = np.array([[l[uv].uv.x * N, l[uv].uv.y * N] for l in f.loops])
    x0, y0 = np.floor(P.min(0)).astype(int); x1, y1 = np.ceil(P.max(0)).astype(int)
    x0, y0 = max(x0, 0), max(y0, 0); x1, y1 = min(x1, N - 1), min(y1, N - 1)
    if x1 < x0 or y1 < y0: continue
    xs, ys = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
    inside = np.zeros(xs.shape, bool)
    for t in range(1, len(P) - 1):                      # fan triangles
        A, B, C = P[0], P[t], P[t + 1]
        def s(p1, p2): return (xs - p2[0]) * (p1[1] - p2[1]) - (p1[0] - p2[0]) * (ys - p2[1])
        d1, d2, d3 = s(A, B), s(B, C), s(C, A)
        inside |= ~(((d1 < 0) | (d2 < 0) | (d3 < 0)) & ((d1 > 0) | (d2 > 0) | (d3 > 0)))
    cov[y0:y1 + 1, x0:x1 + 1] += inside
r['uv_coverage'] = round(float((cov > 0).mean()), 4)
r['uv_overlap_texels_pct'] = round(float((cov > 1).sum() / max((cov > 0).sum(), 1) * 100), 3)
# occluded faces: a face whose centre, pushed 1 mm along its normal, is inside / within 1.5 mm of another face of the mesh
t = BVHTree.FromBMesh(bm); occl = 0; occ_area = 0.0; tot = 0.0
for f in bm.faces:
    c = f.calc_center_median(); n = f.normal; tot += f.calc_area()
    h = t.ray_cast(c + n * 1e-4, n, 0.004)
    if h[0] is not None and h[2] != f.index: occl += 1; occ_area += f.calc_area()
r['faces_facing_another_face_within_4mm'] = occl
r['tris'] = sum(len(f.verts) - 2 for f in bm.faces)
print("AUDIT", NAME, json.dumps(r))
