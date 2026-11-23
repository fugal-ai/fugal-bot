// Unit tests for seed-workspace.mjs (node, no dependencies, no network):
//   node bot/test_seed.mjs
// Each case runs the real script as a child process against a throwaway data
// directory, the way entrypoint.sh runs it.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const SCRIPT = path.join(here, "seed-workspace.mjs");
const SEED = path.join(here, "seed");
const HISTORY = path.join(here, "seed-history.json");
const NAMES = ["IDENTITY.md", "SOUL.md", "USER.md", "AGENTS.md", "BOOTSTRAP.md"];
const WALLET = "5GbydRVj98hQZvR2cf4t8WtcoUVjAuJykxxuqauiJpJqwB7m";
const sha = (s) => crypto.createHash("sha256").update(s).digest("hex");
const history = JSON.parse(fs.readFileSync(HISTORY, "utf8")).templates;
// The templates the bots running in production were seeded from: the image of
// feat/hosted f772dc0 (AGENTS.md of cad32b2, BOOTSTRAP.md of f6542c5).
const byHash = (name, prefix) => {
  const t = history[name].find((x) => sha(x).startsWith(prefix));
  assert.ok(t, `${name} ${prefix} is in seed-history.json`);
  return t;
};
const PREV = {
  "AGENTS.md": byHash("AGENTS.md", "a2dfd282"),
  "BOOTSTRAP.md": byHash("BOOTSTRAP.md", "92c5f4d8"),
};

