"""ひよりん便 — 画像とお知らせ文を、プラン別のDiscordウェブフックへ送る道具。

使い方(くわしくは「使い方.txt」):
    python discord_send.py 送るフォルダ             # その場ですぐ送る
    python discord_send.py 送るフォルダ --dry-run   # 送らずに、何をどう送るかだけ表示
    python discord_send.py --auto 予約フォルダ      # 予約時刻を過ぎたフォルダを送る(タスク スケジューラ用)

送るフォルダの中身:
    gold/   画像 + お知らせ文.txt (任意)
    silver/ 画像 + お知らせ文.txt (任意)
    bronze/ 画像 + お知らせ文.txt (任意)
    お知らせ文.txt  (プランのフォルダに無いときに使う共通の文。任意)

追加のライブラリは不要 (Python 3.8 以上の標準機能だけで動く)。
"""

import argparse
import datetime as dt
import io
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "discord_webhooks.txt")
TEMPLATE_PATH = os.path.join(HERE, "discord_webhooks_ひな形.txt")

TIERS = ("gold", "silver", "bronze")
NOTICE_NAME = "お知らせ文.txt"
RECORD_NAME = "送信記録.json"
IMAGE_EXTS = (".png", ".jpg", ".jpeg")
MAX_FILES_PER_MESSAGE = 10   # Discordの決まり: 1投稿あたり最大10ファイル
MAX_CONTENT_CHARS = 2000     # Discordの決まり: 1投稿の文字数上限
MiB = 1024 * 1024

DEFAULTS = {
    "max_file_mb": "20",   # 1ファイルの上限 (2026-09-03時点・ブーストなし)
    "max_total_mb": "24",  # 1投稿の合計上限 (本当は25MB。文字分の余裕をみて24)
    "zip_tiers": "gold",   # ZIPにまとめて送るプラン (カンマ区切り。空なら全部画像のまま)
    "username": "",        # 投稿者名の上書き (空ならウェブフックの名前)
}


class SendError(Exception):
    pass


# ---------------------------------------------------------------- 設定

def load_config(path=CONFIG_PATH):
    if not os.path.exists(path):
        if os.path.exists(TEMPLATE_PATH):
            shutil.copyfile(TEMPLATE_PATH, path)
        raise SendError(
            f"設定ファイルを作りました: {path}\n"
            "ここにウェブフックURLを書いてから、もう一度実行してください。")
    cfg = dict(DEFAULTS)
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        cfg[key.strip().lower()] = value.strip()
    return cfg


def webhook_for(cfg, tier):
    url = cfg.get(tier, "")
    if not url:
        return None
    if not re.match(r"^https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$", url):
        raise SendError(f"{tier} のウェブフックURLの形が正しくありません。設定ファイルを確認してください。")
    return url


def read_text(path):
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise SendError(f"文字コードが読めません: {path} (UTF-8で保存してください)")


# ---------------------------------------------------------------- 生成情報の削除

# PNGで残してよい部分 (絵そのものと色の情報だけ)。文字の情報 (tEXt/iTXt/zTXt) やEXIFは消す。
PNG_KEEP = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"cHRM",
            b"sRGB", b"iCCP", b"sBIT", b"pHYs", b"bKGD"}
PNG_SIG = b"\x89PNG\r\n\x1a\n"


def strip_png(data):
    if not data.startswith(PNG_SIG):
        raise SendError("PNGファイルとして読めません")
    out = io.BytesIO()
    out.write(PNG_SIG)
    pos = len(PNG_SIG)
    while pos < len(data):
        if pos + 8 > len(data):
            raise SendError("PNGファイルが壊れています")
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        end = pos + 12 + length
        if ctype in PNG_KEEP:
            out.write(data[pos:end])
        pos = end
        if ctype == b"IEND":
            break
    return out.getvalue()


