import asyncio
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from powerbi_dashboard_comparator import browser
from powerbi_dashboard_comparator.models import (
    DashboardState,
    FilterCombination,
    FilterSelection,
)


def test_comparison_page_names_exclude_home_and_view() -> None:
    assert browser._comparison_page_names(
        ["Home", "VIEW", "File", "Summary", "Compliance"]
    ) == ["Summary", "Compliance"]

    assert browser._comparison_page_names(["Home", "View"]) == ["Report"]
    assert browser._comparison_page_names(["File", "Home", "View"]) == ["Report"]


def test_screenshot_detection_runs_once_and_reuses_results_across_page_states(
    monkeypatch, tmp_path: Path
) -> None:
    screenshot = tmp_path / "reference.png"
    screenshot.write_bytes(b"reference screenshot")
    reference = DashboardState(
        dashboard="Dashboard A",
        page="Orders",
        filter=None,
        screenshot=screenshot,
        extracted={},
        visuals=[
            {
                "visual_id": "TABLE_001",
                "visual_type": "Unknown",
                "x": 20,
                "y": 30,
                "width": 400,
                "height": 250,
            }
        ],
    )
    filtered = DashboardState(
        dashboard="Dashboard A",
        page="Orders",
        filter=FilterSelection("Facility", "North"),
        screenshot=Path("filtered.png"),
        extracted={},
        visuals=[
            {
                "visual_id": "VISUAL_001",
                "visual_type": None,
                "x": 20,
                "y": 30,
                "width": 400,
                "height": 250,
            }
        ],
    )
    calls = []

    async def detect(image, page_name, visuals, **kwargs):
        calls.append((image, page_name, visuals, kwargs))
        return [
            {
                "visual_id": "TABLE_001",
                "visual_name": "Open Orders",
                "visual_type": "Table",
                "rendering_status": "Rendered",
                "confidence": "High",
                "observation": "A titled grid is visible.",
                "bounds": {"x": 20, "y": 30, "width": 400, "height": 250},
            }
        ]

    monkeypatch.setattr(
        browser, "detect_visual_types_from_screenshot", detect
    )
    asyncio.run(
        browser._detect_and_apply_page_visual_types(
            [reference, filtered],
            reference,
            dashboard="Dashboard A",
            page_name="Orders",
            job_id="job-1",
            operation_log_path=None,
        )
    )

    assert len(calls) == 1
    assert calls[0][0] == b"reference screenshot"
    assert calls[0][1] == "Orders"
    assert reference.visuals[0]["visual_name"] == "Open Orders"
    assert filtered.visuals[0]["visual_name"] == "Open Orders"
    assert filtered.visuals[0]["visual_type"] == "Table"
    assert filtered.visuals[0]["rendering_status"] == "Unable to Verify"
    assert filtered.extracted["visual_type_detection"]["source_state_key"] == "Orders|initial"


def test_partial_visual_type_detection_is_marked_unverified() -> None:
    reference = DashboardState(
        dashboard="Dashboard A",
        page="Orders",
        filter=None,
        screenshot=Path("reference.png"),
        extracted={},
        visuals=[
            {
                "visual_id": "TABLE_001",
                "visual_name": "Orders",
                "visual_type": None,
                "x": 100,
                "y": 100,
                "width": 300,
                "height": 200,
            },
            {
                "visual_id": "CHART_001",
                "visual_name": "Sales",
                "visual_type": None,
                "x": 500,
                "y": 100,
                "width": 300,
                "height": 200,
            },
        ],
    )
    detections = [
        {
            "visual_id": "TABLE_001",
            "visual_name": "Orders",
            "visual_type": "Table",
            "rendering_status": "Rendered",
            "confidence": "High",
            "observation": "A titled grid is visible.",
            "bounds": {"x": 100, "y": 100, "width": 300, "height": 200},
        }
    ]

    browser._apply_visual_type_detections([reference], reference, detections)

    assert reference.extracted["visual_type_detection"]["status"] == "partial"
    assert reference.extracted["visual_type_detection"][
        "unclassified_visual_ids"
    ] == ["CHART_001"]


def test_screenshot_only_visuals_are_added_with_projected_geometry() -> None:
    reference = DashboardState(
        dashboard="Dashboard A",
        page="Summary",
        filter=None,
        screenshot=Path("reference.png"),
        extracted={},
        visuals=[
            {
                "visual_id": "TEXT_001",
                "visual_type": "Text Box",
                "x": 100,
                "y": 100,
                "width": 100,
                "height": 100,
            },
            {
                "visual_id": "KPI_001",
                "visual_type": "KPI",
                "x": 900,
                "y": 500,
                "width": 100,
                "height": 100,
            },
        ],
    )
    filtered = DashboardState(
        dashboard="Dashboard A",
        page="Summary",
        filter=FilterSelection("Facility", "North"),
        screenshot=Path("filtered.png"),
        extracted={},
        visuals=[
            {
                "visual_id": "TEXT_001",
                "visual_type": "Text Box",
                "x": 100,
                "y": 100,
                "width": 100,
                "height": 100,
            },
            {
                "visual_id": "KPI_001",
                "visual_type": "KPI",
                "x": 900,
                "y": 500,
                "width": 100,
                "height": 100,
            },
        ],
    )
    detections = [
        {
            "visual_id": "TEXT_001",
            "visual_name": "Report Title",
            "visual_type": "Text Box",
            "rendering_status": "Rendered",
            "confidence": "High",
            "observation": "A report title is visible.",
            "bounds": {"x": 100, "y": 100, "width": 100, "height": 100},
        },
        {
            "visual_id": "KPI_001",
            "visual_name": "Orders KPI",
            "visual_type": "KPI",
            "rendering_status": "Rendered",
            "confidence": "High",
            "observation": "A KPI card is visible.",
            "bounds": {"x": 900, "y": 500, "width": 100, "height": 100},
        },
        {
            "visual_id": "SCREENSHOT_001",
            "visual_name": "Orders by Facility",
            "visual_type": "Bar Chart",
            "rendering_status": "Rendered",
            "confidence": "High",
            "observation": "A horizontal bar chart is visible.",
            "bounds": {"x": 300, "y": 300, "width": 400, "height": 200},
        },
    ]

    browser._apply_visual_type_detections(
        [reference, filtered], reference, detections
    )

    screenshot_visual = reference.visuals[-1]
    assert screenshot_visual["visual_id"] == "SCREENSHOT_001"
    assert screenshot_visual["visual_name"] == "Orders by Facility"
    assert screenshot_visual["visual_type"] == "Bar Chart"
    assert screenshot_visual["rendering_status"] == "Rendered"
    assert (
        screenshot_visual["x"],
        screenshot_visual["y"],
        screenshot_visual["width"],
        screenshot_visual["height"],
    ) == (300, 300, 400, 200)
    assert filtered.visuals[-1]["visual_id"] == "SCREENSHOT_001"
    assert filtered.visuals[-1]["rendering_status"] == "Unable to Verify"


def test_wait_for_comparable_report_tabs_retries_application_tabs(
    monkeypatch,
) -> None:
    tab_names = iter(
        [
            ["File", "Home", "View"],
            ["File", "Home", "View", "Summary"],
        ]
    )
    waits = []

    async def accessible_tabs(_frame):
        return next(tab_names)

    class Page:
        frames = [object()]

        async def wait_for_timeout(self, duration):
            waits.append(duration)
            await asyncio.sleep(0)

    monkeypatch.setattr(
        browser, "_accessible_tab_names", accessible_tabs
    )

    result = asyncio.run(browser._wait_for_comparable_report_tabs(Page()))

    assert result == ["File", "Home", "View", "Summary"]
    assert waits == [250]


def test_report_ready_accepts_visible_visual_without_slicer_controls(
    monkeypatch,
) -> None:
    monkeypatch.setattr(browser, "REPORT_STABLE_SECONDS", 0.0)

    class Page:
        async def wait_for_function(self, script, *, timeout, polling):
            assert timeout == browser.REPORT_READY_TIMEOUT_MS
            assert polling == 500
            assert "reportTabs" not in script
            assert "div.visualContainer, [data-visual-id]" in script
            assert "'[role=\"combobox\"], select'" in script

        async def evaluate(self, script):
            return {
                "elements": 10,
                "drawn": 2,
                "busy": 0,
                "content": "stable",
            }

        async def wait_for_timeout(self, duration):
            assert duration == browser.REPORT_POLL_MS

    asyncio.run(browser._wait_for_report_ready(Page()))


def test_report_ready_requires_rendered_visual_content(monkeypatch) -> None:
    monkeypatch.setattr(browser, "REPORT_STABLE_SECONDS", 0.0)

    class Page:
        async def wait_for_function(self, script, *, timeout, polling):
            assert "svg, canvas, img, table" in script
            assert 'aria-busy="true"' in script
            assert "innerText" in script
            assert "visuals.length === 0" in script
            assert "available resources" in script

        async def evaluate(self, script):
            assert '[class*="spinner" i]' in script
            assert 'spinner[title*="loading" i]' in script
            assert '[class*="loading" i]' in script
            assert "main.querySelectorAll(selector)" in script
            assert "visual.querySelectorAll('img')" in script
            assert "getAnimations({ subtree: true })" in script
            assert "/refresh|reload/i" in script
            assert "timing?.iterations !== Infinity" in script
            assert "visualLoadingCandidates" in script
            assert "animation.effect?.getTiming().iterations === Infinity" in script
            assert "visual_loading_markers" in script
            assert "busy_details" in script
            assert "render_errors" in script
            assert "query_resource_exceeded" in script
            assert "root.innerText" in script
            assert "visuals: visuals.length" in script
            assert "content: (contentHash >>> 0).toString(16)" in script
            return {
                "elements": 10,
                "drawn": 2,
                "busy": 0,
                "content": "stable",
            }

        async def wait_for_timeout(self, duration):
            pass

    asyncio.run(browser._wait_for_report_ready(Page()))


def test_report_load_waits_while_dom_changes_or_busy(monkeypatch) -> None:
    monkeypatch.setattr(browser, "REPORT_STABLE_SECONDS", 0.05)
    clock = [0.0]
    states = iter(
        [
            {"elements": 10, "drawn": 0, "busy": 0, "content": "a"},
            {"elements": 20, "drawn": 1, "busy": 0, "content": "b"},
            {"elements": 20, "drawn": 1, "busy": 1, "content": "b"},
            {"elements": 20, "drawn": 1, "busy": 0, "content": "b"},
        ]
    )
    last = {"elements": 20, "drawn": 1, "busy": 0, "content": "b"}
    polls: list[int] = []

    class Page:
        async def evaluate(self, _script):
            nonlocal last
            try:
                last = next(states)
            except StopIteration:
                pass
            return last

        async def wait_for_timeout(self, duration):
            polls.append(duration)

    asyncio.run(browser._wait_for_report_load_to_settle(Page()))

    assert len(polls) >= 4


def test_report_load_waits_for_text_updates_even_when_dom_is_stable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(browser, "REPORT_STABLE_SECONDS", 0.05)
    clock = [0.0]
    states = iter(
        [
            {"elements": 20, "drawn": 1, "busy": 0, "content": "headers"},
            {"elements": 20, "drawn": 1, "busy": 0, "content": "headers"},
            {"elements": 20, "drawn": 1, "busy": 0, "content": "loaded rows"},
        ]
    )
    polls: list[int] = []

    class Page:
        async def evaluate(self, _script):
            try:
                return next(states)
            except StopIteration:
                return {
                    "elements": 20,
                    "drawn": 1,
                    "busy": 0,
                    "content": "loaded rows",
                }

        async def wait_for_timeout(self, duration):
            polls.append(duration)
            clock[0] += 0.02

    monkeypatch.setattr(browser.time, "monotonic", lambda: clock[0])
    asyncio.run(browser._wait_for_report_load_to_settle(Page()))

    assert len(polls) >= 5


def test_report_load_times_out_when_never_settles(monkeypatch) -> None:
    monkeypatch.setattr(browser, "REPORT_READY_TIMEOUT_MS", 0)

    class Page:
        async def evaluate(self, _script):
            return {
                "elements": 10,
                "drawn": 0,
                "busy": 1,
                "content": "loading",
            }

        async def wait_for_timeout(self, duration):
            pass

    with pytest.raises(RuntimeError, match="did not finish loading"):
        asyncio.run(browser._wait_for_report_load_to_settle(Page()))


def test_report_ready_timeout_includes_observed_dom_state() -> None:
    diagnostics = {
        "title": "HR Now Report",
        "readyState": "complete",
        "mainVisible": True,
        "visibleTabs": 6,
        "visibleReportTabs": [],
        "visibleComboboxes": 0,
        "visibleVisuals": 0,
        "pageStateHints": [],
    }

    class Page:
        async def wait_for_function(self, _script, *, timeout, polling):
            assert timeout == browser.REPORT_READY_TIMEOUT_MS
            assert polling == 500
            raise browser.PlaywrightTimeoutError("timed out")

        async def evaluate(self, script):
            assert "visibleReportTabs" in script
            return diagnostics

    with pytest.raises(
        RuntimeError,
        match="observed page state: .*visibleComboboxes",
    ):
        asyncio.run(browser._wait_for_report_ready(Page()))


def test_filter_option_inventory_does_not_truncate_large_dropdowns() -> None:
    class Options:
        async def evaluate_all(self, _script):
            return [f"Option {index}" for index in range(250)]

    values = asyncio.run(browser._accessible_option_texts(Options()))

    assert len(values) == 250
    assert values[-1] == "Option 249"


@pytest.mark.parametrize(
    ("page_count", "expected_workers"),
    [(1, 1), (2, 2), (4, 4), (5, 5), (8, 8), (12, browser.MAX_PARALLEL_PAGE_CAPTURES)],
)
def test_page_capture_batch_size_scales_with_available_pages(
    page_count: int, expected_workers: int
) -> None:
    assert browser._page_worker_batch_size(page_count) == expected_workers


def test_identical_screenshots_reuse_gemini_extraction(monkeypatch) -> None:
    calls = []

    async def extract(screenshot_bytes: bytes, page_name: str) -> dict:
        calls.append((screenshot_bytes, page_name))
        return {"page": page_name, "elements": []}

    monkeypatch.setattr(browser, "extract_screenshot", extract)

    async def run():
        cache = {}
        performance = {}
        first = await browser._extract_or_reuse(
            b"same screenshot",
            "Report",
            cache,
            performance_seconds=performance,
        )
        second = await browser._extract_or_reuse(
            b"same screenshot",
            "Report",
            cache,
            performance_seconds=performance,
        )
        return first, second, performance

    first, second, performance = asyncio.run(run())

    assert first is second
    assert len(calls) == 1
    assert performance["extraction_model_seconds"] >= 0
    assert performance["extraction_cache_hits"] == 1


def test_gemini_extraction_is_logged_as_a_separate_operation(
    monkeypatch, tmp_path: Path
) -> None:
    async def extract(
        screenshot_bytes: bytes, page_name: str, **_kwargs
    ) -> dict:
        return {"page": page_name, "elements": []}

    monkeypatch.setattr(browser, "extract_screenshot", extract)
    operation_log_path = tmp_path / "operation_timings.jsonl"
    asyncio.run(
        browser._extract_or_reuse(
            b"screenshot",
            "Summary",
            {},
            dashboard="dashboard_A",
            job_id="timing-job",
            operation_log_path=operation_log_path,
            page="Summary",
            filter_state="initial",
        )
    )

    events = [
        json.loads(line)
        for line in operation_log_path.read_text(encoding="utf-8").splitlines()
    ]
    gemini_events = [
        event
        for event in events
        if event["operation"] == "gemini_screenshot_extraction"
    ]
    assert [event["event"] for event in gemini_events] == [
        "started",
        "finished",
    ]
    assert gemini_events[-1]["duration_seconds"] >= 0
    assert all(
        event["job_id"] == "timing-job"
        and event["dashboard"] == "dashboard_A"
        and event["page"] == "Summary"
        and event["filter_state"] == "initial"
        for event in gemini_events
    )


def test_extraction_cache_includes_exported_visual_context(monkeypatch) -> None:
    calls = []

    async def extract(
        screenshot_bytes: bytes,
        page_name: str,
        exported_visual_titles: list[str] | None = None,
    ) -> dict:
        calls.append(exported_visual_titles)
        return {"page": page_name, "elements": []}

    monkeypatch.setattr(browser, "extract_screenshot", extract)

    async def run():
        cache = {}
        first = await browser._extract_or_reuse(
            b"same screenshot", "Report", cache, ["Sales trend"]
        )
        second = await browser._extract_or_reuse(
            b"same screenshot", "Report", cache, ["Sales trend"]
        )
        third = await browser._extract_or_reuse(
            b"same screenshot", "Report", cache, ["Orders"]
        )
        return first, second, third

    first, second, third = asyncio.run(run())

    assert first is second
    assert third is not first
    assert calls == [["Sales trend"], ["Orders"]]


def test_exported_visual_highlight_marks_only_requested_visuals() -> None:
    scripts = []
    active_masks = 0
    max_active_masks = 0

    class Visual:
        def __init__(self, title, visual_id):
            self.title = title
            self.visual_id = visual_id

        async def is_visible(self):
            return True

        async def evaluate(self, script, config=None):
            nonlocal active_masks, max_active_masks
            if config is None:
                return {
                    "title": self.title,
                    "visualId": self.visual_id,
                }
            active_masks += 1
            max_active_masks = max(max_active_masks, active_masks)
            await asyncio.sleep(0.01)
            scripts.append((script, config))
            active_masks -= 1
            return True

    class VisualLocator:
        visuals = [
            Visual("sales trend", "sales-id"),
            Visual("orders", "orders-id"),
        ]

        async def count(self):
            return len(self.visuals)

        def nth(self, index):
            return self.visuals[index]

    class HighlightLocator:
        async def evaluate_all(self, script):
            scripts.append((script, None))

    class Frame:
        def locator(self, selector):
            if selector == browser.VISUAL_SELECTOR:
                return VisualLocator()
            return HighlightLocator()

    class Page:
        frames = [Frame()]

    async def highlight_and_clear():
        marker = await browser._highlight_exported_visuals(
            Page(), ["  Sales   trend ", "Orders"]
        )
        assert marker is not None
        await browser._clear_exported_visual_highlights(Page(), marker)
        return marker

    marker = asyncio.run(highlight_and_clear())

    mask_scripts = [(script, config) for script, config in scripts if config]
    assert len(mask_scripts) == 2
    assert [config["titleKey"] for _, config in mask_scripts] == [
        "sales trend",
        "orders",
    ]
    marker_script, config = mask_scripts[0]
    assert "background: '#ffffff'" in marker_script
    assert "titleBounds" in marker_script
    assert "titleBounds.bottom + 2" in marker_script
    assert "border: '2px solid #ffffff'" in marker_script
    assert "titleKey" in marker_script
    assert "pointerEvents: 'none'" in marker_script
    assert config["titleKey"] == "sales trend"
    assert marker in config["markerAttribute"]
    cleanup_script, _ = next(
        (script, config)
        for script, config in scripts
        if config is None
    )
    assert "element.remove()" in cleanup_script
    assert max_active_masks == 1


def test_exported_visual_highlight_fails_when_a_visual_cannot_be_masked() -> None:
    with pytest.raises(RuntimeError, match="Could not uniquely locate exported visual"):
        asyncio.run(
            browser._highlight_exported_visuals(
                SimpleNamespace(frames=[]), ["Orders"]
            )
        )


def test_exported_screenshot_visual_values_are_marked_and_removed() -> None:
    extracted = {
        "elements": [
            {
                "type": "chart",
                "title": "Sales trend",
                "value": 100,
                "categories": ["Week 1"],
                "values": [100],
                "rows": [["Week 1", 100]],
                "content": {"rows": [["Week 1", 100]]},
                "extraction_status": "complete",
            },
            {
                "type": "chart",
                "title": "Orders",
                "value": 25,
                "extraction_status": "complete",
            },
        ],
        "extraction_summary": {},
    }

    browser._mark_exported_screenshot_visuals(
        extracted, [{"title": "Sales trend"}]
    )

    sales = extracted["elements"][0]
    assert sales["screenshot_value_comparison_skipped"] is True
    assert sales["extraction_status"] == "skipped_exported"
    assert sales["value"] is None
    assert sales["categories"] == []
    assert sales["values"] == []
    assert sales["rows"] == []
    assert sales["content"] == {}
    assert extracted["elements"][1]["value"] == 25
    assert extracted["extraction_summary"]["skipped_element_count"] == 1
    assert extracted["extraction_summary"]["requires_additional_screenshots"] is False


def test_export_option_without_parsed_data_uses_screenshot_fallback() -> None:
    extracted = {
        "elements": [
            {
                "type": "chart",
                "title": "Orders",
                "value": 25,
                "categories": ["Week 1"],
                "values": [25],
                "rows": [["Week 1", 25]],
                "content": {"rows": [["Week 1", 25]]},
                "extraction_status": "complete",
            }
        ],
        "extraction_summary": {},
    }

    browser._mark_exported_screenshot_visuals(
        extracted,
        [],
        [
            {
                "title": "Orders",
                "exportOptionAvailable": True,
                "export_status": "downloaded_no_tabular_data",
            }
        ],
    )

    orders = extracted["elements"][0]
    assert "screenshot_value_comparison_skipped" not in orders
    assert orders["extraction_status"] == "partial"
    assert orders["value"] == 25
    assert orders["content"] == {"rows": [["Week 1", 25]]}
    assert "screenshot extraction was used as fallback" in orders["uncertainties"][0]
    assert extracted["extraction_summary"]["partial_element_count"] == 1


def test_capture_uses_screenshot_fallback_when_export_times_out(
    monkeypatch, tmp_path: Path
) -> None:
    events = []
    export_options = [
        {
            "title": "Orders",
            "exportOptionAvailable": True,
            "export_status": "download_timeout",
            "export_data_format": "summarized",
            "export_error": "No export download arrived",
            "export_file": None,
            "screenshot": None,
            "export_dialog_screenshot": None,
        }
    ]

    async def no_wait(*_args):
        return None

    async def render_wait(_page, _settle_seconds, **_kwargs):
        events.append("render-wait")

    async def export(_page, _directory, performance_timings=None):
        assert performance_timings == []
        events.append("export")
        return [], False, export_options

    async def dismiss(_page):
        events.append("dismiss")

    async def clear_masks(_page, _marker):
        events.append("clear-masks")

    async def highlight(_page, titles):
        events.append(("highlight", titles))
        return "highlight-marker"

    async def screenshot(*, path, full_page):
        events.append(("screenshot", Path(path).name, full_page))
        return b"clean screenshot"

    async def extract(
        _image, _page_name, _cache, titles, _performance_seconds=None
    ):
        events.append(("extract", titles))
        return {
            "elements": [
                {"type": "chart", "title": "Orders", "value": 25}
            ]
        }

    async def read_filters(*_args):
        return {}

    monkeypatch.setattr(browser, "_wait_for_state_render", render_wait)
    monkeypatch.setattr(browser, "_move_pointer_off_dashboard", no_wait)
    monkeypatch.setattr(browser, "_export_visual_data", export)
    monkeypatch.setattr(browser, "_dismiss_export_success_toasts", dismiss)
    monkeypatch.setattr(browser, "_clear_exported_visual_highlights", clear_masks)
    monkeypatch.setattr(browser, "_highlight_exported_visuals", highlight)
    monkeypatch.setattr(browser, "_extract_or_reuse", extract)
    monkeypatch.setattr(browser, "_read_filter_values", read_filters)
    page = SimpleNamespace(
        screenshot=screenshot,
        frames=[],
    )

    state = asyncio.run(
        browser._capture_state(
            page,
            "Dashboard",
            "Overview",
            None,
            tmp_path,
            0,
            {},
        )
    )

    assert events[:7] == [
        "render-wait",
        "export",
        "dismiss",
        "render-wait",
        ("screenshot", "initial.png", True),
        ("extract", []),
    ]
    assert events.count("render-wait") == 2
    assert events.index("dismiss") < events.index("render-wait", 1)
    assert events.index("render-wait", 1) < events.index(
        ("screenshot", "initial.png", True)
    )
    assert events.count("clear-masks") == 0
    assert state.extracted["elements"][0]["extraction_status"] == "partial"


def test_layout_and_filter_capture_skips_visual_value_extraction(monkeypatch, tmp_path):
    async def no_wait(*_args, **_kwargs):
        return None

    async def screenshot(*, path, full_page):
        assert full_page
        Path(path).write_bytes(b"filter state screenshot")
        return b"filter state screenshot"

    async def forbidden_extract(*_args, **_kwargs):
        raise AssertionError("visual extraction must be skipped")

    async def read_filters(_page, inventory):
        assert inventory == [
            {
                "name": "Year",
                "values": ["2024", "2025"],
                "control_type": "radio_dropdown",
            }
        ]
        return {"Year": ["2025"]}

    async def read_visuals(_page, _inventory):
        return [
            {
                "visual_id": "visual-1",
                "visual_name": "Orders",
                "visual_type": "Bar Chart",
                "x": 100,
                "y": 200,
                "width": 300,
                "height": 400,
            }
        ]

    async def read_artifact_title(script):
        assert '[data-testid="artifact-info-title"]' in script
        return "  Inventory   Insights  "

    monkeypatch.setattr(browser, "_wait_for_state_render", no_wait)
    monkeypatch.setattr(browser, "_move_pointer_off_dashboard", no_wait)
    monkeypatch.setattr(browser, "_extract_or_reuse", forbidden_extract)
    monkeypatch.setattr(browser, "_read_filter_values", read_filters)
    monkeypatch.setattr(browser, "_read_visual_layout_inventory", read_visuals)

    state = asyncio.run(
        browser._capture_state(
            SimpleNamespace(
                screenshot=screenshot,
                frames=[],
                evaluate=read_artifact_title,
            ),
            "Dashboard A",
            "Overview",
            None,
            tmp_path,
            0,
            {},
            filter_inventory=[
                {
                    "name": "Year",
                    "values": ["2024", "2025"],
                    "control_type": "radio_dropdown",
                }
            ],
            extract_visual_data=False,
        )
    )

    assert state.screenshot.is_file()
    assert state.extracted["dashboard_metadata"]["report_title"] == (
        "Inventory Insights"
    )
    assert state.filter_values == {"Year": ["2025"]}
    assert state.visuals == [
        {
            "visual_id": "visual-1",
            "visual_name": "Orders",
            "visual_type": "Bar Chart",
            "x": 100,
            "y": 200,
            "width": 300,
            "height": 400,
        }
    ]
    assert state.filter_options == {"Year": ["2024", "2025"]}
    assert state.filter_control_types == {"Year": "radio_dropdown"}
    assert state.extracted["capture_mode"] == "visual_layout_filters"
    assert state.extracted["elements"] == []
    assert [
        (item["category"], item["text"], item["associated_visual"])
        for item in state.extracted["title_items"]
    ] == [("slicer_value", "2025", "Year")]


def test_dom_title_inventory_keeps_only_titles_headers_metadata_and_filters():
    class Frame:
        url = "https://report-frame.invalid"

        async def evaluate(self, _script, _filter_names):
            return {
                "items": [
                    {
                        "text": "Orders by Facility",
                        "category": "dashboard_title",
                        "associated_visual_dom_id": None,
                        "bounding_box": {"x": 20, "y": 10, "width": 200, "height": 20},
                        "normalized_bounding_box": {
                            "x": 0.01,
                            "y": 0.01,
                            "width": 0.1,
                            "height": 0.02,
                        },
                    },
                    {
                        "text": "Data Refresh Date : 11/3/2026 6:37:54 AM",
                        "category": "metadata_label",
                        "associated_visual_dom_id": None,
                        "bounding_box": {"x": 20, "y": 40, "width": 300, "height": 20},
                        "normalized_bounding_box": {
                            "x": 0.01,
                            "y": 0.04,
                            "width": 0.15,
                            "height": 0.02,
                        },
                    },
                    {
                        "text": "Facility Nbr",
                        "category": "column_header",
                        "associated_visual_dom_id": "table-dom-id",
                        "bounding_box": {"x": 200, "y": 250, "width": 100, "height": 20},
                        "normalized_bounding_box": {
                            "x": 0.1,
                            "y": 0.25,
                            "width": 0.05,
                            "height": 0.02,
                        },
                    },
                ],
                "filter_bounds": [
                    {
                        "name": "Calendar",
                        "bounding_box": {
                            "x": 20,
                            "y": 100,
                            "width": 150,
                            "height": 30,
                        },
                        "normalized_bounding_box": {
                            "x": 0.01,
                            "y": 0.1,
                            "width": 0.08,
                            "height": 0.03,
                        },
                    }
                ],
            }

    items, refresh_timestamp = asyncio.run(
        browser._read_dom_title_items(
            SimpleNamespace(frames=[Frame()]),
            [{"dom_visual_id": "table-dom-id", "visual_id": "TABLE_001"}],
            [{"name": "Calendar"}],
            {"Calendar": ["All"]},
        )
    )

    assert refresh_timestamp == "11/3/2026 6:37:54 AM"
    assert [
        (item["category"], item["text"], item["associated_visual"])
        for item in items
    ] == [
        ("dashboard_title", "Orders by Facility", None),
        ("metadata_label", "Data Refresh Date : 11/3/2026 6:37:54 AM", None),
        ("slicer_value", "All", "Calendar"),
        ("column_header", "Facility Nbr", "TABLE_001"),
    ]


def test_dom_title_script_defines_visibility_before_root_lookup():
    script = browser._TITLE_LABEL_SCRIPT

    assert script.index("const visible =") < script.index(".find(visible)")


def test_accessible_slicer_values_fill_missing_title_items_without_collapsing_repeats():
    title_items = [
        {
            "title_id": "TITLE_001",
            "text": "Orders",
            "category": "chart_title",
            "reading_order": 1,
        },
        {
            "title_id": "TITLE_002",
            "text": "All",
            "category": "slicer_value",
            "associated_visual": "Calendar",
            "reading_order": 2,
        },
    ]

    merged, added = browser._merge_accessible_filter_values(
        title_items,
        {
            "Calendar": ["All"],
            "Facility": ["All", "All"],
            "Minimum value": ["0"],
        },
    )

    slicer_values = [
        item
        for item in merged
        if item.get("category") == "slicer_value"
    ]
    assert added == 3
    assert [
        (item["associated_visual"], item["text"])
        for item in slicer_values
    ] == [
        ("Calendar", "All"),
        ("Facility", "All"),
        ("Facility", "All"),
        ("Minimum value", "0"),
    ]
    assert all(
        item["evidence_sources"] == ["accessible_filter"]
        for item in slicer_values[1:]
    )


def test_mask_failure_does_not_abort_capture(monkeypatch, tmp_path: Path) -> None:
    exported = [
        {
            "type": "chart",
            "title": "Orders",
            "categories": ["Week"],
            "rows": [["Week 1", 25]],
            "exportOptionAvailable": True,
            "export_status": "exported",
            "export_data_format": "summarized",
            "export_file": "orders.xlsx",
            "export_file_path": "initial_exports/orders.xlsx",
            "export_json_file": "initial_exports/orders.json",
            "data_sources": ["powerbi_export"],
            "visual_id": "orders-visual",
            "visual_index": 0,
            "page_name": "Overview",
            "filter_state": "initial",
            "mask_status": "pending",
        }
    ]
    export_options = [
        {
            "title": "Orders",
            "visual_index": 0,
            "page_name": "Overview",
            "filter_state": "initial",
            "exportOptionAvailable": True,
            "export_status": "exported",
            "export_data_format": "summarized",
            "export_file": "orders.xlsx",
            "export_file_path": "initial_exports/orders.xlsx",
            "export_json_file": "initial_exports/orders.json",
            "screenshot": None,
            "export_dialog_screenshot": None,
            "mask_status": "pending",
        }
    ]

    async def no_wait(*_args, **_kwargs):
        return None

    async def export(*_args, **_kwargs):
        return exported, False, export_options

    async def fail_mask(*_args, **_kwargs):
        raise RuntimeError("visual moved before masking")

    async def screenshot(*, path, full_page):
        assert full_page
        Path(path).write_bytes(b"unmasked screenshot")
        return b"unmasked screenshot"

    async def extract(_image, _page_name, _cache, titles, *_args, **_kwargs):
        assert titles == ["Orders"]
        return {
            "elements": [
                {
                    "type": "chart",
                    "title": "Orders",
                    "value": 25,
                    "extraction_status": "complete",
                }
            ]
        }

    async def read_filters(*_args):
        return {}

    monkeypatch.setattr(browser, "_wait_for_state_render", no_wait)
    monkeypatch.setattr(browser, "_move_pointer_off_dashboard", no_wait)
    monkeypatch.setattr(browser, "_export_visual_data", export)
    monkeypatch.setattr(browser, "_dismiss_export_success_toasts", no_wait)
    monkeypatch.setattr(browser, "_highlight_exported_visuals", fail_mask)
    monkeypatch.setattr(browser, "_extract_or_reuse", extract)
    monkeypatch.setattr(browser, "_read_filter_values", read_filters)

    state = asyncio.run(
        browser._capture_state(
            SimpleNamespace(screenshot=screenshot, frames=[]),
            "Dashboard A",
            "Overview",
            None,
            tmp_path,
            0,
            {},
        )
    )

    assert state.extracted["export_options"][0]["mask_status"] == "failed"
    assert (
        state.extracted["export_options"][0]["mask_error"]
        == "visual moved before masking"
    )
    assert state.extracted["exported_data"][0]["mask_status"] == "failed"
    assert state.extracted["elements"][0]["screenshot_value_comparison_skipped"]


def test_successful_export_is_masked_for_extraction(
    monkeypatch, tmp_path: Path
) -> None:
    exported = [
        {
            "type": "chart",
            "title": "Orders",
            "categories": ["Week"],
            "rows": [["Week 1", 25]],
            "exportOptionAvailable": True,
            "export_status": "exported",
            "export_data_format": "summarized",
            "export_file": "orders.xlsx",
            "export_file_path": "initial_exports/orders.xlsx",
            "export_json_file": "initial_exports/orders.json",
            "data_sources": ["powerbi_export"],
            "visual_id": "orders-visual",
            "visual_index": 0,
            "page_name": "Overview",
            "filter_state": "initial",
            "mask_status": "pending",
        }
    ]
    export_options = [
        {
            "title": "Orders",
            "visual_index": 0,
            "page_name": "Overview",
            "filter_state": "initial",
            "exportOptionAvailable": True,
            "export_status": "exported",
            "export_data_format": "summarized",
            "export_file": "orders.xlsx",
            "export_file_path": "initial_exports/orders.xlsx",
            "export_json_file": "initial_exports/orders.json",
            "screenshot": None,
            "export_dialog_screenshot": None,
            "mask_status": "pending",
        }
    ]
    screenshots = []
    extracted_images = []

    async def no_wait(*_args, **_kwargs):
        return None

    async def export(*_args, **_kwargs):
        return exported, False, export_options

    async def highlight(_page, titles, **kwargs):
        assert titles == ["Orders"]
        assert kwargs["exported_visuals"] == exported
        return "mask-marker"

    async def clear_masks(_page, marker):
        assert marker == "mask-marker"

    async def screenshot(*, path, full_page):
        assert full_page
        image = b"raw screenshot" if not screenshots else b"masked screenshot"
        screenshots.append(Path(path).name)
        Path(path).write_bytes(image)
        return image

    async def extract(image, _page_name, _cache, titles, *_args, **_kwargs):
        assert titles == ["Orders"]
        extracted_images.append(image)
        return {
            "elements": [
                {
                    "type": "chart",
                    "title": "Orders",
                    "value": 25,
                    "extraction_status": "complete",
                }
            ]
        }

    async def read_filters(*_args):
        return {}

    monkeypatch.setattr(browser, "_wait_for_state_render", no_wait)
    monkeypatch.setattr(browser, "_move_pointer_off_dashboard", no_wait)
    monkeypatch.setattr(browser, "_export_visual_data", export)
    monkeypatch.setattr(browser, "_dismiss_export_success_toasts", no_wait)
    monkeypatch.setattr(browser, "_highlight_exported_visuals", highlight)
    monkeypatch.setattr(browser, "_clear_exported_visual_highlights", clear_masks)
    monkeypatch.setattr(browser, "_extract_or_reuse", extract)
    monkeypatch.setattr(browser, "_read_filter_values", read_filters)

    state = asyncio.run(
        browser._capture_state(
            SimpleNamespace(screenshot=screenshot, frames=[]),
            "Dashboard A",
            "Overview",
            None,
            tmp_path,
            0,
            {},
        )
    )

    assert screenshots == [
        "initial.png",
        "exported_visuals_highlighted.png",
    ]
    assert extracted_images == [b"masked screenshot"]
    assert state.extracted["export_options"][0]["mask_status"] == "masked"
    assert state.extracted["exported_data"][0]["mask_status"] == "masked"
    assert state.extracted["elements"][0]["title"] == "Orders"


