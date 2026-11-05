"""Visual-container and filter-configuration comparison.

This module compares only what the dashboard comparator is meant to validate:

* visual containers, by screenshot-identified name/type and layout;
* filter configurations, by name, control type, options, selection and mode.

It never reads chart labels, headers, cell values or KPI numbers.
"""

from __future__ import annotations

import math
import re
from typing import Any

MATCH = "MATCH"
DIFFERENT = "DIFFERENT"
UNVERIFIED = "UNVERIFIED"
INFO = "INFO"


def filter_selection_result(
    selected_a: Any,
    selected_b: Any,
) -> tuple[str, str]:
    value_a = "" if selected_a is None else str(selected_a).strip()
    value_b = "" if selected_b is None else str(selected_b).strip()
    key_a = " ".join(value_a.split()).casefold()
    key_b = " ".join(value_b.split()).casefold()
    has_both = value_a not in {"", "-"} and value_b not in {"", "-"}
    if has_both and key_a == key_b:
        return MATCH, "Filter selection matches."
    if has_both:
        return (
            DIFFERENT,
            f"Filter selection differs: Base {value_a}; Compare {value_b}.",
        )
    return (
        UNVERIFIED,
        "Filter selection differs: "
        f"Base {value_a or 'not captured'}; "
        f"Compare {value_b or 'not captured'}.",
    )


# Relative to the combined page extent. A visual whose centre moves by more
# than this fraction of the page, or whose width/height changes by more than
# SIZE_TOLERANCE, is reported as a layout difference.
POSITION_TOLERANCE = 0.05
SIZE_TOLERANCE = 0.15

# Ordered: more specific names must come before the names they contain.
_VISUAL_TYPE_RULES: tuple[tuple[str, str], ...] = (
    (r"combo|line and (stacked|clustered) column", "Combo Chart"),
    (r"100% stacked bar", "100% Stacked Bar Chart"),
    (r"clustered bar", "Clustered Bar Chart"),
    (r"stacked bar", "Stacked Bar Chart"),
    (r"bar", "Bar Chart"),
    (r"100% stacked column", "100% Stacked Column Chart"),
    (r"clustered column", "Clustered Column Chart"),
    (r"stacked column", "Stacked Column Chart"),
    (r"column", "Column Chart"),
    (r"multi[- ]row card", "Multi-row Card"),
    (r"ribbon", "Ribbon Chart"),
    (r"donut", "Donut Chart"),
    (r"pie", "Pie Chart"),
    (r"treemap", "Treemap"),
    (r"bubble", "Bubble Chart"),
    (r"scatter", "Scatter Plot"),
    (r"waterfall", "Waterfall Chart"),
    (r"funnel", "Funnel Chart"),
    (r"gauge", "Gauge"),
    (r"area", "Area Chart"),
    (r"shape|divider|separator|horizontal line", "Shape / Line"),
    (r"line", "Line Chart"),
    (r"matrix", "Matrix"),
    (r"table", "Table"),
    (r"kpi", "KPI"),
    (r"card", "Card"),
    (r"heat ?map", "Heatmap"),
    (r"histogram", "Histogram"),
    (r"slicer|dropdown", "Slicer / Dropdown"),
    (r"text ?box", "Text Box"),
    (r"header", "Header"),
    (r"image|logo", "Image / Logo"),
    (r"button", "Button"),
    (r"filled map", "Filled Map"),
    (r"map", "Map"),
)
_COMPILED_TYPE_RULES = tuple(
    (re.compile(rf"\b{pattern}\b"), label) for pattern, label in _VISUAL_TYPE_RULES
)


