import datetime as dt
import io
import json
import os
import struct
import sys
import tempfile
import unittest
import urllib.error
import zipfile
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "ReoNa", "05_コンテンツ制作部門", "自動生成"))
import discord_send as ds  # noqa: E402

URL = "https://discord.com/api/webhooks/123/abc-DEF_9"


def chunk(ctype, data):
    return (struct.pack(">I", len(data)) + ctype + data
            + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF))


def png(extra=b"", text=b"parameters\x00masterpiece, seed 42"):
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (ds.PNG_SIG + chunk(b"IHDR", ihdr) + chunk(b"tEXt", text)
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00" + extra))
            + chunk(b"IEND", b""))


def jpeg():
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    exif = b"Exif\x00\x00seed 42"
    app1 = b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif
    com = b"\xff\xfe" + struct.pack(">H", 7) + b"hello"
    return b"\xff\xd8" + app0 + app1 + com + b"\xff\xda\x00\x02IMAGEDATA\xff\xd9"


class FakeResp:
    def __init__(self, body=b"{}"):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


class Opener:
    def __init__(self, fail_first=None):
        self.requests = []
        self.fail_first = list(fail_first or [])

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if self.fail_first:
            raise self.fail_first.pop(0)
        return FakeResp(b'{"id": "1"}')


def cfg(**kw):
    c = dict(ds.DEFAULTS, gold=URL, silver=URL, bronze="")
    c.update(kw)
    return c


class StripTest(unittest.TestCase):
    def test_png_text_removed_image_kept(self):
        clean = ds.strip_png(png())
        self.assertNotIn(b"masterpiece", clean)
        self.assertNotIn(b"tEXt", clean)
        self.assertIn(b"IDAT", clean)
        self.assertTrue(clean.endswith(chunk(b"IEND", b"")))

    def test_jpeg_exif_and_comment_removed(self):
        clean = ds.strip_jpeg(jpeg())
        self.assertNotIn(b"seed 42", clean)
        self.assertNotIn(b"hello", clean)
        self.assertIn(b"JFIF", clean)
        self.assertTrue(clean.endswith(b"IMAGEDATA\xff\xd9"))

    def test_bad_file_rejected(self):
        with self.assertRaises(ds.SendError):
            ds.strip_png(b"not a png")


class PostCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.post = os.path.join(self.tmp.name, "2026-11-01_2100")
        self.work = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()
        self.work.cleanup()

    def add(self, tier, n, size=0, notice=None):
        d = os.path.join(self.post, tier)
        os.makedirs(d, exist_ok=True)
        for i in range(n):
            with open(os.path.join(d, f"{i:03}.png"), "wb") as f:
                f.write(png(extra=os.urandom(size)))
        if notice is not None:
            with open(os.path.join(d, ds.NOTICE_NAME), "w", encoding="utf-8-sig") as f:
                f.write(notice)


class PlanTest(PostCase):

    def test_30_images_go_in_3_messages_with_notice_first(self):
        self.add("silver", 30, notice="今週のひよりん")
        msgs, _ = ds.build_plan(self.post, "silver", cfg(), self.work.name)
        self.assertEqual([len(m["files"]) for m in msgs], [10, 10, 10])
        self.assertEqual(msgs[0]["content"], "今週のひよりん")
        self.assertEqual(msgs[1]["content"], "")

    def test_size_limit_splits_messages(self):
        self.add("silver", 4, size=300_000)
        msgs, _ = ds.build_plan(self.post, "silver", cfg(max_total_mb="0.7"),
                                self.work.name)
        self.assertEqual([len(m["files"]) for m in msgs], [2, 2])

    def test_gold_zip_split_into_standalone_parts(self):
        self.add("gold", 5, size=300_000)
        msgs, _ = ds.build_plan(self.post, "gold",
                                cfg(max_file_mb="0.7", max_total_mb="0.7"), self.work.name)
        zips = [p for m in msgs for p in m["files"]]
        self.assertTrue(all(p.endswith("of3.zip") for p in zips), zips)
        names = []
        for p in zips:
            self.assertLessEqual(os.path.getsize(p), 0.7 * ds.MiB)
            with zipfile.ZipFile(p) as z:
                names += z.namelist()
                for n in z.namelist():
                    self.assertNotIn(b"masterpiece", z.read(n))
        self.assertEqual(sorted(names), [f"{i:03}.png" for i in range(5)])

    def test_single_zip_name(self):
        self.add("gold", 3)
        msgs, _ = ds.build_plan(self.post, "gold", cfg(), self.work.name)
        self.assertEqual([os.path.basename(p) for p in msgs[0]["files"]],
                         ["2026-11-01_2100_gold.zip"])

    def test_too_big_image_is_an_error(self):
        self.add("silver", 1, size=300_000)
        with self.assertRaises(ds.SendError):
            ds.build_plan(self.post, "silver", cfg(max_file_mb="0.1"), self.work.name)

    def test_common_notice_and_long_text_split(self):
        self.add("silver", 1)
        with open(os.path.join(self.post, ds.NOTICE_NAME), "w", encoding="utf-8") as f:
            f.write(("あ" * 1500 + "\n") * 2)
        msgs, _ = ds.build_plan(self.post, "silver", cfg(), self.work.name)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["files"], [])
        self.assertEqual(len(msgs[1]["files"]), 1)
        self.assertTrue(all(len(m["content"]) <= 2000 for m in msgs))

    def test_non_images_warned(self):
        self.add("silver", 1)
        open(os.path.join(self.post, "silver", "memo.webp"), "wb").close()
        _, warnings = ds.build_plan(self.post, "silver", cfg(), self.work.name)
        self.assertTrue(any("memo.webp" in w for w in warnings))


