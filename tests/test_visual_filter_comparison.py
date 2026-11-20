from Backend.visual_filter_comparison import (
    DIFFERENT,
    INFO,
    MATCH,
    UNVERIFIED,
    classify_visual_type,
    compare_filter_configurations,
    compare_visual_inventories,
    same_visual_type,
)


def visual(visual_id, visual_type, x, y, width, height):
    return {
        "visual_id": visual_id,
        "visual_type": visual_type,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
    }


GRID = {
    "top_left": (0, 0, 400, 300),
    "top_right": (420, 0, 400, 300),
    "bottom_left": (0, 320, 400, 300),
    "bottom_right": (420, 320, 400, 300),
}


def single(visual_type, slot="top_left", visual_id="v1"):
    return [visual(visual_id, visual_type, *GRID[slot])]


def statuses(rows):
    return [row["status"] for row in rows]


def test_table_vs_table_is_match() -> None:
    rows = compare_visual_inventories("Overview", single("Table"), single("Table"))
    assert statuses(rows) == [MATCH]


def test_hierarchical_table_headers_are_compared() -> None:
    base = {
        **visual("v1", "Table", *GRID["top_left"]),
        "categories": ["Facility", "Ordered Cases"],
        "row_headers": [{"text": "North", "level": 1}],
        "hierarchy_extraction": "sort_expand_drill_up",
    }
    compare = {
        **visual("v1", "Table", *GRID["top_left"]),
        "categories": ["Different first column", "Other measure"],
        "row_headers": [{"text": "North", "level": 1}],
        "hierarchy_extraction": "sort_expand_drill_up",
    }

    rows = compare_visual_inventories("Report", [base], [compare])

    assert statuses(rows) == [MATCH]
    assert "row headers, hierarchy levels, parent paths, row types, and order match" in rows[0]["comparison_notes"]
    assert rows[0]["row_headers_a"] == [{"text": "North", "level": 1}]


def test_hierarchical_table_header_difference_fails() -> None:
    base = {
        **visual("v1", "Table", *GRID["top_left"]),
        "categories": ["Facility"],
        "row_headers": [{"text": "North", "level": 1}],
        "hierarchy_extraction": "sort_expand_drill_up",
    }
    compare = {
        **visual("v1", "Table", *GRID["top_left"]),
        "categories": ["Facility"],
        "row_headers": [{"text": "North", "level": 2}],
        "hierarchy_extraction": "sort_expand_drill_up",
    }

    rows = compare_visual_inventories("Report", [base], [compare])

    assert statuses(rows) == [DIFFERENT]
    assert "row headers, hierarchy levels, parent paths, row types, or order differ" in rows[0]["comparison_notes"]


def test_hierarchical_export_structure_uses_parent_paths_and_row_types() -> None:
    structure = {
        "status": "PASS",
        "columns": [{"name": "Dimension A", "level": 1}],
        "depth": 2,
        "rows": [
            {
                "hierarchy_level": 1,
                "parent_path": [],
                "current_value": "North",
                "row_type": "subtotal",
            },
            {
                "hierarchy_level": 2,
                "parent_path": ["North"],
                "current_value": "Store 1",
                "row_type": "detail",
            },
        ],
    }
    base = {
        **visual("v1", "Table", *GRID["top_left"]),
        "hierarchy_extraction": "sort_expand_drill_up",
        "hierarchy_structure": structure,
    }
    compare = {
        **visual("v1", "Table", *GRID["top_left"]),
        "hierarchy_extraction": "sort_expand_drill_up",
        "hierarchy_structure": {**structure, "columns": [{"name": "Other A", "level": 1}]},
    }

    rows = compare_visual_inventories("Report", [base], [compare])

    assert statuses(rows) == [MATCH]
    assert rows[0]["hierarchy_comparison"]["row_count_a"] == 2


def test_hierarchy_structure_is_compared_without_legacy_marker() -> None:
    structure = {
        "status": "PASS",
        "columns": [{"name": "Dimension", "level": 1}],
        "depth": 1,
        "rows": [
            {
                "hierarchy_level": 1,
                "parent_path": [],
                "current_value": "North",
                "row_type": "detail",
            }
        ],
    }
    base = {
        **visual("v1", "Table", *GRID["top_left"]),
        "hierarchy_structure": structure,
    }
    compare = {
        **visual("v1", "Table", *GRID["top_left"]),
        "hierarchy_structure": {
            **structure,
            "rows": [{**structure["rows"][0], "current_value": "South"}],
        },
    }

    rows = compare_visual_inventories("Report", [base], [compare])

    assert statuses(rows) == [DIFFERENT]
    assert rows[0]["hierarchy_comparison"]["status"] == DIFFERENT
    assert len(rows[0]["hierarchy_comparison"]["different_rows"]) == 1


