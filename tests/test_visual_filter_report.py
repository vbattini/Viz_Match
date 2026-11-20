from datetime import date
from pathlib import Path

from openpyxl import load_workbook

try:
    from powerbi_dashboard_comparator.visual_filter_comparison import (
        compare_filter_configurations,
        compare_visual_inventories,
    )
    from powerbi_dashboard_comparator.visual_filter_report import (
        FAIL_LABEL,
        PASS_LABEL,
        UNVERIFIED_LABEL,
        _both_visual_types_unavailable,
        _hierarchy_excel_rows,
        _presentation_match,
        _pass_fail,
        _visual_display_name,
        write_visual_filter_docx,
        write_visual_validation_sheet,
    )
    from powerbi_dashboard_comparator.docx_report import _filter_action_summary
except ModuleNotFoundError:
    from Backend.visual_filter_comparison import (
        compare_filter_configurations,
        compare_visual_inventories,
    )
    from Backend.visual_filter_report import (
        FAIL_LABEL,
        PASS_LABEL,
        UNVERIFIED_LABEL,
        _both_visual_types_unavailable,
        _hierarchy_excel_rows,
        _presentation_match,
        _pass_fail,
        _visual_display_name,
        write_visual_filter_docx,
        write_visual_validation_sheet,
    )
    from Backend.docx_report import _filter_action_summary

GRID_TL = (0, 0, 400, 300)


def test_filter_action_summary_contains_complete_ordered_state_and_terminal_semicolons() -> None:
    rows = [
        {
            "filter_name": name,
            "selected_values_a": value,
            "selected_values_b": value,
        }
        for name, value in (
            ("Facility", "02 - Sioux Falls"),
            ("Manager, Supply Planner", "All"),
            ("Category", "Grocery"),
            ("Master Vendor", "ABC Vendor"),
            ("Purchase Status", "Open"),
            ("Center Store Item", "All"),
        )
    ]

    summary = _filter_action_summary(rows)

    assert summary.splitlines() == [
        "Facility: 02 - Sioux Falls;",
        "Manager, Supply Planner: All;",
        "Category: Grocery;",
        "Master Vendor: ABC Vendor;",
        "Purchase Status: Open;",
        "Center Store Item: All;",
    ]


def test_hierarchy_excel_rows_preserve_four_level_paths_and_status() -> None:
    structure_a = {
        "status": "PASS",
        "columns": [
            {"name": "Master Vendor", "index": 0, "level": 1},
            {"name": "Supply Planner", "index": 1, "level": 2},
            {"name": "Facility", "index": 2, "level": 3},
            {"name": "Item", "index": 3, "level": 4},
        ],
        "rows": [
            {
                "hierarchy_level": 4,
                "row_type": "detail",
                "original_row_order": 0,
                "parent_path": [
                    "025000 - THE COCA COLA COMPANY",
                    "Kris Bosshart",
                    "39 - Oklahoma City",
                ],
                "current_value": "1162002217 - I/O FAIRLIFE NUTR PLN VAN",
                "values": [
                    "025000 - THE COCA COLA COMPANY",
                    "Kris Bosshart",
                    "39 - Oklahoma City",
                    "1162002217 - I/O FAIRLIFE NUTR PLN VAN",
                ],
            }
        ],
    }
    structure_b = {
        **structure_a,
        "rows": [
            {
                **structure_a["rows"][0],
                "current_value": "1162002218 - I/O FAIRLIFE NUTRTN PLAN",
                "values": [
                    "025000 - THE COCA COLA COMPANY",
                    "Kris Bosshart",
                    "39 - Oklahoma City",
                    "1162002218 - I/O FAIRLIFE NUTRTN PLAN",
                ],
            }
        ],
    }

    rows = _hierarchy_excel_rows(
        {
            "comparisons": [
                {
                    "page": "Top Excess Items",
                    "state": "Top Excess Items|initial",
                    "visual_name": "Table",
                    "hierarchy_structure_a": structure_a,
                    "hierarchy_structure_b": structure_b,
                }
            ]
        }
    )

    assert rows[0][4] == 4
    assert rows[0][5:13] == [
        "025000 - THE COCA COLA COMPANY",
        "025000 - THE COCA COLA COMPANY",
        "Kris Bosshart",
        "Kris Bosshart",
        "39 - Oklahoma City",
        "39 - Oklahoma City",
        "1162002217 - I/O FAIRLIFE NUTR PLN VAN",
        "1162002218 - I/O FAIRLIFE NUTRTN PLAN",
    ]
    assert rows[0][-1] == "Fail"


def test_rows_with_unavailable_types_on_both_sides_are_identified_for_exclusion() -> None:
    for type_a, type_b in (
        ("", None),
        ("Unidentified", "undefined"),
        ("-", "—"),
        (" -- ", "---"),
        ("Missing", "Missing"),
        ("Missing", "Unidentified"),
        ("Unidentified", "Missing"),
    ):
        assert _both_visual_types_unavailable(
            {"visual_type_a": type_a, "visual_type_b": type_b}
        )

    assert not _both_visual_types_unavailable(
        {"visual_type_a": "Table", "visual_type_b": "—"}
    )


