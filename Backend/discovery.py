"""Discover and verify the Snowflake and GCP reports from Power BI."""

import asyncio
import json
import os
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)
from playwright.async_api import async_playwright

from .browser import (
    CAPTURE_VIEWPORT,
    _read_report_title,
    _wait_for_report_ready,
    register_report_ui_target,
    unregister_report_ui_target,
)

POWERBI_HOME_URL_SETTING = "POWERBI_HOME_URL"
DEFAULT_POWERBI_HOME_URL = "https://app.powerbi.com/home?experience=power-bi"
WORKSPACE_PREFIX_SETTING = "POWERBI_GCP_WORKSPACE_PREFIX"
REPORT_SUFFIX_SETTING = "POWERBI_GCP_REPORT_SUFFIX"
DEFAULT_WORKSPACE_PREFIX = "Tredence "
DEFAULT_REPORT_SUFFIX = " GCP"
# Power BI can take a while to populate workspace results after navigation.
WORKSPACE_LINK_WAIT_ATTEMPTS = 40
WORKSPACE_LINK_POLL_MS = 500
REPORT_LINK_WAIT_ATTEMPTS = 40
REPORT_LINK_POLL_MS = 500

_WORKSPACE_PATH = re.compile(r"/groups/([^/]+)(?:/|$)")
_REPORT_PATH = re.compile(r"/groups/([^/]+)/reports/([^/]+)(?:/|$)")
_WHITESPACE = re.compile(r"\s+")
_WORKSPACE_CAPACITY_SUFFIX = re.compile(
    r"(?:\s*Premium capacity)+$", re.IGNORECASE
)
_LOGIN_HOSTS = {"login.microsoftonline.com", "login.live.com"}
_ACCESS_DENIED = re.compile(
    r"you don['’]t have access|you do not have access|access denied|"
    r"request access|not authorized",
    re.IGNORECASE,
)

ProgressCallback = Callable[[int, str, str | None], None]


class DiscoveryError(RuntimeError):
    """A clear, actionable Power BI discovery failure."""


@asynccontextmanager
async def _discovery_browser(shared_browser: Browser | None):
    if shared_browser is not None:
        yield shared_browser
        return
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            await browser.close()


