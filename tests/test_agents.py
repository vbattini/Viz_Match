import asyncio
import json

import aiohttp
import pytest
from google.auth.exceptions import TransportError

from powerbi_dashboard_comparator import agents
from powerbi_dashboard_comparator.agents import (
    DASHBOARD_COMPARISON_SKILL,
    VISION_INSTRUCTIONS,
    _align_shared_plan,
    _deterministic_filter_plan,
    _normalize_plan,
    _parse_json_response,
    _planning_inventory,
    _validate_plan,
    plan_from_dom,
)


def test_run_agent_retries_incomplete_model_response(monkeypatch) -> None:
    attempts = 0

    async def flaky_run(_agent, _content):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise aiohttp.ClientPayloadError(
                "Response payload is not completed"
            ) from ConnectionResetError("connection reset")
        return "model response"

    async def no_wait(_delay):
        return None

    monkeypatch.setattr(agents, "_run_agent_once", flaky_run)
    monkeypatch.setattr(agents.asyncio, "sleep", no_wait)

    assert asyncio.run(agents._run_agent(object(), object())) == "model response"
    assert attempts == 2


def test_run_agent_retries_google_auth_transport_failure(monkeypatch) -> None:
    attempts = 0

    async def failing_then_successful_run(_agent, _content):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TransportError("OAuth token endpoint is unreachable")
        return "model response"

    async def no_wait(_delay):
        return None

    monkeypatch.setattr(agents, "_run_agent_once", failing_then_successful_run)
    monkeypatch.setattr(agents.asyncio, "sleep", no_wait)

    assert asyncio.run(agents._run_agent(object(), object())) == "model response"
    assert attempts == 2


def test_run_agent_retries_repeated_transport_failures(monkeypatch) -> None:
    attempts = 0
    delays = []
    monkeypatch.setattr(agents, "MODEL_REQUEST_ATTEMPTS", 5)

    async def flaky_run(_agent, _content):
        nonlocal attempts
        attempts += 1
        if attempts < 4:
            raise TransportError("OAuth token endpoint is unreachable")
        return "model response"

    async def record_delay(delay):
        delays.append(delay)

    monkeypatch.setattr(agents, "_run_agent_once", flaky_run)
    monkeypatch.setattr(agents.asyncio, "sleep", record_delay)

    assert asyncio.run(agents._run_agent(object(), object())) == "model response"
    assert attempts == 4
    assert delays == [1, 2, 4]


def test_run_agent_surfaces_persistent_transport_failure(monkeypatch) -> None:
    attempts = 0
    monkeypatch.setattr(agents, "MODEL_REQUEST_ATTEMPTS", 2)

    async def failing_run(_agent, _content):
        nonlocal attempts
        attempts += 1
        raise aiohttp.ClientPayloadError("incomplete response")

    async def no_wait(_delay):
        return None

    monkeypatch.setattr(agents, "_run_agent_once", failing_run)
    monkeypatch.setattr(agents.asyncio, "sleep", no_wait)

    with pytest.raises(RuntimeError, match="failed after 2 attempts"):
        asyncio.run(agents._run_agent(object(), object()))
    assert attempts == 2


def test_run_agent_times_out_and_retries_once(monkeypatch) -> None:
    attempts = 0
    monkeypatch.setattr(agents, "MODEL_REQUEST_ATTEMPTS", 2)
    monkeypatch.setattr(agents, "MODEL_REQUEST_TIMEOUT_SECONDS", 0.01)

    async def slow_run(_agent, _content):
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(1)

    monkeypatch.setattr(agents, "_run_agent_once", slow_run)

    with pytest.raises(
        RuntimeError, match="failed after 2 attempts due to request timeout"
    ):
        asyncio.run(agents._run_agent(object(), object()))
    assert attempts == 2


def test_run_agent_batches_requests_with_bounded_concurrency(monkeypatch) -> None:
    active_requests = 0
    maximum_active_requests = 0
    monkeypatch.setattr(agents, "MAX_PARALLEL_MODEL_REQUESTS", 2)

    async def track_request(_agent, content):
        nonlocal active_requests, maximum_active_requests
        active_requests += 1
        maximum_active_requests = max(
            maximum_active_requests, active_requests
        )
        await asyncio.sleep(0.01)
        active_requests -= 1
        return content

    monkeypatch.setattr(agents, "_run_agent_once", track_request)

    async def run_batch():
        return await asyncio.gather(
            *(
                agents._run_agent(object(), f"request-{index}")
                for index in range(5)
            )
        )

    results = asyncio.run(run_batch())

    assert results == [f"request-{index}" for index in range(5)]
    assert maximum_active_requests == 2


def test_run_agent_does_not_retry_non_network_error(monkeypatch) -> None:
    attempts = 0

    async def failing_run(_agent, _content):
        nonlocal attempts
        attempts += 1
        raise ValueError("invalid model response")

    monkeypatch.setattr(agents, "_run_agent_once", failing_run)

    with pytest.raises(ValueError, match="invalid model response"):
        asyncio.run(agents._run_agent(object(), object()))
    assert attempts == 1


def test_parse_json_response_accepts_markdown_json_fence() -> None:
    assert _parse_json_response('```json\n{"filters":[]}\n```', "planner") == {
        "filters": []
    }


def test_screenshot_extraction_retries_invalid_json(monkeypatch) -> None:
    responses = iter(['{"schema_version":"1.0",', "{}"])
    prompts = []

    async def run_agent(_agent, content):
        prompts.append(content.parts[0].text)
        return next(responses)

    monkeypatch.setattr(agents, "LlmAgent", lambda **_kwargs: object())
    monkeypatch.setattr(agents, "_run_agent", run_agent)

    extracted = asyncio.run(
        agents.extract_screenshots([b"image"], "Overview", ["Sales trend"])
    )

    assert extracted["page"] == "Overview"
    assert len(prompts) == 2
    assert all("Sales trend" in prompt for prompt in prompts)
    assert all("skipped_exported" in prompt for prompt in prompts)
    assert all("solid white mask" in prompt for prompt in prompts)
    assert all("skipped_export_option" in prompt for prompt in prompts)
    assert "previous response was truncated or invalid" in prompts[1]


