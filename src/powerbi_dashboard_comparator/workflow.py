import asyncio
import time
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from google.adk.agents import SequentialAgent
from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import PrivateAttr

from .browser import capture_dashboard_pair
from .comparison import (
    _validate_comparison_output,
    compare_dashboards,
    merge_incremental_comparisons,
    write_comparison,
)
from .config import positive_int_setting
from .models import DashboardState
from .timing import measure_stage, write_operation_event

APP_NAME = "powerbi_dashboard_comparator"
USER_ID = "dashboard_comparator"
COMPARISON_WORKERS = positive_int_setting("PBI_MAX_PARALLEL_COMPARISONS", 4)


class ComparisonWorkflowRun:
    def __init__(
        self,
        job_id: str,
        url_a: str,
        url_b: str,
        output_dir: Path,
        storage_state: Path,
    ) -> None:
        self.job_id = job_id
        self.url_a = url_a
        self.url_b = url_b
        self.output_dir = output_dir
        self.storage_state = storage_state
        self.states_a: list[DashboardState] = []
        self.states_b: list[DashboardState] = []
        self.partial_comparisons: dict[str, dict[str, Any]] = {}
        self.result: dict[str, Any] | None = None
        self.operation_log_path = output_dir / "operation_timings.jsonl"
        self.started_at = time.perf_counter()
        self.finished = False
        write_operation_event(
            self.operation_log_path,
            job_id=self.job_id,
            operation="job",
            event="started",
            outcome="in_progress",
        )

    async def run_step(self, step: str) -> None:
        with measure_stage(
            self.operation_log_path,
            job_id=self.job_id,
            operation=f"workflow_{step}",
        ):
            if step == "capture":
                await self._capture()
            elif step == "validate":
                _validate_captured_states(self.states_a, self.states_b)
            elif step == "compare":
                self.result = (
                    merge_incremental_comparisons(
                        self.partial_comparisons, self.states_a, self.states_b
                    )
                    if self.partial_comparisons
                    else compare_dashboards(self.states_a, self.states_b)
                )
            elif step == "report":
                self._write_report()
            else:
                raise ValueError(f"Unknown dashboard workflow step: {step}")

    async def _capture(self) -> None:
        completed_states: asyncio.Queue[
            tuple[str, DashboardState] | None
        ] = asyncio.Queue()

        def publish_state(dashboard: str, state: DashboardState) -> None:
            completed_states.put_nowait((dashboard, state))

        async def capture_pair() -> tuple[list[DashboardState], list[DashboardState]]:
            try:
                return await capture_dashboard_pair(
                    url_a=self.url_a,
                    url_b=self.url_b,
                    destination=self.output_dir,
                    storage_state=self.storage_state,
                    headed=False,
                    settle_seconds=2.0,
                    extract_visual_data=False,
                    job_id=self.job_id,
                    operation_log_path=self.operation_log_path,
                    on_state_captured=publish_state,
                    detect_visual_types=True,
                )
            finally:
                completed_states.put_nowait(None)

        capture_task = asyncio.create_task(capture_pair())
        states_by_side: dict[str, dict[str, DashboardState]] = {
            "dashboard_a": {},
            "dashboard_b": {},
        }
        comparison_tasks: dict[str, asyncio.Task[dict[str, Any]]] = {}
        comparison_pool = ThreadPoolExecutor(
            max_workers=COMPARISON_WORKERS,
            thread_name_prefix="state-comparison",
        )
        loop = asyncio.get_running_loop()
        streamed_states = False

        async def compare_state_pair(
            state_a: DashboardState | None,
            state_b: DashboardState | None,
        ) -> dict[str, Any]:
            with measure_stage(
                self.operation_log_path,
                job_id=self.job_id,
                operation="incremental_state_comparison",
                page=(state_a or state_b).page if state_a or state_b else None,
                filter_state=(
                    (state_a or state_b).filter.key
                    if (state_a or state_b)
                    and (state_a or state_b).filter is not None
                    else "initial"
                ),
            ):
                return await loop.run_in_executor(
                    comparison_pool,
                    compare_dashboards,
                    [state_a] if state_a is not None else [],
                    [state_b] if state_b is not None else [],
                )

        processed_keys: set[str] = set()
        try:
            while True:
                event = await completed_states.get()
                if event is None:
                    break
                streamed_states = True
                dashboard, state = event
                side = dashboard.casefold()
                if side not in states_by_side:
                    raise ValueError(
                        f"Capture returned an unknown dashboard side {dashboard!r}"
                    )
                _validate_captured_states(
                    [state] if side == "dashboard_a" else [],
                    [state] if side == "dashboard_b" else [],
                )
                key = state.state_key.casefold()
                side_states = states_by_side[side]
                if key in side_states:
                    raise ValueError(
                        f"Dashboard {dashboard} returned duplicate state {state.state_key!r}"
                    )
                side_states[key] = state

                if key in processed_keys:
                    continue
                state_a = states_by_side["dashboard_a"].get(key)
                state_b = states_by_side["dashboard_b"].get(key)
                if state_a is not None and state_b is not None:
                    processed_keys.add(key)
                    comparison_tasks[key] = asyncio.create_task(
                        compare_state_pair(state_a, state_b)
                    )

            self.states_a, self.states_b = await capture_task
            if streamed_states:
                _validate_captured_states(self.states_a, self.states_b)
                states_by_side = {
                    "dashboard_a": {
                        state.state_key.casefold(): state
                        for state in self.states_a
                    },
                    "dashboard_b": {
                        state.state_key.casefold(): state
                        for state in self.states_b
                    },
                }
                for key in sorted(
                    set(states_by_side["dashboard_a"])
                    | set(states_by_side["dashboard_b"])
                ):
                    if key in processed_keys:
                        continue
                    processed_keys.add(key)
                    comparison_tasks[key] = asyncio.create_task(
                        compare_state_pair(
                            states_by_side["dashboard_a"].get(key),
                            states_by_side["dashboard_b"].get(key),
                        )
                    )
            self.partial_comparisons = {
                key: await task for key, task in comparison_tasks.items()
            }
        except BaseException:
            if not capture_task.done():
                capture_task.cancel()
            for task in comparison_tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                capture_task, *comparison_tasks.values(), return_exceptions=True
            )
            raise
        finally:
            comparison_pool.shutdown(wait=False, cancel_futures=True)

    def _write_report(self) -> None:
        if self.result is None:
            raise RuntimeError("Comparison must complete before report generation")
        _validate_comparison_output(self.result)
        source_names = self.result["kpis"]["source_names"]
        write_comparison(
            self.result,
            self.output_dir,
            source_names["a"],
            source_names["b"],
            job_id=self.job_id,
            operation_log_path=self.operation_log_path,
        )

    def finish(self, outcome: str, error_type: str | None = None) -> None:
        if self.finished:
            return
        self.finished = True
        write_operation_event(
            self.operation_log_path,
            job_id=self.job_id,
            operation="job",
            event="finished",
            outcome=outcome,
            duration_seconds=time.perf_counter() - self.started_at,
            error_type=error_type,
        )


