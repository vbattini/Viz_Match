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
   that page's filter states. Dashboard A and B run concurrently. Scenario
   execution processes up to five page pairs at a time; standard capture is
   bounded to two pages per dashboard and two concurrent state captures by
   default.
   Dropdown slicers are dismissed and their collapsed state verified after
   reading or applying a selection, so an open filter menu cannot cover report
   visuals during export or screenshot capture.
   Gemini requests run with a global limit of two concurrent requests by
   default. Each request has a five-minute timeout and up to five attempts with
   exponential backoff, helping transient Vertex AI transport failures recover
   without occupying capture workers indefinitely. Comparison work is also
   bounded to two workers by default.
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
`PBI_MAX_PARALLEL_PAGE_CAPTURES` (default `2`),
`PBI_MAX_PARALLEL_STATE_CAPTURES` (default `2`),
`PBI_MAX_PARALLEL_COMPARISONS` (default `2`), and
`PBI_VALIDATION_MAX_PARALLEL_PAGES` (default `5` page pairs; one page from each
dashboard). Values must be positive. Model requests retry transient transport
failures, rate limits, server errors, and timeouts with exponential backoff,
up to the configured attempt count. Lower concurrency if Vertex AI quotas or
machine capacity require it.
Screenshot extraction uses the 65,536-token output limit for Gemini 2.5 Flash,
3.7 Flash, and 3.8 Flash; other configured models retain their own default
output limit. Gemini 3.7 and 3.8 Flash use model-default sampling settings
because `temperature` is not configurable for these models.
Concurrency settings are read when the API starts. Restart Uvicorn after
changing them for the new values to take effect.

For reports requiring authentication, create a Playwright storage state file
with an authenticated browser session and pass it to the CLI:

```powershell
python -m playwright codegen --save-storage=auth.json https://app.powerbi.com/
```

Sign in in the opened browser, then close it to save the session. Protect
`auth.json`; it contains session credentials and should not be committed.

## Run

Set `POWERBI_HOME_URL` to your tenant-scoped Power BI Home URL before starting
the app so discovery preserves its `ctid`:

```powershell
$env:POWERBI_HOME_URL = "https://app.powerbi.com/home?ctid=<tenant-id>&experience=power-bi"
python -m uvicorn Backend.api:app --host 127.0.0.1 --port 8000 --reload
```

Run this from the repository root. `--reload` picks up source changes during
development; without it, stop and restart Uvicorn after changing code. The
`powerbi-compare` console command uses the checkout registered by the most
recent editable install, so run `python -m pip install -e .` here if that
command is resolving to another checkout.

Then open <http://127.0.0.1:8000>. Add up to 10 jobs with Workspace Name,
Semantic Model, and Report Name; all three fields are required for each job.
Edit job values directly in the worksheet and validate the rows before running.
To fill multiple cells at once, select the starting worksheet cell and paste
tab-separated rows copied from Excel with Ctrl+V. The worksheet keeps the
pasted row and column layout and enforces the 10-job limit.
All 10 input rows fit without a worksheet scrollbar. After the batch is
accepted, the input sheet collapses and the results panel moves into view; use
Show input sheet to expand it again.
The web UI runs the standard capture and comparison, then captures all four
functional scenarios, validates their evidence, and compares each page/scenario
pair. Standard DOCX/Excel generation is deferred until those scenario
comparisons finish. Clear **Run four functional scenarios** to run only the
standard comparison. Scenario runs save their per-page screenshots and JSON
under the job folder, and add Scenario DOCX, Scenario JSON, Evidence Validation
JSON, and Execution JSON links to the job results.
Jobs execute sequentially in the displayed order, and a failed job does not
prevent later jobs from running by default. Semantic Model is retained as job
metadata only and is not used to find or validate Power BI assets. The UI uses
the configured Vertex AI project/model and local `auth.json` storage state,
and does not accept credentials in the form.
The GCP workspace/report names default to `Tredence <Workspace Name>` and
`<Report Name> GCP`; configure `POWERBI_GCP_WORKSPACE_PREFIX` or
`POWERBI_GCP_REPORT_SUFFIX` to change those naming rules.
The DOCX summary uses the submitted report and semantic-model names plus the
discovered Snowflake and GCP workspace names.
Only one comparison runs at a time. Job status is polled in the browser, and
the UI stopwatch counts up while the comparison runs and displays the final
execution time as `MM:SS:mmm` (minutes, seconds, milliseconds) when it
completes. There is no 60-second time limit. Completed DOCX and Excel reports
can be downloaded for each job. Each downloadable ZIP folder contains only the
report-named DOCX, report-named Excel workbook, and a `PowerBI_Links.txt` file
with the Dashboard A/Snowflake and Dashboard B/GCP URLs. When scenarios ran,
the ZIP also includes `Validation_Execution_Summary.json`; there is no
separate scenario-comparison report. The batch download collects available
report folders, including completed jobs if a later job fails or is skipped.
Each web-UI job folder uses the first dashboard's report name with a unique
job ID suffix. At the start of each run, the app retains only the five most
recently modified job folders under `output/jobs` and removes older folders.
If Windows blocks a folder rename, the comparison still completes and keeps
the output in its UUID-named folder.

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
assignment, then report generation. When scenarios are enabled, that final
report stage is deferred until after scenario capture, evidence validation, and
scenario comparisons. ADK/Gemini is used for screenshot extraction; comparison
statuses remain controlled by Python. See
[workflow.py](./Backend/workflow.py).

