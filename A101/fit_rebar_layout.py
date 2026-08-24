from __future__ import annotations

from collections import Counter
from itertools import combinations
from math import ceil, floor, gcd, lcm
from typing import Mapping


_EPS = 1e-8


def _axis(v):
    v = str(v).lower()
    if v not in {'x', 'y'}:
        raise ValueError("axis должен быть 'x' или 'y'")
    return v


def _geom(x):
    if hasattr(x, 'bounds'):
        return x
    try:
        from shapely.geometry import Polygon, shape
        g = shape(x) if isinstance(x, dict) else Polygon([tuple(map(float, p[:2])) for p in x])
        if not g.is_valid:
            g = g.buffer(0)
        if g.is_empty or g.area <= 0:
            raise ValueError
        return g
    except Exception as e:
        raise ValueError('Полигон должен быть Shapely-геометрией или массивом [[x,y], ...]') from e


def _polygons(items, values=None):
    if values is None:
        try:
            raw, vals = zip(*items)
        except Exception as e:
            raise ValueError('Передайте values либо polygons=[(geometry_or_coords, value), ...]') from e
    else:
        if len(items) != len(values):
            raise ValueError('len(polygons) != len(values)')
        raw, vals = items, values
    return [_geom(x) for x in raw], list(map(int, vals))


def _rectangles(items):
    out = []
    for i, r in enumerate(items):
        if len(r) == 2 and hasattr(r[0], 'bounds'):
            x0, y0, x1, y1, c = (*r[0].bounds, r[1])
        elif len(r) == 5:
            x0, y0, x1, y1, c = r
        else:
            raise ValueError(f'rectangles[{i}] должен быть (x0,y0,x1,y1,class) или (geometry,class)')
        if not x0 < x1 or not y0 < y1:
            raise ValueError(f'Некорректный rectangles[{i}]')
        out.append((float(x0), float(y0), float(x1), float(y1), int(c)))
    return out


def _profile(value, recipes, mode):
    if value <= 0:
        return ()
    r = tuple(map(int, recipes.get(value, (value,))))
    if not r or any(x <= 0 for x in r):
        raise ValueError(f'Некорректный recipe для {value}')
    if mode == 'exact':
        return tuple(sorted(Counter(r).items()))
    if mode != 'threshold':
        raise ValueError("recipe_mode: 'threshold' или 'exact'")
    return tuple((t, sum(x >= t for x in r)) for t in sorted(set(r)))


def _contributes(c, t, mode):
    return c == t if mode == 'exact' else c >= t


def _field_geometry(geoms, field):
    from shapely.geometry import box
    from shapely.ops import unary_union
    return unary_union(geoms) if field is None else (field if hasattr(field, 'bounds') else box(*map(float, field)))


def _scaled(geoms, vals, rects, divisors, min_width, scale, field):
    s = float(scale)
    if s <= 0:
        raise ValueError('scale должен быть положительным')
    boxes = []
    for g, v in zip(geoms, vals):
        x0, y0, x1, y1 = g.bounds
        boxes.append((floor(x0*s + 1e-9), floor(y0*s + 1e-9), ceil(x1*s - 1e-9), ceil(y1*s - 1e-9), v))
    rr = [(round(x0*s), round(y0*s), round(x1*s), round(y1*s), c) for x0, y0, x1, y1, c in rects]
    steps = {int(c): round(float(v)*s) for c, v in divisors.items()}
    if not steps or any(v <= 0 for v in steps.values()):
        raise ValueError('Все divisors должны быть положительными')
    missing = sorted({r[4] for r in rr} - steps.keys())
    if missing:
        raise ValueError(f'Нет divisors для классов {missing}')
    src = min_width if isinstance(min_width, Mapping) else ({c: min_width for c in steps} if min_width is not None else {})
    mins = {c: max(0, ceil(max(0.0,float(src.get(c,0)))*s/steps[c]-1e-12)*steps[c]) for c in steps}
    x0, y0, x1, y1 = field.bounds
    B = floor(x0*s), floor(y0*s), ceil(x1*s), ceil(y1*s)
    return boxes, rr, steps, mins, B, s


def _cross(r, axis):
    return (r[0], r[2]) if axis == 'y' else (r[1], r[3])


def _long(r, axis):
    return (r[1], r[3]) if axis == 'y' else (r[0], r[2])