def test_screenshot_extraction_retries_max_tokens_response(monkeypatch) -> None:
    attempts = 0
    prompts = []
    agent_configs = []

    async def run_agent(_agent, content):
        nonlocal attempts
        attempts += 1
        prompts.append(content.parts[0].text)
        if attempts == 1:
            raise agents.TruncatedAgentResponseError(
                "ADK agent response was truncated after reaching MAX_TOKENS"
            )
        return "{}"

    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setattr(
        agents,
        "LlmAgent",
        lambda **kwargs: agent_configs.append(kwargs) or object(),
    )
    monkeypatch.setattr(agents, "_run_agent", run_agent)

    extracted = asyncio.run(agents.extract_screenshots([b"image"], "Overview"))

    assert attempts == 2
    assert extracted["page"] == "Overview"
    assert "previous response was truncated or invalid" in prompts[1]
    assert (
        agent_configs[0]["generate_content_config"].max_output_tokens
        == 65_536
    )


def test_screenshot_extraction_surfaces_invalid_json_after_retry(monkeypatch) -> None:
    attempts = 0

    async def run_agent(_agent, _content):
        nonlocal attempts
        attempts += 1
        return '{"schema_version":"1.0",'

    monkeypatch.setattr(agents, "LlmAgent", lambda **_kwargs: object())
    monkeypatch.setattr(agents, "_run_agent", run_agent)

    with pytest.raises(ValueError, match="screenshot extractor retry agent returned invalid JSON"):
        asyncio.run(agents.extract_screenshots([b"image"], "Overview"))
    assert attempts == 2


def test_visual_type_detection_returns_screenshot_classifications(monkeypatch) -> None:
    agent_configs = []
    requests = []
    response = {
        "visuals": [
            {
                "visual_id": "TABLE_001",
                "visual_name": "Open Orders",
                "visual_type": "Table",
                "rendering_status": "Rendered",
                "confidence": "High",
                "observation": "A titled grid with visible column headers.",
                "bounds": {"x": 20, "y": 30, "width": 300, "height": 200},
            }
        ]
    }

    async def run_agent(agent, content, **_kwargs):
        requests.append((agent, content))
        return json.dumps(response)

    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")
    monkeypatch.setattr(
        agents,
        "LlmAgent",
        lambda **kwargs: agent_configs.append(kwargs) or object(),
    )
    monkeypatch.setattr(agents, "_run_agent", run_agent)

    detected = asyncio.run(
        agents.detect_visual_types_from_screenshot(
            b"image-bytes",
            "Open Orders",
            [
                {
                    "visual_id": "TABLE_001",
                    "visual_name": None,
                    "x": 10,
                    "y": 20,
                    "width": 300,
                    "height": 200,
                }
            ],
        )
    )

    assert detected == response["visuals"]
    assert agent_configs[0]["output_schema"] is agents.VisualTypeDetectionResult
    assert agent_configs[0]["generate_content_config"].temperature is None
    assert len(requests) == 1
    assert "Do not transcribe KPI values" in agents.VISUAL_TYPE_DETECTION_INSTRUCTIONS
    assert "TABLE_001" in requests[0][1].parts[0].text


def test_visual_type_detection_continues_when_candidate_ids_are_missing(
    monkeypatch,
) -> None:
    async def run_agent(_agent, _content, **_kwargs):
        return json.dumps({"visuals": []})

    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")
    monkeypatch.setattr(agents, "LlmAgent", lambda **_kwargs: object())
    monkeypatch.setattr(agents, "_run_agent", run_agent)

    detected = asyncio.run(
        agents.detect_visual_types_from_screenshot(
            b"image-bytes",
            "Open Orders",
            [
                {
                    "visual_id": "TABLE_001",
                    "x": 10,
                    "y": 20,
                    "width": 300,
                    "height": 200,
                }
            ],
        )
    )

    assert detected == []


def test_visual_type_detection_accepts_screenshot_only_visuals(monkeypatch) -> None:
    response = {
        "visuals": [
            {
                "visual_id": "TABLE_001",
                "visual_name": "Open Orders",
                "visual_type": "Table",
                "rendering_status": "Rendered",
                "confidence": "High",
                "observation": "A titled grid is visible.",
                "bounds": {"x": 100, "y": 100, "width": 300, "height": 250},
            },
            {
                "visual_id": "SCREENSHOT_001",
                "visual_name": "Orders by Facility",
                "visual_type": "Bar Chart",
                "rendering_status": "Rendered",
                "confidence": "High",
                "observation": "Horizontal bars and a title are visible.",
                "bounds": {"x": 500, "y": 400, "width": 300, "height": 250},
            },
        ]
    }

    async def run_agent(_agent, _content, **_kwargs):
        return json.dumps(response)

    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")
    monkeypatch.setattr(agents, "LlmAgent", lambda **_kwargs: object())
    monkeypatch.setattr(agents, "_run_agent", run_agent)

    detected = asyncio.run(
        agents.detect_visual_types_from_screenshot(
            b"image-bytes",
            "Open Orders",
            [
                {
                    "visual_id": "TABLE_001",
                    "x": 20,
                    "y": 30,
                    "width": 300,
                    "height": 200,
                }
            ],
        )
    )

    assert [item["visual_id"] for item in detected] == [
        "TABLE_001",
        "SCREENSHOT_001",
    ]


