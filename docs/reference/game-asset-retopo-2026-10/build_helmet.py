import bpy, bmesh, json, math, sys
from mathutils import Vector
from mathutils.bvhtree import BVHTree
a = sys.argv[sys.argv.index('--') + 1:]; G = json.load(open(a[0])); OUT = a[1]
C = Vector(G['C']); sc = bpy.context.scene
LIFT = 0.0008

def mesh(name, data):
    me = bpy.data.meshes.new(name); me.from_pydata([tuple(v) for v in data['verts']], [], data['faces']); me.update()
    o = bpy.data.objects.new(name, me); sc.collection.objects.link(o); return o

def dirv(th, ph):
    t, p = math.radians(th), math.radians(ph)
    return Vector((math.cos(p) * math.cos(t), math.cos(p) * math.sin(t), math.sin(p)))

shell = mesh("Helmet_shell", G['shell'])
bm = bmesh.new(); bm.from_mesh(shell.data); bmesh.ops.recalc_face_normals(bm, faces=bm.faces); bm.to_mesh(shell.data); bm.free()
bvh = BVHTree.FromObject(shell, bpy.context.evaluated_depsgraph_get())
LIFTS = {}
def on_shell(th, ph, lift=None):
    d = dirv(th, ph); h = bvh.ray_cast(C + d * 1.5, -d, 1.5)
    return (h[0] + d * (LIFT if lift is None else lift)) if h[0] else None, d

def resample(path, step=2.5):
    out = []
    for (t0, p0, w0), (t1, p1, w1) in zip(path, path[1:]):
        if t1 - t0 > 180: t1 -= 360
        if t1 - t0 < -180: t1 += 360
        n = max(1, int(math.hypot(t1 - t0, p1 - p0) / step))
        for k in range(n): f = k / n; out.append((t0 + (t1 - t0) * f, p0 + (p1 - p0) * f, w0 + (w1 - w0) * f))
    out.append(tuple(path[-1])); return out

def rail(path, wdeg, h, lift, K=4):
    """rail band: K points across the width (follows shell curvature), bottom on the shell + lift, top = bottom + h"""
    pts = resample(path); b = bmesh.new(); secs = []
    for k, (th, ph, wd) in enumerate(pts):
        A = pts[max(k - 1, 0)]; B = pts[min(k + 1, len(pts) - 1)]
        dt = B[0] - A[0]; dt = dt - 360 if dt > 180 else dt + 360 if dt < -180 else dt
        tx, ty = dt * math.cos(math.radians(ph)), B[1] - A[1]; L = math.hypot(tx, ty); tx, ty = tx / L, ty / L
        bot, top = [], []
        for q in range(K):
            s_ = -1 + 2 * q / (K - 1)
            nx, ny = -ty * wd / 2 * s_, tx * wd / 2 * s_
            p, d = on_shell(th + nx / max(math.cos(math.radians(ph)), 0.3), ph + ny, lift)
            bot.append(b.verts.new(p)); top.append(b.verts.new(p + d * h))
        secs.append(bot + top[::-1])                    # ring: bottom left->right, top right->left
    R = 2 * K
    for A, B in zip(secs, secs[1:]):
        for e in range(R): b.faces.new((A[e], B[e], B[(e + 1) % R], A[(e + 1) % R]))
    for S_, rev in ((secs[0], True), (secs[-1], False)):
        for q in range(K - 1):
            f = [S_[q], S_[q + 1], S_[R - 2 - q], S_[R - 1 - q]]
            b.faces.new(f[::-1] if rev else f)
    bmesh.ops.recalc_face_normals(b, faces=b.faces)
    me = bpy.data.meshes.new("Helmet_rail"); b.to_mesh(me); b.free()
    o = bpy.data.objects.new("Helmet_rail", me); sc.collection.objects.link(o); return o

def clear_of_shell(build, name):
    lift = LIFT
    while True:
        o = build(lift)
        n_ = len(BVHTree.FromObject(o, bpy.context.evaluated_depsgraph_get()).overlap(bvh))
        if n_ == 0 or lift > 0.008:
            print("LIFT", name, round(lift, 5), "remaining", n_); return o
        bpy.data.objects.remove(o); lift *= 1.35
