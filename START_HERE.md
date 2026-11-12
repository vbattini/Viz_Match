# 🚀 Power BI Dashboard Comparator - START HERE

Welcome! This guide will get you up and running with **Five Validation Scenarios** in 5 minutes.

---

## What Are You Getting?

A complete, production-ready system to:
- ✅ Compare two Power BI dashboards
- ✅ Run 5 automated validation scenarios
- ✅ Generate detailed comparison reports
- ✅ Use AI (Gemini) for intelligent analysis
- ✅ Process via Web API or CLI

### The Five Scenarios:
1. **Initial State** - Baseline comparison
2. **Single Filter** - Test one filter
3. **Multiple Filters** - Test up to 2 filters
4. **Drilldown** - Test hierarchy navigation
5. **Table Headers** - AI-powered header comparison

---

## 🏃 Quick Start (5 Minutes)

### Step 1: Activate Environment
```powershell
cd C:\Users\VijayBattini\Pictures\test3
.\venv\Scripts\Activate.ps1
```

### Step 2: Install (First Time Only)
```powershell
pip install -e .
playwright install chromium
```

### Step 3: Set Environment Variables
```powershell
# Power BI
$env:POWERBI_HOME_URL = "https://app.powerbi.com/home?ctid=e7c6f609-f63a-4ec7-b71e-6734c4c9d231&experience=power-bi"

# Google Cloud (required for Gemini AI)
$env:GOOGLE_CLOUD_PROJECT = "your-gcp-project-id"
$env:GOOGLE_APPLICATION_CREDENTIALS = "C:\path\to\credentials.json"
```

### Step 4: Start API Server
```powershell
uvicorn Backend.api:app --host 127.0.0.1 --port 8001 --reload
```

### Step 5: Run Comparison (In Another PowerShell Window)
```powershell
# Activate venv first
.\venv\Scripts\Activate.ps1

# Then send request
$body = @{
    workspace_name = "Your Workspace"
    semantic_model = "Your Model"
    report_name = "Your Report"
    run_validation_scenarios = $true
} | ConvertTo-Json

Invoke-WebRequest -Uri "http://127.0.0.1:8001/api/compare" `
    -Method POST `
    -Headers @{"Content-Type"="application/json"} `
    -Body $body
```

**Done!** Results will be in: `output/jobs/<report_name>_<job_id>/`

---

## 📚 Documentation Guide

Pick what you need:

### For Beginners
👉 **[QUICK_START.md](QUICK_START.md)** (5 min read)
- Basic commands
- API examples
- Common issues

### For Complete Setup
👉 **[LOCAL_SETUP_GUIDE.md](LOCAL_SETUP_GUIDE.md)** (30 min read)
- Environment setup
- Authentication
- Troubleshooting
- Advanced options

### For API Usage
👉 **[API_QUICK_REFERENCE.md](API_QUICK_REFERENCE.md)** (10 min read)
- All endpoints
- PowerShell examples
- Batch processing
- Error handling

### For Technical Details
👉 **[INTEGRATION_GUIDE.md](INTEGRATION_GUIDE.md)** (20 min read)
- How scenarios work
- Code architecture
- Customization
- Performance tuning

### Overview
👉 **[README_FIVE_SCENARIOS.md](README_FIVE_SCENARIOS.md)**
- Complete reference

### Plain Text Quick Reference
👉 **[GETTING_STARTED.txt](GETTING_STARTED.txt)**
- Printable quick reference

---

## 🎯 Choose Your Path

### I Want to Use the API
```powershell
# 1. Start server (see Quick Start above)
uvicorn Backend.api:app --host 127.0.0.1 --port 8001

# 2. Visit API docs
# http://127.0.0.1:8001/docs

# 3. Read API guide
# → API_QUICK_REFERENCE.md
```

### I Want to Use the CLI
```powershell
# Activate venv
.\venv\Scripts\Activate.ps1

# Run comparison with all 5 scenarios
python -m Backend.cli `
    --url-a "https://app.powerbi.com/..." `
    --url-b "https://app.powerbi.com/..." `
    --storage-state auth.json `
    --validation-scenarios

# Read CLI guide
# → LOCAL_SETUP_GUIDE.md (section: "Option 2: Command-Line Interface")
```

### I Want to Run Just One Scenario
```powershell
# Run only scenario 01 (initial state)
python -m Backend.cli `
    --url-a "https://..." `
    --url-b "https://..." `
    --storage-state auth.json `
    --validation-scenarios `
    --scenario 01
```

