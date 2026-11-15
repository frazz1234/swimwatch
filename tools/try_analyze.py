"""Analyze picture files offline (no Home Assistant needed).

    python tools/try_analyze.py --zone "0.45,0.42 0.66,0.31 0.82,0.42 0.73,0.64 0.50,0.62" a.jpg b.jpg

Writes <name>_a.jpg (zone and boxes drawn) next to each picture.
"""
import argparse, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "addon" / "app"))
from swimwatch.analyze import Analyzer  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--zone", default="", help="water zone: x,y pairs from 0 to 1, separated by spaces")
ap.add_argument("--model", default="", help="detector, default PekingU/rtdetr_v2_r50vd")
ap.add_argument("--models-dir", default="models")
ap.add_argument("pictures", nargs="+")
a = ap.parse_args()
zone = [[float(v) for v in p.split(",")] for p in a.zone.split()]
md = pathlib.Path(a.models_dir); md.mkdir(exist_ok=True)
t = time.time()
an = Analyzer(md, {"model": a.model}, 4)
print("models loaded in", round(time.time() - t, 1), "s")
for f in a.pictures:
    b = pathlib.Path(f).read_bytes()
    t = time.time()
    r = an.analyze(b, zone)
    print(pathlib.Path(f).name, f"{time.time() - t:.2f}s", "clip", r["p_zs"], "prior", r["prior"],
          "in_water", r["in_water"], "near", r["persons_near"], "animals", r["animals"])
    for d in r["dets"]:
        print("   ", d["kind"], d["conf"], "overlap", d["overlap"], "in_water", d["in_water"], "near", d["near"])
    pathlib.Path(f).with_name(pathlib.Path(f).stem + "_a.jpg").write_bytes(an.annotate(b, zone, r["dets"]))
