from pathlib import Path
import json
from dataclasses import replace

from openpyxl import load_workbook

from powerbi_dashboard_comparator.comparison import (
    _apply_bookmark_associations,
    _bookmark_association_labels,
    _merge_metadata_label_values,
    compare_dashboards,
    merge_incremental_comparisons,
    write_comparison,
)
from powerbi_dashboard_comparator.docx_report import write_docx_report
from powerbi_dashboard_comparator.models import (
    DashboardState,
    FilterCombination,
    FilterSelection,
)


def _docx_text(path: Path) -> str:
    import docx
    from docx.oxml.ns import qn

    document = docx.Document(path)
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
                for control in cell._tc.xpath(".//w:sdt"):
                    parts.extend(
                        node.text or "" for node in control.iter(qn("w:t"))
                    )
    return "\n".join(parts)


def _assert_status_cell_style(cell, color: str) -> None:
    from docx.oxml.ns import qn

    assert cell._tc.tcPr.find(qn("w:shd")) is None
    runs = cell._tc.xpath(".//w:sdtContent//w:r")
    assert runs
    properties = runs[0].find(qn("w:rPr"))
    assert properties.find(qn("w:b")) is not None
    assert properties.find(qn("w:color")).get(qn("w:val")) == color
    assert properties.find(qn("w:sz")).get(qn("w:val")) == "28"


def _state(
    dashboard: str,
    value: object,
    filter_names: list[str] | None = None,
    filter_values: dict[str, list[str]] | None = None,
) -> DashboardState:
    return DashboardState(
        dashboard=dashboard,
        page="Sales",
        filter=FilterSelection("Year", "2025"),
        screenshot=Path("state.png"),
        extracted={
            "elements": [
                {"type": "kpi", "title": "Total Sales", "value": value},
            ],
            "title_items": [],
        },
        filter_names=filter_names or [],
        filter_values=filter_values or {},
    )


def test_comparison_normalizes_numeric_formatting() -> None:
    result = compare_dashboards([_state("A", "1,245,300")], [_state("B", 1245300)])

    assert result["summary"]["matches"] == 1
    assert result["summary"]["differences"] == 0


def test_persistent_spinner_marks_state_unverified_without_dropping_comparisons(
    tmp_path: Path,
):
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["render_unverified"] = {
        "reason": "Power BI visual loading indicators remained active.",
        "indicators": [
            {
                "visual_count": 1,
                "busy_details": {
                    "visual_loading_markers": [
                        {"class_name": "powerbi-spinner xsmall shown"}
                    ]
                },
            }
        ],
    }

    result = compare_dashboards([state_a], [state_b])

    warning_row = next(
        row
        for row in result["comparisons"]
        if row["element"] == "Visual rendering status"
    )
    warning_record = next(
        record
        for record in result["comparison_records"]
        if record["item_name"] == "Visual rendering status"
    )
    assert warning_row["status"] == "UNVERIFIED"
    assert warning_record["status"] == "UNVERIFIED"
    assert result["summary"]["matches"] == 1
    assert result["summary"]["unverified"] == 1
    assert result["screenshot_evidence"]["dashboard_a"][
        state_a.state_key
    ]["capture_status"] == "captured"
    assert result["screenshot_evidence"]["dashboard_a"][
        state_a.state_key
    ]["render_unverified"]["indicators"]
    incremental_result = merge_incremental_comparisons(
        {"Sales|initial": result},
        [state_a],
        [state_b],
    )
    assert any(
        row["element"] == "Visual rendering status"
        and row["status"] == "UNVERIFIED"
        for row in incremental_result["comparisons"]
    )

    write_docx_report(
        result, tmp_path / "comparison.docx", "Dashboard A", "Dashboard B"
    )
    from docx import Document

    visual_log_text = "\n".join(
        cell.text
        for row in Document(tmp_path / "comparison.docx").tables[2].rows
        for cell in row.cells
    )
    assert "Visual rendering status" not in visual_log_text

    write_comparison(result, tmp_path, "Dashboard A", "Dashboard B")
    workbook = load_workbook(tmp_path / "Dashboard_Comparison.xlsx", read_only=True)
    assert workbook.sheetnames == ["Visual_Validation"]
    workbook.close()


def test_query_resource_error_is_reported_as_unverified() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["render_unverified"] = {
        "reason": "Power BI query exceeded available resources",
        "errors": ["query_resource_exceeded"],
        "indicators": [],
    }

    result = compare_dashboards([state_a], [state_b])
    warning = next(
        row
        for row in result["comparisons"]
        if row["element"] == "Visual rendering status"
    )

    assert warning["status"] == "UNVERIFIED"
    assert warning["dashboard_a"] == (
        "UNVERIFIED: Power BI query exceeded available resources"
    )
    assert warning["data_source_a"] == ["browser_render_error"]
    assert "Power BI query exceeded available resources" in (
        warning["comparison_note"]
    )


def test_filter_combination_has_a_distinct_serializable_state_key() -> None:
    combination = FilterCombination(
        (
            FilterSelection("Year", "2025"),
            FilterSelection("Region", "West"),
        )
    )
    state = DashboardState(
        dashboard="A",
        page="Sales",
        filter=combination,
        screenshot=Path("state.png"),
        extracted={},
    )

    assert state.state_key == "Sales|Year=2025; Region=West"
    assert state.to_dict()["filter"] == {
        "selections": [
            {"name": "Year", "value": "2025"},
            {"name": "Region", "value": "West"},
        ]
    }


def test_filters_only_comparison_omits_visual_unverified_rows() -> None:
    def filters_state(dashboard: str, selected_year: str) -> DashboardState:
        return DashboardState(
            dashboard=dashboard,
            page="Sales",
            filter=None,
            screenshot=Path("state.png"),
            extracted={
                "capture_mode": "filters_only",
                "elements": [],
                "title_items": [],
            },
            filter_names=["Year"],
            filter_values={"Year": [selected_year]},
        )

    result = compare_dashboards(
        [filters_state("A", "2025")],
        [filters_state("B", "2024")],
    )

    rows = result["comparisons"]
    assert [row["element"] for row in rows] == ["Year"]
    assert rows[0]["status"] == "DIFFERENT"


def test_layout_and_filter_capture_omits_visual_value_placeholders() -> None:
    def metadata_state(dashboard: str) -> DashboardState:
        return DashboardState(
            dashboard=dashboard,
            page="Sales",
            filter=None,
            screenshot=Path("state.png"),
            extracted={
                "capture_mode": "visual_layout_filters",
                "elements": [],
                "title_items": [],
            },
            visuals=[
                {
                    "visual_id": "VISUAL_001",
                    "visual_name": "Orders",
                    "visual_type": "Bar Chart",
                    "x": 0.1,
                    "y": 0.1,
                    "width": 0.4,
                    "height": 0.4,
                }
            ],
        )

    result = compare_dashboards(
        [metadata_state("A")],
        [metadata_state("B")],
    )

    assert not any(
        row["element"] == "(no comparable visual elements)"
        for row in result["comparisons"]
    )
    assert result["visual_filter"]["visual_rows"][0]["status"] == "MATCH"


def test_docx_report_truncates_large_values_without_mutating_result(
    tmp_path: Path,
) -> None:
    result = compare_dashboards([_state("A", "baseline")], [_state("B", "test")])
    large_value = {"rows": [["cell " * 10_000]]}
    result["comparisons"][0]["dashboard_data_a"] = large_value

    write_docx_report(
        result, tmp_path / "comparison.docx", "A" * 10_000, "B", tester_name="Vijay Battini"
    )

    assert (tmp_path / "comparison.docx").read_bytes().startswith(b"PK")
    assert "Vijay Battini" in _docx_text(tmp_path / "comparison.docx")
    assert len(_docx_text(tmp_path / "comparison.docx")) < 100_000
    assert result["comparisons"][0]["dashboard_data_a"] == large_value


def test_comparison_docx_keeps_tester_name_dropdown_and_default(
    tmp_path: Path,
) -> None:
    from docx import Document
    from docx.oxml.ns import qn

    result = compare_dashboards([_state("A", 100)], [_state("B", 100)])
    output = tmp_path / "comparison.docx"
    write_docx_report(result, output, "Dashboard A", "Dashboard B")

    document = Document(output)
    for cell in (
        document.tables[0].cell(4, 1),
        document.tables[3].cell(1, 1),
    ):
        control = cell._tc.xpath(".//w:sdt")[0]
        content = control.find(qn("w:sdtContent"))
        dropdown = control.find(qn("w:sdtPr")).find(qn("w:dropDownList"))
        displayed_value = "".join(
            node.text or "" for node in content.iter(qn("w:t"))
        )
        options = {
            item.get(qn("w:value"))
            for item in dropdown.findall(qn("w:listItem"))
        }

        assert displayed_value == "Vijay Battini"
        assert "Vijay Battini" in options
        assert "Battini Vijay" in options
        outside_text = "".join(
            node.text or ""
            for node in cell._tc.xpath(".//w:t[not(ancestor::w:sdt)]")
        )
        assert not outside_text.strip()

    for cell in (
        document.tables[0].cell(6, 1),
        document.tables[3].cell(0, 1),
    ):
        control = cell._tc.xpath(".//w:sdt")[0]
        content = control.find(qn("w:sdtContent"))
        assert "".join(
            node.text or "" for node in content.iter(qn("w:t"))
        ) == "Pass"
        _assert_status_cell_style(cell, "008000")


def test_comparison_docx_formats_filter_validation_log_and_result_dropdowns(
    tmp_path: Path,
) -> None:
    import docx
    from docx.oxml.ns import qn

    dashboard_a = [
        replace(_state("A", 100), page="Department", filter=None),
        replace(_state("A", 200), page="Private Label", filter=None),
    ]
    dashboard_b = [
        replace(_state("B", 100), page="Department", filter=None),
        replace(_state("B", 201), page="Private Label", filter=None),
    ]
    result = compare_dashboards(dashboard_a, dashboard_b)
    result["visual_filter"] = {
        "filter_rows": [
            {
                "page": "Department",
                "state": dashboard_a[0].state_key,
                "filter_name": "Calendar",
                "selected_values_a": "All",
                "selected_values_b": "All",
                "status": "UNVERIFIED",
                "comparison_notes": "Filter control type could not be identified.",
                "aligned_position": 1,
            },
            {
                "page": "Department",
                "state": dashboard_a[0].state_key,
                "filter_name": "Facility",
                "selected_values_a": "North",
                "selected_values_b": "South",
                "status": "UNVERIFIED",
                "comparison_notes": "Filter control type could not be identified.",
                "aligned_position": 2,
            },
            {
                "page": "Private Label",
                "state": dashboard_a[1].state_key,
                "filter_name": "Calendar",
                "selected_values_a": "All",
                "selected_values_b": "All",
                "status": "UNVERIFIED",
                "comparison_notes": "Filter control type could not be identified.",
                "aligned_position": 1,
            },
        ],
        "visual_rows": [
            {
                "page": "Department",
                "state": dashboard_a[0].state_key,
                "visual_name": "Sales KPI",
                "comparison_notes": "Both dashboards show the same KPI visual.",
            },
            {
                "page": "Private Label",
                "state": dashboard_a[1].state_key,
                "visual_name": "Sales KPI",
                "comparison_notes": "A KPI card is visible on both dashboards.",
            },
        ],
    }
    write_comparison(result, tmp_path, "Dashboard A", "Dashboard B")
    output = tmp_path / "Dashboard_Comparison.docx"

    document = docx.Document(output)
    filter_log = document.tables[1]
    assert [filter_log.cell(row, 1).text for row in (1, 2)] == [
        "Department",
        "Private Label",
    ]
    assert len(filter_log.rows) == 3
    assert [filter_log.cell(row, 2).text for row in (1, 2)] == [
        "Calendar: All\nFacility: Snowflake: North; GCP: South",
        "Calendar: All",
    ]
    assert [filter_log.cell(row, 3).text for row in (1, 2)] == [
        "Filters are applied and visuals are working as expected.",
        "Filters are applied and visuals are working as expected.",
    ]
    assert filter_log.cell(0, 4).text == (
        "Result (Pass/Fail/Unable to Verify)"
    )

    for row_index, expected_status in ((1, "Fail"), (2, "Pass")):
        control = filter_log.cell(row_index, 4)._tc.xpath(".//w:sdt")[0]
        properties = control.find(qn("w:sdtPr"))
        dropdown = properties.find(qn("w:dropDownList"))
        assert properties.find(qn("w:alias")).get(qn("w:val")) == (
            "Pass / Fail / Unable to Verify"
        )
        assert [
            item.get(qn("w:displayText"))
            for item in dropdown.findall(qn("w:listItem"))
        ] == ["Pass", "Fail", "Unable to Verify"]
        assert dropdown.get(qn("w:lastValue")) == expected_status
        assert "".join(
            node.text or ""
            for node in control.find(qn("w:sdtContent")).iter(qn("w:t"))
        ) == expected_status
        _assert_status_cell_style(
            filter_log.cell(row_index, 4),
            "C00000" if expected_status == "Fail" else "008000",
        )
    workbook = load_workbook(
        tmp_path / "Dashboard_Comparison.xlsx",
        data_only=True,
        read_only=False,
    )
    worksheet = workbook["Visual_Validation"]
    excel_filter_results = {
        (worksheet.cell(row, 2).value, worksheet.cell(row, 3).value):
        worksheet.cell(row, 7).value
        for row in range(4, worksheet.max_row + 1)
        if worksheet.cell(row, 3).value in {"Calendar", "Facility"}
    }
    assert excel_filter_results == {
        ("Department", "Calendar"): "Pass",
        ("Department", "Facility"): "Fail",
        ("Private Label", "Calendar"): "Pass",
    }
    assert "Base North; Compare South." in next(
        str(worksheet.cell(row, 6).value)
        for row in range(4, worksheet.max_row + 1)
        if worksheet.cell(row, 2).value == "Department"
        and worksheet.cell(row, 3).value == "Facility"
    )
    for (page, filter_name), expected_status in excel_filter_results.items():
        row = next(
            row
            for row in range(4, worksheet.max_row + 1)
            if worksheet.cell(row, 2).value == page
            and worksheet.cell(row, 3).value == filter_name
        )
        cell = worksheet.cell(row, 7)
        assert cell.fill.fill_type is None
        assert cell.font.size == 14 and cell.font.bold
        assert cell.font.color.rgb[-6:] == (
            "C00000" if expected_status == "Fail" else "008000"
        )
    assert len(worksheet.conditional_formatting) > 0
    workbook.close()
    text = _docx_text(output)
    assert "\u2705" not in text and "\u274c" not in text


