import asyncio
import json
from pathlib import Path

import pytest

from powerbi_dashboard_comparator import workflow
from powerbi_dashboard_comparator.models import DashboardState


def _state(dashboard: str) -> DashboardState:
    return DashboardState(
        dashboard=dashboard,
        page="Home",
        filter=None,
        screenshot=Path("sample.png"),
        extracted={
            "elements": [
                {
                    "element_id": "KPI_001",
                    "element_type": "kpi",
                    "type": "kpi",
                    "title": "Open cases",
                    "value": 12,
                    "reading_order": 1,
                    "extraction_status": "complete",
                }
            ],
            "title_items": [],
        },
    )


def test_validation_rejects_unknown_extraction_status() -> None:
    state = _state("Dashboard A")
    state.extracted["elements"][0]["extraction_status"] = "verified-ish"

    with pytest.raises(ValueError, match="unsupported extraction status"):
        workflow._validate_captured_states([state], [_state("Dashboard B")])


def test_validation_accepts_explicitly_unverified_capture_state() -> None:
    state = _state("Dashboard A")
    state.extracted.clear()
    state.extracted.update(
        {
            "capture_status": "unverified",
            "capture_error": "Requested filter option was unavailable",
        }
    )

    workflow._validate_captured_states([state], [_state("Dashboard B")])


def test_adk_sequential_workflow_runs_capture_validate_compare_and_report(
    monkeypatch, tmp_path
) -> None:
    calls = []
    captured_a, captured_b = [_state("Dashboard A")], [_state("Dashboard B")]
    original_validate = workflow._validate_captured_states
    original_compare = workflow.compare_dashboards

    async def capture(**kwargs):
        assert kwargs["extract_visual_data"] is False
        assert kwargs["detect_visual_types"] is True
        calls.append("capture")
        return captured_a, captured_b

    def validate(states_a, states_b):
        calls.append("validate")
        original_validate(states_a, states_b)

    def compare(states_a, states_b):
        calls.append("compare")
        return original_compare(states_a, states_b)

    def write(*_args, **_kwargs):
        calls.append("report")

    monkeypatch.setattr(workflow, "capture_dashboard_pair", capture)
    monkeypatch.setattr(workflow, "_validate_captured_states", validate)
    monkeypatch.setattr(workflow, "compare_dashboards", compare)
    monkeypatch.setattr(workflow, "write_comparison", write)

    result = asyncio.run(
        workflow.run_dashboard_comparison(
            job_id="job-1",
            url_a="https://app.powerbi.com/report/a",
            url_b="https://app.powerbi.com/report/b",
            output_dir=tmp_path,
            storage_state=tmp_path / "auth.json",
        )
    )

    assert calls == ["capture", "validate", "compare", "report"]
    assert result["kpis"]["matches"] == 1
    operation_events = [
        json.loads(line)
        for line in (tmp_path / "operation_timings.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    job_events = [
        event for event in operation_events if event["operation"] == "job"
    ]
    assert [event["event"] for event in job_events] == ["started", "finished"]
    assert job_events[-1]["outcome"] == "success"
    assert any(
        event["operation"] == "workflow_report"
        and event["outcome"] == "success"
        for event in operation_events
    )
    assert "https://app.powerbi.com/report/a" not in json.dumps(operation_events)


def test_workflow_failure_is_recorded_and_reraised(monkeypatch, tmp_path) -> None:
    async def fail_capture(**_kwargs):
        raise TimeoutError("private report URL must not be logged")

    monkeypatch.setattr(workflow, "capture_dashboard_pair", fail_capture)

    with pytest.raises(TimeoutError, match="private report URL"):
        asyncio.run(
            workflow.run_dashboard_comparison(
                job_id="failed-job",
                url_a="https://app.powerbi.com/report/a",
                url_b="https://app.powerbi.com/report/b",
                output_dir=tmp_path,
                storage_state=tmp_path / "auth.json",
            )
        )

    operation_events = [
        json.loads(line)
        for line in (tmp_path / "operation_timings.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    failed_events = [
        event for event in operation_events if event["outcome"] == "failed"
    ]
    assert any(
        event["operation"] == "workflow_capture"
        and event["error_type"] == "TimeoutError"
        for event in failed_events
    )
    assert any(
        event["operation"] == "job"
        and event["error_type"] == "TimeoutError"
        for event in failed_events
    )
    assert "private report URL" not in json.dumps(operation_events)


def test_capture_compares_matching_states_as_they_arrive(
    monkeypatch, tmp_path
) -> None:
    state_a = _state("Dashboard A")
    state_b = _state("Dashboard B")

    async def capture(**kwargs):
        publish = kwargs["on_state_captured"]
        publish("dashboard_A", state_a)
        publish("dashboard_B", state_b)
        return [state_a], [state_b]

    monkeypatch.setattr(workflow, "capture_dashboard_pair", capture)
    run = workflow.ComparisonWorkflowRun(
        job_id="incremental-job",
        url_a="https://app.powerbi.com/report/a",
        url_b="https://app.powerbi.com/report/b",
        output_dir=tmp_path,
        storage_state=tmp_path / "auth.json",
    )

    asyncio.run(run._capture())

    assert list(run.partial_comparisons) == ["home|initial"]
    assert run.partial_comparisons["home|initial"]["comparisons"][0]["status"] == "MATCH"
