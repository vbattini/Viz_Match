"""Deterministic functional validation scenarios for Dashboard A and Dashboard B.

Scenarios reuse the existing Playwright session, filter inventory, filter
selection, restore and screenshot helpers from ``browser.py``. Selection,
verification, drilldown and header comparison are plain Python logic; no
Gemini calls are made here.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agents import is_kpi_selection_filter
from .browser import (
    _POPUP_SCROLLING_ENABLED,
    CAPTURE_VIEWPORT,
    _accessible_inventory,
    _capture_browser_session,
    _current_filter_value,
    _filter_values_equivalent,
    _filter_values_match,
    _filters_match_baseline,
    _find_page_tab,
    _goto_report,
    _restore_report_filters,
    _select_filter,
    _wait_for_comparable_report_tabs,
    _wait_for_filter_selection,
    _wait_for_report_ready,
    _wait_for_state_render,
    _comparison_page_names,
    _move_pointer_off_dashboard,
)
from .config import positive_int_setting
from .models import FilterSelection
from .scenario_evidence import (
    EvidenceManifestStore,
    dashboard_execution_evidence_path,
    dashboard_scenario_directory,
    evidence_pairing_key,
    safe_screenshot_path,
    scenario_result_directory,
    SCENARIO_DETAILS,
)
from .timing import write_operation_event

SCENARIO_TABLE_HEADERS = "01_table_column_header_comparison"
SCENARIO_SINGLE_FILTER = "02_single_filter_selection"
SCENARIO_MULTIPLE_FILTER = "03_multiple_filter_selection"
SCENARIO_DRILLDOWN = "04_drilldown"
SCENARIO_DIRECTORIES = (
    SCENARIO_TABLE_HEADERS,
    SCENARIO_SINGLE_FILTER,
    SCENARIO_MULTIPLE_FILTER,
    SCENARIO_DRILLDOWN,
)
SCENARIO_BY_CODE = dict(zip(("01", "02", "03", "04"), SCENARIO_DIRECTORIES))
SCENARIO_ID_BY_NAME = {
    scenario_name: scenario_id
    for scenario_id, scenario_name in SCENARIO_BY_CODE.items()
}

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_SKIPPED = "SKIPPED"
STATUS_UNVERIFIED = "UNVERIFIED"

INVENTORY_TIMEOUT_SECONDS = 150
# Page pairs (A and B together) that run at once. Each pair holds two live Power BI pages.
VALIDATION_MAX_PARALLEL_PAGES = positive_int_setting(
    "PBI_VALIDATION_MAX_PARALLEL_PAGES", 5
)
# Safety bound only; drilling normally stops when the next-level control is disabled or absent.
MAX_DRILL_LEVELS = 10
HEADER_SCROLL_STEP_VIEWPORT_FRACTION = 0.5
HEADER_SCROLL_POSITION_TOLERANCE = 0.002
_AGGREGATE_LABELS = frozenset({"all", "select all"})
_WHITESPACE = re.compile(r"\s+")
_UNSAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def normalize_label(value: str) -> str:
    """Casefold and collapse whitespace so 'Region ' and 'REGION' compare equal."""
    return _WHITESPACE.sub(" ", value).strip().casefold()


def is_aggregate_label(value: str) -> bool:
    text = normalize_label(value).strip("()").strip()
    return text in _AGGREGATE_LABELS


def safe_path_part(value: str) -> str:
    return _UNSAFE_PATH_CHARS.sub("_", value).strip("_") or "item"


def individual_options(item: dict[str, Any]) -> list[str]:
    """Distinct, non-aggregate option labels in inventory order."""
    seen: set[str] = set()
    options: list[str] = []
    for value in item.get("values", []):
        if not isinstance(value, str) or not value.strip():
            continue
        if is_aggregate_label(value):
            continue
        key = normalize_label(value)
        if key in seen:
            continue
        seen.add(key)
        options.append(value)
    return options


def shared_option_pairs(
    item_a: dict[str, Any], item_b: dict[str, Any]
) -> list[tuple[str, str]]:
    """(label on A, label on B) for individual options present on both sides."""
    by_key_b: dict[str, str] = {}
    for value in individual_options(item_b):
        by_key_b.setdefault(normalize_label(value), value)
    pairs: list[tuple[str, str]] = []
    for value in individual_options(item_a):
        match = by_key_b.get(normalize_label(value))
        if match is not None:
            pairs.append((value, match))
    return pairs


def common_filters(
    items_a: list[dict[str, Any]], items_b: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Filters present on both dashboards after normalized-name matching."""
    index_b: dict[str, dict[str, Any]] = {}
    for item in items_b:
        if is_kpi_selection_filter(item):
            continue
        index_b.setdefault(normalize_label(item.get("name", "")), item)
    common: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items_a:
        if is_kpi_selection_filter(item):
            continue
        key = normalize_label(item.get("name", ""))
        if not key or key in seen or key not in index_b:
            continue
        seen.add(key)
        other = index_b[key]
        common.append(
            {
                "key": key,
                "item_a": item,
                "item_b": other,
                "pairs": shared_option_pairs(item, other),
            }
        )
    return sorted(common, key=lambda entry: entry["key"])


def plan_single_filter(
    common: list[dict[str, Any]], rng: random.Random
) -> dict[str, Any] | None:
    """Pick one common filter and one common option at random."""
    candidates = [entry for entry in common if entry["pairs"]]
    if not candidates:
        return None
    entry = rng.choice(candidates)
    value_a, value_b = rng.choice(entry["pairs"])
    return {
        "filter_key": entry["key"],
        "name_a": entry["item_a"]["name"],
        "name_b": entry["item_b"]["name"],
        "value_a": value_a,
        "value_b": value_b,
    }


def plan_multi_filter(
    common: list[dict[str, Any]],
    rng: random.Random,
    excluded_filter_key: str | None = None,
) -> dict[str, Any] | None:
    """Pick one shared option on a common filter distinct from the first filter."""
    excluded_key = normalize_label(excluded_filter_key or "")
    candidates = []
    for entry in common:
        if entry["key"] == excluded_key:
            continue
        selected_a = {
            normalize_label(value)
            for value in entry["item_a"].get("selected", [])
            if isinstance(value, str)
        }
        selected_b = {
            normalize_label(value)
            for value in entry["item_b"].get("selected", [])
            if isinstance(value, str)
        }
        available_pairs = [
            pair
            for pair in entry["pairs"]
            if normalize_label(pair[0]) not in selected_a
            and normalize_label(pair[1]) not in selected_b
        ]
        if available_pairs:
            candidates.append((entry, available_pairs))
    if not candidates:
        return None
    entry, pairs = rng.choice(candidates)
    value_a, value_b = rng.choice(pairs)
    return {
        "filter_key": entry["key"],
        "name_a": entry["item_a"]["name"],
        "name_b": entry["item_b"]["name"],
        "values_a": [value_a],
        "values_b": [value_b],
    }


def selection_outcome(
    verified_a: bool | None, verified_b: bool | None
) -> str:
    """True means verified, False means wrong value, None means unreadable state."""
    values = (verified_a, verified_b)
    if any(value is False for value in values):
        return STATUS_FAIL
    if any(value is None for value in values):
        return STATUS_UNVERIFIED
    return STATUS_PASS


def _types_compatible(type_a: str, type_b: str) -> bool:
    return not type_a or not type_b or type_a.casefold() == type_b.casefold()


def pair_visuals(
    visuals_a: list[dict[str, Any]], visuals_b: list[dict[str, Any]]
) -> list[tuple[dict[str, Any] | None, dict[str, Any] | None]]:
    """Pair visuals by normalized title and compatible visual type.

    Untitled visuals pair by compatible type in document order, because both dashboards come from the same report layout.
    """
    remaining = list(visuals_b)
    pairs: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] = []
    for visual_a in visuals_a:
        key = normalize_label(visual_a.get("title") or "")
        match = None
        if key:
            match = next(
                (
                    candidate
                    for candidate in remaining
                    if normalize_label(candidate.get("title") or "") == key
                    and _types_compatible(
                        visual_a.get("visual_type", ""),
                        candidate.get("visual_type", ""),
                    )
                ),
                None,
            )
        else:
            match = next(
                (
                    candidate
                    for candidate in remaining
                    if not normalize_label(candidate.get("title") or "")
                    and _types_compatible(
                        visual_a.get("visual_type", ""),
                        candidate.get("visual_type", ""),
                    )
                ),
                None,
            )
        if match is not None:
            remaining.remove(match)
        pairs.append((visual_a, match))
    pairs.extend((None, visual) for visual in remaining)
    return pairs