def test_comparison_docx_populates_only_template_sections(
    tmp_path: Path,
) -> None:
    import docx
    from docx.oxml.ns import qn

    template_path = Path(__file__).parents[1] / "Report Validation Template.docx"
    template = docx.Document(template_path)
    template_header_style = template.tables[1].cell(0, 0)._tc.tcPr.xml
    result = compare_dashboards([_state("A", "Snowflake")], [_state("B", "GCP")])
    state = result["comparisons"][0]["state"]
    result["screenshot_evidence"] = {
        "dashboard_a": {
            state: {
                "report_title": "Snowflake Sales",
                "semantic_model": "Snowflake Model",
                "workspace": "Snowflake Workspace",
                "capture_status": "captured",
            }
        },
        "dashboard_b": {
            state: {
                "report_title": "GCP Sales",
                "semantic_model": "GCP Model",
                "workspace": "GCP Workspace",
                "capture_status": "captured",
            }
        },
    }
    output = tmp_path / "comparison.docx"

    write_docx_report(
        result, output, "Dashboard A", "Dashboard B", report_date="04-Nov-2026"
    )

    document = docx.Document(output)
    text = _docx_text(output)
    assert document.paragraphs[0].text == "Snowflake Sales"
    assert document.tables[0].cell(0, 1).text == "Snowflake Sales"
    assert document.tables[0].cell(1, 1).text == "Snowflake Sales"
    assert document.tables[0].cell(2, 1).text == "Snowflake Sales"
    assert document.tables[0].cell(3, 1).text == "GCP Sales"
    assert "Snowflake vs. GCP Power BI Report Validation" in text
    assert "BigQuery" not in text
    assert "GCP Sales" in text
    assert "Snowflake Model" not in text and "GCP Model" not in text
    assert "Snowflake Workspace" not in text and "GCP Workspace" not in text
    assert "5. Filter / Slicer Value Comparison" not in text
    assert "6. Screenshot Evidence" not in text
    assert "7. Data Export Validation Log" not in text
    assert document.tables[1].cell(0, 0)._tc.tcPr.xml == template_header_style
    assert len(document.tables) == 4
    assert len(document.inline_shapes) == 0
    assert "Current screenshots are not available" in text
    filter_log = document.tables[1]
    assert filter_log.cell(1, 2).text == "No filter selections captured."
    assert filter_log.cell(1, 3).text == (
        "Filters are applied and visuals are working as expected."
    )
    filter_status_control = filter_log.cell(1, 4)._tc.xpath(".//w:sdt")[0]
    assert "".join(
        node.text or ""
        for node in filter_status_control.find(
            qn("w:sdtContent")
        ).iter(qn("w:t"))
    ) == "Unable to Verify"
    signoff = document.tables[3]
    assert signoff.cell(0, 0).text == "Result Status Dropdown (Pass/Fail)"
    status_control = signoff.cell(0, 1)._tc.xpath(".//w:sdt")[0]
    properties = status_control.find(qn("w:sdtPr"))
    dropdown = properties.find(qn("w:dropDownList"))
    assert [
        node.text for node in status_control.find(qn("w:sdtContent")).iter(qn("w:t"))
    ] == ["Fail"]
    assert properties.find(qn("w:alias")).get(qn("w:val")) == (
        "Result Status Dropdown (Pass/Fail)"
    )
    assert [
        item.get(qn("w:displayText"))
        for item in dropdown.findall(qn("w:listItem"))
    ] == ["Pass", "Fail"]
    for cell in (document.tables[0].cell(6, 1), signoff.cell(0, 1)):
        control = cell._tc.xpath(".//w:sdt")[0]
        assert "".join(
            node.text or ""
            for node in control.find(qn("w:sdtContent")).iter(qn("w:t"))
        ) == "Fail"
        _assert_status_cell_style(cell, "C00000")
    assert "☐" not in text and "☑" not in text


def test_comparison_docx_replaces_template_screenshots_with_current_captures(
    tmp_path: Path,
) -> None:
    import hashlib
    from collections import Counter

    import docx
    from PIL import Image

    result = compare_dashboards([_state("A", 100)], [_state("B", 100)])
    state = "Sales|Year=2025"
    screenshot_a = tmp_path / "current-a.png"
    screenshot_b = tmp_path / "current-b.png"
    Image.new("RGB", (320, 180), "red").save(screenshot_a)
    Image.new("RGB", (320, 180), "blue").save(screenshot_b)
    result["screenshot_evidence"] = {
        "dashboard_a": {
            state: {
                "path": str(screenshot_a),
                "capture_status": "captured",
                "image_width": 320,
                "image_height": 180,
            }
        },
        "dashboard_b": {
            state: {
                "path": str(screenshot_b),
                "capture_status": "captured",
                "image_width": 320,
                "image_height": 180,
            }
        },
    }
    output = tmp_path / "comparison.docx"

    write_docx_report(result, output, "Dashboard A", "Dashboard B")

    document = docx.Document(output)
    embedded_hashes = []
    for shape in document.inline_shapes:
        relationship_id = shape._inline.graphic.graphicData.pic.blipFill.blip.embed
        image_bytes = document.part.related_parts[relationship_id].blob
        embedded_hashes.append(hashlib.sha256(image_bytes).hexdigest())
    expected_hashes = [
        hashlib.sha256(screenshot.read_bytes()).hexdigest()
        for screenshot in (screenshot_a, screenshot_b)
    ]

    assert len(embedded_hashes) == 4
    assert Counter(embedded_hashes) == Counter(
        [expected_hashes[0]] * 2 + [expected_hashes[1]] * 2
    )


def test_comparison_docx_includes_screenshots_for_every_page(
    tmp_path: Path,
) -> None:
    import hashlib
    import docx
    from docx.oxml.ns import qn
    from docx.shared import Inches
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    from PIL import Image

    screenshot_paths = {
        "Sales": (tmp_path / "sales-a.png", tmp_path / "sales-b.png"),
        "Inventory": (tmp_path / "inventory-a.png", tmp_path / "inventory-b.png"),
    }
    colors = {
        "Sales": ("red", "blue"),
        "Inventory": ("green", "yellow"),
    }
    evidence_a = {}
    evidence_b = {}
    for page, paths in screenshot_paths.items():
        for path, color in zip(paths, colors[page]):
            Image.new("RGB", (320, 180), color).save(path)
        state = f"{page}|Default State"
        evidence_a[state] = {
            "path": str(paths[0]),
            "capture_status": "captured",
            "image_width": 320,
            "image_height": 180,
        }
        evidence_b[state] = {
            "path": str(paths[1]),
            "capture_status": "captured",
            "image_width": 320,
            "image_height": 180,
        }

    result = compare_dashboards([_state("A", 100)], [_state("B", 100)])
    result["screenshot_evidence"] = {
        "dashboard_a": evidence_a,
        "dashboard_b": evidence_b,
    }
    output = tmp_path / "comparison.docx"

    write_docx_report(result, output, "Snowflake", "GCP")

    document = docx.Document(output)
    embedded_hashes = {
        hashlib.sha256(
            document.part.related_parts[
                shape._inline.graphic.graphicData.pic.blipFill.blip.embed
            ].blob
        ).hexdigest()
        for shape in document.inline_shapes
    }
    expected_hashes = {
        hashlib.sha256(path.read_bytes()).hexdigest()
        for paths in screenshot_paths.values()
        for path in paths
    }

    assert embedded_hashes == expected_hashes
    blocks = []
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            blocks.append(("paragraph", Paragraph(child, document)))
        elif child.tag == qn("w:tbl"):
            blocks.append(("table", Table(child, document)))

    def section_content(heading: str, next_heading: str):
        start = next(
            index
            for index, (kind, block) in enumerate(blocks)
            if kind == "paragraph" and block.text.strip() == heading
        )
        end = next(
            index
            for index in range(start + 1, len(blocks))
            if blocks[index][0] == "paragraph"
            and blocks[index][1].text.strip() == next_heading
        )
        return blocks[start + 1 : end]

    sections = (
        (
            "2.1 Page-by-Page Screenshots (Snowflake vs. GCP)",
            "3. Visual & Functional Validation Log",
        ),
        (
            "3.1 Page-by-Page Screenshots (Snowflake vs. GCP)",
            "4. Summary & Sign-off",
        ),
    )
    for heading, next_heading in sections:
        content = section_content(heading, next_heading)
        captions = [
            block.text.strip()
            for kind, block in content
            if kind == "paragraph" and block.text.startswith("Page/Tab Name:")
        ]
        assert captions == ["Page/Tab Name: Sales", "Page/Tab Name: Inventory"]
        for index, (kind, block) in enumerate(content):
            if kind != "paragraph" or not block.text.startswith("Page/Tab Name:"):
                continue
            snowflake_label = content[index + 1][1]
            snowflake_image = content[index + 2][1]
            gcp_label = content[index + 3][1]
            gcp_image = content[index + 4][1]
            assert [snowflake_label.text, gcp_label.text] == ["Snowflake", "GCP"]
            assert len(snowflake_image._p.xpath(".//w:drawing")) == 1
            assert len(gcp_image._p.xpath(".//w:drawing")) == 1
            assert all(
                kind == "paragraph"
                for kind, _block in content[index + 1 : index + 5]
            )

    assert all(
        shape.width <= Inches(6.0) and shape.height <= Inches(4.5)
        for shape in document.inline_shapes
    )

    summary_index = next(
        index
        for index, (kind, block) in enumerate(blocks)
        if kind == "paragraph" and block.text.strip() == "4. Summary & Sign-off"
    )
    assert not any(
        kind == "table" and block._tbl.xpath(".//w:drawing")
        for kind, block in blocks[summary_index + 1 :]
    )
    assert "Dashboard Page Screenshots" not in _docx_text(output)


def test_comparison_docx_omits_slicer_comparison_appendix(
    tmp_path: Path,
) -> None:
    import docx

    result = compare_dashboards([_state("A", 100)], [_state("B", 100)])
    result["title_comparisons"] = [
        {
            "state": "Sales|initial",
            "standard_element_name_a": "slicer_title",
            "standard_element_name_b": "slicer_title",
            "associated_visual_a": "Calendar",
            "associated_visual_b": "Calendar",
            "element_text_a": "Calendar",
            "element_text_b": "Calendar",
            "dashboard_a": "Calendar",
            "dashboard_b": "Calendar",
            "status": "MATCH",
            "title_status": "unchanged",
        },
        {
            "state": "Sales|initial",
            "standard_element_name_a": "slicer_value",
            "standard_element_name_b": "slicer_value",
            "associated_visual_a": "Calendar",
            "associated_visual_b": "Calendar",
            "element_text_a": "All",
            "element_text_b": "All",
            "dashboard_a": "All",
            "dashboard_b": "All",
            "status": "MATCH",
            "title_status": "unchanged",
        },
        {
            "state": "Sales|initial",
            "standard_element_name_a": "slicer_value",
            "standard_element_name_b": "slicer_value",
            "associated_visual_a": "Relative Time",
            "associated_visual_b": "Relative Time",
            "element_text_a": "All",
            "element_text_b": None,
            "dashboard_a": "All",
            "dashboard_b": "not extracted",
            "status": "UNVERIFIED",
            "title_status": "extraction_uncertain",
        },
    ]
    output = tmp_path / "comparison.docx"

    write_docx_report(result, output, "Dashboard A", "Dashboard B")

    document = docx.Document(output)
    text = _docx_text(output)
    assert len(document.tables) == 4
    assert "Filter / Slicer Value Comparison" not in text
    assert "Associated Visual A" not in text
    assert "No filter selections captured." in text


def test_comparison_normalizes_currency_zero() -> None:
    result = compare_dashboards([_state("A", 0)], [_state("B", "$0")])

    assert result["summary"]["matches"] == 1
    assert result["summary"]["differences"] == 0


def test_export_numbers_match_within_relative_tolerance() -> None:
    from powerbi_dashboard_comparator.comparison import _export_values_match

    assert _export_values_match(0.302050664132327, 0.302050664132328, "Average Time")
    assert not _export_values_match(0.3020506, 0.3020607, "Average Time")
    assert not _export_values_match("abc", "abd", "Name")
    assert _export_values_match("1,245.5", 1245.5, "Amount")


def test_equal_dashboard_values_match_for_each_aligned_filter_state() -> None:
    states_a = [
        _state("A", 100),
        replace(_state("A", 120), filter=FilterSelection("Year", "2026")),
    ]
    states_b = [
        _state("B", 100),
        replace(_state("B", 120), filter=FilterSelection("Year", "2026")),
    ]

    result = compare_dashboards(states_a, states_b)

    assert [row["status"] for row in result["comparisons"]] == [
        "MATCH",
        "MATCH",
    ]
    assert result["summary"]["matches"] == 2
    assert result["summary"]["differences"] == 0


def test_unavailable_filter_state_is_unverified_without_screenshot_evidence() -> None:
    unavailable = _state("A", 100)
    unavailable.extracted.clear()
    unavailable.extracted.update(
        {
            "capture_status": "unverified",
            "capture_error": (
                "Option 'Benefits' is unavailable for filter 'Service Team'"
            ),
        }
    )

    result = compare_dashboards([unavailable], [_state("B", 100)])

    row = result["comparisons"][0]
    assert row["status"] == "UNVERIFIED"
    assert row["alignment_status"] == "capture_unavailable"
    assert "Benefits" in row["capture_error_a"]
    assert result["summary"]["matches"] == 0
    assert result["summary"]["differences"] == 0
    assert result["summary"]["unverified"] == 1
    assert result["kpis"]["unverified"] == 1
    assert unavailable.to_dict()["screenshot"] is None
    assert result["screenshot_evidence"]["dashboard_a"]["Sales|Year=2025"][
        "path"
    ] is None


def test_both_unavailable_filter_states_remain_unverified() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    for state in (state_a, state_b):
        state.extracted.clear()
        state.extracted.update(
            {
                "capture_status": "unverified",
                "capture_error": "Requested filter option was unavailable",
            }
        )

    result = compare_dashboards([state_a], [state_b])

    assert len(result["comparisons"]) == 1
    assert result["comparisons"][0]["status"] == "UNVERIFIED"
    assert result["comparisons"][0]["capture_error_a"]
    assert result["comparisons"][0]["capture_error_b"]
    assert result["screenshot_evidence"]["dashboard_a"]["Sales|Year=2025"][
        "path"
    ] is None
    assert result["screenshot_evidence"]["dashboard_b"]["Sales|Year=2025"][
        "path"
    ] is None


def test_incremental_results_merge_to_the_full_comparison() -> None:
    states_a = [
        _state("A", 100),
        replace(_state("A", 120), filter=FilterSelection("Year", "2026")),
    ]
    states_b = [
        _state("B", 100),
        replace(_state("B", 140), filter=FilterSelection("Year", "2027")),
    ]
    by_key_a = {state.state_key.casefold(): state for state in states_a}
    by_key_b = {state.state_key.casefold(): state for state in states_b}
    partial_results = {
        key: compare_dashboards(
            [by_key_a[key]] if key in by_key_a else [],
            [by_key_b[key]] if key in by_key_b else [],
        )
        for key in sorted(set(by_key_a) | set(by_key_b))
    }

    assert merge_incremental_comparisons(
        partial_results, states_a, states_b
    ) == compare_dashboards(states_a, states_b)


def test_comparison_reports_changed_values() -> None:
    result = compare_dashboards([_state("A", 100)], [_state("B", 120)])

    assert result["summary"]["differences"] == 1
    assert result["comparisons"][0]["status"] == "DIFFERENT"


def test_canonical_records_are_unique_and_drive_consistent_kpis() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["elements"].append(
        {"type": "kpi", "title": "Open Cases", "value": 5}
    )
    state_b.extracted["elements"].append(
        {"type": "kpi", "title": "Open Cases", "value": 8}
    )
    for state, name, refresh in (
        (state_a, "Trendence", "2026-01-01"),
        (state_b, "SpartanNash", "2026-01-02"),
    ):
        state.extracted["dashboard_metadata"] = {
            "report_title": name,
            "refresh_timestamp": refresh,
        }
        state.extracted["title_items"] = [
            {
                "title_id": "TITLE_001",
                "text": "Sales overview",
                "category": "dashboard_title",
                "reading_order": 1,
            }
        ]
        state.extracted["exported_data"] = [
            {
                "type": "table",
                "title": "Daily Sales",
                "categories": ["Region", "Sales"],
                "rows": [["East", 100]],
                "data_sources": ["summarized_export"],
            }
        ]

    result = compare_dashboards([state_a], [state_b])
    records = result["comparison_records"]
    kpis = result["kpis"]

    assert result["schema_version"] == "2.0"
    assert len({record["comparison_id"] for record in records}) == len(records)
    assert kpis["total_items"] == len(records)
    assert kpis["matches"] == sum(record["status"] == "MATCH" for record in records)
    assert kpis["differences"] == sum(
        record["status"] in {"DIFFERENT", "MISSING_IN_A", "MISSING_IN_B"}
        for record in records
    )
    assert kpis["source_names"] == {"a": "Trendence", "b": "SpartanNash"}
    assert {record["record_type"] for record in records} >= {
        "visual",
        "title",
        "metadata",
        "exported_data",
    }
    assert all(record["ai_confidence"] is None for record in records)
    assert kpis["verified_agreement_rate_percent"] == round(
        kpis["matches"] * 100 / kpis["verified_items"], 2
    )


