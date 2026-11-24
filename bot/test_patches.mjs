// Unit tests for patch-openclaw.mjs and owner-name.mjs (node, no dependencies,
// no network):
//   node bot/test_patches.mjs
// With OPENCLAW_PACKAGE=<an unpacked openclaw npm package or /app of the image>
// the real patch list is also applied to a copy of that package's dist/.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import zlib from "node:zlib";
import { PATCHES, STARTERS, applyPatches } from "./patch-openclaw.mjs";
import { ownerDisplayName, setOwnerName } from "./owner-name.mjs";

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), "fugal-patch-"));
const write = (root, rel, text) => {
  fs.mkdirSync(path.dirname(path.join(root, rel)), { recursive: true });
  fs.writeFileSync(path.join(root, rel), text);
};
const read = (root, rel) => fs.readFileSync(path.join(root, rel), "utf8");
const P = [{ id: "t", dir: "dist/a", ext: ".js", find: "sessionTarget:`isolated`", replace: "sessionTarget:`main`" }];
let n = 0;
const test = async (name, fn) => {
  await fn();
  n++;
  console.log(`ok - ${name}`);
};

await test("patch applied once, compressed copy written again", () => {
  const r = tmp();
  write(r, "dist/a/x-1.js", "a,sessionTarget:`isolated`,b");
  write(r, "dist/a/x-1.js.gz", zlib.gzipSync("a,sessionTarget:`isolated`,b"));
  write(r, "dist/a/y-2.js", "nothing here");
  const log = applyPatches(r, P);
  assert.equal(read(r, "dist/a/x-1.js"), "a,sessionTarget:`main`,b");
  assert.equal(zlib.gunzipSync(fs.readFileSync(path.join(r, "dist/a/x-1.js.gz"))).toString(), "a,sessionTarget:`main`,b");
  assert.equal(read(r, "dist/a/y-2.js"), "nothing here");
  assert.match(log[0], /x-1\.js \(\+\.gz\)/);
});

await test("missing text stops the build", () => {
  const r = tmp();
  write(r, "dist/a/x.js", "sessionTarget:`main`");
  assert.throws(() => applyPatches(r, P), /patch t: expected its text exactly once .*found in no file/);
});

await test("text twice in one file stops the build", () => {
  const r = tmp();
  write(r, "dist/a/x.js", "sessionTarget:`isolated`;sessionTarget:`isolated`");
  assert.throws(() => applyPatches(r, P), /x\.js \(2x\)/);
  assert.equal(read(r, "dist/a/x.js"), "sessionTarget:`isolated`;sessionTarget:`isolated`");
});

await test("text in two files stops the build", () => {
  const r = tmp();
  write(r, "dist/a/x.js", "sessionTarget:`isolated`");
  write(r, "dist/a/y.js", "sessionTarget:`isolated`");
  assert.throws(() => applyPatches(r, P), /x\.js \(1x\), dist\/a\/y\.js \(1x\)|y\.js \(1x\), dist\/a\/x\.js \(1x\)/);
});

await test("other extensions and $-patterns in the replacement are left alone", () => {
  const r = tmp();
  write(r, "dist/a/x.js", "k=`${a}` find");
  write(r, "dist/a/x.css", "find");
  applyPatches(r, [{ id: "d", dir: "dist/a", ext: ".js", find: "find", replace: "$& $1 ${b}" }]);
  assert.equal(read(r, "dist/a/x.js"), "k=`${a}` $& $1 ${b}");
  assert.equal(read(r, "dist/a/x.css"), "find");
});