def _split_identifier(text: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return re.sub(r"[-_]+", " ", text).lower()


def classify_visual_type(*hints: Any) -> str | None:
    """Return the canonical Power BI visual type from DOM hints, or None.

    Hints are DOM attribute values such as a visual's type attribute, its class
    names or its accessible name. Only the first matching rule is used, so
    more specific visual names win. Returns None when nothing matches, so the
    caller can fall back to screenshot identification or report UNVERIFIED.
    """
    for hint in hints:
        if not hint:
            continue
        normalized = _split_identifier(str(hint))
        for pattern, label in _COMPILED_TYPE_RULES:
            if pattern.search(normalized):
                return label
    return None


def _geometry(visual: dict[str, Any]) -> tuple[float, float, float, float]:
    return (
        float(visual["x"]),
        float(visual["y"]),
        float(visual["width"]),
        float(visual["height"]),
    )


def _page_extent(visuals: list[dict[str, Any]]) -> tuple[float, float]:
    width = max((_geometry(v)[0] + _geometry(v)[2] for v in visuals), default=1.0)
    height = max((_geometry(v)[1] + _geometry(v)[3] for v in visuals), default=1.0)
    return max(width, 1.0), max(height, 1.0)


def _format_position(visual: dict[str, Any]) -> str:
    x, y, _, _ = _geometry(visual)
    return f"({round(x)}, {round(y)})"


def _format_size(visual: dict[str, Any]) -> str:
    _, _, width, height = _geometry(visual)
    return f"{round(width)} x {round(height)}"


def _geometry_cost(
    a: dict[str, Any], b: dict[str, Any], page_w: float, page_h: float
) -> tuple[float, float, float]:
    """Return (centre offset, relative size change, combined pairing cost)."""
    ax, ay, aw, ah = _geometry(a)
    bx, by, bw, bh = _geometry(b)
    centre_offset = math.hypot(
        ((ax + aw / 2) - (bx + bw / 2)) / page_w,
        ((ay + ah / 2) - (by + bh / 2)) / page_h,
    )
    size_change = max(
        abs(aw - bw) / max(aw, bw, 1.0),
        abs(ah - bh) / max(ah, bh, 1.0),
    )
    return centre_offset, size_change, centre_offset + size_change


def _pair_visuals(
    visuals_a: list[dict[str, Any]],
    visuals_b: list[dict[str, Any]],
    page_w: float,
    page_h: float,
) -> tuple[list[tuple[int, int, float, float]], list[int], list[int]]:
    """Pair by stable ID, then title and visual family to disambiguate repeats."""
    used_a: set[int] = set()
    used_b: set[int] = set()
    pairs = []

    def normalized_name(visual: dict[str, Any]) -> str:
        name = re.sub(
            r"\s+",
            " ",
            str(visual.get("visual_name") or ""),
        ).strip().casefold()
        visual_id = str(visual.get("visual_id") or "").strip().casefold()
        if (
            name in {"unknown", "unidentified", "unnamed visual"}
            or name in {visual_id, f"visual {visual_id}"}
        ):
            return ""
        return name

    def type_family(visual: dict[str, Any]) -> str:
        visual_type = str(visual.get("visual_type") or "").casefold()
        visual_type = re.sub(r"100%\s*", " ", visual_type)
        visual_type = re.sub(r"\b(?:stacked|clustered)\b", " ", visual_type)
        return re.sub(r"[^a-z0-9]+", " ", visual_type).strip()

    def pair_unique_matches(key: Any) -> None:
        by_key_a: dict[str, list[int]] = {}
        by_key_b: dict[str, list[int]] = {}
        for index, visual in enumerate(visuals_a):
            if index not in used_a and key(visual):
                by_key_a.setdefault(str(key(visual)), []).append(index)
        for index, visual in enumerate(visuals_b):
            if index not in used_b and key(visual):
                by_key_b.setdefault(str(key(visual)), []).append(index)
        for value, indexes_a in by_key_a.items():
            indexes_b = by_key_b.get(value, [])
            if len(indexes_a) != 1 or len(indexes_b) != 1:
                continue
            i, j = indexes_a[0], indexes_b[0]
            centre, size, _cost = _geometry_cost(
                visuals_a[i], visuals_b[j], page_w, page_h
            )
            used_a.add(i)
            used_b.add(j)
            pairs.append((i, j, centre, size))

    pair_unique_matches(
        lambda visual: (
            visual.get("visual_id")
            if not re.fullmatch(
                r"SCREENSHOT_\d{3}",
                str(visual.get("visual_id") or ""),
                flags=re.IGNORECASE,
            )
            else ""
        )
    )
    pair_unique_matches(normalized_name)
    pair_unique_matches(
        lambda visual: (
            (normalized_name(visual), type_family(visual))
            if normalized_name(visual) and type_family(visual)
            else ""
        )
    )
    pair_unique_matches(normalized_name)
    pairs.sort(key=lambda item: item[0])
    unpaired_a = [i for i in range(len(visuals_a)) if i not in used_a]
    unpaired_b = [j for j in range(len(visuals_b)) if j not in used_b]
    return pairs, unpaired_a, unpaired_b


def _visual_id(visual: dict[str, Any] | None) -> str:
    if visual is None:
        return "-"
    return str(visual.get("visual_id") or "-")


def _visual_type_label(visual: dict[str, Any] | None) -> str:
    if visual is None:
        return "Missing"
    visual_type = str(visual.get("visual_type") or "").strip()
    if visual_type.casefold() in {
        "",
        "unknown",
        "unidentified",
        "other / unidentified",
    }:
        return "Unidentified"
    return visual_type


def _visual_name(*candidates: dict[str, Any] | None) -> str:
    # Display-only identification. Never used to decide a visual type match.
    # Uses the first visual's title, then falls back to its visual id.
    for visual in candidates:
        if visual and visual.get("visual_name"):
            name = str(visual["visual_name"]).strip()
            if name.casefold() not in {"unknown", "unidentified"}:
                return name
    for visual in candidates:
        if visual and visual.get("visual_id"):
            return str(visual["visual_id"])
    return "Unidentified"


def _visual_row(
    page: str,
    visual_a: dict[str, Any] | None,
    visual_b: dict[str, Any] | None,
    status: str,
    notes: str,
    action_applied: str,
) -> dict[str, Any]:
    type_a = _visual_type_label(visual_a)
    type_b = _visual_type_label(visual_b)
    expected_result = type_a if visual_a else type_b
    if (
        visual_a
        and str(visual_a.get("rendering_status") or "").casefold() == "blank"
        and type_b not in {"Missing", "Unidentified"}
    ):
        expected_result = type_b
    elif (
        visual_b
        and str(visual_b.get("rendering_status") or "").casefold() == "blank"
        and type_a not in {"Missing", "Unidentified"}
    ):
        expected_result = type_a
    return {
        "page": page,
        "visual_name": _visual_name(visual_a, visual_b),
        "action_applied": action_applied,
        "expected_result": expected_result if visual_a or visual_b else "-",
        "visual_id_a": _visual_id(visual_a),
        "visual_id_b": _visual_id(visual_b),
        "visual_type_a": type_a,
        "visual_type_b": type_b,
        "rendering_status_a": (
            visual_a.get("rendering_status") if visual_a else "Missing"
        ),
        "rendering_status_b": (
            visual_b.get("rendering_status") if visual_b else "Missing"
        ),
        "visual_confidence_a": visual_a.get("confidence") if visual_a else None,
        "visual_confidence_b": visual_b.get("confidence") if visual_b else None,
        "position_a": _format_position(visual_a) if visual_a else "-",
        "position_b": _format_position(visual_b) if visual_b else "-",
        "size_a": _format_size(visual_a) if visual_a else "-",
        "size_b": _format_size(visual_b) if visual_b else "-",
        "status": status,
        "comparison_notes": notes,
    }


def compare_visual_inventories(
    page: str,
    visuals_a: list[dict[str, Any]],
    visuals_b: list[dict[str, Any]],
    action_applied: str = "None (default view)",
) -> list[dict[str, Any]]:
    """Compare visual containers of one page in Dashboard A and Dashboard B.

    Each input item needs: visual_id, visual_type (canonical or None), x, y,
    width, height. An optional visual_name is used only for display. Titles
    and text are never used for matching. action_applied records the filter
    combination in effect for this comparison run.
    """
    rows: list[dict[str, Any]] = []
    page_w, page_h = _page_extent(visuals_a + visuals_b)
    pairs, unpaired_a, unpaired_b = _pair_visuals(
        visuals_a, visuals_b, page_w, page_h
    )

    for i, j, centre, size in pairs:
        a, b = visuals_a[i], visuals_b[j]
        type_a, type_b = a.get("visual_type"), b.get("visual_type")
        notes: list[str] = []
        render_statuses = {
            "A": str(a.get("rendering_status") or "").casefold(),
            "B": str(b.get("rendering_status") or "").casefold(),
        }
        failed_sides = [
            side for side, status in render_statuses.items()
            if status in {"blank", "error"}
        ]
        incomplete_sides = [
            side for side, status in render_statuses.items()
            if status in {"partial", "loading", "unable to verify"}
        ]
        unidentified_sides = [
            side
            for side, visual_type in (("A", type_a), ("B", type_b))
            if not visual_type
            or str(visual_type).strip().casefold()
            in {"unknown", "unidentified"}
        ]
        if failed_sides:
            status = DIFFERENT
            for side in failed_sides:
                rendering = render_statuses[side]
                notes.append(
                    f"Visual is {rendering} on Dashboard {side}"
                )
            identified_type = type_b if failed_sides == ["A"] else type_a
            if identified_type and str(identified_type).casefold() not in {
                "unknown",
                "unidentified",
                "missing",
            }:
                notes.append(
                    f"Expected type inferred from the other dashboard: "
                    f"{identified_type}"
                )
        elif incomplete_sides:
            status = UNVERIFIED
            notes.extend(
                f"Visual rendering could not be verified on Dashboard {side}"
                for side in incomplete_sides
            )
        elif unidentified_sides:
            status = UNVERIFIED
            notes.extend(
                f"Visual type could not be identified for Dashboard {side}"
                for side in unidentified_sides
            )
        elif type_a != type_b:
            status = DIFFERENT
            notes.append(f"Visual type changed from {type_a} to {type_b}")
        else:
            status = MATCH
            notes.append(f"Same visual type ({type_a})")

        name_a = str(a.get("visual_name") or "").strip()
        name_b = str(b.get("visual_name") or "").strip()
        if (
            name_a
            and name_b
            and name_a.casefold() != name_b.casefold()
            and not incomplete_sides
        ):
            status = DIFFERENT
            notes.append(f"Visual title/name differs ({name_a} vs {name_b})")

        if centre > POSITION_TOLERANCE or size > SIZE_TOLERANCE:
            if status == MATCH:
                status = DIFFERENT
            notes.append(
                "Layout differs"
                f" (position shift {centre:.0%} of page, size change {size:.0%})"
            )
        rows.append(
            _visual_row(page, a, b, status, "; ".join(notes), action_applied)
        )

    for i in unpaired_a:
        confirmed_missing = (
            len(visuals_a) > len(visuals_b) and not unpaired_b
        )
        rows.append(
            _visual_row(
                page,
                visuals_a[i],
                None,
                DIFFERENT if confirmed_missing else UNVERIFIED,
                (
                    "Visual is present in Dashboard A and no corresponding "
                    "visual is present in Dashboard B"
                    if confirmed_missing
                    else "No visual with a matching ID or unique title was "
                    "identified in Dashboard B; this may be a missing, "
                    "renamed, or unmatched visual."
                ),
                action_applied,
            )
        )
    for j in unpaired_b:
        confirmed_missing = (
            len(visuals_b) > len(visuals_a) and not unpaired_a
        )
        rows.append(
            _visual_row(
                page,
                None,
                visuals_b[j],
                DIFFERENT if confirmed_missing else UNVERIFIED,
                (
                    "Visual is present in Dashboard B and no corresponding "
                    "visual is present in Dashboard A"
                    if confirmed_missing
                    else "No visual with a matching ID or unique title was "
                    "identified in Dashboard A; this may be a missing, "
                    "renamed, or unmatched visual."
                ),
                action_applied,
            )
        )

    if len(visuals_a) != len(visuals_b):
        rows.append(
            {
                "page": page,
                "visual_name": "-",
                "action_applied": action_applied,
                "expected_result": "-",
                "visual_id_a": f"{len(visuals_a)} visuals",
                "visual_id_b": f"{len(visuals_b)} visuals",
                "visual_type_a": "-",
                "visual_type_b": "-",
                "position_a": "-",
                "position_b": "-",
                "size_a": "-",
                "size_b": "-",
                "status": INFO,
                "comparison_notes": "Visual count differs between dashboards",
            }
        )
    return rows


# Filter control types produced by the filter discovery workflow.
_FILTER_TYPE_LABELS = {
    "radio_dropdown": "Dropdown",
    "checkbox_dropdown": "Checkbox",
    "date": "Date slicer",
    "bookmark_buttons": "Bookmark buttons",
}
_SELECTION_MODE_LABELS = {
    "radio_dropdown": "Single-select",
    "checkbox_dropdown": "Multi-select",
}


def _filter_key(name: Any) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip().casefold()


def _values(values: Any) -> list[str]:
    return [str(value).strip() for value in (values or []) if str(value).strip()]


def _canonical_filter_selection(filter_item: dict[str, Any] | None) -> list[str]:
    selected = _values((filter_item or {}).get("selected"))
    if not selected:
        return []
    aggregate_labels = {"all", "(all)", "select all"}
    clear_labels = {"clear selection", "clear selections"}
    option_values = _values((filter_item or {}).get("options"))
    option_keys = {_filter_key(value) for value in option_values}
    selected_keys = {
        _filter_key(value)
        for value in selected
        if _filter_key(value) not in clear_labels
    }
    selected_all_marker = any(
        key == "select all" or (key in {"all", "(all)"} and key not in option_keys)
        for key in selected_keys
    )
    if selected_all_marker:
        return ["All"]
    selectable_option_keys = {
        _filter_key(value)
        for value in option_values
        if _filter_key(value) not in aggregate_labels | clear_labels
    }
    if (
        (filter_item or {}).get("control_type") in {"checkbox_dropdown", "checkbox"}
        and selectable_option_keys
        and selected_keys == selectable_option_keys
    ):
        return ["All"]
    return selected


def _filter_label(filter_item: dict[str, Any] | None) -> tuple[str, str]:
    if filter_item is None:
        return "Missing", "-"
    control = filter_item.get("control_type")
    if not control:
        return "Unknown", "-"
    return (
        _FILTER_TYPE_LABELS.get(control, str(control)),
        _SELECTION_MODE_LABELS.get(control, "N/A"),
    )


def _filter_row(
    page: str,
    name: str,
    filter_a: dict[str, Any] | None,
    filter_b: dict[str, Any] | None,
    status: str,
    notes: str,
) -> dict[str, Any]:
    type_a, mode_a = _filter_label(filter_a)
    type_b, mode_b = _filter_label(filter_b)
    return {
        "page": page,
        "filter_name": name,
        "filter_type_a": type_a,
        "filter_type_b": type_b,
        "available_options_a": ", ".join(_values(filter_a and filter_a.get("options"))) or "-",
        "available_options_b": ", ".join(_values(filter_b and filter_b.get("options"))) or "-",
        "selected_values_a": ", ".join(_canonical_filter_selection(filter_a)) or "-",
        "selected_values_b": ", ".join(_canonical_filter_selection(filter_b)) or "-",
        "selection_mode_a": mode_a,
        "selection_mode_b": mode_b,
        "status": status,
        "comparison_notes": notes,
    }


def compare_filter_configurations(
    page: str,
    filters_a: list[dict[str, Any]],
    filters_b: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compare filter configurations of one page by filter name.

    Options and selected values are compared as sets, so their order does not
    matter. A filter present on only one side is DIFFERENT.
    """
    by_key_a = {_filter_key(f.get("name")): f for f in filters_a if f.get("name")}
    by_key_b = {_filter_key(f.get("name")): f for f in filters_b if f.get("name")}
    rows: list[dict[str, Any]] = []

    ordered_keys = list(
        dict.fromkeys(
            _filter_key(filter_item.get("name"))
            for filter_item in [*filters_a, *filters_b]
            if filter_item.get("name")
        )
    )
    for key in ordered_keys:
        filter_a = by_key_a.get(key)
        filter_b = by_key_b.get(key)
        name = (filter_a or filter_b).get("name")
        if filter_a is None or filter_b is None:
            missing = "A" if filter_a is None else "B"
            present = "B" if filter_a is None else "A"
            rows.append(
                _filter_row(
                    page,
                    name,
                    filter_a,
                    filter_b,
                    DIFFERENT,
                    f"Filter exists in Dashboard {present} but is missing in Dashboard {missing}",
                )
            )
            continue

        notes: list[str] = []
        status = MATCH
        type_a, mode_a = _filter_label(filter_a)
        type_b, mode_b = _filter_label(filter_b)
        if not filter_a.get("control_type") or not filter_b.get("control_type"):
            status = UNVERIFIED
            notes.append("Filter control type could not be identified")
        elif type_a != type_b:
            status = DIFFERENT
            notes.append(f"Filter type differs ({type_a} vs {type_b})")
        if mode_a != mode_b and "N/A" not in (mode_a, mode_b):
            status = DIFFERENT
            notes.append(f"Selection mode differs ({mode_a} vs {mode_b})")
        if set(_values(filter_a.get("options"))) != set(_values(filter_b.get("options"))):
            status = DIFFERENT if status != UNVERIFIED else status
            notes.append("Available options differ")
        selected_a = {
            _filter_key(value) for value in _canonical_filter_selection(filter_a)
        }
        selected_b = {
            _filter_key(value) for value in _canonical_filter_selection(filter_b)
        }
        if selected_a != selected_b:
            status = DIFFERENT if status != UNVERIFIED else status
            notes.append("Selected values differ")
        if not notes:
            notes.append("Same filter configuration")
        rows.append(_filter_row(page, name, filter_a, filter_b, status, "; ".join(notes)))
    return rows


# Extracted element types that represent a visual container. Text items such as
# column headers, titles and metadata are deliberately not listed.
_VISUAL_CONTAINER_TYPES = frozenset(
    {
        "table",
        "matrix",
        "kpi",
        "card",
        "chart",
        "visual",
        "bar_chart",
        "column_chart",
        "line_chart",
        "area_chart",
        "pie_chart",
        "donut_chart",
        "treemap",
        "scatter_chart",
        "combo_chart",
        "map",
        "gauge",
        "funnel",
        "waterfall",
    }
)


def _normalize_type(value: Any) -> str:
    return str(value or "").strip().casefold().replace(" ", "_").replace("-", "_")


def _visual_geometry(element: dict[str, Any]) -> dict[str, float] | None:
    # Normalized boxes are preferred so both dashboards use the same coordinate space.
    if all(
        isinstance(element.get(k), (int, float))
        for k in ("x", "y", "width", "height")
    ):
        return {
            k: float(element[k]) for k in ("x", "y", "width", "height")
        }
    for key in ("normalized_bounding_box", "bounding_box"):
        box = element.get(key)
        if isinstance(box, dict) and all(
            isinstance(box.get(k), (int, float)) for k in ("x", "y", "width", "height")
        ):
            return {k: float(box[k]) for k in ("x", "y", "width", "height")}
    return None


def visual_inventory(state: Any) -> list[dict[str, Any]]:
    """Return the visual containers of one captured state.

    Only the element type, identifier, geometry and display name are read. The
    display name is the visual's own title and is never used for matching.
    """
    visuals: list[dict[str, Any]] = []
    captured_visuals = getattr(state, "visuals", None) or []
    if captured_visuals:
        for visual in captured_visuals:
            if not isinstance(visual, dict):
                continue
            geometry = _visual_geometry(visual)
            if geometry is None:
                continue
            item = {
                "visual_id": visual.get("visual_id")
                or f"visual_{len(visuals) + 1}",
                "visual_name": visual.get("visual_name") or None,
                "visual_type": _visual_type_label(visual),
                **geometry,
            }
            for key in ("rendering_status", "confidence"):
                if visual.get(key) is not None:
                    item[key] = visual[key]
            visuals.append(item)
        return visuals

    elements = (state.extracted or {}).get("elements") or []
    for element in elements:
        if not isinstance(element, dict):
            continue
        element_type = _normalize_type(element.get("element_type"))
        if element_type not in _VISUAL_CONTAINER_TYPES:
            continue
        geometry = _visual_geometry(element)
        if geometry is None:
            continue
        visuals.append(
            {
                "visual_id": element.get("element_id") or f"{element_type}_{len(visuals) + 1}",
                "visual_name": element.get("element_text") or element.get("title") or None,
                "visual_type": classify_visual_type(element_type),
                **geometry,
            }
        )
    return visuals


def action_label(filter_item: Any) -> str:
    """Describe the filter combination applied in a captured state."""
    if filter_item is None:
        return "None (default view)"
    selections = getattr(filter_item, "selections", None)
    if selections is None:
        selections = [filter_item]
    return "; ".join(f"{s.name}: {s.value}" for s in selections)


def filter_inventory(state: Any) -> list[dict[str, Any]]:
    """Return the filter configuration of one captured state.

    Current selections, available options, and control types come from the
    accessible filter capture. Legacy states without separate option metadata
    retain their previous behavior.
    """
    options_by_name = getattr(state, "filter_options", {}) or {}
    types_by_name = getattr(state, "filter_control_types", {}) or {}
    selected_by_name = getattr(state, "filter_values", {}) or {}
    applied = getattr(state.filter, "selections", None)
    if applied is None and state.filter is not None:
        applied = [state.filter]
    applied = applied or []
    names = list(dict.fromkeys([*(state.filter_names or []), *(state.filter_values or {})]))
    inventory: list[dict[str, Any]] = []
    for name in names:
        has_captured_options = name in options_by_name
        selected = selected_by_name.get(name)
        if not has_captured_options:
            selected = [s.value for s in applied if s.name == name]
        inventory.append(
            {
                "name": name,
                "control_type": types_by_name.get(name),
                "options": options_by_name.get(
                    name, selected_by_name.get(name, [])
                ),
                "selected": selected or [],
            }
        )
    return inventory


def build_visual_filter_result(
    states_a: list[Any],
    states_b: list[Any],
) -> dict[str, Any]:
    """Build visual and filter rows by pairing states of the same page and filter.

    States with no counterpart on the other side are listed in
    unmatched_states instead of being compared silently.
    """
    by_key_b: dict[str, Any] = {}
    for state in states_b:
        by_key_b.setdefault(state.state_key, state)
    matched_keys: set[str] = set()
    visual_rows: list[dict[str, Any]] = []
    filter_rows: list[dict[str, Any]] = []
    unmatched: list[str] = []

    for state_a in states_a:
        state_b = by_key_b.get(state_a.state_key)
        if state_b is None:
            unmatched.append(f"A only: {state_a.state_key}")
            continue
        matched_keys.add(state_a.state_key)
        current_visual_rows = compare_visual_inventories(
            state_a.page,
            visual_inventory(state_a),
            visual_inventory(state_b),
            action_applied=action_label(state_a.filter),
        )
        for row in current_visual_rows:
            row["state"] = state_a.state_key
        visual_rows.extend(current_visual_rows)
        current_filter_rows = compare_filter_configurations(
            state_a.page,
            filter_inventory(state_a),
            filter_inventory(state_b),
        )
        order_a = {
            _filter_key(name): index
            for index, name in enumerate(
                dict.fromkeys(
                    [*(state_a.filter_names or []), *(state_a.filter_values or {})]
                ),
                start=1,
            )
        }
        order_b = {
            _filter_key(name): index
            for index, name in enumerate(
                dict.fromkeys(
                    [*(state_b.filter_names or []), *(state_b.filter_values or {})]
                ),
                start=1,
            )
        }
        for aligned_position, row in enumerate(current_filter_rows, start=1):
            key = _filter_key(row["filter_name"])
            row["state"] = state_a.state_key
            row["aligned_position"] = aligned_position
            row["order_a"] = order_a.get(key)
            row["order_b"] = order_b.get(key)
        filter_rows.extend(current_filter_rows)

    unmatched.extend(
        f"B only: {key}" for key in by_key_b if key not in matched_keys
    )
    return {
        "visual_rows": visual_rows,
        "filter_rows": filter_rows,
        "unmatched_states": unmatched,
    }
