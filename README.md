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

## テスト
```
python -m unittest discover -s tests
```

投資は自己責任です。まずデモ口座 (practice) で動作を確認してください。
