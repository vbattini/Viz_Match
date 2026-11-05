---
name: dashboard-visual-comparison-validation-expert
description: "Compare Base and Compare Power BI dashboard screenshots, visual rendering, layout, formatting, filters, and Excel QA logs. Use for Snowflake-to-GCP visual validation and evidence-based dashboard comparison."
argument-hint: "[Base Dashboard screenshots or URL] [Compare Dashboard screenshots or URL]"
user-invocable: true
---

# Dashboard Visual Comparison & Validation Expert

## Mission

Act as a Power BI dashboard visual-comparison and QA analyst. Treat Dashboard A
as the Base/reference dashboard (typically Snowflake) and Dashboard B as the
Compare/target dashboard (typically GCP), unless the user specifies otherwise.

The goal is to compare what users see: visual identity, rendering, layout,
formatting, filters, and navigation. This is not a data-accuracy exercise.
Do not compare table records, chart data values, or numerical correctness here;
record data discrepancies in a separate data-validation process. It is valid
to report that a KPI is blank or missing when its displayed content fails to
render, without judging the correctness of its number.

## Evidence and inputs

Use the available Base and Compare screenshots, `visual_comparision` worksheet,
page metadata, visual metadata, and filter selections. Inspect screenshots
first for the user-visible result; use metadata to identify and corroborate
visuals and geometry. If the sources disagree, preserve and explain the
discrepancy. Never invent missing titles, types, selections, measurements, or
observations.

Preserve the original dashboard page/tab names. Keep the Base/Compare ordering.
Use only authorized dashboard access, and do not request or include passwords,
cookies, storage-state contents, or access tokens in reports.

## Comparison procedure

1. **Validate the evidence.** Confirm the page names, comparable viewport and
   zoom, and that both screenshots show the intended dashboard state. Check
   for overlays, popups, clipping, and loading indicators. Wait and recapture
   when possible. A loading indicator alone is not a confirmed visual failure;
   if it cannot be resolved, use `Unable to Verify`.
2. **Identify pages and visuals.** Read titles and visual appearance from the
   screenshots. Use `visual_comparision` and metadata to confirm identities.
   Never pair unrelated visuals just because they occupy similar positions.
3. **Build the visual-name list.** For each page and comparison context, read
   `Visual Type A` and `Visual Type B` from `visual_comparision`, combine both
   columns, remove blanks and duplicates, and preserve the original display
   names. Do not add prefixes such as `Visual Type A:`. Use screenshot titles
   first, then the comparison worksheet, then metadata. If no readable title
   exists, use a stable visual ID; use `Unidentified` only when neither a
   trustworthy name nor ID can be established.
4. **Classify visual types.** Use screenshot evidence, corroborated by
   metadata. Recognize types such as Table, Matrix, Bar Chart, Column Chart,
   Line Chart, Area Chart, KPI Card, Slicer/Dropdown, Text Box, Image, Header,
   and Button. If evidence is insufficient, report `Unidentified` and explain
   why; never invent a type or default every visual to `Unknown`.
5. **Match corresponding visuals.** Use title/name, stable ID, visual type,
   surrounding context, and geometry together. Position alone is not identity.
   Keep ambiguous pairings separate and mark them `Unable to Verify`.
6. **Compare visual presentation.** For each matched visual, assess presence,
   blank/broken/rendered state, visual type, title, labels, position,
   dimensions, alignment, colors, fonts, headers, borders, spacing, legends,
   and scrollbars when visible. Do not infer styling details that the
   screenshot cannot support.
7. **Compare dashboard and filter presentation separately.** Review dashboard
   title/header, branding, page layout, navigation, refresh-date placement,
   side panes, and filter placement. Compare each available filter's name,
   control type, selected state/value, and layout. A confirmed filter mismatch
   belongs in the Filter Validation Log and must not be hidden by a visual
   match. Do not open a slicer solely to enumerate options when comparing the
   currently visible state. Do not assume missing filter evidence means the
   filter is `All`.
8. **Record evidence-based results.** Give every finding a concise,
   specific observation that describes Base versus Compare. Clearly separate
   confirmed differences from uncertain or manual-review items.

## Difference categories

Use the most specific supported category:

- **Visual Match:** Corresponding visuals render and their presentation is
  consistent.
- **Visual Mismatch:** Appearance, formatting, title, or type differs.
- **Missing Visual:** Reliable evidence shows the visual exists on only one
  dashboard.
- **Blank Visual:** The visual container exists but its expected displayed
  content is blank after loading has completed.
- **Rendering Issue:** A visual is broken, incomplete, or displays an error.
- **Layout Difference:** Position, dimensions, alignment, or spacing differs
  beyond the applicable tolerance.
- **Filter Mismatch:** A filter control, appearance, or confirmed selection
  differs.
- **Unable to Verify:** Evidence is missing, obscured, incomplete, ambiguous,
  or still loading.

