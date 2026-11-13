import asyncio
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import ValidationError

from Backend import discovery
from Backend import api, browser, workflow
from Backend.api import CompareRequest, app
from fastapi.testclient import TestClient


WORKSPACE_A = "workspace-a"
WORKSPACE_B = "workspace-b"
REPORT_A = "report-a"
REPORT_B = "report-b"


def test_derive_gcp_names_normalizes_whitespace_and_applies_conventions(
    monkeypatch,
) -> None:
    assert discovery.derive_gcp_names("  Supply   Chain ", " Daywork ") == (
        "Tredence Supply Chain",
        "Daywork GCP",
    )
    assert discovery.derive_gcp_names(
        "Retail", "Inventory Insights", "Test ", " - QA"
    ) == ("Test Retail", "Inventory Insights - QA")
    monkeypatch.setenv(discovery.WORKSPACE_PREFIX_SETTING, "QA ")
    monkeypatch.setenv(discovery.REPORT_SUFFIX_SETTING, " - GCP")
    assert discovery.derive_gcp_names("Retail", "Daywork") == (
        "QA Retail",
        "Daywork - GCP",
    )


def test_powerbi_home_url_preserves_configured_tenant(monkeypatch) -> None:
    home_url = (
        "https://app.powerbi.com/home?ctid=tenant-example&experience=power-bi"
    )
    monkeypatch.setenv(discovery.POWERBI_HOME_URL_SETTING, home_url)
    configured_url = discovery._powerbi_home_url()

    assert configured_url == home_url
    with pytest.raises(discovery.DiscoveryError, match="POWERBI_HOME_URL"):
        discovery._powerbi_home_url("https://example.com/home")
    with pytest.raises(discovery.DiscoveryError, match="Home page"):
        discovery._powerbi_home_url("https://app.powerbi.com/groups")


def test_compare_request_requires_names_and_preserves_semantic_metadata() -> None:
    request = CompareRequest(
        workspace_name="  Supply   Chain ",
        semantic_model="  Supply   Model ",
        report_name=" Daywork ",
    )

    assert request.workspace_name == "Supply Chain"
    assert request.semantic_model == "  Supply   Model "
    assert request.report_name == "Daywork"
    assert CompareRequest(
        workspace_name="Supply Chain", report_name="Daywork"
    ).semantic_model == ""
    with pytest.raises(ValidationError):
        CompareRequest(workspace_name=" ", report_name="Daywork")


def test_form_exposes_three_column_batch_grid_and_one_live_progress_status() -> None:
    client = TestClient(app)
    html = client.get("/").text
    script = client.get("/static/app.js").text
    grid = re.search(r'<table id="batch-grid".*?</table>', html, re.DOTALL)
    assert grid is not None
    columns = re.findall(r"<th scope=\"col\"[^>]*>(.*?)</th>", grid.group())
    assert columns == ["Workspace Name", "Semantic Model", "Report Name"]
    assert '<th class="row-number-heading">S.No</th>' in grid.group()
    assert "name=\"url_a\"" not in html
    assert "name=\"url_b\"" not in html
    assert "Import Excel / CSV" not in html
    assert "Paste from Excel" not in html
    assert "import-preview" not in script
    assert "addPastedRows" not in script
    assert "Add row" in html
    assert "Delete row" in html
    assert "Clear all" in html
    assert "Validate inputs" in html
    assert "Run batch" in html
    assert "MAX_JOBS = 10" in script
    assert html.count('data-status="pending"') == 1
    assert 'id="current-stage-label"' in html
    assert 'id="progress-list"' not in html


def test_exact_candidates_deduplicates_same_asset_and_rejects_ambiguity() -> None:
    same = {"text": "Supply Chain", "href": f"/groups/{WORKSPACE_A}/list"}
    assert discovery._exact_candidates(
        [same, same], "Supply Chain", "Base workspace"
    ) == [same]

    with pytest.raises(discovery.DiscoveryError, match="Multiple exact"):
        discovery._exact_candidates(
            [
                same,
                {"text": "Supply Chain", "href": f"/groups/{WORKSPACE_B}/list"},
            ],
            "Supply Chain",
            "Base workspace",
        )


