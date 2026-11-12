# Quick Start - Power BI Dashboard Comparator with Five Scenarios

## 30-Second Setup

```powershell
# 1. Open PowerShell in your project directory
cd C:\Users\VijayBattini\Pictures\test3

# 2. Set up environment and launch API
.\setup_and_run.ps1 -Mode api -TenantId "e7c6f609-f63a-4ec7-b71e-6734c4c9d231" -GoogleCloudProject "your-gcp-project"

# 3. API starts on http://127.0.0.1:8001
# Check docs at http://127.0.0.1:8001/docs
```

## Manual Steps (If Setup Script Doesn't Work)

```powershell
# Activate environment
.\venv\Scripts\Activate.ps1

# Set credentials
$env:POWERBI_HOME_URL = "https://app.powerbi.com/home?ctid=e7c6f609-f63a-4ec7-b71e-6734c4c9d231&experience=power-bi"
$env:GOOGLE_CLOUD_PROJECT = "your-project"
$env:GOOGLE_APPLICATION_CREDENTIALS = "C:\path\to\credentials.json"

# Start API server
uvicorn Backend.api:app --host 127.0.0.1 --port 8001 --reload
```

## Test Scenarios via cURL

```powershell
# Scenario 1: Run comparison WITH five validation scenarios
$body = @{
    workspace_name = "Your Workspace"
    semantic_model = "Your Model"
    report_name = "Your Report"
    run_validation_scenarios = $true
} | ConvertTo-Json

$response = Invoke-WebRequest -Uri "http://127.0.0.1:8001/api/compare" `
    -Method POST -Headers @{"Content-Type"="application/json"} -Body $body

$response.Content | ConvertFrom-Json
# Response: {"job_id": "uuid", "status": "queued"}

# Scenario 2: Check job status
$job_id = "paste-job-id-here"
Invoke-WebRequest -Uri "http://127.0.0.1:8001/api/jobs/$job_id" -Method GET | ConvertFrom-Json

# Scenario 3: Get validation summary
Invoke-WebRequest -Uri "http://127.0.0.1:8001/api/jobs/$job_id/validation-summary.json" -Method GET
```

## Test via CLI

```powershell
# Activate environment
.\venv\Scripts\Activate.ps1

# Set URL environment variable
$env:POWERBI_HOME_URL = "https://app.powerbi.com/home?ctid=e7c6f609-f63a-4ec7-b71e-6734c4c9d231&experience=power-bi"

# Run all five scenarios
python -m Backend.cli `
    --url-a "https://app.powerbi.com/groups/.../reports/..." `
    --url-b "https://app.powerbi.com/groups/.../reports/..." `
    --output-dir output/test_scenarios `
    --storage-state auth.json `
    --validation-scenarios

# Run only scenario 01 (initial state)
python -m Backend.cli `
    --url-a "dashboard-a-url" `
    --url-b "dashboard-b-url" `
    --output-dir output/s01 `
    --storage-state auth.json `
    --validation-scenarios `
    --scenario 01

# Run scenarios 02 through 04
python -m Backend.cli `
    --url-a "dashboard-a-url" `
    --url-b "dashboard-b-url" `
    --output-dir output/s02-s04 `
    --storage-state auth.json `
    --validation-scenarios `
    --scenario 02 `
    --through 04
```

## Five Scenarios Explained

| # | Name | Tests |
|---|------|-------|
| 01 | **Initial Default State** | Base dashboard rendering |
| 02 | **Single Filter** | Select one filter value |
| 03 | **Multiple Filters** | Select up to 2 filter values |
| 04 | **Drilldown** | Navigate drilldown hierarchy |
| 05 | **Table Headers** | Column header comparison |

## Output Location

Results go to: `output/jobs/<report_name>_<job_id>/`

Key files:
- `Dashboard_Comparison.docx` and `Dashboard_Comparison.xlsx` - Existing reports, including matched scenario states
- `validation_summary.json` - Run-level scenario execution summary
- `dashboard_A/` and `dashboard_B/` - Scenario screenshots organized by page, with an `evidence_manifest.json` in each
- `planning/scenario_evidence/` - Per-page execution, DOM, and status JSON evidence
- `operation_timings.jsonl` - Performance metrics

## Common Issues & Fixes

| Issue | Fix |
|-------|-----|
| `auth.json not found` | Run: `python -m Backend.cli --url-a test --url-b test 2>&1` to trigger auth |
| `Playwright not installed` | Run: `playwright install chromium` |
| `ModuleNotFoundError` | Activate venv: `.\venv\Scripts\Activate.ps1` |
| `uvicorn not found` | Install: `pip install -e .` |
| `Google credentials missing` | Set `GOOGLE_APPLICATION_CREDENTIALS` env var |
| `"in_progress" for hours` | Increase timeout: `--settle-seconds 5` or `--headed` mode |

## Folder Structure

```
test3/
├── Backend/
│   ├── api.py               ← API server
│   ├── cli.py               ← CLI commands
│   ├── validation_scenarios.py  ← Five scenarios implementation
│   ├── workflow.py          ← Main orchestration
│   └── ...
├── Frontend/                ← Web UI (optional)
├── output/
│   └── jobs/                ← Results go here
├── auth.json                ← Power BI authentication
├── LOCAL_SETUP_GUIDE.md     ← Detailed setup
├── QUICK_START.md           ← This file
├── setup_and_run.ps1        ← Auto setup script
└── pyproject.toml           ← Dependencies
```

## Next: Advanced Usage

- **Batch processing:** See `LOCAL_SETUP_GUIDE.md` section "API Batch Requests"
- **Custom model:** Set `GEMINI_MODEL=gemini-pro` before running
- **Headed mode:** Add `--headed` flag to see browser
- **Debug mode:** Add `--headed` for visual debugging
- **Performance:** Check `operation_timings.jsonl` for bottlenecks

---

**For detailed setup, see:** [`LOCAL_SETUP_GUIDE.md`](./LOCAL_SETUP_GUIDE.md)
