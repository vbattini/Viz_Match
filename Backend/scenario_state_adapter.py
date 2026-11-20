from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from .agents import extract_screenshot
from .config import positive_int_setting
from .models import DashboardState, FilterCombination, FilterSelection
from .scenario_evidence import (
    dashboard_execution_evidence_path,
    read_evidence_manifest,
)

logger = logging.getLogger(__name__)

_SCENARIO_NAMES = {
    "01": "Table Column Header and Hierarchy Validation",
    "02": "Single Filter Selection",
    "03": "Multiple Filter Selection",
    "04": "Drill Down",
}
_DASHBOARDS = ("dashboard_A", "dashboard_B")
_MAX_EXTRACTION_WORKERS = positive_int_setting(
    "PBI_MAX_EXTRACTION_WORKERS", 4
)


def _dom_column_header_items(headers: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "text": str(header).strip(),
            "category": "column_header",
            "extraction_status": "complete",
            "evidence_sources": ["dom_accessibility"],
            "reading_order": idx + 1,
            "bounding_box": {"x": 10 * idx, "y": 10, "width": 50, "height": 20},
        }
        for idx, header in enumerate(headers)
        if header and str(header).strip()
    ]


def _extracted_column_headers(extracted: dict[str, Any]) -> list[str]:
    return list(
        dict.fromkeys(
            str(item.get("text")).strip()
            for item in extracted.get("title_items") or []
            if isinstance(item, dict)
            and item.get("category") == "column_header"
            and str(item.get("text") or "").strip()
        )
    )


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read scenario execution evidence: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Scenario execution evidence is not an object: {path}")
    return value


def _selection_data(entry: dict[str, Any] | None) -> tuple[Any, list[str], dict[str, list[str]]]:
    selections = (entry or {}).get("filter_selections") or []
    filters: list[FilterSelection] = []
    values_by_name: dict[str, list[str]] = {}
    for selection in selections:
        if not isinstance(selection, dict):
            continue
        name = str(selection.get("filter_name") or "").strip()
        values = selection.get("selected_values") or selection.get("requested_values") or []
        if not name or not isinstance(values, list):
            continue
        selected = [str(value) for value in values]
        if not selected:
            continue
        filters.append(FilterSelection(name, ", ".join(selected)))
        values_by_name[name] = selected
    if len(filters) == 1:
        return filters[0], [filters[0].name], values_by_name
    if filters:
        return FilterCombination(tuple(filters)), [item.name for item in filters], values_by_name
    return None, [], {}


def _snapshot_data(
    entry: dict[str, Any] | None,
) -> tuple[list[str], dict[str, list[str]]]:
    snapshot = (entry or {}).get("filter_state_snapshot") or []
    filter_names: list[str] = []
    filter_values: dict[str, list[str]] = {}
    for item in snapshot:
        if not isinstance(item, dict):
            continue
        name = str(item.get("filter_name") or "").strip()
        if not name or name in filter_names:
            continue
        filter_names.append(name)
        values = item.get("selected_values")
        if isinstance(values, list):
            filter_values[name] = [str(value) for value in values]
    return filter_names, filter_values


def _state_for(
    *,
    dashboard: str,
    page_name: str,
    scenario_id: str,
    pairing_key: str,
    entry: dict[str, Any] | None,
    extracted: dict[str, Any] | None,
    error: str | None,
    evidence_path: Path,
) -> DashboardState:
    selection, filter_names, filter_values = _selection_data(entry)
    snapshot_names, snapshot_values = _snapshot_data(entry)
    if snapshot_names:
        filter_names = snapshot_names
        filter_values = snapshot_values
    if extracted is None:
        extracted = {
            "capture_status": "unverified",
            "capture_error": error or "Scenario screenshot or execution evidence is unavailable.",
        }
    return DashboardState(
        dashboard=dashboard,
        page=page_name,
        filter=selection,
        screenshot=Path(
            str((entry or {}).get("screenshot_full_path") or evidence_path)
        ),
        extracted=extracted,
        scenario_id=scenario_id,
        scenario_name=_SCENARIO_NAMES[scenario_id],
        scenario_step=pairing_key,
        filter_names=filter_names,
        filter_values=filter_values,
        filter_state_snapshot=(entry or {}).get("filter_state_snapshot") or [],
    )


def _baseline_dom_headers(destination: Path, dashboard: str) -> list[str]:
    path = destination / dashboard / "extracted.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    extracted = payload[0].get("extracted") if isinstance(payload, list) and payload else payload
    if not isinstance(extracted, dict):
        return []
    headers = [
        item.get("text")
        for item in extracted.get("title_items") or []
        if isinstance(item, dict)
        and item.get("category") == "column_header"
        and item.get("text")
    ]
    return list(dict.fromkeys(str(header).strip() for header in headers if str(header).strip()))