def _union(a, b):
    return b[:4] if a is None else (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _interval(old0, old1, req0, req1, length, lo, hi):
    if length > hi-lo or req1-req0 > length:
        return None
    a, b = max(lo, req1-length), min(hi-length, req0)
    if a > b:
        return None
    x = min(max((old0 + old1 - length)/2, a), b)
    return x, x + length


def _fit(old, req, step, minimum, axis, B):
    if axis == 'y':
        n = ceil(max(1, minimum, req[2]-req[0])/step - 1e-12)*step
        X, Y = _interval(old[0], old[2], req[0], req[2], n, B[0], B[2]), (req[1], req[3])
    else:
        n = ceil(max(1, minimum, req[3]-req[1])/step - 1e-12)*step
        X, Y = (req[0], req[2]), _interval(old[1], old[3], req[1], req[3], n, B[1], B[3])
    return None if X is None or Y is None or X[0] >= X[1] or Y[0] >= Y[1] else (X[0], Y[0], X[1], Y[1], old[4])


def _inside_field(r, field, scale, rel_tol=1e-6):
    if field is None:
        return True
    from shapely.geometry import box
    b = box(*(x/scale for x in r[:4]))
    if field.covers(b):
        return True
    # covers() - точный предикат: он даёт False и из-за микрощелей в unary_union
    # триангулированной мозаики, и из-за округления координат зоны. Реальный выход
    # за контур - это проценты площади зоны, артефакт - 1e-9 и меньше, поэтому
    # различаем их по площади, а не по предикату.
    return b.difference(field).area <= rel_tol * b.area


def _stages(boxes, recipes, mode):
    out = []
    for value in sorted({b[4] for b in boxes if b[4] > 0}, reverse=True):
        for layer, _ in sorted(_profile(value, recipes, mode), reverse=True):
            if (value, layer) not in out:
                out.append((value, layer))
    return out


def _touch_limits(rects, steps, axis):
    top = max(steps)
    lo, hi, contacts = [-float('inf')]*len(rects), [float('inf')]*len(rects), []
    for i, j in combinations(range(len(rects)), 2):
        a, b = rects[i], rects[j]
        # Preserve also corner/endpoint contacts: after assignment longitudinal
        # spans may grow and an ignored endpoint contact can turn into an overlap.
        if a[4] != b[4] or min(_long(a, axis)[1], _long(b, axis)[1]) < max(_long(a, axis)[0], _long(b, axis)[0]) - _EPS:
            continue
        a0, a1 = _cross(a, axis); b0, b1 = _cross(b, axis)
        mul = 2 if a[4] == top else 1
        if abs(a1-b0) <= _EPS:
            hi[i], lo[j] = min(hi[i], a1+mul*steps[a[4]]), max(lo[j], b0-mul*steps[b[4]])
            contacts.append((i, j, a1))
        elif abs(b1-a0) <= _EPS:
            hi[j], lo[i] = min(hi[j], b1+mul*steps[a[4]]), max(lo[i], a0-mul*steps[b[4]])
            contacts.append((j, i, b1))
    return lo, hi, contacts


def _new_class(t, mode, steps, densities, allowed):
    cs = [c for c in allowed if c in steps and (c == t or (mode == 'threshold' and c >= t))]
    return min(cs, key=lambda c: (float(densities.get(c, 1)), c)) if cs else None


def _assign(geoms, boxes, rects, recipes, steps, mins, axis, B, field, scale,
            densities, mode, max_snap, allow_new, allowed, preserve_count=False):
    from shapely.geometry import box
    req, assigned = [None]*len(rects), [[] for _ in rects]
    source, created, priority = list(range(len(rects))), [None]*len(rects), [10**9]*len(rects)
    rg = [box(*(x/scale for x in r[:4])) for r in rects]
    stages, chosen = _stages(boxes, recipes, mode), [set() for _ in boxes]
    by_value = {v: sorted((p for p, b in enumerate(boxes) if b[4] == v), key=lambda p: (-geoms[p].area, p)) for v, _ in stages}
    protected_lo, protected_hi, _ = _touch_limits(rects, steps, axis)
    top = max(steps)
    for stage, (value, t) in enumerate(stages):
        for p in by_value[value]:
            b, g, point = boxes[p], geoms[p], geoms[p].representative_point()
            need, metrics = dict(_profile(value, recipes, mode))[t], None
            while sum(_contributes(rects[i][4], t, mode) for i in chosen[p]) < need:
                if metrics is None or len(metrics) != len(rects):
                    metrics = []
                    for R in rg:
                        inter = R.intersection(g).area
                        metrics.append((R.covers(point), inter/g.area, R.distance(g)))
                best, blocked = None, False
                for i, old in enumerate(rects):
                    if i in chosen[p] or not _contributes(old[4], t, mode):
                        continue
                    hit, overlap, distance = metrics[i]
                    if not hit and overlap <= 1e-12 and distance > max_snap:
                        continue
                    q = _union(req[i], b); q0, q1 = (_cross((*q, old[4]), axis))
                    if q0 < protected_lo[i]-_EPS or q1 > protected_hi[i]+_EPS:
                        continue
                    mul = 2 if old[4] == top else 1
                    o0, o1 = _cross(old, axis); cap = max_snap + mul*steps[old[4]]
                    if q0 < o0-cap or q1 > o1+cap:
                        continue
                    nr = _fit(old, q, steps[old[4]], mins[old[4]], axis, B)
                    if nr is None:
                        continue
                    if field is not None and not _inside_field(nr, field, scale):
                        blocked = True; continue
                    cur = _fit(old, req[i], steps[old[4]], mins[old[4]], axis, B) if req[i] else None
                    area = (nr[2]-nr[0])*(nr[3]-nr[1]); old_area = 0 if cur is None else (cur[2]-cur[0])*(cur[3]-cur[1])
                    tier = 0 if hit else (1 if overlap > 1e-12 else 2)
                    key = (old[4] != t, tier, distance if tier == 2 else -overlap,
                           max(0, area-old_area)*float(densities.get(old[4], 1)),
                           sum(abs(nr[k]-old[k]) for k in range(4)), i)
                    if best is None or key < best[0]:
                        best = key, i
                if best is None and preserve_count:
                    # Fallback: сохраняем N. Если зона уже правильно расположена
                    # поперёк стержней, разрешаем значительно удлинить её вдоль axis.
                    # Это не меняет фазу/контактную топологию сетки.
                    bc0, bc1 = _cross((*b, 0), axis)
                    for i, old in enumerate(rects):
                        if i in chosen[p] or not _contributes(old[4], t, mode):
                            continue
                        o0, o1 = _cross(old, axis)
                        cross_gap = max(0, o0-bc1, bc0-o1)
                        if cross_gap > max_snap + _EPS:
                            continue
                        q = _union(req[i], b); q0, q1 = _cross((*q, old[4]), axis)
                        if q0 < protected_lo[i]-_EPS or q1 > protected_hi[i]+_EPS:
                            continue
                        mul = 2 if old[4] == top else 1
                        cap = max_snap + mul*steps[old[4]]
                        if q0 < o0-cap or q1 > o1+cap:
                            continue
                        nr = _fit(old, q, steps[old[4]], mins[old[4]], axis, B)
                        if nr is None or (field is not None and not _inside_field(nr, field, scale)):
                            continue
                        cur = _fit(old, req[i], steps[old[4]], mins[old[4]], axis, B) if req[i] else old
                        area = (nr[2]-nr[0])*(nr[3]-nr[1])
                        old_area = (cur[2]-cur[0])*(cur[3]-cur[1])
                        key = (old[4] != t, cross_gap,
                               max(0, area-old_area)*float(densities.get(old[4], 1)),
                               sum(abs(nr[k]-old[k]) for k in range(4)), i)
                        if best is None or key < best[0]:
                            best = key, i

                if best is None:
                    if preserve_count or not allow_new:
                        return None, None, None, None, None, stages, [('field' if blocked else 'coverage', p, t, need)]
                    c = _new_class(t, mode, steps, densities, allowed)
                    if c is None:
                        return None, None, None, None, None, stages, [('class', p, t)]
                    nr = _fit((b[0], b[1], b[2], b[3], c), b, steps[c], mins[c], axis, B)
                    if nr is None or (field is not None and not _inside_field(nr, field, scale)):
                        return None, None, None, None, None, stages, [('field', p, t)]
                    i = len(rects); rects.append(nr); rg.append(box(*(x/scale for x in nr[:4])))
                    req.append(b[:4]); assigned.append([p]); source.append(None); created.append(p); priority.append(stage)
                    protected_lo.append(-float('inf')); protected_hi.append(float('inf')); chosen[p].add(i); metrics = None
                else:
                    i = best[1]; req[i] = _union(req[i], b); assigned[i].append(p); chosen[p].add(i); priority[i] = min(priority[i], stage)
    return req, assigned, source, created, priority, stages, []


class _DSU:
    def __init__(self, n): self.p = list(range(n)); self.r = [0]*n
    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]; x = self.p[x]
        return x
    def union(self, a, b):
        a, b = self.find(a), self.find(b)
        if a == b: return
        if self.r[a] < self.r[b]: a, b = b, a
        self.p[b] = a
        if self.r[a] == self.r[b]: self.r[a] += 1


