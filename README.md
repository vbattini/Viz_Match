# Power BI Dashboard Comparator

A Python tool for comparing two Power BI report URLs.

The standard comparison workflow captures visible report titles and labels,
table/matrix headers, refresh metadata, visual candidates and layout from the
report DOM, and visual names, types, and rendering state from screenshots. It
also captures accessible filter names, options, control types, and selections.
It does not extract table cells, chart data, or KPI values.
Screenshot-based visual value extraction is disabled by default to avoid large
model responses. Call `capture_dashboard_pair` with `extract_visual_data=True`
only when screenshot content extraction is needed. Direct callers can enable
the one-screenshot visual detector with `detect_visual_types=True`; the CLI and
standard workflow enable it automatically.

1. Playwright opens each report, waits for explicit busy indicators and
   report content to stabilize, then records accessible controls. Class-only
   loading markers count only while an infinite animation is active; they are
   given up to 30 seconds to clear before the screenshot and accessible
   metadata are captured as `UNVERIFIED`. Explicit busy indicators and
   incomplete visual images use the 300-second readiness timeout. A visible
   Power BI query-resource error is treated as terminal and reported promptly;
   waiting longer cannot resolve a query that exceeds the model or capacity's
   available resources.
2. Playwright discovers accessible report pages and filter values, then
   generates a plan containing every one-option-per-filter combination.
3. Playwright captures the initial state when it is distinct from planned
   filter states, plus each planned combination. If a planned combination
   matches the initial selections for every filter, it covers the initial
   state and avoids duplicate screenshot, export, and extraction work. The
   combinations use nested-product order: the first filter stays on its
   current option while later filters iterate all their options, then the
   first filter advances. Combinations are captured sequentially per
   dashboard so execution follows that order. In the standard workflow, one
   representative screenshot per dashboard/page is sent to the configured
   Vertex AI model to identify visual names, types, and rendering state only;
   dashboard data values are not extracted. When visual data extraction is
   enabled, the application inspects
   rendered visual containers, opens More options, explicitly selects and
   verifies Summarized data when available, and verifies the downloaded CSV/XLSX
   before using it.
   DOM metadata supplies visual candidates and canvas-relative bounds; the
   screenshot detector enriches those candidates and reuses the result across
   that page's filter states. Dashboard A and B run concurrently. Pages run in separate Playwright
   contexts, with up to `min(page_count, 5)` page workers per dashboard and a
   five-worker global page cap.
   Dropdown slicers are dismissed and their collapsed state verified after
   reading or applying a selection, so an open filter menu cannot cover report
   visuals during export or screenshot capture.
   Gemini requests remain independent per extraction and run with a global
   limit of five concurrent requests. Each request has a five-minute timeout
   and up to four retries with exponential backoff, helping transient Vertex
   AI transport failures recover without occupying capture workers indefinitely.
   As page results finish, matching page/filter states
   are compared in bounded workers; their partial results are consolidated and
   validated before the final JSON and Excel reports are written.
   Per-state timings, extraction request duration, cache hits, and per-visual
   export durations are appended to each dashboard's
   `capture_performance.jsonl` as states complete, so completed measurements
   remain available if a later state fails.
   The job-root `operation_timings.jsonl` records start/finish events and
   durations for browser setup, navigation/readiness, page and filter planning,
   capture-slot waits, rendering, exports, screenshots, Gemini request/retry,
   response parsing, per-visual exports, extracted JSON, and JSON/XLSX/DOCX
   report generation. Failed or interrupted operations retain their start
   event; failures record the exception type but not report URLs or exception
   text. Concurrent stage durations can overlap and must not be summed as job
   wall time.
4. When visual extraction is enabled, exported rows are normalized into the
   JSON comparison data. Scrolled views
   are captured when the visual menu confirms that `Export data` is absent,
   export is unverified, or no visuals were detected; a failed export is
   recorded and the visual remains available for screenshot extraction. Accessible
   "Scroll right" / "Scroll down" buttons are used when available.
5. A deterministic comparator matches equivalent page/filter states and writes
   JSON and Excel reports.

By default, the comparison captures the visible/current filter selections and
does not open Power BI slicer dropdowns or enumerate filter options. This is
the visual-only mode and captures the initial state for each page. Use
`--include-filter-cases` to opt into dropdown discovery and per-value filter
testing; this mode can open report slicers and capture combinations from
available options. It does not manipulate arbitrary buttons, custom date
pickers, or report visuals. It will fail explicitly when an agent returns
invalid output or a planned control/value cannot be operated or verified.

