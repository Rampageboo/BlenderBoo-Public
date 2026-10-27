"""Helmet low-poly geometry from the spherical ray map (system Python: numpy/scipy).
Parts: shell (outer + rim lip + inner liner, quad cap), nape pad (closed block), straps (ribbons).
Rails and the NVG mount are emitted as (theta, phi) layouts; Blender projects them onto the low shell.
Output JSON: {part: {verts:[[x,y,z]], faces:[[i..]]}, rails:[...], nvg:{...}}"""
import json, math, sys
import numpy as np
from scipy import ndimage as nd

D = np.load(sys.argv[1]); OUT = sys.argv[2]
TH, PH, F, L, NH, C = D['TH'], D['PH'], D['first'], D['last'], D['nh'], D['C']
dth, dph = TH[1] - TH[0], PH[1] - PH[0]

def mirror_rim():
    pts = [(-90, 4), (-60, 3), (-30, 5), (-15, 7), (0, 7), (15, 6), (19, 4), (23, -6), (27, -20), (31, -28), (40, -29.5), (90, -29)]
    xs, ys = zip(*pts)
    def rim(th):
        t = ((th + 90) % 360) - 90                      # -90..270
        if t > 90: t = 180 - t                          # mirror about the YZ plane
        return float(np.interp(t, xs, ys))
    return rim
RIM = mirror_rim()

def field_sample(Fd, th, ph):
    """bilinear sample with theta wrap"""
    i = (ph - PH[0]) / dph; j = (th % 360 - TH[0]) / dth
    return float(nd.map_coordinates(Fd, [[i], [j]], order=1, mode='grid-wrap' if False else 'nearest')[0]) if False else \
        float(_bilin(Fd, i, j))
def _bilin(Fd, i, j):
    i0 = int(np.floor(i)); j0 = int(np.floor(j)); di, dj = i - i0, j - j0
    n = Fd.shape[1]; i0 = min(max(i0, 0), Fd.shape[0] - 2)
    a = Fd[i0, j0 % n]; b = Fd[i0, (j0 + 1) % n]; c = Fd[i0 + 1, j0 % n]; d = Fd[i0 + 1, (j0 + 1) % n]
    return (a * (1 - dj) + b * dj) * (1 - di) + (c * (1 - dj) + d * dj) * di

def smooth(Fd, mask, sigma):
    """normalized gaussian smoothing restricted to mask, theta wraps; values outside mask filled by nearest smoothing"""
    v = np.where(mask & np.isfinite(Fd), Fd, 0.0); w = (mask & np.isfinite(Fd)).astype(float)
    sv = nd.gaussian_filter(v, sigma, mode=('nearest', 'wrap')); sw = nd.gaussian_filter(w, sigma, mode=('nearest', 'wrap'))
    out = sv / np.maximum(sw, 1e-6)
    # fill far-away cells with a wide smoothing so sampling just outside the mask stays sane
    sv2 = nd.gaussian_filter(v, sigma * 4, mode=('nearest', 'wrap')); sw2 = nd.gaussian_filter(w, sigma * 4, mode=('nearest', 'wrap'))
    out = np.where(sw > 0.05, out, sv2 / np.maximum(sw2, 1e-6))
    return out

rim_grid = np.array([[RIM(t) for t in TH] for _ in PH])
PHG = np.repeat(PH[:, None], len(TH), 1); THG = np.repeat(TH[None, :], len(PH), 0)
shell_mask = (PHG >= rim_grid - 0.5) & np.isfinite(L)
# baseline: lower envelope of the outer surface, used to strip rails/NVG/patches from the shell
Lf = np.where(shell_mask, L, np.nanmax(L))
env = nd.minimum_filter(Lf, size=(13, 13), mode=('nearest', 'wrap'))
base = smooth(env, shell_mask, 4.0)
bump = np.where(shell_mask, L - base, 0.0)
t_ = ((THG + 90) % 360) - 90; t_ = np.where(t_ > 90, 180 - t_, t_)
flare = (t_ > 48) & (PHG < -11) & (PHG > -30)                      # rear flare band belongs to the shell
t2 = THG % 360
nvg_reg = (t2 > 247) & (t2 < 293) & (PHG > 9) & (PHG < 55)
Sraw = np.where(nvg_reg, base, L)
FAIR = np.load(sys.argv[3])['fit']
S = FAIR                                                           # outer shell = fair low-order fit (no AI noise, no features)
inner_mask = shell_mask & np.isfinite(F) & (L - F > 0.008)
I = smooth(F, inner_mask, 3.5)
I = np.minimum(I, S - 0.018)                                       # keep the liner inside the shell

def dirv(th, ph):
    t, p = math.radians(th), math.radians(ph)
    return np.array([math.cos(p) * math.cos(t), math.cos(p) * math.sin(t), math.sin(p)])