def test_asset_ids_accept_only_powerbi_host() -> None:
    assert discovery._workspace_id(
        "https://app.powerbi.com/groups/workspace-id/list"
    ) == "workspace-id"
    assert discovery._workspace_id(
        "https://example.com/groups/workspace-id/list"
    ) is None
    assert discovery._report_ids(
        "https://app.powerbi.com/groups/workspace-id/reports/report-id"
    ) == ("workspace-id", "report-id")
    assert discovery._report_ids(
        "https://example.com/groups/workspace-id/reports/report-id"
    ) is None


class _FakeLocator:
    def __init__(self, page, selector, index=None):
        self.page = page
        self.selector = selector
        self.index = index

    async def evaluate_all(self, _script):
        if self.selector == 'a[href], [role="link"][href]':
            return self.page.links
        assert self.selector == '[data-testid="workspace-item-btn"]'
        return self.page.workspace_buttons

    async def inner_text(self):
        assert self.selector == "body"
        return self.page.body

    async def fill(self, value, **_kwargs):
        if self.selector == 'input[aria-label="Workspace search"]':
            self.page.search_query = value
            return
        assert self.selector == 'input[placeholder="Filter by keyword"]'
        self.page.report_query = value

    async def click(self, **_kwargs):
        if self.selector == "role:menuitem:Workspaces":
            self.page.workspace_menu_open = True
            return
        assert self.selector == '[data-testid="workspace-item-btn"]'
        selected = next(
            button
            for button in self.page.workspace_buttons
            if button["index"] == self.index
        )
        self.page.url = (
            f"https://app.powerbi.com/groups/{selected['workspace_id']}/list"
        )

    def nth(self, index):
        return _FakeLocator(self.page, self.selector, index)


class _FakePage:
    def __init__(self, scenario):
        self.scenario = scenario
        self.url = "about:blank"
        self.link_reads = 0
        self.report_reads = 0
        self.workspace_menu_open = False
        self.search_query = ""
        self.report_query = ""
        self.body = (
            "You don't have access to this report"
            if scenario == "access_denied"
            else "Power BI"
        )

    def locator(self, selector):
        return _FakeLocator(self, selector)

    def get_by_role(self, role, name, exact):
        assert (role, name, exact) == ("menuitem", "Workspaces", True)
        return _FakeLocator(self, "role:menuitem:Workspaces")

    async def wait_for_timeout(self, _milliseconds):
        pass

    async def goto(self, url, **_kwargs):
        if self.scenario == "navigation_timeout":
            raise PlaywrightTimeoutError("timed out")
        if (
            self.scenario == "authentication_expired"
            and url == discovery.DEFAULT_POWERBI_HOME_URL
        ):
            self.url = "https://login.microsoftonline.com/"
            return
        self.url = url

    @property
    def links(self):
        workspace_id = discovery._workspace_id(self.url)
        if workspace_id == WORKSPACE_A:
            self.report_reads += 1
            if (
                self.scenario == "delayed_base_report"
                and self.report_reads < 3
            ):
                return []
            reports = [
                {
                    "text": "Daywork",
                    "href": f"https://app.powerbi.com/groups/{WORKSPACE_A}/reports/{REPORT_A}",
                }
            ]
            if self.scenario == "missing_base_report":
                return []
            if self.scenario == "duplicate_report":
                reports.append(
                    {
                        "text": "Daywork",
                        "href": f"https://app.powerbi.com/groups/{WORKSPACE_A}/reports/report-duplicate",
                    }
                )
            return [
                report
                for report in reports
                if self.report_query.casefold() in report["text"].casefold()
            ]
        if workspace_id == WORKSPACE_B:
            if self.scenario == "missing_gcp_report":
                return []
            reports = [
                {
                    "text": "Daywork GCP",
                    "href": f"https://app.powerbi.com/groups/{WORKSPACE_B}/reports/{REPORT_B}",
                }
            ]
            return [
                report
                for report in reports
                if self.report_query.casefold() in report["text"].casefold()
            ]
        return []

    @property
    def workspace_buttons(self):
        if not self.workspace_menu_open or not self.search_query:
            return []
        self.link_reads += 1
        if (
            self.scenario == "delayed_workspace_list"
            and self.link_reads < 3
        ):
            return []
        workspaces = [
            {"text": "Supply Chain", "workspace_id": WORKSPACE_A},
            {"text": "Tredence Supply Chain", "workspace_id": WORKSPACE_B},
        ]
        if self.scenario == "missing_base_workspace":
            workspaces = workspaces[1:]
        elif self.scenario == "missing_gcp_workspace":
            workspaces = workspaces[:1]
        elif self.scenario == "duplicate_workspace":
            workspaces.append(
                {"text": "Supply Chain", "workspace_id": "workspace-duplicate"}
            )
        return [
            {"index": index, "labels": [item["text"]], "workspace_id": item["workspace_id"]}
            for index, item in enumerate(workspaces)
            if self.search_query.casefold() in item["text"].casefold()
        ]