## Requirements

- Python 3.10 or newer
- Google Cloud Application Default Credentials (ADC) with Vertex AI access
- Access to both Power BI reports
- Playwright browser binaries

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m playwright install chromium
```

Authenticate with Google Cloud ADC (or provide ADC through your organization's
workload identity setup):

```powershell
gcloud auth application-default login
```

The default Vertex AI configuration is project `gc-proj-aiml-dev-01fd`, US
multi-region `us`, model `gemini-3.8-flash`. Override these with `--project-id`,
`--location`, and `--model`, or the corresponding `GOOGLE_CLOUD_PROJECT`,
`GOOGLE_CLOUD_LOCATION`, and `GEMINI_MODEL` environment variables.
Gemini 3.8 Flash's [supported regions](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-8-flash)
include the `us` multi-region; the regional `us-east4` endpoint is not listed.

Concurrency and request deadlines can be tuned with
`PBI_MAX_PARALLEL_MODEL_REQUESTS` (default `10`),
`PBI_MODEL_REQUEST_TIMEOUT_SECONDS` (default `300`),
`PBI_MODEL_REQUEST_ATTEMPTS` (default `5`),
`PBI_MAX_PARALLEL_PAGE_CAPTURES` (default `10`),
`PBI_MAX_PARALLEL_STATE_CAPTURES` (default `10`),
`PBI_MAX_PARALLEL_COMPARISONS` (default `4`). Values must be positive.
Screenshot extraction uses the 65,536-token output limit for Gemini 2.5 Flash,
3.7 Flash, and 3.8 Flash; other configured models retain their own default
output limit. Gemini 3.7 and 3.8 Flash use model-default sampling settings
because `temperature` is not configurable for these models.
The concurrency defaults are a modest throughput increase; lower them if
Power BI or Gemini starts returning throttling errors, timeouts, or browser
resource errors. Compare end-to-end times across several representative runs
before increasing them further.

For reports requiring authentication, create a Playwright storage state file
with an authenticated browser session and pass it to the CLI:

```powershell
python -m playwright codegen --save-storage=auth.json https://app.powerbi.com/
```

Sign in in the opened browser, then close it to save the session. Protect
`auth.json`; it contains session credentials and should not be committed.

## Run

Start the local web UI:

```powershell
uvicorn powerbi_dashboard_comparator.api:app --host 127.0.0.1 --port 8000
```

Then open <http://127.0.0.1:8000>. Enter both Power BI report URLs and start
the comparison. The UI uses the configured Vertex AI project/model and the
local `auth.json` storage state; it does not accept credentials in the form.
Only one comparison runs at a time. Job status is polled in the browser, and
the UI stopwatch counts up while the comparison runs and displays the final
execution time as `MM:SS:mmm` (minutes, seconds, milliseconds) when it
completes. There is no 60-second time limit. Completed Word (DOCX), JSON, and Excel
reports can be downloaded from the page.

The JSON report includes versioned `comparison_records` with source values,
positions, statuses, difference details, and available alignment/extraction
evidence. The verified agreement rate excludes `INFO` and `UNVERIFIED`
records. AI confidence is not calculated by the comparator.
Report titles shown in the Power BI host header are tracked as report identity
metadata, separately from dashboard-authored titles inside the report canvas.
[`examples/comparison_sample.json`](./examples/comparison_sample.json) is a
synthetic, pipeline-generated example showing an insertion without shifting
subsequent title matches; it is not a live dashboard capture.

Screenshot extraction treats a model `MAX_TOKENS` finish reason as truncation,
retries once with a concise schema-focused prompt, and splits multi-screenshot
sequences when the response remains truncated or invalid. An omitted element
extraction status defaults to `partial`. If the extractor-reported item count
exceeds returned records, the gap is retained in extraction diagnostics and
additional screenshots are flagged rather than silently treating the page as
complete.

The runtime workflow is coordinated by an ADK `SequentialAgent`:
capture/extraction, Python validation, deterministic Python alignment/status
assignment, then report generation. ADK/Gemini is used for screenshot
extraction; comparison statuses remain controlled by Python. See
[workflow.py](./src/powerbi_dashboard_comparator/workflow.py).

The API accepts HTTPS URLs on `app.powerbi.com` only. Keep the server bound to
localhost unless you add authentication and HTTPS for remote access.

## CLI

```powershell
powerbi-compare `
  --url-a "https://app.powerbi.com/groups/.../reports/..." `
  --url-b "https://app.powerbi.com/groups/.../reports/..." `
  --output-dir ".\output" `
  --storage-state ".\auth.json"
