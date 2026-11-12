import os

DEFAULT_PROJECT_ID = "gc-proj-aiml-dev-01fd"
DEFAULT_LOCATION = "us"
DEFAULT_MODEL_ID = "gemini-3.8-flash"


def positive_int_setting(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed < 1:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def positive_float_setting(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive number")
    return parsed


def configure_vertex_ai(
    project_id: str | None = None,
    location: str | None = None,
    model_id: str | None = None,
) -> None:
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE"
    os.environ["GOOGLE_CLOUD_PROJECT"] = (
        project_id or os.environ.get("GOOGLE_CLOUD_PROJECT") or DEFAULT_PROJECT_ID
    )
    os.environ["GOOGLE_CLOUD_LOCATION"] = (
        location or os.environ.get("GOOGLE_CLOUD_LOCATION") or DEFAULT_LOCATION
    )
    os.environ["GEMINI_MODEL"] = (
        model_id or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL_ID
    )
