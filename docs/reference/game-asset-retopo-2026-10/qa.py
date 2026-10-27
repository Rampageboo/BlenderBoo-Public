"""Generic low-poly QA: topology, slivers, self-intersections between non-adjacent faces, open-edge locations,
surface deviation vs. the high-poly. Usage: blender -b file --python qa.py -- LOWNAME HIGHNAME OUTJSON SLIVER"""
import bpy, bmesh, json, sys, collections, random
from mathutils.bvhtree import BVHTree
a = sys.argv[sys.argv.index('--') + 1:]; LOW, HIGH, OUT, SLIV = a[0], a[1], a[2], float(a[3])
lo = bpy.data.objects[LOW]; hi = bpy.data.objects[HIGH]
bm = bmesh.new(); bm.from_mesh(lo.data); bm.faces.ensure_lookup_table()
r = {}
sz = collections.Counter(len(f.verts) for f in bm.faces)
r['faces'] = dict(sorted(sz.items())); r['tris'] = sum(len(f.verts) - 2 for f in bm.faces)
r['open_edges'] = sum(1 for e in bm.edges if e.is_boundary)
r['open_edge_z'] = sorted(collections.Counter(round((e.verts[0].co.z + e.verts[1].co.z) / 2, 3) for e in bm.edges if e.is_boundary).items())[:8]
r['nonmanifold_gt2'] = sum(1 for e in bm.edges if len(e.link_faces) > 2)
r['short_edges'] = sum(1 for e in bm.edges if e.calc_length() < SLIV)
r['zero_area'] = sum(1 for f in bm.faces if f.calc_area() < SLIV * SLIV * 0.05)
r['valence'] = dict(sorted(collections.Counter(len(v.link_edges) for v in bm.verts).items()))
t = BVHTree.FromBMesh(bm)
pairs = [(i, j) for i, j in t.overlap(t) if i < j and not (set(v.index for v in bm.faces[i].verts) & set(v.index for v in bm.faces[j].verts))]
r['self_intersections'] = len(pairs)
r['self_int_at'] = [tuple(round(c, 3) for c in ((bm.faces[i].calc_center_median() + bm.faces[j].calc_center_median()) / 2)) for i, j in pairs[:8]]
# deviation: sample low surface points, distance to the high surface
dg = bpy.context.evaluated_depsgraph_get()
th = BVHTree.FromObject(hi, dg)
random.seed(1); ds = []
for f in bm.faces:
    for _ in range(max(1, int(f.calc_area() / 1e-4))):
        p = f.calc_center_median().lerp(random.choice(f.verts).co, random.random() * 0.9)
        h = th.find_nearest(p)
        if h[0]: ds.append(h[3])
ds.sort()
r['deviation'] = dict(n=len(ds), median=round(ds[len(ds) // 2], 5), p90=round(ds[int(len(ds) * .9)], 5), p99=round(ds[int(len(ds) * .99)], 5), max=round(ds[-1], 5))
json.dump(r, open(OUT, 'w'), indent=1)
print('QA', json.dumps(r))
