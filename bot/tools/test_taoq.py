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

    def __call__(self, *args, now=None, env=None):
        merged = {**self.env, **(env or {})}
        old = {k: os.environ.get(k) for k in merged}
        os.environ.update(merged)
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


def variant(mutate) -> str:
    """A copy of the recorded read with `mutate(reads)` applied; returns its path."""
    fx = raw()
    mutate(fx["reads"])
    fd, path = tempfile.mkstemp(suffix=".json.gz")
    os.close(fd)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(fx, f)
    return path


MG70 = taoq._key("call", "SubnetInfoRuntimeApi", "get_metagraph", [70])
PRICES = taoq._key("call", "SwapRuntimeApi", "current_alpha_price_all", [])
DYN = taoq._key("call", "SubnetInfoRuntimeApi", "get_all_dynamic_info", [])
UIDS70 = taoq._key("multi", "SubtensorModule", "Uids", [[70, VALI_HK]])
BLOCK = raw()["block"]
H6 = 6 * 3600


def validator_state(weights_age=None, emission=None, reg=None, permit=None):
    """A mutation of the recorded SN70 metagraph for the validator VALI_HK."""
    def mutate(reads):
        mg = reads[MG70]
        u = mg["hotkeys"].index(VALI_HK)
        if weights_age is not None:
            mg["last_update"][u] = BLOCK - weights_age
        if emission is not None:
            mg["emission"][u] = emission
        if reg is not None:
            mg["block_at_registration"][u] = reg
        if permit is not None:
            mg["validator_permit"][u] = permit
    return mutate


class TestWatchDiff(unittest.TestCase):
    """The comparison itself, on recorded sequences of findings."""

    W = {"weights_stale:SN70": {"severity": "warning", "text": "SN70 uid 1: last set weights 2000 blocks ago"}}
    D_HIGH = {"deregistration:SN70": {"severity": "warning", "text": "risk high"}}
    D_NEXT = {"deregistration:SN70": {"severity": "critical", "text": "risk next"}}

    def step(self, prev, found, t):
        d = taoq.watch_diff(prev, found, t)
        return d, d["stored"]

    def test_new_ongoing_escalated_resolved(self):
        d, st = self.step(None, self.W, T0)                    # new
        self.assertEqual([i["key"] for i in d["new"]], ["weights_stale:SN70"])
        self.assertEqual((d["ongoing"], d["resolved"], d["escalated"]), ([], [], []))
        d, st = self.step(st, self.W, T0 + H6)                 # the same, hours later: ongoing, nothing new
        self.assertEqual((d["new"], d["escalated"], d["resolved"]), ([], [], []))
        self.assertEqual(d["ongoing"], [f"weights_stale:SN70 warning since {taoq.iso(T0)}"])
        d, st = self.step(st, {**self.W, **self.D_HIGH}, T0 + 2 * H6)   # one more problem: only that one is new
        self.assertEqual([i["key"] for i in d["new"]], ["deregistration:SN70"])
        self.assertEqual(len(d["ongoing"]), 1)
        d, st = self.step(st, {**self.W, **self.D_NEXT}, T0 + 3 * H6)   # severity went up
        self.assertEqual([(i["key"], i["was"], i["severity"]) for i in d["escalated"]], [("deregistration:SN70", "warning", "critical")])
        self.assertEqual(d["new"], [])
        self.assertEqual(st["deregistration:SN70"]["since"], taoq.iso(T0 + 2 * H6))   # "since" is kept through an escalation
        d, st = self.step(st, {**self.W, **self.D_HIGH}, T0 + 4 * H6)   # better again: not an alert
        self.assertEqual((d["new"], d["escalated"], d["resolved"]), ([], [], []))
        self.assertIn("deregistration:SN70 warning since", " ".join(d["ongoing"]))
        d, st = self.step(st, {**self.W, **self.D_NEXT}, T0 + 5 * H6)   # worse again: told again
        self.assertEqual(len(d["escalated"]), 1)
        d, st = self.step(st, {}, T0 + 6 * H6)                          # everything cleared
        self.assertEqual(sorted(i["key"] for i in d["resolved"]), ["deregistration:SN70", "weights_stale:SN70"])
        self.assertEqual((d["new"], d["ongoing"], st), ([], [], {}))
        d, st = self.step(st, self.W, T0 + 7 * H6)                      # and back: new again
        self.assertEqual(len(d["new"]), 1)

    def test_events_are_new_every_time_and_not_kept(self):
        ev = {"price_move:SN70": {"severity": "warning", "event": True, "text": "up 12 %"}}
        d, st = self.step({}, ev, T0)
        self.assertEqual((len(d["new"]), st), (1, {}))
        d, st = self.step(st, ev, T0 + H6)
        self.assertEqual(len(d["new"]), 1)
        d, st = self.step(st, {}, T0 + 2 * H6)
        self.assertEqual((d["new"], d["resolved"]), ([], []))

    def test_new_sorted_by_severity(self):
        found = {"b": {"severity": "info", "text": "i"}, "a": {"severity": "warning", "text": "w"}, "c": {"severity": "critical", "text": "c"}}
        self.assertEqual([i["key"] for i in taoq.watch_diff(None, found, T0)["new"]], ["c", "a", "b"])

    def test_alert_field(self):
        prev = {"at": T0, "block": 1}
        quiet = taoq.watch_diff({}, {}, T0 + H6)
        o = taoq.watch_output("hotkey", {}, 2, T0 + H6, prev, quiet, [])
        self.assertIs(o["alert"], False)
        self.assertIn("send no message", o["summary"])
        self.assertEqual(taoq.watch_text(o, "x", "full report"), "NO_REPLY")
        ongoing_only = taoq.watch_diff({"k": {"severity": "warning", "text": "t", "since": "s"}}, {"k": {"severity": "warning", "text": "t"}}, T0)
        self.assertIs(taoq.watch_output("hotkey", {}, 2, T0, prev, ongoing_only, [])["alert"], False)
        res = taoq.watch_diff({"k": {"severity": "warning", "text": "t", "since": "s"}}, {}, T0)
        self.assertIs(taoq.watch_output("hotkey", {}, 2, T0, prev, res, [])["alert"], True)   # a cleared problem is told
        self.assertIs(taoq.watch_output("hotkey", {}, 2, T0, None, quiet, [])["alert"], True)  # the first check always answers

    def test_unusable_state_is_a_first_check(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "w.json")
        for junk in ("{not json", "[]", '{"alerts":[],"at":1}', '{"alerts":{}}'):
            with open(path, "w") as f:
                f.write(junk)
            self.assertIsNone(taoq.load_watch(path))