def normalize_name(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


def normalize_workspace_label(value: str) -> str:
    """Remove Power BI capacity metadata appended to workspace labels."""
    normalized = normalize_name(value)
    return normalize_name(_WORKSPACE_CAPACITY_SUFFIX.sub("", normalized))


def _powerbi_home_url(home_url: str | None = None) -> str:
    configured_url = (
        home_url
        if home_url is not None
        else os.environ.get(POWERBI_HOME_URL_SETTING, DEFAULT_POWERBI_HOME_URL)
    )
    parsed = urlsplit(configured_url.strip())
    if parsed.scheme != "https" or parsed.hostname != "app.powerbi.com":
        raise DiscoveryError(
            "POWERBI_HOME_URL must be an HTTPS Power BI Home URL on "
            "app.powerbi.com."
        )
    if not parsed.path.rstrip("/").endswith("/home"):
        raise DiscoveryError(
            "POWERBI_HOME_URL must point to the Power BI Home page."
        )
    return configured_url.strip()


def _powerbi_path(href: str) -> str | None:
    parsed = urlsplit(href)
    if parsed.scheme and parsed.scheme.casefold() != "https":
        return None
    if parsed.hostname and parsed.hostname.casefold() != "app.powerbi.com":
        return None
    if parsed.netloc and not parsed.hostname:
        return None
    return parsed.path


def derive_gcp_names(
    workspace_name: str,
    report_name: str,
    workspace_prefix: str | None = None,
    report_suffix: str | None = None,
) -> tuple[str, str]:
    prefix = (
        os.environ.get(WORKSPACE_PREFIX_SETTING, DEFAULT_WORKSPACE_PREFIX)
        if workspace_prefix is None
        else workspace_prefix
    )
    suffix = (
        os.environ.get(REPORT_SUFFIX_SETTING, DEFAULT_REPORT_SUFFIX)
        if report_suffix is None
        else report_suffix
    )
    return (
        normalize_name(f"{prefix} {normalize_name(workspace_name)}"),
        normalize_name(f"{normalize_name(report_name)} {suffix}"),
    )


def _report_name_matches(
    actual_name: str | None, expected_name: str, environment: str
) -> bool:
    if actual_name is None:
        return False
    actual = normalize_name(actual_name)
    expected = normalize_name(expected_name)
    return actual == expected


def _workspace_id(href: str) -> str | None:
    path = _powerbi_path(href)
    match = _WORKSPACE_PATH.search(path or "")
    if not match or match.group(1).casefold() == "me":
        return None
    return match.group(1)


def _report_ids(href: str) -> tuple[str, str] | None:
    path = _powerbi_path(href)
    match = _REPORT_PATH.search(path or "")
    if not match:
        return None
    return match.group(1), match.group(2)


async def _visible_named_links(page: Any) -> list[dict[str, Any]]:
    links = page.locator('a, [role="link"]')
    visible_links: list[dict[str, Any]] = []
    for index in range(await links.count()):
        link = links.nth(index)
        if hasattr(link, "is_visible") and not await link.is_visible():
            continue
        label = normalize_name(await link.inner_text())
        if label:
            visible_links.append({"index": index, "text": label})
    return visible_links


async def _click_named_link(page: Any, expected_name: str) -> None:
    matches = [
        link
        for link in await _visible_named_links(page)
        if link["text"] == normalize_name(expected_name)
    ]
    if len(matches) != 1:
        raise DiscoveryError(
            f"Expected one visible Power BI link named "
            f"{normalize_name(expected_name)!r}, found {len(matches)}."
        )
    try:
        await page.locator('a, [role="link"]').nth(
            matches[0]["index"]
        ).click(timeout=120_000)
    except PlaywrightError as exc:
        raise DiscoveryError(
            f"Could not click the Power BI link "
            f"{normalize_name(expected_name)!r}."
        )


async def _visible_workspace_buttons(page: Any) -> list[dict[str, Any]]:
    buttons = await page.locator(
        '[data-testid="workspace-item-btn"]'
    ).evaluate_all(
        """elements => elements.flatMap((element, index) => {
          const rect = element.getBoundingClientRect();
          const style = getComputedStyle(element);
          if (!rect.width || !rect.height || style.display === 'none' ||
              style.visibility === 'hidden' ||
              element.getAttribute('aria-hidden') === 'true') return [];
          const labels = [...new Set([
            element.getAttribute('aria-label'),
            element.title,
            element.innerText,
            element.textContent
          ].map(label => (label || '').replace(/\\s+/g, ' ').trim())
            .filter(Boolean))];
          return labels.length ? [{index, labels}] : [];
        })"""
    )
    return [
        {
            "index": int(button["index"]),
            "labels": [str(label) for label in button["labels"]],
        }
        for button in buttons
        if isinstance(button, dict)
        and isinstance(button.get("index"), int)
        and isinstance(button.get("labels"), list)
    ]


async def _visible_workspace_rows(page: Any) -> list[dict[str, Any]]:
    rows = await page.locator('[role="row"]').evaluate_all(
        """elements => elements.flatMap((element, index) => {
          const rect = element.getBoundingClientRect();
          const style = getComputedStyle(element);
          if (!rect.width || !rect.height || style.display === 'none' ||
              style.visibility === 'hidden' ||
              element.getAttribute('aria-hidden') === 'true') return [];
          const nameCell = element.querySelector(
            '[data-testid*="fluentListCell.name" i], ' +
            '[data-testid*="workspace-name" i], ' +
            '[data-testid*="item-name" i]'
          );
          const firstLine = (element.innerText || '')
            .split(/\\r?\\n/).map(value => value.trim()).find(Boolean) || '';
          const labels = [...new Set([
            element.getAttribute('aria-label'),
            nameCell?.getAttribute('aria-label'),
            nameCell?.innerText,
            firstLine
          ].map(label => (label || '').replace(/\\s+/g, ' ').trim())
            .filter(Boolean))];
          const link = element.querySelector('a[href], [role="link"][href]');
          return labels.length ? [{
            index,
            labels,
            href: link?.href || link?.getAttribute('href') || ''
          }] : [];
        })"""
    )
    return [
        {
            "index": int(row["index"]),
            "labels": [str(label) for label in row["labels"]],
            "href": str(row.get("href") or ""),
        }
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("index"), int)
        and isinstance(row.get("labels"), list)
    ]


