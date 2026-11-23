// Browser-level check of the web chat through the password proxy (TEST ONLY).
// Run by scripts/smoke.sh inside a throwaway container made from the bot image
// (it already has Chromium and playwright-core), pointed at the proxy's
// published port like a real browser:
//   docker run --rm -i -e UI_BASE -e ACCESS_PASSWORD -e UI_MESSAGE ... --entrypoint node <bot image> - < scripts/ui-check.mjs
// Checks:
//   1. websocket handshake with a SAME-origin Origin gets past the origin check
//   2. the same with a CROSS-site Origin is refused for its origin
//   3. real Chromium: log in, the chat page connects (no "origin not allowed"),
//      a message sent from the page gets an assistant reply.
// With UI_SSO=1 (the gate runs with GATE_SSO=on) the same checks run the way a
// hosted bot is used: signed in by a one-time token, inside a frame on the
// page of another origin of the same site, over HTTPS (scripts/sso-edge.Caddyfile
// is the stand-in for the site and for what ends TLS). Added checks: what the
// frame does without a session, the cookie, who may frame the bot, what a
// sibling host can do with the user's cookie, sign-out.
// With UI_EXPECT_NAME (and optionally UI_EXPECT_EMOJI, UI_EXPECT_ACCENT) it also
// checks that the bot's name, emoji and accent colour show in the chat page,
// wherever that page is (in hosted mode: inside the frame).
// UI_DESKTOP_WAIT (seconds, default 45) is how long /desktop/ may take to show
// the screen; raise it for a slow host.
// Prints PASS/FAIL lines; exits 1 if any check failed.
import { createHmac, createHash, randomBytes } from "node:crypto";
import { createRequire } from "node:module";

const require = createRequire("/app/");
const WebSocket = require("ws");
const { chromium } = require("playwright-core");

const BASE = (process.env.UI_BASE || "").replace(/\/+$/, "");
const PASSWORD = process.env.ACCESS_PASSWORD || "";
const MODEL = process.env.UI_MODEL || ""; // e.g. "scripted/tools": sent as "/model ..."
const MESSAGE = process.env.UI_MESSAGE || "Please use your computer, then tell me you are done.";
const EXPECT = process.env.UI_EXPECT || ""; // text the reply must contain ("" = any reply)
// How long /desktop/ may take to show the bot's screen, in seconds. The first
// connection took 8 to 10 s in a sandbox room; the check ends as soon as it is there.
const DESKTOP_WAIT = Math.max(1, Number(process.env.UI_DESKTOP_WAIT) || 45) * 1000;
// ---- hosted sign-in (UI_SSO=1) ---------------------------------------------------
const SSO = process.env.UI_SSO === "1";
const SITE = (process.env.UI_SSO_SITE || "").replace(/\/+$/, "");     // the page around the bot
const BOT = (process.env.UI_SSO_BOT || "").replace(/\/+$/, "");       // the bot, as the browser reaches it
const SIBLING = (process.env.UI_SSO_SIBLING || "").replace(/\/+$/, ""); // another host of the same site
const STRANGER = (process.env.UI_SSO_STRANGER || "").replace(/\/+$/, ""); // another site
const BOT_ID = process.env.GATE_BOT_ID || "";
const COOKIE = "__Host-pb_session";
// ---- identity (optional) ----------------------------------------------------------
const NAME = process.env.UI_EXPECT_NAME || "";
const EMOJI = process.env.UI_EXPECT_EMOJI || "";
const ACCENT = (process.env.UI_EXPECT_ACCENT || "").toLowerCase();
let failed = 0;
const pass = (m) => console.log(`PASS  ${m}`);
const fail = (m) => { console.log(`FAIL  ${m}`); failed++; };

// A token as the site's controller makes it; ACCESS_PASSWORD is a throwaway here.
function mintToken(ttl = 60) {
  const signed = `v1.${BOT_ID}.${Math.floor(Date.now() / 1000) + ttl}.${randomBytes(16).toString("hex")}`;
  const key = createHash("sha256").update("fugal-bot-gate-sso-v1\0").update(PASSWORD).digest();
  return `${signed}.${createHmac("sha256", key).update(signed).digest("base64url")}`;
}

async function login() {
  if (SSO) {
    const r = await fetch(`${BASE}/_gate/sso`, {
      method: "POST",
      redirect: "manual",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ token: mintToken(), next: "/" }),
    });
    const c = (r.headers.get("set-cookie") || "").split(";")[0];
    if (r.status !== 303 || !c.startsWith(`${COOKIE}=`)) throw new Error(`sign-in by token failed: ${r.status}`);
    return c;
  }
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

