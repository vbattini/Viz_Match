from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from openpyxl import load_workbook

from Backend.comparison import compare_dashboards, write_comparison
from Backend.models import DashboardState, FilterCombination, FilterSelection
from Backend.visual_filter_comparison import (
    build_visual_filter_result,
    visual_inventory,
)
from Backend.visual_filter_report import (
    write_visual_validation_sheet,
)


def _box(x: float, y: float, w: float, h: float) -> dict[str, float]:
    return {"x": x, "y": y, "width": w, "height": h}


def _element(element_id: str, element_type: str, box: dict[str, float] | None, text: str = "") -> dict:
    return {
        "element_id": element_id,
        "element_type": element_type,
        "element_text": text,
        "normalized_bounding_box": box,
        "bounding_box": {"x": None, "y": None, "width": None, "height": None},
    }


def _state(
    dashboard: str,
    elements: list[dict],
    filter_item=None,
    filter_values=None,
    visuals=None,
    filter_options=None,
    filter_control_types=None,
) -> DashboardState:
    return DashboardState(
        dashboard=dashboard,
        page="Landing Page",
        filter=filter_item,
        screenshot=Path("unused.png"),
        extracted={"elements": elements},
        filter_names=list((filter_values or {}).keys()),
        filter_values=filter_values or {},
        visuals=visuals or [],
        filter_options=filter_options or {},
        filter_control_types=filter_control_types or {},
    )


def test_visual_inventory_keeps_only_visual_containers_and_uses_title_as_name() -> None:
    state = _state(
        "A",
        [
            _element("TABLE_001", "table", _box(0.1, 0.1, 0.4, 0.4), text="Sales by Dept"),
            _element("COL_001", "column_header", _box(0.1, 0.1, 0.1, 0.05), text="Facility Nbr"),
            _element("TITLE_001", "report_title", _box(0.0, 0.0, 0.5, 0.05), text="Sales by Dept"),
            _element("META_001", "metadata_value", _box(0.8, 0.0, 0.1, 0.03), text="11/3/2026"),
        ],
    )

    visuals = visual_inventory(state)

    assert [v["visual_id"] for v in visuals] == ["TABLE_001"]
    assert visuals[0]["visual_type"] == "Table"
    assert visuals[0]["visual_name"] == "Sales by Dept"


def test_visual_inventory_skips_header_divider_lines_but_keeps_other_shapes() -> None:
    state = _state(
        "A",
        [
            _element(
                "DIVIDER_001",
                "header_divider_line",
                _box(0.1, 0.2, 0.8, 0.002),
            ),
            _element(
                "SHAPE_001",
                "shape_line",
                _box(0.1, 0.3, 0.8, 0.02),
                text="Callout Border",
            ),
        ],
    )

    visuals = visual_inventory(state)

    assert len(visuals) == 1
    assert visuals[0]["visual_id"] == "SHAPE_001"
    assert visuals[0]["visual_type"] == "Shape / Line"


def test_visual_inventory_skips_screenshot_visual_named_header_divider_line() -> None:
    state = _state(
        "A",
        [],
        visuals=[
            {
                "visual_id": "DIVIDER_001",
                "visual_name": "Header Divider Line",
                "visual_type": "Shape / Line",
                **_box(0.1, 0.2, 0.8, 0.002),
            }
        ],
    )

    assert visual_inventory(state) == []


def test_scenario_page_name_and_verified_filter_status_in_excel(tmp_path) -> None:
    scenario = {
        "page": "Item Counts",
        "scenario_id": "02",
        "scenario_name": "Single Filter Selection",
        "scenario_step": "single_filter:vendor=(blank)",
    }
    state_a = replace(
        _state(
            "A",
            [],
            filter_values={"Master Vendor": ["000006 - PETKING INC"]},
        ),
        **scenario,
    )
    state_b = replace(
        _state(
            "B",
            [],
            filter_values={"Master Vendor": ["000006 - PETKING INC"]},
        ),
        **scenario,
    )

    result = compare_dashboards([state_a], [state_b])
    output = tmp_path / "report.xlsx"
    write_visual_validation_sheet(output, result)

    workbook = load_workbook(output, data_only=True)
    sheet = workbook.active
    page_values = [
        sheet.cell(row, 2).value
        for row in range(1, sheet.max_row + 1)
        if sheet.cell(row, 2).value
    ]
    filter_row = next(
        row
        for row in range(1, sheet.max_row + 1)
        if sheet.cell(row, 3).value == "Master Vendor"
    )

    assert "Item Counts" in page_values
    assert not any(
        "Scenario" in str(page) or "single_filter:" in str(page)
        for page in page_values
    )
    assert sheet.cell(filter_row, 2).value == "Item Counts"
    assert sheet.cell(filter_row, 7).value == "Pass"