for k, p in enumerate(G.get('rails') or []):
    clear_of_shell(lambda lf, p=p: rail([tuple(x) for x in p], G['rail_width_deg'], G['rail_height'], lf), f"rail{k}")

def sweep(path, closed, lift, K=None, cham=0.18):
    if K is None: K = 4 if max(x[2] for x in path) > 4.5 else 3
    P = [tuple(x) for x in path]
    if closed: P = P + [P[0]]
    pts = resample_w(P, 3.5)
    if closed: pts = pts[:-1]
    n = len(pts); b = bmesh.new(); secs = []
    for k, (th, ph, wd, hh) in enumerate(pts):
        A = pts[(k - 1) % n] if closed else pts[max(k - 1, 0)]; B = pts[(k + 1) % n] if closed else pts[min(k + 1, n - 1)]
        dt = B[0] - A[0]; dt = dt - 360 if dt > 180 else dt + 360 if dt < -180 else dt
        tx, ty = dt * math.cos(math.radians(ph)), B[1] - A[1]; Lt = math.hypot(tx, ty); tx, ty = tx / Lt, ty / Lt
        # mitre: widen where the path turns
        if closed or 0 < k < n - 1:
            a0 = pts[(k - 1) % n]; a1 = pts[(k + 1) % n]
            d0 = ((th - a0[0] + 540) % 360 - 180) * math.cos(math.radians(ph)), ph - a0[1]
            d1 = ((a1[0] - th + 540) % 360 - 180) * math.cos(math.radians(ph)), a1[1] - ph
            l0, l1 = math.hypot(*d0), math.hypot(*d1)
            cosb = max(0.5, (d0[0] * d1[0] + d0[1] * d1[1]) / max(l0 * l1, 1e-9) * 0.5 + 0.5) ** 0.5
            wd = wd / cosb
        bot, top = [], []
        for q in range(K):
            s_ = -1 + 2 * q / (K - 1)
            for lst, ss, hh_ in ((bot, s_, 0.0), (top, s_ * (1 - cham), hh)):
                nx, ny = -ty * wd / 2 * ss, tx * wd / 2 * ss
                p_, d_ = on_shell(th + nx / max(math.cos(math.radians(ph)), 0.3), ph + ny, lift)
                lst.append(b.verts.new(p_ + d_ * hh_))
        secs.append(bot + top[::-1])
    R = 2 * K
    rng = range(n) if closed else range(n - 1)
    for k in rng:
        A_, B_ = secs[k], secs[(k + 1) % n]
        for e in range(K - 1, R - 1):            # skip the K-1 bottom strips (hidden against the shell)
            b.faces.new((A_[e], B_[e], B_[(e + 1) % R], A_[(e + 1) % R]))
        e = R - 1; b.faces.new((A_[e], B_[e], B_[0], A_[0]))
    if not closed:
        for S_, rev in ((secs[0], True), (secs[-1], False)):
            for q in range(K - 1):
                f = [S_[q], S_[q + 1], S_[R - 2 - q], S_[R - 1 - q]]; b.faces.new(f[::-1] if rev else f)
    bmesh.ops.recalc_face_normals(b, faces=b.faces)
    me = bpy.data.meshes.new("Helmet_sweep"); b.to_mesh(me); b.free()
    o = bpy.data.objects.new("Helmet_sweep", me); sc.collection.objects.link(o); return o

def resample_w(path, step):
    out = []
    for (t0, p0, w0, h0), (t1, p1, w1, h1) in zip(path, path[1:]):
        if t1 - t0 > 180: t1 -= 360
        if t1 - t0 < -180: t1 += 360
        nn = max(1, int(math.hypot((t1 - t0) * math.cos(math.radians((p0 + p1) / 2)), p1 - p0) / step))
        for k in range(nn): f = k / nn; out.append((t0 + (t1 - t0) * f, p0 + (p1 - p0) * f, w0 + (w1 - w0) * f, h0 + (h1 - h0) * f))
    out.append(tuple(path[-1])); return out

