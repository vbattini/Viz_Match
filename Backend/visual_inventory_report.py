"""Create a screenshot-only visual and filter inventory DOCX."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from docx import Document

from .agents import extract_compare_visual_inventory
from .config import (
    DEFAULT_LOCATION,
    DEFAULT_MODEL_ID,
    DEFAULT_PROJECT_ID,
    configure_vertex_ai,
)
from .docx_report import (
    COMPARISON_TEMPLATE_PATH,
    DEFAULT_TESTER_NAME,
    RESULT_STATUS_DROPDOWN_LABEL,
    _replace_table_data,
    _replace_template_screenshots,
    _set_cell_text,
    _set_dropdown_control,
    _style_status_cell,
)
from .json_evidence import (
    read_json_object as _read_json,
    write_json_atomic as _write_json,
)
from .scenario_evidence import read_evidence_manifest
from .timing import write_operation_event
from .validation_scenarios import safe_path_part

logger = logging.getLogger(__name__)

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_UNVERIFIED = "UNVERIFIED"
REPORT_STATUS = {
    STATUS_PASS: "Pass",
    STATUS_FAIL: "Fail",
    STATUS_UNVERIFIED: "Unable to Verify",
}

HEADER_SCENARIO_ID = "01"


def _position_order(position: str) -> tuple[int, int]:
    if position == "initial":
        return (0, 0)
    if position.startswith("horizontal_scroll_"):
        suffix = position.removeprefix("horizontal_scroll_")
        if suffix.isdigit():
            return (1, int(suffix))
    return (2, 0)


def _screenshots_for_page(
    page_name: str,
    source_root: Path,
) -> tuple[list[tuple[str, bytes]], dict[str, dict[str, Any]], list[str]]:
    errors = []
    side_paths: dict[str, dict[str, Path]] = {
        "dashboard_A": {},
        "dashboard_B": {},
    }
    for side in side_paths:
        manifest = read_evidence_manifest(source_root, side)
        if manifest is None:
            errors.append(f"{side} screenshot evidence manifest is missing.")
            continue
        entries = [
            entry
            for entry in manifest["screenshots"]
            if entry.get("scenario_id") == HEADER_SCENARIO_ID
            and entry.get("page_name") == page_name
            and entry.get("capture_status") == "CAPTURED"
        ]
        for entry in entries:
            position = str(entry.get("scroll_position_id") or "initial")
            value = entry.get("screenshot_full_path")
            path = Path(value) if value else None
            if path and not path.is_absolute():
                path = source_root / path
            if path and path.is_file():
                side_paths[side][position] = path.resolve()
            else:
                errors.append(f"{side} {position} screenshot is missing: {value}")
        if not entries:
            errors.append(
                f"{side} has no Scenario 01 screenshots for page {page_name}."
            )

    image_inputs: list[tuple[str, bytes]] = []
    evidence: dict[str, dict[str, Any]] = {
        "dashboard_A": {},
        "dashboard_B": {},
    }
    for side, label in (
        ("dashboard_A", "Dashboard A"),
        ("dashboard_B", "Dashboard B"),
    ):
        for position, path in sorted(
            side_paths[side].items(), key=lambda item: _position_order(item[0])
        ):
            image_inputs.append(
                (f"Page: {page_name} | {label} | {position}", path.read_bytes())
            )
            evidence[side][position] = {"path": str(path.resolve())}

    return image_inputs, evidence, errors


def _finding_status(findings: list[dict[str, Any]]) -> str:
    statuses = {str(item.get("status", "")).upper() for item in findings}
    if STATUS_FAIL in statuses:
        return STATUS_FAIL
    if not findings or STATUS_UNVERIFIED in statuses:
        return STATUS_UNVERIFIED
    if statuses == {STATUS_PASS}:
        return STATUS_PASS
    return STATUS_UNVERIFIED


def _normalize_inventory_result(page: dict[str, Any]) -> dict[str, Any]:
    notes = page.setdefault("notes", [])
    for side in ("dashboard_A", "dashboard_B"):
        inventory = page.get(side) or {}
        for visual in inventory.get("visuals", []):
            title = visual.get("title")
            if isinstance(title, str) and not title.strip():
                visual["title"] = None
                visual_type = str(visual.get("visual_type") or "visual")
                note = f"A {visual_type} visual has no visible title."
                if note not in notes:
                    notes.append(note)

    unavailable_table = any(
        str(visual.get("visual_type") or "").casefold()
        in {"table", "matrix"}
        and str(visual.get("rendering_status") or "").casefold()
        in {"error", "loading", "unable to verify"}
        for side in ("dashboard_A", "dashboard_B")
        for visual in (page.get(side) or {}).get("visuals", [])
    )
    if unavailable_table:
        for finding in page.get("findings", []):
            if finding.get("element_type") == "Table Header":
                finding["status"] = STATUS_UNVERIFIED
                finding["observation"] = (
                    "Table headers could not be fully compared because one "
                    "dashboard's table or matrix is loading, in error, or "
                    "otherwise unverified."
                )
    page["status"] = _finding_status(page.get("findings", []))
    return page


async def _extract_page(
    page_name: str,
    source_root: Path,
    output_root: Path,
    job_id: str,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    screenshots, evidence, errors = _screenshots_for_page(page_name, source_root)
    page_result: dict[str, Any]
    async with semaphore:
        try:
            page_result = await extract_compare_visual_inventory(
                screenshots,
                page_name,
                job_id=job_id,
                operation_log_path=output_root / "operation_timings.jsonl",
            )
            if page_result["page_name"] != page_name:
                raise ValueError(
                    f"Vision returned page {page_result['page_name']!r}, "
                    f"expected {page_name!r}"
                )
            page_result = _normalize_inventory_result(page_result)
        except Exception as exc:
            logger.exception("Visual inventory comparison failed for %s", page_name)
            page_result = {
                "page_name": page_name,
                "status": STATUS_UNVERIFIED,
                "dashboard_A": {
                    "report_title": None,
                    "dashboard_title": None,
                    "filters": [],
                    "titles": [],
                    "visuals": [],
                },
                "dashboard_B": {
                    "report_title": None,
                    "dashboard_title": None,
                    "filters": [],
                    "titles": [],
                    "visuals": [],
                },
                "findings": [],
                "notes": [
                    f"Vision comparison failed: {type(exc).__name__}: {exc}"
                ],
            }
    if not screenshots:
        errors.append("No corresponding initial screenshots were available.")
        page_result["status"] = STATUS_UNVERIFIED

    result = {
        **page_result,
        "evidence": {
            "dashboard_A_screenshots": evidence["dashboard_A"],
            "dashboard_B_screenshots": evidence["dashboard_B"],
            "errors": errors,
        },
    }
    _write_json(
        output_root / "page_inventories" / f"{safe_path_part(page_name)}.json",
        result,
    )
    return result


def _visible_filter_summary(inventory: dict[str, Any]) -> str:
    filters = inventory.get("filters") or []
    names = []
    for item in filters:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        control_type = item.get("control_type")
        names.append(
            f"{item['name']} ({control_type})" if control_type else str(item["name"])
        )
    if not names:
        return "Visible filter names not verified; selections omitted."
    return "Visible filter names / types (values omitted): " + "; ".join(names)


def _filter_findings(page: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in page.get("findings", [])
        if item.get("element_type") == "Filter"
    ]


def _visual_findings(page: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in page.get("findings", [])
        if item.get("element_type") != "Filter"
    ]


def _word_status(status: Any) -> str:
    return REPORT_STATUS.get(str(status).upper(), "Unable to Verify")


def _visual_for_finding(
    inventory: dict[str, Any], finding_name: Any
) -> dict[str, Any] | None:
    visuals = [
        item
        for item in inventory.get("visuals", [])
        if isinstance(item, dict)
    ]
    text = str(finding_name or "").casefold()
    for visual in visuals:
        title = str(visual.get("title") or "").strip()
        if title and title.casefold() in text:
            return visual
    type_matches = [
        visual
        for visual in visuals
        if (visual_type := str(visual.get("visual_type") or "").strip())
        and visual_type.casefold() in text
    ]
    if len(type_matches) == 1:
        return type_matches[0]
    if len(visuals) == 1:
        return visuals[0]
    return None


def _visual_check_label(page: dict[str, Any], finding: dict[str, Any]) -> str:
    visual_a = _visual_for_finding(
        page.get("dashboard_A") or {}, finding.get("element_a")
    )
    visual_b = _visual_for_finding(
        page.get("dashboard_B") or {}, finding.get("element_b")
    )
    title_a = str((visual_a or {}).get("title") or "").strip()
    title_b = str((visual_b or {}).get("title") or "").strip()
    name_a = finding.get("element_a")
    name_b = finding.get("element_b")
    if title_a and title_b and title_a != title_b:
        title = f"Dashboard A: {title_a}; Dashboard B: {title_b}"
    else:
        title = title_a or title_b
    if not title:
        fallback_types = [
            str((visual or {}).get("visual_type") or "").strip()
            for visual in (visual_a, visual_b)
        ]
        fallback_types = [value for value in fallback_types if value]
        if (
            len(fallback_types) == 2
            and fallback_types[0].casefold() == fallback_types[1].casefold()
        ):
            title = fallback_types[0]
        else:
            title = " / ".join(fallback_types)
    if not title:
        title = str(name_a or name_b or "Unidentified visual")

    type_a = str((visual_a or {}).get("visual_type") or "").strip()
    type_b = str((visual_b or {}).get("visual_type") or "").strip()
    visual_type = (
        type_a
        if type_a and type_a == type_b
        else " / ".join(value for value in (type_a, type_b) if value)
    )
    if visual_type and visual_type.casefold() not in title.casefold():
        title = f"{title} ({visual_type})"

    render_a = str((visual_a or {}).get("rendering_status") or "").strip()
    render_b = str((visual_b or {}).get("rendering_status") or "").strip()
    if render_a and render_b and render_a != render_b:
        title += f" - Dashboard A: {render_a}; Dashboard B: {render_b}"
    return f"Visual: {title}"


def _write_report(
    output_path: Path,
    page_results: list[dict[str, Any]],
    report_title: str,
    tester_name: str = DEFAULT_TESTER_NAME,
) -> None:
    document = Document(str(COMPARISON_TEMPLATE_PATH))
    tables = document.tables
    if len(tables) < 4:
        raise ValueError("The report validation template is missing required tables")

    titles_a = [
        page["dashboard_A"].get("report_title")
        for page in page_results
        if page.get("dashboard_A", {}).get("report_title")
    ]
    titles_b = [
        page["dashboard_B"].get("report_title")
        for page in page_results
        if page.get("dashboard_B", {}).get("report_title")
    ]
    report_name = report_title
    if titles_a and titles_b and titles_a[0] != titles_b[0]:
        report_name = f"Dashboard A: {titles_a[0]} | Dashboard B: {titles_b[0]}"
    _set_cell_text(tables[0].cell(0, 1), report_name)
    _set_cell_text(tables[0].cell(1, 1), "")
    _set_cell_text(tables[0].cell(2, 1), "")
    _set_cell_text(tables[0].cell(3, 1), "")
    _set_dropdown_control(
        tables[0].cell(4, 1),
        tester_name,
        title="Tester Name",
        options=(tester_name, "Battini Vijay"),
    )
    _set_cell_text(tables[0].cell(5, 1), date.today().strftime("%m/%d/%Y"))

    filter_rows: list[list[str]] = []
    visual_rows: list[list[str]] = []
    page_evidence_a: dict[str, dict[str, Any]] = {}
    page_evidence_b: dict[str, dict[str, Any]] = {}
    states: list[str] = []
    all_statuses: list[str] = []
    for page_result in page_results:
        page_name = page_result["page_name"]
        filters = _filter_findings(page_result)
        filter_status = _finding_status(filters)
        all_statuses.append(filter_status)
        filter_rows.append(
            [
                f"{len(filter_rows) + 1:02d}",
                page_name,
                _visible_filter_summary(page_result.get("dashboard_A") or {}),
                "Visible filter names and control types are consistent; selected values were not evaluated.",
                _word_status(filter_status),
            ]
        )

        page_filter_text = _visible_filter_summary(
            page_result.get("dashboard_A") or {}
        )
        findings = _visual_findings(page_result)
        if not findings:
            findings = [
                {
                    "element_type": "Visual Inventory",
                    "element_a": None,
                    "element_b": None,
                    "status": STATUS_UNVERIFIED,
                    "observation": "No verified visual/title/header comparison was returned.",
                }
            ]
        for finding in findings:
            status = _word_status(finding.get("status"))
            all_statuses.append(status)
            name_a = finding.get("element_a") or "Not visible"
            name_b = finding.get("element_b") or "Not visible"
            visual_check = str(finding.get("element_type") or "Visual")
            if name_a == name_b:
                visual_check += f": {name_a}"
            else:
                visual_check += f": A={name_a}; B={name_b}"
            if finding.get("element_type") == "Visual":
                visual_check = _visual_check_label(page_result, finding)
            visual_rows.append(
                [
                    f"{len(visual_rows) + 1:02d}",
                    page_name,
                    visual_check,
                    page_filter_text,
                    (
                        str(
                            finding.get("observation")
                            or "No observation was provided."
                        )
                        + " Data values were not assessed."
                    ),
                    status,
                ]
            )

        state = f"{page_name}|Default state"
        states.append(state)
        for side, target in (
            ("dashboard_A", page_evidence_a),
            ("dashboard_B", page_evidence_b),
        ):
            initial = (
                page_result.get("evidence", {})
                .get(f"{side}_screenshots", {})
                .get("initial")
            )
            if initial:
                target[state] = {"path": initial["path"]}

    _set_cell_text(
        tables[1].cell(0, 4), "Result (Pass/Fail/Unable to Verify)"
    )
    _replace_table_data(tables[1], filter_rows)
    for index, row in enumerate(filter_rows, start=1):
        cell = tables[1].cell(index, 4)
        _set_dropdown_control(
            cell,
            row[4],
            title="Pass / Fail / Unable to Verify",
            options=("Pass", "Fail", "Unable to Verify"),
        )
        _style_status_cell(cell, row[4])

    _set_cell_text(tables[2].cell(0, 2), "Visual / Check")
    _replace_table_data(tables[2], visual_rows)
    for index, row in enumerate(visual_rows, start=1):
        cell = tables[2].cell(index, 5)
        _set_dropdown_control(
            cell,
            row[5],
            title="Pass / Fail / Unable to Verify",
            options=("Pass", "Fail", "Unable to Verify"),
        )
        _style_status_cell(cell, row[5])

    overall = (
        "Pass"
        if all_statuses and all(status == "Pass" for status in all_statuses)
        else "Fail"
    )
    overall_cell = tables[0].cell(6, 1)
    _set_dropdown_control(
        overall_cell,
        overall,
        title=RESULT_STATUS_DROPDOWN_LABEL,
        options=("Pass", "Fail"),
    )
    _style_status_cell(overall_cell, overall)
    _set_cell_text(tables[3].cell(0, 0), RESULT_STATUS_DROPDOWN_LABEL)
    _set_dropdown_control(
        tables[3].cell(0, 1),
        overall,
        title=RESULT_STATUS_DROPDOWN_LABEL,
        options=("Pass", "Fail"),
    )
    _style_status_cell(tables[3].cell(0, 1), overall)
    _set_cell_text(tables[3].cell(1, 1), tester_name)
    _set_cell_text(tables[3].cell(2, 1), date.today().strftime("%m/%d/%Y"))

    for paragraph in document.paragraphs:
        if paragraph.text.strip() == "Snowflake vs. BigQuery Power BI Report Validation":
            paragraph.text = "Snowflake vs. GCP Visual Inventory Validation"
        elif paragraph.text.strip().startswith("Comments:"):
            paragraph.text = (
                "Comments: Visual names, visible filter names, titles, and "
                "table/matrix headers were reviewed from screenshots. KPI, "
                "chart, table-cell, and selected-filter values were not extracted."
            )

    _replace_template_screenshots(
        document, page_evidence_a, page_evidence_b, states
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(output_path))


async def run_visual_inventory_report(
    source: Path,
    output_dir: Path,
    *,
    report_title: str = "Inventory Insights",
    tester_name: str = DEFAULT_TESTER_NAME,
    job_id: str = "visual-inventory-report",
) -> dict[str, Any]:
    source_root = source.resolve()
    summary, error = _read_json(source_root / "validation_summary.json")
    if not summary:
        raise FileNotFoundError(error or "scenario run summary is unavailable")
    page_names = [
        str(item["page"])
        for item in summary.get("pages", [])
        if isinstance(item, dict) and item.get("page")
    ]
    if not page_names:
        raise ValueError("No report pages were found in the scenario run")

    output_root = output_dir.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(5)
    page_results = await asyncio.gather(
        *(
            _extract_page(
                page_name,
                source_root,
                output_root,
                job_id,
                semaphore,
            )
            for page_name in page_names
        )
    )

    status_counts = Counter(page.get("status") for page in page_results)
    finding_counts = Counter(
        f"{item.get('element_type')}:{item.get('status')}"
        for page in page_results
        for item in page.get("findings", [])
    )
    result_summary = {
        "report_title": report_title,
        "source_evidence": {
            "scenario_run": str(source_root),
        },
        "page_count": len(page_results),
        "status_counts": {
            STATUS_PASS: status_counts.get(STATUS_PASS, 0),
            STATUS_FAIL: status_counts.get(STATUS_FAIL, 0),
            STATUS_UNVERIFIED: status_counts.get(STATUS_UNVERIFIED, 0),
        },
        "finding_counts": dict(sorted(finding_counts.items())),
        "data_values_extracted": False,
        "pages": [
            {
                "page_name": page["page_name"],
                "status": page["status"],
                "findings": len(page.get("findings", [])),
                "evidence": page.get("evidence", {}),
            }
            for page in page_results
        ],
    }
    _write_json(output_root / "visual_inventory_summary.json", result_summary)
    report_path = output_root / "Inventory_Insights_Visual_Functional_Validation.docx"
    _write_report(report_path, page_results, report_title, tester_name)
    write_operation_event(
        output_root / "operation_timings.jsonl",
        job_id=job_id,
        operation="visual_inventory_report",
        event="finished",
        outcome="success",
    )
    return {
        "summary": result_summary,
        "summary_json": str((output_root / "visual_inventory_summary.json").resolve()),
        "docx_report": str(report_path.resolve()),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract screenshot-only dashboard visuals, filters, titles, and headers from Scenario 01 evidence."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("output_visual_inventory"))
    parser.add_argument("--report-title", default="Inventory Insights")
    parser.add_argument("--tester-name", default=DEFAULT_TESTER_NAME)
    parser.add_argument(
        "--model", default=os.environ.get("GEMINI_MODEL", DEFAULT_MODEL_ID)
    )
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
    configure_vertex_ai(args.project_id, args.location, args.model)
    result = asyncio.run(
        run_visual_inventory_report(
            args.source,
            args.output_dir,
            report_title=args.report_title,
            tester_name=args.tester_name,
        )
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
