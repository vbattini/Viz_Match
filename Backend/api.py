import base64
import binascii
import csv
import io
import asyncio
import json
import logging
import os
import re
import shutil
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from pydantic import BaseModel, field_validator
from playwright.async_api import async_playwright
from starlette.background import BackgroundTask

from .config import (
    DEFAULT_LOCATION,
    DEFAULT_MODEL_ID,
    DEFAULT_PROJECT_ID,
    configure_vertex_ai,
)
from .discovery import discover_dashboard_pair, normalize_name
from .workflow import run_dashboard_comparison

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = Path(__file__).resolve().parents[1] / "Frontend"
OUTPUT_ROOT = PROJECT_ROOT / "output" / "jobs"
MAX_RETAINED_JOBS = 20
MAX_BATCH_JOBS = 10
MAX_IMPORT_BYTES = 5 * 1024 * 1024
JOBS: dict[str, dict] = {}
BATCHES: dict[str, dict] = {}
JOB_TASKS: set[asyncio.Task] = set()
ACTIVE_JOB_ID: str | None = None
ACTIVE_BATCH_ID: str | None = None
JOB_LOCK = asyncio.Lock()
IMPORT_FIELDS = {
    "workspace name": "workspace_name",
    "semantic model": "semantic_model",
    "report name": "report_name",
}

app = FastAPI(title="Power BI Dashboard Comparator")
app.mount("/static", StaticFiles(directory=FRONTEND_ROOT / "static"), name="static")


class CompareRequest(BaseModel):
    workspace_name: str
    semantic_model: str = ""
    report_name: str
    run_validation_scenarios: bool = False

    @field_validator("workspace_name", "report_name")
    @classmethod
    def validate_required_name(cls, value: str) -> str:
        normalized = normalize_name(value)
        if not normalized:
            raise ValueError("This field is required.")
        return normalized


class BatchJobRequest(BaseModel):
    workspace_name: str
    semantic_model: str
    report_name: str

    @field_validator("workspace_name", "semantic_model", "report_name")
    @classmethod
    def validate_required_name(cls, value: str) -> str:
        normalized = normalize_name(value)
        if not normalized:
            raise ValueError("This field is required.")
        return normalized


class BatchRequest(BaseModel):
    jobs: list[BatchJobRequest]
    stop_on_first_failure: bool = False
    run_validation_scenarios: bool = False

    @field_validator("jobs")
    @classmethod
    def validate_job_count(cls, jobs: list[BatchJobRequest]) -> list[BatchJobRequest]:
        if not jobs:
            raise ValueError("Add at least one complete job.")
        if len(jobs) > MAX_BATCH_JOBS:
            raise ValueError(f"A batch can contain at most {MAX_BATCH_JOBS} jobs.")
        return jobs


class ImportPreviewRequest(BaseModel):
    filename: str
    content_base64: str


def _import_cell_text(value: object) -> str:
    return normalize_name(str(value)) if value is not None else ""