def _topology(rects, assigned, priority, axis):
    n, d, edges = len(rects), _DSU(2*len(rects)), []
    for i, r in enumerate(rects):
        a, b = _long(r, axis); x0, x1 = _cross(r, axis)
        edges += [(i, 0, x0, a, b, r[4]), (i, 1, x1, a, b, r[4])]
    contacts, linked = [], []
    for u, v in combinations(range(2*n), 2):
        i, si, x, a, b, c = edges[u]; j, sj, y, p, q, e = edges[v]
        if i == j or abs(x-y) > _EPS or min(b, q) <= max(a, p)+_EPS:
            continue
        shared = bool(set(assigned[i]) & set(assigned[j]))
        if c == e and si != sj:
            d.union(u, v)
            contacts.append((i, j, x) if si == 1 else (j, i, x))
        elif c != e and shared:
            # Recipe-related coincident boundaries are a preference, not a hard equality:
            # different layers may need different edges to preserve coverage.
            linked.append((i, si, j, sj, x))
    roots = {}
    for u in range(2*n): roots.setdefault(d.find(u), []).append(u)
    cid = {r: k for k, r in enumerate(roots)}
    edge_comp = [cid[d.find(u)] for u in range(2*n)]
    comps = []
    for members in roots.values():
        comps.append({'members': members,
                      'target': sum(edges[u][2] for u in members)/len(members),
                      'priority': min(priority[edges[u][0]] for u in members)})
    return edge_comp, comps, sorted(set(contacts)), linked, edges


def _structural(rects, req, priority, steps, mins, axis, B, edge_comp, comps, edges, quantum):
    try:
        import numpy as np
        from scipy.optimize import linprog
    except ImportError as e:
        raise ImportError('Для топологической подгонки установите scipy') from e
    m, lo, hi = len(comps), *((B[0], B[2]) if axis == 'y' else (B[1], B[3]))
    bounds = [(lo, hi)]*m
    for k, c in enumerate(comps):
        xs = [edges[u][2] for u in c['members']]
        if any(abs(x-lo) <= _EPS for x in xs): bounds[k] = (lo, lo)
        if any(abs(x-hi) <= _EPS for x in xs): bounds[k] = (hi, hi)
    A, bb = [], []
    for i, r in enumerate(rects):
        L, R, q, st = edge_comp[2*i], edge_comp[2*i+1], req[i] or r[:4], steps[r[4]]
        q0, q1 = _cross((*q, r[4]), axis)
        row = np.zeros(m); row[L] = 1; A.append(row); bb.append(q0+st)
        row = np.zeros(m); row[R] = -1; A.append(row); bb.append(-(q1-st))
        # Structural envelope must contain at least one final width that is a whole
        # number of bar steps and can cover q with the same relaxation as _candidates().
        allow = st + quantum
        kmin = max(1, ceil(mins[r[4]]/st - 1e-9),
                   ceil(max(0, q1-q0-2*allow)/st - 1e-9))
        row = np.zeros(m); row[L] = 1; row[R] = -1; A.append(row); bb.append(-(kmin*st))
    # Исходно разделённые однотипные зоны не могут поменять порядок или создать новый нахлёст.
    for i, j in combinations(range(len(rects)), 2):
        if rects[i][4] != rects[j][4] or min(_long(rects[i], axis)[1], _long(rects[j], axis)[1]) <= max(_long(rects[i], axis)[0], _long(rects[j], axis)[0])+_EPS: continue
        a0,a1=_cross(rects[i],axis); b0,b1=_cross(rects[j],axis)
        if a1 <= b0+_EPS:
            row=np.zeros(m); row[edge_comp[2*i+1]]=1; row[edge_comp[2*j]]=-1; A.append(row); bb.append(0)
        elif b1 <= a0+_EPS:
            row=np.zeros(m); row[edge_comp[2*j+1]]=1; row[edge_comp[2*i]]=-1; A.append(row); bb.append(0)
    A = np.asarray(A); bb = np.asarray(bb)
    AA = np.zeros((len(A), 2*m)); AA[:, :m] = A
    dev, db = [], []
    maxp = max(priority)
    c = np.zeros(2*m)
    for k, comp in enumerate(comps):
        c[m+k] = 1 + 10*(maxp-comp['priority']); t = comp['target']
        row = np.zeros(2*m); row[k] = 1; row[m+k] = -1; dev.append(row); db.append(t)
        row = np.zeros(2*m); row[k] = -1; row[m+k] = -1; dev.append(row); db.append(-t)
    z = linprog(c, A_ub=np.vstack((AA, dev)), b_ub=np.r_[bb, db], bounds=bounds+[(0, None)]*m, method='highs')
    if not z.success:
        return None, [('topology', z.message)]
    x, out = z.x[:m], []
    for i, r in enumerate(rects):
        q = req[i] or r[:4]
        out.append((x[edge_comp[2*i]], q[1], x[edge_comp[2*i+1]], q[3], r[4]) if axis == 'y'
                   else (q[0], x[edge_comp[2*i]], q[2], x[edge_comp[2*i+1]], r[4]))
    return out, []


def _quantum(steps, gap):
    g = 0
    for x in steps.values(): g = gcd(g, int(round(x)))
    if gap <= 0: return g
    for q in range(max(1, int(round(gap))), g+1):
        if g % q == 0: return q
    return g


def _candidates(i, structural, req, originals, steps, mins, axis, origin, quantum, B):
    r, q, st = structural[i], req[i] or structural[i][:4], steps[structural[i][4]]
    allow = st + quantum
    s0, s1 = _cross(r, axis); q0, q1 = _cross((*q, r[4]), axis)
    f0, f1 = (B[0], B[2]) if axis == 'y' else (B[1], B[3])
    kmax = floor((s1-s0)/st + 1e-9)
    kmin = max(1, ceil(mins[r[4]]/st - 1e-9), ceil(max(0, q1-q0-2*allow)/st - 1e-9))
    out = []
    for k in range(kmin, kmax+1):
        w = k*st
        # Final bar-bounded zone may move by one phase quantum outside the
        # continuous structural envelope; this is needed to reach a valid global phase.
        lo = max(f0, s0-quantum, q1-allow-w)
        hi = min(f1-w, s1+quantum-w, q0+allow)
        if lo > hi+_EPS: continue
        a,b=ceil((lo-origin)/quantum-1e-9),floor((hi-origin)/quantum+1e-9)
        xs={lo,hi}; xs.update(origin+n*quantum for n in range(a,b+1)); xs.update(x for x in (s0,s1-w) if lo-_EPS<=x<=hi+_EPS)
        # Для крайнего полигона обязательно рассматриваем вариант прямо от границы
        # поля, даже если structural-envelope расположен дальше чем на один quantum.
        if q0 <= f0+_EPS and q1 <= f0+w+allow+_EPS: xs.add(f0)
        if q1 >= f1-_EPS and q0 >= f1-w-allow-_EPS: xs.add(f1-w)
        for x in sorted(xs): out.append((x,r[1],x+w,r[3],r[4]) if axis=='y' else (r[0],x,r[2],x+w,r[4]))
    return out


def _positions(r, step, axis):
    a, b = _cross(r, axis); return tuple(a+k*step for k in range(round((b-a)/step)+1))


def _overlap(r, q, axis):
    c = max(0, min(_cross(r, axis)[1], _cross(q, axis)[1])-max(_cross(r, axis)[0], _cross(q, axis)[0]))
    a = max(0, min(_long(r, axis)[1], _long(q, axis)[1])-max(_long(r, axis)[0], _long(q, axis)[0]))
    return c, a


def _area_overlap(a, b):
    return max(0, min(a[2], b[2])-max(a[0], b[0]))*max(0, min(a[3], b[3])-max(a[1], b[1]))


