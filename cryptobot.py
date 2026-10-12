#!/usr/bin/env python3
"""MEXC 無期限先物 (USDT-M) でアルトコインを売買するトレンドフォロー・ボット.

コマンド:
  status    口座・ポジション・各銘柄の相場状態とシグナルを表示
  backtest  過去データで戦略を検証 (手数料・スリッページ込み、APIキー不要)
  run       1サイクル実行: リスク判定 → シグナル → 発注 (--execute で実際に発注)
  halt      キルスイッチを入れる (以後の新規エントリーを全停止)
  resume    キルスイッチを解除する

戦略 (判断はすべてコードで決定的に行う):
  - 相場状態: ボラティリティが平常の CRISIS_MULT 倍以上なら crisis (新規禁止)、
    EMA50 と EMA200 の乖離が TREND_GAP 以上なら trend、それ以外は range (見送り)
  - エントリー: trend 中に直近 BREAKOUT 本の高値/安値を終値で抜けたら順張り
  - 損切り: ATR × STOP_ATR / 利確: 損切り幅 × TAKE_R (取引所側に同時に設定)

安全装置 (モデルや設定ミスで上書きされないようコードで強制):
  - 既定はドライラン。発注には --execute と --live-ok の両方が必要
    (MEXC 先物にはテストネットがないため、--execute は常に本番)
  - 1トレードの損失は残高の RISK_PCT (既定2%) まで。レバレッジは MAX_LEVERAGE まで
  - 1日の損失が DAILY_LOSS_PCT を超えたら当日は新規エントリーしない
  - 最高値からのドローダウンが MAX_DD_PCT を超えたら全決済して停止 (resume まで再開しない)
  - 同時保有は MAX_POSITIONS 銘柄まで、ロスカット価格が損切りより遠いことを確認
"""
import argparse
import datetime as dt
import json
import math
import os
import sys


def _env(name, default, cast=float):
    return cast(os.environ.get(name, default))


CONFIG = {
    "symbols": [s.strip() for s in os.environ.get(
        "CRYPTO_SYMBOLS", "ETH/USDT:USDT,SOL/USDT:USDT,XRP/USDT:USDT").split(",") if s.strip()],
    "timeframe": os.environ.get("CRYPTO_TIMEFRAME", "4h"),
    "risk_pct": _env("RISK_PCT", "0.02"),
    "max_leverage": _env("MAX_LEVERAGE", "3", int),
    "max_positions": _env("MAX_POSITIONS", "2", int),
    "daily_loss_pct": _env("DAILY_LOSS_PCT", "0.05"),
    "max_dd_pct": _env("MAX_DD_PCT", "0.15"),
    "fee": _env("FEE_RATE", "0.0004"),        # 片道の手数料率 (自分の口座の料率に合わせる)
    "slippage": _env("SLIPPAGE", "0.0005"),   # 成行の滑り (片道)
    "ema_fast": 50,
    "ema_slow": 200,
    "atr_n": 14,
    "breakout": 20,
    "trend_gap": 0.02,
    "crisis_mult": 2.5,
    "stop_atr": 2.0,
    "take_r": 2.0,
}

STATE_FILE = os.environ.get("CRYPTO_STATE", "cryptobot_state.json")
TAG = "cryptobot"
LIVE_BARS = 399  # 本番で判断に使う確定足の本数 (バックテストも同じ窓で判断する)


# ---------- テクニカル ----------

