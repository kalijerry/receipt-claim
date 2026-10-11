"""Run: .venv/bin/python -m unittest discover -s tests -v   (Windows: .venv\\Scripts\\python -m unittest discover -s tests -v)"""
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import native  # noqa: E402
from core import scan as S  # noqa: E402
from core import trip as T  # noqa: E402
from core.excel import write_workbook  # noqa: E402
from core.extract import build_rows, extract  # noqa: E402

# Optional local sample set (not in the repository: real receipts are personal data).
# Put photos in DATA/ plus DATA/truth.json: {"<first 8 chars of file name>": {"amount": 12.5,
# "date": "2026-09-24" or null, "warn": "<text expected in a warning>"}}. Without it these tests are skipped.
DATA = APP.parent / "DATA"
TRUTH_FILE = DATA / "truth.json"
TRUTH = {k: (v["amount"], v["date"]) for k, v in json.loads(TRUTH_FILE.read_text()).items()} \
    if TRUTH_FILE.exists() else {}
TRUTH_WARN = {k: v["warn"] for k, v in json.loads(TRUTH_FILE.read_text()).items() if v.get("warn")} \
    if TRUTH_FILE.exists() else {}
needs_samples = unittest.skipUnless(TRUTH, "no local sample set (DATA/truth.json)")


def sample_files():
    return sorted(p for p in DATA.iterdir() if p.is_file() and p.name[:8] in TRUTH)


def rec(i, amount, day, **kw):
    r = {"id": f"r{i}", "orig_name": kw.pop("name", f"IMG_{i}.jpeg"), "stored": f"r{i}.jpeg", "sha1": f"s{i}",
         "amount": amount, "date": day, "time": kw.pop("time", None), "merchant": "", "include": True,
         "exclude_reason": "", "group": None, "note": "", "ocr": {"amount": amount, "date": day}}
    r.update(kw)
    return r


@needs_samples
class TestExtractOnRealData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = {Path(o["file"]).name[:8]: extract(o) for o in native.ocr(sample_files()).values()}

    def test_every_file_covered(self):
        self.assertEqual(set(self.res), set(TRUTH))

    def test_amounts(self):
        for k, (amt, _) in TRUTH.items():
            with self.subTest(k):
                self.assertAlmostEqual(self.res[k]["amount"], amt, places=2)

    def test_dates(self):
        for k, (_, day) in TRUTH.items():
            with self.subTest(k):
                if day:
                    self.assertEqual(self.res[k]["date"], day)
                    self.assertEqual(self.res[k]["date_source"], "ocr")
                else:
                    self.assertEqual(self.res[k]["date_source"], "photo")
                    self.assertTrue(any("照片拍摄日期" in w for w in self.res[k]["warnings"]))

    def test_expected_warnings(self):
        for k, text in TRUTH_WARN.items():
            with self.subTest(k):
                self.assertTrue(any(text in w for w in self.res[k]["warnings"]))


class TestPeriods(unittest.TestCase):
    P = [{"start": "2026-09-03", "end": "2026-09-06"}, {"start": "2026-09-08", "end": "2026-09-12"}]

    def test_multi_segment_days(self):
        self.assertEqual(T.total_days(self.P), 4 + 5)
        self.assertEqual(T.summary({**T.new_trip("x"), "periods": self.P})["cap"], 450)

    def test_boundaries_and_gap(self):
        for day, inside in [("2026-09-03", True), ("2026-09-06", True), ("2026-09-07", False),
                            ("2026-09-08", True), ("2026-09-12", True), ("2026-09-13", False), (None, False)]:
            with self.subTest(day):
                self.assertEqual(T.in_periods(day, self.P), inside)

    def test_problems(self):
        self.assertEqual(T.period_problems(self.P), [])
        self.assertTrue(T.period_problems([{"start": "2026-09-05", "end": "2026-09-01"}]))
        overlap = [{"start": "2026-09-01", "end": "2026-09-05"}, {"start": "2026-09-05", "end": "2026-09-06"}]
        self.assertTrue(any("重叠" in p for p in T.period_problems(overlap)))
        self.assertEqual(T.total_days(overlap), 6)  # overlapping day counted once