class _FakeContext:
    def __init__(self, scenario):
        self.scenario = scenario

    async def new_page(self):
        return _FakePage(self.scenario)

    async def close(self):
        pass


class _FakeBrowser:
    def __init__(self, scenario):
        self.scenario = scenario

    async def new_context(self, **_kwargs):
        return _FakeContext(self.scenario)

    async def close(self):
        pass


class _FakeChromium:
    def __init__(self, scenario):
        self.scenario = scenario

    async def launch(self, **_kwargs):
        return _FakeBrowser(self.scenario)


class _FakePlaywright:
    def __init__(self, scenario):
        self.chromium = _FakeChromium(scenario)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass


def _install_fake_playwright(monkeypatch, scenario):
    monkeypatch.setattr(
        discovery, "async_playwright", lambda: _FakePlaywright(scenario)
    )

    async def goto_report(page, url):
        await page.goto(url)

    async def report_ready(_page):
        pass

    async def report_title(page):
        report_ids = discovery._report_ids(page.url)
        if (
            page.scenario == "wrong_report_title"
            and report_ids
            and report_ids[1] == REPORT_A
        ):
            return "Unexpected report"
        return {
            REPORT_A: "Daywork",
            REPORT_B: "Daywork GCP",
            "report-duplicate": "Daywork",
        }.get(report_ids[1]) if report_ids else None

    monkeypatch.setattr(discovery, "_goto_report", goto_report)
    monkeypatch.setattr(discovery, "_wait_for_report_ready", report_ready)
    monkeypatch.setattr(discovery, "_read_report_title", report_title)


def test_discovery_finds_and_verifies_both_reports(monkeypatch, tmp_path) -> None:
    _install_fake_playwright(monkeypatch, "success")
    progress = []

    result = asyncio.run(
        discovery.discover_dashboard_pair(
            "Supply Chain",
            "Supply Model",
            "Daywork",
            tmp_path / "auth.json",
            on_progress=lambda *update: progress.append(update),
        )
    )

    assert result.discovery == {
        "inputs": {
            "workspace_name": "Supply Chain",
            "semantic_model": "Supply Model",
            "report_name": "Daywork",
        },
        "dashboard_a": {
            "environment": "Snowflake",
            "workspace_name": "Supply Chain",
            "report_name": "Daywork",
            "workspace_id": WORKSPACE_A,
            "report_id": REPORT_A,
            "report_url": f"https://app.powerbi.com/groups/{WORKSPACE_A}/reports/{REPORT_A}",
            "status": "discovered",
        },
        "dashboard_b": {
            "environment": "GCP",
            "workspace_name": "Tredence Supply Chain",
            "report_name": "Daywork GCP",
            "workspace_id": WORKSPACE_B,
            "report_id": REPORT_B,
            "report_url": f"https://app.powerbi.com/groups/{WORKSPACE_B}/reports/{REPORT_B}",
            "status": "discovered",
        },
        "discovery_status": "success",
    }
    assert discovery._report_ids(result.url_a) == (WORKSPACE_A, REPORT_A)
    assert discovery._report_ids(result.url_b) == (WORKSPACE_B, REPORT_B)
    assert [status for _, status, _ in progress] == [
        "in_progress",
        "completed",
        "in_progress",
        "completed",
        "in_progress",
        "completed",
        "in_progress",
        "completed",
        "in_progress",
        "completed",
        "in_progress",
        "completed",
        "in_progress",
        "completed",
    ]


