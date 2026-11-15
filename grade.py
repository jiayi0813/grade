#!/usr/bin/env python3
"""Shot matching and look creation for DaVinci Resolve via .cube LUTs.

Works on stills exported from Resolve (display-referred, e.g. Rec.709 JPG/PNG).

  grade.py analyze shot.jpg
  grade.py match shot.jpg reference.jpg -o match.cube
  grade.py look -o look.cube --contrast 1.15 --shadows -0.02 -0.03 --highlights 0.01 0.04
  grade.py apply shot.jpg match.cube -o preview.jpg
"""
import argparse

import numpy as np
from PIL import Image

LUT_SIZE = 33
MAX_SLOPE = 3.0  # steepest (and 1/shallowest) tone curve a match may apply


# ---------- colour space: encoded sRGB/Rec.709 <-> Oklab ----------

def to_oklab(rgb):
    rgb = np.clip(rgb, 0, 1)
    lin = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    lms = lin @ np.array([[0.4122214708, 0.2119034982, 0.0883024619],
                          [0.5363325363, 0.6806995451, 0.2817188376],
                          [0.0514459929, 0.1073969566, 0.6299787005]])
    return np.cbrt(lms) @ np.array([[0.2104542553, 1.9779984951, 0.0259040371],
                                    [0.7936177850, -2.4285922050, 0.7827717662],
                                    [-0.0040720468, 0.4505937099, -0.8086757660]])


def from_oklab(lab):
    lms = lab @ np.array([[1.0, 1.0, 1.0],
                          [0.3963377774, -0.1055613458, -0.0894841775],
                          [0.2158037573, -0.0638541728, -1.2914855480]])
    lin = (lms ** 3) @ np.array([[4.0767416621, -1.2684380046, -0.0041960863],
                                 [-3.3077115913, 2.6097574011, -0.7034186147],
                                 [0.2309699292, -0.3413193965, 1.7076147010]])
    lin = np.clip(lin, 0, 1)
    return np.where(lin <= 0.0031308, lin * 12.92, 1.055 * lin ** (1 / 2.4) - 0.055)


def bands(L):
    """Smooth shadow / midtone / highlight weights, summing to 1."""
    L = np.clip(L, 0, 1)
    return np.stack([np.clip(1 - 2 * L, 0, 1),
                     1 - np.abs(2 * L - 1),
                     np.clip(2 * L - 1, 0, 1)], axis=-1)


# ---------- image and LUT I/O ----------

def load(path, max_side=None):
    img = Image.open(path).convert("RGB")
    if max_side:
        img.thumbnail((max_side, max_side))
    return np.asarray(img, dtype=np.float64) / 255


def build_lut(fn, size=LUT_SIZE):
    """Run fn over the LUT grid; rows are in .cube order (red varies fastest)."""
    g = np.linspace(0, 1, size)
    B, G, R = np.meshgrid(g, g, g, indexing="ij")
    return np.clip(fn(np.stack([R, G, B], -1).reshape(-1, 3)), 0, 1)


def as_grid(rows):
    """.cube rows -> array indexed [r, g, b] for apply_lut."""
    n = round(len(rows) ** (1 / 3))
    return rows.reshape(n, n, n, 3).transpose(2, 1, 0, 3)


def write_cube(path, fn, size=LUT_SIZE):
    with open(path, "w") as f:
        f.write(f"LUT_3D_SIZE {size}\n")
        np.savetxt(f, build_lut(fn, size), fmt="%.6f")
    print(f"wrote {path}")


def read_cube(path):
    rows = []
    for line in open(path):
        parts = line.split()
        if parts and parts[0][0] in "-.0123456789":
            rows.append([float(p) for p in parts])
    return as_grid(np.array(rows))


def apply_lut(rgb, lut):
    n = lut.shape[0]
    x = np.clip(rgb, 0, 1) * (n - 1)
    i = np.clip(np.floor(x).astype(int), 0, n - 2)
    f = x - i
    r, g, b = i[..., 0], i[..., 1], i[..., 2]
    fr, fg, fb = f[..., 0:1], f[..., 1:2], f[..., 2:3]
    c00 = lut[r, g, b] * (1 - fr) + lut[r + 1, g, b] * fr
    c10 = lut[r, g + 1, b] * (1 - fr) + lut[r + 1, g + 1, b] * fr
    c01 = lut[r, g, b + 1] * (1 - fr) + lut[r + 1, g, b + 1] * fr
    c11 = lut[r, g + 1, b + 1] * (1 - fr) + lut[r + 1, g + 1, b + 1] * fr
    return (c00 * (1 - fg) + c10 * fg) * (1 - fb) + (c01 * (1 - fg) + c11 * fg) * fb


# ---------- statistics ----------

PCTS = np.linspace(0, 100, 101)


def stats(rgb):
    lab = to_oklab(rgb.reshape(-1, 3))
    L, ab = lab[:, 0], lab[:, 1:]
    w = bands(L)
    prior = 0.01 * len(L)  # bands with few pixels fall back to the global mean
    band_ab = (w.T @ ab + prior * ab.mean(0)) / (w.sum(0)[:, None] + prior)
    return {"L": np.percentile(L, PCTS), "band_ab": band_ab,
            "chroma": np.hypot(ab[:, 0], ab[:, 1]).mean()}


