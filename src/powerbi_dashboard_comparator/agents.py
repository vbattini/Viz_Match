import asyncio
import json
import hashlib
import logging
import os
import re
import struct
import weakref
from collections import Counter
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import aiohttp
from google.auth.exceptions import TransportError

from .config import (
    DEFAULT_MODEL_ID,
    configure_vertex_ai,
    positive_float_setting,
    positive_int_setting,
)
from .standard_elements import (
    reading_order as sort_reading_order,
    standard_element_name,
)
from .timing import measure_stage
from pydantic import BaseModel, Field, StrictStr, ValidationError

configure_vertex_ai()

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
logger = logging.getLogger(__name__)
MODEL_REQUEST_ATTEMPTS = positive_int_setting(
    "PBI_MODEL_REQUEST_ATTEMPTS", 5
)
MODEL_REQUEST_TIMEOUT_SECONDS = positive_float_setting(
    "PBI_MODEL_REQUEST_TIMEOUT_SECONDS", 300.0
)
MAX_PARALLEL_MODEL_REQUESTS = positive_int_setting(
    "PBI_MAX_PARALLEL_MODEL_REQUESTS", 10
)
DEFAULT_SCREENSHOT_MAX_OUTPUT_TOKENS = 65_536
MODEL_RETRYABLE_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
_model_request_semaphores: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


CellScalar = StrictStr | None


class ExtractedCell(BaseModel):
    raw: CellScalar = None
    status: Literal["read", "unreadable", "not_visible"] = "read"
    reason: str | None = None


class DashboardMetadata(BaseModel):
    report_title: str | None = None
    page_name: str | None = None
    refresh_timestamp: str | None = None
    screenshot_id: str = ""
    image_width: int | None = None
    image_height: int | None = None


class BoundingBox(BaseModel):
    x: float | None = None
    y: float | None = None
    width: float | None = None
    height: float | None = None


class ChartSeries(BaseModel):
    name: str
    values: list[ExtractedCell] = Field(default_factory=list)
    color: str | None = None
    marker_style: str | None = None


class DashboardElementContent(BaseModel):
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, ExtractedCell]] = Field(default_factory=list)
    row_types: list[str | None] = Field(default_factory=list)
    chart_type: str | None = None
    categories: list[ExtractedCell] = Field(default_factory=list)
    series: list[ChartSeries] = Field(default_factory=list)
    axes: dict[str, Any] = Field(default_factory=dict)
    legend: list[str] = Field(default_factory=list)
    annotations: list[str] = Field(default_factory=list)
    value: ExtractedCell | None = None
    unit: str | None = None
    secondary_values: dict[str, ExtractedCell] = Field(default_factory=dict)
    trend: dict[str, Any] = Field(default_factory=dict)
    filter_name: str | None = None
    selected_values: list[ExtractedCell] = Field(default_factory=list)
    visible_options: list[ExtractedCell] = Field(default_factory=list)
    selection_state: str | None = None
    text: ExtractedCell | None = None
    role: str | None = None
    values: list[ExtractedCell] = Field(default_factory=list)


class DashboardElement(BaseModel):
    element_id: str
    element_type: str
    title: str
    standard_element_name: str = "visual_title"
    element_text: str = ""
    associated_visual: str | None = None
    is_application_chrome: bool = False
    is_logo: bool = False
    reading_order: int | None = Field(default=None, ge=1)
    bounding_box: BoundingBox = Field(default_factory=BoundingBox)
    normalized_bounding_box: BoundingBox = Field(default_factory=BoundingBox)
    content: DashboardElementContent = Field(default_factory=DashboardElementContent)
    extraction_status: Literal[
        "complete",
        "partial",
        "failed",
        "skipped_exported",
        "skipped_export_option",
    ] = "partial"
    uncertainties: list[str] = Field(default_factory=list)


class DashboardTitle(BaseModel):
    title_id: str
    text: StrictStr
    category: StrictStr
    standard_element_name: str = "visual_title"
    element_text: str = ""
    element_type: str = "visual_title"
    associated_visual: str | None = None
    is_application_chrome: bool = False
    is_logo: bool = False
    reading_order: int | None = Field(default=None, ge=1)
    bounding_box: BoundingBox = Field(default_factory=BoundingBox)
    normalized_bounding_box: BoundingBox = Field(default_factory=BoundingBox)


class ExtractionSummary(BaseModel):
    visible_element_count: int = 0
    extracted_element_count: int = 0
    complete_element_count: int = 0
    partial_element_count: int = 0
    failed_element_count: int = 0
    skipped_element_count: int = 0
    unreadable_item_count: int = 0
    requires_additional_screenshots: bool = False
    notes: list[str] = Field(default_factory=list)


class ScreenshotExtraction(BaseModel):
    schema_version: str = "1.0"
    dashboard_metadata: DashboardMetadata = Field(default_factory=DashboardMetadata)
    elements: list[DashboardElement] = Field(default_factory=list)
    title_items: list[DashboardTitle] = Field(default_factory=list)
    extraction_summary: ExtractionSummary = Field(default_factory=ExtractionSummary)


class VisualTypeDetection(BaseModel):
    visual_id: StrictStr
    visual_name: StrictStr
    visual_type: StrictStr
    rendering_status: Literal[
        "Rendered",
        "Blank",
        "Partial",
        "Loading",
        "Error",
        "Unable to Verify",
    ]
    confidence: Literal["High", "Medium", "Low"]
    observation: StrictStr
    bounds: BoundingBox = Field(...)


class VisualTypeDetectionResult(BaseModel):
    visuals: list[VisualTypeDetection] = Field(default_factory=list)