def drill_step_verified(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """A drill step counts only when the visible hierarchy changed (row or header text)."""
    if before is None or after is None:
        return False
    return (
        before.get("rows") != after.get("rows")
        or before.get("headers") != after.get("headers")
    )


def scenario_status(outcomes: list[str]) -> str:
    """Skipped operations never count as PASS."""
    applicable = [outcome for outcome in outcomes if outcome != STATUS_SKIPPED]
    if not applicable:
        return STATUS_SKIPPED
    if STATUS_FAIL in applicable:
        return STATUS_FAIL
    if STATUS_UNVERIFIED in applicable:
        return STATUS_UNVERIFIED
    return STATUS_PASS


_ROOTS_JS = r"""
const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
const visualRoots = () => {
  const seen = new Set();
  const roots = [];
  for (const el of document.querySelectorAll('div.visualContainer, [data-visual-id]')) {
    const root = el.closest('[data-visual-id]') || el;
    if (seen.has(root)) continue;
    seen.add(root);
    roots.push(root);
  }
  return roots;
};
const titleOf = (root) => {
  const t = root.querySelector('.visualTitle, .titleText');
  return norm(t ? t.innerText : root.getAttribute('aria-label'));
};
const visualInfos = () => visualRoots()
  .filter((root) => {
    const b = root.getBoundingClientRect();
    return b.width > 100 && b.height > 100;
  })
  .map((root, i) => {
    const title = titleOf(root);
    return {
      key: title || 'visual-' + i,
      root: root,
      rect: root.getBoundingClientRect(),
      title: title,
      visual_type: root.getAttribute('data-visual-type') || '',
    };
  });
"""

_TABLE_SCROLL_POSITION_SCRIPT = r"""(fraction) => {
  const grids = [...document.querySelectorAll('[role="grid"], [role="table"]')];
  const seen = new Set();
  const controls = [];
  for (const grid of grids) {
    const root = grid.closest('[data-visual-id]') || grid.closest('.visualContainer') || grid;
    if (seen.has(root)) continue;
    seen.add(root);
    const tracks = [...root.querySelectorAll('.scroll-bar-div')].filter((track) => {
      const r = track.getBoundingClientRect();
      return r.width > 100 && r.width > r.height * 4;
    });
    const custom = tracks.map((track) => {
      const thumb = track.querySelector('.scroll-bar-part-bar');
      if (!thumb) return null;
      const t = track.getBoundingClientRect();
      const b = thumb.getBoundingClientRect();
      const travel = Math.max(0, t.width - b.width);
      if (travel <= 2) return null;
      return {
        kind: 'custom',
        track: {x: t.x, y: t.y, width: t.width, height: t.height},
        thumb: {x: b.x, y: b.y, width: b.width, height: b.height},
        range_ratio: Math.max(0, t.width / Math.max(1, b.width) - 1),
        actual_fraction: Math.max(0, Math.min(1, (b.x - t.x) / travel)),
      };
    }).filter(Boolean);
    if (custom.length) {
      controls.push(...custom);
      continue;
    }
    const native = [grid, ...root.querySelectorAll('*')].filter((node) => {
      const style = getComputedStyle(node);
      return node.clientWidth > 100 && node.scrollWidth > node.clientWidth + 2 &&
        ['auto', 'scroll'].includes(style.overflowX);
    });
    for (const node of native) {
      const max = node.scrollWidth - node.clientWidth;
      const scrollTop = node.scrollTop;
      if (fraction !== null) node.scrollLeft = Math.round(max * Math.min(1, Math.max(0, fraction)));
      node.scrollTop = scrollTop;
      controls.push({
        kind: 'native',
        requested_fraction: fraction,
        max_scroll_left: max,
        range_ratio: max / node.clientWidth,
        actual_scroll_left: node.scrollLeft,
        actual_fraction: node.scrollLeft / max,
      });
    }
  }
  return {scroller_count: controls.length, controls: controls};
}"""

# Power BI shows the visual header toolbar only while its visual is hovered, so callers hover first.
_VISUAL_CENTERS_SCRIPT = (
    "() => {"
    + _ROOTS_JS
    + r"""
  return visualInfos().map((v) => ({
    key: v.key,
    title: v.title,
    visual_type: v.visual_type,
    x: v.rect.left + v.rect.width / 2,
    y: v.rect.top + v.rect.height / 2,
  }));
}
"""
)

# The toolbar button sits above its visual's container, so each button is matched to the
# smallest visual box that contains it, allowing 60px above the box top.
_DRILL_BUTTONS_SCRIPT = (
    "(prefix) => {"
    + _ROOTS_JS
    + r"""
  const selector = '[data-automation-type="drillDownLevelButton"], [data-testid="drill-down-level-btn"]';
  for (const old of document.querySelectorAll('[data-validation-drill]')) {
    old.removeAttribute('data-validation-drill');
  }
  const roots = visualInfos();
  const out = [];
  let counter = 0;
  for (const button of document.querySelectorAll(selector)) {
    const rect = button.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const owners = roots.filter((v) => cx >= v.rect.left && cx <= v.rect.right
      && cy >= v.rect.top - 60 && cy <= v.rect.bottom);
    owners.sort((a, b) => a.rect.width * a.rect.height - b.rect.width * b.rect.height);
    const owner = owners[0] || {key: '', title: '', visual_type: ''};
    const enabled = !button.disabled && button.getAttribute('aria-disabled') !== 'true';
    const tag = enabled ? prefix + '-' + counter++ : '';
    if (enabled) button.setAttribute('data-validation-drill', tag);
    out.push({
      key: owner.key,
      title: owner.title,
      visual_type: owner.visual_type,
      label: button.getAttribute('aria-label') || '',
      enabled: enabled,
      tag: tag,
    });
  }
  return out;
}
"""
)


_VISUAL_SNAPSHOT_SCRIPT = (
    "(key) => {"
    + _ROOTS_JS
    + r"""
  const info = visualInfos().find((v) => v.key === key);
  if (!info) return null;
  const root = info.root;
  const rows = [...root.querySelectorAll('[role="rowheader"], [role="gridcell"]')]
    .slice(0, 200).map((c) => norm(c.innerText));
  const headers = [...root.querySelectorAll('[role="columnheader"]')]
    .slice(0, 50).map((c) => norm(c.innerText));
  return {
    rows: rows,
    headers: headers,
    drill_up_available: !!root.querySelector('button[aria-label*="drill up" i], [role="button"][aria-label*="drill up" i]'),
  };
}
"""
)


async def _capture_png(page: Any, path: Path, settle_seconds: float) -> str:
    await _wait_for_state_render(page, settle_seconds)
    path.parent.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(path), full_page=True)
    return str(path)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )


def _file_sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class _Side:
    """One dashboard's open page for a single report page."""

    def __init__(
        self,
        label: str,
        url: str,
        page_name: str,
        page: Any,
        diagnostics: Path,
        *,
        page_number: int = 1,
        manifest_store: EvidenceManifestStore | None = None,
    ) -> None:
        self.label = label
        self.url = url
        self.page_name = page_name
        self.page = page
        self.diagnostics = diagnostics
        self.page_number = page_number
        self.manifest_store = manifest_store
        self.filters: list[dict[str, Any]] = []
        self.baseline: list[dict[str, Any]] = []
        self.inventory_error: str | None = None

    async def reload(self) -> None:
        await _goto_report(self.page, self.url)
        await _wait_for_report_ready(self.page)
        await _find_page_tab(self.page, self.page_name)

    async def restore(self) -> bool:
        restored = await _restore_report_filters(self.page, self.baseline, self.diagnostics)
        if restored and await _filters_match_baseline(self.page, self.baseline):
            return True
        await self.reload()
        restored = await _restore_report_filters(self.page, self.baseline, self.diagnostics)
        return bool(restored) and await _filters_match_baseline(self.page, self.baseline)

    async def inventory(self) -> list[dict[str, Any]]:
        try:
            inventory = await asyncio.wait_for(
                _accessible_inventory(
                    self.page,
                    self.diagnostics,
                    clear_selection_actions=False,
                    inspect_filter_options=True,
                ),
                timeout=INVENTORY_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            self.inventory_error = f"{self.label} filter inventory failed: {type(exc).__name__}: {exc}"
            return []
        return list(inventory.get("filters", []))


def _filter_selections(filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selections = []
    for item in filters:
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        selected = item.get("selected")
        selections.append(
            {
                "filter_name": name,
                "selected_values": (
                    list(selected)
                    if isinstance(selected, (list, tuple))
                    else [selected]
                    if selected is not None
                    else None
                ),
            }
        )
    return selections


def _selection_record(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not record:
        return None
    name = record.get("filter")
    if not isinstance(name, str) or not name.strip():
        return None
    selected = record.get("selected")
    requested = record.get("requested")
    if isinstance(selected, (list, tuple)):
        values = list(selected)
    elif selected is None:
        values = None
    else:
        values = [selected]
    if isinstance(requested, (list, tuple)):
        requested_values = list(requested)
    elif requested is None:
        requested_values = None
    else:
        requested_values = [requested]
    return {
        "filter_name": name,
        "selected_values": values,
        "requested_values": requested_values,
        "selection_verified": record.get("verified"),
    }


def _scenario_execution_paths(side: _Side, scenario_id: str) -> tuple[Path, Path]:
    execution_path = dashboard_execution_evidence_path(
        side.diagnostics, side.label, scenario_id, side.page_name
    )
    return execution_path, dashboard_execution_evidence_path(
        side.diagnostics,
        side.label,
        scenario_id,
        side.page_name,
        status=True,
    )


def _write_side_scenario_evidence(
    side: _Side,
    scenario_id: str,
    execution: dict[str, Any],
    status: str,
    reason: str | None,
) -> None:
    execution_path = dashboard_execution_evidence_path(
        side.diagnostics, side.label, scenario_id, side.page_name
    )
    _write_json(
        execution_path,
        {
            "dashboard_id": side.label,
            "page_name": side.page_name,
            "scenario_id": scenario_id,
            "scenario_name": SCENARIO_DETAILS[scenario_id][1],
            **execution,
        },
    )
    _write_json(
        dashboard_execution_evidence_path(
            side.diagnostics,
            side.label,
            scenario_id,
            side.page_name,
            status=True,
        ),
        {"status": status, "reason": reason},
    )


def _selection_descriptor(selections: list[dict[str, Any]], prefix: str) -> str:
    parts = []
    for item in selections:
        values = "-".join(
            str(value)
            for value in (
                item.get("selected_values")
                or item.get("requested_values")
                or []
            )
        )
        parts.append(f"{item['filter_name']}_{values or 'Unspecified'}")
    return f"{prefix}_{'_'.join(parts)}" if parts else prefix


async def _capture_scenario_png(
    side: _Side,
    scenario_id: str,
    descriptor: str,
    settle_seconds: float,
    *,
    capture_role: str,
    filter_selections: list[dict[str, Any]] | None = None,
    combination_id: str | None = None,
    drill_down_level: int | None = None,
    visual_identifier: str | None = None,
    horizontal_scroll_position: Any = None,
    scroll_position_id: str | None = None,
    horizontal_scrollbar_present: bool | None = None,
    full_page: bool = True,
    wait_for_render: bool = True,
) -> str:
    selections = filter_selections or []
    screenshot_path = safe_screenshot_path(
        side.diagnostics,
        side.label,
        scenario_id,
        side.page_number,
        side.page_name,
        descriptor,
    )
    execution_path, status_path = _scenario_execution_paths(side, scenario_id)
    selected_values = {
        item["filter_name"]: item.get("selected_values") for item in selections
    }
    requested_values = {
        item["filter_name"]: item.get("requested_values") for item in selections
    }
    pairing_key = evidence_pairing_key(scenario_id, selections, capture_role)
    if scenario_id == "04":
        pairing_key = (
            f"{capture_role}:{normalize_label(visual_identifier or 'page')}:"
            f"{drill_down_level if drill_down_level is not None else 'initial'}"
        )
    elif scenario_id == "01":
        pairing_key = f"{capture_role}:{scroll_position_id or 'initial'}"
    entry: dict[str, Any] = {
        "dashboard_id": side.label,
        "page_name": side.page_name,
        "page_identifier": safe_path_part(side.page_name),
        "page_number": side.page_number,
        "scenario_id": scenario_id,
        "scenario_name": SCENARIO_DETAILS[scenario_id][1],
        "capture_role": capture_role,
        "pairing_key": pairing_key,
        "screenshot_filename": screenshot_path.name,
        "screenshot_full_path": str(screenshot_path.resolve()),
        "filter_names": [item["filter_name"] for item in selections],
        "filter_selections": selections,
        "selected_filter_values": selected_values,
        "requested_filter_values": requested_values,
        "combination_id": combination_id,
        "drill_down_level": drill_down_level,
        "visual_identifier": visual_identifier,
        "horizontal_scroll_position": horizontal_scroll_position,
        "scroll_position_id": scroll_position_id,
        "horizontal_scrollbar_present": horizontal_scrollbar_present,
        "screenshot_capture_timestamp": datetime.now(timezone.utc).isoformat(),
        "capture_status": "CAPTURE_PENDING",
        "screenshot_capture_status": "CAPTURE_PENDING",
        "execution_json_evidence_path": str(execution_path.resolve()),
        "execution_status_json_evidence_path": str(status_path.resolve()),
    }
    try:
        if wait_for_render:
            await _wait_for_state_render(side.page, settle_seconds)
        await _move_pointer_off_dashboard(side.page)
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        await side.page.screenshot(
            path=str(screenshot_path), full_page=full_page
        )
    except Exception as exc:
        entry["capture_status"] = "CAPTURE_FAILED"
        entry["screenshot_capture_status"] = "CAPTURE_FAILED"
        entry["capture_error"] = f"{type(exc).__name__}: {exc}"
        if side.manifest_store is not None:
            await side.manifest_store.add(entry)
        raise

    entry["capture_status"] = "CAPTURED"
    entry["screenshot_capture_status"] = "CAPTURED"
    if screenshot_path.is_file():
        entry["sha256"] = _file_sha256(str(screenshot_path))
    if side.manifest_store is not None:
        await side.manifest_store.add(entry)
    return str(screenshot_path)


def _filter_item(filters: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next(
        (item for item in filters if normalize_label(item.get("name", "")) == normalize_label(name)),
        None,
    )


async def _apply_single(side: _Side, name: str, value: str) -> dict[str, Any]:
    record: dict[str, Any] = {"filter": name, "requested": [value], "applied": True}
    try:
        await _select_filter(
            side.page, FilterSelection(name=name, value=value), side.filters, side.diagnostics
        )
    except Exception as exc:  # noqa: BLE001 - evidence is recorded, scenario fails
        record.update(applied=False, error=f"{type(exc).__name__}: {exc}", verified=None)
        return record
    item = _filter_item(side.filters, name)
    selected = await _wait_for_filter_selection(side.page, item, value) if item else None
    record["selected"] = selected
    record["verified"] = None if selected is None else _filter_values_match([value], selected)
    return record


async def _verify_filter_transition(
    side: _Side, name: str, selected: list[str]
) -> dict[str, Any]:
    baseline_target = _filter_item(side.baseline, name)
    if baseline_target is None:
        return {
            "changed_from_initial": None,
            "initial_values": None,
            "initial_source": "initial_filter_inventory_unavailable",
            "other_filters_unchanged": None,
            "other_filters": [],
            "verified": None,
        }

    initial_values = baseline_target.get("selected")
    if not isinstance(initial_values, list):
        initial_values = None
        initial_source = "initial_selection_unavailable"
    else:
        initial_source = "initial_inventory"
    changed = (
        None
        if initial_values is None
        else not _filter_values_equivalent(
            baseline_target, initial_values, selected
        )
    )

    other_results: list[dict[str, Any]] = []
    for item in side.baseline:
        if normalize_label(item.get("name", "")) == normalize_label(name):
            continue
        expected = item.get("selected")
        current = await _current_filter_value(side.page, item)
        if current is None or not isinstance(expected, list):
            unchanged = None
        else:
            unchanged = _filter_values_equivalent(item, expected, current)
        other_results.append(
            {
                "filter": item.get("name"),
                "initial_values": expected,
                "current_values": current,
                "verified": unchanged,
            }
        )

    if any(item["verified"] is False for item in other_results):
        other_unchanged = False
    elif any(item["verified"] is None for item in other_results):
        other_unchanged = None
    else:
        other_unchanged = True

    if changed is False or other_unchanged is False:
        verified = False
    elif changed is None or other_unchanged is None:
        verified = None
    else:
        verified = True
    return {
        "changed_from_initial": changed,
        "initial_values": initial_values,
        "initial_source": initial_source,
        "other_filters_unchanged": other_unchanged,
        "other_filters": other_results,
        "verified": verified,
    }


async def _hover_visual(side: _Side, key: str) -> bool:
    """Hover a visual so Power BI renders its header toolbar; returns False if not found."""
    centers = await side.page.evaluate(_VISUAL_CENTERS_SCRIPT)
    center = next((c for c in centers if c["key"] == key), None)
    if center is None:
        return False
    await side.page.mouse.move(center["x"], center["y"])
    await side.page.wait_for_timeout(350)
    return True


async def _find_drill_control(side: _Side, key: str, prefix: str) -> dict[str, Any] | None:
    """Return the drill button owned by the visual `key`, preferring an enabled one."""
    buttons = await side.page.evaluate(_DRILL_BUTTONS_SCRIPT, prefix)
    owned = [b for b in buttons if b["key"] == key]
    if not owned:
        return None
    return next((b for b in owned if b["enabled"]), owned[0])


async def _discover_drill_candidates(side: _Side, prefix: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    centers = await side.page.evaluate(_VISUAL_CENTERS_SCRIPT)
    for center in centers:
        if not await _hover_visual(side, center["key"]):
            continue
        control = await _find_drill_control(side, center["key"], prefix)
        if control and control["enabled"]:
            candidates.append(
                {
                    "key": center["key"],
                    "title": center["title"],
                    "visual_type": center["visual_type"],
                    "control_label": control["label"],
                }
            )
    return candidates


async def _snapshot_visual(side: _Side, key: str) -> dict[str, Any] | None:
    return await side.page.evaluate(_VISUAL_SNAPSHOT_SCRIPT, key)


async def _drill_visual(
    side: _Side,
    key: str,
    visual_dir: Path,
    settle_seconds: float,
    visual_identifier: str | None = None,
) -> dict[str, Any]:
    """Drill one visual level by level; each step must change its visible rows."""
    steps: list[dict[str, Any]] = []
    before = await _snapshot_visual(side, key)
    result: dict[str, Any] = {
        "visual": key,
        "initial_screenshot": None,
        "steps": steps,
        "status": STATUS_UNVERIFIED,
    }
    if before is None:
        result["reason"] = "visual not found after navigation"
        return result
    stop_reason = "next-level control disabled or absent"
    for level in range(1, MAX_DRILL_LEVELS + 1):
        if not await _hover_visual(side, key):
            stop_reason = "visual no longer found"
            break
        control = await _find_drill_control(side, key, f"drill{level}")
        if control is None:
            stop_reason = "next-level control absent"
            break
        if not control["enabled"]:
            stop_reason = "next-level control disabled"
            break
        visual_id = visual_identifier or key
        before_shot = await _capture_scenario_png(
            side,
            "04",
            f"Visual_{visual_id}_Level{level:02d}_Before",
            settle_seconds,
            capture_role="drilldown_before",
            drill_down_level=level - 1,
            visual_identifier=visual_id,
        )
        await side.page.locator(f'[data-validation-drill="{control["tag"]}"]').click()
        after = None
        verified = False
        deadline = asyncio.get_running_loop().time() + 8
        while asyncio.get_running_loop().time() < deadline:
            after = await _snapshot_visual(side, key)
            if drill_step_verified(before, after):
                verified = True
                break
            await side.page.wait_for_timeout(250)
        after_shot = await _capture_scenario_png(
            side,
            "04",
            f"Visual_{visual_id}_Level{level:02d}_After",
            settle_seconds,
            capture_role="drilldown_after",
            drill_down_level=level,
            visual_identifier=visual_id,
        )
        steps.append(
            {
                "level": level,
                "control_label": control["label"],
                "verified": verified,
                "before_screenshot": before_shot,
                "after_screenshot": after_shot,
                "rows_before": (before or {}).get("rows", [])[:20],
                "rows_after": (after or {}).get("rows", [])[:20],
            }
        )
        if not verified:
            result["reason"] = f"drill level {level} did not change the hierarchy"
            result["stop_reason"] = "unverified step"
            return result
        before = after
    else:
        stop_reason = f"stopped at safety bound of {MAX_DRILL_LEVELS} levels"
    result["stop_reason"] = stop_reason
    if not steps:
        result["status"] = STATUS_SKIPPED
        result["reason"] = "no verified drill-down control"
        return result
    result["initial_screenshot"] = steps[0]["before_screenshot"]
    result["status"] = STATUS_PASS
    return result


async def _restore_both(side_a: _Side, side_b: _Side) -> bool:
    restored_a, restored_b = await asyncio.gather(side_a.restore(), side_b.restore())
    return restored_a and restored_b


def _selected_scenarios(only: str | None, last: str | None) -> list[str]:
    start = SCENARIO_DIRECTORIES.index(only) if only else 0
    end = SCENARIO_DIRECTORIES.index(last) + 1 if last else len(SCENARIO_DIRECTORIES)
    return list(SCENARIO_DIRECTORIES[start:end])


async def _guard_step(name: str, step: Any) -> dict[str, Any]:
    """A failing step marks only itself UNVERIFIED; the page keeps running."""
    try:
        return await step
    except Exception as exc:
        return {"status": STATUS_UNVERIFIED, "reason": f"{name} failed: {type(exc).__name__}: {exc}"}





async def _capture_side(
    side: _Side,
    folder: Path,
    settle_seconds: float,
    *,
    scenario_id: str,
    descriptor: str,
    capture_role: str,
    filter_selections: list[dict[str, Any]] | None = None,
    combination_id: str | None = None,
) -> str:
    return await _capture_scenario_png(
        side,
        scenario_id,
        descriptor,
        settle_seconds,
        capture_role=capture_role,
        filter_selections=filter_selections,
        combination_id=combination_id,
    )


async def _safe_restore(side: _Side) -> tuple[bool, str | None]:
    """Restore the default state; a restore error is recorded rather than raised."""
    try:
        return await side.restore(), None
    except Exception as exc:  # noqa: BLE001 - evidence is recorded, scenario reports it
        return False, f"{type(exc).__name__}: {exc}"


async def _single_step(
    side: _Side, plan: dict[str, Any], folder: Path, settle_seconds: float, keep_state: bool = False
) -> dict[str, Any]:
    name, value = (
        (plan["name_a"], plan["value_a"]) if side.label == "dashboard_A" else (plan["name_b"], plan["value_b"])
    )
    shot = None
    keep = False
    record: dict[str, Any] = {}
    try:
        record = await _apply_single(side, name, value)
        if record.get("verified") is True:
            transition = await _verify_filter_transition(
                side, name, record.get("selected") or []
            )
            record["state_transition"] = transition
            if transition.get("verified") is False:
                record["verified"] = False
            elif transition.get("verified") is None:
                record["verified"] = None
        if record.get("verified") is True:
            selection = _selection_record(record)
            selections = [selection] if selection else []
            shot = await _capture_side(
                side,
                folder,
                settle_seconds,
                scenario_id="02",
                descriptor=_selection_descriptor(selections, "Filter01"),
                capture_role="single_filter",
                filter_selections=selections,
                combination_id="Filter01",
            )
        else:
            record["screenshot_skipped_reason"] = (
                "filter selection transition or unchanged filters were not verified"
            )
    finally:
        keep = keep_state and shot is not None and record.get("verified") is True
        restored, restore_error = (None, None) if keep else await _safe_restore(side)
    return {
        "record": record, "verified": record.get("verified"), "screenshot": shot,
        "restored": restored, "restore_error": restore_error, "kept_for_next": keep,
    }


async def _multi_step(
    side: _Side,
    plan: dict[str, Any],
    folder: Path,
    settle_seconds: float,
    base: dict[str, Any],
) -> dict[str, Any]:
    """Apply one option on a distinct filter and verify the first filter stayed selected."""
    name, values = (
        (plan["name_a"], plan["values_a"]) if side.label == "dashboard_A" else (plan["name_b"], plan["values_b"])
    )
    value = values[0]
    shot = None
    record: dict[str, Any] = {}
    try:
        record = await _apply_single(side, name, value)
        base_record = base["record"]
        base_item = _filter_item(side.filters, base_record.get("filter", ""))
        base_now = await _current_filter_value(side.page, base_item) if base_item else None
        if base_now is not None:
            base_ok = _filter_values_match(base_record.get("requested", []), base_now)
            source = "dom_readback"
        else:
            base_ok = None
            base_now = base_record.get("selected")
            source = "filter_not_found" if base_item is None else "dom_readback_unavailable"
        record["base_selection"] = {
            "filter": base_record.get("filter"),
            "requested": base_record.get("requested"),
            "selected": base_now,
            "verified": base_ok,
            "source": source,
        }
        if record.get("verified") is False or base_ok is False:
            record["verified"] = False
        elif record.get("verified") is None or base_ok is None:
            record["verified"] = None
        else:
            record["verified"] = True
        selections = []
        base_selection = _selection_record(base.get("record") or {})
        if base_selection:
            selections.append(base_selection)
        selection = _selection_record(record)
        if selection and all(
            normalize_label(item["filter_name"])
            != normalize_label(selection["filter_name"])
            for item in selections
        ):
            selections.append(selection)
        if record.get("verified") is True:
            shot = await _capture_side(
                side,
                folder,
                settle_seconds,
                scenario_id="03",
                descriptor=_selection_descriptor(selections, "Combination01"),
                capture_role="multiple_filter",
                filter_selections=selections,
                combination_id="Combination01",
            )
        else:
            record["screenshot_skipped_reason"] = (
                "new filter selection or preserved first selection was not verified"
            )
    finally:
        restored, restore_error = await _safe_restore(side)
    return {
        "record": record, "verified": record.get("verified"), "screenshot": shot,
        "restored": restored, "restore_error": restore_error,
    }


async def _run_filter_scenarios(
    sides: tuple[_Side, _Side],
    selected: list[str],
    filter_error: str | None,
    single_plan: dict[str, Any] | None,
    multi_plan: dict[str, Any] | None,
    dirs: dict[str, Path],
    settle_seconds: float,
) -> dict[str, dict[str, Any]]:
    """Run the filter steps in order, gating the second on both first-step results."""
    records: dict[str, dict[str, Any]] = {side.label: {} for side in sides}
    if SCENARIO_SINGLE_FILTER in selected:
        if filter_error:
            for side in sides:
                records[side.label][SCENARIO_SINGLE_FILTER] = {
                    "status": STATUS_UNVERIFIED,
                    "reason": filter_error,
                }
        elif single_plan is None:
            for side in sides:
                records[side.label][SCENARIO_SINGLE_FILTER] = {
                    "status": STATUS_SKIPPED,
                    "reason": "no common filter option",
                }
        else:
            keep_for_multiple = (
                SCENARIO_MULTIPLE_FILTER in selected and multi_plan is not None
            )
            first_results = await asyncio.gather(
                *(
                    _guard_step(
                        SCENARIO_SINGLE_FILTER,
                        _single_step(
                            side,
                            single_plan,
                            dirs[SCENARIO_SINGLE_FILTER],
                            settle_seconds,
                            keep_state=keep_for_multiple,
                        ),
                    )
                    for side in sides
                )
            )
            for side, result in zip(sides, first_results):
                records[side.label][SCENARIO_SINGLE_FILTER] = result

    if SCENARIO_MULTIPLE_FILTER in selected:
        skip_or_error = None
        if filter_error:
            skip_or_error = {"status": STATUS_UNVERIFIED, "reason": filter_error}
        elif SCENARIO_SINGLE_FILTER not in selected:
            skip_or_error = {
                "status": STATUS_SKIPPED,
                "reason": (
                    "single-filter scenario was not run; its selection cannot be preserved"
                ),
            }
        elif single_plan is None:
            skip_or_error = {
                "status": STATUS_SKIPPED,
                "reason": "no common filter option for the single-filter prerequisite",
            }
        elif multi_plan is None:
            skip_or_error = {
                "status": STATUS_SKIPPED,
                "reason": (
                    "no distinct common filter with an unselected shared option"
                ),
            }
        else:
            first_results = [
                records[side.label].get(SCENARIO_SINGLE_FILTER, {})
                for side in sides
            ]
            if not all(
                result.get("verified") is True
                and result.get("kept_for_next") is True
                for result in first_results
            ):
                skip_or_error = {
                    "status": STATUS_SKIPPED,
                    "reason": (
                        "single-filter selection was not verified on both dashboards; "
                        "additional filter scenario skipped"
                    ),
                }
            else:
                multi_results = await asyncio.gather(
                    *(
                        _guard_step(
                            SCENARIO_MULTIPLE_FILTER,
                            _multi_step(
                                side,
                                multi_plan,
                                dirs[SCENARIO_MULTIPLE_FILTER],
                                settle_seconds,
                                base=records[side.label][SCENARIO_SINGLE_FILTER],
                            ),
                        )
                        for side in sides
                    )
                )
                for side, result in zip(sides, multi_results):
                    records[side.label][SCENARIO_MULTIPLE_FILTER] = result
                    single_result = records[side.label][SCENARIO_SINGLE_FILTER]
                    single_result["kept_for_next"] = False
                    single_result["restored"] = result.get("restored")
                    single_result["restore_error"] = result.get("restore_error")
                    single_result["restore_performed_by"] = SCENARIO_MULTIPLE_FILTER

        if skip_or_error is not None:
            for side in sides:
                records[side.label][SCENARIO_MULTIPLE_FILTER] = dict(skip_or_error)
    return records


async def _drill_step(
    side: _Side,
    pairs: list[tuple[dict[str, Any] | None, dict[str, Any] | None]],
    folder: Path,
    settle_seconds: float,
) -> dict[str, Any]:
    """Drill each paired visual on this dashboard, restoring the page after each one."""
    index = 0 if side.label == "dashboard_A" else 1
    visuals: list[dict[str, Any]] = []
    for pair in pairs:
        title = (pair[0] or pair[1] or {}).get("title", "")
        label = (pair[0] or pair[1] or {}).get("key", "") or "visual"
        if pair[0] is None or pair[1] is None:
            visuals.append({"visual": label, "status": STATUS_UNVERIFIED,
                            "reason": "drill control found on one dashboard only"})
            continue
        try:
            result = await _drill_visual(
                side,
                pair[index]["key"],
                folder / safe_path_part(label),
                settle_seconds,
                visual_identifier=title or label,
            )
        except Exception as exc:
            result = {"visual": label, "status": STATUS_UNVERIFIED, "steps": [],
                      "reason": f"{type(exc).__name__}: {exc}"}
        result["title"] = title
        result["restored"] = await side.restore()
        visuals.append(result)
    return {"visuals": visuals}


async def _header_initial_step(
    side: _Side, folder: Path, settle_seconds: float
) -> dict[str, Any]:
    """Capture the initial header viewport and inspect scroll-control geometry only."""
    await _wait_for_state_render(side.page, settle_seconds)
    scroll_state = await side.page.evaluate(_TABLE_SCROLL_POSITION_SCRIPT, None)
    controls = scroll_state.get("controls", [])
    ranges = [float(item.get("range_ratio", 0)) for item in controls]
    screenshot_path = await _capture_scenario_png(
        side,
        "01",
        "Initial",
        settle_seconds,
        capture_role="table_headers_initial",
        horizontal_scroll_position=scroll_state,
        scroll_position_id="initial",
        horizontal_scrollbar_present=bool(controls),
        full_page=False,
        wait_for_render=False,
    )
    return {
        "screenshots": {"initial": str(screenshot_path)},
        "scroll_positions": {"initial": scroll_state},
        "horizontal_scrollbar_present": bool(controls),
        "max_horizontal_scroll_range_ratio": max(ranges, default=0.0),
    }


async def _header_scroll_step(
    side: _Side,
    folder: Path,
    settle_seconds: float,
    fractions: list[float],
    initial_state: dict[str, Any],
) -> dict[str, Any]:
    screenshots: dict[str, str] = {}
    scroll_positions: dict[str, Any] = {}
    attempts: list[dict[str, Any]] = []
    reason = None
    previous_fractions = _header_actual_fractions(initial_state)
    reached_extreme_right = _header_is_at_extreme_right(previous_fractions)
    try:
        for fraction in fractions:
            position = await _set_table_scroll_position(side.page, fraction)
            current_fractions = _header_actual_fractions(position)
            changed = _header_scroll_position_changed(
                previous_fractions, current_fractions
            )
            attempt = {
                "requested_fraction": fraction,
                "actual_positions": position.get("actual_positions"),
                "position_changed": changed,
                "screenshot": None,
            }
            if changed is not False:
                await side.page.wait_for_timeout(250)
                label = f"horizontal_scroll_{len(screenshots) + 1:02d}"
                screenshot_path = await _capture_scenario_png(
                    side,
                    "01",
                    f"Scroll{len(screenshots) + 1:02d}",
                    settle_seconds,
                    capture_role="table_headers_scroll",
                    horizontal_scroll_position=position,
                    scroll_position_id=label,
                    horizontal_scrollbar_present=True,
                    full_page=False,
                )
                screenshots[label] = screenshot_path
                scroll_positions[label] = position
                attempt["screenshot"] = screenshot_path
            attempts.append(attempt)
            reached_extreme_right = _header_is_at_extreme_right(current_fractions)
            if changed is None:
                reason = "could not verify whether the horizontal position changed"
                break
            if reached_extreme_right:
                break
            if current_fractions is not None:
                previous_fractions = current_fractions
        if not reached_extreme_right and reason is None:
            reason = "horizontal scrollbar did not reach its extreme-right position"
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
    return {
        "screenshots": screenshots,
        "scroll_positions": scroll_positions,
        "scroll_attempts": attempts,
        "reached_extreme_right": reached_extreme_right,
        "reason": reason,
    }


def _choose_horizontal_scroll_strategy(
    initial_a: dict[str, Any], initial_b: dict[str, Any]
) -> str | None:
    if any(
        record.get("status") == STATUS_UNVERIFIED
        or not (record.get("screenshots") or {}).get("initial")
        for record in (initial_a, initial_b)
    ):
        return None
    if not (
        initial_a.get("horizontal_scrollbar_present")
        or initial_b.get("horizontal_scrollbar_present")
    ):
        return "NONE"
    return "INCREMENTAL"


def _header_scroll_fractions(record: dict[str, Any]) -> list[float]:
    initial_state = (record.get("scroll_positions") or {}).get("initial", {})
    initial_positions = _header_actual_fractions(initial_state)
    if initial_positions is None:
        return []
    if (
        max(initial_positions) - min(initial_positions)
        > HEADER_SCROLL_POSITION_TOLERANCE
    ):
        raise ValueError(
            "table scroll controls do not share an aligned initial position"
        )
    start_fraction = max(initial_positions)
    if _header_is_at_extreme_right(initial_positions):
        return []
    range_ratio = max(
        0.0, float(record.get("max_horizontal_scroll_range_ratio", 0.0))
    )
    remaining_range_ratio = range_ratio * (1.0 - start_fraction)
    step_count = max(
        1,
        math.ceil(
            remaining_range_ratio / HEADER_SCROLL_STEP_VIEWPORT_FRACTION
        ),
    )
    return [
        start_fraction + (1.0 - start_fraction) * step / step_count
        for step in range(1, step_count + 1)
    ]


def _header_actual_fractions(state: dict[str, Any]) -> tuple[float, ...] | None:
    controls = state.get("actual_positions") or state.get("controls") or []
    if not controls:
        return None
    fractions: list[float] = []
    for control in controls:
        value = control.get("actual_fraction")
        if not isinstance(value, (int, float)):
            return None
        fractions.append(float(value))
    return tuple(fractions)


def _header_scroll_position_changed(
    before: tuple[float, ...] | None, after: tuple[float, ...] | None
) -> bool | None:
    if before is None or after is None or len(before) != len(after):
        return None
    return any(
        abs(previous - current) > HEADER_SCROLL_POSITION_TOLERANCE
        for previous, current in zip(before, after)
    )


def _header_is_at_extreme_right(
    positions: tuple[float, ...] | None,
) -> bool:
    return bool(positions) and all(
        position >= 1.0 - HEADER_SCROLL_POSITION_TOLERANCE
        for position in positions
    )


async def _set_table_scroll_position(page: Any, fraction: float) -> dict[str, Any]:
    result = await page.evaluate(_TABLE_SCROLL_POSITION_SCRIPT, fraction)
    controls = result.get("controls", [])
    for control in controls:
        if control.get("kind") != "custom":
            continue
        track = control["track"]
        thumb = control["thumb"]
        start_x = thumb["x"] + thumb["width"] / 2
        center_y = thumb["y"] + thumb["height"] / 2
        travel = max(0, track["width"] - thumb["width"])
        target_x = track["x"] + thumb["width"] / 2 + travel * fraction
        await page.mouse.move(start_x, center_y)
        await page.mouse.down()
        await page.mouse.move(target_x, center_y, steps=10)
        await page.mouse.up()
    if controls:
        await page.wait_for_timeout(250)
        actual = await page.evaluate(_TABLE_SCROLL_POSITION_SCRIPT, None)
        actual_custom = [
            {
                "kind": "custom",
                "actual_fraction": (
                    (control["thumb"]["x"] - control["track"]["x"])
                    / max(1, control["track"]["width"] - control["thumb"]["width"])
                ),
            }
            for control in actual.get("controls", [])
            if control.get("kind") == "custom"
        ]
        result["actual_custom_positions"] = actual_custom
        custom_fractions = iter(
            item["actual_fraction"] for item in actual_custom
        )
        actual_positions = []
        for control in controls:
            actual_fraction = (
                next(custom_fractions, None)
                if control.get("kind") == "custom"
                else control.get("actual_fraction")
            )
            actual_positions.append(
                {
                    "kind": control.get("kind"),
                    "actual_fraction": actual_fraction,
                }
            )
        result["actual_positions"] = actual_positions
    result["requested_fraction"] = fraction
    return result


def _write_scenario_files(
    folder: Path,
    *,
    status: str,
    dom: dict[str, Any],
    execution: dict[str, Any],
    reason: str | None = None,
) -> None:
    _write_json(folder / "dom_extraction.json", dom)
    _write_json(folder / "scenario_execution.json", execution)
    _write_json(folder / "execution_status.json", {"status": status, "reason": reason})


def _combine_selection(rec_a: dict[str, Any], rec_b: dict[str, Any]) -> str:
    if rec_a.get("status") == STATUS_SKIPPED or rec_b.get("status") == STATUS_SKIPPED:
        return STATUS_SKIPPED
    if not (rec_a.get("restored") and rec_b.get("restored")):
        return STATUS_UNVERIFIED
    return selection_outcome(rec_a.get("verified"), rec_b.get("verified")) or STATUS_UNVERIFIED


def _drill_execution_status(record: dict[str, Any]) -> str:
    visuals = record.get("visuals") or []
    if not visuals:
        return record.get("status") or STATUS_SKIPPED
    statuses = [item.get("status") for item in visuals if isinstance(item, dict)]
    if statuses and all(status == STATUS_PASS for status in statuses) and all(
        item.get("restored") is True
        for item in visuals
        if isinstance(item, dict)
    ):
        return STATUS_PASS
    if STATUS_FAIL in statuses:
        return STATUS_FAIL
    if statuses and all(status == STATUS_SKIPPED for status in statuses):
        return STATUS_SKIPPED
    return STATUS_UNVERIFIED


async def _run_page_scenarios(
    side_a: _Side,
    side_b: _Side,
    destination: Path,
    settle_seconds: float,
    seed: int | None,
    job_id: str,
    operation_log_path: Path,
    last: str | None = None,
    only: str | None = None,
) -> dict[str, Any]:
    """Capture one page's scenario evidence after paired inventories and drill discovery.

    Read-only inventories run first so both dashboards share one filter plan. Each dashboard then runs its
    state-changing actions concurrently with the other, and results from both are collected before returning.
    """
    page_key = safe_path_part(side_a.page_name)
    selected = _selected_scenarios(only, last)
    dirs = {
        name: scenario_result_directory(
            destination, SCENARIO_ID_BY_NAME[name], side_a.page_name
        )
        for name in SCENARIO_DIRECTORIES
    }
    rng = random.Random(None if seed is None else f"{seed}:{side_a.page_name}")
    sides = (side_a, side_b)
    records: dict[str, dict[str, dict[str, Any]]] = {side.label: {} for side in sides}

    # Phase 1a: read-only filter inventory for both dashboards at once.
    side_a.filters, side_b.filters = await asyncio.gather(side_a.inventory(), side_b.inventory())
    side_a.baseline = copy.deepcopy(side_a.filters)
    side_b.baseline = copy.deepcopy(side_b.filters)
    filter_error = side_a.inventory_error or side_b.inventory_error
    common = common_filters(side_a.filters, side_b.filters)
    single_plan = plan_single_filter(common, rng)
    multi_plan = plan_multi_filter(
        common,
        rng,
        excluded_filter_key=single_plan["filter_key"] if single_plan else None,
    )

    # Phase 1b: read-only drill-candidate discovery, so both dashboards drill matching visuals.
    candidates: dict[str, list[dict[str, Any]]] = {}
    drill_pairs: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] = []
    if SCENARIO_DRILLDOWN in selected:
        found = await asyncio.gather(
            *(_discover_drill_candidates(side, f"cand{side.label[-1]}") for side in sides)
        )
        for side, items in zip(sides, found):
            candidates[side.label] = items
        drill_pairs = pair_visuals(candidates[side_a.label], candidates[side_b.label])

    # Planning gate: every discovered filter is listed with its plan outcome before any action runs.
    for side in sides:
        planned_names = {entry["key"] for entry in common}
        entries = []
        for item in side.filters:
            name = item.get("name", "")
            accounted = normalize_label(name) in planned_names
            if is_kpi_selection_filter(item):
                reason = "KPI selection filter, excluded from filter scenarios"
            elif accounted:
                reason = None
            else:
                reason = "no matching filter on the other dashboard"
            entries.append(
                {
                    "name": name,
                    "control_type": item.get("control_type"),
                    "option_inventory_status": item.get("option_inventory_status"),
                    "eligible": accounted,
                    "reason": reason,
                }
            )
        status = STATUS_UNVERIFIED if filter_error else "COMPLETED"
        _write_json(
            destination / "planning" / page_key / f"{side.label}_plan.json",
            {
                "dashboard": side.label,
                "page_name": side.page_name,
                "planning_status": status,
                "inventory_error": filter_error,
                "filters": entries,
                "single_filter_plan": single_plan,
                "multi_filter_plan": multi_plan,
                "drill_candidates": candidates.get(side.label, []),
            },
        )

    # Scenario 01 captures headers before any dashboard state is changed.
    if SCENARIO_TABLE_HEADERS in selected:
        initial_a, initial_b = await asyncio.gather(
            _guard_step(
                SCENARIO_TABLE_HEADERS,
                _header_initial_step(side_a, dirs[SCENARIO_TABLE_HEADERS], settle_seconds),
            ),
            _guard_step(
                SCENARIO_TABLE_HEADERS,
                _header_initial_step(side_b, dirs[SCENARIO_TABLE_HEADERS], settle_seconds),
            ),
        )
        records[side_a.label][SCENARIO_TABLE_HEADERS] = initial_a
        records[side_b.label][SCENARIO_TABLE_HEADERS] = initial_b
        scroll_strategy = _choose_horizontal_scroll_strategy(initial_a, initial_b)

        if scroll_strategy == "INCREMENTAL":
            async def extend_screenshots(side: _Side, record: dict[str, Any]) -> None:
                if not record.get("horizontal_scrollbar_present"):
                    record["reached_extreme_right"] = True
                    return
                try:
                    fractions = _header_scroll_fractions(record)
                    extension = await _header_scroll_step(
                        side,
                        dirs[SCENARIO_TABLE_HEADERS],
                        settle_seconds,
                        fractions,
                        record.get("scroll_positions", {}).get("initial", {}),
                    )
                    record["screenshots"].update(extension["screenshots"])
                    record.setdefault("scroll_positions", {}).update(
                        extension["scroll_positions"]
                    )
                    record["scroll_attempts"] = extension["scroll_attempts"]
                    record["reached_extreme_right"] = extension[
                        "reached_extreme_right"
                    ]
                    if extension.get("reason"):
                        record["status"] = STATUS_UNVERIFIED
                        record["reason"] = extension["reason"]
                except Exception as exc:
                    record["status"] = STATUS_UNVERIFIED
                    record["reason"] = f"{type(exc).__name__}: {exc}"

            await asyncio.gather(
                extend_screenshots(side_a, initial_a),
                extend_screenshots(side_b, initial_b),
            )
        elif scroll_strategy is None:
            for record in (initial_a, initial_b):
                record.setdefault("status", STATUS_UNVERIFIED)
        for record in (initial_a, initial_b):
            record["scroll_strategy"] = scroll_strategy

    filter_records = await _run_filter_scenarios(
        sides,
        selected,
        filter_error,
        single_plan,
        multi_plan,
        dirs,
        settle_seconds,
    )
    for side in sides:
        records[side.label].update(filter_records[side.label])

    if SCENARIO_DRILLDOWN in selected:
        if not drill_pairs:
            for side in sides:
                records[side.label][SCENARIO_DRILLDOWN] = {
                    "status": STATUS_SKIPPED,
                    "reason": "no drill-capable visual in DOM",
                }
        else:
            drill_results = await asyncio.gather(
                *(
                    _guard_step(
                        SCENARIO_DRILLDOWN,
                        _drill_step(
                            side,
                            drill_pairs,
                            dirs[SCENARIO_DRILLDOWN],
                            settle_seconds,
                        ),
                    )
                    for side in sides
                )
            )
            for side, result in zip(sides, drill_results):
                records[side.label][SCENARIO_DRILLDOWN] = result

    # Phase 3: compare A against B per scenario and write the evidence files.
    rec_a_all, rec_b_all = records[side_a.label], records[side_b.label]
    statuses: dict[str, str] = {}
    page_dom = {"page": side_a.page_name}

    if SCENARIO_SINGLE_FILTER in selected:
        ra, rb = rec_a_all[SCENARIO_SINGLE_FILTER], rec_b_all[SCENARIO_SINGLE_FILTER]
        status = _combine_selection(ra, rb)
        reason = ra.get("reason") or rb.get("reason")
        _write_scenario_files(
            dirs[SCENARIO_SINGLE_FILTER], status=status, dom={"plan": single_plan},
            execution={"plan": single_plan, "steps": [ra, rb]},
            reason=reason,
        )
        _write_side_scenario_evidence(
            side_a, "02", {"plan": single_plan, "step": ra}, status, reason
        )
        _write_side_scenario_evidence(
            side_b, "02", {"plan": single_plan, "step": rb}, status, reason
        )
        statuses[SCENARIO_SINGLE_FILTER] = status

    if SCENARIO_MULTIPLE_FILTER in selected:
        ra, rb = rec_a_all[SCENARIO_MULTIPLE_FILTER], rec_b_all[SCENARIO_MULTIPLE_FILTER]
        status = _combine_selection(ra, rb)
        reason = ra.get("reason") or rb.get("reason")
        _write_scenario_files(
            dirs[SCENARIO_MULTIPLE_FILTER], status=status, dom={"plan": multi_plan},
            execution={"plan": multi_plan, "steps": [ra, rb]},
            reason=reason,
        )
        _write_side_scenario_evidence(
            side_a, "03", {"plan": multi_plan, "step": ra}, status, reason
        )
        _write_side_scenario_evidence(
            side_b, "03", {"plan": multi_plan, "step": rb}, status, reason
        )
        statuses[SCENARIO_MULTIPLE_FILTER] = status

    if SCENARIO_DRILLDOWN in selected:
        ra, rb = rec_a_all[SCENARIO_DRILLDOWN], rec_b_all[SCENARIO_DRILLDOWN]
        side_statuses = [
            _drill_execution_status(ra),
            _drill_execution_status(rb),
        ]
        status = scenario_status(side_statuses)
        reason = ra.get("reason") or rb.get("reason")
        _write_scenario_files(
            dirs[SCENARIO_DRILLDOWN], status=status,
            dom={"candidates_A": candidates.get(side_a.label, []), "candidates_B": candidates.get(side_b.label, [])},
            execution={"dashboard_A": ra, "dashboard_B": rb},
            reason=reason,
        )
        _write_side_scenario_evidence(
            side_a,
            "04",
            {"visuals": ra.get("visuals", [])},
            side_statuses[0],
            reason,
        )
        _write_side_scenario_evidence(
            side_b,
            "04",
            {"visuals": rb.get("visuals", [])},
            side_statuses[1],
            reason,
        )
        statuses[SCENARIO_DRILLDOWN] = status

    if SCENARIO_TABLE_HEADERS in selected:
        ra, rb = rec_a_all[SCENARIO_TABLE_HEADERS], rec_b_all[SCENARIO_TABLE_HEADERS]
        strategy = ra.get("scroll_strategy")
        presence = {
            "dashboard_A": ra.get("horizontal_scrollbar_present"),
            "dashboard_B": rb.get("horizontal_scrollbar_present"),
        }
        reason = None
        if strategy == "NONE":
            status = STATUS_SKIPPED
            reason = "neither dashboard has a horizontal table scrollbar"
        elif strategy is None or any(
            record.get("status") == STATUS_UNVERIFIED for record in (ra, rb)
        ):
            status = STATUS_UNVERIFIED
            reason = ra.get("reason") or rb.get("reason") or "initial screenshot capture failed"
        elif not all(
            record.get("reached_extreme_right") is True for record in (ra, rb)
        ):
            status = STATUS_UNVERIFIED
            reason = "A dashboard did not capture evidence through the extreme-right table position."
        else:
            status = STATUS_PASS
        _write_json(
            dirs[SCENARIO_TABLE_HEADERS] / "scenario_execution.json",
            {
                "scenario_name": SCENARIO_TABLE_HEADERS,
                "scroll_strategy": strategy,
                "horizontal_scrollbar_presence": presence,
                "dashboard_A": ra,
                "dashboard_B": rb,
            },
        )
        _write_side_scenario_evidence(
            side_a,
            "01",
            {
                "scroll_strategy": strategy,
                "horizontal_scrollbar_present": ra.get("horizontal_scrollbar_present"),
                **ra,
            },
            STATUS_SKIPPED if strategy == "NONE" else (
                STATUS_PASS
                if ra.get("status") != STATUS_UNVERIFIED
                and ra.get("reached_extreme_right") is True
                else STATUS_UNVERIFIED
            ),
            reason,
        )
        _write_side_scenario_evidence(
            side_b,
            "01",
            {
                "scroll_strategy": strategy,
                "horizontal_scrollbar_present": rb.get("horizontal_scrollbar_present"),
                **rb,
            },
            STATUS_SKIPPED if strategy == "NONE" else (
                STATUS_PASS
                if rb.get("status") != STATUS_UNVERIFIED
                and rb.get("reached_extreme_right") is True
                else STATUS_UNVERIFIED
            ),
            reason,
        )
        _write_json(
            dirs[SCENARIO_TABLE_HEADERS] / "execution_status.json",
            {"status": status, "reason": reason},
        )
        statuses[SCENARIO_TABLE_HEADERS] = status

    return {"page": side_a.page_name, "statuses": statuses}


async def _open_side(
    browser: Any,
    context_options: dict[str, Any],
    url: str,
    page_name: str,
    label: str,
    diagnostics: Path,
    contexts: list[Any],
    reuse_existing_context: bool,
    *,
    page_number: int = 1,
    manifest_store: EvidenceManifestStore | None = None,
) -> _Side:
    if reuse_existing_context:
        # Connected to a signed-in browser (CDP): reuse its default context and close only our tab.
        page = await browser.contexts[0].new_page()
        await page.set_viewport_size(CAPTURE_VIEWPORT)
        contexts.append(page)
    else:
        context = await browser.new_context(**context_options)
        contexts.append(context)
        page = await context.new_page()
    side = _Side(
        label,
        url,
        page_name,
        page,
        diagnostics,
        page_number=page_number,
        manifest_store=manifest_store,
    )
    await side.reload()
    return side


async def _run_page_pair(
    browser: Any,
    *,
    url_a: str,
    url_b: str,
    page_name: str,
    destination: Path,
    storage_state: Path | None,
    settle_seconds: float,
    seed: int | None,
    page_semaphore: asyncio.Semaphore,
    job_id: str,
    operation_log_path: Path,
    reuse_existing_context: bool = False,
    last: str | None = None,
    only: str | None = None,
    page_number: int = 1,
    manifest_store: EvidenceManifestStore | None = None,
) -> dict[str, Any]:
    context_options: dict[str, Any] = {"viewport": CAPTURE_VIEWPORT}
    if storage_state is not None:
        context_options["storage_state"] = str(storage_state)
    contexts: list[Any] = []
    async with page_semaphore:
        write_operation_event(
            operation_log_path, job_id=job_id, operation="validation_page",
            event="started", outcome="in_progress", page=page_name,
        )
        try:
            side_a, side_b = await asyncio.gather(
                _open_side(
                    browser, context_options, url_a, page_name, "dashboard_A",
                    destination, contexts, reuse_existing_context,
                    page_number=page_number, manifest_store=manifest_store,
                ),
                _open_side(
                    browser, context_options, url_b, page_name, "dashboard_B",
                    destination, contexts, reuse_existing_context,
                    page_number=page_number, manifest_store=manifest_store,
                ),
            )
            result = await _run_page_scenarios(
                side_a, side_b, destination, settle_seconds, seed, job_id, operation_log_path,
                last, only,
            )
        finally:
            for context in contexts:
                await context.close()
        write_operation_event(
            operation_log_path, job_id=job_id, operation="validation_page",
            event="finished", outcome="success", page=page_name,
        )
        return result


async def _run_page_pair_isolated(
    browser: Any,
    *,
    page_name: str,
    destination: Path,
    job_id: str,
    operation_log_path: Path,
    last: str | None,
    only: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run one page; a failure marks that page's scenarios UNVERIFIED without stopping other pages."""
    try:
        return await _run_page_pair(
            browser,
            page_name=page_name,
            destination=destination,
            job_id=job_id,
            operation_log_path=operation_log_path,
            last=last,
            only=only,
            **kwargs,
        )
    except Exception as exc:
        reason = f"page failed: {type(exc).__name__}: {exc}"
        selected = _selected_scenarios(only, last)
        statuses = {}
        for name in selected:
            scenario_id = SCENARIO_ID_BY_NAME[name]
            statuses[name] = STATUS_UNVERIFIED
            result_directory = scenario_result_directory(
                destination, scenario_id, page_name
            )
            _write_json(
                result_directory / "execution_status.json",
                {"status": STATUS_UNVERIFIED, "reason": reason},
            )
            _write_json(
                result_directory / "scenario_execution.json",
                {
                    "scenario_id": scenario_id,
                    "scenario_name": SCENARIO_DETAILS[scenario_id][1],
                    "status": STATUS_UNVERIFIED,
                    "reason": reason,
                },
            )
            for dashboard_id in ("dashboard_A", "dashboard_B"):
                _write_json(
                    dashboard_execution_evidence_path(
                        destination, dashboard_id, scenario_id, page_name
                    ),
                    {
                        "dashboard_id": dashboard_id,
                        "scenario_id": scenario_id,
                        "scenario_name": SCENARIO_DETAILS[scenario_id][1],
                        "status": STATUS_UNVERIFIED,
                        "reason": reason,
                    },
                )
                _write_json(
                    dashboard_execution_evidence_path(
                        destination,
                        dashboard_id,
                        scenario_id,
                        page_name,
                        status=True,
                    ),
                    {"status": STATUS_UNVERIFIED, "reason": reason},
                )
        write_operation_event(
            operation_log_path, job_id=job_id, operation="validation_page",
            event="failed", outcome="unverified", page=page_name, error_type=type(exc).__name__,
        )
        return {"page": page_name, "statuses": statuses, "reason": reason}


async def discover_scenario_pages(
    *,
    url_a: str,
    storage_state: Path | None,
    headed: bool,
    destination: Path,
    job_id: str = "standalone",
    operation_log_path: Path | None = None,
    browser: Any | None = None,
) -> list[str]:
    """List Dashboard A report pages without capturing screenshots or DOM for every page."""
    log_path = operation_log_path or destination / "operation_timings.jsonl"
    context_options: dict[str, Any] = {"viewport": CAPTURE_VIEWPORT}
    if storage_state is not None:
        context_options["storage_state"] = str(storage_state)
    async with _capture_browser_session(
        browser, headed, job_id, log_path, "validation_page_discovery"
    ) as active_browser:
        reuse_default = bool(active_browser.contexts)
        context = (
            active_browser.contexts[0] if reuse_default
            else await active_browser.new_context(**context_options)
        )
        try:
            page = await context.new_page()
            if reuse_default:
                await page.set_viewport_size(CAPTURE_VIEWPORT)
            await _goto_report(page, url_a)
            await _wait_for_report_ready(page)
            await _wait_for_comparable_report_tabs(page)
            inventory = await _accessible_inventory(
                page,
                diagnostic_directory=destination,
                clear_selection_actions=False,
                inspect_filter_options=False,
            )
        finally:
            await (page if reuse_default else context).close()
    return _comparison_page_names(inventory["pages"] or ["Report"])


async def run_validation_scenarios(
    *,
    url_a: str,
    url_b: str,
    page_names: list[str],
    destination: Path,
    storage_state: Path | None,
    headed: bool,
    settle_seconds: float,
    browser: Any | None = None,
    job_id: str = "standalone",
    operation_log_path: Path | None = None,
    seed: int | None = None,
    last: str | None = None,
    only: str | None = None,
) -> dict[str, Any]:
    """Run four scenarios per page and write dashboard/scenario/page evidence.

    ``only`` runs just the named scenario; ``last`` stops each page after the
    named scenario (scenarios run in order). ``None`` for both runs all four.

    Filter options are read and clicked only from the rows rendered without
    scrolling; this run sets the flag for its own task context only, so the
    exhaustive mode keeps its scrolling behaviour.
    """
    _POPUP_SCROLLING_ENABLED.set(False)
    log_path = operation_log_path or destination / "operation_timings.jsonl"
    page_semaphore = asyncio.Semaphore(VALIDATION_MAX_PARALLEL_PAGES)
    manifest_store = EvidenceManifestStore(destination)
    manifest_store.initialize()
    async with _capture_browser_session(
        browser, headed, job_id, log_path, "validation_scenarios"
    ) as active_browser:
        reuse_existing_context = bool(active_browser.contexts)
        pages = await asyncio.gather(
            *(
                _run_page_pair_isolated(
                    active_browser,
                    url_a=url_a,
                    url_b=url_b,
                    page_name=page_name,
                    destination=destination,
                    storage_state=storage_state,
                    settle_seconds=settle_seconds,
                    seed=seed,
                    page_semaphore=page_semaphore,
                    job_id=job_id,
                    operation_log_path=log_path,
                    reuse_existing_context=reuse_existing_context,
                    last=last,
                    only=only,
                    page_number=page_number,
                    manifest_store=manifest_store,
                )
                for page_number, page_name in enumerate(page_names, start=1)
            )
        )
    summary = {"pages": pages}
    _write_json(destination / "validation_summary.json", summary)
    return summary
