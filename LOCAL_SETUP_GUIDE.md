# Local Development Setup - Power BI Dashboard Comparator with Five Validation Scenarios

## Overview
When validation scenarios are enabled, the API includes five report scenarios:
1. **Scenario 01: Initial Default State** - The existing baseline capture.
2. **Scenario 02: Single Filter Selection** - Apply and verify one common filter.
3. **Scenario 03: Multiple Filter Selection** - Apply and verify multiple values.
4. **Scenario 04: Drill Down** - Capture verified drill levels.
5. **Scenario 05: Table Column Header Comparison** - Compare header screenshots with Vision only.

The header capture uses internal scenario ID `01`; it is shown as Scenario 05
in the existing comparison reports. Scenarios run and their evidence is
validated before the existing DOCX/Excel reports are generated.

---

## Prerequisites

### 1. Python Environment (3.10+)
```powershell
# Check Python version
python --version

# Verify Python 3.10 or higher is installed
```

### 2. Virtual Environment Setup
```powershell
# Navigate to your project directory
cd C:\Users\VijayBattini\Pictures\test3

# Create virtual environment
python -m venv venv

# Activate virtual environment
.\venv\Scripts\Activate.ps1

# If you get execution policy error, run:
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### 3. Install Dependencies
```powershell
# Ensure you're in the activated venv
(venv) PS> pip install --upgrade pip setuptools wheel

# Install project with all dependencies
(venv) PS> pip install -e .

# Install development dependencies (optional)
(venv) PS> pip install -e ".[dev]"
```

### 4. Playwright Setup
```powershell
# Install Playwright browsers
(venv) PS> playwright install chromium
```

### 5. Power BI Authentication
Create a storage-state file for authenticated Power BI access:

```powershell
# Set the Power BI home URL environment variable
$env:POWERBI_HOME_URL = "https://app.powerbi.com/home?ctid=e7c6f609-f63a-4ec7-b71e-6734c4c9d231&experience=power-bi"

# Run authentication helper (creates auth.json)
(venv) PS> python -m Backend.cli --auth-only
# OR use the CLI tool to authenticate interactively
```

**Manual Authentication (if CLI doesn't work):**
```powershell
# Use this approach if you need to manually set up auth.json
# This will launch a browser for you to sign in
(venv) PS> python -c "
from playwright.sync_api import sync_playwright
from pathlib import Path

with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context()
    page = context.new_page()
    page.goto('https://app.powerbi.com')
    # Sign in manually
    input('Press Enter after signing in...')
    context.storage_state(path='auth.json')
    browser.close()
"
```

### 6. Google Cloud Configuration (for Vertex AI Gemini)
```powershell
# Set your Google Cloud credentials
$env:GOOGLE_APPLICATION_CREDENTIALS = "C:\path\to\your\credentials.json"
$env:GOOGLE_CLOUD_PROJECT = "your-project-id"
$env:GOOGLE_CLOUD_LOCATION = "us-central1"  # or your preferred region

# Optionally set specific Gemini model
$env:GEMINI_MODEL = "gemini-2.0-flash"
```

---

## Running the Application

### Option 1: API Server (FastAPI with Uvicorn)

#### Start the API Server
```powershell
# Activate venv if not already active
.\venv\Scripts\Activate.ps1

# Set environment variables (replace with your actual values)
$env:POWERBI_HOME_URL = "https://app.powerbi.com/home?ctid=e7c6f609-f63a-4ec7-b71e-6734c4c9d231&experience=power-bi"
$env:POWERBI_STORAGE_STATE = "auth.json"
$env:GOOGLE_CLOUD_PROJECT = "your-project-id"
$env:GOOGLE_CLOUD_LOCATION = "us-central1"
$env:GEMINI_MODEL = "gemini-2.0-flash"

# Start the API server on port 8001
(venv) PS> uvicorn Backend.api:app --host 127.0.0.1 --port 8001 --reload

# The API is now available at http://127.0.0.1:8001
# Interactive API docs at http://127.0.0.1:8001/docs
# ReDoc at http://127.0.0.1:8001/redoc
```

#### Testing the API with Validation Scenarios

**Using curl (Windows PowerShell):**
```powershell
# Define request
$body = @{
    workspace_name = "Your Workspace"
    semantic_model = "Your Model"
    report_name = "Your Report"
    run_validation_scenarios = $true
} | ConvertTo-Json

# Send comparison request with scenarios enabled
Invoke-WebRequest -Uri "http://127.0.0.1:8001/api/compare" `
    -Method POST `
    -Headers @{"Content-Type"="application/json"} `
    -Body $body

# Response will be: {"job_id": "uuid", "status": "queued"}
# Use job_id to poll progress:
$job_id = "returned-job-id"
Invoke-WebRequest -Uri "http://127.0.0.1:8001/api/jobs/$job_id" -Method GET
```

