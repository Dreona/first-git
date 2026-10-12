# fxbot — USD/JPY 戻り売りボット (OANDA v20 API)

資金10万円、1トレードのリスクは2% (2,000円) で運用します。

| 項目 | 値 |
|---|---|
| 通貨ペア | USD/JPY 売り |
| エントリー | 指値 159.200 |
| 損切り | 160.200 |
| 利確 | 157.000 (半分) / 155.500 (残り) |
| 取消 | 約定前に 156.700 を割ったら取消 |
| 利確①後 | 残りの損切りを建値 159.200 に移動 |

## セットアップ
```
pip install -r requirements.txt
export OANDA_API_TOKEN=...     # OANDA の API トークン
export OANDA_ACCOUNT_ID=...    # 例: 101-009-XXXXXXX-001
export OANDA_ENV=practice      # 既定。本番は live (+ --live-ok が必要)
```

## 使い方
```
python fxbot.py status              # 口座・レート・テクニカル
python fxbot.py plan                # 注文内容の確認 (ドライラン)
python fxbot.py plan --execute      # 発注
python fxbot.py manage --execute    # 取消判定・建値移動 (定期実行する)
```
`manage` は cron などで 5〜15分ごとに実行してください。

## HFM (MetaTrader 5) 版 — `fxbot_mt5.py`

Windows で MT5 ターミナルを起動し、HFM の口座にログインした状態で実行します。
プランは OANDA 版と共通です (損切り時の損失は残高の2%まで、ロットは自動計算)。

```
pip install MetaTrader5
set MT5_SYMBOL=USDJPY          # 口座タイプにより接尾辞付き (MT5 の気配値表示で確認)
python fxbot_mt5.py status
python fxbot_mt5.py plan                     # ドライラン
python fxbot_mt5.py plan --execute           # デモ口座で発注
python fxbot_mt5.py plan --execute --live-ok # 本番口座で発注
python fxbot_mt5.py manage --execute         # タスクスケジューラで15分ごとに実行
```
ログイン済みの MT5 に接続するので、パスワードの環境変数は不要です
(別の口座に切り替える場合のみ `MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER`)。

## MEXC 無期限先物 (アルトコイン) 版 — `cryptobot.py`

ETH / SOL / XRP などの USDT-M 無期限先物を、トレンドフォローで売買します。
判断はすべてコードで決定的に行い、AI やモデルが売買判断やリスク上限を上書きすることはありません。

| 項目 | 既定値 (環境変数で変更可) |
|---|---|
| 銘柄 | `CRYPTO_SYMBOLS=ETH/USDT:USDT,SOL/USDT:USDT,XRP/USDT:USDT` |
| 時間足 | `CRYPTO_TIMEFRAME=4h` |
| 相場状態 | ATR が平常の2.5倍以上 → crisis (新規禁止) / EMA50-200 乖離2%以上 → trend / それ以外 → range (見送り) |
| エントリー | trend 中に直近20本の高値 (安値) を終値で抜けたら順張り |
| 損切り / 利確 | ATR×2 / 損切り幅の2倍 (発注時に取引所側へ同時設定) |
| 1トレードのリスク | `RISK_PCT=0.02` (残高の2%) |
| レバレッジ | `MAX_LEVERAGE=3` (分離マージン) |
| 同時保有 | `MAX_POSITIONS=2` |
| 1日の損失上限 | `DAILY_LOSS_PCT=0.05` → 当日は新規停止 |
| 最大ドローダウン | `MAX_DD_PCT=0.15` → 全決済して停止 (`resume` まで再開しない) |
| コスト | `FEE_RATE=0.0004` / `SLIPPAGE=0.0005` (片道。自分の手数料率に合わせる) |

```
pip install -r requirements.txt
export MEXC_API_KEY=...        # 先物取引の権限のみ付与 (出金権限は付けない、IP 制限推奨)
export MEXC_API_SECRET=...

python cryptobot.py backtest --bars 3000      # 過去データで検証 (APIキー不要)
python cryptobot.py status                    # 残高・建玉・相場状態・シグナル
python cryptobot.py run                       # ドライラン (発注内容の確認のみ)
python cryptobot.py run --execute --live-ok   # 本番発注 (cron で時間足ごとに実行)
python cryptobot.py halt                      # キルスイッチ (新規エントリー停止)
python cryptobot.py resume                    # 解除
```

注意:
- MEXC 先物にはテストネットがないため、`--execute` は常に本番です。まず `backtest` と
  ドライランで確認し、最初は少額で動かしてください。
- MEXC の先物 API は 2022 年から発注が停止されていた期間があります。2026-03 に再開と
  公式の更新履歴にありますが、自分の口座で発注できるか少額で確認してください。
- バックテストは資金調達率 (funding) を含みません。
- MEXC は日本の金融庁に登録された暗号資産交換業者ではありません。

## テスト
```
python -m unittest discover -s tests
```

投資は自己責任です。まずデモ口座 (practice) で動作を確認してください。
