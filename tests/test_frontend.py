import asyncio
from pathlib import Path

import pytest


def test_inline_cell_edit_without_formula_bar() -> None:
    async_playwright = pytest.importorskip("playwright.async_api").async_playwright
    frontend = Path(__file__).resolve().parents[1] / "Frontend"
    html = (frontend / "index.html").read_text(encoding="utf-8")
    app_script = (frontend / "static" / "app.js").read_text(encoding="utf-8")
    html = html.replace(
        '<script src="/static/app.js"></script>',
        f"<script>{app_script}</script>",
    )
    async def verify_editing() -> None:
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch()
            except Exception as error:
                pytest.skip(f"Chromium unavailable: {error}")
            try:
                page = await browser.new_page()
                await page.set_content(html)
                assert await page.locator(".formula-bar").count() == 0
                assert await page.locator(".results-column").is_hidden()
                inputs = page.locator("#batch-rows tr").first.locator("input")
                await inputs.nth(0).fill("Supply Chain")
                assert await page.locator("#job-counter").inner_text() == "1 of 10 jobs added"
                target_cell = inputs.nth(1)
                await target_cell.fill("Updated Semantic Model")
                assert await target_cell.input_value() == "Updated Semantic Model"
            finally:
                await browser.close()

    asyncio.run(verify_editing())


def test_excel_paste_fills_cells_and_rows() -> None:
    async_playwright = pytest.importorskip("playwright.async_api").async_playwright
    frontend = Path(__file__).resolve().parents[1] / "Frontend"
    html = (frontend / "index.html").read_text(encoding="utf-8")
    app_script = (frontend / "static" / "app.js").read_text(encoding="utf-8")
    html = html.replace(
        '<script src="/static/app.js"></script>',
        f"<script>{app_script}</script>",
    )

    async def verify_paste() -> None:
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch()
            except Exception as error:
                pytest.skip(f"Chromium unavailable: {error}")
            try:
                page = await browser.new_page()
                await page.set_content(html)
                target = page.locator(
                    '#batch-rows tr:nth-child(2) input[data-field="workspace_name"]'
                )
                await target.evaluate("""input => {
                    input.focus();
                    const clipboard = new DataTransfer();
                    clipboard.setData(
                        "text/plain",
                        "Supply Chain Dashboards\\tIB Pallet Forecast\\tIB Pallet Forecast\\r\\n" +
                        "Transportation\\tDG Invoice Data\\tDG Invoice Data"
                    );
                    input.dispatchEvent(new ClipboardEvent("paste", {
                        bubbles: true,
                        cancelable: true,
                        clipboardData: clipboard
                    }));
                }""")
                rows = page.locator("#batch-rows tr")
                assert await rows.nth(1).locator("input").nth(0).input_value() == "Supply Chain Dashboards"
                assert await rows.nth(1).locator("input").nth(1).input_value() == "IB Pallet Forecast"
                assert await rows.nth(1).locator("input").nth(2).input_value() == "IB Pallet Forecast"
                assert await rows.nth(2).locator("input").nth(0).input_value() == "Transportation"
                assert await rows.nth(2).locator("input").nth(1).input_value() == "DG Invoice Data"
                assert await page.locator("#job-counter").inner_text() == "2 of 10 jobs added"
            finally:
                await browser.close()

    asyncio.run(verify_paste())


