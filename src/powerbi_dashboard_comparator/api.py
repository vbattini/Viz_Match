import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from .config import (
    DEFAULT_LOCATION,
    DEFAULT_MODEL_ID,
    DEFAULT_PROJECT_ID,
    configure_vertex_ai,
)
from .workflow import run_dashboard_comparison

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSET_ROOT = Path(__file__).resolve().parent / "static"
OUTPUT_ROOT = PROJECT_ROOT / "output" / "jobs"
JOBS: dict[str, dict] = {}
JOB_TASKS: set[asyncio.Task] = set()
ACTIVE_JOB_ID: str | None = None
JOB_LOCK = asyncio.Lock()

app = FastAPI(title="Power BI Dashboard Comparator")
app.mount("/static", StaticFiles(directory=ASSET_ROOT), name="static")


class CompareRequest(BaseModel):
    url_a: str
    url_b: str

    @field_validator("url_a", "url_b")
    @classmethod
    def validate_power_bi_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.hostname != "app.powerbi.com":
            raise ValueError("Use an HTTPS Power BI report URL on app.powerbi.com")
        return value


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Power BI Dashboard Comparator</title>
  <style>
    :root {
      color-scheme: light;
      font-family: "Segoe UI", Arial, sans-serif;
      font-synthesis: none;
      text-rendering: optimizeLegibility;
      --ink: #1f1e1d;
      --muted: #5f5b57;
      --charcoal: #252423;
      --charcoal-soft: #3b3a39;
      --yellow: #f2c811;
      --yellow-hover: #ffd930;
      --teal: #0f7b6c;
      --line: #d9d4cc;
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; margin: 0; overflow: hidden; }
    body {
      color: var(--ink);
      background: #2b2a28 url("/static/powerbi-wallpaper.jpg") center / cover no-repeat fixed;
    }
    body::before {
      position: fixed;
      inset: 0;
      content: "";
      pointer-events: none;
      background: linear-gradient(180deg, #14120b26 0%, #14120b59 100%);
    }
    .page-shell {
      position: relative;
      display: flex;
      flex-direction: column;
      gap: 16px;
      height: 100vh;
      height: 100dvh;
      max-width: 1200px;
      margin: 0 auto;
      padding: 22px 28px 24px;
    }
    .topbar { display: flex; align-items: center; justify-content: space-between; gap: 18px;
      min-height: 44px; }
    .brand { display: flex; align-items: center; gap: 12px; color: var(--charcoal);
      font-weight: 800; font-size: 17px; letter-spacing: -.02em; white-space: nowrap; }
    .brand-mark { display: grid; width: 40px; height: 40px; place-items: center;
      border-radius: 12px; color: var(--yellow); background: var(--charcoal);
      box-shadow: 0 8px 18px #14120b4d; }
    .brand-mark svg { width: 23px; height: 23px; }
    .topbar-right { display: flex; align-items: center; gap: 14px; }
    .top-note { color: var(--charcoal-soft); font-size: 13px; font-weight: 700; }
    .workspace { display: grid; flex: 1 1 auto; min-height: 0;
      grid-template-columns: minmax(0, 1fr) minmax(360px, .9fr);
      overflow: hidden; border-radius: 26px; background: #ffffffd1;
      -webkit-backdrop-filter: blur(16px); backdrop-filter: blur(16px);
      box-shadow: 0 30px 70px #0f0d0859; }
    .story {
      position: relative;
      display: flex;
      min-height: 0;
      flex-direction: column;
      overflow: hidden;
      padding: clamp(22px, 4vh, 44px) clamp(28px, 3.4vw, 46px);
      color: #fff;
      background:
        radial-gradient(ellipse at 0% 0%, #4a484599 0, transparent 52%),
        linear-gradient(160deg, #2f2e2cd1 0%, #252423d9 60%, #1b1a19e0 100%);
    }
    .story::before, .story::after { position: absolute; content: ""; border: 1px solid #f2c81124;
      border-radius: 50%; pointer-events: none; }
    .story::before { width: 440px; height: 440px; right: -190px; top: -120px; }
    .story::after { width: 300px; height: 300px; right: -110px; top: -50px; }
    .eyebrow { position: relative; display: inline-flex; align-items: center; gap: 8px; width: fit-content;
      padding: 7px 12px; border: 1px solid #f2c81166; border-radius: 999px;
      color: #ffe98a; background: #f2c81714; font-size: 11px; font-weight: 800;
      letter-spacing: .08em; text-transform: uppercase; }
    .eyebrow-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--yellow);
      box-shadow: 0 0 0 4px #f2c81130; }
    h1 { position: relative; max-width: 520px; margin: clamp(12px, 2.4vh, 22px) 0 10px;
      font-size: clamp(28px, 3.3vw, 46px); line-height: 1.08; letter-spacing: -.04em; }
    .story-copy { position: relative; max-width: 460px; margin: 0; color: #e6e1d8;
      font-size: clamp(13px, 1.1vw, 15px); line-height: 1.6; }
    .artwork { position: relative; z-index: 1; display: block; flex: 0 1 auto;
      width: 100%; height: clamp(140px, 30vh, 290px); margin: auto 0; 
      filter: drop-shadow(0 18px 22px #0000004d); }
    .story-foot { position: relative; z-index: 1; margin: 0; color: #bfb9ae;
      font-size: 12px; line-height: 1.55; }
    .form-panel { display: flex; min-height: 0; flex-direction: column; justify-content: center;
      overflow: hidden; background: #ffffffe6; padding: clamp(20px, 3.6vh, 44px) clamp(26px, 3.6vw, 50px); }
    .form-panel h2 { margin: 0; font-size: clamp(21px, 2vw, 24px); letter-spacing: -.03em; }
    .form-intro { margin: 6px 0 16px; color: var(--muted); font-size: 13px; line-height: 1.5; }
    .config { display: flex; align-items: flex-start; gap: 9px; margin: 0 0 16px;
      padding: 10px 12px; border: 1px solid var(--line); border-radius: 12px;
      color: #4a4642; background: #faf8f4; font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
    .config-icon { flex: 0 0 auto; color: var(--teal); font-size: 14px; }
    .url-field { margin: 0 0 14px; }
    .field-label { display: flex; align-items: center; gap: 9px; margin-bottom: 7px;
      color: var(--charcoal); font-size: 13px; font-weight: 800; }
    .label-icon { display: grid; width: 28px; height: 28px; place-items: center;
      border-radius: 9px; font-size: 14px; }
    .baseline .label-icon { color: var(--charcoal); background: var(--yellow); }
    .test .label-icon { color: #fff; background: var(--teal); }
    .role-tag { margin-left: auto; padding: 4px 8px; border-radius: 999px;
      font-size: 10px; font-weight: 900; letter-spacing: .07em; }
    .baseline .role-tag { color: #5c4a00; background: #fff3bf; }
    .test .role-tag { color: #0a5246; background: #d8f1ea; }
    input {
      display: block;
      width: 100%;
      min-height: 46px;
      padding: 12px 14px;
      border: 1px solid #cfc9bf;
      border-radius: 12px;
      color: var(--ink);
      background: #fdfcfa;
      font: inherit;
      font-size: 13px;
      transition: border-color .16s ease, box-shadow .16s ease, background .16s ease;
    }
    input::placeholder { color: #958f86; }
    input:hover { border-color: #a39c91; }
    input:focus { outline: 3px solid #f2c81159; border-color: var(--charcoal); background: #fff; }
    .submit-button { display: flex; align-items: center; justify-content: center; gap: 10px;
      width: 100%; min-height: 50px; margin-top: 4px; padding: 13px 18px;
      border: 0; border-radius: 12px; color: var(--charcoal);
      background: var(--yellow);
      box-shadow: 0 10px 22px #f2c81150; font: inherit; font-weight: 800;
      cursor: pointer; transition: background .16s ease, transform .16s ease, box-shadow .16s ease; }
    .submit-button:hover:not(:disabled) { background: var(--yellow-hover); transform: translateY(-1px);
      box-shadow: 0 12px 24px #f2c81166; }
    .submit-button:focus-visible { outline: 3px solid var(--charcoal); outline-offset: 3px; }
    .submit-button:disabled { opacity: .7; cursor: wait; }
    .button-arrow { font-size: 18px; line-height: 1; }
    #status { min-height: 44px; margin-top: 14px; padding: 11px 14px;
      border: 1px solid var(--line); border-radius: 12px; color: #3f3b37;
      background: #f7f5f1; font-size: 13px; line-height: 1.5; white-space: pre-wrap; }
    #status:not(:empty) { overflow-wrap: anywhere; }
    #status.error { border-color: #f1b8b7; color: #8b2026; background: #fff0ef; }
    #results { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
    #results:empty { display: none; }
    #results a { display: inline-flex; align-items: center; padding: 8px 11px;
      border: 1px solid #e3d48f; border-radius: 10px; color: var(--charcoal);
      background: #fff9dc; font-size: 12px; font-weight: 700; text-decoration: none; }
    #results a:hover { border-color: var(--charcoal); background: var(--yellow); }
    #execution-time { padding: 7px 14px; border-radius: 999px;
      color: var(--yellow); background: var(--charcoal); box-shadow: 0 5px 14px #14120b40;
      font-size: 12px; font-weight: 700; white-space: nowrap; }
    @media (max-height: 780px) {
      .page-shell { gap: 12px; padding-top: 16px; padding-bottom: 16px; }
      .form-intro { display: none; }
      .story-foot { display: none; }
      .config { margin-bottom: 12px; }
      .url-field { margin-bottom: 10px; }
      input { min-height: 42px; }
      #status { margin-top: 10px; }
    }
    @media (max-width: 860px) {
      .page-shell { padding: 14px 14px 16px; }
      .workspace { grid-template-columns: 1fr; box-shadow: 0 14px 34px #0f0d0859; }
      .story { display: none; }
      .form-panel { padding: 24px 24px 26px; }
      .top-note { display: none; }
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { scroll-behavior: auto !important; transition-duration: .01ms !important; }
    }
  </style>
</head>
<body>
  <main class="page-shell">
    <header class="topbar">
      <div class="brand">
        <span class="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none">
            <path d="M4 17.5V12h4v5.5H4Zm6 0V6h4v11.5h-4Zm6 0V9h4v8.5h-4Z"
              stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>
            <path d="m4.5 9 5-4 4 2 5-4" stroke="currentColor" stroke-width="2"
              stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </span>
        <span>Dashboard Compare</span>
      </div>
      <div class="topbar-right">
        <div class="top-note">A clearer view of what changed</div>
        <output id="execution-time" aria-live="off">Elapsed: 00:00:000</output>
      </div>
    </header>
    <div class="workspace">
      <section class="story" aria-labelledby="hero-title">
        <span class="eyebrow"><span class="eyebrow-dot"></span>Report checkup</span>
        <h1 id="hero-title">Spot the story behind the numbers.</h1>
        <p class="story-copy">Compare two Power BI reports side by side and get an easy-to-follow
          summary of what matches, what changed, and what needs a closer look.</p>
        <svg class="artwork" viewBox="0 0 520 370" role="img"
          aria-label="Cartoon dashboards A and B being compared by a friendly robot">
          <defs>
            <linearGradient id="screenA" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0" stop-color="#e5f3ff"/><stop offset="1" stop-color="#fff"/>
            </linearGradient>
            <linearGradient id="robot" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0" stop-color="#8cf0d1"/><stop offset="1" stop-color="#36bfae"/>
            </linearGradient>
          </defs>
          <circle cx="270" cy="184" r="155" fill="#ffffff0b"/>
          <path d="M40 311c64-25 111-21 162-2 64 24 146 24 220-6 24-10 43-15 65-14v60H40v-38Z"
            fill="#061f3a" opacity=".3"/>
          <g transform="translate(30 76) rotate(-7 125 100)">
            <rect x="20" y="19" width="183" height="204" rx="18" fill="#8ab9d2" opacity=".35"/>
            <rect x="11" y="9" width="183" height="204" rx="18" fill="url(#screenA)"/>
            <rect x="11" y="9" width="183" height="37" rx="18" fill="#dcecff"/>
            <path d="M11 28a19 19 0 0 1 19-19h145a19 19 0 0 1 19 19v18H11V28Z" fill="#dcecff"/>
            <circle cx="31" cy="28" r="4" fill="#ff827b"/><circle cx="45" cy="28" r="4" fill="#ffc45c"/>
            <circle cx="59" cy="28" r="4" fill="#52cfae"/>
            <rect x="29" y="65" width="76" height="7" rx="3.5" fill="#7395ae"/>
            <rect x="29" y="80" width="45" height="5" rx="2.5" fill="#c0d0dc"/>
            <rect x="29" y="112" width="69" height="73" rx="10" fill="#edf5fc"/>
            <path d="M41 167v-19h12v19H41Zm19 0v-39h12v39H60Zm19 0v-29h12v29H79Z"
              fill="#4387ec"/>
            <rect x="110" y="112" width="65" height="73" rx="10" fill="#edf5fc"/>
            <path d="M123 160c10-28 16 10 25-14s14-17 19-29" fill="none" stroke="#18a994"
              stroke-width="5" stroke-linecap="round"/>
            <rect x="33" y="191" width="66" height="21" rx="10.5" fill="#dbeaff"/>
            <text x="66" y="205" text-anchor="middle" fill="#1d58a1" font-size="10"
              font-family="Arial" font-weight="700">BASELINE</text>
          </g>
          <g transform="translate(308 91) rotate(7 92 100)">
            <rect x="12" y="18" width="153" height="178" rx="17" fill="#8ab9d2" opacity=".35"/>
            <rect x="3" y="8" width="153" height="178" rx="17" fill="url(#screenA)"/>
            <rect x="3" y="8" width="153" height="34" rx="17" fill="#dff8ef"/>
            <path d="M3 25A17 17 0 0 1 20 8h119a17 17 0 0 1 17 17v17H3V25Z" fill="#dff8ef"/>
            <circle cx="21" cy="25" r="3.5" fill="#ff827b"/><circle cx="33" cy="25" r="3.5" fill="#ffc45c"/>
            <circle cx="45" cy="25" r="3.5" fill="#52cfae"/>
            <rect x="20" y="59" width="80" height="6" rx="3" fill="#7395ae"/>
            <rect x="20" y="73" width="50" height="5" rx="2.5" fill="#c0d0dc"/>
            <rect x="20" y="100" width="119" height="65" rx="9" fill="#edf7f4"/>
            <path d="M32 150v-16h13v16H32Zm21 0v-34h13v34H53Zm21 0v-26h13v26H74Zm21 0v-42h13v42H95Z"
              fill="#16a891"/>
            <rect x="28" y="171" width="54" height="19" rx="9.5" fill="#d9f7ec"/>
            <text x="55" y="184" text-anchor="middle" fill="#09644f" font-size="9"
              font-family="Arial" font-weight="700">TEST</text>
          </g>
          <g transform="translate(185 145)">
            <path d="M68 18v-12" stroke="#8cf0d1" stroke-width="6" stroke-linecap="round"/>
            <circle cx="68" cy="4" r="7" fill="#ffcf68"/>
            <rect x="15" y="22" width="106" height="112" rx="30" fill="url(#robot)"/>
            <rect x="27" y="40" width="82" height="51" rx="20" fill="#103952"/>
            <circle cx="53" cy="65" r="7" fill="#e8fff9"/>
            <circle cx="82" cy="65" r="7" fill="#e8fff9"/>
            <path d="M57 79c7 6 14 6 21 0" fill="none" stroke="#76e9c7" stroke-width="4"
              stroke-linecap="round"/>
            <path d="m15 62-11 7m117-7 11 7" stroke="#8cf0d1" stroke-width="7" stroke-linecap="round"/>
            <rect x="43" y="101" width="50" height="42" rx="12" fill="#48c6b4"/>
            <path d="m52 122 8 8 21-22" fill="none" stroke="#fff" stroke-width="6"
              stroke-linecap="round" stroke-linejoin="round"/>
            <path d="M42 140 32 165m73-25 10 25" stroke="#8cf0d1" stroke-width="9"
              stroke-linecap="round"/>
          </g>
          <g transform="translate(194 307)">
            <rect width="132" height="32" rx="16" fill="#fff"/>
            <circle cx="19" cy="16" r="6" fill="#16b991"/>
            <text x="75" y="20" text-anchor="middle" fill="#123b56" font-size="11"
              font-family="Arial" font-weight="700">LOOKING GOOD!</text>
          </g>
          <path d="m453 71 5 12 12 5-12 5-5 12-5-12-12-5 12-5 5-12Z" fill="#ffd36d"/>
          <circle cx="74" cy="55" r="5" fill="#79e4cd"/>
          <circle cx="444" cy="232" r="4" fill="#ffbb79"/>
        </svg>
        <p class="story-foot">Your authenticated Power BI session stays on this machine.
          Results include clear evidence and downloadable reports.</p>
      </section>
      <section class="form-panel" aria-labelledby="form-title">
        <h2 id="form-title">Set up your comparison</h2>
        <p class="form-intro">Paste a report link into each field. Dashboard A is the baseline;
          Dashboard B is checked against it.</p>
        <p id="config" class="config"><span class="config-icon" aria-hidden="true">●</span>
          <span class="config-copy">Checking local configuration…</span></p>
        <form id="compare-form">
          <div class="url-field baseline">
            <label class="field-label" for="url-a">
              <span class="label-icon" aria-hidden="true">A</span>
              Dashboard A
              <span class="role-tag">BASELINE</span>
            </label>
            <input id="url-a" name="url_a" type="url" autocomplete="url"
              placeholder="https://app.powerbi.com/…" spellcheck="false" required>
          </div>
          <div class="url-field test">
            <label class="field-label" for="url-b">
              <span class="label-icon" aria-hidden="true">B</span>
              Dashboard B
              <span class="role-tag">TEST REPORT</span>
            </label>
            <input id="url-b" name="url_b" type="url" autocomplete="url"
              placeholder="https://app.powerbi.com/…" spellcheck="false" required>
          </div>
          <button id="submit" class="submit-button" type="submit">
            <span>Run comparison</span><span class="button-arrow" aria-hidden="true">→</span>
          </button>
        </form>
        <div id="status" role="status" aria-live="polite">Ready when you are.</div>
        <div id="results" aria-label="Comparison reports"></div>
      </section>
    </div>
  </main>
  <script>
    const form = document.querySelector("#compare-form");
    const button = document.querySelector("#submit");
    const statusBox = document.querySelector("#status");
    const results = document.querySelector("#results");
    const configBox = document.querySelector("#config .config-copy");
    let pollTimer;
    let elapsedTimer;
    let elapsedSeconds = 0;
    let elapsedUpdatedAt = 0;
    let jobStatus = "idle";

    function currentElapsedSeconds() {
      return elapsedSeconds +
        (elapsedUpdatedAt ? (performance.now() - elapsedUpdatedAt) / 1000 : 0);
    }

    function formatStopwatch(milliseconds) {
      const totalMilliseconds = Math.max(0, Math.floor(milliseconds));
      const minutes = Math.floor(totalMilliseconds / 60000);
      const seconds = Math.floor(totalMilliseconds / 1000) % 60;
      const remainingMilliseconds = totalMilliseconds % 1000;
      return `${String(minutes).padStart(2, "0")}:` +
        `${String(seconds).padStart(2, "0")}:` +
        `${String(remainingMilliseconds).padStart(3, "0")}`;
    }

    function renderExecutionTime() {
      const elapsed = currentElapsedSeconds();
      const timer = document.querySelector("#execution-time");
      if (jobStatus === "queued" || jobStatus === "running") {
        timer.textContent = `Elapsed: ${formatStopwatch(elapsed * 1000)}`;
      } else if (jobStatus === "completed") {
        timer.textContent = `Execution time: ${formatStopwatch(elapsed * 1000)}`;
      } else if (jobStatus === "failed") {
        timer.textContent = `Failed after ${formatStopwatch(elapsed * 1000)}`;
      } else {
        timer.textContent = "Elapsed: 00:00:000";
      }
    }

    function updateExecutionTime(job) {
      elapsedSeconds = Number(job.elapsed_seconds) || 0;
      jobStatus = job.status;
      elapsedUpdatedAt = jobStatus === "running"
        ? performance.now()
        : 0;
      renderExecutionTime();
      if ((job.status === "queued" || job.status === "running") && !elapsedTimer) {
        elapsedTimer = setInterval(renderExecutionTime, 250);
      } else if (job.status !== "queued" && job.status !== "running" && elapsedTimer) {
        clearInterval(elapsedTimer);
        elapsedTimer = null;
      }
    }

    fetch("/api/config").then(r => r.json()).then(config => {
      configBox.textContent =
        `Vertex AI: ${config.location} / ${config.model}` +
        (config.authentication_ready ? " · Power BI sign-in ready" : " · auth.json missing");
    }).catch(() => { configBox.textContent = "Could not load local configuration."; });

    async function checkJob(jobId) {
      const response = await fetch(`/api/jobs/${jobId}`);
      const job = await response.json();
      if (!response.ok) throw new Error(job.detail || "Could not read job status.");
      updateExecutionTime(job);
      statusBox.classList.toggle(
        "error",
        job.status === "failed"
      );
      if (job.status === "queued" || job.status === "running") {
        statusBox.textContent = job.status === "queued"
          ? "Comparison queued…"
          : "Comparing dashboards…";
        pollTimer = setTimeout(() => checkJob(jobId).catch(showError), 1500);
        return;
      }
      button.disabled = false;
      if (job.status === "failed") {
        statusBox.textContent = `Comparison failed: ${job.error}`;
        return;
      }
      const summary = job.summary;
      const successRate = summary.success_percentage == null
        ? "N/A"
        : `${summary.success_percentage.toFixed(2)}%`;
      statusBox.textContent =
        `Complete · ${successRate} success · ` +
        `${summary.matched_states} matched state(s) · ` +
        `${summary.matches} match(es) · ${summary.differences} difference(s) · ` +
        `${summary.unverified} unverified · ` +
        `${summary.exported_data_matches} exported cell match(es) · ` +
        `${summary.exported_data_differences} exported cell difference(s)`;
      for (const [label, url] of [
        ["Download Word report", `/api/jobs/${jobId}/Dashboard_Comparison.docx`],
        ["Download Excel report", `/api/jobs/${jobId}/Dashboard_Comparison.xlsx`],
        ["View JSON report", `/api/jobs/${jobId}/comparison.json`]
      ]) {
        const link = document.createElement("a");
        link.href = url;
        link.textContent = label;
        link.target = "_blank";
        link.rel = "noopener";
        results.append(link);
      }
    }

    function showError(error) {
      button.disabled = false;
      statusBox.classList.add("error");
      statusBox.textContent = error.message;
    }

    form.addEventListener("submit", async event => {
      event.preventDefault();
      clearTimeout(pollTimer);
      clearInterval(elapsedTimer);
      elapsedTimer = null;
      elapsedSeconds = 0;
      elapsedUpdatedAt = 0;
      jobStatus = "idle";
      renderExecutionTime();
      results.replaceChildren();
      statusBox.classList.remove("error");
      statusBox.textContent = "Starting comparison…";
      button.disabled = true;
      try {
        const response = await fetch("/api/compare", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            url_a: form.elements.url_a.value,
            url_b: form.elements.url_b.value
          })
        });
        const body = await response.json();
        if (!response.ok) throw new Error(body.detail || "Could not start comparison.");
        await checkJob(body.job_id);
      } catch (error) {
        showError(error);
      }
    });
  </script>
</body>
</html>"""


def _storage_state_path() -> Path:
    configured_path = Path(os.environ.get("POWERBI_STORAGE_STATE", "auth.json"))
    if not configured_path.is_absolute():
        configured_path = PROJECT_ROOT / configured_path
    return configured_path


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


@app.get("/api/config")
async def get_config() -> dict:
    storage_state = _storage_state_path()
    return {
        "project_id": os.environ.get("GOOGLE_CLOUD_PROJECT", DEFAULT_PROJECT_ID),
        "location": os.environ.get("GOOGLE_CLOUD_LOCATION", DEFAULT_LOCATION),
        "model": os.environ.get("GEMINI_MODEL", DEFAULT_MODEL_ID),
        "authentication_ready": storage_state.is_file(),
    }


@app.post("/api/compare", status_code=202)
async def start_comparison(request: CompareRequest) -> dict:
    storage_state = _storage_state_path()
    if not storage_state.is_file():
        raise HTTPException(
            status_code=503,
            detail="Power BI authentication is missing. Create a Playwright storage-state file first.",
        )

    global ACTIVE_JOB_ID
    async with JOB_LOCK:
        if ACTIVE_JOB_ID is not None:
            raise HTTPException(
                status_code=409, detail="A comparison is already running."
            )
        job_id = str(uuid4())
        JOBS[job_id] = {
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "started_monotonic": None,
            "elapsed_seconds": 0.0,
            "summary": None,
            "error": None,
        }
        ACTIVE_JOB_ID = job_id
        task = asyncio.create_task(_run_comparison(job_id, request, storage_state))
        JOB_TASKS.add(task)
        task.add_done_callback(JOB_TASKS.discard)
    return {"job_id": job_id, "status": "queued"}


async def _execute_comparison(
    job_id: str, request: CompareRequest, storage_state: Path
) -> dict:
    configure_vertex_ai()
    output_dir = OUTPUT_ROOT / job_id
    result = await run_dashboard_comparison(
        job_id=job_id,
        url_a=request.url_a,
        url_b=request.url_b,
        output_dir=output_dir,
        storage_state=storage_state,
    )
    return result["summary"]


async def _run_comparison(
    job_id: str, request: CompareRequest, storage_state: Path
) -> None:
    global ACTIVE_JOB_ID
    job = JOBS[job_id]
    job["status"] = "running"
    job["started_monotonic"] = time.monotonic()
    work_task = asyncio.create_task(
        _execute_comparison(job_id, request, storage_state)
    )
    try:
        job["summary"] = await work_task
        job["status"] = "completed"
    except Exception as exc:
        logger.exception("Comparison job %s failed", job_id)
        job["error"] = f"{type(exc).__name__}: {exc}"
        job["status"] = "failed"
    finally:
        started_monotonic = job.get("started_monotonic")
        if started_monotonic is not None:
            job["elapsed_seconds"] = time.monotonic() - started_monotonic
        async with JOB_LOCK:
            if ACTIVE_JOB_ID == job_id:
                ACTIVE_JOB_ID = None


def _job(job_id: str) -> dict:
    try:
        canonical_id = str(UUID(job_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Comparison job not found.") from exc
    job = JOBS.get(canonical_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Comparison job not found.")
    return job


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str) -> dict:
    job = _job(job_id)
    started_monotonic = job.get("started_monotonic")
    elapsed_seconds = (
        time.monotonic() - started_monotonic
        if job["status"] == "running" and started_monotonic is not None
        else job.get("elapsed_seconds", 0.0)
    )
    return {
        "job_id": str(UUID(job_id)),
        "status": job["status"],
        "elapsed_seconds": elapsed_seconds,
        "summary": job["summary"],
        "error": job["error"],
    }


@app.get("/api/jobs/{job_id}/comparison.json")
async def get_json_report(job_id: str) -> FileResponse:
    _job(job_id)
    path = OUTPUT_ROOT / str(UUID(job_id)) / "comparison.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="JSON report is not ready.")
    return FileResponse(path, media_type="application/json", filename="comparison.json")


@app.get("/api/jobs/{job_id}/Dashboard_Comparison.xlsx")
async def get_excel_report(job_id: str) -> FileResponse:
    _job(job_id)
    path = OUTPUT_ROOT / str(UUID(job_id)) / "Dashboard_Comparison.xlsx"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Excel report is not ready.")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="Dashboard_Comparison.xlsx",
    )


@app.get("/api/jobs/{job_id}/Dashboard_Comparison.docx")
async def get_docx_report(job_id: str) -> FileResponse:
    _job(job_id)
    path = OUTPUT_ROOT / str(UUID(job_id)) / "Dashboard_Comparison.docx"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="DOCX report is not ready.")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename="Dashboard_Comparison.docx",
    )