def P(th, ph, fld, dr=0.0):
    return C + dirv(th, ph) * (_bilin(fld, (ph - PH[0]) / dph, (th % 360) / dth) + dr)

# columns (symmetric about the YZ plane, extra density where the rim drops behind the ears), 56 = 4 x 14
cols = [i * 5.0 for i in range(72)]
PTOP = 72.0
T_OUT = [0, 0.02, 0.05, 0.09, 0.14, 0.2, 0.26, 0.32, 0.39, 0.46, 0.53, 0.6, 0.67, 0.74, 0.81, 0.88, 0.94, 1.0]
T_IN = [0, 0.3, 0.65, 1.0]

class M:
    def __init__(s): s.v = []; s.f = []
    def add(s, p): s.v.append([float(x) for x in p]); return len(s.v) - 1
    def quad(s, q, flip=False): s.f.append(list(q[::-1]) if flip else list(q))

def cap(m, ring_ids, fld, inward):
    """14x14 quad patch filling a 56-vertex ring at phi = PTOP (ring ordered by increasing theta)"""
    N = len(cols) // 4; uv = np.linspace(-1, 1, N + 1)
    # square perimeter order starting at (1,0) going counter-clockwise
    per = []
    k0 = N // 2
    for j in range(k0, N + 1): per.append((N, j))
    for i in range(N - 1, -1, -1): per.append((i, N))
    for j in range(N - 1, -1, -1): per.append((0, j))
    for i in range(1, N + 1): per.append((i, 0))
    for j in range(1, k0): per.append((N, j))
    assert len(per) == len(cols)
    def disk(i, j):
        u, v = uv[i], uv[j]; return u * math.sqrt(1 - v * v / 2), v * math.sqrt(1 - u * u / 2)
    a_b = np.unwrap([math.atan2(*disk(i, j)[::-1]) for i, j in per]); th_b = np.unwrap(np.radians(cols))
    a_b = np.append(a_b, a_b[0] + 2 * math.pi); th_b = np.append(th_b, th_b[0] + 2 * math.pi)
    ids = {}
    for k, (i, j) in enumerate(per): ids[(i, j)] = ring_ids[k]
    for i in range(1, N):
        for j in range(1, N):
            x, y = disk(i, j); a = math.atan2(y, x)
            while a < a_b[0]: a += 2 * math.pi
            th = math.degrees(np.interp(a, a_b, th_b)); rho = min(1.0, math.hypot(x, y))
            ids[(i, j)] = m.add(P(th, 90 - (90 - PTOP) * rho, fld))
    for i in range(N):
        for j in range(N):
            q = [ids[(i, j)], ids[(i + 1, j)], ids[(i + 1, j + 1)], ids[(i, j + 1)]]
            m.quad(q, flip=inward)

def surface(m, fld, ts, inward):
    rows = []
    for t in ts:
        rows.append([m.add(P(th, RIM(th) + t * (PTOP - RIM(th)), fld)) for th in cols])
    n = len(cols)
    for a in range(len(rows) - 1):
        for k in range(n):
            q = [rows[a][k], rows[a][(k + 1) % n], rows[a + 1][(k + 1) % n], rows[a + 1][k]]
            m.quad(q, flip=inward)
    cap(m, rows[-1], fld, inward)
    return rows

shell = M()
ro = surface(shell, S, T_OUT, inward=False)
ri = surface(shell, I, T_IN, inward=True)
n = len(cols)
for k in range(n):                                                   # rim lip
    shell.quad([ri[0][k], ri[0][(k + 1) % n], ro[0][(k + 1) % n], ro[0][k]])

# nape pad: closed radial block below the back rim
PADC = [c for c in cols if 37.5 <= c <= 142.5]
def pad_bot(th):
    t = th if th <= 90 else 180 - th
    return float(np.interp(t, [37.5, 45, 52.5, 60, 67.5, 75, 82.5, 90], [-44, -48, -53, -56, -59, -60, -61, -61]))
pad_mask = (PHG < -29.5) & (PHG > -66) & np.isfinite(L) & (THG > 30) & (THG < 150)
PO = smooth(L, pad_mask, 2.0); PI_ = smooth(F, pad_mask & np.isfinite(F), 2.5); PI_ = np.minimum(PI_, PO - 0.04)
pad = M(); TOPP = -30.6
RP = [0, 0.25, 0.5, 0.75, 1.0]
po = [[pad.add(P(th, TOPP + t * (pad_bot(th) - TOPP), PO)) for th in PADC] for t in RP]
pi = [[pad.add(P(th, TOPP + t * (pad_bot(th) - TOPP), PI_)) for th in PADC] for t in RP]
m_ = len(PADC)
for a in range(len(RP) - 1):
    for k in range(m_ - 1):
        pad.quad([po[a][k], po[a + 1][k], po[a + 1][k + 1], po[a][k + 1]])
        pad.quad([pi[a][k], pi[a][k + 1], pi[a + 1][k + 1], pi[a + 1][k]])
