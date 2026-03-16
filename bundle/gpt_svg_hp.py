#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import vpype


def install_vpype_numpy_compat_patch() -> None:
    """Patch vpype's SVG importer for NumPy 2.x object-array compatibility."""

    import svgelements
    import vpype.io as vpype_io
    from shapely.geometry import LineString

    if getattr(vpype_io, "_svgen_numpy_compat_patch", False):
        return

    def _coerce_point_rows(points) -> np.ndarray:
        if isinstance(points, np.ndarray):
            if np.issubdtype(points.dtype, np.complexfloating):
                return np.column_stack((points.real, points.imag)).astype(float, copy=False)
            if points.ndim == 2 and points.shape[1] == 2:
                return np.ascontiguousarray(points, dtype=float)
            if points.ndim == 1 and points.size % 2 == 0 and np.issubdtype(
                points.dtype, np.number
            ):
                return np.ascontiguousarray(points.reshape(-1, 2), dtype=float)

        if hasattr(points, "coords"):
            points = points.coords

        rows: list[tuple[float, float]] = []
        for point in points:
            if isinstance(point, complex):
                rows.append((float(point.real), float(point.imag)))
            elif hasattr(point, "x") and hasattr(point, "y"):
                rows.append((float(point.x), float(point.y)))
            else:
                x, y = point
                rows.append((float(x), float(y)))

        return np.ascontiguousarray(rows, dtype=float)

    def _points_to_complex(points) -> np.ndarray:
        if isinstance(points, np.ndarray) and np.issubdtype(points.dtype, np.complexfloating):
            return np.ascontiguousarray(points, dtype=complex).reshape(-1)

        rows = _coerce_point_rows(points)
        if len(rows) == 0:
            return np.empty(0, dtype=complex)
        return rows.view(dtype=complex).reshape(len(rows))

    def _flattened_paths_to_line_collection(
        paths,
        quantization: float,
        simplify: bool,
        parallel: bool,
        metadata=None,
    ):
        def _process_path(path):
            if len(path) == 0:
                return []

            result = []
            point_stack = vpype_io._ComplexStack()
            for seg in path:
                if isinstance(seg, svgelements.Arc) and (seg.rx == 0 or seg.ry == 0):
                    seg = svgelements.Line(start=seg.start, end=seg.end)

                if isinstance(seg, svgelements.Move):
                    if len(point_stack) > 0:
                        result.append(point_stack.get())
                        point_stack = vpype_io._ComplexStack()

                    point_stack.append(complex(seg.end))
                elif isinstance(seg, (svgelements.Line, svgelements.Close)):
                    start = complex(seg.start)
                    end = complex(seg.end)
                    if not point_stack.ends_with(start):
                        point_stack.append(start)
                    if end != start:
                        point_stack.append(end)
                elif isinstance(seg, (svgelements.Polygon, svgelements.Polyline)):
                    line = _points_to_complex(seg.points)
                    if len(line) == 0:
                        continue
                    if point_stack.ends_with(line[0]):
                        point_stack.extend(line[1:])
                    else:
                        point_stack.extend(line)
                else:
                    step = max(2, int(math.ceil(seg.length() / quantization)))
                    line = seg.npoint(np.linspace(0, 1, step))

                    if simplify:
                        line = LineString(_coerce_point_rows(line)).simplify(
                            tolerance=quantization
                        )

                    line = _points_to_complex(line)
                    if len(line) == 0:
                        continue
                    if point_stack.ends_with(line[0]):
                        point_stack.extend(line[1:])
                    else:
                        point_stack.extend(line)

            if len(point_stack) > 0:
                result.append(point_stack.get())

            return result

        if parallel:
            with vpype_io.Pool() as pool:
                results = pool.map(_process_path, paths)
        else:
            results = map(_process_path, paths)

        lc = vpype_io.LineCollection(metadata=metadata)
        for res in results:
            lc.extend(res)
        return lc

    vpype_io._flattened_paths_to_line_collection = _flattened_paths_to_line_collection
    vpype_io._svgen_numpy_compat_patch = True


