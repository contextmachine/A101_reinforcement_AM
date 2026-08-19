# A101 Reinforcement — frontend

Web UI for the A101 additional-reinforcement solver: set up a run, look at the
zones it produced, and read the metrics off them.

The solver backend is not available yet. **Everything except the "process a
drawing" step already works today**, driven by the solver's own result `.json`:
open one from disk and the drawing viewer, the per-zone table and the analytics
all light up. Processing is wired but disabled until a backend is pointed at the
build — see [Backend](#backend).

**Requires Node `^20.19.0 || >=22.12.0`** — the Vite 8 toolchain will not start
on anything older (it fails on a missing `node:util` export rather than on the
version check). `.nvmrc` pins a working major:

```bash
nvm use              # or: nvm install 22
npm install
npm run dev          # http://localhost:5173
```

Click **Load sample result** to explore the UI with the bundled reference result.

## What it does

**Set up a run.** Drop in a `.dwg`/`.dxf`, then set the bar direction, the
background mesh (`back_grid`), the rebar `stock` the solver may pick from, and
the constraints — `max_lay`, `min_w`, `iron_dens`, `anchor_k`.

**Look at the result.** A pan/zoom plan view drawn from the result geometry:
zone rectangles filled and outlined by rebar type, every bar drawn at its true
position and spacing, bar thickness tracking the diameter, and a scale bar in
millimetres. Zones are directly labelled, labels are collision-culled, and the
legend doubles as a per-rebar-type filter. Hovering reads out a zone; clicking
selects it, and the selection is shared with the table and the charts.

**Read the numbers.** A sortable, filterable table — one row per zone with its
extents, width, bar length, bar count, rebar length (net and with anchorage),
mass, share of total, and kg/m², with column totals and a CSV export.

**See the summary.** Totals for mass, bar count, rebar length, reinforced area
and intensity; a mass-by-zone chart marking how few zones carry 80% of the
steel; a breakdown by rebar type in mass, length or bar count; a rebar schedule;
and an echo of the parameters the run used.

**And it checks the result.** Mass is recomputed from the bar geometry and
compared against the reported figure, and the UI flags rebar outside the
configured stock, zones narrower than `min_w`, zones with no primary rectangle,
and bar counts that disagree with the geometry.

### Metrics, and where they come from

The solver's mass is exactly

```
mass = iron_dens · π·⌀²/4 · Σ(bar lengths)
```

with no anchorage included — verified against the reference result to within
floating-point noise (`src/lib/__tests__/metrics.test.ts`). Anchorage is
therefore reported *alongside* the net figure, never folded into it: the UI adds
`anchor_k · ⌀` at each end of every bar and shows the result as a separate
"with anchorage" number. Bar lengths are measured from the bar segments rather
than trusted from the `length` field, so a malformed result shows up instead of
silently skewing the totals.

## Backend

There is no backend in this repository, and the UI does not ship a stand-in for
one. `src/api/client.ts` is the **only** module that touches the network, and
everything in it — endpoint paths, the job/polling shape, the response types —
is a placeholder chosen to get the UI built, **not an agreed contract**. When
the real service exists, align that one file with it.

What the UI needs, in whatever shape the backend prefers:

1. hand it a drawing plus the solver parameters, and get a handle back;
2. ask whether that handle has finished;
3. fetch the result JSON — the solver's own output format, unchanged;
4. fetch the drawing files it produced.

Point the app at it with `VITE_API_BASE` (or `VITE_PROXY_TARGET` in
development); see `.env.example`. With neither set, the app says so plainly and
offers the open-a-result-file flow instead.

### The result format

The shape the viewer reads is the solver's own output, typed in
`src/lib/types.ts` as `RawResult`:

```jsonc
{
  "N": 33,                     // zone count
  "mass": 2750.686,            // total additional-reinforcement mass, kg
  "zones": [{
    "primary rectangle": [x1, y1, x2, y2] | null,  // pre-quantisation source
    "final rectangle":   [x1, y1, x2, y2],         // as built, mm
    "width": 900.0,            // extent across the bars, mm
    "length": 350.0,           // length of one bar, mm
    "diameter": 25.0,
    "step": 150.0,
    "bars count": 7,
    "zone mass": 9.44,         // kg
    "bars": [[x1, y1, x2, y2]] // one segment per bar, mm
  }]
}
```

Two optional keys are read when present: `params` (the parameters the run used,
so the UI reports them rather than guessing) and `direction` (`"x"` or `"y"`).
Anything else is ignored, so the backend can add fields freely.

`samples/TopY_arming.json` is a real result and doubles as the fixture the unit
tests assert against; `public/sample-result.json` is the same file, served to the
**Load sample result** button.

## Development

```bash
npm run dev        # dev server
npm test           # unit tests (vitest)
npm run typecheck  # tsc --build
npm run lint       # oxlint
npm run build      # typecheck + production build to dist/
```

### Layout

```
src/
  api/client.ts        the only network module — provisional, see Backend
  lib/
    types.ts           result + parameter types, mirroring the solver output
    metrics.ts         normalisation, derived metrics, aggregates, warnings
    viewport.ts        world↔screen transform for the plan viewer
    colors.ts          categorical palette, resolved from CSS variables
    params.ts          parameter validation and canonicalisation
    download.ts        CSV/JSON/file download helpers
  components/
    ParamsForm.tsx     drawing input and solver parameters
    PlanViewer.tsx     canvas plan view: pan, zoom, hit-testing, labels
    ZonesTable.tsx     per-zone metrics table
    SummaryPanel.tsx   KPIs, charts, rebar schedule, warnings
    Charts.tsx         mass-by-zone and by-rebar-type charts
```

Units are millimetres throughout, matching the solver; masses are kilograms and
lengths in the UI are converted to metres at the point of display.

### Colour

Series colours come from a categorical palette validated for colour-vision
deficiency and for contrast against both the light and dark chart surfaces (see
the palette block in `src/index.css`). Slots are assigned in fixed order by mass
rank at load time and never recomputed while filtering, so a colour always means
the same rebar type. Colour is never the sole encoding: rebar type is also
carried by the on-plan labels, the legend text, and the table.