class TestClaims(unittest.TestCase):
    def trip(self, receipts, groups=None):
        t = T.new_trip("t")
        t["periods"] = [{"start": "2026-09-03", "end": "2026-09-06"}, {"start": "2026-09-08", "end": "2026-09-12"}]
        t["receipts"], t["groups"] = receipts, groups or {}
        return t

    def test_aa_group_counts_once_at_personal_share(self):
        t = self.trip([rec(1, 60.00, "2026-09-04", group="G1"), rec(2, 20.00, "2026-09-04", group="G1"),
                       rec(3, 12.00, "2026-09-05")], {"G1": {"paid": None}})
        claims = T.build_claims(t)
        self.assertEqual(len(claims), 2)
        g = next(c for c in claims if c["group"])
        self.assertEqual(g["amount"], 20.00)                    # default = smallest (AA share)
        self.assertEqual(T.summary(t, claims)["spent"], 32.00)  # not 92
        t["groups"]["G1"]["paid"] = 25.5                        # user override
        self.assertEqual(T.summary(t)["spent"], 37.5)

    def test_group_filenames_dash_numbers(self):
        t = self.trip([rec(1, 60.00, "2026-09-04", group="G1"), rec(2, 20.00, "2026-09-04", group="G1",
                                                                     name="b.HEIC")], {"G1": {"paid": None}})
        plan = T.plan_filenames(t, T.build_claims(t))
        self.assertEqual(plan["r1"], "01_20.00_2026-09-04-1.jpeg")  # full bill first
        self.assertEqual(plan["r2"], "01_20.00_2026-09-04-2.heic")

    def test_unrelated_same_name_never_collides(self):
        t = self.trip([rec(1, 9.5, "2026-09-04"), rec(2, 9.5, "2026-09-04", name="x.heic"),
                       rec(3, 9.5, "2026-09-04", group="G1"), rec(4, 5, "2026-09-04", group="G1"),
                       rec(5, 5.0, "2026-09-04")], {"G1": {"paid": 9.5}})
        plan = T.plan_filenames(t, T.build_claims(t))
        bases = [v.rsplit(".", 1)[0].lower() for v in plan.values()]
        self.assertEqual(len(bases), len(set(bases)))
        self.assertEqual(sum(b.endswith("_9.50_2026-09-04") for b in bases), 2)   # numbers keep them apart

    def test_filenames_follow_date_order(self):
        t = self.trip([rec(1, 100, "2026-09-11", time="09:00"), rec(2, 12.5, "2026-09-03"),
                       rec(3, 9.99, "2026-09-05", time="20:00"), rec(4, 7, "2026-09-05", time="08:30"),
                       rec(5, 3, "2026-09-05")])                     # no time -> last on its day
        plan = T.plan_filenames(t, T.build_claims(t))
        names = sorted(plan.values())                                 # what Finder shows
        self.assertEqual(names, ["01_12.50_2026-09-03.jpeg", "02_7.00_2026-09-05.jpeg",
                                 "03_9.99_2026-09-05.jpeg", "04_3.00_2026-09-05.jpeg",
                                 "05_100.00_2026-09-11.jpeg"])

    def test_excluded_and_out_of_period(self):
        t = self.trip([rec(1, 10, "2026-09-07"), rec(2, 30, "2026-09-09", include=False, exclude_reason="重复")])
        claims = T.build_claims(t)
        self.assertEqual(len(claims), 1)
        self.assertFalse(claims[0]["in_period"])
        s = T.summary(t, claims)
        self.assertEqual((s["spent"], s["out_of_period"]), (0, 10))
        plan = T.plan_filenames(t, claims)
        self.assertTrue(plan["r2"].startswith("未计入/"))
        self.assertEqual(plan["r1"], "未计入/01_10.00_2026-09-07.jpeg")   # out of period keeps its number

    def test_cap(self):
        t = self.trip([rec(i, 100, "2026-09-04") for i in range(6)])   # 600 spent, 9 days * 50 = 450 cap
        s = T.summary(t)
        self.assertEqual((s["cap"], s["reimburse"], s["over"]), (450, 450, 150))

    def test_duplicates(self):
        rs = [rec(1, 33.92, "2026-09-30", time="20:34"), rec(2, 33.92, "2026-09-30", time="20:34"),
              rec(3, 33.92, "2026-09-30", time="21:00")]
        T.mark_duplicates(rs)
        self.assertEqual([r["include"] for r in rs], [True, False, True])

    def test_verification_invalidated_by_changes(self):
        t = self.trip([rec(1, 60, "2026-09-04"), rec(2, 20, "2026-09-04")])
        T.set_verified(t, ["r1", "r2"])
        self.assertEqual(T.unverified(t), [])
        t["receipts"][0]["amount"] = 61
        self.assertEqual([r["id"] for r in T.unverified(t)], ["r1"])
        T.set_verified(t, ["r1"])
        t["receipts"][0]["group"] = t["receipts"][1]["group"] = "G1"   # linking re-opens both
        self.assertEqual(len(T.unverified(t)), 2)
        T.set_verified(t, ["r1", "r2"])
        t["groups"] = {"G1": {"paid": 30}}                             # changing personal paid too
        self.assertEqual(len(T.unverified(t)), 2)

    def test_number_format_does_not_undo_confirmation(self):
        t = self.trip([rec(1, 14, "2026-09-04")])
        T.set_verified(t, ["r1"])
        e = {"id": "r1", "amount": 14}
        T.check_receipt_edit(e)                     # stored as 14.0 after validation
        t["receipts"][0]["amount"] = e["amount"]
        self.assertEqual(T.unverified(t), [])

    def test_excluding_group_member_reopens_whole_claim(self):
        t = self.trip([rec(1, 100, "2026-09-04", group="G1"), rec(2, 25, "2026-09-04", group="G1")],
                      {"G1": {"paid": None}})
        T.set_verified(t, ["r1", "r2"])
        self.assertEqual(T.build_claims(t)[0]["amount"], 25)
        t["receipts"][1]["include"] = False                            # exclude the AA share
        self.assertIn("r1", [r["id"] for r in T.unverified(t)])         # remaining member must be re-checked
        T.normalize_groups(t)                                           # and the 1-member group is dissolved
        self.assertEqual([r["group"] for r in t["receipts"]], [None, None])
        self.assertEqual(t["groups"], {})

    def test_amount_change_inside_group_reopens_all(self):
        t = self.trip([rec(1, 100, "2026-09-04", group="G1"), rec(2, 25, "2026-09-04", group="G1")],
                      {"G1": {"paid": None}})
        T.set_verified(t, ["r1", "r2"])
        t["receipts"][1]["amount"] = 30
        self.assertEqual(len(T.unverified(t)), 2)

    def test_edit_validation(self):
        for bad in ({"date": "2026-13-01"}, {"date": "abc"}, {"amount": -1}, {"amount": "x"}):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    T.check_receipt_edit(dict(bad))
        e = {"amount": "12.345", "date": "2026-09-04"}
        T.check_receipt_edit(e)
        self.assertEqual(e["amount"], 12.35 if round(12.345, 2) == 12.35 else 12.34)
        with self.assertRaises(ValueError):
            T.check_periods([{"start": "2026-02-30", "end": "2026-03-01"}])
        t = self.trip([rec(1, 5, "2026-09-04", group="G1"), rec(2, 6, "2026-09-04", group="G1")], {"G1": {"paid": -3}})
        with self.assertRaises(ValueError):
            T.normalize_groups(t)


