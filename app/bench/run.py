"""Accuracy benchmark: receipt photos + ground truth through the real OCR -> extract pipeline.

  python bench/run.py run [--data DIR] [--images DIR] [--tag NAME] [--stress] [--scan] [--limit N]
  python bench/run.py compare A.json B.json [--strict]
  python bench/run.py from-trip TRIP_FOLDER [--out DIR]      truth.json from a trip you already confirmed by hand
  python bench/run.py synth [OUTDIR] [--n 30] [--seed 1]     synthetic receipts + truth.json (smoke test only)

The ground truth is DATA/truth.json (same format as the unit tests): {"<first 8 chars of file name>":
{"amount": 15.81, "date": "2026-09-28" or null, "merchant": "optional", "warn": "optional text expected in a warning",
"date_printed": true/false, optional}}. With "date_printed": false the date may come from the photo's EXIF time.
`from-trip` builds it from a trip folder made by the app (every receipt in it was confirmed by a person) and also
scores the recognition stored in that trip (e.g. what macOS Vision did at import time), so other engines can be
compared with it.
Results go to output/bench/<tag>.json; `compare` shows what changed between two runs, file by file.

What matters most is not the hit rate but FALSE-CONFIDENT: wrong amount/date with no warning at all - those slip
through to a tired human. A wrong result that carries a warning is caught.
"""
import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import native  # noqa: E402
import stress  # noqa: E402
import synth  # noqa: E402
from core import scan as S  # noqa: E402
from core import trip as T  # noqa: E402
from core.extract import extract  # noqa: E402

OUT = Path(os.environ.get("BX_BENCH_OUT", APP.parent / "output" / "bench"))
SCAN_MODES = ("crop", "box", "enhance")      # same preference order as server.SCAN_MODES


# ---------------- evaluation ----------------

def norm(s):
    return "".join(c for c in (s or "").lower().replace("’", "'") if c.isalnum())


def merchant_ok(got, want):
    """OCR often drops the spaces of a name: compare without them, tolerate a character or two."""
    a, b = norm(got), norm(want)
    if not b:
        return None
    return bool(a) and (b in a or a in b or SequenceMatcher(None, a, b).ratio() >= 0.8)


def score(got, truth):
    """One receipt: the extracted fields vs the truth. Used for live runs and for results stored in a trip alike.

    date_ok:   the date VALUE is right (wherever it came from, a photo's EXIF time is fine if it is the real date)
    date_read: of the dates printed on the receipt, was it actually read from the print (not taken from the photo)?
    wrong:     amount or date value wrong. false_confident: wrong AND no warning at all (the dangerous case).
    """
    amount_ok = got["amount"] is not None and abs(got["amount"] - truth["amount"]) < 0.005
    date_ok = None if not truth.get("date") else got["date"] == truth["date"]
    printed = bool(truth.get("date")) and truth.get("date_printed", True)    # legacy truth files: a given date is printed
    date_read = (got["date_source"] == "ocr" and date_ok) if printed else None
    wrong = (not amount_ok) or date_ok is False
    flagged = bool(got["warnings"])
    return {"amount_ok": amount_ok, "date_ok": date_ok, "date_read": date_read, "flagged": flagged, "wrong": wrong,
            "false_confident": wrong and not flagged}


def evaluate(path, truth, variant, keep_raw=False, periods=None):
    t0 = time.time()
    raw = native.ocr([path])[str(path)]
    ex = extract(raw, periods=periods)
    secs = time.time() - t0
    rec = {"file": Path(path).name, "variant": variant, "truth": {k: truth.get(k) for k in ("amount", "date")},
           "amount": ex["amount"], "date": ex["date"], "date_source": ex["date_source"], "merchant": ex["merchant"],
           "exif_date": raw.get("exif_date"), "amount_conf": ex["amount_conf"], "amount_alts": ex["amount_alts"],
           "warnings": ex["warnings"], "agree": ex.get("agree"), "alt": ex.get("alt"), **score(ex, truth),
           "merchant_ok": merchant_ok(ex["merchant"], truth.get("merchant")),
           "warn_hit": None if not truth.get("warn") else any(truth["warn"] in w for w in ex["warnings"]),
           "secs": round(secs, 2)}
    return (rec, raw) if keep_raw else rec