async def _visible_report_rows(page: Any) -> list[dict[str, Any]]:
    rows = await page.locator('[role="row"]').evaluate_all(
        """elements => elements.flatMap((element, index) => {
          const rect = element.getBoundingClientRect();
          const style = getComputedStyle(element);
          if (!rect.width || !rect.height || style.display === 'none' ||
              style.visibility === 'hidden' ||
              element.getAttribute('aria-hidden') === 'true') return [];
          const nameCell = element.querySelector(
            '[data-testid*="fluentListCell.name" i], ' +
            '[data-testid*="report-name" i], ' +
            '[data-testid*="item-name" i]'
          );
          const firstLine = (element.innerText || '')
            .split(/\\r?\\n/).map(value => value.trim()).find(Boolean) || '';
          const labels = [...new Set([
            element.getAttribute('aria-label'),
            nameCell?.getAttribute('aria-label'),
            nameCell?.innerText,
            firstLine
          ].map(label => (label || '').replace(/\\s+/g, ' ').trim())
            .filter(Boolean))];
          return labels.length ? [{index, labels}] : [];
        })"""
    )
    return [
        {
            "index": int(row["index"]),
            "labels": [str(label) for label in row["labels"]],
        }
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("labels"), list)
        and isinstance(row.get("index"), int)
    ]


async def _visible_workspace_links(page: Any) -> list[dict[str, Any]]:
    return await _visible_named_links(page)


async def _click_workspace_button_in_current_dom(
    page: Any, expected_name: str
) -> bool:
    buttons = await _visible_workspace_buttons(page)
    matches = [
        button
        for button in buttons
        if any(
            normalize_workspace_label(label) == normalize_name(expected_name)
            for label in button["labels"]
        )
    ]
    if len(matches) != 1:
        return False
    try:
        await page.locator('[data-testid="workspace-item-btn"]').nth(
            matches[0]["index"]
        ).click(timeout=10_000)
    except PlaywrightError:
        return False
    return True


async def _click_workspace_button(page: Any, expected_name: str) -> Any | None:
    """Keep resolving the exact workspace as Power BI refreshes its result list."""
    last_error: PlaywrightTimeoutError | None = None
    attempts = WORKSPACE_LINK_WAIT_ATTEMPTS // 2
    for attempt in range(attempts):
        buttons = await _visible_workspace_buttons(page)
        matching = [
            button
            for button in buttons
            if any(
                normalize_workspace_label(label) == expected_name
                for label in button["labels"]
            )
        ]
        if matching:
            index = matching[0]["index"]
            if await _click_workspace_button_in_current_dom(page, expected_name):
                return page.locator(
                    '[data-testid="workspace-item-btn"]'
                ).nth(index)
            button = page.locator(
                '[data-testid="workspace-item-btn"]'
            ).nth(index)
            try:
                await button.click(timeout=1_500)
                return button
            except PlaywrightTimeoutError as exc:
                last_error = exc
                if attempt == 0:
                    try:
                        await button.click(timeout=1_500, force=True)
                        return button
                    except PlaywrightTimeoutError as force_exc:
                        last_error = force_exc

        rows = await _visible_workspace_rows(page)
        matching_rows = [
            row
            for row in rows
            if any(
                normalize_workspace_label(
                    re.sub(r"^Workspace\s+", "", label, flags=re.IGNORECASE)
                )
                == expected_name
                for label in row["labels"]
            )
        ]
        if len(matching_rows) == 1:
            row = page.locator('[role="row"]').nth(matching_rows[0]["index"])
            try:
                await row.click(timeout=1_500)
                return row
            except PlaywrightTimeoutError as exc:
                last_error = exc
                try:
                    await row.click(timeout=1_500, force=True)
                    return row
                except PlaywrightTimeoutError as force_exc:
                    last_error = force_exc

        links = await _visible_workspace_links(page)
        matching_links = [
            link
            for link in links
            if normalize_workspace_label(link.get("text", "")) == expected_name
        ]
        if len(matching_links) == 1:
            try:
                await _click_named_link(page, matching_links[0]["text"])
                return None
            except PlaywrightTimeoutError as exc:
                last_error = exc

        if attempt + 1 < attempts:
            await page.wait_for_timeout(WORKSPACE_LINK_POLL_MS)

    raise DiscoveryError(
        f"Power BI showed workspace {expected_name!r}, but its result could not "
        f"be clicked in {attempts * WORKSPACE_LINK_POLL_MS // 1000} "
        f"seconds. Confirm the result is still visible and enabled."
    ) from last_error


async def _dismiss_workspace_overlay(page: Any) -> None:
    for selector in (
        ".cdk-overlay-backdrop:visible",
        ".mat-drawer-backdrop:visible",
    ):
        backdrop = page.locator(selector)
        if await backdrop.count():
            await backdrop.last.click(timeout=10_000)