def _build_import_preview(raw_rows) -> dict:
    iterator = iter(raw_rows)
    header_row = next(iterator, None)
    if header_row is None:
        return {
            "headers_valid": False,
            "rows": [],
            "errors": ["The file is empty."],
            "nonempty_records": 0,
            "can_import": False,
        }

    positions: dict[str, int] = {}
    header_errors: list[str] = []
    for index, value in enumerate(header_row):
        header = _import_cell_text(value).casefold()
        if not header:
            continue
        if header not in IMPORT_FIELDS:
            header_errors.append(f"Unexpected column: {header!r}.")
        elif header in positions:
            header_errors.append(f"Duplicate column: {header!r}.")
        else:
            positions[header] = index

    for header in IMPORT_FIELDS:
        if header not in positions:
            header_errors.append(f"Missing required column: {header.title()}.")
    if len(positions) != len(IMPORT_FIELDS):
        header_errors.append(
            "The file must contain exactly Workspace Name, Semantic Model, "
            "and Report Name columns."
        )
    if header_errors:
        return {
            "headers_valid": False,
            "rows": [],
            "errors": header_errors,
            "nonempty_records": 0,
            "can_import": False,
        }

    preview_rows = []
    nonempty_records = 0
    expected_indexes = set(positions.values())
    for row_number, raw_row in enumerate(iterator, start=2):
        cells = list(raw_row)
        values = {
            field: _import_cell_text(
                cells[positions[header]]
                if positions[header] < len(cells)
                else None
            )
            for header, field in IMPORT_FIELDS.items()
        }
        unexpected_data = any(
            _import_cell_text(value)
            for index, value in enumerate(cells)
            if index not in expected_indexes
        )
        if not any(values.values()) and not unexpected_data:
            continue
        nonempty_records += 1
        row_errors = [
            f"{field.replace('_', ' ').title()} is required."
            for field, value in values.items()
            if not value
        ]
        if unexpected_data:
            row_errors.append("Unexpected data outside the named columns.")
        preview_rows.append(
            {
                "row_number": row_number,
                **values,
                "errors": row_errors,
            }
        )
        if nonempty_records > MAX_BATCH_JOBS:
            break

    errors = []
    if nonempty_records > MAX_BATCH_JOBS:
        errors.append(
            f"The file contains more than {MAX_BATCH_JOBS} non-empty records."
        )
    elif not preview_rows:
        errors.append("The file contains no non-empty records.")
    return {
        "headers_valid": True,
        "rows": preview_rows,
        "errors": errors,
        "nonempty_records": nonempty_records,
        "too_many_records": nonempty_records > MAX_BATCH_JOBS,
        "can_import": (
            not errors
            and bool(preview_rows)
            and all(not row["errors"] for row in preview_rows)
        ),
    }


def _parse_batch_import(filename: str, content_base64: str) -> dict:
    extension = Path(filename).suffix.casefold()
    if extension not in {".csv", ".xlsx"}:
        raise HTTPException(
            status_code=415, detail="Choose a .csv or .xlsx file."
        )
    if len(content_base64) > ((MAX_IMPORT_BYTES + 2) // 3) * 4:
        raise HTTPException(status_code=413, detail="Import files must be 5 MB or smaller.")
    try:
        content = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail="The uploaded file data is invalid."
        ) from exc
    if len(content) > MAX_IMPORT_BYTES:
        raise HTTPException(
            status_code=413, detail="Import files must be 5 MB or smaller."
        )

    if extension == ".csv":
        try:
            text = content.decode("utf-8-sig")
            return _build_import_preview(csv.reader(io.StringIO(text, newline="")))
        except (UnicodeDecodeError, csv.Error) as exc:
            raise HTTPException(
                status_code=422, detail="The CSV file could not be parsed as UTF-8."
            ) from exc

    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except (InvalidFileException, OSError, ValueError, zipfile.BadZipFile) as exc:
        raise HTTPException(
            status_code=422, detail="The XLSX file could not be opened."
        ) from exc
    try:
        if not workbook.worksheets:
            return _build_import_preview([])
        return _build_import_preview(
            workbook.active.iter_rows(values_only=True)
        )
    finally:
        workbook.close()


PROGRESS_STEPS = (
    "Validate inputs",
    "Discover Dashboard A workspace",
    "Discover Dashboard A report",
    "Open Dashboard A",
    "Discover Dashboard B workspace",
    "Discover Dashboard B report",
    "Open Dashboard B",
    "Validate dashboard pair",
    "Capture screenshots",
    "Execute visual comparison",
    "Run four functional scenarios",
    "Validate scenario evidence",
    "Generate final reports",
)

def _storage_state_path() -> Path:
    configured_path = Path(os.environ.get("POWERBI_STORAGE_STATE", "auth.json"))
    if not configured_path.is_absolute():
        configured_path = PROJECT_ROOT / configured_path
    return configured_path


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((FRONTEND_ROOT / "index.html").read_text(encoding="utf-8"))