def test_summary_tab_percentages_cover_screenshot_and_exported_data(
    tmp_path: Path,
) -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    for state in (state_a, state_b):
        state.extracted["title_items"] = [
            {"text": "Sales trend", "category": "chart_title", "reading_order": 1}
        ]
    state_a.extracted["elements"].append(
        {"type": "kpi", "title": "Open Cases", "value": 5}
    )
    state_b.extracted["elements"].append(
        {"type": "kpi", "title": "Open Cases", "value": 8}
    )
    for state, values in ((state_a, [100, 200]), (state_b, [100, 300])):
        state.extracted["exported_data"] = [
            {
                "type": "table",
                "title": "Exported totals",
                "categories": ["Value"],
                "rows": [[value] for value in values],
                "data_sources": ["summarized_export"],
            }
        ]
        state.extracted["export_options"] = [
            {
                "title": "Empty export",
                "exportOptionAvailable": True,
                "export_status": "downloaded_no_tabular_data",
            }
        ]

    result = compare_dashboards([state_a], [state_b])
    summary = result["summary"]

    assert summary["matches"] == 1
    assert summary["differences"] == 1
    assert summary["exported_data_matches"] == 1
    assert summary["exported_data_differences"] == 1
    assert summary["exported_data_unverified"] == 1
    assert summary["verified_checks"] == 4
    assert summary["successful_checks"] == 2
    assert summary["success_percentage"] == 50.0

    write_comparison(result, tmp_path, "Dashboard A", "Dashboard B")
def test_success_percentage_is_unavailable_without_verified_checks() -> None:
    state_a = _state("A", None)
    state_b = _state("B", None)
    state_a.extracted["elements"] = []
    state_b.extracted["elements"] = []

    result = compare_dashboards([state_a], [state_b])

    assert result["summary"]["verified_checks"] == 0
    assert result["summary"]["success_percentage"] is None
    assert result["kpis"]["verified_items"] == 0
    assert result["kpis"]["verified_agreement_rate_percent"] is None
    visual_record = next(
        record for record in result["comparison_records"]
        if record["record_type"] == "visual"
    )
    assert visual_record["status"] == "UNVERIFIED"
    assert visual_record["unverified_reason"]


def test_successful_export_notifications_are_not_compared_as_dashboard_content() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    for state in (state_a, state_b):
        state.extracted["elements"].extend(
            [
                {
                    "type": "text",
                    "title": "Successful export",
                    "value": "Successful export",
                },
                {
                    "type": "text",
                    "title": "Your data was exported.",
                    "value": "Your data was exported.",
                },
            ]
        )
        state.extracted["title_items"] = [
            {"text": "Successful export", "category": "annotation_title"},
            {"text": "Your data was exported.", "category": "annotation_title"},
        ]

    result = compare_dashboards([state_a], [state_b])

    assert [row["element"] for row in result["comparisons"]] == ["Total Sales"]
    assert result["title_comparisons"] == []


def test_logos_and_buttons_are_excluded_while_labeled_icons_are_retained() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    for state in (state_a, state_b):
        state.extracted["elements"].extend(
            [
                {"type": "navigation", "title": "Home", "value": "Home"},
                {
                    "type": "navigation",
                    "title": "Extra application menu",
                    "value": "Admin",
                },
                {
                    "type": "image",
                    "title": "SpartanNash",
                    "standard_element_name": "brand_logo",
                    "value": "SpartanNash",
                },
                {"type": "button", "title": "View Report", "value": "View Report"},
            ]
        )
        state.extracted["title_items"] = [
            {"text": "Home", "category": "navigation_label"},
            {
                "text": "Extra application menu",
                "category": "application_navigation",
            },
            {
                "text": "SpartanNash",
                "category": "brand_logo",
                "is_logo": True,
            },
            {"text": "View Report", "category": "button_label"},
            {"text": "Help", "category": "icon"},
            {
                "text": "How to switch to Legacy SN Fiscal Calendar",
                "category": "annotation_title",
                "is_application_chrome": True,
            },
        ]

    result = compare_dashboards([state_a], [state_b])

    comparison_rows = {row["element"]: row for row in result["comparisons"]}
    assert set(comparison_rows) == {"Total Sales"}
    assert [row["element"] for row in result["title_comparisons"]] == [
        "Help",
        "How to switch to Legacy SN Fiscal Calendar",
    ]
    assert (
        result["title_comparisons"][1]["standardized_category_a"]
        == "Instruction / Annotation"
    )
    assert result["title_comparisons"][0]["standard_element_name_a"] == "icon"
    assert all(
        row["standard_element_name_a"] not in {"brand_logo", "button_label"}
        and row["standard_element_name_b"] not in {"brand_logo", "button_label"}
        for row in result["title_comparisons"]
    )
    assert not any(
        record["item_name"] in {"SpartanNash", "View Report"}
        for record in result["comparison_records"]
    )


def test_comparison_omits_non_data_labels_and_summarizes_exported_visuals() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    labels = [
        {"type": "text", "title": "HR Now Report", "value": "HR Now Report"},
        {"type": "navigation", "title": "Home", "value": "Home"},
        {"type": "metadata", "title": "Last refresh", "value": "10/8/2026"},
        {
            "type": "text",
            "title": "How to switch to Legacy SN Fiscal Calendar",
            "value": "How to switch to Legacy SN Fiscal Calendar",
        },
    ]
    for state, headers, row in (
        (state_a, ["Region", "Sales"], ["West", 100]),
        (state_b, ["Sales", "region"], [100, "West"]),
    ):
        state.extracted["elements"] = [
            *labels,
            {
                "type": "chart",
                "title": "Sales trend",
                "categories": headers,
                "rows": [row],
                "data_sources": ["screenshot", "summarized_export"],
                "export_status": "exported",
            },
        ]
        state.extracted["exported_data"] = [
            {
                "type": "chart",
                "title": "Sales trend",
                "categories": headers,
                "rows": [row],
                "data_sources": ["summarized_export"],
                "export_status": "exported",
            }
        ]

    result = compare_dashboards([state_a], [state_b])

    assert [row["element"] for row in result["comparisons"]] == ["Sales trend"]
    comparison = result["comparisons"][0]
    assert comparison["dashboard_a"] == "Already exported"
    assert comparison["dashboard_b"] == "Already exported"
    assert comparison["screenshot_comparison_skipped"] is True
    assert comparison["status"] == "MATCH"
    assert comparison["dashboard_data_a"]["rows"] == [["West", 100]]
    assert comparison["dashboard_data_b"]["rows"] == [[100, "West"]]
    assert {row["status"] for row in result["exported_data_comparisons"]} == {
        "MATCH"
    }


def test_untitled_elements_are_compared_at_the_same_extracted_position() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["elements"] = [
        {
            "type": "chart",
            "title": "",
            "element_id": "CHART_001",
            "value": 12,
            "categories": [],
            "values": [],
            "rows": [],
        }
    ]
    state_b.extracted["elements"] = [
        {
            "type": "chart",
            "title": "",
            "element_id": "CHART_001",
            "value": 12,
            "categories": [],
            "values": [],
            "rows": [],
        }
    ]

    result = compare_dashboards([state_a], [state_b])

    assert result["summary"]["matches"] == 1
    assert result["summary"]["differences"] == 0
    assert result["summary"]["unverified"] == 0
    assert result["comparisons"][0]["order_a"] == 1
    assert result["comparisons"][0]["order_b"] == 1


def test_text_only_elements_are_omitted_from_main_comparison() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["elements"] = [
        {
            "type": "text",
            "title": "Untitled visual",
            "element_id": "TEXT_001",
            "reading_order": 1,
            "value": "Home",
        },
        {
            "type": "text",
            "title": "Untitled visual",
            "element_id": "TEXT_002",
            "reading_order": 2,
            "value": "Summary",
        },
    ]
    state_b.extracted["elements"] = [
        {
            "type": "text",
            "title": "Untitled visual",
            "element_id": "TEXT_001",
            "reading_order": 1,
            "value": "Summary",
        },
        {
            "type": "text",
            "title": "Untitled visual",
            "element_id": "TEXT_002",
            "reading_order": 2,
            "value": "Home",
        },
    ]

    result = compare_dashboards([state_a], [state_b])

    row = result["comparisons"][0]
    assert row["element"] == "(no comparable visual elements)"
    assert row["dashboard_a"] == "no comparable visuals"
    assert row["dashboard_b"] == "no comparable visuals"
    assert row["status"] == "UNVERIFIED"


def test_title_comparison_uses_context_and_distinguishes_moves_and_renames() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["title_items"] = [
        {
            "text": "Orders",
            "category": "chart_title",
            "associated_visual": "Orders visual",
            "reading_order": 1,
            "normalized_bounding_box": {
                "x": 0.1,
                "y": 0.1,
                "width": 0.2,
                "height": 0.05,
            },
        },
        {
            "text": "Revenue",
            "category": "chart_title",
            "associated_visual": "Revenue visual",
            "reading_order": 2,
            "normalized_bounding_box": {
                "x": 0.6,
                "y": 0.1,
                "width": 0.2,
                "height": 0.05,
            },
        },
        {
            "text": "Old heading",
            "category": "section_title",
            "associated_visual": "Old section",
            "reading_order": 3,
            "normalized_bounding_box": {
                "x": 0.4,
                "y": 0.4,
                "width": 0.2,
                "height": 0.05,
            },
        },
    ]
    state_b.extracted["title_items"] = [
        {
            "text": "Orders",
            "category": "chart_title",
            "associated_visual": "Orders visual",
            "reading_order": 1,
            "normalized_bounding_box": {
                "x": 0.3,
                "y": 0.3,
                "width": 0.2,
                "height": 0.05,
            },
        },
        {
            "text": "Net Sales",
            "category": "chart_title",
            "associated_visual": "Revenue visual",
            "reading_order": 2,
            "normalized_bounding_box": {
                "x": 0.6,
                "y": 0.1,
                "width": 0.2,
                "height": 0.05,
            },
        },
        {
            "text": "Customer Volume",
            "category": "section_title",
            "associated_visual": "Different section",
            "reading_order": 3,
            "normalized_bounding_box": {
                "x": 0.4,
                "y": 0.4,
                "width": 0.2,
                "height": 0.05,
            },
        },
    ]

    result = compare_dashboards([state_a], [state_b])
    titles = result["title_comparisons"]

    assert [(row["dashboard_a"], row["dashboard_b"], row["title_status"]) for row in titles] == [
        ("Orders", "Orders", "moved"),
        ("Revenue", "Net Sales", "renamed"),
        ("Old heading", "Customer Volume", "reclassified"),
    ]
    assert titles[0]["status"] == "INFO"
    assert titles[1]["status"] == "DIFFERENT"
    assert titles[1]["match_method"] == "sequence_alignment"
    assert titles[2]["status"] == "DIFFERENT"
    assert titles[2]["order_a"] == titles[2]["order_b"] == 3
    assert result["summary"]["title_differences"] == 2
    assert result["summary"]["title_unverified"] == 0


def test_title_position_is_not_compared_using_raw_pixel_coordinates() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["title_items"] = [
        {
            "text": "Sales",
            "category": "chart_title",
            "associated_visual": "Sales",
            "bounding_box": {"x": 100, "y": 100, "width": 80, "height": 20},
        }
    ]
    state_b.extracted["title_items"] = [
        {
            "text": "Sales",
            "category": "chart_title",
            "associated_visual": "Sales",
            "bounding_box": {"x": 500, "y": 400, "width": 80, "height": 20},
        }
    ]

    result = compare_dashboards([state_a], [state_b])
    title = next(
        row for row in result["title_comparisons"]
    )

    assert title["title_status"] == "unchanged"
    assert title["position_status"] == "not_comparable"
    assert title["position_distance"] is None


def test_title_comparison_preserves_repeated_text_and_refresh_metadata(
    tmp_path: Path,
) -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["title_items"] = [
        {"text": "Time Period", "category": "slicer_title", "associated_visual": "Time Period"},
        {"text": "All", "category": "slicer_value", "associated_visual": "Time Period"},
        {"text": "Relative Time", "category": "slicer_title", "associated_visual": "Relative Time"},
        {"text": "Last 4 Weeks", "category": "slicer_value", "associated_visual": "Relative Time"},
        {"text": "E-Mail", "category": "legend_item", "associated_visual": "Cases by Segment"},
        {"text": "E-Mail", "category": "legend_item", "associated_visual": "Cases by Caller Type"},
        {"text": "Last refresh", "category": "metadata_label", "associated_visual": "Last refresh"},
        {
            "text": "10/7/2026 7:03:56 AM",
            "category": "metadata_value",
            "associated_visual": "Last refresh",
        },
    ]
    state_b.extracted["title_items"] = [
        {"text": "Time Period", "category": "slicer_title", "associated_visual": "Time Period"},
        {"text": "All", "category": "slicer_value", "associated_visual": "Time Period"},
        {"text": "Relative Time", "category": "slicer_title", "associated_visual": "Relative Time"},
        {"text": "Last 4 Weeks", "category": "slicer_value", "associated_visual": "Relative Time"},
        {"text": "E-Mail", "category": "legend_item", "associated_visual": "Cases by Segment"},
        {"text": "E-Mail", "category": "legend_item", "associated_visual": "Cases by Caller Type"},
        {"text": "Last refresh", "category": "metadata_label", "associated_visual": "Last refresh"},
        {
            "text": "10/8/2026 7:03:56 AM",
            "category": "metadata_value",
            "associated_visual": "Last refresh",
        },
    ]

    result = compare_dashboards([state_a], [state_b])
    titles = result["title_comparisons"]
    matched = {
        (
            row["standard_element_name_a"],
            row["associated_visual_a"],
            row["dashboard_a"],
        ): row
        for row in titles
        if row["element_text_a"] and row["element_text_b"]
    }

    assert matched[("slicer_value", "Time Period", "All")]["dashboard_b"] == "All"
    assert matched[("slicer_value", "Relative Time", "Last 4 Weeks")]["dashboard_b"] == "Last 4 Weeks"
    assert matched[("legend_item", "Cases by Segment", "E-Mail")]["dashboard_b"] == "E-Mail"
    assert matched[("legend_item", "Cases by Caller Type", "E-Mail")]["dashboard_b"] == "E-Mail"
    timestamp = matched[
        ("metadata_label", "Last refresh", "Last refresh 10/7/2026 7:03:56 AM")
    ]
    assert timestamp["dashboard_b"] == "Last refresh 10/8/2026 7:03:56 AM"
    assert timestamp["status"] == "DIFFERENT"
    assert [row["order_a"] for row in titles] == list(range(1, 8))
    assert [row["order_b"] for row in titles] == list(range(1, 8))

def test_metadata_label_and_value_merge_into_one_item() -> None:
    items = [
        {"text": "Other", "standard_element_name": "slicer_value"},
        {"text": "Data Refresh Date:", "standard_element_name": "metadata_label"},
        {"text": "10/8/2026 7:01:49 AM", "standard_element_name": "metadata_value"},
        {"text": "Orphan label:", "standard_element_name": "metadata_label"},
        {"text": "Done", "standard_element_name": "slicer_value"},
    ]

    merged = _merge_metadata_label_values(items)

    assert [item["text"] for item in merged] == [
        "Other",
        "Data Refresh Date: 10/8/2026 7:01:49 AM",
        "Orphan label:",
        "Done",
    ]


def test_comparison_retains_accessible_slicer_selected_values(
    tmp_path: Path,
) -> None:
    title_items = [
        {
            "text": f"Column {index}",
            "category": "column_header",
            "associated_visual": "Sales by Dept",
            "reading_order": index,
        }
        for index in range(1, 9)
    ] + [
        {
            "text": "All",
            "category": "slicer_value",
            "associated_visual": name,
            "reading_order": 11 + index,
        }
        for index, name in enumerate(("Calendar", "Facility"))
    ]
    state_a = _state(
        "A",
        100,
        filter_names=["Calendar", "Facility"],
        filter_values={"Calendar": ["All"], "Facility": ["All"]},
    )
    state_b = _state(
        "B",
        100,
        filter_names=["Calendar", "Facility"],
        filter_values={"Calendar": ["All"], "Facility": ["All"]},
    )
    state_a = replace(
        state_a,
        filter_options={"Calendar": [], "Facility": []},
        extracted={**state_a.extracted, "title_items": title_items},
    )
    state_b = replace(
        state_b,
        filter_options={"Calendar": [], "Facility": []},
        extracted={**state_b.extracted, "title_items": title_items},
    )
    result = compare_dashboards([state_a], [state_b])

    assert {
        row["associated_visual_a"]
        for row in result["title_comparisons"]
        if row["standard_element_name_a"] == "slicer_value"
    } == {"Calendar", "Facility"}


