import asyncio
import base64
import io
import logging
import os
import zipfile
from fastapi.testclient import TestClient
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from openpyxl import Workbook
from pydantic import ValidationError

from Backend import api
from Backend.api import JOBS, app


def test_job_output_retention_keeps_twenty_newest_directories(
    tmp_path, monkeypatch
) -> None:
    output_root = tmp_path / "jobs"
    monkeypatch.setattr(api, "OUTPUT_ROOT", output_root)
    for index in range(21):
        job_dir = output_root / f"old-{index}"
        job_dir.mkdir(parents=True)
        os.utime(job_dir, (index + 1, index + 1))
    (output_root / "unrelated.txt").write_text("keep")

    current_dir = api._prepare_job_output("current")

    assert current_dir.is_dir()
    assert {
        path.name for path in output_root.iterdir() if path.is_dir()
    } == {"current"} | {f"old-{index}" for index in range(2, 21)}
    assert (output_root / "unrelated.txt").read_text() == "keep"


def test_job_output_retention_handles_locked_directory_without_failing(
    tmp_path, monkeypatch
) -> None:
    output_root = tmp_path / "jobs"
    monkeypatch.setattr(api, "OUTPUT_ROOT", output_root)
    for index in range(21):
        job_dir = output_root / f"old-{index}"
        job_dir.mkdir(parents=True)
        os.utime(job_dir, (index + 1, index + 1))

    original_rmtree = api.shutil.rmtree

    def locked_rmtree(path, *args, **kwargs):
        if Path(path).name == "old-0":
            raise PermissionError("[WinError 32] The process cannot access the file because it is being used by another process: 'Dashboard_Comparison.docx'")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(api.shutil, "rmtree", locked_rmtree)

    current_dir = api._prepare_job_output("current")

    assert current_dir.is_dir()
    assert (output_root / "old-0").is_dir()


def test_job_output_folder_uses_first_report_name_and_updates_paths(
    tmp_path, monkeypatch
) -> None:
    output_root = tmp_path / "jobs"
    monkeypatch.setattr(api, "OUTPUT_ROOT", output_root)
    job_id = str(uuid4())
    output_dir = output_root / job_id
    output_dir.mkdir(parents=True)
    (output_dir / "comparison.json").write_text("{}")
    JOBS[job_id] = {"output_dir": str(output_dir)}
    result = {
        "screenshot_evidence": {
            "dashboard_a": {
                "page|initial": {
                    "report_title": "Quarterly / Sales",
                    "path": str(output_dir / "screenshot.png"),
                }
            },
            "dashboard_b": {
                "page|initial": {
                    "report_title": "Other Report",
                    "path": str(output_dir / "other.png"),
                }
            },
        },
        "summary": {},
    }

    try:
        named_output_dir = api._finalize_job_output(job_id, output_dir, result)

        assert named_output_dir.name == f"Quarterly _ Sales_{job_id}"
        assert api._job_output_dir(job_id) == named_output_dir
        assert result["screenshot_evidence"]["dashboard_a"]["page|initial"][
            "path"
        ] == str(named_output_dir / "screenshot.png")
        assert result["screenshot_evidence"]["dashboard_b"]["page|initial"][
            "path"
        ] == str(named_output_dir / "other.png")
        assert (named_output_dir / "comparison.json").is_file()
    finally:
        JOBS.pop(job_id, None)


