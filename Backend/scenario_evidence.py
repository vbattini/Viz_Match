from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

EVIDENCE_MANIFEST = "evidence_manifest.json"
SCENARIO_DETAILS = {
    "01": (
        "01_table_column_header_comparison",
        "Table Column Header Comparison",
        "table_headers",
    ),
    "02": ("02_single_filter_selection", "Single Filter Selection", "single_filter"),
    "03": (
        "03_multiple_filter_selection",
        "Multiple Filter Selection",
        "multiple_filter",
    ),
    "04": ("04_drilldown", "Drill Down", "drilldown"),
}
LEGACY_SCENARIO_IDS = {"05": "01", "02": "02", "03": "03", "04": "04"}
_DASHBOARDS = ("dashboard_A", "dashboard_B")


def _token(value: str, limit: int = 28) -> str:
    token = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-") or "item"
    if len(token) <= limit:
        return token
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
    return f"{token[: limit - 9]}_{digest}"


def safe_screenshot_path(
    root: Path,
    dashboard_id: str,
    scenario_id: str,
    page_number: int,
    page_name: str,
    descriptor: str,
) -> Path:
    if dashboard_id not in _DASHBOARDS:
        raise ValueError(f"Unknown dashboard identifier: {dashboard_id}")
    try:
        _, _, scenario_slug = SCENARIO_DETAILS[scenario_id]
    except KeyError as exc:
        raise ValueError(f"Unknown scenario identifier: {scenario_id}") from exc

    parts = [dashboard_id, f"S{scenario_id}_{scenario_slug}", f"Page{page_number:02d}"]
    if descriptor and descriptor != "default":
        parts.append(_token(descriptor, 48))
    folder = root / dashboard_id / _token(page_name, 80)
    folder.mkdir(parents=True, exist_ok=True)
    stem = "_".join(parts)
    path = folder / f"{stem}.png"
    capture_number = 2
    while path.exists():
        path = folder / f"{stem}_Capture{capture_number:02d}.png"
        capture_number += 1
    return path


def scenario_result_directory(root: Path, scenario_id: str, page_name: str) -> Path:
    if scenario_id not in SCENARIO_DETAILS:
        raise ValueError(f"Unknown scenario identifier: {scenario_id}")
    return (
        root
        / "planning"
        / "scenario_evidence"
        / f"scenario_{scenario_id}"
        / "pages"
        / _token(page_name, 80)
    )


def dashboard_scenario_directory(
    root: Path, dashboard_id: str, scenario_id: str, page_name: str
) -> Path:
    if dashboard_id not in _DASHBOARDS:
        raise ValueError(f"Unknown dashboard identifier: {dashboard_id}")
    if scenario_id not in SCENARIO_DETAILS:
        raise ValueError(f"Unknown scenario identifier: {scenario_id}")
    return root / dashboard_id / _token(page_name, 80)


def dashboard_execution_evidence_path(
    root: Path,
    dashboard_id: str,
    scenario_id: str,
    page_name: str,
    *,
    status: bool = False,
) -> Path:
    if dashboard_id not in _DASHBOARDS:
        raise ValueError(f"Unknown dashboard identifier: {dashboard_id}")
    suffix = "execution_status" if status else "scenario_execution"
    return (
        scenario_result_directory(root, scenario_id, page_name)
        / f"{dashboard_id}_{suffix}.json"
    )


def _manifest_path(root: Path, dashboard_id: str) -> Path:
    return root / dashboard_id / EVIDENCE_MANIFEST


def read_evidence_manifest(root: Path, dashboard_id: str) -> dict[str, Any] | None:
    path = _manifest_path(root, dashboard_id)
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("screenshots"), list):
        raise ValueError(f"Invalid scenario evidence manifest: {path}")
    if value.get("dashboard_id") != dashboard_id:
        raise ValueError(f"Dashboard identifier mismatch in manifest: {path}")
    value["screenshots"] = [
        entry
        for entry in value["screenshots"]
        if not (
            isinstance(entry, dict)
            and str(entry.get("scenario_id")) == "04"
            and entry.get("capture_role") == "drilldown_initial"
        )
    ]
    return value