**Using Python:**
```python
import requests
import json

# Start comparison with validation scenarios
response = requests.post(
    "http://127.0.0.1:8001/api/compare",
    json={
        "workspace_name": "Your Workspace",
        "semantic_model": "Your Model", 
        "report_name": "Your Report",
        "run_validation_scenarios": True
    }
)

job_data = response.json()
job_id = job_data["job_id"]
print(f"Job started: {job_id}")

# Poll job status
import time
while True:
    status = requests.get(f"http://127.0.0.1:8001/api/jobs/{job_id}").json()
    print(f"Status: {status['status']}")
    if status['status'] in ['completed', 'failed']:
        break
    time.sleep(5)
```

### Option 2: Command-Line Interface (CLI)

#### Run Comparison with Validation Scenarios
```powershell
# Set environment variables
$env:POWERBI_HOME_URL = "https://app.powerbi.com/home?ctid=e7c6f609-f63a-4ec7-b71e-6734c4c9d231&experience=power-bi"
$env:GOOGLE_CLOUD_PROJECT = "your-project-id"
$env:GOOGLE_CLOUD_LOCATION = "us-central1"

# Full comparison with all five scenarios
(venv) PS> python -m Backend.cli `
    --url-a "https://app.powerbi.com/..." `
    --url-b "https://app.powerbi.com/..." `
    --output-dir output/scenarios_test `
    --storage-state auth.json `
    --validation-scenarios

# Run only specific scenario (e.g., scenario 02 - single filter)
(venv) PS> python -m Backend.cli `
    --url-a "https://app.powerbi.com/..." `
    --url-b "https://app.powerbi.com/..." `
    --output-dir output/scenario_02 `
    --storage-state auth.json `
    --validation-scenarios `
    --scenario 02

# Run scenarios 02 through 04
(venv) PS> python -m Backend.cli `
    --url-a "https://app.powerbi.com/..." `
    --url-b "https://app.powerbi.com/..." `
    --output-dir output/scenario_02_to_04 `
    --storage-state auth.json `
    --validation-scenarios `
    --scenario 02 `
    --through 04

# Show browser during execution (headed mode)
(venv) PS> python -m Backend.cli `
    --url-a "https://app.powerbi.com/..." `
    --url-b "https://app.powerbi.com/..." `
    --output-dir output/scenarios_headed `
    --storage-state auth.json `
    --validation-scenarios `
    --headed
```

---

## Output Structure

### After Running Validation Scenarios
The output directory will contain:

```
output/
├── jobs/
│   └── <job_id>/
│       ├── dashboard_A/
│       │   ├── <page>/
│       │   │   └── all applicable scenario screenshots
│       │   └── evidence_manifest.json
│       ├── dashboard_B/
│       │   ├── <page>/
│       │   │   └── all applicable scenario screenshots
│       │   └── evidence_manifest.json
│       ├── planning/scenario_evidence/
│       │   └── scenario_<ID>/pages/<page>/
│       │       ├── dom_extraction.json
│       │       ├── scenario_execution.json
│       │       └── execution_status.json
│       ├── validation_summary.json
│       ├── Dashboard_Comparison.docx
│       ├── Dashboard_Comparison.xlsx
│       └── operation_timings.jsonl
```