def strip_jpeg(data):
    if not data.startswith(b"\xff\xd8"):
        raise SendError("JPGファイルとして読めません")
    out = io.BytesIO()
    out.write(b"\xff\xd8")
    pos = 2
    while pos < len(data):
        if data[pos] != 0xFF:
            raise SendError("JPGファイルが壊れています")
        marker = data[pos + 1]
        if marker == 0xDA:  # ここから先は絵のデータ
            out.write(data[pos:])
            break
        if marker in (0x01,) or 0xD0 <= marker <= 0xD7:
            out.write(data[pos:pos + 2])
            pos += 2
            continue
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        seg = data[pos:pos + 2 + length]
        drop = (marker == 0xFE                                  # コメント
                or (0xE1 <= marker <= 0xEF                      # EXIF・XMPなど
                    and not (marker == 0xE2 and seg[4:16] == b"ICC_PROFILE\x00")
                    and marker != 0xEE))
        if not drop:
            out.write(seg)
        pos += 2 + length
    return out.getvalue()


def strip_metadata(src, dst):
    with open(src, "rb") as f:
        data = f.read()
    ext = os.path.splitext(src)[1].lower()
    clean = strip_png(data) if ext == ".png" else strip_jpeg(data)
    with open(dst, "wb") as f:
        f.write(clean)


# ---------------------------------------------------------------- 送る単位に分ける

def list_images(folder):
    names = sorted(n for n in os.listdir(folder)
                   if os.path.splitext(n)[1].lower() in IMAGE_EXTS
                   and not n.startswith("."))
    others = sorted(n for n in os.listdir(folder)
                    if os.path.isfile(os.path.join(folder, n))
                    and n != NOTICE_NAME and n not in names and not n.startswith("."))
    return [os.path.join(folder, n) for n in names], others


def group_by_size(paths, max_count, max_total):
    """ファイルを、件数とサイズの上限を超えないように順番のまま分ける。"""
    groups, cur, cur_size = [], [], 0
    for p in paths:
        size = os.path.getsize(p)
        if cur and (len(cur) >= max_count or cur_size + size > max_total):
            groups.append(cur)
            cur, cur_size = [], 0
        cur.append(p)
        cur_size += size
    if cur:
        groups.append(cur)
    return groups


def make_zips(paths, workdir, base_name, max_size):
    """画像をZIPにまとめる。上限を超えるときは、それぞれ単独で開けるZIPに分ける。"""
    # ZIPの見出し分の余裕として1ファイルあたり1KBをみる
    groups, cur, cur_size = [], [], 0
    for p in paths:
        size = os.path.getsize(p) + 1024
        if cur and cur_size + size > max_size:
            groups.append(cur)
            cur, cur_size = [], 0
        cur.append(p)
        cur_size += size
    if cur:
        groups.append(cur)
    zips = []
    for i, group in enumerate(groups, 1):
        name = f"{base_name}.zip" if len(groups) == 1 else f"{base_name}_{i}of{len(groups)}.zip"
        zpath = os.path.join(workdir, name)
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
            for p in group:
                z.write(p, os.path.basename(p))
        zips.append(zpath)
    return zips


def split_text(text, limit=MAX_CONTENT_CHARS):
    text = text.strip()
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    if text:
        parts.append(text)
    return parts