async def _check_access(page: Any, stage: str) -> None:
    current_url = str(page.url)
    parsed_url = urlsplit(current_url)
    hostname = (parsed_url.hostname or "").casefold()
    if hostname in _LOGIN_HOSTS:
        raise DiscoveryError(
            "Power BI authentication expired or is missing. Refresh the "
            "Playwright storage-state file and retry."
        )
    if hostname != "app.powerbi.com":
        raise DiscoveryError(
            f"Power BI redirected outside app.powerbi.com during {stage}. "
            "Confirm authentication and retry."
        )
    if "singlesignon" in parsed_url.path.casefold():
        raise DiscoveryError(
            "Power BI authentication expired or is missing. Refresh the "
            "Playwright storage-state file and retry."
        )
    body = await page.locator("body").inner_text()
    if re.search(r"enter your work or school email", body, re.IGNORECASE):
        raise DiscoveryError(
            "Power BI authentication expired or is missing. Refresh the "
            "Playwright storage-state file and retry."
        )
    if _ACCESS_DENIED.search(body):
        raise DiscoveryError(
            f"Power BI denied access during {stage}. Confirm your account has "
            "permission to both workspaces and reports."
        )


async def _navigate(page: Any, url: str, stage: str) -> None:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    except PlaywrightTimeoutError as exc:
        raise DiscoveryError(
            f"Navigation timed out during {stage}. Confirm Power BI is "
            "reachable and retry."
        )
    await _check_access(page, stage)


async def _with_progress(
    callback: ProgressCallback | None,
    step: int,
    operation: Callable[[], Any],
) -> Any:
    if callback:
        callback(step, "in_progress", None)
    try:
        result = await operation()
    except (DiscoveryError, PlaywrightError) as exc:
        if callback:
            callback(step, "failed", str(exc))
        raise
    if callback:
        callback(step, "completed", None)
    return result


async def _open_workspace_search(page: Any, expected_name: str) -> None:
    last_error: PlaywrightTimeoutError | None = None
    opened = False
    controls: list[Any] = []
    if hasattr(page, "get_by_test_id"):
        controls.append(page.get_by_test_id("navbar-label-item-workspaces"))
    for role in ("menuitem", "button", "link"):
        controls.append(page.get_by_role(role, name="Workspaces", exact=True))
    if not hasattr(page, "get_by_test_id") and hasattr(page, "get_by_text"):
        controls.append(page.get_by_text("Workspaces", exact=True))
    for control in controls:
        try:
            if hasattr(control, "wait_for"):
                await control.wait_for(state="visible", timeout=5_000)
            await control.click(timeout=10_000)
            opened = True
            break
        except PlaywrightTimeoutError as exc:
            last_error = exc
    if not opened:
        raise last_error or PlaywrightTimeoutError(
            "The Workspaces navigation control was not found."
        )

    search_selectors = (
        'input[aria-label="Workspace search"]',
        'input[aria-label*="workspace" i]',
        'input[placeholder*="workspace" i]',
    )
    for selector in search_selectors:
        search = page.locator(selector)
        if hasattr(search, "count") and not await search.count():
            continue
        if hasattr(search, "is_visible") and not await search.is_visible():
            continue
        try:
            await search.fill(expected_name, timeout=10_000)
            return
        except PlaywrightTimeoutError as exc:
            last_error = exc
    raise last_error or PlaywrightTimeoutError(
        "The workspace search field was not found after opening Workspaces."
    )


def _report_search_input(page: Any) -> Any:
    """Return Power BI's report-list search input, preferring its stable test id."""
    if hasattr(page, "get_by_test_id"):
        return page.get_by_test_id("keyword-search-filter").get_by_test_id(
            "tri-search-box"
        )
    return page.locator('input[placeholder="Filter by keyword"]')


