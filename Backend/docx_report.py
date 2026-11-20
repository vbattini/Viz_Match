"""Build the comparison DOCX from the supplied report validation template."""

from __future__ import annotations

import logging
import re
import time
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

logger = logging.getLogger(__name__)

TEMPLATE_PATH = Path(__file__).parent / "assets" / "report_template.docx"
_PROJECT_COMPARISON_TEMPLATE = (
    Path(__file__).resolve().parents[2] / "Report Validation Template.docx"
)
COMPARISON_TEMPLATE_PATH = (
    _PROJECT_COMPARISON_TEMPLATE
    if _PROJECT_COMPARISON_TEMPLATE.is_file()
    else Path(__file__).parent / "assets" / "comparison_report_template.docx"
)
DEFAULT_TESTER_NAME = "Vijay Battini"
RESULT_STATUS_DROPDOWN_LABEL = "Result Status Dropdown (Pass/Fail)"
FILTER_EXPECTED_BEHAVIOR = (
    "Filters are applied and visuals are working as expected."
)
DOCX_FILTER_VALIDATION_HEADERS = [
    "ID",
    "Page / Tab Name",
    "Action / Filter Applied",
    "Expected Behavior",
    "Result (Pass/Fail/Unable to Verify)",
]
DOCX_VISUAL_VALIDATION_HEADERS = [
    "ID",
    "Page / Area",
    "Visual / Check",
    "Action / Filter Applied",
    "Expected Result",
    "Result",
]
NO_FILTER_SELECTIONS_TEXT = "No filter selections captured."
STATUS_FONT_SIZE_PT = 14
UNVERIFIED_FONT_SIZE_PT = 10
STATUS_TEXT_COLORS = {
    "pass": "008000",
    "fail": "C00000",
    "unable to verify": "ED7D31",
}

NAVY = "1F3864"
BORDER_GRAY = "BFBFBF"