// The name, emoji and accent colour from the environment, as the chat page
// shows them. They arrive over the websocket shortly after the page connects.
async function identityChecks(page, view) {
  const where = SSO ? "in the frame, " : "";
  let seen = {};
  for (let i = 0; i < 20; i++) {
    seen = await view.evaluate(() => ({
      title: document.title,
      text: document.body.innerText,
      html: document.documentElement.outerHTML,
      accent: getComputedStyle(document.documentElement).getPropertyValue("--accent").trim().toLowerCase(),
    })).catch(() => ({ title: "", text: "", html: "", accent: "" }));
    if (seen.title.includes(NAME) && seen.text.includes(NAME)) break;
    await page.waitForTimeout(1500);
  }
  console.log(`      chat page title: "${seen.title}"; accent: ${seen.accent}`);
  if (seen.title.includes(NAME) && seen.text.includes(NAME)) pass(`browser: ${where}the web chat shows the bot's name ("${NAME}") in the page title and on the page`);
  else fail(`browser: ${where}the web chat does not show the name "${NAME}"`);
  if (EMOJI) {
    if (seen.html.includes(EMOJI)) pass(`browser: ${where}the bot's emoji (${EMOJI}) is on the page`);
    else fail(`browser: ${where}the emoji ${EMOJI} is not on the page`);
  }
  if (ACCENT) {
    if (seen.accent === ACCENT) pass(`browser: ${where}the accent colour is ${ACCENT}`);
    else fail(`browser: ${where}the accent colour is "${seen.accent}", expected ${ACCENT}`);
  }
}

// ---- hosted: the bot inside a frame on the site's page ---------------------------------
// What the site's page does with the reply of its controller: put the one-time
// token in a hidden form and post it into the frame.
const postToken = (page, next) => page.evaluate(({ action, token, next }) => {
  const f = document.getElementById("botOpen");
  f.action = action; f.target = "fugalbot";
  f.elements.namedItem("token").value = token; f.elements.namedItem("next").value = next;
  f.submit();
}, { action: `${BOT}/_gate/sso`, token: mintToken(), next });

async function openFramed(context, page) {
  await page.addInitScript(() => {
    if (window.top !== window) return;
    window.gateMessages = [];
    window.addEventListener("message", (e) => window.gateMessages.push({ origin: e.origin, type: e.data && e.data.type }));
  });
  await page.goto(`${SITE}/bot`, { waitUntil: "domcontentloaded" });
  const frame = page.frame({ name: "fugalbot" });

  // No session yet: the frame must stay on the bot and tell the page around it.
  await page.evaluate((u) => { document.getElementById("botFrame").src = u; }, `${BOT}/`);
  let told = [];
  try {
    await page.waitForFunction(() => window.gateMessages.some((m) => m.type === "fugal-bot:auth-required"), null, { timeout: 20000 });
    told = await page.evaluate(() => window.gateMessages);
  } catch { /* reported below */ }
  const shown = await frame.locator("body").innerText().catch(() => "");
  console.log(`      messages to the page: ${JSON.stringify(told)}; frame at ${frame.url()}`);
  if (told.length === 1 && told[0].origin === BOT && told[0].type === "fugal-bot:auth-required" && frame.url() === `${BOT}/` && /Signing you in/.test(shown)) {
    pass("hosted: a frame without a session stays on the bot, shows \"Signing you in\" and tells the page around it (one message, from the bot's origin)");
  } else fail("hosted: a frame without a session did not report auth-required to the page around it");

  await postToken(page, "/");
  await frame.locator("textarea").first().waitFor({ state: "visible", timeout: 60000 });
  pass("hosted: the token posted into the frame signs it in and the chat opens there");

  const mine = (await context.cookies(BOT)).filter((c) => c.name === COOKIE);
  const c = mine[0] || {};
  console.log(`      cookie: ${JSON.stringify({ ...c, value: c.value ? "(set)" : "" })}`);
  if (mine.length === 1 && c.secure && c.httpOnly && c.sameSite === "Strict" && c.path === "/" && c.domain === new URL(BOT).hostname) {
    pass(`hosted: the browser kept ${COOKIE} (Secure, HttpOnly, SameSite=Strict, Path=/, this host only)`);
  } else fail(`hosted: ${COOKIE} is missing or has the wrong attributes`);
  const elsewhere = [...await context.cookies(SITE), ...await context.cookies(SIBLING), ...await context.cookies(STRANGER)].filter((x) => x.name === COOKIE);
  if (elsewhere.length === 0) pass("hosted: the cookie is not sent to the site, to a sibling host or to another site");
  else fail("hosted: the cookie would be sent to another host");
  return frame;
}