def test_scenario_enabled_job_uses_existing_comparator_after_capture(
    tmp_path, monkeypatch
) -> None:
    job_id = str(uuid4())
    output_root = tmp_path / "jobs"
    monkeypatch.setattr(api, "OUTPUT_ROOT", output_root)
    monkeypatch.setattr(api, "configure_vertex_ai", lambda: None)
    job = {
        "progress": [
            {"step": name, "status": "pending", "message": None}
            for name in api.PROGRESS_STEPS
        ]
    }
    monkeypatch.setattr(api, "JOBS", {job_id: job})
    workflow_events = []

    class FakeBrowser:
        async def close(self):
            return None

    browser = FakeBrowser()

    class FakeChromium:
        async def launch(self, *, headless):
            assert headless is True
            return browser

    class FakePlaywright:
        chromium = FakeChromium()

    class FakePlaywrightContext:
        async def __aenter__(self):
            return FakePlaywright()

        async def __aexit__(self, *_args):
            return None

    async def fake_discover_pair(**_kwargs):
        workflow_events.append("discover_dashboards")
        return SimpleNamespace(
            url_a="https://app.powerbi.com/reports/base",
            url_b="https://app.powerbi.com/reports/compare",
            discovery={"report": "Inventory Insights"},
        )

    async def fake_discover_pages(**kwargs):
        workflow_events.append("discover_scenario_pages")
        assert kwargs["browser"] is browser
        return ["All Items"]

    async def fake_validation_scenarios(**kwargs):
        workflow_events.append("capture_all_scenarios")
        assert kwargs["page_names"] == ["All Items"]
        for dashboard_id in ("dashboard_A", "dashboard_B"):
            page_dir = kwargs["destination"] / dashboard_id / "All Items"
            page_dir.mkdir(parents=True, exist_ok=True)
            for scenario_id in ("01", "02", "03", "04"):
                (page_dir / f"S{scenario_id}_Page01.png").write_bytes(
                    f"{dashboard_id}-{scenario_id}".encode()
                )
        return {"pages": [{"page": "All Items"}]}

    async def fake_scenario_state_adapter(**kwargs):
        workflow_events.append("validate_and_adapt_scenario_evidence")
        assert kwargs["execution"] == {"pages": [{"page": "All Items"}]}
        return [], [], {
            "execution": kwargs["execution"],
            "evidence_validation": {"status": "VALIDATED", "issue_count": 0},
        }

    async def fake_dashboard_comparison(**kwargs):
        output = kwargs["output_dir"]
        workflow_events.append("existing_comparator")
        assert kwargs["generate_report"] is True
        assert kwargs["scenario_state_loader"] is not None
        assert all(
            len(list((output / dashboard_id / "All Items").glob("*.png"))) == 4
            for dashboard_id in ("dashboard_A", "dashboard_B")
        )
        states_a, states_b, context = await kwargs["scenario_state_loader"]()
        assert states_a == states_b == []
        assert context["execution"] == {"pages": [{"page": "All Items"}]}
        kwargs["progress_callback"](9, "completed")
        kwargs["progress_callback"](10, "completed")
        kwargs["progress_callback"](13, "in_progress")
        (output / "Dashboard_Comparison.docx").write_bytes(b"standard docx")
        (output / "Dashboard_Comparison.xlsx").write_bytes(b"standard xlsx")
        (output / "validation_summary.json").write_text(
            '{"pages":[{"page":"All Items"}]}', encoding="utf-8"
        )
        kwargs["progress_callback"](13, "completed")
        return {
            "summary": {
                "matches": 1,
                "validation_scenarios": context["execution"],
            },
            "validation_scenarios": context,
            "screenshot_evidence": {
                "dashboard_a": {
                    "initial": {"report_title": "Inventory Insights"}
                }
            },
        }

    monkeypatch.setattr(api, "async_playwright", FakePlaywrightContext)
    monkeypatch.setattr(api, "discover_dashboard_pair", fake_discover_pair)
    monkeypatch.setattr(api, "run_dashboard_comparison", fake_dashboard_comparison)
    monkeypatch.setattr(
        "Backend.validation_scenarios.discover_scenario_pages", fake_discover_pages
    )
    monkeypatch.setattr(
        "Backend.validation_scenarios.run_validation_scenarios",
        fake_validation_scenarios,
    )
    monkeypatch.setattr(
        "Backend.scenario_state_adapter.load_scenario_states",
        fake_scenario_state_adapter,
    )

    summary = asyncio.run(
        api._execute_comparison(
            job_id,
            api.CompareRequest(
                workspace_name="Supply Chain",
                report_name="Inventory Insights",
                run_validation_scenarios=True,
            ),
            Path("auth.json"),
        )
    )

    assert workflow_events == [
        "discover_dashboards",
        "discover_scenario_pages",
        "capture_all_scenarios",
        "existing_comparator",
        "validate_and_adapt_scenario_evidence",
    ]
    output_dir = output_root / job_id
    assert job["output_dir"] == str(output_dir)
    assert (output_dir / "comparison.json").is_file()
    assert summary["validation_scenarios"] == {"pages": [{"page": "All Items"}]}
    assert not (output_dir / "comparison_results").exists()
    assert [step["status"] for step in job["progress"][-3:]] == [
        "completed",
        "completed",
        "completed",
    ]
    assert set(api._available_job_outputs(job_id)) == {
        "docx",
        "xlsx",
        "validation_json",
    }
    client = TestClient(app)
    assert client.get(
        f"/api/jobs/{job_id}/scenario-comparison.docx"
    ).status_code == 404
    assert client.get(
        f"/api/jobs/{job_id}/scenario-comparison.json"
    ).status_code == 404
    assert client.get(
        f"/api/jobs/{job_id}/evidence-validation.json"
    ).status_code == 404
    assert client.get(
        f"/api/jobs/{job_id}/validation-summary.json"
    ).status_code == 200

    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        assert api._write_job_archive(
            archive,
            job_id,
            {
                **job,
                "report_name": "Inventory Insights",
                "dashboard_a_url": "https://app.powerbi.com/reports/base",
                "dashboard_b_url": "https://app.powerbi.com/reports/compare",
            },
            "Inventory Insights",
        )
    with zipfile.ZipFile(io.BytesIO(archive_buffer.getvalue())) as archive:
        names = archive.namelist()
    assert "Inventory Insights/Validation_Execution_Summary.json" in names