def test_same_position_reports_role_or_visual_association_differences() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["title_items"] = [
        {"text": "All", "category": "slicer_value", "associated_visual": "Time Period"},
        {"text": "E-Mail", "category": "legend_item", "associated_visual": "Cases by Segment"},
    ]
    state_b.extracted["title_items"] = [
        {"text": "All", "category": "slicer_title", "associated_visual": "Time Period"},
        {"text": "E-Mail", "category": "legend_item", "associated_visual": "Cases by Caller Type"},
    ]

    result = compare_dashboards([state_a], [state_b])

    assert len(result["title_comparisons"]) == 2
    classification_row, association_row = result["title_comparisons"]
    assert classification_row["title_status"] == "classification_changed"
    assert classification_row["status"] == "UNVERIFIED"
    assert association_row["title_status"] == "association_changed"
    assert association_row["status"] == "INFO"
    assert all(row["order_a"] == row["order_b"] for row in result["title_comparisons"])


def test_blank_associated_visual_with_matching_title_is_not_a_difference() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["title_items"] = [
        {"text": "SLA Compliance % - Email", "category": "visual_title", "associated_visual": "SLA Compliance % - Email"},
    ]
    state_b.extracted["title_items"] = [
        {"text": "SLA Compliance % - Email", "category": "visual_title", "associated_visual": ""},
    ]

    result = compare_dashboards([state_a], [state_b])
    title = result["title_comparisons"][0]

    assert title["title_status"] == "association_changed"
    assert title["status"] == "INFO"
    assert result["summary"]["title_differences"] == 0


def test_title_items_align_by_original_reading_order_without_shifting() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["title_items"] = [
        {"text": "Dashboard name", "category": "metadata_label", "reading_order": 3},
        {"text": "Time Period", "category": "slicer_title", "reading_order": 6},
        {"text": "All", "category": "slicer_value", "reading_order": 8},
        {"text": "Last refresh", "category": "metadata_label", "reading_order": 17},
    ]
    state_b.extracted["title_items"] = [
        {"text": "Dashboard name", "category": "metadata_label", "reading_order": 3},
        {"text": "Inserted label", "category": "annotation_title", "reading_order": 5},
        {"text": "Time Period", "category": "slicer_title", "reading_order": 6},
        {"text": "All", "category": "slicer_value", "reading_order": 8},
        {"text": "Last refresh", "category": "metadata_label", "reading_order": 17},
    ]

    titles = compare_dashboards([state_a], [state_b])["title_comparisons"]

    assert [(row["order_a"], row["order_b"]) for row in titles] == [
        (3, 3),
        (None, 5),
        (6, 6),
        (8, 8),
        (17, 17),
    ]
    assert titles[1]["dashboard_a"] == "not extracted"
    assert titles[1]["dashboard_b"] == "Inserted label"
    assert titles[1]["title_status"] == "added"
    assert titles[1]["status"] == "UNVERIFIED"
    assert titles[2]["dashboard_a"] == titles[2]["dashboard_b"] == "Time Period"
    assert titles[2]["status"] == "MATCH"


def test_title_sequence_alignment_keeps_items_after_an_insertion_aligned() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["title_items"] = [
        {
            "text": "Phone Call",
            "category": "slicer_value",
            "associated_visual": "Case Type",
            "reading_order": 1,
            "normalized_bounding_box": {"x": 0.1, "y": 0.2, "width": 0.1, "height": 0.02},
        },
        {
            "text": "Workday Cases",
            "category": "slicer_value",
            "associated_visual": "Case Type",
            "reading_order": 2,
            "normalized_bounding_box": {"x": 0.1, "y": 0.24, "width": 0.1, "height": 0.02},
        },
        {"text": "Service Team", "category": "slicer_title", "reading_order": 3},
        {"text": "HR Support Center", "category": "slicer_value", "reading_order": 4},
        {"text": "Disposition Area", "category": "slicer_title", "reading_order": 5},
    ]
    state_b.extracted["title_items"] = [
        {
            "text": "Workday Cases",
            "category": "slicer_value",
            "associated_visual": "Case Type",
            "reading_order": 1,
            "normalized_bounding_box": {"x": 0.1, "y": 0.2, "width": 0.1, "height": 0.02},
        },
        {"text": "Service Team", "category": "slicer_title", "reading_order": 2},
        {"text": "HR Support Center", "category": "slicer_value", "reading_order": 3},
        {"text": "Disposition Area", "category": "slicer_title", "reading_order": 4},
    ]

    titles = compare_dashboards([state_a], [state_b])["title_comparisons"]

    assert [
        (row["dashboard_a"], row["dashboard_b"], row["title_status"])
        for row in titles
    ] == [
        ("Phone Call", "not extracted", "removed"),
        ("Workday Cases", "Workday Cases", "unchanged"),
        ("Service Team", "Service Team", "unchanged"),
        ("HR Support Center", "HR Support Center", "unchanged"),
        ("Disposition Area", "Disposition Area", "unchanged"),
    ]
    assert [(row["order_a"], row["order_b"]) for row in titles] == [
        (1, None),
        (2, 1),
        (3, 2),
        (4, 3),
        (5, 4),
    ]
    assert [row["aligned_position"] for row in titles] == [1, 2, 3, 4, 5]


def test_title_sequence_alignment_uses_associations_for_repeated_values() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["title_items"] = [
        {"text": "Time Period", "category": "slicer_title", "reading_order": 1},
        {
            "text": "All",
            "category": "slicer_value",
            "associated_visual": "Time Period",
            "reading_order": 2,
        },
        {"text": "Relative Time", "category": "slicer_title", "reading_order": 3},
        {
            "text": "All",
            "category": "slicer_value",
            "associated_visual": "Relative Time",
            "reading_order": 4,
        },
    ]
    state_b.extracted["title_items"] = [
        {"text": "Time Period", "category": "slicer_title", "reading_order": 1},
        {
            "text": "All",
            "category": "slicer_value",
            "associated_visual": "Time Period",
            "reading_order": 2,
        },
        {"text": "Case Type", "category": "slicer_title", "reading_order": 3},
        {"text": "Relative Time", "category": "slicer_title", "reading_order": 4},
        {
            "text": "All",
            "category": "slicer_value",
            "associated_visual": "Relative Time",
            "reading_order": 5,
        },
    ]

    titles = compare_dashboards([state_a], [state_b])["title_comparisons"]

    assert [(row["dashboard_a"], row["dashboard_b"]) for row in titles] == [
        ("Time Period", "Time Period"),
        ("All", "All"),
        ("not extracted", "Case Type"),
        ("Relative Time", "Relative Time"),
        ("All", "All"),
    ]
    assert titles[1]["associated_visual_a"] == titles[1]["associated_visual_b"] == "Time Period"
    assert titles[4]["associated_visual_a"] == titles[4]["associated_visual_b"] == "Relative Time"


def test_report_title_and_dashboard_title_are_distinct_comparison_fields() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["title_items"] = [
        {
            "text": "HR Now Report",
            "category": "report_title",
            "reading_order": 1,
        },
        {
            "text": "HR NOW - Summary",
            "category": "dashboard_title",
            "reading_order": 2,
        },
    ]
    state_b.extracted["title_items"] = [
        {
            "text": "HR Now Report Prod",
            "category": "report_title",
            "reading_order": 1,
        },
        {
            "text": "HR NOW - Summary",
            "category": "dashboard_title",
            "reading_order": 2,
        },
    ]

    titles = compare_dashboards([state_a], [state_b])["title_comparisons"]
    report_title = next(
        row
        for row in titles
        if row["standard_element_name_a"] == "report_title"
        or row["standard_element_name_b"] == "report_title"
    )
    dashboard_title = next(
        row
        for row in titles
        if row["standard_element_name_a"] == "dashboard_title"
        or row["standard_element_name_b"] == "dashboard_title"
    )

    assert report_title["status"] == "INFO"
    assert report_title["title_status"] == "identity_changed"
    assert report_title["standard_element_name_a"] == "report_title"
    assert report_title["standard_element_name_b"] == "report_title"
    assert dashboard_title["status"] == "MATCH"
    assert dashboard_title["standard_element_name_a"] == "dashboard_title"
    assert dashboard_title["standard_element_name_b"] == "dashboard_title"
    assert not report_title["classification_variance"]

    state_a.extracted["title_items"] = [
        {"text": "HR Now", "category": "report_title", "reading_order": 1}
    ]
    state_b.extracted["title_items"] = [
        {"text": "HR Now", "category": "dashboard_title", "reading_order": 1}
    ]
    cross_role_titles = compare_dashboards([state_a], [state_b])[
        "title_comparisons"
    ]
    assert len(cross_role_titles) == 2
    assert all(row["status"] == "UNVERIFIED" for row in cross_role_titles)
    assert all(
        not (
            row["standard_element_name_a"]
            and row["standard_element_name_b"]
        )
        for row in cross_role_titles
    )


def test_matching_slicers_are_matched_by_role_and_visual_association() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    slicers = [
        ("Time Period", "All", "slicer_title", "slicer_value"),
        ("Relative Time", "Last 4 Weeks", "slicer_title", "slicer_value"),
        ("Case Type", "All", "slicer_title", "slicer_value"),
        ("Service Team", "All", "slicer_title", "slicer_value"),
        ("Disposition Area", "All", "slicer_title", "slicer_value"),
    ]
    state_a.extracted["title_items"] = []
    state_b.extracted["title_items"] = []
    for index, (name, value, title_type, value_type) in enumerate(slicers):
        order = index * 2 + 1
        state_a.extracted["title_items"].extend(
            [
                {
                    "text": name,
                    "category": title_type,
                    "associated_visual": name,
                    "reading_order": order,
                },
                {
                    "text": value,
                    "category": value_type,
                    "associated_visual": name,
                    "reading_order": order + 1,
                },
            ]
        )
        state_b.extracted["title_items"].extend(
            [
                {
                    "text": name,
                    "category": "filter_title",
                    "associated_visual": name,
                    "reading_order": order,
                },
                {
                    "text": value,
                    "category": value_type,
                    "associated_visual": name,
                    "reading_order": order + 1,
                },
            ]
        )

    titles = compare_dashboards([state_a], [state_b])["title_comparisons"]

    assert len(titles) == 10
    assert all(row["status"] == "MATCH" for row in titles)
    assert all(row["confidence"] == "high" for row in titles)


def test_comparison_sheet_normalizes_known_type_aliases_only(
    tmp_path: Path,
) -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["title_items"] = [
        {
            "text": "Case Type",
            "category": "filter_title",
            "associated_visual": "Case Type",
            "reading_order": 1,
        },
        {
            "text": "Average Queue Time (Minutes)",
            "category": "visual_title",
            "associated_visual": "Average Queue Time (Minutes)",
            "reading_order": 2,
        },
        {
            "text": "E-Mail",
            "category": "filter_value",
            "associated_visual": "Case Type",
            "reading_order": 3,
        },
    ]
    state_b.extracted["title_items"] = [
        {
            "text": "Case Type",
            "category": "slicer_title",
            "associated_visual": "Case Type",
            "reading_order": 1,
        },
        {
            "text": "Average Queue Time (Minutes)",
            "category": "legend_item",
            "associated_visual": "Average Queue Time (Minutes)",
            "reading_order": 2,
        },
        {
            "text": "E-Mail",
            "category": "slicer_value",
            "associated_visual": "Case Type",
            "reading_order": 3,
        },
    ]

    result = compare_dashboards([state_a], [state_b])
    assert result["title_comparisons"][0]["standard_element_name_a"] == (
        "filter_title"
    )
    assert result["title_comparisons"][0]["standard_element_name_b"] == (
        "slicer_title"
    )
    assert result["title_comparisons"][0]["classification_variance"] is True
    assert result["title_comparisons"][1]["status"] == "UNVERIFIED"
    assert result["title_comparisons"][1]["title_status"] == "classification_changed"
    assert result["title_comparisons"][1]["classification_variance"] is False
    assert result["title_comparisons"][2]["classification_variance"] is True

def test_bookmark_button_items_share_group_association_on_both_sides() -> None:
    group = "Bookmark buttons: E-Mail | Phone Call | Workday Cases"
    state_a = _state("A", 100, filter_values={group: ["E-Mail"]})
    state_b = _state("B", 100, filter_values={group: ["E-Mail"]})
    labels = _bookmark_association_labels(state_a, state_b)
    items = [
        {"text": "Phone Call", "associated_visual": "Case Type"},
        {"text": "Workday Cases", "associated_visual": None},
        {"text": "E-Mail", "associated_visual": "E-Mail"},
        {"text": "All", "associated_visual": "Service Team"},
    ]

    updated = _apply_bookmark_associations(items, labels)

    combined = "E-Mail / Phone Call / Workday Cases"
    assert updated[0]["associated_visual"] == combined
    assert updated[1]["associated_visual"] == combined
    assert updated[2]["associated_visual"] == "E-Mail"
    assert updated[3]["associated_visual"] == "Service Team"

    group = "Bookmark buttons: E-Mail | Phone Call | Workday Cases"
    state_a = _state("A", 100, filter_values={group: ["Workday Cases"]})
    state_b = _state("B", 100, filter_values={group: ["E-Mail"]})

    result = compare_dashboards([state_a], [state_b])

    rows = [row for row in result["comparisons"] if row["element"] == "Bookmark buttons"]
    assert len(rows) == 1
    assert rows[0]["dashboard_a"] == "E-Mail, Phone Call, Workday Cases"
    assert rows[0]["dashboard_b"] == "E-Mail, Phone Call, Workday Cases"
    assert rows[0]["status"] == "MATCH"


def test_filter_state_keys_normalize_whitespace_but_not_values() -> None:
    state_a = replace(
        _state("A", 100),
        filter=FilterSelection("Case   Type", " E-Mail "),
    )
    state_b = replace(
        _state("B", 100),
        filter=FilterSelection("case type", "e-mail"),
    )
    state_a.extracted["title_items"] = [
        {"text": "Case Type", "category": "slicer_title"}
    ]
    state_b.extracted["title_items"] = [
        {"text": "Case Type", "category": "slicer_title"}
    ]

    result = compare_dashboards([state_a], [state_b])

    assert len(result["title_comparisons"]) == 1
    assert result["title_comparisons"][0]["status"] == "MATCH"

    state_b_phone = replace(
        state_b, filter=FilterSelection("Case Type", "Phone Call")
    )
    different_filter_result = compare_dashboards([state_a], [state_b_phone])
    assert {
        row["state"] for row in different_filter_result["title_comparisons"]
    } == {
        "sales|case type=e-mail",
        "sales|case type=phone call",
    }
    assert all(
        row["status"] == "UNVERIFIED"
        for row in different_filter_result["title_comparisons"]
    )


def test_brand_logos_are_excluded_from_comparison() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["title_items"] = [
        {"text": "SpartanNash", "category": "brand_logo", "reading_order": 1}
    ]
    state_b.extracted["title_items"] = [
        {"text": "SpartanNash.", "category": "brand_logo", "reading_order": 1}
    ]

    result = compare_dashboards([state_a], [state_b])

    assert result["title_comparisons"] == []
    assert not any(
        record["item_name"] in {"SpartanNash", "SpartanNash."}
        for record in result["comparison_records"]
    )


