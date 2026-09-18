import csv
import glob
import os

from tools.shadow_replay import bootstrap_mean_ci


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

for path in sorted(glob.glob("logs/shadow_rows_*.csv")):
    rows = []
    with open(path, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if r.get("filled") != "True":
                continue
            r["net"] = num(r.get("net"))
            rows.append(r)
    nets = [r["net"] for r in rows if r["net"] is not None]
    if not nets:
        print(f"{os.path.basename(path):36s} n=0")
        continue
    lo, hi = bootstrap_mean_ci(nets)
    print(f"{os.path.basename(path):36s} n={len(nets):3d} 净={sum(nets):+8.2f} 均={sum(nets)/len(nets):+6.2f} "
          f"90%CI=[{lo:+6.2f},{hi:+6.2f}] {'PASS' if (len(nets)>=20 and lo>0) else ''}")