### I Want Batch Processing
```powershell
# Submit 3 comparisons via API
# → See API_QUICK_REFERENCE.md (section: "Batch Processing")
```

### I Want Automated Setup
```powershell
# Run setup script (sets up environment and starts server)
.\setup_and_run.ps1 -Mode api -TenantId "e7c6f609-f63a-4ec7-b71e-6734c4c9d231" -GoogleCloudProject "your-project"
```

---

## ⚙️ Prerequisites Checklist

- [ ] Python 3.10+ installed
- [ ] Virtual environment exists (`venv/`)
- [ ] Dependencies installed (`pip install -e .`)
- [ ] Playwright browsers installed (`playwright install chromium`)
- [ ] Power BI credentials (auth.json will be auto-created)
- [ ] Google Cloud project ID
- [ ] Google Cloud credentials (service account JSON)

---

## 🔧 Common First-Time Tasks

### Create auth.json (Power BI Authentication)
```powershell
# The first time you run a command, it will prompt for Power BI login
# This creates auth.json automatically

# Or manually trigger:
python -m Backend.cli --url-a "test" --url-b "test"
```

### Set Google Cloud Credentials
```powershell
# Download from Google Cloud Console:
# https://console.cloud.google.com/apis/credentials

$env:GOOGLE_APPLICATION_CREDENTIALS = "C:\path\to\service-account-key.json"
$env:GOOGLE_CLOUD_PROJECT = "your-project-id"
```

### Test API is Working
```powershell
# Visit in browser:
# http://127.0.0.1:8001/api/config

# Should show:
# {
#   "project_id": "your-project",
#   "authentication_ready": true
# }
```

---

## 📊 Expected Output

After running scenarios, you'll get:

```
output/jobs/
└── <job_id>/
    ├── dashboard_A/<page>/         (screenshots + evidence manifest)
    ├── dashboard_B/<page>/         (screenshots + evidence manifest)
    ├── planning/scenario_evidence/ (execution, DOM, and status JSON)
    ├── validation_summary.json
    ├── Dashboard_Comparison.docx
    ├── Dashboard_Comparison.xlsx
    └── operation_timings.jsonl
```

The existing DOCX and Excel reports contain the scenario comparisons and
screenshots; there is no separate scenario-comparison report.

---

## 🆘 Troubleshooting

### Problem: "auth.json not found"
**Solution:** Run any CLI command once to auto-generate it.

### Problem: "ModuleNotFoundError"
**Solution:** Activate venv: `.\venv\Scripts\Activate.ps1`

### Problem: "authentication_ready: false" in API
**Solution:** Create auth.json first by running a CLI command.

### Problem: Scenarios timeout
**Solution:** Increase wait time: `--settle-seconds 5` (CLI)

### Problem: Google API error
**Solution:** Check `GOOGLE_CLOUD_PROJECT` and `GOOGLE_APPLICATION_CREDENTIALS` are set.

**For more help:** See troubleshooting section in [LOCAL_SETUP_GUIDE.md](LOCAL_SETUP_GUIDE.md)

---

## 🚀 Next Steps After First Run

### 1. Review Results
```powershell
# Check validation summary
cat output/jobs/*/validation_summary.json | ConvertFrom-Json | Format-List
```

### 2. Download Reports
```powershell
# API provides auto-generated reports:
# - Dashboard_Comparison.docx
# - Dashboard_Comparison.xlsx
# Scenario screenshots and comparison results are included in these reports.
```

### 3. Try Different Scenarios
```powershell
# Run only scenarios 02-04
python -m Backend.cli --url-a "..." --url-b "..." --validation-scenarios --scenario 02 --through 04
```

### 4. Batch Multiple Dashboards
```powershell
# See: API_QUICK_REFERENCE.md (section: "Batch Processing")
```

### 5. Integrate Into Workflow
```powershell
# Schedule regular comparisons
# Add to CI/CD pipeline
# See: INTEGRATION_GUIDE.md
```

---

## 📖 Documentation Files Summary