def test_locked_job_output_folder_keeps_uuid_folder_without_failing(
    tmp_path, monkeypatch
) -> None:
    output_root = tmp_path / "jobs"
    monkeypatch.setattr(api, "OUTPUT_ROOT", output_root)
    job_id = str(uuid4())
    output_dir = output_root / job_id
    output_dir.mkdir(parents=True)
    comparison_path = output_dir / "comparison.json"
    comparison_path.write_text("{}", encoding="utf-8")
    JOBS[job_id] = {"output_dir": str(output_dir)}
    original_rename = Path.rename

    def deny_job_folder_rename(path, target):
        if path == output_dir:
            raise PermissionError("directory is locked")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", deny_job_folder_rename)
    result = {
        "screenshot_evidence": {
            "dashboard_a": {
                "state": {
                    "report_title": "Quarterly Sales",
                    "path": str(output_dir / "screenshot.png"),
                }
            }
        }
    }

    try:
        final_output_dir = api._finalize_job_output(job_id, output_dir, result)

        assert final_output_dir == output_dir
        assert api._job_output_dir(job_id) == output_dir
        assert comparison_path.read_text(encoding="utf-8") == "{}"
    finally:
        JOBS.pop(job_id, None)


def test_ui_and_runtime_config_endpoints() -> None:
    client = TestClient(app)

    page = client.get("/")
    config = client.get("/api/config")

    page_text = (
        page.text
        + client.get("/static/app.js").text
        + client.get("/static/styles.css").text
    )
    page = type("Combined", (), {"status_code": page.status_code, "text": page_text})
    assert "${config.project_id}" not in page.text
    assert 'id="execution-time"' in page.text
    assert "function renderElapsed()" in page.text
    assert "Batch elapsed:" in page.text
    assert "Batch time:" in page.text
    assert "Comparison timed out after 60 seconds." not in page.text
    assert "Open interactive results" not in page.text
    assert "overflow: hidden" in page.text
    assert "Dashboard Comparison" in page.text
    assert "POWER BI · SNOWFLAKE VS GCP" in page.text
    assert 'class="heading-card"' in page.text
    assert "width: min(100%, 380px)" in page.text
    assert "border-radius: 12px" in page.text
    assert "background: linear-gradient(140deg, #edf7e9, #dcefd8)" in page.text
    assert "box-shadow: 0 3px 8px rgba(33, 74, 55, .08), 0 8px 18px rgba(33, 74, 55, .1)" in page.text
    assert "border: 2px solid var(--forest-dark)" in page.text
    assert page.text.count("border: 2px solid var(--forest-dark)") == 2
    assert "Workspace Name" in page.text
    assert "Semantic Model" in page.text
    assert "Report Name" in page.text
    assert 'class="brand-logo brand-logo--cs"' in page.text
    assert page.text.count('class="brand-logo"') == 1
    assert "CS_Wholesale_Grocers_logo.svg" in page.text
    assert "SpartanNash-Company-Logo-260x63.png" in page.text
    assert ".brand-logos {\n  display: inline-flex;\n  flex: 0 0 auto;\n  align-items: center;\n  gap: 0;" in page.text.replace("\r\n", "\n")
    assert ".brand-logo:not(.brand-logo--cs) {\n  margin-left: -18px;" in page.text.replace("\r\n", "\n")
    assert "background: transparent" in page.text
    assert "border: 0" in page.text
    assert "mix-blend-mode: multiply" in page.text
    assert "width: 156px" in page.text
    assert "height: 40px" in page.text
    assert "width: 210px" in page.text
    assert "height: 50px" in page.text
    assert "padding-top: 4px" in page.text
    assert "transform: translateY(8px)" in page.text
    assert '<span>Dashboard <strong>Comparison</strong></span>' not in page.text
    assert page.text.count("class=\"workflow-art-image ") == 13
    assert page.text.count("/static/playwright-logo-transparent.png") == 2
    assert page.text.count("/static/powerbi-symbol-additional.png") == 1
    assert page.text.count("/static/gemini-logo-ai-tower.png") == 1
    assert page.text.count("/static/ai-brain-circuit.png") == 2
    assert page.text.count("/static/microsoft-word-icon.png") == 1
    assert "m.media-amazon.com/images/S/aplus-media-library-service-media/" not in page.text
    assert page.text.count("Microsoft-Power-BI-Symbol-700x394.png") == 2
    assert page.text.count("google-gemini-logo-png-transparent.png") == 2
    assert "Microsoft-Power-BI-Logo-2013.png" not in page.text
    assert "google-gemini_LOGO.png" not in page.text
    assert page.text.count("Agentic-AI-Newgen.png") == 2
    assert "mask-image: radial-gradient" in page.text
    assert "opacity: .3" in page.text
    assert "filter: saturate(.75) contrast(1.02)" in page.text
    assert ".workflow-art-image--word-icon {\n  top: 58%;\n  right: 0;" in page.text.replace("\r\n", "\n")
    playwright_asset = client.get("/static/playwright-logo-transparent.png")
    assert playwright_asset.status_code == 200
    assert playwright_asset.headers["content-type"].startswith("image/png")
    symbol_asset = client.get("/static/powerbi-symbol-additional.png")
    assert symbol_asset.status_code == 200
    assert symbol_asset.headers["content-type"].startswith("image/png")
    gemini_asset = client.get("/static/gemini-logo-ai-tower.png")
    assert gemini_asset.status_code == 200
    assert gemini_asset.headers["content-type"].startswith("image/png")
    brain_asset = client.get("/static/ai-brain-circuit.png")
    assert brain_asset.status_code == 200
    assert brain_asset.headers["content-type"].startswith("image/png")
    word_asset = client.get("/static/microsoft-word-icon.png")
    assert word_asset.status_code == 200
    assert word_asset.headers["content-type"].startswith("image/png")
    assert "Hide input sheet" in page.text
    assert 'aria-controls="configuration-content"' in page.text
    assert '<th class="row-number-heading">S.No</th>' in page.text
    assert 'url("/static/powerbi-wallpaper.jpg")' not in page.text
    assert "--yellow: #f6f3e7" in page.text
    assert "background: var(--yellow)" in page.text
    assert "width: 60%" in page.text
    assert "grid-template-columns: minmax(0, 1fr)" in page.text
    assert "@media (max-width: 1100px)" in page.text
    assert 'id="batch-grid"' in page.text
    assert "max-height: 261px" in page.text
    assert "position: sticky" in page.text
    assert "<th scope=\"col\">Workspace Name</th>" in page.text
    assert "<th scope=\"col\">Semantic Model</th>" in page.text
    assert "<th scope=\"col\">Report Name</th>" in page.text
    assert "Import Excel / CSV" not in page.text
    assert "Paste from Excel" not in page.text
    assert "Run batch" in page.text
    assert 'id="run-validation-scenarios"' in page.text
    assert 'id="run-validation-scenarios" type="checkbox" checked' in page.text
    assert "/api/batches/import-preview" not in page.text
    assert 'fetch("/api/batches"' in page.text
    assert "stop_on_first_failure" in page.text
    assert "Download all report folders" in page.text
    assert "Dashboard A" in page.text
    assert "Dashboard B" in page.text
    assert "DOCX" in page.text
    assert "/api/batches/${batch.batch_id}/outputs.zip" in page.text
    assert "/api/jobs/${job.job_id}/outputs.zip" in page.text
    assert 'id="batch-summary-download"' not in page.text
    assert "/api/jobs/${job.job_id}/comparison.json" not in page.text
    assert "/api/jobs/${job.job_id}/discovery.json" not in page.text
    assert "skipped" in page.text
    assert "name=\"url_a\"" not in page.text
    assert "name=\"url_b\"" not in page.text
    assert config.status_code == 200
    assert config.json()["model"] == "gemini-3.8-flash"