def _save_document_atomic(doc: Any, path: Path) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        doc.save(str(temporary))
        for attempt in range(5):
            try:
                temporary.replace(path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                logger.warning(
                    "Temporary permission error replacing %s; retrying (%d/5)",
                    path,
                    attempt + 1,
                )
                time.sleep(0.1 * (2**attempt))
    finally:
        temporary.unlink(missing_ok=True)
RED_TEXT = {"DIFFERENT", "FAIL", "Fail", "Variance"}
MAX_CELL_CHARS = 500
SCREENSHOT_MAX_WIDTH_IN = 6.0
SCREENSHOT_MAX_HEIGHT_IN = 4.5


def write_docx_report(
    result: dict[str, Any],
    path: Path,
    dashboard_a: str,
    dashboard_b: str,
    *,
    tester_name: str = DEFAULT_TESTER_NAME,
    report_date: date | str | None = None,
    report_metadata: dict[str, Any] | None = None,
    filter_validation_log: tuple[list[str], list[list[Any]]] | None = None,
    visual_validation_log: tuple[list[str], list[list[Any]]] | None = None,
    visual_validation_states: list[str] | None = None,
) -> None:
    doc = Document(str(COMPARISON_TEMPLATE_PATH))
    if (
        filter_validation_log is None
        or visual_validation_log is None
        or visual_validation_states is None
    ):
        from .visual_filter_report import (
            FILTER_VALIDATION_HEADERS,
            VISUAL_VALIDATION_HEADERS,
            filter_validation_log_rows,
            visual_validation_log_data,
        )

    if filter_validation_log is None:
        filter_validation_log = (
            FILTER_VALIDATION_HEADERS,
            filter_validation_log_rows(result),
        )
    if visual_validation_log is None or visual_validation_states is None:
        generated_visual_rows, generated_visual_states = (
            visual_validation_log_data(result)
        )
    if visual_validation_log is None:
        visual_validation_log = (
            VISUAL_VALIDATION_HEADERS,
            generated_visual_rows,
        )
    if visual_validation_states is None:
        if visual_validation_log[1] != generated_visual_rows:
            raise ValueError(
                "Visual validation rows do not match their scenario states."
            )
        visual_validation_states = generated_visual_states

    summary = result.get("summary") or {}
    comparisons = [row for row in result.get("comparisons") or [] if isinstance(row, dict)]
    exported_rows = [
        row
        for row in result.get("exported_data_comparisons") or []
        if isinstance(row, dict)
    ]
    evidence_a = _evidence(result, "dashboard_a")
    evidence_b = _evidence(result, "dashboard_b")
    states = _ordered_states(result, comparisons, exported_rows, summary)
    a_only = set(summary.get("dashboard_a_only_states") or [])
    b_only = set(summary.get("dashboard_b_only_states") or [])
    overall = _overall_status(result)
    _populate_comparison_template(
        doc,
        result,
        dashboard_a,
        dashboard_b,
        tester_name,
        report_date,
        evidence_a,
        evidence_b,
        states,
        a_only,
        b_only,
        overall,
        report_metadata,
        filter_validation_log,
        visual_validation_log,
        visual_validation_states,
    )
    _replace_template_screenshots(
        doc,
        evidence_a,
        evidence_b,
        states,
        label_initial_default_state=False,
    )
    _save_document_atomic(doc, path)


def _populate_comparison_template(
    doc: Any,
    result: dict[str, Any],
    dashboard_a: str,
    dashboard_b: str,
    tester_name: str,
    report_date: date | str | None,
    evidence_a: dict[str, dict[str, Any]],
    evidence_b: dict[str, dict[str, Any]],
    states: list[str],
    a_only: set[str],
    b_only: set[str],
    overall: str,
    report_metadata: dict[str, Any] | None,
    filter_validation_log: tuple[list[str], list[list[Any]]],
    visual_validation_log: tuple[list[str], list[list[Any]]],
    visual_validation_states: list[str],
) -> None:
    metadata = report_metadata or {}
    inputs = metadata.get("inputs") or {}
    dashboard_a_metadata = metadata.get("dashboard_a") or {}
    dashboard_b_metadata = metadata.get("dashboard_b") or {}
    report_title = (
        inputs.get("report_name")
        or _side_value(evidence_a, "report_title")
        or dashboard_a
    )
    semantic_model = inputs.get("semantic_model") or ""
    workspace_a = dashboard_a_metadata.get("workspace_name") or ""
    workspace_b = dashboard_b_metadata.get("workspace_name") or ""
    summary = result.get("summary") or {}
    tables = doc.tables
    _set_cell_text(tables[0].cell(2, 0), "Snowflake Workspace Name")
    _set_cell_text(tables[0].cell(3, 0), "GCP Workspace Name")
    _set_cell_text(tables[0].cell(0, 1), report_title)
    _set_cell_text(tables[0].cell(1, 1), semantic_model)
    _set_cell_text(tables[0].cell(2, 1), workspace_a)
    _set_cell_text(tables[0].cell(3, 1), workspace_b)
    _set_dropdown_cell_text(
        tables[0].cell(4, 1),
        tester_name,
        additional_options=("Battini Vijay",),
    )
    _set_cell_text(tables[0].cell(5, 1), _format_date(report_date))
    def apply_overall(status: str) -> None:
        overall_cell = tables[0].cell(6, 1)
        if overall_cell._tc.xpath(".//w:sdtPr/w:dropDownList"):
            _set_dropdown_cell_text(
                overall_cell,
                _docx_status_label(status),
                replace_options=("Pass", "Fail"),
            )
        else:
            _set_cell_text(overall_cell, status)
        _style_status_cell(overall_cell, status)

    apply_overall(overall)

    filter_headers, source_filter_rows = filter_validation_log
    filter_rows = _docx_filter_validation_rows(
        filter_headers,
        source_filter_rows,
        (result.get("visual_filter") or {}).get("filter_rows") or [],
    )
    _replace_docx_log_table(
        doc,
        tables[1],
        DOCX_FILTER_VALIDATION_HEADERS,
        filter_rows,
        column_weights=[0.4, 0.9, 1.7, 2.45, 0.95],
        status_column=4,
    )

    visual_headers, source_visual_rows = visual_validation_log
    visual_rows = _docx_visual_validation_rows(
        visual_headers,
        source_visual_rows,
        visual_validation_states,
        (result.get("visual_filter") or {}).get("filter_rows") or [],
        (result.get("visual_filter") or {}).get("visual_rows") or [],
    )
    _replace_docx_log_table(
        doc,
        tables[2],
        DOCX_VISUAL_VALIDATION_HEADERS,
        visual_rows,
        column_weights=[0.35, 0.8, 1.0, 1.55, 2.0, 0.6],
        status_column=5,
    )
    shown_statuses = [
        _docx_status_label(str(row[4] or "")) for row in filter_rows
    ] + [
        _docx_status_label(str(row[5] or "")) for row in visual_rows
    ]
    overall = (
        "PASS"
        if shown_statuses and all(status != "Fail" for status in shown_statuses)
        else "FAIL"
    )
    apply_overall(overall)

    _set_cell_text(tables[3].cell(0, 0), RESULT_STATUS_DROPDOWN_LABEL)
    _set_dropdown_control(
        tables[3].cell(0, 1),
        _docx_status_label(overall),
        title=RESULT_STATUS_DROPDOWN_LABEL,
        options=("Pass", "Fail"),
    )
    _style_status_cell(tables[3].cell(0, 1), overall)
    _set_dropdown_cell_text(
        tables[3].cell(1, 1),
        tester_name,
        additional_options=("Battini Vijay",),
    )
    _set_cell_text(tables[3].cell(2, 1), _format_date(report_date))

    _replace_paragraph(
        doc,
        "Snowflake vs. BigQuery Power BI Report Validation",
        "Snowflake vs. GCP Power BI Report Validation",
    )
    _replace_paragraph(doc, "Report Name", report_title)
    _replace_paragraph(
        doc,
        "Comments: Data matched accurately between Snowflake and GCP, with no data discrepancies identified during the validation process.",
        (
            "Comments: "
            f"{summary.get('differences', 0)} comparison differences and "
            f"{summary.get('exported_data_differences', 0)} exported-data differences; "
            f"overall result: {overall}."
        ),
    )


def _set_cell_text(cell: Any, text: str) -> None:
    paragraph = cell.paragraphs[0]
    runs = paragraph.runs
    if runs:
        runs[0].text = text
        for run in runs[1:]:
            run._element.getparent().remove(run._element)
    else:
        paragraph.add_run(text)
    for extra_paragraph in cell.paragraphs[1:]:
        extra_paragraph._element.getparent().remove(extra_paragraph._element)


def _set_dropdown_cell_text(
    cell: Any,
    text: str,
    *,
    additional_options: tuple[str, ...] = (),
    replace_options: tuple[str, ...] | None = None,
) -> None:
    for control in cell._tc.xpath(".//w:sdt"):
        properties = control.find(qn("w:sdtPr"))
        dropdown = (
            properties.find(qn("w:dropDownList"))
            if properties is not None
            else None
        )
        if dropdown is None:
            continue
        if replace_options is not None:
            for item in dropdown.findall(qn("w:listItem")):
                dropdown.remove(item)
        items = dropdown.findall(qn("w:listItem"))
        available_values = {
            item.get(qn("w:value")) or item.get(qn("w:displayText"))
            for item in items
        }
        options = replace_options or ()
        for option in (*options, *additional_options, text):
            if option in available_values:
                continue
            item = OxmlElement("w:listItem")
            item.set(qn("w:displayText"), option)
            item.set(qn("w:value"), option)
            dropdown.append(item)
            available_values.add(option)
        dropdown.set(qn("w:lastValue"), text)
        content = control.find(qn("w:sdtContent"))
        if content is None:
            continue
        text_nodes = list(content.iter(qn("w:t")))
        if text_nodes:
            text_nodes[0].text = text
            for extra_text in text_nodes[1:]:
                extra_text.getparent().remove(extra_text)
        else:
            run = OxmlElement("w:r")
            text_node = OxmlElement("w:t")
            text_node.text = text
            run.append(text_node)
            content.append(run)
        return
    _set_cell_text(cell, text)


def _set_dropdown_control(
    cell: Any,
    text: str,
    *,
    title: str,
    options: tuple[str, ...],
) -> None:
    for control in cell._tc.xpath(".//w:sdt"):
        control.getparent().remove(control)
    paragraph = cell.paragraphs[0]
    for child in list(paragraph._p):
        if child.tag != qn("w:pPr"):
            paragraph._p.remove(child)
    for extra_paragraph in cell.paragraphs[1:]:
        extra_paragraph._element.getparent().remove(extra_paragraph._element)

    control = OxmlElement("w:sdt")
    properties = OxmlElement("w:sdtPr")
    alias = OxmlElement("w:alias")
    alias.set(qn("w:val"), title)
    properties.append(alias)
    tag = OxmlElement("w:tag")
    tag.set(qn("w:val"), "result-status-dropdown")
    properties.append(tag)
    dropdown = OxmlElement("w:dropDownList")
    dropdown.set(qn("w:lastValue"), text)
    for option in options:
        item = OxmlElement("w:listItem")
        item.set(qn("w:displayText"), option)
        item.set(qn("w:value"), option)
        dropdown.append(item)
    properties.append(dropdown)
    content = OxmlElement("w:sdtContent")
    run = OxmlElement("w:r")
    text_node = OxmlElement("w:t")
    text_node.text = text
    run.append(text_node)
    content.append(run)
    control.append(properties)
    control.append(content)
    paragraph._p.append(control)


def _replace_docx_log_table(
    doc: Any,
    anchor_table: Any,
    headers: list[str],
    rows: list[list[Any]],
    *,
    column_weights: list[float],
    status_column: int,
    font_size: float = 7.5,
) -> Any:
    if len(headers) != len(column_weights) or any(
        len(row) != len(headers) for row in rows
    ):
        raise ValueError("DOCX log rows must match their declared columns.")

    section = doc.sections[-1]
    available_width = (
        section.page_width - section.left_margin - section.right_margin
    ) / 914400
    weight_total = sum(column_weights)
    widths = [
        available_width * weight / weight_total for weight in column_weights
    ]

    table = doc.add_table(rows=1 + len(rows), cols=len(headers))
    table.autofit = False
    _set_borders(table)
    for column, width in enumerate(widths):
        table.columns[column].width = Inches(width)

    for row_index, values in enumerate([headers, *rows]):
        table_row = table.rows[row_index]
        row_properties = table_row._tr.get_or_add_trPr()
        if row_index == 0:
            row_properties.append(OxmlElement("w:tblHeader"))
        else:
            row_properties.append(OxmlElement("w:cantSplit"))
        for column, value in enumerate(values):
            cell = table_row.cells[column]
            cell.width = Inches(widths[column])
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
            if row_index == 0:
                _shade(cell, NAVY)
            _set_cell_text(cell, "" if value is None else str(value))
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.space_before = Pt(0)
                paragraph.paragraph_format.space_after = Pt(0)
                for run in paragraph.runs:
                    run.font.size = Pt(font_size)
                    if row_index == 0:
                        run.bold = True
                        run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
                    elif column == status_column:
                        run.font.color.rgb = RGBColor.from_string(
                            _status_text_color(value)
                        )

    old_table = anchor_table._tbl
    new_table = table._tbl
    old_table.addprevious(new_table)
    old_table.getparent().remove(old_table)
    return table


def _docx_filter_validation_rows(
    source_headers: list[str],
    source_rows: list[list[Any]],
    structured_rows: list[dict[str, Any]] | None = None,
) -> list[list[str]]:
    source_columns = {
        str(header): index for index, header in enumerate(source_headers)
    }
    required_headers = (
        "ID",
        "Page / Area",
        "Compare Dashboard",
        "Base Dashboard",
        "Visual / Check",
        "Result",
    )
    if any(header not in source_columns for header in required_headers):
        raise ValueError(
            "Source Filter Validation Log is missing a required column."
        )

    def value(row: list[Any], column: str) -> str:
        return "" if row[source_columns[column]] is None else str(
            row[source_columns[column]]
        )

    grouped: list[dict[str, Any]] = []

    def flush_group(group: dict[str, Any] | None) -> None:
        if group is None:
            return
        grouped.append(group)

    if structured_rows and all(isinstance(row, dict) for row in structured_rows):
        groups_by_state: dict[tuple[str, str], dict[str, Any]] = {}
        for row in structured_rows:
            state = str(row.get("state") or "").strip()
            page = str(row.get("page") or "").strip()
            if not page and state:
                page = state.split("|", 1)[0].strip()
            filter_name = str(row.get("filter_name") or "").strip()
            if not page or not filter_name:
                continue
            group = groups_by_state.setdefault(
                (page, state),
                {
                    "page": page,
                    "filter_names": set(),
                    "filters": [],
                    "statuses": [],
                },
            )
            group["filter_names"].add(filter_name.casefold())
            group["filters"].append(
                {
                    "name": filter_name,
                    "base": str(row.get("selected_values_a") or "").strip()
                    or "Not captured",
                    "compare": str(row.get("selected_values_b") or "").strip()
                    or "Not captured",
                    "observation": str(row.get("comparison_notes") or "").strip(),
                }
            )
            group["statuses"].append(str(row.get("status") or ""))
        grouped.extend(groups_by_state.values())
    else:
        current: dict[str, Any] | None = None
        for row in source_rows:
            if len(row) != len(source_headers):
                raise ValueError(
                    "Source Filter Validation Log row does not match its headers."
                )
            page = value(row, "Page / Area")
            filter_name = value(row, "Compare Dashboard")
            base_selection = value(row, "Base Dashboard") or "Not captured"
            compare_selection = value(row, "Visual / Check") or "Not captured"
            normalized_name = " ".join(filter_name.split()).casefold()
            if (
                current is None
                or current["page"] != page
                or normalized_name in current["filter_names"]
            ):
                flush_group(current)
                current = {
                    "page": page,
                    "filter_names": set(),
                    "filters": [],
                    "statuses": [],
                }
            current["filter_names"].add(normalized_name)
            current["filters"].append(
                {
                    "name": filter_name,
                    "base": base_selection,
                    "compare": compare_selection,
                    "observation": value(row, "Observation"),
                }
            )
            current["statuses"].append(value(row, "Result"))
        flush_group(current)

    result: list[list[str]] = []
    for index, group in enumerate(grouped, start=1):
        actions: list[str] = []
        failure_details: list[str] = []
        for item in group["filters"]:
            base_selection = item["base"]
            compare_selection = item["compare"]
            if " ".join(base_selection.split()).casefold() == " ".join(
                compare_selection.split()
            ).casefold():
                action = f"{item['name']}: {base_selection}"
            else:
                action = (
                    f"{item['name']}: Base: {base_selection}; "
                    f"Compare: {compare_selection}"
                )
            actions.append(f"{action};")
            observation = " ".join(item["observation"].split())
            if observation and observation != "-":
                failure_details.append(f"{item['name']}: {observation}")

        statuses = {
            _docx_status_label(str(status or ""))
            for status in group["statuses"]
        }
        result_status = (
            "Fail"
            if "Fail" in statuses
            else "Unable to Verify"
            if "Unable to Verify" in statuses
            else "Pass"
        )
        if result_status != "Pass" and failure_details:
            actions.append("Validation detail: " + " ".join(failure_details))
        result.append(
            [
                f"{index:02d}",
                group["page"],
                "\n".join(actions),
                FILTER_EXPECTED_BEHAVIOR,
                result_status,
            ]
        )
    return result


def _docx_visual_validation_rows(
    source_headers: list[str],
    source_rows: list[list[Any]],
    states: list[str],
    filter_rows: list[dict[str, Any]],
    visual_metadata: list[dict[str, Any]],
) -> list[list[str]]:
    source_columns = {
        str(header): index for index, header in enumerate(source_headers)
    }
    required_headers = (
        "ID",
        "Page / Area",
        "Compare Dashboard",
        "Base Dashboard",
        "Visual Name",
        "Visual / Check",
        "Observation",
        "Result",
    )
    if any(header not in source_columns for header in required_headers):
        raise ValueError(
            "Source Visual & Functional Validation Log is missing a required column."
        )
    if len(states) != len(source_rows):
        raise ValueError(
            "Visual validation rows must have one matching scenario state each."
        )

    def value(row: list[Any], column: str) -> str:
        return "" if row[source_columns[column]] is None else str(
            row[source_columns[column]]
        )

    result: list[list[str]] = []
    for row, state in zip(source_rows, states):
        if len(row) != len(source_headers):
            raise ValueError(
                "Source Visual & Functional Validation Log row does not match its headers."
            )
        compare_raw = value(row, "Compare Dashboard")
        base_raw = value(row, "Base Dashboard")
        visual_name_raw = value(row, "Visual Name")
        check_raw = value(row, "Visual / Check")
        visual_check = _docx_visual_check(
            visual_name_raw,
            check_raw,
            compare_raw,
            base_raw,
            state,
            visual_metadata,
        )
        status = value(row, "Result")
        expected_result = _docx_expected_result(
            value(row, "Observation")
        )
        result.append(
            [
                value(row, "ID"),
                value(row, "Page / Area"),
                visual_check,
                _docx_visual_action(state, filter_rows, visual_metadata),
                expected_result,
                status,
            ]
        )
    return result


def _docx_expected_result(observation: str) -> str:
    """Keep the validation expectation but omit the appended filter context."""
    normalized = " ".join(observation.split()).strip()
    parts = re.split(r"\s+Filter\s*:", normalized, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) == 1:
        return normalized
    expectation = parts[0].strip()
    return expectation.rstrip(" .") + "." if expectation else ""


def _docx_visual_check(
    visual_name: str,
    check: str,
    compare: str,
    base: str,
    state: str,
    visual_metadata: list[dict[str, Any]],
) -> str:
    normalized_name = " ".join(visual_name.split())
    lowered_name = normalized_name.casefold()
    if (
        normalized_name
        and lowered_name not in {"-", "untitled visual", "not available"}
        and not lowered_name.startswith(
            ("table_headers", "visual_id", "table_header", "table_visual")
        )
        and not (
            "_" in normalized_name
            and any(character.isdigit() for character in normalized_name)
        )
        and ":" not in normalized_name
        and len(normalized_name) <= 64
    ):
        return normalized_name

    state_key = state.casefold()
    for row in visual_metadata:
        if not isinstance(row, dict) or str(row.get("state") or "").casefold() != state_key:
            continue
        for key in ("visual_type_a", "visual_type_b"):
            visual_type = _simple_visual_type(str(row.get(key) or ""))
            if visual_type:
                return visual_type

    for candidate in (visual_name, check, compare, base):
        visual_type = _simple_visual_type(candidate)
        if visual_type:
            return visual_type
    return "Visual"


def _simple_visual_type(value: str) -> str:
    normalized = " ".join(value.split()).casefold()
    if not normalized or normalized in {"-", "not available", "not captured"}:
        return ""
    if normalized.startswith("blank "):
        if "table" in normalized or "matrix" in normalized:
            return "Blank Table"
        if "kpi" in normalized or "card" in normalized:
            return "Blank KPI"
        return "Blank Chart"
    for token, label in (
        ("text box", "Text Box"),
        ("matrix", "Matrix"),
        ("table", "Table"),
        ("header", "Header"),
        ("slicer", "Slicer"),
        ("dropdown", "Slicer"),
        ("chart", "Chart"),
        ("kpi", "KPI"),
        ("image", "Image"),
        ("logo", "Image"),
        ("button", "Button"),
        ("card", "Card"),
    ):
        if token in normalized:
            return label
    return ""


def _docx_visual_action(
    state: str,
    filter_rows: list[dict[str, Any]],
    visual_metadata: list[dict[str, Any]],
) -> str:
    def rows_for_state(candidate_state: str) -> list[dict[str, Any]]:
        return [
            row
            for row in filter_rows
            if isinstance(row, dict)
            and str(row.get("state") or "").casefold()
            == candidate_state.casefold()
        ]

    matching_rows = rows_for_state(state)
    if not matching_rows and "|scenario_01|" in state.casefold():
        page_name = state.split("|", 1)[0]
        matching_rows = rows_for_state(f"{page_name}|initial")

    if matching_rows:
        matching_rows.sort(
            key=lambda row: (
                row.get("aligned_position")
                if isinstance(row.get("aligned_position"), (int, float))
                else float("inf")
            )
        )
        actions = []
        for row in matching_rows:
            name = " ".join(str(row.get("filter_name") or "").split())
            base_value = " ".join(
                str(row.get("selected_values_a") or "Not captured").split()
            )
            compare_value = " ".join(
                str(row.get("selected_values_b") or "Not captured").split()
            )
            if base_value.casefold() == compare_value.casefold():
                actions.append(f"{name}: {base_value}" if name else base_value)
            else:
                actions.append(
                    f"{name}: Base: {base_value}; Compare: {compare_value}"
                    if name
                    else f"Base: {base_value}; Compare: {compare_value}"
                )
        return "\n".join(
            f"{action.rstrip(';')};"
            for action in actions
            if action.strip()
        )

    state_key = state.casefold()
    applied_actions = []
    default_action_found = False
    for row in visual_metadata:
        if not isinstance(row, dict) or str(row.get("state") or "").casefold() != state_key:
            continue
        action = " ".join(str(row.get("action_applied") or "").split())
        if action.casefold() == "none (default view)":
            default_action_found = True
        elif action:
            applied_actions.append(action)
    if applied_actions:
        return "\n".join(
            f"{action.rstrip(';')};"
            for action in dict.fromkeys(applied_actions)
            if action.strip()
        )
    if default_action_found:
        return "Default filter state"
    return "Not captured"





def _replace_paragraph(doc: Any, old_text: str, new_text: str) -> None:
    for paragraph in doc.paragraphs:
        if paragraph.text == old_text:
            runs = paragraph.runs
            if runs:
                runs[0].text = new_text
                for run in runs[1:]:
                    run._element.getparent().remove(run._element)
            else:
                paragraph.add_run(new_text)
            return


def _clear_body(doc: Any) -> None:
    body = doc.element.body
    for child in list(body):
        if child.tag != qn("w:sectPr"):
            body.remove(child)


def _add_title(doc: Any, report_title: str) -> None:
    title = doc.add_paragraph()
    run = title.add_run(report_title)
    run.bold = True
    run.font.size = Pt(17)
    run.font.color.rgb = RGBColor.from_string(NAVY)
    subtitle = doc.add_paragraph()
    sub_run = subtitle.add_run("Power BI Report Validation")
    sub_run.font.size = Pt(12)
    sub_run.font.color.rgb = RGBColor.from_string("595959")


def _add_heading(doc: Any, text: str, level: int) -> None:
    doc.add_paragraph(text, style=f"Heading {level}")


def _add_table(
    doc: Any,
    headers: list[str],
    rows: list[Any],
    widths: list[float],
    *,
    font_size: float = 9,
) -> Any:
    normalized = [
        (list(item[0]), item[1]) if isinstance(item, tuple) else (list(item), None)
        for item in rows
    ]
    table = doc.add_table(rows=1 + len(normalized), cols=len(headers))
    table.autofit = False
    _set_borders(table)
    for row_index, values in enumerate([headers] + [cells for cells, _ in normalized]):
        table_row = table.rows[row_index]
        is_header = row_index == 0
        fill = NAVY if is_header else normalized[row_index - 1][1]
        if is_header:
            trPr = table_row._tr.get_or_add_trPr()
            trPr.append(OxmlElement("w:tblHeader"))
        for col_index, text in enumerate(values):
            cell = table_row.cells[col_index]
            cell.width = Inches(widths[col_index])
            if fill:
                _shade(cell, fill)
            paragraph = cell.paragraphs[0]
            run = paragraph.add_run(_text(text, limit=MAX_CELL_CHARS))
            run.font.size = Pt(font_size)
            if is_header:
                run.bold = True
                run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
            elif _text(text) in RED_TEXT:
                run.bold = True
                run.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)
    doc.add_paragraph()
    return table


