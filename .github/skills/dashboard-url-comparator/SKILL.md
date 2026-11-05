---
name: dashboard-url-comparator
description: "Extract and compare dashboard titles, labels, visual content, and anomalies across Power BI or other dashboard screenshots and URLs. Use for reading-order extraction, title matching, rename/add/remove/move detection, baseline-test comparison, and validation reports."
argument-hint: "[baseline Power BI URL] [test Power BI URL]"
user-invocable: true
---

# Dashboard Screenshot Extraction

Use this extraction-only mode when the request is to read a dashboard screenshot
or prepare evidence for a later comparison. Do not compare dashboards in this
mode. Return exactly one JSON object conforming to the schema below, with no
Markdown, explanation, or code fence.

## Universal title and label extraction

- Discover dashboard, report, page, navigation, visual, chart, table, matrix,
  KPI/card, section, filter/slicer, axis, legend, row/column header, subtitle,
  tooltip, button, menu, annotation, and metadata labels dynamically.
- Emit every title/label occurrence in a separate `title_items` record. Keep
  exact source text and identify its category and associated visual/section.
  Use null for an association that is not visually supportable.
- Assign a dashboard-wide 1-based reading order. Scan top-to-bottom and
  left-to-right within a row; use visible bounds to order nested, overlapping,
  or irregular visuals. Record source-image bounding boxes when measurable
  and normalized coordinates when image dimensions are known.
- Do not collapse duplicate text from distinct locations or roles. Do not
  extract hidden/cache text or process unrelated cache content.
- Keep title evidence distinct from visual data evidence. Preserve exact
  source text even when the comparison later identifies a rename.
- Exclude browser/platform chrome, host toolbar commands, workspace/app
  navigation, collapsed viewer-pane handles, page navigation/navigator
  controls, and decorative or icon-only images from both `elements` and
  `title_items`. Exclude Home and Pages navigation labels.
  Mark recognized host interface items with `is_application_chrome: true` and
  host logos with `is_logo: true`; they are removed before comparison. Keep
  dashboard-authored branding as a `brand_logo` title item with exact visible
  text (for example, "SpartanNash"), even when it is also marked
  `is_logo: true`.
- Keep dashboard/report titles, refresh metadata, report-authored visuals,
  meaningful report images, actual report slicers/filters, and meaningful
  labeled icons such as Help and Information. Do not mistake Power BI's host
  navigation, toolbar, or Filters-pane toggle for report content.

## Lossless cross-source extraction

- Treat screenshot `title_items` and structured `elements` as complementary,
  independent evidence sources. Extract all visible title/label occurrences
  into `title_items` even when the same text also appears as a visual title,
  table header, slicer field, legend, axis, or annotation in `elements`.
- Never choose one source as authoritative and discard the other. Structured
  visual content can recover labels missed by title extraction; title evidence
  can recover text that is absent from or incomplete in the visual structure.
- Reconcile records as occurrences, not as a set of strings. Merge only
  one-to-one records supported as the same visible occurrence by exact
  whitespace-normalized text, compatible role/category, compatible visual
  association, and overlapping/near-identical bounds when both bounds exist.
  Text equality by itself is never enough to remove a record.
- Preserve repeated labels when they have different locations, roles,
  associations, or source occurrence counts. If coordinates are missing,
  match source occurrences one-to-one and keep every unmatched occurrence;
  do not use a global `(text, category)` set that collapses repeated values
  such as `All`, repeated legend entries, or duplicate headers.
- When sources give conflicting text for a likely shared occurrence, retain
  both exact readings and mark the evidence uncertain for review. Do not
  silently prefer the structured value, title text, or a fuzzy-corrected value.
- Keep provenance when available (for example, `title_items`, `elements`, or
  both). Verify that every eligible source occurrence is represented in the
  merged output, and report counts/uncertainties for unresolved conflicts.
  The normalizer may add `evidence_sources`, `title_item_count`, and
  `title_conflict_count`; preserve these diagnostics in machine-readable output.

## Evidence and completeness

- Extract only information visibly supported by the supplied screenshot(s).
  Never invent, calculate, assume, or estimate hidden or unreadable values.