def test_job_status_includes_final_elapsed_seconds() -> None:
    client = TestClient(app)
    job_id = str(uuid4())
    JOBS[job_id] = {
        "status": "completed",
        "started_monotonic": None,
        "elapsed_seconds": 97.25,
        "summary": {},
        "error": None,
    }
    try:
        response = client.get(f"/api/jobs/{job_id}")
    finally:
        JOBS.pop(job_id, None)

    assert response.status_code == 200
    assert response.json()["elapsed_seconds"] == 97.25


def test_comparison_records_execution_time_over_sixty_seconds(monkeypatch) -> None:
    job_id = str(uuid4())
    JOBS[job_id] = {
        "status": "queued",
        "started_monotonic": None,
        "elapsed_seconds": 0.0,
        "summary": None,
        "error": None,
    }
    api.ACTIVE_JOB_ID = job_id
    elapsed = [0.0]

    async def slow_comparison(*_args):
        await asyncio.sleep(0)
        elapsed[0] += 61
        return {"matches": 1}

    monkeypatch.setattr(api.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(api, "_execute_comparison", slow_comparison)
    try:
        asyncio.run(
            api._run_comparison(
                job_id,
                api.CompareRequest(
                    workspace_name="Supply Chain",
                    report_name="Daywork",
                ),
                Path("auth.json"),
            )
        )
        assert JOBS[job_id]["status"] == "completed"
        assert JOBS[job_id]["summary"] == {"matches": 1}
        assert JOBS[job_id]["elapsed_seconds"] == 61
        assert api.ACTIVE_JOB_ID is None
    finally:
        JOBS.pop(job_id, None)


def test_api_requires_workspace_and_report_names() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/compare",
        json={
            "workspace_name": " ",
            "report_name": "Daywork",
        },
    )

    assert response.status_code == 422