def test_discovery_runs_both_dashboard_targets_concurrently(
    monkeypatch, tmp_path
) -> None:
    _install_fake_playwright(monkeypatch, "success")
    original_find_workspace = discovery._find_workspace
    workspace_pages = []
    both_targets_started = asyncio.Event()

    async def verify_concurrent_workspace_discovery(page, name, environment):
        workspace_pages.append(page)
        if len(workspace_pages) == 2:
            both_targets_started.set()
        await asyncio.wait_for(both_targets_started.wait(), timeout=1)
        return await original_find_workspace(page, name, environment)

    monkeypatch.setattr(
        discovery, "_find_workspace", verify_concurrent_workspace_discovery
    )
    result = asyncio.run(
        discovery.discover_dashboard_pair(
            "Supply Chain",
            "",
            "Daywork",
            tmp_path / "auth.json",
        )
    )

    assert len(workspace_pages) == 2
    assert workspace_pages[0] is not workspace_pages[1]
    assert result.discovery["discovery_status"] == "success"


def test_discovery_waits_for_powerbi_workspace_list(monkeypatch, tmp_path) -> None:
    _install_fake_playwright(monkeypatch, "delayed_workspace_list")

    result = asyncio.run(
        discovery.discover_dashboard_pair(
            "Supply Chain",
            "",
            "Daywork",
            tmp_path / "auth.json",
        )
    )

    assert result.discovery["dashboard_a"]["workspace_id"] == WORKSPACE_A
    assert result.discovery["dashboard_b"]["workspace_id"] == WORKSPACE_B


def test_discovery_waits_for_powerbi_report_list(monkeypatch, tmp_path) -> None:
    _install_fake_playwright(monkeypatch, "delayed_base_report")

    result = asyncio.run(
        discovery.discover_dashboard_pair(
            "Supply Chain",
            "",
            "Daywork",
            tmp_path / "auth.json",
        )
    )

    assert result.discovery["dashboard_a"]["report_id"] == REPORT_A
    assert result.discovery["dashboard_b"]["report_id"] == REPORT_B