def ema(values, n):
    if len(values) < n:
        return None
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def atr_series(candles, n):
    """Wilder の ATR を各足について返す (計算できない足は None)."""
    trs = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c["h"] - c["l"])
        else:
            pc = candles[i - 1]["c"]
            trs.append(max(c["h"] - c["l"], abs(c["h"] - pc), abs(c["l"] - pc)))
    out = [None] * len(candles)
    if len(trs) < n:
        return out
    a = sum(trs[:n]) / n
    out[n - 1] = a
    for i in range(n, len(trs)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


def regime(candles, cfg=CONFIG):
    """直近の確定足までで相場状態を判定する."""
    closes = [c["c"] for c in candles]
    need = max(cfg["ema_slow"], 100 + cfg["atr_n"])
    if len(candles) < need:
        return {"regime": None, "reason": f"足が不足 ({len(candles)}/{need})"}
    fast, slow = ema(closes, cfg["ema_fast"]), ema(closes, cfg["ema_slow"])
    atrs = atr_series(candles, cfg["atr_n"])
    atr = atrs[-1]
    hist = sorted(a / c for a, c in zip(atrs[-100:], closes[-100:]))
    atr_pct, base = atr / closes[-1], hist[len(hist) // 2]
    gap = (fast - slow) / closes[-1]
    if atr_pct >= base * cfg["crisis_mult"]:
        name = "crisis"
    elif abs(gap) >= cfg["trend_gap"]:
        name = "trend"
    else:
        name = "range"
    return {"regime": name, "direction": "long" if gap > 0 else "short",
            "ema_fast": fast, "ema_slow": slow, "atr": atr,
            "atr_pct": atr_pct, "atr_pct_median": base, "gap": gap}


def signal(candles, cfg=CONFIG):
    """確定足のリストから売買シグナルを返す. なければ None."""
    r = regime(candles, cfg)
    if r["regime"] != "trend":
        return None
    n = cfg["breakout"]
    prior = candles[-n - 1:-1]
    last = candles[-1]["c"]
    stop_dist = r["atr"] * cfg["stop_atr"]
    if r["direction"] == "long" and last > max(c["h"] for c in prior):
        side, stop, take = "buy", last - stop_dist, last + stop_dist * cfg["take_r"]
    elif r["direction"] == "short" and last < min(c["l"] for c in prior):
        side, stop, take = "sell", last + stop_dist, last - stop_dist * cfg["take_r"]
    else:
        return None
    return {"side": side, "entry": last, "stop": stop, "take": take}


# ---------- リスク ----------

def size_position(equity, entry, stop, cfg=CONFIG):
    """損切り時の損失が equity × risk_pct になる数量 (基軸通貨建て).

    1銘柄あたりの建玉金額は equity × max_leverage / max_positions まで。
    """
    per_unit = abs(entry - stop)
    if per_unit <= 0 or equity <= 0:
        return 0.0
    qty = equity * cfg["risk_pct"] / per_unit
    cap = equity * cfg["max_leverage"] / cfg["max_positions"] / entry
    return min(qty, cap)


def to_contracts(qty, contract_size, step, min_contracts):
    """数量を契約数に変換し、刻みで切り捨てる. 最小未満なら 0."""
    n = math.floor(qty / contract_size / step + 1e-9) * step
    return n if n >= min_contracts else 0


def stop_inside_liquidation(entry, stop, leverage):
    """分離マージンのロスカット (おおよそ 1/レバレッジ の逆行) より手前に損切りがあるか."""
    return abs(entry - stop) / entry < 0.5 / leverage


def update_state(state, equity, today):
    s = dict(state)
    if s.get("day") != today:
        s["day"], s["day_start_equity"] = today, equity
    s["peak_equity"] = max(s.get("peak_equity", equity), equity)
    return s


def risk_gate(state, equity, n_open, cfg=CONFIG):
    """新規エントリー可否を判定する. (可否, 理由, 全決済すべきか) を返す."""
    if state.get("halted"):
        return False, "キルスイッチ作動中 (resume で解除)", False
    dd = 1 - equity / state["peak_equity"]
    if dd >= cfg["max_dd_pct"]:
        return False, f"最大ドローダウン {dd:.1%} ≥ {cfg['max_dd_pct']:.0%}: 全決済して停止", True
    day_loss = 1 - equity / state["day_start_equity"]
    if day_loss >= cfg["daily_loss_pct"]:
        return False, f"本日の損失 {day_loss:.1%} ≥ {cfg['daily_loss_pct']:.0%}: 本日は新規停止", False
    if n_open >= cfg["max_positions"]:
        return False, f"同時保有の上限 ({cfg['max_positions']})", False
    return True, "OK", False


def load_state(path=STATE_FILE):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state, path=STATE_FILE):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


# ---------- バックテスト ----------

def backtest(candles, cfg=CONFIG, equity=100.0, lot=0.0, min_lot=0.0):
    """シグナルを次の足の始値で約定させて検証する.

    lot / min_lot を指定すると、数量を取引所の刻み (基軸通貨建て) で切り捨て、
    最小数量に満たないシグナルは見送る (少額口座での実際の動きに近づける)。
    同じ足で損切りと利確の両方に届いた場合は損切りを優先 (保守的)。
    資金調達率 (funding) は含まない。
    """
    start, peak, max_dd = equity, equity, 0.0
    trades, pos, skipped = [], None, 0
    warm = max(cfg["ema_slow"], 100 + cfg["atr_n"])
    cost = cfg["fee"] + cfg["slippage"]
    for i in range(warm, len(candles)):
        bar = candles[i]
        if pos:
            long = pos["side"] == "buy"
            hit_stop = bar["l"] <= pos["stop"] if long else bar["h"] >= pos["stop"]
            hit_take = bar["h"] >= pos["take"] if long else bar["l"] <= pos["take"]
            if hit_stop or hit_take:
                px = pos["stop"] if hit_stop else pos["take"]
                pnl = (px - pos["entry"]) * pos["qty"] * (1 if long else -1)
                pnl -= (pos["entry"] + px) * pos["qty"] * cost
                equity += pnl
                trades.append({"side": pos["side"], "entry": pos["entry"], "exit": px,
                               "pnl": pnl, "win": pnl > 0})
                pos = None
        if pos is None and i + 1 < len(candles):
            sig = signal(candles[max(0, i + 1 - LIVE_BARS):i + 1], cfg)
            if sig:
                fill = candles[i + 1]["o"] * (1 + cfg["slippage"] * (1 if sig["side"] == "buy" else -1))
                shift = fill - sig["entry"]
                stop, take = sig["stop"] + shift, sig["take"] + shift
                qty = size_position(equity, fill, stop, cfg)
                if lot:
                    qty = to_contracts(qty, lot, 1, max(1, round(min_lot / lot))) * lot
                if not stop_inside_liquidation(fill, stop, cfg["max_leverage"]):
                    qty = 0
                if qty <= 0:
                    skipped += 1
                else:
                    pos = {"side": sig["side"], "entry": fill, "stop": stop,
                           "take": take, "qty": qty}
        peak = max(peak, equity)
        max_dd = max(max_dd, 1 - equity / peak)
    wins = [t["pnl"] for t in trades if t["win"]]
    losses = [-t["pnl"] for t in trades if not t["win"]]
    return {
        "trades": len(trades),
        "skipped": skipped,
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "profit_factor": sum(wins) / sum(losses) if losses else float("inf") if wins else 0.0,
        "return": equity / start - 1,
        "max_dd": max_dd,
        "final_equity": equity,
        "log": trades,
    }


# ---------- MEXC クライアント ----------

def to_candles(ohlcv):
    return [{"t": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4]} for r in ohlcv]


