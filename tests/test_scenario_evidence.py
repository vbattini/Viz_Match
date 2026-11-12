import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from Backend.scenario_evidence import (
    EvidenceManifestStore,
    dashboard_execution_evidence_path,
    organize_scenario_screenshots,
    read_evidence_manifest,
    scenario_result_directory,
)


OLD_SCENARIO_DIRECTORIES = {
    "05": "05_table_column_header_comparison",
    "02": "02_single_filter_selection",
    "03": "03_multiple_filter_selection",
    "04": "04_drilldown",
}
NEW_SCENARIO_DIRECTORIES = {
    "01": "01_table_column_header_comparison",
    "02": "02_single_filter_selection",
    "03": "03_multiple_filter_selection",
    "04": "04_drilldown",
}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_manifest_inventory_excludes_drilldown_initial_screenshots(tmp_path):
    manifest_path = tmp_path / "dashboard_A" / "evidence_manifest.json"
    _write_json(
        manifest_path,
        {
            "dashboard_id": "dashboard_A",
            "screenshots": [
                {
                    "scenario_id": "04",
                    "capture_role": "drilldown_initial",
                    "screenshot_filename": "dashboard_A_S04_drilldown_Page01_Initial.png",
                },
                {
                    "scenario_id": "04",
                    "capture_role": "drilldown_before",
                    "screenshot_filename": "dashboard_A_S04_drilldown_Page01_Before.png",
                },
            ],
        },
    )

    manifest = read_evidence_manifest(tmp_path, "dashboard_A")

    assert [
        item["capture_role"] for item in manifest["screenshots"]
    ] == ["drilldown_before"]