def test_api_passes_discovered_urls_and_shared_browser_to_pipeline(
    monkeypatch, tmp_path
) -> None:
    job_id = "discovery-api-test"
    progress = [
        {"step": name, "status": "pending", "message": None}
        for name in api.PROGRESS_STEPS
    ]
    api.JOBS[job_id] = {"progress": progress}
    monkeypatch.setattr(api, "OUTPUT_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(api, "configure_vertex_ai", lambda: None)
    browser = _FakeBrowser("success")

    class _PlaywrightManager:
        def __init__(self):
            class Chromium:
                async def launch(self, **_kwargs):
                    return browser

            self.chromium = Chromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

    calls = {}

    async def discover(**kwargs):
        calls["discovery_browser"] = kwargs["browser"]
        calls["semantic_model"] = kwargs["semantic_model"]
        for step in range(2, 9):
            kwargs["on_progress"](step, "completed", None)
        return discovery.DashboardPairDiscovery(
            discovery={
                "inputs": {
                    "workspace_name": kwargs["workspace_name"],
                    "semantic_model": kwargs["semantic_model"],
                    "report_name": kwargs["report_name"],
                },
                "dashboard_a": {"workspace_name": "Supply Chain"},
                "dashboard_b": {
                    "workspace_name": "Tredence Supply Chain"
                },
                "discovery_status": "success",
            },
            url_a="https://app.powerbi.com/groups/a/reports/report-a",
            url_b="https://app.powerbi.com/groups/b/reports/report-b",
        )

    async def compare(**kwargs):
        calls["comparison_browser"] = kwargs["browser"]
        calls["urls"] = (kwargs["url_a"], kwargs["url_b"])
        calls["report_metadata"] = kwargs["report_metadata"]
        for step in range(9, 12):
            kwargs["progress_callback"](step, "in_progress", None)
            kwargs["progress_callback"](step, "completed", None)
        return {"summary": {"matches": 1}}

    monkeypatch.setattr(api, "async_playwright", _PlaywrightManager)
    monkeypatch.setattr(api, "discover_dashboard_pair", discover)
    monkeypatch.setattr(api, "run_dashboard_comparison", compare)

    try:
        summary = asyncio.run(
            api._execute_comparison(
                job_id,
                CompareRequest(
                    workspace_name="Supply Chain",
                    semantic_model="Supply Model",
                    report_name="Daywork",
                ),
                tmp_path / "auth.json",
            )
        )
        assert summary == {"matches": 1}
        assert calls["discovery_browser"] is browser
        assert calls["comparison_browser"] is browser
        assert calls["semantic_model"] == "Supply Model"
        assert calls["report_metadata"]["inputs"]["semantic_model"] == "Supply Model"
        assert (
            calls["report_metadata"]["dashboard_b"]["workspace_name"]
            == "Tredence Supply Chain"
        )
        assert calls["urls"] == (
            "https://app.powerbi.com/groups/a/reports/report-a",
            "https://app.powerbi.com/groups/b/reports/report-b",
        )
        discovery_path = tmp_path / "jobs" / job_id / "discovery.json"
        assert discovery_path.is_file()
        assert progress[0]["status"] == "completed"
        assert [step["status"] for step in progress[1:]] == [
            "completed"
        ] * 10
    finally:
        api.JOBS.pop(job_id, None)


def test_workflow_forwards_shared_browser_to_existing_capture(
    monkeypatch, tmp_path
) -> None:
    shared_browser = object()
    captured = {}

    async def capture(**kwargs):
        captured.update(kwargs)
        return [], []

    monkeypatch.setattr(workflow, "capture_dashboard_pair", capture)
    run = workflow.ComparisonWorkflowRun(
        job_id="shared-browser-test",
        url_a="https://app.powerbi.com/groups/a/reports/a",
        url_b="https://app.powerbi.com/groups/b/reports/b",
        output_dir=tmp_path,
        storage_state=tmp_path / "auth.json",
        browser=shared_browser,
    )

    asyncio.run(run._capture())

    assert captured["browser"] is shared_browser


def test_capture_browser_session_reuses_shared_browser(monkeypatch, tmp_path) -> None:
    shared_browser = object()

    async def reuse_browser():
        async with browser._capture_browser_session(
            shared_browser,
            False,
            "shared-browser-test",
            tmp_path / "timings.jsonl",
            "dashboard_A",
        ) as actual_browser:
            assert actual_browser is shared_browser

    asyncio.run(reuse_browser())


@pytest.mark.parametrize(
    ("scenario", "message"),
    [
        ("missing_base_workspace", "Base/Snowflake workspace"),
        ("missing_base_report", "Daywork.*not found"),
        ("missing_gcp_workspace", "GCP workspace"),
        ("missing_gcp_report", "Daywork GCP.*not found"),
        ("duplicate_workspace", "Multiple exact"),
        ("duplicate_report", "Multiple exact"),
        ("authentication_expired", "authentication expired"),
        ("access_denied", "denied access"),
        ("wrong_report_title", "expected the exact title"),
        ("navigation_timeout", "Navigation timed out"),
    ],
)
def test_discovery_stops_with_actionable_errors(
    monkeypatch, tmp_path, scenario, message
) -> None:
    _install_fake_playwright(monkeypatch, scenario)
    progress = []

    with pytest.raises(discovery.DiscoveryError, match=message):
        asyncio.run(
            discovery.discover_dashboard_pair(
                "Supply Chain",
                "",
                "Daywork",
                tmp_path / "auth.json",
                on_progress=lambda *update: progress.append(update),
            )
        )
    assert any(status == "failed" for _, status, _ in progress)
