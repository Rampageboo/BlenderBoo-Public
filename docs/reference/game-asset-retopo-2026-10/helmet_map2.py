"""Spherical ray map of the helmet from centre C: first hit (liner), last hit (outer shell) per (azimuth, elevation)."""
import bpy, json, math, sys
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree
OUT = sys.argv[sys.argv.index('--') + 1]
C = Vector((0.0, 0.0, 0.55))
hi = bpy.data.objects['Helmet_high']
t = BVHTree.FromObject(hi, bpy.context.evaluated_depsgraph_get())
TH = np.arange(0, 360, 1.0); PH = np.arange(-75, 90.1, 1.0)
first = np.full((len(PH), len(TH)), np.nan); last = np.full_like(first, np.nan); nh = np.zeros_like(first)
for i, ph in enumerate(PH):
    for j, th in enumerate(TH):
        d = Vector((math.cos(math.radians(ph)) * math.cos(math.radians(th)), math.cos(math.radians(ph)) * math.sin(math.radians(th)), math.sin(math.radians(ph))))
        s = 0.0; hits = []
        while len(hits) < 12:
            h = t.ray_cast(C + d * s, d, 1.2 - s)
            if not h[0]: break
            r = (h[0] - C).length; hits.append(r); s = r + 1e-4
        if hits: first[i, j] = hits[0]; last[i, j] = hits[-1]; nh[i, j] = len(hits)
np.savez(OUT, TH=TH, PH=PH, first=first, last=last, nh=nh, C=np.array(C))
print("MAP done", np.isfinite(last).mean())
