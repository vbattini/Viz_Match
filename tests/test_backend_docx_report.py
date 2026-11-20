import hashlib
import time
from pathlib import Path

from docx import Document
from PIL import Image
from openpyxl import load_workbook

from Backend.comparison import compare_dashboards
from Backend.docx_report import (
    DOCX_FILTER_VALIDATION_HEADERS,
    DOCX_VISUAL_VALIDATION_HEADERS,
    FILTER_EXPECTED_BEHAVIOR,
    _docx_filter_validation_rows,
    _docx_visual_validation_rows,
    _save_document_atomic,
    write_docx_report,
)
from Backend.models import DashboardState
from Backend.visual_filter_report import (
    FILTER_VALIDATION_HEADERS,
    VISUAL_VALIDATION_HEADERS,
    _visual_log_observation,
    filter_validation_log_rows,
    visual_functional_log_rows,
    visual_validation_log_rows,
    write_visual_validation_sheet,
)


def test_docx_filter_log_consolidates_multiple_filters_in_one_state() -> None:
    rows = _docx_filter_validation_rows(
        FILTER_VALIDATION_HEADERS,
        [],
        structured_rows=[
            {
                "state": "Top Excess Items|scenario_02|filter-selection",
                "filter_name": "Facility",
                "selected_values_a": "All",
                "selected_values_b": "All",
                "status": "MATCH",
                "comparison_notes": "",
            },
            {
                "state": "Top Excess Items|scenario_02|filter-selection",
                "filter_name": "Master Vendor",
                "selected_values_a": "000006 - PETKING INC",
                "selected_values_b": "000006 - PETKING INC",
                "status": "MATCH",
                "comparison_notes": "",
            },
        ],
    )

    assert rows == [
        [
            "01",
            "Top Excess Items",
            "Facility: All;\nMaster Vendor: 000006 - PETKING INC;",
            FILTER_EXPECTED_BEHAVIOR,
            "Pass",
        ]
    ]


def test_docx_save_retries_transient_replace_lock(tmp_path, monkeypatch) -> None:
    target = tmp_path / "Dashboard_Comparison.docx"
    original_replace = Path.replace
    attempts = 0

    class FakeDocument:
        def save(self, path) -> None:
            Path(path).write_bytes(b"complete docx")

    def replace_with_transient_lock(source, destination):
        nonlocal attempts
        if destination == target:
            attempts += 1
            if attempts < 3:
                raise PermissionError("file is temporarily locked")
        return original_replace(source, destination)

    monkeypatch.setattr(Path, "replace", replace_with_transient_lock)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    _save_document_atomic(FakeDocument(), target)

    assert attempts == 3
    assert target.read_bytes() == b"complete docx"
    assert list(tmp_path.iterdir()) == [target]


def test_docx_save_preserves_existing_report_when_lock_persists(
    tmp_path, monkeypatch
) -> None:
    target = tmp_path / "Dashboard_Comparison.docx"
    target.write_bytes(b"previous report")

    class FakeDocument:
        def save(self, path) -> None:
            Path(path).write_bytes(b"new report")

    def deny_replace(_source, _destination):
        raise PermissionError("file remains locked")

    monkeypatch.setattr(Path, "replace", deny_replace)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    try:
        _save_document_atomic(FakeDocument(), target)
    except PermissionError as exc:
        assert str(exc) == "file remains locked"
    else:
        raise AssertionError("persistent file lock should be reported")

    assert target.read_bytes() == b"previous report"
    assert list(tmp_path.iterdir()) == [target]


