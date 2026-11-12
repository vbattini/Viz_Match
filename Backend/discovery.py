"""Discover and verify the Snowflake and GCP reports from Power BI."""

import asyncio
import os
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from playwright.async_api import (
    Browser,
    TimeoutError as PlaywrightTimeoutError,
)
from playwright.async_api import async_playwright

from .browser import _goto_report, _read_report_title, _wait_for_report_ready

POWERBI_HOME_URL_SETTING = "POWERBI_HOME_URL"
DEFAULT_POWERBI_HOME_URL = "https://app.powerbi.com/home?experience=power-bi"
WORKSPACE_PREFIX_SETTING = "POWERBI_GCP_WORKSPACE_PREFIX"
REPORT_SUFFIX_SETTING = "POWERBI_GCP_REPORT_SUFFIX"
DEFAULT_WORKSPACE_PREFIX = "Tredence "
DEFAULT_REPORT_SUFFIX = " GCP"
WORKSPACE_LINK_WAIT_ATTEMPTS = 40
WORKSPACE_LINK_POLL_MS = 500
REPORT_LINK_WAIT_ATTEMPTS = 40
REPORT_LINK_POLL_MS = 500

_WORKSPACE_PATH = re.compile(r"/groups/([^/]+)(?:/|$)")
_REPORT_PATH = re.compile(r"/groups/([^/]+)/reports/([^/]+)(?:/|$)")
_WHITESPACE = re.compile(r"\s+")
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


def _exact_candidates(
    links: list[dict[str, str]], expected_name: str, asset_kind: str
) -> list[dict[str, str]]:
    expected = normalize_name(expected_name)
    matches_by_identity: dict[str, dict[str, str]] = {}
    for link in links:
        href = (link.get("href") or "").strip()
        if not href or normalize_name(link.get("text", "")) != expected:
            continue
        if "report" in asset_kind.casefold():
            report_ids = _report_ids(href)
            identity = report_ids[1] if report_ids else None
        else:
            identity = _workspace_id(href)
        matches_by_identity[identity or href] = link
    matches = list(matches_by_identity.values())
    if not matches:
        raise DiscoveryError(f"{asset_kind} {expected!r} was not found.")
    if len(matches) > 1:
        candidates = ", ".join(
            link["href"] for link in matches[:5]
        )
        raise DiscoveryError(
            f"Multiple exact {asset_kind.lower()} matches were found for "
            f"{expected!r}. No result was selected. Candidates: {candidates}"
        )
    return matches


async def _visible_links(page: Any) -> list[dict[str, str]]:
    links = await page.locator(
        'a[href], [role="link"][href]'
    ).evaluate_all(
        """elements => elements.flatMap(element => {
          const rect = element.getBoundingClientRect();
          const style = getComputedStyle(element);
          if (!rect.width || !rect.height || style.display === 'none' ||
              style.visibility === 'hidden' ||
              element.getAttribute('aria-hidden') === 'true') return [];
          const labels = new Set([
            element.getAttribute('aria-label'),
            element.title,
            element.innerText,
            element.textContent
          ].map(label => (label || '').replace(/\\s+/g, ' ').trim())
            .filter(Boolean));
          const href = element.href || element.getAttribute('href') || '';
          return href ? [...labels].map(text => ({text, href})) : [];
        })"""
    )
    return [
        {"text": str(link["text"]), "href": str(link["href"])}
        for link in links
        if isinstance(link, dict)
        and isinstance(link.get("text"), str)
        and isinstance(link.get("href"), str)
    ]


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


async def _check_access(page: Any, stage: str) -> None:
    current_url = str(page.url)
    hostname = (urlsplit(current_url).hostname or "").casefold()
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
    body = await page.locator("body").inner_text()
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
        ) from exc
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
    except Exception as exc:
        if callback:
            callback(step, "failed", str(exc))
        raise
    if callback:
        callback(step, "completed", None)
    return result


