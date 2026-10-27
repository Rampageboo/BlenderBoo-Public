"""Tent low-poly v2. Fabric wrapped over the frame: eave/ridge beams are profile fillets, rafters/posts are rounded rib
bands (outside rounded, inside stepped), gable walls are quad columns with a door opening, floor grid welded to the shell.
Door rolls and door-edge poles are separate tubes with 0.5 mm clearance. Model units (x5 at export)."""
import bpy, bmesh, math, sys
OUT = sys.argv[sys.argv.index('--') + 1]

FLOOR, SLOPE, XW, EAVE = 0.013, 0.569, 0.318, 0.42
T_FAB, RIB_OUT, RIB_IN = 0.005, 0.012, 0.018
DOOR_X, DOOR_Z, SAG = 0.22, 0.37, 0.004
FOOT = FLOOR + 0.008                  # rib bulge starts just above the floor (floor line stays straight)
SEC = math.hypot(1, SLOPE)
RF = 0.016                            # eave fillet radius (fabric over the eave beam)

def roof_z(x): return EAVE + SLOPE * (XW - abs(x))
TH = math.pi - math.atan(1 / SLOPE)                     # interior eave angle
NB = (1 + SLOPE / SEC, 1 / SEC); _l = math.hypot(*NB); NB = (NB[0] / _l, NB[1] / _l)   # eave bisector (outward)

def half(d, rf, sag=0.0, bulge=0.0, floor_shift=True):
    """right-half profile offset inward by d (negative = outward), fillet radius rf at the eave.
    returns [(x, z)] floor, foot, door, p1, pm, p2, roof@DOOR_X, roof@0.03, ridge-shoulder, ridge"""
    xw = XW - d
    zl = lambda x: roof_z(x) - d * SEC                    # offset roof line
    cz = zl(xw)                                           # offset eave corner (xw, cz)
    t = rf / math.tan(TH / 2)
    p1 = (xw, cz - t); p2 = (xw - t / SEC, cz + t * SLOPE / SEC)
    dm = rf / math.sin(TH / 2) - rf
    pm = (xw - NB[0] * (dm - bulge), cz - NB[1] * (dm - bulge))
    return [(xw if floor_shift else XW, FLOOR), (xw, FOOT), (xw, DOOR_Z), p1, pm, p2,
            (DOOR_X, zl(DOOR_X) - sag), (0.03, zl(0.03) - sag * 0.5), (0.016, zl(0.0) + 0.005), (0.0, zl(0.0) + 0.011)]

RF_IN = 0.008
def ring(sag=0.0, out_k=0.0, in_rib=False):
    do = -RIB_OUT * out_k
    o = half(do, RF - do, sag, bulge=0.003, floor_shift=False)
    i = half(T_FAB + (RIB_IN if in_rib else 0.0), RF_IN, sag)
    full = lambda h: [(-x, z) for x, z in h] + [(x, z) for x, z in reversed(h)][1:]
    return full(o), full(i)

def band(yc, w):
    """rounded rib band. Outer bulge follows cos(theta); inner rib (stepped) spans the -45..+45 deg stations."""
    out = []
    for a in (-90, -45, -22.5, 0, 22.5, 45, 90):
        y, k = yc + w * math.sin(math.radians(a)), math.cos(math.radians(a))
        if a == -45: out += [(y, k, False), (y, k, True)]
        elif a == 45: out += [(y, k, True), (y, k, False)]
        else: out.append((y, k, abs(a) < 45))
    return out

G = band(-0.415, 0.015)                  # gable band y -0.430 .. -0.400
YGF0, YGF1 = G[2][0], G[3][0]            # gable fabric outer/inner faces (-45 / -22.5 deg stations)
M = band(0.0, 0.012)
mir = lambda B: [(-y, k, r) for y, k, r in reversed(B)]
ST = G + [('SAG', -0.21)] + M + [('SAG', 0.21)] + mir(G)

bm = bmesh.new(); VK = {}
def V(x, y, z):
    k = (round(x, 6), round(y, 6), round(z, 6))
    if k not in VK: VK[k] = bm.verts.new((x, y, z))
    return VK[k]
def face(vs):
    d = []
    for v in vs:
        if not d or d[-1] is not v: d.append(v)
    if len(d) > 2 and d[0] is d[-1]: d.pop()
    if len(set(d)) >= 3:
        try: return bm.faces.new(d)
        except ValueError: print("face fail", len(d))

loops = []
for s in ST:
    if s[0] == 'SAG':
        y = s[1]; o, i = ring(sag=SAG); loops.append((y, 0.0, False, o, i))
    else:
        y, k, r = s; o, i = ring(out_k=k, in_rib=r); loops.append((y, k, r, o, i))
