from datetime import date
from pathlib import Path

from openpyxl import load_workbook

from powerbi_dashboard_comparator.visual_filter_comparison import (
    compare_filter_configurations,
    compare_visual_inventories,
)
from powerbi_dashboard_comparator.visual_filter_report import (
    FAIL_LABEL,
    PASS_LABEL,
    _presentation_match,
    _pass_fail,
    write_visual_filter_docx,
    write_visual_validation_sheet,
)

GRID_TL = (0, 0, 400, 300)


def _visual_row(status: str) -> dict:
    return {
        "page": "Overview",
        "visual_name": "",
        "action_applied": "Facility = East",
        "expected_result": "Same visual type",
        "visual_type_a": "Table",
        "visual_type_b": "Table" if status == "MATCH" else "Line Chart",
        "status": status,
        "comparison_notes": "",
    }


def _rows():
    visuals = compare_visual_inventories(
        "Overview",
        [
            {"visual_id": "TABLE_001", "visual_name": "Orders", "visual_type": "Table", "x": 0, "y": 0, "width": 400, "height": 300},
            {"visual_id": "CHART_001", "visual_name": "Sales", "visual_type": "Bar Chart", "x": 420, "y": 0, "width": 400, "height": 300},
        ],
        [
            {"visual_id": "TABLE_001", "visual_name": "Orders", "visual_type": "Table", "x": 0, "y": 0, "width": 400, "height": 300},
            {"visual_id": "CHART_001", "visual_name": "Sales", "visual_type": "Line Chart", "x": 420, "y": 0, "width": 400, "height": 300},
        ],
        action_applied="Facility = East",
    )
    filters = compare_filter_configurations(
        "Overview",
        [{"name": "Facility", "control_type": "radio_dropdown", "options": ["East", "West"], "selected": ["East"]}],
        [{"name": "Facility", "control_type": "checkbox_dropdown", "options": ["East", "West"], "selected": ["East"]}],
    )
    return visuals, filters


def test_visual_validation_excludes_kpi_numbers_and_table_records() -> None:
    kpi_status, _ = _presentation_match(
        {
            "dashboard_data_a": {"value": 0.65},
            "dashboard_data_b": {"value": 0.0},
        },
        "KPI",
    )
    table_status, _ = _presentation_match(
        {
            "dashboard_data_a": {
                "categories": ["Customer", "Facility"],
                "rows": [["Customer A", "DC 1"]],
            },
            "dashboard_data_b": {
                "categories": ["Customer", "Facility"],
                "rows": [["Customer B", "DC 2"]],
            },
        },
        "Table",
    )

    assert kpi_status == "MATCH"
    assert table_status == "MATCH"


def test_visual_validation_fails_for_blank_kpi_and_different_table_headers() -> None:
    blank_kpi_status, _ = _presentation_match(
        {
            "dashboard_data_a": {"value": "0.65"},
            "dashboard_data_b": {"value": None},
        },
        "KPI",
    )
    table_status, _ = _presentation_match(
        {
            "dashboard_data_a": {"categories": ["Customer", "Facility"]},
            "dashboard_data_b": {"categories": ["Customer", "City"]},
        },
        "Table",
    )

    assert blank_kpi_status == "DIFFERENT"
    assert table_status == "DIFFERENT"