def test_legacy_evidence_is_verified_and_reorganized_into_four_scenarios(tmp_path):
    page_name = "Page One"
    page_key = "Page_One"
    (tmp_path / "validation_summary.json").write_text(
        json.dumps({"pages": [{"page": page_name}]}), encoding="utf-8"
    )
    store = EvidenceManifestStore(tmp_path)
    store.initialize()
    old_screenshots = {}
    old_execution_paths = {}

    for old_scenario_id in ("01", "02", "03", "04", "05"):
        scenario_directory = (
            "01_initial_default_state"
            if old_scenario_id == "01"
            else OLD_SCENARIO_DIRECTORIES[old_scenario_id]
        )
        page_directory = tmp_path / scenario_directory / page_key
        page_directory.mkdir(parents=True, exist_ok=True)
        old_screenshots[old_scenario_id] = {}
        for dashboard_id in ("dashboard_A", "dashboard_B"):
            screenshot = tmp_path / dashboard_id / (
                f"old_S{old_scenario_id}_{dashboard_id}.png"
            )
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            screenshot.write_bytes(f"{old_scenario_id}:{dashboard_id}".encode())
            old_screenshots[old_scenario_id][dashboard_id] = screenshot
            comparison_filename = (
                "visual_comparison.json"
                if old_scenario_id == "05"
                else "comparison_result.json"
            )
            entry = {
                    "dashboard_id": dashboard_id,
                    "page_name": page_name,
                    "page_identifier": page_key,
                    "page_number": 1,
                    "scenario_id": old_scenario_id,
                    "scenario_name": (
                        "Initial Default State"
                        if old_scenario_id == "01"
                        else "Table Column Header Comparison"
                        if old_scenario_id == "05"
                        else f"Scenario {old_scenario_id}"
                    ),
                    "capture_role": "table_headers_initial" if old_scenario_id == "05" else "capture",
                    "pairing_key": "table_headers_initial:initial" if old_scenario_id == "05" else f"state:{old_scenario_id}",
                    "scroll_position_id": "initial" if old_scenario_id == "05" else None,
                    "capture_status": "CAPTURED",
                    "screenshot_filename": screenshot.name,
                    "screenshot_full_path": str(screenshot.resolve()),
                    "filter_selections": [],
                    "execution_json_evidence_path": str(
                        (page_directory / "scenario_execution.json").resolve()
                    ),
                    "comparison_json_evidence_path": str(
                        (page_directory / comparison_filename).resolve()
                    ),
                    "sha256": hashlib.sha256(screenshot.read_bytes()).hexdigest(),
                }
            asyncio.run(store.add(entry))
        if old_scenario_id == "05":
            execution = {
                "dashboard_A": {
                    "screenshots": {
                        "initial": str(old_screenshots["05"]["dashboard_A"].resolve())
                    },
                    "scroll_positions": {"initial": {}},
                },
                "dashboard_B": {
                    "screenshots": {
                        "initial": str(old_screenshots["05"]["dashboard_B"].resolve())
                    },
                    "scroll_positions": {"initial": {}},
                },
            }
            comparison = {
                "dashboard_A_screenshot_paths": {
                    "initial": str(old_screenshots["05"]["dashboard_A"].resolve())
                },
                "dashboard_B_screenshot_paths": {
                    "initial": str(old_screenshots["05"]["dashboard_B"].resolve())
                },
                "llm_vision_comparison": {"status": "PASS"},
            }
        elif old_scenario_id in {"02", "03"}:
            execution = {"steps": [{}, {}], "plan": {}}
            comparison = {
                "screenshots": {
                    side: str(old_screenshots[old_scenario_id][side].resolve())
                    for side in ("dashboard_A", "dashboard_B")
                }
            }
        elif old_scenario_id == "04":
            execution = {"visuals": []}
            comparison = {"screenshots": {}}
        elif old_scenario_id == "01":
            execution = {
                "dashboard_A_screenshot": str(
                    old_screenshots["01"]["dashboard_A"].resolve()
                )
            }
            comparison = {"screenshots": {}}
        else:
            execution = {"steps": []}
            comparison = {"screenshots": {}}
        _write_json(page_directory / "scenario_execution.json", execution)
        _write_json(page_directory / "execution_status.json", {"status": "PASS"})
        _write_json(page_directory / comparison_filename, comparison)
        old_execution_paths[old_scenario_id] = page_directory

    legacy_cache = (
        tmp_path
        / "comparison_results"
        / "scenario_05"
        / "pages"
        / page_key
        / "comparison.json"
    )
    _write_json(
        legacy_cache,
        {
            "scenario_id": "05",
            "scenario_name": "Table and Matrix Headers",
            "page_name": page_name,
            "scenario_execution_status": "PASS",
            "evidence": {
                "source_directory": str(tmp_path.resolve()),
                "scenario_directory": str(
                    (tmp_path / OLD_SCENARIO_DIRECTORIES["05"] / page_key).resolve()
                ),
                "scenario_execution_json": str(
                    (
                        tmp_path
                        / OLD_SCENARIO_DIRECTORIES["05"]
                        / page_key
                        / "scenario_execution.json"
                    ).resolve()
                ),
                "execution_status_json": str(
                    (
                        tmp_path
                        / OLD_SCENARIO_DIRECTORIES["05"]
                        / page_key
                        / "execution_status.json"
                    ).resolve()
                ),
                "comparison_json": str(
                    (
                        tmp_path
                        / OLD_SCENARIO_DIRECTORIES["05"]
                        / page_key
                        / "visual_comparison.json"
                    ).resolve()
                ),
                "dashboard_A_screenshots": [
                    {
                        "label": "initial",
                        "path": str(
                            old_screenshots["05"]["dashboard_A"].resolve()
                        ),
                    }
                ],
                "dashboard_B_screenshots": [
                    {
                        "label": "initial",
                        "path": str(
                            old_screenshots["05"]["dashboard_B"].resolve()
                        ),
                    }
                ],
                "errors": [],
            },
            "visual_comparison": {"status": "PASS"},
        },
    )

    counts = organize_scenario_screenshots(
        {scenario_id: tmp_path for scenario_id in ("01", "02", "03", "04")},
        tmp_path,
    )

    assert counts == {"dashboard_A": 4, "dashboard_B": 4}
    for dashboard_id in ("dashboard_A", "dashboard_B"):
        manifest = read_evidence_manifest(tmp_path, dashboard_id)
        entries = manifest["screenshots"]
        assert {entry["scenario_id"] for entry in entries} == {
            "01",
            "02",
            "03",
            "04",
        }
        for entry in entries:
            screenshot = Path(entry["screenshot_full_path"])
            assert screenshot.is_file()
            assert screenshot.parent == tmp_path / dashboard_id / page_key
            assert entry["screenshot_capture_timestamp"]
            assert Path(entry["execution_json_evidence_path"]).is_file()
            assert Path(entry["execution_json_evidence_path"]) == (
                dashboard_execution_evidence_path(
                    tmp_path, dashboard_id, entry["scenario_id"], page_name
                )
            )
            assert Path(entry["comparison_json_evidence_path"]).is_file()
            assert hashlib.sha256(screenshot.read_bytes()).hexdigest() == entry["sha256"]
        assert not old_screenshots["01"][dashboard_id].exists()
        for scenario_directory in NEW_SCENARIO_DIRECTORIES.values():
            assert not (tmp_path / dashboard_id / scenario_directory).exists()

    migrated_header = (
        tmp_path
        / "planning"
        / "scenario_evidence"
        / "scenario_01"
        / "pages"
        / page_key
        / "visual_comparison.json"
    )
    migrated_data = json.loads(migrated_header.read_text(encoding="utf-8"))
    assert Path(
        migrated_data["dashboard_A_screenshot_paths"]["initial"]
    ).is_file()
    migrated_cache = json.loads(
        (
            scenario_result_directory(tmp_path, "01", page_name)
            / "comparison.json"
        ).read_text(encoding="utf-8")
    )
    assert migrated_cache["scenario_id"] == "01"
    assert migrated_cache["scenario_name"] == "Table Column Header Comparison"
    assert Path(migrated_cache["evidence"]["scenario_directory"]) == (
        scenario_result_directory(tmp_path, "01", page_name).resolve()
    )
    assert Path(
        migrated_cache["evidence"]["dashboard_A_screenshots"][0]["path"]
    ).is_file()
    archived_default_screenshot = (
        tmp_path
        / "planning"
        / "legacy_initial_default_state"
        / "screenshots"
        / "dashboard_A"
        / page_key
        / old_screenshots["01"]["dashboard_A"].name
    )
    assert archived_default_screenshot.is_file()
    archived_default_execution = (
        tmp_path
        / "planning"
        / "legacy_initial_default_state"
        / "execution"
        / page_key
        / "scenario_execution.json"
    )
    archived_execution_data = json.loads(
        archived_default_execution.read_text(encoding="utf-8")
    )
    assert Path(archived_execution_data["dashboard_A_screenshot"]) == (
        archived_default_screenshot.resolve()
    )
    assert not (tmp_path / "01_initial_default_state").exists()
    for old_directory in OLD_SCENARIO_DIRECTORIES.values():
        assert not (tmp_path / old_directory).exists()


