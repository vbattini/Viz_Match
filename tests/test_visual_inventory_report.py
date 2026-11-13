import asyncio
import json

from docx import Document

from Backend import agents
from Backend.visual_inventory_report import (
    _normalize_inventory_result,
    _screenshots_for_page,
    _visual_check_label,
    _write_report,
)
from Backend.scenario_evidence import EvidenceManifestStore, dashboard_scenario_directory


def _inventory_response():
    dashboard = {
        "report_title": "Inventory Insights",
        "dashboard_title": "Item Overview",
        "filters": [{"name": "Facility", "control_type": "checkbox_dropdown"}],
        "titles": [
            {"role": "dashboard_title", "text": "Item Overview"},
            {"role": "filter_title", "text": "Facility"},
        ],
        "visuals": [
            {
                "title": "Items by Facility",
                "visual_type": "Table",
                "rendering_status": "Rendered",
                "table_headers": ["Item", "Facility"],
            }
        ],
    }
    return {
        "page_name": "All Items",
        "status": "PASS",
        "dashboard_A": dashboard,
        "dashboard_B": dashboard,
        "findings": [
            {
                "element_type": "Filter",
                "status": "PASS",
                "element_a": "Facility",
                "element_b": "Facility",
                "observation": "The filter name and control type match.",
            },
            {
                "element_type": "Visual",
                "status": "PASS",
                "element_a": "Items by Facility (Table)",
                "element_b": "Items by Facility (Table)",
                "observation": "The table is rendered with a matching title and type.",
            },
            {
                "element_type": "Table Header",
                "status": "PASS",
                "element_a": "Item; Facility",
                "element_b": "Item; Facility",
                "observation": "The visible headers match in order.",
            },
        ],
        "notes": [],
    }


def test_visual_inventory_agent_uses_initial_and_scroll_screenshots(monkeypatch):
    class _Agent:
        def __init__(self, **kwargs):
            assert "Do not transcribe" in kwargs["instruction"]
            assert "table body/cell values" in kwargs["instruction"]
            assert "return null rather than inventing one" in kwargs["instruction"]

    async def fake_run(_agent, content, **_kwargs):
        labels = [
            part.text
            for part in content.parts
            if getattr(part, "text", None)
            and part.text.startswith("Screenshot:")
        ]
        assert labels == [
            "Screenshot: Page: All Items | Dashboard A | initial",
            "Screenshot: Page: All Items | Dashboard A | horizontal_scroll_01",
            "Screenshot: Page: All Items | Dashboard B | initial",
        ]
        return json.dumps(_inventory_response())

    monkeypatch.setattr(agents, "_model_name", lambda: "test-model")
    monkeypatch.setattr(agents, "_generation_config", lambda _model: None)
    monkeypatch.setattr(agents, "LlmAgent", _Agent)
    monkeypatch.setattr(agents, "_run_agent", fake_run)

    result = asyncio.run(
        agents.extract_compare_visual_inventory(
            [
                ("Page: All Items | Dashboard A | initial", b"a"),
                ("Page: All Items | Dashboard A | horizontal_scroll_01", b"b"),
                ("Page: All Items | Dashboard B | initial", b"c"),
            ],
            "All Items",
        )
    )

    assert result["dashboard_A"]["filters"][0]["name"] == "Facility"
    assert result["dashboard_A"]["visuals"][0]["table_headers"] == [
        "Item",
        "Facility",
    ]
    assert "selected_values" not in json.dumps(result)
    assert "123.45" not in json.dumps(result)


def test_visual_inventory_agent_requires_initial_screenshots():
    try:
        asyncio.run(
            agents.extract_compare_visual_inventory(
                [("Page: All Items | Dashboard A | initial", b"a")],
                "All Items",
            )
        )
    except ValueError as exc:
        assert "Dashboard B" in str(exc)
    else:
        raise AssertionError("Expected missing initial screenshot to fail")


