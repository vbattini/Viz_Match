import asyncio
import json
import os
from pathlib import Path

from Backend import agents
from Backend import scenario_comparison as sc
from Backend.scenario_evidence import (
    EvidenceManifestStore,
    dashboard_execution_evidence_path,
    dashboard_scenario_directory,
    scenario_result_directory,
)


def test_step_filter_comparison_detects_mismatched_selection():
    execution = {
        "plan": {"name_a": "Department", "name_b": "Department"},
        "steps": [
            {
                "record": {
                    "filter": "Department",
                    "requested": ["BAKERY"],
                    "selected": ["All"],
                    "verified": False,
                }
            },
            {
                "record": {
                    "filter": "Department",
                    "requested": ["BAKERY"],
                    "selected": ["BAKERY"],
                    "verified": True,
                }
            },
        ],
    }

    result = sc._compare_step_records("03", execution)

    assert result["state_equivalent"] is False
    assert result["expected_state_applied"] is False
    assert result["status"] == sc.STATUS_FAIL
    assert any(
        item["category"] == "DIFFERENT_VALUE" for item in result["items"]
    )


def test_json_writer_retries_transient_windows_replace_lock(
    tmp_path, monkeypatch
):
    target = tmp_path / "comparison_progress.json"
    original_replace = Path.replace
    temp_paths = []
    attempts = 0

    def replace_with_transient_lock(source, destination):
        nonlocal attempts
        if destination == target:
            temp_paths.append(source)
            attempts += 1
            if attempts < 3:
                raise PermissionError("file is temporarily locked")
        return original_replace(source, destination)

    monkeypatch.setattr(Path, "replace", replace_with_transient_lock)
    monkeypatch.setattr(sc.time, "sleep", lambda _seconds: None)

    sc._write_json(target, {"status": "IN_PROGRESS"})

    assert attempts == 3
    assert len(set(temp_paths)) == 1
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "status": "IN_PROGRESS"
    }
    assert list(tmp_path.glob("*.tmp")) == []


def test_json_writer_reports_persistent_replace_permission_error(
    tmp_path, monkeypatch
):
    target = tmp_path / "comparison_progress.json"
    monkeypatch.setattr(
        Path,
        "replace",
        lambda _source, _destination: (_ for _ in ()).throw(
            PermissionError("file remains locked")
        ),
    )
    monkeypatch.setattr(sc.time, "sleep", lambda _seconds: None)

    try:
        sc._write_json(target, {"status": "IN_PROGRESS"})
    except PermissionError as exc:
        assert str(exc) == "file remains locked"
    else:
        raise AssertionError("persistent file lock should be reported")

    assert list(tmp_path.iterdir()) == []


def test_manifest_screenshots_pair_only_identical_scenario_states(tmp_path):
    store = EvidenceManifestStore(tmp_path)
    store.initialize()

    async def add(side, name, pairing_key):
        screenshot = tmp_path / side / name
        screenshot.write_bytes(name.encode())
        await store.add(
            {
                "dashboard_id": side,
                "page_name": "Page One",
                "scenario_id": "02",
                "pairing_key": pairing_key,
                "capture_status": "CAPTURED",
                "screenshot_filename": screenshot.name,
                "screenshot_full_path": str(screenshot.resolve()),
            }
        )

    async def populate():
        await add("dashboard_A", "a_north.png", "single_filter:region=north")
        await add("dashboard_A", "a_south.png", "single_filter:region=south")
        await add("dashboard_B", "b_north.png", "single_filter:region=north")

    asyncio.run(populate())
    sides, errors = sc._comparison_screenshots(
        "02", "Page One", {}, tmp_path, tmp_path
    )

    assert [item["label"] for item in sides["dashboard_A"]] == [
        "single_filter:region=north"
    ]
    assert [item["label"] for item in sides["dashboard_B"]] == [
        "single_filter:region=north"
    ]
    assert any("single_filter:region=south" in error for error in errors)


def test_scenario01_keeps_every_scroll_capture_and_excludes_generic_initial(tmp_path):
    source_root = tmp_path / "source"
    store = EvidenceManifestStore(source_root)
    store.initialize()
    positions_by_side = {
        "dashboard_A": ["initial", "horizontal_scroll_01", "horizontal_scroll_02"],
        "dashboard_B": ["initial", "horizontal_scroll_01"],
    }

    async def populate():
        for side, positions in positions_by_side.items():
            screenshot_paths = {}
            for position in positions:
                screenshot = (
                    source_root
                    / side
                    / "Page_One"
                    / f"{side}_{position}.png"
                )
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                screenshot.write_bytes(f"{side}:{position}".encode())
                screenshot_paths[position] = str(screenshot.resolve())
                await store.add(
                    {
                        "dashboard_id": side,
                        "page_name": "Page One",
                        "scenario_id": "01",
                        "capture_role": (
                            "table_headers_initial"
                            if position == "initial"
                            else "table_headers_scroll"
                        ),
                        "pairing_key": f"table_headers:{position}",
                        "scroll_position_id": position,
                        "capture_status": "CAPTURED",
                        "screenshot_filename": screenshot.name,
                        "screenshot_full_path": str(screenshot.resolve()),
                    }
                )
            execution_path = dashboard_execution_evidence_path(
                source_root, side, "01", "Page One"
            )
            execution_path.parent.mkdir(parents=True, exist_ok=True)
            execution_path.write_text(
                json.dumps({"screenshots": screenshot_paths}),
                encoding="utf-8",
            )
            (source_root / side / "Page_One" / "initial.png").write_bytes(
                b"generic initial must not be used"
            )

    asyncio.run(populate())
    sides, errors = sc._comparison_screenshots(
        "01", "Page One", {}, source_root, tmp_path
    )

    assert errors == []
    assert [item["label"] for item in sides["dashboard_A"]] == [
        "initial",
        "horizontal_scroll_01",
        "horizontal_scroll_02",
    ]
    assert [item["label"] for item in sides["dashboard_B"]] == [
        "initial",
        "horizontal_scroll_01",
    ]
    assert all(Path(item["path"]).name != "initial.png" for group in sides.values() for item in group)
    assert [label for label, _ in sc._header_vision_inputs(sides)] == [
        "Dashboard A initial",
        "Dashboard A horizontal_scroll_01",
        "Dashboard A horizontal_scroll_02",
        "Dashboard B initial",
        "Dashboard B horizontal_scroll_01",
    ]