def test_visual_type_detection_runs_without_dom_candidates(monkeypatch) -> None:
    response = {
        "visuals": [
            {
                "visual_id": "SCREENSHOT_001",
                "visual_name": "Orders by Facility",
                "visual_type": "Bar Chart",
                "rendering_status": "Rendered",
                "confidence": "High",
                "observation": "Horizontal bars are visible.",
                "bounds": {"x": 100, "y": 200, "width": 500, "height": 300},
            }
        ]
    }

    async def run_agent(_agent, _content, **_kwargs):
        return json.dumps(response)

    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")
    monkeypatch.setattr(agents, "LlmAgent", lambda **_kwargs: object())
    monkeypatch.setattr(agents, "_run_agent", run_agent)

    detected = asyncio.run(
        agents.detect_visual_types_from_screenshot(
            b"image-bytes", "Orders", []
        )
    )

    assert detected[0]["visual_name"] == "Orders by Facility"


def test_visual_type_detection_skips_screenshot_visual_without_bounds(
    monkeypatch,
) -> None:
    async def run_agent(_agent, _content, **_kwargs):
        return json.dumps(
            {
                "visuals": [
                    {
                        "visual_id": "TABLE_001",
                        "visual_name": "Open Orders",
                        "visual_type": "Table",
                        "rendering_status": "Rendered",
                        "confidence": "High",
                        "observation": "A titled grid is visible.",
                        "bounds": {
                            "x": 20,
                            "y": 30,
                            "width": 300,
                            "height": 200,
                        },
                    },
                    {
                        "visual_id": "SCREENSHOT_001",
                        "visual_name": "Orders by Facility",
                        "visual_type": "Bar Chart",
                        "rendering_status": "Rendered",
                        "confidence": "High",
                        "observation": "Horizontal bars are visible.",
                        "bounds": {},
                    },
                ]
            }
        )

    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")
    monkeypatch.setattr(agents, "LlmAgent", lambda **_kwargs: object())
    monkeypatch.setattr(agents, "_run_agent", run_agent)

    detected = asyncio.run(
        agents.detect_visual_types_from_screenshot(
            b"image-bytes",
            "Open Orders",
            [
                {
                    "visual_id": "TABLE_001",
                    "x": 20,
                    "y": 30,
                    "width": 300,
                    "height": 200,
                }
            ],
        )
    )

    assert [item["visual_id"] for item in detected] == ["TABLE_001"]


def test_visual_type_detection_keeps_candidate_with_unusable_bounds(
    monkeypatch,
) -> None:
    response = {
        "visuals": [
            {
                "visual_id": "TABLE_001",
                "visual_name": "Open Orders",
                "visual_type": "Table",
                "rendering_status": "Rendered",
                "confidence": "High",
                "observation": "A titled grid is visible.",
                "bounds": {"x": 1010, "y": 30, "width": 300, "height": 200},
            }
        ]
    }

    async def run_agent(_agent, _content, **_kwargs):
        return json.dumps(response)

    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")
    monkeypatch.setattr(agents, "LlmAgent", lambda **_kwargs: object())
    monkeypatch.setattr(agents, "_run_agent", run_agent)

    detected = asyncio.run(
        agents.detect_visual_types_from_screenshot(
            b"image-bytes",
            "Open Orders",
            [
                {
                    "visual_id": "TABLE_001",
                    "x": 20,
                    "y": 30,
                    "width": 300,
                    "height": 200,
                }
            ],
        )
    )

    assert detected[0]["visual_type"] == "Table"
    assert detected[0]["bounds"] == {
        "x": None,
        "y": None,
        "width": None,
        "height": None,
    }


def test_filter_plan_rejects_values_not_in_dom() -> None:
    inventory = {"filters": [{"name": "Year", "values": ["2025", "2026"]}]}

    with pytest.raises(ValueError, match="invented a value"):
        _validate_plan(
            {"filters": [{"name": "Year", "values": ["2027"]}]},
            inventory,
        )


def test_filter_plan_normalizes_duplicate_entries_without_cap() -> None:
    inventory = {
        "filters": [{"name": "Facility", "values": ["North", "South", "West"]}]
    }

    plan = _normalize_plan(
        {
            "filters": [
                {"name": "Facility", "values": ["North"]},
                {"name": "Facility", "values": ["South", "West", "North"]},
            ]
        },
        inventory,
    )

    assert plan == {
        "filters": [{"name": "Facility", "values": ["North", "South", "West"]}]
    }


def test_shared_plan_includes_every_filter_and_value_from_second_dashboard() -> None:
    shared_plan = {
        "filters": [
            {"name": "Facility", "values": ["North", "South"]},
            {"name": "Supervisor", "values": ["Alex"]},
        ]
    }
    second_inventory = {
        "filters": [
            {"name": "facility", "values": ["North", "East"]},
            {"name": "Region", "values": ["West", "Central"]},
        ]
    }

    plan = _align_shared_plan(shared_plan, second_inventory)

    assert plan == {
        "filters": [
            {"name": "Facility", "values": ["North", "East"]},
            {"name": "Region", "values": ["West", "Central"]},
        ]
    }


def test_shared_filter_plan_preserves_canonical_order_for_combinations() -> None:
    shared_plan = {
        "filters": [
            {"name": "Filter 1", "values": ["A1", "A2"]},
            {"name": "Filter 2", "values": ["B1", "B2"]},
        ]
    }
    second_inventory = {
        "filters": [
            {"name": "Filter 2", "values": ["B1", "B2"]},
            {"name": "Filter 1", "values": ["A1", "A2"]},
        ]
    }

    assert _align_shared_plan(shared_plan, second_inventory) == {
        "filters": [
            {"name": "Filter 1", "values": ["A1", "A2"]},
            {"name": "Filter 2", "values": ["B1", "B2"]},
        ]
    }