def test_manifest_store_records_hierarchical_capture_metadata_and_timestamp(tmp_path):
    events = []

    class _Page:
        async def screenshot(self, *, path, full_page):
            assert full_page
            events.append("screenshot")
            Path(path).write_bytes(b"image")

    from Backend import validation_scenarios as vs

    async def no_wait(*_args):
        events.append("render")

    async def move_pointer_off_dashboard(_page):
        events.append("pointer")

    store = EvidenceManifestStore(tmp_path)
    store.initialize()
    side = vs._Side(
        "dashboard_A",
        "https://example",
        "Page One",
        _Page(),
        tmp_path,
        page_number=2,
        manifest_store=store,
    )

    with (
        patch.object(vs, "_wait_for_state_render", no_wait),
        patch.object(vs, "_move_pointer_off_dashboard", move_pointer_off_dashboard),
    ):
        screenshot = Path(
            asyncio.run(
                vs._capture_scenario_png(
                    side,
                    "02",
                    "Filter01_Region_North",
                    0,
                    capture_role="single_filter",
                    filter_selections=[
                        {"filter_name": "Region", "selected_values": ["North"]}
                    ],
                    combination_id="Filter01",
                )
            )
        )

    assert events == ["render", "pointer", "screenshot"]
    assert screenshot.parent == tmp_path / "dashboard_A" / "Page_One"
    assert "S02_single_filter" in screenshot.name
    assert "Page02" in screenshot.name
    manifest = read_evidence_manifest(tmp_path, "dashboard_A")
    assert len(manifest["screenshots"]) == 1
    entry = manifest["screenshots"][0]
    assert entry["filter_names"] == ["Region"]
    assert entry["selected_filter_values"] == {"Region": ["North"]}
    assert entry["combination_id"] == "Filter01"
    assert entry["screenshot_capture_timestamp"]
    assert entry["capture_status"] == "CAPTURED"