def build_plan(post_dir, tier, cfg, workdir):
    """1つのプランについて、送る投稿(文と添付ファイル)の並びを作る。"""
    tier_dir = os.path.join(post_dir, tier)
    images, others = list_images(tier_dir)
    warnings = [f"画像ではないので送りません: {n}" for n in others]

    notice_path = os.path.join(tier_dir, NOTICE_NAME)
    if not os.path.exists(notice_path):
        notice_path = os.path.join(post_dir, NOTICE_NAME)
    notice = read_text(notice_path) if os.path.exists(notice_path) else ""
    if not images and not notice.strip():
        return [], warnings

    max_file = int(float(cfg["max_file_mb"]) * MiB)
    max_total = int(float(cfg["max_total_mb"]) * MiB)

    clean_dir = os.path.join(workdir, tier)
    os.makedirs(clean_dir)
    cleaned = []
    for src in images:
        dst = os.path.join(clean_dir, os.path.basename(src))
        strip_metadata(src, dst)
        if os.path.getsize(dst) > max_file:
            raise SendError(
                f"{tier}/{os.path.basename(src)} が大きすぎます "
                f"({os.path.getsize(dst) / MiB:.1f}MB。上限 {cfg['max_file_mb']}MB)。"
                "JPGにするか、小さくしてから入れてください。")
        cleaned.append(dst)

    zip_tiers = {t.strip().lower() for t in cfg["zip_tiers"].split(",") if t.strip()}
    if tier in zip_tiers and cleaned:
        base = f"{os.path.basename(os.path.normpath(post_dir))}_{tier}"
        files = make_zips(cleaned, workdir, base, min(max_file, max_total))
    else:
        files = cleaned

    groups = group_by_size(files, MAX_FILES_PER_MESSAGE, max_total)
    texts = split_text(notice)
    messages = [{"content": t, "files": []} for t in texts[:-1]]
    last_text = texts[-1] if texts else ""
    if groups:
        messages.append({"content": last_text, "files": groups[0]})
        messages += [{"content": "", "files": g} for g in groups[1:]]
    elif last_text:
        messages.append({"content": last_text, "files": []})
    return messages, warnings


# ---------------------------------------------------------------- 送信

