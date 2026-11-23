"""Unit tests for taoq (standard library only; no network).

    python3 -m unittest discover -s bot/tools -p 'test_*.py'

The recorded fixture (fixtures/finney.json.gz) is a real read of finney at
block 9568291 (contact fields blanked): taoq replays it through the same
code path as a live read. Expected figures below are computed here from the
fixture's raw values, not copied from taoq's output.
"""

import contextlib
import gzip
import io
import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import taoq  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "finney.json.gz")
OWNER_CK = "5GbydRVj98hQZvR2cf4t8WtcoUVjAuJykxxuqauiJpJqwB7m"   # SN70's owner coldkey (public)
OWNER_HK = "5E4x811vM4DLkrTwMhrMX1deUp9zPt3GJLinxZneBvAMTL5U"
VALI_HK = "5E2LP6EnZ54m3wS8s1yPvD5c3xo71kQroBw7aUVK32TKeZ5u"    # an SN70 validator (public)
ALICE = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
T0 = 1795400000.0


def raw():
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as f:
        return json.load(f)


def read(fx, kind, a, b, params):
    return fx["reads"][taoq._key(kind, a, b, params)]


class Run:
    """taoq main() against the fixture with a private data directory."""

    def __init__(self):
        self.dir = tempfile.mkdtemp()
        self.env = {"TAOQ_FIXTURE": FIXTURE, "TAOQ_DATA_DIR": self.dir, "TAOQ_NOW": str(T0),
                    "TAOQ_ENDPOINTS": "ws://127.0.0.1:9"}

    def __call__(self, *args, now=None):
        old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        if now is not None:
            os.environ["TAOQ_NOW"] = str(now)
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = taoq.main(list(args))
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return code, out.getvalue(), err.getvalue()

    def json(self, *args, **kw):
        code, out, err = self(*args, **kw)
        if code != 0:
            raise AssertionError(f"exit {code}: {err}")
        return json.loads(out)


class TestPieces(unittest.TestCase):
    def test_ss58(self):
        self.assertEqual(taoq.check_ss58(ALICE, "x"), ALICE)
        with self.assertRaisesRegex(taoq.Usage, "checksum"):
            taoq.check_ss58(ALICE[:-1] + "Z", "x")
        with self.assertRaisesRegex(taoq.Usage, "prefix 42"):
            taoq.check_ss58("15oF4uVJwmo4TdGW7VfQxNLavjCXviqxT9S1MgbjMNHr6Sp5", "x")   # a Polkadot (prefix 0) address
        with self.assertRaisesRegex(taoq.Usage, "not an SS58"):
            taoq.check_ss58("0xdeadbeef", "x")

    def test_exact_numbers(self):
        self.assertEqual(taoq.dumps({"a": taoq.tao(1), "b": taoq.tao(123456789012345678), "c": Decimal("2.500"), "d": None}),
                         '{"a":0.000000001,"b":123456789.012345678,"c":2.5,"d":null}')
        self.assertEqual(taoq.q9(Decimal("0.0000000015")), Decimal("0.000000002"))
        self.assertEqual(taoq.pct(110, 100), Decimal("10.00"))
        self.assertIsNone(taoq.pct(1, 0))
        self.assertEqual(taoq.frac(65535), Decimal(1))

    def test_pick_base(self):
        snaps = [{"at": T0 - 30 * 3600}, {"at": T0 - 22 * 3600}, {"at": T0 - 2 * 3600}, {"at": T0 - 60}]
        self.assertEqual(taoq.pick_base(snaps, T0, 20)["at"], T0 - 22 * 3600)
        self.assertEqual(taoq.pick_base(snaps[2:], T0, 20)["at"], T0 - 2 * 3600)   # nothing old enough: the oldest
        self.assertIsNone(taoq.pick_base(snaps[3:], T0, 20))                       # a minute old: no comparison

    def test_prune_order(self):
        # uid: 0 owner (immortal), 1 immune (new), 2..5 non-immune
        mg = {"hotkeys": ["h0", "h1", "h2", "h3", "h4", "h5"], "coldkeys": ["own", "c", "c", "c", "c", "c"],
              "owner_coldkey": "own", "owner_hotkey": "h0", "immunity_period": 100,
              "emission": [0, 0, 5, 0, 0, 9], "block_at_registration": [1, 990, 10, 30, 20, 5]}
        non_immune, immune, immortal, nxt = taoq.prune_order(mg, 1000, 1, 0)
        self.assertEqual(immortal, {0})
        self.assertEqual(immune, [1])
        self.assertEqual(non_immune, [4, 3, 2, 5])   # emission 0 first, older registration first, then by emission
        self.assertEqual(nxt, 4)
        # Not more non-immune neurons than MinNonImmuneUids: an immune one goes.
        self.assertEqual(taoq.prune_order(mg, 1000, 1, 4)[3], 1)


