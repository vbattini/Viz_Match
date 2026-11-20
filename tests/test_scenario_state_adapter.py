import asyncio
import json

from Backend import scenario_state_adapter as adapter
from Backend import workflow
from Backend.comparison import compare_dashboards
from Backend.models import DashboardState
from Backend.scenario_evidence import dashboard_execution_evidence_path


def test_snapshot_data_preserves_all_filters_and_unreadable_values() -> None:
    names, values = adapter._snapshot_data(
        {
            "filter_state_snapshot": [
                {"filter_name": "Facility", "selected_values": ["02 - Sioux Falls"]},
                {"filter_name": "Category", "selected_values": ["All"]},
                {"filter_name": "Master Vendor", "selected_values": None},
            ]
        }
    )

    assert names == ["Facility", "Category", "Master Vendor"]
    assert values == {
        "Facility": ["02 - Sioux Falls"],
        "Category": ["All"],
    }


def _write_side(
    root,
    dashboard,
    *,
    scenario_id="01",
    pairing_key="table_headers_initial:scroll_01",
    headers=("Item", "Quantity"),
):
    screenshot = root / dashboard / "Sales" / f"{scenario_id}-{pairing_key}.png"
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    screenshot.write_bytes(b"png")
    execution_path = dashboard_execution_evidence_path(
        root, dashboard, scenario_id, "Sales"
    )
    status_path = dashboard_execution_evidence_path(
        root, dashboard, scenario_id, "Sales", status=True
    )
    execution_path.parent.mkdir(parents=True, exist_ok=True)
    execution_path.write_text('{"status":"PASS"}', encoding="utf-8")
    status_path.write_text('{"status":"PASS"}', encoding="utf-8")
    return {
        "dashboard_id": dashboard,
        "page_name": "Sales",
        "scenario_id": scenario_id,
        "scenario_name": "Table Column Header Comparison",
        "capture_role": "table_headers_initial",
        "pairing_key": pairing_key,
        "screenshot_filename": screenshot.name,
        "screenshot_full_path": str(screenshot),
        "filter_selections": [],
        "capture_status": "CAPTURED",
        "execution_json_evidence_path": str(execution_path),
        "headers": list(headers),
    }


def _write_manifest(root, dashboard, entries):
    directory = root / dashboard
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "evidence_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dashboard_id": dashboard,
                "screenshots": entries,
            }
        ),
        encoding="utf-8",
    )


def test_scenario_states_use_matched_screenshot_extraction_and_header_only_mode(
    tmp_path, monkeypatch
):
    entries = {}
    for dashboard, headers in (
        ("dashboard_A", ("Item", "Quantity")),
        ("dashboard_B", ("Item", "Quantity")),
    ):
        entries[dashboard] = [
            _write_side(tmp_path, dashboard, headers=headers)
        ]
        _write_manifest(tmp_path, dashboard, entries[dashboard])
    extraction_modes = []

    async def fake_extract(_image, page_name, **kwargs):
        assert page_name == "Sales"
        extraction_modes.append(kwargs["header_only"])
        return {
            "elements": [
                {
                    "element_id": "TABLE_001",
                    "type": "table",
                    "title": "Inventory",
                    "categories": ["Item", "Quantity"],
                    "values": [],
                    "rows": [],
                }
            ],
            "title_items": [],
        }

    monkeypatch.setattr(adapter, "extract_screenshot", fake_extract)
    states_a, states_b, context = asyncio.run(
        adapter.load_scenario_states(
            destination=tmp_path,
            job_id="job",
            operation_log_path=tmp_path / "operations.jsonl",
            execution={
                "pages": [
                    {
                        "page": "Sales",
                        "statuses": {
                            "01_table_column_header_comparison": "PASS"
                        },
                    }
                ]
            },
        )
    )

    assert len(states_a) == len(states_b) == 1
    assert states_a[0].state_key == states_b[0].state_key
    assert extraction_modes == [True, True]
    assert context["evidence_validation"]["status"] == "VALIDATED"
    result = compare_dashboards(states_a, states_b)
    assert result["comparisons"][0]["status"] == "MATCH"


def test_header_only_extraction_synthesizes_table_visual_from_headers(
    tmp_path, monkeypatch
):
    entries = {}
    for dashboard in ("dashboard_A", "dashboard_B"):
        entries[dashboard] = [_write_side(tmp_path, dashboard)]
        _write_manifest(tmp_path, dashboard, entries[dashboard])

    async def fake_extract(*_args, **_kwargs):
        return {
            "elements": [],
            "title_items": [
                {"category": "column_header", "text": "Item"},
                {"category": "column_header", "text": "Quantity"},
            ],
        }

    monkeypatch.setattr(adapter, "extract_screenshot", fake_extract)
    states_a, states_b, _ = asyncio.run(
        adapter.load_scenario_states(
            destination=tmp_path,
            job_id="job",
            operation_log_path=tmp_path / "operations.jsonl",
            execution={
                "pages": [
                    {
                        "page": "Sales",
                        "statuses": {
                            "01_table_column_header_comparison": "PASS"
                        },
                    }
                ]
            },
        )
    )

    for state in (states_a[0], states_b[0]):
        assert state.extracted["elements"] == [
            {
                "element_id": "TABLE_001",
                "type": "table",
                "title": "Table",
                "categories": ["Item", "Quantity"],
            }
        ]


