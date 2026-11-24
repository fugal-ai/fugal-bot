// The owner's name in the Control UI (avatar menu, People), set at gateway
// start while the gateway is stopped (start-gateway.sh):
//   node owner-name.mjs [path to the state database]
//
// The password proxy signs the owner in as the trusted-proxy user "owner"
// (gateway.auth.trustedProxy.allowUsers and identityScopes match that id).
// OpenClaw keeps that id as an email alias of a user profile it creates at the
// first sign-in, named after the alias: "owner". The id stays; only the
// profile's display name changes, to the owner's short wallet address
// (OWNER_WALLET_SS58, e.g. "5Gbydr…wB7m") or "You" without one.
//
// Only a name that is still OpenClaw's default ("owner") is replaced: a name
// the owner picked under Settings > Profile is kept. Nothing happens before the
// first sign-in (no profile yet): the name changes at the next start (bots are
// restarted daily). Any failure is logged and the gateway starts anyway.
import fs from "node:fs";

const SS58 = /^[1-9A-HJ-NP-Za-km-z]{46,48}$/;

export function ownerDisplayName(wallet) {
  const w = (wallet || "").trim();
  return SS58.test(w) ? `${w.slice(0, 6)}…${w.slice(-4)}` : "You";
}

// Returns what was done, for the log: "renamed", "kept", "no-profile", "no-database".
export async function setOwnerName(dbPath, name, now = Date.now()) {
  if (!fs.existsSync(dbPath)) return "no-database";
  const { DatabaseSync } = await import("node:sqlite");
  const db = new DatabaseSync(dbPath);
  try {
    db.exec("PRAGMA busy_timeout = 5000");
    const tables = db
      .prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('user_profiles', 'user_profile_emails')")
      .all();
    if (tables.length !== 2) return "no-profile";
    const row = db
      .prepare(
        "SELECT p.id AS id, p.display_name AS name FROM user_profile_emails e JOIN user_profiles p ON p.id = e.profile_id WHERE e.email = 'owner'",
      )
      .get();
    if (!row) return "no-profile";
    if (row.name !== "owner") return "kept";
    db.prepare("UPDATE user_profiles SET display_name = ?, updated_at = ? WHERE id = ? AND display_name = 'owner'").run(
      name,
      now,
      row.id,
    );
    return "renamed";
  } finally {
    db.close();
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const stateDir = process.env.OPENCLAW_STATE_DIR || "/home/node/.openclaw";
  const dbPath = process.argv[2] || `${stateDir}/state/openclaw.sqlite`;
  const name = ownerDisplayName(process.env.OWNER_WALLET_SS58);
  try {
    const done = await setOwnerName(dbPath, name);
    if (done === "renamed") console.log(`[fugal-bot] owner profile name set to "${name}"`);
  } catch (e) {
    console.error(`[fugal-bot] owner profile name not set (${e.code || e.message}); continuing`);
  }
}