def _set_borders(table: Any) -> None:
    tbl_pr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        element = OxmlElement(f"w:{edge}")
        element.set(qn("w:val"), "single")
        element.set(qn("w:sz"), "4")
        element.set(qn("w:space"), "0")
        element.set(qn("w:color"), BORDER_GRAY)
        borders.append(element)
    successor = next(
        (
            child
            for child in tbl_pr
            if child.tag
            in (qn("w:shd"), qn("w:tblLayout"), qn("w:tblCellMar"), qn("w:tblLook"))
        ),
        None,
    )
    if successor is not None:
        successor.addprevious(borders)
    else:
        tbl_pr.append(borders)


def _shade(cell: Any, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    for existing in tc_pr.findall(qn("w:shd")):
        tc_pr.remove(existing)
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


def _status_text_color(status: Any) -> str:
    normalized = str(status).strip().casefold()
    if normalized.startswith("pass"):
        return STATUS_TEXT_COLORS["pass"]
    if normalized.startswith("fail"):
        return STATUS_TEXT_COLORS["fail"]
    return STATUS_TEXT_COLORS["unable to verify"]


def _style_status_cell(cell: Any, status: Any) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    for shd in tc_pr.findall(qn("w:shd")):
        tc_pr.remove(shd)

    color = _status_text_color(status)
    font_size_pt = (
        UNVERIFIED_FONT_SIZE_PT
        if color == STATUS_TEXT_COLORS["unable to verify"]
        else STATUS_FONT_SIZE_PT
    )
    for run in cell._tc.xpath(".//w:r"):
        run_properties = run.find(qn("w:rPr"))
        if run_properties is None:
            run_properties = OxmlElement("w:rPr")
            run.insert(0, run_properties)
        for tag_name in (
            "w:b",
            "w:bCs",
            "w:color",
            "w:sz",
            "w:szCs",
            "w:highlight",
            "w:shd",
            "w:i",
            "w:iCs",
            "w:rStyle",
        ):
            for existing in run_properties.findall(qn(tag_name)):
                run_properties.remove(existing)
        for tag_name in ("w:i", "w:iCs"):
            italic_off = OxmlElement(tag_name)
            italic_off.set(qn("w:val"), "0")
            run_properties.append(italic_off)
        for tag_name in ("w:b", "w:bCs"):
            bold = OxmlElement(tag_name)
            run_properties.append(bold)
        color_element = OxmlElement("w:color")
        color_element.set(qn("w:val"), color)
        run_properties.append(color_element)
        for tag_name in ("w:sz", "w:szCs"):
            size = OxmlElement(tag_name)
            size.set(qn("w:val"), str(font_size_pt * 2))
            run_properties.append(size)


def _insert_screenshot(
    paragraph: Any,
    evidence: dict[str, Any] | None,
    side_label: str,
    *,
    max_width_in: float = SCREENSHOT_MAX_WIDTH_IN,
) -> None:
    path = Path(str((evidence or {}).get("path") or ""))
    if evidence and path.name and path.is_file():
        width = max_width_in
        image_width = evidence.get("image_width")
        image_height = evidence.get("image_height")
        if image_width and image_height:
            height_in = width * float(image_height) / float(image_width)
            if height_in > SCREENSHOT_MAX_HEIGHT_IN:
                width = SCREENSHOT_MAX_HEIGHT_IN * float(image_width) / float(image_height)
        paragraph.add_run().add_picture(str(path), width=Inches(width))


def _replace_template_screenshots(
    doc: Any,
    evidence_a: dict[str, dict[str, Any]],
    evidence_b: dict[str, dict[str, Any]],
    states: list[str],
    *,
    label_initial_default_state: bool = False,
) -> None:
    initial_state_heading = (
        "Scenario 01 — Initial Default State: Page-by-Page Screenshots"
        if label_initial_default_state
        else "2.1 Page-by-Page Screenshots (Snowflake vs. GCP)"
    )
    sections = (
        (
            "2.1 Snowflake",
            "2.2 GCP",
            initial_state_heading,
            "3. Visual & Functional Validation Log",
        ),
        (
            "3.1 Snowflake",
            "3.2 GCP",
            initial_state_heading,
            "4. Summary & Sign-off",
        ),
    )
    page_pairs = _page_screenshot_pairs(states, evidence_a, evidence_b)

    for heading, other_heading, gallery_heading, next_heading in sections:
        anchor = _screenshot_anchor(doc, heading, other_heading)
        if anchor is None:
            continue
        heading_paragraph = next(
            paragraph
            for paragraph in doc.paragraphs
            if paragraph.text.strip() == heading
        )
        heading_paragraph.text = gallery_heading
        _insert_page_screenshot_pairs(doc, anchor, page_pairs)
        anchor._p.getparent().remove(anchor._p)
        _remove_screenshot_subsection(doc, other_heading, next_heading)


def _insert_page_screenshot_pairs(
    doc: Any,
    anchor: Any,
    page_pairs: list[
        tuple[str, dict[str, Any] | None, dict[str, Any] | None]
    ],
) -> None:
    if not page_pairs:
        return

    for page, screenshot_a, screenshot_b in page_pairs:
        available = tuple(
            (path := Path(str((screenshot or {}).get("path") or ""))).name
            and path.is_file()
            for screenshot in (screenshot_a, screenshot_b)
        )
        if not any(available):
            continue
        for screenshot, label, screenshot_available in (
            (screenshot_a, "Snowflake", available[0]),
            (screenshot_b, "GCP", available[1]),
        ):
            if not screenshot_available:
                continue
            screenshot_path = Path(str((screenshot or {}).get("path") or ""))
            caption = anchor.insert_paragraph_before()
            caption.add_run(f"{page} – ({screenshot_path.name})").bold = True
            caption.paragraph_format.keep_with_next = True
            side_heading = anchor.insert_paragraph_before()
            side_heading.add_run(label).bold = True
            side_heading.paragraph_format.keep_with_next = True
            image_paragraph = anchor.insert_paragraph_before()
            _insert_screenshot(
                image_paragraph,
                screenshot,
                label,
                max_width_in=SCREENSHOT_MAX_WIDTH_IN,
            )


def _page_screenshot_pairs(
    states: list[str],
    evidence_a: dict[str, dict[str, Any]],
    evidence_b: dict[str, dict[str, Any]],
) -> list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]]:
    ordered_states = list(dict.fromkeys([*evidence_a, *evidence_b, *states]))

    def ordering(state: str) -> tuple[str, int, str]:
        page, suffix = _split_state(state)
        if suffix.casefold() == "default state":
            scenario_order = 0
        else:
            scenario_id = suffix.partition("|")[0].removeprefix("scenario_")
            try:
                scenario_order = 5 if scenario_id == "01" else int(scenario_id)
            except ValueError:
                scenario_order = 99
        return page.casefold(), scenario_order, state.casefold()

    ordered_states.sort(key=ordering)
    return [
        (
            _screenshot_state_label(state),
            evidence_a.get(state),
            evidence_b.get(state),
        )
        for state in ordered_states
    ]