The app finds exact workspace and report names in the authenticated Power BI
session before starting screenshot capture. Keep the server bound to localhost
unless you add authentication and HTTPS for remote access.

## CLI

```powershell
powerbi-compare `
  --url-a "https://app.powerbi.com/groups/.../reports/..." `
  --url-b "https://app.powerbi.com/groups/.../reports/..." `
  --output-dir ".\output" `
  --storage-state ".\auth.json"
```

To compare saved evidence without opening Power BI, run only Scenario 01 with
the job directory that contains its dashboard manifests and planning evidence:

```powershell
python -m Backend.scenario_comparison `
  --scenario-id 01 `
  --scenario-source "01=.\output\jobs\<job-id>" `
  --output-dir ".\output\scenario-01-comparison"
```

This compares table headers using the designated initial screenshot and every
manifested horizontal-scroll screenshot. It does not execute Scenarios 02–04.

Use `--headed` to watch browser activity or `--settle-seconds` to adjust the
wait after selecting a filter. Filters are exercised exhaustively (see Notes),
so there is no per-filter value cap.

Add `--validation-scenarios` to run the deterministic validation scenarios
for every page instead of the default capture (see Validation scenarios below).
The filter plan is built from the DOM before any scenario action runs. Add
`--scenario {01,02,03,04}` (with `--validation-scenarios`) to run only that
one scenario. Requesting `--scenario 03` by itself skips the multiple-filter
scenario because its single-filter prerequisite was not run.
Add `--through` with `--scenario` to run a contiguous range, for example
`--scenario 02 --through 03`. When both filter scenarios run in that range, the
multiple-filter scenario preserves the single-filter selection and adds exactly
one option on a different filter. Both selections are verified before its
screenshot, then the original filter state is restored.
Validation runs five page pairs at a time by default (Dashboard A and B in
parallel within each pair, up to ten live report pages); set
`PBI_VALIDATION_MAX_PARALLEL_PAGES` before starting the API to change this
limit. Increase it cautiously because each additional pair opens two more live
Power BI pages; lower it if the machine becomes unstable. Add `--cdp-url` to
attach to an already-signed-in Chromium or Edge instance.

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
non-filter visual checks by page and omits slicer/dropdown controls, which are
handled in the Filter Validation Log. Remaining visual checks include applied
filter selections when available and use Pass/Fail/Unable to Verify dropdowns.
Page / Tab Name fields contain only the Power BI page name; scenario names and
filter actions remain separate validation-log fields. Screenshot galleries
include only captured images and do not add missing-screenshot notification
paragraphs; capture and evidence failures remain visible in validation
statuses. Matching, reliably captured filter selections pass even when the
control type is unknown; differing selections fail, and unavailable selections
remain Unable to Verify.
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

## Validation scenarios

With `--validation-scenarios`, each page runs four checks in the same browser
sessions. Scenario 01 uses LLM Vision to compare the captured table-header
screenshots; scenarios 02 through 04 also use LLM Vision on corresponding
captures after all applicable evidence has been gathered. One page pair runs
at a time by default, with Dashboard A and Dashboard B running in parallel for
that pair. Both sides build a plan from the DOM first, which is written to
`planning/<page>/`.
Scenario 01 is captured before any filter or drill interaction. No independent
Initial Default State scenario is run. The per-page scenarios run in this order:

1. `01_table_column_header_comparison`: captures the initial viewport first.
   If neither dashboard has a horizontal table scrollbar, it captures no more
   images and marks the scenario `SKIPPED`. Otherwise each dashboard is
   scrolled right in increments no larger than half a viewport, with a
   screenshot after each position change, until its extreme-right position
   is reached. This provides overlapping views while avoiding duplicate
   screenshots when the position does not change; vertical scroll is never
   changed. After both sides finish capture, one LLM Vision comparison reads
   every screenshot and reports matching, missing, additional, renamed,
   differently ordered, or unreadable headers. Header identification and
   comparison use screenshot evidence only; DOM text, OCR, programmatic text
   extraction, regex, and fuzzy matching are not used.
2. `02_single_filter_selection`: applies exactly one random shared option on a
   common filter and verifies the selection on both dashboards. Its screenshot
   is captured only after successful verification. Skipped when no common
   filter option is available.
3. `03_multiple_filter_selection`: runs only after scenario 02 has been
   verified on both dashboards. It preserves the exact scenario 02 selection,
   applies exactly one shared option on a different common filter, and verifies
   both selections from the DOM before capture. Any supported filter type can
   be used. If scenario 02 was not run or verified, or no distinct filter with
   an unselected shared option is available, scenario 03 is `SKIPPED` with a
   reason; it never changes the first filter or falls back to selecting
   multiple values in one filter.
4. `04_drilldown`: clicks the enabled
   "Go to the next level in the hierarchy" control and captures each level. It keeps
   clicking until that control is disabled or absent, verifying that visible rows
   changed after each click. Skipped when no visual has a verified drill control;
   unverified visuals are skipped. Before and after screenshots are written for
   every level.

Default filter state is restored between independent scenarios. Scenario 02's
state is held only while scenario 03 adds its second filter, then both scenarios
are restored together. Before every scenario screenshot, the pointer is moved
off the dashboard and focused controls are blurred to avoid covering content
or leaving tooltips visible. A scenario that cannot run is `SKIPPED`. A
scenario that fails during execution is `UNVERIFIED` with diagnostic evidence,
and the page continues.

Each dashboard stores screenshots directly in a page folder:
`<output-dir>/dashboard_A/<page>/` or
`<output-dir>/dashboard_B/<page>/`. All applicable scenario screenshots for a
page share that folder; scenario subfolders are not used. Filenames include the
dashboard, scenario, page, and applicable filter combination, drill level, or
scroll position. There are no independent Initial Default State screenshots.
For API comparison jobs, the Dashboard A/B initial screenshots provide Scenario
01. The four functional scenarios are captured before comparison; their
screenshots are extracted with the existing Vision utility and added as
scenario-tagged states to the existing Dashboard Comparison pipeline. The old
`compare_dashboards` logic generates the comparison, and the existing DOCX and
Excel reports include all captured scenario screenshots and per-state results.
The table-header execution keeps internal ID `01` but is labeled Scenario 05
in the report. It uses screenshot-only Vision extraction of table headers;
DOM and OCR are not used for that scenario.
Each dashboard root contains an `evidence_manifest.json` with the page
identity, scenario and capture role, selected filter values, combination or
drill/scroll identifiers, capture timestamp and status, screenshot path,
scrollbar state, and associated execution JSON evidence. DOM, execution, and
execution-status JSON evidence is stored under
`planning/scenario_evidence/scenario_<ID>/pages/<page>/`. No separate
`comparison_results` directory or scenario-only report is produced by API jobs.
The standalone validation-scenario capture command does not write the base
comparison JSON, Excel, or DOCX reports. API jobs that run validation scenarios
generate the existing `Dashboard_Comparison.docx` and
`Dashboard_Comparison.xlsx` only after all scenario screenshots and execution
evidence have been captured and validated and the existing comparator has
processed the matched states. Missing, unmatched, or invalid scenario evidence
is reported as `UNVERIFIED`; skipped operations remain `SKIPPED`. A run-level
execution summary is written to `<output-dir>/validation_summary.json`.

For a screenshot-only inventory in the supplied report-template format, run:

```powershell
python -m Backend.visual_inventory_report `
  --source .\output_all `
  --output-dir .\output_visual_inventory
```

This uses LLM Vision on Scenario 01's saved initial screenshots and, where
available, the incremental horizontal screenshots. It records visible report/page
titles, filter names and control types, visual names/types, and ordered
table/matrix headers. Selected filter values, KPI numbers, chart data, and
table cells are intentionally excluded. The output includes a DOCX with Filter
Validation and Visual & Functional Validation logs, page screenshot pairs,
plus per-page inventory JSON.


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
- Captures use a consistent 2304x1080 viewport so wide report pages are not
  clipped at the right edge. Before screenshots, the mouse is
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
  runtime guidance in `Backend/agents.py`; it does not
  automatically load VS Code skill files.