def mm_to_hpgl_units(mm: float) -> int:
    return int(round(mm * 40.0))


# ── SVG subpath counting ───────────────────────────────────────────────────────
# vpype splits a multi-subpath <path d="M…Z M…Z"> into N separate lines (one per
# M command), losing the original element grouping.  extract_subpath_groups() reads
# the raw SVG to recover those counts so write_annotated_hpgl can re-group the
# vpype lines into a single PM0/PM2 polygon block — required for correct even-odd
# fill rendering (e.g. circle icons with an inner "hole").

_SHAPE_TAGS: frozenset[str] = frozenset(
    {"path", "circle", "ellipse", "rect", "line", "polyline", "polygon"}
)
_M_RE = re.compile(r"[Mm]")


def _count_subpaths(d: str) -> int:
    """Number of subpaths in an SVG 'd' attribute (one per M/m command)."""
    return max(1, len(_M_RE.findall(d)))


def extract_subpath_groups(svg_path: str | Path) -> list[int]:
    """Return subpath-count per SVG shape element in document order.

    Non-path shapes (rect, circle, etc.) always contribute 1.
    A <path d="M…M…"> with N move commands contributes N because vpype
    emits one line per subpath.  The returned list drives polygon grouping
    in write_annotated_hpgl.
    """
    from xml.etree import ElementTree as ET

    counts: list[int] = []

    def _walk(elem: ET.Element) -> None:
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag in _SHAPE_TAGS:
            counts.append(
                _count_subpaths(elem.get("d", "")) if tag == "path" else 1
            )
        for child in elem:
            _walk(child)

    _walk(ET.parse(str(svg_path)).getroot())
    return counts


# ── Pen colour table ───────────────────────────────────────────────────────────
# Standard plotter pen colours.  Per-layer colours resolved by vpype override these
# when writing the CO "PEN:N=…" table that HPGL→SVG converters read back.

_DEFAULT_PEN_COLORS: dict[int, str] = {
    1: "#000000",
    2: "#ff0000",
    3: "#00aa00",
    4: "#0000ff",
    5: "#ff8800",
    6: "#aa00aa",
    7: "#00aaaa",
    8: "#888888",
}


def _extract_layer_color(lc) -> str | None:
    """Pull hex fill/stroke colour from a vpype LineCollection (best-effort)."""
    try:
        c = lc.property("vp_color")
        if c is None:
            return None
        if isinstance(c, int):                               # packed RGBA int32 (R=MSB)
            r, g, b = (c >> 24) & 0xFF, (c >> 16) & 0xFF, (c >> 8) & 0xFF
            return f"#{r:02x}{g:02x}{b:02x}"
        if hasattr(c, "red"):                                # float-component object
            return f"#{int(c.red*255):02x}{int(c.green*255):02x}{int(c.blue*255):02x}"
        s = str(c)
        return s if s.startswith("#") else None
    except Exception:
        return None


def hpgl_escape_comment(text: str) -> str:
    return text.replace(";", ",").replace("\n", " ").strip()


def complex_path_to_points(path: Iterable[complex]) -> list[tuple[int, int]]:
    pts: list[tuple[int, int]] = []
    for p in path:
        pts.append((mm_to_hpgl_units(p.real), mm_to_hpgl_units(p.imag)))
    return pts


def point_line_distance(p: tuple[int, int], a: tuple[int, int], b: tuple[int, int]) -> float:
    px, py = p
    ax, ay = a
    bx, by = b

    if a == b:
        return math.hypot(px - ax, py - ay)

    dx = bx - ax
    dy = by - ay
    t = ((px - ax) * dx + (py - ay) * dy) / float(dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))

    proj_x = ax + t * dx
    proj_y = ay + t * dy
    return math.hypot(px - proj_x, py - proj_y)