def nvg_frame(lift):
    cs = [252, 262, 270, 278, 287]; rs = [12, 18, 26, 33, 41]
    win = {(1, 1), (1, 2), (2, 1), (2, 2)}
    cells = {(i, j) for i in range(4) for j in range(4) if (i, j) not in win}
    ch = lambda i, j: 0.05 if j == 0 else (0.038 if j == 3 else 0.045)
    b = bmesh.new(); Vb = {}; Vt = {}
    for i in range(5):
        for j in range(5):
            adj = [(ii, jj) for ii in (i - 1, i) for jj in (j - 1, j) if (ii, jj) in cells]
            if not adj: continue
            h = max(ch(*c) for c in adj)
            p_, d_ = on_shell(cs[i], rs[j], lift); Vb[(i, j)] = b.verts.new(p_); Vt[(i, j)] = b.verts.new(p_ + d_ * h)
    E = {}
    for (i, j) in cells:
        q = [(i, j), (i + 1, j), (i + 1, j + 1), (i, j + 1)]
        b.faces.new([Vt[k] for k in q]); b.faces.new([Vb[k] for k in q][::-1])
        for e in zip(q, q[1:] + q[:1]): k_ = tuple(sorted(e)); E[k_] = E.get(k_, 0) + 1
    for (u_, v_), c in E.items():
        if c == 1: b.faces.new((Vb[u_], Vb[v_], Vt[v_], Vt[u_]))
    bmesh.ops.recalc_face_normals(b, faces=b.faces)
    me = bpy.data.meshes.new("Helmet_nvg"); b.to_mesh(me); b.free()
    o = bpy.data.objects.new("Helmet_nvg", me); sc.collection.objects.link(o); return o
def plate_obj(pl, mirror, lift):
    b = bmesh.new(); Vb = []; Vt = []
    for t, p_, h in pl['verts']:
        t = (180 - t) % 360 if mirror else t
        q, d = on_shell(t, p_, lift); Vb.append(b.verts.new(q)); Vt.append(b.verts.new(q + d * h))
    E = {}
    for c in pl['cells']:
        b.faces.new([Vt[i] for i in c])
        for e in zip(c, c[1:] + c[:1]): k_ = tuple(sorted(e)); E[k_] = E.get(k_, 0) + 1
    for (u_, v_), cnt in E.items():
        if cnt == 1: b.faces.new((Vb[u_], Vb[v_], Vt[v_], Vt[u_]))
    bmesh.ops.recalc_face_normals(b, faces=b.faces)
    me = bpy.data.meshes.new("Helmet_plate"); b.to_mesh(me); b.free()
    o = bpy.data.objects.new("Helmet_plate", me); sc.collection.objects.link(o); return o
PL = G['plates']
clear_of_shell(lambda lf: plate_obj(PL['wing'], False, lf), "wingR")
clear_of_shell(lambda lf: plate_obj(PL['wing'], True, lf), "wingL")
clear_of_shell(lambda lf: plate_obj(PL['nvg'], False, lf), "nvg")
for k, sw in enumerate(G['sweeps']):
    clear_of_shell(lambda lf, sw=sw: sweep(sw['path'], sw['closed'], lf), f"sweep{k}")

mesh("Helmet_pad", G['pad'])
# straps from measured 3D centre lines (cross-section centres of the source straps)
def strap3d(pts, w=0.04, t=0.006, N=None, up=None):
    from mathutils import Vector as V3
    P = [V3(p) for p in pts]
    # resample to ~0.02 spacing (Catmull-Rom)
    out = []
    for k in range(len(P) - 1):
        p0, p1, p2, p3 = P[max(k - 1, 0)], P[k], P[k + 1], P[min(k + 2, len(P) - 1)]
        n = max(1, int((p2 - p1).length / 0.02))
        for j in range(n):
            u = j / n
            out.append(0.5 * ((2 * p1) + (-p0 + p2) * u + (2 * p0 - 5 * p1 + 4 * p2 - p3) * u * u + (-p0 + 3 * p1 - 3 * p2 + p3) * u ** 3))
    out.append(P[-1])
    b = bmesh.new(); secs = []
    for k, c in enumerate(out):
        tg = (out[min(k + 1, len(out) - 1)] - out[max(k - 1, 0)]).normalized()
        nrm = V3(up) if up else V3((c.x, c.y, 0)).normalized()
        bw = tg.cross(nrm).normalized(); nt = bw.cross(tg).normalized()
        if nt.dot(nrm) < 0: nt = -nt
        secs.append([b.verts.new(c + bw * w / 2 + nt * t / 2), b.verts.new(c - bw * w / 2 + nt * t / 2),
                     b.verts.new(c - bw * w / 2 - nt * t / 2), b.verts.new(c + bw * w / 2 - nt * t / 2)])
    for A, B in zip(secs, secs[1:]):
        for e in range(4): b.faces.new((A[e], B[e], B[(e + 1) % 4], A[(e + 1) % 4]))
    b.faces.new(secs[0][::-1]); b.faces.new(secs[-1])
    bmesh.ops.recalc_face_normals(b, faces=b.faces)
    me = bpy.data.meshes.new("Helmet_strap"); b.to_mesh(me); b.free()
    o = bpy.data.objects.new("Helmet_strap", me); sc.collection.objects.link(o); return o