def test_hierarchical_export_structure_reports_missing_branch() -> None:
    base = {
        **visual("v1", "Table", *GRID["top_left"]),
        "hierarchy_extraction": "sort_expand_drill_up",
        "hierarchy_structure": {
            "status": "PASS",
            "columns": [{"name": "A", "level": 1}],
            "depth": 1,
            "rows": [
                {
                    "hierarchy_level": 1,
                    "parent_path": [],
                    "current_value": "North",
                    "row_type": "detail",
                },
                {
                    "hierarchy_level": 1,
                    "parent_path": [],
                    "current_value": "South",
                    "row_type": "detail",
                },
            ],
        },
    }
    compare = {
        **visual("v1", "Table", *GRID["top_left"]),
        "hierarchy_extraction": "sort_expand_drill_up",
        "hierarchy_structure": {
            "status": "PASS",
            "columns": [{"name": "B", "level": 1}],
            "depth": 1,
            "rows": [
                {
                    "hierarchy_level": 1,
                    "parent_path": [],
                    "current_value": "North",
                    "row_type": "detail",
                }
            ],
        },
    }

    rows = compare_visual_inventories("Report", [base], [compare])

    assert statuses(rows) == [DIFFERENT]
    assert len(rows[0]["hierarchy_comparison"]["missing_rows"]) == 1


def test_kpi_vs_kpi_is_match() -> None:
    rows = compare_visual_inventories("Overview", single("KPI"), single("KPI"))
    assert statuses(rows) == [MATCH]


def test_matching_explicitly_blank_kpis_match_the_base_dashboard() -> None:
    base = {
        **visual("fill-rate", "KPI", *GRID["top_left"]),
        "visual_name": "Fill Rate",
        "rendering_status": "Blank",
        "confidence": "High",
    }
    compare = {
        **visual("fill-rate", "KPI", *GRID["top_left"]),
        "visual_name": "Fill Rate",
        "rendering_status": "Blank",
        "confidence": "High",
    }

    rows = compare_visual_inventories("Fill Rate Trend", [base], [compare])

    assert statuses(rows) == [MATCH]
    assert "matches the Base dashboard" in rows[0]["comparison_notes"]


def test_blank_kpi_on_only_one_dashboard_remains_a_failure() -> None:
    base = {
        **visual("fill-rate", "KPI", *GRID["top_left"]),
        "visual_name": "Fill Rate",
        "rendering_status": "Blank",
        "confidence": "High",
    }
    compare = {
        **visual("fill-rate", "KPI", *GRID["top_left"]),
        "visual_name": "Fill Rate",
        "rendering_status": "Rendered",
        "confidence": "High",
    }

    rows = compare_visual_inventories("Fill Rate Trend", [base], [compare])

    assert statuses(rows) == [DIFFERENT]


def test_exact_visual_title_matches_when_type_labels_differ() -> None:
    rows = compare_visual_inventories(
        "Overview",
        [
            {
                **visual("base-1", "Multi-row Card", *GRID["top_left"]),
                "visual_name": "Upcoming Holiday",
            }
        ],
        [
            {
                **visual("compare-1", "KPI", *GRID["top_left"]),
                "visual_name": "Upcoming Holiday",
            }
        ],
    )

    assert rows[0]["visual_name"] == "Upcoming Holiday"
    assert rows[0]["visual_name_a"] == "Upcoming Holiday"
    assert rows[0]["visual_name_b"] == "Upcoming Holiday"
    assert rows[0]["visual_type_a"] == "Multi-row Card"
    assert rows[0]["visual_type_b"] == "KPI"
    assert rows[0]["status"] == MATCH
    assert "type labels differ" in rows[0]["comparison_notes"]


