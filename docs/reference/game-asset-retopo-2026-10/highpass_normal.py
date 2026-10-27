"""High-pass a tangent-space normal map: keep fine detail, drop large-scale bumps (AI surface noise / form mismatch)."""
import sys, numpy as np
from PIL import Image
from scipy import ndimage as nd
src, dst_gl, dst_dx, sigma = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
im = np.asarray(Image.open(src).convert('RGB')).astype(np.float32) / 255.0
n = im * 2 - 1
mask = (np.abs(n[..., 2]) > 0.05).astype(np.float32)          # baked texels
blur = np.stack([nd.gaussian_filter(n[..., c] * mask, sigma) for c in range(3)], -1) / np.maximum(nd.gaussian_filter(mask, sigma), 1e-4)[..., None]
hp = n - blur + np.array([0, 0, 1.0])
hp /= np.linalg.norm(hp, axis=-1, keepdims=True)
hp = np.where(mask[..., None] > 0, hp, n)
out = ((hp + 1) * 0.5 * 255).clip(0, 255).astype(np.uint8)
Image.fromarray(out).save(dst_gl)
dx = out.copy(); dx[..., 1] = 255 - dx[..., 1]; Image.fromarray(dx).save(dst_dx)
print("HP done; mean |delta| removed", float(np.abs(blur[..., :2][mask > 0]).mean()))