DASHBOARD_COMPARISON_SKILL = """You are using the dashboard_url_comparator
extraction workflow. Extract all information visibly available in the supplied
dashboard screenshot, regardless of dashboard platform or layout. Extract one
dashboard capture independently; never compare dashboards at this stage. When
multiple supplied screenshots are successive overlapping views, merge only
repeated evidence of the same item in the overlap and preserve distinct
occurrences elsewhere. Use only visible evidence. Do not infer values that are
not legible, invent, calculate, infer hidden values, or silently correct
uncertain OCR. Preserve exact source text, spelling, capitalization,
punctuation, numeric formatting, zeroes, signs, and precision.

Keep report identity, dashboard-authored titles, and page identity separate.
When visible, capture the Power BI report name from its host header only as
dashboard_metadata.report_title; do not emit that host-header report name as a
dashboard_title or title_items record. Extract the dashboard-authored title
inside the report canvas as a title_items record with category
dashboard_title. These are distinct fields and must never be substituted for
or aligned with each other, even if their displayed text happens to match.
Store the selected report page name separately as dashboard_metadata.page_name.
Also extract every other visible title or label:
visual, chart, table, matrix, KPI/card, section, slicer/filter, axis, legend,
row/column header, subtitle, tooltip, button, tab, navigation/menu, annotation,
and metadata labels such as refresh date, owner, or updated date. Discover
items dynamically; do not assume a fixed layout. Avoid duplicates unless text
appears in distinct locations or has distinct roles. Exclude invisible cache
text and do not perform cache-text processing.

Exclude host/platform interface chrome from both elements and title_items:
browser controls, Power BI application toolbar commands/icons (such as File,
Export, Share, alerts, bookmarks, and menus), workspace/app sidebars (such as
Home, Pages, Workspaces, workspace lists, product/page navigation, report
navigators, and collapsed viewer-pane handles such as a vertical Filters tab.
Exclude decorative/icon-only images. Retain dashboard-authored branding as a
separate brand_logo title item, including its exact visible name (for example,
"SpartanNash"), and retain meaningful labeled icons such as Help and
Information. Mark host interface items with is_application_chrome=true;
identify dashboard branding with category=brand_logo and is_logo=true. Do not
mistake host logos or controls for dashboard branding.
The report name in the Power BI host header is the sole exception: capture it
as report-title metadata only, while continuing to exclude the rest of the host
chrome.
Do not exclude dashboard-authored visuals, dashboard/report titles, refresh
metadata, actual slicers/filter controls and their visible contents, or
meaningful report images. A host Filters-pane toggle is chrome; filter names
and values inside an opened pane are dashboard evidence only when they are
actual report filters in scope.

Return a separate title_items record for every title/label with exact text,
category, associated_visual, 1-based dashboard-wide reading_order, and
bounding_box. Use the fixed standard_element_name and element_type values
from this list, never invent a new name: dashboard_title, report_title,
page_title, page_navigation, visual_title, chart_title, table_title,
matrix_title, kpi_title, section_title, slicer_title, slicer_value, filter_title,
x_axis_title, y_axis_title, secondary_axis_title, legend_title, legend_item,
column_header, row_header, visual_subtitle, tooltip_title, button_label, icon,
brand_logo, navigation_label, annotation_title, metadata_label, metadata_value,
export_status, export_message, application_label, instruction_text.
For every slicer/filter, emit each selected value as a separate
`title_items` record with `slicer_value` type and the filter name in
`associated_visual`; never represent a selected value as a slicer title.
Store the exact displayed string in element_text (and text for compatibility),
element_type is the same fixed category, and keep associated_visual separate.
Use role categories consistently: use slicer_title for a slicer name and
slicer_value only for a selected value; use metadata_label/metadata_value only
for explicit metadata label/value pairs such as Last refresh; use legend_title
for the legend heading and legend_item for series labels; use axis-title types
for axis headings, not tick or category values. Use the most specific visual
title type supported by the visible visual (chart_title, table_title,
matrix_title, or kpi_title); use visual_title only when no specific visual type
is supported. Never change a role merely to make Dashboard A and Dashboard B
appear consistent; preserve uncertain or conflicting evidence.
Associate nested titles and labels with their containing visual or section
using its visible title; use null if the association cannot be established.
Record bounds as image-pixel coordinates when measurable, otherwise null.
The reading order is top-to-bottom, then left-to-right among items on the same
visual row; use visible boundaries for overlapping or nested objects. Preserve
each dashboard's original ordered sequence for insertion/deletion-aware
comparison. Sequence alignment may use text, type, association, and bounds as
combined evidence, but text alone must not establish correspondence; extract
each item's properties independently and retain its original reading_order.
Treat elements and title_items as complementary inventories. Include each
visible title or label in title_items even when the same evidence also appears
in a visual's structured content; the application merges only records for the
same visible occurrence. Preserve same-text items at distinct locations or
with distinct roles/associations. Keep distinct visible lines separate; do not
join neighboring lines into one text item.
Do not include Home, Pages, page tabs, navigation labels, or navigator controls
in either the element list or title_items. Page identity may remain in
dashboard_metadata.page_name without emitting navigation text as a title item.

Also extract the dashboard elements and their visible data. Return elements in
dashboard-wide reading order, not grouped by type. Use a distinct text element
for each actual report-authored text box or annotation. Do not create a
standalone element for a dashboard/report/page title, navigation label,
refresh label/value, axis or legend label, or table header; keep those in
title_items and attach visual labels to their owning visual. Capture every
visible table column and row in original order, including totals; never
deduplicate rows. Table columns must contain only displayed column headers,
while chart categories must contain axis/category values and series must keep
their own names and values. Do not treat chart categories as table headers.
For charts retain type, axes, categories, every visible series/value, legend,
units, styles, and annotations; never estimate values from geometry. For KPIs
retain primary and secondary values, units, trends, and comparison period.
For filters retain selected values, visible options, date ranges, and
selection state without assuming "All". Mark clipped, unreadable, or partial
evidence explicitly. Never claim off-screen content was extracted.
Screenshot extraction complements downloaded exports. For visuals explicitly
listed in the extraction prompt as having an available Export data option,
retain their visual titles and labels but skip screenshot data values. Parsed
export data is compared in the exported-data results; unsuccessful or
unparseable exports remain explicitly unverified. For other visuals, preserve
every visible series, category, and field and keep its source as screenshot
evidence.
When an extraction screenshot shows a solid white mask over a visual body,
the visual has a successfully parsed Export data result: do not transcribe its
chart/table data. The visual title remains outside the mask; capture its title,
type, bounds, and any labels still visible outside the mask. If the mask is
absent, still do not duplicate values from a successfully parsed export. Use
skipped_exported only when parsed export data is present; otherwise use
skipped_export_option and explain that screenshot values were skipped because
the export option was available.

Do not emit comparison statuses or invent confidence scores, similarity
percentages, embeddings, SSIM values, heatmaps, or visual-diff results."""


VISION_INSTRUCTIONS = DASHBOARD_COMPARISON_SKILL + """

Return exactly one JSON object, no Markdown or commentary, with this schema:
{
  "schema_version": "1.0",
  "dashboard_metadata": {
    "report_title": null,
    "page_name": "page name",
    "refresh_timestamp": null,
    "screenshot_id": "",
    "image_width": null,
    "image_height": null
  },
  "elements": [{
    "element_id": "TABLE_001",
    "element_type": "table",
    "standard_element_name": "table_title",
    "element_text": "exact displayed title",
    "title": "",
    "associated_visual": null,
    "is_application_chrome": false,
    "is_logo": false,
    "reading_order": 1,
    "bounding_box": {"x": null, "y": null, "width": null, "height": null},
    "normalized_bounding_box": {
      "x": null, "y": null, "width": null, "height": null
    },
    "content": {
      "columns": [],
      "rows": [],
      "row_types": [],
      "chart_type": null,
      "categories": [],
      "series": [],
      "axes": {},
      "legend": [],
      "annotations": [],
      "value": null,
      "unit": null,
      "secondary_values": {},
      "trend": {},
      "filter_name": null,
      "selected_values": [],
      "visible_options": [],
      "selection_state": null,
      "text": null,
      "role": null,
      "values": []
    },
    "extraction_status": "complete",
    "uncertainties": []
  }],
  "title_items": [{
    "title_id": "TITLE_001",
    "standard_element_name": "chart_title",
    "element_type": "chart_title",
    "element_text": "exact displayed title or label",
    "text": "exact displayed title or label",
    "category": "chart_title",
    "associated_visual": "containing visual title or null",
    "is_application_chrome": false,
    "is_logo": false,
    "reading_order": 1,
    "bounding_box": {"x": null, "y": null, "width": null, "height": null},
    "normalized_bounding_box": {
      "x": null, "y": null, "width": null, "height": null
    }
  }],
  "extraction_summary": {
    "visible_element_count": 0,
    "extracted_element_count": 0,
    "complete_element_count": 0,
    "partial_element_count": 0,
    "failed_element_count": 0,
    "skipped_element_count": 0,
    "unreadable_item_count": 0,
    "requires_additional_screenshots": false,
    "notes": []
  }
}
Use element_type values such as kpi, chart, table, matrix, filter, slicer,
text, button, icon, image, or metadata. For tables, content.columns is the
ordered list of headers and content.rows is a list of objects keyed by every
header; each cell is {"raw":"exact visible text","status":"read","reason":null}.
When totals/subtotals can be identified, content.row_types is aligned to rows
and records labels such as "total" or "subtotal"; otherwise use null.
Never omit rows or columns. If cell content is unreadable/outside the image, use
{"raw":null,"status":"unreadable","reason":"short reason"} or
{"raw":null,"status":"not_visible","reason":"short reason"} as appropriate.
Set reading_order to the 1-based position in the complete elements array.
For charts, content must include chart_type, categories, series, axes, legend,
and annotations; each series has name and category-aligned values. For KPIs,
content includes value, unit, secondary_values, and trend. Filters include
filter_name, selected_values, visible_options, and selection_state. Text,
buttons, and metadata include text and role. Preserve original displayed
representations. Use bounding_box coordinates only when reliably measurable;
otherwise use nulls. Do not claim complete underlying data beyond the supplied
image. Mark clipped/uncertain elements partial and explain them in
uncertainties. Ensure summary counts reflect the elements and unreadable cells.
Do not omit filter/slicer elements from extraction; comparison logic separates
their state from visual matching. Every title-like text item must also appear
in title_items exactly once per visible occurrence, with its category and
association, even when it is already present in an element title or content.
Do not suppress or omit text because another extraction field contains it.
Include the visible Power BI report header name as dashboard_metadata.report_title
and the Last refresh title/value when present. Do not create a title_items
record for the host-header report name. A dashboard-authored title inside the
report canvas must be a separate dashboard_title title_items record. Never use
one field as a substitute for the other. The title_items array must include
separate entries for dashboard branding (brand_logo), dashboard titles,
visual/chart/table/matrix/KPI titles, section headings, filter names, axis and
legend titles/names, table column and row headers, subtitles, tooltip titles,
meaningful labeled icons such as Help and Information, report button labels,
annotation headings, and visible metadata labels/values. Do not include
navigation labels or navigator controls. Return an empty
title_items list only when no title-like text is visible."""