- Use two deliberate passes: first read visible text occurrences from the
  screenshot; then extract structured visual content (titles, headers,
  legends, slicer labels/values, and visible values). Reconcile them using the
  lossless occurrence rules above rather than suppressing one pass because the
  other returned some text.
- Inspect each screenshot top-to-bottom and left-to-right. Include dashboard
  and page names, refresh metadata, every visible KPI, chart, table/matrix,
  report filter/slicer, meaningful labeled icon, legend, annotation, and text.
  Exclude page-navigation labels and navigator controls. Apply the
  host-chrome/logo exclusions above.
- Assign stable, unique IDs in reading order using the appropriate prefix:
  `KPI_001`, `CHART_001`, `TABLE_001`, `FILTER_001`, `TEXT_001`,
  `BUTTON_001`, or `METADATA_001`. Return one dashboard-wide element sequence
  in top-to-bottom, then left-to-right order; do not group elements by type.
  Preserve displayed titles and spelling, and extract each visible text item
  separately with its text and role.
- For each table/matrix, include every visible header and row in original order,
  including totals/subtotals. Use one cell object per column. Do not drop empty,
  repeated, or unreadable columns or rows. A visible but unreadable cell is
  `{"raw":null,"status":"unreadable","reason":"..."}`; use `not_visible` only
  when it is outside the supplied image/not displayed. Preserve raw formatting,
  zeroes, dates, percentages, currency, signs, and precision.
- Extract only rows visible in the screenshots. Merge overlapping scrolled
  views in order without duplicating rows. Mark a clipped/partial visual as
  `partial`; never claim hidden rows or the underlying dataset were extracted.
- For charts, retain title, identifiable type, axes/ticks, categories, every
  series and visible data label/value, legend, units, distinguishable colors or
  markers, and annotations. Align series values to categories. If a precise
  value is not printed or legible, use null/unavailable; do not derive it from
  bar height or line position.
- For KPIs, preserve the displayed value, unit/formatting, secondary values,
  trend, comparison period, and explanatory text.
- For filters/slicers, record the filter name, selected values, visibly listed
  options, date range, and selection state. Do not assume `All`.
- Record the Power BI report title and refresh date/time only if visibly
  supported. If unavailable, use null and explain the limitation in notes.
  Store the Power BI report name from the host header as
  `dashboard_metadata.report_title` (the sole host-chrome exception); do not
  emit it as a dashboard title or title-item occurrence.
  Store the dashboard-authored canvas title (for example, `HR NOW - Summary`)
  as a `dashboard_title` in `title_items`. These are distinct fields and must
  never be substituted for or aligned with each other. Keep the selected page
  name in `page_name`.
  Include dimensions and bounds only when known; never fabricate geometry.

## Required JSON shape

Use this exact top-level structure. Adapt `content` to the element type while
keeping the outer element fields and extraction-summary fields:

```json
{
  "schema_version": "1.0",
  "dashboard_metadata": {
    "report_title": null,
    "page_name": null,
    "refresh_timestamp": null,
    "screenshot_id": "",
    "image_width": null,
    "image_height": null
  },
  "elements": [
    {
      "element_id": "TABLE_001",
      "element_type": "table",
      "title": "",
      "exportOptionAvailable": null,
      "export_status": null,
      "export_data_format": null,
      "is_application_chrome": false,
      "is_logo": false,
      "reading_order": 1,
      "bounding_box": {
        "x": null,
        "y": null,
        "width": null,
        "height": null
      },
      "content": {
        "columns": [],
        "rows": []
      },
      "extraction_status": "complete",
      "uncertainties": []
    }
  ],
  "title_items": [
    {
      "title_id": "TITLE_001",
      "text": "exact displayed title or label",
      "category": "chart_title",
      "associated_visual": "containing visual title or null",
      "is_application_chrome": false,
      "is_logo": false,
      "reading_order": 1,
      "bounding_box": {
        "x": null,
        "y": null,
        "width": null,
        "height": null
      },
      "normalized_bounding_box": {
        "x": null,
        "y": null,
        "width": null,
        "height": null
      }
    }
  ],
  "export_options": [
    {
      "title": "visual title",
      "element_type": "chart",
      "exportOptionAvailable": null,
      "export_status": "not_detected",
      "export_data_format": null,
      "export_file": null,
      "screenshot": null,
      "export_dialog_screenshot": null
    }
  ],
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
```