def test_excel_paste_rejects_more_than_ten_jobs() -> None:
    async_playwright = pytest.importorskip("playwright.async_api").async_playwright
    frontend = Path(__file__).resolve().parents[1] / "Frontend"
    html = (frontend / "index.html").read_text(encoding="utf-8")
    app_script = (frontend / "static" / "app.js").read_text(encoding="utf-8")
    html = html.replace(
        '<script src="/static/app.js"></script>',
        f"<script>{app_script}</script>",
    )

    async def verify_limit() -> None:
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch()
            except Exception as error:
                pytest.skip(f"Chromium unavailable: {error}")
            try:
                page = await browser.new_page()
                await page.set_content(html)
                rows = page.locator("#batch-rows tr")
                for index in range(10):
                    await rows.nth(index).locator("input").nth(0).fill(f"Workspace {index + 1}")
                await page.locator("#add-row").click()
                target = page.locator("#batch-rows tr").last.locator("input").first
                await target.evaluate("""input => {
                    input.focus();
                    const clipboard = new DataTransfer();
                    clipboard.setData("text/plain", "Too many\\tSemantic model\\tReport");
                    input.dispatchEvent(new ClipboardEvent("paste", {
                        bubbles: true,
                        cancelable: true,
                        clipboardData: clipboard
                    }));
                }""")
                assert await page.locator("#job-counter").inner_text() == "10 of 10 jobs added"
                assert "exceed the 10-job limit" in await page.locator("#status").inner_text()
                assert await target.input_value() == ""
            finally:
                await browser.close()

    asyncio.run(verify_limit())


def test_completed_job_shows_docx_dashboard_links_and_download_folders() -> None:
    async_playwright = pytest.importorskip("playwright.async_api").async_playwright
    frontend = Path(__file__).resolve().parents[1] / "Frontend"
    html = (frontend / "index.html").read_text(encoding="utf-8")
    app_script = (frontend / "static" / "app.js").read_text(encoding="utf-8")
    fetch_mock = """<script>
      const batch = {
        batch_id: "batch-id",
        status: "completed",
        total: 1,
        completed: 1,
        failed: 0,
        skipped: 0,
        downloadable_job_count: 1,
        current_job_number: null,
        current_stage: null,
        jobs: [{
          number: 1,
          job_id: "job-id",
          status: "completed",
          workspace_name: "Supply Chain",
          semantic_model: "Supply Model",
          report_name: "Daywork",
          error: null,
          progress: [],
          dashboard_a_url: "https://app.powerbi.com/reports/base",
          dashboard_b_url: "https://app.powerbi.com/reports/gcp",
          available_outputs: [
            "docx",
            "xlsx",
            "validation_json"
          ],
          download_folder_ready: true
        }]
      };
      window.batchRequestBody = null;
      window.fetch = async (url, options = {}) => {
        if (url === "/api/batches" && options.body) {
          window.batchRequestBody = JSON.parse(options.body);
        }
        return {
        ok: true,
        json: async () => url === "/api/batches"
          ? { batch_id: "batch-id" }
          : batch
        };
      };
    </script>"""
    html = html.replace(
        '<script src="/static/app.js"></script>',
        f"{fetch_mock}<script>{app_script}</script>",
    )

    async def verify_results() -> None:
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch()
            except Exception as error:
                pytest.skip(f"Chromium unavailable: {error}")
            try:
                page = await browser.new_page()
                await page.set_content(html)
                inputs = page.locator("#batch-rows tr").first.locator("input")
                await inputs.nth(0).fill("Supply Chain")
                await inputs.nth(1).fill("Supply Model")
                await inputs.nth(2).fill("Daywork")
                await page.locator("#run-batch").click()
                links = page.locator("#batch-job-rows a")
                await page.locator("#batch-folder-download a").wait_for()
                assert await page.locator("#configuration-content").is_hidden()
                assert await page.locator(".results-column").is_visible()
                labels = await links.all_inner_texts()
                assert "Dashboard A" in labels
                assert "Dashboard B" in labels
                assert "DOCX" in labels
                assert "Excel" in labels
                assert "Execution JSON" in labels
                assert "Download folder" in labels
                assert "Discovery" not in labels
                assert await page.evaluate(
                    "window.batchRequestBody.run_validation_scenarios"
                ) is True
                assert await page.locator(
                    "#batch-job-rows a[href='https://app.powerbi.com/reports/base']"
                ).count() == 1
                assert await page.locator(
                    "#batch-job-rows a[href='/api/jobs/job-id/Dashboard_Comparison.docx']"
                ).count() == 1
                assert await page.locator(
                    "#batch-folder-download a[href='/api/batches/batch-id/outputs.zip']"
                ).count() == 1
            finally:
                await browser.close()

    asyncio.run(verify_results())
