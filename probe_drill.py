import asyncio
import json

from playwright.async_api import async_playwright

URL = (
    "https://app.powerbi.com/groups/265017eb-3520-4bd2-bf34-8c4167b03c59/reports/"
    "d2cc8a2e-4d55-4107-9332-bbc9b8036e07/2373de5590ee0726410a?ctid=e7c6f609-f63a-4ec7-b71e-6734c4c9d231&experience=power-bi"
)

DUMP = r"""
() => {
  const out = [];
  for (const b of document.querySelectorAll('button, [role="button"]')) {
    const label = b.getAttribute('aria-label') || '';
    const cls = b.className && b.className.baseVal === undefined ? String(b.className) : '';
    if (/drill|next level/i.test(label) || /drill/i.test(cls) || b.getAttribute('data-automation-type') || b.getAttribute('data-testid')?.includes('drill')) {
      const r = b.getBoundingClientRect();
      out.push({
        label, cls: cls.slice(0, 120), auto: b.getAttribute('data-automation-type'),
        testid: b.getAttribute('data-testid'), disabled: b.disabled,
        aria_disabled: b.getAttribute('aria-disabled'),
        x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height),
      });
    }
  }
  return out;
}
"""


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(storage_state="auth.json")
        page = await context.new_page()
        await page.goto(URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(12000)
        await page.get_by_text("Item Counts", exact=True).first.click()
        await page.wait_for_timeout(15000)
        visuals = await page.evaluate(
            "() => [...document.querySelectorAll('div.visualContainer, [data-visual-id]')]"
            ".map(e => { const r = e.getBoundingClientRect(); return {"
            "id: e.getAttribute('data-visual-id'), type: e.getAttribute('data-visual-type'),"
            "title: (e.querySelector('.visualTitle, .titleText') || {}).innerText || e.getAttribute('aria-label'),"
            "x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height)}; })"
        )
        print("VISUALS", json.dumps(visuals, indent=1))
        print("BEFORE_HOVER", json.dumps(await page.evaluate(DUMP)))
        for v in visuals:
            if v["w"] < 100 or v["h"] < 100:
                continue
            await page.mouse.move(v["x"] + v["w"] / 2, v["y"] + v["h"] / 2)
            await page.wait_for_timeout(500)
            hits = await page.evaluate(DUMP)
            if hits:
                print("HOVER", v.get("title"), json.dumps(hits))
        await page.screenshot(path="probe_drill.png")
        await browser.close()


asyncio.run(main())
