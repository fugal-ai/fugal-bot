// SEED_WORKSPACE=1: put our starter files into the agent's workspace, and keep
// them current. Run by entrypoint.sh BEFORE render-config.mjs and before
// OpenClaw starts.
//
//   IDENTITY.md   who the agent is (name, emoji)
//   SOUL.md       two lines on tone
//   USER.md       what is known about the user (the public wallet address)
//   AGENTS.md     house rules (ask first, no wallet secrets, be economical)
//   BOOTSTRAP.md  the script for the first conversation; the agent deletes it
//                 when that conversation is done
//
// Rules:
//   - A missing file is written (BOOTSTRAP.md: see below).
//   - A file that exists is replaced by this image's version ONLY if its
//     content is still exactly what we shipped: the SHA-256 recorded when we
//     wrote it (~/.openclaw/fugal-seed.json), or any earlier shipped template
//     (seed-history.json) rendered with this bot's values. That is how
//     improved guidance reaches bots made by an older image. A file the user
//     (or the agent) changed in any way is never touched again: their edits
//     always win.
//   - BOOTSTRAP.md is written only into a brand-new data directory (no
//     OpenClaw config file yet). Once the agent has deleted it (retired), it
//     is never written again, and a restored backup carries the config file,
//     so a restore does not bring the first conversation back either. A
//     BOOTSTRAP.md that is still there and unedited is updated like the rest.
//   - OpenClaw (v2026.11.6) keeps files it finds: it only writes its own
//     templates for files that are missing, and it leaves a BOOTSTRAP.md that
//     differs from its stock one in place until it is deleted.
//   - They are short on purpose. OpenClaw sends every one of these files to
//     the model with EVERY request, so each line costs input tokens for the
//     life of the bot (BOOTSTRAP.md: until it is deleted).
//
// Template variables: {{NAME}} (BOT_DISPLAY_NAME), {{EMOJI}} (BOT_EMOJI),
// {{OWNER_WALLET}} (OWNER_WALLET_SS58: the PUBLIC Bittensor address the owner
// signed in with; not a secret). {{#X}}...{{/X}} is kept only when X is set,
// {{^X}}...{{/X}} only when it is not.
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

const env = process.env;
const seedDir = env.SEED_DIR || "/opt/fugal-bot/seed";
const historyPath = env.SEED_HISTORY || path.join(path.dirname(seedDir), "seed-history.json");
const workspace = env.OPENCLAW_WORKSPACE_DIR || "/home/node/workspace";
const cfgPath = env.OPENCLAW_CONFIG_PATH || "/home/node/.openclaw/openclaw.json";
const statePath = path.join(path.dirname(cfgPath), "fugal-seed.json");
const NAMES = ["IDENTITY.md", "SOUL.md", "USER.md", "AGENTS.md", "BOOTSTRAP.md"];

// One line, no characters that would change the structure of a Markdown
// prompt file, at most `max` whole characters.
const clean = (s, max) =>
  Array.from((s || "").replace(/[\u0000-\u001f\u007f`<>{}\\]+/g, " ").replace(/\s+/g, " ").trim())
    .slice(0, max).join("").trim();

const vars = {
  NAME: clean(env.BOT_DISPLAY_NAME, 64) || "Fugal Bot",
  EMOJI: clean(env.BOT_EMOJI, 16),
  OWNER_WALLET: (env.OWNER_WALLET_SS58 || "").trim(),
};
// An SS58 address is base58, 47 or 48 characters for Bittensor (prefix 42).
if (vars.OWNER_WALLET && !/^[1-9A-HJ-NP-Za-km-z]{46,49}$/.test(vars.OWNER_WALLET)) {
  console.error("[fugal-bot] OWNER_WALLET_SS58 does not look like an SS58 address; seeding without it");
  vars.OWNER_WALLET = "";
}

function render(text) {
  return text
    .replace(/\{\{([#^])([A-Z_]+)\}\}([\s\S]*?)\{\{\/\2\}\}/g, (_, kind, name, body) =>
      (kind === "#") === Boolean(vars[name]) ? body : "")
    .replace(/\{\{([A-Z_]+)\}\}/g, (m, name) => (name in vars ? vars[name] : m));
}
const sha = (s) => crypto.createHash("sha256").update(s).digest("hex");

function readJson(p, fallback) {
  try {
    const v = JSON.parse(fs.readFileSync(p, "utf8"));
    return v && typeof v === "object" && !Array.isArray(v) ? v : fallback;
  } catch {
    return fallback;
  }
}
const history = readJson(historyPath, {}).templates || {};
const state = readJson(statePath, {});
const recorded = state.files && typeof state.files === "object" ? state.files : {};
const nextState = { version: 1, files: { ...recorded } };

// Write `text` to `dest`: "wx" for a new file (fail rather than overwrite one
// that appeared meanwhile), else a temporary file renamed over the old one, so
// a crash leaves the old or the new content, never a mix.
function put(dest, text, replace) {
  if (!replace) return fs.writeFileSync(dest, text, { flag: "wx", mode: 0o600 });
  const tmp = `${dest}.fugal-seed-${process.pid}`;
  fs.writeFileSync(tmp, text, { flag: "w", mode: 0o600 });
  fs.renameSync(tmp, dest);
}

const fresh = !fs.existsSync(cfgPath);
fs.mkdirSync(workspace, { recursive: true });
const done = [];
for (const name of NAMES) {
  const dest = path.join(workspace, name);
  const text = render(fs.readFileSync(path.join(seedDir, name), "utf8"));
  const want = sha(text);
  // lstat: a dangling symlink also counts as "there".
  const st = fs.lstatSync(dest, { throwIfNoEntry: false });
  if (!st) {
    if (name === "BOOTSTRAP.md" && !fresh) {
      done.push(`${name}=skipped (not a new data directory)`);
      continue;
    }
    try {
      put(dest, text, false);
      nextState.files[name] = { sha256: want };
      done.push(`${name}=written`);
    } catch (e) {
      if (e.code !== "EEXIST") throw e;
      done.push(`${name}=kept`);
    }
    continue;
  }
  if (!st.isFile()) {
    done.push(`${name}=kept (not a plain file)`);
    continue;
  }
  const have = sha(fs.readFileSync(dest));
  if (have === want) {
    nextState.files[name] = { sha256: want };
    done.push(`${name}=current`);
    continue;
  }
  const shipped = new Set([recorded[name]?.sha256, ...(history[name] || []).map((t) => sha(render(t)))]);
  if (!shipped.has(have)) {
    done.push(`${name}=kept (edited)`);
    continue;
  }
  put(dest, text, true);
  nextState.files[name] = { sha256: want };
  done.push(`${name}=updated`);
}

try {
  fs.mkdirSync(path.dirname(statePath), { recursive: true, mode: 0o700 });
  const tmp = `${statePath}.tmp-${process.pid}`;
  fs.writeFileSync(tmp, `${JSON.stringify(nextState)}\n`, { mode: 0o600 });
  fs.renameSync(tmp, statePath);
} catch (e) {
  console.error(`[fugal-bot] could not record the seed files' hashes (${e.code || e.message}); continuing`);
}
console.log(`[fugal-bot] workspace seed: ${done.join(", ")}`);