def exchange(keys=True):
    import ccxt
    opts = {"enableRateLimit": True, "options": {"defaultType": "swap"}}
    if keys:
        key, secret = os.environ.get("MEXC_API_KEY"), os.environ.get("MEXC_API_SECRET")
        if not key or not secret:
            sys.exit("環境変数 MEXC_API_KEY と MEXC_API_SECRET を設定してください。")
        opts.update(apiKey=key, secret=secret)
    ex = ccxt.mexc(opts)
    ex.load_markets()
    return ex


def closed_candles(ex, symbol, timeframe, limit=LIVE_BARS + 1):
    """確定足だけを返す (最後の足は形成中なので捨てる)."""
    return to_candles(ex.fetch_ohlcv(symbol, timeframe, limit=limit))[:-1]


def fetch_history(ex, symbol, timeframe, bars):
    ms = ex.parse_timeframe(timeframe) * 1000
    since = ex.milliseconds() - ms * (bars + 1)
    rows = []
    while since < ex.milliseconds() - ms:
        batch = ex.fetch_ohlcv(symbol, timeframe, since=since, limit=500)
        if not batch:
            break
        rows += [r for r in batch if not rows or r[0] > rows[-1][0]]
        since = batch[-1][0] + ms
    return to_candles(rows)[:-1]


