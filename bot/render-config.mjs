// Renders the managed part of OpenClaw's config from the environment at every
// start, merged into the existing ~/.openclaw/openclaw.json so settings the
// owner changes in the web UI survive, while the keys below are always forced
// back to our values (model endpoint, auth, marketplace/update/telemetry off).
//
// Secrets: this container does not have the model endpoint's API key at all.
// MODEL_BASE_URL is the model proxy's internal address and MODEL_API_KEY a
// placeholder; the modelproxy service adds the real key (../modelproxy/).
// The Telegram token IS in this container's environment; it is never written
// into the file (the config holds a SecretRef, {source:"env"}, that OpenClaw
// resolves from the process environment at runtime).
//
// Optional settings (all unset = OpenClaw's own defaults, as before):
//   BOT_DISPLAY_NAME, BOT_EMOJI   agents.entries.<agent>.identity.name / .emoji
//   UI_ACCENT_COLOR               ui.seamColor (the Control UI's accent colour)
//   MODEL_INPUT                   models.providers.bot.models[0].input (text | text,image)
//   MODEL_EXTRA_BODY              agents.defaults.models["bot/<model>"].params.extra_body
//   BACKGROUND_MODEL_USE=off      plugins.entries.memory-core.config.dreaming.enabled: false
//   TOOLS_DENY                    tools.deny (default: the tools that cannot work here)
import fs from "node:fs";
import path from "node:path";

const env = process.env;
const cfgPath = env.OPENCLAW_CONFIG_PATH || "/home/node/.openclaw/openclaw.json";

function need(name) {
  const v = (env[name] || "").trim();
  if (!v) {
    console.error(`[fugal-bot] ${name} is required (set it in .env)`);
    process.exit(2);
  }
  return v;
}
const envRef = (id) => ({ source: "env", provider: "default", id });
const intEnv = (name, dflt) => {
  const n = Number.parseInt(env[name] || "", 10);
  return Number.isFinite(n) && n > 0 ? n : dflt;
};
const list = (s) => (s || "").split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
// One line of plain text, at most `max` characters (whole characters: an emoji
// is never cut in half).
const oneLine = (s, max) =>
  Array.from((s || "").replace(/[\u0000-\u001f\u007f]+/g, " ").trim()).slice(0, max).join("").trim();
function fail(msg) {
  console.error(`[fugal-bot] ${msg}`);
  process.exit(2);
}

function isObj(x) {
  return x && typeof x === "object" && !Array.isArray(x);
}
// Deep merge: managed values win; arrays are replaced, not concatenated.
function merge(base, over) {
  const out = isObj(base) ? { ...base } : {};
  for (const [k, v] of Object.entries(over)) {
    if (v === undefined) continue;
    out[k] = isObj(v) && isObj(out[k]) ? merge(out[k], v) : v;
  }
  return out;
}

let current = {};
if (fs.existsSync(cfgPath)) {
  try {
    current = JSON.parse(fs.readFileSync(cfgPath, "utf8"));
  } catch {
    const bak = `${cfgPath}.unparseable-${Date.now()}`;
    fs.copyFileSync(cfgPath, bak);
    console.error(`[fugal-bot] existing config was not plain JSON; saved a copy to ${bak} and starting fresh`);
    current = {};
  }
}

const baseUrl = need("MODEL_BASE_URL").replace(/\/+$/, "");
const modelName = need("MODEL_NAME");
const providerId = "bot";
const port = intEnv("PROXY_PORT", 8080);
const hostname = (env.PUBLIC_HOSTNAME || "").trim();
// The proxy checks that a browser's Origin is the address it is talking to,
// then rewrites it to this one internal origin (any public name works: local,
// PUBLIC_HOSTNAME, a tunnel URL). It MUST be in allowedOrigins, or the Control
// UI's websocket is refused for every browser. Same value on both sides:
// CONTROL_UI_ORIGIN from compose.yaml, same default in proxy/entrypoint.sh.
const uiOrigin = (env.CONTROL_UI_ORIGIN || "https://bot.invalid").trim();
const origins = [uiOrigin, `http://127.0.0.1:${port}`, `http://localhost:${port}`];
if (hostname) origins.push(`https://${hostname}`, `https://${hostname}:${port}`);