def scan_eval(path, truth, raw):
    """Do the three scan variants read back the confirmed amount/date? (mirrors server.scan_state)"""
    orig = S.facts(raw)
    with tempfile.TemporaryDirectory() as tmp:
        metas, facts = {}, {}
        t0 = time.time()
        for mode in SCAN_MODES:
            out = Path(tmp) / f"{mode}.jpg"
            m = native.scan_image(path, out, mode)
            if m.get("ok") and out.exists():
                metas[mode], facts[mode] = m, S.facts(native.ocr([out])[str(out)])
        res = {}
        for mode in SCAN_MODES:
            if mode not in facts:
                res[mode] = {"ok": False, "how": None}
                continue
            c = S.check({"amount": truth["amount"], "date": truth.get("date")}, orig, facts[mode],
                        full=facts.get("enhance"), cropped=metas[mode].get("cropped", mode != "enhance"))
            res[mode] = {"ok": c["ok"], "how": metas[mode].get("how"), "money_cov": c["money_cov"],
                         "word_cov": c["word_cov"]}
        return {"variants": res, "adopted": next((m for m in SCAN_MODES if res[m]["ok"]), None),
                "secs": round(time.time() - t0, 2)}


def pct(a, b):
    return None if not b else round(100 * a / b, 1)


def summarize(recs):
    n = len(recs)
    amt = [r for r in recs if r["amount_ok"] is not None]
    dat = [r for r in recs if r["date_ok"] is not None]
    red = [r for r in recs if r.get("date_read") is not None]
    mer = [r for r in recs if r["merchant_ok"] is not None]
    wrong = [r for r in recs if r["wrong"]]
    scans = [r["scan"] for r in recs if r.get("scan")]
    return {"n": n,
            "amount_acc": pct(sum(r["amount_ok"] for r in amt), len(amt)),
            "date_acc": pct(sum(r["date_ok"] for r in dat), len(dat)),
            "date_read": pct(sum(bool(r["date_read"]) for r in red), len(red)),
            "merchant_acc": pct(sum(r["merchant_ok"] for r in mer), len(mer)),
            "flag_rate": pct(sum(r["flagged"] for r in recs), n),
            "false_confident": sum(r["false_confident"] for r in recs),
            "false_conf_rate": pct(sum(r["false_confident"] for r in recs), n),
            "catch_rate": pct(sum(r["flagged"] for r in wrong), len(wrong)),
            "scan_pass": pct(sum(1 for s in scans if s["adopted"]), len(scans)),
            "crop_adopted": pct(sum(1 for s in scans if s["adopted"] == "crop"), len(scans)),
            "secs_avg": round(sum(r["secs"] for r in recs) / n, 2) if n else None}


def calibration(recs):
    """Is amount_conf trustworthy? hit rate per confidence bucket."""
    out = []
    for lo, hi in ((0, 0.5), (0.5, 0.7), (0.7, 0.85), (0.85, 1.01)):
        rows = [r for r in recs if r["amount_conf"] is not None and lo <= r["amount_conf"] < hi]
        out.append({"conf": f"{lo:.2f}-{min(hi, 1):.2f}", "n": len(rows),
                    "amount_acc": pct(sum(r["amount_ok"] for r in rows), len(rows)),
                    "flagged": pct(sum(r["flagged"] for r in rows), len(rows))})
    return out


METRICS = [("n", "票数"), ("amount_acc", "金额命中 %"), ("date_acc", "日期值正确 %"), ("date_read", "印刷日期读出 %"),
           ("merchant_acc", "商户命中 %"),
           ("flag_rate", "需关注 %"), ("false_confident", "误报高把握 张"), ("false_conf_rate", "误报高把握 %"),
           ("catch_rate", "错误被提示 %"), ("scan_pass", "扫描通过 %"), ("crop_adopted", "裁边矫正采用 %"),
           ("secs_avg", "平均秒/张")]


def show(v):
    return "-" if v is None else str(v)


def print_table(columns):
    """columns: {title: summary dict}"""
    titles = list(columns)
    width = max([len(t) for t in titles] + [8])
    print(f"{'':<18}" + "".join(f"{t:>{width + 2}}" for t in titles))
    for key, label in METRICS:
        row = "".join(f"{show(columns[t].get(key)):>{width + 2}}" for t in titles)
        pad = 18 - sum(2 if ord(c) > 255 else 1 for c in label)
        print(label + " " * max(1, pad) + row)


# ---------------- commands ----------------

