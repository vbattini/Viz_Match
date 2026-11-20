(() => {
  const MAX_JOBS = 10;
  const fields = [
    "workspace_name",
    "semantic_model",
    "report_name",
    "url_a",
    "url_b"
  ];
  const fieldLabels = {
    workspace_name: "Workspace Name",
    semantic_model: "Semantic Model",
    report_name: "Report Name",
    url_a: "Base Report URL",
    url_b: "GCP Report URL"
  };
  const rowsBody = document.querySelector("#batch-rows");
  const grid = document.querySelector("#batch-grid");
  const counter = document.querySelector("#job-counter");
  const dashboardColumns = document.querySelector(".dashboard-columns");
  const configurationContent = document.querySelector("#configuration-content");
  const configurationToggle = document.querySelector("#config-toggle");
  const configurationToggleIcon = document.querySelector("#config-toggle-icon");
  const configurationToggleLabel = document.querySelector("#config-toggle-label");
  const resultsColumn = document.querySelector(".results-column");
  const validationMessages = document.querySelector("#validation-messages");
  const statusBox = document.querySelector("#status");
  const runButton = document.querySelector("#run-batch");
  const runValidationScenarios = document.querySelector("#run-validation-scenarios");
  const batchJobRows = document.querySelector("#batch-job-rows");
  const batchOverallProgress = document.querySelector("#batch-overall-progress");
  const currentJobLabel = document.querySelector("#current-job-label");
  const currentStageLabel = document.querySelector("#current-stage-label");
  const currentStageStatus = document.querySelector("#current-stage-status");
  const batchProgressFill = document.querySelector("#batch-progress-fill");
  const progressTrack = batchProgressFill.parentElement;
  const folderDownload = document.querySelector("#batch-folder-download");
  let rowSequence = 0;
  let rows = Array.from({ length: MAX_JOBS }, () => newRow());
  let activeCell = { rowId: rows[0].id, field: fields[0] };
  let selectedRows = new Set();
  let validationRequested = false;
  let pollTimer = null;
  let elapsedTimer = null;
  let batchId = null;
  let batchStartedAt = 0;
  let batchElapsed = 0;
  let batchRunning = false;

  function newRow(values = {}) {
    rowSequence += 1;
    return {
      id: `row-${rowSequence}`,
      workspace_name: values.workspace_name || "",
      semantic_model: values.semantic_model || "",
      report_name: values.report_name || "",
      url_a: values.url_a || "",
      url_b: values.url_b || ""
    };
  }

  function emptyRows() {
    return Array.from({ length: MAX_JOBS }, () => newRow());
  }

  function setConfigurationExpanded(expanded) {
    configurationContent.hidden = !expanded;
    dashboardColumns.classList.toggle("configuration-collapsed", !expanded);
    configurationToggle.setAttribute("aria-expanded", String(expanded));
    configurationToggleIcon.textContent = expanded ? "⌃" : "⌄";
    configurationToggleLabel.textContent = expanded ? "Hide input sheet" : "Show input sheet";
  }

  function refreshActiveCell() {
    if (!rows.some(row => row.id === activeCell.rowId)) {
      activeCell = { rowId: rows[0].id, field: fields[0] };
    }
    rowsBody.querySelectorAll("input[data-row-id][data-field]").forEach(input => {
      input.classList.toggle(
        "active-cell",
        input.dataset.rowId === activeCell.rowId &&
          input.dataset.field === activeCell.field
      );
    });
  }

  function setActiveCell(rowId, field) {
    if (!fields.includes(field) || !rows.some(row => row.id === rowId)) return;
    activeCell = { rowId, field };
    refreshActiveCell();
  }

  function valueOf(row, field) {
    return String(row[field] || "").trim();
  }

  function isNonEmpty(row) {
    return fields.some(field => valueOf(row, field));
  }

  function updateCounter() {
    const count = rows.filter(isNonEmpty).length;
    counter.textContent = `${count} of ${MAX_JOBS} jobs added`;
  }

  function collectValidation() {
    const jobs = [];
    const errors = [];
    const warnings = [];
    const rowErrors = new Map();
    const duplicateRows = new Set();
    const firstOccurrence = new Map();

    rows.forEach((row, index) => {
      if (!isNonEmpty(row)) return;
      const job = Object.fromEntries(
        fields.map(field => [field, valueOf(row, field)])
      );
      jobs.push(job);
      const urlMode = Boolean(job.url_a || job.url_b);
      const requiredFields = urlMode
        ? ["url_a", "url_b"]
        : ["workspace_name", "semantic_model", "report_name"];
      const missing = requiredFields.filter(field => !job[field]);
      if (missing.length) {
        const messages = missing.map(field => `${fieldLabels[field]} is required.`);
        rowErrors.set(row.id, messages);
        errors.push(`Row ${index + 1}: ${messages.join(" ")}`);
      }
      if (urlMode && (job.url_a || job.url_b)) {
        for (const field of ["url_a", "url_b"]) {
          if (job[field] && !/^https:\/\/app\.powerbi\.com\//i.test(job[field])) {
            const message = `${fieldLabels[field]} must be an HTTPS app.powerbi.com URL.`;
            rowErrors.set(row.id, [...(rowErrors.get(row.id) || []), message]);
            errors.push(`Row ${index + 1}: ${message}`);
          }
        }
      }
      const key = fields.map(field => job[field].toLocaleLowerCase()).join("\u0000");
      if (firstOccurrence.has(key)) {
        const originalRow = firstOccurrence.get(key);
        duplicateRows.add(row.id);
        duplicateRows.add(originalRow.id);
        warnings.push(`Row ${index + 1} duplicates row ${originalRow.index + 1}.`);
      } else {
        firstOccurrence.set(key, { id: row.id, index });
      }
    });

    if (jobs.length > MAX_JOBS) {
      errors.push(`Only ${MAX_JOBS} non-empty jobs are allowed; remove ${jobs.length - MAX_JOBS}.`);
    }
    if (jobs.length === 0) errors.push("Add at least one job.");
    return { jobs, errors, warnings, rowErrors, duplicateRows };
  }

  function renderValidation(showRequired = validationRequested) {
    const validation = collectValidation();
    validationMessages.replaceChildren();
    const messages = [
      ...(showRequired ? validation.errors : []),
      ...validation.warnings
    ];
    if (messages.length) {
      const list = document.createElement("ul");
      messages.forEach(message => {
        const item = document.createElement("li");
        item.textContent = message;
        list.append(item);
      });
      validationMessages.append(list);
    }
    rowsBody.querySelectorAll("tr[data-row-id]").forEach(rowElement => {
      const missing = new Set(validation.rowErrors.get(rowElement.dataset.rowId) || []);
      rowElement.querySelectorAll("input[data-field]").forEach(input => {
        const label = `${fieldLabels[input.dataset.field]} is required.`;
        input.classList.toggle("invalid", showRequired && missing.has(label));
      });
      rowElement.dataset.duplicate =
        validation.duplicateRows.has(rowElement.dataset.rowId) ? "true" : "false";
    });
    updateCounter();
    return validation;
  }

  function renderGrid() {
    rowsBody.replaceChildren();
    rows.forEach((row, rowIndex) => {
      const tr = document.createElement("tr");
      tr.dataset.rowId = row.id;
      tr.setAttribute("aria-selected", selectedRows.has(row.id) ? "true" : "false");
      if (selectedRows.has(row.id)) tr.classList.add("selected");
      const rowNumber = document.createElement("th");
      rowNumber.scope = "row";
      rowNumber.className = "row-number";
      rowNumber.textContent = String(rowIndex + 1);
      tr.append(rowNumber);
      fields.forEach(field => {
        const td = document.createElement("td");
        const input = document.createElement("input");
        input.type = "text";
        input.autocomplete = "off";
        input.spellcheck = false;
        input.value = row[field];
        input.dataset.rowId = row.id;
        input.dataset.field = field;
        input.setAttribute("aria-label", `${fieldLabels[field]}, row ${rowIndex + 1}`);
        input.addEventListener("focus", () => setActiveCell(row.id, field));
        td.append(input);
        tr.append(td);
      });
      rowsBody.append(tr);
    });
    rowsBody.querySelectorAll("input").forEach(input => {
      input.disabled = batchRunning;
    });
    refreshActiveCell();
    renderValidation();
  }

  function selectRow(rowId, additive) {
    if (!additive) selectedRows.clear();
    if (additive && selectedRows.has(rowId)) selectedRows.delete(rowId);
    else selectedRows.add(rowId);
    rowsBody.querySelectorAll("tr[data-row-id]").forEach(row => {
      const selected = selectedRows.has(row.dataset.rowId);
      row.classList.toggle("selected", selected);
      row.setAttribute("aria-selected", selected ? "true" : "false");
    });
  }

  function parseClipboardGrid(text) {
    const matrix = [[]];
    let value = "";
    let quoted = false;

    for (let index = 0; index < text.length; index += 1) {
      const character = text[index];
      if (character === '"') {
        if (quoted && text[index + 1] === '"') {
          value += '"';
          index += 1;
        } else if (quoted) {
          quoted = false;
        } else if (!value) {
          quoted = true;
        } else {
          value += character;
        }
      } else if (!quoted && character === "\t") {
        matrix[matrix.length - 1].push(value.trim());
        value = "";
      } else if (!quoted && (character === "\r" || character === "\n")) {
        matrix[matrix.length - 1].push(value.trim());
        value = "";
        if (character === "\r" && text[index + 1] === "\n") index += 1;
        matrix.push([]);
      } else {
        value += character;
      }
    }

    matrix[matrix.length - 1].push(value.trim());
    const finalRow = matrix[matrix.length - 1];
    if (
      (text.endsWith("\n") || text.endsWith("\r")) &&
      finalRow.length === 1 &&
      !finalRow[0]
    ) {
      matrix.pop();
    }
    return matrix;
  }

  function pasteClipboardGrid(event) {
    const input = event.target.closest("input[data-row-id][data-field]");
    const text = event.clipboardData?.getData("text/plain") || "";
    if (!input || batchRunning || !/[\t\r\n]/.test(text)) return;

    event.preventDefault();
    const matrix = parseClipboardGrid(text);
    const startRow = rows.findIndex(row => row.id === input.dataset.rowId);
    const startColumn = fields.indexOf(input.dataset.field);
    if (startRow < 0 || startColumn < 0) return;

    if (matrix.some(values => values.length > fields.length - startColumn)) {
      showStatus(
        "The pasted range extends beyond the remaining worksheet columns. Start in Workspace Name or paste fewer columns.",
        true
      );
      return;
    }
    if (startRow + matrix.length > rows.length) {
      showStatus(
        "The pasted range extends beyond the worksheet rows. Add rows before pasting the remaining data.",
        true
      );
      return;
    }

    const candidateRows = rows.map(row => ({ ...row }));
    matrix.forEach((values, rowOffset) => {
      values.forEach((value, columnOffset) => {
        candidateRows[startRow + rowOffset][fields[startColumn + columnOffset]] = value;
      });
    });
    if (candidateRows.filter(isNonEmpty).length > MAX_JOBS) {
      showStatus(
        `Pasting these values would exceed the ${MAX_JOBS}-job limit. Remove a job before pasting.`,
        true
      );
      return;
    }

    rows = candidateRows;
    activeCell = { rowId: rows[startRow].id, field: fields[startColumn] };
    renderGrid();
    const pastedCell = [...rowsBody.querySelectorAll("input[data-row-id][data-field]")]
      .find(cell =>
        cell.dataset.rowId === activeCell.rowId &&
        cell.dataset.field === activeCell.field
      );
    pastedCell?.focus();
    showStatus(`Pasted ${matrix.length} row(s) from Excel.`);
  }

  function showStatus(message, isError = false) {
    statusBox.textContent = message;
    statusBox.classList.toggle("error", isError);
  }

  function markProgress(steps, currentStage) {
    if (!Array.isArray(steps)) return;
    const currentStep = currentStage?.step
      ? steps.find(step => step.step === currentStage.step)
      : null;
    const step = currentStep ||
      steps.find(item => item.status === "in_progress") ||
      [...steps].reverse().find(item => item.status === "failed") ||
      [...steps].reverse().find(item => item.status === "completed") ||
      steps[0];
    if (!step) return;
    const status = step.status || "pending";
    currentStageLabel.textContent =
      currentStage?.message || step.message || step.step || "Waiting for update";
    currentStageStatus.textContent = statusLabel(status);
    currentStageStatus.dataset.status = status;
  }

  function statusLabel(status) {
    return {
      pending: "Pending",
      queued: "Pending",
      in_progress: "In Progress",
      completed: "Completed",
      failed: "Failed",
      skipped: "Skipped"
    }[status] || status;
  }

  function addResultLink(container, label, url, isDownload = false) {
    const link = document.createElement("a");
    link.href = url;
    link.textContent = label;
    if (isDownload) {
      link.className = "folder-download-link";
      link.download = "";
    } else {
      link.target = "_blank";
      link.rel = "noopener";
    }
    container.append(link);
  }

  function renderBatch(batch) {
    batchOverallProgress.textContent =
      `${batch.completed} of ${batch.total} jobs completed · ` +
      `${batch.failed} failed · ${batch.skipped} skipped`;
    const settledJobs = batch.completed + batch.failed + batch.skipped;
    const progressPercent = batch.total
      ? Math.round((settledJobs / batch.total) * 100)
      : 0;
    batchProgressFill.style.width = `${progressPercent}%`;
    progressTrack.setAttribute("aria-valuenow", String(progressPercent));
    batchJobRows.replaceChildren();
    batch.jobs.forEach(job => {
      const tr = document.createElement("tr");
      tr.dataset.status = job.status;
      const values = [
        String(job.number),
        job.workspace_name,
        job.semantic_model,
        job.report_name
      ];
      values.forEach(value => {
        const td = document.createElement("td");
        td.textContent = value;
        tr.append(td);
      });
      const statusCell = document.createElement("td");
      const status = document.createElement("strong");
      status.textContent = statusLabel(job.status);
      statusCell.append(status);
      if (job.error) {
        const error = document.createElement("div");
        error.textContent = job.error;
        statusCell.append(error);
      }
      tr.append(statusCell);

      const resultCell = document.createElement("td");
      const links = document.createElement("div");
      links.className = "job-result-links";
      if (job.dashboard_a_url) {
        addResultLink(links, "Dashboard A", job.dashboard_a_url);
      }
      if (job.dashboard_b_url) {
        addResultLink(links, "Dashboard B", job.dashboard_b_url);
      }
      const availableOutputs = job.available_outputs || [];
      const isSettled = job.status === "completed" || job.status === "failed";
      if (isSettled && job.job_id) {
        if (availableOutputs.includes("docx")) {
          addResultLink(links, "DOCX", `/api/jobs/${job.job_id}/Dashboard_Comparison.docx`);
        }
        if (availableOutputs.includes("xlsx")) {
          addResultLink(links, "Excel", `/api/jobs/${job.job_id}/Dashboard_Comparison.xlsx`);
        }
        if (availableOutputs.includes("validation_json")) {
          addResultLink(
            links,
            "Execution JSON",
            `/api/jobs/${job.job_id}/validation-summary.json`
          );
        }
      }
      if (job.job_id && job.download_folder_ready) {
        addResultLink(
          links,
          "Download folder",
          `/api/jobs/${job.job_id}/outputs.zip`,
          true
        );
      }
      resultCell.append(links);
      tr.append(resultCell);
      batchJobRows.append(tr);
    });

    const activeJob = batch.jobs.find(job => job.status === "in_progress");
    const activeOrLast = activeJob || [...batch.jobs].reverse().find(
      job => Array.isArray(job.progress)
    );
    if (activeJob) {
      currentJobLabel.textContent =
        `Job ${activeJob.number} of ${batch.total} · ${activeJob.workspace_name} / ` +
        `${activeJob.report_name}`;
      markProgress(activeJob.progress, batch.current_stage);
    } else if (batch.status === "queued" || batch.status === "running") {
      currentJobLabel.textContent = batch.current_job_number
        ? `Preparing job ${batch.current_job_number} of ${batch.total}`
        : "Batch queued · waiting for the first job";
      currentStageLabel.textContent =
        batch.current_stage?.message || "Preparing the next comparison job.";
      currentStageStatus.textContent = "In Progress";
      currentStageStatus.dataset.status = "in_progress";
      if (activeOrLast) markProgress(activeOrLast.progress, batch.current_stage);
    } else {
      const hasFailures = batch.failed > 0;
      currentJobLabel.textContent = hasFailures
        ? "Batch finished with failures"
        : "Batch finished";
      currentStageLabel.textContent = "Job results and report files are ready below.";
      currentStageStatus.textContent = hasFailures ? "Failed" : "Completed";
      currentStageStatus.dataset.status = hasFailures ? "failed" : "completed";
    }

    folderDownload.replaceChildren();
    if (batch.downloadable_job_count > 0) {
      addResultLink(
        folderDownload,
        `Download all report folders (${batch.downloadable_job_count})`,
        `/api/batches/${batch.batch_id}/outputs.zip`,
        true
      );
    }

  }

  function renderElapsed() {
    const elapsed = batchRunning
      ? batchElapsed + (performance.now() - batchStartedAt) / 1000
      : batchElapsed;
    const totalMs = Math.max(0, Math.floor(elapsed * 1000));
    const minutes = Math.floor(totalMs / 60000);
    const seconds = Math.floor(totalMs / 1000) % 60;
    const millis = totalMs % 1000;
    const prefix = batchRunning ? "Batch elapsed: " : "Batch time: ";
    document.querySelector("#execution-time").textContent =
      `${prefix}${String(minutes).padStart(2, "0")}:` +
      `${String(seconds).padStart(2, "0")}:${String(millis).padStart(3, "0")}`;
  }

  async function pollBatch(id) {
    try {
      const response = await fetch(`/api/batches/${id}`);
      const batch = await response.json();
      if (!response.ok) throw new Error(batch.detail || "Could not read batch status.");
      renderBatch(batch);
      if (batch.status === "queued" || batch.status === "running") {
        showStatus(
          batch.current_job_number
            ? `Running job ${batch.current_job_number} of ${batch.total}...`
            : "Batch queued..."
        );
        pollTimer = setTimeout(() => pollBatch(id), 1200);
        return;
      }
      batchRunning = false;
      batchElapsed += (performance.now() - batchStartedAt) / 1000;
      clearInterval(elapsedTimer);
      elapsedTimer = null;
      renderElapsed();
      setBusy(false);
      showStatus(
        `Batch finished: ${batch.completed} completed, ${batch.failed} failed, ` +
        `${batch.skipped} skipped.`
      );
    } catch (error) {
      showStatus(`${error.message} Retrying batch status...`, true);
      pollTimer = setTimeout(() => pollBatch(id), 3000);
    }
  }

  function setBusy(busy) {
    batchRunning = busy;
    [
      "#add-row", "#delete-rows", "#clear-rows", "#validate-inputs",
      "#run-batch",
      "#stop-on-failure"
    ].forEach(selector => {
      document.querySelector(selector).disabled = busy;
    });
    rowsBody.querySelectorAll("input").forEach(input => {
      input.disabled = busy;
    });
  }

  configurationToggle.addEventListener("click", () => {
    setConfigurationExpanded(configurationContent.hidden);
  });

  grid.addEventListener("click", event => {
    const row = event.target.closest("tr[data-row-id]");
    if (row) selectRow(row.dataset.rowId, event.ctrlKey || event.metaKey);
  });
  grid.addEventListener("paste", pasteClipboardGrid);
  grid.addEventListener("input", event => {
    const input = event.target.closest("input[data-row-id][data-field]");
    if (!input) return;
    const row = rows.find(item => item.id === input.dataset.rowId);
    if (!row) return;
    const previousValue = row[input.dataset.field];
    row[input.dataset.field] = input.value;
    if (rows.filter(isNonEmpty).length > MAX_JOBS) {
      row[input.dataset.field] = previousValue;
      input.value = previousValue;
      showStatus(
        `The grid is limited to ${MAX_JOBS} non-empty jobs. Remove a row before adding another.`,
        true
      );
      return;
    }
    renderValidation();
  });
  document.querySelector("#add-row").addEventListener("click", () => {
    rows.push(newRow());
    renderGrid();
    rowsBody.lastElementChild?.querySelector("input")?.focus();
  });
  document.querySelector("#delete-rows").addEventListener("click", () => {
    if (!selectedRows.size) {
      showStatus("Select one or more rows to delete.", true);
      return;
    }
    rows = rows.filter(row => !selectedRows.has(row.id));
    if (!rows.length) rows = emptyRows();
    selectedRows.clear();
    renderGrid();
    showStatus("Selected rows deleted.");
  });
  document.querySelector("#clear-rows").addEventListener("click", () => {
    rows = emptyRows();
    selectedRows.clear();
    validationRequested = false;
    renderGrid();
    batchJobRows.replaceChildren();
    folderDownload.replaceChildren();
    batchOverallProgress.textContent = "0 of 0 jobs completed · 0 failed · 0 skipped";
    currentJobLabel.textContent = "No active job";
    currentStageLabel.textContent = "Add jobs and run a batch to begin.";
    currentStageStatus.textContent = "Pending";
    currentStageStatus.dataset.status = "pending";
    batchProgressFill.style.width = "0%";
    progressTrack.setAttribute("aria-valuenow", "0");
    showStatus("Input grid cleared.");
  });
  document.querySelector("#validate-inputs").addEventListener("click", () => {
    validationRequested = true;
    const validation = renderValidation(true);
    showStatus(
      validation.errors.length
        ? `${validation.errors.length} input issue(s) need correction.`
        : "Inputs are valid. Review any duplicate-row warnings before running."
    , validation.errors.length > 0);
  });

  document.querySelector("#run-batch").addEventListener("click", async () => {
    validationRequested = true;
    const validation = renderValidation(true);
    if (validation.errors.length) {
      showStatus("Correct the input issues before running the batch.", true);
      return;
    }
    const snapshot = validation.jobs.map(job => ({ ...job }));
    clearTimeout(pollTimer);
    batchJobRows.replaceChildren();
    folderDownload.replaceChildren();
    batchOverallProgress.textContent =
      `0 of ${snapshot.length} jobs completed · 0 failed · 0 skipped`;
    currentJobLabel.textContent = "Batch queued · waiting for the first job";
    currentStageLabel.textContent = "Preparing the comparison jobs.";
    currentStageStatus.textContent = "In Progress";
    currentStageStatus.dataset.status = "in_progress";
    batchProgressFill.style.width = "0%";
    progressTrack.setAttribute("aria-valuenow", "0");
    showStatus("Starting batch...");
    setBusy(true);
    batchElapsed = 0;
    batchStartedAt = performance.now();
    renderElapsed();
    elapsedTimer = setInterval(renderElapsed, 250);
    try {
      const response = await fetch("/api/batches", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          jobs: snapshot,
          stop_on_first_failure:
            document.querySelector("#stop-on-failure").checked,
          run_validation_scenarios: runValidationScenarios.checked
        })
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "Could not start the batch.");
      batchId = body.batch_id;
      resultsColumn.hidden = false;
      setConfigurationExpanded(false);
      await pollBatch(batchId);
    } catch (error) {
      batchRunning = false;
      clearInterval(elapsedTimer);
      elapsedTimer = null;
      setBusy(false);
      showStatus(error.message, true);
    }
  });

  renderGrid();
})();