def test_scenario01_result_preserves_repeated_headers_and_normalizes_whitespace():
    result, findings = sc._scenario01_result(
        {
            "llm_vision_comparison": {
                "status": "FAIL",
                "tables_found_a": True,
                "tables_found_b": True,
                "headers_a": ["Item  ID", "Department", "Department"],
                "headers_b": ["Item ID", "Department"],
                "matching_headers": ["Item ID", "Department"],
                "notes": [],
            }
        }
    )

    assert result["status"] == "FAIL"
    assert result["missing_headers"] == ["Department"]
    assert result["additional_headers"] == []
    assert any(
        item["category"] == "MISSING"
        and "Department" in item["observation"]
        for item in findings
    )


def test_scenario01_compares_initial_only_execution_even_when_scroll_is_skipped():
    assert (
        sc._status_from_results(
            "01",
            sc.STATUS_SKIPPED,
            {"items": []},
            {"status": sc.STATUS_PASS},
            [],
        )
        == sc.STATUS_PASS
    )


def test_verified_flag_does_not_override_requested_value_readback():
    record = {
        "filter": "Department",
        "requested": ["BAKERY"],
        "selected": ["All"],
        "verified": True,
    }

    result = sc._compare_step_records(
        "02",
        {
            "steps": [
                {"record": record},
                {"record": dict(record)},
            ]
        },
    )

    assert result["expected_state_applied"] is False
    assert result["status"] == sc.STATUS_FAIL
    assert any(
        item["category"] == "BEHAVIOR_DIFFERENCE" for item in result["items"]
    )