def test_title_insertions_do_not_shift_later_report_and_section_titles() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["title_items"] = [
        {"text": "HR Now", "category": "report_title", "reading_order": 1},
        {"text": "Summary", "category": "section_title", "reading_order": 2},
    ]
    state_b.extracted["title_items"] = [
        {
            "text": f"Chart {index}",
            "category": "chart_title",
            "associated_visual": f"Visual {index}",
            "reading_order": index,
        }
        for index in range(1, 5)
    ] + [
        {"text": "HR Now", "category": "report_title", "reading_order": 5},
        {"text": "Summary", "category": "section_title", "reading_order": 6},
    ]

    titles = compare_dashboards([state_a], [state_b])["title_comparisons"]

    assert [(row["dashboard_a"], row["dashboard_b"]) for row in titles[-2:]] == [
        ("HR Now", "HR Now"),
        ("Summary", "Summary"),
    ]
    assert [row["status"] for row in titles[-2:]] == ["MATCH", "MATCH"]


def test_blank_title_extraction_is_retained_as_uncertain() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["title_items"] = [
        {
            "text": "",
            "category": "chart_title",
            "reading_order": 1,
            "normalized_bounding_box": {"x": 0.2, "y": 0.2, "width": 0.2, "height": 0.02},
        }
    ]
    state_b.extracted["title_items"] = [
        {
            "text": "Sales Trend",
            "category": "chart_title",
            "reading_order": 1,
            "normalized_bounding_box": {"x": 0.2, "y": 0.2, "width": 0.2, "height": 0.02},
        }
    ]

    title = compare_dashboards([state_a], [state_b])["title_comparisons"][0]

    assert title["dashboard_a"] == ""
    assert title["dashboard_b"] == "Sales Trend"
    assert title["title_status"] == "extraction_uncertain"
    assert title["status"] == "UNVERIFIED"


def test_visual_sequence_alignment_does_not_cascade_after_an_insertion() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["elements"] = [
        {"type": "kpi", "title": "Phone Call", "value": 1, "reading_order": 1},
        {"type": "kpi", "title": "Workday Cases", "value": 2, "reading_order": 2},
        {"type": "kpi", "title": "Service Team", "value": 3, "reading_order": 3},
    ]
    state_b.extracted["elements"] = [
        {"type": "kpi", "title": "Workday Cases", "value": 2, "reading_order": 1},
        {"type": "kpi", "title": "Service Team", "value": 3, "reading_order": 2},
    ]

    comparisons = compare_dashboards([state_a], [state_b])["comparisons"]

    assert [
        (row["element"], row["alignment_status"], row["status"])
        for row in comparisons
    ] == [
        ("Phone Call", "missing_in_b", "UNVERIFIED"),
        ("Workday Cases", "sequence_aligned", "MATCH"),
        ("Service Team", "sequence_aligned", "MATCH"),
    ]
    assert [(row["order_a"], row["order_b"]) for row in comparisons] == [
        (1, None),
        (2, 1),
        (3, 2),
    ]
    assert [row["aligned_position"] for row in comparisons] == [1, 2, 3]


def test_slicer_sequence_alignment_keeps_later_values_after_an_extra_option() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["elements"] = [
        {
            "type": "slicer_value",
            "title": "Phone Call",
            "value": "Phone Call",
            "associated_visual": "E-Mail",
            "reading_order": 1,
        },
        {
            "type": "slicer_value",
            "title": "Workday Cases",
            "value": "Workday Cases",
            "associated_visual": "E-Mail",
            "reading_order": 2,
        },
        {
            "type": "slicer_title",
            "title": "Service Team",
            "value": "Service Team",
            "reading_order": 3,
        },
        {
            "type": "slicer_value",
            "title": "HR Support Center",
            "value": "HR Support Center",
            "associated_visual": "Service Team",
            "reading_order": 4,
        },
        {
            "type": "slicer_title",
            "title": "Disposition Area",
            "value": "Disposition Area",
            "reading_order": 5,
        },
    ]
    state_b.extracted["elements"] = [
        {
            "type": "slicer_value",
            "title": "Workday Cases",
            "value": "Workday Cases",
            "associated_visual": "E-Mail",
            "reading_order": 1,
        },
        {
            "type": "slicer_title",
            "title": "Service Team",
            "value": "Service Team",
            "reading_order": 2,
        },
        {
            "type": "slicer_value",
            "title": "HR Support Center",
            "value": "HR Support Center",
            "associated_visual": "Service Team",
            "reading_order": 3,
        },
        {
            "type": "slicer_title",
            "title": "Disposition Area",
            "value": "Disposition Area",
            "reading_order": 4,
        },
    ]

    comparisons = compare_dashboards([state_a], [state_b])["comparisons"]

    assert [
        (row["element"], row["order_a"], row["order_b"], row["status"])
        for row in comparisons
    ] == [
        ("Phone Call", 1, None, "UNVERIFIED"),
        ("Workday Cases", 2, 1, "MATCH"),
        ("Service Team", 3, 2, "MATCH"),
        ("HR Support Center", 4, 3, "MATCH"),
        ("Disposition Area", 5, 4, "MATCH"),
    ]


def test_unknown_element_types_make_weak_pairs_ambiguous() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["elements"] = [
        {
            "type": "other",
            "title": "Unclassified element",
            "value": 1,
            "values": [1],
            "reading_order": 1,
        }
    ]
    state_b.extracted["elements"] = [
        {
            "type": "other",
            "title": "Unclassified element",
            "value": 1,
            "values": [1],
            "reading_order": 1,
        }
    ]

    comparison = compare_dashboards([state_a], [state_b])["comparisons"][0]

    assert comparison["alignment_status"] == "ambiguous_alignment"
    assert comparison["alignment_confidence"] == "low"
    assert comparison["difference_details"] == ["AMBIGUOUS_ALIGNMENT"]
    assert comparison["status"] == "UNVERIFIED"


def test_repeated_values_without_visual_context_are_ambiguous() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["elements"] = [
        {"type": "slicer_value", "title": "All", "value": "All"},
        {"type": "slicer_value", "title": "All", "value": "All"},
    ]
    state_b.extracted["elements"] = [
        {"type": "slicer_value", "title": "All", "value": "All"},
        {"type": "slicer_value", "title": "All", "value": "All"},
    ]

    comparisons = compare_dashboards([state_a], [state_b])["comparisons"]

    assert len(comparisons) == 2
    assert all(
        row["alignment_status"] == "ambiguous_alignment"
        and row["status"] == "UNVERIFIED"
        for row in comparisons
    )


def test_visual_layout_difference_is_reported_separately() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["elements"] = [
        {
            "type": "kpi",
            "title": "Total Cases",
            "value": 10,
            "normalized_bounding_box": {
                "x": 0.1,
                "y": 0.1,
                "width": 0.2,
                "height": 0.1,
            },
        }
    ]
    state_b.extracted["elements"] = [
        {
            "type": "kpi",
            "title": "Total Cases",
            "value": 10,
            "normalized_bounding_box": {
                "x": 0.5,
                "y": 0.5,
                "width": 0.2,
                "height": 0.1,
            },
        }
    ]

    comparison = compare_dashboards([state_a], [state_b])["comparisons"][0]

    assert comparison["layout_status"] == "layout_mismatch"
    assert comparison["difference_details"] == ["LAYOUT_MISMATCH"]
    assert comparison["status"] == "DIFFERENT"


def test_visuals_compare_type_and_values_at_the_same_reading_order() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["elements"] = [
        {"type": "kpi", "title": "metadata_label", "value": 20, "reading_order": 3},
        {"type": "kpi", "title": "slicer_title", "value": 10, "reading_order": 6},
    ]
    state_b.extracted["elements"] = [
        {"type": "chart", "title": "metadata_label", "value": 20, "reading_order": 3},
        {"type": "kpi", "title": "slicer_title", "value": 20, "reading_order": 6},
    ]

    comparisons = compare_dashboards([state_a], [state_b])["comparisons"]

    assert [(row["order_a"], row["order_b"]) for row in comparisons] == [
        (3, 3),
        (6, 6),
    ]
    assert comparisons[0]["element_type_a"] == "kpi"
    assert comparisons[0]["element_type_b"] == "chart"
    assert comparisons[0]["difference_details"] == ["TYPE_MISMATCH"]
    assert comparisons[0]["status"] == "UNVERIFIED"
    assert comparisons[1]["difference_details"] == ["CONTENT_MISMATCH"]
    assert comparisons[1]["status"] == "DIFFERENT"


def test_child_labels_use_paired_visual_identity_when_visual_is_renamed() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    box = {"x": 0.2, "y": 0.2, "width": 0.3, "height": 0.2}
    state_a.extracted["elements"] = [
        {
            "type": "chart",
            "title": "Cases by Segment",
            "categories": ["Segment"],
            "values": ["E-Mail"],
            "normalized_bounding_box": box,
        }
    ]
    state_b.extracted["elements"] = [
        {
            "type": "chart",
            "title": "Cases by Segment Updated",
            "categories": ["Segment"],
            "values": ["E-Mail"],
            "normalized_bounding_box": box,
        }
    ]
    state_a.extracted["title_items"] = [
        {
            "text": "Cases by Segment",
            "category": "chart_title",
            "associated_visual": "Cases by Segment",
        },
        {
            "text": "E-Mail",
            "category": "legend_item",
            "associated_visual": "Cases by Segment",
        },
    ]
    state_b.extracted["title_items"] = [
        {
            "text": "Cases by Segment Updated",
            "category": "chart_title",
            "associated_visual": "Cases by Segment Updated",
        },
        {
            "text": "E-Mail",
            "category": "legend_item",
            "associated_visual": "Cases by Segment Updated",
        },
    ]

    result = compare_dashboards([state_a], [state_b])
    labels = result["title_comparisons"]

    chart_title = next(row for row in labels if row["standard_element_name_a"] == "chart_title")
    legend_item = next(row for row in labels if row["standard_element_name_a"] == "legend_item")
    assert chart_title["title_status"] == "renamed"
    assert legend_item["dashboard_a"] == legend_item["dashboard_b"] == "E-Mail"
    assert legend_item["status"] == "MATCH"
    assert legend_item["associated_visual_a"] == "Cases by Segment"
    assert legend_item["associated_visual_b"] == "Cases by Segment Updated"


def test_title_items_are_never_matched_across_pages(tmp_path: Path) -> None:
    state_a = replace(_state("A", 100), page="Summary")
    state_b = replace(_state("B", 100), page="Compliance")
    for state in (state_a, state_b):
        state.extracted["title_items"] = [
            {
                "text": "E-Mail",
                "category": "legend_item",
                "associated_visual": "Cases by Segment",
            }
        ]

    result = compare_dashboards([state_a], [state_b])

    assert len(result["title_comparisons"]) == 2
    assert all(row["status"] == "UNVERIFIED" for row in result["title_comparisons"])
    assert {row["state"] for row in result["title_comparisons"]} == {
        "summary|year=2025",
        "compliance|year=2025",
    }

    write_comparison(result, tmp_path, "Dashboard A", "Dashboard B")
    workbook = load_workbook(tmp_path / "Dashboard_Comparison.xlsx", read_only=True)
    assert workbook.sheetnames == ["Visual_Validation"]
    workbook.close()


def test_comparison_reports_visuals_from_both_dashboards() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["elements"] = [
        {"type": "kpi", "title": "Baseline KPI", "value": 100},
    ]
    state_b.extracted["elements"] = [
        {"type": "kpi", "title": "Baseline KPI", "value": 100},
        {"type": "kpi", "title": "B-only KPI", "value": 500},
    ]
    state_b.extracted["title_items"] = [
        {"text": "B-only label", "category": "chart_title"},
    ]

    result = compare_dashboards([state_a], [state_b])

    assert len(result["comparisons"]) == 2
    rows_by_element = {row["element"]: row for row in result["comparisons"]}
    assert rows_by_element["Baseline KPI"]["status"] == "MATCH"
    assert rows_by_element["B-only KPI"]["dashboard_a"] == "not extracted"
    assert rows_by_element["B-only KPI"]["dashboard_b"] == "500"
    assert rows_by_element["B-only KPI"]["status"] == "UNVERIFIED"
    assert len(result["title_comparisons"]) == 1
    assert result["title_comparisons"][0]["element_text_b"] == "B-only label"
    assert result["title_comparisons"][0]["standard_element_name_b"] == "chart_title"
    assert result["title_comparisons"][0]["status"] == "UNVERIFIED"
    assert result["export_option_comparisons"] == []
    assert result["exported_data_comparisons"] == []


def test_unmatched_visuals_on_both_sides_are_both_reported() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["elements"] = [
        {"type": "kpi", "title": "Baseline KPI", "value": 100},
    ]
    state_b.extracted["elements"] = [
        {"type": "kpi", "title": "B-only KPI", "value": 500},
    ]

    result = compare_dashboards([state_a], [state_b])

    assert len(result["comparisons"]) == 2
    baseline, test_only = result["comparisons"]
    assert baseline["element"] == "Baseline KPI"
    assert baseline["dashboard_a"] == "100"
    assert baseline["dashboard_b"] == "not extracted"
    assert baseline["order_a"] == 1
    assert baseline["order_b"] is None
    assert baseline["alignment_status"] == "missing_in_b"
    assert baseline["status"] == "UNVERIFIED"
    assert test_only["dashboard_a"] == "not extracted"
    assert test_only["dashboard_b"] == "500"
    assert test_only["order_a"] is None
    assert test_only["order_b"] == 1
    assert test_only["alignment_status"] == "extra_in_b"
    assert test_only["status"] == "UNVERIFIED"


def test_comparison_reports_dashboard_b_only_states() -> None:
    state_a = _state("A", 100)
    state_a.extracted["elements"] = []
    state_b = DashboardState(
        dashboard="B",
        page="Additional page",
        filter=None,
        screenshot=Path("additional.png"),
        extracted={
            "elements": [
                {"type": "kpi", "title": "New KPI", "value": 25},
            ],
            "title_items": [],
        },
    )

    result = compare_dashboards([state_a], [state_b])

    row = next(row for row in result["comparisons"] if row["element"] == "New KPI")
    assert row["state"] == "additional page|initial"
    assert row["dashboard_a"] == "state unavailable"
    assert row["dashboard_b"] == "25"
    assert row["status"] == "UNVERIFIED"
    assert row["alignment_status"] == "extra_in_b"


def test_duplicate_visuals_align_by_extracted_position() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["elements"] = [
        {"type": "kpi", "title": "Open cases", "value": 10},
        {"type": "kpi", "title": "Open cases", "value": 20},
    ]
    state_b.extracted["elements"] = [
        {"type": "kpi", "title": "Open cases", "value": 10},
    ]

    result = compare_dashboards([state_a], [state_b])

    assert len(result["comparisons"]) == 2
    assert result["comparisons"][0]["status"] == "MATCH"
    assert result["comparisons"][0]["order_a"] == result["comparisons"][0]["order_b"] == 1
    assert result["comparisons"][1]["alignment_status"] == "missing_in_b"
    assert result["comparisons"][1]["status"] == "UNVERIFIED"


def test_comparison_excludes_legacy_unflagged_power_bi_chrome() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["elements"] = [
        {
            "type": "text",
            "title": "Pages",
            "value": "Pages",
            "bounding_box": {"x": 20, "y": 90, "width": 50, "height": 15},
        },
        {
            "type": "kpi",
            "title": "Sales",
            "value": 100,
            "bounding_box": {"x": 300, "y": 200, "width": 50, "height": 15},
        },
    ]
    state_b.extracted["elements"] = [
        {"type": "kpi", "title": "Sales", "value": 100},
    ]
    state_a.extracted["title_items"] = [
        {
            "text": "HR Now Report",
            "category": "report_title",
            "bounding_box": {"x": 140, "y": 17, "width": 100, "height": 20},
        },
        {
            "text": "Search",
            "category": "visual_title",
            "bounding_box": {"x": 380, "y": 17, "width": 100, "height": 20},
        },
        {
            "text": "Pages",
            "category": "section_title",
            "bounding_box": {"x": 20, "y": 90, "width": 50, "height": 15},
        },
        {"text": "Sales", "category": "chart_title"},
    ]

    result = compare_dashboards([state_a], [state_b])

    assert [row["element"] for row in result["comparisons"]] == ["Sales"]
    assert [row["dashboard_a"] for row in result["title_comparisons"]] == [
        "HR Now Report",
        "Sales",
    ]


