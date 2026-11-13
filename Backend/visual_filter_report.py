"""Excel and DOCX output for visual-type and filter validation.

The Excel workbook summarizes visible rendering, filter selections, and layout
while keeping table rows and chart values out of the visual-validation checks.
Screenshot-only styling and host-pane checks remain available for manual review.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from docx import Document
from docx.oxml.ns import qn

from .docx_report import (
    DEFAULT_TESTER_NAME,
    FILTER_EXPECTED_BEHAVIOR,
    RESULT_STATUS_DROPDOWN_LABEL,
    STATUS_FONT_SIZE_PT,
    STATUS_TEXT_COLORS,
    TEMPLATE_PATH,
    _add_heading,
    _add_table,
    _add_title,
    _clear_body,
    _filter_action_summary,
    _set_dropdown_control,
    _status_text_color,
    _style_status_cell,
)
from .visual_filter_comparison import (
    effective_rendering_status,
    same_visual_type,
    same_visual_name,
    DIFFERENT,
    MATCH,
    UNVERIFIED,
    filter_selection_result,
)

VISUAL_VALIDATION_SHEET = "Visual_Validation"
HEADER_FILL = "1F4E78"
FILTER_VALIDATION_HEADERS = [
    "ID",
    "Page / Area",
    "Compare Dashboard",
    "Base Dashboard",
    "Visual / Check",
    "Observation",
    "Result",
]
VISUAL_VALIDATION_HEADERS = [
    "ID",
    "Page / Area",
    "Compare Dashboard",
    "Base Dashboard",
    "Visual Name",
    "Visual / Check",
    "Observation",
    "Result",
]

PASS_LABEL = "PASS"
FAIL_LABEL = "FAIL"
UNVERIFIED_LABEL = "Unable to Verify"
PASS_SIGNOFF = "PASS ☑  FAIL ☐"
FAIL_SIGNOFF = "PASS ☐  FAIL ☑"
STATUS_LABELS = {
    PASS_LABEL,
    FAIL_LABEL,
    "Pass",
    "Fail",
    UNVERIFIED_LABEL,
    PASS_SIGNOFF,
    FAIL_SIGNOFF,
}
VISUAL_VALIDATION_EXPECTED = (
    "The visual should render correctly and maintain consistent appearance, "
    "chart type, positioning, dimensions, titles, labels and formatting across "
    "both dashboards."
)
_MANUAL_VISUAL_REVIEW_CHECKS = {
    "table header formatting",
    "table row formatting",
    "total row formatting",
    "filters pane",
    "dashboard navigation",
}


def _report_status(status: Any) -> str:
    return PASS_LABEL if status == MATCH else FAIL_LABEL


def _signoff_label(status: str) -> str:
    return PASS_SIGNOFF if status == PASS_LABEL else FAIL_SIGNOFF


def _validation_label(status: str) -> str:
    if status == MATCH:
        return "Pass"
    if status == DIFFERENT:
        return "Fail"
    return UNVERIFIED_LABEL


def _combined_validation_status(*statuses: str) -> str:
    if DIFFERENT in statuses:
        return DIFFERENT
    if statuses and all(status == MATCH for status in statuses):
        return MATCH
    return UNVERIFIED


def _style_status_cells(doc: Any) -> None:
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                label = cell.text.strip()
                if not label:
                    for control in cell._tc.xpath(".//w:sdt"):
                        content = control.find(qn("w:sdtContent"))
                        if content is not None:
                            label = "".join(
                                node.text or ""
                                for node in content.iter(qn("w:t"))
                            ).strip()
                            if label:
                                break
                if label not in STATUS_LABELS:
                    continue
                _style_status_cell(cell, label)


def _pass_fail(visual_rows: list[dict[str, Any]], filter_rows: list[dict[str, Any]]) -> str:
    """Pass only when at least one check exists and every check is MATCH."""
    checks = [row.get("status") for row in visual_rows + filter_rows]
    if checks and all(status == MATCH for status in checks):
        return PASS_LABEL
    return FAIL_LABEL


def _status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        PASS_LABEL: sum(row.get("status") == MATCH for row in rows),
        FAIL_LABEL: sum(row.get("status") != MATCH for row in rows),
    }


def _overall(rows: list[dict[str, Any]]) -> str:
    """Answer one question: do the two dashboards match for this section?

    Any status other than MATCH fails the section.
    """
    return (
        PASS_LABEL
        if rows and all(row.get("status") == MATCH for row in rows)
        else FAIL_LABEL
    )


def build_summary(
    dashboard_a: str,
    dashboard_b: str,
    visual_rows: list[dict[str, Any]],
    filter_rows: list[dict[str, Any]],
) -> list[tuple[str, Any]]:
    visual_rows = [
        row for row in visual_rows if not _both_visual_types_unavailable(row)
    ]
    pages = {row["page"] for row in visual_rows} | {row["page"] for row in filter_rows}
    visual_counts = _status_counts(visual_rows)
    filter_counts = _status_counts(filter_rows)
    summary: list[tuple[str, Any]] = [
        ("Dashboard A", dashboard_a),
        ("Dashboard B", dashboard_b),
        ("Pages compared", len(pages)),
        ("Are the visual types and layouts the same?", _overall(visual_rows)),
        ("Are the filters and their configurations the same?", _overall(filter_rows)),
        ("Sign-Off Status", _signoff_label(_pass_fail(visual_rows, filter_rows))),
    ]
    summary += [(f"Visual rows: {s}", n) for s, n in visual_counts.items()]
    summary += [(f"Filter rows: {s}", n) for s, n in filter_counts.items()]
    return summary


def _cell(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value)


_EXCLUDED_VISUAL_PATTERN = re.compile(
    r"logo|info(rmation)?\s*(icon|button)|^info$|data refresh date|"
    r"last refresh|refresh timestamp",
    re.IGNORECASE,
)


def _is_excluded_visual_element(row: dict[str, Any]) -> bool:
    if (
        _display_key(row.get("comparison_notes"))
        == "visual count differs between dashboards"
    ):
        return True
    if any(
        token in _display_key(row.get(field))
        for field in ("visual_type_a", "visual_type_b")
        for token in ("slicer", "dropdown")
    ):
        return True
    return any(
        _EXCLUDED_VISUAL_PATTERN.search(_cell(row.get(field)))
        or _cell(row.get(field)).strip().casefold() == "image / logo"
        for field in (
            "visual_name",
            "visual_type_a",
            "visual_type_b",
        )
    )


def _visual_sheet_rows(visual_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in visual_rows
        if _is_table_or_chart_type(row.get("visual_type_a"))
    ]


def _is_table_or_chart_type(value: Any) -> bool:
    visual_type = _cell(value).casefold()
    return visual_type == "table" or "chart" in visual_type


def _visual_validation_rows(
    visual_rows: list[dict[str, Any]],
    filter_rows: list[dict[str, Any]],
    page_filter_actions: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    filters_by_page: dict[str, list[dict[str, Any]]] = {}
    for row in filter_rows:
        page = _cell(row.get("page"))
        page_key = page.casefold()
        filters_by_page.setdefault(page_key, []).append(row)

    default_filter_rows: dict[str, list[dict[str, Any]]] = {}
    for page_key, rows in filters_by_page.items():
        states: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            state = _cell(row.get("state"))
            states.setdefault(state.casefold(), []).append(row)
        default_state = next(
            (
                state
                for state in states
                if state.partition("|")[2].strip().casefold()
                in {"", "initial", "default state"}
            ),
            next(iter(states), ""),
        )
        default_filter_rows[page_key] = sorted(
            states.get(default_state, []),
            key=lambda row: row.get("aligned_position") or 0,
        )

    validation_rows = []
    for row in _visual_sheet_rows(visual_rows):
        if _both_visual_types_unavailable(row):
            continue
        page = _cell(row.get("page"))
        page_key = page.casefold()
        visual_name = _visual_display_name(row)
        validation_rows.append(
            {
                "page": page,
                "base_visual_type": _cell(row.get("visual_type_a")),
                "action_applied": (page_filter_actions or {}).get(
                    page_key,
                    _filter_action_summary(
                        default_filter_rows.get(page_key, [])
                    ),
                ),
                "expected_result": _visual_expected_result(row, visual_name),
                "status": row.get("status")
                if row.get("status") in {MATCH, UNVERIFIED}
                else "DIFFERENT",
            }
        )
    return validation_rows


def _first_display_value(*values: Any) -> str:
    unavailable = {"", "-", "unknown", "unidentified", "not available"}
    for value in values:
        text = _cell(value)
        if text.casefold() not in unavailable:
            return text
    return "Unidentified"


def _visual_display_name(row: dict[str, Any]) -> str:
    titles = []
    for side in ("a", "b"):
        title = str(
            row.get(f"visual_title_{side}")
            or row.get(f"visual_name_{side}")
            or ""
        ).strip()
        visual_id = str(row.get(f"visual_id_{side}") or "").strip()
        if (
            title
            and _display_key(title) != _display_key(visual_id)
            and _display_key(title)
            not in {"-", "unknown", "unidentified", "not available"}
        ):
            titles.append((side, title))

    if len(titles) == 2 and _display_key(titles[0][1]) != _display_key(titles[1][1]):
        return f"Base: {titles[0][1]}; Compare: {titles[1][1]}"
    if titles:
        return titles[0][1]

    name = str(row.get("visual_name") or "").strip()
    visual_ids = {
        _display_key(row.get("visual_id_a")),
        _display_key(row.get("visual_id_b")),
    }
    if name and _display_key(name) not in visual_ids:
        return name
    return _first_display_value(
        row.get("visual_id_a"),
        row.get("visual_id_b"),
        name,
    )


def _visual_expected_result(row: dict[str, Any], visual_name: str) -> str:
    unavailable_types = {"", "-", "unknown", "unidentified", "not available"}
    type_a = _cell(row.get("visual_type_a"))
    type_b = _cell(row.get("visual_type_b"))
    expected_type = (
        type_a
        if type_a.casefold() not in unavailable_types
        else type_b
        if type_b.casefold() not in unavailable_types
        else ""
    )
    if not expected_type:
        return VISUAL_VALIDATION_EXPECTED
    return (
        f"{visual_name} should render as {expected_type} on both dashboards "
        "with consistent titles and presentation."
    )


def _display_key(value: Any) -> str:
    return " ".join(("" if value is None else str(value)).split()).casefold()


_UNAVAILABLE_VISUAL_TYPES = {
    "",
    "-",
    "–",
    "—",
    "−",
    "missing",
    "unknown",
    "unidentified",
    "undefined",
    "not identified",
    "not available",
    "n/a",
    "na",
    "none",
    "null",
}


def _both_visual_types_unavailable(row: dict[str, Any]) -> bool:
    def unavailable(side: str) -> bool:
        value = _display_key(row.get(f"visual_type_{side}"))
        return value in _UNAVAILABLE_VISUAL_TYPES or (
            bool(value) and set(value) <= {"-", "–", "—", "−"}
        )

    return unavailable("a") and unavailable("b")


def _presentation_kind(row: dict[str, Any]) -> str | None:
    hints = [
        str(row.get(field) or "").casefold()
        for field in (
            "standard_element_name",
            "element_type_a",
            "element_type_b",
        )
    ]
    if any("kpi" in hint or "card" in hint for hint in hints):
        return "KPI"
    if any("matrix" in hint or "table" in hint for hint in hints):
        return "Table"
    if any(
        "chart" in hint
        or any(token in hint for token in ("bar", "line", "area", "pie"))
        for hint in hints
    ):
        return "Chart"
    return None


def _has_displayed_content(value: Any) -> bool:
    if isinstance(value, dict):
        if "raw" in value:
            return _has_displayed_content(value.get("raw"))
        return any(_has_displayed_content(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_displayed_content(item) for item in value)
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().casefold().strip("!") not in {
            "",
            "blank",
            "(blank)",
            "missing",
            "not extracted",
            "unavailable",
            "state unavailable",
        }
    return True


def _table_display_columns(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return []
    categories = data.get("categories")
    if not isinstance(categories, list):
        return []
    return [
        str(item.get("raw") if isinstance(item, dict) else item).strip()
        for item in categories
        if item is not None
        and str(item.get("raw") if isinstance(item, dict) else item).strip()
    ]


def _presentation_side(row: dict[str, Any], side: str, kind: str) -> str:
    suffix = "a" if side == "Base" else "b"
    alignment = str(row.get("alignment_status") or "")
    if (side == "Base" and alignment == "missing_in_a") or (
        side == "Compare" and alignment == "missing_in_b"
    ):
        return "Visual missing"

    data = row.get(f"dashboard_data_{suffix}")
    data = data if isinstance(data, dict) else {}
    element_type = row.get(f"element_type_{suffix}")
    if element_type is None and not _has_displayed_content(data):
        return "Visual not captured"

    if kind == "KPI":
        value = data.get("value")
        if value is None or (
            isinstance(value, str)
            and value.strip().casefold().strip("!") in {"", "blank", "(blank)"}
        ):
            return "Blank / no value displayed"
        return f"Value displayed: {value}"

    if kind == "Table":
        columns = _table_display_columns(data)
        if columns:
            return f"Matrix/Table displayed; columns: {', '.join(columns)}"
        return "Matrix/Table displayed; column headers not captured"

    plotted_data = data.get("values") or data.get("rows")
    if _has_displayed_content(plotted_data):
        return "Chart displayed"
    return "Empty or unverified chart (no plotted values extracted)"


def _presentation_match(
    row: dict[str, Any], kind: str
) -> tuple[str, str]:
    data_a = row.get("dashboard_data_a")
    data_b = row.get("dashboard_data_b")
    data_a = data_a if isinstance(data_a, dict) else {}
    data_b = data_b if isinstance(data_b, dict) else {}
    if row.get("screenshot_comparison_skipped"):
        return (
            UNVERIFIED,
            "Visual rendering requires screenshot review; exported data is excluded.",
        )

    if kind == "KPI":
        value_a = data_a.get("value")
        value_b = data_b.get("value")
        visible_a = _has_displayed_content(value_a)
        visible_b = _has_displayed_content(value_b)
        if visible_a and visible_b:
            return (
                MATCH,
                "KPI content is displayed on both dashboards; numeric values are excluded from visual validation.",
            )
        if visible_a != visible_b:
            return (
                DIFFERENT,
                "KPI content is displayed on only one dashboard; this is a rendering difference.",
            )
        return UNVERIFIED, "KPI display state could not be verified on either dashboard."

    if kind == "Table":
        columns_a = _table_display_columns(data_a)
        columns_b = _table_display_columns(data_b)
        if columns_a and columns_b:
            if [_display_key(item) for item in columns_a] == [
                _display_key(item) for item in columns_b
            ]:
                return (
                    MATCH,
                    "Same table column structure; row-level data is excluded and belongs in Data Validation.",
                )
            return DIFFERENT, "Table column structure differs."
        has_content_a = _has_displayed_content(data_a)
        has_content_b = _has_displayed_content(data_b)
        if has_content_a != has_content_b:
            return (
                DIFFERENT,
                "Table content is displayed on only one dashboard; rendering differs.",
            )
        return UNVERIFIED, "Table column structure could not be verified on both dashboards."

    types_a = _display_key(row.get("element_type_a"))
    types_b = _display_key(row.get("element_type_b"))
    if types_a and types_b and types_a != types_b:
        return DIFFERENT, "Visual types differ."
    displayed_a = _has_displayed_content(data_a.get("values") or data_a.get("rows"))
    displayed_b = _has_displayed_content(data_b.get("values") or data_b.get("rows"))
    if displayed_a and displayed_b:
        return (
            MATCH,
            "Charts render on both dashboards; chart data values are excluded from visual validation.",
        )
    if displayed_a != displayed_b:
        return DIFFERENT, "A chart renders on only one dashboard."
    return UNVERIFIED, "Chart rendering could not be verified from the available evidence."


def visual_functional_log_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    write_visual_validation_sheet(None, result, rows_out=rows)
    return rows


def filter_validation_log_rows(result: dict[str, Any]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    write_visual_validation_sheet(None, result, filter_rows_out=rows)
    return rows


def visual_validation_log_rows(result: dict[str, Any]) -> list[list[Any]]:
    rows, _ = visual_validation_log_data(result)
    return rows


def visual_validation_log_data(
    result: dict[str, Any],
) -> tuple[list[list[Any]], list[str]]:
    rows: list[list[Any]] = []
    states: list[str] = []
    write_visual_validation_sheet(
        None,
        result,
        visual_rows_out=rows,
        visual_states_out=states,
    )
    return rows, states


def _visual_evidence_filename(
    result: dict[str, Any], side: str, state: str
) -> str | None:
    manifests = result.get("screenshot_evidence") or {}
    if not isinstance(manifests, dict):
        return None
    evidence = manifests.get(side) or {}
    if not isinstance(evidence, dict):
        return None
    item = next(
        (
            value
            for key, value in evidence.items()
            if str(key).casefold() == state.casefold()
            and isinstance(value, dict)
        ),
        {},
    )
    path = item.get("path")
    if not path or item.get("capture_status") != "captured":
        return None
    screenshot = Path(str(path))
    return screenshot.name if screenshot.is_file() else None


def _visual_evidence_note(result: dict[str, Any], state: str) -> str:
    filenames = []
    unavailable = []
    for side, label in (
        ("dashboard_a", "Base Dashboard"),
        ("dashboard_b", "Compare Dashboard"),
    ):
        filename = _visual_evidence_filename(result, side, state)
        if filename:
            filenames.append(filename)
        else:
            unavailable.append(label)
    if not filenames:
        return " Screenshot evidence unavailable."
    note = f" Evidence: {', '.join(filenames)}."
    if unavailable:
        note += f" Screenshot evidence unavailable for {', '.join(unavailable)}."
    return note


def _visual_missing(value: Any) -> bool:
    normalized = _display_key(value)
    return normalized in {
        "missing",
        "visual missing",
        "missing visual",
        "not present",
    }


def _table_columns(value: Any) -> list[str]:
    match = re.search(r"\bcolumns:\s*(.*)", str(value or ""), re.IGNORECASE)
    if match is None:
        return []
    return [column.strip() for column in match.group(1).split(",") if column.strip()]


def _visual_kind(row: dict[str, Any]) -> str:
    text = " ".join(
        str(row.get(key) or "")
        for key in ("visual_check", "base", "compare")
    ).casefold()
    if "matrix" in text:
        return "Matrix"
    if "table" in text:
        return "Table"
    if "chart" in text:
        return "Chart"
    if "kpi" in text:
        return "KPI"
    return "Visual"


def _brief_visual_side(value: Any) -> str:
    text = " ".join(str(value or "").split())
    normalized = text.casefold()
    if _visual_missing(text):
        return "missing"
    if "columns:" in normalized or "table_header" in normalized:
        return "table headers"
    if "blank" in normalized:
        return "blank"
    if "error" in normalized or "failed" in normalized:
        return "rendering error"
    if len(text) <= 48 and ":" not in text:
        return text
    return "different visual content"


def _visual_log_observation(row: dict[str, Any], result: dict[str, Any]) -> str:
    observation = " ".join(str(row.get("observation") or "").split())
    lowered = observation.casefold()
    status = str(row.get("status") or "").casefold()
    base = row.get("base")
    compare = row.get("compare")
    state = str(row.get("state") or "")
    kind = _visual_kind(row)
    base_columns = _table_columns(base)
    compare_columns = _table_columns(compare)

    if status == "fail":
        if _visual_missing(base) != _visual_missing(compare):
            missing_side = (
                "Base Dashboard"
                if _visual_missing(base)
                else "Compare Dashboard"
            )
            reason = f"{kind} visual is missing from {missing_side}."
        elif (
            base_columns
            and compare_columns
            and [column.casefold() for column in base_columns]
            != [column.casefold() for column in compare_columns]
        ):
            base_normalized = [column.casefold() for column in base_columns]
            compare_normalized = [column.casefold() for column in compare_columns]
            if set(base_normalized) == set(compare_normalized):
                if len(base_columns) != len(compare_columns):
                    reason = (
                        f"Table header count differs: Base has "
                        f"{len(base_columns)} columns; Compare has "
                        f"{len(compare_columns)}."
                    )
                else:
                    position = next(
                        index
                        for index, (base_column, compare_column) in enumerate(
                            zip(base_columns, compare_columns), start=1
                        )
                        if base_column.casefold() != compare_column.casefold()
                    )
                    reason = (
                        f"Table column order differs at position {position}: "
                        f"Base has {base_columns[position - 1]}; "
                        f"Compare has {compare_columns[position - 1]}."
                    )
            else:
                missing_from_compare = [
                    column
                    for column in base_columns
                    if column.casefold() not in set(compare_normalized)
                ]
                missing_from_base = [
                    column
                    for column in compare_columns
                    if column.casefold() not in set(base_normalized)
                ]
                if missing_from_compare and missing_from_base:
                    reason = (
                        f"Table headers differ: Compare is missing "
                        f"{', '.join(missing_from_compare)} and has "
                        f"{', '.join(missing_from_base)} instead."
                    )
                elif missing_from_compare:
                    reason = (
                        "Table headers missing from Compare Dashboard: "
                        f"{', '.join(missing_from_compare)}."
                    )
                else:
                    reason = (
                        "Table headers missing from Base Dashboard: "
                        f"{', '.join(missing_from_base)}."
                    )
            return reason + _visual_evidence_note(result, state)
        elif base_columns and compare_columns and base_columns != compare_columns:
            position = next(
                index
                for index, (base_column, compare_column) in enumerate(
                    zip(base_columns, compare_columns), start=1
                )
                if base_column != compare_column
            )
            reason = (
                f"Table header text differs at position {position}: "
                f"Base has {base_columns[position - 1]}; "
                f"Compare has {compare_columns[position - 1]}."
            )
            return reason + _visual_evidence_note(result, state)
        elif bool(base_columns) != bool(compare_columns):
            side = (
                "Compare Dashboard"
                if base_columns
                else "Base Dashboard"
            )
            reason = f"Table headers could not be compared on {side}."
        elif "type differs" in lowered:
            reason = observation.rstrip(".") + "."
        elif "blank" in lowered or "error" in lowered:
            base_state = _brief_visual_side(base)
            compare_state = _brief_visual_side(compare)
            if base_state in {"blank", "rendering error"}:
                side = "Base Dashboard"
            elif compare_state in {"blank", "rendering error"}:
                side = "Compare Dashboard"
            else:
                side = "one dashboard"
            reason = f"{kind} visual has a blank or rendering error on {side}."
        elif "layout differs" in lowered:
            reason = f"{kind} visual layout differs between dashboards."
        elif "only one dashboard" in lowered:
            base_state = _brief_visual_side(base)
            compare_state = _brief_visual_side(compare)
            if base_state == "missing":
                reason = f"{kind} visual is missing from Base Dashboard."
            elif compare_state == "missing":
                reason = f"{kind} visual is missing from Compare Dashboard."
            else:
                reason = (
                    f"{kind} visual differs between dashboards; "
                    "the missing side could not be identified."
                )
        else:
            reason = (
                f"Visual differs: Base Dashboard shows "
                f"{_brief_visual_side(base)}; Compare Dashboard shows "
                f"{_brief_visual_side(compare)}."
            )
        return reason + _visual_evidence_note(result, state)

    if base_columns and compare_columns and base_columns == compare_columns:
        return "Table headers match between both dashboards."
    if "screenshot styling requires visual review" in lowered:
        return (
            f"{kind} visual types match. "
            "Screenshot styling requires separate visual review."
        )
    if "same visual type" in lowered:
        return f"{kind} visual types match between both dashboards."
    if (
        "columns:" in lowered
        or len(observation) > 160
        or re.search(r"\b[A-Za-z]+_\d+\b", observation)
    ):
        return f"{kind} visual matches between both dashboards."
    return observation


def write_visual_validation_sheet(
    path: Path | None,
    result: dict[str, Any],
    rows_out: list[dict[str, Any]] | None = None,
    filter_rows_out: list[list[Any]] | None = None,
    visual_rows_out: list[list[Any]] | None = None,
    visual_states_out: list[str] | None = None,
) -> None:
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as ExcelImage
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation
    from openpyxl.formatting.rule import CellIsRule

    def style_status_cell(cell: Any, status: str) -> None:
        cell.fill = PatternFill(fill_type=None)
        cell.font = Font(
            name=cell.font.name or "Calibri",
            size=11,
            bold=False,
            color=_status_text_color(status),
        )

    def add_status_formatting(cell_range: str) -> None:
        for label, color in (
            ("Pass", STATUS_TEXT_COLORS["pass"]),
            ("Fail", STATUS_TEXT_COLORS["fail"]),
        ):
            sheet.conditional_formatting.add(
                cell_range,
                CellIsRule(
                    operator="equal",
                    formula=[f'"{label}"'],
                    font=Font(
                        size=11,
                        bold=False,
                        color=color,
                    ),
                ),
            )

    expected_result = (
        "Each visual should have consistent appearance, rendering, chart type, "
        "positioning, dimensions, titles, labels and formatting across both dashboards."
    )
    summary_headers = FILTER_VALIDATION_HEADERS
    visual_summary_headers = VISUAL_VALIDATION_HEADERS
    headers = [
        "ID",
        "Page / Area",
        "Visual Name",
        "Action / Filter Applied",
        "Expected Result",
        "Base Visual Type",
        "Compare Visual Type",
        "Base Position",
        "Compare Position",
        "Base Size",
        "Compare Size",
        "Observation",
        "Result",
        "Base Dashboard Screenshot",
        "Compare Dashboard Screenshot",
    ]
    visual_filter = result.get("visual_filter") or {}
    visual_rows = [
        row
        for row in visual_filter.get("visual_rows") or []
        if isinstance(row, dict)
        and not _is_excluded_visual_element(row)
        and not _both_visual_types_unavailable(row)
    ]
    filter_rows = [
        row
        for row in visual_filter.get("filter_rows") or []
        if isinstance(row, dict)
    ]
    def row_key(row: dict[str, Any], name_field: str) -> tuple[str, str, str]:
        return (
            _display_key(row.get("state")),
            _display_key(row.get("page")),
            _display_key(row.get(name_field)),
        )

    def filter_present_on_both_sides(row: dict[str, Any]) -> bool:
        unavailable = {"", "-", "missing", "not captured", "unknown"}
        return all(
            _display_key(row.get(field)) not in {"", "-", "missing"}
            for field in ("filter_type_a", "filter_type_b")
        ) and all(
            _display_key(row.get(field)) not in unavailable
            for field in ("selected_values_a", "selected_values_b")
        )

    filter_visual_keys = {
        row_key(visual, "visual_name")
        for visual in visual_rows
        if any(
            _display_key(visual.get(f"visual_type_{side}")) == "missing"
            for side in ("a", "b")
        )
        for filter_row in filter_rows
        if row_key(filter_row, "filter_name") == row_key(visual, "visual_name")
        and filter_present_on_both_sides(filter_row)
    }
    visual_rows = [
        row for row in visual_rows if row_key(row, "visual_name") not in filter_visual_keys
    ]
    visual_names = {
        _display_key(value)
        for row in visual_rows
        for value in (
            row.get("visual_name"),
            row.get("visual_id_a"),
            row.get("visual_id_b"),
        )
        if value
    }
    visual_only_checks = {
        "filters pane",
        "dashboard navigation",
        "divider line",
        "table header formatting",
        "table row formatting",
        "total row formatting",
    }
    filter_rows = [
        row
        for row in filter_rows
        if not (
            _display_key(row.get("filter_name")) in visual_only_checks
            or _display_key(row.get("filter_name")).endswith(" position & size")
            or _display_key(row.get("filter_name")).startswith("visual_")
            or (
                _display_key(row.get("filter_name")) in visual_names
                and row_key(row, "filter_name") not in filter_visual_keys
                and all(
                    _display_key(row.get(field))
                    in {"", "-", "unknown", "missing"}
                    for field in ("filter_type_a", "filter_type_b")
                )
            )
        )
    ]
    evidence = result.get("screenshot_evidence") or {}
    base_evidence = evidence.get("dashboard_a") or {}
    compare_evidence = evidence.get("dashboard_b") or {}
    groups: dict[str, dict[str, Any]] = {}

    def get_group(state: str, page: str | None = None) -> dict[str, Any]:
        state = state.strip() or f"{page or 'Unknown page'}|initial"
        key = state.casefold()
        if key not in groups:
            groups[key] = {
                "state": state,
                "page": page or state.split("|", 1)[0].strip(),
                "visual_rows": [],
                "unmatched_side": None,
            }
        return groups[key]

    for row in visual_rows:
        group = get_group(
            str(row.get("state") or ""),
            str(row.get("page") or "Unknown page"),
        )
        group["visual_rows"].append(row)

    if isinstance(base_evidence, dict) and isinstance(compare_evidence, dict):
        compare_keys = {str(key).casefold() for key in compare_evidence}
        for state in base_evidence:
            if str(state).casefold() in compare_keys:
                get_group(str(state))

    for unmatched in visual_filter.get("unmatched_states") or []:
        side, separator, state = str(unmatched).partition(": ")
        if separator:
            get_group(state)["unmatched_side"] = side.split()[0].upper()

    def priority(state: str) -> int:
        _page, separator, suffix = state.partition("|")
        if not separator or suffix.casefold() == "initial":
            return 0
        selections = [part.strip() for part in suffix.split(";") if part.strip()]
        if selections and all(
            "=" in selection
            and selection.rsplit("=", 1)[1].strip().casefold()
            in {"all", "(all)", "is (all)"}
            for selection in selections
        ):
            return 1
        return 2

    has_scenario_groups = any(
        "|scenario_" in str(group["state"]).casefold()
        for group in groups.values()
    )
    page_groups: dict[str, dict[str, Any]] = {}
    if has_scenario_groups:
        page_groups = {
            str(group["state"]).casefold(): group for group in groups.values()
        }
    else:
        for group in groups.values():
            key = str(group["page"]).casefold()
            current = page_groups.get(key)
            if current is None or priority(group["state"]) < priority(current["state"]):
                page_groups[key] = group

    def evidence_for(manifest: Any, state: str) -> dict[str, Any]:
        if not isinstance(manifest, dict):
            return {}
        return next(
            (
                item
                for key, item in manifest.items()
                if str(key).casefold() == state.casefold()
                and isinstance(item, dict)
            ),
            {},
        )

    def screenshot_path(item: dict[str, Any]) -> Path | None:
        value = item.get("path")
        if not value or item.get("capture_status") != "captured":
            return None
        candidate = Path(str(value))
        return candidate if candidate.is_file() else None

    def action_for(group: dict[str, Any]) -> str:
        state = str(group["state"]).casefold()
        actions = []
        selected = sorted(
            (
                row
                for row in filter_rows
                if str(row.get("state") or "").casefold() == state
            ),
            key=lambda row: row.get("aligned_position") or 0,
        )
        for row in selected:
            name = str(row.get("filter_name") or "").strip()
            if not name:
                continue
            value_a = str(row.get("selected_values_a") or "-").strip() or "-"
            value_b = str(row.get("selected_values_b") or "-").strip() or "-"
            value = (
                value_a
                if value_a.casefold() == value_b.casefold()
                else f"Base: {value_a}; Compare: {value_b}"
            )
            actions.append(f"{name}: {value}")
        if actions:
            return "\n".join(actions)
        rows = group["visual_rows"]
        return str(
            rows[0].get("action_applied")
            if rows
            else "Default filter state"
        )

    def related_title_issues(visual: dict[str, Any], state: str) -> list[str]:
        names = {
            " ".join(str(value).split()).casefold()
            for value in (
                visual.get("visual_name"),
                visual.get("visual_id_a"),
                visual.get("visual_id_b"),
            )
            if value
        }
        issues = []
        for row in result.get("title_comparisons") or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("state") or "").casefold() != state.casefold():
                continue
            if row.get("status") != DIFFERENT:
                continue
            associations = (
                row.get("associated_visual_a"),
                row.get("associated_visual_b"),
                row.get("element_text_a"),
                row.get("element_text_b"),
            )
            if names.intersection(
                " ".join(str(value).split()).casefold()
                for value in associations
                if value
            ):
                text_a = str(row.get("element_text_a") or "Not captured")
                text_b = str(row.get("element_text_b") or "Not captured")
                issues.append(f"Visual title/label differs: {text_a} vs {text_b}")
        return list(dict.fromkeys(issues))

    def related_title_unverified(visual: dict[str, Any], state: str) -> bool:
        names = {
            _display_key(value)
            for value in (
                visual.get("visual_name"),
                visual.get("visual_id_a"),
                visual.get("visual_id_b"),
            )
            if value
        }
        return any(
            isinstance(row, dict)
            and str(row.get("state") or "").casefold() == state.casefold()
            and row.get("status") == UNVERIFIED
            and names.intersection(
                _display_key(value)
                for value in (
                    row.get("associated_visual_a"),
                    row.get("associated_visual_b"),
                    row.get("element_text_a"),
                    row.get("element_text_b"),
                )
                if value
            )
            for row in result.get("title_comparisons") or []
        )

    summary_sections = (
        "Dashboard / Page Validation",
        "Filter Validation Log",
        "Visual & Functional Validation Log",
    )
    summary_rows_by_section: dict[str, list[dict[str, Any]]] = {
        section: [] for section in summary_sections
    }

    def add_summary_row(
        page: str,
        visual_check: str,
        base: str,
        compare: str,
        observation: str,
        status: str = "",
        section: str = "Visual & Functional Validation Log",
    ) -> None:
        if section == "Visual & Functional Validation Log" and (
            _EXCLUDED_VISUAL_PATTERN.search(visual_check or "")
        ):
            return
        summary_rows_by_section[section].append(
            {
                "page": page,
                "visual_check": visual_check,
                "base": base,
                "compare": compare,
                "observation": observation,
                "status": status,
                "section": section,
                "state": current_state,
            }
        )

    def matching_layout(
        comparison: dict[str, Any], group: dict[str, Any]
    ) -> dict[str, Any] | None:
        names = {
            _display_key(value)
            for value in (
                comparison.get("element"),
                comparison.get("associated_visual_a"),
                comparison.get("associated_visual_b"),
                comparison.get("visual_id_a"),
                comparison.get("visual_id_b"),
            )
            if value
        }
        for visual in group["visual_rows"]:
            visual_names = {
                _display_key(value)
                for value in (
                    visual.get("visual_name"),
                    visual.get("visual_id_a"),
                    visual.get("visual_id_b"),
                )
                if value
            }
            if names.intersection(visual_names):
                return visual
        return None

    def layout_summary(
        layout: dict[str, Any] | None,
    ) -> tuple[str, str, str, str]:
        if layout is None:
            return (
                "Not Available",
                "Not Available",
                "Position and dimensions could not be verified from metadata.",
                UNVERIFIED,
            )
        position_a = str(layout.get("position_a") or "-").strip()
        position_b = str(layout.get("position_b") or "-").strip()
        size_a = str(layout.get("size_a") or "-").strip()
        size_b = str(layout.get("size_b") or "-").strip()
        base = f"Position: {position_a}; Size: {size_a}"
        compare = f"Position: {position_b}; Size: {size_b}"
        has_geometry = all(
            value not in {"", "-", "Not Available"}
            for value in (position_a, position_b, size_a, size_b)
        )
        notes = str(layout.get("comparison_notes") or "")
        if "layout differs" in notes.casefold():
            return (
                base,
                compare,
                "Position or dimensions differ beyond the configured layout tolerance. "
                + notes,
                DIFFERENT,
            )
        if has_geometry:
            return (
                base,
                compare,
                "Positions and dimensions are within the configured layout tolerances.",
                MATCH,
            )
        return (
            base,
            compare,
            "Position and dimensions could not be fully verified.",
            UNVERIFIED,
        )

    def with_layout(
        description: str,
        visual: dict[str, Any] | None,
        side: str,
    ) -> str:
        if visual is None:
            return description
        suffix = "a" if side == "Base" else "b"
        visual_type = str(visual.get(f"visual_type_{suffix}") or "").strip()
        position = str(visual.get(f"position_{suffix}") or "").strip()
        size = str(visual.get(f"size_{suffix}") or "").strip()
        if visual_type.casefold() == "missing":
            return "Visual missing"
        details = [
            item
            for item in (
                f"Type: {visual_type}" if visual_type else "",
                f"Position: {position}" if position and position != "-" else "",
                f"Size: {size}" if size and size != "-" else "",
            )
            if item
        ]
        return description + (f"\n{'; '.join(details)}" if details else "")

    current_state = ""
    for group in page_groups.values():
        state = str(group["state"])
        current_state = state
        page = str(group["page"])
        state_key = state.casefold()
        base_state = evidence_for(base_evidence, state)
        compare_state = evidence_for(compare_evidence, state)

        base_title = str(base_state.get("dashboard_title") or "").strip()
        compare_title = str(compare_state.get("dashboard_title") or "").strip()
        header_status = (
            MATCH
            if base_title
            and compare_title
            and _display_key(base_title) == _display_key(compare_title)
            else DIFFERENT
            if base_title and compare_title
            else UNVERIFIED
        )
        if base_title or compare_title:
            add_summary_row(
                page,
                "Dashboard Header",
                base_title or "Not captured",
                compare_title or "Not captured",
                "Canvas header matches."
                if header_status == MATCH
                else "Canvas headers differ."
                if header_status == DIFFERENT
                else "Canvas header could not be verified on both dashboards.",
                _validation_label(header_status),
                "Dashboard / Page Validation",
            )

        page_filters = sorted(
            (
                row
                for row in filter_rows
                if str(row.get("state") or "").casefold() == state_key
            ),
            key=lambda row: row.get("aligned_position") or 0,
        )
        for filter_row in page_filters:
            selected_a = str(filter_row.get("selected_values_a") or "").strip()
            selected_b = str(filter_row.get("selected_values_b") or "").strip()
            recorded_status = filter_row.get("status")
            selection_status, selection_observation = filter_selection_result(
                selected_a,
                selected_b,
            )
            if selection_status == UNVERIFIED:
                filter_observation = _cell(filter_row.get("comparison_notes"))
                if filter_observation == "-":
                    filter_observation = selection_observation
                elif selection_observation not in filter_observation:
                    filter_observation = (
                        f"{filter_observation}; {selection_observation}"
                    )
                filter_status = (
                    DIFFERENT
                    if recorded_status == DIFFERENT
                    else UNVERIFIED
                )
            elif recorded_status == DIFFERENT:
                filter_status = DIFFERENT
                filter_observation = _cell(filter_row.get("comparison_notes"))
                if filter_observation == "-":
                    filter_observation = selection_observation
            else:
                filter_status = selection_status
                filter_observation = selection_observation
            add_summary_row(
                page,
                str(filter_row.get("filter_name") or "Filter"),
                selected_a or "Not captured",
                selected_b or "Not captured",
                filter_observation,
                _validation_label(filter_status),
                "Filter Validation Log",
            )

        page_comparisons = [
            row
            for row in result.get("comparisons") or []
            if isinstance(row, dict)
            and str(row.get("state") or "").casefold() == state_key
            and _presentation_kind(row) is not None
        ]
        table_comparison_count = sum(
            _presentation_kind(row) == "Table" for row in page_comparisons
        )
        matched_layout_names: set[str] = set()
        for comparison in page_comparisons:
            kind = _presentation_kind(comparison)
            if kind is None:
                continue
            layout = matching_layout(comparison, group)
            name = (
                _visual_display_name(layout)
                if layout is not None
                else str(comparison.get("element") or f"Unnamed {kind}").strip()
            )
            if layout is not None:
                matched_layout_names.update(
                    _display_key(value)
                    for value in (
                        layout.get("visual_name"),
                        layout.get("visual_id_a"),
                        layout.get("visual_id_b"),
                    )
                    if value
                )

            if comparison.get("screenshot_comparison_skipped"):
                base_description = "Screenshot review required; exported data excluded."
                compare_description = "Screenshot review required; exported data excluded."
                observation = (
                    "Rendering and formatting were not assessed automatically; "
                    "review the embedded screenshots."
                )
                visual_status = UNVERIFIED
            else:
                base_description = _presentation_side(comparison, "Base", kind)
                compare_description = _presentation_side(comparison, "Compare", kind)
                visual_status, observation = _presentation_match(comparison, kind)
                title_issues = related_title_issues(comparison, state)
                if title_issues:
                    observation += " " + "; ".join(title_issues)
                    visual_status = DIFFERENT
                elif related_title_unverified(comparison, state):
                    observation += " Related title or label could not be verified."
                    visual_status = _combined_validation_status(
                        visual_status, UNVERIFIED
                    )
                if base_state.get("render_unverified") or compare_state.get(
                    "render_unverified"
                ):
                    observation += " A dashboard is still loading or has an unverified render."
                    if not title_issues:
                        visual_status = UNVERIFIED
                if screenshot_path(base_state) is None or screenshot_path(
                    compare_state
                ) is None:
                    observation += " Screenshot evidence is missing on at least one side."
                    if not title_issues:
                        visual_status = UNVERIFIED

            add_summary_row(
                page,
                name,
                base_description,
                compare_description,
                observation,
                _validation_label(visual_status),
            )
            base_layout, compare_layout, layout_observation, layout_status = (
                layout_summary(layout)
            )
            layout_check = (
                "Table Position & Size"
                if kind == "Table" and table_comparison_count == 1
                else f"{name} Position & Size"
            )
            add_summary_row(
                page,
                layout_check,
                base_layout,
                compare_layout,
                layout_observation,
                _validation_label(layout_status),
            )

        for visual in group["visual_rows"]:
            identity = {
                _display_key(value)
                for value in (
                    visual.get("visual_name"),
                    visual.get("visual_id_a"),
                    visual.get("visual_id_b"),
                )
                if value
            }
            if identity.intersection(matched_layout_names):
                continue
            observation = str(visual.get("comparison_notes") or "").strip()
            type_a = str(visual.get("visual_type_a") or "").strip()
            type_b = str(visual.get("visual_type_b") or "").strip()
            same_name = same_visual_name(
                visual.get("visual_name_a"),
                visual.get("visual_name_b"),
            )
            matching_blank_kpi = (
                same_name
                and type_a.casefold() in {"kpi", "kpi card"}
                and type_b.casefold() in {"kpi", "kpi card"}
                and str(
                    visual.get("rendering_status_a") or ""
                ).casefold()
                == "blank"
                and str(
                    visual.get("rendering_status_b") or ""
                ).casefold()
                == "blank"
                and str(
                    visual.get("visual_confidence_a") or ""
                ).casefold()
                == "high"
                and str(
                    visual.get("visual_confidence_b") or ""
                ).casefold()
                == "high"
            )
            type_unverified = (
                not type_a
                or not type_b
                or {type_a.casefold(), type_b.casefold()}
                & {"unknown", "unidentified"}
                or visual.get("status") == UNVERIFIED
            )
            if matching_blank_kpi:
                visual_status = MATCH
                observation = (
                    "Both dashboards display this KPI as (Blank); "
                    "it matches the Base dashboard."
                )
            elif visual.get("status") == UNVERIFIED and not same_name:
                visual_status = UNVERIFIED
                observation = observation or (
                    "The visual could not be matched confidently from its "
                    "ID or title."
                )
            elif type_a.casefold() == "missing" or type_b.casefold() == "missing":
                visual_status = DIFFERENT
                observation = "Visual is present on only one dashboard."
            elif any(
                effective_rendering_status(
                    visual.get(f"rendering_status_{side}"),
                    visual.get(f"visual_type_{side}") or type_a or type_b,
                )
                in {"blank", "error"}
                for side in ("a", "b")
            ):
                visual_status = DIFFERENT
                observation = observation or "A visual is blank or displays an error."
            elif any(
                str(visual.get(f"rendering_status_{side}") or "").casefold()
                in {"partial", "loading", "unable to verify"}
                for side in ("a", "b")
            ):
                visual_status = UNVERIFIED
                observation = observation or (
                    "Visual rendering could not be verified because the "
                    "screenshot shows an incomplete or unresolved state."
                )
            elif type_unverified and not same_name:
                visual_status = UNVERIFIED
                observation = observation or (
                    "Visual type or rendering could not be verified."
                )
            elif not same_visual_type(type_a, type_b) and not same_name:
                visual_status = DIFFERENT
                observation = f"Visual type differs: Base {type_a}; Compare {type_b}."
            else:
                visual_status = MATCH
                if same_name and type_unverified:
                    observation = (
                        f"Exact visual title matches "
                        f"({visual.get('visual_name_a')}); one or more type "
                        "labels are unavailable and do not reject the match."
                    )
                elif same_name and not same_visual_type(type_a, type_b):
                    observation = (
                        f"Exact visual title matches "
                        f"({visual.get('visual_name_a')}); type labels differ "
                        f"(Base {type_a}; Compare {type_b}) and are not used "
                        "to reject the match."
                    )
                elif (
                    not same_name
                    and visual.get("status") == MATCH
                    and same_visual_type(type_a, type_b)
                ):
                    observation = observation or (
                        "Visual types match; screenshot-derived names are "
                        "informational."
                    )
                else:
                    observation = (
                        "Visual containers have the same type; screenshot "
                        "styling requires visual review."
                    )
            visual_rendered_on_both = all(
                str(visual.get(f"rendering_status_{side}") or "")
                .strip()
                .casefold()
                == "rendered"
                for side in ("a", "b")
            )
            if (
                (base_state.get("render_unverified") or compare_state.get(
                    "render_unverified"
                ))
                and not visual_rendered_on_both
                and not matching_blank_kpi
            ):
                if visual_status != DIFFERENT:
                    visual_status = UNVERIFIED
                observation += " A dashboard is still loading or has an unverified render."
            if screenshot_path(base_state) is None or screenshot_path(
                compare_state
            ) is None:
                if visual_status != DIFFERENT:
                    visual_status = UNVERIFIED
                observation += " Screenshot evidence is missing."
            add_summary_row(
                page,
                _visual_display_name(visual),
                type_a or "Not Available",
                type_b or "Not Available",
                observation,
                _validation_label(visual_status),
            )
            base_layout, compare_layout, layout_observation, layout_status = (
                layout_summary(visual)
            )
            add_summary_row(
                page,
                f"{visual.get('visual_name') or 'Visual'} Position & Size",
                base_layout,
                compare_layout,
                layout_observation,
                _validation_label(layout_status),
            )

        has_table = any(
            _presentation_kind(row) == "Table" for row in page_comparisons
        ) or any(
            any(
                token in str(row.get(f"visual_type_{side}") or "").casefold()
                for token in ("table", "matrix")
            )
            for row in group["visual_rows"]
            for side in ("a", "b")
        )
        if has_table:
            for check in (
                "Table Header Formatting",
                "Table Row Formatting",
                "Total Row Formatting",
            ):
                add_summary_row(
                    page,
                    check,
                    "Screenshot review required",
                    "Screenshot review required",
                    "Formatting is not extracted automatically; select Pass or Fail only after reviewing both screenshots.",
                    UNVERIFIED_LABEL,
                )

        for check in ("Filters Pane", "Dashboard Navigation"):
            add_summary_row(
                page,
                check,
                "Screenshot review required",
                "Screenshot review required",
                "Host-pane/navigation appearance is not classified automatically; review both screenshots before assigning Pass or Fail.",
                UNVERIFIED_LABEL,
            )

    filter_log_rows = [
        [
            f"{section_id:02d}",
            row["page"],
            row["visual_check"],
            row["base"],
            row["compare"],
            row["observation"],
            row["status"],
        ]
        for section_id, row in enumerate(
            (
                row
                for row in summary_rows_by_section["Filter Validation Log"]
                if not str(row["visual_check"]).endswith("Position & Size")
            ),
            start=1,
        )
    ]
    if filter_rows_out is not None:
        filter_rows_out.extend(filter_log_rows)

    visual_log_rows: list[list[Any]] = []
    visual_log_states: list[str] = []
    for row in summary_rows_by_section["Visual & Functional Validation Log"]:
        if (
            row["status"] == UNVERIFIED_LABEL
            and _display_key(row["visual_check"]) in _MANUAL_VISUAL_REVIEW_CHECKS
        ):
            continue
        if str(row["visual_check"]).endswith("Position & Size"):
            continue
        section_id = len(visual_log_rows) + 1
        visual_log_rows.append(
            [
                f"{section_id:02d}",
                row["page"],
                row["compare"],
                row["base"],
                row["visual_check"],
                row["base"],
                _visual_log_observation(row, result),
                row["status"],
            ]
        )
        visual_log_states.append(str(row.get("state") or ""))
    if visual_rows_out is not None:
        visual_rows_out.extend(visual_log_rows)
    if visual_states_out is not None:
        visual_states_out.extend(visual_log_states)

    if rows_out is not None:
        rows_out.extend(
            summary_rows_by_section["Visual & Functional Validation Log"]
        )
    if rows_out is not None or path is None:
        return

    workbook = Workbook()
    try:
        sheet = workbook.active
        sheet.title = VISUAL_VALIDATION_SHEET
        sheet.append(["Visual Differences Summary"])
        sheet.merge_cells(
            start_row=1,
            start_column=1,
            end_row=1,
            end_column=len(visual_summary_headers),
        )
        sheet.cell(1, 1).font = Font(bold=True, color="FFFFFF", size=14)
        sheet.cell(1, 1).fill = PatternFill("solid", fgColor=HEADER_FILL)
        summary_validation = DataValidation(
            type="list",
            formula1='"Pass,Fail,Unable to Verify"',
            allow_blank=True,
        )
        sheet.add_data_validation(summary_validation)
        summary_status_rows: list[tuple[int, int]] = []
        for section in summary_sections:
            if (
                section == "Dashboard / Page Validation"
                and not summary_rows_by_section[section]
            ):
                continue
            section_title_row = sheet.max_row + 1
            sheet.cell(section_title_row, 1, section)
            section_headers = (
                visual_summary_headers
                if section == "Visual & Functional Validation Log"
                else summary_headers
            )
            sheet.merge_cells(
                start_row=section_title_row,
                start_column=1,
                end_row=section_title_row,
                end_column=len(section_headers),
            )
            sheet.cell(section_title_row, 1).font = Font(
                bold=True, color="FFFFFF"
            )
            sheet.cell(section_title_row, 1).fill = PatternFill(
                "solid", fgColor=HEADER_FILL
            )
            summary_header_row = section_title_row + 1
            sheet.append(section_headers)
            for cell in sheet[summary_header_row]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor=HEADER_FILL)
                cell.alignment = Alignment(wrap_text=True, vertical="center")

            section_id = 0
            for row in summary_rows_by_section[section]:
                if (
                    section == "Visual & Functional Validation Log"
                    and row["status"] == UNVERIFIED_LABEL
                    and _display_key(row["visual_check"])
                    in _MANUAL_VISUAL_REVIEW_CHECKS
                ):
                    continue
                if str(row["visual_check"]).endswith("Position & Size"):
                    continue
                section_id += 1
                if section == "Visual & Functional Validation Log":
                    values = visual_log_rows[section_id - 1]
                else:
                    values = filter_log_rows[section_id - 1]
                sheet.append(values)
                row_index = sheet.max_row
                status_column = (
                    8
                    if section == "Visual & Functional Validation Log"
                    else 7
                )
                summary_status_rows.append((row_index, status_column))
                status_coordinate = (
                    f"{get_column_letter(status_column)}{row_index}"
                )
                summary_validation.add(status_coordinate)
                style_status_cell(
                    sheet.cell(row_index, status_column), row["status"]
                )
                for cell in sheet[row_index]:
                    cell.alignment = Alignment(wrap_text=True, vertical="top")

        result_summary_row = sheet.max_row + 2
        sheet.cell(result_summary_row, 1, "Result Summary")
        sheet.merge_cells(
            start_row=result_summary_row,
            start_column=1,
            end_row=result_summary_row,
            end_column=2,
        )
        sheet.cell(result_summary_row, 1).font = Font(
            bold=True, color="FFFFFF"
        )
        sheet.cell(result_summary_row, 1).fill = PatternFill(
            "solid", fgColor=HEADER_FILL
        )
        result_header_row = result_summary_row + 1
        sheet.cell(result_header_row, 1, "Result")
        sheet.cell(result_header_row, 2, "Count")
        for cell in sheet[result_header_row][:2]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor=HEADER_FILL)
        status_ranges: list[str] = []
        for column in dict.fromkeys(
            status_column for _, status_column in summary_status_rows
        ):
            rows = sorted(
                row_index
                for row_index, status_column in summary_status_rows
                if status_column == column
            )
            start = previous = rows[0]
            for row_index in rows[1:]:
                if row_index != previous + 1:
                    letter = get_column_letter(column)
                    status_ranges.append(
                        f"{letter}{start}:{letter}{previous}"
                        if start != previous
                        else f"{letter}{start}"
                    )
                    start = row_index
                previous = row_index
            letter = get_column_letter(column)
            status_ranges.append(
                f"{letter}{start}:{letter}{previous}"
                if start != previous
                else f"{letter}{start}"
            )
        result_counts = {
            label: (
                "="
                + "+".join(
                    f'COUNTIF({cell_range},"{label}")'
                    for cell_range in status_ranges
                )
                if status_ranges
                else "=0"
            )
            for label in ("Pass", "Fail", UNVERIFIED_LABEL)
        }
        for label, count in result_counts.items():
            sheet.append([label, count])
            style_status_cell(sheet.cell(sheet.max_row, 1), label)
        first_count_row = result_header_row + 1
        last_count_row = result_header_row + len(result_counts)
        sheet.append(["Total Checks", f"=SUM(B{first_count_row}:B{last_count_row})"])

        for row_index, status_column in summary_status_rows:
            letter = get_column_letter(status_column)
            add_status_formatting(f"{letter}{row_index}")

        for column, width in enumerate(
            (8, 22, 32, 32, 38, 26, 58, 12), start=1
        ):
            sheet.column_dimensions[get_column_letter(column)].width = width
        sheet.freeze_panes = "A3"
        workbook.save(path)
    finally:
        workbook.close()


def write_visual_filter_docx(
    path: Path,
    dashboard_a: str,
    dashboard_b: str,
    visual_rows: list[dict[str, Any]],
    filter_rows: list[dict[str, Any]],
    *,
    tester_name: str = DEFAULT_TESTER_NAME,
    report_date: date | str | None = None,
) -> None:
    visual_rows = [
        row for row in visual_rows if not _both_visual_types_unavailable(row)
    ]
    doc = Document(str(TEMPLATE_PATH))
    _clear_body(doc)
    _add_title(doc, f"{dashboard_a} vs {dashboard_b} Power BI Report Validation")
    overall = _pass_fail(visual_rows, filter_rows)
    overall_fill = None

    _add_heading(doc, "1. Dashboard & Environment Details", 1)
    _add_table(
        doc,
        ["Field", "Value"],
        [
            ([label, _cell(value)], overall_fill if label == "Overall Status" else None)
            for label, value in (
                ("Dashboard A", dashboard_a),
                ("Dashboard B", dashboard_b),
                ("Tester Name", tester_name),
                ("Validation Date", _format_date(report_date)),
                ("Overall Status", overall),
            )
        ],
        [3.0, 3.5],
    )

    _add_heading(doc, "2. Filter Validation Log", 1)
    _add_table(
        doc,
        ["ID", "Page / Tab Name", "Filter Name", "Action / Filter Applied", "Expected Behavior", "Result (Pass/Fail)"],
        [
            (
                [
                    f"{index:02d}",
                    _cell(row.get("page")),
                    _cell(row.get("filter_name")),
                    f"A: {_cell(row.get('selected_values_a'))} ({_cell(row.get('selection_mode_a'))})\n"
                    f"B: {_cell(row.get('selected_values_b'))} ({_cell(row.get('selection_mode_b'))})",
                    FILTER_EXPECTED_BEHAVIOR,
                    _result_label(row["status"]),
                ],
                None,
            )
            for index, row in enumerate(filter_rows, start=1)
        ]
        or [(["-", "-", "No filters found", "-", "-", "-"], None)],
        [0.4, 0.9, 1.0, 1.4, 2.0, 0.8],
    )

    _add_heading(doc, "3. Visual & Functional Validation Log", 1)
    validation_rows = _visual_validation_rows(visual_rows, filter_rows)
    visual_table = _add_table(
        doc,
        ["ID", "Page / Area", "Base Visual Type", "Action / Filter Applied", "Expected Result", "Result"],
        [
            (
                [
                    f"{index:02d}",
                    _cell(row.get("page")),
                    _cell(row.get("base_visual_type")),
                    _cell(row.get("action_applied")),
                    _cell(row.get("expected_result")),
                    _result_label(row["status"]),
                ],
                None,
            )
            for index, row in enumerate(validation_rows, start=1)
        ],
        [0.4, 1.0, 1.1, 1.3, 2.0, 0.8],
    )
    for row_index, row in enumerate(validation_rows, start=1):
        _set_dropdown_control(
            visual_table.cell(row_index, 5),
            "Pass" if row["status"] == MATCH else "Fail",
            title=RESULT_STATUS_DROPDOWN_LABEL,
            options=("Pass", "Fail"),
        )

    _add_heading(doc, "4. Summary & Sign-off", 1)
    summary_rows = [
        ([metric, _cell(value)], None)
        for metric, value in build_summary(
            dashboard_a, dashboard_b, visual_rows, filter_rows
        )
        if metric not in ("Dashboard A", "Dashboard B", "Sign-Off Status")
    ]
    _add_table(
        doc,
        ["Metric", "Value"],
        summary_rows,
        [4.5, 2.0],
    )
    signoff_table = _add_table(
        doc,
        [RESULT_STATUS_DROPDOWN_LABEL, "Tester Name", "Sign-Off Date"],
        [
            (
                [
                    "Pass" if overall == PASS_LABEL else "Fail",
                    _cell(tester_name),
                    _format_date(report_date),
                ],
                overall_fill,
            )
        ],
        [2.17, 2.17, 2.16],
    )
    _set_dropdown_control(
        signoff_table.cell(1, 0),
        "Pass" if overall == PASS_LABEL else "Fail",
        title=RESULT_STATUS_DROPDOWN_LABEL,
        options=("Pass", "Fail"),
    )
    _style_status_cells(doc)
    doc.save(str(path))


def _result_label(status: str) -> str:
    return _report_status(status)


def _format_date(report_date: date | str | None) -> str:
    if report_date is None:
        report_date = date.today()
    if isinstance(report_date, date):
        return report_date.strftime("%d-%b-%Y")
    return str(report_date)