def test_shared_aggregate_plan_expands_to_individual_values() -> None:
    shared_plan = {"filters": [{"name": "Status", "values": ["Select all"]}]}
    second_inventory = {
        "filters": [
            {
                "name": "Status",
                "selected": ["Open"],
                "values": ["(All)", "Open", "Closed"],
            }
        ]
    }

    assert _align_shared_plan(shared_plan, second_inventory) == {
        "filters": [{"name": "Status", "values": ["Open", "Closed"]}]
    }


def test_shared_plan_expands_aggregate_when_second_dashboard_has_none() -> None:
    shared_plan = {"filters": [{"name": "Status", "values": ["Select all"]}]}
    second_inventory = {
        "filters": [
            {
                "name": "Status",
                "selected": ["Open", "Closed", "Pending"],
                "values": ["Open", "Closed", "Pending"],
            }
        ]
    }

    assert _align_shared_plan(shared_plan, second_inventory) == {
        "filters": [{"name": "Status", "values": ["Open", "Closed", "Pending"]}]
    }


def test_filter_planning_skips_model_when_no_supported_filters(monkeypatch) -> None:
    async def unexpected_agent_call(*args, **kwargs):
        raise AssertionError("No model call is needed without supported filters")

    monkeypatch.setattr(
        "powerbi_dashboard_comparator.agents._run_agent", unexpected_agent_call
    )
    inventory = {"pages": ["Excess Summary"], "filters": []}

    analysis, plan = asyncio.run(plan_from_dom(inventory))

    assert analysis == {"pages": ["Excess Summary"], "filters": []}
    assert plan == {"filters": []}


def test_filter_plan_uses_only_deterministic_dom_values(monkeypatch) -> None:
    async def unexpected_agent_call(*args, **kwargs):
        raise AssertionError("Filter plans must not be generated by the model")

    monkeypatch.setattr(
        "powerbi_dashboard_comparator.agents._run_agent", unexpected_agent_call
    )
    inventory = {
        "pages": ["Overview"],
        "filters": [
            {
                "name": "Time Period",
                "selected": ["Current Year"],
                "values": ["Select all", "Current Year", "Benefits", "Prior Year"],
            }
        ],
    }

    analysis, plan = asyncio.run(plan_from_dom(inventory))

    assert analysis == {
        "pages": ["Overview"],
        "filters": inventory["filters"],
    }
    assert plan == {
        "filters": [
            {
                "name": "Time Period",
                "values": ["Current Year", "Benefits", "Prior Year"],
            }
        ]
    }


def test_filter_plan_never_clicks_aggregate_option() -> None:
    inventory = {
        "filters": [
            {
                "name": "Time Period",
                "values": ["All", "(All)", "Select all", "Benefits", "Finance"],
            }
        ]
    }

    assert _deterministic_filter_plan(inventory) == {
        "filters": [
            {"name": "Time Period", "values": ["Benefits", "Finance"]}
        ]
    }


def test_radio_filter_tests_every_option_including_all() -> None:
    inventory = {
        "filters": [
            {
                "name": "Period",
                "control_type": "radio",
                "selected": ["All"],
                "values": ["All", "Current", "Previous"],
            }
        ]
    }

    assert _deterministic_filter_plan(inventory) == {
        "filters": [
            {
                "name": "Period",
                "values": ["All", "Current", "Previous"],
            }
        ]
    }


def test_radio_filter_rejects_clear_selection_option() -> None:
    inventory = {
        "filters": [
            {
                "name": "Period",
                "control_type": "radio",
                "selected": ["All"],
                "values": ["All", "Current", "Clear Selection"],
            }
        ]
    }

    with pytest.raises(ValueError, match="must not expose a Clear Selection"):
        _deterministic_filter_plan(inventory)


def test_checkbox_filter_without_clear_option_tests_each_value() -> None:
    inventory = {
        "filters": [
            {
                "name": "Service Team",
                "control_type": "checkbox",
                "selected": ["All"],
                "values": ["Benefits", "HRIS"],
            }
        ]
    }

    assert _planning_inventory(inventory)["filters"] == inventory["filters"]
    assert asyncio.run(plan_from_dom(inventory))[1] == {
        "filters": [{"name": "Service Team", "values": ["Benefits", "HRIS"]}]
    }


def test_filter_with_clear_selection_strategy_still_tests_every_value() -> None:
    filter_item = {
        "name": "Service Team",
        "control_type": "radio_dropdown",
        "selected": ["All"],
        "values": ["All", "Benefits", "HRIS"],
        "reset_strategy": "clear_selection",
    }
    inventory = {"filters": [filter_item]}

    assert _planning_inventory(inventory)["filters"] == [filter_item]
    assert _deterministic_filter_plan(inventory) == {
        "filters": [
            {"name": "Service Team", "values": ["All", "Benefits", "HRIS"]}
        ]
    }


def test_radio_dropdown_is_not_treated_as_a_clearable_aggregate_filter() -> None:
    inventory = {
        "filters": [
            {
                "name": "Relative Time",
                "control_type": "radio_dropdown",
                "selected": ["All"],
                "values": ["All", "Last 4 Weeks", "Other Weeks"],
            }
        ]
    }

    assert _deterministic_filter_plan(inventory) == {
        "filters": [
            {
                "name": "Relative Time",
                "values": ["All", "Last 4 Weeks", "Other Weeks"],
            }
        ]
    }


def test_filter_plan_clicks_every_value_when_no_aggregate_offered() -> None:
    inventory = {
        "filters": [
            {
                "name": "Facility",
                "values": ["North", "South", "East", "West", "Central"],
            }
        ]
    }

    assert _deterministic_filter_plan(inventory) == {
        "filters": [
            {
                "name": "Facility",
                "values": ["North", "South", "East", "West", "Central"],
            }
        ]
    }


