#!/usr/bin/env python3
"""USD/JPY 戻り売りプランを OANDA v20 API で執行するボット.

コマンド:
  status   口座・レート・注文・ポジション・テクニカル状況を表示
  plan     プラン通りの注文を計算して表示 (--execute で実際に発注)
  manage   注文の取消判定と、利確①後の損切りの建値移動 (--execute で反映)

安全装置:
  - 既定はデモ口座 (practice)。本番は OANDA_ENV=live かつ --live-ok が必要
  - 既定はドライラン。--execute を付けたときだけ API に書き込む
  - 1トレードの損失は口座残高の RISK_PCT (既定2%) を超えない
"""
import argparse
import os
import sys

import requests

HOSTS = {
    "practice": "https://api-fxpractice.oanda.com",
    "live": "https://api-fxtrade.oanda.com",
}

TAG = "fxbot"

# トレードプラン (2026-09-26 作成)
PLAN = {
    "instrument": "USD_JPY",
    "side": "sell",
    "entry": 159.200,
    "stop": 160.200,
    "targets": [157.000, 155.500],  # 数量を均等に分けて利確
    "cancel_below": 156.700,        # 約定前にここを割ったら注文取消
    "risk_pct": float(os.environ.get("RISK_PCT", "0.02")),
    "max_leverage": 25,
}


# ---------- テクニカル ----------

def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def candle_pattern(candles):
    """直近の確定足から弱気の反転パターンを判定する. candles は dict(o,h,l,c) のリスト."""
    if len(candles) < 3:
        return None
    a, b, c = candles[-3], candles[-2], candles[-1]
    body = abs(c["c"] - c["o"])
    upper = c["h"] - max(c["o"], c["c"])
    rng = c["h"] - c["l"]
    if (b["c"] > b["o"] and c["c"] < c["o"]
            and c["o"] >= b["c"] and c["c"] <= b["o"]):
        return "陰の包み足"
    if (a["c"] > a["o"] and abs(b["c"] - b["o"]) < abs(a["c"] - a["o"]) * 0.3
            and c["c"] < c["o"] and c["c"] < (a["o"] + a["c"]) / 2):
        return "宵の明星"
    if rng > 0 and upper >= body * 2 and min(c["o"], c["c"]) - c["l"] <= rng / 3:
        return "上ヒゲ (シューティングスター)"
    return None


def technicals(candles):
    closes = [x["c"] for x in candles]
    s25, s75 = sma(closes, 25), sma(closes, 75)
    trend = None
    if s25 is not None and s75 is not None:
        trend = "上昇" if s25 > s75 else "下降"
    return {"sma25": s25, "sma75": s75, "trend": trend,
            "pattern": candle_pattern(candles)}


# ---------- プラン計算 ----------

def size_units(balance, entry, stop, risk_pct, max_leverage):
    """損切り幅から逆算した通貨数 (1,000通貨単位で切り捨て)."""
    risk_jpy = balance * risk_pct
    per_unit = abs(stop - entry)
    units = int(risk_jpy / per_unit) // 1000 * 1000
    margin_cap = int(balance * max_leverage / entry) // 1000 * 1000
    return min(units, margin_cap)


def build_orders(balance, plan=PLAN):
    units = size_units(balance, plan["entry"], plan["stop"],
                       plan["risk_pct"], plan["max_leverage"])
    n = len(plan["targets"])
    lot = units // n // 1000 * 1000
    if lot <= 0:
        return []
    sign = -1 if plan["side"] == "sell" else 1
    orders = []
    for tp in plan["targets"]:
        orders.append({
            "type": "LIMIT",
            "instrument": plan["instrument"],
            "units": str(sign * lot),
            "price": f"{plan['entry']:.3f}",
            "timeInForce": "GTC",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": f"{plan['stop']:.3f}"},
            "takeProfitOnFill": {"price": f"{tp:.3f}"},
            "clientExtensions": {"tag": TAG},
            "tradeClientExtensions": {"tag": TAG},
        })
    return orders


def manage_actions(price, pending, trades, plan=PLAN):
    """現在値と注文・ポジションから、取るべき操作の一覧を返す."""
    actions = []
    ours_pending = [o for o in pending
                    if o.get("clientExtensions", {}).get("tag") == TAG]
    ours_trades = [t for t in trades
                   if t.get("clientExtensions", {}).get("tag") == TAG]
    if not ours_trades and ours_pending and price < plan["cancel_below"]:
        for o in ours_pending:
            actions.append(("cancel", o["id"],
                            f"約定前に {plan['cancel_below']} を下抜け"))
    if price <= plan["targets"][0]:
        for t in ours_trades:
            sl = float(t.get("stopLossOrder", {}).get("price", "inf"))
            if sl > plan["entry"]:
                actions.append(("move_sl", t["id"], f"{plan['entry']:.3f}"))
    return actions


# ---------- OANDA クライアント ----------