// The renderer of seed-workspace.mjs (unchanged since the first seeded bots).
function render(text, vars) {
  return text
    .replace(/\{\{([#^])([A-Z_]+)\}\}([\s\S]*?)\{\{\/\2\}\}/g, (_, kind, name, body) =>
      (kind === "#") === Boolean(vars[name]) ? body : "")
    .replace(/\{\{([A-Z_]+)\}\}/g, (m, name) => (name in vars ? vars[name] : m));
}
const VARS = { NAME: "Smoke Otter", EMOJI: "🦦", OWNER_WALLET: WALLET };
const ENV = { BOT_DISPLAY_NAME: VARS.NAME, BOT_EMOJI: VARS.EMOJI, OWNER_WALLET_SS58: WALLET };
const current = (name, vars = VARS) => render(fs.readFileSync(path.join(SEED, name), "utf8"), vars);

function home() {
  const h = fs.mkdtempSync(path.join(os.tmpdir(), "fugal-seed-test-"));
  fs.mkdirSync(path.join(h, "workspace"));
  fs.mkdirSync(path.join(h, ".openclaw"));
  return h;
}
function run(h, env = ENV) {
  const out = execFileSync(process.execPath, [SCRIPT], {
    env: {
      PATH: process.env.PATH, SEED_DIR: SEED, SEED_HISTORY: HISTORY,
      OPENCLAW_WORKSPACE_DIR: path.join(h, "workspace"),
      OPENCLAW_CONFIG_PATH: path.join(h, ".openclaw", "openclaw.json"), ...env,
    },
    encoding: "utf8",
  });
  const line = out.split("\n").find((l) => l.includes("workspace seed:")) || "";
  return Object.fromEntries(line.replace(/.*workspace seed: /, "").split(", ").map((kv) => {
    const i = kv.indexOf("=");
    return [kv.slice(0, i), kv.slice(i + 1)];
  }));
}
const ws = (h, name) => path.join(h, "workspace", name);
const read = (h, name) => fs.readFileSync(ws(h, name), "utf8");
const write = (h, name, text) => fs.writeFileSync(ws(h, name), text);
const configExists = (h) => fs.writeFileSync(path.join(h, ".openclaw", "openclaw.json"), "{}\n");
const state = (h) => JSON.parse(fs.readFileSync(path.join(h, ".openclaw", "fugal-seed.json"), "utf8"));

const cases = [];
const test = (name, fn) => cases.push([name, fn]);

test("a new data directory gets all five files, and their hashes are recorded", () => {
  const h = home();
  const r = run(h);
  for (const n of NAMES) assert.equal(r[n], "written", n);
  for (const n of NAMES) assert.equal(read(h, n), current(n), n);
  for (const n of NAMES) assert.equal(state(h).files[n].sha256, sha(current(n)), n);
  assert.equal((fs.statSync(ws(h, "AGENTS.md")).mode & 0o777), 0o600);
  configExists(h);
  const again = run(h);
  for (const n of NAMES) assert.equal(again[n], "current", n);
});

test("production case 1: a bot seeded by the previous image, files unedited -> updated", () => {
  const h = home();
  configExists(h);   // an existing bot: OpenClaw's config is there; no fugal-seed.json yet
  for (const n of NAMES) write(h, n, current(n));
  write(h, "AGENTS.md", render(PREV["AGENTS.md"], VARS));
  write(h, "BOOTSTRAP.md", render(PREV["BOOTSTRAP.md"], VARS));
  assert.notEqual(read(h, "AGENTS.md"), current("AGENTS.md"), "the template did change");
  const r = run(h);
  assert.equal(r["AGENTS.md"], "updated");
  assert.equal(read(h, "AGENTS.md"), current("AGENTS.md"));
  assert.equal(r["BOOTSTRAP.md"], read(h, "BOOTSTRAP.md") === current("BOOTSTRAP.md") ? "current" : "updated");
  for (const n of ["IDENTITY.md", "SOUL.md", "USER.md"]) assert.equal(r[n], "current", n);
  assert.equal(state(h).files["AGENTS.md"].sha256, sha(current("AGENTS.md")));
});

test("production case 1b: the same bot after its first conversation (BOOTSTRAP.md retired) -> AGENTS.md updated, BOOTSTRAP.md not re-created", () => {
  const h = home();
  configExists(h);
  for (const n of ["IDENTITY.md", "SOUL.md", "USER.md"]) write(h, n, current(n));
  write(h, "AGENTS.md", render(PREV["AGENTS.md"], VARS));
  const r = run(h);
  assert.equal(r["AGENTS.md"], "updated");
  assert.match(r["BOOTSTRAP.md"], /^skipped/);
  assert.equal(fs.existsSync(ws(h, "BOOTSTRAP.md")), false);
});

test("production case 2: AGENTS.md was edited -> never touched; USER.md the agent wrote to -> kept", () => {
  const h = home();
  configExists(h);
  const edited = render(PREV["AGENTS.md"], VARS) + "\n## Learned\n\n- The user likes short answers.\n";
  write(h, "AGENTS.md", edited);
  const user = current("USER.md").replace("not known yet. Ask when a schedule needs it.", "America/New_York");
  write(h, "USER.md", user);
  for (const n of ["IDENTITY.md", "SOUL.md"]) write(h, n, current(n));
  const before = fs.statSync(ws(h, "AGENTS.md")).mtimeMs;
  for (let i = 0; i < 2; i++) {
    const r = run(h);
    assert.equal(r["AGENTS.md"], "kept (edited)");
    assert.equal(r["USER.md"], "kept (edited)");
  }
  assert.equal(read(h, "AGENTS.md"), edited);
  assert.equal(read(h, "USER.md"), user);
  assert.equal(fs.statSync(ws(h, "AGENTS.md")).mtimeMs, before);
  assert.equal(state(h).files["AGENTS.md"], undefined, "no hash is claimed for a file we did not write");
});

test("a one-character edit counts as an edit", () => {
  const h = home();
  configExists(h);
  write(h, "AGENTS.md", render(PREV["AGENTS.md"], VARS).replace("House rules", "House Rules"));
  assert.equal(run(h)["AGENTS.md"], "kept (edited)");
});

test("every earlier shipped template, rendered with this bot's values, is updated", () => {
  for (const n of ["AGENTS.md", "BOOTSTRAP.md"]) {
    for (const t of history[n]) {
      const h = home();
      configExists(h);
      write(h, n, render(t, VARS));
      const r = run(h);
      assert.ok(["updated", "current"].includes(r[n]), `${n} ${sha(t).slice(0, 8)}: ${r[n]}`);
      assert.equal(read(h, n), current(n));
    }
  }
});

test("an old template rendered with OTHER values (the name changed since) is left alone", () => {
  const h = home();
  configExists(h);
  write(h, "AGENTS.md", render(PREV["AGENTS.md"], { ...VARS, NAME: "Another Name" }));
  assert.equal(run(h)["AGENTS.md"], "kept (edited)");
});

test("without an owner address: the right variant is rendered and updated", () => {
  const h = home();
  configExists(h);
  const vars = { NAME: "Fugal Bot", EMOJI: "", OWNER_WALLET: "" };
  write(h, "AGENTS.md", render(PREV["AGENTS.md"], vars));
  const r = run(h, {});
  assert.equal(r["AGENTS.md"], "updated");
  assert.equal(read(h, "AGENTS.md"), current("AGENTS.md", vars));
});

test("the recorded hash: a file this script wrote (from any later template) is updated", () => {
  const h = home();
  configExists(h);
  const futureOld = "# AGENTS.md - a version no history list knows\n";
  write(h, "AGENTS.md", futureOld);
  fs.writeFileSync(path.join(h, ".openclaw", "fugal-seed.json"),
    JSON.stringify({ version: 1, files: { "AGENTS.md": { sha256: sha(futureOld) } } }));
  assert.equal(run(h)["AGENTS.md"], "updated");
  assert.equal(read(h, "AGENTS.md"), current("AGENTS.md"));
});

test("a symlink is never followed or replaced", () => {
  const h = home();
  configExists(h);
  fs.symlinkSync("/nonexistent/target", ws(h, "AGENTS.md"));
  assert.equal(run(h)["AGENTS.md"], "kept (not a plain file)");
  assert.equal(fs.readlinkSync(ws(h, "AGENTS.md")), "/nonexistent/target");
});

test("an unreadable state file is ignored (history still applies)", () => {
  const h = home();
  configExists(h);
  fs.writeFileSync(path.join(h, ".openclaw", "fugal-seed.json"), "not json");
  write(h, "AGENTS.md", render(PREV["AGENTS.md"], VARS));
  assert.equal(run(h)["AGENTS.md"], "updated");
  assert.equal(state(h).version, 1);
});

let failed = 0;
for (const [name, fn] of cases) {
  try {
    fn();
    console.log(`ok    ${name}`);
  } catch (e) {
    failed++;
    console.log(`FAIL  ${name}\n      ${e.message}`);
  }
}
console.log(`${cases.length - failed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