def rdp(points: list[tuple[int, int]], epsilon: float) -> list[tuple[int, int]]:
    if len(points) <= 2:
        return points[:]

    start = points[0]
    end = points[-1]

    max_dist = -1.0
    index = -1
    for i in range(1, len(points) - 1):
        d = point_line_distance(points[i], start, end)
        if d > max_dist:
            max_dist = d
            index = i

    if max_dist > epsilon:
        left = rdp(points[: index + 1], epsilon)
        right = rdp(points[index:], epsilon)
        return left[:-1] + right
    else:
        return [start, end]


def is_closed_path(points: list[tuple[int, int]], close_tolerance: float = 1.0) -> bool:
    if len(points) < 3:
        return False
    a = points[0]
    b = points[-1]
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= close_tolerance


def simplify_points(points: list[tuple[int, int]], epsilon: float) -> list[tuple[int, int]]:
    if len(points) <= 2 or epsilon <= 0:
        return points[:]

    closed = is_closed_path(points)

    if not closed:
        return rdp(points, epsilon)

    core = points[:-1] if points[0] == points[-1] else points[:]
    if len(core) <= 2:
        return points[:]

    seam_idx = min(range(len(core)), key=lambda i: (core[i][0], core[i][1]))
    rotated = core[seam_idx:] + core[:seam_idx] + [core[seam_idx]]

    simplified = rdp(rotated, epsilon)

    if simplified[0] != simplified[-1]:
        simplified.append(simplified[0])

    if len(simplified) < 4:
        return points[:]

    return simplified


def chunk_points(points: list[tuple[int, int]], chunk_size: int) -> list[list[tuple[int, int]]]:
    if chunk_size <= 0:
        return [points]
    return [points[i : i + chunk_size] for i in range(0, len(points), chunk_size)]


def emit_pr_commands(points: list[tuple[int, int]], start: tuple[int, int], max_points_per_pd: int) -> list[str]:
    """Emit PD; (pen down at current pos) then PR{dx,dy,...}; chunks using relative deltas."""
    if not points:
        return []

    prev = start
    deltas: list[tuple[int, int]] = []
    for x, y in points:
        deltas.append((x - prev[0], y - prev[1]))
        prev = (x, y)

    cmds = ["PD;"]
    for chunk in chunk_points(deltas, max_points_per_pd):
        cmds.append("PR" + ",".join(f"{dx},{dy}" for dx, dy in chunk) + ";")
    return cmds