def _screenshot_state_label(state: str) -> str:
    page, _suffix = _split_state(state)
    return page


def _remove_screenshot_subsection(
    doc: Any,
    heading: str,
    next_heading: str,
) -> None:
    paragraphs = doc.paragraphs
    heading_index = next(
        (
            index
            for index, paragraph in enumerate(paragraphs)
            if paragraph.text.strip() == heading
        ),
        None,
    )
    if heading_index is None:
        return
    anchor = _screenshot_anchor(doc, heading, next_heading)
    if anchor is None:
        return
    anchor_index = next(
        index
        for index, paragraph in enumerate(paragraphs)
        if paragraph._p is anchor._p
    )
    for paragraph in paragraphs[heading_index : anchor_index + 1]:
        paragraph._p.getparent().remove(paragraph._p)


def _screenshot_anchor(doc: Any, heading: str, next_heading: str) -> Any | None:
    paragraphs = doc.paragraphs
    heading_index = next(
        (
            index
            for index, paragraph in enumerate(paragraphs)
            if paragraph.text.strip() == heading
        ),
        None,
    )
    if heading_index is None:
        return None
    for paragraph in paragraphs[heading_index + 1 :]:
        if paragraph.text.strip() == next_heading:
            break
        if paragraph._p.xpath(".//w:drawing"):
            return paragraph
    return None