const execMode = ["full", "ask", "auto"].includes(env.EXEC_MODE) ? env.EXEC_MODE : "full";

// What the model accepts. Text always; "image" only for a model that takes
// image parts (a text-only model answers HTTP 400 to a request that has one).
const modelInput = ["text"];
for (const kind of list(env.MODEL_INPUT).map((x) => x.toLowerCase())) {
  if (kind === "image") modelInput.push("image");
  else if (kind !== "text") fail(`MODEL_INPUT: unknown value "${kind}" (use text or text,image)`);
}
const modelInputs = [...new Set(modelInput)];

// Extra fields for every model request body, e.g. the switch that turns a
// model's "thinking" off: {"chat_template_kwargs":{"enable_thinking":false}}.
// OpenClaw merges params.extra_body into each openai-completions request last,
// so it wins over fields OpenClaw generates itself.
let extraBody;
if ((env.MODEL_EXTRA_BODY || "").trim()) {
  try {
    extraBody = JSON.parse(env.MODEL_EXTRA_BODY);
  } catch (e) {
    fail(`MODEL_EXTRA_BODY is not valid JSON (${e.message})`);
  }
  if (!isObj(extraBody)) fail("MODEL_EXTRA_BODY must be a JSON object, e.g. {\"chat_template_kwargs\":{\"enable_thinking\":false}}");
  for (const k of ["model", "messages", "tools", "stream"]) {
    if (k in extraBody) fail(`MODEL_EXTRA_BODY must not set "${k}"`);
  }
}
const modelRef = `${providerId}/${modelName}`;

// The owner's time zone (TZ, an IANA name the controller takes from their
// browser) for the agent's sense of local time and for schedules.
const tz = (env.TZ || "").trim();
let userTimezone = "";
if (tz) {
  try {
    userTimezone = new Intl.DateTimeFormat("en-US", { timeZone: tz }).resolvedOptions().timeZone;
  } catch {
    console.error(`[fugal-bot] TZ "${tz}" is not a known time zone; agents.defaults.userTimezone left as it is`);
  }
}

// What this script applied from the environment last time (no secrets). It
// lets the name and emoji be defaults rather than forced values: a new value
// in the environment is applied once, and a later change made in the web UI
// (Agents page) is kept. It also lets switches be undone when a variable is
// removed again.
const appliedPath = path.join(path.dirname(cfgPath), "fugal-bot-applied.json");
let applied = {};
try {
  const a = JSON.parse(fs.readFileSync(appliedPath, "utf8"));
  if (isObj(a)) applied = a;
} catch {}
const nextApplied = {};