`title_items` is an ordered array with one record per visible title-like
occurrence. Use concise categories such as `brand_logo`, `dashboard_title`,
`report_title`, `page_title`, `chart_title`, `table_title`, `matrix_title`,
`kpi_title`, `section_title`, `filter_title`, `axis_title`, `legend_title`,
`column_header`, `row_header`, `subtitle`, `tooltip_title`, `button_label`,
`icon`, `annotation_title`, and `metadata_label`. Preserve raw coordinates in
`bounding_box`; use normalized 0-1 coordinates only when image dimensions and
bounds are known. `reading_order` is evidence, not identity.

Table `content` uses ordered `columns` and `rows`, with each row an object
mapping every column name to `{"raw":"displayed value","status":"read"}` or
the appropriate null/status/reason object. Chart `content` includes
`chart_type`, `categories`, `series`, `axes`, `legend`, and `annotations`.
KPI `content` includes `value`, `unit`, `secondary_values`, and `trend`.
Filter `content` includes `filter_name`, `selected_values`, `visible_options`,
and `selection_state`. Text, button, and metadata `content` preserve `text` and
its `role`. Every unavailable field should be null/empty as appropriate, not a
guessed value.

Use element extraction statuses consistently: `complete` means all information
expected to be visible for that element was read; `partial` means some visible
content is clipped, too small, or unavailable; `failed` means the element was
identified but its content could not be reliably extracted; `skipped_exported`
means screenshot values were intentionally skipped because parsed summarized
export data will be compared instead. Summary counts must agree with the
returned elements and unreadable cells. Use
`requires_additional_screenshots: true` when visible content is clipped or
additional scrolled views are needed. Before responding, verify that all
visible rows, columns, cells, chart series, metadata, and controls are present;
JSON is valid; no visible zero became null; no value or label was silently
corrected; and no incomplete element is labeled complete.
Always emit an extraction status explicitly. If the evidence does not support
claiming that all expected visible content was read, use `partial` rather than
defaulting to `complete`. Preserve extractor-reported visible/extracted counts
when they disagree with returned element records, flag the count gap in notes,
and set `requires_additional_screenshots: true`; do not silently replace the
discrepancy with matching counts.
Treat a model `MAX_TOKENS` finish reason as a truncated response even when it
contains parseable text. Retry once with a concise schema-focused instruction,
then split a multi-screenshot sequence into smaller groups if truncation or
invalid JSON persists. Surface an unrecoverable single-image extraction error
explicitly instead of reporting an empty or complete extraction.

# Dashboard Comparison and Anomaly Report

Use the comparison mode below when the user asks to compare a baseline with a
test dashboard or produce an anomaly report. Extract evidence independently
first; do not return the extraction-only JSON unless specifically requested.

Use this skill to compare a baseline dashboard with a test dashboard and
produce a traceable report. The example skill name `dashboard_url_comparator`
refers to this skill; its slash command is `/dashboard-url-comparator`.

## Inputs

- Baseline report URL or image (`image_a`)
- Test report URL or image (`image_b`)
- Optional verified-element agreement threshold (default: `0.95`)
- Optional output format: `json`, `markdown`, `xlsx`, or `pdf`

Ask only for missing information that changes the comparison, such as which
report is the baseline, which pages or filters to include, or whether a
threshold should be applied. Preserve the user's A/B ordering.

## Procedure

The application coordinates these stages with a Google ADK `SequentialAgent`:
capture/extraction, Python validation, Python alignment and status assignment,
and report generation. Screenshot extraction may use Gemini through ADK;
alignment and status assignment are deterministic Python operations.

1. **Confirm scope and access**
   - Treat Dashboard A as the baseline and Dashboard B as the test unless the
     user specifies otherwise.
   - Exclude report pages named `Home` or `View` (case-insensitively) from
     filter planning, capture, and comparison.
   - Use only authorized Power BI access. Never request or include passwords,
     cookies, storage-state contents, or access tokens in the report.
   - Record the report names, page coverage, filter coverage, and capture time
     when available.

