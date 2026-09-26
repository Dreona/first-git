#!/usr/bin/env python3
"""USD/JPY 戻り売りプランを MetaTrader 5 (HFM など) で執行するボット.

Windows 上で MT5 ターミナルを起動・ログインした状態で実行します。
プラン (エントリー・損切り・利確・取消ライン) は fxbot.PLAN を共有します。

コマンド:
  status   口座・レート・注文・ポジション・テクニカル状況を表示
  plan     プラン通りの注文を計算して表示 (--execute で実際に発注)
  manage   注文の取消判定と、利確①後の損切りの建値移動 (--execute で反映)

安全装置:
  - 既定はドライラン。--execute を付けたときだけ注文を送る
  - デモ口座以外では --live-ok が必要
  - 1トレードの損失は口座残高の RISK_PCT (既定2%) を超えない。
    ハイレバレッジ口座でも、数量は損切り幅から逆算した分しか建てない
"""
import argparse
import math
import os
import sys

from fxbot import PLAN, TAG, technicals

MAGIC = 20260926
SYMBOL = os.environ.get("MT5_SYMBOL", "USDJPY")  # 口座タイプで接尾辞が付く場合は変更


# ---------- 純粋ロジック (テスト対象) ----------

def size_lots(balance, risk_pct, loss_per_lot, vol_min, vol_step, n_splits):
    """損切り時の損失が balance*risk_pct 以内になるロット数を、分割数ごとに返す."""
    if loss_per_lot <= 0:
        return 0.0
    per_split = balance * risk_pct / loss_per_lot / n_splits
    lots = math.floor(per_split / vol_step + 1e-9) * vol_step
    lots = round(lots, 8)
    return lots if lots >= vol_min else 0.0


def build_requests(lots, digits, plan=PLAN, symbol=SYMBOL):
    """MT5 の order_send に渡すリクエスト (定数は名前のまま) を返す."""
    fmt = lambda p: round(p, digits)
    kind = "ORDER_TYPE_SELL_LIMIT" if plan["side"] == "sell" else "ORDER_TYPE_BUY_LIMIT"
    return [{
        "action": "TRADE_ACTION_PENDING",
        "symbol": symbol,
        "volume": lots,
        "type": kind,
        "price": fmt(plan["entry"]),
        "sl": fmt(plan["stop"]),
        "tp": fmt(tp),
        "magic": MAGIC,
        "comment": TAG,
        "type_time": "ORDER_TIME_GTC",
    } for tp in plan["targets"]]


def manage_actions(price, orders, positions, plan=PLAN):
    """orders/positions は dict(ticket, magic, sl, tp) のリスト."""
    ours_o = [o for o in orders if o["magic"] == MAGIC]
    ours_p = [p for p in positions if p["magic"] == MAGIC]
    acts = []
    if not ours_p and ours_o and price < plan["cancel_below"]:
        acts += [("cancel", o["ticket"], None) for o in ours_o]
    if price <= plan["targets"][0]:
        for p in ours_p:
            if p["sl"] > plan["entry"]:
                acts.append(("move_sl", p["ticket"], (plan["entry"], p["tp"])))
    return acts


# ---------- MT5 接続 ----------

def connect(live_ok):
    try:
        import MetaTrader5 as mt5
    except ImportError:
        sys.exit("pip install MetaTrader5 を実行してください (Windows のみ対応)。")
    kw = {}
    if os.environ.get("MT5_PATH"):
        kw["path"] = os.environ["MT5_PATH"]
    if os.environ.get("MT5_LOGIN"):
        kw.update(login=int(os.environ["MT5_LOGIN"]),
                  password=os.environ.get("MT5_PASSWORD", ""),
                  server=os.environ.get("MT5_SERVER", ""))
    if not mt5.initialize(**kw):
        sys.exit(f"MT5 に接続できません: {mt5.last_error()}")
    acc = mt5.account_info()
    demo = acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO
    print(f"[{acc.server} / #{acc.login} / {'デモ' if demo else '本番'} / {acc.currency}]")
    if not demo and not live_ok:
        mt5.shutdown()
        sys.exit("本番口座です。実行するには --live-ok を付けてください。")
    if not mt5.symbol_select(SYMBOL, True):
        mt5.shutdown()
        sys.exit(f"銘柄 {SYMBOL} が見つかりません。MT5_SYMBOL を口座の表記に合わせてください。")
    return mt5, acc


def snapshot(mt5):
    orders = [{"ticket": o.ticket, "magic": o.magic, "sl": o.sl, "tp": o.tp,
               "volume": o.volume_current, "price": o.price_open}
              for o in (mt5.orders_get(symbol=SYMBOL) or [])]
    positions = [{"ticket": p.ticket, "magic": p.magic, "sl": p.sl, "tp": p.tp,
                  "volume": p.volume, "price": p.price_open, "profit": p.profit}
                 for p in (mt5.positions_get(symbol=SYMBOL) or [])]
    return orders, positions