def equity_usdt(ex):
    bal = ex.fetch_balance({"type": "swap"})
    return float(bal.get("total", {}).get("USDT") or 0)


def open_positions(ex, symbols):
    return [p for p in ex.fetch_positions(symbols) if float(p.get("contracts") or 0) > 0]


def place_entry(ex, symbol, sig, contracts, leverage):
    return ex.create_order(symbol, "market", sig["side"], contracts, None, {
        "marginMode": "isolated",
        "leverage": leverage,
        "stopLossPrice": float(ex.price_to_precision(symbol, sig["stop"])),
        "takeProfitPrice": float(ex.price_to_precision(symbol, sig["take"])),
        "clientOrderId": f"{TAG}-{ex.milliseconds()}",
    })


def close_position(ex, pos):
    side = "sell" if pos["side"] == "long" else "buy"
    return ex.create_order(pos["symbol"], "market", side, float(pos["contracts"]), None,
                           {"reduceOnly": True, "marginMode": pos.get("marginMode") or "isolated",
                            "leverage": int(float(pos.get("leverage") or CONFIG["max_leverage"]))})


# ---------- CLI ----------

def describe(symbol, candles):
    r = regime(candles)
    if r["regime"] is None:
        return f"{symbol}: {r['reason']}"
    s = signal(candles)
    line = (f"{symbol}: 終値 {candles[-1]['c']:.6g}  状態 {r['regime']} ({r['direction']})  "
            f"EMA乖離 {r['gap']:+.1%}  ATR {r['atr_pct']:.2%} (平常 {r['atr_pct_median']:.2%})")
    if s:
        line += f"\n  シグナル {s['side']} @ {s['entry']:.6g}  SL {s['stop']:.6g}  TP {s['take']:.6g}"
    return line


def cmd_status(args):
    ex = exchange()
    eq = equity_usdt(ex)
    state = load_state()
    print(f"証拠金残高 {eq:,.2f} USDT  最高値 {state.get('peak_equity', eq):,.2f}"
          + ("  [停止中]" if state.get("halted") else ""))
    for p in open_positions(ex, CONFIG["symbols"]):
        print(f"建玉 {p['symbol']} {p['side']} {p['contracts']}枚 @ {p.get('entryPrice')} "
              f"損益 {float(p.get('unrealizedPnl') or 0):,.2f}")
    for sym in CONFIG["symbols"]:
        print(describe(sym, closed_candles(ex, sym, CONFIG["timeframe"])))


def cmd_backtest(args):
    ex = exchange(keys=False)
    for sym in CONFIG["symbols"]:
        m = ex.market(sym)
        lot = (m.get("contractSize") or 1) * (m["precision"]["amount"] or 1)
        min_lot = (m.get("contractSize") or 1) * ((m["limits"]["amount"] or {}).get("min") or 1)
        res = backtest(fetch_history(ex, sym, CONFIG["timeframe"], args.bars),
                       equity=args.equity, lot=lot, min_lot=min_lot)
        print(f"{sym}: 取引 {res['trades']}回 (最小数量未満で見送り {res['skipped']}回)  "
              f"勝率 {res['win_rate']:.0%}  PF {res['profit_factor']:.2f}  "
              f"{args.equity:,.0f} → {res['final_equity']:,.2f} USDT ({res['return']:+.1%})  "
              f"最大DD {res['max_dd']:.1%}  [最小 {min_lot:g} / 刻み {lot:g}]")
    print(f"(時間足 {CONFIG['timeframe']} / 直近 {args.bars} 本 / 手数料 {CONFIG['fee']:.2%} "
          f"+ 滑り {CONFIG['slippage']:.2%} 片道 / funding は含まない)")