### Key Output Files:
- **Dashboard_Comparison.docx / .xlsx** - Existing comparison reports, generated after scenario evidence is captured and compared
- **validation_summary.json** - Run-level scenario execution summary
- **dashboard_A/evidence_manifest.json** and **dashboard_B/evidence_manifest.json** - Screenshot-to-scenario/page/evidence mappings
- **operation_timings.jsonl** - Performance metrics
- **planning/scenario_evidence/** - DOM, execution, and status JSON evidence; API jobs do not produce a separate scenario-comparison report

---

## Environment Variables Reference

```powershell
# Power BI
$env:POWERBI_HOME_URL = "https://app.powerbi.com/home?ctid=YOUR_TENANT_ID&experience=power-bi"
$env:POWERBI_STORAGE_STATE = "auth.json"  # Default location

# Google Cloud / Vertex AI
$env:GOOGLE_APPLICATION_CREDENTIALS = "C:\path\to\credentials.json"
$env:GOOGLE_CLOUD_PROJECT = "your-gcp-project-id"
$env:GOOGLE_CLOUD_LOCATION = "us-central1"  # us-west1, europe-west1, etc.
$env:GEMINI_MODEL = "gemini-2.0-flash"  # or "gemini-pro", "gemini-1.5-pro"

# Validation Scenarios
$env:PBI_VALIDATION_MAX_PARALLEL_PAGES = "5"  # Five pairs (up to ten live pages)
$env:PBI_MAX_PARALLEL_PAGE_CAPTURES = "2"
$env:PBI_MAX_PARALLEL_STATE_CAPTURES = "2"
$env:PBI_MAX_PARALLEL_MODEL_REQUESTS = "10"
$env:PBI_MODEL_REQUEST_ATTEMPTS = "5"  # Total attempts for transient Gemini errors
$env:PBI_MAX_PARALLEL_COMPARISONS = "2"

# API Server
$env:POWERBI_STORAGE_STATE = "auth.json"
```

---

## Troubleshooting

### Issue: "uvicorn not found" or "ModuleNotFoundError"
**Solution:**
```powershell
# Verify venv is activated (should see (venv) prefix)
.\venv\Scripts\Activate.ps1

# Reinstall dependencies
pip install -e .
pip install uvicorn
```

### Issue: "auth.json not found"
**Solution:**
```powershell
# Create authentication file
# Ensure POWERBI_HOME_URL is set first
$env:POWERBI_HOME_URL = "https://app.powerbi.com/home?ctid=e7c6f609-f63a-4ec7-b71e-6734c4c9d231&experience=power-bi"

# Run any CLI command which will trigger authentication if auth.json is missing
python -m Backend.cli --url-a "https://app.powerbi.com/..." --url-b "https://app.powerbi.com/..." --storage-state auth.json
```

### Issue: "Playwright browsers not installed"
**Solution:**
```powershell
playwright install chromium
# or install all browsers
playwright install
```

### Issue: "GOOGLE_APPLICATION_CREDENTIALS not set"
**Solution:**
```powershell
# Download credentials from Google Cloud Console
# https://console.cloud.google.com/apis/credentials

$env:GOOGLE_APPLICATION_CREDENTIALS = "C:\Users\VijayBattini\credentials.json"
$env:GOOGLE_CLOUD_PROJECT = "your-project-id"

# Verify by running a simple test
python -c "from google.adk.vertex_ai import configure; print('Google Cloud configured successfully')"
```

### Issue: "Playwright timeout" or "Dashboard not loading"
**Solution:**
```powershell
# Increase timeout and settle time
python -m Backend.cli `
    --url-a "..." `
    --url-b "..." `
    --settle-seconds 5 `  # Increase from default 2
    --validation-scenarios
```

### Issue: API returns 503 "authentication_ready: false"
**Solution:**
```powershell
# The API needs a valid auth.json file
# Create it first:
$env:POWERBI_HOME_URL = "https://app.powerbi.com/home?ctid=e7c6f609-f63a-4ec7-b71e-6734c4c9d231&experience=power-bi"

# Then make a test CLI call to ensure auth.json exists:
python -m Backend.cli --url-a "test" --url-b "test" 2>&1
```

---

## Next Steps

1. **Verify Setup:**
   ```powershell
   # Test basic connectivity
   python -c "from Backend.browser import _capture_browser_session; print('Import successful')"
   ```

2. **Test Validation Scenarios:**
   ```powershell
   # Create a test run with one scenario
   python -m Backend.cli `
       --url-a "<dashboard-a-url>" `
       --url-b "<dashboard-b-url>" `
       --output-dir output/test_single `
       --storage-state auth.json `
       --validation-scenarios `
       --scenario 01
   ```

3. **Monitor Progress:**
   - Check `output/test_single/operation_timings.jsonl` for detailed timings
   - Review `output/test_single/validation_summary.json` for results

4. **Scale Up:**
   - Run all five scenarios
   - Test batch processing via API
   - Integrate into CI/CD pipeline

---

## Common Commands Quick Reference

```powershell
# Activate environment
.\venv\Scripts\Activate.ps1

# Start API server
uvicorn Backend.api:app --host 127.0.0.1 --port 8001 --reload

# Run CLI with all five scenarios
python -m Backend.cli --url-a <url-a> --url-b <url-b> --storage-state auth.json --validation-scenarios

# Run specific scenario
python -m Backend.cli --url-a <url-a> --url-b <url-b> --storage-state auth.json --validation-scenarios --scenario 02

# Run scenarios in batch (e.g., 02, 03, 04)
python -m Backend.cli --url-a <url-a> --url-b <url-b> --storage-state auth.json --validation-scenarios --scenario 02 --through 04

# View API documentation
# Navigate to: http://127.0.0.1:8001/docs
```

---

## Integration Notes

The **five validation scenarios** are already integrated into:

1. **API (`Backend/api.py`)**:
   - `CompareRequest.run_validation_scenarios: bool = False`
   - Endpoints: `POST /api/compare`, `POST /api/batches`
   - Results: `/api/jobs/{job_id}/validation-summary.json`

2. **CLI (`Backend/cli.py`)**:
   - `--validation-scenarios` flag
   - `--scenario` to run specific scenario (01-05)
   - `--through` to run range of scenarios

3. **Core Implementation (`Backend/validation_scenarios.py`)**:
   - `run_validation_scenarios()` async function
   - All five scenarios in `SCENARIO_DIRECTORIES`
   - Each scenario has isolated page-pair execution

No additional integration needed—just set up your environment and run!
