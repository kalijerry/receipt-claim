"""Local web app for receipt reimbursement. Run: .venv/bin/python server.py  (Windows: .venv\\Scripts\\python server.py)
->  http://127.0.0.1:8765"""
import hashlib
import json
import mimetypes
import os
import shutil
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)           # the packaged .exe (PyInstaller)
APP = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))      # code, static files, models
ROOT = Path(sys.executable).parent if FROZEN else APP.parent               # next to the .exe / the repository
sys.path.insert(0, str(APP))
import native  # noqa: E402  (everything OS specific: OCR, scan, preview, open folder)
from core import trip as T  # noqa: E402
from core.excel import write_workbook  # noqa: E402
from core.extract import extract, locate  # noqa: E402
from core import scan as S  # noqa: E402

OUTPUT = Path(os.environ.get("BX_OUTPUT", ROOT / "output"))
STATIC = APP / "static"
LOCK = threading.Lock()
SCAN_LOCK = threading.Lock()   # one scan at a time; scanning itself runs outside LOCK

ORIG_DIR = "原始票据"
OUT_DIR = "报销票据"
SCAN_OUT_DIR = "扫描版票据"
CACHE_DIR = ".cache"


# ---------------- storage ----------------

def trip_dir(name):
    return OUTPUT / T.safe_name(name)


def load_trip(name):
    p = trip_dir(name) / "trip.json"
    if not p.exists():
        raise FileNotFoundError(name)
    return json.loads(p.read_text("utf-8"))