def test_scenario_adapter_does_not_pair_different_filter_states(
    tmp_path, monkeypatch
):
    entry_a = _write_side(
        tmp_path,
        "dashboard_A",
        scenario_id="02",
        pairing_key="single_filter:facility=west",
    )
    entry_b = _write_side(
        tmp_path,
        "dashboard_B",
        scenario_id="02",
        pairing_key="single_filter:facility=east",
    )
    _write_manifest(tmp_path, "dashboard_A", [entry_a])
    _write_manifest(tmp_path, "dashboard_B", [entry_b])

    async def fake_extract(*_args, **_kwargs):
        return {"elements": [], "title_items": []}

    monkeypatch.setattr(adapter, "extract_screenshot", fake_extract)
    states_a, states_b, _ = asyncio.run(
        adapter.load_scenario_states(
            destination=tmp_path,
            job_id="job",
            operation_log_path=tmp_path / "operations.jsonl",
            execution={"pages": [{"page": "Sales", "statuses": {}}]},
        )
    )

    result = compare_dashboards(states_a, states_b)
    assert len(states_a) == len(states_b) == 2
    assert result["summary"]["matched_states"] == 2
    assert all(row["status"] == "UNVERIFIED" for row in result["comparisons"])


def test_scenario_adapter_uses_one_duplicate_single_filter_capture(
    tmp_path, monkeypatch
):
    pairing_key = "single_filter:facility nbr desc=5 - minot"
    entry_a = _write_side(
        tmp_path, "dashboard_A", scenario_id="02", pairing_key=pairing_key
    )
    duplicate_a = _write_side(
        tmp_path, "dashboard_A", scenario_id="02", pairing_key=pairing_key
    )
    entry_b = _write_side(
        tmp_path, "dashboard_B", scenario_id="02", pairing_key=pairing_key
    )
    _write_manifest(tmp_path, "dashboard_A", [entry_a, duplicate_a])
    _write_manifest(tmp_path, "dashboard_B", [entry_b])

    async def fake_extract(*_args, **_kwargs):
        return {"elements": [], "title_items": []}

    monkeypatch.setattr(adapter, "extract_screenshot", fake_extract)
    states_a, states_b, context = asyncio.run(
        adapter.load_scenario_states(
            destination=tmp_path,
            job_id="job",
            operation_log_path=tmp_path / "operations.jsonl",
            execution={"pages": [{"page": "Sales", "statuses": {}}]},
        )
    )

    assert len(states_a) == len(states_b) == 1
    assert any(
        "Duplicate single-filter screenshot evidence" in item["reason"]
        for item in context["evidence_validation"]["issues"]
    )


def test_scenario_workflow_compares_only_after_state_loader_finishes(
    tmp_path, monkeypatch
):
    events = []
    base_a = DashboardState("A", "Sales", None, tmp_path / "a.png", {"elements": []})
    base_b = DashboardState("B", "Sales", None, tmp_path / "b.png", {"elements": []})
    scenario_a = DashboardState(
        "A",
        "Sales",
        None,
        tmp_path / "scenario-a.png",
        {"elements": []},
        scenario_id="02",
        scenario_name="Single Filter Selection",
        scenario_step="single_filter:facility=west",
    )
    scenario_b = DashboardState(
        "B",
        "Sales",
        None,
        tmp_path / "scenario-b.png",
        {"elements": []},
        scenario_id="02",
        scenario_name="Single Filter Selection",
        scenario_step="single_filter:facility=west",
    )

    async def load_scenario_states():
        events.append("scenario_evidence_loaded")
        return [scenario_a], [scenario_b], {"execution": {"pages": []}}

    def compare_all(states_a, states_b):
        events.append("old_comparator")
        assert states_a == [base_a, scenario_a]
        assert states_b == [base_b, scenario_b]
        return {"summary": {"matches": 1}}

    def fail_if_incremental(*_args):
        raise AssertionError("Scenario-enabled runs must defer incremental comparisons.")

    monkeypatch.setattr(workflow, "compare_dashboards", compare_all)
    monkeypatch.setattr(workflow, "merge_incremental_comparisons", fail_if_incremental)
    run = workflow.ComparisonWorkflowRun(
        job_id="job",
        url_a="a",
        url_b="b",
        output_dir=tmp_path,
        storage_state=tmp_path / "auth.json",
        scenario_state_loader=load_scenario_states,
    )
    run.states_a = [base_a]
    run.states_b = [base_b]
    run.partial_comparisons = {"base": {"ignored": True}}

    async def execute_steps():
        await run.run_step("validate")
        await run.run_step("compare")

    asyncio.run(execute_steps())

    assert events == ["scenario_evidence_loaded", "old_comparator"]
    assert run.result["summary"]["validation_scenarios"] == {"pages": []}
