// Browser-level check that the bot's name, emoji and accent colour from the
// environment reach the real web chat (TEST ONLY). Run by scripts/smoke.sh in
// a throwaway container made from the bot image, like ui-check.mjs:
//   docker run --rm -i -e UI_BASE -e ACCESS_PASSWORD -e UI_EXPECT_NAME -e UI_EXPECT_EMOJI \
//     -e UI_EXPECT_ACCENT ... --entrypoint node <bot image> --input-type=module - < scripts/ui-identity-check.mjs
// Prints PASS/FAIL lines; exits 1 if any check failed.
import { createRequire } from "node:module";

const require = createRequire("/app/");
const { chromium } = require("playwright-core");

const BASE = (process.env.UI_BASE || "").replace(/\/+$/, "");
const PASSWORD = process.env.ACCESS_PASSWORD || "";
const NAME = process.env.UI_EXPECT_NAME || "";
const EMOJI = process.env.UI_EXPECT_EMOJI || "";
const ACCENT = (process.env.UI_EXPECT_ACCENT || "").toLowerCase();
let failed = 0;
const pass = (m) => console.log(`PASS  ${m}`);
const fail = (m) => { console.log(`FAIL  ${m}`); failed++; };

async function main() {
  const browser = await chromium.launch({ executablePath: "/usr/bin/chromium", headless: true, args: ["--no-sandbox"] });
  try {
    const page = await (await browser.newContext()).newPage();
    await page.goto(`${BASE}/`, { waitUntil: "domcontentloaded" });
    await page.fill('input[name="password"]', PASSWORD);
    await Promise.all([page.waitForNavigation({ waitUntil: "domcontentloaded" }), page.click('button[type="submit"]')]);
    const box = page.locator("textarea").first();
    await box.waitFor({ state: "visible", timeout: 60000 });
    // The identity arrives over the websocket shortly after the page connects.
    let seen = {};
    for (let i = 0; i < 20; i++) {
      await page.waitForTimeout(1500);
      seen = await page.evaluate(() => ({
        title: document.title,
        placeholder: document.querySelector("textarea")?.getAttribute("placeholder") || "",
        text: document.body.innerText,
        html: document.documentElement.outerHTML,
        accent: getComputedStyle(document.documentElement).getPropertyValue("--accent").trim().toLowerCase(),
      }));
      if (seen.title.includes(NAME) && seen.text.includes(NAME)) break;
    }
    console.log(`      page title: "${seen.title}"; message box: "${seen.placeholder}"; accent: ${seen.accent}`);
    if (seen.title.includes(NAME) && seen.text.includes(NAME)) pass(`browser: the web chat shows the bot's name ("${NAME}") in the page title and on the page`);
    else fail(`browser: the web chat does not show the name "${NAME}"`);
    if (EMOJI) {
      if (seen.html.includes(EMOJI)) pass(`browser: the bot's emoji (${EMOJI}) is on the page`);
      else fail(`browser: the emoji ${EMOJI} is not on the page`);
    }
    if (ACCENT) {
      if (seen.accent === ACCENT) pass(`browser: the accent colour is ${ACCENT}`);
      else fail(`browser: the accent colour is "${seen.accent}", expected ${ACCENT}`);
    }
  } finally {
    await browser.close();
  }
}

main().then(() => process.exit(failed ? 1 : 0), (e) => { console.log(`FAIL  ui-identity-check crashed: ${e.message}`); process.exit(1); });