def write_annotated_hpgl(
    doc: vpype.Document,
    output_path: str | Path,
    include_layer_comments: bool = True,
    include_path_comments: bool = True,
    pen_by_layer: bool = True,
    velocity: Optional[int] = None,
    simplify_tolerance: float = 8.0,
    max_points_per_pd: int = 40,
    subpath_groups: Optional[list[int]] = None,
    pen_colors: Optional[dict[int, str]] = None,
):
    # ── Pen colour table ──────────────────────────────────────────────────────
    effective_pens: dict[int, str] = dict(_DEFAULT_PEN_COLORS)
    if pen_colors:
        effective_pens.update(pen_colors)

    out: list[str] = []

    # CO "PEN:N=…" annotations are read back by HPGL→SVG converters for colour fidelity.
    for pen_num, color in sorted(effective_pens.items()):
        out.append(f'CO "PEN:{pen_num}={color}";')

    out.append("IN;")
    out.append("DF;")

    if velocity is not None:
        out.append(f"VS{int(velocity)};")

    # ── Validate subpath groups ───────────────────────────────────────────────
    # subpath_groups[i] = how many consecutive vpype lines came from SVG element i.
    # Lines in the same group land in one PM0/PM2 block so that multi-subpath paths
    # (e.g. a circle icon with an inner hole) render with correct even-odd fill.
    # Fall back to one group per line when counts don't align (multi-layer SVGs).
    layer_ids = sorted(doc.ids())
    total_lines = sum(len(list(doc[lid])) for lid in layer_ids)
    groups_flat = (
        subpath_groups
        if subpath_groups and sum(subpath_groups) == total_lines
        else [1] * total_lines
    )
    group_iter = iter(groups_flat)

    for layer_id in layer_ids:
        lc = doc[layer_id]

        layer_name = None
        try:
            layer_name = lc.property("vp_name")
        except Exception:
            pass

        if pen_by_layer:
            out.append(f"SP{int(layer_id)};")

        if include_layer_comments:
            label = f"layer {layer_id}" + (f" ({layer_name})" if layer_name else "")
            out.append(f"CO {hpgl_escape_comment(label)};")

        lines_list = list(lc)
        line_idx = 0

        while line_idx < len(lines_list):
            # Consume the next group: N consecutive vpype lines from one SVG element.
            n = next(group_iter, 1)
            group_lines = lines_list[line_idx : line_idx + n]
            line_idx += n

            # Build raw point lists, then simplify.
            raw_group = [complex_path_to_points(ln) for ln in group_lines]
            raw_group = [p for p in raw_group if p]
            if not raw_group:
                continue

            orig_total = sum(len(p) for p in raw_group)
            simp_group = [simplify_points(pts, simplify_tolerance) for pts in raw_group]
            simp_total = sum(len(p) for p in simp_group)

            if include_path_comments:
                label = f"layer {layer_id} path {line_idx - n + 1}"
                if n > 1:
                    label += f"-{line_idx}"
                out.append(f"CO {hpgl_escape_comment(label + f' points={orig_total}->{simp_total}')};")

            any_closed = any(is_closed_path(pts) for pts in simp_group)

            if any_closed:
                # ── Polygon fill block ────────────────────────────────────────
                # PM0 begins the polygon at the current pen position.
                # PM1 closes the current subpath and opens a new one — needed for
                # multi-subpath paths (outer ring + inner hole) so ezdxf applies
                # even-odd fill and produces the correct donut/cutout shape.
                # FP fills the polygon; EP strokes its outline.
                x0, y0 = simp_group[0][0]
                out.append(f"PU{x0},{y0};")
                out.append("PM0;")
                for i, pts in enumerate(simp_group):
                    if i > 0:
                        out.append("PM1;")
                        sx, sy = pts[0]
                        out.append(f"PU{sx},{sy};")
                    if len(pts) > 1:
                        out.extend(emit_pr_commands(pts[1:], pts[0], max_points_per_pd))
                out.append("PM2;")
                out.append("FP;")
                out.append("EP;")
                out.append("PU;")
            else:
                # ── Open / stroked path ───────────────────────────────────────
                for pts in simp_group:
                    x0, y0 = pts[0]
                    out.append(f"PU{x0},{y0};")
                    if len(pts) > 1:
                        out.extend(emit_pr_commands(pts[1:], pts[0], max_points_per_pd))
                out.append("PU;")

    out.append("SP0;")
    out.append("IN;")

    Path(output_path).write_text("\n".join(out) + "\n", encoding="ascii")


def convert_svg_to_hpgl(
    input_svg: str | Path,
    output_hpgl: str | Path,
    quantization: float,
    no_import_simplify: bool,
    simplify_tolerance: float,
    max_points_per_pd: int,
    velocity: Optional[int],
    no_layer_comments: bool,
    no_path_comments: bool,
    single_pen: bool,
) -> None:
    install_vpype_numpy_compat_patch()

    # Read subpath groups before vpype, which splits multi-subpath <path> elements
    # into separate lines and loses the original element boundary information.
    subpath_groups = extract_subpath_groups(input_svg)

    doc = vpype.read_multilayer_svg(
        str(input_svg),
        quantization=quantization,
        simplify=not no_import_simplify,
    )

    # Collect per-layer colours resolved by vpype (e.g. currentColor → #000000).
    pen_colors = {
        int(lid): color
        for lid in doc.ids()
        if (color := _extract_layer_color(doc[lid])) is not None
    }

    write_annotated_hpgl(
        doc=doc,
        output_path=output_hpgl,
        include_layer_comments=not no_layer_comments,
        include_path_comments=not no_path_comments,
        pen_by_layer=not single_pen,
        velocity=velocity,
        simplify_tolerance=simplify_tolerance,
        max_points_per_pd=max_points_per_pd,
        subpath_groups=subpath_groups,
        pen_colors=pen_colors,
    )