async def load_scenario_states(
    *,
    destination: Path,
    job_id: str,
    operation_log_path: Path,
    execution: dict[str, Any],
) -> tuple[list[DashboardState], list[DashboardState], dict[str, Any]]:
    manifests = {
        dashboard: read_evidence_manifest(destination, dashboard) for dashboard in _DASHBOARDS
    }
    groups: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = {}
    issues: list[dict[str, str]] = []
    for dashboard, manifest in manifests.items():
        if manifest is None:
            issues.append(
                {
                    "dashboard_id": dashboard,
                    "reason": "Evidence manifest is missing.",
                }
            )
            continue
        for entry in manifest["screenshots"]:
            scenario_id = str(entry.get("scenario_id") or "")
            if scenario_id not in _SCENARIO_NAMES:
                continue
            filename = str(entry.get("screenshot_filename") or "")
            if not filename and entry.get("screenshot_full_path"):
                filename = Path(str(entry.get("screenshot_full_path"))).name
            if "Scroll" in filename or entry.get("capture_role") == "table_headers_scroll":
                continue
            page_name = str(entry.get("page_name") or "").strip()
            pairing_key = str(entry.get("pairing_key") or "").strip()
            if not page_name or not pairing_key:
                issues.append(
                    {
                        "dashboard_id": dashboard,
                        "reason": "Screenshot evidence has no page or pairing key.",
                    }
                )
                continue
            group = groups.setdefault((page_name, scenario_id, pairing_key), {})
            if dashboard in group:
                if scenario_id == "02":
                    issues.append(
                        {
                            "dashboard_id": dashboard,
                            "page_name": page_name,
                            "scenario_id": scenario_id,
                            "reason": (
                                "Duplicate single-filter screenshot evidence; "
                                "using the first capture for this filter state."
                            ),
                        }
                    )
                    continue
                raise ValueError(
                    f"Duplicate scenario evidence pairing for {dashboard}, "
                    f"{page_name}, scenario {scenario_id}, {pairing_key}."
                )
            group[dashboard] = entry

    pages = execution.get("pages") or []
    scenario_ids_by_name = {
        "Table Column Header Comparison": "01",
        "Single Filter Selection": "02",
        "Multiple Filter Selection": "03",
        "Drill Down": "04",
        "01_table_column_header_comparison": "01",
        "02_single_filter_selection": "02",
        "03_multiple_filter_selection": "03",
        "04_drilldown": "04",
    }
    for page in pages:
        if not isinstance(page, dict):
            continue
        page_name = str(page.get("page") or "").strip()
        for scenario_name, status in (page.get("statuses") or {}).items():
            scenario_id = scenario_ids_by_name.get(str(scenario_name))
            if not scenario_id or not page_name:
                continue
            if not any(
                group_page == page_name and group_scenario == scenario_id
                for group_page, group_scenario, _ in groups
            ):
                key = (page_name, scenario_id, "missing_evidence")
                groups.setdefault(key, {})
                issues.append(
                    {
                        "page_name": page_name,
                        "scenario_id": scenario_id,
                        "reason": (
                            f"No screenshot evidence was recorded "
                            f"(execution status: {status})."
                        ),
                    }
                )

    semaphore = asyncio.Semaphore(_MAX_EXTRACTION_WORKERS)

    async def load_side(
        page_name: str,
        scenario_id: str,
        pairing_key: str,
        dashboard: str,
        entry: dict[str, Any] | None,
    ) -> DashboardState:
        side_evidence_path = dashboard_execution_evidence_path(
            destination, dashboard, scenario_id, page_name
        )
        status_path = dashboard_execution_evidence_path(
            destination, dashboard, scenario_id, page_name, status=True
        )
        screenshot_path = Path(
            str((entry or {}).get("screenshot_full_path") or "")
        )
        errors: list[str] = []
        extracted: dict[str, Any] | None = None

        if entry is None:
            errors.append("Matching screenshot is missing from this dashboard.")
        elif (
            str(entry.get("capture_status") or "").upper() != "CAPTURED"
            or not screenshot_path.is_file()
        ):
            errors.append(
                str(entry.get("capture_error") or "Screenshot capture is missing or failed.")
            )

        execution_path = Path(
            str((entry or {}).get("execution_json_evidence_path") or side_evidence_path)
        )
        execution_data: dict[str, Any] = {}
        try:
            execution_data = _read_json_object(execution_path)
            status_data = _read_json_object(status_path)
            execution_status = str(status_data.get("status") or "").upper()
            if execution_status != "PASS":
                errors.append(
                    "Scenario execution status is "
                    f"{execution_status or 'missing'}"
                    + (
                        f": {status_data['reason']}"
                        if status_data.get("reason")
                        else "."
                    )
                )
        except ValueError as exc:
            errors.append(str(exc))

        dom_headers = []
        baseline_headers = _baseline_dom_headers(destination, dashboard)
        if isinstance(execution_data, dict):
            dom_headers = execution_data.get("dom_headers") or []
            if not dom_headers and isinstance(execution_data.get("step"), dict):
                rec = execution_data["step"].get("record") or {}
                dom_headers = rec.get("dom_headers") or []
        if not dom_headers and isinstance(entry, dict):
            dom_headers = entry.get("headers") or []

        screenshot_filename = str((entry or {}).get("screenshot_filename") or screenshot_path.name)
        is_scroll_screenshot = "Scroll" in screenshot_filename

        if not errors and not is_scroll_screenshot:
            try:
                async with semaphore:
                    extracted = await extract_screenshot(
                        screenshot_path.read_bytes(),
                        page_name,
                        job_id=job_id,
                        operation_log_path=operation_log_path,
                        dashboard=dashboard,
                        filter_state=f"scenario_{scenario_id}:{pairing_key}",
                        header_only=scenario_id == "01",
                    )
            except Exception as exc:
                logger.exception(
                    "Scenario screenshot extraction failed for %s / %s / %s",
                    dashboard,
                    page_name,
                    pairing_key,
                )
                errors.append(f"{type(exc).__name__}: {exc}")

        if dom_headers:
            dom_headers = list(dict.fromkeys([*baseline_headers, *dom_headers]))
            if extracted is None:
                extracted = {
                    "title_items": _dom_column_header_items(dom_headers),
                    "elements": [] if is_scroll_screenshot else [
                        {
                            "element_id": "TABLE_001",
                            "type": "table",
                            "title": "Table",
                            "categories": list(dom_headers),
                        }
                    ],
                    "capture_status": "captured",
                }
            else:
                other_items = [
                    item for item in (extracted.get("title_items") or [])
                    if item.get("category") != "column_header"
                ]
                extracted["title_items"] = other_items + _dom_column_header_items(dom_headers)
                if is_scroll_screenshot:
                    extracted["elements"] = []
                else:
                    for el in (extracted.get("elements") or []):
                        if el.get("type") in {"table", "matrix"}:
                            el["categories"] = list(dom_headers)
        elif (
            scenario_id not in {"02", "03"}
            and baseline_headers
            and extracted is not None
            and not is_scroll_screenshot
        ):
            for el in (extracted.get("elements") or []):
                if el.get("type") in {"table", "matrix"}:
                    el["categories"] = list(
                        dict.fromkeys(
                            [
                                *baseline_headers,
                                *(el.get("categories") or []),
                            ]
                        )
                    )
        elif is_scroll_screenshot and extracted is not None:
            extracted["elements"] = []

        if (
            scenario_id == "01"
            and not is_scroll_screenshot
            and extracted is not None
            and not any(
                str(element.get("type") or "").casefold() in {"table", "matrix"}
                for element in extracted.get("elements") or []
                if isinstance(element, dict)
            )
        ):
            header_schema = list(
                dict.fromkeys(
                    [
                        *baseline_headers,
                        *dom_headers,
                        *_extracted_column_headers(extracted),
                    ]
                )
            )
            if header_schema:
                extracted["elements"] = [
                    {
                        "element_id": "TABLE_001",
                        "type": "table",
                        "title": "Table",
                        "categories": header_schema,
                    }
                ]

        if errors:
            issues.append(
                {
                    "dashboard_id": dashboard,
                    "page_name": page_name,
                    "scenario_id": scenario_id,
                    "pairing_key": pairing_key,
                    "reason": "; ".join(errors),
                }
            )
        return _state_for(
            dashboard=dashboard,
            page_name=page_name,
            scenario_id=scenario_id,
            pairing_key=pairing_key,
            entry=entry,
            extracted=extracted,
            error="; ".join(errors) if errors else None,
            evidence_path=side_evidence_path,
        )

    ordered_groups = sorted(groups.items(), key=lambda item: item[0])
    loaded = await asyncio.gather(
        *(
            load_side(page_name, scenario_id, pairing_key, dashboard, sides.get(dashboard))
            for (page_name, scenario_id, pairing_key), sides in ordered_groups
            for dashboard in _DASHBOARDS
        )
    )
    states_by_side = {dashboard: [] for dashboard in _DASHBOARDS}
    for state in loaded:
        states_by_side[state.dashboard].append(state)

    context = {
        "execution": execution,
        "evidence_validation": {
            "status": "UNVERIFIED" if issues else "VALIDATED",
            "issue_count": len(issues),
            "issues": issues,
            "manifest_paths": {
                dashboard: str(destination / dashboard / "evidence_manifest.json")
                for dashboard in _DASHBOARDS
            },
        },
    }
    return states_by_side["dashboard_A"], states_by_side["dashboard_B"], context
