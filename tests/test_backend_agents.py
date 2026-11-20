import asyncio
import os

from google.auth.exceptions import TransportError
from google.genai.errors import ServerError

from Backend import agents


def test_backend_model_requests_allow_ten_concurrent_calls(monkeypatch) -> None:
    active_requests = 0
    maximum_active_requests = 0
    if os.getenv("PBI_MAX_PARALLEL_MODEL_REQUESTS") is None:
        assert agents.MAX_PARALLEL_MODEL_REQUESTS == 10
    monkeypatch.setattr(agents, "MAX_PARALLEL_MODEL_REQUESTS", 10)

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
        requests = [
            agents._run_agent(object(), f"request-{index}")
            for index in range(12)
        ]
        return await asyncio.gather(*requests)

    results = asyncio.run(run_batch())

    assert results == [f"request-{index}" for index in range(12)]
    assert maximum_active_requests == 10


def test_backend_model_requests_retry_transient_transport_failures(
    monkeypatch,
) -> None:
    attempts = 0
    delays = []
    monkeypatch.setattr(agents, "MODEL_REQUEST_ATTEMPTS", 2)

    async def flaky_request(_agent, _content):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TransportError("temporary transport failure")
        return "response"

    async def record_delay(delay):
        delays.append(delay)

    monkeypatch.setattr(agents, "_run_agent_once", flaky_request)
    monkeypatch.setattr(agents.asyncio, "sleep", record_delay)

    assert asyncio.run(agents._run_agent(object(), object())) == "response"
    assert attempts == 2
    assert delays == [1]


def test_backend_model_requests_retry_google_genai_503(monkeypatch) -> None:
    attempts = 0
    delays = []
    monkeypatch.setattr(agents, "MODEL_REQUEST_ATTEMPTS", 2)

    async def flaky_request(_agent, _content):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ServerError(
                503,
                {
                    "message": (
                        "upstream connect error or disconnect/reset before "
                        "headers"
                    )
                },
                None,
            )
        return "response"

    async def record_delay(delay):
        delays.append(delay)

    monkeypatch.setattr(agents, "_run_agent_once", flaky_request)
    monkeypatch.setattr(agents.asyncio, "sleep", record_delay)

    assert asyncio.run(agents._run_agent(object(), object())) == "response"
    assert attempts == 2
    assert delays == [1]