class TestExcelFormulas(unittest.TestCase):
    def test_formulas_evaluate_correctly(self):
        from pycel import ExcelCompiler
        t = T.new_trip("公式测试")
        t["periods"] = [{"start": "2026-09-03", "end": "2026-09-06"}, {"start": "2026-09-08", "end": "2026-09-12"}]
        t["receipts"] = [rec(1, 60, "2026-09-04", group="G1"), rec(2, 20, "2026-09-04", group="G1"),
                         rec(3, 12.34, "2026-09-12"), rec(4, 99, "2026-09-07"), rec(5, 5, "2026-09-03"),
                         rec(6, 7, "2026-09-05", include=False)]
        t["groups"] = {"G1": {"paid": None}}
        claims = T.build_claims(t)
        plan = T.plan_filenames(t, claims)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.xlsx"
            write_workbook(p, t, claims, plan, lambda r: "")
            xl = ExcelCompiler(filename=str(p))
            got = {xl.evaluate(f"汇总!A{r}"): xl.evaluate(f"汇总!B{r}") for r in range(8, 15)}  # row 15 uses COUNTA, which pycel lacks
            self.assertEqual(got["出差总天数"], 9)
            self.assertEqual(got["可报销上限（天数 × 额度）"], 450)
            self.assertAlmostEqual(got["期内个人实付合计"], 20 + 12.34 + 5)
            self.assertAlmostEqual(got["期外金额（不计入）"], 99)
            self.assertAlmostEqual(got["应报销金额"], 37.34)
            self.assertEqual(got["超出上限自付部分"], 0)
            flags = [xl.evaluate(f"明细!H{i}") for i in range(2, 2 + len(claims))]
            self.assertEqual(flags, ["是" if c["in_period"] else "否" for c in claims])
            s = T.summary(t, claims)
            self.assertAlmostEqual(got["应报销金额"], s["reimburse"])


class TestScanCheck(unittest.TestCase):
    def test_pound_misread_tolerated(self):
        self.assertTrue(S.money_match(212.48, 12.48))
        self.assertTrue(S.money_match(20.00, 0.00))
        self.assertFalse(S.money_match(14.98, 4.98))
        # ...but never for the confirmed amount itself: 21.50 on the scan must not "confirm" £1.50
        scan = {"money": [21.5], "words": [], "dates": []}
        self.assertFalse(S.check({"amount": 1.5, "date": None}, scan, scan, scan)["amount_ok"])

    def test_crop_that_lost_a_receipt_fails(self):
        # photo with two receipts side by side; the crop kept only the card slip
        orig = {"money": [1.5, 4.0, 16.0, 21.5], "words": ["DRINKS", "STEW", "BROKEN", "RICE", "VISA"], "dates": ["2026-01-05"]}
        full = {"money": [1.5, 4.0, 16.0, 21.5], "words": ["DRINKS", "STEW", "BROKEN", "RICE", "VISA"], "dates": ["2026-01-05"]}
        crop = {"money": [21.5], "words": ["VISA"], "dates": ["2026-01-05"]}
        r = {"amount": 21.5, "date": "2026-01-05"}
        self.assertFalse(S.check(r, orig, crop, full)["ok"])
        self.assertTrue(S.check(r, orig, full, full, cropped=False)["ok"])

    def test_crop_that_reads_better_than_original_passes(self):
        # original + full-frame scan misread '£' as '2'; the cleaner crop reads correctly
        orig = {"money": [2.5, 2.99, 11.99, 14.98, 20.0, 212.48], "words": ["BURGER", "MEAL"], "dates": ["2026-01-06"]}
        crop = {"money": [0.0, 2.5, 2.99, 11.99, 12.48, 14.98], "words": ["BURGER", "MEAL"], "dates": ["2026-01-06"]}
        self.assertTrue(S.check({"amount": 14.98, "date": "2026-01-06"}, orig, crop, orig)["ok"])

    def test_wrong_amount_detected(self):
        scan = {"money": [15.81], "words": [], "dates": ["2026-09-28"]}
        c = S.check({"amount": 15.18, "date": "2026-09-28"}, scan, scan, scan)
        self.assertFalse(c["ok"])
        self.assertFalse(c["amount_ok"])


@needs_samples
class TestLocate(unittest.TestCase):
    def test_amount_and_date_found_on_every_real_receipt(self):
        from core.extract import locate
        for o in native.ocr(sample_files()).values():
            k = Path(o["file"]).name[:8]
            amt, day = TRUTH[k]
            with self.subTest(k):
                hl = locate(o["lines"], amt, day)
                self.assertTrue(hl["amount"], "amount not located")
                if day:
                    self.assertTrue(hl["date"], "date not located")
                for b in hl["amount"] + hl["date"]:
                    self.assertTrue(all(0 <= v <= 1.0001 for v in b))
                self.assertEqual(locate(o["lines"], 123456.78, None)["amount"], [])   # wrong amount -> nothing


