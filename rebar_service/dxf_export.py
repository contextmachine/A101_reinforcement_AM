from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping, Sequence

import ezdxf


APPID = "REBAR_OPT"
MM_TO_SOURCE_DXF = 0.001  # extract_polygons() multiplies DXF coordinates by 1000.
_EPS = 1e-9


class DxfExportError(ValueError):
    """Expected/processable DXF export error."""


@dataclass(frozen=True)
class ExportedDxf:
    content: bytes
    filename: str


def _safe_stem(filename: str | None) -> str:
    stem = Path(filename or "source.dxf").stem.strip() or "source"
    return "".join(ch for ch in stem if ch not in '<>:"/\\|?*').strip() or "source"


def _scaled_point(x: float, y: float, scale: float) -> tuple[float, float]:
    return float(x) * scale, float(y) * scale


def _rect_points(rect: Sequence[float], scale: float) -> list[tuple[float, float]]:
    x0, y0, x1, y1 = map(float, rect[:4])
    return [
        _scaled_point(x0, y0, scale),
        _scaled_point(x1, y0, scale),
        _scaled_point(x1, y1, scale),
        _scaled_point(x0, y1, scale),
    ]


def _bar4(raw: Any) -> tuple[float, float, float, float]:
    if isinstance(raw, Mapping):
        return tuple(float(raw[k]) for k in ("x0", "y0", "x1", "y1"))
    return tuple(map(float, raw[:4]))


def _class_color(cls: int) -> int:
    # Stable visible ACI color in the common 1..6 range.
    return 1 + (abs(int(cls)) * 2) % 6


def _ensure_layer(doc, name: str, *, color: int, lineweight: int = -3) -> None:
    if name not in doc.layers:
        doc.layers.add(name, color=color, lineweight=lineweight)


def _xdata(entity, *, n: int, zone: int, cls: int, kind: str, extra: Mapping[str, Any] | None = None) -> None:
    values = [
        (1000, f"N={int(n)}"),
        (1000, f"ZONE={int(zone)}"),
        (1000, f"CLASS={int(cls)}"),
        (1000, f"KIND={kind}"),
    ]
    for key, value in (extra or {}).items():
        if value is not None:
            values.append((1000, f"{key}={value}"))
    entity.set_xdata(APPID, values)


def _add_rect(msp, rect, layer: str, scale: float, *, n: int, zone: int, cls: int, kind: str):
    if not rect:
        return None
    entity = msp.add_lwpolyline(
        _rect_points(rect, scale),
        close=True,
        dxfattribs={"layer": layer},
    )
    _xdata(entity, n=n, zone=zone, cls=cls, kind=kind)
    return entity


def _add_bar(msp, raw, layer: str, scale: float, *, n: int, zone: int, cls: int, kind: str):
    x0, y0, x1, y1 = _bar4(raw)
    entity = msp.add_line(
        _scaled_point(x0, y0, scale),
        _scaled_point(x1, y1, scale),
        dxfattribs={"layer": layer},
    )
    _xdata(entity, n=n, zone=zone, cls=cls, kind=kind)
    return entity


def _anchorage_extensions(base_raw, anchored_raw) -> list[tuple[float, float, float, float]]:
    """Return only extra endpoint pieces present in anchored_raw but absent in base_raw."""
    bx0, by0, bx1, by1 = _bar4(base_raw)
    ax0, ay0, ax1, ay1 = _bar4(anchored_raw)
    out: list[tuple[float, float, float, float]] = []

    if abs(by1 - by0) >= abs(bx1 - bx0):
        x = (bx0 + bx1) / 2.0
        blo, bhi = sorted((by0, by1))
        alo, ahi = sorted((ay0, ay1))
        if alo < blo - _EPS:
            out.append((x, alo, x, blo))
        if ahi > bhi + _EPS:
            out.append((x, bhi, x, ahi))
    else:
        y = (by0 + by1) / 2.0
        blo, bhi = sorted((bx0, bx1))
        alo, ahi = sorted((ax0, ax1))
        if alo < blo - _EPS:
            out.append((alo, y, blo, y))
        if ahi > bhi + _EPS:
            out.append((bhi, y, ahi, y))
    return out


def _zone_label(n: int, zone_index: int, cls: int, zone: Mapping[str, Any]) -> str:
    d = zone.get("diameter")
    step = zone.get("step")
    width = zone.get("width")
    length = zone.get("length")
    mass = zone.get("zone mass without anchorage")
    anchored_mass = zone.get("zone mass with anchorage")

    parts = [f"N={n}", f"ZONE={zone_index}", f"CLASS={cls}"]
    if d is not None:
        parts.append(f"D={float(d):g}mm")
    if step is not None:
        parts.append(f"STEP={float(step):g}mm")
    if width is not None:
        parts.append(f"W={float(width):g}mm")
    if length is not None:
        parts.append(f"L={float(length):g}mm")
    if mass is not None:
        parts.append(f"M={float(mass):.2f}kg")
    if anchored_mass is not None:
        parts.append(f"M+ANCH={float(anchored_mass):.2f}kg")
    return " | ".join(parts)


