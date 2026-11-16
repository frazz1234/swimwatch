"""Picture analysis, all local, CPU is enough:
1. RT-DETR v2 (Apache-2.0) finds people and animals. A person whose box sits in the water zone counts as
   "in the water".
2. CLIP (MIT) turns the pool area into a vector (for learning) and gives a first guess with no training.
3. Labeled past events (k nearest neighbours on those vectors) correct the guess as labels come in.
"""
from __future__ import annotations

import io
import logging
import threading
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("swimwatch.analyze")

KINDS = {"person", "dog", "cat", "bird"}
DEFAULT_DETECTOR = "PekingU/rtdetr_v2_r50vd"   # "PekingU/rtdetr_v2_r18vd" is 2x faster, a bit less sure

SWIM_PROMPTS = [
    "a photo of people swimming in a backyard pool",
    "a person in the water of a swimming pool",
    "children playing in the water of a swimming pool",
    "the head of a person in the water of a swimming pool",
]
OTHER_PROMPTS = [
    "a photo of an empty backyard swimming pool",
    "a swimming pool with nobody in the water",
    "a covered swimming pool in winter",
    "a swimming pool with snow",
    "a person standing next to a swimming pool",
    "a dog next to a swimming pool",
    "a swimming pool at night",
]


class Analyzer:
    def __init__(self, models_dir: Path, detector: dict, threads: int = 4, light: bool = False):
        import open_clip
        import torch
        from transformers import AutoImageProcessor, AutoModelForObjectDetection

        torch.set_num_threads(threads)
        self.torch = torch
        name = detector.get("model") or DEFAULT_DETECTOR
        cache = str(models_dir / "hf")
        self.proc = AutoImageProcessor.from_pretrained(name, cache_dir=cache)
        self.det = AutoModelForObjectDetection.from_pretrained(name, cache_dir=cache).eval()
        self.id2label = self.det.config.id2label
        self.conf = float(detector.get("confidence", 0.35))
        self.light = light
        self.clip = None
        if not light:   # light mode: no CLIP (about 600 MB less); learning uses a small picture fingerprint
            self.clip, _, self.preprocess = open_clip.create_model_and_transforms(
                "ViT-B-32", pretrained="laion2b_s34b_b79k", cache_dir=str(models_dir / "clip"))
            self.clip.eval()
            tok = open_clip.get_tokenizer("ViT-B-32")
            with torch.no_grad():
                txt = self.clip.encode_text(tok(SWIM_PROMPTS + OTHER_PROMPTS)).float()
            self.text = (txt / txt.norm(dim=-1, keepdim=True)).numpy()
        self.lock = threading.Lock()
        log.info("models loaded (%s, %s)", name, "light mode, no CLIP" if light else "CLIP ViT-B-32")

    @staticmethod
    def fingerprint(crop: Image.Image) -> np.ndarray:
        """Small picture fingerprint for light mode: grey 32x18 + colour 8x4, centred and normalized,
        so the cosine between two fingerprints is how alike the two scenes look."""
        g = np.asarray(crop.convert("L").resize((32, 18)), np.float32).ravel()
        c = np.asarray(crop.convert("RGB").resize((8, 4)), np.float32).ravel()
        g -= g.mean()
        c -= c.mean()
        v = np.concatenate([g / (np.linalg.norm(g) + 1e-6), 0.5 * c / (np.linalg.norm(c) + 1e-6)])
        return (v / (np.linalg.norm(v) + 1e-6)).astype(np.float32)

    # ---------- geometry ----------
    @staticmethod
    def zone_mask(zone, w, h):
        m = Image.new("L", (w, h), 0)
        if len(zone) >= 3:
            ImageDraw.Draw(m).polygon([(x * w, y * h) for x, y in zone], fill=255)
        return np.asarray(m) > 0

    @staticmethod
    def zone_box(zone, w, h, grow=0.0):
        xs = [p[0] * w for p in zone] or [0, w]
        ys = [p[1] * h for p in zone] or [0, h]
        x1, x2, y1, y2 = min(xs), max(xs), min(ys), max(ys)
        gx, gy = (x2 - x1) * grow, (y2 - y1) * grow
        return max(0, x1 - gx), max(0, y1 - gy), min(w, x2 + gx), min(h, y2 + gy)

    def _detect(self, img: Image.Image):
        from torchvision.ops import batched_nms
        torch = self.torch
        with torch.no_grad():
            out = self.det(**self.proc(images=img, return_tensors="pt"))
        r = self.proc.post_process_object_detection(out, target_sizes=[img.size[::-1]], threshold=self.conf)[0]
        keep = [i for i, lab in enumerate(r["labels"].tolist()) if self.id2label[lab] in KINDS]
        if not keep:
            return []
        boxes, scores, labels = r["boxes"][keep], r["scores"][keep], r["labels"][keep]
        # RT-DETR can give two boxes for one thing (or "dog" and "cat" for the same dog): keep the best one.
        # People in one group, all animals in another.
        groups = torch.tensor([0 if self.id2label[x] == "person" else 1 for x in labels.tolist()])
        idx = batched_nms(boxes, scores, groups, 0.5).tolist()
        return [(boxes[i].tolist(), self.id2label[labels[i].item()], float(scores[i])) for i in idx]

    # ---------- one picture ----------
    def analyze(self, jpeg: bytes, zone: list) -> dict:
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        w, h = img.size
        mask = self.zone_mask(zone, w, h)
        near = self.zone_box(zone, w, h, grow=0.4)
        with self.lock:
            dets = []
            for box, kind, conf in self._detect(img):
                x1, y1, x2, y2 = (int(round(v)) for v in box)
                x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
                area = max(1, (x2 - x1) * (y2 - y1))
                overlap = float(mask[y1:y2, x1:x2].sum()) / area
                cx, by = (x1 + x2) // 2, min(h - 1, max(0, y2 - 2))
                bottom_in = bool(mask[by, min(w - 1, cx)])
                ccx, ccy = (x1 + x2) / 2, (y1 + y2) / 2
                is_near = overlap > 0 or (near[0] <= ccx <= near[2] and near[1] <= ccy <= near[3])
                in_water = kind == "person" and overlap >= 0.35 and bottom_in
                dets.append({"kind": kind, "conf": round(conf, 3), "box": [x1 / w, y1 / h, x2 / w, y2 / h],
                             "overlap": round(overlap, 3), "in_water": in_water, "near": bool(is_near)})
            # CLIP on the pool area (a bit wider than the water)
            cx1, cy1, cx2, cy2 = self.zone_box(zone, w, h, grow=0.15)
            crop = img.crop((int(cx1), int(cy1), int(cx2), int(cy2))) if len(zone) >= 3 else img
            if self.clip is not None:
                with self.torch.no_grad():
                    f = self.clip.encode_image(self.preprocess(crop).unsqueeze(0)).float()
                emb = (f / f.norm(dim=-1, keepdim=True)).numpy()[0]
            else:
                emb = self.fingerprint(crop)
        if self.clip is not None:
            logits = 100.0 * self.text @ emb
            probs = np.exp(logits - logits.max())
            probs /= probs.sum()
            p_zs = float(probs[: len(SWIM_PROMPTS)].sum())
        else:
            p_zs = 0.0

        in_water = sum(d["in_water"] for d in dets)
        persons_near = sum(d["kind"] == "person" and d["near"] and not d["in_water"] for d in dets)
        animals = sum(d["kind"] != "person" and d["near"] for d in dets)
        rule = 0.85 if in_water else (0.15 if persons_near else 0.03)
        prior = max(rule, 0.8 * p_zs)
        return {"size": [w, h], "dets": dets, "in_water": in_water, "persons_near": persons_near,
                "animals": animals, "p_zs": round(p_zs, 3), "prior": round(prior, 3), "emb": emb.astype(np.float32)}

    # ---------- learning ----------
    def knn(self, emb: np.ndarray, embs: np.ndarray | None, labels: list[str], k: int = 5):
        """Returns (p_swim, trust). trust is how much to believe the labels: high when a very similar
        picture was already labeled, low for a new kind of picture."""
        if embs is None or len(labels) < 3:
            return None, 0.0
        sims = embs @ emb
        idx = np.argsort(-sims)[:k]
        top = sims[idx]
        wts = np.exp((top - 1.0) / 0.05)
        p = float(sum(wt for wt, i in zip(wts, idx) if labels[i] == "swim") / wts.sum())
        lo, span = (0.85, 0.10) if self.clip is None else (0.80, 0.12)   # fingerprint vs CLIP scale
        trust = float(np.clip((top[0] - lo) / span, 0, 1)) * min(1.0, len(labels) / 10)
        return round(p, 3), round(trust, 3)

    # ---------- picture for people ----------
    @staticmethod
    def annotate(jpeg: bytes, zone: list, dets: list, kinds: dict | None = None) -> bytes:
        kinds = kinds or {}
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        w, h = img.size
        if w < 1000:  # small event pictures: bigger, so the boxes are readable on a phone
            img = img.resize((w * 2, h * 2), Image.LANCZOS)
            w, h = img.size
        d = ImageDraw.Draw(img)
        lw = max(2, w // 400)
        if len(zone) >= 3:
            d.polygon([(x * w, y * h) for x, y in zone], outline=(0, 200, 255), width=lw)
        size = max(14, w // 60)
        font = None
        for name in ("DejaVuSans.ttf", "arial.ttf"):
            try:
                font = ImageFont.truetype(name, size)
                break
            except OSError:
                pass
        font = font or ImageFont.load_default()
        for det in dets:
            x1, y1, x2, y2 = det["box"]
            col = (255, 40, 40) if det["in_water"] else (255, 160, 0) if det["kind"] == "person" else (80, 140, 255)
            d.rectangle([x1 * w, y1 * h, x2 * w, y2 * h], outline=col, width=lw)
            d.text((x1 * w + 3, max(0, y1 * h - size - 4)), f"{kinds.get(det['kind'], det['kind'])} {det['conf']:.0%}",
                   fill=col, font=font)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=85)
        return out.getvalue()