def cmd_run(args):
    if args.execute and not args.live_ok:
        sys.exit("MEXC 先物にテストネットはありません。本番発注には --live-ok も付けてください。")
    ex = exchange()
    eq = equity_usdt(ex)
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    state = update_state(load_state(), eq, today)
    positions = open_positions(ex, CONFIG["symbols"])
    ok, reason, flatten = risk_gate(state, eq, len(positions))
    print(f"[{today}] 残高 {eq:,.2f} USDT  建玉 {len(positions)}  リスク判定: {reason}")
    if flatten:
        for p in positions:
            print(f"全決済: {p['symbol']} {p['side']} {p['contracts']}枚")
            if args.execute:
                close_position(ex, p)
        if args.execute:
            state["halted"] = True
    if args.execute:
        save_state(state)
    if not ok:
        return
    held = {p["symbol"] for p in positions}
    slots = CONFIG["max_positions"] - len(positions)
    for sym in CONFIG["symbols"]:
        if slots <= 0:
            break
        if sym in held:
            continue
        sig = signal(closed_candles(ex, sym, CONFIG["timeframe"]))
        if not sig:
            print(f"{sym}: シグナルなし")
            continue
        lev = CONFIG["max_leverage"]
        if not stop_inside_liquidation(sig["entry"], sig["stop"], lev):
            print(f"{sym}: 損切り幅がロスカットに近すぎるため見送り")
            continue
        m = ex.market(sym)
        qty = size_position(eq, sig["entry"], sig["stop"])
        step = m["precision"]["amount"] or 1
        contracts = to_contracts(qty, m.get("contractSize") or 1, step,
                                 (m["limits"]["amount"] or {}).get("min") or step)
        if not contracts:
            print(f"{sym}: 最小枚数に満たないため見送り")
            continue
        size = contracts * (m.get("contractSize") or 1)
        loss = size * abs(sig["entry"] - sig["stop"])
        print(f"{sym}: {sig['side']} {contracts}枚 ({size:.6g}) @ ~{sig['entry']:.6g}  "
              f"SL {sig['stop']:.6g}  TP {sig['take']:.6g}  "
              f"損切り時 約 {loss:,.2f} USDT ({loss / eq:.1%})  レバ上限 {lev}x 分離")
        if args.execute:
            res = place_entry(ex, sym, sig, contracts, lev)
            print(f"  発注しました: {res.get('id')}")
        slots -= 1
    if not args.execute:
        print("ドライランです。発注するには --execute --live-ok を付けてください。")


def cmd_halt(args, value=True):
    state = load_state()
    state["halted"] = value
    if not value:
        state.pop("peak_equity", None)  # 再開時は現在の残高を新しい最高値とする
    save_state(state)
    print("キルスイッチを入れました。" if value else "キルスイッチを解除しました。")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["status", "backtest", "run", "halt", "resume"])
    p.add_argument("--execute", action="store_true", help="実際に発注する")
    p.add_argument("--live-ok", action="store_true", help="本番口座での発注を許可")
    p.add_argument("--bars", type=int, default=3000, help="backtest に使う足の本数")
    p.add_argument("--equity", type=float, default=100.0, help="backtest の初期資金 (USDT)")
    args = p.parse_args(argv)
    {"status": cmd_status, "backtest": cmd_backtest, "run": cmd_run,
     "halt": cmd_halt, "resume": lambda a: cmd_halt(a, False)}[args.command](args)


if __name__ == "__main__":
    main()