def test_existing_report_contains_default_and_all_scenario_screenshots(tmp_path) -> None:
    baseline_a = tmp_path / "baseline-a.png"
    baseline_b = tmp_path / "baseline-b.png"
    Image.new("RGB", (320, 180), "red").save(baseline_a)
    Image.new("RGB", (320, 180), "blue").save(baseline_b)
    expected_images = [baseline_a, baseline_b]
    scenario_definitions = (
        ("01", "Table Column Header Comparison", "green", "yellow"),
        ("02", "Single Filter Selection", "orange", "purple"),
        ("03", "Multiple Filter Selection", "black", "white"),
        ("04", "Drilldown", "cyan", "magenta"),
    )
    for scenario_id, scenario_name, color_a, color_b in scenario_definitions:
        screenshot_a = tmp_path / f"scenario-{scenario_id}-a.png"
        screenshot_b = tmp_path / f"scenario-{scenario_id}-b.png"
        Image.new("RGB", (320, 180), color_a).save(screenshot_a)
        Image.new("RGB", (320, 180), color_b).save(screenshot_b)
        expected_images.extend((screenshot_a, screenshot_b))
    states_a = [
        DashboardState(
            "A", "Sales", None, baseline_a, {"elements": [], "title_items": []}
        )
    ]
    states_b = [
        DashboardState(
            "B", "Sales", None, baseline_b, {"elements": [], "title_items": []}
        )
    ]
    for scenario_id, scenario_name, *_colors in scenario_definitions:
        screenshot_a = tmp_path / f"scenario-{scenario_id}-a.png"
        screenshot_b = tmp_path / f"scenario-{scenario_id}-b.png"
        extracted_a = {"elements": [], "title_items": []}
        extracted_b = {"elements": [], "title_items": []}
        if scenario_id == "01":
            extracted_a["elements"] = [
                {
                    "element_id": "TABLE_001",
                    "type": "table",
                    "title": "Inventory",
                    "categories": ["Item", "Quantity", "Status"],
                    "rows": [],
                }
            ]
            extracted_b["elements"] = [
                {
                    "element_id": "TABLE_001",
                    "type": "table",
                    "title": "Inventory",
                    "categories": ["Item", "Quantity", "Status"],
                    "rows": [],
                }
            ]
        state_args = {
            "scenario_id": scenario_id,
            "scenario_name": scenario_name,
            "scenario_step": "capture-01",
        }
        states_a.append(
            DashboardState(
                "A",
                "Sales",
                None,
                screenshot_a,
                extracted_a,
                **state_args,
            )
        )
        states_b.append(
            DashboardState(
                "B",
                "Sales",
                None,
                screenshot_b,
                extracted_b,
                **state_args,
            )
        )

    result = compare_dashboards(states_a, states_b)
    log_rows = visual_functional_log_rows(result)
    assert any(
        str(row.get("state") or "").startswith("Sales|scenario_01|")
        for row in log_rows
    )
    output = tmp_path / "Dashboard_Comparison.docx"
    write_docx_report(result, output, "Dashboard A", "Dashboard B")

    generated = Document(output)
    paragraphs = "\n".join(paragraph.text for paragraph in generated.paragraphs)
    assert "Page / Scenario:" not in paragraphs
    assert "Current screenshots are not available" not in paragraphs
    assert "Current screenshot not available" not in paragraphs
    assert "Scenario-by-Scenario Screenshot Evidence" not in paragraphs
    screenshot_captions = [
        paragraph.text.strip()
        for paragraph in generated.paragraphs
        if paragraph.text.strip().startswith("Sales – (")
    ]
    assert "Sales – (baseline-a.png)" in screenshot_captions
    assert "Sales – (baseline-b.png)" in screenshot_captions
    assert "Sales – (scenario-01-a.png)" in screenshot_captions
    assert "Sales – (scenario-01-b.png)" in screenshot_captions
    log_table = generated.tables[2]
    assert [cell.text for cell in log_table.rows[0].cells] == (
        DOCX_VISUAL_VALIDATION_HEADERS
    )
    visual_text = "\n".join(
        cell.text for row in log_table.rows for cell in row.cells
    )
    assert "TABLE_001" not in visual_text
    assert "columns:" not in visual_text.casefold()
    assert "table_headers_initial:initial" not in visual_text
    embedded_hashes = {
        hashlib.sha256(
            generated.part.related_parts[
                shape._inline.graphic.graphicData.pic.blipFill.blip.embed
            ].blob
        ).hexdigest()
        for shape in generated.inline_shapes
    }
    for screenshot in expected_images:
        assert hashlib.sha256(screenshot.read_bytes()).hexdigest() in embedded_hashes