2. **Capture both sides independently**
   - Open and capture each URL separately, even if both URLs are identical.
     Identical-URL comparisons are useful for testing capture and model
     consistency; never copy one side's screenshot or extracted data to the
     other.
   - Dashboard A and B capture concurrently. Within each dashboard, up to two
     pages run concurrently, with a global cap of four page workers and an
     independent Playwright context per page. Filter-state capture uses up to
     three workers per dashboard and is bounded to six active captures across
     the pair; each filter case also gets a separate context. Preserve
     deterministic page/filter result order and never share a mutable report
     page between cases.
   - Keep the viewport, browser zoom, page, and filter state aligned.
   - Use a consistent high-resolution viewport (the application default is
     1920x1080). Move the pointer outside the report and wait for hover tooltips
     or visual headers to disappear before saving each screenshot.
   - Prefer accessible exported table data when available. Use scrolled-view
     extraction only when a visual's menu confirms `Export data` is absent, or
     when no visuals are detected. If `Export data` is offered but the later
     export attempt fails, record the failure and do not switch to scrolling.
     Scrolled views are captured only when the dashboard exposes "Scroll right"
     or "Scroll down" buttons; pages without them are captured as a single
     screenshot and are not mouse-wheel scrolled.
   - For each detected visual, hover it and open its More options menu. If
     `Export data` is available, choose it, leave the data-format selection
     untouched, and click `Export` directly. Do not inspect or change whether
     `Current layout` or `Summarized data` is selected. Save screenshots of the
     open menu and the export dialog before clicking Export; keep the clean
     dashboard screenshot for visual extraction.
   - Record `exportOptionAvailable` per detected visual: `true` when the menu
     exposes `Export data`, `false` when the visual is detected but that action
     is absent, and `null` when the visual control could not be detected.
     Record the attempt outcome, format, downloaded file name, and evidence
     screenshot reference. Do not treat an unavailable or failed export as an
     empty dataset.
   - If the report refreshes or changes while capture is in progress, report
     that timing as a possible cause rather than attributing it to extraction.

3. **Extract visible evidence**
   - Identify KPI cards, charts, tables, text, and visible layout changes.
   - Record the dashboard/report name and the displayed data refresh date/time
     for each side as required metadata. Prefer an accessible report title or
     explicit refresh label/value; otherwise use clearly visible screenshot
     evidence. Mark either field unavailable if it cannot be verified.
   - Preserve titles, labels, row order, and rendered numeric formatting.
   - Never omit a table column or trend series. Keep all column headers in their
     displayed order, even when a cell is unreadable; represent only that
     unreadable cell as null. When horizontal scrolling exposes more columns,
     merge all views into one full ordered schema and align every row to it.
   - Compare downloaded export files cell-by-cell. When a visual has
     successfully parsed export data, skip screenshot value comparison for
     that visual and mark it as already exported; keep extracting its visible
     titles/labels. Include export availability and outcomes in JSON and the
     `Export Options` worksheet, and exported cell comparisons in
     `comparison.json`.
   - Treat successive screenshots as overlapping views: merge unique rows and
     do not duplicate rows merely because they appear in multiple screenshots.
   - Keep slicer/filter selections separate from visual content.
   - Do not infer clipped, hidden, or unreadable values. Mark insufficient
     evidence as unverified instead of treating it as a confirmed difference.