class TestHotkeyWatch(unittest.TestCase):
    """taoq hotkey --watch, end to end on variants of the recorded read, one watch after the other."""

    def setUp(self):
        self.run = Run()
        self.t = T0
        self.args = ("hotkey", VALI_HK, "--netuid", "70", "--watch")

    def check(self, mutate=None, *extra):
        self.t += H6
        env = {"TAOQ_FIXTURE": variant(mutate)} if mutate else {}
        return self.run.json(*self.args, *extra, now=self.t, env=env)

    def test_the_sequence(self):
        first = self.run.json(*self.args, now=T0)
        self.assertEqual((first["watch"], first["first_check"], first["alert"]), (True, True, True))
        self.assertEqual(first["registered_on"], [70])
        self.assertNotIn("subnets", first)                      # no 30 kB metagraph dump in a watch
        q = self.check()                                        # same chain: nothing to tell
        self.assertEqual((q["alert"], q["new"], q["ongoing"], q["escalated"], q["resolved"]), (False, [], [], [], []))
        code, out, _ = self.run(*self.args, "--text", now=self.t + 60)
        self.assertEqual((code, out.strip()), (0, "NO_REPLY"))
        s = self.check(validator_state(weights_age=2000))       # weights not set for 2000 blocks
        self.assertIs(s["alert"], True)
        self.assertEqual([(i["key"], i["severity"]) for i in s["new"]], [("weights_stale:SN70", "warning")])
        self.assertEqual(s["new"][0]["data"]["blocks_since_weights"], 2000)
        self.assertIn("2000 blocks", s["new"][0]["text"])
        o = self.check(validator_state(weights_age=2500))       # still not set, more blocks: the same alert is not repeated
        self.assertEqual((o["alert"], o["new"], o["escalated"], o["resolved"]), (False, [], [], []))
        self.assertEqual(len(o["ongoing"]), 1)
        self.assertTrue(o["ongoing"][0].startswith("weights_stale:SN70 warning since "))
        code, out, _ = self.run(*self.args, "--text", now=self.t + 60, env={"TAOQ_FIXTURE": variant(validator_state(weights_age=2600))})
        self.assertEqual(out.strip(), "NO_REPLY")
        # Emission to zero and one of the oldest registrations: the lowest emission goes first, ties to the oldest.
        mg = raw()["reads"][MG70]
        zero_regs = sorted(r for r, e, i in zip(mg["block_at_registration"], mg["emission"], range(len(mg["emission"]))) if e == 0)
        high = self.check(validator_state(weights_age=2500, emission=0, reg=zero_regs[1] + 1))       # third from the bottom
        self.assertEqual([(i["key"], i["severity"]) for i in high["new"]], [("deregistration:SN70", "warning")])
        self.assertIn("deregistration risk high", high["new"][0]["text"])
        self.assertEqual(len(high["ongoing"]), 1)               # the weights alert is not repeated
        nxt = self.check(validator_state(weights_age=2500, emission=0, reg=zero_regs[0]))
        self.assertEqual([(i["key"], i["was"], i["severity"]) for i in nxt["escalated"]], [("deregistration:SN70", "warning", "critical")])
        self.assertEqual((nxt["new"], nxt["alert"]), ([], True))
        code, out, _ = self.run(*self.args, "--text", now=self.t + 60,
                                env={"TAOQ_FIXTURE": variant(validator_state(weights_age=2500, emission=0, reg=zero_regs[1]))})
        # (better than before, high again: not an alert)
        self.assertEqual(out.strip(), "NO_REPLY")
        back = self.check()                                     # everything normal again
        self.assertIs(back["alert"], True)
        self.assertEqual(sorted(i["key"] for i in back["resolved"]), ["deregistration:SN70", "weights_stale:SN70"])
        self.assertEqual((back["new"], back["ongoing"]), ([], []))
        self.assertIs(self.check()["alert"], False)

    def test_text_of_an_alert(self):
        self.run.json(*self.args, now=T0)
        self.t += H6
        code, out, _ = self.run(*self.args, "--text", now=self.t, env={"TAOQ_FIXTURE": variant(validator_state(weights_age=2000))})
        lines = out.strip().splitlines()
        self.assertEqual(code, 0)
        self.assertTrue(lines[0].startswith("Hotkey watch 5E2LP6"), lines[0])
        self.assertIn("6.0 h since the last check", lines[0])
        self.assertTrue(lines[1].startswith("NEW warning: SN70 uid 1: has a validator permit but last set weights 2000 blocks"), lines[1])
        self.assertEqual(len(lines), 2)

    def test_first_check_text_is_the_report_and_a_note(self):
        code, out, _ = self.run(*self.args, "--text", now=T0)
        self.assertIn("registered on 1 subnet(s)", out)
        self.assertTrue(out.strip().endswith(taoq.WATCH_NOTE))

    def test_lost_permit_and_lost_registration_stay_until_regained(self):
        self.run.json(*self.args, now=T0)
        lost = self.check(validator_state(permit=False))
        self.assertEqual([(i["key"], i["severity"]) for i in lost["new"]], [("permit_lost:SN70", "warning")])
        again = self.check(validator_state(permit=False))
        self.assertEqual((again["alert"], len(again["ongoing"])), (False, 1))
        back = self.check()
        self.assertEqual([i["key"] for i in back["resolved"]], ["permit_lost:SN70"])
        # Not registered any more.
        gone = self.check(lambda reads: reads.__setitem__(UIDS70, [None]))
        self.assertEqual([(i["key"], i["severity"]) for i in gone["new"]], [("registration_lost:SN70", "critical")])
        self.assertEqual(gone["registered_on"], [])
        still = self.check(lambda reads: reads.__setitem__(UIDS70, [None]))
        self.assertEqual((still["alert"], len(still["ongoing"])), (False, 1))
        back = self.check()
        self.assertEqual([i["key"] for i in back["resolved"]], ["registration_lost:SN70"])

    def test_no_save_leaves_the_last_check_alone(self):
        self.run.json(*self.args, now=T0)
        self.t += H6
        self.run.json(*self.args, "--no-save", now=self.t, env={"TAOQ_FIXTURE": variant(validator_state(weights_age=2000))})
        again = self.check(validator_state(weights_age=2000))   # still new: the --no-save run recorded nothing
        self.assertEqual(len(again["new"]), 1)

    def test_other_watches_do_not_share_state(self):
        self.run.json(*self.args, now=T0)
        other = self.run.json("hotkey", VALI_HK, "--netuid", "70", "--watch", "--no-save", now=T0)
        self.assertEqual(other["first_check"], False)
        self.assertEqual(sorted(f for f in os.listdir(self.run.dir) if f.startswith("watch-")),
                         [f"watch-hotkey-{VALI_HK}-sn70.json"])

    def test_without_watch_the_report_is_unchanged(self):
        o = self.run.json("hotkey", VALI_HK, "--netuid", "70")
        self.assertNotIn("watch", o)
        self.assertEqual(o["flags"], [])