async def _find_workspace(page: Any, name: str, environment: str) -> dict[str, str]:
    await _navigate(
        page,
        _powerbi_home_url(),
        f"opening Power BI Home for {environment} discovery",
    )
    expected_name = normalize_name(name)
    try:
        await _open_workspace_search(page, expected_name)
    except PlaywrightTimeoutError as exc:
        raise DiscoveryError(
            f"Could not open the Power BI Workspaces search for {environment}. "
            f"Current page: {urlsplit(str(page.url)).path or '/'}; "
            f"navigation/search failure: {exc}. Confirm the Home page loaded "
            "and your account has workspace access."
        ) from exc

    workspace_buttons: list[dict[str, Any]] = []
    workspace_rows: list[dict[str, Any]] = []
    workspace_link_labels: list[dict[str, Any]] = []
    exact_matches: list[dict[str, Any]] = []
    exact_row_matches: list[dict[str, Any]] = []
    exact_link_matches: list[dict[str, str]] = []
    workspace_button = None
    selected_by_button = False
    for attempt in range(WORKSPACE_LINK_WAIT_ATTEMPTS):
        workspace_buttons = await _visible_workspace_buttons(page)
        exact_matches = [
            button
            for button in workspace_buttons
            if any(
                normalize_workspace_label(label) == expected_name
                for label in button["labels"]
            )
        ]
        if len(exact_matches) == 1 and await _click_workspace_button_in_current_dom(
            page, expected_name
        ):
            workspace_button = page.locator(
                '[data-testid="workspace-item-btn"]'
            ).nth(exact_matches[0]["index"])
            selected_by_button = True
            break
        workspace_rows = await _visible_workspace_rows(page)
        exact_row_matches = [
            row
            for row in workspace_rows
            if any(
                normalize_workspace_label(
                    re.sub(r"^Workspace\s+", "", label, flags=re.IGNORECASE)
                ) == expected_name
                for label in row["labels"]
            )
        ]
        if exact_matches or exact_row_matches:
            break
        workspace_link_labels = await _visible_named_links(page)
        link_matches = [
            link
            for link in workspace_link_labels
            if normalize_workspace_label(link["text"]) == expected_name
        ]
        exact_link_matches = []
        seen_link_indexes: set[int] = set()
        for link in link_matches:
            if link["index"] not in seen_link_indexes:
                seen_link_indexes.add(link["index"])
                exact_link_matches.append(link)
        if attempt + 1 < WORKSPACE_LINK_WAIT_ATTEMPTS:
            await page.wait_for_timeout(WORKSPACE_LINK_POLL_MS)
    if not exact_matches and not exact_row_matches and not exact_link_matches:
        await _capture_workspace_navigation_failure(
            page, expected_name, environment, None
        )
        visible_names = sorted(
            {
                normalize_name(label)
                for button in workspace_buttons
                for label in button["labels"]
                if normalize_name(label)
            }
            | {
                normalize_name(
                    re.sub(r"^Workspace\s+", "", label, flags=re.IGNORECASE)
                )
                for row in workspace_rows
                for label in row["labels"]
                if normalize_name(label)
            }
            | {
                normalize_name(link["text"])
                for link in workspace_link_labels
                if normalize_name(link["text"])
            }
        )
        hint = (
            f" Visible workspace names: {', '.join(visible_names[:8])}."
            if visible_names
            else ""
        )
        raise DiscoveryError(
            f"The {environment} workspace {expected_name!r} was not found in "
            "Power BI Workspaces. Check the exact name, tenant, and your "
            f"permissions.{hint}"
        )
    if (
        len(exact_matches) > 1
        or len(exact_row_matches) > 1
        or len(exact_link_matches) > 1
    ):
        candidates = ", ".join(
            f"result {button['index'] + 1}: {expected_name!r}"
            for button in exact_matches[:5]
        )
        if exact_row_matches:
            row_candidates = ", ".join(
                f"row {row['index'] + 1}: {row['labels'][0]!r}"
                for row in exact_row_matches[:5]
            )
            candidates = f"{candidates}, {row_candidates}".strip(", ")
        if exact_link_matches:
            link_candidates = ", ".join(
                link["text"] for link in exact_link_matches[:5]
            )
            candidates = f"{candidates}, {link_candidates}".strip(", ")
        raise DiscoveryError(
            f"Multiple exact {environment} workspace matches were found for "
            f"{expected_name!r}. No result was selected. Candidates: {candidates}"
        )

    selected_by_row = False
    if not selected_by_button and exact_matches:
        workspace_button = page.locator(
            '[data-testid="workspace-item-btn"]'
        ).nth(exact_matches[0]["index"])
        selected_by_button = True
    elif not selected_by_button and exact_row_matches:
        workspace_row = page.locator('[role="row"]').nth(
            exact_row_matches[0]["index"]
        )
        selected_by_row = True

    if selected_by_button:
        if workspace_button is None:
            workspace_button = await _click_workspace_button(
                page, expected_name
            )
    elif selected_by_row:
        try:
            await workspace_row.click(timeout=10_000)
        except PlaywrightTimeoutError:
            await workspace_row.click(timeout=10_000, force=True)
    else:
        await _click_named_link(page, exact_link_matches[0]["text"])

    report_filter = _report_search_input(page)
    retried_workspace_click = False
    for attempt in range(WORKSPACE_LINK_WAIT_ATTEMPTS):
        # Home can expose a similarly named filter while the workspace click
        # is still being processed. Do not treat that Home filter as the
        # workspace report list.
        in_workspace = _workspace_id(str(page.url)) is not None
        filter_visible = await report_filter.count() > 0
        if filter_visible and hasattr(report_filter, "is_visible"):
            filter_visible = await report_filter.is_visible()
        if in_workspace and filter_visible:
            break
        if attempt == 10 and selected_by_button and not retried_workspace_click:
            current_buttons = await _visible_workspace_buttons(page)
            if any(
                any(
                    normalize_workspace_label(label) == expected_name
                    for label in button["labels"]
                )
                for button in current_buttons
            ):
                workspace_button = await _click_workspace_button(
                    page, expected_name
                )
            retried_workspace_click = True
        if attempt + 1 < WORKSPACE_LINK_WAIT_ATTEMPTS:
            await page.wait_for_timeout(WORKSPACE_LINK_POLL_MS)
    else:
        await _capture_workspace_navigation_failure(
            page, expected_name, environment, workspace_button if selected_by_button else None
        )
        raise DiscoveryError(
            f"Power BI did not open the selected {environment} workspace "
            f"{expected_name!r}. The workspace report list did not load. "
            "Retry and check your permissions."
        )
    if hasattr(page, "get_by_test_id"):
        active_workspace = page.get_by_test_id("workspace-name-header")
    else:
        workspace_section = page.locator(
            '[data-testid="new-section-workspace"]'
        )
        if (
            await workspace_section.count()
            and hasattr(workspace_section, "get_by_text")
        ):
            active_workspace = workspace_section.get_by_text(
                expected_name, exact=True
            )
        else:
            active_workspace = page.get_by_text(expected_name, exact=True)
    if hasattr(active_workspace, "wait_for"):
        try:
            await active_workspace.wait_for(
                state="visible", timeout=10_000
            )
        except PlaywrightTimeoutError as exc:
            raise DiscoveryError(
                f"Power BI opened the workspace results, but the active "
                f"workspace name {expected_name!r} could not be verified."
            ) from exc
    elif not await active_workspace.count():
        raise DiscoveryError(
            f"Power BI opened the workspace results, but the active workspace "
            f"name {expected_name!r} could not be verified."
        )
    if hasattr(active_workspace, "is_visible") and not await active_workspace.is_visible():
        raise DiscoveryError(
            f"Power BI opened a workspace, but {expected_name!r} was not "
            "visible as the active workspace."
        )
    await _check_access(page, f"opening the {environment} workspace")
    workspace_id = _workspace_id(str(page.url)) or ""
    return {
        "workspace_id": workspace_id,
        "workspace_name": expected_name,
        "workspace_url": str(page.url),
    }