def _try_svgen_hpgl_to_svg(input_hpgl: str | Path, output_svg: str | Path) -> bool:
    """Parse the annotated HPGL format produced by write_annotated_hpgl and emit SVG.

    Understands the exact command set we generate:
      PU / PD  with inline coord lists
      PM0 / PM1 / PM2 / FP / EP  — polygon-fill blocks
      SP  — pen select
      CO "PEN:N=#rrggbb"  — colour table annotations

    HPGL units are used as-is; the SVG viewBox is derived from the content bbox.
    Filled shapes use fill-rule="evenodd" so multi-subpath paths (e.g. a circle
    with an inner hole) render correctly without needing winding-direction tricks.
    """
    from xml.etree import ElementTree as ET

    hpgl_text = Path(input_hpgl).read_text(encoding="utf-8", errors="replace")

    # ── Read CO "PEN:N=…" colour table ────────────────────────────────────────
    pen_colors: dict[int, str] = dict(_DEFAULT_PEN_COLORS)
    for m in re.finditer(r'CO\s*"PEN:(\d+)=([^"]+)"', hpgl_text, re.IGNORECASE):
        pen_colors[int(m.group(1))] = m.group(2).strip()

    # ── Tokenise: 2-letter command + args until ";" ────────────────────────────
    tokens = re.findall(r'([A-Za-z]{2})([^;]*);?', hpgl_text)

    # ── Parser state ──────────────────────────────────────────────────────────
    cx = cy = 0.0
    pen_num = 1
    pen_down = False
    current_stroke: list[tuple[float, float]] = []

    # Polygon mode — one list per subpath (PM0 starts first, PM1 opens each next)
    in_polygon = False
    polygon_subpaths: list[list[tuple[float, float]]] = []
    polygon_pen = 1
    pending_fill = False

    # Completed shapes: (pen, subpaths, filled)
    shapes: list[tuple[int, list[list[tuple[float, float]]], bool]] = []

    def flush_stroke() -> None:
        nonlocal current_stroke
        if len(current_stroke) > 1:
            shapes.append((pen_num, [list(current_stroke)], False))
        current_stroke = []

    def flush_polygon(filled: bool) -> None:
        nonlocal polygon_subpaths, pending_fill
        valid = [sp for sp in polygon_subpaths if len(sp) > 1]
        if valid:
            shapes.append((polygon_pen, valid, filled))
        polygon_subpaths = []
        pending_fill = False

    for cmd_raw, args_raw in tokens:
        cmd = cmd_raw.upper()
        coords = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", args_raw)]

        if cmd in ("IN", "DF", "VS", "LT", "CO", "PG"):
            pass  # init / velocity / comment / page-feed — ignored

        elif cmd == "SP":
            if not in_polygon:
                flush_stroke()
            n = int(coords[0]) if coords else 0
            if n > 0:
                pen_num = n

        elif cmd == "PU":
            if in_polygon:
                # Within polygon mode PU repositions without drawing a line.
                # If the current subpath is empty (right after PM1), this
                # coordinate becomes its first vertex.
                if coords and len(coords) >= 2:
                    cx, cy = coords[0], coords[1]
                    if polygon_subpaths and not polygon_subpaths[-1]:
                        polygon_subpaths[-1].append((cx, cy))
            else:
                flush_stroke()
                pen_down = False
                if coords and len(coords) >= 2:
                    cx, cy = coords[0], coords[1]

        elif cmd == "PD":
            if in_polygon:
                for i in range(0, len(coords) - 1, 2):
                    cx, cy = coords[i], coords[i + 1]
                    if polygon_subpaths:
                        polygon_subpaths[-1].append((cx, cy))
            else:
                if not pen_down:
                    current_stroke = [(cx, cy)]
                pen_down = True
                for i in range(0, len(coords) - 1, 2):
                    cx, cy = coords[i], coords[i + 1]
                    current_stroke.append((cx, cy))

        elif cmd in ("PA", "PR"):
            relative = cmd == "PR"
            for i in range(0, len(coords) - 1, 2):
                if relative:
                    cx += coords[i]
                    cy += coords[i + 1]
                else:
                    cx, cy = coords[i], coords[i + 1]
                if in_polygon and polygon_subpaths:
                    polygon_subpaths[-1].append((cx, cy))
                elif pen_down:
                    current_stroke.append((cx, cy))

        elif cmd == "PM":
            n = int(coords[0]) if coords else 0
            if n == 0:
                # Begin polygon at current pen position
                flush_stroke()
                in_polygon = True
                polygon_subpaths = [[(cx, cy)]]
                polygon_pen = pen_num
                pending_fill = False
            elif n == 1:
                # Close current subpath, open a new empty one at current position
                polygon_subpaths.append([])
            elif n == 2:
                # End polygon mode (FP/EP commands follow)
                in_polygon = False

        elif cmd == "FP":
            pending_fill = True

        elif cmd == "EP":
            flush_polygon(filled=pending_fill)

    flush_stroke()  # flush any trailing open path

    if not shapes:
        return False

    # ── Bounding box → viewBox ─────────────────────────────────────────────────
    all_x = [x for _, sps, _ in shapes for sp in sps for x, _ in sp]
    all_y = [y for _, sps, _ in shapes for sp in sps for _, y in sp]
    if not all_x:
        return False

    min_x, max_x = min(all_x), max(all_x)
    min_y, max_y = min(all_y), max(all_y)
    pad = max(max_x - min_x, max_y - min_y) * 0.02
    w = max_x - min_x + pad * 2
    h = max_y - min_y + pad * 2
    stroke_w = max(2.0, (max_x - min_x) * 0.003)

    # ── Build SVG ──────────────────────────────────────────────────────────────
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    svg = ET.Element("svg", {
        "xmlns": "http://www.w3.org/2000/svg",
        "width": f"{w:.1f}",
        "height": f"{h:.1f}",
        "viewBox": f"{min_x - pad:.1f} {min_y - pad:.1f} {w:.1f} {h:.1f}",
    })

    for pen, subpaths, filled in shapes:
        color = pen_colors.get(pen, "#000000")
        if filled:
            # Combine subpaths into one <path> with even-odd fill so that inner
            # subpaths (e.g. the ring of a digit, a donut hole) become transparent.
            d = " ".join(
                "M " + " ".join(f"{x:.1f},{y:.1f}" for x, y in sp) + " Z"
                for sp in subpaths if len(sp) > 1
            )
            if d:
                ET.SubElement(svg, "path", {
                    "d": d,
                    "fill": color,
                    "fill-rule": "evenodd",
                    "stroke": color,
                    "stroke-width": f"{stroke_w:.1f}",
                    "stroke-linejoin": "round",
                })
        else:
            for sp in subpaths:
                if len(sp) > 1:
                    ET.SubElement(svg, "polyline", {
                        "points": " ".join(f"{x:.1f},{y:.1f}" for x, y in sp),
                        "fill": "none",
                        "stroke": color,
                        "stroke-width": f"{stroke_w:.1f}",
                        "stroke-linecap": "round",
                        "stroke-linejoin": "round",
                    })

    ET.indent(svg, space="  ")
    Path(output_svg).write_text(
        ET.tostring(svg, encoding="unicode", xml_declaration=False),
        encoding="utf-8",
    )
    return True


