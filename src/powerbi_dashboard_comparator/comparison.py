import hashlib
import json
import math
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .docx_report import write_docx_report
from .models import DashboardState
from .standard_elements import reading_order, standard_element_name
from .timing import measure_stage, write_operation_event
from .visual_filter_comparison import build_visual_filter_result
from .visual_filter_report import (
    write_visual_validation_sheet,
)


def _canonical_value(value: Any) -> Any:
    if isinstance(value, str):
        text = " ".join(value.strip().split())
        candidate = text.replace(",", "").replace(" ", "")
        negative = candidate.startswith("(") and candidate.endswith(")")
        if negative:
            candidate = candidate[1:-1]
        candidate = re.sub(r"[$€£¥%]", "", candidate)
        multiplier = 1
        suffix = re.search(r"([kmb])$", candidate, re.IGNORECASE)
        if suffix:
            multiplier = {
                "k": 1_000,
                "m": 1_000_000,
                "b": 1_000_000_000,
            }[suffix.group(1).casefold()]
            candidate = candidate[:-1]
        try:
            number = float(candidate) * multiplier
        except ValueError:
            return text.casefold()
        return -number if negative else number
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonical_value(value[key]) for key in sorted(value)}
    return value


_EXPORT_DIMENSION_PATTERN = re.compile(
    r"\b(date|day|week|month|quarter|year|period|id|key|name|type|status|"
    r"team|area|segment|category|department|group|region|channel|resolver|"
    r"disposition)\b",
    re.IGNORECASE,
)
_EXPORT_DATE_PATTERN = re.compile(
    r"(?<!\d)(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})(?!\d)"
)


def _is_export_dimension_header(header: Any) -> bool:
    return bool(_EXPORT_DIMENSION_PATTERN.search(str(header or "")))


def _normalize_export_date(match: re.Match[str]) -> str:
    month, day, year = (int(part) for part in match.groups())
    if year < 100:
        year += 2000 if year < 70 else 1900
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return match.group(0)


def _canonical_export_value(value: Any, header: Any) -> Any:
    if isinstance(value, str) and _is_export_dimension_header(header):
        value = _EXPORT_DATE_PATTERN.sub(_normalize_export_date, value)
    return _canonical_value(value)


# Exported numbers are compared with a relative tolerance so floating-point
# noise in the last significant digits does not register as a difference.
_EXPORT_NUMERIC_REL_TOL = 1e-9


def _export_values_match(value_a: Any, value_b: Any, header: Any) -> bool:
    canonical_a = _canonical_export_value(value_a, header)
    canonical_b = _canonical_export_value(value_b, header)
    if canonical_a == canonical_b:
        return True
    if isinstance(canonical_a, float) and isinstance(canonical_b, float):
        return math.isclose(
            canonical_a, canonical_b, rel_tol=_EXPORT_NUMERIC_REL_TOL, abs_tol=0.0
        )
    return False


