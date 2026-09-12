"""Exact selector (CP-SAT) over a candidate pool — the referee for agglo.

The model is always the same weighted set-cover with a zone budget:

    min  Σ cost(c)·y(c)      over candidate boxes c
    s.t. every deficit cell lies inside ≥1 selected box
         Σ y(c) ≤ K

Coverage is pure 0/1 incidence: a zone's option is the worst requirement
anywhere under its own rectangle (per-zone acceptability), so "cell inside box"
already implies "box strong enough for that cell" — no sufficiency cross-terms.

What changes between experiments is the *pool*:

  --mode pool   every box agglo ever constructed on this file (greedy merges,
                beam children, tightened repair/exchange/refill variants),
                harvested via agglo.harvest_start().  ILP mass vs agglo mass =
                **selection loss**: what the sequential search left on the table
                among candidates it had already generated.

  --mode canon  an agglo-independent enumeration: boxes whose edges lie on a
                lattice of `--lattice` cells, each tightened to the bbox of the
                deficit cells inside it (tightening never loses coverage and
                never costs more, so the pool is canonical for that lattice).
                Gap vs `pool` = **generation loss**: mass sitting in candidates
                agglo never proposed.  At lattice=1 the pool provably contains a
                global optimum, but that is ~1e8 boxes here — the lattice is the
                tractability dial, and refining it tightens the referee.

  --mode both   union of the two (+ `--baseline-dir` adds the colleagues' core
                rectangles) — the cross-approach supersolution.

Every reported layout is a real layout: verified with Engine.verify_cover and
written as {stem}.zones_K{K}.csv in agglo's format, so experiments/compare.py
and experiments/overlap_audit.py read it unchanged.

Run:  poetry run python experiments/ilp.py "examples/*.dxf" --mode canon -o out_ilp
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments"))

from a101_reinforcement.catalog import map_bands, resolve_background  # noqa: E402
from a101_reinforcement.dxf_io import read_mosaic  # noqa: E402
from a101_reinforcement.grid import build_grid, denoise_bands  # noqa: E402

import agglo  # noqa: E402
from agglo import Engine  # noqa: E402

# K and reference masses of the equal-K head-to-head (2026-08-04):
# baseline = colleagues' pipeline at --gap-mm 0, agglo = v9.
TABLE = {
    "Верхнее армирование вдоль ОСИ У": (37, 5453, 5026),
    "Верхнее армирование вдоль ОСИ Х": (14, 8311, 8567),
    "Верхняя по У": (40, 4199, 3993),
    "Верхняя по Х": (30, 8436, 8648),
    "Нижнее армирование вдоль ОСИ У": (30, 2584, 2446),
    "Нижнее армирование вдоль ОСИ Х": (36, 3880, 3690),
    "С1_t_800_Верхняя по оси У": (24, 3359, 3387),
    "С1_t_800_Верхняя по оси Х": (20, 4768, 4970),
    "С1_t_800_Нижняя по оси У": (34, 3288, 3127),
    "С1_t_800_Нижняя по оси Х": (51, 6489, 6827),
    "С2_t_700_Верхняя по оси У": (33, 3989, 3915),
    "С2_t_700_Верхняя по оси Х": (40, 4772, 5113),
    "С2_t_700_Нижняя по оси У": (39, 3402, 3444),
    "С2_t_700_Нижняя по оси Х": (53, 4214, 4782),
}


# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #
def load_engine(path: Path, args) -> Engine:
    mosaic = read_mosaic(path)
    scale = sorted(mosaic.scale, key=lambda b: b[1])
    mapping, _ = map_bands(mosaic.scale, resolve_background(scale[0][2]))
    bands = denoise_bands(mosaic, args.isolated_fe)
    grid = build_grid(mosaic, bands, mapping, cell_mm=args.cell)
    return Engine(grid, min_width_fe=args.min_width_fe,
                  anch_d=getattr(args, "anch_d", agglo.ANCH_D))


# --------------------------------------------------------------------------- #
# Pool 1 — everything agglo constructs
# --------------------------------------------------------------------------- #
def agglo_pool(path: Path, k: int, args) -> tuple[np.ndarray, np.ndarray | None, float]:
    """Run agglo with the harvest sink on.  Returns (pool, layout@K, seconds).

    The returned layout is *this run's* agglo answer — the honest reference for
    selection loss, since polish is time-budgeted and scatters a couple of
    percent between runs.
    """
    a = agglo.build_parser().parse_args([str(path), "--zones", str(k)])
    a.cell, a.isolated_fe, a.min_width_fe = args.cell, args.isolated_fe, args.min_width_fe
    a.polish_time = args.polish_time
    a.anch_d = getattr(args, "anch_d", agglo.ANCH_D)
    t0 = time.perf_counter()
    agglo.harvest_start(all_costed=args.harvest_all)
    try:
        res = agglo.solve_file(path, a)
    finally:
        pool = agglo.harvest_stop()
    dt = time.perf_counter() - t0
    boxes = None
    if res and res.get("report"):
        rep = res["report"].get(k) or res["report"][min(res["report"])]
        boxes = np.asarray(rep["boxes"], np.int64)
    return pool, boxes, dt


# --------------------------------------------------------------------------- #
# Pool 2 — canonical enumeration on a lattice
# --------------------------------------------------------------------------- #
def canon_pool(eng: Engine, lattice: int, cap: int) -> np.ndarray:
    """Tightened boxes whose (pre-tightening) edges lie on a `lattice`-cell grid.

    A zone may always shrink to the bbox of the deficit cells it covers, so
    tightening loses nothing; and because tightening only strips *empty* rows and
    columns, a deficit cell is inside the tightened box iff it was inside the
    lattice box — the covered cell sets are exactly the lattice-box ones.
    """
    mask = eng.need > 0
    nv, nu = mask.shape
    rows = sorted({*range(0, nv, lattice), nv})
    cols = sorted({*range(0, nu, lattice), nu})
    # column-cumulative (over rows) and row-cumulative (over columns)
    cum_j = np.zeros((nv + 1, nu), np.int64)
    cum_j[1:] = np.cumsum(mask, 0)
    cum_i = np.zeros((nv, nu + 1), np.int64)
    cum_i[:, 1:] = np.cumsum(mask, 1)

    out = []
    total = 0
    for a in range(len(rows) - 1):
        for b in range(a, len(rows) - 1):
            j0, j1 = rows[a], rows[b + 1] - 1
            colcnt = cum_j[j1 + 1] - cum_j[j0]          # deficit per column
            occ = np.flatnonzero(colcnt)
            if len(occ) == 0:
                continue
            # tighten in u: left edge snaps to the first occupied column at or
            # after the lattice line, right edge to the last one at or before
            nxt = np.searchsorted(occ, cols[:-1], "left")
            prv = np.searchsorted(occ, [c - 1 for c in cols[1:]], "right") - 1
            xs, ys = np.triu_indices(len(cols) - 1, 0)
            lo, hi = nxt[xs], prv[ys]
            ok = (lo < len(occ)) & (hi >= 0) & (lo <= hi)
            if not ok.any():
                continue
            i0s, i1s = occ[lo[ok]], occ[hi[ok]]
            # tighten in v: first/last row of the band with a cell in [i0, i1]
            sub = cum_i[j0:j1 + 1]
            cnt = sub[:, i1s + 1] - sub[:, i0s]          # (rows, cands)
            hit = cnt > 0
            jj0 = j0 + np.argmax(hit, 0)
            jj1 = j1 - np.argmax(hit[::-1], 0)
            cand = np.stack([i0s, i1s, jj0, jj1], 1)
            out.append(np.unique(cand, axis=0))
            total += len(out[-1])
            if total > cap * 4:                          # dedup keeps it honest
                out = [np.unique(np.concatenate(out), axis=0)]
                total = len(out[0])
    if not out:
        return np.empty((0, 4), np.int64)
    return np.unique(np.concatenate(out), axis=0)


def canon_pool_auto(eng: Engine, lattice: int, cap: int) -> tuple[np.ndarray, int]:
    """Finest lattice whose pool still fits the cap (`lattice`>0 forces one)."""
    if lattice:
        return canon_pool(eng, lattice, cap), lattice
    probe = canon_pool(eng, 10, cap)                 # pool size scales ~ L^-4
    if len(probe) > cap:
        L = 10
        while len(probe) > cap and L < 25:
            L += 1
            probe = canon_pool(eng, L, cap)
        return probe, L
    L = max(2, int(10 * (len(probe) / max(cap, 1)) ** 0.25))
    best, bestL = probe, 10
    while L < 10:
        p = canon_pool(eng, L, cap)
        if len(p) <= cap:
            return p, L
        L += 1
    return best, bestL


def baseline_pool(eng: Engine, result_json: Path) -> np.ndarray:
    """Core rectangles of the colleagues' layout, rasterised and tightened."""
    g = eng.grid
    data = json.loads(result_json.read_text("utf-8"))
    boxes = []
    for z in data["zones"]:
        if g.direction == "X":
            u0, u1, v0, v1 = z["core_x0_mm"], z["core_x1_mm"], z["core_y0_mm"], z["core_y1_mm"]
        else:
            u0, u1, v0, v1 = z["core_y0_mm"], z["core_y1_mm"], z["core_x0_mm"], z["core_x1_mm"]
        nv, nu = eng.need.shape
        i0 = max(0, int(np.ceil((u0 - g.origin_u) / g.cell_mm - 0.5)))
        i1 = min(nu - 1, int(np.floor((u1 - g.origin_u) / g.cell_mm - 0.5)))
        j0 = max(0, int(np.ceil((v0 - g.origin_v) / g.cell_mm - 0.5)))
        j1 = min(nv - 1, int(np.floor((v1 - g.origin_v) / g.cell_mm - 0.5)))
        if i0 > i1 or j0 > j1:
            continue
        t = agglo.tighten(eng, i0, i1, j0, j1)
        if t:
            boxes.append(t)
    return np.unique(np.array(boxes, np.int64), axis=0) if boxes else np.empty((0, 4), np.int64)


