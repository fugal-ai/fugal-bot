#!/opt/fugal-bot/tools/venv/bin/python3 -I
"""taoq: read-only Bittensor (finney) chain reports for the agent.

No wallet, no keys, no transactions: it opens a websocket to a public
Subtensor endpoint, reads public state at the latest FINALIZED block and
prints one line of JSON (or, with --text, a short report for a person).

    taoq stake <coldkey>            stake positions, values, change since the last run, flags
    taoq subnet <netuid>...         price, pool, emission, neurons, registration cost, owner, links
    taoq hotkey <hotkey> [--netuid N]   a miner's / validator's standing on each subnet, deregistration risk
    taoq hotkey|subnet ... --watch  the same, for a scheduled job: only what is new, worse or resolved
    taoq week                       price movers, new subnets, emission changes over about 7 days
    taoq subnet-brief <netuid>      the on-chain facts for a deep dive on one subnet

Units (every output):
    TAO and alpha amounts are decimal numbers of whole TAO / whole alpha, exact
    to the rao (1 TAO = 1 alpha unit = 1,000,000,000 rao; 9 decimals at most).
    Prices are TAO per 1 alpha. Netuid 0 (root) is staked in TAO at price 1.
    Shares (incentive, dividends, trust, consensus) are fractions 0..1 of the
    subnet total (the chain stores them as u16 / 65535). Percentages are
    numbers like 12.5 meaning 12.5 %, rounded to 2 decimals.
    Block times are about 12 s; a day is about 7,200 blocks.
A value that could not be read is null and named in "unread"; nothing is
estimated in its place.

Snapshots (for "since last time"): under $TAOQ_DATA_DIR, default
$OPENCLAW_WORKSPACE_DIR/taoq (the agent's workspace). --no-save skips writing.

Environment:
    TAOQ_ENDPOINTS   comma-separated wss:// endpoints, tried in order
                     (default: the two public finney endpoints below)
    TAOQ_ARCHIVE_ENDPOINTS   for `week` when no week-old snapshot exists yet
    TAOQ_TIMEOUT     seconds per attempt (default 40); TAOQ_ROUNDS (default 2)
    TAOQ_FIXTURE     replay a recorded read instead of the network (tests, CI)
    TAOQ_RECORD      record every read of this run to that file

Exit status: 0 ok, 1 the chain could not be read, 2 bad arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import os
import re
import sys
import time
from decimal import ROUND_HALF_UP, Decimal

VERSION = "1"
RAO = 10**9
D9 = Decimal("0.000000001")
DEFAULT_ENDPOINTS = "wss://entrypoint-finney.opentensor.ai:443,wss://lite.chain.opentensor.ai:443"
DEFAULT_ARCHIVE = "wss://archive.chain.opentensor.ai:443"
BLOCKS_PER_DAY = 7200
U16 = 65535
HISTORY_MAX = 200           # stake snapshots kept per coldkey
SUBNET_HISTORY_DAYS = 45    # subnet price history kept
SUBNET_HISTORY_GAP = 4 * 3600   # at most one subnet snapshot per 4 hours


class Unreadable(Exception):
    """The chain could not be read (all endpoints, all retries)."""


class Usage(Exception):
    """Bad arguments."""


# ---- SS58 ------------------------------------------------------------------------
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def check_ss58(addr: str, what: str) -> str:
    """A Bittensor (prefix 42) SS58 address with a correct checksum, or Usage."""
    addr = (addr or "").strip()
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{46,49}", addr):
        raise Usage(f"{what} is not an SS58 address: {addr[:60]!r}")
    n = 0
    for ch in addr:
        n = n * 58 + _B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    raw = b"\0" * (len(addr) - len(addr.lstrip("1"))) + raw
    if len(raw) != 35 or raw[0] != 42:
        raise Usage(f"{what} is not a Bittensor (SS58 prefix 42) public address: {addr}")
    want = hashlib.blake2b(b"SS58PRE" + raw[:33], digest_size=64).digest()[:2]
    if raw[33:] != want:
        raise Usage(f"{what} has a wrong checksum (a typo?): {addr}")
    return addr


# ---- numbers -----------------------------------------------------------------------
def tao(rao) -> Decimal | None:
    """rao (int) -> whole TAO / alpha, exact."""
    if rao is None:
        return None
    return Decimal(int(rao)) / RAO


def q9(d: Decimal | None) -> Decimal | None:
    return None if d is None else d.quantize(D9, rounding=ROUND_HALF_UP)


def pct(new, old) -> Decimal | None:
    if new is None or old is None or old == 0:
        return None
    return ((Decimal(new) - Decimal(old)) * 100 / Decimal(old)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def frac(u16v) -> Decimal | None:
    if u16v is None:
        return None
    return (Decimal(int(u16v)) / U16).quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP)


def fixed_point(v, frac_bits=32) -> Decimal | None:
    """I96F32 / U64F64 style {"bits": n} -> Decimal."""
    if isinstance(v, dict) and "bits" in v:
        v = v["bits"]
    if not isinstance(v, int):
        return None
    return Decimal(v) / (Decimal(2) ** frac_bits)


def text_of(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        try:
            return bytes(v).decode("utf-8", "replace").strip() or None
        except (TypeError, ValueError):
            return None
    return str(v).strip() or None


def _num(d: Decimal) -> str:
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


_MARK = "\x00N"


def _prep(o):
    if isinstance(o, Decimal):
        return _MARK + _num(o)
    if isinstance(o, dict):
        return {k: _prep(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_prep(v) for v in o]
    return o


def dumps(o) -> str:
    """Compact JSON with Decimals written as exact JSON numbers."""
    s = json.dumps(_prep(o), separators=(",", ":"), ensure_ascii=False)
    return re.sub(r'"\\u0000N(-?[0-9.]+)"', r"\1", s)


def short(addr: str | None) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if addr else "?"


def fmt(d, places=4) -> str:
    """For --text only: a readable number."""
    if d is None:
        return "n/a"
    d = Decimal(d)
    if 0 < abs(d) < Decimal("0.000001"):
        return _num(d)
    if abs(d) >= 1000:
        return f"{d:,.2f}"
    if abs(d) >= 1:
        return f"{d:,.{places}f}".rstrip("0").rstrip(".")
    return f"{d:.{max(places, 6)}f}".rstrip("0").rstrip(".") or "0"


def signed(d, places=4) -> str:
    if d is None:
        return "n/a"
    return ("+" if d >= 0 else "") + fmt(d, places)


# ---- reading the chain -------------------------------------------------------------
def _plain(v):
    """Decoder objects -> JSON-able Python values (what fixtures store)."""
    v = getattr(v, "value", v)
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, (bytes, bytearray)):
        return list(v)
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return str(v)


def _key(kind, a, b, params) -> str:
    return json.dumps([kind, a, b, params], separators=(",", ":"))


class Reader:
    """All reads of one run, at one finalized block. Live, recording or replaying."""

    def __init__(self, substrate=None, block_hash=None, block=None, fixture=None):
        self.s = substrate
        self.block_hash = block_hash
        self.block = block
        self.fixture = fixture
        self.recorded = {}

    async def _do(self, key, coro_fn):
        if self.fixture is not None:
            if key not in self.fixture["reads"]:
                raise Unreadable(f"the fixture has no recorded answer for {key}")
            return self.fixture["reads"][key]
        v = _plain(await coro_fn())
        self.recorded[key] = v
        return v

    async def call(self, api, method, params):
        return await self._do(_key("call", api, method, params),
                              lambda: self.s.runtime_call(api, method, params, block_hash=self.block_hash))

    async def query(self, module, storage, params):
        return await self._do(_key("query", module, storage, params),
                              lambda: self.s.query(module, storage, params, block_hash=self.block_hash))

    async def query_many(self, module, storage, param_list):
        """One round trip for many keys of one storage map."""
        async def go():
            keys = [await self.s.create_storage_key(module, storage, p, block_hash=self.block_hash) for p in param_list]
            res = await self.s.query_multi(keys, block_hash=self.block_hash)
            by = {json.dumps(k.params, default=str): _plain(v) for k, v in res}
            return [by.get(json.dumps(p, default=str)) for p in param_list]
        return await self._do(_key("multi", module, storage, param_list), go)


def endpoints(var="TAOQ_ENDPOINTS", default=DEFAULT_ENDPOINTS) -> list[str]:
    eps = [e.strip() for e in (os.environ.get(var) or default).split(",") if e.strip()]
    for e in eps:
        if not e.startswith(("wss://", "ws://")):
            raise Usage(f"{var}: {e!r} is not a ws:// or wss:// address")
    return eps


def run_read(job, eps=None, at_block=None):
    """Run `job(reader)` against the chain: fixture, or each endpoint in turn,
    TAOQ_ROUNDS rounds, TAOQ_TIMEOUT seconds per attempt."""
    fixture_path = os.environ.get("TAOQ_FIXTURE")
    if fixture_path:
        opener = gzip.open if fixture_path.endswith(".gz") else open
        with opener(fixture_path, "rt", encoding="utf-8") as f:
            fx = json.load(f)
        r = Reader(block=fx["block"], block_hash=fx.get("block_hash"), fixture=fx)
        if at_block is not None:
            past = fx.get("past", {}).get(str(at_block))
            if past is None:
                raise Unreadable(f"the fixture has no recorded state for block {at_block}")
            r = Reader(block=past["block"], block_hash=past.get("block_hash"), fixture=past)
        return asyncio.run(job(r)), r

    timeout = float(os.environ.get("TAOQ_TIMEOUT") or 40)
    rounds = max(1, int(os.environ.get("TAOQ_ROUNDS") or 2))
    eps = eps or endpoints()
    errors = []

    async def attempt(url):
        from async_substrate_interface import AsyncSubstrateInterface  # noqa: PLC0415 (slow import, only when needed)

        async with AsyncSubstrateInterface(url=url, ss58_format=42, max_retries=2, retry_timeout=timeout) as s:
            if at_block is None:
                bh = await s.get_chain_finalised_head()
                block = int(await s.get_block_number(bh))
            else:
                bh = await s.get_block_hash(at_block)
                block = at_block
            r = Reader(s, bh, block)
            return await job(r), r

    for rnd in range(rounds):
        for url in eps:
            try:
                out, r = asyncio.run(asyncio.wait_for(attempt(url), timeout=timeout))
                rec = os.environ.get("TAOQ_RECORD")
                if rec:
                    _record(rec, r, at_block)
                return out, r
            except Usage:
                raise
            except BaseException as e:  # timeouts, closed sockets, decode errors
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                msg = f"{type(e).__name__}: {str(e)[:160]}".strip().replace("\n", " ")
                errors.append(f"{url.split('://')[-1]}: {msg}")
        if rnd + 1 < rounds:
            time.sleep(2)
    raise Unreadable("could not read the chain (" + "; ".join(errors[-4:]) + ")")


def _record(path, r: Reader, at_block):
    data = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    entry = {"block": r.block, "block_hash": r.block_hash, "reads": r.recorded}
    if at_block is None:
        data.update({k: v for k, v in entry.items() if k != "reads"})
        data.setdefault("reads", {}).update(r.recorded)
    else:
        data.setdefault("past", {})[str(at_block)] = entry
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), sort_keys=True)


# ---- reads shared by several commands --------------------------------------------
async def read_subnets(r: Reader) -> dict:
    """netuid -> subnet facts from get_all_dynamic_info + current prices."""
    dyn, prices = await asyncio.gather(
        r.call("SubnetInfoRuntimeApi", "get_all_dynamic_info", []),
        r.call("SwapRuntimeApi", "current_alpha_price_all", []),
    )
    price = {int(p["netuid"]): int(p["price"]) for p in prices or []}
    nets = sorted(int(d["netuid"]) for d in dyn or [] if d)
    # A subnet's TAO emission per block is what goes into its pool
    # (SubnetTaoInEmission) plus the excess the chain uses to buy its alpha
    # (SubnetExcessTao); over all subnets the two add up to the block emission.
    excess = dict(zip(nets, await r.query_many("SubtensorModule", "SubnetExcessTao", [[n] for n in nets]))) if nets else {}
    out = {}
    for d in dyn or []:
        if not d:
            continue
        n = int(d["netuid"])
        ident = d.get("subnet_identity") or {}
        out[n] = {
            "netuid": n,
            "name": text_of(ident.get("subnet_name")) or text_of(d.get("subnet_name")),
            "symbol": text_of(d.get("token_symbol")),
            "price_rao": price.get(n),
            "tao_in": int(d["tao_in"]),
            "alpha_in": int(d["alpha_in"]),
            "alpha_out": int(d["alpha_out"]),
            "tao_in_emission": int(d["tao_in_emission"]),
            "excess_tao": None if excess.get(n) is None else int(excess[n]),
            "tao_emission": None if excess.get(n) is None else int(d["tao_in_emission"]) + int(excess[n]),
            "alpha_out_emission": int(d["alpha_out_emission"]),
            "alpha_in_emission": int(d["alpha_in_emission"]),
            "moving_price": fixed_point(d.get("moving_price")),
            "tempo": int(d["tempo"]),
            "registered_at": int(d["network_registered_at"]),
            "owner_hotkey": d.get("owner_hotkey"),
            "owner_coldkey": d.get("owner_coldkey"),
            "volume": int(d.get("subnet_volume") or 0),
            "identity": {
                "github": text_of(ident.get("github_repo")),
                "url": text_of(ident.get("subnet_url")),
                "description": text_of(ident.get("description")),
                "discord": text_of(ident.get("discord")),
                "contact": text_of(ident.get("subnet_contact")),
            } if ident else None,
        }
    return out


def price_of(sn: dict) -> Decimal | None:
    if sn is None:
        return None
    if sn["netuid"] == 0:
        return Decimal(1)
    return tao(sn["price_rao"])


def price_notes(subnets: dict, netuids) -> list[str]:
    """Cross-check: the swap's price against the pool's reserve ratio."""
    notes = []
    for n in netuids:
        sn = subnets.get(n)
        if not sn or n == 0 or not sn["alpha_in"] or sn["price_rao"] is None:
            continue
        ratio = Decimal(sn["tao_in"]) / Decimal(sn["alpha_in"])
        p = tao(sn["price_rao"])
        if p and abs(ratio - p) / p > Decimal("0.05"):
            notes.append(f"SN{n}: swap price {_num(q9(p))} differs from the pool ratio {_num(q9(ratio))} by over 5%")
    return notes


# ---- history files -------------------------------------------------------------------
def data_dir() -> str:
    d = os.environ.get("TAOQ_DATA_DIR")
    if not d:
        ws = os.environ.get("OPENCLAW_WORKSPACE_DIR") or os.path.join(os.path.expanduser("~"), "workspace")
        d = os.path.join(ws, "taoq")
    return d


def now() -> float:
    return float(os.environ.get("TAOQ_NOW") or time.time())


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        # A damaged file is set aside, never silently trusted.
        try:
            os.replace(path, f"{path}.damaged-{int(time.time())}")
        except OSError:
            pass
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, path)


def pick_base(snaps: list, t_now: float, since_hours: float):
    """The newest snapshot at least `since_hours` old; else the oldest one."""
    older = [s for s in snaps if t_now - s["at"] >= since_hours * 3600]
    if older:
        return max(older, key=lambda s: s["at"])
    earlier = [s for s in snaps if t_now - s["at"] >= min(3600, since_hours * 3600)]
    return min(earlier, key=lambda s: s["at"]) if earlier else None


def subnet_history_update(subnets: dict, block: int, t_now: float, save: bool) -> list:
    path = os.path.join(data_dir(), "subnets.json")
    hist = load_json(path, {"version": VERSION, "snapshots": []})
    snaps = [s for s in hist.get("snapshots", []) if isinstance(s, dict) and "at" in s]
    if save and (not snaps or t_now - max(s["at"] for s in snaps) >= SUBNET_HISTORY_GAP):
        snaps.append({
            "at": t_now, "block": block,
            "subnets": {str(n): [s["price_rao"], s["tao_in"], s["alpha_in"], s["tao_emission"], s["registered_at"], s["name"]]
                        for n, s in subnets.items()},
        })
        snaps = [s for s in snaps if t_now - s["at"] <= SUBNET_HISTORY_DAYS * 86400]
        save_json(path, {"version": VERSION, "snapshots": snaps})
    return snaps


def _hist_row(snap, n):
    row = snap["subnets"].get(str(n))
    if not row:
        return None
    return {"price_rao": row[0], "tao_in": row[1], "alpha_in": row[2], "tao_emission": row[3],
            "registered_at": row[4], "name": row[5]}


def head(r: Reader, t_now: float) -> dict:
    return {"block": r.block, "at": iso(t_now), "finalized": True}


# ---- watches: only what is new --------------------------------------------------
# `taoq hotkey --watch` and `taoq subnet --watch` are for scheduled jobs. Every
# finding has a stable key (for example weights_stale:SN70) and a severity. The
# findings of the last check are kept in $TAOQ_DATA_DIR/watch-*.json; a run tells
# only what is new, what got worse, and what cleared, and says in one field
# ("alert") whether there is anything to tell. What is unchanged is listed in
# one short line and is not to be repeated to the user.
SEVERITIES = {"info": 0, "warning": 1, "critical": 2}
NO_REPLY = "NO_REPLY"    # the token OpenClaw's scheduler does not deliver
WATCH_NOTE = "Watching from now: I will message you only when something new or worse shows up, or a problem clears."


def watch_path(kind: str, ident: str) -> str:
    return os.path.join(data_dir(), f"watch-{kind}-{ident}.json")


def load_watch(path: str) -> dict | None:
    """The state of the last check, or None (first check, or an unusable file)."""
    st = load_json(path, None)
    if not isinstance(st, dict) or not isinstance(st.get("alerts"), dict) or not isinstance(st.get("at"), (int, float)):
        return None
    return st


def save_watch(path: str, found_state: dict, memo: dict, block: int, t_now: float):
    save_json(path, {"version": VERSION, "at": t_now, "block": block, "alerts": found_state, "memo": memo})


def _by_severity(items):
    return sorted(items, key=lambda i: (-SEVERITIES[i["severity"]], i["key"]))


def watch_diff(prev_alerts: dict | None, found: dict, t_now: float) -> dict:
    """Compare this check's findings with the last check's.

    found: key -> {"severity", "text", optional "event": True}. A condition
    (no "event") stays `ongoing` for as long as it holds at the same severity.
    An event (a price move, a release) is `new` when it happens and is not kept.
    Returns new / escalated / ongoing (short strings) / resolved, and `stored`:
    the conditions to keep for the next check."""
    prev_alerts = prev_alerts or {}
    new, escalated, ongoing, resolved, stored = [], [], [], [], {}
    for key in sorted(found):
        f = found[key]
        item = {"key": key, "severity": f["severity"], "text": f["text"]}
        if "data" in f:
            item["data"] = f["data"]
        if f.get("event"):
            new.append(item)
            continue
        old = prev_alerts.get(key)
        if not isinstance(old, dict) or old.get("severity") not in SEVERITIES:
            new.append(item)
            stored[key] = {"severity": f["severity"], "text": f["text"], "since": iso(t_now)}
            continue
        since = old.get("since") or iso(t_now)
        stored[key] = {"severity": f["severity"], "text": f["text"], "since": since}
        if SEVERITIES[f["severity"]] > SEVERITIES[old["severity"]]:
            escalated.append({**item, "was": old["severity"]})
        else:
            ongoing.append(f"{key} {f['severity']} since {since}")   # also when it got better: not an alert
    for key in sorted(prev_alerts):
        old = prev_alerts[key]
        if key not in found and isinstance(old, dict):
            resolved.append({"key": key, "was": old.get("severity"), "text": old.get("text")})
    return {"new": _by_severity(new), "escalated": _by_severity(escalated), "ongoing": ongoing,
            "resolved": resolved, "stored": stored}


def watch_output(command: str, who: dict, block: int, t_now: float, prev: dict | None, diff: dict, unread: list) -> dict:
    first = prev is None
    alert = first or bool(diff["new"] or diff["escalated"] or diff["resolved"])
    out = {"ok": True, "command": command, "watch": True, "alert": alert, **who, **head_dict(block, t_now),
           "first_check": first,
           "previous_check": None if first else {"at": iso(prev["at"]), "block": prev["block"],
                                                 "hours": round((t_now - prev["at"]) / 3600, 1)},
           "new": diff["new"], "escalated": diff["escalated"], "resolved": diff["resolved"], "ongoing": diff["ongoing"]}
    if first:
        out["summary"] = "first check: the current state is now recorded; later checks report only changes"
    elif alert:
        out["summary"] = (f"{len(diff['new'])} new, {len(diff['escalated'])} worse, {len(diff['resolved'])} resolved; "
                          f"{len(diff['ongoing'])} unchanged (do not repeat those)")
    else:
        out["summary"] = f"nothing new, worse or resolved since {iso(prev['at'])}; send no message"
    if unread:
        out["unread"] = unread
    return out


def watch_text(o: dict, label: str, first_report: str) -> str:
    """alert false: exactly NO_REPLY. First check: the full report and a note."""
    if not o["alert"]:
        return NO_REPLY
    if o["first_check"]:
        lines = [first_report, WATCH_NOTE]
    else:
        lines = [f"{label} (block {o['block']}, {o['at']}), {o['previous_check']['hours']} h since the last check"]
        lines += [f"NEW {i['severity']}: {i['text']}" for i in o["new"]]
        lines += [f"WORSE, now {i['severity']} (was {i['was']}): {i['text']}" for i in o["escalated"]]
        lines += [f"RESOLVED (was {i['was']}): {i['text']}" for i in o["resolved"]]
        if o["ongoing"]:
            lines.append("Same as before, not new: " + "; ".join(o["ongoing"]))
    if o.get("unread"):
        lines.append("Could not read: " + "; ".join(o["unread"]))
    return "\n".join(lines)


# ---- stake -------------------------------------------------------------------------
async def _stake_reads(r: Reader, coldkey: str):
    subnets, infos, account = await asyncio.gather(
        read_subnets(r),
        r.call("StakeInfoRuntimeApi", "get_stake_info_for_coldkey", [coldkey]),
        r.query("System", "Account", [coldkey]),
    )
    positions = []
    for i in infos or []:
        if i.get("coldkey") != coldkey:
            raise Unreadable("the chain's answer names another coldkey; the decoder is out of step with the runtime")
        st = i.get("stake")
        if isinstance(st, bool) or not isinstance(st, int) or st < 0:
            raise Unreadable("a stake amount in the chain's answer is not a whole number of rao")
        if st == 0:
            continue
        positions.append({"netuid": int(i["netuid"]), "hotkey": i["hotkey"], "stake_rao": st,
                          "locked_rao": int(i.get("locked") or 0), "registered": bool(i.get("is_registered"))})
    # Validator facts for each (subnet, hotkey) the coldkey stakes to.
    pairs = sorted({(p["netuid"], p["hotkey"]) for p in positions if p["netuid"] != 0})
    uids = await r.query_many("SubtensorModule", "Uids", [[n, h] for n, h in pairs]) if pairs else []
    neurons = {}
    lookups = [(n, h, u) for (n, h), u in zip(pairs, uids) if u is not None]
    lites = await asyncio.gather(*[r.call("NeuronInfoRuntimeApi", "get_neuron_lite", [n, u]) for n, h, u in lookups])
    for (n, h, u), lite in zip(lookups, lites):
        if lite and lite.get("hotkey") == h:
            neurons[(n, h)] = {"uid": u, "permit": bool(lite.get("validator_permit")),
                               "last_update": int(lite.get("last_update") or 0)}
    # Names of the validators (on-chain identity of the hotkey's owner).
    hks = sorted({p["hotkey"] for p in positions})
    owners = await r.query_many("SubtensorModule", "Owner", [[h] for h in hks]) if hks else []
    cks = sorted({o for o in owners if o})
    ids = await r.query_many("SubtensorModule", "IdentitiesV2", [[c] for c in cks]) if cks else []
    idname = {c: text_of((i or {}).get("name")) for c, i in zip(cks, ids)}
    names = {h: idname.get(o) for h, o in zip(hks, owners)}
    own = {h for h, o in zip(hks, owners) if o == coldkey}
    # What unstaking each position now would return (after slippage and fees).
    alpha_pos = [p for p in positions if p["netuid"] != 0]
    sims = await asyncio.gather(*[r.call("SwapRuntimeApi", "sim_swap_alpha_for_tao", [p["netuid"], p["stake_rao"]])
                                  for p in alpha_pos])
    sim = {(p["netuid"], p["hotkey"]): s for p, s in zip(alpha_pos, sims)}
    free = ((account or {}).get("data") or {}).get("free")
    return {"subnets": subnets, "positions": positions, "neurons": neurons, "names": names, "own": own, "sim": sim, "free": free}


def stake_report(coldkey: str, reads: dict, block: int, t_now: float, prev: dict | None,
                 price_alert: Decimal, stale_blocks: int | None) -> dict:
    subnets = reads["subnets"]
    unread = []
    rows = []
    total_value = Decimal(0)
    total_sell = Decimal(0)
    value_complete = True
    sell_complete = True
    prev_pos = {(p["netuid"], p["hotkey"]): p for p in (prev or {}).get("positions", [])}
    flags = []
    for p in sorted(reads["positions"], key=lambda x: (x["netuid"], x["hotkey"])):
        n, h = p["netuid"], p["hotkey"]
        sn = subnets.get(n)
        price = price_of(sn)
        amount = tao(p["stake_rao"])
        value = q9(amount * price) if price is not None else None
        if value is None:
            value_complete = False
            unread.append(f"price of SN{n}")
        else:
            total_value += value
        row = {"netuid": n, "subnet": (sn or {}).get("name"), "hotkey": h, "validator": reads["names"].get(h),
               "alpha" if n else "tao": amount, "price": q9(price), "value_tao": value}
        row["own_hotkey"] = h in reads["own"]
        if p["locked_rao"]:
            row["locked"] = tao(p["locked_rao"])
        if n:
            s = reads["sim"].get((n, h))
            if s and isinstance(s.get("tao_amount"), int):
                row["unstake_now_tao"] = tao(s["tao_amount"])
                total_sell += row["unstake_now_tao"]
            else:
                row["unstake_now_tao"] = None
                sell_complete = False
                unread.append(f"unstake quote for SN{n}")
        else:
            total_sell += amount
        nf = reads["neurons"].get((n, h))
        if n:
            row["hotkey_registered"] = p["registered"]
            row["validator_permit"] = nf["permit"] if nf else (False if not p["registered"] else None)
            if nf:
                row["blocks_since_weights"] = block - nf["last_update"] if nf["last_update"] else None
            if not p["registered"]:
                flags.append({"kind": "hotkey_not_registered", "netuid": n, "hotkey": h,
                              "text": f"SN{n}: the hotkey {short(h)} you stake to is not registered on this subnet, so the stake earns nothing there"})
            elif nf and not nf["permit"] and not row["own_hotkey"]:
                flags.append({"kind": "no_validator_permit", "netuid": n, "hotkey": h,
                              "text": f"SN{n}: the hotkey {short(h)} has no validator permit, so the stake earns no validator dividends"})
            elif nf and nf["permit"]:
                limit = stale_blocks if stale_blocks else 3 * ((sn or {}).get("tempo") or 360)
                since = row.get("blocks_since_weights")
                if since is not None and since > limit:
                    flags.append({"kind": "weights_stale", "netuid": n, "hotkey": h, "blocks": since,
                                  "text": f"SN{n}: validator {short(h)} last set weights {since} blocks (about {since * 12 / 3600:.1f} h) ago"})
        old = prev_pos.pop((n, h), None)
        if prev is not None:
            if old is None:
                row["change"] = {"new": True}
                flags.append({"kind": "new_position", "netuid": n, "hotkey": h,
                              "text": f"SN{n}: a new position on {short(h)} since the last report"})
            else:
                a0, a1 = tao(old["stake_rao"]), amount
                p0 = tao(old["price_rao"]) if old.get("price_rao") is not None else None
                ch = {"alpha" if n else "tao": a1 - a0}
                if n and price is not None and p0 is not None:
                    ch["price"] = q9(price - p0)
                    ch["price_pct"] = pct(price, p0)
                    # value change = from alpha change (at today's price) + from price change (on the old amount)
                    ch["value_from_alpha_tao"] = q9((a1 - a0) * price)
                    ch["value_from_price_tao"] = q9(a0 * (price - p0))
                    if ch["price_pct"] is not None and abs(ch["price_pct"]) >= price_alert:
                        flags.append({"kind": "price_move", "netuid": n, "pct": ch["price_pct"],
                                      "text": f"SN{n} price {signed(ch['price_pct'], 2)}% since the last report"})
                if a1 < a0:
                    flags.append({"kind": "stake_decreased", "netuid": n, "hotkey": h,
                                  "text": f"SN{n}: stake on {short(h)} went down by {fmt(a0 - a1)} (unstaked or moved?)"})
                if old.get("permit") and row.get("validator_permit") is False:
                    flags.append({"kind": "validator_permit_lost", "netuid": n, "hotkey": h,
                                  "text": f"SN{n}: validator {short(h)} lost its validator permit"})
                row["change"] = ch
        rows.append(row)
    for (n, h), old in sorted(prev_pos.items()):
        flags.append({"kind": "position_gone", "netuid": n, "hotkey": h, "was": tao(old["stake_rao"]),
                      "text": f"SN{n}: the position on {short(h)} ({fmt(tao(old['stake_rao']))} {'TAO' if n == 0 else 'alpha'}) is gone"})
    free = tao(reads["free"]) if reads["free"] is not None else None
    if free is None:
        unread.append("free balance")
    totals = {
        "positions": len(rows),
        "stake_value_tao": q9(total_value) if value_complete else None,
        "unstake_now_tao": q9(total_sell) if sell_complete else None,
        "free_tao": free,
        "total_tao": q9(total_value + free) if value_complete and free is not None else None,
    }
    out = {"ok": True, "command": "stake", "coldkey": coldkey, **head_dict(block, t_now), "totals": totals,
           "positions": rows, "flags": flags}
    if prev is not None:
        hours = (t_now - prev["at"]) / 3600
        earned = Decimal(0)
        price_eff = Decimal(0)
        for row in rows:
            ch = row.get("change") or {}
            earned += ch.get("value_from_alpha_tao") or 0
            price_eff += ch.get("value_from_price_tao") or 0
            if row["netuid"] == 0 and "tao" in ch:
                earned += ch["tao"]
        out["since"] = {
            "at": iso(prev["at"]), "block": prev["block"], "hours": round(hours, 1),
            "value_from_alpha_tao": q9(earned),
            "value_from_price_tao": q9(price_eff),
            "note": "alpha growth is staking rewards only if you did not add, remove or move stake in between",
        }
        if prev.get("total_value_tao") is not None and totals["stake_value_tao"] is not None:
            out["since"]["stake_value_change_tao"] = q9(totals["stake_value_tao"] - Decimal(prev["total_value_tao"]))
    else:
        out["since"] = None
    notes = price_notes(subnets, {r_["netuid"] for r_ in rows})
    if notes:
        out["notes"] = notes
    if unread:
        out["unread"] = unread
    return out


def head_dict(block, t_now):
    return {"block": block, "at": iso(t_now)}


def stake_snapshot(report: dict, reads: dict, t_now: float) -> dict:
    positions = []
    for p in reads["positions"]:
        sn = reads["subnets"].get(p["netuid"])
        nf = reads["neurons"].get((p["netuid"], p["hotkey"]))
        positions.append({"netuid": p["netuid"], "hotkey": p["hotkey"], "stake_rao": p["stake_rao"],
                          "price_rao": (RAO if p["netuid"] == 0 else (sn or {}).get("price_rao")),
                          "permit": nf["permit"] if nf else None})
    tv = report["totals"]["stake_value_tao"]
    return {"at": t_now, "block": report["block"], "free_rao": reads["free"],
            "total_value_tao": _num(tv) if tv is not None else None, "positions": positions}


def stake_text(o: dict) -> str:
    t = o["totals"]
    lines = [f"Stake report for {short(o['coldkey'])} (block {o['block']}, {o['at']})"]
    for p in o["positions"]:
        n = p["netuid"]
        who = ("your own hotkey " if p.get("own_hotkey") else "") + (p.get("validator") or short(p["hotkey"]))
        name = f"SN{n}" + (f" {p['subnet']}" if p.get("subnet") else "")
        if n == 0:
            line = f"- {name}: {fmt(p['tao'])} TAO on {who}"
        else:
            line = f"- {name}: {fmt(p['alpha'])} α × {fmt(p['price'], 6)} = {fmt(p['value_tao'])} TAO on {who}"
        ch = p.get("change")
        if ch and not ch.get("new"):
            bits = []
            a = ch.get("alpha", ch.get("tao"))
            if a is not None and a != 0:
                bits.append(f"{signed(a)} {'TAO' if n == 0 else 'α'}")
            if ch.get("price_pct") is not None:
                bits.append(f"price {signed(ch['price_pct'], 2)}%")
            if bits:
                line += " (" + ", ".join(bits) + ")"
        elif ch and ch.get("new"):
            line += " (new)"
        lines.append(line)
    if not o["positions"]:
        lines.append("- no stake positions")
    lines.append(f"Total: {fmt(t['stake_value_tao'])} TAO staked (at current prices; about {fmt(t['unstake_now_tao'])} TAO if unstaked now), "
                 f"{fmt(t['free_tao'])} TAO free, {fmt(t['total_tao'])} TAO in all.")
    s = o.get("since")
    if s:
        lines.append(f"Since {s['at']} ({s['hours']} h): {signed(s['value_from_alpha_tao'])} TAO from more alpha (rewards, unless you moved stake), "
                     f"{signed(s['value_from_price_tao'])} TAO from price moves.")
    else:
        lines.append("First report: changes will show from the next one.")
    for f in o["flags"]:
        lines.append(f"! {f['text']}")
    if o.get("unread"):
        lines.append("Could not read: " + ", ".join(o["unread"]))
    return "\n".join(lines)


def cmd_stake(args):
    ck = check_ss58(args.coldkey, "the coldkey")
    reads, r = run_read(lambda rd: _stake_reads(rd, ck))
    t_now = now()
    path = os.path.join(data_dir(), f"stake-{ck}.json")
    hist = load_json(path, {"version": VERSION, "coldkey": ck, "snapshots": []})
    snaps = [s for s in hist.get("snapshots", []) if isinstance(s, dict) and "at" in s]
    prev = pick_base(snaps, t_now, args.since_hours)
    out = stake_report(ck, reads, r.block, t_now, prev, Decimal(str(args.price_alert)), args.stale_blocks)
    if not args.no_save:
        snaps.append(stake_snapshot(out, reads, t_now))
        save_json(path, {"version": VERSION, "coldkey": ck, "snapshots": snaps[-HISTORY_MAX:]})
    subnet_history_update(reads["subnets"], r.block, t_now, not args.no_save)
    return out, stake_text


# ---- subnet ------------------------------------------------------------------------
async def _subnet_reads(r: Reader, netuids: list[int]):
    subnets = await read_subnets(r)
    missing = [n for n in netuids if n not in subnets]
    if missing:
        raise Usage("no such subnet: " + ", ".join(f"SN{n}" for n in missing))
    nl = [[n] for n in netuids]
    count, maxu, burn, immunity, regs = await asyncio.gather(
        r.query_many("SubtensorModule", "SubnetworkN", nl),
        r.query_many("SubtensorModule", "MaxAllowedUids", nl),
        r.query_many("SubtensorModule", "Burn", nl),
        r.query_many("SubtensorModule", "ImmunityPeriod", nl),
        r.query_many("SubtensorModule", "NetworkRegistrationAllowed", nl),
    )
    extra = {n: {"neurons": c, "max_neurons": m, "burn": b, "immunity_period": i, "registration_allowed": g}
             for n, c, m, b, i, g in zip(netuids, count, maxu, burn, immunity, regs)}
    return {"subnets": subnets, "extra": extra}


def subnet_facts(n: int, subnets: dict, extra: dict, block: int, snaps: list, t_now: float, since_hours: float) -> dict:
    sn = subnets[n]
    ex = extra.get(n, {})
    total_emission = sum(s["tao_emission"] or 0 for s in subnets.values())
    price = price_of(sn)
    epochs_per_day = Decimal(BLOCKS_PER_DAY) / (sn["tempo"] + 1)
    out = {
        "netuid": n, "name": sn["name"], "symbol": sn["symbol"],
        "price": q9(price),
        "ema_price": q9(sn["moving_price"]) if sn["moving_price"] is not None else None,
        "pool": {"tao": tao(sn["tao_in"]), "alpha": tao(sn["alpha_in"])},
        "alpha_outstanding": tao(sn["alpha_out"]),
        "market_cap_tao": q9(tao(sn["alpha_out"] + sn["alpha_in"]) * price) if price is not None else None,
        "emission": {
            "tao_per_day": q9(tao(sn["tao_emission"]) * BLOCKS_PER_DAY) if sn["tao_emission"] is not None else None,
            "share_pct": (Decimal(sn["tao_emission"]) * 100 / total_emission).quantize(Decimal("0.01"))
            if total_emission and sn["tao_emission"] is not None else None,
            "tao_into_pool_per_day": q9(tao(sn["tao_in_emission"]) * BLOCKS_PER_DAY),
            "tao_for_alpha_buys_per_day": q9(tao(sn["excess_tao"]) * BLOCKS_PER_DAY) if sn["excess_tao"] is not None else None,
            "alpha_per_day": q9(tao(sn["alpha_out_emission"]) * BLOCKS_PER_DAY),
            "basis": "current per-block rate x 7200 blocks",
        },
        "tempo": sn["tempo"], "epochs_per_day": epochs_per_day.quantize(Decimal("0.01")),
        "neurons": ex.get("neurons"), "max_neurons": ex.get("max_neurons"),
        "registration_cost_tao": tao(ex.get("burn")),
        "registration_allowed": ex.get("registration_allowed"),
        "immunity_period_blocks": ex.get("immunity_period"),
        "registered_at_block": sn["registered_at"],
        "age_days": round((block - sn["registered_at"]) / BLOCKS_PER_DAY, 1),
        "owner_hotkey": sn["owner_hotkey"], "owner_coldkey": sn["owner_coldkey"],
        "identity": sn["identity"],
    }
    gh = (sn["identity"] or {}).get("github") or ""
    m = re.match(r"(?:(?:https?://)?(?:www\.)?github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", gh.strip())
    if m:
        out["releases_api"] = f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}/releases?per_page=3"
        out["commits_api"] = f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}/commits?per_page=3"
    changes = {}
    for label, hours in (("since_last", since_hours), ("7d", 6 * 24)):
        base = pick_base(snaps, t_now, hours)
        row = _hist_row(base, n) if base else None
        if row and row["registered_at"] == sn["registered_at"] and (label == "since_last" or t_now - base["at"] >= hours * 3600):
            changes[label] = {"at": iso(base["at"]), "hours": round((t_now - base["at"]) / 3600, 1),
                              "price_pct": pct(sn["price_rao"], row["price_rao"]) if n else Decimal(0),
                              "pool_tao_pct": pct(sn["tao_in"], row["tao_in"])}
    out["price_change"] = changes or None
    return out


def subnet_text_one(s: dict) -> str:
    e = s["emission"]
    lines = [f"SN{s['netuid']} {s['name'] or ''} ({s['symbol'] or ''}): price {fmt(s['price'], 6)} TAO/α, pool {fmt(s['pool']['tao'])} TAO / {fmt(s['pool']['alpha'])} α",
             f"  emission {fmt(e['tao_per_day'])} TAO/day ({e['share_pct']}% of all TAO emission); "
             f"neurons {s['neurons']}/{s['max_neurons']}, registration {fmt(s['registration_cost_tao'], 6)} TAO; age {s['age_days']} days"]
    for k, c in (s.get("price_change") or {}).items():
        lines.append(f"  price {signed(c['price_pct'], 2)}% since {c['at']} ({c['hours']} h)")
    ident = s.get("identity") or {}
    if ident.get("github") or ident.get("url"):
        lines.append(f"  {ident.get('github') or ''} {ident.get('url') or ''}".rstrip())
    return "\n".join(lines)


def fetch_releases(url: str, timeout: float = 10) -> list | None:
    """Recent GitHub releases (newest first) from a releases_api link, or None if
    they could not be read. Public API, no token. Never touches the network in
    fixture mode (TAOQ_FIXTURE) unless TAOQ_RELEASES_FIXTURE gives the answers."""
    fx = os.environ.get("TAOQ_RELEASES_FIXTURE")
    try:
        if fx:
            with open(fx, encoding="utf-8") as f:
                data = json.load(f).get(url)
        elif os.environ.get("TAOQ_FIXTURE"):
            return None
        else:
            if not url.startswith("https://api.github.com/repos/"):
                return None
            import urllib.request  # noqa: PLC0415
            req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "taoq"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https, fixed host)
                data = json.loads(resp.read(2_000_000))
    except (OSError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    return [{"tag": str(x.get("tag_name") or ""), "name": str(x.get("name") or x.get("tag_name") or ""),
             "published_at": x.get("published_at"), "url": x.get("html_url"), "prerelease": bool(x.get("prerelease"))}
            for x in data if isinstance(x, dict) and x.get("tag_name") and not x.get("draft")]


def subnet_watch(items: list, subnets: dict, releases: dict, prev: dict | None, threshold: Decimal) -> tuple[dict, dict, list]:
    """(findings, memo for the next check, unread). All findings are events:
    a price move since the last check, a subnet replaced by another one, a new
    release. The first check only records the state."""
    memo_prev = (prev or {}).get("memo") or {}
    old_price = memo_prev.get("price_rao") or {}
    old_reg = memo_prev.get("registered_at") or {}
    old_rel = memo_prev.get("releases_seen") or {}
    found, unread = {}, []
    memo = {"price_rao": {}, "registered_at": {}, "releases_seen": {}}
    for it in items:
        n = it["netuid"]
        sn = subnets[n]
        label = f"SN{n} {sn['name'] or ''}".strip()
        memo["price_rao"][str(n)] = sn["price_rao"]
        memo["registered_at"][str(n)] = sn["registered_at"]
        replaced = str(n) in old_reg and old_reg[str(n)] != sn["registered_at"]
        if replaced:
            found[f"subnet_replaced:SN{n}"] = {"severity": "critical", "event": True,
                                               "text": f"{label}: this netuid now belongs to a different subnet (registered at block {sn['registered_at']}, it was {old_reg[str(n)]})"}
        elif n and sn["price_rao"] is not None and old_price.get(str(n)):
            ch = pct(sn["price_rao"], old_price[str(n)])
            if ch is not None and abs(ch) >= threshold:
                found[f"price_move:SN{n}"] = {"severity": "warning", "event": True,
                                              "text": f"{label}: price {signed(ch, 2)}% since the last check ({_num(tao(old_price[str(n)]))} to {_num(tao(sn['price_rao']))} TAO per alpha)",
                                              "data": {"price_before": tao(old_price[str(n)]), "price_after": tao(sn["price_rao"]), "price_pct": ch}}
        seen = old_rel.get(str(n))
        rels = releases.get(n)
        if it.get("releases_api") and rels is None:
            unread.append(f"releases of SN{n}")
        if rels is not None:
            tags = [x["tag"] for x in rels]
            if seen is not None:
                for x in rels:
                    if x["tag"] not in seen:
                        pre = " (pre-release)" if x["prerelease"] else ""
                        found[f"release:SN{n}:{x['tag']}"] = {"severity": "info", "event": True,
                                                              "text": f"{label}: new release {x['tag']}{pre}, published {x['published_at']} {x['url'] or ''}".rstrip()}
            memo["releases_seen"][str(n)] = sorted(set(tags) | set(seen or []))[-30:]
        elif seen is not None:
            memo["releases_seen"][str(n)] = seen
    return found, memo, unread


def cmd_subnet(args):
    netuids = list(dict.fromkeys(args.netuid))
    reads, r = run_read(lambda rd: _subnet_reads(rd, netuids))
    t_now = now()
    snaps = subnet_history_update(reads["subnets"], r.block, t_now, not args.no_save)
    prior = [s for s in snaps if s["at"] < t_now]
    items = [subnet_facts(n, reads["subnets"], reads["extra"], r.block, prior, t_now, args.since_hours) for n in netuids]
    out = {"ok": True, "command": "subnet", **head_dict(r.block, t_now), "subnets": items}
    notes = price_notes(reads["subnets"], netuids)
    if notes:
        out["notes"] = notes

    def text(o):
        return "\n".join(subnet_text_one(s) for s in o["subnets"])
    if not args.watch:
        return out, text

    path = watch_path("subnet", "-".join(str(n) for n in sorted(netuids)))
    prev = load_watch(path)
    releases = {it["netuid"]: fetch_releases(it["releases_api"]) for it in items if it.get("releases_api")}
    found, memo, unread = subnet_watch(items, reads["subnets"], releases, prev, Decimal(str(args.price_alert)))
    diff = watch_diff(prev["alerts"] if prev else None, found, t_now)
    w = watch_output("subnet", {"netuids": netuids}, r.block, t_now, prev, diff, unread)
    if not args.no_save:
        save_watch(path, diff["stored"], memo, r.block, t_now)
    return w, lambda o: watch_text(o, "Subnet watch " + ", ".join(f"SN{n}" for n in netuids), text(out))


# ---- hotkey ------------------------------------------------------------------------
async def _metagraph_extras(r: Reader, n: int):
    mg, lim, minfree = await asyncio.gather(
        r.call("SubnetInfoRuntimeApi", "get_metagraph", [n]),
        r.query("SubtensorModule", "ImmuneOwnerUidsLimit", [n]),
        r.query("SubtensorModule", "MinNonImmuneUids", [n]),
    )
    return {"mg": mg, "owner_limit": lim, "min_non_immune": minfree}


async def _hotkey_reads(r: Reader, hotkey: str, netuid: int | None):
    subnets = await read_subnets(r)
    if netuid is not None:
        if netuid not in subnets:
            raise Usage(f"no such subnet: SN{netuid}")
        cands = [netuid]
    else:
        cands = sorted(n for n in subnets if n != 0)
    uids = await r.query_many("SubtensorModule", "Uids", [[n, hotkey] for n in cands])
    on = [(n, u) for n, u in zip(cands, uids) if u is not None]
    graphs = await asyncio.gather(*[_metagraph_extras(r, n) for n, _ in on])
    lites = await asyncio.gather(*[r.call("NeuronInfoRuntimeApi", "get_neuron_lite", [n, u]) for n, u in on])
    owner = await r.query("SubtensorModule", "Owner", [hotkey])
    return {"subnets": subnets, "on": on, "graphs": graphs, "lites": lites, "owner": owner}


def immortal_uids(mg: dict, owner_limit: int) -> set[int]:
    """subtensor get_immune_owner_tuples: the subnet owner's hotkeys on the subnet,
    oldest registration first, at most ImmuneOwnerUidsLimit, with the subnet
    owner hotkey put first."""
    owner_ck = mg["owner_coldkey"]
    trip = sorted((mg["block_at_registration"][u], u) for u, ck in enumerate(mg["coldkeys"]) if ck == owner_ck)
    chosen = [u for _, u in trip][:owner_limit]
    ohk = mg.get("owner_hotkey")
    if ohk in mg["hotkeys"]:
        ou = mg["hotkeys"].index(ohk)
        if ou not in chosen:
            chosen.insert(0, ou)
            chosen = chosen[:owner_limit]
    return set(chosen)


def prune_order(mg: dict, block: int, owner_limit: int, min_non_immune: int):
    """What subtensor's get_neuron_to_prune does (pallets/subtensor/src/subnets/
    registration.rs): skip the owner's immortal uids; among the rest, lowest
    emission first, then earliest registration, then lowest uid; non-immune
    neurons first unless there are no more than MinNonImmuneUids of them.
    Returns (non_immune_order, immune_order, immortal set, next uid to prune)."""
    n = len(mg["hotkeys"])
    imm_period = int(mg["immunity_period"])
    immortal = immortal_uids(mg, owner_limit)
    non_immune, immune = [], []
    for u in range(n):
        if u in immortal:
            continue
        key = (int(mg["emission"][u]), int(mg["block_at_registration"][u]), u)
        (immune if block - key[1] < imm_period else non_immune).append(key)
    non_immune.sort()
    immune.sort()
    nxt = None
    if non_immune and len(non_immune) > min_non_immune:
        nxt = non_immune[0][2]
    elif immune:
        nxt = immune[0][2]
    elif non_immune:
        nxt = non_immune[0][2]
    return [k[2] for k in non_immune], [k[2] for k in immune], immortal, nxt


def hotkey_entry(n: int, uid: int, g: dict, lite: dict, sn: dict, block: int, hotkey: str) -> dict:
    mg = g["mg"]
    if mg["hotkeys"][uid] != hotkey:
        raise Unreadable(f"SN{n}: the metagraph does not list this hotkey at uid {uid}; the read is inconsistent")
    tempo = int(mg["tempo"])
    per_day = Decimal(BLOCKS_PER_DAY) / (tempo + 1)
    em = tao(mg["emission"][uid])
    reg = int(mg["block_at_registration"][uid])
    imm = int(mg["immunity_period"])
    last = int(mg["last_update"][uid])
    full = int(mg["num_uids"]) >= int(mg["max_uids"])
    non_immune, immune, immortal, nxt = prune_order(mg, block, int(g["owner_limit"] or 0), int(g["min_non_immune"] or 0))
    e = {
        "netuid": n, "subnet": sn.get("name"), "uid": uid,
        "active": bool(mg["active"][uid]),
        "validator_permit": bool(mg["validator_permit"][uid]),
        "incentive": frac(mg["incentives"][uid]), "dividends": frac(mg["dividends"][uid]),
        "consensus": frac(lite.get("consensus")), "trust": frac(lite.get("trust")),
        "validator_trust": frac(lite.get("validator_trust")), "rank": frac(lite.get("rank")),
        "emission_alpha_per_epoch": em,
        "emission_alpha_per_day": q9(em * per_day),
        "emission_tao_per_day_at_price": q9(em * per_day * price_of(sn)) if price_of(sn) is not None else None,
        "stake_alpha": tao(mg["alpha_stake"][uid]),
        "stake_tao_root": tao(mg["tao_stake"][uid]),
        "last_weights_block": last or None,
        "blocks_since_weights": (block - last) if last else None,
        "registered_at_block": reg,
        "immune": block - reg < imm,
        "immunity_blocks_left": max(0, imm - (block - reg)),
        "owner_immortal": uid in immortal,
        "neurons": int(mg["num_uids"]), "max_neurons": int(mg["max_uids"]),
        "subnet_full": full,
    }
    # Deregistration reading.
    if uid in immortal:
        risk, text = "none", "the subnet owner's immortal uid: never pruned"
    elif not full:
        risk, text = "none_now", "the subnet is not full: a new registration takes an empty slot, nobody is pruned"
    elif e["immune"]:
        risk = "immune"
        text = f"immune for {e['immunity_blocks_left']} more blocks (about {e['immunity_blocks_left'] * 12 / 3600:.1f} h)"
    else:
        pos = non_immune.index(uid) + 1
        e["prune_position"] = pos
        e["non_immune_count"] = len(non_immune)
        risk = "next" if nxt == uid else ("high" if pos <= 3 else ("medium" if pos <= 10 else "low"))
        lowest = int(mg["emission"][non_immune[0]])
        e["emission_equals_lowest"] = int(mg["emission"][uid]) == lowest
        text = (f"{'NEXT to be pruned' if nxt == uid else f'position {pos} of {len(non_immune)}'} from the bottom by emission among non-immune neurons "
                f"(each new registration replaces the lowest)")
        if e["emission_equals_lowest"] and nxt != uid:
            ties = sum(1 for u in non_immune if int(mg["emission"][u]) == lowest)
            text += f"; its emission is the lowest ({_num(tao(lowest))} α), shared by {ties} neurons, and among those the oldest registration goes first"
    e["deregistration"] = {"risk": risk, "text": text, "next_to_prune_uid": nxt,
                           "next_to_prune_emission_alpha_per_epoch": tao(mg["emission"][nxt]) if nxt is not None else None,
                           "rule": "when the subnet is full each registration replaces the non-immune neuron with the lowest emission (ties: oldest registration); next = it goes at the next registration, high = within the next 3, medium = within the next 10, low = further"}
    return e


def hotkey_findings(e: dict, tempo: int) -> dict:
    """What is worth a warning about one registration: key -> {severity, text}.
    Only a deregistration reading of next (critical) or high (warning), and a
    validator that has not set weights for over 3 tempos (warning); the readings
    medium and low are normal and are never findings."""
    n, tag = e["netuid"], f"SN{e['netuid']} uid {e['uid']}"
    found = {}
    risk = e["deregistration"]["risk"]
    if risk in ("next", "high"):
        found[f"deregistration:SN{n}"] = {"severity": "critical" if risk == "next" else "warning",
                                          "text": f"{tag}: deregistration risk {risk} ({e['deregistration']['text']})"}
    since = e["blocks_since_weights"]
    if e["validator_permit"] and since is not None and since > 3 * (tempo + 1):
        found[f"weights_stale:SN{n}"] = {"severity": "warning",
                                         "text": f"{tag}: has a validator permit but last set weights {since} blocks (about {since * 12 / 3600:.1f} h) ago",
                                         "data": {"blocks_since_weights": since}}
    return found


def hotkey_watch(entries: list, subnets: dict, prev: dict | None, t_now: float) -> tuple[dict, dict]:
    """(findings, memo for the next check). Losing a registration or a validator
    permit is remembered until it is regained, so it stays `ongoing` and then
    `resolved` instead of being told once and forgotten."""
    memo_prev = (prev or {}).get("memo") or {}
    was = {int(n): v for n, v in (memo_prev.get("registered") or {}).items()}
    now_reg = {e["netuid"]: {"permit": e["validator_permit"], "uid": e["uid"]} for e in entries}
    found = {}
    for e in entries:
        found.update(hotkey_findings(e, subnets[e["netuid"]]["tempo"]))
    lost_reg = (set(memo_prev.get("lost_registration") or []) | (set(was) - set(now_reg))) - set(now_reg)
    lost_permit = set(memo_prev.get("lost_permit") or [])
    lost_permit |= {n for n, v in was.items() if v.get("permit") and n in now_reg and not now_reg[n]["permit"]}
    lost_permit = {n for n in lost_permit if n in now_reg and not now_reg[n]["permit"]}
    for n in sorted(lost_reg):
        found[f"registration_lost:SN{n}"] = {"severity": "critical", "text": f"SN{n}: the hotkey is no longer registered on this subnet (it was at an earlier check)"}
    for n in sorted(lost_permit):
        found[f"permit_lost:SN{n}"] = {"severity": "warning", "text": f"SN{n} uid {now_reg[n]['uid']}: lost its validator permit (it had one at an earlier check)"}
    memo = {"registered": {str(n): v for n, v in now_reg.items()}, "lost_registration": sorted(lost_reg), "lost_permit": sorted(lost_permit)}
    return found, memo


def cmd_hotkey(args):
    hk = check_ss58(args.hotkey, "the hotkey")
    reads, r = run_read(lambda rd: _hotkey_reads(rd, hk, args.netuid))
    t_now = now()
    entries = [hotkey_entry(n, u, g, lite or {}, reads["subnets"][n], r.block, hk)
               for (n, u), g, lite in zip(reads["on"], reads["graphs"], reads["lites"])]
    flags = []
    for e in entries:
        flags += [f["text"] for f in hotkey_findings(e, reads["subnets"][e["netuid"]]["tempo"]).values()]
    out = {"ok": True, "command": "hotkey", "hotkey": hk, "owner_coldkey": reads["owner"], **head_dict(r.block, t_now),
           "registered_on": [e["netuid"] for e in entries], "subnets": entries, "flags": flags}
    if not args.no_save:
        subnet_history_update(reads["subnets"], r.block, t_now, True)

    def text(o):
        if not o["subnets"]:
            return f"Hotkey {short(o['hotkey'])} is not registered on {'SN' + str(args.netuid) if args.netuid is not None else 'any subnet'} (block {o['block']})."
        lines = [f"Hotkey {short(o['hotkey'])} (block {o['block']}): registered on {len(o['subnets'])} subnet(s)"]
        for e in o["subnets"]:
            role = "validator" if e["validator_permit"] else "miner"
            w = f", weights set {e['blocks_since_weights']} blocks ago" if e["validator_permit"] else ""
            lines.append(f"- SN{e['netuid']} {e['subnet'] or ''} uid {e['uid']} ({role}): incentive {fmt(e['incentive'])}, dividends {fmt(e['dividends'])}, "
                         f"{fmt(e['emission_alpha_per_day'])} α/day{w}; "
                         f"deregistration: {e['deregistration']['risk']} ({e['deregistration']['text']})")
        lines += [f"! {f}" for f in o["flags"]]
        return "\n".join(lines)
    if not args.watch:
        return out, text

    path = watch_path("hotkey", f"{hk}-{'sn' + str(args.netuid) if args.netuid is not None else 'all'}")
    prev = load_watch(path)
    found, memo = hotkey_watch(entries, reads["subnets"], prev, t_now)
    diff = watch_diff(prev["alerts"] if prev else None, found, t_now)
    w = watch_output("hotkey", {"hotkey": hk, "registered_on": out["registered_on"]}, r.block, t_now, prev, diff, [])
    if not args.no_save:
        save_watch(path, diff["stored"], memo, r.block, t_now)
    return w, lambda o: watch_text(o, f"Hotkey watch {short(hk)}", text(out))


# ---- week --------------------------------------------------------------------------
def week_report(subnets: dict, block: int, t_now: float, base: dict | None, base_source: str | None, top: int) -> dict:
    out = {"ok": True, "command": "week", **head_dict(block, t_now), "subnets_now": len(subnets)}
    total_now = sum(s["tao_emission"] or 0 for s in subnets.values())
    new = [s for s in subnets.values() if block - s["registered_at"] <= 7 * BLOCKS_PER_DAY and s["netuid"] != 0]
    out["new_subnets"] = [{"netuid": s["netuid"], "name": s["name"], "registered_at_block": s["registered_at"],
                           "days_ago": round((block - s["registered_at"]) / BLOCKS_PER_DAY, 1),
                           "github": (s["identity"] or {}).get("github")} for s in sorted(new, key=lambda s: -s["registered_at"])]
    if base is None:
        out["compared_to"] = None
        out["movers"] = None
        out["emission_changes"] = None
        out["note"] = "no earlier snapshot yet: price and emission changes need one (taoq keeps them in its data directory)"
    else:
        out["compared_to"] = {"at": iso(base["at"]) if base.get("at") else None, "block": base["block"],
                              "days": round((block - base["block"]) / BLOCKS_PER_DAY, 1), "source": base_source}
        rows = []
        total_then = sum((b["tao_emission"] or 0) for b in base["subnets"].values())
        for n, s in subnets.items():
            b = base["subnets"].get(n)
            if n == 0 or not b or b["registered_at"] != s["registered_at"]:
                continue
            share_now = Decimal(s["tao_emission"]) * 100 / total_now if total_now and s["tao_emission"] is not None else None
            share_then = Decimal(b["tao_emission"]) * 100 / total_then if total_then and b["tao_emission"] is not None else None
            rows.append({"netuid": n, "name": s["name"], "price": q9(price_of(s)), "price_pct": pct(s["price_rao"], b["price_rao"]),
                         "pool_tao": q9(tao(s["tao_in"])),
                         "emission_share_pct": share_now.quantize(Decimal("0.01")) if share_now is not None else None,
                         "emission_share_change_pts": (share_now - share_then).quantize(Decimal("0.01")) if share_now is not None and share_then is not None else None})
        priced = [x for x in rows if x["price_pct"] is not None]
        out["movers"] = {"up": sorted(priced, key=lambda x: -x["price_pct"])[:top],
                         "down": sorted(priced, key=lambda x: x["price_pct"])[:top]}
        em = [x for x in rows if x["emission_share_change_pts"] is not None and x["emission_share_change_pts"] != 0]
        out["emission_changes"] = sorted(em, key=lambda x: -abs(x["emission_share_change_pts"]))[:top]
        gone = sorted(set(base["subnets"]) - set(subnets))
        replaced = [n for n, s in subnets.items() if n in base["subnets"] and base["subnets"][n]["registered_at"] != s["registered_at"]]
        if gone or replaced:
            out["deregistered_or_replaced"] = sorted(set(gone) | set(replaced))
    top_em = sorted(subnets.values(), key=lambda s: -(s["tao_emission"] or 0))[:top]
    out["top_emission"] = [{"netuid": s["netuid"], "name": s["name"],
                            "share_pct": (Decimal(s["tao_emission"] or 0) * 100 / total_now).quantize(Decimal("0.01")) if total_now else None}
                           for s in top_em]
    return out


def _base_from_hist(snap) -> dict:
    return {"at": snap["at"], "block": snap["block"],
            "subnets": {int(n): _hist_row(snap, n) for n in snap["subnets"]}}


def cmd_week(args):
    subnets, r = run_read(read_subnets)
    t_now = now()
    snaps = subnet_history_update(subnets, r.block, t_now, not args.no_save)
    prior = [s for s in snaps if s["at"] < t_now]
    week_old = [s for s in prior if t_now - s["at"] >= 6 * 86400]
    base, src = None, None
    unread = []
    if week_old:
        base, src = _base_from_hist(min(week_old, key=lambda s: abs(t_now - s["at"] - 7 * 86400))), "snapshot"
    elif not args.no_archive:
        past_block = r.block - 7 * BLOCKS_PER_DAY
        try:
            past, _ = run_read(read_subnets, eps=endpoints("TAOQ_ARCHIVE_ENDPOINTS", DEFAULT_ARCHIVE), at_block=past_block)
            base = {"at": None, "block": past_block, "subnets": past}
            src = "archive node"
        except (Unreadable, Usage) as e:
            unread.append(f"state 7 days ago from an archive node ({str(e)[:160]})")
    if base is None and prior:
        base, src = _base_from_hist(min(prior, key=lambda s: s["at"])), "oldest snapshot (less than 6 days old)"
    out = week_report(subnets, r.block, t_now, base, src, args.top)
    if unread:
        out["unread"] = unread

    def text(o):
        lines = [f"Bittensor week (block {o['block']}): {o['subnets_now']} subnets"]
        c = o.get("compared_to")
        if c:
            lines.append(f"Compared with block {c['block']} ({c['days']} days earlier, {c['source']}).")
            lines.append("Up: " + ", ".join(f"SN{x['netuid']} {x['name'] or ''} {signed(x['price_pct'], 1)}%" for x in o["movers"]["up"]))
            lines.append("Down: " + ", ".join(f"SN{x['netuid']} {x['name'] or ''} {signed(x['price_pct'], 1)}%" for x in o["movers"]["down"]))
            if o["emission_changes"]:
                lines.append("Emission share: " + ", ".join(f"SN{x['netuid']} {signed(x['emission_share_change_pts'], 2)} pts" for x in o["emission_changes"]))
        else:
            lines.append(o.get("note", ""))
        lines.append("New: " + (", ".join(f"SN{x['netuid']} {x['name'] or ''} ({x['days_ago']} d)" for x in o["new_subnets"]) or "none"))
        return "\n".join(lines)
    return out, text


# ---- subnet-brief --------------------------------------------------------------------
async def _brief_reads(r: Reader, n: int):
    base = await _subnet_reads(r, [n])
    g = await _metagraph_extras(r, n)
    return {**base, "g": g}


def brief_report(n: int, reads: dict, block: int, snaps: list, t_now: float, top: int) -> dict:
    facts = subnet_facts(n, reads["subnets"], reads["extra"], block, snaps, t_now, 20)
    mg = reads["g"]["mg"]
    size = len(mg["hotkeys"])
    ids = mg.get("identities") or [None] * size
    vals = [u for u in range(size) if mg["validator_permit"][u]]
    cutoff = int(mg["activity_cutoff"])
    tempo = int(mg["tempo"])
    per_day = Decimal(BLOCKS_PER_DAY) / (tempo + 1)
    vrows = sorted(vals, key=lambda u: -int(mg["total_stake"][u]))[:top]
    validators = [{"uid": u, "hotkey": mg["hotkeys"][u], "name": text_of((ids[u] or {}).get("name")) if ids[u] else None,
                   "stake_alpha": tao(mg["alpha_stake"][u]), "stake_weight": tao(mg["total_stake"][u]),
                   "dividends": frac(mg["dividends"][u]), "blocks_since_weights": block - int(mg["last_update"][u])} for u in vrows]
    miners = [u for u in range(size) if int(mg["incentives"][u]) > 0]
    inc_sorted = sorted(miners, key=lambda u: -int(mg["incentives"][u]))
    total_inc = sum(int(mg["incentives"][u]) for u in miners) or 1
    top_miners = [{"uid": u, "hotkey": mg["hotkeys"][u], "coldkey": mg["coldkeys"][u], "incentive": frac(mg["incentives"][u]),
                   "emission_alpha_per_day": q9(tao(mg["emission"][u]) * per_day)} for u in inc_sorted[:top]]
    non_immune, immune, immortal, nxt = prune_order(mg, block, int(reads["g"]["owner_limit"] or 0), int(reads["g"]["min_non_immune"] or 0))
    out = {"ok": True, "command": "subnet-brief", **head_dict(block, t_now), "subnet": facts,
           "metagraph": {
               "neurons": size, "validators_with_permit": len(vals),
               "validators_setting_weights": sum(1 for u in vals if block - int(mg["last_update"][u]) <= cutoff),
               "activity_cutoff_blocks": cutoff,
               "miners_with_incentive": len(miners),
               "distinct_miner_coldkeys": len({mg["coldkeys"][u] for u in miners}),
               "top1_incentive_share": (Decimal(int(mg["incentives"][inc_sorted[0]])) / total_inc).quantize(Decimal("0.0001")) if miners else None,
               "top5_incentive_share": (Decimal(sum(int(mg["incentives"][u]) for u in inc_sorted[:5])) / total_inc).quantize(Decimal("0.0001")) if miners else None,
               "immune_neurons": len(immune), "next_to_prune_uid": nxt,
               "commit_reveal": mg.get("commit_reveal_weights_enabled"),
               "max_validators": mg.get("max_validators"),
           },
           "top_validators": validators, "top_miners": top_miners}
    return out


def cmd_brief(args):
    n = args.netuid
    reads, r = run_read(lambda rd: _brief_reads(rd, n))
    t_now = now()
    snaps = subnet_history_update(reads["subnets"], r.block, t_now, not args.no_save)
    out = brief_report(n, reads, r.block, [s for s in snaps if s["at"] < t_now], t_now, args.top)

    def text(o):
        m = o["metagraph"]
        lines = [subnet_text_one(o["subnet"]),
                 f"  {m['neurons']} neurons: {m['validators_with_permit']} validators with permit ({m['validators_setting_weights']} setting weights), "
                 f"{m['miners_with_incentive']} miners earning from {m['distinct_miner_coldkeys']} coldkeys; top miner holds {m['top1_incentive_share']} of incentive, top 5 {m['top5_incentive_share']}"]
        lines += [f"  validator uid {v['uid']} {v['name'] or short(v['hotkey'])}: dividends {v['dividends']}" for v in o["top_validators"]]
        return "\n".join(lines)
    return out, text


# ---- main --------------------------------------------------------------------------
WATCH_HELP = ("for a scheduled watch: report only what is new, worse or resolved since the last check "
              '("alert": false = nothing to tell; with --text the output is then exactly NO_REPLY); the first check records the state')


def build_parser():
    p = argparse.ArgumentParser(prog="taoq", description="Read-only Bittensor chain reports (JSON). Units: see --help-units.",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("Units (every output):")[1].split("Exit status")[0])
    p.add_argument("--version", action="version", version=f"taoq {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--text", action="store_true", help="a short report for a person instead of JSON")
        sp.add_argument("--no-save", action="store_true", help="do not write snapshots")
        return sp

    s = common(sub.add_parser("stake", help="stake positions of a coldkey"))
    s.add_argument("coldkey")
    s.add_argument("--since-hours", type=float, default=20, help="compare with the newest snapshot at least this old (default 20)")
    s.add_argument("--price-alert", type=float, default=10, help="flag price moves of at least this many percent (default 10)")
    s.add_argument("--stale-blocks", type=int, default=None, help="flag validators that have not set weights for this many blocks (default 3 tempos)")
    s.set_defaults(fn=cmd_stake)
    s = common(sub.add_parser("subnet", help="facts about one or more subnets"))
    s.add_argument("netuid", type=int, nargs="+")
    s.add_argument("--since-hours", type=float, default=20)
    s.add_argument("--watch", action="store_true", help=WATCH_HELP)
    s.add_argument("--price-alert", type=float, default=10, help="with --watch: a price move of at least this many percent since the last check is an event (default 10)")
    s.set_defaults(fn=cmd_subnet)
    s = common(sub.add_parser("hotkey", help="a hotkey's standing on each subnet"))
    s.add_argument("hotkey")
    s.add_argument("--netuid", type=int, default=None)
    s.add_argument("--watch", action="store_true", help=WATCH_HELP)
    s.set_defaults(fn=cmd_hotkey)
    s = common(sub.add_parser("week", help="price movers, new subnets, emission changes over about 7 days"))
    s.add_argument("--top", type=int, default=5)
    s.add_argument("--no-archive", action="store_true", help="never ask an archive node for the state 7 days ago")
    s.set_defaults(fn=cmd_week)
    s = common(sub.add_parser("subnet-brief", help="on-chain facts for a deep dive on one subnet"))
    s.add_argument("netuid", type=int)
    s.add_argument("--top", type=int, default=5)
    s.set_defaults(fn=cmd_brief)
    return p


def main(argv=None) -> int:
    p = build_parser()
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code not in (0, None) else 0
    try:
        out, text = args.fn(args)
    except Usage as e:
        print(dumps({"ok": False, "error": str(e)}))
        print(f"taoq: {e}", file=sys.stderr)
        return 2
    except Unreadable as e:
        print(dumps({"ok": False, "error": str(e)}))
        print(f"taoq: {e}", file=sys.stderr)
        return 1
    print(text(out) if args.text else dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