def test_excel_does_not_pass_filter_without_selected_value_evidence(tmp_path) -> None:
    state_a = _state(
        "A",
        [],
        filter_values={"Facility": []},
        filter_control_types={"Facility": "dropdown"},
    )
    state_b = _state(
        "B",
        [],
        filter_values={"Facility": []},
        filter_control_types={"Facility": "dropdown"},
    )
    result = compare_dashboards([state_a], [state_b])
    result["visual_filter"]["filter_rows"][0]["status"] = "MATCH"
    output = tmp_path / "report.xlsx"

    write_visual_validation_sheet(output, result)

    workbook = load_workbook(output, data_only=True)
    sheet = workbook.active
    filter_row = next(
        row
        for row in range(1, sheet.max_row + 1)
        if sheet.cell(row, 3).value == "Facility"
    )

    assert sheet.cell(filter_row, 7).value == "Unable to Verify"


def test_visual_inventory_skips_visuals_without_geometry() -> None:
    state = _state("A", [_element("TABLE_001", "table", None)])

    assert visual_inventory(state) == []


def test_visual_inventory_uses_dom_layout_without_visual_values() -> None:
    state = _state(
        "A",
        [],
        visuals=[
            {
                "visual_id": "visual-1",
                "visual_name": "Orders",
                "visual_type": "Bar Chart",
                "x": 0.1,
                "y": 0.2,
                "width": 0.3,
                "height": 0.4,
            }
        ],
    )
    state.extracted["title_items"] = [
        {
            "text": "Orders",
            "category": "visual_title",
            "extraction_status": "complete",
            "normalized_bounding_box": {
                "x": 0.1,
                "y": 0.2,
                "width": 0.3,
                "height": 0.05,
            },
        }
    ]

    assert visual_inventory(state) == [
        {
            "visual_id": "visual-1",
            "visual_name": "Orders",
            "visual_type": "Bar Chart",
            "x": 0.1,
            "y": 0.2,
            "width": 0.3,
            "height": 0.4,
        }
    ]
    assert "rows" not in str(state.visuals)
    assert "value" not in str(state.visuals)


def test_visual_inventory_falls_back_to_screenshot_detected_title() -> None:
    state = _state(
        "A",
        [],
        visuals=[
            {
                "visual_id": "TABLE_001",
                "visual_name": "Top Excess Items Table",
                "visual_type": "Table",
                "x": 0.1,
                "y": 0.2,
                "width": 0.3,
                "height": 0.4,
            }
        ],
    )
    state.extracted["title_items"] = []

    visual = visual_inventory(state)[0]
    assert visual["visual_name"] is None
    assert visual["visual_title"] == "Top Excess Items Table"


def test_visual_filter_matches_unique_screenshot_detected_titles() -> None:
    visuals = [
        {
            "visual_id": "SCREENSHOT_001",
            "visual_name": "Excess IO",
            "visual_type": "Column Chart",
            "confidence": "High",
            "x": 0.1,
            "y": 0.2,
            "width": 0.5,
            "height": 0.3,
        }
    ]
    state_a = _state("A", [], visuals=visuals.copy())
    state_b = _state("B", [], visuals=visuals.copy())
    state_a.extracted["title_items"] = []
    state_b.extracted["title_items"] = []

    rows = build_visual_filter_result([state_a], [state_b])["visual_rows"]

    assert len(rows) == 1
    assert rows[0]["visual_name"] == "Excess IO"
    assert rows[0]["visual_name_a"] == "Excess IO"
    assert rows[0]["visual_name_b"] == "Excess IO"
    assert rows[0]["visual_id_a"] == rows[0]["visual_id_b"] == "SCREENSHOT_001"
    assert rows[0]["status"] == "MATCH"