@app.get("/api/config")
async def get_config() -> dict:
    storage_state = _storage_state_path()
    return {
        "project_id": os.environ.get("GOOGLE_CLOUD_PROJECT", DEFAULT_PROJECT_ID),
        "location": os.environ.get("GOOGLE_CLOUD_LOCATION", DEFAULT_LOCATION),
        "model": os.environ.get("GEMINI_MODEL", DEFAULT_MODEL_ID),
        "authentication_ready": storage_state.is_file(),
    }


@app.post("/api/batches/import-preview")
async def preview_batch_import(request: ImportPreviewRequest) -> dict:
    return _parse_batch_import(request.filename, request.content_base64)


@app.post("/api/compare", status_code=202)
async def start_comparison(request: CompareRequest) -> dict:
    storage_state = _storage_state_path()
    if not storage_state.is_file():
        raise HTTPException(
            status_code=503,
            detail="Power BI authentication is missing. Create a Playwright storage-state file first.",
        )

    global ACTIVE_JOB_ID
    async with JOB_LOCK:
        if ACTIVE_JOB_ID is not None or ACTIVE_BATCH_ID is not None:
            raise HTTPException(
                status_code=409, detail="A comparison or batch is already running."
            )
        job_id = str(uuid4())
        JOBS[job_id] = {
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "started_monotonic": None,
            "elapsed_seconds": 0.0,
            "summary": None,
            "error": None,
            "progress": [
                {"step": name, "status": "pending", "message": None}
                for name in PROGRESS_STEPS
            ],
        }
        ACTIVE_JOB_ID = job_id
        task = asyncio.create_task(_run_comparison(job_id, request, storage_state))
        JOB_TASKS.add(task)
        task.add_done_callback(JOB_TASKS.discard)
    return {"job_id": job_id, "status": "queued"}


@app.post("/api/batches", status_code=202)
async def start_batch(request: BatchRequest) -> dict:
    storage_state = _storage_state_path()
    if not storage_state.is_file():
        raise HTTPException(
            status_code=503,
            detail="Power BI authentication is missing. Create a Playwright storage-state file first.",
        )

    global ACTIVE_BATCH_ID
    async with JOB_LOCK:
        if ACTIVE_JOB_ID is not None or ACTIVE_BATCH_ID is not None:
            raise HTTPException(
                status_code=409, detail="A comparison or batch is already running."
            )
        batch_id = str(uuid4())
        batch_jobs = []
        for number, request_row in enumerate(request.jobs, start=1):
            job_id = str(uuid4())
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
            JOBS[job_id] = {
                "status": "queued",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "started_monotonic": None,
                "elapsed_seconds": 0.0,
                "summary": None,
                "error": None,
                "progress": [
                    {"step": name, "status": "pending", "message": None}
                    for name in PROGRESS_STEPS
                ],
                "batch_id": batch_id,
                "batch_job_number": number,
            }
        BATCHES[batch_id] = {
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "current_job_number": None,
            "stop_on_first_failure": request.stop_on_first_failure,
            "jobs": batch_jobs,
            "error": None,
        }
        ACTIVE_BATCH_ID = batch_id
        task = asyncio.create_task(_run_batch(batch_id, request, storage_state))
        JOB_TASKS.add(task)
        task.add_done_callback(JOB_TASKS.discard)
    return {"batch_id": batch_id, "status": "queued"}