def test_different_titles_do_not_hide_different_visual_types() -> None:
    rows = compare_visual_inventories(
        "Overview",
        [
            {
                **visual("same-id", "Table", *GRID["top_left"]),
                "visual_name": "Orders",
            }
        ],
        [
            {
                **visual("same-id", "KPI", *GRID["top_left"]),
                "visual_name": "Scheduled This Week",
            }
        ],
    )

    assert rows[0]["status"] == DIFFERENT
    assert rows[0]["visual_name_a"] == "Orders"
    assert rows[0]["visual_name_b"] == "Scheduled This Week"


def test_bar_vs_bar_is_match() -> None:
    rows = compare_visual_inventories(
        "Overview", single("Bar Chart"), single("Bar Chart")
    )
    assert statuses(rows) == [MATCH]


def test_bar_vs_line_is_different() -> None:
    rows = compare_visual_inventories(
        "Overview", single("Bar Chart"), single("Line Chart")
    )
    assert statuses(rows) == [DIFFERENT]
    assert rows[0]["visual_type_a"] == "Bar Chart"
    assert rows[0]["visual_type_b"] == "Line Chart"


def test_bar_vs_kpi_is_different() -> None:
    rows = compare_visual_inventories("Overview", single("Bar Chart"), single("KPI"))
    assert statuses(rows) == [DIFFERENT]


def test_table_vs_matrix_is_match() -> None:
    rows = compare_visual_inventories("Overview", single("Table"), single("Matrix"))
    assert statuses(rows) == [MATCH]


def test_treemap_vs_treemap_is_match() -> None:
    rows = compare_visual_inventories("Overview", single("Treemap"), single("Treemap"))
    assert statuses(rows) == [MATCH]


def test_pie_vs_donut_is_different() -> None:
    rows = compare_visual_inventories(
        "Overview", single("Pie Chart"), single("Donut Chart")
    )
    assert statuses(rows) == [DIFFERENT]


def test_visual_only_in_dashboard_a_is_reported_as_missing() -> None:
    rows = compare_visual_inventories("Overview", single("Table"), [])
    assert rows[0]["status"] == DIFFERENT
    assert rows[0]["visual_type_b"] == "Missing"
    assert "no corresponding visual was detected in Dashboard B" in rows[0]["comparison_notes"]
    assert INFO in statuses(rows)


def test_visual_only_in_dashboard_b_is_reported_as_added() -> None:
    rows = compare_visual_inventories("Overview", [], single("Card"))
    assert rows[0]["status"] == DIFFERENT
    assert rows[0]["visual_type_a"] == "Missing"
    assert "no corresponding visual was detected in Dashboard A" in rows[0]["comparison_notes"]


def test_high_confidence_one_sided_visual_fails_with_other_unmatched_visuals() -> None:
    dashboard_a = [
        {
            **visual("base-table", "Table", *GRID["top_left"]),
            "visual_name": "All Items Table",
            "rendering_status": "Rendered",
            "confidence": "High",
        }
    ]
    dashboard_b = [
        {
            **visual("compare-matrix", "Matrix", *GRID["bottom_right"]),
            "visual_name": "Different Matrix",
            "rendering_status": "Rendered",
            "confidence": "High",
        }
    ]

    rows = compare_visual_inventories("Overview", dashboard_a, dashboard_b)

    assert [row["status"] for row in rows[:2]] == [DIFFERENT, DIFFERENT]
    assert "no corresponding visual was detected in Dashboard B" in (
        rows[0]["comparison_notes"]
    )
    assert "no corresponding visual was detected in Dashboard A" in (
        rows[1]["comparison_notes"]
    )


def test_uncertain_visual_detection_is_not_confirmed_as_missing() -> None:
    divider = {
        **visual("VISUAL_005", "Shape / Line", *GRID["top_left"]),
        "rendering_status": "Blank",
        "confidence": "Medium",
    }

    rows = compare_visual_inventories("Overview", [divider], [])

    assert rows[0]["status"] == UNVERIFIED
    assert rows[0]["visual_type_b"] == "Missing"
    assert "may be a missing, renamed, or unmatched visual" in rows[0]["comparison_notes"]


def test_same_type_with_significantly_different_layout_is_different() -> None:
    rows = compare_visual_inventories(
        "Overview",
        [visual("v1", "Table", 0, 0, 400, 300)],
        [visual("v1", "Table", 0, 0, 900, 300)],
    )
    assert statuses(rows) == [DIFFERENT]
    assert "Layout differs" in rows[0]["comparison_notes"]


