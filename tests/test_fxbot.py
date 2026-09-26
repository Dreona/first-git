import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import fxbot  # noqa: E402


def c(o, h, l, cl):
    return {"o": o, "h": h, "l": l, "c": cl}


class SizingTest(unittest.TestCase):
    def test_100k_balance_risks_2000_yen(self):
        orders = fxbot.build_orders(100_000)
        self.assertEqual([o["units"] for o in orders], ["-1000", "-1000"])
        self.assertEqual([o["takeProfitOnFill"]["price"] for o in orders],
                         ["157.000", "155.500"])
        self.assertTrue(all(o["stopLossOnFill"]["price"] == "160.200" for o in orders))
        total = sum(abs(int(o["units"])) for o in orders)
        self.assertLessEqual(total * 1.0, 100_000 * 0.02)

    def test_too_small_balance_places_nothing(self):
        self.assertEqual(fxbot.build_orders(50_000), [])

    def test_margin_cap(self):
        plan = dict(fxbot.PLAN, stop=159.201)  # 極端に狭い損切り
        units = fxbot.size_units(100_000, plan["entry"], plan["stop"], 0.02, 25)
        self.assertLessEqual(units * plan["entry"] / 25, 100_000)


class ManageTest(unittest.TestCase):
    pending = [{"id": "10", "clientExtensions": {"tag": "fxbot"}}]

    def test_cancel_when_price_breaks_down_before_fill(self):
        acts = fxbot.manage_actions(156.5, self.pending, [])
        self.assertEqual(acts[0][:2], ("cancel", "10"))

    def test_no_cancel_above_line(self):
        self.assertEqual(fxbot.manage_actions(158.0, self.pending, []), [])

    def test_move_stop_to_breakeven_after_tp1(self):
        trades = [{"id": "20", "clientExtensions": {"tag": "fxbot"},
                   "stopLossOrder": {"price": "160.200"}}]
        acts = fxbot.manage_actions(156.9, [], trades)
        self.assertEqual(acts, [("move_sl", "20", "159.200")])

    def test_ignores_foreign_orders(self):
        acts = fxbot.manage_actions(150.0, [{"id": "1", "clientExtensions": {}}], [])
        self.assertEqual(acts, [])


class PatternTest(unittest.TestCase):
    def test_bearish_engulfing(self):
        cs = [c(157, 158, 156.5, 157.5), c(158, 159, 157.8, 158.8),
              c(159, 159.2, 157.5, 157.7)]
        self.assertEqual(fxbot.candle_pattern(cs), "陰の包み足")

    def test_shooting_star(self):
        cs = [c(157, 158, 156.5, 157.5), c(157.5, 158, 157, 157.4),
              c(158.0, 159.5, 157.9, 158.1)]
        self.assertTrue(fxbot.candle_pattern(cs).startswith("上ヒゲ"))

    def test_sma_trend(self):
        closes = [150 + i * 0.1 for i in range(80)]
        t = fxbot.technicals([c(x, x, x, x) for x in closes])
        self.assertEqual(t["trend"], "上昇")


if __name__ == "__main__":
    unittest.main()