def test_workbook_omits_visuals_unidentified_on_both_sides(
    tmp_path: Path,
) -> None:
    visuals = compare_visual_inventories(
        "Overview",
        [
            {
                "visual_id": "table",
                "visual_name": "Orders",
                "visual_type": "Table",
                "x": 0,
                "y": 0,
                "width": 400,
                "height": 300,
            }
        ],
        [
            {
                "visual_id": "table",
                "visual_name": "Orders",
                "visual_type": "Table",
                "x": 0,
                "y": 0,
                "width": 400,
                "height": 300,
            }
        ],
    )
    visuals[0]["state"] = "Overview|initial"
    visuals.append(
        {
            "state": "Overview|initial",
            "page": "Overview",
            "visual_name": "No identified type",
            "visual_type_a": "Unidentified",
            "visual_type_b": "—",
            "status": "UNVERIFIED",
        }
    )
    visuals.extend(
        {
            "state": "Overview|initial",
            "page": "Overview",
            "visual_name": f"Unavailable visual {index}",
            "visual_type_a": type_a,
            "visual_type_b": type_b,
            "status": "DIFFERENT",
        }
        for index, (type_a, type_b) in enumerate(
            (
                ("Missing", "Missing"),
                ("Missing", "Unidentified"),
                ("Unidentified", "Missing"),
            ),
            start=1,
        )
    )
    workbook_path = tmp_path / "comparison.xlsx"
    base_screenshot = tmp_path / "base.png"
    compare_screenshot = tmp_path / "compare.png"
    base_screenshot.touch()
    compare_screenshot.touch()
    write_visual_validation_sheet(
        workbook_path,
        {
            "visual_filter": {"visual_rows": visuals, "filter_rows": []},
            "screenshot_evidence": {
                "dashboard_a": {
                    "Overview|initial": {
                        "path": str(base_screenshot),
                        "capture_status": "captured",
                    }
                },
                "dashboard_b": {
                    "Overview|initial": {
                        "path": str(compare_screenshot),
                        "capture_status": "captured",
                    }
                },
            },
        },
    )

    workbook = load_workbook(workbook_path, data_only=True)
    sheet = workbook["Visual_Validation"]
    section_row = next(
        row
        for row in range(1, sheet.max_row + 1)
        if sheet.cell(row, 1).value == "Visual & Functional Validation Log"
    )
    visual_names = [
        sheet.cell(row, 5).value
        for row in range(section_row + 2, sheet.max_row + 1)
        if str(sheet.cell(row, 1).value or "").isdigit()
    ]
    assert visual_names == ["Table"]
    workbook.close()


def test_workbook_ids_restart_for_each_validation_section(tmp_path: Path) -> None:
    state = "LTMO|initial"
    base_screenshot = tmp_path / "base.png"
    compare_screenshot = tmp_path / "compare.png"
    base_screenshot.touch()
    compare_screenshot.touch()
    workbook_path = tmp_path / "comparison.xlsx"
    write_visual_validation_sheet(
        workbook_path,
        {
            "visual_filter": {
                "visual_rows": [
                    {
                        "state": state,
                        "page": "LTMO",
                        "visual_name": "Main Table",
                        "visual_type_a": "Table",
                        "visual_type_b": "Table",
                        "status": "MATCH",
                    },
                    {
                        "state": "Inbound POs|initial",
                        "page": "Inbound POs",
                        "visual_name": "Inbound Table",
                        "visual_type_a": "Table",
                        "visual_type_b": "Table",
                        "status": "MATCH",
                    },
                ],
                "filter_rows": [
                    {
                        "state": state,
                        "page": "LTMO",
                        "filter_name": "Facility",
                        "selected_values_a": "All",
                        "selected_values_b": "All",
                        "filter_type_a": "Dropdown",
                        "filter_type_b": "Dropdown",
                        "status": "MATCH",
                    }
                ],
            },
            "screenshot_evidence": {
                "dashboard_a": {
                    state: {
                        "path": str(base_screenshot),
                        "capture_status": "captured",
                    },
                    "Inbound POs|initial": {
                        "path": str(base_screenshot),
                        "capture_status": "captured",
                    },
                },
                "dashboard_b": {
                    state: {
                        "path": str(compare_screenshot),
                        "capture_status": "captured",
                    },
                    "Inbound POs|initial": {
                        "path": str(compare_screenshot),
                        "capture_status": "captured",
                    },
                },
            },
        },
    )

    workbook = load_workbook(workbook_path, data_only=True)
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
    filter_ids = [
        sheet.cell(row, 1).value
        for row in range(filter_section_row + 2, visual_section_row)
        if str(sheet.cell(row, 1).value or "").isdigit()
    ]
    visual_ids = [
        sheet.cell(row, 1).value
        for row in range(visual_section_row + 2, sheet.max_row + 1)
        if str(sheet.cell(row, 1).value or "").isdigit()
    ]
    assert filter_ids == ["01"]
    assert visual_ids == ["01", "02"]
    workbook.close()


