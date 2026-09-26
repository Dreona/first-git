import os
import sys
import types
import unittest
from argparse import Namespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import fxbot_mt5 as bot  # noqa: E402


class FakeMT5(types.SimpleNamespace):
    """MetaTrader5 モジュールの必要部分だけを模したもの (円口座・残高10万円)."""

    def __init__(self, balance=100_000.0, bid=158.1, orders=(), positions=()):
        super().__init__(
            TRADE_ACTION_PENDING=5, TRADE_ACTION_REMOVE=8, TRADE_ACTION_SLTP=6,
            ORDER_TYPE_SELL_LIMIT=3, ORDER_TYPE_SELL=1, ORDER_TYPE_BUY=0,
            ORDER_TIME_GTC=0, ORDER_FILLING_FOK=0, ORDER_FILLING_IOC=1,
            ORDER_FILLING_RETURN=2, TRADE_RETCODE_DONE=10009,
            TRADE_RETCODE_INVALID_FILL=10030, TIMEFRAME_D1=16408)
        self.acc = types.SimpleNamespace(balance=balance, currency="JPY")
        self.tick = types.SimpleNamespace(bid=bid, ask=bid + 0.01)
        self.orders, self.positions, self.sent = list(orders), list(positions), []
        self.reject_return = False

    def symbol_info_tick(self, s):
        return self.tick

    def symbol_info(self, s):
        return types.SimpleNamespace(volume_min=0.01, volume_step=0.01, digits=3)

    def order_calc_profit(self, t, s, vol, open_, close):
        return (open_ - close) * 100_000 * vol  # 1lot = 10万通貨、円建て

    def orders_get(self, symbol):
        return self.orders

    def positions_get(self, symbol):
        return self.positions

    def order_send(self, req):
        self.sent.append(req)
        code = (self.TRADE_RETCODE_INVALID_FILL
                if self.reject_return and req.get("type_filling") == self.ORDER_FILLING_RETURN
                else self.TRADE_RETCODE_DONE)
        return types.SimpleNamespace(retcode=code, order=len(self.sent), comment="")

    def last_error(self):
        return (0, "")


def pos(ticket, sl, magic=bot.MAGIC):
    return types.SimpleNamespace(ticket=ticket, magic=magic, sl=sl, tp=155.5,
                                 volume=0.01, price_open=159.2, profit=0.0)


class SizingTest(unittest.TestCase):
    def test_jpy_account_100k(self):
        # 1lot の損失 10万円、リスク 2,000円 → 0.02lot を2分割
        self.assertAlmostEqual(bot.size_lots(100_000, 0.02, 100_000, 0.01, 0.01, 2), 0.01)

    def test_usd_account(self):
        # 約 $640 の口座、1lot の損失 約 $624
        self.assertAlmostEqual(bot.size_lots(640, 0.02, 624, 0.01, 0.01, 2), 0.01)

    def test_below_min_lot(self):
        self.assertEqual(bot.size_lots(40_000, 0.02, 100_000, 0.01, 0.01, 2), 0.0)

    def test_never_exceeds_risk(self):
        for bal in (100_000, 123_456, 250_000, 1_000_000):
            lots = bot.size_lots(bal, 0.02, 100_000, 0.01, 0.01, 2)
            self.assertLessEqual(lots * 2 * 100_000, bal * 0.02 + 1e-6)


class ManageTest(unittest.TestCase):
    order = {"ticket": 1, "magic": bot.MAGIC, "sl": 160.2, "tp": 157.0}

    def test_cancel_below_line(self):
        self.assertEqual(bot.manage_actions(156.6, [self.order], []), [("cancel", 1, None)])

    def test_keep_order_above_line(self):
        self.assertEqual(bot.manage_actions(158.0, [self.order], []), [])

    def test_breakeven_after_tp1(self):
        p = {"ticket": 2, "magic": bot.MAGIC, "sl": 160.2, "tp": 155.5}
        self.assertEqual(bot.manage_actions(156.9, [], [p]),
                         [("move_sl", 2, (159.2, 155.5))])

    def test_ignores_manual_positions(self):
        p = {"ticket": 3, "magic": 0, "sl": 160.2, "tp": 155.5}
        self.assertEqual(bot.manage_actions(150.0, [], [p]), [])


class CommandTest(unittest.TestCase):
    def test_dry_run_sends_nothing(self):
        mt5 = FakeMT5()
        bot.cmd_plan(mt5, mt5.acc, Namespace(execute=False))
        self.assertEqual(mt5.sent, [])

    def test_execute_sends_two_sell_limits(self):
        mt5 = FakeMT5()
        bot.cmd_plan(mt5, mt5.acc, Namespace(execute=True))
        self.assertEqual(len(mt5.sent), 2)
        for r, tp in zip(mt5.sent, (157.0, 155.5)):
            self.assertEqual(r["action"], mt5.TRADE_ACTION_PENDING)
            self.assertEqual(r["type"], mt5.ORDER_TYPE_SELL_LIMIT)
            self.assertEqual((r["price"], r["sl"], r["tp"], r["volume"]),
                             (159.2, 160.2, tp, 0.01))
            self.assertEqual(r["magic"], bot.MAGIC)

    def test_retries_other_filling_mode(self):
        mt5 = FakeMT5()
        mt5.reject_return = True
        bot.cmd_plan(mt5, mt5.acc, Namespace(execute=True))
        self.assertEqual([r["type_filling"] for r in mt5.sent],
                         [2, 0, 2, 0])

    def test_refuses_duplicate(self):
        mt5 = FakeMT5(positions=[pos(9, 160.2)])
        with self.assertRaises(SystemExit):
            bot.cmd_plan(mt5, mt5.acc, Namespace(execute=True))
        self.assertEqual(mt5.sent, [])

    def test_refuses_below_cancel_line(self):
        mt5 = FakeMT5(bid=156.5)
        with self.assertRaises(SystemExit):
            bot.cmd_plan(mt5, mt5.acc, Namespace(execute=True))

    def test_manage_moves_stop(self):
        mt5 = FakeMT5(bid=156.9, positions=[pos(7, 160.2)])
        bot.cmd_manage(mt5, mt5.acc, Namespace(execute=True))
        self.assertEqual(mt5.sent[0]["action"], mt5.TRADE_ACTION_SLTP)
        self.assertEqual((mt5.sent[0]["position"], mt5.sent[0]["sl"]), (7, 159.2))


if __name__ == "__main__":
    unittest.main()