async def _capture_workspace_navigation_failure(
    page: Any,
    workspace_name: str,
    environment: str,
    workspace_button: Any | None,
) -> None:
    debug_dir = Path(os.environ.get("POWERBI_DEBUG_DIR", "discovery_debug"))
    debug_dir.mkdir(parents=True, exist_ok=True)
    prefix = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        f"{normalize_name(environment)}_{normalize_name(workspace_name)}",
    )
    details: dict[str, Any] = {
        "url": str(page.url),
        "workspace": normalize_name(workspace_name),
        "environment": normalize_name(environment),
    }
    if workspace_button is not None and hasattr(workspace_button, "evaluate"):
        details["workspace_result"] = await workspace_button.evaluate(
            """element => ({
                tag: element.tagName,
                outerHTML: element.outerHTML,
                parentHTML: element.parentElement ? element.parentElement.outerHTML : null,
                disabled: element.disabled,
                ariaDisabled: element.getAttribute('aria-disabled')
            })"""
        )
    if hasattr(page, "context"):
        details["page_count"] = len(page.context.pages)
    (debug_dir / f"{prefix}_navigation.json").write_text(
        json.dumps(details, indent=2, default=str), encoding="utf-8"
    )
    if hasattr(page, "content"):
        (debug_dir / f"{prefix}_dom.html").write_text(
            await page.content(), encoding="utf-8"
        )
    if hasattr(page, "screenshot"):
        await page.screenshot(
            path=str(debug_dir / f"{prefix}_navigation.png"), full_page=True
        )