def test_csv_import_preview_maps_headers_and_validates_rows() -> None:
    content = (
        "Report Name,Semantic Model,Workspace Name\n"
        "Active Items,Wholesale Data Model,Supply Chain Dashboards\n"
        "Daywork,,Supply Chain\n"
    ).encode("utf-8")
    preview = api._parse_batch_import(
        "jobs.csv", base64.b64encode(content).decode("ascii")
    )

    assert preview["headers_valid"]
    assert preview["rows"][0]["workspace_name"] == "Supply Chain Dashboards"
    assert preview["rows"][0]["semantic_model"] == "Wholesale Data Model"
    assert preview["rows"][0]["report_name"] == "Active Items"
    assert preview["rows"][1]["errors"] == ["Semantic Model is required."]
    assert not preview["can_import"]


def test_batch_import_preview_endpoint_returns_validated_rows() -> None:
    client = TestClient(app)
    content = (
        "Workspace Name,Semantic Model,Report Name\n"
        "Supply Chain,Supply Model,Daywork\n"
    ).encode("utf-8")

    response = client.post(
        "/api/batches/import-preview",
        json={
            "filename": "jobs.csv",
            "content_base64": base64.b64encode(content).decode("ascii"),
        },
    )

    assert response.status_code == 200
    preview = response.json()
    assert preview["headers_valid"]
    assert preview["can_import"]
    assert preview["rows"][0]["workspace_name"] == "Supply Chain"
    assert preview["rows"][0]["semantic_model"] == "Supply Model"
    assert preview["rows"][0]["report_name"] == "Daywork"


def test_xlsx_import_preview_maps_reordered_columns() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Semantic Model", "Report Name", "Workspace Name"])
    sheet.append(["DG Invoice Data", "DG Invoice Data", "Transportation"])
    content = io.BytesIO()
    workbook.save(content)
    workbook.close()

    preview = api._parse_batch_import(
        "jobs.xlsx", base64.b64encode(content.getvalue()).decode("ascii")
    )

    assert preview["headers_valid"]
    assert preview["can_import"]
    assert preview["rows"] == [
        {
            "row_number": 2,
            "workspace_name": "Transportation",
            "semantic_model": "DG Invoice Data",
            "report_name": "DG Invoice Data",
            "errors": [],
        }
    ]


