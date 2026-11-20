"""Compare saved validation-scenario evidence and write JSON and DOCX reports."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import time
from uuid import uuid4
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from openpyxl import load_workbook
from PIL import Image, ImageChops

from .agents import (
    compare_scenario_screenshots,
    compare_table_headers_from_screenshots,
)
from .config import (
    DEFAULT_LOCATION,
    DEFAULT_MODEL_ID,
    DEFAULT_PROJECT_ID,
    configure_vertex_ai,
)
from .scenario_evidence import (
    dashboard_execution_evidence_path,
    dashboard_scenario_directory,
    organize_scenario_screenshots,
    read_evidence_manifest,
    scenario_result_directory,
)
from .json_evidence import read_json_object as _read_json
from .json_evidence import write_json_atomic as _write_json
from .timing import write_operation_event
from .validation_scenarios import normalize_label, safe_path_part

logger = logging.getLogger(__name__)

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_UNVERIFIED = "UNVERIFIED"
STATUS_SKIPPED = "SKIPPED"

COMPARISON_CATEGORIES = {
    "MATCH",
    "MISSING",
    "ADDITIONAL",
    "RENAMED",
    "DIFFERENT_VALUE",
    "DIFFERENT_ORDER",
    "VISUAL_DIFFERENCE",
    "BEHAVIOR_DIFFERENCE",
    "UNVERIFIED",
}
CONFIRMED_DIFFERENCES = COMPARISON_CATEGORIES - {"MATCH", "UNVERIFIED"}

SCENARIOS = (
    ("01", "01_table_column_header_comparison", "Table Column Header and Hierarchy Validation"),
    ("02", "02_single_filter_selection", "Single Filter Selection"),
    ("03", "03_multiple_filter_selection", "Multiple Filter Selection"),
)

DOCX_NAVY = "1F3864"


def _resolve_source(path: Path) -> Path:
    resolved = path.expanduser()
    if not resolved.is_absolute():
        resolved = Path.cwd() / resolved
    return resolved.resolve()


def _resolve_evidence_path(
    value: Any, source_root: Path, workspace_root: Path
) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = Path(value)
    candidates = [raw] if raw.is_absolute() else [workspace_root / raw, source_root / raw]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return candidates[0].resolve() if candidates else None


def _page_names(source_root: Path) -> list[str]:
    summary, _ = _read_json(source_root / "validation_summary.json")
    if summary:
        names = [
            str(page.get("page"))
            for page in summary.get("pages", [])
            if isinstance(page, dict) and page.get("page")
        ]
        if names:
            return names

    names = []
    planning_root = source_root / "planning"
    page_directories = (
        sorted(path for path in planning_root.iterdir() if path.is_dir())
        if planning_root.is_dir()
        else []
    )
    for page_dir in page_directories:
        page_name = None
        data, _ = _read_json(page_dir / "dashboard_A_plan.json")
        if data:
            page_name = data.get("page") or data.get("page_name")
        names.append(str(page_name or page_dir.name.replace("_", " ")))
    return names


def _load_scenario_file(
    source_root: Path, scenario_directory: str, page_name: str, filename: str
) -> tuple[dict[str, Any] | None, str | None, Path]:
    scenario_id = next(
        scenario_id
        for scenario_id, directory, _ in SCENARIOS
        if directory == scenario_directory
    )
    path = scenario_result_directory(source_root, scenario_id, page_name) / filename
    data, error = _read_json(path)
    return data, error, path


def _page_status(
    source_root: Path, scenario_directory: str, page_name: str
) -> tuple[str, str | None, dict[str, Any] | None, Path]:
    scenario_id = next(
        scenario_id
        for scenario_id, directory, _ in SCENARIOS
        if directory == scenario_directory
    )
    page_dir = scenario_result_directory(source_root, scenario_id, page_name)
    data, error = _read_json(page_dir / "execution_status.json")
    if not data:
        return STATUS_UNVERIFIED, error, None, page_dir
    status = str(data.get("status", STATUS_UNVERIFIED)).upper()
    if status not in {
        STATUS_PASS,
        STATUS_FAIL,
        STATUS_UNVERIFIED,
        STATUS_SKIPPED,
    }:
        return STATUS_UNVERIFIED, f"invalid execution status {status!r}", data, page_dir
    return status, data.get("reason"), data, page_dir


def _normal_values(values: Any) -> list[str] | None:
    if values is None:
        return None
    if not isinstance(values, list):
        return [normalize_label(str(values))]
    return sorted(normalize_label(str(value)) for value in values)


def _finding(
    category: str,
    element_type: str,
    observation: str,
    element_a: Any = None,
    element_b: Any = None,
) -> dict[str, Any]:
    return {
        "category": category,
        "element_type": element_type,
        "element_a": None if element_a is None else str(element_a),
        "element_b": None if element_b is None else str(element_b),
        "observation": observation,
    }


def _compare_filter_inventory(
    source_root: Path, page_name: str
) -> dict[str, Any]:
    page_key = safe_path_part(page_name)
    plan_a, error_a = _read_json(
        source_root / "planning" / page_key / "dashboard_A_plan.json"
    )
    plan_b, error_b = _read_json(
        source_root / "planning" / page_key / "dashboard_B_plan.json"
    )
    if not plan_a or not plan_b:
        return {
            "status": STATUS_UNVERIFIED,
            "items": [],
            "available_options_status": STATUS_UNVERIFIED,
            "reason": error_a or error_b or "filter inventory is missing",
        }

    def index_filters(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            normalize_label(str(item.get("name", ""))): item
            for item in plan.get("filters", [])
            if isinstance(item, dict) and item.get("name")
        }

    filters_a, filters_b = index_filters(plan_a), index_filters(plan_b)
    rows = []
    for key in sorted(set(filters_a) | set(filters_b)):
        item_a, item_b = filters_a.get(key), filters_b.get(key)
        if item_a is None:
            rows.append(
                {
                    "category": "ADDITIONAL",
                    "name_a": None,
                    "name_b": item_b.get("name"),
                    "control_type_a": None,
                    "control_type_b": item_b.get("control_type"),
                }
            )
        elif item_b is None:
            rows.append(
                {
                    "category": "MISSING",
                    "name_a": item_a.get("name"),
                    "name_b": None,
                    "control_type_a": item_a.get("control_type"),
                    "control_type_b": None,
                }
            )
        else:
            control_a = item_a.get("control_type")
            control_b = item_b.get("control_type")
            types_match = (
                normalize_label(str(control_a))
                == normalize_label(str(control_b))
                if control_a and control_b
                else None
            )
            rows.append(
                {
                    "category": (
                        "UNVERIFIED"
                        if types_match is None
                        else "MATCH"
                        if types_match
                        else "BEHAVIOR_DIFFERENCE"
                    ),
                    "name_a": item_a.get("name"),
                    "name_b": item_b.get("name"),
                    "control_type_a": control_a,
                    "control_type_b": control_b,
                }
            )
    categories = {row["category"] for row in rows}
    status = (
        STATUS_FAIL
        if categories.intersection(
            {
                "MISSING",
                "ADDITIONAL",
                "VISUAL_DIFFERENCE",
                "BEHAVIOR_DIFFERENCE",
            }
        )
        else STATUS_UNVERIFIED
        if not rows or "UNVERIFIED" in categories
        else STATUS_PASS
    )
    options_available = any(
        item.get("values") or item.get("options")
        for plan in (plan_a, plan_b)
        for item in plan.get("filters", [])
        if isinstance(item, dict)
    )
    return {
        "status": status,
        "items": rows,
        "available_options_status": (
            STATUS_PASS if options_available else STATUS_UNVERIFIED
        ),
        "reason": (
            None
            if options_available
            else "the saved planning evidence contains filter names and control types, but not available option lists"
        ),
    }


def _compare_step_records(
    scenario_id: str,
    execution: dict[str, Any] | None,
) -> dict[str, Any]:
    steps = (execution or {}).get("steps", [])
    if len(steps) < 2:
        return {
            "status": STATUS_UNVERIFIED,
            "state_equivalent": None,
            "expected_state_applied": None,
            "items": [],
            "reason": "Dashboard A and Dashboard B selection records are incomplete",
        }
    step_a, step_b = steps[0], steps[1]
    record_a = step_a.get("record") or {}
    record_b = step_b.get("record") or {}
    if not record_a or not record_b:
        return {
            "status": STATUS_UNVERIFIED,
            "state_equivalent": None,
            "expected_state_applied": None,
            "items": [],
            "reason": "filter selection records are missing",
        }

    items: list[dict[str, Any]] = []
    name_a, name_b = record_a.get("filter"), record_b.get("filter")
    values_a, values_b = _normal_values(record_a.get("selected")), _normal_values(
        record_b.get("selected")
    )
    requests_a, requests_b = _normal_values(record_a.get("requested")), _normal_values(
        record_b.get("requested")
    )
    request_matches_a = requests_a is not None and requests_a == values_a
    request_matches_b = requests_b is not None and requests_b == values_b
    if not name_a or not name_b:
        items.append(
            _finding(
                "UNVERIFIED",
                "Filter",
                "Filter identity is not available on both dashboards.",
                name_a,
                name_b,
            )
        )
    elif normalize_label(str(name_a)) != normalize_label(str(name_b)):
        items.extend(
            [
                _finding("MISSING", "Filter", "Filter is absent on Dashboard B.", name_a),
                _finding(
                    "ADDITIONAL",
                    "Filter",
                    "Filter is present only on Dashboard B.",
                    element_b=name_b,
                ),
            ]
        )
    elif values_a is None or values_b is None:
        items.append(
            _finding(
                "UNVERIFIED",
                "Filter Selection",
                f"Selected values for {name_a} are not available on both dashboards.",
                name_a,
                name_b,
            )
        )
    elif values_a != values_b:
        items.append(
            _finding(
                "DIFFERENT_VALUE",
                "Filter Selection",
                f"Selected values differ for {name_a}.",
                record_a.get("selected"),
                record_b.get("selected"),
            )
        )
    else:
        items.append(
            _finding(
                "MATCH",
                "Filter Selection",
                f"Both dashboards show the same selected value(s) for {name_a}.",
                record_a.get("selected"),
                record_b.get("selected"),
            )
        )

    if requests_a is not None and requests_b is not None and requests_a != requests_b:
        items.append(
            _finding(
                "DIFFERENT_VALUE",
                "Requested Filter State",
                "The recorded requested filter values differ between dashboards.",
                record_a.get("requested"),
                record_b.get("requested"),
            )
        )

    base_a, base_b = record_a.get("base_selection"), record_b.get("base_selection")
    base_state_equivalent = True
    if base_a or base_b:
        if not base_a or not base_b:
            base_state_equivalent = False
            items.append(
                _finding(
                    "UNVERIFIED",
                    "Base Filter Selection",
                    "The retained Scenario 02 selection is not recorded on both dashboards.",
                    (base_a or {}).get("filter"),
                    (base_b or {}).get("filter"),
                )
            )
        else:
            base_values_a = _normal_values(base_a.get("selected"))
            base_values_b = _normal_values(base_b.get("selected"))
            base_state_equivalent = (
                normalize_label(str(base_a.get("filter", "")))
                == normalize_label(str(base_b.get("filter", "")))
                and base_values_a is not None
                and base_values_a == base_values_b
                and _normal_values(base_a.get("requested")) == base_values_a
                and _normal_values(base_b.get("requested")) == base_values_b
                and base_a.get("verified") is True
                and base_b.get("verified") is True
            )
            items.append(
                _finding(
                    "MATCH" if base_state_equivalent else "DIFFERENT_VALUE",
                    "Base Filter Selection",
                    (
                        "The retained Scenario 02 filter state matches."
                        if base_state_equivalent
                        else "The retained Scenario 02 filter state differs."
                    ),
                    f"{base_a.get('filter')}: {base_a.get('selected')}",
                    f"{base_b.get('filter')}: {base_b.get('selected')}",
                )
            )

    state_equivalent = (
        normalize_label(str(name_a or "")) == normalize_label(str(name_b or ""))
        and values_a is not None
        and values_a == values_b
        and base_state_equivalent
    )
    verified_a = record_a.get("verified") is True and request_matches_a
    verified_b = record_b.get("verified") is True and request_matches_b
    expected_state_applied = verified_a and verified_b
    request_count_a = len(requests_a or [])
    request_count_b = len(requests_b or [])
    if scenario_id == "02":
        expected_state_applied = (
            expected_state_applied
            and request_count_a == 1
            and request_count_b == 1
        )
        if not expected_state_applied:
            items.append(
                _finding(
                    "BEHAVIOR_DIFFERENCE"
                    if not request_matches_a
                    or not request_matches_b
                    or record_a.get("verified") is False
                    or record_b.get("verified") is False
                    else "UNVERIFIED",
                    "Filter Action",
                    "The requested single filter value was not verified as the selected value on both dashboards.",
                    record_a.get("requested"),
                    record_b.get("requested"),
                )
            )
    elif scenario_id == "03":
        plan = (execution or {}).get("plan") or {}
        plan_values_a = plan.get("values_a") or []
        plan_values_b = plan.get("values_b") or []
        selected_count_a = len(record_a.get("requested") or [])
        selected_count_b = len(record_b.get("requested") or [])
        has_multi_values = selected_count_a >= 2 and selected_count_b >= 2
        has_chained_selection = bool(base_a and base_b)
        request_shape_valid = (
            request_count_a == request_count_b == 1
            if has_chained_selection
            else 1 <= request_count_a <= 2 and 1 <= request_count_b <= 2
        )
        expected_state_applied = (
            expected_state_applied
            and (has_multi_values or has_chained_selection)
            and request_shape_valid
            and (
                not plan_values_a
                or selected_count_a <= len(plan_values_a)
            )
            and (
                not plan_values_b
                or selected_count_b <= len(plan_values_b)
            )
        )
        if not expected_state_applied:
            items.append(
                _finding(
                    "BEHAVIOR_DIFFERENCE"
                    if record_a.get("verified") is False
                    or record_b.get("verified") is False
                    or not request_matches_a
                    or not request_matches_b
                    or (
                        scenario_id == "02"
                        and (request_count_a != 1 or request_count_b != 1)
                    )
                    else "UNVERIFIED",
                    "Multiple Filter Action",
                    "Evidence does not verify either two selected values in one filter or a verified retained base filter plus an additional selection.",
                    record_a.get("selected"),
                    record_b.get("selected"),
                )
            )
    elif not expected_state_applied:
        category = (
            "BEHAVIOR_DIFFERENCE"
            if record_a.get("verified") is False
            or record_b.get("verified") is False
            or not request_matches_a
            or not request_matches_b
            or (
                scenario_id == "02"
                and (request_count_a != 1 or request_count_b != 1)
            )
            else "UNVERIFIED"
        )
        items.append(
            _finding(
                category,
                "Filter Action",
                "The requested filter selection was not verified on both dashboards.",
                record_a.get("selected"),
                record_b.get("selected"),
            )
        )

    status = (
        STATUS_FAIL
        if any(item["category"] in CONFIRMED_DIFFERENCES for item in items)
        else STATUS_UNVERIFIED
        if not state_equivalent or not expected_state_applied
        else STATUS_PASS
    )
    return {
        "status": status,
        "state_equivalent": state_equivalent,
        "expected_state_applied": expected_state_applied,
        "items": items,
        "dashboard_A": record_a,
        "dashboard_B": record_b,
    }


def _scenario01_position_order(position: str) -> tuple[int, int]:
    if position == "initial":
        return (0, 0)
    prefix = "horizontal_scroll_"
    suffix = position.removeprefix(prefix) if position.startswith(prefix) else ""
    if suffix.isdigit() and int(suffix) > 0:
        return (1, int(suffix))
    raise ValueError(f"Invalid Scenario 01 screenshot position: {position}")


def _scenario01_position_label(value: Any) -> str:
    label = str(value).strip().casefold()
    if "initial" in label:
        return "initial"
    prefix = "horizontal_scroll_"
    start = label.find(prefix)
    if start >= 0:
        suffix = label[start + len(prefix) :]
        if suffix.isdigit() and int(suffix) > 0:
            return f"{prefix}{int(suffix):02d}"
    raise ValueError(f"Invalid Scenario 01 screenshot label: {value}")


def _comparison_screenshots(
    scenario_id: str,
    page_name: str,
    comparison: dict[str, Any] | None,
    source_root: Path,
    workspace_root: Path,
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    sides: dict[str, list[dict[str, Any]]] = {
        "dashboard_A": [],
        "dashboard_B": [],
    }
    errors: list[str] = []
    manifests = {
        side: read_evidence_manifest(source_root, side)
        for side in ("dashboard_A", "dashboard_B")
    }
    manifest_has_scenario = any(
        entry.get("scenario_id") == scenario_id
        and entry.get("page_name") == page_name
        and not (
            scenario_id == "04"
            and entry.get("capture_role") == "drilldown_initial"
        )
        for manifest in manifests.values()
        if manifest is not None
        for entry in manifest["screenshots"]
    )
    if manifest_has_scenario:
        if scenario_id == "01":
            for side, manifest in manifests.items():
                if manifest is None:
                    errors.append(f"{side} evidence manifest is missing")
                    continue
                positions: dict[str, tuple[Path, dict[str, Any]]] = {}
                for entry in manifest["screenshots"]:
                    if (
                        str(entry.get("scenario_id")) != scenario_id
                        or entry.get("page_name") != page_name
                    ):
                        continue
                    role = entry.get("capture_role")
                    if role and role not in {
                        "table_headers_initial",
                        "table_headers_scroll",
                    }:
                        continue
                    if entry.get("capture_status") != "CAPTURED":
                        errors.append(
                            f"{side} screenshot capture was not successful: "
                            f"{entry.get('screenshot_filename')}"
                        )
                        continue
                    position = str(entry.get("scroll_position_id") or "initial")
                    if position != "initial" and not (
                        position.startswith("horizontal_scroll_")
                        and position.removeprefix("horizontal_scroll_").isdigit()
                        and int(position.removeprefix("horizontal_scroll_")) > 0
                    ):
                        errors.append(
                            f"{side} has an invalid Scenario 01 screenshot position: "
                            f"{position}"
                        )
                        continue
                    path = _resolve_evidence_path(
                        entry.get("screenshot_full_path"),
                        source_root,
                        workspace_root,
                    )
                    if path is None or not path.is_file():
                        errors.append(
                            f"{side} manifest screenshot is missing: "
                            f"{entry.get('screenshot_filename')}"
                        )
                        continue
                    previous = positions.get(position)
                    if previous is not None:
                        errors.append(
                            f"{side} has duplicate screenshots for position "
                            f"{position}; only the latest capture is included"
                        )
                        previous_timestamp = str(
                            previous[1].get("screenshot_capture_timestamp") or ""
                        )
                        current_timestamp = str(
                            entry.get("screenshot_capture_timestamp") or ""
                        )
                        if current_timestamp <= previous_timestamp:
                            continue
                    positions[position] = (path, entry)

                if "initial" not in positions:
                    errors.append(
                        f"{side} has no designated Scenario 01 initial screenshot "
                        f"for page {page_name}"
                    )

                execution_path = dashboard_execution_evidence_path(
                    source_root, side, scenario_id, page_name
                )
                execution, execution_error = _read_json(execution_path)
                expected_screenshots = (
                    execution.get("screenshots") if execution else None
                )
                if not isinstance(expected_screenshots, dict):
                    errors.append(
                        f"{side} Scenario 01 execution JSON has no screenshot inventory: "
                        f"{execution_error or execution_path}"
                    )
                else:
                    expected_positions = set(expected_screenshots)
                    actual_positions = set(positions)
                    for missing_position in sorted(
                        expected_positions - actual_positions
                    ):
                        errors.append(
                            f"{side} execution JSON references missing screenshot "
                            f"position {missing_position}"
                        )
                    for extra_position in sorted(
                        actual_positions - expected_positions
                    ):
                        errors.append(
                            f"{side} manifest contains screenshot position "
                            f"{extra_position} absent from execution JSON"
                        )
                    for position in sorted(
                        expected_positions & actual_positions,
                        key=_scenario01_position_order,
                    ):
                        expected_path = _resolve_evidence_path(
                            expected_screenshots[position],
                            source_root,
                            workspace_root,
                        )
                        actual_path = positions[position][0]
                        if (
                            expected_path is None
                            or not expected_path.is_file()
                            or expected_path != actual_path
                        ):
                            errors.append(
                                f"{side} execution JSON and manifest disagree for "
                                f"screenshot position {position}"
                            )

                if not positions:
                    errors.append(
                        f"{side} has no captured screenshots for scenario "
                        f"{scenario_id} on page {page_name}"
                    )
                sides[side] = [
                    {"label": position, "path": str(path)}
                    for position, (path, _) in sorted(
                        positions.items(),
                        key=lambda item: _scenario01_position_order(item[0]),
                    )
                ]
            return sides, errors

        grouped: dict[str, dict[str, list[tuple[Path, dict[str, Any]]]]] = {
            side: {} for side in sides
        }
        for side, manifest in manifests.items():
            if manifest is None:
                errors.append(f"{side} evidence manifest is missing")
                continue
            for entry in manifest["screenshots"]:
                if (
                    str(entry.get("scenario_id")) != scenario_id
                    or entry.get("page_name") != page_name
                ):
                    continue
                if entry.get("capture_status") != "CAPTURED":
                    errors.append(
                        f"{side} screenshot capture was not successful: "
                        f"{entry.get('screenshot_filename')}"
                    )
                    continue
                pairing_key = entry.get("pairing_key")
                path = _resolve_evidence_path(
                    entry.get("screenshot_full_path"), source_root, workspace_root
                )
                if not pairing_key or path is None or not path.is_file():
                    errors.append(
                        f"{side} manifest screenshot is missing or incomplete: "
                        f"{entry.get('screenshot_filename')}"
                    )
                    continue
                grouped[side].setdefault(str(pairing_key), []).append((path, entry))
        for records in grouped.values():
            for items in records.values():
                items.sort(key=lambda item: item[0].name)
        for side in ("dashboard_A", "dashboard_B"):
            if not grouped[side]:
                errors.append(
                    f"{side} has no captured screenshots for scenario "
                    f"{scenario_id} on page {page_name}"
                )
            other_side = "dashboard_B" if side == "dashboard_A" else "dashboard_A"
            for key, items in grouped[side].items():
                if key not in grouped[other_side]:
                    errors.append(f"unpaired {side} screenshot state: {key}")
        for side in ("dashboard_A", "dashboard_B"):
            other_side = "dashboard_B" if side == "dashboard_A" else "dashboard_A"
            for key in sorted(set(grouped[side]) & set(grouped[other_side])):
                items = grouped[side][key]
                other_items = grouped[other_side][key]
                pair_count = min(len(items), len(other_items))
                for index in range(pair_count):
                    label = key
                    if pair_count > 1:
                        label = f"{key} capture {index + 1:02d}"
                    sides[side].append(
                        {"label": label, "path": str(items[index][0])}
                    )
                if len(items) != len(other_items):
                    errors.append(
                        f"unpaired duplicate screenshots for state {key}: "
                        f"{side}={len(items)}, {other_side}={len(other_items)}"
                    )
        return sides, errors

    if not comparison:
        return sides, ["comparison evidence and screenshot manifests are missing"]

    if scenario_id == "01":
        keys = (
            ("dashboard_A", "dashboard_A_screenshot_paths"),
            ("dashboard_B", "dashboard_B_screenshot_paths"),
        )
        for side, key in keys:
            for label, value in ((comparison.get(key) or {}).items()):
                if Path(str(value)).name.casefold() == "initial.png":
                    continue
                path = _resolve_evidence_path(value, source_root, workspace_root)
                if path and path.is_file():
                    position = _scenario01_position_label(label)
                    sides[side].append(
                        {"label": position, "path": str(path)}
                    )
                else:
                    errors.append(f"{side} screenshot is missing: {value}")
            sides[side].sort(
                key=lambda item: _scenario01_position_order(item["label"])
            )
        return sides, errors

    if scenario_id == "04":
        for visual in comparison.get("visuals", []):
            if not isinstance(visual, dict):
                continue
            label = str(visual.get("visual") or visual.get("title") or "visual")
            result_a = visual.get("dashboard_A") or {}
            result_b = visual.get("dashboard_B") or {}
            expand_a = result_a.get("expand_all") or {}
            expand_b = result_b.get("expand_all") or {}
            expand_path_a = _resolve_evidence_path(
                expand_a.get("final_screenshot"), source_root, workspace_root
            )
            expand_path_b = _resolve_evidence_path(
                expand_b.get("final_screenshot"), source_root, workspace_root
            )
            if expand_path_a and expand_path_a.is_file() and expand_path_b and expand_path_b.is_file():
                sides["dashboard_A"].append(
                    {"label": f"{label} expand all", "path": str(expand_path_a)}
                )
                sides["dashboard_B"].append(
                    {"label": f"{label} expand all", "path": str(expand_path_b)}
                )
            elif expand_a.get("final_screenshot") or expand_b.get("final_screenshot"):
                errors.append(f"unpaired expand-all screenshot for {label}")
            levels_a = {
                str(step.get("level")): step
                for step in result_a.get("next_levels", [])
                if isinstance(step, dict)
            }
            levels_b = {
                str(step.get("level")): step
                for step in result_b.get("next_levels", [])
                if isinstance(step, dict)
            }
            for level in sorted(set(levels_a) & set(levels_b), key=lambda x: int(x)):
                path_a = _resolve_evidence_path(
                    levels_a[level].get("screenshot"), source_root, workspace_root
                )
                path_b = _resolve_evidence_path(
                    levels_b[level].get("screenshot"), source_root, workspace_root
                )
                if path_a and path_a.is_file() and path_b and path_b.is_file():
                    screenshot_label = f"{label} next level {level}"
                    sides["dashboard_A"].append(
                        {"label": screenshot_label, "path": str(path_a)}
                    )
                    sides["dashboard_B"].append(
                        {"label": screenshot_label, "path": str(path_b)}
                    )
                elif levels_a[level].get("screenshot") or levels_b[level].get("screenshot"):
                    errors.append(f"unpaired next-level screenshot for {label} level {level}")
            steps_a = {
                str(step.get("level")): step
                for step in result_a.get("steps", [])
                if isinstance(step, dict)
            }
            steps_b = {
                str(step.get("level")): step
                for step in result_b.get("steps", [])
                if isinstance(step, dict)
            }
            for level in sorted(set(steps_a) & set(steps_b), key=lambda x: int(x)):
                level_label = f"{label} level {level}"
                for phase, key in (
                    ("before", "before_screenshot"),
                    ("after", "after_screenshot"),
                ):
                    path_a = _resolve_evidence_path(
                        steps_a[level].get(key), source_root, workspace_root
                    )
                    path_b = _resolve_evidence_path(
                        steps_b[level].get(key), source_root, workspace_root
                    )
                    if path_a and path_a.is_file() and path_b and path_b.is_file():
                        screenshot_label = f"{level_label} {phase}"
                        sides["dashboard_A"].append(
                            {"label": screenshot_label, "path": str(path_a)}
                        )
                        sides["dashboard_B"].append(
                            {"label": screenshot_label, "path": str(path_b)}
                        )
                    elif steps_a[level].get(key) or steps_b[level].get(key):
                        errors.append(
                            f"unpaired drill screenshot for {visual.get('visual')} level {level} {phase}"
                        )
        return sides, errors

    paths = comparison.get("screenshots") or {}
    if not paths.get("dashboard_A") or not paths.get("dashboard_B"):
        errors.append("a corresponding screenshot pair is unavailable")
    for side in ("dashboard_A", "dashboard_B"):
        value = paths.get(side)
        path = _resolve_evidence_path(value, source_root, workspace_root)
        if path and path.is_file():
            sides[side].append({"label": "state", "path": str(path)})
        elif value:
            errors.append(f"{side} screenshot is missing: {value}")
    return sides, errors


def _filter_state_description(filter_comparison: dict[str, Any]) -> str:
    items = filter_comparison.get("items") or []
    descriptions = []
    for item in items:
        if "filter_key" in item:
            descriptions.append(
                f"{item.get('name_a') or item.get('name_b')}: "
                f"A={item.get('selected_a')}, B={item.get('selected_b')}"
            )
        elif item.get("element_type") in {
            "Filter Selection",
            "Base Filter Selection",
        }:
            descriptions.append(item["observation"])
    return "; ".join(descriptions) or "No verified filter state was recorded."


def _filter_findings(filter_comparison: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for item in filter_comparison.get("items", []):
        if not isinstance(item, dict):
            continue
        if item.get("filter_key") is not None:
            findings.append(
                _finding(
                    item.get("category", "UNVERIFIED"),
                    "Default Filter Selection",
                    (
                        f"Recorded default values: A={item.get('selected_a')}; "
                        f"B={item.get('selected_b')}."
                    ),
                    item.get("name_a"),
                    item.get("name_b"),
                )
            )
        elif item.get("element_type"):
            findings.append(
                _finding(
                    item.get("category", "UNVERIFIED"),
                    item.get("element_type", "Filter"),
                    item.get("observation", "Filter comparison from saved evidence."),
                    item.get("element_a"),
                    item.get("element_b"),
                )
            )

    inventory = filter_comparison.get("inventory") or {}
    for item in inventory.get("items", []):
        category = item.get("category", "UNVERIFIED")
        findings.append(
            _finding(
                category,
                "Filter Control",
                (
                    f"Control types: A={item.get('control_type_a') or 'not recorded'}; "
                    f"B={item.get('control_type_b') or 'not recorded'}."
                ),
                item.get("name_a"),
                item.get("name_b"),
            )
        )
    if inventory.get("available_options_status") == STATUS_UNVERIFIED:
        findings.append(
            _finding(
                "UNVERIFIED",
                "Available Filter Options",
                inventory.get("reason")
                or "Available filter options are not in the saved evidence.",
            )
        )
    return findings


def _status_from_results(
    scenario_id: str,
    execution_status: str,
    filter_comparison: dict[str, Any],
    visual_comparison: dict[str, Any] | None,
    evidence_errors: list[str] | None = None,
) -> str:
    if execution_status == STATUS_SKIPPED and scenario_id != "01":
        return STATUS_UNVERIFIED if evidence_errors else STATUS_SKIPPED
    categories = {
        item.get("category")
        for item in filter_comparison.get("items", [])
        if isinstance(item, dict)
    }
    categories.update(
        item.get("category")
        for item in (filter_comparison.get("inventory") or {}).get("items", [])
        if isinstance(item, dict)
    )
    if categories.intersection(CONFIRMED_DIFFERENCES):
        return STATUS_FAIL
    if execution_status == STATUS_FAIL:
        return STATUS_FAIL
    if (visual_comparison or {}).get("status") == STATUS_FAIL:
        return STATUS_FAIL
    if evidence_errors:
        return STATUS_UNVERIFIED
    if scenario_id == "01":
        status = (visual_comparison or {}).get("status")
        return status if status in {
            STATUS_PASS,
            STATUS_FAIL,
            STATUS_UNVERIFIED,
            STATUS_SKIPPED,
        } else STATUS_UNVERIFIED
    if visual_comparison and visual_comparison.get("status") == STATUS_FAIL:
        return STATUS_FAIL
    if (
        execution_status == STATUS_UNVERIFIED
        or filter_comparison.get("status") == STATUS_UNVERIFIED
        or filter_comparison.get("inventory", {}).get(
            "available_options_status"
        ) == STATUS_UNVERIFIED
        or not visual_comparison
        or visual_comparison.get("status") != STATUS_PASS
    ):
        return STATUS_UNVERIFIED
    return STATUS_PASS


def _select_scenario_screenshot_pairs(
    items_a: list[dict[str, Any]],
    items_b: list[dict[str, Any]],
    max_pairs: int = 4,
    exclude_scroll: bool = False,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    matched_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    b_by_label = {item["label"]: item for item in items_b if item.get("label")}
    for item_a in items_a:
        label = item_a.get("label") or ""
        path_a = str(item_a.get("path") or "")
        if exclude_scroll and ("Scroll" in Path(path_a).name or "Scroll" in label):
            continue
        if label in b_by_label:
            item_b = b_by_label[label]
            path_b = str(item_b.get("path") or "")
            if exclude_scroll and ("Scroll" in Path(path_b).name or "Scroll" in str(item_b.get("label") or "")):
                continue
            matched_pairs.append((item_a, item_b))
    if len(matched_pairs) <= max_pairs:
        return matched_pairs
    selected = [matched_pairs[0]]
    step_indices = [
        int(round(i * (len(matched_pairs) - 1) / (max_pairs - 1)))
        for i in range(1, max_pairs)
    ]
    for idx in dict.fromkeys(step_indices):
        if idx < len(matched_pairs) and matched_pairs[idx] not in selected:
            selected.append(matched_pairs[idx])
    return selected[:max_pairs]


def compare_dom_headers(
    headers_a: list[str], headers_b: list[str]
) -> dict[str, Any]:
    def norm(h: Any) -> str:
        return " ".join(str(h).split())

    norm_a = [norm(h) for h in headers_a if str(h).strip()]
    norm_b = [norm(h) for h in headers_b if str(h).strip()]

    if not norm_a and not norm_b:
        return {
            "status": STATUS_UNVERIFIED,
            "tables_found_a": None,
            "tables_found_b": None,
            "headers_a": [],
            "headers_b": [],
            "matching_headers": [],
            "missing_headers": [],
            "additional_headers": [],
            "renamed_headers": [],
            "differently_ordered_headers": [],
            "notes": ["No table column headers found in DOM on either dashboard."],
        }

    count_a = Counter(norm_a)
    count_b = Counter(norm_b)

    matching = []
    for h, c in (count_a & count_b).items():
        matching.extend([h] * c)

    missing = []
    for h, c in (count_a - count_b).items():
        missing.extend([h] * c)

    additional = []
    for h, c in (count_b - count_a).items():
        additional.extend([h] * c)

    order_diffs = []
    if count_a == count_b and norm_a != norm_b:
        for idx, (a, b) in enumerate(zip(norm_a, norm_b)):
            if a != b:
                order_diffs.append({
                    "header": a,
                    "position_a": idx + 1,
                    "position_b": norm_b.index(a) + 1 if a in norm_b else None,
                })

    status = STATUS_PASS if norm_a == norm_b else STATUS_FAIL

    return {
        "status": status,
        "tables_found_a": True if norm_a else False,
        "tables_found_b": True if norm_b else False,
        "headers_a": norm_a,
        "headers_b": norm_b,
        "matching_headers": matching,
        "missing_headers": missing,
        "additional_headers": additional,
        "renamed_headers": [],
        "differently_ordered_headers": order_diffs,
        "notes": [
            "DOM table column headers evaluated directly from live DOM without LLM extraction."
        ],
    }


def _make_vision_inputs(
    page_name: str, screenshot_sides: dict[str, list[dict[str, Any]]]
) -> list[tuple[str, bytes]]:
    items_a = screenshot_sides.get("dashboard_A") or []
    items_b = screenshot_sides.get("dashboard_B") or []
    selected_pairs = _select_scenario_screenshot_pairs(
        items_a, items_b, max_pairs=4, exclude_scroll=True
    )
    if not selected_pairs:
        return []
    screenshots = []
    for item_a, item_b in selected_pairs:
        descriptor = item_a["label"]
        screenshots.extend(
            [
                (
                    f"Page: {page_name} | {descriptor} | Dashboard A",
                    Path(item_a["path"]).read_bytes(),
                ),
                (
                    f"Page: {page_name} | {descriptor} | Dashboard B",
                    Path(item_b["path"]).read_bytes(),
                ),
            ]
        )
    return screenshots


def _header_vision_inputs(
    screenshot_sides: dict[str, list[dict[str, Any]]],
) -> list[tuple[str, bytes]]:
    screenshots: list[tuple[str, bytes]] = []
    for side, label in (("dashboard_A", "A"), ("dashboard_B", "B")):
        for item in sorted(
            screenshot_sides[side],
            key=lambda screenshot: _scenario01_position_order(
                str(screenshot["label"])
            ),
        ):
            screenshots.append(
                (
                    f"Dashboard {label} {item['label']}",
                    Path(item["path"]).read_bytes(),
                )
            )
    return screenshots


def _dom_header_evidence(
    source_root: Path,
    side: str,
    page_name: str,
    initial_screenshot: str | None,
    workspace_root: Path,
) -> dict[str, Any]:
    evidence_path = source_root / side / "extracted.json"
    result: dict[str, Any] = {
        "status": STATUS_UNVERIFIED,
        "path": str(evidence_path.resolve()),
        "headers": [],
    }
    if not initial_screenshot:
        result["reason"] = "designated initial screenshot is missing"
        return result
    if not evidence_path.is_file():
        result["reason"] = "DOM extraction JSON is missing"
        return result
    try:
        records = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result["reason"] = f"DOM extraction JSON could not be read: {exc}"
        return result
    if not isinstance(records, list):
        result["reason"] = "DOM extraction JSON does not contain a record list"
        return result

    record = next(
        (
            item
            for item in records
            if isinstance(item, dict)
            and item.get("dashboard") == side
            and item.get("page") == page_name
        ),
        None,
    )
    if record is None:
        result["reason"] = f"DOM extraction has no record for {page_name}"
        return result
    dom_screenshot = _resolve_evidence_path(
        record.get("screenshot"), source_root, workspace_root
    )
    designated_screenshot = Path(initial_screenshot).resolve()
    if (
        dom_screenshot is None
        or not dom_screenshot.is_file()
        or not designated_screenshot.is_file()
    ):
        result["reason"] = "DOM and designated initial screenshots are unavailable"
        return result
    try:
        with Image.open(dom_screenshot) as dom_image, Image.open(
            designated_screenshot
        ) as designated_image:
            same_pixels = (
                dom_image.size == designated_image.size
                and ImageChops.difference(
                    dom_image.convert("RGB"), designated_image.convert("RGB")
                ).getbbox()
                is None
            )
    except OSError as exc:
        result["reason"] = f"Screenshot pixels could not be compared: {exc}"
        return result
    if not same_pixels:
        result["reason"] = (
            "DOM extraction screenshot is not pixel-identical to the designated "
            "Scenario 01 initial screenshot"
        )
        return result

    extracted = record.get("extracted") or {}
    title_items = extracted.get("title_items") or []
    headers = []
    for item in title_items:
        if (
            not isinstance(item, dict)
            or str(item.get("category") or "").casefold() != "column_header"
            or item.get("extraction_status") != "complete"
            or "dom_accessibility" not in (item.get("evidence_sources") or [])
        ):
            continue
        bounds = item.get("bounding_box") or {}
        if bounds.get("width", 0) < 3 or bounds.get("height", 0) < 3:
            continue
        text = " ".join(str(item.get("text") or "").split())
        if text:
            headers.append(
                (
                    item.get("reading_order")
                    if isinstance(item.get("reading_order"), (int, float))
                    else bounds.get("x", 0),
                    text,
                )
            )
    headers.sort(key=lambda item: item[0])
    result.update(
        {
            "status": "VERIFIED",
            "screenshot": str(dom_screenshot),
            "headers": [header for _, header in headers],
        }
    )
    return result


def _scenario01_result(
    comparison: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not comparison:
        return {"status": STATUS_UNVERIFIED}, [
            _finding(
                "UNVERIFIED",
                "Table Headers",
                "Scenario 01 visual comparison evidence is missing.",
            )
        ]
    vision = comparison.get("llm_vision_comparison") or {}
    findings: list[dict[str, Any]] = []
    headers_a = list(vision.get("headers_a") or [])
    headers_b = list(vision.get("headers_b") or [])

    def normalize_header(value: Any) -> str:
        return " ".join(str(value).split())

    normalized_a = [normalize_header(value) for value in headers_a]
    normalized_b = [normalize_header(value) for value in headers_b]

    def unmatched(source: list[str], target: list[str]) -> list[str]:
        target_counts = Counter(normalize_header(value) for value in target)
        result = []
        for value in source:
            normalized = normalize_header(value)
            if target_counts[normalized]:
                target_counts[normalized] -= 1
            else:
                result.append(value)
        return result

    missing_headers = unmatched(headers_a, headers_b)
    additional_headers = unmatched(headers_b, headers_a)
    reported_status = str(vision.get("status") or "").upper()
    tables_found_a = vision.get("tables_found_a")
    tables_found_b = vision.get("tables_found_b")
    notes = list(vision.get("notes") or [])
    if tables_found_a is False and tables_found_b is False:
        status = STATUS_UNVERIFIED
        if not notes:
            notes.append(
                "Table headers are not applicable because neither dashboard "
                "shows a table or matrix."
            )
    elif tables_found_a is False or tables_found_b is False:
        status = STATUS_FAIL if reported_status == STATUS_FAIL else STATUS_UNVERIFIED
        if not notes:
            notes.append(
                "A table or matrix was not confirmed on both dashboards."
            )
    elif not headers_a or not headers_b:
        status = STATUS_UNVERIFIED
        if not notes:
            notes.append(
                "A complete table-header sequence could not be verified on "
                "both dashboards."
            )
    elif reported_status == STATUS_UNVERIFIED:
        status = STATUS_UNVERIFIED
    elif normalized_a == normalized_b:
        status = (
            STATUS_PASS
            if reported_status == STATUS_PASS
            else STATUS_UNVERIFIED
        )
        if status == STATUS_UNVERIFIED and not notes:
            notes.append(
                "The reported status conflicts with the identical complete header sequences."
            )
    else:
        status = (
            STATUS_FAIL
            if reported_status in {STATUS_FAIL, STATUS_PASS}
            else STATUS_UNVERIFIED
        )
        if status == STATUS_UNVERIFIED and not notes:
            notes.append("Header evidence is incomplete or ambiguous.")

    for header in vision.get("matching_headers", []):
        findings.append(
            _finding("MATCH", "Table Header", "Header matched by LLM Vision.", header, header)
        )
    renamed_headers = list(vision.get("renamed_headers") or [])
    order_differences = list(vision.get("differently_ordered_headers") or [])
    if (
        not order_differences
        and normalized_a != normalized_b
        and Counter(normalized_a) == Counter(normalized_b)
    ):
        positions_b: dict[str, list[int]] = {}
        for index, header in enumerate(normalized_b, start=1):
            positions_b.setdefault(header, []).append(index)
        occurrences: Counter[str] = Counter()
        for position_a, (header_a, normalized) in enumerate(
            zip(headers_a, normalized_a), start=1
        ):
            occurrence = occurrences[normalized]
            occurrences[normalized] += 1
            position_b = positions_b[normalized][occurrence]
            if position_a != position_b:
                order_differences.append(
                    {
                        "header": header_a,
                        "position_a": position_a,
                        "position_b": position_b,
                    }
                )
    if status == STATUS_FAIL:
        if tables_found_a is False and tables_found_b is True:
            findings.append(
                _finding(
                    "MISSING",
                    "Table",
                    "Dashboard A does not show the table or matrix found on Dashboard B.",
                )
            )
        elif tables_found_a is True and tables_found_b is False:
            findings.append(
                _finding(
                    "MISSING",
                    "Table",
                    "Dashboard B does not show the table or matrix found on Dashboard A.",
                )
            )
        for header in missing_headers:
            findings.append(
                _finding(
                    "MISSING",
                    "Table Header",
                    f"Dashboard B is missing the {header!r} column.",
                    header,
                )
            )
        for header in additional_headers:
            findings.append(
                _finding(
                    "ADDITIONAL",
                    "Table Header",
                    f"Dashboard B contains an additional {header!r} column.",
                    element_b=header,
                )
            )
        for item in renamed_headers:
            header_a = item.get("header_a")
            header_b = item.get("header_b")
            findings.append(
                _finding(
                    "RENAMED",
                    "Table Header",
                    f"Column name differs: Dashboard A {header_a!r}; "
                    f"Dashboard B {header_b!r}.",
                    header_a,
                    header_b,
                )
            )
        for item in order_differences:
            header = item.get("header")
            position_a = item.get("position_a")
            position_b = item.get("position_b")
            findings.append(
                _finding(
                    "DIFFERENT_ORDER",
                    "Table Header",
                    f"{header!r} appears at position {position_a} on Dashboard A "
                    f"and position {position_b} on Dashboard B.",
                    header,
                    header,
                )
            )

    if status == STATUS_UNVERIFIED and not any(
        item["category"] == "UNVERIFIED" for item in findings
    ):
        findings.append(
            _finding(
                "UNVERIFIED",
                "Table Headers",
                "; ".join(notes)
                or "Table headers could not be reliably compared from the available evidence.",
            )
        )
    return {
        "status": status,
        "tables_found_a": tables_found_a,
        "tables_found_b": tables_found_b,
        "headers_a": headers_a,
        "headers_b": headers_b,
        "matching_headers": vision.get("matching_headers", []),
        "missing_headers": missing_headers,
        "additional_headers": additional_headers,
        "renamed_headers": renamed_headers,
        "differently_ordered_headers": order_differences,
        "notes": notes,
        "source_result": vision,
    }, findings


def _prepare_page(
    scenario_id: str,
    scenario_directory: str,
    source_root: Path,
    page_name: str,
    workspace_root: Path,
    *,
    compare: bool = True,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    execution_status, reason, _, page_dir = _page_status(
        source_root, scenario_directory, page_name
    )
    execution, execution_error, execution_path = _load_scenario_file(
        source_root, scenario_directory, page_name, "scenario_execution.json"
    )
    comparison_filename = (
        "visual_comparison.json" if scenario_id == "01" else "comparison_result.json"
    )
    comparison, comparison_error, comparison_path = _load_scenario_file(
        source_root, scenario_directory, page_name, comparison_filename
    )
    screenshots, screenshot_errors = _comparison_screenshots(
        scenario_id, page_name, comparison, source_root, workspace_root
    )
    evidence_errors = [
        error
        for error in (execution_error, comparison_error)
        if error and not (scenario_id == "01" and comparison is None)
    ]
    evidence_errors.extend(screenshot_errors)
    if execution_status == STATUS_UNVERIFIED and reason:
        evidence_errors.append(reason)
    if (
        scenario_id == "01"
        and execution_status == STATUS_SKIPPED
        and all(
            any(item.get("label") == "initial" for item in screenshots[side])
            for side in ("dashboard_A", "dashboard_B")
        )
    ):
        execution_status = STATUS_PASS
        reason = "Initial screenshots were captured; horizontal scrolling was not applicable."

    page_result = {
        "page_name": page_name,
        "scenario_execution_status": execution_status,
        "scenario_execution_reason": reason,
        "comparison_status": execution_status,
        "filter_comparison": {
            "status": "PENDING",
            "state_equivalent": None,
            "items": [],
        },
        "visual_comparison": None,
        "findings": [],
        "evidence": {
            "source_directory": str(source_root),
            "scenario_directory": str(page_dir.resolve()),
            "execution_status_json": str(
                (page_dir / "execution_status.json").resolve()
            ),
            "scenario_execution_json": str(execution_path.resolve()),
            "comparison_json": (
                str(comparison_path.resolve()) if comparison_path.is_file() else None
            ),
            "dashboard_A_screenshots": screenshots["dashboard_A"],
            "dashboard_B_screenshots": screenshots["dashboard_B"],
            "errors": evidence_errors,
        },
        "_scenario_execution": execution,
        "_source_comparison": comparison,
        "_screenshot_errors": screenshot_errors,
    }
    if compare:
        _complete_page_comparison(
            scenario_id, page_name, source_root, page_result
        )
    return page_result, screenshots


def _complete_page_comparison(
    scenario_id: str,
    page_name: str,
    source_root: Path,
    page: dict[str, Any],
) -> None:
    execution = page.get("_scenario_execution")
    comparison = page.get("_source_comparison")
    inventory = (
        _compare_filter_inventory(source_root, page_name)
        if scenario_id in {"02", "03"}
        else None
    )
    if scenario_id in {"02", "03"}:
        filter_comparison = _compare_step_records(scenario_id, execution)
        filter_comparison["inventory"] = inventory
    else:
        filter_comparison = {
            "status": "NOT_APPLICABLE",
            "state_equivalent": None,
            "items": [],
        }

    findings: list[dict[str, Any]] = []
    visual_comparison = None
    if scenario_id == "01":
        visual_comparison, findings = _scenario01_result(comparison)
        if page.get("_screenshot_errors") and visual_comparison.get("status") in {
            STATUS_PASS,
            STATUS_FAIL,
        }:
            visual_comparison = {
                **visual_comparison,
                "status": STATUS_UNVERIFIED,
                "reason": "one or more comparison screenshots are unavailable",
            }
    page["filter_comparison"] = filter_comparison
    page["visual_comparison"] = visual_comparison
    page["findings"] = findings


def _unverified_page(
    scenario_id: str,
    scenario_directory: str,
    source_root: Path,
    page_name: str,
    error: Exception,
) -> dict[str, Any]:
    page_dir = scenario_result_directory(source_root, scenario_id, page_name)
    reason = f"{type(error).__name__}: {error}"
    return {
        "page_name": page_name,
        "scenario_execution_status": STATUS_UNVERIFIED,
        "scenario_execution_reason": reason,
        "comparison_status": STATUS_UNVERIFIED,
        "filter_comparison": {
            "status": STATUS_UNVERIFIED,
            "state_equivalent": None,
            "items": [],
            "reason": "Scenario evidence could not be prepared.",
        },
        "visual_comparison": {
            "status": STATUS_UNVERIFIED,
            "reason": reason,
        },
        "findings": [
            _finding(
                "UNVERIFIED",
                "Scenario Comparison",
                f"Evidence preparation failed: {reason}",
            )
        ],
        "evidence": {
            "source_directory": str(source_root),
            "scenario_directory": str(page_dir.resolve()),
            "execution_status_json": str(
                (page_dir / "execution_status.json").resolve()
            ),
            "scenario_execution_json": str(
                (page_dir / "scenario_execution.json").resolve()
            ),
            "comparison_json": str(
                (
                    page_dir
                    / (
                        "visual_comparison.json"
                        if scenario_id == "01"
                        else "comparison_result.json"
                    )
                ).resolve()
            ),
            "dashboard_A_screenshots": [],
            "dashboard_B_screenshots": [],
            "errors": [f"evidence preparation failed: {reason}"],
        },
    }


def _visual_result_findings(result: dict[str, Any]) -> list[dict[str, Any]]:
    findings = [
        _finding(
            "MATCH",
            "Visual",
            f"LLM Vision reported a match for {element}.",
            element,
            element,
        )
        for element in result.get("matching_elements", [])
    ]
    findings.extend(
        item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
        for item in result.get("findings", [])
    )
    return findings


def _scenario_summary(pages: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts = Counter(page.get("comparison_status") for page in pages)
    category_counts = Counter(
        finding.get("category")
        for page in pages
        for finding in page.get("findings", [])
    )
    return {
        "page_count": len(pages),
        "status_counts": {
            status: status_counts.get(status, 0)
            for status in (
                STATUS_PASS,
                STATUS_FAIL,
                STATUS_UNVERIFIED,
                STATUS_SKIPPED,
            )
        },
        "category_counts": {
            category: category_counts.get(category, 0)
            for category in sorted(COMPARISON_CATEGORIES)
        },
    }


def _strip_internal_fields(page: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in page.items() if not key.startswith("_")}


def _write_page_result(
    comparison_root: Path,
    scenario_id: str,
    scenario_name: str,
    page: dict[str, Any],
) -> None:
    page_path = (
        comparison_root
        / f"scenario_{scenario_id}"
        / "pages"
        / safe_path_part(page["page_name"])
        / "comparison.json"
    )
    evidence = page.setdefault("evidence", {})
    source_comparison = evidence.get("comparison_json")
    if source_comparison and Path(source_comparison).resolve() != page_path.resolve():
        evidence.setdefault("source_comparison_json", source_comparison)
    evidence["comparison_json"] = str(page_path.resolve())
    _write_json(
        page_path,
        {
            "scenario_id": scenario_id,
            "scenario_name": scenario_name,
            **_strip_internal_fields(page),
        },
    )


def _write_scenario_result(
    comparison_root: Path,
    scenario_id: str,
    scenario_name: str,
    source_root: Path,
    pages: list[dict[str, Any]],
) -> None:
    _write_json(
        comparison_root / f"scenario_{scenario_id}" / "comparison.json",
        {
            "scenario_id": scenario_id,
            "scenario_name": scenario_name,
            "source_directory": str(source_root),
            "pages": [_strip_internal_fields(page) for page in pages],
            "summary": _scenario_summary(pages),
        },
    )


def _cached_page_result(
    cache_root: Path,
    scenario_id: str,
    scenario_directory: str,
    scenario_name: str,
    source_root: Path,
    page_name: str,
) -> dict[str, Any] | None:
    cache_path = (
        cache_root
        / f"scenario_{scenario_id}"
        / "pages"
        / safe_path_part(page_name)
        / "comparison.json"
    )
    cached, error = _read_json(cache_path)
    if error or not cached:
        return None
    expected_page_dir = scenario_result_directory(
        source_root, scenario_id, page_name
    )
    evidence = cached.get("evidence") or {}
    cached_source = evidence.get("source_directory")
    cached_page_dir = evidence.get("scenario_directory")
    if (
        cached.get("scenario_id") != scenario_id
        or cached.get("scenario_name") != scenario_name
        or cached.get("page_name") != page_name
        or not isinstance(cached_source, str)
        or not isinstance(cached_page_dir, str)
        or Path(cached_source).resolve() != source_root.resolve()
        or Path(cached_page_dir).resolve() != expected_page_dir.resolve()
        or not expected_page_dir.is_dir()
    ):
        return None

    execution_status, _, _, _ = _page_status(
        source_root, scenario_directory, page_name
    )
    if cached.get("scenario_execution_status") != execution_status:
        return None
    visual_result = cached.get("visual_comparison") or {}
    if visual_result.get("reason") == "visual comparison is in progress":
        return None

    cache_time = cache_path.stat().st_mtime_ns
    input_directories = [
        expected_page_dir,
        source_root / "planning" / safe_path_part(page_name),
    ]
    for input_directory in input_directories:
        if not input_directory.exists():
            continue
        if input_directory.stat().st_mtime_ns > cache_time:
            return None
        if any(
            item.stat().st_mtime_ns > cache_time
            for item in input_directory.rglob("*")
            if item.is_file()
        ):
            return None

    screenshot_paths = [
        Path(item["path"])
        for side in ("dashboard_A_screenshots", "dashboard_B_screenshots")
        for item in evidence.get(side, [])
        if isinstance(item, dict) and item.get("path")
    ]
    if any(not path.is_file() or path.stat().st_mtime_ns > cache_time for path in screenshot_paths):
        return None
    return cached


def _evidence_presence(page: dict[str, Any], side: str) -> str:
    screenshots = (page.get("evidence") or {}).get(side, [])
    if not screenshots:
        return "MISSING"
    paths = [
        Path(item["path"])
        for item in screenshots
        if isinstance(item, dict) and item.get("path")
    ]
    if len(paths) != len(screenshots) or not all(path.is_file() for path in paths):
        return "INCOMPLETE"
    return "AVAILABLE"


def _validate_prepared_evidence(
    scenario_id: str,
    page_name: str,
    source_root: Path,
    page: dict[str, Any],
    prepare_error: Exception | None = None,
) -> dict[str, Any]:
    evidence = page.get("evidence") or {}
    page_key = safe_path_part(page_name)
    required_json = [
        ("execution_status", evidence.get("execution_status_json")),
        ("scenario_execution", evidence.get("scenario_execution_json")),
        (
            "dashboard_A_manifest",
            source_root / "dashboard_A" / "evidence_manifest.json",
        ),
        (
            "dashboard_B_manifest",
            source_root / "dashboard_B" / "evidence_manifest.json",
        ),
        (
            "dashboard_A_scenario_execution",
            dashboard_execution_evidence_path(
                source_root, "dashboard_A", scenario_id, page_name
            ),
        ),
        (
            "dashboard_B_scenario_execution",
            dashboard_execution_evidence_path(
                source_root, "dashboard_B", scenario_id, page_name
            ),
        ),
        (
            "dashboard_A_execution_status",
            dashboard_execution_evidence_path(
                source_root, "dashboard_A", scenario_id, page_name, status=True
            ),
        ),
        (
            "dashboard_B_execution_status",
            dashboard_execution_evidence_path(
                source_root, "dashboard_B", scenario_id, page_name, status=True
            ),
        ),
        (
            "dashboard_A_dom_plan",
            source_root / "planning" / page_key / "dashboard_A_plan.json",
        ),
        (
            "dashboard_B_dom_plan",
            source_root / "planning" / page_key / "dashboard_B_plan.json",
        ),
    ]
    if scenario_id != "01":
        required_json.insert(
            2, ("scenario_comparison", evidence.get("comparison_json"))
        )
    json_files = []
    errors = list(evidence.get("errors") or [])
    for name, value in required_json:
        path = Path(value) if value else None
        exists = bool(path and path.is_file())
        data, read_error = _read_json(path) if path and exists else (None, None)
        valid = exists and read_error is None and data is not None
        json_files.append(
            {
                "name": name,
                "path": str(path.resolve()) if path else None,
                "exists": exists,
                "valid": valid,
            }
        )
        if not exists:
            errors.append(f"required JSON evidence is missing: {name}")
        elif not valid:
            errors.append(
                f"required JSON evidence is invalid: {name}: {read_error}"
            )

    screenshot_status = {
        side: _evidence_presence(page, f"{side}_screenshots")
        for side in ("dashboard_A", "dashboard_B")
    }
    execution_status = page.get(
        "scenario_execution_status", STATUS_UNVERIFIED
    )
    if execution_status != STATUS_SKIPPED or scenario_id == "01":
        for side, status in screenshot_status.items():
            if status != "AVAILABLE":
                errors.append(f"{side} screenshots are {status.lower()}")
    if prepare_error is not None:
        errors.append(
            f"evidence preparation failed: {type(prepare_error).__name__}: "
            f"{prepare_error}"
        )
    errors = list(dict.fromkeys(errors))
    if execution_status == STATUS_SKIPPED and not errors:
        validation_status = STATUS_SKIPPED
    elif execution_status == STATUS_FAIL:
        validation_status = STATUS_FAIL
    elif errors:
        validation_status = STATUS_UNVERIFIED
    else:
        validation_status = STATUS_PASS
    return {
        "scenario_id": scenario_id,
        "page_name": page_name,
        "status": validation_status,
        "scenario_execution_status": execution_status,
        "dashboard_A_screenshot_status": screenshot_status["dashboard_A"],
        "dashboard_B_screenshot_status": screenshot_status["dashboard_B"],
        "required_json_evidence": json_files,
        "errors": errors,
    }


def _page_group_summary(pages: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts = Counter(page.get("comparison_status") for page in pages)
    category_counts = Counter(
        finding.get("category")
        for page in pages
        for finding in page.get("findings", [])
    )
    return {
        "scenario_count": len(pages),
        "status_counts": {
            status: status_counts.get(status, 0)
            for status in (
                STATUS_PASS,
                STATUS_FAIL,
                STATUS_UNVERIFIED,
                STATUS_SKIPPED,
            )
        },
        "category_counts": {
            category: category_counts.get(category, 0)
            for category in sorted(COMPARISON_CATEGORIES)
        },
    }


def _write_page_group_result(
    comparison_root: Path, page_name: str, pages: list[dict[str, Any]]
) -> Path:
    page_path = (
        comparison_root / "pages" / safe_path_part(page_name) / "comparison.json"
    )
    summary = _page_group_summary(pages)
    status_counts = Counter(
        page.get("comparison_status") for page in pages
    )
    category_counts = Counter(
        finding.get("category")
        for page in pages
        for finding in page.get("findings", [])
    )
    if status_counts.get(STATUS_SKIPPED, 0) == len(pages):
        overall_status = STATUS_SKIPPED
    else:
        overall_status = _overall_status(status_counts, category_counts)
    _write_json(
        page_path,
        {
            "page_name": page_name,
            "overall_status": overall_status,
            **summary,
            "scenarios": [
                _strip_internal_fields(page)
                for page in pages
            ],
        },
    )
    return page_path


def _write_comparison_progress(
    progress_path: Path,
    progress: dict[str, Any],
    callback: Callable[[dict[str, Any]], None] | None,
) -> None:
    progress["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(progress_path, progress)
    if callback:
        callback(dict(progress))


def _scenario_visual_batches(
    scenario_id: str,
    pages: list[dict[str, Any]],
    screenshot_data: dict[str, dict[str, list[dict[str, Any]]]],
) -> tuple[list[tuple[str, bytes]], list[str], dict[str, str]]:
    inputs: list[tuple[str, bytes]] = []
    eligible_pages: list[str] = []
    page_states: dict[str, str] = {}
    for page in pages:
        if page.get("scenario_execution_status") == STATUS_SKIPPED:
            continue
        filter_comparison = page.get("filter_comparison") or {}
        if scenario_id in {"01", "02", "03"} and filter_comparison.get(
            "state_equivalent"
        ) is not True:
            continue
        if scenario_id in {"02", "03"} and filter_comparison.get(
            "expected_state_applied"
        ) is not True:
            continue
        page_name = page["page_name"]
        screenshots = _make_vision_inputs(page_name, screenshot_data[page_name])
        if not screenshots:
            continue
        eligible_pages.append(page_name)
        inputs.extend(screenshots)
        page_states[page_name] = _filter_state_description(filter_comparison)
    return inputs, eligible_pages, page_states


async def _compare_scenario(
    scenario_id: str,
    scenario_directory: str,
    scenario_name: str,
    source_root: Path,
    page_names: list[str],
    comparison_root: Path,
    workspace_root: Path,
    job_id: str,
    operation_log_path: Path,
    prepared_pages: dict[
        str, tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]
    ]
    | None = None,
) -> dict[str, Any]:
    pages: list[dict[str, Any]] = []
    screenshot_data: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for page_name in page_names:
        prepared = (prepared_pages or {}).get(page_name)
        if prepared:
            page, screenshots = prepared
        else:
            page, screenshots = _prepare_page(
                scenario_id,
                scenario_directory,
                source_root,
                page_name,
                workspace_root,
            )
        screenshot_data[page_name] = screenshots
        page["findings"].extend(
            _filter_findings(page.get("filter_comparison") or {})
        )
        page["comparison_status"] = _status_from_results(
            scenario_id,
            page["scenario_execution_status"],
            page["filter_comparison"],
            page["visual_comparison"],
            page["evidence"].get("errors"),
        )
        pages.append(page)
        _write_page_result(comparison_root, scenario_id, scenario_name, page)
        _write_scenario_result(
            comparison_root, scenario_id, scenario_name, source_root, pages
        )

    if scenario_id == "01":
        for page in pages:
            source_comparison = page.get("_source_comparison") or {}
            saved_vision = source_comparison.get("llm_vision_comparison")
            if not isinstance(saved_vision, dict):
                page_name = page["page_name"]
                headers_a: list[str] = []
                headers_b: list[str] = []
                for candidate in (
                    source_root / "01_table_column_header_comparison" / safe_path_part(page_name) / "dom_extraction.json",
                    source_root / "01_table_column_header_comparison" / page_name / "dom_extraction.json",
                ):
                    if candidate.is_file():
                        try:
                            dom_json, _ = _read_json(candidate)
                            if dom_json:
                                headers_a = dom_json.get("headers_a") or []
                                headers_b = dom_json.get("headers_b") or []
                                break
                        except Exception:
                            pass

                dom_evidence = {
                    side: _dom_header_evidence(
                        source_root,
                        side,
                        page["page_name"],
                        next(
                            (
                                item["path"]
                                for item in screenshot_data[page["page_name"]][side]
                                if item.get("label") == "initial"
                            ),
                            None,
                        ),
                        workspace_root,
                    )
                    for side in ("dashboard_A", "dashboard_B")
                }
                if not headers_a:
                    headers_a = dom_evidence.get("dashboard_A", {}).get("headers") or []
                if not headers_b:
                    headers_b = dom_evidence.get("dashboard_B", {}).get("headers") or []

                vision_result = compare_dom_headers(headers_a, headers_b)
                vision_result["dom_header_evidence"] = dom_evidence
                page["evidence"]["dom_header_evidence"] = dom_evidence
                page["visual_comparison"], page["findings"] = _scenario01_result(
                    {"llm_vision_comparison": vision_result}
                )
            page["comparison_status"] = _status_from_results(
                scenario_id,
                page["scenario_execution_status"],
                page["filter_comparison"],
                page["visual_comparison"],
                page["evidence"].get("errors"),
            )
            _write_page_result(comparison_root, scenario_id, scenario_name, page)
        _write_scenario_result(
            comparison_root, scenario_id, scenario_name, source_root, pages
        )
        return {
            "scenario_id": scenario_id,
            "scenario_name": scenario_name,
            "source_directory": str(source_root),
            "pages": pages,
            "summary": _scenario_summary(pages),
        }

    screenshots, eligible_pages, page_states = _scenario_visual_batches(
        scenario_id, pages, screenshot_data
    )
    if eligible_pages:
        for page in pages:
            if page["page_name"] in eligible_pages:
                page["visual_comparison"] = {
                    "status": STATUS_UNVERIFIED,
                    "reason": "visual comparison is in progress",
                }
        _write_scenario_result(
            comparison_root, scenario_id, scenario_name, source_root, pages
        )
        try:
            vision_result = await compare_scenario_screenshots(
                screenshots,
                eligible_pages,
                scenario_id,
                scenario_name,
                page_states=page_states,
                job_id=job_id,
                operation_log_path=operation_log_path,
            )
            vision_by_page = {
                item["page_name"]: item
                for item in vision_result.get("page_results", [])
                if isinstance(item, dict) and item.get("page_name")
            }
            for page in pages:
                if page["page_name"] not in eligible_pages:
                    continue
                result = vision_by_page.get(page["page_name"])
                if result is None:
                    page["visual_comparison"] = {
                        "status": STATUS_UNVERIFIED,
                        "reason": "vision response omitted this page",
                    }
                else:
                    page["visual_comparison"] = result
                    page["findings"].extend(_visual_result_findings(result))
                page["comparison_status"] = _status_from_results(
                    scenario_id,
                    page["scenario_execution_status"],
                    page["filter_comparison"],
                    page["visual_comparison"],
                    page["evidence"].get("errors"),
                )
                _write_page_result(
                    comparison_root, scenario_id, scenario_name, page
                )
                _write_scenario_result(
                    comparison_root, scenario_id, scenario_name, source_root, pages
                )
        except Exception as exc:
            logger.exception(
                "Scenario %s vision comparison failed for pages %s",
                scenario_id,
                eligible_pages,
            )
            for page in pages:
                if page["page_name"] not in eligible_pages:
                    continue
                page["visual_comparison"] = {
                    "status": STATUS_UNVERIFIED,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
                page["comparison_status"] = _status_from_results(
                    scenario_id,
                    page["scenario_execution_status"],
                    page["filter_comparison"],
                    page["visual_comparison"],
                    page["evidence"].get("errors"),
                )
                page["evidence"]["errors"].append(
                    f"vision comparison failed: {type(exc).__name__}: {exc}"
                )
                _write_page_result(
                    comparison_root, scenario_id, scenario_name, page
                )
                _write_scenario_result(
                    comparison_root, scenario_id, scenario_name, source_root, pages
                )

    for page in pages:
        if page["page_name"] not in eligible_pages:
            if page["scenario_execution_status"] != STATUS_SKIPPED:
                page["visual_comparison"] = {
                    "status": STATUS_UNVERIFIED,
                    "reason": (
                        "no equivalent, verified screenshot pair is available"
                    ),
                }
                page["comparison_status"] = _status_from_results(
                    scenario_id,
                    page["scenario_execution_status"],
                    page["filter_comparison"],
                    page["visual_comparison"],
                    page["evidence"].get("errors"),
                )
            _write_page_result(comparison_root, scenario_id, scenario_name, page)
    _write_scenario_result(
        comparison_root, scenario_id, scenario_name, source_root, pages
    )
    return {
        "scenario_id": scenario_id,
        "scenario_name": scenario_name,
        "source_directory": str(source_root),
        "pages": pages,
        "summary": _scenario_summary(pages),
    }


def _reference_metadata(
    workbook_path: Path | None, docx_path: Path | None
) -> dict[str, Any]:
    info: dict[str, Any] = {}
    if workbook_path and workbook_path.is_file():
        workbook = load_workbook(workbook_path, read_only=True, data_only=True)
        info["workbook"] = {
            "path": str(workbook_path.resolve()),
            "sheets": workbook.sheetnames,
        }
        workbook.close()
    if docx_path and docx_path.is_file():
        document = Document(str(docx_path))
        paragraphs = [p.text.strip() for p in document.paragraphs if p.text.strip()]
        info["docx"] = {
            "path": str(docx_path.resolve()),
            "title": paragraphs[0] if paragraphs else "",
            "sections": [
                p.text.strip()
                for p in document.paragraphs
                if p.style.name.startswith("Heading") and p.text.strip()
            ],
        }
    return info


def _count_findings(
    scenarios: list[dict[str, Any]],
) -> Counter[str]:
    return Counter(
        finding.get("category", "UNVERIFIED")
        for scenario in scenarios
        for page in scenario["pages"]
        for finding in page.get("findings", [])
    )


def _overall_status(
    status_counts: Counter[str], category_counts: Counter[str]
) -> str:
    if status_counts.get(STATUS_FAIL, 0) or any(
        category_counts.get(category, 0)
        for category in CONFIRMED_DIFFERENCES
    ):
        return STATUS_FAIL
    if status_counts.get(STATUS_UNVERIFIED, 0):
        return STATUS_UNVERIFIED
    return STATUS_PASS


def _final_summary(
    scenarios: list[dict[str, Any]], references: dict[str, Any]
) -> dict[str, Any]:
    pages = [page for scenario in scenarios for page in scenario["pages"]]
    status_counts = Counter(page.get("comparison_status") for page in pages)
    category_counts = _count_findings(scenarios)
    compared_count = sum(
        1
        for page in pages
        if page.get("comparison_status") != STATUS_SKIPPED
        and (
            page.get("visual_comparison", {}).get("status")
            in {STATUS_PASS, STATUS_FAIL}
            or page.get("filter_comparison", {}).get("status")
            in {STATUS_PASS, STATUS_FAIL}
        )
    )
    scope_note = (
        "Only table and matrix column headers were evaluated; KPI/chart values, "
        "filters, table cell data, and all other dashboard content were not compared."
        if len(scenarios) == 1 and scenarios[0].get("scenario_id") == "01"
        else (
            "Numeric chart/KPI values and table cell records were not validated; "
            "Scenario 02 and Scenario 03 cover visual rendering and filter "
            "validation only. Table headers and hierarchy are evaluated only "
            "by their dedicated scenarios."
        )
    )
    return {
        "total_scenario_types": len(scenarios),
        "total_page_scenarios_processed": len(pages),
        "total_scenarios_executed": sum(
            1
            for page in pages
            if page.get("scenario_execution_status") != STATUS_SKIPPED
        ),
        "total_scenarios_compared": compared_count,
        "passed_scenarios": status_counts.get(STATUS_PASS, 0),
        "failed_scenarios": status_counts.get(STATUS_FAIL, 0),
        "unverified_scenarios": status_counts.get(STATUS_UNVERIFIED, 0),
        "skipped_scenarios": status_counts.get(STATUS_SKIPPED, 0),
        "total_matching_elements": category_counts.get("MATCH", 0),
        "total_missing_elements": category_counts.get("MISSING", 0),
        "total_additional_elements": category_counts.get("ADDITIONAL", 0),
        "total_renamed_elements": category_counts.get("RENAMED", 0),
        "total_value_differences": category_counts.get("DIFFERENT_VALUE", 0),
        "total_order_differences": category_counts.get("DIFFERENT_ORDER", 0),
        "total_visual_differences": category_counts.get("VISUAL_DIFFERENCE", 0),
        "total_behavior_differences": category_counts.get("BEHAVIOR_DIFFERENCE", 0),
        "total_unverified_findings": category_counts.get("UNVERIFIED", 0),
        "scenario_summaries": [
            {
                "scenario_id": scenario["scenario_id"],
                "scenario_name": scenario["scenario_name"],
                **scenario["summary"],
            }
            for scenario in scenarios
        ],
        "overall_dashboard_result": _overall_status(
            status_counts, category_counts
        ),
        "visual_validation_scope_note": scope_note,
        "reference_documents": references,
    }


def _set_cell_shading(cell: Any, color: str) -> None:
    properties = cell._tc.get_or_add_tcPr()
    shading = OxmlElement("w:shd")
    shading.set(qn("w:fill"), color)
    properties.append(shading)


def _add_table(
    document: Any,
    headers: list[str],
    rows: list[list[str]],
    widths: list[float] | None = None,
    font_size: float = 8.0,
) -> Any:
    table = document.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    table.autofit = False
    for row_index, row_values in enumerate([headers] + rows):
        row = table.rows[0] if row_index == 0 else table.add_row()
        for col_index, value in enumerate(row_values):
            cell = row.cells[col_index]
            if widths and col_index < len(widths):
                cell.width = Inches(widths[col_index])
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.space_after = Pt(0)
            run = paragraph.add_run(str(value))
            run.font.size = Pt(font_size)
            if row_index == 0:
                run.bold = True
                run.font.color.rgb = RGBColor(255, 255, 255)
                _set_cell_shading(cell, DOCX_NAVY)
        if row_index == 0:
            row_properties = row._tr.get_or_add_trPr()
            row_properties.append(OxmlElement("w:tblHeader"))
    document.add_paragraph()
    return table


def _summary_findings(page: dict[str, Any], limit: int = 4) -> str:
    findings = page.get("findings", [])
    differences = [
        item
        for item in findings
        if item.get("category") not in {"MATCH"}
    ]
    if not differences:
        visual = page.get("visual_comparison") or {}
        notes = visual.get("notes") or []
        if notes:
            return "; ".join(str(note) for note in notes[:limit])
        return "No confirmed differences in available evidence."
    return "; ".join(
        f"{item.get('category')}: {item.get('observation')}"
        for item in differences[:limit]
    )


def _filter_summary(page: dict[str, Any]) -> str:
    comparison = page.get("filter_comparison") or {}
    if comparison.get("status") == "NOT_APPLICABLE":
        return "Not applicable"
    items = comparison.get("items") or []
    if not items:
        return str(comparison.get("reason") or "Not available")
    return f"{comparison.get('status')}: {len(items)} recorded item(s)"


def _add_image_pair(
    document: Any,
    page_name: str,
    label: str,
    path_a: str | None,
    path_b: str | None,
) -> None:
    document.add_heading(f"{page_name} — {label}", level=3)
    table = document.add_table(rows=1, cols=2)
    table.style = "Table Grid"
    for cell, side, path in zip(
        table.rows[0].cells,
        ("Dashboard A / Snowflake", "Dashboard B / GCP"),
        (path_a, path_b),
    ):
        paragraph = cell.paragraphs[0]
        paragraph.add_run(side).bold = True
        image_path = Path(path) if path else None
        if image_path and image_path.is_file():
            cell.add_paragraph().add_run().add_picture(
                str(image_path), width=Inches(4.5)
            )
            caption = cell.add_paragraph(str(image_path))
            for run in caption.runs:
                run.font.size = Pt(6)
        else:
            cell.add_paragraph(f"Screenshot unavailable: {path or 'not captured'}")
    document.add_paragraph()


def _write_docx(
    path: Path,
    summary: dict[str, Any],
    scenarios: list[dict[str, Any]],
    reference_metadata: dict[str, Any],
) -> None:
    document = Document()
    section = document.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = section.page_height, section.page_width
    section.left_margin = Inches(0.45)
    section.right_margin = Inches(0.45)
    section.top_margin = Inches(0.5)
    section.bottom_margin = Inches(0.5)
    normal = document.styles["Normal"]
    normal.font.name = "Arial"
    normal.font.size = Pt(9)
    document.core_properties.title = "Inventory Insights Scenario Comparison"
    document.core_properties.subject = "Snowflake vs. GCP Power BI visual validation"

    title = document.add_paragraph()
    title.alignment = 1
    run = title.add_run("Inventory Insights")
    run.bold = True
    run.font.size = Pt(20)
    run.font.color.rgb = RGBColor.from_string(DOCX_NAVY)
    subtitle = document.add_paragraph()
    subtitle.alignment = 1
    subtitle.add_run("Snowflake vs. GCP Scenario-by-Scenario Comparison").bold = True
    if [scenario.get("scenario_id") for scenario in scenarios] == ["01"]:
        document.add_paragraph(
            "Scenario 01 table-header comparison using saved screenshot evidence; "
            "no other validation scenarios were executed."
        )
    else:
        document.add_paragraph(
            "Evidence-only visual validation; functional scenarios were not rerun."
        )

    document.add_heading("1. Comparison Summary", level=1)
    summary_rows = [
        ["Scenario types", str(summary["total_scenario_types"])],
        ["Page scenarios processed", str(summary["total_page_scenarios_processed"])],
        ["Scenarios executed (excluding skips)", str(summary["total_scenarios_executed"])],
        ["Scenarios compared", str(summary["total_scenarios_compared"])],
        ["Pass", str(summary["passed_scenarios"])],
        ["Fail", str(summary["failed_scenarios"])],
        ["Unverified", str(summary["unverified_scenarios"])],
        ["Skipped", str(summary["skipped_scenarios"])],
        ["Overall Dashboard A vs. B result", summary["overall_dashboard_result"]],
    ]
    _add_table(document, ["Measure", "Result"], summary_rows, [4.5, 5.5])
    document.add_paragraph(summary["visual_validation_scope_note"])

    document.add_heading("2. Scenario Summary", level=1)
    scenario_rows = []
    for item in summary["scenario_summaries"]:
        counts = item["status_counts"]
        category = item["category_counts"]
        scenario_rows.append(
            [
                item["scenario_id"],
                item["scenario_name"],
                str(item["page_count"]),
                str(counts[STATUS_PASS]),
                str(counts[STATUS_FAIL]),
                str(counts[STATUS_UNVERIFIED]),
                str(counts[STATUS_SKIPPED]),
                str(category.get("MISSING", 0) + category.get("ADDITIONAL", 0)),
                str(category.get("VISUAL_DIFFERENCE", 0)),
            ]
        )
    _add_table(
        document,
        [
            "ID",
            "Scenario",
            "Pages",
            "Pass",
            "Fail",
            "Unverified",
            "Skipped",
            "Missing/Additional",
            "Visual differences",
        ],
        scenario_rows,
        [0.55, 2.2, 0.55, 0.5, 0.5, 0.8, 0.65, 1.15, 1.0],
        font_size=7.5,
    )

    document.add_heading("3. Scenario-by-Scenario Findings", level=1)
    for scenario in scenarios:
        document.add_heading(
            f"Scenario {scenario['scenario_id']} — {scenario['scenario_name']}",
            level=2,
        )
        document.add_paragraph(
            f"Evidence source: {scenario['source_directory']}"
        )
        page_rows = []
        for page in scenario["pages"]:
            page_rows.append(
                [
                    page["page_name"],
                    page["scenario_execution_status"],
                    page["comparison_status"],
                    _filter_summary(page),
                    _summary_findings(page),
                ]
            )
        _add_table(
            document,
            [
                "Page",
                "Execution",
                "Comparison",
                "Filters / slicers",
                "Finding summary",
            ],
            page_rows,
            [1.45, 1.0, 1.0, 1.8, 5.2],
            font_size=7.5,
        )
        for page in scenario["pages"]:
            evidence = page.get("evidence") or {}
            validation = evidence.get("validation") or {}
            document.add_paragraph(
                f"Evidence validation: {validation.get('status', STATUS_UNVERIFIED)}"
            )
            json_references = []
            for item in validation.get("required_json_evidence", []):
                evidence_status = (
                    "available"
                    if item.get("valid")
                    else "MISSING"
                    if not item.get("exists")
                    else "INVALID"
                )
                json_references.append(
                    f"{item.get('name', 'JSON evidence')}: "
                    f"{item.get('path') or 'missing'} ({evidence_status})"
                )
            if json_references:
                document.add_paragraph("JSON evidence: " + "; ".join(json_references))
            if scenario.get("scenario_id") == "01":
                visual = page.get("visual_comparison") or {}
                document.add_heading(
                    f"{page['page_name']} — Table Header Comparison", level=3
                )
                document.add_paragraph(
                    f"Comparison status: {visual.get('status', STATUS_UNVERIFIED)}"
                )
                for side, label in (
                    ("headers_a", "Dashboard A complete ordered headers"),
                    ("headers_b", "Dashboard B complete ordered headers"),
                ):
                    headers = visual.get(side) or []
                    ordered = ", ".join(
                        f"{index}. {header}"
                        for index, header in enumerate(headers, start=1)
                    )
                    document.add_paragraph(
                        f"{label}: {ordered or 'Not verified'}"
                    )
                for key, label in (
                    ("missing_headers", "Missing headers on Dashboard B"),
                    ("additional_headers", "Additional headers on Dashboard B"),
                ):
                    values = visual.get(key) or []
                    document.add_paragraph(
                        f"{label}: {', '.join(str(value) for value in values) or 'None'}"
                    )
                renamed = visual.get("renamed_headers") or []
                document.add_paragraph(
                    "Header-name differences: "
                    + (
                        "; ".join(
                            f"{item.get('header_a')} -> {item.get('header_b')}"
                            for item in renamed
                        )
                        or "None"
                    )
                )
                order_differences = (
                    visual.get("differently_ordered_headers") or []
                )
                document.add_paragraph(
                    "Header-order differences: "
                    + (
                        "; ".join(
                            f"{item.get('header')} at A:{item.get('position_a')} / "
                            f"B:{item.get('position_b')}"
                            for item in order_differences
                        )
                        or "None"
                    )
                )
                dom_evidence = evidence.get("dom_header_evidence") or {}
                if dom_evidence:
                    document.add_paragraph(
                        "DOM header corroboration: "
                        + "; ".join(
                            f"{side}: {item.get('status')} "
                            f"({item.get('path') or 'no evidence file'})"
                            for side, item in dom_evidence.items()
                        )
                    )
            detail_items = [
                item
                for item in page.get("findings", [])
                if item.get("category") != "MATCH"
            ]
            if detail_items:
                document.add_heading(
                    f"{page['page_name']} — Findings", level=3
                )
                _add_table(
                    document,
                    [
                        "Category",
                        "Element",
                        "Dashboard A",
                        "Dashboard B",
                        "Observation",
                    ],
                    [
                        [
                            item.get("category", ""),
                            item.get("element_type", ""),
                            item.get("element_a") or "",
                            item.get("element_b") or "",
                            item.get("observation", ""),
                        ]
                        for item in detail_items
                    ],
                    [1.15, 1.2, 1.65, 1.65, 4.8],
                    font_size=7,
                )
            if page.get("evidence", {}).get("errors"):
                document.add_paragraph(
                    "Evidence limitations: "
                    + "; ".join(page["evidence"]["errors"])
                )

    document.add_heading("4. Screenshot Evidence", level=1)
    document.add_paragraph(
        "All scenario screenshots used in comparisons are referenced below or "
        "in the corresponding JSON page records. Scenario 01 compares each "
        "dashboard's designated initial screenshot and every captured "
        "horizontal-scroll screenshot; only table and matrix headers are evaluated."
    )
    for scenario in scenarios:
        for page in scenario["pages"]:
            shots_a = page.get("evidence", {}).get("dashboard_A_screenshots", [])
            shots_b = page.get("evidence", {}).get("dashboard_B_screenshots", [])
            if not shots_a and not shots_b:
                continue
            selected_pairs = _select_scenario_screenshot_pairs(shots_a, shots_b, max_pairs=4)
            for item_a, item_b in selected_pairs:
                label = item_a.get("label") or "screenshot"
                _add_image_pair(
                    document,
                    f"Scenario {scenario['scenario_id']} — {page['page_name']}",
                    label,
                    item_a.get("path"),
                    item_b.get("path"),
                )

    document.add_heading("5. Reference Documents and Limitations", level=1)
    workbook = reference_metadata.get("workbook") or {}
    docx_info = reference_metadata.get("docx") or {}
    document.add_paragraph(
        f"Reference workbook: {workbook.get('path', 'Not provided')}; "
        f"worksheets: {', '.join(workbook.get('sheets', [])) or 'Not available'}."
    )
    document.add_paragraph(
        f"Reference DOCX: {docx_info.get('path', 'Not provided')}; "
        f"title: {docx_info.get('title', 'Not available')}."
    )
    document.add_paragraph(
        "Evidence validation JSON: "
        f"{summary.get('evidence_validation_json', 'Not available')}."
    )
    document.add_paragraph(summary["visual_validation_scope_note"])
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(path))


async def run_scenario_comparison(
    scenario_sources: dict[str, Path],
    output_dir: Path,
    *,
    only_scenario_id: str | None = None,
    reference_xlsx: Path | None = None,
    reference_docx: Path | None = None,
    job_id: str = "scenario-comparison",
    comparison_cache_dir: Path | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if only_scenario_id is not None and only_scenario_id not in {
        scenario_id for scenario_id, _, _ in SCENARIOS
    }:
        raise ValueError(f"Unknown scenario ID: {only_scenario_id}")
    selected_scenarios = tuple(
        scenario
        for scenario in SCENARIOS
        if only_scenario_id is None or scenario[0] == only_scenario_id
    )
    selected_ids = {scenario_id for scenario_id, _, _ in selected_scenarios}
    missing = selected_ids - set(scenario_sources)
    if missing:
        raise ValueError(f"Missing evidence sources for scenarios: {sorted(missing)}")
    unexpected = set(scenario_sources) - selected_ids
    if unexpected:
        raise ValueError(
            f"Evidence sources provided for unselected scenarios: {sorted(unexpected)}"
        )
    sources = {
        scenario_id: _resolve_source(path)
        for scenario_id, path in scenario_sources.items()
    }
    output_root = _resolve_source(output_dir)
    if only_scenario_id is None:
        organize_scenario_screenshots(sources, output_root)
    for scenario_id, _, _ in selected_scenarios:
        if not all(
            (sources[scenario_id] / dashboard_id).is_dir()
            for dashboard_id in ("dashboard_A", "dashboard_B")
        ):
            raise FileNotFoundError(
                f"Scenario {scenario_id} dashboard evidence folders not found under "
                f"{sources[scenario_id]}"
            )

    page_names = list(
        dict.fromkeys(
            page_name
            for scenario_id, _, _ in selected_scenarios
            for page_name in _page_names(sources[scenario_id])
        )
    )
    if not page_names:
        raise ValueError("No report pages were found in the scenario evidence")
    comparison_root = output_root / "comparison_results"
    comparison_root.mkdir(parents=True, exist_ok=True)
    operation_log = output_root / "operation_timings.jsonl"
    references = _reference_metadata(reference_xlsx, reference_docx)
    cache_root = (
        _resolve_source(comparison_cache_dir)
        if comparison_cache_dir is not None
        else None
    )
    progress_path = comparison_root / "comparison_progress.json"
    total_comparisons = len(page_names) * len(selected_scenarios)
    progress: dict[str, Any] = {
        "status": "IN_PROGRESS",
        "phase": "EVIDENCE_VALIDATION",
        "current_page": None,
        "current_page_number": None,
        "page_count": len(page_names),
        "current_scenario_id": None,
        "current_scenario": None,
        "dashboard_A_evidence_status": "PENDING",
        "dashboard_B_evidence_status": "PENDING",
        "comparison_status": "PENDING",
        "completed_comparisons": 0,
        "remaining_comparisons": total_comparisons,
        "total_comparisons": total_comparisons,
        "identified_differences": 0,
        "unverified_findings": 0,
        "comparison_cache_directory": str(cache_root) if cache_root else None,
    }
    _write_comparison_progress(progress_path, progress, progress_callback)

    prepared_evidence: dict[
        tuple[str, str],
        tuple[dict[str, Any], dict[str, list[dict[str, Any]]], Exception | None],
    ] = {}
    evidence_validation_units = []
    for page_index, page_name in enumerate(page_names, start=1):
        for scenario_index, (
            scenario_id,
            scenario_directory,
            scenario_name,
        ) in enumerate(selected_scenarios, start=1):
            prepare_error = None
            try:
                prepared_page, prepared_screenshots = _prepare_page(
                    scenario_id,
                    scenario_directory,
                    sources[scenario_id],
                    page_name,
                    Path.cwd().resolve(),
                    compare=False,
                )
            except Exception as exc:
                logger.exception(
                    "Scenario %s evidence preparation failed for page %s",
                    scenario_id,
                    page_name,
                )
                prepare_error = exc
                prepared_page = _unverified_page(
                    scenario_id,
                    scenario_directory,
                    sources[scenario_id],
                    page_name,
                    exc,
                )
                prepared_screenshots = {
                    "dashboard_A": [],
                    "dashboard_B": [],
                }

            validation = _validate_prepared_evidence(
                scenario_id,
                page_name,
                sources[scenario_id],
                prepared_page,
                prepare_error,
            )
            prepared_page["evidence"]["validation"] = validation
            prepared_page["evidence"]["errors"] = list(
                dict.fromkeys(
                    prepared_page["evidence"]["errors"]
                    + validation["errors"]
                )
            )
            prepared_page["evidence"]["errors"] = validation["errors"]
            prepared_evidence[(scenario_id, page_name)] = (
                prepared_page,
                prepared_screenshots,
                prepare_error,
            )
            evidence_validation_units.append(validation)
            progress.update(
                {
                    "phase": "EVIDENCE_VALIDATION",
                    "current_page": page_name,
                    "current_page_number": page_index,
                    "current_scenario_id": scenario_id,
                    "current_scenario": scenario_name,
                    "dashboard_A_evidence_status": _evidence_presence(
                        prepared_page, "dashboard_A_screenshots"
                    ),
                    "dashboard_B_evidence_status": _evidence_presence(
                        prepared_page, "dashboard_B_screenshots"
                    ),
                    "evidence_validation_status": validation["status"],
                    "comparison_status": "PENDING",
                    "scenario_number": scenario_index,
                    "scenario_count": len(selected_scenarios),
                }
            )
            _write_comparison_progress(progress_path, progress, progress_callback)

    evidence_validation_counts = Counter(
        item["status"] for item in evidence_validation_units
    )
    evidence_validation_path = comparison_root / "evidence_validation.json"
    _write_json(
        evidence_validation_path,
        {
            "status": (
                STATUS_FAIL
                if evidence_validation_counts.get(STATUS_FAIL, 0)
                else STATUS_UNVERIFIED
                if evidence_validation_counts.get(STATUS_UNVERIFIED, 0)
                else STATUS_PASS
                if evidence_validation_counts.get(STATUS_PASS, 0)
                else STATUS_SKIPPED
            ),
            "page_count": len(page_names),
            "scenario_count": len(selected_scenarios),
            "unit_count": len(evidence_validation_units),
            "status_counts": {
                status: evidence_validation_counts.get(status, 0)
                for status in (
                    STATUS_PASS,
                    STATUS_FAIL,
                    STATUS_UNVERIFIED,
                    STATUS_SKIPPED,
                )
            },
            "units": evidence_validation_units,
        },
    )

    scenario_pages: dict[str, list[dict[str, Any]]] = {
        scenario_id: [] for scenario_id, _, _ in selected_scenarios
    }
    page_scenarios: dict[str, list[dict[str, Any]]] = {
        page_name: [] for page_name in page_names
    }
    all_completed_pages: list[dict[str, Any]] = []

    for page_index, page_name in enumerate(page_names, start=1):
        for scenario_index, (scenario_id, scenario_directory, scenario_name) in enumerate(
            selected_scenarios, start=1
        ):
            prepared_page, prepared_screenshots, prepare_error = prepared_evidence[
                (scenario_id, page_name)
            ]
            validation = prepared_page["evidence"]["validation"]
            cached_page = (
                _cached_page_result(
                    cache_root,
                    scenario_id,
                    scenario_directory,
                    scenario_name,
                    sources[scenario_id],
                    page_name,
                )
                if cache_root
                else None
            )
            if validation["status"] not in {STATUS_PASS, STATUS_SKIPPED}:
                cached_page = None
            if cached_page is not None:
                cached_evidence = cached_page.setdefault("evidence", {})
                cached_evidence["validation"] = validation
                cached_evidence["errors"] = list(
                    dict.fromkeys(
                        (cached_evidence.get("errors") or [])
                        + validation["errors"]
                    )
                )
                evidence_page = cached_page
                comparison_source = "REUSED_UNCHANGED_SAVED_COMPARISON"
            else:
                if prepare_error is None:
                    try:
                        _complete_page_comparison(
                            scenario_id,
                            page_name,
                            sources[scenario_id],
                            prepared_page,
                        )
                        prepared_page["evidence"]["validation"] = validation
                        prepared_page["evidence"]["errors"] = validation["errors"]
                    except Exception as exc:
                        logger.exception(
                            "Scenario %s comparison preparation failed for page %s",
                            scenario_id,
                            page_name,
                        )
                        prepare_error = exc
                        prepared_page = _unverified_page(
                            scenario_id,
                            scenario_directory,
                            sources[scenario_id],
                            page_name,
                            exc,
                        )
                        prepared_page["evidence"]["validation"] = validation
                evidence_page = prepared_page
                comparison_source = "LIVE_COMPARISON"
                if scenario_id == "01":
                    comparison_source = "LIVE_TABLE_HEADER_COMPARISON"
                if prepare_error is not None:
                    comparison_source = "EVIDENCE_PREPARATION_FAILED"

            progress.update(
                {
                    "phase": "COMPARISON",
                    "status": "IN_PROGRESS",
                    "current_page": page_name,
                    "current_page_number": page_index,
                    "current_scenario_id": scenario_id,
                    "current_scenario": scenario_name,
                    "evidence_validation_status": validation["status"],
                    "dashboard_A_evidence_status": _evidence_presence(
                        evidence_page, "dashboard_A_screenshots"
                    ),
                    "dashboard_B_evidence_status": _evidence_presence(
                        evidence_page, "dashboard_B_screenshots"
                    ),
                    "comparison_status": "IN_PROGRESS",
                    "comparison_source": comparison_source,
                    "scenario_number": scenario_index,
                    "scenario_count": len(selected_scenarios),
                }
            )
            _write_comparison_progress(progress_path, progress, progress_callback)

            if cached_page is not None:
                page = {
                    key: value
                    for key, value in cached_page.items()
                    if key not in {"scenario_id", "scenario_name"}
                }
            elif prepare_error is not None:
                page = prepared_page
            else:
                try:
                    scenario_result = await _compare_scenario(
                        scenario_id,
                        scenario_directory,
                        scenario_name,
                        sources[scenario_id],
                        [page_name],
                        comparison_root,
                        Path.cwd().resolve(),
                        job_id,
                        operation_log,
                        prepared_pages={
                            page_name: (prepared_page, prepared_screenshots)
                        },
                    )
                    page = scenario_result["pages"][0]
                except Exception as exc:
                    logger.exception(
                        "Scenario %s comparison failed for page %s",
                        scenario_id,
                        page_name,
                    )
                    if prepared_page is None:
                        raise
                    page = prepared_page
                    page["findings"].extend(
                        _filter_findings(page.get("filter_comparison") or {})
                    )
                    page["comparison_status"] = STATUS_UNVERIFIED
                    if scenario_id != "01" or not page.get("visual_comparison"):
                        page["visual_comparison"] = {
                            "status": STATUS_UNVERIFIED,
                            "reason": (
                                f"{type(exc).__name__}: {exc}"
                            ),
                        }
                    page["evidence"]["errors"].append(
                        f"scenario comparison failed: {type(exc).__name__}: {exc}"
                    )
                    page["findings"].append(
                        _finding(
                            "UNVERIFIED",
                            "Scenario Comparison",
                            (
                                "The page comparison could not be completed: "
                                f"{type(exc).__name__}: {exc}"
                            ),
                        )
                    )

            page["scenario_id"] = scenario_id
            page["scenario_name"] = scenario_name
            page["comparison_provenance"] = comparison_source
            _write_page_result(comparison_root, scenario_id, scenario_name, page)
            scenario_pages[scenario_id].append(page)
            page_scenarios[page_name].append(page)
            all_completed_pages.append(page)
            _write_scenario_result(
                comparison_root,
                scenario_id,
                scenario_name,
                sources[scenario_id],
                scenario_pages[scenario_id],
            )
            _write_page_group_result(
                comparison_root, page_name, page_scenarios[page_name]
            )

            completed = len(all_completed_pages)
            category_counts = Counter(
                finding.get("category")
                for completed_page in all_completed_pages
                for finding in completed_page.get("findings", [])
            )
            progress.update(
                {
                    "completed_comparisons": completed,
                    "remaining_comparisons": total_comparisons - completed,
                    "comparison_status": page.get(
                        "comparison_status", STATUS_UNVERIFIED
                    ),
                    "identified_differences": sum(
                        category_counts.get(category, 0)
                        for category in CONFIRMED_DIFFERENCES
                    ),
                    "unverified_findings": category_counts.get(
                        "UNVERIFIED", 0
                    ),
                }
            )
            _write_comparison_progress(progress_path, progress, progress_callback)

    scenarios = [
        {
            "scenario_id": scenario_id,
            "scenario_name": scenario_name,
            "source_directory": str(sources[scenario_id]),
            "pages": scenario_pages[scenario_id],
            "summary": _scenario_summary(scenario_pages[scenario_id]),
        }
        for scenario_id, _, scenario_name in selected_scenarios
    ]
    summary = _final_summary(scenarios, references)
    page_summaries = []
    page_wise_results = []
    for page_name in page_names:
        pages = page_scenarios[page_name]
        group_summary = _page_group_summary(pages)
        status_counts = Counter(page.get("comparison_status") for page in pages)
        category_counts = Counter(
            finding.get("category")
            for page in pages
            for finding in page.get("findings", [])
        )
        page_summary = {
            "page_name": page_name,
            "comparison_json": str(
                (
                    comparison_root
                    / "pages"
                    / safe_path_part(page_name)
                    / "comparison.json"
                ).resolve()
            ),
            "overall_status": (
                STATUS_SKIPPED
                if status_counts.get(STATUS_SKIPPED, 0) == len(pages)
                else _overall_status(status_counts, category_counts)
            ),
            **group_summary,
        }
        page_summaries.append(page_summary)
        page_wise_results.append(
            {
                **page_summary,
                "scenarios": [_strip_internal_fields(page) for page in pages],
            }
        )
    scenario_wise_path = comparison_root / "scenario_wise_comparison.json"
    page_wise_path = comparison_root / "page_wise_comparison.json"
    summary.update(
        {
            "comparison_order": "page_then_scenario",
            "page_names": page_names,
            "page_summaries": page_summaries,
            "total_pages_compared": len(page_names),
            "total_comparison_units": total_comparisons,
            "comparison_progress_json": str(progress_path.resolve()),
            "evidence_validation_json": str(evidence_validation_path.resolve()),
            "evidence_validation_status_counts": {
                status: evidence_validation_counts.get(status, 0)
                for status in (
                    STATUS_PASS,
                    STATUS_FAIL,
                    STATUS_UNVERIFIED,
                    STATUS_SKIPPED,
                )
            },
            "scenario_wise_comparison_json": str(scenario_wise_path.resolve()),
            "page_wise_comparison_json": str(page_wise_path.resolve()),
        }
    )
    _write_json(
        scenario_wise_path,
        {
            "comparison_order": "page_then_scenario",
            "scenarios": [
                {
                    **scenario,
                    "pages": [
                        _strip_internal_fields(page) for page in scenario["pages"]
                    ],
                }
                for scenario in scenarios
            ],
        },
    )
    _write_json(
        page_wise_path,
        {
            "comparison_order": "page_then_scenario",
            "pages": page_wise_results,
        },
    )
    final_path = comparison_root / "final_comparison_summary.json"
    _write_json(final_path, summary)
    report_path = output_root / "Inventory_Insights_Scenario_Comparison.docx"
    progress.update(
        {
            "phase": "REPORT_GENERATION",
            "current_page": None,
            "current_page_number": None,
            "current_scenario_id": None,
            "current_scenario": None,
            "comparison_status": "COMPLETED",
        }
    )
    _write_comparison_progress(progress_path, progress, progress_callback)
    _write_docx(report_path, summary, scenarios, references)
    obsolete_scenario_output = comparison_root / "scenario_05"
    if obsolete_scenario_output.is_dir():
        try:
            shutil.rmtree(obsolete_scenario_output)
        except OSError:
            logger.warning(
                "Could not remove obsolete scenario directory %s",
                obsolete_scenario_output,
                exc_info=True,
            )
    write_operation_event(
        operation_log,
        job_id=job_id,
        operation="scenario_comparison_report",
        event="finished",
        outcome="success",
    )
    progress.update(
        {
            "status": "COMPLETED",
            "phase": "COMPLETED",
            "current_page": None,
            "current_page_number": None,
            "current_scenario_id": None,
            "current_scenario": None,
            "comparison_status": "COMPLETED",
            "completed_comparisons": total_comparisons,
            "remaining_comparisons": 0,
            "report_status": "READY",
        }
    )
    _write_comparison_progress(progress_path, progress, progress_callback)
    return {
        "summary": summary,
        "comparison_json": str(final_path),
        "scenario_wise_comparison_json": str(scenario_wise_path),
        "page_wise_comparison_json": str(page_wise_path),
        "docx_report": str(report_path),
        "comparison_progress_json": str(progress_path),
        "page_comparisons": [
            item["comparison_json"] for item in page_summaries
        ],
    }


def _parse_scenario_source(value: str) -> tuple[str, Path]:
    scenario_id, separator, path = value.partition("=")
    if not separator or scenario_id not in {item[0] for item in SCENARIOS} or not path:
        raise argparse.ArgumentTypeError(
            "scenario source must be in the form ID=PATH, where ID is 01 through 04"
        )
    return scenario_id, Path(path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare existing scenario evidence without rerunning dashboards."
    )
    parser.add_argument(
        "--scenario-source",
        type=_parse_scenario_source,
        action="append",
        required=True,
        metavar="ID=PATH",
        help="Scenario evidence root. Repeat once for each scenario to compare.",
    )
    parser.add_argument(
        "--scenario-id",
        choices=[item[0] for item in SCENARIOS],
        help="Compare only this scenario ID; requires only its matching --scenario-source.",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("output_scenario_comparison")
    )
    parser.add_argument(
        "--comparison-cache-dir",
        type=Path,
        help=(
            "Reuse unchanged per-page comparison JSON from a prior "
            "comparison_results directory."
        ),
    )
    parser.add_argument("--reference-xlsx", type=Path)
    parser.add_argument("--reference-docx", type=Path)
    parser.add_argument("--model", default=os.environ.get("GEMINI_MODEL", DEFAULT_MODEL_ID))
    parser.add_argument(
        "--project-id",
        default=os.environ.get("GOOGLE_CLOUD_PROJECT", DEFAULT_PROJECT_ID),
    )
    parser.add_argument(
        "--location",
        default=os.environ.get("GOOGLE_CLOUD_LOCATION", DEFAULT_LOCATION),
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    scenario_sources = dict(args.scenario_source)
    if len(scenario_sources) != len(args.scenario_source):
        raise ValueError("Each scenario ID may be specified only once")
    configure_vertex_ai(args.project_id, args.location, args.model)
    result = asyncio.run(
        run_scenario_comparison(
            scenario_sources,
            args.output_dir,
            only_scenario_id=args.scenario_id,
            reference_xlsx=args.reference_xlsx,
            reference_docx=args.reference_docx,
            comparison_cache_dir=args.comparison_cache_dir,
        )
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