class TestLayout(unittest.TestCase):
    def test_strict_order_and_nothing_dropped(self):
        items = [{"key": f"k{i}", "px_w": w, "px_h": h, "line_h": lh, "caption": f"#{i}"}
                 for i, (w, h, lh) in enumerate([(1300, 3300, .025), (800, 1000, None), (648, 3483, .0082),
                                                 (300, 4000, .004),    # ~80 x 600 mm till roll
                                                 (4000, 900, .01),     # very wide photo
                                                 (900, 1200, .02)] * 2)]
        pages = S.layout(items)
        seq = [p["key"] for pg in pages for p in pg]
        # reading order = input order; every receipt present, slices consecutive
        self.assertEqual(list(dict.fromkeys(seq)), [it["key"] for it in items])
        for it in items:
            pieces = [p for pg in pages for p in pg if p["key"] == it["key"]]
            crops = [p["crop"] for p in pieces]
            self.assertAlmostEqual(crops[0][0], 0.0)
            self.assertAlmostEqual(crops[-1][1], 1.0)                 # whole receipt covered
            for a, b in zip(crops, crops[1:]):
                self.assertLessEqual(b[0], a[1])                      # slices overlap / touch
        tall = [p for pg in pages for p in pg if p["key"] == "k3"]
        self.assertGreaterEqual(len(tall), 3)

    def test_cover_pages(self):
        self.assertEqual(S.cover_page_count(5), 1)
        self.assertEqual(S.cover_page_count(S.COVER_ROWS_FIRST), 1)
        self.assertEqual(S.cover_page_count(S.COVER_ROWS_FIRST + 1), 2)
        pages = [[{"key": "a"}, {"key": "b"}], [{"key": "b"}, {"key": "c"}]]
        rows = [{"keys": ["a"]}, {"keys": ["b"]}, {"keys": ["c", "a"]}, {"keys": ["zz"]}]
        self.assertEqual(S.claim_pages(pages, rows, 1), [2, 2, 2, None])

    def test_pages_valid_and_readable(self):
        items = [{"key": "short", "px_w": 1300, "px_h": 3300, "line_h": 0.025, "caption": "a"},
                 {"key": "long", "px_w": 648, "px_h": 3483, "line_h": 0.0082, "caption": "asda"},
                 {"key": "lowinfo", "px_w": 800, "px_h": 1000, "line_h": None, "caption": "c"}] * 3
        pages = S.layout(items)
        for page in pages:
            for p in page:
                self.assertGreaterEqual(p["x"], S.MARGIN - 1e-6)
                self.assertLessEqual(p["x"] + p["w"], S.PAGE_W - S.MARGIN + 1e-6)
                self.assertLessEqual(p["y"] + p["h"] + S.CAPTION, S.PAGE_H - S.MARGIN + 1e-6)
            for a in page:   # no overlaps
                for b in page:
                    if a is not b:
                        sep = (a["x"] + a["w"] <= b["x"] + 1e-6 or b["x"] + b["w"] <= a["x"] + 1e-6 or
                               a["y"] + a["h"] + S.CAPTION <= b["y"] + 1e-6 or b["y"] + b["h"] + S.CAPTION <= a["y"] + 1e-6)
                        self.assertTrue(sep, (a, b))
        long_pieces = [p for pg in pages for p in pg if p["key"] == "long"]
        self.assertEqual(len(long_pieces), 6)   # 3 long receipts, split in 2 slices each
        # text height on paper never below the minimum
        for pg in pages:
            for p in pg:
                it = next(i for i in items if i["key"] == p["key"])
                if it["line_h"]:
                    line_mm = it["line_h"] * it["px_h"] * (p["w"] / it["px_w"])
                    self.assertGreaterEqual(line_mm, S.MIN_LINE_MM - 0.05)



def mono_font(size):
    from PIL import ImageFont
    for f in ("/System/Library/Fonts/Menlo.ttc", "C:/Windows/Fonts/consola.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"):
        if Path(f).exists():
            return ImageFont.truetype(f, size)
    raise unittest.SkipTest("no monospaced system font to draw test receipts with")


def make_receipt(path, lines):
    """A plain synthetic till receipt photo: white paper on a grey table, monospaced text."""
    from PIL import Image, ImageDraw
    font = mono_font(46)
    img = Image.new("RGB", (1500, 2000), (120, 118, 112))
    d = ImageDraw.Draw(img)
    d.rectangle((330, 160, 1170, 1840), fill=(250, 250, 247))
    for i, t in enumerate(lines):
        d.text((380, 260 + i * 80), t, fill=(25, 25, 25), font=font)
    img.save(path, "JPEG", quality=92)