class _ComparisonStepAgent(BaseAgent):
    step: Literal["capture", "validate", "compare", "report"]
    _workflow_run: ComparisonWorkflowRun = PrivateAttr()

    def __init__(
        self,
        *,
        name: str,
        step: Literal["capture", "validate", "compare", "report"],
        workflow_run: ComparisonWorkflowRun,
    ) -> None:
        super().__init__(
            name=name,
            description=f"Run the deterministic {step} stage of the dashboard workflow.",
            step=step,
        )
        self._workflow_run = workflow_run

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        await self._workflow_run.run_step(self.step)
        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            branch=ctx.branch,
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"{self.step} stage completed")],
            ),
            turn_complete=True,
        )


def _validate_captured_states(
    states_a: list[DashboardState], states_b: list[DashboardState]
) -> None:
    allowed_extraction_statuses = {
        "complete",
        "partial",
        "failed",
        "skipped_exported",
        "skipped_export_option",
    }
    for side, states in (("A", states_a), ("B", states_b)):
        if not isinstance(states, list):
            raise ValueError(f"Dashboard {side} capture must return a state list")
        for state in states:
            if not isinstance(state.extracted, dict):
                raise ValueError(f"Dashboard {side} state has invalid extracted data")
            capture_status = state.extracted.get("capture_status", "captured")
            if not isinstance(capture_status, str) or capture_status not in {
                "captured",
                "unverified",
            }:
                raise ValueError(
                    f"Dashboard {side} state has unsupported capture status "
                    f"{capture_status!r}"
                )
            if capture_status == "unverified":
                capture_error = state.extracted.get("capture_error")
                if not isinstance(capture_error, str) or not capture_error.strip():
                    raise ValueError(
                        f"Dashboard {side} unverified state has no capture reason"
                    )
            for collection_name, id_name in (
                ("elements", "element_id"),
                ("title_items", "title_id"),
            ):
                records = state.extracted.get(collection_name)
                if records is None:
                    continue
                if not isinstance(records, list):
                    raise ValueError(
                        f"Dashboard {side} {collection_name} must be an array"
                    )
                ids: set[str] = set()
                for record in records:
                    if not isinstance(record, dict):
                        raise ValueError(
                            f"Dashboard {side} {collection_name} contains an invalid record"
                        )
                    record_id = record.get(id_name)
                    if record_id is not None:
                        if not isinstance(record_id, str) or not record_id.strip():
                            raise ValueError(
                                f"Dashboard {side} {collection_name} has an invalid {id_name}"
                            )
                        if record_id in ids:
                            raise ValueError(
                                f"Dashboard {side} {collection_name} has duplicate ID {record_id}"
                            )
                        ids.add(record_id)
                    status = record.get("extraction_status")
                    if status is not None and (
                        not isinstance(status, str)
                        or status not in allowed_extraction_statuses
                    ):
                        raise ValueError(
                            f"Dashboard {side} {collection_name} has unsupported "
                            f"extraction status {status!r}"
                        )