def test_visual_validation_assesses_filters_data_and_layout_separately(
    tmp_path: Path,
) -> None:
    from PIL import Image

    screenshot_a = tmp_path / "base.png"
    screenshot_b = tmp_path / "compare.png"
    Image.new("RGB", (32, 18), "white").save(screenshot_a)
    Image.new("RGB", (32, 18), "gray").save(screenshot_b)
    state = "Fill Rate|initial"
    visual_name = "Fill Rate by DC & Customer & Department"
    workbook_path = tmp_path / "comparison.xlsx"
    result = {
        "visual_filter": {
            "visual_rows": [
                {
                    "state": state,
                    "page": "Fill Rate",
                    "visual_name": visual_name,
                    "visual_id_a": "table-a",
                    "visual_id_b": "table-b",
                    "visual_type_a": "Matrix",
                    "visual_type_b": "Matrix",
                    "position_a": "(0, 0)",
                    "position_b": "(40, 0)",
                    "size_a": "800 x 400",
                    "size_b": "760 x 350",
                    "status": "DIFFERENT",
                    "comparison_notes": (
                        "Same visual type (Matrix); Layout differs "
                        "(position shift 6%, size change 13%)"
                    ),
                }
            ],
            "filter_rows": [
                {
                    "state": state,
                    "page": "Fill Rate",
                    "filter_name": "Relative Time",
                    "selected_values_a": "Yesterday",
                    "selected_values_b": "All",
                    "aligned_position": 1,
                    "status": "DIFFERENT",
                }
            ],
        },
        "screenshot_evidence": {
            "dashboard_a": {
                state: {
                    "path": str(screenshot_a),
                    "capture_status": "captured",
                    "dashboard_title": "Fill Rate by DC, Customer & Department",
                    "refresh_timestamp": "11/4/2026 5:40:11 AM",
                }
            },
            "dashboard_b": {
                state: {
                    "path": str(screenshot_b),
                    "capture_status": "captured",
                    "dashboard_title": "Fill Rate by DC, Customer & Department",
                    "refresh_timestamp": "10/28/2026 5:38:09 AM",
                }
            },
        },
        "comparisons": [
            {
                "state": state,
                "element": visual_name,
                "element_type_a": "table",
                "element_type_b": "table",
                "dashboard_data_a": {
                    "categories": ["Customer", "Facility", "City", "State", "Department"],
                    "rows": [["Customer A", "DC 1", "A", "MN", "Produce"]],
                },
                "dashboard_data_b": {
                    "categories": ["Customer", "Facility", "City", "State", "Department"],
                    "rows": [["Customer B", "DC 2", "B", "WI", "Dairy"]],
                },
            }
        ],
    }

    write_visual_validation_sheet(workbook_path, result)

    workbook = load_workbook(workbook_path)
    assert workbook.sheetnames == ["Visual_Validation"]
    sheet = workbook["Visual_Validation"]
    technical_title_row = next(
        row
        for row in range(1, sheet.max_row + 1)
        if sheet.cell(row, 1).value == "Visual Inventory & Screenshots"
    )
    checks = {
        sheet.cell(row, 3).value: row
        for row in range(1, technical_title_row)
        if str(sheet.cell(row, 1).value or "").isdigit()
    }
    assert "Data Refresh Date" not in checks
    assert sheet.cell(checks["Relative Time"], 7).value == "Fail"
    assert sheet.cell(checks[visual_name], 7).value == "Pass"
    assert "row-level data is excluded" in sheet.cell(checks[visual_name], 6).value
    assert "Table Position & Size" not in checks
    assert "Filters Pane" not in checks
    workbook.close()


def test_filter_validation_log_omits_visual_and_layout_checks(
    tmp_path: Path,
) -> None:
    state = "Overview|initial"
    visual_names = [
        "Filters Pane",
        "Dashboard Navigation",
        "Divider Line",
        "Table Header Formatting",
        "Table Row Formatting",
        "Total Row Formatting",
        "Facility Metrics Matrix",
        "Facility Metrics Matrix Position & Size",
        "VISUAL_005",
    ]
    filter_rows = [
        {
            "state": state,
            "page": "Overview",
            "filter_name": name,
            "selected_values_a": "All",
            "selected_values_b": "All",
            "filter_type_a": "Unknown",
            "filter_type_b": "Unknown",
            "status": "UNVERIFIED",
        }
        for name in visual_names
    ]
    filter_rows.append(
        {
            "state": state,
            "page": "Overview",
            "filter_name": "Facility",
            "selected_values_a": "All",
            "selected_values_b": "All",
            "filter_type_a": "Dropdown",
            "filter_type_b": "Dropdown",
            "status": "MATCH",
        }
    )
    result = {
        "visual_filter": {
            "visual_rows": [
                {
                    "state": state,
                    "page": "Overview",
                    "visual_name": "Facility Metrics Matrix",
                    "visual_id_a": "VISUAL_005",
                    "visual_id_b": "visual-5",
                    "visual_type_a": "Matrix",
                    "visual_type_b": "Matrix",
                    "status": "MATCH",
                }
            ],
            "filter_rows": filter_rows,
        }
    }

    workbook_path = tmp_path / "comparison.xlsx"
    write_visual_validation_sheet(workbook_path, result)

    workbook = load_workbook(workbook_path)
    sheet = workbook["Visual_Validation"]
    filter_section_row = next(
        row
        for row in range(1, sheet.max_row + 1)
        if sheet.cell(row, 1).value == "Filter Validation Log"
    )
    visual_section_row = next(
        row
        for row in range(filter_section_row + 1, sheet.max_row + 1)
        if sheet.cell(row, 1).value == "Visual & Functional Validation Log"
    )
    filter_names = [
        sheet.cell(row, 3).value
        for row in range(filter_section_row + 2, visual_section_row)
        if sheet.cell(row, 1).value
    ]
    assert filter_names == ["Facility"]
    workbook.close()