def test_workbook_keeps_rendered_visual_match_despite_unrelated_page_warning(
    tmp_path: Path,
) -> None:
    state = "Expired LTMO|initial"
    base_screenshot = tmp_path / "base.png"
    compare_screenshot = tmp_path / "compare.png"
    base_screenshot.touch()
    compare_screenshot.touch()
    workbook_path = tmp_path / "comparison.xlsx"
    warning = {
        "reason": "Power BI chart content did not finish rendering",
        "indicators": [
            {"unrendered_chart_visuals": [{"title": "DC_NAME\nAll"}]}
        ],
    }

    write_visual_validation_sheet(
        workbook_path,
        {
            "visual_filter": {
                "visual_rows": [
                    {
                        "state": state,
                        "page": "Expired LTMO",
                        "visual_name": "TABLE_001",
                        "visual_id_a": "TABLE_001",
                        "visual_id_b": "TABLE_001",
                        "visual_type_a": "Table",
                        "visual_type_b": "Table",
                        "rendering_status_a": "Rendered",
                        "rendering_status_b": "Rendered",
                        "position_a": "(186, 111)",
                        "position_b": "(186, 111)",
                        "size_a": "1448 x 736",
                        "size_b": "1448 x 736",
                        "status": "MATCH",
                    }
                ],
                "filter_rows": [],
            },
            "screenshot_evidence": {
                "dashboard_a": {
                    state: {
                        "path": str(base_screenshot),
                        "capture_status": "captured",
                        "render_unverified": warning,
                    }
                },
                "dashboard_b": {
                    state: {
                        "path": str(compare_screenshot),
                        "capture_status": "captured",
                        "render_unverified": warning,
                    }
                },
            },
        },
    )

    workbook = load_workbook(workbook_path, data_only=True)
    sheet = workbook["Visual_Validation"]
    visual_row = next(
        row
        for row in sheet.iter_rows(values_only=True)
        if row[1] == "Expired LTMO" and row[4] == "Table"
    )
    assert (
        "Table visual types match between both dashboards. "
        "Screenshot styling requires separate visual review."
        in visual_row[6]
    )
    assert "Evidence:" not in visual_row[6]
    assert "Filter:" in visual_row[6]
    assert visual_row[7] == "Pass"
    workbook.close()


def test_workbook_passes_matching_blank_kpis_despite_page_render_warning(
    tmp_path: Path,
) -> None:
    state = "Fill Rate Trend|initial"
    base_screenshot = tmp_path / "base.png"
    compare_screenshot = tmp_path / "compare.png"
    base_screenshot.touch()
    compare_screenshot.touch()
    workbook_path = tmp_path / "comparison.xlsx"
    warning = {
        "reason": "Power BI chart content did not finish rendering",
        "indicators": [
            {
                "unrendered_chart_visuals": [
                    {"title": "Fill Rate\n(Blank)\n4 Week Avg: 83.5%"}
                ]
            }
        ],
    }

    write_visual_validation_sheet(
        workbook_path,
        {
            "visual_filter": {
                "visual_rows": [
                    {
                        "state": state,
                        "page": "Fill Rate Trend",
                        "visual_name": "Fill Rate",
                        "visual_name_a": "Fill Rate",
                        "visual_name_b": "Fill Rate",
                        "visual_id_a": "VISUAL_006",
                        "visual_id_b": "VISUAL_006",
                        "visual_type_a": "KPI",
                        "visual_type_b": "KPI",
                        "visual_confidence_a": "High",
                        "visual_confidence_b": "High",
                        "rendering_status_a": "Blank",
                        "rendering_status_b": "Blank",
                        "position_a": "(357, 634)",
                        "position_b": "(357, 634)",
                        "size_a": "337 x 143",
                        "size_b": "337 x 143",
                        "status": "MATCH",
                    }
                ],
                "filter_rows": [],
            },
            "screenshot_evidence": {
                "dashboard_a": {
                    state: {
                        "path": str(base_screenshot),
                        "capture_status": "captured",
                        "render_unverified": warning,
                    }
                },
                "dashboard_b": {
                    state: {
                        "path": str(compare_screenshot),
                        "capture_status": "captured",
                        "render_unverified": warning,
                    }
                },
            },
        },
    )

    workbook = load_workbook(workbook_path, data_only=True)
    sheet = workbook["Visual_Validation"]
    visual_row = next(
        row
        for row in sheet.iter_rows(values_only=True)
        if row[1] == "Fill Rate Trend" and row[4] == "KPI"
    )
    assert (
        "KPI visual types match between both dashboards. "
        "Screenshot styling requires separate visual review."
        in visual_row[6]
    )
    assert "Evidence:" not in visual_row[6]
    assert "Filter:" in visual_row[6]
    assert visual_row[7] == "Pass"
    workbook.close()