def encode_multipart(payload, files):
    boundary = uuid.uuid4().hex
    body = io.BytesIO()

    def part(headers, data):
        body.write(f"--{boundary}\r\n".encode())
        for h in headers:
            body.write(h.encode("utf-8") + b"\r\n")
        body.write(b"\r\n")
        body.write(data)
        body.write(b"\r\n")

    part(['Content-Disposition: form-data; name="payload_json"',
          "Content-Type: application/json"],
         json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    for i, path in enumerate(files):
        name = os.path.basename(path)
        ctype = {".png": "image/png", ".zip": "application/zip"}.get(
            os.path.splitext(name)[1].lower(), "image/jpeg")
        with open(path, "rb") as f:
            part([f'Content-Disposition: form-data; name="files[{i}]"; filename="{name}"',
                  f"Content-Type: {ctype}"], f.read())
    body.write(f"--{boundary}--\r\n".encode())
    return body.getvalue(), f"multipart/form-data; boundary={boundary}"


def post_message(url, message, username="", opener=urllib.request.urlopen, sleep=time.sleep):
    payload = {"content": message["content"], "allowed_mentions": {"parse": []}}
    if username:
        payload["username"] = username
    if message["files"]:
        payload["attachments"] = [{"id": i, "filename": os.path.basename(p)}
                                  for i, p in enumerate(message["files"])]
    data, ctype = encode_multipart(payload, message["files"])
    for attempt in range(5):
        req = urllib.request.Request(url + "?wait=true", data=data, method="POST",
                                     headers={"Content-Type": ctype,
                                              "User-Agent": "hiyorin-bin/1.0"})
        try:
            with opener(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            if e.code == 429:  # 送るのが速すぎる → 言われた秒数だけ待つ
                try:
                    wait = float(json.loads(detail).get("retry_after", 2))
                except ValueError:
                    wait = 2.0
                sleep(wait + 0.5)
                continue
            if e.code in (401, 404):
                raise SendError("ウェブフックURLが無効です(削除された可能性)。作り直して設定ファイルに貼り直してください。")
            if e.code == 413:
                raise SendError("ファイルが大きすぎるとDiscordに断られました。設定の max_file_mb / max_total_mb を下げてください。")
            if e.code >= 500 and attempt < 4:
                sleep(2 ** attempt)
                continue
            raise SendError(f"Discordに断られました (コード {e.code}): {detail[:300]}")
        except urllib.error.URLError as e:
            if attempt < 4:
                sleep(2 ** attempt)
                continue
            raise SendError(f"インターネットにつながりません: {e.reason}")
    raise SendError("何度やり直しても送れませんでした。時間をおいてもう一度実行してください。")


def load_record(post_dir):
    path = os.path.join(post_dir, RECORD_NAME)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_record(post_dir, record):
    with open(os.path.join(post_dir, RECORD_NAME), "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)


def send_post(post_dir, cfg, dry_run=False, opener=urllib.request.urlopen,
              sleep=time.sleep, log=print):
    """1つの投稿フォルダを送る。途中で止まっても、次は続きから送る。"""
    tiers = [t for t in TIERS if os.path.isdir(os.path.join(post_dir, t))]
    if not tiers:
        raise SendError(f"{post_dir} に gold / silver / bronze のフォルダがありません。")
    record = load_record(post_dir)
    with tempfile.TemporaryDirectory() as workdir:
        plans = {}
        for tier in tiers:  # 先に全部チェックして、問題があれば1件も送らない
            url = webhook_for(cfg, tier)
            if url is None:
                log(f"[{tier}] 設定ファイルにURLが無いので送りません。")
                continue
            messages, warnings = build_plan(post_dir, tier, cfg, workdir)
            for w in warnings:
                log(f"[{tier}] 注意: {w}")
            if messages:
                plans[tier] = (url, messages)
            else:
                log(f"[{tier}] 画像もお知らせ文も無いので送りません。")

        for tier, (url, messages) in plans.items():
            done = record.get(tier, {}).get("done", 0)
            if done >= len(messages):
                log(f"[{tier}] 送信済みです。")
                continue
            for i, msg in enumerate(messages):
                names = ", ".join(os.path.basename(p) for p in msg["files"]) or "(文だけ)"
                size = sum(os.path.getsize(p) for p in msg["files"]) / MiB
                status = "送信済み" if i < done else ("送る予定" if dry_run else "送信中")
                log(f"[{tier}] {i + 1}/{len(messages)} {status}: {names} ({size:.1f}MB)")
                if i < done or dry_run:
                    continue
                post_message(url, msg, cfg.get("username", ""), opener=opener, sleep=sleep)
                record[tier] = {"done": i + 1, "total": len(messages),
                                "last": dt.datetime.now().isoformat(timespec="seconds")}
                save_record(post_dir, record)
                sleep(1)  # 立て続けに送りすぎないように
    return plans


# ---------------------------------------------------------------- 予約

SCHEDULE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[_ ](\d{2})[-:]?(\d{2})")


def due_folders(root, now):
    """フォルダ名の日時 (例: 2026-11-01_2100) を過ぎていて、まだ送り終わっていないものを古い順に返す。"""
    due = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        m = SCHEDULE_RE.match(name)
        if not (m and os.path.isdir(path)):
            continue
        when = dt.datetime(*map(int, m.groups()))
        record = load_record(path)
        finished = record.get("_完了") is True
        if when <= now and not finished:
            due.append((when, path))
    return due


def run_auto(root, cfg, now=None, log=print, **kw):
    now = now or dt.datetime.now()
    os.makedirs(root, exist_ok=True)
    ok = True
    for when, path in due_folders(root, now):
        log(f"=== {os.path.basename(path)} (予約 {when:%Y-%m-%d %H:%M}) ===")
        if now - when > dt.timedelta(hours=6):
            log("注意: 予約時刻から6時間以上遅れて送ります(パソコンの電源が切れていた可能性)。")
        try:
            send_post(path, cfg, log=log, **kw)
            if kw.get("dry_run"):
                continue
            record = load_record(path)
            record["_完了"] = True
            save_record(path, record)
        except SendError as e:
            ok = False
            log(f"エラー: {e}")
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(description="画像とお知らせ文をDiscordのプラン別の部屋へ送ります。")
    ap.add_argument("folder", help="送るフォルダ (--auto のときは予約フォルダ)")
    ap.add_argument("--auto", action="store_true", help="予約時刻を過ぎたフォルダだけ送る")
    ap.add_argument("--dry-run", action="store_true", help="送らずに、送る内容だけ表示する")
    args = ap.parse_args(argv)
    try:
        cfg = load_config()
        if args.auto:
            return 0 if run_auto(args.folder, cfg, dry_run=args.dry_run) else 1
        send_post(args.folder, cfg, dry_run=args.dry_run)
        print("ドライラン(送っていません)。" if args.dry_run else "送り終わりました。")
        return 0
    except SendError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