def test_filter_plan_checks_all_values_when_all_selected_without_aggregate() -> None:
    filter_item = {
        "name": "Service Team",
        "selected": ["Benefits", "HRIS", "Learning"],
        "values": ["Benefits", "HRIS", "Learning"],
    }
    inventory = {"filters": [filter_item]}

    assert _planning_inventory(inventory)["filters"] == [filter_item]
    assert _deterministic_filter_plan(inventory) == {
        "filters": [
            {
                "name": "Service Team",
                "values": ["Benefits", "HRIS", "Learning"],
            }
        ]
    }


def test_filter_plan_tests_values_when_default_all_has_no_aggregate_option() -> None:
    filter_item = {
        "name": "Service Team",
        "selected": ["All"],
        "values": ["Benefits", "HRIS", "Learning"],
    }
    inventory = {"filters": [filter_item]}

    assert _planning_inventory(inventory)["filters"] == [filter_item]
    assert _deterministic_filter_plan(inventory) == {
        "filters": [
            {
                "name": "Service Team",
                "values": ["Benefits", "HRIS", "Learning"],
            }
        ]
    }


def test_filter_plan_tests_values_when_no_clear_or_aggregate_option() -> None:
    filter_item = {
        "name": "Service Team",
        "selected": ["All"],
        "values": ["Benefits", "HRIS", "Learning"],
        "reset_strategy": "test_individually",
    }
    inventory = {"filters": [filter_item]}

    assert _planning_inventory(inventory)["filters"] == [filter_item]
    assert _deterministic_filter_plan(inventory) == {
        "filters": [
            {
                "name": "Service Team",
                "values": ["Benefits", "HRIS", "Learning"],
            }
        ]
    }


def test_filter_plan_tests_individual_values_when_all_is_selected() -> None:
    inventory = {
        "filters": [
            {
                "name": "Time Period",
                "selected": ["All"],
                "values": ["All", "Benefits", "Finance", "Ops"],
            }
        ]
    }

    assert _deterministic_filter_plan(inventory) == {
        "filters": [
            {
                "name": "Time Period",
                "values": ["Benefits", "Finance", "Ops"],
            }
        ]
    }


def test_filter_plan_rechecks_every_individual_value_when_all_are_selected() -> None:
    inventory = {
        "filters": [
            {
                "name": "Case Type",
                "selected": ["Phone Call", "Email"],
                "values": ["Select all", "Phone Call", "Email"],
            }
        ]
    }

    assert _deterministic_filter_plan(inventory) == {
        "filters": [
            {"name": "Case Type", "values": ["Phone Call", "Email"]}
        ]
    }


def test_filter_plan_includes_options_already_selected() -> None:
    inventory = {
        "filters": [
            {
                "name": "Relative Time",
                "selected": ["Last 4 Weeks"],
                "values": ["Last 4 Weeks", "Other Weeks"],
            }
        ]
    }

    assert _deterministic_filter_plan(inventory) == {
        "filters": [
            {
                "name": "Relative Time",
                "values": ["Last 4 Weeks", "Other Weeks"],
            }
        ]
    }


def test_filter_plan_exercises_each_bookmark_button_state() -> None:
    inventory = {
        "filters": [
            {
                "name": "Bookmark buttons: E-Mail | Phone Call | Workday Cases",
                "control_type": "bookmark_buttons",
                "selected": ["Workday Cases"],
                "values": ["E-Mail", "Phone Call", "Workday Cases"],
            }
        ]
    }

    assert _deterministic_filter_plan(inventory) == {
        "filters": [
            {
                "name": "Bookmark buttons: E-Mail | Phone Call | Workday Cases",
                "values": ["E-Mail", "Phone Call", "Workday Cases"],
            }
        ]
    }


def test_planning_keeps_all_selected_category_and_other_filters() -> None:
    inventory = {
        "pages": ["Excess by Item"],
        "filters": [
            {
                "name": "Category",
                "selected": ["All"],
                "values": ["Select all", "CANDY", "FRESH"],
            },
            {
                "name": "Week Ending",
                "selected": ["3/14/2026"],
                "values": ["3/14/2026", "3/21/2026"],
            },
        ],
        "controls": [{"name": "Category"}],
    }

    assert _planning_inventory(inventory) == {
        "pages": ["Excess by Item"],
        "filters": inventory["filters"],
    }


def test_filter_planning_is_deterministic_when_category_is_all_selected(
    monkeypatch,
) -> None:
    async def unexpected_agent_call(*args, **kwargs):
        raise AssertionError("Category is already at its default all selection")

    monkeypatch.setattr(
        "powerbi_dashboard_comparator.agents._run_agent", unexpected_agent_call
    )
    category = {
        "name": "Category",
        "selected": ["All"],
        "values": ["Select all", "CANDY", "FRESH"],
    }
    inventory = {"pages": ["Excess Summary"], "filters": [category]}

    analysis, plan = asyncio.run(plan_from_dom(inventory))

    assert analysis["filters"] == [category]
    assert plan == {
        "filters": [{"name": "Category", "values": ["CANDY", "FRESH"]}]
    }