```

Use `--headed` to watch browser activity or `--settle-seconds` to adjust the
wait after selecting a filter. Filters are exercised exhaustively (see Notes),
so there is no per-filter value cap.

The output directory contains one folder per dashboard (`dom.json`,
`filter_plan.json`, screenshots, and `extracted.json`) plus
`comparison.json`, `Dashboard_Comparison.xlsx`, and `Dashboard_Comparison.docx`. The
final Word report uses `Report Validation Template.docx`: its existing sections,
labels, styles, and colors are retained, while embedded template screenshots
are replaced by the current default-state dashboard captures. It also appends
one Base/Compare screenshot pair for every dashboard page. Dashboard A is labeled
Snowflake, and Dashboard B is labeled GCP. Detailed filter/slicer comparisons remain in the Excel validation log, and
comparison details and data-export validation remain in `comparison.json`.
Complete cell-level results are retained in `comparison.json`. No separate
`comparison.docx` is generated. Tester name defaults to "Vijay Battini".
The comparison DOCX tester dropdown also includes "Battini Vijay" while
retaining "Vijay Battini" as its default selection.
Status cells in the Excel validation worksheet and Word report show only
`PASS` or `FAIL`:
`MATCH` maps to `PASS`, and every other result (including `DIFFERENT`, `INFO`,
and `UNVERIFIED`) maps to `FAIL`. The DOCX sign-off uses a `Result Status
Dropdown (Pass/Fail)` control with `Pass` and `Fail` options, without checkbox
symbols. It selects `Pass` only when at least one check exists and every check
passes.
Internal comparison statuses in `comparison.json` remain unchanged.
Dashboard A is the comparison baseline. Screenshot-extracted elements are
independently ordered by their
dashboard-wide `reading_order`, then aligned as sequences with insertion and
deletion gaps. The `Order A` and `Order B` columns preserve each source
position; `Aligned position` is a separate comparison index. Alignment uses
text similarity together with element type, group/association, and normalized
coordinates when available; text alone does not establish identity. One-sided
elements and unmatched page/filter states are `UNVERIFIED` unless capture
evidence proves absence; the comparator never treats a missing extraction as a
confirmed missing dashboard item. Pages are paired only when their normalized
page and filter-state keys agree. Insertions and deletions do not shift the
alignment of later matching elements. Title/label comparisons use the same
sequence alignment and retain title, type, association, and coordinate
differences as separate evidence. Type, group, visual association, and nearby
coordinates are alignment evidence rather than hard matching keys; blank or
generic types are treated as unknown. Report pages named `Home` or `View` are
excluded from filter planning, capture, and comparison, case-insensitively.
Report titles are compared as report identity metadata, separate from
dashboard-authored canvas titles. Filter/slicer title aliases are treated as
equivalent classifications while their original categories remain visible in
the report. Trailing punctuation on brand-logo text is ignored for matching,
while both raw strings are preserved. Low-evidence pairs are marked
`AMBIGUOUS_ALIGNMENT` and `UNVERIFIED` for review instead of being reported as
matches or confirmed differences. Visual rows report layout status separately
from content and type differences. Exported table rows
continue to use their shared dimension keys when available, with positional
row comparison as the fallback. Comparison records, including scalar-only
visual values (for example, a `Time Period` filter displaying `All`), remain
available in `comparison.json`.
Successfully parsed exports skip screenshot data-value extraction and
comparison for that visual; full exported visual cell comparisons remain in
`comparison.json`. Screenshot title/label evidence remains in `comparison.json`, grouped as
dashboard titles, report metadata, dashboard metadata, slicers, and visual
labels.
Report-title mismatches are
identity warnings; refresh-timestamp mismatches are informational freshness
differences; and unavailable metadata is unverified. Metadata rows are shown
once rather than duplicated as ordinary title values.
Slicer names and selected values have distinct `slicer_title` and
`slicer_value` types; legends and headers retain their own types and associated
visual. Accessible selected values are merged into title evidence when
screenshot extraction omits them. Title matching uses compatible type families
and associated-visual identity before text. Repeated values such as `All` are
disambiguated by their
slicer association; without sufficient context they remain unverified.
Unpaired extraction evidence is marked `unmatched / unverified`, not confirmed
missing.
The validation worksheet focuses on dashboard visuals, filter selections,
results, and screenshot evidence; export diagnostics and element metadata
remain in `comparison.json`.
Screenshot evidence is used for visual-value comparison when neither side has
parsed export data. Export-option availability (`Yes`, `No`, or `Unknown`) and
each attempt's outcome are recorded in `comparison.json`, which also retains
parsed summarized exports compared cell-by-cell, with columns aligned
by normalized name and
rows aligned by shared dimension values rather than position when a stable key
can be inferred. Missing or extra keyed rows remain verified differences;
tables without a usable row key retain positional comparison. Differences in
comparable exported values remain `DIFFERENT`. Row keys tolerate export type
suffixes such as `(Text Type)` and equivalent date formats. Extra hierarchy
dimensions present in only one export (for example fiscal year/quarter fields)
do not make every aligned row differ; additional measure columns remain
explicitly compared.
For a paired page/filter state with sufficient comparable evidence, equal
normalized values are classified as `MATCH`. Missing or ambiguous evidence
remains `UNVERIFIED`; matching values do not hide separate type or layout
differences.
Every extracted title/label retains its fixed `standard_element_name` and
exact visible text. Measurable elements are ordered top-to-bottom and
left-to-right in each dashboard; ambiguous one-sided labels remain
`UNVERIFIED`.
Raw extraction tags dashboard branding, report/dashboard, visual, filter, icon,
annotation, and metadata labels with fixed standard element names. The
comparison report excludes `brand_logo` and `button_label` records. Navigation
labels are also excluded. Transient `Successful
export` / `Your data was exported.` notifications are excluded from dashboard
comparisons.
Each state's `extracted.json` records `exportOptionAvailable` and export status
for detected visuals. Downloaded Excel/CSV files are saved directly in the
state's `_exports` folder, with a JSON sidecar for each file containing
every worksheet, structured rows, and any applied-filter metadata. Evidence
screenshots of the More options menu, export dialog, successful export
notification, and scrolled views are stored separately in `_exports\evidence`.
Each export record also lists the Playwright locator-click steps that were
completed.
`Dashboard_Comparison.xlsx` contains only the `Visual_Validation` worksheet.
Dashboard title/label evidence, exported cell comparisons, and detailed
comparison records remain in `comparison.json`; no `Screenshot Data`,
`Summary`, or `Visual_Comparison` worksheets are generated.
`Visual_Validation` starts with a side-by-side `Visual Differences Summary`,
split into `Dashboard / Page Validation`, `Filter Validation Log`, and
`Visual & Functional Validation Log` sections, followed by Pass/Fail/Unable to
Verify counts. A difference in refresh timestamps is informational and passes
when both timestamps are displayed. Filter-selection mismatches fail the
filter check. KPI numeric values and table records are excluded from data
validation; the visual check compares whether KPI content renders and whether
table headers match. Position and size are assessed separately using the
configured layout tolerances. Checks that require screenshot-only judgment
(such as table styling, the Filters pane, and dashboard navigation) default to
Unable to Verify until reviewed. The detailed visual inventory follows, and
Base and Compare screenshots are embedded side by side once per page.
`Dashboard_Comparison.docx` is the final template-based Word report and
retains the template sections and appends a page-by-page screenshot section.
Current screenshots replace the template screenshots; detailed slicer and
export data remain in JSON. Its Visual & Functional Validation Log groups
entries by page, lists unique visual types from the comparison results,
includes filter selections when available, and uses Pass/Fail dropdowns.
Screenshot-based visual comparison records remain available in
`comparison.json` and the DOCX report. Known filter/slicer type aliases are
displayed consistently as `slicer_title` or `slicer_value`; source categories
remain in `comparison.json`. Other element-type differences remain visible and
are flagged rather than being forced into a match.
Exported cell values and export-only visual rows are retained in
`comparison.json`. Each exported-data comparison's source export filenames,
JSON sidecars, and attempt statuses are preserved in `comparison.json`, not
repeated as worksheet columns. Exports
from either dashboard are included; cell comparisons require corresponding
tabular data from both sides. A
download containing only Power BI `Applied filters` metadata is marked
`downloaded_no_tabular_data`, excluded from cell comparisons, and its parse
explanation is recorded in state JSON; its sidecar preserves the metadata
but does not treat it as data. Screenshot-extracted values are kept separate
from exported values; exported cell details appear only in `comparison.json`.
The JSON retains the detailed comparison counts and its verified-element
agreement rate, which excludes informational and unverified records.
For each visual, the export record includes the active page, filter state,
visual title/identifier/bounds, option availability, export status, and file
path. A download is accepted only when its saved CSV/XLSX file exists and
contains parseable tabular data. Only those successfully parsed exports are
masked for screenshot extraction. Each successfully parsed export is masked
immediately with an opaque white overlay covering its full visual block,
including its title. A two-pixel inset preserves the visual borders and report
layout, and the dashboard screenshot is captured with these exported blocks
masked. Failed exports are not masked, are explicitly recorded, and remain
available for normal screenshot extraction. If applying or cleaning up a mask
fails, the issue is logged and the job continues with unmasked extraction.
Masked extraction screenshots are saved under the state's `_exports\evidence`
folder.

## Notes

- With `--include-filter-cases`, filter handling is independent per filter and
  dashboard. The comparator
  identifies checkbox and radio options before applying control-specific
  behavior. It checks each non-bookmark filter for `Clear selections`; when
  available, it clicks once, records the name in `initial_cleared_slicers` in
  `dom.json`, and skips any further action on that filter, including planning
  and capture. Without a clear action, checkbox filters select all individual
  values, and radio or single-select filters are iterated one option at a time
  with a capture for each option. Dropdown listboxes marked
  `aria-multiselectable="false"` are treated
  as single-choice controls, including when their options use `role="option"`
  and checked state rather than `role="radio"`. Their labels and selected
  state are read from the associated popup. Radio selection verification reads
  native `input.checked`, accessible checked/selected states, or Power BI's
  selected slicer marker; an unreadable state is an explicit inspection error,
  not an empty selection. The execution plan captures the
  Cartesian product of one option from each
  remaining filter, in nested order, with one isolated report state per
  combination. Aggregate `All` / `Select all` entries are excluded from
  multi-select filter cases; exposed radio options are tested as individual
  choices. If no associated popup is found, option discovery checks the nearest
  visible listbox, menu, tree, dialog, or labeled DOM choices. It waits up to 15
  seconds for delayed options; if options remain unreadable, it saves popup
  and screenshot diagnostics before closing the control and fails explicitly,
  unless the visible selection summary confirms an aggregate `All` state. In
  that case it preserves the verified initial state, records that option
  inventory is unavailable, and skips per-value cases for that filter.
  Option locators stay scoped to the filter's associated popup, or to the
  nearest visible popup only when no association exists; page-global options
  are not used because they can belong to another filter. If a planned dropdown
  value is not currently rendered, the comparator waits up to 15 seconds for
  its option or popup search field. For radio dropdowns, it uses the associated
  popup `Search` input first, or a geometrically nearby input when no popup
  association exists; distant host-header search fields are ignored. It can
  also activate a nearby non-choice `Search` affordance to reveal the input.
  For radio dropdowns, it scrolls the option inside that popup's own list
  container, reacquires the exact option after scrolling, and clicks the
  custom `role="option"` row when no native radio or associated label exists.
  If that click does not change the verified state, it retries once with the
  row's Space-key interaction. It verifies that the requested value is the
  sole selection before continuing. It keeps any search query active through
  verification and saves the popup DOM, option/radio
  attributes, row identity, click target, scroll-container state, selection,
  and screenshot if verification still fails. It then closes the popup. A requested
  value must match one individual value in the filter inventory; combined
  labels are rejected before interaction.
  Dropdown option inventories are reacquired from the associated popup before
  filter cases are prepared, and a parent container's combined text is not
  treated as one option. Values come from the accessible DOM and are never
  invented by the model. For single-select slicers, the last listed individual
  option is selected as the default and used as the baseline; each available
  option is still included in the filter-case plan.
- Scrolled-view extraction is used only when a visual's More options menu
  confirms that `Export data` is absent, or no visuals were detected. Finding
  `Export data` suppresses scrolling even if that visual's download fails; the
  visual remains available for normal screenshot extraction. Scrolling itself
  happens only when the dashboard exposes accessible `Scroll right` or
  `Scroll down` buttons. Pages without those buttons are captured as a single
  screenshot and no mouse-wheel scrolling is performed.
- Filter controls and options are activated with Playwright clicks, including
  controls covered by Power BI's visual header overlay. The selected value is
  verified after each click. If a requested option is specifically unavailable
  in the live filter popup, that filter combination is skipped without saving a
  screenshot and reported as `UNVERIFIED`; later combinations continue. Other
  selection and verification failures still stop the run with a diagnostic.
- Bookmark-button selections are applied before other filters in a combination,
  because a bookmark can restore saved slicer values. The comparator then
  re-verifies every requested filter value before capturing the state. When a
  bookmark changes the available slicer options, those options are rediscovered
  under each bookmark before the combinations are planned. During capture, the
  target bookmark is activated and allowed to settle before restoring its
  baseline slicers. The capture baseline reflects the requested bookmark, so
  restoration does not revert a bookmark-only page to its startup selection.
  Dependent values are restored against the correct option list.
- Each page's filter combinations run sequentially in nested-product order,
  each in an isolated browser context. Different pages and dashboard sides can
  still capture concurrently, bounded by the page and global capture limits.
  Each capture uses the page's validated filter inventory and restores its
  baseline before applying the combination.
- Captures use a consistent 1920x1080 viewport. Before screenshots, the mouse is
  moved outside the dashboard and transient hover overlays are allowed to
  disappear, preventing the pointer or chart tooltip from obscuring evidence.
- Each detected visual is hovered independently. When its More options menu
  exposes `Export data`, the capture waits up to two seconds for the menu button
  to appear, activates it through the DOM, then leaves the dialog's default
  option selected before clicking `Export`. After the export pass,
  success notifications are dismissed and successfully exported visual blocks
  are masked white in a separate sequential pass before screenshot extraction.
  This preserves each visual's title while masking the data area below it.
  The export scan checks every matched visual container; it does not truncate
  the scan at a fixed visual count. Containers without a usable More options
  button remain explicitly unverified and use screenshot extraction rather
  than terminating the job. `Export data` is marked absent only after a visible
  menu is confirmed and the action is not present; an unverified menu does not
  trigger scrolling. Export checks are paired
  with extracted visuals by exact title
  first and by overlapping visual bounds when OCR/model title text differs,
  such as an axis label being mistaken for a chart title; ambiguous matches
  still remain unverified and do not get masked. The export dialog is polled
  for up to five seconds, and downloads are allowed up to 60 seconds. A
  completed download is accepted only for CSV/XLSX files with parseable data.
  The export dialog's default data option is left unchanged, and
  `export_data_format` records `default`.
  `exportOptionAvailable` is true when that action is found and false only
  when the visible More options menu confirms that no export action exists.
  Export attempts are retained for screenshot-identified charts,
  tables, matrices, and KPIs, plus any successfully exported visual; slicers,
  navigation/bookmark controls, and decorative images are excluded. The
  `export_option_scan` counts in state JSON show how many non-data candidates
  were excluded. Evidence captures the menu after the Playwright More options
  click, the dialog before clicking Export, and the success notification after
  the Playwright Export click.
- Screenshot extraction records elements and a separate `title_items` list in
  dashboard-wide reading order. Every title item has a fixed
  `standard_element_name`/`element_type` and its exact `element_text`; title
  comparisons retain these names, associated visual, reading order, and
  source/normalized coordinates. `title_items` is the canonical visible-text
  inventory when supplied; the same labels are not appended a second time from
  structured visual content. Distinct visible lines remain distinct items.
- Capture performance is logged by major stage in `operation_timings.jsonl`
  and recorded per state in `extracted.json` and `capture_performance.jsonl`.
  State settling can finish early only after a visible Power BI loading
  indicator or visible image load is observed and then clears; when no such
  signal exists, the configured settle delay is preserved. Class-only animated
  spinners are allowed 30 seconds before screenshot and accessible metadata
  capture continues with an `UNVERIFIED` render warning. Explicit busy
  indicators and incomplete visual images retain the 300-second readiness
  timeout. A visible query-resource error is recorded as a terminal render
  issue, not mistaken for loading. The report is checked again after visual
  exports and before screenshots are saved.
- Host/browser chrome (including Power BI's toolbar, workspace navigation, and
  collapsed Filters-pane handle), Home/Pages navigation labels, page navigators,
  host logos, and decorative/icon-only images are excluded from extraction.
  Dashboard-authored brand names are retained as `brand_logo` title items;
  meaningful labeled icons (such as Help and Information), report images, and
  actual report slicers remain in scope.
- Raw `extracted.json` preserves dashboard-authored branding and button-label
  evidence, but `brand_logo` and `button_label` records are omitted from
  comparison results and report artifacts.
- Pages without accessible dropdown/select filters are compared in their
  initial state only unless they expose accessible checkbox-based slicers or
  grouped Power BI bookmark-button controls with a verifiable active option.
  Visual labels and chart text are not treated as filters.
- Each filter is checked for a `Clear selections` action on every report page.
  When available, the action is clicked and that filter is omitted from
  planning. Because each filter case runs in a fresh browser context, the
  recorded cleared state is reapplied in that context before other planned
  filters are selected. Filters without a clear action contribute their
  individual options to the Cartesian product; checkbox filters are first
  verified with all options selected. Aggregate `All` / `Select all` entries
  are not individual multi-select cases. With `n` planned filters, the number
  of states is the product of their planned option counts.
- Captured filter values are compared directly; slicer controls are excluded
  from visual matching. Equivalent currency/number formatting is normalized,
  dashboard-name differences are surfaced as identity metadata, and refresh
  timestamps are reported as freshness metadata rather than data mismatches.
-   Elements and title/label occurrences are sequence-aligned in each side's
  original `reading_order`, allowing insertion and deletion gaps so later
  elements do not cascade into mismatches. Pairing uses text similarity,
  category/type, associated visual, and coordinates when available; text alone
  is not an identity key. Original `reading_order` values are retained
  separately from the aligned comparison position. One-sided items are
  reported as added/removed and remain `UNVERIFIED` when screenshot evidence
  alone cannot prove absence. At aligned elements, reports distinguish
  unchanged, renamed, moved, and reclassified title properties. A move uses a heuristic 0.08
  normalized-coordinate center-distance threshold only when both normalized
  bounds are available; raw pixel bounds are not used to classify movement, and
  sequence changes alone do not count as a spatial move. Table rows and headers
  remain in captured order. Entries without enough extracted data are
  `UNVERIFIED` instead of confirmed mismatches.
- Page tabs are visited when exposed as accessible `tab` controls. After report
  readiness, discovery waits briefly if only Power BI application tabs are
  exposed; if no report pages appear, it reports the observed tabs rather than
  treating host navigation as report pages. DOM and filter plans are captured
  per page.
- Dashboard A's filter plan supplies canonical names for matching controls.
  Each dashboard's own inventory supplies its complete option list, so
  dashboard-only filters and values are captured too. Those unmatched
  page/filter states remain visible in the comparison instead of being
  silently dropped.
- Both URLs are always opened and extracted independently, including when they
  identify the same report. This makes identical-URL comparisons useful for
  checking capture/model consistency rather than forcing a match by reusing data.
- Model calls use zero-temperature generation and a shared
  `dashboard_url_comparator` extraction skill prompt to reduce extraction
  variation; small differences can still occur if the report changes while it
  is being captured.
- Interrupted Vertex AI responses and transient transport failures are retried
  up to three times with short backoff. Persistent failures are reported as
  comparison errors; the dashboard capture and filter/export actions are not
  automatically replayed.
- Screenshot extraction returns versioned structured JSON with visible
  dashboard/page/refresh metadata, typed visual content, table columns and
  cell-level read status, chart series, filter state, and extraction-quality
  counts. Scrolled screenshot merges retain the structured table cells as well
  as the comparison-compatible rows; clipped or unreadable content is marked
  rather than filled with guessed values.
- Power BI may render some report content inside inaccessible or canvas-based
  visuals. Gemini extraction is visual and can be uncertain; review extracted
  JSON before treating differences as authoritative.
- Exported visual files are saved beside each state under an `_exports`
  directory. Exported table rows are included in each element's `rows` field.
- Successfully exported summarized data is compared cell-by-cell and included
  in `comparison.json`. Export option
  availability, outcome, file names, and menu screenshot references remain in
  `comparison.json`. Missing or failed exports remain distinct from empty
  data.
- The browser does not bypass authentication or access controls. Use an
  authorized storage state and comply with your organization's policies.

  ## Copilot skill

  The workspace includes `.github/skills/dashboard-url-comparator/SKILL.md`.
  Invoke it in VS Code with `/dashboard-url-comparator` for a structured
  comparison workflow. The Google ADK/Gemini extraction agent uses the matching
  runtime guidance in `src/powerbi_dashboard_comparator/agents.py`; it does not
  automatically load VS Code skill files.