def test_blank_detection_preserves_unnamed_shape_lines() -> None:
    visual_a = {
        "visual_id": "VISUAL_005",
        "visual_type": "Shape / Line",
        "rendering_status": "Blank",
        "x": 0.1,
        "y": 0.1,
        "width": 0.8,
        "height": 0.01,
    }
    visual_b = {
        **visual_a,
        "rendering_status": "Rendered",
    }
    state_a = _state("A", [], visuals=[visual_a])
    state_b = _state("B", [], visuals=[visual_b])

    rows = build_visual_filter_result([state_a], [state_b])["visual_rows"]

    assert len(rows) == 1
    assert rows[0]["visual_id_a"] == rows[0]["visual_id_b"] == "VISUAL_005"
    assert rows[0]["visual_type_a"] == rows[0]["visual_type_b"] == "Shape / Line"
    assert rows[0]["status"] == "MATCH"


def test_visible_kpi_title_does_not_override_blank_rendering_status() -> None:
    visual = {
        "visual_id": "VISUAL_009",
        "visual_name": "Pallets on Premise, Not Rec'd",
        "visual_type": "KPI",
        "x": 728,
        "y": 262,
        "width": 247,
        "height": 67,
        "rendering_status": "Blank",
    }
    title_item = {
        "text": "Pallets",
        "category": "chart_title",
        "extraction_status": "complete",
        "bounding_box": {
            "x": 729.3,
            "y": 263.3,
            "width": 244.2,
            "height": 26,
        },
        "normalized_bounding_box": {
            "x": 0.38,
            "y": 0.24,
            "width": 0.13,
            "height": 0.03,
        },
    }
    state_a = _state(
        "A",
        [],
        visuals=[visual.copy()],
    )
    rendered_visual = {**visual, "rendering_status": "Rendered"}
    state_b = _state("B", [], visuals=[rendered_visual])
    state_a.extracted["title_items"] = [title_item]
    state_b.extracted["title_items"] = [title_item]

    assert visual_inventory(state_a)[0]["rendering_status"] == "Blank"
    assert visual_inventory(state_b)[0]["rendering_status"] == "Rendered"
    assert visual_inventory(state_a)[0]["visual_name"] == "Pallets"
    assert visual_inventory(state_b)[0]["visual_name"] == "Pallets"

    result = build_visual_filter_result([state_a], [state_b])

    assert result["visual_rows"][0]["status"] == "DIFFERENT"
    assert "Visual is blank on Dashboard A" in result["visual_rows"][0][
        "comparison_notes"
    ]


def test_blank_kpi_without_visible_title_is_preserved() -> None:
    blank_visual = {
        "visual_id": "VISUAL_009",
        "visual_name": "Pallets on Premise, Not Rec'd",
        "visual_type": "KPI",
        "x": 728,
        "y": 262,
        "width": 247,
        "height": 67,
        "rendering_status": "Blank",
    }
    state_a = _state(
        "A",
        [],
        visuals=[blank_visual.copy()],
    )
    state_b = _state("B", [], visuals=[blank_visual.copy()])

    assert visual_inventory(state_a)[0]["rendering_status"] == "Blank"
    assert visual_inventory(state_b)[0]["rendering_status"] == "Blank"

    result = build_visual_filter_result([state_a], [state_b])

    assert len(result["visual_rows"]) == 1
    assert result["visual_rows"][0]["status"] == "DIFFERENT"


