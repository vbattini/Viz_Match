import os

from powerbi_dashboard_comparator.config import (
    DEFAULT_LOCATION,
    DEFAULT_MODEL_ID,
    configure_vertex_ai,
    positive_float_setting,
    positive_int_setting,
)
from powerbi_dashboard_comparator.api import CompareRequest
from pydantic import ValidationError


def test_default_vertex_ai_model_and_region(monkeypatch) -> None:
    for name in (
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "GEMINI_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    configure_vertex_ai()

    assert DEFAULT_LOCATION == "us"
    assert DEFAULT_MODEL_ID == "gemini-3.8-flash"
    assert os.environ["GOOGLE_CLOUD_LOCATION"] == "us"
    assert os.environ["GEMINI_MODEL"] == "gemini-3.8-flash"


def test_vertex_ai_configuration(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "old-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "old-location")
    monkeypatch.setenv("GEMINI_MODEL", "old-model")
    configure_vertex_ai(
        project_id="test-project",
        location="us-east4",
        model_id="gemini-2.5-flash",
    )

    assert os.environ["GOOGLE_GENAI_USE_VERTEXAI"] == "TRUE"
    assert os.environ["GOOGLE_CLOUD_PROJECT"] == "test-project"
    assert os.environ["GOOGLE_CLOUD_LOCATION"] == "us-east4"
    assert os.environ["GEMINI_MODEL"] == "gemini-2.5-flash"


def test_performance_settings_require_positive_values(monkeypatch) -> None:
    monkeypatch.setenv("PBI_WORKERS", "2")
    monkeypatch.setenv("PBI_TIMEOUT", "300")

    assert positive_int_setting("PBI_WORKERS", 4) == 2
    assert positive_float_setting("PBI_TIMEOUT", 60.0) == 300.0

    monkeypatch.setenv("PBI_WORKERS", "0")
    try:
        positive_int_setting("PBI_WORKERS", 4)
    except ValueError as exc:
        assert "positive integer" in str(exc)
    else:
        raise AssertionError("Non-positive worker limit should be rejected")


def test_compare_request_only_accepts_powerbi_host() -> None:
    request = CompareRequest(
        url_a="https://app.powerbi.com/reports/one",
        url_b="https://app.powerbi.com/reports/two",
    )

    assert request.url_a.endswith("/one")
    try:
        CompareRequest(url_a="https://example.com/report", url_b=request.url_b)
    except ValidationError:
        pass
    else:
        raise AssertionError("Non-Power BI host should be rejected")
