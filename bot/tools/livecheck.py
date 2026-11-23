"""Live, read-only check of taoq against finney, each figure cross-checked by a
second, independent read at the SAME block. Not part of the image; run it by
hand or from scripts/taoq-check.sh (network needed, no key, no wallet):

    python3 bot/tools/livecheck.py [--taoq "venv/bin/python -I bot/tools/taoq.py"]

Addresses: public SN70 (netuid 70) metagraph entries, picked at run time:
  - stake: the coldkey of an SN70 neuron that holds stake on several subnets
    (fallback: a fixed SN70 coldkey);
  - hotkey: an SN70 validator hotkey and an SN70 miner hotkey.
How each figure is checked a second way:
  stake      StakeInfoRuntimeApi (taoq) against the share-pool storage
             (AlphaV2 x TotalHotkeyAlpha / TotalHotkeySharesV2), and the set of
             positions against StakingHotkeys x AlphaV2; free balance against a
             raw System.Account storage read decoded here by hand (not by the
             library); price against the pool reserves in storage
             (SubnetTAO / SubnetAlphaIn); value = amount x price recomputed.
  subnet     reserves, owner, neuron count, burn, immunity from plain storage
             items against the runtime API answers taoq uses; TAO emission
             shares add up to the block emission.
  hotkey     uid, emission, validator permit, last update, registration block
             from plain storage (Uids, Emission, ValidatorPermit, LastUpdate,
             BlockAtRegistration) against the metagraph runtime call; the next
             uid to prune recomputed from storage alone.
  week       a mover's price change recomputed from the swap's price at both
             blocks (current_alpha_price at the archive block).
  subnet-brief  validator count from ValidatorPermit storage; incentive shares
             from NeuronInfoRuntimeApi.get_neurons_lite.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shlex
import subprocess
import sys
from decimal import Decimal, getcontext

getcontext().prec = 80
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import taoq  # noqa: E402

LITE = os.environ.get("LIVECHECK_ENDPOINT", "wss://lite.chain.opentensor.ai:443")
ARCHIVE = os.environ.get("LIVECHECK_ARCHIVE", taoq.DEFAULT_ARCHIVE)
FALLBACK_COLDKEY = "5E4Gnhbb9cTEYPFSvaNrhSUxjaXyPUrRDm7uJUqiSLgFFbhf"
RAO = 10**9
PASS = FAIL = 0


def ok(cond, text):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS  " + text)
    else:
        FAIL += 1
        print("FAIL  " + text)


def D(x):
    return Decimal(str(x))


def run_taoq(cmd, *args):
    p = subprocess.run(shlex.split(cmd) + list(args) + ["--no-save"], capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        raise SystemExit(f"taoq {' '.join(args)} failed: {p.stderr.strip()}")
    return json.loads(p.stdout)


def v(x):
    return getattr(x, "value", x)


def ss58_pub(addr):
    n = 0
    for ch in addr:
        n = n * 58 + taoq._B58.index(ch)
    return n.to_bytes(35, "big")[1:33]


async def raw_free_balance(s, bh, addr):
    """System.Account free balance from the raw storage bytes, decoded here."""
    import xxhash

    def tw128(b):
        return xxhash.xxh64(b, seed=0).digest()[::-1] + xxhash.xxh64(b, seed=1).digest()[::-1]

    pk = ss58_pub(addr)
    key = "0x" + (tw128(b"System") + tw128(b"Account") + hashlib.blake2b(pk, digest_size=16).digest() + pk).hex()
    res = await s.rpc_request("state_getStorage", [key, bh])
    raw = bytes.fromhex(res["result"][2:])
    # AccountInfo: nonce u32, consumers u32, providers u32, sufficients u32, then data.free u128 (little-endian)
    return int.from_bytes(raw[16:32], "little")


def safe_float(sf):
    """subtensor SafeFloat {mantissa, exponent} -> Decimal."""
    return Decimal(int(sf["mantissa"])) * (Decimal(10) ** int(sf["exponent"]))


async def pick_addresses(s, bh):
    mg = v(await s.runtime_call("SubnetInfoRuntimeApi", "get_metagraph", [70], block_hash=bh))
    cks = list(dict.fromkeys(mg["coldkeys"]))[:60]
    res = v(await s.runtime_call("StakeInfoRuntimeApi", "get_stake_info_for_coldkeys", [cks], block_hash=bh))
    best = None
    for ck, infos in res:
        nets = {i["netuid"] for i in infos if i["stake"] > RAO}
        if 2 <= len(nets) <= 6 and (best is None or len(nets) > best[1]):
            best = (ck, len(nets))
    coldkey = best[0] if best else FALLBACK_COLDKEY
    vals = [u for u in range(len(mg["hotkeys"])) if mg["validator_permit"][u]]
    miners = [u for u in range(len(mg["hotkeys"])) if not mg["validator_permit"][u] and mg["incentives"][u] > 0] or \
             [u for u in range(len(mg["hotkeys"])) if not mg["validator_permit"][u]]
    return coldkey, mg["hotkeys"][vals[-1]], mg["hotkeys"][miners[0]]


async def check_stake(s, out):
    bh = await s.get_block_hash(out["block"])
    ck = out["coldkey"]
    print(f"-- stake {ck} at block {out['block']}: {len(out['positions'])} positions")
    for p in out["positions"]:
        n, hk = p["netuid"], p["hotkey"]
        amount = D(p["alpha"] if n else p["tao"])
        share = v(await s.query("SubtensorModule", "AlphaV2", [hk, ck, n], block_hash=bh))
        total_shares = v(await s.query("SubtensorModule", "TotalHotkeySharesV2", [hk, n], block_hash=bh))
        total_alpha = v(await s.query("SubtensorModule", "TotalHotkeyAlpha", [hk, n], block_hash=bh))
        if share and total_shares and safe_float(total_shares) != 0:
            second = int(safe_float(share) * Decimal(total_alpha) / safe_float(total_shares))
            ok(abs(second - int(amount * RAO)) <= 1,
               f"SN{n} {hk[:8]}: stake {amount} from StakeInfoRuntimeApi = {Decimal(second) / RAO} from AlphaV2 x TotalHotkeyAlpha / TotalHotkeySharesV2 (within 1 rao)")
        else:
            ok(False, f"SN{n} {hk[:8]}: no share-pool entry in storage for a position taoq reports")
        if n:
            tin = v(await s.query("SubtensorModule", "SubnetTAO", [n], block_hash=bh))
            ain = v(await s.query("SubtensorModule", "SubnetAlphaIn", [n], block_hash=bh))
            ratio = Decimal(tin) / Decimal(ain)
            ok(abs(ratio - D(p["price"])) / ratio < Decimal("0.001"),
               f"SN{n}: price {p['price']} (SwapRuntimeApi) vs pool ratio SubnetTAO/SubnetAlphaIn {ratio:.9f} (within 0.1%)")
            ok(abs(D(p["value_tao"]) - amount * D(p["price"])) <= Decimal("0.000000001"),
               f"SN{n}: value {p['value_tao']} TAO = {amount} x {p['price']}")
    # The set of positions: every staking hotkey x every subnet with shares.
    hks = v(await s.query("SubtensorModule", "StakingHotkeys", [ck], block_hash=bh)) or []
    nets = sorted({p["netuid"] for p in out["positions"]} | {0})
    allnets = [int(d["netuid"]) for d in v(await s.runtime_call("SubnetInfoRuntimeApi", "get_all_dynamic_info", [], block_hash=bh)) if d]
    keys = [await s.create_storage_key("SubtensorModule", "AlphaV2", [h, ck, n], block_hash=bh) for h in hks for n in allnets]
    found = set()
    for i in range(0, len(keys), 500):
        for k, val in await s.query_multi(keys[i:i + 500], block_hash=bh):
            val = v(val)
            if val and int(val.get("mantissa", 0)) > 0:
                found.add((k.params[2], k.params[0]))
    mine = {(p["netuid"], p["hotkey"]) for p in out["positions"]}
    # A share entry can hold a dust amount that rounds to 0 rao: taoq leaves those out.
    ok(mine <= found and len(found - mine) <= 2,
       f"positions: taoq {len(mine)}, share-pool storage over StakingHotkeys ({len(hks)} hotkeys x {len(allnets)} subnets) {len(found)}")
    free = await raw_free_balance(s, bh, ck)
    ok(Decimal(free) / RAO == D(out["totals"]["free_tao"]),
       f"free balance {out['totals']['free_tao']} TAO = raw System.Account bytes decoded here {Decimal(free) / RAO}")
    total = sum(D(p["value_tao"]) for p in out["positions"])
    ok(total == D(out["totals"]["stake_value_tao"]), f"stake value total {out['totals']['stake_value_tao']} = sum of positions {total}")
    _ = nets


async def check_subnet(s, out):
    bh = await s.get_block_hash(out["block"])
    for sn in out["subnets"]:
        n = sn["netuid"]
        print(f"-- subnet {n} at block {out['block']}")
        tin = v(await s.query("SubtensorModule", "SubnetTAO", [n], block_hash=bh))
        ain = v(await s.query("SubtensorModule", "SubnetAlphaIn", [n], block_hash=bh))
        ok(D(sn["pool"]["tao"]) == Decimal(tin) / RAO and D(sn["pool"]["alpha"]) == Decimal(ain) / RAO,
           f"SN{n}: pool {sn['pool']['tao']} TAO / {sn['pool']['alpha']} alpha = storage SubnetTAO / SubnetAlphaIn")
        owner = v(await s.query("SubtensorModule", "SubnetOwner", [n], block_hash=bh))
        ohk = v(await s.query("SubtensorModule", "SubnetOwnerHotkey", [n], block_hash=bh))
        ok(owner == sn["owner_coldkey"] and ohk == sn["owner_hotkey"], f"SN{n}: owner {owner[:8]}/{ohk[:8]} = storage SubnetOwner / SubnetOwnerHotkey")
        mg = v(await s.runtime_call("SubnetInfoRuntimeApi", "get_metagraph", [n], block_hash=bh))
        ok(sn["neurons"] == len(mg["hotkeys"]) == mg["num_uids"], f"SN{n}: {sn['neurons']} neurons = metagraph's {len(mg['hotkeys'])}")
        ok(D(sn["registration_cost_tao"]) == Decimal(mg["burn"]) / RAO, f"SN{n}: registration {sn['registration_cost_tao']} TAO = metagraph burn {Decimal(mg['burn']) / RAO}")
        ok(sn["immunity_period_blocks"] == mg["immunity_period"], f"SN{n}: immunity period {sn['immunity_period_blocks']} = metagraph's")
        ok(abs(D(sn["price"]) - Decimal(tin) / Decimal(ain)) / D(sn["price"]) < Decimal("0.001"), f"SN{n}: price {sn['price']} vs reserve ratio (within 0.1%)")
    be = v(await s.runtime_call("SubnetInfoRuntimeApi", "get_block_emission", [], block_hash=bh))
    nets = [int(d["netuid"]) for d in v(await s.runtime_call("SubnetInfoRuntimeApi", "get_all_dynamic_info", [], block_hash=bh)) if d]
    keys = [await s.create_storage_key("SubtensorModule", st, [n], block_hash=bh) for st in ("SubnetTaoInEmission", "SubnetExcessTao") for n in nets]
    tot = sum(v(x) or 0 for _, x in await s.query_multi(keys, block_hash=bh))
    ok(abs(tot - be) <= len(nets) * 2, f"TAO emission over all subnets {tot} rao/block = block emission {be} (within rounding: {abs(tot - be)} rao)")


async def check_hotkey(s, out):
    bh = await s.get_block_hash(out["block"])
    hk = out["hotkey"]
    for e in out["subnets"]:
        n, uid = e["netuid"], e["uid"]
        print(f"-- hotkey {hk[:8]} on SN{n} uid {uid} at block {out['block']}")
        ok(v(await s.query("SubtensorModule", "Uids", [n, hk], block_hash=bh)) == uid, f"SN{n}: uid {uid} = storage Uids")
        em = v(await s.query("SubtensorModule", "Emission", [n], block_hash=bh))
        ok(Decimal(em[uid]) / RAO == D(e["emission_alpha_per_epoch"]), f"SN{n}: emission {e['emission_alpha_per_epoch']} alpha/epoch = storage Emission[{uid}]")
        vp = v(await s.query("SubtensorModule", "ValidatorPermit", [n], block_hash=bh))
        ok(bool(vp[uid]) == e["validator_permit"], f"SN{n}: validator permit {e['validator_permit']} = storage ValidatorPermit")
        lu = v(await s.query("SubtensorModule", "LastUpdate", [n], block_hash=bh))
        ok(lu[uid] == e["last_weights_block"], f"SN{n}: last weights block {e['last_weights_block']} = storage LastUpdate[{uid}]")
        reg = v(await s.query("SubtensorModule", "BlockAtRegistration", [n, uid], block_hash=bh))
        ok(reg == e["registered_at_block"], f"SN{n}: registered at {reg} = storage BlockAtRegistration")
        # The next uid to prune, from storage only.
        size = v(await s.query("SubtensorModule", "SubnetworkN", [n], block_hash=bh))
        imm = v(await s.query("SubtensorModule", "ImmunityPeriod", [n], block_hash=bh))
        regs = [v(x) for _, x in await s.query_multi(
            [await s.create_storage_key("SubtensorModule", "BlockAtRegistration", [n, u], block_hash=bh) for u in range(size)], block_hash=bh)]
        keys = [v(x) for _, x in await s.query_multi(
            [await s.create_storage_key("SubtensorModule", "Keys", [n, u], block_hash=bh) for u in range(size)], block_hash=bh)]
        owners = [v(x) for _, x in await s.query_multi(
            [await s.create_storage_key("SubtensorModule", "Owner", [k], block_hash=bh) for k in keys], block_hash=bh)]
        owner_ck = v(await s.query("SubtensorModule", "SubnetOwner", [n], block_hash=bh))
        owner_hk = v(await s.query("SubtensorModule", "SubnetOwnerHotkey", [n], block_hash=bh))
        limit = v(await s.query("SubtensorModule", "ImmuneOwnerUidsLimit", [n], block_hash=bh))
        minfree = v(await s.query("SubtensorModule", "MinNonImmuneUids", [n], block_hash=bh))
        trip = sorted((regs[u], u) for u in range(size) if owners[u] == owner_ck)
        immortal = [u for _, u in trip][:limit]
        if owner_hk in keys and keys.index(owner_hk) not in immortal:
            immortal = ([keys.index(owner_hk)] + immortal)[:limit]
        free = sorted((em[u], regs[u], u) for u in range(size) if u not in immortal and out["block"] - regs[u] >= imm)
        immune = sorted((em[u], regs[u], u) for u in range(size) if u not in immortal and out["block"] - regs[u] < imm)
        nxt = free[0][2] if free and len(free) > minfree else (immune[0][2] if immune else (free[0][2] if free else None))
        ok(nxt == e["deregistration"]["next_to_prune_uid"],
           f"SN{n}: next uid to prune {e['deregistration']['next_to_prune_uid']} = recomputed from storage (Emission, BlockAtRegistration, Keys, Owner, SubnetOwner*) {nxt}")


async def check_week(s, out, archive):
    c = out["compared_to"]
    if not c:
        ok(False, "week: nothing to compare with")
        return
    print(f"-- week: block {out['block']} against {c['block']} ({c['source']})")
    bh = await s.get_block_hash(out["block"])
    async with archive as a:
        pbh = await a.get_block_hash(c["block"])
        for m in out["movers"]["up"][:2] + out["movers"]["down"][:2]:
            n = m["netuid"]
            now_p = v(await s.runtime_call("SwapRuntimeApi", "current_alpha_price", [n], block_hash=bh))
            then_tao = v(await a.query("SubtensorModule", "SubnetTAO", [n], block_hash=pbh))
            then_alpha = v(await a.query("SubtensorModule", "SubnetAlphaIn", [n], block_hash=pbh))
            then_ratio = Decimal(then_tao) / Decimal(then_alpha) * RAO
            second = (Decimal(now_p) - then_ratio) * 100 / then_ratio
            ok(abs(second - D(m["price_pct"])) < Decimal("0.5"),
               f"SN{n}: 7-day price change {m['price_pct']}% vs {second:.2f}% from SubnetTAO/SubnetAlphaIn storage at the old block (within 0.5 pts)")


async def check_brief(s, out):
    bh = await s.get_block_hash(out["block"])
    n = out["subnet"]["netuid"]
    print(f"-- subnet-brief SN{n} at block {out['block']}")
    vp = v(await s.query("SubtensorModule", "ValidatorPermit", [n], block_hash=bh))
    ok(sum(1 for x in vp if x) == out["metagraph"]["validators_with_permit"], f"SN{n}: {out['metagraph']['validators_with_permit']} validators = storage ValidatorPermit count")
    lite = v(await s.runtime_call("NeuronInfoRuntimeApi", "get_neurons_lite", [n], block_hash=bh))
    inc = sorted((x["incentive"] for x in lite if x["incentive"] > 0), reverse=True)
    ok(len(inc) == out["metagraph"]["miners_with_incentive"], f"SN{n}: {len(inc)} neurons with incentive in get_neurons_lite = {out['metagraph']['miners_with_incentive']}")
    if inc:
        share = (Decimal(inc[0]) / sum(inc)).quantize(Decimal("0.0001"))
        ok(share == D(out["metagraph"]["top1_incentive_share"]), f"SN{n}: top miner's incentive share {share} = {out['metagraph']['top1_incentive_share']}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--taoq", default=f"{sys.executable} -I {os.path.join(HERE, 'taoq.py')}")
    args = ap.parse_args()
    from async_substrate_interface import AsyncSubstrateInterface

    async with AsyncSubstrateInterface(url=LITE, ss58_format=42) as s:
        bh = await s.get_chain_finalised_head()
        coldkey, vhk, mhk = await pick_addresses(s, bh)
        print(f"addresses from the SN70 metagraph: coldkey {coldkey}, validator hotkey {vhk}, miner hotkey {mhk}")
        await check_stake(s, run_taoq(args.taoq, "stake", coldkey))
        await check_subnet(s, run_taoq(args.taoq, "subnet", "70", "4"))
        await check_hotkey(s, run_taoq(args.taoq, "hotkey", vhk, "--netuid", "70"))
        await check_hotkey(s, run_taoq(args.taoq, "hotkey", mhk, "--netuid", "70"))
        await check_brief(s, run_taoq(args.taoq, "subnet-brief", "70"))
        week = run_taoq(args.taoq, "week")
        await check_week(s, week, AsyncSubstrateInterface(url=ARCHIVE, ss58_format=42))
    print(f"\n== livecheck: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