def _phase(a, b, module, gap):
    if a == b: return a/2 if a == module else 0
    big, small = max(a, b), min(a, b)
    if big % small == 0:
        if big//small == 2: return big/3 if small/3 >= gap else small/2
        return 0
    g = gcd(a, b); return g/2 if g >= 2*gap else 0


def _cyclic(x, y, mod):
    d = abs((x-y) % mod); return min(d, mod-d)


def _issues(rects, steps, axis, gap):
    pos = [_positions(r, steps[r[4]], axis) for r in rects]
    triples, close = [], []
    for i, j, k in combinations(range(len(rects)), 3):
        c0, c1 = max(_cross(rects[z], axis)[0] for z in (i, j, k)), min(_cross(rects[z], axis)[1] for z in (i, j, k))
        a0, a1 = max(_long(rects[z], axis)[0] for z in (i, j, k)), min(_long(rects[z], axis)[1] for z in (i, j, k))
        if c1 >= c0 and a1 > a0:
            for p in set(pos[i]) & set(pos[j]) & set(pos[k]):
                if c0-_EPS <= p <= c1+_EPS: triples.append((i, j, k, p, a0, a1))
    for i, j in combinations(range(len(rects)), 2):
        c0, c1 = max(_cross(rects[i], axis)[0], _cross(rects[j], axis)[0]), min(_cross(rects[i], axis)[1], _cross(rects[j], axis)[1])
        a0, a1 = max(_long(rects[i], axis)[0], _long(rects[j], axis)[0]), min(_long(rects[i], axis)[1], _long(rects[j], axis)[1])
        if c1 < c0 or a1 <= a0: continue
        ds = [(abs(x-y), x, y) for x in pos[i] for y in pos[j]
              if c0-_EPS <= x <= c1+_EPS and c0-_EPS <= y <= c1+_EPS and abs(x-y) > _EPS]
        if ds and min(ds)[0] < gap-_EPS:
            d, x, y = min(ds); close.append((i, j, d, x, y, a0, a1))
    return triples, close


def _local_score(R, i, originals, assigned, contacts, steps, priority, axis, gap, boxes=None, B=None,
                 all_zones=False):
    r, p = R[i], set(_positions(R[i], steps[R[i][4]], axis))
    new_n = new_a = triple = close_n = close_d = phase_n = phase_d = 0
    edge_n = edge_d = cover_n = cover_d = 0
    if boxes is not None and B is not None:
        f0, f1 = (B[0], B[2]) if axis == 'y' else (B[1], B[3])
        for pidx in assigned[i]:
            b = boxes[pidx]; b0, b1 = _cross((*b, r[4]), axis)
            a0, a1 = _cross(r, axis); miss = max(0, a0-b0, b1-a1)
            if b0 <= f0+_EPS or b1 >= f1-_EPS:
                if miss > _EPS: edge_n += 1; edge_d += miss
            else:
                extra = max(0, miss-steps[r[4]])
                if extra > _EPS: cover_n += 1; cover_d += extra
    module = lcm(*steps.values())
    pi = priority[i]
    for j, q in enumerate(R):
        # priority-фильтр нужен только на этапе последовательной раскладки (_choose),
        # где зоны с большим priority ещё не поставлены. После раскладки все зоны
        # уже имеют реальные координаты, поэтому конфликт с ними реален и его
        # нельзя не замечать - иначе перестановка зоны молча ломает соседей.
        if i == j or (not all_zones and priority[j] > pi): continue
        if r[4] == q[4] and _area_overlap(originals[i], originals[j]) <= _EPS:
            a = _area_overlap(r, q)
            if a > _EPS: new_n += 1; new_a += a
        c, a = _overlap(r, q, axis)
        if c > 0 and a > 0:
            lo, hi = max(_cross(r, axis)[0], _cross(q, axis)[0]), min(_cross(r, axis)[1], _cross(q, axis)[1])
            d = [abs(x-y) for x in p for y in _positions(q, steps[q[4]], axis)
                 if lo-_EPS <= x <= hi+_EPS and lo-_EPS <= y <= hi+_EPS and abs(x-y) > _EPS]
            if d and min(d) < gap-_EPS: close_n += 1; close_d += gap-min(d)
            if set(assigned[i]) & set(assigned[j]):
                g = gcd(steps[r[4]], steps[q[4]]); pref = _phase(steps[r[4]], steps[q[4]], module, gap)
                z = (_cross(r, axis)[0]-_cross(q, axis)[0]) % g
                e = min(_cyclic(z, pref, g), _cyclic(z, -pref, g))
                if e > _EPS: phase_n += 1; phase_d += e
    for j, k in combinations((z for z in range(len(R)) if z != i and priority[z] <= pi), 2):
        c0, c1 = max(_cross(R[z], axis)[0] for z in (i, j, k)), min(_cross(R[z], axis)[1] for z in (i, j, k))
        a0, a1 = max(_long(R[z], axis)[0] for z in (i, j, k)), min(_long(R[z], axis)[1] for z in (i, j, k))
        if c1 >= c0 and a1 > a0:
            triple += sum(c0-_EPS <= x <= c1+_EPS for x in p & set(_positions(R[j], steps[R[j][4]], axis)) & set(_positions(R[k], steps[R[k][4]], axis)))
    contact_n = contact_d = 0
    for a, b, _ in contacts:
        if i not in (a, b) or priority[a] > pi or priority[b] > pi: continue
        z = _cross(R[b], axis)[0]-_cross(R[a], axis)[1]; target = steps[R[a][4]]
        if z < -_EPS: contact_n += 1000; contact_d += -z
        elif abs(z-target) > _EPS: contact_n += 1; contact_d += abs(z-target)
    area = (r[2]-r[0])*(r[3]-r[1])
    move = sum(abs(r[k]-originals[i][k]) for k in range(4))
    return new_n, new_a, triple, close_n, close_d, edge_n, edge_d, cover_n, cover_d, contact_n, contact_d, phase_n, phase_d, area, move


def _choose(candidates, originals, assigned, contacts, steps, priority, axis, gap, passes, boxes=None, B=None):
    R = [min(c, key=lambda r: ((r[2]-r[0])*(r[3]-r[1]), sum(abs(r[k]-originals[i][k]) for k in range(4)))) for i, c in enumerate(candidates)]
    for p in sorted(set(priority)):
        active = [i for i, x in enumerate(priority) if x == p]
        for turn in range(passes):
            changed = False
            for i in (active if turn % 2 == 0 else active[::-1]):
                old = R[i]; best = (_local_score(R, i, originals, assigned, contacts, steps, priority, axis, gap, boxes, B), old)
                for r in candidates[i]:
                    if r == old: continue
                    R[i] = r; key = _local_score(R, i, originals, assigned, contacts, steps, priority, axis, gap, boxes, B)
                    if key < best[0]: best = key, r
                R[i] = best[1]; changed |= R[i] != old
            if not changed: break
    return R