async def _find_report(
    page: Any,
    workspace_name: str,
    report_name: str,
    environment: str,
) -> dict[str, str]:
    expected_name = normalize_name(report_name)
    asset_kind = f"{environment} report in workspace {workspace_name!r}"
    try:
        report_search = _report_search_input(page)
        if hasattr(report_search, "wait_for"):
            await report_search.wait_for(state="visible", timeout=10_000)
        await report_search.fill(expected_name, timeout=10_000)
    except PlaywrightTimeoutError as exc:
        raise DiscoveryError(
            f"Could not use the report filter in workspace "
            f"{normalize_name(workspace_name)!r} ({environment}). "
            "Confirm the workspace report list loaded and retry."
        ) from exc

    selected_name: str | None = None
    visible_report_names: set[str] = set()
    for attempt in range(REPORT_LINK_WAIT_ATTEMPTS):
        links = await _visible_named_links(page)
        visible_report_names.update(
            link["text"] for link in links if link["text"]
        )
        exact_links = [
            link for link in links if link["text"] == expected_name
        ]
        if len(exact_links) > 1:
            raise DiscoveryError(
                f"Multiple exact visible {environment} reports named "
                f"{expected_name!r} were found in workspace "
                f"{normalize_name(workspace_name)!r}. No result was selected."
            )
        if exact_links:
            selected_name = expected_name
            break
        visible_rows = await _visible_report_rows(page)
        for row in visible_rows:
            visible_report_names.update(
                normalize_name(label)
                for label in row["labels"]
                if normalize_name(label)
            )
        row_matches = [
            (row["index"], label)
            for row in visible_rows
            for label in row["labels"]
            if normalize_name(label) == expected_name
        ]
        if len(row_matches) > 1:
            raise DiscoveryError(
                f"Multiple exact visible {environment} reports named "
                f"{expected_name!r} were found in workspace "
                f"{normalize_name(workspace_name)!r}. No result was selected."
            )
        if row_matches:
            selected_name = expected_name
            break
        else:
            if attempt + 1 == REPORT_LINK_WAIT_ATTEMPTS:
                raise DiscoveryError(
                    f"Report {expected_name!r} was not found in workspace "
                    f"{normalize_name(workspace_name)!r} ({environment}). "
                    "The expected report name must match exactly."
                    + (
                        f" Visible report names: "
                        f"{', '.join(sorted(visible_report_names)[:10])}."
                        if visible_report_names
                        else " No report rows or links were visible after the "
                        "workspace filter was applied."
                    )
                )
            await page.wait_for_timeout(REPORT_LINK_POLL_MS)

    if selected_name is None:
        raise DiscoveryError(
            f"Report {expected_name!r} was not found in workspace "
            f"{normalize_name(workspace_name)!r} ({environment}). "
            "The expected report name must match exactly."
        )
    report = {"report_display_name": selected_name}
    if not exact_links and row_matches:
        report["report_row_index"] = str(row_matches[0][0])
    return report


async def _click_report_by_name(
    page: Any, report_name: str, report_row_index: int | None = None
) -> None:
    links = page.locator('a, [role="link"]')
    matches = []
    for index in range(await links.count()):
        link = links.nth(index)
        if (
            hasattr(link, "is_visible")
            and not await link.is_visible()
        ):
            continue
        if normalize_name(await link.inner_text()) == normalize_name(report_name):
            matches.append(link)
    if not matches and report_row_index is not None:
        rows = page.locator('[role="row"]')
        row = rows.nth(report_row_index)
        if hasattr(row, "is_visible") and not await row.is_visible():
            raise DiscoveryError(
                f"The selected Power BI report row {report_name!r} is no "
                "longer visible."
            )
        try:
            await row.click(timeout=120_000)
            return
        except PlaywrightError as exc:
            raise DiscoveryError(
                f"Could not click the selected Power BI report row "
                f"{report_name!r}."
            ) from exc
    if len(matches) != 1:
        raise DiscoveryError(
            f"The selected Power BI report {report_name!r} was not available "
            "as one visible report link after filtering."
        )
    try:
        await matches[0].click(timeout=120_000)
    except PlaywrightError as exc:
        raise DiscoveryError(
            f"Could not click the selected Power BI report {report_name!r}."
        ) from exc


async def _open_and_verify(
    page: Any,
    report_name: str,
    environment: str,
    report_link_name: str | None = None,
    report_row_index: int | None = None,
) -> str:
    try:
        await _click_report_by_name(
            page,
            report_link_name or report_name,
            report_row_index,
        )
        if hasattr(page, "wait_for_load_state"):
            await page.wait_for_load_state(
                "domcontentloaded", timeout=120_000
            )
        await _check_access(page, f"opening the {environment} report")
        await _wait_for_report_ready(page)
    except (PlaywrightTimeoutError, DiscoveryError) as exc:
        if isinstance(exc, DiscoveryError):
            raise
        raise DiscoveryError(
            f"Timed out while clicking or opening {environment} report "
            f"{report_name!r}. Retry after confirming Power BI is responsive."
        ) from exc
    title = await _read_report_title(page)
    if not _report_name_matches(title, report_name, environment):
        actual = title if title is not None else "unavailable"
        raise DiscoveryError(
            f"Opened {environment} report title {actual!r}, but expected the "
            f"exact title {normalize_name(report_name)!r}."
        )
    return str(page.url)