4. **Align before comparing**
   - Compare each side's `title_items` independently within the same page and
     filter state. Preserve exact source strings, categories, associations,
     source coordinates, normalized coordinates, and reading order.
   - Pair pages and filter states only when their normalized page/state keys
     agree. Do not cross-pair different pages to force a match; an unpaired
     page/state is `UNVERIFIED` unless capture evidence proves it is absent.
   - Independently order each side's title items by original dashboard-wide
     `reading_order`, then align the sequences with insertion/deletion gaps.
     Use text similarity together with category/type, group, associated
     visual, and normalized coordinates when available; these are alignment
     signals, not hard matching keys, and text alone must not establish
     identity. Blank or generic element types are unknown, not evidence of a
     type match. Preserve each source reading-order index separately from the
     aligned comparison position.
   - Never treat `report_title` and `dashboard_title` as compatible
     classifications: the report identity in the host header and the
     dashboard-authored title inside the canvas are distinct evidence. Compare
     report titles as identity metadata and compare dashboard titles as
     dashboard content. Keep `filter_title`/`slicer_title` compatible when
     text and context support the same item, preserving original categories
     and recording classification variance.
   - Preserve raw extraction evidence for `brand_logo` and `button_label`,
     but exclude both categories from title comparisons, canonical comparison
     records, and report artifacts.
   - Keep unmatched items one-sided and report them as added or
     removed/unverified as supported by the evidence. Do not let an insertion
     or deletion shift later matching elements.
   - Do not classify a one-sided extracted item as `MISSING_A` or `MISSING_B`
     solely because it was not extracted on the other side. Use `UNVERIFIED`
     unless complete capture/extraction evidence supports absence.
   - Identify `unchanged`, `renamed`, `added`, `removed`, `moved`, and
     `reclassified` titles. Exact text matched across different coordinates
     is `moved`, not renamed; a changed sequence number alone is not a move.
     Keep a rename distinct from a move when both text and coordinates change.
   - Report both reading-order positions, both categories, associated visual
     names, and source/normalized coordinates for each title comparison.
   - Sequence-align extracted data visuals in their original dashboard-wide
     `reading_order`, allowing insertion/deletion gaps. Compare type, title,
     association, spatial context, and values after alignment. Preserve table
     row/header order within each visual.
   - Mark weak or competing pairings `AMBIGUOUS_ALIGNMENT` and `UNVERIFIED`
     for review rather than asserting a match or difference. Keep one-sided
     elements separate so extra/missing visuals do not shift later matches.
     Report layout status separately from content and element-type status;
     compare geometry only when both sides have usable bounds.
   - For tables, preserve and compare the captured header and cell order.
   - Normalize only equivalent representations, such as `$0` and numeric `0`,
     separators, currency symbols, and unambiguous percentage or K/M/B formats.
     Preserve the original displayed value in report details.
   - Compare report titles and refresh date/time in a separate metadata
     section. A report-title mismatch is an identity warning that must be surfaced;
     refresh differences must include both observed timestamps and be flagged
     as freshness information, not hidden or silently counted as a data-value
     mismatch.
   - Do not infer title correspondence from position alone. If the available
     text/type/group/association/spatial evidence is weak or ambiguous, leave
     the items unmatched or mark the pairing `AMBIGUOUS_ALIGNMENT` and
     unverified rather than forcing a match.

5. **Apply comparison logic by data type**
   | Comparison type | Recommended logic |
   | --- | --- |
   | Report identity | Compare the Power BI report title for A and B; show both titles and flag a mismatch. |
   | Refresh date/time | Compare the displayed refresh values and report both timestamps as freshness metadata. |
   | KPI values | Compare numeric values using a configured tolerance; if none is configured, use exact normalized comparison and say so. |
   | Percentages | Normalize percent strings and numeric fractions to a common percentage representation before comparing. |
   | Tables | Compare headers and cells in captured order; report missing and extra rows/columns. |
   | Charts | Match title, type, categories, series, and visible values. |
   | Filters | Compare selected values and date ranges for corresponding filter names. Only test exact values discovered from the Power BI control; never synthesize filter values. |
   | Text | Normalize whitespace; compare case-insensitively when case is not meaningful. |
   | Layout | Compare captured bounding boxes and dimensions within configured tolerances. If geometry was not captured, mark layout comparison unavailable. |
   | Titles and labels | Compare exact source text, category, visual association, and spatial context; report rename/add/remove/move separately. |
   | Missing visuals | Match using title, type, association, and spatial context; report unmatched visuals when correspondence is ambiguous. |
   | Extraction quality | Check missing cells, absent columns, inconsistent row widths, truncated content, and uncertain values before deciding a mismatch. |

   Do not claim a check was performed unless the required evidence was actually
   captured. The current application supports normalized extracted values,
   table headers/rows, accessible filter selections, and refresh-label
   differences.

   Filter cases are chosen deterministically from each filter's own popup. If
   `All` is already selected by default, leave it unchanged because the initial
   state is already captured. Otherwise, do not click an aggregate option;
   click every individual value, one at a time, including values already
   selected in the initial state. Other baseline selections are cleared and
   the final selected value is verified before capture. There is no value cap.
   The model does not generate filter names or values. The application uses
   real Playwright clicks and verifies that a planned selection took effect
   before capture. It does not
   provide a configurable KPI tolerance. It records title bounding boxes when
   the extractor can determine them; mark geometry unavailable when bounds are
   missing and use exact normalized numeric comparison when no KPI tolerance
   is configured.