// Run after the chat and desktop checks, with the session cookie still in the browser.
async function hostedBoundaries(browser, context, page) {
  // Who may frame the bot: only the configured page (shown above, the chat ran in it).
  for (const [who, origin] of [["a sibling host of the same site", SIBLING], ["another site", STRANGER]]) {
    const p = await context.newPage();
    const logs = [];
    p.on("console", (m) => logs.push(m.text()));
    await p.goto(`${origin}/`, { waitUntil: "domcontentloaded" });
    await p.evaluate((u) => { document.getElementById("botFrame").src = u; }, `${BOT}/`);
    await p.waitForTimeout(5000);
    const f = p.frame({ name: "fugalbot" });
    const boxes = await f.locator("textarea").count().catch(() => 0);
    const text = await f.locator("body").innerText().catch(() => "");
    const refused = logs.some((l) => /frame-ancestors/.test(l)) || f.url().startsWith("chrome-error:");
    console.log(`      framed by ${origin}: frame at ${f.url()}; ${logs.filter((l) => /frame-ancestors/.test(l)).join(" | ").slice(0, 260)}`);
    if (refused && boxes === 0 && !/Signing you in/.test(text)) pass(`hosted: ${who} cannot frame the bot (frame-ancestors)`);
    else fail(`hosted: ${who} could frame the bot`);

    if (origin === SIBLING) {
      // Any host under the domain may set a cookie for the whole domain. The
      // __Host- name is what keeps a sibling from replacing the session cookie.
      const mine = (await context.cookies(BOT)).find((c) => c.name === COOKIE);
      await p.evaluate((d) => {
        document.cookie = `__Host-pb_session=planted; Domain=${d}; Path=/; Secure`;
        document.cookie = `pb_session=planted; Domain=${d}; Path=/; Secure`;
      }, new URL(SITE).hostname);
      const now = await context.cookies(BOT);
      const kept = now.filter((c) => c.name === COOKIE);
      console.log(`      cookies planted from the sibling host for the whole domain, as the bot's host sees them: ${JSON.stringify(now.filter((c) => c.value === "planted").map((c) => c.name))}`);
      if (mine && kept.length === 1 && kept[0].value === mine.value) pass(`hosted: a sibling host cannot plant or replace ${COOKIE} (a cookie without the prefix it can)`);
      else fail(`hosted: a sibling host changed ${COOKIE}`);

      // Same site, so the browser DOES send the user's cookie from here. The
      // gate must refuse everything but a plain navigation.
      const seen = [];
      p.on("response", (r) => { if (r.url().startsWith(`${BOT}/v1/`)) seen.push(r.status()); });
      await p.evaluate((u) => fetch(`${u}/v1/chat/completions`, {
        method: "POST", mode: "no-cors", credentials: "include", headers: { "content-type": "text/plain" },
        body: JSON.stringify({ model: "openclaw/default", messages: [{ role: "user", content: "from a sibling host" }] }),
      }).then(() => "sent", (e) => `error ${e.message}`), BOT);
      await p.waitForTimeout(1000);
      console.log(`      POST to the bot's API from the sibling host, with the user's cookie -> ${JSON.stringify(seen)}`);
      if (seen.length === 1 && seen[0] === 403) pass("hosted: a sibling host cannot call the bot's API with the user's cookie (403 at the gate)");
      else fail("hosted: a request from a sibling host was not refused by the gate");
      const ws = await p.evaluate((u) => new Promise((resolve) => {
        const w = new WebSocket(`${u.replace(/^http/, "ws")}/desktop/websockify`, ["binary"]);
        w.onopen = () => { w.close(); resolve("opened"); };
        w.onerror = () => resolve("refused");
        setTimeout(() => resolve("timeout"), 8000);
      }), BOT);
      console.log(`      desktop websocket from the sibling host, with the user's cookie -> ${ws}`);
      if (ws === "refused") pass("hosted: a sibling host cannot open the desktop websocket with the user's cookie");
      else fail("hosted: a sibling host opened the desktop websocket");
    }
    await p.close();
  }

  // Sign-out, as the site's page does it when the wallet signs out.
  await page.evaluate((u) => fetch(`${u}/_gate/logout`, { method: "POST", mode: "no-cors", credentials: "include" }).then(() => "ok", (e) => `error ${e.message}`), BOT);
  const left = (await context.cookies(BOT)).filter((c) => c.name === COOKIE);
  if (left.length === 0) pass("hosted: POST /_gate/logout from the site's page removes the cookie");
  else fail("hosted: the cookie is still there after sign-out");

  // Opened directly (not in a frame) without a session: on to the site's page.
  const fresh = await browser.newContext({ ignoreHTTPSErrors: true });
  try {
    const p = await fresh.newPage();
    await p.goto(`${BOT}/`, { waitUntil: "domcontentloaded" });
    await p.waitForURL(`${SITE}/bot`, { timeout: 15000 }).catch(() => {});
    console.log(`      top-level without a session: ended at ${p.url()}`);
    if (p.url() === `${SITE}/bot`) pass("hosted: opened directly without a session, the browser goes to the site's page (GATE_LOGIN_URL)");
    else fail("hosted: opened directly without a session, the browser was not sent to GATE_LOGIN_URL");
  } finally {
    await fresh.close();
  }
}

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
  const browser = await chromium.launch({ executablePath: "/usr/bin/chromium", headless: true,
    args: ["--no-sandbox", ...(SSO ? ["--ignore-certificate-errors"] : [])] });
  try {
    const context = await browser.newContext({ ignoreHTTPSErrors: SSO });
    const page = await context.newPage();
    const logs = [];
    page.on("console", (m) => logs.push(m.text()));
    // `view` is where the bot's own pages are: the page itself, or (hosted)
    // the frame on the site's page.
    let view = page.mainFrame();
    if (SSO) {
      view = await openFramed(context, page);
    } else {
      await page.goto(`${BASE}/`, { waitUntil: "domcontentloaded" });
      await page.fill('input[name="password"]', PASSWORD);
      await Promise.all([page.waitForNavigation({ waitUntil: "domcontentloaded" }), page.click('button[type="submit"]')]);
    }
    const box = view.locator("textarea").first();
    await box.waitFor({ state: "visible", timeout: 60000 });
    await page.waitForTimeout(3000);
    const body0 = await view.locator("body").innerText();
    if (/origin not allowed/i.test(body0) || logs.some((l) => /origin not allowed|origin-mismatch/i.test(l))) {
      fail("browser: chat page shows 'origin not allowed'");
    } else {
      pass("browser: chat page loaded and connected (no origin error)");
    }
    if (NAME) await identityChecks(page, view);
    const send = async (text) => { await box.fill(text); await box.press("Enter"); };
    if (MODEL) { await send(`/model ${MODEL}`); await page.waitForTimeout(4000); }
    // A finished assistant turn shows "Done in N seconds" under the reply. A
    // session that already holds a finished turn shows only the newest such
    // line, so counting them never goes up there: instead our message must be
    // on the page once more than before, with a "Done in" after it.
    const occurrences = (t, x) => t.split(x).length - 1;
    const sentBefore = occurrences(await view.locator("body").innerText(), MESSAGE);
    const answered = (t) => occurrences(t, MESSAGE) > sentBefore && /Done in \d+/.test(t.slice(t.lastIndexOf(MESSAGE)));
    await send(MESSAGE);
    const t0 = Date.now();
    let ok = false, text = "";
    while (Date.now() - t0 < 240000) {
      await page.waitForTimeout(3000);
      text = await view.locator("body").innerText();
      if (answered(text) && (!EXPECT || text.includes(EXPECT))) { ok = true; break; }
    }
    if (ok) pass(`browser: message sent from the page got a reply${EXPECT ? ` containing "${EXPECT}"` : ""}`);
    else { fail("browser: no reply in the page within 240 s"); console.log(text.slice(-800)); }

    // The desktop page: noVNC must connect on its own (VNC password comes in
    // the URL fragment) without asking for credentials.
    if (SSO) await postToken(page, "/desktop/");   // a fresh token with next=/desktop/, as the site does
    else await page.goto(`${BASE}/desktop/`, { waitUntil: "domcontentloaded" });
    const look = () => view.evaluate(() => ({
      url: location.pathname,
      askedForPassword: !!document.querySelector("#noVNC_credentials_dlg.noVNC_open"),
      canvas: [...document.querySelectorAll("#noVNC_container canvas, canvas")].some((c) => c.width > 100),
      status: (document.querySelector("#noVNC_status")?.textContent || "").trim().slice(0, 80),
    })).catch(() => ({ url: "", askedForPassword: false, canvas: false, status: "(page still loading)" }));
    const connected = (v) => v.url.startsWith("/desktop/") && v.canvas && !v.askedForPassword;
    let vnc = await look();
    for (const t1 = Date.now(); !connected(vnc) && Date.now() - t1 < DESKTOP_WAIT;) {
      await page.waitForTimeout(1000);
      vnc = await look();
    }
    console.log(`      desktop page: ${JSON.stringify(vnc)}`);
    if (connected(vnc)) pass("browser: /desktop/ connected to the bot's screen without a password prompt");
    else fail("browser: /desktop/ did not connect");

    if (SSO) await hostedBoundaries(browser, context, page);
  } finally {
    await browser.close();
  }
}

main().then(() => process.exit(failed ? 1 : 0), (e) => { console.log(`FAIL  ui-check crashed: ${e.message}`); process.exit(1); });