| File | Purpose | Read Time |
|------|---------|-----------|
| **START_HERE.md** (this file) | Overview & quick start | 5 min |
| **QUICK_START.md** | Quick reference & commands | 5 min |
| **LOCAL_SETUP_GUIDE.md** | Complete setup instructions | 30 min |
| **API_QUICK_REFERENCE.md** | API endpoints & examples | 10 min |
| **INTEGRATION_GUIDE.md** | Technical deep dive | 20 min |
| **README_FIVE_SCENARIOS.md** | Comprehensive reference | 15 min |
| **GETTING_STARTED.txt** | Plain text reference | 10 min |
| **setup_and_run.ps1** | Automated setup script | - |

---

## 💡 Pro Tips

1. **Always activate venv first:**
   ```powershell
   .\venv\Scripts\Activate.ps1
   ```

2. **See browser during testing:**
   ```powershell
   python -m Backend.cli --validation-scenarios --headed
   ```

3. **Run specific scenario for testing:**
   ```powershell
   python -m Backend.cli --validation-scenarios --scenario 01
   ```

4. **Check results in real-time:**
   ```powershell
   Get-Content output/jobs/*/operation_timings.jsonl -Tail 5 -Wait
   ```

5. **Use API docs for exploration:**
   Visit http://127.0.0.1:8001/docs when server is running

---

## 🎓 Learning Path

**Absolute Beginner:**
1. Read this file (START_HERE.md)
2. Follow Quick Start section
3. Read QUICK_START.md

**Want to Use API:**
1. Follow Quick Start → API setup
2. Read API_QUICK_REFERENCE.md
3. Try examples from http://127.0.0.1:8001/docs

**Want to Use CLI:**
1. Follow Quick Start → CLI setup
2. Read LOCAL_SETUP_GUIDE.md (CLI section)
3. Try different scenario flags

**Want Technical Understanding:**
1. Read INTEGRATION_GUIDE.md
2. Review Backend/validation_scenarios.py code
3. Read comments in Backend/api.py

---

## ❓ FAQ

**Q: Do I need to set up Google Cloud?**
A: Yes, for the AI-powered header comparison (scenario 05). Other scenarios work without it.

**Q: Can I run just one scenario?**
A: Yes, use `--scenario 01` flag or set `last: "01"` in API request.

**Q: How long does it take?**
A: ~50-60 seconds per page for all 5 scenarios. Per-scenario: 3-15 seconds.

**Q: Can I run multiple dashboards?**
A: Yes, use batch processing. See API_QUICK_REFERENCE.md

**Q: Can I see the browser during testing?**
A: Yes, add `--headed` flag to CLI or use headed mode in API.

**Q: Where are the results?**
A: `output/jobs/<report_name>_<job_id>/` with `validation_summary.json` as main file.

**Q: Can I customize the scenarios?**
A: Yes, see INTEGRATION_GUIDE.md for customization examples.

**Q: How do I integrate into CI/CD?**
A: See INTEGRATION_GUIDE.md (section: "Integration with CI/CD").

---

## 📞 Getting Help

1. **Check QUICK_START.md** - Most common issues solved there
2. **Check LOCAL_SETUP_GUIDE.md** - Complete troubleshooting section
3. **Check API_QUICK_REFERENCE.md** - Error handling section
4. **Check INTEGRATION_GUIDE.md** - Debugging section
5. **Visit http://127.0.0.1:8001/docs** - Interactive API documentation

---

## ✅ What's Included

- ✅ Five validation scenarios fully implemented and integrated
- ✅ FastAPI web server with endpoints for comparison
- ✅ Command-line interface for direct usage
- ✅ Batch processing support
- ✅ AI-powered analysis with Gemini
- ✅ Automated screenshot capture
- ✅ Performance monitoring
- ✅ Detailed JSON output
- ✅ Auto-generated Word and Excel reports

---

## 🎯 You're Ready!

Everything you need is:
1. **In the code** - Already integrated and working
2. **In the documentation** - Pick what you need
3. **In the output** - Results will be ready after running

**Next: Follow the Quick Start section above!**

---

<br>

---

**Need more details?**
- 📖 [QUICK_START.md](QUICK_START.md) - 5-minute quick reference
- 🔌 [API_QUICK_REFERENCE.md](API_QUICK_REFERENCE.md) - All API endpoints
- 📚 [LOCAL_SETUP_GUIDE.md](LOCAL_SETUP_GUIDE.md) - Complete setup guide
- 🔧 [INTEGRATION_GUIDE.md](INTEGRATION_GUIDE.md) - Technical deep dive

**Happy testing! 🚀**