def test_visuals_are_matched_by_unique_title_not_by_order() -> None:
    dashboard_a = [
        {**visual("a1", "Table", *GRID["top_left"]), "visual_name": "Orders"},
        {
            **visual("a2", "Bar Chart", *GRID["bottom_right"]),
            "visual_name": "Sales by Month",
        },
    ]
    dashboard_b = [
        {
            **visual("b2", "Bar Chart", *GRID["bottom_right"]),
            "visual_name": "Sales by Month",
        },
        {**visual("b1", "Table", *GRID["top_left"]), "visual_name": "Orders"},
    ]
    rows = compare_visual_inventories("Overview", dashboard_a, dashboard_b)
    assert statuses(rows) == [MATCH, MATCH]
    assert {(r["visual_id_a"], r["visual_id_b"]) for r in rows} == {
        ("a1", "b1"),
        ("a2", "b2"),
    }


def test_geometry_alone_does_not_pair_unidentified_visuals() -> None:
    rows = compare_visual_inventories(
        "Overview",
        single("Table", visual_id="base-table"),
        single("Table", visual_id="compare-table"),
    )

    assert len(rows) == 2
    assert [row["status"] for row in rows] == [UNVERIFIED, UNVERIFIED]
    assert all(
        "matching ID or unique title" in row["comparison_notes"]
        for row in rows
    )


def test_renamed_line_charts_pair_as_one_title_mismatch() -> None:
    rows = compare_visual_inventories(
        "Day of Week Insights",
        [
            {
                **visual("base-chart", "Line Chart", *GRID["top_left"]),
                "visual_name": "Amount by Week Ending",
            }
        ],
        [
            {
                **visual("compare-chart", "Line Chart", *GRID["top_left"]),
                "visual_name": "Paid Hours and Cases Selected by Week Ending",
            }
        ],
    )

    assert len(rows) == 1
    assert rows[0]["visual_name_a"] == "Amount by Week Ending"
    assert rows[0]["visual_name_b"] == "Paid Hours and Cases Selected by Week Ending"
    assert rows[0]["visual_type_a"] == rows[0]["visual_type_b"] == "Line Chart"
    assert rows[0]["status"] == DIFFERENT
    assert "Visual title/name differs" in rows[0]["comparison_notes"]


def test_screenshot_detected_line_chart_labels_do_not_create_missing_visuals() -> None:
    base = {
        **visual("SCREENSHOT_001", "Line Chart", *GRID["top_left"]),
        "visual_name": "Fill Rate and Vendor Adj Fill Rate by Fiscal Year",
        "rendering_status": "Rendered",
        "confidence": "High",
    }
    compare = {
        **visual("SCREENSHOT_001", "Line Chart", 8, 2, 405, 302),
        "visual_name": "Fill Rate Trend Line Chart",
        "rendering_status": "Rendered",
        "confidence": "High",
    }

    rows = compare_visual_inventories("Fill Rate Trend", [base], [compare])

    assert len(rows) == 1
    assert rows[0]["status"] == MATCH
    assert "generated visual labels differ" in rows[0]["comparison_notes"]


def test_renamed_visual_candidates_use_geometry_to_disambiguate() -> None:
    visuals_a = [
        {
            **visual("base-amount", "Line Chart", *GRID["top_left"]),
            "visual_name": "Amount by Week Ending",
        },
        {
            **visual("base-cases", "Line Chart", *GRID["bottom_right"]),
            "visual_name": "Cases by Week Ending",
        },
    ]
    visuals_b = [
        {
            **visual("compare-paid", "Line Chart", *GRID["bottom_right"]),
            "visual_name": "Paid Hours and Cases Selected by Week Ending",
        },
        {
            **visual("compare-total", "Line Chart", *GRID["top_left"]),
            "visual_name": "Total Items by Week Ending",
        },
    ]

    rows = compare_visual_inventories("Overview", visuals_a, visuals_b)

    assert {
        (row["visual_id_a"], row["visual_id_b"]) for row in rows
    } == {
        ("base-amount", "compare-total"),
        ("base-cases", "compare-paid"),
    }


def test_unidentified_visual_type_is_unverified_not_guessed() -> None:
    rows = compare_visual_inventories(
        "Overview", single(None), single("Table")
    )
    assert statuses(rows) == [UNVERIFIED]