def test_docx_filter_log_matches_all_excel_filter_records(tmp_path) -> None:
    states_a = []
    states_b = []
    scenario_filters = [
        ("01", "All", "All", "MATCH"),
        (
            "02",
            "000006 - PETKING INC",
            "000006 - PETKING INC",
            "MATCH",
        ),
        ("03", "North, (Blank)", "North, (Blank)", "MATCH"),
        ("04", "Region A", "Region B", "DIFFERENT"),
        ("05", "", "", "UNVERIFIED"),
    ]
    for scenario_id, *_ in scenario_filters:
        screenshot_a = tmp_path / f"scenario-{scenario_id}-a.png"
        screenshot_b = tmp_path / f"scenario-{scenario_id}-b.png"
        Image.new("RGB", (160, 90), "red").save(screenshot_a)
        Image.new("RGB", (160, 90), "blue").save(screenshot_b)
        state_args = {
            "scenario_id": scenario_id,
            "scenario_name": f"Scenario {scenario_id}",
            "scenario_step": "filter-selection",
        }
        states_a.append(
            DashboardState(
                "A",
                "Inventory",
                None,
                screenshot_a,
                {"elements": [], "title_items": []},
                **state_args,
            )
        )
        states_b.append(
            DashboardState(
                "B",
                "Inventory",
                None,
                screenshot_b,
                {"elements": [], "title_items": []},
                **state_args,
            )
        )

    result = compare_dashboards(states_a, states_b)
    result["visual_filter"]["filter_rows"] = [
        {
            "state": f"Inventory|scenario_{scenario_id}|filter-selection",
            "filter_name": "Master Vendor",
            "selected_values_a": selected_a,
            "selected_values_b": selected_b,
            "status": status,
            "aligned_position": index,
            "comparison_notes": "",
        }
        for index, (scenario_id, selected_a, selected_b, status) in enumerate(
            scenario_filters
        )
    ]

    excel_path = tmp_path / "Dashboard_Comparison.xlsx"
    filter_rows = []
    visual_rows = []
    visual_states = []
    write_visual_validation_sheet(
        excel_path,
        result,
        filter_rows_out=filter_rows,
        visual_rows_out=visual_rows,
        visual_states_out=visual_states,
    )
    docx_path = tmp_path / "Dashboard_Comparison.docx"
    write_docx_report(
        result,
        docx_path,
        "Dashboard A",
        "Dashboard B",
        filter_validation_log=(FILTER_VALIDATION_HEADERS, filter_rows),
        visual_validation_log=(VISUAL_VALIDATION_HEADERS, visual_rows),
        visual_validation_states=visual_states,
    )

    workbook = load_workbook(excel_path, data_only=True)
    sheet = workbook["Visual_Validation"]
    filter_section_row = next(
        row
        for row in range(1, sheet.max_row + 1)
        if sheet.cell(row, 1).value == "Filter Validation Log"
    )
    excel_headers = [
        sheet.cell(filter_section_row + 1, column).value
        for column in range(1, 8)
    ]
    visual_section_row = next(
        row
        for row in range(filter_section_row + 2, sheet.max_row + 1)
        if sheet.cell(row, 1).value == "Visual & Functional Validation Log"
    )
    excel_rows = [
        [sheet.cell(row, column).value for column in range(1, 8)]
        for row in range(filter_section_row + 2, visual_section_row)
        if sheet.cell(row, 1).value is not None
    ]
    visual_section_row = next(
        row
        for row in range(1, sheet.max_row + 1)
        if sheet.cell(row, 1).value == "Visual & Functional Validation Log"
    )
    result_summary_row = next(
        row
        for row in range(visual_section_row + 2, sheet.max_row + 1)
        if sheet.cell(row, 1).value == "Result Summary"
    )
    excel_visual_headers = [
        sheet.cell(visual_section_row + 1, column).value
        for column in range(1, 9)
    ]
    excel_visual_rows = [
        [sheet.cell(row, column).value for column in range(1, 9)]
        for row in range(visual_section_row + 2, result_summary_row)
        if sheet.cell(row, 1).value is not None
    ]
    workbook.close()

    generated = Document(docx_path)
    docx_table = generated.tables[1]
    docx_rows = [[cell.text for cell in row.cells] for row in docx_table.rows]
    assert excel_headers == FILTER_VALIDATION_HEADERS
    assert docx_rows == [
        DOCX_FILTER_VALIDATION_HEADERS,
        [
            "01",
            "Inventory",
            "Master Vendor: All;",
            FILTER_EXPECTED_BEHAVIOR,
            "Pass",
        ],
        [
            "02",
            "Inventory",
            "Master Vendor: 000006 - PETKING INC;",
            FILTER_EXPECTED_BEHAVIOR,
            "Pass",
        ],
        [
            "03",
            "Inventory",
            "Master Vendor: North, (Blank);",
            FILTER_EXPECTED_BEHAVIOR,
            "Pass",
        ],
        [
            "04",
            "Inventory",
            "Master Vendor: Base: Region A; Compare: Region B;",
            FILTER_EXPECTED_BEHAVIOR,
            "Fail",
        ],
        [
            "05",
            "Inventory",
            "Master Vendor: Not captured;",
            FILTER_EXPECTED_BEHAVIOR,
            "Unable to Verify",
        ],
    ]
    assert len(docx_rows) - 1 == 5
    assert len(excel_rows) < len(docx_rows) - 1
    assert filter_rows == filter_validation_log_rows(result)
    assert docx_table.rows[0]._tr.xpath("./w:trPr/w:tblHeader")
    assert all(
        row._tr.xpath("./w:trPr/w:cantSplit")
        for row in docx_table.rows[1:]
    )

    docx_visual_table = generated.tables[2]
    docx_visual_rows = [
        [cell.text for cell in row.cells] for row in docx_visual_table.rows
    ]
    assert excel_visual_headers == VISUAL_VALIDATION_HEADERS
    assert excel_visual_rows == visual_validation_log_rows(result)
    assert docx_visual_rows[0] == DOCX_VISUAL_VALIDATION_HEADERS
    assert len(docx_visual_rows) == len(excel_visual_rows) + 1
    assert [row[0] for row in docx_visual_rows[1:]] == [
        str(row[0]) for row in excel_visual_rows
    ]
    assert [row[1] for row in docx_visual_rows[1:]] == [
        str(row[1]) for row in excel_visual_rows
    ]
    assert [row[5] for row in docx_visual_rows[1:]] == [
        str(row[7]) for row in excel_visual_rows
    ]
    assert [row[4] for row in docx_visual_rows[1:]] == [
        str(row[6]) for row in excel_visual_rows
    ]
    assert all(len(row) == 6 for row in docx_visual_rows)


