import asyncio
import copy
import csv
import hashlib
import io
import json
import logging
import math
import re
import time
from itertools import product
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Iterator
from uuid import uuid4

from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from .agents import (
    TruncatedAgentResponseError,
    _align_shared_plan,
    _is_aggregate_value,
    extract_screenshot,
    extract_screenshots,
    detect_visual_types_from_screenshot,
    plan_from_dom,
)
from .config import positive_int_setting
from .models import DashboardState, FilterCombination, FilterSelection
from .timing import measure_stage, write_operation_event
from .visual_filter_comparison import classify_visual_type

MAX_DOM_CONTROLS = 500
MAX_RENDER_WAIT_SECONDS = 15.0
CLASS_ONLY_RENDER_TIMEOUT_MS = 30_000
UNRENDERED_CHART_TIMEOUT_MS = 30_000
MAX_PAGE_TAB_WAIT_SECONDS = 60.0
REPORT_READY_TIMEOUT_MS = 300_000
REPORT_STABLE_SECONDS = 4.0
REPORT_POLL_MS = 500
SLICER_CONTROL_ACTION_TIMEOUT_MS = 15_000
CLEAR_SELECTION_VISIBLE_TIMEOUT_MS = 2_000
EXPORT_DIALOG_BUTTON_TIMEOUT_MS = 5_000
EXPORT_DOWNLOAD_TIMEOUT_MS = 60_000
MORE_OPTIONS_WAIT_ATTEMPTS = 20
VISUAL_SELECTOR = "div.visualContainer, [data-visual-id]"
_VISUAL_LAYOUT_SCRIPT = """() => {
  const main = document.querySelector('[role="main"]') || document.body;
  const visible = element => {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 &&
      style.display !== 'none' &&
      style.visibility !== 'hidden' &&
      element.getAttribute('aria-hidden') !== 'true';
  };
  const found = [...new Set(main.querySelectorAll(
    'div.visualContainer, [data-visual-id]'
  ))].filter(visible);
  const candidates = found.filter(node =>
    !found.some(parent => parent !== node && parent.contains(node))
  );
  const root = [...main.querySelectorAll('.reportCanvas')].find(visible) || main;
  const rootBounds = root.getBoundingClientRect();
  const typeRules = [
    [/\\bclustered\\s+bar\\b/i, 'Clustered Bar Chart'],
    [/\\bstacked\\s+bar\\b/i, 'Stacked Bar Chart'],
    [/\\bbar\\b/i, 'Bar Chart'],
    [/\\bclustered\\s+column\\b/i, 'Clustered Column Chart'],
    [/\\bstacked\\s+column\\b/i, 'Stacked Column Chart'],
    [/\\bcolumn\\b/i, 'Column Chart'],
    [/\\bcombo\\b|\\bline and (stacked|clustered) column\\b/i, 'Combo Chart'],
    [/\\bdonut\\b/i, 'Donut Chart'],
    [/\\bpie\\b/i, 'Pie Chart'],
    [/\\btreemap\\b/i, 'Treemap'],
    [/\\bscatter\\b/i, 'Scatter Chart'],
    [/\\bwaterfall\\b/i, 'Waterfall'],
    [/\\bfunnel\\b/i, 'Funnel'],
    [/\\bgauge\\b/i, 'Gauge'],
    [/\\barea\\b/i, 'Area Chart'],
    [/\\bline\\b/i, 'Line Chart'],
    [/\\bmatrix\\b/i, 'Matrix'],
    [/\\btable\\b/i, 'Table'],
    [/\\bkpi\\b/i, 'KPI'],
    [/\\bcard\\b/i, 'Card'],
    [/\\bmap\\b/i, 'Map']
  ];
  const valuesForType = node => {
    const values = [
      node.getAttribute('data-visual-type'),
      node.getAttribute('data-visual-kind'),
      typeof node.className === 'string'
        ? node.className
        : node.className?.baseVal || ''
    ];
    for (const child of node.querySelectorAll(
      '[data-visual-type], [data-visual-kind], [aria-label]'
    )) {
      values.push(
        child.getAttribute('data-visual-type'),
        child.getAttribute('data-visual-kind'),
        child.getAttribute('aria-label')
      );
    }
    if (node.querySelector('[role="treegrid"]')) values.push('matrix');
    else if (node.querySelector('[role="grid"], table')) values.push('table');
    return values.filter(Boolean);
  };
  const detectType = hints => {
    for (const [pattern, label] of typeRules) {
      if (hints.some(hint => pattern.test(hint))) return label;
    }
    return null;
  };
  const output = [];
  for (const node of candidates) {
    const className = typeof node.className === 'string'
      ? node.className
      : node.className?.baseVal || '';
    const visualKind = [
      node.getAttribute('data-visual-type'),
      node.getAttribute('data-visual-kind'),
      className
    ].filter(Boolean).join(' ');
    const isFilterVisual =
      /\\b(slicer|filter|bookmark)\\b/i.test(visualKind) ||
      !!node.querySelector(
        '[role="combobox"], [role="listbox"], select, ' +
        '[aria-haspopup="listbox"]'
      );
    if (isFilterVisual) continue;

    const bounds = node.getBoundingClientRect();
    if (rootBounds.width <= 0 || rootBounds.height <= 0) continue;
    const title = node.querySelector(
      '[role="heading"], h1, h2, h3, h4, [class*="visualTitle"]'
    );
    const titleText = title?.innerText?.replace(/\\s+/g, ' ').trim() || '';
    output.push({
      visual_id: node.getAttribute('data-visual-id') || node.id || '',
      visual_name: titleText,
      visual_type: detectType(valuesForType(node)),
      x: bounds.left - rootBounds.left,
      y: bounds.top - rootBounds.top,
      width: bounds.width,
      height: bounds.height
    });
  }
  return output;
}"""
_TITLE_LABEL_SCRIPT = """filterNames => {
  const main = document.querySelector('[role="main"]') || document.body;
  const visible = element => {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 &&
      style.display !== 'none' &&
      style.visibility !== 'hidden' &&
      element.getAttribute('aria-hidden') !== 'true';
  };
  const root = [...main.querySelectorAll('.reportCanvas')].find(visible) || main;
  const normalize = value => (value || '').replace(/\\s+/g, ' ').trim();
  const bounds = element => {
    const rect = element.getBoundingClientRect();
    const rootRect = root.getBoundingClientRect();
    return {
      bounding_box: {
        x: rect.left - rootRect.left,
        y: rect.top - rootRect.top,
        width: rect.width,
        height: rect.height
      },
      normalized_bounding_box: {
        x: (rect.left - rootRect.left) / rootRect.width,
        y: (rect.top - rootRect.top) / rootRect.height,
        width: rect.width / rootRect.width,
        height: rect.height / rootRect.height
      }
    };
  };
  const typeRules = [
    [/\\bclustered\\s+bar\\b/i, 'Clustered Bar Chart'],
    [/\\bstacked\\s+bar\\b/i, 'Stacked Bar Chart'],
    [/\\bbar\\b/i, 'Bar Chart'],
    [/\\bclustered\\s+column\\b/i, 'Clustered Column Chart'],
    [/\\bstacked\\s+column\\b/i, 'Stacked Column Chart'],
    [/\\bcolumn\\b/i, 'Column Chart'],
    [/\\bcombo\\b|\\bline and (stacked|clustered) column\\b/i, 'Combo Chart'],
    [/\\bdonut\\b/i, 'Donut Chart'],
    [/\\bpie\\b/i, 'Pie Chart'],
    [/\\btreemap\\b/i, 'Treemap'],
    [/\\bscatter\\b/i, 'Scatter Chart'],
    [/\\bwaterfall\\b/i, 'Waterfall'],
    [/\\bfunnel\\b/i, 'Funnel'],
    [/\\bgauge\\b/i, 'Gauge'],
    [/\\barea\\b/i, 'Area Chart'],
    [/\\bline\\b/i, 'Line Chart'],
    [/\\bmatrix\\b/i, 'Matrix'],
    [/\\btable\\b/i, 'Table'],
    [/\\bkpi\\b/i, 'KPI'],
    [/\\bcard\\b/i, 'Card'],
    [/\\bmap\\b/i, 'Map']
  ];
  const foundVisualNodes = [...new Set(main.querySelectorAll(
    'div.visualContainer, [data-visual-id]'
  ))].filter(visible);
  const visualNodes = foundVisualNodes.filter(node =>
    !foundVisualNodes.some(parent =>
      parent !== node && parent.contains(node)
    )
  );
  const visualType = node => {
    const className = typeof node.className === 'string'
      ? node.className
      : node.className?.baseVal || '';
    const hints = [
      node.getAttribute('data-visual-type'),
      node.getAttribute('data-visual-kind'),
      className
    ];
    for (const child of node.querySelectorAll(
      '[data-visual-type], [data-visual-kind], [aria-label]'
    )) {
      hints.push(
        child.getAttribute('data-visual-type'),
        child.getAttribute('data-visual-kind'),
        child.getAttribute('aria-label')
      );
    }
    if (node.querySelector('[role="treegrid"]')) hints.push('matrix');
    else if (node.querySelector('[role="grid"], table')) hints.push('table');
    for (const [pattern, label] of typeRules) {
      if (hints.some(hint => hint && pattern.test(hint))) return label;
    }
    return null;
  };
  const items = [];
  const add = (node, text, category, visualId = null) => {
    text = normalize(text);
    if (!text || !visible(node)) return;
    const geometry = bounds(node);
    if (category === 'metadata_label' && items.some(item =>
      item.category === category &&
      item.text === text &&
      Math.abs(item.bounding_box.x - geometry.bounding_box.x) < 3 &&
      Math.abs(item.bounding_box.y - geometry.bounding_box.y) < 3
    )) return;
    items.push({
      text,
      category,
      associated_visual_dom_id: visualId,
      ...geometry
    });
  };
  const filterKeys = filterNames.map(name =>
    normalize(name).toLocaleLowerCase()
  );
  const isFilterVisual = node => {
    const className = typeof node.className === 'string'
      ? node.className
      : node.className?.baseVal || '';
    return /\\b(slicer|filter|bookmark)\\b/i.test(
      [
        node.getAttribute('data-visual-type'),
        node.getAttribute('data-visual-kind'),
        className
      ].filter(Boolean).join(' ')
    ) || !!node.querySelector(
      '[role="combobox"], [role="listbox"], select, ' +
      '[aria-haspopup="listbox"]'
    );
  };
  for (const node of visualNodes) {
    const visualId = node.getAttribute('data-visual-id') || node.id || null;
    if (isFilterVisual(node)) continue;
    const type = visualType(node);
    const title = node.querySelector(
      '[role="heading"], h1, h2, h3, h4, [class*="visualTitle"]'
    );
    if (title) {
      const text = normalize(title.innerText);
      if (!filterKeys.includes(text.toLocaleLowerCase())) {
        const category = type === 'Table' ? 'table_title'
          : type === 'Matrix' ? 'matrix_title'
          : type === 'KPI' || type === 'Card' ? 'kpi_title'
          : 'chart_title';
        add(title, text, category, visualId);
      }
    }
    if (type === 'Table' || type === 'Matrix') {
      for (const header of node.querySelectorAll(
        '[role="columnheader"], th'
      )) {
        add(header, header.innerText, 'column_header', visualId);
      }
    }
  }
  const headings = [...main.querySelectorAll(
    'h1, h2, h3, h4, [role="heading"]'
  )].filter(node =>
    visible(node) &&
    !node.closest('div.visualContainer, [data-visual-id]') &&
    !node.closest('[role="tab"], [role="button"], nav, [class*="navigator" i]')
  ).map(node => ({
    node,
    text: normalize(node.innerText),
    rect: node.getBoundingClientRect()
  })).filter(item =>
    item.text &&
    !filterKeys.includes(item.text.toLocaleLowerCase()) &&
    !/^(home|pages|filters?|file|help)$/i.test(item.text) &&
    !/\\b(refresh|refreshed|last updated|data updated)\\b/i.test(item.text)
  ).sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);
  headings.forEach((item, index) => {
    add(item.node, item.text, index === 0 ? 'dashboard_title' : 'section_title');
  });
  for (const node of main.querySelectorAll('p, span, h1, h2, h3, h4')) {
    if (!visible(node)) continue;
    if (node.closest('table, [role="grid"], [role="row"], td, th')) continue;
    const text = [...node.childNodes]
      .filter(child => child.nodeType === Node.TEXT_NODE)
      .map(child => child.textContent || '')
      .join(' ');
    for (const line of text.split(/\\n+/)) {
      let label = normalize(line);
      if (
        /\\b(refresh|refreshed|last updated|data updated)\\b/i.test(label)
      ) {
        const sibling = node.nextElementSibling;
        if (sibling && visible(sibling)) {
          const siblingText = normalize([...sibling.childNodes]
            .filter(child => child.nodeType === Node.TEXT_NODE)
            .map(child => child.textContent || '')
            .join(' '));
          if (
            siblingText.length <= 80 &&
            /\\b\\d{1,2}[/-]\\d{1,2}[/-]\\d{2,4}\\b|\\b\\d{4}-\\d{1,2}-\\d{1,2}\\b|\\b\\d{1,2}:\\d{2}(?::\\d{2})?\\b/i.test(siblingText) &&
            !/\\b(visual|row|column|total)\\b/i.test(siblingText)
          ) {
            label = normalize(label + ' ' + siblingText);
          }
        }
        if (label.length > 180) continue;
        add(node, label, 'metadata_label');
      }
    }
  }
  const filterBounds = [];
  const candidates = [...main.querySelectorAll(
    '[role="combobox"], select, [role="group"], [class*="slicer"], ' +
    '[aria-label], [title]'
  )].filter(visible);
  for (const name of filterNames) {
    const key = normalize(name).toLocaleLowerCase();
    let best = null;
    let bestArea = Infinity;
    for (const node of candidates) {
      const heading = node.querySelector(
        '[role="heading"], h1, h2, h3, h4, [class*="slicerTitle"]'
      );
      const labels = [
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        heading?.innerText
      ].map(normalize).filter(Boolean);
      if (!labels.some(label => {
        const value = label.toLocaleLowerCase();
        return value === key || value.startsWith(key + ' ');
      })) continue;
      const rect = node.getBoundingClientRect();
      const area = rect.width * rect.height;
      if (area < bestArea) {
        best = node;
        bestArea = area;
      }
    }
    if (best) {
      filterBounds.push({name, ...bounds(best)});
    }
  }
  return {items, filter_bounds: filterBounds};
}"""
DATA_VISUAL_TYPES = frozenset(
    {
        "chart",
        "bar chart",
        "line chart",
        "column chart",
        "table",
        "matrix",
        "kpi",
        "card",
        "grid",
    }
)
CAPTURE_VIEWPORT = {"width": 1920, "height": 1080}
CAPTURE_DEVICE_SCALE_FACTOR = 1
MAX_PARALLEL_STATE_CAPTURES = positive_int_setting(
    "PBI_MAX_PARALLEL_STATE_CAPTURES", 10
)
MAX_PARALLEL_PAGE_CAPTURES = positive_int_setting(
    "PBI_MAX_PARALLEL_PAGE_CAPTURES", 10
)
MAX_PARALLEL_PAGES_PER_DASHBOARD = MAX_PARALLEL_PAGE_CAPTURES
EXCLUDED_REPORT_PAGES = {"home", "view"}
SINGLE_REPORT_PAGE_NAME = "Report"
POWER_BI_APPLICATION_TAB_NAMES = {"file"}
COMBOBOX_METADATA_SCRIPT = """elements => elements.map((el, index) => {
  const group = el.closest('[role="group"]');
  const heading = group?.querySelector(
    'h1, h2, h3, h4, [role="heading"]'
  );
  const selectorName = el.getAttribute('aria-label') ||
    el.getAttribute('title') || '';
  const groupText = group?.innerText ||
    group?.getAttribute('aria-label') || '';
  const groupTitle = groupText.split(/\\n+/).map(value => value.trim())
    .find(value => value &&
      value.toLocaleLowerCase() !== selectorName.toLocaleLowerCase());
  const name = heading?.innerText?.trim() || groupTitle || selectorName ||
    el.innerText?.trim() || '';
  return {index, name, selectorName: selectorName || name};
})"""
logger = logging.getLogger(__name__)


class UnavailableFilterOptionError(RuntimeError):
    pass


class FilterSelectionInspectionError(RuntimeError):
    pass


def _safe_path_component(value: str, max_length: int) -> str:
    safe = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in value
    ).strip("_")
    safe = safe or "item"
    if len(safe) <= max_length:
        return safe
    suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{safe[: max_length - len(suffix) - 1]}_{suffix}"


def _comparison_page_names(page_names: list[str]) -> list[str]:
    if not all(isinstance(name, str) and name.strip() for name in page_names):
        raise ValueError("Power BI report contains an invalid page name")
    included = _comparable_report_page_names(page_names)
    if not included:
        logger.info(
            "No page tabs remain after excluding Home, View, and Power BI "
            "application tabs; treating the report as a single page. "
            "Discovered tabs: %r",
            page_names,
        )
        return [SINGLE_REPORT_PAGE_NAME]
    return included


def _comparable_report_page_names(page_names: list[str]) -> list[str]:
    return [
        name
        for name in page_names
        if name.strip().casefold()
        not in EXCLUDED_REPORT_PAGES | POWER_BI_APPLICATION_TAB_NAMES
    ]


async def _move_pointer_off_dashboard(page: Page) -> None:
    await page.mouse.move(-10, -10)
    # A focused Clear selections button keeps its tooltip visible.
    for frame in getattr(page, "frames", None) or []:
        try:
            await frame.evaluate(
                "() => document.activeElement && document.activeElement.blur "
                "&& document.activeElement.blur()"
            )
        except Exception:
            continue
    await page.wait_for_timeout(350)


def _normalize_option_text(value: str) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    midpoint = len(text) // 2
    if (
        len(text) % 2 == 0
        and text[:midpoint]
        and text[:midpoint] == text[midpoint:]
    ):
        return text[:midpoint]
    return text


async def _accessible_option_texts(options: Any) -> list[str]:
    values = await options.evaluate_all(
        """elements => elements.map(element => {
          const searchControl = element.matches(
            'input[type="search"], input[role="searchbox"], ' +
            'textarea[role="searchbox"], [role="searchbox"], ' +
            'input[aria-label*="search" i], input[placeholder*="search" i], ' +
            'textarea[aria-label*="search" i], ' +
            'textarea[placeholder*="search" i]'
          ) || element.closest('[role="searchbox"]');
          if (searchControl) return null;
          const choiceSelector =
            '[role="option"], [role="radio"], [role="checkbox"], ' +
            '[role="treeitem"], [role="menuitem"], ' +
            'input[type="radio"], input[type="checkbox"]';
          if (element.querySelectorAll(choiceSelector).length > 1) {
            return null;
          }
          const label = (element.getAttribute('aria-label') ||
            element.getAttribute('title') ||
            [...(element.labels || [])].map(label => label.innerText.trim())
              .filter(Boolean).join(' ') ||
            element.closest('[role="option"]')?.innerText ||
            element.closest('label')?.innerText ||
            element.innerText || element.textContent ||
            element.parentElement?.innerText || '')
            .replace(/\\s+/g, ' ').trim();
          const isSelectable = element.matches(choiceSelector) ||
            !!element.closest(choiceSelector) ||
            element.hasAttribute('aria-selected') ||
            element.hasAttribute('aria-checked') ||
            element.hasAttribute('aria-pressed');
          if (label.toLocaleLowerCase() === 'search' && !isSelectable) {
            return null;
          }
          const midpoint = label.length / 2;
          return label.length % 2 === 0 &&
            label.slice(0, midpoint) === label.slice(midpoint)
            ? label.slice(0, midpoint) : label;
        }).filter(Boolean)"""
    )
    return list(
        dict.fromkeys(
            _normalize_option_text(value)
            for value in values
            if isinstance(value, str) and _normalize_option_text(value)
        )
    )


async def _choice_filter_options(
    frame: Any,
    selector: str,
    filter_item: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    controls = frame.locator(selector)
    items = await controls.evaluate_all(
        """elements => elements.map((element, index) => {
          const visible = node => {
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 &&
              style.visibility !== 'hidden' && style.display !== 'none';
          };
          const container = element.closest(
            'div.visualContainer, [data-visual-id], [role="group"]'
          );
          if (!container || !visible(element)) return null;
          const heading = container.querySelector(
            '[role="heading"], h1, h2, h3, h4, [class*="visualTitle"]'
          );
          const name = (heading?.innerText?.trim() ||
            container.getAttribute('aria-label') || '')
            .split(/\\n+/).map(value => value.trim()).find(Boolean) || '';
          const label = element.getAttribute('aria-label') ||
            element.getAttribute('title') ||
            [...(element.labels || [])].map(item => item.innerText.trim())
              .filter(Boolean).join(' ') ||
            element.innerText?.trim() ||
            element.closest('label')?.innerText?.trim() ||
            element.parentElement?.innerText?.trim() || '';
          return {
            index,
            name,
            label: label.replace(/\\s+/g, ' ').trim(),
            selected: element.tagName === 'INPUT'
              ? element.checked
              : element.getAttribute('aria-checked') === 'true',
            visualId: container.getAttribute('data-visual-id') || ''
          };
        }).filter(item => item && item.name && item.label)"""
    )
    if filter_item is None:
        return [
            {
                **item,
                "name": str(item.get("name", "")).splitlines()[0].strip(),
            }
            for item in items
            if isinstance(item, dict)
            and str(item.get("name", "")).splitlines()
        ]
    selector_names = {
        str(value).strip().casefold()
        for value in (
            filter_item.get("name"),
            filter_item.get("selector_name"),
        )
        if isinstance(value, str) and value.strip()
    }
    visual_id = filter_item.get("visual_id")
    return [
        {
            **item,
            "name": str(item.get("name", "")).splitlines()[0].strip(),
        }
        for item in items
        if isinstance(item, dict)
        and str(item.get("name", "")).splitlines()
        and str(item.get("name", "")).splitlines()[0].strip().casefold()
        in selector_names
        and (not visual_id or item.get("visualId") == visual_id)
    ]


async def _checkbox_filter_options(
    frame: Any, filter_item: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    return await _choice_filter_options(
        frame,
        '[role="checkbox"], input[type="checkbox"]',
        filter_item,
    )


async def _radio_filter_options(
    frame: Any, filter_item: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    return await _choice_filter_options(
        frame,
        '[role="radio"], input[type="radio"]',
        filter_item,
    )


async def _bookmark_button_groups(frame: Any) -> list[dict[str, Any]]:
    groups = frame.locator('.visualContainerGroup[role="group"]')
    result = []
    for group_index in range(min(await groups.count(), 100)):
        group = groups.nth(group_index)
        accessible_name = (await group.get_attribute("aria-label") or "").strip()
        count_match = re.fullmatch(r"(\d+)\s+items?", accessible_name, re.I)
        if not count_match or not await group.is_visible():
            continue

        visuals = group.locator(
            '.visualContainer[role="link"][aria-label^="Bookmark"]'
        )
        expected_count = int(count_match.group(1))
        if await visuals.count() != expected_count:
            continue

        options = []
        for visual_index in range(expected_count):
            visual = visuals.nth(visual_index)
            labels = visual.locator(".ui-role-button-text")
            if not await visual.is_visible() or await labels.count() != 1:
                options = []
                break
            label = (await labels.first.inner_text()).strip()
            if not label:
                options = []
                break
            style = await visual.evaluate(
                """element => {
                  const shape = element.querySelector('path.ui-role-button-fill');
                  const text = element.querySelector('.ui-role-button-text');
                  return {
                    fill: shape?.getAttribute('fill') || '',
                    textColor: text ? getComputedStyle(text).color : ''
                  };
                }"""
            )
            options.append(
                {
                    "label": label,
                    "fill": str(style.get("fill") or "").casefold(),
                    "text_color": str(style.get("textColor") or "").casefold(),
                    "locator": visual,
                }
            )
        if len(options) < 2:
            continue
        option_values = [option["label"] for option in options]
        if len({_normalized_control_text(value) for value in option_values}) != len(
            option_values
        ):
            continue

        fill_counts: dict[str, int] = {}
        color_counts: dict[str, int] = {}
        for option in options:
            fill_counts[option["fill"]] = fill_counts.get(option["fill"], 0) + 1
            color_counts[option["text_color"]] = (
                color_counts.get(option["text_color"], 0) + 1
            )
        common_fill = max(fill_counts, key=fill_counts.get)
        common_color = max(color_counts, key=color_counts.get)
        selected = [
            option
            for option in options
            if option["fill"] != common_fill
        ]
        if len(selected) != 1:
            selected = [
                option
                for option in options
                if option["text_color"] != common_color
            ]
        if len(selected) != 1:
            selected = [
                option
                for option in options
                if option["fill"] in {"#000", "#000000", "black"}
                and option["text_color"]
                in {"#fff", "#ffffff", "white", "rgb(255, 255, 255)"}
            ]
        if len(selected) != 1:
            raise RuntimeError(
                "Cannot determine the active option in bookmark-button group "
                f"{option_values!r}"
            )
        selected_label = selected[0]["label"]
        for option in options:
            option["selected"] = option["label"] == selected_label

        result.append(
            {
                "name": "Bookmark buttons: " + " | ".join(option_values),
                "values": option_values,
                "selected": [selected_label] if selected_label else [],
                "options": options,
            }
        )
    return result


async def _click_checkbox_filter_option(
    frame: Any, filter_item: dict[str, Any], value: str
) -> None:
    checkboxes = frame.locator('[role="checkbox"], input[type="checkbox"]')
    options = await _checkbox_filter_options(frame, filter_item)
    match = next(
        (
            item
            for item in options
            if _normalized_control_text(item["label"])
            == _normalized_control_text(value)
        ),
        None,
    )
    if match is None:
        raise UnavailableFilterOptionError(
            f"Option {value!r} is unavailable in checkbox filter "
            f"{filter_item.get('name')!r}; available options: "
            f"{[item['label'] for item in options]!r}"
        )
    if not match["selected"]:
        await checkboxes.nth(match["index"]).click(force=True, timeout=5_000)


async def _click_radio_filter_option(
    frame: Any, filter_item: dict[str, Any], value: str
) -> None:
    if _is_clear_selection_label(value):
        raise RuntimeError(
            f"Clear Selection is not an available option in radio filter "
            f"{filter_item.get('name')!r}"
        )
    radios = frame.locator('[role="radio"], input[type="radio"]')
    options = await _radio_filter_options(frame, filter_item)
    match = next(
        (
            item
            for item in options
            if _normalized_control_text(item["label"])
            == _normalized_control_text(value)
        ),
        None,
    )
    if match is None:
        raise UnavailableFilterOptionError(
            f"Option {value!r} is unavailable in radio filter "
            f"{filter_item.get('name')!r}; available options: "
            f"{[item['label'] for item in options]!r}"
        )
    if not match["selected"]:
        await radios.nth(match["index"]).click(force=True, timeout=5_000)


async def _options_for_combobox(frame: Any, combo: Any) -> Any:
    async def has_choices(locator: Any) -> bool:
        if not await locator.count():
            return False
        if hasattr(locator, "evaluate_all"):
            return bool(await _accessible_option_texts(locator))
        return True

    async def choices_in(container: Any) -> Any:
        options = container.get_by_role("option")
        if await has_choices(options):
            return options
        checkboxes = container.locator(
            '[role="checkbox"], input[type="checkbox"]'
        )
        if await has_choices(checkboxes):
            return checkboxes
        radios = container.locator('[role="radio"], input[type="radio"]')
        if await has_choices(radios):
            return radios
        treeitems = container.get_by_role("treeitem")
        if await has_choices(treeitems):
            return treeitems
        menuitems = container.get_by_role("menuitem")
        if await has_choices(menuitems):
            return menuitems
        generic_choices = container.locator(
            '[role="none"], li, button, [data-testid], [aria-label], [title]'
        )
        if await has_choices(generic_choices):
            return generic_choices
        return options

    controls = [
        *(
            await combo.get_attribute("aria-controls") or ""
        ).split(),
        *(
            await combo.get_attribute("aria-owns") or ""
        ).split(),
    ]
    related_popup_choices = None
    related_popup_found = False
    for control_id in dict.fromkeys(controls):
        popup = frame.locator(f"[id={json.dumps(control_id)}]")
        if await popup.count():
            related_popup_found = True
            choices = await choices_in(popup)
            if await choices.count():
                return choices
            related_popup_choices = choices

    if related_popup_found:
        return related_popup_choices

    combo_box = (
        await combo.bounding_box()
        if hasattr(combo, "bounding_box")
        else None
    )

    async def distance_from_combobox(container: Any) -> float | None:
        if combo_box is None:
            return None
        box = await container.bounding_box()
        if box is None:
            return None
        center_x = combo_box["x"] + combo_box["width"] / 2
        center_y = combo_box["y"] + combo_box["height"] / 2
        horizontal_gap = max(
            box["x"] - center_x,
            0,
            center_x - (box["x"] + box["width"]),
        )
        vertical_gap = max(
            box["y"] - center_y,
            0,
            center_y - (box["y"] + box["height"]),
        )
        return horizontal_gap**2 + vertical_gap**2

    async def nearest_choices(containers: Any) -> Any | None:
        nearest_choices = None
        nearest_distance = float("inf")
        for index in range(await containers.count()):
            container = containers.nth(index)
            if not await container.is_visible():
                continue
            distance = await distance_from_combobox(container)
            if distance is None or distance > 800**2:
                continue
            choices = await choices_in(container)
            if (
                await choices.count()
                and distance < nearest_distance
            ):
                nearest_choices = choices
                nearest_distance = distance
        return nearest_choices

    if combo_box is not None:
        listboxes = frame.get_by_role("listbox")
        choices = await nearest_choices(listboxes)
        if choices is not None:
            return choices
        for role in ("menu", "tree", "dialog"):
            containers = frame.get_by_role(role)
            choices = await nearest_choices(containers)
            if choices is not None:
                return choices
    if related_popup_choices is not None:
        return related_popup_choices
    return frame.locator("xpath=//*[false()]")


TEXT_ROW_LABELS_JS = """const textRowLabels = listbox => {
  const excluded = '[role="option"], .visual-actionButton, .visual-modern, ' +
    '[data-automation-type="visual"], [aria-hidden="true"]';
  const optionParents = new Set([...listbox.querySelectorAll('[role="option"]')]
    .map(option => option.parentElement));
  const rows = new Set([
    ...listbox.querySelectorAll('.slicerItemContainer, .slicerText'),
    ...[...optionParents].flatMap(parent => [...parent.children])
  ]);
  return [...rows].map(row => {
    if (row.querySelector('[role="option"]')) return null;
    let node = row;
    while (node && node !== listbox) {
      if (node.matches(excluded)) return null;
      node = node.parentElement;
    }
    if (node !== listbox) return null;
    const rect = row.getBoundingClientRect();
    const style = getComputedStyle(row);
    if (rect.width <= 0 || rect.height <= 0 || style.visibility === 'hidden' ||
        style.display === 'none') return null;
    const label = (row.querySelector('.slicerText')?.innerText ||
      row.innerText || row.textContent || '').replace(/\\s+/g, ' ').trim();
    return label ? { label, selected: null } : null;
  }).filter(Boolean);
};
"""

LISTBOX_READ_SCRIPT = """(listbox, index) => {
  """ + TEXT_ROW_LABELS_JS + """
  const options = [...listbox.querySelectorAll('[role="option"]')];
  return {
    index,
    visible: Boolean(listbox.getClientRects().length),
    label: (listbox.getAttribute('aria-label') ||
      listbox.getAttribute('title') || '').trim(),
    multiSelect: listbox.getAttribute('aria-multiselectable'),
    setSize: Number(options[0]?.getAttribute('aria-setsize')) || null,
    options: [...options.map(option => {
      const label = (option.getAttribute('title') ||
        option.getAttribute('aria-label') ||
        option.querySelector('.slicerText')?.innerText ||
        option.innerText || option.textContent || '')
        .replace(/\\s+/g, ' ').trim();
      const state = option.getAttribute('aria-checked') ??
        option.getAttribute('aria-selected');
      return {
        label,
        selected: state === 'true' ? true :
          state === 'false' ? false : null
      };
    }), ...textRowLabels(listbox)].filter(option => option.label)
  };
}"""

LISTBOX_SCROLL_CONTAINER_JS = """const isScrollable = node => node.scrollHeight > node.clientHeight + 1;
  const container = isScrollable(listbox) ? listbox :
    [...listbox.querySelectorAll('*')].find(node =>
      isScrollable(node) &&
      (/auto|scroll|hidden/.test(getComputedStyle(node).overflowY) ||
       node.classList.contains('scroll-content'))) || listbox;"""

LISTBOX_SCROLL_FALLBACK_SCRIPT = """(listbox, direction) => {
  """ + LISTBOX_SCROLL_CONTAINER_JS + """
  container.scrollTop += direction * Math.max(container.clientHeight * 0.8, 20);
  return container.scrollTop;
}"""

LISTBOX_EDGE_SCRIPT = """(listbox, edge) => {
  """ + LISTBOX_SCROLL_CONTAINER_JS + """
  container.scrollTop = edge === 'top' ? 0 : container.scrollHeight;
  return container.scrollTop;
}"""

MAX_LISTBOX_SCROLL_STEPS = 200
LISTBOX_STALE_SCROLL_LIMIT = 2


async def _scroll_listbox_step(page: Any, listbox: Any, direction: int = 1) -> None:
    """Scroll a virtualized listbox one step (+1 down, -1 up).

    Uses real wheel input when possible, otherwise adjusts the scroll container.
    """
    if page is not None and hasattr(listbox, "bounding_box"):
        box = await listbox.bounding_box()
        if box:
            await page.mouse.move(
                box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            )
            await page.mouse.wheel(0, direction * max(box["height"] * 0.6, 60))
            await page.wait_for_timeout(150)
            return
    await listbox.evaluate(LISTBOX_SCROLL_FALLBACK_SCRIPT, direction)
    await asyncio.sleep(0.15)


async def _jump_listbox_to_edge(listbox: Any, edge: str) -> None:
    """Move the listbox scroll position to its top or bottom."""
    await listbox.evaluate(LISTBOX_EDGE_SCRIPT, edge)
    await asyncio.sleep(0.2)


async def _collect_listbox_options(
    listbox: Any,
    page: Any,
    index: int,
    seen: dict[str, dict[str, Any]],
    direction: int,
) -> dict[str, Any]:
    """Scroll in one direction from the current position, adding options to seen."""
    data: dict[str, Any] = {}
    stale_steps = 0
    for _ in range(MAX_LISTBOX_SCROLL_STEPS):
        data = await listbox.evaluate(LISTBOX_READ_SCRIPT, index)
        before = len(seen)
        for option in data.get("options", []):
            seen.setdefault(option["label"], option)
        setsize = data.get("setSize")
        if isinstance(setsize, int) and len(seen) >= setsize:
            break
        stale_steps = 0 if len(seen) > before else stale_steps + 1
        if stale_steps >= LISTBOX_STALE_SCROLL_LIMIT:
            break
        await _scroll_listbox_step(page, listbox, direction)
    return data


async def _snapshot_scrolled_listboxes(
    listboxes: Any, page: Any = None
) -> list[dict[str, Any]]:
    """Read every option in each listbox, scrolling virtualized lists.

    The list opens at the position of the current selection, so items above it
    are missed by scrolling down alone. Read from the top downwards first, then
    from the bottom upwards if aria-setsize has not been reached.
    """
    snapshots: list[dict[str, Any]] = []
    for index in range(await listboxes.count()):
        listbox = listboxes.nth(index)
        seen: dict[str, dict[str, Any]] = {}
        data: dict[str, Any] = {}
        setsize: int | None = None
        for edge, direction in (("top", 1), ("bottom", -1)):
            await _jump_listbox_to_edge(listbox, edge)
            data = await _collect_listbox_options(
                listbox, page, index, seen, direction
            )
            setsize = data.get("setSize")
            if isinstance(setsize, int) and len(seen) >= setsize:
                break
        snapshots.append({**data, "options": list(seen.values())})
    return snapshots



async def _single_select_dropdown_options(
    frame: Any, combo: Any, filter_item: dict[str, Any]
) -> tuple[list[str], list[str] | None] | None:
    control_ids = [
        *(
            await combo.get_attribute("aria-controls") or ""
        ).split(),
        *(
            await combo.get_attribute("aria-owns") or ""
        ).split(),
    ]
    filter_names = {
        _normalized_control_text(value)
        for value in (
            filter_item.get("name"),
            filter_item.get("selector_name"),
        )
        if isinstance(value, str) and value.strip()
    }

    def parse_listbox_data(
        listbox_data: list[dict[str, Any]],
    ) -> tuple[list[str], list[str] | None, str | None] | None:
        candidates = [
            item
            for item in listbox_data
            if isinstance(item, dict)
            and item.get("visible", True)
            and item.get("options")
        ]
        matching = [
            item
            for item in candidates
            if _normalized_control_text(str(item.get("label") or ""))
            in filter_names
        ]
        candidates = matching or (candidates if len(candidates) == 1 else [])
        if len(candidates) != 1:
            return None

        option_data = candidates[0]["options"]
        values = [
            option["label"]
            for option in option_data
            if isinstance(option, dict)
            and isinstance(option.get("label"), str)
        ]
        if not values:
            return None
        selected = (
            None
            if any(
                not isinstance(option, dict)
                or not isinstance(option.get("selected"), bool)
                for option in option_data
            )
            else [
                option["label"]
                for option in option_data
                if option["selected"]
            ]
        )
        multi_select = candidates[0].get("multiSelect")
        control_type = (
            "checkbox_dropdown"
            if multi_select == "true"
            else "radio_dropdown"
            if multi_select == "false"
            else None
        )
        return values, selected, control_type

    for control_id in dict.fromkeys(control_ids):
        popup = frame.locator(f"[id={json.dumps(control_id)}]")
        if not await popup.count():
            continue
        listboxes = popup.locator('[role="listbox"]')
        listbox_data = await _snapshot_scrolled_listboxes(
            listboxes, getattr(frame, "page", None)
        )
        single_select = [
            item
            for item in listbox_data
            if isinstance(item, dict)
            and item.get("multiSelect") == "false"
            and item.get("options")
        ]
        matching = [
            item
            for item in single_select
            if _normalized_control_text(str(item.get("label") or ""))
            in filter_names
        ]
        candidates = matching or (
            single_select if len(single_select) == 1 else []
        )
        if len(candidates) != 1:
            continue
        options = candidates[0]["options"]
        values = [
            item["label"]
            for item in options
            if isinstance(item, dict) and isinstance(item.get("label"), str)
        ]
        if not values:
            continue
        if any(
            not isinstance(item, dict)
            or not isinstance(item.get("selected"), bool)
            for item in options
        ):
            return values, None
        selected = [
            item["label"]
            for item in options
            if item["selected"] and isinstance(item.get("label"), str)
        ]
        return values, selected
    return None


async def _associated_listbox_options(
    frame: Any, combo: Any, filter_item: dict[str, Any]
) -> tuple[list[str], list[str] | None, str | None] | None:
    control_ids = [
        *(await combo.get_attribute("aria-controls") or "").split(),
        *(await combo.get_attribute("aria-owns") or "").split(),
    ]
    filter_names = {
        _normalized_control_text(value)
        for value in (
            filter_item.get("name"),
            filter_item.get("selector_name"),
        )
        if isinstance(value, str) and value.strip()
    }

    def parse_listbox_data(
        listbox_data: list[dict[str, Any]],
    ) -> tuple[list[str], list[str] | None, str | None] | None:
        candidates = [
            item
            for item in listbox_data
            if isinstance(item, dict)
            and item.get("visible", True)
            and item.get("options")
        ]
        matching = [
            item
            for item in candidates
            if _normalized_control_text(str(item.get("label") or ""))
            in filter_names
        ]
        candidates = matching or (candidates if len(candidates) == 1 else [])
        if len(candidates) != 1:
            return None

        option_data = candidates[0]["options"]
        values = [
            option["label"]
            for option in option_data
            if isinstance(option, dict)
            and isinstance(option.get("label"), str)
        ]
        if not values:
            return None
        selected = (
            None
            if any(
                not isinstance(option, dict)
                or not isinstance(option.get("selected"), bool)
                for option in option_data
            )
            else [
                option["label"]
                for option in option_data
                if option["selected"]
            ]
        )
        multi_select = candidates[0].get("multiSelect")
        control_type = (
            "checkbox_dropdown"
            if multi_select == "true"
            else "radio_dropdown"
            if multi_select == "false"
            else None
        )
        return values, selected, control_type

    for control_id in dict.fromkeys(control_ids):
        popup = frame.locator(f"[id={json.dumps(control_id)}]")
        if not await popup.count():
            continue
        listboxes = popup.locator('[role="listbox"]')
        listbox_data = await _snapshot_scrolled_listboxes(
            listboxes, getattr(frame, "page", None)
        )
        result = parse_listbox_data(listbox_data)
        if result is not None:
            return result

    if not hasattr(combo, "bounding_box"):
        return None
    options = await _options_for_combobox(frame, combo)
    listbox_data = await options.evaluate_all(
        """options => {
          """ + TEXT_ROW_LABELS_JS + """
          const listboxes = [...new Set(options
            .map(option => option.closest('[role="listbox"]'))
            .filter(Boolean))];
          return listboxes.map((listbox, index) => ({
            index,
            visible: Boolean(listbox.getClientRects().length),
            label: (listbox.getAttribute('aria-label') ||
              listbox.getAttribute('title') || '').trim(),
            multiSelect: listbox.getAttribute('aria-multiselectable'),
            options: [...[...listbox.querySelectorAll('[role="option"]')]
              .map(option => {
                const label = (option.getAttribute('title') ||
                  option.getAttribute('aria-label') ||
                  option.querySelector('.slicerText')?.innerText ||
                  option.innerText || option.textContent || '')
                  .replace(/\\s+/g, ' ').trim();
                const state = option.getAttribute('aria-checked') ??
                  option.getAttribute('aria-selected');
                return {
                  label,
                  selected: state === 'true' ? true :
                    state === 'false' ? false : null
                };
              }), ...textRowLabels(listbox)].filter(option => option.label)
          }));
        }"""
    )
    return parse_listbox_data(listbox_data)


async def _wait_for_combobox_options(
    page: Page, frame: Any, combo: Any, filter_probe: dict[str, Any]
) -> tuple[
    Any,
    list[str],
    list[str] | None,
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    deadline = time.monotonic() + 15
    filter_name = filter_probe.get("name") or filter_probe.get(
        "selector_name"
    )
    while True:
        if not await _slicer_dropdown_is_open(combo):
            try:
                await _click_slicer_control(combo)
                await _wait_for_slicer_open(page, combo)
            except RuntimeError as exc:
                raise RuntimeError(
                    f"Could not open Power BI filter {filter_name!r}: {exc}"
                ) from exc
        option_locators = await _options_for_combobox(frame, combo)
        values = await _accessible_option_texts(option_locators)
        selected = await _selected_combobox_options(option_locators)
        checkbox_choices = await _checkbox_filter_options(frame, filter_probe)
        radio_choices = await _radio_filter_options(frame, filter_probe)
        if values or checkbox_choices or radio_choices:
            return (
                option_locators,
                values,
                selected,
                checkbox_choices,
                radio_choices,
            )
        if time.monotonic() >= deadline:
            return (
                option_locators,
                values,
                selected,
                checkbox_choices,
                radio_choices,
            )
        await page.wait_for_timeout(150)


def _filter_inventory_option_values(
    values: list[str],
    selected: list[str] | None,
    choices: list[dict[str, Any]],
    name: str,
    selector_name: str,
) -> tuple[list[str], list[str] | None]:
    if choices:
        option_values = list(
            dict.fromkeys(
                choice["label"]
                for choice in choices
                if isinstance(choice.get("label"), str)
                and choice["label"].strip()
            )
        )
        selected_values = [
            choice["label"]
            for choice in choices
            if choice.get("selected")
            and isinstance(choice.get("label"), str)
            and choice["label"].strip()
        ]
        return option_values, selected_values

    control_labels = {
        _normalized_control_text(value)
        for value in (name, selector_name)
        if isinstance(value, str) and value.strip()
    }
    filtered_values = [
        value
        for value in values
        if _normalized_control_text(value) not in control_labels
    ]
    if selected is None:
        return filtered_values, None
    return (
        filtered_values,
        [
            value
            for value in selected
            if _normalized_control_text(value) not in control_labels
        ],
    )


async def _save_filter_inventory_diagnostics(
    page: Page,
    combo: Any,
    filter_name: str,
    destination: Path,
    requested_option: str | None = None,
    selected_values: list[str] | None = None,
    failure_detail: str | None = None,
    target_locator: str | None = None,
    selection_state: str | None = None,
    selection_history: list[dict[str, Any]] | None = None,
    last_scroll_state: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    destination.mkdir(parents=True, exist_ok=True)
    stem = (
        f"filter_inventory_failure_"
        f"{_safe_path_component(filter_name, 40)}_{uuid4().hex[:8]}"
    )
    screenshot_path = destination / f"{stem}.png"
    diagnostics_path = destination / f"{stem}.json"
    controls = await combo.evaluate(
        """element => {
          const summarize = node => ({
            id: node.id || '',
            tag: node.tagName.toLowerCase(),
            role: node.getAttribute('role') || '',
            label: node.getAttribute('aria-label') || '',
            expanded: node.getAttribute('aria-expanded'),
            selected: node.getAttribute('aria-selected'),
            checked: node.getAttribute('aria-checked'),
            text: (node.innerText || node.textContent || '')
              .replace(/\\s+/g, ' ').trim().slice(0, 160),
            children: [...node.querySelectorAll(
              '[role], option, button, input'
            )].slice(0, 40).map(child => ({
              tag: child.tagName.toLowerCase(),
              role: child.getAttribute('role') || '',
              label: child.getAttribute('aria-label') || '',
              selected: child.getAttribute('aria-selected'),
              checked: child.getAttribute('aria-checked'),
              text: (child.innerText || child.textContent || '')
                .replace(/\\s+/g, ' ').trim().slice(0, 120)
            }))
          });
          const ids = [
            ...(element.getAttribute('aria-controls') || '').split(/\\s+/),
            ...(element.getAttribute('aria-owns') || '').split(/\\s+/)
          ].filter(Boolean);
          const popupNodes = [...new Set([
            ...ids.map(id => document.getElementById(id)),
            ...document.querySelectorAll('[role="listbox"]')
          ].filter(Boolean))];
          return {
            combobox: summarize(element),
            ariaControls: element.getAttribute('aria-controls') || '',
            ariaOwns: element.getAttribute('aria-owns') || '',
            expanded: element.getAttribute('aria-expanded'),
            relatedPopups: ids.map(id => ({
              requestedId: id,
              popup: document.getElementById(id)
                ? summarize(document.getElementById(id)) : null
            })),
            visibleListboxes: [...document.querySelectorAll('[role="listbox"]')]
              .filter(node => {
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return rect.width > 0 && rect.height > 0 &&
                  style.display !== 'none' && style.visibility !== 'hidden';
              }).slice(0, 10).map(summarize),
            visiblePopupContainers: [...document.querySelectorAll(
              '[role="menu"], [role="tree"], [role="dialog"]'
            )].filter(node => {
              const rect = node.getBoundingClientRect();
              const style = getComputedStyle(node);
              return rect.width > 0 && rect.height > 0 &&
                style.display !== 'none' && style.visibility !== 'hidden';
            }).slice(0, 10).map(summarize),
            visibleOptionCandidates: [...document.querySelectorAll(
              '[role="option"], [role="menuitem"], [role="treeitem"], ' +
              '[role="none"], li, button, [data-testid]'
            )].filter(node => {
              const rect = node.getBoundingClientRect();
              const style = getComputedStyle(node);
              return rect.width > 0 && rect.height > 0 &&
                style.display !== 'none' && style.visibility !== 'hidden';
            }).slice(0, 40).map(summarize),
            associatedRadioOptions: popupNodes.flatMap(popup => {
              const optionNodes = [
                ...(popup.matches(
                  '[role="option"], [role="radio"], input[type="radio"]'
                ) ? [popup] : []),
                ...popup.querySelectorAll(
                '[role="option"], [role="radio"], input[type="radio"]'
                )
              ];
              return optionNodes.map(option => {
                const rect = option.getBoundingClientRect();
                const style = getComputedStyle(option);
                const input = option.matches('input[type="radio"]')
                  ? option : option.querySelector('input[type="radio"]');
                const label = option.getAttribute('title') ||
                  option.getAttribute('aria-label') ||
                  option.querySelector('.slicerText')?.innerText ||
                  option.innerText || option.textContent || '';
                return {
                  tag: option.tagName.toLowerCase(),
                  popupId: popup.id || '',
                  popupRole: popup.getAttribute('role') || '',
                  role: option.getAttribute('role') || '',
                  className: typeof option.className === 'string'
                    ? option.className : '',
                  label: label.replace(/\\s+/g, ' ').trim(),
                  title: option.getAttribute('title') || '',
                  ariaLabel: option.getAttribute('aria-label') || '',
                  ariaChecked: option.getAttribute('aria-checked'),
                  ariaSelected: option.getAttribute('aria-selected'),
                  rowIndex: option.getAttribute('data-row-index'),
                  rowId: option.getAttribute('data-row-id'),
                  tabIndex: option.getAttribute('tabindex'),
                  inputType: input?.type || '',
                  inputName: input?.name || '',
                  inputValue: input?.value || '',
                  inputChecked: input?.checked ?? null,
                  inputDisabled: input?.disabled ?? null,
                  labels: input?.labels
                    ? [...input.labels].map(item => item.innerText.trim())
                    : [],
                  disabled: option.getAttribute('aria-disabled') === 'true' ||
                    !!option.disabled || !!input?.disabled,
                  visible: rect.width > 0 && rect.height > 0 &&
                    style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    option.getAttribute('aria-hidden') !== 'true',
                  bounds: {
                    x: rect.x,
                    y: rect.y,
                    width: rect.width,
                    height: rect.height
                  },
                  scrollAncestors: (() => {
                    const ancestors = [];
                    let node = option.parentElement;
                    while (node && node !== popup) {
                      const nodeStyle = getComputedStyle(node);
                      if (node.scrollHeight > node.clientHeight + 1 ||
                          /auto|scroll|hidden/.test(nodeStyle.overflowY) ||
                          node.classList.contains('scroll-content')) {
                        ancestors.push({
                          tag: node.tagName.toLowerCase(),
                          className: typeof node.className === 'string'
                            ? node.className : '',
                          overflowY: nodeStyle.overflowY,
                          clientHeight: node.clientHeight,
                          scrollHeight: node.scrollHeight,
                          scrollTop: node.scrollTop
                        });
                      }
                      node = node.parentElement;
                    }
                    return ancestors;
                  })()
                };
              });
            })
          };
        }"""
    )
    await page.screenshot(path=str(screenshot_path), full_page=True)
    diagnostics = {
        "filter_name": filter_name,
        "requested_option": requested_option,
        "failure_detail": failure_detail,
        "target_locator": target_locator,
        "controls": controls,
        "screenshot": screenshot_path.name,
    }
    if selection_state == "unavailable":
        diagnostics["selection_state"] = "unavailable"
    else:
        diagnostics["selected_values"] = selected_values
        if selection_state is not None:
            diagnostics["selection_state"] = selection_state
    if selection_history is not None:
        diagnostics["selection_history"] = selection_history
    if last_scroll_state is not None:
        diagnostics["last_scroll_state"] = last_scroll_state
    diagnostics_path.write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return screenshot_path, diagnostics_path


async def _combobox_for_filter(frame: Any, filter_item: dict[str, Any]) -> Any | None:
    filter_name = str(filter_item.get("name") or "")
    selector_name = str(
        filter_item.get("selector_name") or filter_name
    )
    combos = frame.get_by_role("combobox")
    combo_metadata = await combos.evaluate_all(COMBOBOX_METADATA_SCRIPT)
    matching_names = [
        item
        for item in combo_metadata
        if _normalized_control_text(item.get("name") or "")
        == _normalized_control_text(filter_name)
    ]
    matching_selectors = [
        item
        for item in matching_names
        if _normalized_control_text(item.get("selectorName") or "")
        == _normalized_control_text(selector_name)
    ]
    if len(matching_selectors) == 1:
        return combos.nth(matching_selectors[0]["index"])
    if len(matching_names) == 1:
        return combos.nth(matching_names[0]["index"])
    return None


async def _selected_combobox_options(options: Any) -> list[str] | None:
    items = await options.evaluate_all(
        """elements => elements.map(element => {
          const searchControl = element.matches(
            'input[type="search"], input[role="searchbox"], ' +
            'textarea[role="searchbox"], [role="searchbox"], ' +
            'input[aria-label*="search" i], input[placeholder*="search" i], ' +
            'textarea[aria-label*="search" i], ' +
            'textarea[placeholder*="search" i]'
          ) || element.closest('[role="searchbox"]');
          if (searchControl) return null;
          const choiceSelector =
            '[role="option"], [role="radio"], [role="checkbox"], ' +
            '[role="treeitem"], [role="menuitem"], ' +
            'input[type="radio"], input[type="checkbox"]';
          if (element.querySelectorAll(choiceSelector).length > 1) {
            return null;
          }
          const label = (element.getAttribute('aria-label') ||
            element.getAttribute('title') ||
            [...(element.labels || [])].map(item => item.innerText.trim())
              .filter(Boolean).join(' ') ||
            element.closest('[role="option"]')?.innerText ||
            element.closest('label')?.innerText ||
            element.innerText || element.textContent ||
            element.parentElement?.innerText || '')
            .replace(/\\s+/g, ' ').trim();
          const midpoint = label.length / 2;
          const cleanLabel = label.length % 2 === 0 &&
            label.slice(0, midpoint) === label.slice(midpoint)
              ? label.slice(0, midpoint) : label;
          const isSelectable = element.matches(choiceSelector) ||
            !!element.closest(choiceSelector) ||
            element.hasAttribute('aria-selected') ||
            element.hasAttribute('aria-checked') ||
            element.hasAttribute('aria-pressed');
          if (
            cleanLabel.toLocaleLowerCase() === 'search' &&
            !isSelectable
          ) return null;
          const checkbox = element.matches(
            '[role="checkbox"], input[type="checkbox"]'
          ) ? element : element.querySelector(
            '[role="checkbox"], input[type="checkbox"]'
          );
          const radio = element.matches(
            '[role="radio"], input[type="radio"]'
          ) ? element : element.querySelector(
            '[role="radio"], input[type="radio"]'
          );
          const states = [
            element.getAttribute('aria-selected'),
            element.getAttribute('aria-checked'),
            element.getAttribute('aria-pressed'),
            checkbox?.getAttribute('aria-checked'),
            radio?.getAttribute('aria-checked'),
          ];
          let selected = null;
          for (const state of states) {
            if (state === 'true' || state === 'false') {
              selected = state === 'true';
              break;
            }
          }
          if (selected === null && checkbox?.tagName === 'INPUT') {
            selected = checkbox.checked;
          }
          if (selected === null && radio?.tagName === 'INPUT') {
            selected = radio.checked;
          }
          return cleanLabel ? {label: cleanLabel, selected} : null;
        }).filter(Boolean)"""
    )
    if (
        not isinstance(items, list)
        or not items
        or any(
            not isinstance(item, dict) or not isinstance(item.get("selected"), bool)
            for item in items
        )
    ):
        return None
    return [
        _normalize_option_text(item["label"])
        for item in items
        if item["selected"]
        and isinstance(item.get("label"), str)
        and _normalize_option_text(item["label"])
    ]


def _normalized_control_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _validate_individual_filter_option(
    value: str,
    filter_name: str | None,
    available_values: list[str],
) -> None:
    if not all(isinstance(option, str) for option in available_values):
        raise ValueError(
            f"Filter {filter_name!r} has an invalid option inventory"
        )
    if not any(
        _normalized_control_text(value)
        == _normalized_control_text(option)
        for option in available_values
    ):
        raise RuntimeError(
            f"Requested value {value!r} is not one individual discovered "
            f"option for filter {filter_name!r}; available options: "
            f"{available_values!r}"
        )


def _is_clear_selection_label(value: str) -> bool:
    return _normalized_control_text(value) in {
        "clear selection",
        "clear selections",
    }


def _filter_selection_is_clear(
    filter_item: dict[str, Any], selected: list[str] | None
) -> bool:
    if selected is None:
        return False
    if not selected or any(_is_aggregate_value(value) for value in selected):
        return True
    options = {
        _normalized_control_text(value)
        for value in filter_item.get("values", [])
        if isinstance(value, str)
        and not _is_aggregate_value(value)
        and not _is_clear_selection_label(value)
    }
    return bool(options) and {
        _normalized_control_text(value) for value in selected
    } == options


def _selected_values_with_summary(
    selected: list[str] | None,
    summary: str,
    options: list[str],
    control_type: str | None = None,
) -> list[str] | None:
    if selected:
        normalized_options = {
            _normalized_control_text(value) for value in options
        }
        selected_values = [
            value
            for value in selected
            if not _is_clear_selection_label(value)
        ]
        selected_keys = {
            _normalized_control_text(value) for value in selected_values
        }
        selected_all_marker = any(
            key == "select all"
            or (
                _is_aggregate_value(key)
                and key not in normalized_options
            )
            for key in selected_keys
        )
        if selected_all_marker:
            return ["All"]
        option_keys = {
            _normalized_control_text(value)
            for value in options
            if not _is_aggregate_value(_normalized_control_text(value))
            and not _is_clear_selection_label(value)
        }
        if (
            control_type in {"checkbox_dropdown", "checkbox"}
            and option_keys
            and selected_keys == option_keys
        ):
            return ["All"]
        return selected

    aggregate = next(
        (
            line.strip()
            for line in summary.splitlines()
            if _is_aggregate_value(_normalized_control_text(line))
        ),
        None,
    )
    if aggregate:
        return [aggregate]

    normalized_summary = _normalized_control_text(summary)
    matching_options = [
        option
        for option in options
        if _normalized_control_text(option) == normalized_summary
    ]
    if len(matching_options) == 1:
        return matching_options
    return selected


def _visible_selected_values(
    name: str, selector_name: str, summary: str
) -> list[str]:
    selected = _selected_values_with_summary(None, summary, [])
    if selected:
        return selected

    labels = {
        _normalized_control_text(label)
        for label in (name, selector_name)
        if label
    }
    visible_values = []
    for line in summary.splitlines():
        value = line.strip()
        if not value:
            continue
        normalized = _normalized_control_text(value)
        if normalized in labels:
            continue
        for label in (name, selector_name):
            if not label or value[: len(label)].casefold() != label.casefold():
                continue
            remainder = value[len(label) :]
            if remainder and not (
                remainder[0].isspace() or remainder[0] in ":–—-"
            ):
                continue
            value = remainder.strip(" \t:–—-")
            break
        if value and _normalized_control_text(value) not in labels:
            visible_values.append(value)
    return list(dict.fromkeys(visible_values))


def _selection_matches(expected: str, actual: str) -> bool:
    expected_text = _normalized_control_text(expected)
    actual_text = _normalized_control_text(actual)
    if _is_aggregate_value(expected_text) and _is_aggregate_value(actual_text):
        # Power BI shows an active "Select all" as "All" or "(All)".
        return True
    return expected_text == actual_text or expected_text in actual_text


def _filter_values_match(expected: list[str], actual: list[str]) -> bool:
    if len(expected) != len(actual):
        return False
    unmatched = list(actual)
    for expected_value in expected:
        match_index = next(
            (
                index
                for index, actual_value in enumerate(unmatched)
                if (
                    _is_aggregate_value(_normalized_control_text(expected_value))
                    and _is_aggregate_value(_normalized_control_text(actual_value))
                )
                or _normalized_control_text(expected_value)
                == _normalized_control_text(actual_value)
            ),
            None,
        )
        if match_index is None:
            return False
        unmatched.pop(match_index)
    return not unmatched


def _filter_values_equivalent(
    filter_item: dict[str, Any], expected: list[str], actual: list[str]
) -> bool:
    if _filter_values_match(expected, actual):
        return True
    values = [
        value
        for value in filter_item.get("values", [])
        if isinstance(value, str)
        and not _is_aggregate_value(_normalized_control_text(value))
    ]
    if len(actual) == 1 and _is_aggregate_value(
        _normalized_control_text(actual[0])
    ):
        return _filter_values_match(expected, values)
    if len(expected) == 1 and _is_aggregate_value(
        _normalized_control_text(expected[0])
    ):
        return _filter_values_match(actual, values)
    return False


def _filter_selection_matches(
    filter_item: dict[str, Any], expected: str, actual: list[str]
) -> bool:
    if _is_aggregate_value(_normalized_control_text(expected)):
        if any(
            _is_aggregate_value(_normalized_control_text(value))
            for value in actual
        ):
            return True
        available = [
            value
            for value in filter_item.get("values", [])
            if not _is_aggregate_value(_normalized_control_text(value))
        ]
        return bool(available) and _filter_values_match(available, actual)
    return len(actual) == 1 and _selection_matches(expected, actual[0])


async def _click_option_by_text(options: Any, value: str) -> str | None:
    match = await options.evaluate_all(
        """(elements, requested) => {
          const normalize = value =>
            (value || '').replace(/\\s+/g, ' ').trim().toLocaleLowerCase();
          const isSearchControl = element => element.matches(
            'input[type="search"], input[role="searchbox"], ' +
            'textarea[role="searchbox"], [role="searchbox"], ' +
            'input[aria-label*="search" i], input[placeholder*="search" i], ' +
            'textarea[aria-label*="search" i], ' +
            'textarea[placeholder*="search" i]'
          ) || !!element.closest('[role="searchbox"]');
          const labelOf = element => (
            element.getAttribute('aria-label') ||
            element.getAttribute('title') ||
            [...(element.labels || [])].map(label => label.innerText.trim())
              .filter(Boolean).join(' ') ||
            element.closest('[role="option"]')?.innerText ||
            element.closest('label')?.innerText ||
            element.innerText || element.textContent ||
            element.parentElement?.innerText || ''
          ).replace(/\\s+/g, ' ').trim();
          const cleanLabel = label => {
            const midpoint = label.length / 2;
            return label.length % 2 === 0 &&
              label.slice(0, midpoint) === label.slice(midpoint)
              ? label.slice(0, midpoint) : label;
          };
          const choiceSelector =
            '[role="option"], [role="radio"], [role="checkbox"], ' +
            'input[type="radio"], input[type="checkbox"]';
          const isSelectable = element => element.matches(choiceSelector) ||
            !!element.closest(choiceSelector) ||
            !!element.querySelector(choiceSelector) ||
            element.hasAttribute('aria-selected') ||
            element.hasAttribute('aria-checked') ||
            element.hasAttribute('aria-pressed');
          const expected = normalize(requested);
          const index = elements.findIndex(element => {
            if (isSearchControl(element)) return false;
            const label = cleanLabel(labelOf(element));
            if (
              normalize(label) === 'search' &&
              !isSelectable(element)
            ) return false;
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            return normalize(label) === expected &&
              rect.width > 0 && rect.height > 0 &&
              style.visibility !== 'hidden' && style.display !== 'none';
          });
          if (index < 0) return null;
          const option = elements[index];
          return {
            index,
            label: cleanLabel(labelOf(option))
          };
        }""",
        value,
    )
    if not match:
        return None
    option = options.nth(match["index"])
    await option.click(force=True, timeout=5_000)
    return match["label"]


async def _radio_dropdown_option_candidates(options: Any) -> list[dict[str, Any]]:
    candidates = await options.evaluate_all(
        """elements => elements.map((element, index) => {
          const optionElements = elements.filter(candidate =>
            candidate.getAttribute('role') === 'option'
          );
          const label = (
            element.getAttribute('title') ||
            element.getAttribute('aria-label') ||
            element.querySelector('.slicerText')?.innerText ||
            element.innerText || element.textContent || ''
          ).replace(/\\s+/g, ' ').trim();
          let node = element;
          let visible = true;
          while (node && node.nodeType === Node.ELEMENT_NODE) {
            const style = getComputedStyle(node);
            if (style.display === 'none' ||
                style.visibility === 'hidden' ||
                node.getAttribute('aria-hidden') === 'true') {
              visible = false;
              break;
            }
            node = node.parentElement;
          }
          const rect = element.getBoundingClientRect();
          return {
            index,
            label,
            visible: visible && rect.width > 0 && rect.height > 0,
            position: Number(
              element.getAttribute('aria-posinset') ||
              element.getAttribute('data-row-index') || 0
            ) || null,
            nestedOptionIndices: optionElements
              .map((candidate, candidateIndex) =>
                candidate !== element && element.contains(candidate)
                  ? elements.indexOf(candidate)
                  : null
              )
              .filter(candidateIndex => candidateIndex !== null),
            role: element.getAttribute('role') || '',
            ariaSelected: element.getAttribute('aria-selected'),
            ariaChecked: element.getAttribute('aria-checked'),
            bounds: {
              x: rect.x,
              y: rect.y,
              width: rect.width,
              height: rect.height
            },
            disabled: element.disabled === true ||
              element.getAttribute('aria-disabled') === 'true'
          };
        })"""
    )
    return [
        item for item in candidates if isinstance(item, dict)
    ] if isinstance(candidates, list) else []


async def _radio_dropdown_option_locator(
    options: Any, value: str, filter_name: str
) -> Any | None:
    candidates = await _radio_dropdown_option_candidates(options)
    matches = [
        item
        for item in candidates
        if item.get("visible")
        and not item.get("disabled")
        and isinstance(item.get("label"), str)
        and _normalized_control_text(item["label"])
        == _normalized_control_text(value)
    ]
    nested_matches = [
        item
        for item in matches
        if not any(
            other.get("index") in item.get("nestedOptionIndices", [])
            for other in matches
        )
    ]
    resolved_nested_duplicate = len(matches) > 1 and len(nested_matches) == 1
    if resolved_nested_duplicate:
        matches = nested_matches
    if len(matches) > 1:
        raise RuntimeError(
            f"Multiple visible options match {value!r} in filter "
            f"{filter_name!r}: "
            f"{[item for item in matches]!r}"
        )
    if not matches:
        return None
    option = options.nth(matches[0]["index"])
    if hasattr(option, "locator"):
        listbox = option.locator(
            'xpath=ancestor-or-self::*[@role="listbox"][1]'
        )
        if await listbox.count():
            named_options = listbox.get_by_role(
                "option", name=value, exact=True
            )
            named_count = await named_options.count()
            if named_count != 1:
                if resolved_nested_duplicate and named_count == 2:
                    return option
                if named_count == 0:
                    return option
                raise RuntimeError(
                    f"Could not reacquire the unique accessible option "
                    f"{value!r} in filter {filter_name!r}; matches: "
                    f"{named_count}"
                )
            return option
    return option


async def _scroll_filter_option_into_view(
    page: Page,
    frame: Any,
    combo: Any,
    value: str,
    filter_name: str,
    expected_options: list[str],
) -> tuple[Any | None, dict[str, Any]]:
    state: dict[str, Any] = {"visible": False}
    option = None
    for _ in range(12):
        options = await _options_for_combobox(frame, combo)
        candidates = await _radio_dropdown_option_candidates(options)
        option = await _radio_dropdown_option_locator(
            options, value, filter_name
        )
        if option is None:
            positions = [
                item["position"]
                for item in candidates
                if item.get("visible")
                and isinstance(item.get("position"), int)
            ]
            expected_position = next(
                (
                    index + 1
                    for index, label in enumerate(expected_options)
                    if _normalized_control_text(label)
                    == _normalized_control_text(value)
                ),
                None,
            )
            if not positions or expected_position is None:
                return None, {
                    "visible": False,
                    "reason": (
                        "exact option is not currently rendered and the "
                        "popup exposes no usable option positions"
                    ),
                    "visible_positions": positions,
                }
            min_position = min(positions)
            max_position = max(positions)
            if expected_position < min_position:
                wheel_delta = -160
            elif expected_position > max_position:
                wheel_delta = 160
            else:
                wheel_delta = None
            anchor = options.nth(
                next(
                    item["index"]
                    for item in candidates
                    if item.get("visible")
                    and isinstance(item.get("position"), int)
                )
            )
            scroll_state = await anchor.evaluate(
                """element => {
                  const listbox = element.closest('[role="listbox"]');
                  if (!listbox) return null;
                  let scrollContainer = null;
                  let node = element.parentElement;
                  while (node) {
                    const style = getComputedStyle(node);
                    const hasScrollRange =
                      node.scrollHeight > node.clientHeight + 1;
                    if (hasScrollRange &&
                        (/auto|scroll/.test(style.overflowY) ||
                         node.classList.contains('scroll-content') ||
                         node === listbox)) {
                      scrollContainer = node;
                      break;
                    }
                    if (node === listbox) break;
                    node = node.parentElement;
                  }
                  const viewport = scrollContainer || listbox;
                  const rect = viewport.getBoundingClientRect();
                  return {
                    x: rect.x,
                    y: rect.y,
                    width: rect.width,
                    height: rect.height,
                    scrollTop: viewport.scrollTop,
                    clientHeight: viewport.clientHeight,
                    scrollHeight: viewport.scrollHeight
                  };
                }"""
            )
            if (
                isinstance(scroll_state, dict)
                and isinstance(scroll_state.get("clientHeight"), (int, float))
                and isinstance(scroll_state.get("scrollHeight"), (int, float))
                and scroll_state["scrollHeight"]
                <= scroll_state["clientHeight"] + 1
            ):
                return None, {
                    "visible": False,
                    "reason": (
                        "exact option is not rendered and the active popup "
                        "has no scrollable content"
                    ),
                    "visible_positions": positions,
                    "visible_options": [
                        item["label"]
                        for item in candidates
                        if item.get("visible")
                        and isinstance(item.get("label"), str)
                    ],
                    "scroll_state": scroll_state,
                }
            if wheel_delta is None:
                return None, {
                    "visible": False,
                    "reason": (
                        "exact option is absent from the active popup even "
                        "though its inventory position falls within the "
                        "currently rendered range"
                    ),
                    "visible_positions": positions,
                    "expected_position": expected_position,
                    "visible_options": [
                        item["label"]
                        for item in candidates
                        if item.get("visible")
                        and isinstance(item.get("label"), str)
                    ],
                    "scroll_state": scroll_state,
                }
            bounds = scroll_state
            if (
                not isinstance(bounds, dict)
                or bounds["width"] <= 0
                or bounds["height"] <= 0
            ):
                return None, {
                    "visible": False,
                    "reason": "popup scroll container could not be located",
                    "visible_positions": positions,
                }
            await page.mouse.move(
                bounds["x"] + bounds["width"] / 2,
                bounds["y"] + bounds["height"] / 2,
            )
            await page.mouse.wheel(0, wheel_delta)
            await page.wait_for_timeout(100)
            state = {
                "visible": False,
                "reason": "scrolled popup to reveal virtualized option",
                "wheel_delta": wheel_delta,
                "visible_positions": positions,
                "expected_position": expected_position,
                "scroll_bounds": bounds,
            }
            continue
        if hasattr(option, "scroll_into_view_if_needed"):
            try:
                await option.scroll_into_view_if_needed(timeout=2_000)
            except PlaywrightError as exc:
                state = {
                    "visible": False,
                    "reason": (
                        "Playwright could not scroll the exact popup option "
                        f"into view: {exc}"
                    ),
                }
                await page.wait_for_timeout(100)
                continue
        try:
            state = await asyncio.wait_for(
                option.evaluate(
                    """element => {
          const listbox = element.closest('[role="listbox"]');
          if (!listbox) {
            return {visible: false, reason: 'option has no listbox ancestor'};
          }
          let scrollContainer = null;
          let node = element.parentElement;
          while (node) {
            const style = getComputedStyle(node);
            const hasScrollRange = node.scrollHeight > node.clientHeight + 1;
            const customScrollContent =
              node.classList.contains('scroll-content');
            if (hasScrollRange &&
                (/auto|scroll/.test(style.overflowY) ||
                 customScrollContent ||
                 node === listbox)) {
              scrollContainer = node;
              break;
            }
            if (node === listbox) break;
            node = node.parentElement;
          }
          const viewport = scrollContainer || listbox;
          const bounds = viewport.getBoundingClientRect();
          return new Promise(resolve => requestAnimationFrame(() => {
            const rect = element.getBoundingClientRect();
            const clip = viewport.getBoundingClientRect();
            const wheelDelta = rect.top < clip.top - 1
              ? -Math.max(60, Math.min(240, clip.top - rect.top))
              : rect.bottom > clip.bottom + 1
                ? Math.max(60, Math.min(240, rect.bottom - clip.bottom))
                : 0;
            resolve({
              visible: rect.width > 0 && rect.height > 0 &&
                rect.top >= clip.top - 1 &&
                rect.bottom <= clip.bottom + 1 &&
                rect.left >= clip.left - 1 &&
                rect.right <= clip.right + 1,
              wheelDelta,
              scrollBounds: {
                x: clip.x,
                y: clip.y,
                width: clip.width,
                height: clip.height
              },
              containerClass: scrollContainer &&
                typeof scrollContainer.className === 'string'
                  ? scrollContainer.className : '',
              scrollTop: scrollContainer?.scrollTop ?? null,
              clientHeight: scrollContainer?.clientHeight ?? null,
              scrollHeight: scrollContainer?.scrollHeight ?? null
            });
          }));
        }"""
                ),
                timeout=2.0,
            )
        except (PlaywrightError, asyncio.TimeoutError) as exc:
            state = {
                "visible": False,
                "reason": (
                    "The popup option changed while checking its visibility; "
                    f"reacquiring it: {exc}"
                ),
            }
            await page.wait_for_timeout(100)
            continue
        if state.get("visible"):
            return option, state
        bounds = state.get("scrollBounds")
        wheel_delta = state.get("wheelDelta")
        if (
            not isinstance(bounds, dict)
            or not isinstance(wheel_delta, (int, float))
            or wheel_delta == 0
        ):
            return option, state
        await page.mouse.move(
            bounds["x"] + bounds["width"] / 2,
            bounds["y"] + bounds["height"] / 2,
        )
        await page.mouse.wheel(0, wheel_delta)
        await page.wait_for_timeout(100)
    return option, state


def _radio_dropdown_selected_labels(
    items: Any, filter_name: str
) -> list[str]:
    if not isinstance(items, list) or not items:
        raise FilterSelectionInspectionError(
            f"Cannot inspect radio selection for filter {filter_name!r}: "
            "its associated popup exposed no option state"
        )
    unreadable = [
        item
        for item in items
        if not isinstance(item, dict)
        or not isinstance(item.get("label"), str)
        or not item["label"].strip()
        or not isinstance(item.get("selected"), bool)
    ]
    if unreadable:
        details = [
            {
                "label": item.get("label") if isinstance(item, dict) else None,
                "role": item.get("role") if isinstance(item, dict) else None,
                "ariaChecked": (
                    item.get("ariaChecked") if isinstance(item, dict) else None
                ),
                "ariaSelected": (
                    item.get("ariaSelected") if isinstance(item, dict) else None
                ),
                "inputChecked": (
                    item.get("inputChecked") if isinstance(item, dict) else None
                ),
            }
            for item in unreadable[:10]
        ]
        raise FilterSelectionInspectionError(
            f"Cannot inspect radio selection for filter {filter_name!r}; "
            f"some popup options expose no checked/selected state: {details!r}"
        )
    return [
        _normalize_option_text(item["label"])
        for item in items
        if item["selected"] and _normalize_option_text(item["label"])
    ]


RADIO_DROPDOWN_SELECTION_SCRIPT = """elements => elements.map(element => {
  const input = element.matches('input[type="radio"]')
    ? element : element.querySelector('input[type="radio"]');
  const radioRole = element.matches('[role="radio"]')
    ? element : element.querySelector('[role="radio"]');
  const label = (
    element.getAttribute('title') ||
    element.getAttribute('aria-label') ||
    [...(input?.labels || [])].map(item => item.innerText.trim())
      .filter(Boolean).join(' ') ||
    element.querySelector('.slicerText')?.innerText ||
    element.closest('label')?.innerText ||
    element.innerText || element.textContent || ''
  ).replace(/\\s+/g, ' ').trim();
  let selected = null;
  let stateSource = null;
  if (input) {
    selected = input.checked;
    stateSource = 'input.checked';
  } else {
    const states = [
      [radioRole?.getAttribute('aria-checked'), 'radio.aria-checked'],
      [element.getAttribute('aria-checked'), 'option.aria-checked'],
      [element.getAttribute('aria-selected'), 'option.aria-selected'],
      [element.getAttribute('aria-pressed'), 'option.aria-pressed']
    ];
    for (const [state, source] of states) {
      if (state === 'true' || state === 'false') {
        selected = state === 'true';
        stateSource = source;
        break;
      }
    }
    if (selected === null) {
      const indicator = element.matches('.slicerCheckbox')
        ? element : element.querySelector('.slicerCheckbox');
      if (indicator) {
        const classes = indicator.classList;
        selected = classes.contains('selected') ||
          classes.contains('checked') ||
          classes.contains('is-selected') ||
          classes.contains('is-checked');
        stateSource = 'Power BI slicerCheckbox class';
      }
    }
  }
  const rect = element.getBoundingClientRect();
  let node = element;
  let visible = true;
  while (node) {
    const style = getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' ||
        node.getAttribute('aria-hidden') === 'true') {
      visible = false;
      break;
    }
    if (node.getAttribute('role') === 'listbox') break;
    node = node.parentElement;
  }
  const isVisible = rect.width > 0 && rect.height > 0 &&
    visible;
  if (!isVisible) return null;
  return {
    label,
    selected,
    stateSource,
    role: element.getAttribute('role') || '',
    radioRole: radioRole?.getAttribute('role') || '',
    ariaChecked: element.getAttribute('aria-checked'),
    ariaSelected: element.getAttribute('aria-selected'),
    ariaPressed: element.getAttribute('aria-pressed'),
    inputType: input?.type || '',
    inputName: input?.name || '',
    inputValue: input?.value || '',
    inputChecked: input ? input.checked : null
  };
}).filter(Boolean)"""


async def _radio_dropdown_selection(
    page: Page, frame: Any, combo: Any, filter_name: str
) -> list[str]:
    if not await _slicer_dropdown_is_open(combo):
        await _click_slicer_control(combo)
        await _wait_for_slicer_open(page, combo)

    controls = list(
        dict.fromkeys(
            [
                *(await combo.get_attribute("aria-controls") or "").split(),
                *(await combo.get_attribute("aria-owns") or "").split(),
            ]
        )
    )
    if not controls:
        raise FilterSelectionInspectionError(
            f"Cannot inspect radio selection for filter {filter_name!r}: "
            "its combobox does not identify an associated popup"
        )

    options = None
    last_popup_state = "associated popup has no visible listbox"
    for popup_attempt, wait_seconds in enumerate((3, 8)):
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            visible_listboxes = []
            for control_id in controls:
                popup = frame.locator(f"[id={json.dumps(control_id)}]")
                if await popup.count() != 1:
                    continue
                listboxes = popup.locator('[role="listbox"]')
                for index in range(await listboxes.count()):
                    listbox = listboxes.nth(index)
                    if await listbox.is_visible():
                        label = (
                            await listbox.get_attribute("aria-label")
                            or await listbox.get_attribute("title")
                            or ""
                        )
                        visible_listboxes.append((listbox, label))

            matching_listboxes = [
                item
                for item in visible_listboxes
                if _normalized_control_text(filter_name)
                in _normalized_control_text(item[1])
            ]
            candidates = matching_listboxes or visible_listboxes
            if len(candidates) == 1:
                option_nodes = candidates[0][0].locator(
                    '[role="option"], [role="radio"], input[type="radio"]'
                )
                if await option_nodes.count():
                    options = option_nodes
                    break
                last_popup_state = (
                    "associated listbox has no rendered option or radio nodes"
                )
            elif len(candidates) > 1:
                raise FilterSelectionInspectionError(
                    f"Cannot inspect radio selection for filter "
                    f"{filter_name!r}: its associated popup has multiple "
                    "visible listboxes"
                )

            await page.wait_for_timeout(100)
        if options is not None:
            break
        if popup_attempt == 0:
            await _close_slicer_dropdown(page, combo)
            if not await _slicer_dropdown_is_open(combo):
                await _click_slicer_control(combo)
                await _wait_for_slicer_open(page, combo)

    if options is None:
        raise FilterSelectionInspectionError(
            f"Cannot inspect radio selection for filter {filter_name!r}: "
            f"{last_popup_state} after waiting for popup options"
        )

    try:
        items = await options.evaluate_all(RADIO_DROPDOWN_SELECTION_SCRIPT)
    except PlaywrightError as exc:
        raise FilterSelectionInspectionError(
            f"Cannot inspect radio selection for filter {filter_name!r}: "
            f"popup option-state read failed: {exc}"
        ) from exc
    return _radio_dropdown_selected_labels(items, filter_name)


async def _select_radio_dropdown_option(
    page: Page,
    frame: Any,
    combo: Any,
    filter_item: dict[str, Any],
    value: str,
    diagnostic_directory: Path | None = None,
) -> list[str]:
    search_field = None
    search_action_attempted = False
    selected: list[str] = []
    last_inspection_error = None
    last_click_error = None
    last_click_target = "not clicked"
    last_selection_issue = "selection did not start"
    last_scroll_state: dict[str, Any] | None = None
    selection_history: list[dict[str, Any]] = []

    def record_selection(
        attempt: int,
        phase: str,
        values: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        observation = {
            "attempt": attempt + 1,
            "phase": phase,
            "selected_values": values,
            "inspection_error": error,
        }
        if not selection_history or selection_history[-1] != observation:
            selection_history.append(observation)

    async def find_option(options: Any) -> Any | None:
        try:
            return await _radio_dropdown_option_locator(
                options, value, str(filter_item.get("name") or "")
            )
        except RuntimeError as exc:
            if diagnostic_directory is not None:
                await _save_filter_inventory_diagnostics(
                    page,
                    combo,
                    str(filter_item.get("name") or ""),
                    diagnostic_directory,
                    requested_option=value,
                    selected_values=selected,
                    failure_detail=str(exc),
                    target_locator=(
                        "associated popup option with exact label "
                        f"{value!r}"
                    ),
                    selection_history=selection_history,
                    selection_state=(
                        "inspected"
                        if selected is not None
                        else "unavailable"
                    ),
                )
            raise

    for attempt in range(2):
        if not await _slicer_dropdown_is_open(combo):
            await _click_slicer_control(combo)
            await _wait_for_slicer_open(page, combo)

        options = await _options_for_combobox(frame, combo)
        try:
            selected = await _radio_dropdown_selection(
                page,
                frame,
                combo,
                str(filter_item.get("name") or ""),
            )
            last_inspection_error = None
            record_selection(attempt, "before_click", selected)
        except FilterSelectionInspectionError as exc:
            last_inspection_error = str(exc)
            record_selection(attempt, "before_click", error=last_inspection_error)
            continue
        if (
            len(selected) == 1
            and _normalized_control_text(value)
            == _normalized_control_text(selected[0])
        ):
            return selected

        option = await find_option(options)
        if option is None and not search_action_attempted:
            search_action_attempted = True
            search_field = await _combobox_search_field(frame, combo)
            if search_field is None and await _activate_combobox_search_action(
                frame, combo
            ):
                search_field = await _combobox_search_field(frame, combo)
            if search_field is not None:
                await search_field.fill(value)
                for _ in range(10):
                    await page.wait_for_timeout(100)
                    options = await _options_for_combobox(frame, combo)
                    option = await find_option(options)
                    if option is not None:
                        break
                if option is None:
                    await search_field.fill("")
                    for _ in range(20):
                        await page.wait_for_timeout(100)
                        options = await _options_for_combobox(frame, combo)
                        option = await find_option(options)
                        if option is not None:
                            break

        option, scroll_state = await _scroll_filter_option_into_view(
            page,
            frame,
            combo,
            value,
            str(filter_item.get("name") or ""),
            [
                label
                for label in filter_item.get("values", [])
                if isinstance(label, str)
            ],
        )
        last_scroll_state = scroll_state
        if option is not None:
            options = await _options_for_combobox(frame, combo)
            option = await find_option(options)
        if option is None:
            last_selection_issue = str(
                scroll_state.get("reason") or "option locator was unavailable"
            )
            if scroll_state:
                last_selection_issue += f"; scroll state={scroll_state!r}"
            if attempt == 0 and await _slicer_dropdown_is_open(combo):
                await _close_slicer_dropdown(page, combo)
                search_action_attempted = False
            await page.wait_for_timeout(150)
            continue
        is_visible = await option.is_visible()
        is_enabled = await option.is_enabled()
        if (
            not scroll_state.get("visible")
            or not is_visible
            or not is_enabled
        ):
            last_selection_issue = (
                "exact option did not become visible and enabled in its "
                f"listbox; state={scroll_state!r}; visible={is_visible}; "
                f"enabled={is_enabled}"
            )
            await page.wait_for_timeout(150)
            continue

        click_target = option
        last_click_target = "role=option row click"
        radio_controls = option.locator(
            'input[type="radio"], [role="radio"]'
        )
        for radio_index in range(await radio_controls.count()):
            radio = radio_controls.nth(radio_index)
            if await radio.is_visible() and await radio.is_enabled():
                click_target = radio
                last_click_target = "native or accessible radio control"
                break
        else:
            labels = option.locator("label")
            for label_index in range(await labels.count()):
                label = labels.nth(label_index)
                if await label.is_visible() and await label.is_enabled():
                    click_target = label
                    last_click_target = "associated label"
                    break

        try:
            # Power BI report overlays can intercept pointer hit-testing for
            # its custom listbox; the target is already scoped, unique,
            # visible, and enabled above.
            await click_target.click(force=True, timeout=5_000)
        except PlaywrightError as exc:
            last_click_error = str(exc)
            last_selection_issue = (
                f"click on {last_click_target} failed: {last_click_error}"
            )
            continue

        last_selection_issue = (
            f"clicked {last_click_target}, but {value!r} did not become the "
            "sole selection"
        )
        await page.wait_for_timeout(150)

        selection_deadline = time.monotonic() + 3
        while time.monotonic() < selection_deadline:
            try:
                selected = await _radio_dropdown_selection(
                    page,
                    frame,
                    combo,
                    str(filter_item.get("name") or ""),
                )
                last_inspection_error = None
            except FilterSelectionInspectionError as exc:
                last_inspection_error = str(exc)
                record_selection(
                    attempt, "after_click", error=last_inspection_error
                )
                break
            record_selection(attempt, "after_click", selected)
            if (
                len(selected) == 1
                and _normalized_control_text(value)
                == _normalized_control_text(selected[0])
            ):
                return selected
            await page.wait_for_timeout(150)

    if diagnostic_directory is not None:
        selection_diagnostics = (
            {"selection_state": "unavailable"}
            if last_inspection_error
            else {
                "selected_values": selected,
                "selection_state": "inspected",
            }
        )
        await _save_filter_inventory_diagnostics(
            page,
            combo,
            str(filter_item.get("name") or ""),
            diagnostic_directory,
            requested_option=value,
            target_locator=(
                "associated popup option with exact label "
                f"{value!r}"
            ),
            failure_detail=(
                (
                    "Selection state remained unavailable after two "
                    f"inspection attempts: {last_inspection_error}; "
                    if last_inspection_error
                    else "Option did not become the sole selected value "
                    "after two selection attempts; "
                )
                +
                f"last click target: {last_click_target}; "
                f"selection issue: {last_selection_issue}; "
                f"last click error: {last_click_error!r}"
            ),
            selection_history=selection_history,
            last_scroll_state=last_scroll_state,
            **selection_diagnostics,
        )
    if last_inspection_error:
        raise RuntimeError(
            f"Radio option {value!r} could not be verified in "
            f"{filter_item.get('name')!r}; selection state inspection failed: "
            f"{last_inspection_error}; last click error: "
            f"{last_click_error!r}; last click target: "
            f"{last_click_target}; selection issue: {last_selection_issue}"
        )
    if (
        last_scroll_state
        and last_scroll_state.get("reason")
        in {
            "exact option is not rendered and the active popup has no scrollable content",
            "exact option is absent from the active popup even though its inventory position falls within the currently rendered range",
        }
    ):
        availability_detail = (
            "the popup has no scrollable content"
            if last_scroll_state.get("reason")
            == "exact option is not rendered and the active popup has no scrollable content"
            else
            "its inventory position is already within the visible range, "
            "but the exact label is absent"
        )
        raise UnavailableFilterOptionError(
            f"Option {value!r} is unavailable in the active popup for filter "
            f"{filter_item.get('name')!r}; selected values: {selected!r}; "
            "visible options: "
            f"{last_scroll_state.get('visible_options', [])!r}; "
            f"{availability_detail}"
        )
    raise RuntimeError(
        f"Radio option {value!r} did not become the only selection in "
        f"{filter_item.get('name')!r}; selected values: {selected!r}; "
        f"last click error: {last_click_error!r}; "
        f"last click target: {last_click_target}; "
        f"selection issue: {last_selection_issue}"
    )


async def _combobox_search_field(frame: Any, combo: Any) -> Any | None:
    async def matching_field(container: Any) -> Any | None:
        fields = container.locator("input, [role='searchbox']")
        for index in range(await fields.count()):
            field = fields.nth(index)
            if not await field.is_visible():
                continue
            metadata = await field.evaluate(
                """element => ({
                  label: element.getAttribute('aria-label') || '',
                  placeholder: element.getAttribute('placeholder') || '',
                  role: element.getAttribute('role') || '',
                  type: element.getAttribute('type') || ''
                })"""
            )
            if any(
                "search" in str(metadata.get(key, "")).casefold()
                for key in ("label", "placeholder", "role", "type")
            ):
                return field
        return None

    controls = [
        *(
            await combo.get_attribute("aria-controls") or ""
        ).split(),
        *(
            await combo.get_attribute("aria-owns") or ""
        ).split(),
    ]
    for control_id in dict.fromkeys(controls):
        popup = frame.locator(f"[id={json.dumps(control_id)}]")
        if not await popup.count():
            continue
        field = await matching_field(popup)
        if field is not None:
            return field

    combo_box = await combo.bounding_box()
    if combo_box is None:
        return None
    fields = frame.locator("input, [role='searchbox']")
    for index in range(await fields.count()):
        field = fields.nth(index)
        if not await field.is_visible():
            continue
        metadata = await field.evaluate(
            """element => ({
              label: element.getAttribute('aria-label') || '',
              placeholder: element.getAttribute('placeholder') || '',
              role: element.getAttribute('role') || '',
              type: element.getAttribute('type') || ''
            })"""
        )
        if not any(
            "search" in str(metadata.get(key, "")).casefold()
            for key in ("label", "placeholder", "role", "type")
        ):
            continue
        field_box = await field.bounding_box()
        if field_box is None:
            continue
        field_bottom = field_box["y"] + field_box["height"]
        if field_bottom < combo_box["y"] - 30:
            continue
        center_x = combo_box["x"] + combo_box["width"] / 2
        center_y = combo_box["y"] + combo_box["height"] / 2
        horizontal_gap = max(
            field_box["x"] - center_x,
            0,
            center_x - (field_box["x"] + field_box["width"]),
        )
        vertical_gap = max(
            field_box["y"] - center_y,
            0,
            center_y - (field_box["y"] + field_box["height"]),
        )
        if horizontal_gap <= 120 and vertical_gap <= 200:
            return field
    return None


async def _activate_combobox_search_action(frame: Any, combo: Any) -> bool:
    combo_box = await combo.bounding_box()
    if combo_box is None:
        return False
    search_actions = frame.get_by_text("Search", exact=True)
    center_x = combo_box["x"] + combo_box["width"] / 2
    center_y = combo_box["y"] + combo_box["height"] / 2
    for index in range(await search_actions.count()):
        action = search_actions.nth(index)
        if not await action.is_visible():
            continue
        metadata = await action.evaluate(
            """element => {
              const choice = element.closest(
                '[role="radio"], [role="checkbox"], ' +
                'input[type="radio"], input[type="checkbox"]'
              );
              return {
                role: element.getAttribute('role') || '',
                hasChoiceControl: !!choice ||
                  element.hasAttribute('aria-checked')
              };
            }"""
        )
        if metadata.get("hasChoiceControl"):
            continue
        action_box = await action.bounding_box()
        if action_box is None:
            continue
        horizontal_gap = max(
            action_box["x"] - center_x,
            0,
            center_x - (action_box["x"] + action_box["width"]),
        )
        vertical_gap = max(
            action_box["y"] - center_y,
            0,
            center_y - (action_box["y"] + action_box["height"]),
        )
        if horizontal_gap > 120 or vertical_gap > 400:
            continue
        await action.click(force=True, timeout=5_000)
        return True
    return False


async def _click_slicer_control(locator: Any) -> None:
    await locator.click(force=True, timeout=SLICER_CONTROL_ACTION_TIMEOUT_MS)


async def _slicer_dropdown_is_open(locator: Any) -> bool:
    if await locator.get_attribute("aria-expanded") == "true":
        return True
    if not hasattr(locator, "evaluate"):
        return False
    return await locator.evaluate(
        """element => {
          const visible = node => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 &&
              style.display !== 'none' && style.visibility !== 'hidden' &&
              node.getAttribute('aria-hidden') !== 'true';
          };
          const popupIds = [
            ...(element.getAttribute('aria-controls') || '').split(/\\s+/),
            ...(element.getAttribute('aria-owns') || '').split(/\\s+/)
          ].filter(Boolean);
          const popupRoles =
            '[role="listbox"], [role="menu"], [role="tree"], [role="dialog"]';
          const choices =
            '[role="option"], [role="radio"], [role="checkbox"], ' +
            '[role="treeitem"], [role="menuitem"], ' +
            'input[type="radio"], input[type="checkbox"]';
          return popupIds.some(id => {
            const popup = document.getElementById(id);
            if (!visible(popup)) return false;
            if (popup.matches(popupRoles)) return true;
            return [...popup.querySelectorAll(popupRoles)].some(container =>
              visible(container) &&
              (container.matches(choices) || container.querySelector(choices))
            );
          });
        }"""
    )


async def _wait_for_slicer_open(page: Any, locator: Any) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if await _slicer_dropdown_is_open(locator):
            return
        await page.wait_for_timeout(100)
    raise RuntimeError("Power BI dropdown did not open after activating its combobox")


async def _close_slicer_dropdown(page: Any, locator: Any) -> None:
    if not await _slicer_dropdown_is_open(locator):
        return
    await locator.press("Escape")
    await page.wait_for_timeout(100)
    if await _slicer_dropdown_is_open(locator):
        await _click_slicer_control(locator)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not await _slicer_dropdown_is_open(locator):
            return
        await page.wait_for_timeout(100)
    raise RuntimeError("Power BI dropdown remained open after closing the slicer")


async def _wait_for_filter_selection(
    page: Page,
    filter_item: dict[str, Any],
    value: str,
) -> list[str] | None:
    deadline = time.monotonic() + 8
    current: list[str] | None = None
    while time.monotonic() < deadline:
        current = await _current_filter_value(page, filter_item)
        if current and _filter_selection_matches(filter_item, value, current):
            return current
        await page.wait_for_timeout(200)
    return current


async def _wait_for_state_render(
    page: Page,
    settle_seconds: float,
) -> float:
    wait_seconds = max(0.0, settle_seconds)

    started_at = time.monotonic()
    minimum_wait = min(0.5, wait_seconds)
    stable_clear_seconds = max(0.3, wait_seconds) if wait_seconds else 0.0
    observed_strong_loading = False
    observed_class_loading = False
    observed_change = False
    observed_unrendered_chart = False
    previous_signature: tuple[str, ...] | None = None
    stable_since: float | None = None

    while True:
        frames = getattr(page, "frames", None)
        states = []
        if frames is None:
            states.append(await page.evaluate(_REPORT_LOAD_STATE_SCRIPT))
        else:
            for frame in frames:
                states.append(
                    await frame.evaluate(_REPORT_LOAD_STATE_SCRIPT)
                )
        report_states = [
            state for state in states if state.get("visuals", 0) > 0
        ]
        relevant_states = report_states or states[:1]
        indicator_states = states[:1] + [
            state for state in report_states if state is not states[0]
        ]
        render_errors = {
            error
            for state in states
            for error in state.get("render_errors", [])
        }
        if render_errors:
            logger.warning(
                "Power BI reported terminal visual errors: %s",
                sorted(render_errors),
            )
            return time.monotonic() - started_at
        loading = any(state["busy"] for state in indicator_states)
        unrendered_chart = any(
            state.get("unrendered_chart_visuals")
            for state in relevant_states
        )
        observed_unrendered_chart |= unrendered_chart
        signal_strengths = [
            _loading_signal_strength(state) for state in indicator_states
        ]
        strong_loading = any(strong for strong, _ in signal_strengths)
        class_loading = any(class_only for _, class_only in signal_strengths)
        signature = tuple(
            f"{state.get('visuals', 0)}|{state['content']}"
            for state in relevant_states
        )
        now = time.monotonic()
        elapsed = now - started_at
        if loading:
            observed_strong_loading |= strong_loading
            observed_class_loading |= class_loading
            stable_since = None
        if (
            not class_loading
            and not unrendered_chart
            and signature != previous_signature
        ):
            if previous_signature is not None:
                observed_change = True
            stable_since = now
        elif (
            not class_loading
            and not unrendered_chart
            and stable_since is None
        ):
            stable_since = now
        if (
            not loading
            and not unrendered_chart
            and stable_since is not None
            and elapsed >= minimum_wait
            and now - stable_since >= stable_clear_seconds
        ):
            return elapsed
        previous_signature = signature

        max_wait = (
            UNRENDERED_CHART_TIMEOUT_MS / 1_000
            if unrendered_chart
            else CLASS_ONLY_RENDER_TIMEOUT_MS / 1_000
            if observed_class_loading and not observed_strong_loading
            and not observed_unrendered_chart
            else REPORT_READY_TIMEOUT_MS / 1_000
            if (
                observed_strong_loading
                or observed_class_loading
                or observed_unrendered_chart
            )
            else max(wait_seconds, MAX_RENDER_WAIT_SECONDS)
            if observed_change
            else wait_seconds
        )
        remaining = max_wait - elapsed
        if remaining <= 0:
            if unrendered_chart:
                logger.warning(
                    "Continuing capture after %.0f seconds with chart visuals "
                    "that have not rendered data marks",
                    elapsed,
                )
                return time.monotonic() - started_at
            if loading and class_loading and not strong_loading:
                logger.warning(
                    "Continuing capture after %d seconds with class-only "
                    "Power BI loading indicators still active",
                    CLASS_ONLY_RENDER_TIMEOUT_MS // 1_000,
                )
                return time.monotonic() - started_at
            if loading or observed_change:
                raise RuntimeError(
                    "Dashboard visuals or visible content did not finish loading "
                    f"within {max_wait:.0f} seconds "
                    f"(loading={loading}, content_changed={observed_change}, "
                    f"visuals={sum(state.get('visuals', 0) for state in relevant_states)}, "
                    "busy_details="
                    f"{[state.get('busy_details', {}) for state in indicator_states]!r})"
                )
            return time.monotonic() - started_at
        await page.wait_for_timeout(min(100, max(1, int(remaining * 1000))))


async def _read_render_warning(page: Any) -> dict[str, Any] | None:
    frames = getattr(page, "frames", None)
    if frames is None:
        states = [await page.evaluate(_REPORT_LOAD_STATE_SCRIPT)]
    else:
        states = [
            await frame.evaluate(_REPORT_LOAD_STATE_SCRIPT)
            for frame in frames
        ]
    report_states = [state for state in states if state.get("visuals", 0) > 0]
    relevant_states = report_states or states[:1]
    errors = sorted(
        {
            error
            for state in states
            for error in state.get("render_errors", [])
        }
    )
    active_indicators = [
        state
        for state in relevant_states
        if state.get("busy") or state.get("unrendered_chart_visuals")
    ]
    if not errors and not active_indicators:
        return None
    if errors:
        reason = "Power BI query exceeded available resources"
    elif any(state.get("unrendered_chart_visuals") for state in active_indicators):
        reason = "Power BI chart content did not finish rendering"
    else:
        reason = "Power BI visual loading indicators remained active"
    return {
        "reason": reason,
        "errors": errors,
        "indicators": [
            {
                "visuals": state.get("visuals", 0),
                "busy_details": state.get("busy_details", {}),
                "unrendered_chart_visuals": state.get(
                    "unrendered_chart_visuals", []
                ),
            }
            for state in active_indicators
        ],
    }


def _loading_signal_strength(state: dict[str, Any]) -> tuple[bool, bool]:
    busy = int(state.get("busy", 0) or 0)
    if not busy:
        return False, False
    details = state.get("busy_details")
    if not isinstance(details, dict):
        return True, False

    explicit = int(details.get("explicit_indicators", 0) or 0)
    images = int(details.get("incomplete_visual_images", 0) or 0)
    refresh = int(details.get("rotating_refresh_controls", 0) or 0)
    class_indicators = int(details.get("visual_loading_classes", 0) or 0)
    strong = bool(explicit or images or refresh)
    class_only = bool(class_indicators and not strong)
    if not strong and not class_only:
        strong = True
    return strong, class_only


async def _extract_or_reuse(
    screenshot_bytes: bytes,
    page_name: str,
    extraction_cache: dict[str, dict[str, Any]],
    exported_visual_titles: list[str] | None = None,
    performance_seconds: dict[str, float] | None = None,
    dashboard: str | None = None,
    job_id: str = "standalone",
    operation_log_path: Path | None = None,
    page: str | None = None,
    filter_state: str | None = None,
) -> dict[str, Any]:
    cache_key = hashlib.sha256(
        page_name.encode("utf-8")
        + b"\0"
        + screenshot_bytes
        + b"\0"
        + json.dumps(
            sorted(
                title.casefold().strip()
                for title in exported_visual_titles or []
                if isinstance(title, str) and title.strip()
            ),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    if cache_key not in extraction_cache:
        extraction_started_at = time.perf_counter()
        try:
            with measure_stage(
                operation_log_path,
                job_id=job_id,
                operation="gemini_screenshot_extraction",
                dashboard=dashboard,
                page=page or page_name,
                filter_state=filter_state,
            ):
                model_timing_context = (
                    {
                        "job_id": job_id,
                        "operation_log_path": operation_log_path,
                        "dashboard": dashboard,
                        "filter_state": filter_state,
                    }
                    if operation_log_path is not None
                    else {}
                )
                if exported_visual_titles:
                    extraction_cache[cache_key] = await extract_screenshot(
                        screenshot_bytes,
                        page_name,
                        exported_visual_titles,
                        **model_timing_context,
                    )
                else:
                    extraction_cache[cache_key] = await extract_screenshot(
                        screenshot_bytes, page_name, **model_timing_context
                    )
        finally:
            if performance_seconds is not None:
                performance_seconds["extraction_model_seconds"] = (
                    performance_seconds.get("extraction_model_seconds", 0.0)
                    + time.perf_counter()
                    - extraction_started_at
                )
    elif performance_seconds is not None:
        performance_seconds["extraction_cache_hits"] = (
            performance_seconds.get("extraction_cache_hits", 0.0) + 1
        )
    return extraction_cache[cache_key]


async def _accessible_tab_names(frame: Any) -> list[str]:
    labels = await frame.locator('[role="tab"]').evaluate_all(
        """elements => elements.slice(0, 100).map(element =>
          element.getAttribute('aria-label') ||
          element.innerText ||
          element.textContent ||
          ''
        )"""
    )
    names: list[str] = []
    for label in labels:
        if not isinstance(label, str):
            continue
        name = re.sub(
            r"\s+Selected$", "", label.strip(), flags=re.IGNORECASE
        ).strip()
        if name and name not in names:
            names.append(name)
    return names


async def _wait_for_comparable_report_tabs(page: Page) -> list[str]:
    deadline = time.monotonic() + MAX_PAGE_TAB_WAIT_SECONDS
    latest_names: list[str] = []
    while True:
        latest_names = []
        for frame in page.frames:
            for name in await _accessible_tab_names(frame):
                if name not in latest_names:
                    latest_names.append(name)
        if (
            not latest_names
            or _comparable_report_page_names(latest_names)
            or time.monotonic() >= deadline
        ):
            return latest_names
        await page.wait_for_timeout(250)


async def _accessible_inventory(
    page: Page,
    diagnostic_directory: Path | None = None,
    *,
    clear_selection_actions: bool = True,
    inspect_filter_options: bool = True,
) -> dict[str, Any]:
    frames: list[dict[str, Any]] = []
    for frame_index, frame in enumerate(page.frames):
        try:
            items = await frame.locator(
                "button, [role], select, input, textarea"
            ).evaluate_all(
                """elements => elements.slice(0, 500).map(el => {
                  const role = el.getAttribute('role') ||
                    (el.tagName === 'BUTTON' ? 'button' :
                     el.tagName === 'SELECT' ? 'select' : el.tagName.toLowerCase());
                  const labels = el.labels ? [...el.labels].map(x => x.innerText.trim()) : [];
                  return {
                    role,
                    name: el.getAttribute('aria-label') ||
                      el.getAttribute('title') || labels.join(' ') ||
                      el.innerText?.trim().slice(0, 120) ||
                      el.getAttribute('placeholder') || '',
                    tag: el.tagName.toLowerCase(),
                    values: el.tagName === 'SELECT'
                      ? [...el.options].map(o => o.label || o.textContent.trim()).filter(Boolean)
                      : []
                  };
                }).filter(item => item.name || item.values.length)"""
            )
            frames.append({"index": frame_index, "url": frame.url, "controls": items})
        except Exception as exc:
            raise RuntimeError(
                f"Could not inspect Power BI frame {frame_index} ({frame.url}): {exc}"
            ) from exc

    tabs: list[str] = []
    for frame in page.frames:
        for label in await _accessible_tab_names(frame):
            if label not in tabs:
                tabs.append(label)

    filters: list[dict[str, Any]] = []
    cleared_slicers: list[str] = []

    def was_cleared(name: str) -> bool:
        return any(
            existing.casefold() == name.casefold()
            for existing in cleared_slicers
        )

    for frame in page.frames:
        selects = frame.locator("select")
        for index in range(await selects.count()):
            locator = selects.nth(index)
            selector_name = (
                await locator.get_attribute("aria-label")
                or await locator.get_attribute("title")
                or ""
            )
            if not selector_name:
                selector_name = await locator.evaluate(
                    """el => {
                      const label = el.labels && [...el.labels]
                        .map(item => item.innerText.trim()).filter(Boolean).join(' ');
                      return label || '';
                    }"""
                )
            name = selector_name
            if not name or not await locator.is_visible():
                continue
            if name and was_cleared(name):
                continue
            if name and clear_selection_actions:
                await _click_slicer_control(locator)
                clear_button = await _clear_selection_button(
                    frame,
                    {"name": name, "selector_name": selector_name},
                    control=locator,
                )
                if clear_button is not None:
                    await _click_clear_selection_button(clear_button)
                    cleared_slicers.append(name)
                    continue
            values = await locator.locator("option").all_text_contents()
            values = [value.strip() for value in values if value.strip()]
            if not clear_selection_actions:
                values = [
                    value
                    for value in values
                    if not _is_clear_selection_label(value)
                ]
            if name and values:
                selected = await locator.locator("option:checked").all_text_contents()
                if not clear_selection_actions:
                    selected = [
                        value
                        for value in selected
                        if not _is_clear_selection_label(value.strip())
                    ]
                await locator.press("Escape")
                filters.append(
                    {
                        "name": name,
                        "selector_name": selector_name,
                        "selected": [value.strip() for value in selected],
                        "values": values,
                        "clear_selection_checked": True,
                    }
                )

        processed_combo_keys: set[tuple[str, str, int]] = set()
        while True:
            combos = frame.get_by_role("combobox")
            combo_metadata = await combos.evaluate_all(
                COMBOBOX_METADATA_SCRIPT
            )
            occurrences: dict[tuple[str, str], int] = {}
            candidate = None
            for item in combo_metadata:
                name = (item.get("name") or "").strip()
                selector_name = (item.get("selectorName") or "").strip()
                if not name or any(
                    existing["name"].casefold() == name.casefold()
                    for existing in filters
                ) or was_cleared(name):
                    continue
                identity = (name.casefold(), selector_name.casefold())
                occurrence = occurrences.get(identity, 0)
                occurrences[identity] = occurrence + 1
                key = (*identity, occurrence)
                if key not in processed_combo_keys:
                    candidate = (item, key)
                    break
            if candidate is None:
                break
            item, candidate_key = candidate
            name = item["name"].strip()
            selector_name = item["selectorName"].strip()
            processed_combo_keys.add(candidate_key)
            combo = combos.nth(item["index"])
            if not await combo.is_visible():
                continue
            filter_probe = {
                "name": name,
                "selector_name": selector_name or name,
            }
            if not inspect_filter_options:
                summary = (await combo.inner_text()).strip()
                filters.append(
                    {
                        **filter_probe,
                        "selected": _visible_selected_values(
                            name, selector_name, summary
                        ),
                        "values": [],
                        "clear_selection_checked": False,
                        "option_inventory_status": "skipped",
                        "option_inventory_note": (
                            "Dropdown was not opened; only the visible "
                            "selection was captured."
                        ),
                    }
                )
                continue
            await _click_slicer_control(combo)
            await _wait_for_slicer_open(page, combo)
            (
                option_locators,
                values,
                selected,
                checkbox_choices,
                radio_choices,
            ) = await _wait_for_combobox_options(
                page, frame, combo, filter_probe
            )
            choice_type = None
            control_type = None
            associated_listbox_options = await _associated_listbox_options(
                frame, combo, filter_probe
            )
            if (
                associated_listbox_options is not None
                and associated_listbox_options[2] is not None
            ):
                values, selected, control_type = associated_listbox_options
                if control_type == "checkbox_dropdown":
                    choice_type = "checkbox"
                    choices = checkbox_choices
                else:
                    choice_type = "radio"
                    choices = radio_choices
                inventory_choices = []
            elif radio_choices:
                choice_type = "radio"
                control_type = "radio_dropdown"
                choices = radio_choices
                inventory_choices = radio_choices
            elif checkbox_choices:
                choice_type = "checkbox"
                control_type = "checkbox_dropdown"
                choices = checkbox_choices
                inventory_choices = checkbox_choices
            else:
                choices = []
                inventory_choices = []
                if associated_listbox_options is not None:
                    values, selected, control_type = (
                        associated_listbox_options
                    )
                    if control_type == "checkbox_dropdown":
                        choice_type = "checkbox"
                    elif control_type == "radio_dropdown":
                        choice_type = "radio"
                else:
                    single_select_options = (
                        await _single_select_dropdown_options(
                            frame, combo, filter_probe
                        )
                    )
                    if single_select_options is not None:
                        values, selected = single_select_options
                        choice_type = "radio"
                        control_type = "radio_dropdown"
            values, selected = _filter_inventory_option_values(
                values,
                selected,
                inventory_choices,
                name,
                selector_name,
            )
            if control_type is not None:
                filter_probe["control_type"] = control_type
            if clear_selection_actions:
                clear_button = await _clear_selection_button(
                    frame, filter_probe, control=combo
                )
                if clear_button is not None:
                    await _click_clear_selection_button(clear_button)
                    cleared_slicers.append(name)
                    continue
                if choices:
                    clear_choice = next(
                        (
                            choice
                            for choice in choices
                            if _is_clear_selection_label(choice["label"])
                        ),
                        None,
                    )
                    if clear_choice is not None:
                        choice_locator = frame.locator(
                            '[role="radio"], input[type="radio"]'
                            if choice_type == "radio"
                            else '[role="checkbox"], input[type="checkbox"]'
                        ).nth(clear_choice["index"])
                        await choice_locator.click(
                            force=True, timeout=5_000
                        )
                        cleared_slicers.append(name)
                        continue
                clear_value = next(
                    (
                        value
                        for value in values
                        if _is_clear_selection_label(value)
                    ),
                    None,
                )
                if clear_value is not None:
                    await _click_filter_option(
                        page,
                        frame,
                        combo,
                        clear_value,
                        filter_name=name,
                        expected_options=values,
                    )
                    cleared_slicers.append(name)
                    continue
            else:
                values = [
                    value
                    for value in values
                    if not _is_clear_selection_label(value)
                ]
                if selected is not None:
                    selected = [
                        value
                        for value in selected
                        if not _is_clear_selection_label(value)
                    ]
            summary = (await combo.inner_text()).strip()
            if values:
                await combo.press("Escape")
                filter_entry = {
                    "name": name,
                    "selector_name": selector_name or name,
                    "selected": (
                        selected
                        if control_type is not None
                        else _selected_values_with_summary(
                            selected, summary, values
                        )
                    ),
                    "values": values,
                    "clear_selection_checked": True,
                }
                if control_type is not None:
                    filter_entry["control_type"] = control_type
                filters.append(filter_entry)
            else:
                visible_selection = _selected_values_with_summary(
                    selected, summary, values
                )
                if visible_selection and all(
                    _is_aggregate_value(value)
                    for value in visible_selection
                ):
                    await combo.press("Escape")
                    filters.append(
                        {
                            "name": name,
                            "selector_name": selector_name or name,
                            "selected": visible_selection,
                            "values": [],
                            "clear_selection_checked": True,
                            "option_inventory_status": "unavailable",
                            "option_inventory_note": (
                                "The popup exposed no readable options; "
                                "the visible aggregate selection was retained."
                            ),
                            **(
                                {"control_type": control_type}
                                if control_type is not None
                                else {}
                            ),
                        }
                    )
                    logger.warning(
                        "Could not read options for filter %r; preserving "
                        "visible aggregate selection %r and skipping its "
                        "filter-value cases",
                        name,
                        visible_selection,
                    )
                    continue
                diagnostic_details = ""
                if diagnostic_directory is not None:
                    screenshot_path, diagnostics_path = (
                        await _save_filter_inventory_diagnostics(
                            page,
                            combo,
                            name,
                            diagnostic_directory,
                        )
                    )
                    diagnostic_details = (
                        f"; popup diagnostics saved to "
                        f"{diagnostics_path} and {screenshot_path}"
                    )
                await combo.press("Escape")
                raise RuntimeError(
                    f"Could not read any options from visible filter "
                    f"{name!r} after opening its combobox"
                    f"{diagnostic_details}"
                )

    for control_type, options_for_frame in (
        ("checkbox", _checkbox_filter_options),
        ("radio", _radio_filter_options),
    ):
        choice_groups: dict[tuple[str, str], dict[str, Any]] = {}
        for frame_index, frame in enumerate(page.frames):
            for option in await options_for_frame(frame):
                name = option["name"].strip()
                value = option["label"]
                visual_id = option.get("visualId") or ""
                key = (str(frame_index), visual_id or name.casefold())
                group = choice_groups.setdefault(
                    key,
                    {
                        "name": name,
                        "selector_name": name,
                        "visual_id": visual_id,
                        "control_type": control_type,
                        "selected": [],
                        "values": [],
                    },
                )
                if value not in group["values"]:
                    group["values"].append(value)
                if option["selected"] and value not in group["selected"]:
                    group["selected"].append(value)
        for key, item in choice_groups.items():
            if not item["values"] or any(
                existing["name"].casefold() == item["name"].casefold()
                for existing in filters
            ) or was_cleared(item["name"]):
                continue
            frame = page.frames[int(key[0])]
            if clear_selection_actions:
                clear_button = await _clear_selection_button(frame, item)
                if clear_button is not None:
                    await _click_clear_selection_button(clear_button)
                    cleared_slicers.append(item["name"])
                    continue
                clear_choice = next(
                    (
                        value
                        for value in item["values"]
                        if _is_clear_selection_label(value)
                    ),
                    None,
                )
                if clear_choice is not None:
                    if control_type == "checkbox":
                        choice = next(
                            option
                            for option in await _checkbox_filter_options(
                                frame, item
                            )
                            if _is_clear_selection_label(option["label"])
                        )
                        checkboxes = frame.locator(
                            '[role="checkbox"], input[type="checkbox"]'
                        )
                        await checkboxes.nth(choice["index"]).click(
                            force=True, timeout=5_000
                        )
                    else:
                        radios = frame.locator(
                            '[role="radio"], input[type="radio"]'
                        )
                        choice = next(
                            option
                            for option in await _radio_filter_options(
                                frame, item
                            )
                            if _is_clear_selection_label(option["label"])
                        )
                        await radios.nth(choice["index"]).click(
                            force=True, timeout=5_000
                        )
                    cleared_slicers.append(item["name"])
                    continue
            else:
                item["values"] = [
                    value
                    for value in item["values"]
                    if not _is_clear_selection_label(value)
                ]
                item["selected"] = [
                    value
                    for value in item["selected"]
                    if not _is_clear_selection_label(value)
                ]
            item["clear_selection_checked"] = True
            filters.append(item)

    for frame in page.frames:
        for group in await _bookmark_button_groups(frame):
            if any(
                existing["name"].casefold() == group["name"].casefold()
                for existing in filters
            ):
                continue
            filters.append(
                {
                    "name": group["name"],
                    "selector_name": group["name"],
                    "control_type": "bookmark_buttons",
                    "selected": group["selected"],
                    "values": group["values"],
                }
            )

    controls = [
        control
        for frame in frames
        for control in frame["controls"]
    ][:MAX_DOM_CONTROLS]
    return {
        "pages": tabs,
        "filters": filters,
        "frames": frames,
        "controls": controls,
        "initial_cleared_slicers": cleared_slicers,
    }


_REPORT_ARTIFACT_TITLE_SCRIPT = """() => {
  const isVisible = element => {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 &&
      style.display !== 'none' &&
      style.visibility !== 'hidden' &&
      element.getAttribute('aria-hidden') !== 'true';
  };
  for (const element of document.querySelectorAll(
    '[data-testid="artifact-info-title"]'
  )) {
    if (!isVisible(element)) continue;
    const title = (element.innerText || element.textContent || '')
      .replace(/\\s+/g, ' ')
      .trim();
    if (title) return title;
  }
  return null;
}"""


_REPORT_LOAD_STATE_SCRIPT = """() => {
  const main = document.querySelector('[role="main"]') || document.body;
  const visible = element => {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 &&
      style.display !== 'none' &&
      style.visibility !== 'hidden' &&
      element.getAttribute('aria-hidden') !== 'true';
  };
  const visuals = [
    ...main.querySelectorAll('div.visualContainer, [data-visual-id]')
  ].filter(visible);
  const busySelectors = [
    '[aria-busy="true"]',
    '[role="progressbar"]',
    'spinner[title*="loading" i]',
    '[data-testid*="loading" i]'
  ];
  const explicitBusyElements = new Set(
    busySelectors.flatMap(selector =>
      [...main.querySelectorAll(selector)].filter(visible)
    )
  );
  const busyElements = new Set(explicitBusyElements);
  const visualLoadingCandidates = new Set(visuals.flatMap(visual => [
    ...(visual.matches('[class*="spinner" i], [class*="loading" i]')
      ? [visual]
      : []),
    ...visual.querySelectorAll('[class*="spinner" i], [class*="loading" i]')
  ]).filter(visible));
  const visualLoadingElements = [...visualLoadingCandidates].filter(element =>
    element.getAnimations({ subtree: true }).some(animation =>
      animation.playState === 'running' &&
      animation.effect?.getTiming().iterations === Infinity
    )
  );
  const visualLoadingMarkers = visualLoadingElements.map(element => ({
    tag: element.tagName,
    class_name: (element.getAttribute('class') || '').slice(0, 120),
    visual_index: visuals.findIndex(visual =>
      visual === element || visual.contains(element)
    ) + 1,
    visual_id: element.closest('[data-visual-id]')?.getAttribute(
      'data-visual-id'
    ) || ''
  }));
  visualLoadingElements.forEach(element => busyElements.add(element));
  const refreshControls = [
    ...main.querySelectorAll('button, [role="button"]')
  ].filter(element => {
    if (!visible(element)) return false;
    const label = [
      element.getAttribute('aria-label'),
      element.getAttribute('title'),
      element.innerText
    ].filter(Boolean).join(' ');
    return /refresh|reload/i.test(label) &&
      element.getAnimations({ subtree: true }).some(animation => {
        if (animation.playState !== 'running') return false;
        const timing = animation.effect?.getTiming();
        if (timing?.iterations !== Infinity) return false;
        return animation.effect?.getKeyframes().some(frame =>
          /rotate\\s*\\(/i.test(String(frame.transform || ''))
        );
      });
  });
  refreshControls.forEach(element => busyElements.add(element));
  const contentRoots = visuals.length ? visuals : [main];
  const reportText = contentRoots.map(root => [
    root.innerText || '',
    ...[...root.querySelectorAll('[aria-label], [title]')]
      .filter(visible)
      .map(element =>
        element.getAttribute('aria-label') || element.getAttribute('title')
      )
  ].join('\\u001f')).join('\\u001e');
  const renderErrorText = `${reportText}\\u001e${main.innerText || ''}`;
  const renderErrors = [];
  if (/\\bquery\\b.{0,80}\\bexceeded\\b.{0,80}\\bavailable resources\\b/i
      .test(renderErrorText)) {
    renderErrors.push('query_resource_exceeded');
  }
  const incompleteVisualImages = visuals.reduce(
    (count, visual) => count + [
      ...visual.querySelectorAll('img')
    ].filter(image => visible(image) && !image.complete).length,
    0
  );
  const isChartVisual = visual => {
    const hints = [
      visual.getAttribute('data-visual-type'),
      visual.getAttribute('data-visual-kind'),
      typeof visual.className === 'string'
        ? visual.className
        : visual.className?.baseVal || '',
      ...[...visual.querySelectorAll(
        '[data-visual-type], [data-visual-kind], [aria-label], [title], [class]'
      )].flatMap(element => [
        element.getAttribute('data-visual-type'),
        element.getAttribute('data-visual-kind'),
        element.getAttribute('aria-label'),
        element.getAttribute('title'),
        typeof element.className === 'string'
          ? element.className
          : element.className?.baseVal || ''
      ])
    ].filter(Boolean).join(' ');
    return /chart|graph|plot/i.test(hints);
  };
  const hasPlotMarks = visual => {
    const svg = visual.querySelector('svg');
    if (svg) {
      const svgBounds = svg.getBoundingClientRect();
      return [...svg.querySelectorAll(
        'path, rect, circle, ellipse, polygon, polyline'
      )].some(mark => {
        const className = String(mark.getAttribute('class') || '');
        if (/axis|grid|tick|domain|background|plotarea|legend|title/i
            .test(className)) return false;
        const bounds = mark.getBoundingClientRect();
        if (bounds.width <= 2 && bounds.height <= 2) return false;
        const namedDataMark =
          /data|mark|series|line|bar|column|area|point|slice|bubble/i
            .test(className);
        const occupiesArea =
          bounds.width > 2 &&
          bounds.height > 2 &&
          bounds.width < svgBounds.width * 0.98 &&
          bounds.height < svgBounds.height * 0.98;
        return namedDataMark || occupiesArea;
      });
    }

    const canvas = visual.querySelector('canvas');
    if (!canvas || !canvas.width || !canvas.height) return false;
    const context = canvas.getContext('2d', { willReadFrequently: true });
    if (!context) return false;
    try {
      const left = Math.floor(canvas.width * 0.1);
      const right = Math.ceil(canvas.width * 0.9);
      const top = Math.floor(canvas.height * 0.15);
      const bottom = Math.ceil(canvas.height * 0.85);
      const pixels = context.getImageData(
        left,
        top,
        Math.max(1, right - left),
        Math.max(1, bottom - top)
      ).data;
      let nonBackgroundPixels = 0;
      for (let index = 0; index < pixels.length; index += 4) {
        const maximum = Math.max(pixels[index], pixels[index + 1], pixels[index + 2]);
        const minimum = Math.min(pixels[index], pixels[index + 1], pixels[index + 2]);
        if (pixels[index + 3] > 0 && (maximum - minimum > 20 || maximum < 200)) {
          nonBackgroundPixels += 1;
          if (nonBackgroundPixels >= 8) return true;
        }
      }
    } catch {
      return false;
    }
    return false;
  };
  const unrenderedChartVisuals = visuals.flatMap((visual, index) => {
    if (!isChartVisual(visual) || hasPlotMarks(visual) ||
        /no data|no results|no records|nothing to display/i.test(
          visual.innerText || ''
        )) return [];
    return [{
      visual_index: index + 1,
      visual_id: visual.getAttribute('data-visual-id') || '',
      title: (visual.innerText || '').trim().slice(0, 120)
    }];
  });
  let contentHash = 2166136261;
  for (let index = 0; index < reportText.length; index += 1) {
    contentHash ^= reportText.charCodeAt(index);
    contentHash = Math.imul(contentHash, 16777619);
  }
  return {
    elements: visuals.reduce(
      (count, visual) => count + visual.querySelectorAll('*').length,
      0
    ),
    drawn: visuals.reduce(
      (count, visual) => count + visual.querySelectorAll(
        'svg, canvas, img, table, [role="grid"], [role="row"]'
      ).length,
      0
    ),
    visuals: visuals.length,
    busy: busyElements.size + incompleteVisualImages,
    busy_details: {
      explicit_indicators: explicitBusyElements.size,
      visual_loading_candidates: visualLoadingCandidates.size,
      visual_loading_classes: visualLoadingElements.length,
      visual_loading_markers: visualLoadingMarkers,
      incomplete_visual_images: incompleteVisualImages,
      rotating_refresh_controls: refreshControls.length
    },
    unrendered_chart_visuals: unrenderedChartVisuals,
    render_errors: renderErrors,
    content: (contentHash >>> 0).toString(16)
  };
}"""


async def _wait_for_report_load_to_settle(page: Page) -> None:
    """Wait for report readiness, deferring class-only spinners to capture."""
    started_at = time.monotonic()
    previous: str | None = None
    stable_since: float | None = None
    while True:
        state = await page.evaluate(_REPORT_LOAD_STATE_SCRIPT)
        now = time.monotonic()
        if state.get("render_errors"):
            logger.warning(
                "Power BI reported terminal visual errors: %s",
                state["render_errors"],
            )
            return
        strong_loading, class_loading = _loading_signal_strength(state)
        if class_loading and not strong_loading:
            logger.info(
                "Deferring class-only Power BI loading wait to state capture: %r",
                state.get("busy_details", {}),
            )
            return
        signature = (
            f"{state['elements']}|{state['drawn']}|{state['content']}"
        )
        if state["busy"] or signature != previous:
            stable_since = None
        elif stable_since is None:
            stable_since = now
        previous = signature
        if stable_since is not None and (
            now - stable_since >= REPORT_STABLE_SECONDS
        ):
            return
        max_wait = REPORT_READY_TIMEOUT_MS / 1_000
        if now - started_at >= max_wait:
            raise RuntimeError(
                "Power BI report did not finish loading within "
                f"{max_wait:.0f} seconds; "
                f"last observed state: {state!r}"
            )
        await page.wait_for_timeout(REPORT_POLL_MS)


async def _read_visual_layout_inventory(
    page: Page,
    filter_inventory: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    visuals: list[dict[str, Any]] = []
    filter_names = {
        str(item.get("name", "")).strip().casefold()
        for item in filter_inventory or []
        if isinstance(item, dict) and item.get("name")
    }
    for frame_index, frame in enumerate(page.frames):
        try:
            captured = await frame.evaluate(_VISUAL_LAYOUT_SCRIPT)
        except Exception as exc:
            raise RuntimeError(
                f"Could not inspect visual layout in Power BI frame "
                f"{frame_index} ({frame.url}): {exc}"
            ) from exc
        for item in captured:
            if not isinstance(item, dict):
                continue
            name = str(item.get("visual_name") or "").strip()
            if name and name.casefold() in filter_names:
                continue
            bounds = {
                key: item.get(key)
                for key in ("x", "y", "width", "height")
            }
            if not all(
                isinstance(value, (int, float)) for value in bounds.values()
            ):
                continue
            hints = [item.get("visual_type")] if item.get("visual_type") else []
            visual_type = classify_visual_type(*hints)
            visuals.append(
                {
                    "dom_visual_id": str(item.get("visual_id") or ""),
                    "visual_name": name or None,
                    "visual_type": visual_type,
                    **bounds,
                }
            )
    visuals.sort(key=lambda visual: (visual["y"], visual["x"]))
    type_counts: dict[str, int] = {}
    for visual in visuals:
        visual_type = str(visual.get("visual_type") or "").casefold()
        prefix = (
            "TABLE"
            if visual_type == "table"
            else "MATRIX"
            if visual_type == "matrix"
            else "KPI"
            if visual_type in {"kpi", "card"}
            else "CHART"
            if visual_type and visual_type != "visual"
            else "VISUAL"
        )
        type_counts[prefix] = type_counts.get(prefix, 0) + 1
        visual["visual_id"] = f"{prefix}_{type_counts[prefix]:03d}"
    return visuals


async def _read_report_title(page: Page) -> str | None:
    value = await page.evaluate(_REPORT_ARTIFACT_TITLE_SCRIPT)
    if not isinstance(value, str):
        return None
    title = re.sub(r"\s+", " ", value).strip()
    return title or None


async def _read_dom_title_items(
    page: Page,
    visuals: list[dict[str, Any]],
    filter_inventory: list[dict[str, Any]],
    filter_values: dict[str, list[str]],
) -> tuple[list[dict[str, Any]], str | None]:
    filter_names = [
        item["name"]
        for item in filter_inventory
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]
    visual_ids = {
        visual["dom_visual_id"]: visual["visual_id"]
        for visual in visuals
        if visual.get("dom_visual_id")
    }
    items: list[dict[str, Any]] = []
    filter_bounds: dict[str, dict[str, Any]] = {}
    for frame_index, frame in enumerate(page.frames):
        try:
            inventory = await frame.evaluate(
                _TITLE_LABEL_SCRIPT, filter_names
            )
        except Exception as exc:
            raise RuntimeError(
                f"Could not inspect visible report labels in Power BI frame "
                f"{frame_index} ({frame.url}): {exc}"
            ) from exc
        for item in inventory.get("items", []):
            if not isinstance(item, dict):
                continue
            associated_visual = visual_ids.get(
                str(item.get("associated_visual_dom_id") or "")
            )
            items.append(
                {
                    "text": item.get("text"),
                    "category": item.get("category"),
                    "associated_visual": associated_visual,
                    "bounding_box": item.get("bounding_box"),
                    "normalized_bounding_box": item.get(
                        "normalized_bounding_box"
                    ),
                    "evidence_sources": ["dom_accessibility"],
                    "extraction_status": "complete",
                    "uncertainties": [],
                }
            )
        for item in inventory.get("filter_bounds", []):
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                filter_bounds[item["name"].casefold()] = item

    for name in filter_names:
        if name not in filter_values:
            continue
        bounds = filter_bounds.get(name.casefold(), {})
        for value in filter_values[name]:
            text = str(value).strip()
            if not text:
                continue
            items.append(
                {
                    "text": text,
                    "category": "slicer_value",
                    "associated_visual": name,
                    "bounding_box": bounds.get("bounding_box"),
                    "normalized_bounding_box": bounds.get(
                        "normalized_bounding_box"
                    ),
                    "evidence_sources": ["accessible_filter"],
                    "extraction_status": "complete",
                    "uncertainties": [],
                }
            )

    def item_order(item: dict[str, Any], index: int) -> tuple[Any, ...]:
        box = item.get("normalized_bounding_box")
        if isinstance(box, dict) and isinstance(box.get("y"), (int, float)):
            return (0, box["y"], box.get("x", 0), index)
        return (1, index, 0, index)

    items = [
        item
        for _, item in sorted(
            enumerate(items),
            key=lambda indexed: item_order(indexed[1], indexed[0]),
        )
    ]
    for index, item in enumerate(items, 1):
        item["title_id"] = f"TITLE_{index:03d}"
        item["reading_order"] = index

    refresh_timestamp = None
    for item in items:
        if item.get("category") != "metadata_label":
            continue
        text = str(item.get("text") or "")
        match = re.search(
            r"(?:refresh|refreshed|last updated|data updated)"
            r"[^:]{0,40}:\s*(.+)$",
            text,
            flags=re.IGNORECASE,
        )
        if match and match.group(1).strip():
            refresh_timestamp = match.group(1).strip()
            break
    return items, refresh_timestamp


async def _goto_report(page: Page, url: str, attempts: int = 3) -> None:
    """Navigate with a longer timeout, retrying on Power BI load stalls."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            await page.goto(url, wait_until="commit", timeout=180_000)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=120_000)
            except Exception:
                pass
            return
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(5 * (attempt + 1))
    assert last_error is not None
    raise last_error


async def _wait_for_report_ready(page: Page) -> None:
    try:
        await page.wait_for_function(
            """() => {
              const main = document.querySelector('[role="main"]');
              const visible = element => {
                const rect = element?.getBoundingClientRect();
                const style = element && getComputedStyle(element);
                return !!rect && rect.width > 0 && rect.height > 0 &&
                  style.display !== 'none' &&
                  style.visibility !== 'hidden' &&
                  element.getAttribute('aria-hidden') !== 'true';
              };
              if (!main) return false;
              const hasFilterControl = [
                ...main.querySelectorAll(
                  '[role="combobox"], select'
                )
              ].some(visible);
              const visuals = [
                ...main.querySelectorAll(
                  'div.visualContainer, [data-visual-id]'
                )
              ].filter(visible);
              const resourceError =
                /\\bquery\\b.{0,80}\\bexceeded\\b.{0,80}\\bavailable resources\\b/i
                  .test(main.innerText || '');
              if (resourceError) return true;
              // A visual frame can be drawn before its data renders. Slicers
              // and text boxes have no chart markup, so text or controls also
              // count as rendered. Empty chart frames have neither.
              const rendered = visual => {
                if (visual.querySelector(
                  'svg, canvas, img, table, [role="grid"], [role="row"], ' +
                  '[role="combobox"], select'
                )) {
                  return true;
                }
                return (visual.innerText || '').trim().length > 0;
              };
              const busy = visuals.some(visual =>
                [...visual.querySelectorAll(
                  '[aria-busy="true"], [role="progressbar"]'
                )].some(visible)
              );
              if (visuals.length === 0) return hasFilterControl;
              return !busy && visuals.every(rendered);
            }""",
            timeout=REPORT_READY_TIMEOUT_MS,
            polling=500,
        )
    except PlaywrightTimeoutError as exc:
        diagnostics = await page.evaluate(
            """() => {
              const visible = element => {
                const rect = element?.getBoundingClientRect();
                const style = element && getComputedStyle(element);
                return !!rect && rect.width > 0 && rect.height > 0 &&
                  style.display !== 'none' &&
                  style.visibility !== 'hidden' &&
                  element.getAttribute('aria-hidden') !== 'true';
              };
              const main = document.querySelector('[role="main"]');
              const text = document.body?.innerText || '';
              return {
                title: document.title,
                readyState: document.readyState,
                mainVisible: visible(main),
                visibleTabs: [...(main?.querySelectorAll('[role="tab"]') || [])]
                  .filter(visible)
                  .map(tab => (
                    tab.getAttribute('aria-label') ||
                    tab.getAttribute('title') ||
                    tab.innerText ||
                    tab.textContent ||
                    ''
                  ).replace(/\\s+Selected$/i, '').replace(/\\s+/g, ' ').trim()),
                visibleReportTabs: [...(main?.querySelectorAll(
                  '[role="tab"]'
                ) || [])]
                  .filter(visible)
                  .map(tab => (
                    tab.getAttribute('aria-label') ||
                    tab.getAttribute('title') ||
                    tab.innerText ||
                    tab.textContent ||
                    ''
                  ).replace(/\\s+Selected$/i, '').replace(/\\s+/g, ' ')
                    .trim().toLocaleLowerCase())
                  .filter(name => name && !['home', 'view', 'file'].includes(name)),
                visibleComboboxes: [...(main?.querySelectorAll(
                  '[role="combobox"], select'
                ) || [])].filter(visible).length,
                visibleVisuals: [...(main?.querySelectorAll(
                  'div.visualContainer, [data-visual-id]'
                ) || [])].filter(visible).length,
                unrenderedVisuals: [...(main?.querySelectorAll(
                  'div.visualContainer, [data-visual-id]'
                ) || [])].filter(visible).filter(visual => {
                  const hasMarkup = visual.querySelector(
                    'svg, canvas, img, table, [role="grid"], [role="row"], ' +
                    '[role="combobox"], select'
                  );
                  return !hasMarkup &&
                    (visual.innerText || '').trim().length === 0;
                }).map(visual => {
                  const rect = visual.getBoundingClientRect();
                  return {
                    id: visual.getAttribute('data-visual-id') || '',
                    cls: String(visual.className).slice(0, 80),
                    size: Math.round(rect.width) + 'x' + Math.round(rect.height),
                    busy: !!visual.querySelector('[aria-busy="true"]'),
                    progress: !!visual.querySelector('[role="progressbar"]')
                  };
                }).slice(0, 10),
                pageStateHints: [
                  'sign in',
                  'access denied',
                  'something went wrong',
                  'unable to load',
                  'loading'
                ].filter(hint => text.toLocaleLowerCase().includes(hint))
              };
            }"""
        )
        content_rendered = (
            diagnostics.get("mainVisible")
            and diagnostics.get("visibleVisuals", 0) > 0
            and not diagnostics.get("unrenderedVisuals")
            and not diagnostics.get("pageStateHints")
        )
        if not content_rendered:
            raise RuntimeError(
                "Power BI report content did not become accessible within "
                f"{REPORT_READY_TIMEOUT_MS // 1_000} seconds; "
                f"observed page state: {diagnostics!r}"
            ) from exc
    await _wait_for_report_load_to_settle(page)


async def _current_filter_value(
    page: Page, filter_item: dict[str, Any]
) -> list[str] | None:
    selector_name = filter_item.get("selector_name") or filter_item["name"]
    if filter_item.get("control_type") == "bookmark_buttons":
        for frame in page.frames:
            group = next(
                (
                    item
                    for item in await _bookmark_button_groups(frame)
                    if item["name"].casefold()
                    == filter_item.get("name", "").casefold()
                ),
                None,
            )
            if group is not None:
                return group["selected"]
        return None
    if filter_item.get("control_type") == "checkbox":
        for frame in page.frames:
            options = await _checkbox_filter_options(frame, filter_item)
            if options:
                return [
                    item["label"] for item in options if item.get("selected")
                ]
        return None
    if filter_item.get("control_type") == "radio":
        for frame in page.frames:
            options = await _radio_filter_options(frame, filter_item)
            if options:
                return [
                    item["label"] for item in options if item.get("selected")
                ]
        return None
    for frame in page.frames:
        control = await _combobox_for_filter(frame, filter_item)
        if control is not None:
            if not await _slicer_dropdown_is_open(control):
                await _click_slicer_control(control)
                await _wait_for_slicer_open(page, control)
            try:
                selected = await _selected_combobox_options(
                    await _options_for_combobox(frame, control)
                )
                summary = (await control.inner_text()).strip()
                return _selected_values_with_summary(
                    selected,
                    summary,
                    [
                        value
                        for value in filter_item.get("values", [])
                        if isinstance(value, str)
                    ],
                    control_type=filter_item.get("control_type"),
                )
            finally:
                await _close_slicer_dropdown(page, control)
        selects = frame.locator("select")
        for index in range(await selects.count()):
            candidate = selects.nth(index)
            name = (
                await candidate.get_attribute("aria-label")
                or await candidate.get_attribute("title")
                or await candidate.evaluate(
                    """el => el.labels && [...el.labels]
                      .map(item => item.innerText.trim()).filter(Boolean).join(' ')"""
                )
            )
            if name == selector_name:
                selected = await candidate.locator("option:checked").all_text_contents()
                return [value.strip() for value in selected]
    return None


async def _read_filter_values(
    page: Page, filters: list[dict[str, Any]]
) -> dict[str, list[str]]:
    selected_values: dict[str, list[str]] = {}
    for item in filters:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        selected = await _current_filter_value(page, item)
        if selected is not None:
            selected_values[item["name"]] = selected
    return selected_values


def _merge_accessible_filter_values(
    title_items: Any, filter_values: dict[str, list[str]]
) -> tuple[list[Any], int]:
    items = list(title_items) if isinstance(title_items, list) else []
    existing_counts: dict[tuple[str, str], int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = str(
            item.get("standard_element_name")
            or item.get("element_type")
            or item.get("category")
            or ""
        ).strip().casefold()
        if item_type not in {"slicer_value", "filter_value"}:
            continue
        text = item.get("element_text")
        if text is None:
            text = item.get("text")
        associated_visual = item.get("associated_visual")
        if text is None or associated_visual is None:
            continue
        key = (
            " ".join(str(associated_visual).split()).casefold(),
            " ".join(str(text).split()),
        )
        existing_counts[key] = existing_counts.get(key, 0) + 1

    next_order = max(
        (
            item.get("reading_order")
            for item in items
            if isinstance(item, dict)
            and isinstance(item.get("reading_order"), int)
            and not isinstance(item.get("reading_order"), bool)
        ),
        default=0,
    )
    next_title_id = max(
        (
            int(match.group(1))
            for item in items
            if isinstance(item, dict)
            and isinstance(item.get("title_id"), str)
            and (match := re.fullmatch(r"TITLE_(\d+)", item["title_id"]))
        ),
        default=0,
    )
    seen_counts: dict[tuple[str, str], int] = {}
    added = 0
    for name, values in filter_values.items():
        associated_visual = " ".join(str(name).split()).casefold()
        if not associated_visual:
            continue
        for value in values:
            if value is None:
                continue
            text = str(value).strip()
            if not text:
                continue
            key = (associated_visual, " ".join(text.split()))
            seen = seen_counts.get(key, 0)
            seen_counts[key] = seen + 1
            if seen < existing_counts.get(key, 0):
                continue
            next_order += 1
            next_title_id += 1
            items.append(
                {
                    "title_id": f"TITLE_{next_title_id:03d}",
                    "text": text,
                    "element_text": text,
                    "category": "slicer_value",
                    "standard_element_name": "slicer_value",
                    "element_type": "slicer_value",
                    "associated_visual": str(name),
                    "is_application_chrome": False,
                    "is_logo": False,
                    "reading_order": next_order,
                    "bounding_box": None,
                    "normalized_bounding_box": None,
                    "evidence_sources": ["accessible_filter"],
                    "extraction_status": "complete",
                    "uncertainties": [],
                }
            )
            added += 1
    return items, added


async def _clear_selection_button(
    frame: Any,
    filter_item: dict[str, Any],
    control: Any | None = None,
) -> Any | None:
    if filter_item.get("control_type") == "bookmark_buttons":
        return None

    if control is None and filter_item.get("control_type") == "checkbox":
        options = await _checkbox_filter_options(frame, filter_item)
        if options:
            control = frame.locator(
                '[role="checkbox"], input[type="checkbox"]'
            ).nth(options[0]["index"])
    if control is None and filter_item.get("control_type") == "radio":
        options = await _radio_filter_options(frame, filter_item)
        if options:
            control = frame.locator(
                '[role="radio"], input[type="radio"]'
            ).nth(options[0]["index"])
    if control is None:
        control = await _combobox_for_filter(frame, filter_item)
        if control is None:
            selector_name = (
                filter_item.get("selector_name") or filter_item.get("name")
            )
            selects = frame.locator("select")
            for index in range(await selects.count()):
                candidate = selects.nth(index)
                name = (
                    await candidate.get_attribute("aria-label")
                    or await candidate.get_attribute("title")
                    or await candidate.evaluate(
                        """el => el.labels && [...el.labels]
                          .map(item => item.innerText.trim())
                          .filter(Boolean).join(' ')"""
                    )
                )
                if name == selector_name:
                    control = candidate
                    break

    if control is None:
        return None

    visual = control.locator(
        "xpath=ancestor::*[@data-visual-id or "
        "contains(concat(' ', normalize-space(@class), ' '), "
        "' visualContainer ')][1]"
    )
    group = control.locator("xpath=ancestor::*[@role='group'][1]")
    clear_name = re.compile(
        r"^Clear selections?$", re.IGNORECASE
    )
    scopes = (visual,) if await visual.count() else (group,)
    filter_name = filter_item.get("name")
    button, found = await _visible_clear_selection_button(scopes, clear_name)
    visible_before_hover = button is not None
    if button is None:
        await control.hover(timeout=SLICER_CONTROL_ACTION_TIMEOUT_MS)
        button, found = await _visible_clear_selection_button(
            scopes,
            clear_name,
            wait_timeout_ms=CLEAR_SELECTION_VISIBLE_TIMEOUT_MS,
        )
    logger.info(
        "Clear selections for filter %r: found=%s visible_before_hover=%s "
        "visible_after_hover=%s",
        filter_name,
        found > 0,
        visible_before_hover,
        button is not None,
    )
    return button


async def _visible_clear_selection_button(
    scopes: tuple[Any, ...],
    clear_name: re.Pattern[str],
    *,
    wait_timeout_ms: int | None = None,
) -> tuple[Any | None, int]:
    """Return the first visible, enabled Clear selections button and the candidate count.

    When ``wait_timeout_ms`` is set, each candidate is waited on for visibility
    (event-driven, so it returns as soon as the header control appears).
    """
    found = 0
    for scope in scopes:
        buttons = scope.get_by_role("button", name=clear_name, exact=True)
        for button_index in range(await buttons.count()):
            found += 1
            button = buttons.nth(button_index)
            if wait_timeout_ms is not None:
                try:
                    await button.wait_for(
                        state="visible", timeout=wait_timeout_ms
                    )
                except PlaywrightTimeoutError:
                    continue
            if await button.is_visible() and await button.is_enabled():
                return button, found
    return None, found


async def _click_clear_selection_button(
    button: Any,
    filter_name: str | None = None,
) -> bool:
    """Click a slicer's Clear selections control without waiting for navigations.

    Power BI keeps the page busy after clearing, so Playwright's default
    post-click navigation wait can hang even though the click was delivered.
    The button is only passed here after it was confirmed visible, so no
    force-click is used; a failed click returns False so callers can fall back.
    """
    try:
        await button.click(timeout=5_000, no_wait_after=True)
    except PlaywrightError:
        logger.warning(
            "Clear selections click failed for filter %r",
            filter_name,
            exc_info=True,
        )
        return False
    logger.info("Clear selections clicked for filter %r", filter_name)
    try:
        await button.evaluate("element => element.blur()", timeout=1_000)
        await button.page.mouse.move(-10, -10)
    except Exception:
        pass
    return True


async def _clear_page_slicer_selections(
    page: Page,
    filter_names: list[str] | None = None,
    *,
    require_all: bool = True,
) -> list[str]:
    requested_names = (
        {
            _normalized_control_text(name)
            for name in filter_names
            if isinstance(name, str) and name.strip()
        }
        if filter_names is not None
        else None
    )
    if requested_names == set():
        return []

    cleared_slicers: list[str] = []
    for frame in page.frames:
        if requested_names is not None:
            selects = frame.locator("select")
            for index in range(await selects.count()):
                control = selects.nth(index)
                name = (
                    await control.get_attribute("aria-label")
                    or await control.get_attribute("title")
                    or await control.evaluate(
                        """element => [...(element.labels || [])]
                          .map(label => label.innerText.trim())
                          .filter(Boolean).join(' ')"""
                    )
                )
                if (
                    _normalized_control_text(name) not in requested_names
                    or _normalized_control_text(name)
                    in {_normalized_control_text(item) for item in cleared_slicers}
                ):
                    continue
                button = await _clear_selection_button(
                    frame,
                    {"name": name, "selector_name": name},
                    control=control,
                )
                if button is not None:
                    await _click_clear_selection_button(button)
                    await page.wait_for_timeout(150)
                    cleared_slicers.append(name)

        combos = frame.get_by_role("combobox")
        metadata = await combos.evaluate_all(COMBOBOX_METADATA_SCRIPT)
        for item in metadata:
            combo = combos.nth(item["index"])
            if not await combo.is_visible():
                continue
            name = str(item.get("name") or "").strip()
            selector_name = str(item.get("selectorName") or name).strip()
            if not name:
                continue
            normalized_name = _normalized_control_text(name)
            if (
                requested_names is not None
                and normalized_name not in requested_names
            ) or normalized_name in {
                _normalized_control_text(cleared)
                for cleared in cleared_slicers
            }:
                continue
            if not await _slicer_dropdown_is_open(combo):
                await _click_slicer_control(combo)
                await _wait_for_slicer_open(page, combo)
            filter_probe = {
                "name": name,
                "selector_name": selector_name,
            }
            listbox_options = await _associated_listbox_options(
                frame, combo, filter_probe
            )
            associated_control_type = (
                listbox_options[2] if listbox_options is not None else None
            )
            if associated_control_type == "radio_dropdown":
                button = await _clear_selection_button(
                    frame,
                    {
                        **filter_probe,
                        "control_type": associated_control_type,
                    },
                    control=combo,
                )
                if button is not None:
                    await _click_clear_selection_button(button)
                    await page.wait_for_timeout(150)
                    clear_filter = {
                        **filter_probe,
                        "control_type": associated_control_type,
                        "values": listbox_options[0],
                    }
                    selected = await _current_filter_value(page, clear_filter)
                    if not _filter_selection_is_clear(clear_filter, selected):
                        raise RuntimeError(
                            f"Could not clear slicer {name!r} at page startup; "
                            f"selected values: {selected!r}; Clear selections "
                            "did not clear the filter"
                        )
                    cleared_slicers.append(name)
                    continue
                await _close_slicer_dropdown(page, combo)
                continue
            if associated_control_type == "checkbox_dropdown":
                button = await _clear_selection_button(
                    frame,
                    {
                        **filter_probe,
                        "control_type": associated_control_type,
                    },
                    control=combo,
                )
                if button is not None:
                    await _click_clear_selection_button(button)
                    await page.wait_for_timeout(150)
                    cleared_slicers.append(name)
                    continue
            (
                options,
                values,
                _selected,
                checkbox_choices,
                radio_choices,
            ) = await _wait_for_combobox_options(
                page, frame, combo, filter_probe
            )
            choices = radio_choices or checkbox_choices
            control_type = (
                "radio_dropdown"
                if radio_choices
                else "checkbox_dropdown"
                if checkbox_choices
                else associated_control_type
            )
            if control_type == "radio_dropdown":
                button = await _clear_selection_button(
                    frame,
                    {
                        "name": name,
                        "selector_name": selector_name,
                        "control_type": control_type,
                    },
                    control=combo,
                )
                if button is not None:
                    await _click_clear_selection_button(button)
                    await page.wait_for_timeout(150)
                    clear_filter = {
                        "name": name,
                        "selector_name": selector_name,
                        "control_type": control_type,
                        "values": values,
                    }
                    selected = await _current_filter_value(page, clear_filter)
                    if not _filter_selection_is_clear(clear_filter, selected):
                        raise RuntimeError(
                            f"Could not clear slicer {name!r} at page startup; "
                            f"selected values: {selected!r}; Clear selections "
                            "did not clear the filter"
                        )
                    cleared_slicers.append(name)
                    continue
                await _close_slicer_dropdown(page, combo)
                continue
            button = await _clear_selection_button(
                frame,
                {
                    "name": name,
                    "selector_name": selector_name,
                    "control_type": control_type,
                },
                control=combo,
            )
            if button is not None:
                await _click_clear_selection_button(button)
                await page.wait_for_timeout(150)
                cleared_slicers.append(name)
                continue
            clear_choice = next(
                (
                    choice
                    for choice in choices
                    if _is_clear_selection_label(choice["label"])
                ),
                None,
            )
            if clear_choice is not None:
                choice_locator = frame.locator(
                    '[role="radio"], input[type="radio"]'
                    if radio_choices
                    else '[role="checkbox"], input[type="checkbox"]'
                ).nth(clear_choice["index"])
                await choice_locator.click(force=True, timeout=5_000)
                await page.wait_for_timeout(150)
                cleared_slicers.append(name)
                continue
            clear_value = next(
                (
                    value
                    for value in values
                    if _is_clear_selection_label(value)
                ),
                None,
            )
            if clear_value is not None:
                await _click_filter_option(
                    page,
                    frame,
                    combo,
                    clear_value,
                    filter_name=name,
                    expected_options=values,
                )
                cleared_slicers.append(name)
                continue
            if await _slicer_dropdown_is_open(combo):
                await _close_slicer_dropdown(page, combo)

        if requested_names is not None:
            for control_type, options_for_frame in (
                ("checkbox", _checkbox_filter_options),
                ("radio", _radio_filter_options),
            ):
                groups: dict[str, dict[str, Any]] = {}
                for option in await options_for_frame(frame):
                    name = option["name"].strip()
                    normalized_name = _normalized_control_text(name)
                    if (
                        normalized_name not in requested_names
                        or normalized_name
                        in {
                            _normalized_control_text(item)
                            for item in cleared_slicers
                        }
                    ):
                        continue
                    key = option.get("visualId") or normalized_name
                    group = groups.setdefault(
                        key,
                        {
                            "name": name,
                            "selector_name": name,
                            "visual_id": option.get("visualId") or "",
                            "control_type": control_type,
                        },
                    )
                    if _is_clear_selection_label(option["label"]):
                        group["clear_option_index"] = option["index"]
                for group in groups.values():
                    button = await _clear_selection_button(frame, group)
                    if button is not None:
                        await _click_clear_selection_button(button)
                    elif "clear_option_index" in group:
                        controls = frame.locator(
                            '[role="checkbox"], input[type="checkbox"]'
                            if control_type == "checkbox"
                            else '[role="radio"], input[type="radio"]'
                        )
                        await controls.nth(
                            group["clear_option_index"]
                        ).click(force=True, timeout=5_000)
                    else:
                        continue
                    await page.wait_for_timeout(150)
                    cleared_slicers.append(group["name"])

    if requested_names is not None and require_all:
        cleared_names = {
            _normalized_control_text(name) for name in cleared_slicers
        }
        missing_names = requested_names - cleared_names
        if missing_names:
            raise RuntimeError(
                "Previously cleared filters no longer expose a clear action "
                "on this page: "
                f"{sorted(missing_names)!r}"
            )
    return cleared_slicers


async def _inventory_page_after_clearing_slicers(
    page: Page,
    diagnostic_directory: Path | None = None,
    *,
    clear_slicers: bool = True,
    inspect_filter_options: bool = True,
) -> dict[str, Any]:
    cleared_slicers = (
        await _clear_page_slicer_selections(page, require_all=False)
        if clear_slicers
        else []
    )
    await _wait_for_state_render(page, settle_seconds=2.0)
    if inspect_filter_options:
        inventory = await _accessible_inventory(
            page,
            diagnostic_directory=diagnostic_directory,
            clear_selection_actions=False,
        )
    else:
        inventory = await _accessible_inventory(
            page,
            diagnostic_directory=diagnostic_directory,
            clear_selection_actions=False,
            inspect_filter_options=False,
        )
    if not clear_slicers:
        inventory["initial_cleared_slicers"] = []
        return inventory

    filters = [
        item
        for item in inventory["filters"]
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and item.get("control_type") != "bookmark_buttons"
    ]
    already_cleared = {
        _normalized_control_text(name) for name in cleared_slicers
    }
    remaining_filter_names = [
        item["name"]
        for item in filters
        if item.get("control_type") not in {"radio", "radio_dropdown"}
        and item.get("option_inventory_status") != "unavailable"
        and _normalized_control_text(item["name"]) not in already_cleared
    ]
    cleared_slicers.extend(
        await _clear_page_slicer_selections(
            page, remaining_filter_names, require_all=False
        )
    )
    await _wait_for_state_render(page, settle_seconds=2.0)

    retried_clear = False
    for filter_item in filters:
        if (
            filter_item.get("option_inventory_status") != "skipped"
            or _normalized_control_text(str(filter_item.get("name") or ""))
            in {_normalized_control_text(name) for name in cleared_slicers}
        ):
            continue
        current = filter_item.get("selected")
        if not current or _filter_selection_is_clear(filter_item, current):
            continue
        # The popup can hide the header Clear button, so retry with the
        # dropdown closed and the pointer hovering the slicer.
        for frame in page.frames:
            control = await _combobox_for_filter(frame, filter_item)
            if control is None:
                continue
            button = await _clear_selection_button(
                frame, filter_item, control=control
            )
            if button is not None and await _click_clear_selection_button(
                button, filter_item.get("name")
            ):
                cleared_slicers.append(filter_item["name"])
                retried_clear = True
                await page.wait_for_timeout(500)
            break
    if retried_clear:
        await _wait_for_state_render(page, settle_seconds=2.0)

    for filter_item in filters:
        selected = await _current_filter_value(page, filter_item)
        if filter_item.get("option_inventory_status") == "unavailable":
            if selected is None:
                raise RuntimeError(
                    f"Could not inspect filter {filter_item['name']!r} at "
                    "page startup; its popup options and selection state "
                    "are unavailable"
                )
            filter_item["selected"] = selected
            continue
        if filter_item.get("control_type") in {"radio", "radio_dropdown"}:
            values = filter_item.get("values", [])
            if selected is not None and (
                len(selected) > 1
                or any(
                    not any(
                        _selection_matches(value, available)
                        for available in values
                        if isinstance(available, str)
                    )
                    for value in selected
                )
            ):
                raise RuntimeError(
                    f"Could not inspect the single-select slicer "
                    f"{filter_item['name']!r} at page startup; selected "
                    f"values: {selected!r}; available options: {values!r}"
                )
            if selected is not None:
                filter_item["selected"] = selected
            continue
        was_cleared = _normalized_control_text(
            str(filter_item.get("name") or "")
        ) in {
            _normalized_control_text(name) for name in cleared_slicers
        }
        if was_cleared and (
            selected is None
            or not _filter_selection_is_clear(filter_item, selected)
        ):
            raise RuntimeError(
                f"Could not clear slicer {filter_item['name']!r} at page "
                f"startup; selected values: {selected!r}; Clear selections "
                "was unavailable or did not clear the filter"
            )
        if selected is None:
            raise RuntimeError(
                f"Could not inspect slicer {filter_item['name']!r} at page "
                "startup; its selection state is unknown and no clear "
                "action was confirmed"
            )
        filter_item["selected"] = selected

    await _set_single_select_defaults(
        page,
        filters,
        cleared_slicers,
        diagnostic_directory=diagnostic_directory,
    )
    inventory["initial_cleared_slicers"] = cleared_slicers
    return inventory


async def _set_single_select_defaults(
    page: Page,
    filters: list[dict[str, Any]],
    cleared_slicers: list[str],
    diagnostic_directory: Path | None = None,
) -> None:
    for filter_item in filters:
        if (
            not isinstance(filter_item, dict)
            or filter_item.get("control_type")
            not in {"radio", "radio_dropdown"}
            or not isinstance(filter_item.get("name"), str)
            or filter_item.get("option_inventory_status") == "unavailable"
        ):
            continue
        values = [
            value
            for value in filter_item.get("values", [])
            if isinstance(value, str)
            and not _is_clear_selection_label(value)
            and not _is_aggregate_value(value)
        ]
        if not values:
            raise RuntimeError(
                f"Single-select filter {filter_item['name']!r} has no "
                "individual options to use as its default"
            )
        last_value = values[-1]
        selected = filter_item.get("selected")
        if (
            not isinstance(selected, list)
            or len(selected) != 1
            or not _selection_matches(last_value, selected[0])
        ):
            if filter_item.get("control_type") == "radio_dropdown":
                await _refresh_dropdown_filter_options(page, filter_item)
                values = [
                    value
                    for value in filter_item.get("values", [])
                    if isinstance(value, str)
                    and not _is_clear_selection_label(value)
                    and not _is_aggregate_value(value)
                ]
                if not values:
                    raise RuntimeError(
                        f"Single-select filter {filter_item['name']!r} has "
                        "no individual options to use as its default"
                    )
                last_value = values[-1]
            await _select_filter(
                page,
                FilterSelection(name=filter_item["name"], value=last_value),
                [filter_item],
                diagnostic_directory=diagnostic_directory,
            )
        filter_item["values"] = values
        filter_item["selected"] = [last_value]
        cleared_slicers[:] = [
            name
            for name in cleared_slicers
            if _normalized_control_text(name)
            != _normalized_control_text(filter_item["name"])
        ]


async def _prepare_slicer_selections(
    page: Page,
    filters: list[dict[str, Any]],
    already_cleared_slicers: list[str] | None = None,
    diagnostic_directory: Path | None = None,
) -> None:
    already_cleared = {
        _normalized_control_text(name)
        for name in already_cleared_slicers or []
    }
    for filter_item in filters:
        if (
            not isinstance(filter_item, dict)
            or not isinstance(filter_item.get("name"), str)
            or filter_item.get("control_type") == "bookmark_buttons"
        ):
            continue
        if filter_item.get("option_inventory_status") == "unavailable":
            filter_item["reset_strategy"] = "preserve_selection"
            continue

        normalized_name = _normalized_control_text(filter_item["name"])
        if normalized_name in already_cleared:
            filter_item["reset_strategy"] = "clear_selection"
            filter_item["skip_filter_cases"] = True
            continue

        control_type = filter_item.get("control_type")
        if control_type in {"radio", "radio_dropdown"}:
            values = [
                value
                for value in filter_item.get("values", [])
                if isinstance(value, str)
                and not _is_clear_selection_label(value)
                and not _is_aggregate_value(value)
            ]
            if not values:
                raise RuntimeError(
                    f"Single-select filter {filter_item['name']!r} has no "
                    "individual options to use as its default"
                )
            last_value = values[-1]
            selected = filter_item.get("selected")
            if (
                not isinstance(selected, list)
                or len(selected) != 1
                or not _selection_matches(last_value, selected[0])
            ):
                await _select_filter(
                    page,
                    FilterSelection(
                        name=filter_item["name"], value=last_value
                    ),
                    [filter_item],
                    diagnostic_directory=diagnostic_directory,
                )
            filter_item["values"] = values
            filter_item["selected"] = [last_value]
            filter_item["reset_strategy"] = "preserve_selection"
            continue

        clear_button = None
        if (
            not filter_item.get("clear_selection_checked")
            and filter_item.get("control_type") != "bookmark_buttons"
        ):
            for frame in page.frames:
                clear_button = await _clear_selection_button(frame, filter_item)
                if clear_button is not None:
                    break

        if clear_button is not None and await _click_clear_selection_button(
            clear_button, filter_item.get("name")
        ):
            await page.wait_for_timeout(150)
            selected = await _current_filter_value(page, filter_item)
            if selected is None or not _filter_selection_is_clear(
                filter_item, selected
            ):
                raise RuntimeError(
                    f"Could not clear slicer {filter_item['name']!r}; "
                    f"selected values after clicking Clear selections: "
                    f"{selected!r}"
                )
            logger.info(
                "Cleared slicer %r; final selected values: %r",
                filter_item["name"],
                selected,
            )
            filter_item["selected"] = selected
            filter_item["reset_strategy"] = "clear_selection"
            filter_item["skip_filter_cases"] = True
            continue

        control_type = filter_item.get("control_type")
        if control_type not in {"radio", "checkbox", "bookmark_buttons"}:
            await _refresh_dropdown_filter_options(page, filter_item)
            control_type = filter_item.get("control_type")

        values = [
            value
            for value in filter_item.get("values", [])
            if isinstance(value, str)
            and not _is_clear_selection_label(value)
            and not _is_aggregate_value(value)
        ]
        filter_item["values"] = values
        if not values:
            selected = filter_item.get("selected")
            if isinstance(selected, list) and all(
                isinstance(value, str) for value in selected
            ):
                filter_item["reset_strategy"] = "preserve_selection"
                continue
            raise RuntimeError(
                f"Filter {filter_item['name']!r} has no individually "
                "selectable options and its current selection is unavailable"
            )
        for value in values:
            await _select_filter(
                page,
                FilterSelection(name=filter_item["name"], value=value),
                [filter_item],
                diagnostic_directory=diagnostic_directory,
            )
            filter_item["selected"] = [value]
        filter_item["reset_strategy"] = "preserve_selection"


async def _refresh_dropdown_filter_options(
    page: Page, filter_item: dict[str, Any]
) -> bool:
    if filter_item.get("control_type") in {
        "radio",
        "checkbox",
        "bookmark_buttons",
    }:
        return False

    filter_name = str(filter_item.get("name") or "")
    selector_name = str(
        filter_item.get("selector_name") or filter_name
    )
    for frame in page.frames:
        if not hasattr(frame, "get_by_role"):
            continue
        combo = await _combobox_for_filter(frame, filter_item)
        if combo is None:
            continue
        if not await _slicer_dropdown_is_open(combo):
            await _click_slicer_control(combo)
            await _wait_for_slicer_open(page, combo)
        try:
            (
                _option_locators,
                values,
                selected,
                checkbox_choices,
                radio_choices,
            ) = await _wait_for_combobox_options(
                page, frame, combo, filter_item
            )
            associated_options = await _associated_listbox_options(
                frame, combo, filter_item
            )
            if (
                associated_options is not None
                and associated_options[2] is not None
            ):
                values, selected, associated_control_type = associated_options
                filter_item["control_type"] = associated_control_type
            else:
                choices = radio_choices or checkbox_choices
                values, selected = _filter_inventory_option_values(
                    values,
                    selected,
                    choices,
                    filter_name,
                    selector_name,
                )
                if radio_choices:
                    filter_item["control_type"] = "radio_dropdown"
                elif checkbox_choices:
                    filter_item["control_type"] = "checkbox_dropdown"
            if not values:
                raise RuntimeError(
                    f"Could not reacquire individual options for filter "
                    f"{filter_name!r}"
                )
            filter_item["values"] = values
            if selected is not None:
                filter_item["selected"] = selected
            return True
        finally:
            await _close_slicer_dropdown(page, combo)
    return False


async def _reset_report_filters(
    page: Page,
    required: bool = False,
    baseline_filters: list[dict[str, Any]] | None = None,
    diagnostic_directory: Path | None = None,
) -> bool:
    reset_name = re.compile(r"reset", re.IGNORECASE)
    attempts = 4 if required else 1
    for attempt in range(attempts):
        for frame in page.frames:
            reset_button = frame.get_by_role("button", name=reset_name)
            if await reset_button.count():
                button = reset_button.first
                if await button.is_enabled():
                    await button.click()
                    confirmation = frame.get_by_role(
                        "button", name=re.compile(r"^Reset$", re.IGNORECASE), exact=True
                    )
                    try:
                        await confirmation.first.wait_for(
                            state="visible", timeout=5_000
                        )
                    except PlaywrightTimeoutError:
                        pass
                    else:
                        await confirmation.first.click()
                    await page.wait_for_timeout(2_000)
                    await _wait_for_report_ready(page)
                    if baseline_filters is not None:
                        return await _restore_report_filters(
                            page,
                            baseline_filters,
                            diagnostic_directory=diagnostic_directory,
                        )
                    return True
                if baseline_filters and await _filters_match_baseline(
                    page, baseline_filters
                ):
                    return True
                if baseline_filters is not None:
                    return await _restore_report_filters(
                        page,
                        baseline_filters,
                        diagnostic_directory=diagnostic_directory,
                    )
                return False
        if attempt + 1 < attempts:
            await page.wait_for_timeout(1_000)
    if baseline_filters:
        if await _filters_match_baseline(page, baseline_filters):
            return False
        if await _restore_report_filters(
            page,
            baseline_filters,
            diagnostic_directory=diagnostic_directory,
        ):
            return True
    if required:
        raise RuntimeError(
            "Cannot isolate a filter case: reset control is unavailable and "
            "the baseline selections could not be restored"
        )
    return False


async def _filters_match_baseline(
    page: Page, baseline_filters: list[dict[str, Any]]
) -> bool:
    for filter_item in baseline_filters:
        current = await _current_filter_value(page, filter_item)
        expected = filter_item.get("selected", [])
        if filter_item.get("reset_strategy") == "clear_selection":
            if not _filter_selection_is_clear(filter_item, current):
                return False
            continue
        if (
            current is None
            or not isinstance(expected, list)
            or not _filter_values_equivalent(filter_item, expected, current)
        ):
            return False
    return True


async def _restore_dropdown_filter_values(
    page: Page, filter_item: dict[str, Any], expected: list[str]
) -> bool:
    selector_name = filter_item.get("selector_name") or filter_item["name"]
    for frame in page.frames:
        selects = frame.locator("select")
        for index in range(await selects.count()):
            candidate = selects.nth(index)
            name = (
                await candidate.get_attribute("aria-label")
                or await candidate.get_attribute("title")
                or await candidate.evaluate(
                    """el => el.labels && [...el.labels]
                      .map(item => item.innerText.trim()).filter(Boolean).join(' ')"""
                )
            )
            if name != selector_name:
                continue
            await candidate.select_option(label=expected)
            current = await candidate.locator("option:checked").all_text_contents()
            return _filter_values_equivalent(
                filter_item,
                expected,
                [value.strip() for value in current],
            )

        control = await _combobox_for_filter(frame, filter_item)
        if control is None:
            continue
        current = await _current_filter_value(page, filter_item)
        if current is None:
            raise RuntimeError(
                f"Cannot read current selections for filter "
                f"{filter_item.get('name')!r}"
            )
        if _filter_values_equivalent(filter_item, expected, current):
            return True
        available = filter_item.get("values", [])
        if any(
            not any(
                _selection_matches(value, option)
                for option in available
            )
            for value in current
        ):
            raise RuntimeError(
                f"Cannot safely restore filter {filter_item.get('name')!r}; "
                f"selected options are not individually exposed: {current!r}"
            )

        for value in current:
            if not any(
                _selection_matches(value, target) for target in expected
            ):
                await _click_filter_option(
                    page,
                    frame,
                    control,
                    value,
                    filter_name=filter_item.get("name"),
                    expected_options=available,
                )
        current = await _current_filter_value(page, filter_item)
        if current is None:
            raise RuntimeError(
                f"Cannot verify restored selections for filter "
                f"{filter_item.get('name')!r}"
            )
        for value in expected:
            if not any(
                _selection_matches(value, selected) for selected in current
            ):
                await _click_filter_option(
                    page,
                    frame,
                    control,
                    value,
                    filter_name=filter_item.get("name"),
                    expected_options=available,
                )
                current = await _current_filter_value(page, filter_item)
                if current is None:
                    raise RuntimeError(
                        f"Cannot verify restored selections for filter "
                        f"{filter_item.get('name')!r}"
                    )
        return _filter_values_equivalent(filter_item, expected, current)
    raise RuntimeError(
        f"Cannot restore baseline for dropdown filter "
        f"{filter_item.get('name')!r}: control is unavailable"
    )


async def _restore_bookmark_button_values(
    page: Page, filter_item: dict[str, Any], expected: list[str]
) -> bool:
    for frame in page.frames:
        group = next(
            (
                item
                for item in await _bookmark_button_groups(frame)
                if item["name"].casefold()
                == filter_item.get("name", "").casefold()
            ),
            None,
        )
        if group is None:
            continue
        if _filter_values_match(expected, group["selected"]):
            return True
        if len(expected) != 1:
            return False
        option = next(
            (
                item
                for item in group["options"]
                if _selection_matches(expected[0], item["label"])
            ),
            None,
        )
        if option is None:
            raise RuntimeError(
                f"Baseline bookmark option {expected[0]!r} is unavailable in "
                f"{filter_item.get('name')!r}"
            )
        await option["locator"].click(force=True, timeout=5_000)
        selected = await _wait_for_filter_selection(
            page, filter_item, expected[0]
        )
        return bool(
            selected
            and len(selected) == 1
            and _selection_matches(expected[0], selected[0])
        )
    return False


async def _restore_report_filters(
    page: Page,
    baseline_filters: list[dict[str, Any]],
    diagnostic_directory: Path | None = None,
) -> bool:
    ordered_filters = sorted(
        enumerate(baseline_filters),
        key=lambda indexed_filter: (
            indexed_filter[1].get("control_type") != "bookmark_buttons",
            indexed_filter[0],
        ),
    )
    bookmark_state_pending = any(
        item.get("control_type") == "bookmark_buttons"
        for item in baseline_filters
    )
    for _, filter_item in ordered_filters:
        if (
            bookmark_state_pending
            and filter_item.get("control_type") != "bookmark_buttons"
        ):
            await _wait_for_state_render(page, settle_seconds=2.0)
            bookmark_state_pending = False
        expected = filter_item.get("selected", [])
        if not isinstance(expected, list) or not all(
            isinstance(value, str) for value in expected
        ):
            raise ValueError(
                f"Filter {filter_item.get('name')!r} has invalid baseline selections"
            )
        if await _filters_match_baseline(page, [filter_item]):
            continue

        if filter_item.get("reset_strategy") == "clear_selection":
            clear_button = None
            for frame in page.frames:
                clear_button = await _clear_selection_button(
                    frame, filter_item
                )
                if clear_button is not None:
                    break
            if clear_button is None:
                raise RuntimeError(
                    f"Cannot restore cleared baseline for filter "
                    f"{filter_item.get('name')!r}: Clear selections is "
                    "unavailable"
                )
            if not await _click_clear_selection_button(
                clear_button, filter_item.get("name")
            ):
                raise RuntimeError(
                    f"Cannot restore cleared baseline for filter "
                    f"{filter_item.get('name')!r}: Clear selections is "
                    "visible but could not be clicked"
                )
            await page.wait_for_timeout(150)
            current = await _current_filter_value(page, filter_item)
            if not _filter_selection_is_clear(filter_item, current):
                raise RuntimeError(
                    f"Could not restore cleared baseline for filter "
                    f"{filter_item.get('name')!r}; selected values: "
                    f"{current!r}"
                )
        elif filter_item.get("control_type") == "bookmark_buttons":
            restored = await _restore_bookmark_button_values(
                page, filter_item, expected
            )
            if not restored:
                raise RuntimeError(
                    f"Could not restore baseline bookmark selection for "
                    f"{filter_item.get('name')!r}; expected {expected!r}"
                )
        elif filter_item.get("control_type") in {
            "radio",
            "radio_dropdown",
        }:
            if len(expected) != 1:
                raise RuntimeError(
                    f"Cannot restore radio filter {filter_item.get('name')!r}; "
                    f"expected exactly one selected value, got {expected!r}"
                )
            await _select_filter(
                page,
                FilterSelection(
                    name=filter_item["name"],
                    value=expected[0],
                ),
                [filter_item],
                diagnostic_directory=diagnostic_directory,
            )
        elif filter_item.get("control_type") == "checkbox":
            restored = False
            for frame in page.frames:
                options = await _checkbox_filter_options(frame, filter_item)
                if not options:
                    continue
                selected = [
                    item["label"] for item in options if item.get("selected")
                ]
                for value in selected:
                    if not any(
                        _selection_matches(value, target) for target in expected
                    ):
                        await _click_checkbox_filter_option(
                            frame, filter_item, value
                        )
                selected = [
                    item["label"]
                    for item in await _checkbox_filter_options(frame, filter_item)
                    if item.get("selected")
                ]
                for value in expected:
                    if not any(
                        _selection_matches(value, target) for target in selected
                    ):
                        await _click_checkbox_filter_option(
                            frame, filter_item, value
                        )
                restored = True
                break
            if not restored:
                raise RuntimeError(
                    f"Cannot restore baseline for checkbox filter "
                    f"{filter_item.get('name')!r}: control is unavailable"
                )
        else:
            restored = await _restore_dropdown_filter_values(
                page, filter_item, expected
            )
            if not restored:
                raise RuntimeError(
                    f"Could not restore baseline selections for filter "
                    f"{filter_item.get('name')!r}; expected {expected!r}"
                )

        if not await _filters_match_baseline(page, [filter_item]):
            raise RuntimeError(
                f"Could not restore baseline selections for filter "
                f"{filter_item.get('name')!r}; expected {expected!r}"
            )
    return await _filters_match_baseline(page, baseline_filters)


async def _find_page_tab(page: Page, page_name: str) -> None:
    if page_name == "Report":
        return
    if page_name.strip().casefold() in POWER_BI_APPLICATION_TAB_NAMES:
        raise ValueError(
            f"{page_name!r} is a Power BI application tab, not a report page."
        )
    deadline = time.monotonic() + MAX_PAGE_TAB_WAIT_SECONDS
    while True:
        for frame in page.frames:
            tabs = frame.get_by_role("tab", name=page_name, exact=False)
            for index in range(await tabs.count()):
                tab = tabs.nth(index)
                if await tab.is_visible():
                    await tab.click()
                    await _wait_for_report_ready(page)
                    return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            visible_tabs: list[str] = []
            for frame in page.frames:
                for label in await _accessible_tab_names(frame):
                    if label not in visible_tabs:
                        visible_tabs.append(label)
            logger.warning(
                "Report page tab %r was not found; visible tab labels: %s",
                page_name,
                visible_tabs,
            )
            raise RuntimeError(
                f"Report tab {page_name!r} did not become visible within "
                f"{MAX_PAGE_TAB_WAIT_SECONDS:.0f} seconds after reload; "
                f"visible tab labels: {visible_tabs!r}"
            )
        await page.wait_for_timeout(min(250, max(1, int(remaining * 1000))))


def _selector_name(filters: list[dict[str, Any]], name: str) -> str:
    for item in filters:
        if item.get("name", "").casefold() == name.casefold():
            return item.get("selector_name") or name
    raise RuntimeError(f"Filter {name!r} is not present in the captured DOM inventory")


async def _click_filter_option(
    page: Page,
    frame: Any,
    combo: Any,
    value: str,
    *,
    filter_name: str | None = None,
    activate_search: bool = False,
    clear_search_on_success: bool = True,
    expected_options: list[str] | None = None,
) -> None:
    if expected_options is not None:
        _validate_individual_filter_option(
            value, filter_name, expected_options
        )
    if not await _slicer_dropdown_is_open(combo):
        await _click_slicer_control(combo)
        await _wait_for_slicer_open(page, combo)
    deadline = time.monotonic() + 15
    search_field = None
    search_applied = False
    search_action_attempted = False
    if activate_search:
        search_field = await _combobox_search_field(frame, combo)
        if search_field is None:
            search_action_attempted = True
            if await _activate_combobox_search_action(frame, combo):
                search_field = await _combobox_search_field(frame, combo)
        if search_field is not None:
            await search_field.fill(value)
            search_applied = True
    while time.monotonic() < deadline:
        options = await _options_for_combobox(frame, combo)
        selected_option = await _click_option_by_text(options, value)
        if selected_option is not None:
            if (
                search_applied
                and clear_search_on_success
                and await _slicer_dropdown_is_open(combo)
                and await search_field.is_visible()
            ):
                await search_field.fill("")
            await page.wait_for_timeout(150)
            return
        if not search_applied:
            search_field = await _combobox_search_field(frame, combo)
            if search_field is None and activate_search and not search_action_attempted:
                search_action_attempted = True
                if await _activate_combobox_search_action(frame, combo):
                    search_field = await _combobox_search_field(frame, combo)
            if search_field is not None:
                await search_field.fill(value)
                search_applied = True
        await page.wait_for_timeout(150)

    available_options = await _accessible_option_texts(
        await _options_for_combobox(frame, combo)
    )
    filter_context = (
        f" for filter {filter_name!r}" if filter_name is not None else ""
    )
    raise UnavailableFilterOptionError(
        f"Option {value!r} is unavailable{filter_context} in filter popup "
        "after waiting for "
        f"the option list and popup search; available options: "
        f"{available_options!r}"
    )


async def _select_filter(
    page: Page,
    selection: FilterSelection,
    filters: list[dict[str, Any]],
    diagnostic_directory: Path | None = None,
) -> None:
    selector_name = _selector_name(filters, selection.name)
    filter_item = next(
        item
        for item in filters
        if item.get("name", "").casefold() == selection.name.casefold()
    )
    if _is_aggregate_value(selection.value):
        current = await _current_filter_value(page, filter_item)
        if current is not None and _filter_selection_matches(
            filter_item, selection.value, current
        ):
            return
        raise RuntimeError(
            f"Aggregate option {selection.value!r} is not currently selected "
            f"for filter {selection.name!r}; select individual options "
            "instead"
        )
    available_values = filter_item.get("values")
    if isinstance(available_values, list) and available_values:
        _validate_individual_filter_option(
            selection.value, selection.name, available_values
        )
    if filter_item.get("control_type") == "bookmark_buttons":
        for frame in page.frames:
            group = next(
                (
                    item
                    for item in await _bookmark_button_groups(frame)
                    if item["name"].casefold() == selection.name.casefold()
                ),
                None,
            )
            if group is None:
                continue
            option = next(
                (
                    item
                    for item in group["options"]
                    if _normalized_control_text(item["label"])
                    == _normalized_control_text(selection.value)
                ),
                None,
            )
            if option is None:
                raise UnavailableFilterOptionError(
                    f"Bookmark option {selection.value!r} is unavailable in "
                    f"{selection.name!r}; available options: {group['values']!r}"
                )
            if group["selected"] != [option["label"]]:
                await option["locator"].click(force=True, timeout=5_000)
            selected_values = await _wait_for_filter_selection(
                page, filter_item, selection.value
            )
            if (
                selected_values is None
                or len(selected_values) != 1
                or not _selection_matches(
                    selection.value, selected_values[0]
                )
            ):
                raise RuntimeError(
                    f"Clicking bookmark option {selection.value!r} did not "
                    f"select it for {selection.name!r}; selected values: "
                    f"{selected_values!r}"
                )
            return
        raise RuntimeError(
            f"Power BI bookmark button group {selection.name!r} was not found"
        )
    if filter_item.get("control_type") == "checkbox":
        for frame in page.frames:
            options = await _checkbox_filter_options(frame, filter_item)
            if not options:
                continue
            selected_values = [
                item["label"] for item in options if item.get("selected")
            ]
            for selected_value in selected_values:
                if not _selection_matches(selection.value, selected_value):
                    await _click_checkbox_filter_option(
                        frame, filter_item, selected_value
                    )
            if not any(
                _selection_matches(selection.value, value)
                for value in selected_values
            ):
                await _click_checkbox_filter_option(
                    frame, filter_item, selection.value
                )
            current = await _wait_for_filter_selection(
                page, filter_item, selection.value
            )
            if (
                current is None
                or len(current) != 1
                or not _selection_matches(selection.value, current[0])
            ):
                raise RuntimeError(
                    f"Filter {selection.name!r} did not isolate "
                    f"{selection.value!r}; selected values: {current!r}"
                )
            return
        raise RuntimeError(
            f"Accessible checkbox filter {selection.name!r} was not found"
        )
    if filter_item.get("control_type") == "radio":
        for frame in page.frames:
            options = await _radio_filter_options(frame, filter_item)
            if not options:
                continue
            await _click_radio_filter_option(
                frame, filter_item, selection.value
            )
            selected = await _wait_for_filter_selection(
                page, filter_item, selection.value
            )
            if (
                selected is None
                or len(selected) != 1
                or not _selection_matches(selection.value, selected[0])
            ):
                raise RuntimeError(
                    f"Radio filter {selection.name!r} did not select "
                    f"{selection.value!r}; selected values: {selected!r}"
                )
            return
        raise RuntimeError(
            f"Accessible radio filter {selection.name!r} was not found"
        )
    if filter_item.get("control_type") == "radio_dropdown":
        for frame in page.frames:
            combo = await _combobox_for_filter(frame, filter_item)
            if combo is None:
                continue
            try:
                popup_selected = await _select_radio_dropdown_option(
                    page,
                    frame,
                    combo,
                    filter_item,
                    selection.value,
                    diagnostic_directory=diagnostic_directory,
                )
            finally:
                await _close_slicer_dropdown(page, combo)
            selected = await _wait_for_filter_selection(
                page, filter_item, selection.value
            )
            if (
                popup_selected is None
                or len(popup_selected) != 1
                or _normalized_control_text(popup_selected[0])
                != _normalized_control_text(selection.value)
                or selected is None
                or len(selected) != 1
                or _normalized_control_text(selected[0])
                != _normalized_control_text(selection.value)
            ):
                raise RuntimeError(
                    f"Radio option {selection.value!r} did not become the "
                    f"only selection in {selection.name!r}; selected "
                    f"values: {selected!r}"
                )
            return
        raise RuntimeError(
            f"Accessible radio dropdown {selection.name!r} was not found"
        )

    for frame in page.frames:
        select = frame.locator("select").filter(
            has=frame.locator("option", has_text=selection.value)
        )
        for index in range(await select.count()):
            candidate = select.nth(index)
            name = (
                await candidate.get_attribute("aria-label")
                or await candidate.get_attribute("title")
                or await candidate.evaluate(
                    """el => el.labels && [...el.labels]
                      .map(item => item.innerText.trim()).filter(Boolean).join(' ')"""
                )
            )
            if name == selector_name:
                await candidate.select_option(label=selection.value)
                selected = await candidate.locator(
                    "option:checked"
                ).all_text_contents()
                if not any(
                    _selection_matches(selection.value, value)
                    for value in selected
                ):
                    raise RuntimeError(
                        f"Filter {selection.name!r} did not select "
                        f"{selection.value!r}; selected values: {selected!r}"
                    )
                return

        combo_control = await _combobox_for_filter(frame, filter_item)
        if combo_control is not None:
            if not await _slicer_dropdown_is_open(combo_control):
                await _click_slicer_control(combo_control)
                await _wait_for_slicer_open(page, combo_control)
            selection_deadline = time.monotonic() + 3
            initial_selected = None
            while time.monotonic() < selection_deadline:
                options = await _options_for_combobox(frame, combo_control)
                available_options = await _accessible_option_texts(options)
                initial_selected = await _selected_combobox_options(options)
                if initial_selected is None:
                    initial_selected = _selected_values_with_summary(
                        None,
                        (await combo_control.inner_text()).strip(),
                        available_options,
                    )
                if initial_selected is not None:
                    break
                await page.wait_for_timeout(150)
            if initial_selected is None:
                raise RuntimeError(
                    f"Cannot determine the current selection for filter "
                    f"{selection.name!r} from its open popup; available "
                    f"options: {available_options!r}"
                )
            target_was_selected = any(
                _selection_matches(selection.value, value)
                for value in initial_selected
            )
            for selected_value in initial_selected:
                if _selection_matches(selection.value, selected_value):
                    continue
                if not await _slicer_dropdown_is_open(combo_control):
                    await _click_slicer_control(combo_control)
                    await _wait_for_slicer_open(page, combo_control)
                available_options = await _accessible_option_texts(
                    await _options_for_combobox(frame, combo_control)
                )
                matching_option = next(
                    (
                        option
                        for option in available_options
                        if _selection_matches(selected_value, option)
                    ),
                    None,
                )
                if matching_option is not None:
                    await _click_filter_option(
                        page,
                        frame,
                        combo_control,
                        matching_option,
                        filter_name=selection.name,
                        expected_options=available_options,
                    )
            await _click_filter_option(
                page,
                frame,
                combo_control,
                selection.value,
                filter_name=selection.name,
                expected_options=filter_item.get("values"),
            )
            if target_was_selected:
                await _click_filter_option(
                    page,
                    frame,
                    combo_control,
                    selection.value,
                    filter_name=selection.name,
                    expected_options=filter_item.get("values"),
                )
            await _close_slicer_dropdown(page, combo_control)
            selected_values = await _wait_for_filter_selection(
                page,
                filter_item,
                selection.value,
            )
            if not selected_values or not _filter_selection_matches(
                filter_item, selection.value, selected_values
            ):
                raise RuntimeError(
                    f"Clicking option {selection.value!r} did not change "
                    f"filter {selection.name!r}; selected values: "
                    f"{selected_values!r}"
                )
            return
    raise RuntimeError(f"Accessible dropdown filter {selection.name!r} was not found")


def _cell_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _export_json_document(
    title: str, filename: str, content: bytes
) -> dict[str, Any]:
    extension = Path(filename).suffix.casefold()
    if extension == ".csv":
        text = content.decode("utf-8-sig")
        source_sheets = [("Sheet1", list(csv.reader(io.StringIO(text))))]
    elif extension == ".xlsx":
        from openpyxl import load_workbook

        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        try:
            source_sheets = []
            for sheet in workbook.worksheets:
                sheet.reset_dimensions()
                source_sheets.append(
                    (
                        sheet.title,
                        [list(row) for row in sheet.iter_rows(values_only=True)],
                    )
                )
        finally:
            workbook.close()
    else:
        raise ValueError(f"Unsupported Power BI export format: {extension or filename!r}")

    sheets = []
    for sheet_name, source_rows in source_sheets:
        rows = [
            row
            for row in source_rows
            if any(value not in (None, "") for value in row)
        ]
        applied_filters = []
        table_rows = []
        for row in rows:
            first_cell = row[0] if row else None
            if (
                isinstance(first_cell, str)
                and first_cell.lstrip().casefold().startswith("applied filters:")
            ):
                applied_filters.extend(
                    line.strip()
                    for line in first_cell.splitlines()[1:]
                    if line.strip()
                )
                continue
            table_rows.append(row)

        if len(table_rows) < 2:
            sheets.append(
                {
                    "name": sheet_name,
                    "columns": [],
                    "rows": [],
                    "applied_filters": applied_filters,
                    "status": "no_tabular_data",
                }
            )
            continue

        column_count = max(len(row) for row in table_rows)
        headers = []
        used_headers: set[str] = set()
        for index in range(column_count):
            value = table_rows[0][index] if index < len(table_rows[0]) else None
            base_header = (
                str(value).strip() if value not in (None, "") else ""
            ) or f"Column {index + 1}"
            header = base_header
            suffix = 2
            while header in used_headers:
                header = f"{base_header} ({suffix})"
                suffix += 1
            headers.append(header)
            used_headers.add(header)

        data_rows = []
        for row in table_rows[1:]:
            values = [_cell_value(value) for value in row]
            values.extend([None] * (column_count - len(values)))
            data_rows.append(
                {header: value for header, value in zip(headers, values)}
            )
        sheets.append(
            {
                "name": sheet_name,
                "columns": headers,
                "rows": data_rows,
                "applied_filters": applied_filters,
                "status": "converted",
            }
        )

    has_data = any(sheet["status"] == "converted" for sheet in sheets)
    return {
        "source_file": filename,
        "visual": title,
        "format": extension.removeprefix("."),
        "status": "converted" if has_data else "no_tabular_data",
        "worksheets": sheets,
    }


def _export_element_from_json(
    document: dict[str, Any], visual_type: str
) -> dict[str, Any]:
    data_sheets = [
        sheet
        for sheet in document["worksheets"]
        if sheet["status"] == "converted"
    ]
    if not data_sheets:
        has_filter_metadata = any(
            sheet["applied_filters"] for sheet in document["worksheets"]
        )
        if has_filter_metadata:
            raise ValueError(
                f"Power BI export for {document['visual']!r} contains "
                "applied-filter metadata but no visual data table"
            )
        raise ValueError(
            f"Power BI export for {document['visual']!r} contains no tabular data"
        )

    def table_element(sheet: dict[str, Any]) -> dict[str, Any]:
        headers = sheet["columns"]
        return {
            "name": sheet["name"],
            "categories": headers,
            "rows": [
                [row.get(header) for header in headers] for row in sheet["rows"]
            ],
        }

    primary = data_sheets[0]
    element = {
        "type": visual_type,
        "title": document["visual"],
        "value": None,
        "categories": primary["columns"],
        "values": [],
        "rows": table_element(primary)["rows"],
    }
    if len(data_sheets) > 1:
        element["worksheets"] = [table_element(sheet) for sheet in data_sheets]
    return element


def _export_element(title: str, filename: str, content: bytes, visual_type: str) -> dict[str, Any]:
    document = _export_json_document(title, filename, content)
    return _export_element_from_json(document, visual_type)


def _merge_extractions(
    base: dict[str, Any], additional: dict[str, Any]
) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    base_metadata = merged.get("dashboard_metadata")
    additional_metadata = additional.get("dashboard_metadata")
    if isinstance(base_metadata, dict) and isinstance(additional_metadata, dict):
        for key, value in additional_metadata.items():
            if key == "screenshot_id" and base_metadata.get(key) and value:
                base_metadata[key] = hashlib.sha256(
                    f"{base_metadata[key]}\0{value}".encode("utf-8")
                ).hexdigest()
            elif not base_metadata.get(key) and value:
                base_metadata[key] = copy.deepcopy(value)

    elements = merged.setdefault("elements", [])
    positions = {
        (
            str(item.get("type") or "other").strip().casefold(),
            re.sub(r"\s+", " ", str(item.get("title") or "")).strip().casefold(),
        ): index
        for index, item in enumerate(elements)
        if isinstance(item, dict)
    }
    for item in additional.get("elements", []):
        if not isinstance(item, dict):
            continue
        key = (
            str(item.get("type") or "other").strip().casefold(),
            re.sub(r"\s+", " ", str(item.get("title") or "")).strip().casefold(),
        )
        index = positions.get(key)
        if index is None:
            positions[key] = len(elements)
            elements.append(copy.deepcopy(item))
            continue
        target = elements[index]
        _merge_element_columns(target, item)
        if item.get("extraction_status") == "partial":
            target["extraction_status"] = "partial"
        target["uncertainties"] = list(
            dict.fromkeys(
                (target.get("uncertainties") or [])
                + (item.get("uncertainties") or [])
            )
        )
        if item.get("values"):
            target["values"] = _merge_value_sequences(
                target.get("values", []), item["values"]
            )
        if target.get("value") is None and item.get("value") is not None:
            target["value"] = copy.deepcopy(item["value"])
    _refresh_extraction_summary(merged, additional)
    return merged


def _merge_element_columns(
    target: dict[str, Any],
    additional: dict[str, Any],
) -> None:
    additional = _with_structured_chart_content(target, additional)
    if _merge_structured_chart(target, additional):
        return

    headers = target.get("categories") or []
    additional_headers = additional.get("categories") or []
    if not isinstance(headers, list):
        headers = []
    if not isinstance(additional_headers, list):
        additional_headers = []
    headers = copy.deepcopy(headers)
    additional_headers = copy.deepcopy(additional_headers)

    if headers and additional_headers and all(
        isinstance(header, str) for header in headers + additional_headers
    ):
        known = {header.strip().casefold() for header in headers}
        for header in additional_headers:
            if header.strip().casefold() not in known:
                headers.append(copy.deepcopy(header))
                known.add(header.strip().casefold())
    elif not headers:
        headers = additional_headers

    rows_target = target.get("rows") or []
    rows_additional = additional.get("rows") or []
    if not isinstance(rows_target, list):
        rows_target = []
    if not isinstance(rows_additional, list):
        rows_additional = []

    if headers and all(isinstance(header, str) for header in headers):
        widest_row = max(
            (
                len(row)
                for row in rows_target + rows_additional
                if isinstance(row, list)
            ),
            default=0,
        )
        headers.extend(
            f"Column {index + 1}"
            for index in range(len(headers), widest_row)
        )
        aligned_target = _align_rows_to_headers(
            target.get("categories") or [], rows_target, headers
        )
        aligned_additional = _align_rows_to_headers(
            additional.get("categories") or [], rows_additional, headers
        )
    else:
        aligned_target = copy.deepcopy(rows_target)
        aligned_additional = copy.deepcopy(rows_additional)

    merged_rows = _merge_rows(aligned_target, aligned_additional)
    if headers:
        target["categories"] = headers
    if merged_rows:
        target["rows"] = merged_rows
    _sync_structured_table(target, additional)


def _with_structured_chart_content(
    target: dict[str, Any],
    additional: dict[str, Any],
) -> dict[str, Any]:
    if (
        str(target.get("type") or "").casefold()
        not in {"chart", "line chart", "bar chart", "column chart"}
        or isinstance(additional.get("content"), dict)
    ):
        return additional
    headers = additional.get("categories")
    rows = additional.get("rows")
    if not isinstance(headers, list) or not headers or not isinstance(rows, list):
        return additional

    chart_content = {
        "categories": [],
        "series": [
            {"name": str(header), "values": []}
            for header in headers[1:]
        ],
    }
    chart_categories = []
    series_values = [[] for _ in headers[1:]]
    for row in rows:
        if not isinstance(row, list):
            continue
        chart_categories.append(
            {
                "raw": copy.deepcopy(row[0]) if row else None,
                "status": "read" if row and row[0] is not None else "not_visible",
                "reason": None if row and row[0] is not None else "Category is unavailable",
            }
        )
        for index, values in enumerate(series_values):
            value = row[index + 1] if index + 1 < len(row) else None
            values.append(
                {
                    "raw": copy.deepcopy(value),
                    "status": "read" if value is not None else "not_visible",
                    "reason": None if value is not None else "Not present in exported row",
                }
            )
    chart_content["categories"] = chart_categories
    for index, values in enumerate(series_values):
        chart_content["series"][index]["values"] = values
    return {
        **additional,
        "content": chart_content,
        "_prefer_additional_chart_data": True,
    }


def _merge_structured_chart(
    target: dict[str, Any],
    additional: dict[str, Any],
) -> bool:
    target_content = target.get("content")
    additional_content = additional.get("content")
    prefer_additional = bool(additional.get("_prefer_additional_chart_data"))
    if not isinstance(target_content, dict) or not isinstance(additional_content, dict):
        return False
    if not (
        target_content.get("series") is not None
        and additional_content.get("series") is not None
    ):
        return False
    if (
        not target_content.get("series")
        and not additional_content.get("series")
        and not target_content.get("categories")
        and not additional_content.get("categories")
    ):
        return False

    def cell_raw(value: Any) -> Any:
        return value.get("raw") if isinstance(value, dict) and "raw" in value else value

    def indexed_categories(content: dict[str, Any]) -> list[tuple[tuple[str, int], Any]]:
        occurrences: dict[str, int] = {}
        indexed = []
        for category in content.get("categories", []):
            raw = cell_raw(category)
            key = repr(raw)
            occurrence = occurrences.get(key, 0)
            occurrences[key] = occurrence + 1
            indexed.append(((key, occurrence), copy.deepcopy(category)))
        return indexed

    base_categories = indexed_categories(target_content)
    extra_categories = indexed_categories(additional_content)
    category_values: dict[tuple[str, int], Any] = dict(base_categories)
    ordered_keys = [key for key, _ in base_categories]
    for key, value in extra_categories:
        if key not in category_values:
            ordered_keys.append(key)
        category_values[key] = category_values.get(key, value)

    series_by_name: dict[str, dict[str, Any]] = {}
    series_values: dict[str, dict[tuple[str, int], Any]] = {}
    for content in (target_content, additional_content):
        indexed = indexed_categories(content)
        for series in content.get("series", []):
            if not isinstance(series, dict) or not isinstance(series.get("name"), str):
                continue
            name = series["name"]
            signature = name.strip().casefold()
            if signature not in series_by_name:
                series_by_name[signature] = copy.deepcopy(series)
                series_values[signature] = {}
            values = series.get("values") or []
            for index, value in enumerate(values):
                if index < len(indexed):
                    key = indexed[index][0]
                else:
                    key = (f"__unlabeled_{index}", 0)
                    if key not in category_values:
                        ordered_keys.append(key)
                        category_values[key] = {
                            "raw": None,
                            "status": "not_visible",
                            "reason": "Category label is not visible",
                        }
                current = series_values[signature].get(key)
                if prefer_additional or current is None or (
                    cell_raw(current) is None and cell_raw(value) is not None
                ):
                    series_values[signature][key] = copy.deepcopy(value)

    ordered_categories = [category_values[key] for key in ordered_keys]
    merged_series = []
    for signature, series in series_by_name.items():
        values = series_values[signature]
        series["values"] = [
            copy.deepcopy(
                values.get(
                    key,
                    {
                        "raw": None,
                        "status": "not_visible",
                        "reason": "Value is not visible for this category",
                    },
                )
            )
            for key in ordered_keys
        ]
        merged_series.append(series)
    target_content["categories"] = ordered_categories
    target_content["series"] = merged_series
    target_content["legend"] = list(
        dict.fromkeys(
            (target_content.get("legend") or [])
            + (additional_content.get("legend") or [])
        )
    )
    if not target_content.get("chart_type"):
        target_content["chart_type"] = additional_content.get("chart_type")
    if not target_content.get("axes"):
        target_content["axes"] = copy.deepcopy(additional_content.get("axes") or {})
    target_content["annotations"] = list(
        dict.fromkeys(
            (target_content.get("annotations") or [])
            + (additional_content.get("annotations") or [])
        )
    )

    raw_categories = [cell_raw(value) for value in ordered_categories]
    raw_series = [
        [cell_raw(value) for value in series["values"]]
        for series in merged_series
    ]
    target["categories"] = raw_categories
    target["rows"] = [
        [
            raw_categories[row_index],
            *[
                values[row_index] if row_index < len(values) else None
                for values in raw_series
            ],
        ]
        for row_index in range(len(raw_categories))
    ]
    return True


def _sync_structured_table(
    target: dict[str, Any],
    additional: dict[str, Any],
) -> None:
    content = target.get("content")
    if not isinstance(content, dict) or not (
        content.get("columns") or content.get("rows")
    ):
        return
    original_content = copy.deepcopy(content)

    columns = target.get("categories") or content.get("columns") or []
    rows = target.get("rows") or []
    if not isinstance(columns, list) or not all(
        isinstance(column, str) for column in columns
    ):
        return

    known_cells: dict[tuple[str, str], dict[str, Any]] = {}
    for source in (original_content, additional.get("content")):
        if not isinstance(source, dict):
            continue
        for row in source.get("rows", []):
            if not isinstance(row, dict):
                continue
            for column, cell in row.items():
                if isinstance(cell, dict) and "raw" in cell:
                    key = (str(column).strip().casefold(), repr(cell["raw"]))
                    known_cells[key] = copy.deepcopy(cell)
                else:
                    key = (str(column).strip().casefold(), repr(cell))
                    known_cells[key] = {"raw": copy.deepcopy(cell), "status": "read"}

    structured_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        structured_row: dict[str, Any] = {}
        for index, column in enumerate(columns):
            raw = copy.deepcopy(row[index]) if index < len(row) else None
            cell = known_cells.get((column.strip().casefold(), repr(raw)))
            if cell is None:
                cell = {
                    "raw": raw,
                    "status": "read" if raw is not None else "not_visible",
                    "reason": None if raw is not None else "Not captured in this view",
                }
            structured_row[column] = cell
        structured_rows.append(structured_row)

    content["columns"] = copy.deepcopy(columns)
    content["rows"] = structured_rows
    row_types: dict[str, str | None] = {}
    for source in (original_content, additional.get("content")):
        if not isinstance(source, dict):
            continue
        source_columns = source.get("columns") or []
        source_rows = source.get("rows") or []
        source_types = source.get("row_types") or []
        first_column = source_columns[0] if source_columns else None
        if not isinstance(first_column, str):
            continue
        for index, row in enumerate(source_rows):
            if (
                isinstance(row, dict)
                and index < len(source_types)
                and source_types[index] is not None
            ):
                cell = row.get(first_column)
                raw = cell.get("raw") if isinstance(cell, dict) else cell
                row_types[repr(raw)] = source_types[index]
    content["row_types"] = [
        row_types.get(repr(row[columns[0]]["raw"])) if columns else None
        for row in structured_rows
    ]


def _refresh_extraction_summary(
    extracted: dict[str, Any],
    additional: dict[str, Any] | None = None,
) -> None:
    elements = extracted.get("elements", [])
    if not isinstance(elements, list):
        return
    summary = extracted.setdefault("extraction_summary", {})
    if not isinstance(summary, dict):
        summary = {}
        extracted["extraction_summary"] = summary
    statuses = [
        str(element.get("extraction_status") or "complete")
        for element in elements
        if isinstance(element, dict)
    ]
    def count_unreadable(value: Any) -> int:
        if isinstance(value, dict):
            count = int(value.get("status") in {"unreadable", "not_visible"})
            return count + sum(count_unreadable(item) for item in value.values())
        if isinstance(value, list):
            return sum(count_unreadable(item) for item in value)
        return 0

    source_summaries = [summary]
    if isinstance(additional, dict) and isinstance(
        additional.get("extraction_summary"), dict
    ):
        source_summaries.append(additional["extraction_summary"])
    summary["visible_element_count"] = max(
        len(elements),
        *[
            int(item.get("visible_element_count") or 0)
            for item in source_summaries
        ],
    )
    summary["extracted_element_count"] = len(elements)
    summary["complete_element_count"] = statuses.count("complete")
    summary["partial_element_count"] = statuses.count("partial")
    summary["failed_element_count"] = statuses.count("failed")
    summary["skipped_element_count"] = sum(
        statuses.count(status)
        for status in ("skipped_exported", "skipped_export_option")
    )
    summary["unreadable_item_count"] = count_unreadable(elements)
    summary["requires_additional_screenshots"] = bool(
        any(
            item.get("requires_additional_screenshots")
            for item in source_summaries
        )
        or statuses.count("partial")
        or statuses.count("failed")
    )
    notes = []
    for item in source_summaries:
        notes.extend(item.get("notes") or [])
    summary["notes"] = list(dict.fromkeys(notes))


def _align_rows_to_headers(
    source_headers: list[Any],
    rows: list[Any],
    target_headers: list[str],
) -> list[list[Any]]:
    if not source_headers:
        return [
            list(row) + [None] * max(0, len(target_headers) - len(row))
            if isinstance(row, list)
            else [row] + [None] * (len(target_headers) - 1)
            for row in rows
        ]

    target_positions = {
        header.strip().casefold(): index for index, header in enumerate(target_headers)
    }
    source_positions = [
        target_positions.get(header.strip().casefold())
        if isinstance(header, str)
        else None
        for header in source_headers
    ]
    aligned_rows: list[list[Any]] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        aligned = [None] * len(target_headers)
        for source_index, value in enumerate(row):
            if source_index < len(source_positions):
                target_index = source_positions[source_index]
            else:
                target_index = source_index if source_index < len(aligned) else None
            if target_index is not None and target_index < len(aligned):
                aligned[target_index] = copy.deepcopy(value)
        aligned_rows.append(aligned)
    return aligned_rows


def _merge_value_sequences(
    existing: list[Any], additional: list[Any]
) -> list[Any]:
    overlap = min(len(existing), len(additional))
    while overlap and existing[-overlap:] != additional[:overlap]:
        overlap -= 1
    return copy.deepcopy(existing + additional[overlap:])


def _merge_rows(
    existing: list[list[Any]], additional: list[list[Any]]
) -> list[list[Any]]:
    merged = copy.deepcopy(existing)
    positions: dict[str, int] = {}
    date_pattern = re.compile(r"^\d{1,4}[/-]\d{1,2}[/-]\d{1,4}$")

    def row_key(row: list[Any]) -> str | None:
        if not row or not isinstance(row[0], str):
            return None
        label = row[0].strip()
        if label.casefold() in {"total", "grand total"}:
            return label.casefold()
        return label.casefold() if date_pattern.fullmatch(label) else None

    for index, row in enumerate(merged):
        key = row_key(row)
        if key is not None:
            positions[key] = index
    for row in additional:
        key = row_key(row)
        if key is not None and key in positions:
            existing = merged[positions[key]]
            width = max(len(existing), len(row))
            merged_row = [
                copy.deepcopy(existing[index]) if index < len(existing) else None
                for index in range(width)
            ]
            for index, value in enumerate(row):
                if value is not None:
                    merged_row[index] = copy.deepcopy(value)
            merged[positions[key]] = merged_row
        elif key is not None:
            positions[key] = len(merged)
            merged.append(copy.deepcopy(row))
        elif row not in merged:
            merged.append(copy.deepcopy(row))
    return merged


async def _extract_screenshot_sequence(
    screenshots: list[bytes],
    page_name: str,
    extraction_cache: dict[str, dict[str, Any]],
    exported_visual_titles: list[str] | None = None,
    performance_seconds: dict[str, float] | None = None,
    dashboard: str | None = None,
    job_id: str = "standalone",
    operation_log_path: Path | None = None,
    page: str | None = None,
    filter_state: str | None = None,
) -> dict[str, Any]:
    sequence_key = hashlib.sha256(
        page_name.encode("utf-8")
        + b"\0"
        + b"".join(hashlib.sha256(image).digest() for image in screenshots)
        + b"\0"
        + json.dumps(
            sorted(
                title.casefold().strip()
                for title in exported_visual_titles or []
                if isinstance(title, str) and title.strip()
            ),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    if sequence_key in extraction_cache:
        return extraction_cache[sequence_key]
    try:
        extraction_started_at = time.perf_counter()
        try:
            with measure_stage(
                operation_log_path,
                job_id=job_id,
                operation="gemini_scrolled_screenshot_extraction",
                dashboard=dashboard,
                page=page or page_name,
                filter_state=filter_state,
            ):
                model_timing_context = (
                    {
                        "job_id": job_id,
                        "operation_log_path": operation_log_path,
                        "dashboard": dashboard,
                        "filter_state": filter_state,
                    }
                    if operation_log_path is not None
                    else {}
                )
                if exported_visual_titles:
                    extracted = await extract_screenshots(
                        screenshots,
                        page_name,
                        exported_visual_titles,
                        **model_timing_context,
                    )
                else:
                    extracted = await extract_screenshots(
                        screenshots, page_name, **model_timing_context
                    )
        finally:
            if performance_seconds is not None:
                performance_seconds["extraction_model_seconds"] = (
                    performance_seconds.get("extraction_model_seconds", 0.0)
                    + time.perf_counter()
                    - extraction_started_at
                )
    except ValueError as exc:
        retryable_schema_error = (
            "agent returned invalid JSON" in str(exc)
            or "does not match schema version 1.0" in str(exc)
            or isinstance(exc, TruncatedAgentResponseError)
        )
        if not retryable_schema_error or len(screenshots) < 2:
            raise
        midpoint = len(screenshots) // 2
        extracted = await _extract_screenshot_sequence(
            screenshots[:midpoint],
            page_name,
            extraction_cache,
            exported_visual_titles,
            performance_seconds,
            dashboard,
            job_id,
            operation_log_path,
            page,
            filter_state,
        )
        second_half = await _extract_screenshot_sequence(
            screenshots[midpoint:],
            page_name,
            extraction_cache,
            exported_visual_titles,
            performance_seconds,
            dashboard,
            job_id,
            operation_log_path,
            page,
            filter_state,
        )
        extracted = _merge_extractions(extracted, second_half)
    extraction_cache[sequence_key] = extracted
    return extracted


def _merge_exported_elements(
    extracted: dict[str, Any], exported: list[dict[str, Any]]
) -> dict[str, Any]:
    merged = copy.deepcopy(extracted)
    elements = merged.setdefault("elements", [])
    merged["screenshot_elements"] = copy.deepcopy(
        merged.get("screenshot_elements", elements)
    )
    exported_data = merged.get("exported_data")
    if not isinstance(exported_data, list):
        exported_data = []
        merged["exported_data"] = exported_data
    exported_positions = {
        _normalized_control_text(str(item.get("title") or "")): index
        for index, item in enumerate(exported_data)
        if isinstance(item, dict)
    }
    by_title = {
        re.sub(r"\s+", " ", str(item.get("title") or "")).strip().casefold(): index
        for index, item in enumerate(elements)
        if isinstance(item, dict)
    }
    merged_visual_indices: set[int] = set()
    for item in exported:
        title_key = re.sub(r"\s+", " ", item["title"]).strip().casefold()
        exported_item = copy.deepcopy(item)
        exported_item["data_sources"] = ["powerbi_export"]
        exported_index = exported_positions.get(
            _normalized_control_text(str(item.get("title") or ""))
        )
        if exported_index is None:
            exported_positions[_normalized_control_text(str(item.get("title") or ""))] = (
                len(exported_data)
            )
            exported_data.append(exported_item)
        else:
            exported_data[exported_index] = exported_item
        index = by_title.get(title_key)
        if index is None and exported_item.get("visual_bounds"):
            spatial_matches = [
                (
                    _export_option_spatial_score(element, exported_item, merged),
                    element_index,
                )
                for element_index, element in enumerate(elements)
                if element_index not in merged_visual_indices
                and isinstance(element, dict)
                and str(
                    element.get("type") or element.get("element_type") or ""
                ).casefold()
                in DATA_VISUAL_TYPES
            ]
            spatial_matches = [
                (score, element_index)
                for score, element_index in spatial_matches
                if score is not None
            ]
            if spatial_matches:
                best_score = max(score for score, _ in spatial_matches)
                best_indices = [
                    element_index
                    for score, element_index in spatial_matches
                    if abs(score - best_score) < 0.05
                ]
                if len(best_indices) == 1:
                    index = best_indices[0]
        if index is None:
            by_title[title_key] = len(elements)
            elements.append(copy.deepcopy(item))
            continue
        merged_visual_indices.add(index)
        if _normalized_control_text(
            str(elements[index].get("title") or "")
        ) != _normalized_control_text(str(item.get("title") or "")):
            elements[index]["export_option_visual_title"] = item.get("title")
        _merge_element_columns(elements[index], item)
        if item.get("values"):
            elements[index]["values"] = copy.deepcopy(item["values"])
        target = elements[index]
        target["exportOptionAvailable"] = item.get("exportOptionAvailable", True)
        target["export_status"] = item.get("export_status", "exported")
        target["export_data_format"] = item.get(
            "export_data_format", "default"
        )
        target["export_file_path"] = item.get("export_file_path")
        target["page_name"] = item.get("page_name")
        target["filter_state"] = item.get("filter_state")
        target["visual_id"] = item.get("visual_id")
        target["mask_status"] = item.get("mask_status")
        target["export_json_file"] = item.get("export_json_file")
        sources = list(target.get("data_sources") or ["screenshot"])
        if "powerbi_export" not in sources:
            sources.append("powerbi_export")
        target["data_sources"] = sources
    _refresh_extraction_summary(merged)
    return merged


def _mark_exported_screenshot_visuals(
    extracted: dict[str, Any],
    exported: list[dict[str, Any]],
    export_options: list[dict[str, Any]] | None = None,
) -> None:
    exported_titles = {
        _normalized_control_text(str(item.get("title") or ""))
        for item in exported
        if isinstance(item, dict) and item.get("title")
    }
    options = [
        item for item in export_options or [] if isinstance(item, dict)
    ]
    if not exported_titles and not options:
        return

    elements = extracted.get("elements")
    if not isinstance(elements, list):
        return
    used_options: set[int] = set()
    for element in elements:
        if not isinstance(element, dict):
            continue
        title = _normalized_control_text(str(element.get("title") or ""))
        element_type = str(
            element.get("type") or element.get("element_type") or ""
        ).casefold()
        option_index = _matching_export_option_index(
            element,
            options,
            used_options,
            extracted,
            allow_spatial=element_type in DATA_VISUAL_TYPES,
        )
        option = options[option_index] if option_index is not None else None
        if option_index is not None:
            used_options.add(option_index)
        if title in exported_titles or (
            option is not None and option.get("export_status") == "exported"
        ):
            element["screenshot_value_comparison_skipped"] = True
            element["extraction_status"] = "skipped_exported"
            element["uncertainties"] = list(
                dict.fromkeys(
                    [
                        *(element.get("uncertainties") or []),
                        "Screenshot values skipped because parsed export data "
                        "is compared separately.",
                    ]
                )
            )
            element["value"] = None
            element["values"] = []
            element["categories"] = []
            element["rows"] = []
            element["content"] = {}
            continue

        if (
            option is not None
            and option.get("exportOptionAvailable") is True
            and option.get("export_status") != "exported"
        ):
            element["extraction_status"] = "partial"
            detail = option.get("export_error") or option.get("export_status")
            element["uncertainties"] = list(
                dict.fromkeys(
                    [
                        *(element.get("uncertainties") or []),
                        "Export data was available but no parsed export was "
                        f"produced ({detail}); screenshot extraction was used "
                        "as fallback.",
                    ]
                )
            )
    _refresh_extraction_summary(extracted)


async def _dismiss_export_success_toasts(page: Page) -> None:
    toast_selector = (
        '[role="alert"], [role="status"], [class*="toast"], '
        '[class*="notification"]'
    )
    success_message = re.compile(
        r"successful(?:ly)?\s+export(?:ed)?|export(?:ed)?\s+(?:was\s+)?"
        r"successful(?:ly)?|your data was exported",
        re.I,
    )
    for frame in page.frames:
        for _ in range(100):
            toasts = frame.locator(toast_selector)
            toast = None
            message = ""
            for index in range(await toasts.count()):
                candidate = toasts.nth(index)
                if not await candidate.is_visible():
                    continue
                candidate_message = await candidate.inner_text()
                if success_message.search(candidate_message):
                    toast = candidate
                    message = candidate_message
                    break
            if toast is None:
                break

            dismissed = False
            for attempt in range(3):
                dismissal = await toast.evaluate(
                    """element => {
                      const isSuccess = /successful(?:ly)?\\s+export(?:ed)?|export(?:ed)?\\s+(?:was\\s+)?successful(?:ly)?|your data was exported/i;
                      const closeName = /\\b(close|dismiss)\\b/i;
                      const textOf = node => (
                        node.innerText || node.textContent || ''
                      ).replace(/\\s+/g, ' ').trim();
                      let wrapper = element;
                      let toastWrapper = null;
                      let liveRegion = null;
                      let closeButton = null;
                      let node = element;
                      if (!isSuccess.test(textOf(element))) return false;
                      for (let depth = 0; node && depth < 7; depth++) {
                        const role = node.getAttribute('role') || '';
                        const classes = typeof node.className === 'string'
                          ? node.className : '';
                        const nodeText = textOf(node);
                        if (isSuccess.test(nodeText) &&
                            /toast|notification/i.test(classes)) {
                          toastWrapper = node;
                        }
                        if (isSuccess.test(nodeText) &&
                            !liveRegion && (role === 'alert' || role === 'status')) {
                          liveRegion = node;
                        }
                        if (!closeButton) {
                          const buttons = node.querySelectorAll(
                            'button, [role="button"]'
                          );
                          for (const candidate of buttons) {
                            const label = [
                              candidate.getAttribute('aria-label'),
                              candidate.getAttribute('title'),
                              candidate.getAttribute('data-testid'),
                              candidate.innerText
                            ].filter(Boolean).join(' ');
                            const bounds = candidate.getBoundingClientRect();
                            if (closeName.test(label) && bounds.width > 0 &&
                                bounds.height > 0 && !candidate.disabled) {
                              closeButton = candidate;
                              break;
                            }
                          }
                        }
                        if (node === document.body) break;
                        node = node.parentElement;
                      }
                      wrapper = toastWrapper || liveRegion || element;
                      if (closeButton) {
                        closeButton.click();
                        return 'clicked';
                      }
                      if (wrapper.isConnected) wrapper.remove();
                      if (element.isConnected && isSuccess.test(textOf(element)))
                        element.remove();
                      return !element.isConnected && !wrapper.isConnected
                        ? 'removed' : false;
                    }"""
                )
                if dismissal == "clicked":
                    try:
                        await toast.wait_for(state="hidden", timeout=1_000)
                    except PlaywrightTimeoutError:
                        await toast.evaluate(
                            """element => {
                              const isSuccess = /successful(?:ly)?\\s+export(?:ed)?|export(?:ed)?\\s+(?:was\\s+)?successful(?:ly)?|your data was exported/i;
                              const textOf = node => (
                                node.innerText || node.textContent || ''
                              ).replace(/\\s+/g, ' ').trim();
                              let wrapper = element;
                              let node = element;
                              for (let depth = 0; node && depth < 7; depth++) {
                                const role = node.getAttribute('role') || '';
                                const classes = typeof node.className === 'string'
                                  ? node.className : '';
                                if (isSuccess.test(textOf(node)) &&
                                    /toast|notification/i.test(classes)) {
                                  wrapper = node;
                                } else if (
                                    isSuccess.test(textOf(node)) &&
                                    (role === 'alert' || role === 'status')) {
                                  wrapper = node;
                                }
                                if (node === document.body) break;
                                node = node.parentElement;
                              }
                              if (wrapper.isConnected) wrapper.remove();
                              if (element.isConnected &&
                                  isSuccess.test(textOf(element))) element.remove();
                              return !element.isConnected && !wrapper.isConnected;
                            }"""
                        )
                    if not await toast.is_visible():
                        dismissed = True
                        break
                elif not await toast.is_visible():
                    dismissed = True
                    break
                if attempt < 2:
                    await page.wait_for_timeout(100)
            if not dismissed:
                hidden = await toast.evaluate(
                    """element => {
                      const isSuccess = /successful(?:ly)?\\s+export(?:ed)?|export(?:ed)?\\s+(?:was\\s+)?successful(?:ly)?|your data was exported/i;
                      const textOf = node => (
                        node.innerText || node.textContent || ''
                      ).replace(/\\s+/g, ' ').trim();
                      if (!isSuccess.test(textOf(element))) return false;
                      let wrapper = element;
                      let node = element;
                      for (let depth = 0; node && depth < 7; depth++) {
                        const role = node.getAttribute('role') || '';
                        const classes = typeof node.className === 'string'
                          ? node.className : '';
                        if (isSuccess.test(textOf(node)) &&
                            (/toast|notification/i.test(classes) ||
                             role === 'alert' || role === 'status')) {
                          wrapper = node;
                        }
                        if (node === document.body) break;
                        node = node.parentElement;
                      }
                      wrapper.style.setProperty(
                        'display', 'none', 'important'
                      );
                      element.style.setProperty(
                        'display', 'none', 'important'
                      );
                      return true;
                    }"""
                )
                dismissed = bool(hidden) and not await toast.is_visible()
            if not dismissed:
                raise RuntimeError(
                    "Cannot dismiss the visible export-success notification "
                    f"before screenshot capture ({message.strip()})."
                )
        else:
            raise RuntimeError(
                "Too many export-success notifications remained during "
                "screenshot cleanup."
            )


def _extracted_data_visuals(extracted: dict[str, Any]) -> list[dict[str, Any]]:
    elements = extracted.get("screenshot_elements")
    if not isinstance(elements, list):
        elements = extracted.get("elements")
    return [
        element
        for element in elements or []
        if isinstance(element, dict)
        and str(element.get("type") or element.get("element_type") or "").casefold()
        in DATA_VISUAL_TYPES
    ]


def _valid_bounding_box(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, dict):
        return None
    coordinates = tuple(value.get(key) for key in ("x", "y", "width", "height"))
    if not all(
        isinstance(coordinate, (int, float)) and not isinstance(coordinate, bool)
        for coordinate in coordinates
    ):
        return None
    x, y, width, height = (float(coordinate) for coordinate in coordinates)
    if width <= 0 or height <= 0:
        return None
    return x, y, width, height


def _export_option_spatial_score(
    element: dict[str, Any],
    option: dict[str, Any],
    extracted: dict[str, Any],
) -> float | None:
    element_bounds = _valid_bounding_box(element.get("bounding_box"))
    normalized_element_bounds = False
    if element_bounds is None:
        element_bounds = _valid_bounding_box(
            element.get("normalized_bounding_box")
        )
        normalized_element_bounds = element_bounds is not None
    elif all(0 <= coordinate <= 1 for coordinate in element_bounds):
        normalized_element_bounds = True
    option_bounds = _valid_bounding_box(option.get("visual_bounds"))
    if element_bounds is None or option_bounds is None:
        return None

    if normalized_element_bounds:
        metadata = extracted.get("dashboard_metadata")
        if not isinstance(metadata, dict):
            return None
        image_width = metadata.get("image_width")
        image_height = metadata.get("image_height")
        if not (
            isinstance(image_width, (int, float))
            and isinstance(image_height, (int, float))
            and image_width > 0
            and image_height > 0
        ):
            return None
        option_bounds = (
            option_bounds[0] / image_width,
            option_bounds[1] / image_height,
            option_bounds[2] / image_width,
            option_bounds[3] / image_height,
        )

    element_x, element_y, element_width, element_height = element_bounds
    option_x, option_y, option_width, option_height = option_bounds
    intersection_width = max(
        0.0,
        min(element_x + element_width, option_x + option_width)
        - max(element_x, option_x),
    )
    intersection_height = max(
        0.0,
        min(element_y + element_height, option_y + option_height)
        - max(element_y, option_y),
    )
    intersection = intersection_width * intersection_height
    smaller_area = min(
        element_width * element_height, option_width * option_height
    )
    if smaller_area <= 0:
        return None
    score = intersection / smaller_area
    return score if score >= 0.5 else None


def _matching_export_option_index(
    element: dict[str, Any],
    export_options: list[dict[str, Any]],
    used_indices: set[int],
    extracted: dict[str, Any],
    *,
    allow_spatial: bool = True,
) -> int | None:
    title = _normalized_control_text(str(element.get("title") or ""))
    exact_matches = [
        index
        for index, option in enumerate(export_options)
        if index not in used_indices
        and _normalized_control_text(str(option.get("title") or "")) == title
    ]
    if exact_matches:
        return exact_matches[0]
    if not allow_spatial:
        return None

    spatial_matches = [
        (
            _export_option_spatial_score(element, option, extracted),
            index,
        )
        for index, option in enumerate(export_options)
        if index not in used_indices
    ]
    spatial_matches = [
        (score, index)
        for score, index in spatial_matches
        if score is not None
    ]
    if not spatial_matches:
        return None

    best_score = max(score for score, _ in spatial_matches)
    best_indices = [
        index
        for score, index in spatial_matches
        if abs(score - best_score) < 0.05
    ]
    availability_signatures = {
        (
            export_options[index].get("exportOptionAvailable"),
            export_options[index].get("export_status"),
        )
        for index in best_indices
    }
    if len(availability_signatures) != 1:
        raise RuntimeError(
            f"Could not unambiguously associate export availability with "
            f"data visual {element.get('title')!r}."
        )
    return best_indices[0]


def _filter_export_options_to_data_visuals(
    extracted: dict[str, Any], export_options: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    visual_elements = _extracted_data_visuals(extracted)
    visual_titles: set[str] = set()
    for element in visual_elements:
        title = _normalized_control_text(str(element.get("title") or ""))
        if title:
            visual_titles.add(title)

    retained = []
    for option in export_options:
        if not isinstance(option, dict):
            continue
        title = _normalized_control_text(str(option.get("title") or ""))
        overlaps_data_visual = any(
            _export_option_spatial_score(element, option, extracted) is not None
            for element in visual_elements
        )
        if (
            option.get("exportOptionAvailable") is True
            or option.get("export_status") == "exported"
            or title in visual_titles
            or overlaps_data_visual
        ):
            retained.append(option)
    return retained, len(export_options) - len(retained)


def _validate_data_visual_export_coverage(
    extracted: dict[str, Any], export_options: list[dict[str, Any]]
) -> None:
    used_options: set[int] = set()
    visual_elements = _extracted_data_visuals(extracted)
    for element in visual_elements:
        title = _normalized_control_text(str(element.get("title") or ""))
        if not title:
            raise RuntimeError(
                "A data visual was extracted without a title, so its export "
                "attempt cannot be verified."
            )
        option_index = _matching_export_option_index(
            element, export_options, used_options, extracted
        )
        if option_index is None:
            raise RuntimeError(
                f"No export availability check was recorded for data visual "
                f"{element.get('title')!r}."
            )
        used_options.add(option_index)
        option = export_options[option_index]
        status = option.get("export_status")
        available = option.get("exportOptionAvailable")
        if available is None and not isinstance(status, str):
            raise RuntimeError(
                f"Could not verify export availability for data visual "
                f"{element.get('title')!r}: no explicit status was recorded."
            )


def _attach_export_option_metadata(
    extracted: dict[str, Any], export_options: list[dict[str, Any]]
) -> dict[str, Any]:
    merged = copy.deepcopy(extracted)
    merged["export_options"] = copy.deepcopy(export_options)

    def attach(elements: list[Any]) -> None:
        used_options: set[int] = set()
        for element in elements:
            if not isinstance(element, dict):
                continue
            element_type = str(
                element.get("type") or element.get("element_type") or ""
            ).casefold()
            option_index = _matching_export_option_index(
                element,
                export_options,
                used_options,
                merged,
                allow_spatial=element_type in DATA_VISUAL_TYPES,
            )
            if option_index is not None:
                used_options.add(option_index)
                option = export_options[option_index]
                if _normalized_control_text(
                    str(option.get("title") or "")
                ) != _normalized_control_text(str(element.get("title") or "")):
                    element["export_option_visual_title"] = option.get("title")
                element["exportOptionAvailable"] = option["exportOptionAvailable"]
                element["export_status"] = option["export_status"]
                element["export_data_format"] = option["export_data_format"]
                element["export_file"] = option["export_file"]
                element["export_file_path"] = option.get("export_file_path")
                element["export_json_file"] = option.get("export_json_file")
                element["page_name"] = option.get("page_name")
                element["filter_state"] = option.get("filter_state")
                element["visual_id"] = option.get("visual_id")
                element["mask_status"] = option.get("mask_status")
                element["mask_error"] = option.get("mask_error")
                element["export_screenshot"] = option["screenshot"]
                element["export_dialog_screenshot"] = option.get(
                    "export_dialog_screenshot"
                )
                element["export_error"] = option.get("export_error")
            elif element_type in DATA_VISUAL_TYPES:
                element.setdefault("exportOptionAvailable", None)
                element.setdefault("export_status", "not_detected")
                element.setdefault("export_data_format", None)
                element.setdefault("export_file", None)
                element.setdefault("export_file_path", None)
                element.setdefault("export_screenshot", None)
                element.setdefault("export_dialog_screenshot", None)

    attach(merged.get("elements", []))
    screenshot_elements = merged.get("screenshot_elements")
    if isinstance(screenshot_elements, list):
        attach(screenshot_elements)
    return merged


async def _visual_export_menu_is_open(page: Page) -> bool:
    for frame in page.frames:
        menus = frame.get_by_role("menu")
        for menu_index in range(await menus.count()):
            if await menus.nth(menu_index).is_visible():
                return True

        menu_items = frame.get_by_role("menuitem")
        for item_index in range(await menu_items.count()):
            if await menu_items.nth(item_index).is_visible():
                return True
    return False


async def _find_export_data_option(page: Page) -> tuple[Any | None, bool]:
    export_option = None
    deadline = time.monotonic() + 3
    while export_option is None and time.monotonic() < deadline:
        for candidate_frame in page.frames:
            candidates = candidate_frame.get_by_role(
                "menuitem", name=re.compile(r"^Export data$", re.I)
            )
            for candidate_index in range(await candidates.count()):
                candidate = candidates.nth(candidate_index)
                if (
                    await candidate.is_visible()
                    and await candidate.is_enabled()
                ):
                    export_option = candidate
                    break
            if export_option is None:
                candidates = candidate_frame.get_by_text(
                    re.compile(r"^Export data$", re.I), exact=True
                )
                for candidate_index in range(await candidates.count()):
                    candidate = candidates.nth(candidate_index)
                    if await candidate.is_visible():
                        export_option = candidate
                        break
            if export_option is not None:
                break
        if export_option is None:
            await page.wait_for_timeout(100)
    menu_open = (
        True
        if export_option is not None
        else await _visual_export_menu_is_open(page)
    )
    return export_option, menu_open


async def _wait_for_export_dialog_button(page: Page) -> Any | None:
    deadline = time.monotonic() + EXPORT_DIALOG_BUTTON_TIMEOUT_MS / 1_000
    while time.monotonic() < deadline:
        for frame in page.frames:
            dialogs = frame.get_by_role("dialog")
            for dialog_index in range(await dialogs.count()):
                dialog = dialogs.nth(dialog_index)
                if not await dialog.is_visible():
                    continue
                buttons = dialog.get_by_role(
                    "button", name=re.compile(r"^Export$", re.I)
                )
                for button_index in range(await buttons.count()):
                    button = buttons.nth(button_index)
                    if await button.is_visible() and await button.is_enabled():
                        return button
        await page.wait_for_timeout(100)
    return None


async def _mask_exportable_visual(
    container: Any,
    marker: str,
    title: str,
    visual_index: int,
    visual_id: str | None = None,
) -> None:
    marker_attribute = f"data-export-skip-{marker}"
    marker_value = f"{marker}:{visual_index}"
    masked = await container.evaluate(
        """(element, config) => {
          const node = element.closest(
            'div.visualContainer, [data-visual-id]'
          );
          if (!node) return false;
          const visualId = node.getAttribute('data-visual-id') ||
            node.id || null;
          if (config.visualId && visualId !== config.visualId) return false;
          const titleNode = node.querySelector(
            '[role="heading"], h1, h2, h3, [class*="visualTitle"]'
          );
          const actualTitle = (
            titleNode?.innerText?.trim() ||
            node.getAttribute('aria-label') ||
            node.innerText?.trim().split(/\\n+/)[0] ||
            ''
          ).replace(/\\s+/g, ' ').trim().toLocaleLowerCase();
          if (config.titleKey && actualTitle !== config.titleKey) return false;
          const document = node.ownerDocument;
          if (document.querySelector(
            `[${config.markerAttribute}="${config.markerValue}"]`
          )) return true;
          const bounds = node.getBoundingClientRect();
          if (bounds.width <= 0 || bounds.height <= 0) return false;
          const titleBounds = titleNode?.getBoundingClientRect();
          const top = titleBounds
            ? Math.min(bounds.bottom, Math.max(bounds.top, titleBounds.bottom + 2))
            : bounds.top;
          if (bounds.bottom <= top) return false;
          const mask = document.createElement('div');
          mask.setAttribute(config.markerAttribute, config.markerValue);
          mask.setAttribute('aria-hidden', 'true');
          mask.title = config.title;
          Object.assign(mask.style, {
            position: 'absolute',
            left: `${bounds.left + window.scrollX}px`,
            top: `${top + window.scrollY}px`,
            width: `${bounds.width}px`,
            height: `${bounds.bottom - top}px`,
            background: '#ffffff',
            border: '2px solid #ffffff',
            boxSizing: 'border-box',
            zIndex: '2147483647',
            pointerEvents: 'none'
          });
          (document.body || document.documentElement).appendChild(mask);
          return true;
        }""",
        {
            "markerAttribute": marker_attribute,
            "markerValue": marker_value,
            "title": title,
            "titleKey": _normalized_control_text(title),
            "visualId": visual_id,
        },
    )
    if not masked:
        raise RuntimeError(
            f"Could not apply the export mask to visual {title!r} after its "
            "export attempt."
        )


async def _visual_more_options_button(
    frame: Any,
    container: Any,
    visual_title: str,
    visual_bounds: dict[str, float] | None = None,
    candidate_frames: list[Any] | None = None,
) -> Any | None:
    button = container.get_by_role(
        "button", name=re.compile(r"more options", re.I)
    )
    for button_index in range(await button.count()):
        candidate = button.nth(button_index)
        if await candidate.is_visible() and await candidate.is_enabled():
            return candidate

    frames = candidate_frames or [frame]
    if all(candidate_frame is not frame for candidate_frame in frames):
        frames = [frame, *frames]
    title_matches = []
    bounds_matches = []
    for candidate_frame in frames:
        buttons = candidate_frame.get_by_role(
            "button", name=re.compile(r"more options", re.I)
        )
        for button_index in range(await buttons.count()):
            candidate = buttons.nth(button_index)
            if not await candidate.is_visible() or not await candidate.is_enabled():
                continue
            if visual_bounds is not None:
                candidate_bounds = await candidate.bounding_box()
                if candidate_bounds:
                    center_x = candidate_bounds["x"] + candidate_bounds["width"] / 2
                    center_y = candidate_bounds["y"] + candidate_bounds["height"] / 2
                    if (
                        visual_bounds["x"] <= center_x
                        <= visual_bounds["x"] + visual_bounds["width"]
                        and visual_bounds["y"] <= center_y
                        <= visual_bounds["y"] + visual_bounds["height"]
                    ):
                        bounds_matches.append(candidate)
            associated_visual = await candidate.evaluate(
                """el => {
                  const node = el.closest(
                    'div.visualContainer, [data-visual-id], [role="group"]'
                  );
                  const heading = node?.querySelector(
                    '[role="heading"], h1, h2, h3, [class*="visualTitle"]'
                  );
                  return (heading?.innerText?.trim() ||
                    node?.getAttribute('aria-label') ||
                    node?.innerText?.trim().split(/\\n+/)[0] || '').trim();
                }"""
            )
            if _normalized_control_text(associated_visual or "") == (
                _normalized_control_text(visual_title)
            ):
                title_matches.append(candidate)
    if len(title_matches) == 1:
        return title_matches[0]
    if len(bounds_matches) == 1:
        return bounds_matches[0]
    return None


async def _wait_for_visual_more_options(
    page: Page,
    frame: Any,
    container: Any,
    visual_title: str,
    visual_bounds: dict[str, float] | None = None,
) -> Any | None:
    for attempt in range(MORE_OPTIONS_WAIT_ATTEMPTS):
        button = await _visual_more_options_button(
            frame,
            container,
            visual_title,
            visual_bounds,
            getattr(page, "frames", None),
        )
        if button is not None:
            return button
        if attempt < MORE_OPTIONS_WAIT_ATTEMPTS - 1:
            await page.wait_for_timeout(100)
    return None


def _needs_scrolled_view_fallback(
    visual_count: int, export_options: list[dict[str, Any]]
) -> bool:
    return visual_count == 0 or any(
        option.get("exportOptionAvailable") is False
        for option in export_options
    )


async def _export_visual_data(
    page: Page,
    export_directory: Path,
    performance_timings: list[dict[str, Any]] | None = None,
    job_id: str = "standalone",
    dashboard: str | None = None,
    page_name: str | None = None,
    filter_state: str | None = None,
    operation_log_path: Path | None = None,
) -> tuple[list[dict[str, Any]], bool, list[dict[str, Any]]]:
    export_directory.mkdir(parents=True, exist_ok=True)
    evidence_directory = export_directory / "evidence"
    evidence_directory.mkdir(parents=True, exist_ok=True)
    visuals: list[tuple[Any, Any, dict[str, Any]]] = []
    for frame in page.frames:
        containers = frame.locator(VISUAL_SELECTOR)
        for index in range(await containers.count()):
            container = containers.nth(index)
            if not await container.is_visible():
                continue
            visual = await container.evaluate(
                """el => {
                  const node = el.closest(
                    'div.visualContainer, [data-visual-id], [role="group"]'
                  );
                  if (!node) return null;
                  const heading = node.querySelector(
                    '[role="heading"], h1, h2, h3, [class*="visualTitle"]'
                  );
                  const title = heading?.innerText?.trim() ||
                    node.getAttribute('aria-label') ||
                    node.innerText?.trim().split(/\\n+/)[0] || '';
                  const type = node.querySelector('[role="table"], [role="grid"]')
                    ? 'table' : 'chart';
                  return {
                    title: title.trim(),
                    type,
                    visual_id: node.getAttribute('data-visual-id') ||
                      node.id || null
                  };
                }"""
            )
            if not visual or not visual["title"]:
                visual = {
                    "title": "",
                    "type": "unknown",
                    "visual_id": None,
                }
            visuals.append((frame, container, visual))

    exported: list[dict[str, Any]] = []
    export_options: list[dict[str, Any]] = []
    for index, (frame, container, visual) in enumerate(visuals):
        visual_started_at = time.perf_counter()
        record: dict[str, Any] = {
            "title": visual["title"],
            "element_type": visual["type"],
            "visual_id": visual.get("visual_id"),
            "visual_index": index,
            "page_name": page_name,
            "filter_state": filter_state,
            "exportOptionAvailable": None,
            "export_status": "more_options_unavailable",
            "export_data_format": None,
            "export_file": None,
            "export_file_path": None,
            "screenshot": None,
            "export_dialog_screenshot": None,
            "export_error": None,
            "visual_bounds": None,
            "mask_status": "not_applied",
        }
        write_operation_event(
            operation_log_path,
            job_id=job_id,
            operation="export_visual",
            event="started",
            outcome="in_progress",
            dashboard=dashboard,
            page=page_name,
            filter_state=filter_state,
            visual=visual["title"],
        )

        def record_visual_timing() -> None:
            outcome = (
                "failed"
                if record["export_status"]
                in {
                    "visual_identity_unavailable",
                    "more_options_unavailable",
                    "export_button_unavailable",
                    "download_timeout",
                    "downloaded_no_tabular_data",
                    "unsupported_export_format",
                    "download_file_missing",
                    "download_save_failed",
                    "download_unreadable_data",
                    "download_failed",
                    "visual_inspection_failed",
                    "export_interaction_failed",
                    "export_menu_unverified",
                }
                else "success"
            )
            write_operation_event(
                operation_log_path,
                job_id=job_id,
                operation="export_visual",
                event="finished",
                outcome=outcome,
                dashboard=dashboard,
                page=page_name,
                filter_state=filter_state,
                visual=visual["title"],
                duration_seconds=time.perf_counter() - visual_started_at,
            )
            if performance_timings is not None:
                performance_timings.append(
                    {
                        "title": visual["title"],
                        "export_status": record["export_status"],
                        "duration_seconds": round(
                            time.perf_counter() - visual_started_at, 3
                        ),
                    }
                )

        if not visual["title"]:
            record["export_status"] = "visual_identity_unavailable"
            record["export_error"] = (
                "Could not identify this candidate visual by title"
            )
            record_visual_timing()
            export_options.append(record)
            continue

        try:
            bounds = await container.bounding_box()
            if bounds and bounds["width"] > 0 and bounds["height"] > 0:
                record["visual_bounds"] = {
                    key: bounds[key]
                    for key in ("x", "y", "width", "height")
                }
            if bounds and bounds["width"] > 0 and bounds["height"] > 0:
                await page.mouse.move(
                    bounds["x"] + bounds["width"] / 2,
                    bounds["y"] + bounds["height"] / 2,
                )
            more_options = await _wait_for_visual_more_options(
                page, frame, container, visual["title"], bounds
            )
        except PlaywrightError as exc:
            record["export_status"] = "visual_inspection_failed"
            record["export_error"] = str(exc)
            record_visual_timing()
            export_options.append(record)
            continue
        if more_options is None:
            record["export_error"] = (
                "More options button did not become available after hovering "
                "the visual"
            )
            record["export_status"] = "more_options_unavailable"
            record_visual_timing()
            export_options.append(record)
            continue

        # Power BI's visual wrapper can intercept pointer events over this button.
        safe_title = _safe_path_component(visual["title"], 40)
        try:
            await more_options.evaluate("(element) => element.click()")
            await page.wait_for_timeout(150)
            export_option, export_menu_open = await _find_export_data_option(
                page
            )
            screenshot_path = (
                evidence_directory / f"{index:02d}_{safe_title}_options.png"
            )
            await page.screenshot(path=str(screenshot_path), full_page=False)
            record["screenshot"] = screenshot_path.relative_to(
                export_directory.parent
            ).as_posix()
        except PlaywrightError as exc:
            record["export_status"] = "export_interaction_failed"
            record["export_error"] = str(exc)
            record_visual_timing()
            export_options.append(record)
            continue

        async def finish_export_attempt() -> None:
            try:
                await page.keyboard.press("Escape")
            except PlaywrightError:
                logger.warning(
                    "Could not dismiss export UI for visual %s on page %s",
                    visual["title"],
                    page_name,
                    exc_info=True,
                )

        if export_option is None:
            if not export_menu_open:
                record["export_status"] = "export_menu_unverified"
                record["export_error"] = (
                    "The More options menu did not open, so export "
                    "availability could not be verified"
                )
                record_visual_timing()
                export_options.append(record)
                continue
            record["exportOptionAvailable"] = False
            record["export_status"] = "export_data_unavailable"
            record_visual_timing()
            export_options.append(record)
            await finish_export_attempt()
            continue
        record["exportOptionAvailable"] = True

        try:
            await export_option.click()
        except PlaywrightError as exc:
            record["export_status"] = "export_interaction_failed"
            record["export_error"] = str(exc)
            record_visual_timing()
            export_options.append(record)
            await finish_export_attempt()
            continue
        dialog_path = (
            evidence_directory
            / f"{index:02d}_{safe_title}_export_dialog.png"
        )
        try:
            await page.screenshot(path=str(dialog_path), full_page=False)
        except PlaywrightError as exc:
            record["export_status"] = "export_interaction_failed"
            record["export_error"] = str(exc)
            record_visual_timing()
            export_options.append(record)
            await finish_export_attempt()
            continue
        record["export_dialog_screenshot"] = dialog_path.relative_to(
            export_directory.parent
        ).as_posix()

        try:
            export_button = await _wait_for_export_dialog_button(page)
        except PlaywrightError as exc:
            record["export_status"] = "export_interaction_failed"
            record["export_error"] = str(exc)
            record_visual_timing()
            export_options.append(record)
            await finish_export_attempt()
            continue
        if export_button is None:
            record["export_status"] = "export_button_unavailable"
            record_visual_timing()
            export_options.append(record)
            await finish_export_attempt()
            continue

        record["export_data_format"] = "default"

        try:
            async with page.expect_download(
                timeout=EXPORT_DOWNLOAD_TIMEOUT_MS
            ) as download_info:
                await export_button.click(force=True, timeout=5_000)
            download = await download_info.value
        except PlaywrightTimeoutError:
            record["export_status"] = "download_timeout"
            record["export_error"] = (
                "No export download arrived within "
                f"{EXPORT_DOWNLOAD_TIMEOUT_MS / 1_000:.0f} seconds"
            )
            record_visual_timing()
            export_options.append(record)
            await finish_export_attempt()
            continue
        except PlaywrightError as exc:
            record["export_status"] = "download_failed"
            record["export_error"] = str(exc)
            record_visual_timing()
            export_options.append(record)
            await finish_export_attempt()
            continue

        extension = Path(download.suggested_filename).suffix.casefold()
        export_path = export_directory / f"{index:02d}_{safe_title}{extension}"
        try:
            await download.save_as(str(export_path))
        except Exception as exc:
            logger.exception(
                "Could not save export for visual %s on page %s",
                visual["title"],
                page_name,
            )
            record["export_status"] = "download_save_failed"
            record["export_error"] = str(exc)
            record_visual_timing()
            export_options.append(record)
            await finish_export_attempt()
            continue
        if not export_path.is_file() or export_path.stat().st_size == 0:
            record["export_status"] = "download_file_missing"
            record["export_file"] = export_path.name
            record["export_file_path"] = export_path.relative_to(
                export_directory.parent
            ).as_posix()
            record["export_error"] = (
                "Power BI reported a download, but the saved file is missing "
                "or empty"
            )
            record_visual_timing()
            export_options.append(record)
            await finish_export_attempt()
            continue
        if extension not in {".csv", ".xlsx"}:
            record["export_status"] = "unsupported_export_format"
            record["export_file"] = export_path.name
            record["export_file_path"] = export_path.relative_to(
                export_directory.parent
            ).as_posix()
            record["export_error"] = (
                f"Unsupported Power BI export format: "
                f"{extension or download.suggested_filename!r}"
            )
            record_visual_timing()
            export_options.append(record)
            await finish_export_attempt()
            continue
        json_path = export_path.with_suffix(".json")
        export_json_file = json_path.relative_to(export_directory.parent).as_posix()
        try:
            export_document = _export_json_document(
                visual["title"],
                download.suggested_filename,
                export_path.read_bytes(),
            )
            json_path.write_text(
                json.dumps(export_document, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            exported_element = _export_element_from_json(
                export_document, visual["type"]
            )
        except ValueError as exc:
            record.update(
                {
                    "export_status": "downloaded_no_tabular_data",
                    "export_file": export_path.name,
                    "export_file_path": export_path.relative_to(
                        export_directory.parent
                    ).as_posix(),
                    "export_json_file": export_json_file,
                    "export_error": str(exc),
                }
            )
            record_visual_timing()
            export_options.append(record)
            await finish_export_attempt()
            continue
        except Exception as exc:
            logger.exception(
                "Could not parse export for visual %s on page %s",
                visual["title"],
                page_name,
            )
            record.update(
                {
                    "export_status": "download_unreadable_data",
                    "export_file": export_path.name,
                    "export_file_path": export_path.relative_to(
                        export_directory.parent
                    ).as_posix(),
                    "export_error": str(exc),
                }
            )
            record_visual_timing()
            export_options.append(record)
            await finish_export_attempt()
            continue
        exported_element.update(
            {
                "exportOptionAvailable": True,
                "export_status": "exported",
                "export_data_format": "default",
                "export_file": export_path.name,
                "export_file_path": export_path.relative_to(
                    export_directory.parent
                ).as_posix(),
                "export_json_file": export_json_file,
                "data_sources": ["powerbi_export"],
                "visual_bounds": record.get("visual_bounds"),
                "visual_id": record.get("visual_id"),
                "visual_index": record.get("visual_index"),
                "page_name": page_name,
                "filter_state": filter_state,
            }
        )
        exported.append(exported_element)
        record.update(
            {
                "export_status": "exported",
                "export_data_format": "default",
                "export_file": export_path.name,
                "export_file_path": export_path.relative_to(
                    export_directory.parent
                ).as_posix(),
                "export_json_file": export_json_file,
                "export_error": None,
            }
        )
        await finish_export_attempt()
        record_visual_timing()
        export_options.append(record)

    await _move_pointer_off_dashboard(page)
    needs_scroll_fallback = _needs_scrolled_view_fallback(
        len(visuals), export_options
    )
    return exported, needs_scroll_fallback, export_options


async def _highlight_exported_visuals(
    page: Page,
    exported_visual_titles: list[str],
    marker: str | None = None,
    exported_visuals: list[dict[str, Any]] | None = None,
) -> str | None:
    if exported_visuals is None:
        targets = [
            {"title": title}
            for title in exported_visual_titles
            if isinstance(title, str) and title.strip()
        ]
    else:
        targets = [
            item
            for item in exported_visuals
            if isinstance(item, dict)
            and isinstance(item.get("title"), str)
            and item["title"].strip()
        ]
    if not targets:
        return None

    marker = marker or uuid4().hex
    candidates: list[tuple[int, Any, dict[str, Any]]] = []
    for frame in page.frames:
        containers = frame.locator(VISUAL_SELECTOR)
        for index in range(await containers.count()):
            container = containers.nth(index)
            if not await container.is_visible():
                continue
            identity = await container.evaluate(
                """element => {
                  const node = element.closest(
                    'div.visualContainer, [data-visual-id]'
                  );
                  if (!node) return null;
                  const heading = node.querySelector(
                    '[role="heading"], h1, h2, h3, [class*="visualTitle"]'
                  );
                  const title = (
                    heading?.innerText?.trim() ||
                    node.getAttribute('aria-label') ||
                    node.innerText?.trim().split(/\\n+/)[0] ||
                    ''
                  ).replace(/\\s+/g, ' ').trim().toLocaleLowerCase();
                  return {
                    title,
                    visualId: node.getAttribute('data-visual-id') ||
                      node.id || null
                  };
                }""",
            )
            if isinstance(identity, dict):
                candidates.append((len(candidates), container, identity))

    marked_targets = 0
    try:
        mask_targets: list[tuple[Any, str, int, str | None]] = []
        for target in targets:
            title = target["title"]
            title_key = _normalized_control_text(title)
            visual_id = target.get("visual_id")
            visual_index = target.get("visual_index")
            candidate = None
            if isinstance(visual_index, int) and 0 <= visual_index < len(candidates):
                indexed_candidate = candidates[visual_index]
                indexed_identity = indexed_candidate[2]
                if (
                    indexed_identity["title"] == title_key
                    and (
                        not isinstance(visual_id, str)
                        or indexed_identity.get("visualId") == visual_id
                    )
                ):
                    candidate = indexed_candidate
            if candidate is None and isinstance(visual_id, str) and visual_id:
                id_matches = [
                    item
                    for item in candidates
                    if item[2].get("visualId") == visual_id
                    and item[2]["title"] == title_key
                ]
                if len(id_matches) == 1:
                    candidate = id_matches[0]
            if candidate is None:
                title_matches = [
                    item
                    for item in candidates
                    if item[2]["title"] == title_key
                ]
                if len(title_matches) == 1:
                    candidate = title_matches[0]
            if candidate is None:
                raise RuntimeError(
                    "Could not uniquely locate exported visual "
                    f"{title!r} for masking."
                )
            candidate_index, candidate_container, _ = candidate
            mask_targets.append(
                (
                    candidate_container,
                    title,
                    candidate_index,
                    visual_id if isinstance(visual_id, str) else None,
                )
            )
        for container, title, candidate_index, visual_id in mask_targets:
            await _mask_exportable_visual(
                container,
                marker,
                title,
                candidate_index,
                visual_id,
            )
            marked_targets += 1
    except BaseException:
        try:
            await _clear_exported_visual_highlights(page, marker)
        except PlaywrightError:
            logger.warning(
                "Could not remove partial exported-visual masks",
                exc_info=True,
            )
        raise

    if marked_targets != len(targets):
        raise RuntimeError(
            "Could not mask every successfully exported visual in the "
            "extraction screenshot."
        )
    return marker


async def _clear_exported_visual_highlights(page: Page, marker: str) -> None:
    marker_attribute = f"data-export-skip-{marker}"
    for frame in page.frames:
        await frame.locator(
            f'div[{marker_attribute}^="{marker}"]'
        ).evaluate_all("(elements) => elements.forEach(element => element.remove())")


async def _extract_scrolled_views(
    page: Page,
    page_name: str,
    extraction_cache: dict[str, dict[str, Any]],
    screenshot_directory: Path,
    initial_screenshot: bytes | None = None,
    exported_visual_titles: list[str] | None = None,
    performance_seconds: dict[str, float] | None = None,
    dashboard: str | None = None,
    job_id: str = "standalone",
    operation_log_path: Path | None = None,
    filter_state: str | None = None,
) -> dict[str, Any]:
    screenshot_directory.mkdir(parents=True, exist_ok=True)
    screenshots: list[bytes] = []
    index = 0
    await _move_pointer_off_dashboard(page)

    for frame in page.frames:
        for direction in ("Scroll right", "Scroll down"):
            scroll_buttons = frame.get_by_role(
                "button", name=re.compile(rf"^{re.escape(direction)}$", re.I)
            )
            button_count = min(await scroll_buttons.count(), 100)
            for button_index in range(button_count):
                button = scroll_buttons.nth(button_index)
                for _ in range(20):
                    if not await button.is_visible() or not await button.is_enabled():
                        break
                    await _move_pointer_off_dashboard(page)
                    previous = await page.screenshot(full_page=False)
                    await button.evaluate("(element) => element.click()")
                    await page.wait_for_timeout(300)
                    await _move_pointer_off_dashboard(page)
                    index += 1
                    screenshot_path = (
                        screenshot_directory / f"scrolled_{index:02d}.png"
                    )
                    screenshot = await page.screenshot(
                        path=str(screenshot_path), full_page=False
                    )
                    if hashlib.sha256(screenshot).digest() == hashlib.sha256(
                        previous
                    ).digest():
                        break
                    screenshots.append(screenshot)

    if initial_screenshot is not None:
        screenshots.insert(0, initial_screenshot)
    if not screenshots:
        return {"page": page_name, "elements": []}
    return await _extract_screenshot_sequence(
        screenshots,
        page_name,
        extraction_cache,
        exported_visual_titles,
        performance_seconds,
        dashboard,
        job_id,
        operation_log_path,
        page_name,
        filter_state,
    )


async def _capture_state(
    page: Page,
    dashboard: str,
    page_name: str,
    selection: FilterSelection | FilterCombination | None,
    destination: Path,
    settle_seconds: float,
    extraction_cache: dict[str, dict[str, Any]],
    filter_inventory: list[dict[str, Any]] | None = None,
    job_id: str = "standalone",
    operation_log_path: Path | None = None,
    extract_visual_data: bool = True,
) -> DashboardState:
    timings: dict[str, float] = {}
    capture_started_at = time.perf_counter()
    state_name = "initial" if selection is None else selection.key
    stage_started_at = time.perf_counter()
    with measure_stage(
        operation_log_path,
        job_id=job_id,
        operation="wait_for_render",
        dashboard=dashboard,
        page=page_name,
        filter_state=state_name,
    ):
        await _wait_for_state_render(page, settle_seconds)
    render_warning = await _read_render_warning(page)
    timings["render_wait"] = time.perf_counter() - stage_started_at

    stage_started_at = time.perf_counter()
    await _move_pointer_off_dashboard(page)
    with measure_stage(
        operation_log_path,
        job_id=job_id,
        operation="capture_visual_layout",
        dashboard=dashboard,
        page=page_name,
        filter_state=state_name,
    ):
        visuals = await _read_visual_layout_inventory(
            page, filter_inventory
        )
    timings["visual_layout"] = time.perf_counter() - stage_started_at
    safe_page = _safe_path_component(page_name, 40)
    safe_state = _safe_path_component(state_name, 32)
    if isinstance(selection, FilterCombination):
        suffix = hashlib.sha256(state_name.encode("utf-8")).hexdigest()[:10]
        safe_state = f"{_safe_path_component(state_name, 21)}_{suffix}"
    screenshot_path = destination / safe_page / f"{safe_state}.png"
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)

    stage_started_at = time.perf_counter()
    with measure_stage(
        operation_log_path,
        job_id=job_id,
        operation="capture_screenshot",
        dashboard=dashboard,
        page=page_name,
        filter_state=state_name,
    ):
        screenshot_bytes = await page.screenshot(
            path=str(screenshot_path), full_page=True
        )
    timings["screenshot"] = time.perf_counter() - stage_started_at

    extraction_log_context = (
        {
            "dashboard": dashboard,
            "job_id": job_id,
            "operation_log_path": operation_log_path,
            "page": page_name,
            "filter_state": state_name,
        }
        if operation_log_path is not None
        else {}
    )
    stage_started_at = time.perf_counter()
    use_screenshot_extraction = (
        extract_visual_data and render_warning is None
    )
    if use_screenshot_extraction:
        with measure_stage(
            operation_log_path,
            job_id=job_id,
            operation="extract_screenshot_content",
            dashboard=dashboard,
            page=page_name,
            filter_state=state_name,
        ):
            extracted = await _extract_or_reuse(
                screenshot_bytes,
                page_name,
                extraction_cache,
                None,
                timings,
                **extraction_log_context,
            )
    else:
        write_operation_event(
            operation_log_path,
            job_id=job_id,
            operation="extract_screenshot_content",
            event="started",
            outcome="in_progress",
            dashboard=dashboard,
            page=page_name,
            filter_state=state_name,
        )
        extracted = {
            "schema_version": "1.0",
            "capture_mode": "visual_layout_filters",
            "dashboard_metadata": {
                "report_title": None,
                "page_name": page_name,
                "refresh_timestamp": None,
                "screenshot_id": screenshot_path.stem,
                "image_width": None,
                "image_height": None,
            },
            "elements": [],
            "title_items": [],
            "export_options": [],
            "extraction_summary": {
                "visible_element_count": 0,
                "extracted_element_count": 0,
                "complete_element_count": 0,
                "partial_element_count": 0,
                "failed_element_count": 0,
                "skipped_element_count": 0,
                "unreadable_item_count": 0,
                "requires_additional_screenshots": False,
                "notes": [
                    "Screenshot text and visual values were not extracted; "
                    "visual type/layout metadata and accessible filter "
                    "configuration were captured from the page DOM."
                ],
            },
        }
    if callable(getattr(page, "evaluate", None)):
        report_title = await _read_report_title(page)
        if report_title:
            metadata = extracted.get("dashboard_metadata")
            if not isinstance(metadata, dict):
                metadata = {}
                extracted["dashboard_metadata"] = metadata
            metadata["report_title"] = report_title
    if render_warning is not None:
        extracted["render_unverified"] = render_warning
        summary = extracted.get("extraction_summary")
        if isinstance(summary, dict):
            notes = summary.setdefault("notes", [])
            if isinstance(notes, list):
                notes.append(
                    f"{render_warning['reason']}; visual values are unverified."
                )
    timings["screenshot_extraction"] = time.perf_counter() - stage_started_at
    if not use_screenshot_extraction:
        write_operation_event(
            operation_log_path,
            job_id=job_id,
            operation="extract_screenshot_content",
            event="finished",
            outcome="skipped_value_extraction",
            dashboard=dashboard,
            page=page_name,
            filter_state=state_name,
            duration_seconds=timings["screenshot_extraction"],
        )

    stage_started_at = time.perf_counter()
    with measure_stage(
        operation_log_path,
        job_id=job_id,
        operation="read_filter_values",
        dashboard=dashboard,
        page=page_name,
        filter_state=state_name,
    ):
        filter_values = await _read_filter_values(page, filter_inventory or [])
    timings["merge_and_filter_read"] = time.perf_counter() - stage_started_at
    if not use_screenshot_extraction:
        stage_started_at = time.perf_counter()
        with measure_stage(
            operation_log_path,
            job_id=job_id,
            operation="capture_dom_title_labels",
            dashboard=dashboard,
            page=page_name,
            filter_state=state_name,
        ):
            title_items, refresh_timestamp = await _read_dom_title_items(
                page,
                visuals,
                filter_inventory or [],
                filter_values,
            )
        extracted["title_items"] = title_items
        extracted["dashboard_metadata"]["refresh_timestamp"] = (
            refresh_timestamp
        )
        extracted["extraction_summary"]["notes"] = [
            "DOM titles, refresh metadata, filter selections, and visual "
            "headers were captured; table cells and chart/KPI values were "
            "not extracted."
        ]
        timings["dom_title_labels"] = time.perf_counter() - stage_started_at
    title_items, added_filter_values = _merge_accessible_filter_values(
        extracted.get("title_items"), filter_values
    )
    extracted["title_items"] = title_items
    if added_filter_values:
        extraction_summary = extracted.get("extraction_summary")
        if isinstance(extraction_summary, dict):
            extraction_summary["title_item_count"] = len(title_items)
    timings["total"] = time.perf_counter() - capture_started_at
    extracted["capture_performance_seconds"] = {
        name: round(duration, 3)
        for name, duration in timings.items()
        if name != "extraction_cache_hits"
    }
    extracted["capture_performance_counts"] = {
        "extraction_cache_hits": int(timings.get("extraction_cache_hits", 0))
    }
    logger.info(
        "Captured dashboard state %s in %.3fs "
        "(render=%.3fs, screenshot=%.3fs, "
        "extraction=%.3fs, model=%.3fs, cache_hits=%.0f)",
        dashboard,
        timings["total"],
        timings["render_wait"],
        timings["screenshot"],
        timings["screenshot_extraction"],
        timings.get("extraction_model_seconds", 0.0),
        timings.get("extraction_cache_hits", 0.0),
    )
    return DashboardState(
        dashboard=dashboard,
        page=page_name,
        filter=selection,
        screenshot=screenshot_path,
        extracted=extracted,
        filter_names=[
            item["name"]
            for item in filter_inventory or []
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ],
        filter_values=filter_values,
        visuals=visuals,
        filter_options={
            item["name"]: list(item.get("values") or [])
            for item in filter_inventory or []
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        },
        filter_control_types={
            item["name"]: item.get("control_type")
            for item in filter_inventory or []
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        },
    )


async def prepare_filter_plans(
    url: str,
    destination: Path,
    storage_state: Path | None,
    headed: bool,
    job_id: str = "standalone",
    operation_log_path: Path | None = None,
) -> dict[str, dict[str, Any]]:
    destination.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        with measure_stage(
            operation_log_path,
            job_id=job_id,
            operation="launch_filter_planning_browser",
            dashboard="dashboard_A",
        ):
            browser = await playwright.chromium.launch(headless=not headed)
        context_options: dict[str, Any] = {
            "viewport": CAPTURE_VIEWPORT,
            "device_scale_factor": CAPTURE_DEVICE_SCALE_FACTOR,
            "accept_downloads": True,
        }
        if storage_state is not None:
            context_options["storage_state"] = str(storage_state)
        try:
            with measure_stage(
                operation_log_path,
                job_id=job_id,
                operation="create_filter_planning_context",
                dashboard="dashboard_A",
            ):
                context: BrowserContext = await browser.new_context(
                    **context_options
                )
            try:
                page = await context.new_page()
                with measure_stage(
                    operation_log_path,
                    job_id=job_id,
                    operation="navigate_filter_planning_report",
                    dashboard="dashboard_A",
                ):
                    await _goto_report(page, url)
                with measure_stage(
                    operation_log_path,
                    job_id=job_id,
                    operation="wait_for_filter_planning_report_ready",
                    dashboard="dashboard_A",
                ):
                    await _wait_for_report_ready(page)
                    await _wait_for_comparable_report_tabs(page)
                with measure_stage(
                    operation_log_path,
                    job_id=job_id,
                    operation="inventory_filter_planning_report",
                    dashboard="dashboard_A",
                ):
                    await _reset_report_filters(page)
                    await _wait_for_state_render(page, settle_seconds=2.0)
                    initial_inventory = await _accessible_inventory(
                        page, diagnostic_directory=destination
                    )
                page_names = _comparison_page_names(
                    initial_inventory["pages"] or ["Report"]
                )
            finally:
                await context.close()

            page_semaphore = asyncio.Semaphore(MAX_PARALLEL_PAGE_CAPTURES)

            async def prepare_page_plan(page_name: str) -> tuple[str, dict[str, Any]]:
                async with page_semaphore:
                    with measure_stage(
                        operation_log_path,
                        job_id=job_id,
                        operation="prepare_page_filter_plan",
                        dashboard="dashboard_A",
                        page=page_name,
                    ):
                        page_context: BrowserContext = await browser.new_context(
                            **context_options
                        )
                        try:
                            page = await page_context.new_page()
                            with measure_stage(
                                operation_log_path,
                                job_id=job_id,
                                operation="navigate_filter_plan_page",
                                dashboard="dashboard_A",
                                page=page_name,
                            ):
                                await _goto_report(page, url)
                            with measure_stage(
                                operation_log_path,
                                job_id=job_id,
                                operation="wait_for_filter_plan_page_ready",
                                dashboard="dashboard_A",
                                page=page_name,
                            ):
                                await _wait_for_report_ready(page)
                                await _wait_for_state_render(
                                    page, settle_seconds=2.0
                                )
                            with measure_stage(
                                operation_log_path,
                                job_id=job_id,
                                operation="select_filter_plan_page",
                                dashboard="dashboard_A",
                                page=page_name,
                            ):
                                await _find_page_tab(page, page_name)
                                await _reset_report_filters(page)
                                await _wait_for_state_render(
                                    page, settle_seconds=2.0
                                )
                                page_inventory = (
                                    await _inventory_page_after_clearing_slicers(
                                        page,
                                        diagnostic_directory=destination,
                                    )
                                )
                                cleared_slicers = page_inventory.get(
                                    "initial_cleared_slicers", []
                                )
                                await _prepare_slicer_selections(
                                    page,
                                    page_inventory["filters"],
                                    already_cleared_slicers=cleared_slicers,
                                    diagnostic_directory=destination,
                                )
                                page_inventory["filters"] = [
                                    item
                                    for item in page_inventory["filters"]
                                    if not item.get("skip_filter_cases")
                                ]
                            with measure_stage(
                                operation_log_path,
                                job_id=job_id,
                                operation="plan_page_filters",
                                dashboard="dashboard_A",
                                page=page_name,
                            ):
                                _, plan = await plan_from_dom(page_inventory)
                            return page_name, plan
                        finally:
                            await page_context.close()

            prepared_plans = await _capture_items_in_ordered_batches(
                page_names,
                prepare_page_plan,
                _page_worker_batch_size(len(page_names)),
            )
            return dict(prepared_plans)
        finally:
            with measure_stage(
                operation_log_path,
                job_id=job_id,
                operation="close_filter_planning_browser",
                dashboard="dashboard_A",
            ):
                await browser.close()


def _unverified_filter_case_state(
    exc: Exception,
    selection: FilterSelection | FilterCombination,
    dashboard: str,
    page_name: str,
    destination: Path,
    job_id: str,
    operation_log_path: Path | None,
) -> DashboardState:
    write_operation_event(
        operation_log_path,
        job_id=job_id,
        operation="filter_case_unverified",
        event="finished",
        outcome="skipped_unverified",
        dashboard=dashboard,
        page=page_name,
        filter_state=selection.key,
        error_type=type(exc).__name__,
    )
    safe_page = _safe_path_component(page_name, 40)
    unavailable_screenshot = (
        destination
        / safe_page
        / (
            "unverified_"
            + hashlib.sha256(selection.key.encode("utf-8")).hexdigest()[:16]
            + ".png"
        )
    )
    return DashboardState(
        dashboard=dashboard,
        page=page_name,
        filter=selection,
        screenshot=unavailable_screenshot,
        extracted={
            "capture_status": "unverified",
            "capture_error": str(exc),
        },
    )


async def _capture_filter_case(
    browser: Any,
    context_options: dict[str, Any],
    url: str,
    dashboard: str,
    page_name: str,
    selection: FilterSelection | FilterCombination,
    filter_inventory: list[dict[str, Any]],
    destination: Path,
    settle_seconds: float,
    extraction_cache: dict[str, dict[str, Any]],
    capture_semaphore: asyncio.Semaphore,
    job_id: str = "standalone",
    operation_log_path: Path | None = None,
    initially_cleared_slicers: list[str] | None = None,
    extract_visual_data: bool = True,
) -> DashboardState:
    wait_started_at = time.perf_counter()
    write_operation_event(
        operation_log_path,
        job_id=job_id,
        operation="wait_for_capture_slot",
        event="started",
        outcome="in_progress",
        dashboard=dashboard,
        page=page_name,
        filter_state=selection.key,
    )
    await capture_semaphore.acquire()
    write_operation_event(
        operation_log_path,
        job_id=job_id,
        operation="wait_for_capture_slot",
        event="finished",
        outcome="success",
        dashboard=dashboard,
        page=page_name,
        filter_state=selection.key,
        duration_seconds=time.perf_counter() - wait_started_at,
    )
    try:
        with measure_stage(
            operation_log_path,
            job_id=job_id,
            operation="capture_filter_case",
            dashboard=dashboard,
            page=page_name,
            filter_state=selection.key,
        ):
            with measure_stage(
                operation_log_path,
                job_id=job_id,
                operation="create_filter_context",
                dashboard=dashboard,
                page=page_name,
                filter_state=selection.key,
            ):
                context: BrowserContext = await browser.new_context(
                    **context_options
                )
            try:
                page = await context.new_page()
                with measure_stage(
                    operation_log_path,
                    job_id=job_id,
                    operation="navigate_to_filter_page",
                    dashboard=dashboard,
                    page=page_name,
                    filter_state=selection.key,
                ):
                    await _goto_report(page, url)
                with measure_stage(
                    operation_log_path,
                    job_id=job_id,
                    operation="wait_for_filter_page_ready",
                    dashboard=dashboard,
                    page=page_name,
                    filter_state=selection.key,
                ):
                    await _wait_for_report_ready(page)
                with measure_stage(
                    operation_log_path,
                    job_id=job_id,
                    operation="select_filter_page",
                    dashboard=dashboard,
                    page=page_name,
                    filter_state=selection.key,
                ):
                    await _find_page_tab(page, page_name)
                filters_to_clear = list(
                    dict.fromkeys(
                        [
                            *(
                                initially_cleared_slicers
                                if initially_cleared_slicers
                                else []
                            ),
                            *[
                                str(filter_item["name"])
                                for filter_item in filter_inventory
                                if isinstance(filter_item, dict)
                                and isinstance(filter_item.get("name"), str)
                                and filter_item["name"].strip()
                            ],
                        ]
                    )
                )
                with measure_stage(
                    operation_log_path,
                    job_id=job_id,
                    operation="clear_page_slicers",
                    dashboard=dashboard,
                    page=page_name,
                    filter_state=selection.key,
                ):
                    await _clear_page_slicer_selections(
                        page,
                        filters_to_clear or None,
                        require_all=False,
                    )
                    await _wait_for_state_render(page, settle_seconds=2.0)
                filter_selections = (
                    selection.selections
                    if isinstance(selection, FilterCombination)
                    else (selection,)
                )
                selected_filter_names = {
                    _normalized_control_text(item.name)
                    for item in filter_selections
                }
                bookmark_filter_names = {
                    item["name"].casefold()
                    for item in filter_inventory
                    if isinstance(item, dict)
                    and isinstance(item.get("name"), str)
                    and item.get("control_type") == "bookmark_buttons"
                }
                bookmark_selections = tuple(
                    filter_selection
                    for filter_selection in filter_selections
                    if filter_selection.name.casefold()
                    in bookmark_filter_names
                )
                try:
                    if bookmark_selections:
                        await _apply_filter_selections(
                            page,
                            bookmark_selections,
                            filter_inventory,
                            diagnostic_directory=destination,
                        )
                        await _wait_for_state_render(
                            page, settle_seconds=2.0
                        )
                    baseline_filters = [
                        item
                        for item in filter_inventory
                        if isinstance(item, dict)
                        and isinstance(item.get("name"), str)
                        and _normalized_control_text(item["name"])
                        not in selected_filter_names
                    ]
                    with measure_stage(
                        operation_log_path,
                        job_id=job_id,
                        operation="restore_baseline_filters",
                        dashboard=dashboard,
                        page=page_name,
                        filter_state=selection.key,
                    ):
                        if baseline_filters and not await _filters_match_baseline(
                            page, baseline_filters
                        ):
                            await _restore_report_filters(
                                page,
                                baseline_filters,
                                diagnostic_directory=destination,
                            )
                            if not await _filters_match_baseline(
                                page, baseline_filters
                            ):
                                raise RuntimeError(
                                    "Could not establish baseline filter "
                                    f"selections for dashboard {dashboard}, "
                                    f"page {page_name!r}"
                                )
                except UnavailableFilterOptionError as exc:
                    return _unverified_filter_case_state(
                        exc,
                        selection,
                        dashboard,
                        page_name,
                        destination,
                        job_id,
                        operation_log_path,
                    )
                try:
                    with measure_stage(
                        operation_log_path,
                        job_id=job_id,
                        operation="select_filter_combination",
                        dashboard=dashboard,
                        page=page_name,
                        filter_state=selection.key,
                    ):
                        await _apply_filter_selections(
                            page,
                            filter_selections,
                            filter_inventory,
                            diagnostic_directory=destination,
                        )
                except UnavailableFilterOptionError as exc:
                    return _unverified_filter_case_state(
                        exc,
                        selection,
                        dashboard,
                        page_name,
                        destination,
                        job_id,
                        operation_log_path,
                    )
                filters_to_reclear = [
                    name
                    for name in initially_cleared_slicers or []
                    if _normalized_control_text(name)
                    not in selected_filter_names
                ]
                if filters_to_reclear:
                    with measure_stage(
                        operation_log_path,
                        job_id=job_id,
                        operation="reclear_unselected_slicers_after_combination",
                        dashboard=dashboard,
                        page=page_name,
                        filter_state=selection.key,
                    ):
                        await _clear_page_slicer_selections(
                            page, filters_to_reclear, require_all=True
                        )
                        await _wait_for_state_render(page, settle_seconds=2.0)
                    for filter_selection in filter_selections:
                        filter_item = next(
                            (
                                item
                                for item in filter_inventory
                                if isinstance(item, dict)
                                and isinstance(item.get("name"), str)
                                and item["name"].casefold()
                                == filter_selection.name.casefold()
                            ),
                            None,
                        )
                        if filter_item is None:
                            raise RuntimeError(
                                f"Filter {filter_selection.name!r} is missing "
                                "from the capture inventory after clearing "
                                "unselected slicers"
                            )
                        selected = await _wait_for_filter_selection(
                            page, filter_item, filter_selection.value
                        )
                        if selected is None or not _filter_selection_matches(
                            filter_item, filter_selection.value, selected
                        ):
                            raise RuntimeError(
                                f"Clearing unselected slicers changed filter "
                                f"{filter_selection.name!r}; expected "
                                f"{filter_selection.value!r}, selected values: "
                                f"{selected!r}"
                            )
                return await _capture_state(
                    page,
                    dashboard,
                    page_name,
                    selection,
                    destination,
                    settle_seconds,
                    dict(extraction_cache),
                    filter_inventory=filter_inventory,
                    job_id=job_id,
                    operation_log_path=operation_log_path,
                    extract_visual_data=extract_visual_data,
                )
            finally:
                await context.close()
    finally:
        capture_semaphore.release()


async def _capture_items_in_ordered_batches(
    items: list[Any],
    capture_item: Callable[[Any], Awaitable[Any]],
    batch_size: int,
) -> list[Any]:
    captured: list[Any] = []
    for offset in range(0, len(items), batch_size):
        batch = items[offset : offset + batch_size]
        tasks = [
            asyncio.create_task(capture_item(item))
            for item in batch
        ]
        try:
            captured.extend(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
    return captured


def _page_worker_batch_size(page_count: int) -> int:
    if page_count < 1:
        raise ValueError("At least one report page is required for capture")
    return min(page_count, MAX_PARALLEL_PAGES_PER_DASHBOARD)


async def _apply_filter_selections(
    page: Page,
    selections: tuple[FilterSelection, ...],
    filter_inventory: list[dict[str, Any]],
    diagnostic_directory: Path | None = None,
) -> None:
    filters_by_name = {
        item["name"].casefold(): item
        for item in filter_inventory
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    ordered_selections = sorted(
        enumerate(selections),
        key=lambda indexed_selection: (
            filters_by_name.get(
                indexed_selection[1].name.casefold(), {}
            ).get("control_type")
            != "bookmark_buttons",
            indexed_selection[0],
        ),
    )
    for _, filter_selection in ordered_selections:
        await _select_filter(
            page,
            filter_selection,
            filter_inventory,
            diagnostic_directory=diagnostic_directory,
        )

    for filter_selection in selections:
        filter_item = filters_by_name.get(filter_selection.name.casefold())
        if filter_item is None:
            raise RuntimeError(
                f"Filter {filter_selection.name!r} is missing from the "
                "capture inventory"
            )
        selected = await _wait_for_filter_selection(
            page, filter_item, filter_selection.value
        )
        if selected is None or not _filter_selection_matches(
            filter_item, filter_selection.value, selected
        ):
            raise RuntimeError(
                f"Filter {filter_selection.name!r} changed while applying "
                f"the combination; expected {filter_selection.value!r}, "
                f"selected values: {selected!r}"
            )


def _filter_case_combinations(
    execution_plan: dict[str, Any],
) -> Iterator[FilterSelection | FilterCombination]:
    bookmark_variants = execution_plan.get("bookmark_variants")
    if bookmark_variants is not None:
        if not isinstance(bookmark_variants, list):
            raise ValueError("Bookmark variants must be a list")
        for variant in bookmark_variants:
            if not isinstance(variant, dict):
                raise ValueError("Each bookmark variant must be an object")
            combinations = variant.get("combinations")
            if not isinstance(combinations, list):
                raise ValueError(
                    "Each bookmark variant must have a combinations list"
                )
            for entries in combinations:
                if not isinstance(entries, list) or not entries:
                    raise ValueError(
                        "Each filter combination must contain selections"
                    )
                selections = tuple(
                    FilterSelection(
                        name=entry["name"],
                        value=entry["value"],
                    )
                    for entry in entries
                    if isinstance(entry, dict)
                    and isinstance(entry.get("name"), str)
                    and isinstance(entry.get("value"), str)
                )
                if len(selections) != len(entries):
                    raise ValueError(
                        "Filter combination selections require string names "
                        "and values"
                    )
                yield (
                    selections[0]
                    if len(selections) == 1
                    else FilterCombination(selections)
                )
        return

    planned_filters = execution_plan.get("filters", [])
    if not planned_filters:
        return
    for values in product(*(item["values"] for item in planned_filters)):
        selections = tuple(
            FilterSelection(name=item["name"], value=value)
            for item, value in zip(planned_filters, values)
        )
        yield (
            selections[0]
            if len(selections) == 1
            else FilterCombination(selections)
        )


def _build_bookmark_variant_execution_plan(
    execution_plan: dict[str, Any],
    scoped_inventories: list[
        tuple[tuple[FilterSelection, ...], dict[str, Any]]
    ],
    base_filter_inventory: list[dict[str, Any]],
    excluded_filter_names: Iterable[str] = (),
) -> dict[str, Any]:
    excluded_names = {
        _normalized_control_text(name) for name in excluded_filter_names
    }
    variants: list[dict[str, Any]] = []
    merged_filters: dict[str, dict[str, Any]] = {}
    seen_combinations: set[str] = set()

    for bookmark_selections, inventory in scoped_inventories:
        if not bookmark_selections:
            continue
        aligned_plan = _align_shared_plan(execution_plan, inventory)
        aligned_filters = [
            copy.deepcopy(item)
            for item in aligned_plan["filters"]
            if _normalized_control_text(item["name"]) not in excluded_names
        ]
        aligned_by_name = {
            _normalized_control_text(item["name"]): item
            for item in aligned_filters
        }

        for selection in bookmark_selections:
            normalized_name = _normalized_control_text(selection.name)
            aligned_filter = aligned_by_name.get(normalized_name)
            if aligned_filter is None:
                aligned_filter = {
                    "name": selection.name,
                    "values": [selection.value],
                }
                aligned_filters.append(aligned_filter)
                aligned_by_name[normalized_name] = aligned_filter
            else:
                aligned_filter["values"] = [selection.value]

        filter_order = {
            _normalized_control_text(item["name"]): index
            for index, item in enumerate(execution_plan.get("filters", []))
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        aligned_filters = [
            item
            for _, item in sorted(
                enumerate(aligned_filters),
                key=lambda indexed_item: (
                    filter_order.get(
                        _normalized_control_text(indexed_item[1]["name"]),
                        len(filter_order),
                    ),
                    indexed_item[0],
                ),
            )
        ]
        aligned_by_name = {
            _normalized_control_text(item["name"]): item
            for item in aligned_filters
        }

        capture_inventory: list[dict[str, Any]] = []
        for item in inventory.get("filters", []):
            if not isinstance(item, dict) or not isinstance(
                item.get("name"), str
            ):
                continue
            normalized_name = _normalized_control_text(item["name"])
            if normalized_name in excluded_names:
                continue
            capture_item = copy.deepcopy(item)
            aligned_filter = aligned_by_name.get(normalized_name)
            if aligned_filter is not None:
                capture_item["name"] = aligned_filter["name"]
                if normalized_name not in {
                    _normalized_control_text(selection.name)
                    for selection in bookmark_selections
                }:
                    capture_item["values"] = copy.deepcopy(
                        aligned_filter["values"]
                    )
            capture_inventory.append(capture_item)

        for selection in bookmark_selections:
            normalized_name = _normalized_control_text(selection.name)
            if any(
                _normalized_control_text(item["name"]) == normalized_name
                for item in capture_inventory
            ):
                continue
            base_item = next(
                (
                    item
                    for item in base_filter_inventory
                    if isinstance(item, dict)
                    and isinstance(item.get("name"), str)
                    and _normalized_control_text(item["name"])
                    == normalized_name
                ),
                None,
            )
            if base_item is None:
                raise ValueError(
                    f"Bookmark filter {selection.name!r} is missing from "
                    "the capture inventory"
                )
            capture_inventory.append(copy.deepcopy(base_item))

        combination_entries: list[list[dict[str, str]]] = []
        for combination in _filter_case_combinations(
            {"filters": aligned_filters}
        ):
            entries = (
                [combination.to_dict()]
                if isinstance(combination, FilterSelection)
                else [
                    selection.to_dict()
                    for selection in combination.selections
                ]
            )
            combination_key = json.dumps(
                entries, ensure_ascii=False, sort_keys=True
            )
            if combination_key in seen_combinations:
                continue
            seen_combinations.add(combination_key)
            combination_entries.append(entries)

        for item in aligned_filters:
            normalized_name = _normalized_control_text(item["name"])
            merged_item = merged_filters.setdefault(
                normalized_name,
                {"name": item["name"], "values": []},
            )
            for value in item["values"]:
                if value not in merged_item["values"]:
                    merged_item["values"].append(value)

        variants.append(
            {
                "bookmark_selections": [
                    selection.to_dict()
                    for selection in bookmark_selections
                ],
                "filter_inventory": capture_inventory,
                "combinations": combination_entries,
            }
        )

    return {
        "filters": list(merged_filters.values()),
        "bookmark_variants": variants,
    }


async def _discover_bookmark_variant_execution_plan(
    browser: Any,
    context_options: dict[str, Any],
    url: str,
    dashboard: str,
    page_name: str,
    page_inventory: dict[str, Any],
    execution_plan: dict[str, Any],
    destination: Path,
    settle_seconds: float,
    job_id: str,
    operation_log_path: Path | None,
) -> dict[str, Any]:
    bookmark_filters = [
        item
        for item in page_inventory.get("filters", [])
        if isinstance(item, dict)
        and item.get("control_type") == "bookmark_buttons"
        and isinstance(item.get("name"), str)
        and isinstance(item.get("values"), list)
        and item["values"]
    ]
    bookmark_names = {
        _normalized_control_text(item["name"])
        for item in bookmark_filters
    }
    if not bookmark_filters or not any(
        _normalized_control_text(item["name"]) not in bookmark_names
        for item in execution_plan.get("filters", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ):
        return execution_plan

    option_lists = [
        [
            value
            for value in item["values"]
            if isinstance(value, str) and value.strip()
        ]
        for item in bookmark_filters
    ]
    if any(not values for values in option_lists):
        raise ValueError(
            f"Bookmark filter options could not be read for page {page_name!r}"
        )

    scoped_inventories: list[
        tuple[tuple[FilterSelection, ...], dict[str, Any]]
    ] = []
    for values in product(*option_lists):
        bookmark_selections = tuple(
            FilterSelection(name=item["name"], value=value)
            for item, value in zip(bookmark_filters, values)
        )
        filter_state = "; ".join(
            selection.key for selection in bookmark_selections
        )
        context: BrowserContext = await browser.new_context(**context_options)
        try:
            with measure_stage(
                operation_log_path,
                job_id=job_id,
                operation="discover_bookmark_filter_options",
                dashboard=dashboard,
                page=page_name,
                filter_state=filter_state,
            ):
                page = await context.new_page()
                await _goto_report(page, url)
                await _wait_for_report_ready(page)
                await _find_page_tab(page, page_name)
                await _reset_report_filters(page)
                await _wait_for_state_render(
                    page, settle_seconds=settle_seconds
                )
                bookmark_inventory = (
                    await _inventory_page_after_clearing_slicers(
                        page, diagnostic_directory=destination
                    )
                )
                bookmark_capture_inventory = [
                    *bookmark_inventory["filters"],
                    *[
                        item
                        for item in page_inventory["filters"]
                        if isinstance(item, dict)
                        and item.get("control_type") == "bookmark_buttons"
                    ],
                ]
                await _apply_filter_selections(
                    page,
                    bookmark_selections,
                    bookmark_capture_inventory,
                    diagnostic_directory=destination,
                )
                await _wait_for_state_render(
                    page, settle_seconds=max(2.0, settle_seconds)
                )
                scoped_inventory = (
                    await _inventory_page_after_clearing_slicers(
                        page, diagnostic_directory=destination
                    )
                )
                scoped_inventories.append(
                    (bookmark_selections, scoped_inventory)
                )
        finally:
            await context.close()

    excluded_filter_names = page_inventory.get(
        "initial_cleared_slicers", []
    )
    return _build_bookmark_variant_execution_plan(
        execution_plan,
        scoped_inventories,
        page_inventory["filters"],
        excluded_filter_names,
    )


def _filter_inventory_for_selection(
    execution_plan: dict[str, Any],
    selection: FilterSelection | FilterCombination,
    default_inventory: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    variants = execution_plan.get("bookmark_variants")
    if variants is None:
        inventory = copy.deepcopy(default_inventory)
    else:
        if not isinstance(variants, list):
            raise ValueError("Bookmark variants must be a list")
        inventory = None
    selections = (
        [selection]
        if isinstance(selection, FilterSelection)
        else list(selection.selections)
    )
    if variants is not None:
        for variant in variants:
            if not isinstance(variant, dict):
                raise ValueError("Each bookmark variant must be an object")
            bookmark_selections = variant.get("bookmark_selections")
            if not isinstance(bookmark_selections, list):
                raise ValueError(
                    "Each bookmark variant must have bookmark selections"
                )
            if not all(
                any(
                    isinstance(entry, dict)
                    and isinstance(entry.get("name"), str)
                    and isinstance(entry.get("value"), str)
                    and _normalized_control_text(entry["name"])
                    == _normalized_control_text(selected.name)
                    and entry["value"] == selected.value
                    for selected in selections
                )
                for entry in bookmark_selections
            ):
                continue
            variant_inventory = variant.get("filter_inventory")
            if not isinstance(variant_inventory, list):
                raise ValueError(
                    "Each bookmark variant must have a filter inventory"
                )
            inventory = copy.deepcopy(variant_inventory)
            break
        else:
            raise ValueError(
                "The selected bookmark values are missing from the execution plan"
            )
    for item in inventory or []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        if item.get("control_type") != "bookmark_buttons":
            continue
        selected_bookmark = next(
            (
                selected.value
                for selected in selections
                if _normalized_control_text(selected.name)
                == _normalized_control_text(item["name"])
            ),
            None,
        )
        if selected_bookmark is not None:
            item["selected"] = [selected_bookmark]
    return inventory or []


async def _capture_filter_combinations_in_order(
    selections: Iterable[FilterSelection | FilterCombination],
    capture_filter_case: Callable[
        [FilterSelection | FilterCombination], Awaitable[Any]
    ],
    on_error: Callable[
        [FilterSelection | FilterCombination, Exception], Any
    ]
    | None = None,
) -> list[Any]:
    async def capture_one(selection: FilterSelection | FilterCombination) -> Any:
        try:
            return await capture_filter_case(selection)
        except (PlaywrightError, RuntimeError, TimeoutError) as exc:
            if on_error is None:
                raise
            return on_error(selection, exc)

    return list(
        await asyncio.gather(*(capture_one(selection) for selection in selections))
    )


def _append_capture_performance_record(
    destination: Path, state: DashboardState
) -> None:
    extracted = state.extracted
    record = {
        "dashboard": state.dashboard,
        "page": state.page,
        "state_key": state.state_key,
        "filter": (
            state.filter.to_dict()
            if state.filter is not None
            else None
        ),
        "completed_at_unix_seconds": time.time(),
        "capture_performance_seconds": extracted.get(
            "capture_performance_seconds", {}
        ),
        "capture_performance_counts": extracted.get(
            "capture_performance_counts", {}
        ),
        "visual_export_timings": extracted.get("visual_export_timings", []),
    }
    with (destination / "capture_performance.jsonl").open(
        "a", encoding="utf-8"
    ) as performance_file:
        performance_file.write(
            json.dumps(record, ensure_ascii=False) + "\n"
        )


def _initial_state_covered_by_filter_plan(
    filter_inventory: list[dict[str, Any]],
    execution_plan: dict[str, Any],
) -> dict[str, Any] | None:
    if "bookmark_variants" in execution_plan:
        for combination in _filter_case_combinations(execution_plan):
            selections = (
                [combination]
                if isinstance(combination, FilterSelection)
                else list(combination.selections)
            )
            covered_selections: list[dict[str, str]] = []
            for selection in selections:
                baseline = next(
                    (
                        item
                        for item in filter_inventory
                        if isinstance(item, dict)
                        and isinstance(item.get("name"), str)
                        and _normalized_control_text(item["name"])
                        == _normalized_control_text(selection.name)
                    ),
                    None,
                )
                selected = (
                    baseline.get("selected")
                    if baseline is not None
                    else None
                )
                if (
                    not isinstance(selected, list)
                    or len(selected) != 1
                    or not isinstance(selected[0], str)
                    or not _filter_values_match(
                        [selected[0]], [selection.value]
                    )
                ):
                    break
                covered_selections.append(selection.to_dict())
            else:
                if len(covered_selections) == 1:
                    return covered_selections[0]
                if covered_selections:
                    return {"selections": covered_selections}
        return None

    covered_selections: list[dict[str, str]] = []
    for planned_filter in execution_plan.get("filters", []):
        if not isinstance(planned_filter, dict):
            continue
        name = planned_filter.get("name")
        values = planned_filter.get("values")
        if not isinstance(name, str) or not isinstance(values, list):
            continue
        baseline = next(
            (
                item
                for item in filter_inventory
                if isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and item["name"].casefold() == name.casefold()
            ),
            None,
        )
        selected = baseline.get("selected") if baseline is not None else None
        if (
            not isinstance(selected, list)
            or len(selected) != 1
            or not isinstance(selected[0], str)
        ):
            return None
        matching_value = next(
            (
                value
                for value in values
                if isinstance(value, str)
                and _filter_values_match([selected[0]], [value])
            ),
            None,
        )
        if matching_value is None:
            return None
        covered_selections.append(
            {"name": name, "value": matching_value}
        )
    if len(covered_selections) == 1:
        return covered_selections[0]
    if covered_selections:
        return {"selections": covered_selections}
    return None


def _selection_signature(selection: Any) -> set[tuple[str, str]]:
    selections = getattr(selection, "selections", None)
    if selections is None:
        selections = [selection] if selection is not None else []
    return {
        (
            " ".join(str(item.name).split()).casefold(),
            " ".join(str(item.value).split()).casefold(),
        )
        for item in selections
    }


def _covered_state_signature(covered_by: dict[str, Any]) -> set[tuple[str, str]]:
    selections = covered_by.get("selections")
    if not isinstance(selections, list):
        selections = [covered_by]
    return {
        (
            " ".join(str(item.get("name") or "").split()).casefold(),
            " ".join(str(item.get("value") or "").split()).casefold(),
        )
        for item in selections
        if isinstance(item, dict)
    }


def _fit_coordinate_transform(
    anchors: list[tuple[float, float]],
) -> tuple[float, float] | None:
    if len(anchors) < 2:
        return None
    screen_values = [screen for screen, _canvas in anchors]
    canvas_values = [canvas for _screen, canvas in anchors]
    screen_mean = sum(screen_values) / len(screen_values)
    canvas_mean = sum(canvas_values) / len(canvas_values)
    variance = sum((value - screen_mean) ** 2 for value in screen_values)
    if variance <= 1e-9:
        return None
    slope = sum(
        (screen - screen_mean) * (canvas - canvas_mean)
        for screen, canvas in anchors
    ) / variance
    if slope <= 0:
        return None
    return slope, canvas_mean - slope * screen_mean


def _screenshot_to_canvas_transform(
    reference_visuals: dict[str, dict[str, Any]],
    detections: list[dict[str, Any]],
) -> tuple[tuple[float, float], tuple[float, float]]:
    by_id = {str(item.get("visual_id")): item for item in detections}
    x_anchors = []
    y_anchors = []
    for visual_id, visual in reference_visuals.items():
        bounds = by_id.get(visual_id, {}).get("bounds") or {}
        if not all(
            isinstance(bounds.get(key), (int, float))
            and isinstance(visual.get(key), (int, float))
            for key in ("x", "y", "width", "height")
        ):
            continue
        x_anchors.append(
            (
                float(bounds["x"]) + float(bounds["width"]) / 2,
                float(visual["x"]) + float(visual["width"]) / 2,
            )
        )
        y_anchors.append(
            (
                float(bounds["y"]) + float(bounds["height"]) / 2,
                float(visual["y"]) + float(visual["height"]) / 2,
            )
        )

    max_x = max(
        (
            float(visual["x"]) + float(visual["width"])
            for visual in reference_visuals.values()
        ),
        default=1000.0,
    )
    max_y = max(
        1000.0,
        max(
            (
                float(visual["y"]) + float(visual["height"])
                for visual in reference_visuals.values()
            ),
            default=1000.0,
        ),
    )
    return (
        _fit_coordinate_transform(x_anchors) or (max_x / 1000, 0.0),
        _fit_coordinate_transform(y_anchors) or (max_y / 1000, 0.0),
    )


def _project_screenshot_bounds(
    bounds: dict[str, Any],
    transform: tuple[tuple[float, float], tuple[float, float]],
) -> dict[str, float] | None:
    if not all(
        isinstance(bounds.get(key), (int, float))
        for key in ("x", "y", "width", "height")
    ):
        return None
    (scale_x, offset_x), (scale_y, offset_y) = transform
    width = float(bounds["width"]) * scale_x
    height = float(bounds["height"]) * scale_y
    center_x = (
        float(bounds["x"]) + float(bounds["width"]) / 2
    ) * scale_x + offset_x
    center_y = (
        float(bounds["y"]) + float(bounds["height"]) / 2
    ) * scale_y + offset_y
    return {
        "x": center_x - width / 2,
        "y": center_y - height / 2,
        "width": width,
        "height": height,
    }


def _apply_visual_type_detections(
    states: list[DashboardState],
    reference_state: DashboardState,
    detections: list[dict[str, Any]],
) -> None:
    reference_visuals = {
        str(visual.get("visual_id")): visual
        for visual in reference_state.visuals
        if isinstance(visual, dict) and visual.get("visual_id")
    }
    detections_by_id = {
        str(detection["visual_id"]): detection
        for detection in detections
    }
    reference_state_key = reference_state.state_key
    missing_visual_ids = sorted(
        set(reference_visuals) - set(detections_by_id)
    )
    transform = _screenshot_to_canvas_transform(
        reference_visuals, detections
    )
    additional_visuals = [
        detection
        for detection in detections
        if str(detection.get("visual_id") or "")
        not in reference_visuals
    ]

    for state in states:
        state.extracted["visual_type_detection"] = {
            "status": "partial" if missing_visual_ids else "complete",
            "source": "screenshot",
            "source_state_key": reference_state_key,
            "unclassified_visual_ids": missing_visual_ids,
            "visuals": detections,
        }
        for visual in state.visuals:
            if not isinstance(visual, dict):
                continue
            detection = detections_by_id.get(str(visual.get("visual_id") or ""))
            if detection is None:
                target_geometry = reference_visuals
                matches = []
                for visual_id, reference_visual in target_geometry.items():
                    if all(
                        isinstance(visual.get(key), (int, float))
                        and isinstance(reference_visual.get(key), (int, float))
                        and math.isclose(
                            float(visual[key]),
                            float(reference_visual[key]),
                            rel_tol=0.005,
                            abs_tol=2.0,
                        )
                        for key in ("x", "y", "width", "height")
                    ):
                        candidate = detections_by_id.get(visual_id)
                        if candidate is not None:
                            matches.append(candidate)
                if len(matches) == 1:
                    detection = matches[0]
            if detection is None:
                continue

            name = str(detection.get("visual_name") or "").strip()
            if name and name.casefold() not in {"unknown", "unidentified"}:
                visual["visual_name"] = name
            visual_type = str(detection.get("visual_type") or "").strip()
            if visual_type:
                normalized_type = (
                    "Unidentified"
                    if visual_type.casefold()
                    in {"unknown", "unidentified", "other / unidentified"}
                    else classify_visual_type(visual_type) or visual_type
                )
                visual["visual_type"] = normalized_type
            for key in ("rendering_status", "confidence", "observation"):
                if (
                    detection.get(key) is not None
                    and (
                        key != "rendering_status"
                        or state is reference_state
                    )
                ):
                    visual[key] = detection[key]
                elif key == "rendering_status" and state is not reference_state:
                    visual[key] = "Unable to Verify"
        existing_ids = {
            str(visual.get("visual_id") or "")
            for visual in state.visuals
            if isinstance(visual, dict)
        }
        for detection in additional_visuals:
            visual_id = str(detection["visual_id"])
            if visual_id in existing_ids:
                continue
            geometry = _project_screenshot_bounds(
                detection.get("bounds") or {}, transform
            )
            if geometry is None:
                raise ValueError(
                    f"Screenshot-only visual {visual_id} has no usable bounds"
                )
            visual = {
                "visual_id": visual_id,
                "visual_name": detection["visual_name"],
                "visual_type": detection["visual_type"],
                **geometry,
                "confidence": detection["confidence"],
            }
            if state is reference_state:
                visual["rendering_status"] = detection["rendering_status"]
                visual["observation"] = detection["observation"]
            else:
                visual["rendering_status"] = "Unable to Verify"
                visual["observation"] = (
                    "Rendering was not classified for this filter state."
                )
            state.visuals.append(visual)
            existing_ids.add(visual_id)


async def _detect_and_apply_page_visual_types(
    states: list[DashboardState],
    reference_state: DashboardState | None,
    *,
    dashboard: str,
    page_name: str,
    job_id: str,
    operation_log_path: Path | None,
) -> None:
    if reference_state is None:
        return
    screenshot_path = Path(reference_state.screenshot)
    if not screenshot_path.is_file():
        raise FileNotFoundError(
            "Representative screenshot is missing for visual type detection: "
            f"{screenshot_path}"
        )
    filter_state = (
        reference_state.filter.key
        if reference_state.filter is not None
        else "initial"
    )
    with measure_stage(
        operation_log_path,
        job_id=job_id,
        operation="detect_visual_types_from_screenshot",
        dashboard=dashboard,
        page=page_name,
        filter_state=filter_state,
    ):
        detections = await detect_visual_types_from_screenshot(
            screenshot_path.read_bytes(),
            page_name,
            reference_state.visuals,
            job_id=job_id,
            operation_log_path=operation_log_path,
            dashboard=dashboard,
            filter_state=filter_state,
        )
    _apply_visual_type_detections(states, reference_state, detections)


async def _capture_dashboard_page(
    browser: Any,
    context_options: dict[str, Any],
    url: str,
    dashboard: str,
    page_name: str,
    destination: Path,
    settle_seconds: float,
    shared_filter_plans: dict[str, dict[str, Any]] | None,
    capture_semaphore: asyncio.Semaphore,
    page_semaphore: asyncio.Semaphore,
    extraction_cache: dict[str, dict[str, Any]],
    on_page_prepared: Callable[
        [str, dict[str, Any], dict[str, Any]], None
    ] | None = None,
    job_id: str = "standalone",
    operation_log_path: Path | None = None,
    extract_visual_data: bool = True,
    include_filter_cases: bool = True,
    detect_visual_types: bool = False,
) -> dict[str, Any]:
    page_wait_started_at = time.perf_counter()
    write_operation_event(
        operation_log_path,
        job_id=job_id,
        operation="wait_for_page_slot",
        event="started",
        outcome="in_progress",
        dashboard=dashboard,
        page=page_name,
    )
    async with page_semaphore:
        write_operation_event(
            operation_log_path,
            job_id=job_id,
            operation="wait_for_page_slot",
            event="finished",
            outcome="success",
            dashboard=dashboard,
            page=page_name,
            duration_seconds=time.perf_counter() - page_wait_started_at,
        )
        with measure_stage(
            operation_log_path,
            job_id=job_id,
            operation="create_page_context",
            dashboard=dashboard,
            page=page_name,
        ):
            context: BrowserContext = await browser.new_context(**context_options)
        context_open = True
        try:
            page = await context.new_page()
            with measure_stage(
                operation_log_path,
                job_id=job_id,
                operation="navigate_to_report_page",
                dashboard=dashboard,
                page=page_name,
            ):
                await _goto_report(page, url)
            with measure_stage(
                operation_log_path,
                job_id=job_id,
                operation="wait_for_report_ready",
                dashboard=dashboard,
                page=page_name,
            ):
                await _wait_for_report_ready(page)
            with measure_stage(
                operation_log_path,
                job_id=job_id,
                operation="select_report_page",
                dashboard=dashboard,
                page=page_name,
            ):
                await _find_page_tab(page, page_name)
            with measure_stage(
                operation_log_path,
                job_id=job_id,
                operation="discover_page_inventory",
                dashboard=dashboard,
                page=page_name,
            ):
                if include_filter_cases:
                    await _reset_report_filters(page)
                if include_filter_cases:
                    page_inventory = (
                        await _inventory_page_after_clearing_slicers(
                            page, diagnostic_directory=destination
                        )
                    )
                else:
                    page_inventory = (
                        await _inventory_page_after_clearing_slicers(
                            page,
                            diagnostic_directory=destination,
                            clear_slicers=False,
                            inspect_filter_options=False,
                        )
                    )
                if include_filter_cases:
                    cleared_slicers = page_inventory.get(
                        "initial_cleared_slicers", []
                    )
                    await _prepare_slicer_selections(
                        page,
                        page_inventory["filters"],
                        already_cleared_slicers=cleared_slicers,
                        diagnostic_directory=destination,
                    )
                page_inventory["filters"] = [
                    item
                    for item in page_inventory["filters"]
                    if not item.get("skip_filter_cases")
                ]

            shared_plan = next(
                (
                    candidate
                    for name, candidate in (shared_filter_plans or {}).items()
                    if name.casefold() == page_name.casefold()
                ),
                None,
            )
            with measure_stage(
                operation_log_path,
                job_id=job_id,
                operation="build_filter_plan",
                dashboard=dashboard,
                page=page_name,
            ):
                if not include_filter_cases:
                    analysis = {
                        "pages": page_inventory["pages"],
                        "filters": page_inventory["filters"],
                    }
                    plan = {"filters": [], "mode": "visible_only"}
                    execution_plan = {"filters": []}
                elif shared_plan is not None:
                    analysis = {
                        "pages": page_inventory["pages"],
                        "filters": page_inventory["filters"],
                    }
                    plan = copy.deepcopy(shared_plan)
                    execution_plan = _align_shared_plan(
                        shared_plan, page_inventory
                    )
                else:
                    analysis, plan = await plan_from_dom(page_inventory)
                    execution_plan = plan

            if include_filter_cases and any(
                isinstance(item, dict)
                and item.get("control_type") == "bookmark_buttons"
                for item in page_inventory["filters"]
            ):
                with measure_stage(
                    operation_log_path,
                    job_id=job_id,
                    operation="build_bookmark_variant_plan",
                    dashboard=dashboard,
                    page=page_name,
                ):
                    execution_plan = (
                        await _discover_bookmark_variant_execution_plan(
                            browser,
                            context_options,
                            url,
                            dashboard,
                            page_name,
                            page_inventory,
                            execution_plan,
                            destination,
                            settle_seconds,
                            job_id,
                            operation_log_path,
                        )
                    )

            initial_state_covered_by = _initial_state_covered_by_filter_plan(
                page_inventory["filters"], execution_plan
            )
            if initial_state_covered_by is None:
                page_inventory["initial_state_capture"] = {
                    "status": "captured",
                }
            else:
                page_inventory["initial_state_capture"] = {
                    "status": "skipped_duplicate",
                    "covered_by": initial_state_covered_by,
                }

            page_plan = {
                "analysis": analysis,
                "plan": plan,
                "execution_plan": execution_plan,
            }
            if on_page_prepared is not None:
                on_page_prepared(page_name, page_inventory, page_plan)

            initial_state = None
            if initial_state_covered_by is None:
                capture_wait_started_at = time.perf_counter()
                write_operation_event(
                    operation_log_path,
                    job_id=job_id,
                    operation="wait_for_capture_slot",
                    event="started",
                    outcome="in_progress",
                    dashboard=dashboard,
                    page=page_name,
                    filter_state="initial",
                )
                async with capture_semaphore:
                    write_operation_event(
                        operation_log_path,
                        job_id=job_id,
                        operation="wait_for_capture_slot",
                        event="finished",
                        outcome="success",
                        dashboard=dashboard,
                        page=page_name,
                        filter_state="initial",
                        duration_seconds=(
                            time.perf_counter() - capture_wait_started_at
                        ),
                    )
                    initial_state = await _capture_state(
                        page,
                        dashboard,
                        page_name,
                        None,
                        destination,
                        settle_seconds,
                        extraction_cache,
                        filter_inventory=page_inventory["filters"],
                        job_id=job_id,
                        operation_log_path=operation_log_path,
                        extract_visual_data=extract_visual_data,
                    )
                _append_capture_performance_record(destination, initial_state)
            else:
                write_operation_event(
                    operation_log_path,
                    job_id=job_id,
                    operation="capture_initial_state",
                    event="finished",
                    outcome="skipped_duplicate",
                    dashboard=dashboard,
                    page=page_name,
                    filter_state="initial",
                )
            await context.close()
            context_open = False

            selections = _filter_case_combinations(execution_plan)

            async def capture_filter_case(
                selection: FilterSelection | FilterCombination,
            ) -> DashboardState:
                filter_inventory = _filter_inventory_for_selection(
                    execution_plan,
                    selection,
                    page_inventory["filters"],
                )
                state = await _capture_filter_case(
                    browser,
                    context_options,
                    url,
                    dashboard,
                    page_name,
                    selection,
                    filter_inventory,
                    destination,
                    settle_seconds,
                    extraction_cache,
                    capture_semaphore,
                    job_id,
                    operation_log_path,
                    page_inventory.get("initial_cleared_slicers", []),
                    extract_visual_data,
                )
                if state.extracted.get("capture_status") != "unverified":
                    _append_capture_performance_record(destination, state)
                return state

            filter_states = await _capture_filter_combinations_in_order(
                selections,
                capture_filter_case,
                on_error=lambda selection, exc: _unverified_filter_case_state(
                    exc,
                    selection,
                    dashboard,
                    page_name,
                    destination,
                    job_id,
                    operation_log_path,
                ),
            )
            states = (
                ([initial_state] if initial_state is not None else [])
                + filter_states
            )
            if detect_visual_types:
                reference_state = initial_state
                if reference_state is None and initial_state_covered_by is not None:
                    expected_signature = _covered_state_signature(
                        initial_state_covered_by
                    )
                    reference_state = next(
                        (
                            state
                            for state in filter_states
                            if _selection_signature(state.filter)
                            == expected_signature
                        ),
                        None,
                    )
                if reference_state is None and filter_states:
                    reference_state = filter_states[0]
                await _detect_and_apply_page_visual_types(
                    states,
                    reference_state,
                    dashboard=dashboard,
                    page_name=page_name,
                    job_id=job_id,
                    operation_log_path=operation_log_path,
                )
            return {
                "page_inventory": page_inventory,
                "plan": page_plan,
                "states": states,
            }
        finally:
            if context_open:
                await context.close()


async def capture_dashboard(
    url: str,
    dashboard: str,
    destination: Path,
    storage_state: Path | None,
    headed: bool,
    settle_seconds: float,
    extraction_cache: dict[str, dict[str, Any]] | None = None,
    shared_filter_plans: dict[str, dict[str, Any]] | None = None,
    capture_semaphore: asyncio.Semaphore | None = None,
    page_semaphore: asyncio.Semaphore | None = None,
    job_id: str = "standalone",
    operation_log_path: Path | None = None,
    on_state_captured: Callable[[str, DashboardState], None] | None = None,
    extract_visual_data: bool = False,
    include_filter_cases: bool = True,
    detect_visual_types: bool = False,
) -> list[DashboardState]:
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "capture_performance.jsonl").write_text(
        "", encoding="utf-8"
    )
    if extraction_cache is None:
        extraction_cache = {}
    async with async_playwright() as playwright:
        with measure_stage(
            operation_log_path,
            job_id=job_id,
            operation="launch_dashboard_browser",
            dashboard=dashboard,
        ):
            browser = await playwright.chromium.launch(headless=not headed)
        context_options: dict[str, Any] = {
            "viewport": CAPTURE_VIEWPORT,
            "device_scale_factor": CAPTURE_DEVICE_SCALE_FACTOR,
            "accept_downloads": True,
        }
        if storage_state is not None:
            context_options["storage_state"] = str(storage_state)
        if capture_semaphore is None:
            capture_semaphore = asyncio.Semaphore(
                MAX_PARALLEL_STATE_CAPTURES
            )
        if page_semaphore is None:
            page_semaphore = asyncio.Semaphore(
                MAX_PARALLEL_PAGE_CAPTURES
            )
        try:
            with measure_stage(
                operation_log_path,
                job_id=job_id,
                operation="create_page_discovery_context",
                dashboard=dashboard,
            ):
                discovery_context: BrowserContext = await browser.new_context(
                    **context_options
                )
            try:
                page = await discovery_context.new_page()
                with measure_stage(
                    operation_log_path,
                    job_id=job_id,
                    operation="navigate_dashboard_for_discovery",
                    dashboard=dashboard,
                ):
                    await _goto_report(page, url)
                with measure_stage(
                    operation_log_path,
                    job_id=job_id,
                    operation="wait_for_discovery_report_ready",
                    dashboard=dashboard,
                ):
                    await _wait_for_report_ready(page)
                    await _wait_for_comparable_report_tabs(page)
                with measure_stage(
                    operation_log_path,
                    job_id=job_id,
                    operation="discover_report_pages",
                    dashboard=dashboard,
                ):
                    if include_filter_cases:
                        await _reset_report_filters(page)
                    if include_filter_cases:
                        initial_inventory = await _accessible_inventory(
                            page, diagnostic_directory=destination
                        )
                    else:
                        initial_inventory = await _accessible_inventory(
                            page,
                            diagnostic_directory=destination,
                            clear_selection_actions=False,
                            inspect_filter_options=False,
                        )
                page_names = _comparison_page_names(
                    initial_inventory["pages"] or ["Report"]
                )
            finally:
                await discovery_context.close()

            page_inventories: dict[str, dict[str, Any]] = {}
            page_plans: dict[str, dict[str, Any]] = {}

            def persist_page_documents() -> None:
                with measure_stage(
                    operation_log_path,
                    job_id=job_id,
                    operation="save_page_inventory_and_filter_plan",
                    dashboard=dashboard,
                ):
                    dom_document = {
                        "pages": page_names,
                        "page_inventories": {
                            name: page_inventories[name]
                            for name in page_names
                            if name in page_inventories
                        },
                    }
                    plan_document = {
                        "pages": {
                            name: page_plans[name]
                            for name in page_names
                            if name in page_plans
                        }
                    }
                    (destination / "dom.json").write_text(
                        json.dumps(dom_document, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    (destination / "filter_plan.json").write_text(
                        json.dumps(plan_document, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )

            def on_page_prepared(
                page_name: str,
                page_inventory: dict[str, Any],
                page_plan: dict[str, Any],
            ) -> None:
                page_inventories[page_name] = page_inventory
                page_plans[page_name] = page_plan
                persist_page_documents()

            async def capture_page(page_name: str) -> dict[str, Any]:
                result = await _capture_dashboard_page(
                    browser,
                    context_options,
                    url,
                    dashboard,
                    page_name,
                    destination,
                    settle_seconds,
                    shared_filter_plans,
                    capture_semaphore,
                    page_semaphore,
                    dict(extraction_cache),
                    on_page_prepared,
                    job_id,
                    operation_log_path,
                    extract_visual_data,
                    include_filter_cases,
                    detect_visual_types,
                )
                if on_state_captured is not None:
                    for state in result["states"]:
                        on_state_captured(dashboard, state)
                return result

            page_results = await _capture_items_in_ordered_batches(
                page_names,
                capture_page,
                _page_worker_batch_size(len(page_names)),
            )
            states = [
                state
                for page_result in page_results
                for state in page_result["states"]
            ]
            with measure_stage(
                operation_log_path,
                job_id=job_id,
                operation="save_extracted_json",
                dashboard=dashboard,
            ):
                (destination / "extracted.json").write_text(
                    json.dumps(
                        [state.to_dict() for state in states],
                        indent=2,
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            return states
        finally:
            with measure_stage(
                operation_log_path,
                job_id=job_id,
                operation="close_dashboard_browser",
                dashboard=dashboard,
            ):
                await browser.close()


async def capture_dashboard_pair(
    url_a: str,
    url_b: str,
    destination: Path,
    storage_state: Path | None,
    headed: bool,
    settle_seconds: float,
    job_id: str = "standalone",
    operation_log_path: Path | None = None,
    on_state_captured: Callable[[str, DashboardState], None] | None = None,
    extract_visual_data: bool = False,
    include_filter_cases: bool = False,
    detect_visual_types: bool = False,
) -> tuple[list[DashboardState], list[DashboardState]]:
    operation_log_path = operation_log_path or destination / "operation_timings.jsonl"
    dashboard_a_directory = destination / "dashboard_A"
    dashboard_b_directory = destination / "dashboard_B"
    if include_filter_cases:
        with measure_stage(
            operation_log_path,
            job_id=job_id,
            operation="prepare_shared_filter_plans",
            dashboard="dashboard_A",
        ):
            shared_filter_plans = await prepare_filter_plans(
                url=url_a,
                destination=dashboard_a_directory,
                storage_state=storage_state,
                headed=headed,
                job_id=job_id,
                operation_log_path=operation_log_path,
            )
    else:
        shared_filter_plans = {}
        write_operation_event(
            operation_log_path,
            job_id=job_id,
            operation="prepare_shared_filter_plans",
            event="finished",
            outcome="skipped_visual_only",
            dashboard="dashboard_A",
        )
    capture_semaphore = asyncio.Semaphore(
        MAX_PARALLEL_STATE_CAPTURES
    )
    page_semaphore = asyncio.Semaphore(MAX_PARALLEL_PAGE_CAPTURES)

    return await asyncio.gather(
        capture_dashboard(
            url=url_a,
            dashboard="dashboard_A",
            destination=dashboard_a_directory,
            storage_state=storage_state,
            headed=headed,
            settle_seconds=settle_seconds,
            shared_filter_plans=shared_filter_plans,
            capture_semaphore=capture_semaphore,
            page_semaphore=page_semaphore,
            job_id=job_id,
            operation_log_path=operation_log_path,
            on_state_captured=on_state_captured,
            extract_visual_data=extract_visual_data,
            include_filter_cases=include_filter_cases,
            detect_visual_types=detect_visual_types,
        ),
        capture_dashboard(
            url=url_b,
            dashboard="dashboard_B",
            destination=dashboard_b_directory,
            storage_state=storage_state,
            headed=headed,
            settle_seconds=settle_seconds,
            shared_filter_plans=shared_filter_plans,
            capture_semaphore=capture_semaphore,
            page_semaphore=page_semaphore,
            job_id=job_id,
            operation_log_path=operation_log_path,
            on_state_captured=on_state_captured,
            extract_visual_data=extract_visual_data,
            include_filter_cases=include_filter_cases,
            detect_visual_types=detect_visual_types,
        ),
    )


def load_execution_plans(path: Path) -> dict[str, dict[str, Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    pages = document.get("pages")
    if not isinstance(pages, dict):
        raise ValueError(f"Filter plan file has no pages object: {path}")
    result: dict[str, dict[str, Any]] = {}
    for page_name, entry in pages.items():
        if not isinstance(entry, dict):
            raise ValueError(f"Invalid filter plan entry for page {page_name!r}")
        plan = entry.get("execution_plan")
        if not isinstance(plan, dict):
            raise ValueError(f"Page {page_name!r} has no execution plan")
        result[page_name] = plan
    return result
