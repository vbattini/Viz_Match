"""Visual-container and filter-configuration comparison.

This module compares only what the dashboard comparator is meant to validate:

* visual containers, by screenshot-identified name/type and layout;
* filter configurations, by name, control type, options, selection and mode.

It never reads chart labels, headers, cell values or KPI numbers.
"""

from __future__ import annotations

import math
from pathlib import Path
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
    unavailable = {"", "-", "not captured", "unavailable", "unknown"}
    has_both = key_a not in unavailable and key_b not in unavailable
    if has_both and key_a == key_b:
        return MATCH, "Filter selection successfully verified on both dashboards."
    if has_both:
        return (
            DIFFERENT,
            f"Filter selection differs: Base {value_a}; Compare {value_b}.",
        )
    return (
        UNVERIFIED,
        "Filter selection could not be verified: "
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
    (r"shape|divider|separator|horizontal line|rule", "Shape / Line"),
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


def _visual_match_name(visual: dict[str, Any]) -> str:
    visual_id = str(visual.get("visual_id") or "").strip().casefold()
    for field in ("visual_name", "visual_title"):
        name = re.sub(r"\s+", " ", str(visual.get(field) or "")).strip()
        key = name.casefold()
        if (
            not key
            or key in {"unknown", "unidentified", "unnamed visual"}
            or key in {visual_id, f"visual {visual_id}"}
            or re.fullmatch(r"(?:visual|screenshot)[\s_-]*\d+", key)
        ):
            continue
        return name
    return ""


def _is_reliable_screenshot_pair(
    visual_a: dict[str, Any],
    visual_b: dict[str, Any],
    type_a: Any,
    type_b: Any,
    centre_offset: float,
    size_change: float,
) -> bool:
    return (
        re.fullmatch(
            r"SCREENSHOT_\d{3}",
            str(visual_a.get("visual_id") or ""),
            flags=re.IGNORECASE,
        )
        is not None
        and re.fullmatch(
            r"SCREENSHOT_\d{3}",
            str(visual_b.get("visual_id") or ""),
            flags=re.IGNORECASE,
        )
        is not None
        and same_visual_type(type_a, type_b)
        and str(visual_a.get("confidence") or "").casefold() == "high"
        and str(visual_b.get("confidence") or "").casefold() == "high"
        and str(visual_a.get("rendering_status") or "").casefold() == "rendered"
        and str(visual_b.get("rendering_status") or "").casefold() == "rendered"
        and centre_offset <= POSITION_TOLERANCE
        and size_change <= SIZE_TOLERANCE
    )


def _pair_visuals(
    visuals_a: list[dict[str, Any]],
    visuals_b: list[dict[str, Any]],
    page_w: float,
    page_h: float,
) -> tuple[list[tuple[int, int, float, float]], list[int], list[int]]:
    """Pair by stable ID and unique title, then use type and geometry for repeats."""
    used_a: set[int] = set()
    used_b: set[int] = set()
    pairs = []

    def normalized_name(visual: dict[str, Any]) -> str:
        return _visual_match_name(visual).casefold()

    def type_family(visual: dict[str, Any]) -> str:
        visual_type = str(visual.get("visual_type") or "").casefold()
        visual_type = re.sub(r"100%\s*", " ", visual_type)
        visual_type = re.sub(r"\b(?:stacked|clustered)\b", " ", visual_type)
        return re.sub(r"[^a-z0-9]+", " ", visual_type).strip()

    def meaningful_name_tokens(visual: dict[str, Any]) -> set[str]:
        stop_words = {
            "a",
            "an",
            "and",
            "at",
            "by",
            "for",
            "from",
            "in",
            "of",
            "on",
            "the",
            "to",
            "with",
        }
        return {
            token
            for token in re.findall(r"[a-z0-9]+", normalized_name(visual))
            if token not in stop_words
        }

    def related_names(a: dict[str, Any], b: dict[str, Any]) -> bool:
        tokens_a = meaningful_name_tokens(a)
        tokens_b = meaningful_name_tokens(b)
        shared = tokens_a & tokens_b
        shorter_name_length = min(len(tokens_a), len(tokens_b))
        return (
            bool(tokens_a and tokens_b)
            and len(shared) >= 2
            and len(shared) / shorter_name_length >= 0.5
        )

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

    # A renamed visual can still be paired when its name retains a distinctive
    # phrase and its visual type agrees. Require a unique candidate on both
    # sides; geometry alone is never used to establish identity.
    candidates_a: dict[int, list[tuple[int, float]]] = {}
    candidates_b: dict[int, list[tuple[int, float]]] = {}
    for i, visual_a in enumerate(visuals_a):
        if i in used_a:
            continue
        for j, visual_b in enumerate(visuals_b):
            if j in used_b:
                continue
            if (
                not type_family(visual_a)
                or type_family(visual_a) != type_family(visual_b)
            ):
                continue
            centre, size, cost = _geometry_cost(
                visual_a, visual_b, page_w, page_h
            )
            if related_names(visual_a, visual_b) or _is_reliable_screenshot_pair(
                visual_a,
                visual_b,
                visual_a.get("visual_type"),
                visual_b.get("visual_type"),
                centre,
                size,
            ):
                candidates_a.setdefault(i, []).append((j, cost))
                candidates_b.setdefault(j, []).append((i, cost))

    def unique_best_candidate(
        candidates: list[tuple[int, float]],
    ) -> int | None:
        ranked = sorted(candidates, key=lambda candidate: candidate[1])
        if len(ranked) > 1 and ranked[1][1] - ranked[0][1] <= POSITION_TOLERANCE:
            return None
        return ranked[0][0] if ranked else None

    best_b_for_a = {
        i: unique_best_candidate(candidates)
        for i, candidates in candidates_a.items()
    }
    best_a_for_b = {
        j: unique_best_candidate(candidates)
        for j, candidates in candidates_b.items()
    }
    for i, j in best_b_for_a.items():
        if j is None or best_a_for_b.get(j) != i:
            continue
        centre, size, _cost = _geometry_cost(
            visuals_a[i], visuals_b[j], page_w, page_h
        )
        used_a.add(i)
        used_b.add(j)
        pairs.append((i, j, centre, size))

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


def _visual_type_key(value: Any) -> str:
    return re.sub(r"[\s_-]+", " ", str(value or "").strip().casefold())


def _visual_type_from_evidence(visual: dict[str, Any]) -> str:
    visual_type = _visual_type_label(visual)
    if visual_type.casefold() not in {"kpi", "kpi card"}:
        return visual_type
    observation = " ".join(str(visual.get("observation") or "").split()).casefold()
    if any(
        phrase in observation
        for phrase in ("alert banner", "callout box", "alert status text")
    ):
        return "Text Box"
    return visual_type


def _visual_name(*candidates: dict[str, Any] | None) -> str:
    for visual in candidates:
        if visual:
            name = _visual_match_name(visual)
            if name:
                return name
    for visual in candidates:
        if visual and visual.get("visual_id"):
            return str(visual["visual_id"])
    return "Unidentified"


def _visual_title(visual: dict[str, Any] | None) -> str:
    if not visual:
        return ""
    return str(visual.get("visual_title") or visual.get("visual_name") or "").strip()


def _hierarchy_headers(visual: dict[str, Any] | None) -> list[tuple[str, int]]:
    if not isinstance(visual, dict):
        return []
    headers = visual.get("row_headers") or []
    result: list[tuple[str, int]] = []
    for item in headers:
        if isinstance(item, dict):
            text = " ".join(str(item.get("text") or "").split())
            level = int(item.get("level") or 1)
        else:
            text = " ".join(str(item or "").split())
            level = 1
        if text:
            result.append((text.casefold(), level))
    return result


def _hierarchy_structure(visual: dict[str, Any] | None) -> dict[str, Any]:
    structure = visual.get("hierarchy_structure") if isinstance(visual, dict) else None
    if isinstance(structure, dict) and structure.get("status"):
        return structure
    headers = visual.get("row_headers") if isinstance(visual, dict) else []
    rows = []
    for index, header in enumerate(headers or []):
        if isinstance(header, dict):
            text = header.get("text")
            level = header.get("level") or 1
        else:
            text = header
            level = 1
        if text:
            rows.append(
                {
                    "original_row_index": index,
                    "original_row_order": index,
                    "hierarchy_level": level,
                    "parent_path": [],
                    "current_value": text,
                    "row_type": "detail",
                }
            )
    return {
        "status": "PASS" if rows else "UNVERIFIED",
        "columns": [],
        "rows": rows,
        "depth": max((row["hierarchy_level"] for row in rows), default=0),
        "row_count": len(rows),
    }


def _hierarchy_row_signature(structure: dict[str, Any]) -> list[tuple[Any, ...]]:
    signatures: list[tuple[Any, ...]] = []
    for row in structure.get("rows") or []:
        if not isinstance(row, dict):
            continue
        signatures.append(
            (
                int(row.get("hierarchy_level") or 0),
                tuple(
                    " ".join(str(value or "").split()).casefold()
                    for value in row.get("parent_path") or []
                ),
                " ".join(str(row.get("current_value") or "").split()).casefold(),
                str(row.get("row_type") or "").casefold(),
            )
        )
    return signatures


def _compare_hierarchy_structures(
    structure_a: dict[str, Any],
    structure_b: dict[str, Any],
) -> dict[str, Any]:
    status_a = str(structure_a.get("status") or "UNVERIFIED").upper()
    status_b = str(structure_b.get("status") or "UNVERIFIED").upper()
    result: dict[str, Any] = {
        "status": "UNVERIFIED",
        "detected_columns_a": structure_a.get("columns") or [],
        "detected_columns_b": structure_b.get("columns") or [],
        "depth_a": structure_a.get("depth") or 0,
        "depth_b": structure_b.get("depth") or 0,
        "row_count_a": structure_a.get("row_count") or len(structure_a.get("rows") or []),
        "row_count_b": structure_b.get("row_count") or len(structure_b.get("rows") or []),
        "missing_rows": [],
        "additional_rows": [],
        "different_rows": [],
        "notes": [],
    }
    if status_a != "PASS" or status_b != "PASS":
        result["notes"].append("Hierarchy detection or export extraction is unverified.")
        return result

    signatures_a = _hierarchy_row_signature(structure_a)
    signatures_b = _hierarchy_row_signature(structure_b)
    if structure_a.get("depth") != structure_b.get("depth"):
        result["status"] = "DIFFERENT"
        result["notes"].append("Detected hierarchy depth differs.")
    if len(structure_a.get("columns") or []) != len(structure_b.get("columns") or []):
        result["status"] = "DIFFERENT"
        result["notes"].append("Detected hierarchy column count differs.")

    if signatures_a == signatures_b and result["status"] != "DIFFERENT":
        result["status"] = "MATCH"
        result["notes"].append("Hierarchy rows, parent paths, levels, types, and order match.")
        return result

    if result["status"] != "DIFFERENT":
        result["status"] = "DIFFERENT"
    common_length = min(len(signatures_a), len(signatures_b))
    for index in range(common_length):
        if signatures_a[index] != signatures_b[index]:
            result["different_rows"].append(
                {
                    "row_order": index,
                    "base": signatures_a[index],
                    "compare": signatures_b[index],
                }
            )
    if len(signatures_a) > common_length:
        result["missing_rows"] = [
            {"row_order": index, "signature": signatures_a[index]}
            for index in range(common_length, len(signatures_a))
        ]
    if len(signatures_b) > common_length:
        result["additional_rows"] = [
            {"row_order": index, "signature": signatures_b[index]}
            for index in range(common_length, len(signatures_b))
        ]
    result["notes"].append("Hierarchy row order, values, parent paths, or row types differ.")
    return result


def _compare_drill_down_levels(
    visual_a: dict[str, Any] | None,
    visual_b: dict[str, Any] | None,
) -> dict[str, Any]:
    levels_a = (
        ((visual_a or {}).get("drill_down") or {}).get("levels") or []
    )
    levels_b = (
        ((visual_b or {}).get("drill_down") or {}).get("levels") or []
    )
    if not levels_a and not levels_b:
        return {"status": "SKIPPED", "levels": []}
    level_results: list[dict[str, Any]] = []
    status = "MATCH"
    for index in range(max(len(levels_a), len(levels_b))):
        left = levels_a[index] if index < len(levels_a) else None
        right = levels_b[index] if index < len(levels_b) else None
        left_headers = (left or {}).get("headers", [])
        right_headers = (right or {}).get("headers", [])
        left_rows = (left or {}).get("rows", [])
        right_rows = (right or {}).get("rows", [])
        matched = (
            left is not None
            and right is not None
            and left_headers == right_headers
            and left_rows == right_rows
        )
        if not matched:
            status = "DIFFERENT"
        level_results.append(
            {
                "level": index + 1,
                "status": "MATCH" if matched else "DIFFERENT",
                "headers_a": left_headers,
                "headers_b": right_headers,
                "rows_a": left_rows,
                "rows_b": right_rows,
            }
        )
    return {"status": status, "levels": level_results}


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
        "visual_name_a": (
            _visual_match_name(visual_a)
            if visual_a
            else ""
        ),
        "visual_name_b": (
            _visual_match_name(visual_b)
            if visual_b
            else ""
        ),
        "visual_title_a": _visual_title(visual_a),
        "visual_title_b": _visual_title(visual_b),
        "action_applied": action_applied,
        "expected_result": expected_result if visual_a or visual_b else "-",
        "visual_id_a": _visual_id(visual_a),
        "visual_id_b": _visual_id(visual_b),
        "visual_type_a": type_a,
        "visual_type_b": type_b,
        "categories_a": visual_a.get("categories") if visual_a else None,
        "categories_b": visual_b.get("categories") if visual_b else None,
        "row_headers_a": (
            visual_a.get("row_headers") if visual_a else None
        ),
        "row_headers_b": (
            visual_b.get("row_headers") if visual_b else None
        ),
        "hierarchy_structure_a": (
            visual_a.get("hierarchy_structure") if visual_a else None
        ),
        "hierarchy_structure_b": (
            visual_b.get("hierarchy_structure") if visual_b else None
        ),
        "hierarchy_extraction_a": (
            visual_a.get("hierarchy_extraction") if visual_a else None
        ),
        "hierarchy_extraction_b": (
            visual_b.get("hierarchy_extraction") if visual_b else None
        ),
        "drill_down_comparison": _compare_drill_down_levels(
            visual_a, visual_b
        ),
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


_DECORATIVE_TYPES = {"shape / line", "text box", "header", "image"}
_VISUAL_TYPE_EQUIVALENCE_GROUPS = (
    {"text box", "header"},
    {"text box", "kpi"},
    {"table", "matrix"},
    {"table", "multi row card"},
)


def effective_rendering_status(status: Any, visual_type: Any) -> str:
    """Decorative visuals (lines, text, images) have no data, so are never blank."""
    text = str(status or "").casefold()
    if text == "blank" and str(visual_type or "").strip().casefold() in _DECORATIVE_TYPES:
        return "ok"
    return text


def same_visual_type(type_a: Any, type_b: Any) -> bool:
    a = _visual_type_key(type_a)
    b = _visual_type_key(type_b)
    return a == b or any(
        a in group and b in group for group in _VISUAL_TYPE_EQUIVALENCE_GROUPS
    )


def same_visual_name(name_a: Any, name_b: Any) -> bool:
    def normalize(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip().casefold()

    normalized_a = normalize(name_a)
    normalized_b = normalize(name_b)
    return bool(normalized_a) and normalized_a == normalized_b


def compare_visual_inventories(
    page: str,
    visuals_a: list[dict[str, Any]],
    visuals_b: list[dict[str, Any]],
    action_applied: str = "None (default view)",
) -> list[dict[str, Any]]:
    """Compare visual containers of one page in Dashboard A and Dashboard B.

    Each input item needs: visual_id, visual_type (canonical or None), x, y,
    width, height. An optional visual_name helps pair visuals and is preserved
    in the result. action_applied records the filter combination in effect.
    """
    rows: list[dict[str, Any]] = []
    page_w, page_h = _page_extent(visuals_a + visuals_b)
    pairs, unpaired_a, unpaired_b = _pair_visuals(
        visuals_a, visuals_b, page_w, page_h
    )

    for i, j, centre, size in pairs:
        a, b = visuals_a[i], visuals_b[j]
        type_a, type_b = _visual_type_label(a), _visual_type_label(b)
        screenshot_visual_pair = _is_reliable_screenshot_pair(
            a, b, type_a, type_b, centre, size
        )
        matched_name_a = _visual_match_name(a)
        matched_name_b = _visual_match_name(b)
        same_name = bool(matched_name_a) and (
            matched_name_a.casefold() == matched_name_b.casefold()
        )
        notes: list[str] = []
        render_statuses = {
            "A": effective_rendering_status(
                a.get("rendering_status"), type_a or type_b
            ),
            "B": effective_rendering_status(
                b.get("rendering_status"), type_b or type_a
            ),
        }
        failed_sides = [
            side for side, status in render_statuses.items()
            if status in {"blank", "error"}
        ]
        incomplete_sides = [
            side for side, status in render_statuses.items()
            if status in {"partial", "loading", "unable to verify"}
        ]
        matching_blank_kpi = (
            same_name
            and type_a.casefold() in {"kpi", "kpi card"}
            and type_b.casefold() in {"kpi", "kpi card"}
            and render_statuses["A"] == "blank"
            and render_statuses["B"] == "blank"
            and str(a.get("confidence") or "").casefold() == "high"
            and str(b.get("confidence") or "").casefold() == "high"
        )
        matching_blank_visual = (
            same_name
            and same_visual_type(type_a, type_b)
            and render_statuses["A"] == "blank"
            and render_statuses["B"] == "blank"
        )
        unidentified_sides = [
            side
            for side, visual_type in (("A", type_a), ("B", type_b))
            if not visual_type
            or str(visual_type).strip().casefold()
            in {"unknown", "unidentified"}
        ]
        if matching_blank_kpi or matching_blank_visual:
            status = MATCH
            notes.append(
                "Both dashboards explicitly display this visual as blank; "
                "it matches the Base dashboard."
            )
        elif failed_sides:
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
        elif unidentified_sides and not same_name:
            status = UNVERIFIED
            notes.extend(
                f"Visual type could not be identified for Dashboard {side}"
                for side in unidentified_sides
            )
        elif not same_visual_type(type_a, type_b) and not same_name:
            status = DIFFERENT
            notes.append(f"Visual type changed from {type_a} to {type_b}")
        elif same_name and (
            unidentified_sides or not same_visual_type(type_a, type_b)
        ):
            status = MATCH
            notes.append(
                f"Exact visual title matches ({matched_name_a}); "
                f"type labels differ (Base {type_a}; Compare {type_b}) "
                "and are not used to reject the match."
            )
        else:
            status = MATCH
            if screenshot_visual_pair and not same_name:
                notes.append(
                    "Both screenshots show the same rendered visual type "
                    "with matching geometry; generated visual labels differ "
                    "and are not treated as a title mismatch."
                )
            else:
                notes.append(f"Same visual type ({type_a})")

        stable_id_a = str(a.get("visual_id") or "").strip().casefold()
        stable_id_b = str(b.get("visual_id") or "").strip().casefold()
        stable_id_match = (
            bool(stable_id_a)
            and stable_id_a == stable_id_b
            and not re.fullmatch(r"screenshot_\d{3}", stable_id_a)
        )
        if (
            matched_name_a
            and matched_name_b
            and not same_name
            and not stable_id_match
            and not screenshot_visual_pair
        ):
            if status == MATCH:
                status = DIFFERENT
            notes.append(
                f"Visual title/name differs: Base {matched_name_a}; "
                f"Compare {matched_name_b}."
            )

        if centre > POSITION_TOLERANCE or size > SIZE_TOLERANCE:
            if status == MATCH:
                status = DIFFERENT
            notes.append(
                "Layout differs"
                f" (position shift {centre:.0%} of page, size change {size:.0%})"
            )
        hierarchy_a = a.get("hierarchy_extraction")
        hierarchy_b = b.get("hierarchy_extraction")
        hierarchy_evidence_a = (
            hierarchy_a
            or a.get("hierarchy_structure")
            or a.get("row_headers")
        )
        hierarchy_evidence_b = (
            hierarchy_b
            or b.get("hierarchy_structure")
            or b.get("row_headers")
        )
        hierarchy_comparison: dict[str, Any] | None = None
        if hierarchy_evidence_a or hierarchy_evidence_b:
            hierarchy_comparison = _compare_hierarchy_structures(
                _hierarchy_structure(a),
                _hierarchy_structure(b),
            )
            notes.append(
                "Hierarchy detection: "
                f"Base depth {hierarchy_comparison['depth_a']}, "
                f"Compare depth {hierarchy_comparison['depth_b']}; "
                f"Base rows {hierarchy_comparison['row_count_a']}, "
                f"Compare rows {hierarchy_comparison['row_count_b']}."
            )
            notes.append(
                "Detected hierarchy columns: "
                f"Base {[(item.get('name'), item.get('level')) for item in hierarchy_comparison['detected_columns_a']]}; "
                f"Compare {[(item.get('name'), item.get('level')) for item in hierarchy_comparison['detected_columns_b']]}."
            )
            if hierarchy_comparison["status"] == "UNVERIFIED":
                status = UNVERIFIED
                notes.append(
                    "Hierarchical table extraction could not be verified on "
                    "both dashboards."
                )
            elif hierarchy_comparison["status"] == "DIFFERENT":
                if status == MATCH:
                    status = DIFFERENT
                notes.append(
                    "Hierarchy differences: "
                    f"{len(hierarchy_comparison['missing_rows'])} missing, "
                    f"{len(hierarchy_comparison['additional_rows'])} additional, "
                    f"{len(hierarchy_comparison['different_rows'])} different rows."
                )
                notes.append(
                    "Hierarchical table row headers, hierarchy levels, "
                    "parent paths, row types, or order differ."
                )
            else:
                notes.append(
                    "Hierarchical table row headers, hierarchy levels, "
                    "parent paths, row types, and order match."
                )
        row = _visual_row(page, a, b, status, "; ".join(notes), action_applied)
        if hierarchy_comparison is not None:
            row["hierarchy_comparison"] = hierarchy_comparison
        rows.append(row)

    def detection_is_uncertain(visual: dict[str, Any]) -> bool:
        rendering = str(visual.get("rendering_status") or "").casefold()
        confidence = str(visual.get("confidence") or "").casefold()
        return rendering in {"blank", "partial", "loading", "unable to verify"} or (
            confidence in {"low", "medium"}
        )

    def presence_is_confirmed(visual: dict[str, Any]) -> bool:
        rendering = str(visual.get("rendering_status") or "").casefold()
        confidence = str(visual.get("confidence") or "").casefold()
        return confidence == "high" and rendering not in {
            "partial",
            "loading",
            "unable to verify",
        }

    for i in unpaired_a:
        confirmed_missing = (
            (
                len(visuals_a) > len(visuals_b)
                and not unpaired_b
                and not detection_is_uncertain(visuals_a[i])
            )
            or presence_is_confirmed(visuals_a[i])
        )
        rows.append(
            _visual_row(
                page,
                visuals_a[i],
                None,
                DIFFERENT if confirmed_missing else UNVERIFIED,
                (
                    "Visual was detected in Dashboard A, but no corresponding "
                    "visual was detected in Dashboard B."
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
            (
                len(visuals_b) > len(visuals_a)
                and not unpaired_a
                and not detection_is_uncertain(visuals_b[j])
            )
            or presence_is_confirmed(visuals_b[j])
        )
        rows.append(
            _visual_row(
                page,
                None,
                visuals_b[j],
                DIFFERENT if confirmed_missing else UNVERIFIED,
                (
                    "Visual was detected in Dashboard B, but no corresponding "
                    "visual was detected in Dashboard A."
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
    "dropdown": "Dropdown",
    "select": "Dropdown",
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
            notes.append("Filter control type could not be identified")
        elif type_a != type_b:
            status = DIFFERENT
            notes.append(f"Filter type differs ({type_a} vs {type_b})")
        if (
            filter_a.get("control_type")
            and filter_b.get("control_type")
            and mode_a != mode_b
            and "N/A" not in (mode_a, mode_b)
        ):
            status = DIFFERENT
            notes.append(f"Selection mode differs ({mode_a} vs {mode_b})")
        selected_a = {
            _filter_key(value) for value in _canonical_filter_selection(filter_a)
        }
        selected_b = {
            _filter_key(value) for value in _canonical_filter_selection(filter_b)
        }
        options_a = {_filter_key(value) for value in _values(filter_a.get("options"))}
        options_b = {_filter_key(value) for value in _values(filter_b.get("options"))}
        if options_a and options_b and options_a != options_b:
            status = DIFFERENT
            notes.append("Available options differ")
        elif bool(options_a) != bool(options_b):
            notes.append("Available options could not be verified on both dashboards")
        if not selected_a or not selected_b:
            if status == MATCH:
                status = UNVERIFIED
            missing_sides = [
                side
                for side, selected, options in (
                    ("Base", selected_a, options_a),
                    ("Compare", selected_b, options_b),
                )
                if not selected and not options
            ]
            notes.append(
                "Filter selection could not be verified: selected values are "
                "missing on "
                + " and ".join(missing_sides or [
                    side
                    for side, selected in (
                        ("Base", selected_a),
                        ("Compare", selected_b),
                    )
                    if not selected
                ])
            )
        elif selected_a != selected_b:
            status = DIFFERENT
            notes.append("Selected values differ")
        else:
            notes.append("Filter selection successfully verified on both dashboards")
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
        "shape",
        "shape_line",
        "shape_/_line",
    }
)


def _normalize_type(value: Any) -> str:
    return str(value or "").strip().casefold().replace(" ", "_").replace("-", "_")


def is_header_divider(visual: dict[str, Any]) -> bool:
    visual_type = _normalize_type(
        visual.get("visual_type") or visual.get("element_type") or visual.get("type")
    )
    if visual_type in {
        "divider",
        "divider_line",
        "header_divider_line",
        "horizontal_line",
        "separator",
        "separator_line",
        "rule",
    }:
        return True

    names = (
        visual.get("visual_name"),
        visual.get("visual_title"),
        visual.get("title"),
        visual.get("element_text"),
    )
    return any(
        re.sub(r"[^a-z0-9]+", " ", str(name or "").casefold()).strip()
        in {
            "header divider",
            "header divider line",
            "divider",
            "divider line",
            "header separator",
            "header separator line",
            "separator",
            "separator line",
            "horizontal line",
        }
        for name in names
    )


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


def _visible_visual_title(
    extracted: dict[str, Any], visual: dict[str, Any]
) -> str | None:
    visual_geometry = _visual_geometry(visual)
    if visual_geometry is None:
        return None

    left = visual_geometry["x"]
    top = visual_geometry["y"]
    right = left + visual_geometry["width"]
    bottom = top + visual_geometry["height"]
    visible_titles: list[tuple[int, float, str]] = []
    for item in extracted.get("title_items") or []:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "").casefold()
        if category not in {"chart_title", "visual_title"}:
            continue
        if item.get("extraction_status") != "complete":
            continue
        title = " ".join(str(item.get("text") or "").split())
        if not title:
            continue
        title_box = item.get("bounding_box")
        title_geometry = (
            {key: float(title_box[key]) for key in ("x", "y", "width", "height")}
            if isinstance(title_box, dict)
            and all(
                isinstance(title_box.get(key), (int, float))
                for key in ("x", "y", "width", "height")
            )
            else _visual_geometry(item)
        )
        if title_geometry is None:
            continue
        center_x = title_geometry["x"] + title_geometry["width"] / 2
        center_y = title_geometry["y"] + title_geometry["height"] / 2
        if left <= center_x <= right and top <= center_y <= bottom:
            visible_titles.append(
                (0 if category == "visual_title" else 1, center_y - top, title)
            )
    if not visible_titles:
        return None
    return min(visible_titles)[2]


def _is_scroll_screenshot_state(state: Any) -> bool:
    if state is None:
        return False
    screenshot = getattr(state, "screenshot", None)
    if screenshot:
        filename = Path(str(screenshot)).name
        if "Scroll" in filename:
            return True
    step = str(getattr(state, "scenario_step", "") or "")
    if "Scroll" in step:
        return True
    return False


def _is_table_or_matrix_type(visual_type: Any) -> bool:
    normalized = str(visual_type or "").casefold()
    return "table" in normalized or "matrix" in normalized


def visual_inventory(state: Any) -> list[dict[str, Any]]:
    """Return the visual containers of one captured state.

    Names used for matching come from spatially matched title evidence when
    title items are available. Screenshot-detected names are retained
    separately for display when title evidence has no spatial match.
    """
    if _is_scroll_screenshot_state(state):
        return []
    visuals: list[dict[str, Any]] = []
    captured_visuals = getattr(state, "visuals", None) or []
    if captured_visuals:
        extracted = state.extracted or {}
        extracted_elements = extracted.get("elements") or []
        for visual in captured_visuals:
            if not isinstance(visual, dict):
                continue
            if is_header_divider(visual):
                continue
            if (
                visual.get("scrollable")
                and "chart"
                in str(visual.get("visual_type") or "").casefold()
            ):
                continue
            geometry = _visual_geometry(visual)
            if geometry is None:
                continue
            visible_title = _visible_visual_title(
                extracted,
                visual,
            )
            visual_name = (
                visible_title
                if "title_items" in extracted
                else visual.get("visual_name")
            )
            item = {
                "visual_id": visual.get("visual_id")
                or f"visual_{len(visuals) + 1}",
                "visual_name": visual_name or None,
                "visual_type": _visual_type_from_evidence(visual),
                **geometry,
            }
            display_title = visible_title or visual.get("visual_name")
            if display_title and display_title != visual_name:
                item["visual_title"] = display_title
            for key in ("rendering_status", "confidence"):
                if visual.get(key) is not None:
                    item[key] = visual[key]
            categories = visual.get("categories") or []
            extracted_visual = next(
                (
                    element
                    for element in extracted_elements
                    if isinstance(element, dict)
                    and str(element.get("element_id") or "").casefold()
                    == str(visual.get("visual_id") or "").casefold()
                ),
                None,
            )
            extracted_categories = (
                extracted_visual.get("categories") or []
                if extracted_visual is not None
                else []
            )
            categories = list(
                dict.fromkeys(
                    [
                        *(
                            str(value).strip()
                            for value in categories
                            if str(value).strip()
                        ),
                        *(
                            str(value).strip()
                            for value in extracted_categories
                            if str(value).strip()
                        ),
                    ]
                )
            )
            if not categories and "title_items" in extracted:
                categories = [
                    t["text"]
                    for t in (extracted.get("title_items") or [])
                    if t.get("category") == "column_header" and t.get("text")
                ]
            item["categories"] = categories
            rendering_status = str(
                item.get("rendering_status") or ""
            ).casefold()
            is_kpi = item["visual_type"].casefold() in {"kpi", "kpi card"}
            visuals.append(item)
        return visuals

    elements = (state.extracted or {}).get("elements") or []
    for element in elements:
        if not isinstance(element, dict):
            continue
        if is_header_divider(element):
            continue
        element_type = _normalize_type(element.get("element_type") or element.get("type"))
        if element_type not in _VISUAL_CONTAINER_TYPES:
            continue
        geometry = _visual_geometry(element)
        if geometry is None:
            continue
        categories = element.get("categories") or []
        if not categories and "title_items" in (state.extracted or {}):
            categories = [
                t["text"]
                for t in ((state.extracted or {}).get("title_items") or [])
                if t.get("category") == "column_header" and t.get("text")
            ]
        visuals.append(
            {
                "visual_id": element.get("element_id") or f"{element_type}_{len(visuals) + 1}",
                "visual_name": element.get("element_text") or element.get("title") or None,
                "visual_type": classify_visual_type(element_type),
                "categories": categories,
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
        applied_for_name = [s.value for s in applied if s.name == name]
        if not has_captured_options and (
            name not in selected_by_name or applied_for_name
        ):
            selected = applied_for_name
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
    header_schemas: dict[tuple[str, str], list[str]] = {}
    header_schemas_by_type: dict[tuple[str, str], list[str]] = {}

    for state in [*states_a, *states_b]:
        if _is_scroll_screenshot_state(state):
            continue
        page_key = " ".join(
            str(getattr(state, "page", "") or "").split()
        ).casefold()
        for visual in visual_inventory(state):
            if not _is_table_or_matrix_type(visual.get("visual_type")):
                continue
            categories = [
                str(value).strip()
                for value in (visual.get("categories") or [])
                if str(value).strip()
            ]
            if not categories:
                continue
            visual_key = " ".join(
                str(visual.get("visual_id") or visual.get("visual_name") or "").split()
            ).casefold()
            key = (page_key, visual_key)
            existing = header_schemas.get(key, [])
            if len(categories) > len(existing):
                header_schemas[key] = list(dict.fromkeys(categories))
            type_key = (
                page_key,
                str(visual.get("visual_type") or "").casefold(),
            )
            type_existing = header_schemas_by_type.get(type_key, [])
            if len(categories) > len(type_existing):
                header_schemas_by_type[type_key] = list(dict.fromkeys(categories))

    for state_a in states_a:
        state_b = by_key_b.get(state_a.state_key)
        if state_b is None:
            if not _is_scroll_screenshot_state(state_a):
                unmatched.append(f"A only: {state_a.state_key}")
            continue
        matched_keys.add(state_a.state_key)
        if not (_is_scroll_screenshot_state(state_a) or _is_scroll_screenshot_state(state_b)):
            inventory_a = visual_inventory(state_a)
            inventory_b = visual_inventory(state_b)
            for inventory in (inventory_a, inventory_b):
                for visual in inventory:
                    if not _is_table_or_matrix_type(visual.get("visual_type")):
                        continue
                    visual_key = " ".join(
                        str(
                            visual.get("visual_id")
                            or visual.get("visual_name")
                            or ""
                        ).split()
                    ).casefold()
                    schema = header_schemas.get(
                        (
                            " ".join(str(state_a.page or "").split()).casefold(),
                            visual_key,
                        ),
                        [],
                    )
                    type_schema = header_schemas_by_type.get(
                        (
                            " ".join(str(state_a.page or "").split()).casefold(),
                            str(visual.get("visual_type") or "").casefold(),
                        ),
                        [],
                    )
                    schema = list(dict.fromkeys([*schema, *type_schema]))
                    categories = [
                        str(value).strip()
                        for value in (visual.get("categories") or [])
                        if str(value).strip()
                    ]
                    visual["categories"] = list(
                        dict.fromkeys([*categories, *schema])
                    )
            current_visual_rows = compare_visual_inventories(
                state_a.page,
                inventory_a,
                inventory_b,
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
        f"B only: {key}"
        for key in by_key_b
        if key not in matched_keys and not _is_scroll_screenshot_state(by_key_b[key])
    )
    return {
        "visual_rows": visual_rows,
        "filter_rows": filter_rows,
        "unmatched_states": unmatched,
    }