def save_trip(trip, touch=True):
    """touch=True marks the trip as changed (an earlier export is then out of date)."""
    if touch:
        trip["modified_ts"] = time.time()
    d = trip_dir(trip["name"])
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "trip.json.tmp"
    tmp.write_text(json.dumps(trip, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(d / "trip.json")


def list_trips():
    out = []
    if OUTPUT.exists():
        for p in sorted(OUTPUT.glob("*/trip.json"), key=lambda p: -p.stat().st_mtime):
            try:
                t = json.loads(p.read_text("utf-8"))
                out.append({"name": t["name"], "receipts": len(t["receipts"]), "periods": t["periods"]})
            except (ValueError, KeyError):
                pass
    return out


# ---------------- OCR / images ----------------

def run_ocr(paths):
    return native.ocr(paths) if paths else {}


def preview(trip_name, rec, size):
    """JPEG preview (browsers can't show HEIC). Cached per receipt + size."""
    d = trip_dir(trip_name)
    src = d / ORIG_DIR / rec["stored"]
    cache = d / CACHE_DIR / f"{rec['id']}_{size}.jpg"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(f"{cache.stem}.{uuid.uuid4().hex[:6]}.tmp.jpg")   # never serve a half-written file
        try:
            native.make_preview(src, tmp, size)
        except (OSError, ValueError):
            pass                          # unreadable image: no preview (the caller answers 404)
        if tmp.exists():
            tmp.replace(cache)
    return cache


def compact_lines(raw):
    """OCR lines kept for highlighting: text + normalized box (top-left origin)."""
    return [{"text": ln["text"], "box": [round(v, 4) for v in ln["box"]]} for ln in raw.get("lines", [])]


def ensure_lines(trip):
    """Trips imported before line boxes were stored: OCR once more and keep the boxes. Returns True if changed."""
    d = trip_dir(trip["name"]) / ORIG_DIR
    todo = [r for r in trip["receipts"] if "lines" not in r.get("ocr", {})]
    if not todo:
        return False
    ocr = run_ocr([d / r["stored"] for r in todo])
    for r in todo:
        r.setdefault("ocr", {})["lines"] = compact_lines(ocr.get(str(d / r["stored"]), {}))
    return True


def add_files(trip, files):
    """files: list of (original filename, bytes or source Path). Returns list of new receipts."""
    d = trip_dir(trip["name"]) / ORIG_DIR
    d.mkdir(parents=True, exist_ok=True)
    known = {r["sha1"]: r for r in trip["receipts"]}
    new, skipped = [], []
    for name, data in files:
        if Path(name).suffix.lower() not in T.IMAGE_EXT:
            skipped.append(f"{name}（不是图片）")
            continue
        raw = data.read_bytes() if isinstance(data, Path) else data
        sha = hashlib.sha1(raw).hexdigest()
        if sha in known:
            skipped.append(f"{name}（与已导入的 {known[sha]['orig_name']} 是同一文件，已跳过）")
            continue
        rid = uuid.uuid4().hex[:10]
        stored = f"{rid}{Path(name).suffix.lower()}"
        (d / stored).write_bytes(raw)
        rec = {"id": rid, "orig_name": Path(name).name, "stored": stored, "sha1": sha,
               "include": True, "exclude_reason": "", "group": None, "note": ""}
        known[sha] = rec
        new.append(rec)
    ocr = run_ocr([d / r["stored"] for r in new])
    for r in new:
        raw = ocr.get(str(d / r["stored"]), {"lines": []})
        ex = extract(raw, periods=trip.get("periods"))     # the trip's dates make a misread date easier to spot
        r["ocr"] = {k: ex[k] for k in ("amount", "date", "time", "merchant", "warnings",
                                       "amount_alts", "date_source", "text", "agree", "alt") if k in ex}
        r["ocr"]["lines"] = compact_lines(raw)
        r["ocr"]["facts"] = S.facts_all(raw)                # what the photo shows, for checking its scan later
        r.update(amount=ex["amount"], date=ex["date"], time=ex["time"], merchant=ex["merchant"])
    trip["receipts"].extend(new)
    T.mark_duplicates(trip["receipts"], [r["id"] for r in new])
    return new, skipped


# ---------------- scanned version ----------------

def scan_path(trip_name, rid, mode):
    return trip_dir(trip_name) / CACHE_DIR / "scan" / f"{rid}_{mode}.jpg"


SCAN_MODES = ("crop", "box", "enhance")   # preference order: cleanest first
HOW_LABEL = {"perspective": "裁边矫正", "box": "按文字区域裁切", "full": "整图增强"}


def scan_receipts(trip_name, recs):
    """Make the scan variants for each receipt, read them back -> {receipt id: scan info}.

    Pure work on files (no trip state changes), so it runs without holding LOCK.
    recs: [{"id", "stored", "sha1", "amount", "date", "facts"}]  (amount / date: what is confirmed so far;
    facts: what the original photo showed, kept from the import so it is not read a second time)

    Reading is the expensive part, so it is done in as few steps as will do:
      1. the full-frame "enhance" variant (the reference every other variant is checked against) and the cropped
         one, each read by the first reader only;
      2. a variant that does not read back the confirmed amount / date is read by the second reader as well;
      3. only when the cropped variant still fails, the "box" fallback is made and read the same way.
    """
    from concurrent.futures import ThreadPoolExecutor
    d = trip_dir(trip_name)
    (d / CACHE_DIR / "scan").mkdir(parents=True, exist_ok=True)
    src = lambda r: d / ORIG_DIR / r["stored"]  # noqa: E731
    out = lambda r, mode: scan_path(trip_name, r["id"], mode)  # noqa: E731
    metas, facts = {}, {}                        # (receipt id, mode) -> scan meta / facts read from the scan

    orig = {r["id"]: r.get("facts") for r in recs}
    unread = [r for r in recs if not orig[r["id"]]]
    if unread:
        ocr = native.ocr([src(r) for r in unread])
        for r in unread:
            orig[r["id"]] = S.facts_all(ocr.get(str(src(r)), {}))

    def make_and_read(jobs):
        def make(job):
            r, mode = job
            return (r["id"], mode), native.scan_image(src(r), out(r, mode), mode)
        with ThreadPoolExecutor(max_workers=max(2, min(6, (os.cpu_count() or 4) // 2))) as pool:
            for key, m in pool.map(make, jobs):
                metas[key] = m
        made = [(r, mode) for r, mode in jobs if metas[(r["id"], mode)].get("ok") and out(r, mode).exists()]
        facts.update(read(made, native.lead_readers()))

    def read(jobs, readers):
        res = native.ocr([out(r, mode) for r, mode in jobs], readers) if jobs else {}
        return {(r["id"], mode): S.facts(res.get(str(out(r, mode)), {})) for r, mode in jobs}

    def verdict(r, mode):
        f = facts.get((r["id"], mode))
        if f is None or r.get("amount") is None:
            return None                          # nothing to judge with
        return S.check({"amount": r["amount"], "date": r.get("date")}, orig[r["id"]], f,
                       full=facts.get((r["id"], "enhance")), cropped=metas[(r["id"], mode)].get("cropped", mode != "enhance"))

    def second_opinion(mode):
        if not native.second_readers():
            return
        todo = [(r, mode) for r in recs if (v := verdict(r, mode)) is not None and not v["ok"]]
        for key, f in read(todo, native.second_readers()).items():
            facts[key] = S.merge_facts(facts[key], f)

    make_and_read([(r, mode) for r in recs for mode in ("enhance", "crop")])
    second_opinion("enhance")                   # first: the crop is checked against it
    second_opinion("crop")
    box = [(r, "box") for r in recs if (v := verdict(r, "crop")) is None or not v["ok"]]
    if box:
        make_and_read(box)
        second_opinion("box")
    infos = {}
    for r in recs:
        info = {"orig": orig[r["id"]], "sha1": r["sha1"], "at": datetime.now().isoformat(timespec="seconds")}
        for mode in SCAN_MODES:
            key, m = (r["id"], mode), metas.get((r["id"], mode), {})
            if m.get("ok") and key in facts:
                info[mode] = {**facts[key], "w": m["w"], "h": m["h"], "cropped": m.get("cropped", False),
                              "how": m.get("how", "full"), "upscale": m.get("upscale", 1)}
        infos[r["id"]] = info
    return infos


def scan_state(r):
    """Pick the variant to use and judge it against the receipt's confirmed amount/date."""
    sc = r.get("scan")
    if not sc or sc.get("sha1") != r.get("sha1"):
        return None
    best = None
    for mode in SCAN_MODES:   # the first passing variant (cleanest) wins; otherwise the best scoring one
        v = sc.get(mode)
        if not v:
            continue
        c = S.check(r, sc["orig"], v, full=sc.get("enhance"), cropped=v.get("cropped", mode != "enhance"))
        score = (c["ok"], c["amount_ok"], c["date_ok"] is not False, c["money_cov"] + c["word_cov"])
        if best is None or (score > best[0] and not best[2]["ok"]):
            best = (score, mode, c)
    if not best:
        return {"mode": None, "ok": False, "problems": ["扫描失败"], "acked": False}
    _, mode, c = best
    ack_key = json.dumps([mode, r.get("amount"), r.get("date")])
    v = sc[mode]
    problems = S.explain(c)
    # text line height in pixels of the ORIGINAL photo (before smoothing upscale)
    low_res = v.get("line_h") and v["line_h"] * v["h"] / v.get("upscale", 1) < 14
    if low_res:
        problems.append("原图分辨率偏低，建议从相册导出原图后重新导入")
    return {"mode": mode, "how": HOW_LABEL.get(v.get("how"), "扫描"), "ok": c["ok"], "check": c, "problems": problems,
            "acked": r.get("scan_ack") == ack_key, "ack_key": ack_key, "low_res": bool(low_res)}


def pdf_receipts(trip, claims=None):
    """Receipts that go into the scanned PDF: members of claims inside the trip dates."""
    claims = claims if claims is not None else T.build_claims(trip)
    return [m for c in claims if c["in_period"] for m in c["members"]]


def needs_scan(r):
    st = scan_state(r)
    return st is None or not st["mode"]


def scan_gate(trip):
    """Problems that block export when scanned versions are in use."""
    todo, bad = [], []
    for r in pdf_receipts(trip):
        st = scan_state(r)
        if st is None:
            todo.append(r["orig_name"])
        elif not st["mode"] or (not st["ok"] and not st["acked"]):
            bad.append(r["orig_name"])
    out = []
    if todo:
        out.append(f"{len(todo)} 张票据还没有扫描版，请在第 4 步点「扫描」，或关闭扫描版")
    if bad:
        out.append(f"{len(bad)} 张扫描版与确认数据不一致，需在核对窗口中比对确认：" + "、".join(bad[:4]))
    return out


# ---------------- export ----------------

def status_of(trip):
    claims_ids = {}
    for c in T.build_claims(trip):
        for m in c["members"]:
            claims_ids[m["id"]] = c

    def f(r):
        if not r.get("include", True):
            return "未计入：" + (r.get("exclude_reason") or "手动排除")
        c = claims_ids.get(r["id"])
        if c and not c["in_period"]:
            return "期外，不计入（在 未计入 文件夹）"
        if c and c["group"] and len(c["members"]) > 1:
            return "计入（关联票，合并为一单）"
        return "计入"
    return f


def export(trip):
    problems = T.period_problems(trip["periods"])
    if not trip["periods"]:
        problems.append("还没有填写报销日期")
    if not trip["receipts"]:
        problems.append("还没有导入票据")
    todo = T.unverified(trip)
    if todo:
        problems.append(f"还有 {len(todo)} 张票据未人工核对确认：" + "、".join(r["orig_name"] for r in todo[:5])
                        + ("…" if len(todo) > 5 else ""))
    missing = [r["orig_name"] for r in trip["receipts"]
               if r.get("include", True) and (r.get("amount") is None or not r.get("date"))]
    if missing:
        problems.append("以下票据缺少金额或日期：" + "、".join(missing[:5]))
    use_scan = bool(trip.get("scan_enabled"))
    if use_scan:
        problems += scan_gate(trip)
    if problems:
        return {"ok": False, "problems": problems}
    claims = T.build_claims(trip)
    plan = T.plan_filenames(trip, claims)
    base = trip_dir(trip["name"])
    out = base / OUT_DIR
    if out.exists():
        shutil.rmtree(out)  # regenerated from 原始票据 every export
    out.mkdir(parents=True)
    for r in trip["receipts"]:
        dst = out / plan[r["id"]]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(base / ORIG_DIR / r["stored"], dst)
    xlsx = base / f"{T.safe_name(trip['name'])}_报销表.xlsx"
    pdf = base / f"{T.safe_name(trip['name'])}_扫描版.pdf"
    for old in (xlsx, pdf):          # only our own files; never a copy the user saved under another name
        old.unlink(missing_ok=True)
    scan_dir = base / SCAN_OUT_DIR
    if scan_dir.exists():
        shutil.rmtree(scan_dir)
    summ = T.summary(trip, claims)
    if use_scan:
        scan_dir.mkdir()
        items, rows = [], []
        sym = {"GBP": "£", "EUR": "€", "USD": "$", "CNY": "¥"}.get(trip.get("currency"), "")
        for c in claims:
            if not c["in_period"]:
                continue      # not reimbursed -> not in the PDF (still listed in Excel / 未计入)
            keys = []
            for m in c["members"]:
                st = scan_state(m)
                name = plan[m["id"]].rsplit(".", 1)[0] + ".jpg"
                shutil.copy2(scan_path(trip["name"], m["id"], st["mode"]), scan_dir / name)
                v = m["scan"][st["mode"]]
                items.append({"key": name, "px_w": v["w"], "px_h": v["h"], "line_h": v.get("line_h"),
                              "caption": name.rsplit(".", 1)[0]})
                keys.append(name)
            rows.append({"no": c["no"], "date": c["date"], "merchant": c["merchant"],
                         "n": f"关联 {len(c['members'])} 张" if len(c["members"]) > 1 else "1",
                         "amount": f"{sym}{c['amount']:.2f}", "keys": keys})
        periods = "、".join(f"{p['start']} 至 {p['end']}" for p in T.sort_periods(trip["periods"]))
        cover = {"head": [("报销日期", periods),
                          ("总天数 × 每日额度", f"{summ['days']} 天 × {sym}{float(trip.get('rate') or 0):.2f}"
                                               f" = {sym}{summ['cap']:.2f}"),
                          ("期内个人实付", f"{sym}{summ['spent']:.2f}（{len(rows)} 单）"),
                          ("应报销金额", f"{sym}{summ['reimburse']:.2f}"),
                          ("超出上限自付", f"{sym}{summ['over']:.2f}")]
                 + ([("期外（不计入）", f"{sym}{summ['out_of_period']:.2f}，见 Excel 与 未计入 文件夹")]
                    if summ["out_of_period"] else []),
                 "rows": rows, "total": f"合计 {sym}{summ['spent']:.2f}"}
        S.render_pdf(pdf, S.layout(items), lambda k: scan_dir / k, title=f"{trip['name']} 报销票据", cover=cover)
    def scan_note(r):
        st = scan_state(r) if use_scan and r.get("include", True) else None
        if not st:
            return ""
        return f"{st['how']}，数据一致" if st["ok"] else f"{st['how']}，已人工比对确认"
    write_workbook(xlsx, trip, claims, plan, status_of(trip), scan_note)
    trip["exported_at"] = datetime.now().isoformat(timespec="seconds")
    trip["exported_ts"] = time.time()
    save_trip(trip, touch=False)
    return {"ok": True, "folder": str(base), "excel": str(xlsx), "pdf": str(pdf) if use_scan else None,
            "summary": summ, "files": sorted(plan.values())}


def view(trip):
    """Trip + derived data for the front end."""
    claims = T.build_claims(trip)
    plan = T.plan_filenames(trip, claims)
    claim_of = {m["id"]: c for c in claims for m in c["members"]}
    for r in trip["receipts"]:
        c = claim_of.get(r["id"])
        r["_claim_no"] = c["no"] if c else None
        r["_in_period"] = c["in_period"] if c else None
        r["_filename"] = plan[r["id"]]
        r["_verified"] = T.is_verified(trip, r)
        r["_scan"] = scan_state(r)
        r["_hl"] = locate(r.get("ocr", {}).get("lines"), r.get("amount"), r.get("date"))
    return {"trip": trip, "summary": T.summary(trip, claims), "problems": T.period_problems(trip["periods"]),
            "folder": str(trip_dir(trip["name"])),
            "scan_enabled": bool(trip.get("scan_enabled")),
            "exported_at": trip.get("exported_at"),
            "export_stale": bool(trip.get("exported_ts")) and trip.get("modified_ts", 0) > trip["exported_ts"],
            "scan_gate": scan_gate(trip) if trip.get("scan_enabled") else [],
            "claims": [{"no": c["no"], "group": c["group"], "amount": c["amount"], "date": c["date"],
                        "in_period": c["in_period"], "ids": [m["id"] for m in c["members"]],
                        "verified": all(T.is_verified(trip, m) for m in c["members"])} for c in claims]}


def clean(trip):
    for r in trip["receipts"]:
        for k in [k for k in r if k.startswith("_")]:
            del r[k]
    return trip


# ---------------- HTTP ----------------

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def parts(self):
        p = urllib.parse.urlparse(self.path)
        return [urllib.parse.unquote(x) for x in p.path.strip("/").split("/")], urllib.parse.parse_qs(p.query)

    def do_GET(self):
        try:
            seg, q = self.parts()
            if seg == [""] or seg == ["index.html"]:
                return self.send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if seg == ["api", "trips"]:
                return self.send(200, list_trips())
            if len(seg) == 3 and seg[:2] == ["api", "trips"]:
                with LOCK:
                    t = load_trip(seg[2])
                    if ensure_lines(t):
                        save_trip(clean(t), touch=False)
                    return self.send(200, view(t))
            if len(seg) == 5 and seg[:2] == ["api", "trips"] and seg[3] == "scan":
                t = load_trip(seg[2])
                rec = next(r for r in t["receipts"] if r["id"] == seg[4])
                st = scan_state(rec)
                if not st or not st["mode"]:
                    return self.send(404, {"error": "还没有扫描版"})
                return self.send(200, scan_path(t["name"], rec["id"], st["mode"]).read_bytes(), "image/jpeg")
            if len(seg) == 5 and seg[:2] == ["api", "trips"] and seg[3] in ("thumb", "image"):
                t = load_trip(seg[2])
                rec = next(r for r in t["receipts"] if r["id"] == seg[4])
                img = preview(t["name"], rec, 360 if seg[3] == "thumb" else 2400)
                return self.send(200, img.read_bytes(), "image/jpeg")
            self.send(404, {"error": "not found"})
        except (FileNotFoundError, StopIteration):
            self.send(404, {"error": "找不到该出差或票据"})
        except Exception as e:  # noqa: BLE001
            self.send(500, {"error": str(e)})

    def do_POST(self):
        self.handle_write("POST")

    def do_PUT(self):
        self.handle_write("PUT")

    def do_DELETE(self):
        self.handle_write("DELETE")

    def handle_write(self, method):
        try:
            seg, q = self.parts()
            if method == "POST" and len(seg) == 4 and seg[:2] == ["api", "trips"] and seg[3] == "scan":
                return self.scan(seg[2], q)
            with LOCK:
                return self.route(method, seg, q)
        except FileNotFoundError:
            self.send(404, {"error": "找不到该出差"})
        except ValueError as e:
            self.send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self.send(500, {"error": f"{type(e).__name__}: {e}"})

    def scan(self, name, q):
        """Scanning can take a while: snapshot under LOCK, scan without it, merge results under LOCK."""
        if not SCAN_LOCK.acquire(blocking=False):
            raise ValueError("正在扫描中，请稍候")
        try:
            with LOCK:
                t = load_trip(name)
                todo = [{"id": r["id"], "stored": r["stored"], "sha1": r["sha1"], "amount": r.get("amount"),
                         "date": r.get("date"), "facts": r.get("ocr", {}).get("facts")}
                        for r in pdf_receipts(t) if q.get("all") or needs_scan(r)]
            infos = scan_receipts(t["name"], todo)
            with LOCK:
                t = load_trip(name)
                for r in t["receipts"]:
                    info = infos.get(r["id"])
                    if info and info["sha1"] == r["sha1"]:
                        r["scan"] = info
                        r.pop("scan_ack", None)
                t["scan_enabled"] = True
                save_trip(clean(t))
                v = view(t)
                sc = [r["_scan"] for r in pdf_receipts(t) if r.get("_scan")]
                return self.send(200, {**v, "scanned": len(todo), "scan_ok": sum(1 for x in sc if x["ok"]),
                                       "scan_bad": sum(1 for x in sc if not x["ok"])})
        finally:
            SCAN_LOCK.release()

    def route(self, method, seg, q):
        if seg == ["api", "trips"] and method == "POST":
            name = json.loads(self.body() or b"{}").get("name", "").strip()
            if not name:
                raise ValueError("请填写出差名称")
            if (trip_dir(name) / "trip.json").exists():
                raise ValueError("已有同名出差，请直接打开或换个名字")
            t = T.new_trip(T.safe_name(name))
            save_trip(t)
            return self.send(200, view(t))
        if len(seg) < 3 or seg[:2] != ["api", "trips"]:
            return self.send(404, {"error": "not found"})
        t = load_trip(seg[2])
        action = seg[3] if len(seg) > 3 else ""

        if method == "PUT" and not action:
            # Save edits: periods, rate, currency, groups, per-receipt fields.
            data = json.loads(self.body())
            if "periods" in data:
                T.check_periods(data["periods"])
                data["periods"] = T.sort_periods(data["periods"])
            if "rate" in data:
                try:
                    data["rate"] = float(data["rate"])
                except (TypeError, ValueError):
                    raise ValueError("每日额度必须是数字")
                if data["rate"] < 0:
                    raise ValueError("每日额度不能为负数")
            for e in data.get("receipts", []):
                T.check_receipt_edit(e)
            for k in ("periods", "rate", "currency", "groups"):
                if k in data:
                    t[k] = data[k]
            if "receipts" in data:
                edits = {r["id"]: r for r in data["receipts"]}
                for r in t["receipts"]:
                    e = edits.get(r["id"])
                    if e:
                        for k in ("amount", "date", "time", "merchant", "include", "exclude_reason", "group", "note"):
                            if k in e:
                                r[k] = e[k]
            T.normalize_groups(t)
            save_trip(clean(t))
            return self.send(200, view(t))

        if method == "POST" and action == "upload":
            name = urllib.parse.unquote(self.headers.get("X-Filename", "upload.jpg"))
            new, skipped = add_files(t, [(name, self.body())])
            save_trip(t)
            return self.send(200, {**view(t), "added": len(new), "skipped": skipped})

        if method == "POST" and action == "import-folder":
            # Explorer's "Copy as path" wraps the path in quotes
            folder = Path(json.loads(self.body()).get("path", "").strip().strip('"')).expanduser()
            if not folder.is_dir():
                raise ValueError(f"文件夹不存在：{folder}")
            files = [(p.name, p) for p in sorted(folder.iterdir())
                     if p.is_file() and p.suffix.lower() in T.IMAGE_EXT]
            if not files:
                raise ValueError("该文件夹里没有图片")
            new, skipped = add_files(t, files)
            save_trip(t)
            return self.send(200, {**view(t), "added": len(new), "skipped": skipped})

        if method == "DELETE" and action == "receipt" and len(seg) == 5:
            rec = next((r for r in t["receipts"] if r["id"] == seg[4]), None)
            if rec:
                t["receipts"].remove(rec)
                (trip_dir(t["name"]) / ORIG_DIR / rec["stored"]).unlink(missing_ok=True)
                for c in [*(trip_dir(t["name"]) / CACHE_DIR).glob(f"{rec['id']}_*"),
                          *(trip_dir(t["name"]) / CACHE_DIR / "scan").glob(f"{rec['id']}_*")]:
                    c.unlink()
                T.normalize_groups(t)
                save_trip(clean(t))
            return self.send(200, view(t))

        if method == "POST" and action == "confirm":
            data = json.loads(self.body())
            if data.get("on", True):
                bad = [r["orig_name"] for r in t["receipts"] if r["id"] in set(data.get("ids", []))
                       and r.get("include", True) and (r.get("amount") is None or not r.get("date"))]
                if bad:
                    raise ValueError("还缺金额或日期，补全后再确认：" + "、".join(bad))
            T.set_verified(t, data.get("ids", []), data.get("on", True))
            save_trip(clean(t))
            return self.send(200, view(t))

        if method == "POST" and action == "scan-enabled":
            t["scan_enabled"] = bool(json.loads(self.body()).get("on"))
            save_trip(clean(t))
            return self.send(200, view(t))

        if method == "POST" and action == "scan-ack":
            data = json.loads(self.body())
            for r in t["receipts"]:
                if r["id"] in set(data.get("ids", [])):
                    st = scan_state(r)
                    if st and st["mode"]:
                        r["scan_ack"] = st["ack_key"] if data.get("on", True) else ""
            save_trip(clean(t))
            return self.send(200, view(t))

        if method == "POST" and action == "export":
            try:
                res = export(t)
            except PermissionError as e:     # Windows refuses to replace a file that is open in another program
                res = {"ok": False, "problems": [
                    f"文件正被其他程序占用：{e.filename or e}。请先关闭已打开的 Excel / PDF / 图片，"
                    "以及资源管理器里正在查看该出差文件夹的窗口，再重新生成"]}
            return self.send(200 if res["ok"] else 400, res)

        if method == "POST" and action == "open":
            native.open_folder(trip_dir(t["name"]))
            return self.send(200, {"ok": True})

        self.send(404, {"error": "not found"})


def main():
    for stream in (sys.stdout, sys.stderr):      # a legacy console (e.g. GBK) must not crash on an odd path
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    port = int(os.environ.get("BX_PORT", 8765))
    OUTPUT.mkdir(parents=True, exist_ok=True)
    from version import __version__
    print(f"报销助手 v{__version__}", flush=True)
    print("正在准备文字识别……", flush=True)
    info = native.warmup()                       # first run: picks the fastest backend; later: a second or two
    if info:
        print(f"识别引擎：{info}", flush=True)
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    url = f"http://127.0.0.1:{port}"
    print(f"报销助手已启动：{url}   （输出目录：{OUTPUT}）  Ctrl+C 退出")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    mimetypes.init()
    main()