for k in range(m_ - 1):
    pad.quad([po[0][k], po[0][k + 1], pi[0][k + 1], pi[0][k]])                # top
    pad.quad([po[-1][k], pi[-1][k], pi[-1][k + 1], po[-1][k + 1]])            # bottom
for a in range(len(RP) - 1):
    pad.quad([po[a][0], pi[a][0], pi[a + 1][0], po[a + 1][0]])                # side ends
    pad.quad([po[a][-1], po[a + 1][-1], pi[a + 1][-1], pi[a][-1]])

# straps: ribbons following the outer surface map, thickness inward from the strap's outer face
STR = smooth(L, np.isfinite(L) & (PHG < 5), 1.5)
def ribbon(path, width_deg, thick=0.008, name='', step=3.0):
    """path: [(theta, phi[, width_deg])]; evenly resampled, one smoothed radius per section, 4 verts per section"""
    pts = [(p[0], p[1], p[2] if len(p) > 2 else width_deg) for p in path]
    rs_ = []
    for (t0, p0, w0), (t1, p1, w1) in zip(pts, pts[1:]):
        n_ = max(1, int(math.hypot((t1 - t0) * math.cos(math.radians((p0 + p1) / 2)), p1 - p0) / step))
        for k in range(n_): f_ = k / n_; rs_.append((t0 + (t1 - t0) * f_, p0 + (p1 - p0) * f_, w0 + (w1 - w0) * f_))
    rs_.append(pts[-1]); pts = rs_
    rad = np.array([_bilin(STR, (ph - PH[0]) / dph, (th % 360) / dth) for th, ph, _ in pts])
    rad = np.convolve(np.pad(rad, 2, mode='edge'), np.ones(5) / 5, mode='valid')
    m = M(); secs = []
    for k, (th, ph, wd) in enumerate(pts):
        a_ = pts[max(k - 1, 0)]; b_ = pts[min(k + 1, len(pts) - 1)]
        tdir = np.array([(b_[0] - a_[0]) * math.cos(math.radians(ph)), b_[1] - a_[1]], dtype=float); tdir /= np.linalg.norm(tdir)
        nrm = np.array([-tdir[1], tdir[0]]) * wd / 2
        sides = [(th + s_ * nrm[0] / max(math.cos(math.radians(ph)), 0.3), ph + s_ * nrm[1]) for s_ in (-1, 1)]
        o0, o1 = C + dirv(*sides[0]) * rad[k], C + dirv(*sides[1]) * rad[k]
        i0, i1 = C + dirv(*sides[0]) * (rad[k] - thick), C + dirv(*sides[1]) * (rad[k] - thick)
        secs.append([m.add(o0), m.add(o1), m.add(i1), m.add(i0)])
    for A, B in zip(secs, secs[1:]):
        for e in range(4): m.quad([A[e], B[e], B[(e + 1) % 4], A[(e + 1) % 4]])
    m.quad(secs[0][::-1]); m.quad(secs[-1])
    return m

def mir(path): return [(180 - p[0],) + tuple(p[1:]) for p in path][::-1]
front_R = [(320.7, 1.6), (317, -16.9), (312.8, -34.5), (309.8, -49.2), (309.5, -53)]
loop_R = [(270, -52, 5), (284, -52.5, 5), (297, -53.5, 5), (311, -58, 5), (330, -60, 5), (354, -59.7, 5), (4, -57.5, 5),
          (13, -55, 5), (26, -51, 5), (34, -47.5, 5)]
straps = [ribbon(front_R, 5, thick=0.006), ribbon([(180 - t, p) for t, p in front_R], 5, thick=0.006)]
loop = loop_R[::-1][:-1] if False else None
full_loop = [(180 - p[0],) + tuple(p[1:]) for p in loop_R[::-1]] + loop_R[1:]
full_loop = [(t % 360, p, w) for t, p, w in full_loop]
# unwrap theta along the loop so the path is continuous (146 -> 270 -> 34+360)
ul = []; prev = None
for t, p, w in full_loop:
    if prev is not None:
        while t - prev > 180: t -= 360
        while t - prev < -180: t += 360
    ul.append((t, p, w)); prev = t
straps.append(ribbon(ul, 5, thick=0.006))