class TestFixture(unittest.TestCase):
    def setUp(self):
        self.fx = raw()
        self.run = Run()

    def test_stake(self):
        o = self.run.json("stake", OWNER_CK)
        infos = read(self.fx, "call", "StakeInfoRuntimeApi", "get_stake_info_for_coldkey", [OWNER_CK])
        prices = {p["netuid"]: p["price"] for p in read(self.fx, "call", "SwapRuntimeApi", "current_alpha_price_all", [])}
        acct = read(self.fx, "query", "System", "Account", [OWNER_CK])
        self.assertEqual(o["block"], self.fx["block"])
        self.assertEqual(len(o["positions"]), sum(1 for i in infos if i["stake"] > 0))
        p = o["positions"][0]
        i = next(x for x in infos if x["netuid"] == p["netuid"] and x["hotkey"] == p["hotkey"])
        self.assertEqual(Decimal(str(p["alpha"])), Decimal(i["stake"]) / 10**9)
        self.assertEqual(Decimal(str(p["price"])), Decimal(prices[p["netuid"]]) / 10**9)
        self.assertEqual(Decimal(str(p["value_tao"])), (Decimal(i["stake"]) * prices[p["netuid"]] / 10**18).quantize(Decimal("1e-9")))
        self.assertEqual(Decimal(str(o["totals"]["free_tao"])), Decimal(acct["data"]["free"]) / 10**9)
        self.assertTrue(p["own_hotkey"])
        self.assertIsNone(o["since"])
        self.assertLess(p["unstake_now_tao"], p["value_tao"])   # slippage on a large position
        # The snapshot was written; the next day's run compares with it.
        o2 = self.run.json("stake", OWNER_CK, now=T0 + 23 * 3600)
        self.assertEqual(o2["since"]["hours"], 23.0)
        self.assertEqual(o2["positions"][0]["change"]["alpha"], 0)

    def test_stake_changes_and_flags(self):
        o = self.run.json("stake", OWNER_CK)
        p = o["positions"][0]
        alpha_now = Decimal(str(p["alpha"]))
        price_now = Decimal(str(p["price"]))
        # Yesterday: 100 alpha less at a 20 % higher price, plus a position that is gone now.
        prev = {"at": T0 - 24 * 3600, "block": o["block"] - 7200, "free_rao": 0, "total_value_tao": "1",
                "positions": [
                    {"netuid": p["netuid"], "hotkey": p["hotkey"], "stake_rao": int((alpha_now - 100) * 10**9),
                     "price_rao": int(price_now * Decimal("1.2") * 10**9), "permit": True},
                    {"netuid": 4, "hotkey": VALI_HK, "stake_rao": 5 * 10**9, "price_rao": 10**8, "permit": True}]}
        path = os.path.join(self.run.dir, f"stake-{OWNER_CK}.json")
        with open(path, "w") as f:
            json.dump({"version": "1", "coldkey": OWNER_CK, "snapshots": [prev]}, f)
        o = self.run.json("stake", OWNER_CK, "--no-save")
        ch = o["positions"][0]["change"]
        self.assertEqual(Decimal(str(ch["alpha"])), Decimal(100))
        a0, p0 = alpha_now - 100, Decimal(prev["positions"][0]["price_rao"]) / 10**9
        self.assertEqual(Decimal(str(ch["value_from_alpha_tao"])), (100 * price_now).quantize(Decimal("1e-9")))
        self.assertEqual(Decimal(str(ch["value_from_price_tao"])), (a0 * (price_now - p0)).quantize(Decimal("1e-9")))
        # The two parts add up to the change in value of the position.
        self.assertAlmostEqual(float(ch["value_from_alpha_tao"]) + float(ch["value_from_price_tao"]),
                               float(alpha_now * price_now - a0 * p0), places=6)
        kinds = {f["kind"] for f in o["flags"]}
        self.assertIn("price_move", kinds)
        self.assertIn("position_gone", kinds)
        self.assertEqual(o["since"]["hours"], 24.0)

    def test_subnet(self):
        o = self.run.json("subnet", "70", "4")
        dyn = {d["netuid"]: d for d in read(self.fx, "call", "SubnetInfoRuntimeApi", "get_all_dynamic_info", [])}
        s = o["subnets"][0]
        self.assertEqual(s["netuid"], 70)
        self.assertEqual(Decimal(str(s["pool"]["tao"])), Decimal(dyn[70]["tao_in"]) / 10**9)
        self.assertEqual(s["owner_coldkey"], OWNER_CK)
        self.assertEqual(s["releases_api"], "https://api.github.com/repos/fugal-ai/fugal-subnet/releases?per_page=3")
        self.assertEqual(s["neurons"], read(self.fx, "multi", "SubtensorModule", "SubnetworkN", [[70], [4]])[0])
        shares = sum(Decimal(str(x["emission"]["share_pct"])) for x in o["subnets"])
        self.assertGreater(shares, 0)
        code, out, err = self.run("subnet", "999")
        self.assertEqual(code, 2)
        self.assertIn("no such subnet", err)

    def test_hotkey(self):
        o = self.run.json("hotkey", VALI_HK, "--netuid", "70")
        e = o["subnets"][0]
        mg = read(self.fx, "call", "SubnetInfoRuntimeApi", "get_metagraph", [70])
        uid = mg["hotkeys"].index(VALI_HK)
        self.assertEqual(e["uid"], uid)
        self.assertEqual(Decimal(str(e["emission_alpha_per_epoch"])), Decimal(mg["emission"][uid]) / 10**9)
        self.assertTrue(e["validator_permit"])
        self.assertEqual(e["blocks_since_weights"], o["block"] - mg["last_update"][uid])
        self.assertIn(e["deregistration"]["risk"], ("low", "medium", "high", "next", "immune", "none", "none_now"))
        self.assertIsNotNone(e["deregistration"]["next_to_prune_uid"])

    def test_week_and_brief(self):
        o = self.run.json("week")
        self.assertEqual(o["compared_to"]["source"], "archive node")
        self.assertEqual(len(o["movers"]["up"]), 5)
        ups = [m["price_pct"] for m in o["movers"]["up"]]
        self.assertEqual(ups, sorted(ups, reverse=True))
        b = self.run.json("subnet-brief", "70")
        mg = read(self.fx, "call", "SubnetInfoRuntimeApi", "get_metagraph", [70])
        self.assertEqual(b["metagraph"]["validators_with_permit"], sum(mg["validator_permit"]))
        code, text, _ = self.run("subnet-brief", "70", "--text")
        self.assertEqual(code, 0)
        self.assertIn("SN70 Fugal", text)

    def test_unreadable_is_an_error_not_a_number(self):
        code, out, err = self.run("stake", ALICE)   # not in the fixture
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["ok"], False)
        self.assertEqual(len(err.strip().splitlines()), 1)

    def test_endpoints_all_down(self):
        env = {"TAOQ_ENDPOINTS": "ws://127.0.0.1:9,ws://127.0.0.1:10", "TAOQ_TIMEOUT": "3", "TAOQ_ROUNDS": "1"}
        old = {k: os.environ.get(k) for k in [*env, "TAOQ_FIXTURE"]}
        os.environ.update(env)
        os.environ.pop("TAOQ_FIXTURE", None)
        try:
            try:
                import async_substrate_interface  # noqa: F401
            except ImportError:
                self.skipTest("async-substrate-interface is not installed here")
            with self.assertRaisesRegex(taoq.Unreadable, "127.0.0.1:9.*127.0.0.1:10"):
                taoq.run_read(lambda r: taoq.read_subnets(r))
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