def _minimize_cross(R, candidates, originals, assigned, contacts, steps, priority, axis, gap, boxes, B, passes=2):
    R = list(R)
    def breaks_exact(i, cand):
        old=R[i]; R[i]=cand
        bad=False
        for a,b,_ in contacts:
            if i not in (a,b): continue
            # Защищаем только контакт, который до изменения уже был идеальным.
            R[i]=old; d0=_cross(R[b],axis)[0]-_cross(R[a],axis)[1]; target=steps[R[a][4]]
            R[i]=cand; d1=_cross(R[b],axis)[0]-_cross(R[a],axis)[1]
            if abs(d0-target)<=_EPS and abs(d1-target)>_EPS: bad=True; break
        R[i]=old
        return bad
    for _ in range(passes):
        changed = False
        for i in sorted(range(len(R)), key=lambda i: priority[i]):
            old = R[i]; ow = _cross(old, axis)[1]-_cross(old, axis)[0]
            def compact_key():
                z=_local_score(R, i, originals, assigned, contacts, steps, priority, axis, gap, boxes, B,
                               all_zones=True)
                # Счётчики жёстких нарушений (new_n, triple, close_n) идут строго впереди
                # их величин (new_a, close_d). Иначе лексикографическое сравнение обрывается
                # на мизерном выигрыше по площади нахлёста и берёт вариант, добавляющий
                # нарушения bar_gap - раскладка становится Infeasible из-за экономии 0.1%.
                return (z[0], z[2], z[3]) + (z[1], z[4]) + z[5:9] + (z[13],) + z[9:13] + (z[14],)
            best = (compact_key(), old)
            for r in candidates[i]:
                if _cross(r, axis)[1]-_cross(r, axis)[0] > ow+_EPS or breaks_exact(i,r): continue
                R[i] = r; key = compact_key()
                if key < best[0]: best = key, r
                R[i]=old
            R[i] = best[1]; changed |= R[i] != old
        if not changed: break
    return R


def _new_overlaps(X, originals, created=None):
    """Нахлёсты одного класса, которых не было в исходных зонах (жёсткая ошибка)."""
    if originals is None: return 0
    return sum(1 for i, j in combinations(range(len(X)), 2)
               if X[i][4] == X[j][4]
               and _area_overlap(originals[i], originals[j]) <= _EPS
               and _area_overlap(X[i], X[j]) > _EPS
               and (created is None or (created[i] is None and created[j] is None)))


def _repair_spacing(R, candidates, steps, axis, gap, passes=3, *, originals=None, created=None):
    R=list(R)
    def score(X):
        t,c=_issues(X,steps,axis,gap)
        o=_new_overlaps(X,originals,created)
        # Сначала общее число жёстких нарушений: иначе починка bar_gap может
        # разменять его на new_overlap, и раскладка всё равно останется Infeasible.
        return o+len(t)+len(c),o,len(t),len(c),sum((r[2]-r[0])*(r[3]-r[1]) for r in X)
    best=score(R)
    for _ in range(passes):
        changed=False
        _, close=_issues(R,steps,axis,gap)
        for conflict in close:
            for i in conflict[:2]:
                old=R[i]
                for r in candidates[i]:
                    if r==old: continue
                    R[i]=r; z=score(R)
                    if z<best: best=z; old=r; changed=True
                R[i]=old
        if not changed: break
    return R


def _repair_contacts(R, candidates, contacts, steps, axis, gap):
    R=list(R); adj={}
    for i,j,_ in contacts:
        t=min(steps[R[i][4]],steps[R[j][4]])
        adj.setdefault(i,[]).append((j,1,t)); adj.setdefault(j,[]).append((i,-1,t))
    seen=set()
    for start in list(adj):
        if start in seen: continue
        comp=[]; stack=[start]
        while stack:
            u=stack.pop()
            if u in seen: continue
            seen.add(u); comp.append(u); stack += [v for v,_,_ in adj.get(u,[]) if v not in seen]
        if len(comp)<2: continue
        order=sorted(comp,key=lambda u:-len(adj.get(u,[])))
        cur={u:R[u] for u in comp}; best=[None]
        def compatible(u,r,chosen):
            for v,side,t in adj.get(u,[]):
                if v not in chosen: continue
                q=chosen[v]
                d=(_cross(r,axis)[0]-_cross(q,axis)[1]) if side<0 else (_cross(q,axis)[0]-_cross(r,axis)[1])
                if abs(d-t)>_EPS: return False
            return True
        def dfs(k,chosen,cost):
            if best[0] is not None and cost>=best[0][0]: return
            if k==len(order): best[0]=(cost,dict(chosen)); return
            u=order[k]
            opts=sorted(candidates[u],key=lambda r:(sum(abs(r[z]-cur[u][z]) for z in range(4)),(r[2]-r[0])*(r[3]-r[1])))
            for r in opts:
                if compatible(u,r,chosen):
                    dfs(k+1,{**chosen,u:r},cost+sum(abs(r[z]-cur[u][z]) for z in range(4)))
        dfs(0,{},0)
        if best[0] is None: continue
        old_t,old_c=_issues(R,steps,axis,gap); old={u:R[u] for u in comp}
        for u,r in best[0][1].items(): R[u]=r
        new_t,new_c=_issues(R,steps,axis,gap)
        if (len(new_t),len(new_c))>(len(old_t),len(old_c)):
            for u,r in old.items(): R[u]=r
    return R

def _edge_score(R, boxes, recipes, mode, axis, B):
    f0,f1=(B[0],B[2]) if axis=='y' else (B[1],B[3]); bad=misssum=0
    for b in boxes:
        b0,b1=_cross((*b,b[4]),axis)
        if b0>f0+_EPS and b1<f1-_EPS: continue
        for t,need in _profile(b[4],recipes,mode):
            miss=[]
            for r in R:
                if not _contributes(r[4],t,mode): continue
                a,b2=_long(r,axis); c,d=_long((*b,r[4]),axis)
                if a>c+_EPS or b2<d-_EPS: continue
                a,b2=_cross(r,axis); miss.append(max(0,a-b0,b1-b2))
            miss.sort(); exact=sum(x<=_EPS for x in miss)
            bad += max(0,need-exact)
            if exact<need: misssum += sum(miss[:need]) if len(miss)>=need else 10**9
    return bad,misssum


def _edge_relaxations(R, boxes, recipes, mode, axis, B):
    f0,f1=(B[0],B[2]) if axis=='y' else (B[1],B[3]); out=[]
    for p,b in enumerate(boxes):
        b0,b1=_cross((*b,b[4]),axis)
        if b0>f0+_EPS and b1<f1-_EPS: continue
        for t,need in _profile(b[4],recipes,mode):
            q=[]
            for i,r in enumerate(R):
                if not _contributes(r[4],t,mode): continue
                a,z=_long(r,axis); c,d=_long((*b,r[4]),axis)
                if a>c+_EPS or z<d-_EPS: continue
                a,z=_cross(r,axis); q.append((max(0,a-b0,b1-z),i))
            exact=sum(m<=_EPS for m,_ in q)
            if exact<need:
                q.sort(); out.append({'polygon':p,'class':b[4],'threshold':t,'covered':exact,'required':need,
                                      'best_miss':None if len(q)<need else q[need-1][0]})
    return out