async def navigate_to_report_by_ui(
    page: Any, workspace_name: str, report_name: str
) -> str:
    await _find_workspace(page, workspace_name, "comparison capture")
    report = await _find_report(
        page, workspace_name, report_name, "comparison capture"
    )
    return await _open_and_verify(
        page,
        report_name,
        "comparison capture",
        report.get("report_display_name"),
        (
            int(report["report_row_index"])
            if report.get("report_row_index") is not None
            else None
        ),
    )


@dataclass(frozen=True)
class DashboardPairDiscovery:
    discovery: dict[str, Any]
    url_a: str
    url_b: str
    page_a: Any | None = None
    page_b: Any | None = None
    _context: Any | None = None

    async def close(self) -> None:
        unregister_report_ui_target(self.url_a)
        unregister_report_ui_target(self.url_b)
        if self._context is not None:
            await self._context.close()


async def discover_dashboard_pair(
    workspace_name: str,
    semantic_model: str,
    report_name: str,
    storage_state: Path,
    on_progress: ProgressCallback | None = None,
    browser: Browser | None = None,
) -> DashboardPairDiscovery:
    base_workspace = normalize_name(workspace_name)
    base_report = normalize_name(report_name)
    gcp_workspace, gcp_report = derive_gcp_names(base_workspace, base_report)

    async with _discovery_browser(browser) as active_browser:
        context = await active_browser.new_context(
            storage_state=str(storage_state),
            permissions=["clipboard-read", "clipboard-write"],
            viewport=CAPTURE_VIEWPORT,
            accept_downloads=True,
        )
        try:
            page_a, page_b = await asyncio.gather(
                context.new_page(), context.new_page()
            )

            async def discover_target(
                page: Any,
                target_workspace: str,
                target_report: str,
                environment: str,
                steps: tuple[int, int, int],
                progress_callback: ProgressCallback | None,
            ) -> tuple[dict[str, str], dict[str, str]]:
                workspace = await _with_progress(
                    progress_callback,
                    steps[0],
                    lambda: _find_workspace(page, target_workspace, environment),
                )
                report = await _with_progress(
                    progress_callback,
                    steps[1],
                    lambda: _find_report(
                        page,
                        target_workspace,
                        target_report,
                        environment,
                    ),
                )
                report_url = await _with_progress(
                    progress_callback,
                    steps[2],
                    lambda: _open_and_verify(
                        page,
                        target_report,
                        environment,
                        report.get("report_display_name"),
                        (
                            int(report["report_row_index"])
                            if report.get("report_row_index") is not None
                            else None
                        ),
                    ),
                )
                report["report_url"] = report_url
                report["report_id"] = (
                    _report_ids(report_url) or ("", "")
                )[1]
                return workspace, report

            results = await asyncio.gather(
                discover_target(
                    page_a,
                    base_workspace,
                    base_report,
                    "Base/Snowflake",
                    (2, 3, 4),
                    on_progress,
                ),
                discover_target(
                    page_b,
                    gcp_workspace,
                    gcp_report,
                    "GCP",
                    (5, 6, 7),
                    on_progress,
                ),
                return_exceptions=True,
            )
            if isinstance(results[0], BaseException):
                raise results[0]
            if isinstance(results[1], BaseException):
                raise results[1]
            workspace_a, report_a = results[0]
            workspace_b, report_b = results[1]
            register_report_ui_target(
                report_a["report_url"], base_workspace, base_report
            )
            register_report_ui_target(
                report_b["report_url"], gcp_workspace, gcp_report
            )
            pair = {
                "inputs": {
                    "workspace_name": base_workspace,
                    "semantic_model": semantic_model,
                    "report_name": base_report,
                },
                "dashboard_a": {
                    "environment": "Snowflake",
                    "workspace_name": base_workspace,
                    "report_name": base_report,
                    "workspace_id": workspace_a["workspace_id"],
                    "report_id": report_a["report_id"],
                    "report_url": report_a["report_url"],
                    "status": "discovered",
                },
                "dashboard_b": {
                    "environment": "GCP",
                    "workspace_name": gcp_workspace,
                    "report_name": gcp_report,
                    "workspace_id": workspace_b["workspace_id"],
                    "report_id": report_b["report_id"],
                    "report_url": report_b["report_url"],
                    "status": "discovered",
                },
                "discovery_status": "success",
            }

            return DashboardPairDiscovery(
                discovery=pair,
                url_a=report_a["report_url"],
                url_b=report_b["report_url"],
                page_a=page_a,
                page_b=page_b,
                _context=context,
            )
        except BaseException:
            await context.close()
            raise