class TestServerEndToEnd(unittest.TestCase):
    """Runs the real HTTP server against a temporary output folder with two generated receipts."""

    @classmethod
    def setUpClass(cls):
        import importlib
        import os
        import threading
        from http.server import ThreadingHTTPServer
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["BX_OUTPUT"] = cls.tmp.name + "/out"
        import server
        cls.srv_mod = importlib.reload(server)
        cls.srv_mod.OUTPUT.mkdir(parents=True)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cls.srv_mod.H)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_port}/api/trips"
        cls.src = Path(cls.tmp.name) / "in"
        cls.src.mkdir()
        make_receipt(cls.src / "a.jpg", ["  TEST NOODLE BAR", "  1 HIGH STREET", "", "05-09-2026     12:30",
                                          "BEEF STEW     *16.00", "DRINKS         *3.50", "-" * 20,
                                          "TOTAL         *19.50", "CARD          *19.50", "", "   THANK YOU"])
        make_receipt(cls.src / "b.jpg", ["   CORNER CAFE", "", "Date: 06/09/2026 13:10", "Sandwich      £9.00",
                                          "Coffee        £5.00", "-" * 20, "Total:       £14.00",
                                          "Card:        £14.00", "", "   APPROVED"])

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.tmp.cleanup()

    def call(self, method, path="", body=None):
        import urllib.error
        import urllib.parse
        import urllib.request
        url = self.base + "/" + urllib.parse.quote("测试") + path
        req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_flow(self):
        import urllib.request
        req = urllib.request.Request(self.base, method="POST", data=json.dumps({"name": "测试"}).encode())
        urllib.request.urlopen(req).read()
        code, v = self.call("POST", "/import-folder", {"path": str(self.src)})
        self.assertEqual((code, v.get("added", v)), (200, 2))
        ids = [r["id"] for r in v["trip"]["receipts"]]
        got = sorted((r["date"], r["amount"]) for r in v["trip"]["receipts"])
        self.assertEqual(got, [("2026-09-05", 19.5), ("2026-09-06", 14.0)])
        self.assertTrue(all(r["_hl"]["amount"] and r["_hl"]["date"] for r in v["trip"]["receipts"]))

        # invalid input is rejected and the trip still opens
        code, _ = self.call("PUT", "", {"receipts": [{"id": ids[0], "date": "2026-02-31"}]})
        self.assertEqual(code, 400)
        code, _ = self.call("PUT", "", {"periods": [{"start": "x", "end": "2026-10-01"}]})
        self.assertEqual(code, 400)
        self.assertEqual(self.call("GET")[0], 200)

        # periods are stored sorted
        code, v = self.call("PUT", "", {"periods": [{"start": "2026-09-06", "end": "2026-09-07"},
                                                    {"start": "2026-09-04", "end": "2026-09-05"}]})
        self.assertEqual([p["start"] for p in v["trip"]["periods"]], ["2026-09-04", "2026-09-06"])

        # link both, confirm, then exclude one -> group dissolved, the other must be re-checked
        code, v = self.call("PUT", "", {"receipts": [{"id": i, "group": "G1"} for i in ids], "groups": {"G1": {"paid": None}}})
        self.assertEqual(len(v["claims"]), 1)
        self.call("POST", "/confirm", {"ids": ids})
        code, v = self.call("PUT", "", {"receipts": [{"id": ids[1], "include": False, "exclude_reason": "测试"}]})
        self.assertEqual([r["group"] for r in v["trip"]["receipts"]], [None, None])
        self.assertFalse(v["claims"][0]["verified"])

        # confirm everything; export must keep a file the user saved next to ours
        self.call("PUT", "", {"receipts": [{"id": ids[1], "include": True, "exclude_reason": ""}]})
        self.call("POST", "/confirm", {"ids": ids})
        trip_dir = self.srv_mod.trip_dir("测试")
        (trip_dir / "测试_报销表_改.xlsx").write_text("mine")
        code, res = self.call("POST", "/export")
        self.assertEqual(code, 200, res)
        self.assertTrue((trip_dir / "测试_报销表_改.xlsx").exists())
        names = sorted(p.name for p in (trip_dir / "报销票据").iterdir() if p.is_file())
        self.assertEqual(names, ["01_19.50_2026-09-05.jpg", "02_14.00_2026-09-06.jpg"])
        self.assertFalse(self.call("GET")[1]["export_stale"])

        # scanned version -> PDF with cover page + receipts
        code, v = self.call("POST", "/scan")
        self.assertEqual((code, v["scan_bad"]), (200, 0))
        self.assertTrue(self.call("GET")[1]["export_stale"])      # changed since last export
        code, res = self.call("POST", "/export")
        self.assertEqual(code, 200, res)
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(res["pdf"])
        cover = pdf[0].get_textpage().get_text_range()
        self.assertIn("应报销金额", cover)
        self.assertIn("£33.50", cover)
        self.assertGreaterEqual(len(pdf), 2)
        pdf.close()      # Windows cannot replace a file that is still open (the next export rewrites the PDF)

        # out-of-period claims leave the PDF and go to 未计入
        self.call("PUT", "", {"periods": [{"start": "2026-09-10", "end": "2026-09-11"}]})
        code, res = self.call("POST", "/export")
        self.assertEqual(code, 200, res)
        self.assertEqual(res["summary"]["spent"], 0)
        self.assertTrue(all(f.startswith("未计入/") for f in res["files"]))