def resolve(mt5, req):
    """定数名を MT5 の値に置き換え、口座が許す約定方式を付ける."""
    out = dict(req)
    for k in ("action", "type", "type_time"):
        out[k] = getattr(mt5, req[k])
    out["type_filling"] = mt5.ORDER_FILLING_RETURN
    return out


def send(mt5, req):
    res = mt5.order_send(req)
    # 業者が RETURN を受け付けない場合だけ、約定方式を変えて再送
    for filling in (mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC):
        if res is None or res.retcode != mt5.TRADE_RETCODE_INVALID_FILL:
            break
        res = mt5.order_send(dict(req, type_filling=filling))
    if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
        raise RuntimeError(f"注文失敗: {res.comment if res else mt5.last_error()}")
    return res


# ---------- コマンド ----------

def cmd_status(mt5, acc, args):
    tick = mt5.symbol_info_tick(SYMBOL)
    rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_D1, 1, 100)
    if rates is None:
        rates = []
    t = technicals([{"o": r["open"], "h": r["high"], "l": r["low"], "c": r["close"]}
                    for r in rates])
    print(f"残高 {acc.balance:,.2f} {acc.currency}  有効証拠金 {acc.equity:,.2f}  "
          f"レバレッジ {acc.leverage}倍")
    print(f"{SYMBOL} bid {tick.bid} / ask {tick.ask}")
    if t["sma25"]:
        print(f"SMA25 {t['sma25']:.3f}  SMA75 {t['sma75'] or 0:.3f}  トレンド {t['trend']}")
    print(f"直近の足: {t['pattern'] or '反転シグナルなし'}")
    orders, positions = snapshot(mt5)
    for o in orders:
        print(f"注文 #{o['ticket']} {o['volume']}lot @ {o['price']} SL {o['sl']} TP {o['tp']}")
    for p in positions:
        print(f"建玉 #{p['ticket']} {p['volume']}lot @ {p['price']} 損益 {p['profit']:,.2f}")


def cmd_plan(mt5, acc, args):
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick.bid < PLAN["cancel_below"]:
        sys.exit(f"現在値 {tick.bid} が取消ライン {PLAN['cancel_below']} を下回っています。発注しません。")
    orders, positions = snapshot(mt5)
    if any(x["magic"] == MAGIC for x in orders + positions):
        sys.exit("fxbot の注文・建玉がすでにあります。二重発注はしません。")
    info = mt5.symbol_info(SYMBOL)
    order_type = mt5.ORDER_TYPE_SELL if PLAN["side"] == "sell" else mt5.ORDER_TYPE_BUY
    pnl = mt5.order_calc_profit(order_type, SYMBOL, 1.0, PLAN["entry"], PLAN["stop"])
    if pnl is None:
        sys.exit(f"損失額を計算できません: {mt5.last_error()}")
    loss_per_lot = abs(pnl)
    n = len(PLAN["targets"])
    lots = size_lots(acc.balance, PLAN["risk_pct"], loss_per_lot,
                     info.volume_min, info.volume_step, n)
    if lots <= 0:
        sys.exit("残高に対して最小ロット未満になるため発注しません。")
    reqs = build_requests(lots, info.digits)
    loss = loss_per_lot * lots * n
    print(f"残高 {acc.balance:,.2f} {acc.currency} / 現在値 {tick.bid}")
    print(f"合計 {lots * n:.2f} lot  損切り時の損失 約 {loss:,.2f} {acc.currency} "
          f"({loss / acc.balance:.1%})")
    for r in reqs:
        print(f"  売り指値 {r['volume']}lot @ {r['price']}  SL {r['sl']}  TP {r['tp']}")
    if not args.execute:
        print("ドライランです。発注するには --execute を付けてください。")
        return
    for r in reqs:
        res = send(mt5, resolve(mt5, r))
        print(f"発注しました: 注文 #{res.order} {r['volume']}lot @ {r['price']}")


def cmd_manage(mt5, acc, args):
    tick = mt5.symbol_info_tick(SYMBOL)
    orders, positions = snapshot(mt5)
    acts = manage_actions(tick.ask, orders, positions)
    if not acts:
        print(f"現在値 {tick.ask}: 対応不要")
        return
    for kind, ticket, detail in acts:
        if kind == "cancel":
            print(f"注文取消: #{ticket} (約定前に {PLAN['cancel_below']} を下抜け)")
            req = {"action": mt5.TRADE_ACTION_REMOVE, "order": ticket}
        else:
            print(f"損切りを {detail[0]} に移動: #{ticket}")
            req = {"action": mt5.TRADE_ACTION_SLTP, "position": ticket,
                   "symbol": SYMBOL, "sl": detail[0], "tp": detail[1]}
        if args.execute:
            send(mt5, req)
    if not args.execute:
        print("ドライランです。反映するには --execute を付けてください。")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["status", "plan", "manage"])
    p.add_argument("--execute", action="store_true", help="実際に注文を送る")
    p.add_argument("--live-ok", action="store_true", help="本番口座での実行を許可")
    args = p.parse_args(argv)
    mt5, acc = connect(args.live_ok)
    try:
        {"status": cmd_status, "plan": cmd_plan, "manage": cmd_manage}[args.command](mt5, acc, args)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
