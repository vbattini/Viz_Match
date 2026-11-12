# Scenario Integration Guide

## Active API workflow

Start a job with `run_validation_scenarios = $true` in the
`POST /api/compare` request. The API then:

1. Captures the baseline state for each report page.
2. Captures the applicable filter, drill-down, and table-header evidence for
   both dashboards.
3. Validates the scenario manifests and execution JSON. Unmatched or missing
   evidence remains `UNVERIFIED`; unsupported scenarios are `SKIPPED`.
4. Uses the existing Vision extraction and deterministic dashboard comparator
   to compare matching page/scenario states.
5. Generates the existing `Dashboard_Comparison.docx` and
   `Dashboard_Comparison.xlsx` reports after comparison is complete.

The API does not run the separate legacy scenario-comparison pipeline or create
a separate scenario DOCX.

## Scenario labels in reports

| Report scenario | Source |
|---|---|
| 01 - Initial Default State | The baseline dashboard captures |
| 02 - Single Filter Selection | One verified common filter selection |
| 03 - Multiple Filter Selection | Verified multiple values on an applicable filter |
| 04 - Drill Down | Captured drill levels whose execution and restoration are verified |
| 05 - Table Column Header Comparison | Internal capture ID `01`; screenshot-only Vision header extraction/comparison |

Scenario 05 does not use OCR, DOM text, or programmatic text extraction to
identify or compare table headers. Other scenario screenshots use the existing
Vision-based extraction and comparison path.

## Job evidence layout

```text
output/jobs/<job_id>/
├── dashboard_A/
│   ├── <page>/
│   │   └── scenario screenshots for that page
│   └── evidence_manifest.json
├── dashboard_B/
│   ├── <page>/
│   │   └── scenario screenshots for that page
│   └── evidence_manifest.json
├── planning/
│   └── scenario_evidence/
│       └── scenario_<ID>/pages/<page>/
│           ├── dom_extraction.json
│           ├── scenario_execution.json
│           └── execution_status.json
├── validation_summary.json
├── Dashboard_Comparison.docx
└── Dashboard_Comparison.xlsx
```

Each dashboard manifest maps screenshot paths to page identity, scenario,
filter selections, pairing key, capture status, and associated execution JSON.
The dashboard page folders intentionally do not contain scenario subfolders.

## Local API invocation

```powershell
$body = @{
    workspace_name = "Your Workspace"
    semantic_model = "Your Model"
    report_name = "Your Report"
    run_validation_scenarios = $true
} | ConvertTo-Json

$response = Invoke-WebRequest -Uri "http://127.0.0.1:8001/api/compare" `
    -Method POST `
    -Headers @{"Content-Type"="application/json"} `
    -Body $body

$job = $response.Content | ConvertFrom-Json
$job_id = $job.job_id
```

Poll `GET /api/jobs/{job_id}` until the job is `completed` or `failed`, then
download the existing DOCX and Excel routes documented in
[API_QUICK_REFERENCE.md](./API_QUICK_REFERENCE.md). Review
`validation_summary.json` and the manifests if a scenario is skipped or
unverified.

## Implementation entry points

- `Backend/api.py` runs scenario capture before invoking the comparison workflow.
- `Backend/validation_scenarios.py` captures per-page scenario evidence.
- `Backend/scenario_evidence.py` manages screenshot paths and manifests.
- `Backend/scenario_state_adapter.py` validates and adapts evidence into
  scenario-tagged `DashboardState` objects.
- `Backend/workflow.py` defers comparisons until scenario evidence is loaded.
- `Backend/comparison.py` performs the existing deterministic comparison.
- `Backend/docx_report.py` and `Backend/visual_filter_report.py` generate the
  existing DOCX and Excel reports.