def test_visual_count_difference_adds_info_row() -> None:
    rows = compare_visual_inventories(
        "Overview",
        [visual("a1", "Table", *GRID["top_left"])],
        [
            visual("b1", "Table", *GRID["top_left"]),
            visual("b2", "Card", *GRID["top_right"]),
        ],
    )
    assert INFO in statuses(rows)
    assert UNVERIFIED in statuses(rows)


def test_visual_rows_carry_the_four_report_fields() -> None:
    rows = compare_visual_inventories(
        "Overview",
        [visual("a1", "Table", *GRID["top_left"])],
        [visual("b1", "Table", *GRID["top_left"])],
        action_applied="Facility = East",
    )
    row = rows[0]
    assert row["page"] == "Overview"
    assert row["visual_name"] == "a1"
    assert row["action_applied"] == "Facility = East"
    assert row["expected_result"] == "Table"
    assert {
        "visual_id_a",
        "visual_id_b",
        "position_a",
        "position_b",
        "size_a",
        "size_b",
    } <= set(row)


def test_stable_id_pairs_visuals_with_different_titles() -> None:
    a = [{**visual("a1", "Table", *GRID["top_left"]), "visual_name": "Sales by Dept"}]
    b = [{**visual("a1", "Table", *GRID["top_left"]), "visual_name": "Revenue"}]
    rows = compare_visual_inventories("Overview", a, b)
    assert statuses(rows) == [MATCH]
    assert rows[0]["visual_name"] == "Sales by Dept"


def test_screenshot_ids_do_not_override_matching_titles() -> None:
    visuals_a = [
        {
            **visual("SCREENSHOT_001", "Bar Chart", *GRID["top_left"]),
            "visual_name": "Orders by Facility",
        },
        {
            **visual("SCREENSHOT_002", "Column Chart", *GRID["top_right"]),
            "visual_name": "Fill Rate",
        },
    ]
    visuals_b = [
        {
            **visual("SCREENSHOT_001", "Column Chart", *GRID["top_right"]),
            "visual_name": "Fill Rate",
        },
        {
            **visual("SCREENSHOT_002", "Bar Chart", *GRID["top_left"]),
            "visual_name": "Orders by Facility",
        },
    ]

    rows = compare_visual_inventories("Overview", visuals_a, visuals_b)

    assert {row["visual_name"] for row in rows} == {
        "Orders by Facility",
        "Fill Rate",
    }
    assert all(row["status"] == MATCH for row in rows)


def test_duplicate_visual_titles_pair_by_chart_family() -> None:
    visuals_a = [
        {
            **visual("base-column", "Column Chart", *GRID["top_left"]),
            "visual_name": "Inventory by DOH Band",
        },
        {
            **visual("base-line", "Line Chart", *GRID["bottom_right"]),
            "visual_name": "Inventory by DOH Band",
        },
    ]
    visuals_b = [
        {
            **visual(
                "compare-column",
                "Column Chart (100% stacked)",
                *GRID["top_left"],
            ),
            "visual_name": "Inventory by DOH Band",
        },
        {
            **visual("compare-line", "Line Chart", *GRID["bottom_right"]),
            "visual_name": "Inventory by DOH Band",
        },
    ]

    rows = compare_visual_inventories("Weekly Trending", visuals_a, visuals_b)

    assert len(rows) == 2
    by_base_type = {row["visual_type_a"]: row for row in rows}
    assert by_base_type["Column Chart"]["visual_type_b"] == (
        "Column Chart (100% stacked)"
    )
    assert by_base_type["Column Chart"]["status"] == MATCH
    assert "type labels differ" in by_base_type["Column Chart"]["comparison_notes"]
    assert by_base_type["Line Chart"]["visual_type_b"] == "Line Chart"
    assert by_base_type["Line Chart"]["status"] == MATCH


def test_blank_visual_is_a_failure_and_keeps_expected_type_separate() -> None:
    blank = {
        **visual("base-1", "Unidentified", *GRID["top_left"]),
        "rendering_status": "Blank",
    }
    rendered = {
        **visual("base-1", "Bar Chart", *GRID["top_left"]),
        "rendering_status": "Rendered",
    }

    rows = compare_visual_inventories("Overview", [blank], [rendered])

    assert rows[0]["status"] == DIFFERENT
    assert rows[0]["visual_type_a"] == "Unidentified"
    assert rows[0]["visual_type_b"] == "Bar Chart"
    assert rows[0]["expected_result"] == "Bar Chart"
    assert "blank on Dashboard A" in rows[0]["comparison_notes"]
    assert "Expected type inferred" in rows[0]["comparison_notes"]