def test_filter_controls_are_excluded_and_actual_filter_values_are_checked() -> None:
    state_a = _state("A", 100, ["Category"], {"Category": ["N"]})
    state_b = _state("B", 100, ["Category"], {"Category": ["All"]})
    state_a.extracted["elements"].append(
        {"type": "text", "title": "Category", "value": "N"}
    )
    state_b.extracted["elements"].append(
        {"type": "text", "title": "Category", "value": "All"}
    )

    result = compare_dashboards([state_a], [state_b])

    assert [row["element"] for row in result["comparisons"]] == [
        "Total Sales",
        "Category",
    ]
    assert result["summary"]["matches"] == 1
    assert result["summary"]["differences"] == 1


def test_filter_all_labels_are_treated_as_equivalent() -> None:
    state_a = _state("A", 100, ["Category"], {"Category": ["All"]})
    state_b = _state("B", 100, ["Category"], {"Category": ["Select all"]})

    result = compare_dashboards([state_a], [state_b])

    assert result["summary"]["matches"] == 1
    assert result["summary"]["differences"] == 0


def test_excel_comparison_shows_filter_heading_and_plain_dashboard_values(
    tmp_path: Path,
) -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    for state in (state_a, state_b):
        state.extracted["elements"] = [
            {
                "type": "kpi",
                "title": "Time Period",
                "value": {"value": "All"},
            }
        ]

    result = compare_dashboards([state_a], [state_b])
    assert result["comparisons"][0]["dashboard_a"] == "All"
    assert result["comparisons"][0]["dashboard_b"] == "All"
    write_comparison(result, tmp_path, "Dashboard A", "Dashboard B")

    workbook = load_workbook(tmp_path / "Dashboard_Comparison.xlsx", read_only=True)
    assert "Comparison" not in workbook.sheetnames
    workbook.close()
    assert result["comparisons"][0]["element"] == "Time Period"
    assert result["comparisons"][0]["dashboard_a"] == "All"
    assert result["comparisons"][0]["dashboard_b"] == "All"


def test_tables_are_paired_in_order_when_titles_differ() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    headers = ["Week Ending", "On Hand $", "Gross Excess $"]
    state_a.extracted["elements"] = [
        {
            "type": "table",
            "title": "table",
            "categories": [],
            "rows": [headers, ["10/4/2026", 0, 0]],
            "value": None,
            "values": [],
        }
    ]
    state_b.extracted["elements"] = [
        {
            "type": "table",
            "title": "Excess by Item Data",
            "categories": headers,
            "rows": [["10/4/2026", "$0", "$0"]],
            "value": None,
            "values": [],
        }
    ]

    result = compare_dashboards([state_a], [state_b])

    assert len(result["comparisons"]) == 1
    assert result["comparisons"][0]["status"] == "MATCH"


def test_visuals_match_when_titles_differ_only_by_ocr() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    headers = ["Week Ending", "Center Store", "Fresh", "Total"]
    state_a.extracted["elements"] = [
        {
            "type": "table",
            "title": "Inventory ($M)",
            "categories": headers,
            "rows": [["10/4/2026", 100, 25, 125]],
            "value": None,
            "values": [],
        }
    ]
    state_b.extracted["elements"] = [
        {
            "type": "table",
            "title": "Inventory (SM)",
            "categories": headers,
            "rows": [["10/4/2026", "$100", "$25", "$125"]],
            "value": None,
            "values": [],
        }
    ]

    result = compare_dashboards([state_a], [state_b])

    assert len(result["comparisons"]) == 1
    assert result["comparisons"][0]["status"] == "MATCH"


def test_refresh_timestamp_difference_is_informational() -> None:
    state_a = _state("A", "10/7/2026 12:45 PM")
    state_b = _state("B", "10/8/2026 11:08 AM")
    state_a.extracted["elements"][0]["title"] = "Last Refresh"
    state_b.extracted["elements"][0]["title"] = "Last Refresh"

    result = compare_dashboards([state_a], [state_b])

    assert result["comparisons"][0]["status"] == "INFO"
    assert result["summary"]["informational"] == 1
    assert result["summary"]["differences"] == 0


def test_dashboard_name_difference_is_reported_as_identity_metadata() -> None:
    state_a = _state("A", "Excess Summary")
    state_b = _state("B", "Excess Summary - GCP")
    state_a.extracted["elements"][0]["title"] = "Dashboard name"
    state_b.extracted["elements"][0]["title"] = "Dashboard name"

    result = compare_dashboards([state_a], [state_b])

    assert result["comparisons"][0]["status"] == "INFO"
    assert "Excess Summary" in result["comparisons"][0]["dashboard_a"]
    assert "Excess Summary - GCP" in result["comparisons"][0]["dashboard_b"]
    assert result["summary"]["informational"] == 1
    assert result["summary"]["differences"] == 0


def test_dashboard_metadata_is_compared_separately_and_informationally(
    tmp_path: Path,
) -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["dashboard_metadata"] = {
        "report_title": "HR Now Report",
        "refresh_timestamp": "10/7/2026 7:03:56 AM",
    }
    state_b.extracted["dashboard_metadata"] = {
        "report_title": "HR Now Report - Production",
        "refresh_timestamp": "10/8/2026 7:03:56 AM",
    }
    state_a.extracted["title_items"] = [
        {"text": "HR NOW - Summary", "category": "dashboard_title"},
        {
            "text": "10/7/2026 7:03:56 AM",
            "category": "metadata_value",
            "associated_visual": "Last refresh",
        },
    ]
    state_b.extracted["title_items"] = [
        {"text": "HR NOW - Summary", "category": "dashboard_title"},
        {
            "text": "10/8/2026 7:03:56 AM",
            "category": "metadata_value",
            "associated_visual": "Last refresh",
        },
    ]

    result = compare_dashboards([state_a], [state_b])

    metadata = {
        row["metadata_field"]: row for row in result["metadata_comparisons"]
    }
    assert metadata["report_title"]["status"] == "INFO"
    assert metadata["report_title"]["identity_warning"] is True
    assert metadata["refresh_timestamp"]["status"] == "INFO"
    assert metadata["refresh_timestamp"]["dashboard_a"] == "10/7/2026 7:03:56 AM"
    assert result["summary"]["metadata_identity_warnings"] == 1
    assert result["summary"]["metadata_refresh_differences"] == 1
    assert result["summary"]["differences"] == 0

def test_unavailable_dashboard_metadata_is_unverified() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["dashboard_metadata"] = {
        "report_title": "HR Now Report"
    }

    result = compare_dashboards([state_a], [state_b])

    assert result["metadata_comparisons"] == [
        {
            "state": "sales|year=2025",
            "element": "Report title",
            "element_type": "report_title",
            "dashboard_a": "HR Now Report",
            "dashboard_b": "unavailable",
            "status": "UNVERIFIED",
            "identity_warning": False,
            "metadata_field": "report_title",
        }
    ]


def test_equal_dashboard_metadata_matches_and_missing_refresh_is_unverified() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["dashboard_metadata"] = {
        "report_title": "HR Now Report",
        "refresh_timestamp": "10/7/2026 7:03:56 AM",
    }
    state_b.extracted["dashboard_metadata"] = {
        "report_title": "HR Now Report",
    }

    result = compare_dashboards([state_a], [state_b])
    metadata = {
        row["metadata_field"]: row for row in result["metadata_comparisons"]
    }

    assert metadata["report_title"]["status"] == "MATCH"
    assert metadata["refresh_timestamp"]["status"] == "UNVERIFIED"
    assert result["summary"]["metadata_identity_warnings"] == 0
    assert result["summary"]["metadata_refresh_differences"] == 0


def test_rows_without_any_values_are_unverified() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["elements"] = [
        {
            "type": "chart",
            "title": "Fiscal Year 2026",
            "categories": [],
            "values": [],
            "rows": [["Nov 2025", None], ["Jan 2026", None]],
            "value": None,
        }
    ]
    state_b.extracted["elements"] = []

    result = compare_dashboards([state_a], [state_b])

    assert result["comparisons"][0]["status"] == "UNVERIFIED"
    assert result["summary"]["differences"] == 0
    assert result["summary"]["unverified"] == 1


def test_comparison_includes_exported_rows() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["elements"][0]["rows"] = [["Week 1", 100]]
    state_b.extracted["elements"][0]["rows"] = [["Week 1", 120]]

    result = compare_dashboards([state_a], [state_b])

    assert result["summary"]["differences"] == 1
    assert result["comparisons"][0]["status"] == "DIFFERENT"


def test_missing_row_in_two_parsed_exports_remains_a_verified_difference() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    for state, rows in (
        (state_a, [["Week 1", 100], ["Week 2", 200]]),
        (state_b, [["Week 1", 100]]),
    ):
        state.extracted["exported_data"] = [
            {
                "type": "table",
                "title": "Weekly sales",
                "categories": ["Week", "Sales"],
                "rows": rows,
                "data_sources": ["summarized_export"],
                "export_file": "weekly.xlsx",
            }
        ]

    result = compare_dashboards([state_a], [state_b])

    omitted_row = next(
        row
        for row in result["exported_data_comparisons"]
        if row["row"] == 2 and row["column"] == "Week"
    )
    assert omitted_row["dashboard_b"] is None
    assert omitted_row["status"] == "DIFFERENT"


def test_exported_rows_match_by_dimensions_instead_of_position() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    headers = [
        "Fiscal Week End Date",
        "% Cases Meeting SLA",
        "Cases Registered",
        "Cases Meeting SLA",
    ]
    for state, rows in (
        (
            state_a,
            [
                ["9/12/2026", 2.80952381, 42, "No"],
                ["9/12/2026", 1, 118, "Yes"],
                ["9/19/2026", 2.612244898, 49, "No"],
                ["9/19/2026", 1, 128, "Yes"],
            ],
        ),
        (
            state_b,
            [
                ["9/19/2026", 1, 128, "Yes"],
                ["9/19/2026", 2.612244898, 49, "No"],
                ["9/26/2026", 3.58974359, 39, "No"],
            ],
        ),
    ):
        state.extracted["exported_data"] = [
            {
                "type": "table",
                "title": "SLA Compliance",
                "categories": headers,
                "rows": rows,
                "data_sources": ["summarized_export"],
                "export_file": "sla.xlsx",
            }
        ]

    result = compare_dashboards([state_a], [state_b])
    rows_by_number = {}
    for cell in result["exported_data_comparisons"]:
        rows_by_number.setdefault(cell["row"], {})[cell["column"]] = cell

    assert rows_by_number[1]["Fiscal Week End Date"]["dashboard_a"] == "9/12/2026"
    assert rows_by_number[1]["Fiscal Week End Date"]["dashboard_b"] is None
    assert rows_by_number[1]["Fiscal Week End Date"]["status"] == "DIFFERENT"
    assert rows_by_number[2]["Cases Meeting SLA"]["dashboard_b"] is None
    assert rows_by_number[3]["Fiscal Week End Date"]["dashboard_b"] == "9/19/2026"
    assert rows_by_number[3]["Cases Meeting SLA"]["dashboard_b"] == "No"
    assert all(
        cell["status"] == "MATCH" for cell in rows_by_number[3].values()
    )
    assert rows_by_number[4]["Fiscal Week End Date"]["dashboard_b"] == "9/19/2026"
    assert rows_by_number[4]["Cases Meeting SLA"]["dashboard_b"] == "Yes"
    assert all(
        cell["status"] == "MATCH" for cell in rows_by_number[4].values()
    )
    assert rows_by_number[5]["Fiscal Week End Date"]["dashboard_a"] is None
    assert rows_by_number[5]["Fiscal Week End Date"]["dashboard_b"] == "9/26/2026"
    assert rows_by_number[5]["Fiscal Week End Date"]["status"] == "DIFFERENT"


def test_exported_rows_match_with_extra_fiscal_dimensions_and_date_formats() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["exported_data"] = [
        {
            "type": "table",
            "title": "Cases Registered vs Cases Resolved",
            "categories": [
                "Fiscal Week End Date(Text Type)",
                "Day Name",
                "Cases Registered",
                "Cases Resolved",
            ],
            "rows": [
                ["9/19/2026", "Sunday", 13, 10],
                ["9/19/2026", "Monday", 170, 162],
                ["9/26/2026", "Sunday", 20, 17],
            ],
            "data_sources": ["summarized_export"],
        }
    ]
    state_b.extracted["exported_data"] = [
        {
            "type": "table",
            "title": "Cases Registered vs Cases Resolved",
            "categories": [
                "Fiscal Year",
                "Fiscal Quarter Detail",
                "Fiscal Week End Date",
                "Day Name",
                "Cases Registered",
                "Cases Resolved",
                "Additional Metric",
            ],
            "rows": [
                ["2026", "Quarter 4 2026", "09-19-2026", "Monday", 170, 162, 1],
                ["2026", "Quarter 4 2026", "09-19-2026", "Sunday", 13, 10, 1],
                ["2026", "Quarter 4 2026", "09-26-2026", "Sunday", 20, 17, 1],
                ["2026", "Quarter 1 2027", "10-10-2026", "Sunday", 22, 15, 1],
            ],
            "data_sources": ["summarized_export"],
        }
    ]

    result = compare_dashboards([state_a], [state_b])
    by_row = {}
    for cell in result["exported_data_comparisons"]:
        by_row.setdefault(cell["row"], {})[cell["column"]] = cell

    for row_number in (1, 2, 3):
        assert by_row[row_number]["Fiscal Week End Date(Text Type)"]["status"] == "MATCH"
        assert by_row[row_number]["Cases Registered"]["status"] == "MATCH"
        assert by_row[row_number]["Cases Resolved"]["status"] == "MATCH"
        assert "Fiscal Year" not in by_row[row_number]
        assert "Fiscal Quarter Detail" not in by_row[row_number]
    assert by_row[1]["Additional Metric"]["dashboard_a"] is None
    assert by_row[1]["Additional Metric"]["status"] == "DIFFERENT"
    assert by_row[4]["Fiscal Week End Date(Text Type)"]["dashboard_a"] is None
    assert by_row[4]["Fiscal Week End Date(Text Type)"]["dashboard_b"] == "10-10-2026"
    assert by_row[4]["Fiscal Week End Date(Text Type)"]["status"] == "DIFFERENT"


def test_comparison_includes_default_export_status_and_cell_results() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    for state, amount, filename in (
        (state_a, 100, "trend_a.xlsx"),
        (state_b, 120, "trend_b.xlsx"),
    ):
        state.extracted["elements"] = [
            {
                "type": "chart",
                "title": "Sales trend",
                "categories": ["Week", "Sales"],
                "rows": [["Week 1", amount]],
                "data_sources": ["powerbi_export"],
                "exportOptionAvailable": True,
                "export_status": "exported",
                "export_data_format": "default",
                "export_file": filename,
                "export_json_file": filename.removesuffix(".xlsx") + ".json",
            }
        ]
        state.extracted["exported_data"] = [
            state.extracted["elements"][0].copy()
        ]
        state.extracted["export_options"] = [
            {
                "title": "Sales trend",
                "exportOptionAvailable": True,
                "export_status": "exported",
                "export_data_format": "default",
                "export_file": filename,
                "export_json_file": filename.removesuffix(".xlsx") + ".json",
                "screenshot": "initial_exports/menu.png",
            }
        ]

    result = compare_dashboards([state_a], [state_b])

    screenshot_row = result["comparisons"][0]
    assert screenshot_row["screenshot_comparison_skipped"] is True
    assert screenshot_row["already_exported_a"] == "Yes"
    assert screenshot_row["already_exported_b"] == "Yes"
    assert screenshot_row["status"] == "DIFFERENT"
    assert screenshot_row["dashboard_a"] == "Already exported"
    assert screenshot_row["dashboard_b"] == "Already exported"
    assert screenshot_row["dashboard_data_a"]["rows"] == [["Week 1", 100]]
    assert screenshot_row["dashboard_data_b"]["rows"] == [["Week 1", 120]]
    assert result["export_option_comparisons"][0]["status"] == "MATCH"
    assert result["export_option_comparisons"][0]["export_data_format_a"] == "default"
    assert result["exported_data_comparisons"] == [
        {
            "state": "sales|year=2025",
            "visual": "Sales trend",
            "row": 1,
            "column": "Week",
            "dashboard_a": "Week 1",
            "dashboard_b": "Week 1",
            "data_source_a": "powerbi_export",
            "data_source_b": "powerbi_export",
            "export_file_a": "trend_a.xlsx",
            "export_file_b": "trend_b.xlsx",
            "export_json_file_a": "trend_a.json",
            "export_json_file_b": "trend_b.json",
            "export_status_a": "exported",
            "export_status_b": "exported",
            "exportOptionAvailable_a": True,
            "exportOptionAvailable_b": True,
            "export_error_a": None,
            "export_error_b": None,
            "status": "MATCH",
        },
        {
            "state": "sales|year=2025",
            "visual": "Sales trend",
            "row": 1,
            "column": "Sales",
            "dashboard_a": 100,
            "dashboard_b": 120,
            "data_source_a": "powerbi_export",
            "data_source_b": "powerbi_export",
            "export_file_a": "trend_a.xlsx",
            "export_file_b": "trend_b.xlsx",
            "export_json_file_a": "trend_a.json",
            "export_json_file_b": "trend_b.json",
            "export_status_a": "exported",
            "export_status_b": "exported",
            "exportOptionAvailable_a": True,
            "exportOptionAvailable_b": True,
            "export_error_a": None,
            "export_error_b": None,
            "status": "DIFFERENT",
        },
    ]
    assert result["summary"]["exported_data_differences"] == 1