def test_alert_banner_description_corrects_kpi_type_guess(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from PIL import Image

    state_a = _state(
        "A",
        [],
        visuals=[
            {
                "visual_id": "VISUAL_003",
                "visual_name": (
                    "Active Dashboard Alerts: Scheduled Pallets Fix Pending"
                ),
                "visual_type": "Text Box",
                "observation": (
                    "Yellow callout box displaying alert status text in the "
                    "top right corner."
                ),
                "x": 1455,
                "y": 58,
                "width": 393,
                "height": 83,
                "rendering_status": "Rendered",
            }
        ],
    )
    state_b = _state(
        "B",
        [],
        visuals=[
            {
                "visual_id": "VISUAL_003",
                "visual_name": "Active Dashboard Alerts:",
                "visual_type": "KPI",
                "observation": (
                    "Yellow callout alert banner reading Active Dashboard "
                    "Alerts: Scheduled Pallets Fix Pending."
                ),
                "x": 1455,
                "y": 58,
                "width": 393,
                "height": 83,
                "rendering_status": "Rendered",
            }
        ],
    )

    result = build_visual_filter_result([state_a], [state_b])

    assert result["visual_rows"][0]["visual_type_a"] == "Text Box"
    assert result["visual_rows"][0]["visual_type_b"] == "Text Box"
    assert result["visual_rows"][0]["status"] == "MATCH"

    states = []
    for state, name in ((state_a, "base"), (state_b, "compare")):
        screenshot = tmp_path / f"{name}.png"
        Image.new("RGB", (20, 20), "white").save(screenshot)
        state.extracted["capture_status"] = "captured"
        states.append(replace(state, screenshot=screenshot))

    report = compare_dashboards(states[:1], states[1:])
    write_comparison(report, tmp_path, "Base", "Compare")
    from docx import Document

    docx = Document(tmp_path / "Dashboard_Comparison.docx")
    docx_text = " ".join(
        cell.text
        for table in docx.tables
        for row in table.rows
        for cell in row.cells
    )
    assert (
        "Active Dashboard Alerts: Scheduled Pallets Fix Pending" in docx_text
    )


def test_unknown_chart_type_is_unverified_not_guessed() -> None:
    state_a = _state("A", [_element("CHART_001", "chart", _box(0.1, 0.1, 0.4, 0.4))])
    state_b = _state("B", [_element("CHART_001", "chart", _box(0.1, 0.1, 0.4, 0.4))])

    result = build_visual_filter_result([state_a], [state_b])

    assert result["visual_rows"][0]["status"] == "UNVERIFIED"


def test_build_result_compares_visual_types_and_lists_unmatched_states() -> None:
    a = _state(
        "A",
        [
            _element("TABLE_001", "table", _box(0.1, 0.1, 0.4, 0.4)),
            _element("KPI_001", "kpi", _box(0.6, 0.1, 0.3, 0.2)),
        ],
    )
    b = _state(
        "B",
        [
            _element("TABLE_001", "table", _box(0.1, 0.1, 0.4, 0.4)),
            _element("KPI_001", "chart", _box(0.6, 0.1, 0.3, 0.2)),
        ],
    )
    extra = _state("A", [], filter_item=FilterSelection("Region", "West"))

    result = build_visual_filter_result([a, extra], [b])

    statuses = {row["visual_type_a"]: row["status"] for row in result["visual_rows"]}
    assert statuses["Table"] == "MATCH"
    assert statuses["KPI"] == "UNVERIFIED"
    assert result["unmatched_states"] == [f"A only: {extra.state_key}"]
    for row in result["visual_rows"]:
        assert "column_header" not in str(row)
        assert "report_title" not in str(row)


def test_filter_rows_use_applied_selection_and_options() -> None:
    selection = FilterCombination((FilterSelection("Region", "West"),))
    a = _state("A", [], filter_item=selection, filter_values={"Region": ["East", "West"]})
    b = _state("B", [], filter_item=selection, filter_values={"Region": ["East", "West"]})

    result = build_visual_filter_result([a], [b])

    row = result["filter_rows"][0]
    assert row["filter_name"] == "Region"
    assert row["selected_values_a"] == "West"
    assert row["available_options_a"] == "East, West"
    assert row["filter_type_a"] == "Unknown"
    assert row["status"] == "MATCH"


def test_generic_dropdown_with_matching_selected_value_is_verified() -> None:
    a = _state(
        "A",
        [],
        filter_values={"Facility": ["All"]},
        filter_control_types={"Facility": "dropdown"},
    )
    b = _state(
        "B",
        [],
        filter_values={"Facility": ["All"]},
        filter_control_types={"Facility": "dropdown"},
    )

    row = build_visual_filter_result([a], [b])["filter_rows"][0]

    assert row["filter_type_a"] == row["filter_type_b"] == "Dropdown"
    assert row["selected_values_a"] == row["selected_values_b"] == "All"
    assert row["status"] == "MATCH"


def test_missing_filter_values_remain_unverified() -> None:
    a = _state(
        "A",
        [],
        filter_values={"KPI Selection": []},
        filter_control_types={"KPI Selection": "dropdown"},
    )
    b = _state(
        "B",
        [],
        filter_values={"KPI Selection": []},
        filter_control_types={"KPI Selection": "dropdown"},
    )

    row = build_visual_filter_result([a], [b])["filter_rows"][0]

    assert row["status"] == "UNVERIFIED"
    assert "selected values are missing" in row["comparison_notes"]


def test_filter_rows_use_captured_options_types_and_selections() -> None:
    selection = FilterSelection("Region", "West")
    a = _state(
        "A",
        [],
        filter_item=selection,
        filter_values={"Region": ["West"]},
        filter_options={"Region": ["East", "West"]},
        filter_control_types={"Region": "radio_dropdown"},
    )
    b = _state(
        "B",
        [],
        filter_item=selection,
        filter_values={"Region": ["West"]},
        filter_options={"Region": ["East", "West"]},
        filter_control_types={"Region": "radio_dropdown"},
    )

    result = build_visual_filter_result([a], [b])

    row = result["filter_rows"][0]
    assert row["available_options_a"] == "East, West"
    assert row["selected_values_a"] == "West"
    assert row["filter_type_a"] == "Dropdown"
    assert row["status"] == "MATCH"


def test_unverified_filter_log_explains_missing_control_evidence(
    tmp_path: Path,
) -> None:
    state_a = _state("A", [], filter_values={"Facility": []})
    state_b = _state("B", [], filter_values={"Facility": []})
    result = compare_dashboards([state_a], [state_b])

    filter_row = result["visual_filter"]["filter_rows"][0]
    assert filter_row["status"] == "UNVERIFIED"
    assert "Filter control type could not be identified" in filter_row["comparison_notes"]
    assert "Filter selection could not be verified" in filter_row["comparison_notes"]

    write_comparison(result, tmp_path, "A", "B")
    workbook = load_workbook(tmp_path / "Dashboard_Comparison.xlsx")
    sheet = workbook["Visual_Validation"]
    facility_row = next(
        row
        for row in sheet.iter_rows(values_only=True)
        if len(row) >= 7 and row[2] == "Facility"
    )
    assert "Filter control type could not be identified" in facility_row[5]
    assert "Filter selection could not be verified" in facility_row[5]
    assert facility_row[6] == "Unable to Verify"
    assert "differs" not in facility_row[5].casefold()
    workbook.close()


def test_comparison_workbook_contains_only_visual_validation(
    tmp_path: Path,
) -> None:
    visuals_a = [
        {
            "visual_id": "visual-a",
            "visual_name": "Orders",
            "visual_type": "Bar Chart",
            "x": 100,
            "y": 100,
            "width": 400,
            "height": 300,
        }
    ]
    visuals_b = [
        {
            **visuals_a[0],
            "visual_id": "visual-b",
            "x": 200,
        }
    ]
    filter_options = {"Region": ["East", "West"]}
    filter_types = {"Region": "radio_dropdown"}
    state_a = _state(
        "A",
        [],
        filter_values={"Region": ["West"]},
        visuals=visuals_a,
        filter_options=filter_options,
        filter_control_types=filter_types,
    )
    state_b = _state(
        "B",
        [],
        filter_values={"Region": ["West"]},
        visuals=visuals_b,
        filter_options=filter_options,
        filter_control_types=filter_types,
    )
    result = compare_dashboards([state_a], [state_b])
    write_comparison(result, tmp_path, "A", "B")

    workbook = load_workbook(tmp_path / "Dashboard_Comparison.xlsx")
    assert workbook.sheetnames == ["Visual_Validation"]
    values = [
        cell.value
        for row in workbook["Visual_Validation"].iter_rows()
        for cell in row
        if cell.value is not None
    ]
    assert values
    assert "Visual Differences Summary" in values


def test_slicer_title_and_value_comparison_is_retained(
    tmp_path: Path,
) -> None:
    from docx import Document

    slicer_elements = [
        _element("SLICER_TITLE_001", "slicer_title", _box(0.0, 0.0, 0.2, 0.05), text="Facility Nbr"),
        _element("SLICER_VALUE_001", "slicer_value", _box(0.0, 0.05, 0.2, 0.05), text="Facility 42"),
    ]
    a = _state(
        "A",
        [*slicer_elements, _element("TABLE_001", "table", _box(0.1, 0.1, 0.4, 0.4))],
        filter_values={"Facility Nbr": ["Facility 42"]},
        filter_options={"Facility Nbr": ["Facility 42"]},
        filter_control_types={"Facility Nbr": "dropdown"},
    )
    b = _state(
        "B",
        [*slicer_elements, _element("TABLE_001", "table", _box(0.1, 0.1, 0.4, 0.4))],
        filter_values={"Facility Nbr": ["Facility 42"]},
        filter_options={"Facility Nbr": ["Facility 42"]},
        filter_control_types={"Facility Nbr": "dropdown"},
    )
    for state in (a, b):
        state.extracted["title_items"] = [
            {
                "text": "Facility Nbr",
                "category": "slicer_title",
                "associated_visual": "Facility Nbr",
                "reading_order": 1,
            },
            {
                "text": "Facility 42",
                "category": "slicer_value",
                "associated_visual": "Facility Nbr",
                "reading_order": 2,
            },
        ]

    result = compare_dashboards([a], [b])
    write_comparison(result, tmp_path, "A", "B", operation_log_path=tmp_path / "ops.jsonl")

    workbook = load_workbook(tmp_path / "Dashboard_Comparison.xlsx")
    assert workbook.sheetnames == ["Visual_Validation"]
    xlsx_text = " ".join(
        str(cell.value) for sheet in workbook for row in sheet.iter_rows() for cell in row if cell.value
    )
    document = Document(tmp_path / "Dashboard_Comparison.docx")
    docx_text = " ".join(p.text for p in document.paragraphs)
    docx_text += " " + " ".join(
        cell.text for table in document.tables for row in table.rows for cell in row.cells
    )
    assert "Facility 42" in xlsx_text
    assert "Facility Nbr" in xlsx_text
    assert any(
        row["dashboard_a"] == "Facility 42"
        and row["dashboard_b"] == "Facility 42"
        for row in result["title_comparisons"]
    )
    assert "5. Filter / Slicer Value Comparison" not in docx_text
    assert "6. Screenshot Evidence" not in docx_text
    assert "7. Data Export Validation Log" not in docx_text
    workbook.close()


def test_write_comparison_includes_aligned_title_and_label_rows(tmp_path: Path) -> None:
    a = _state(
        "A",
        [
            _element("TABLE_001", "table", _box(0.1, 0.1, 0.4, 0.4), text="Sales by Dept"),
            _element("COL_001", "column_header", _box(0.1, 0.1, 0.1, 0.05), text="Facility Nbr"),
            _element("TITLE_001", "report_title", _box(0.0, 0.0, 0.5, 0.05), text="Sales by Dept"),
        ],
    )
    b = _state(
        "B",
        [_element("TABLE_001", "table", _box(0.1, 0.1, 0.4, 0.4), text="Sales by Dept")],
    )

    a.extracted["title_items"] = [
        {
            "text": "Sales by Dept",
            "category": "dashboard_title",
            "reading_order": 1,
        },
        {
            "text": "Facility Nbr",
            "category": "column_header",
            "associated_visual": "TABLE_001",
            "reading_order": 2,
        },
    ]
    b.extracted["title_items"] = [
        {
            "text": "Sales by Dept",
            "category": "dashboard_title",
            "reading_order": 1,
        }
    ]
    result = compare_dashboards([a], [b])
    write_comparison(result, tmp_path, "A", "B", operation_log_path=tmp_path / "ops.jsonl")

    assert any(
        row["dashboard_a"] == "Facility Nbr"
        for row in result["title_comparisons"]
    )
    workbook = load_workbook(tmp_path / "Dashboard_Comparison.xlsx")
    assert workbook.sheetnames == ["Visual_Validation"]
    visual_sheet = workbook["Visual_Validation"]
    values = [cell.value for row in visual_sheet.iter_rows() for cell in row]
    assert "Facility Nbr" not in values
    assert "report_title" not in values
    assert (tmp_path / "Dashboard_Comparison.docx").exists()
    assert not (tmp_path / "comparison.docx").exists()
