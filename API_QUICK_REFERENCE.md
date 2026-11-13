# API Quick Reference - Power BI Dashboard Comparator

## Start the API

```powershell
# Navigate to project
cd C:\Users\VijayBattini\Pictures\test3

# Activate environment
.\venv\Scripts\Activate.ps1

# Set environment variables
$env:POWERBI_HOME_URL = "https://app.powerbi.com/home?ctid=e7c6f609-f63a-4ec7-b71e-6734c4c9d231&experience=power-bi"
$env:GOOGLE_CLOUD_PROJECT = "your-gcp-project-id"
$env:GOOGLE_APPLICATION_CREDENTIALS = "C:\path\to\credentials.json"

# Start API server
uvicorn Backend.api:app --host 127.0.0.1 --port 8001 --reload

# Output:
# Uvicorn running on http://127.0.0.1:8001
# Press CTRL+C to quit
```

## API Endpoints

### 1. Compare Two Dashboards

**POST** `/api/compare`

Start a comparison job with optional validation scenarios.

**Request:**
```powershell
$body = @{
    workspace_name = "Sales Analytics"
    semantic_model = "Sales Model"  # Optional
    report_name = "Sales Dashboard"
    run_validation_scenarios = $true  # Enable five scenarios
} | ConvertTo-Json

Invoke-WebRequest -Uri "http://127.0.0.1:8001/api/compare" `
    -Method POST `
    -Headers @{"Content-Type"="application/json"} `
    -Body $body
```

**Response:**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "queued"
}
```

### 2. Get Job Status

**GET** `/api/jobs/{job_id}`

Check job progress and status.

```powershell
$job_id = "550e8400-e29b-41d4-a716-446655440000"

$job = Invoke-WebRequest -Uri "http://127.0.0.1:8001/api/jobs/$job_id" -Method GET | ConvertFrom-Json

$job
# Output:
# status: "in_progress"
# progress: [
#   {step: "Validate inputs", status: "completed"}
#   {step: "Discover Dashboard A workspace", status: "completed"}
#   {step: "Discover Dashboard A report", status: "completed"}
#   ...
#   {step: "Run five functional scenarios", status: "in_progress"}
# ]
```

### 3. Get Validation Summary

**GET** `/api/jobs/{job_id}/validation-summary.json`

Get results of the five scenarios.

```powershell
$summary = Invoke-WebRequest -Uri "http://127.0.0.1:8001/api/jobs/$job_id/validation-summary.json" | ConvertFrom-Json

# Shows:
# - All five scenarios results
# - Pages compared
# - Pass/fail status
# - Screenshots and inventory data
```

### 4. Get Comparison JSON

**GET** `/api/jobs/{job_id}/comparison.json`

Full comparison data.

```powershell
$comparison = Invoke-WebRequest -Uri "http://127.0.0.1:8001/api/jobs/$job_id/comparison.json" | ConvertFrom-Json
```

### 5. Download Reports

**GET** `/api/jobs/{job_id}/Dashboard_Comparison.docx`

Download Word report.

```powershell
Invoke-WebRequest -Uri "http://127.0.0.1:8001/api/jobs/$job_id/Dashboard_Comparison.docx" `
    -OutFile "C:\Downloads\report.docx"
```

**GET** `/api/jobs/{job_id}/Dashboard_Comparison.xlsx`

Download Excel report.

```powershell
Invoke-WebRequest -Uri "http://127.0.0.1:8001/api/jobs/$job_id/Dashboard_Comparison.xlsx" `
    -OutFile "C:\Downloads\report.xlsx"
```

When validation scenarios are enabled, their screenshots and comparisons are
included in the existing `Dashboard_Comparison.docx` and
`Dashboard_Comparison.xlsx`. There is no separate scenario-comparison report
endpoint.

### 6. Batch Processing

**POST** `/api/batches`

Submit multiple comparison jobs.

```powershell
$batch = @{
    jobs = @(
        @{
            workspace_name = "Sales"
            semantic_model = "Sales Model"
            report_name = "Sales Dashboard"
        },
        @{
            workspace_name = "Finance"
            semantic_model = "Finance Model"
            report_name = "Finance Dashboard"
        }
    )
    stop_on_first_failure = $false
    run_validation_scenarios = $true
} | ConvertTo-Json -Depth 10

$batch_result = Invoke-WebRequest -Uri "http://127.0.0.1:8001/api/batches" `
    -Method POST `
    -Headers @{"Content-Type"="application/json"} `
    -Body $batch