def test_classify_visual_type_from_dom_hints() -> None:
    assert classify_visual_type("clusteredBarChart") == "Clustered Bar Chart"
    assert classify_visual_type("visual-lineChart") == "Line Chart"
    assert classify_visual_type("donutChart") == "Donut Chart"
    assert classify_visual_type("treemap") == "Treemap"
    assert classify_visual_type("Shape / Line") == "Shape / Line"
    assert classify_visual_type("kpi") == "KPI"
    assert classify_visual_type("unrelated-container") is None
    assert classify_visual_type(None, "") is None


def filter_item(name, control_type, options, selected):
    return {
        "name": name,
        "control_type": control_type,
        "options": options,
        "selected": selected,
    }


def test_same_filter_configuration_is_match() -> None:
    a = [filter_item("Calendar", "date", [], ["2026-01-01"])]
    b = [filter_item("Calendar", "date", [], ["2026-01-01"])]
    rows = compare_filter_configurations("Overview", a, b)
    assert statuses(rows) == [MATCH]


def test_matching_filter_selections_pass_when_control_type_is_unknown() -> None:
    a = [filter_item("Master Vendor", None, [], ["000006 - PETKING INC"])]
    b = [filter_item("Master Vendor", None, [], ["000006 - PETKING INC"])]

    rows = compare_filter_configurations("Item Counts", a, b)

    assert statuses(rows) == [MATCH]
    assert rows[0]["selected_values_a"] == "000006 - PETKING INC"
    assert rows[0]["selected_values_b"] == "000006 - PETKING INC"
    assert "Filter control type could not be identified" in rows[0]["comparison_notes"]
    assert "Filter selection successfully verified" in rows[0]["comparison_notes"]


def test_different_filter_selections_fail_when_control_type_is_unknown() -> None:
    a = [filter_item("Master Vendor", None, [], ["000006 - PETKING INC"])]
    b = [filter_item("Master Vendor", None, [], ["000007 - OTHER VENDOR"])]

    rows = compare_filter_configurations("Item Counts", a, b)

    assert statuses(rows) == [DIFFERENT]


def test_scroll_screenshots_are_skipped_for_visual_type_and_missing_visual_validation():
    from pathlib import Path
    from Backend.models import DashboardState
    from Backend.visual_filter_comparison import (
        build_visual_filter_result,
        visual_inventory,
    )

    state_scroll_a = DashboardState(
        dashboard="dashboard_A",
        page="Report",
        filter=None,
        screenshot=Path("dashboard_A_S01_table_headers_Page01_Scroll01.png"),
        extracted={"elements": [{"element_id": "TABLE_001", "type": "table", "title": "Table"}]},
        scenario_id="01",
        scenario_step="table_headers_scroll:horizontal_scroll_01",
    )
    state_scroll_b = DashboardState(
        dashboard="dashboard_B",
        page="Report",
        filter=None,
        screenshot=Path("dashboard_B_S01_table_headers_Page01_Scroll01.png"),
        extracted={"elements": []},
        scenario_id="01",
        scenario_step="table_headers_scroll:horizontal_scroll_01",
    )

    state_initial_a = DashboardState(
        dashboard="dashboard_A",
        page="Report",
        filter=None,
        screenshot=Path("dashboard_A_S01_table_headers_Page01_Initial.png"),
        extracted={"elements": [{"element_id": "TABLE_001", "type": "table", "title": "Table", "bounding_box": {"x": 0, "y": 0, "width": 100, "height": 100}}]},
        scenario_id="01",
        scenario_step="table_headers_initial:initial",
    )
    state_initial_b = DashboardState(
        dashboard="dashboard_B",
        page="Report",
        filter=None,
        screenshot=Path("dashboard_B_S01_table_headers_Page01_Initial.png"),
        extracted={"elements": [{"element_id": "TABLE_001", "type": "table", "title": "Table", "bounding_box": {"x": 0, "y": 0, "width": 100, "height": 100}}]},
        scenario_id="01",
        scenario_step="table_headers_initial:initial",
    )

    assert visual_inventory(state_scroll_a) == []
    assert visual_inventory(state_scroll_b) == []

    inv_initial = visual_inventory(state_initial_a)
    assert len(inv_initial) == 1
    assert inv_initial[0]["visual_type"] == "Table"

    res = build_visual_filter_result(
        [state_initial_a, state_scroll_a],
        [state_initial_b, state_scroll_b],
    )
    scroll_rows = [row for row in res["visual_rows"] if "Scroll" in str(row.get("state", ""))]
    assert scroll_rows == []


