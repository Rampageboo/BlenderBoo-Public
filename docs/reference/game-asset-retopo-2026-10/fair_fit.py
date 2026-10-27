"""Fair dome: robust low-order fit of the outer radius r(theta, phi) with x-mirror symmetry.
Basis: cos(m*theta) [mirror about the YZ plane means r(theta) = r(180 - theta) -> use cos(m*(theta-90))] x Legendre-ish in sin(phi)."""
import numpy as np, sys, json
D = np.load(sys.argv[1]); OUT = sys.argv[2]
TH, PH, L = D['TH'], D['PH'], D['last']
def rimf(th):
    pts = [(-90, 4), (-60, 3), (-30, 5), (-15, 7), (0, 7), (15, 6), (19, 4), (23, -6), (27, -20), (31, -28), (40, -29.5), (90, -29)]
    xs, ys = zip(*pts); t = ((th + 90) % 360) - 90; t = np.where(t > 90, 180 - t, t); return np.interp(t, xs, ys)
THG, PHG = np.meshgrid(TH, PH)
valid = np.isfinite(L) & (PHG >= rimf(THG) + 1)
a = np.radians(THG - 90); s = np.sin(np.radians(PHG))
M, K = 8, 7
def basis(a, s):
    cols = []
    for m in range(M + 1):
        for k in range(K + 1):
            cols.append(np.cos(m * a) * s ** k)
    return np.stack(cols, -1)
B = basis(a, s)
w = valid.copy()
for it in range(8):
    coef, *_ = np.linalg.lstsq(B[w], L[w], rcond=None)
    fit = B @ coef
    res = L - fit
    sd = np.nanstd(res[w])
    w = valid & (res < 1.5 * sd) & (res > -2.5 * sd)          # features stick out: reject positive outliers
    print("iter", it, "kept", int(w.sum()), "sd", round(float(sd), 5))
fit = B @ coef
res = np.where(valid, L - fit, np.nan)
np.savez(OUT, TH=TH, PH=PH, fit=fit, res=res, coef=coef, M=M, K=K)
print("FAIR residual on kept: mean %.5f sd %.5f ; dome noise p95 |res| %.4f" % (np.mean(res[w]), np.std(res[w]), np.percentile(np.abs(res[w]), 95)))