FRONT = [(0.274, -0.224, 0.56), (0.258, -0.244, 0.42), (0.226, -0.237, 0.32), (0.207, -0.239, 0.24), (0.200, -0.240, 0.19), (0.199, -0.236, 0.152)]
RSIDE = [(0.200, -0.200, 0.12), (0.215, -0.110, 0.14), (0.227, -0.023, 0.16), (0.236, 0.017, 0.18), (0.243, 0.057, 0.20),
         (0.247, 0.117, 0.22), (0.252, 0.165, 0.235)]
CHIN = [(0.15, -0.29, 0.11), (0.0, -0.334, 0.10), (-0.15, -0.29, 0.11)]
mx = lambda L: [(-x, y, z) for x, y, z in L]
strap3d(FRONT); strap3d(mx(FRONT))
loop = list(reversed(RSIDE)) + CHIN + mx(RSIDE)
strap3d(loop)
CUP = [(0.13, -0.252, 0.058), (0.09, -0.256, 0.032), (0.0, -0.262, 0.014), (-0.09, -0.256, 0.032), (-0.13, -0.252, 0.058)]
strap3d(CUP, w=0.046, t=0.008, up=(0, 0, -1))

import itertools
parts = [o for o in bpy.data.objects if o.name.startswith("Helmet_") and o.name != "Helmet_high"]
dg = bpy.context.evaluated_depsgraph_get()
T = {o.name: BVHTree.FromObject(o, dg) for o in parts}
for o in parts:
    bmx = bmesh.new(); bmx.from_mesh(o.data); bmx.faces.ensure_lookup_table()
    pr = [(i, j) for i, j in T[o.name].overlap(T[o.name]) if i < j and not (set(bmx.faces[i].verts) & set(bmx.faces[j].verts))]
    if pr: print("XSELF", o.name, len(pr), [tuple(round(c, 3) for c in bmx.faces[i].calc_center_median()) for i, j in pr[:4]])
    bmx.free()
for a_, b_ in itertools.combinations(parts, 2):
    n_ = len(T[a_.name].overlap(T[b_.name]))
    if n_: print("XPAIR", a_.name, b_.name, n_)
for k_, o_ in enumerate([o for o in bpy.data.objects if o.name.startswith("Helmet_") and o.name != "Helmet_high"]):
    at = o_.data.attributes.new("part", 'INT', 'FACE')
    pid = 0 if o_.name == "Helmet_shell" else k_ + 1
    for d_ in at.data: d_.value = pid
vl = bpy.context.view_layer
for x in sc.objects: x.select_set(False)
for o in bpy.data.objects:
    if o.name.startswith("Helmet_") and o.name != "Helmet_high": o.select_set(True)
vl.objects.active = shell; bpy.ops.object.join()
lo = vl.objects.active; lo.name = "Helmet_low"; lo.data.name = "Helmet_low"
b = bmesh.new(); b.from_mesh(lo.data)
bmesh.ops.recalc_face_normals(b, faces=b.faces)
for e in b.edges:
    if len(e.link_faces) == 2 and e.calc_face_angle(0) > math.radians(40): e.smooth = False; e.seam = True
sz = {}
for f in b.faces: sz[len(f.verts)] = sz.get(len(f.verts), 0) + 1
print("HELMET faces", dict(sorted(sz.items())), "tris", sum(len(f.verts) - 2 for f in b.faces), "open", sum(1 for e in b.edges if e.is_boundary))
b.to_mesh(lo.data); b.free()
bpy.ops.object.shade_smooth(keep_sharp_edges=True)
bpy.data.objects['Helmet_high'].hide_render = True
bpy.ops.wm.save_as_mainfile(filepath=OUT, copy=True)
