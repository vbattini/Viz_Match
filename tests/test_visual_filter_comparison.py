from powerbi_dashboard_comparator.visual_filter_comparison import (
    DIFFERENT,
    INFO,
    MATCH,
    UNVERIFIED,
    classify_visual_type,
    compare_filter_configurations,
    compare_visual_inventories,
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


def test_kpi_vs_kpi_is_match() -> None:
    rows = compare_visual_inventories("Overview", single("KPI"), single("KPI"))
    assert statuses(rows) == [MATCH]


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


def test_table_vs_matrix_is_different() -> None:
    rows = compare_visual_inventories("Overview", single("Table"), single("Matrix"))
    assert statuses(rows) == [DIFFERENT]


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
    assert "no corresponding visual is present in Dashboard B" in rows[0]["comparison_notes"]
    assert INFO in statuses(rows)


def test_visual_only_in_dashboard_b_is_reported_as_added() -> None:
    rows = compare_visual_inventories("Overview", [], single("Card"))
    assert rows[0]["status"] == DIFFERENT
    assert rows[0]["visual_type_a"] == "Missing"
    assert "no corresponding visual is present in Dashboard A" in rows[0]["comparison_notes"]


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
    assert statuses(rows) == [DIFFERENT]
    assert rows[0]["visual_name"] == "Sales by Dept"
    assert "Visual title/name differs" in rows[0]["comparison_notes"]


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
    assert by_base_type["Column Chart"]["status"] == DIFFERENT
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