def test_already_exported_value_marker_is_per_dashboard() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    for state in (state_a, state_b):
        state.extracted["elements"] = [
            {
                "type": "chart",
                "title": "Sales trend",
                "value": 100,
                "categories": ["Week", "Sales"],
                "rows": [["Week 1", 100]],
            }
        ]
    state_a.extracted["exported_data"] = [
        {
            "type": "chart",
            "title": "Sales trend",
            "categories": ["Week", "Sales"],
            "rows": [["Week 1", 100]],
            "data_sources": ["summarized_export"],
        }
    ]

    row = compare_dashboards([state_a], [state_b])["comparisons"][0]

    assert row["dashboard_a"] == "Already exported"
    assert row["dashboard_b"] == (
        "Screenshot comparison skipped; other side exported"
    )
    assert row["already_exported_a"] == "Yes"
    assert row["already_exported_b"] == "Unknown"


def test_comparison_displays_screenshot_only_columns_and_series() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    for state, screenshot_only_table, screenshot_only_series in (
        (state_a, 7, 5),
        (state_b, 8, 6),
    ):
        state.extracted["elements"] = [
            {
                "type": "table",
                "title": "Case details",
                "categories": ["Week", "Exported count", "Screenshot-only field"],
                "rows": [["Week 1", 100, screenshot_only_table]],
                "data_sources": ["screenshot", "summarized_export"],
            },
            {
                "type": "chart",
                "title": "Case trend",
                "categories": ["Week", "Exported series", "Screenshot-only series"],
                "rows": [["Week 1", 10, screenshot_only_series]],
                "data_sources": ["screenshot", "summarized_export"],
                "content": {
                    "categories": [{"raw": "Week 1", "status": "read"}],
                    "series": [
                        {
                            "name": "Exported series",
                            "values": [{"raw": 10, "status": "read"}],
                        },
                        {
                            "name": "Screenshot-only series",
                            "values": [
                                {"raw": screenshot_only_series, "status": "read"}
                            ],
                        },
                    ],
                },
            },
        ]
        state.extracted["exported_data"] = [
            {
                "type": "table",
                "title": "Case details",
                "categories": ["Week", "Exported count"],
                "rows": [["Week 1", 100]],
                "data_sources": ["summarized_export"],
            },
            {
                "type": "chart",
                "title": "Case trend",
                "categories": ["Week", "Exported series"],
                "rows": [["Week 1", 10]],
                "data_sources": ["summarized_export"],
            },
        ]

    result = compare_dashboards([state_a], [state_b])
    by_title = {row["element"]: row for row in result["comparisons"]}

    assert by_title["Case details"]["dashboard_a"] == "Already exported"
    assert by_title["Case details"]["dashboard_b"] == "Already exported"
    assert by_title["Case trend"]["dashboard_a"] == "Already exported"
    assert by_title["Case trend"]["dashboard_b"] == "Already exported"
    assert by_title["Case details"]["status"] == "MATCH"
    assert by_title["Case trend"]["status"] == "MATCH"
    assert all(
        row["column"] not in {"Screenshot-only field", "Screenshot-only series"}
        for row in result["exported_data_comparisons"]
    )
    assert all(row["status"] == "MATCH" for row in result["exported_data_comparisons"])


def test_main_comparison_skips_values_for_exported_visuals_and_marks_exports(
    tmp_path: Path,
) -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    for state, screenshot_value, exported_value, available in (
        (state_a, 101, 100, True),
        (state_b, 101, 120, False),
    ):
        state.extracted["elements"] = [
            {
                "type": "table",
                "title": "Weekly sales",
                "categories": ["Week", "Sales"],
                "rows": [["Week 1", exported_value]],
                "exportOptionAvailable": available,
                "export_status": "exported",
                "data_sources": ["screenshot", "summarized_export"],
            }
        ]
        state.extracted["screenshot_elements"] = [
            {
                "type": "table",
                "title": "Weekly sales",
                "categories": ["Week", "Sales"],
                "rows": [["Week 1", screenshot_value]],
                "exportOptionAvailable": available,
            }
        ]
        state.extracted["exported_data"] = [
            {
                "type": "table",
                "title": "Weekly sales",
                "categories": ["Week", "Sales"],
                "rows": [["Week 1", exported_value]],
                "data_sources": ["summarized_export"],
            }
        ]

    result = compare_dashboards([state_a], [state_b])
    row = result["comparisons"][0]

    assert row["dashboard_a"] == "Already exported"
    assert row["dashboard_b"] == "Already exported"
    assert row["screenshot_comparison_skipped"] is True
    assert row["standard_element_name"] == "table_title"
    assert row["element_text_a"] == "Weekly sales"
    assert row["status"] == "DIFFERENT"
    assert row["already_exported_a"] == "Yes"
    assert row["already_exported_b"] == "Yes"
    assert row["exportOptionAvailable_a"] is True
    assert row["exportOptionAvailable_b"] is False
    assert any(
        item["column"] == "Sales"
        and item["dashboard_a"] == 100
        and item["dashboard_b"] == 120
        and item["status"] == "DIFFERENT"
        for item in result["exported_data_comparisons"]
    )
    row["screenshot_comparison_skipped"] = False
    write_comparison(result, tmp_path, "Dashboard A", "Dashboard B")
    workbook = load_workbook(tmp_path / "Dashboard_Comparison.xlsx", read_only=True)
    assert "Comparison" not in workbook.sheetnames
    workbook.close()

    exported_json = json.loads(
        (tmp_path / "comparison.json").read_text(encoding="utf-8")
    )
    assert any(
        row["column"] == "Sales"
        and row["dashboard_a"] == 100
        and row["dashboard_b"] == 120
        for row in exported_json["exported_data_comparisons"]
    )


def test_screenshot_merged_exports_are_not_treated_as_raw_export_data() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    for state in (state_a, state_b):
        state.extracted["elements"] = [
            {
                "type": "chart",
                "title": "Caller types",
                "categories": ["Caller", "Count"],
                "rows": [["HR", 2]],
                "data_sources": ["screenshot", "summarized_export"],
                "export_file": "filters_only.xlsx",
            }
        ]
        state.extracted["export_options"] = [
            {
                "title": "Caller types",
                "exportOptionAvailable": True,
                "export_status": "downloaded_no_tabular_data",
                "export_file": "filters_only.xlsx",
                "export_error": "Applied-filter metadata found; no visual rows.",
            }
        ]

    result = compare_dashboards([state_a], [state_b])

    assert len(result["exported_data_comparisons"]) == 1
    availability = result["exported_data_comparisons"][0]
    assert availability["column"] == "Export data availability"
    assert availability["status"] == "UNVERIFIED"
    assert availability["export_status_a"] == "downloaded_no_tabular_data"
    assert "no visual rows" in availability["export_error_a"]

    state_a.extracted["exported_data"] = [
        {
            "type": "chart",
            "title": "Caller types",
            "categories": ["Caller", "Count"],
            "rows": [["HR", 2]],
            "data_sources": ["summarized_export"],
            "export_file": "parsed.xlsx",
            "export_status": "exported",
        }
    ]
    result = compare_dashboards([state_a], [state_b])

    assert len(result["exported_data_comparisons"]) == 1
    assert result["exported_data_comparisons"][0]["status"] == "UNVERIFIED"
    assert result["export_option_comparisons"][0]["export_status_a"] == (
        "downloaded_no_tabular_data"
    )
    assert "no visual rows" in result["export_option_comparisons"][0]["export_error_a"]


def test_export_comparison_includes_dashboard_b_only_files() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_b.extracted["exported_data"] = [
        {
            "type": "table",
            "title": "New export",
            "categories": ["Name", "Count"],
            "rows": [["Current", 3]],
            "data_sources": ["summarized_export"],
            "export_file": "new_export.xlsx",
        }
    ]
    state_b.extracted["export_options"] = [
        {
            "title": "New export",
            "exportOptionAvailable": True,
            "export_status": "exported",
            "export_file": "new_export.xlsx",
        }
    ]

    result = compare_dashboards([state_a], [state_b])

    assert result["export_option_comparisons"][0]["visual"] == "New export"
    assert result["exported_data_comparisons"] == [
        {
            "state": "sales|year=2025",
            "visual": "New export",
            "row": None,
            "column": "Export data availability",
            "dashboard_a": "No parsed exported data",
            "dashboard_b": "parsed",
            "data_source_a": None,
            "data_source_b": "summarized_export",
            "export_file_a": None,
            "export_file_b": "new_export.xlsx",
            "export_status_a": None,
            "export_status_b": "exported",
            "exportOptionAvailable_a": None,
            "exportOptionAvailable_b": True,
            "export_error_a": None,
            "export_error_b": None,
            "status": "UNVERIFIED",
        }
    ]


def test_exported_data_compares_every_worksheet() -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    for state, west_sales, east_sales in (
        (state_a, 100, 200),
        (state_b, 100, 220),
    ):
        state.extracted["exported_data"] = [
            {
                "type": "table",
                "title": "Sales report",
                "data_sources": ["summarized_export"],
                "worksheets": [
                    {
                        "name": "Summary",
                        "categories": ["Region", "Sales"],
                        "rows": [["West", west_sales]],
                    },
                    {
                        "name": "Details",
                        "categories": ["Region", "Sales"],
                        "rows": [["East", east_sales]],
                    },
                ],
            }
        ]

    result = compare_dashboards([state_a], [state_b])

    assert [(row["worksheet"], row["column"], row["status"]) for row in result[
        "exported_data_comparisons"
    ]] == [
        ("Details", "Region", "MATCH"),
        ("Details", "Sales", "DIFFERENT"),
        ("Summary", "Region", "MATCH"),
        ("Summary", "Sales", "MATCH"),
    ]


def test_comparison_reports_unmatched_state() -> None:
    result = compare_dashboards([_state("A", 100)], [])

    assert result["summary"]["dashboard_a_only_states"] == ["sales|year=2025"]
    assert result["comparisons"][0]["status"] == "UNVERIFIED"
    assert result["comparisons"][0]["alignment_status"] == "missing_in_b"


def test_comparison_writes_json_excel_and_docx_reports(tmp_path: Path) -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["title_items"] = [
        {"text": "Sales trend", "category": "chart_title", "reading_order": 1}
    ]
    state_b.extracted["title_items"] = [
        {"text": "Sales trend", "category": "chart_title", "reading_order": 1}
    ]
    result = compare_dashboards([state_a], [state_b])

    write_comparison(result, tmp_path, "Dashboard A", "Dashboard B")

    assert (tmp_path / "comparison.json").is_file()
    assert not (tmp_path / "comparison.xlsx").exists()
    assert (tmp_path / "Dashboard_Comparison.xlsx").is_file()
    assert (tmp_path / "Dashboard_Comparison.docx").read_bytes().startswith(b"PK")
    assert not (tmp_path / "comparison.docx").exists()
    assert not (tmp_path / "comparison.pdf").exists()
    assert result["comparisons"][0]["element"] == "Total Sales"
    assert result["comparisons"][0]["status"] == "MATCH"
    workbook = load_workbook(tmp_path / "Dashboard_Comparison.xlsx", read_only=True)
    assert workbook.sheetnames == ["Visual_Validation"]
    workbook.close()