def _try_ezdxf_hpgl2_python_api(input_hpgl: str | Path, output_svg: str | Path) -> bool:
    """
    Best-effort support for multiple ezdxf hpgl2 API layouts across versions.
    """
    candidates = [
        ("ezdxf.addons.hpgl2.api", "to_svg"),
        ("ezdxf.addons.hpgl2.api", "hpgl2_to_svg"),
        ("ezdxf.addons.hpgl2", "to_svg"),
        ("ezdxf.addons.hpgl2", "hpgl2_to_svg"),
    ]

    for module_name, func_name in candidates:
        try:
            module = importlib.import_module(module_name)
            func = getattr(module, func_name, None)
            if func is None:
                continue

            result = func(str(input_hpgl))

            # Possible return types across versions/helpers:
            # - SVG string
            # - object with .tostring()
            # - object with .write()
            if isinstance(result, str):
                Path(output_svg).write_text(result, encoding="utf-8")
                return True

            if hasattr(result, "tostring"):
                svg_text = result.tostring()
                Path(output_svg).write_text(svg_text, encoding="utf-8")
                return True

            if hasattr(result, "write"):
                with open(output_svg, "w", encoding="utf-8") as f:
                    result.write(f)
                return True

        except Exception:
            continue

    return False


def _try_ezdxf_hpgl2_cli(input_hpgl: str | Path, output_svg: str | Path) -> bool:
    """
    Fallback to ezdxf CLI. We try a few argument variants because they differ across releases.
    """
    cli_candidates = []

    ezdxf_bin = shutil.which("ezdxf")
    if ezdxf_bin:
        cli_candidates.extend(
            [
                [ezdxf_bin, "hpgl", str(input_hpgl), "--export", "svg", "--output", str(output_svg)],
                [ezdxf_bin, "hpgl", str(input_hpgl), "--export", "SVG", "--output", str(output_svg)],
                [ezdxf_bin, "hpgl", str(input_hpgl), "-f", "svg", "-o", str(output_svg)],
            ]
        )

    cli_candidates.extend(
        [
            [sys.executable, "-m", "ezdxf", "hpgl", str(input_hpgl), "--export", "svg", "--output", str(output_svg)],
            [sys.executable, "-m", "ezdxf", "hpgl", str(input_hpgl), "--export", "SVG", "--output", str(output_svg)],
            [sys.executable, "-m", "ezdxf", "hpgl", str(input_hpgl), "-f", "svg", "-o", str(output_svg)],
        ]
    )

    for cmd in cli_candidates:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode == 0 and Path(output_svg).exists():
                return True
        except Exception:
            continue

    return False