class SendTest(PostCase):
    def test_send_and_resume(self):
        self.add("silver", 25, notice="お知らせ")
        opener = Opener()
        ds.send_post(self.post, cfg(), opener=opener, sleep=lambda s: None, log=lambda *a: None)
        self.assertEqual(len(opener.requests), 3)
        body = opener.requests[0].data
        self.assertIn(b'"content": "\xe3\x81\x8a\xe7\x9f\xa5\xe3\x82\x89\xe3\x81\x9b"', body)
        self.assertIn(b'name="files[9]"', body)
        self.assertNotIn(b"masterpiece", body)
        self.assertTrue(opener.requests[0].full_url.endswith("?wait=true"))
        # もう一度動かしても二重に送らない
        again = Opener()
        ds.send_post(self.post, cfg(), opener=again, sleep=lambda s: None, log=lambda *a: None)
        self.assertEqual(again.requests, [])

    def test_resume_after_failure(self):
        self.add("silver", 25)
        err = urllib.error.HTTPError(URL, 400, "bad", {}, io.BytesIO(b"{}"))
        opener = Opener()
        calls = {"n": 0}

        def flaky(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise err
            return opener(req, timeout)

        with self.assertRaises(ds.SendError):
            ds.send_post(self.post, cfg(), opener=flaky, sleep=lambda s: None,
                         log=lambda *a: None)
        self.assertEqual(ds.load_record(self.post)["silver"]["done"], 1)
        rest = Opener()
        ds.send_post(self.post, cfg(), opener=rest, sleep=lambda s: None, log=lambda *a: None)
        self.assertEqual(len(rest.requests), 2)

    def test_rate_limit_waits_and_retries(self):
        self.add("silver", 1)
        e429 = urllib.error.HTTPError(URL, 429, "slow", {},
                                      io.BytesIO(b'{"retry_after": 1.5}'))
        opener = Opener(fail_first=[e429])
        waits = []
        ds.send_post(self.post, cfg(), opener=opener, sleep=waits.append, log=lambda *a: None)
        self.assertEqual(len(opener.requests), 2)
        self.assertIn(2.0, waits)

    def test_dry_run_sends_nothing(self):
        self.add("gold", 3)
        opener = Opener()
        ds.send_post(self.post, cfg(), dry_run=True, opener=opener, log=lambda *a: None)
        self.assertEqual(opener.requests, [])
        self.assertEqual(ds.load_record(self.post), {})

    def test_tier_without_url_skipped(self):
        self.add("bronze", 2)
        opener = Opener()
        ds.send_post(self.post, cfg(), opener=opener, log=lambda *a: None)
        self.assertEqual(opener.requests, [])

    def test_bad_url_rejected(self):
        self.add("gold", 1)
        with self.assertRaises(ds.SendError):
            ds.send_post(self.post, cfg(gold="https://example.com/x"), log=lambda *a: None)


class AutoTest(PostCase):
    def test_only_due_folders_sent_once(self):
        self.add("silver", 1)
        later = os.path.join(self.tmp.name, "2026-11-08_2100", "silver")
        os.makedirs(later)
        opener = Opener()
        now = dt.datetime(2026, 11, 1, 21, 5)
        ok = ds.run_auto(self.tmp.name, cfg(), now=now, opener=opener,
                         sleep=lambda s: None, log=lambda *a: None)
        self.assertTrue(ok)
        self.assertEqual(len(opener.requests), 1)
        self.assertTrue(ds.load_record(self.post)["_完了"])
        self.assertEqual(ds.due_folders(self.tmp.name, now), [])

    def test_not_yet_due(self):
        self.add("silver", 1)
        self.assertEqual(ds.due_folders(self.tmp.name, dt.datetime(2026, 11, 1, 20, 59)), [])


class ConfigTest(unittest.TestCase):
    def test_parse(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.txt")
            with open(p, "w", encoding="utf-8-sig") as f:
                f.write("# memo\ngold = %s\nsilver=\nmax_file_mb=50\n" % URL)
            c = ds.load_config(p)
        self.assertEqual(c["gold"], URL)
        self.assertEqual(c["silver"], "")
        self.assertEqual(c["max_file_mb"], "50")
        self.assertEqual(c["zip_tiers"], "gold")


if __name__ == "__main__":
    unittest.main()