def test_docx_has_summary_visual_and_filter_sections_only(tmp_path) -> None:
    from docx import Document

    visuals, filters = _rows()
    path = tmp_path / "Report.docx"
    write_visual_filter_docx(
        path,
        "Dashboard A",
        "Dashboard B",
        visuals,
        filters,
        tester_name="Vijay",
        report_date=date(2026, 11, 3),
    )
    doc = Document(str(path))
    headings = [p.text for p in doc.paragraphs if p.style.name.startswith("Heading")]
    assert headings == [
        "1. Dashboard & Environment Details",
        "2. Filter Validation Log",
        "3. Visual & Functional Validation Log",
        "4. Summary & Sign-off",
    ]
    filter_table = doc.tables[1]
    assert [c.text for c in filter_table.rows[0].cells] == [
        "ID", "Page / Tab Name", "Filter Name", "Action / Filter Applied",
        "Expected Behavior", "Result (Pass/Fail)",
    ]
    assert filter_table.cell(1, 4).text == (
        "Filters are applied and visuals are working as expected."
    )
    visual_table = doc.tables[2]
    assert "Vijay" in [c.text for c in doc.tables[-1].rows[1].cells]


def test_docx_row_results_use_pass_and_fail_labels(tmp_path) -> None:
    from docx import Document
    from docx.oxml.ns import qn

    visuals = [
        {**_visual_row("MATCH"), "page": "Department", "visual_name": "Sales"},
        {**_visual_row("DIFFERENT"), "page": "Private Label", "visual_name": "Trend"},
    ]
    path = tmp_path / "Report.docx"
    write_visual_filter_docx(
        path, "A", "B", visuals, [], tester_name="Vijay", report_date=date(2026, 11, 3)
    )
    visual_table = Document(str(path)).tables[2]
    results = [
        "".join(
            node.text or ""
            for node in row.cells[-1]._tc.xpath(".//w:sdtContent//w:t")
        )
        for row in visual_table.rows[1:]
    ]
    assert results == ["Pass", "Fail"]
    for row, color in zip(visual_table.rows[1:], ("008000", "C00000")):
        cell = row.cells[-1]
        assert cell._tc.tcPr.find(qn("w:shd")) is None
        run = cell._tc.xpath(".//w:sdtContent//w:r")[0]
        properties = run.find(qn("w:rPr"))
        assert properties.find(qn("w:b")) is not None
        assert properties.find(qn("w:color")).get(qn("w:val")) == color
        assert properties.find(qn("w:sz")).get(qn("w:val")) == "28"


def test_visual_name_falls_back_to_visual_id_when_no_title() -> None:
    rows = compare_visual_inventories(
        "Overview",
        [{"visual_id": "a1", "visual_type": "Table", "x": 0, "y": 0, "width": 400, "height": 300}],
        [{"visual_id": "a1", "visual_type": "Table", "x": 0, "y": 0, "width": 400, "height": 300}],
    )
    assert rows[0]["visual_name"] == "a1"


def test_docx_visual_log_shows_base_types_and_only_tables_or_charts(
    tmp_path,
) -> None:
    from docx import Document

    visuals = [
        {
            **_visual_row("MATCH"),
            "visual_name": "Sales by Dept",
            "visual_type_a": "Table",
            "visual_type_b": "Table",
        },
        {
            **_visual_row("DIFFERENT"),
            "visual_name": "Page Header",
            "visual_type_a": "Header",
            "visual_type_b": "Header",
        },
        {
            **_visual_row("DIFFERENT"),
            "visual_name": "Monthly Trend",
            "visual_type_a": "Line Chart",
            "visual_type_b": "Area Chart",
        },
    ]
    docx_path = tmp_path / "Report.docx"
    write_visual_filter_docx(
        docx_path, "A", "B", visuals, [], tester_name="Vijay", report_date=date(2026, 11, 3)
    )

    table = Document(str(docx_path)).tables[2]
    assert table.cell(0, 2).text == "Base Visual Type"
    base_types = [row.cells[2].text for row in table.rows[1:]]
    assert base_types == ["Table", "Line Chart"]


def test_docx_visual_validation_log_uses_unique_types_and_default_filters(
    tmp_path,
) -> None:
    from docx import Document
    from docx.oxml.ns import qn

    visuals, filters = _rows()
    path = tmp_path / "Report.docx"
    write_visual_filter_docx(path, "A", "B", visuals, filters)

    table = Document(str(path)).tables[2]
    assert [cell.text for cell in table.rows[0].cells] == [
        "ID",
        "Page / Area",
        "Base Visual Type",
        "Action / Filter Applied",
        "Expected Result",
        "Result",
    ]
    assert len(table.rows) == 3
    row = table.rows[1]
    assert row.cells[1].text == "Overview"
    assert row.cells[2].text == "Table"
    assert row.cells[3].text == "Facility: East"
    assert row.cells[4].text == (
        "Orders should render as Table on both dashboards with consistent "
        "titles and presentation."
    )
    assert table.cell(2, 2).text == "Bar Chart"
    assert table.cell(2, 3).text == "Facility: East"
    assert table.cell(2, 4).text == (
        "Sales should render as Bar Chart on both dashboards with consistent "
        "titles and presentation."
    )
    control = row.cells[5]._tc.xpath(".//w:sdt")[0]
    properties = control.find(qn("w:sdtPr"))
    dropdown = properties.find(qn("w:dropDownList"))
    assert properties.find(qn("w:alias")).get(qn("w:val")) == (
        "Result Status Dropdown (Pass/Fail)"
    )
    assert [
        node.text for node in control.find(qn("w:sdtContent")).iter(qn("w:t"))
    ] == ["Pass"]
    assert [
        item.get(qn("w:displayText"))
        for item in dropdown.findall(qn("w:listItem"))
    ] == ["Pass", "Fail"]
    second_result = table.cell(2, 5)._tc.xpath(".//w:sdtContent//w:t")
    assert "".join(node.text or "" for node in second_result) == "Fail"