def test_screenshot_extractor_uses_comparison_skill_and_zero_temperature(
    monkeypatch,
) -> None:
    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    async def fake_run_agent(_agent, _content):
        return (
            '{"schema_version":"1.0","dashboard_metadata":{},'
            '"elements":[],"extraction_summary":{}}'
        )

    monkeypatch.setattr(agents, "LlmAgent", FakeAgent)
    monkeypatch.setattr(agents, "_model_name", lambda: "test-model")
    monkeypatch.setattr(agents, "_run_agent", fake_run_agent)

    result = asyncio.run(agents.extract_screenshots([b"image"], "Overview"))

    assert result["page"] == "Overview"
    assert result["dashboard_metadata"]["page_name"] == "Overview"
    assert result["extraction_summary"]["extracted_element_count"] == 0
    assert captured["generate_content_config"].temperature == 0
    assert DASHBOARD_COMPARISON_SKILL in VISION_INSTRUCTIONS
    assert "Do not infer values that are not legible" in " ".join(
        VISION_INSTRUCTIONS.split()
    )
    assert "dashboard_metadata.report_title" in VISION_INSTRUCTIONS
    assert "dashboard-authored title inside the" in VISION_INSTRUCTIONS
    assert "Last refresh" in " ".join(VISION_INSTRUCTIONS.split())
    assert "Treat elements and title_items as complementary inventories" in VISION_INSTRUCTIONS
    assert "Use role categories consistently" in VISION_INSTRUCTIONS
    assert "Never change a role merely to make Dashboard A and Dashboard B" in (
        " ".join(VISION_INSTRUCTIONS.split())
    )
    assert "do not join neighboring lines into one text item" in " ".join(
        VISION_INSTRUCTIONS.split()
    )


def test_screenshot_extraction_preserves_table_cells_and_summary() -> None:
    extracted = {
        "schema_version": "1.0",
        "dashboard_metadata": {
            "report_title": "Service Summary",
            "page_name": "Summary",
            "refresh_timestamp": "10/10/2026 4:00 PM",
        },
        "elements": [
            {
                "element_id": "TABLE_001",
                "element_type": "table",
                "title": "Cases by Week",
                "bounding_box": {
                    "x": 1,
                    "y": 2,
                    "width": 300,
                    "height": 150,
                },
                "content": {
                    "columns": ["Week", "Cases", "SLA %"],
                    "rows": [
                        {
                            "Week": {"raw": "10/3/2026", "status": "read"},
                            "Cases": {"raw": "0", "status": "read"},
                            "SLA %": {
                                "raw": None,
                                "status": "unreadable",
                                "reason": "small text",
                            },
                        }
                    ],
                },
                "extraction_status": "partial",
                "uncertainties": ["SLA percentage unreadable"],
            }
        ],
        "extraction_summary": {},
    }

    result = agents._prepare_screenshot_extraction(extracted, [b"image"], "Summary")

    table = result["elements"][0]
    assert table["element_id"] == "TABLE_001"
    assert table["rows"] == [["10/3/2026", "0", None]]
    assert table["categories"] == ["Week", "Cases", "SLA %"]
    assert table["bounding_box"]["width"] == 300
    assert result["extraction_summary"]["partial_element_count"] == 1
    assert result["extraction_summary"]["complete_element_count"] == 2
    assert result["extraction_summary"]["extracted_element_count"] == 3
    assert result["extraction_summary"]["visible_element_count"] == 3
    assert result["extraction_summary"]["unreadable_item_count"] == 1
    assert result["extraction_summary"]["requires_additional_screenshots"] is True
    assert result["elements"][1]["title"] == "Report title"
    assert result["elements"][2]["title"] == "Last refresh"


def test_missing_extraction_status_defaults_to_partial() -> None:
    element = agents.DashboardElement.model_validate(
        {
            "element_id": "TEXT_001",
            "element_type": "text",
            "title": "Visible label",
        }
    )

    assert element.extraction_status == "partial"


def test_truncated_model_finish_reason_is_not_treated_as_complete_text() -> None:
    class Event:
        finish_reason = "MAX_TOKENS"
        content = type("Content", (), {"parts": []})()

        @staticmethod
        def is_final_response():
            return True

    with pytest.raises(agents.TruncatedAgentResponseError, match="MAX_TOKENS"):
        agents._text_from_event(Event())


def test_reported_extraction_count_gap_is_preserved_as_diagnostic() -> None:
    result = agents._prepare_screenshot_extraction(
        {
            "elements": [
                {
                    "element_id": "TEXT_001",
                    "element_type": "text",
                    "title": "Visible label",
                    "extraction_status": "complete",
                }
            ],
            "extraction_summary": {
                "visible_element_count": 3,
                "extracted_element_count": 3,
            },
        },
        [b"image"],
        "Summary",
    )

    summary = result["extraction_summary"]
    assert summary["visible_element_count"] == 1
    assert summary["reported_visible_element_count"] == 3
    assert summary["reported_extracted_element_count"] == 3
    assert summary["extraction_gap_count"] == 2
    assert summary["requires_additional_screenshots"] is True
    assert any("may be incomplete" in note for note in summary["notes"])


def test_screenshot_extraction_orders_elements_by_position() -> None:
    extracted = {
        "dashboard_metadata": {},
        "elements": [
            {
                "element_type": "text",
                "title": "",
                "bounding_box": {"x": 200, "y": 20, "width": 100, "height": 10},
                "content": {"text": {"raw": "right", "status": "read"}},
            },
            {
                "element_type": "text",
                "title": "",
                "bounding_box": {"x": 10, "y": 10, "width": 100, "height": 10},
                "content": {"text": {"raw": "top", "status": "read"}},
            },
            {
                "element_type": "text",
                "title": "",
                "bounding_box": {"x": 10, "y": 20, "width": 100, "height": 10},
                "content": {"text": {"raw": "left", "status": "read"}},
            },
        ],
        "extraction_summary": {},
    }

    result = agents._prepare_screenshot_extraction(extracted, [b"image"], "Summary")

    assert [item["value"] for item in result["elements"]] == ["top", "left", "right"]
    assert [item["reading_order"] for item in result["elements"]] == [1, 2, 3]
    assert [item["element_id"] for item in result["elements"]] == [
        "TEXT_001",
        "TEXT_002",
        "TEXT_003",
    ]