class TestPlatformIndependentHelpers(unittest.TestCase):
    def test_folder_names_valid_on_windows(self):
        self.assertEqual(T.safe_name('a:b*c?"d'), "a_b_c__d")
        self.assertEqual(T.safe_name("CON"), "_CON")
        self.assertEqual(T.safe_name("nul.txt"), "_nul.txt")
        self.assertEqual(T.safe_name("com1"), "_com1")
        self.assertEqual(T.safe_name("console"), "console")
        self.assertEqual(T.safe_name("  ... "), "未命名出差")

    def test_rows_use_tilt_free_box_when_given(self):
        # photo tilted: "TOTAL" at the left and its amount at the right are 0.06 apart in y, one line pitch is 0.03
        lines = [{"text": "TOTAL", "box": [0.1, 0.30, 0.2, 0.02], "rbox": [0.1, 0.33, 0.2, 0.02]},
                 {"text": "14.30", "box": [0.7, 0.36, 0.1, 0.02], "rbox": [0.7, 0.33, 0.1, 0.02]},
                 {"text": "VAT", "box": [0.1, 0.33, 0.2, 0.02], "rbox": [0.1, 0.36, 0.2, 0.02]},
                 {"text": "2.38", "box": [0.7, 0.39, 0.1, 0.02], "rbox": [0.7, 0.36, 0.1, 0.02]}]
        self.assertEqual(build_rows(lines), ["TOTAL 14.30", "VAT 2.38"])
        # without rbox (macOS Vision) nothing changes: boxes are used as they are
        self.assertEqual(build_rows([{k: v for k, v in ln.items() if k != "rbox"} for ln in lines]),
                         ["TOTAL", "VAT", "14.30", "2.38"])


@unittest.skipUnless(sys.platform == "win32", "Windows backend")
class TestWindowsBackend(unittest.TestCase):
    """Synthetic phone photos through the pure-Python OCR / scan / preview of native/windows.py."""
    LINES = ["  TEST NOODLE BAR", "  1 HIGH STREET", "", "Date: 05/09/2026   12:30", "1 x Ramen       £11.50",
             "1 x Gyoza        £6.20", "-" * 26, "SUBTOTAL        £17.70", "VAT              £2.95",
             "TOTAL           £17.70", "CARD PAYMENT    £17.70", "", "   THANK YOU"]

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def photo(self, name, degrees=0, exif=None, size=(1500, 2000)):
        from PIL import Image
        flat = self.dir / "flat.jpg"
        make_receipt(flat, self.LINES)
        im = Image.open(flat)
        if degrees:
            im = im.rotate(degrees, resample=Image.BICUBIC, fillcolor=(120, 118, 112))
        path = self.dir / name
        im.save(path, quality=92, **({"exif": exif} if exif is not None else {}))
        return path

    def test_tilted_photo_reads_the_total_not_the_vat(self):
        path = self.photo("tilt.jpg", degrees=7)
        got = extract(native.ocr([path])[str(path)])
        self.assertEqual((got["amount"], got["date"]), (17.70, "2026-09-05"))

    def test_exif_orientation_and_taken_date(self):
        from PIL import Image
        exif = Image.Exif()
        exif[0x0112] = 6                                   # stored sideways, viewers turn it 90 degrees clockwise
        exif.get_ifd(0x8769)[0x9003] = "2026:09:05 12:31:00"
        flat = self.dir / "up.jpg"
        make_receipt(flat, self.LINES)
        path = self.dir / "side.jpg"
        Image.open(flat).rotate(90, expand=True).save(path, quality=92, exif=exif)
        res = native.ocr([path])[str(path)]
        self.assertEqual(res["exif_date"], "2026:09:05 12:31:00")
        self.assertEqual(extract(res)["amount"], 17.70)
        for ln in res["lines"]:                            # boxes are normalized and inside the image
            self.assertTrue(all(-1e-6 <= v <= 1 + 1e-6 for v in ln["box"]))

    def test_heic_photo(self):
        try:
            import pillow_heif  # noqa: F401
            from PIL import Image
            pillow_heif.register_heif_opener()
            path = self.dir / "iphone.heic"
            make_receipt(self.dir / "h.jpg", self.LINES)
            Image.open(self.dir / "h.jpg").save(path, quality=80)
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"cannot write HEIC here: {e}")
        self.assertEqual(extract(native.ocr([path])[str(path)])["amount"], 17.70)
        prev = self.dir / "iphone_360.jpg"
        native.make_preview(path, prev, 360)
        from PIL import Image
        with Image.open(prev) as im:
            self.assertEqual((im.format, max(im.size)), ("JPEG", 360))

    def test_scans_agree_with_the_original(self):
        path = self.photo("scan.jpg", degrees=4)
        orig = S.facts(native.ocr([path])[str(path)])
        for mode in S_MODES:
            with self.subTest(mode):
                out = self.dir / f"scan_{mode}.jpg"
                meta = native.scan_image(path, out, mode)
                self.assertTrue(meta["ok"], meta)
                scan = S.facts(native.ocr([out])[str(out)])
                c = S.check({"amount": 17.70, "date": "2026-09-05"}, orig, scan, cropped=meta["cropped"])
                self.assertTrue(c["ok"], c)
        self.assertFalse(native.scan_image(self.dir / "missing.jpg", self.dir / "x.jpg", "enhance")["ok"])

    def test_unreadable_file_is_an_empty_result(self):
        bad = self.dir / "bad.jpg"
        bad.write_bytes(b"not an image")
        self.assertEqual(native.ocr([bad])[str(bad)]["lines"], [])


S_MODES = ("crop", "box", "enhance")