const displayName = oneLine(env.BOT_DISPLAY_NAME, 64);
const emoji = oneLine(env.BOT_EMOJI, 16);
const accentRaw = (env.UI_ACCENT_COLOR || "").trim();
if (accentRaw && !/^#?[0-9a-fA-F]{6}$/.test(accentRaw)) fail("UI_ACCENT_COLOR must be a hex colour like #2f6fed");
const accent = accentRaw ? `#${accentRaw.replace(/^#/, "").toLowerCase()}` : "";
const backgroundOff = (env.BACKGROUND_MODEL_USE || "").trim().toLowerCase() === "off";

// Tools that cannot do anything in this package, taken out of the list the
// model is sent with every request (measured on v2026.11.6: about 8,200
// characters less per request, most of it the github_publish schema):
//   paired devices (none can be paired here), voice and meeting capture (no
//   provider), publishing to GitHub (no GitHub identity), plugin installs and
//   self-written skills (both blocked below), portals (an app preview needs a
//   private wildcard HTTPS ingress, gateway.portals.ingress, which a hosted bot
//   behind one tunnel hostname does not have: the Portals pane only says "This
//   portal needs a remote address"; AGENTS.md tells the agent what to do instead).
// Reading public GitHub (github_read, github_search) stays. TOOLS_DENY
// replaces the list; TOOLS_DENY=none denies nothing.
const DEFAULT_TOOLS_DENY = [
  "canvas", "nodes", "node_inference", "mobile_ui", "dir_fetch", "dir_list", "file_fetch", "file_write",
  "talk_voice", "tts", "transcripts",
  "github_publish", "github_identity_status",
  "plugins", "skill_workshop",
  "portal",
];
const toolsDenyRaw = (env.TOOLS_DENY || "").trim();
const toolsDeny = toolsDenyRaw.toLowerCase() === "none" ? [] : toolsDenyRaw ? list(toolsDenyRaw) : DEFAULT_TOOLS_DENY;
for (const t of toolsDeny) if (!/^[A-Za-z0-9_.:*-]{1,80}$/.test(t)) fail(`TOOLS_DENY: "${t}" is not a tool name`);

const managed = {
  gateway: {
    mode: "local",
    port: 18789,
    bind: env.GATEWAY_BIND === "loopback" ? "loopback" : "lan",
    // Only the password proxy (fixed address on the compose "front" network)
    // may assert identity. Not the subnet: Docker's gateway address on that
    // network is where published-port clients appear from.
    trustedProxies: [need("PROXY_IP")],
    auth: {
      mode: "trusted-proxy",
      trustedProxy: {
        userHeader: "x-forwarded-user",
        requiredHeaders: ["x-forwarded-proto", "x-forwarded-host"],
        allowUsers: ["owner"],
        // Only in the all-host-network layout (compose.cathedral.yaml), where the
        // proxy necessarily connects from 127.0.0.1. See SECURITY-NOTES.md.
        allowLoopback: env.PROXY_FROM_LOOPBACK === "true",
        deviceAutoApprove: { enabled: true },
      },
      identityScopes: { owner: ["operator.admin"] },
    },
    tailscale: { mode: "off" },
    // OpenAI-compatible /v1/chat/completions into the agent (owner-only: it
    // sits behind the password proxy). Used by scripts/smoke.sh.
    http: { endpoints: { chatCompletions: { enabled: env.ENABLE_OPENAI_API !== "false" } } },
    controlUi: {
      enabled: true,
      allowedOrigins: origins,
      communityInvite: false,
      automaticallyFetchFavicons: false,
      // Live session status digests: on by default, and richer ones call the
      // agent's utility model (the user's allowance). Off; the chat shows
      // tool activity anyway.
      sessionObserver: false,
    },
  },

  // --- model: any OpenAI-compatible endpoint, reached via the model proxy ----
  models: {
    mode: "replace",
    providers: {
      [providerId]: {
        baseUrl,
        apiKey: envRef("MODEL_API_KEY"),
        api: "openai-completions",
        models: [
          {
            id: modelName,
            name: modelName,
            reasoning: env.MODEL_REASONING === "true",
            input: modelInputs,
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
            contextWindow: intEnv("MODEL_CONTEXT_WINDOW", 32768),
            maxTokens: intEnv("MODEL_MAX_TOKENS", 4096),
            // Small local models (e.g. gemma3:4b on Ollama) reject any request
            // that carries tools; MODEL_SUPPORTS_TOOLS=false keeps plain chat working.
            compat: {
              supportsTools: env.MODEL_SUPPORTS_TOOLS !== "false",
              // OpenClaw then asks for token usage on streamed replies and so
              // knows how full the context is. The model proxy asks the
              // endpoint for the same thing (MODELPROXY_STREAM_USAGE, the one
              // switch for both), so this adds nothing new for the endpoint.
              supportsUsageInStreaming: env.MODEL_STREAM_USAGE !== "false",
            },
          },
        ],
      },
    },
  },
  agents: {
    defaults: {
      model: { primary: modelRef },
      workspace: env.OPENCLAW_WORKSPACE_DIR || "/home/node/workspace",
      ...(userTimezone ? { userTimezone } : {}),
    },
  },

  // --- tools: shell + files + browser on this container ---------------------
  tools: {
    exec: { mode: execMode },
    // One fetched page goes into every later request of the turn. OpenClaw's
    // defaults (20,000 characters, ~5,000 tokens) let a few pages fill a
    // small model's context and the user's allowance; 6,000 by default and at
    // most 12,000 when the agent asks for more (~1,500 / ~3,000 tokens).
    web: { fetch: { maxChars: 6000, maxCharsCap: 12000 } },
    // Ours plus whatever the owner denied in the web UI; names we put there
    // at an earlier start and no longer want are taken out again.
    deny: [
      ...new Set([
        ...(Array.isArray(current.tools?.deny) ? current.tools.deny : []).filter(
          (t) => !(Array.isArray(applied.toolsDeny) ? applied.toolsDeny : []).includes(t),
        ),
        ...toolsDeny,
      ]),
    ],
  },
  browser: {
    enabled: true,
    headless: false, // visible on the bot's desktop (DISPLAY=:1, watch via noVNC)
    noSandbox: true, // Chromium's own sandbox needs userns, which we do not grant
    executablePath: "/usr/bin/chromium",
    // Snapshots in the compact form unless the agent asks for another
    // (interactive elements only, depth 6, at most 8,000 characters; the full
    // form allows 40,000). It lists what can be clicked, NOT the page's text:
    // reading a page is the browser's `text` action (AGENTS.md says so, with
    // maxChars; OpenClaw has no setting for its default there).
    snapshotDefaults: { mode: "efficient" },
    // The agent shows small web apps it writes by serving them on this
    // container's loopback (AGENTS.md); portals need an ingress a hosted bot
    // does not have. Only localhost is added: the rest of the private network
    // stays blocked. (Without an explicit policy OpenClaw grants loopback only
    // in some cases; in a room the browser refused 127.0.0.1.) The agent can
    // reach these ports with its shell anyway, so this grants nothing new.
    ssrfPolicy: { allowedHostnames: ["localhost", "127.0.0.1"] },
  },

  // The Control UI's built-in Desktop panel shows this container's screen:
  // attach to our own loopback x11vnc (port 5900, VncAuth password file
  // written by entrypoint.sh). Same screen as /desktop/.
  desktop: {
    host: {
      enabled: true,
      port: 5900,
      passwordFile: env.VNC_PASSWORD_FILE || "/home/node/.vnc/password.txt",
    },
  },

  // --- removed / disabled: marketplace, installs, updates, telemetry --------
  skills: {
    allowBundled: [], // no bundled third-party-CLI skills; built-in tools stay
    // Our own reference skills, read-only in the image (they change with it).
    // Only each skill's name and one-line description go into every request;
    // the agent reads a file's section when a question needs it.
    load: { extraDirs: ["/opt/fugal-bot/skills"] },
    install: { allowUploadedArchives: false },
    workshop: { autonomous: { mode: "off" } },
  },
  security: {
    // Enabled with no policy command => every skill/plugin install is blocked
    // (ClawHub, uploads, git/local, dependency installers, plugin updates).
    installPolicy: { enabled: true, targets: ["skill", "plugin"] },
  },
  update: { checkOnStart: false, auto: { enabled: false } },
  telemetry: { enabled: false },
};

// --- model request extras ------------------------------------------------------
// Entries for this provider that an earlier start wrote for another model name
// would otherwise linger: take our field out of all of them first.
const modelEntries = current.agents?.defaults?.models;
if (isObj(modelEntries)) {
  for (const [ref, entry] of Object.entries(modelEntries)) {
    if (!ref.startsWith(`${providerId}/`) || !isObj(entry) || !isObj(entry.params)) continue;
    delete entry.params.extra_body;
    delete entry.params.extraBody;
    if (!Object.keys(entry.params).length) delete entry.params;
    if (!Object.keys(entry).length) delete modelEntries[ref];
  }
}
if (extraBody) managed.agents.defaults.models = { [modelRef]: { params: { extra_body: extraBody } } };

if (toolsDeny.length) nextApplied.toolsDeny = toolsDeny;

// --- identity and look (optional) -----------------------------------------------
// The agent the name belongs to: "main" (OpenClaw's implicit agent) unless the
// owner replaced it with exactly one agent of another id.
const agentIds = Object.keys(isObj(current.agents?.entries) ? current.agents.entries : {});
const agentId = !agentIds.length || agentIds.includes("main") ? "main" : agentIds.length === 1 ? agentIds[0] : "";
const identityNow = (agentId && current.agents?.entries?.[agentId]?.identity) || {};
const identity = {};
for (const [key, value] of [["name", displayName], ["emoji", emoji]]) {
  if (!value) continue;
  nextApplied[key] = value;
  // A default, not a forced value: write it when nothing is set yet or when
  // the environment value itself changed; otherwise keep what is in the file.
  if (!identityNow[key] || applied[key] !== value) identity[key] = value;
}
if (agentId && Object.keys(identity).length) {
  managed.agents.entries = { [agentId]: { identity } };
} else if (!agentId && (displayName || emoji)) {
  console.error("[fugal-bot] BOT_DISPLAY_NAME / BOT_EMOJI not applied: several agents are configured and none is \"main\"");
}
if (accent) {
  // The operator's accent colour. A colour the user picks under Settings >
  // Appearance is stored separately (ui.prefs.accent) and wins over this one.
  managed.ui = { seamColor: accent };
  nextApplied.accent = accent;
} else if (applied.accent && current.ui?.seamColor === applied.accent) {
  delete current.ui.seamColor;
}

// --- background model use --------------------------------------------------------
// OpenClaw's memory plugin schedules a nightly "dreaming" job (memory
// consolidation, cron 0 3 * * *) that calls the model with no user message.
// BACKGROUND_MODEL_USE=off disables it; OpenClaw then removes the job.
if (backgroundOff) {
  managed.plugins = { entries: { "memory-core": { config: { dreaming: { enabled: false } } } } };
  nextApplied.backgroundOff = true;
} else if (applied.backgroundOff) {
  const dreaming = current.plugins?.entries?.["memory-core"]?.config?.dreaming;
  if (isObj(dreaming) && dreaming.enabled === false) delete dreaming.enabled;
}

// Earlier versions added a second, test-only provider that the bot reached
// directly. Every model call now goes through the model proxy; drop it from
// config files written back then.
if (current.models?.providers?.scripted) delete current.models.providers.scripted;

// --- Telegram: on when a token is present -----------------------------------
const tgToken = (env.TELEGRAM_BOT_TOKEN || "").trim();
if (tgToken) {
  const allow = list(env.TELEGRAM_ALLOWED_USER_IDS);
  managed.channels = {
    telegram: {
      enabled: true,
      botToken: envRef("TELEGRAM_BOT_TOKEN"),
      // Allowlisted user ids if given; otherwise each new DM gets a pairing
      // code that the owner approves in the web UI (Settings > Channels).
      dmPolicy: allow.length ? "allowlist" : "pairing",
      ...(allow.length ? { allowFrom: allow } : {}),
      groupPolicy: "disabled",
    },
  };
} else if (current.channels?.telegram) {
  managed.channels = { telegram: { enabled: false } };
}

let next = merge(current, managed);
// Keys we must not inherit from an older file.
if (next.security?.installPolicy) delete next.security.installPolicy.exec;
if (next.gateway?.auth) {
  delete next.gateway.auth.token;
  delete next.gateway.auth.password;
}
if (next.channels?.telegram && !tgToken) delete next.channels.telegram.botToken;

fs.mkdirSync(path.dirname(cfgPath), { recursive: true, mode: 0o700 });
const tmp = `${cfgPath}.tmp-${process.pid}`;
fs.writeFileSync(tmp, `${JSON.stringify(next, null, 2)}\n`, { mode: 0o600 });
fs.renameSync(tmp, cfgPath);
try {
  if (Object.keys(nextApplied).length) {
    fs.writeFileSync(`${appliedPath}.tmp-${process.pid}`, `${JSON.stringify(nextApplied)}\n`, { mode: 0o600 });
    fs.renameSync(`${appliedPath}.tmp-${process.pid}`, appliedPath);
  } else {
    fs.rmSync(appliedPath, { force: true });
  }
} catch (e) {
  console.error(`[fugal-bot] could not record applied settings (${e.code || e.message}); continuing`);
}
console.log(
  `[fugal-bot] config rendered: model=${modelRef} input=${modelInputs.join("+")} endpoint=${new URL(baseUrl).host} ` +
    `telegram=${tgToken ? "on" : "off"} exec=${execMode}` +
    `${displayName ? ` name="${displayName}"` : ""}${accent ? ` accent=${accent}` : ""}` +
    `${extraBody ? ` extra-body-fields=${Object.keys(extraBody).join(",")}` : ""}` +
    `${backgroundOff ? " background-model-use=off" : ""} tools-denied=${next.tools.deny.length}`,
);
