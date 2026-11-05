import asyncio
from fastapi.testclient import TestClient
from pathlib import Path
from uuid import uuid4

from powerbi_dashboard_comparator import api
from powerbi_dashboard_comparator.api import JOBS, app


def test_ui_and_runtime_config_endpoints() -> None:
    client = TestClient(app)

    page = client.get("/")
    config = client.get("/api/config")
    background = client.get("/static/powerbi-wallpaper.jpg")

    assert page.status_code == 200
    assert "Power BI Dashboard Comparator" in page.text
    assert "`Vertex AI: ${config.location} / ${config.model}`" in page.text
    assert "${config.project_id}" not in page.text
    assert 'url("/static/powerbi-wallpaper.jpg")' in page.text
    assert 'id="execution-time"' in page.text
    assert "Elapsed: 00:00:000" in page.text
    assert "function formatStopwatch(milliseconds)" in page.text
    assert "Elapsed: ${formatStopwatch(elapsed * 1000)}" in page.text
    assert "Execution time: ${formatStopwatch(elapsed * 1000)}" in page.text
    assert "Comparison timed out after 60 seconds." not in page.text
    assert "success_percentage" in page.text
    assert "exported cell match" in page.text
    assert "Open interactive results" not in page.text
    assert "overflow: hidden" in page.text
    assert "Spot the story behind the numbers." in page.text
    assert "BASELINE" in page.text
    assert "TEST REPORT" in page.text
    assert '<svg class="artwork"' in page.text
    assert "@media (max-height: 780px)" in page.text
    assert "@media (max-width: 860px)" in page.text
    assert 'id="url-a"' in page.text
    assert 'id="url-b"' in page.text
    assert background.status_code == 200
    assert background.headers["content-type"] == "image/jpeg"
    assert background.content.startswith(b"\xff\xd8\xff")
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
                    url_a="https://app.powerbi.com/report/a",
                    url_b="https://app.powerbi.com/report/b",
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


def test_api_rejects_non_powerbi_urls() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/compare",
        json={
            "url_a": "https://example.com/report",
            "url_b": "https://app.powerbi.com/reports/report",
        },
    )

    assert response.status_code == 422


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
