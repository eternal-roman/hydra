/**
 * One-shot Playwright smoke for the Vite dashboard.
 * Usage: npx --yes playwright@1.55.0 test --config=...  OR
 *   node --experimental-vm-modules after `npx playwright install chromium`
 */
import { chromium } from "playwright";

const url = process.env.HYDRA_DASH_URL || "http://127.0.0.1:4173";
const jwt = (process.env.HYDRA_SMOKE_JWT || "").trim();
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errors = [];
page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
page.on("console", (msg) => {
  if (msg.type() === "error") errors.push(`console: ${msg.text()}`);
});

if (jwt) {
  await page.addInitScript((token) => {
    localStorage.setItem("hydra_jwt", token);
  }, jwt);
}

await page.goto(url, { waitUntil: "domcontentloaded", timeout: 20000 });
await page.waitForTimeout(1500);

const body = await page.locator("body").innerText();
if (!body || body.trim().length < 8) {
  errors.push("empty body");
}

// Tabs if the live shell mounted (JWT wall may show login first — still a render).
const tabLabels = ["LIVE", "RESEARCH", "SETTINGS"];
for (const label of tabLabels) {
  const tab = page.getByText(label, { exact: true }).first();
  if (await tab.count()) {
    await tab.click();
    await page.waitForTimeout(400);
  }
}

await browser.close();
if (errors.length) {
  console.error("SMOKE FAIL");
  for (const e of errors) console.error(" -", e);
  process.exit(1);
}
console.log("SMOKE OK", url);
process.exit(0);
