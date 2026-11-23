// SEED_WORKSPACE=1: put our starter files into the agent's workspace. Run by
// entrypoint.sh BEFORE render-config.mjs and before OpenClaw starts.
//
//   IDENTITY.md   who the agent is (name, emoji)
//   SOUL.md       two lines on tone
//   USER.md       what is known about the user (the public wallet address)
//   AGENTS.md     house rules (ask first, no wallet secrets, be economical)
//   BOOTSTRAP.md  the script for the first conversation; the agent deletes it
//                 when that conversation is done
//
// Rules:
//   - A file that exists is never touched, whatever is in it: the user's (or
//     the agent's) edits always win.
//   - BOOTSTRAP.md is written only into a brand-new data directory (no
//     OpenClaw config file yet). Otherwise it would come back at every start
//     after the agent deleted it, and the bot would introduce itself again. A
//     restored backup carries the config file, so a restore does not bring the
//     first conversation back either.
//   - OpenClaw (v2026.11.6) keeps files it finds: it only writes its own
//     templates for files that are missing, and it leaves a BOOTSTRAP.md that
//     differs from its stock one in place until it is deleted.
//   - They are short on purpose. OpenClaw sends every one of these files to
//     the model with EVERY request, so each line costs input tokens for the
//     life of the bot (BOOTSTRAP.md: until it is deleted). All five together
//     are about a third of the size of OpenClaw's own templates.
//
// Template variables: {{NAME}} (BOT_DISPLAY_NAME), {{EMOJI}} (BOT_EMOJI),
// {{OWNER_WALLET}} (OWNER_WALLET_SS58: the PUBLIC Bittensor address the owner
// signed in with; not a secret). {{#X}}...{{/X}} is kept only when X is set,
// {{^X}}...{{/X}} only when it is not.
import fs from "node:fs";
import path from "node:path";

const env = process.env;
const seedDir = env.SEED_DIR || "/opt/fugal-bot/seed";
const workspace = env.OPENCLAW_WORKSPACE_DIR || "/home/node/workspace";
const cfgPath = env.OPENCLAW_CONFIG_PATH || "/home/node/.openclaw/openclaw.json";

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

const fresh = !fs.existsSync(cfgPath);
fs.mkdirSync(workspace, { recursive: true });
const done = [];
for (const name of ["IDENTITY.md", "SOUL.md", "USER.md", "AGENTS.md", "BOOTSTRAP.md"]) {
  const dest = path.join(workspace, name);
  // lstat: a dangling symlink also counts as "there".
  if (fs.lstatSync(dest, { throwIfNoEntry: false })) {
    done.push(`${name}=kept`);
    continue;
  }
  if (name === "BOOTSTRAP.md" && !fresh) {
    done.push(`${name}=skipped (not a new data directory)`);
    continue;
  }
  const text = render(fs.readFileSync(path.join(seedDir, name), "utf8"));
  try {
    // "wx": fail rather than overwrite if the file appeared meanwhile.
    fs.writeFileSync(dest, text, { flag: "wx", mode: 0o600 });
    done.push(`${name}=written`);
  } catch (e) {
    if (e.code !== "EEXIST") throw e;
    done.push(`${name}=kept`);
  }
}
console.log(`[fugal-bot] workspace seed: ${done.join(", ")}`);
