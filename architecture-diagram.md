# Power BI Dashboard Comparator — Architecture Chart

This is the project architecture diagram. The rendered version is available
as [architecture-diagram.svg](./architecture-diagram.svg).

```mermaid
flowchart TB
    User[User] --> UI[Frontend UI<br/>Frontend/index.html<br/>static/app.js<br/>static/styles.css]
    User --> CLI[CLI<br/>Backend/cli.py]

    UI --> API[FastAPI API<br/>Backend/api.py]
    API --> Jobs[Job Manager<br/>status, progress, retention]
    CLI --> Workflow
    Jobs --> Workflow

    Workflow[ADK Sequential Workflow<br/>Backend/workflow.py]
    Workflow --> Capture[Capture]
    Workflow --> Validate[Validate]
    Workflow --> Compare[Compare]
    Workflow --> Reports[Report]

    Capture --> Discovery[Power BI Discovery<br/>Backend/discovery.py]
    Capture --> Browser[Playwright Capture Engine<br/>Backend/browser.py]
    Discovery --> Browser

    Browser --> Navigation[Navigation]
    Browser --> Ready[Render Readiness]
    Browser --> Inventory[DOM and Accessibility Inventory]
    Browser --> Filters[Filter Planning and Selection]
    Browser --> Screenshots[Screenshot Capture]
    Browser --> Exports[Visual and Hierarchy Exports]

    Navigation --> Base[Dashboard A<br/>Snowflake / Base]
    Navigation --> Target[Dashboard B<br/>GCP / Compare]
    Browser -. authenticated storage state .-> Auth[Playwright Auth State]

    Screenshots --> AI[Gemini / Vertex AI<br/>Backend/agents.py]
    Inventory --> AI
    AI --> Extraction[Visual title, type,<br/>rendering and bounds]
    AI --> Retry[Schema validation<br/>retry and backoff]

    Validate --> Scenarios[Functional Scenarios<br/>Backend/validation_scenarios.py]
    Scenarios --> S01[Scenario 01<br/>Table/header + hierarchy]
    Scenarios --> S02[Scenario 02<br/>Single filter]
    Scenarios --> S03[Scenario 03<br/>Multiple filters]

    S01 --> Hierarchy[Hierarchy sequence<br/>Value cell → Drill Up → Expand All<br/>→ screenshot/export → Next Level screenshot]
    Hierarchy --> Screenshots
    Hierarchy --> Exports
    S01 --> Evidence[Evidence manifests<br/>Backend/scenario_evidence.py]
    S02 --> Evidence
    S03 --> Evidence
    Evidence --> ScenarioCompare[Scenario comparison<br/>Backend/scenario_comparison.py]

    AI --> Comparator[Deterministic Comparator<br/>Backend/comparison.py]
    Browser --> Comparator
    Compare --> Comparator
    Comparator --> VisualFilter[Visual/filter comparison]
    Comparator --> Alignment[State alignment and<br/>PASS / FAIL / UNVERIFIED]
    ScenarioCompare --> Alignment

    Reports --> StandardReports[Standard report writers]
    VisualFilter --> StandardReports
    Alignment --> StandardReports
    StandardReports --> JSON[JSON evidence]
    StandardReports --> XLSX[Excel workbook]
    StandardReports --> DOCX[DOCX report]
    Screenshots --> ImageFiles[Screenshots]
    Exports --> HierarchyFiles[Hierarchy Excel evidence]
    Workflow --> Timings[Timing and performance JSONL]

    JSON --> Output[(output/jobs/{job_id})]
    XLSX --> Output
    DOCX --> Output
    ImageFiles --> Output
    HierarchyFiles --> Output
    Timings --> Output
    Output --> API
    API --> UI

    AI -.-> Vertex[Google Vertex AI]

    classDef layer fill:#eef5fb,stroke:#4779a8,color:#102a43;
    classDef external fill:#fff5e3,stroke:#c58a2b,color:#4a2c00;
    classDef output fill:#eaf8ef,stroke:#4a9b69,color:#164a2b;

    class UI,CLI,API,Jobs,Workflow,Capture,Validate,Compare,Reports,Discovery,Browser,Navigation,Ready,Inventory,Filters,Screenshots,Exports,AI,Extraction,Retry,Scenarios,S01,S02,S03,Hierarchy,Evidence,ScenarioCompare,Comparator,VisualFilter,Alignment,StandardReports layer;
    class Base,Target,Auth,Vertex external;
    class JSON,XLSX,DOCX,ImageFiles,HierarchyFiles,Timings,Output output;
```

## Main flow

```text
User
  -> Frontend or CLI
  -> FastAPI job manager
  -> ADK sequential workflow
  -> Power BI discovery and Playwright capture
  -> AI evidence enrichment
  -> deterministic comparison
  -> JSON / Excel / DOCX / screenshot evidence
  -> output/jobs/{job_id}
  -> frontend download links
```
