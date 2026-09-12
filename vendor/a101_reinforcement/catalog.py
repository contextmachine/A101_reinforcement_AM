"""Rebar catalogue + mapping of LIRA colour-scale bands onto concrete rebar options.

The ЛИРА mosaic encodes, per finite element, the *total* required reinforcement
area (cm²/m).  The colour scale of every export we have seen is built exactly on
the А101 standard ladders from tables 2.4.2 … 2.4.4 of the ТЗ:

    upper bound of band k  ==  A_background + A_additional(k)

so a band can be mapped back to a concrete (diameter, step) pair by matching its
upper bound against ``background + ladder option``.  Example, ``Верхняя по Х``:

    scale:  8.5  17   25   29   40   58   70   89   110  169
    ladder: bg   +18@300 +18@150 +20@150 +20@100 +25@100 +28@100 +32@100 +36@100 +32x2@100
    totals: 8.48 16.96 25.44 29.42 39.90 57.57 69.98 88.88 110.18 169.28
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

STEEL_DENSITY = 7850.0  # kg/m³
ANCHORAGE_DIAMETERS = 40  # ТЗ: заведение за границу мозаики на 40d


def bar_area_mm2(diameter: float) -> float:
    return math.pi * diameter * diameter / 4.0


def area_cm2_per_m(diameter: float, step: float, layers: int = 1) -> float:
    """Reinforcement area of a mesh of ``diameter`` bars at ``step`` mm, cm²/m."""
    return layers * bar_area_mm2(diameter) * (1000.0 / step) / 100.0


def linear_mass_kg_m(diameter: float, layers: int = 1) -> float:
    return layers * STEEL_DENSITY * bar_area_mm2(diameter) * 1e-6


@dataclass(frozen=True, order=True)
class RebarOption:
    """One additional-reinforcement variant: bars of ``diameter`` every ``step``."""

    as_cm2_m: float
    diameter: int
    step: int
    layers: int = 1

    @staticmethod
    def make(diameter: int, step: int, layers: int = 1) -> "RebarOption":
        return RebarOption(area_cm2_per_m(diameter, step, layers), diameter, step, layers)

    @property
    def anchorage_mm(self) -> float:
        return ANCHORAGE_DIAMETERS * self.diameter

    @property
    def linear_mass_kg_m(self) -> float:
        return linear_mass_kg_m(self.diameter, self.layers)

    @property
    def label(self) -> str:
        d = f"{self.diameter}x{self.layers}" if self.layers > 1 else str(self.diameter)
        return f"d{d}s{self.step}"

    def bar_count(self, width_mm: float) -> int:
        """Bars in a zone of the given width.

        ТЗ example: width 1900 mm at step 100 -> 20 bars, width 800 -> 9 bars,
        i.e. the width is measured between the outermost bars.
        """
        return max(2, int(round(width_mm / self.step)) + 1) * self.layers

    def annotation(self, width_mm: float, length_mm: float) -> str:
        return (
            f"ф{self.diameter}-{int(round(length_mm))} "
            f"шаг {self.step} ({self.bar_count(width_mm)}шт.)"
        )


# --------------------------------------------------------------------------- #
# А101 standard ladders (ТЗ, tables 2.4.2 / 2.4.3 / 2.4.4).
# key   = background mesh (diameter, step)
# value = ordered additional variants (diameter, step, layers)
# --------------------------------------------------------------------------- #
LADDERS: dict[tuple[int, int], tuple[tuple[int, int, int], ...]] = {
    # т. 2.4.4 — плита перекрытия типового этажа t=200 (АТС 3.0)
    (10, 300): ((10, 300, 1), (10, 150, 1), (12, 150, 1), (12, 100, 1), (16, 150, 1), (16, 100, 1)),
    # т. 2.4.4 — плита перекрытия типового этажа t=160 (АТС 2.0)
    (10, 240): ((10, 240, 1), (10, 120, 1), (12, 120, 1), (16, 120, 1)),
    # т. 2.4.2 — плита перекрытия / покрытия подземной автостоянки
    (12, 300): (
        (12, 300, 1), (12, 150, 1), (14, 150, 1), (16, 150, 1),
        (14, 100, 1), (16, 100, 1), (20, 100, 1), (22, 100, 1),
    ),
    # т. 2.4.2 — капитель t=500-600
    (12, 150): (
        (12, 300, 1), (12, 150, 1), (14, 150, 1), (16, 150, 1),
        (14, 100, 1), (16, 100, 1), (20, 100, 1), (22, 100, 1), (25, 100, 1),
    ),
    # т. 2.4.2 / 2.4.3 — фундаментная плита t=450-550 и t=500
    (14, 300): (
        (14, 300, 1), (14, 150, 1), (16, 150, 1), (20, 150, 1),
        (20, 100, 1), (25, 100, 1), (28, 100, 1), (32, 100, 1),
    ),
    # т. 2.4.3 — фундаментная плита t=600-700
    (16, 300): (
        (16, 300, 1), (16, 150, 1), (20, 150, 1), (20, 100, 1),
        (25, 100, 1), (28, 100, 1), (32, 100, 1), (36, 100, 1),
    ),
    # т. 2.4.3 — фундаментная плита t=800-900
    (18, 300): (
        (18, 300, 1), (18, 150, 1), (20, 150, 1), (20, 100, 1),
        (25, 100, 1), (28, 100, 1), (32, 100, 1), (36, 100, 1),
    ),
    # т. 2.4.3 — фундаментная плита t=1000-1100
    (20, 300): (
        (20, 300, 1), (20, 150, 1), (25, 150, 1), (25, 100, 1),
        (28, 100, 1), (32, 100, 1), (36, 100, 1), (32, 100, 2),
    ),
    # т. 2.4.3 — фундаментная плита t=1200-1300
    (22, 300): (
        (22, 300, 1), (22, 150, 1), (25, 150, 1), (25, 100, 1), (28, 100, 1),
        (32, 100, 1), (36, 100, 1), (32, 100, 2), (36, 100, 2),
    ),
    # т. 2.4.3 — фундаментная плита t=1400-1500
    (25, 300): (
        (25, 300, 1), (25, 150, 1), (28, 100, 1), (32, 100, 1),
        (36, 100, 1), (32, 100, 2), (36, 100, 2),
    ),
}

# Appended to every ladder so that scales going above the table still resolve.
LADDER_TAIL: tuple[tuple[int, int, int], ...] = (
    (28, 100, 1), (32, 100, 1), (36, 100, 1), (32, 100, 2), (36, 100, 2),
)


@dataclass
class Background:
    """Фоновое (basic) reinforcement of the slab."""

    diameter: int
    step: int
    as_cm2_m: float

    @property
    def label(self) -> str:
        return f"s{self.step}d{self.diameter}"


def resolve_background(as_cm2_m: float) -> Background:
    """Pick the (d, step) background mesh whose area matches ``as_cm2_m``."""
    best = min(
        LADDERS,
        key=lambda ds: abs(area_cm2_per_m(*ds) - as_cm2_m),
    )
    return Background(best[0], best[1], area_cm2_per_m(*best))


def ladder_options(background: Background) -> list[RebarOption]:
    """Ordered (ascending area) additional variants available for a background."""
    specs = LADDERS.get((background.diameter, background.step), ()) + LADDER_TAIL
    seen: set[tuple[int, int, int]] = set()
    out: list[RebarOption] = []
    for spec in specs:
        if spec in seen:
            continue
        seen.add(spec)
        out.append(RebarOption.make(*spec))
    out.sort()
    return out


@dataclass
class BandMapping:
    """A colour-scale band and the additional reinforcement it demands."""

    index: int
    color: int
    lower: float
    upper: float
    option: RebarOption | None
    provided_total: float
    rel_error: float

    @property
    def needs_additional(self) -> bool:
        return self.option is not None


def map_bands(
    bands: list[tuple[int, float, float]],
    background: Background,
    *,
    policy: str = "nearest",
    warn_rel: float = 0.02,
) -> tuple[list[BandMapping], list[str]]:
    """Map ``(color, lower, upper)`` bands onto rebar options.

    ``policy='nearest'`` picks the ladder step closest to the band's upper bound
    (the scale bounds are normally rounded ladder values); ``policy='ceil'`` picks
    the smallest option that fully covers the upper bound.
    """
    options = ladder_options(background)
    mapped: list[BandMapping] = []
    warnings: list[str] = []
    # the scale bounds are typed by hand ("8.5" for 8.48 cm²/m), so a band whose
    # upper bound is within a couple of percent of the background needs nothing
    tol = max(0.05, 0.02 * background.as_cm2_m)

    for i, (color, lower, upper) in enumerate(bands):
        if upper <= background.as_cm2_m + tol:
            mapped.append(BandMapping(i, color, lower, upper, None, background.as_cm2_m, 0.0))
            continue
        need = upper - background.as_cm2_m
        if policy == "ceil":
            covering = [o for o in options if o.as_cm2_m >= need - tol]
            option = covering[0] if covering else options[-1]
        else:
            option = min(options, key=lambda o: (abs(o.as_cm2_m - need), o.as_cm2_m))
        total = background.as_cm2_m + option.as_cm2_m
        rel = (upper - total) / upper
        mapped.append(BandMapping(i, color, lower, upper, option, total, rel))
        if rel > warn_rel:
            warnings.append(
                f"band #{i} (color {color}, upper {upper:g} cm²/m) resolved to "
                f"{background.label}+{option.label} = {total:.1f} cm²/m — "
                f"{rel * 100:.1f}% below the band bound; check the scale or use policy='ceil'"
            )
        elif rel < -warn_rel:
            warnings.append(
                f"band #{i} (color {color}, upper {upper:g} cm²/m) resolved to "
                f"{background.label}+{option.label} = {total:.1f} cm²/m — "
                f"{-rel * 100:.1f}% above the band bound (rounded up)"
            )
    return mapped, warnings


def load_overrides(path: str | Path) -> dict[int, tuple[int, int, int] | None]:
    """Read a JSON ``{"<color>": [d, step, layers] | null}`` override file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[int, tuple[int, int, int] | None] = {}
    for key, val in data.items():
        if val is None:
            out[int(key)] = None
        else:
            d, step, *rest = val
            out[int(key)] = (int(d), int(step), int(rest[0]) if rest else 1)
    return out


def apply_overrides(
    mapped: list[BandMapping], overrides: dict[int, tuple[int, int, int] | None]
) -> list[BandMapping]:
    out = []
    for band in mapped:
        if band.color in overrides:
            spec = overrides[band.color]
            option = RebarOption.make(*spec) if spec else None
            out.append(
                BandMapping(
                    band.index, band.color, band.lower, band.upper, option,
                    band.provided_total, band.rel_error,
                )
            )
        else:
            out.append(band)
    return out