async def _find_workspace(page: Any, name: str, environment: str) -> dict[str, str]:
    await _navigate(
        page,
        _powerbi_home_url(),
        f"opening Power BI Home for {environment} discovery",
    )
    expected_name = normalize_name(name)
    try:
        await page.get_by_role(
            "menuitem", name="Workspaces", exact=True
        ).click(timeout=15_000)
        await page.locator('input[aria-label="Workspace search"]').fill(
            expected_name, timeout=15_000
        )
    except PlaywrightTimeoutError as exc:
        raise DiscoveryError(
            f"Could not open the Power BI Workspaces search for {environment}. "
            "Confirm the Home page loaded and your account has workspace access."
        ) from exc

    workspace_buttons: list[dict[str, Any]] = []
    exact_matches: list[dict[str, Any]] = []
    for attempt in range(WORKSPACE_LINK_WAIT_ATTEMPTS):
        workspace_buttons = await _visible_workspace_buttons(page)
        exact_matches = [
            button
            for button in workspace_buttons
            if expected_name in button["labels"]
        ]
        if exact_matches:
            break
        if attempt + 1 < WORKSPACE_LINK_WAIT_ATTEMPTS:
            await page.wait_for_timeout(WORKSPACE_LINK_POLL_MS)
    if not exact_matches:
        raise DiscoveryError(
            f"The {environment} workspace {expected_name!r} was not found in "
            "Power BI Workspaces. Check the exact name and your permissions."
        )
    if len(exact_matches) > 1:
        candidates = ", ".join(
            f"result {button['index'] + 1}: {expected_name!r}"
            for button in exact_matches[:5]
        )
        raise DiscoveryError(
            f"Multiple exact {environment} workspace matches were found for "
            f"{expected_name!r}. No result was selected. Candidates: {candidates}"
        )

    await page.locator(
        '[data-testid="workspace-item-btn"]'
    ).nth(exact_matches[0]["index"]).click()
    workspace_id = None
    for attempt in range(WORKSPACE_LINK_WAIT_ATTEMPTS):
        workspace_id = _workspace_id(str(page.url))
        if workspace_id is not None:
            break
        if attempt + 1 < WORKSPACE_LINK_WAIT_ATTEMPTS:
            await page.wait_for_timeout(WORKSPACE_LINK_POLL_MS)
    if workspace_id is None:
        raise DiscoveryError(
            f"Power BI did not open the selected {environment} workspace "
            f"{expected_name!r}. Retry and check your permissions."
        )
    await _check_access(page, f"opening the {environment} workspace")
    return {
        "workspace_id": workspace_id,
        "workspace_url": str(page.url),
    }


async def _find_report(
    page: Any,
    workspace_id: str,
    workspace_name: str,
    report_name: str,
    environment: str,
) -> dict[str, str]:
    expected_name = normalize_name(report_name)
    asset_kind = f"{environment} report in workspace {workspace_name!r}"
    try:
        await page.locator('input[placeholder="Filter by keyword"]').fill(
            expected_name, timeout=15_000
        )
    except PlaywrightTimeoutError as exc:
        raise DiscoveryError(
            f"Could not use the report filter in workspace "
            f"{normalize_name(workspace_name)!r} ({environment}). "
            "Confirm the workspace report list loaded and retry."
        ) from exc

    matches = None
    for attempt in range(REPORT_LINK_WAIT_ATTEMPTS):
        links = await _visible_links(page)
        report_links = [
            link
            for link in links
            if (ids := _report_ids(link["href"])) is not None
            and ids[0] == workspace_id
        ]
        try:
            matches = _exact_candidates(report_links, expected_name, asset_kind)
            break
        except DiscoveryError as exc:
            if "was not found" not in str(exc):
                raise
            if attempt + 1 == REPORT_LINK_WAIT_ATTEMPTS:
                raise DiscoveryError(
                    f"Report {expected_name!r} was not found in workspace "
                    f"{normalize_name(workspace_name)!r} ({environment}). "
                    "The expected report name must match exactly."
                ) from exc
            await page.wait_for_timeout(REPORT_LINK_POLL_MS)

    if matches is None:
        raise DiscoveryError(
            f"Report {expected_name!r} was not found in workspace "
            f"{normalize_name(workspace_name)!r} ({environment}). "
            "The expected report name must match exactly."
        )
    report_url = urljoin(str(page.url), matches[0]["href"])
    ids = _report_ids(report_url)
    if ids is None or ids[0] != workspace_id:
        raise DiscoveryError(
            f"Power BI returned a report outside the expected workspace "
            f"{normalize_name(workspace_name)!r}."
        )
    return {"report_id": ids[1], "report_url": report_url}