def test_visual_validation_tab_compares_visuals_and_embeds_page_screenshots(
    tmp_path: Path,
) -> None:
    from PIL import Image

    screenshot_a = tmp_path / "base.png"
    screenshot_b = tmp_path / "compare.png"
    Image.new("RGB", (32, 18), "white").save(screenshot_a)
    Image.new("RGB", (32, 18), "gray").save(screenshot_b)

    filter_names = [
        "Facility",
        "Supply Planner Hierarchy",
        "Department",
    ]
    filter_values = {
        "Facility": ["All"],
        "Supply Planner Hierarchy": ["All"],
        "Department": ["All"],
    }
    filter_options = {name: ["All"] for name in filter_names}
    filter_control_types = {name: "dropdown" for name in filter_names}

    def page_state(dashboard: str, screenshot: Path) -> DashboardState:
        return replace(
            _state(dashboard, 100),
            page="Executive Summary",
            filter=None,
            screenshot=screenshot,
            filter_names=filter_names,
            filter_values=filter_values,
            filter_options=filter_options,
            filter_control_types=filter_control_types,
        )

    base = page_state("Base", screenshot_a)
    compare = page_state("Compare", screenshot_b)
    base_visuals = [
        {
            "visual_id": "base-on-hand",
            "visual_name": "On Hand $",
            "visual_type": "KPI",
            "x": 0,
            "y": 0,
            "width": 100,
            "height": 50,
        },
        {
            "visual_id": "base-on-order",
            "visual_name": "On Order $",
            "visual_type": "KPI",
            "x": 110,
            "y": 0,
            "width": 100,
            "height": 50,
        },
    ]
    base = replace(base, visuals=base_visuals)
    compare = replace(
        compare,
        visuals=[
            {
                **base_visuals[0],
                "visual_id": "compare-on-hand",
            }
        ],
    )

    result = compare_dashboards([base], [compare])
    state_key = next(iter(result["screenshot_evidence"]["dashboard_a"]))
    result["screenshot_evidence"]["dashboard_a"][state_key].update(
        {
            "dashboard_title": "Fill Rate by DC, Customer & Department",
            "refresh_timestamp": "11/4/2026 5:40:11 AM",
        }
    )
    result["screenshot_evidence"]["dashboard_b"][state_key].update(
        {
            "dashboard_title": "Fill Rate by DC, Customer & Department",
            "refresh_timestamp": "10/28/2026 5:38:09 AM",
        }
    )
    write_comparison(result, tmp_path, "Base", "Compare")

    workbook = load_workbook(tmp_path / "Dashboard_Comparison.xlsx")
    assert "Visual_Validation" in workbook.sheetnames
    sheet = workbook["Visual_Validation"]
    assert [sheet.cell(1, column).value for column in range(1, 8)] == [
        "Visual Differences Summary",
        None,
        None,
        None,
        None,
        None,
        None,
    ]
    summary_header_row = next(
        row
        for row in range(1, sheet.max_row + 1)
        if sheet.cell(row, 1).value == "ID"
        and sheet.cell(row, 5).value == "Visual / Check"
    )
    assert [sheet.cell(summary_header_row, column).value for column in range(1, 8)] == [
        "ID",
        "Page / Area",
        "Compare Dashboard",
        "Base Dashboard",
        "Visual / Check",
        "Observation",
        "Result",
    ]
    technical_title_row = next(
        row
        for row in range(1, sheet.max_row + 1)
        if sheet.cell(row, 1).value == "Visual Inventory & Screenshots"
    )
    summary_rows = {
        sheet.cell(row, 3).value: row
        for row in range(summary_header_row + 1, technical_title_row)
        if str(sheet.cell(row, 1).value or "").isdigit()
    }
    summary_sections = {
        sheet.cell(row, 1).value
        for row in range(1, technical_title_row)
        if sheet.cell(row, 1).value
        in {
            "Dashboard / Page Validation",
            "Filter Validation Log",
            "Visual & Functional Validation Log",
        }
    }
    assert summary_sections == {
        "Dashboard / Page Validation",
        "Filter Validation Log",
        "Visual & Functional Validation Log",
    }
    assert sheet.cell(summary_rows["Facility"], 4).value == "All"
    assert sheet.cell(summary_rows["Supply Planner Hierarchy"], 5).value == "All"
    assert sheet.cell(summary_rows["Department"], 6).value == "Filter selection matches."
    assert "Data Refresh Date" not in summary_rows
    assert sheet.cell(summary_rows["On Hand $"], 7).value == "Pass"
    assert sheet.cell(summary_rows["On Order $"], 7).value == "Fail"
    assert "Filters Pane" not in summary_rows
    result_summary_row = next(
        row
        for row in range(1, sheet.max_row + 1)
        if sheet.cell(row, 1).value == "Result Summary"
    )
    assert sheet.cell(result_summary_row + 1, 1).value == "Result"
    assert sheet.cell(result_summary_row + 1, 2).value == "Count"
    result_count_rows = {
        sheet.cell(row, 1).value: row
        for row in range(result_summary_row + 2, result_summary_row + 6)
    }
    for label in ("Pass", "Fail", "Unable to Verify"):
        assert sheet.cell(result_count_rows[label], 2).value.startswith("=COUNTIF(")
    assert sheet.cell(result_count_rows["Total Checks"], 2).value.startswith("=SUM(")

    technical_header_row = next(
        row
        for row in range(technical_title_row + 1, sheet.max_row + 1)
        if sheet.cell(row, 1).value == "ID"
        and sheet.cell(row, 3).value == "Visual Name"
    )
    assert sheet.cell(technical_header_row, 2).value == "Page / Area"
    assert sheet.cell(technical_header_row + 1, 2).value == "Executive Summary"
    assert sheet.cell(technical_header_row + 1, 3).value == "On Hand $"
    assert sheet.cell(technical_header_row + 1, 4).value == (
        "Facility: All\nSupply Planner Hierarchy: All\nDepartment: All"
    )
    assert sheet.cell(technical_header_row + 1, 6).value == "KPI"
    assert sheet.cell(technical_header_row + 1, 7).value == "KPI"
    assert sheet.cell(technical_header_row + 1, 13).value == "Pass"
    assert sheet.cell(technical_header_row + 2, 3).value == "On Order $"
    assert sheet.cell(technical_header_row + 2, 13).value == "Fail"
    assert len(sheet._images) == 2
    assert all(
        image.anchor._from.row == technical_header_row
        for image in sheet._images
    )
    summary_validation, technical_validation = sheet.data_validations.dataValidation
    assert summary_validation.type == "list"
    assert summary_validation.formula1 == '"Pass,Fail,Unable to Verify"'
    assert f"G{summary_rows['On Hand $']}" in str(summary_validation.sqref)
    assert technical_validation.type == "list"
    assert technical_validation.formula1 == '"Pass,Fail,Unable to Verify"'
    assert f"M{technical_header_row + 1}" in str(technical_validation.sqref)
    assert f"M{technical_header_row + 2}" in str(technical_validation.sqref)
    workbook.close()


def test_visual_validation_omits_uncaptured_dashboard_page_checks(
    tmp_path: Path,
) -> None:
    result = compare_dashboards(
        [_state("Base", 100)],
        [_state("Compare", 100)],
    )
    write_comparison(result, tmp_path, "Base", "Compare")

    workbook = load_workbook(tmp_path / "Dashboard_Comparison.xlsx")
    sheet = workbook["Visual_Validation"]
    first_column = [
        sheet.cell(row, 1).value for row in range(1, sheet.max_row + 1)
    ]
    assert "Dashboard / Page Validation" not in first_column
    assert "Dashboard Header" not in first_column
    assert "Data Refresh Date" not in first_column
    workbook.close()


def test_docx_visual_validation_log_uses_unique_visual_types_and_default_filters(
    tmp_path: Path,
) -> None:
    from docx import Document
    from docx.oxml.ns import qn

    state_a = _state("A", 100)
    state_b = _state("B", 100)
    result = compare_dashboards([state_a], [state_b])
    result["visual_filter"] = {
        "visual_rows": [
            {
                "page": "Sales",
                "visual_name": "Order Table",
                "visual_type_a": "Table",
                "visual_type_b": "Table",
                "status": "MATCH",
            },
            {
                "page": "Sales",
                "visual_name": "Inventory by DOH Band",
                "visual_type_a": "Column Chart",
                "visual_type_b": "Column Chart (100% stacked)",
                "status": "DIFFERENT",
            },
            {
                "page": "Sales",
                "visual_name": "Sales Card",
                "visual_type_a": "KPI",
                "visual_type_b": "KPI",
                "status": "MATCH",
            },
            {
                "page": "Sales",
                "visual_name": "Customer Matrix",
                "visual_type_a": "Matrix",
                "visual_type_b": "Table",
                "status": "MATCH",
            },
        ],
        "filter_rows": [
            {
                "page": "Sales",
                "state": state_a.state_key,
                "filter_name": "Calendar",
                "selected_values_a": "All",
                "selected_values_b": "All",
                "aligned_position": 1,
                "status": "MATCH",
            },
        ],
    }

    output = tmp_path / "comparison.docx"
    write_docx_report(result, output, "Dashboard A", "Dashboard B")

    table = Document(output).tables[2]
    assert [cell.text for cell in table.rows[0].cells] == [
        "ID",
        "Page / Area",
        "Visual / Check",
        "Action / Filter Applied",
        "Expected Result",
        "Result",
    ]
    assert [cell.text for cell in table.rows[1].cells[:4]] == [
        "01",
        "Sales",
        "Column Chart (100% stacked)",
        "Calendar: All",
    ]
    assert [cell.text for cell in table.rows[2].cells[:4]] == [
        "02",
        "Sales",
        "Table",
        "Calendar: All",
    ]
    assert len(table.rows) == 3
    assert table.cell(1, 4).text.startswith(
        "Visual type differs: Base Column Chart; Compare Column Chart"
    )
    assert table.cell(2, 4).text.startswith(
        "Visual type differs: Base Matrix; Compare Table."
    )
    assert "Sales Card" not in _docx_text(output)
    for row in (1, 2):
        result_cell = table.cell(row, 5)
        content = result_cell._tc.xpath(".//w:sdtContent//w:t")
        assert "".join(node.text or "" for node in content) == "Fail"
        dropdown = result_cell._tc.xpath(".//w:sdtPr/w:dropDownList")[0]
        assert [
            item.get(qn("w:displayText"))
            for item in dropdown.findall(qn("w:listItem"))
        ] == ["Pass", "Fail"]


def test_docx_omits_exported_cell_details(
    tmp_path: Path,
) -> None:
    from PIL import Image

    screenshot_a = tmp_path / "dashboard-a.png"
    screenshot_b = tmp_path / "dashboard-b.png"
    Image.new("RGB", (200, 100), "white").save(screenshot_a)
    Image.new("RGB", (200, 100), "white").save(screenshot_b)

    state_a = replace(_state("A", 100), screenshot=screenshot_a)
    state_b = replace(_state("B", 110), screenshot=screenshot_b)
    for state, x in ((state_a, 10), (state_b, 20)):
        state.extracted["dashboard_metadata"] = {
            "image_width": 200,
            "image_height": 100,
            "report_title": f"Report {state.dashboard}",
            "refresh_timestamp": "2025-01-01 12:00",
        }
        state.extracted["elements"][0]["bounding_box"] = {
            "x": x,
            "y": 15,
            "width": 80,
            "height": 30,
        }
        state.extracted["elements"][0]["normalized_bounding_box"] = {
            "x": x / 200,
            "y": 0.15,
            "width": 0.4,
            "height": 0.3,
        }

    result = compare_dashboards([state_a], [state_b])
    result["exported_data_comparisons"] = [
        {
            "state": "Sales|Year=2025",
            "visual": "Revenue",
            "column": "Amount",
            "row": 1,
            "dashboard_a": "PDF_MATCHED_CELL",
            "dashboard_b": "PDF_MATCHED_CELL",
            "status": "MATCH",
        },
        {
            "state": "Sales|Year=2025",
            "visual": "Revenue",
            "column": "Amount",
            "row": 2,
            "dashboard_a": "PDF_EXCEPTION_A",
            "dashboard_b": "PDF_EXCEPTION_B",
            "status": "DIFFERENT",
        },
    ]
    write_comparison(result, tmp_path, "Dashboard A", "Dashboard B")

    report_text = _docx_text(tmp_path / "Dashboard_Comparison.docx")

    assert "PDF_MATCHED_CELL" not in report_text
    assert "PDF_EXCEPTION_A" not in report_text
    assert "PDF_EXCEPTION_B" not in report_text
    assert "Data Export Validation Log" not in report_text
    assert result["comparisons"][0]["bounding_box_a"]["x"] == 10
    assert result["comparisons"][0]["bounding_box_b"]["x"] == 20
    assert result["screenshot_evidence"]["dashboard_a"]["Sales|Year=2025"][
        "image_width"
    ] == 200


def test_exported_table_data_is_not_added_to_visual_validation_workbook(
    tmp_path: Path,
) -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    for state in (state_a, state_b):
        state.extracted["elements"] = []
        state.extracted["title_items"] = [
            {
                "text": "Sales Table",
                "category": "table_title",
                "associated_visual": "Sales Table",
                "reading_order": 1,
            },
            {
                "text": "Metric",
                "category": "column_header",
                "associated_visual": "Sales Table",
                "reading_order": 2,
            },
            {
                "text": "1234",
                "category": "row_header",
                "associated_visual": "Sales Table",
                "reading_order": 3,
            },
        ]
        state.extracted["exported_data"] = [
            {
                "type": "table",
                "title": "Sales Table",
                "categories": ["Metric"],
                "rows": [[1234]],
                "data_sources": ["summarized_export"],
            }
        ]

    result = compare_dashboards([state_a], [state_b])
    assert result["exported_data_comparisons"]
    write_comparison(result, tmp_path, "Dashboard A", "Dashboard B")

    workbook = load_workbook(tmp_path / "Dashboard_Comparison.xlsx", read_only=True)
    assert workbook.sheetnames == ["Visual_Validation"]
    assert any(
        row["dashboard_a"] == 1234 or row["dashboard_b"] == 1234
        for row in result["exported_data_comparisons"]
    )
    workbook.close()


def test_comparison_sheet_is_omitted_but_export_status_is_preserved(
    tmp_path: Path,
) -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    for state, export_status in (
        (state_a, "exported"),
        (state_b, "downloaded_no_tabular_data"),
    ):
        state.extracted["elements"] = [
            {"type": "chart", "title": "Sales trend", "value": 100}
        ]
        state.extracted["export_options"] = [
            {
                "title": "Sales trend",
                "exportOptionAvailable": True,
                "export_status": export_status,
            }
        ]

    result = compare_dashboards([state_a], [state_b])
    row = result["comparisons"][0]
    assert row["export_status_a"] == "exported"
    assert row["export_status_b"] == "downloaded_no_tabular_data"

    write_comparison(result, tmp_path, "Dashboard A", "Dashboard B")
    workbook = load_workbook(tmp_path / "Dashboard_Comparison.xlsx", read_only=True)
    assert workbook.sheetnames == ["Visual_Validation"]
    workbook.close()
    assert row["export_status_a"] == "exported"
    assert row["export_status_b"] == "downloaded_no_tabular_data"

    export_a = _state("A", 100)
    export_b = _state("B", 100)
    for state, value, filename in (
        (export_a, 100, "sales_a.xlsx"),
        (export_b, 120, "sales_b.xlsx"),
    ):
        state.extracted["elements"] = [
            {
                "type": "chart",
                "title": "Sales trend",
                "categories": ["Week", "Sales"],
                "rows": [["Week 1", value]],
                "data_sources": ["summarized_export"],
                "export_file": filename,
                "export_json_file": filename.removesuffix(".xlsx") + ".json",
                "export_status": "exported",
            }
        ]
        state.extracted["exported_data"] = [
            state.extracted["elements"][0].copy()
        ]
        state.extracted["export_options"] = [
            {
                "title": "Sales trend",
                "exportOptionAvailable": True,
                "export_status": "exported",
                "export_data_format": "summarized",
                "export_file": filename,
                "export_json_file": filename.removesuffix(".xlsx") + ".json",
                "screenshot": "initial_exports/menu.png",
            }
        ]
    exported_result = compare_dashboards([export_a], [export_b])
    write_comparison(exported_result, tmp_path / "with_exports", "A", "B")
    exported_workbook = load_workbook(
        tmp_path / "with_exports" / "Dashboard_Comparison.xlsx", read_only=True
    )
    assert exported_workbook.sheetnames == ["Visual_Validation"]
    assert exported_result["exported_data_comparisons"][0]["export_file_a"] == "sales_a.xlsx"
    assert exported_result["exported_data_comparisons"][0]["export_json_file_a"] == "sales_a.json"
    assert any(
        row["dashboard_a"] == 100 and row["dashboard_b"] == 120
        for row in exported_result["exported_data_comparisons"]
    )
    exported_workbook.close()


def test_comparison_sheet_shows_export_attempts_without_visual_extraction(
    tmp_path: Path,
) -> None:
    state_a = _state("A", 100)
    state_b = _state("B", 100)
    state_a.extracted["elements"] = []
    state_b.extracted["elements"] = []
    state_a.extracted["export_options"] = [
        {
            "title": "Revenue chart",
            "element_type": "chart",
            "exportOptionAvailable": True,
            "export_status": "exported",
        }
    ]
    state_b.extracted["export_options"] = [
        {
            "title": "Revenue chart",
            "element_type": "chart",
            "exportOptionAvailable": None,
            "export_status": "more_options_unavailable",
        }
    ]
    state_a.extracted["exported_data"] = [
        {
            "type": "chart",
            "title": "Revenue chart",
            "categories": ["Month", "Revenue"],
            "rows": [["January", 100]],
            "data_sources": ["summarized_export"],
        }
    ]

    result = compare_dashboards([state_a], [state_b])
    write_comparison(result, tmp_path, "Dashboard A", "Dashboard B")

    workbook = load_workbook(tmp_path / "Dashboard_Comparison.xlsx", read_only=True)
    assert workbook.sheetnames == ["Visual_Validation"]
    workbook.close()


def test_docx_report_omits_exported_cell_details(tmp_path: Path) -> None:
    import docx

    def cell(state: str, row: int, status: str, value_b: float) -> dict:
        return {
            "state": state,
            "visual": "Average Queue Time (Minutes)",
            "row": row,
            "column": "Average Time",
            "dashboard_a": 5.691985303,
            "dashboard_b": value_b,
            "status": status,
        }

    result = compare_dashboards([_state("A", 100)], [_state("B", 100)])
    result["exported_data_comparisons"] = [
        cell("performance|initial", 2, "DIFFERENT", 0.123456789),
        cell("performance|initial", 5, "DIFFERENT", 0.302052962),
        cell("performance|initial", 6, "MATCH", 5.691985303),
    ]
    path = tmp_path / "comparison.docx"
    write_docx_report(result, path, "Dashboard A", "Dashboard B")

    text = _docx_text(path)
    document = docx.Document(path)
    assert len(document.tables) == 4
    assert "0.123456789" not in text
    assert "0.302052962" not in text
    assert "Vijay Battini" in text
    assert "Data Export Validation Log" not in text
    assert "DIFFERENT" not in text
    assert "UNVERIFIED" not in text
    assert "Result Status Dropdown (Pass/Fail)" in text
    assert "Fail" in text
    assert "☐" not in text and "☑" not in text