def test_docx_visual_log_shortens_header_lists_and_identifiers() -> None:
    headers = VISUAL_VALIDATION_HEADERS
    long_headers = (
        "Matrix/Table displayed; columns: Item Number, Facility Name, "
        "Last Received Date, Inventory Total"
    )
    rows = [
        [
            "07",
            "Report",
            long_headers,
            long_headers,
            "table_headers_initial:initial",
            long_headers,
            "Same table column structure; row-level data is excluded.",
            "Pass",
        ],
        [
            "08",
            "Report",
            "Table",
            "Table",
            "TABLE_002",
            "Table",
            "Visual is present on only one dashboard.",
            "Fail",
        ],
    ]
    states = [
        "Report|scenario_01|table_headers_initial:initial",
        "Report|scenario_02|single_filter:master vendor=000006 - petking inc",
    ]
    filter_rows = [
        {
            "state": "Report|initial",
            "filter_name": "Facility Nbr Desc",
            "selected_values_a": "All",
            "selected_values_b": "All",
        },
        {
            "state": states[1],
            "filter_name": "Master Vendor",
            "selected_values_a": "000006 - PETKING INC",
            "selected_values_b": "000006 - PETKING INC",
        },
    ]

    result = _docx_visual_validation_rows(
        headers, rows, states, filter_rows, []
    )

    assert result == [
        [
            "07",
            "Report",
            "Table",
            "Facility Nbr Desc: All;",
            "Same table column structure; row-level data is excluded.",
            "Pass",
        ],
        [
            "08",
            "Report",
            "Table",
            "Master Vendor: 000006 - PETKING INC;",
            "Visual is present on only one dashboard.",
            "Fail",
        ],
    ]