out = {'C': C.tolist()}
def dump(m): return {'verts': m.v, 'faces': m.f}
out['shell'] = dump(shell); out['pad'] = dump(pad)
out['straps'] = [dump(s) for s in straps]
# rails / NVG layouts in (theta, phi); heights from the measured bump
rail_R = [(297, 15, 7), (305, 16, 9), (315, 15, 12), (330, 16, 16), (345, 16, 17), (0, 16, 17), (10, 16, 16), (18, 15, 14),
          (24, 13, 10), (29, 8, 8), (33, 1, 7), (36, -6, 7), (39, -13, 7), (43, -20, 7), (48, -25, 6)]
def bump_at(th, ph): return _bilin(np.where(shell_mask, L - S, 0), (ph - PH[0]) / dph, (th % 360) / dth)
hs = sorted(bump_at(t, p) for t, p, w in rail_R)
out['rail_height'] = 0.025
# hard-surface sweeps on the fair shell: (theta, phi, width_deg, height); right side defined, left mirrored
RAIL_TOP = [(320, 17.5, 4, 0.006), (325, 19, 5.5, 0.016), (335, 22.5, 5, 0.026), (345, 22.5, 5, 0.025), (355, 22, 5.5, 0.025),
            (5, 21.5, 6, 0.028), (15, 21, 6, 0.031), (21, 19, 6.5, 0.028)]             # ends where the ear-wing plate starts
RAIL_LOW = [(328, 9.5, 2.5, 0.006), (335, 10, 3.5, 0.013), (350, 10.5, 3.5, 0.012), (5, 10.5, 3.5, 0.012), (15, 10.5, 3.5, 0.010), (20, 10.5, 2.5, 0.006)]
D_FRAME = [(25, 8, 2.5, 0.008), (25, 0, 3, 0.012), (26, -8, 3, 0.012), (28, -14, 3, 0.012), (30.5, -18.5, 3, 0.011), (35, -21.5, 3, 0.010), (41, -22.5, 3, 0.010), (45, -22.5, 2.5, 0.008)]
REAR = [(54, -24.5, 3.5, 0.010), (70, -24.5, 3.5, 0.010), (90, -24.5, 3.5, 0.010), (110, -24.5, 3.5, 0.010), (126, -24.5, 3.5, 0.010)]
NVG_LOOP = [(257, 15), (269.5, 15), (282, 15), (281.5, 26), (281, 37), (269.5, 37), (258, 37), (257.5, 26)]
NW = {'side': 9.0, 'tb': 6.5}
nvg = []
for k, (t, p) in enumerate(NVG_LOOP):
    side = k in (3, 7); top = k in (4, 5, 6); bot = k in (0, 1, 2)
    nvg.append((t, p, NW['side'] if side or k in (0, 2, 4, 6) and False else (NW['tb'] if (top or bot) and k in (1, 5) else 8.0),
                0.045 if side else (0.038 if top else 0.05)))
NVG_TAB = [(270, 39, 9, 0.02), (270, 45, 9, 0.022), (270, 50, 7, 0.016)]
mirror = lambda P: [((180 - t) % 360, p, w, h) for t, p, w, h in P][::-1]
out['sweeps'] = [dict(path=RAIL_TOP, closed=False), dict(path=mirror(RAIL_TOP), closed=False),
                 dict(path=RAIL_LOW, closed=False), dict(path=mirror(RAIL_LOW), closed=False),
                 dict(path=D_FRAME, closed=False), dict(path=mirror(D_FRAME), closed=False),
                 dict(path=REAR, closed=False)]
out['plates'] = json.load(open(sys.argv[4]))
out['rails'] = []
out['rail_width_deg'] = 6.0
NC = [250, 255, 260, 265, 270, 275, 280, 285, 290]; NR = [11, 15, 19, 23, 27, 31, 35, 39, 43, 47, 51]
Ls = smooth(L, shell_mask, 0.7)
nb = lambda t, p: _bilin(np.where(shell_mask, L - base, 0), (p - PH[0]) / dph, (t % 360) / dth)
cells = [[i, j] for i in range(len(NC) - 1) for j in range(len(NR) - 1)
         if np.mean([nb(NC[i] + a_ * (NC[i + 1] - NC[i]), NR[j] + b_ * (NR[j + 1] - NR[j])) for a_ in (0.25, 0.75) for b_ in (0.25, 0.75)]) > 0.012]
topr = {f"{i},{j}": _bilin(Ls, (NR[j] - PH[0]) / dph, (NC[i] % 360) / dth) for i in range(len(NC)) for j in range(len(NR))}
out['nvg'] = None
print("NVG cells", len(cells))
# fields for Blender-side sampling of rail/NVG directions
json.dump(out, open(OUT, 'w'))
tri = lambda m: sum(len(f) - 2 for f in m.f)
print("HELMET shell faces", len(shell.f), "tris", tri(shell), "pad", tri(pad), "straps", sum(tri(s) for s in straps),
      "sweeps", len(out['sweeps']))