def _try_local_hpgl_parser(input_hpgl: str | Path, output_svg: str | Path) -> bool:
    """Fallback to the repo's local HPGL parser when ezdxf hpgl2 isn't available."""

    try:
        local_svg_hpgl = importlib.import_module("svg_hpgl")
        hpgl_to_svg = getattr(local_svg_hpgl, "hpgl_to_svg", None)
        if hpgl_to_svg is None:
            return False

        hpgl_text = Path(input_hpgl).read_text(encoding="utf-8", errors="replace")
        svg_text = hpgl_to_svg(hpgl_text)
        Path(output_svg).write_text(svg_text, encoding="utf-8")
        return True
    except Exception:
        return False


def _scale_svg_output(
    svg_path: str | Path,
    max_width: Optional[float],
    max_height: Optional[float],
) -> None:
    """Rewrite the width/height attributes of an SVG to fit within the given constraint.

    Exactly one of max_width or max_height must be provided.  The other dimension
    is derived from the existing width/height so the aspect ratio is preserved.
    The viewBox is left unchanged so the content coordinates remain valid.
    """
    if max_width is None and max_height is None:
        return

    from xml.etree import ElementTree as ET

    tree = ET.parse(str(svg_path))
    root = tree.getroot()

    ns = ""
    tag = root.tag
    if tag.startswith("{"):
        ns = tag[: tag.index("}") + 1]

    w_attr = root.get("width", "")
    h_attr = root.get("height", "")

    def _parse_dim(s: str) -> Optional[float]:
        m = re.match(r"([\d.]+)", s.strip())
        return float(m.group(1)) if m else None

    orig_w = _parse_dim(w_attr)
    orig_h = _parse_dim(h_attr)

    # Fall back to viewBox dimensions when explicit width/height are absent.
    if orig_w is None or orig_h is None:
        vb = root.get("viewBox", "")
        parts = re.findall(r"[\d.]+", vb)
        if len(parts) == 4:
            orig_w = float(parts[2])
            orig_h = float(parts[3])

    if orig_w is None or orig_h is None or orig_w == 0 or orig_h == 0:
        return  # cannot determine current dimensions

    if max_width is not None:
        scale = max_width / orig_w
    else:
        scale = max_height / orig_h  # type: ignore[operator]

    new_w = orig_w * scale
    new_h = orig_h * scale

    root.set("width", f"{new_w:.1f}")
    root.set("height", f"{new_h:.1f}")

    ET.register_namespace("", "http://www.w3.org/2000/svg")
    tree.write(str(svg_path), encoding="unicode", xml_declaration=False)