def test_matching_filter_selection_passes_when_only_one_control_type_is_known() -> None:
    a = [filter_item("Master Vendor", "radio_dropdown", [], ["000006 - PETKING INC"])]
    b = [filter_item("Master Vendor", None, [], ["000006 - PETKING INC"])]

    rows = compare_filter_configurations("Item Counts", a, b)

    assert statuses(rows) == [MATCH]


def test_missing_filter_selection_remains_unverified_without_control_type() -> None:
    a = [filter_item("Master Vendor", None, [], [])]
    b = [filter_item("Master Vendor", None, [], ["000006 - PETKING INC"])]

    rows = compare_filter_configurations("Item Counts", a, b)

    assert statuses(rows) == [UNVERIFIED]


def test_different_filter_type_is_different() -> None:
    a = [filter_item("Facility", "radio_dropdown", ["X", "Y"], ["X"])]
    b = [filter_item("Facility", "checkbox_dropdown", ["X", "Y"], ["X"])]
    rows = compare_filter_configurations("Overview", a, b)
    assert statuses(rows) == [DIFFERENT]
    assert rows[0]["filter_type_a"] == "Dropdown"
    assert rows[0]["filter_type_b"] == "Checkbox"


def test_single_vs_multi_select_is_different() -> None:
    a = [filter_item("Customer Type", "radio_dropdown", ["A", "B"], ["A"])]
    b = [filter_item("Customer Type", "checkbox_dropdown", ["A", "B"], ["A"])]
    rows = compare_filter_configurations("Overview", a, b)
    assert statuses(rows) == [DIFFERENT]
    assert rows[0]["selection_mode_a"] == "Single-select"
    assert rows[0]["selection_mode_b"] == "Multi-select"


def test_different_available_options_is_different() -> None:
    a = [filter_item("Region", "radio_dropdown", ["East", "West"], ["East"])]
    b = [filter_item("Region", "radio_dropdown", ["East", "North"], ["East"])]
    rows = compare_filter_configurations("Overview", a, b)
    assert statuses(rows) == [DIFFERENT]
    assert "Available options differ" in rows[0]["comparison_notes"]


def test_different_selected_values_is_different() -> None:
    a = [filter_item("Region", "radio_dropdown", ["East", "West"], ["East"])]
    b = [filter_item("Region", "radio_dropdown", ["East", "West"], ["West"])]
    rows = compare_filter_configurations("Overview", a, b)
    assert statuses(rows) == [DIFFERENT]
    assert "Selected values differ" in rows[0]["comparison_notes"]


def test_option_and_selection_order_does_not_matter() -> None:
    a = [filter_item("Region", "checkbox_dropdown", ["East", "West"], ["East", "West"])]
    b = [filter_item("Region", "checkbox_dropdown", ["West", "East"], ["West", "East"])]
    rows = compare_filter_configurations("Overview", a, b)
    assert statuses(rows) == [MATCH]


def test_all_selection_matches_every_option_selected() -> None:
    options = ["Select all", "(Blank)", "Alona Florenz", "Amanda Stromgren"]
    a = [
        filter_item(
            "Supply Planner Hierarchy",
            "checkbox_dropdown",
            options,
            ["All"],
        )
    ]
    b = [
        filter_item(
            "Supply Planner Hierarchy",
            "checkbox_dropdown",
            options,
            options,
        )
    ]

    rows = compare_filter_configurations("All Items", a, b)

    assert statuses(rows) == [MATCH]
    assert rows[0]["selected_values_a"] == "All"
    assert rows[0]["selected_values_b"] == "All"


def test_filter_missing_on_one_side_is_different() -> None:
    a = [filter_item("Calendar", "date", [], [])]
    rows = compare_filter_configurations("Overview", a, [])
    assert statuses(rows) == [DIFFERENT]
    assert "missing in Dashboard B" in rows[0]["comparison_notes"]