def cmd_run(a):
    data = Path(a.data or os.environ.get("BX_DATA") or APP.parent / "DATA")
    images = Path(a.images) if a.images else data
    truth_file = data / "truth.json"
    if not truth_file.exists():
        sys.exit(f"没有找到 {truth_file}\n把真实票据照片放进 {data}，再写 truth.json（格式见本文件开头）；"
                 f"也可以用 `python bench/run.py synth` 先生成一批合成票据做冒烟测试。")
    truth = json.loads(truth_file.read_text("utf-8"))
    periods = truth.pop("_meta", {}).get("periods")          # the trip's dates: a prior for reading dates
    files = sorted(p for p in images.iterdir() if p.is_file() and p.name[:8] in truth
                   and p.suffix.lower() in T.IMAGE_EXT)
    if a.limit:
        files = files[:a.limit]
    missing = sorted(set(truth) - {p.name[:8] for p in files})
    if missing and not a.limit:
        print(f"注意：truth.json 里这些条目没有对应的图片：{', '.join(missing)}")
    print(f"引擎：{native.engine_info()}\n数据：{images}（{len(files)} 张）  压力集：{'开' if a.stress else '关'}  "
          f"扫描检查：{'开' if a.scan else '关'}\n")
    recs = []
    stress_root = OUT / "_stress"
    for i, path in enumerate(files, 1):
        key = path.name[:8]
        rec, raw = evaluate(path, truth[key], "orig", keep_raw=True, periods=periods)
        if a.scan:
            rec["scan"] = scan_eval(path, truth[key], raw)
        recs.append(rec)
        print(f"[{i}/{len(files)}] {path.name[:28]:<28} {mark(rec)}  {rec['secs']}s", flush=True)
        if a.stress:
            for variant, p in stress.make_all(path, stress_root).items():
                r = evaluate(p, truth[key], variant, periods=periods)
                r["file"] = path.name
                recs.append(r)
    orig = [r for r in recs if r["variant"] == "orig"]
    stressed = [r for r in recs if r["variant"] != "orig"]
    columns = {"原图": summarize(orig)}
    if stressed:
        columns["压力集合计"] = summarize(stressed)
        for v in stress.VARIANTS:
            rows = [r for r in stressed if r["variant"] == v]
            if rows:
                columns[v] = summarize(rows)
    print()
    print_table(columns)
    print("\n置信度校准（原图+压力集）：")
    for row in calibration(recs):
        print(f"  conf {row['conf']}: {row['n']:>4} 张  金额命中 {show(row['amount_acc'])}%  被提示 {show(row['flagged'])}%")
    bad = [r for r in orig if r["wrong"]]
    if bad:
        print("\n原图中识别错误的：")
        for r in bad:
            print(f"  {r['file']:<28} 金额 {r['amount']} (应为 {r['truth']['amount']})  日期 {r['date']} "
                  f"(应为 {r['truth']['date']})  {'已提示' if r['flagged'] else '【无提示】'}")
    tag = a.tag or datetime.now().strftime("%Y%m%d-%H%M%S")
    OUT.mkdir(parents=True, exist_ok=True)
    dst = OUT / f"{tag}.json"
    dst.write_text(json.dumps({"tag": tag, "created": datetime.now().isoformat(timespec="seconds"),
                               "engine": native.engine_info(), "platform": sys.platform, "data": str(data),
                               "columns": columns, "calibration": calibration(recs), "files": recs},
                              ensure_ascii=False, indent=1), "utf-8")
    print(f"\n已保存：{dst}")


def mark(r):
    return ("金额ok" if r["amount_ok"] else f"金额错({r['amount']})") + " " + \
           ("日期-" if r["date_ok"] is None else "日期ok" if r["date_ok"] else f"日期错({r['date']})") + \
           (" 日期未从票面读出" if r.get("date_read") is False else "") + \
           (" 有提示" if r["flagged"] else "") + (" 【无提示的错误】" if r["false_confident"] else "")


def cmd_compare(a):
    A, B = (json.loads(Path(p).read_text("utf-8")) for p in (a.a, a.b))
    print(f"A = {A['tag']}  {A['engine']}\nB = {B['tag']}  {B['engine']}\n")
    for col in ("原图", "压力集合计"):
        if col in A["columns"] and col in B["columns"]:
            print(f"【{col}】")
            print_table({"A": A["columns"][col], "B": B["columns"][col]})
            print()
    ka = {(r["file"], r["variant"]): r for r in A["files"]}
    kb = {(r["file"], r["variant"]): r for r in B["files"]}
    regress, fixed, newfc = [], [], []
    for k in sorted(set(ka) & set(kb)):
        ra, rb = ka[k], kb[k]
        if not ra["wrong"] and rb["wrong"]:
            regress.append(k)
        elif ra["wrong"] and not rb["wrong"]:
            fixed.append(k)
        if rb["false_confident"] and not ra["false_confident"]:
            newfc.append(k)
    for title, keys in (("退步（A 对 → B 错）", regress), ("修好（A 错 → B 对）", fixed),
                        ("新增的「无提示错误」", newfc)):
        print(f"{title}：{len(keys)}")
        for f, v in keys[:40]:
            rb = kb[(f, v)]
            print(f"  {f:<28} [{v}] 金额 {rb['amount']} (应为 {rb['truth']['amount']}) 日期 {rb['date']} "
                  f"(应为 {rb['truth']['date']})")
    only = (set(ka) ^ set(kb))
    if only:
        print(f"\n只在一边出现的条目：{len(only)}")
    if a.strict and (regress or newfc):
        sys.exit(1)


