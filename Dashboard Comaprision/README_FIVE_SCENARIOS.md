# Five Scenario Comparison

The API's `run_validation_scenarios` option adds the scenario captures to the
existing Dashboard Comparison workflow. Scenarios are captured and validated
for both dashboards before matching page/scenario states are compared and the
existing DOCX and Excel reports are generated.

## Scenarios

| Report label | Evidence |
|---|---|
| 01 - Initial Default State | The baseline dashboard screenshot for each page |
| 02 - Single Filter Selection | One verified common filter selection |
| 03 - Multiple Filter Selection | Multiple verified values on an applicable filter |
| 04 - Drill Down | Verified screenshots at drill levels |
| 05 - Table Column Header Comparison | Horizontal table screenshots compared with screenshot-only Vision |

The table-header capture uses internal scenario ID `01`, but is displayed as
Scenario 05. It does not use OCR, DOM text, or programmatic text extraction for
header identification or comparison.

## Run locally

Start the API from the repository root:

```powershell
.\venv\Scripts\Activate.ps1
$env:POWERBI_HOME_URL = "https://app.powerbi.com/home?ctid=<tenant-id>&experience=power-bi"
$env:GOOGLE_CLOUD_PROJECT = "your-gcp-project-id"
$env:GOOGLE_APPLICATION_CREDENTIALS = "C:\path\to\credentials.json"
uvicorn Backend.api:app --host 127.0.0.1 --port 8001 --reload
```

In another PowerShell window, submit a job:

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

$job_id = ($response.Content | ConvertFrom-Json).job_id
Invoke-WebRequest -Uri "http://127.0.0.1:8001/api/jobs/$job_id"
```

Poll the job endpoint until completion. The existing report downloads are
`Dashboard_Comparison.docx` and `Dashboard_Comparison.xlsx`; API jobs do not
produce a separate scenario-comparison report.

## Evidence locations

```text
output/jobs/<job_id>/
├── dashboard_A/<page>/             # Scenario screenshots for Dashboard A
├── dashboard_A/evidence_manifest.json
├── dashboard_B/<page>/             # Scenario screenshots for Dashboard B
├── dashboard_B/evidence_manifest.json
├── planning/scenario_evidence/     # DOM, execution, and status JSON
├── validation_summary.json
├── Dashboard_Comparison.docx
└── Dashboard_Comparison.xlsx
```

The manifests identify the corresponding page, scenario, filter state,
drill/scroll state, screenshot path, capture status, and execution evidence.
Scenario subfolders are not needed inside the dashboard page folders.

See [README.md](./README.md) for workflow behavior and
[LOCAL_SETUP_GUIDE.md](./LOCAL_SETUP_GUIDE.md) for setup details.