def _write_manifest(root: Path, dashboard_id: str, entries: list[dict[str, Any]]) -> None:
    path = _manifest_path(root, dashboard_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dashboard_id": dashboard_id,
                "screenshots": entries,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


class EvidenceManifestStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._lock = asyncio.Lock()
        self._entries: dict[str, list[dict[str, Any]]] = {}
        for dashboard_id in _DASHBOARDS:
            manifest = read_evidence_manifest(self.root, dashboard_id)
            self._entries[dashboard_id] = (
                list(manifest["screenshots"]) if manifest else []
            )

    def initialize(self) -> None:
        for dashboard_id in _DASHBOARDS:
            folder = self.root / dashboard_id
            folder.mkdir(parents=True, exist_ok=True)
            if not _manifest_path(self.root, dashboard_id).exists():
                _write_manifest(self.root, dashboard_id, self._entries[dashboard_id])

    async def add(self, entry: dict[str, Any]) -> None:
        dashboard_id = entry.get("dashboard_id")
        if dashboard_id not in _DASHBOARDS:
            raise ValueError(f"Unknown dashboard identifier: {dashboard_id}")
        async with self._lock:
            entries = self._entries[dashboard_id]
            screenshot_path = os.path.normcase(
                str(Path(entry["screenshot_full_path"]).resolve())
            )
            matching_index = next(
                (
                    index
                    for index, existing in enumerate(entries)
                    if os.path.normcase(
                        str(Path(existing["screenshot_full_path"]).resolve())
                    )
                    == screenshot_path
                ),
                None,
            )
            if matching_index is None:
                entries.append(entry)
            else:
                entries[matching_index] = entry
            entries.sort(
                key=lambda item: (
                    int(item.get("page_number") or 0),
                    str(item.get("scenario_id") or ""),
                    str(item.get("pairing_key") or ""),
                    str(item.get("screenshot_filename") or ""),
                )
            )
            _write_manifest(self.root, dashboard_id, entries)


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Evidence file is not a JSON object: {path}")
    return value


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _normal_key(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def evidence_pairing_key(
    scenario_id: str, selections: list[dict[str, Any]], role: str
) -> str:
    if scenario_id == "01":
        return role
    if scenario_id == "02":
        return "single_filter:" + "|".join(
            f"{_normal_key(item['filter_name'])}="
            f"{','.join(_normal_key(value) for value in (item.get('selected_values') or []))}"
            for item in selections
        )
    if scenario_id == "03":
        return "multiple_filter:" + "|".join(
            f"{_normal_key(item['filter_name'])}="
            f"{','.join(_normal_key(value) for value in (item.get('selected_values') or []))}"
            for item in selections
        )
    return role


def _replace_paths(value: Any, mapping: dict[str, str], root: Path) -> tuple[Any, bool]:
    if isinstance(value, str):
        candidates = [Path(value)]
        if not candidates[0].is_absolute():
            candidates.extend((Path.cwd() / candidates[0], root / candidates[0]))
        for candidate in candidates:
            key = _path_key(candidate)
            if key in mapping:
                return mapping[key], True
        return value, False
    if isinstance(value, list):
        changed = False
        result = []
        for item in value:
            updated, item_changed = _replace_paths(item, mapping, root)
            result.append(updated)
            changed = changed or item_changed
        return result, changed
    if isinstance(value, dict):
        changed = False
        result = {}
        for key, item in value.items():
            updated, item_changed = _replace_paths(item, mapping, root)
            result[key] = updated
            changed = changed or item_changed
        return result, changed
    return value, False


def _update_json_paths(root: Path, mapping: dict[str, str]) -> None:
    paths = [
        path
        for path in root.rglob("*.json")
        if path.name != EVIDENCE_MANIFEST
    ]
    paths.sort(key=lambda path: ("comparison_results" in path.parts, str(path)))
    for path in paths:
        value = json.loads(path.read_text(encoding="utf-8"))
        updated, changed = _replace_paths(value, mapping, root)
        if changed:
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(updated, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)


def _legacy_pages(source_root: Path, scenario_directory: str) -> list[tuple[str, Path]]:
    root = source_root / scenario_directory
    if not root.is_dir():
        return []
    page_dirs = sorted(path for path in root.iterdir() if path.is_dir())
    summary = _read_json_object(source_root / "validation_summary.json")
    names = {
        re.sub(r"[^A-Za-z0-9._-]+", "_", str(page.get("page", ""))).strip("_"): str(
            page.get("page", "")
        )
        for page in summary.get("pages", [])
        if isinstance(page, dict) and page.get("page")
    }
    result = []
    for page_dir in page_dirs:
        page_name = names.get(page_dir.name)
        if not page_name:
            for candidate in (
                page_dir / "dom_extraction.json",
                source_root / "planning" / page_dir.name / "dashboard_A_plan.json",
            ):
                data = _read_json_object(candidate)
                page_name = data.get("page") or data.get("page_name")
                if page_name:
                    break
        result.append((str(page_name or page_dir.name.replace("_", " ")), page_dir))
    return result


def _map_scenario_fields(value: Any, scenario_id: str) -> Any:
    if isinstance(value, list):
        return [_map_scenario_fields(item, scenario_id) for item in value]
    if not isinstance(value, dict):
        return value
    scenario_name = SCENARIO_DETAILS[scenario_id][1]
    mapped = {}
    for key, item in value.items():
        if key == "scenario_id" and isinstance(item, str):
            item = LEGACY_SCENARIO_IDS.get(item, item)
        elif key == "scenario_name" and isinstance(item, str):
            item = scenario_name
        mapped[key] = _map_scenario_fields(item, scenario_id)
    return mapped


def _entry_descriptor(entry: dict[str, Any], scenario_id: str) -> str:
    if scenario_id == "01":
        position = str(entry.get("scroll_position_id") or "initial")
        if position == "initial":
            return "Initial"
        suffix = position.rsplit("_", 1)[-1]
        return f"Scroll{int(suffix):02d}" if suffix.isdigit() else "Scroll01"
    if scenario_id == "02":
        selections = entry.get("filter_selections") or []
        detail = "_".join(
            f"{item.get('filter_name', 'Filter')}_"
            f"{'_'.join(str(value) for value in (item.get('selected_values') or []))}"
            for item in selections
            if isinstance(item, dict)
        )
        return "Filter01" + (f"_{detail}" if detail else "")
    if scenario_id == "03":
        return str(entry.get("combination_id") or "Combination01")
    level = entry.get("drill_down_level")
    visual = entry.get("visual_identifier") or "Visual"
    role = str(entry.get("capture_role") or "")
    phase = "before" if "before" in role else "after" if "after" in role else "Initial"
    return f"Visual_{visual}_Level{int(level):02d}_{phase}" if level else str(phase)


def _side_execution_payload(
    execution: dict[str, Any], scenario_id: str, dashboard_id: str
) -> dict[str, Any]:
    if scenario_id == "01":
        return dict(execution.get(dashboard_id) or {})
    if scenario_id in {"02", "03"}:
        side_index = 0 if dashboard_id == "dashboard_A" else 1
        steps = execution.get("steps") or []
        return {
            "plan": execution.get("plan"),
            "step": steps[side_index] if side_index < len(steps) else {},
        }
    visuals = []
    for item in execution.get("visuals") or []:
        if isinstance(item, dict):
            visuals.append(
                {
                    key: value
                    for key, value in item.items()
                    if key not in {"dashboard_A", "dashboard_B"}
                }
                | {dashboard_id: item.get(dashboard_id)}
            )
    return {"visuals": visuals}


def _page_name_lookup(source_root: Path) -> dict[str, str]:
    summary = _read_json_object(source_root / "validation_summary.json")
    names = {}
    for page in summary.get("pages", []):
        if isinstance(page, dict) and page.get("page"):
            name = str(page["page"])
            names[_token(name, 80)] = name
    return names


def organize_scenario_screenshots(
    scenario_sources: Mapping[str, Path], output_root: Path | None = None
) -> dict[str, int]:
    """Rehome screenshots into dashboard/page folders and verify evidence before cleanup."""
    roots = sorted(
        {
            Path(source).expanduser().resolve()
            for source in scenario_sources.values()
        },
        key=str,
    )
    counts = {dashboard_id: 0 for dashboard_id in _DASHBOARDS}

    for root in roots:
        manifests = {
            dashboard_id: read_evidence_manifest(root, dashboard_id)
            for dashboard_id in _DASHBOARDS
        }
        legacy_migration = False
        manifested_image_paths = {
            _path_key(Path(entry["screenshot_full_path"]))
            for manifest in manifests.values()
            if manifest
            for entry in manifest["screenshots"]
            if entry.get("screenshot_full_path")
        }
        page_names = _page_name_lookup(root)
        entries_by_side: dict[str, list[dict[str, Any]]] = {
            dashboard_id: [] for dashboard_id in _DASHBOARDS
        }
        path_mapping: dict[str, str] = {}
        json_path_mapping: dict[str, str] = {}
        old_images: set[Path] = set()

        for dashboard_id in _DASHBOARDS:
            for original in (manifests[dashboard_id] or {}).get("screenshots", []):
                old_scenario_id = str(original.get("scenario_id") or "")
                if old_scenario_id == "01" and str(
                    original.get("scenario_name") or ""
                ).casefold() in {"initial default state", "default filter state"}:
                    legacy_migration = True
                    path_value = original.get("screenshot_full_path")
                    if path_value:
                        source_path = Path(path_value).resolve()
                        if not source_path.is_file():
                            if original.get("capture_status") == "CAPTURED":
                                raise FileNotFoundError(
                                    f"Manifest screenshot is missing: {source_path}"
                                )
                            continue
                        page_name = str(original.get("page_name") or "Page")
                        archive_path = (
                            root
                            / "planning"
                            / "legacy_initial_default_state"
                            / "screenshots"
                            / dashboard_id
                            / _token(page_name, 80)
                            / source_path.name
                        )
                        archive_path.parent.mkdir(parents=True, exist_ok=True)
                        if archive_path.exists():
                            source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
                            archive_hash = hashlib.sha256(archive_path.read_bytes()).hexdigest()
                            if source_hash != archive_hash:
                                raise OSError(
                                    f"Legacy screenshot archive collision: {archive_path}"
                                )
                        else:
                            shutil.copy2(source_path, archive_path)
                        if hashlib.sha256(source_path.read_bytes()).hexdigest() != hashlib.sha256(
                            archive_path.read_bytes()
                        ).hexdigest():
                            raise OSError(
                                f"Legacy screenshot archive verification failed: {source_path}"
                            )
                        path_mapping[_path_key(source_path)] = str(
                            archive_path.resolve()
                        )
                        old_images.add(source_path)
                    continue
                scenario_id = LEGACY_SCENARIO_IDS.get(
                    old_scenario_id, old_scenario_id
                )
                legacy_migration = legacy_migration or old_scenario_id == "05"
                if scenario_id not in SCENARIO_DETAILS:
                    raise ValueError(
                        f"Unknown scenario identifier in evidence: {old_scenario_id}"
                    )
                entry = dict(original)
                source_path = Path(entry["screenshot_full_path"]).resolve()
                if not source_path.is_file():
                    if entry.get("capture_status") == "CAPTURED":
                        raise FileNotFoundError(
                            f"Manifest screenshot is missing: {source_path}"
                        )
                    continue
                page_name = str(entry.get("page_name") or "Page")
                page_number = int(entry.get("page_number") or 1)
                descriptor = _entry_descriptor(entry, scenario_id)
                expected_parent = dashboard_scenario_directory(
                    root, dashboard_id, scenario_id, page_name
                )
                expected_parent.mkdir(parents=True, exist_ok=True)
                if source_path.parent == expected_parent:
                    target_path = source_path
                else:
                    legacy_migration = True
                    target_path = (
                        expected_parent / source_path.name
                        if re.search(
                            rf"_S{re.escape(scenario_id)}_",
                            source_path.name,
                            flags=re.IGNORECASE,
                        )
                        else safe_screenshot_path(
                            root,
                            dashboard_id,
                            scenario_id,
                            page_number,
                            page_name,
                            descriptor,
                        )
                    )
                    if target_path.exists():
                        same_content = hashlib.sha256(target_path.read_bytes()).hexdigest() == hashlib.sha256(
                            source_path.read_bytes()
                        ).hexdigest()
                        if same_content:
                            target_path = target_path
                        else:
                            target_path = safe_screenshot_path(
                                root,
                                dashboard_id,
                                scenario_id,
                                page_number,
                                page_name,
                                descriptor,
                            )
                    if target_path != source_path and not target_path.exists():
                        shutil.copy2(source_path, target_path)
                    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
                    target_hash = hashlib.sha256(target_path.read_bytes()).hexdigest()
                    if source_hash != target_hash:
                        raise OSError(
                            f"Screenshot copy verification failed: {source_path}"
                        )
                    path_mapping[_path_key(source_path)] = str(target_path.resolve())
                    old_images.add(source_path)

                scenario_directory, scenario_name, _ = SCENARIO_DETAILS[scenario_id]
                result_dir = scenario_result_directory(root, scenario_id, page_name)
                entry.update(
                    {
                        "scenario_id": scenario_id,
                        "scenario_name": scenario_name,
                        "screenshot_filename": target_path.name,
                        "screenshot_full_path": str(target_path.resolve()),
                        "screenshot_capture_timestamp": entry.get(
                            "screenshot_capture_timestamp"
                        )
                        or datetime.fromtimestamp(
                            source_path.stat().st_mtime, timezone.utc
                        ).isoformat(),
                        "execution_json_evidence_path": str(
                            dashboard_execution_evidence_path(
                                root, dashboard_id, scenario_id, page_name
                            ).resolve()
                        ),
                        "comparison_json_evidence_path": str(
                            (
                                result_dir
                                / (
                                    "visual_comparison.json"
                                    if scenario_id == "01"
                                    else "comparison_result.json"
                                )
                            ).resolve()
                        ),
                        "migrated_from": str(source_path),
                        "sha256": hashlib.sha256(target_path.read_bytes()).hexdigest(),
                    }
                )
                counts[dashboard_id] += 1
                entries_by_side[dashboard_id].append(entry)

        for dashboard_id in _DASHBOARDS:
            for scenario_id, (scenario_directory, _, _) in SCENARIO_DETAILS.items():
                legacy_side_root = root / dashboard_id / scenario_directory
                if not legacy_side_root.is_dir():
                    continue
                for old_page_dir in (
                    path for path in legacy_side_root.iterdir() if path.is_dir()
                ):
                    page_name = page_names.get(
                        _token(old_page_dir.name, 80),
                        old_page_dir.name.replace("_", " "),
                    )
                    result_dir = scenario_result_directory(
                        root, scenario_id, page_name
                    )
                    result_dir.mkdir(parents=True, exist_ok=True)
                    json_path_mapping[_path_key(old_page_dir)] = str(
                        result_dir.resolve()
                    )
                    for source_file in old_page_dir.rglob("*"):
                        if not source_file.is_file() or source_file.suffix.casefold() == ".png":
                            continue
                        if source_file.name == "scenario_execution.json":
                            target_file = dashboard_execution_evidence_path(
                                root, dashboard_id, scenario_id, page_name
                            )
                        elif source_file.name == "execution_status.json":
                            target_file = dashboard_execution_evidence_path(
                                root,
                                dashboard_id,
                                scenario_id,
                                page_name,
                                status=True,
                            )
                        else:
                            target_file = (
                                result_dir
                                / f"{dashboard_id}_{source_file.name}"
                            )
                        target_file.parent.mkdir(parents=True, exist_ok=True)
                        if source_file.suffix.casefold() == ".json":
                            data = _map_scenario_fields(
                                _read_json_object(source_file), scenario_id
                            )
                            target_file.write_text(
                                json.dumps(
                                    data, ensure_ascii=False, indent=2, default=str
                                )
                                + "\n",
                                encoding="utf-8",
                            )
                        else:
                            shutil.copy2(source_file, target_file)
                        json_path_mapping[_path_key(source_file)] = str(
                            target_file.resolve()
                        )

        default_state_directory = root / "01_initial_default_state"
        default_state_archive = (
            root / "planning" / "legacy_initial_default_state" / "execution"
        )
        if default_state_directory.is_dir():
            json_path_mapping[_path_key(default_state_directory)] = str(
                default_state_archive.resolve()
            )
        for page_name, old_page_dir in _legacy_pages(
            root, "01_initial_default_state"
        ):
            page_name = page_names.get(_token(page_name, 80), page_name)
            target_dir = default_state_archive / _token(page_name, 80)
            target_dir.mkdir(parents=True, exist_ok=True)
            json_path_mapping[_path_key(old_page_dir)] = str(target_dir.resolve())
            for source_file in old_page_dir.rglob("*"):
                if not source_file.is_file():
                    continue
                relative = source_file.relative_to(old_page_dir)
                target_file = target_dir / relative
                target_file.parent.mkdir(parents=True, exist_ok=True)
                if target_file.exists():
                    source_hash = hashlib.sha256(source_file.read_bytes()).hexdigest()
                    target_hash = hashlib.sha256(target_file.read_bytes()).hexdigest()
                    if source_hash != target_hash:
                        raise OSError(
                            f"Legacy execution archive collision: {target_file}"
                        )
                else:
                    shutil.copy2(source_file, target_file)
                if hashlib.sha256(source_file.read_bytes()).hexdigest() != hashlib.sha256(
                    target_file.read_bytes()
                ).hexdigest():
                    raise OSError(
                        f"Legacy execution archive verification failed: {source_file}"
                    )
                json_path_mapping[_path_key(source_file)] = str(
                    target_file.resolve()
                )

        scenario_roots = {
            "01": "05_table_column_header_comparison",
            "02": "02_single_filter_selection",
            "03": "03_multiple_filter_selection",
            "04": "04_drilldown",
        }
        for new_scenario_id, old_directory in scenario_roots.items():
            pages = _legacy_pages(root, old_directory)
            for page_name, old_page_dir in pages:
                page_name = page_names.get(_token(page_name, 80), page_name)
                target_dir = scenario_result_directory(root, new_scenario_id, page_name)
                target_dir.mkdir(parents=True, exist_ok=True)
                json_path_mapping[_path_key(old_page_dir)] = str(
                    target_dir.resolve()
                )
                for source_file in old_page_dir.rglob("*"):
                    if not source_file.is_file() or source_file.suffix.casefold() == ".png":
                        continue
                    relative = source_file.relative_to(old_page_dir)
                    target_file = target_dir / relative
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    if source_file.suffix.casefold() == ".json":
                        data = _read_json_object(source_file)
                        data = _map_scenario_fields(data, new_scenario_id)
                        target_file.write_text(
                            json.dumps(
                                data, ensure_ascii=False, indent=2, default=str
                            )
                            + "\n",
                            encoding="utf-8",
                        )
                    else:
                        shutil.copy2(source_file, target_file)
                    json_path_mapping[_path_key(source_file)] = str(
                        target_file.resolve()
                    )

                execution_path = old_page_dir / "scenario_execution.json"
                execution = _read_json_object(execution_path)
                status_data = _read_json_object(
                    old_page_dir / "execution_status.json"
                )
                status = str(status_data.get("status") or "UNVERIFIED").upper()
                reason = status_data.get("reason")
                for dashboard_id in _DASHBOARDS:
                    execution_path = dashboard_execution_evidence_path(
                        root, dashboard_id, new_scenario_id, page_name
                    )
                    side_payload = {
                        "dashboard_id": dashboard_id,
                        "scenario_id": new_scenario_id,
                        "scenario_name": SCENARIO_DETAILS[new_scenario_id][1],
                        "page_name": page_name,
                        **_side_execution_payload(
                            execution, new_scenario_id, dashboard_id
                        ),
                    }
                    if not execution_path.is_file():
                        execution_path.write_text(
                            json.dumps(side_payload, ensure_ascii=False, indent=2)
                            + "\n",
                            encoding="utf-8",
                        )
                    status_path = dashboard_execution_evidence_path(
                        root,
                        dashboard_id,
                        new_scenario_id,
                        page_name,
                        status=True,
                    )
                    if not status_path.is_file():
                        status_path.write_text(
                            json.dumps(
                                {"status": status, "reason": reason},
                                ensure_ascii=False,
                                indent=2,
                            )
                            + "\n",
                            encoding="utf-8",
                        )

        legacy_header_results = (
            root / "comparison_results" / "scenario_05" / "pages"
        )
        if legacy_header_results.is_dir():
            legacy_migration = True
            for page_dir in legacy_header_results.iterdir():
                cache_file = page_dir / "comparison.json"
                if not cache_file.is_file():
                    continue
                data = _read_json_object(cache_file)
                page_name = str(
                    data.get("page_name")
                    or page_names.get(_token(page_dir.name, 80))
                    or page_dir.name.replace("_", " ")
                )
                target_cache = (
                    scenario_result_directory(root, "01", page_name)
                    / "comparison.json"
                )
                target_cache.parent.mkdir(parents=True, exist_ok=True)
                data = _map_scenario_fields(data, "01")
                data["scenario_id"] = "01"
                data["scenario_name"] = SCENARIO_DETAILS["01"][1]
                target_cache.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2, default=str)
                    + "\n",
                    encoding="utf-8",
                )
                json_path_mapping[_path_key(cache_file)] = str(
                    target_cache.resolve()
                )

        path_mapping.update(json_path_mapping)
        _update_json_paths(root, path_mapping)

        for dashboard_id, entries in entries_by_side.items():
            entries.sort(
                key=lambda item: (
                    int(item.get("page_number") or 0),
                    str(item.get("scenario_id") or ""),
                    str(item.get("pairing_key") or ""),
                    str(item.get("screenshot_filename") or ""),
                )
            )
            _write_manifest(root, dashboard_id, entries)

        for dashboard_id in _DASHBOARDS:
            manifest = read_evidence_manifest(root, dashboard_id) or {}
            for entry in manifest.get("screenshots", []):
                path = Path(entry.get("screenshot_full_path") or "")
                if not path.is_file():
                    raise FileNotFoundError(
                        f"Organized screenshot is missing: {path}"
                    )
                expected_hash = entry.get("sha256")
                if expected_hash and hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                    raise OSError(f"Organized screenshot hash mismatch: {path}")
                if not Path(entry["execution_json_evidence_path"]).is_file():
                    raise FileNotFoundError(
                        "Manifest execution evidence is missing: "
                        f"{entry['execution_json_evidence_path']}"
                    )
                if not Path(entry["comparison_json_evidence_path"]).is_file():
                    raise FileNotFoundError(
                        "Manifest comparison evidence is missing: "
                        f"{entry['comparison_json_evidence_path']}"
                    )

        for json_path in root.rglob("*.json"):
            if json_path.name == EVIDENCE_MANIFEST:
                continue
            data = json.loads(json_path.read_text(encoding="utf-8"))
            updated, changed = _replace_paths(data, path_mapping, root)
            if changed:
                raise OSError(
                    f"Evidence JSON still references an old location: {json_path}"
                )

        if legacy_migration:
            for scenario_id in SCENARIO_DETAILS:
                cached_results = (
                    root
                    / "comparison_results"
                    / f"scenario_{scenario_id}"
                    / "pages"
                )
                if not cached_results.is_dir():
                    continue
                for cache_file in cached_results.glob("*/comparison.json"):
                    os.utime(cache_file, None)

        legacy_dashboard_scenario_roots = {
            root / dashboard_id / scenario_directory
            for dashboard_id in _DASHBOARDS
            for scenario_directory, _, _ in SCENARIO_DETAILS.values()
        }
        for legacy_path in legacy_dashboard_scenario_roots:
            if not legacy_path.is_dir():
                continue
            for image_path in legacy_path.rglob("*.png"):
                if _path_key(image_path) not in manifested_image_paths:
                    raise OSError(
                        "Refusing to remove scenario evidence containing an "
                        f"unmanifested screenshot: {image_path}"
                    )
            for evidence_path in legacy_path.rglob("*"):
                if evidence_path.is_file() and evidence_path.suffix.casefold() != ".png":
                    if _path_key(evidence_path) not in json_path_mapping:
                        raise OSError(
                            "Refusing to remove unarchived scenario evidence: "
                            f"{evidence_path}"
                        )

        for old_directory in {
            "01_initial_default_state",
            *scenario_roots.values(),
        }:
            legacy_path = root / old_directory
            if not legacy_path.is_dir():
                continue
            for image_path in legacy_path.rglob("*.png"):
                if _path_key(image_path) not in manifested_image_paths:
                    raise OSError(
                        "Refusing to remove scenario evidence containing an "
                        f"unmanifested screenshot: {image_path}"
                    )

        organized_paths = {
            Path(entry["screenshot_full_path"]).resolve()
            for entries in entries_by_side.values()
            for entry in entries
        }
        for source_path in old_images:
            if source_path.exists() and source_path not in organized_paths:
                source_path.unlink()

        for old_directory in {"01_initial_default_state", *scenario_roots.values()}:
            legacy_path = root / old_directory
            if legacy_path.is_dir():
                shutil.rmtree(legacy_path)
        for legacy_path in legacy_dashboard_scenario_roots:
            if legacy_path.is_dir():
                shutil.rmtree(legacy_path)

    return counts


def consolidate_scenario_screenshots(
    scenario_sources: Mapping[str, Path], output_root: Path
) -> dict[str, int]:
    """Compatibility wrapper for callers using the former helper name."""
    return organize_scenario_screenshots(scenario_sources, output_root)