def _display(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return "" if value is None else str(value)


def _comparison_display(
    element: dict[str, Any] | None,
) -> str:
    if element is None:
        return ""

    content = element.get("content")
    content = content if isinstance(content, dict) else {}
    series = element.get("series") or content.get("series") or []
    categories = content.get("categories") or []
    if series and categories and all(isinstance(item, dict) for item in series):
        series_names = [
            str(item.get("name") or f"Series {index + 1}")
            for index, item in enumerate(series)
        ]
        rows = []
        for row_index, category in enumerate(categories):
            row = [_raw_comparison_value(category)]
            row.extend(
                _raw_comparison_value(
                    item.get("values", [])[row_index]
                    if row_index < len(item.get("values") or [])
                    else None
                )
                for item in series
            )
            rows.append(row)
        return _format_comparison_rows(["Category", *series_names], rows)

    rows = element.get("rows") or []
    if rows:
        headers = list(element.get("categories") or [])
        row_width = max(
            (
                len(row) if isinstance(row, (list, dict)) else 1
                for row in rows
            ),
            default=0,
        )
        if len(headers) != row_width or not all(
            isinstance(header, str) for header in headers
        ):
            headers = [f"Column {index + 1}" for index in range(row_width)]
        return _format_comparison_rows(headers, rows)

    value = _raw_comparison_value(element.get("value"))
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    categories = element.get("categories") or []
    if categories:
        return ", ".join(str(category) for category in categories)
    values = element.get("values") or []
    if values:
        return ", ".join(str(item) for item in values)
    return "No comparable values"


def _raw_comparison_value(value: Any) -> Any:
    while isinstance(value, dict) and len(value) == 1:
        if "raw" in value:
            value = value["raw"]
        elif "value" in value:
            value = value["value"]
        else:
            break
    return value


def _format_comparison_rows(
    headers: list[Any],
    rows: list[Any],
) -> str:
    header_keys = _header_identity_keys(headers)
    header_labels = dict(zip(header_keys, headers))
    ordered_keys = header_keys
    ordered_headers = [header_labels[key] for key in ordered_keys]
    index_by_key = {key: index for index, key in enumerate(header_keys)}

    formatted_rows = []
    for row in rows:
        cells = []
        for key, header in zip(ordered_keys, ordered_headers):
            index = index_by_key[key]
            cell = (
                row.get(headers[index])
                if isinstance(row, dict)
                else row[index]
                if isinstance(row, list) and index < len(row)
                else None
            )
            cell = _raw_comparison_value(cell)
            rendered = "null" if cell is None else str(cell)
            cells.append(f"{header}: {rendered}")
        formatted_rows.append("; ".join(cells))
    text = "\n".join(formatted_rows)
    if len(text) > 30_000:
        return (
            text[:29_900]
            + "\n... truncated; see comparison.json for all values"
        )
    return text


def _title_signature(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


_STANDARDIZED_TITLE_CATEGORIES = {
    **{
        _title_signature(value): "Dashboard Title"
        for value in (
            "HR Now - Compliance",
            "HR Now - Performance",
            "HR Now - Yearly Metrics",
        )
    },
    "hrnowreport": "Report Title",
    **{
        _title_signature(value): "Page Navigation"
        for value in ("Home", "Summary", "Performance", "Compliance", "Yearly Metrics")
    },
    **{
        _title_signature(value): "Compliance Visual Titles"
        for value in (
            "SLA Compliance % - Time to First Response by Service Team",
            "SLA Compliance % - Time to Resolve by Service Team",
        )
    },
    **{
        _title_signature(value): "Performance KPI Titles"
        for value in (
            "Average time to resolve (Days)",
            "Pending cases duration",
            "Resolution efficiency",
        )
    },
    **{
        _title_signature(value): "Performance Chart Titles"
        for value in (
            "Cases by Caller Type",
            "Cases Registered vs Cases Resolved",
            "Cases by Segment",
            "Cases Registered by Disposition Area",
        )
    },
    **{
        _title_signature(value): "Filter / Slicer Labels"
        for value in ("Disposition Area", "Service Team")
    },
    **{
        _title_signature(value): "Yearly Metrics Visual Titles"
        for value in (
            "Workday Cases",
            "Case details by Disposition Area",
            "Case details by Service Team",
        )
    },
    "howtoswitchtolegacysnfiscalcalendar": "Instruction / Annotation",
    **{
        _title_signature(value): "Metadata Labels"
        for value in ("Dashboard name", "Last refresh", "Data Refresh Date")
    },
    "successfulexport": "Export Status",
    "yourdatawasexported": "Export Status",
}
_COMPARISON_ONLY_TITLE_CATEGORIES = {"Other Navigation / Application Labels"}
_EXCLUDED_COMPARISON_TITLE_TYPES = {"brand_logo", "button_label"}
_TRANSIENT_EXPORT_STATUSES = {"successfulexport", "yourdatawasexported"}
_NON_DATA_LABEL_CATEGORIES = {
    "Dashboard branding",
    "Dashboard Title",
    "Report Title",
    "Page Navigation",
    "Instruction / Annotation",
    "Metadata Labels",
    "Other Navigation / Application Labels",
    "Filter / Slicer Labels",
}
_KEEP_STANDARDIZED_CATEGORIES = set(_STANDARDIZED_TITLE_CATEGORIES.values()) - {
    "Export Status"
}


def _standardized_title_category(
    text: Any, source_category: Any = None
) -> str | None:
    signature = _title_signature(text)
    category = _STANDARDIZED_TITLE_CATEGORIES.get(signature)
    if category:
        return category
    source = str(source_category or "").casefold().replace("-", "_").replace(" ", "_")
    fixed_categories = {
        "dashboard_title": "Dashboard Title",
        "report_title": "Report Title",
        "brand_logo": "Dashboard branding",
        "page_title": "Page Titles",
        "page_navigation": "Page Navigation",
        "navigation_label": "Page Navigation",
        "slicer_title": "Filter / Slicer Labels",
        "slicer_value": "Filter / Slicer Labels",
        "filter_title": "Filter / Slicer Labels",
        "metadata_label": "Metadata Labels",
        "metadata_value": "Metadata Labels",
        "visual_title": "Visual Titles",
        "chart_title": "Visual Titles",
        "table_title": "Visual Titles",
        "matrix_title": "Visual Titles",
        "kpi_title": "Visual Titles",
        "legend_item": "Visual Labels",
        "legend_title": "Visual Labels",
        "column_header": "Visual Labels",
        "row_header": "Visual Labels",
        "icon": "Visual Labels",
    }
    if source in fixed_categories:
        return fixed_categories[source]
    if any(token in source for token in ("navigation", "application", "toolbar", "workspace")):
        return "Other Navigation / Application Labels"
    return None


def _is_transient_export_status(item: dict[str, Any]) -> bool:
    return any(
        _title_signature(item.get(field)) in _TRANSIENT_EXPORT_STATUSES
        for field in ("text", "title", "value")
    )


def _is_application_chrome(item: dict[str, Any]) -> bool:
    element_type = str(
        item.get("element_type") or item.get("type") or ""
    ).casefold().replace("-", "_")
    category = str(
        item.get("category") or item.get("standard_element_name") or ""
    ).casefold().replace("-", "_")
    is_brand_logo = (
        category == "brand_logo"
        or item.get("standard_element_name") == "brand_logo"
        or element_type == "brand_logo"
    )
    if item.get("is_application_chrome") is True:
        return True
    if item.get("is_logo") is True and not is_brand_logo:
        return True
    if any(token in category for token in ("chrome", "toolbar", "workspace", "platform")):
        return True
    if category in {
        "application_navigation",
        "application_sidebar",
        "host_navigation",
        "workspace_navigation",
        "viewer_control",
        "logo",
        "brand_mark",
    }:
        return True

    label = _title_signature(item.get("text") or item.get("title") or item.get("value"))
    box = item.get("bounding_box")
    if not isinstance(box, dict):
        return False
    x, y = box.get("x"), box.get("y")
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return False

    sidebar_labels = {
        "pages", "apps", "scorecards", "browse", "learn", "realtime",
        "workloads", "workspaces", "onelakecatalog",
    }
    toolbar_labels = {
        "search", "file", "export", "share", "explore", "subscribe",
        "setalert", "monitor", "hrnowreport",
    }
    is_text_control = element_type in {
        "text", "button", "navigation", "metadata", "other"
    } or category in {"navigation_label", "button_label", "section_title"}
    return (
        not is_brand_logo
        and (
            (x < 150 and label in sidebar_labels)
            or (y < 70 and label in toolbar_labels)
            or (
                is_text_control
                and y < 70
                and category in {"navigation_label", "button_label"}
            )
        )
    )


def _is_excluded_navigation(item: dict[str, Any]) -> bool:
    element_type = str(
        item.get("element_type") or item.get("type") or ""
    ).casefold().replace("-", "_")
    category = str(
        item.get("category") or item.get("standard_element_name") or ""
    ).casefold().replace("-", "_")
    label = " ".join(
        str(item.get("text") or item.get("title") or item.get("value") or "")
        .casefold()
        .split()
    )
    box = item.get("bounding_box") or item.get("normalized_bounding_box") or {}
    x = box.get("x") if isinstance(box, dict) else None
    left_page_navigation = (
        (category in {"page_title", "page_name"} or element_type in {"page_title", "page_name"})
        and isinstance(x, (int, float))
        and (x < 0.1 if 0 <= x <= 1 else x < 180)
    )
    return (
        element_type in {"navigation", "navigator", "page_navigator"}
        or category
        in {
            "page_navigation",
            "navigation_label",
            "navigator",
            "page_navigator",
            "navigation_button",
            "application_navigation",
        }
        or label in {"home", "pages"}
        or left_page_navigation
    )


def _element_map(
    extracted: dict[str, Any], filter_names: list[str] | None = None
) -> dict[tuple[str, str], dict[str, Any]]:
    counts: defaultdict[tuple[str, str], int] = defaultdict(int)
    elements: dict[tuple[str, str], dict[str, Any]] = {}
    ignored_titles = {_title_signature(name) for name in filter_names or []}
    for element in extracted.get("elements", []):
        if not isinstance(element, dict):
            continue
        if _is_transient_export_status(element) or _is_excluded_navigation(element):
            continue
        element_type = standard_element_name(
            element.get("standard_element_name") or element.get("category"),
            element.get("element_type") or element.get("type"),
            element.get("element_text") or element.get("title") or element.get("value"),
        )
        if element_type in _EXCLUDED_COMPARISON_TITLE_TYPES:
            continue
        standardized_category = _standardized_title_category(
            element.get("title") or element.get("value"),
            element.get("category") or element.get("element_type"),
        )
        if (
            _is_application_chrome(element)
            and standardized_category not in _KEEP_STANDARDIZED_CATEGORIES
        ):
            continue
        kind = str(element.get("type") or "other").strip().casefold()
        if kind in {"text", "button", "navigation", "metadata"}:
            continue
        if (
            kind == "other"
            and standardized_category in _NON_DATA_LABEL_CATEGORIES
        ):
            continue
        title = _title_signature(element.get("title")) or _title_signature(
            element.get("element_id")
        )
        if not title or title in ignored_titles:
            continue
        categories = element.get("categories") or []
        values = element.get("values") or []
        rows = element.get("rows") or []
        value = element.get("value")
        if kind in {"filter", "slicer"}:
            continue
        if kind == "other" and not rows and not values and not categories:
            continue
        if kind in {"other", "text"} and not rows and not values and categories:
            continue
        if (
            kind in {"other", "text"}
            and not rows
            and isinstance(value, str)
            and _canonical_value(value) in {"all", "select all"}
        ):
            continue
        if (
            value is None
            and not categories
            and not values
            and not rows
        ):
            continue
        base = (kind, title)
        index = counts[base]
        counts[base] += 1
        elements[(kind, f"{title}#{index}")] = element
    return elements


def _table_headers(element: dict[str, Any]) -> tuple[str, ...] | None:
    kind = str(element.get("type") or "").casefold()
    title = _title_signature(element.get("title"))
    if kind not in {"table", "grid"} and "table" not in title and "data" not in title:
        return None
    headers = element.get("categories")
    if not isinstance(headers, list) or len(headers) < 3:
        headers = element.get("rows", [[]])
        headers = headers[0] if headers and isinstance(headers[0], list) else []
    if len(headers) < 3 or not all(isinstance(header, str) for header in headers):
        return None
    normalized = tuple(_title_signature(header) for header in headers)
    return normalized if all(normalized) else None


def _pair_elements(
    map_a: dict[tuple[str, str], dict[str, Any]],
    map_b: dict[tuple[str, str], dict[str, Any]],
) -> list[tuple[tuple[str, str] | None, tuple[str, str] | None]]:
    keys_a = _ordered_item_keys(map_a)
    keys_b = _ordered_item_keys(map_b)
    aligned = _align_ordered_items(
        [map_a[key] for key in keys_a],
        [map_b[key] for key in keys_b],
    )
    return [
        (
            keys_a[index_a] if index_a is not None else None,
            keys_b[index_b] if index_b is not None else None,
        )
        for index_a, index_b, _ in aligned
    ]


def _ordered_item_keys(
    elements: dict[tuple[str, str], dict[str, Any]],
) -> list[tuple[str, str]]:
    indexed = list(elements.items())
    return [
        entry[1][0]
        for entry in sorted(
            enumerate(indexed, 1),
            key=lambda entry: (
                _extracted_order(entry[1][1].get("reading_order"), entry[0]),
                entry[0],
            ),
        )
    ]


def _alignment_text(item: dict[str, Any]) -> str:
    value = item.get("text")
    if value is None:
        value = item.get("element_text")
    if value is None:
        value = item.get("title")
    if value is None:
        value = item.get("value")
    text = _normalized_title_text(value)
    if _alignment_category(item) == "brandlogo":
        return text.rstrip(".,;:!?")
    return text


def _alignment_category(item: dict[str, Any]) -> str:
    value = (
        item.get("standard_element_name")
        or item.get("element_type")
        or item.get("category")
        or item.get("type")
    )
    category = _title_signature(value)
    if category in {"", "other", "otherlabel", "unknown", "undefined"}:
        return ""
    return {
        "filtertitle": "slicertitle",
        "filtervalue": "slicervalue",
    }.get(category, category)


def _alignment_group(item: dict[str, Any]) -> str:
    value = (
        item.get("group")
        or item.get("visual_group")
        or item.get("section")
        or item.get("parent_visual")
    )
    return _title_signature(value)


def _alignment_structure(item: dict[str, Any]) -> tuple[str, ...] | None:
    headers = _table_headers(item)
    if headers:
        return ("table", *headers)
    categories = item.get("categories")
    if isinstance(categories, list) and categories:
        return (
            str(item.get("type") or "").casefold(),
            *(_title_signature(category) for category in categories),
        )
    return None


def _alignment_pair_cost(
    item_a: dict[str, Any], item_b: dict[str, Any]
) -> float:
    text_a = _alignment_text(item_a)
    text_b = _alignment_text(item_b)
    category_a = _alignment_category(item_a)
    category_b = _alignment_category(item_b)
    if {category_a, category_b} == {"reporttitle", "dashboardtitle"}:
        return 2.0
    association_a = _title_signature(item_a.get("associated_visual"))
    association_b = _title_signature(item_b.get("associated_visual"))
    group_a = _alignment_group(item_a)
    group_b = _alignment_group(item_b)
    distance = _title_distance(item_a, item_b)
    same_category = bool(category_a and category_a == category_b)
    same_association = bool(association_a and association_a == association_b)
    same_group = bool(group_a and group_a == group_b)
    structure_a = _alignment_structure(item_a)
    structure_b = _alignment_structure(item_b)
    same_structure = bool(structure_a and structure_a == structure_b)

    if text_a and text_b and text_a == text_b:
        cost = 0.05
    elif text_a and text_b:
        similarity = SequenceMatcher(None, text_a, text_b).ratio()
        if similarity >= 0.9:
            cost = 0.25
        elif similarity >= 0.72:
            cost = 0.55
        elif same_structure:
            cost = 0.75
        else:
            cost = 1.45
    else:
        cost = 0.9 if not text_a and not text_b else 1.25

    if category_a and category_b:
        cost += -0.18 if same_category else 0.35
    else:
        cost += 0.2
    if group_a and group_b:
        cost += -0.15 if same_group else 0.25
    if association_a and association_b:
        cost += -0.15 if same_association else 0.3
    if same_structure:
        cost -= 0.15
    data_a = (
        _comparable_element_values(item_a)
        if _has_comparable_data(item_a)
        else {}
    )
    data_b = (
        _comparable_element_values(item_b)
        if _has_comparable_data(item_b)
        else {}
    )
    if data_a and data_b:
        cost += (
            -0.15
            if _canonical_value(data_a) == _canonical_value(data_b)
            else 0.2
        )
    if distance is not None:
        if distance <= 0.02:
            cost -= 0.2
            if same_category:
                cost -= 0.25
        elif distance > 0.08:
            cost += min(distance, 0.5) * 0.2
    return max(0.0, cost)


def _alignment_confidence(item_a: dict[str, Any], item_b: dict[str, Any]) -> str:
    text_a = _alignment_text(item_a)
    text_b = _alignment_text(item_b)
    category_a = _alignment_category(item_a)
    category_b = _alignment_category(item_b)
    association_a = _title_signature(item_a.get("associated_visual"))
    association_b = _title_signature(item_b.get("associated_visual"))
    group_a = _alignment_group(item_a)
    group_b = _alignment_group(item_b)
    distance = _title_distance(item_a, item_b)
    same_type = bool(category_a and category_b and category_a == category_b)
    known_types = bool(category_a and category_b)
    same_association = bool(
        association_a and association_b and association_a == association_b
    )
    same_group = bool(group_a and group_b and group_a == group_b)
    close_coordinates = distance is not None and distance <= 0.02
    structure_a = _alignment_structure(item_a)
    structure_b = _alignment_structure(item_b)
    same_structure = bool(structure_a and structure_a == structure_b)
    same_text = bool(text_a and text_b and text_a == text_b)
    similarity = (
        SequenceMatcher(None, text_a, text_b).ratio()
        if text_a and text_b
        else 0.0
    )
    data_a = (
        _comparable_element_values(item_a)
        if _has_comparable_data(item_a)
        else {}
    )
    data_b = (
        _comparable_element_values(item_b)
        if _has_comparable_data(item_b)
        else {}
    )
    same_data = bool(
        data_a
        and data_b
        and _canonical_value(data_a) == _canonical_value(data_b)
    )
    contextual_support = (
        same_type or same_association or same_group or close_coordinates or same_structure
    )

    if same_text and same_type:
        return "high"
    if not text_a and not text_b and same_type and same_data:
        return "medium"
    if same_text and (same_association or same_group or close_coordinates or same_structure):
        return "high"
    if same_text and known_types:
        return "medium"
    if similarity >= 0.9 and contextual_support:
        return "high"
    if similarity >= 0.72 and contextual_support:
        return "medium"
    if (
        close_coordinates
        and (same_type or same_association or same_group)
    ) or (same_structure and (same_type or same_association or same_group)):
        return "medium"
    return "low"


def _has_competing_alignment(
    items_a: list[dict[str, Any]],
    items_b: list[dict[str, Any]],
    index_a: int,
    index_b: int,
) -> bool:
    item_a = items_a[index_a]
    item_b = items_b[index_b]
    text_a = _alignment_text(item_a)
    text_b = _alignment_text(item_b)
    if not text_a or text_a != text_b:
        return False
    current_cost = _alignment_pair_cost(item_a, item_b)
    return any(
        _alignment_pair_cost(item_a, candidate) <= current_cost + 0.05
        for candidate_index, candidate in enumerate(items_b)
        if candidate_index != index_b and _alignment_text(candidate) == text_a
    ) or any(
        _alignment_pair_cost(candidate, item_b) <= current_cost + 0.05
        for candidate_index, candidate in enumerate(items_a)
        if candidate_index != index_a and _alignment_text(candidate) == text_b
    )


def _align_ordered_items(
    items_a: list[dict[str, Any]],
    items_b: list[dict[str, Any]],
    gap_cost: float = 0.6,
) -> list[tuple[int | None, int | None, float | None]]:
    """Align ordered extractions while allowing insertions and deletions."""
    n, m = len(items_a), len(items_b)
    costs = [[0.0] * (m + 1) for _ in range(n + 1)]
    back: list[list[str | None]] = [[None] * (m + 1) for _ in range(n + 1)]

    for i in range(1, n + 1):
        costs[i][0] = i * gap_cost
        back[i][0] = "A_ONLY"
    for j in range(1, m + 1):
        costs[0][j] = j * gap_cost
        back[0][j] = "B_ONLY"

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            pair_cost = _alignment_pair_cost(items_a[i - 1], items_b[j - 1])
            choices = (
                (costs[i - 1][j - 1] + pair_cost, "PAIR"),
                (costs[i - 1][j] + gap_cost, "A_ONLY"),
                (costs[i][j - 1] + gap_cost, "B_ONLY"),
            )
            minimum_cost = min(choice[0] for choice in choices)
            tied = [
                choice for choice in choices if choice[0] == minimum_cost
            ]
            tied_moves = {choice[1] for choice in tied}
            preferred_move = (
                "A_ONLY"
                if "PAIR" in tied_moves and "A_ONLY" in tied_moves
                else "B_ONLY"
                if "PAIR" not in tied_moves and "B_ONLY" in tied_moves
                else tied[0][1]
            )
            costs[i][j] = minimum_cost
            back[i][j] = preferred_move

    aligned: list[tuple[int | None, int | None, float | None]] = []
    i, j = n, m
    while i > 0 or j > 0:
        move = back[i][j]
        if move == "PAIR":
            pair_cost = _alignment_pair_cost(items_a[i - 1], items_b[j - 1])
            aligned.append((i - 1, j - 1, pair_cost))
            i -= 1
            j -= 1
        elif move == "A_ONLY":
            aligned.append((i - 1, None, None))
            i -= 1
        elif move == "B_ONLY":
            aligned.append((None, j - 1, None))
            j -= 1
        else:
            raise ValueError("Invalid dashboard sequence-alignment state")

    aligned.reverse()
    return aligned


def _extracted_order(value: Any, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        position = float(value)
    except (TypeError, ValueError):
        return fallback
    if position < 1 or not position.is_integer():
        return fallback
    return int(position)


def _element_label(element: dict[str, Any]) -> str:
    kind = str(element.get("type") or "").casefold()
    value = element.get("value")
    if kind in {"text", "button", "navigation"} and isinstance(value, str) and value:
        return value
    title = element.get("title")
    if title and _title_signature(title) != "untitledvisual":
        return str(title)
    return "Untitled visual"


def _normalized_title_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _title_box(item: dict[str, Any]) -> tuple[float, float, float, float] | None:
    box = item.get("normalized_bounding_box")
    if not isinstance(box, dict):
        return None
    values = tuple(box.get(key) for key in ("x", "y", "width", "height"))
    if not all(isinstance(value, (int, float)) for value in values):
        return None
    x, y, width, height = values
    return float(x), float(y), float(width), float(height)


def _source_title_box(item: dict[str, Any]) -> dict[str, Any] | None:
    box = item.get("bounding_box")
    if not isinstance(box, dict):
        return None
    return {key: box.get(key) for key in ("x", "y", "width", "height")}


def _normalized_title_box(item: dict[str, Any]) -> dict[str, Any] | None:
    box = item.get("normalized_bounding_box")
    if not isinstance(box, dict):
        return None
    return {key: box.get(key) for key in ("x", "y", "width", "height")}


def _title_distance(
    item_a: dict[str, Any], item_b: dict[str, Any]
) -> float | None:
    box_a = _title_box(item_a)
    box_b = _title_box(item_b)
    if box_a is None or box_b is None:
        return None
    center_a = (box_a[0] + box_a[2] / 2, box_a[1] + box_a[3] / 2)
    center_b = (box_b[0] + box_b[2] / 2, box_b[1] + box_b[3] / 2)
    return ((center_a[0] - center_b[0]) ** 2 + (center_a[1] - center_b[1]) ** 2) ** 0.5


def _title_item_map(extracted: dict[str, Any]) -> list[dict[str, Any]]:
    items = extracted.get("title_items")
    if isinstance(items, list):
        result = []
        for item in items:
            if (
                not isinstance(item, dict)
                or _is_excluded_navigation(item)
            ):
                continue
            if _is_transient_export_status(item):
                continue
            standard_name = standard_element_name(
                item.get("standard_element_name") or item.get("category"),
                item.get("element_type"),
                item.get("element_text") or item.get("text"),
            )
            if standard_name in _EXCLUDED_COMPARISON_TITLE_TYPES:
                continue
            category = _standardized_title_category(
                item.get("text"), item.get("category")
            )
            if category in _COMPARISON_ONLY_TITLE_CATEGORIES:
                continue
            if (
                _is_application_chrome(item)
                and category not in _KEEP_STANDARDIZED_CATEGORIES
            ):
                continue
            item = dict(item)
            item["standard_element_name"] = standard_name
            item["element_type"] = standard_name
            item["element_text"] = item.get("element_text") or item.get("text")
            result.append(item)
        if all(
            _extracted_order(item.get("reading_order"), 0) > 0 for item in result
        ):
            return [
                item
                for _, item in sorted(
                    enumerate(result, 1),
                    key=lambda indexed: (
                        _extracted_order(indexed[1].get("reading_order"), indexed[0]),
                        indexed[0],
                    ),
                )
            ]
        return reading_order(result)

    legacy_items = []
    for element in extracted.get("elements", []):
        if (
            not isinstance(element, dict)
            or not element.get("title")
            or _is_transient_export_status(element)
            or _is_excluded_navigation(element)
        ):
            continue
        standard_name = standard_element_name(
            element.get("standard_element_name") or element.get("category"),
            element.get("element_type") or element.get("type"),
            element.get("element_text") or element.get("title"),
        )
        if standard_name in _EXCLUDED_COMPARISON_TITLE_TYPES:
            continue
        category = _standardized_title_category(
            element.get("title"),
            element.get("category") or element.get("element_type"),
        )
        if category in _COMPARISON_ONLY_TITLE_CATEGORIES:
            continue
        if (
            _is_application_chrome(element)
            and category not in _KEEP_STANDARDIZED_CATEGORIES
        ):
            continue
        legacy_items.append(
            {
                "text": element["title"],
                "category": str(element.get("category") or element.get("type") or "visual_title") + "_title",
                "standard_element_name": standard_name,
                "element_type": standard_name,
                "element_text": element.get("element_text") or element["title"],
                "associated_visual": element.get("title"),
                "reading_order": element.get("reading_order"),
                "bounding_box": element.get("bounding_box"),
                "normalized_bounding_box": element.get("normalized_bounding_box"),
            }
        )
    return legacy_items


def _bookmark_association_labels(
    *states: DashboardState | None,
) -> dict[str, str]:
    labels: dict[str, str] = {}
    for state in states:
        if state is None:
            continue
        for name in state.filter_values:
            options = _bookmark_group_options(name)
            if not options:
                continue
            label = " / ".join(options)
            for option in options:
                labels.setdefault(_title_signature(option), label)
    return labels


def _apply_bookmark_associations(
    items: list[dict[str, Any]], labels: dict[str, str]
) -> list[dict[str, Any]]:
    if not labels:
        return items
    result = []
    for item in items:
        label = labels.get(_title_signature(item.get("text") or item.get("element_text")))
        if label is None or _title_signature(item.get("associated_visual")) == _title_signature(
            item.get("text") or item.get("element_text")
        ):
            result.append(item)
            continue
        result.append({**item, "associated_visual": label})
    return result


def _merge_metadata_label_values(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    index = 0
    while index < len(items):
        item = items[index]
        following = items[index + 1] if index + 1 < len(items) else None
        label = item.get("text") or item.get("element_text")
        value = (following.get("text") or following.get("element_text")) if following else None
        if (
            item.get("standard_element_name") == "metadata_label"
            and following is not None
            and following.get("standard_element_name") == "metadata_value"
            and label
            and value
        ):
            combined = f"{str(label).strip()} {str(value).strip()}"
            merged.append({**item, "text": combined, "element_text": combined})
            index += 2
            continue
        merged.append(item)
        index += 1
    return merged


def _compare_title_items(
    state_key: str,
    state_a: DashboardState | None,
    state_b: DashboardState | None,
) -> list[dict[str, Any]]:
    bookmark_labels = _bookmark_association_labels(state_a, state_b)
    items_a = _merge_metadata_label_values(
        _apply_bookmark_associations(
            _title_item_map(state_a.extracted) if state_a else [], bookmark_labels
        )
    )
    items_b = _merge_metadata_label_values(
        _apply_bookmark_associations(
            _title_item_map(state_b.extracted) if state_b else [], bookmark_labels
        )
    )
    association_aliases_b: dict[str, str] = {}
    if state_a and state_b:
        extracted_a = state_a.extracted
        extracted_b = state_b.extracted
        if isinstance(extracted_a.get("screenshot_elements"), list):
            extracted_a = {
                **extracted_a,
                "elements": extracted_a["screenshot_elements"],
            }
        if isinstance(extracted_b.get("screenshot_elements"), list):
            extracted_b = {
                **extracted_b,
                "elements": extracted_b["screenshot_elements"],
            }
        visual_map_a = _element_map(extracted_a, state_a.filter_names)
        visual_map_b = _element_map(extracted_b, state_b.filter_names)
        for key_a, key_b in _pair_elements(visual_map_a, visual_map_b):
            visual_a = visual_map_a.get(key_a) if key_a is not None else None
            visual_b = visual_map_b.get(key_b) if key_b is not None else None
            if visual_a and visual_b:
                title_a = visual_a.get("title") or visual_a.get("associated_visual")
                title_b = visual_b.get("title") or visual_b.get("associated_visual")
                if title_a and title_b:
                    association_aliases_b[_title_signature(title_b)] = str(title_a)

    scoring_items_b = [
        {
            **item,
            "associated_visual": association_aliases_b.get(
                _title_signature(item.get("associated_visual")),
                item.get("associated_visual"),
            ),
        }
        for item in items_b
    ]
    aligned_items = _align_ordered_items(items_a, scoring_items_b)
    matches: list[tuple[int | None, int | None, str, float | None, str]] = []
    for index_a, index_b, alignment_cost in aligned_items:
        if index_a is None or index_b is None:
            matches.append(
                (
                    index_a,
                    index_b,
                    "added" if index_a is None else "removed",
                    None,
                    "not_applicable",
                )
            )
            continue
        item_a = items_a[index_a]
        item_b = scoring_items_b[index_b]
        confidence = _alignment_confidence(item_a, item_b)
        if _has_competing_alignment(items_a, scoring_items_b, index_a, index_b):
            confidence = "low"
        text_a = _alignment_text(item_a)
        text_b = _alignment_text(item_b)
        text_same = text_a == text_b
        text_available = bool(text_a and text_b)
        category_same = bool(
            _alignment_category(item_a)
            and _alignment_category(item_a) == _alignment_category(item_b)
        )
        association_same = (
            _title_signature(item_a.get("associated_visual"))
            == _title_signature(item_b.get("associated_visual"))
        )
        distance = _title_distance(item_a, item_b)
        position_status = (
            "not_comparable"
            if distance is None
            else "moved"
            if distance > 0.08
            else "unchanged"
        )
        if confidence == "low":
            action = "ambiguous_alignment"
        elif not text_available:
            action = "extraction_uncertain"
        elif not text_same and item_a.get("standard_element_name") == "metadata_value":
            action = "metadata_changed"
        elif not text_same and item_a.get("standard_element_name") == "report_title":
            action = "identity_changed"
        elif text_same and not category_same:
            action = "classification_changed"
        elif text_same and not association_same:
            action = "association_changed"
        elif not category_same or not association_same:
            action = "reclassified"
        elif not text_same:
            action = "renamed"
        elif position_status == "moved":
            action = "moved"
        else:
            action = "unchanged"
        matches.append((index_a, index_b, action, alignment_cost, confidence))

    rows = []
    for aligned_position, (
        index_a,
        index_b,
        action,
        alignment_cost,
        alignment_confidence,
    ) in enumerate(matches, 1):
        item_a = items_a[index_a] if index_a is not None else None
        item_b = items_b[index_b] if index_b is not None else None
        source_item = item_a or item_b or {}
        distance = (
            _title_distance(item_a, item_b)
            if item_a is not None and item_b is not None
            else None
        )
        position_status = (
            "not_comparable"
            if distance is None
            else "moved"
            if distance > 0.08
            else "unchanged"
        )
        if alignment_cost is None:
            match_method, confidence = "unmatched", "not_applicable"
        else:
            match_method = "sequence_alignment"
            confidence = alignment_confidence
        status = {
            "unchanged": "MATCH",
            "moved": "INFO",
            "metadata_changed": "INFO",
            "identity_changed": "INFO",
            "association_changed": "INFO",
            "classification_changed": "UNVERIFIED",
            "added": "UNVERIFIED",
            "removed": "UNVERIFIED",
            "extraction_uncertain": "UNVERIFIED",
            "ambiguous_alignment": "UNVERIFIED",
        }.get(action, "DIFFERENT")
        rows.append(
            {
                "state": state_key,
                "aligned_position": aligned_position,
                "element": str(source_item.get("text") or "(unlabeled title)"),
                "dashboard_a": (
                    "not extracted" if item_a is None else str(item_a.get("text") or "")
                ),
                "dashboard_b": (
                    "not extracted" if item_b is None else str(item_b.get("text") or "")
                ),
                "status": status,
                "title_status": action,
                "alignment_cost": alignment_cost,
                "standard_element_name": (
                    item_a.get("standard_element_name")
                    if item_a
                    else item_b.get("standard_element_name")
                    if item_b
                    else None
                ),
                "standard_element_name_a": (
                    item_a.get("standard_element_name") if item_a else None
                ),
                "standard_element_name_b": (
                    item_b.get("standard_element_name") if item_b else None
                ),
                "element_type_a": item_a.get("element_type") if item_a else None,
                "element_type_b": item_b.get("element_type") if item_b else None,
                "order_a": (
                    _extracted_order(item_a.get("reading_order"), index_a + 1)
                    if item_a
                    else None
                ),
                "order_b": (
                    _extracted_order(item_b.get("reading_order"), index_b + 1)
                    if item_b
                    else None
                ),
                "element_text_a": item_a.get("element_text") if item_a else None,
                "element_text_b": item_b.get("element_text") if item_b else None,
                "extraction_status_a": (
                    item_a.get("extraction_status") if item_a else None
                ),
                "extraction_status_b": (
                    item_b.get("extraction_status") if item_b else None
                ),
                "extraction_uncertainties_a": (
                    item_a.get("uncertainties") or [] if item_a else []
                ),
                "extraction_uncertainties_b": (
                    item_b.get("uncertainties") or [] if item_b else []
                ),
                "evidence_sources_a": (
                    item_a.get("evidence_sources") if item_a else None
                ),
                "evidence_sources_b": (
                    item_b.get("evidence_sources") if item_b else None
                ),
                "classification_variance": (
                    bool(
                        _alignment_category(item_a)
                        and _alignment_category(item_a)
                        == _alignment_category(item_b)
                        and (
                            item_a.get("standard_element_name")
                            != item_b.get("standard_element_name")
                            or item_a.get("category") != item_b.get("category")
                        )
                    )
                    if item_a is not None and item_b is not None
                    else False
                ),
                "text_normalization": (
                    "trailing_logo_punctuation_ignored"
                    if (
                        item_a is not None
                        and item_b is not None
                        and _alignment_category(item_a) == "brandlogo"
                        and _normalized_title_text(item_a.get("text"))
                        != _normalized_title_text(item_b.get("text"))
                        and text_same
                    )
                    else None
                ),
                "position_status": position_status,
                "position_distance": distance,
                "match_method": match_method,
                "confidence": confidence,
                "alignment_status": (
                    "ambiguous_alignment"
                    if action == "ambiguous_alignment"
                    else "sequence_aligned"
                    if alignment_cost is not None
                    else action
                ),
                "category_a": item_a.get("category") if item_a else None,
                "category_b": item_b.get("category") if item_b else None,
                "standardized_category_a": (
                    _standardized_title_category(
                        item_a.get("text"), item_a.get("category")
                    )
                    if item_a
                    else None
                ),
                "standardized_category_b": (
                    _standardized_title_category(
                        item_b.get("text"), item_b.get("category")
                    )
                    if item_b
                    else None
                ),
                "associated_visual_a": item_a.get("associated_visual") if item_a else None,
                "associated_visual_b": item_b.get("associated_visual") if item_b else None,
                "bounding_box_a": _source_title_box(item_a) if item_a else None,
                "bounding_box_b": _source_title_box(item_b) if item_b else None,
                "normalized_bounding_box_a": (
                    _normalized_title_box(item_a) if item_a else None
                ),
                "normalized_bounding_box_b": (
                    _normalized_title_box(item_b) if item_b else None
                ),
            }
        )
    return rows


def _comparable_element_values(element: dict[str, Any]) -> dict[str, Any]:
    kind = str(element.get("type") or "").casefold()
    if kind in {"text", "button", "navigation", "metadata"}:
        value = element.get("value")
        if value is not None:
            return {"value": value}
    categories = list(element.get("categories") or [])
    rows = list(element.get("rows") or [])
    if not categories and rows and _table_headers(
        {**element, "categories": [], "rows": [rows[0]]}
    ):
        categories = rows.pop(0)
    return {
        key: value
        for key, value in (
            ("value", element.get("value")),
            ("categories", categories),
            ("values", element.get("values") or []),
            ("rows", rows),
        )
        if key == "value" or value
    }


def _has_comparable_data(element: dict[str, Any]) -> bool:
    values = _comparable_element_values(element)
    if values.get("value") is not None:
        return True
    if any(value is not None for value in values.get("values", [])):
        return True
    rows = values.get("rows", [])
    for row in rows:
        if not isinstance(row, list):
            continue
        data_cells = row[1:] if len(row) > 1 else row
        if any(value is not None for value in data_cells):
            return True
    return False


def _comparison_data_object(
    element: dict[str, Any] | None, fallback_value: Any = None
) -> dict[str, Any]:
    values = _comparable_element_values(element) if element else {}
    return {
        "value": values.get("value", fallback_value),
        "categories": values.get("categories", []),
        "values": values.get("values", []),
        "rows": values.get("rows", []),
    }


def _is_informational_metadata(title: Any) -> bool:
    signature = _title_signature(title)
    informational_titles = {
        "dashboardname",
        "dashboardtitle",
        "reportname",
        "reporttitle",
        "lastrefresh",
        "lastrefreshdate",
        "lastrefreshtime",
        "dataupdated",
        "datarefreshed",
        "refreshedon",
        "refreshdatetime",
        "refreshdate",
        "hrnowreport",
    }
    return signature in informational_titles or any(
        signature.startswith(prefix)
        for prefix in ("datarefreshdate", "lastrefresh", "hrnowreport")
    )


def _export_option_map(
    states: list[DashboardState],
) -> dict[str, dict[str, dict[str, Any]]]:
    by_state: dict[str, dict[str, dict[str, Any]]] = {}
    for state in states:
        options = state.extracted.get("export_options")
        if not isinstance(options, list):
            continue
        state_options = by_state.setdefault(state.state_key.casefold(), {})
        for option in options:
            if (
                not isinstance(option, dict)
                or not option.get("title")
                or _is_application_chrome(option)
            ):
                continue
            key = _title_signature(option["title"])
            state_options.setdefault(key, option)
    return by_state


def _element_export_status(
    state: DashboardState | None, element: dict[str, Any] | None
) -> Any:
    if element is None:
        return None
    status = element.get("export_status")
    if status is not None or state is None:
        return status
    title = _title_signature(element.get("title"))
    if not title:
        return None
    for option in state.extracted.get("export_options", []):
        if (
            isinstance(option, dict)
            and _title_signature(option.get("title")) == title
        ):
            return option.get("export_status")
    return None


def _element_export_option(
    state: DashboardState | None, element: dict[str, Any] | None
) -> Any:
    if element is None:
        return None
    available = element.get("exportOptionAvailable")
    if available is not None or state is None:
        return available
    title = _title_signature(element.get("title"))
    if not title:
        return None
    for option in state.extracted.get("export_options", []):
        if (
            isinstance(option, dict)
            and _title_signature(option.get("title")) == title
        ):
            return option.get("exportOptionAvailable")
    return None


def _already_exported_status(
    state: DashboardState | None,
    element: dict[str, Any] | None,
    exported_element: dict[str, Any] | None,
) -> str:
    if state is None:
        return "Unknown"
    if exported_element is not None:
        return "Yes"
    export_status = _element_export_status(state, element)
    if export_status == "exported":
        return "Yes"
    if export_status is not None or _element_export_option(state, element) is False:
        return "No"
    return "Unknown"


def _export_status_label(status: Any) -> str:
    if status is None:
        return "Unknown"
    if status == "exported":
        return "Exported"
    if status == "downloaded_no_tabular_data":
        return "Downloaded (no tabular data)"
    return str(status).replace("_", " ").capitalize()


def _compare_export_options(
    states_a: list[DashboardState], states_b: list[DashboardState]
) -> list[dict[str, Any]]:
    options_a = _export_option_map(states_a)
    options_b = _export_option_map(states_b)
    state_map_a = {state.state_key.casefold(): state for state in states_a}
    state_map_b = {state.state_key.casefold(): state for state in states_b}
    rows = []
    for state_key in sorted(set(options_a) | set(options_b)):
        exported_data_a = _exported_elements(state_map_a.get(state_key))
        exported_data_b = _exported_elements(state_map_b.get(state_key))
        for title_key in sorted(
            set(options_a.get(state_key, {})) | set(options_b.get(state_key, {}))
        ):
            option_a = options_a.get(state_key, {}).get(title_key)
            option_b = options_b.get(state_key, {}).get(title_key)
            available_a = (
                option_a.get("exportOptionAvailable") if option_a is not None else None
            )
            available_b = (
                option_b.get("exportOptionAvailable") if option_b is not None else None
            )
            availability_status = (
                "UNVERIFIED"
                if available_a is None or available_b is None
                else "MATCH"
                if available_a == available_b
                else "DIFFERENT"
            )
            rows.append(
                {
                    "state": state_key,
                    "visual": (option_a or option_b or {}).get("title"),
                    "visual_a": option_a.get("title") if option_a else None,
                    "visual_b": option_b.get("title") if option_b else None,
                    "exportOptionAvailable_a": available_a,
                    "exportOptionAvailable_b": available_b,
                    "export_status_a": (
                        option_a.get("export_status") if option_a else None
                    ),
                    "export_status_b": (
                        option_b.get("export_status") if option_b else None
                    ),
                    "export_error_a": (
                        option_a.get("export_error") if option_a else None
                    ),
                    "export_error_b": (
                        option_b.get("export_error") if option_b else None
                    ),
                    "export_data_format_a": (
                        option_a.get("export_data_format") if option_a else None
                    ),
                    "export_data_format_b": (
                        option_b.get("export_data_format") if option_b else None
                    ),
                    "export_file_a": option_a.get("export_file") if option_a else None,
                    "export_file_b": option_b.get("export_file") if option_b else None,
                    "export_json_file_a": (
                        option_a.get("export_json_file") if option_a else None
                    ),
                    "export_json_file_b": (
                        option_b.get("export_json_file") if option_b else None
                    ),
                    "screenshot_a": option_a.get("screenshot") if option_a else None,
                    "screenshot_b": option_b.get("screenshot") if option_b else None,
                    "export_dialog_screenshot_a": (
                        option_a.get("export_dialog_screenshot") if option_a else None
                    ),
                    "export_dialog_screenshot_b": (
                        option_b.get("export_dialog_screenshot") if option_b else None
                    ),
                    "exported_data_a": exported_data_a.get(title_key),
                    "exported_data_b": exported_data_b.get(title_key),
                    "status": availability_status,
                }
            )
    return rows


def _export_data_source(element: dict[str, Any] | None) -> str | None:
    if element is None:
        return None
    sources = element.get("data_sources") or []
    if "powerbi_export" in sources:
        return "powerbi_export"
    if "summarized_export" in sources:
        return "summarized_export"
    return "powerbi_export"


def _is_export_data_source(value: Any) -> bool:
    valid_sources = {"powerbi_export", "summarized_export"}
    if isinstance(value, str):
        return value in valid_sources
    if isinstance(value, (list, tuple)):
        return any(source in valid_sources for source in value)
    return False


def _exported_elements(state: DashboardState | None) -> dict[str, dict[str, Any]]:
    if state is None:
        return {}
    result = {}
    for element in state.extracted.get("exported_data", []):
        if not isinstance(element, dict) or _is_application_chrome(element):
            continue
        sources = element.get("data_sources") or []
        if not {"powerbi_export", "summarized_export"}.intersection(sources):
            continue
        key = _title_signature(element.get("title"))
        if key:
            result.setdefault(key, element)
    return result


def _exported_sheets(element: dict[str, Any]) -> dict[str | None, dict[str, Any]]:
    worksheets = element.get("worksheets")
    if isinstance(worksheets, list) and worksheets:
        return {
            str(sheet.get("name") or f"Sheet {index + 1}"): sheet
            for index, sheet in enumerate(worksheets)
            if isinstance(sheet, dict)
        }
    return {None: element}


def _header_identity_keys(headers: list[Any]) -> list[tuple[str, int]]:
    occurrences: defaultdict[str, int] = defaultdict(int)
    keys = []
    for header in headers:
        normalized = " ".join(str(header or "").split()).casefold()
        normalized = re.sub(
            r"\s*\(\s*(?:text|date|datetime|whole number|decimal number|"
            r"fixed decimal number|integer|number|boolean)\s+type\s*\)\s*$",
            "",
            normalized,
        )
        occurrences[normalized] += 1
        keys.append((normalized, occurrences[normalized]))
    return keys


def _export_row_pairs(
    headers_a: list[Any],
    headers_b: list[Any],
    header_keys_a: list[tuple[str, int]],
    header_keys_b: list[tuple[str, int]],
    rows_a: list[Any],
    rows_b: list[Any],
) -> list[tuple[Any, Any]]:
    indices_a = {key: index for index, key in enumerate(header_keys_a)}
    indices_b = {key: index for index, key in enumerate(header_keys_b)}
    common_keys = [key for key in header_keys_a if key in indices_b]
    candidates = []
    for header_key in common_keys:
        index_a = indices_a[header_key]
        index_b = indices_b[header_key]
        column_values = [
            row[index]
            for source_rows, index in ((rows_a, index_a), (rows_b, index_b))
            for row in source_rows
            if isinstance(row, list) and index < len(row)
        ]
        if (
            len(column_values) != len(rows_a) + len(rows_b)
            or any(
                value is None
                or (isinstance(value, str) and not value.strip())
                for value in column_values
            )
        ):
            continue
        header = str(headers_a[index_a] or "")
        explicit = _is_export_dimension_header(header)
        if not explicit and any(
            not isinstance(value, str)
            or isinstance(_canonical_value(value), (int, float))
            for value in column_values
        ):
            continue
        candidates.append((not explicit, header_key))
    candidate_keys = [key for _, key in sorted(candidates)]

    cell_cache: dict[tuple[str, tuple[str, int]], list[str | None]] = {}

    def column_signatures(
        side: str,
        rows: list[Any],
        indices: dict[tuple[str, int], int],
        headers: list[Any],
        key: tuple[str, int],
    ) -> list[str | None]:
        cache_key = (side, key)
        cached = cell_cache.get(cache_key)
        if cached is None:
            index = indices[key]
            cached = []
            for row in rows:
                if not isinstance(row, list) or index >= len(row) or row[index] is None:
                    cached.append(None)
                else:
                    cached.append(
                        json.dumps(
                            _canonical_export_value(row[index], headers[index]),
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    )
            cell_cache[cache_key] = cached
        return cached

    def row_signatures(
        side: str,
        rows: list[Any],
        indices: dict[tuple[str, int], int],
        headers: list[Any],
        selected: list[tuple[str, int]],
    ) -> list[tuple[str, ...] | None]:
        columns = [
            column_signatures(side, rows, indices, headers, key) for key in selected
        ]
        signatures: list[tuple[str, ...] | None] = []
        for row_index in range(len(rows)):
            cells = [column[row_index] for column in columns]
            signatures.append(
                None if any(cell is None for cell in cells) else tuple(cells)
            )
        return signatures

    selected_keys: list[tuple[str, int]] = []
    signatures_a: list[tuple[str, ...] | None] = []
    signatures_b: list[tuple[str, ...] | None] = []
    for key in candidate_keys:
        selected_keys.append(key)
        signatures_a = row_signatures("a", rows_a, indices_a, headers_a, selected_keys)
        signatures_b = row_signatures("b", rows_b, indices_b, headers_b, selected_keys)
        if (
            all(signature is not None for signature in signatures_a + signatures_b)
            and len(set(signatures_a)) == len(signatures_a)
            and len(set(signatures_b)) == len(signatures_b)
        ):
            break

    if not selected_keys:
        return [
            (
                rows_a[index] if index < len(rows_a) else None,
                rows_b[index] if index < len(rows_b) else None,
            )
            for index in range(max(len(rows_a), len(rows_b)))
        ]

    rows_by_key_b: defaultdict[
        tuple[Any, ...], list[tuple[int, Any]]
    ] = defaultdict(list)
    for index, (row, key) in enumerate(zip(rows_b, signatures_b)):
        if key is not None:
            rows_by_key_b[key].append((index, row))

    pairs = []
    matched_b = set()
    for row, key in zip(rows_a, signatures_a):
        matches = rows_by_key_b.get(key, []) if key is not None else []
        if matches:
            index_b, match = matches.pop(0)
            matched_b.add(index_b)
            pairs.append((row, match))
        else:
            pairs.append((row, None))
    pairs.extend(
        (None, row) for index, row in enumerate(rows_b) if index not in matched_b
    )
    return pairs


def _compare_exported_data(
    states_a: list[DashboardState], states_b: list[DashboardState]
) -> list[dict[str, Any]]:
    by_key_a = {state.state_key.casefold(): state for state in states_a}
    by_key_b = {state.state_key.casefold(): state for state in states_b}
    rows = []
    for state_key in sorted(set(by_key_a) | set(by_key_b)):
        state_a = by_key_a.get(state_key)
        state_b = by_key_b.get(state_key)
        exported_a = _exported_elements(state_a)
        exported_b = _exported_elements(state_b)
        options_a = {
            _title_signature(item.get("title")): item
            for item in ((state_a.extracted.get("export_options") or []) if state_a else [])
            if isinstance(item, dict) and not _is_application_chrome(item)
        }
        options_b = {
            _title_signature(item.get("title")): item
            for item in ((state_b.extracted.get("export_options") or []) if state_b else [])
            if isinstance(item, dict) and not _is_application_chrome(item)
        }
        unavailable_data_keys = {
            key
            for key, option in {**options_a, **options_b}.items()
            if option.get("export_status") == "downloaded_no_tabular_data"
        }
        for visual_key in sorted(
            set(exported_a) | set(exported_b) | unavailable_data_keys
        ):
            element_a = exported_a.get(visual_key)
            element_b = exported_b.get(visual_key)
            option_a = options_a.get(visual_key)
            option_b = options_b.get(visual_key)
            if element_a is None or element_b is None:
                row = {
                    "state": state_key,
                    "visual": (option_a or option_b or element_a or element_b or {}).get("title"),
                    "row": None,
                    "column": "Export data availability",
                    "dashboard_a": (
                        "parsed"
                        if element_a is not None
                        else "No parsed exported data"
                    ),
                    "dashboard_b": (
                        "parsed"
                        if element_b is not None
                        else "No parsed exported data"
                    ),
                    "data_source_a": _export_data_source(element_a),
                    "data_source_b": _export_data_source(element_b),
                    "export_file_a": (
                        (element_a or option_a or {}).get("export_file")
                    ),
                    "export_file_b": (
                        (element_b or option_b or {}).get("export_file")
                    ),
                    "export_status_a": (
                        (option_a or element_a or {}).get("export_status")
                    ),
                    "export_status_b": (
                        (option_b or element_b or {}).get("export_status")
                    ),
                    "exportOptionAvailable_a": (
                        option_a.get("exportOptionAvailable") if option_a else None
                    ),
                    "exportOptionAvailable_b": (
                        option_b.get("exportOptionAvailable") if option_b else None
                    ),
                    "export_error_a": (
                        option_a.get("export_error") if option_a else None
                    ),
                    "export_error_b": (
                        option_b.get("export_error") if option_b else None
                    ),
                    "status": "UNVERIFIED",
                }
                for side, item in (
                    ("a", element_a or option_a),
                    ("b", element_b or option_b),
                ):
                    json_file = item.get("export_json_file") if item else None
                    if json_file:
                        row[f"export_json_file_{side}"] = json_file
                rows.append(row)
                continue

            sheets_a = _exported_sheets(element_a)
            sheets_b = _exported_sheets(element_b)
            compare_sheet_names = bool(
                element_a.get("worksheets") or element_b.get("worksheets")
            )
            if compare_sheet_names:
                if None in sheets_a and len(sheets_b) == 1:
                    sheets_a[next(iter(sheets_b))] = sheets_a.pop(None)
                if None in sheets_b and len(sheets_a) == 1:
                    sheets_b[next(iter(sheets_a))] = sheets_b.pop(None)
            for sheet_name in sorted(
                set(sheets_a) | set(sheets_b), key=lambda value: value or ""
            ):
                sheet_a = sheets_a.get(sheet_name)
                sheet_b = sheets_b.get(sheet_name)
                if sheet_a is None or sheet_b is None:
                    row = {
                        "state": state_key,
                        "visual": element_a.get("title") or element_b.get("title"),
                        "row": None,
                        "column": "Worksheet availability",
                        "dashboard_a": "parsed" if sheet_a else "missing",
                        "dashboard_b": "parsed" if sheet_b else "missing",
                        "data_source_a": _export_data_source(element_a),
                        "data_source_b": _export_data_source(element_b),
                        "export_file_a": element_a.get("export_file"),
                        "export_file_b": element_b.get("export_file"),
                        "export_status_a": (
                            option_a.get("export_status") if option_a else None
                        ),
                        "export_status_b": (
                            option_b.get("export_status") if option_b else None
                        ),
                        "exportOptionAvailable_a": (
                            option_a.get("exportOptionAvailable")
                            if option_a
                            else None
                        ),
                        "exportOptionAvailable_b": (
                            option_b.get("exportOptionAvailable")
                            if option_b
                            else None
                        ),
                        "export_error_a": (
                            option_a.get("export_error") if option_a else None
                        ),
                        "export_error_b": (
                            option_b.get("export_error") if option_b else None
                        ),
                        "status": "DIFFERENT",
                    }
                    if compare_sheet_names:
                        row["worksheet"] = sheet_name
                    for side, element in (("a", element_a), ("b", element_b)):
                        json_file = element.get("export_json_file")
                        if json_file:
                            row[f"export_json_file_{side}"] = json_file
                    rows.append(row)
                    continue

                headers_a = list(sheet_a.get("categories") or [])
                headers_b = list(sheet_b.get("categories") or [])
                header_keys_a = _header_identity_keys(headers_a)
                header_keys_b = _header_identity_keys(headers_b)
                header_labels = dict(zip(header_keys_a, headers_a))
                for key, header in zip(header_keys_b, headers_b):
                    header_labels.setdefault(key, header)
                all_header_keys = list(header_keys_a)
                all_header_keys.extend(
                    key for key in header_keys_b if key not in header_keys_a
                )
                header_keys = [
                    key
                    for key in all_header_keys
                    if (
                        key in header_keys_a
                        and key in header_keys_b
                        or not _is_export_dimension_header(header_labels[key])
                    )
                ]
                values_a = list(sheet_a.get("rows") or [])
                values_b = list(sheet_b.get("rows") or [])
                row_pairs = _export_row_pairs(
                    headers_a,
                    headers_b,
                    header_keys_a,
                    header_keys_b,
                    values_a,
                    values_b,
                )
                for row_index, (row_a, row_b) in enumerate(row_pairs):
                    for header_key in header_keys:
                        index_a = (
                            header_keys_a.index(header_key)
                            if header_key in header_keys_a
                            else -1
                        )
                        index_b = (
                            header_keys_b.index(header_key)
                            if header_key in header_keys_b
                            else -1
                        )
                        header = header_labels[header_key]
                        value_a = (
                            row_a[index_a]
                            if isinstance(row_a, list)
                            and 0 <= index_a < len(row_a)
                            else None
                        )
                        value_b = (
                            row_b[index_b]
                            if isinstance(row_b, list) and 0 <= index_b < len(row_b)
                            else None
                        )
                        if row_a is None or row_b is None:
                            status = "DIFFERENT"
                        elif index_a < 0 or index_b < 0:
                            status = "DIFFERENT"
                        elif value_a is None or value_b is None:
                            status = "UNVERIFIED"
                        else:
                            status = (
                                "MATCH"
                                if _export_values_match(value_a, value_b, header)
                                else "DIFFERENT"
                            )
                        row = {
                            "state": state_key,
                            "visual": element_a.get("title"),
                            "row": row_index + 1,
                            "column": header,
                            "dashboard_a": value_a,
                            "dashboard_b": value_b,
                            "data_source_a": _export_data_source(element_a),
                            "data_source_b": _export_data_source(element_b),
                            "export_file_a": element_a.get("export_file"),
                            "export_file_b": element_b.get("export_file"),
                            "export_status_a": (
                                option_a.get("export_status") if option_a else None
                            ),
                            "export_status_b": (
                                option_b.get("export_status") if option_b else None
                            ),
                            "exportOptionAvailable_a": (
                                option_a.get("exportOptionAvailable")
                                if option_a
                                else None
                            ),
                            "exportOptionAvailable_b": (
                                option_b.get("exportOptionAvailable")
                                if option_b
                                else None
                            ),
                            "export_error_a": (
                                option_a.get("export_error") if option_a else None
                            ),
                            "export_error_b": (
                                option_b.get("export_error") if option_b else None
                            ),
                            "status": status,
                        }
                        if compare_sheet_names:
                            row["worksheet"] = sheet_name
                        for side, element in (("a", element_a), ("b", element_b)):
                            json_file = element.get("export_json_file")
                            if json_file:
                                row[f"export_json_file_{side}"] = json_file
                        rows.append(row)
    return rows


def _compare_dashboard_metadata(
    states_a: list[DashboardState], states_b: list[DashboardState]
) -> list[dict[str, Any]]:
    by_key_a = {state.state_key.casefold(): state for state in states_a}
    by_key_b = {state.state_key.casefold(): state for state in states_b}
    comparisons = []
    for state_key in sorted(set(by_key_a) | set(by_key_b)):
        state_a = by_key_a.get(state_key)
        state_b = by_key_b.get(state_key)
        metadata_a = (
            (state_a.extracted.get("dashboard_metadata") or {})
            if state_a
            else {}
        )
        metadata_b = (
            (state_b.extracted.get("dashboard_metadata") or {})
            if state_b
            else {}
        )
        for field, label, element_type in (
            ("report_title", "Report title", "report_title"),
            ("refresh_timestamp", "Refresh timestamp", "metadata_value"),
        ):
            value_a = metadata_a.get(field)
            value_b = metadata_b.get(field)
            if field == "report_title":
                value_a = value_a or metadata_a.get("dashboard_name")
                value_b = value_b or metadata_b.get("dashboard_name")
            if not value_a and not value_b:
                continue
            equal = (
                _normalized_title_text(value_a)
                == _normalized_title_text(value_b)
                if value_a and value_b
                else False
            )
            comparisons.append(
                {
                    "state": state_key,
                    "element": label,
                    "element_type": element_type,
                    "dashboard_a": value_a or "unavailable",
                    "dashboard_b": value_b or "unavailable",
                    "status": (
                        "MATCH"
                        if equal
                        else "INFO"
                        if value_a and value_b
                        else "UNVERIFIED"
                    ),
                    "identity_warning": (
                        field == "report_title"
                        and bool(value_a and value_b and not equal)
                    ),
                    "metadata_field": field,
                }
            )
    return comparisons


def _state_capture_status(state: DashboardState) -> str:
    return str(state.extracted.get("capture_status", "captured"))


def _render_warning_row(
    state_key: str,
    warning_a: dict[str, Any] | None,
    warning_b: dict[str, Any] | None,
) -> dict[str, Any]:
    def description(warning: dict[str, Any] | None) -> str:
        if warning is None:
            return "No Power BI render issue detected"
        reason = warning.get("reason")
        if isinstance(reason, str) and reason:
            return f"UNVERIFIED: {reason}"
        indicators = warning.get("indicators")
        count = sum(
            len(
                item.get("busy_details", {}).get(
                    "visual_loading_markers", []
                )
            )
            for item in indicators
            if isinstance(item, dict)
            and isinstance(item.get("busy_details"), dict)
        ) if isinstance(indicators, list) else 0
        return (
            "UNVERIFIED: Power BI visual loading indicators remained active"
            + (f" ({count} detected)" if count else "")
        )

    def data_source(warning: dict[str, Any] | None) -> list[str]:
        if warning and warning.get("errors"):
            return ["browser_render_error"]
        return ["browser_loading_indicator"]

    reasons = [
        warning.get("reason")
        for warning in (warning_a, warning_b)
        if isinstance(warning, dict) and warning.get("reason")
    ]
    return {
        "state": state_key,
        "element": "Visual rendering status",
        "dashboard_a": description(warning_a),
        "dashboard_b": description(warning_b),
        "dashboard_data_a": {"render_unverified": warning_a},
        "dashboard_data_b": {"render_unverified": warning_b},
        "data_source_a": data_source(warning_a),
        "data_source_b": data_source(warning_b),
        "exportOptionAvailable_a": None,
        "exportOptionAvailable_b": None,
        "export_status_a": None,
        "export_status_b": None,
        "already_exported_a": "N/A",
        "already_exported_b": "N/A",
        "difference_details": [],
        "comparison_note": (
            f"{'; '.join(dict.fromkeys(reasons))}; accessible metadata was "
            "captured, but visual values are UNVERIFIED."
            if reasons
            else (
                "Accessible metadata was captured, but one or more Power BI "
                "visuals remained in a loading state; this state is UNVERIFIED."
            )
        ),
        "alignment_status": "render_incomplete",
        "status": "UNVERIFIED",
    }


def _dashboard_label(states: list[DashboardState], fallback: str) -> str:
    for state in states:
        metadata = state.extracted.get("dashboard_metadata") or {}
        if isinstance(metadata, dict):
            name = metadata.get("report_title") or metadata.get("dashboard_name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return fallback


def _canonical_status(value: Any) -> str:
    status = str(value or "UNVERIFIED").strip().upper()
    aliases = {
        "EXTRA_IN_A": "MISSING_IN_B",
        "MISSING_A": "MISSING_IN_A",
        "EXTRA_IN_B": "MISSING_IN_A",
        "MISSING_B": "MISSING_IN_B",
    }
    status = aliases.get(status, status)
    if status not in {
        "MATCH",
        "DIFFERENT",
        "INFO",
        "UNVERIFIED",
        "MISSING_IN_A",
        "MISSING_IN_B",
    }:
        raise ValueError(f"Unsupported comparison status: {status}")
    return status


def _canonical_record(
    record_type: str,
    section: str,
    row: dict[str, Any],
    source_a_name: str,
    source_b_name: str,
    occurrence: int,
) -> dict[str, Any]:
    item_name = str(
        row.get("element")
        or row.get("visual")
        or row.get("metadata_field")
        or "(unnamed item)"
    )
    if record_type == "visual":
        value_a = row.get("dashboard_data_a", {"value": row.get("dashboard_a")})
        value_b = row.get("dashboard_data_b", {"value": row.get("dashboard_b")})
        element_a = row.get("element_text_a")
        element_b = row.get("element_text_b")
        type_a = row.get("element_type_a")
        type_b = row.get("element_type_b")
        order_a = row.get("order_a")
        order_b = row.get("order_b")
        association_a = row.get("associated_visual_a")
        association_b = row.get("associated_visual_b")
    elif record_type == "title":
        value_a = row.get("dashboard_a")
        value_b = row.get("dashboard_b")
        element_a = row.get("element_text_a") or row.get("dashboard_a")
        element_b = row.get("element_text_b") or row.get("dashboard_b")
        type_a = row.get("category_a") or row.get("standard_element_name_a")
        type_b = row.get("category_b") or row.get("standard_element_name_b")
        order_a = row.get("order_a")
        order_b = row.get("order_b")
        association_a = row.get("associated_visual_a")
        association_b = row.get("associated_visual_b")
    else:
        value_a = row.get("dashboard_a")
        value_b = row.get("dashboard_b")
        element_a = row.get("visual_a") or row.get("element")
        element_b = row.get("visual_b") or row.get("element")
        type_a = row.get("element_type_a")
        type_b = row.get("element_type_b")
        order_a = row.get("order_a")
        order_b = row.get("order_b")
        association_a = row.get("worksheet") or row.get("visual")
        association_b = association_a

    if record_type == "visual":
        exists_a = bool(
            row.get("order_a") is not None
            or "accessible_filter" in (row.get("data_source_a") or [])
            or "browser_loading_indicator" in (row.get("data_source_a") or [])
            or "browser_render_error" in (row.get("data_source_a") or [])
        )
        exists_b = bool(
            row.get("order_b") is not None
            or "accessible_filter" in (row.get("data_source_b") or [])
            or "browser_loading_indicator" in (row.get("data_source_b") or [])
            or "browser_render_error" in (row.get("data_source_b") or [])
        )
    elif record_type == "title":
        exists_a = row.get("order_a") is not None
        exists_b = row.get("order_b") is not None
    elif record_type == "metadata":
        exists_a = row.get("dashboard_a") not in {None, "", "unavailable"}
        exists_b = row.get("dashboard_b") not in {None, "", "unavailable"}
    elif record_type == "export_option":
        exists_a = row.get("visual_a") is not None
        exists_b = row.get("visual_b") is not None
    else:
        exists_a = row.get("data_source_a") is not None
        exists_b = row.get("data_source_b") is not None

    identity = {
        "record_type": record_type,
        "state": row.get("state"),
        "aligned_position": row.get("aligned_position"),
        "order_a": order_a,
        "order_b": order_b,
        "item_name": item_name,
        "association_a": association_a,
        "association_b": association_b,
        "worksheet": row.get("worksheet"),
        "row": row.get("row"),
        "column": row.get("column"),
        "occurrence": occurrence,
    }
    comparison_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:20]
    status = _canonical_status(row.get("status"))
    difference_types = row.get("difference_details")
    if not isinstance(difference_types, list):
        difference_types = [
            value
            for value in (
                row.get("title_status")
                if row.get("title_status") not in {None, "unchanged"}
                else None,
                row.get("metadata_field") if status == "INFO" else None,
                "exported_cell" if record_type == "exported_data" else None,
            )
            if value
        ]
    else:
        difference_types = [str(value) for value in difference_types]

    confidence = row.get("alignment_confidence") or row.get("confidence")
    if confidence is None and (
        _is_export_data_source(row.get("data_source_a"))
        and _is_export_data_source(row.get("data_source_b"))
    ):
        confidence = "high"
    evidence = {
        "matching_method": row.get("match_method")
        or ("sequence_alignment" if row.get("aligned_position") else None),
        "alignment_status": row.get("alignment_status"),
        "alignment_cost": row.get("alignment_cost"),
        "alignment_confidence": confidence,
        "source_positions": {"a": order_a, "b": order_b},
        "aligned_position": row.get("aligned_position"),
        "bounding_boxes": {
            "a": row.get("bounding_box_a"),
            "b": row.get("bounding_box_b"),
        },
        "normalized_bounding_boxes": {
            "a": row.get("normalized_bounding_box_a"),
            "b": row.get("normalized_bounding_box_b"),
        },
        "data_sources": {
            "a": row.get("data_source_a"),
            "b": row.get("data_source_b"),
        },
        "unverified_reason": _unverified_reason(record_type, row, status),
        "raw_comparison": row,
    }

    return {
        "comparison_id": f"CMP_{comparison_id}",
        "page": str(row.get("state") or "").split("|", 1)[0],
        "section": section,
        "item_name": item_name,
        "record_type": record_type,
        "visual_type": type_a or type_b,
        "visual_role": row.get("standardized_category")
        or row.get("standard_element_name")
        or row.get("metadata_field"),
        "source_a": {
            "name": source_a_name,
            "exists": exists_a,
            "label": element_a,
            "element_type": type_a,
            "associated_visual": association_a,
            "value": value_a,
            "extraction_status": row.get("extraction_status_a"),
            "extraction_uncertainties": row.get("extraction_uncertainties_a") or [],
            "evidence_sources": row.get("evidence_sources_a")
            or row.get("data_source_a"),
            "position": {
                "reading_order": order_a,
                "aligned_position": row.get("aligned_position"),
                "bounding_box": row.get("bounding_box_a"),
                "normalized_bounding_box": row.get("normalized_bounding_box_a"),
            },
        },
        "source_b": {
            "name": source_b_name,
            "exists": exists_b,
            "label": element_b,
            "element_type": type_b,
            "associated_visual": association_b,
            "value": value_b,
            "extraction_status": row.get("extraction_status_b"),
            "extraction_uncertainties": row.get("extraction_uncertainties_b") or [],
            "evidence_sources": row.get("evidence_sources_b")
            or row.get("data_source_b"),
            "position": {
                "reading_order": order_b,
                "aligned_position": row.get("aligned_position"),
                "bounding_box": row.get("bounding_box_b"),
                "normalized_bounding_box": row.get("normalized_bounding_box_b"),
            },
        },
        "status": status,
        "difference_type": difference_types,
        "difference_summary": (
            "; ".join(difference_types)
            if difference_types
            else "No difference detected"
            if status == "MATCH"
            else str(row.get("comparison_note") or status.replace("_", " ").title())
        ),
        "unverified_reason": _unverified_reason(record_type, row, status),
        "matching_confidence": confidence,
        "ai_confidence": None,
        "evidence": evidence,
    }


def _unverified_reason(
    record_type: str, row: dict[str, Any], status: str
) -> str | None:
    if status != "UNVERIFIED":
        return None
    alignment_status = str(row.get("alignment_status") or "")
    details = row.get("difference_details") or []
    if alignment_status == "capture_unavailable":
        reasons = [
            str(reason)
            for reason in (
                row.get("capture_error_a"),
                row.get("capture_error_b"),
            )
            if reason
        ]
        return (
            "Filter state capture was skipped: " + "; ".join(reasons)
            if reasons
            else str(row.get("comparison_note") or "Filter state was not captured.")
        )
    if "AMBIGUOUS_ALIGNMENT" in details or alignment_status == "ambiguous_alignment":
        return "Sequence alignment had competing plausible matches."
    if alignment_status == "state_unmatched":
        return "The corresponding page or filter state was not captured on both sides."
    if alignment_status == "no_comparable_visuals":
        return "No comparable visual elements were extracted for this state."
    if record_type == "title":
        return (
            "This title occurrence was unpaired in the extraction; its absence "
            "from the dashboard is not confirmed."
        )
    if record_type == "exported_data":
        return "Comparable summarized export data was unavailable for this cell."
    if record_type == "metadata":
        return "The metadata value was unavailable on at least one side."
    if "TYPE_UNVERIFIED" in details:
        return "The element type could not be reliably classified."
    return "Available extraction evidence was incomplete or insufficient."


def _build_canonical_records(
    rows: list[dict[str, Any]],
    title_rows: list[dict[str, Any]],
    metadata_rows: list[dict[str, Any]],
    export_option_rows: list[dict[str, Any]],
    exported_data_rows: list[dict[str, Any]],
    states_a: list[DashboardState],
    states_b: list[DashboardState],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source_a_name = _dashboard_label(states_a, "Dashboard A")
    source_b_name = _dashboard_label(states_b, "Dashboard B")
    collections = (
        ("visual", "Visuals & filters", rows),
        ("title", "Titles & labels", title_rows),
        ("metadata", "Dashboard metadata", metadata_rows),
        ("export_option", "Export availability", export_option_rows),
        ("exported_data", "Exported data", exported_data_rows),
    )
    records = []
    occurrence_counts: defaultdict[tuple[str, str, str], int] = defaultdict(int)
    for record_type, section, source_rows in collections:
        for row in source_rows:
            item_name = str(row.get("element") or row.get("visual") or "")
            key = (record_type, str(row.get("state") or ""), item_name)
            occurrence_counts[key] += 1
            records.append(
                _canonical_record(
                    record_type,
                    section,
                    row,
                    source_a_name,
                    source_b_name,
                    occurrence_counts[key],
                )
            )

    status_counts = {
        status: sum(record["status"] == status for record in records)
        for status in (
            "MATCH",
            "DIFFERENT",
            "MISSING_IN_A",
            "MISSING_IN_B",
            "INFO",
            "UNVERIFIED",
        )
    }
    verified_count = (
        status_counts["MATCH"]
        + status_counts["DIFFERENT"]
        + status_counts["MISSING_IN_A"]
        + status_counts["MISSING_IN_B"]
    )
    kpis = {
        "total_items": len(records),
        "matches": status_counts["MATCH"],
        "differences": (
            status_counts["DIFFERENT"]
            + status_counts["MISSING_IN_A"]
            + status_counts["MISSING_IN_B"]
        ),
        "missing_in_a": status_counts["MISSING_IN_A"],
        "missing_in_b": status_counts["MISSING_IN_B"],
        "informational": status_counts["INFO"],
        "unverified": status_counts["UNVERIFIED"],
        "verified_items": verified_count,
        "verified_agreement_rate_percent": (
            round(status_counts["MATCH"] * 100 / verified_count, 2)
            if verified_count
            else None
        ),
        "ai_confidence_percent": None,
        "status_counts": status_counts,
        "source_names": {"a": source_a_name, "b": source_b_name},
    }
    return records, kpis


def _validate_comparison_output(result: dict[str, Any]) -> None:
    records = result.get("comparison_records")
    kpis = result.get("kpis")
    if not isinstance(records, list) or not isinstance(kpis, dict):
        raise ValueError("Comparison output is missing canonical records or KPIs")
    ids = [record.get("comparison_id") for record in records]
    if any(not isinstance(value, str) or not value for value in ids):
        raise ValueError("Canonical comparison records require non-empty IDs")
    if len(ids) != len(set(ids)):
        raise ValueError("Canonical comparison record IDs must be unique")
    for record in records:
        if record.get("status") not in {
            "MATCH",
            "DIFFERENT",
            "INFO",
            "UNVERIFIED",
            "MISSING_IN_A",
            "MISSING_IN_B",
        }:
            raise ValueError("Canonical comparison record has an invalid status")
    if kpis.get("total_items") != len(records):
        raise ValueError("Canonical total_items KPI does not match record count")
    for status, count in kpis.get("status_counts", {}).items():
        if count != sum(record["status"] == status for record in records):
            raise ValueError(f"Canonical {status} KPI does not match records")


def compare_dashboards(
    states_a: list[DashboardState], states_b: list[DashboardState]
) -> dict[str, Any]:
    by_key_a = {state.state_key.casefold(): state for state in states_a}
    by_key_b = {state.state_key.casefold(): state for state in states_b}
    rows: list[dict[str, Any]] = []
    all_state_keys = sorted(set(by_key_a) | set(by_key_b))
    title_comparison_rows: list[dict[str, Any]] = []
    captured_states_a = [
        state for state in states_a if _state_capture_status(state) == "captured"
    ]
    captured_states_b = [
        state for state in states_b if _state_capture_status(state) == "captured"
    ]
    exported_data_comparisons = _compare_exported_data(
        captured_states_a, captured_states_b
    )
    metadata_comparisons = _compare_dashboard_metadata(
        captured_states_a, captured_states_b
    )

    for state_key in all_state_keys:
        state_a = by_key_a.get(state_key)
        state_b = by_key_b.get(state_key)
        if any(
            state is not None and _state_capture_status(state) != "captured"
            for state in (state_a, state_b)
        ):
            capture_status_a = (
                _state_capture_status(state_a) if state_a is not None else "missing"
            )
            capture_status_b = (
                _state_capture_status(state_b) if state_b is not None else "missing"
            )
            capture_error_a = (
                state_a.extracted.get("capture_error")
                if state_a is not None
                else None
            )
            capture_error_b = (
                state_b.extracted.get("capture_error")
                if state_b is not None
                else None
            )
            rows.append(
                {
                    "state": state_key,
                    "element": "(filter state not captured)",
                    "dashboard_a": (
                        f"UNVERIFIED: {capture_error_a}"
                        if capture_error_a
                        else "Captured; not compared"
                        if capture_status_a == "captured"
                        else f"UNVERIFIED: capture status {capture_status_a}"
                    ),
                    "dashboard_b": (
                        f"UNVERIFIED: {capture_error_b}"
                        if capture_error_b
                        else "Captured; not compared"
                        if capture_status_b == "captured"
                        else f"UNVERIFIED: capture status {capture_status_b}"
                    ),
                    "dashboard_data_a": {
                        "capture_status": capture_status_a,
                        "reason": capture_error_a,
                    },
                    "dashboard_data_b": {
                        "capture_status": capture_status_b,
                        "reason": capture_error_b,
                    },
                    "data_source_a": None,
                    "data_source_b": None,
                    "exportOptionAvailable_a": None,
                    "exportOptionAvailable_b": None,
                    "export_status_a": None,
                    "export_status_b": None,
                    "already_exported_a": "N/A",
                    "already_exported_b": "N/A",
                    "status": "UNVERIFIED",
                    "difference_details": [],
                    "comparison_note": (
                        "The filter state could not be verified; no screenshot "
                        "evidence was captured."
                    ),
                    "capture_error_a": capture_error_a,
                    "capture_error_b": capture_error_b,
                    "alignment_status": "capture_unavailable",
                }
            )
            continue
        extracted_a = state_a.extracted if state_a else {}
        extracted_b = state_b.extracted if state_b else {}
        if isinstance(extracted_a.get("screenshot_elements"), list):
            extracted_a = {
                **extracted_a,
                "elements": extracted_a["screenshot_elements"],
            }
        if isinstance(extracted_b.get("screenshot_elements"), list):
            extracted_b = {
                **extracted_b,
                "elements": extracted_b["screenshot_elements"],
            }
        map_a = (
            _element_map(extracted_a, state_a.filter_names) if state_a else {}
        )
        map_b = (
            _element_map(extracted_b, state_b.filter_names) if state_b else {}
        )
        exported_map_a = _exported_elements(state_a) if state_a else {}
        exported_map_b = _exported_elements(state_b) if state_b else {}
        element_pairs = _pair_elements(map_a, map_b)
        ordered_keys_a = _ordered_item_keys(map_a)
        ordered_keys_b = _ordered_item_keys(map_b)
        ordered_items_a = [map_a[key] for key in ordered_keys_a]
        ordered_items_b = [map_b[key] for key in ordered_keys_b]
        ordered_indexes_a = {
            key: index for index, key in enumerate(ordered_keys_a)
        }
        ordered_indexes_b = {
            key: index for index, key in enumerate(ordered_keys_b)
        }
        positions_a = {
            key: _extracted_order(element.get("reading_order"), index)
            for index, (key, element) in enumerate(map_a.items(), 1)
        }
        positions_b = {
            key: _extracted_order(element.get("reading_order"), index)
            for index, (key, element) in enumerate(map_b.items(), 1)
        }
        metadata_only = bool(
            state_a
            and state_b
            and state_a.extracted.get("capture_mode")
            in {"filters_only", "visual_layout_filters"}
            and state_b.extracted.get("capture_mode")
            in {"filters_only", "visual_layout_filters"}
        )
        if not element_pairs and not metadata_only:
            rows.append(
                {
                    "state": state_key,
                    "element": "(no comparable visual elements)",
                    "dashboard_a": "state unavailable" if state_a is None else "no comparable visuals",
                    "dashboard_b": "state unavailable" if state_b is None else "no comparable visuals",
                    "dashboard_data_a": {
                        "value": "state unavailable" if state_a is None else "no comparable visuals"
                    },
                    "dashboard_data_b": {
                        "value": "state unavailable" if state_b is None else "no comparable visuals"
                    },
                    "data_source_a": None,
                    "data_source_b": None,
                    "exportOptionAvailable_a": None,
                    "exportOptionAvailable_b": None,
                    "export_status_a": None,
                    "export_status_b": None,
                    "already_exported_a": "Unknown",
                    "already_exported_b": "Unknown",
                    "status": "UNVERIFIED",
                    "alignment_status": (
                        "state_unmatched"
                        if state_a is None or state_b is None
                        else "no_comparable_visuals"
                    ),
                }
            )
        for aligned_position, (key_a, key_b) in enumerate(element_pairs, 1):
            element_a = map_a.get(key_a) if key_a is not None else None
            element_b = map_b.get(key_b) if key_b is not None else None
            alignment_cost = (
                _alignment_pair_cost(element_a, element_b)
                if element_a is not None and element_b is not None
                else None
            )
            alignment_confidence = (
                _alignment_confidence(element_a, element_b)
                if (
                    alignment_cost is not None
                    and element_a is not None
                    and element_b is not None
                )
                else "not_applicable"
            )
            if (
                key_a is not None
                and key_b is not None
                and _has_competing_alignment(
                    ordered_items_a,
                    ordered_items_b,
                    ordered_indexes_a[key_a],
                    ordered_indexes_b[key_b],
                )
            ):
                alignment_confidence = "low"
            ambiguous_alignment = alignment_confidence == "low"
            layout_distance = (
                _title_distance(element_a, element_b)
                if element_a is not None and element_b is not None
                else None
            )
            layout_status = (
                "not_comparable"
                if layout_distance is None or ambiguous_alignment
                else "layout_mismatch"
                if layout_distance > 0.08
                else "layout_match"
            )
            element = element_a or element_b or {}
            title = _element_label(element)
            element_type = element.get("type") or element.get("element_type")
            standard_name = standard_element_name(
                element.get("standard_element_name") or element_type,
                element_type,
                title,
            )
            standardized_category = _standardized_title_category(
                title, element.get("category") or element.get("type")
            )
            visual_key_a = _title_signature(
                (element_a or element).get("title")
            )
            visual_key_b = _title_signature(
                (element_b or element).get("title")
            )
            exported_a = exported_map_a.get(visual_key_a)
            exported_b = exported_map_b.get(visual_key_b)
            already_exported_a = exported_a is not None
            already_exported_b = exported_b is not None
            screenshot_comparison_skipped = (
                already_exported_a or already_exported_b
            )
            display_element_a = element_a
            display_element_b = element_b
            value_a = _comparable_element_values(element_a) if element_a else None
            value_b = _comparable_element_values(element_b) if element_b else None
            has_data_a = bool(element_a and _has_comparable_data(element_a))
            has_data_b = bool(element_b and _has_comparable_data(element_b))
            difference_details = []
            type_a = (
                str(element_a.get("type") or element_a.get("element_type") or "")
                .strip()
                .casefold()
                if element_a
                else ""
            )
            type_b = (
                str(element_b.get("type") or element_b.get("element_type") or "")
                .strip()
                .casefold()
                if element_b
                else ""
            )
            unknown_types = {"", "other", "unknown", "undefined", "none"}
            if type_a in unknown_types:
                type_a = ""
            if type_b in unknown_types:
                type_b = ""
            type_mismatch = bool(
                element_a
                and element_b
                and type_a
                and type_b
                and type_a != type_b
            )
            type_unverified = bool(
                element_a and element_b and (not type_a or not type_b)
            )
            if ambiguous_alignment:
                difference_details.append("AMBIGUOUS_ALIGNMENT")
                status = "UNVERIFIED"
            elif type_mismatch:
                difference_details.append("TYPE_MISMATCH")
                if _canonical_value(value_a) == _canonical_value(value_b):
                    status = "UNVERIFIED"
                else:
                    difference_details.append("CONTENT_MISMATCH")
                    status = "DIFFERENT"
            elif screenshot_comparison_skipped:
                status = "INFO"
                if type_unverified:
                    difference_details.append("TYPE_UNVERIFIED")
            elif element_a is None or element_b is None:
                status = "UNVERIFIED"
            elif type_unverified:
                difference_details.append("TYPE_UNVERIFIED")
                status = "UNVERIFIED"
            elif _is_informational_metadata(title):
                status = "INFO"
            elif not has_data_a and not has_data_b:
                status = "UNVERIFIED"
            elif element_a is not None and element_b is not None and (
                not has_data_a or not has_data_b
            ):
                status = "UNVERIFIED"
            else:
                status = (
                    "MATCH"
                    if _canonical_value(value_a) == _canonical_value(value_b)
                    else "DIFFERENT"
                )
                if status == "DIFFERENT":
                    difference_details.append("CONTENT_MISMATCH")
            if already_exported_a and already_exported_b:
                exported_statuses = [
                    row["status"]
                    for row in exported_data_comparisons
                    if str(row.get("state") or "").casefold()
                    == state_key.casefold()
                    and _title_signature(row.get("visual"))
                    in {visual_key_a, visual_key_b}
                ]
                if not ambiguous_alignment and not type_mismatch and not type_unverified:
                    status = (
                        "DIFFERENT"
                        if "DIFFERENT" in exported_statuses
                        else "UNVERIFIED"
                        if "UNVERIFIED" in exported_statuses or not exported_statuses
                        else "MATCH"
                    )
            if layout_status == "layout_mismatch" and not ambiguous_alignment:
                difference_details.append("LAYOUT_MISMATCH")
                if status != "UNVERIFIED":
                    status = "DIFFERENT"
            dashboard_data_a = _comparison_data_object(
                exported_a or element_a,
                "missing"
                if element_a is None and state_a is not None
                else "state unavailable"
                if state_a is None
                else None,
            )
            dashboard_data_b = _comparison_data_object(
                exported_b or element_b,
                "missing"
                if element_b is None and state_b is not None
                else "state unavailable"
                if state_b is None
                else None,
            )
            rows.append(
                {
                    "state": state_key,
                    "element": str(title),
                    "dashboard_data_a": dashboard_data_a,
                    "dashboard_data_b": dashboard_data_b,
                    "standard_element_name": standard_name,
                    "element_type_a": element_a.get("type") if element_a else None,
                    "element_type_b": element_b.get("type") if element_b else None,
                    "element_text_a": (
                        element_a.get("element_text") or element_a.get("title")
                        if element_a
                        else None
                    ),
                    "element_text_b": (
                        element_b.get("element_text") or element_b.get("title")
                        if element_b
                        else None
                    ),
                    "extraction_status_a": (
                        element_a.get("extraction_status") if element_a else None
                    ),
                    "extraction_status_b": (
                        element_b.get("extraction_status") if element_b else None
                    ),
                    "extraction_uncertainties_a": (
                        element_a.get("uncertainties") or [] if element_a else []
                    ),
                    "extraction_uncertainties_b": (
                        element_b.get("uncertainties") or [] if element_b else []
                    ),
                    "evidence_sources_a": (
                        element_a.get("evidence_sources") or element_a.get("data_sources")
                        if element_a
                        else None
                    ),
                    "evidence_sources_b": (
                        element_b.get("evidence_sources") or element_b.get("data_sources")
                        if element_b
                        else None
                    ),
                    "dashboard_a": (
                        "state unavailable"
                        if state_a is None
                        else "not extracted"
                        if display_element_a is None
                        else "Already exported"
                        if already_exported_a
                        else "Screenshot comparison skipped; other side exported"
                        if screenshot_comparison_skipped
                        else _comparison_display(display_element_a)
                    ),
                    "dashboard_b": (
                        "state unavailable"
                        if state_b is None
                        else "not extracted"
                        if display_element_b is None
                        else "Already exported"
                        if already_exported_b
                        else "Screenshot comparison skipped; other side exported"
                        if screenshot_comparison_skipped
                        else _comparison_display(
                            display_element_b,
                        )
                    ),
                    "status": status,
                    "difference_details": difference_details,
                    "screenshot_comparison_skipped": screenshot_comparison_skipped,
                    "already_exported_a": _already_exported_status(
                        state_a, element_a, exported_a
                    ),
                    "already_exported_b": _already_exported_status(
                        state_b, element_b, exported_b
                    ),
                    "comparison_note": (
                        "Screenshot values skipped; see comparison.json."
                        if screenshot_comparison_skipped
                        else None
                    ),
                    "data_source_a": (
                        exported_a.get("data_sources") or ["powerbi_export"]
                        if exported_a
                        else element_a.get("data_sources") or ["screenshot"]
                        if element_a
                        else None
                    ),
                    "data_source_b": (
                        exported_b.get("data_sources") or ["powerbi_export"]
                        if exported_b
                        else element_b.get("data_sources") or ["screenshot"]
                        if element_b
                        else None
                    ),
                    "exportOptionAvailable_a": (
                        _element_export_option(state_a, element_a)
                    ),
                    "exportOptionAvailable_b": (
                        _element_export_option(state_b, element_b)
                    ),
                    "export_status_a": (
                        _element_export_status(state_a, element_a or exported_a)
                    ),
                    "export_status_b": (
                        _element_export_status(state_b, element_b or exported_b)
                    ),
                    "standardized_category": standardized_category,
                    "aligned_position": aligned_position,
                    "alignment_cost": alignment_cost,
                    "alignment_confidence": alignment_confidence,
                    "layout_status": layout_status,
                    "layout_distance": layout_distance,
                    "bounding_box_a": (
                        _source_title_box(element_a) if element_a else None
                    ),
                    "bounding_box_b": (
                        _source_title_box(element_b) if element_b else None
                    ),
                    "normalized_bounding_box_a": (
                        _normalized_title_box(element_a) if element_a else None
                    ),
                    "normalized_bounding_box_b": (
                        _normalized_title_box(element_b) if element_b else None
                    ),
                    "order_a": (
                        positions_a.get(key_a)
                        if element_a
                        else None
                    ),
                    "order_b": (
                        positions_b.get(key_b)
                        if element_b
                        else None
                    ),
                    "alignment_status": (
                        "ambiguous_alignment"
                        if ambiguous_alignment
                        else "missing_in_b"
                        if element_a is not None and element_b is None
                        else "extra_in_b"
                        if element_a is None and element_b is not None
                        else "sequence_aligned"
                    ),
                }
            )

        title_comparison_rows.extend(
            _compare_title_items(state_key, state_a, state_b)
        )

        filter_values_a = _normalized_filter_values(state_a) if state_a else {}
        filter_values_b = _normalized_filter_values(state_b) if state_b else {}
        for name in sorted(set(filter_values_a) | set(filter_values_b)):
            filter_a = filter_values_a.get(name)
            filter_b = filter_values_b.get(name)
            bookmark_options = _bookmark_group_options((filter_a or filter_b)[0])
            if bookmark_options is not None:
                rows.append(
                    _bookmark_group_row(state_key, filter_a, filter_b, bookmark_options)
                )
                continue
            if filter_a is None:
                continue
            if filter_a is not None and filter_b is not None and filter_a[2] == filter_b[2]:
                continue
            rows.append(
                {
                    "state": state_key,
                    "element": (filter_a or filter_b)[0],
                    "dashboard_a": "unavailable" if filter_a is None else _display(filter_a[1]),
                    "dashboard_b": "unavailable" if filter_b is None else _display(filter_b[1]),
                    "dashboard_data_a": {
                        "value": "unavailable" if filter_a is None else filter_a[1]
                    },
                    "dashboard_data_b": {
                        "value": "unavailable" if filter_b is None else filter_b[1]
                    },
                    "data_source_a": ["accessible_filter"] if filter_a else None,
                    "data_source_b": ["accessible_filter"] if filter_b else None,
                    "exportOptionAvailable_a": None,
                    "exportOptionAvailable_b": None,
                    "export_status_a": None,
                    "export_status_b": None,
                    "already_exported_a": "N/A",
                    "already_exported_b": "N/A",
                    "status": "DIFFERENT",
                }
            )
        render_warning_a = (
            state_a.extracted.get("render_unverified")
            if state_a is not None
            else None
        )
        render_warning_b = (
            state_b.extracted.get("render_unverified")
            if state_b is not None
            else None
        )
        if isinstance(render_warning_a, dict) or isinstance(render_warning_b, dict):
            rows.append(
                _render_warning_row(
                    state_key,
                    render_warning_a
                    if isinstance(render_warning_a, dict)
                    else None,
                    render_warning_b
                    if isinstance(render_warning_b, dict)
                    else None,
                )
            )

    export_option_comparisons = _compare_export_options(
        captured_states_a, captured_states_b
    )
    comparison_matches = sum(row["status"] == "MATCH" for row in rows)
    comparison_differences = sum(row["status"] == "DIFFERENT" for row in rows)
    exported_matches = sum(
        row["status"] == "MATCH" for row in exported_data_comparisons
    )
    exported_differences = sum(
        row["status"] == "DIFFERENT" for row in exported_data_comparisons
    )
    verified_checks = (
        comparison_matches
        + comparison_differences
        + exported_matches
        + exported_differences
    )
    successful_checks = comparison_matches + exported_matches

    def screenshot_manifest(states: list[DashboardState]) -> dict[str, dict[str, Any]]:
        manifest = {}
        for state in states:
            metadata = state.extracted.get("dashboard_metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            manifest[state.state_key] = {
                "path": (
                    str(state.screenshot)
                    if _state_capture_status(state) == "captured"
                    else None
                ),
                "capture_status": _state_capture_status(state),
                "capture_error": state.extracted.get("capture_error"),
                "render_unverified": state.extracted.get("render_unverified"),
                "image_width": metadata.get("image_width"),
                "image_height": metadata.get("image_height"),
                "report_title": metadata.get("report_title"),
                "dashboard_title": metadata.get("dashboard_title"),
                "refresh_timestamp": metadata.get("refresh_timestamp"),
                "workspace": metadata.get("workspace_name") or metadata.get("workspace"),
                "semantic_model": metadata.get("semantic_model_name")
                or metadata.get("semantic_model"),
            }
        return manifest

    screenshot_evidence = {
        "dashboard_a": screenshot_manifest(states_a),
        "dashboard_b": screenshot_manifest(states_b),
    }
    result = {
        "schema_version": "2.0",
        "screenshot_evidence": screenshot_evidence,
        "summary": {
            "matched_states": len(set(by_key_a) & set(by_key_b)),
            "dashboard_a_only_states": sorted(set(by_key_a) - set(by_key_b)),
            "dashboard_b_only_states": sorted(set(by_key_b) - set(by_key_a)),
            "matches": comparison_matches,
            "differences": comparison_differences,
            "informational": sum(row["status"] == "INFO" for row in rows),
            "unverified": sum(row["status"] == "UNVERIFIED" for row in rows),
            "title_matches": sum(
                row["status"] == "MATCH" for row in title_comparison_rows
            ),
            "title_differences": sum(
                row["status"] == "DIFFERENT" for row in title_comparison_rows
            ),
            "title_informational": sum(
                row["status"] == "INFO" for row in title_comparison_rows
            ),
            "title_unverified": sum(
                row["status"] == "UNVERIFIED" for row in title_comparison_rows
            ),
            "title_status_counts": {
                title_status: sum(
                    row.get("title_status") == title_status
                    for row in title_comparison_rows
                )
                for title_status in (
                    "unchanged",
                    "renamed",
                    "added",
                    "removed",
                    "unverified",
                    "moved",
                    "reclassified",
                    "classification_changed",
                    "association_changed",
                    "metadata_changed",
                    "identity_changed",
                )
            },
            "metadata_identity_warnings": sum(
                row["identity_warning"] for row in metadata_comparisons
            ),
            "metadata_refresh_differences": sum(
                row["metadata_field"] == "refresh_timestamp"
                and row["status"] == "INFO"
                for row in metadata_comparisons
            ),
            "export_option_matches": sum(
                row["status"] == "MATCH" for row in export_option_comparisons
            ),
            "export_option_differences": sum(
                row["status"] == "DIFFERENT" for row in export_option_comparisons
            ),
            "export_option_unverified": sum(
                row["status"] == "UNVERIFIED" for row in export_option_comparisons
            ),
            "exported_data_matches": exported_matches,
            "exported_data_differences": exported_differences,
            "exported_data_unverified": sum(
                row["status"] == "UNVERIFIED" for row in exported_data_comparisons
            ),
            "verified_checks": verified_checks,
            "successful_checks": successful_checks,
            "success_percentage": (
                round(successful_checks * 100 / verified_checks, 2)
                if verified_checks
                else None
            ),
        },
        "comparisons": rows,
        "title_comparisons": title_comparison_rows,
        "metadata_comparisons": metadata_comparisons,
        "export_option_comparisons": export_option_comparisons,
        "exported_data_comparisons": exported_data_comparisons,
    }
    records, kpis = _build_canonical_records(
        rows,
        title_comparison_rows,
        metadata_comparisons,
        export_option_comparisons,
        exported_data_comparisons,
        states_a,
        states_b,
    )
    result["comparison_records"] = records
    result["kpis"] = kpis
    result["visual_filter"] = build_visual_filter_result(states_a, states_b)
    _validate_comparison_output(result)
    return result


def merge_incremental_comparisons(
    partial_results: dict[str, dict[str, Any]],
    states_a: list[DashboardState],
    states_b: list[DashboardState],
) -> dict[str, Any]:
    if not partial_results:
        return compare_dashboards(states_a, states_b)

    ordered_results = [
        partial_results[key] for key in sorted(partial_results)
    ]
    rows = [
        row
        for result in ordered_results
        for row in result["comparisons"]
    ]
    title_rows = [
        row
        for result in ordered_results
        for row in result["title_comparisons"]
    ]
    metadata_rows = [
        row
        for result in ordered_results
        for row in result["metadata_comparisons"]
    ]
    export_option_rows = [
        row
        for result in ordered_results
        for row in result["export_option_comparisons"]
    ]
    exported_data_rows = [
        row
        for result in ordered_results
        for row in result["exported_data_comparisons"]
    ]

    comparison_matches = sum(row["status"] == "MATCH" for row in rows)
    comparison_differences = sum(row["status"] == "DIFFERENT" for row in rows)
    exported_matches = sum(
        row["status"] == "MATCH" for row in exported_data_rows
    )
    exported_differences = sum(
        row["status"] == "DIFFERENT" for row in exported_data_rows
    )
    verified_checks = (
        comparison_matches
        + comparison_differences
        + exported_matches
        + exported_differences
    )
    successful_checks = comparison_matches + exported_matches
    keys_a = {state.state_key.casefold() for state in states_a}
    keys_b = {state.state_key.casefold() for state in states_b}
    title_statuses = (
        "unchanged",
        "renamed",
        "added",
        "removed",
        "unverified",
        "moved",
        "reclassified",
        "classification_changed",
        "association_changed",
        "metadata_changed",
        "identity_changed",
    )

    def screenshot_manifest(
        states: list[DashboardState],
    ) -> dict[str, dict[str, Any]]:
        manifest = {}
        for state in states:
            metadata = state.extracted.get("dashboard_metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            manifest[state.state_key] = {
                "path": (
                    str(state.screenshot)
                    if _state_capture_status(state) == "captured"
                    else None
                ),
                "capture_status": _state_capture_status(state),
                "capture_error": state.extracted.get("capture_error"),
                "render_unverified": state.extracted.get("render_unverified"),
                "image_width": metadata.get("image_width"),
                "image_height": metadata.get("image_height"),
                "report_title": metadata.get("report_title"),
                "dashboard_title": metadata.get("dashboard_title"),
                "refresh_timestamp": metadata.get("refresh_timestamp"),
                "workspace": metadata.get("workspace_name")
                or metadata.get("workspace"),
                "semantic_model": metadata.get("semantic_model_name")
                or metadata.get("semantic_model"),
            }
        return manifest

    result = {
        "schema_version": "2.0",
        "screenshot_evidence": {
            "dashboard_a": screenshot_manifest(states_a),
            "dashboard_b": screenshot_manifest(states_b),
        },
        "summary": {
            "matched_states": len(keys_a & keys_b),
            "dashboard_a_only_states": sorted(keys_a - keys_b),
            "dashboard_b_only_states": sorted(keys_b - keys_a),
            "matches": comparison_matches,
            "differences": comparison_differences,
            "informational": sum(row["status"] == "INFO" for row in rows),
            "unverified": sum(row["status"] == "UNVERIFIED" for row in rows),
            "title_matches": sum(
                row["status"] == "MATCH" for row in title_rows
            ),
            "title_differences": sum(
                row["status"] == "DIFFERENT" for row in title_rows
            ),
            "title_informational": sum(
                row["status"] == "INFO" for row in title_rows
            ),
            "title_unverified": sum(
                row["status"] == "UNVERIFIED" for row in title_rows
            ),
            "title_status_counts": {
                status: sum(row.get("title_status") == status for row in title_rows)
                for status in title_statuses
            },
            "metadata_identity_warnings": sum(
                row["identity_warning"] for row in metadata_rows
            ),
            "metadata_refresh_differences": sum(
                row["metadata_field"] == "refresh_timestamp"
                and row["status"] == "INFO"
                for row in metadata_rows
            ),
            "export_option_matches": sum(
                row["status"] == "MATCH" for row in export_option_rows
            ),
            "export_option_differences": sum(
                row["status"] == "DIFFERENT" for row in export_option_rows
            ),
            "export_option_unverified": sum(
                row["status"] == "UNVERIFIED" for row in export_option_rows
            ),
            "exported_data_matches": exported_matches,
            "exported_data_differences": exported_differences,
            "exported_data_unverified": sum(
                row["status"] == "UNVERIFIED" for row in exported_data_rows
            ),
            "verified_checks": verified_checks,
            "successful_checks": successful_checks,
            "success_percentage": (
                round(successful_checks * 100 / verified_checks, 2)
                if verified_checks
                else None
            ),
        },
        "comparisons": rows,
        "title_comparisons": title_rows,
        "metadata_comparisons": metadata_rows,
        "export_option_comparisons": export_option_rows,
        "exported_data_comparisons": exported_data_rows,
    }
    records, kpis = _build_canonical_records(
        rows,
        title_rows,
        metadata_rows,
        export_option_rows,
        exported_data_rows,
        states_a,
        states_b,
    )
    result["comparison_records"] = records
    result["kpis"] = kpis
    result["visual_filter"] = build_visual_filter_result(states_a, states_b)
    _validate_comparison_output(result)
    return result


_BOOKMARK_GROUP_PREFIX =  "Bookmark buttons: "


def _bookmark_group_options(name: str) -> list[str] | None:
    if not name.startswith(_BOOKMARK_GROUP_PREFIX):
        return None
    return [
        option.strip()
        for option in name[len(_BOOKMARK_GROUP_PREFIX) :].split(" | ")
        if option.strip()
    ]


def _bookmark_group_row(
    state_key: str,
    filter_a: tuple[str, list[str], list[Any]] | None,
    filter_b: tuple[str, list[str], list[Any]] | None,
    options: list[str],
) -> dict[str, Any]:
    text = ", ".join(options)
    value_a = text if filter_a is not None else "unavailable"
    value_b = text if filter_b is not None else "unavailable"
    return {
        "state": state_key,
        "element": "Bookmark buttons",
        "dashboard_a": value_a,
        "dashboard_b": value_b,
        "dashboard_data_a": {"value": value_a},
        "dashboard_data_b": {"value": value_b},
        "data_source_a": ["bookmark_buttons"] if filter_a else None,
        "data_source_b": ["bookmark_buttons"] if filter_b else None,
        "exportOptionAvailable_a": None,
        "exportOptionAvailable_b": None,
        "export_status_a": None,
        "export_status_b": None,
        "already_exported_a": "N/A",
        "already_exported_b": "N/A",
        "status": "MATCH" if filter_a is not None and filter_b is not None else "DIFFERENT",
    }


def _normalized_filter_values(
    state: DashboardState | None,
) -> dict[str, tuple[str, list[str], list[Any]]]:
    if state is None:
        return {}
    normalized: dict[str, tuple[str, list[str], list[Any]]] = {}
    for name, values in state.filter_values.items():
        canonical_values = []
        for value in values:
            canonical = _canonical_value(value)
            if canonical in {"all", "select all", "(all)"}:
                canonical = "all"
            canonical_values.append(canonical)
        canonical_values.sort(key=lambda value: json.dumps(value, sort_keys=True))
        normalized[_title_signature(name)] = (name, values, canonical_values)
    return normalized


def write_comparison(
    result: dict[str, Any],
    output_dir: Path,
    dashboard_a: str,
    dashboard_b: str,
    job_id: str = "standalone",
    operation_log_path: Path | None = None,
) -> None:
    _validate_comparison_output(result)
    output_dir.mkdir(parents=True, exist_ok=True)
    operation_log_path = operation_log_path or output_dir / "operation_timings.jsonl"
    with measure_stage(
        operation_log_path, job_id=job_id, operation="write_comparison_json"
    ):
        (output_dir / "comparison.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )

    xlsx_started_at = time.perf_counter()
    write_operation_event(
        operation_log_path,
        job_id=job_id,
        operation="write_comparison_xlsx",
        event="started",
        outcome="in_progress",
    )
    with measure_stage(
        operation_log_path,
        job_id=job_id,
        operation="write_visual_validation_xlsx",
    ):
        write_visual_validation_sheet(
            output_dir / "Dashboard_Comparison.xlsx", result
        )
    write_operation_event(
        operation_log_path,
        job_id=job_id,
        operation="write_comparison_xlsx",
        event="finished",
        outcome="success",
        duration_seconds=time.perf_counter() - xlsx_started_at,
    )
    with measure_stage(
        operation_log_path, job_id=job_id, operation="write_comparison_docx"
    ):
        write_docx_report(
            result,
            output_dir / "Dashboard_Comparison.docx",
            dashboard_a,
            dashboard_b,
        )
