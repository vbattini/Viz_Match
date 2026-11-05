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
    DIFFERENT,
    MATCH,
    UNVERIFIED,
    filter_selection_result,
)

VISUAL_VALIDATION_SHEET = "Visual_Validation"
HEADER_FILL = "1F4E78"

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
        page = _cell(row.get("page"))
        page_key = page.casefold()
        visual_name = _first_display_value(
            row.get("visual_name"),
            row.get("visual_id_a"),
            row.get("visual_id_b"),
        )
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


def write_visual_validation_sheet(
    path: Path | None,
    result: dict[str, Any],
    rows_out: list[dict[str, Any]] | None = None,
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
            size=STATUS_FONT_SIZE_PT,
            bold=True,
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
                        size=STATUS_FONT_SIZE_PT,
                        bold=True,
                        color=color,
                    ),
                ),
            )

    expected_result = (
        "Each visual should have consistent appearance, rendering, chart type, "
        "positioning, dimensions, titles, labels and formatting across both dashboards."
    )
    summary_headers = [
        "ID",
        "Page / Area",
        "Compare Dashboard",
        "Base Dashboard",
        "Visual / Check",
        "Observation",
        "Result",
    ]
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
        if isinstance(row, dict) and not _is_excluded_visual_element(row)
    ]
    filter_rows = [
        row
        for row in visual_filter.get("filter_rows") or []
        if isinstance(row, dict)
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

    page_groups: dict[str, dict[str, Any]] = {}
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

    for group in page_groups.values():
        state = str(group["state"])
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
            filter_status, filter_observation = filter_selection_result(
                selected_a,
                selected_b,
            )
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
            name = str(comparison.get("element") or f"Unnamed {kind}").strip()
            layout = matching_layout(comparison, group)
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
            if visual.get("status") == UNVERIFIED:
                visual_status = UNVERIFIED
                observation = observation or (
                    "The visual could not be matched confidently from its "
                    "ID or title."
                )
            elif type_a.casefold() == "missing" or type_b.casefold() == "missing":
                visual_status = DIFFERENT
                observation = "Visual is present on only one dashboard."
            elif any(
                str(visual.get(f"rendering_status_{side}") or "").casefold()
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
            elif (
                not type_a
                or not type_b
                or {type_a.casefold(), type_b.casefold()}
                & {"unknown", "unidentified"}
                or visual.get("status") == UNVERIFIED
            ):
                visual_status = UNVERIFIED
                observation = observation or (
                    "Visual type or rendering could not be verified."
                )
            elif type_a.casefold() != type_b.casefold():
                visual_status = DIFFERENT
                observation = f"Visual type differs: Base {type_a}; Compare {type_b}."
            else:
                visual_status = MATCH
                observation = (
                    "Visual containers have the same type; screenshot styling "
                    "requires visual review."
                )
            if base_state.get("render_unverified") or compare_state.get(
                "render_unverified"
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
                str(visual.get("visual_name") or "Unnamed visual"),
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

    if rows_out is not None:
        rows_out.extend(
            summary_rows_by_section["Visual & Functional Validation Log"]
        )
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
            end_column=len(summary_headers),
        )
        sheet.cell(1, 1).font = Font(bold=True, color="FFFFFF", size=14)
        sheet.cell(1, 1).fill = PatternFill("solid", fgColor=HEADER_FILL)
        summary_validation = DataValidation(
            type="list",
            formula1='"Pass,Fail,Unable to Verify"',
            allow_blank=True,
        )
        sheet.add_data_validation(summary_validation)
        summary_id = 0
        summary_status_rows: list[int] = []
        for section in summary_sections:
            if (
                section == "Dashboard / Page Validation"
                and not summary_rows_by_section[section]
            ):
                continue
            section_title_row = sheet.max_row + 1
            sheet.cell(section_title_row, 1, section)
            sheet.merge_cells(
                start_row=section_title_row,
                start_column=1,
                end_row=section_title_row,
                end_column=len(summary_headers),
            )
            sheet.cell(section_title_row, 1).font = Font(
                bold=True, color="FFFFFF"
            )
            sheet.cell(section_title_row, 1).fill = PatternFill(
                "solid", fgColor=HEADER_FILL
            )
            summary_header_row = section_title_row + 1
            sheet.append(summary_headers)
            for cell in sheet[summary_header_row]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor=HEADER_FILL)
                cell.alignment = Alignment(wrap_text=True, vertical="center")

            for row in summary_rows_by_section[section]:
                if (
                    section == "Visual & Functional Validation Log"
                    and row["status"] == UNVERIFIED_LABEL
                ):
                    continue
                if str(row["visual_check"]).endswith("Position & Size"):
                    continue
                summary_id += 1
                sheet.append(
                    [
                        f"{summary_id:02d}",
                        row["page"],
                        row["visual_check"],
                        row["base"],
                        row["compare"],
                        row["observation"],
                        row["status"],
                    ]
                )
                row_index = sheet.max_row
                summary_status_rows.append(row_index)
                summary_validation.add(f"G{row_index}")
                style_status_cell(sheet.cell(row_index, 7), row["status"])
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
        result_counts = {
            label: (
                f'=COUNTIF(G{summary_status_rows[0]}:G'
                f'{summary_status_rows[-1]},"{label}")'
                if summary_status_rows
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

        technical_title_row = sheet.max_row + 2
        sheet.cell(
            technical_title_row, 1, "Visual Inventory & Screenshots"
        )
        sheet.merge_cells(
            start_row=technical_title_row,
            start_column=1,
            end_row=technical_title_row,
            end_column=len(headers),
        )
        sheet.cell(technical_title_row, 1).font = Font(
            bold=True, color="FFFFFF", size=14
        )
        sheet.cell(technical_title_row, 1).fill = PatternFill(
            "solid", fgColor=HEADER_FILL
        )
        technical_header_row = technical_title_row + 1
        sheet.append(headers)
        for cell in sheet[technical_header_row]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor=HEADER_FILL)
            cell.alignment = Alignment(wrap_text=True, vertical="center")

        validation = DataValidation(
            type="list",
            formula1='"Pass,Fail,Unable to Verify"',
            allow_blank=False,
        )
        sheet.add_data_validation(validation)

        for group in page_groups.values():
            state = str(group["state"])
            page = str(group["page"])
            base_state = evidence_for(base_evidence, state)
            compare_state = evidence_for(compare_evidence, state)
            base_path = screenshot_path(base_state)
            compare_path = screenshot_path(compare_state)
            rows = [
                row
                for row in group["visual_rows"]
                if row.get("status") != "INFO"
            ]
            if group["unmatched_side"]:
                side = group["unmatched_side"]
                rows = [
                    {
                        "visual_name": "Unmatched page/filter state",
                        "visual_type_a": "Page captured" if side == "A" else "Missing",
                        "visual_type_b": "Page captured" if side == "B" else "Missing",
                        "status": "DIFFERENT",
                        "comparison_notes": (
                            "No matching page/filter state was captured in the other dashboard"
                        ),
                    }
                ]
            elif not rows:
                rows = [
                    {
                        "visual_name": "No visuals detected",
                        "status": "DIFFERENT",
                        "comparison_notes": (
                            "No visual containers were detected for this page"
                        ),
                    }
                ]

            filter_mismatches = [
                row
                for row in filter_rows
                if str(row.get("state") or "").casefold() == state.casefold()
                and filter_selection_result(
                    row.get("selected_values_a"),
                    row.get("selected_values_b"),
                )[0]
                == DIFFERENT
            ]
            filter_mismatch = bool(filter_mismatches)
            for index, visual in enumerate(rows):
                title_issues = related_title_issues(visual, state)
                title_unverified = related_title_unverified(visual, state)
                notes = [str(visual.get("comparison_notes") or "").strip()]
                notes.extend(title_issues)
                if title_unverified:
                    notes.append("Related title or label could not be verified")
                for filter_row in filter_mismatches:
                    _, filter_note = filter_selection_result(
                        filter_row.get("selected_values_a"),
                        filter_row.get("selected_values_b"),
                    )
                    filter_name = str(
                        filter_row.get("filter_name") or "Filter"
                    ).strip()
                    notes.append(f"{filter_name}: {filter_note}")
                render_unverified = False
                for side, item in (
                    ("Base", base_state),
                    ("Compare", compare_state),
                ):
                    render_issue = item.get("render_unverified")
                    if render_issue:
                        render_unverified = True
                        reason = (
                            render_issue.get("reason")
                            if isinstance(render_issue, dict)
                            else None
                        )
                        notes.append(
                            f"{side} rendering is unverified"
                            + (f": {reason}" if reason else "")
                        )
                if base_path is None:
                    notes.append("Base dashboard screenshot is unavailable")
                if compare_path is None:
                    notes.append("Compare dashboard screenshot is unavailable")

                status = (
                    DIFFERENT
                    if visual.get("status") == DIFFERENT
                    or title_issues
                    or filter_mismatch
                    else UNVERIFIED
                    if visual.get("status") == UNVERIFIED
                    or title_unverified
                    or render_unverified
                    or base_path is None
                    or compare_path is None
                    else MATCH
                )
                if not any(notes):
                    notes = (
                        ["A visual difference was detected; details were not captured."]
                        if visual.get("status") == DIFFERENT
                        else ["Unable to verify; required visual evidence is incomplete."]
                        if visual.get("status") == UNVERIFIED
                        else ["Visual type and layout match."]
                    )
                label = _validation_label(status)
                sheet.append(
                    [
                        f"{sheet.max_row - technical_header_row:02d}",
                        page,
                        str(visual.get("visual_name") or "Unnamed visual"),
                        action_for(group),
                        expected_result,
                        str(visual.get("visual_type_a") or "-"),
                        str(visual.get("visual_type_b") or "-"),
                        str(visual.get("position_a") or "-"),
                        str(visual.get("position_b") or "-"),
                        str(visual.get("size_a") or "-"),
                        str(visual.get("size_b") or "-"),
                        "; ".join(note for note in notes if note),
                        label,
                        "Embedded below" if base_path else "Not captured",
                        "Embedded below" if compare_path else "Not captured",
                    ]
                )
                row_index = sheet.max_row
                validation.add(f"M{row_index}")
                style_status_cell(sheet.cell(row_index, 13), label)
                for cell in sheet[row_index]:
                    cell.alignment = Alignment(wrap_text=True, vertical="top")

                if index == 0:
                    image_heights = []
                    for column, screenshot in ((14, base_path), (15, compare_path)):
                        if screenshot is None:
                            continue
                        image = ExcelImage(str(screenshot))
                        scale = min(420 / image.width, 236 / image.height, 1)
                        image.width = int(image.width * scale)
                        image.height = int(image.height * scale)
                        image_heights.append(image.height)
                        sheet.add_image(
                            image,
                            f"{get_column_letter(column)}{row_index}",
                        )
                    if image_heights:
                        sheet.row_dimensions[row_index].height = (
                            max(image_heights) * 0.75 + 6
                        )

        for row_index in summary_status_rows:
            add_status_formatting(f"G{row_index}")
        if sheet.max_row > technical_header_row:
            add_status_formatting(
                    f"M{technical_header_row + 1}:M{sheet.max_row}"
            )

        for column, width in enumerate(
            (8, 22, 32, 45, 45, 58, 12, 16, 18, 14, 16, 54, 12, 58, 58),
            start=1,
        ):
            sheet.column_dimensions[get_column_letter(column)].width = width
        sheet.freeze_panes = "A3"
        sheet.auto_filter.ref = (
            f"A{technical_header_row}:O{sheet.max_row}"
        )
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