def test_docx_defaults_tester_and_uses_status_dropdown(tmp_path) -> None:
    from docx import Document
    from docx.oxml.ns import qn

    path = tmp_path / "Report.docx"
    write_visual_filter_docx(path, "A", "B", [_visual_row("MATCH")], [])
    doc = Document(str(path))
    sign_off = doc.tables[-1]
    assert sign_off.rows[1].cells[1].text == "Vijay Battini"
    status_cell = doc.tables[-1].rows[1].cells[0]
    assert sign_off.rows[0].cells[0].text == "Result Status Dropdown (Pass/Fail)"
    status_control = status_cell._tc.xpath(".//w:sdt")[0]
    properties = status_control.find(qn("w:sdtPr"))
    dropdown = properties.find(qn("w:dropDownList"))
    assert [
        node.text for node in status_control.find(qn("w:sdtContent")).iter(qn("w:t"))
    ] == ["Pass"]
    assert [
        item.get(qn("w:displayText"))
        for item in dropdown.findall(qn("w:listItem"))
    ] == [
        "Pass",
        "Fail",
    ]
    assert "☐" not in "\n".join(cell.text for table in doc.tables for row in table.rows for cell in row.cells)
    assert "☑" not in "\n".join(cell.text for table in doc.tables for row in table.rows for cell in row.cells)
    assert status_cell._tc.tcPr.find(qn("w:shd")) is None
    status_run = status_control.find(qn("w:sdtContent")).find(qn("w:r"))
    properties = status_run.find(qn("w:rPr"))
    assert properties.find(qn("w:b")) is not None
    assert properties.find(qn("w:color")).get(qn("w:val")) == "008000"
    assert properties.find(qn("w:sz")).get(qn("w:val")) == "28"


def _row(status: str) -> dict:
    return {"status": status}


def test_pass_only_when_every_check_matches() -> None:
    assert _pass_fail([_row("MATCH"), _row("MATCH")], [_row("MATCH")]) == PASS_LABEL


def test_info_rows_fail_overall_result() -> None:
    assert _pass_fail([_row("MATCH"), _row("INFO")], []) == FAIL_LABEL


def test_any_difference_is_fail() -> None:
    assert _pass_fail([_row("MATCH")], [_row("DIFFERENT")]) == FAIL_LABEL


def test_unverified_is_not_100_percent_so_fail() -> None:
    assert _pass_fail([_row("MATCH"), _row("UNVERIFIED")], []) == FAIL_LABEL


def test_nothing_to_compare_is_fail() -> None:
    assert _pass_fail([], []) == FAIL_LABEL


def test_docx_sign_off_uses_selected_pass_fail_dropdown(tmp_path) -> None:
    from docx import Document
    from docx.oxml.ns import qn

    path = tmp_path / "pass.docx"
    write_visual_filter_docx(
        path, "A", "B",
        [{"page": "P", "visual_name": "-", "action_applied": "-", "expected_result": "Table",
          "visual_type_a": "Table", "visual_type_b": "Table", "status": "MATCH",
          "comparison_notes": "", "visual_id_a": "", "visual_id_b": "", "position_a": "",
          "position_b": "", "size_a": "", "size_b": ""}],
        [],
        tester_name="Vijay",
    )
    doc = Document(str(path))
    sign_off = doc.tables[-1]
    assert sign_off.rows[0].cells[0].text == "Result Status Dropdown (Pass/Fail)"
    status_control = sign_off.rows[1].cells[0]._tc.xpath(".//w:sdt")[0]
    properties = status_control.find(qn("w:sdtPr"))
    dropdown = properties.find(qn("w:dropDownList"))
    assert [
        node.text for node in status_control.find(qn("w:sdtContent")).iter(qn("w:t"))
    ] == ["Pass"]
    assert [
        item.get(qn("w:displayText"))
        for item in dropdown.findall(qn("w:listItem"))
    ] == [
        "Pass",
        "Fail",
    ]
    assert doc.tables[0].rows[-1].cells[1].text == PASS_LABEL