$batch_result | ConvertFrom-Json
# Output: {"batch_id": "uuid", "status": "queued"}
```

**GET** `/api/batches/{batch_id}`

Get batch status and individual job results.

```powershell
$batch_status = Invoke-WebRequest -Uri "http://127.0.0.1:8001/api/batches/$batch_id" | ConvertFrom-Json
```

### 7. Get API Configuration

**GET** `/api/config`

Check API configuration and authentication status.

```powershell
$config = Invoke-WebRequest -Uri "http://127.0.0.1:8001/api/config" | ConvertFrom-Json

$config
# Output:
# project_id: "your-gcp-project"
# location: "us-central1"
# model: "gemini-2.0-flash"
# authentication_ready: true
```

---

## PowerShell Helper Functions

Add these to your PowerShell profile for easier API usage:

```powershell
# Start comparison job
function Start-PBIComparison {
    param(
        [Parameter(Mandatory=$true)]
        [string]$Workspace,
        
        [Parameter(Mandatory=$true)]
        [string]$Report,
        
        [string]$Model = "",
        [bool]$WithScenarios = $true,
        [string]$ApiUrl = "http://127.0.0.1:8001"
    )
    
    $body = @{
        workspace_name = $Workspace
        semantic_model = $Model
        report_name = $Report
        run_validation_scenarios = $WithScenarios
    } | ConvertTo-Json
    
    $result = Invoke-WebRequest -Uri "$ApiUrl/api/compare" `
        -Method POST `
        -Headers @{"Content-Type"="application/json"} `
        -Body $body
    
    return ($result.Content | ConvertFrom-Json).job_id
}

# Check job status
function Get-PBIJobStatus {
    param(
        [Parameter(Mandatory=$true)]
        [string]$JobId,
        
        [string]$ApiUrl = "http://127.0.0.1:8001"
    )
    
    return Invoke-WebRequest -Uri "$ApiUrl/api/jobs/$JobId" | ConvertFrom-Json
}

# Wait for job to complete
function Wait-PBIJob {
    param(
        [Parameter(Mandatory=$true)]
        [string]$JobId,
        
        [int]$CheckIntervalSeconds = 5,
        [string]$ApiUrl = "http://127.0.0.1:8001"
    )
    
    while ($true) {
        $job = Get-PBIJobStatus -JobId $JobId -ApiUrl $ApiUrl
        Write-Host "Status: $($job.status) - Elapsed: $($job.elapsed_seconds)s"
        
        if ($job.status -in @("completed", "failed")) {
            return $job
        }
        
        Start-Sleep -Seconds $CheckIntervalSeconds
    }
}

# Download results
function Get-PBIResults {
    param(
        [Parameter(Mandatory=$true)]
        [string]$JobId,
        
        [Parameter(Mandatory=$true)]
        [string]$OutputPath,
        
        [string]$ApiUrl = "http://127.0.0.1:8001"
    )
    
    # Download Word report
    Invoke-WebRequest -Uri "$ApiUrl/api/jobs/$JobId/Dashboard_Comparison.docx" `
        -OutFile "$OutputPath\comparison.docx"
    
    # Download Excel report
    Invoke-WebRequest -Uri "$ApiUrl/api/jobs/$JobId/Dashboard_Comparison.xlsx" `
        -OutFile "$OutputPath\comparison.xlsx"
    
    Write-Host "Results downloaded to $OutputPath"
}

# Full workflow
function Invoke-PBIComparison {
    param(
        [Parameter(Mandatory=$true)]
        [string]$Workspace,
        
        [Parameter(Mandatory=$true)]
        [string]$Report,
        
        [string]$Model = "",
        [string]$OutputPath = "$PWD\output",
        [string]$ApiUrl = "http://127.0.0.1:8001"
    )
    
    Write-Host "Starting comparison: $Workspace / $Report" -ForegroundColor Green
    
    $jobId = Start-PBIComparison -Workspace $Workspace -Report $Report -Model $Model -ApiUrl $ApiUrl
    Write-Host "Job ID: $jobId" -ForegroundColor Green
    
    Write-Host "Waiting for completion..." -ForegroundColor Yellow
    $result = Wait-PBIJob -JobId $jobId -ApiUrl $ApiUrl
    
    if ($result.status -eq "completed") {
        Write-Host "✓ Job completed successfully" -ForegroundColor Green
        
        # Ensure output directory exists
        if (-not (Test-Path $OutputPath)) {
            New-Item -ItemType Directory -Path $OutputPath | Out-Null
        }
        
        Get-PBIResults -JobId $jobId -OutputPath $OutputPath -ApiUrl $ApiUrl
    } else {
        Write-Host "✗ Job failed: $($result.error)" -ForegroundColor Red
    }
}

# Usage examples:
# $jobId = Start-PBIComparison -Workspace "Sales" -Report "Dashboard"
# Get-PBIJobStatus -JobId $jobId
# Wait-PBIJob -JobId $jobId
# Invoke-PBIComparison -Workspace "Sales" -Report "Dashboard" -OutputPath "C:\Reports"
```

---

## Example Workflows

### Complete Comparison with Scenarios

```powershell
# 1. Start API
Start-Process powershell -ArgumentList '-NoExit', '-Command', '
cd C:\Users\VijayBattini\Pictures\test3
.\venv\Scripts\Activate.ps1
uvicorn Backend.api:app --host 127.0.0.1 --port 8001
'

# 2. In another PowerShell window
cd C:\Users\VijayBattini\Pictures\test3
.\venv\Scripts\Activate.ps1

# 3. Run comparison
$jobId = (Invoke-WebRequest -Uri "http://127.0.0.1:8001/api/compare" `
    -Method POST `
    -Headers @{"Content-Type"="application/json"} `
    -Body (@{
        workspace_name = "Your Workspace"
        semantic_model = "Your Model"
        report_name = "Your Report"
        run_validation_scenarios = $true
    } | ConvertTo-Json)).Content | ConvertFrom-Json | Select-Object -ExpandProperty job_id