def _model_name() -> str:
    return os.environ["GEMINI_MODEL"]


def _generation_config(
    model_name: str,
    *,
    max_output_tokens: int | None = None,
) -> types.GenerateContentConfig:
    config: dict[str, Any] = {}
    if not model_name.startswith(("gemini-3.7-", "gemini-3.8-")):
        config["temperature"] = 0
    if max_output_tokens is not None:
        config["max_output_tokens"] = max_output_tokens
    return types.GenerateContentConfig(**config)


VISUAL_TYPE_DETECTION_INSTRUCTIONS = """You are a screenshot-first dashboard
visual inventory and classification agent. Inspect the entire supplied
screenshot and identify every visible report-canvas visual container, including
charts or shapes that are absent from the candidate list. The candidate IDs,
titles, and bounds are locators only; classify from screenshot appearance, not
from an ID, title, DOM attribute, or HTML type. Exclude Power BI application
chrome, page navigation, and filter slicers, which are captured separately.

For each supplied candidate, reuse its visual_id if it is visible in the
screenshot. For any additional visible visual, assign an ID SCREENSHOT_001,
SCREENSHOT_002, and so on in top-to-bottom, then left-to-right reading order.
For every returned visual, provide its visible title or a concise descriptive
name supported by the screenshot, visual_type, rendering_status, confidence,
a short evidence-based observation, and screenshot bounds normalized from 0
to 1000 relative to the entire screenshot. Do not use generic "Visual <ID>"
names when a readable title or distinctive visible role exists. Use
"Unidentified" only when neither a reliable name nor type can be established.
Include every supplied candidate exactly once, even if its rendering status
is Unable to Verify.

Recognize Table, Matrix, Bar Chart (clustered/stacked/100% stacked), Column
Chart (clustered/stacked/100% stacked), Line Chart, Area Chart, Pie Chart,
Donut Chart, Treemap, Scatter Plot, Bubble Chart, KPI Card, Multi-row Card,
Gauge, Funnel Chart, Waterfall Chart, Ribbon Chart, Combo Chart, Map, Filled
Map, Heatmap, Histogram, Slicer / Dropdown, Text Box, Header, Shape / Line,
Image / Logo, and Button.
Distinguish types by visible structure. A blank visual may have an
Unidentified actual type; do not infer its actual type from its title.

Use rendering_status values Rendered, Blank, Partial, Loading, Error, or
Unable to Verify. A visible KPI value of 0.00 is Rendered; "(Blank)" is Blank.
A titled chart with an empty plotting area is Blank only when loading is
complete. Loading is not a confirmed failure. Keep visual type separate from
rendering status. A visible rule, divider, or line shape is Rendered when the
line is visible, even though it has no data labels or chart marks.

Do not transcribe KPI values, table cells, chart values, categories, or other
dashboard data. Do not compare dashboards. Do not invent text or types. Return
exactly one result for every supplied visual_id plus any additional
SCREENSHOT_NNN visual IDs for visible visual containers missing from the list.
Return bounds as x, y, width, and height, each on a 0-to-1000 scale relative to
the full screenshot."""


async def detect_visual_types_from_screenshot(
    screenshot_bytes: bytes,
    page_name: str,
    visuals: list[dict[str, Any]],
    *,
    job_id: str = "standalone",
    operation_log_path: Path | None = None,
    dashboard: str | None = None,
    filter_state: str | None = None,
) -> list[dict[str, Any]]:
    if not screenshot_bytes:
        raise ValueError("A screenshot is required for visual type detection")
    candidates = []
    for visual in visuals:
        if not isinstance(visual, dict):
            continue
        visual_id = str(visual.get("visual_id") or "").strip()
        if not visual_id:
            raise ValueError(
                "Visual type detection requires an ID for every visual"
            )
        candidates.append(
            {
                "visual_id": visual_id,
                "current_name_hint": visual.get("visual_name"),
                "bounds": {
                    key: visual.get(key)
                    for key in ("x", "y", "width", "height")
                },
            }
        )
    expected_ids = [item["visual_id"] for item in candidates]
    if len(expected_ids) != len(set(expected_ids)):
        raise ValueError("Visual type detection received duplicate visual IDs")
    with measure_stage(
        operation_log_path,
        job_id=job_id,
        operation="prepare_visual_type_detector",
        dashboard=dashboard,
        page=page_name,
        filter_state=filter_state,
    ):
        model_name = _model_name()
        agent = LlmAgent(
            name="visual_type_detector",
            model=model_name,
            instruction=VISUAL_TYPE_DETECTION_INSTRUCTIONS,
            output_schema=VisualTypeDetectionResult,
            generate_content_config=_generation_config(model_name),
        )
        prompt = (
            f"Classify the visual containers on page {page_name!r}. "
            "Use the screenshot as the primary evidence and the candidate "
            "bounds only to locate each container. Candidate visuals:\n"
            + json.dumps(candidates, ensure_ascii=False)
        )
        content = types.Content(
            role="user",
            parts=[
                types.Part(text=prompt),
                types.Part.from_bytes(
                    data=screenshot_bytes,
                    mime_type="image/png",
                ),
            ],
        )
    response = await _run_agent(
        agent,
        content,
        job_id=job_id,
        operation_log_path=operation_log_path,
        dashboard=dashboard,
        page=page_name,
        filter_state=filter_state,
    )
    with measure_stage(
        operation_log_path,
        job_id=job_id,
        operation="validate_visual_type_detection",
        dashboard=dashboard,
        page=page_name,
        filter_state=filter_state,
    ):
        try:
            result = VisualTypeDetectionResult.model_validate(
                _parse_json_response(response, "visual type detector")
            )
        except ValidationError as exc:
            raise ValueError(
                f"Visual type detector response has an invalid schema: {exc}"
            ) from exc
        actual_ids = [item.visual_id for item in result.visuals]
        counts = Counter(actual_ids)
        expected_id_set = set(expected_ids)
        duplicated_ids = {
            visual_id for visual_id, count in counts.items() if count > 1
        }

        def valid_bounds(item: VisualTypeDetection) -> bool:
            bounds = item.bounds
            return (
                bounds.x is not None
                and bounds.y is not None
                and bounds.width is not None
                and bounds.height is not None
                and bounds.width > 0
                and bounds.height > 0
                and bounds.x >= 0
                and bounds.y >= 0
                and bounds.x + bounds.width <= 1000
                and bounds.y + bounds.height <= 1000
            )

        detections: list[dict[str, Any]] = []
        discarded_ids: list[str] = []
        candidate_bounds_missing: list[str] = []
        for item in result.visuals:
            visual_id = item.visual_id
            if visual_id in duplicated_ids:
                discarded_ids.append(visual_id)
                continue
            bounds_are_valid = valid_bounds(item)
            if visual_id in expected_id_set:
                detection = item.model_dump(mode="json")
                if not bounds_are_valid:
                    detection["bounds"] = {
                        "x": None,
                        "y": None,
                        "width": None,
                        "height": None,
                    }
                    candidate_bounds_missing.append(visual_id)
                detections.append(detection)
                continue
            if (
                re.fullmatch(r"SCREENSHOT_\d{3}", visual_id)
                and bounds_are_valid
            ):
                detections.append(item.model_dump(mode="json"))
            else:
                discarded_ids.append(visual_id)

        missing_ids = expected_id_set - set(actual_ids)
        if missing_ids or duplicated_ids or discarded_ids or candidate_bounds_missing:
            logger.warning(
                "Using partial visual type detector results for page %s; "
                "missing IDs=%s, duplicate IDs=%s, discarded IDs=%s, "
                "candidate bounds unavailable=%s",
                page_name,
                sorted(missing_ids),
                sorted(duplicated_ids),
                discarded_ids,
                candidate_bounds_missing,
            )
    return detections