class TestSubnetWatch(unittest.TestCase):
    URL = "https://api.github.com/repos/fugal-ai/fugal-subnet/releases?per_page=3"

    def setUp(self):
        self.run = Run()
        self.t = T0
        self.args = ("subnet", "70", "--watch")
        self.rel = os.path.join(self.run.dir, "releases.json")
        self.set_releases(["v1.0.0"])

    def set_releases(self, tags):
        with open(self.rel, "w") as f:
            json.dump({self.URL: [{"tag_name": t, "name": t, "published_at": "2026-11-20T10:00:00Z",
                                   "html_url": f"https://github.com/fugal-ai/fugal-subnet/releases/tag/{t}", "draft": False, "prerelease": False}
                                  for t in tags]}, f)

    def price_x(self, factor):
        def mutate(reads):
            for p in reads[PRICES]:
                if p["netuid"] == 70:
                    p["price"] = int(Decimal(p["price"]) * Decimal(factor))
        return mutate

    def check(self, mutate=None, *extra):
        self.t += H6
        env = {"TAOQ_RELEASES_FIXTURE": self.rel}
        if mutate:
            env["TAOQ_FIXTURE"] = variant(mutate)
        return self.run.json(*self.args, *extra, now=self.t, env=env)

    def test_the_sequence(self):
        first = self.run.json(*self.args, now=T0, env={"TAOQ_RELEASES_FIXTURE": self.rel})
        self.assertEqual((first["first_check"], first["alert"], first["netuids"]), (True, True, [70]))
        q = self.check()
        self.assertEqual((q["alert"], q["new"], q["ongoing"]), (False, [], []))
        code, out, _ = self.run(*self.args, "--text", now=self.t + 60, env={"TAOQ_RELEASES_FIXTURE": self.rel})
        self.assertEqual(out.strip(), "NO_REPLY")
        up = self.check(self.price_x("1.25"))
        self.assertEqual([(i["key"], i["severity"]) for i in up["new"]], [("price_move:SN70", "warning")])
        d = up["new"][0]["data"]
        self.assertEqual((d["price_pct"], Decimal(str(d["price_before"]))), (25, Decimal("0.003493646")))   # exact, from the raw rao
        self.assertEqual(Decimal(str(d["price_after"])), Decimal("0.004367057"))
        self.assertIn("+25", up["new"][0]["text"])
        flat = self.check(self.price_x("1.25"))                 # stays there: the move was told once
        self.assertEqual((flat["alert"], flat["new"]), (False, []))
        small = self.check(self.price_x("1.30"))                # +4 %: below the threshold
        self.assertEqual(small["alert"], False)
        down = self.check(self.price_x("0.5"))
        self.assertEqual([i["key"] for i in down["new"]], ["price_move:SN70"])
        self.assertLess(down["new"][0]["data"]["price_pct"], -50)
        none = self.run.json(*self.args, "--price-alert", "80", now=self.t + 3 * H6, env={"TAOQ_RELEASES_FIXTURE": self.rel, "TAOQ_FIXTURE": variant(self.price_x("0.5"))})
        self.assertFalse(none["alert"])

    def test_releases(self):
        self.run.json(*self.args, now=T0, env={"TAOQ_RELEASES_FIXTURE": self.rel})
        self.set_releases(["v1.1.0", "v1.0.0"])
        r = self.check()
        self.assertEqual([(i["key"], i["severity"]) for i in r["new"]], [("release:SN70:v1.1.0", "info")])
        self.assertIn("https://github.com/fugal-ai/fugal-subnet/releases/tag/v1.1.0", r["new"][0]["text"])
        self.assertFalse(self.check()["alert"])                 # told once
        code, out, _ = self.run(*self.args, "--text", now=self.t + H6 + 5, env={"TAOQ_RELEASES_FIXTURE": self.rel})
        self.assertEqual(out.strip(), "NO_REPLY")

    def test_releases_that_cannot_be_read_are_named_not_guessed(self):
        self.run.json(*self.args, now=T0, env={"TAOQ_RELEASES_FIXTURE": self.rel})
        self.t += H6
        o = self.run.json(*self.args, now=self.t)               # fixture mode and no release answers: no network
        self.assertEqual((o["alert"], o["unread"]), (False, ["releases of SN70"]))
        self.set_releases(["v1.1.0", "v1.0.0"])                 # the next readable check still has its baseline
        self.assertEqual([i["key"] for i in self.check()["new"]], ["release:SN70:v1.1.0"])

    def test_a_replaced_subnet_is_critical(self):
        self.run.json(*self.args, now=T0, env={"TAOQ_RELEASES_FIXTURE": self.rel})

        def replaced(reads):
            for d in reads[DYN]:
                if d and d["netuid"] == 70:
                    d["network_registered_at"] = BLOCK - 10
        o = self.check(replaced)
        self.assertEqual([(i["key"], i["severity"]) for i in o["new"]], [("subnet_replaced:SN70", "critical")])

    def test_several_subnets_one_watch(self):
        o = self.run.json("subnet", "70", "4", "--watch", now=T0)
        self.assertEqual(o["netuids"], [70, 4])
        self.assertTrue(os.path.exists(os.path.join(self.run.dir, "watch-subnet-4-70.json")))

    def test_without_watch_the_report_is_unchanged(self):
        o = self.run.json("subnet", "70")
        self.assertNotIn("watch", o)
        self.assertEqual(o["subnets"][0]["netuid"], 70)