# 4. Monitor progress
$job = Invoke-WebRequest -Uri "http://127.0.0.1:8001/api/jobs/$jobId" | ConvertFrom-Json
$job.progress | Format-Table

# 5. Get results
$summary = Invoke-WebRequest -Uri "http://127.0.0.1:8001/api/jobs/$jobId/validation-summary.json" | ConvertFrom-Json
$summary.pages | Format-Table
```

### Batch Processing

```powershell
# Submit batch of 3 comparisons
$batch = @{
    jobs = @(
        @{ workspace_name="WS1"; report_name="Report1" },
        @{ workspace_name="WS2"; report_name="Report2" },
        @{ workspace_name="WS3"; report_name="Report3" }
    )
    stop_on_first_failure = $false
    run_validation_scenarios = $true
} | ConvertTo-Json -Depth 10

$batchId = (Invoke-WebRequest -Uri "http://127.0.0.1:8001/api/batches" `
    -Method POST `
    -Headers @{"Content-Type"="application/json"} `
    -Body $batch).Content | ConvertFrom-Json | Select-Object -ExpandProperty batch_id

# Monitor batch
while ($true) {
    $batch_status = Invoke-WebRequest -Uri "http://127.0.0.1:8001/api/batches/$batchId" | ConvertFrom-Json
    Write-Host "Batch Status: $($batch_status.status)"
    $batch_status.jobs | ForEach-Object { Write-Host "  Job $($_.number): $($_.status)" }
    
    if ($batch_status.status -in @("completed", "failed")) { break }
    Start-Sleep -Seconds 10
}

# Download results
Invoke-WebRequest -Uri "http://127.0.0.1:8001/api/batches/$batchId/outputs.zip" `
    -OutFile "batch_results.zip"
```

---

## Error Handling

### Common API Errors

```powershell
# 503 - Authentication not ready
# Solution: Create auth.json file
$env:POWERBI_HOME_URL = "https://app.powerbi.com/home?ctid=e7c6f609-f63a-4ec7-b71e-6734c4c9d231&experience=power-bi"
python -m Backend.cli --url-a test --url-b test

# 409 - Another comparison already running
# Solution: Wait for previous job to complete
Get-PBIJobStatus -JobId $previousJobId

# 422 - Invalid request
# Solution: Check workspace/report names exist

# 500 - Server error
# Solution: Check API console for detailed error
# Re-run with --headed to debug
```

---

## See Also

- **[LOCAL_SETUP_GUIDE.md](LOCAL_SETUP_GUIDE.md)** - Full setup instructions
- **[QUICK_START.md](QUICK_START.md)** - Quick reference
- **[INTEGRATION_GUIDE.md](INTEGRATION_GUIDE.md)** - Detailed scenario documentation
- **API Docs:** http://127.0.0.1:8001/docs (when API is running)