class TruncatedAgentResponseError(ValueError):
    pass


def _model_request_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _model_request_semaphores.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(MAX_PARALLEL_MODEL_REQUESTS)
        _model_request_semaphores[loop] = semaphore
    return semaphore


def _finish_reason_name(event: Any) -> str:
    reason = getattr(event, "finish_reason", None)
    name = getattr(reason, "name", None)
    if isinstance(name, str):
        return name.upper()
    value = getattr(reason, "value", reason)
    return str(value or "").rsplit(".", 1)[-1].upper()


def _text_from_event(event: Any) -> str | None:
    if _finish_reason_name(event) == "MAX_TOKENS":
        raise TruncatedAgentResponseError(
            "ADK agent response was truncated after reaching MAX_TOKENS"
        )
    if not event.is_final_response() or event.content is None:
        return None
    return "".join(part.text or "" for part in event.content.parts)


async def _run_agent_once(
    agent: Any,
    content: types.Content,
    *,
    job_id: str = "standalone",
    operation_log_path: Path | None = None,
    dashboard: str | None = None,
    page: str | None = None,
    filter_state: str | None = None,
) -> str:
    app_name = "powerbi_dashboard_comparator"
    user_id = "dashboard_comparator"
    session_id = str(uuid4())
    session_service = InMemorySessionService()
    with measure_stage(
        operation_log_path,
        job_id=job_id,
        operation="create_gemini_session",
        dashboard=dashboard,
        page=page,
        filter_state=filter_state,
    ):
        await session_service.create_session(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
        )
    runner = Runner(
        agent=agent,
        app_name=app_name,
        session_service=session_service,
    )
    final_text = None
    with measure_stage(
        operation_log_path,
        job_id=job_id,
        operation="gemini_model_request_and_response",
        dashboard=dashboard,
        page=page,
        filter_state=filter_state,
    ):
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=content,
        ):
            text = _text_from_event(event)
            if text is not None:
                final_text = text
    if not final_text:
        raise RuntimeError(f"ADK agent {agent.name!r} returned no final response")
    return final_text


def _is_retryable_model_error(error: BaseException) -> bool:
    pending = [error]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        if isinstance(
            current,
            (
                aiohttp.ClientPayloadError,
                aiohttp.ServerDisconnectedError,
                aiohttp.ClientConnectionError,
                TransportError,
                asyncio.TimeoutError,
                ConnectionError,
            ),
        ):
            return True
        if (
            isinstance(current, aiohttp.ClientResponseError)
            and current.status in MODEL_RETRYABLE_HTTP_STATUSES
        ):
            return True
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False


