"""Quad plates on the shell from residual contours: grid cells inside the outline, boundary vertices snapped onto the
outline polygon, heights from the filtered residual. Output in (theta, phi, h) for Blender to project."""
import json, sys, math
import numpy as np
from scipy import ndimage as nd
import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
from matplotlib.path import Path

F = np.load(sys.argv[1]); OUT = sys.argv[2]
TH, PH, res = F['TH'], F['PH'], np.nan_to_num(F['res'])
# height field: flatten slot holes (median 3 deg) then soften
H = nd.gaussian_filter(nd.median_filter(res, size=(3, 3), mode='wrap'), 0.8, mode=('nearest', 'wrap'))

def hval(t, p):
    i = (p - PH[0]); j = t % 360
    i0, j0 = int(math.floor(i)), int(math.floor(j)); di, dj = i - i0, j - j0
    a = H[i0, j0 % 360]; b = H[i0, (j0 + 1) % 360]; c = H[i0 + 1, j0 % 360]; d = H[i0 + 1, (j0 + 1) % 360]
    return (a * (1 - dj) + b * dj) * (1 - di) + (c * (1 - dj) + d * dj) * di

def contour(t0, t1, p0, p1, level):
    sl = (TH >= t0) & (TH <= t1); sp = (PH >= p0) & (PH <= p1)
    cs = plt.contour(TH[sl], PH[sp], H[np.ix_(sp, sl)], levels=[level])
    paths = [p.vertices for p in cs.get_paths()]
    plt.close('all')
    segs = []
    for v in paths:
        # split multi-loop paths at jumps
        cut = np.where(np.hypot(*np.diff(v, axis=0).T) > 3)[0]
        segs += np.split(v, cut + 1)
    segs = [s for s in segs if len(s) > 10]
    segs.sort(key=lambda s: -abs(0.5 * np.sum(s[:-1, 0] * s[1:, 1] - s[1:, 0] * s[:-1, 1])))
    return segs

def rdp(pts, eps):
    if len(pts) < 3: return pts
    a, b = pts[0], pts[-1]; ab = b - a; L = np.linalg.norm(ab)
    d = np.abs(ab[0] * (pts[:, 1] - a[1]) - ab[1] * (pts[:, 0] - a[0])) / L if L > 0 else np.linalg.norm(pts - a, axis=1)
    i = int(np.argmax(d))
    if d[i] > eps: return np.vstack([rdp(pts[:i + 1], eps)[:-1], rdp(pts[i:], eps)])
    return np.vstack([a, b])

def snap(p, polys):
    best = None
    for poly in polys:
        P = np.vstack([poly, poly[:1]])
        for a, b in zip(P[:-1], P[1:]):
            ab = b - a; t = np.clip(np.dot(p - a, ab) / max(np.dot(ab, ab), 1e-12), 0, 1); q = a + t * ab
            dd = np.linalg.norm(p - q)
            if best is None or dd < best[0]: best = (dd, q)
    return best[1]