async def _execute_comparison(
    job_id: str, request: CompareRequest, storage_state: Path
) -> dict:
    output_dir = _prepare_job_output(job_id)
    configure_vertex_ai()
    job = JOBS[job_id]
    job.update(
        workspace_name=request.workspace_name,
        semantic_model=request.semantic_model,
        report_name=request.report_name,
    )

    def update_progress(
        step_number: int, status: str, message: str | None = None
    ) -> None:
        progress = job["progress"]
        progress[step_number - 1]["status"] = status
        progress[step_number - 1]["message"] = message

    update_progress(1, "in_progress")
    update_progress(1, "completed")
    update_progress(2, "in_progress")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            discovery = await discover_dashboard_pair(
                workspace_name=request.workspace_name,
                semantic_model=request.semantic_model,
                report_name=request.report_name,
                storage_state=storage_state,
                on_progress=update_progress,
                browser=browser,
            )
            (output_dir / "discovery.json").write_text(
                json.dumps(
                    discovery.discovery,
                    indent=2,
                    ensure_ascii=False,
                    allow_nan=False,
                ),
                encoding="utf-8",
            )
            job["dashboard_a_url"] = discovery.url_a
            job["dashboard_b_url"] = discovery.url_b
            scenario_execution = None
            scenario_state_loader = None
            if request.run_validation_scenarios:
                from .scenario_state_adapter import (
                    load_scenario_states as adapt_scenario_states,
                )
                from .validation_scenarios import (
                    discover_scenario_pages,
                    run_validation_scenarios as execute_validation_scenarios,
                )

                operation_log_path = output_dir / "operation_timings.jsonl"
                update_progress(11, "in_progress")
                page_names = await discover_scenario_pages(
                    url_a=discovery.url_a,
                    storage_state=storage_state,
                    headed=False,
                    destination=output_dir,
                    job_id=job_id,
                    operation_log_path=operation_log_path,
                    browser=browser,
                )
                scenario_execution = await execute_validation_scenarios(
                    url_a=discovery.url_a,
                    url_b=discovery.url_b,
                    page_names=page_names,
                    destination=output_dir,
                    storage_state=storage_state,
                    headed=False,
                    settle_seconds=2.0,
                    browser=browser,
                    job_id=job_id,
                    operation_log_path=operation_log_path,
                )
                update_progress(
                    11,
                    "completed",
                    "Scenario screenshots and execution evidence captured.",
                )

                async def load_scenario_state_pair():
                    update_progress(
                        12,
                        "in_progress",
                        "Validating evidence and extracting scenario screenshots with Vision.",
                    )
                    states_a, states_b, context = await adapt_scenario_states(
                        destination=output_dir,
                        job_id=job_id,
                        operation_log_path=operation_log_path,
                        execution=scenario_execution,
                    )
                    update_progress(
                        12,
                        "completed",
                        "Scenario evidence validated and added to the existing comparison.",
                    )
                    return states_a, states_b, context

                scenario_state_loader = load_scenario_state_pair
            else:
                update_progress(
                    11, "skipped", "Functional scenarios were not selected."
                )
                update_progress(
                    12, "skipped", "Functional scenarios were not selected."
                )
            result = await run_dashboard_comparison(
                job_id=job_id,
                url_a=discovery.url_a,
                url_b=discovery.url_b,
                output_dir=output_dir,
                storage_state=storage_state,
                progress_callback=update_progress,
                browser=browser,
                report_metadata=discovery.discovery,
                generate_report=True,
                scenario_state_loader=scenario_state_loader,
            )
        finally:
            await browser.close()
    _finalize_job_output(job_id, output_dir, result)
    return result["summary"]