def test_batch_import_rejects_missing_headers_and_more_than_ten_jobs() -> None:
    missing_header = api._build_import_preview(
        [
            ["Workspace Name", "Report Name"],
            ["Supply Chain", "Daywork"],
        ]
    )
    assert not missing_header["headers_valid"]
    assert "Missing required column: Semantic Model." in missing_header["errors"]

    content = (
        "Workspace Name,Semantic Model,Report Name\n"
        + "".join(f"Workspace {index},Model {index},Report {index}\n" for index in range(11))
    ).encode("utf-8")
    too_many = api._parse_batch_import(
        "jobs.csv", base64.b64encode(content).decode("ascii")
    )
    assert too_many["nonempty_records"] == 11
    assert len(too_many["rows"]) == 11
    assert not too_many["can_import"]
    assert any("more than 10" in error for error in too_many["errors"])


def test_batch_request_requires_all_three_fields_and_caps_jobs() -> None:
    row = {
        "workspace_name": "Workspace",
        "semantic_model": "Model",
        "report_name": "Report",
    }
    assert len(api.BatchRequest(jobs=[row] * 10).jobs) == 10
    with pytest.raises(ValidationError):
        api.BatchRequest(jobs=[{**row, "semantic_model": " "}])
    with pytest.raises(ValidationError):
        api.BatchRequest(jobs=[row] * 11)


@pytest.mark.parametrize(
    ("method", "path", "status_code", "should_log"),
    [
        ("GET", "/api/batches/123", 200, False),
        ("GET", "/api/batches/123?poll=1", 200, False),
        ("GET", "/api/batches/123", 404, True),
        ("GET", "/api/batches/123/summary.json", 200, True),
        ("POST", "/api/batches/123", 200, True),
    ],
)
def test_batch_poll_access_log_filter_keeps_non_poll_requests(
    method: str, path: str, status_code: int, should_log: bool
) -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", method, path, "1.1", status_code),
        None,
    )

    assert api._BatchPollingAccessLogFilter().filter(record) is should_log


def test_batch_cleanup_removes_only_transient_discovery_state(
    tmp_path, monkeypatch
) -> None:
    debug_dir = tmp_path / "discovery_debug"
    debug_dir.mkdir()
    (debug_dir / "stale.html").write_text("stale", encoding="utf-8")
    nested = debug_dir / "nested"
    nested.mkdir()
    (nested / "stale.json").write_text("stale", encoding="utf-8")
    auth_path = tmp_path / "auth.json"
    auth_path.write_text("authentication", encoding="utf-8")
    monkeypatch.setenv("POWERBI_DEBUG_DIR", str(debug_dir))

    removed = api._clear_batch_transient_state(auth_path)

    assert removed == 2
    assert list(debug_dir.iterdir()) == []
    assert auth_path.read_text(encoding="utf-8") == "authentication"


def test_batch_fails_explicitly_when_transient_cleanup_fails(
    monkeypatch, caplog
) -> None:
    batch_id = str(uuid4())
    job_id = str(uuid4())
    request = api.BatchRequest(
        jobs=[
            api.BatchJobRequest(
                workspace_name="Workspace",
                semantic_model="Model",
                report_name="Report",
            )
        ]
    )
    monkeypatch.setattr(
        api,
        "JOBS",
        {job_id: {"status": "queued", "error": None, "summary": None}},
    )
    monkeypatch.setattr(
        api,
        "BATCHES",
        {
            batch_id: {
                "status": "queued",
                "current_job_number": None,
                "stop_on_first_failure": False,
                "jobs": [
                    {
                        "number": 1,
                        "job_id": job_id,
                        "status": "pending",
                        "summary": None,
                        "error": None,
                    }
                ],
                "error": None,
            }
        },
    )
    monkeypatch.setattr(api, "ACTIVE_BATCH_ID", batch_id)
    monkeypatch.setattr(api, "ACTIVE_JOB_ID", None)
    monkeypatch.setattr(
        api,
        "_clear_batch_transient_state",
        lambda _storage_state: (_ for _ in ()).throw(
            RuntimeError("cleanup failed explicitly")
        ),
    )

    asyncio.run(api._run_batch(batch_id, request, Path("auth.json")))

    assert api.BATCHES[batch_id]["status"] == "failed"
    assert "cleanup failed explicitly" in api.BATCHES[batch_id]["error"]
    assert "failed while running" in caplog.text