def plate(outer, holes, step, hmin, hmax, hfix=None):
    allp = [outer] + holes
    t0, t1 = outer[:, 0].min(), outer[:, 0].max(); p0, p1 = outer[:, 1].min(), outer[:, 1].max()
    ts = np.arange(t0 - step, t1 + step * 1.01, step); ps = np.arange(p0 - step, p1 + step * 1.01, step)
    po = Path(outer); ph_ = [Path(h) for h in holes]
    inside = lambda q: po.contains_point(q) and not any(h.contains_point(q) for h in ph_)
    cells = [(i, j) for i in range(len(ts) - 1) for j in range(len(ps) - 1)
             if inside(((ts[i] + ts[i + 1]) / 2, (ps[j] + ps[j + 1]) / 2))]
    cset = set(cells)
    verts = {}
    for (i, j) in cells:
        for k in ((i, j), (i + 1, j), (i, j + 1), (i + 1, j + 1)): verts[k] = np.array([ts[k[0]], ps[k[1]]], float)
    # boundary vertices: touch a missing cell
    def is_b(k):
        i, j = k
        return any((ii, jj) not in cset for ii in (i - 1, i) for jj in (j - 1, j))
    for k in list(verts):
        if is_b(k): verts[k] = snap(verts[k], allp)
    # drop cells that collapsed after snapping
    def area(q):
        P = [verts[x] for x in q]; return 0.5 * abs(sum(P[a][0] * P[(a + 1) % 4][1] - P[(a + 1) % 4][0] * P[a][1] for a in range(4)))
    cells = [c for c in cells if area([(c[0], c[1]), (c[0] + 1, c[1]), (c[0] + 1, c[1] + 1), (c[0], c[1] + 1)]) > 0.15 * step * step]
    used = {k for (i, j) in cells for k in ((i, j), (i + 1, j), (i, j + 1), (i + 1, j + 1))}
    keys = sorted(used); idx = {k: n for n, k in enumerate(keys)}
    vlist = []
    for k in keys:
        t, p = verts[k]
        h = hfix(t, p) if hfix else float(np.clip(hval(t, p), hmin, hmax))
        vlist.append([float(t), float(p), h])
    return dict(verts=vlist, cells=[[idx[(i, j)], idx[(i + 1, j)], idx[(i + 1, j + 1)], idx[(i, j + 1)]] for (i, j) in cells])

out = {}
# ear wing (right side; left mirrored in Blender by theta -> 180 - theta)
segs = contour(23, 58, -27, 16.5, 0.012)
W = segs[0]
W = W[(W[:, 1] <= 16.0)] if False else W
wing_outer = rdp(np.vstack([W, W[:1]]), 0.6)[:-1]
out['wing'] = plate(wing_outer, [], 3.0, 0.012, 0.055)
# NVG frame: outer outline + window hole
segs = contour(248, 292, 9, 53, 0.010)
nv_outer = rdp(np.vstack([segs[0], segs[0][:1]]), 0.6)[:-1]
holes = [rdp(np.vstack([s, s[:1]]), 0.6)[:-1] for s in segs[1:2]]
def nvg_h(t, p):
    if p < 17.5: return 0.05
    if p > 41.5: return 0.02
    if t < 257 or t > 282: return 0.045
    if p > 34: return 0.038
    return 0.022
out['nvg'] = plate(nv_outer, holes, 3.0, 0.010, 0.055, hfix=nvg_h)
out['outlines'] = dict(wing=wing_outer.tolist(), nvg=nv_outer.tolist(), nvg_holes=[h.tolist() for h in holes])
json.dump(out, open(OUT, 'w'))
for k in ('wing', 'nvg'):
    print("PLATE", k, "verts", len(out[k]['verts']), "quads", len(out[k]['cells']), "outline pts", len(out['outlines'][k]))
fig, ax = plt.subplots(1, 2, figsize=(16, 8))
for a, (k, o) in zip(ax, (('wing', wing_outer), ('nvg', nv_outer))):
    V = np.array(out[k]['verts'])
    for c in out[k]['cells']:
        q = V[c + [c[0]], :2]; a.plot(q[:, 0], q[:, 1], 'k-', lw=0.5)
    oo = np.vstack([o, o[:1]]); a.plot(oo[:, 0], oo[:, 1], 'r-', lw=1.5)
    if k == 'nvg':
        for h in holes: hh = np.vstack([h, h[:1]]); a.plot(hh[:, 0], hh[:, 1], 'b-', lw=1.5)
    sc_ = a.scatter(V[:, 0], V[:, 1], c=V[:, 2], s=8, cmap='viridis'); a.set_aspect('equal'); a.set_title(k)
plt.colorbar(sc_, ax=ax); plt.savefig(OUT.replace('.json', '.png'), dpi=60)
