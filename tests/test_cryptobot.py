import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cryptobot  # noqa: E402

CFG = cryptobot.CONFIG


def bar(o, h, l, c):
    return {"o": o, "h": h, "l": l, "c": c}


def trend_up(n=320, start=100.0, step=0.3, wiggle=1.0):
    """ゆるやかな上昇トレンド (毎足 ±wiggle の値幅)."""
    out, p = [], start
    for i in range(n):
        o = p
        p = p + step + (wiggle * 0.5 if i % 2 else -wiggle * 0.3)
        out.append(bar(o, max(o, p) + wiggle * 0.5, min(o, p) - wiggle * 0.5, p))
    return out


def flat(n=320, price=100.0, wiggle=1.0):
    return [bar(price, price + wiggle, price - wiggle, price + (0.2 if i % 2 else -0.2))
            for i in range(n)]


class IndicatorTest(unittest.TestCase):
    def test_ema_matches_constant(self):
        self.assertAlmostEqual(cryptobot.ema([5.0] * 30, 10), 5.0)
        self.assertIsNone(cryptobot.ema([1.0] * 5, 10))

    def test_atr_of_constant_range(self):
        atrs = cryptobot.atr_series(flat(50, wiggle=1.0), 14)
        self.assertIsNone(atrs[12])
        self.assertAlmostEqual(atrs[-1], 2.0, places=6)


class RegimeTest(unittest.TestCase):
    def test_uptrend(self):
        r = cryptobot.regime(trend_up())
        self.assertEqual((r["regime"], r["direction"]), ("trend", "long"))

    def test_flat_is_range(self):
        self.assertEqual(cryptobot.regime(flat())["regime"], "range")

    def test_volatility_spike_is_crisis(self):
        cs = trend_up()
        p = cs[-1]["c"]
        cs += [bar(p, p + 40, p - 40, p + (5 if i % 2 else -5)) for i in range(15)]
        self.assertEqual(cryptobot.regime(cs)["regime"], "crisis")

    def test_too_few_bars(self):
        self.assertIsNone(cryptobot.regime(trend_up(100))["regime"])


class SignalTest(unittest.TestCase):
    def test_breakout_long_with_stop_and_target(self):
        cs = trend_up()
        p = cs[-1]["c"]
        cs.append(bar(p, p + 6, p, p + 5))  # 直近高値を終値で上抜け
        s = cryptobot.signal(cs)
        self.assertEqual(s["side"], "buy")
        self.assertLess(s["stop"], s["entry"])
        self.assertAlmostEqual(s["take"] - s["entry"],
                               (s["entry"] - s["stop"]) * CFG["take_r"])

    def test_no_signal_in_range(self):
        self.assertIsNone(cryptobot.signal(flat()))

    def test_no_counter_trend_short(self):
        cs = trend_up()
        p = cs[-1]["c"]
        cs.append(bar(p, p, p - 30, p - 29))
        sig = cryptobot.signal(cs)
        self.assertTrue(sig is None or sig["side"] == "buy")


class SizingTest(unittest.TestCase):
    def test_risk_is_two_percent(self):
        qty = cryptobot.size_position(1000, 100, 95)
        self.assertAlmostEqual(qty * 5, 1000 * CFG["risk_pct"])

    def test_leverage_cap(self):
        qty = cryptobot.size_position(1000, 100, 99.99)  # 極端に狭い損切り
        cap = 1000 * CFG["max_leverage"] / CFG["max_positions"]
        self.assertLessEqual(qty * 100, cap + 1e-9)

    def test_contracts_round_down_and_minimum(self):
        self.assertEqual(cryptobot.to_contracts(2.57, 0.01, 1, 1), 257)
        self.assertEqual(cryptobot.to_contracts(0.009, 0.01, 1, 1), 0)

    def test_liquidation_check(self):
        self.assertTrue(cryptobot.stop_inside_liquidation(100, 95, 3))
        self.assertFalse(cryptobot.stop_inside_liquidation(100, 80, 3))


class RiskGateTest(unittest.TestCase):
    def state(self, peak=1000, day_start=1000, **kw):
        return dict({"peak_equity": peak, "day_start_equity": day_start, "day": "d"}, **kw)

    def test_ok(self):
        self.assertTrue(cryptobot.risk_gate(self.state(), 990, 0)[0])

    def test_daily_loss_blocks_entries(self):
        ok, _, flatten = cryptobot.risk_gate(self.state(), 940, 0)
        self.assertFalse(ok)
        self.assertFalse(flatten)

    def test_max_drawdown_flattens(self):
        ok, _, flatten = cryptobot.risk_gate(self.state(day_start=850), 840, 0)
        self.assertFalse(ok)
        self.assertTrue(flatten)

    def test_halted(self):
        self.assertFalse(cryptobot.risk_gate(self.state(halted=True), 1000, 0)[0])

    def test_max_positions(self):
        self.assertFalse(cryptobot.risk_gate(self.state(), 1000, CFG["max_positions"])[0])

    def test_update_state_rolls_day_and_tracks_peak(self):
        s = cryptobot.update_state({"day": "a", "day_start_equity": 900,
                                    "peak_equity": 1200}, 1000, "b")
        self.assertEqual((s["day_start_equity"], s["peak_equity"]), (1000, 1200))
        s = cryptobot.update_state(s, 1300, "b")
        self.assertEqual((s["day_start_equity"], s["peak_equity"]), (1000, 1300))

    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            self.assertEqual(cryptobot.load_state(path), {})
            cryptobot.save_state({"halted": True}, path)
            self.assertEqual(cryptobot.load_state(path), {"halted": True})


class BacktestTest(unittest.TestCase):
    def test_runs_and_accounts_costs(self):
        res = cryptobot.backtest(trend_up(600))
        self.assertGreater(res["trades"], 0)
        for t in res["log"]:
            gross = (t["exit"] - t["entry"]) * (1 if t["side"] == "buy" else -1)
            if gross <= 0:
                self.assertLess(t["pnl"], 0)
        self.assertGreaterEqual(res["max_dd"], 0)

    def test_no_trades_in_range(self):
        res = cryptobot.backtest(flat(500))
        self.assertEqual((res["trades"], res["return"]), (0, 0))

    def test_stop_hit_loses_about_risk(self):
        cs = trend_up(320)
        p = cs[-1]["c"]
        cs.append(bar(p, p + 6, p, p + 5))           # シグナル
        q = cs[-1]["c"]
        cs.append(bar(q, q, q - 50, q - 49))          # 次の足で約定して損切り
        res = cryptobot.backtest(cs)
        last = res["log"][-1]
        self.assertEqual((last["side"], last["win"]), ("buy", False))
        equity_before = res["final_equity"] - last["pnl"]
        loss = -last["pnl"] / equity_before
        self.assertGreater(loss, CFG["risk_pct"] * 0.9)
        self.assertLess(loss, CFG["risk_pct"] * 1.3)


if __name__ == "__main__":
    unittest.main()