def test_workbook_keeps_matched_screenshot_chart_pair_observation(
    tmp_path: Path,
) -> None:
    state = "Fill Rate Trend|initial"
    base_screenshot = tmp_path / "base.png"
    compare_screenshot = tmp_path / "compare.png"
    base_screenshot.touch()
    compare_screenshot.touch()
    workbook_path = tmp_path / "comparison.xlsx"
    observation = (
        "Both screenshots show the same rendered visual type with matching "
        "geometry; generated visual labels differ and are not treated as a "
        "title mismatch."
    )

    write_visual_validation_sheet(
        workbook_path,
        {
            "visual_filter": {
                "visual_rows": [
                    {
                        "state": state,
                        "page": "Fill Rate Trend",
                        "visual_name": (
                            "Base: Fill Rate and Vendor Adj Fill Rate by "
                            "Fiscal Year; Compare: Fill Rate Trend Line Chart"
                        ),
                        "visual_name_a": (
                            "Fill Rate and Vendor Adj Fill Rate by Fiscal Year"
                        ),
                        "visual_name_b": "Fill Rate Trend Line Chart",
                        "visual_id_a": "SCREENSHOT_001",
                        "visual_id_b": "SCREENSHOT_001",
                        "visual_type_a": "Line Chart",
                        "visual_type_b": "Line Chart",
                        "visual_confidence_a": "High",
                        "visual_confidence_b": "High",
                        "rendering_status_a": "Rendered",
                        "rendering_status_b": "Rendered",
                        "position_a": "(703, 167)",
                        "position_b": "(705, 161)",
                        "size_a": "1357 x 375",
                        "size_b": "1362 x 392",
                        "status": "MATCH",
                        "comparison_notes": observation,
                    }
                ],
                "filter_rows": [],
            },
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
            },
        },
    )

    workbook = load_workbook(workbook_path, data_only=True)
    sheet = workbook["Visual_Validation"]
    visual_row = next(
        row
        for row in sheet.iter_rows(values_only=True)
        if row[1] == "Fill Rate Trend" and row[4] == "Line Chart"
    )
    assert (
        "Line Chart visual types match between both dashboards. "
        "Screenshot styling requires separate visual review."
        in visual_row[6]
    )
    assert "Evidence:" not in visual_row[6]
    assert "Filter:" in visual_row[6]
    assert visual_row[7] == "Pass"
    workbook.close()


def test_workbook_keeps_unverified_visual_rows(tmp_path: Path) -> None:
    state = "Expired LTMO|initial"
    workbook_path = tmp_path / "comparison.xlsx"
    write_visual_validation_sheet(
        workbook_path,
        {
            "visual_filter": {
                "visual_rows": [
                    {
                        "state": state,
                        "page": "Expired LTMO",
                        "visual_name": "TABLE_001",
                        "visual_id_a": "TABLE_001",
                        "visual_id_b": "TABLE_001",
                        "visual_type_a": "Table",
                        "visual_type_b": "Table",
                        "rendering_status_a": "Unable to Verify",
                        "rendering_status_b": "Unable to Verify",
                        "status": "UNVERIFIED",
                    }
                ],
                "filter_rows": [],
            },
            "screenshot_evidence": {"dashboard_a": {}, "dashboard_b": {}},
        },
    )

    workbook = load_workbook(workbook_path, data_only=True)
    sheet = workbook["Visual_Validation"]
    visual_row = next(
        row
        for row in sheet.iter_rows(values_only=True)
        if row[1] == "Expired LTMO" and row[4] == "Table"
    )
    assert visual_row[7] == "Unable to Verify"
    workbook.close()


def test_workbook_keeps_exact_title_and_types_when_title_matches(
    tmp_path: Path,
) -> None:
    state = "Forecast|initial"
    visuals = compare_visual_inventories(
        "Forecast",
        [
            {
                "visual_id": "base-card",
                "visual_name": "Upcoming Holiday",
                "visual_type": "Multi-row Card",
                "x": 0,
                "y": 0,
                "width": 400,
                "height": 300,
            }
        ],
        [
            {
                "visual_id": "compare-kpi",
                "visual_name": "Upcoming Holiday",
                "visual_type": "KPI",
                "x": 0,
                "y": 0,
                "width": 400,
                "height": 300,
            }
        ],
    )
    visuals[0]["state"] = state
    workbook_path = tmp_path / "comparison.xlsx"
    base_screenshot = tmp_path / "base.png"
    compare_screenshot = tmp_path / "compare.png"
    base_screenshot.touch()
    compare_screenshot.touch()
    write_visual_validation_sheet(
        workbook_path,
        {
            "visual_filter": {"visual_rows": visuals, "filter_rows": []},
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
            },
        },
    )

    workbook = load_workbook(workbook_path, data_only=True)
    sheet = workbook["Visual_Validation"]
    section_row = next(
        row
        for row in range(1, sheet.max_row + 1)
        if sheet.cell(row, 1).value == "Visual & Functional Validation Log"
    )
    visual_row = section_row + 2
    assert sheet.cell(visual_row, 3).value == "KPI"
    assert sheet.cell(visual_row, 4).value == "Multi-row Card"
    assert sheet.cell(visual_row, 5).value == "KPI"
    assert sheet.cell(visual_row, 6).value == "KPI"
    assert sheet.cell(visual_row, 8).value == "Pass"
    assert (
        "Compare Dashboard shows KPI, whereas Base Dashboard shows Multi-row Card."
        in sheet.cell(visual_row, 7).value
    )
    workbook.close()