def test_filter_control_type_difference_is_behavior_difference(tmp_path):
    source = tmp_path / "source"
    planning = source / "planning" / "Page_One"
    planning.mkdir(parents=True)
    for dashboard, control_type in (
        ("dashboard_A", "checkbox_dropdown"),
        ("dashboard_B", "dropdown"),
    ):
        (planning / f"{dashboard}_plan.json").write_text(
            json.dumps(
                {
                    "filters": [
                        {
                            "name": "Department",
                            "control_type": control_type,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    result = sc._compare_filter_inventory(source, "Page One")

    assert result["status"] == sc.STATUS_FAIL
    assert result["items"][0]["category"] == "BEHAVIOR_DIFFERENCE"


def test_multiple_filter_comparison_accepts_verified_base_plus_additional_filter():
    record_a = {
        "filter": "Facility",
        "requested": ["38 - Bloomington"],
        "selected": ["38 - Bloomington"],
        "verified": True,
        "base_selection": {
            "filter": "Vendor",
            "requested": ["- DACOTAH PAPER CO"],
            "selected": ["- DACOTAH PAPER CO"],
            "verified": True,
        },
    }
    execution = {
        "plan": {
            "values_a": ["38 - Bloomington", "03 - Fargo"],
            "values_b": ["38 - Bloomington", "03 - Fargo"],
        },
        "steps": [
            {"record": record_a},
            {"record": {**record_a, "base_selection": dict(record_a["base_selection"]) }},
        ],
    }

    result = sc._compare_step_records("03", execution)

    assert result["state_equivalent"] is True
    assert result["expected_state_applied"] is True
    assert result["status"] == sc.STATUS_PASS


def test_scenario01_maps_only_saved_vision_result():
    result, findings = sc._scenario01_result(
        {
            "overall_comparison_status": "FAIL",
            "llm_vision_comparison": {
                "status": "FAIL",
                "headers_a": ["Item", "Facility"],
                "headers_b": ["Facility", "Item Code"],
                "matching_headers": ["Facility"],
                "missing_headers": ["Item"],
                "additional_headers": ["Item Code"],
                "renamed_headers": [],
                "differently_ordered_headers": [],
            },
        }
    )

    assert result["status"] == "FAIL"
    assert [item["category"] for item in findings] == [
        "MATCH",
        "MISSING",
        "ADDITIONAL",
    ]


def test_merged_hierarchy_does_not_compare_unrelated_filter_inventory(tmp_path):
    source = tmp_path / "source"
    page_dir = scenario_result_directory(source, "01", "Page One")
    page_dir.mkdir(parents=True)
    (page_dir / "execution_status.json").write_text(
        json.dumps({"status": "SKIPPED", "reason": "no drill control"}),
        encoding="utf-8",
    )
    (page_dir / "scenario_execution.json").write_text(
        json.dumps({"visuals": []}), encoding="utf-8"
    )
    (page_dir / "comparison_result.json").write_text(
        json.dumps({"visuals": [], "screenshots": {}}), encoding="utf-8"
    )

    page, _ = sc._prepare_page(
        "01",
        "01_table_column_header_comparison",
        source,
        "Page One",
        tmp_path,
    )

    assert page["filter_comparison"]["status"] == "NOT_APPLICABLE"
    assert "inventory" not in page["filter_comparison"]
    assert sc._filter_findings(page["filter_comparison"]) == []


def test_screenshot_comparison_batches_same_scenario_pages(monkeypatch):
    response = {
        "page_results": [
            {
                "page_name": "All Items",
                "status": "PASS",
                "matching_elements": ["All Items Table"],
                "findings": [],
                "notes": [],
            }
        ]
    }

    class _Agent:
        def __init__(self, **_kwargs):
            pass

    async def fake_run(_agent, content, **_kwargs):
        labels = [
            part.text
            for part in content.parts
            if getattr(part, "text", None)
            and part.text.startswith("Screenshot:")
        ]
        assert labels == [
            "Screenshot: Page: All Items | state | Dashboard A",
            "Screenshot: Page: All Items | state | Dashboard B",
        ]
        assert "Vendor = ['DACOTAH']" in content.parts[0].text
        return json.dumps(response)

    monkeypatch.setattr(agents, "_model_name", lambda: "test-model")
    monkeypatch.setattr(agents, "_generation_config", lambda _model: None)
    monkeypatch.setattr(agents, "LlmAgent", _Agent)
    monkeypatch.setattr(agents, "_run_agent", fake_run)

    result = asyncio.run(
        agents.compare_scenario_screenshots(
            [
                ("Page: All Items | state | Dashboard A", b"a"),
                ("Page: All Items | state | Dashboard B", b"b"),
            ],
            ["All Items"],
            "02",
            "Single Filter Selection",
            page_states={"All Items": "Vendor = ['DACOTAH']"},
        )
    )

    assert result["page_results"][0]["status"] == "PASS"
    assert result["page_results"][0]["matching_elements"] == ["All Items Table"]


def test_docx_report_includes_scenario_comparison_summary(tmp_path, monkeypatch):
    screenshot_pairs = []
    scenarios = [
        {
            "scenario_id": "01",
            "scenario_name": "Table Column Header Comparison",
            "source_directory": "output_all",
            "pages": [
                {
                    "page_name": "Executive Summary",
                    "scenario_execution_status": "PASS",
                    "comparison_status": "UNVERIFIED",
                    "filter_comparison": {"status": "UNVERIFIED", "items": []},
                    "visual_comparison": {"status": "UNVERIFIED"},
                    "findings": [],
                    "evidence": {
                        "errors": ["Options not captured"],
                        "dashboard_A_screenshots": [
                            {"label": "scroll-01", "path": "dashboard_A_01.png"},
                            {"label": "scroll-02", "path": "dashboard_A_02.png"},
                        ],
                        "dashboard_B_screenshots": [
                            {"label": "scroll-01", "path": "dashboard_B_01.png"},
                            {"label": "scroll-02", "path": "dashboard_B_02.png"},
                        ],
                        "validation": {
                            "status": "UNVERIFIED",
                            "required_json_evidence": [
                                {
                                    "name": "dashboard_A_dom_plan",
                                    "path": "planning/Executive_Summary/dashboard_A_plan.json",
                                    "exists": False,
                                }
                            ],
                        },
                    },
                }
            ],
            "summary": {
                "page_count": 1,
                "status_counts": {
                    "PASS": 0,
                    "FAIL": 0,
                    "UNVERIFIED": 1,
                    "SKIPPED": 0,
                },
                "category_counts": {},
            },
        }
    ]
    summary = {
        "total_scenario_types": 1,
        "total_page_scenarios_processed": 1,
        "total_scenarios_executed": 1,
        "total_scenarios_compared": 1,
        "passed_scenarios": 0,
        "failed_scenarios": 0,
        "unverified_scenarios": 1,
        "skipped_scenarios": 0,
        "overall_dashboard_result": "UNVERIFIED",
        "evidence_validation_json": "comparison_results/evidence_validation.json",
        "visual_validation_scope_note": "Numeric data values were not assessed.",
        "scenario_summaries": [
            {
                "scenario_id": "01",
                "scenario_name": "Table Column Header Comparison",
                "page_count": 1,
                "status_counts": {
                    "PASS": 0,
                    "FAIL": 0,
                    "UNVERIFIED": 1,
                    "SKIPPED": 0,
                },
                "category_counts": {},
            }
        ],
    }
    path = tmp_path / "report.docx"
    monkeypatch.setattr(
        sc,
        "_add_image_pair",
        lambda _document, _page, label, path_a, path_b: screenshot_pairs.append(
            (label, path_a, path_b)
        ),
    )

    sc._write_docx(path, summary, scenarios, {})

    assert path.is_file()
    from docx import Document

    text = "\n".join(paragraph.text for paragraph in Document(path).paragraphs)
    assert "Scenario-by-Scenario Comparison" in text
    assert "Scenario 01 — Table Column Header Comparison" in text
    assert "dashboard_A_dom_plan" in text
    assert "planning/Executive_Summary/dashboard_A_plan.json (MISSING)" in text
    assert "comparison_results/evidence_validation.json" in text
    assert screenshot_pairs == [
        ("scroll-01", "dashboard_A_01.png", "dashboard_B_01.png"),
        ("scroll-02", "dashboard_A_02.png", "dashboard_B_02.png"),
    ]


def _create_comparison_sources(root: Path, page_names: list[str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "validation_summary.json").write_text(
        json.dumps({"pages": [{"page": name} for name in page_names]}),
        encoding="utf-8",
    )
    store = EvidenceManifestStore(root)
    store.initialize()
    for page_name in page_names:
        planning_dir = root / "planning" / sc.safe_path_part(page_name)
        planning_dir.mkdir(parents=True, exist_ok=True)
        for dashboard_id in ("dashboard_A", "dashboard_B"):
            (planning_dir / f"{dashboard_id}_plan.json").write_text(
                json.dumps({"filters": []}), encoding="utf-8"
            )
    for scenario_id, scenario_directory, scenario_name in sc.SCENARIOS:
        for page_name in page_names:
            page_dir = scenario_result_directory(root, scenario_id, page_name)
            page_dir.mkdir(parents=True, exist_ok=True)
            (page_dir / "execution_status.json").write_text(
                json.dumps({"status": "PASS"}), encoding="utf-8"
            )
            (page_dir / "scenario_execution.json").write_text(
                json.dumps(
                    {
                        "scenario_id": scenario_id,
                        "scenario_name": scenario_name,
                        "plan": {},
                        "steps": [],
                        "visuals": [],
                    }
                ),
                encoding="utf-8",
            )
            comparison_filename = (
                "visual_comparison.json"
                if scenario_id == "01"
                else "comparison_result.json"
            )
            (page_dir / comparison_filename).write_text(
                json.dumps(
                    {
                        "screenshots": {},
                        "visuals": [],
                        "llm_vision_comparison": {"status": "PASS"},
                    }
                ),
                encoding="utf-8",
            )
            for dashboard_id in ("dashboard_A", "dashboard_B"):
                dashboard_dir = dashboard_scenario_directory(
                    root, dashboard_id, scenario_id, page_name
                )
                dashboard_dir.mkdir(parents=True, exist_ok=True)
                execution_path = dashboard_execution_evidence_path(
                    root, dashboard_id, scenario_id, page_name
                )
                execution_path.write_text(
                    json.dumps({"scenario_id": scenario_id}),
                    encoding="utf-8",
                )
                dashboard_execution_evidence_path(
                    root,
                    dashboard_id,
                    scenario_id,
                    page_name,
                    status=True,
                ).write_text(
                    json.dumps({"status": "PASS"}),
                    encoding="utf-8",
                )
                screenshot = dashboard_dir / f"{scenario_id}_{dashboard_id}.png"
                screenshot.write_bytes(f"{dashboard_id}:{scenario_id}".encode())
                if scenario_id == "01":
                    execution_path.write_text(
                        json.dumps(
                            {
                                "scenario_id": scenario_id,
                                "screenshots": {
                                    "initial": str(screenshot.resolve())
                                },
                            }
                        ),
                        encoding="utf-8",
                    )
                asyncio.run(
                    store.add(
                        {
                            "dashboard_id": dashboard_id,
                            "page_name": page_name,
                            "page_identifier": sc.safe_path_part(page_name),
                            "page_number": 1,
                            "scenario_id": scenario_id,
                            "scenario_name": scenario_name,
                            "capture_role": "table_headers_initial" if scenario_id == "01" else "state",
                            "pairing_key": (
                                "table_headers_initial:initial"
                                if scenario_id == "01"
                                else f"state:{scenario_id}"
                            ),
                            "scroll_position_id": "initial" if scenario_id == "01" else None,
                            "capture_status": "CAPTURED",
                            "screenshot_filename": screenshot.name,
                            "screenshot_full_path": str(screenshot.resolve()),
                            "execution_json_evidence_path": str(
                                execution_path.resolve()
                            ),
                            "comparison_json_evidence_path": str(
                                (page_dir / comparison_filename).resolve()
                            ),
                        }
                    )
                )


def _cached_page(
    cache_root: Path,
    source_root: Path,
    scenario_id: str,
    scenario_name: str,
    page_name: str,
) -> None:
    page_dir = scenario_result_directory(source_root, scenario_id, page_name)
    screenshot_paths = []
    for side in ("dashboard_A", "dashboard_B"):
        screenshot_directory = dashboard_scenario_directory(
            source_root, side, scenario_id, page_name
        )
        screenshot_directory.mkdir(parents=True, exist_ok=True)
        screenshot = screenshot_directory / f"cached_{scenario_id}_{side}.png"
        screenshot.write_bytes(b"png")
        screenshot_paths.append(screenshot)
    cache_file = (
        cache_root
        / f"scenario_{scenario_id}"
        / "pages"
        / sc.safe_path_part(page_name)
        / "comparison.json"
    )
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        json.dumps(
            {
                "scenario_id": scenario_id,
                "scenario_name": scenario_name,
                "page_name": page_name,
                "scenario_execution_status": "PASS",
                "comparison_status": "PASS",
                "filter_comparison": {
                    "status": "PASS",
                    "state_equivalent": True,
                    "items": [],
                    "inventory": {"status": "PASS", "items": []},
                },
                "visual_comparison": {
                    "status": "PASS",
                    "matching_elements": ["Visual"],
                },
                "findings": [
                    {
                        "category": "MATCH",
                        "element_type": "Visual",
                        "observation": "Vision comparison matched the visual.",
                    }
                ],
                "evidence": {
                    "source_directory": str(source_root.resolve()),
                    "scenario_directory": str(page_dir.resolve()),
                    "dashboard_A_screenshots": [
                        {"label": "state", "path": str(screenshot_paths[0])}
                    ],
                    "dashboard_B_screenshots": [
                        {"label": "state", "path": str(screenshot_paths[1])}
                    ],
                    "errors": [],
                },
            }
        ),
        encoding="utf-8",
    )


def test_scenario_evidence_validation_finishes_before_comparison(
    tmp_path, monkeypatch
):
    source_root = tmp_path / "source"
    output_root = tmp_path / "output"
    page_names = ["Page One", "Page Two"]
    _create_comparison_sources(source_root, page_names)
    validated = []
    compared = []
    original_prepare = sc._prepare_page

    def track_prepare(*args, **kwargs):
        prepared = original_prepare(*args, **kwargs)
        validated.append((args[0], args[3]))
        return prepared

    async def compare_after_preflight(
        scenario_id,
        _scenario_directory,
        _scenario_name,
        _source_root,
        comparison_pages,
        _comparison_root,
        _workspace_root,
        _job_id,
        _operation_log,
        *,
        prepared_pages,
    ):
        assert len(validated) == len(page_names) * len(sc.SCENARIOS)
        page_name = comparison_pages[0]
        page, screenshots = prepared_pages[page_name]
        assert screenshots["dashboard_A"]
        assert screenshots["dashboard_B"]
        compared.append((scenario_id, page_name))
        page["comparison_status"] = sc.STATUS_PASS
        page["visual_comparison"] = {"status": sc.STATUS_PASS}
        page["filter_comparison"]["status"] = sc.STATUS_PASS
        return {"pages": [page]}

    def write_report(path, _summary, _scenarios, _references):
        assert len(compared) == len(page_names) * len(sc.SCENARIOS)
        path.write_bytes(b"complete report")

    monkeypatch.setattr(sc, "_prepare_page", track_prepare)
    monkeypatch.setattr(sc, "_compare_scenario", compare_after_preflight)
    monkeypatch.setattr(sc, "_write_docx", write_report)

    result = asyncio.run(
        sc.run_scenario_comparison(
            {scenario_id: source_root for scenario_id, _, _ in sc.SCENARIOS},
            output_root,
        )
    )

    assert len(validated) == len(page_names) * len(sc.SCENARIOS)
    assert len(compared) == len(page_names) * len(sc.SCENARIOS)
    validation = json.loads(
        (
            output_root
            / "comparison_results"
            / "evidence_validation.json"
        ).read_text(encoding="utf-8")
    )
    assert validation["unit_count"] == len(page_names) * len(sc.SCENARIOS)
    assert validation["status_counts"][sc.STATUS_PASS] == validation["unit_count"]
    assert Path(result["docx_report"]).read_bytes() == b"complete report"


def test_scenario_comparison_can_run_scenario01_only(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    output_root = tmp_path / "output"
    (source_root / "dashboard_A").mkdir(parents=True)
    (source_root / "dashboard_B").mkdir(parents=True)
    (source_root / "validation_summary.json").write_text(
        json.dumps({"pages": [{"page": "Page One"}]}),
        encoding="utf-8",
    )
    prepared = []
    compared = []

    def prepare(scenario_id, _directory, _source, page_name, _workspace, **_kwargs):
        assert scenario_id == "01"
        prepared.append(scenario_id)
        return (
            {
                "page_name": page_name,
                "scenario_execution_status": sc.STATUS_PASS,
                "scenario_execution_reason": None,
                "comparison_status": sc.STATUS_PASS,
                "filter_comparison": {"status": "NOT_APPLICABLE", "items": []},
                "visual_comparison": {"status": sc.STATUS_PASS},
                "findings": [],
                "evidence": {
                    "dashboard_A_screenshots": [],
                    "dashboard_B_screenshots": [],
                    "errors": [],
                },
            },
            {"dashboard_A": [], "dashboard_B": []},
        )

    def validate(_scenario_id, page_name, _source, _page, _error=None):
        return {
            "scenario_id": "01",
            "page_name": page_name,
            "status": sc.STATUS_PASS,
            "scenario_execution_status": sc.STATUS_PASS,
            "dashboard_A_screenshot_status": "AVAILABLE",
            "dashboard_B_screenshot_status": "AVAILABLE",
            "required_json_evidence": [],
            "errors": [],
        }

    async def compare(scenario_id, *_args, prepared_pages, **_kwargs):
        assert scenario_id == "01"
        compared.append(scenario_id)
        page, _screenshots = prepared_pages["Page One"]
        return {"pages": [page]}

    monkeypatch.setattr(sc, "_prepare_page", prepare)
    monkeypatch.setattr(sc, "_validate_prepared_evidence", validate)
    monkeypatch.setattr(sc, "_compare_scenario", compare)
    monkeypatch.setattr(
        sc,
        "organize_scenario_screenshots",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("single-scenario comparison must not process other evidence")
        ),
    )
    monkeypatch.setattr(
        sc,
        "_write_docx",
        lambda path, *_args: path.write_bytes(b"scenario 01 report"),
    )

    result = asyncio.run(
        sc.run_scenario_comparison(
            {"01": source_root},
            output_root,
            only_scenario_id="01",
        )
    )

    assert prepared == ["01"]
    assert compared == ["01"]
    assert result["summary"]["total_scenario_types"] == 1
    assert result["summary"]["total_comparison_units"] == 1
    assert result["summary"]["visual_validation_scope_note"] == (
        "Only table and matrix column headers were evaluated; KPI/chart values, "
        "filters, table cell data, and all other dashboard content were not compared."
    )
    assert not (output_root / "comparison_results" / "scenario_02").exists()
    scenario_wise = json.loads(
        Path(result["scenario_wise_comparison_json"]).read_text(encoding="utf-8")
    )
    assert [scenario["scenario_id"] for scenario in scenario_wise["scenarios"]] == [
        "01"
    ]


def test_missing_scenario_screenshot_is_unverified(tmp_path):
    source_root = tmp_path / "source"
    _create_comparison_sources(source_root, ["Page One"])
    scenario_id, scenario_directory, _ = next(
        scenario for scenario in sc.SCENARIOS if scenario[0] == "02"
    )
    page, screenshots = sc._prepare_page(
        scenario_id,
        scenario_directory,
        source_root,
        "Page One",
        tmp_path,
    )
    Path(screenshots["dashboard_B"][0]["path"]).unlink()
    missing_plan = (
        source_root
        / "planning"
        / sc.safe_path_part("Page One")
        / "dashboard_B_plan.json"
    )
    missing_plan.unlink()

    validation = sc._validate_prepared_evidence(
        scenario_id,
        "Page One",
        source_root,
        page,
    )

    assert validation["status"] == sc.STATUS_UNVERIFIED
    assert validation["dashboard_A_screenshot_status"] == "AVAILABLE"
    assert validation["dashboard_B_screenshot_status"] == "INCOMPLETE"
    assert any("dashboard_B screenshots are incomplete" in error for error in validation["errors"])
    assert any(
        item["name"] == "dashboard_B_dom_plan" and not item["exists"]
        for item in validation["required_json_evidence"]
    )


def test_scenario_comparison_runs_page_first_and_reuses_valid_cached_results(
    tmp_path, monkeypatch
):
    source_root = tmp_path / "source"
    cache_root = tmp_path / "cache"
    output_root = tmp_path / "output"
    pages = ["Page One", "Page Two"]
    _create_comparison_sources(source_root, pages)
    for scenario_id, _, scenario_name in sc.SCENARIOS:
        for page_name in pages:
            _cached_page(
                cache_root,
                source_root,
                scenario_id,
                scenario_name,
                page_name,
            )

    def fail_if_compared(*_args, **_kwargs):
        raise AssertionError("A valid cached result should be reused.")

    monkeypatch.setattr(sc, "_compare_scenario", fail_if_compared)
    monkeypatch.setattr(
        sc,
        "_write_docx",
        lambda path, *_args: path.write_bytes(b"docx"),
    )
    progress = []

    result = asyncio.run(
        sc.run_scenario_comparison(
            {scenario_id: source_root for scenario_id, _, _ in sc.SCENARIOS},
            output_root,
            comparison_cache_dir=cache_root,
            progress_callback=progress.append,
        )
    )

    started = [
        (item["current_page"], item["current_scenario_id"])
        for item in progress
        if item["comparison_status"] == "IN_PROGRESS"
        and item.get("current_page")
    ]
    assert started == [
        (page_name, scenario_id)
        for page_name in pages
        for scenario_id, _, _ in sc.SCENARIOS
    ]
    assert result["summary"]["comparison_order"] == "page_then_scenario"
    assert result["summary"]["total_comparison_units"] == 6
    comparison_root = output_root / "comparison_results"
    scenario_wise = json.loads(
        (comparison_root / "scenario_wise_comparison.json").read_text()
    )
    page_wise = json.loads(
        (comparison_root / "page_wise_comparison.json").read_text()
    )
    assert len(scenario_wise["scenarios"]) == 3
    assert [
        (scenario["scenario_id"], scenario["scenario_name"])
        for scenario in scenario_wise["scenarios"]
    ] == [(scenario_id, name) for scenario_id, _, name in sc.SCENARIOS]
    assert len(page_wise["pages"]) == 2
    assert len(page_wise["pages"][0]["scenarios"]) == 3
    assert [
        (scenario["scenario_id"], scenario["scenario_name"])
        for scenario in page_wise["pages"][0]["scenarios"]
    ] == [(scenario_id, name) for scenario_id, _, name in sc.SCENARIOS]
    assert Path(result["summary"]["scenario_wise_comparison_json"]).is_file()
    assert Path(result["summary"]["page_wise_comparison_json"]).is_file()
    assert len(result["page_comparisons"]) == 2
    assert progress[-1]["status"] == "COMPLETED"
    assert progress[-1]["completed_comparisons"] == 6
    assert progress[-1]["remaining_comparisons"] == 0
    page_result = json.loads(Path(result["page_comparisons"][0]).read_text())
    assert len(page_result["scenarios"]) == 3
    assert all(
        item["comparison_provenance"] == "REUSED_UNCHANGED_SAVED_COMPARISON"
        for item in page_result["scenarios"]
    )
    progress_file = Path(result["comparison_progress_json"])
    assert json.loads(progress_file.read_text())["status"] == "COMPLETED"


def test_cached_page_comparison_is_rejected_after_source_screenshot_changes(
    tmp_path,
):
    source_root = tmp_path / "source"
    cache_root = tmp_path / "cache"
    _create_comparison_sources(source_root, ["Page One"])
    _cached_page(
        cache_root,
        source_root,
        "01",
        sc.SCENARIOS[0][2],
        "Page One",
    )
    cache_file = (
        cache_root
        / "scenario_01"
        / "pages"
        / sc.safe_path_part("Page One")
        / "comparison.json"
    )
    screenshot = (
        dashboard_scenario_directory(source_root, "dashboard_A", "01", "Page One")
        / "cached_01_dashboard_A.png"
    )
    changed_time = cache_file.stat().st_mtime + 10
    os.utime(screenshot, (changed_time, changed_time))

    cached = sc._cached_page_result(
        cache_root,
        "01",
        sc.SCENARIOS[0][1],
        sc.SCENARIOS[0][2],
        source_root,
        "Page One",
    )

    assert cached is None


def test_scenario_comparison_marks_failed_pair_unverified_and_continues(
    tmp_path, monkeypatch
):
    source_root = tmp_path / "source"
    output_root = tmp_path / "output"
    pages = ["Page One", "Page Two"]
    _create_comparison_sources(source_root, pages)
    calls = []

    async def compare_one_page(
        scenario_id,
        _scenario_directory,
        _scenario_name,
        _source_root,
        page_names,
        _comparison_root,
        _workspace_root,
        _job_id,
        _operation_log,
        *,
        prepared_pages,
    ):
        page_name = page_names[0]
        calls.append((page_name, scenario_id))
        if page_name == "Page One" and scenario_id == "02":
            raise RuntimeError("vision unavailable")
        page, _ = prepared_pages[page_name]
        page["comparison_status"] = "PASS"
        page["visual_comparison"] = {"status": "PASS"}
        page["findings"].append(
            {
                "category": "MATCH",
                "element_type": "Visual",
                "observation": "The visual matched.",
            }
        )
        return {"pages": [page]}

    monkeypatch.setattr(sc, "_compare_scenario", compare_one_page)
    monkeypatch.setattr(
        sc,
        "_write_docx",
        lambda path, *_args: path.write_bytes(b"docx"),
    )

    result = asyncio.run(
        sc.run_scenario_comparison(
            {scenario_id: source_root for scenario_id, _, _ in sc.SCENARIOS},
            output_root,
        )
    )

    expected_order = [
        (page_name, scenario_id)
        for page_name in pages
        for scenario_id, _, _ in sc.SCENARIOS
    ]
    assert calls == expected_order
    first_page = json.loads(Path(result["page_comparisons"][0]).read_text())
    assert first_page["scenarios"][0]["comparison_status"] == "PASS"
    assert first_page["scenarios"][1]["comparison_status"] == "UNVERIFIED"
    assert "vision unavailable" in first_page["scenarios"][1]["evidence"]["errors"][-1]
    assert len(first_page["scenarios"]) == 3
    assert result["summary"]["total_page_scenarios_processed"] == 6


def test_one_page_comparison_pairs_active_scenarios_from_manifests(
    tmp_path, monkeypatch
):
    source_root = tmp_path / "job"
    page_name = "Page One"
    page_key = sc.safe_path_part(page_name)
    _create_comparison_sources(source_root, [page_name])
    store = EvidenceManifestStore(source_root)
    store.initialize()
    vision_calls = []

    def save_json(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    for dashboard in ("dashboard_A", "dashboard_B"):
        plan_dir = source_root / "planning" / page_key
        save_json(
            plan_dir / f"{dashboard}_plan.json",
            {
                "page_name": page_name,
                "filters": [
                    {
                        "name": "Region",
                        "control_type": "checkbox",
                        "option_inventory_status": "AVAILABLE",
                        "values": ["North", "South"],
                    }
                ],
            },
        )

    for scenario_id, selected, requested in (
        ("02", ["North"], ["North"]),
        ("03", ["North", "South"], ["North", "South"]),
    ):
        save_json(
            scenario_result_directory(source_root, scenario_id, page_name)
            / "scenario_execution.json",
            {
                "steps": [
                    {
                        "record": {
                            "filter": "Region",
                            "requested": requested,
                            "selected": selected,
                            "verified": True,
                        }
                    }
                    for _ in range(2)
                ]
            },
        )
    save_json(
        scenario_result_directory(source_root, "01", page_name)
        / "visual_comparison.json",
        {
            "overall_comparison_status": "PASS",
            "llm_vision_comparison": {
                "status": "PASS",
                "headers_a": ["Region"],
                "headers_b": ["Region"],
                "matching_headers": ["Region"],
            },
        },
    )

    scenario_states = {
        "01": [("table_headers_scroll", "table_headers_scroll:horizontal_scroll_01")],
        "02": [("single_filter", "single_filter:region=north")],
        "03": [("multiple_filter", "multiple_filter:region=north,south")],
    }

    async def create_manifest_entries():
        for scenario_id, states in scenario_states.items():
            for dashboard in ("dashboard_A", "dashboard_B"):
                page_dir = dashboard_scenario_directory(
                    source_root, dashboard, scenario_id, page_name
                )
                page_dir.mkdir(parents=True, exist_ok=True)
                for role, pairing_key in states:
                    screenshot = page_dir / f"{dashboard}_S{scenario_id}_{role}.png"
                    screenshot.write_bytes(f"{dashboard}:{pairing_key}".encode())
                    await store.add(
                        {
                            "dashboard_id": dashboard,
                            "page_name": page_name,
                            "page_identifier": page_key,
                            "page_number": 1,
                            "scenario_id": scenario_id,
                            "scenario_name": dict(
                                (item[0], item[2]) for item in sc.SCENARIOS
                            )[scenario_id],
                            "capture_role": role,
                            "pairing_key": pairing_key,
                            "scroll_position_id": (
                                "initial"
                                if scenario_id == "01" and role == "table_headers_initial"
                                else "horizontal_scroll_01"
                                if scenario_id == "01"
                                else None
                            ),
                            "screenshot_filename": screenshot.name,
                            "screenshot_full_path": str(screenshot.resolve()),
                            "capture_status": "CAPTURED",
                            "execution_json_evidence_path": str(
                                (page_dir / "scenario_execution.json").resolve()
                            ),
                            "comparison_json_evidence_path": str(
                                (
                                    scenario_result_directory(
                                        source_root, scenario_id, page_name
                                    )
                                    / (
                                        "visual_comparison.json"
                                        if scenario_id == "01"
                                        else "comparison_result.json"
                                    )
                                ).resolve()
                            ),
                        }
                    )
                if scenario_id == "01":
                    dashboard_execution_evidence_path(
                        source_root, dashboard, "01", page_name
                    ).write_text(
                        json.dumps(
                            {
                                "screenshots": {
                                    "initial": str(
                                        (
                                            page_dir
                                            / f"01_{dashboard}.png"
                                        ).resolve()
                                    ),
                                    "horizontal_scroll_01": str(
                                        (
                                            page_dir
                                            / f"{dashboard}_S01_table_headers_scroll.png"
                                        ).resolve()
                                    ),
                                }
                            }
                        ),
                        encoding="utf-8",
                    )

    asyncio.run(create_manifest_entries())

    async def compare_screenshots(
        screenshots, pages, scenario_id, _scenario_name, **_kwargs
    ):
        vision_calls.append((scenario_id, screenshots))
        return {
            "page_results": [
                {
                    "page_name": pages[0],
                    "status": "PASS",
                    "matching_elements": ["Matched visual"],
                    "findings": [],
                    "notes": [],
                }
            ]
        }

    monkeypatch.setattr(sc, "compare_scenario_screenshots", compare_screenshots)
    monkeypatch.setattr(
        sc, "_write_docx", lambda path, *_args: path.write_bytes(b"docx")
    )

    result = asyncio.run(
        sc.run_scenario_comparison(
            {scenario_id: source_root for scenario_id, _, _ in sc.SCENARIOS},
            source_root,
        )
    )

    assert [scenario_id for scenario_id, _ in vision_calls] == ["02", "03"]
    assert len(vision_calls[-1][1]) == 4
    assert result["summary"]["total_page_scenarios_processed"] == 3
    page_result = json.loads(Path(result["page_comparisons"][0]).read_text())
    assert len(page_result["scenarios"]) == 3
    assert page_result["scenarios"][0]["comparison_status"] == "PASS"
    assert page_result["scenarios"][2]["visual_comparison"]["status"] == "PASS"
    assert (source_root / "comparison_results" / "scenario_wise_comparison.json").is_file()
    assert (source_root / "comparison_results" / "page_wise_comparison.json").is_file()
    assert (source_root / "comparison_results" / "final_comparison_summary.json").is_file()


def test_compare_dom_headers_matches_identical_sequences():
    res = sc.compare_dom_headers(["Item", "Price", "Qty"], ["Item", "Price", "Qty"])
    assert res["status"] == "PASS"
    assert res["matching_headers"] == ["Item", "Price", "Qty"]
    assert res["missing_headers"] == []
    assert res["additional_headers"] == []


def test_compare_dom_headers_detects_order_differences():
    res = sc.compare_dom_headers(["Item", "Price", "Qty"], ["Price", "Item", "Qty"])
    assert res["status"] == "FAIL"
    assert len(res["differently_ordered_headers"]) == 2
    assert res["missing_headers"] == []
    assert res["additional_headers"] == []


def test_compare_dom_headers_preserves_duplicate_occurrences():
    res = sc.compare_dom_headers(["ID", "Amt", "Amt"], ["ID", "Amt"])
    assert res["status"] == "FAIL"
    assert res["missing_headers"] == ["Amt"]
    assert res["matching_headers"] == ["ID", "Amt"]


def test_select_scenario_screenshot_pairs_honors_requested_cap():
    items_a = [{"label": f"step_{i}", "path": f"a_{i}.png"} for i in range(10)]
    items_b = [{"label": f"step_{i}", "path": f"b_{i}.png"} for i in range(10)]
    selected = sc._select_scenario_screenshot_pairs(items_a, items_b, max_pairs=4)
    assert len(selected) == 4
    assert selected[0][0]["label"] == "step_0"
    assert selected[-1][0]["label"] == "step_9"
    # Ensure no duplicates
    labels = [pair[0]["label"] for pair in selected]
    assert len(labels) == len(set(labels))

    # Scroll screenshots are excluded from selected pairs when exclude_scroll=True
    items_a.append({"label": "Scroll01", "path": "dashboard_A_Scroll01.png"})
    items_b.append({"label": "Scroll01", "path": "dashboard_B_Scroll01.png"})
    selected_with_scroll = sc._select_scenario_screenshot_pairs(items_a, items_b, max_pairs=4, exclude_scroll=True)
    assert not any("Scroll" in p[0]["path"] or "Scroll" in p[0]["label"] for p in selected_with_scroll)