def _prepare_job_output(job_id: str) -> Path:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    output_dir = OUTPUT_ROOT / job_id
    output_dir.mkdir(parents=True, exist_ok=True)

    job_directories = sorted(
        (
            path
            for path in OUTPUT_ROOT.iterdir()
            if path.is_dir() and not path.is_symlink()
        ),
        key=lambda path: (path == output_dir, path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    for path in job_directories[MAX_RETAINED_JOBS:]:
        shutil.rmtree(path)
    return output_dir


def _first_report_title(result: dict) -> str | None:
    evidence = result.get("screenshot_evidence")
    dashboard_a = evidence.get("dashboard_a") if isinstance(evidence, dict) else None
    if not isinstance(dashboard_a, dict):
        return None
    for state in dashboard_a.values():
        if isinstance(state, dict):
            title = state.get("report_title")
            if isinstance(title, str) and title.strip():
                return title.strip()
    return None


def _safe_report_name(report_name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", report_name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    name = name[:100].rstrip(" .") or "report"
    return name


def _safe_report_folder_name(report_title: str, job_id: str) -> str:
    return f"{_safe_report_name(report_title)}_{job_id}"


def _finalize_job_output(job_id: str, output_dir: Path, result: dict) -> Path:
    report_title = _first_report_title(result)
    has_validation_scenarios = "validation_scenarios" in result
    if report_title and not has_validation_scenarios:
        named_output_dir = OUTPUT_ROOT / _safe_report_folder_name(
            report_title, job_id
        )
        try:
            output_dir.rename(named_output_dir)
        except PermissionError:
            logger.warning(
                "Could not rename job output folder for %s; keeping UUID folder %s",
                job_id,
                output_dir,
                exc_info=True,
            )
            JOBS[job_id]["output_dir"] = str(output_dir)
            return output_dir
        output_dir = named_output_dir
        JOBS[job_id]["output_dir"] = str(output_dir)

        screenshot_evidence = result.get("screenshot_evidence")
        if isinstance(screenshot_evidence, dict):
            old_path = str(named_output_dir.parent / job_id)
            for states in screenshot_evidence.values():
                if not isinstance(states, dict):
                    continue
                for state in states.values():
                    if isinstance(state, dict) and isinstance(state.get("path"), str):
                        state["path"] = state["path"].replace(old_path, str(output_dir))

        comparison_path = output_dir / "comparison.json"
        comparison_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )
    else:
        JOBS[job_id]["output_dir"] = str(output_dir)
        if has_validation_scenarios:
            (output_dir / "comparison.json").write_text(
                json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False),
                encoding="utf-8",
            )
    return output_dir


def _job_output_dir(job_id: str) -> Path:
    canonical_id = str(UUID(job_id))
    job = _job(canonical_id)
    output_dir = job.get("output_dir")
    return Path(output_dir) if output_dir else OUTPUT_ROOT / canonical_id


def _available_job_outputs(job_id: str) -> dict[str, Path]:
    output_dir = _job_output_dir(job_id)
    return {
        key: path
        for key, path in {
            "docx": output_dir / "Dashboard_Comparison.docx",
            "xlsx": output_dir / "Dashboard_Comparison.xlsx",
            "validation_json": output_dir / "validation_summary.json",
        }.items()
        if path.is_file()
    }


def _job_download_folder_ready(job_id: str, job: dict | None = None) -> bool:
    record = job if job is not None else _job(job_id)
    return (
        set(_available_job_outputs(job_id)) == {"docx", "xlsx"}
        and bool(record.get("dashboard_a_url"))
        and bool(record.get("dashboard_b_url"))
    )


async def _run_comparison(
    job_id: str, request: CompareRequest, storage_state: Path
) -> None:
    global ACTIVE_JOB_ID
    job = JOBS[job_id]
    job["status"] = "running"
    job["started_monotonic"] = time.monotonic()
    work_task = asyncio.create_task(
        _execute_comparison(job_id, request, storage_state)
    )
    try:
        job["summary"] = await work_task
        job["status"] = "completed"
    except Exception as exc:
        logger.exception("Comparison job %s failed", job_id)
        job["error"] = f"{type(exc).__name__}: {exc}"
        job["status"] = "failed"
        progress = job.get("progress", [])
        active_step = next(
            (step for step in progress if step["status"] == "in_progress"),
            None,
        )
        failed_step = active_step or next(
            (step for step in progress if step["status"] == "pending"),
            None,
        )
        if failed_step is not None:
            failed_step["status"] = "failed"
            failed_step["message"] = job["error"]
    finally:
        started_monotonic = job.get("started_monotonic")
        if started_monotonic is not None:
            job["elapsed_seconds"] = time.monotonic() - started_monotonic
        async with JOB_LOCK:
            if ACTIVE_JOB_ID == job_id:
                ACTIVE_JOB_ID = None


async def _run_batch(
    batch_id: str, request: BatchRequest, storage_state: Path
) -> None:
    global ACTIVE_BATCH_ID, ACTIVE_JOB_ID
    batch = BATCHES[batch_id]
    batch["status"] = "running"
    failed_job_seen = False
    try:
        for index, batch_job in enumerate(batch["jobs"]):
            if failed_job_seen and batch["stop_on_first_failure"]:
                batch_job["status"] = "skipped"
                batch_job["error"] = "Skipped because an earlier job failed."
                JOBS[batch_job["job_id"]]["status"] = "skipped"
                JOBS[batch_job["job_id"]]["error"] = batch_job["error"]
                continue

            job_id = batch_job["job_id"]
            batch["current_job_number"] = batch_job["number"]
            batch_job["status"] = "in_progress"
            async with JOB_LOCK:
                if ACTIVE_BATCH_ID != batch_id:
                    raise RuntimeError("The active batch changed unexpectedly.")
                ACTIVE_JOB_ID = job_id

            request_row = request.jobs[index]
            await _run_comparison(
                job_id,
                CompareRequest(
                    workspace_name=request_row.workspace_name,
                    semantic_model=request_row.semantic_model,
                    report_name=request_row.report_name,
                    run_validation_scenarios=request.run_validation_scenarios,
                ),
                storage_state,
            )
            job = JOBS[job_id]
            batch_job["status"] = job["status"]
            batch_job["summary"] = job.get("summary")
            batch_job["error"] = job.get("error")
            if job["status"] == "failed":
                failed_job_seen = True

        batch["status"] = "completed"
    except Exception as exc:
        logger.exception("Batch %s failed while running", batch_id)
        batch["status"] = "failed"
        batch["error"] = f"{type(exc).__name__}: {exc}"
        for batch_job in batch["jobs"]:
            if batch_job["status"] == "in_progress":
                batch_job["status"] = "failed"
                batch_job["error"] = batch["error"]
                JOBS[batch_job["job_id"]]["status"] = "failed"
                JOBS[batch_job["job_id"]]["error"] = batch["error"]
            elif batch_job["status"] == "pending":
                batch_job["status"] = "skipped"
                batch_job["error"] = "Skipped because the batch could not continue."
                JOBS[batch_job["job_id"]]["status"] = "skipped"
                JOBS[batch_job["job_id"]]["error"] = batch_job["error"]
    finally:
        batch["current_job_number"] = None
        batch["finished_at"] = datetime.now(timezone.utc).isoformat()
        async with JOB_LOCK:
            if (
                ACTIVE_JOB_ID is not None
                and JOBS.get(ACTIVE_JOB_ID, {}).get("batch_id") == batch_id
            ):
                ACTIVE_JOB_ID = None
            if ACTIVE_BATCH_ID == batch_id:
                ACTIVE_BATCH_ID = None


def _batch(batch_id: str) -> tuple[str, dict]:
    try:
        canonical_id = str(UUID(batch_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Batch not found.") from exc
    batch = BATCHES.get(canonical_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found.")
    return canonical_id, batch


def _batch_counts(batch: dict) -> dict[str, int]:
    statuses = [job["status"] for job in batch["jobs"]]
    return {
        "total": len(statuses),
        "completed": statuses.count("completed"),
        "failed": statuses.count("failed"),
        "skipped": statuses.count("skipped"),
    }


def _batch_payload(batch_id: str, batch: dict) -> dict:
    current_job_number = batch.get("current_job_number")
    current_stage = None
    jobs = []
    for batch_job in batch["jobs"]:
        job = JOBS[batch_job["job_id"]]
        progress = job.get("progress", [])
        available_outputs = _available_job_outputs(batch_job["job_id"])
        download_folder_ready = (
            batch_job["status"] in {"completed", "failed"}
            and _job_download_folder_ready(batch_job["job_id"], job)
        )
        if batch_job["number"] == current_job_number:
            current_stage = next(
                (
                    {"step": step["step"], "message": step.get("message")}
                    for step in progress
                    if step["status"] == "in_progress"
                ),
                None,
            )
        jobs.append(
            {
                **batch_job,
                "progress": progress,
                "elapsed_seconds": job.get("elapsed_seconds", 0.0),
                "dashboard_a_url": job.get("dashboard_a_url"),
                "dashboard_b_url": job.get("dashboard_b_url"),
                "available_outputs": sorted(available_outputs),
                "download_folder_ready": download_folder_ready,
            }
        )
    downloadable_job_count = sum(
        job["download_folder_ready"] for job in jobs
    )
    return {
        "batch_id": batch_id,
        "status": batch["status"],
        **_batch_counts(batch),
        "downloadable_job_count": downloadable_job_count,
        "current_job_number": current_job_number,
        "current_stage": current_stage,
        "error": batch.get("error"),
        "jobs": jobs,
    }


@app.get("/api/batches/{batch_id}")
async def get_batch_status(batch_id: str) -> dict:
    canonical_id, batch = _batch(batch_id)
    return _batch_payload(canonical_id, batch)


@app.get("/api/batches/{batch_id}/summary.json")
async def get_batch_summary(batch_id: str) -> JSONResponse:
    canonical_id, batch = _batch(batch_id)
    payload = _batch_payload(canonical_id, batch)
    summary = {
        key: payload[key]
        for key in ("batch_id", "status", "total", "completed", "failed", "skipped", "error")
    }
    summary["jobs"] = [
        {
            key: job[key]
            for key in (
                "number",
                "job_id",
                "workspace_name",
                "semantic_model",
                "report_name",
                "status",
                "summary",
                "error",
            )
        }
        for job in payload["jobs"]
    ]
    return JSONResponse(
        content=summary,
        headers={
            "Content-Disposition": f'attachment; filename="batch-{canonical_id}-summary.json"'
        },
    )


def _write_job_archive(
    archive: zipfile.ZipFile, job_id: str, job: dict, folder_name: str
) -> bool:
    output_dir = _job_output_dir(job_id)
    docx_path = output_dir / "Dashboard_Comparison.docx"
    xlsx_path = output_dir / "Dashboard_Comparison.xlsx"
    dashboard_a_url = job.get("dashboard_a_url")
    dashboard_b_url = job.get("dashboard_b_url")
    if not (
        docx_path.is_file()
        and xlsx_path.is_file()
        and dashboard_a_url
        and dashboard_b_url
    ):
        return False

    report_stem = _safe_report_name(job.get("report_name") or "Report")
    archive.write(docx_path, f"{folder_name}/{report_stem}.docx")
    archive.write(xlsx_path, f"{folder_name}/{report_stem}.xlsx")
    archive.writestr(
        f"{folder_name}/PowerBI_Links.txt",
        f"Dashboard A (Snowflake): {dashboard_a_url}\n"
        f"Dashboard B (GCP): {dashboard_b_url}\n",
    )
    for path, archive_name in (
        (output_dir / "validation_summary.json", "Validation_Execution_Summary.json"),
    ):
        if path.is_file():
            archive.write(path, f"{folder_name}/{archive_name}")
    return True


def _archive_response(archive_path: Path, filename: str) -> FileResponse:
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(archive_path.unlink, missing_ok=True),
    )


@app.get("/api/batches/{batch_id}/outputs.zip")
async def get_batch_output_folder(batch_id: str) -> FileResponse:
    canonical_id, batch = _batch(batch_id)
    included_jobs = [
        job
        for job in batch["jobs"]
        if job["status"] in {"completed", "failed"}
        and _job_download_folder_ready(
            job["job_id"], JOBS[job["job_id"]]
        )
    ]
    if not included_jobs:
        raise HTTPException(
            status_code=409,
            detail="No completed report files are available to download yet.",
        )

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    archive_path = OUTPUT_ROOT / f".batch-download-{uuid4().hex}.zip"
    archive_written = False
    try:
        with zipfile.ZipFile(
            archive_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for job in included_jobs:
                folder_name = (
                    f"{job['number']:02d}-"
                    f"{_safe_report_folder_name(job['report_name'], job['job_id'])}"
                )
                _write_job_archive(
                    archive,
                    job["job_id"],
                    {**JOBS[job["job_id"]], **job},
                    folder_name,
                )
        archive_written = True
    finally:
        if not archive_written:
            archive_path.unlink(missing_ok=True)

    return _archive_response(
        archive_path, f"Dashboard_Comparison_Batch_{canonical_id}.zip"
    )


def _job(job_id: str) -> dict:
    try:
        canonical_id = str(UUID(job_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Comparison job not found.") from exc
    job = JOBS.get(canonical_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Comparison job not found.")
    return job


@app.get("/api/jobs/{job_id}/outputs.zip")
async def get_job_output_folder(job_id: str) -> FileResponse:
    job = _job(job_id)
    canonical_id = str(UUID(job_id))
    if job["status"] not in {"completed", "failed"}:
        raise HTTPException(
            status_code=409,
            detail="This comparison has no completed report package to download yet.",
        )
    if not _job_download_folder_ready(canonical_id, job):
        raise HTTPException(
            status_code=409,
            detail="Both report files and both Power BI links must be available before downloading.",
        )

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    archive_path = OUTPUT_ROOT / f".job-download-{uuid4().hex}.zip"
    archive_written = False
    try:
        with zipfile.ZipFile(
            archive_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            report_name = _safe_report_name(job.get("report_name", "Report"))
            folder_name = report_name
            _write_job_archive(archive, canonical_id, job, folder_name)
        archive_written = True
    finally:
        if not archive_written:
            archive_path.unlink(missing_ok=True)

    folder_name = _safe_report_folder_name(
        job.get("report_name", "Report"), canonical_id
    )
    return _archive_response(
        archive_path, f"{report_name}_outputs.zip"
    )


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str) -> dict:
    job = _job(job_id)
    started_monotonic = job.get("started_monotonic")
    elapsed_seconds = (
        time.monotonic() - started_monotonic
        if job["status"] == "running" and started_monotonic is not None
        else job.get("elapsed_seconds", 0.0)
    )
    return {
        "job_id": str(UUID(job_id)),
        "status": job["status"],
        "elapsed_seconds": elapsed_seconds,
        "summary": job["summary"],
        "error": job["error"],
        "progress": job.get("progress", []),
        "available_outputs": sorted(_available_job_outputs(job_id)),
    }


@app.get("/api/jobs/{job_id}/comparison.json")
async def get_json_report(job_id: str) -> FileResponse:
    path = _job_output_dir(job_id) / "comparison.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="JSON report is not ready.")
    return FileResponse(path, media_type="application/json", filename="comparison.json")


@app.get("/api/jobs/{job_id}/discovery.json")
async def get_discovery_report(job_id: str) -> FileResponse:
    path = _job_output_dir(job_id) / "discovery.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Discovery details are not ready.")
    return FileResponse(
        path, media_type="application/json", filename="discovery.json"
    )


@app.get("/api/jobs/{job_id}/Dashboard_Comparison.xlsx")
async def get_excel_report(job_id: str) -> FileResponse:
    job = _job(job_id)
    path = _job_output_dir(job_id) / "Dashboard_Comparison.xlsx"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Excel report is not ready.")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"{_safe_report_name(job.get('report_name') or 'Report')}.xlsx",
    )


@app.get("/api/jobs/{job_id}/Dashboard_Comparison.docx")
async def get_docx_report(job_id: str) -> FileResponse:
    job = _job(job_id)
    path = _job_output_dir(job_id) / "Dashboard_Comparison.docx"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="DOCX report is not ready.")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"{_safe_report_name(job.get('report_name') or 'Report')}.docx",
    )


@app.get("/api/jobs/{job_id}/validation-summary.json")
async def get_validation_execution_summary(job_id: str) -> FileResponse:
    path = _job_output_dir(job_id) / "validation_summary.json"
    if not path.is_file():
        raise HTTPException(
            status_code=404, detail="Validation execution summary is not ready."
        )
    return FileResponse(path, media_type="application/json", filename=path.name)