def test_visual_display_name_preserves_both_changed_titles() -> None:
    assert _visual_display_name(
        {
            "visual_name": "VISUAL_003",
            "visual_name_a": "In-Out Item Insights",
            "visual_name_b": "In-Out Item Insights Top Excess Items",
            "visual_id_a": "VISUAL_003",
            "visual_id_b": "VISUAL_003",
        }
    ) == (
        "Base: In-Out Item Insights; "
        "Compare: In-Out Item Insights Top Excess Items"
    )


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
    technical_title_row = sheet.max_row + 1
    checks = {
        sheet.cell(row, 3).value: row
        for row in range(1, technical_title_row)
        if str(sheet.cell(row, 1).value or "").isdigit()
    }
    visual_title_row = next(
        row
        for row in range(1, technical_title_row)
        if sheet.cell(row, 5).value == "Matrix"
    )
    assert "Data Refresh Date" not in checks
    assert sheet.cell(checks["Relative Time"], 7).value == "Fail"
    assert sheet.cell(visual_title_row, 8).value == "Pass"
    assert (
        "Matrix visual types match between both dashboards. "
        "Screenshot styling requires separate visual review."
        in sheet.cell(visual_title_row, 7).value
    )
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


def test_visual_count_summary_is_not_written_as_a_visual_check(tmp_path) -> None:
    visuals = compare_visual_inventories(
        "Overview",
        [
            {
                "visual_id": "TABLE_001",
                "visual_name": "Orders",
                "visual_type": "Table",
                "x": 0,
                "y": 0,
                "width": 400,
                "height": 300,
            }
        ],
        [
            {
                "visual_id": "TABLE_001",
                "visual_name": "Orders",
                "visual_type": "Table",
                "x": 0,
                "y": 0,
                "width": 400,
                "height": 300,
            }
        ],
    )
    visuals.append(
        {
            "page": "Overview",
            "visual_name": "-",
            "visual_id_a": "3 visuals",
            "visual_id_b": "2 visuals",
            "visual_type_a": "-",
            "visual_type_b": "-",
            "status": "INFO",
            "comparison_notes": "Visual count differs between dashboards",
        }
    )
    path = tmp_path / "comparison.xlsx"
    base_screenshot = tmp_path / "base.png"
    compare_screenshot = tmp_path / "compare.png"
    base_screenshot.touch()
    compare_screenshot.touch()
    write_visual_validation_sheet(
        path,
        {
            "visual_filter": {"visual_rows": visuals, "filter_rows": []},
            "screenshot_evidence": {
                "dashboard_a": {
                    "Overview|initial": {
                        "path": str(base_screenshot),
                        "capture_status": "captured",
                    }
                },
                "dashboard_b": {
                    "Overview|initial": {
                        "path": str(compare_screenshot),
                        "capture_status": "captured",
                    }
                },
            },
        },
    )

    workbook = load_workbook(path, data_only=True)
    sheet = workbook["Visual_Validation"]
    section_row = next(
        row
        for row in range(1, sheet.max_row + 1)
        if sheet.cell(row, 1).value == "Visual & Functional Validation Log"
    )
    checks = [
        tuple(sheet.cell(row, column).value for column in (3, 4, 5))
        for row in range(section_row + 2, sheet.max_row + 1)
        if str(sheet.cell(row, 1).value or "").isdigit()
    ]
    assert checks == [("Table", "Table", "Table")]
    workbook.close()


def test_dropdowns_are_not_written_as_visual_checks(tmp_path) -> None:
    state = "Report|initial"
    visuals = compare_visual_inventories(
        "Report",
        [
            {
                "visual_id": "TABLE_001",
                "visual_name": "Item Master Table",
                "visual_type": "Table",
                "x": 0,
                "y": 226,
                "width": 1589,
                "height": 743,
            }
        ],
        [
            {
                "visual_id": "TABLE_001",
                "visual_name": "Item Master Table",
                "visual_type": "Table",
                "x": 0,
                "y": 226,
                "width": 1589,
                "height": 743,
            }
        ],
    )
    visuals[0]["state"] = state
    visuals.append(
        {
            "state": state,
            "page": "Report",
            "visual_name": "Facility Nbr Desc",
            "visual_type_a": "Slicer / Dropdown",
            "visual_type_b": "Slicer / Dropdown",
            "status": "MATCH",
        }
    )
    base_screenshot = tmp_path / "base.png"
    compare_screenshot = tmp_path / "compare.png"
    base_screenshot.touch()
    compare_screenshot.touch()
    workbook_path = tmp_path / "comparison.xlsx"
    write_visual_validation_sheet(
        workbook_path,
        {
            "visual_filter": {"visual_rows": visuals, "filter_rows": []},
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
            },
        },
    )

    workbook = load_workbook(workbook_path, data_only=True)
    sheet = workbook["Visual_Validation"]
    section_row = next(
        row
        for row in range(1, sheet.max_row + 1)
        if sheet.cell(row, 1).value == "Visual & Functional Validation Log"
    )
    checks = [
        sheet.cell(row, 5).value
        for row in range(section_row + 2, sheet.max_row + 1)
        if str(sheet.cell(row, 1).value or "").isdigit()
    ]
    assert checks == ["Table"]
    workbook.close()


