// Renders the managed part of OpenClaw's config from the environment at every
// start, merged into the existing ~/.openclaw/openclaw.json so settings the
// owner changes in the web UI survive, while the keys below are always forced
// back to our values (model endpoint, auth, marketplace/update/telemetry off).
//
// Secrets: the model key and the Telegram token are NEVER written into the
// file. The config holds SecretRefs ({source:"env"}) that OpenClaw resolves
// from the process environment at runtime.
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
    },
  },

  // --- model: any OpenAI-compatible endpoint --------------------------------
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
            input: ["text"],
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
            contextWindow: intEnv("MODEL_CONTEXT_WINDOW", 32768),
            maxTokens: intEnv("MODEL_MAX_TOKENS", 4096),
            // Small local models (e.g. gemma3:4b on Ollama) reject any request
            // that carries tools; MODEL_SUPPORTS_TOOLS=false keeps plain chat working.
            compat: { supportsTools: env.MODEL_SUPPORTS_TOOLS !== "false" },
          },
        ],
      },
    },
  },
  agents: {
    defaults: {
      model: { primary: `${providerId}/${modelName}` },
      workspace: env.OPENCLAW_WORKSPACE_DIR || "/home/node/workspace",
    },
  },

  // --- tools: shell + files + browser on this container ---------------------
  tools: {
    exec: { mode: execMode },
  },
  browser: {
    enabled: true,
    headless: false, // visible on the bot's desktop (DISPLAY=:1, watch via noVNC)
    noSandbox: true, // Chromium's own sandbox needs userns, which we do not grant
    executablePath: "/usr/bin/chromium",
  },

  // --- removed / disabled: marketplace, installs, updates, telemetry --------
  skills: {
    allowBundled: [], // no bundled third-party-CLI skills; built-in tools stay
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

// --- test only: a scripted, deterministic "brain" that emits tool calls -----
// (scripts/smoke.sh). Lets the smoke test drive real tools without a paid model.
const scriptedUrl = (env.SCRIPTED_BRAIN_URL || "").trim();
if (scriptedUrl) {
  managed.models.providers.scripted = {
    baseUrl: scriptedUrl,
    apiKey: "scripted",
    api: "openai-completions",
    models: [{ id: "tools", name: "scripted tool driver (test)", reasoning: false, input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 131072, maxTokens: 4096 }],
  };
} else if (current.models?.providers?.scripted) {
  delete current.models.providers.scripted;
}

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
console.log(
  `[fugal-bot] config rendered: model=${providerId}/${modelName} endpoint=${new URL(baseUrl).host} ` +
    `telegram=${tgToken ? "on" : "off"} exec=${execMode}`,
);