# ---------- commands ----------

def match_fn(src, ref, tone=1.0, colour=1.0):
    """Transform that moves an image with stats `src` towards stats `ref`.

    `tone` and `colour` (0-1) set how far brightness/contrast and hue/saturation move.
    """
    scale = np.clip(ref["chroma"] / max(src["chroma"], 1e-6), 0.5, 2.0)
    # Tone curve from matched percentiles, with its slope capped so it cannot
    # stretch a narrow tonal range enough to exaggerate noise or banding.
    dx = np.diff(src["L"])
    slope = np.clip(np.diff(ref["L"]) / np.maximum(dx, 1e-6), 1 / MAX_SLOPE, MAX_SLOPE)
    curve = np.concatenate([[0], np.cumsum(slope * dx)])
    curve += ref["L"][50] - curve[50]  # keep the medians aligned

    def fn(rgb):
        lab = to_oklab(rgb)
        L = lab[:, 0]
        L2 = np.interp(L, src["L"], curve)
        ab = lab[:, 1:]
        ab2 = (ab - bands(L) @ src["band_ab"]) * scale + bands(L2) @ ref["band_ab"]
        return from_oklab(np.column_stack([L + tone * (L2 - L), ab + colour * (ab2 - ab)]))

    return fn


def look_fn(exposure=0.0, contrast=1.0, fade=0.0, saturation=1.0, temp=0.0, tint=0.0,
            shadows=(0, 0), highlights=(0, 0)):
    def fn(rgb):
        lab = to_oklab(rgb) * 2 ** (exposure / 3)  # Oklab is cube-root of linear
        L = 0.5 + (lab[:, 0] - 0.5) * contrast
        L = fade + L * (1 - fade)
        w = bands(L)
        ab = lab[:, 1:] * saturation
        ab = ab + np.clip(L, 0, 1)[:, None] * np.array([tint, temp])
        ab = ab + w[:, 0:1] * np.array(shadows) + w[:, 2:3] * np.array(highlights)
        return from_oklab(np.column_stack([L, ab]))

    return fn


def cmd_analyze(a):
    s = stats(load(a.image, 512))
    p = s["L"]
    print(f"lightness  p1={p[1]:.3f} p5={p[5]:.3f} p25={p[25]:.3f} p50={p[50]:.3f} "
          f"p75={p[75]:.3f} p95={p[95]:.3f} p99={p[99]:.3f}")
    print(f"mean chroma {s['chroma']:.4f}")
    print("colour cast (Oklab a: green- / magenta+, b: blue- / yellow+)")
    for name, (ca, cb) in zip(["shadows", "midtones", "highlights"], s["band_ab"]):
        print(f"  {name:10s} a={ca:+.4f} b={cb:+.4f}")


def cmd_match(a):
    src, ref = stats(load(a.source, 512)), stats(load(a.reference, 512))
    write_cube(a.output, match_fn(src, ref, a.strength, a.strength))


def cmd_look(a):
    write_cube(a.output, look_fn(a.exposure, a.contrast, a.fade, a.saturation, a.temp, a.tint,
                                 a.shadows, a.highlights))


def cmd_apply(a):
    out = apply_lut(load(a.image), read_cube(a.lut))
    Image.fromarray((out * 255 + 0.5).astype(np.uint8)).save(a.output)
    print(f"wrote {a.output}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("analyze", help="print tone and colour-cast statistics")
    s.add_argument("image")
    s.set_defaults(func=cmd_analyze)

    s = sub.add_parser("match", help="LUT that makes SOURCE look like REFERENCE")
    s.add_argument("source")
    s.add_argument("reference")
    s.add_argument("-o", "--output", required=True)
    s.add_argument("--strength", type=float, default=1.0, help="0 = no change, 1 = full match")
    s.set_defaults(func=cmd_match)

    s = sub.add_parser("look", help="LUT from look parameters")
    s.add_argument("-o", "--output", required=True)
    s.add_argument("--exposure", type=float, default=0.0, help="stops")
    s.add_argument("--contrast", type=float, default=1.0)
    s.add_argument("--fade", type=float, default=0.0, help="lift blacks, 0-0.2")
    s.add_argument("--saturation", type=float, default=1.0)
    s.add_argument("--temp", type=float, default=0.0, help="+ warmer / - cooler, about +-0.05")
    s.add_argument("--tint", type=float, default=0.0, help="+ magenta / - green, about +-0.05")
    s.add_argument("--shadows", type=float, nargs=2, default=[0, 0], metavar=("A", "B"),
                   help="Oklab a b tint for shadows, about +-0.05")
    s.add_argument("--highlights", type=float, nargs=2, default=[0, 0], metavar=("A", "B"),
                   help="Oklab a b tint for highlights")
    s.set_defaults(func=cmd_look)

    s = sub.add_parser("apply", help="preview a LUT on a still")
    s.add_argument("image")
    s.add_argument("lut")
    s.add_argument("-o", "--output", required=True)
    s.set_defaults(func=cmd_apply)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
