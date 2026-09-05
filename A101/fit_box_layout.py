"""Ranked-class box fitting with recipe fallback, Python 3.9 compatible.

A rectangle of class ``c`` directly covers every demand class ``d <= c``.
Recipes remain valid alternative realisations: e.g. class 2 may also be covered
by two class-1 rectangles. Spatial demands are never added together.
"""
from collections import Counter, defaultdict

VERSION = "v9-ranked-or-recipe-py39-2026-09-03"


def fit_box_layout(polygons, rectangles, recipes=None, *, recipe_mode="threshold",
                   densities=None, min_w=None, nearest=8, per_direction=1,
                   max_distance=None, time_limit=None, mip_rel_gap=0,
                   allow_class_upgrade=True, allowed_classes=None,
                   milp_backend="auto", threads=1, output=False, eps=1e-8):
    import numpy as np
    import shapely
    from shapely.geometry import box
    from shapely.ops import polygonize, unary_union
    from shapely.strtree import STRtree

    if recipe_mode not in {"threshold", "exact"}:
        raise ValueError("recipe_mode: 'threshold' или 'exact'")

    def seq(value):
        return (value,) if np.isscalar(value) else tuple(value)

    recipes = {int(k): tuple(map(int, seq(v))) for k, v in dict(recipes or {}).items()}
    densities = {int(k): float(v) for k, v in dict(densities or {}).items()}
    leaf_cache = {}

    def leaves(cls, stack=()):
        cls = int(cls)
        if cls in leaf_cache:
            return leaf_cache[cls]
        if cls not in recipes:
            return (cls,)
        if cls in stack:
            raise ValueError("Циклический recipe для класса %s" % cls)
        value = tuple(x for child in recipes[cls] for x in leaves(child, stack + (cls,)))
        leaf_cache[cls] = value
        return value

    def recipe_density(cls):
        cls = int(cls)
        if cls in densities:
            return float(densities[cls])
        q = leaves(cls)
        return sum(densities[x] for x in q) if q != (cls,) and all(x in densities for x in q) else float("inf")

    def contribution(cls, threshold):
        q, threshold = leaves(cls), int(threshold)
        return q.count(threshold) if recipe_mode == "exact" else sum(x >= threshold for x in q)

    def profile(cls):
        cls = int(cls)
        if cls <= 0:
            return []
        q = leaves(cls)
        if not q or any(x <= 0 for x in q):
            raise ValueError("Некорректный recipe для %s" % cls)
        return (sorted(Counter(q).items()) if recipe_mode == "exact" else
                [(t, sum(x >= t for x in q)) for t in sorted(set(q))])

    def parse_polygon(row):
        if isinstance(row, dict):
            geometry, cls = row["geometry"], row.get("class", row.get("load"))
        else:
            geometry, cls = row
        geometry = geometry if geometry.is_valid else geometry.buffer(0)
        return geometry, int(cls)

    def parse_rectangle(row):
        if isinstance(row, dict):
            geometry = row.get("geometry")
            if geometry is not None:
                x0, y0, x1, y1 = geometry.bounds
            elif row.get("bounds") is not None:
                x0, y0, x1, y1 = row["bounds"][:4]
            else:
                x0, y0 = row["x"], row["y"]
                x1, y1 = x0 + row["width"], y0 + row["height"]
            cls = int(row.get("class", row.get("load")))
            default = recipe_density(cls)
            density = float(row.get("density", row.get("weight", default if np.isfinite(default) else 1.0)))
        else:
            x0, y0, x1, y1, cls = row
            cls = int(cls); default = recipe_density(cls)
            density = float(default if np.isfinite(default) else 1.0)
        if not (float(x0) < float(x1) and float(y0) < float(y1)):
            raise ValueError("Некорректный прямоугольник: %r" % (row,))
        return (float(x0), float(y0), float(x1), float(y1), cls), density

    demand = [parse_polygon(row) for row in polygons]
    parsed = [parse_rectangle(row) for row in rectangles]
    source = [row for row, _ in parsed]
    bounds0 = np.asarray([row[:4] for row in source], dtype=float) if source else np.empty((0, 4))
    classes0 = np.asarray([row[4] for row in source], dtype=int)
    explicit_density = np.asarray([value for _, value in parsed], dtype=float)
    geometries = np.asarray([box(*row[:4]) for row in source], dtype=object)

    physical = (set(map(int, allowed_classes)) if allowed_classes is not None else
                set(densities) | {leaf for cls in recipes for leaf in leaves(cls)})
    candidates = set(densities) | set(recipes) | physical | {leaf for cls in recipes for leaf in leaves(cls)}
    constructible = {cls for cls in candidates if all(leaf in physical for leaf in leaves(cls))}
    upgrade_available = constructible if allowed_classes is not None else candidates
    available = sorted(set(map(int, classes0)) | upgrade_available)
    thresholds = sorted({leaf for cls in available if cls > 0 for leaf in leaves(cls)})

    def density(cls, owner=None):
        value = recipe_density(cls)
        if np.isfinite(value):
            return float(value)
        if owner is not None and int(cls) == int(classes0[owner]):
            return float(explicit_density[owner])
        return float("inf")

    def dominates(left, right):
        # Demand classes form a monotone ranking.
        return int(left) >= int(right)

    def join_class(classes):
        target = max(map(int, classes))
        q = [cls for cls in available if cls >= target and np.isfinite(density(cls))]
        return min(q, key=lambda cls: (cls, density(cls))) if q else None

    def upgrade_classes(owner, demand_class, threshold):
        original, demand_class = int(classes0[owner]), int(demand_class)
        q = [cls for cls in upgrade_available | {original}
             if cls >= original and np.isfinite(density(cls, owner))
             and (cls >= demand_class or contribution(cls, threshold) > 0)]
        if not allow_class_upgrade:
            q = [cls for cls in q if cls == original]
        keep = []
        for cls in sorted(q, key=lambda c: (density(c, owner), c)):
            if not any(old >= cls and density(old, owner) <= density(cls, owner) + eps for old in keep):
                keep.append(cls)
        return keep

    def minimum_width(cls):
        value = min_w.get(int(cls), 0) if hasattr(min_w, "get") else (0 if min_w is None else min_w)
        return max(0.0, float(value))

    relevant = {}
    for cls in available:
        rows = [g for g, need_cls in demand
                if cls >= need_cls or any(contribution(cls, t) > 0 for t, _ in profile(need_cls))]
        relevant[cls] = unary_union(rows) if rows else None

    def tighten(owner, raw_bounds, cls):
        raw_bounds = tuple(map(float, raw_bounds)); useful = relevant.get(int(cls))
        hit = None if useful is None else box(*raw_bounds).intersection(useful)
        x0, y0, x1, y1 = raw_bounds if hit is None or hit.is_empty or hit.area <= eps else map(float, hit.bounds)
        width = minimum_width(cls)
        if x1 - x0 < width - eps:
            d = (width - x1 + x0) / 2.0; x0 -= d; x1 += d
        if y1 - y0 < width - eps:
            d = (width - y1 + y0) / 2.0; y0 -= d; y1 += d
        return float(x0), float(y0), float(x1), float(y1)

    tree = STRtree(geometries) if len(geometries) else None
    cells = []
    for polygon_index, (geometry, demand_class) in enumerate(demand):
        hits = [] if tree is None else list(map(int, tree.query(geometry, predicate="intersects")))
        borders = [geometry.boundary] + [geometries[i].boundary for i in hits]
        for atom in polygonize(unary_union(borders)):
            point = atom.representative_point()
            if atom.area <= eps or not geometry.covers(point):
                continue
            covering = [] if tree is None else list(map(int, tree.query(point, predicate="within")))
            for threshold, need in profile(demand_class):
                have = sum(need if int(classes0[i]) >= int(demand_class)
                           else contribution(classes0[i], threshold) for i in covering)
                if have < need:
                    cells.append({"g": atom, "b": atom.bounds, "d": int(demand_class),
                                  "t": int(threshold), "need": int(need),
                                  "deficit": int(need - have), "polygon": polygon_index})

    def result_rectangle(raw_bounds, cls):
        x0, y0, x1, y1 = map(float, raw_bounds)
        return x0, y0, x1, y1, int(cls)

    source_mass = sum((bounds0[i, 2] - bounds0[i, 0]) * (bounds0[i, 3] - bounds0[i, 1]) *
                      explicit_density[i] for i in range(len(source)))
    if not cells:
        fitted_bounds = [tighten(i, bounds0[i], classes0[i]) for i in range(len(source))]
        out = [result_rectangle(fitted_bounds[i], classes0[i]) for i in range(len(source))]
        mass = sum((b[2] - b[0]) * (b[3] - b[1]) * explicit_density[i]
                   for i, b in enumerate(fitted_bounds))
        return {"status": "Already covered", "is_feasible": True, "is_optimal": True,
                "rectangles": out, "objective": float(mass - source_mass), "mass": float(mass),
                "missing_groups": [], "class_changes": [],
                "stats": {"missing_cells": 0, "missing_groups": 0, "class_upgrades": 0}}

    grouped = defaultdict(list)
    for index, atom in enumerate(cells):
        grouped[(atom["d"], atom["t"], atom["deficit"])].append(index)
    groups = []
    for (demand_class, threshold, deficit), ids in grouped.items():
        parts = np.asarray([cells[i]["g"] for i in ids], dtype=object)
        local_tree, parent = STRtree(parts), list(range(len(parts)))

        def root(value):
            while parent[value] != value:
                parent[value] = parent[parent[value]]; value = parent[value]
            return value

        for left, right in zip(*local_tree.query(parts, predicate="intersects")):
            left, right = root(int(left)), root(int(right))
            if left != right:
                parent[right] = left
        components = defaultdict(list)
        for local, original in enumerate(ids):
            components[root(local)].append(original)
        for member_ids in components.values():
            geometry = shapely.union_all([cells[i]["g"] for i in member_ids])
            groups.append({"g": geometry, "b": geometry.bounds, "d": demand_class,
                           "t": threshold, "deficit": deficit})

    proposals = [set() for _ in source]
    for group in groups:
        target_classes = {i: upgrade_classes(i, group["d"], group["t"]) for i in range(len(source))}
        target_classes = {i: values for i, values in target_classes.items() if values}
        eligible = []
        for i, values in target_classes.items():
            covered = bool(geometries[i].covers(group["g"]))
            useful = [cls for cls in values if not (covered and cls == int(classes0[i]))]
            if useful:
                target_classes[i] = useful; eligible.append(i)
        if not eligible:
            continue
        ids = np.asarray(eligible, dtype=int)
        distances = np.asarray(shapely.distance(geometries[ids], group["g"]), dtype=float)
        if max_distance is not None:
            mask = distances <= float(max_distance) + eps
            ids, distances = ids[mask], distances[mask]
        if not len(ids):
            continue
        current, target_bounds = bounds0[ids], np.asarray(group["b"], dtype=float)
        expanded_area = ((np.maximum(current[:, 2], target_bounds[2]) - np.minimum(current[:, 0], target_bounds[0])) *
                         (np.maximum(current[:, 3], target_bounds[3]) - np.minimum(current[:, 1], target_bounds[1])))
        scores = np.asarray([
            min(expanded_area[k] * density(cls, i) for cls in target_classes[int(i)]) -
            (current[k, 2] - current[k, 0]) * (current[k, 3] - current[k, 1]) * explicit_density[i]
            for k, i in enumerate(ids)
        ])
        order = np.lexsort((scores, distances))
        chosen = set(map(int, ids[order[:max(int(nearest), int(group["deficit"]))]]))
        sx = np.where(current[:, 2] <= target_bounds[0] + eps, -1,
                      np.where(current[:, 0] >= target_bounds[2] - eps, 1, 0))
        sy = np.where(current[:, 3] <= target_bounds[1] + eps, -1,
                      np.where(current[:, 1] >= target_bounds[3] - eps, 1, 0))
        for direction in set(zip(sx, sy)) - {(0, 0)}:
            positions = np.flatnonzero((sx == direction[0]) & (sy == direction[1]))
            chosen.update(map(int, ids[positions[np.lexsort((scores[positions], distances[positions]))[:int(per_direction)]]]))
        for i in chosen:
            expanded = (min(bounds0[i, 0], target_bounds[0]), min(bounds0[i, 1], target_bounds[1]),
                        max(bounds0[i, 2], target_bounds[2]), max(bounds0[i, 3], target_bounds[3]))
            for cls in target_classes[i]:
                proposals[i].add((expanded, int(cls)))

    cell_bounds = np.asarray([row["b"] for row in cells], dtype=float)
    cell_demands = np.asarray([row["d"] for row in cells], dtype=int)
    cell_thresholds = np.asarray([row["t"] for row in cells], dtype=int)
    cell_needs = np.asarray([row["need"] for row in cells], dtype=int)

    def cell_contribution(cls, j):
        return int(cell_needs[j]) if int(cls) >= int(cell_demands[j]) else contribution(cls, cell_thresholds[j])
    variants = []
    for owner, owner_proposals in enumerate(proposals):
        states = {(tuple(bounds0[owner]), int(classes0[owner]))}
        for proposal_bounds, proposal_class in owner_proposals:
            additions = set()
            for state_bounds, state_class in states:
                cls = join_class((state_class, proposal_class))
                if cls is None:
                    continue
                additions.add(((min(state_bounds[0], proposal_bounds[0]), min(state_bounds[1], proposal_bounds[1]),
                                max(state_bounds[2], proposal_bounds[2]), max(state_bounds[3], proposal_bounds[3])), cls))
            states |= additions

        best = {}
        original = tuple(bounds0[owner])
        original_mass = (original[2] - original[0]) * (original[3] - original[1]) * explicit_density[owner]
        for raw_bounds, cls in states:
            value_density = density(cls, owner)
            if not np.isfinite(value_density):
                continue
            fitted = tighten(owner, raw_bounds, cls)
            inside = ((fitted[0] <= cell_bounds[:, 0] + eps) & (fitted[1] <= cell_bounds[:, 1] + eps) &
                      (fitted[2] >= cell_bounds[:, 2] - eps) & (fitted[3] >= cell_bounds[:, 3] - eps))
            signature = tuple((int(j), int(cell_contribution(cls, j)))
                              for j in np.flatnonzero(inside)
                              if cell_contribution(cls, j) > 0)
            cost = (fitted[2] - fitted[0]) * (fitted[3] - fitted[1]) * value_density - original_mass
            if signature not in best or cost < best[signature][0] - eps:
                best[signature] = (float(cost), fitted, int(cls))

        kept = []
        for signature, (cost, fitted, cls) in sorted(best.items(), key=lambda item: (item[1][0], -sum(v for _, v in item[0]))):
            coverage = dict(signature)
            dominated = False
            for old_signature, old_cost, _, _ in kept:
                old = dict(old_signature)
                if old_cost <= cost + eps and all(old.get(cell, 0) >= value for cell, value in coverage.items()):
                    dominated = True; break
            if not dominated:
                kept.append((signature, cost, fitted, cls))
        variants.append(kept)

    flat, owners = [], []
    for owner, rows in enumerate(variants):
        owners.append([])
        for signature, cost, fitted, cls in rows:
            owners[owner].append(len(flat)); flat.append((owner, signature, cost, fitted, cls))

    coverage = [[] for _ in cells]
    for variable, (_, signature, _, _, _) in enumerate(flat):
        for cell, coefficient in signature:
            coverage[cell].append((variable, coefficient))

    rows = [(ids, [1.0] * len(ids), 1.0, 1.0) for ids in owners]
    merged = {}
    for atom, entries in zip(cells, coverage):
        entries = tuple(sorted(entries))
        maximum = defaultdict(int)
        for variable, coefficient in entries:
            maximum[flat[variable][0]] = max(maximum[flat[variable][0]], int(coefficient))
        if sum(maximum.values()) < atom["need"]:
            return {"status": "Infeasible", "is_feasible": False, "is_optimal": False,
                    "rectangles": None, "errors": [("coverage", atom["polygon"], atom["t"])],
                    "missing_groups": groups, "missing_required_classes": sorted({x["d"] for x in cells}),
                    "available_classes": available}
        merged[entries] = max(merged.get(entries, 0), int(atom["need"]))
    rows += [([v for v, _ in entries], [float(c) for _, c in entries], float(need), float("inf"))
             for entries, need in merged.items()]

    def solve_scipy():
        from scipy.optimize import Bounds, LinearConstraint, milp
        from scipy.sparse import csr_matrix

        ri, ci, values, lower, upper = [], [], [], [], []
        for row, (ids, coefficients, lo, hi) in enumerate(rows):
            ri.extend([row] * len(ids)); ci.extend(ids); values.extend(coefficients)
            lower.append(lo); upper.append(hi)
        matrix = csr_matrix((values, (ri, ci)), shape=(len(rows), len(flat)))
        options = {"disp": bool(output), "mip_rel_gap": float(mip_rel_gap)}
        if time_limit is not None:
            options["time_limit"] = float(time_limit)
        result = milp(c=np.asarray([row[2] for row in flat]), integrality=np.ones(len(flat), dtype=np.int8),
                      bounds=Bounds(np.zeros(len(flat)), np.ones(len(flat))),
                      constraints=LinearConstraint(matrix, np.asarray(lower), np.asarray(upper)), options=options)
        return None if result.x is None else np.asarray(result.x), str(result.message), bool(result.success)

    def solve_highs():
        import highspy

        solver = highspy.Highs(); solver.setOptionValue("output_flag", bool(output))
        solver.setOptionValue("threads", max(1, int(threads))); solver.setOptionValue("mip_rel_gap", float(mip_rel_gap))
        if time_limit is not None:
            solver.setOptionValue("time_limit", float(time_limit))
        count = len(flat); indices = np.arange(count, dtype=np.int32)
        solver.addVars(count, np.zeros(count), np.ones(count))
        solver.changeColsCost(count, indices, np.asarray([row[2] for row in flat]))
        solver.changeColsIntegrality(count, indices, np.asarray([highspy.HighsVarType.kInteger] * count))
        lengths = np.asarray([len(row[0]) for row in rows], dtype=np.int32)
        starts = np.r_[0, np.cumsum(lengths[:-1])].astype(np.int32)
        columns = np.asarray([v for ids, _, _, _ in rows for v in ids], dtype=np.int32)
        coefficients = np.asarray([c for _, values, _, _ in rows for c in values], dtype=float)
        solver.addRows(len(rows), np.asarray([row[2] for row in rows]),
                       np.asarray([highspy.kHighsInf if not np.isfinite(row[3]) else row[3] for row in rows]),
                       len(columns), starts, columns, coefficients)
        solver.run(); status = solver.getModelStatus(); values = np.asarray(list(solver.getSolution().col_value))
        return (values if len(values) == count else None), solver.modelStatusToString(status), status == highspy.HighsModelStatus.kOptimal

    backend = str(milp_backend).lower()
    if backend not in {"auto", "highs", "scipy"}:
        raise ValueError("milp_backend: 'auto', 'highs' или 'scipy'")
    values = status = optimal = None
    if backend in {"auto", "highs"}:
        try:
            values, status, optimal = solve_highs()
        except Exception:
            if backend == "highs":
                raise
            values = None
    if values is None and backend in {"auto", "scipy"}:
        values, status, optimal = solve_scipy()

    chosen = [max(ids, key=lambda variable: values[variable]) for ids in owners] if values is not None else []
    selected = set(chosen)
    feasible = len(chosen) == len(owners) and all(values[variable] > .5 for variable in chosen)
    feasible = feasible and all(lo - eps <= sum(c for v, c in zip(ids, coefficients) if v in selected) <= hi + eps
                                for ids, coefficients, lo, hi in rows)
    if not feasible:
        return {"status": status, "is_feasible": False, "is_optimal": False, "rectangles": None,
                "errors": [("solver", status)], "missing_groups": groups}

    out = [result_rectangle(flat[variable][3], flat[variable][4]) for variable in chosen]
    changes = [{"source_index": i, "old_class": int(classes0[i]), "new_class": int(flat[variable][4])}
               for i, variable in enumerate(chosen) if int(flat[variable][4]) != int(classes0[i])]
    mass = sum((flat[variable][3][2] - flat[variable][3][0]) *
               (flat[variable][3][3] - flat[variable][3][1]) * density(flat[variable][4], i)
               for i, variable in enumerate(chosen))
    return {"status": status, "is_feasible": True, "is_optimal": bool(optimal), "rectangles": out,
            "objective": float(mass - source_mass), "mass": float(mass), "missing_groups": groups,
            "class_changes": changes,
            "stats": {"missing_cells": len(cells), "missing_groups": len(groups), "variables": len(flat),
                      "constraints": len(rows), "variants_per_rectangle": list(map(len, owners)),
                      "class_upgrades": len(changes)}}