@pytest.mark.parametrize(
    ("stop_on_first_failure", "expected_order", "expected_statuses"),
    [
        (
            False,
            ["First", "Second", "Third"],
            ["completed", "failed", "completed"],
        ),
        (
            True,
            ["First", "Second"],
            ["completed", "failed", "skipped"],
        ),
    ],
)
def test_batch_runs_sequentially_and_handles_failure(
    monkeypatch,
    caplog,
    stop_on_first_failure,
    expected_order,
    expected_statuses,
) -> None:
    caplog.set_level(logging.INFO, logger="Backend.api")
    batch_id = str(uuid4())
    request = api.BatchRequest(
        jobs=[
            api.BatchJobRequest(
                workspace_name=f"Workspace {index}",
                semantic_model=f"Model {index}",
                report_name=report,
            )
            for index, report in enumerate(("First", "Second", "Third"), start=1)
        ],
        stop_on_first_failure=stop_on_first_failure,
    )
    jobs = {}
    batch_jobs = []
    for number, request_row in enumerate(request.jobs, start=1):
        job_id = str(uuid4())
        jobs[job_id] = {
            "status": "queued",
            "batch_id": batch_id,
            "summary": None,
            "error": None,
        }
        batch_jobs.append(
            {
                "number": number,
                "job_id": job_id,
                "workspace_name": request_row.workspace_name,
                "semantic_model": request_row.semantic_model,
                "report_name": request_row.report_name,
                "status": "pending",
                "summary": None,
                "error": None,
            }
        )
    monkeypatch.setattr(api, "JOBS", jobs)
    monkeypatch.setattr(
        api,
        "BATCHES",
        {
            batch_id: {
                "status": "queued",
                "current_job_number": None,
                "stop_on_first_failure": stop_on_first_failure,
                "jobs": batch_jobs,
                "error": None,
            }
        },
    )
    monkeypatch.setattr(api, "ACTIVE_BATCH_ID", batch_id)
    monkeypatch.setattr(api, "ACTIVE_JOB_ID", None)
    order = []
    events = []
    cooldowns = []
    active_jobs = 0
    max_active_jobs = 0

    async def fake_run_comparison(job_id, job_request, _storage_state):
        nonlocal active_jobs, max_active_jobs
        active_jobs += 1
        max_active_jobs = max(max_active_jobs, active_jobs)
        order.append(job_request.report_name)
        events.append(job_request.report_name)
        if job_request.report_name == "Second":
            jobs[job_id].update(status="failed", error="expected failure")
        else:
            jobs[job_id].update(status="completed", summary={"matches": 1})
        active_jobs -= 1

    async def fake_sleep(duration):
        cooldowns.append(duration)
        events.append(f"rest:{duration}")
        assert api.BATCHES[batch_id]["cooldown_message"] == (
            f"Waiting {api.BATCH_JOB_COOLDOWN_SECONDS} seconds before "
            f"starting job {len(order) + 1}."
        )

    monkeypatch.setattr(api, "_run_comparison", fake_run_comparison)
    monkeypatch.setattr(api, "_clear_batch_transient_state", lambda _storage: 0)
    monkeypatch.setattr(api.asyncio, "sleep", fake_sleep)
    asyncio.run(api._run_batch(batch_id, request, Path("auth.json")))

    assert order == expected_order
    expected_cooldowns = (
        [api.BATCH_JOB_COOLDOWN_SECONDS]
        * (len(expected_order) - 1)
    )
    assert cooldowns == expected_cooldowns
    assert events == [
        item
        for index, report in enumerate(expected_order)
        for item in (
            (report, f"rest:{api.BATCH_JOB_COOLDOWN_SECONDS}")
            if index < len(expected_order) - 1
            else (report,)
        )
    ]
    assert max_active_jobs == 1
    assert [job["status"] for job in batch_jobs] == expected_statuses
    assert api._batch_counts(api.BATCHES[batch_id]) == {
        "total": 3,
        "completed": expected_statuses.count("completed"),
        "failed": 1,
        "skipped": expected_statuses.count("skipped"),
    }
    assert f"Batch started: batch_id={batch_id}" in caplog.text
    assert "Batch job started:" in caplog.text
    assert "Batch job finished:" in caplog.text
    assert f"Batch finished: batch_id={batch_id}" in caplog.text
    assert "elapsed_seconds=" in caplog.text