async def run_dashboard_comparison(
    job_id: str,
    url_a: str,
    url_b: str,
    output_dir: Path,
    storage_state: Path,
) -> dict[str, Any]:
    workflow_run = ComparisonWorkflowRun(
        job_id=job_id,
        url_a=url_a,
        url_b=url_b,
        output_dir=output_dir,
        storage_state=storage_state,
    )
    workflow = SequentialAgent(
        name="dashboard_comparison_workflow",
        description=(
            "Capture, validate, align, and report a dashboard comparison in "
            "deterministic order."
        ),
        sub_agents=[
            _ComparisonStepAgent(
                name="capture_dashboards",
                step="capture",
                workflow_run=workflow_run,
            ),
            _ComparisonStepAgent(
                name="validate_extraction",
                step="validate",
                workflow_run=workflow_run,
            ),
            _ComparisonStepAgent(
                name="align_and_compare",
                step="compare",
                workflow_run=workflow_run,
            ),
            _ComparisonStepAgent(
                name="generate_comparison_report",
                step="report",
                workflow_run=workflow_run,
            ),
        ],
    )
    session_service = InMemorySessionService()
    session_id = str(uuid4())
    await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=session_id,
    )
    runner = Runner(
        agent=workflow,
        app_name=APP_NAME,
        session_service=session_service,
    )
    try:
        async for event in runner.run_async(
            user_id=USER_ID,
            session_id=session_id,
            new_message=types.Content(
                role="user",
                parts=[types.Part(text=f"Run dashboard comparison job {job_id}.")],
            ),
        ):
            if event.error_code:
                raise RuntimeError(
                    f"ADK workflow stage {event.author!r} failed: "
                    f"{event.error_code}: {event.error_message or ''}"
                )

        if workflow_run.result is None:
            raise RuntimeError(
                "ADK comparison workflow completed without a result"
            )
    except BaseException as exc:
        workflow_run.finish(
            "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
            type(exc).__name__,
        )
        raise
    workflow_run.finish("success")
    return workflow_run.result