def test_export_success_toast_is_closed() -> None:
    dismissed = []
    scripts = []
    waits = []

    class Toast:
        def __init__(self, index):
            self.index = index

        async def is_visible(self):
            return not dismissed[self.index]

        async def inner_text(self):
            return "Successful export. Your data was exported."

        async def evaluate(self, script):
            scripts.append(script)
            return "clicked"

        async def wait_for(self, *, state, timeout):
            assert state == "hidden"
            waits.append(timeout)
            dismissed[self.index] = True

    class Toasts:
        async def count(self):
            return sum(not value for value in dismissed)

        def nth(self, index):
            available = [
                index for index, value in enumerate(dismissed) if not value
            ]
            return Toast(available[index])

    class Frame:
        def locator(self, _selector):
            return Toasts()

    dismissed.extend([False, False])
    asyncio.run(browser._dismiss_export_success_toasts(SimpleNamespace(frames=[Frame()])))
    assert dismissed == [True, True]
    assert len(scripts) == 2
    assert all("closeButton.click()" in script for script in scripts)
    assert all("return 'clicked'" in script for script in scripts)
    assert waits == [1_000, 1_000]


def test_export_success_toast_uses_dom_cleanup_if_close_does_not_dismiss() -> None:
    dismissed = False
    scripts = []

    class Toast:
        async def is_visible(self):
            return not dismissed

        async def inner_text(self):
            return "Successful export. Your data was exported."

        async def evaluate(self, script):
            nonlocal dismissed
            scripts.append(script)
            if len(scripts) == 1:
                return "clicked"
            dismissed = True
            return True

        async def wait_for(self, *, state, timeout):
            assert state == "hidden"
            assert timeout == 1_000
            raise browser.PlaywrightTimeoutError("toast remained visible")

    class Toasts:
        async def count(self):
            return int(not dismissed)

        def nth(self, _index):
            return Toast()

    class Frame:
        def locator(self, _selector):
            return Toasts()

    asyncio.run(browser._dismiss_export_success_toasts(SimpleNamespace(frames=[Frame()])))

    assert dismissed
    assert len(scripts) == 2
    assert "closeButton.click()" in scripts[0]
    assert "wrapper.remove()" in scripts[1]


def test_export_success_toast_is_hidden_if_dom_removal_does_not_stick() -> None:
    hidden = False
    scripts = []
    waits = []

    class Toast:
        async def is_visible(self):
            return not hidden

        async def inner_text(self):
            return "Successful export. Your data was exported."

        async def evaluate(self, script):
            nonlocal hidden
            scripts.append(script)
            if "style.setProperty" in script:
                hidden = True
                return True
            return False

    class Toasts:
        async def count(self):
            return int(not hidden)

        def nth(self, _index):
            return Toast()

    class Frame:
        def locator(self, _selector):
            return Toasts()

    class Page:
        frames = [Frame()]

        async def wait_for_timeout(self, milliseconds):
            waits.append(milliseconds)

    asyncio.run(browser._dismiss_export_success_toasts(Page()))

    assert hidden
    assert "style.setProperty" in scripts[-1]
    assert waits == [100, 100]


def test_export_success_toast_dismissal_retries_during_dom_transition() -> None:
    attempts = 0
    waits = []
    hidden = False

    class Toast:
        async def is_visible(self):
            return not hidden

        async def inner_text(self):
            return "Successful export. Your data was exported."

        async def evaluate(self, _script):
            nonlocal attempts, hidden
            attempts += 1
            if attempts == 1:
                return False
            hidden = True
            return True

    class Toasts:
        async def count(self):
            return 0 if hidden else 1

        def nth(self, _index):
            return Toast()

    class Frame:
        def locator(self, _selector):
            return Toasts()

    class Page:
        frames = [Frame()]

        async def wait_for_timeout(self, milliseconds):
            waits.append(milliseconds)

    asyncio.run(browser._dismiss_export_success_toasts(Page()))

    assert attempts == 2
    assert waits == [100]


def test_export_scan_keeps_data_visuals_and_drops_report_controls() -> None:
    extracted = {
        "screenshot_elements": [
            {"type": "chart", "title": "Sales trend"},
            {"type": "table", "title": "Case details"},
            {"type": "filter", "title": "Service Team"},
        ]
    }
    options = [
        {
            "title": "Sales trend",
            "exportOptionAvailable": False,
            "export_status": "more_options_unavailable",
        },
        {
            "title": "Service Team",
            "exportOptionAvailable": False,
            "export_status": "more_options_unavailable",
        },
        {
            "title": "Bookmark . Click here to follow",
            "exportOptionAvailable": False,
            "export_status": "more_options_unavailable",
        },
        {
            "title": "Parameter",
            "exportOptionAvailable": True,
            "export_status": "exported",
        },
    ]

    retained, excluded_count = browser._filter_export_options_to_data_visuals(
        extracted, options
    )

    assert [item["title"] for item in retained] == ["Sales trend", "Parameter"]
    assert excluded_count == 2


def test_export_coverage_matches_visual_by_bounds_when_titles_differ() -> None:
    extracted = {
        "dashboard_metadata": {"image_width": 1920, "image_height": 1080},
        "elements": [
            {
                "type": "chart",
                "title": "Queue Type",
                "bounding_box": {
                    "x": 1400,
                    "y": 570,
                    "width": 90,
                    "height": 24,
                },
            }
        ],
    }
    option = {
        "title": "Average Queue Time (Minutes)",
        "exportOptionAvailable": True,
        "export_status": "downloaded_no_tabular_data",
        "export_data_format": None,
        "export_file": None,
        "screenshot": None,
        "export_dialog_screenshot": None,
        "export_error": "No tabular data in export",
        "visual_bounds": {
            "x": 1072,
            "y": 280,
            "width": 738,
            "height": 322,
        },
    }

    retained, excluded_count = browser._filter_export_options_to_data_visuals(
        extracted, [option]
    )

    assert retained == [option]
    assert excluded_count == 0
    browser._validate_data_visual_export_coverage(extracted, retained)

    browser._mark_exported_screenshot_visuals(extracted, [], retained)
    attached = browser._attach_export_option_metadata(extracted, retained)

    assert extracted["elements"][0]["extraction_status"] == "partial"
    assert attached["elements"][0]["exportOptionAvailable"] is True
    assert (
        attached["elements"][0]["export_option_visual_title"]
        == "Average Queue Time (Minutes)"
    )


def test_successfully_exported_visual_is_spatially_matched_for_screenshot_skip() -> None:
    screenshot_element = {
        "type": "chart",
        "title": "Queue Type",
        "bounding_box": {
            "x": 1400,
            "y": 570,
            "width": 90,
            "height": 24,
        },
    }
    option = {
        "title": "Average Queue Time (Minutes)",
        "exportOptionAvailable": True,
        "export_status": "exported",
        "visual_bounds": {
            "x": 1072,
            "y": 280,
            "width": 738,
            "height": 322,
        },
    }
    exported = {
        "title": "Average Queue Time (Minutes)",
        "type": "chart",
        "visual_bounds": option["visual_bounds"],
        "values": [42],
        "categories": ["Week 1"],
    }
    extracted = {
        "dashboard_metadata": {"image_width": 1920, "image_height": 1080},
        "elements": [dict(screenshot_element)],
    }

    browser._mark_exported_screenshot_visuals(extracted, [exported], [option])
    merged = browser._merge_exported_elements(extracted, [exported])

    assert extracted["elements"][0]["extraction_status"] == "skipped_exported"
    assert len(merged["elements"]) == 1
    assert merged["elements"][0]["export_option_visual_title"] == (
        "Average Queue Time (Minutes)"
    )


def test_render_wait_uses_report_loading_signal_to_finish_early(monkeypatch) -> None:
    clock = [0.0]
    loading_states = iter([True, False, False, False, False, False])

    class Frame:
        async def evaluate(self, _script):
            return {
                "elements": 10,
                "drawn": 2,
                "busy": int(next(loading_states, False)),
                "content": "loaded",
            }

    class Page:
        frames = [Frame()]

        async def wait_for_timeout(self, milliseconds):
            clock[0] += milliseconds / 1000

    monkeypatch.setattr(browser.time, "monotonic", lambda: clock[0])

    elapsed = asyncio.run(browser._wait_for_state_render(Page(), 2.0))

    assert elapsed >= 2.0


def test_render_wait_keeps_full_settle_when_no_loading_signal_exists(monkeypatch) -> None:
    clock = [0.0]

    class Page:
        async def evaluate(self, _script):
            return {
                "elements": 10,
                "drawn": 2,
                "busy": 0,
                "content": "stable",
            }

        async def wait_for_timeout(self, milliseconds):
            clock[0] += milliseconds / 1000

    monkeypatch.setattr(browser.time, "monotonic", lambda: clock[0])

    elapsed = asyncio.run(browser._wait_for_state_render(Page(), 0.3))

    assert abs(elapsed - 0.3) < 1e-9


def test_render_wait_waits_for_visible_images_to_finish_loading(monkeypatch) -> None:
    clock = [0.0]
    loading_states = iter([True, False, False, False, False, False])

    class Frame:
        async def evaluate(self, script):
            assert "incompleteVisualImages" in script
            assert "visual.querySelectorAll('img')" in script
            return {
                "elements": 10,
                "drawn": 2,
                "busy": int(next(loading_states, False)),
                "content": "loaded",
            }

    class Page:
        frames = [Frame()]

        async def wait_for_timeout(self, milliseconds):
            clock[0] += milliseconds / 1000

    monkeypatch.setattr(browser.time, "monotonic", lambda: clock[0])

    elapsed = asyncio.run(browser._wait_for_state_render(Page(), 2.0))

    assert elapsed >= 2.0


def test_render_wait_restarts_stability_when_report_text_changes(
    monkeypatch,
) -> None:
    clock = [0.0]
    contents = iter(
        ["headers", "headers", "headers", "loaded rows", "loaded rows"]
    )

    class Frame:
        async def evaluate(self, _script):
            return {
                "elements": 20,
                "drawn": 1,
                "busy": 0,
                "content": next(contents, "loaded rows"),
            }

    class Page:
        frames = [Frame()]

        async def wait_for_timeout(self, milliseconds):
            clock[0] += milliseconds / 1000

    monkeypatch.setattr(browser.time, "monotonic", lambda: clock[0])

    elapsed = asyncio.run(browser._wait_for_state_render(Page(), 0.5))

    assert elapsed >= 0.8


def test_render_wait_waits_for_chart_data_marks(monkeypatch) -> None:
    clock = [0.0]
    states = iter(
        [
            {
                "elements": 20,
                "drawn": 2,
                "busy": 0,
                "visuals": 1,
                "unrendered_chart_visuals": [{"visual_id": "chart-1"}],
                "content": "axes",
            },
            {
                "elements": 20,
                "drawn": 2,
                "busy": 0,
                "visuals": 1,
                "unrendered_chart_visuals": [{"visual_id": "chart-1"}],
                "content": "axes",
            },
            {
                "elements": 40,
                "drawn": 8,
                "busy": 0,
                "visuals": 1,
                "unrendered_chart_visuals": [],
                "content": "axes and data",
            },
        ]
    )
    loaded = {
        "elements": 40,
        "drawn": 8,
        "busy": 0,
        "visuals": 1,
        "unrendered_chart_visuals": [],
        "content": "axes and data",
    }

    class Frame:
        async def evaluate(self, _script):
            return next(states, loaded)

    class Page:
        frames = [Frame()]

        async def wait_for_timeout(self, milliseconds):
            clock[0] += milliseconds / 1000

    monkeypatch.setattr(browser.time, "monotonic", lambda: clock[0])

    elapsed = asyncio.run(browser._wait_for_state_render(Page(), 0.2))

    assert elapsed >= 0.4


