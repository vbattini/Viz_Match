import asyncio
from types import SimpleNamespace

from powerbi_dashboard_comparator import browser, cli


def test_cli_defaults_to_supported_vertex_model_and_region(monkeypatch) -> None:
    for name in (
        "GEMINI_MODEL",
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_CLOUD_PROJECT",
    ):
        monkeypatch.delenv(name, raising=False)

    args = cli._parser().parse_args(
        [
            "--url-a",
            "https://app.powerbi.com/report-a",
            "--url-b",
            "https://app.powerbi.com/report-b",
        ]
    )

    assert args.model == "gemini-3.8-flash"
    assert args.location == "us"


def test_cli_uses_shared_dashboard_capture_pipeline(monkeypatch, tmp_path) -> None:
    captured = {}

    async def capture_dashboard_pair(**kwargs):
        captured.update(kwargs)
        return ["A state"], ["B state"]

    monkeypatch.setattr(browser, "capture_dashboard_pair", capture_dashboard_pair)
    monkeypatch.setattr(cli, "configure_vertex_ai", lambda *_args: None)
    monkeypatch.setattr(
        cli,
        "compare_dashboards",
        lambda states_a, states_b: {
            "summary": {},
            "states": (states_a, states_b),
        },
    )
    monkeypatch.setattr(
        cli, "write_comparison", lambda *_args, **_kwargs: None
    )

    asyncio.run(
        cli._run(
            SimpleNamespace(
                settle_seconds=0,
                storage_state=None,
                project_id="project",
                location="region",
                model="model",
                url_a="https://app.powerbi.com/report-a",
                url_b="https://app.powerbi.com/report-b",
                output_dir=tmp_path,
                headed=False,
                include_filter_cases=False,
            )
        )
    )

    assert {
        key: value
        for key, value in captured.items()
        if key not in {"job_id", "operation_log_path"}
    } == {
        "url_a": "https://app.powerbi.com/report-a",
        "url_b": "https://app.powerbi.com/report-b",
        "destination": tmp_path,
        "storage_state": None,
        "headed": False,
        "settle_seconds": 0,
        "include_filter_cases": False,
        "detect_visual_types": True,
    }
    assert captured["job_id"]
    assert captured["operation_log_path"] == tmp_path / "operation_timings.jsonl"
