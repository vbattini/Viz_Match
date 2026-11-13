from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


def read_json_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"missing evidence file: {path}"
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"could not read {path}: {type(exc).__name__}: {exc}"
    if not isinstance(value, dict):
        return None, f"evidence file is not a JSON object: {path}"
    return value, None


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        for attempt in range(5):
            try:
                temporary.replace(path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                logger.warning(
                    "Temporary permission error replacing %s; retrying (%d/5)",
                    path,
                    attempt + 1,
                )
                time.sleep(0.1 * (2**attempt))
    finally:
        temporary.unlink(missing_ok=True)