def convert_hpgl_to_svg(
    input_hpgl: str | Path,
    output_svg: str | Path,
    max_width: Optional[float] = None,
    max_height: Optional[float] = None,
) -> None:
    # Try our own parser first — it understands the exact format we generate.
    if _try_svgen_hpgl_to_svg(input_hpgl, output_svg):
        _scale_svg_output(output_svg, max_width, max_height)
        return

    if _try_ezdxf_hpgl2_python_api(input_hpgl, output_svg):
        _scale_svg_output(output_svg, max_width, max_height)
        return

    if _try_ezdxf_hpgl2_cli(input_hpgl, output_svg):
        _scale_svg_output(output_svg, max_width, max_height)
        return

    if _try_local_hpgl_parser(input_hpgl, output_svg):
        _scale_svg_output(output_svg, max_width, max_height)
        return

    raise RuntimeError(
        "HPGL -> SVG conversion failed. Install a recent ezdxf with hpgl2 support, "
        "or ensure the local svg_hpgl.py fallback remains available."
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert SVG<->HPGL: SVG->HPGL uses vpype, HPGL->SVG uses ezdxf hpgl2."
    )
    parser.add_argument("input_path", help="Input file path (.svg or .hp)")
    parser.add_argument("output_path", help="Output file path (.hp or .svg)")

    parser.add_argument("--quantization", type=float, default=1.0)
    parser.add_argument("--no-import-simplify", action="store_true")
    parser.add_argument("--simplify", type=float, default=8.0)
    parser.add_argument("--max-points-per-pd", type=int, default=40)
    parser.add_argument("--velocity", type=int, default=None)
    parser.add_argument("--no-layer-comments", action="store_true")
    parser.add_argument("--no-path-comments", action="store_true")
    parser.add_argument("--single-pen", action="store_true")

    scale_group = parser.add_mutually_exclusive_group()
    scale_group.add_argument(
        "--max-width", "-w", type=float, default=None, metavar="PX",
        help="Scale the output SVG so its width is at most PX (HPGL->SVG only, aspect ratio preserved).",
    )
    scale_group.add_argument(
        "--max-height", "-H", type=float, default=None, metavar="PX",
        help="Scale the output SVG so its height is at most PX (HPGL->SVG only, aspect ratio preserved).",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    input_path = Path(args.input_path)
    output_path = Path(args.output_path)

    input_ext = input_path.suffix.lower()
    output_ext = output_path.suffix.lower()

    if input_ext == ".svg" and output_ext == ".hp":
        convert_svg_to_hpgl(
            input_svg=input_path,
            output_hpgl=output_path,
            quantization=args.quantization,
            no_import_simplify=args.no_import_simplify,
            simplify_tolerance=args.simplify,
            max_points_per_pd=args.max_points_per_pd,
            velocity=args.velocity,
            no_layer_comments=args.no_layer_comments,
            no_path_comments=args.no_path_comments,
            single_pen=args.single_pen,
        )
        print(f"Wrote {output_path}")
        return

    if input_ext == ".hp" and output_ext == ".svg":
        convert_hpgl_to_svg(input_path, output_path,
                            max_width=args.max_width,
                            max_height=args.max_height)
        print(f"Wrote {output_path}")
        return

    raise SystemExit(
        "Unsupported conversion. Use either:\n"
        "  input.svg output.hp\n"
        "or\n"
        "  input.hp output.svg"
    )


if __name__ == "__main__":
    main()
    