def test_filter_with_unknown_types_is_unverified_and_not_reported_as_missing_visual(
    tmp_path,
) -> None:
    state = "Report|initial"
    visuals = compare_visual_inventories(
        "Report",
        [
            {
                "visual_id": "TABLE_001",
                "visual_name": "Item Master Table",
                "visual_type": "Table",
                "x": 0,
                "y": 226,
                "width": 1589,
                "height": 743,
            }
        ],
        [
            {
                "visual_id": "TABLE_001",
                "visual_name": "Item Master Table",
                "visual_type": "Table",
                "x": 0,
                "y": 226,
                "width": 1589,
                "height": 743,
            }
        ],
    )
    visuals[0]["state"] = state
    visuals.append(
        {
            "state": state,
            "page": "Report",
            "visual_name": "Facility Nbr Desc",
            "visual_type_a": "Missing",
            "visual_type_b": "Slicer / Dropdown",
            "status": "DIFFERENT",
            "comparison_notes": (
                "Visual is present in Dashboard B and no corresponding "
                "visual is present in Dashboard A"
            ),
        }
    )
    filters = [
        {
            "state": state,
            "page": "Report",
            "filter_name": "Facility Nbr Desc",
            "filter_type_a": "Unknown",
            "filter_type_b": "Unknown",
            "selected_values_a": "All",
            "selected_values_b": "All",
            "status": "UNVERIFIED",
        }
    ]
    base_screenshot = tmp_path / "base.png"
    compare_screenshot = tmp_path / "compare.png"
    base_screenshot.touch()
    compare_screenshot.touch()
    workbook_path = tmp_path / "comparison.xlsx"
    write_visual_validation_sheet(
        workbook_path,
        {
            "visual_filter": {"visual_rows": visuals, "filter_rows": filters},
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
            },
        },
    )

    workbook = load_workbook(workbook_path, data_only=True)
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
    filter_rows = [
        row
        for row in range(filter_section_row + 2, visual_section_row)
        if sheet.cell(row, 3).value == "Facility Nbr Desc"
    ]
    visual_checks = [
        sheet.cell(row, 5).value
        for row in range(visual_section_row + 2, sheet.max_row + 1)
        if str(sheet.cell(row, 1).value or "").isdigit()
    ]
    assert len(filter_rows) == 1
    assert sheet.cell(filter_rows[0], 7).value == UNVERIFIED_LABEL
    assert visual_checks == ["Table"]
    workbook.close()


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
    assert row.cells[3].text == "Facility: East;"
    assert row.cells[4].text == (
        "Orders should render as Table on both dashboards with consistent "
        "titles and presentation."
    )
    assert table.cell(2, 2).text == "Bar Chart"
    assert table.cell(2, 3).text == "Facility: East;"
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
    assert "".join(node.text or "" for node in second_result) == "Pass"


