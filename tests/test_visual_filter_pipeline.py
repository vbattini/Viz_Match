from __future__ import annotations

from pathlib import Path

from openpyxl import load_workbook

from powerbi_dashboard_comparator.comparison import compare_dashboards, write_comparison
from powerbi_dashboard_comparator.models import DashboardState, FilterCombination, FilterSelection
from powerbi_dashboard_comparator.visual_filter_comparison import (
    build_visual_filter_result,
    visual_inventory,
)
from powerbi_dashboard_comparator.visual_filter_report import (
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
    assert row["status"] == "UNVERIFIED"


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
    assert "Orders" in values
    assert "Bar Chart" in values
    assert "Region" in values
    assert "West" in values


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