def test_screenshot_extraction_orders_title_rows_and_keeps_repeated_labels() -> None:
    extracted = {
        "dashboard_metadata": {},
        "elements": [],
        "title_items": [
            {
                "text": "Right title",
                "category": "chart_title",
                "associated_visual": "Chart B",
                "reading_order": 1,
                "bounding_box": {"x": 300, "y": 110, "width": 100, "height": 40},
            },
            {
                "text": "Repeated",
                "category": "legend_item",
                "associated_visual": "Chart A",
                "reading_order": 2,
                "bounding_box": {"x": 20, "y": 100, "width": 100, "height": 40},
            },
            {
                "text": "Repeated",
                "category": "legend_item",
                "associated_visual": "Chart A",
                "reading_order": 3,
                "bounding_box": {"x": 160, "y": 101, "width": 100, "height": 40},
            },
            {
                "text": "Next row",
                "category": "section_title",
                "associated_visual": None,
                "reading_order": 4,
                "bounding_box": {"x": 10, "y": 160, "width": 100, "height": 40},
            },
        ],
        "extraction_summary": {},
    }

    result = agents._prepare_screenshot_extraction(extracted, [b"image"], "Summary")

    assert [item["text"] for item in result["title_items"]] == [
        "Repeated",
        "Repeated",
        "Right title",
        "Next row",
    ]
    assert [item["reading_order"] for item in result["title_items"]] == [1, 2, 3, 4]
    assert {
        item["standard_element_name"] for item in result["title_items"]
    } == {"chart_title", "legend_item", "section_title"}


def test_title_items_and_element_content_complement_without_losing_repeats() -> None:
    extracted = {
        "dashboard_metadata": {},
        "elements": [
            {
                "element_type": "chart",
                "title": "Shared title",
                "reading_order": 1,
                "bounding_box": {"x": 100, "y": 50, "width": 200, "height": 30},
                "content": {
                    "series": [{"name": "Same label"}, {"name": "Same label"}],
                    "legend": ["Additional label"],
                },
            }
        ],
        "title_items": [
            {
                "text": "Shared title",
                "category": "chart_title",
                "associated_visual": "Shared title",
                "reading_order": 1,
                "bounding_box": {"x": 100, "y": 50, "width": 200, "height": 30},
            },
            {
                "text": "Same label",
                "category": "legend_item",
                "associated_visual": "Shared title",
                "reading_order": 2,
            },
        ],
        "extraction_summary": {},
    }

    result = agents._prepare_screenshot_extraction(extracted, [b"image"], "Summary")
    titles = result["title_items"]
    same_labels = [
        item for item in titles if item["text"] == "Same label"
    ]

    assert [item["text"] for item in titles].count("Shared title") == 1
    assert len(same_labels) == 2
    assert [item["text"] for item in titles].count("Additional label") == 1
    assert any(
        item.get("evidence_sources") == ["title_items", "elements"]
        for item in titles
        if item["text"] == "Shared title"
    )
    assert sorted(
        tuple(item.get("evidence_sources", [])) for item in same_labels
    ) == [("elements",), ("title_items", "elements")]


def test_conflicting_title_text_is_retained_and_flagged() -> None:
    extracted = {
        "dashboard_metadata": {},
        "elements": [
            {
                "element_type": "chart",
                "title": "Salse Trend",
                "associated_visual": "Sales visual",
                "reading_order": 1,
                "bounding_box": {"x": 100, "y": 50, "width": 200, "height": 30},
            }
        ],
        "title_items": [
            {
                "text": "Sales Trend",
                "category": "chart_title",
                "associated_visual": "Sales visual",
                "reading_order": 1,
                "bounding_box": {"x": 100, "y": 50, "width": 200, "height": 30},
            }
        ],
        "extraction_summary": {},
    }

    result = agents._prepare_screenshot_extraction(extracted, [b"image"], "Summary")

    assert {item["text"] for item in result["title_items"]} == {
        "Sales Trend",
        "Salse Trend",
    }
    assert all(
        item["reconciliation_status"] == "conflicting_readings"
        for item in result["title_items"]
    )
    assert result["extraction_summary"]["title_conflict_count"] == 2
    assert any(
        "Conflicting visible text" in note
        for note in result["extraction_summary"]["notes"]
    )


def test_title_order_uses_source_positions_when_some_bounds_are_missing() -> None:
    extracted = {
        "dashboard_metadata": {},
        "elements": [],
        "title_items": [
            {
                "text": "Second",
                "category": "chart_title",
                "reading_order": 2,
            },
            {
                "text": "First",
                "category": "chart_title",
                "reading_order": 1,
                "bounding_box": {"x": 500, "y": 100, "width": 100, "height": 20},
            },
        ],
        "extraction_summary": {},
    }

    result = agents._prepare_screenshot_extraction(extracted, [b"image"], "Summary")

    assert [item["text"] for item in result["title_items"]] == ["First", "Second"]
    assert [item["reading_order"] for item in result["title_items"]] == [1, 2]


def test_dashboard_metadata_is_not_synthesized_as_visible_title_text() -> None:
    extracted = {
        "dashboard_metadata": {
            "report_title": "Service Summary",
            "refresh_timestamp": "10/10/2026 4:00 PM",
        },
        "elements": [],
        "title_items": [],
        "extraction_summary": {},
    }

    result = agents._prepare_screenshot_extraction(extracted, [b"image"], "Summary")

    assert result["dashboard_metadata"]["report_title"] == "Service Summary"
    assert result["dashboard_metadata"]["refresh_timestamp"] == "10/10/2026 4:00 PM"
    assert result["title_items"] == []