def test_blank_visual_classification_in_validation_log(tmp_path: Path) -> None:
    from Backend.visual_filter_report import (
        classify_dashboard_visual,
        _visual_check_display,
        visual_validation_log_data,
    )

    # 1. Test classify_dashboard_visual function directly
    # Blank Table
    assert classify_dashboard_visual("Table", "Blank") == "Blank Table"
    assert classify_dashboard_visual("Matrix", "Blank") == "Blank Table"
    assert classify_dashboard_visual(
        "Table",
        "Rendered",
        raw_data={"rows": [], "rows_extracted": True},
    ) == "Blank Table"
    assert classify_dashboard_visual(
        "Table",
        "Rendered",
        raw_data={"rows": []},
    ) == "Table"
    assert classify_dashboard_visual("Table", "Rendered", raw_data={"rows": [["item1"]]}) == "Table"

    # Blank KPI
    assert classify_dashboard_visual("KPI", "Blank") == "Blank KPI"
    assert classify_dashboard_visual("Card", "Blank") == "Blank KPI"
    assert classify_dashboard_visual("KPI", "Rendered", raw_data={"value": "(Blank)"}) == "Blank KPI"
    assert classify_dashboard_visual("KPI", "Rendered", raw_data={"value": "100"}) == "KPI"

    # Blank Chart
    assert classify_dashboard_visual("Bar Chart", "Blank") == "Blank Chart"
    assert classify_dashboard_visual("Line Chart", "Blank") == "Blank Chart"
    assert classify_dashboard_visual("Bar Chart", "Rendered", raw_data={"values": []}) == "Blank Chart"
    assert classify_dashboard_visual("Bar Chart", "Rendered", raw_data={"values": [10, 20]}) == "Bar Chart"
    assert classify_dashboard_visual("Treemap", "Rendered") == "Treemap"

    # Missing
    assert classify_dashboard_visual("Missing") == "Missing"
    assert classify_dashboard_visual(None) == "Missing"

    # 2. Test _visual_check_display
    assert _visual_check_display("Blank Table", "Blank Table") == "Blank Table"
    assert _visual_check_display("Table", "Blank Table") == "Table"
    assert _visual_check_display("Bar Chart", "Blank Chart") == "Bar Chart"
    assert _visual_check_display("Missing", "Table") == "Table"

    # 3. Test visual_validation_log_data output rows
    result = {
        "visual_filter": {
            "visual_rows": [
                {
                    "state": "Overview|initial",
                    "page": "Overview",
                    "visual_name": "Orders Table",
                    "visual_type_a": "Table",
                    "visual_type_b": "Table",
                    "rendering_status_a": "Blank",
                    "rendering_status_b": "Blank",
                    "status": "MATCH",
                },
                {
                    "state": "Overview|initial",
                    "page": "Overview",
                    "visual_name": "Total Sales KPI",
                    "visual_type_a": "KPI",
                    "visual_type_b": "KPI",
                    "rendering_status_a": "Blank",
                    "rendering_status_b": "Blank",
                    "status": "MATCH",
                },
                {
                    "state": "Overview|initial",
                    "page": "Overview",
                    "visual_name": "Revenue Trend",
                    "visual_type_a": "Line Chart",
                    "visual_type_b": "Line Chart",
                    "rendering_status_a": "Blank",
                    "rendering_status_b": "Blank",
                    "status": "MATCH",
                },
                {
                    "state": "Overview|initial",
                    "page": "Overview",
                    "visual_name": "Item Summary Table",
                    "visual_type_a": "Table",
                    "visual_type_b": "Table",
                    "rendering_status_a": "Rendered",
                    "rendering_status_b": "Blank",
                    "status": "DIFFERENT",
                },
                {
                    "state": "Overview|initial",
                    "page": "Overview",
                    "visual_name": "Category Distribution",
                    "visual_type_a": "Bar Chart",
                    "visual_type_b": "Bar Chart",
                    "rendering_status_a": "Rendered",
                    "rendering_status_b": "Blank",
                    "status": "DIFFERENT",
                },
                {
                    "state": "Overview|initial",
                    "page": "Overview",
                    "visual_name": "Discontinued Items",
                    "visual_type_a": "Table",
                    "visual_type_b": "Missing",
                    "rendering_status_a": "Rendered",
                    "rendering_status_b": "Missing",
                    "status": "DIFFERENT",
                },
            ],
            "filter_rows": [],
        }
    }

    rows, states = visual_validation_log_data(result)
    assert len(rows) == 5

    # Row 1: Blank Table | Blank Table -> Pass
    assert rows[0][2] == "Blank Table"  # Compare Dashboard
    assert rows[0][3] == "Blank Table"  # Base Dashboard
    assert rows[0][5] == "Table"  # Visual / Check
    assert rows[0][7] == "Pass"         # Result

    # Row 2: Blank KPI | Blank KPI -> Pass
    assert rows[1][2] == "Blank KPI"
    assert rows[1][3] == "Blank KPI"
    assert rows[1][5] == "KPI"
    assert rows[1][7] == "Pass"

    # Row 3: Blank Chart | Blank Chart -> Pass
    assert rows[2][2] == "Blank Chart"
    assert rows[2][3] == "Blank Chart"
    assert rows[2][5] == "Line Chart"
    assert rows[2][7] == "Pass"

    # Row 4: Blank Table | Table -> Fail
    assert rows[3][2] == "Blank Table"
    assert rows[3][3] == "Table"
    assert rows[3][5] == "Table"
    assert rows[3][7] == "Fail"

    # Row 5: Blank Chart | Bar Chart -> Fail
    assert rows[4][2] == "Blank Chart"
    assert rows[4][3] == "Bar Chart"
    assert rows[4][5] == "Bar Chart"
    assert rows[4][7] == "Fail"

    # Row with DOM table columns
    cols_str = "Row Selection, Item, UPC Case, UPC Unit, MDSE Dept"
    screen_a = tmp_path / "col_a.png"
    screen_b = tmp_path / "col_b.png"
    screen_a.touch()
    screen_b.touch()
    result_with_cols = {
        "visual_filter": {
            "visual_rows": [
                {
                    "state": "Report|initial",
                    "page": "Report",
                    "visual_name": "Untitled visual",
                    "visual_type_a": "Table",
                    "visual_type_b": "Table",
                    "categories_a": ["Row Selection", "Item", "UPC Case", "UPC Unit", "MDSE Dept"],
                    "categories_b": ["Row Selection", "Item", "UPC Case", "UPC Unit", "MDSE Dept"],
                    "status": "MATCH",
                }
            ],
            "filter_rows": [],
        },
        "screenshot_evidence": {
            "dashboard_a": {
                "Report|initial": {
                    "path": str(screen_a),
                    "capture_status": "captured",
                }
            },
            "dashboard_b": {
                "Report|initial": {
                    "path": str(screen_b),
                    "capture_status": "captured",
                }
            },
        },
    }
    cols_rows, _ = visual_validation_log_data(result_with_cols)
    assert len(cols_rows) == 2
    # Visual Type check row
    assert cols_rows[0][1] == "Report"
    assert cols_rows[0][2] == "Table"
    assert cols_rows[0][3] == "Table"
    assert cols_rows[0][4] == "Table"
    assert cols_rows[0][5] == "Table"
    assert cols_rows[0][7] == "Pass"
    # Column Headers check row
    assert cols_rows[1][1] == "Report"
    assert cols_rows[1][2] == f"Matrix/Table displayed; columns: {cols_str}"
    assert cols_rows[1][3] == f"Matrix/Table displayed; columns: {cols_str}"
    assert cols_rows[1][4] == "Table column headers"
    assert cols_rows[1][5] == "Table column headers"
    assert "Table column headers match between both dashboards." in cols_rows[1][6]
    assert "Evidence:" not in cols_rows[1][6]
    assert "Filter:" in cols_rows[1][6]
    assert cols_rows[1][7] == "Pass"