def cmd_from_trip(a):
    f = Path(a.trip)
    f = f if f.is_file() else f / "trip.json"
    trip = json.loads(f.read_text("utf-8"))
    images = f.parent / "原始票据"
    out = Path(a.out or OUT / "real")
    out.mkdir(parents=True, exist_ok=True)
    truth, stored = {}, []
    # <out>/fixes.json: {"<key>": {"date": "...", "note": "..."}} where looking at the photo showed the confirmed value
    # in the trip is itself wrong (e.g. the app offered the photo's date and nobody checked the printed one)
    fixes_file = out / "fixes.json"
    fixes = json.loads(fixes_file.read_text("utf-8")) if fixes_file.exists() else {}
    for r in trip["receipts"]:
        if not r.get("include", True) or r.get("amount") is None:
            continue
        o = r.get("ocr", {})
        key = r["stored"][:8]
        read = o.get("date_source") == "ocr" and o.get("date") == r.get("date")
        truth[key] = {"amount": r["amount"], "date": r.get("date"), "orig_name": r["orig_name"],
                      "date_printed": bool(read)}      # unknown for the rest: a person may have taken it from the photo
        truth[key].update({k: v for k, v in fixes.get(key, {}).items() if k != "note"})
        # what the engine of that time produced (stored at import), scored exactly like a live run
        got = {"amount": o.get("amount"), "date": o.get("date"), "date_source": o.get("date_source"),
               "warnings": o.get("warnings", [])}
        if a.reextract:      # the stored lines through TODAY's extraction: shows what a rule change does to Vision output
            ex = extract({"lines": o["lines"], "exif_date": stress.taken_date(images / r["stored"])},
                         periods=trip.get("periods"))
            got = {k: ex[k] for k in ("amount", "date", "date_source", "warnings")}
        stored.append({"file": r["stored"], "variant": "orig", "truth": {"amount": r["amount"], "date": r.get("date")},
                       **got, "merchant": o.get("merchant"), "exif_date": None, "amount_conf": None,
                       "amount_alts": o.get("amount_alts", []), **score(got, truth[key]), "merchant_ok": None,
                       "warn_hit": None, "secs": 0.0})
    (out / "truth.json").write_text(
        json.dumps({**truth, "_meta": {"periods": trip.get("periods", [])}}, ensure_ascii=False, indent=1), "utf-8")
    tag = "stored-" + (a.tag or ("vision-reextract" if a.reextract else "import-time"))
    OUT.mkdir(parents=True, exist_ok=True)
    columns = {"原图": summarize(stored)}
    (OUT / f"{tag}.json").write_text(json.dumps({
        "tag": tag, "created": datetime.now().isoformat(timespec="seconds"),
        "engine": ("trip.json 里保存的 Vision 文字行，用当前的 extract 重新提取" if a.reextract
                   else "识别结果取自 trip.json（导入当时的引擎，macOS 上即 Vision）"), "platform": "stored",
        "data": str(images), "columns": columns, "calibration": [], "files": stored}, ensure_ascii=False, indent=1),
        "utf-8")
    print(f"{len(truth)} 张已确认的票据 -> {out / 'truth.json'}")
    print(f"导入当时的识别结果评分 -> {OUT / (tag + '.json')}\n")
    print_table(columns)
    print(f"\n用当前引擎跑：python bench/run.py run --data \"{out}\" --images \"{images}\" --stress --tag <名字>")


def cmd_synth(a):
    out = Path(a.out or OUT / "synthetic")
    truth = synth.generate(out, a.n, a.seed)
    print(f"已生成 {len(truth)} 张合成票据 + truth.json：{out}\n跑评测：python bench/run.py run --data \"{out}\" --stress")


def main():
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--data", help="folder with truth.json (and the photos, unless --images is given)")
    r.add_argument("--images", help="folder with the photos, when they are not next to truth.json")
    r.add_argument("--tag")
    r.add_argument("--stress", action="store_true", help="also run degraded copies (rotate, keystone, blur, ...)")
    r.add_argument("--scan", action="store_true", help="also check the three scan variants (slow)")
    r.add_argument("--limit", type=int, help="only the first N files")
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    c.add_argument("--strict", action="store_true", help="exit 1 on any regression or new false-confident")
    f = sub.add_parser("from-trip")
    f.add_argument("trip")
    f.add_argument("--out")
    f.add_argument("--tag")
    f.add_argument("--reextract", action="store_true", help="re-run today's extract over the stored OCR lines")
    s = sub.add_parser("synth")
    s.add_argument("out", nargs="?")
    s.add_argument("--n", type=int, default=30)
    s.add_argument("--seed", type=int, default=1)
    a = p.parse_args()
    {"run": cmd_run, "compare": cmd_compare, "from-trip": cmd_from_trip, "synth": cmd_synth}[a.cmd](a)


if __name__ == "__main__":
    main()
