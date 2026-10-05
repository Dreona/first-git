@echo off
rem ひよりん便: 予約フォルダのうち、時刻を過ぎたものをDiscordへ送る。
rem タスク スケジューラから15分おきに動かす用。結果は 送信ログ.txt に追記される。
chcp 65001 > nul
set PYTHONUTF8=1
cd /d "%~dp0"
echo ==== %date% %time% ==== >> 送信ログ.txt
python discord_send.py --auto "%~dp0予約" >> 送信ログ.txt 2>&1