def _evidence(result: dict[str, Any], side: str) -> dict[str, dict[str, Any]]:
    screenshots = result.get("screenshot_evidence") or {}
    value = screenshots.get(side) or {}
    return {str(key): item for key, item in value.items() if isinstance(item, dict)}


def _side_value(evidence: dict[str, dict[str, Any]], key: str) -> str:
    for item in evidence.values():
        if item.get(key):
            return str(item[key])
    return ""


def _capture_summary(evidence: dict[str, dict[str, Any]], states: list[str]) -> str:
    captured = sum(
        1 for state in states if (evidence.get(state) or {}).get("capture_status") == "captured"
    )
    return f"{captured} of {len(states)} states"


def _capture_label(evidence: dict[str, dict[str, Any]], state: str) -> str:
    status = (evidence.get(state) or {}).get("capture_status")
    if not status:
        return "Missing"
    return "Captured" if status == "captured" else str(status).title()


def _ordered_states(
    result: dict[str, Any],
    comparisons: list[dict[str, Any]],
    exported_rows: list[dict[str, Any]],
    summary: dict[str, Any],
) -> list[str]:
    states: set[str] = set()
    for side in ("dashboard_a", "dashboard_b"):
        states.update(_evidence(result, side).keys())
    states.update(str(row["state"]) for row in comparisons if row.get("state"))
    states.update(str(row["state"]) for row in exported_rows if row.get("state"))
    states.update(str(item) for item in summary.get("dashboard_a_only_states") or [])
    states.update(str(item) for item in summary.get("dashboard_b_only_states") or [])
    return sorted(state for state in states if state)


