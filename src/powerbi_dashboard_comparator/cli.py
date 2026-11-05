import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from uuid import uuid4

from .comparison import compare_dashboards, write_comparison
from .config import (
    DEFAULT_LOCATION,
    DEFAULT_MODEL_ID,
    DEFAULT_PROJECT_ID,
    configure_vertex_ai,
)
from .timing import write_operation_event


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture and compare two Power BI dashboard URLs."
    )
    parser.add_argument("--url-a", required=True, help="First Power BI report URL")
    parser.add_argument("--url-b", required=True, help="Second Power BI report URL")
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument(
        "--storage-state",
        type=Path,
        help="Playwright storage-state JSON for authenticated report access",
    )
    parser.add_argument("--headed", action="store_true", help="Show the Chromium window")
    parser.add_argument("--settle-seconds", type=float, default=2.0)
    parser.add_argument(
        "--include-filter-cases",
        action="store_true",
        help=(
            "Open report slicers to inventory their options and capture "
            "per-filter cases; default captures only visible selections."
        ),
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("GEMINI_MODEL", DEFAULT_MODEL_ID),
        help="Gemini model ID",
    )
    parser.add_argument(
        "--project-id",
        default=os.environ.get("GOOGLE_CLOUD_PROJECT", DEFAULT_PROJECT_ID),
        help="Google Cloud project ID for Vertex AI",
    )
    parser.add_argument(
        "--location",
        default=os.environ.get("GOOGLE_CLOUD_LOCATION", DEFAULT_LOCATION),
        help="Vertex AI region",
    )
    return parser


async def _run(args: argparse.Namespace) -> None:
    if args.settle_seconds < 0:
        raise ValueError("--settle-seconds cannot be negative")
    if args.storage_state is not None and not args.storage_state.is_file():
        raise FileNotFoundError(f"Storage state file not found: {args.storage_state}")
    job_id = uuid4().hex
    operation_log_path = args.output_dir / "operation_timings.jsonl"
    started_at = time.perf_counter()
    write_operation_event(
        operation_log_path,
        job_id=job_id,
        operation="job",
        event="started",
        outcome="in_progress",
    )
    try:
        configure_vertex_ai(args.project_id, args.location, args.model)

        from .browser import capture_dashboard_pair

        states_a, states_b = await capture_dashboard_pair(
            url_a=args.url_a,
            url_b=args.url_b,
            destination=args.output_dir,
            storage_state=args.storage_state,
            headed=args.headed,
            settle_seconds=args.settle_seconds,
            include_filter_cases=args.include_filter_cases,
            detect_visual_types=True,
            job_id=job_id,
            operation_log_path=operation_log_path,
        )
        result = compare_dashboards(states_a, states_b)
        write_comparison(
            result,
            args.output_dir,
            "Dashboard A",
            "Dashboard B",
            job_id=job_id,
            operation_log_path=operation_log_path,
        )
        print(json.dumps(result["summary"], indent=2, ensure_ascii=False))
    except BaseException as exc:
        write_operation_event(
            operation_log_path,
            job_id=job_id,
            operation="job",
            event="finished",
            outcome=(
                "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            ),
            duration_seconds=time.perf_counter() - started_at,
            error_type=type(exc).__name__,
        )
        raise
    else:
        write_operation_event(
            operation_log_path,
            job_id=job_id,
            operation="job",
            event="finished",
            outcome="success",
            duration_seconds=time.perf_counter() - started_at,
        )


def main() -> None:
    args = _parser().parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
