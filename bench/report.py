"""Print the benchmark table from bench/out/*.json (production baseline / HiPO+presolve-off / lattice pool)."""
from __future__ import annotations

import glob, json, os, sys

def prod_rows(path, label):
    d = json.load(open(path)); out = []
    for n, r in d["ns"].items():
        e = (d.get("evaluation") or {}).get(n) or {}
        solve = d["stages"].get(f"v2_solve:{n}")
        total = round(d["stages"]["v2_prepare:None"] + sum(v for k, v in d["stages"].items() if k.endswith(f":{n}")), 1)
        mass = e.get("additional_with_anchorage_kg")
        out.append((os.path.basename(d["dxf"]), label, int(n), solve, total, mass, e.get("bars"), e.get("deficit_kg"), e.get("deficit_polygons"), f"{r['state']}/{r['status']}"))
    return out

def exp_rows(path):
    d = json.load(open(path)); e = d.get("evaluation") or {}
    return [(os.path.basename(d["dxf"]), f"lattice pool (L={d.get('lattice')}, pool {d.get('pool')})", d["k"], d.get("t_solve"), d.get("t_total"),
             e.get("additional_with_anchorage_kg"), e.get("bars"), e.get("deficit_kg"), e.get("deficit_polygons"), f"{d['status']} (own model {d.get('exp_mass_kg', 0):.0f} kg)")]

rows = []
for scene in sys.argv[1:] or ["A", "B", "C"]:
    for path in sorted(glob.glob(f"bench/out/{scene}/*.json")) + sorted(glob.glob(f"bench/out/{scene}_*.json")):
        name = os.path.basename(path)
        if "prep_" in name or "logs" in name: continue
        if name.startswith("base") or name.endswith("_base.json") or name.endswith("_base_fixed.json"): rows += prod_rows(path, "baseline" + (" (fixed)" if "fixed" in name else ""))
        elif name.startswith("hipo") or name.endswith("_hipo.json"): rows += prod_rows(path, "hipo+presolve off")
        elif "exp" in name: rows += exp_rows(path)
print(f"{'scene':30s} {'variant':34s} {'N':>3} {'solve s':>8} {'total s':>8} {'add+anch kg':>12} {'bars':>5} {'deficit kg':>10} {'def polys':>9}  status")
for r in rows:
    print(f"{r[0][:30]:30s} {r[1][:34]:34s} {r[2]:3d} {str(r[3]):>8} {str(r[4]):>8} {str(r[5]):>12} {str(r[6]):>5} {str(r[7]):>10} {str(r[8]):>9}  {r[9]}")