def test_visual_validation_log_includes_baseline_initial_state_with_scenarios(
    tmp_path: Path,
) -> None:
    from Backend.visual_filter_report import visual_validation_log_data

    initial_a = tmp_path / "initial_a.png"
    initial_b = tmp_path / "initial_b.png"
    scenario_a = tmp_path / "scenario_a.png"
    scenario_b = tmp_path / "scenario_b.png"
    for path in (initial_a, initial_b, scenario_a, scenario_b):
        path.touch()

    initial_state = "Report|initial"
    scenario_state = "Report|scenario_01|table_headers_initial:initial"
    result = {
        "visual_filter": {
            "visual_rows": [
                {
                    "state": initial_state,
                    "page": "Report",
                    "visual_name": "Initial visual",
                    "visual_type_a": "Table",
                    "visual_type_b": "Table",
                    "status": "MATCH",
                },
                {
                    "state": scenario_state,
                    "page": "Report",
                    "visual_name": "Scenario visual",
                    "visual_type_a": "Table",
                    "visual_type_b": "Table",
                    "status": "MATCH",
                },
            ],
            "filter_rows": [],
        },
        "screenshot_evidence": {
            "dashboard_a": {
                initial_state: {"path": str(initial_a), "capture_status": "captured"},
                scenario_state: {
                    "path": str(scenario_a),
                    "capture_status": "captured",
                },
            },
            "dashboard_b": {
                initial_state: {"path": str(initial_b), "capture_status": "captured"},
                scenario_state: {
                    "path": str(scenario_b),
                    "capture_status": "captured",
                },
            },
        },
    }

    rows, states = visual_validation_log_data(result)

    assert states == [initial_state, scenario_state]
    assert rows
    assert {row[1] for row in rows} == {"Report"}
    assert any("Table" in row for row in rows)


def test_visual_validation_log_uses_initial_headers_and_emits_two_rows_per_image():
    from Backend.visual_filter_report import visual_validation_log_data

    complete_headers = [
        "Facility Nbr Desc",
        "Purchase Status",
        "Item Nbr",
        "Item Nbr HS",
        "Item",
        "UPC Case",
        "UPC Unit",
        "MDSE Dept",
        "MDSE Dept Group",
        "MDSE Group",
        "MDSE Category",
        "Master Vendor",
        "Last Received Date",
        "Last Shipped Date",
        "Cases per week",
        "Inventory Total",
        "Reserve Committed",
        "Weeks of Supply",
        "On Order Total",
    ]
    partial_headers = complete_headers[:10]
    initial_state = "Report|initial"
    filtered_state = "Report|scenario_02|single_filter:facility=16"

    def comparison(state, headers, name, source):
        data = {"categories": headers, "values": [], "rows": []}
        return {
            "state": state,
            "element": name,
            "element_type_a": "table",
            "element_type_b": "table",
            "dashboard_data_a": data,
            "dashboard_data_b": data,
            "data_source_a": [source],
            "data_source_b": [source],
            "alignment_status": "sequence_aligned",
            "status": "MATCH",
        }

    result = {
        "comparisons": [
            comparison(
                initial_state,
                complete_headers,
                "initial-table",
                "powerbi_export_headers",
            ),
            comparison(initial_state, partial_headers, "repeated-initial-table", "screenshot"),
            comparison(filtered_state, partial_headers, "filtered-table", "screenshot"),
        ],
        "visual_filter": {"visual_rows": [], "filter_rows": []},
        "screenshot_evidence": {
            "dashboard_a": {
                initial_state: {},
                filtered_state: {},
            },
            "dashboard_b": {
                initial_state: {},
                filtered_state: {},
            },
        },
    }

    rows, states = visual_validation_log_data(result)

    assert len(rows) == 4
    assert states == [initial_state, initial_state, filtered_state, filtered_state]
    assert rows[0][2:4] == ["Table", "Table"]
    assert rows[2][2:4] == ["Table", "Table"]
    for row in (rows[1], rows[3]):
        assert row[4] == "Table column headers"
        assert row[2] == row[3] == (
            "Matrix/Table displayed; columns: " + ", ".join(complete_headers)
        )
        assert row[7] == "Pass"


def test_visual_validation_log_falls_back_to_initial_when_page_has_no_scenario_visuals(
    tmp_path: Path,
) -> None:
    from Backend.visual_filter_report import visual_validation_log_data

    initial_a = tmp_path / "initial_a.png"
    initial_b = tmp_path / "initial_b.png"
    initial_a.touch()
    initial_b.touch()
    initial_state = "Executive Summary|initial"
    scenario_state = "Executive Summary|scenario_02|missing_evidence"
    result = {
        "visual_filter": {
            "visual_rows": [
                {
                    "state": initial_state,
                    "page": "Executive Summary",
                    "visual_name": "Health Index",
                    "visual_type_a": "KPI",
                    "visual_type_b": "KPI",
                    "status": "MATCH",
                }
            ],
            "filter_rows": [],
        },
        "screenshot_evidence": {
            "dashboard_a": {
                initial_state: {"path": str(initial_a), "capture_status": "captured"},
                scenario_state: {"path": None, "capture_status": "unverified"},
            },
            "dashboard_b": {
                initial_state: {"path": str(initial_b), "capture_status": "captured"},
                scenario_state: {"path": None, "capture_status": "unverified"},
            },
        },
    }

    rows, states = visual_validation_log_data(result)

    assert states == [initial_state]
    assert rows
    assert rows[0][1] == "Executive Summary"


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