def _repair_edge_pairs(R, candidates, originals, assigned, boxes, recipes, mode, steps, axis, B, gap):
    R=list(R); base=_edge_score(R,boxes,recipes,mode,axis,B)
    f0,f1=(B[0],B[2]) if axis=='y' else (B[1],B[3])
    for i,j in combinations(range(len(R)),2):
        if R[i][4]!=R[j][4] or min(_long(R[i],axis)[1],_long(R[j],axis)[1])<=max(_long(R[i],axis)[0],_long(R[j],axis)[0])+_EPS: continue
        def edge_sides(k):
            out=set()
            for p in assigned[k]:
                a,b=_cross((*boxes[p],R[k][4]),axis)
                if a<=f0+_EPS: out.add(0)
                if b>=f1-_EPS: out.add(1)
            return out
        sides=edge_sides(i)&edge_sides(j)
        if not sides: continue
        def edge_opts(k):
            st=steps[R[k][4]]
            return [r for r in candidates[k] if any((_cross(r,axis)[0] <= f0+st+_EPS) if side==0 else (_cross(r,axis)[1] >= f1-st-_EPS) for side in sides)]
        A,C=edge_opts(i),edge_opts(j)
        if not A or not C: continue
        oldi,oldj=R[i],R[j]; best=(base, (oldi[2]-oldi[0])*(oldi[3]-oldi[1])+(oldj[2]-oldj[0])*(oldj[3]-oldj[1]), oldi,oldj)
        for a in A:
            R[i]=a
            for b in C:
                R[j]=b
                tr,cl=_issues(R,steps,axis,gap)
                if tr or cl: continue
                bad_overlap=False
                for u in (i,j):
                    for v in range(len(R)):
                        if u==v or R[u][4]!=R[v][4]: continue
                        if _area_overlap(originals[u],originals[v])<=_EPS and _area_overlap(R[u],R[v])>_EPS: bad_overlap=True; break
                    if bad_overlap: break
                if bad_overlap: continue
                e=_edge_score(R,boxes,recipes,mode,axis,B)
                ar=(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])
                if (e,ar)<(best[0],best[1]): best=(e,ar,a,b)
        R[i],R[j]=best[2],best[3]
        cov,_=_coverage(R,boxes,recipes,mode,steps,_quantum(steps,gap),axis)
        if cov: R[i],R[j]=oldi,oldj
        else: base=best[0]
    return R


def _shrink_long(R, boxes, recipes, mode, steps, quantum, axis, B, passes=2):
    R = list(R)
    def ok(r, b, t):
        if not _contributes(r[4], t, mode): return False
        a,b1=_long(r,axis); c,d=_long((*b,r[4]),axis)
        if a>c+_EPS or b1<d-_EPS: return False
        a,b1=_cross(r,axis); c,d=_cross((*b,r[4]),axis)
        return max(0, max(0,a-c,d-b1)-steps[r[4]]) <= quantum+_EPS
    f0,f1=(B[0],B[2]) if axis=='y' else (B[1],B[3])
    def exact(r,b,t):
        if not _contributes(r[4],t,mode): return False
        return r[0]<=b[0]+_EPS and r[1]<=b[1]+_EPS and r[2]>=b[2]-_EPS and r[3]>=b[3]-_EPS
    for _ in range(passes):
        changed=False
        for i in range(len(R)):
            essential=[]
            for b in boxes:
                for t,need in _profile(b[4],recipes,mode):
                    q=[j for j,r in enumerate(R) if ok(r,b,t)]
                    if i in q and len(q)<=need: essential.append(b)
                    b0,b1=_cross((*b,b[4]),axis)
                    if b0<=f0+_EPS or b1>=f1-_EPS:
                        q=[j for j,r in enumerate(R) if exact(r,b,t)]
                        if i in q and len(q)<=need: essential.append(b)
            if not essential: continue
            lo=min(_long((*b,R[i][4]),axis)[0] for b in essential)
            hi=max(_long((*b,R[i][4]),axis)[1] for b in essential)
            a,b=_long(R[i],axis); lo=max(a,lo); hi=min(b,hi)
            if lo>a+_EPS or hi<b-_EPS:
                r=R[i]; R[i]=(r[0],lo,r[2],hi,r[4]) if axis=='y' else (lo,r[1],hi,r[3],r[4]); changed=True
        if not changed: break
    return R


def _coverage(rects, boxes, recipes, mode, steps, quantum, axis):
    errors, relax = [], []
    for p, b in enumerate(boxes):
        for t, need in _profile(b[4], recipes, mode):
            options = []
            for i, r in enumerate(rects):
                if not _contributes(r[4], t, mode): continue
                if _long(r, axis)[0] > _long((*b, r[4]), axis)[0]+_EPS or _long(r, axis)[1] < _long((*b, r[4]), axis)[1]-_EPS: continue
                a0, a1 = _cross(r, axis); b0, b1 = _cross((*b, r[4]), axis)
                miss = max(0, a0-b0, b1-a1); extra = max(0, miss-steps[r[4]])
                if extra <= quantum+_EPS: options.append((extra, i, miss))
            options.sort()
            if len(options) < need:
                errors.append(('coverage', p, t, len(options), need)); continue
            relax += [{'polygon': p, 'threshold': t, 'zone': i, 'extra': e, 'miss': m}
                      for e, i, m in options[:need] if e > _EPS]
    return errors, relax