class Oanda:
    def __init__(self, token, account, env):
        self.base = HOSTS[env]
        self.account = account
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}",
                               "Content-Type": "application/json"})

    def _req(self, method, path, **kw):
        r = self.s.request(method, self.base + path, timeout=15, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text}")
        return r.json()

    def summary(self):
        return self._req("GET", f"/v3/accounts/{self.account}/summary")["account"]

    def price(self, instrument):
        p = self._req("GET", f"/v3/accounts/{self.account}/pricing",
                      params={"instruments": instrument})["prices"][0]
        return float(p["bids"][0]["price"]), float(p["asks"][0]["price"])

    def candles(self, instrument, granularity="D", count=100):
        data = self._req("GET", f"/v3/instruments/{instrument}/candles",
                         params={"granularity": granularity, "count": count,
                                 "price": "M"})["candles"]
        return [{k: float(c["mid"][k]) for k in "ohlc"}
                for c in data if c["complete"]]

    def pending(self):
        return self._req("GET", f"/v3/accounts/{self.account}/pendingOrders")["orders"]

    def trades(self):
        return self._req("GET", f"/v3/accounts/{self.account}/openTrades")["trades"]

    def place(self, order):
        return self._req("POST", f"/v3/accounts/{self.account}/orders",
                         json={"order": order})

    def cancel(self, order_id):
        return self._req("PUT", f"/v3/accounts/{self.account}/orders/{order_id}/cancel")

    def set_stop(self, trade_id, price):
        return self._req("PUT", f"/v3/accounts/{self.account}/trades/{trade_id}/orders",
                         json={"stopLoss": {"price": price}})


# ---------- CLI ----------

def client_from_env(live_ok):
    env = os.environ.get("OANDA_ENV", "practice")
    if env not in HOSTS:
        sys.exit(f"OANDA_ENV は practice か live: {env}")
    if env == "live" and not live_ok:
        sys.exit("本番口座です。実行するには --live-ok を付けてください。")
    token = os.environ.get("OANDA_API_TOKEN")
    account = os.environ.get("OANDA_ACCOUNT_ID")
    if not token or not account:
        sys.exit("環境変数 OANDA_API_TOKEN と OANDA_ACCOUNT_ID を設定してください。")
    print(f"[接続先: {env}]")
    return Oanda(token, account, env)


def cmd_status(api, args):
    acc = api.summary()
    bid, ask = api.price(PLAN["instrument"])
    t = technicals(api.candles(PLAN["instrument"]))
    print(f"残高 {float(acc['balance']):,.0f} {acc['currency']}  "
          f"評価損益 {float(acc['unrealizedPL']):,.0f}")
    print(f"{PLAN['instrument']} bid {bid:.3f} / ask {ask:.3f}")
    if t["sma25"]:
        print(f"SMA25 {t['sma25']:.3f}  SMA75 {t['sma75'] or 0:.3f}  トレンド {t['trend']}")
    print(f"直近の足: {t['pattern'] or '反転シグナルなし'}")
    for o in api.pending():
        print(f"注文 #{o['id']} {o['type']} {o.get('units')} @ {o.get('price')}")
    for tr in api.trades():
        print(f"建玉 #{tr['id']} {tr['currentUnits']} @ {tr['price']} "
              f"損益 {float(tr['unrealizedPL']):,.0f}")


def cmd_plan(api, args):
    balance = float(api.summary()["balance"])
    bid, _ = api.price(PLAN["instrument"])
    if bid < PLAN["cancel_below"]:
        sys.exit(f"現在値 {bid:.3f} が取消ライン {PLAN['cancel_below']} を下回っています。発注しません。")
    if any(o.get("clientExtensions", {}).get("tag") == TAG for o in api.pending()):
        sys.exit("fxbot の注文がすでにあります。二重発注はしません。")
    orders = build_orders(balance)
    if not orders:
        sys.exit("残高に対して数量が 1,000 通貨未満になるため発注しません。")
    total = sum(abs(int(o["units"])) for o in orders)
    loss = total * abs(PLAN["stop"] - PLAN["entry"])
    print(f"残高 {balance:,.0f} 円 / 現在値 {bid:.3f}")
    print(f"合計 {total:,} 通貨  損切り時の損失 約 {loss:,.0f} 円 "
          f"({loss / balance:.1%})")
    for o in orders:
        print(f"  指値 {o['units']} @ {o['price']}  "
              f"SL {o['stopLossOnFill']['price']}  TP {o['takeProfitOnFill']['price']}")
    if not args.execute:
        print("ドライランです。発注するには --execute を付けてください。")
        return
    for o in orders:
        res = api.place(o)
        tx = res.get("orderCreateTransaction", {})
        print(f"発注しました: #{tx.get('id')} {tx.get('units')} @ {tx.get('price')}")


def cmd_manage(api, args):
    bid, ask = api.price(PLAN["instrument"])
    acts = manage_actions(ask, api.pending(), api.trades())
    if not acts:
        print(f"現在値 {ask:.3f}: 対応不要")
        return
    for kind, ident, detail in acts:
        label = "注文取消" if kind == "cancel" else f"損切りを {detail} に移動"
        print(f"{label}: #{ident}" + (f" ({detail})" if kind == "cancel" else ""))
        if args.execute:
            api.cancel(ident) if kind == "cancel" else api.set_stop(ident, detail)
    if not args.execute:
        print("ドライランです。反映するには --execute を付けてください。")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["status", "plan", "manage"])
    p.add_argument("--execute", action="store_true", help="実際に API へ書き込む")
    p.add_argument("--live-ok", action="store_true", help="本番口座での実行を許可")
    args = p.parse_args(argv)
    api = client_from_env(args.live_ok)
    {"status": cmd_status, "plan": cmd_plan, "manage": cmd_manage}[args.command](api, args)


if __name__ == "__main__":
    main()