def test_visual_inventory_reads_only_scenario_01_manifest_screenshots(tmp_path):
    store = EvidenceManifestStore(tmp_path)
    store.initialize()

    async def add(side, scenario_id, position, contents):
        directory = dashboard_scenario_directory(
            tmp_path, side, scenario_id, "Page One"
        )
        directory.mkdir(parents=True, exist_ok=True)
        screenshot = directory / f"{side}_{position}.png"
        screenshot.write_bytes(contents)
        await store.add(
            {
                "dashboard_id": side,
                "page_name": "Page One",
                "scenario_id": scenario_id,
                "scroll_position_id": position,
                "capture_status": "CAPTURED",
                "screenshot_filename": screenshot.name,
                "screenshot_full_path": str(screenshot.resolve()),
            }
        )

    async def populate():
        for side in ("dashboard_A", "dashboard_B"):
            await add(side, "01", "initial", f"{side}:initial".encode())
            if side == "dashboard_A":
                await add(side, "01", "horizontal_scroll_01", b"scroll")
            await add(side, "02", "single_filter", b"must not be included")

    asyncio.run(populate())

    screenshots, evidence, errors = _screenshots_for_page(
        "Page One", tmp_path
    )

    assert [label for label, _ in screenshots] == [
        "Page: Page One | Dashboard A | initial",
        "Page: Page One | Dashboard A | horizontal_scroll_01",
        "Page: Page One | Dashboard B | initial",
    ]
    assert "must not be included" not in str([image for _, image in screenshots])
    assert evidence["dashboard_A"]["initial"]["path"].endswith("dashboard_A_initial.png")
    assert errors == []


def test_template_report_logs_names_titles_and_headers_without_values(tmp_path):
    page = _inventory_response()
    page["evidence"] = {
        "dashboard_A_screenshots": {},
        "dashboard_B_screenshots": {},
        "errors": [],
    }
    path = tmp_path / "vision_inventory.docx"

    _write_report(path, [page], "Inventory Insights")

    document = Document(path)
    text = "\n".join(
        [paragraph.text for paragraph in document.paragraphs]
        + [cell.text for table in document.tables for row in table.rows for cell in row.cells]
    )
    assert "Visible filter names / types (values omitted): Facility" in text
    assert "Items by Facility (Table)" in text
    assert "Item; Facility" in text
    assert "123.45" not in text
    assert document.tables[0].cell(1, 1).text == ""
    assert len(document.tables[1].rows) == 2
    assert len(document.tables[2].rows) == 3


def test_untitled_table_visual_report_uses_type_and_rendering_status():
    page = {
        "dashboard_A": {
            "visuals": [
                {
                    "title": "",
                    "visual_type": "Matrix",
                    "rendering_status": "Rendered",
                }
            ]
        },
        "dashboard_B": {
            "visuals": [
                {
                    "title": "",
                    "visual_type": "Matrix",
                    "rendering_status": "Error",
                }
            ]
        },
    }

    label = _visual_check_label(
        page,
        {
            "element_a": "Matrix visual",
            "element_b": "Matrix visual",
        },
    )

    assert label == "Visual: Matrix - Dashboard A: Rendered; Dashboard B: Error"


def test_unrendered_table_headers_are_unverified_not_missing():
    page = _inventory_response()
    page["dashboard_A"]["visuals"][0]["title"] = ""
    page["dashboard_A"]["visuals"][0]["visual_type"] = "Table"
    page["dashboard_B"]["visuals"][0]["title"] = ""
    page["dashboard_B"]["visuals"][0]["visual_type"] = "Table"
    page["dashboard_B"]["visuals"][0]["rendering_status"] = "Error"
    page["findings"].append(
        {
            "element_type": "Table Header",
            "status": "FAIL",
            "element_a": "Item; Facility",
            "element_b": None,
            "observation": "The table headers are missing on Dashboard B.",
        }
    )

    normalized = _normalize_inventory_result(page)

    header_finding = [
        item
        for item in normalized["findings"]
        if item["element_type"] == "Table Header"
    ][0]
    assert normalized["dashboard_A"]["visuals"][0]["title"] is None
    assert header_finding["status"] == "UNVERIFIED"
    assert normalized["status"] == "UNVERIFIED"