def _split_state(state: str) -> tuple[str, str]:
    page, _, filter_part = state.partition("|")
    filter_part = filter_part.strip()
    if not filter_part or filter_part.casefold() == "initial":
        filter_part = "Default state"
    elif filter_part.casefold().startswith("scenario_"):
        scenario_part, separator, pairing = filter_part.partition("|")
        filter_part = f"{scenario_part}|{pairing}" if separator else scenario_part
    return page.strip() or "Unknown page", filter_part


def _state_result(
    state: str,
    rows: list[dict[str, Any]],
    a_only: set[str],
    b_only: set[str],
) -> str:
    if state in a_only or state in b_only:
        return "FAIL"
    statuses = {row.get("status") for row in rows}
    return "PASS" if statuses and all(status == "MATCH" for status in statuses) else "FAIL"


def _filter_action_summary(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return NO_FILTER_SELECTIONS_TEXT
    actions = []
    for row in rows:
        name = str(row.get("filter_name") or "").strip()
        if not name:
            continue
        value_a = str(row.get("selected_values_a") or "").strip()
        value_b = str(row.get("selected_values_b") or "").strip()
        value_a = value_a if value_a not in {"", "-"} else "Not captured"
        value_b = value_b if value_b not in {"", "-"} else "Not captured"
        if " ".join(value_a.split()).casefold() == " ".join(
            value_b.split()
        ).casefold():
            action = f"{name}: {value_a}"
        else:
            action = f"{name}: Snowflake: {value_a}; GCP: {value_b}"
        actions.append(f"{action};")
    return "\n".join(actions) or NO_FILTER_SELECTIONS_TEXT


def _overall_status(result: dict[str, Any]) -> str:
    summary = result.get("summary") or {}
    no_missing = not summary.get("dashboard_a_only_states") and not summary.get(
        "dashboard_b_only_states"
    )
    status_rows = []
    for key in (
        "comparisons",
        "title_comparisons",
        "metadata_comparisons",
        "export_option_comparisons",
        "exported_data_comparisons",
    ):
        status_rows.extend(
            row
            for row in result.get(key, [])
            if isinstance(row, dict)
        )
    visual_filter = result.get("visual_filter") or {}
    for key in ("visual_rows", "filter_rows"):
        status_rows.extend(
            row
            for row in visual_filter.get(key, [])
            if isinstance(row, dict)
        )
    if visual_filter.get("unmatched_states"):
        no_missing = False
    statuses = [row.get("status") for row in status_rows]
    return (
        "PASS"
        if statuses and all(status == "MATCH" for status in statuses) and no_missing
        else "FAIL"
    )


def _refresh_status(status: Any) -> str:
    return _status_label(status)


def _status_label(status: Any) -> str:
    return "PASS" if status == "MATCH" else "FAIL"


def _docx_status_label(status: str) -> str:
    normalized = str(status).strip().upper()
    if normalized in {"PASS", "MATCH"}:
        return "Pass"
    if normalized in {"UNVERIFIED", "UNABLE TO VERIFY"}:
        return "Unable to Verify"
    return "Fail"


def _format_date(report_date: date | str | None) -> str:
    if report_date is None:
        report_date = date.today()
    if isinstance(report_date, date):
        return report_date.strftime("%m/%d/%Y")
    return str(report_date)


def _text(value: Any, limit: int | None = None) -> str:
    text = "" if value is None else str(value)
    if limit is not None and len(text) > limit:
        return text[: limit - 1] + "…"
    return text
