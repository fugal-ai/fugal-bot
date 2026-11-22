// Browser-level check of the web chat through the password proxy (TEST ONLY).
// Run by scripts/smoke.sh inside a throwaway container made from the bot image
// (it already has Chromium and playwright-core), pointed at the proxy's
// published port like a real browser:
//   docker run --rm -i -e UI_BASE -e ACCESS_PASSWORD -e UI_MODEL ... --entrypoint node <bot image> - < scripts/ui-check.mjs
// Checks:
//   1. websocket handshake with a SAME-origin Origin gets past the origin check
//   2. the same with a CROSS-site Origin is refused for its origin
//   3. real Chromium: log in, the chat page connects (no "origin not allowed"),
//      a message sent from the page gets an assistant reply.
// Prints PASS/FAIL lines; exits 1 if any check failed.
import { createRequire } from "node:module";

const require = createRequire("/app/");
const WebSocket = require("ws");
const { chromium } = require("playwright-core");

const BASE = (process.env.UI_BASE || "").replace(/\/+$/, "");
const PASSWORD = process.env.ACCESS_PASSWORD || "";
const MODEL = process.env.UI_MODEL || ""; // e.g. "scripted/tools": sent as "/model ..."
const MESSAGE = process.env.UI_MESSAGE || "Please use your computer, then tell me you are done.";
const EXPECT = process.env.UI_EXPECT || ""; // text the reply must contain ("" = any reply)
let failed = 0;
const pass = (m) => console.log(`PASS  ${m}`);
const fail = (m) => { console.log(`FAIL  ${m}`); failed++; };

async function login() {
  const r = await fetch(`${BASE}/_gate/login`, {
    method: "POST",
    redirect: "manual",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ password: PASSWORD, next: "/" }),
  });
  const c = (r.headers.get("set-cookie") || "").split(";")[0];
  if (r.status !== 303 || !c) throw new Error(`login failed: ${r.status}`);
  return c;
}

// Opens the gateway websocket with the given Origin, answers nothing but a
// minimal connect request, and returns the first error text the gateway sends
// (or "" if it got past the origin check to a different error / success).
function wsProbe(cookie, origin) {
  return new Promise((resolve) => {
    const url = BASE.replace(/^http/, "ws") + "/";
    const ws = new WebSocket(url, { headers: { Cookie: cookie, Origin: origin } });
    const done = (v) => { try { ws.close(); } catch {} resolve(v); };
    const t = setTimeout(() => done("timeout"), 15000);
    ws.on("unexpected-response", (_req, res) => { clearTimeout(t); done(`http ${res.statusCode}`); });
    ws.on("error", (e) => { clearTimeout(t); done(`error ${e.message}`); });
    ws.on("message", (buf) => {
      let m;
      try { m = JSON.parse(String(buf)); } catch { return; }
      if (m.type === "event" && m.event === "connect.challenge") {
        ws.send(JSON.stringify({
          type: "req", id: "probe-1", method: "connect",
          params: { minProtocol: 1, maxProtocol: 99, client: { id: "openclaw-control-ui", mode: "webchat", version: "probe", platform: "web" }, role: "operator", scopes: ["operator.read"] },
        }));
      } else if (m.type === "res") {
        clearTimeout(t);
        done(m.ok ? "ok" : JSON.stringify(m.error || m).slice(0, 300));
      }
    });
    ws.on("close", (code, reason) => { clearTimeout(t); done(`closed ${code} ${String(reason).slice(0, 200)}`); });
  });
}

const isOriginError = (s) => /origin/i.test(s);

async function main() {
  const cookie = await login();
  const u = new URL(BASE);

  const same = await wsProbe(cookie, u.origin);
  console.log(`      same-origin connect -> ${same}`);
  if (!isOriginError(same) && !same.startsWith("http ") && same !== "timeout") pass("websocket: same-origin browser Origin accepted by the origin check");
  else fail("websocket: same-origin browser Origin refused");

  const cross = await wsProbe(cookie, "https://evil.example");
  console.log(`      cross-site connect -> ${cross}`);
  if (isOriginError(cross) || cross.startsWith("http 403")) pass("websocket: cross-site Origin refused");
  else fail("websocket: cross-site Origin NOT refused");

  // Real browser.
  const browser = await chromium.launch({ executablePath: "/usr/bin/chromium", headless: true, args: ["--no-sandbox"] });
  try {
    const page = await (await browser.newContext()).newPage();
    const logs = [];
    page.on("console", (m) => logs.push(m.text()));
    await page.goto(`${BASE}/`, { waitUntil: "domcontentloaded" });
    await page.fill('input[name="password"]', PASSWORD);
    await Promise.all([page.waitForNavigation({ waitUntil: "domcontentloaded" }), page.click('button[type="submit"]')]);
    const box = page.locator("textarea").first();
    await box.waitFor({ state: "visible", timeout: 60000 });
    await page.waitForTimeout(3000);
    const body0 = await page.locator("body").innerText();
    if (/origin not allowed/i.test(body0) || logs.some((l) => /origin not allowed|origin-mismatch/i.test(l))) {
      fail("browser: chat page shows 'origin not allowed'");
    } else {
      pass("browser: chat page loaded and connected (no origin error)");
    }
    const send = async (text) => { await box.fill(text); await box.press("Enter"); };
    if (MODEL) { await send(`/model ${MODEL}`); await page.waitForTimeout(4000); }
    const before = (await page.locator("body").innerText()).length;
    await send(MESSAGE);
    const t0 = Date.now();
    let ok = false, text = "";
    while (Date.now() - t0 < 240000) {
      await page.waitForTimeout(3000);
      text = await page.locator("body").innerText();
      if (EXPECT ? text.includes(EXPECT) : text.length > before + MESSAGE.length + 20) { ok = true; break; }
    }
    if (ok) pass(`browser: message sent from the page got a reply${EXPECT ? ` containing "${EXPECT}"` : ""}`);
    else { fail("browser: no reply in the page within 240 s"); console.log(text.slice(-800)); }

    // The desktop page: noVNC must connect on its own (VNC password comes in
    // the URL fragment) without asking for credentials.
    await page.goto(`${BASE}/desktop/`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(8000);
    const vnc = await page.evaluate(() => ({
      url: location.pathname,
      askedForPassword: !!document.querySelector("#noVNC_credentials_dlg.noVNC_open"),
      canvas: [...document.querySelectorAll("#noVNC_container canvas, canvas")].some((c) => c.width > 100),
      status: (document.querySelector("#noVNC_status")?.textContent || "").trim().slice(0, 80),
    }));
    console.log(`      desktop page: ${JSON.stringify(vnc)}`);
    if (vnc.canvas && !vnc.askedForPassword) pass("browser: /desktop/ connected to the bot's screen without a password prompt");
    else fail("browser: /desktop/ did not connect");
  } finally {
    await browser.close();
  }
}

main().then(() => process.exit(failed ? 1 : 0), (e) => { console.log(`FAIL  ui-check crashed: ${e.message}`); process.exit(1); });