def _validate_inputs(source_input: Mapping[str, Any], result: Mapping[str, Any]) -> tuple[list, list]:
    if source_input.get("kind") != "dxf":
        raise DxfExportError("DXF export доступен только для задач с исходным DXF")
    content = source_input.get("content")
    if not isinstance(content, (bytes, bytearray)) or not content:
        raise DxfExportError("В задаче отсутствуют исходные DXF bytes")

    fit = result.get("fit_result")
    summary = result.get("summary")
    if not isinstance(fit, Mapping) or not isinstance(summary, Mapping):
        raise DxfExportError("Для DXF export требуется результат после postprocess")
    fit_zones = list(fit.get("zones") or [])
    summary_zones = list(summary.get("zones") or [])
    if not fit_zones or not summary_zones:
        raise DxfExportError("Для DXF export требуется непустой postprocess result")
    if len(fit_zones) != len(summary_zones):
        raise DxfExportError("Несогласованные зоны fit_result и summary")
    return fit_zones, summary_zones


def build_solution_dxf(
    source_input: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    n: int,
    mm_to_dxf: float = MM_TO_SOURCE_DXF,
) -> ExportedDxf:
    """Overlay a postprocessed solution onto the original DXF and return new DXF bytes.

    The current DXF reader converts source coordinates to millimetres by multiplying by
    1000, therefore solution geometry is converted back with the default factor 0.001.
    The original drawing is round-tripped through ezdxf and remains the background.
    """
    fit_zones, summary_zones = _validate_inputs(source_input, result)
    content = bytes(source_input["content"])
    source_name = str(source_input.get("filename") or "source.dxf")

    with TemporaryDirectory(prefix="rebar-dxf-") as tmp:
        tmp_path = Path(tmp)
        source_path = tmp_path / "source.dxf"
        output_path = tmp_path / "solution.dxf"
        source_path.write_bytes(content)
        try:
            doc = ezdxf.readfile(source_path)
        except Exception as exc:
            raise DxfExportError(f"Не удалось открыть исходный DXF: {exc}") from exc

        if APPID not in doc.appids:
            doc.appids.add(APPID)
        _ensure_layer(doc, "RB_LABEL", color=7)
        msp = doc.modelspace()

        for zone_index, (fit_zone, zone) in enumerate(zip(fit_zones, summary_zones)):
            try:
                cls = int(fit_zone["class"])
            except Exception as exc:
                raise DxfExportError(f"У зоны {zone_index} отсутствует class") from exc

            color = _class_color(cls)
            layers = {
                "primary": f"RB_C{cls}_PRIMARY",
                "zone": f"RB_C{cls}_ZONE",
                "zone_anch": f"RB_C{cls}_ZONE_ANCH",
                "bar": f"RB_C{cls}_BAR",
                "anch": f"RB_C{cls}_ANCH",
            }
            _ensure_layer(doc, layers["primary"], color=8)
            _ensure_layer(doc, layers["zone"], color=color)
            _ensure_layer(doc, layers["zone_anch"], color=2)
            _ensure_layer(doc, layers["bar"], color=color)
            _ensure_layer(doc, layers["anch"], color=1)

            _add_rect(
                msp, zone.get("primary rectangle"), layers["primary"], mm_to_dxf,
                n=n, zone=zone_index, cls=cls, kind="PRIMARY",
            )
            final_rect = zone.get("final rectangle") or fit_zone.get("bounds")
            _add_rect(
                msp, final_rect, layers["zone"], mm_to_dxf,
                n=n, zone=zone_index, cls=cls, kind="ZONE",
            )
            _add_rect(
                msp, zone.get("final rectangle with anchorage"), layers["zone_anch"], mm_to_dxf,
                n=n, zone=zone_index, cls=cls, kind="ZONE_ANCH",
            )

            bars = list(zone.get("bars") or [])
            anchored_bars = list(zone.get("bars with anchorage") or [])
            if anchored_bars and len(anchored_bars) != len(bars):
                raise DxfExportError(f"У зоны {zone_index} различается число bars и bars with anchorage")

            for bar_index, bar in enumerate(bars):
                entity = _add_bar(
                    msp, bar, layers["bar"], mm_to_dxf,
                    n=n, zone=zone_index, cls=cls, kind="BAR",
                )
                # Add bar index without changing the common XDATA contract.
                entity.set_xdata(APPID, entity.get_xdata(APPID) + [(1000, f"BAR={bar_index}")])

                if anchored_bars:
                    for extension in _anchorage_extensions(bar, anchored_bars[bar_index]):
                        anch = _add_bar(
                            msp, extension, layers["anch"], mm_to_dxf,
                            n=n, zone=zone_index, cls=cls, kind="ANCH",
                        )
                        anch.set_xdata(APPID, anch.get_xdata(APPID) + [(1000, f"BAR={bar_index}")])

            if final_rect:
                x0, y0, x1, y1 = map(float, final_rect[:4])
                width_dxf = abs(x1 - x0) * mm_to_dxf
                height_dxf = abs(y1 - y0) * mm_to_dxf
                text_height = max(0.03, min(0.15, max(width_dxf, height_dxf) * 0.025))
                label = msp.add_text(
                    _zone_label(int(n), zone_index, cls, zone),
                    height=text_height,
                    dxfattribs={"layer": "RB_LABEL"},
                )
                label.set_placement(_scaled_point(x0, y1 + 150.0, mm_to_dxf))
                _xdata(
                    label,
                    n=n,
                    zone=zone_index,
                    cls=cls,
                    kind="LABEL",
                    extra={"DIAMETER": zone.get("diameter"), "STEP": zone.get("step")},
                )

        try:
            doc.saveas(output_path)
        except Exception as exc:
            raise DxfExportError(f"Не удалось сохранить DXF: {exc}") from exc

        filename = f"{_safe_stem(source_name)}_N{int(n)}_rebar.dxf"
        return ExportedDxf(content=output_path.read_bytes(), filename=filename)