async def _open_and_verify(
    page: Any,
    workspace_id: str,
    report_id: str,
    report_url: str,
    workspace_name: str,
    report_name: str,
    environment: str,
) -> None:
    try:
        await _goto_report(page, report_url)
        await _check_access(page, f"opening the {environment} report")
        await _wait_for_report_ready(page)
    except PlaywrightTimeoutError as exc:
        raise DiscoveryError(
            f"Navigation timed out while opening {environment} report "
            f"{report_name!r}. Retry after confirming Power BI is responsive."
        ) from exc
    ids = _report_ids(str(page.url))
    if ids != (workspace_id, report_id):
        raise DiscoveryError(
            f"Power BI opened an unexpected report for workspace "
            f"{workspace_name!r}. The report pair was not accepted."
        )
    title = await _read_report_title(page)
    if title is None or normalize_name(title) != normalize_name(report_name):
        actual = title if title is not None else "unavailable"
        raise DiscoveryError(
            f"Opened {environment} report title {actual!r}, but expected the "
            f"exact title {normalize_name(report_name)!r} in workspace "
            f"{normalize_name(workspace_name)!r}."
        )


@dataclass(frozen=True)
class DashboardPairDiscovery:
    discovery: dict[str, Any]
    url_a: str
    url_b: str


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
        context = await active_browser.new_context(storage_state=str(storage_state))
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
            ) -> tuple[dict[str, str], dict[str, str]]:
                workspace = await _with_progress(
                    on_progress,
                    steps[0],
                    lambda: _find_workspace(page, target_workspace, environment),
                )
                report = await _with_progress(
                    on_progress,
                    steps[1],
                    lambda: _find_report(
                        page,
                        workspace["workspace_id"],
                        target_workspace,
                        target_report,
                        environment,
                    ),
                )
                await _with_progress(
                    on_progress,
                    steps[2],
                    lambda: _open_and_verify(
                        page,
                        workspace["workspace_id"],
                        report["report_id"],
                        report["report_url"],
                        target_workspace,
                        target_report,
                        environment,
                    ),
                )
                return workspace, report

            results = await asyncio.gather(
                discover_target(
                    page_a,
                    base_workspace,
                    base_report,
                    "Base/Snowflake",
                    (2, 3, 4),
                ),
                discover_target(
                    page_b,
                    gcp_workspace,
                    gcp_report,
                    "GCP",
                    (5, 6, 7),
                ),
                return_exceptions=True,
            )
            if isinstance(results[0], BaseException):
                raise results[0]
            if isinstance(results[1], BaseException):
                raise results[1]
            workspace_a, report_a = results[0]
            workspace_b, report_b = results[1]
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

            async def validate_pair() -> dict[str, Any]:
                if (
                    workspace_a["workspace_id"] == workspace_b["workspace_id"]
                    or report_a["report_id"] == report_b["report_id"]
                ):
                    raise DiscoveryError(
                        "The discovered Base and GCP targets are not a "
                        "distinct dashboard pair. Check the naming convention."
                    )
                return pair

            await _with_progress(on_progress, 8, validate_pair)
            return DashboardPairDiscovery(
                discovery=pair,
                url_a=report_a["report_url"],
                url_b=report_b["report_url"],
            )
        finally:
            await context.close()
