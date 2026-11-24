// Build-time patches of OpenClaw's built files (bot/Dockerfile, prep stage):
//   node patch-openclaw.mjs /app
//
// Each patch is an exact string replacement in one built file. The file is
// found by content (bundle file names carry build hashes), and the expected
// text must occur exactly once in exactly one file of the given directory, or
// the build stops: an upstream change to any patched spot is then looked at
// instead of being shipped unpatched. A compressed copy that the gateway
// serves next to a patched UI file (<file>.gz, <file>.br) is written again
// from the patched text.
//
// Only what cannot be set by configuration is patched here (checked against
// OpenClaw v2026.11.7's config schema). Every patch hides or rewords
// something that cannot work in a hosted bot; none widens what the bot can do.
//
// Browser caching: the gateway serves dist/control-ui/assets/* as immutable
// under their hashed names. A patched file keeps its name, so a browser that
// already holds the unpatched file of the SAME OpenClaw version keeps it.
// Change a UI patch only together with an OpenClaw upgrade (new file names),
// or accept that cached browsers see the change late. index.html is always
// revalidated, so the style block below takes effect at once.
import fs from "node:fs";
import path from "node:path";
import zlib from "node:zlib";

export const PATCHES = [
  // --- Plugins page -------------------------------------------------------------
  // The marketplace (ClawHub) is switched off: OPENCLAW_CLAWHUB_URL points at a
  // dead address and every install is blocked by security.installPolicy. The
  // categories request then failed ("Plugin categories are unavailable: fetch
  // failed | bad port ... Retry to reconnect to ClawHub"). Answer it with no
  // categories instead of trying the network; the built-in category chips stay.
  {
    id: "plugins-categories-offline",
    dir: "dist",
    ext: ".mjs",
    find: "e(true, { categories: await fetchClawHubPluginCategories() }, void 0);",
    replace: "e(true, { categories: [] }, void 0);",
  },
  // The plugin list falls back to installed plugins; say why in plain words
  // instead of "ClawHub is unavailable: fetch failed | bad port".
  {
    id: "plugins-catalog-offline-note",
    dir: "dist",
    ext: ".mjs",
    find: "remoteError: `ClawHub is unavailable: ${formatErrorMessage(n)}.${",
    replace: "remoteError: `Installing plugins is turned off on this bot.${",
  },
  {
    id: "plugins-detail-offline-note",
    dir: "dist",
    ext: ".mjs",
    find: "o = `ClawHub details are unavailable: ${formatErrorMessage(e)}. Showing installed plugin metadata.`;",
    replace: "o = `Installing plugins is turned off on this bot. Showing installed plugin metadata.`;",
  },

  // --- Systems > Backups ----------------------------------------------------------
  // Fugal backs every bot up itself (an encrypted copy outside the bot at each
  // daily restart); the panel told the user to run a command they cannot run.
  {
    id: "backups-no-cli-hint",
    dir: "dist/control-ui/assets",
    ext: ".js",
    find: "`<p class=systems-backups__hint> <code>openclaw backup enable --to &lt;location&gt;`",
    replace: "`<p class=systems-backups__hint> `",
  },
  {
    id: "backups-hosted-text",
    dir: "dist/control-ui/assets",
    ext: ".js",
    find: "empty:`No backups recorded —`",
    replace: "empty:`Fugal makes an encrypted backup of this bot every day, kept outside the bot. Nothing to set up here.`",
  },

  // --- Automations ------------------------------------------------------------------
  // A new automation ran in an isolated session with no delivery, so its result
  // never reached the web chat (a web-only bot has no channel to announce to:
  // "Channel is required ... Run openclaw channels add"). Start new ones in the
  // main session instead: the run is a turn of the main conversation, where the
  // owner reads it. "Isolated session" stays one click away in the form.
  {
    id: "automations-default-main-session",
    dir: "dist/control-ui/assets",
    ext: ".js",
    find: "triggerOnce:!1,sessionTarget:`isolated`,wakeMode:`now`",
    replace: "triggerOnce:!1,sessionTarget:`main`,wakeMode:`now`",
  },
  {
    id: "automations-starters-main-session",
    dir: "dist/control-ui/assets",
    ext: ".js",
    find: "payloadKind:`agentTurn`,sessionTarget:`isolated`,wakeMode:`now`,deleteAfterRun:!1",
    replace: "payloadKind:`agentTurn`,sessionTarget:`main`,wakeMode:`now`,deleteAfterRun:!1",
  },

  // --- Chat composer: microphone ------------------------------------------------------
  // Dictation and Talk send audio to a speech-to-text provider configured on
  // the gateway; a hosted bot has none (talk_voice and tts are denied too), so
  // the button could only fail. Hidden with a style rule in index.html (style-src
  // allows inline styles; the page is revalidated on every load).
  {
    id: "composer-hide-microphone",
    dir: "dist/control-ui",
    ext: ".html",
    find: "  </head>",
    replace:
      '    <style id="fugal-hosted">.chat-talk-control,.chat-mobile-dictation-action,.chat-mobile-talk-action{display:none!important}</style>\n  </head>',
  },
];

function compressAgain(file, text) {
  const written = [];
  if (fs.existsSync(`${file}.gz`)) {
    fs.writeFileSync(`${file}.gz`, zlib.gzipSync(text, { level: 9 }));
    written.push(".gz");
  }
  if (fs.existsSync(`${file}.br`)) {
    fs.writeFileSync(
      `${file}.br`,
      zlib.brotliCompressSync(text, { params: { [zlib.constants.BROTLI_PARAM_QUALITY]: 11 } }),
    );
    written.push(".br");
  }
  return written;
}

function count(haystack, needle) {
  let n = 0;
  for (let i = haystack.indexOf(needle); i !== -1; i = haystack.indexOf(needle, i + needle.length)) n++;
  return n;
}

// Applies every patch under `root`; returns a list of lines for the build log.
// Throws on the first patch whose text is not found exactly once.
export function applyPatches(root, patches = PATCHES) {
  const log = [];
  for (const p of patches) {
    const dir = path.join(root, p.dir);
    const names = fs.readdirSync(dir).filter((n) => n.endsWith(p.ext) && fs.statSync(path.join(dir, n)).isFile());
    const hits = [];
    for (const name of names) {
      const file = path.join(dir, name);
      const text = fs.readFileSync(file, "utf8");
      const n = count(text, p.find);
      if (n) hits.push({ file, text, n });
    }
    if (hits.length !== 1 || hits[0].n !== 1) {
      const where = hits.map((h) => `${path.relative(root, h.file)} (${h.n}x)`).join(", ") || "no file";
      throw new Error(`patch ${p.id}: expected its text exactly once in ${p.dir}/*${p.ext}, found in ${where}`);
    }
    const { file, text } = hits[0];
    const out = text.replace(p.find, () => p.replace);
    fs.writeFileSync(file, out);
    const again = compressAgain(file, out);
    log.push(`${p.id}: ${path.relative(root, file)}${again.length ? ` (+${again.join(", ")})` : ""}`);
  }
  return log;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const root = process.argv[2];
  if (!root) {
    console.error("usage: node patch-openclaw.mjs <openclaw package root, e.g. /app>");
    process.exit(2);
  }
  try {
    for (const line of applyPatches(root)) console.log(`[patch-openclaw] ${line}`);
  } catch (e) {
    console.error(`[patch-openclaw] ${e.message}`);
    process.exit(1);
  }
}