def test_batch_output_folder_contains_reports_and_dashboard_links(
    tmp_path, monkeypatch
) -> None:
    output_root = tmp_path / "jobs"
    completed_id = str(uuid4())
    failed_id = str(uuid4())
    batch_id = str(uuid4())
    completed_dir = output_root / "Active_Items"
    failed_dir = output_root / "Stopped_Report"
    completed_dir.mkdir(parents=True)
    failed_dir.mkdir(parents=True)
    (completed_dir / "Dashboard_Comparison.docx").write_bytes(b"docx-data")
    (completed_dir / "Dashboard_Comparison.xlsx").write_bytes(b"xlsx-data")
    (completed_dir / "discovery.json").write_text("{}", encoding="utf-8")
    (failed_dir / "Dashboard_Comparison.docx").write_bytes(b"partial-docx")
    monkeypatch.setattr(api, "OUTPUT_ROOT", output_root)
    monkeypatch.setattr(
        api,
        "JOBS",
        {
            completed_id: {
                "status": "completed",
                "output_dir": str(completed_dir),
                "workspace_name": "Supply Chain",
                "report_name": "Active Items",
                "dashboard_a_url": "https://app.powerbi.com/reports/base-report",
                "dashboard_b_url": "https://app.powerbi.com/reports/gcp-report",
                "progress": [],
                "elapsed_seconds": 1.0,
                "summary": {},
                "error": None,
            },
            failed_id: {
                "status": "failed",
                "output_dir": str(failed_dir),
                "workspace_name": "Supply Chain",
                "report_name": "Stopped Report",
                "dashboard_a_url": "https://app.powerbi.com/reports/stopped-base",
                "dashboard_b_url": None,
                "progress": [],
                "elapsed_seconds": 1.0,
                "summary": None,
                "error": "A later comparison stage failed.",
            },
        },
    )
    monkeypatch.setattr(
        api,
        "BATCHES",
        {
            batch_id: {
                "status": "completed",
                "current_job_number": None,
                "stop_on_first_failure": True,
                "jobs": [
                    {
                        "number": 1,
                        "job_id": completed_id,
                        "workspace_name": "Supply Chain",
                        "semantic_model": "Supply Model",
                        "report_name": "Active Items",
                        "status": "completed",
                        "summary": {},
                        "error": None,
                    },
                    {
                        "number": 2,
                        "job_id": failed_id,
                        "workspace_name": "Supply Chain",
                        "semantic_model": "Supply Model",
                        "report_name": "Stopped Report",
                        "status": "failed",
                        "summary": None,
                        "error": "A later comparison stage failed.",
                    },
                ],
                "error": None,
            }
        },
    )

    client = TestClient(app)
    response = client.get(f"/api/batches/{batch_id}/outputs.zip")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert len(names) == 3
        assert any(name.endswith("/Active Items.docx") for name in names)
        assert any(name.endswith("/Active Items.xlsx") for name in names)
        assert any(name.endswith("/PowerBI_Links.txt") for name in names)
        assert not any(name.lower().endswith(".json") for name in names)
        links_file = next(
            name for name in names if name.endswith("/PowerBI_Links.txt")
        )
        links = archive.read(links_file).decode("utf-8")
        assert "https://app.powerbi.com/reports/base-report" in links
        assert "https://app.powerbi.com/reports/gcp-report" in links

    job_response = client.get(f"/api/jobs/{completed_id}/outputs.zip")
    assert job_response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(job_response.content)) as archive:
        names = archive.namelist()
        assert len(names) == 3
        assert any(name.endswith("/Active Items.docx") for name in names)
        assert any(name.endswith("/Active Items.xlsx") for name in names)
        assert any(name.endswith("/PowerBI_Links.txt") for name in names)
        assert not any(name.lower().endswith(".json") for name in names)

    docx_response = client.get(
        f"/api/jobs/{completed_id}/Dashboard_Comparison.docx"
    )
    excel_response = client.get(
        f"/api/jobs/{completed_id}/Dashboard_Comparison.xlsx"
    )
    assert "Active%20Items.docx" in docx_response.headers["content-disposition"]
    assert "Active%20Items.xlsx" in excel_response.headers["content-disposition"]


def test_unknown_job_is_not_found() -> None:
    client = TestClient(app)

    response = client.get("/api/jobs/not-a-uuid")

    assert response.status_code == 404


def test_interactive_results_route_is_removed() -> None:
    client = TestClient(app)
    job_id = str(uuid4())
    JOBS[job_id] = {
        "status": "completed",
        "summary": {},
        "error": None,
    }
    try:
        response = client.get(f"/jobs/{job_id}")
    finally:
        JOBS.pop(job_id, None)

    assert response.status_code == 404
