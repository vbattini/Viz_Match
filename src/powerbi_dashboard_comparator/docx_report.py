"""Build the comparison DOCX from the supplied report validation template."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from .visual_filter_comparison import (
    DIFFERENT,
    MATCH,
    UNVERIFIED,
    filter_selection_result,
)

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
STATUS_FONT_SIZE_PT = 14
STATUS_TEXT_COLORS = {
    "pass": "008000",
    "fail": "C00000",
    "unable to verify": "666666",
}

NAVY = "1F3864"
BORDER_GRAY = "BFBFBF"
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
) -> None:
    doc = Document(str(COMPARISON_TEMPLATE_PATH))

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
    rows_by_state = _group_by_state(comparisons)
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
        rows_by_state,
        a_only,
        b_only,
        overall,
    )
    _replace_template_screenshots(doc, evidence_a, evidence_b, states)
    doc.save(str(path))


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
    rows_by_state: dict[str, list[dict[str, Any]]],
    a_only: set[str],
    b_only: set[str],
    overall: str,
) -> None:
    report_title = _side_value(evidence_a, "report_title") or dashboard_a
    report_title_b = _side_value(evidence_b, "report_title") or dashboard_b
    summary = result.get("summary") or {}
    tables = doc.tables
    _set_cell_text(tables[0].cell(0, 1), report_title)
    _set_cell_text(tables[0].cell(1, 1), report_title)
    _set_cell_text(tables[0].cell(2, 1), report_title)
    _set_cell_text(tables[0].cell(3, 1), report_title_b)
    _set_dropdown_cell_text(
        tables[0].cell(4, 1),
        tester_name,
        additional_options=("Battini Vijay",),
    )
    _set_cell_text(tables[0].cell(5, 1), _format_date(report_date))
    overall_cell = tables[0].cell(6, 1)
    if overall_cell._tc.xpath(".//w:sdtPr/w:dropDownList"):
        _set_dropdown_cell_text(
            overall_cell,
            _docx_status_label(overall),
            replace_options=("Pass", "Fail"),
        )
    else:
        _set_cell_text(overall_cell, overall)
    _style_status_cell(overall_cell, overall)

    visual_filter = result.get("visual_filter") or {}
    captured_filter_rows = [
        row
        for row in visual_filter.get("filter_rows") or []
        if isinstance(row, dict)
    ]
    states_by_page: dict[str, tuple[str, list[str]]] = {}
    for state in states:
        page, _ = _split_state(state)
        page_key = page.casefold()
        if page_key not in states_by_page:
            states_by_page[page_key] = (page, [])
        states_by_page[page_key][1].append(state)

    filter_rows = []
    for index, (page, page_states) in enumerate(states_by_page.values(), start=1):
        state = next(
            (
                candidate
                for candidate in page_states
                if _split_state(candidate)[1].casefold() == "default state"
            ),
            None,
        )
        if state is None:
            state = next(
                (
                    candidate
                    for candidate in page_states
                    if candidate in rows_by_state
                ),
                page_states[0],
            )
        page_filter_rows = sorted(
            (
                row
                for row in captured_filter_rows
                if str(row.get("state") or "").casefold() == state.casefold()
            ),
            key=lambda row: row.get("aligned_position") or 0,
        )
        filter_rows.append(
            [
                f"{index:02d}",
                page,
                _filter_action_summary(page_filter_rows),
                FILTER_EXPECTED_BEHAVIOR,
                _filter_validation_status(page_filter_rows),
            ]
        )
    if not filter_rows:
        filter_rows.append(
            [
                "01",
                "—",
                "No filter selections captured.",
                FILTER_EXPECTED_BEHAVIOR,
                "UNVERIFIED",
            ]
        )
    _set_cell_text(
        tables[1].cell(0, 4),
        "Result (Pass/Fail/Unable to Verify)",
    )
    _replace_table_data(tables[1], filter_rows)
    for row_index, row in enumerate(filter_rows, start=1):
        result_cell = tables[1].cell(row_index, 4)
        status = _docx_status_label(row[4])
        if result_cell._tc.xpath(".//w:sdtPr/w:dropDownList"):
            _set_dropdown_cell_text(
                result_cell,
                status,
                replace_options=("Pass", "Fail", "Unable to Verify"),
            )
            for alias in result_cell._tc.xpath(".//w:sdtPr/w:alias"):
                alias.set(
                    qn("w:val"), "Pass / Fail / Unable to Verify"
                )
        else:
            _set_dropdown_control(
                result_cell,
                status,
                title="Pass / Fail / Unable to Verify",
                options=("Pass", "Fail", "Unable to Verify"),
            )
        _style_status_cell(result_cell, status)

    from .visual_filter_report import visual_functional_log_rows

    page_actions = {str(row[1]).casefold(): str(row[2]) for row in filter_rows}
    visual_rows = []
    for log_row in visual_functional_log_rows(result):
        page = _text(log_row.get("page"))
        check = _text(log_row.get("visual_check"))
        base = _text(log_row.get("base"))
        if check.endswith(" Position & Size") or base.startswith("Position:"):
            continue
        status = _docx_status_label(str(log_row.get("status") or ""))
        if status not in {"Pass", "Fail"}:
            continue
        expected = _text(log_row.get("observation")) or FILTER_EXPECTED_BEHAVIOR
        visual_rows.append(
            [
                f"{len(visual_rows) + 1:02d}",
                page,
                _text(log_row.get("compare")) or check,
                page_actions.get(page.casefold(), ""),
                expected,
                status,
            ]
        )
    _set_cell_text(tables[2].cell(0, 2), "Visual / Check")
    _replace_table_data(tables[2], visual_rows)
    _match_run_format(tables[2], source_column=3, target_columns=(1, 2, 4))
    for row_index, row in enumerate(visual_rows, start=1):
        _set_dropdown_control(
            tables[2].cell(row_index, 5),
            _docx_status_label(row[5]),
            title="Pass / Fail",
            options=("Pass", "Fail"),
        )
        _style_status_cell(tables[2].cell(row_index, 5), row[5])

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


def _replace_table_data(table: Any, rows: list[list[str]]) -> None:
    template_row = deepcopy(table.rows[1]._tr)
    while len(table.rows) > 1:
        table._tbl.remove(table.rows[1]._tr)
    for values in rows:
        row = deepcopy(template_row)
        table._tbl.append(row)
        for cell, value in zip(table.rows[-1].cells, values):
            text = _text(value, limit=MAX_CELL_CHARS)
            if cell._tc.xpath(".//w:sdtPr/w:dropDownList"):
                _set_dropdown_cell_text(cell, text)
            else:
                _set_cell_text(cell, text)


def _match_run_format(
    table: Any, source_column: int, target_columns: tuple[int, ...]
) -> None:
    for row in table.rows[1:]:
        source_runs = row.cells[source_column]._tc.xpath(".//w:r")
        source_props = (
            source_runs[0].find(qn("w:rPr")) if source_runs else None
        )
        for column in target_columns:
            for run in row.cells[column]._tc.xpath(".//w:r"):
                existing = run.find(qn("w:rPr"))
                if existing is not None:
                    run.remove(existing)
                if source_props is not None:
                    run.insert(0, deepcopy(source_props))


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
        ):
            for existing in run_properties.findall(qn(tag_name)):
                run_properties.remove(existing)
        for tag_name in ("w:b", "w:bCs"):
            bold = OxmlElement(tag_name)
            run_properties.append(bold)
        color_element = OxmlElement("w:color")
        color_element.set(qn("w:val"), color)
        run_properties.append(color_element)
        for tag_name in ("w:sz", "w:szCs"):
            size = OxmlElement(tag_name)
            size.set(qn("w:val"), str(STATUS_FONT_SIZE_PT * 2))
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
        return
    paragraph.add_run(f"Current screenshot not available for {side_label}.")


def _replace_template_screenshots(
    doc: Any,
    evidence_a: dict[str, dict[str, Any]],
    evidence_b: dict[str, dict[str, Any]],
    states: list[str],
) -> None:
    sections = (
        (
            "2.1 Snowflake",
            "2.2 GCP",
            "2.1 Page-by-Page Screenshots (Snowflake vs. GCP)",
            "3. Visual & Functional Validation Log",
        ),
        (
            "3.1 Snowflake",
            "3.2 GCP",
            "3.1 Page-by-Page Screenshots (Snowflake vs. GCP)",
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
        anchor.insert_paragraph_before(
            "No page screenshots were captured for either dashboard."
        )
        return

    for page, screenshot_a, screenshot_b in page_pairs:
        caption = anchor.insert_paragraph_before()
        caption.add_run(f"Page/Tab Name: {page}").bold = True
        caption.paragraph_format.keep_with_next = True
        available = tuple(
            (path := Path(str((screenshot or {}).get("path") or ""))).name
            and path.is_file()
            for screenshot in (screenshot_a, screenshot_b)
        )
        if not any(available):
            anchor.insert_paragraph_before(
                f"Current screenshots are not available for {page}."
            )
            continue
        for screenshot, label in (
            (screenshot_a, "Snowflake"),
            (screenshot_b, "GCP"),
        ):
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
    pages: dict[str, str] = {}
    ordered_states = list(dict.fromkeys([*evidence_a, *evidence_b, *states]))
    for state in ordered_states:
        page, _ = _split_state(state)
        pages.setdefault(page.casefold(), page)

    def page_evidence(
        page_key: str,
        evidence: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        candidates = [
            state
            for state in ordered_states
            if _split_state(state)[0].casefold() == page_key
            and state in evidence
        ]
        candidates.sort(
            key=lambda state: (
                0
                if Path(str(evidence[state].get("path") or "")).is_file()
                else 1,
                0 if _split_state(state)[1].casefold() == "default state" else 1,
                ordered_states.index(state),
            )
        )
        return evidence[candidates[0]] if candidates else None

    return [
        (page, page_evidence(page_key, evidence_a), page_evidence(page_key, evidence_b))
        for page_key, page in pages.items()
    ]


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


def _group_by_state(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("state") or ""), []).append(row)
    return grouped


def _split_state(state: str) -> tuple[str, str]:
    page, _, filter_part = state.partition("|")
    filter_part = filter_part.strip()
    if not filter_part or filter_part.casefold() == "initial":
        filter_part = "Default state"
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
        return "No filter selections captured."
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
            action = (
                f"{name}: Snowflake: {value_a}; GCP: {value_b}"
            )
        actions.append(action)
    return "\n".join(actions) or "No filter selections captured."


def _filter_validation_status(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "UNVERIFIED"
    statuses = {
        filter_selection_result(
            row.get("selected_values_a"),
            row.get("selected_values_b"),
        )[0]
        for row in rows
    }
    if DIFFERENT in statuses:
        return "FAIL"
    if UNVERIFIED in statuses or statuses - {MATCH}:
        return "UNVERIFIED"
    return "PASS"


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