def _bars(rects, steps, axis, scale, share):
    raw, by_zone = [], [[] for _ in rects]
    for i, r in enumerate(rects):
        for p in _positions(r, steps[r[4]], axis):
            if axis == 'y': x0 = x1 = p/scale; y0, y1 = r[1]/scale, r[3]/scale
            else: y0 = y1 = p/scale; x0, x1 = r[0]/scale, r[2]/scale
            z = {'zone': i, 'zones': [i], 'class': r[4], 'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1}
            raw.append(z); by_zone[i].append(z)
    if not share:
        physical = [dict(z, id=i) for i, z in enumerate(raw)]
    else:
        groups, physical = {}, []
        for z in raw:
            p = z['x0'] if axis == 'y' else z['y0']; a = z['y0'] if axis == 'y' else z['x0']; b = z['y1'] if axis == 'y' else z['x1']
            groups.setdefault((z['class'], p), []).append((a, b, z['zone']))
        for (c, p), spans in groups.items():
            spans.sort(); merged = []
            for a, b, i in spans:
                if merged and a <= merged[-1][1]+_EPS: merged[-1][1] = max(merged[-1][1], b); merged[-1][2].add(i)
                else: merged.append([a, b, {i}])
            for a, b, zones in merged:
                q = {'id': len(physical), 'zone': next(iter(zones)) if len(zones) == 1 else None,
                     'zones': sorted(zones), 'class': c}
                q.update({'x0': p, 'x1': p, 'y0': a, 'y1': b} if axis == 'y' else {'x0': a, 'x1': b, 'y0': p, 'y1': p})
                physical.append(q)
    for bars in by_zone:
        for z in bars:
            for p in physical:
                same = p['x0'] == z['x0'] == p['x1'] == z['x1'] if axis == 'y' else p['y0'] == z['y0'] == p['y1'] == z['y1']
                span = p['y0'] <= z['y0']+_EPS and p['y1'] >= z['y1']-_EPS if axis == 'y' else p['x0'] <= z['x0']+_EPS and p['x1'] >= z['x1']-_EPS
                if p['class'] == z['class'] and z['zone'] in p['zones'] and same and span:
                    z['physical_id'] = p['id']; break
    return physical, by_zone


def _bar_stats(rects, steps, axis, physical):
    best = None
    for i, j in combinations(range(len(rects)), 2):
        c, a = _overlap(rects[i], rects[j], axis)
        if not c or not a: continue
        lo, hi = max(_cross(rects[i], axis)[0], _cross(rects[j], axis)[0]), min(_cross(rects[i], axis)[1], _cross(rects[j], axis)[1])
        d = [abs(x-y) for x in _positions(rects[i], steps[rects[i][4]], axis) for y in _positions(rects[j], steps[rects[j][4]], axis)
             if lo-_EPS <= x <= hi+_EPS and lo-_EPS <= y <= hi+_EPS and abs(x-y) > _EPS]
        if d: best = min(min(d), best) if best is not None else min(d)
    groups = {}
    for b in physical:
        p = b['x0'] if axis == 'y' else b['y0']; a = b['y0'] if axis == 'y' else b['x0']; z = b['y1'] if axis == 'y' else b['x1']
        groups.setdefault(round(p, 9), []).append((a, z))
    depth = 0
    for spans in groups.values():
        events = sorted([(a, 1) for a, z in spans]+[(z, -1) for a, z in spans], key=lambda x: (x[0], -x[1]))
        n = 0
        for _, d in events: n += d; depth = max(depth, n)
    return best, depth


def fit_rebar_layout(polygons, rectangles, recipes, divisors, values=None, densities=None, *,
                     min_width=None, axis='y', recipe_mode='threshold', field=None,
                     strict_field=True, field_tol=None, scale=1, origin=None, drop_unused=False,
                     max_snap=None, allow_new_zones=True, preserve_zone_count=True, min_bar_gap=50,
                     layout_passes=8, strict_layout=True, coincident_policy='stack'):
    """Иерархическая топологическая постобработка зон и единая сетка стержней."""
    axis, recipes, densities = _axis(axis), dict(recipes or {}), dict(densities or {})
    if coincident_policy not in {'stack', 'share_same_class'}:
        raise ValueError("coincident_policy: 'stack' или 'share_same_class'")
    geoms, vals = _polygons(polygons, values); source_rects = _rectangles(rectangles)
    field_geom = _field_geometry(geoms, field)
    boxes, rr, steps, mins, B, s = _scaled(geoms, vals, source_rects, divisors, min_width, scale, field_geom)
    # Координаты зон квантуются до целых единиц (floor/ceil в _scaled), поэтому зона
    # может выступать за контур на величину до одной единицы. Проверять вхождение с
    # допуском 1e-7 строже, чем собственное округление алгоритма: раскладка падала
    # из-за срезов в десятки мм² у зон размером в десятки метров.
    field_eps = (1.0/s) if field_tol is None else max(0.0, float(field_tol))
    exact = field_geom.buffer(field_eps) if strict_field else None
    snap = 2*max(map(float, divisors.values())) if max_snap is None else max(0, float(max_snap))
    allowed = {r[4] for r in rr} | {int(c) for c in densities if int(c) > 0 and int(c) not in recipes}
    req, assigned, source, created, priority, order, errors = _assign(
        geoms, boxes, rr, recipes, steps, mins, axis, B, exact, s, densities,
        recipe_mode, snap, allow_new_zones, allowed, preserve_zone_count)
    if req is None:
        return {'status': 'Infeasible', 'is_feasible': False, 'is_optimal': False,
                'axis': axis, 'errors': errors, 'rectangles': None, 'zones': [], 'bars': [], 'warnings': []}
    keep = [i for i in range(len(rr)) if req[i] is not None or not drop_unused]
    rr, req, assigned = [rr[i] for i in keep], [req[i] or rr[i][:4] for i in keep], [assigned[i] for i in keep]
    source, created, priority = [source[i] for i in keep], [created[i] for i in keep], [priority[i] for i in keep]
    q = _quantum(steps, round(float(min_bar_gap)*s))
    o = (B[0] if axis == 'y' else B[1]) if origin is None else round(float(origin)*s)
    edge_comp, comps, contacts, linked, edge_data = _topology(rr, assigned, priority, axis)
    for comp in comps:
        if len(comp['members']) > 1:
            comp['target'] = o + round((comp['target']-o)/q)*q
    structural, e = _structural(rr, req, priority, steps, mins, axis, B, edge_comp, comps, edge_data, q)
    errors += e
    if structural is None:
        return {'status': 'Infeasible', 'is_feasible': False, 'is_optimal': False,
                'axis': axis, 'errors': errors, 'rectangles': None, 'zones': [], 'bars': [], 'warnings': []}
    candidates = [_candidates(i, structural, req, rr, steps, mins, axis, o, q, B) for i in range(len(rr))]
    if any(not x for x in candidates):
        errors += [('phase_domain', i) for i, x in enumerate(candidates) if not x]
        return {'status': 'Infeasible', 'is_feasible': False, 'is_optimal': False,
                'axis': axis, 'errors': errors, 'rectangles': None, 'zones': [], 'bars': [], 'warnings': [],
                'structural_rectangles': [(a/s, b/s, c/s, d/s, k) for a, b, c, d, k in structural]}
    fitted = _choose(candidates, rr, assigned, contacts, steps, priority, axis, round(float(min_bar_gap)*s), layout_passes, boxes, B)
    fitted = _repair_spacing(fitted, candidates, steps, axis, round(float(min_bar_gap)*s),
                             originals=rr, created=created)
    fitted = _repair_contacts(fitted, candidates, contacts, steps, axis, round(float(min_bar_gap)*s))
    area_before_cleanup = sum((r[2]-r[0])*(r[3]-r[1]) for r in fitted)/(s*s)
    fitted = _minimize_cross(fitted, candidates, rr, assigned, contacts, steps, priority, axis, round(float(min_bar_gap)*s), boxes, B)
    fitted = _repair_edge_pairs(fitted, candidates, rr, assigned, boxes, recipes, recipe_mode, steps, axis, B, round(float(min_bar_gap)*s))
    fitted = _shrink_long(fitted, boxes, recipes, recipe_mode, steps, q, axis, B)
    # _minimize_cross/_repair_edge_pairs/_shrink_long двигают зоны по своим критериям и
    # могут вернуть нарушения, снятые ранее. Финальная починка идёт по глобальному числу
    # жёстких нарушений и принимает только строгие улучшения, поэтому безопасна.
    fitted = _repair_spacing(fitted, candidates, steps, axis, round(float(min_bar_gap)*s),
                             originals=rr, created=created)
    area_after_cleanup = sum((r[2]-r[0])*(r[3]-r[1]) for r in fitted)/(s*s)
    edge_relax = _edge_relaxations(fitted, boxes, recipes, recipe_mode, axis, B)
    triples, close = _issues(fitted, steps, axis, round(float(min_bar_gap)*s))
    all_new_overlaps=[(i,j,_area_overlap(fitted[i],fitted[j])) for i,j in combinations(range(len(fitted)),2)
                      if fitted[i][4]==fitted[j][4] and _area_overlap(rr[i],rr[j])<=_EPS and _area_overlap(fitted[i],fitted[j])>_EPS]
    share = coincident_policy == 'share_same_class'
    # Нахлёст двух зон ОДНОГО класса фатален только при policy='stack': там каждая зона
    # получает свой стержень, и в пересечении их оказалось бы два в одной точке.
    # При 'share_same_class' совпадающие стержни объединяются в один физический,
    # что и есть корректная раскладка, поэтому это предупреждение, а не ошибка.
    new_overlaps=[] if share else [x for x in all_new_overlaps
                                   if created[x[0]] is None and created[x[1]] is None]
    repair_overlaps=[x for x in all_new_overlaps if x not in new_overlaps]
    cov_errors, cov_relax = _coverage(fitted, boxes, recipes, recipe_mode, steps, q, axis)
    errors += cov_errors
    for i, r in enumerate(fitted):
        if not (B[0] <= r[0] < r[2] <= B[2] and B[1] <= r[1] < r[3] <= B[3]): errors.append(('bounds', i))
        if exact is not None and not _inside_field(r, exact, s): errors.append(('field', i))
    hard = [('new_overlap', *x) for x in new_overlaps] + [('triple_bar', *x) for x in triples] + [('bar_gap', *x) for x in close]
    if strict_layout: errors += hard
    out = [(float(a)/s, float(b)/s, float(c)/s, float(d)/s, k) for a, b, c, d, k in fitted]
    structural_out = [(float(a)/s, float(b)/s, float(c)/s, float(d)/s, k) for a, b, c, d, k in structural]
    if errors:
        return {'status': 'Infeasible', 'is_feasible': False, 'is_optimal': False, 'axis': axis,
                'errors': errors, 'rectangles': None, 'candidate_rectangles': out,
                'structural_rectangles': structural_out, 'zones': [], 'bars': [], 'warnings': []}
    bars, zone_bars = _bars(fitted, steps, axis, s, share)
    actual_gap, max_layers = _bar_stats(fitted, steps, axis, bars)
    contact_relax = []
    for i, j, old in contacts:
        gap = _cross(fitted[j], axis)[0]-_cross(fitted[i], axis)[1]
        if abs(gap-steps[fitted[i][4]]) > _EPS:
            contact_relax.append((i, j, gap/s, steps[fitted[i][4]]/s))
    phase_miss = 0; module = lcm(*steps.values())
    for i, j in combinations(range(len(fitted)), 2):
        if not (set(assigned[i]) & set(assigned[j])) or min(_overlap(fitted[i], fitted[j], axis)) <= 0: continue
        g = gcd(steps[fitted[i][4]], steps[fitted[j][4]]); pref = _phase(steps[fitted[i][4]], steps[fitted[j][4]], module, round(float(min_bar_gap)*s))
        z = (_cross(fitted[i], axis)[0]-_cross(fitted[j], axis)[0]) % g
        phase_miss += min(_cyclic(z, pref, g), _cyclic(z, -pref, g)) > _EPS
    warnings=[]
    if repair_overlaps: warnings.append(f'Локальных recipe-нахлёстов новых зон: {len(repair_overlaps)}')
    if contact_relax: warnings.append(f'Ослаблено целевых зазоров исходных контактов: {len(contact_relax)}')
    if phase_miss: warnings.append(f'Ослаблено фазовых правил recipes: {phase_miss}')
    if cov_relax: warnings.append(f'Локальная релаксация покрытия при привязке к общей сетке: {len(cov_relax)}')
    if edge_relax: warnings.append(f'Вынужденных релаксаций у внешней границы: {len(edge_relax)}')
    if any(x is not None for x in created): warnings.append(f'Добавлено локальных зон: {sum(x is not None for x in created)}')
    zones, shifts = [], []
    for i, r in enumerate(out):
        old = source_rects[source[i]] if source[i] is not None else r
        shift = sum(abs(r[k]-old[k]) for k in range(4)); shifts.append(shift)
        zrel = [x['extra']/s for x in cov_relax if x['zone'] == i]
        zones.append({'id': i, 'source_index': source[i], 'created_for_polygon': created[i],
                      'class': r[4], 'stage': order[priority[i]] if priority[i] < len(order) else None,
                      'priority': priority[i], 'bounds': r[:4], 'structural_bounds': structural_out[i][:4],
                      'step': steps[r[4]]/s, 'coverage_margin': (steps[r[4]]+q)/s,
                      'coverage_relaxation': max(zrel, default=0), 'assigned_polygons': assigned[i],
                      'edge_shift': shift, 'bars': zone_bars[i]})
    groups = _DSU(len(rr))
    for i, j, _ in contacts: groups.union(i, j)
    contact_groups = len({groups.find(i) for i in range(len(rr))})
    topology_contacts = [{'left_zone': i, 'right_zone': j, 'class': fitted[i][4],
                          'structural_coordinate': _cross(structural[i], axis)[1]/s,
                          'final_gap': (_cross(fitted[j], axis)[0]-_cross(fitted[i], axis)[1])/s,
                          'target_gap': steps[fitted[i][4]]/s} for i, j, _ in contacts]
    linked_boundaries = [{'zone_a': i, 'side_a': a, 'zone_b': j, 'side_b': b, 'coordinate': x/s}
                         for i, a, j, b, x in linked]
    return {'status': 'Feasible', 'is_feasible': True, 'is_optimal': False, 'axis': axis,
            'coincident_policy': coincident_policy, 'rectangles': out,
            'structural_rectangles': structural_out, 'zones': zones, 'bars': bars,
            'errors': [], 'warnings': warnings, 'coverage_relaxations': cov_relax, 'edge_relaxations': edge_relax,
            'contact_relaxations': contact_relax, 'topology_contacts': topology_contacts,
            'linked_boundaries': linked_boundaries,
            'stats': {'polygons': len(boxes), 'input_zones': len(source_rects), 'zones': len(out),
                      'created_zones': sum(x is not None for x in created), 'bars': len(bars),
                      'max_edge_shift': max(shifts, default=0), 'max_snap': snap,
                      'min_bar_gap': float(min_bar_gap),
                      'actual_min_bar_gap': None if actual_gap is None else actual_gap/s,
                      'max_physical_layers': max_layers, 'hard_conflicts': 0,
                      'new_same_class_overlaps': 0, 'repair_overlaps': len(repair_overlaps), 'phase_quantum': q/s,
                      'coupled_boundary_components': sum(len(c['members']) > 1 for c in comps),
                      'contact_groups': contact_groups, 'contacts': len(contacts),
                      'contact_relaxations': len(contact_relax), 'phase_relaxations': phase_miss,
                      'coverage_relaxations': len(cov_relax), 'edge_relaxations': len(edge_relax),
                      'max_coverage_relaxation': max((x['extra'] for x in cov_relax), default=0)/s,
                      'area_before_cleanup': area_before_cleanup, 'area_after_cleanup': area_after_cleanup,
                      'area_reduction': area_before_cleanup-area_after_cleanup,
                      'processing_order': order}}