class TestBenchmarkTools(unittest.TestCase):
    """The accuracy benchmark in bench/ must itself be right, or its numbers mean nothing."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(APP / "bench"))
        import run as bench
        import stress
        import synth
        cls.bench, cls.stress, cls.synth = bench, stress, synth

    def test_merchant_match_ignores_spacing_and_case(self):
        m = self.bench.merchant_ok
        self.assertTrue(m("TESTNOODLEBAR", "Test Noodle Bar"))
        self.assertTrue(m("Sainsburys Local", "Sainsbury's Local"))
        self.assertFalse(m("Costa", "Greggs"))
        self.assertFalse(m("", "Greggs"))
        self.assertIsNone(m("anything", None))

    def test_summary_counts_false_confident_separately_from_caught_errors(self):
        def r(amount_ok, flagged):
            wrong = not amount_ok
            return {"amount_ok": amount_ok, "date_ok": None, "merchant_ok": None, "flagged": flagged, "wrong": wrong,
                    "false_confident": wrong and not flagged, "secs": 1.0, "amount_conf": 0.9}
        s = self.bench.summarize([r(True, False), r(True, True), r(False, True), r(False, False)])
        self.assertEqual((s["n"], s["amount_acc"], s["flag_rate"]), (4, 50.0, 50.0))
        self.assertEqual((s["false_confident"], s["catch_rate"]), (1, 50.0))

    def test_synthetic_set_has_matching_truth_and_every_stress_variant_renders(self):
        with tempfile.TemporaryDirectory() as tmp:
            truth = self.synth.generate(tmp, n=3, seed=7)
            self.assertEqual(sorted(truth), ["synth001", "synth002", "synth003"])
            self.assertEqual(json.loads((Path(tmp) / "truth.json").read_text("utf-8")), truth)
            for t in truth.values():
                self.assertGreater(t["amount"], 0)
            made = self.stress.make_all(Path(tmp) / "synth001.jpg", Path(tmp) / "stress")
            self.assertEqual(set(made), set(self.stress.VARIANTS))
            for variant, path in made.items():
                self.assertEqual(path.stem, "synth001")                      # truth lookup uses the name
                self.assertIsNotNone(self.stress.open_image(path), variant)
            self.assertLess(self.stress.open_image(made["half"]).width, self.stress.open_image(made["blur"]).width)


class TestExtractRules(unittest.TestCase):
    """Rules learned from real receipts (OCR slips in dates, repeated item prices) and the two-reader merge."""
    TODAY = date(2026, 10, 11)
    PERIOD = [{"start": "2026-09-22", "end": "2026-10-09"}]

    @staticmethod
    def lines(rows):
        return [{"text": t, "box": [0.1, 0.03 * i, 0.8, 0.02]} for i, t in enumerate(rows)]

    def ex(self, rows, alt=None, exif=None, periods=None):
        ocr = {"lines": self.lines(rows)}
        if alt is not None:
            ocr["alt"] = {"reader": "second", "lines": self.lines(alt)}
        if exif:
            ocr["exif_date"] = exif
        return extract(ocr, today=self.TODAY, periods=periods)

    def date_of(self, row):
        return self.ex([row])["date"] if self.ex([row])["date_source"] == "ocr" else None

    def test_dates_that_ocr_garbles(self):
        self.assertEqual(self.date_of("15/08/202616:56"), "2026-08-15")             # date and time glued together
        self.assertEqual(self.date_of("04 0ct 2026 15:07"), "2026-10-04")           # O read as 0 in the month
        self.assertEqual(self.date_of("#6407 16:49:41 040CT2026"), "2026-10-04")     # all glued, O read as 0
        self.assertEqual(self.date_of("9 Oct:'26 12:49 PM"), "2026-10-09")           # odd punctuation
        self.assertEqual(self.date_of("O5/O9/2O26"), "2026-09-05")                  # letters for digits
        self.assertEqual(self.date_of("15:14:51 050C12026"), "2026-10-05")           # T read as 1 (one wrong character)
        self.assertIsNone(self.date_of("12 xyz 2026"))                              # no month fits
        self.assertEqual(self.date_of("Date 05/10/2026"), "2026-10-05")             # the plain forms still work
        self.assertIsNone(self.date_of("24/05/2006"))                               # a misread year is no date

    def test_repeated_item_prices_do_not_beat_the_total(self):
        rows = ["JS BAY LEAVES £1.15", "JS CLOVES £1.15", "JS SEED £1.15", "CUCUMBER £1.15", "CUCUMBER £1.15",
                "ORGANIC BEEF £8.00", "BALANCE DUE £38.10", "Visa DEBIT £38.10", "CHANGE £0.00"]
        self.assertEqual(self.ex(rows)["amount"], 38.10)

    def test_labels_glued_by_ocr(self):
        rows = ["1 x Latte f3.05", "1 x Bottled water f10.26", "1 X Pasta bake f9.51", "SUBTOTAL 39.94",
                "BALANCE DUE 39.94", "VISACONTACTLESS 39.94"]
        self.assertEqual(self.ex(rows)["amount"], 39.94)

    def test_pound_misread_as_f(self):
        self.assertEqual(self.ex(["Total f12.40"])["amount"], 12.40)

    def test_single_reader_result_has_no_agreement_fields(self):
        r = self.ex(["TOTAL £9.00", "05/10/2026"])
        self.assertNotIn("agree", r)
        self.assertEqual((r["amount"], r["date"], r["warnings"]), (9.0, "2026-10-05", []))

    def test_two_readers_that_agree_raise_no_warning(self):
        rows = ["TOTAL £9.00", "05/10/2026"]
        r = self.ex(rows, alt=rows, exif="2026:10:05 12:00:00", periods=self.PERIOD)
        self.assertEqual(r["warnings"], [])
        self.assertEqual(r["agree"], {"amount": True, "date": True})

    def test_readers_that_disagree_on_the_amount_ask_a_person(self):
        r = self.ex(["Balance due £20.00", "05/10/2026"], alt=["Total £18.00", "05/10/2026"])
        self.assertEqual(r["agree"]["amount"], False)
        self.assertTrue(any("金额不同" in w for w in r["warnings"]))
        self.assertEqual(set(r["amount_alts"][:2]), {18.0, 20.0})                  # both stay one click away

    def test_readers_that_disagree_on_the_date_prefer_the_one_that_fits(self):
        r = self.ex(["TOTAL £9.00", "08/10/2026"], alt=["TOTAL £9.00", "08/05/2026"], periods=self.PERIOD)
        self.assertEqual(r["date"], "2026-10-08")
        self.assertTrue(any("日期不同" in w for w in r["warnings"]))

    def test_a_date_only_one_reader_found_needs_support(self):
        rows = ["TOTAL £9.00", "05/10/2026"]
        no_date = ["TOTAL £9.00"]
        fits = self.ex(rows, alt=no_date, exif="2026:10:05 09:00:00")
        self.assertFalse(any("只有一个" in w for w in fits["warnings"]))           # the photo's time backs it
        alone = self.ex(rows, alt=no_date)
        self.assertTrue(any("只有一个" in w for w in alone["warnings"]))

    def test_printed_date_must_fit_the_photo_and_the_trip(self):
        far = self.ex(["TOTAL £9.00", "05/10/2026"], exif="2026:10:07 18:00:00")
        self.assertTrue(any("拍摄日期" in w for w in far["warnings"]))
        near = self.ex(["TOTAL £9.00", "05/10/2026"], exif="2026:10:06 00:30:00")
        self.assertEqual(near["warnings"], [])
        outside = self.ex(["TOTAL £9.00", "01/09/2026"], periods=self.PERIOD)
        self.assertTrue(any("不在报销日期内" in w for w in outside["warnings"]))

    def test_no_printed_date_falls_back_to_the_photo(self):
        r = self.ex(["TOTAL £9.00"], exif="2026:10:05 09:00:00")
        self.assertEqual((r["date"], r["date_source"]), ("2026-10-05", "photo"))
        self.assertTrue(any("拍摄日期" in w for w in r["warnings"]))


@unittest.skipUnless(sys.platform == "win32", "Windows backend")
class TestDeviceChoice(unittest.TestCase):
    """native/devices.py picks the fastest backend that reads the same text as the CPU (fakes stand in for the models)."""
    TEXTS = ["TOTAL", "£14.30"]

    def choose(self, speeds, texts=None, cache=None, env=None):
        import time
        from unittest import mock
        from native import devices
        texts = texts or {}
        made = []

        def make(device):
            made.append(device)
            return device

        def run(engine, img):
            time.sleep(speeds[engine])
            return texts.get(engine, self.TEXTS)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(devices, "candidates", return_value=list(speeds)), \
                mock.patch.object(devices, "_fingerprint", return_value={"fake": 1}), \
                mock.patch.object(devices, "_test_images", return_value=[None] * 3), \
                mock.patch.object(devices, "CACHE", Path(cache or tmp.name) / "device.json"), \
                mock.patch.dict("os.environ", env or {}, clear=False):
            got = devices.choose(make, run, log=lambda *_: None)
        return got, made

    def test_fastest_wins(self):
        self.assertEqual(self.choose({"cpu": 0.02, "dml": 0.004, "ov-cpu": 0.01})[0], "dml")

    def test_output_that_differs_from_the_cpu_is_never_used(self):
        got, _ = self.choose({"cpu": 0.02, "dml": 0.002, "ov-gpu": 0.006}, texts={"dml": ["TOTAL", "£94.30"]})
        self.assertEqual(got, "ov-gpu")

    def test_a_backend_that_is_not_clearly_faster_is_not_worth_it(self):
        self.assertEqual(self.choose({"cpu": 0.010, "ov-gpu": 0.0095})[0], "cpu")

    def test_a_backend_that_fails_to_start_is_skipped(self):
        import time
        from unittest import mock
        from native import devices
        speeds = {"cpu": 0.02, "dml": 0.004}

        def make(device):
            if device == "dml":
                raise RuntimeError("driver")
            return device

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(devices, "candidates", return_value=list(speeds)), \
                mock.patch.object(devices, "_fingerprint", return_value={"fake": 2}), \
                mock.patch.object(devices, "_test_images", return_value=[None] * 2), \
                mock.patch.object(devices, "CACHE", Path(tmp.name) / "device.json"):
            self.assertEqual(devices.choose(make, lambda e, i: (time.sleep(0.001) or self.TEXTS), log=lambda *_: None),
                             "cpu")

    def test_the_result_is_cached_for_the_same_hardware(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        first, made1 = self.choose({"cpu": 0.02, "dml": 0.004}, cache=tmp.name)
        second, made2 = self.choose({"cpu": 0.02, "dml": 0.004}, cache=tmp.name)
        self.assertEqual((first, second), ("dml", "dml"))
        self.assertTrue(made1 and not made2)                   # the second time nothing was measured

    def test_forcing_a_device(self):
        got, made = self.choose({"cpu": 0.02, "dml": 0.004}, env={"BX_OCR_DEVICE": "cpu"})
        self.assertEqual((got, made), ("cpu", []))
        got, _ = self.choose({"cpu": 0.02, "dml": 0.004}, env={"BX_OCR_DEVICE": "ov-gpu"})   # not there: measured instead
        self.assertEqual(got, "dml")

    def test_only_the_cpu_needs_no_measuring(self):
        self.assertEqual(self.choose({"cpu": 0.01}), ("cpu", []))


if __name__ == "__main__":
    unittest.main()