def test_render_wait_times_out_unrendered_charts_as_unverified(monkeypatch) -> None:
    clock = [0.0]
    pending = {
        "elements": 20,
        "drawn": 2,
        "busy": 0,
        "visuals": 1,
        "unrendered_chart_visuals": [{"visual_id": "chart-1"}],
        "content": "axes",
    }

    class Frame:
        async def evaluate(self, _script):
            return pending

    class Page:
        frames = [Frame()]

        async def wait_for_timeout(self, milliseconds):
            clock[0] += milliseconds / 1000

    page = Page()
    monkeypatch.setattr(browser.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(browser, "UNRENDERED_CHART_TIMEOUT_MS", 200)

    elapsed = asyncio.run(browser._wait_for_state_render(page, 0))
    warning = asyncio.run(browser._read_render_warning(page))

    assert 0.2 <= elapsed < 0.3
    assert warning is not None
    assert warning["reason"] == "Power BI chart content did not finish rendering"
    assert warning["indicators"][0]["unrendered_chart_visuals"] == [
        {"visual_id": "chart-1"}
    ]


def test_render_wait_ignores_nonreport_frame_content_changes(monkeypatch) -> None:
    clock = [0.0]
    host_reads = [0]

    class HostFrame:
        async def evaluate(self, _script):
            host_reads[0] += 1
            return {
                "elements": 0,
                "drawn": 0,
                "busy": 0,
                "visuals": 0,
                "content": str(host_reads[0]),
            }

    class ReportFrame:
        async def evaluate(self, _script):
            return {
                "elements": 20,
                "drawn": 1,
                "busy": 0,
                "visuals": 1,
                "content": "stable report",
            }

    class Page:
        frames = [HostFrame(), ReportFrame()]

        async def wait_for_timeout(self, milliseconds):
            clock[0] += milliseconds / 1000

    monkeypatch.setattr(browser.time, "monotonic", lambda: clock[0])

    elapsed = asyncio.run(browser._wait_for_state_render(Page(), 0.3))

    assert abs(elapsed - 0.3) < 1e-9
    assert host_reads[0] > 1


def test_render_wait_fails_when_visible_images_never_finish_loading(
    monkeypatch,
) -> None:
    clock = [0.0]

    class Frame:
        async def evaluate(self, _script):
            return {
                "elements": 10,
                "drawn": 2,
                "busy": 1,
                "busy_details": {"incomplete_visual_images": 1},
                "content": "loading",
            }

    class Page:
        frames = [Frame()]

        async def wait_for_timeout(self, milliseconds):
            clock[0] += milliseconds / 1000

    monkeypatch.setattr(browser.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(browser, "REPORT_READY_TIMEOUT_MS", 500)

    with pytest.raises(
        RuntimeError,
        match=r"did not finish loading.*busy_details=.*incomplete_visual_images",
    ):
        asyncio.run(browser._wait_for_state_render(Page(), 0.2))


def test_render_wait_continues_after_persistent_class_only_loading(monkeypatch) -> None:
    clock = [0.0]

    class Frame:
        async def evaluate(self, _script):
            return {
                "elements": 100,
                "drawn": 14,
                "visuals": 14,
                "busy": 2,
                "busy_details": {
                    "explicit_indicators": 0,
                    "visual_loading_candidates": 2,
                    "visual_loading_classes": 2,
                    "visual_loading_markers": [
                        {
                            "class_name": "visual-loading",
                            "visual_id": "visual-1",
                        }
                    ],
                    "incomplete_visual_images": 0,
                    "rotating_refresh_controls": 0,
                },
                "content": "stable",
            }

    class Page:
        frames = [Frame()]

        async def wait_for_timeout(self, milliseconds):
            clock[0] += milliseconds / 1000

    monkeypatch.setattr(browser.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(browser, "CLASS_ONLY_RENDER_TIMEOUT_MS", 1_000)

    elapsed = asyncio.run(browser._wait_for_state_render(Page(), 0.2))

    assert elapsed >= 1.0
    assert clock[0] >= 1.0


def test_render_wait_waits_for_class_only_loading_to_clear(monkeypatch) -> None:
    clock = [0.0]

    class Frame:
        async def evaluate(self, _script):
            loading = clock[0] < 1.2
            return {
                "elements": 100,
                "drawn": 14,
                "visuals": 14,
                "busy": 2 if loading else 0,
                "busy_details": {
                    "explicit_indicators": 0,
                    "visual_loading_candidates": 2 if loading else 0,
                    "visual_loading_classes": 2 if loading else 0,
                    "visual_loading_markers": (
                        [{"class_name": "visual-loading", "visual_id": "visual-1"}]
                        if loading
                        else []
                    ),
                    "incomplete_visual_images": 0,
                    "rotating_refresh_controls": 0,
                },
                "content": "loaded",
            }

    class Page:
        frames = [Frame()]

        async def wait_for_timeout(self, milliseconds):
            clock[0] += milliseconds / 1000

    monkeypatch.setattr(browser.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(browser, "REPORT_READY_TIMEOUT_MS", 3_000)

    elapsed = asyncio.run(browser._wait_for_state_render(Page(), 0.2))

    assert elapsed >= 1.5
    assert clock[0] >= 1.5


def test_render_wait_stops_immediately_on_power_bi_resource_error(monkeypatch) -> None:
    clock = [0.0]

    class Frame:
        async def evaluate(self, _script):
            return {
                "elements": 100,
                "drawn": 14,
                "visuals": 14,
                "busy": 0,
                "render_errors": ["query_resource_exceeded"],
                "content": "query error",
            }

    class Page:
        frames = [Frame()]

        async def wait_for_timeout(self, milliseconds):
            clock[0] += milliseconds / 1000

    monkeypatch.setattr(browser.time, "monotonic", lambda: clock[0])

    elapsed = asyncio.run(browser._wait_for_state_render(Page(), 2.0))

    assert elapsed == 0
    assert clock[0] == 0


def test_capture_preserves_slicers_when_visual_query_exceeds_resources(
    monkeypatch, tmp_path: Path
) -> None:
    class Frame:
        async def evaluate(self, _script):
            return {
                "visuals": 1,
                "busy": 0,
                "busy_details": {},
                "render_errors": ["query_resource_exceeded"],
            }

    async def no_wait(*_args, **_kwargs):
        return None

    async def screenshot(*, path, full_page):
        assert full_page
        Path(path).write_bytes(b"resource error screenshot")
        return b"resource error screenshot"

    async def read_filters(_page, _inventory):
        return {"Time Period": ["All"]}

    async def read_visuals(_page, _inventory):
        return []

    async def forbidden_extract(*_args, **_kwargs):
        raise AssertionError("query-error state must skip screenshot model extraction")

    async def read_titles(_page, _visuals, _inventory, _values):
        return (
            [
                {
                    "category": "slicer_value",
                    "text": "All",
                    "associated_visual": "Time Period",
                }
            ],
            "10/28/2026 5:38:09 AM",
        )

    monkeypatch.setattr(browser, "_wait_for_state_render", no_wait)
    monkeypatch.setattr(browser, "_move_pointer_off_dashboard", no_wait)
    monkeypatch.setattr(browser, "_read_visual_layout_inventory", read_visuals)
    monkeypatch.setattr(browser, "_read_filter_values", read_filters)
    monkeypatch.setattr(browser, "_read_dom_title_items", read_titles)
    monkeypatch.setattr(browser, "_extract_or_reuse", forbidden_extract)
    page = SimpleNamespace(screenshot=screenshot, frames=[Frame()])

    state = asyncio.run(
        browser._capture_state(
            page,
            "Dashboard A",
            "Private Label",
            None,
            tmp_path,
            0,
            {},
            filter_inventory=[{"name": "Time Period", "values": ["All"]}],
            extract_visual_data=True,
        )
    )

    assert state.screenshot.is_file()
    assert state.filter_values == {"Time Period": ["All"]}
    assert state.extracted["title_items"][0]["text"] == "All"
    assert state.extracted["capture_mode"] == "visual_layout_filters"
    assert state.extracted["dashboard_metadata"]["refresh_timestamp"] == (
        "10/28/2026 5:38:09 AM"
    )
    assert state.extracted["render_unverified"]["reason"] == (
        "Power BI query exceeded available resources"
    )


def test_report_load_settle_defers_class_only_loading_to_capture(monkeypatch) -> None:
    clock = [0.0]

    class Page:
        async def evaluate(self, _script):
            return {
                "elements": 100,
                "drawn": 14,
                "visuals": 14,
                "busy": 2,
                "busy_details": {
                    "explicit_indicators": 0,
                    "visual_loading_candidates": 2,
                    "visual_loading_classes": 2,
                    "visual_loading_markers": [
                        {
                            "class_name": "visual-loading",
                            "visual_id": "visual-1",
                        }
                    ],
                    "incomplete_visual_images": 0,
                    "rotating_refresh_controls": 0,
                },
                "content": "stable",
            }

        async def wait_for_timeout(self, milliseconds):
            clock[0] += milliseconds / 1000

    monkeypatch.setattr(browser.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        browser, "REPORT_READY_TIMEOUT_MS", 1_000
    )

    asyncio.run(browser._wait_for_report_load_to_settle(Page()))

    assert clock[0] == 0


def test_report_load_settle_stops_on_terminal_resource_error() -> None:
    class Page:
        async def evaluate(self, _script):
            return {
                "elements": 100,
                "drawn": 14,
                "visuals": 14,
                "busy": 1,
                "render_errors": ["query_resource_exceeded"],
                "content": "query error",
            }

    asyncio.run(browser._wait_for_report_load_to_settle(Page()))


def test_safe_path_component_shortens_long_names_without_collisions() -> None:
    first = browser._safe_path_component("Bookmark buttons: " + "A" * 100, 32)
    second = browser._safe_path_component("Bookmark buttons: " + "B" * 100, 32)

    assert len(first) == 32
    assert len(second) == 32
    assert first != second
    assert first == browser._safe_path_component(
        "Bookmark buttons: " + "A" * 100, 32
    )


def test_accessible_option_texts_reads_options_as_one_dom_snapshot() -> None:
    class Options:
        async def evaluate_all(self, script):
            assert "elements.map(element =>" in script
            assert "elements.slice" not in script
            assert "textContent" in script
            assert "input[placeholder*=\"search\" i]" in script
            assert "label.toLocaleLowerCase() === 'search'" in script
            assert "element.hasAttribute('aria-selected')" in script
            assert (
                "element.querySelectorAll(choiceSelector).length > 1"
                in script
            )
            return [
                "Fiscal Year 2026",
                "Benefits",
                "Benefits",
                "10/3/202610/3/2026",
                "",
            ]

    values = asyncio.run(browser._accessible_option_texts(Options()))

    assert values == ["Fiscal Year 2026", "Benefits", "10/3/2026"]


def test_filter_inventory_uses_choice_controls_and_omits_control_names() -> None:
    values, selected = browser._filter_inventory_option_values(
        ["Service Team Nm", "Benefits", "Compensation"],
        ["Service Team Nm", "Benefits"],
        [
            {"label": "Benefits", "selected": True},
            {"label": "Compensation", "selected": False},
        ],
        "Service Team",
        "Service Team Nm",
    )
    assert values == ["Benefits", "Compensation"]
    assert selected == ["Benefits"]

    values, selected = browser._filter_inventory_option_values(
        ["Service Team Nm", "Benefits"],
        ["Service Team Nm"],
        [],
        "Service Team",
        "Service Team Nm",
    )
    assert values == ["Benefits"]
    assert selected == []


def test_checkbox_filter_options_are_scoped_to_visual_and_name() -> None:
    class Locator:
        async def evaluate_all(self, _script):
            return [
                {
                    "index": 0,
                    "name": "Relative Time",
                    "label": "Last 4 Weeks",
                    "selected": True,
                    "visualId": "relative-time",
                },
                {
                    "index": 1,
                    "name": "Relative Time",
                    "label": "Other Weeks",
                    "selected": False,
                    "visualId": "relative-time",
                },
                {
                    "index": 2,
                    "name": "Case Type",
                    "label": "Phone",
                    "selected": False,
                    "visualId": "case-type",
                },
            ]

    class Frame:
        def locator(self, selector):
            assert selector == '[role="checkbox"], input[type="checkbox"]'
            return Locator()

    options = asyncio.run(
        browser._checkbox_filter_options(
            Frame(),
            {
                "name": "Relative Time",
                "selector_name": "Relative Time",
                "visual_id": "relative-time",
            },
        )
    )

    assert [item["label"] for item in options] == [
        "Last 4 Weeks",
        "Other Weeks",
    ]
    assert [item["selected"] for item in options] == [True, False]


def test_radio_filter_options_match_visual_name_and_aria_name_aliases() -> None:
    class Locator:
        async def evaluate_all(self, _script):
            return [
                {
                    "index": 0,
                    "name": "Service Team\nBenefits",
                    "label": "Benefits",
                    "selected": True,
                    "visualId": "service-team",
                },
                {
                    "index": 1,
                    "name": "Service Team\nBenefits",
                    "label": "Compensation",
                    "selected": False,
                    "visualId": "service-team",
                },
            ]

    class Frame:
        def locator(self, selector):
            assert selector == '[role="radio"], input[type="radio"]'
            return Locator()

    options = asyncio.run(
        browser._radio_filter_options(
            Frame(),
            {
                "name": "Service Team",
                "selector_name": "Service Team Nm",
                "visual_id": "service-team",
            },
        )
    )

    assert [option["label"] for option in options] == [
        "Benefits",
        "Compensation",
    ]


def test_combobox_options_include_radio_controls_in_scoped_popup() -> None:
    class EmptyOptions:
        async def count(self):
            return 0

    class Radios:
        async def count(self):
            return 2

    radios = Radios()

    class Popup:
        async def count(self):
            return 1

        def get_by_role(self, role):
            assert role == "option"
            return EmptyOptions()

        def locator(self, selector):
            if selector == '[role="checkbox"], input[type="checkbox"]':
                return EmptyOptions()
            if selector == '[role="radio"], input[type="radio"]':
                return radios
            raise AssertionError(selector)

    class Frame:
        def locator(self, selector, **_kwargs):
            assert selector == '[id="service-team-popup"]'
            return Popup()

    class Combo:
        async def get_attribute(self, name):
            return "service-team-popup" if name == "aria-controls" else None

    assert asyncio.run(
        browser._options_for_combobox(Frame(), Combo())
    ) is radios


def test_listbox_sweep_counts_text_only_slicer_row_toward_setsize() -> None:
    assert "textRowLabels(listbox)" in browser.LISTBOX_READ_SCRIPT
    assert ".slicerItemContainer, .slicerText" in browser.TEXT_ROW_LABELS_JS
    assert ".visual-actionButton" in browser.TEXT_ROW_LABELS_JS
    assert '[aria-hidden="true"]' in browser.TEXT_ROW_LABELS_JS

    role_labels = [
        "Business Unit",
        "Skill",
        "Course",
        "Course Duration",
        "Completion Rate",
        "Time Period",
    ]
    read_calls = []

    class Listbox:
        async def evaluate(self, script, *args):
            if script is browser.LISTBOX_READ_SCRIPT:
                read_calls.append(args)
                return {
                    "index": 0,
                    "setSize": 7,
                    "options": [
                        {"label": label, "selected": False}
                        for label in [*role_labels, "Time Spent"]
                    ],
                }
            if script is browser.LISTBOX_EDGE_SCRIPT:
                return None
            raise AssertionError("scroll should not be needed")

    class Listboxes:
        async def count(self):
            return 1

        def nth(self, _index):
            return Listbox()

    snapshots = asyncio.run(browser._snapshot_scrolled_listboxes(Listboxes()))

    assert [option["label"] for option in snapshots[0]["options"]] == [
        *role_labels,
        "Time Spent",
    ]
    assert len(read_calls) == 1


def test_text_row_fallback_ignores_action_button_visual_and_keeps_roles() -> None:
    async_playwright = pytest.importorskip(
        "playwright.async_api"
    ).async_playwright

    html = """
    <div class="visualContainer" data-visual-id="slicer">
      <div role="listbox" aria-label="Category">
        <div role="option" aria-setsize="6"><div class="slicerText">Business Unit</div></div>
        <div role="option" aria-setsize="6"><div class="slicerText">Skill</div></div>
        <div role="option" aria-setsize="6"><div class="slicerText">Course</div></div>
        <div role="option" aria-setsize="6"><div class="slicerText">Course Duration</div></div>
        <div role="option" aria-setsize="6"><div class="slicerText">Completion Rate</div></div>
        <div role="option" aria-setsize="6"><div class="slicerText">Time Period</div></div>
        <div class="visual-actionButton" aria-hidden="true">
          <div class="ui-role-button-text">Time Spent</div>
        </div>
      </div>
    </div>
    <div class="visual visual-actionButton" data-automation-type="visual" aria-hidden="true">
      <div class="ui-role-button-text">Time Spent</div>
    </div>
    """

    async def read_labels() -> list[str]:
        async with async_playwright() as playwright:
            try:
                chromium = await playwright.chromium.launch()
            except Exception as error:
                pytest.skip(f"Chromium unavailable: {error}")
            page = await chromium.new_page()
            await page.set_content(html)
            data = await page.locator('[role="listbox"]').evaluate(
                browser.LISTBOX_READ_SCRIPT, 0
            )
            await chromium.close()
        return [option["label"] for option in data["options"]]

    assert asyncio.run(read_labels()) == [
        "Business Unit",
        "Skill",
        "Course",
        "Course Duration",
        "Completion Rate",
        "Time Period",
    ]


def test_bookmark_button_groups_discover_labels_and_active_option() -> None:
    values = [
        ("E-Mail", "#FFFFFF", "rgb(82, 80, 81)"),
        ("Phone Call", "#FFFFFF", "rgb(82, 80, 81)"),
        ("Workday Cases", "#000000", "rgb(255, 255, 255)"),
    ]

    class Label:
        def __init__(self, value):
            self.value = value
            self.first = self

        async def count(self):
            return 1

        async def inner_text(self):
            return self.value

    class Visual:
        def __init__(self, label, fill, color):
            self.label = label
            self.fill = fill
            self.color = color

        async def is_visible(self):
            return True

        def locator(self, selector):
            assert selector == ".ui-role-button-text"
            return Label(self.label)

        async def evaluate(self, _script):
            return {"fill": self.fill, "textColor": self.color}

    visuals = [Visual(*value) for value in values]

    class Visuals:
        async def count(self):
            return len(visuals)

        def nth(self, index):
            return visuals[index]

    class Group:
        async def get_attribute(self, name):
            assert name == "aria-label"
            return "3 items"

        async def is_visible(self):
            return True

        def locator(self, selector):
            assert selector == (
                '.visualContainer[role="link"][aria-label^="Bookmark"]'
            )
            return Visuals()

    class Groups:
        async def count(self):
            return 1

        def nth(self, index):
            assert index == 0
            return Group()

    class Frame:
        def locator(self, selector):
            assert selector == '.visualContainerGroup[role="group"]'
            return Groups()

    groups = asyncio.run(browser._bookmark_button_groups(Frame()))

    assert groups[0]["values"] == [
        "E-Mail",
        "Phone Call",
        "Workday Cases",
    ]
    assert groups[0]["selected"] == ["Workday Cases"]
    assert groups[0]["name"] == (
        "Bookmark buttons: E-Mail | Phone Call | Workday Cases"
    )


def test_select_filter_activates_and_verifies_bookmark_button(monkeypatch) -> None:
    values = ["E-Mail", "Phone Call", "Workday Cases"]
    selected = {"Workday Cases"}
    clicks = []

    class Option:
        def __init__(self, label):
            self.label = label

        async def click(self, **kwargs):
            assert kwargs == {"force": True, "timeout": 5_000}
            clicks.append(self.label)
            selected.clear()
            selected.add(self.label)

    async def button_groups(_frame):
        return [
            {
                "name": "Bookmark buttons: " + " | ".join(values),
                "values": values,
                "selected": list(selected),
                "options": [
                    {"label": value, "locator": Option(value)}
                    for value in values
                ],
            }
        ]

    async def wait_for_selection(_page, _filter, _value):
        return list(selected)

    monkeypatch.setattr(browser, "_bookmark_button_groups", button_groups)
    monkeypatch.setattr(
        browser, "_wait_for_filter_selection", wait_for_selection
    )
    filter_name = "Bookmark buttons: " + " | ".join(values)
    filters = [
        {
            "name": filter_name,
            "selector_name": filter_name,
            "control_type": "bookmark_buttons",
            "selected": ["Workday Cases"],
            "values": values,
        }
    ]

    asyncio.run(
        browser._select_filter(
            SimpleNamespace(frames=[object()]),
            browser.FilterSelection(name=filter_name, value="E-Mail"),
            filters,
        )
    )

    assert clicks == ["E-Mail"]
    assert selected == {"E-Mail"}


def test_restore_report_filters_restores_bookmark_button_baseline(
    monkeypatch,
) -> None:
    values = ["E-Mail", "Phone Call", "Workday Cases"]
    selected = {"E-Mail"}
    clicks = []

    class Option:
        def __init__(self, label):
            self.label = label

        async def click(self, **_kwargs):
            clicks.append(self.label)
            selected.clear()
            selected.add(self.label)

    filter_name = "Bookmark buttons: " + " | ".join(values)
    filter_item = {
        "name": filter_name,
        "selector_name": filter_name,
        "control_type": "bookmark_buttons",
        "selected": ["Workday Cases"],
        "values": values,
    }

    async def button_groups(_frame):
        return [
            {
                "name": filter_name,
                "values": values,
                "selected": list(selected),
                "options": [
                    {"label": value, "locator": Option(value)}
                    for value in values
                ],
            }
        ]

    async def wait_for_selection(_page, _filter, value):
        return [value] if value in selected else None

    monkeypatch.setattr(browser, "_bookmark_button_groups", button_groups)
    monkeypatch.setattr(
        browser, "_wait_for_filter_selection", wait_for_selection
    )

    restored = asyncio.run(
        browser._restore_report_filters(
            SimpleNamespace(frames=[object()]), [filter_item]
        )
    )

    assert restored
    assert clicks == ["Workday Cases"]
    assert selected == {"Workday Cases"}


def test_restore_report_filters_restores_bookmark_before_dependent_slicer(
    monkeypatch,
) -> None:
    restored = set()
    restoration_order = []

    async def wait_for_render(_page, settle_seconds):
        assert settle_seconds == 2.0
        restoration_order.append("settled")

    async def baseline_matches(_page, filters):
        return all(item["name"] in restored for item in filters)

    async def restore_bookmark(_page, filter_item, _expected):
        restoration_order.append(filter_item["name"])
        restored.add(filter_item["name"])
        return True

    async def restore_dropdown(_page, filter_item, _expected):
        restoration_order.append(filter_item["name"])
        restored.add(filter_item["name"])
        return True

    monkeypatch.setattr(browser, "_filters_match_baseline", baseline_matches)
    monkeypatch.setattr(
        browser, "_restore_bookmark_button_values", restore_bookmark
    )
    monkeypatch.setattr(
        browser, "_restore_dropdown_filter_values", restore_dropdown
    )
    monkeypatch.setattr(browser, "_wait_for_state_render", wait_for_render)

    restored_baseline = asyncio.run(
        browser._restore_report_filters(
            SimpleNamespace(frames=[]),
            [
                {
                    "name": "Service Team",
                    "control_type": "dropdown",
                    "selected": ["HR Support"],
                },
                {
                    "name": "View",
                    "control_type": "bookmark_buttons",
                    "selected": ["E-Mail"],
                },
            ],
        )
    )

    assert restored_baseline
    assert restoration_order == ["View", "settled", "Service Team"]


def test_combobox_options_are_scoped_to_aria_controls_popup() -> None:
    class Options:
        async def count(self):
            return 1

    options = Options()

    class Popup:
        async def count(self):
            return 1

        def get_by_role(self, role):
            assert role == "option"
            return options

    class Frame:
        def locator(self, selector, **_kwargs):
            assert selector == '[id="service-team-popup"]'
            return Popup()

    class Combo:
        async def get_attribute(self, name):
            return "service-team-popup" if name == "aria-controls" else None

    result = asyncio.run(browser._options_for_combobox(Frame(), Combo()))

    assert result is options


class _ScrolledListboxes:
    """Fake listbox collection with the count/nth/evaluate surface."""

    def data(self):
        raise NotImplementedError

    async def count(self):
        return len(self.data())

    def nth(self, index):
        listboxes = self

        class _Listbox:
            async def evaluate(self, script, _index=None):
                return listboxes.data()[index]

        return _Listbox()


def test_single_select_listbox_reads_radio_style_power_bi_options() -> None:
    values = [
        "Benefits",
        "Compensation",
        "HR Business Partners",
        "HRIS",
        "Learning",
        "Legal Service Team",
        "Payroll",
        "Recruiting",
        "Risk Management",
        "Talent",
    ]

    class Listboxes(_ScrolledListboxes):
        def data(self):
            return [
                {
                    "index": 0,
                    "label": "Service Team Nm",
                    "multiSelect": "false",
                    "options": [
                        {
                            "label": value,
                            "selected": value == "HR Business Partners",
                        }
                        for value in values
                    ],
                }
            ]

    class Popup:
        async def count(self):
            return 1

        def locator(self, selector):
            assert selector == '[role="listbox"]'
            return Listboxes()

    class Frame:
        def locator(self, selector):
            assert selector == '[id="service-team-popup"]'
            return Popup()

    class Combo:
        async def get_attribute(self, name):
            if name == "aria-controls":
                return "service-team-popup"
            return None

    result = asyncio.run(
        browser._single_select_dropdown_options(
            Frame(),
            Combo(),
            {
                "name": "Service Team",
                "selector_name": "Service Team Nm",
            },
        )
    )

    assert result == (values, ["HR Business Partners"])


def test_single_select_listbox_detection_leaves_multi_select_alone() -> None:
    class Listboxes(_ScrolledListboxes):
        def data(self):
            return [
                {
                    "index": 0,
                    "label": "Service Team Nm",
                    "multiSelect": "true",
                    "options": [{"label": "Benefits", "selected": False}],
                }
            ]

    class Popup:
        async def count(self):
            return 1

        def locator(self, _selector):
            return Listboxes()

    class Frame:
        def locator(self, _selector):
            return Popup()

    class Combo:
        async def get_attribute(self, name):
            return "service-team-popup" if name == "aria-controls" else None

    result = asyncio.run(
        browser._single_select_dropdown_options(
            Frame(),
            Combo(),
            {"name": "Service Team", "selector_name": "Service Team Nm"},
        )
    )

    assert result is None


def test_associated_listbox_fallback_reads_multiselect_checkbox_options() -> None:
    class Listboxes(_ScrolledListboxes):
        def data(self):
            return [
                {
                    "index": 0,
                    "label": "Relative Time",
                    "multiSelect": "true",
                    "options": [
                        {"label": "Last 4 Weeks", "selected": True},
                        {"label": "Other Weeks", "selected": False},
                    ],
                }
            ]

    class Popup:
        async def count(self):
            return 1

        def locator(self, selector):
            assert selector == '[role="listbox"]'
            return Listboxes()

    class Frame:
        def locator(self, selector):
            assert selector == '[id="relative-time-popup"]'
            return Popup()

    class Combo:
        async def get_attribute(self, name):
            if name == "aria-controls":
                return "relative-time-popup"
            return None

    result = asyncio.run(
        browser._associated_listbox_options(
            Frame(),
            Combo(),
            {"name": "Relative Time", "selector_name": "Relative Time"},
        )
    )

    assert result == (
        ["Last 4 Weeks", "Other Weeks"],
        ["Last 4 Weeks"],
        "checkbox_dropdown",
    )


def test_associated_listbox_uses_combobox_options_when_controls_are_missing(
    monkeypatch,
) -> None:
    class Combo:
        async def get_attribute(self, _name):
            return None

        async def bounding_box(self):
            return {"x": 0, "y": 0, "width": 100, "height": 30}

    class Options:
        async def evaluate_all(self, _script):
            return [
                {
                    "index": 0,
                    "label": "Service Team",
                    "multiSelect": "false",
                    "options": [
                        {"label": "HR Support Center", "selected": True},
                        {"label": "Payroll", "selected": False},
                    ],
                }
            ]

    options = Options()

    async def options_for_combo(_frame, combo):
        assert isinstance(combo, Combo)
        return options

    monkeypatch.setattr(
        browser, "_options_for_combobox", options_for_combo
    )

    result = asyncio.run(
        browser._associated_listbox_options(
            object(),
            Combo(),
            {"name": "Service Team", "selector_name": "Service Team"},
        )
    )

    assert result == (
        ["HR Support Center", "Payroll"],
        ["HR Support Center"],
        "radio_dropdown",
    )


def test_inventory_uses_associated_listbox_when_generic_options_are_empty(
    monkeypatch,
) -> None:
    class Locator:
        async def evaluate_all(self, _script):
            return []

        async def count(self):
            return 0

    class Combo:
        async def is_visible(self):
            return True

        async def get_attribute(self, _name):
            return "true"

        async def inner_text(self):
            return "All"

        async def press(self, key):
            assert key == "Escape"

    combo = Combo()

    class Combos:
        async def evaluate_all(self, _script):
            return [
                {
                    "index": 0,
                    "name": "Relative Time",
                    "selectorName": "Relative Time",
                }
            ]

        def nth(self, _index):
            return combo

    class Frame:
        url = "https://app.powerbi.com/report"

        def locator(self, _selector):
            return Locator()

        def get_by_role(self, role):
            return Combos() if role == "combobox" else Locator()

    class Page:
        frames = [Frame()]

        async def wait_for_timeout(self, _milliseconds):
            pass

    async def no_tabs(_frame):
        return []

    async def no_choices(_frame, *_args):
        return []

    async def no_bookmarks(_frame):
        return []

    async def no_click(_combo):
        return None

    async def no_wait(_page, _combo):
        return None

    async def no_options(_page, _frame, _combo, _filter):
        return object(), [], None, [], []

    async def listbox_options(_frame, _combo, _filter):
        return (
            ["Last 4 Weeks", "Other Weeks"],
            ["Last 4 Weeks"],
            "checkbox_dropdown",
        )

    monkeypatch.setattr(browser, "_accessible_tab_names", no_tabs)
    monkeypatch.setattr(browser, "_click_slicer_control", no_click)
    monkeypatch.setattr(browser, "_wait_for_slicer_open", no_wait)
    monkeypatch.setattr(browser, "_wait_for_combobox_options", no_options)
    monkeypatch.setattr(
        browser, "_associated_listbox_options", listbox_options
    )
    monkeypatch.setattr(browser, "_checkbox_filter_options", no_choices)
    monkeypatch.setattr(browser, "_radio_filter_options", no_choices)
    monkeypatch.setattr(browser, "_bookmark_button_groups", no_bookmarks)

    inventory = asyncio.run(
        browser._accessible_inventory(
            Page(), clear_selection_actions=False
        )
    )

    assert inventory["filters"] == [
        {
            "name": "Relative Time",
            "selector_name": "Relative Time",
            "selected": ["Last 4 Weeks"],
            "values": ["Last 4 Weeks", "Other Weeks"],
            "clear_selection_checked": True,
            "control_type": "checkbox_dropdown",
        }
    ]


def test_visual_only_inventory_reads_visible_selection_without_opening_dropdown(
    monkeypatch,
) -> None:
    class Locator:
        async def evaluate_all(self, _script):
            return []

        async def count(self):
            return 0

    class Combo:
        async def is_visible(self):
            return True

        async def inner_text(self):
            return "Relative Time\nYesterday"

    combo = Combo()

    class Combos:
        async def evaluate_all(self, _script):
            return [
                {
                    "index": 0,
                    "name": "Relative Time",
                    "selectorName": "Relative Time",
                }
            ]

        def nth(self, _index):
            return combo

    class Frame:
        url = "https://app.powerbi.com/report"

        def locator(self, _selector):
            return Locator()

        def get_by_role(self, role):
            return Combos() if role == "combobox" else Locator()

    class Page:
        frames = [Frame()]

        async def wait_for_timeout(self, _milliseconds):
            pass

    async def no_tabs(_frame):
        return []

    async def no_choices(_frame, *_args):
        return []

    async def no_bookmarks(_frame):
        return []

    async def unexpected_open(*_args):
        raise AssertionError("visual-only inventory must not open slicers")

    monkeypatch.setattr(browser, "_accessible_tab_names", no_tabs)
    monkeypatch.setattr(browser, "_checkbox_filter_options", no_choices)
    monkeypatch.setattr(browser, "_radio_filter_options", no_choices)
    monkeypatch.setattr(browser, "_bookmark_button_groups", no_bookmarks)
    monkeypatch.setattr(browser, "_click_slicer_control", unexpected_open)

    inventory = asyncio.run(
        browser._accessible_inventory(
            Page(),
            clear_selection_actions=False,
            inspect_filter_options=False,
        )
    )

    assert inventory["filters"] == [
        {
            "name": "Relative Time",
            "selector_name": "Relative Time",
            "selected": ["Yesterday"],
            "values": [],
            "clear_selection_checked": False,
            "option_inventory_status": "skipped",
            "option_inventory_note": (
                "Dropdown was not opened; only the visible selection was captured."
            ),
        }
    ]
    assert browser._visible_selected_values(
        "Relative Time", "Relative Time", "Relative Time: Yesterday"
    ) == ["Yesterday"]
    assert browser._visible_selected_values("Facility", "Facility", "All") == [
        "All"
    ]


def test_inventory_preserves_visible_all_when_popup_options_are_unavailable(
    monkeypatch,
) -> None:
    class Locator:
        async def evaluate_all(self, _script):
            return []

        async def count(self):
            return 0

    class Combo:
        async def is_visible(self):
            return True

        async def get_attribute(self, _name):
            return "true"

        async def inner_text(self):
            return "All"

        async def press(self, key):
            assert key == "Escape"

    combo = Combo()

    class Combos:
        async def evaluate_all(self, _script):
            return [
                {
                    "index": 0,
                    "name": "Supply Planner Hierarchy",
                    "selectorName": "2nd Lvl Mgr, Manager, Planner",
                }
            ]

        def nth(self, _index):
            return combo

    class Frame:
        url = "https://app.powerbi.com/report"

        def locator(self, _selector):
            return Locator()

        def get_by_role(self, role):
            return Combos() if role == "combobox" else Locator()

    class Page:
        frames = [Frame()]

        async def wait_for_timeout(self, _milliseconds):
            pass

    async def no_tabs(_frame):
        return []

    async def no_choices(_frame, *_args):
        return []

    async def no_bookmarks(_frame):
        return []

    async def no_click(_combo):
        return None

    async def no_wait(_page, _combo):
        return None

    async def no_options(_page, _frame, _combo, _filter):
        return object(), [], None, [], []

    async def no_associated_options(_frame, _combo, _filter):
        return None

    async def no_single_select_options(_frame, _combo, _filter):
        return None

    monkeypatch.setattr(browser, "_accessible_tab_names", no_tabs)
    monkeypatch.setattr(browser, "_click_slicer_control", no_click)
    monkeypatch.setattr(browser, "_wait_for_slicer_open", no_wait)
    monkeypatch.setattr(browser, "_wait_for_combobox_options", no_options)
    monkeypatch.setattr(
        browser, "_associated_listbox_options", no_associated_options
    )
    monkeypatch.setattr(
        browser, "_single_select_dropdown_options", no_single_select_options
    )
    monkeypatch.setattr(browser, "_checkbox_filter_options", no_choices)
    monkeypatch.setattr(browser, "_radio_filter_options", no_choices)
    monkeypatch.setattr(browser, "_bookmark_button_groups", no_bookmarks)

    inventory = asyncio.run(
        browser._accessible_inventory(
            Page(), clear_selection_actions=False
        )
    )

    assert inventory["filters"] == [
        {
            "name": "Supply Planner Hierarchy",
            "selector_name": "2nd Lvl Mgr, Manager, Planner",
            "selected": ["All"],
            "values": [],
            "clear_selection_checked": True,
            "option_inventory_status": "unavailable",
            "option_inventory_note": (
                "The popup exposed no readable options; the visible "
                "aggregate selection was retained."
            ),
        }
    ]

    async def unexpected_refresh(*_args):
        raise AssertionError("unavailable options should not be retried")

    monkeypatch.setattr(
        browser, "_refresh_dropdown_filter_options", unexpected_refresh
    )
    asyncio.run(browser._prepare_slicer_selections(Page(), inventory["filters"]))
    assert inventory["filters"][0]["reset_strategy"] == "preserve_selection"


def test_inventory_prefers_single_select_listbox_over_checkbox_descendants(
    monkeypatch,
) -> None:
    class Locator:
        async def evaluate_all(self, _script):
            return []

        async def count(self):
            return 0

    class Combo:
        async def is_visible(self):
            return True

        async def get_attribute(self, _name):
            return "true"

        async def inner_text(self):
            return "HR Support Center"

        async def press(self, key):
            assert key == "Escape"

    combo = Combo()

    class Combos:
        async def evaluate_all(self, _script):
            return [
                {
                    "index": 0,
                    "name": "Service Team",
                    "selectorName": "Service Team",
                }
            ]

        def nth(self, _index):
            return combo

    class Frame:
        url = "https://app.powerbi.com/report"

        def locator(self, _selector):
            return Locator()

        def get_by_role(self, role):
            return Combos() if role == "combobox" else Locator()

    class Page:
        frames = [Frame()]

        async def wait_for_timeout(self, _milliseconds):
            pass

    async def no_tabs(_frame):
        return []

    async def no_bookmarks(_frame):
        return []

    async def no_click(_combo):
        return None

    async def no_wait(_page, _combo):
        return None

    async def options_with_checkbox_descendant(_page, _frame, _combo, _filter):
        return (
            object(),
            ["HR Support Center", "Payroll"],
            ["HR Support Center"],
            [{"label": "HR Support Center", "selected": True}],
            [],
        )

    async def single_select_listbox(_frame, _combo, _filter):
        return (
            ["HR Support Center", "Payroll"],
            ["HR Support Center"],
            "radio_dropdown",
        )

    async def no_choices(_frame):
        return []

    monkeypatch.setattr(browser, "_accessible_tab_names", no_tabs)
    monkeypatch.setattr(browser, "_click_slicer_control", no_click)
    monkeypatch.setattr(browser, "_wait_for_slicer_open", no_wait)
    monkeypatch.setattr(
        browser,
        "_wait_for_combobox_options",
        options_with_checkbox_descendant,
    )
    monkeypatch.setattr(
        browser, "_associated_listbox_options", single_select_listbox
    )
    monkeypatch.setattr(browser, "_checkbox_filter_options", no_choices)
    monkeypatch.setattr(browser, "_radio_filter_options", no_choices)
    monkeypatch.setattr(browser, "_bookmark_button_groups", no_bookmarks)

    inventory = asyncio.run(
        browser._accessible_inventory(
            Page(), clear_selection_actions=False
        )
    )

    assert inventory["filters"] == [
        {
            "name": "Service Team",
            "selector_name": "Service Team",
            "selected": ["HR Support Center"],
            "values": ["HR Support Center", "Payroll"],
            "clear_selection_checked": True,
            "control_type": "radio_dropdown",
        }
    ]


def test_refresh_prefers_single_select_listbox_mode_over_checkbox_choices(
    monkeypatch,
) -> None:
    combo = object()
    filter_item = {
        "name": "Service Team",
        "selector_name": "Service Team",
        "control_type": "checkbox_dropdown",
        "values": ["HR Support Center", "Payroll"],
        "selected": ["HR Support Center"],
    }

    class Frame:
        def get_by_role(self, _role):
            return None

    class Page:
        frames = [Frame()]

    async def find_combo(_frame, _filter):
        return combo

    async def dropdown_is_open(_combo):
        return True

    async def checkbox_choices(_page, _frame, _combo, _filter):
        return (
            object(),
            ["HR Support Center", "Payroll"],
            ["HR Support Center"],
            [{"label": "HR Support Center", "selected": True}],
            [],
        )

    async def single_select_listbox(_frame, _combo, _filter):
        return (
            ["HR Support Center", "Payroll"],
            ["HR Support Center"],
            "radio_dropdown",
        )

    async def close_dropdown(_page, _combo):
        pass

    monkeypatch.setattr(browser, "_combobox_for_filter", find_combo)
    monkeypatch.setattr(browser, "_slicer_dropdown_is_open", dropdown_is_open)
    monkeypatch.setattr(
        browser, "_wait_for_combobox_options", checkbox_choices
    )
    monkeypatch.setattr(
        browser, "_associated_listbox_options", single_select_listbox
    )
    monkeypatch.setattr(browser, "_close_slicer_dropdown", close_dropdown)

    refreshed = asyncio.run(
        browser._refresh_dropdown_filter_options(Page(), filter_item)
    )

    assert refreshed is True
    assert filter_item["control_type"] == "radio_dropdown"
    assert filter_item["values"] == ["HR Support Center", "Payroll"]
    assert filter_item["selected"] == ["HR Support Center"]


def test_prepare_refreshes_checkbox_dropdown_mode_before_enumerating(
    monkeypatch,
) -> None:
    combo = object()
    filter_item = {
        "name": "Service Team",
        "selector_name": "Service Team",
        "control_type": "checkbox_dropdown",
        "values": ["All", "HR Support Center", "Payroll"],
        "selected": ["HR Support Center"],
        "clear_selection_checked": True,
    }
    selected_values = []

    class Frame:
        def get_by_role(self, _role):
            return None

    class Page:
        frames = [Frame()]

    async def find_combo(_frame, _filter):
        return combo

    async def dropdown_is_open(_combo):
        return True

    async def checkbox_choices(_page, _frame, _combo, _filter):
        return (
            object(),
            ["All", "HR Support Center", "Payroll"],
            ["HR Support Center"],
            [{"label": "HR Support Center", "selected": True}],
            [],
        )

    async def single_select_listbox(_frame, _combo, _filter):
        return (
            ["All", "HR Support Center", "Payroll"],
            ["HR Support Center"],
            "radio_dropdown",
        )

    async def close_dropdown(_page, _combo):
        pass

    async def select_filter(_page, selection, _filters, **_kwargs):
        selected_values.append(selection.value)

    monkeypatch.setattr(browser, "_combobox_for_filter", find_combo)
    monkeypatch.setattr(browser, "_slicer_dropdown_is_open", dropdown_is_open)
    monkeypatch.setattr(
        browser, "_wait_for_combobox_options", checkbox_choices
    )
    monkeypatch.setattr(
        browser, "_associated_listbox_options", single_select_listbox
    )
    monkeypatch.setattr(browser, "_close_slicer_dropdown", close_dropdown)
    monkeypatch.setattr(browser, "_select_filter", select_filter)

    asyncio.run(browser._prepare_slicer_selections(Page(), [filter_item]))

    assert filter_item["control_type"] == "radio_dropdown"
    assert filter_item["values"] == ["HR Support Center", "Payroll"]
    assert selected_values == ["HR Support Center", "Payroll"]


def test_combobox_does_not_use_unrelated_listbox_when_associated_popup_is_empty(
) -> None:
    class EmptyOptions:
        async def count(self):
            return 0

    empty_options = EmptyOptions()

    class Popup:
        async def count(self):
            return 1

        def get_by_role(self, _role):
            return empty_options

        def locator(self, _selector):
            return empty_options

    class Frame:
        def locator(self, selector):
            assert selector == '[id="time-period-popup"]'
            return Popup()

        def get_by_role(self, _role):
            raise AssertionError(
                "An unrelated page listbox must not replace the scoped popup"
            )

    class Combo:
        async def get_attribute(self, name):
            if name == "aria-controls":
                return "time-period-popup"
            return None

    result = asyncio.run(browser._options_for_combobox(Frame(), Combo()))

    assert result is empty_options


def test_wait_for_combobox_options_reopens_collapsed_popup(monkeypatch) -> None:
    expanded = False
    options = object()

    class Combo:
        async def get_attribute(self, name):
            assert name == "aria-expanded"
            return "true" if expanded else "false"

    class Page:
        async def wait_for_timeout(self, _milliseconds):
            return None

    async def open_control(_combo):
        nonlocal expanded
        expanded = True

    async def wait_open(_page, _combo):
        return None

    async def get_options(_frame, _combo):
        return options

    async def option_texts(_options):
        return ["2026", "2027"]

    async def selected_options(_options):
        return ["2026"]

    async def no_choices(_frame, _filter):
        return []

    monkeypatch.setattr(browser, "_click_slicer_control", open_control)
    monkeypatch.setattr(browser, "_wait_for_slicer_open", wait_open)
    monkeypatch.setattr(browser, "_options_for_combobox", get_options)
    monkeypatch.setattr(browser, "_accessible_option_texts", option_texts)
    monkeypatch.setattr(browser, "_selected_combobox_options", selected_options)
    monkeypatch.setattr(browser, "_checkbox_filter_options", no_choices)
    monkeypatch.setattr(browser, "_radio_filter_options", no_choices)

    result = asyncio.run(
        browser._wait_for_combobox_options(
            Page(), object(), Combo(), {"name": "Time Period"}
        )
    )

    assert expanded
    assert result[0] is options
    assert result[1] == ["2026", "2027"]


def test_combobox_tree_options_are_scoped_to_aria_controls_popup() -> None:
    class EmptyOptions:
        async def count(self):
            return 0

    class TreeItems:
        async def count(self):
            return 2

    treeitems = TreeItems()

    class Popup:
        async def count(self):
            return 1

        def get_by_role(self, role):
            if role == "option":
                return EmptyOptions()
            if role == "treeitem":
                return treeitems
            raise AssertionError(role)

        def locator(self, selector):
            assert selector in {
                '[role="checkbox"], input[type="checkbox"]',
                '[role="radio"], input[type="radio"]',
                '[role="none"], li, button, [data-testid], [aria-label], [title]',
            }
            return EmptyOptions()

    class Frame:
        def locator(self, selector, **_kwargs):
            assert selector == '[id="time-period-popup"]'
            return Popup()

    class Combo:
        async def get_attribute(self, name):
            return "time-period-popup" if name == "aria-controls" else None

    result = asyncio.run(browser._options_for_combobox(Frame(), Combo()))

    assert result is treeitems


def test_combobox_falls_back_to_nearest_visible_listbox() -> None:
    class EmptyChoices:
        async def count(self):
            return 0

    class Options:
        def __init__(self, value):
            self.value = value

        async def count(self):
            return 1

        async def evaluate_all(self, _script):
            return [self.value]

    options = Options("Benefits")
    unrelated_options = Options("Service Team")

    class Listbox:
        def __init__(self, x, y, options):
            self.x = x
            self.y = y
            self.options = options

        async def is_visible(self):
            return True

        async def bounding_box(self):
            return {
                "x": self.x,
                "y": self.y,
                "width": 300,
                "height": 240,
            }

        def get_by_role(self, role):
            assert role in {"option", "treeitem", "menuitem"}
            return self.options if role == "option" else EmptyChoices()

        def locator(self, _selector):
            return EmptyChoices()

    class Listboxes:
        async def count(self):
            return 2

        def nth(self, index):
            if index == 0:
                return Listbox(650, 550, unrelated_options)
            assert index == 1
            return Listbox(100, 150, options)

    class EmptyContainers:
        async def count(self):
            return 0

    class Frame:
        def locator(self, selector, **_kwargs):
            raise AssertionError(
                f"Unexpected associated-popup lookup: {selector}"
            )

        def get_by_role(self, role):
            if role == "listbox":
                return Listboxes()
            return EmptyContainers()

    class Combo:
        async def get_attribute(self, _name):
            return None

        async def bounding_box(self):
            return {"x": 100, "y": 100, "width": 300, "height": 40}

    result = asyncio.run(browser._options_for_combobox(Frame(), Combo()))

    assert result is options


def test_checkbox_inventory_identifies_type_before_clicking_clear(monkeypatch) -> None:
    events = []

    class Locator:
        async def evaluate_all(self, _script):
            return []

        async def count(self):
            return 0

    class Combo:
        async def is_visible(self):
            return True

        async def click(self, **_kwargs):
            events.append("open")

        async def get_attribute(self, name):
            if name == "aria-expanded":
                return "true"
            assert name in {"aria-controls", "aria-owns"}
            return None

        async def inner_text(self):
            raise AssertionError("A cleared filter must not be read")

        async def press(self, _key):
            raise AssertionError("A cleared filter must not be closed")

    combo = Combo()

    class Combos:
        async def evaluate_all(self, _script):
            return [
                {
                    "index": 0,
                    "name": "Time Period",
                    "selectorName": "Time Period",
                }
            ]

        def nth(self, index):
            assert index == 0
            return combo

    class Frame:
        url = "https://app.powerbi.com/report"

        def locator(self, selector):
            assert selector in {
                "button, [role], select, input, textarea",
                '[role="tab"]',
                "select",
            }
            return Locator()

        def get_by_role(self, role):
            assert role == "combobox"
            return Combos()

    class ClearButton:
        async def click(self, **kwargs):
            assert kwargs == {"timeout": 5_000, "no_wait_after": True}
            events.append("clear")

    async def clear_button(_frame, filter_item, control=None):
        assert filter_item["name"] == "Time Period"
        assert control is combo
        events.append("check_clear")
        return ClearButton()

    async def checkbox_options(_page, _frame, _combo, _filter_probe):
        events.append("read_options")
        return (
            object(),
            ["Time Period Nm", "Current", "Previous"],
            ["Current"],
            [{"label": "Current", "selected": True}],
            [],
        )

    async def no_choices(_frame, *_args):
        return []

    async def no_bookmarks(_frame):
        return []

    monkeypatch.setattr(browser, "_clear_selection_button", clear_button)
    monkeypatch.setattr(browser, "_checkbox_filter_options", no_choices)
    monkeypatch.setattr(browser, "_radio_filter_options", no_choices)
    monkeypatch.setattr(browser, "_bookmark_button_groups", no_bookmarks)
    monkeypatch.setattr(
        browser, "_wait_for_combobox_options", checkbox_options
    )

    class Page:
        frames = [Frame()]

    inventory = asyncio.run(browser._accessible_inventory(Page()))

    assert events == ["open", "read_options", "check_clear", "clear"]
    assert inventory["filters"] == []
    assert inventory["initial_cleared_slicers"] == ["Time Period"]


def test_radio_inventory_checks_clear_then_uses_radio_choices(
    monkeypatch,
) -> None:
    events = []

    class Locator:
        async def evaluate_all(self, _script):
            return []

        async def count(self):
            return 0

    class Combo:
        async def is_visible(self):
            return True

        async def click(self, **_kwargs):
            events.append("open")

        async def get_attribute(self, name):
            if name == "aria-expanded":
                return "true"
            assert name in {"aria-controls", "aria-owns"}
            return None

        async def inner_text(self):
            return "Benefits"

        async def press(self, key):
            assert key == "Escape"
            events.append("close")

    combo = Combo()

    class Combos:
        async def evaluate_all(self, _script):
            return [
                {
                    "index": 0,
                    "name": "Service Team",
                    "selectorName": "Service Team Nm",
                }
            ]

        def nth(self, index):
            assert index == 0
            return combo

    class Frame:
        url = "https://app.powerbi.com/report"

        def locator(self, _selector):
            return Locator()

        def get_by_role(self, role):
            if role == "combobox":
                return Combos()
            return Locator()

    async def no_clear(*_args, **_kwargs):
        events.append("check_clear")
        return None

    async def radio_options(*_args):
        events.append("read_options")
        return (
            object(),
            ["Search", "Service Team Nm", "Benefits", "HRIS"],
            ["Benefits"],
            [],
            [
                {"label": "Benefits", "selected": True},
                {"label": "HRIS", "selected": False},
            ],
        )

    async def no_choices(*_args):
        return []

    monkeypatch.setattr(browser, "_clear_selection_button", no_clear)
    monkeypatch.setattr(
        browser, "_wait_for_combobox_options", radio_options
    )
    monkeypatch.setattr(browser, "_checkbox_filter_options", no_choices)
    monkeypatch.setattr(browser, "_radio_filter_options", no_choices)
    monkeypatch.setattr(browser, "_bookmark_button_groups", no_choices)

    class Page:
        frames = [Frame()]

    inventory = asyncio.run(browser._accessible_inventory(Page()))

    assert events == ["open", "read_options", "check_clear", "close"]
    assert inventory["initial_cleared_slicers"] == []
    assert inventory["filters"] == [
        {
            "name": "Service Team",
            "selector_name": "Service Team Nm",
            "selected": ["Benefits"],
            "values": ["Benefits", "HRIS"],
            "clear_selection_checked": True,
            "control_type": "radio_dropdown",
        }
    ]


def test_radio_inventory_clears_filter_and_skips_it_when_clear_is_available(
    monkeypatch,
) -> None:
    events = []

    class Locator:
        async def evaluate_all(self, _script):
            return []

        async def count(self):
            return 0

    class Combo:
        async def is_visible(self):
            return True

        async def click(self, **_kwargs):
            events.append("open")

        async def get_attribute(self, name):
            if name == "aria-expanded":
                return "true"
            assert name in {"aria-controls", "aria-owns"}
            return None

    combo = Combo()

    class Combos:
        async def evaluate_all(self, _script):
            return [
                {
                    "index": 0,
                    "name": "Service Team",
                    "selectorName": "Service Team Nm",
                }
            ]

        def nth(self, index):
            assert index == 0
            return combo

    class Frame:
        url = "https://app.powerbi.com/report"

        def locator(self, _selector):
            return Locator()

        def get_by_role(self, role):
            return Combos() if role == "combobox" else Locator()

    class ClearButton:
        async def click(self, **_kwargs):
            events.append("clear")

    async def read_options(*_args):
        events.append("read_options")
        return (
            object(),
            ["Benefits", "HRIS"],
            ["Benefits"],
            [],
            [{"label": "Benefits", "selected": True}],
        )

    async def clear_button(_frame, filter_item, control=None):
        assert filter_item["name"] == "Service Team"
        assert filter_item["control_type"] == "radio_dropdown"
        assert control is combo
        events.append("check_clear")
        return ClearButton()

    async def no_choices(*_args):
        return []

    monkeypatch.setattr(browser, "_wait_for_combobox_options", read_options)
    monkeypatch.setattr(browser, "_clear_selection_button", clear_button)
    monkeypatch.setattr(browser, "_checkbox_filter_options", no_choices)
    monkeypatch.setattr(browser, "_radio_filter_options", no_choices)
    monkeypatch.setattr(browser, "_bookmark_button_groups", no_choices)

    class Page:
        frames = [Frame()]

    inventory = asyncio.run(browser._accessible_inventory(Page()))

    assert events == ["open", "read_options", "check_clear", "clear"]
    assert inventory["filters"] == []
    assert inventory["initial_cleared_slicers"] == ["Service Team"]


def test_filter_option_uses_popup_search_for_virtualized_value(
    monkeypatch,
) -> None:
    interactions = []

    class SearchField:
        async def is_visible(self):
            return True

        async def evaluate(self, _script):
            return {
                "label": "Search",
                "placeholder": "",
                "role": "",
            }

        async def fill(self, value):
            interactions.append(("search", value))

    search_field = SearchField()

    class Popup:
        async def count(self):
            return 1

        def locator(self, selector):
            assert selector == "input, [role='searchbox']"
            return SimpleNamespace(
                count=lambda: asyncio.sleep(0, result=1),
                nth=lambda _index: search_field,
            )

    class Frame:
        def locator(self, selector):
            assert selector == '[id="service-team-popup"]'
            return Popup()

    class Combo:
        async def get_attribute(self, name):
            if name == "aria-controls":
                return "service-team-popup"
            if name == "aria-expanded":
                return "true"
            return None

    class Page:
        async def wait_for_timeout(self, milliseconds):
            assert milliseconds == 150

    async def get_options(_frame, _combo):
        return object()

    async def click_option(_options, value):
        if ("search", value) in interactions:
            interactions.append(("click", value))
            return value
        return None

    async def read_options(_options):
        return ["HR Support Center", "HR Systems"]

    monkeypatch.setattr(browser, "_options_for_combobox", get_options)
    monkeypatch.setattr(browser, "_click_option_by_text", click_option)
    monkeypatch.setattr(browser, "_accessible_option_texts", read_options)

    asyncio.run(
        browser._click_filter_option(
            Page(), Frame(), Combo(), "Create Job Requisition"
        )
    )

    assert interactions == [
        ("search", "Create Job Requisition"),
        ("click", "Create Job Requisition"),
        ("search", ""),
    ]


def test_filter_option_activates_search_action_for_virtualized_radio_value(
    monkeypatch,
) -> None:
    interactions = []
    search_open = False

    class SearchField:
        async def is_visible(self):
            return search_open

        async def fill(self, value):
            interactions.append(("search", value))

    search_field = SearchField()

    class Combo:
        async def get_attribute(self, name):
            if name == "aria-expanded":
                return "true"
            assert name in {"aria-controls", "aria-owns"}
            return None

    class Page:
        async def wait_for_timeout(self, milliseconds):
            assert milliseconds == 150

    async def get_options(_frame, _combo):
        return object()

    async def find_search_field(_frame, _combo):
        return search_field if search_open else None

    async def activate_search(_frame, _combo):
        nonlocal search_open
        interactions.append(("activate", "Search"))
        search_open = True
        return True

    async def click_option(_options, value):
        if ("search", value) in interactions:
            interactions.append(("click", value))
            return value
        return None

    monkeypatch.setattr(browser, "_options_for_combobox", get_options)
    monkeypatch.setattr(browser, "_combobox_search_field", find_search_field)
    monkeypatch.setattr(
        browser, "_activate_combobox_search_action", activate_search
    )
    monkeypatch.setattr(browser, "_click_option_by_text", click_option)

    asyncio.run(
        browser._click_filter_option(
            Page(),
            object(),
            Combo(),
            "Create Job Requisition",
            filter_name="Service Team",
            activate_search=True,
        )
    )

    assert interactions == [
        ("activate", "Search"),
        ("search", "Create Job Requisition"),
        ("click", "Create Job Requisition"),
        ("search", ""),
    ]


def test_radio_dropdown_searches_first_and_keeps_query_until_verified(
    monkeypatch,
) -> None:
    interactions = []

    class SearchField:
        async def is_visible(self):
            return True

        async def fill(self, value):
            interactions.append(("search", value))

    class Combo:
        async def get_attribute(self, name):
            assert name == "aria-expanded"
            return "true"

    class Page:
        async def wait_for_timeout(self, _milliseconds):
            return None

    async def get_search_field(_frame, _combo):
        return SearchField()

    async def get_options(_frame, _combo):
        return object()

    async def click_option(_options, value):
        assert interactions == [("search", value)]
        interactions.append(("click", value))
        return value

    monkeypatch.setattr(browser, "_combobox_search_field", get_search_field)
    monkeypatch.setattr(browser, "_options_for_combobox", get_options)
    monkeypatch.setattr(browser, "_click_option_by_text", click_option)

    asyncio.run(
        browser._click_filter_option(
            Page(),
            object(),
            Combo(),
            "Payroll",
            filter_name="Service Team",
            activate_search=True,
            clear_search_on_success=False,
            expected_options=["HR Support Center", "Payroll"],
        )
    )

    assert interactions == [("search", "Payroll"), ("click", "Payroll")]


def test_combobox_search_action_ignores_radio_search_option() -> None:
    clicked = []

    class SearchAction:
        def __init__(self, is_radio):
            self.is_radio = is_radio

        async def is_visible(self):
            return True

        async def evaluate(self, _script):
            return {"hasChoiceControl": self.is_radio}

        async def bounding_box(self):
            return {"x": 105, "y": 155, "width": 60, "height": 20}

        async def click(self, **_kwargs):
            clicked.append(self.is_radio)

    actions = [SearchAction(True), SearchAction(False)]

    class SearchActions:
        async def count(self):
            return len(actions)

        def nth(self, index):
            return actions[index]

    class Frame:
        def get_by_text(self, text, exact):
            assert text == "Search"
            assert exact is True
            return SearchActions()

    class Combo:
        async def bounding_box(self):
            return {"x": 100, "y": 100, "width": 100, "height": 40}

    assert asyncio.run(
        browser._activate_combobox_search_action(Frame(), Combo())
    )
    assert clicked == [False]


def test_filter_option_waits_for_delayed_popup_search_field(monkeypatch) -> None:
    search_calls = 0
    search_actions = []
    search_ready = False

    class SearchField:
        async def is_visible(self):
            return True

        async def fill(self, value):
            nonlocal search_ready
            search_actions.append(value)
            search_ready = bool(value)

    search_field = SearchField()

    class Combo:
        async def get_attribute(self, name):
            assert name == "aria-expanded"
            return "true"

    class Page:
        async def wait_for_timeout(self, milliseconds):
            assert milliseconds == 150
            await asyncio.sleep(0)

    async def get_search_field(_frame, _combo):
        nonlocal search_calls
        search_calls += 1
        return search_field if search_calls > 1 else None

    async def get_options(_frame, _combo):
        return object()

    async def click_option(_options, value):
        return value if search_ready else None

    monkeypatch.setattr(browser, "_combobox_search_field", get_search_field)
    monkeypatch.setattr(browser, "_options_for_combobox", get_options)
    monkeypatch.setattr(browser, "_click_option_by_text", click_option)

    asyncio.run(
        browser._click_filter_option(
            Page(), object(), Combo(), "Benefits"
        )
    )

    assert search_calls == 2
    assert search_actions == ["Benefits", ""]


def test_combobox_checks_all_explicitly_related_popup_ids() -> None:
    class Options:
        def __init__(self, available):
            self.available = available

        async def count(self):
            return self.available

    options = Options(1)
    empty = Options(0)

    class Popup:
        def __init__(self, option_count):
            self.option_count = option_count

        async def count(self):
            return 1

        def get_by_role(self, role):
            assert role in {"option", "treeitem", "menuitem"}
            return options if self.option_count else empty

        def locator(self, _selector):
            return empty

    class Frame:
        def locator(self, selector, **_kwargs):
            if selector == '[id="time-period-wrapper"]':
                return Popup(0)
            if selector == '[id="time-period-options"]':
                return Popup(1)
            raise AssertionError(selector)

    class Combo:
        async def get_attribute(self, name):
            if name == "aria-controls":
                return "time-period-wrapper time-period-options"
            return None

    result = asyncio.run(browser._options_for_combobox(Frame(), Combo()))

    assert result is options


def test_combobox_checkbox_choices_are_scoped_to_aria_controls_popup() -> None:
    checkboxes = object()

    class Options:
        async def count(self):
            return 0

    class Checkboxes:
        async def count(self):
            return 2

    class Popup:
        async def count(self):
            return 1

        def get_by_role(self, role):
            assert role == "option"
            return Options()

        def locator(self, selector):
            assert selector == '[role="checkbox"], input[type="checkbox"]'
            return Checkboxes()

    class Frame:
        def locator(self, selector, **_kwargs):
            assert selector == '[id="relative-time-popup"]'
            return Popup()

    class Combo:
        async def get_attribute(self, name):
            return "relative-time-popup" if name == "aria-controls" else None

    result = asyncio.run(browser._options_for_combobox(Frame(), Combo()))

    assert isinstance(result, Checkboxes)


def test_combobox_search_ignores_distant_host_search_field() -> None:
    class SearchField:
        def __init__(self, y):
            self.y = y

        async def is_visible(self):
            return True

        async def evaluate(self, _script):
            return {
                "label": "Search",
                "placeholder": "Search",
                "role": "",
                "type": "text",
            }

        async def bounding_box(self):
            return {"x": 100, "y": self.y, "width": 120, "height": 24}

    class Fields:
        def __init__(self, fields):
            self.fields = fields

        async def count(self):
            return len(self.fields)

        def nth(self, index):
            return self.fields[index]

    host_search = SearchField(20)
    slicer_search = SearchField(340)

    class Frame:
        def __init__(self, fields):
            self.fields = Fields(fields)

        def locator(self, selector):
            assert selector == "input, [role='searchbox']"
            return self.fields

    class Combo:
        async def get_attribute(self, _name):
            return None

        async def bounding_box(self):
            return {"x": 100, "y": 300, "width": 120, "height": 32}

    result = asyncio.run(
        browser._combobox_search_field(
            Frame([host_search, slicer_search]), Combo()
        )
    )

    assert result is slicer_search


def test_combobox_does_not_fall_back_to_an_unrelated_visible_listbox() -> None:
    class Options:
        async def count(self):
            return 0

    options = Options()

    class Checkboxes:
        async def count(self):
            return 0

    class Popup:
        async def count(self):
            return 1

        def get_by_role(self, role):
            assert role in {"option", "treeitem", "menuitem"}
            return options

        def locator(self, selector):
            assert selector in {
                '[role="checkbox"], input[type="checkbox"]',
                '[role="radio"], input[type="radio"]',
                '[role="none"], li, button, [data-testid], [aria-label], [title]',
            }
            return Checkboxes()

    class Frame:
        def locator(self, selector, **_kwargs):
            assert selector == '[id="relative-time-popup"]'
            return Popup()

        def get_by_role(self, role):
            raise AssertionError("Should not inspect unrelated visible listboxes")

    class Combo:
        async def get_attribute(self, name):
            return "relative-time-popup" if name == "aria-controls" else None

    result = asyncio.run(browser._options_for_combobox(Frame(), Combo()))

    assert result is options


def test_combobox_without_a_scoped_popup_does_not_use_global_options() -> None:
    empty_options = object()

    class Frame:
        def get_by_role(self, role):
            raise AssertionError(
                f"Global {role!r} options must not be used for a filter"
            )

        def locator(self, selector):
            assert selector == "xpath=//*[false()]"
            return empty_options

    class Combo:
        async def get_attribute(self, name):
            assert name in {"aria-controls", "aria-owns"}
            return None

    result = asyncio.run(
        browser._options_for_combobox(Frame(), Combo())
    )

    assert result is empty_options


def test_combobox_inventory_waits_for_delayed_popup_options(monkeypatch) -> None:
    locator = object()
    values = iter([[], ["Fiscal Year", "Fiscal Quarter"]])
    waited = []

    async def get_options(_frame, _combo):
        return locator

    async def get_texts(_options):
        return next(values)

    async def get_selected(_options):
        return None

    async def no_checkbox_choices(_frame, _filter):
        return []

    async def no_radio_choices(_frame, _filter):
        return []

    class Page:
        async def wait_for_timeout(self, duration):
            waited.append(duration)
            await asyncio.sleep(0)

    class Combo:
        async def get_attribute(self, name):
            assert name == "aria-expanded"
            return "true"

    monkeypatch.setattr(browser, "_options_for_combobox", get_options)
    monkeypatch.setattr(browser, "_accessible_option_texts", get_texts)
    monkeypatch.setattr(browser, "_selected_combobox_options", get_selected)
    monkeypatch.setattr(browser, "_checkbox_filter_options", no_checkbox_choices)
    monkeypatch.setattr(browser, "_radio_filter_options", no_radio_choices)

    result = asyncio.run(
        browser._wait_for_combobox_options(
            Page(), object(), Combo(), {"name": "Time Period"}
        )
    )

    assert result == (
        locator,
        ["Fiscal Year", "Fiscal Quarter"],
        None,
        [],
        [],
    )
    assert waited == [150]


def test_filter_inventory_failure_saves_popup_diagnostics(tmp_path) -> None:
    selection_history = [
        {"attempt": 1, "phase": "before_click", "selected_values": ["Recruiting"]},
        {"attempt": 1, "phase": "after_click", "selected_values": []},
    ]

    class Combo:
        async def evaluate(self, script):
            assert "relatedPopups" in script
            return {
                "ariaControls": "time-period-popup",
                "relatedPopups": [{"requestedId": "time-period-popup"}],
            }

    class Page:
        async def screenshot(self, path, full_page):
            assert full_page
            Path(path).write_bytes(b"image")

    screenshot_path, diagnostics_path = asyncio.run(
        browser._save_filter_inventory_diagnostics(
            Page(),
            Combo(),
            "Time Period",
            tmp_path,
            selection_history=selection_history,
        )
    )

    assert screenshot_path.is_file()
    assert json.loads(diagnostics_path.read_text(encoding="utf-8")) == {
        "filter_name": "Time Period",
        "requested_option": None,
        "selected_values": None,
        "failure_detail": None,
        "target_locator": None,
        "selection_history": selection_history,
        "controls": {
            "ariaControls": "time-period-popup",
            "relatedPopups": [{"requestedId": "time-period-popup"}],
        },
        "screenshot": screenshot_path.name,
    }


def test_relative_time_combobox_uses_stable_group_heading() -> None:
    combos = [object() for _ in range(4)]

    class Combos:
        async def evaluate_all(self, script):
            assert "[role=\"group\"]" in script
            assert "groupText.split(" in script
            assert "groupTitle" in script
            return [
                {
                    "index": index,
                    "name": "Relative Time" if index == 3 else f"Filter {index}",
                    "selectorName": "Last 4 Weeks",
                }
                for index in range(4)
            ]

        def nth(self, index):
            return combos[index]

    class Frame:
        def get_by_role(self, role, name=None, exact=None):
            assert role == "combobox"
            assert name is None
            return Combos()

    result = asyncio.run(
        browser._combobox_for_filter(
            Frame(),
            {
                "name": "Relative Time",
                "selector_name": "Last 4 Weeks",
                "values": ["Last 4 Weeks", "Other Weeks"],
            },
        )
    )

    assert result is combos[3]


def test_combobox_lookup_does_not_match_another_filter_selector_alias() -> None:
    combos = [object(), object()]

    class Combos:
        async def evaluate_all(self, _script):
            return [
                {
                    "index": 0,
                    "name": "Service Team",
                    "selectorName": "Service Team Nm",
                },
                {
                    "index": 1,
                    "name": "Time Period",
                    "selectorName": "Fiscal Week End Date",
                },
            ]

        def nth(self, index):
            return combos[index]

    class Frame:
        def get_by_role(self, role):
            assert role == "combobox"
            return Combos()

    result = asyncio.run(
        browser._combobox_for_filter(
            Frame(),
            {
                "name": "Time Period",
                "selector_name": "Service Team Nm",
                "values": ["9/26/2026"],
            },
        )
    )

    assert result is combos[1]


def test_filter_option_click_matches_normalized_visible_dom_text() -> None:
    class Options:
        async def evaluate_all(self, script, requested):
            assert "elements.find" in script
            assert requested == "Benefits"
            return {"index": 0, "label": "Benefits"}

        def nth(self, index):
            assert index == 0
            return Option()

    class Option:
        async def click(self, force, timeout):
            assert force is True
            assert timeout == 5_000

    selected = asyncio.run(
        browser._click_option_by_text(Options(), "Benefits")
    )

    assert selected == "Benefits"


def test_slicer_activation_dispatches_on_combobox_element() -> None:
    class Locator:
        options = None

        async def click(self, **kwargs):
            self.options = kwargs

    locator = Locator()

    asyncio.run(browser._click_slicer_control(locator))

    assert locator.options == {
        "force": True,
        "timeout": browser.SLICER_CONTROL_ACTION_TIMEOUT_MS,
    }


def test_close_slicer_dropdown_falls_back_to_toggling_open_combobox() -> None:
    class Combo:
        open = True

        async def get_attribute(self, name):
            assert name == "aria-expanded"
            return "true" if self.open else "false"

        async def press(self, key):
            assert key == "Escape"

        async def click(self, **kwargs):
            assert kwargs == {
                "force": True,
                "timeout": browser.SLICER_CONTROL_ACTION_TIMEOUT_MS,
            }
            self.open = False

    class Page:
        async def wait_for_timeout(self, _ms):
            pass

    combo = Combo()

    asyncio.run(browser._close_slicer_dropdown(Page(), combo))

    assert not combo.open


def test_slicer_dropdown_uses_associated_popup_when_expanded_state_is_stale() -> None:
    class Combo:
        popup_visible = True

        async def get_attribute(self, name):
            assert name == "aria-expanded"
            return "false"

        async def evaluate(self, script):
            assert "aria-controls" in script
            assert "aria-owns" in script
            return self.popup_visible

        async def press(self, key):
            assert key == "Escape"
            self.popup_visible = False

    class Page:
        async def wait_for_timeout(self, _milliseconds):
            pass

    combo = Combo()

    assert asyncio.run(browser._slicer_dropdown_is_open(combo))
    asyncio.run(browser._wait_for_slicer_open(Page(), combo))
    asyncio.run(browser._close_slicer_dropdown(Page(), combo))

    assert not combo.popup_visible


def test_filter_selection_uses_real_click_and_verifies_selected_state() -> None:
    class Combo:
        def __init__(self):
            self.values = {"All", "Benefits"}
            self.open = False

        async def click(self, **kwargs):
            assert kwargs == {
                "force": True,
                "timeout": browser.SLICER_CONTROL_ACTION_TIMEOUT_MS,
            }
            self.open = True

        async def get_attribute(self, name):
            if name == "aria-controls":
                return "filter-popup"
            if name == "aria-expanded":
                return "true" if self.open else "false"
            return None

        async def inner_text(self):
            if len(self.values) > 1:
                return "Multiple selections"
            return ", ".join(sorted(self.values))

        async def press(self, key):
            assert key == "Escape"
            self.open = False

    class Option:
        def __init__(self, combo, value):
            self.combo = combo
            self.value = value

        async def click(self, force, timeout):
            assert force is True
            assert timeout == 5_000
            if self.value in self.combo.values:
                self.combo.values.remove(self.value)
            else:
                self.combo.values.add(self.value)

    class Options:
        def __init__(self, combo):
            self.combo = combo

        async def count(self):
            return 2

        async def evaluate_all(self, _script, value=None):
            values = ["All", "Benefits"]
            if "const states = [" in _script:
                return [
                    {"label": option, "selected": option in self.combo.values}
                    for option in values
                ]
            if value is None:
                return values
            if value not in values:
                return None
            return {"index": values.index(value), "label": value}

        def nth(self, index):
            return Option(self.combo, ["All", "Benefits"][index])

    class Popup:
        def __init__(self, combo):
            self.combo = combo

        async def count(self):
            return 1

        def get_by_role(self, role):
            assert role == "option"
            return Options(self.combo)

    class EmptySelect:
        async def count(self):
            return 0

    class Frame:
        def __init__(self):
            self.combo = Combo()

        def locator(self, selector, **_kwargs):
            if selector == "select":
                return SimpleNamespace(filter=lambda **_kwargs: EmptySelect())
            if selector == "option":
                return SimpleNamespace()
            if selector == '[id="filter-popup"]':
                return Popup(self.combo)
            raise AssertionError(selector)

        def get_by_role(self, role, name=None, exact=None):
            assert role == "combobox"
            assert name is None
            assert exact is None
            return ComboLocator(self.combo)

    class ComboLocator:
        def __init__(self, combo):
            self.combo = combo

        async def evaluate_all(self, _script):
            return [
                {
                    "index": 0,
                    "name": "Service Team",
                    "selectorName": "Service Team",
                }
            ]

        def nth(self, index):
            assert index == 0
            return self.combo

    class Page:
        def __init__(self, frame):
            self.frames = [frame]

        async def wait_for_timeout(self, _ms):
            pass

    frame = Frame()
    filters = [
        {
            "name": "Service Team",
            "selector_name": "Service Team",
            "selected": None,
            "values": ["All", "Benefits"],
        }
    ]
    selection = browser.FilterSelection(name="Service Team", value="Benefits")

    asyncio.run(browser._select_filter(Page(frame), selection, filters))

    assert frame.combo.values == {"Benefits"}
    assert not frame.combo.open


def test_checkbox_filter_sequentially_isolates_each_option(monkeypatch) -> None:
    selected = {"Last 4 Weeks"}
    clicks = []
    frame = object()
    page = SimpleNamespace(frames=[frame], wait_for_timeout=lambda _ms: None)

    async def checkbox_options(_frame, _filter):
        return [
            {"label": value, "selected": value in selected}
            for value in ("Last 4 Weeks", "Other Weeks")
        ]

    async def click_option(_frame, _filter, value):
        clicks.append(value)
        if value in selected:
            selected.remove(value)
        else:
            selected.add(value)

    async def wait_for_selection(_page, _filter, _value):
        return list(selected)

    monkeypatch.setattr(browser, "_checkbox_filter_options", checkbox_options)
    monkeypatch.setattr(browser, "_click_checkbox_filter_option", click_option)
    monkeypatch.setattr(
        browser, "_wait_for_filter_selection", wait_for_selection
    )
    filters = [
        {
            "name": "Relative Time",
            "selector_name": "Relative Time",
            "control_type": "checkbox",
            "selected": ["Last 4 Weeks"],
            "values": ["Last 4 Weeks", "Other Weeks"],
        }
    ]

    asyncio.run(
        browser._select_filter(
            page,
            browser.FilterSelection(name="Relative Time", value="Other Weeks"),
            filters,
        )
    )

    assert clicks == ["Last 4 Weeks", "Other Weeks"]
    assert selected == {"Other Weeks"}


def test_checkbox_filter_does_not_click_exposed_all_option(monkeypatch) -> None:
    selected = {"Benefits"}
    clicks = []
    frame = object()

    async def checkbox_options(_frame, _filter):
        return [
            {"label": value, "selected": value in selected}
            for value in ("All", "Benefits")
        ]

    async def click_option(_frame, _filter, value):
        clicks.append(value)
        if value in selected:
            selected.remove(value)
        else:
            selected.add(value)

    async def wait_for_selection(_page, _filter, _value):
        return list(selected)

    async def current_value(_page, _filter):
        return list(selected)

    monkeypatch.setattr(browser, "_checkbox_filter_options", checkbox_options)
    monkeypatch.setattr(browser, "_click_checkbox_filter_option", click_option)
    monkeypatch.setattr(
        browser, "_wait_for_filter_selection", wait_for_selection
    )
    monkeypatch.setattr(browser, "_current_filter_value", current_value)
    filters = [
        {
            "name": "Service Team",
            "selector_name": "Service Team",
            "control_type": "checkbox",
            "selected": ["Benefits"],
            "values": ["All", "Benefits", "HRIS"],
        }
    ]

    with pytest.raises(RuntimeError, match="Aggregate option 'All'"):
        asyncio.run(
            browser._select_filter(
                SimpleNamespace(frames=[frame]),
                browser.FilterSelection(name="Service Team", value="All"),
                filters,
            )
        )

    assert clicks == []
    assert selected == {"Benefits"}


def test_checkbox_filter_does_not_select_aggregate_all(
    monkeypatch,
) -> None:
    selected = {"Last 4 Weeks"}
    clicks = []
    filter_item = {
        "name": "Relative Time",
        "selector_name": "Relative Time",
        "control_type": "checkbox",
        "selected": ["Last 4 Weeks"],
        "values": ["Last 4 Weeks", "Other Weeks"],
    }

    async def checkbox_options(_frame, _filter):
        return [
            {
                "index": index,
                "label": value,
                "selected": value in selected,
            }
            for index, value in enumerate(filter_item["values"])
        ]

    async def click_option(_frame, _filter, value):
        clicks.append(value)
        selected.add(value)

    async def current_value(_page, _filter):
        return list(selected)

    monkeypatch.setattr(browser, "_checkbox_filter_options", checkbox_options)
    monkeypatch.setattr(browser, "_click_checkbox_filter_option", click_option)
    monkeypatch.setattr(browser, "_current_filter_value", current_value)

    with pytest.raises(RuntimeError, match="Aggregate option 'All'"):
        asyncio.run(
            browser._select_filter(
                SimpleNamespace(frames=[object()]),
                browser.FilterSelection("Relative Time", "All"),
                [filter_item],
            )
        )

    assert clicks == []
    assert selected == {"Last 4 Weeks"}


def test_checkbox_dropdown_does_not_select_aggregate_all(
    monkeypatch,
) -> None:
    selected = {"Last 4 Weeks"}
    clicks = []
    frame = object()
    filter_item = {
        "name": "Relative Time",
        "selector_name": "Relative Time",
        "control_type": "checkbox_dropdown",
        "selected": ["Last 4 Weeks"],
        "values": ["Last 4 Weeks", "Other Weeks"],
    }

    class Combo:
        async def get_attribute(self, _name):
            return "false"

    async def combo_for_filter(_frame, _filter):
        return Combo()

    async def current_value(_page, _filter):
        return list(selected)

    async def options_for_combo(_frame, _combo):
        return object()

    async def option_texts(_options):
        return ["Last 4 Weeks", "Other Weeks"]

    async def selected_options(_options):
        return list(selected)

    async def click_option(
        _page,
        _frame,
        _combo,
        value,
        *,
        filter_name=None,
        expected_options=None,
    ):
        clicks.append(value)
        selected.add(value)

    async def no_wait(*_args):
        return None

    async def close_dropdown(*_args):
        return None

    monkeypatch.setattr(browser, "_combobox_for_filter", combo_for_filter)
    monkeypatch.setattr(browser, "_current_filter_value", current_value)
    monkeypatch.setattr(browser, "_options_for_combobox", options_for_combo)
    monkeypatch.setattr(browser, "_accessible_option_texts", option_texts)
    monkeypatch.setattr(
        browser, "_selected_combobox_options", selected_options
    )
    monkeypatch.setattr(browser, "_click_slicer_control", no_wait)
    monkeypatch.setattr(browser, "_wait_for_slicer_open", no_wait)
    monkeypatch.setattr(browser, "_click_filter_option", click_option)
    monkeypatch.setattr(browser, "_close_slicer_dropdown", close_dropdown)

    with pytest.raises(RuntimeError, match="Aggregate option 'All'"):
        asyncio.run(
            browser._select_filter(
                SimpleNamespace(
                    frames=[frame], wait_for_timeout=lambda _milliseconds: None
                ),
                browser.FilterSelection("Relative Time", "All"),
                [filter_item],
            )
        )

    assert clicks == []
    assert selected == {"Last 4 Weeks"}


def test_radio_filter_selects_individual_values_without_clear(
    monkeypatch,
) -> None:
    clicked = []
    frame = object()
    filter_item = {
        "name": "Relative Time",
        "selector_name": "Relative Time",
        "control_type": "radio",
        "selected": ["Last 4 Weeks"],
        "values": ["All", "Last 4 Weeks", "Other Weeks"],
    }
    selected = ["Last 4 Weeks"]

    async def radio_options(_frame, _filter):
        return [
            {
                "index": index,
                "label": value,
                "selected": value in selected,
            }
            for index, value in enumerate(filter_item["values"])
        ]

    async def click_option(_frame, _filter, value):
        clicked.append(value)
        selected[:] = [value]

    async def wait_for_selection(_page, _filter, value):
        return [value]

    async def current_value(_page, _filter):
        return list(selected)

    async def no_clear_button(_frame, _filter):
        return None

    monkeypatch.setattr(browser, "_radio_filter_options", radio_options)
    monkeypatch.setattr(browser, "_click_radio_filter_option", click_option)
    monkeypatch.setattr(
        browser, "_wait_for_filter_selection", wait_for_selection
    )
    monkeypatch.setattr(
        browser, "_clear_selection_button", no_clear_button
    )
    monkeypatch.setattr(browser, "_current_filter_value", current_value)

    class Page:
        frames = [frame]

    asyncio.run(browser._prepare_slicer_selections(Page(), [filter_item]))
    asyncio.run(
        browser._select_filter(
            Page(),
            browser.FilterSelection("Relative Time", "Other Weeks"),
            [filter_item],
        )
    )

    assert filter_item["reset_strategy"] == "preserve_selection"
    assert clicked == ["Other Weeks", "Other Weeks"]
    assert filter_item["values"] == ["Last 4 Weeks", "Other Weeks"]


def test_radio_dropdown_clicks_and_verifies_each_single_choice(
    monkeypatch,
) -> None:
    selected = ["Benefits"]
    clicks = []
    expanded = False
    combo = object()
    options = object()
    filter_item = {
        "name": "Service Team",
        "selector_name": "Service Team Nm",
        "control_type": "radio_dropdown",
        "selected": ["Benefits"],
        "values": ["Benefits", "Compensation", "Compliance"],
    }

    async def find_combo(_frame, _filter):
        return combo

    async def click_control(_combo):
        nonlocal expanded
        expanded = True
        clicks.append("open")

    async def wait_open(_page, _combo):
        return None

    async def get_options(_frame, _combo):
        return options

    async def select_radio_option(
        _page, _frame, _combo, _filter, value, diagnostic_directory=None
    ):
        nonlocal expanded
        assert diagnostic_directory is None
        expanded = True
        clicks.append("open")
        clicks.append(value)
        selected[:] = [value]
        clicks.append("verified")
        return list(selected)

    async def close_dropdown(_page, _combo):
        nonlocal expanded
        expanded = False
        clicks.append("close")

    async def wait_selection(_page, _filter, _value):
        clicks.append("verified")
        return list(selected)

    async def wait_timeout(_milliseconds):
        return None

    async def no_search_field(_frame, _combo):
        return None

    async def no_search_action(_frame, _combo):
        return False

    monkeypatch.setattr(browser, "_combobox_for_filter", find_combo)
    monkeypatch.setattr(browser, "_click_slicer_control", click_control)
    monkeypatch.setattr(browser, "_wait_for_slicer_open", wait_open)
    monkeypatch.setattr(browser, "_options_for_combobox", get_options)
    monkeypatch.setattr(
        browser, "_select_radio_dropdown_option", select_radio_option
    )
    monkeypatch.setattr(browser, "_combobox_search_field", no_search_field)
    monkeypatch.setattr(
        browser, "_activate_combobox_search_action", no_search_action
    )
    monkeypatch.setattr(browser, "_close_slicer_dropdown", close_dropdown)
    monkeypatch.setattr(
        browser, "_wait_for_filter_selection", wait_selection
    )

    class Combo:
        async def get_attribute(self, name):
            assert name == "aria-expanded"
            return "true" if expanded else "false"

    combo = Combo()

    asyncio.run(
        browser._select_filter(
            SimpleNamespace(
                frames=[object()], wait_for_timeout=wait_timeout
            ),
            browser.FilterSelection("Service Team", "Compensation"),
            [filter_item],
        )
    )

    assert clicks == [
        "open",
        "Compensation",
        "verified",
        "close",
        "verified",
    ]
    assert selected == ["Compensation"]


def test_radio_dropdown_finds_exact_visible_option_in_scoped_popup() -> None:
    class Options:
        requested_index = None

        async def evaluate_all(self, _script):
            return [
                {"index": 0, "label": "HR Support Center", "visible": True},
                {"index": 1, "label": "Payroll", "visible": False},
                {"index": 2, "label": "Payroll Support", "visible": True},
                {"index": 3, "label": "Payroll", "visible": True},
            ]

        def nth(self, index):
            self.requested_index = index
            return f"option-{index}"

    options = Options()
    result = asyncio.run(
        browser._radio_dropdown_option_locator(
            options, "Payroll", "Service Team"
        )
    )

    assert result == "option-3"
    assert options.requested_index == 3


def test_radio_dropdown_keeps_verified_option_locator_in_listbox() -> None:
    class NamedOptions:
        async def count(self):
            return 1

    named_options = NamedOptions()

    class Listbox:
        def get_by_role(self, role, *, name, exact):
            assert role == "option"
            assert name == "Payroll"
            assert exact is True
            return named_options

        async def count(self):
            return 1

    listbox = Listbox()

    class Candidate:
        def locator(self, selector):
            assert selector == (
                'xpath=ancestor-or-self::*[@role="listbox"][1]'
            )
            return listbox

    candidate = Candidate()

    class Options:
        async def evaluate_all(self, _script):
            return [
                {"index": 0, "label": "Payroll", "visible": True}
            ]

        def nth(self, _index):
            return candidate

    result = asyncio.run(
        browser._radio_dropdown_option_locator(
            Options(), "Payroll", "Service Team"
        )
    )

    assert result is candidate


def test_radio_dropdown_rejects_duplicate_visible_exact_options() -> None:
    class Options:
        async def evaluate_all(self, _script):
            return [
                {"index": 2, "label": "Payroll", "visible": True},
                {"index": 8, "label": "Payroll", "visible": True},
            ]

    with pytest.raises(
        RuntimeError,
        match="Multiple visible options match 'Payroll' in filter 'Service Team'",
    ):
        asyncio.run(
            browser._radio_dropdown_option_locator(
                Options(), "Payroll", "Service Team"
            )
        )


def test_radio_dropdown_resolves_nested_duplicate_option_nodes() -> None:
    class Listbox:
        async def count(self):
            return 1

        def get_by_role(self, role, *, name, exact):
            assert role == "option"
            assert name == "Payroll"
            assert exact is True

            class NamedOptions:
                async def count(self):
                    return 2

            return NamedOptions()

    class Candidate:
        def __init__(self, index):
            self.index = index

        def locator(self, selector):
            assert selector == (
                'xpath=ancestor-or-self::*[@role="listbox"][1]'
            )
            return Listbox()

    class Options:
        requested_index = None

        async def evaluate_all(self, _script):
            return [
                {
                    "index": 2,
                    "label": "Payroll",
                    "visible": True,
                    "nestedOptionIndices": [8],
                    "disabled": False,
                },
                {
                    "index": 8,
                    "label": "Payroll",
                    "visible": True,
                    "nestedOptionIndices": [],
                    "disabled": False,
                },
            ]

        def nth(self, index):
            self.requested_index = index
            return Candidate(index)

    options = Options()
    result = asyncio.run(
        browser._radio_dropdown_option_locator(
            options, "Payroll", "Service Team"
        )
    )

    assert isinstance(result, Candidate)
    assert result.index == 8
    assert options.requested_index == 8


def test_radio_dropdown_scroll_targets_listbox_scroll_container(
    monkeypatch,
) -> None:
    class Option:
        script = ""
        scroll_calls = 0

        async def scroll_into_view_if_needed(self, **kwargs):
            assert kwargs == {"timeout": 2_000}
            self.scroll_calls += 1

        async def evaluate(self, script):
            self.script = script
            return {
                "visible": True,
                "containerClass": "scroll-content",
                "scrollTop": 42,
                "scrollBounds": {"x": 1, "y": 2, "width": 100, "height": 80},
            }

    class Options:
        async def evaluate_all(self, _script):
            return [
                {"index": 0, "label": "Payroll", "visible": True}
            ]

        def nth(self, _index):
            return option

    class Mouse:
        async def move(self, *_args):
            raise AssertionError("Visible options should not require wheel input")

        async def wheel(self, *_args):
            raise AssertionError("Visible options should not require wheel input")

    class Page:
        mouse = Mouse()

        async def wait_for_timeout(self, _milliseconds):
            await asyncio.sleep(0)

    async def get_options(_frame, _combo):
        return options

    option = Option()
    options = Options()
    monkeypatch.setattr(browser, "_options_for_combobox", get_options)
    result, state = asyncio.run(
        browser._scroll_filter_option_into_view(
            Page(), object(), object(), "Payroll", "Service Team", ["Payroll"]
        )
    )

    assert result is option
    assert state["visible"] is True
    assert state["containerClass"] == "scroll-content"
    assert option.scroll_calls == 1
    assert "element.closest('[role=\"listbox\"]')" in option.script
    assert "scrollContainer.scrollTop" not in option.script
    assert "scroll-content" in option.script


def test_radio_dropdown_does_not_wheel_when_popup_has_no_scroll_range(
    monkeypatch,
) -> None:
    class Anchor:
        async def evaluate(self, _script):
            return {
                "x": 10,
                "y": 20,
                "width": 120,
                "height": 80,
                "scrollTop": 0,
                "clientHeight": 179,
                "scrollHeight": 179,
            }

    class Options:
        async def evaluate_all(self, _script):
            return [
                {
                    "index": 0,
                    "label": "HR Support",
                    "visible": True,
                    "position": 1,
                }
            ]

        def nth(self, _index):
            return anchor

    class Mouse:
        async def move(self, *_args):
            raise AssertionError("A non-scrollable popup must not be wheeled")

        async def wheel(self, *_args):
            raise AssertionError("A non-scrollable popup must not be wheeled")

    class Page:
        mouse = Mouse()

    async def get_options(_frame, _combo):
        return options

    anchor = Anchor()
    options = Options()
    monkeypatch.setattr(browser, "_options_for_combobox", get_options)

    result, state = asyncio.run(
        browser._scroll_filter_option_into_view(
            Page(),
            object(),
            object(),
            "Benefits",
            "Service Team",
            ["Benefits", "HR Support"],
        )
    )

    assert result is None
    assert state["visible"] is False
    assert "no scrollable content" in state["reason"]


def test_radio_dropdown_reacquires_option_after_wheeling_inside_listbox(
    monkeypatch,
) -> None:
    class Option:
        calls = 0

        async def evaluate(self, _script):
            self.calls += 1
            return {
                "visible": self.calls == 2,
                "wheelDelta": 100,
                "scrollBounds": {
                    "x": 10,
                    "y": 20,
                    "width": 120,
                    "height": 80,
                },
            }

    class Options:
        locator_reads = 0

        async def evaluate_all(self, _script):
            return [{"index": 3, "label": "Benefits", "visible": True}]

        def nth(self, _index):
            self.locator_reads += 1
            return option

    class Mouse:
        moves = []
        wheel_events = []

        async def move(self, x, y):
            self.moves.append((x, y))

        async def wheel(self, x, y):
            self.wheel_events.append((x, y))

    class Page:
        mouse = Mouse()

        async def wait_for_timeout(self, _milliseconds):
            await asyncio.sleep(0)

    async def get_options(_frame, _combo):
        return options

    option = Option()
    options = Options()
    monkeypatch.setattr(browser, "_options_for_combobox", get_options)

    result, state = asyncio.run(
        browser._scroll_filter_option_into_view(
            Page(),
            object(),
            object(),
            "Benefits",
            "Service Team",
            ["Benefits", "Compensation"],
        )
    )

    assert result is option
    assert state["visible"] is True
    assert option.calls == 2
    assert options.locator_reads == 2
    assert Page.mouse.moves == [(70, 60)]
    assert Page.mouse.wheel_events == [(0, 100)]


def test_radio_dropdown_retries_when_option_rerenders_during_visibility_check(
    monkeypatch,
) -> None:
    class Option:
        calls = 0

        async def scroll_into_view_if_needed(self, **kwargs):
            assert kwargs == {"timeout": 2_000}

        async def evaluate(self, _script):
            self.calls += 1
            if self.calls == 1:
                raise asyncio.TimeoutError
            return {"visible": True}

    class Options:
        async def evaluate_all(self, _script):
            return [{"index": 0, "label": "Payroll", "visible": True}]

    class Mouse:
        async def move(self, *_args):
            raise AssertionError("Visible options should not require wheel input")

        async def wheel(self, *_args):
            raise AssertionError("Visible options should not require wheel input")

    class Page:
        mouse = Mouse()

        async def wait_for_timeout(self, _milliseconds):
            await asyncio.sleep(0)

    async def get_options(_frame, _combo):
        return options

    async def get_option(*_args):
        return option

    option = Option()
    options = Options()
    monkeypatch.setattr(browser, "_options_for_combobox", get_options)
    monkeypatch.setattr(browser, "_radio_dropdown_option_locator", get_option)

    result, state = asyncio.run(
        browser._scroll_filter_option_into_view(
            Page(), object(), object(), "Payroll", "Service Team", ["Payroll"]
        )
    )

    assert result is option
    assert state["visible"] is True
    assert option.calls == 2


def test_radio_dropdown_scrolls_to_virtualized_option_by_inventory_position(
    monkeypatch,
) -> None:
    class Anchor:
        async def evaluate(self, _script):
            return {"x": 10, "y": 20, "width": 120, "height": 80}

    class Option:
        async def evaluate(self, _script):
            return {"visible": True, "wheelDelta": 0}

    class Options:
        reads = 0
        target_rendered = False

        async def evaluate_all(self, _script):
            self.reads += 1
            if self.target_rendered:
                return [
                    {"index": 0, "label": "Benefits", "visible": True, "position": 1}
                ]
            return [
                {
                    "index": 0,
                    "label": "Compensation",
                    "visible": True,
                    "position": 2,
                }
            ]

        def nth(self, _index):
            return option if self.target_rendered else anchor

    class Mouse:
        wheel_events = []

        async def move(self, *_args):
            return None

        async def wheel(self, _x, delta):
            self.wheel_events.append(delta)
            options.target_rendered = True

    class Page:
        mouse = Mouse()

        async def wait_for_timeout(self, _milliseconds):
            await asyncio.sleep(0)

    async def get_options(_frame, _combo):
        return options

    anchor = Anchor()
    option = Option()
    options = Options()
    monkeypatch.setattr(browser, "_options_for_combobox", get_options)

    result, state = asyncio.run(
        browser._scroll_filter_option_into_view(
            Page(),
            object(),
            object(),
            "Benefits",
            "Service Team",
            ["Benefits", "Compensation"],
        )
    )

    assert result is option
    assert state["visible"] is True
    assert Mouse.wheel_events == [-160]
    assert options.reads >= 4


def test_radio_dropdown_does_not_scroll_when_inventory_position_is_visible(
    monkeypatch,
) -> None:
    class Anchor:
        async def evaluate(self, _script):
            return {
                "x": 10,
                "y": 20,
                "width": 120,
                "height": 80,
                "scrollTop": 0,
                "clientHeight": 179,
                "scrollHeight": 209,
            }

    class Options:
        async def evaluate_all(self, _script):
            labels = [
                "Benefits",
                "Compensation",
                "HR Business Partners",
                "HR Support Center",
                "HRIS",
                "Learning",
                "Legal Service Team",
                "Payroll",
                "Recruiting",
                "Risk Management",
                "Talent",
            ]
            return [
                {
                    "index": index,
                    "label": label,
                    "visible": True,
                    "position": index + 1,
                }
                for index, label in enumerate(labels)
            ]

        def nth(self, _index):
            return anchor

    class Mouse:
        async def move(self, *_args):
            raise AssertionError(
                "Do not wheel when the plan index is already visible"
            )

        async def wheel(self, *_args):
            raise AssertionError(
                "Do not wheel when the plan index is already visible"
            )

    class Page:
        mouse = Mouse()

    async def get_options(_frame, _combo):
        return options

    anchor = Anchor()
    options = Options()
    monkeypatch.setattr(browser, "_options_for_combobox", get_options)

    result, state = asyncio.run(
        browser._scroll_filter_option_into_view(
            Page(),
            object(),
            object(),
            "Compliance",
            "Service Team",
            [
                "Benefits",
                "Compensation",
                "Compliance",
                "Create Job Requisition",
                "HR Business Partners",
                "HR Support Center",
                "HRIS",
                "Learning",
                "Legal Service Team",
                "Payroll",
                "Recruiting",
                "Risk Management",
                "Talent",
            ],
        )
    )

    assert result is None
    assert state["expected_position"] == 3
    assert state["visible_positions"] == list(range(1, 12))
    assert "Compliance" not in state["visible_options"]
    assert "falls within the currently rendered range" in state["reason"]


def test_radio_dropdown_selected_labels_use_inspected_group_state() -> None:
    assert browser._radio_dropdown_selected_labels(
        [
            {
                "label": "HR Support Center",
                "selected": False,
                "role": "option",
                "ariaChecked": "false",
            },
            {
                "label": "Payroll",
                "selected": True,
                "role": "option",
                "ariaChecked": "true",
            },
        ],
        "Service Team",
    ) == ["Payroll"]

    assert browser._radio_dropdown_selected_labels(
        [
            {"label": "HR Support Center", "selected": False},
            {"label": "Payroll", "selected": False},
        ],
        "Service Team",
    ) == []


def test_radio_dropdown_selection_reads_options_only_from_associated_listbox(
    monkeypatch,
) -> None:
    loaded = False

    class OptionNodes:
        async def count(self):
            return 2 if loaded else 0

        async def evaluate_all(self, script):
            assert script == browser.RADIO_DROPDOWN_SELECTION_SCRIPT
            return [
                {
                    "label": "HR Support",
                    "selected": True,
                    "role": "option",
                    "ariaChecked": "true",
                },
                {
                    "label": "Benefits",
                    "selected": False,
                    "role": "option",
                    "ariaChecked": "false",
                },
            ]

    option_nodes = OptionNodes()

    class Listbox:
        async def is_visible(self):
            return True

        async def get_attribute(self, name):
            return "Service Team Nm" if name == "aria-label" else None

        def locator(self, selector):
            assert selector == (
                '[role="option"], [role="radio"], input[type="radio"]'
            )
            return option_nodes

    listbox = Listbox()

    class Listboxes:
        async def count(self):
            return 1

        def nth(self, _index):
            return listbox

    class Popup:
        async def count(self):
            return 1

        def locator(self, selector):
            assert selector == '[role="listbox"]'
            return Listboxes()

    popup = Popup()

    class Frame:
        def locator(self, selector):
            assert selector == '[id="service-team-popup"]'
            return popup

    class Combo:
        async def get_attribute(self, name):
            return {
                "aria-expanded": "true",
                "aria-controls": "service-team-popup",
                "aria-owns": None,
            }[name]

    class Page:
        async def wait_for_timeout(self, milliseconds):
            nonlocal loaded
            assert milliseconds == 100
            loaded = True

    async def popup_open(_combo):
        return True

    async def unrelated_options(*_args):
        raise AssertionError(
            "Selection inspection must not use generic popup candidates"
        )

    monkeypatch.setattr(browser, "_slicer_dropdown_is_open", popup_open)
    monkeypatch.setattr(browser, "_options_for_combobox", unrelated_options)

    result = asyncio.run(
        browser._radio_dropdown_selection(
            Page(), Frame(), Combo(), "Service Team"
        )
    )

    assert result == ["HR Support"]
    assert loaded is True


def test_radio_dropdown_reopens_empty_popup_before_reading_selection(
    monkeypatch,
) -> None:
    state = {"open": True, "ready": False}
    clock = {"seconds": 0.0}

    class OptionNodes:
        async def count(self):
            return 1

        async def evaluate_all(self, script):
            assert script == browser.RADIO_DROPDOWN_SELECTION_SCRIPT
            return [{"label": "Talent", "selected": True}]

    class Listbox:
        async def is_visible(self):
            return True

        async def get_attribute(self, name):
            return "Service Team Nm" if name == "aria-label" else None

        def locator(self, _selector):
            return option_nodes

    class Listboxes:
        async def count(self):
            return 1 if state["ready"] else 0

        def nth(self, _index):
            return listbox

    class Popup:
        async def count(self):
            return 1

        def locator(self, _selector):
            return listboxes

    class Frame:
        def locator(self, _selector):
            return popup

    class Combo:
        async def get_attribute(self, name):
            return "service-team-popup" if name == "aria-controls" else None

    class Page:
        async def wait_for_timeout(self, _milliseconds):
            clock["seconds"] += 4
            return None

    async def is_open(_combo):
        return state["open"]

    async def close_dropdown(_page, _combo):
        state["open"] = False

    async def click_control(_combo):
        state["open"] = True
        state["ready"] = True

    async def wait_for_open(_page, _combo):
        return None

    monkeypatch.setattr(browser.time, "monotonic", lambda: clock["seconds"])
    monkeypatch.setattr(browser, "_slicer_dropdown_is_open", is_open)
    monkeypatch.setattr(browser, "_close_slicer_dropdown", close_dropdown)
    monkeypatch.setattr(browser, "_click_slicer_control", click_control)
    monkeypatch.setattr(browser, "_wait_for_slicer_open", wait_for_open)

    option_nodes = OptionNodes()
    listbox = Listbox()
    listboxes = Listboxes()
    popup = Popup()

    result = asyncio.run(
        browser._radio_dropdown_selection(
            Page(), Frame(), Combo(), "Service Team"
        )
    )

    assert result == ["Talent"]
    assert state == {"open": True, "ready": True}


def test_radio_dropdown_selected_labels_reject_unreadable_state() -> None:
    with pytest.raises(
        browser.FilterSelectionInspectionError,
        match="some popup options expose no checked/selected state",
    ):
        browser._radio_dropdown_selected_labels(
            [
                {"label": "HR Support Center", "selected": None},
                {"label": "Payroll", "selected": None},
            ],
            "Service Team",
        )


@pytest.mark.parametrize(
    ("previous_selection", "requested_value"),
    [
        ("HR Support Center", "Payroll"),
        ("Risk Management", "Payroll"),
        ("HR Support Center", "Benefits"),
    ],
)
def test_radio_dropdown_replaces_previous_selection(
    monkeypatch, previous_selection, requested_value
) -> None:
    selected = [previous_selection]
    clicks = []
    monkeypatch.setattr(
        browser, "time", SimpleNamespace(monotonic=lambda: 0.0)
    )

    class Options:
        pass

    class EmptyLocator:
        async def count(self):
            return 0

    class Option:
        async def is_visible(self):
            return True

        async def is_enabled(self):
            return True

        def locator(self, _selector):
            return EmptyLocator()

        async def click(self, **kwargs):
            assert kwargs == {"force": True, "timeout": 5_000}
            clicks.append(f"{requested_value} role=option")
            selected[:] = [requested_value]

        async def press(self, key, **_kwargs):
            raise AssertionError(
                f"Keyboard interaction {key!r} is invalid for a role=option"
            )

    class Page:
        async def wait_for_timeout(self, _milliseconds):
            await asyncio.sleep(0)

    options = Options()
    option = Option()

    async def popup_open(_combo):
        return True

    async def get_options(_frame, _combo):
        return options

    async def read_selection(_page, _frame, _combo, _filter_name):
        return list(selected)

    async def find_option(_options, _value, _filter_name):
        return option

    async def scroll_option(
        _page, _frame, _combo, _value, _filter_name, _expected_options
    ):
        return option, {"visible": True}

    monkeypatch.setattr(browser, "_slicer_dropdown_is_open", popup_open)
    monkeypatch.setattr(browser, "_options_for_combobox", get_options)
    monkeypatch.setattr(browser, "_radio_dropdown_selection", read_selection)
    monkeypatch.setattr(
        browser, "_radio_dropdown_option_locator", find_option
    )
    monkeypatch.setattr(
        browser, "_scroll_filter_option_into_view", scroll_option
    )

    async def select_option():
        return await browser._select_radio_dropdown_option(
            Page(),
            object(),
            object(),
            {"name": "Service Team"},
            requested_value,
        )

    assert asyncio.run(select_option()) == [requested_value]
    assert clicks == [f"{requested_value} role=option"]


def test_radio_dropdown_clears_unmatched_search_before_scrolling(
    monkeypatch,
) -> None:
    selected = ["HR Support"]
    clicks = []

    class SearchField:
        value = None
        fills = []

        async def fill(self, value):
            self.value = value
            self.fills.append(value)

    class Options:
        pass

    class EmptyLocator:
        async def count(self):
            return 0

    class Option:
        async def is_visible(self):
            return True

        async def is_enabled(self):
            return True

        def locator(self, _selector):
            return EmptyLocator()

        async def click(self, **_kwargs):
            clicks.append("Benefits")
            selected[:] = ["Benefits"]

    class Page:
        async def wait_for_timeout(self, _milliseconds):
            await asyncio.sleep(0)

    search_field = SearchField()
    options = Options()
    option = Option()
    cleared_query_reads = 0

    async def popup_open(_combo):
        return True

    async def get_options(_frame, _combo):
        return options

    async def read_selection(_page, _frame, _combo, _filter_name):
        return list(selected)

    async def find_option(_options, _value, _filter_name):
        nonlocal cleared_query_reads
        if search_field.value != "":
            return None
        cleared_query_reads += 1
        return option if cleared_query_reads >= 5 else None

    async def get_search_field(_frame, _combo):
        return search_field

    async def no_search_action(_frame, _combo):
        return False

    async def scroll_option(
        _page, _frame, _combo, _value, _filter_name, _expected_options
    ):
        assert search_field.value == ""
        return option, {"visible": True}

    monkeypatch.setattr(browser, "_slicer_dropdown_is_open", popup_open)
    monkeypatch.setattr(browser, "_options_for_combobox", get_options)
    monkeypatch.setattr(browser, "_radio_dropdown_selection", read_selection)
    monkeypatch.setattr(browser, "_radio_dropdown_option_locator", find_option)
    monkeypatch.setattr(browser, "_combobox_search_field", get_search_field)
    monkeypatch.setattr(
        browser, "_activate_combobox_search_action", no_search_action
    )
    monkeypatch.setattr(
        browser, "_scroll_filter_option_into_view", scroll_option
    )

    result = asyncio.run(
        browser._select_radio_dropdown_option(
            Page(),
            object(),
            object(),
            {"name": "Service Team", "values": ["Benefits", "HR Support"]},
            "Benefits",
        )
    )

    assert result == ["Benefits"]
    assert search_field.fills == ["Benefits", ""]
    assert cleared_query_reads >= 5
    assert clicks == ["Benefits"]


@pytest.mark.parametrize(
    "reason",
    [
        "exact option is not rendered and the active popup has no scrollable content",
        "exact option is absent from the active popup even though its inventory position falls within the currently rendered range",
    ],
)
def test_radio_dropdown_reports_unavailable_option_without_scroll_range(
    monkeypatch, tmp_path: Path, reason: str
) -> None:
    diagnostics = []
    selected = ["HR Support"]
    scroll_state = {
        "visible": False,
        "reason": reason,
        "visible_positions": [1],
        "visible_options": ["HR Support"],
    }

    class Page:
        async def wait_for_timeout(self, _milliseconds):
            await asyncio.sleep(0)

    async def popup_open(_combo):
        return True

    async def get_options(_frame, _combo):
        return object()

    async def read_selection(_page, _frame, _combo, _filter_name):
        return list(selected)

    async def find_option(_options, _value, _filter_name):
        return None

    async def no_search_field(_frame, _combo):
        return None

    async def no_search_action(_frame, _combo):
        return False

    async def no_scroll(
        _page, _frame, _combo, _value, _filter_name, _expected_options
    ):
        return None, scroll_state

    async def save_diagnostics(
        _page, _combo, _filter_name, _directory, **details
    ):
        diagnostics.append(details)
        return tmp_path / "failure.png", tmp_path / "failure.json"

    monkeypatch.setattr(
        browser, "time", SimpleNamespace(monotonic=lambda: 0.0)
    )
    monkeypatch.setattr(browser, "_slicer_dropdown_is_open", popup_open)
    monkeypatch.setattr(browser, "_options_for_combobox", get_options)
    monkeypatch.setattr(browser, "_radio_dropdown_selection", read_selection)
    monkeypatch.setattr(browser, "_radio_dropdown_option_locator", find_option)
    monkeypatch.setattr(browser, "_combobox_search_field", no_search_field)
    monkeypatch.setattr(
        browser, "_activate_combobox_search_action", no_search_action
    )
    monkeypatch.setattr(browser, "_scroll_filter_option_into_view", no_scroll)
    monkeypatch.setattr(
        browser, "_save_filter_inventory_diagnostics", save_diagnostics
    )
    monkeypatch.setattr(
        browser, "_close_slicer_dropdown", lambda *_args: asyncio.sleep(0)
    )

    with pytest.raises(
        browser.UnavailableFilterOptionError,
        match="visible options: \\['HR Support'\\]",
    ):
        asyncio.run(
            browser._select_radio_dropdown_option(
                Page(),
                object(),
                object(),
                {"name": "Service Team", "values": ["Benefits", "HR Support"]},
                "Benefits",
                diagnostic_directory=tmp_path,
            )
        )

    assert diagnostics[0]["last_scroll_state"] == scroll_state
    assert diagnostics[0]["selected_values"] == ["HR Support"]


def test_radio_dropdown_reopens_stale_popup_before_retrying_option(
    monkeypatch,
) -> None:
    state = {"open": True, "refreshed": False, "selected": "HR Support"}
    reopen_calls = []

    class Option:
        async def is_visible(self):
            return True

        async def is_enabled(self):
            return True

        def locator(self, _selector):
            return EmptyLocator()

        async def click(self, **_kwargs):
            state["selected"] = "Benefits"

    class EmptyLocator:
        async def count(self):
            return 0

    class Page:
        async def wait_for_timeout(self, _milliseconds):
            await asyncio.sleep(0)

    option = Option()

    async def popup_open(_combo):
        return state["open"]

    async def close_popup(_page, _combo):
        reopen_calls.append("closed")
        state["open"] = False

    async def click_control(_combo):
        reopen_calls.append("opened")
        state["open"] = True
        state["refreshed"] = True

    async def wait_for_open(_page, _combo):
        return None

    async def read_selection(_page, _frame, _combo, _filter_name):
        return [state["selected"]]

    async def find_option(_options, _value, _filter_name):
        return option if state["refreshed"] else None

    async def no_search_field(_frame, _combo):
        return None

    async def no_search_action(_frame, _combo):
        return False

    async def scroll_option(
        _page, _frame, _combo, _value, _filter_name, _expected_options
    ):
        if state["refreshed"]:
            return option, {"visible": True}
        return None, {
            "visible": False,
            "reason": "stale popup does not contain the requested option",
        }

    monkeypatch.setattr(browser, "_slicer_dropdown_is_open", popup_open)
    monkeypatch.setattr(browser, "_close_slicer_dropdown", close_popup)
    monkeypatch.setattr(browser, "_click_slicer_control", click_control)
    monkeypatch.setattr(browser, "_wait_for_slicer_open", wait_for_open)
    async def options_for_combobox(*_args):
        return object()

    monkeypatch.setattr(
        browser, "_options_for_combobox", options_for_combobox
    )
    monkeypatch.setattr(browser, "_radio_dropdown_selection", read_selection)
    monkeypatch.setattr(
        browser, "_radio_dropdown_option_locator", find_option
    )
    monkeypatch.setattr(
        browser, "_combobox_search_field", no_search_field
    )
    monkeypatch.setattr(
        browser, "_activate_combobox_search_action", no_search_action
    )
    monkeypatch.setattr(
        browser, "_scroll_filter_option_into_view", scroll_option
    )

    selected = asyncio.run(
        browser._select_radio_dropdown_option(
            Page(),
            object(),
            object(),
            {"name": "Service Team", "values": ["Benefits", "HR Support"]},
            "Benefits",
        )
    )

    assert selected == ["Benefits"]
    assert reopen_calls == ["closed", "opened"]


def test_radio_dropdown_does_not_click_payroll_when_already_selected(
    monkeypatch,
) -> None:
    selected = ["Payroll"]
    clicks = []

    class Page:
        async def wait_for_timeout(self, _milliseconds):
            await asyncio.sleep(0)

    async def popup_open(_combo):
        return True

    async def get_options(_frame, _combo):
        return object()

    async def read_selection(_page, _frame, _combo, _filter_name):
        return list(selected)

    monkeypatch.setattr(browser, "_slicer_dropdown_is_open", popup_open)
    monkeypatch.setattr(browser, "_options_for_combobox", get_options)
    monkeypatch.setattr(browser, "_radio_dropdown_selection", read_selection)

    result = asyncio.run(
        browser._select_radio_dropdown_option(
            Page(),
            object(),
            object(),
            {"name": "Service Team"},
            "Payroll",
        )
    )

    assert result == ["Payroll"]
    assert clicks == []


def test_radio_dropdown_retries_and_saves_diagnostics_when_selection_sticks(
    monkeypatch, tmp_path: Path
) -> None:
    clicks = []
    diagnostics = []

    class Options:
        pass

    class Option:
        async def is_visible(self):
            return True

        async def is_enabled(self):
            return True

        def locator(self, _selector):
            return EmptyLocator()

        async def click(self, **_kwargs):
            clicks.append("role=option")

        async def press(self, key, **_kwargs):
            raise AssertionError(
                f"Keyboard interaction {key!r} is invalid for a role=option"
            )

    class EmptyLocator:
        async def count(self):
            return 0

    class Page:
        async def wait_for_timeout(self, _milliseconds):
            await asyncio.sleep(0)

    options = Options()
    option = Option()
    current_time = iter([0.0, 1.0, 4.0, 5.0, 6.0, 9.0])
    monkeypatch.setattr(
        browser, "time", SimpleNamespace(monotonic=lambda: next(current_time))
    )

    async def popup_open(_combo):
        return True

    monkeypatch.setattr(browser, "_slicer_dropdown_is_open", popup_open)

    async def get_options(_frame, _combo):
        return options

    async def get_selected(_page, _frame, _combo, _filter_name):
        return ["HR Support Center"]

    async def find_option(_options, _value, _filter_name):
        return option

    async def scroll_option(
        _page, _frame, _combo, _value, _filter_name, _expected_options
    ):
        return option, {"visible": True}

    async def save_diagnostics(
        _page,
        _combo,
        _filter_name,
        _directory,
        **details,
    ):
        diagnostics.append(details)
        return tmp_path / "failure.png", tmp_path / "failure.json"

    async def wait_open(_page, _combo):
        return None

    monkeypatch.setattr(browser, "_options_for_combobox", get_options)
    monkeypatch.setattr(browser, "_radio_dropdown_selection", get_selected)
    monkeypatch.setattr(
        browser, "_radio_dropdown_option_locator", find_option
    )
    monkeypatch.setattr(
        browser, "_scroll_filter_option_into_view", scroll_option
    )
    monkeypatch.setattr(
        browser, "_save_filter_inventory_diagnostics", save_diagnostics
    )
    monkeypatch.setattr(browser, "_wait_for_slicer_open", wait_open)

    with pytest.raises(
        RuntimeError,
        match="selected values: \\['HR Support Center'\\]",
    ):
        asyncio.run(
            browser._select_radio_dropdown_option(
                Page(),
                object(),
                object(),
                {"name": "Service Team"},
                "Payroll",
                diagnostic_directory=tmp_path,
            )
        )

    assert clicks == ["role=option", "role=option"]
    assert diagnostics[0]["selection_history"] == [
        {
            "attempt": 1,
            "phase": "before_click",
            "selected_values": ["HR Support Center"],
            "inspection_error": None,
        },
        {
            "attempt": 1,
            "phase": "after_click",
            "selected_values": ["HR Support Center"],
            "inspection_error": None,
        },
        {
            "attempt": 2,
            "phase": "before_click",
            "selected_values": ["HR Support Center"],
            "inspection_error": None,
        },
        {
            "attempt": 2,
            "phase": "after_click",
            "selected_values": ["HR Support Center"],
            "inspection_error": None,
        },
    ]
    assert diagnostics[0]["requested_option"] == "Payroll"
    assert diagnostics[0]["selected_values"] == ["HR Support Center"]
    assert "two selection attempts" in diagnostics[0]["failure_detail"]


def test_radio_dropdown_retries_unavailable_state_and_reports_inspection_error(
    monkeypatch, tmp_path: Path
) -> None:
    diagnostics = []
    inspected = []
    clicks = []

    class Options:
        pass

    class Page:
        async def wait_for_timeout(self, _milliseconds):
            await asyncio.sleep(0)

    options = Options()
    current_time = iter([0.0, 4.0, 8.0, 12.0])
    monkeypatch.setattr(
        browser, "time", SimpleNamespace(monotonic=lambda: next(current_time))
    )

    async def popup_open(_combo):
        return True

    async def get_options(_frame, _combo):
        return options

    async def inspect_state(_page, _frame, _combo, filter_name):
        inspected.append(filter_name)
        raise browser.FilterSelectionInspectionError(
            "option states were unavailable"
        )

    async def find_option(_options, _value, _filter_name):
        return ClickTarget()

    class ClickTarget:
        async def is_visible(self):
            return True

        async def is_enabled(self):
            return True

        def locator(self, _selector):
            return EmptyLocator()

        async def click(self, **_kwargs):
            clicks.append("clicked")

    class EmptyLocator:
        async def count(self):
            return 0

    async def save_diagnostics(
        _page,
        _combo,
        _filter_name,
        _directory,
        **details,
    ):
        diagnostics.append(details)
        return tmp_path / "failure.png", tmp_path / "failure.json"

    monkeypatch.setattr(browser, "_slicer_dropdown_is_open", popup_open)
    monkeypatch.setattr(browser, "_options_for_combobox", get_options)
    monkeypatch.setattr(browser, "_radio_dropdown_selection", inspect_state)
    monkeypatch.setattr(
        browser, "_radio_dropdown_option_locator", find_option
    )
    monkeypatch.setattr(
        browser, "_save_filter_inventory_diagnostics", save_diagnostics
    )

    with pytest.raises(
        RuntimeError,
        match="selection state inspection failed: option states were unavailable",
    ) as error:
        asyncio.run(
            browser._select_radio_dropdown_option(
                Page(),
                object(),
                object(),
                {"name": "Service Team"},
                "Payroll",
                diagnostic_directory=tmp_path,
            )
        )

    assert inspected == ["Service Team", "Service Team"]
    assert clicks == []
    assert "selected values: None" not in str(error.value)
    assert diagnostics[0]["selection_state"] == "unavailable"
    assert "selected_values" not in diagnostics[0]
    assert diagnostics[0]["target_locator"] == (
        "associated popup option with exact label 'Payroll'"
    )


def test_filter_selection_rejects_combined_value_before_opening_popup() -> None:
    values = [
        "Benefits",
        "Compensation",
        "Compliance",
        "Create Job Requisition",
    ]
    filter_item = {
        "name": "Service Team",
        "selector_name": "Service Team Nm",
        "control_type": "radio_dropdown",
        "values": values,
    }
    combined_value = " ".join(values)

    with pytest.raises(
        RuntimeError,
        match="not one individual discovered option for filter 'Service Team'",
    ):
        asyncio.run(
            browser._select_filter(
                SimpleNamespace(frames=[]),
                browser.FilterSelection("Service Team", combined_value),
                [filter_item],
            )
        )


def test_click_radio_filter_option_clicks_exact_exposed_value(monkeypatch) -> None:
    selected = {"Option A"}
    clicks = []

    class Radio:
        def __init__(self, label):
            self.label = label

        async def click(self, **kwargs):
            assert kwargs == {"force": True, "timeout": 5_000}
            clicks.append(self.label)
            selected.clear()
            selected.add(self.label)

    class Radios:
        def nth(self, index):
            return Radio(("Option A", "Option B")[index])

    class Frame:
        def locator(self, selector):
            assert selector == '[role="radio"], input[type="radio"]'
            return Radios()

    async def radio_options(_frame, _filter):
        return [
            {
                "index": index,
                "label": value,
                "selected": value in selected,
            }
            for index, value in enumerate(("Option A", "Option B"))
        ]

    monkeypatch.setattr(browser, "_radio_filter_options", radio_options)

    asyncio.run(
        browser._click_radio_filter_option(
            Frame(),
            {"name": "Period", "control_type": "radio"},
            "Option B",
        )
    )

    assert clicks == ["Option B"]
    assert selected == {"Option B"}

    with pytest.raises(RuntimeError, match="not an available option"):
        asyncio.run(
            browser._click_radio_filter_option(
                Frame(),
                {"name": "Period", "control_type": "radio"},
                "Clear Selection",
            )
        )


def test_prepare_slicer_selections_clicks_clear_for_already_cleared_filter(
    monkeypatch,
) -> None:
    events = []
    filter_item = {
        "name": "Relative Time",
        "control_type": "checkbox_dropdown",
        "selected": ["Last 4 Weeks"],
        "values": ["All", "Last 4 Weeks", "Other Weeks"],
    }

    class ClearButton:
        async def click(self, **kwargs):
            assert kwargs == {"timeout": 5_000, "no_wait_after": True}
            events.append("clear")

    async def clear_button(_frame, _filter):
        return ClearButton()

    async def current_value(_page, _filter):
        return ["All"]

    class Page:
        frames = [object()]

        async def wait_for_timeout(self, milliseconds):
            assert milliseconds == 150

    monkeypatch.setattr(browser, "_clear_selection_button", clear_button)
    monkeypatch.setattr(browser, "_current_filter_value", current_value)

    asyncio.run(browser._prepare_slicer_selections(Page(), [filter_item]))

    assert events == ["clear"]
    assert filter_item["reset_strategy"] == "clear_selection"
    assert filter_item["skip_filter_cases"]


def test_prepare_slicer_selections_defaults_radio_filter_to_last_option(
    monkeypatch,
) -> None:
    events = []
    filter_item = {
        "name": "Period",
        "control_type": "radio_dropdown",
        "selected": ["Current"],
        "values": ["Current", "Previous"],
        "clear_selection_checked": False,
    }

    async def select_filter(
        _page, selection, _filters, diagnostic_directory=None
    ):
        events.append(selection.value)

    class Page:
        pass

    monkeypatch.setattr(browser, "_select_filter", select_filter)

    asyncio.run(browser._prepare_slicer_selections(Page(), [filter_item]))

    assert events == ["Previous"]
    assert filter_item["selected"] == ["Previous"]
    assert filter_item["reset_strategy"] == "preserve_selection"
    assert not filter_item.get("skip_filter_cases", False)


def test_prepare_slicer_selections_does_not_clear_single_select_default(
    monkeypatch,
) -> None:
    events = []
    filter_item = {
        "name": "Service Team",
        "control_type": "radio_dropdown",
        "selected": ["Benefits"],
        "values": ["Benefits", "HRIS"],
        "clear_selection_checked": False,
    }

    async def find_clear_button(_frame, _filter):
        events.append("check_clear")
        raise AssertionError("Single-select slicers should not be cleared")

    async def select_filter(_page, selection, _filters, **_kwargs):
        events.append(selection.value)

    class Page:
        pass

    monkeypatch.setattr(browser, "_clear_selection_button", find_clear_button)
    monkeypatch.setattr(browser, "_select_filter", select_filter)

    asyncio.run(browser._prepare_slicer_selections(Page(), [filter_item]))

    assert events == ["HRIS"]
    assert filter_item["selected"] == ["HRIS"]
    assert filter_item["reset_strategy"] == "preserve_selection"
    assert not filter_item.get("skip_filter_cases", False)


def test_prepare_slicer_selections_iterates_radio_options_without_clear(
    monkeypatch,
) -> None:
    events = []
    filter_item = {
        "name": "Period",
        "control_type": "radio_dropdown",
        "selected": ["Current"],
        "values": ["All", "Current", "Previous"],
        "clear_selection_checked": True,
    }

    async def no_clear_check(*_args):
        raise AssertionError("Clear Selections was already checked")

    async def no_refresh(*_args):
        return False

    async def select_filter(
        _page, selection, _filters, diagnostic_directory=None
    ):
        assert diagnostic_directory is None
        events.append(selection.value)
        filter_item["selected"] = [selection.value]

    async def current_value(_page, _filter):
        return filter_item["selected"]

    class Page:
        frames = [object()]

    monkeypatch.setattr(browser, "_clear_selection_button", no_clear_check)
    monkeypatch.setattr(browser, "_refresh_dropdown_filter_options", no_refresh)
    monkeypatch.setattr(browser, "_select_filter", select_filter)
    monkeypatch.setattr(browser, "_current_filter_value", current_value)

    asyncio.run(browser._prepare_slicer_selections(Page(), [filter_item]))

    assert events == ["Previous"]
    assert filter_item["selected"] == ["Previous"]
    assert filter_item["reset_strategy"] == "preserve_selection"


def test_single_select_does_not_attempt_aggregate_option(monkeypatch) -> None:
    filter_item = {
        "name": "Service Team",
        "control_type": "radio_dropdown",
        "selected": ["Benefits"],
        "values": ["All", "Benefits", "Payroll"],
    }

    async def current_value(_page, _filter):
        return ["Benefits"]

    class Page:
        frames = []

    monkeypatch.setattr(browser, "_current_filter_value", current_value)

    with pytest.raises(RuntimeError, match="Aggregate option 'All'"):
        asyncio.run(
            browser._select_filter(
                Page(),
                FilterSelection("Service Team", "All"),
                [filter_item],
            )
        )


def test_prepare_slicer_selections_does_not_repeat_initial_clear(
    monkeypatch,
) -> None:
    clicks = []
    filter_item = {
        "name": "Service Team",
        "control_type": "checkbox_dropdown",
        "selected": ["All"],
        "values": ["All", "Benefits", "HRIS"],
    }

    class ClearButton:
        async def click(self, **_kwargs):
            clicks.append("clear")

    async def find_clear_button(_frame, _filter):
        raise AssertionError("An initially cleared filter must be skipped")

    async def current_value(_page, _filter):
        raise AssertionError("An initially cleared filter must be skipped")

    class Page:
        frames = [object()]

    monkeypatch.setattr(browser, "_clear_selection_button", find_clear_button)
    monkeypatch.setattr(browser, "_current_filter_value", current_value)

    asyncio.run(
        browser._prepare_slicer_selections(
            Page(), [filter_item], already_cleared_slicers=["Service Team"]
        )
    )

    assert clicks == []
    assert filter_item["reset_strategy"] == "clear_selection"
    assert filter_item["skip_filter_cases"]


def test_prepare_slicer_selections_does_not_act_after_clear_click(
    monkeypatch,
) -> None:
    clicks = []
    filter_item = {
        "name": "Relative Time",
        "control_type": "checkbox_dropdown",
        "selected": ["Last 4 Weeks"],
        "values": ["All", "Last 4 Weeks", "Other Weeks"],
    }

    class ClearButton:
        async def click(self, **kwargs):
            assert kwargs == {"timeout": 5_000, "no_wait_after": True}
            clicks.append("clear")

    async def clear_button(_frame, _filter):
        return ClearButton()

    async def current_value(_page, _filter):
        return ["All"]

    class Page:
        frames = [object()]

        async def wait_for_timeout(self, _milliseconds):
            return None

    monkeypatch.setattr(browser, "_clear_selection_button", clear_button)
    monkeypatch.setattr(browser, "_current_filter_value", current_value)

    asyncio.run(browser._prepare_slicer_selections(Page(), [filter_item]))

    assert clicks == ["clear"]
    assert filter_item["selected"] == ["All"]
    assert filter_item["reset_strategy"] == "clear_selection"
    assert filter_item["skip_filter_cases"]


def test_checkbox_filter_without_clear_control_iterates_options(
    monkeypatch,
) -> None:
    actions = []
    filter_item = {
        "name": "Relative Time",
        "control_type": "checkbox_dropdown",
        "selected": ["Last 4 Weeks"],
        "values": ["All", "Last 4 Weeks", "Other Weeks"],
    }

    async def no_clear_button(_frame, _filter):
        return None

    async def no_refresh(_page, _filter):
        return False

    async def select_individual(_page, selection, _filters, **_kwargs):
        actions.append(selection.value)
        filter_item["selected"] = [selection.value]

    class Page:
        frames = [object()]

    monkeypatch.setattr(browser, "_clear_selection_button", no_clear_button)
    monkeypatch.setattr(browser, "_refresh_dropdown_filter_options", no_refresh)
    monkeypatch.setattr(browser, "_select_filter", select_individual)

    asyncio.run(browser._prepare_slicer_selections(Page(), [filter_item]))

    assert actions == ["Last 4 Weeks", "Other Weeks"]
    assert filter_item["values"] == ["Last 4 Weeks", "Other Weeks"]
    assert filter_item["selected"] == ["Other Weeks"]
    assert filter_item["reset_strategy"] == "preserve_selection"


def test_checkbox_filter_without_aggregate_or_clear_iterates_every_option(
    monkeypatch,
) -> None:
    actions = []
    filter_item = {
        "name": "Service Team",
        "control_type": "checkbox_dropdown",
        "selected": ["Benefits"],
        "values": ["Benefits", "HRIS"],
    }

    class Page:
        frames = [object()]

    async def no_clear_button(_frame, _filter):
        return None

    async def no_refresh(_page, _filter):
        return False

    async def select_individual(_page, selection, _filters, **_kwargs):
        actions.append(selection.value)
        filter_item["selected"] = [selection.value]

    monkeypatch.setattr(browser, "_clear_selection_button", no_clear_button)
    monkeypatch.setattr(browser, "_refresh_dropdown_filter_options", no_refresh)
    monkeypatch.setattr(browser, "_select_filter", select_individual)
    asyncio.run(browser._prepare_slicer_selections(Page(), [filter_item]))

    assert actions == ["Benefits", "HRIS"]
    assert filter_item["selected"] == ["HRIS"]
    assert filter_item["reset_strategy"] == "preserve_selection"


def test_clear_selection_button_is_supported_for_radio_dropdowns() -> None:
    class Button:
        async def is_visible(self):
            return True

        async def is_enabled(self):
            return True

    button = Button()

    class Buttons:
        async def count(self):
            return 1

        def nth(self, index):
            assert index == 0
            return button

    class Scope:
        async def count(self):
            return 1

        def get_by_role(self, role, name, exact):
            assert role == "button"
            assert name.fullmatch("Clear selections")
            assert exact is True
            return Buttons()

    class Control:
        async def hover(self, **_kwargs):
            return None

        def locator(self, _selector):
            return Scope()

    result = asyncio.run(
        browser._clear_selection_button(
            object(),
            {
                "name": "Service Team",
                "control_type": "radio_dropdown",
            },
            control=Control(),
        )
    )

    assert result is button


def test_clear_selection_button_is_scoped_to_slicer_group(monkeypatch) -> None:
    class Button:
        async def is_visible(self):
            return True

        async def is_enabled(self):
            return True

    class Buttons:
        async def count(self):
            return 1

        def nth(self, index):
            assert index == 0
            return Button()

    class Group:
        async def count(self):
            return 1

        def get_by_role(self, role, name=None, exact=None):
            assert role == "button"
            assert name.fullmatch("Clear selections")
            assert exact is True
            return Buttons()

    class Control:
        selectors = []

        async def hover(self, timeout):
            assert timeout == browser.SLICER_CONTROL_ACTION_TIMEOUT_MS

        def locator(self, selector):
            self.selectors.append(selector)
            return Group()

    class Frame:
        pass

    async def combobox_for_filter(_frame, _filter):
        return Control()

    monkeypatch.setattr(browser, "_combobox_for_filter", combobox_for_filter)

    result = asyncio.run(
        browser._clear_selection_button(
            Frame(),
            {"name": "Relative Time", "selector_name": "Relative Time"},
        )
    )

    assert result is not None
    assert Control.selectors[0].startswith(
        "xpath=ancestor::*[@data-visual-id or "
    )
    assert "visualContainer" in Control.selectors[0]


@pytest.mark.parametrize(
    ("initial_value", "click_changes_value"),
    [
        ("Last 4 Weeks", True),
        ("All", False),
        ("Last 4 Weeks", False),
    ],
)
def test_page_slicer_clear_click_happens_before_inventory_and_capture(
    monkeypatch,
    initial_value,
    click_changes_value,
) -> None:
    events = []

    class Button:
        async def click(self, **kwargs):
            assert kwargs == {"timeout": 5_000, "no_wait_after": True}
            if click_changes_value:
                combo.value = "All"
            events.append("clear")

    class Combo:
        value = initial_value
        open = False

        async def is_visible(self):
            return True

        async def get_attribute(self, name):
            assert name == "aria-expanded"
            return "true" if self.open else "false"

        async def click(self, **_kwargs):
            self.open = True

        async def inner_text(self):
            return self.value

        async def press(self, key):
            assert key == "Escape"
            self.open = False

    combo = Combo()

    class Combos:
        async def evaluate_all(self, script):
            assert "COMBOBOX" not in script
            return [
                {
                    "index": 0,
                    "name": "Relative Time",
                    "selectorName": "Last 4 Weeks",
                }
            ]

        def nth(self, index):
            assert index == 0
            return combo

    class Frame:
        def get_by_role(self, role):
            assert role == "combobox"
            return Combos()

    class Page:
        frames = [Frame()]

        async def wait_for_timeout(self, milliseconds):
            assert milliseconds == 150

    async def clear_button(_frame, filter_item, control=None):
        assert filter_item["name"] == "Relative Time"
        assert filter_item["control_type"] == "checkbox_dropdown"
        assert control is combo
        events.append("locate_clear")
        return Button()

    async def checkbox_options(_page, _frame, _combo, _filter_probe):
        raise AssertionError("The clear control must be checked before options")

    async def associated_options(_frame, _combo, _filter_probe):
        return (
            ["Last 4 Weeks", "Other Weeks"],
            [initial_value] if initial_value != "All" else [],
            "checkbox_dropdown",
        )

    monkeypatch.setattr(browser, "_clear_selection_button", clear_button)
    monkeypatch.setattr(
        browser, "_associated_listbox_options", associated_options
    )
    monkeypatch.setattr(
        browser, "_wait_for_combobox_options", checkbox_options
    )

    cleared = asyncio.run(browser._clear_page_slicer_selections(Page()))
    events.append("initial_screenshot_exports")

    assert cleared == ["Relative Time"]
    assert events == [
        "locate_clear",
        "clear",
        "initial_screenshot_exports",
    ]


def test_clear_page_slicer_selections_skips_single_select_dropdown(
    monkeypatch,
) -> None:
    events = []

    class Combo:
        async def is_visible(self):
            return True

        async def get_attribute(self, name):
            assert name == "aria-expanded"
            return "true"

    combo = Combo()

    class Combos:
        async def evaluate_all(self, _script):
            return [
                {
                    "index": 0,
                    "name": "Service Team",
                    "selectorName": "Service Team Nm",
                }
            ]

        def nth(self, _index):
            return combo

    class Frame:
        def get_by_role(self, role):
            assert role == "combobox"
            return Combos()

    class Page:
        frames = [Frame()]

    async def associated_options(_frame, control, _filter_probe):
        assert control is combo
        events.append("inspect_mode")
        return (
            ["HR Support Center", "Payroll"],
            ["HR Support Center"],
            "radio_dropdown",
        )

    async def no_clear_action(*_args, **_kwargs):
        return None

    async def close_dropdown(_page, control):
        assert control is combo
        events.append("close")

    monkeypatch.setattr(
        browser, "_associated_listbox_options", associated_options
    )
    monkeypatch.setattr(
        browser, "_clear_selection_button", no_clear_action
    )
    monkeypatch.setattr(browser, "_close_slicer_dropdown", close_dropdown)

    cleared = asyncio.run(browser._clear_page_slicer_selections(Page()))

    assert cleared == []
    assert events == ["inspect_mode", "close"]


def test_clear_page_slicer_selections_uses_single_select_clear_button(
    monkeypatch,
) -> None:
    events = []

    class Combo:
        async def is_visible(self):
            return True

        async def get_attribute(self, name):
            assert name == "aria-expanded"
            return "true"

    combo = Combo()

    class Combos:
        async def evaluate_all(self, _script):
            return [
                {
                    "index": 0,
                    "name": "Relative Time",
                    "selectorName": "Relative Time",
                }
            ]

        def nth(self, _index):
            return combo

    class Frame:
        def get_by_role(self, role):
            assert role == "combobox"
            return Combos()

    class Page:
        frames = [Frame()]

        async def wait_for_timeout(self, delay):
            assert delay == 150
            events.append("wait")

    class ClearButton:
        async def click(self, **kwargs):
            assert kwargs == {"timeout": 5_000, "no_wait_after": True}
            events.append("clear")

    async def associated_options(_frame, control, _filter_probe):
        assert control is combo
        return (["Last 4 Weeks", "Last 12 Weeks"], ["Last 4 Weeks"], "radio_dropdown")

    async def find_clear_button(_frame, filter_item, *, control):
        assert filter_item["name"] == "Relative Time"
        assert filter_item["control_type"] == "radio_dropdown"
        assert control is combo
        events.append("find_clear")
        return ClearButton()

    async def current_value(_page, filter_item):
        assert filter_item["name"] == "Relative Time"
        events.append("verify_clear")
        return []

    async def unexpected_wait_options(*_args, **_kwargs):
        raise AssertionError("Do not enumerate options after clearing")

    monkeypatch.setattr(
        browser, "_associated_listbox_options", associated_options
    )
    monkeypatch.setattr(
        browser, "_clear_selection_button", find_clear_button
    )
    monkeypatch.setattr(browser, "_current_filter_value", current_value)
    monkeypatch.setattr(
        browser, "_wait_for_combobox_options", unexpected_wait_options
    )

    cleared = asyncio.run(browser._clear_page_slicer_selections(Page()))

    assert cleared == ["Relative Time"]
    assert events == ["find_clear", "clear", "wait", "verify_clear"]


def test_page_start_clears_and_verifies_all_inventory_slicers(monkeypatch) -> None:
    events = []
    filters = [
        {
            "name": "Service Team",
            "control_type": "radio_dropdown",
            "selected": ["Recruiting"],
            "values": ["Payroll", "Recruiting"],
        },
        {
            "name": "Relative Time",
            "control_type": "checkbox_dropdown",
            "selected": ["Last 4 Weeks"],
            "values": ["All", "Last 4 Weeks"],
        },
    ]

    async def inventory(_page, diagnostic_directory=None, *, clear_selection_actions):
        assert clear_selection_actions is False
        events.append("inventory")
        return {"filters": filters, "initial_cleared_slicers": []}

    async def clear_slicers(_page, names=None, *, require_all):
        assert require_all is False
        if names is None:
            events.append("clear_visible")
            return []
        assert names == ["Relative Time"]
        events.append("clear_remaining")
        return names

    async def wait_for_render(_page, settle_seconds):
        assert settle_seconds == 2.0
        events.append("render")

    async def current_value(_page, filter_item):
        events.append(f"verify:{filter_item['name']}")
        return (
            ["Recruiting"]
            if filter_item["name"] == "Service Team"
            else []
        )

    class Page:
        pass

    monkeypatch.setattr(browser, "_accessible_inventory", inventory)
    monkeypatch.setattr(
        browser, "_clear_page_slicer_selections", clear_slicers
    )
    monkeypatch.setattr(browser, "_wait_for_state_render", wait_for_render)
    monkeypatch.setattr(browser, "_current_filter_value", current_value)

    result = asyncio.run(
        browser._inventory_page_after_clearing_slicers(Page())
    )

    assert result["initial_cleared_slicers"] == ["Relative Time"]
    assert [item["selected"] for item in filters] == [
        ["Recruiting"],
        [],
    ]
    assert events == [
        "clear_visible",
        "render",
        "inventory",
        "clear_remaining",
        "render",
        "verify:Service Team",
        "verify:Relative Time",
    ]


def test_page_start_preserves_multiselect_when_no_clear_action_exists(
    monkeypatch,
) -> None:
    filter_item = {
        "name": "Relative Time",
        "control_type": "checkbox_dropdown",
        "selected": ["Last 4 Weeks"],
        "values": ["Last 4 Weeks", "Other Weeks"],
    }

    async def inventory(_page, diagnostic_directory=None, *, clear_selection_actions):
        return {"filters": [filter_item], "initial_cleared_slicers": []}

    async def no_clear_action(_page, _names=None, *, require_all):
        return []

    async def current_value(_page, _filter):
        return ["Last 4 Weeks"]

    async def wait_for_render(_page, settle_seconds):
        assert settle_seconds == 2.0

    class Page:
        pass

    monkeypatch.setattr(browser, "_accessible_inventory", inventory)
    monkeypatch.setattr(
        browser, "_clear_page_slicer_selections", no_clear_action
    )
    monkeypatch.setattr(browser, "_wait_for_state_render", wait_for_render)
    monkeypatch.setattr(browser, "_current_filter_value", current_value)

    result = asyncio.run(
        browser._inventory_page_after_clearing_slicers(Page())
    )

    assert result["initial_cleared_slicers"] == []
    assert filter_item["selected"] == ["Last 4 Weeks"]


def test_page_start_defaults_single_select_to_last_option(
    monkeypatch,
) -> None:
    filter_item = {
        "name": "Service Team",
        "control_type": "radio_dropdown",
        "selected": [],
        "values": ["HR Support Center", "Payroll", "Benefits"],
    }
    events = []

    async def inventory(_page, diagnostic_directory=None, *, clear_selection_actions):
        return {"filters": [filter_item], "initial_cleared_slicers": []}

    async def clear_slicers(_page, names=None, *, require_all):
        if names is None:
            return ["Service Team"]
        return []

    async def current_value(_page, _filter):
        return []

    async def refresh_dropdown_options(_page, _filter):
        return True

    async def select_filter(_page, selection, _filters, **_kwargs):
        events.append(selection.value)

    async def wait_for_render(_page, settle_seconds):
        assert settle_seconds == 2.0

    class Page:
        pass

    monkeypatch.setattr(browser, "_accessible_inventory", inventory)
    monkeypatch.setattr(
        browser, "_clear_page_slicer_selections", clear_slicers
    )
    monkeypatch.setattr(browser, "_wait_for_state_render", wait_for_render)
    monkeypatch.setattr(browser, "_current_filter_value", current_value)
    monkeypatch.setattr(
        browser, "_refresh_dropdown_filter_options", refresh_dropdown_options
    )
    monkeypatch.setattr(browser, "_select_filter", select_filter)

    result = asyncio.run(
        browser._inventory_page_after_clearing_slicers(Page())
    )

    assert result["initial_cleared_slicers"] == []
    assert filter_item["selected"] == ["Benefits"]
    assert events == ["Benefits"]


def test_dashboard_initial_state_is_captured_after_slicer_clear(
    monkeypatch, tmp_path
) -> None:
    events = []

    class Page:
        async def goto(self, *_args, **_kwargs):
            events.append("navigate")

    class Context:
        async def new_page(self):
            return Page()

        async def close(self):
            events.append("close")

    class Browser:
        async def new_context(self, **_kwargs):
            return Context()

    async def wait_for_ready(_page):
        events.append("ready")

    async def wait_for_render(_page, settle_seconds):
        assert settle_seconds == 2.0
        events.append("render")

    async def find_page_tab(_page, _page_name):
        events.append("select_page")

    async def reset_filters(_page):
        events.append("reset_report")

    async def inventory_after_clear(_page, diagnostic_directory=None):
        events.append("inventory")
        events.append("clear_slicers")
        events.append("render")
        return {
            "pages": ["Overview"],
            "filters": [
                {
                    "name": "Department",
                    "selected": ["Finance"],
                    "values": ["Finance", "HR"],
                    "clear_selection_checked": True,
                }
            ],
            "initial_cleared_slicers": ["Relative Time"],
        }

    async def prepare_slicers(
        _page,
        filters,
        already_cleared_slicers=None,
        diagnostic_directory=None,
    ):
        events.append("prepare_slicers")
        assert [item["name"] for item in filters] == ["Department"]
        assert already_cleared_slicers == ["Relative Time"]
        assert diagnostic_directory == tmp_path

    async def plan_from_dom(_inventory):
        return {}, {"filters": []}

    async def capture_state(*_args, **kwargs):
        events.append("initial_screenshot_exports")
        assert kwargs["filter_inventory"][0]["name"] == "Department"
        return SimpleNamespace(
            dashboard="dashboard_A",
            page="Overview",
            filter=None,
            screenshot=tmp_path / "Overview" / "initial.png",
            extracted={},
        )

    monkeypatch.setattr(browser, "_wait_for_report_ready", wait_for_ready)
    monkeypatch.setattr(browser, "_wait_for_state_render", wait_for_render)
    monkeypatch.setattr(browser, "_find_page_tab", find_page_tab)
    monkeypatch.setattr(browser, "_reset_report_filters", reset_filters)
    monkeypatch.setattr(
        browser,
        "_inventory_page_after_clearing_slicers",
        inventory_after_clear,
    )
    monkeypatch.setattr(
        browser, "_prepare_slicer_selections", prepare_slicers
    )
    monkeypatch.setattr(browser, "plan_from_dom", plan_from_dom)
    monkeypatch.setattr(browser, "_capture_state", capture_state)
    monkeypatch.setattr(
        browser, "_append_capture_performance_record", lambda *_args: None
    )

    result = asyncio.run(
        browser._capture_dashboard_page(
            Browser(),
            {},
            "https://app.powerbi.com/report",
            "dashboard_A",
            "Overview",
            tmp_path,
            0,
            None,
            asyncio.Semaphore(1),
            asyncio.Semaphore(1),
            {},
        )
    )

    assert result["page_inventory"]["initial_cleared_slicers"] == [
        "Relative Time"
    ]
    assert events.index("inventory") < events.index("clear_slicers")
    assert events.index("clear_slicers") < events.index("render")
    assert events.index("inventory") < events.index("initial_screenshot_exports")


def test_initial_capture_is_skipped_when_default_is_in_filter_plan() -> None:
    inventory = [
        {
            "name": "Bookmark buttons: E-Mail | Phone Call | Workday Cases",
            "control_type": "bookmark_buttons",
            "selected": ["Workday Cases"],
            "values": ["E-Mail", "Phone Call", "Workday Cases"],
        }
    ]
    plan = {
        "filters": [
            {
                "name": "Bookmark buttons: E-Mail | Phone Call | Workday Cases",
                "values": ["E-Mail", "Phone Call", "Workday Cases"],
            }
        ]
    }

    assert browser._initial_state_covered_by_filter_plan(inventory, plan) == {
        "name": "Bookmark buttons: E-Mail | Phone Call | Workday Cases",
        "value": "Workday Cases",
    }


def test_initial_capture_is_kept_when_clear_state_is_not_in_filter_plan() -> None:
    inventory = [
        {
            "name": "Relative Time",
            "control_type": "checkbox_dropdown",
            "selected": ["All"],
            "values": ["All", "Last 4 Weeks", "Other Weeks"],
        }
    ]
    plan = {
        "filters": [
            {
                "name": "Relative Time",
                "values": ["Last 4 Weeks", "Other Weeks"],
            }
        ]
    }

    assert browser._initial_state_covered_by_filter_plan(inventory, plan) is None


def test_initial_capture_is_covered_only_by_a_complete_filter_combination() -> None:
    inventory = [
        {"name": "Filter 1", "selected": ["2"], "values": ["1", "2"]},
        {"name": "Filter 2", "selected": ["b"], "values": ["a", "b"]},
    ]
    plan = {
        "filters": [
            {"name": "Filter 1", "values": ["1", "2"]},
            {"name": "Filter 2", "values": ["a", "b"]},
        ]
    }

    assert browser._initial_state_covered_by_filter_plan(inventory, plan) == {
        "selections": [
            {"name": "Filter 1", "value": "2"},
            {"name": "Filter 2", "value": "b"},
        ]
    }

    inventory[1]["selected"] = []
    assert browser._initial_state_covered_by_filter_plan(inventory, plan) is None


def test_prepare_slicer_selections_iterates_checkbox_values_individually(
    monkeypatch,
) -> None:
    selected_options = []
    filter_item = {
        "name": "Case Type",
        "selected": ["Phone Call"],
        "values": ["All", "Phone Call", "E-Mail"],
    }

    async def no_clear_button(_frame, _filter):
        return None

    async def select_individual(
        _page, selection, filters, diagnostic_directory=None
    ):
        item = filters[0]
        assert diagnostic_directory is None
        selected_options.append(selection.value)
        item["selected"] = [selection.value]

    class Page:
        frames = [object()]

    monkeypatch.setattr(browser, "_clear_selection_button", no_clear_button)
    monkeypatch.setattr(browser, "_select_filter", select_individual)

    asyncio.run(browser._prepare_slicer_selections(Page(), [filter_item]))

    assert selected_options == ["Phone Call", "E-Mail"]
    assert filter_item["values"] == ["Phone Call", "E-Mail"]
    assert filter_item["selected"] == ["E-Mail"]
    assert filter_item["reset_strategy"] == "preserve_selection"

    no_aggregate = {
        "name": "Relative Time",
        "control_type": "checkbox_dropdown",
        "selected": ["Last 4 Weeks"],
        "values": ["Last 4 Weeks", "Other Weeks"],
    }
    asyncio.run(browser._prepare_slicer_selections(Page(), [no_aggregate]))

    assert selected_options == [
        "Phone Call",
        "E-Mail",
        "Last 4 Weeks",
        "Other Weeks",
    ]
    assert no_aggregate["selected"] == ["Other Weeks"]
    assert no_aggregate["reset_strategy"] == "preserve_selection"


def test_prepare_slicer_selections_selects_each_option_when_current_state_is_unknown(
    monkeypatch, tmp_path: Path
) -> None:
    selected_options = []
    diagnostic_directories = []
    filter_item = {
        "name": "Service Team",
        "selected": [],
        "values": ["Benefits", "Compensation"],
    }

    async def no_clear_button(_frame, _filter):
        return None

    async def select_filter(
        _page, selection, filters, diagnostic_directory=None
    ):
        assert filters == [filter_item]
        diagnostic_directories.append(diagnostic_directory)
        selected_options.append(selection.value)
        filter_item["selected"] = [selection.value]

    async def current_value(*_args):
        raise AssertionError(
            "The no-clear fallback must select discovered options first"
        )

    class Page:
        frames = [object()]

    monkeypatch.setattr(browser, "_clear_selection_button", no_clear_button)
    monkeypatch.setattr(browser, "_select_filter", select_filter)
    monkeypatch.setattr(browser, "_current_filter_value", current_value)

    asyncio.run(
        browser._prepare_slicer_selections(
            Page(), [filter_item], diagnostic_directory=tmp_path
        )
    )

    assert selected_options == ["Benefits", "Compensation"]
    assert diagnostic_directories == [tmp_path, tmp_path]
    assert filter_item["selected"] == ["Compensation"]
    assert filter_item["reset_strategy"] == "preserve_selection"


def test_prepare_slicer_selections_reacquires_individual_dropdown_values(
    monkeypatch,
) -> None:
    values = [
        "Benefits",
        "Compensation",
        "Compliance",
        "Create Job Requisition",
        "HR Business Partners",
        "HR Support Center",
        "HR Systems",
        "HRIS",
        "Learning",
        "Legal Service Team",
        "Payroll",
    ]
    selected = []
    filter_item = {
        "name": "Service Team",
        "selector_name": "Service Team Nm",
        "selected": ["Payroll"],
        "values": [" ".join(values)],
        "clear_selection_checked": True,
    }

    class Frame:
        def get_by_role(self, _role):
            return None

    class Combo:
        async def get_attribute(self, name):
            if name == "aria-expanded":
                return "true"
            assert name in {"aria-controls", "aria-owns"}
            return None

    class Page:
        frames = [Frame()]

        async def wait_for_timeout(self, _milliseconds):
            return None

    async def find_combo(_frame, _filter):
        return Combo()

    async def no_search_field(_frame, _combo):
        return None

    async def read_popup(_page, _frame, _combo, _filter):
        return object(), values, ["Payroll"], [], []

    async def close_popup(_page, _combo):
        return None

    async def select_option(
        _page, selection, _filters, diagnostic_directory=None
    ):
        assert diagnostic_directory is None
        selected.append(selection.value)

    monkeypatch.setattr(browser, "_combobox_for_filter", find_combo)
    monkeypatch.setattr(browser, "_combobox_search_field", no_search_field)
    monkeypatch.setattr(browser, "_wait_for_combobox_options", read_popup)
    monkeypatch.setattr(browser, "_close_slicer_dropdown", close_popup)
    monkeypatch.setattr(browser, "_select_filter", select_option)

    asyncio.run(
        browser._prepare_slicer_selections(Page(), [filter_item])
    )

    assert filter_item["values"] == values
    assert selected == values


def test_move_pointer_off_dashboard_uses_off_viewport_position() -> None:
    class Mouse:
        position = None

        async def move(self, x, y):
            self.position = (x, y)

    class Page:
        def __init__(self):
            self.mouse = Mouse()

        async def wait_for_timeout(self, _ms):
            pass

    page = Page()

    asyncio.run(browser._move_pointer_off_dashboard(page))

    assert page.mouse.position == (-10, -10)


def test_identical_images_on_different_pages_are_extracted_per_page(monkeypatch) -> None:
    calls = []

    async def extract(screenshot_bytes: bytes, page_name: str) -> dict:
        calls.append(page_name)
        return {"page": page_name, "elements": []}

    monkeypatch.setattr(browser, "extract_screenshot", extract)

    async def run():
        cache = {}
        await browser._extract_or_reuse(b"same screenshot", "Page A", cache)
        await browser._extract_or_reuse(b"same screenshot", "Page B", cache)

    asyncio.run(run())
    assert calls == ["Page A", "Page B"]


def test_duplicate_report_urls_are_captured_independently(monkeypatch, tmp_path) -> None:
    capture_calls = []
    filter_plan_calls = []

    async def prepare_filter_plans(**_kwargs):
        filter_plan_calls.append(True)
        return {"Overview": {"filters": []}}

    async def capture_dashboard(**kwargs):
        capture_calls.append(kwargs)
        screenshot = kwargs["destination"] / "Overview" / "initial.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        screenshot.write_bytes(kwargs["dashboard"].encode())
        state = DashboardState(
            dashboard=kwargs["dashboard"],
            page="Overview",
            filter=None,
            screenshot=screenshot,
            extracted={"page": "Overview", "elements": []},
            filter_names=["Fiscal Year"],
            filter_values={"Fiscal Year": ["2026"]},
        )
        return [state]

    monkeypatch.setattr(browser, "prepare_filter_plans", prepare_filter_plans)
    monkeypatch.setattr(browser, "capture_dashboard", capture_dashboard)

    states_a, states_b = asyncio.run(
        browser.capture_dashboard_pair(
            url_a="https://app.powerbi.com/report?id=1&experience=power-bi",
            url_b="https://app.powerbi.com/report?id=1&experience=power-bi",
            destination=tmp_path,
            storage_state=None,
            headed=False,
            settle_seconds=0,
        )
    )

    assert len(capture_calls) == 2
    assert states_a[0].dashboard == "dashboard_A"
    assert states_b[0].dashboard == "dashboard_B"
    assert states_a[0].screenshot != states_b[0].screenshot
    assert states_a[0].screenshot.read_bytes() == b"dashboard_A"
    assert states_b[0].screenshot.read_bytes() == b"dashboard_B"
    assert filter_plan_calls == []
    assert all(not call["include_filter_cases"] for call in capture_calls)


def test_different_report_urls_are_captured_concurrently(monkeypatch, tmp_path) -> None:
    active_captures = 0
    maximum_concurrent_captures = 0
    both_started = asyncio.Event()
    capture_calls = []
    filter_plan_calls = []

    async def prepare_filter_plans(**_kwargs):
        filter_plan_calls.append(True)
        return {"Overview": {"filters": []}}

    async def capture_dashboard(**kwargs):
        nonlocal active_captures, maximum_concurrent_captures
        capture_calls.append(kwargs)
        active_captures += 1
        maximum_concurrent_captures = max(
            maximum_concurrent_captures, active_captures
        )
        if active_captures == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        active_captures -= 1
        return [kwargs["dashboard"]]

    monkeypatch.setattr(browser, "prepare_filter_plans", prepare_filter_plans)
    monkeypatch.setattr(browser, "capture_dashboard", capture_dashboard)

    states_a, states_b = asyncio.run(
        browser.capture_dashboard_pair(
            url_a="https://app.powerbi.com/report-a",
            url_b="https://app.powerbi.com/report-b",
            destination=tmp_path,
            storage_state=None,
            headed=False,
            settle_seconds=0,
            include_filter_cases=True,
        )
    )

    assert maximum_concurrent_captures == 2
    assert states_a == ["dashboard_A"]
    assert states_b == ["dashboard_B"]
    assert filter_plan_calls == [True]
    assert all(call["include_filter_cases"] for call in capture_calls)


def test_filter_case_checks_reset_after_opening_target_page(monkeypatch, tmp_path) -> None:
    events = []
    next_context_id = 0

    class FakePage:
        def __init__(self, context_id):
            self.context_id = context_id
            self.current_page = None
            self.frames = []

        async def goto(self, *args, **kwargs):
            events.append(("goto", self.context_id, args[0]))
            self.current_page = None

        async def wait_for_timeout(self, *args, **kwargs):
            pass

    class FakeContext:
        def __init__(self, context_id):
            self.page = FakePage(context_id)

        async def new_page(self):
            return self.page

        async def close(self):
            pass

    class FakeBrowser:
        async def new_context(self, **kwargs):
            nonlocal next_context_id
            context = FakeContext(next_context_id)
            next_context_id += 1
            return context

        async def close(self):
            pass

    async def launch(**kwargs):
        return FakeBrowser()

    class FakePlaywright:
        chromium = SimpleNamespace(launch=launch)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    async def wait_for_report_ready(_page):
        pass

    async def reset_report_filters(_page, required=False, baseline_filters=None):
        if _page.current_page == "Details":
            assert ("tab", _page.context_id, "Details") in events
        events.append(("reset", _page.context_id, required))
        return True

    async def filters_match_baseline(_page, _filters):
        return True

    async def find_page_tab(_page, page_name):
        _page.current_page = page_name
        events.append(("tab", _page.context_id, page_name))

    async def accessible_inventory(_page, diagnostic_directory=None):
        pages = ["Home", "View", "Overview", "Details"]
        filters = (
            []
            if _page.current_page != "Details"
            else [
                {
                    "name": "Year",
                    "selector_name": "Year",
                    "selected": ["2025"],
                    "values": ["2025", "2026"],
                }
            ]
        )
        return {"pages": pages, "filters": filters}

    async def inventory_after_clear(_page, diagnostic_directory=None):
        inventory = await accessible_inventory(_page, diagnostic_directory)
        inventory["initial_cleared_slicers"] = []
        return inventory

    async def plan_from_dom(inventory):
        plan = (
            {"filters": [{"name": "Year", "values": ["2025"]}]}
            if inventory["filters"]
            else {"filters": []}
        )
        return {}, plan

    captured_pages = []
    captured_contexts = []
    active_captures = 0
    maximum_concurrent_captures = 0

    async def capture_state(*args, **kwargs):
        nonlocal active_captures, maximum_concurrent_captures
        page_name = args[2]
        captured_pages.append(page_name)
        captured_contexts.append(args[0].context_id)
        active_captures += 1
        maximum_concurrent_captures = max(
            maximum_concurrent_captures, active_captures
        )
        await asyncio.sleep(0.01)
        active_captures -= 1
        return DashboardState(
            dashboard=args[1],
            page=page_name,
            filter=args[3],
            screenshot=tmp_path / f"{page_name}.png",
            extracted={
                "capture_performance_seconds": {"total": 0.01},
                "visual_export_timings": [],
            },
        )

    async def select_filter(*args, **kwargs):
        pass

    async def verify_filter(_page, _filter, value):
        return [value]

    async def prepare_slicer_selections(
        _page,
        _filters,
        already_cleared_slicers=None,
        diagnostic_directory=None,
    ):
        pass

    monkeypatch.setattr(browser, "async_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(browser, "_wait_for_report_ready", wait_for_report_ready)
    monkeypatch.setattr(browser, "_reset_report_filters", reset_report_filters)
    monkeypatch.setattr(browser, "_filters_match_baseline", filters_match_baseline)
    monkeypatch.setattr(browser, "_find_page_tab", find_page_tab)
    monkeypatch.setattr(browser, "_accessible_inventory", accessible_inventory)
    monkeypatch.setattr(
        browser,
        "_inventory_page_after_clearing_slicers",
        inventory_after_clear,
    )
    monkeypatch.setattr(browser, "plan_from_dom", plan_from_dom)
    monkeypatch.setattr(browser, "_capture_state", capture_state)
    monkeypatch.setattr(browser, "_select_filter", select_filter)
    monkeypatch.setattr(
        browser, "_wait_for_filter_selection", verify_filter
    )
    monkeypatch.setattr(
        browser, "_prepare_slicer_selections", prepare_slicer_selections
    )

    states = asyncio.run(
        browser.capture_dashboard(
            url="https://app.powerbi.com/report",
            dashboard="A",
            destination=tmp_path,
            storage_state=None,
            headed=False,
            settle_seconds=0,
        )
    )

    assert len(states) == 2
    assert sum(event[0] == "goto" for event in events) == 4
    assert [state.page for state in states] == [
        "Overview",
        "Details",
    ]
    assert sorted(captured_pages) == ["Details", "Overview"]
    assert len(set(captured_contexts)) == 2
    assert not {"Home", "View"} & set(captured_pages)
    dom = json.loads((tmp_path / "dom.json").read_text(encoding="utf-8"))
    assert dom["page_inventories"]["Details"]["initial_state_capture"] == {
        "status": "skipped_duplicate",
        "covered_by": {"name": "Year", "value": "2025"},
    }
    performance_records = (
        tmp_path / "capture_performance.jsonl"
    ).read_text(encoding="utf-8").splitlines()
    assert len(performance_records) == len(states)
    assert all(
        '"capture_performance_seconds"' in record
        for record in performance_records
    )


def test_find_page_tab_waits_for_delayed_tab_after_reload(monkeypatch) -> None:
    class FakeTab:
        def __init__(self, page):
            self.page = page

        async def is_visible(self):
            return self.page.polls >= 2

        async def click(self):
            self.page.clicked = True

    class FakeTabs:
        def __init__(self, page):
            self.page = page

        async def count(self):
            return 1 if self.page.polls >= 2 else 0

        def nth(self, _index):
            return FakeTab(self.page)

    class FakeFrame:
        def __init__(self, page):
            self.page = page

        def get_by_role(self, *_args, **_kwargs):
            return FakeTabs(self.page)

    class FakePage:
        polls = 0
        clicked = False

        @property
        def frames(self):
            return [FakeFrame(self)]

        async def wait_for_timeout(self, _milliseconds):
            self.polls += 1

    async def report_ready(_page):
        pass

    page = FakePage()
    monkeypatch.setattr(browser, "_wait_for_report_ready", report_ready)

    asyncio.run(browser._find_page_tab(page, "Performance"))

    assert browser.MAX_PAGE_TAB_WAIT_SECONDS == 60.0
    assert page.polls == 2
    assert page.clicked


def test_find_page_tab_rejects_power_bi_file_command() -> None:
    class Page:
        frames = []

    with pytest.raises(ValueError, match="application tab, not a report page"):
        asyncio.run(browser._find_page_tab(Page(), "File"))


def test_accessible_tab_names_uses_one_dom_snapshot() -> None:
    class Tabs:
        async def evaluate_all(self, script):
            assert "elements.slice(0, 100)" in script
            return [
                "Overview\nSelected",
                "Performance",
                "Performance",
                "",
            ]

    class Frame:
        def locator(self, selector):
            assert selector == '[role="tab"]'
            return Tabs()

    assert asyncio.run(browser._accessible_tab_names(Frame())) == [
        "Overview",
        "Performance",
    ]


def test_filter_cases_run_in_isolated_contexts_with_bounded_concurrency(
    monkeypatch, tmp_path
) -> None:
    active_captures = 0
    maximum_active_captures = 0
    next_context_id = 0
    inventory_calls = 0
    reset_calls = 0
    clear_calls = []
    contexts = []
    filter_values = [
        "Last 4 Weeks",
        "Last 13 Weeks",
        "Last 26 Weeks",
        "Last 52 Weeks",
        "Last 104 Weeks",
        "Last 156 Weeks",
        "Last 208 Weeks",
        "Last 260 Weeks",
    ]

    class FakePage:
        def __init__(self, context_id):
            self.context_id = context_id
            self.selected = None

        async def goto(self, *_args, **_kwargs):
            pass

    class FakeContext:
        def __init__(self, context_id):
            self.page = FakePage(context_id)
            self.closed = False

        async def new_page(self):
            return self.page

        async def close(self):
            self.closed = True

    class FakeBrowser:
        async def new_context(self, **_kwargs):
            nonlocal next_context_id
            context = FakeContext(next_context_id)
            next_context_id += 1
            contexts.append(context)
            return context

    async def wait_for_report_ready(_page):
        pass

    async def find_page_tab(_page, _page_name):
        pass

    async def wait_for_state_render(_page, settle_seconds):
        assert settle_seconds == 2.0

    async def clear_slicers(_page, names=None, *, require_all=True):
        clear_calls.append((names, require_all))
        return []

    async def reset_report_filters(_page, **_kwargs):
        nonlocal reset_calls
        reset_calls += 1
        return True

    async def accessible_inventory(_page, diagnostic_directory=None):
        nonlocal inventory_calls
        inventory_calls += 1
        return {
            "filters": [
                {
                    "name": "Relative Time",
                    "selector_name": "Relative Time",
                    "selected": ["Last 4 Weeks"],
                    "values": filter_values,
                }
            ]
        }

    async def inventory_after_clear(_page, diagnostic_directory=None):
        inventory = await accessible_inventory(_page, diagnostic_directory)
        inventory["initial_cleared_slicers"] = []
        return inventory

    async def filters_match_baseline(_page, _filters):
        return True

    async def select_filter(
        page, selection, _filters, diagnostic_directory=None
    ):
        page.selected = selection.value

    async def verify_filter(_page, _filter, value):
        return [value]

    async def capture_state(page, _dashboard, _page_name, selection, *_args, **_kwargs):
        nonlocal active_captures, maximum_active_captures
        active_captures += 1
        maximum_active_captures = max(maximum_active_captures, active_captures)
        await asyncio.sleep(0.01)
        active_captures -= 1
        return SimpleNamespace(
            filter_value=selection.value,
            page_id=page.context_id,
            selected_value=page.selected,
        )

    monkeypatch.setattr(browser, "_wait_for_report_ready", wait_for_report_ready)
    monkeypatch.setattr(browser, "_find_page_tab", find_page_tab)
    monkeypatch.setattr(
        browser, "_wait_for_state_render", wait_for_state_render
    )
    monkeypatch.setattr(
        browser, "_clear_page_slicer_selections", clear_slicers
    )
    monkeypatch.setattr(browser, "_reset_report_filters", reset_report_filters)
    monkeypatch.setattr(browser, "_accessible_inventory", accessible_inventory)
    monkeypatch.setattr(
        browser,
        "_inventory_page_after_clearing_slicers",
        inventory_after_clear,
    )
    monkeypatch.setattr(browser, "_filters_match_baseline", filters_match_baseline)
    monkeypatch.setattr(browser, "_select_filter", select_filter)
    monkeypatch.setattr(
        browser, "_wait_for_filter_selection", verify_filter
    )
    monkeypatch.setattr(browser, "_capture_state", capture_state)

    async def run():
        semaphore = asyncio.Semaphore(browser.MAX_PARALLEL_STATE_CAPTURES)
        return await asyncio.gather(
            *(
                browser._capture_filter_case(
                    FakeBrowser(),
                    {},
                    "https://app.powerbi.com/report",
                    "dashboard_A",
                    "Overview",
                    FilterSelection("Relative Time", value),
                    [
                        {
                            "name": "Relative Time",
                            "selector_name": "Relative Time",
                            "selected": ["Last 4 Weeks"],
                            "values": filter_values,
                        }
                    ],
                    tmp_path,
                    0,
                    {},
                    semaphore,
                )
                for value in filter_values
            )
        )

    states = asyncio.run(run())

    assert maximum_active_captures == min(
        len(filter_values), browser.MAX_PARALLEL_STATE_CAPTURES
    )
    assert [state.filter_value for state in states] == filter_values
    assert [state.selected_value for state in states] == filter_values
    assert len({state.page_id for state in states}) == len(filter_values)
    assert len(contexts) == len(filter_values)
    assert all(context.closed for context in contexts)
    assert inventory_calls == 0
    assert reset_calls == 0
    assert clear_calls == [
        (["Relative Time"], False) for _ in filter_values
    ]


def test_filter_case_reapplies_initially_cleared_slicers(
    monkeypatch, tmp_path
) -> None:
    events = []
    clear_calls = []

    class Page:
        async def goto(self, *_args, **_kwargs):
            events.append("navigate")

    class Context:
        async def new_page(self):
            return Page()

        async def close(self):
            events.append("close")

    class Browser:
        async def new_context(self, **_kwargs):
            return Context()

    async def wait_for_ready(_page):
        events.append("ready")

    async def find_page_tab(_page, _name):
        events.append("select_page")

    async def clear_slicers(_page, names, *, require_all):
        clear_calls.append((names, require_all))
        events.append(
            "clear_before_combination"
            if len(clear_calls) == 1
            else "clear_after_combination"
        )
        return []

    async def wait_for_render(_page, settle_seconds):
        assert settle_seconds == 2.0
        events.append("render")

    async def baseline_matches(_page, _filters):
        events.append("baseline")
        return True

    async def apply_selections(
        _page, _selections, _filters, diagnostic_directory=None
    ):
        events.append("apply")

    async def verify_selection(_page, _filter, value):
        events.append("verify")
        return [value]

    async def capture_state(
        _page, _dashboard, _page_name, selection, *_args, **_kwargs
    ):
        events.append("capture")
        return SimpleNamespace(extracted={}, selection=selection)

    monkeypatch.setattr(browser, "_wait_for_report_ready", wait_for_ready)
    monkeypatch.setattr(browser, "_find_page_tab", find_page_tab)
    monkeypatch.setattr(
        browser, "_clear_page_slicer_selections", clear_slicers
    )
    monkeypatch.setattr(browser, "_wait_for_state_render", wait_for_render)
    monkeypatch.setattr(
        browser, "_filters_match_baseline", baseline_matches
    )
    monkeypatch.setattr(
        browser, "_apply_filter_selections", apply_selections
    )
    monkeypatch.setattr(
        browser, "_wait_for_filter_selection", verify_selection
    )
    monkeypatch.setattr(browser, "_capture_state", capture_state)

    selection = FilterSelection("Service Team", "Benefits")
    result = asyncio.run(
        browser._capture_filter_case(
            Browser(),
            {},
            "https://app.powerbi.com/report",
            "dashboard_A",
            "Overview",
            selection,
            [
                {
                    "name": "Service Team",
                    "selected": ["Payroll"],
                    "values": ["Benefits", "Payroll"],
                }
            ],
            tmp_path,
            0,
            {},
            asyncio.Semaphore(1),
            initially_cleared_slicers=["Relative Time"],
        )
    )

    assert result.selection == selection
    assert clear_calls == [
        (["Relative Time", "Service Team"], False),
        (["Relative Time"], True),
    ]
    assert events.index("select_page") < events.index("clear_before_combination")
    assert events.index("clear_before_combination") < events.index("apply")
    assert "baseline" not in events
    assert events.index("apply") < events.index("clear_after_combination")
    assert events.index("clear_after_combination") < events.index("capture")


def test_filter_case_applies_bookmark_before_selected_filters(
    monkeypatch, tmp_path
) -> None:
    events = []
    applied_selections = []

    class Page:
        async def goto(self, *_args, **_kwargs):
            events.append("navigate")

    class Context:
        async def new_page(self):
            return Page()

        async def close(self):
            events.append("close")

    class Browser:
        async def new_context(self, **_kwargs):
            return Context()

    async def no_op(*_args, **_kwargs):
        return None

    async def clear_slicers(*_args, **_kwargs):
        events.append("clear")
        return []

    async def wait_for_render(_page, settle_seconds):
        assert settle_seconds == 2.0
        events.append("settle")

    async def baseline_matches(_page, _filters):
        events.append("baseline")
        return True

    async def apply_selections(
        _page, selections, _filters, diagnostic_directory=None
    ):
        applied_selections.append(
            tuple((item.name, item.value) for item in selections)
        )
        events.append("apply")

    async def verify_selection(_page, _filter, value):
        return [value]

    async def capture_state(
        _page, _dashboard, _page_name, selection, *_args, **_kwargs
    ):
        events.append("capture")
        return SimpleNamespace(extracted={}, selection=selection)

    monkeypatch.setattr(browser, "_wait_for_report_ready", no_op)
    monkeypatch.setattr(browser, "_find_page_tab", no_op)
    monkeypatch.setattr(browser, "_clear_page_slicer_selections", clear_slicers)
    monkeypatch.setattr(browser, "_wait_for_state_render", wait_for_render)
    monkeypatch.setattr(browser, "_filters_match_baseline", baseline_matches)
    monkeypatch.setattr(browser, "_apply_filter_selections", apply_selections)
    monkeypatch.setattr(
        browser, "_wait_for_filter_selection", verify_selection
    )
    monkeypatch.setattr(browser, "_capture_state", capture_state)

    selection = FilterCombination(
        (
            FilterSelection("Service Team", "HR Support"),
            FilterSelection("View", "E-Mail"),
        )
    )
    result = asyncio.run(
        browser._capture_filter_case(
            Browser(),
            {},
            "https://app.powerbi.com/report",
            "dashboard_A",
            "Compliance",
            selection,
            [
                {
                    "name": "Service Team",
                    "control_type": "radio_dropdown",
                    "selected": ["HR Support"],
                    "values": ["HR Support"],
                },
                {
                    "name": "View",
                    "control_type": "bookmark_buttons",
                    "selected": ["E-Mail"],
                    "values": ["E-Mail", "Phone Call"],
                },
            ],
            tmp_path,
            0,
            {},
            asyncio.Semaphore(1),
        )
    )

    assert result.selection == selection
    assert applied_selections == [
        (("View", "E-Mail"),),
        (("Service Team", "HR Support"), ("View", "E-Mail")),
    ]
    bookmark_apply_index = events.index("apply")
    bookmark_settle_index = events.index("settle", bookmark_apply_index + 1)
    assert bookmark_apply_index < bookmark_settle_index
    combination_apply_index = events.index("apply", bookmark_settle_index + 1)
    assert bookmark_settle_index < combination_apply_index
    assert combination_apply_index < events.index("capture")
    assert "baseline" not in events


def test_filter_case_marks_unavailable_baseline_as_unverified(
    monkeypatch, tmp_path
) -> None:
    class Page:
        async def goto(self, *_args, **_kwargs):
            return None

    class Context:
        def __init__(self):
            self.closed = False

        async def new_page(self):
            return Page()

        async def close(self):
            self.closed = True

    class Browser:
        def __init__(self):
            self.context = Context()

        async def new_context(self, **_kwargs):
            return self.context

    browser_instance = Browser()

    async def no_op(*_args, **_kwargs):
        return None

    async def baseline_does_not_match(_page, _filters):
        return False

    async def unavailable_baseline(*_args, **_kwargs):
        raise browser.UnavailableFilterOptionError(
            "HR Support is not in the active bookmark's filter options"
        )

    async def apply_selections(*_args, **_kwargs):
        return None

    monkeypatch.setattr(browser, "_wait_for_report_ready", no_op)
    monkeypatch.setattr(browser, "_find_page_tab", no_op)
    monkeypatch.setattr(browser, "_clear_page_slicer_selections", no_op)
    monkeypatch.setattr(browser, "_wait_for_state_render", no_op)
    monkeypatch.setattr(browser, "_filters_match_baseline", baseline_does_not_match)
    monkeypatch.setattr(
        browser, "_restore_report_filters", unavailable_baseline
    )
    monkeypatch.setattr(browser, "_apply_filter_selections", apply_selections)

    selection = FilterCombination(
        (
            FilterSelection("Service Team", "HR Call Center"),
            FilterSelection("View", "E-Mail"),
        )
    )
    state = asyncio.run(
        browser._capture_filter_case(
            browser_instance,
            {},
            "https://app.powerbi.com/report",
            "dashboard_A",
            "Compliance",
            selection,
            [
                {
                    "name": "Service Team",
                    "control_type": "radio_dropdown",
                    "selected": ["HR Support"],
                    "values": ["HR Support"],
                },
                {
                    "name": "View",
                    "control_type": "bookmark_buttons",
                    "selected": ["E-Mail"],
                    "values": ["E-Mail", "Phone Call", "Workday Cases"],
                },
                {
                    "name": "Relative Time",
                    "selected": ["Last 4 Weeks"],
                    "values": ["Last 4 Weeks"],
                },
            ],
            tmp_path,
            0,
            {},
            asyncio.Semaphore(1),
        )
    )

    assert state.extracted["capture_status"] == "unverified"
    assert "HR Support is not in the active bookmark" in state.extracted[
        "capture_error"
    ]
    assert browser_instance.context.closed


def test_filter_case_combinations_use_nested_product_order() -> None:
    combinations = list(
        browser._filter_case_combinations(
            {
                "filters": [
                    {"name": "Filter 1", "values": ["1", "2", "3"]},
                    {
                        "name": "Filter 2",
                        "values": ["1", "2", "3", "4"],
                    },
                ]
            }
        )
    )

    assert len(combinations) == 12
    assert [
        tuple(selection.value for selection in combination.selections)
        for combination in combinations
    ] == [
        ("1", "1"),
        ("1", "2"),
        ("1", "3"),
        ("1", "4"),
        ("2", "1"),
        ("2", "2"),
        ("2", "3"),
        ("2", "4"),
        ("3", "1"),
        ("3", "2"),
        ("3", "3"),
        ("3", "4"),
    ]


def test_bookmark_variants_use_their_live_filter_options() -> None:
    bookmark_name = "View"
    filter_name = "Service Team"
    base_inventory = [
        {
            "name": bookmark_name,
            "control_type": "bookmark_buttons",
            "values": ["E-Mail", "Phone Call"],
            "selected": ["E-Mail"],
        },
        {
            "name": filter_name,
            "control_type": "dropdown",
            "values": ["Old option"],
            "selected": ["HR Support"],
        },
    ]
    base_plan = {
        "filters": [
            {"name": bookmark_name, "values": ["E-Mail", "Phone Call"]},
            {"name": filter_name, "values": ["Old option"]},
        ]
    }
    scoped_inventories = [
        (
            (FilterSelection(bookmark_name, "E-Mail"),),
            {
                "filters": [
                    {
                        **base_inventory[0],
                        "selected": ["E-Mail"],
                    },
                    {
                        **base_inventory[1],
                        "values": ["HR Support"],
                    },
                ]
            },
        ),
        (
            (FilterSelection(bookmark_name, "Phone Call"),),
            {
                "filters": [
                    {
                        **base_inventory[0],
                        "selected": ["Phone Call"],
                    },
                    {
                        **base_inventory[1],
                        "values": ["N/A", "HR Support"],
                    },
                ]
            },
        ),
    ]

    execution_plan = browser._build_bookmark_variant_execution_plan(
        base_plan,
        scoped_inventories,
        base_inventory,
    )
    combinations = list(browser._filter_case_combinations(execution_plan))

    assert [
        tuple((item.name, item.value) for item in combination.selections)
        for combination in combinations
    ] == [
        ((bookmark_name, "E-Mail"), (filter_name, "HR Support")),
        ((bookmark_name, "Phone Call"), (filter_name, "N/A")),
        ((bookmark_name, "Phone Call"), (filter_name, "HR Support")),
    ]
    assert browser._filter_inventory_for_selection(
        execution_plan, combinations[0], base_inventory
    )[1]["values"] == ["HR Support"]
    assert browser._filter_inventory_for_selection(
        execution_plan, combinations[1], base_inventory
    )[1]["values"] == ["N/A", "HR Support"]
    assert browser._initial_state_covered_by_filter_plan(
        [
            {**base_inventory[0], "selected": ["E-Mail"]},
            {**base_inventory[1], "selected": ["HR Support"]},
        ],
        execution_plan,
    ) == {
        "selections": [
            {"name": bookmark_name, "value": "E-Mail"},
            {"name": filter_name, "value": "HR Support"},
        ]
    }


def test_filter_inventory_uses_requested_bookmark_as_baseline() -> None:
    bookmark_name = "View"
    inventory = [
        {
            "name": bookmark_name,
            "control_type": "bookmark_buttons",
            "selected": ["Workday Cases"],
            "values": ["E-Mail", "Phone Call", "Workday Cases"],
        }
    ]

    selected_inventory = browser._filter_inventory_for_selection(
        {
            "filters": [
                {
                    "name": bookmark_name,
                    "values": ["E-Mail", "Phone Call", "Workday Cases"],
                }
            ]
        },
        FilterSelection(bookmark_name, "E-Mail"),
        inventory,
    )

    assert selected_inventory[0]["selected"] == ["E-Mail"]
    assert inventory[0]["selected"] == ["Workday Cases"]


def test_bookmark_discovery_applies_bookmarks_from_page_inventory(
    monkeypatch, tmp_path
) -> None:
    bookmark_name = "View"
    filter_name = "Service Team"
    workday_values = [
        "HR Support Center",
        "HR Systems",
        "HRIS",
        "Learning",
        "Legal Service Team",
        "Payroll",
        "Recruiting",
        "Risk Management",
        "SOX Compliance",
        "Talent",
    ]
    page_inventory = {
        "filters": [
            {
                "name": bookmark_name,
                "control_type": "bookmark_buttons",
                "values": ["E-Mail", "Phone Call", "Workday Cases"],
                "selected": ["E-Mail"],
            },
            {
                "name": filter_name,
                "control_type": "dropdown",
                "values": ["Old option"],
                "selected": [],
            },
        ],
        "initial_cleared_slicers": [],
    }

    class Page:
        bookmark = None

        async def goto(self, *_args, **_kwargs):
            return None

    class Context:
        def __init__(self):
            self.page = Page()

        async def new_page(self):
            return self.page

        async def close(self):
            return None

    class Browser:
        async def new_context(self, **_kwargs):
            return Context()

    settle_waits = []

    async def no_op(*_args, **_kwargs):
        return None

    async def wait_for_state_render(_page, settle_seconds):
        settle_waits.append(settle_seconds)

    async def inventory_after_clear(page, **_kwargs):
        values_by_bookmark = {
            "E-Mail": ["HR Support"],
            "Phone Call": ["N/A", "HR Support"],
            "Workday Cases": workday_values,
        }
        values = values_by_bookmark.get(page.bookmark, ["Old option"])
        return {
            "filters": [
                {
                    "name": filter_name,
                    "control_type": "dropdown",
                    "values": values,
                    "selected": [],
                }
            ]
        }

    async def apply_bookmark(page, selections, inventory, **_kwargs):
        assert any(
            item.get("name") == bookmark_name
            and item.get("control_type") == "bookmark_buttons"
            for item in inventory
        )
        page.bookmark = selections[0].value

    monkeypatch.setattr(browser, "_wait_for_report_ready", no_op)
    monkeypatch.setattr(browser, "_find_page_tab", no_op)
    monkeypatch.setattr(browser, "_reset_report_filters", no_op)
    monkeypatch.setattr(
        browser, "_wait_for_state_render", wait_for_state_render
    )
    monkeypatch.setattr(
        browser, "_inventory_page_after_clearing_slicers", inventory_after_clear
    )
    monkeypatch.setattr(browser, "_apply_filter_selections", apply_bookmark)

    execution_plan = asyncio.run(
        browser._discover_bookmark_variant_execution_plan(
            Browser(),
            {},
            "https://example.test/report",
            "dashboard",
            "Compliance",
            page_inventory,
            {
                "filters": [
                    {
                        "name": bookmark_name,
                        "values": ["E-Mail", "Phone Call", "Workday Cases"],
                    },
                    {"name": filter_name, "values": ["Old option"]},
                ]
            },
            tmp_path,
            0,
            "test-job",
            None,
        )
    )

    assert [
        combination.key
        for combination in browser._filter_case_combinations(execution_plan)
    ] == [
        "View=E-Mail; Service Team=HR Support",
        "View=Phone Call; Service Team=N/A",
        "View=Phone Call; Service Team=HR Support",
        *[
            f"View=Workday Cases; Service Team={value}"
            for value in workday_values
        ],
    ]
    workday_variant = next(
        variant
        for variant in execution_plan["bookmark_variants"]
        if variant["bookmark_selections"][0]["value"] == "Workday Cases"
    )
    workday_filter = next(
        item
        for item in workday_variant["filter_inventory"]
        if item["name"] == filter_name
    )
    assert workday_filter["values"] == workday_values
    assert settle_waits == [0, 2.0, 0, 2.0, 0, 2.0]


def test_service_team_plan_keeps_each_option_as_a_single_case() -> None:
    values = [
        "Benefits",
        "Compensation",
        "Compliance",
        "Create Job Requisition",
        "HR Business Partners",
        "HR Support Center",
        "HR Systems",
        "HRIS",
        "Learning",
        "Legal Service Team",
        "Payroll",
    ]

    selections = list(
        browser._filter_case_combinations(
            {"filters": [{"name": "Service Team", "values": values}]}
        )
    )

    assert [selection.value for selection in selections] == values


def test_filter_combination_selects_each_filter_before_capture(
    monkeypatch, tmp_path
) -> None:
    selected = []
    capture_selection = []

    class Page:
        async def goto(self, *_args, **_kwargs):
            return None

    class Context:
        async def new_page(self):
            return Page()

        async def close(self):
            return None

    class Browser:
        async def new_context(self, **_kwargs):
            return Context()

    async def no_wait(_page):
        return None

    async def no_tab(_page, _name):
        return None

    async def clear_slicers(_page, _names=None, *, require_all=True):
        return []

    async def no_state_wait(_page, settle_seconds):
        assert settle_seconds == 2.0

    async def baseline_matches(_page, _filters):
        return True

    async def select_filter(
        _page, selection, _filters, diagnostic_directory=None
    ):
        selected.append((selection.name, selection.value))

    async def verify_filter(_page, _filter, value):
        return [value]

    async def capture_state(_page, _dashboard, _page_name, selection, *_args, **_kwargs):
        capture_selection.append(selection)
        return selection

    monkeypatch.setattr(browser, "_wait_for_report_ready", no_wait)
    monkeypatch.setattr(browser, "_find_page_tab", no_tab)
    monkeypatch.setattr(
        browser, "_clear_page_slicer_selections", clear_slicers
    )
    monkeypatch.setattr(browser, "_wait_for_state_render", no_state_wait)
    monkeypatch.setattr(browser, "_filters_match_baseline", baseline_matches)
    monkeypatch.setattr(browser, "_select_filter", select_filter)
    monkeypatch.setattr(
        browser, "_wait_for_filter_selection", verify_filter
    )
    monkeypatch.setattr(browser, "_capture_state", capture_state)

    combination = FilterCombination(
        (
            FilterSelection("Filter 1", "1"),
            FilterSelection("Filter 2", "10"),
        )
    )
    result = asyncio.run(
        browser._capture_filter_case(
            Browser(),
            {},
            "https://app.powerbi.com/report",
            "dashboard_A",
            "Overview",
            combination,
            [
                {"name": "Filter 1", "selected": ["1"], "values": ["1"]},
                {"name": "Filter 2", "selected": ["10"], "values": ["10"]},
            ],
            tmp_path,
            0,
            {},
            asyncio.Semaphore(1),
        )
    )

    assert selected == [("Filter 1", "1"), ("Filter 2", "10")]
    assert capture_selection == [combination]
    assert result is combination


def test_unavailable_filter_option_marks_case_unverified_without_capture(
    monkeypatch, tmp_path
) -> None:
    class Page:
        async def goto(self, *_args, **_kwargs):
            return None

    class Context:
        closed = False

        async def new_page(self):
            return Page()

        async def close(self):
            self.closed = True

    context = Context()

    class Browser:
        async def new_context(self, **_kwargs):
            return context

    async def no_wait(_page):
        return None

    async def no_tab(_page, _name):
        return None

    async def clear_slicers(_page, _names=None, *, require_all=True):
        return []

    async def no_state_wait(_page, settle_seconds):
        assert settle_seconds == 2.0

    async def baseline_matches(_page, _filters):
        return True

    async def unavailable(
        _page, _selections, _filters, diagnostic_directory=None
    ):
        raise browser.UnavailableFilterOptionError(
            "Option 'Benefits' is unavailable for filter 'Service Team'"
        )

    async def unexpected_capture(*_args, **_kwargs):
        raise AssertionError("An unavailable filter state must not be captured")

    monkeypatch.setattr(browser, "_wait_for_report_ready", no_wait)
    monkeypatch.setattr(browser, "_find_page_tab", no_tab)
    monkeypatch.setattr(
        browser, "_clear_page_slicer_selections", clear_slicers
    )
    monkeypatch.setattr(browser, "_wait_for_state_render", no_state_wait)
    monkeypatch.setattr(browser, "_filters_match_baseline", baseline_matches)
    monkeypatch.setattr(browser, "_apply_filter_selections", unavailable)
    monkeypatch.setattr(browser, "_capture_state", unexpected_capture)

    selection = FilterSelection("Service Team", "Benefits")
    state = asyncio.run(
        browser._capture_filter_case(
            Browser(),
            {},
            "https://app.powerbi.com/report",
            "dashboard_A",
            "Overview",
            selection,
            [{"name": "Service Team", "values": ["Benefits"]}],
            tmp_path,
            0,
            {},
            asyncio.Semaphore(1),
        )
    )

    assert state.extracted == {
        "capture_status": "unverified",
        "capture_error": (
            "Option 'Benefits' is unavailable for filter 'Service Team'"
        ),
    }
    assert not state.screenshot.exists()
    assert context.closed


def test_filter_combination_applies_bookmarks_first_and_rechecks_filters(
    monkeypatch,
) -> None:
    selected = {
        "Service Team": ["HR Support Center"],
        "Bookmark buttons: E-Mail | Phone Call | Workday Cases": [
            "Workday Cases"
        ],
    }
    events = []
    filter_inventory = [
        {
            "name": "Service Team",
            "control_type": "radio_dropdown",
            "values": ["HR Support Center", "Benefits"],
        },
        {
            "name": "Bookmark buttons: E-Mail | Phone Call | Workday Cases",
            "control_type": "bookmark_buttons",
            "values": ["E-Mail", "Phone Call", "Workday Cases"],
        },
    ]

    async def select_filter(
        _page, selection, _filters, diagnostic_directory=None
    ):
        events.append(("select", selection.name, selection.value))
        selected[selection.name] = [selection.value]
        if selection.name.startswith("Bookmark buttons:"):
            selected["Service Team"] = ["HR Support Center"]

    async def wait_for_selection(_page, filter_item, _value):
        events.append(("verify", filter_item["name"]))
        return selected[filter_item["name"]]

    monkeypatch.setattr(browser, "_select_filter", select_filter)
    monkeypatch.setattr(
        browser, "_wait_for_filter_selection", wait_for_selection
    )

    asyncio.run(
        browser._apply_filter_selections(
            object(),
            (
                browser.FilterSelection("Service Team", "Benefits"),
                browser.FilterSelection(
                    "Bookmark buttons: E-Mail | Phone Call | Workday Cases",
                    "E-Mail",
                ),
            ),
            filter_inventory,
        )
    )

    assert events == [
        (
            "select",
            "Bookmark buttons: E-Mail | Phone Call | Workday Cases",
            "E-Mail",
        ),
        ("select", "Service Team", "Benefits"),
        ("verify", "Service Team"),
        ("verify", "Bookmark buttons: E-Mail | Phone Call | Workday Cases"),
    ]
    assert selected["Service Team"] == ["Benefits"]


def test_filter_combinations_capture_concurrently_in_plan_order() -> None:
    combinations = browser._filter_case_combinations(
        {
            "filters": [
                {"name": "Filter 1", "values": ["1", "2"]},
                {"name": "Filter 2", "values": ["1", "2", "3"]},
            ]
        }
    )
    captured = []
    active = 0
    maximum_active = 0

    async def capture(combination):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        captured.append(combination.key)
        await asyncio.sleep(0.001)
        active -= 1
        return combination.key

    results = asyncio.run(
        browser._capture_filter_combinations_in_order(
            combinations, capture
        )
    )

    assert maximum_active == 6
    assert results == captured
    assert captured == [
        "Filter 1=1; Filter 2=1",
        "Filter 1=1; Filter 2=2",
        "Filter 1=1; Filter 2=3",
        "Filter 1=2; Filter 2=1",
        "Filter 1=2; Filter 2=2",
        "Filter 1=2; Filter 2=3",
    ]


def test_filter_case_failure_is_recorded_and_later_combinations_continue() -> None:
    selections = [
        browser.FilterSelection("Service Team", "SOX Compliance"),
        browser.FilterSelection("Service Team", "Talent"),
    ]
    attempted = []

    async def capture(selection):
        attempted.append(selection.value)
        if selection.value == "SOX Compliance":
            raise RuntimeError("baseline restoration failed")
        return selection.value

    def record_failure(selection, exc):
        return f"unverified:{selection.value}:{exc}"

    results = asyncio.run(
        browser._capture_filter_combinations_in_order(
            selections, capture, on_error=record_failure
        )
    )

    assert attempted == ["SOX Compliance", "Talent"]
    assert results == [
        "unverified:SOX Compliance:baseline restoration failed",
        "Talent",
    ]




def test_exported_csv_is_converted_to_visual_rows() -> None:
    element = browser._export_element(
        "Excess Before Investments",
        "data.csv",
        b"Week Ending,Center Store,Military\r\n2026-01-01,100,25\r\n",
        "chart",
    )

    assert element == {
        "type": "chart",
        "title": "Excess Before Investments",
        "value": None,
        "categories": ["Week Ending", "Center Store", "Military"],
        "values": [],
        "rows": [["2026-01-01", "100", "25"]],
    }


def test_exported_csv_preserves_columns_missing_from_first_header_row() -> None:
    element = browser._export_element(
        "Trend",
        "data.csv",
        b"Week Ending,Sales\r\n2026-01-01,100,25\r\n2026-01-08,110\r\n",
        "chart",
    )

    assert element["categories"] == ["Week Ending", "Sales", "Column 3"]
    assert element["rows"] == [
        ["2026-01-01", "100", "25"],
        ["2026-01-08", "110", None],
    ]


def test_exported_xlsx_is_converted_to_visual_rows() -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Week Ending", "Center Store"])
    sheet.append(["2026-01-01", 100])
    content = io.BytesIO()
    workbook.save(content)

    element = browser._export_element(
        "Excess Before Investments",
        "data.xlsx",
        content.getvalue(),
        "chart",
    )

    assert element["categories"] == ["Week Ending", "Center Store"]
    assert element["rows"] == [["2026-01-01", 100]]


def test_exported_xlsx_converts_every_worksheet_to_json_rows() -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    summary = workbook.active
    summary.title = "Summary"
    summary.append(["Week", "Sales"])
    summary.append(["Week 1", 100])
    details = workbook.create_sheet("Details")
    details.append(["Week", "Region"])
    details.append(["Week 1", "West"])
    content = io.BytesIO()
    workbook.save(content)

    document = browser._export_json_document(
        "Sales trend", "data.xlsx", content.getvalue()
    )
    element = browser._export_element(
        "Sales trend", "data.xlsx", content.getvalue(), "chart"
    )

    assert document["status"] == "converted"
    assert document["worksheets"][0]["rows"] == [{"Week": "Week 1", "Sales": 100}]
    assert document["worksheets"][1]["rows"] == [
        {"Week": "Week 1", "Region": "West"}
    ]
    assert [sheet["name"] for sheet in element["worksheets"]] == [
        "Summary",
        "Details",
    ]


def test_exported_xlsx_reads_data_when_declared_dimensions_are_incorrect() -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Applied filters:\nRegion is West"])
    sheet.append(["Region", "Sales"])
    sheet.append(["West", 100])
    source = io.BytesIO()
    workbook.save(source)

    corrupted_dimensions = io.BytesIO()
    with zipfile.ZipFile(source) as original:
        with zipfile.ZipFile(corrupted_dimensions, "w") as rebuilt:
            for item in original.infolist():
                content = original.read(item.filename)
                if item.filename == "xl/worksheets/sheet1.xml":
                    content = content.replace(
                        b'<dimension ref="A1:B3"/>',
                        b'<dimension ref="A1"/>',
                    )
                rebuilt.writestr(item, content)

    document = browser._export_json_document(
        "Sales table", "data.xlsx", corrupted_dimensions.getvalue()
    )
    element = browser._export_element(
        "Sales table", "data.xlsx", corrupted_dimensions.getvalue(), "table"
    )

    assert document["status"] == "converted"
    assert document["worksheets"][0]["applied_filters"] == ["Region is West"]
    assert document["worksheets"][0]["rows"] == [{"Region": "West", "Sales": 100}]
    assert element["rows"] == [["West", 100]]


def test_export_with_applied_filters_only_is_not_visual_data() -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.active.append(["Applied filters:\nFiscal Year is greater than 2022"])
    content = io.BytesIO()
    workbook.save(content)

    document = browser._export_json_document(
        "Cases by Caller Type", "data.xlsx", content.getvalue()
    )

    assert document["status"] == "no_tabular_data"
    assert document["worksheets"][0]["rows"] == []
    assert document["worksheets"][0]["applied_filters"] == [
        "Fiscal Year is greater than 2022"
    ]
    with pytest.raises(ValueError, match="applied-filter metadata"):
        browser._export_element(
            "Cases by Caller Type",
            "data.xlsx",
            content.getvalue(),
            "chart",
        )


def test_visual_export_uses_default_and_saves_evidence(
    monkeypatch, tmp_path
) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.active.append(["Week", "Cases"])
    workbook.active.append(["10/3/2026", 68])
    content = io.BytesIO()
    workbook.save(content)
    events = []

    class LocatorList:
        def __init__(self, items):
            self.items = items

        async def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    class MoreOptionsButton:
        def __init__(self, title):
            self.title = title

        async def is_visible(self):
            return True

        async def is_enabled(self):
            return True

        async def hover(self):
            raise AssertionError("The intercepted visual button must not be hovered")

        async def evaluate(self, script):
            assert script == "(element) => element.click()"
            events.append(f"more-options:{self.title}")
            page.current_title = self.title
            page.menu_open = True


    class Container:
        def __init__(self, title):
            self.title = title

        def get_by_role(self, role, name):
            assert role == "button"
            assert name.search("more options")
            return LocatorList([MoreOptionsButton(self.title)])

        async def is_visible(self):
            return True

        async def bounding_box(self):
            return {"x": 10, "y": 20, "width": 400, "height": 200}

        async def evaluate(self, _script, config=None):
            if config is not None:
                raise AssertionError("Masking occurs after the export pass")
            return {
                "title": self.title,
                "type": "chart",
                "visual_id": self.title.casefold().replace(" ", "-"),
            }

    class ExportButton:
        async def is_visible(self):
            return True

        async def is_enabled(self):
            return True

        async def click(self, **kwargs):
            assert kwargs == {"force": True, "timeout": 5_000}
            events.append(f"export:{page.current_title}")
            page.download_expectation.future.set_result(
                Download(page.current_title)
            )

    class Download:
        def __init__(self, title):
            self.suggested_filename = f"{title}.xlsx"

        async def save_as(self, path):
            Path(path).write_bytes(content.getvalue())

    class Dialog:
        async def is_visible(self):
            return page.dialog_open

        def get_by_role(self, role, name=None):
            if role == "button":
                assert name.search("Export")
                return LocatorList([ExportButton()])
            raise AssertionError(role)

    class MenuItem:
        async def is_visible(self):
            return page.menu_open

        async def is_enabled(self):
            return True

        async def click(self):
            events.append(f"export-data:{page.current_title}")
            page.dialog_open = True

    class Menu:
        async def is_visible(self):
            return page.menu_open

    class Frame:
        def locator(self, selector):
            assert selector == browser.VISUAL_SELECTOR
            return LocatorList(
                [
                    Container("Pending cases duration"),
                    Container("Cases by Caller Type"),
                ]
            )

        def get_by_role(self, role, name=None):
            if role == "menu":
                return LocatorList([Menu()] if page.menu_open else [])
            if role == "menuitem":
                assert name.search("Export data")
                return LocatorList([MenuItem()] if page.menu_open else [])
            if role == "dialog":
                return LocatorList([Dialog()])
            if role == "button":
                assert name.search("More options")
                return LocatorList([])
            raise AssertionError(role)

    class DownloadExpectation:
        def __init__(self):
            self.future = asyncio.get_running_loop().create_future()

        async def __aenter__(self):
            page.download_expectation = self
            return self

        async def __aexit__(self, *_args):
            page.download_expectation = None

        @property
        def value(self):
            return self.future

    mouse_positions = []

    class Mouse:
        async def move(self, x, y):
            mouse_positions.append((x, y))

    class Keyboard:
        async def press(self, key):
            assert key == "Escape"
            events.append(f"escape:{page.current_title}")
            page.menu_open = False
            page.dialog_open = False

    class Page:
        def __init__(self):
            self.frames = [Frame()]
            self.mouse = Mouse()
            self.keyboard = Keyboard()
            self.menu_open = False
            self.dialog_open = False
            self.download_expectation = None
            self.current_title = ""

        async def screenshot(self, path, full_page=False):
            assert full_page is False
            Path(path).write_bytes(b"menu screenshot")
            return b"menu screenshot"

        def expect_download(self, timeout):
            assert timeout == browser.EXPORT_DOWNLOAD_TIMEOUT_MS
            return DownloadExpectation()

        async def wait_for_timeout(self, _milliseconds):
            pass

    page = Page()
    performance_timings = []

    exported, needs_fallback, options = asyncio.run(
        browser._export_visual_data(
            page,
            tmp_path / "state_exports",
            performance_timings=performance_timings,
            dashboard="Dashboard A",
            page_name="Summary",
            filter_state="initial",
        )
    )

    assert events[:4] == [
        "more-options:Pending cases duration",
        "export-data:Pending cases duration",
        "export:Pending cases duration",
        "escape:Pending cases duration",
    ]
    assert events[4:8] == [
        "more-options:Cases by Caller Type",
        "export-data:Cases by Caller Type",
        "export:Cases by Caller Type",
        "escape:Cases by Caller Type",
    ]
    assert mouse_positions[:2] == [(210, 120), (210, 120)]
    assert [item["title"] for item in exported] == [
        "Pending cases duration",
        "Cases by Caller Type",
    ]
    assert all("mask_status" not in item for item in exported)
    assert all(item["rows"] == [["10/3/2026", 68]] for item in exported)
    assert exported[0]["exportOptionAvailable"] is True
    assert exported[0]["export_data_format"] == "default"
    assert exported[0]["data_sources"] == ["powerbi_export"]
    assert exported[0]["page_name"] == "Summary"
    assert exported[0]["filter_state"] == "initial"
    assert exported[0]["visual_index"] == 0
    assert exported[0]["visual_id"] == "pending-cases-duration"
    assert options[0]["export_file"] == "00_Pending_cases_duration.xlsx"
    assert options[0]["export_file_path"] == (
        "state_exports/00_Pending_cases_duration.xlsx"
    )
    assert options[0]["exportOptionAvailable"] is True
    assert options[0]["export_status"] == "exported"
    assert options[0]["export_data_format"] == "default"
    assert options[0]["page_name"] == "Summary"
    assert options[0]["filter_state"] == "initial"
    assert options[0]["screenshot"] == (
        "state_exports/evidence/00_Pending_cases_duration_options.png"
    )
    assert options[0]["export_dialog_screenshot"] == (
        "state_exports/evidence/00_Pending_cases_duration_export_dialog.png"
    )
    assert [item["title"] for item in performance_timings] == [
        "Pending cases duration",
        "Cases by Caller Type",
    ]
    assert all(item["duration_seconds"] >= 0 for item in performance_timings)
    assert (tmp_path / "state_exports" / "00_Pending_cases_duration.xlsx").is_file()
    assert (tmp_path / options[0]["screenshot"]).is_file()
    assert (tmp_path / options[0]["export_dialog_screenshot"]).is_file()
    assert (tmp_path / options[0]["export_file_path"]).is_file()
    assert needs_fallback is False


def test_export_data_option_matches_exact_menuitem() -> None:
    class LocatorList:
        def __init__(self, items=()):
            self.items = list(items)

        async def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    class ExportButton:
        async def is_visible(self):
            return True

        async def is_enabled(self):
            return True

    class Frame:
        def get_by_role(self, role, name=None):
            if role == "menuitem":
                assert name.search("Export data")
                return LocatorList([ExportButton()])
            if role == "button":
                raise AssertionError("Export should be located as a menu item")
            raise AssertionError(role)

        def get_by_text(self, *_args, **_kwargs):
            raise AssertionError("The button role should match first")

    class Page:
        frames = [Frame()]

    option, menu_open = asyncio.run(
        browser._find_export_data_option(Page())
    )

    assert isinstance(option, ExportButton)
    assert menu_open is True


def test_export_waits_for_delayed_more_options_button() -> None:
    waits = []
    polls = [0]

    class Button:
        async def is_visible(self):
            return True

        async def is_enabled(self):
            return True

    button = Button()

    class Buttons:
        def __init__(self, scoped):
            self.scoped = scoped

        async def count(self):
            if not self.scoped:
                return 0
            polls[0] += 1
            return 1 if polls[0] >= 3 else 0

        def nth(self, index):
            assert index == 0
            return button

    class Container:
        def get_by_role(self, role, name):
            assert role == "button"
            assert name.search("more options")
            return Buttons(scoped=True)

    class Frame:
        def get_by_role(self, role, name):
            assert role == "button"
            assert name.search("more options")
            return Buttons(scoped=False)

    class Page:
        async def wait_for_timeout(self, milliseconds):
            waits.append(milliseconds)

    result = asyncio.run(
        browser._wait_for_visual_more_options(
            Page(), Frame(), Container(), "Average Queue Time (Minutes)"
        )
    )

    assert result is button
    assert waits == [100, 100]


def test_more_options_button_can_be_matched_by_visual_bounds() -> None:
    class LocatorList:
        def __init__(self, items=()):
            self.items = list(items)

        async def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    class Button:
        async def is_visible(self):
            return True

        async def is_enabled(self):
            return True

        async def bounding_box(self):
            return {"x": 195, "y": 105, "width": 10, "height": 10}

        async def evaluate(self, _script):
            return "A different accessible title"

    button = Button()

    class Container:
        def get_by_role(self, role, name):
            assert role == "button"
            assert name.search("more options")
            return LocatorList()

    class Frame:
        def __init__(self, buttons=()):
            self.buttons = buttons

        def get_by_role(self, role, name):
            assert role == "button"
            assert name.search("more options")
            return LocatorList(self.buttons)

    visual_frame = Frame()
    toolbar_frame = Frame([button])

    result = asyncio.run(
        browser._visual_more_options_button(
            visual_frame,
            Container(),
            "Average InQueue Time",
            {"x": 100, "y": 50, "width": 200, "height": 100},
            [visual_frame, toolbar_frame],
        )
    )

    assert result is button


def test_unavailable_more_options_is_deferred_until_data_visual_identification(
    monkeypatch, tmp_path
) -> None:
    async def no_button(*args):
        return None

    async def move_pointer_off_dashboard(page):
        return None

    monkeypatch.setattr(browser, "_wait_for_visual_more_options", no_button)
    monkeypatch.setattr(
        browser, "_move_pointer_off_dashboard", move_pointer_off_dashboard
    )

    class Container:
        async def is_visible(self):
            return True

        async def evaluate(self, script):
            return {"title": "Average Queue Time (Minutes)", "type": "chart"}

        async def bounding_box(self):
            return {"x": 0, "y": 0, "width": 100, "height": 100}

    container = Container()

    class Containers:
        async def count(self):
            return 1

        def nth(self, index):
            assert index == 0
            return container

    class Frame:
        def locator(self, selector):
            return Containers()

    class Mouse:
        async def move(self, x, y):
            return None

    class Page:
        frames = [Frame()]
        mouse = Mouse()

    _, needs_fallback, options = asyncio.run(
        browser._export_visual_data(Page(), tmp_path)
    )

    assert needs_fallback is False
    assert options[0]["exportOptionAvailable"] is None
    assert options[0]["export_status"] == "more_options_unavailable"
    browser._validate_data_visual_export_coverage(
        {"elements": [{"type": "chart", "title": "Average Queue Time (Minutes)"}]},
        options,
    )


@pytest.mark.parametrize(
    ("status", "expected_error"),
    [
        ("download_timeout", None),
        ("export_button_unavailable", None),
        ("downloaded_no_tabular_data", None),
    ],
)
def test_data_visual_export_validation_allows_screenshot_fallback(
    status, expected_error
) -> None:
    extracted = {
        "elements": [
            {"type": "table", "title": "Cases by Caller Type"},
        ]
    }
    options = [
        {
            "title": "Cases by Caller Type",
            "exportOptionAvailable": True,
            "export_status": status,
        }
    ]

    if expected_error:
        with pytest.raises(RuntimeError, match=expected_error):
            browser._validate_data_visual_export_coverage(extracted, options)
    else:
        browser._validate_data_visual_export_coverage(extracted, options)


def test_data_visual_export_validation_rejects_unscanned_visual() -> None:
    extracted = {
        "elements": [
            {"type": "chart", "title": "Cases by Caller Type"},
        ]
    }

    with pytest.raises(RuntimeError, match="No export availability check"):
        browser._validate_data_visual_export_coverage(extracted, [])


def test_closed_more_options_menu_is_recorded_as_unverified(
    tmp_path
) -> None:
    class LocatorList:
        def __init__(self, items=()):
            self.items = list(items)

        async def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    class MoreOptionsButton:
        async def is_visible(self):
            return True

        async def is_enabled(self):
            return True

        async def evaluate(self, _script):
            return None

    class Container:
        def get_by_role(self, role, name=None):
            assert role == "button"
            assert name.search("more options")
            return LocatorList([MoreOptionsButton()])

        async def is_visible(self):
            return True

        async def bounding_box(self):
            return {"x": 0, "y": 0, "width": 100, "height": 100}

        async def evaluate(self, _script):
            return {"title": "Average Queue Time", "type": "chart"}

    class Frame:
        def locator(self, _selector):
            return LocatorList([Container()])

        def get_by_role(self, role, name=None):
            assert role in {"menu", "menuitem", "button"}
            return LocatorList()

        def get_by_text(self, *_args, **_kwargs):
            return LocatorList()

    class Mouse:
        async def move(self, *_args):
            pass

    class Page:
        frames = [Frame()]
        mouse = Mouse()

        async def wait_for_timeout(self, _milliseconds):
            pass

        async def screenshot(self, **_kwargs):
            return b"menu evidence"

    _, needs_fallback, options = asyncio.run(
        browser._export_visual_data(Page(), tmp_path)
    )

    assert needs_fallback is False
    assert options[0]["exportOptionAvailable"] is None
    assert options[0]["export_status"] == "export_menu_unverified"
    assert "could not be verified" in options[0]["export_error"]


@pytest.mark.parametrize(
    ("visual_count", "options", "expected"),
    [
        (1, [{"exportOptionAvailable": False}], True),
        (1, [{"exportOptionAvailable": True, "export_status": "download_timeout"}], False),
        (1, [{"exportOptionAvailable": True, "export_status": "downloaded_no_tabular_data"}], False),
        (1, [{"exportOptionAvailable": True, "export_status": "exported"}], False),
        (1, [{"exportOptionAvailable": None, "export_status": "more_options_unavailable"}], False),
        (0, [], True),
    ],
)
def test_scrolling_fallback_requires_missing_export_option(
    visual_count, options, expected
) -> None:
    assert browser._needs_scrolled_view_fallback(visual_count, options) is expected


def test_screenshot_merge_unions_and_aligns_all_table_columns() -> None:
    base = {
        "page": "Report",
        "elements": [
            {
                "type": "table",
                "title": "Trend",
                "categories": ["Week Ending", "Sales", "Inventory"],
                "rows": [["1/1/2026", 100, 500]],
                "content": {
                    "columns": ["Week Ending", "Sales", "Inventory"],
                    "rows": [
                        {
                            "Week Ending": {"raw": "1/1/2026", "status": "read"},
                            "Sales": {"raw": 100, "status": "read"},
                            "Inventory": {"raw": 500, "status": "read"},
                        }
                    ],
                },
            }
        ],
    }
    horizontal_view = {
        "page": "Report",
        "elements": [
            {
                "type": "table",
                "title": "Trend",
                "categories": ["Week Ending", "Inventory", "Orders"],
                "rows": [["1/1/2026", 500, 25], ["1/8/2026", 510, 30]],
                "content": {
                    "columns": ["Week Ending", "Inventory", "Orders"],
                    "rows": [
                        {
                            "Week Ending": {"raw": "1/1/2026", "status": "read"},
                            "Inventory": {"raw": 500, "status": "read"},
                            "Orders": {"raw": 25, "status": "read"},
                        },
                        {
                            "Week Ending": {"raw": "1/8/2026", "status": "read"},
                            "Inventory": {"raw": 510, "status": "read"},
                            "Orders": {"raw": 30, "status": "read"},
                        },
                    ],
                },
            }
        ],
    }

    merged = browser._merge_extractions(base, horizontal_view)

    assert merged["elements"][0]["categories"] == [
        "Week Ending",
        "Sales",
        "Inventory",
        "Orders",
    ]
    assert merged["elements"][0]["rows"] == [
        ["1/1/2026", 100, 500, 25],
        ["1/8/2026", None, 510, 30],
    ]
    assert merged["elements"][0]["content"]["columns"] == [
        "Week Ending",
        "Sales",
        "Inventory",
        "Orders",
    ]
    assert merged["elements"][0]["content"]["rows"][1]["Sales"] == {
        "raw": None,
        "status": "not_visible",
        "reason": "Not captured in this view",
    }


def test_export_option_metadata_is_attached_to_extracted_visuals() -> None:
    extracted = {
        "elements": [
            {"type": "chart", "title": "Sales trend"},
            {"type": "chart", "title": "Orders"},
        ]
    }
    options = [
        {
            "title": "Sales trend",
            "exportOptionAvailable": True,
            "export_status": "exported",
            "export_data_format": "default",
            "export_file": "sales.xlsx",
            "export_file_path": "state_exports/sales.xlsx",
            "page_name": "Summary",
            "filter_state": "Service Team=Payroll",
            "visual_id": "sales-visual",
            "mask_status": "masked",
            "screenshot": "state_exports/sales_options.png",
        },
        {
            "title": "Orders",
            "exportOptionAvailable": False,
            "export_status": "export_data_unavailable",
            "export_data_format": None,
            "export_file": None,
            "screenshot": "state_exports/orders_options.png",
        },
    ]

    result = browser._attach_export_option_metadata(extracted, options)

    assert result["elements"][0]["exportOptionAvailable"] is True
    assert result["elements"][0]["export_file"] == "sales.xlsx"
    assert result["elements"][0]["export_file_path"] == (
        "state_exports/sales.xlsx"
    )
    assert result["elements"][0]["page_name"] == "Summary"
    assert result["elements"][0]["filter_state"] == "Service Team=Payroll"
    assert result["elements"][0]["visual_id"] == "sales-visual"
    assert result["elements"][0]["mask_status"] == "masked"
    assert result["elements"][0]["export_data_format"] == "default"
    assert result["elements"][1]["exportOptionAvailable"] is False
    assert result["elements"][1]["export_status"] == "export_data_unavailable"
    assert result["export_options"] == options


def test_screenshot_merge_preserves_row_cells_without_a_header() -> None:
    base = {
        "page": "Report",
        "elements": [
            {
                "type": "table",
                "title": "Trend",
                "categories": ["Week Ending", "Sales"],
                "rows": [["1/1/2026", 100, 500]],
            }
        ],
    }
    additional = {
        "page": "Report",
        "elements": [
            {
                "type": "table",
                "title": "Trend",
                "categories": ["Week Ending", "Sales"],
                "rows": [["1/8/2026", 110, 510, 700]],
            }
        ],
    }

    merged = browser._merge_extractions(base, additional)

    assert merged["elements"][0]["categories"] == [
        "Week Ending",
        "Sales",
        "Column 3",
        "Column 4",
    ]
    assert merged["elements"][0]["rows"] == [
        ["1/1/2026", 100, 500, None],
        ["1/8/2026", 110, 510, 700],
    ]


def test_export_merge_does_not_discard_screenshot_only_columns() -> None:
    extracted = {
        "page": "Report",
        "elements": [
            {
                "type": "chart",
                "title": "Trend",
                "categories": ["Week Ending", "Sales", "Inventory"],
                "rows": [["1/1/2026", 100, 500]],
            }
        ],
    }
    exported = [
        {
            "type": "chart",
            "title": "Trend",
            "categories": ["Week Ending", "Orders"],
            "values": [],
            "rows": [["1/1/2026", 25], ["1/8/2026", 30]],
        }
    ]

    merged = browser._merge_exported_elements(extracted, exported)

    assert merged["exported_data"][0]["categories"] == ["Week Ending", "Orders"]
    assert merged["exported_data"][0]["rows"] == [
        ["1/1/2026", 25],
        ["1/8/2026", 30],
    ]
    assert merged["elements"][0]["categories"] == [
        "Week Ending",
        "Sales",
        "Inventory",
        "Orders",
    ]
    assert merged["elements"][0]["rows"] == [
        ["1/1/2026", 100, 500, 25],
        ["1/8/2026", None, None, 30],
    ]
    assert merged["screenshot_elements"] == extracted["elements"]


def test_chart_screenshot_merge_keeps_structured_series_in_sync() -> None:
    base = {
        "elements": [
            {
                "type": "chart",
                "title": "Sales trend",
                "categories": ["Week 1", "Week 2"],
                "rows": [["Week 1", 10], ["Week 2", 20]],
                "content": {
                    "chart_type": "line",
                    "categories": [
                        {"raw": "Week 1", "status": "read"},
                        {"raw": "Week 2", "status": "read"},
                    ],
                    "series": [
                        {
                            "name": "Sales",
                            "values": [
                                {"raw": 10, "status": "read"},
                                {"raw": 20, "status": "read"},
                            ],
                        }
                    ],
                    "legend": ["Sales"],
                },
            }
        ]
    }
    second_view = {
        "elements": [
            {
                "type": "chart",
                "title": "Sales trend",
                "categories": ["Week 2", "Week 3"],
                "rows": [["Week 2", 20], ["Week 3", 30]],
                "content": {
                    "chart_type": "line",
                    "categories": [
                        {"raw": "Week 2", "status": "read"},
                        {"raw": "Week 3", "status": "read"},
                    ],
                    "series": [
                        {
                            "name": "Sales",
                            "values": [
                                {"raw": 20, "status": "read"},
                                {"raw": 30, "status": "read"},
                            ],
                        }
                    ],
                    "legend": ["Sales"],
                },
            }
        ]
    }

    merged = browser._merge_extractions(base, second_view)

    assert merged["elements"][0]["categories"] == ["Week 1", "Week 2", "Week 3"]
    assert merged["elements"][0]["rows"] == [
        ["Week 1", 10],
        ["Week 2", 20],
        ["Week 3", 30],
    ]
    assert [
        item["raw"]
        for item in merged["elements"][0]["content"]["series"][0]["values"]
    ] == [10, 20, 30]


def test_chart_export_merge_updates_structured_categories_and_series() -> None:
    extracted = {
        "elements": [
            {
                "type": "chart",
                "title": "Sales trend",
                "categories": ["Week 1"],
                "rows": [["Week 1", 10]],
                "content": {
                    "chart_type": "line",
                    "categories": [{"raw": "Week 1", "status": "read"}],
                    "series": [
                        {
                            "name": "Sales",
                            "values": [{"raw": 10, "status": "read"}],
                        }
                    ],
                },
            }
        ]
    }
    exported = [
        {
            "type": "chart",
            "title": "Sales trend",
            "categories": ["Week", "Sales"],
            "values": [],
            "rows": [["Week 1", 10], ["Week 2", 20]],
        }
    ]

    merged = browser._merge_exported_elements(extracted, exported)

    assert [
        item["raw"] for item in merged["elements"][0]["content"]["categories"]
    ] == ["Week 1", "Week 2"]
    assert [
        item["raw"]
        for item in merged["elements"][0]["content"]["series"][0]["values"]
    ] == [10, 20]


def test_scroll_fallback_batches_horizontal_chart_views(monkeypatch, tmp_path) -> None:
    class ScrollButton:
        def __init__(self, page, direction):
            self.page = page
            self.direction = direction

        async def is_visible(self):
            return True

        async def is_enabled(self):
            return self.page.positions[self.direction] < 2

        async def click(self):
            self.page.positions[self.direction] += 1

        async def evaluate(self, _script):
            await self.click()

    class ScrollButtons:
        def __init__(self, page, direction):
            self.button = ScrollButton(page, direction)

        async def count(self):
            return 1

        def nth(self, _index):
            return self.button

    class Frame:
        def __init__(self, page):
            self.page = page

        def get_by_role(self, _role, name):
            direction = "Scroll down" if "down" in name.pattern else "Scroll right"
            return ScrollButtons(self.page, direction)

    class Mouse:
        async def move(self, *_args):
            pass

        async def wheel(self, *_args):
            pass

    class Page:
        def __init__(self):
            self.positions = {"Scroll right": 0, "Scroll down": 0}
            self.frames = [Frame(self)]
            self.viewport_size = {"width": 1280, "height": 720}
            self.mouse = Mouse()

        async def screenshot(self, path=None, full_page=False):
            content = "|".join(
                f"{direction}:{position}"
                for direction, position in self.positions.items()
            ).encode()
            if path is not None:
                Path(path).write_bytes(content)
            return content

        async def wait_for_timeout(self, *_args):
            pass

    calls = []

    async def extract_sequence(screenshots, page_name):
        calls.append((screenshots, page_name))
        return {"page": page_name, "elements": []}

    monkeypatch.setattr(browser, "extract_screenshots", extract_sequence)
    page = Page()

    result = asyncio.run(
        browser._extract_scrolled_views(
            page, "Report", {}, tmp_path, initial_screenshot=b"initial"
        )
    )

    assert result == {"page": "Report", "elements": []}
    assert len(calls) == 1
    assert len(calls[0][0]) == 5
    assert calls[0][1] == "Report"
    assert calls[0][0][0] == b"initial"


def test_no_scroll_buttons_means_no_mouse_wheel_scrolling(monkeypatch, tmp_path) -> None:
    wheel_calls = []

    class EmptyScrollButtons:
        async def count(self):
            return 0

    class Frame:
        def get_by_role(self, _role, name=None):
            return EmptyScrollButtons()

    class Mouse:
        async def move(self, *_args):
            pass

        async def wheel(self, *args):
            wheel_calls.append(args)

    class Page:
        def __init__(self):
            self.frames = [Frame()]
            self.viewport_size = {"width": 1280, "height": 720}
            self.mouse = Mouse()

        async def screenshot(self, path=None, full_page=False):
            return b"page"

        async def wait_for_timeout(self, *_args):
            pass

    calls = []

    async def extract_sequence(screenshots, page_name):
        calls.append((screenshots, page_name))
        return {"page": page_name, "elements": []}

    monkeypatch.setattr(browser, "extract_screenshots", extract_sequence)

    result = asyncio.run(
        browser._extract_scrolled_views(
            Page(), "Report", {}, tmp_path, initial_screenshot=b"initial"
        )
    )

    assert result == {"page": "Report", "elements": []}
    assert wheel_calls == []
    assert calls == [([b"initial"], "Report")]


def test_selection_check_treats_any_aggregate_label_as_select_all() -> None:
    assert browser._selection_matches("Select all", "All")
    assert browser._selection_matches("Select all", "(All)")
    assert browser._selection_matches("Benefits", "  benefits ")
    assert not browser._selection_matches("Benefits", "Finance")
    assert not browser._selection_matches("Select all", "Finance")


def test_empty_option_selection_uses_default_all_control_summary() -> None:
    selected = browser._selected_values_with_summary(
        [], "Case Type\nAll", ["Select all", "Workday Cases"]
    )

    assert selected == ["All"]


def test_all_checked_options_are_reported_as_default_all() -> None:
    options = ["Select all", "(Blank)", "Alona Florenz", "Amanda Stromgren"]

    selected = browser._selected_values_with_summary(
        options,
        "Supply Planner Hierarchy",
        options,
        control_type="checkbox_dropdown",
    )

    assert selected == ["All"]


def test_all_summary_without_aggregate_option_is_preserved() -> None:
    selected = browser._selected_values_with_summary(
        [], "Service Team\nAll", ["Benefits", "HRIS", "Learning"]
    )

    assert selected == ["All"]


def test_empty_option_selection_uses_single_value_control_summary() -> None:
    selected = browser._selected_values_with_summary(
        [], "Workday Cases", ["Select all", "Workday Cases"]
    )

    assert selected == ["Workday Cases"]


def test_baseline_filter_comparison_normalizes_powerbi_aggregate_labels(
    monkeypatch,
) -> None:
    async def current_filter_value(_page, _filter):
        return ["(All)"]

    monkeypatch.setattr(browser, "_current_filter_value", current_filter_value)

    matches = asyncio.run(
        browser._filters_match_baseline(
            SimpleNamespace(),
            [{"name": "Category", "selected": ["Select all"]}],
        )
    )

    assert matches


def test_baseline_comparison_accepts_cleared_selection_state(monkeypatch) -> None:
    async def current_filter_value(_page, _filter):
        return []

    monkeypatch.setattr(browser, "_current_filter_value", current_filter_value)

    matches = asyncio.run(
        browser._filters_match_baseline(
            SimpleNamespace(),
            [
                {
                    "name": "Time Period",
                    "selected": ["All"],
                    "values": ["2026", "2027"],
                    "reset_strategy": "clear_selection",
                }
            ],
        )
    )

    assert matches


def test_report_reset_restores_cleared_baseline_filters(monkeypatch) -> None:
    events = []

    class ResetButton:
        first = None

        async def count(self):
            return 1

        async def is_enabled(self):
            return True

        async def click(self):
            events.append("reset")

    reset_button = ResetButton()
    reset_button.first = reset_button

    class Confirmation:
        first = None

        async def wait_for(self, **_kwargs):
            raise browser.PlaywrightTimeoutError("not shown")

    confirmation = Confirmation()
    confirmation.first = confirmation

    class Frame:
        def get_by_role(self, role, name=None, exact=None):
            assert role == "button"
            if exact:
                return confirmation
            return reset_button

    class Page:
        frames = [Frame()]

        async def wait_for_timeout(self, _milliseconds):
            return None

    async def wait_for_ready(_page):
        return None

    async def restore_baseline(
        _page, baseline_filters, diagnostic_directory=None
    ):
        assert diagnostic_directory is None
        events.append(("restore", baseline_filters))
        return True

    monkeypatch.setattr(browser, "_wait_for_report_ready", wait_for_ready)
    monkeypatch.setattr(browser, "_restore_report_filters", restore_baseline)

    baseline = [{"name": "Time Period", "reset_strategy": "clear_selection"}]
    restored = asyncio.run(
        browser._reset_report_filters(
            Page(), required=True, baseline_filters=baseline
        )
    )

    assert restored
    assert events == ["reset", ("restore", baseline)]


def test_restore_report_filters_reapplies_clear_strategy(monkeypatch) -> None:
    events = []

    class ClearButton:
        async def click(self, **kwargs):
            assert kwargs == {"timeout": 5_000, "no_wait_after": True}
            events.append("clear")

    async def find_clear_button(_frame, _filter):
        return ClearButton()

    comparisons = iter([False, True, True])

    async def matches_baseline(_page, _filters):
        return next(comparisons)

    values = iter([[]])

    async def current_value(_page, _filter):
        return next(values)

    class Page:
        frames = [object()]

        async def wait_for_timeout(self, milliseconds):
            assert milliseconds == 150

    monkeypatch.setattr(browser, "_clear_selection_button", find_clear_button)
    monkeypatch.setattr(browser, "_filters_match_baseline", matches_baseline)
    monkeypatch.setattr(browser, "_current_filter_value", current_value)

    restored = asyncio.run(
        browser._restore_report_filters(
            Page(),
            [
                {
                    "name": "Time Period",
                    "selected": ["All"],
                    "values": ["2026", "2027"],
                    "reset_strategy": "clear_selection",
                }
            ],
        )
    )

    assert restored
    assert events == ["clear"]


def test_select_filter_keeps_default_all_without_clicking_aggregate(
    monkeypatch,
) -> None:
    async def current_filter_value(_page, _filter):
        return ["All"]

    monkeypatch.setattr(browser, "_current_filter_value", current_filter_value)

    asyncio.run(
        browser._select_filter(
            SimpleNamespace(),
            browser.FilterSelection(name="Case Type", value="Select all"),
            [
                {
                    "name": "Case Type",
                    "selected": ["All"],
                    "values": ["Select all", "Phone Call", "Email"],
                }
            ],
        )
    )


def test_reset_falls_back_to_restoring_checkbox_baseline(monkeypatch) -> None:
    selected = {"2026"}

    class NoResetButton:
        async def count(self):
            return 0

    class Frame:
        def get_by_role(self, role, name=None):
            assert role == "button"
            assert name.search("Reset filters, slicers")
            return NoResetButton()

    class Page:
        frames = [Frame()]

        async def wait_for_timeout(self, _ms):
            pass

    async def checkbox_options(_frame, filter_item):
        return [
            {
                "label": value,
                "selected": value in selected,
            }
            for value in ("2025", "2026")
        ]

    async def click_checkbox_option(_frame, _filter_item, value):
        if value in selected:
            selected.remove(value)
        else:
            selected.add(value)

    monkeypatch.setattr(browser, "_checkbox_filter_options", checkbox_options)
    monkeypatch.setattr(
        browser, "_click_checkbox_filter_option", click_checkbox_option
    )
    baseline = [
        {
            "name": "Fiscal Year",
            "selector_name": "Fiscal Year",
            "control_type": "checkbox",
            "selected": ["2025"],
            "values": ["2025", "2026"],
        }
    ]

    restored = asyncio.run(
        browser._reset_report_filters(
            Page(), required=True, baseline_filters=baseline
        )
    )

    assert restored
    assert selected == {"2025"}


def test_reset_falls_back_to_restoring_dropdown_baseline(monkeypatch) -> None:
    selected = {"2026"}

    class NoResetButton:
        async def count(self):
            return 0

    class EmptySelect:
        async def count(self):
            return 0

    class Combo:
        pass

    class ComboLocator:
        def evaluate_all(self, _script):
            async def result():
                return [
                    {
                        "index": 0,
                        "name": "Fiscal Year",
                        "selectorName": "Fiscal Year",
                    }
                ]
            return result()

        def nth(self, index):
            assert index == 0
            return Combo()

    class Frame:
        def locator(self, selector):
            assert selector == "select"
            return EmptySelect()

        def get_by_role(self, role, name=None, exact=None):
            if role == "button":
                return NoResetButton()
            assert role == "combobox"
            assert name is None
            assert exact is None
            return ComboLocator()

    class Page:
        frames = [Frame()]

        async def wait_for_timeout(self, _ms):
            pass

    async def current_filter_value(_page, _filter):
        return list(selected)

    async def click_filter_option(
        _page,
        _frame,
        _combo,
        value,
        *,
        filter_name=None,
        expected_options=None,
    ):
        if value in selected:
            selected.remove(value)
        else:
            selected.add(value)

    monkeypatch.setattr(browser, "_current_filter_value", current_filter_value)
    monkeypatch.setattr(browser, "_click_filter_option", click_filter_option)
    baseline = [
        {
            "name": "Fiscal Year",
            "selector_name": "Fiscal Year",
            "selected": ["2025"],
            "values": ["2025", "2026"],
        }
    ]

    restored = asyncio.run(
        browser._reset_report_filters(
            Page(), required=True, baseline_filters=baseline
        )
    )

    assert restored
    assert selected == {"2025"}


def test_invalid_screenshot_json_retries_with_smaller_sequence(monkeypatch) -> None:
    calls = []

    async def extract_sequence(screenshots, page_name):
        calls.append(len(screenshots))
        if len(screenshots) > 2:
            raise ValueError("screenshot extractor agent returned invalid JSON")
        values = [1, 2] if screenshots[0] == b"first" else [2, 3]
        return {
            "page": page_name,
            "elements": [{"type": "chart", "title": "Values", "values": values}],
        }

    monkeypatch.setattr(browser, "extract_screenshots", extract_sequence)

    result = asyncio.run(
        browser._extract_screenshot_sequence(
            [b"first", b"overlap", b"overlap", b"last"], "Report", {}
        )
    )

    assert calls == [4, 2, 2]
    assert result["elements"][0]["values"] == [1, 2, 3]


def test_max_tokens_response_retries_with_smaller_sequence(monkeypatch) -> None:
    calls = []

    async def extract_sequence(screenshots, page_name):
        calls.append(len(screenshots))
        if len(screenshots) > 1:
            raise browser.TruncatedAgentResponseError(
                "ADK agent response was truncated after reaching MAX_TOKENS"
            )
        return {
            "page": page_name,
            "elements": [
                {"type": "chart", "title": screenshots[0].decode("utf-8")}
            ],
        }

    monkeypatch.setattr(browser, "extract_screenshots", extract_sequence)

    result = asyncio.run(
        browser._extract_screenshot_sequence([b"first", b"last"], "Report", {})
    )

    assert calls == [2, 1, 1]
    assert [item["title"] for item in result["elements"]] == ["first", "last"]


def test_scroll_extractions_preserve_chart_values_and_merge_overlapping_rows() -> None:
    base = {
        "page": "Report",
        "elements": [
            {
                "type": "chart",
                "title": "Inventory",
                "categories": ["Week Ending", "Total"],
                "values": [7],
                "rows": [["1/1/2026", 7], ["Total", 7]],
            }
        ],
    }
    scrolled = {
        "page": "Report",
        "elements": [
            {
                "type": "chart",
                "title": "Inventory",
                "categories": ["Week Ending", "Total"],
                "values": [7, 7, 8],
                "rows": [
                    ["1/1/2026", 7.1],
                    ["1/8/2026", 7],
                    ["Total", 15],
                ],
            }
        ],
    }

    merged = browser._merge_extractions(base, scrolled)

    assert merged["elements"][0]["values"] == [7, 7, 8]
    assert merged["elements"][0]["rows"] == [
        ["1/1/2026", 7.1],
        ["Total", 15],
        ["1/8/2026", 7],
    ]