def test_screenshot_extraction_normalizes_title_coordinates() -> None:
    extracted = {
        "dashboard_metadata": {"image_width": 1000, "image_height": 500},
        "elements": [],
        "title_items": [
            {
                "title_id": "TITLE_001",
                "standard_element_name": "chart_title",
                "element_type": "chart_title",
                "element_text": "Revenue",
                "text": "Revenue",
                "category": "chart_title",
                "associated_visual": "Revenue chart",
                "bounding_box": {"x": 250, "y": 100, "width": 200, "height": 30},
            }
        ],
        "extraction_summary": {},
    }

    result = agents._prepare_screenshot_extraction(extracted, [b"image"], "Summary")

    assert result["title_items"] == [
        {
            "title_id": "TITLE_001",
            "standard_element_name": "chart_title",
            "element_type": "chart_title",
            "element_text": "Revenue",
            "text": "Revenue",
            "category": "chart_title",
            "associated_visual": "Revenue chart",
            "reading_order": 1,
            "bounding_box": {"x": 250, "y": 100, "width": 200, "height": 30},
            "normalized_bounding_box": {
                "x": 0.25,
                "y": 0.2,
                "width": 0.2,
                "height": 0.06,
            },
        }
    ]


def test_screenshot_extraction_keeps_dashboard_branding_and_excludes_navigation() -> None:
    extracted = {
        "dashboard_metadata": {},
        "elements": [
            {
                "element_type": "button",
                "title": "Export",
                "is_application_chrome": True,
                "content": {"text": {"raw": "Export", "status": "read"}},
            },
            {
                "element_type": "navigation",
                "title": "Home",
                "content": {"text": {"raw": "Home", "status": "read"}},
            },
            {
                "element_type": "image",
                "title": "Company logo",
                "is_logo": True,
                "content": {},
            },
            {
                "element_type": "image",
                "title": "Sales by region",
                "content": {"text": {"raw": "Regional map", "status": "read"}},
            },
            {
                "element_type": "filter",
                "title": "Department",
                "content": {
                    "filter_name": "Department",
                    "selected_values": [{"raw": "All", "status": "read"}],
                },
            },
        ],
        "title_items": [
            {
                "text": "Export",
                "category": "button_label",
                "is_application_chrome": True,
            },
            {
                "text": "Filters",
                "category": "filter_pane_toggle",
            },
            {
                "text": "Company",
                "category": "brand_logo",
                "is_logo": True,
            },
            {
                "text": "Help",
                "category": "icon",
            },
            {
                "text": "Information",
                "category": "icon",
            },
            {
                "text": "Home",
                "category": "page_navigation",
            },
            {
                "text": "Pages",
                "category": "application_navigation",
            },
            {
                "text": "Compliance",
                "category": "page_title",
                "bounding_box": {"x": 10, "y": 240, "width": 120, "height": 20},
            },
            {
                "text": "HR Now - Compliance",
                "category": "page_title",
                "bounding_box": {"x": 200, "y": 90, "width": 320, "height": 40},
            },
            {
                "text": "Background decoration",
                "category": "decorative_image",
            },
            {
                "text": "Sales by region",
                "category": "visual_title",
                "associated_visual": "Sales by region",
            },
            {
                "text": "Department",
                "category": "filter_title",
                "associated_visual": "Department",
            },
            {
                "text": "All",
                "category": "slicer_value",
                "associated_visual": "Department",
            },
        ],
        "extraction_summary": {},
    }

    result = agents._prepare_screenshot_extraction(extracted, [b"image"], "Summary")

    assert [item["title"] for item in result["elements"]] == [
        "Sales by region",
        "Department",
    ]
    assert [item["text"] for item in result["title_items"]] == [
        "Company",
        "Help",
        "Information",
        "HR Now - Compliance",
        "Sales by region",
        "Department",
        "All",
    ]
    brand = result["title_items"][0]
    assert brand["standard_element_name"] == "brand_logo"
    assert brand["element_type"] == "brand_logo"
    assert brand["is_logo"] is True
    slicer_value = next(
        item for item in result["title_items"] if item["text"] == "All"
    )
    assert slicer_value["standard_element_name"] == "slicer_value"
    assert slicer_value["element_type"] == "slicer_value"
    assert slicer_value["associated_visual"] == "Department"
    assert result["extraction_summary"]["visible_element_count"] == 2


def test_shared_plan_does_not_override_default_all_category() -> None:
    shared_plan = {
        "filters": [
            {"name": "Category", "values": ["CANDY", "FRESH"]},
            {"name": "Week Ending", "values": ["3/14/2026"]},
        ]
    }
    inventory = {
        "filters": [
            {
                "name": "Category",
                "selected": ["All"],
                "values": ["Select all", "CANDY", "FRESH"],
            },
            {
                "name": "Week Ending",
                "selected": ["3/14/2026"],
                "values": ["3/14/2026", "3/21/2026"],
            },
        ]
    }

    assert _align_shared_plan(shared_plan, inventory) == {
        "filters": [
            {"name": "Category", "values": ["CANDY", "FRESH"]},
            {"name": "Week Ending", "values": ["3/14/2026", "3/21/2026"]},
        ]
    }


def test_shared_plan_includes_default_all_filters_without_aggregate_option() -> None:
    shared_plan = {"filters": [{"name": "Service Team", "values": ["Benefits"]}]}
    inventory = {
        "filters": [
            {
                "name": "Service Team",
                "selected": ["All"],
                "values": ["Benefits", "HRIS", "Learning"],
            }
        ]
    }

    assert _align_shared_plan(shared_plan, inventory) == {
        "filters": [
            {
                "name": "Service Team",
                "values": ["Benefits", "HRIS", "Learning"],
            }
        ]
    }