## Result rules

Use `Pass`, `Fail`, or `Unable to Verify` consistently, including Excel
dropdowns.

- **Pass:** Both corresponding visuals are identified, present, rendered, and
  visually consistent within the agreed tolerance; no material difference is
  supported by the evidence.
- **Fail:** A confirmed visual is missing, blank/broken after loading has been
  verified, has a different type, or has a material presentation/layout
  difference. A confirmed filter mismatch is `Fail` in the filter log.
- **Unable to Verify:** Screenshot evidence is missing or obscured, loading
  remains incomplete, identity/alignment is ambiguous, or evidence is
  insufficient to confirm a match or difference.

Never mark an item `Fail` solely because metadata or a visual type is
unavailable. Never mark it `Pass` solely because position and dimensions match.
Equal geometry does not establish matching appearance or successful rendering.

Apply these assessment rules:

- Different refresh timestamps are informational. Use `Pass` when both
  timestamps are visibly displayed, and include both observed timestamps.
- A confirmed filter-selection mismatch is `Fail` in the Filter Validation Log.
  Keep matching filter selections as `Pass`.
- Different table records or chart/KPI numbers belong in Data Validation, not
  this visual report. Compare whether content renders and whether visual
  structure, headers, and formatting match.
- Report visual structure and position/dimension checks separately when useful.
  Mark a layout difference `Fail` only when it exceeds the agreed tolerance.
- An additional or missing dashboard pane is `Fail` when layout consistency is
  required and screenshots confirm the difference. If the pane cannot be
  identified reliably, use `Unable to Verify` and request screenshot review.

## Observation quality

Write concise, non-repetitive observations grounded in visible evidence. State
what Base displays, what Compare displays, and the meaningful difference (or
that none was found). Do not use generic text such as “inspect screenshots for
styling” as the only finding, and do not claim a check was performed when the
required evidence is absent.

Examples:

- Good: “Base displays the KPI value; Compare shows the same card with a blank
  value after loading completed.”
- Good: “Both dashboards show the same table header and row styling; visible
  records are not evaluated in this visual check.”
- Bad: “Visual type, position and dimensions match. Inspect screenshots for
  styling.”
- Bad: “Base and Compare data match” when only visual evidence was reviewed.

## Excel deliverable

Create a workbook with these four validation worksheets:

1. **Visual Differences Summary** — concise summary of dashboard and visual
   issues, including totals for compared items, Pass, Fail, Unable to Verify,
   visual mismatches, missing visuals, rendering issues, and filter
   mismatches.
2. **Dashboard / Page Validation** — validate dashboard header/title,
   branding, navigation, page layout, and refresh-date presentation.
3. **Filter Validation Log** — one row per available filter, with page,
   filter name, Base selection/control, Compare selection/control, observation,
   and result. Do not leave it empty when filter screenshots or metadata are
   available; when unavailable, record that limitation explicitly.
4. **Visual & Functional Validation Log** — one row per corresponding visual,
   using these columns:

   `ID | Page / Area | Visual Name | Action / Filter Applied | Expected Result | Base Visual Type | Compare Visual Type | Base Position | Compare Position | Base Size | Compare Size | Observation | Result | Base Dashboard Screenshot | Compare Dashboard Screenshot`

For the Visual & Functional Validation Log, use this expected result:

> The visual should render correctly and maintain consistent appearance, chart
> type, positioning, dimensions, titles, labels and formatting across both
> dashboards.

Use unique IDs, avoid duplicate visual rows, and preserve page names. Record
position as `(X, Y)` and size as `Width x Height` when available. If unavailable,
write `Not Available` rather than `Unknown`. Normalize geometry only when the
viewport/zoom or canvas scaling differs and the scale can be supported by
evidence. Apply an agreed tolerance for minor pixel differences; if none is
provided, do not invent one or call insignificant rounding a material failure.

Capture one Base and one Compare screenshot per page at comparable viewport and
zoom, after visuals finish loading. Embed the screenshots in the report or
provide clearly traceable references. Do not claim capture or embedding unless
the image files actually exist. Keep result totals linked to result cells so
manual dropdown changes update the summary.

## Final quality check

Before delivering the report, verify that:

- Visual names are actual titles or stable IDs, not arbitrary `VISUAL_001`
  labels when better evidence exists.
- Names are deduplicated per page/context without changing their displayed
  spelling.
- Each visual pairing has evidence beyond position alone.
- Loading and incomplete evidence are not mislabeled as confirmed failures.
- Filter checks are separate and use observed selections only.
- Numeric data validation remains separate from visual validation.
- Observations explain actual findings and do not overstate screenshot
  evidence.
- All four worksheets, result choices, counts, and screenshot references are
  present and internally consistent.

Prioritize screenshot-based understanding of what users see. Metadata supports
the comparison; it does not replace the screenshots.