await test("the patch list is well formed", () => {
  const ids = new Set();
  for (const p of PATCHES) {
    assert.ok(p.id && !ids.has(p.id), `unique id ${p.id}`);
    ids.add(p.id);
    assert.ok(p.find.length >= 8 && p.replace !== p.find, p.id);
    assert.ok(p.dir.startsWith("dist"), p.id);
    // Nothing that loads more code or reaches the network is added.
    assert.doesNotMatch(p.replace, /<script|https?:|import\(|fetch\(/, p.id);
  }
  // Starter texts go into a template literal and are looked up as translation
  // keys (a "." would split them into a key path).
  assert.equal(STARTERS.length, 4);
  for (const s of STARTERS) assert.match(s, /^[^.`$\\]+$/, s);
});

await test("the About page keeps upstream's name and licence credit", () => {
  for (const p of PATCHES) {
    assert.doesNotMatch(p.find, /OpenClaw Foundation|MIT License/, p.id);
  }
});

const pkg = (process.env.OPENCLAW_PACKAGE || "").trim();
if (pkg) {
  await test(`real patch list applies to ${pkg}`, () => {
    const r = tmp();
    fs.cpSync(path.join(pkg, "dist"), path.join(r, "dist"), { recursive: true });
    const log = applyPatches(r);
    assert.equal(log.length, PATCHES.length);
    for (const line of log) console.log(`    ${line}`);
    const html = read(r, "dist/control-ui/index.html");
    assert.match(html, /<title>Fugal Bot<\/title>/);
    assert.doesNotMatch(html, /<title>[^<]*OpenClaw/);
    const assets = path.join(r, "dist/control-ui/assets");
    const ui = fs.readdirSync(assets).filter((n) => n.endsWith(".js")).map((n) => fs.readFileSync(path.join(assets, n), "utf8")).join("\n");
    for (const s of STARTERS) assert.ok(ui.includes(`\`${s}\``), s);
    assert.ok(ui.includes("© 2026 OpenClaw Foundation — MIT License."), "About page credit kept");
    fs.rmSync(r, { recursive: true, force: true });
  });
}

await test("owner display name: short wallet or You", () => {
  assert.equal(ownerDisplayName("5GbydRVj98hQZvR2cf4t8WtcoUVjAuJykxxuqauiJpJqwB7m"), "5GbydR…wB7m");
  assert.equal(ownerDisplayName(""), "You");
  assert.equal(ownerDisplayName("not a wallet <b>"), "You");
});

let sqlite = null;
try {
  sqlite = await import("node:sqlite");
} catch {}
if (!sqlite) {
  console.log("skip - owner profile rename (this Node has no node:sqlite; the image's Node 24 has)");
} else {
  const schema = `
    CREATE TABLE user_profiles (id TEXT NOT NULL PRIMARY KEY, display_name TEXT, primary_github_account_id INTEGER,
      avatar BLOB, avatar_mime TEXT, avatar_sha256 TEXT, merged_into TEXT, role TEXT,
      created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL) STRICT;
    CREATE TABLE user_profile_emails (email TEXT NOT NULL PRIMARY KEY, profile_id TEXT NOT NULL, binding_id TEXT,
      created_at INTEGER NOT NULL) STRICT;`;
  const mk = (name) => {
    const f = path.join(tmp(), "openclaw.sqlite");
    const db = new sqlite.DatabaseSync(f);
    db.exec(schema);
    if (name !== null) {
      db.prepare("INSERT INTO user_profiles (id, display_name, created_at, updated_at) VALUES ('p1', ?, 1, 1)").run(name);
      db.exec("INSERT INTO user_profile_emails (email, profile_id, created_at) VALUES ('owner', 'p1', 1)");
      db.exec("INSERT INTO user_profiles (id, display_name, created_at, updated_at) VALUES ('p2', 'owner', 1, 1)");
    }
    db.close();
    return f;
  };
  const nameOf = (f, id = "p1") => {
    const db = new sqlite.DatabaseSync(f);
    const r = db.prepare("SELECT display_name AS n, updated_at AS u FROM user_profiles WHERE id = ?").get(id);
    db.close();
    return r;
  };
  await test("owner profile: default name replaced, nothing else touched", async () => {
    const f = mk("owner");
    assert.equal(await setOwnerName(f, "5GbydR…wB7m", 99), "renamed");
    assert.deepEqual({ ...nameOf(f) }, { n: "5GbydR…wB7m", u: 99 });
    assert.equal(nameOf(f, "p2").n, "owner");
    assert.equal(await setOwnerName(f, "You", 100), "kept");
  });
  await test("owner profile: a name the owner chose is kept", async () => {
    const f = mk("Ada");
    assert.equal(await setOwnerName(f, "You"), "kept");
    assert.equal(nameOf(f).n, "Ada");
  });
  await test("owner profile: before the first sign-in nothing happens", async () => {
    assert.equal(await setOwnerName(mk(null), "You"), "no-profile");
    assert.equal(await setOwnerName(path.join(tmp(), "none.sqlite"), "You"), "no-database");
    const empty = path.join(tmp(), "empty.sqlite");
    new sqlite.DatabaseSync(empty).close();
    assert.equal(await setOwnerName(empty, "You"), "no-profile");
  });
}

console.log(`${n} passed`);