# --------------------------------------------------------------------------- #
# The model
# --------------------------------------------------------------------------- #
def rows_of(eng: Engine, cand: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Deficit cells deduped by coverage signature -> (rep_j, rep_i).

    Cells covered by exactly the same candidates give identical constraints;
    one representative each is enough.  Signature = XOR of the covering
    candidates' 64-bit hashes, accumulated by rectangle writes.
    """
    sig = np.zeros(eng.need.shape, np.uint64)
    h = np.random.default_rng(12345).integers(1, 2**63, len(cand), dtype=np.uint64)
    for (i0, i1, j0, j1), hv in zip(cand, h):
        sig[j0:j1 + 1, i0:i1 + 1] ^= hv
    mask = eng.need > 0
    js, is_ = np.nonzero(mask)
    _, idx = np.unique(sig[js, is_], return_index=True)
    return js[idx], is_[idx]


def greedy_cover(eng: Engine, cand: np.ndarray, rj: np.ndarray, ri: np.ndarray,
                 cost: np.ndarray, k: int) -> list[int] | None:
    """Warm start: greedy cheapest-per-newly-covered-cell set cover.

    Newly covered cells per candidate is an integral-image query on the
    still-uncovered indicator, so a round costs one prefix sum + one vector pass
    over the pool however large it is."""
    uncov = np.zeros(eng.need.shape, np.int64)
    uncov[rj, ri] = 1
    chosen: list[int] = []
    while uncov.any() and len(chosen) < k:
        c = np.zeros((uncov.shape[0] + 1, uncov.shape[1] + 1), np.int64)
        c[1:, 1:] = np.cumsum(np.cumsum(uncov, 0), 1)
        i0, i1, j0, j1 = (cand[:, t] for t in range(4))
        new = c[j1 + 1, i1 + 1] - c[j0, i1 + 1] - c[j1 + 1, i0] + c[j0, i0]
        score = np.where(new > 0, cost / np.maximum(new, 1), np.inf)
        b = int(np.argmin(score))
        if not np.isfinite(score[b]):
            return None
        chosen.append(b)
        uncov[cand[b, 2]:cand[b, 3] + 1, cand[b, 0]:cand[b, 1] + 1] = 0
    return chosen if not uncov.any() else None


def solve_mip(eng, cand, cost, cover, k, args, hint_idx, tl, t_build):
    """Same model through a branch-and-cut MIP solver (HiGHS / SCIP / CBC)."""
    from ortools.linear_solver import pywraplp

    s = pywraplp.Solver.CreateSolver(args.solver.upper())
    y = [s.BoolVar(f"y{c}") for c in range(len(cand))]
    obj = s.Objective()
    for c in range(len(cand)):
        obj.SetCoefficient(y[c], float(cost[c]))
    obj.SetMinimization()
    card = s.Constraint(-s.infinity(), float(k))
    for v in y:
        card.SetCoefficient(v, 1.0)
    for cs in cover:
        con = s.Constraint(1.0, s.infinity())
        for c in cs:
            con.SetCoefficient(y[int(c)], 1.0)
    if hint_idx is not None and args.solver == "scip":  # HiGHS segfaults on hints
        s.SetHint([y[c] for c in hint_idx], [1.0] * len(hint_idx))
    s.SetTimeLimit(int(tl * 1000))
    if args.solver == "highs":
        # SetTimeLimit alone is not honoured inside HiGHS' presolve/root on the
        # largest models; its own options are.  A relative gap turns "hours to
        # close the last percent" into "seconds, with the gap reported".
        s.SetSolverSpecificParametersAsString(
            f"time_limit = {tl}\nmip_rel_gap = {args.mip_gap}\n"
            f"threads = {args.workers}\noutput_flag = false\n")
    st = s.Solve()
    ok = st in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE)
    if not ok:
        # HiGHS can run out of time with no incumbent (its wrapper segfaults on
        # SetHint, so it starts cold).  The warm start we could not give it is
        # still a feasible layout of this very pool — report that, with whatever
        # bound the run did establish.
        if hint_idx is not None:
            hb = cand[np.array(hint_idx, np.int64)]
            return dict(status="HINT-KEPT", boxes=hb,
                        mass=float(cost[np.array(hint_idx, np.int64)].sum()),
                        bound=max(0.0, obj.BestBound()), rows=len(cover),
                        cols=len(cand), t_build=t_build,
                        t_solve=s.WallTime() / 1e3, hinted=len(hint_idx))
        return dict(status="NO-SOLUTION", boxes=None, rows=len(cover),
                    cols=len(cand), t_build=t_build, t_solve=s.WallTime() / 1e3,
                    hinted=0)
    sel = np.array([c for c in range(len(cand)) if y[c].solution_value() > 0.5], np.int64)
    mass, bound = float(cost[sel].sum()), obj.BestBound()
    # pywraplp reports OPTIMAL when HiGHS stops at --mip-gap; call that what it is
    name = ("OPTIMAL" if st == pywraplp.Solver.OPTIMAL and mass - bound <= 1e-4 * mass
            else "GAP" if st == pywraplp.Solver.OPTIMAL else "FEASIBLE")
    return dict(status=name, boxes=cand[sel], mass=mass, bound=bound,
                rows=len(cover), cols=len(cand), t_build=t_build,
                t_solve=s.WallTime() / 1e3, hinted=0)


def solve(eng: Engine, cand: np.ndarray, k: int, args, hint: np.ndarray | None,
          time_limit: float | None = None):
    from ortools.sat.python import cp_model

    t0 = time.perf_counter()
    cost, _ = eng.cost(*(cand[:, c] for c in range(4)))
    cost = np.asarray(cost, float)

    # a zone costing more than a whole known layout can never be in an optimal
    # one (all costs are ≥ 0), and every box of that layout survives the cut —
    # so this only ever removes candidates, never solutions
    if hint is not None and len(hint):
        ub = float(eng.cost(*(hint[:, c] for c in range(4)))[0].sum())
        keep = np.flatnonzero(cost <= ub + 1e-6)
        if 0 < len(keep) < len(cand):
            cand, cost = cand[keep], cost[keep]

    rj, ri = rows_of(eng, cand)
    cover = []
    for j, i in zip(rj, ri):
        cs = np.flatnonzero((cand[:, 0] <= i) & (i <= cand[:, 1])
                            & (cand[:, 2] <= j) & (j <= cand[:, 3]))
        if len(cs) == 0:
            raise RuntimeError(f"cell ({j},{i}) is inside no candidate — pool broken")
        cover.append(cs)
    t_build = time.perf_counter() - t0
    tl = float(args.time_limit if time_limit is None else time_limit)

    hint_idx = None
    if hint is not None and len(hint):
        key = {tuple(int(x) for x in b): c for c, b in enumerate(cand)}
        got = [key[t] for b in hint if (t := tuple(int(x) for x in b)) in key]
        if len(got) == len(hint):
            hint_idx = got
    # HiGHS is far better on these set-cover models, but pywraplp feeds it one
    # coefficient per Python call — on a dense model (few zones ⇒ huge boxes ⇒
    # every candidate covers thousands of cells) the build alone runs for tens of
    # minutes.  CP-SAT takes its literals as lists and builds in seconds.
    nnz = sum(len(c) for c in cover)
    if args.solver != "cpsat" and nnz <= args.max_nnz:
        return solve_mip(eng, cand, cost, cover, k, args, hint_idx, tl, t_build)
    if args.solver != "cpsat":
        print(f"    [solver] {nnz / 1e6:.0f}M nonzeros > cap — falling back to CP-SAT",
              flush=True)

    m = cp_model.CpModel()
    y = [m.NewBoolVar(f"y{c}") for c in range(len(cand))]
    for cs in cover:
        m.AddBoolOr([y[int(c)] for c in cs])
    m.Add(sum(y) <= k)
    m.Minimize(sum(int(round(cost[c] * 1000)) * y[c] for c in range(len(cand))))

    hinted = 0
    if hint_idx is not None:
        on = set(hint_idx)
        for c in range(len(cand)):
            m.AddHint(y[c], 1 if c in on else 0)
        hinted = len(hint_idx)
    if not hinted:
        g = greedy_cover(eng, cand, rj, ri, cost, k)
        if g:
            on = set(g)
            for c in range(len(cand)):
                m.AddHint(y[c], 1 if c in on else 0)
            hinted = -len(g)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(
        args.time_limit if time_limit is None else time_limit)
    solver.parameters.num_workers = args.workers
    solver.parameters.log_search_progress = args.verbose
    status = solver.Solve(m)
    name = solver.StatusName(status)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return dict(status=name, boxes=None, rows=len(cover), cols=len(cand),
                    t_build=t_build, t_solve=solver.WallTime(), hinted=hinted)
    sel = np.array([c for c in range(len(cand)) if solver.Value(y[c])], np.int64)
    boxes = cand[sel]
    return dict(status=name, boxes=boxes, mass=float(cost[sel].sum()),
                bound=solver.BestObjectiveBound() / 1000.0, rows=len(cover),
                cols=len(cand), t_build=t_build, t_solve=solver.WallTime(),
                hinted=hinted)


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def write_zones(eng: Engine, boxes: np.ndarray, out: Path, stem: str, k: int):
    out.mkdir(parents=True, exist_ok=True)
    rows = eng.zone_rows(boxes)
    with open(out / f"{stem}.zones_K{k}.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("patterns", nargs="*", default=["examples/*.dxf"])
    ap.add_argument("-o", "--out", default="out_ilp")
    ap.add_argument("--mode", choices=("pool", "canon", "both"), default="canon")
    ap.add_argument("--lattice", type=int, default=0,
                    help="canonical edge lattice in cells (0 = finest that fits --cap)")
    ap.add_argument("--cap", type=int, default=150_000, help="canon pool size cap")
    ap.add_argument("--baseline-dir", default="", help="add core rects from result.json")
    ap.add_argument("--k", type=int, default=0, help="override K (default: the table)")
    ap.add_argument("--k-sweep", type=lambda s: [int(x) for x in s.split(",") if x],
                    default=[], help="solve the same pool at several K -> proven curve")
    ap.add_argument("--time-limit", type=float, default=60.0)
    ap.add_argument("--solver", choices=("cpsat", "highs", "scip", "cbc"),
                    default="cpsat")
    ap.add_argument("--mip-gap", type=float, default=0.002,
                    help="stop the MIP at this relative gap (highs)")
    ap.add_argument("--max-nnz", type=int, default=20_000_000,
                    help="above this model density, use CP-SAT (pywraplp builds too slowly)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--harvest-all", action="store_true",
                    help="pool mode: also take boxes agglo merely priced")
    ap.add_argument("--polish-time", type=float, default=20.0)
    ap.add_argument("--cell", type=float, default=100.0)
    ap.add_argument("--min-width-fe", type=int, default=2)
    ap.add_argument("--isolated-fe", type=int, default=1)
    ap.add_argument("--anch-d", type=float, default=agglo.ANCH_D,
                    help="anchorage in bar diameters per side (0 = none)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    files = sorted({Path(p) for pat in args.patterns for p in glob.glob(pat)})
    files = [f for f in files if f.suffix.lower() == ".dxf"]
    if not files:
        sys.exit("no dxf files matched")
    out = Path(args.out)

    print(f"{'file':<38} {'K':>3} {'pool':>7} {'rows':>5} | {'ILP kg':>8} {'bound':>8} "
          f"{'gap':>6} {'status':>9} | {'agglo':>6} {'now':>6} {'Δnow':>7} | "
          f"{'base':>6} {'Δbase':>7} {'t,s':>6}")
    tot = {"ilp": 0.0, "base": 0.0, "agglo": 0.0, "now": 0.0}
    for path in files:
        stem = path.stem
        k, base_kg, agglo_kg = TABLE.get(stem, (0, float("nan"), float("nan")))
        k = args.k or k
        if not k:
            print(f"{stem:<38}  no K — skipped")
            continue
        t0 = time.perf_counter()
        eng = load_engine(path, args)
        if not (eng.need > 0).any():
            continue

        pools, hint, now = [], None, float("nan")
        if args.mode in ("pool", "both"):
            p, hint, t_ag = agglo_pool(path, k, args)
            pools.append(p)
            if hint is not None:
                now = float(eng.cost(*(hint[:, c] for c in range(4)))[0].sum())
                write_zones(eng, hint, out.with_name(out.name + "_agglo"), stem, len(hint))
            print(f"    [agglo] {len(p)} boxes harvested in {t_ag:.0f}s, "
                  f"layout {now:.0f} kg at K={0 if hint is None else len(hint)}", flush=True)
        if args.mode in ("canon", "both"):
            p, L = canon_pool_auto(eng, args.lattice, args.cap)
            pools.append(p)
            print(f"    [canon] lattice={L} ({L * args.cell:.0f} mm): {len(p)} boxes",
                  flush=True)
            if args.mode == "canon" and hint is None:
                # nested lattices: a coarse optimum is itself a candidate of every
                # finer pool (its edges are a subset of the finer lattice lines),
                # so each level warm-starts the next exactly
                for coarse in (4 * L, 2 * L):
                    cp = canon_pool(eng, coarse, args.cap)
                    rr = solve(eng, cp, k, args, hint,
                               time_limit=args.time_limit * 0.25)
                    if rr["boxes"] is not None:
                        hint = rr["boxes"]
                        print(f"    [canon] lattice={coarse}: {len(cp)} boxes -> "
                              f"{rr['mass']:.0f} kg  {rr['status']}", flush=True)
        if args.baseline_dir:
            bp = baseline_pool(eng, Path(args.baseline_dir) / f"{stem}.result.json")
            pools.append(bp)
            print(f"    [base ] {len(bp)} core rects", flush=True)
        cand = np.unique(np.concatenate([p for p in pools if len(p)]), axis=0)
        if hint is not None:
            # the warm start must be selectable
            cand = np.unique(np.concatenate([cand, hint]), axis=0)

        if args.k_sweep:
            # proven mass↔K curve over one fixed pool.  Ascending K, each solve
            # warm-started from the previous one — a layout with K' ≤ K zones is
            # feasible at K, so the hint is always valid.
            out.mkdir(parents=True, exist_ok=True)
            prev = hint
            with open(out / f"{stem}.kcurve.csv", "w", encoding="utf-8") as f:
                f.write("zones,mass_kg,bound_kg,status,seconds\n")
                for kk in sorted(args.k_sweep):
                    rr = solve(eng, cand, kk, args, prev)
                    if rr["boxes"] is None:
                        print(f"    K={kk:<4d} {rr['status']}", flush=True)
                        continue
                    prev = rr["boxes"]
                    f.write(f"{len(rr['boxes'])},{rr['mass']:.1f},{rr['bound']:.1f},"
                            f"{rr['status']},{rr['t_solve']:.1f}\n")
                    print(f"    K={kk:<4d} -> {rr['mass']:>8.0f} kg  {rr['status']:>9}"
                          f"  ({rr['t_solve']:.0f}s)", flush=True)
            continue

        r = solve(eng, cand, k, args, hint)
        dt = time.perf_counter() - t0
        if r["boxes"] is None:
            print(f"{stem:<38} {k:>3} {len(cand):>7} {r['rows']:>5} | "
                  f"{'—':>8} {'':>8} {'':>6} {r['status']:>9} | {dt:>6.0f}")
            continue
        boxes = r["boxes"]
        ok = eng.verify_cover(boxes)
        mass = r["mass"]
        gap = (mass - r["bound"]) / mass * 100.0 if mass else 0.0
        write_zones(eng, boxes, out, stem, len(boxes))
        tot["ilp"] += mass
        tot["base"] += base_kg
        tot["agglo"] += agglo_kg
        tot["now"] += now if now == now else agglo_kg
        d_now = (mass - now) / now * 100.0 if now == now else float("nan")
        print(f"{stem:<38} {len(boxes):>3} {len(cand):>7} {r['rows']:>5} | "
              f"{mass:>8.0f} {r['bound']:>8.0f} {gap:>5.1f}% {r['status']:>9} | "
              f"{agglo_kg:>6.0f} {now:>6.0f} {d_now:>+6.1f}% | "
              f"{base_kg:>6.0f} {(mass - base_kg) / base_kg * 100:>+6.1f}% {dt:>6.0f}"
              f" ({r['t_build']:.0f}b/{r['t_solve']:.0f}s)"
              + ("" if ok else "  COVER FAIL"), flush=True)
    if tot["base"]:
        print(f"{'TOTAL':<38} {'':>3} {'':>7} {'':>5} | {tot['ilp']:>8.0f} {'':>8} "
              f"{'':>6} {'':>9} | {tot['agglo']:>6.0f} {tot['now']:>6.0f} "
              f"{(tot['ilp'] - tot['now']) / tot['now'] * 100:>+6.1f}% | "
              f"{tot['base']:>6.0f} "
              f"{(tot['ilp'] - tot['base']) / tot['base'] * 100:>+6.1f}%")


if __name__ == "__main__":
    main()