# ---- news and the daily briefing (the shared feed) ----------------------------------------
# fixtures/feed-sn70.json and feed-network.json are real answers of the feed (the controller's
# feed/ service, run locally on 2026-11-23 after one polite pass over its sources):
#   /feed/v1/items?since=2026-11-22T21:00:00Z&limit=40&netuids=70   and   ...&limit=30&network=1
FEED_SN70 = os.path.join(HERE, "fixtures", "feed-sn70.json")
FEED_NET = os.path.join(HERE, "fixtures", "feed-network.json")


def feed_answer(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class FeedServer:
    """The feed on loopback: answers /feed/v1/items like the real one, from recorded answers, and
    keeps every query it was sent."""

    def __init__(self, subnets=None, network=None, status=200, body=None):
        import http.server
        import threading
        self.queries = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                from urllib.parse import parse_qs, urlsplit
                u = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                outer.queries.append((u.path, q))
                if body is not None:
                    data = body
                else:
                    ans = subnets if "netuids" in q else network
                    data = json.dumps(ans if ans is not None else {"ok": True, "now": "2026-11-23T21:27:46Z", "since": q.get("since"),
                                                                  "items": [], "more": False, "not_covered": {}, "failing": []}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/feed/v1"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class NewsRun(Run):
    """taoq against the chain fixture AND a feed (a loopback server, or nothing listening)."""

    def __init__(self, feed_url="http://127.0.0.1:9/feed/v1"):
        super().__init__()
        self.env["TAOQ_FEED_URL"] = feed_url
        self.env.pop("TAOQ_FEED_FIXTURE", None)

    def __call__(self, *args, now=None, env=None):
        # the chain fixture alone would switch the feed off; here the feed is read from TAOQ_FEED_URL
        saved = taoq.feed_get

        def real_feed_get(params):
            fx = os.environ.pop("TAOQ_FIXTURE", None)
            try:
                return saved(params)
            finally:
                if fx is not None:
                    os.environ["TAOQ_FIXTURE"] = fx
        taoq.feed_get = real_feed_get
        try:
            return super().__call__(*args, now=now, env=env)
        finally:
            taoq.feed_get = saved


class TestNews(unittest.TestCase):
    def setUp(self):
        self.feed = FeedServer(feed_answer(FEED_SN70), feed_answer(FEED_NET))
        self.run = NewsRun(self.feed.url)

    def tearDown(self):
        self.feed.close()

    def test_news_for_a_subnet_asks_the_feed_and_passes_its_items_on(self):
        o = self.run.json("news", "70", "--since", "2026-11-22T21:00:00Z")
        self.assertEqual(self.feed.queries, [("/feed/v1/items", {"since": "2026-11-22T21:00:00Z", "limit": "30", "netuids": "70"})])
        want = feed_answer(FEED_SN70)
        self.assertEqual([i["title"] for i in o["items"]], [i["title"] for i in want["items"]])
        self.assertEqual((o["netuids"], o["network"], o["now"], o["not_covered"]), ([70], False, want["now"], {"70": ["blog", "discord", "x"]}))
        self.assertIn("Discord chat or X", o["note"])
        for it in o["items"]:
            self.assertEqual(set(it), {"t", "sn", "k", "src", "title", "text", "url"})
            self.assertLessEqual(len(it["text"]), 200)

    def test_no_netuids_means_network_wide_and_netuids_can_be_a_list(self):
        self.run.json("news")
        self.run.json("news", "--netuids", "64,70", "--network", "--since-hours", "48")
        q1, q2 = self.feed.queries[0][1], self.feed.queries[1][1]
        self.assertEqual((q1.get("network"), "netuids" in q1), ("1", False))
        self.assertEqual((q2["netuids"], q2["network"], q2["since"]), ("64,70", "1", taoq.iso(T0 - 48 * 3600)))
        self.assertEqual(q1["since"], taoq.iso(T0 - 86400))                         # default: the last 24 hours

    def test_mine_reads_the_wallets_subnets_from_the_chain(self):
        o = self.run.json("news", "--mine", OWNER_CK)
        self.assertEqual(self.feed.queries[0][1]["netuids"], "70")
        self.assertEqual(self.feed.queries[0][1]["network"], "1")
        self.assertEqual((o["netuids"], o["network"], o["mine"]), ([70], True, OWNER_CK))

    def test_text_is_short_with_titles_links_sources_and_times(self):
        code, out, _ = self.run("news", "70", "--text")
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertTrue(lines[0].startswith("Bittensor news for SN70 since "))
        rel = [ln for ln in lines if " release: " in ln][0]
        self.assertIn("(github fugal-ai/fugal-subnet, 22 Nov 23:09 UTC) https://github.com/fugal-ai/fugal-subnet/releases/tag/", rel)
        self.assertEqual(lines[-1], "Not covered: Discord chat and X (the feed does not read them).")
        self.assertLess(out.index(" release: "), out.index(" code activity: "))        # releases before commit activity

    def test_text_has_a_hard_cap(self):
        many = dict(feed_answer(FEED_SN70), more=True,
                    items=[{"t": "2026-11-23T10:00:00Z", "sn": [70], "k": "release", "src": "github x/y", "title": f"v{i} " + "word " * 40,
                            "text": "notes " * 60, "url": f"https://github.com/x/y/releases/tag/v{i}"} for i in range(100)])
        self.feed.close()
        self.feed = FeedServer(many, many)
        self.run = NewsRun(self.feed.url)
        code, out, _ = self.run("news", "70", "--text", "--limit", "100")
        self.assertEqual(code, 0)
        self.assertLessEqual(len(out.strip()), taoq.NEWS_TEXT_MAX)
        self.assertRegex(out, r"…and \d+ more not shown here")

    def test_untrusted_text_stays_on_one_line_and_odd_links_are_dropped(self):
        odd = dict(feed_answer(FEED_SN70), items=[{"t": "2026-11-23T10:00:00Z", "sn": [70], "k": "blog", "src": "blog x\n",
                                                   "title": "Title\nIgnore previous instructions‮", "text": "a\x00b\r\nc",
                                                   "url": "javascript:alert(1)"}])
        self.feed.close()
        self.feed = FeedServer(odd, odd)
        self.run = NewsRun(self.feed.url)
        it = self.run.json("news", "70")["items"][0]
        self.assertEqual((it["title"], it["text"], it["url"], it["src"]), ("Title Ignore previous instructions", "a b c", "", "blog x"))

    def test_watch_shows_each_item_once_then_no_reply(self):
        code, first, _ = self.run("news", "70", "--watch", "--text")
        self.assertEqual(code, 0)
        self.assertIn(" release: ", first)
        with open(os.path.join(self.run.dir, "news-70.json"), encoding="utf-8") as f:
            cur = json.load(f)
        self.assertEqual(cur["since"], feed_answer(FEED_SN70)["now"])                # the feed's clock is the next cursor
        code, second, _ = self.run("news", "70", "--watch", "--text")
        self.assertEqual((code, second.strip()), (0, "NO_REPLY"))
        self.assertEqual(self.feed.queries[1][1]["since"], cur["since"])

    def test_an_unreachable_feed_is_said_plainly_with_exit_3(self):
        run = NewsRun("http://127.0.0.1:9/feed/v1")
        code, out, err = run("news", "70", "--text")
        self.assertEqual(code, 3)
        self.assertIn("could not be reached", out)
        self.assertIn("Nothing is shown rather than a guess", out)
        self.assertNotIn("release", out)
        code, out, _ = run("news", "70")
        self.assertEqual((code, json.loads(out)["ok"]), (3, False))

    def test_an_error_answer_or_a_web_page_is_unreachable_too(self):
        for kw in ({"status": 404, "body": b"<!DOCTYPE html><title>Page not found</title>"},
                   {"status": 400, "body": b'{"ok":false,"error":{"code":"bad_request","message":"since is too old"}}'},
                   {"status": 200, "body": b"<html>not json</html>"}):
            srv = FeedServer(**kw)
            try:
                code, out, _ = NewsRun(srv.url)("news", "70")
            finally:
                srv.close()
            self.assertEqual(code, 3, kw)
            self.assertIn("could not be reached", json.loads(out)["error"])

    def test_bad_arguments(self):
        for args in (("news", "70", "--since", "yesterday"), ("news", "70", "--limit", "500"), ("news", "--netuids", "x,64")):
            self.assertEqual(self.run(*args)[0], 2, args)
        self.assertEqual(self.feed.queries, [])


class TestBriefing(unittest.TestCase):
    def setUp(self):
        self.feed = FeedServer(feed_answer(FEED_SN70), feed_answer(FEED_NET))
        self.run = NewsRun(self.feed.url)

    def tearDown(self):
        self.feed.close()

    def test_one_command_stake_then_your_subnets_then_headlines(self):
        code, out, _ = self.run("briefing", OWNER_CK, "--text")
        self.assertEqual(code, 0)
        stake = Run()("stake", OWNER_CK, "--text", "--no-news")[1].strip()
        self.assertTrue(out.startswith(stake + "\n\n"), out[:400])                     # the stake report, exactly
        a, b = out.index("What changed on your subnets since "), out.index("Bittensor headlines:")
        self.assertLess(a, b)
        self.assertIn("- SN70 release: Miner image image-v2026.11.22b", out[a:b])
        self.assertIn("- Network runtime: Runtime 494", out[b:])
        self.assertTrue(out.strip().endswith("Not covered: Discord chat and X (the feed does not read them)."))
        self.assertEqual([q[1].get("netuids") for q in self.feed.queries], ["70", None])  # your subnets, then the network
        self.assertEqual(self.feed.queries[1][1]["network"], "1")
        o = json.loads(self.run("briefing", OWNER_CK, "--no-save")[1])
        self.assertEqual((o["command"], o["usd"], o["news"]["ok"], o["stake"]["command"]), ("briefing", None, True, "stake"))

    def test_the_next_briefing_shows_only_what_is_new(self):
        self.run("briefing", OWNER_CK, "--text")
        with open(os.path.join(self.run.dir, f"news-briefing-{OWNER_CK}.json"), encoding="utf-8") as f:
            cur = json.load(f)
        self.assertEqual(cur["since"], feed_answer(FEED_NET)["now"])
        code, out, _ = self.run("briefing", OWNER_CK, "--text", now=T0 + 86400)
        self.assertEqual(self.feed.queries[-1][1]["since"], cur["since"])
        self.assertIn("- nothing new from the feed for SN70.", out)                  # the same answers: all shown already
        self.assertIn("- nothing new on the network.", out)

    def test_no_feed_the_stake_report_and_one_line(self):
        run = NewsRun("http://127.0.0.1:9/feed/v1")
        code, out, _ = run("briefing", OWNER_CK, "--text")
        stake = Run()("stake", OWNER_CK, "--text", "--no-news")[1].strip()
        self.assertEqual(code, 0)
        rest = out.strip()[len(stake):].strip()
        self.assertTrue(out.startswith(stake))
        self.assertEqual(len(rest.splitlines()), 1)
        self.assertTrue(rest.startswith("News: the news feed could not be reached"), rest)
        self.assertFalse(os.path.exists(os.path.join(run.dir, f"news-briefing-{OWNER_CK}.json")))   # the cursor did not move

    def test_no_stake_a_network_briefing(self):
        orig = taoq._stake_reads

        async def empty(rd, ck):
            r = dict(await orig(rd, OWNER_CK))
            r["positions"] = []
            return r
        taoq._stake_reads = empty
        try:
            code, out, _ = self.run("briefing", ALICE, "--text")
        finally:
            taoq._stake_reads = orig
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("Daily briefing for 5Grwva…utQY: no stake on this wallet yet (2.3535 TAO free), so here is the network news."), out)
        self.assertNotIn("What changed on your subnets", out)
        self.assertIn("Bittensor headlines:\n- Network runtime: Runtime 494", out)
        self.assertEqual([q[1].get("netuids") for q in self.feed.queries], [None])

    def test_the_chain_unreadable_is_still_exit_1(self):
        orig = taoq.run_read

        def down(*a, **k):
            raise taoq.Unreadable("could not read the chain from any endpoint")
        taoq.run_read = down
        try:
            code, out, _ = self.run("briefing", OWNER_CK, "--text")
        finally:
            taoq.run_read = orig
        self.assertEqual(code, 1)
        self.assertEqual(self.feed.queries, [])


class TestOldDailyJobs(unittest.TestCase):
    """Daily jobs made before the briefing run `taoq stake <address> --text`."""

    def test_stake_text_gains_the_news_when_the_feed_answers(self):
        feed = FeedServer(feed_answer(FEED_SN70), feed_answer(FEED_NET))
        try:
            run = NewsRun(feed.url)
            out = run("stake", OWNER_CK, "--text")[1]
            briefing = NewsRun(feed.url)("briefing", OWNER_CK, "--text")[1]
        finally:
            feed.close()
        self.assertEqual(out, briefing)                                               # the same text as the briefing
        self.assertIn("What changed on your subnets", out)

    def test_stake_text_is_unchanged_when_the_feed_does_not_answer(self):
        plain = Run()("stake", OWNER_CK, "--text")[1]                                 # fixture mode: the feed is not read
        self.assertEqual(NewsRun("http://127.0.0.1:9/feed/v1")("stake", OWNER_CK, "--text")[1], plain)
        self.assertNotIn("News", plain)
        self.assertNotIn("headlines", plain)

    def test_stake_json_never_reads_the_feed(self):
        feed = FeedServer(feed_answer(FEED_SN70), feed_answer(FEED_NET))
        try:
            o = NewsRun(feed.url).json("stake", OWNER_CK)
        finally:
            feed.close()
        self.assertEqual(feed.queries, [])
        self.assertNotIn("news", o)


class TestFollowSubnet(unittest.TestCase):
    """"Follow subnet N": `taoq subnet N --watch --news --text`, one script job, no model call."""

    def test_feed_items_become_events_once_and_quiet_is_no_reply(self):
        blog = {"t": "2026-11-23T12:00:00Z", "sn": [70], "k": "blog", "src": "blog fugalai.com", "title": "Router v4 is live",
                "text": "What changed for miners.", "url": "https://fugalai.com/blog/v4"}
        gh = {"t": "2026-11-23T12:00:00Z", "sn": [70], "k": "release", "src": "github fugal-ai/fugal-subnet", "title": "v9",
              "text": "", "url": "https://github.com/fugal-ai/fugal-subnet/releases/tag/v9"}
        empty = dict(feed_answer(FEED_SN70), items=[])
        feed = FeedServer(empty, empty)
        run = NewsRun(feed.url)
        try:
            code, first, _ = run("subnet", "70", "--watch", "--news", "--text")              # the baseline
            self.assertEqual(code, 0)
            self.assertIn("Watching from now", first)
            feed.close()
            feed = FeedServer(dict(empty, items=[blog, gh] + feed_answer(FEED_SN70)["items"]), empty)
            run.env["TAOQ_FEED_URL"] = feed.url
            code, out, _ = run("subnet", "70", "--watch", "--news", "--text", now=T0 + 6 * 3600)
            self.assertEqual(code, 0)
            self.assertIn("NEW info: SN70 blog: Router v4 is live — What changed for miners. (blog fugalai.com, 23 Nov 12:00 UTC) https://fugalai.com/blog/v4", out)
            self.assertNotIn("v9", out)                                     # GitHub releases: the watch reads them itself
            self.assertNotIn("code activity", out)
            self.assertEqual(feed.queries[0][1]["netuids"], "70")
            code, out, _ = run("subnet", "70", "--watch", "--news", "--text", now=T0 + 12 * 3600)
            self.assertEqual((code, out.strip()), (0, "NO_REPLY"))          # the same items: already told
        finally:
            feed.close()

    def test_an_unreachable_feed_does_not_wake_anyone(self):
        run = NewsRun("http://127.0.0.1:9/feed/v1")
        run("subnet", "70", "--watch", "--news", "--text")
        code, out, _ = run("subnet", "70", "--watch", "--news", "--text", now=T0 + 6 * 3600)
        self.assertEqual((code, out.strip()), (0, "NO_REPLY"))
        o = json.loads(run("subnet", "70", "--watch", "--news", now=T0 + 12 * 3600)[1])
        self.assertIn("the news feed", o["unread"])


if __name__ == "__main__":
    unittest.main()