L = [(y, k, r, [V(x, y, z) for x, z in o], [V(x, y, z) for x, z in i]) for y, k, r, o, i in loops]
n = len(L[0][3])
gable_skip = {(round(YGF0, 6), round(YGF1, 6)), (round(-YGF1, 6), round(-YGF0, 6))}
for a_, b_ in zip(L, L[1:]):
    y0, k0, r0, o0, i0 = a_; y1, k1, r1, o1, i1 = b_
    same = abs(y0 - y1) < 1e-9
    for a in range(n - 1):
        if same:
            if abs(k0 - k1) > 1e-9: face([o0[a], o0[a + 1], o1[a + 1], o1[a]])
            if r0 != r1: face([i0[a + 1], i0[a], i1[a], i1[a + 1]])
            continue
        face([o0[a], o1[a], o1[a + 1], o0[a + 1]])
        if (round(y0, 6), round(y1, 6)) not in gable_skip:
            face([i0[a + 1], i1[a + 1], i1[a], i0[a]])
# end faces: fabric annulus at both ends
for (y, k, r, o, i), rev in ((L[0], False), (L[-1], True)):
    for a in range(n - 1):
        f = [o[a + 1], o[a], i[a], i[a + 1]]; face(f[::-1] if rev else f)

# gable walls: side regions (wall .. door edge) = 2 bottom quads + 2 quads around the eave fillet; door-top columns
def gable(y, flip, inner):
    hp = inner[len(inner) // 2:]                     # right half, ridge -> floor (mirror for left)
    hp = list(reversed(hp))                         # floor, foot, door, p1, pm, p2, roofDX, r03, r016, ridge
    fl, ft, dr, p1, pm, p2, rdx = hp[:7]
    tops = hp[6:]                                   # roofDX .. ridge (x >= 0)
    quads = []
    for s_ in (1, -1):
        X = lambda q: (s_ * q[0], q[1])
        dxf, dxt, dxd = (DOOR_X, FLOOR), (DOOR_X, FOOT), (DOOR_X, DOOR_Z)
        side = [[dxf, fl, ft, dxt], [dxt, ft, dr, dxd], [dxd, dr, p1, pm], [dxd, pm, p2, rdx]]
        cols = [[(a_[0], DOOR_Z), (b_[0], DOOR_Z), b_, a_] for a_, b_ in zip(tops[1:], tops)]   # x decreasing toward ridge
        for q in side + [[(c[1][0], c[1][1]), (c[0][0], c[0][1]), c[3], c[2]] for c in cols]:
            qq = [X(p) for p in q]
            if s_ < 0: qq = qq[::-1]
            quads.append(qq)
    for q in quads:
        vs = [V(x, y, z) for x, z in q]
        face(vs if flip else vs[::-1])
    return sorted({x for x, _ in tops} | {-x for x, _ in tops}), (fl[0], ft, dr)
inner_g = [(v.co.x, v.co.z) for v in L[3][4]]
cx, gw = gable(YGF0, True, inner_g); gable(YGF1, False, inner_g)
gable(-YGF0, False, inner_g); gable(-YGF1, True, inner_g)
gx = [gw[0], -gw[0], DOOR_X, -DOOR_X]
for y0, y1 in ((YGF0, YGF1), (-YGF1, -YGF0)):
    edge = [(-DOOR_X, FLOOR), (-DOOR_X, FOOT)] + [(x, DOOR_Z) for x in cx] + [(DOOR_X, FOOT), (DOOR_X, FLOOR)]
    for (xa, za), (xb, zb) in zip(edge, edge[1:]):
        f = [V(xa, y0, za), V(xb, y0, zb), V(xb, y1, zb), V(xa, y1, za)]
        face(f if y0 < 0 else f[::-1])

# floor grid welded to shell bottoms
FX, FY = 0.378, 0.489
ys = sorted({-FY, FY} | {round(l[0], 6) for l in L})
xs = sorted({0.0, FX, -FX} | {round(s_ * v, 6) for s_ in (1, -1) for v in (XW, XW - T_FAB, XW - T_FAB - RIB_IN)} | {round(x, 6) for x in gx})
segs = [(a_[0], b_[0], a_[2] and b_[2]) for a_, b_ in zip(L, L[1:]) if abs(a_[0] - b_[0]) > 1e-9]
def footprint(cx_, cy):
    seg = next((g for g in segs if g[0] <= cy <= g[1]), None)
    if seg is None: return False
    xi = XW - T_FAB - (RIB_IN if seg[2] else 0.0)
    if xi < abs(cx_) < XW: return True
    if (YGF0 < cy < YGF1 or -YGF1 < cy < -YGF0) and DOOR_X < abs(cx_) < xi: return True
    return False
for i in range(len(xs) - 1):
    for j in range(len(ys) - 1):
        if not footprint((xs[i] + xs[i + 1]) / 2, (ys[j] + ys[j + 1]) / 2):
            face([V(xs[i], ys[j], FLOOR), V(xs[i + 1], ys[j], FLOOR), V(xs[i + 1], ys[j + 1], FLOOR), V(xs[i], ys[j + 1], FLOOR)])
        face([V(xs[i], ys[j + 1], 0.0), V(xs[i + 1], ys[j + 1], 0.0), V(xs[i + 1], ys[j], 0.0), V(xs[i], ys[j], 0.0)])
for i in range(len(xs) - 1):
    for yv in (-FY, FY):
        f = [V(xs[i], yv, 0.0), V(xs[i + 1], yv, 0.0), V(xs[i + 1], yv, FLOOR), V(xs[i], yv, FLOOR)]; face(f if yv < 0 else f[::-1])
for j in range(len(ys) - 1):
    for xv in (-FX, FX):
        f = [V(xv, ys[j], 0.0), V(xv, ys[j], FLOOR), V(xv, ys[j + 1], FLOOR), V(xv, ys[j + 1], 0.0)]; face(f if xv < 0 else f[::-1])

bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
CH = 0.025                                   # floor-sheet corner chamfer (source sheet has rounded corners)
for v in bm.verts:
    if abs(abs(v.co.x) - FX) < 1e-6 and abs(abs(v.co.y) - FY) < 1e-6:
        v.co.x -= math.copysign(CH * 0.5, v.co.x); v.co.y -= math.copysign(CH * 0.5, v.co.y)
bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
me = bpy.data.meshes.new("Tent_shell"); bm.to_mesh(me); bm.free()
shell = bpy.data.objects.new("Tent_shell", me); bpy.context.scene.collection.objects.link(shell)

def tube(name, p0, axis, length, r, N, rot_axis):
    """N-segment tube from p0 along axis ('x' or 'z'), quad end caps (N = 8 or 12)"""
    b = bmesh.new(); A = []; B = []
    for k in range(N):
        c, s = r * math.cos(2 * math.pi * k / N), r * math.sin(2 * math.pi * k / N)
        if axis == 'x': pa = (p0[0], p0[1] + c, p0[2] + s); pb = (p0[0] + length, p0[1] + c, p0[2] + s)
        else: pa = (p0[0] + c, p0[1] + s, p0[2]); pb = (p0[0] + c, p0[1] + s, p0[2] + length)
        A.append(b.verts.new(pa)); B.append(b.verts.new(pb))
    for k in range(N): b.faces.new((A[k], A[(k + 1) % N], B[(k + 1) % N], B[k]))
    caps = {8: ((0, 1, 2, 3), (3, 4, 7, 0), (4, 5, 6, 7)), 12: ((0, 1, 2, 3), (3, 4, 5, 6), (6, 7, 8, 9), (9, 10, 11, 0), (0, 3, 6, 9))}[N]
    for C in (A, B):
        for q in caps: b.faces.new([C[j] for j in q])
    bmesh.ops.recalc_face_normals(b, faces=b.faces)
    m = bpy.data.meshes.new(name); b.to_mesh(m); b.free()
    o = bpy.data.objects.new(name, m); bpy.context.scene.collection.objects.link(o); return o

CLR = 0.0005
RR = 0.025
for sy in (-1, 1):
    tube("Tent_roll", (-0.2255, sy * (-G[0][0] + RR + CLR), 0.400), 'x', 0.451, RR, 12, None)
RP = 0.008
for sy in (-1, 1):
    yp = sy * (-YGF1 - RP - CLR) * -1 if False else (YGF1 + RP + CLR) * (1 if sy < 0 else -1)
    for sx in (-1, 1):
        tube("Tent_pole", (sx * (DOOR_X + RP + CLR), yp, FLOOR + CLR), 'z', DOOR_Z - FLOOR - 2 * CLR, RP, 8, None)

vl = bpy.context.view_layer
for x in bpy.context.scene.objects: x.select_set(False)
for o in bpy.data.objects:
    if o.name.startswith("Tent_"): o.select_set(True)
vl.objects.active = shell; bpy.ops.object.join()
lo = vl.objects.active; lo.name = "Tent_low"; lo.data.name = "Tent_low"
b = bmesh.new(); b.from_mesh(lo.data)
for e in b.edges:
    if len(e.link_faces) == 2 and e.calc_face_angle(0) > math.radians(35): e.smooth = False; e.seam = True
sizes = {}
for f in b.faces: sizes[len(f.verts)] = sizes.get(len(f.verts), 0) + 1
print("TENT faces", len(b.faces), "sizes", dict(sorted(sizes.items())), "tris", sum(len(f.verts) - 2 for f in b.faces),
      "open", sum(1 for e in b.edges if e.is_boundary), ">2", sum(1 for e in b.edges if len(e.link_faces) > 2))
b.to_mesh(lo.data); b.free()
bpy.ops.object.shade_smooth(keep_sharp_edges=True)
src = [o for o in bpy.data.objects if o.type == 'MESH' and o.name.startswith('tripo')][0]
src.name = "Tent_high"; src.hide_render = True
bpy.ops.wm.save_as_mainfile(filepath=OUT, copy=True)
