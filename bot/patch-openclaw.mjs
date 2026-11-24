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
// something that cannot work in a hosted bot, or puts our product name and
// starter prompts where upstream shows its own; none widens what the bot can do.
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

// The product name the hosted Control UI shows where upstream shows its own
// (OpenClaw has no config setting for it: the name comes from the theme, and
// only a plugin theme can set one). The bot's own name (BOT_DISPLAY_NAME, the
// agent identity) is shown by OpenClaw itself wherever it names the agent.
// The About page keeps upstream's name and licence credit.
const BRAND = "Fugal Bot";
// A patch that only rewords display text: the same span with our name for
// upstream's. Each `find` is chosen to hold user-visible strings only.
const rebrand = (id, dir, ext, find) => ({ id, dir, ext, find, replace: find.replaceAll("OpenClaw", BRAND) });

// Starter prompts on an empty chat: label and prompt are the same text (a
// chip puts it in the composer and sends it). Plain text without "." or "`":
// the UI looks each one up as a translation key and, finding none, shows it
// as it is.
export const STARTERS = [
  "Send me a daily briefing",
  "Watch a subnet for me",
  "Watch a miner or validator hotkey",
  "What changed on Bittensor this week?",
];
const starterList = `[${STARTERS.map((s) => `\`${s}\``).join(",")}]`;

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

  // --- Empty chat: starter prompts -----------------------------------------------------
  // The empty chat showed starter chips only while the owner had no other
  // chat; from the second chat on it showed "Recent chats" instead (the same
  // chats as the sidebar). Always show the starters.
  {
    id: "chat-welcome-always-starters",
    dir: "dist/control-ui/assets",
    ext: ".js",
    find: "get when(){return n().length>0},get fallback(){var t=rPe();",
    replace: "get when(){return!1},get fallback(){var t=rPe();",
  },
  // Upstream's chips are about OpenClaw itself (channels, its code, pull
  // requests); ours are what this bot is for. Both upstream sets (chosen by
  // the kind of operator sign-in) get the same four.
  {
    id: "chat-welcome-starter-texts",
    dir: "dist/control-ui/assets",
    ext: ".js",
    find:
      "ZAe=[`chat.welcome.suggestions.whatCanYouDo`,`chat.welcome.suggestions.summarizeRecentSessions`," +
      "`chat.welcome.suggestions.configureChannel`,`chat.welcome.suggestions.checkSystemHealth`]," +
      "QAe=[`chat.welcome.suggestions.whatCanYouDo`,`chat.welcome.suggestions.exploreOpenClaw`," +
      "`chat.welcome.suggestions.investigateInSandbox`,`chat.welcome.suggestions.openDraftPullRequest`]",
    replace: `ZAe=${starterList},QAe=${starterList}`,
  },

  // --- Product name ----------------------------------------------------------------------
  // The default theme's branding: the tab title ("Chat — <name>"), the "Ask
  // <name>" entry, the sidebar name when no agent name is set, the sign-in
  // and profile pages.
  {
    id: "brand-name-default",
    dir: "dist/control-ui/assets",
    ext: ".js",
    find: "brandName:e?.brandName??`OpenClaw`,",
    replace: `brandName:e?.brandName??\`${BRAND}\`,`,
  },
  // ...except the About page, which describes the software: it keeps
  // upstream's product name above upstream's links and licence line.
  {
    id: "about-page-keeps-upstream-name",
    dir: "dist/control-ui/assets",
    ext: ".js",
    find: "w(r,()=>n.read().brandName)",
    replace: "w(r,()=>_(`aboutPage.productName`))",
  },
  // The title before the app sets its own (first paint, a page that fails to load).
  {
    id: "page-title",
    dir: "dist/control-ui",
    ext: ".html",
    find: "<title>OpenClaw Control</title>",
    replace: `<title>${BRAND}</title>`,
  },
  // "Ask OpenClaw" (the setup helper panel): its name, composer placeholder,
  // progress and error lines, and the panel controls.
  rebrand(
    "ask-panel-texts",
    "dist/control-ui/assets",
    ".js",
    "custodian:{pluginPlaceholder:`Ask OpenClaw about {plugin}`,pluginIntroTitle:`Ask about {plugin}`," +
      "pluginStarterPurpose:`What does it do?`,pluginStarterTools:`What tools does it have?`," +
      "pluginStarterSetup:`How do I set it up?`,pluginPromptPurpose:`What does {plugin} do?`," +
      "pluginPromptTools:`What tools does {plugin} provide?`,pluginPromptSetup:`How do I set up {plugin}?`," +
      "pluginHelpQuestion:`Explain {setting}`,pluginHelpFailed:`Could not prepare the setting question. Try Ask OpenClaw again.`," +
      "pluginHelpUnset:`Not set`,pluginHelpValue:`Current value: {value}`,title:`OpenClaw`," +
      "subtitleCaretaker:`System setup and care.`,exitSetup:`Exit setup`,hatchDraft:`Wake up, my friend!`," +
      "placeholder:`Message OpenClaw…`,sensitivePlaceholder:`Enter sensitive value…`," +
      "sensitiveReply:`Sensitive reply sent`,cancel:`Cancel`,send:`Send`,thinking:`OpenClaw is thinking`," +
      "earlier:`Earlier`,requestFailed:`OpenClaw could not reply. Try again.`",
  ),
  rebrand(
    "ask-panel-controls",
    "dist/control-ui/assets",
    ".js",
    "panel:{title:`OpenClaw`,toggle:`Toggle Ask OpenClaw`,close:`Close Ask OpenClaw`," +
      "resize:`Resize Ask OpenClaw`,dockBottom:`Dock Ask OpenClaw at bottom`,dockRight:`Dock Ask OpenClaw at right`}",
  ),
  // Automations page: the prompt box and the two hints about it.
  rebrand("automations-subtitle", "dist/control-ui/assets", ".js",
    "newSubtitle:`Describe what OpenClaw should do, then pick when it runs.`"),
  rebrand("automations-prompt-placeholder", "dist/control-ui/assets", ".js",
    "promptPlaceholder:`Describe what OpenClaw should do...`"),
  rebrand("automations-empty-hint", "dist/control-ui/assets", ".js",
    "emptyHint:`Describe what OpenClaw should do and when — it runs on schedule.`"),
  // Default voice wake words, shown under Settings > Talk (only paired devices
  // listen for them, and none can be paired here). A list the owner saved is kept.
  {
    id: "voice-wake-default-words",
    dir: "dist",
    ext: ".mjs",
    find: 'const DEFAULT_TRIGGERS = [\n\t"openclaw",',
    replace: 'const DEFAULT_TRIGGERS = [\n\t"fugal bot",',
  },

  // --- Apps page ------------------------------------------------------------------------------
  // Download links for OpenClaw's phone, watch, desktop and browser companions
  // and for ClawHub; none can connect to a hosted bot (no device pairing, no
  // marketplace). Left out of the sidebar the way upstream leaves out
  // Templates when they are off (the command palette still lists it).
  {
    id: "sidebar-hide-apps",
    dir: "dist/control-ui/assets",
    ext: ".js",
    find: "enabledRouteIds:Wy(t.runtimeConfig.state)===!0?wt:wt.filter(e=>e!==`templates`),",
    replace: "enabledRouteIds:(Wy(t.runtimeConfig.state)===!0?wt:wt.filter(e=>e!==`templates`)).filter(e=>e!==`apps`),",
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
