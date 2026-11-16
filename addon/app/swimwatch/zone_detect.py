"""Finds the water of the pool in a camera picture, to pre-draw the zone in the setup page.

UperNet ConvNeXt-small trained on ADE20K (MIT license) labels each pixel. The biggest "water" /
"swimming pool" area becomes a polygon of about 12 points that the user then adjusts.
The model is only loaded for this and freed right after (it is not needed to watch the pool).
"""
from __future__ import annotations

import gc
import io
import logging
import threading
from pathlib import Path

import numpy as np
from PIL import Image

log = logging.getLogger("swimwatch.zone")
MODEL = "openmmlab/upernet-convnext-small"
_lock = threading.Lock()


def _largest_blob(mask: np.ndarray) -> np.ndarray:
    """Biggest 4-connected group of True pixels (simple flood fill, mask is small)."""
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    best: list = []
    for y0, x0 in zip(*np.nonzero(mask)):
        if seen[y0, x0]:
            continue
        stack, blob = [(y0, x0)], []
        seen[y0, x0] = True
        while stack:
            y, x = stack.pop()
            blob.append((y, x))
            for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        if len(blob) > len(best):
            best = blob
    out = np.zeros_like(mask, dtype=bool)
    if best:
        ys, xs = zip(*best)
        out[list(ys), list(xs)] = True
    return out


def _hull(points: np.ndarray) -> np.ndarray:
    """Convex hull (monotone chain). points: (n, 2) as x, y."""
    pts = sorted(set(map(tuple, points.tolist())))
    if len(pts) < 3:
        return np.array(pts, dtype=float)

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return np.array(lower[:-1] + upper[:-1], dtype=float)


def _simplify(poly: np.ndarray, n: int) -> np.ndarray:
    """Removes the points that change the shape least (Visvalingam) until n are left."""
    pts = [tuple(p) for p in poly]
    while len(pts) > n:
        areas = []
        for i in range(len(pts)):
            a, b, c = pts[i - 1], pts[i], pts[(i + 1) % len(pts)]
            areas.append(abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])))
        pts.pop(int(np.argmin(areas)))
    return np.array(pts)


def detect_zone(jpeg: bytes, models_dir: Path, points: int = 12) -> list[list[float]] | None:
    """Returns the water zone as [[x, y], ...] from 0 to 1, or None if no pool was found."""
    import torch
    from transformers import AutoImageProcessor, AutoModelForSemanticSegmentation

    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    w, h = img.size
    with _lock:
        cache = str(models_dir / "hf")
        proc = AutoImageProcessor.from_pretrained(MODEL, cache_dir=cache)
        model = AutoModelForSemanticSegmentation.from_pretrained(MODEL, cache_dir=cache).eval()
        try:
            with torch.no_grad():
                out = model(**proc(images=img, return_tensors="pt"))
            # work on a small map: fast flood fill, and enough for a zone
            sw, sh = 320, max(1, round(320 * h / w))
            seg = proc.post_process_semantic_segmentation(out, target_sizes=[(sh, sw)])[0].numpy()
            labels = model.config.id2label
        finally:
            del model
            gc.collect()
            try:   # give the memory back to the system (Linux / glibc)
                import ctypes
                ctypes.CDLL("libc.so.6").malloc_trim(0)
            except (OSError, AttributeError):
                pass
    water_ids = [i for i, name in labels.items() if any(k in name.lower() for k in ("swimming pool", "water"))]
    blob = _largest_blob(np.isin(seg, water_ids))
    if blob.sum() < 0.005 * blob.size:   # less than 0.5 % of the picture: no pool found
        return None
    ys, xs = np.nonzero(blob)
    hull = _hull(np.stack([xs, ys], axis=1))
    poly = _simplify(hull, points)
    log.info("pool found: %.1f %% of the picture, %d points", 100 * blob.mean(), len(poly))
    return [[round(float(x) / sw, 4), round(float(y) / sh, 4)] for x, y in poly]
