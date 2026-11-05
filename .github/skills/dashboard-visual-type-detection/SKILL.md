---
name: dashboard-visual-type-detection
description: "Identify and classify Power BI dashboard visuals from screenshots. Use for screenshot-first visual inventory, chart-type detection, blank-visual assessment, and Base-versus-Compare type matching."
argument-hint: "[Base and Compare dashboard screenshots]"
user-invocable: true
---

# Dashboard Visual Type Detection & Classification

## Mission

Identify every visible dashboard visual from screenshot evidence, determine its
visual type and rendering status, and produce a reliable visual inventory for
use in a later dashboard comparison. Analyze Base and Compare screenshots
independently before matching them.

## Evidence priority

Use evidence in this order:

1. Dashboard screenshots and visible visual structure.
2. Axes, marks, headers, labels, legends, and visual boundaries.
3. Visual titles and surrounding page context.
4. DOM/OuterHTML.
5. Visual metadata and IDs.

Never classify a visual using only its ID, HTML container, or title. Use DOM
and metadata to corroborate screenshot findings, not replace them. If evidence
conflicts, report the discrepancy. Never invent a title, type, measurement, or
rendering state.

## Procedure

1. **Validate screenshots.** Confirm the intended page and dashboard, and check
   for overlays, clipping, loading indicators, or incomplete capture. Exclude
   browser chrome and application navigation from the dashboard canvas.
2. **Inventory each dashboard separately.** Identify every visible report
   visual from the full screenshot, including charts and shapes absent from
   DOM metadata. Record its visible title, type, position, size, rendering
   status, and confidence. Treat each visual as an independent object; identify
   slicers separately from charts. When a stable DOM ID is unavailable, assign
   a screenshot ID in reading order and provide normalized screenshot bounds.
3. **Classify by appearance.** Use chart marks, axes, layout, and content
   structure. Titles are supporting evidence only.
4. **Assess rendering separately from type.** A visual can have an identifiable
   expected type while its current content is blank, loading, partial, or in
   error.
5. **Match after independent classification.** Match Base to Compare using
   title, type, structure, and context together. Position alone is not enough.
6. **Report uncertainty.** Use `Unidentified` when the type cannot reasonably
   be established. Use `Not Available` for unsupported measurements. Explain
   the evidence gap rather than defaulting to `Unknown`.

## Visual type guide

- **Table:** Flat records in rows and columns with headers.
- **Matrix:** Hierarchical or cross-tabulated rows/columns, often with grouped
  levels, expand/collapse, or hierarchical subtotals.
- **Bar Chart:** Horizontal bars. Distinguish clustered, stacked, and 100%
  stacked forms when the marks support it.
- **Column Chart:** Vertical bars. Distinguish clustered, stacked, and 100%
  stacked forms when supported.
- **Line Chart:** Data points connected by line(s).
- **Area Chart:** A line series with a filled region beneath it.
- **Pie Chart / Donut Chart:** Circular category slices; a donut has a central
  hole.
- **Treemap:** Adjacent or nested rectangles whose area represents values.
- **Scatter Plot:** Individual points positioned on two numerical axes.
- **Bubble Chart:** Scatter-like points whose size encodes another measure.
- **KPI Card:** A prominent single metric, possibly with a trend, comparison,
  or sparkline. A multi-row card presents several such metrics together.
- **Gauge:** An arc, pointer, or filled range showing progress toward a target.
- **Funnel Chart:** Progressively narrowing stages.
- **Waterfall Chart:** Floating positive/negative bars showing contributions
  between starting and ending values.
- **Ribbon Chart:** Ribbons show rank changes across categories.
- **Combo Chart:** Two chart types, such as columns and a line, in one visual.
- **Map / Filled Map:** Geographic points or shaded geographic regions.
- **Heatmap:** Values represented by color intensity across a grid or area.
- **Histogram:** Bins show the distribution of a numerical measure.
- **Slicer / Dropdown:** An interactive filter control such as a list, dropdown,
  checkbox selector, or date selector.
- **Text Box / Image / Logo / Button:** Classify by visible content and
  function.
- **Shape / Line:** A visible divider or rule is rendered even when it has no
  data marks, labels, or other content.
- **Other / Unidentified:** Use only when the visible evidence does not
  support a more specific type.

Do not treat every grid as a Table or every rectangular mark as a bar chart.
Distinguish Bar from Column, Line from Area, and Pie from Donut by their
visible structure. Do not assign a type from a title alone.

## Rendering status

Record one status independently of visual type:

- **Rendered:** Content is visible and identifiable.
- **Blank:** The visual container exists, but expected content is not visible.
- **Partial:** Some content is visible, but the visual is incomplete.
- **Loading:** A loading indicator or progress state is visible.
- **Error:** An explicit error or broken visual is shown.
- **Unable to Verify:** Screenshot evidence is insufficient or obscured.

Examples:

- A KPI showing `(Blank)` is `Blank`.
- A KPI showing `0.00` is `Rendered`; do not infer that the value is correct.
- A titled chart with an empty plotting area is `Blank` only after loading is
  complete.
- A spinner is `Loading`, not automatically a failure.
- A chart with visible bars and labels is `Rendered`.

Do not compare underlying data values as part of visual-type detection.
Record whether content renders; send data-accuracy discrepancies to a separate
data-validation process.

## Base and Compare matching

Classify each side independently, then match corresponding visuals using
title/name, visual structure, context, and metadata together. Record each
side's actual type and rendering status.

When a Base visual is identifiable but its matched Compare visual is blank,
the Base type may be recorded as the **expected** Compare type. Clearly label
it as inferred. Do not claim that the blank Compare visual's actual type was
detected. Its type match is `Unable to Verify` unless other visible evidence
confirms the type; its confirmed blank rendering may still be reported
separately.

## Required output

Produce both tables, preserving page names and Base/Compare order.

### Visual comparison

One row per matched or unmatched visual:

`Page | Visual Name | Base Visual Type | Compare Visual Type | Base Rendering | Compare Rendering | Type Match | Observation`

### Visual inventory

One row per detected visual on each dashboard:

`Page | Dashboard | Visual Name | Detected Type | X | Y | Width | Height | Rendering Status | Confidence`

Use `Not Available` when a measurement is not supported. Confidence is:

- **High:** defining visual structure is clearly visible.
- **Medium:** the type is likely, but some defining evidence is obscured.
- **Low:** evidence is insufficient to distinguish plausible types.

Use actual titles where readable. If a title is absent, use a stable visual ID
only when it reliably identifies the visual; otherwise use `Unidentified`.
Avoid duplicate rows.

## Final checks

- Every visible visual has an inventory entry.
- Screenshot appearance, not DOM metadata alone, supports each classification.
- Tables and matrices; bars and columns; lines and areas; pies and donuts are
  distinguished using visible structure.
- KPI cards and slicers are identified separately from charts.
- Blank visuals are not labeled as rendered based only on title or metadata.
- A blank Compare visual's expected type is clearly separated from its
  unobserved actual type.
- Loading is not reported as a confirmed failure.
- Names, types, dimensions, and observations are evidence-based.

Use a two-stage workflow: first detect and classify each screenshot
independently; then match Base and Compare visuals and generate the comparison.
Screenshot-first visual recognition is mandatory.