def test_docx_visual_expected_result_excludes_filter_context() -> None:
    headers = VISUAL_VALIDATION_HEADERS
    rows = [
        [
            "01",
            "Report",
            "Table",
            "Table",
            "TABLE_001",
            "Table",
            (
                "Table visual types match between both dashboards. "
                "Screenshot styling requires separate visual review. "
                "Filter: Facility: All; Manager, Supply Planner: All;."
            ),
            "Pass",
        ]
    ]
    states = ["Report|scenario_02|single_filter:facility=All"]
    filter_rows = [
        {
            "state": states[0],
            "filter_name": "Facility",
            "selected_values_a": "All",
            "selected_values_b": "All",
        }
    ]

    result = _docx_visual_validation_rows(
        headers, rows, states, filter_rows, []
    )

    assert result[0][3] == "Facility: All;"
    assert result[0][4] == (
        "Table visual types match between both dashboards. "
        "Screenshot styling requires separate visual review."
    )


def test_visual_failures_name_missing_side_and_real_evidence_files(tmp_path) -> None:
    state = "Report|scenario_01|table_headers_scroll:horizontal_scroll_01"
    base_screenshot = tmp_path / "dashboard_A_S01_scroll.png"
    compare_screenshot = tmp_path / "dashboard_B_S01_scroll.png"
    Image.new("RGB", (160, 90), "red").save(base_screenshot)
    Image.new("RGB", (160, 90), "blue").save(compare_screenshot)
    result = {
        "screenshot_evidence": {
            "dashboard_a": {
                state: {
                    "path": str(base_screenshot),
                    "capture_status": "captured",
                }
            },
            "dashboard_b": {
                state: {
                    "path": str(compare_screenshot),
                    "capture_status": "captured",
                }
            },
        }
    }

    missing_base = _visual_log_observation(
        {
            "visual_check": "TABLE_001",
            "base": "Missing",
            "compare": "Table",
            "observation": "Visual is present on only one dashboard.",
            "status": "Fail",
            "state": state,
        },
        result,
    )
    missing_compare = _visual_log_observation(
        {
            "visual_check": "TABLE_001",
            "base": "Table",
            "compare": "Missing",
            "observation": "Visual is present on only one dashboard.",
            "status": "Fail",
            "state": state,
        },
        result,
    )
    changed_headers = _visual_log_observation(
        {
            "visual_check": "Untitled visual",
            "base": "Matrix/Table displayed; columns: MDSE Dept, MDSE Group",
            "compare": "Matrix/Table displayed; columns: DSE Dept, MDSE Group",
            "observation": "Table column structure differs.",
            "status": "Fail",
            "state": state,
        },
        result,
    )
    missing_evidence = _visual_log_observation(
        {
            "visual_check": "TABLE_001",
            "base": "Missing",
            "compare": "Table",
            "observation": "Visual is present on only one dashboard.",
            "status": "Fail",
            "state": state,
        },
        {"screenshot_evidence": {}},
    )

    assert missing_base.startswith("Table visual is missing from Base Dashboard.")
    assert missing_compare.startswith(
        "Table visual is missing from Compare Dashboard."
    )
    assert "dashboard_A_S01_scroll.png" in missing_base
    assert "dashboard_B_S01_scroll.png" in missing_base
    assert "dashboard_A_S01_scroll.png" in missing_compare
    assert "dashboard_B_S01_scroll.png" in missing_compare
    assert "Compare is missing MDSE Dept and has DSE Dept instead." in changed_headers
    assert "dashboard_A_S01_scroll.png" in changed_headers
    assert "dashboard_B_S01_scroll.png" in changed_headers
    assert missing_evidence.endswith("Screenshot evidence unavailable.")
