import asyncio
import json
import logging
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_EVENT_LOG_LOCK = threading.Lock()


def write_operation_event(
    path: Path | None,
    *,
    job_id: str,
    operation: str,
    event: str,
    outcome: str,
    dashboard: str | None = None,
    page: str | None = None,
    filter_state: str | None = None,
    visual: str | None = None,
    duration_seconds: float | None = None,
    error_type: str | None = None,
) -> None:
    record: dict[str, Any] = {
        "timestamp_unix_seconds": time.time(),
        "job_id": job_id,
        "dashboard": dashboard,
        "page": page,
        "filter_state": filter_state,
        "visual": visual,
        "operation": operation,
        "event": event,
        "outcome": outcome,
    }
    if duration_seconds is not None:
        record["duration_seconds"] = round(duration_seconds, 3)
    if error_type is not None:
        record["error_type"] = error_type

    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with _EVENT_LOG_LOCK, path.open("a", encoding="utf-8") as event_file:
            event_file.write(line)

    logger.info(
        "operation=%s event=%s job_id=%s dashboard=%s page=%s "
        "filter_state=%s outcome=%s duration_seconds=%s error_type=%s",
        operation,
        event,
        job_id,
        dashboard or "-",
        page or "-",
        filter_state or "-",
        outcome,
        f"{duration_seconds:.3f}" if duration_seconds is not None else "-",
        error_type or "-",
    )


@contextmanager
def measure_stage(
    path: Path | None,
    *,
    job_id: str,
    operation: str,
    dashboard: str | None = None,
    page: str | None = None,
    filter_state: str | None = None,
) -> Iterator[None]:
    started_at = time.perf_counter()
    write_operation_event(
        path,
        job_id=job_id,
        operation=operation,
        event="started",
        outcome="in_progress",
        dashboard=dashboard,
        page=page,
        filter_state=filter_state,
    )
    try:
        yield
    except BaseException as exc:
        outcome = (
            "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
        )
        write_operation_event(
            path,
            job_id=job_id,
            operation=operation,
            event="finished",
            outcome=outcome,
            dashboard=dashboard,
            page=page,
            filter_state=filter_state,
            duration_seconds=time.perf_counter() - started_at,
            error_type=type(exc).__name__,
        )
        raise
    else:
        write_operation_event(
            path,
            job_id=job_id,
            operation=operation,
            event="finished",
            outcome="success",
            dashboard=dashboard,
            page=page,
            filter_state=filter_state,
            duration_seconds=time.perf_counter() - started_at,
        )