async def _run_agent(
    agent: Any,
    content: types.Content,
    *,
    job_id: str = "standalone",
    operation_log_path: Path | None = None,
    dashboard: str | None = None,
    page: str | None = None,
    filter_state: str | None = None,
) -> str:
    semaphore = _model_request_semaphore()
    for attempt in range(1, MODEL_REQUEST_ATTEMPTS + 1):
        acquired = False
        try:
            timing_context = (
                {
                    "job_id": job_id,
                    "operation_log_path": operation_log_path,
                    "dashboard": dashboard,
                    "page": page,
                    "filter_state": filter_state,
                }
                if operation_log_path is not None
                else {}
            )
            with measure_stage(
                operation_log_path,
                job_id=job_id,
                operation="wait_for_gemini_request_slot",
                dashboard=dashboard,
                page=page,
                filter_state=filter_state,
            ):
                await semaphore.acquire()
            acquired = True
            return await asyncio.wait_for(
                _run_agent_once(agent, content, **timing_context),
                timeout=MODEL_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            if not _is_retryable_model_error(exc):
                raise
            if attempt == MODEL_REQUEST_ATTEMPTS:
                reason = (
                    "request timeout"
                    if isinstance(exc, asyncio.TimeoutError)
                    else "transient network error"
                )
                raise RuntimeError(
                    f"Vertex AI request failed after {attempt} attempts due to "
                    f"{reason}: {exc}"
                ) from exc
            delay = 2 ** (attempt - 1)
            logger.warning(
                "Transient Vertex AI transport failure for agent %s "
                "(attempt %s/%s); retrying in %s second(s): %s",
                getattr(agent, "name", type(agent).__name__),
                attempt,
                MODEL_REQUEST_ATTEMPTS,
                delay,
                exc,
            )
            with measure_stage(
                operation_log_path,
                job_id=job_id,
                operation="gemini_retry_backoff",
                dashboard=dashboard,
                page=page,
                filter_state=filter_state,
            ):
                await asyncio.sleep(delay)
        finally:
            if acquired:
                semaphore.release()
    raise RuntimeError("Vertex AI request retry loop exited unexpectedly")


def _parse_json_response(text: str, stage: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[0].startswith("```") and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{stage} agent returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{stage} agent response must be a JSON object")
    return value


def _raw_cell(value: Any) -> Any:
    if isinstance(value, dict) and "raw" in value:
        return value["raw"]
    return value


def _unreadable_count(value: Any) -> int:
    if isinstance(value, dict):
        count = int(value.get("status") in {"unreadable", "not_visible"})
        return count + sum(_unreadable_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(_unreadable_count(item) for item in value)
    return 0


def _comparison_element(element: dict[str, Any]) -> dict[str, Any]:
    element_type = str(element.get("element_type") or "other").casefold()
    title = element.get("title") or ""
    content = element.get("content") or {}
    if element_type == "metadata":
        metadata_role = str(content.get("role") or "").casefold()
        standard_name = (
            "metadata_value"
            if metadata_role == "refresh_timestamp"
            else "report_title"
            if metadata_role in {"report_title", "dashboard_name"}
            else "metadata_label"
        )
    else:
        standard_name = standard_element_name(element_type, text=title)
    columns = list(content.get("columns") or [])
    rows: list[list[Any]] = []
    categories = [_raw_cell(value) for value in content.get("categories", [])]
    series = content.get("series") or []

    if columns:
        for row in content.get("rows", []):
            if isinstance(row, dict):
                rows.append([_raw_cell(row.get(column)) for column in columns])
    elif element_type in {"chart", "line chart", "bar chart", "column chart"}:
        series_values = [
            [_raw_cell(value) for value in item.get("values", [])]
            for item in series
        ]
        row_count = max(
            [len(categories), *(len(values) for values in series_values)],
            default=0,
        )
        rows = [
            [
                categories[index] if index < len(categories) else None,
                *[
                    values[index] if index < len(values) else None
                    for values in series_values
                ],
            ]
            for index in range(row_count)
        ]

    value = _raw_cell(content.get("value"))
    if element_type in {"text", "button", "navigation", "metadata"}:
        value = _raw_cell(content.get("text")) if content.get("text") is not None else value

    comparison_type = "table" if element_type == "matrix" else element_type
    return {
        **element,
        "type": comparison_type,
        "title": title,
        "standard_element_name": standard_name,
        "element_text": element.get("element_text") or title,
        "value": value,
        "categories": columns or categories,
        "values": [_raw_cell(item) for item in content.get("values", [])],
        "rows": rows,
    }


def _is_excluded_chrome_or_logo(item: dict[str, Any]) -> bool:
    if item.get("is_application_chrome") is True:
        return True
    element_type = str(item.get("element_type") or "").casefold().replace("-", "_")
    category = str(
        item.get("category") or item.get("standard_element_name") or ""
    ).casefold().replace("-", "_")
    is_brand_logo = (
        category == "brand_logo"
        or item.get("standard_element_name") == "brand_logo"
        or element_type == "brand_logo"
    )
    if item.get("is_logo") is True and not is_brand_logo:
        return True
    if element_type in {
        "logo",
        "brand_image",
        "decorative_image",
        "icon_only",
    } and not is_brand_logo:
        return True
    if category in {
        "application_chrome",
        "platform_chrome",
        "host_interface",
        "host_toolbar",
        "viewer_toolbar",
        "toolbar_command",
        "toolbar_button",
        "viewer_control",
        "application_navigation",
        "page_navigation",
        "navigation_label",
        "navigator",
        "page_navigator",
        "navigation_button",
        "application_sidebar",
        "application_toolbar",
        "browser_chrome",
        "decorative_image",
        "icon_only",
        "platform_navigation",
        "host_navigation",
        "workspace_navigation",
        "workspace_sidebar",
        "filter_pane_toggle",
        "logo",
        "brand_mark",
        "platform_logo",
        "app_logo",
    }:
        return True
    return not is_brand_logo and (
        "logo" in category or "chrome" in category or "toolbar" in category
    )


def _is_excluded_navigation(item: dict[str, Any]) -> bool:
    element_type = str(item.get("element_type") or "").casefold().replace("-", "_")
    category = str(
        item.get("category") or item.get("standard_element_name") or ""
    ).casefold().replace("-", "_")
    label = str(item.get("text") or item.get("title") or item.get("value") or "")
    label = " ".join(label.casefold().split())
    box = item.get("bounding_box") or item.get("normalized_bounding_box") or {}
    x = box.get("x") if isinstance(box, dict) else None
    left_page_navigation = (
        (category in {"page_title", "page_name"} or element_type in {"page_title", "page_name"})
        and isinstance(x, (int, float))
        and (x < 0.1 if 0 <= x <= 1 else x < 180)
    )
    return (
        element_type in {"navigation", "navigator", "page_navigator"}
        or category
        in {
            "page_navigation",
            "navigation_label",
            "navigator",
            "page_navigator",
            "navigation_button",
            "application_navigation",
        }
        or label in {"home", "pages"}
        or left_page_navigation
    )


def _prepare_title_items(
    extracted: dict[str, Any], elements: list[dict[str, Any]], metadata: dict[str, Any]
) -> list[dict[str, Any]]:
    title_items = extracted.get("title_items", [])
    if not isinstance(title_items, list):
        raise ValueError("Screenshot extractor response must contain a title_items array")
    normalized: list[dict[str, Any]] = []
    for item in title_items:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise ValueError("Screenshot extractor returned an invalid title item")
        if _is_excluded_chrome_or_logo(item) or _is_excluded_navigation(item):
            continue
        text = item["text"]
        if not text.strip():
            continue
        normalized.append(
            {
                **item,
                "text": text,
                "category": str(item.get("category") or "other_label"),
                "standard_element_name": standard_element_name(
                    item.get("standard_element_name") or item.get("category"),
                    item.get("element_type"),
                    text,
                ),
                "element_text": item.get("element_text") or text,
                "element_type": standard_element_name(
                    item.get("element_type") or item.get("category"),
                    text=text,
                ),
                "associated_visual": item.get("associated_visual"),
            }
        )

    source_title_item_count = len(normalized)
    matched_title_items: set[int] = set()

    def title_box(item: dict[str, Any]) -> tuple[float, float, float, float] | None:
        box = item.get("bounding_box")
        if not isinstance(box, dict):
            box = item.get("normalized_bounding_box")
        if not isinstance(box, dict):
            return None
        values = tuple(box.get(key) for key in ("x", "y", "width", "height"))
        if not all(isinstance(value, (int, float)) for value in values):
            return None
        x, y, width, height = (float(value) for value in values)
        if width <= 0 or height <= 0:
            return None
        return x, y, width, height

    def same_role(
        item: dict[str, Any], category: str, association: str | None
    ) -> bool:
        item_category = standard_element_name(
            item.get("standard_element_name") or item.get("category"),
            item.get("element_type"),
            item.get("text"),
        )
        candidate_category = standard_element_name(category, text=text)
        if item_category != candidate_category:
            return False
        item_association = " ".join(
            str(item.get("associated_visual") or "").casefold().split()
        )
        candidate_association = " ".join(str(association or "").casefold().split())
        if (
            item_association
            and candidate_association
            and item_association != candidate_association
        ):
            return False
        return True

    def same_bounds(
        item_a: dict[str, Any], item_b: dict[str, Any]
    ) -> bool | None:
        box_a = title_box(item_a)
        box_b = title_box(item_b)
        if box_a is None or box_b is None:
            return None

        xa, ya, wa, ha = box_a
        xb, yb, wb, hb = box_b
        intersection_width = max(0.0, min(xa + wa, xb + wb) - max(xa, xb))
        intersection_height = max(0.0, min(ya + ha, yb + hb) - max(ya, yb))
        intersection = intersection_width * intersection_height
        smaller_area = min(wa * ha, wb * hb)
        if smaller_area > 0 and intersection / smaller_area >= 0.5:
            return True

        normalized = max(abs(xa), abs(ya), abs(xb), abs(yb), wa, ha, wb, hb) <= 1.5
        tolerance = 0.015 if normalized else max(4.0, min(ha, hb) * 0.5)
        center_distance_squared = (
            xa + wa / 2 - xb - wb / 2
        ) ** 2 + (ya + ha / 2 - yb - hb / 2) ** 2
        return center_distance_squared <= tolerance**2

    def same_occurrence(
        item: dict[str, Any],
        text: str,
        category: str,
        association: str | None,
        bounding_box: dict[str, Any] | None,
    ) -> bool:
        if " ".join(item["text"].casefold().split()) != " ".join(
            text.casefold().split()
        ):
            return False
        if not same_role(item, category, association):
            return False
        bounds_match = same_bounds(item, {"bounding_box": bounding_box})
        return bounds_match is not False

    def add_title(
        text: Any,
        category: str,
        association: str | None,
        bounding_box: dict[str, Any] | None = None,
        reading_order: Any = None,
    ) -> None:
        if (
            not isinstance(text, str)
            or not text.strip()
            or _is_excluded_navigation({"category": category})
        ):
            return
        candidate_record = {
            "text": text,
            "category": category,
            "associated_visual": association,
            "bounding_box": bounding_box,
        }
        conflicting_source_indices = []
        for index in range(source_title_item_count):
            source_item = normalized[index]
            if not same_role(source_item, category, association):
                continue
            if (
                " ".join(source_item["text"].casefold().split())
                != " ".join(text.casefold().split())
                and same_bounds(source_item, candidate_record) is True
            ):
                source_item["reconciliation_status"] = "conflicting_readings"
                conflicting_source_indices.append(index)
                continue
            if index not in matched_title_items and same_occurrence(
                source_item, text, category, association, bounding_box
            ):
                matched_title_items.add(index)
                sources = normalized[index].get("evidence_sources") or ["title_items"]
                normalized[index]["evidence_sources"] = list(
                    dict.fromkeys([*sources, "elements"])
                )
                return
        normalized.append(
            {
                "text": text,
                "category": category,
                "associated_visual": association,
                "bounding_box": bounding_box or {},
                "reading_order": reading_order,
                "evidence_sources": ["elements"],
            }
        )
        if conflicting_source_indices:
            normalized[-1]["reconciliation_status"] = "conflicting_readings"

    def extracted_text(value: Any) -> str | None:
        if isinstance(value, dict):
            if value.get("status") not in (None, "read"):
                return None
            value = value.get("raw")
        return value if isinstance(value, str) and value.strip() else None

    for element in elements:
        title = str(element.get("title") or "")
        element_type = str(element.get("element_type") or "visual").casefold()
        if element_type == "metadata":
            box = element.get("bounding_box") or {}
            if not all(
                isinstance(box.get(key), (int, float))
                for key in ("x", "y", "width", "height")
            ):
                continue
        category = "brand_logo" if element.get(
            "standard_element_name"
        ) == "brand_logo" else {
            "chart": "chart_title",
            "table": "table_title",
            "matrix": "matrix_title",
            "kpi": "kpi_title",
            "filter": "filter_title",
            "slicer": "slicer_title",
            "icon": "icon",
            "navigation": "navigation_label",
            "button": "button_label",
            "metadata": "metadata_label",
        }.get(element_type, "visual_title")
        association = element.get("associated_visual") or (title or None)
        element_order = element.get("reading_order")
        add_title(
            title,
            category,
            association,
            element.get("bounding_box"),
            element_order,
        )
        content = element.get("content") or {}
        if element_type in {"table", "matrix"}:
            for header in content.get("columns") or []:
                add_title(
                    header,
                    "column_header",
                    title or None,
                    reading_order=element_order,
                )
        if element_type in {"filter", "slicer"}:
            filter_name = extracted_text(content.get("filter_name")) or title
            filter_association = (
                element.get("associated_visual") or filter_name or None
            )
            if " ".join(filter_name.casefold().split()) != " ".join(
                title.casefold().split()
            ):
                add_title(
                    filter_name,
                    "slicer_title" if element_type == "slicer" else "filter_title",
                    filter_association,
                    reading_order=element_order,
                )
            for selected_value in content.get("selected_values") or []:
                add_title(
                    extracted_text(selected_value),
                    "slicer_value",
                    filter_association,
                    reading_order=element_order,
                )
        for series in content.get("series") or []:
            if isinstance(series, dict):
                add_title(
                    series.get("name"),
                    "legend_item",
                    title or None,
                    reading_order=element_order,
                )
        for legend in content.get("legend") or []:
            add_title(
                legend,
                "legend_item",
                title or None,
                reading_order=element_order,
            )

        def add_axis_titles(value: Any, axis_name: str = "") -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    key_name = str(key).casefold()
                    if "title" in key_name and isinstance(nested, str):
                        category = (
                            "secondary_axis_title"
                            if "secondary" in key_name or "secondary" in axis_name
                            else "y_axis_title"
                            if key_name.startswith("y") or axis_name.startswith("y")
                            else "x_axis_title"
                        )
                        add_title(
                            nested,
                            category,
                            title or None,
                            reading_order=element_order,
                        )
                    else:
                        add_axis_titles(nested, key_name)
            elif isinstance(value, list):
                for nested in value:
                    add_axis_titles(nested, axis_name)

        add_axis_titles(content.get("axes"))

    normalized = sort_reading_order(normalized)

    width = metadata.get("image_width")
    height = metadata.get("image_height")
    for index, item in enumerate(normalized, 1):
        item["title_id"] = f"TITLE_{index:03d}"
        item["standard_element_name"] = standard_element_name(
            item.get("standard_element_name") or item.get("category"),
            item.get("element_type"),
            item.get("element_text") or item.get("text"),
        )
        item["element_type"] = item["standard_element_name"]
        item["element_text"] = item.get("element_text") or item["text"]
        item["reading_order"] = index
        box = item.get("bounding_box")
        if not isinstance(box, dict):
            box = {}
        item["bounding_box"] = {
            key: box.get(key) for key in ("x", "y", "width", "height")
        }
        normalized_box = item.get("normalized_bounding_box")
        if not isinstance(normalized_box, dict):
            normalized_box = {}
        if width and height and all(
            isinstance(box.get(key), (int, float))
            for key in ("x", "y", "width", "height")
        ):
            normalized_box = {
                "x": box["x"] / width,
                "y": box["y"] / height,
                "width": box["width"] / width,
                "height": box["height"] / height,
            }
        item["normalized_bounding_box"] = {
            key: normalized_box.get(key)
            for key in ("x", "y", "width", "height")
        }
    return normalized


def _prepare_screenshot_extraction(
    extracted: dict[str, Any], screenshots: list[bytes], page_name: str
) -> dict[str, Any]:
    metadata = extracted.setdefault("dashboard_metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
        extracted["dashboard_metadata"] = metadata
    metadata["page_name"] = metadata.get("page_name") or page_name
    metadata["screenshot_id"] = hashlib.sha256(b"\0".join(screenshots)).hexdigest()
    if screenshots and len(screenshots[0]) >= 24 and screenshots[0][:8] == b"\x89PNG\r\n\x1a\n":
        metadata["image_width"], metadata["image_height"] = struct.unpack(
            ">II", screenshots[0][16:24]
        )

    elements = extracted.get("elements")
    if not isinstance(elements, list):
        raise ValueError("Screenshot extractor response must contain an elements array")
    if any(not isinstance(element, dict) for element in elements):
        raise ValueError("Screenshot extractor returned an invalid visual element")
    elements = [
        element
        for element in elements
        if not _is_excluded_chrome_or_logo(element)
        and not _is_excluded_navigation(element)
    ]
    prefixes = {
        "kpi": "KPI",
        "chart": "CHART",
        "table": "TABLE",
        "matrix": "TABLE",
        "filter": "FILTER",
        "slicer": "FILTER",
        "text": "TEXT",
        "button": "BUTTON",
        "navigation": "BUTTON",
        "metadata": "METADATA",
    }
    counters: Counter[str] = Counter()
    normalized_elements: list[dict[str, Any]] = []
    ordered_elements = list(elements)
    ordered_elements = sort_reading_order(ordered_elements)
    for reading_order, element in enumerate(ordered_elements, 1):
        element_type = str(element.get("element_type") or "other").casefold()
        prefix = prefixes.get(element_type, "TEXT")
        counters[prefix] += 1
        element["element_id"] = f"{prefix}_{counters[prefix]:03d}"
        element["reading_order"] = reading_order
        normalized_elements.append(_comparison_element(element))

    for key, title in (
        ("report_title", "Report title"),
        ("refresh_timestamp", "Last refresh"),
    ):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            counters["METADATA"] += 1
            normalized_elements.append(
                _comparison_element(
                    {
                        "element_id": f"METADATA_{counters['METADATA']:03d}",
                        "element_type": "metadata",
                        "title": title,
                        "reading_order": len(normalized_elements) + 1,
                        "bounding_box": {
                            "x": None,
                            "y": None,
                            "width": None,
                            "height": None,
                        },
                        "content": {
                            "text": {"raw": value, "status": "read", "reason": None},
                            "role": key,
                        },
                        "extraction_status": "complete",
                        "uncertainties": [],
                    }
                )
            )

    extracted["elements"] = normalized_elements
    extracted["title_items"] = _prepare_title_items(
        extracted, normalized_elements, metadata
    )
    extracted["page"] = metadata["page_name"]

    summary = extracted.setdefault("extraction_summary", {})
    if not isinstance(summary, dict):
        summary = {}
        extracted["extraction_summary"] = summary
    reported_visible_count = summary.get("visible_element_count")
    reported_extracted_count = summary.get("extracted_element_count")
    reported_count = max(
        (
            count
            for count in (reported_visible_count, reported_extracted_count)
            if isinstance(count, int) and not isinstance(count, bool)
        ),
        default=0,
    )
    statuses = [
        str(item.get("extraction_status") or "complete")
        for item in normalized_elements
    ]
    status_counts = Counter(statuses)
    summary["visible_element_count"] = len(normalized_elements)
    summary["extracted_element_count"] = len(normalized_elements)
    summary["complete_element_count"] = status_counts["complete"]
    summary["partial_element_count"] = status_counts["partial"]
    summary["failed_element_count"] = status_counts["failed"]
    summary["unreadable_item_count"] = _unreadable_count(elements)
    summary["skipped_element_count"] = (
        status_counts["skipped_exported"]
        + status_counts["skipped_export_option"]
    )
    summary["requires_additional_screenshots"] = bool(
        summary.get("requires_additional_screenshots")
        or status_counts["partial"]
        or status_counts["failed"]
    )
    summary["title_item_count"] = len(extracted["title_items"])
    summary["title_conflict_count"] = sum(
        item.get("reconciliation_status") == "conflicting_readings"
        for item in extracted["title_items"]
    )
    if isinstance(reported_visible_count, int) and not isinstance(
        reported_visible_count, bool
    ):
        summary["reported_visible_element_count"] = reported_visible_count
    if isinstance(reported_extracted_count, int) and not isinstance(
        reported_extracted_count, bool
    ):
        summary["reported_extracted_element_count"] = reported_extracted_count
    if reported_count > len(normalized_elements):
        summary["extraction_gap_count"] = reported_count - len(normalized_elements)
        summary["requires_additional_screenshots"] = True
        notes = summary.setdefault("notes", [])
        if not isinstance(notes, list):
            raise ValueError("Screenshot extraction summary notes must be an array")
        notes.append(
            "The extractor-reported visible/extracted item count exceeds the "
            "returned element count; this page's extraction may be incomplete."
        )
    if summary["title_conflict_count"]:
        notes = summary.setdefault("notes", [])
        if not isinstance(notes, list):
            raise ValueError("Screenshot extraction summary notes must be an array")
        notes.append(
            "Conflicting visible text was retained from multiple extraction "
            "sources and requires review."
        )
    return extracted


def _planning_inventory(inventory: dict[str, Any]) -> dict[str, Any]:
    filters = inventory.get("filters", [])
    if not isinstance(filters, list):
        raise ValueError("DOM inventory filters must be an array")
    return {
        "pages": inventory.get("pages", []),
        "filters": filters,
    }


AGGREGATE_FILTER_VALUES = frozenset({"all", "(all)", "select all"})


def _is_aggregate_value(value: str) -> bool:
    return value.strip().casefold() in AGGREGATE_FILTER_VALUES


async def plan_from_dom(
    dom_inventory: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    analysis = {
        "pages": dom_inventory.get("pages", []),
        "filters": dom_inventory.get("filters", []),
    }
    planning_inventory = _planning_inventory(dom_inventory)
    plan = _deterministic_filter_plan(planning_inventory)
    _validate_plan(plan, planning_inventory)
    return analysis, plan


def _deterministic_filter_plan(inventory: dict[str, Any]) -> dict[str, Any]:
    plan: list[dict[str, Any]] = []
    for item in inventory.get("filters", []):
        if not isinstance(item, dict):
            raise ValueError("DOM inventory contains an invalid filter entry")
        name = item.get("name")
        values = item.get("values")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("DOM inventory contains a filter without a name")
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            raise ValueError(f"DOM inventory values for {name!r} are invalid")
        control_type = item.get("control_type")
        if control_type in {"radio", "radio_dropdown"} and any(
            value.strip().casefold() in {"clear selection", "clear selections"}
            for value in values
        ):
            raise ValueError(
                f"Radio filter {name!r} must not expose a Clear Selection option"
            )
        # Aggregate options are excluded for multi-select/dropdown controls,
        # where toggling them changes the whole selection. Radio options are
        # single-choice, so every exposed value is tested individually.
        selected_values: list[str] = []
        for value in values:
            if (
                control_type not in {"radio", "radio_dropdown"}
                and _is_aggregate_value(value)
            ) or value in selected_values:
                continue
            selected_values.append(value)
        if selected_values:
            plan.append({"name": name, "values": selected_values})
    return {"filters": plan}


def _normalize_plan(
    plan: dict[str, Any],
    inventory: dict[str, Any],
) -> dict[str, Any]:
    planned_filters = plan.get("filters")
    if not isinstance(planned_filters, list):
        raise ValueError("Filter planner response must contain a filters array")
    known = {
        item["name"]: set(item["values"])
        for item in inventory.get("filters", [])
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and isinstance(item.get("values"), list)
    }
    normalized: dict[str, list[str]] = {}
    for item in planned_filters:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("Filter plan contains an invalid filter entry")
        name = item["name"]
        values = item.get("values")
        if name not in known:
            raise ValueError(f"Filter plan contains unknown filter {name!r}")
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError(f"Filter plan for {name!r} has invalid values")
        planned_values = normalized.setdefault(name, [])
        for value in values:
            if value not in known[name]:
                raise ValueError(f"Filter plan invented a value for {name!r}")
            if value not in planned_values:
                planned_values.append(value)
    return {
        "filters": [
            {"name": name, "values": values}
            for name, values in normalized.items()
            if values
        ]
    }


def _align_shared_plan(
    shared_plan: dict[str, Any],
    inventory: dict[str, Any],
) -> dict[str, Any]:
    planned_filters = shared_plan.get("filters")
    if not isinstance(planned_filters, list):
        raise ValueError("Shared filter plan must contain a filters array")
    canonical_names: dict[str, str] = {}
    for item in planned_filters:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("Shared filter plan contains an invalid filter entry")
        name = item["name"]
        values = item.get("values")
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError(f"Shared filter plan for {name!r} has invalid values")
        canonical_names.setdefault(item["name"].casefold(), item["name"])

    inventory_filters = inventory.get("filters", [])
    if not isinstance(inventory_filters, list):
        raise ValueError("DOM inventory filters must be an array")

    def aligned_filter(item: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("DOM inventory contains an invalid filter entry")
        name = item["name"]
        options = item.get("values")
        if not isinstance(options, list) or not all(
            isinstance(value, str) for value in options
        ):
            raise ValueError(f"DOM inventory values for {name!r} are invalid")
        filter_name = canonical_names.get(name.casefold(), name)
        control_type = item.get("control_type")
        values: list[str] = []
        for value in options:
            if (
                control_type not in {"radio", "radio_dropdown"}
                and _is_aggregate_value(value)
            ):
                continue
            if value not in values:
                values.append(value)
        if not values:
            return None
        return {"name": filter_name, "values": values}

    aligned: list[dict[str, Any]] = []
    used_inventory_indices: set[int] = set()
    for planned_filter in planned_filters:
        planned_name = planned_filter["name"]
        for index, item in enumerate(inventory_filters):
            if (
                index not in used_inventory_indices
                and isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and item["name"].casefold() == planned_name.casefold()
            ):
                aligned_item = aligned_filter(item)
                used_inventory_indices.add(index)
                if aligned_item is not None:
                    aligned_item["name"] = canonical_names[planned_name.casefold()]
                    aligned.append(aligned_item)
                break

    for index, item in enumerate(inventory_filters):
        if index in used_inventory_indices:
            continue
        aligned_item = aligned_filter(item)
        if aligned_item is not None:
            aligned.append(aligned_item)
    return {
        "filters": aligned
    }


def _validate_plan(plan: dict[str, Any], inventory: dict[str, Any]) -> None:
    planned_filters = plan.get("filters")
    if not isinstance(planned_filters, list):
        raise ValueError("Filter planner response must contain a filters array")
    known = {
        (item.get("name"), value)
        for item in inventory.get("filters", [])
        if isinstance(item, dict) and isinstance(item.get("values"), list)
        for value in item["values"]
    }
    for item in planned_filters:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("Filter plan contains an invalid filter entry")
        values = item.get("values")
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValueError(f"Filter plan for {item['name']!r} has invalid values")
        if any((item["name"], value) not in known for value in values):
            raise ValueError(f"Filter plan invented a value for {item['name']!r}")


async def extract_screenshot(
    screenshot_bytes: bytes,
    page_name: str,
    exported_visual_titles: list[str] | None = None,
    *,
    job_id: str = "standalone",
    operation_log_path: Path | None = None,
    dashboard: str | None = None,
    filter_state: str | None = None,
) -> dict[str, Any]:
    return await extract_screenshots(
        [screenshot_bytes],
        page_name,
        exported_visual_titles,
        job_id=job_id,
        operation_log_path=operation_log_path,
        dashboard=dashboard,
        filter_state=filter_state,
    )


async def extract_screenshots(
    screenshots: list[bytes],
    page_name: str,
    exported_visual_titles: list[str] | None = None,
    *,
    job_id: str = "standalone",
    operation_log_path: Path | None = None,
    dashboard: str | None = None,
    filter_state: str | None = None,
) -> dict[str, Any]:
    if not screenshots:
        raise ValueError("At least one screenshot is required for extraction")
    with measure_stage(
        operation_log_path,
        job_id=job_id,
        operation="prepare_gemini_extractor",
        dashboard=dashboard,
        page=page_name,
        filter_state=filter_state,
    ):
        model_name = _model_name()
        max_output_tokens = (
            DEFAULT_SCREENSHOT_MAX_OUTPUT_TOKENS
            if model_name
            in {DEFAULT_MODEL_ID, "gemini-2.5-flash", "gemini-3.7-flash"}
            else None
        )
        agent = LlmAgent(
            name="screenshot_extractor",
            model=model_name,
            instruction=VISION_INSTRUCTIONS,
            output_schema=ScreenshotExtraction,
            generate_content_config=_generation_config(
                model_name,
                max_output_tokens=max_output_tokens,
            ),
        )
    prompt = (
        f"Extract the dashboard state for page: {page_name}. "
        "The images are successive scrolled views of the same report state. "
        "Combine all visible data, preserving repeated values for distinct "
        "categories and avoiding duplicate rows for the same category."
    )
    exported_titles = list(
        dict.fromkeys(
            title.strip()
            for title in exported_visual_titles or []
            if isinstance(title, str) and title.strip()
        )
    )
    if exported_titles:
        prompt += (
            "\nThe following visuals have a confirmed Export data option and "
            "have successfully parsed summarized export data. Their visual "
            "bodies may be covered by a solid white mask, while their "
            "titles remain visible: "
            + json.dumps(exported_titles, ensure_ascii=False)
            + ". For these visuals, do not read or extract screenshot data "
            "values, rows, categories, series, or cells. Still include each "
            "visible visual in elements with its exact title, type, reading "
            "order, and bounding box; leave its data content empty, set "
            "extraction_status to skipped_export_option unless parsed export "
            "data is supplied for that title; use skipped_exported only for "
            "those parsed exports. Explain the selected status in uncertainties. "
            "Continue extracting all visible titles and labels in title_items."
        )

    async def request_extraction(instruction: str) -> str:
        with measure_stage(
            operation_log_path,
            job_id=job_id,
            operation="prepare_gemini_request",
            dashboard=dashboard,
            page=page_name,
            filter_state=filter_state,
        ):
            content = types.Content(
                role="user",
                parts=[
                    types.Part(text=instruction),
                    *[
                        types.Part.from_bytes(data=screenshot, mime_type="image/png")
                        for screenshot in screenshots
                    ],
                ],
            )
        timing_context = (
            {
                "job_id": job_id,
                "operation_log_path": operation_log_path,
                "dashboard": dashboard,
                "page": page_name,
                "filter_state": filter_state,
            }
            if operation_log_path is not None
            else {}
        )
        return await _run_agent(agent, content, **timing_context)

    try:
        response = await request_extraction(prompt)
        with measure_stage(
            operation_log_path,
            job_id=job_id,
            operation="parse_gemini_response",
            dashboard=dashboard,
            page=page_name,
            filter_state=filter_state,
        ):
            extracted = _parse_json_response(response, "screenshot extractor")
    except ValueError as exc:
        invalid_json = "screenshot extractor agent returned invalid JSON" in str(exc)
        if not invalid_json and not isinstance(exc, TruncatedAgentResponseError):
            raise
        logger.warning(
            "Screenshot extraction was incomplete for page %s (%s); retrying "
            "once with a concise, complete-JSON instruction",
            page_name,
            "truncated response" if isinstance(exc, TruncatedAgentResponseError) else "invalid JSON",
        )
        response = await request_extraction(
            f"{prompt} The previous response was truncated or invalid. Retry "
            "and return one complete, valid JSON object matching the required "
            "schema. Keep notes concise, but preserve every visible report "
            "element, table row, column, and value. Do not include markdown."
        )
        with measure_stage(
            operation_log_path,
            job_id=job_id,
            operation="parse_gemini_retry_response",
            dashboard=dashboard,
            page=page_name,
            filter_state=filter_state,
        ):
            extracted = _parse_json_response(
                response, "screenshot extractor retry"
            )
    try:
        with measure_stage(
            operation_log_path,
            job_id=job_id,
            operation="validate_gemini_extraction_schema",
            dashboard=dashboard,
            page=page_name,
            filter_state=filter_state,
        ):
            extracted = ScreenshotExtraction.model_validate(extracted).model_dump(
                mode="json"
            )
    except ValidationError as exc:
        raise ValueError(
            f"Screenshot extractor response does not match schema version 1.0: {exc}"
        ) from exc
    with measure_stage(
        operation_log_path,
        job_id=job_id,
        operation="normalize_gemini_extraction",
        dashboard=dashboard,
        page=page_name,
        filter_state=filter_state,
    ):
        return _prepare_screenshot_extraction(extracted, screenshots, page_name)