6. **Classify findings**
   - `MATCH`: verified values or structure agree after safe normalization.
   - `DIFFERENT`: a verified value, visual, row, or filter selection differs.
   - `MISSING_A` / `MISSING_B`: use only when reliable capture and extraction
     evidence establishes the item is absent from that dashboard.
   - `INFO`: refresh metadata differs without proving a dashboard data mismatch.
     Dashboard-name mismatches must also be called out clearly as identity
     warnings, even when the underlying data can still be compared.
     A title-only move is positional information, not a rename.
   - `UNVERIFIED`: data is empty, clipped, unreadable, missing due to incomplete
     capture, or otherwise insufficient to establish a match or difference.
   - Give each confirmed anomaly a concise description, affected page/filter,
     evidence from both sides, technical severity, and qualitative confidence.
   - Severity: High for a missing key visual or KPI/data mismatch; Medium for
     other verified data or structural differences; Low for cosmetic or
     formatting-only differences. Do not claim business impact without context.
   - Confidence is qualitative, not a calibrated probability: High for
     accessible exported data or clearly legible corroborated evidence; Medium
     for clear screenshot OCR; Low for ambiguous or partial evidence.

6. **Calculate an honest agreement score**
   - When comparison counts are available, calculate the verified-element
     agreement rate as `matches / (matches + differences)`.
   - Exclude `INFO` and `UNVERIFIED` from the calculation. If there are no
     verified matches or differences, report the score as `N/A`.
   - Label this as a verified-element agreement rate, not a pixel, embedding,
     or perceptual similarity score. Compare it with the requested threshold
     (default `0.95`) only when the evidence is sufficient.
   - Never invent CLIP/other vision-embedding scores, SSIM values, heatmaps,
     confidence probabilities, or visual-diff overlays. The current comparator
     reports structured extracted-content matches and differences; it does not
     implement those image-analysis features.

8. **Report results**
   - Summarize the overall result, agreement rate and threshold (if applicable),
     both report titles, both dashboard-authored titles, both refresh
     date/time values, capture coverage,
     matched elements, confirmed anomalies, informational metadata, unverified
     items, severity, confidence, and limitations.
   - Include the exact observed values and enough page/filter context to
     reproduce each finding.
   - Use the application's generated JSON, Excel, and PDF artifacts when
     available. For Markdown, format findings as a concise table.
   - The application’s interactive results view uses the versioned
     `comparison_records` array as its source of truth for filtering, KPIs,
     details, and CSV exports. Keep its unique IDs and source evidence stable;
     calculate displayed counts and agreement rates from those records.
     Display AI confidence as unavailable unless the system implements a
     calibrated, evidence-backed measure.
   - State clearly when the evidence is incomplete; do not call an incomplete
     comparison a pass.

## Consistency Checklist

- Both sides were captured independently, including when the URLs are the same.
- Page, filter, and viewport states were aligned.
- A visible zero was not mistaken for a missing value.
- Table headers and cells were compared in their captured order.
- Title and visual comparisons sequence-aligned original reading order with
  insertion/deletion gaps and preserved both original order values separately
  from aligned positions. Text, type/category, association, and coordinates
  supported alignment; text-only global rematching was not used.
- Added, removed, renamed, moved, and reclassified titles were distinguished.
- A title is classified as moved when its normalized bounding-box centers differ
  by more than 0.08 coordinate units; the threshold is a heuristic, not a
  calibrated layout tolerance. Only normalized 0-1 bounds are used for this
  heuristic; raw pixel coordinates are preserved as evidence but are not used
  to classify movement. Reading-order changes alone do not count as moves.
- Every visible table column and trend series was retained; uncertain cells did
  not cause their entire columns to be dropped.
- Report titles and refresh date/time were captured or explicitly marked
  unavailable; dashboard-authored canvas titles were extracted separately.
- Refresh metadata differences were reported with the observed timestamps;
  report-title mismatches were surfaced as identity warnings.
- Extraction quality was checked for missing columns/cells and truncation.
- Title text was reconciled from both screenshot title items and structured
  visual elements without global text-only deduplication or silent drops.
- Every anomaly is supported by evidence; unsupported image-analysis metrics
  are explicitly marked unavailable.
